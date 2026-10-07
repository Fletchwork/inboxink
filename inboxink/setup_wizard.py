"""`inboxink setup`: a guided, re-runnable first-time setup in plain English.

Eight numbered steps: Gmail, Cloudflare, Instapaper, ad stripping, updates, saving the settings,
the schedule, and a dry run. Everything it asks for is a link plus "paste it here". Passwords and
tokens are read without echo and stored by credentials.py, never in the config file or a command line.

Re-running is safe: answers already in config.toml (and secrets already stored) are offered as
defaults, and the Cloudflare pieces are reused rather than created twice. Ctrl-C stops cleanly.
"""
import argparse, base64, contextlib, copy, dataclasses, getpass, io, json, os, re, secrets, shutil, sys, time, tomllib
import urllib.error, urllib.request

from . import __version__, cloudflare, config, credentials, schedule
from .sources import get_source

APP_PASSWORD_URL = "https://myaccount.google.com/apppasswords"
TOTAL_STEPS = 8


class SetupError(Exception):
    """Setup cannot continue. The message says what to do about it."""


class Console:
    """All reading and printing goes through here so tests can script a whole session."""

    def __init__(self, input_fn=None, getpass_fn=None, out=None):
        self._input = input_fn or input
        self._getpass = getpass_fn or getpass.getpass
        self._out = out or (lambda s="", end="\n": print(s, end=end, flush=True))

    def say(self, *lines):
        for line in lines:
            self._out(line)

    def dot(self):
        self._out(".", end="")

    def ask(self, prompt, default=""):
        shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
        return self._input(shown).strip() or default

    def yesno(self, prompt, default):
        while True:
            hint = "Y/n" if default else "y/N"
            answer = self._input(f"{prompt} ({hint}) ").strip().lower()
            if not answer:
                return default
            if answer in ("y", "yes"):
                return True
            if answer in ("n", "no"):
                return False
            self._out("Please answer y or n.")

    def secret(self, prompt):
        return self._getpass(f"{prompt}: ").strip()

    def choose(self, prompt, labels):
        for i, label in enumerate(labels, 1):
            self._out(f"  {i}. {label}")
        while True:
            answer = self._input(f"{prompt} [1-{len(labels)}]: ").strip()
            if answer.isdigit() and 1 <= int(answer) <= len(labels):
                return int(answer) - 1
            self._out("Please type one of the numbers above.")


# --- a tiny TOML writer (tomllib reads TOML but cannot write it) ------------------------------

_BARE = re.compile(r"[A-Za-z0-9_-]+")


def _toml_key(k):
    return k if _BARE.fullmatch(k) else json.dumps(k, ensure_ascii=False)


def _toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def toml_dumps(data, _path=()):
    """Write nested dicts of str/int/bool/list as TOML that tomllib reads back unchanged."""
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    out = []
    if _path and (scalars or not tables):
        out.append("[" + ".".join(_toml_key(p) for p in _path) + "]")
    out += [f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items()]
    text = "\n".join(out) + ("\n" if out else "")
    for k, sub in tables.items():
        text += ("\n" if text else "") + toml_dumps(sub, _path + (k,))
    return text


def write_config(path, data):
    """Write the settings file atomically, readable by its owner only. Keeps one .bak of the old file."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
        os.chmod(path + ".bak", 0o600)
    header = ("# InboxInk settings, written by `inboxink setup`. Passwords and tokens are NOT stored here.\n"
              "# Every setting you can add is listed in config.example.toml in the InboxInk repository.\n\n")
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(header + toml_dumps(data))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_raw(path):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except FileNotFoundError:
        return {}
    except (tomllib.TOMLDecodeError, OSError):
        return None  # present but unreadable: the caller warns and starts fresh


def _section(raw, name):
    v = raw.get(name)
    return v if isinstance(v, dict) else {}


# --- Instapaper check ---------------------------------------------------------------------------

def verify_instapaper(user, password, timeout=20):
    """True if Instapaper accepts the login, False if it refuses it, None if it could not be asked."""
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request("https://www.instapaper.com/api/authenticate", data=b"",
                                 headers={"Authorization": f"Basic {auth}", "User-Agent": f"inboxink/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected (fixed https://www.instapaper.com endpoint)
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code == 403 else None
    except Exception:
        return None


# --- the steps ----------------------------------------------------------------------------------

def _valid_address(a):
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a))


def _blank_config():
    """Default settings (no file read), used to try a login before anything is saved."""
    return config.load(path="/nonexistent/inboxink.toml", env={}, validate=False)


def _where(stored):
    return "your keychain" if stored == "keychain" else stored


def step_gmail(con, st, raw):
    gmail, source = _section(raw, "gmail"), _section(raw, "source")
    if source.get("kind") == "gog":
        con.say("Your current settings read mail with the `gog` tool. This setup switches InboxInk to the",
                "simpler Gmail app-password method.")
        if not con.yesno("Switch to the app-password method?", False):
            raise SetupError("Nothing was changed. (`gog` setups are advanced; edit config.toml by hand.)")
    while True:
        address = con.ask("What is your Gmail address?", gmail.get("address", ""))
        if _valid_address(address):
            break
        con.say("That does not look like an email address. Try again.")
    st["address"] = address
    st["newsletters_only"] = con.yesno("Is this Gmail only for newsletters?", bool(source.get("newsletters_only", False)))
    con.say("", "InboxInk signs in with a Google 'app password': a 16-letter password just for InboxInk.",
            "  1. 2-Step Verification must be on for your Google account.",
            "  2. Open the link below, name the app 'InboxInk', and press Create.",
            "  3. Copy the 16 letters Google shows you and paste them here.",
            f"     {APP_PASSWORD_URL}", "     (Your typing will not show on screen. That is normal.)")
    if credentials.get_secret("gmail_app_password") and con.yesno("An app password is already saved. Keep it?", True):
        pass
    else:
        for _ in range(3):
            pw = re.sub(r"\s+", "", con.secret("Paste the app password"))
            if len(pw) == 16:
                con.say(f"Saved in {_where(credentials.set_secret('gmail_app_password', pw))}.")
                break
            con.say("An app password is exactly 16 letters (spaces do not matter). Try again.")
        else:
            raise SetupError("No valid app password was entered. Run `inboxink setup` to try again.")
    _test_mail_login(con, st)
    if not st["newsletters_only"]:
        con.say("", "Because this Gmail has other mail too, tell Gmail which messages are newsletters:",
                "  1. Open Gmail on a computer and click the gear, then 'See all settings'.",
                "  2. Open the 'Filters and Blocked Addresses' tab and click 'Create a new filter'.",
                "  3. In 'From', type the newsletter's address (several: put 'OR' between them), then 'Create filter'.",
                "  4. Tick 'Apply the label', choose 'New label...', name it exactly Newsletter, and create the filter.",
                "InboxInk only sends mail that has the Newsletter label. You can also add the label to one email by hand.")


def _test_mail_login(con, st):
    cfg = _blank_config()
    cfg = dataclasses.replace(
        cfg, source=dataclasses.replace(cfg.source, kind="imap", newsletters_only=st["newsletters_only"]),
        gmail=dataclasses.replace(cfg.gmail, address=st["address"]))
    con.say("Checking the Gmail login...")
    try:
        src = get_source(cfg)
        try:
            src.open(create=True)
        finally:
            src.close()
        con.say("Gmail login works.")
    except Exception as e:
        con.say(f"Gmail did not accept the login: {e}",
                "Check the app password (and that 'IMAP' is allowed). Setup will go on; run `inboxink doctor` after fixing it.")


def _suggest_subdomain():
    # Random on purpose: this name is public in every newsletter link, so it must not echo the
    # reader's email address.
    return f"reader-{secrets.token_hex(4)}"


def _pick_subdomain(con, client, account_id):
    sub = client.get_subdomain(account_id)
    if sub:
        return sub
    con.say("Cloudflare gives your account a free address ending in .workers.dev. It appears in the links",
            "to your newsletters, so pick a name you are happy to have there.")
    suggestion = _suggest_subdomain()
    while True:
        name = con.ask("Name for your address", suggestion).lower()
        if not cloudflare.SUBDOMAIN_RE.fullmatch(name):
            con.say("Use only lowercase letters, digits and hyphens (no hyphen at the start or end).")
            continue
        try:
            return client.set_subdomain(account_id, name)
        except cloudflare.CloudflareError as e:
            con.say(f"Cloudflare would not use that name: {e}")
            suggestion = _suggest_subdomain()


def _wait_for_worker(con, base_url, version, tries=30, interval=2):
    for _ in range(tries):
        if cloudflare.probe(base_url)[1] == version:
            return True
        con.dot()
        time.sleep(interval)
    return False


def step_cloudflare(con, st, raw):
    worker = _section(raw, "worker")
    have = [worker.get(k) for k in ("base_url", "kv_namespace_id", "account_id")]
    if all(have) and credentials.get_secret("cloudflare_token"):
        con.say(f"Cloudflare is already set up ({worker['base_url']}).")
        if con.yesno("Keep it?", True):
            st.update(base_url=worker["base_url"], kv_id=worker["kv_namespace_id"], account_id=worker["account_id"])
            return
    con.say("InboxInk hosts each cleaned newsletter on Cloudflare, which is free for this use.",
            "You need a free Cloudflare account (https://dash.cloudflare.com/sign-up) and a token for InboxInk.",
            "Open this link, scroll down, press 'Continue to summary', then 'Create Token', and copy the token:",
            f"  {cloudflare.token_link()}",
            "If the permissions are not filled in, choose 'Create Custom Token' and add these three:",
            f"  {cloudflare.PERM_SCRIPTS}", f"  {cloudflare.PERM_KV}", f"  {cloudflare.PERM_ACCOUNT}")
    client = None
    for _ in range(3):
        token = con.secret("Paste the Cloudflare token")
        if not token:
            continue
        try:
            candidate = cloudflare.Client(token)
            candidate.verify_token()
            client = candidate
            break
        except cloudflare.CloudflareError as e:
            con.say(str(e))
    if client is None:
        raise SetupError("The Cloudflare token did not work. Run `inboxink setup` to try again.")
    con.say(f"Token accepted. Saved in {_where(credentials.set_secret('cloudflare_token', token.strip()))}.")
    try:
        accounts = client.list_accounts()
        if not accounts:
            raise SetupError("This token can't see any Cloudflare account. Check its 'Account Settings: Read' permission.")
        account = accounts[0] if len(accounts) == 1 else accounts[con.choose(
            "Which Cloudflare account should InboxInk use?", [a["name"] or a["id"] for a in accounts])]
        st["account_id"] = account["id"]
        st["kv_id"] = client.ensure_kv_namespace(account["id"])
        con.say("Storage space ready.")
        sub = _pick_subdomain(con, client, account["id"])
        con.say("Uploading the InboxInk Worker (the little web page that serves your newsletters)...")
        st["base_url"], version = client.deploy(account["id"], st["kv_id"], sub)
    except cloudflare.CloudflareError as e:
        raise SetupError(str(e)) from None
    con.say("Waiting for it to come online (up to a minute)")
    if _wait_for_worker(con, st["base_url"], version):
        con.say("", f"Your Worker is live at {st['base_url']}")
    else:
        con.say("", "It is not answering yet. That can take a few minutes the first time; setup will go on, and",
                "`inboxink doctor` will confirm it later.")


def step_instapaper(con, st, raw):
    con.say("InboxInk saves each newsletter to Instapaper (https://www.instapaper.com, free), and your Kobo",
            "syncs from there. Enter the email and password you use to sign in to Instapaper.")
    if credentials.get_secret("instapaper") and con.yesno("An Instapaper login is already saved. Keep it?", True):
        return
    for _ in range(3):
        user = con.ask("Instapaper email")
        pw = con.secret("Instapaper password")
        if not user or not pw:
            con.say("Both the email and the password are needed.")
            continue
        ok = verify_instapaper(user, pw)
        if ok is False:
            con.say("Instapaper did not accept that login. Try again.")
            continue
        if ok is None:
            con.say("(Could not reach Instapaper to check the login. Saving it anyway.)")
        credentials.set_instapaper(user, pw)
        con.say("Instapaper login saved." if ok else "Saved.")
        return
    raise SetupError("The Instapaper login did not work. Run `inboxink setup` to try again.")


def step_adstrip(con, st, raw):
    if shutil.which("claude"):
        st["adstrip"] = con.yesno("Use Claude to cut ads and promotions out of each newsletter?", False)
    else:
        # Written explicitly as off: newsletter text only goes to a model when the user opts in.
        st["adstrip"] = False
        con.say("InboxInk can also cut ads out of newsletters using the Claude Code app, but it is not installed",
                "here. That is optional: skipping it. (Install Claude Code later, then run `inboxink setup`",
                "again to turn it on.)")


def step_updates(con, st, raw):
    st["auto_update"] = con.yesno("Keep InboxInk up to date automatically?",
                                  bool(_section(raw, "updates").get("auto", True)))


def build_config(raw, st):
    """The settings to save: what was there before, with this run's answers on top."""
    d = copy.deepcopy(raw)
    d.setdefault("source", {}).update(kind="imap", newsletters_only=st["newsletters_only"])
    d.setdefault("gmail", {})["address"] = st["address"]
    d.setdefault("worker", {}).update(base_url=st["base_url"], kv_namespace_id=st["kv_id"], account_id=st["account_id"])
    if "adstrip" in st:
        d.setdefault("adstrip", {})["enabled"] = st["adstrip"]
    d.setdefault("updates", {})["auto"] = st["auto_update"]
    return d


def step_save(con, st, raw):
    path = config.config_path()
    write_config(path, build_config(raw, st))
    try:
        st["cfg"] = config.load()
    except config.ConfigError as e:
        raise SetupError(f"The settings were saved to {path} but do not load: {e}") from None
    config.set_current(st["cfg"])
    con.say(f"Settings saved to {path} (readable only by you).")


def step_schedule(con, st, raw):
    res = schedule.install(st["cfg"])
    con.say(res.message)


def count_dry_run(cfg):
    """(number of newsletters waiting, the log lines) from a dry run that changes nothing."""
    from . import run
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = run.execute(argparse.Namespace(dry_run=True, query="", limit=25))
    lines = buf.getvalue().splitlines()
    return sum(1 for l in lines if " would send " in l), rc, lines


def step_dry_run(con, st, raw):
    con.say("Looking for newsletters (nothing is sent in this check)...")
    n, rc, lines = count_dry_run(st["cfg"])
    unavailable = [l for l in lines if "unavailable" in l]
    if unavailable:
        con.say("Could not read your mail: " + unavailable[0].split("unavailable:", 1)[-1].strip(),
                "Fix that (`inboxink doctor` explains how); the schedule will keep trying.")
    elif n:
        con.say(f"{n} newsletter{'s are' if n != 1 else ' is'} waiting and will be sent on the next run.")
    else:
        con.say("No newsletters are waiting right now. New ones will be sent as they arrive.")
    con.say("", "All set. InboxInk checks for newsletters every "
            f"{st['cfg'].schedule.interval_minutes} minutes. Run `inboxink doctor` any time to check on things.")


STEPS = [("Gmail", step_gmail), ("Cloudflare (hosts your newsletters)", step_cloudflare),
         ("Instapaper", step_instapaper), ("Ad stripping (optional)", step_adstrip),
         ("Updates", step_updates), ("Save your settings", step_save),
         ("Schedule", step_schedule), ("Test run", step_dry_run)]


def run_setup(con=None):
    con = con or Console()
    done = 0
    try:
        path = config.config_path()
        raw = _read_raw(path)
        if raw is None:
            con.say(f"Your existing settings file ({path}) could not be read; it is kept as a .bak copy.")
            raw = {}
        elif raw:
            con.say(f"Found earlier settings in {path}. Press Enter to keep each saved answer.")
        con.say(f"InboxInk {__version__} setup: {TOTAL_STEPS} short steps. Press Ctrl-C at any point to stop.")
        st = {}
        for n, (title, fn) in enumerate(STEPS, 1):
            con.say("", f"Step {n} of {TOTAL_STEPS}: {title}")
            fn(con, st, raw)
            done = n
    except (KeyboardInterrupt, EOFError):
        con.say("", f"Stopped. Nothing was changed after step {done}." if done else "Stopped. Nothing was changed.",
                "Run `inboxink setup` again to carry on.")
        return 130
    except SetupError as e:
        con.say("", str(e))
        return 1
    return 0


def execute(args):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("`inboxink setup` asks questions, so it needs to run in a terminal window.")
        return 1
    return run_setup()


# --- uninstall ------------------------------------------------------------------------------------

def run_uninstall(con=None):
    """Remove the schedule; then, only if asked, the Cloudflare pieces and the local files. Every
    question defaults to No, and saying No to the first one changes nothing at all."""
    con = con or Console()
    try:
        cfg = config.load(validate=False)
        con.say("This will stop InboxInk from checking for newsletters. Your newsletters already in Instapaper stay.")
        if not con.yesno("Continue?", False):
            con.say("Cancelled. Nothing was changed.")
            return 0
        con.say("Schedule removed." if schedule.uninstall(cfg) else "There was no schedule to remove.")
        w = cfg.worker
        if w.account_id and w.kv_namespace_id and credentials.get_secret("cloudflare_token"):
            if con.yesno("Also delete the InboxInk Worker and its storage space from Cloudflare? Links to already "
                         "delivered issues stop working.", False):
                try:
                    client = cloudflare.Client()
                    client.delete_worker(w.account_id)
                    client.delete_kv_namespace(w.account_id, w.kv_namespace_id)
                    con.say("Worker and storage deleted from Cloudflare.")
                except cloudflare.CloudflareError as e:
                    con.say(f"Could not delete them: {e}", "You can remove them at https://dash.cloudflare.com.")
        if con.yesno(f"Also delete your saved settings and passwords ({cfg.config_file})?", False):
            for name in credentials.NAMES:
                credentials.delete_secret(name)
            for p in (cfg.config_file, cfg.config_file + ".bak", os.path.join(config.config_dir(), "secrets.json")):
                with contextlib.suppress(FileNotFoundError):
                    os.remove(p)
            con.say("Settings and passwords deleted.")
        con.say("", "To remove the program itself: `uv tool uninstall inboxink` (or `pipx uninstall inboxink`).",
                "Mail labels in Gmail are left as they are.")
        return 0
    except config.ConfigError as e:
        con.say(f"Could not read the settings: {e}")
        return 1
    except (KeyboardInterrupt, EOFError):
        con.say("", "Stopped.")
        return 130
