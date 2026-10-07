import pytest

from inboxink import publish


class _Resp:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_status_sends_browser_user_agent(monkeypatch):
    # Cloudflare 403s Python-urllib's default UA (error 1010); losing the header breaks every send.
    seen = {}

    def fake_urlopen(req, timeout):
        seen["ua"] = req.get_header("User-agent")
        return _Resp()

    monkeypatch.setattr(publish.urllib.request, "urlopen", fake_urlopen)
    assert publish._status("https://example.invalid/a/x") == 200
    assert seen["ua"] and "Python-urllib" not in seen["ua"]


def test_wait_live_reports_last_status(monkeypatch):
    monkeypatch.setattr(publish, "_status", lambda u: 403)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match=r"last status 403"):
        publish.wait_live("abc", [], timeout=0.01)


def test_wait_live_error_never_contains_a_page_link(monkeypatch):
    monkeypatch.setattr(publish, "_status", lambda u: 404)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError) as e:
        publish.wait_live("abc", ["abc-1"], timeout=0.01)
    msg = str(e.value)
    assert "2 page or image link(s) never went live (last status 404)" in msg and "inboxink doctor" in msg
    assert "letters.invalid" not in msg and "http" not in msg


def test_upload_writes_one_bulk_put_through_the_api(pinned_config, monkeypatch):
    import base64, dataclasses
    from inboxink import config
    cfg = dataclasses.replace(pinned_config, worker=dataclasses.replace(pinned_config.worker, account_id="acc1"))
    monkeypatch.setattr(config, "_current", cfg)
    seen = {}

    class FakeClient:
        def kv_bulk_put(self, account, ns, entries):
            seen.update(account=account, ns=ns, entries=entries)
    monkeypatch.setattr(publish.cloudflare, "Client", FakeClient)
    url = publish.upload("a" * 32, "<p>hi</p>", {"a" * 32 + "-1": b"\xff\xd8jpeg"})
    assert url == "https://letters.invalid/a/" + "a" * 32
    assert seen["account"] == "acc1" and seen["ns"] == cfg.worker.kv_namespace_id
    page, img = seen["entries"]
    assert page["value"] == "<p>hi</p>" and page["expiration_ttl"] == cfg.worker.ttl_days * 86400
    assert img["base64"] is True and base64.b64decode(img["value"]) == b"\xff\xd8jpeg"


def test_upload_needs_account_id(pinned_config):
    with pytest.raises(RuntimeError, match="account_id"):
        publish.upload("a" * 32, "x", {})


def test_wait_live_returns_once_every_url_is_200(monkeypatch):
    codes = iter([404, 200])
    monkeypatch.setattr(publish, "_status", lambda u: next(codes))
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    publish.wait_live("abc", [])
