"""Passwords and API tokens, kept out of the config file, argv, logs and error messages.

Secrets are stored by name ("instapaper", "gmail_app_password", "cloudflare_token") in the system
keychain through the `keyring` library. Where no keychain works (a headless Linux box, a scheduler
without a login session), they fall back to a file only the owner can read, secrets.json in the
config directory (~/.config/inboxink/ unless INBOXINK_CONFIG points elsewhere).

The Instapaper login is two values, so it is stored as one JSON text {"user": ..., "password": ...}
(see get_instapaper / set_instapaper). It can also come from two older places, tried after the
store: an env_file holding INSTAPAPER_USER and INSTAPAPER_PASSWORD, or a macOS Keychain item.

Nothing here puts a secret in a command line, and no error message contains one.
"""
import json, os, subprocess

from . import config

SERVICE = "inboxink"
NAMES = ("instapaper", "gmail_app_password", "cloudflare_token")


class CredentialError(RuntimeError):
    """A secret is missing or unusable. The message never contains the secret."""


def _check_name(name):
    if name not in NAMES:
        raise ValueError(f"unknown secret name {name!r}; expected one of {', '.join(NAMES)}")


def _secrets_file():
    return os.path.join(config.config_dir(), "secrets.json")


def _keyring():
    """The keyring module if a real backend is available, else None."""
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


# --- 0600 file fallback --------------------------------------------------------------------------

def _read_file():
    path = _secrets_file()
    try:
        if os.stat(path).st_mode & 0o077:
            raise CredentialError(f"{path} is readable by other users; run: chmod 600 {path}")
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        raise CredentialError(f"{path} is not valid JSON; fix or delete it and run `inboxink setup` again") from None


def _write_file(data):
    path = _secrets_file()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


# --- public API ----------------------------------------------------------------------------------

def get_secret(name):
    """The stored value, or None. Looks in the keychain first, then the fallback file."""
    _check_name(name)
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, name)
            if value:
                return value
        except Exception:
            pass  # a locked or broken keychain: fall through to the file
    return _read_file().get(name) or None


def set_secret(name, value):
    """Store a secret. Returns where it went: "keychain" or the path of the 0600 file."""
    _check_name(name)
    if not value:
        raise ValueError("refusing to store an empty secret")
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, name, value)
            return "keychain"
        except Exception:
            pass
    data = _read_file()
    data[name] = value
    _write_file(data)
    return _secrets_file()


def delete_secret(name):
    """Remove a secret from both places. Missing is not an error."""
    _check_name(name)
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(SERVICE, name)
        except Exception:
            pass
    data = _read_file()
    if data.pop(name, None) is not None:
        _write_file(data)


def set_instapaper(user, password):
    return set_secret("instapaper", json.dumps({"user": user, "password": password}))


# --- Instapaper, including the older places ------------------------------------------------------

def _instapaper_from_store():
    raw = get_secret("instapaper")
    if not raw:
        return "", ""
    try:
        d = json.loads(raw)
        return d.get("user", ""), d.get("password", "")
    except (json.JSONDecodeError, AttributeError):
        raise CredentialError("the stored Instapaper login is damaged; run `inboxink setup` to enter it again") from None


def _instapaper_from_file(path):
    """INSTAPAPER_USER / INSTAPAPER_PASSWORD lines in a file only the owner can read."""
    try:
        if os.stat(path).st_mode & 0o077:
            raise CredentialError(f"{path} is readable by other users; run: chmod 600 {path}")
        with open(path) as f:
            kv = dict(line.rstrip("\n").split("=", 1) for line in f if "=" in line)
    except FileNotFoundError:
        return "", ""
    kv = {k.strip(): v.strip().strip('"').strip("'") for k, v in kv.items()}
    return kv.get("INSTAPAPER_USER", ""), kv.get("INSTAPAPER_PASSWORD", "")


def _instapaper_from_keychain_item(service):
    """A macOS Keychain generic-password item: account = Instapaper login, password = its password."""
    try:
        meta = subprocess.run(["security", "find-generic-password", "-s", service],
                              capture_output=True, text=True)
        pw = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                            capture_output=True, text=True)
    except FileNotFoundError:  # not macOS
        return "", ""
    if pw.returncode != 0:
        return "", ""
    acct = next((l.split("=", 1)[1].strip().strip('"') for l in meta.stdout.splitlines()
                 if l.strip().startswith('"acct"')), "")
    return acct, pw.stdout.rstrip("\n")


def get_instapaper(cfg=None):
    """(user, password). Order: the configured env_file, the inboxink store, the configured Keychain item."""
    cfg = cfg or config.get()
    user = pw = ""
    if cfg.instapaper.env_file:
        user, pw = _instapaper_from_file(cfg.instapaper.env_file)
    if not (user and pw):
        user, pw = _instapaper_from_store()
    if not (user and pw) and cfg.instapaper.keychain_service:
        user, pw = _instapaper_from_keychain_item(cfg.instapaper.keychain_service)
    if not (user and pw):
        raise CredentialError("no Instapaper login found: run `inboxink setup`, or set [instapaper] env_file "
                              "in the config (see config.example.toml)")
    return user, pw
