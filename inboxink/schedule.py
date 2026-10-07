"""Run `inboxink run` on a schedule: a launchd LaunchAgent on macOS, a systemd user timer on Linux.

Every function takes an injectable `runner` (default subprocess.run) and `home`, so tests can
check what would be written and which commands would run without touching the machine.
"""
import os, plistlib, shlex, subprocess, sys
from dataclasses import dataclass

from . import config


@dataclass
class Result:
    installed: bool
    message: str


def _system():
    return "mac" if sys.platform == "darwin" else "linux"


def _command():
    return [sys.executable, "-m", "inboxink.cli", "run"]


def _env(home):
    env = {"HOME": home, "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"}
    if os.environ.get("INBOXINK_CONFIG"):
        env["INBOXINK_CONFIG"] = os.environ["INBOXINK_CONFIG"]
    return env


def _prepare_log(cfg):
    """Make the log's folder, and the log itself readable by this user only: it lists newsletter titles.
    The scheduler would otherwise create it with the default, more open, permissions."""
    os.makedirs(os.path.dirname(cfg.paths.log), exist_ok=True)
    os.close(os.open(cfg.paths.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600))
    try:
        os.chmod(cfg.paths.log, 0o600)
    except OSError:
        pass


def _run(runner, cmd):
    return runner(cmd, capture_output=True, text=True)


# --- macOS -------------------------------------------------------------------------------------

def plist_path(cfg, home=None):
    return os.path.join(home or os.path.expanduser("~"), "Library", "LaunchAgents", f"{cfg.schedule.label}.plist")


def plist_bytes(cfg, home=None):
    home = home or os.path.expanduser("~")
    return plistlib.dumps({
        "Label": cfg.schedule.label,
        "ProgramArguments": _command(),
        "StartInterval": cfg.schedule.interval_minutes * 60,
        "RunAtLoad": True,
        "EnvironmentVariables": _env(home),
        "StandardOutPath": cfg.paths.log,
        "StandardErrorPath": cfg.paths.log,
    })


def _install_mac(cfg, runner, home):
    path = plist_path(cfg, home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _prepare_log(cfg)
    with open(path, "wb") as f:
        f.write(plist_bytes(cfg, home))
    domain = f"gui/{os.getuid()}"
    _run(runner, ["launchctl", "bootout", f"{domain}/{cfg.schedule.label}"])  # fine if it was not loaded
    r = _run(runner, ["launchctl", "bootstrap", domain, path])
    if r.returncode != 0:
        return Result(False, f"macOS would not load the schedule ({(r.stderr or '').strip()[-200:]}). "
                             f"The file is at {path}; log out and back in, then run `inboxink setup` again.")
    _run(runner, ["launchctl", "enable", f"{domain}/{cfg.schedule.label}"])
    return Result(True, f"Checking for newsletters every {cfg.schedule.interval_minutes} minutes.")


def _uninstall_mac(cfg, runner, home):
    _run(runner, ["launchctl", "bootout", f"gui/{os.getuid()}/{cfg.schedule.label}"])
    path = plist_path(cfg, home)
    existed = os.path.exists(path)
    if existed:
        os.remove(path)
    return existed


# --- Linux -------------------------------------------------------------------------------------

def unit_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), ".config", "systemd", "user")


def _quote(arg):
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in arg for c in ' \t"\\') else arg


def service_text(cfg, home=None):
    home = home or os.path.expanduser("~")
    env = "\n".join(f"Environment={_quote(f'{k}={v}')}" for k, v in _env(home).items())
    return (f"[Unit]\nDescription=InboxInk: send new newsletters to your e-reader\n\n"
            f"[Service]\nType=oneshot\nExecStart={' '.join(_quote(a) for a in _command())}\n{env}\n"
            f"StandardOutput=append:{cfg.paths.log}\nStandardError=append:{cfg.paths.log}\n")


def timer_text(cfg):
    n = cfg.schedule.interval_minutes
    return (f"[Unit]\nDescription=Run InboxInk every {n} minutes\n\n"
            f"[Timer]\nOnBootSec=2min\nOnUnitActiveSec={n}min\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")


def cron_line(cfg):
    n = cfg.schedule.interval_minutes
    when = f"*/{n} * * * *" if n < 60 else f"0 */{max(n // 60, 1)} * * *"
    return f"{when} {' '.join(shlex.quote(a) for a in _command())} >> {shlex.quote(cfg.paths.log)} 2>&1"


def _install_linux(cfg, runner, home):
    _prepare_log(cfg)
    probe = None
    try:
        probe = _run(runner, ["systemctl", "--user", "show-environment"])
    except (FileNotFoundError, OSError):
        pass
    if probe is None or probe.returncode != 0:
        return Result(False, "This computer has no per-user systemd timer, so InboxInk can't schedule itself.\n"
                             "Add this line with `crontab -e` instead:\n  " + cron_line(cfg))
    d = unit_dir(home)
    os.makedirs(d, exist_ok=True)
    name = cfg.schedule.label
    with open(os.path.join(d, f"{name}.service"), "w") as f:
        f.write(service_text(cfg, home))
    with open(os.path.join(d, f"{name}.timer"), "w") as f:
        f.write(timer_text(cfg))
    _run(runner, ["systemctl", "--user", "daemon-reload"])
    r = _run(runner, ["systemctl", "--user", "enable", "--now", f"{name}.timer"])
    if r.returncode != 0:
        return Result(False, f"systemd would not start the timer ({(r.stderr or '').strip()[-200:]}). "
                             "Add this line with `crontab -e` instead:\n  " + cron_line(cfg))
    return Result(True, f"Checking for newsletters every {cfg.schedule.interval_minutes} minutes.")


def _uninstall_linux(cfg, runner, home):
    name = cfg.schedule.label
    try:
        _run(runner, ["systemctl", "--user", "disable", "--now", f"{name}.timer"])
    except (FileNotFoundError, OSError):
        pass
    existed = False
    for ext in ("service", "timer"):
        p = os.path.join(unit_dir(home), f"{name}.{ext}")
        if os.path.exists(p):
            os.remove(p)
            existed = True
    if existed:
        try:
            _run(runner, ["systemctl", "--user", "daemon-reload"])
        except (FileNotFoundError, OSError):
            pass
    return existed


# --- public ------------------------------------------------------------------------------------

def install(cfg=None, runner=subprocess.run, home=None, system=None):
    cfg = cfg or config.get()
    return (_install_mac if (system or _system()) == "mac" else _install_linux)(cfg, runner, home)


def uninstall(cfg=None, runner=subprocess.run, home=None, system=None):
    """Remove the schedule. Returns True if there was one."""
    cfg = cfg or config.get()
    return (_uninstall_mac if (system or _system()) == "mac" else _uninstall_linux)(cfg, runner, home)


def is_installed(cfg=None, home=None, system=None):
    cfg = cfg or config.get()
    if (system or _system()) == "mac":
        return os.path.exists(plist_path(cfg, home))
    return os.path.exists(os.path.join(unit_dir(home), f"{cfg.schedule.label}.timer"))
