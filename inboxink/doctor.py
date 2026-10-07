"""`inboxink doctor`: check each piece of the setup and say how to fix whatever is wrong.

Secrets are only ever reported as present or missing, never shown.
"""
from . import __version__, cloudflare, config, credentials, schedule, updater
from .sources import get_source

OK, BAD = "✓", "✗"


def _check(results, label, fn, hint):
    """Run fn() -> (ok, detail). An exception counts as a failure with its message as detail."""
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, str(e)
    results.append((ok, f"{label}{': ' + detail if detail else ''}", None if ok else hint))
    return ok


def run_checks():
    """A list of (ok, line, fix hint or None)."""
    results = []
    try:
        cfg = config.load()
    except config.ConfigError as e:
        results.append((False, f"Settings file: {str(e).splitlines()[0]}", "run `inboxink setup`"))
        results.append((True, f"InboxInk version {__version__}", None))
        return results
    results.append((True, f"Settings file: {cfg.config_file}", None))

    def logins_saved():
        names = (["gmail_app_password"] if cfg.source.kind == "imap" else []) + ["cloudflare_token", "instapaper"]
        missing = []
        for n in names:
            try:
                have = bool(credentials.get_instapaper(cfg)) if n == "instapaper" else bool(credentials.get_secret(n))
            except credentials.CredentialError:
                have = False
            if not have:
                missing.append(n.replace("_", " "))
        return (not missing, "all saved" if not missing else "missing " + ", ".join(missing))
    _check(results, "Passwords and tokens", logins_saved, "run `inboxink setup` to enter them again")

    def mail():
        src = get_source(cfg)
        try:
            src.open(create=False)
        finally:
            src.close()
        return True, "signed in"
    _check(results, "Mail login", mail, "check the app password and address; if a label is missing, run 'inboxink run' once")

    def token():
        cloudflare.Client().verify_token()
        return True, "accepted"
    _check(results, "Cloudflare token", token, "make a new token and run `inboxink setup`")

    def worker():
        status, version = cloudflare.probe(cfg.worker.base_url)
        if status == 0:
            return False, f"{cfg.worker.base_url} did not answer"
        if version and version != cloudflare.worker_version():
            return False, "online, but an older version"
        return True, f"online at {cfg.worker.base_url}"
    _check(results, "Worker", worker, "run `inboxink update` (or `inboxink setup` if it was never deployed)")

    _check(results, "Schedule", lambda: (schedule.is_installed(cfg), f"every {cfg.schedule.interval_minutes} minutes"),
           "run `inboxink setup` to install it")

    latest = updater.fetch_latest(timeout=5)
    if latest and updater.is_newer(latest):
        results.append((False, f"InboxInk version {__version__} (newer: {latest})", "run `inboxink update`"))
    else:
        results.append((True, f"InboxInk version {__version__}", None))
    return results


def execute(args=None, out=print):
    results = run_checks()
    for ok, line, hint in results:
        out(f"{OK if ok else BAD} {line}")
        if hint:
            out(f"    fix: {hint}")
    bad = sum(1 for ok, _, _ in results if not ok)
    out("\nEverything looks good." if not bad else f"\n{bad} thing{'s' if bad != 1 else ''} to fix.")
    return 1 if bad else 0
