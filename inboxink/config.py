"""InboxInk settings: one TOML file, with environment overrides.

The file is $INBOXINK_CONFIG, else ~/.config/inboxink/config.toml. Any scalar key can be
overridden by an environment variable named INBOXINK_<SECTION>_<KEY> in capitals, for example
INBOXINK_WORKER_BASE_URL or INBOXINK_GMAIL_LABELS_DONE. Every key is documented in
config.example.toml at the repository root.

Modules call get() at the moment they need a value, so a missing or broken config surfaces as
one clear ConfigError instead of a traceback from somewhere deep in the pipeline.
"""
import dataclasses, os, re, tomllib, typing
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_PATH = "~/.config/inboxink/config.toml"
KINDS = ("imap", "gog")
EXAMPLE_URL = "https://github.com/Fletchwork/inboxink/blob/main/config.example.toml"


class ConfigError(Exception):
    """The configuration is missing, malformed or incomplete. The message says what to fix."""


@dataclass(frozen=True)
class SourceConfig:
    kind: str = "imap"  # "imap" (default) or "gog"
    newsletters_only: bool = False  # true: every inbox message is a newsletter; false: only the label


@dataclass(frozen=True)
class LabelsConfig:
    source: str = "Newsletter"
    done: str = "Newsletter/Delivered"
    failed: str = "Newsletter/Failed"


@dataclass(frozen=True)
class GmailConfig:
    address: str = ""  # required for kind = "imap"
    labels: LabelsConfig = field(default_factory=LabelsConfig)
    archive_on_done: bool = True


@dataclass(frozen=True)
class GogConfig:
    account: str = ""  # required for kind = "gog"
    bin: str = "gog"


@dataclass(frozen=True)
class WorkerConfig:
    base_url: str = ""  # required
    kv_namespace_id: str = ""  # required
    account_id: str = ""  # optional; the setup wizard uses it
    ttl_days: int = 30


@dataclass(frozen=True)
class ReaderConfig:
    own_addresses: tuple = ()  # lowercased; gmail.address is added automatically
    publication_names: dict = field(default_factory=dict)  # lowercased sender address -> name
    timezone: str = ""  # "" means the system's local zone
    max_image_width: int = 1264


@dataclass(frozen=True)
class CoverConfig:
    font: str = ""  # "" means try common system fonts, then Pillow's built-in one


@dataclass(frozen=True)
class PathsConfig:
    state_dir: str = "~/.local/state/inboxink"
    cache_dir: str = "~/.cache/inboxink"
    log: str = "~/.local/state/inboxink/inboxink.log"

    @property
    def state_file(self):
        return os.path.join(self.state_dir, "state.json")

    @property
    def icon_cache(self):
        return os.path.join(self.cache_dir, "icons")


@dataclass(frozen=True)
class InstapaperConfig:
    env_file: str = ""  # optional file holding INSTAPAPER_USER / INSTAPAPER_PASSWORD
    keychain_service: str = "inboxink-instapaper"  # macOS Keychain item read as a fallback


@dataclass(frozen=True)
class AdstripConfig:
    enabled: bool = False  # off unless the user turns it on: newsletter text goes to a model
    model: str = "haiku"


@dataclass(frozen=True)
class ScheduleConfig:
    interval_minutes: int = 30
    label: str = "io.github.fletchwork.inboxink"


@dataclass(frozen=True)
class UpdatesConfig:
    auto: bool = True


@dataclass(frozen=True)
class Config:
    source: SourceConfig = field(default_factory=SourceConfig)
    gmail: GmailConfig = field(default_factory=GmailConfig)
    gog: GogConfig = field(default_factory=GogConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    cover: CoverConfig = field(default_factory=CoverConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    instapaper: InstapaperConfig = field(default_factory=InstapaperConfig)
    adstrip: AdstripConfig = field(default_factory=AdstripConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    updates: UpdatesConfig = field(default_factory=UpdatesConfig)
    config_file: str = ""  # where the settings were read from (may not exist)

    @property
    def tzinfo(self):
        """The reader's zone, or None for the system's local zone (what astimezone(None) uses)."""
        return ZoneInfo(self.reader.timezone) if self.reader.timezone else None


def config_path(env=None):
    env = os.environ if env is None else env
    return os.path.abspath(os.path.expanduser(env.get("INBOXINK_CONFIG") or DEFAULT_PATH))


def config_dir(env=None):
    """Directory holding the config file; credentials.py keeps its fallback secrets file here."""
    return os.path.dirname(config_path(env))


# --- loading ----------------------------------------------------------------------------------

_TRUE, _FALSE = {"1", "true", "yes", "on"}, {"0", "false", "no", "off"}


def _is_dc(tp):
    return dataclasses.is_dataclass(tp)


def _scalar_kind(tp):
    """'str' | 'bool' | 'int' for env-overridable fields, else None."""
    if typing.get_origin(tp) is typing.Union:  # Optional[bool]
        args = [a for a in typing.get_args(tp) if a is not type(None)]
        tp = args[0] if len(args) == 1 else tp
    return {str: "str", bool: "bool", int: "int"}.get(tp)


def _coerce(raw, kind, name, source):
    """raw is a TOML value or an env string. Returns the typed value or raises ConfigError."""
    if source == "env":
        if kind == "bool":
            low = raw.strip().lower()
            if low in _TRUE:
                return True
            if low in _FALSE:
                return False
            raise ConfigError(f"{name} must be true or false (got a different value)")
        if kind == "int":
            try:
                return int(raw.strip())
            except ValueError:
                raise ConfigError(f"{name} must be a whole number") from None
        return raw
    ok = {"str": isinstance(raw, str), "bool": isinstance(raw, bool),
          "int": isinstance(raw, int) and not isinstance(raw, bool)}[kind]
    if not ok:
        want = {"str": "text in quotes", "bool": "true or false", "int": "a whole number"}[kind]
        raise ConfigError(f"{name} must be {want}")
    return raw


def _build(cls, data, path, env):
    """Instantiate dataclass `cls` from the TOML table `data`, applying env overrides."""
    where = ".".join(path) if path else "top level"
    if not isinstance(data, dict):
        raise ConfigError(f"[{where}] must be a table (a [section] heading)")
    hints = typing.get_type_hints(cls)
    known = {f.name for f in dataclasses.fields(cls)} - {"config_file"}
    for k in data:
        if k not in known:
            raise ConfigError(f"unknown setting '{'.'.join(path + [k])}'; check the spelling against {EXAMPLE_URL}")
    values = {}
    for f in dataclasses.fields(cls):
        if f.name == "config_file":
            continue
        tp, full = hints[f.name], path + [f.name]
        dotted = ".".join(full)
        if _is_dc(tp):
            values[f.name] = _build(tp, data.get(f.name, {}), full, env)
            continue
        kind = _scalar_kind(tp)
        env_name = "INBOXINK_" + "_".join(full).upper()
        if kind and env_name in env:
            values[f.name] = _coerce(env[env_name], kind, env_name, "env")
        elif f.name in data:
            raw = data[f.name]
            if kind:
                values[f.name] = _coerce(raw, kind, dotted, "toml")
            elif f.name == "own_addresses":
                if not (isinstance(raw, list) and all(isinstance(x, str) for x in raw)):
                    raise ConfigError(f"{dotted} must be a list of text values, like [\"me@example.com\"]")
                values[f.name] = tuple(raw)
            elif f.name == "publication_names":
                if not (isinstance(raw, dict) and all(isinstance(v, str) for v in raw.values())):
                    raise ConfigError(f"{dotted} must be a table of \"address\" = \"Name\" pairs")
                values[f.name] = dict(raw)
    return cls(**values)


def _normalize(cfg, file):
    """Expand paths, lowercase addresses, resolve the dynamic defaults."""
    own = []
    for a in (*cfg.reader.own_addresses, cfg.gmail.address):
        a = a.strip().lower()
        if a and a not in own:
            own.append(a)
    reader = dataclasses.replace(
        cfg.reader, own_addresses=tuple(own),
        publication_names={k.strip().lower(): v for k, v in cfg.reader.publication_names.items()})
    paths = PathsConfig(*(os.path.abspath(os.path.expanduser(p)) for p in
                          (cfg.paths.state_dir, cfg.paths.cache_dir, cfg.paths.log)))
    worker = dataclasses.replace(cfg.worker, base_url=cfg.worker.base_url.strip().rstrip("/"),
                                 kv_namespace_id=cfg.worker.kv_namespace_id.strip())
    cover = dataclasses.replace(cfg.cover, font=os.path.expanduser(cfg.cover.font))
    gog = dataclasses.replace(cfg.gog, bin=os.path.expanduser(cfg.gog.bin))
    inst = dataclasses.replace(cfg.instapaper, env_file=os.path.expanduser(cfg.instapaper.env_file))
    return dataclasses.replace(cfg, reader=reader, paths=paths, worker=worker, cover=cover,
                               gog=gog, instapaper=inst, config_file=file)


def _validate(cfg, file, env):
    exists = os.path.exists(file)
    where = file if exists else f"{file} (that file does not exist yet)"
    hint = f"Every setting is listed in {EXAMPLE_URL}"
    if cfg.source.kind not in KINDS:
        raise ConfigError(f"source.kind must be \"imap\" or \"gog\", not \"{cfg.source.kind}\". {hint}")
    required = [("worker", "base_url", cfg.worker.base_url), ("worker", "kv_namespace_id", cfg.worker.kv_namespace_id)]
    if cfg.source.kind == "imap":
        required.insert(0, ("gmail", "address", cfg.gmail.address))
    else:
        required.insert(0, ("gog", "account", cfg.gog.account))
    missing = [(s, k) for s, k, v in required if not v]
    if missing:
        lines = "\n".join(f"  - {s}.{k}   (add `{k} = \"...\"` under [{s}], or set INBOXINK_{s.upper()}_{k.upper()})"
                          for s, k in missing)
        raise ConfigError(f"missing required setting(s) in {where}:\n{lines}\n{hint}")

    def bad(key, why):
        raise ConfigError(f"{key} {why} (in {where}). {hint}")
    if not cfg.worker.base_url.startswith(("https://", "http://")):
        bad("worker.base_url", "must start with http:// or https://")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", cfg.worker.kv_namespace_id):
        bad("worker.kv_namespace_id", "must be the 32-character id Cloudflare shows for the KV namespace")
    if cfg.source.kind == "imap" and "@" not in cfg.gmail.address:
        bad("gmail.address", "must be an email address")
    for key, v in (("worker.ttl_days", cfg.worker.ttl_days), ("schedule.interval_minutes", cfg.schedule.interval_minutes),
                   ("reader.max_image_width", cfg.reader.max_image_width)):
        if v < 1:
            bad(key, "must be 1 or more")
    for key, v in (("gmail.labels.source", cfg.gmail.labels.source), ("gmail.labels.done", cfg.gmail.labels.done),
                   ("gmail.labels.failed", cfg.gmail.labels.failed)):
        if not v.strip():
            bad(key, "must not be empty")
    if len({cfg.gmail.labels.source, cfg.gmail.labels.done, cfg.gmail.labels.failed}) < 3:
        bad("gmail.labels", "source, done and failed must be three different names")
    try:
        cfg.tzinfo
    except (ZoneInfoNotFoundError, ValueError, OSError):
        bad("reader.timezone", f"\"{cfg.reader.timezone}\" is not a known time zone name, like \"America/Chicago\"")


def load(path=None, env=None, validate=True):
    """Read the settings. A missing file is fine when the environment supplies the required keys.
    validate=False skips the required-key checks (for tools that only read defaults, like install)."""
    env = dict(os.environ if env is None else env)
    file = os.path.abspath(os.path.expanduser(path)) if path else config_path(env)
    data = {}
    if os.path.exists(file):
        try:
            with open(file, "rb") as f:
                data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{file} is not valid TOML: {e}") from None
        except OSError as e:
            raise ConfigError(f"cannot read {file}: {e.strerror}") from None
    cfg = _normalize(_build(Config, data, [], env), file)
    if validate:
        _validate(cfg, file, env)
    return cfg


_current = None


def get():
    """The process-wide settings, loaded on first use."""
    global _current
    if _current is None:
        _current = load()
    return _current


def set_current(cfg):
    """Install a Config (tests, or a caller that already loaded one)."""
    global _current
    _current = cfg
