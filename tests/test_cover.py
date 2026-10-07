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
