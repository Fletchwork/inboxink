"""A small Cloudflare REST client (standard library only), so nobody needs Node or wrangler.

It covers exactly what InboxInk needs: check the API token, find the account, create the KV
storage space, upload the Worker, give it a free workers.dev address, and write pages into KV.

The token comes from the credential store (secret name "cloudflare_token"). It is sent only in
the Authorization header: never in a command line, a log line or an error message.
"""
import hashlib, importlib.resources, json, re, secrets, time, urllib.error, urllib.parse, urllib.request  # nosemgrep: python.lang.compatibility.python37.python37-compatibility-importlib2 (needs 3.11+)

from . import __version__, credentials

API = "https://api.cloudflare.com/client/v4"
WORKER_NAME = "inboxink"
KV_TITLE = "inboxink"
KV_BINDING = "LETTERS"  # the name inboxink/worker/index.js reads (env.LETTERS)
COMPATIBILITY_DATE = "2026-10-01"  # fixed on purpose: a newer runtime must not change the Worker's behavior
VERSION_PLACEHOLDER = "__INBOXINK_WORKER_VERSION__"
VERSION_HEADER = "X-InboxInk-Version"
TOKEN_PAGE = "https://dash.cloudflare.com/profile/api-tokens"

# Plain-language names of the three permissions the token needs.
PERM_SCRIPTS = "Account > Workers Scripts > Edit"
PERM_KV = "Account > Workers KV Storage > Edit"
PERM_ACCOUNT = "Account > Account Settings > Read"

# Cloudflare 403s Python's default User-Agent (error 1010), so every request says who it is.
USER_AGENT = f"Mozilla/5.0 (compatible; inboxink/{__version__})"

_BULK_BYTES = 40_000_000  # stay well under Cloudflare's 100 MB limit per bulk request


class CloudflareError(RuntimeError):
    """A Cloudflare call failed. The message is plain English and never contains the token."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def token_link():
    """A link that opens Cloudflare's token page with InboxInk's three permissions pre-selected."""
    keys = [{"key": "workers_scripts", "type": "edit"}, {"key": "workers_kv_storage", "type": "edit"},
            {"key": "account_settings", "type": "read"}]
    query = urllib.parse.urlencode({"permissionGroupKeys": json.dumps(keys, separators=(",", ":")),
                                    "accountId": "*", "zoneId": "all", "name": "InboxInk"})
    return f"{TOKEN_PAGE}?{query}"


# --- the Worker's source and version ---------------------------------------------------------

def worker_source():
    return importlib.resources.files("inboxink").joinpath("worker", "index.js").read_text(encoding="utf-8")


def worker_version():
    """A short hash of the shipped Worker source: it changes exactly when the Worker does."""
    return hashlib.sha256(worker_source().encode()).hexdigest()[:12]


def worker_bundle():
    """The Worker as it is uploaded: the source with its version stamped in."""
    return worker_source().replace(VERSION_PLACEHOLDER, worker_version())


def build_multipart(metadata, module_name, module_text):
    """The body Cloudflare's script-upload endpoint expects. Returns (content_type, bytes)."""
    boundary = "inboxink" + secrets.token_hex(12)

    def part(name, ctype, body, filename=None):
        disp = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        return (f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: {ctype}\r\n\r\n".encode()
                + body + b"\r\n")
    body = (part("metadata", "application/json", json.dumps(metadata).encode())
            + part(module_name, "application/javascript+module", module_text.encode(), filename=module_name)
            + f"--{boundary}--\r\n".encode())
    return f"multipart/form-data; boundary={boundary}", body


def probe(base_url, timeout=15):
    """Ask a deployed Worker who it is. Returns (http_status, version or None); status 0 = no answer."""
    req = urllib.request.Request(base_url.rstrip("/") + "/", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (probe of the user's own workers.dev URL)
            return r.status, r.headers.get(VERSION_HEADER)
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get(VERSION_HEADER) if e.headers else None
    except Exception:
        return 0, None


# --- the client ------------------------------------------------------------------------------

class Client:
    def __init__(self, token=None, timeout=30):
        token = token or credentials.get_secret("cloudflare_token")
        if not token:
            raise CloudflareError("No Cloudflare token is saved yet. Run `inboxink setup` to add one.")
        self._token = token.strip()
        self.timeout = timeout

    # -- plumbing --
    def _scrub(self, text):
        return str(text).replace(self._token, "***") if self._token else str(text)

    def _fail(self, status, errors, need):
        detail = "; ".join(f"{e.get('message', '')} (code {e.get('code', '?')})" for e in errors or [])
        if status == 401:
            msg = ("Cloudflare did not accept the token. It may be mistyped, expired or deleted. "
                   f"Make a new one at {TOKEN_PAGE} and run `inboxink setup` again.")
        elif status == 403:
            msg = (f"The Cloudflare token is missing a permission: {need or 'see the list in `inboxink setup`'}. "
                   f"Edit the token at {TOKEN_PAGE} and add it, then try again.")
        elif status == 429:
            msg = "Cloudflare says we are sending too many requests. Wait a minute and try again."
        elif status and status >= 500:
            msg = "Cloudflare had a problem on its side. Try again in a few minutes."
        else:
            msg = f"Cloudflare refused the request{': ' + detail if detail else ''}."
        raise CloudflareError(self._scrub(msg), status)

    def _request(self, method, path, *, body=None, raw=None, content_type=None, query=None, need=None):
        url = API + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        data, ctype = None, None
        if body is not None:
            data, ctype = json.dumps(body).encode(), "application/json"
        elif raw is not None:
            data, ctype = raw, content_type
        last = None
        for attempt in range(3):  # a blip should not abort a setup: retry the transient ones
            headers = {"Authorization": f"Bearer {self._token}", "User-Agent": USER_AGENT, "Accept": "application/json"}
            if ctype:
                headers["Content-Type"] = ctype
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (fixed https://api.cloudflare.com base)
                    payload = json.loads(r.read().decode() or "{}")
            except urllib.error.HTTPError as e:
                try:
                    payload = json.loads(e.read().decode() or "{}")
                except Exception:
                    payload = {}
                if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                    last = e.code
                    time.sleep(1 + attempt)
                    continue
                self._fail(e.code, payload.get("errors") if isinstance(payload, dict) else None, need)
            except (urllib.error.URLError, TimeoutError, OSError):
                last = 0
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise CloudflareError("Could not reach Cloudflare. Check your internet connection and try again.") from None
            except json.JSONDecodeError:
                raise CloudflareError("Cloudflare sent a reply InboxInk could not read. Try again later.") from None
            if isinstance(payload, dict) and payload.get("success") is False:
                self._fail(200, payload.get("errors"), need)
            return payload.get("result") if isinstance(payload, dict) else payload
        raise CloudflareError(f"Cloudflare kept failing (last status {last}). Try again in a few minutes.")

    # -- token and account --
    def verify_token(self):
        """True if Cloudflare says the token is active."""
        r = self._request("GET", "/user/tokens/verify")
        if (r or {}).get("status") != "active":
            raise CloudflareError("Cloudflare says this token is not active. Make a new one and try again.")
        return True

    def list_accounts(self):
        """[{id, name}] for every account the token can see."""
        r = self._request("GET", "/accounts", need=PERM_ACCOUNT) or []
        return [{"id": a["id"], "name": a.get("name", "")} for a in r]

    # -- KV --
    def list_kv_namespaces(self, account_id):
        r = self._request("GET", f"/accounts/{account_id}/storage/kv/namespaces", query={"per_page": 100}, need=PERM_KV)
        return [{"id": n["id"], "title": n.get("title", "")} for n in r or []]

    def create_kv_namespace(self, account_id, title=KV_TITLE):
        r = self._request("POST", f"/accounts/{account_id}/storage/kv/namespaces", body={"title": title}, need=PERM_KV)
        return r["id"]

    def ensure_kv_namespace(self, account_id, title=KV_TITLE):
        """The id of the namespace called `title`, creating it only if it does not exist (re-runs reuse it)."""
        for n in self.list_kv_namespaces(account_id):
            if n["title"] == title:
                return n["id"]
        return self.create_kv_namespace(account_id, title)

    def delete_kv_namespace(self, account_id, namespace_id):
        self._request("DELETE", f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}", need=PERM_KV)

    def kv_bulk_put(self, account_id, namespace_id, entries):
        """Write [{key, value, expiration_ttl[, base64]}] to KV, in batches under Cloudflare's size limit."""
        batch, size = [], 0
        for e in [*entries, None]:
            n = len(e["value"]) + len(e["key"]) + 100 if e else 0
            if batch and (e is None or size + n > _BULK_BYTES):
                self._request("PUT", f"/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/bulk",
                              body=batch, need=PERM_KV)
                batch, size = [], 0
            if e is not None:
                batch.append(e)
                size += n

    # -- the Worker and its address --
    def upload_worker(self, account_id, namespace_id, name=WORKER_NAME):
        metadata = {"main_module": "worker.mjs", "compatibility_date": COMPATIBILITY_DATE,
                    "bindings": [{"type": "kv_namespace", "name": KV_BINDING, "namespace_id": namespace_id}]}
        ctype, body = build_multipart(metadata, "worker.mjs", worker_bundle())
        self._request("PUT", f"/accounts/{account_id}/workers/scripts/{name}", raw=body, content_type=ctype,
                      need=PERM_SCRIPTS)
        return worker_version()

    def delete_worker(self, account_id, name=WORKER_NAME):
        self._request("DELETE", f"/accounts/{account_id}/workers/scripts/{name}", need=PERM_SCRIPTS)

    def get_subdomain(self, account_id):
        """The account's workers.dev name, or None if it has not chosen one."""
        try:
            r = self._request("GET", f"/accounts/{account_id}/workers/subdomain", need=PERM_SCRIPTS)
        except CloudflareError as e:
            if e.status in (404, 400):
                return None
            raise
        return (r or {}).get("subdomain") or None

    def set_subdomain(self, account_id, subdomain):
        r = self._request("PUT", f"/accounts/{account_id}/workers/subdomain", body={"subdomain": subdomain},
                          need=PERM_SCRIPTS)
        return (r or {}).get("subdomain") or subdomain

    def enable_workers_dev(self, account_id, name=WORKER_NAME):
        self._request("POST", f"/accounts/{account_id}/workers/scripts/{name}/subdomain", body={"enabled": True},
                      need=PERM_SCRIPTS)

    def deploy(self, account_id, namespace_id, subdomain, name=WORKER_NAME):
        """Upload the Worker and switch on its workers.dev address. Returns (base_url, version)."""
        version = self.upload_worker(account_id, namespace_id, name)
        self.enable_workers_dev(account_id, name)
        return f"https://{name}.{subdomain}.workers.dev", version


SUBDOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
