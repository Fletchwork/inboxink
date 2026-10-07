import argparse, os, stat

from inboxink import run


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_state_file_is_private(pinned_config):
    state = run._defaults({})
    run.save_state(state)
    assert mode(pinned_config.paths.state_file) == 0o600
    assert not os.path.exists(pinned_config.paths.state_file + ".tmp")


def test_state_file_tightens_a_leftover_wide_tmp(pinned_config):
    path = pinned_config.paths.state_file
    os.makedirs(os.path.dirname(path))
    with open(path + ".tmp", "w") as f:
        f.write("{}")
    os.chmod(path + ".tmp", 0o644)
    run.save_state(run._defaults({}))
    assert mode(path) == 0o600


def test_lock_file_is_private(pinned_config, monkeypatch):
    def boom(cfg):
        raise RuntimeError("no mail in tests")
    monkeypatch.setattr(run, "get_source", boom)
    assert run.execute(argparse.Namespace(dry_run=True, query="", limit=1)) == 1
    assert mode(pinned_config.paths.state_file + ".lock") == 0o600


def test_log_over_the_limit_is_cut_to_its_tail(tmp_path):
    log = tmp_path / "inboxink.log"
    lines = [f"2026-10-06 10:00:{i % 60:02d} line {i}\n" for i in range(400)]
    log.write_text("".join(lines))
    size = log.stat().st_size
    run.trim_log(str(log), limit=size - 1, keep=1000)
    out = log.read_text()
    assert len(out) <= 1000 and out.endswith(lines[-1])
    assert out.startswith("2026-")  # starts on a whole line, not mid-line
    assert mode(str(log)) == 0o600 and not (tmp_path / "inboxink.log.tmp").exists()


def test_log_under_the_limit_is_left_alone(tmp_path):
    log = tmp_path / "inboxink.log"
    log.write_text("one line\n")
    before = log.stat().st_mtime_ns
    run.trim_log(str(log), limit=100, keep=10)
    run.trim_log(str(tmp_path / "missing.log"))  # no file: no error
    assert log.read_text() == "one line\n" and log.stat().st_mtime_ns == before


def test_defaults_are_one_megabyte_and_two_hundred_kilobytes():
    assert run.LOG_MAX == 1_000_000 and run.LOG_KEEP == 200_000


def test_real_run_trims_the_log_at_the_end(pinned_config, monkeypatch):
    log = pinned_config.paths.log
    with open(log, "w") as f:
        f.write("old line\n" * 200_000)
    monkeypatch.setattr(run, "get_source", lambda cfg: (_ for _ in ()).throw(RuntimeError("no mail")))
    # dry runs never touch the log; a real run needs an Instapaper login, so stub that check
    monkeypatch.setattr(run.instapaper, "check_login", lambda: None)
    run.execute(argparse.Namespace(dry_run=True, query="", limit=1))
    assert os.path.getsize(log) > run.LOG_MAX
    run.execute(argparse.Namespace(dry_run=False, query="", limit=1))
    assert os.path.getsize(log) <= run.LOG_KEEP
