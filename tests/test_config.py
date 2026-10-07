import dataclasses, pathlib, re

import pytest

from inboxink import config
from inboxink.config import ConfigError, load

REQUIRED = """
[gmail]
address = "Me@Example.com"
[worker]
base_url = "https://inboxink.example.workers.dev/"
kv_namespace_id = "0123456789abcdef0123456789abcdef"
"""


def write(tmp_path, text):
    p = tmp_path / "config.toml"
    p.write_text(text)
    return str(p)


def test_defaults(tmp_path):
    cfg = load(write(tmp_path, REQUIRED), env={})
    assert cfg.source.kind == "imap" and cfg.source.newsletters_only is False
    assert cfg.gmail.labels.source == "Newsletter"
    assert cfg.gmail.labels.done == "Newsletter/Delivered"
    assert cfg.gmail.labels.failed == "Newsletter/Failed"
    assert cfg.gmail.archive_on_done is True
    assert cfg.gog.bin == "gog"
    assert cfg.worker.ttl_days == 30 and cfg.worker.account_id == ""
    assert cfg.worker.base_url == "https://inboxink.example.workers.dev"  # trailing slash trimmed
    assert cfg.reader.max_image_width == 1264 and cfg.reader.timezone == ""
    assert cfg.reader.own_addresses == ("me@example.com",)  # gmail.address included, lowercased
    assert cfg.reader.publication_names == {}
    assert cfg.cover.font == ""
    home = pathlib.Path.home()
    assert cfg.paths.state_dir == str(home / ".local/state/inboxink")
    assert cfg.paths.cache_dir == str(home / ".cache/inboxink")
    assert cfg.paths.log == str(home / ".local/state/inboxink/inboxink.log")
    assert cfg.paths.state_file.endswith("inboxink/state.json")
    assert cfg.instapaper.env_file == "" and cfg.instapaper.keychain_service == "inboxink-instapaper"
    assert cfg.adstrip.model == "haiku" and cfg.adstrip.enabled is False  # opt-in, even with `claude` installed
    assert cfg.schedule.interval_minutes == 30 and cfg.schedule.label == "io.github.fletchwork.inboxink"
    assert cfg.updates.auto is True
    assert cfg.tzinfo is None  # system zone


def test_config_is_frozen(tmp_path):
    cfg = load(write(tmp_path, REQUIRED), env={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.worker = None


def test_toml_values_load(tmp_path):
    cfg = load(write(tmp_path, REQUIRED + """
[source]
newsletters_only = true
[gmail.labels]
done = "Letters/Done"
[reader]
own_addresses = ["Other@Example.com"]
publication_names = { "Digest@Example.com" = "Example Digest" }
timezone = "America/Chicago"
max_image_width = 800
[paths]
state_dir = "/tmp/inboxink-test-state"
[adstrip]
enabled = false
"""), env={})
    assert cfg.source.newsletters_only is True
    assert cfg.gmail.labels.done == "Letters/Done" and cfg.gmail.labels.source == "Newsletter"
    assert cfg.reader.own_addresses == ("other@example.com", "me@example.com")
    assert cfg.reader.publication_names == {"digest@example.com": "Example Digest"}
    assert cfg.tzinfo is not None and cfg.reader.max_image_width == 800
    assert cfg.paths.state_dir == "/tmp/inboxink-test-state"
    assert cfg.adstrip.enabled is False


def test_env_overrides_file(tmp_path):
    env = {"INBOXINK_WORKER_TTL_DAYS": "7", "INBOXINK_GMAIL_LABELS_DONE": "From/Env",
           "INBOXINK_SOURCE_NEWSLETTERS_ONLY": "yes", "INBOXINK_WORKER_BASE_URL": "https://env.example.dev"}
    cfg = load(write(tmp_path, REQUIRED + "[gmail.labels]\ndone = \"From/File\"\n"), env=env)
    assert cfg.worker.ttl_days == 7
    assert cfg.gmail.labels.done == "From/Env"  # env beats the file
    assert cfg.source.newsletters_only is True
    assert cfg.worker.base_url == "https://env.example.dev"


def test_env_alone_can_supply_required_keys(tmp_path):
    env = {"INBOXINK_GMAIL_ADDRESS": "me@example.com", "INBOXINK_WORKER_BASE_URL": "https://w.example.dev",
           "INBOXINK_WORKER_KV_NAMESPACE_ID": "0123456789abcdef0123456789abcdef"}
    cfg = load(str(tmp_path / "does-not-exist.toml"), env=env)
    assert cfg.worker.base_url == "https://w.example.dev"


def test_inboxink_config_env_picks_the_file(tmp_path):
    path = write(tmp_path, REQUIRED)
    assert load(env={"INBOXINK_CONFIG": path}).config_file == path


def test_bad_env_value_names_the_variable(tmp_path):
    with pytest.raises(ConfigError, match="INBOXINK_WORKER_TTL_DAYS"):
        load(write(tmp_path, REQUIRED), env={"INBOXINK_WORKER_TTL_DAYS": "soon"})


def test_missing_required_key_names_key_file_and_example(tmp_path):
    path = write(tmp_path, '[gmail]\naddress = "me@example.com"\n')
    with pytest.raises(ConfigError) as e:
        load(path, env={})
    msg = str(e.value)
    assert "worker.base_url" in msg and "worker.kv_namespace_id" in msg
    assert path in msg and "https://github.com/Fletchwork/inboxink/blob/main/config.example.toml" in msg


def test_missing_file_is_reported_with_its_path(tmp_path):
    path = str(tmp_path / "nope" / "config.toml")
    with pytest.raises(ConfigError) as e:
        load(path, env={})
    assert path in str(e.value) and "does not exist" in str(e.value)


def test_gog_kind_requires_account_not_address(tmp_path):
    base = REQUIRED.replace('[gmail]\naddress = "Me@Example.com"\n', "")
    with pytest.raises(ConfigError, match="gog.account"):
        load(write(tmp_path, '[source]\nkind = "gog"\n' + base), env={})
    assert load(write(tmp_path, '[source]\nkind = "gog"\n[gog]\naccount = "me"\n' + base), env={}).gog.account == "me"


REQUIRED_ENV = {"INBOXINK_GMAIL_ADDRESS": "me@example.com", "INBOXINK_WORKER_BASE_URL": "https://w.example.dev",
                "INBOXINK_WORKER_KV_NAMESPACE_ID": "0123456789abcdef0123456789abcdef"}


@pytest.mark.parametrize("text,fragment", [
    ('[source]\nkind = "pop3"\n', "source.kind"),
    ('[worker]\nttl_days = "30"\n', "worker.ttl_days"),
    ('[reader]\ntimezone = "Mars/Olympus"\n', "reader.timezone"),
    ('[reader]\nown_addresses = "me@example.com"\n', "reader.own_addresses"),
    ('[worker]\nttl_dayz = 5\n', "worker.ttl_dayz"),
    ('[wrker]\nx = 1\n', "wrker"),
    ('[gmail.labels]\ndone = "Newsletter"\n', "gmail.labels"),
])
def test_bad_values_are_rejected_with_the_key_named(tmp_path, text, fragment):
    with pytest.raises(ConfigError, match=re.escape(fragment)):
        load(write(tmp_path, text), env=REQUIRED_ENV)


def test_invalid_toml_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(write(tmp_path, "this is = = not toml"), env={})


def test_get_caches_and_set_current_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_current", None)
    monkeypatch.setenv("INBOXINK_CONFIG", write(tmp_path, REQUIRED))
    first = config.get()
    assert config.get() is first


def _example_text():
    return (pathlib.Path(__file__).resolve().parent.parent / "config.example.toml").read_text()


def test_example_file_loads_once_placeholders_are_filled(tmp_path):
    text = _example_text().replace("REPLACE-WITH-32-CHARACTER-KV-NAMESPACE-ID", "0123456789abcdef0123456789abcdef")
    assert load(write(tmp_path, text), env={}).gmail.address == "you@gmail.com"


def test_example_file_documents_only_real_keys(tmp_path):
    # Uncomment every "# key = value" line; loading proves each documented key exists and has the right type.
    text = _example_text().replace("REPLACE-WITH-32-CHARACTER-KV-NAMESPACE-ID", "0123456789abcdef0123456789abcdef")
    text = re.sub(r"^# ([a-z_]+ = .*)$", r"\1", text, flags=re.M)
    cfg = load(write(tmp_path, text), env={})
    assert cfg.reader.publication_names == {"digest@example.com": "The Weekly Widget"}
    # every field of every section appears in the example
    def check(cls, prefix):
        for f in dataclasses.fields(cls):
            if f.name == "config_file":
                continue
            if dataclasses.is_dataclass(f.type):
                check(f.type, prefix + f.name + ".")
            else:
                assert re.search(rf"^{f.name} = ", text, re.M), f"{prefix}{f.name} missing from config.example.toml"
    check(config.Config, "")
