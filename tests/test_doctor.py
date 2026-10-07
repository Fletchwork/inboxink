import dataclasses

from inboxink import cloudflare, config, doctor


def test_config_error_is_reported_with_a_fix(monkeypatch):
    def bad():
        raise config.ConfigError("missing required setting(s) in x:\n  - gmail.address")
    monkeypatch.setattr(doctor.config, "load", bad)
    results = doctor.run_checks()
    assert results[0][0] is False and "run `inboxink setup`" in results[0][2]


def test_checks_report_each_piece(pinned_config, monkeypatch):
    monkeypatch.setattr(doctor.config, "load", lambda: pinned_config)
    monkeypatch.setattr(doctor.credentials, "get_secret", lambda n: None)
    monkeypatch.setattr(doctor.cloudflare, "probe", lambda url: (0, None))
    monkeypatch.setattr(doctor.updater, "fetch_latest", lambda timeout=5: None)
    monkeypatch.setattr(doctor.schedule, "is_installed", lambda cfg: False)
    monkeypatch.setattr(doctor.cloudflare.Client, "__init__", lambda self, token=None: (_ for _ in ()).throw(
        cloudflare.CloudflareError("No Cloudflare token is saved yet.")))
    out = []
    rc = doctor.execute(out=out.append)
    text = "\n".join(out)
    assert rc == 1
    for piece in ("Passwords and tokens: missing", "Cloudflare token", "Worker", "Schedule", "InboxInk version"):
        assert piece in text
    assert "fix:" in text and "\u2717" in text
