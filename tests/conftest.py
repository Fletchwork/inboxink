"""Every test runs against a fixed, invented config, never the developer's real one."""
import pytest

from inboxink import config

TEST_TOML = """
[source]
kind = "gog"

[gog]
account = "reader@example.com"
bin = "/usr/bin/true"

[gmail]
address = "reader@example.com"

[worker]
base_url = "https://letters.invalid"
kv_namespace_id = "0123456789abcdef0123456789abcdef"

[reader]
own_addresses = ["Reader@Example.com"]
publication_names = { "digest@example.com" = "Example Digest" }
timezone = "America/Chicago"

[adstrip]
enabled = false
"""


@pytest.fixture(autouse=True)
def pinned_config(tmp_path, monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("INBOXINK_"):
            monkeypatch.delenv(k)
    toml = tmp_path / "config.toml"
    toml.write_text(TEST_TOML + f'\n[paths]\nstate_dir = "{tmp_path}/state"\ncache_dir = "{tmp_path}/cache"\n'
                    f'log = "{tmp_path}/inboxink.log"\n')
    cfg = config.load(str(toml), env={})
    monkeypatch.setattr(config, "_current", cfg)
    monkeypatch.setenv("INBOXINK_CONFIG", str(toml))  # credentials.py keeps its fallback file beside it
    return cfg
