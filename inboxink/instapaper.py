"""Save a URL to Instapaper through the Simple API (URL-only saves are the kind Kobo syncs)."""
import base64, urllib.error, urllib.parse, urllib.request

from . import credentials


def check_login():
    """Raise before any work if there is no login; a missing login must not burn retry attempts."""
    try:
        credentials.get_instapaper()
    except credentials.CredentialError as e:
        raise RuntimeError(str(e)) from None


def add(url, title):
    user, pw = credentials.get_instapaper()
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    data = urllib.parse.urlencode({"url": url, "title": title}).encode()
    req = urllib.request.Request("https://www.instapaper.com/api/add", data=data,
                                 headers={"Authorization": f"Basic {auth}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (fixed https://www.instapaper.com endpoint)
            if r.status != 201:
                raise RuntimeError(f"Instapaper returned {r.status}")
    except urllib.error.HTTPError as e:
        hint = {400: "bad request or rate limited", 403: "invalid login", 500: "Instapaper error"}.get(e.code, "")
        raise RuntimeError(f"Instapaper {e.code} {hint}") from None
