import json
import os
import re
import socket
import subprocess
import tempfile
import time

import requests
import WU_credentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATUS_FILE = os.path.join(BASE_DIR, "Logs", "weather_status.json")
DEFAULT_WATCHDOG_STATE_FILE = os.path.join(BASE_DIR, "Logs", "watchdog_state.json")

STATUS_FILE = os.getenv("WEATHER_STATUS_FILE", DEFAULT_STATUS_FILE)
WATCHDOG_STATE_FILE = os.getenv("WATCHDOG_STATE_FILE", DEFAULT_WATCHDOG_STATE_FILE)

STALE_UPLOAD_SECONDS = int(os.getenv("WATCHDOG_STALE_UPLOAD_SECONDS", "900"))
STALE_HEARTBEAT_SECONDS = int(os.getenv("WATCHDOG_STALE_HEARTBEAT_SECONDS", "180"))
MAX_FAILURES_BEFORE_REBOOT = int(os.getenv("WATCHDOG_MAX_FAILURES_BEFORE_REBOOT", "3"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("WATCHDOG_ALERT_COOLDOWN_SECONDS", "1800"))
BOOT_GRACE_SECONDS = int(os.getenv("WATCHDOG_BOOT_GRACE_SECONDS", "300"))
REBOOT_ENABLED = os.getenv("WATCHDOG_REBOOT_ENABLED", "true").strip().lower() in {"1", "true", "yes"}
# Item 1: a boolean latch was cleared by any single clean pass, so a station that looked
# healthy for one cycle after a reboot could be rebooted again immediately. A timestamp
# cannot be cleared by recovery.
MIN_SECONDS_BETWEEN_REBOOTS = int(os.getenv("WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS", "21600"))
# Item 27: the uploader's timestamps are wall-clock because they have to cross a process boundary,
# and the Pi has no RTC -- NTP steps the clock, sometimes by hours. These bound how far a timestamp
# may disagree with this process's own clock before it is treated as a clock fault rather than as
# evidence about the station.
CLOCK_SKEW_TOLERANCE_SECONDS = int(os.getenv("WATCHDOG_CLOCK_SKEW_TOLERANCE_SECONDS", "120"))
MAX_FUTURE_SECONDS = int(os.getenv("WATCHDOG_MAX_FUTURE_SECONDS", "3600"))

def get_credential(attr_candidates, env_name):
    env_value = os.getenv(env_name, "").strip()
    if env_value:
        return env_value

    for attr in attr_candidates:
        value = getattr(WU_credentials, attr, "")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


MAILGUN_API_KEY = get_credential(
    ["MAILGUN_API_KEY", "WU_MAILGUN_API_KEY"],
    "MAILGUN_API_KEY",
)
MAILGUN_DOMAIN = get_credential(
    ["MAILGUN_DOMAIN", "WU_MAILGUN_DOMAIN"],
    "MAILGUN_DOMAIN",
)
MAILGUN_FROM = get_credential(
    ["MAILGUN_FROM", "WU_MAILGUN_FROM"],
    "MAILGUN_FROM",
)
MAILGUN_TO = get_credential(
    ["MAILGUN_TO", "WU_MAILGUN_TO", "ALERT_EMAIL_TO"],
    "MAILGUN_TO",
)


def get_uptime_seconds():
    try:
        with open("/proc/uptime", "r") as uptime_file:
            return float(uptime_file.read().split()[0])
    except Exception:
        return 9999999.0


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as file_handle:
            return json.load(file_handle)
    except Exception as exc:
        # A file that exists but will not parse is a real fault, not an empty state.
        # Returning the default makes evaluate_health() report missing timestamps, which
        # escalates -- that is intended. Log it so the cause is visible rather than inferred.
        print(f"WARNING: {path} exists but could not be parsed ({exc}); treating as empty")
        return default


def _atomic_write_json(path, payload, mode=0o644):
    """Write JSON via a temp file + os.replace so a reader never sees a partial file.

    mode is set explicitly because mkstemp() creates 0600 and os.replace preserves it.
    These are status files whose whole purpose is to be observable; secrets are kept out
    of them by redaction at source (item 33), not by permissions."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    handle_fd, temp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w") as temp_file:
            json.dump(payload, temp_file)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def persist_state(state, hostname):
    """consecutive_failures is the only thing carrying state between runs of this oneshot.
    An unguarded write failure would abort main() before the increment landed, so every run
    would reload the same value and the reboot threshold could never be reached -- the
    watchdog would alert forever and never act. Fail loudly instead."""
    try:
        _atomic_write_json(WATCHDOG_STATE_FILE, state)
        return True
    except Exception as exc:
        print(f"CRITICAL: could not persist watchdog state to {WATCHDOG_STATE_FILE}: {exc}")
        send_mailgun_email(
            f"Weather station watchdog state write FAILED on {hostname}",
            f"The watchdog could not persist its own state and cannot escalate reliably.\n"
            f"State file: {WATCHDOG_STATE_FILE}\n"
            f"Error: {exc}\n",
        )
        return False


def format_age(now, timestamp):
    if not isinstance(timestamp, (int, float)):
        return "unknown"
    return f"{int(now - timestamp)}s"


def redact(text):
    """The status file is untrusted input to the alerting path: it is written by another
    process and its contents are interpolated into an email that leaves the network."""
    return re.sub(r'(?i)(password=)[^&\s\'"]*', r'\1<redacted>', str(text))


def describe_last_error(status, now):
    """Report the last upload error only while it is plausibly related to the current
    incident. Reporting it unconditionally sent investigators after a fault that had
    already resolved -- observed live at 15.5 hours stale during healthy operation."""
    error_text = status.get("last_upload_error")
    if not error_text:
        return "none recorded"

    error_at = status.get("last_upload_error_at")
    if not isinstance(error_at, (int, float)):
        return f"{redact(error_text)} (age unknown)"

    if (now - error_at) > STALE_UPLOAD_SECONDS:
        return f"none recent (last was {redact(error_text)}, {format_age(now, error_at)} ago)"

    return f"{redact(error_text)} ({format_age(now, error_at)} ago)"


def send_mailgun_email(subject, body):
    if not (MAILGUN_API_KEY and MAILGUN_DOMAIN and MAILGUN_FROM and MAILGUN_TO):
        print("Mailgun is not configured; skipping email send")
        return False

    url = f"https://api.mailgun.net/v3/{MAILGUN_DOMAIN}/messages"
    payload = {
        "from": MAILGUN_FROM,
        "to": [MAILGUN_TO],
        "subject": subject,
        "text": body,
    }

    try:
        response = requests.post(url, auth=("api", MAILGUN_API_KEY), data=payload, timeout=15)
        if response.status_code >= 300:
            print(f"Mailgun error: {response.status_code} {response.text}")
            return False
        print("Mailgun alert sent")
        return True
    except Exception as exc:
        print(f"Mailgun request failed: {exc}")
        return False


def staleness_issue(label, timestamp, now, limit, uptime=None):
    """Judge one wall-clock timestamp written by the uploader, tolerating clock steps (item 27).

    Returns an issue string, or None when the field is healthy or cannot be judged.

    Three cases beyond plain staleness:

    * Timestamp in the near future -- the clock stepped backward after it was written. Its age is
      meaningless, so report nothing. This cannot hide a dead uploader for long: once the clock
      catches up the timestamp stops being in the future and normal staleness resumes.
    * Timestamp far in the future -- too large to be ordinary skew. That is itself a fault worth
      reporting, and reporting it beats silently ignoring the field forever.
    * Timestamp older than the machine's uptime -- it was written before this boot, so either the
      uploader has not run since boot (real, and the reason this case is NOT suppressed) or the
      clock stepped forward. Say which shape it is; a forward step self-heals at the next 30 s
      heartbeat, so it can poison at most one run out of the three needed to reboot.
    """
    skew = timestamp - now
    if skew > MAX_FUTURE_SECONDS:
        return f"{label} timestamp is {int(skew)}s in the future"
    if skew > CLOCK_SKEW_TOLERANCE_SECONDS:
        return None

    age = now - timestamp
    if age <= limit:
        return None
    if uptime is not None and age > (uptime + CLOCK_SKEW_TOLERANCE_SECONDS):
        return (f"{label} predates this boot ({format_age(now, timestamp)} old, "
                f"uptime {int(uptime)}s)")
    return f"{label} stale for {format_age(now, timestamp)}"


def evaluate_health(status, now, no_upload_since=None, uptime=None):
    issues = []

    last_heartbeat = status.get("last_heartbeat")
    if not isinstance(last_heartbeat, (int, float)):
        issues.append("missing heartbeat timestamp")
    else:
        issue = staleness_issue("heartbeat", last_heartbeat, now, STALE_HEARTBEAT_SECONDS, uptime)
        if issue:
            issues.append(issue)

    last_upload = status.get("last_successful_upload")
    if isinstance(last_upload, (int, float)):
        issue = staleness_issue("upload", last_upload, now, STALE_UPLOAD_SECONDS, uptime)
        if issue:
            issues.append(issue)
    elif no_upload_since is not None:
        # No upload has ever been recorded. Measure from when this watchdog first noticed,
        # not from now, so a fresh install gets a real grace period -- but unlike the
        # uploader's own started_at, this cannot be reset by restarting the service, so a
        # station that never manages to upload is still caught.
        if (now - no_upload_since) > STALE_UPLOAD_SECONDS:
            issues.append(f"no successful upload ever; first noticed {format_age(now, no_upload_since)} ago")

    return issues


def reboot_pi():
    print("Rebooting Raspberry Pi")
    try:
        subprocess.run(["/sbin/reboot"], check=False)
    except Exception as exc:
        print(f"Failed to execute reboot command: {exc}")


def main():
    now = time.time()

    uptime = get_uptime_seconds()
    if uptime < BOOT_GRACE_SECONDS:
        print("Boot grace window active; watchdog check skipped")
        return

    status = load_json(STATUS_FILE, {})
    state = load_json(
        WATCHDOG_STATE_FILE,
        {
            "consecutive_failures": 0,
            "last_alert_at": 0,
            "last_reboot_at": 0,
        },
    )

    hostname = socket.gethostname()

    # Track "never uploaded" in watchdog state, not from the uploader's started_at, which
    # a restart loop would keep refreshing.
    if isinstance(status.get("last_successful_upload"), (int, float)):
        state.pop("no_upload_since", None)
    elif not isinstance(state.get("no_upload_since"), (int, float)):
        state["no_upload_since"] = now

    issues = evaluate_health(status, now, state.get("no_upload_since"), uptime)

    if issues:
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        should_alert = (now - float(state.get("last_alert_at", 0))) >= ALERT_COOLDOWN_SECONDS
        issue_text = "; ".join(issues)
        last_error = describe_last_error(status, now)

        subject = f"Weather station watchdog alert on {hostname}"
        body = (
            f"Watchdog detected stale weather station state on {hostname}.\n"
            f"Issues: {issue_text}\n"
            f"Consecutive failures: {state['consecutive_failures']}\n"
            f"Status file: {STATUS_FILE}\n"
            f"Last upload error: {last_error}\n"
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n"
        )

        if should_alert:
            if send_mailgun_email(subject, body):
                state["last_alert_at"] = now

        last_reboot_at = float(state.get("last_reboot_at", 0) or 0)
        if (
            REBOOT_ENABLED
            and state["consecutive_failures"] >= MAX_FAILURES_BEFORE_REBOOT
            and (now - last_reboot_at) > MIN_SECONDS_BETWEEN_REBOOTS
        ):
            reboot_subject = f"Weather station watchdog rebooting {hostname}"
            reboot_body = body + "\nAction: reboot initiated by watchdog.\n"
            send_mailgun_email(reboot_subject, reboot_body)
            state["last_reboot_at"] = now
            persist_state(state, hostname)
            reboot_pi()
            return

        persist_state(state, hostname)
        return

    recovered = int(state.get("consecutive_failures", 0)) > 0
    if recovered:
        subject = f"Weather station watchdog recovered on {hostname}"
        body = (
            f"Weather station recovered on {hostname}.\n"
            f"Status file: {STATUS_FILE}\n"
            f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}\n"
        )
        send_mailgun_email(subject, body)

    state["consecutive_failures"] = 0
    # last_reboot_at is deliberately NOT cleared here: recovery is exactly when the old
    # boolean latch got reset, which is what allowed reboot loops.
    state.pop("reboot_triggered", None)
    persist_state(state, hostname)
    print("Watchdog check passed")


if __name__ == "__main__":
    main()
