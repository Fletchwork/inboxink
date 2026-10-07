"""One pass: every pending newsletter -> cleaned hosted page -> Instapaper -> filed as done.

    python -m inboxink.run              # the scheduled job (same as `inboxink run`)
    python -m inboxink.run --dry-run    # list what would be sent; writes nothing (labels must exist)
    python -m inboxink.run --query 'newer_than:1d'   # narrow the pending search
"""
import argparse, fcntl, json, os, secrets, sys, time

from . import config, instapaper, publish
from .clean import clean
from .sources import get_source

# {"sent": {msg_id: {url, title, at}}, "inflight": {msg_id: {aid, title}}, "attempts": {msg_id: n}}.
# "inflight" is written before the Instapaper save, so a retry after a timeout re-saves the SAME URL
# (Instapaper is believed to keep one article per URL; unverified) instead of a new one. "sent" guards the Gmail relabel.
# "sent" entries older than SENT_KEEP_DAYS are pruned. The file is paths.state_dir/state.json.
SENT_KEEP_DAYS = 60
# Three runs at the default 30-minute interval: rides out a transient mail/Cloudflare/Instapaper blip,
# gives up in about an hour. Retry a given-up issue by removing its failed label; the count restarts at zero.
MAX_ATTEMPTS = 3
# The log passes LOG_MAX bytes: keep only its last LOG_KEEP bytes (whole lines).
LOG_MAX, LOG_KEEP = 1_000_000, 200_000


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def _private_open(path, extra_flags=0):
    """Open `path` for writing, created readable by this user only (0600) from the first byte."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | extra_flags, 0o600)
    try:
        os.fchmod(fd, 0o600)  # a leftover file from an older version may have wider permissions
    except OSError:
        pass
    return os.fdopen(fd, "w")


def trim_log(path, limit=LOG_MAX, keep=LOG_KEEP):
    """Once the log file passes `limit` bytes, rewrite it (atomically) as its last `keep` bytes,
    starting at a line boundary. Does nothing for a missing file or a terminal."""
    try:
        if os.path.getsize(path) <= limit:
            return
        with open(path, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        tail = tail.split(b"\n", 1)[1] if b"\n" in tail else tail  # drop the cut-off first line
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(tail)
        os.replace(tmp, path)
    except OSError:
        pass


def _state_path():
    return config.get().paths.state_file


def load_state():
    try:
        with open(_state_path()) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _defaults(state):
    for k in ("sent", "inflight", "attempts"):
        state.setdefault(k, {})
    return state


def save_state(state):
    cutoff = time.time() - SENT_KEEP_DAYS * 86400
    state["sent"] = {k: v for k, v in state["sent"].items() if v.get("at", 0) >= cutoff}
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with _private_open(tmp, os.O_TRUNC) as f:  # holds the page links, so never readable by others
        json.dump(state, f, indent=1)
    os.replace(tmp, path)


def process(mid, source, state):
    if mid in state["sent"]:  # saved to Instapaper but filing it as done failed last time
        source.mark_done(mid)
        return "relabeled"
    prior = state["inflight"].get(mid)
    aid = prior["aid"] if prior else secrets.token_hex(16)
    title, doc, images, removed = clean(source.fetch_raw(mid), aid, config.get().worker.base_url)
    url = publish.upload(aid, doc, images)  # same aid on a retry: overwrites the same keys
    publish.wait_live(aid, images)
    state["inflight"][mid] = {"aid": aid, "title": title}
    save_state(state)
    instapaper.add(url, title)
    state["inflight"].pop(mid, None)
    state["sent"][mid] = {"url": url, "title": title, "at": int(time.time())}
    save_state(state)
    source.mark_done(mid)
    return f"sent '{title}' ({len(images)} images, {len(removed)} ad/promo blocks cut)"


def add_arguments(ap):
    ap.add_argument("--dry-run", action="store_true", help="list what would be sent; change nothing")
    ap.add_argument("--query", default="", help="extra mail search terms (Gmail syntax)")
    ap.add_argument("--limit", type=int, default=25, help="most messages to handle in one run")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m inboxink.run")
    add_arguments(ap)
    return execute(ap.parse_args(argv))


def execute(args):
    try:
        cfg = config.get()
    except config.ConfigError as e:
        print(f"ConfigError: {e}", file=sys.stderr)
        return 2
    state_path = cfg.paths.state_file
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    lock = _private_open(state_path + ".lock")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # a manual run during a scheduled run waits its turn
    except BlockingIOError:
        log("another run is active; exiting")
        return 0  # not trimming here: the active run still has the log open
    try:
        return _locked_run(cfg, args)
    finally:  # at the end, not the start: the scheduler's open log handle must not be orphaned mid-run
        if not args.dry_run:
            trim_log(cfg.paths.log)


def _locked_run(cfg, args):
    if not args.dry_run:
        try:
            instapaper.check_login()
        except RuntimeError as e:
            log(str(e))
            return 1

    source = None
    try:
        source = get_source(cfg)
        source.open(create=not args.dry_run)
        todo = source.pending(args.query, args.limit)
    except Exception as e:  # one line per run, not a traceback every 30 minutes
        log(f"mail source unavailable: {e}")
        if source is not None:
            source.close()
        return 1
    try:
        return deliver(source, todo, args.dry_run)
    finally:
        source.close()


def deliver(source, todo, dry_run):
    if not todo:
        return 0
    log(f"{len(todo)} pending")
    state = _defaults(load_state())
    failures = 0
    for m in todo:  # oldest first, so the newest issue lands on top in Instapaper
        mid = m.id
        if dry_run:
            log(f"would send {mid}")
            continue
        try:
            log(f"{mid}: {process(mid, source, state)}")
            state["attempts"].pop(mid, None)
        except Exception as e:
            failures += 1
            n = state["attempts"].get(mid, 0) + 1
            state["attempts"][mid] = n
            log(f"{mid}: attempt {n} failed: {e}")
            save_state(state)  # the count survives even if the relabel below fails
            if n >= MAX_ATTEMPTS:
                try:
                    source.mark_failed(mid)
                    state["attempts"].pop(mid, None)  # removing the label later retries from zero
                    log(f"{mid}: gave up after {n} attempts, labeled '{source.failed_label}'")
                except Exception as e2:
                    log(f"{mid}: could not label failed ({e2}); will retry the label next run")
        save_state(state)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
