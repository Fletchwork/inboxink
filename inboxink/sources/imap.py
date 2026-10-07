"""Gmail over IMAP with an app password (the default source), for personal Gmail accounts.

Selected with source.kind = "imap". Needs gmail.address and the "gmail_app_password" secret
(credentials.py; `inboxink setup` stores it). Google Workspace accounts usually cannot make app
passwords; use source.kind = "gog" for those.

Everything happens in Gmail's "All Mail" folder, found through its \\All special-use flag so a
localized folder name does not matter. A message is identified by its X-GM-MSGID, which is the same
in every folder and never changes (a UID is per folder), and that decimal string is the Pending id.
Gmail labels are IMAP folders ("Newsletter/Delivered" is the folder "Delivered" inside "Newsletter"),
but they are applied and removed with the X-GM-LABELS extension, which does not move anything.

pending() sends one Gmail search (X-GM-RAW) for the same labels the gog source searches for, and
`query` is appended to it, so Gmail search syntax such as newer_than:1d works. Results are then checked
against each message's real label list. Messages are fetched with BODY.PEEK, which leaves them unread.
"""
import base64, email.utils, imaplib, re, ssl

from .. import credentials
from . import Pending, Source

HOST, PORT = "imap.gmail.com", 993
CHUNK = 100  # UIDs per metadata FETCH
NO_PASSWORD = ("no Gmail app password stored: run `inboxink setup`, or create one at "
               "https://myaccount.google.com/apppasswords")
BAD_PASSWORD = ("Gmail would not sign in. Check the Gmail address and the app password. Make a new app password at "
                "https://myaccount.google.com/apppasswords (needs 2-Step Verification; work and school accounts "
                "usually can't)")


# --- text helpers ------------------------------------------------------------------------------

def _quote(s):
    """An IMAP quoted string. Backslash and double quote are escaped; line breaks and NUL are refused."""
    if re.search(r"[\r\n\x00]", s):
        raise ValueError("line breaks are not allowed in a mail search or label name")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _utf7(s):
    """Modified UTF-7 (RFC 3501) for mailbox and label names with characters outside printable ASCII."""
    out, buf = [], []

    def flush():
        if buf:
            raw = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=")
            out.append("&" + raw.replace("/", ",") + "-")
            buf.clear()
    for ch in s:
        if " " <= ch <= "~":
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def _unutf7(s):
    def dec(m):
        if not m.group(1):
            return "&"
        b = m.group(1).replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")
    return re.sub(r"&([^-]*)-", dec, s)


def _what(name, args):
    """The command in words for an error message: 'UID FETCH', 'SELECT'."""
    return f"UID {args[0]}" if name == "uid" and args else name.upper()


def _mailbox(name):
    return _quote(_utf7(name))


def _q(label):
    """Gmail search spells nested/spaced labels with hyphens: 'Newsletter/Delivered' -> newsletter-delivered.
    Anything that could act as search syntax is turned into a hyphen too."""
    return re.sub(r"[^\w.+@&-]+", "-", label.lower()).strip("-")


def _unquote(s):
    return re.sub(r"\\(.)", r"\1", s[1:-1])


_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s()"]+)|(\))')


def _label_list(text, pos):
    """Parse the (...) list of atoms and quoted strings that starts at text[pos] == '('."""
    out = []
    for m in _TOKEN.finditer(text, pos + 1):
        if m.group(3):
            break
        out.append(_unutf7(re.sub(r"\\(.)", r"\1", m.group(1)) if m.group(1) is not None else m.group(2)))
    return out


def _responses(data):
    """Flatten imaplib's FETCH data into one string per response, folding literals back in as quoted text."""
    out, joined = [], False
    for item in data:
        if isinstance(item, tuple):
            head = re.sub(r"\{\d+\}$", "", item[0].decode("utf-8", "replace"))
            piece = head + _quote(item[1].decode("utf-8", "replace"))
            if joined:
                out[-1] += piece
            else:
                out.append(piece)
            joined = True
        elif isinstance(item, bytes):
            piece = item.decode("utf-8", "replace")
            if joined:
                out[-1] += piece
            else:
                out.append(piece)
            joined = False
    return out


def _epoch_ms(internaldate):
    """'06-Oct-2026 10:00:00 +0000' -> milliseconds since the epoch (independent of the system locale)."""
    return int(email.utils.parsedate_to_datetime(internaldate.replace("-", " ")).timestamp() * 1000)


_LIST = re.compile(r'^\((?P<flags>[^)]*)\) (?P<delim>"(?:[^"\\]|\\.)*"|NIL) (?P<name>.*)$', re.S)


def _parse_list(data):
    """[(name, flags)] from LIST responses."""
    out = []
    for item in data:
        if isinstance(item, tuple):  # a literal mailbox name
            head = item[0].decode("utf-8", "replace")
            m = _LIST.match(re.sub(r"\{\d+\}$", "", head).rstrip() + " x")
            if m:
                out.append((_unutf7(item[1].decode("utf-8", "replace")), m.group("flags").split()))
            continue
        if not isinstance(item, bytes):
            continue
        m = _LIST.match(item.decode("utf-8", "replace"))
        if not m:
            continue
        name = m.group("name").strip()
        name = _unquote(name) if name.startswith('"') else name
        out.append((_unutf7(name), m.group("flags").split()))
    return out


class ImapSource(Source):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.conn = None
        self.all_mail = None
        self._selected = False

    # --- connection ------------------------------------------------------------------------------

    def _connect(self):
        password = credentials.get_secret("gmail_app_password")
        if not password:
            raise RuntimeError(NO_PASSWORD)
        try:
            conn = imaplib.IMAP4_SSL(HOST, PORT, ssl_context=ssl.create_default_context(), timeout=60)
        except (OSError, imaplib.IMAP4.error) as e:
            raise RuntimeError(f"cannot connect to {HOST}: {e}") from None
        try:
            conn.login(self.cfg.gmail.address, password)
        except imaplib.IMAP4.error:
            self._drop(conn)
            raise RuntimeError(BAD_PASSWORD) from None
        except OSError as e:
            self._drop(conn)
            raise RuntimeError(f"lost the connection to {HOST} while signing in: {e}") from None
        self.conn, self._selected = conn, False

    @staticmethod
    def _drop(conn):
        try:
            conn.logout()
        except Exception:
            pass

    def _run(self, name, *args, selected=False, literal=None):
        """Run one imaplib command and return its data. A dropped connection is re-opened once.
        selected=True makes sure All Mail is the selected folder first."""
        for attempt in (0, 1):
            try:
                if selected and not self._selected:
                    self._call("select", _mailbox(self.all_mail))
                    self._selected = True
                return self._call(name, *args, literal=literal)
            except (imaplib.IMAP4.abort, OSError) as e:
                if attempt:
                    raise RuntimeError(f"lost the connection to {HOST}: {e}") from None
                if self.conn is not None:
                    self._drop(self.conn)
                self.conn = None
                self._connect()
            except imaplib.IMAP4.error as e:
                raise RuntimeError(f"Gmail did not accept a request ({_what(name, args)}): {e}") from None

    def _call(self, name, *args, literal=None):
        if literal is not None:
            self.conn.literal = literal
        typ, data = getattr(self.conn, name)(*args)
        if typ != "OK":
            detail = data[0].decode("utf-8", "replace") if data and isinstance(data[0], bytes) else ""
            raise RuntimeError(f"Gmail did not accept a request ({_what(name, args)}): {detail[:200]}")
        return data

    def close(self):
        if self.conn is not None:
            self._drop(self.conn)
        self.conn = None

    # --- Source interface ------------------------------------------------------------------------

    def _wanted_labels(self):
        lab = self.cfg.gmail.labels
        return (lab.done, lab.failed) if self.cfg.source.newsletters_only else (lab.source, lab.done, lab.failed)

    def open(self, create=True):
        if self.conn is None:
            self._connect()
        boxes = _parse_list(self._run("list", '""', '"*"'))
        self.all_mail = next((n for n, flags in boxes if "\\All" in flags), None)
        if self.all_mail is None:
            raise RuntimeError("Gmail's All Mail folder is not visible over IMAP; in Gmail settings, under "
                               "Labels, turn on 'Show in IMAP' for All Mail")
        have = {n.casefold() for n, _ in boxes}
        for name in self._wanted_labels():
            if name.casefold() in have:
                continue
            if not create:
                raise RuntimeError(f"Gmail label '{name}' does not exist yet. 'inboxink run' creates it.")
            try:
                self._run("create", _mailbox(name))
            except RuntimeError as e:
                if "ALREADYEXISTS" not in str(e).upper():
                    raise

    def _search(self, *criteria, literal=None):
        data = self._run("uid", "SEARCH", *criteria, selected=True, literal=literal)
        return sorted({int(u) for chunk in data if chunk for u in chunk.split()})

    def _uid_of(self, message_id):
        if not re.fullmatch(r"\d+", str(message_id)):
            raise ValueError("a Gmail message id is a string of digits")
        uids = self._search("X-GM-MSGID", str(message_id))
        if not uids:
            raise RuntimeError(f"message {message_id} not found in Gmail (deleted or moved to Trash?)")
        return str(uids[0])

    def pending(self, query="", limit=50):
        lab, inbox_only = self.cfg.gmail.labels, self.cfg.source.newsletters_only
        where = "in:inbox" if inbox_only else f"label:{_q(lab.source)}"
        q = f"{where} -label:{_q(lab.done)} -label:{_q(lab.failed)} {query}".strip()
        if q.isascii():
            uids = self._search("X-GM-RAW", _quote(q))
        else:  # a non-ASCII search goes as an IMAP literal
            uids = self._search("X-GM-RAW", literal=q.encode("utf-8"))
        skip = {lab.done.casefold(), lab.failed.casefold()}
        need = lab.source.casefold()
        found = {}
        for i in range(0, len(uids), CHUNK):  # oldest UIDs first; stop once `limit` real matches are in hand
            if len(found) >= limit:
                break
            data = self._run("uid", "FETCH", ",".join(map(str, uids[i:i + CHUNK])),
                             "(X-GM-MSGID X-GM-LABELS INTERNALDATE)", selected=True)
            for text in _responses(data):
                mid = re.search(r"X-GM-MSGID (\d+)", text)
                when = re.search(r'INTERNALDATE "([^"]+)"', text)
                lbl = re.search(r"X-GM-LABELS \(", text)
                if not (mid and when and lbl):
                    continue
                have = {x.casefold() for x in _label_list(text, lbl.end() - 1)}
                if have & skip:
                    continue
                if not ("\\inbox" in have if inbox_only else need in have):
                    continue
                found[mid.group(1)] = Pending(mid.group(1), _epoch_ms(when.group(1)))
        return sorted(found.values(), key=lambda p: (p.date, p.id))[:limit]

    def fetch_raw(self, message_id):
        uid = self._uid_of(message_id)
        data = self._run("uid", "FETCH", uid, "(BODY.PEEK[])", selected=True)
        for item in data:
            if isinstance(item, tuple):
                return bytes(item[1])
        raise RuntimeError(f"Gmail returned no body for message {message_id}")

    def _store(self, uid, op, labels):
        self._run("uid", "STORE", uid, op, "(" + " ".join(labels) + ")", selected=True)

    def mark_done(self, message_id):
        """Same as the gog source: label it done and mark it read; leave the inbox when archive_on_done."""
        uid = self._uid_of(message_id)
        self._store(uid, "+X-GM-LABELS", [_mailbox(self.cfg.gmail.labels.done)])
        self._run("uid", "STORE", uid, "+FLAGS", "(\\Seen)", selected=True)
        if self.cfg.gmail.archive_on_done:
            self._store(uid, "-X-GM-LABELS", ["\\Inbox"])

    def mark_failed(self, message_id):
        self._store(self._uid_of(message_id), "+X-GM-LABELS", [_mailbox(self.cfg.gmail.labels.failed)])
