import json, os, stat

import pytest

from inboxink import config, credentials


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """Force the 0600 file path so tests never touch a real keychain."""
    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.setattr(credentials, "_instapaper_from_keychain_item", lambda service: ("", ""))


def test_file_fallback_round_trip_is_private():
    where = credentials.set_secret("cloudflare_token", "tok-123")
    assert where.endswith("secrets.json")
    assert stat.S_IMODE(os.stat(where).st_mode) == 0o600
    assert credentials.get_secret("cloudflare_token") == "tok-123"
    credentials.delete_secret("cloudflare_token")
    assert credentials.get_secret("cloudflare_token") is None


def test_unknown_name_and_empty_value_refused():
    with pytest.raises(ValueError):
        credentials.get_secret("nope")
    with pytest.raises(ValueError):
        credentials.set_secret("instapaper", "")


def test_group_readable_secrets_file_is_refused_without_leaking():
    where = credentials.set_secret("gmail_app_password", "hunter2-hunter2")
    os.chmod(where, 0o644)
    with pytest.raises(credentials.CredentialError) as e:
        credentials.get_secret("gmail_app_password")
    assert "hunter2" not in str(e.value) and where in str(e.value)


def test_instapaper_from_store():
    credentials.set_instapaper("me@example.com", "pw!")
    assert credentials.get_instapaper() == ("me@example.com", "pw!")


def test_instapaper_legacy_env_file_wins_when_configured(tmp_path, pinned_config):
    from dataclasses import replace
    f = tmp_path / "ip.env"
    f.write_text('INSTAPAPER_USER="old@example.com"\nINSTAPAPER_PASSWORD=oldpw\n')
    f.chmod(0o600)
    credentials.set_instapaper("new@example.com", "newpw")
    cfg = replace(pinned_config, instapaper=replace(pinned_config.instapaper, env_file=str(f)))
    assert credentials.get_instapaper(cfg) == ("old@example.com", "oldpw")
    f.chmod(0o644)
    with pytest.raises(credentials.CredentialError, match="readable by other users"):
        credentials.get_instapaper(cfg)


def test_missing_instapaper_login_message_has_no_secret_and_points_to_setup():
    with pytest.raises(credentials.CredentialError, match="inboxink setup"):
        credentials.get_instapaper()
