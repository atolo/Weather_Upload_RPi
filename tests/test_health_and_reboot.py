"""Tests for watchdog health evaluation and reboot policy (AUDIT_PLAN item 1).

Run from the repo root:  python3 -m pytest tests/ -q
"""

import watchdog_mailgun as wd

NOW = 1_000_000.0


class TestHeartbeat:
    def test_fresh_heartbeat_is_healthy(self):
        status = {"last_heartbeat": NOW - 10, "last_successful_upload": NOW - 10}
        assert wd.evaluate_health(status, NOW) == []

    def test_stale_heartbeat_is_flagged(self):
        status = {"last_heartbeat": NOW - 5000, "last_successful_upload": NOW - 10}
        issues = wd.evaluate_health(status, NOW)
        assert any("heartbeat stale" in i for i in issues)

    def test_missing_heartbeat_is_flagged(self):
        """A torn or empty status file must escalate, not be silently tolerated."""
        assert any("missing heartbeat" in i for i in wd.evaluate_health({}, NOW))


class TestNeverUploaded:
    """Item 1: the uploader used to write a fake last_successful_upload at startup so this
    case never arose. Now that it does not, a station that has genuinely never uploaded
    must neither look instantly broken nor be ignored forever."""

    def test_no_upload_and_never_seen_before_is_not_yet_an_issue(self):
        status = {"last_heartbeat": NOW}
        assert wd.evaluate_health(status, NOW, no_upload_since=None) == []

    def test_no_upload_within_grace_is_not_an_issue(self):
        status = {"last_heartbeat": NOW}
        seen_at = NOW - (wd.STALE_UPLOAD_SECONDS - 60)
        assert wd.evaluate_health(status, NOW, no_upload_since=seen_at) == []

    def test_no_upload_beyond_grace_is_flagged(self):
        status = {"last_heartbeat": NOW}
        seen_at = NOW - (wd.STALE_UPLOAD_SECONDS + 60)
        issues = wd.evaluate_health(status, NOW, no_upload_since=seen_at)
        assert any("no successful upload ever" in i for i in issues)

    def test_a_real_upload_takes_precedence_over_never_uploaded(self):
        status = {"last_heartbeat": NOW, "last_successful_upload": NOW - 5}
        stale = NOW - 99999
        assert wd.evaluate_health(status, NOW, no_upload_since=stale) == []


class TestUploadStaleness:
    def test_recent_upload_is_healthy(self):
        status = {"last_heartbeat": NOW, "last_successful_upload": NOW - 10}
        assert wd.evaluate_health(status, NOW) == []

    def test_stale_upload_is_flagged(self):
        status = {
            "last_heartbeat": NOW,
            "last_successful_upload": NOW - (wd.STALE_UPLOAD_SECONDS + 1),
        }
        assert any("upload stale" in i for i in wd.evaluate_health(status, NOW))


class TestClockSteps:
    """Item 27: the uploader's timestamps are wall-clock because they cross a process boundary,
    and the Pi has no RTC. An NTP step must not be mistaken for evidence about the station."""

    UPTIME = 86_400.0  # up one day, so nothing legitimately predates this boot

    def test_backward_step_makes_timestamps_look_future_and_is_tolerated(self):
        """Clock stepped back an hour after the uploader wrote a fresh heartbeat."""
        status = {"last_heartbeat": NOW + 3000, "last_successful_upload": NOW + 3000}
        assert wd.evaluate_health(status, NOW, uptime=self.UPTIME) == []

    def test_absurdly_future_timestamp_is_reported_not_ignored(self):
        """Beyond plausible skew, silence would blind the watchdog indefinitely."""
        status = {"last_heartbeat": NOW + wd.MAX_FUTURE_SECONDS + 60}
        assert any("in the future" in i for i in wd.evaluate_health(status, NOW, uptime=self.UPTIME))

    def test_small_skew_within_tolerance_still_reads_as_healthy(self):
        status = {"last_heartbeat": NOW + 30, "last_successful_upload": NOW + 30}
        assert wd.evaluate_health(status, NOW, uptime=self.UPTIME) == []

    def test_heartbeat_older_than_uptime_is_still_an_issue(self):
        """The important non-suppression: reboot, uploader fails to start, heartbeat is
        frozen before boot forever. Ignoring this case would hide a dead station."""
        status = {"last_heartbeat": NOW - 5000, "last_successful_upload": NOW}
        issues = wd.evaluate_health(status, NOW, uptime=600.0)
        assert any("predates this boot" in i for i in issues)

    def test_stale_within_uptime_reads_as_ordinary_staleness(self):
        status = {"last_heartbeat": NOW - 5000, "last_successful_upload": NOW}
        issues = wd.evaluate_health(status, NOW, uptime=self.UPTIME)
        assert any("heartbeat stale" in i for i in issues)

    def test_unknown_uptime_never_claims_a_boot_relationship(self):
        """get_uptime_seconds() fails open with a huge value; None must behave the same way."""
        status = {"last_heartbeat": NOW - 5000, "last_successful_upload": NOW}
        issues = wd.evaluate_health(status, NOW, uptime=None)
        assert any("heartbeat stale" in i for i in issues)
        assert not any("predates" in i for i in issues)

    def test_stale_upload_survives_the_clock_guard(self):
        status = {
            "last_heartbeat": NOW,
            "last_successful_upload": NOW - (wd.STALE_UPLOAD_SECONDS + 1),
        }
        assert any("upload stale" in i for i in wd.evaluate_health(status, NOW, uptime=self.UPTIME))


class TestRebootCooldown:
    """Item 1: the old reboot_triggered boolean was reset by any single clean pass. Because
    startup wrote a fake upload timestamp, every boot produced exactly one clean pass, which
    cleared the latch and allowed the next reboot ~15-30 minutes later, indefinitely."""

    @staticmethod
    def _may_reboot(state, now):
        last_reboot_at = float(state.get("last_reboot_at", 0) or 0)
        return (
            state["consecutive_failures"] >= wd.MAX_FAILURES_BEFORE_REBOOT
            and (now - last_reboot_at) > wd.MIN_SECONDS_BETWEEN_REBOOTS
        )

    def test_reboots_when_threshold_reached_and_never_rebooted(self):
        assert self._may_reboot({"consecutive_failures": 3, "last_reboot_at": 0}, NOW)

    def test_does_not_reboot_below_threshold(self):
        assert not self._may_reboot({"consecutive_failures": 2, "last_reboot_at": 0}, NOW)

    def test_suppressed_inside_cooldown_even_after_a_clean_pass(self):
        recent = NOW - (wd.MIN_SECONDS_BETWEEN_REBOOTS - 60)
        assert not self._may_reboot({"consecutive_failures": 9, "last_reboot_at": recent}, NOW)

    def test_allowed_once_cooldown_expires(self):
        old = NOW - (wd.MIN_SECONDS_BETWEEN_REBOOTS + 60)
        assert self._may_reboot({"consecutive_failures": 3, "last_reboot_at": old}, NOW)

    def test_default_cooldown_is_long_enough_to_break_a_loop(self):
        """The observed loop period was 15-30 minutes; the cooldown must dwarf it."""
        assert wd.MIN_SECONDS_BETWEEN_REBOOTS >= 3600
