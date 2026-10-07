import io, json, urllib.error

import pytest

from inboxink import cloudflare, credentials

TOKEN = "tok-SECRET-0123456789"  # gitleaks:allow (fake token for tests)


class Resp:
    def __init__(self, payload, status=200, headers=None):
        self._body = json.dumps(payload).encode()
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code, payload):
    return urllib.error.HTTPError("https://api.cloudflare.com/x", code, "err", {}, io.BytesIO(json.dumps(payload).encode()))


@pytest.fixture
def api(monkeypatch):
    """Records every request; replies come from the `replies` list (a Resp or an exception)."""
    calls, replies = [], []

    def fake_urlopen(req, timeout=None):
        calls.append({"method": req.get_method(), "url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                      "data": req.data})
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cloudflare.time, "sleep", lambda s: None)
    api = type("Api", (), {})()
    api.calls, api.replies = calls, replies
    return api


def ok(result):
    return Resp({"success": True, "errors": [], "result": result})


def test_token_only_in_authorization_header(api):
    api.replies.append(ok({"status": "active"}))
    assert cloudflare.Client(TOKEN).verify_token()
    c = api.calls[0]
    assert c["url"] == "https://api.cloudflare.com/client/v4/user/tokens/verify"
    assert c["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in c["url"] and "Python-urllib" not in c["headers"]["user-agent"]


def test_client_reads_token_from_credentials(api, monkeypatch):
    monkeypatch.setattr(credentials, "get_secret", lambda n: TOKEN if n == "cloudflare_token" else None)
    api.replies.append(ok([{"id": "acc1", "name": "Me"}]))
    assert cloudflare.Client().list_accounts() == [{"id": "acc1", "name": "Me"}]


def test_missing_token_message(monkeypatch):
    monkeypatch.setattr(credentials, "get_secret", lambda n: None)
    with pytest.raises(cloudflare.CloudflareError, match="inboxink setup"):
        cloudflare.Client()


def test_403_names_the_missing_permission_and_never_the_token(api):
    api.replies.append(http_error(403, {"success": False, "errors": [{"code": 10000, "message": f"nope {TOKEN}"}]}))
    with pytest.raises(cloudflare.CloudflareError) as e:
        cloudflare.Client(TOKEN).create_kv_namespace("acc1")
    assert "missing a permission" in str(e.value) and "Workers KV Storage" in str(e.value)
    assert TOKEN not in str(e.value) and e.value.status == 403


def test_401_message(api):
    api.replies.append(http_error(401, {"success": False, "errors": []}))
    with pytest.raises(cloudflare.CloudflareError, match="did not accept the token"):
        cloudflare.Client(TOKEN).list_accounts()


def test_error_text_echoing_the_token_is_scrubbed(api):
    api.replies.append(http_error(400, {"success": False, "errors": [{"code": 1, "message": f"bad {TOKEN} value"}]}))
    with pytest.raises(cloudflare.CloudflareError) as e:
        cloudflare.Client(TOKEN).create_kv_namespace("acc1")
    assert TOKEN not in str(e.value) and "***" in str(e.value)


def test_transient_failure_is_retried(api):
    api.replies += [http_error(503, {}), ok({"status": "active"})]
    assert cloudflare.Client(TOKEN).verify_token()
    assert len(api.calls) == 2


def test_network_failure_message(api):
    api.replies += [urllib.error.URLError("down")] * 3
    with pytest.raises(cloudflare.CloudflareError, match="Could not reach Cloudflare"):
        cloudflare.Client(TOKEN).verify_token()


def parse_multipart(call):
    ctype = call["headers"]["content-type"]
    boundary = ctype.split("boundary=")[1]
    parts = {}
    for chunk in call["data"].decode().split("--" + boundary)[1:-1]:
        head, body = chunk.strip("\r\n").split("\r\n\r\n", 1)
        name = head.split('name="')[1].split('"')[0]
        parts[name] = (head, body)
    return ctype, parts


def test_upload_worker_multipart_shape(api):
    api.replies.append(ok({}))
    version = cloudflare.Client(TOKEN).upload_worker("acc1", "ns123")
    c = api.calls[0]
    assert c["method"] == "PUT" and c["url"].endswith("/accounts/acc1/workers/scripts/inboxink")
    ctype, parts = parse_multipart(c)
    assert ctype.startswith("multipart/form-data; boundary=")
    meta = json.loads(parts["metadata"][1])
    assert meta["main_module"] == "worker.mjs" and meta["compatibility_date"] == cloudflare.COMPATIBILITY_DATE
    assert meta["bindings"] == [{"type": "kv_namespace", "name": "LETTERS", "namespace_id": "ns123"}]
    head, js = parts["worker.mjs"]
    assert "Content-Type: application/javascript+module" in head and 'filename="worker.mjs"' in head
    assert version in js and cloudflare.VERSION_PLACEHOLDER not in js
    assert TOKEN not in c["data"].decode()


def test_worker_version_tracks_source(monkeypatch):
    v1 = cloudflare.worker_version()
    monkeypatch.setattr(cloudflare, "worker_source", lambda: "export default {};")
    assert cloudflare.worker_version() != v1 and len(v1) == 12


def test_ensure_kv_reuses_existing(api):
    api.replies.append(ok([{"id": "nsA", "title": "other"}, {"id": "nsB", "title": "inboxink"}]))
    assert cloudflare.Client(TOKEN).ensure_kv_namespace("acc1") == "nsB"
    assert len(api.calls) == 1


def test_ensure_kv_creates_when_missing(api):
    api.replies += [ok([]), ok({"id": "nsNew", "title": "inboxink"})]
    assert cloudflare.Client(TOKEN).ensure_kv_namespace("acc1") == "nsNew"
    assert api.calls[1]["method"] == "POST" and json.loads(api.calls[1]["data"]) == {"title": "inboxink"}


def test_subdomain_none_when_unset(api):
    api.replies.append(http_error(404, {"success": False, "errors": [{"code": 10007, "message": "none"}]}))
    assert cloudflare.Client(TOKEN).get_subdomain("acc1") is None


def test_deploy_builds_url_and_enables_workers_dev(api):
    api.replies += [ok({}), ok({})]
    url, version = cloudflare.Client(TOKEN).deploy("acc1", "ns1", "mine")
    assert url == "https://inboxink.mine.workers.dev"
    assert api.calls[1]["url"].endswith("/workers/scripts/inboxink/subdomain")
    assert json.loads(api.calls[1]["data"]) == {"enabled": True}


def test_kv_bulk_put_body_and_batching(api, monkeypatch):
    api.replies += [ok(None), ok(None)]
    monkeypatch.setattr(cloudflare, "_BULK_BYTES", 500)
    entries = [{"key": f"k{i}", "value": "x" * 100, "expiration_ttl": 60} for i in range(4)]
    cloudflare.Client(TOKEN).kv_bulk_put("acc1", "ns1", entries)
    assert len(api.calls) == 2 and api.calls[0]["method"] == "PUT" and api.calls[0]["url"].endswith("/namespaces/ns1/bulk")
    sent = [e for c in api.calls for e in json.loads(c["data"])]
    assert sent == entries


def test_probe_reads_version_header_off_a_404(monkeypatch):
    err = urllib.error.HTTPError("https://w", 404, "nf", {"X-InboxInk-Version": "abc123"}, io.BytesIO(b"Not found"))

    def boom(req, timeout=None):
        raise err
    monkeypatch.setattr(cloudflare.urllib.request, "urlopen", boom)
    assert cloudflare.probe("https://w.example") == (404, "abc123")


def test_token_link_prefills_three_permissions():
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(cloudflare.token_link()).query)
    keys = {(k["key"], k["type"]) for k in json.loads(q["permissionGroupKeys"][0])}
    assert keys == {("workers_scripts", "edit"), ("workers_kv_storage", "edit"), ("account_settings", "read")}
    assert q["name"] == ["InboxInk"]
