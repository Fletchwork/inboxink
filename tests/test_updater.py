import argparse, time
from types import SimpleNamespace

import pytest

from inboxink import updater


def test_parse_version_stable_only():
    assert updater.parse_version("1.2.3") == (1, 2, 3)
    assert updater.parse_version("0.10") == (0, 10)
    for bad in ("1.2.0rc1", "1.0.0a1", "2.0.dev3", "1.0.post1", "", None):
        assert updater.parse_version(bad) is None


def test_latest_stable_ignores_prereleases_and_yanked():
    pypi = {"releases": {"0.1.0": [{}], "0.2.0": [{}], "0.3.0rc1": [{}], "0.2.5": [{"yanked": True}],
                         "0.2.1": [], "0.10.0": [{}]}}
    assert updater.latest_stable(pypi) == "0.10.0"
    assert updater.latest_stable({"releases": {"1.0.0b1": [{}]}}) is None


def test_is_newer_compares_numbers_not_strings():
    assert updater.is_newer("0.10.0", "0.9.0") and not updater.is_newer("0.1.0", "0.1.0")
    assert not updater.is_newer("1.0.0rc1", "0.1.0")


class Spy:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.rc, stdout="Updated inboxink\n", stderr="")


def test_auto_update_runs_once_a_day(pinned_config):
    spy, lines = Spy(), []
    now = 1_000_000.0
    updater.after_run(pinned_config, now, runner=spy, log=lines.append)
    assert len(spy.calls) == 1 and spy.calls[0][-2:] == ["update", "--auto"]
    assert "auto-update finished" in lines[0]
    updater.after_run(pinned_config, now + 3600, runner=spy, log=lines.append)
    assert len(spy.calls) == 1  # throttled
    updater.after_run(pinned_config, now + 25 * 3600, runner=spy, log=lines.append)
    assert len(spy.calls) == 2


def test_auto_off_only_logs_when_newer_exists(pinned_config):
    import dataclasses
    cfg = dataclasses.replace(pinned_config, updates=dataclasses.replace(pinned_config.updates, auto=False))
    spy, lines = Spy(), []
    updater.after_run(cfg, 5_000_000.0, runner=spy, fetch=lambda: "9.9.9", log=lines.append)
    assert not spy.calls and lines == [f"update available: 9.9.9 (you have {updater.__version__}); run `inboxink update`"]
    lines.clear()
    updater.after_run(cfg, 5_000_000.0 + 25 * 3600, runner=spy, fetch=lambda: updater.__version__, log=lines.append)
    assert lines == []


def test_failures_never_raise(pinned_config):
    def boom(*a, **k):
        raise OSError("no network")
    lines = []
    assert updater.after_run(pinned_config, time.time(), runner=boom, log=lines.append) is None
    assert "skipped" in lines[0]


def test_failed_update_is_logged_not_raised(pinned_config):
    lines = []
    assert updater.after_run(pinned_config, 7e6, runner=Spy(rc=1), log=lines.append) == 1
    assert "failed" in lines[0]


def test_execute_without_uv_or_pipx_prints_instructions(pinned_config):
    out = []
    rc = updater.execute(argparse.Namespace(skip_upgrade=False, auto=False), runner=Spy(), which=lambda n: None, out=out.append)
    assert rc == 1 and "pip install --upgrade inboxink" in out[0]


def test_execute_upgrades_then_reexecs_for_the_new_code(pinned_config):
    spy, out = Spy(), []
    rc = updater.execute(argparse.Namespace(skip_upgrade=False, auto=True), runner=spy,
                         which=lambda n: "/x/uv" if n == "uv" else None, out=out.append)
    assert rc == 0
    assert spy.calls[0] == ["uv", "tool", "upgrade", "inboxink"]
    assert spy.calls[1][-3:] == ["update", "--skip-upgrade", "--auto"]


def test_redeploy_only_when_version_differs(pinned_config, monkeypatch):
    import dataclasses
    cfg = dataclasses.replace(pinned_config, worker=dataclasses.replace(pinned_config.worker, account_id="acc1"))
    uploads = []

    class C:
        def upload_worker(self, a, n): uploads.append(("up", a, n))
        def enable_workers_dev(self, a): uploads.append(("on", a))
    monkeypatch.setattr(updater.cloudflare, "probe", lambda url: (404, updater.cloudflare.worker_version()))
    assert updater.redeploy_if_needed(cfg, client=C(), out=lambda s: None) is False and not uploads
    monkeypatch.setattr(updater.cloudflare, "probe", lambda url: (404, "old"))
    assert updater.redeploy_if_needed(cfg, client=C(), out=lambda s: None) is True
    assert uploads == [("up", "acc1", cfg.worker.kv_namespace_id), ("on", "acc1")]
