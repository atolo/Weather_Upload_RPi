"""Tests for local daily-rain persistence (AUDIT_PLAN item 37).

Run from the repo root:  python3 -m pytest tests/ -q

`import Weather_Station` cannot work anywhere but the Pi -- module scope opens /dev/serial0
and the BME280 over I2C (architectural issue A4). Rather than leave brand-new persistence
logic with no coverage at all, this loads the functions under test out of the real source
file and executes them against stub globals. It therefore tests the shipped code, not a copy.

**This is a stopgap.** When A4 is fixed and Weather_Station.py imports cleanly, delete
load_functions_from_source() and import the module the normal way.
"""

import ast
import json
import os
import sys
import time
import types

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import pytest

SOURCE_FILE = os.path.join(REPO_ROOT, "Weather_Station.py")
FUNCTIONS = ("_atomic_write_json", "save_rain_state", "load_rain_state")


def load_functions_from_source(names, namespace):
    """Exec the named top-level functions from Weather_Station.py into `namespace`."""
    with open(SOURCE_FILE, "r") as source_file:
        tree = ast.parse(source_file.read())

    found = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            exec(compile(ast.Module([node], []), SOURCE_FILE, "exec"), namespace)
            found.add(node.name)

    missing = set(names) - found
    assert not missing, f"{SOURCE_FILE} no longer defines {sorted(missing)}"
    return namespace


@pytest.fixture
def rain(tmp_path):
    """The rain-state functions plus the globals they close over."""
    namespace = {
        "os": os,
        "json": json,
        "time": time,
        "tempfile": __import__("tempfile"),
        "RAIN_STATE_FILE": str(tmp_path / "rain_state.json"),
        "MAX_PLAUSIBLE_DAILY_RAIN_IN": 10.0,
        "suntec": types.SimpleNamespace(rainToday=0.0),
    }
    return load_functions_from_source(FUNCTIONS, namespace)


def today():
    return time.strftime("%y%m%d")


class TestRoundTrip:
    def test_no_state_file_starts_at_zero(self, rain):
        assert rain["load_rain_state"]() == 0.0

    def test_saved_total_is_restored(self, rain):
        rain["suntec"].rainToday = 0.37
        rain["save_rain_state"]()
        assert rain["load_rain_state"]() == 0.37

    def test_stored_shape_is_date_plus_total(self, rain):
        rain["suntec"].rainToday = 0.05
        rain["save_rain_state"]()
        with open(rain["RAIN_STATE_FILE"]) as handle:
            assert json.load(handle) == {"date": today(), "rain_today": 0.05}

    def test_state_file_is_world_readable(self, rain):
        """Logs/ is readable by the read-only diagnostic account; 0600 would break that
        and did once already (commit 942f5c2)."""
        rain["suntec"].rainToday = 0.01
        rain["save_rain_state"]()
        assert oct(os.stat(rain["RAIN_STATE_FILE"]).st_mode)[-3:] == "644"


class TestRejectsUnusableState:
    """Every rejection path must land on 0.0 -- the same place a fresh install starts."""

    def _write(self, rain, payload):
        with open(rain["RAIN_STATE_FILE"], "w") as handle:
            if isinstance(payload, str):
                handle.write(payload)
            else:
                json.dump(payload, handle)

    def test_yesterdays_total_does_not_carry_over(self, rain):
        self._write(rain, {"date": "000101", "rain_today": 0.5})
        assert rain["load_rain_state"]() == 0.0

    def test_implausibly_large_total_is_ignored(self, rain):
        self._write(rain, {"date": today(), "rain_today": 99.0})
        assert rain["load_rain_state"]() == 0.0

    def test_negative_total_is_ignored(self, rain):
        self._write(rain, {"date": today(), "rain_today": -1.0})
        assert rain["load_rain_state"]() == 0.0

    def test_non_numeric_total_is_ignored(self, rain):
        self._write(rain, {"date": today(), "rain_today": "wet"})
        assert rain["load_rain_state"]() == 0.0

    def test_missing_total_is_ignored(self, rain):
        self._write(rain, {"date": today()})
        assert rain["load_rain_state"]() == 0.0

    def test_corrupt_json_is_ignored(self, rain):
        self._write(rain, "{not json")
        assert rain["load_rain_state"]() == 0.0


class TestFailureIsContained:
    def test_unwritable_path_does_not_raise(self, rain):
        """save_rain_state() is called from inside packet decoding. An exception there
        would be caught by the main loop's broad handler and discard the packet."""
        rain["RAIN_STATE_FILE"] = "/proc/definitely-not-writable/rain_state.json"
        rain["save_rain_state"]()  # must not raise
