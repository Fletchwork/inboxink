"""Upload a cleaned issue to the InboxInk Worker's KV and wait until it is publicly readable."""
import base64, time, urllib.error, urllib.request

from . import cloudflare, config


def upload(article_id, doc, images):
    cfg = config.get()
    if not cfg.worker.account_id:
        raise RuntimeError("worker.account_id is not set in the config; run `inboxink setup` "
                           "(or add your Cloudflare account id under [worker])")
    ttl = cfg.worker.ttl_days * 86400  # Instapaper keeps its own copy after saving; this only covers reading lag
    entries = [{"key": article_id, "value": doc, "expiration_ttl": ttl}]
    entries += [{"key": k, "value": base64.b64encode(v).decode(), "base64": True, "expiration_ttl": ttl}
                for k, v in images.items()]
    cloudflare.Client().kv_bulk_put(cfg.worker.account_id, cfg.worker.kv_namespace_id, entries)
    return f"{cfg.worker.base_url}/a/{article_id}"


# Cloudflare answers Python's default "Python-urllib" User-Agent with 403 (error 1010, browser
# signature ban), so the check must send a browser-like one or nothing ever looks live.
# Distinct from safety.UA (third-party fetches) so this check is identifiable in Worker logs.
UA = {"User-Agent": "Mozilla/5.0 (inboxink publish check)"}


def _status(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD", headers=UA), timeout=15) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (pages on the user's own Worker, http(s) checked in config)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def wait_live(article_id, images, timeout=240):
    """KV negative-caches a just-missed key for ~60s; Instapaper fetches once, so every URL must be 200 first."""
    base = config.get().worker.base_url
    urls = [f"{base}/a/{article_id}"] + [f"{base}/i/{k}.jpg" for k in images]
    deadline = time.time() + timeout
    last = {}
    while urls and time.time() < deadline:
        last = {u: _status(u) for u in urls}
        urls = [u for u, code in last.items() if code != 200]
        if urls:
            time.sleep(15)
    if urls:
        status = last[urls[0]]  # the page links are private to the reader, so they never go in an error or the log
        raise RuntimeError(f"{len(urls)} page or image link(s) never went live (last status {status}). "
                           "Run 'inboxink doctor' to check the Worker.")
