"""Tests for credential redaction and stale-error reporting (AUDIT_PLAN items 33 and 34).

Run from the repo root:  python3 -m pytest tests/ -q
"""

import watchdog_mailgun as wd
import WU_upload


SECRET = "hunter2SuperSecret"


class TestRedaction:
    """The WU realtime protocol requires the password as a query parameter, and
    requests/urllib3 exception messages routinely embed the full request URL."""

    def test_redacts_password_in_url(self):
        url = f"https://rtupdate.wunderground.com/x.php?ID=KXX1&PASSWORD={SECRET}&tempf=70"
        out = WU_upload.redact(url)
        assert SECRET not in out
        assert "PASSWORD=<redacted>" in out
        assert "tempf=70" in out, "must not swallow the rest of the query string"

    def test_redacts_when_password_is_last_parameter(self):
        out = WU_upload.redact(f"failed: http://h/x?ID=K&PASSWORD={SECRET}")
        assert SECRET not in out

    def test_case_insensitive(self):
        assert SECRET not in WU_upload.redact(f"?password={SECRET}&a=1")
        assert SECRET not in WU_upload.redact(f"?PassWord={SECRET}&a=1")

    def test_redacts_inside_exception_object(self):
        exc = ValueError(f"tunnel failed for url: https://h/x?PASSWORD={SECRET}&a=1")
        assert SECRET not in WU_upload.redact(exc)

    def test_redacts_quoted_url(self):
        out = WU_upload.redact(f"HTTPSConnectionPool(url='/x?PASSWORD={SECRET}&a=1')")
        assert SECRET not in out

    def test_passthrough_when_no_password(self):
        assert WU_upload.redact("ConnectionError") == "ConnectionError"

    def test_watchdog_redacts_independently(self):
        """Defence in depth: the status file is untrusted input to the alert path."""
        assert SECRET not in wd.redact(f"Unexpected error: ?PASSWORD={SECRET}")


class TestDescribeLastError:
    def test_no_error_recorded(self):
        assert wd.describe_last_error({}, 1000.0) == "none recorded"

    def test_recent_error_is_reported_with_age(self):
        status = {"last_upload_error": "ConnectionError", "last_upload_error_at": 940.0}
        out = wd.describe_last_error(status, 1000.0)
        assert "ConnectionError" in out and "60s ago" in out

    def test_stale_error_is_demoted(self):
        """Observed live: an alert would have reported a 15.5-hour-old ConnectionError
        as though it described the current failure."""
        status = {"last_upload_error": "ConnectionError", "last_upload_error_at": 0.0}
        out = wd.describe_last_error(status, 60000.0)
        assert out.startswith("none recent")
        assert "ConnectionError" in out, "keep it visible, just clearly labelled as old"

    def test_missing_timestamp_is_flagged_not_assumed_fresh(self):
        out = wd.describe_last_error({"last_upload_error": "Timeout"}, 1000.0)
        assert "age unknown" in out

    def test_error_text_is_redacted_before_reaching_email(self):
        status = {
            "last_upload_error": f"Unexpected error: https://h/x?PASSWORD={SECRET}",
            "last_upload_error_at": 990.0,
        }
        assert SECRET not in wd.describe_last_error(status, 1000.0)
