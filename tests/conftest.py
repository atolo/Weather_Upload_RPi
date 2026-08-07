"""Shared test setup: make the repo importable and stub the things that only exist on the Pi.

`WU_credentials` is gitignored and present only on the station, and nothing here should be
making real HTTP requests, so both are replaced with stubs before any test module imports the
code under test. `weatherData_cls` is deliberately NOT stubbed -- it is pure Python and imports
anywhere, and stubbing it would shadow the real class from its own tests.

Doing this here rather than in each test file means the stubs are defined once and stay
consistent -- a stub missing an attribute that a module touches at import time then shows up as
a collection error rather than a confusing test failure.
"""

import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _StubSession:
    """WU_upload creates a module-level requests.Session() at import (item 7)."""

    def get(self, *args, **kwargs):
        raise AssertionError("tests must not make real HTTP requests")


def _stub_exceptions():
    """The names WU_upload catches by hand. Real classes so error paths stay testable."""
    module = types.ModuleType("requests.exceptions")
    for name in ("ConnectionError", "HTTPError", "ConnectTimeout",
                 "ReadTimeout", "RetryError", "Timeout"):
        setattr(module, name, type(name, (Exception,), {}))
    return module


def _install_stub(name, **attributes):
    module = sys.modules.setdefault(name, types.ModuleType(name))
    for attribute, value in attributes.items():
        if not hasattr(module, attribute):
            setattr(module, attribute, value)
    return module


_install_stub("WU_credentials",
               WU_PASSWORD="test-pws-key",
               WU_STATION_ID_SUNTEC="KTEST1",
               WU_API_KEY="test-api-key")
_install_stub("requests", Session=_StubSession, exceptions=_stub_exceptions())
