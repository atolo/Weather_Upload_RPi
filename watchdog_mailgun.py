import json
import os
import socket
import subprocess
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
    except Exception:
        return default


def save_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as file_handle:
        json.dump(payload, file_handle)


def format_age(now, timestamp):
    if not isinstance(timestamp, (int, float)):
        return "unknown"
    return f"{int(now - timestamp)}s"


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


def evaluate_health(status, now):
    issues = []

    last_heartbeat = status.get("last_heartbeat")
    if not isinstance(last_heartbeat, (int, float)):
        issues.append("missing heartbeat timestamp")
    elif (now - last_heartbeat) > STALE_HEARTBEAT_SECONDS:
        issues.append(f"heartbeat stale for {format_age(now, last_heartbeat)}")

    last_upload = status.get("last_successful_upload")
    if not isinstance(last_upload, (int, float)):
        issues.append("missing successful upload timestamp")
    elif (now - last_upload) > STALE_UPLOAD_SECONDS:
        issues.append(f"upload stale for {format_age(now, last_upload)}")

    return issues


def reboot_pi():
    print("Rebooting Raspberry Pi")
    try:
        subprocess.run(["/sbin/reboot"], check=False)
    except Exception as exc:
        print(f"Failed to execute reboot command: {exc}")


def main():
    now = time.time()

    if get_uptime_seconds() < BOOT_GRACE_SECONDS:
        print("Boot grace window active; watchdog check skipped")
        return

    status = load_json(STATUS_FILE, {})
    state = load_json(
        WATCHDOG_STATE_FILE,
        {
            "consecutive_failures": 0,
            "last_alert_at": 0,
            "reboot_triggered": False,
        },
    )

    hostname = socket.gethostname()
    issues = evaluate_health(status, now)

    if issues:
        state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1
        should_alert = (now - float(state.get("last_alert_at", 0))) >= ALERT_COOLDOWN_SECONDS
        issue_text = "; ".join(issues)
        last_error = status.get("last_upload_error", "none")

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

        if (
            REBOOT_ENABLED
            and state["consecutive_failures"] >= MAX_FAILURES_BEFORE_REBOOT
            and not state.get("reboot_triggered", False)
        ):
            reboot_subject = f"Weather station watchdog rebooting {hostname}"
            reboot_body = body + "\nAction: reboot initiated by watchdog.\n"
            send_mailgun_email(reboot_subject, reboot_body)
            state["reboot_triggered"] = True
            save_json(WATCHDOG_STATE_FILE, state)
            reboot_pi()
            return

        save_json(WATCHDOG_STATE_FILE, state)
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
    state["reboot_triggered"] = False
    save_json(WATCHDOG_STATE_FILE, state)
    print("Watchdog check passed")


if __name__ == "__main__":
    main()
