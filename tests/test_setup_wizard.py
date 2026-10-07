import os, stat, tomllib

import pytest

from inboxink import cloudflare, config, credentials, schedule, setup_wizard as sw

TOKEN = "cf-token-SECRET-999"
APP_PW = "abcdefghijklmnop"


class Script:
    """A scripted terminal. input/getpass pop answers; every prompt is recorded."""

    def __init__(self, answers, interrupt_at=None):
        self.answers, self.prompts, self.said, self.interrupt_at = list(answers), [], [], interrupt_at

    def _next(self, prompt):
        self.prompts.append(prompt)
        if self.interrupt_at is not None and len(self.prompts) > self.interrupt_at:
            raise KeyboardInterrupt
        return self.answers.pop(0)

    def console(self):
        return sw.Console(input_fn=self._next, getpass_fn=self._next, out=lambda s="", end="\n": self.said.append(s + end))

    @property
    def text(self):
        return "".join(self.said)


class FakeSource:
    opened = []

    def __init__(self, cfg):
        self.cfg = cfg

    def open(self, create=True):
        FakeSource.opened.append((self.cfg.gmail.address, self.cfg.source.newsletters_only, create))

    def close(self):
        pass


class FakeClient:
    instances = []

    def __init__(self, token=None):
        assert token == TOKEN
        FakeClient.instances.append(self)

    def verify_token(self): return True
    def list_accounts(self): return [{"id": "acc1", "name": "Me"}]
    def ensure_kv_namespace(self, account): return "0123456789abcdef0123456789abcdef"
    def get_subdomain(self, account): return None
    def set_subdomain(self, account, name): return name

    def deploy(self, account, ns, sub):
        return f"https://inboxink.{sub}.workers.dev", "v1"


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_path = tmp_path / "home" / "config.toml"
    monkeypatch.setenv("INBOXINK_CONFIG", str(cfg_path))
    store = {}
    monkeypatch.setattr(credentials, "get_secret", store.get)
    monkeypatch.setattr(credentials, "set_secret", lambda n, v: store.__setitem__(n, v) or "keychain")
    monkeypatch.setattr(credentials, "set_instapaper", lambda u, p: store.__setitem__("instapaper", f"{u}:{p}") or "keychain")
    FakeSource.opened.clear()
    FakeClient.instances.clear()
    monkeypatch.setattr(sw, "get_source", lambda cfg: FakeSource(cfg))
    monkeypatch.setattr(sw.cloudflare, "Client", FakeClient)
    monkeypatch.setattr(sw.cloudflare, "probe", lambda url: (404, "v1"))
    monkeypatch.setattr(sw, "verify_instapaper", lambda u, p: True)
    monkeypatch.setattr(sw.shutil, "which", lambda n: None)
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)
    installed = []
    monkeypatch.setattr(sw.schedule, "install", lambda cfg: installed.append(cfg) or schedule.Result(True, "Scheduled."))
    monkeypatch.setattr(sw, "count_dry_run", lambda cfg: (2, 0, []))
    return dict(path=cfg_path, store=store, installed=installed)


HAPPY = [
    "Reader@Example.com",  # gmail address
    "n",                   # only for newsletters?
    f" {APP_PW[:4]} {APP_PW[4:]} ",  # app password, pasted with spaces
    TOKEN,                 # cloudflare token
    "my-letters",          # workers.dev name
    "reader@example.com",  # instapaper email
    "ip-password",         # instapaper password
    "",                    # keep updates on (default yes)
]


def test_happy_path_writes_config_and_secrets(env):
    s = Script(HAPPY)
    assert sw.run_setup(s.console()) == 0
    raw = tomllib.loads(env["path"].read_text())
    assert raw["source"] == {"kind": "imap", "newsletters_only": False}
    assert raw["gmail"]["address"] == "Reader@Example.com"
    assert raw["worker"] == {"base_url": "https://inboxink.my-letters.workers.dev", "account_id": "acc1",
                             "kv_namespace_id": "0123456789abcdef0123456789abcdef"}
    assert raw["updates"] == {"auto": True}
    assert stat.S_IMODE(os.stat(env["path"]).st_mode) == 0o600
    cfg = config.load()  # the file we wrote passes the real validation
    assert cfg.worker.account_id == "acc1"
    assert env["store"] == {"gmail_app_password": APP_PW, "cloudflare_token": TOKEN,
                            "instapaper": "reader@example.com:ip-password"}
    assert APP_PW not in env["path"].read_text() and TOKEN not in env["path"].read_text()
    assert TOKEN not in s.text and APP_PW not in s.text  # never echoed
    assert FakeSource.opened == [("Reader@Example.com", False, True)]
    assert len(env["installed"]) == 1
    for n in range(1, 9):
        assert f"Step {n} of 8" in s.text
    assert "New label..." in s.text  # filter instructions for a mixed inbox
    assert "https://myaccount.google.com/apppasswords" in s.text
    assert "2 newsletters are waiting" in s.text


def test_newsletters_only_skips_filter_instructions(env):
    answers = list(HAPPY)
    answers[1] = "y"
    s = Script(answers)
    assert sw.run_setup(s.console()) == 0
    assert tomllib.loads(env["path"].read_text())["source"]["newsletters_only"] is True
    assert "Create a new filter" not in s.text


def test_ctrl_c_stops_cleanly_and_changes_no_settings(env):
    s = Script(HAPPY, interrupt_at=4)  # dies at the Cloudflare token prompt: step 1 is done
    assert sw.run_setup(s.console()) == 130
    assert "Nothing was changed after step 1" in s.text
    assert not env["path"].exists() and not env["installed"]


def test_ctrl_c_before_anything(env):
    s = Script(HAPPY, interrupt_at=0)
    assert sw.run_setup(s.console()) == 130
    assert "Stopped. Nothing was changed." in s.text and not env["store"]


def test_rerun_offers_saved_answers(env):
    assert sw.run_setup(Script(HAPPY).console()) == 0
    before = env["path"].read_text()
    FakeClient.instances.clear()
    # address, only-newsletters, keep app password, keep Cloudflare, keep Instapaper, updates: all Enter
    s = Script([""] * 6)
    assert sw.run_setup(s.console()) == 0
    assert "Found earlier settings" in s.text
    assert tomllib.loads(env["path"].read_text())["gmail"]["address"] == "Reader@Example.com"
    assert not FakeClient.instances  # kept Cloudflare, created nothing
    assert open(str(env["path"]) + ".bak").read() == before


def test_bad_app_password_is_reasked_then_gives_up(env):
    s = Script(["me@example.com", "y", "short", "short", "short"])
    assert sw.run_setup(s.console()) == 1
    assert "16 letters" in s.text and not env["path"].exists()


def test_cloudflare_failure_stops_with_the_message(env, monkeypatch):
    class Bad(FakeClient):
        def verify_token(self):
            raise cloudflare.CloudflareError("Cloudflare did not accept the token.")
    monkeypatch.setattr(sw.cloudflare, "Client", Bad)
    s = Script(["me@example.com", "y", APP_PW, TOKEN, TOKEN, TOKEN])
    assert sw.run_setup(s.console()) == 1
    assert "did not accept the token" in s.text and not env["path"].exists()


def test_toml_round_trip():
    data = {"a": {"x": 'he said "hi"\n\\ é', "n": 3, "ok": True, "l": ["a@b.c", "d"],
                  "pub": {"digest@example.com": "The Weekly"}}, "top": "v"}
    assert tomllib.loads(sw.toml_dumps(data)) == data


def _write_cfg(extra=""):
    cfg_file = os.environ["INBOXINK_CONFIG"]
    os.makedirs(os.path.dirname(cfg_file), exist_ok=True)
    with open(cfg_file, "w") as f:
        f.write('[gmail]\naddress = "me@example.com"\n' + extra)
    return cfg_file


def test_uninstall_cancel_is_a_noop(env, monkeypatch):
    calls = []
    monkeypatch.setattr(sw.schedule, "uninstall", lambda cfg: calls.append("schedule"))
    monkeypatch.setattr(sw.credentials, "delete_secret", lambda n: calls.append(n))
    cfg_file = _write_cfg()
    for answer in ("", "n"):
        s = Script([answer])
        assert sw.run_uninstall(s.console()) == 0
        assert "Nothing was changed" in s.text
    assert calls == [] and os.path.exists(cfg_file)


def test_uninstall_defaults_to_keeping_everything_after_the_first_yes(env, monkeypatch):
    calls = []
    monkeypatch.setattr(sw.schedule, "uninstall", lambda cfg: calls.append("schedule") or True)
    monkeypatch.setattr(sw.credentials, "delete_secret", lambda n: calls.append(n))
    cfg_file = _write_cfg('[worker]\naccount_id = "a"\nkv_namespace_id = "0123456789abcdef0123456789abcdef"\n')
    env["store"]["cloudflare_token"] = TOKEN
    s = Script(["y", "", ""])  # continue, then Enter (= No) twice
    assert sw.run_uninstall(s.console()) == 0
    assert calls == ["schedule"] and os.path.exists(cfg_file)
