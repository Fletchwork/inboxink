from dataclasses import replace

import pytest

from inboxink import config
from inboxink.sources import Pending, get_source
from inboxink.sources.gog import GogSource
from inboxink.sources.imap import ImapSource


def fake_gog(calls, threads):
    labels = [{"name": n, "id": f"L-{i}"} for i, n in enumerate(["Newsletter", "Newsletter/Delivered", "Newsletter/Failed"])]

    def _gog(self, *args):
        calls.append(args)
        if args[:3] == ("gmail", "labels", "list"):
            return labels
        if args[:2] == ("gmail", "search"):
            return [{"id": t} for t in threads]
        if args[:3] == ("gmail", "thread", "get"):
            return {"messages": threads[args[3]]}
        return None
    return _gog


def test_get_source_picks_by_kind(pinned_config):
    assert isinstance(get_source(pinned_config), GogSource)
    imap_cfg = replace(pinned_config, source=replace(pinned_config.source, kind="imap"))
    assert isinstance(get_source(imap_cfg), ImapSource)


def test_imap_open_without_app_password_points_at_setup(pinned_config, monkeypatch):
    monkeypatch.setattr("inboxink.credentials.get_secret", lambda name: None)
    with pytest.raises(RuntimeError, match="inboxink setup"):
        ImapSource(pinned_config).open()


def test_gog_pending_filters_and_sorts(pinned_config, monkeypatch):
    calls = []
    threads = {"t1": [{"id": "new", "internalDate": "2000", "labelIds": ["L-0"]},
                      {"id": "old", "internalDate": "1000", "labelIds": ["L-0"]},
                      {"id": "done", "internalDate": "500", "labelIds": ["L-0", "L-1"]},
                      {"id": "other", "internalDate": "400", "labelIds": ["INBOX"]}]}
    monkeypatch.setattr(GogSource, "_gog", fake_gog(calls, threads))
    src = GogSource(pinned_config)
    src.open(create=False)
    assert src.pending("newer_than:1d", 5) == [Pending("old", 1000), Pending("new", 2000)]
    search = next(c for c in calls if c[:2] == ("gmail", "search"))
    assert search[2] == "label:newsletter -label:newsletter-delivered -label:newsletter-failed newer_than:1d"


def test_gog_inbox_mode_and_archive_flag(pinned_config, monkeypatch):
    calls = []
    cfg = replace(pinned_config, source=replace(pinned_config.source, newsletters_only=True),
                  gmail=replace(pinned_config.gmail, archive_on_done=False))
    threads = {"t1": [{"id": "a", "internalDate": "1", "labelIds": ["INBOX"]},
                      {"id": "b", "internalDate": "2", "labelIds": ["L-0"]}]}
    monkeypatch.setattr(GogSource, "_gog", fake_gog(calls, threads))
    src = GogSource(cfg)
    src.open(create=False)
    assert [m.id for m in src.pending()] == ["a"]
    assert next(c for c in calls if c[:2] == ("gmail", "search"))[2].startswith("in:inbox ")
    src.mark_done("a")
    assert calls[-1][-2:] == ("--remove", "UNREAD")  # kept in the inbox, only marked read


def test_gog_open_without_create_reports_missing_label(pinned_config, monkeypatch):
    monkeypatch.setattr(GogSource, "_gog", lambda self, *a: [{"name": "Newsletter", "id": "x"}])
    with pytest.raises(RuntimeError, match="Newsletter/Delivered' does not exist yet. 'inboxink run' creates it"):
        GogSource(pinned_config).open(create=False)
