"""Tests for connectivity classification and the escalation ladder (AUDIT_PLAN items 2 and 39).

Run from the repo root:  python3 -m pytest tests/ -q
"""

import pytest

import watchdog_mailgun as wd

NOW = 1_000_000.0


class TestIsUploadOnly:
    """The distinction the watchdog could not previously make: is the program stuck, or is
    the network down? Only the first justifies doing something destructive."""

    def test_upload_staleness_alone_is_upload_only(self):
        assert wd.is_upload_only(["upload stale for 1000s"])

    def test_never_uploaded_is_upload_only(self):
        assert wd.is_upload_only(["no successful upload ever; first noticed 2000s ago"])

    def test_stale_heartbeat_is_not(self):
        assert not wd.is_upload_only(["heartbeat stale for 400s"])

    def test_missing_heartbeat_is_not(self):
        assert not wd.is_upload_only(["missing heartbeat timestamp"])

    def test_mixed_issues_are_not(self):
        assert not wd.is_upload_only(["upload stale for 1000s", "heartbeat stale for 400s"])

    def test_no_issues_is_not_upload_only(self):
        assert not wd.is_upload_only([])

    def test_every_message_evaluate_health_can_emit_is_classified(self):
        """Guards the prefix matching against a future reword of an issue string."""
        program = wd.evaluate_health({}, NOW)
        assert not wd.is_upload_only(program)

        upload = wd.evaluate_health(
            {"last_heartbeat": NOW, "last_successful_upload": NOW - 99999}, NOW)
        assert wd.is_upload_only(upload)

        never = wd.evaluate_health(
            {"last_heartbeat": NOW}, NOW, no_upload_since=NOW - 99999)
        assert wd.is_upload_only(never)

        predates = wd.evaluate_health(
            {"last_heartbeat": NOW, "last_successful_upload": NOW - 5000}, NOW, uptime=600.0)
        assert wd.is_upload_only(predates), "an upload issue must stay classified as one"


class TestClassifyConnectivity:
    """Probe order matters: the neutral probe is an IP literal, so 'no route' is separable
    from 'DNS is broken'. A hostname-only probe conflates them."""

    @staticmethod
    def _patch(monkeypatch, wu_tcp, neutral_tcp, dns):
        def tcp(host, port=443, timeout=None):
            return neutral_tcp if host == wd.NEUTRAL_PROBE_IP else wu_tcp
        monkeypatch.setattr(wd, "tcp_reachable", tcp)
        monkeypatch.setattr(wd, "resolves", lambda host: dns)

    def test_wu_reachable(self, monkeypatch):
        self._patch(monkeypatch, wu_tcp=True, neutral_tcp=True, dns=True)
        assert wd.classify_connectivity() == "wu_reachable"

    def test_no_route_at_all(self, monkeypatch):
        self._patch(monkeypatch, wu_tcp=False, neutral_tcp=False, dns=False)
        assert wd.classify_connectivity() == "internet_unreachable"

    def test_routing_works_but_dns_does_not(self, monkeypatch):
        self._patch(monkeypatch, wu_tcp=False, neutral_tcp=True, dns=False)
        assert wd.classify_connectivity() == "dns_unavailable"

    def test_wu_specifically_down(self, monkeypatch):
        self._patch(monkeypatch, wu_tcp=False, neutral_tcp=True, dns=True)
        assert wd.classify_connectivity() == "wu_unreachable"

    def test_wu_reachable_wins_even_if_the_neutral_probe_is_blocked(self, monkeypatch):
        """Some networks block 1.1.1.1 while normal HTTPS works. If the host we actually
        need answers, nothing else matters."""
        self._patch(monkeypatch, wu_tcp=True, neutral_tcp=False, dns=True)
        assert wd.classify_connectivity() == "wu_reachable"


class TestDecideAction:
    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.setattr(wd, "REBOOT_ENABLED", True)
        monkeypatch.setattr(wd, "REBOOT_ON_NO_INTERNET", False)

    def test_first_failure_does_nothing(self):
        state = {"consecutive_failures": 1, "incident_started_at": NOW - 300}
        assert wd.decide_action(state, NOW, "wu_reachable") == "none"

    def test_second_failure_restarts_the_service(self):
        state = {"consecutive_failures": 2, "incident_started_at": NOW - 600}
        assert wd.decide_action(state, NOW, "wu_reachable") == "restart"

    def test_reboot_is_not_reachable_before_a_restart(self):
        """Rebooting a Pi to fix a Python process is the last resort, not the first."""
        state = {"consecutive_failures": 99, "incident_started_at": NOW - 30000}
        assert wd.decide_action(state, NOW, "wu_reachable") != "reboot"

    def test_reboot_waits_for_the_restart_to_have_a_chance(self):
        state = {
            "consecutive_failures": 3,
            "incident_started_at": NOW - 900,
            "last_restart_at": NOW - (wd.RESTART_GRACE_SECONDS - 60),
        }
        assert wd.decide_action(state, NOW, "wu_reachable") == "none"

    def test_reboot_once_the_restart_has_demonstrably_not_worked(self):
        state = {
            "consecutive_failures": 3,
            "incident_started_at": NOW - 1800,
            "last_restart_at": NOW - (wd.RESTART_GRACE_SECONDS + 60),
        }
        assert wd.decide_action(state, NOW, "wu_reachable") == "reboot"

    def test_a_restart_from_a_previous_incident_does_not_license_a_reboot(self):
        """Without this the ladder skips its bottom rung on every incident after the first."""
        state = {
            "consecutive_failures": 3,
            "incident_started_at": NOW - 900,
            "last_restart_at": NOW - 86400,
        }
        assert wd.decide_action(state, NOW, "wu_reachable") == "restart"

    def test_reboot_cooldown_still_applies(self):
        state = {
            "consecutive_failures": 3,
            "incident_started_at": NOW - 1800,
            "last_restart_at": NOW - (wd.RESTART_GRACE_SECONDS + 60),
            "last_reboot_at": NOW - 60,
        }
        assert wd.decide_action(state, NOW, "wu_reachable") != "reboot"

    def test_restart_is_rate_limited(self):
        state = {
            "consecutive_failures": 5,
            "incident_started_at": NOW - 1500,
            "last_restart_at": NOW - 60,
        }
        assert wd.decide_action(state, NOW, "wu_reachable") == "none"

    def test_reboot_disabled_falls_back_to_restarting(self, monkeypatch):
        monkeypatch.setattr(wd, "REBOOT_ENABLED", False)
        state = {
            "consecutive_failures": 9,
            "incident_started_at": NOW - 9000,
            "last_restart_at": NOW - (wd.MIN_SECONDS_BETWEEN_RESTARTS + 60),
        }
        assert wd.decide_action(state, NOW, "wu_reachable") == "restart"


class TestNetworkFaultsSuppressAction:
    """Item 2's core claim: a healthy program behind a dead WAN is indistinguishable from a
    wedged one by upload age alone, and every corrective action makes the first case worse."""

    @pytest.fixture(autouse=True)
    def _defaults(self, monkeypatch):
        monkeypatch.setattr(wd, "REBOOT_ENABLED", True)
        monkeypatch.setattr(wd, "REBOOT_ON_NO_INTERNET", False)

    @staticmethod
    def _ripe_for_reboot():
        return {
            "consecutive_failures": 10,
            "incident_started_at": NOW - 3000,
            "last_restart_at": NOW - (wd.RESTART_GRACE_SECONDS + 600),
        }

    @pytest.mark.parametrize("connectivity", wd.NETWORK_FAULTS)
    def test_no_action_while_the_network_is_the_suspect(self, connectivity):
        assert wd.decide_action(self._ripe_for_reboot(), NOW, connectivity) == "none"

    def test_action_resumes_once_wu_is_reachable(self):
        assert wd.decide_action(self._ripe_for_reboot(), NOW, "wu_reachable") == "reboot"

    def test_unprobed_program_faults_are_never_suppressed(self):
        """A stale heartbeat is not probed at all -- the string must not match a network fault."""
        connectivity = "not probed (program not responding)"
        assert connectivity not in wd.NETWORK_FAULTS
        assert wd.decide_action(self._ripe_for_reboot(), NOW, connectivity) == "reboot"

    def test_opt_in_flag_allows_acting_on_a_dead_link(self, monkeypatch):
        """For the case where the Pi's own NIC is suspect. Off by default."""
        monkeypatch.setattr(wd, "REBOOT_ON_NO_INTERNET", True)
        assert wd.decide_action(self._ripe_for_reboot(), NOW, "internet_unreachable") == "reboot"


class TestPrivilegeEscalation:
    def test_root_runs_the_command_directly(self, monkeypatch):
        monkeypatch.setattr(wd.os, "geteuid", lambda: 0)
        assert wd.privileged(["/usr/bin/systemctl", "reboot"]) == ["/usr/bin/systemctl", "reboot"]

    def test_unprivileged_goes_through_sudo_without_prompting(self, monkeypatch):
        """-n matters: a sudo password prompt in a oneshot timer unit would hang, not fail."""
        monkeypatch.setattr(wd.os, "geteuid", lambda: 1000)
        assert wd.privileged(["/usr/bin/systemctl", "reboot"]) == [
            "sudo", "-n", "/usr/bin/systemctl", "reboot"]

    def test_commands_match_the_sudoers_rule(self):
        """The sudoers file grants these two argv exactly; a drift here fails closed at runtime
        with 'a password is required', which is easy to miss on an unattended station."""
        import pathlib
        sudoers = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "weather-watchdog.sudoers"
        granted = [line.split("NOPASSWD:", 1)[1].strip()
                   for line in sudoers.read_text().splitlines()
                   if line.startswith("pi ") and "NOPASSWD:" in line]
        assert f"/usr/bin/systemctl restart {wd.UPLOADER_UNIT}" in granted
        assert "/usr/bin/systemctl reboot" in granted
