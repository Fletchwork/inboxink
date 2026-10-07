import os

from inboxink import cover


def test_icon_cache_keeps_only_the_newest_files(tmp_path):
    tmp_path = tmp_path / "icons"
    tmp_path.mkdir()
    for i in range(12):
        f = tmp_path / f"host{i}.png"
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
    cover._prune_cache(str(tmp_path), keep=5)
    assert set(os.listdir(tmp_path)) == {f"host{i}.png" for i in range(7, 12)}


def test_icon_cache_limit_is_two_hundred():
    assert cover.MAX_CACHED_ICONS == 200


def _png(size=(128, 128), color=(0, 128, 0, 255)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGBA", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_icon_falls_back_to_google_when_the_site_blocks_bots(monkeypatch):
    asked = []

    def fake_get(url, limit=3_000_000):
        asked.append(url)
        if url.startswith(cover.GOOGLE_FAVICONS):
            return _png(), url
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(cover, "_get", fake_get)
    icon = cover.site_icon("blocked.example")
    assert icon is not None and icon.size == (128, 128)
    assert asked[-1] == cover.GOOGLE_FAVICONS + "blocked.example"  # tried only after the site's own icons


def test_cover_without_lead_or_icon_shows_initials(monkeypatch):
    import io
    from PIL import Image
    monkeypatch.setattr(cover, "site_icon", lambda site: None)
    tile = Image.open(io.BytesIO(cover.make_cover(None, "blocked.example", "Example Insights"))).convert("L")
    side = int(cover.SIZE * cover.BADGE)
    top = cover.SIZE // 8
    badge = tile.crop(((cover.SIZE - side) // 2, top, (cover.SIZE + side) // 2, top + side))
    assert badge.getextrema()[0] < 100  # dark initials drawn where the icon would be
