"""The IMAP source against a fake Gmail server (no network)."""
import imaplib
from dataclasses import replace

import pytest

from inboxink import credentials
from inboxink.sources import Pending
from inboxink.sources import imap as imap_mod
from inboxink.sources.imap import ImapSource, _label_list, _quote, _unutf7, _utf7

PASSWORD = "abcd efgh ijkl mnop"
ALL_MAIL = "[Gmail]/All Mail"
LIST_LINES = [
    b'(\\HasNoChildren) "/" "INBOX"',
    b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"',
    b'(\\HasChildren) "/" "Newsletter"',
    b'(\\HasNoChildren) "/" "Newsletter/Delivered"',
    b'(\\HasNoChildren) "/" "Newsletter/Failed"',
]


class FakeGmail:
    """Stands in for imaplib.IMAP4_SSL: records every command, answers from canned data."""
    instances = []
    msgs = {}  # uid -> dict(msgid, labels, date, raw)
    raw_hits = []  # what UID SEARCH X-GM-RAW returns
    list_lines = LIST_LINES
    login_error = None
    drop_on = None  # (instance index, command name) -> raise abort once

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port, self.ctx, self.timeout = host, port, ssl_context, timeout
        self.cmds, self.literal, self.index = [], None, len(FakeGmail.instances)
        FakeGmail.instances.append(self)

    def _rec(self, name, *args):
        self.cmds.append((name, *args))
        if FakeGmail.drop_on == (self.index, name):
            FakeGmail.drop_on = None
            raise imaplib.IMAP4.abort("socket error: EOF")

    def login(self, user, password):
        self._rec("login", user, "<pw>")
        if FakeGmail.login_error:
            raise imaplib.IMAP4.error(FakeGmail.login_error)
        return "OK", [b"signed in"]

    def logout(self):
        self._rec("logout")
        return "BYE", [b""]

    def list(self, directory='""', pattern="*"):
        self._rec("list", directory, pattern)
        return "OK", list(FakeGmail.list_lines)

    def select(self, mailbox="INBOX", readonly=False):
        self._rec("select", mailbox)
        return "OK", [b"1"]

    def create(self, mailbox):
        self._rec("create", mailbox)
        if mailbox == '"Newsletter/Failed"' and not any(b"Newsletter/Failed" in l for l in FakeGmail.list_lines):
            return "NO", [b"[ALREADYEXISTS] Duplicate folder name Newsletter/Failed (Failure)"]
        return "OK", [b"Success"]

    def uid(self, command, *args):
        self._rec("uid", command, *args)
        if command == "SEARCH" and args[0] == "X-GM-RAW":
            return "OK", [b" ".join(str(u).encode() for u in FakeGmail.raw_hits)]
        if command == "SEARCH" and args[0] == "X-GM-MSGID":
            hit = [u for u, m in FakeGmail.msgs.items() if m["msgid"] == args[1]]
            return "OK", [b" ".join(str(u).encode() for u in hit)]
        if command == "FETCH" and args[1] == "(BODY.PEEK[])":
            m = FakeGmail.msgs[int(args[0])]
            return "OK", [(b"1 (UID %s BODY[] {%d}" % (args[0].encode(), len(m["raw"])), m["raw"]), b")"]
        if command == "FETCH":
            out = []
            for u in args[0].split(","):
                m = FakeGmail.msgs[int(u)]
                labels = " ".join(_quote(l) if " " in l or "\\" in l else l for l in m["labels"])
                out.append(f'{u} (X-GM-MSGID {m["msgid"]} X-GM-LABELS ({labels}) UID {u} '
                           f'INTERNALDATE "{m["date"]}")'.encode())
            return "OK", out
        if command == "STORE":
            return "OK", [b"Success"]
        raise AssertionError(f"unexpected command {command} {args}")


@pytest.fixture
def gmail(monkeypatch):
    FakeGmail.instances, FakeGmail.raw_hits = [], []
    FakeGmail.msgs, FakeGmail.list_lines = {}, LIST_LINES
    FakeGmail.login_error = FakeGmail.drop_on = None
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeGmail)
    monkeypatch.setattr(credentials, "get_secret", lambda name: PASSWORD if name == "gmail_app_password" else None)
    return FakeGmail


def msg(uid, msgid, labels, date="06-Oct-2026 10:00:00 +0000", raw=b"Subject: hi\r\n\r\nbody"):
    FakeGmail.msgs[uid] = {"msgid": msgid, "labels": labels, "date": date, "raw": raw}


def source(cfg, newsletters_only=False, archive=True):
    cfg = replace(cfg, source=replace(cfg.source, kind="imap", newsletters_only=newsletters_only),
                  gmail=replace(cfg.gmail, archive_on_done=archive))
    return ImapSource(cfg)


def cmds(name):
    return [c for inst in FakeGmail.instances for c in inst.cmds if c[0] == name]


def uid_cmds(verb):
    return [c[2:] for c in cmds("uid") if c[1] == verb]


# --- connecting -------------------------------------------------------------------------------------

def test_open_logs_in_over_tls_and_finds_all_mail_by_flag(pinned_config, gmail):
    src = source(pinned_config)
    src.open()
    inst = gmail.instances[0]
    assert (inst.host, inst.port) == ("imap.gmail.com", 993)
    assert inst.ctx is not None and inst.timeout
    assert inst.cmds[0] == ("login", "reader@example.com", "<pw>")
    assert src.all_mail == ALL_MAIL
    assert not cmds("create")  # all three labels already exist


def test_open_finds_a_localized_all_mail_folder(pinned_config, gmail):
    gmail.list_lines = [b'(\\All \\HasNoChildren) "/" "[Gmail]/Alle Nachrichten"',
                        b'(\\HasNoChildren) "/" "Newsletter"', b'(\\HasNoChildren) "/" "Newsletter/Delivered"',
                        b'(\\HasNoChildren) "/" "Newsletter/Failed"']
    src = source(pinned_config)
    src.open()
    assert src.all_mail == "[Gmail]/Alle Nachrichten"


def test_login_failure_names_the_fix_and_never_the_password(pinned_config, gmail):
    gmail.login_error = "[AUTHENTICATIONFAILED] Invalid credentials (Failure)"
    with pytest.raises(RuntimeError) as e:
        source(pinned_config).open()
    text = str(e.value)
    assert "myaccount.google.com/apppasswords" in text and "2-Step Verification" in text
    assert "Gmail would not sign in" in text and "usually can't" in text
    assert PASSWORD not in text and "abcd" not in text
    assert e.value.__cause__ is None and e.value.__suppress_context__
    assert "logout" in [c[0] for c in gmail.instances[0].cmds]  # the failed connection is released


def test_a_refused_request_is_named_in_plain_words(pinned_config, gmail, monkeypatch):
    src = source(pinned_config)
    src.open()
    monkeypatch.setattr(FakeGmail, "uid", lambda self, *a: ("NO", [b"[SERVERBUG] Try again"]))
    with pytest.raises(RuntimeError, match=r"Gmail did not accept a request \(UID SEARCH\): \[SERVERBUG\]"):
        src.fetch_raw("123")


def test_missing_password_is_a_clear_error(pinned_config, gmail, monkeypatch):
    monkeypatch.setattr(credentials, "get_secret", lambda name: None)
    with pytest.raises(RuntimeError, match="no Gmail app password"):
        source(pinned_config).open()
    assert not gmail.instances


def test_open_creates_missing_labels_and_ignores_already_exists(pinned_config, gmail):
    gmail.list_lines = LIST_LINES[:2]  # no Newsletter labels visible
    source(pinned_config).open(create=True)
    assert [c[1] for c in cmds("create")] == ['"Newsletter"', '"Newsletter/Delivered"', '"Newsletter/Failed"']


def test_open_dry_run_creates_nothing_and_raises_when_missing(pinned_config, gmail):
    gmail.list_lines = LIST_LINES[:2]
    with pytest.raises(RuntimeError, match="'Newsletter' does not exist yet. 'inboxink run' creates it"):
        source(pinned_config).open(create=False)
    assert not cmds("create")


def test_newsletters_only_does_not_need_the_source_label(pinned_config, gmail):
    gmail.list_lines = [LIST_LINES[1], LIST_LINES[3], LIST_LINES[4]]
    source(pinned_config, newsletters_only=True).open(create=False)  # no "Newsletter" label required


def test_close_tolerates_errors(pinned_config, gmail, monkeypatch):
    src = source(pinned_config)
    src.open()
    monkeypatch.setattr(src.conn, "logout", lambda: (_ for _ in ()).throw(OSError("gone")))
    src.close()
    src.close()  # twice is fine


# --- pending ----------------------------------------------------------------------------------------

def test_pending_label_mode_excludes_done_and_failed_and_sorts_oldest_first(pinned_config, gmail):
    msg(7, "1001", ["Newsletter"], "06-Oct-2026 12:00:00 +0000")
    msg(8, "1002", ["Newsletter", "Newsletter/Delivered"])
    msg(9, "1003", ["Newsletter", "Newsletter/Failed"])
    msg(10, "1004", ["Newsletter", "\\Inbox"], "05-Oct-2026 08:30:00 +0000")
    msg(11, "1005", ["\\Inbox"])  # search over-matched; the label check drops it
    gmail.raw_hits = [7, 8, 9, 10, 11]
    src = source(pinned_config)
    src.open()
    out = src.pending("newer_than:3d", 50)
    assert out == [Pending("1004", 1791189000000), Pending("1001", 1791288000000)]
    (_, raw_q), = [a for a in uid_cmds("SEARCH") if a[0] == "X-GM-RAW"]
    assert raw_q == '"label:newsletter -label:newsletter-delivered -label:newsletter-failed newer_than:3d"'


def test_pending_newsletters_only_uses_the_inbox(pinned_config, gmail):
    msg(7, "2001", ["\\Inbox"])
    msg(8, "2002", ["\\Inbox", "Newsletter/Delivered"])
    msg(9, "2003", ["Newsletter"])  # not in the inbox any more
    gmail.raw_hits = [7, 8, 9]
    src = source(pinned_config, newsletters_only=True)
    src.open()
    assert [p.id for p in src.pending()] == ["2001"]
    (_, raw_q), = [a for a in uid_cmds("SEARCH") if a[0] == "X-GM-RAW"]
    assert raw_q.startswith('"in:inbox -label:newsletter-delivered')


def test_pending_honours_limit_and_stops_fetching_early(pinned_config, gmail):
    for i in range(250):
        msg(i + 1, str(5000 + i), ["Newsletter"], f"06-Oct-2026 10:{i % 60:02d}:00 +0000")
    gmail.raw_hits = list(range(1, 251))
    src = source(pinned_config)
    src.open()
    out = src.pending(limit=5)
    assert len(out) == 5
    assert len(uid_cmds("FETCH")) == 1  # one 100-UID chunk was enough


def test_labels_with_spaces_and_quotes_are_escaped_in_create_and_search(pinned_config, gmail):
    cfg = replace(pinned_config, gmail=replace(pinned_config.gmail, labels=replace(
        pinned_config.gmail.labels, source='Read "Later"', done="Read Later/Done", failed="Read Later/Failed")))
    gmail.list_lines = [LIST_LINES[1]]
    src = source(cfg)
    src.open(create=True)
    assert [c[1] for c in cmds("create")] == ['"Read \\"Later\\""', '"Read Later/Done"', '"Read Later/Failed"']
    src.pending('from:"a b" \\ x')
    (_, raw_q), = [a for a in uid_cmds("SEARCH") if a[0] == "X-GM-RAW"]
    assert raw_q == ('"label:read-later -label:read-later-done -label:read-later-failed '
                     'from:\\"a b\\" \\\\ x"')


def test_pending_non_ascii_search_goes_as_a_literal(pinned_config, gmail):
    src = source(pinned_config)
    src.open()
    src.pending("subject:café")
    search = [c for c in gmail.instances[0].cmds if c[:3] == ("uid", "SEARCH", "X-GM-RAW")]
    assert search and len(search[0]) == 3  # no inline argument; the text rode in conn.literal


# --- fetch and mark ---------------------------------------------------------------------------------

def test_msgid_round_trip_fetches_without_marking_read(pinned_config, gmail):
    msg(7, "1500000000000000001", ["Newsletter"], raw=b"Subject: x\r\n\r\nhello")
    msg(8, "1500000000000000002", ["Newsletter"], raw=b"Subject: y\r\n\r\nworld")
    gmail.raw_hits = [7, 8]
    src = source(pinned_config)
    src.open()
    ids = [p.id for p in src.pending()]
    assert ids == ["1500000000000000001", "1500000000000000002"]
    assert src.fetch_raw(ids[1]) == b"Subject: y\r\n\r\nworld"
    assert ("8", "(BODY.PEEK[])") in uid_cmds("FETCH")  # PEEK: stays unread
    assert ("X-GM-MSGID", "1500000000000000002") in uid_cmds("SEARCH")
    assert ("select", f'"{ALL_MAIL}"') in [c for c in gmail.instances[0].cmds]


def test_fetch_raw_unknown_id_and_bad_id(pinned_config, gmail):
    src = source(pinned_config)
    src.open()
    with pytest.raises(RuntimeError, match="not found"):
        src.fetch_raw("999")
    with pytest.raises(ValueError):
        src.fetch_raw('1 X-GM-RAW "x"')  # an id is digits only; nothing else reaches the command


def test_mark_done_archives_and_marks_read(pinned_config, gmail):
    msg(7, "42", ["Newsletter", "\\Inbox"])
    src = source(pinned_config, archive=True)
    src.open()
    src.mark_done("42")
    assert uid_cmds("STORE") == [("7", "+X-GM-LABELS", '("Newsletter/Delivered")'),
                                 ("7", "+FLAGS", "(\\Seen)"),
                                 ("7", "-X-GM-LABELS", "(\\Inbox)")]


def test_mark_done_without_archive_keeps_it_in_the_inbox_but_still_marks_read(pinned_config, gmail):
    msg(7, "42", ["Newsletter", "\\Inbox"])
    src = source(pinned_config, archive=False)
    src.open()
    src.mark_done("42")
    assert uid_cmds("STORE") == [("7", "+X-GM-LABELS", '("Newsletter/Delivered")'),
                                 ("7", "+FLAGS", "(\\Seen)")]


def test_mark_failed_only_adds_the_failed_label(pinned_config, gmail):
    msg(7, "42", ["Newsletter", "\\Inbox"])
    src = source(pinned_config)
    src.open()
    src.mark_failed("42")
    assert uid_cmds("STORE") == [("7", "+X-GM-LABELS", '("Newsletter/Failed")')]


def test_label_names_with_spaces_slashes_and_quotes_are_quoted_in_store(pinned_config, gmail):
    cfg = replace(pinned_config, gmail=replace(pinned_config.gmail, labels=replace(
        pinned_config.gmail.labels, done='Read "It"/Done \\ ok')))
    msg(7, "42", ["Newsletter"])
    src = source(cfg)
    src.open()
    src.mark_done("42")
    assert uid_cmds("STORE")[0] == ("7", "+X-GM-LABELS", '("Read \\"It\\"/Done \\\\ ok")')


def test_label_with_line_break_is_refused(pinned_config, gmail):
    with pytest.raises(ValueError):
        _quote("a\r\nb")


# --- reconnect --------------------------------------------------------------------------------------

def test_a_dropped_connection_is_reopened_once_and_the_command_retried(pinned_config, gmail):
    msg(7, "42", ["Newsletter"])
    src = source(pinned_config)
    src.open()
    gmail.drop_on = (0, "uid")  # the next UID command dies on the first connection
    src.mark_failed("42")
    assert len(gmail.instances) == 2
    second = [c[0] for c in gmail.instances[1].cmds]
    assert second[:2] == ["login", "select"]  # fresh login, All Mail re-selected
    assert uid_cmds("STORE") == [("7", "+X-GM-LABELS", '("Newsletter/Failed")')]


def test_a_second_drop_in_the_same_call_gives_up_clearly(pinned_config, gmail, monkeypatch):
    src = source(pinned_config)
    src.open()

    def always_drop(self, *a, **k):
        raise imaplib.IMAP4.abort("socket error: EOF")
    monkeypatch.setattr(FakeGmail, "uid", always_drop)
    with pytest.raises(RuntimeError, match="lost the connection"):
        src.fetch_raw("42")
    assert len(gmail.instances) == 2  # exactly one reconnect


# --- helpers ----------------------------------------------------------------------------------------

def test_modified_utf7_round_trip():
    for name in ("Newsletter", "Café/Lettres", "A & B", "日本語"):
        assert _unutf7(_utf7(name)) == name
    assert _utf7("A & B") == "A &- B"
    assert _utf7("Café") == "Caf&AOk-"


def test_label_list_parsing_handles_quotes_spaces_and_system_labels():
    text = r'1 (X-GM-LABELS ("\\Inbox" "Read \"Later\"" Plain "Has ) paren") UID 1)'
    assert _label_list(text, text.index("(", 3)) == ["\\Inbox", 'Read "Later"', "Plain", "Has ) paren"]


def test_imap_module_exposes_the_documented_endpoint():
    assert (imap_mod.HOST, imap_mod.PORT) == ("imap.gmail.com", 993)
