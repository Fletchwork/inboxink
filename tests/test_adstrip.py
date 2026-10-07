from types import SimpleNamespace

from bs4 import BeautifulSoup

from inboxink import adstrip
from inboxink.adstrip import parse_ids


def test_parse_ids_tolerates_fences_quotes_and_prose():
    assert parse_ids("```json\n[1, 13, 28]\n```") == [1, 13, 28]
    assert parse_ids('Here you go: ["3", "4"]') == [3, 4]
    assert parse_ids("[]") == []
    assert parse_ids("None.") == []
    assert parse_ids("Blocks [a] are ads") is None
    assert parse_ids("I think blocks 3 and 4 are ads") is None


def test_unusable_answer_is_logged_without_claudes_reply(monkeypatch, capsys):
    secret = "QUOTED NEWSLETTER SENTENCE"
    monkeypatch.setattr(adstrip.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout=f"I cannot do that: {secret}", stderr=secret, returncode=0))
    soup = BeautifulSoup("<p>Hello world</p><p>Buy now</p>", "html.parser")
    assert adstrip.strip_ads(soup, "Title", "Pub") == []
    err = capsys.readouterr().err
    assert secret not in err
    assert "Claude gave no usable answer, so no ads were removed" in err and "running 'claude' once" in err
