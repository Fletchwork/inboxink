import dataclasses, os, plistlib, sys
from types import SimpleNamespace

from inboxink import schedule


class Runner:
    def __init__(self, fail=()):
        self.cmds, self.fail = [], fail

    def __call__(self, cmd, **kw):
        self.cmds.append(cmd)
        return SimpleNamespace(returncode=1 if tuple(cmd[:3]) in self.fail or tuple(cmd[:2]) in self.fail else 0,
                               stdout="", stderr="boom")


def test_plist_content(pinned_config, tmp_path):
    cfg = dataclasses.replace(pinned_config, paths=dataclasses.replace(pinned_config.paths, log=str(tmp_path / "a&b <log>.txt")))
    data = plistlib.loads(schedule.plist_bytes(cfg, home="/home/x"))
    assert data["ProgramArguments"] == [sys.executable, "-m", "inboxink.cli", "run"]
    assert data["StartInterval"] == 30 * 60 and data["RunAtLoad"] is True
    assert data["StandardOutPath"] == data["StandardErrorPath"] == str(tmp_path / "a&b <log>.txt")
    assert data["Label"] == cfg.schedule.label and data["EnvironmentVariables"]["HOME"] == "/home/x"
    assert b"&amp;" in schedule.plist_bytes(cfg, home="/home/x")  # escaped by plistlib, not by hand


def test_install_mac_boots_out_then_bootstraps(pinned_config, tmp_path):
    r = Runner()
    res = schedule.install(pinned_config, runner=r, home=str(tmp_path), system="mac")
    assert res.installed
    assert [c[:2] for c in r.cmds] == [["launchctl", "bootout"], ["launchctl", "bootstrap"], ["launchctl", "enable"]]
    path = schedule.plist_path(pinned_config, str(tmp_path))
    assert os.path.exists(path) and r.cmds[1][3] == path
    assert schedule.is_installed(pinned_config, str(tmp_path), "mac")
    assert schedule.uninstall(pinned_config, runner=r, home=str(tmp_path), system="mac") is True
    assert not os.path.exists(path)


def test_install_mac_reports_launchctl_failure(pinned_config, tmp_path):
    res = schedule.install(pinned_config, runner=Runner(fail={("launchctl", "bootstrap")}), home=str(tmp_path), system="mac")
    assert not res.installed and "would not load" in res.message


def test_install_linux_writes_units(pinned_config, tmp_path):
    r = Runner()
    res = schedule.install(pinned_config, runner=r, home=str(tmp_path), system="linux")
    assert res.installed
    d = schedule.unit_dir(str(tmp_path))
    timer = open(os.path.join(d, f"{pinned_config.schedule.label}.timer")).read()
    service = open(os.path.join(d, f"{pinned_config.schedule.label}.service")).read()
    assert "OnUnitActiveSec=30min" in timer and "ExecStart=" in service and "inboxink.cli run" in service
    assert ["systemctl", "--user", "enable", "--now", f"{pinned_config.schedule.label}.timer"] in r.cmds
    assert schedule.uninstall(pinned_config, runner=r, home=str(tmp_path), system="linux") is True


def test_linux_without_user_systemd_gives_cron_line(pinned_config, tmp_path):
    res = schedule.install(pinned_config, runner=Runner(fail={("systemctl", "--user")}), home=str(tmp_path), system="linux")
    assert not res.installed and "crontab -e" in res.message and "*/30 * * * *" in res.message
    assert not os.path.exists(schedule.unit_dir(str(tmp_path)))


def test_install_creates_the_log_private(pinned_config, tmp_path):
    for system in ("mac", "linux"):
        os.path.exists(pinned_config.paths.log) and os.remove(pinned_config.paths.log)
        schedule.install(pinned_config, runner=Runner(), home=str(tmp_path), system=system)
        assert oct(os.stat(pinned_config.paths.log).st_mode & 0o777) == "0o600", system
