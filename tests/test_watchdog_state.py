"""Tests for watchdog state persistence (AUDIT_PLAN items 3 and 31).

Run from the repo root:  python3 -m pytest tests/ -q

These import watchdog_mailgun with WU_credentials and requests stubbed by conftest.py,
so they run on any machine. Everything under test here is pure filesystem work.
"""

import json

import pytest

import watchdog_mailgun as wd


def test_creates_missing_directories(tmp_path):
    target = tmp_path / "Logs" / "state.json"
    wd._atomic_write_json(str(target), {"consecutive_failures": 3})
    assert json.loads(target.read_text()) == {"consecutive_failures": 3}


def test_repeated_writes_leave_no_temp_files(tmp_path):
    target = tmp_path / "state.json"
    for i in range(5):
        wd._atomic_write_json(str(target), {"consecutive_failures": i})
    assert json.loads(target.read_text())["consecutive_failures"] == 4
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_failed_write_preserves_previous_state(tmp_path):
    """The reason item 31 could silently disable the watchdog: the old implementation
    used open(path, "w"), which truncates before serialising. A payload that fails to
    encode left an empty state file and reset consecutive_failures to nothing."""
    target = tmp_path / "state.json"
    wd._atomic_write_json(str(target), {"consecutive_failures": 2})

    with pytest.raises(TypeError):
        wd._atomic_write_json(str(target), {"unserialisable": object()})

    assert json.loads(target.read_text())["consecutive_failures"] == 2
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


def test_load_json_missing_file_returns_default(tmp_path):
    assert wd.load_json(str(tmp_path / "absent.json"), {"x": 1}) == {"x": 1}


def test_write_sets_readable_mode_not_mkstemp_default(tmp_path):
    """mkstemp() creates 0600 and os.replace preserves it, which silently made these
    status files unreadable to anything but their owner. They exist to be observed."""
    target = tmp_path / "state.json"
    wd._atomic_write_json(str(target), {"a": 1})
    assert oct(target.stat().st_mode & 0o777) == "0o644"

    wd._atomic_write_json(str(target), {"a": 2}, mode=0o600)
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_load_json_malformed_file_returns_default(tmp_path):
    corrupt = tmp_path / "state.json"
    corrupt.write_text("{not json")
    # Must fall back rather than raise: evaluate_health() then reports missing
    # timestamps, which escalates. Silently succeeding here would blind the watchdog.
    assert wd.load_json(str(corrupt), {"x": 1}) == {"x": 1}


def test_persist_state_reports_failure_without_raising(tmp_path, monkeypatch):
    """A watchdog that cannot persist its own counter must say so and keep going,
    not abort main() before the increment lands."""
    sent = []
    monkeypatch.setattr(wd, "send_mailgun_email", lambda s, b: sent.append(s))
    monkeypatch.setattr(wd, "WATCHDOG_STATE_FILE", str(tmp_path / "state.json"))

    assert wd.persist_state({"consecutive_failures": 1}, "testhost") is True
    assert sent == []

    monkeypatch.setattr(wd, "WATCHDOG_STATE_FILE", str(tmp_path / "nodir" / "x.json"))
    monkeypatch.setattr(wd.os, "makedirs", _raise_oserror)
    assert wd.persist_state({"consecutive_failures": 2}, "testhost") is False
    assert len(sent) == 1


def _raise_oserror(*_args, **_kwargs):
    raise OSError("read-only file system")
