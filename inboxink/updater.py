"""Keep InboxInk current: `inboxink update`, and the once-a-day check that `inboxink run` makes.

Only stable releases count (1.2.3, never 1.3.0rc1). Every network call has a timeout and a
failure never breaks a run: the worst case is a line in the log.
"""
import json, os, re, shutil, subprocess, sys, time, urllib.request

from . import __version__, cloudflare, config, schedule

PYPI_URL = "https://pypi.org/pypi/inboxink/json"
CHECK_EVERY = 24 * 3600
STAMP = "update-check"
_STABLE = re.compile(r"\d+(\.\d+){0,3}")


def _log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


# --- versions ----------------------------------------------------------------------------------

def parse_version(text):
    """(1, 2, 3) for a stable release string, None for pre-releases, dev and anything odd."""
    return tuple(int(p) for p in text.split(".")) if _STABLE.fullmatch(text or "") else None


def latest_stable(pypi_json):
    """The newest non-yanked stable version in PyPI's JSON, as a string, or None."""
    best = None
    for ver, files in (pypi_json.get("releases") or {}).items():
        key = parse_version(ver)
        if key is None or not files or all(f.get("yanked") for f in files):
            continue
        if best is None or key > best[0]:
            best = (key, ver)
    return best[1] if best else None


def fetch_latest(timeout=10):
    """Latest stable version on PyPI, or None if it cannot be read."""
    req = urllib.request.Request(PYPI_URL, headers={"User-Agent": f"inboxink/{__version__} update-check"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (fixed https://pypi.org endpoint)
            return latest_stable(json.loads(r.read().decode()))
    except Exception:
        return None


def is_newer(candidate, current=__version__):
    a, b = parse_version(candidate), parse_version(current)
    return a is not None and (b is None or a > b)


# --- the daily check ---------------------------------------------------------------------------

def _stamp_path(cfg):
    return os.path.join(cfg.paths.state_dir, STAMP)


def check_due(cfg, now=None):
    now = time.time() if now is None else now
    try:
        with open(_stamp_path(cfg)) as f:
            return now - float(f.read().strip()) >= CHECK_EVERY
    except (FileNotFoundError, ValueError, OSError):
        return True


def _write_stamp(cfg, now):
    try:
        os.makedirs(cfg.paths.state_dir, exist_ok=True)
        with open(_stamp_path(cfg), "w") as f:
            f.write(str(now))
    except OSError:
        pass


def after_run(cfg, now=None, runner=subprocess.run, fetch=fetch_latest, log=_log):
    """Called by `inboxink run` once it has finished. At most once a day, either install the update
    (updates.auto) or just note that one exists. Never raises."""
    try:
        now = time.time() if now is None else now
        if not check_due(cfg, now):
            return None
        _write_stamp(cfg, now)  # first, so a failing update is retried tomorrow, not every 30 minutes
        if cfg.updates.auto:
            r = runner([sys.executable, "-m", "inboxink.cli", "update", "--auto"], capture_output=True, text=True, timeout=900)
            tail = ((r.stdout or "").strip().splitlines() or [""])[-1]
            log(f"auto-update {'finished' if r.returncode == 0 else 'failed'}: {tail}")
            return r.returncode
        latest = fetch()
        if latest and is_newer(latest):
            log(f"update available: {latest} (you have {__version__}); run `inboxink update`")
        return None
    except Exception as e:  # an update problem must never turn into a failed delivery run
        try:
            log(f"update check skipped: {e}")
        except Exception:
            pass
        return None


# --- `inboxink update` -------------------------------------------------------------------------

def upgrade_command(which=shutil.which):
    """The command that upgrades the installed tool, or None if neither uv nor pipx is here."""
    if which("uv"):
        return ["uv", "tool", "upgrade", "inboxink"]
    if which("pipx"):
        return ["pipx", "upgrade", "inboxink"]
    return None


def redeploy_if_needed(cfg, client=None, out=print):
    """Upload the Worker again if the deployed one is not this version. Returns True if it did."""
    if not cfg.worker.account_id:
        out("The Worker was not set up by InboxInk, so it is left alone.")
        return False
    status, deployed = cloudflare.probe(cfg.worker.base_url)
    wanted = cloudflare.worker_version()
    if deployed == wanted:
        out("The Worker is current.")
        return False
    out("Updating the Worker on Cloudflare...")
    client = client or cloudflare.Client()
    client.upload_worker(cfg.worker.account_id, cfg.worker.kv_namespace_id)
    client.enable_workers_dev(cfg.worker.account_id)
    out("The Worker is updated.")
    return True


def execute(args, runner=subprocess.run, which=shutil.which, out=print):
    """`inboxink update`. Step 1 upgrades the program; step 2 runs in the NEW program (a fresh
    process) to redeploy the Worker and reinstall the schedule."""
    cfg = config.get()
    if not getattr(args, "skip_upgrade", False):
        cmd = upgrade_command(which)
        if cmd is None:
            out("InboxInk can't update itself here because it was not installed with uv or pipx.\n"
                "Update it the way you installed it, for example `pip install --upgrade inboxink`,\n"
                "then run `inboxink update --skip-upgrade`.")
            return 1
        out(f"Updating InboxInk ({' '.join(cmd)})...")
        r = runner(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            out(f"The upgrade did not work: {((r.stderr or r.stdout or '').strip().splitlines() or ['no details'])[-1]}")
            return 1
        out(((r.stdout or r.stderr or '').strip().splitlines() or ['Done.'])[-1])
        again = [sys.executable, "-m", "inboxink.cli", "update", "--skip-upgrade"]
        return runner(again + (["--auto"] if getattr(args, "auto", False) else [])).returncode
    try:
        redeploy_if_needed(cfg, out=out)
    except cloudflare.CloudflareError as e:
        out(f"Could not update the Worker: {e}")
        return 1
    ok = True
    # Not for the daily auto-update: that runs inside the scheduled job, and reloading a job
    # from inside itself would kill it. The program path does not change on upgrade anyway.
    if not getattr(args, "auto", False) and schedule.is_installed(cfg):
        res = schedule.install(cfg)
        out(res.message)
        ok = res.installed
    out(f"InboxInk {__version__} is ready.")
    return 0 if ok else 1
