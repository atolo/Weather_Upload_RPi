# Deployment — Weather_Upload_RPi

From-scratch installation of the weather station uploader on a Raspberry Pi.

**Status:** written 2026-08-06, describing the deployment as it exists *today* (pre-refactor). It is
accurate for the current codebase and usable right now — including for an emergency rebuild if the
SD card fails.

> **Keep this in sync.** Any change that alters how the software is installed, configured, or
> launched must update this file **in the same commit**. That includes new dependencies, new systemd
> units, new config files, and any change to the `Logs/` layout. A deployment doc that drifts is
> worse than none, because it is trusted.

---

## 1. Bill of materials

| Item | Notes |
|---|---|
| Raspberry Pi | Production unit is a **Pi 3 Model B Rev 1.2**. Any model with a 40-pin header works; §4.3 (UART) differs slightly by generation. |
| SD card | 16 GB is ample (current card: SU16G). Prefer an endurance-rated card — this workload writes ~11,000 log rows/day. |
| RS485 HAT / module | Converts the Davis differential pair to the Pi's UART |
| BME280 breakout | I2C barometric pressure. The Davis 6322C has **no** barometer — it lives in the console, which is not reachable over RS485. |
| RJ11 cable | To the Davis 6322C ISS |
| Power supply | Official/quality PSU. Under-voltage causes silent instability; verify with `vcgencmd get_throttled` (see §9). |

## 2. Wiring

### Davis 6322C → RS485 module (RJ11)

| Pin | Colour | Signal |
|---|---|---|
| 1 | Black | +5 V |
| 2 | Red | B |
| 3 | Green | A |
| 4 | Yellow | GND |

Link parameters: **4800 baud, 8N1**, 8-byte packets, one every ~2.56 s for station ID 1.

### BME280 → Pi (I2C)

`VIN`→3V3, `GND`→GND, `SCL`→GPIO3 (pin 5), `SDA`→GPIO2 (pin 3). The address is hardcoded to **0x76**
at `Weather_Station.py:112`; some breakouts ship as 0x77 and will need that line changed.

---

## 3. Reference configuration — the known-good production Pi

Captured 2026-08-06 from the running station. **This is the authority.** When building a new Pi,
match it; deviate only deliberately and one change at a time.

| Setting | Production value |
|---|---|
| Board | **Raspberry Pi 3 Model B Rev 1.2** |
| OS | Debian GNU/Linux 13 (trixie), 64-bit (`arm_64bit=1`) |
| Python | 3.13.5 (`/usr/bin/python3.13`) |
| `/dev/serial0` | → **`ttyAMA0`** (PL011 — the good UART) |
| UART enabled | `enable_uart=1` under `[all]` |
| Bluetooth | **disabled** — `dtoverlay=pi3-disable-bt` *and* `dtoverlay=disable-bt` |
| Wi-Fi | **disabled** — `dtoverlay=pi3-disable-wifi` *and* `dtoverlay=disable-wifi`. The station runs on **Ethernet**. |
| Serial console | **absent** from `cmdline.txt` — only `console=tty1`. Correct. |
| I2C | `dtparam=i2c_arm=on`; BME280 detected at **0x76** |
| Groups for `pi` | `dialout`, `i2c`, `gpio`, `spi`, `sudo`, … |
| Timezone | `America/New_York`, `System clock synchronized: yes` |
| RTC | `RTC time: n/a` — **no hardware clock** (see §4.5) |

The Bluetooth overlays are why `/dev/serial0` resolves to `ttyAMA0` rather than the mini-UART. Both
the generic and `pi3-` prefixed forms are present — redundant but harmless. **Keep this**; it is the
correct configuration for a continuous 4800-baud link (§4.3 explains why).

Non-essential defaults also present, safe to omit on a headless rebuild: `dtoverlay=vc4-kms-v3d`,
`camera_auto_detect=1`, `display_auto_detect=1`, `dtparam=audio=on`, `max_framebuffers=2`.

### Re-capturing this from a live Pi

Run before wiping any existing unit:

```bash
echo "=== OS / Python ==="; cat /etc/os-release | head -3; python3 --version; ls -l /home/pi/weather/Weather_Upload_RPi/venv/bin/python
echo "=== boot config ==="; sudo cat /boot/firmware/config.txt 2>/dev/null || sudo cat /boot/config.txt
echo "=== cmdline ==="; sudo cat /boot/firmware/cmdline.txt 2>/dev/null || sudo cat /boot/cmdline.txt
echo "=== serial ==="; ls -l /dev/serial*; echo "=== i2c ==="; i2cdetect -y 1
echo "=== groups ==="; groups pi; echo "=== tz ==="; timedatectl | head -5
echo "=== pinned deps ==="; /home/pi/weather/Weather_Upload_RPi/venv/bin/pip freeze
```

Compare against the table above. The two that matter most are **which UART `/dev/serial0` resolves
to** and whether the Bluetooth overlays are present.

---

## 4. Operating system

### 4.1 Install

Flash Raspberry Pi OS (64-bit, Lite is sufficient — no desktop needed) with Raspberry Pi Imager.
In the Imager's advanced options set: hostname `weather`, the `pi` user, SSH on, Wi-Fi if used, and
**the correct locale/timezone** (see §4.4 — this is not cosmetic).

Then:

```bash
sudo apt update && sudo apt full-upgrade -y && sudo apt install -y git python3-venv python3-dev i2c-tools
```

### 4.2 Enable the serial port

```bash
sudo raspi-config
```

`Interface Options` → `Serial Port`:

- *"Would you like a login shell to be accessible over serial?"* → **No**
- *"Would you like the serial port hardware to be enabled?"* → **Yes**

Both answers matter. Leaving the login shell enabled puts a getty on the port and the Davis data will
be consumed by it.

### 4.3 UART selection — required

Pi models with Bluetooth assign the capable **PL011** UART (`ttyAMA0`) to the Bluetooth modem by
default, leaving the **mini-UART** (`ttyS0`) on the GPIO header. The mini-UART derives its baud rate
from the VPU core clock, which varies with CPU load unless pinned — a well-known source of framing
errors on a continuous link.

Production runs on `ttyAMA0` with Bluetooth disabled, and measures **13 decode failures in 3 days**.
Replicate it. Append to `/boot/firmware/config.txt`:

```bash
sudo tee -a /boot/firmware/config.txt >/dev/null <<'EOF'

[all]
enable_uart=1

# Free the PL011 UART for the RS485 link (station runs on Ethernet)
dtoverlay=disable-bt
dtoverlay=disable-wifi
EOF
```

```bash
sudo systemctl disable hciuart && sudo reboot
```

On a Pi 3 specifically, the production unit also carries the `pi3-`prefixed forms
(`dtoverlay=pi3-disable-bt`, `dtoverlay=pi3-disable-wifi`). These are legacy aliases and are
redundant alongside the generic ones — harmless to include, unnecessary to add.

After reboot, **verify before going further**:

```bash
ls -l /dev/serial0
```

Must print `-> ttyAMA0`. If it says `ttyS0`, the overlay did not take and the link will be
unreliable.

> Disabling Wi-Fi assumes Ethernet, as production uses. Omit `dtoverlay=disable-wifi` if the new unit
> needs Wi-Fi — it is independent of the UART fix, which only requires `disable-bt`.

### 4.4 Enable I2C

```bash
sudo raspi-config nonint do_i2c 0 && sudo reboot
```

After reboot, confirm the sensor is visible — expect `76` (or `77`) in the grid:

```bash
i2cdetect -y 1
```

### 4.5 Timezone and clock

**Not cosmetic.** Log filenames use `time.strftime("%y%m%d")` and the daily rain reset compares the
local day-of-month (`Weather_Station.py:811`). A wrong timezone rolls the rain counter at the wrong
hour and misdates every log file.

```bash
sudo timedatectl set-timezone America/New_York && timedatectl
```

The Pi has **no RTC**. On boot it restores the clock from `fake-hwclock` and corrects it once NTP
syncs. Every program interval is measured on the monotonic clock so an NTP step cannot stall uploads
(`temp/AUDIT_PLAN.md` item 27), but log filenames and the daily rain rollover are wall-clock by
necessity, so the timezone still has to be right.

### 4.6 Persistent journald

The current Pi has volatile journald retaining only ~13 hours, which destroyed the evidence for the
Aug 2026 incident. **Do this on every build:**

```bash
sudo mkdir -p /var/log/journal && sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald && journalctl --disk-usage
```

The 200 MB cap keeps it from filling the card.

---

## 5. Application

### 5.1 Clone and create the venv

```bash
mkdir -p /home/pi/weather && cd /home/pi/weather && git clone https://github.com/atolo/Weather_Upload_RPi.git && cd Weather_Upload_RPi
```

```bash
python3 -m venv venv && ./venv/bin/pip install --upgrade pip && ./venv/bin/pip install -r requirements.txt
```

If a `requirements.lock.txt` exists, install from that instead — the pinned versions are known-good,
and `tests/` covers the watchdog only, so nothing here would catch a decoder or main-loop regression
from an upgrade.

> **venv fragility:** the venv is bound to the exact system interpreter (currently
> `/usr/bin/python3.13`). A distribution upgrade that bumps the minor Python version breaks it
> silently. After any `apt full-upgrade` that touches Python, recreate the venv.

### 5.2 Credentials

```bash
cp WU_credentials.example.py WU_credentials.py && nano WU_credentials.py
```

Fill in every field — the example file documents what each one is and where to find it. Then lock it
down, since it holds your PWS key, API key, and Mailgun credentials:

```bash
chmod 600 WU_credentials.py && ls -l WU_credentials.py
```

It is gitignored and must never be committed.

### 5.3 Group membership

The service user needs `dialout` (serial) and `i2c`. The default `pi` user usually has both already;
verify, and add if you used a different username:

```bash
groups pi || sudo usermod -aG dialout,i2c pi
```

Log out and back in for group changes to take effect.

### 5.4 Smoke test before installing the service

Run it in the foreground first. You should see decoded packets within a few seconds:

```bash
cd /home/pi/weather/Weather_Upload_RPi && WEATHER_DEBUG=1 ./venv/bin/python Weather_Station.py
```

`WEATHER_DEBUG=1` matters: per-packet tracing is opt-in, and without it a *working* station prints
almost nothing, which is indistinguishable from a broken one. Never set it in the service unit — it
emits ~46,000 journal lines a day.

Expect: `BME280 sensor initialized successfully`, `Daily rain restored from local state`, then a
stream of `Successfully decoded:` lines and a tabular weather summary. `Ctrl-C` to stop.

If you see CRC failures on every packet, the UART is wrong (§4.3) or the A/B pair is swapped. If the
port cannot be opened at all you get ten `Serial open failed` lines over 30 s and a non-zero exit
(item 5) — check `ls -l /dev/serial0` and group membership above.

Then run the test suite, which needs no hardware:

```bash
./venv/bin/pip install pytest && ./venv/bin/python -m pytest tests/ -q
```

---

## 6. systemd units

**`deploy/` in this repo is the source of truth.** It holds the three units verbatim, plus the
journald drop-in and the watchdog's sudoers rule. Do not transcribe them here — a second copy drifts,
and this section used to prove it.

Read `deploy/weather_uploader.service` and `deploy/weather-watchdog.service` for the reasoning; every
non-obvious directive is commented in place.

### 6.1 Install

```bash
sudo install -m 440 -o root -g root deploy/weather-watchdog.sudoers /etc/sudoers.d/weather-watchdog
```

```bash
sudo visudo -c
```

```bash
sudo install -m 644 deploy/weather_uploader.service deploy/weather-watchdog.service deploy/weather-watchdog.timer /etc/systemd/system/
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now weather_uploader.service weather-watchdog.timer
```

**Install the sudoers file first.** The watchdog runs as `pi` and escalates through it for the only
two privileged things it does (`systemctl restart weather_uploader.service` and `systemctl reboot`).
Without the rule it still runs, still alerts, and reports `restart FAILED -- check the sudoers rule`,
but it cannot act.

Two directives are load-bearing and easy to lose:

| Directive | Why |
|---|---|
| `WorkingDirectory=` on the uploader | No longer load-bearing for logs — every path is anchored to the script directory (item 6) — but keep it so relative paths in future code cannot silently escape |
| `Type=notify` + `WatchdogSec=120` | The fix for the Aug 1–3 2026 outage. Removing it restores the failure mode where a blocked main loop runs for 47 hours |

Upgrading a Pi that predates the item 39 change: `Logs/watchdog_state.json` will be root-owned from
when the watchdog ran as root. `Logs/` is group-writable by `pi` so the atomic writer can replace it
regardless, but make it tidy:

```bash
sudo chown pi:pi /home/pi/weather/Weather_Upload_RPi/Logs/watchdog_state.json
```

### 6.2 Verifying the privilege drop

```bash
sudo -l -U pi | grep -A3 NOPASSWD
```

Must list exactly the two `systemctl` commands. Then confirm the watchdog is no longer root:

```bash
systemctl show weather-watchdog.service -p User -p Group
```

### 6.3 Optional tuning — `/etc/default/weather-watchdog`

Absent by default, which means all defaults apply **and rebooting is enabled**. Create it only to
override:

```bash
WATCHDOG_STALE_UPLOAD_SECONDS=900
WATCHDOG_STALE_HEARTBEAT_SECONDS=180
WATCHDOG_MAX_FAILURES_BEFORE_RESTART=2
WATCHDOG_MAX_FAILURES_BEFORE_REBOOT=3
WATCHDOG_RESTART_GRACE_SECONDS=600
WATCHDOG_MIN_SECONDS_BETWEEN_RESTARTS=900
WATCHDOG_MIN_SECONDS_BETWEEN_REBOOTS=21600
WATCHDOG_ALERT_COOLDOWN_SECONDS=1800
WATCHDOG_BOOT_GRACE_SECONDS=300
WATCHDOG_REBOOT_ENABLED=true
WATCHDOG_REBOOT_ON_NO_INTERNET=false
WATCHDOG_NEUTRAL_PROBE_IP=1.1.1.1
```

The escalation ladder (item 2) runs on a 5-minute timer and prefers the least destructive action:

1. **Two consecutive failures (~10 min)** → `systemctl restart weather_uploader.service`.
2. **Three consecutive failures, and the restart has had 10 minutes to work** → reboot, subject to a
   6-hour reboot cooldown that a recovery does not clear.
3. **Uploads stale but the heartbeat is fresh** → probe connectivity first. If the internet, DNS or
   Weather Underground is unreachable, alert and do **nothing else**: the program is fine, and both
   available actions would only take local logging down too. `WATCHDOG_REBOOT_ON_NO_INTERNET=true`
   overrides this, for the case where the Pi's own NIC is the suspect.

Set `WATCHDOG_REBOOT_ENABLED=false` during commissioning so a misconfiguration cannot put the new Pi
into a reboot cycle while you are still working on it — and remember to remove it afterwards.

---

## 7. Verification checklist

Work through all of it before declaring the build done.

```bash
systemctl is-active weather_uploader; systemctl is-enabled weather_uploader; systemctl list-timers weather-watchdog.timer --no-pager
```

```bash
journalctl -u weather_uploader -n 30 --no-pager
```

```bash
ls -l Logs/ && tail -3 "Logs/Upload Data_$(date +%y%m%d).txt"
```

```bash
cat Logs/weather_status.json
```

| Check | Expected |
|---|---|
| Service active and enabled | `active` / `enabled` |
| Timer scheduled | next trigger ≤ 5 min away |
| Decoding | `Successfully decoded:` lines in the journal |
| Data log growing | new rows every ~7–8 s |
| `weather_status.json` | `last_heartbeat` within 30 s of now; `last_successful_upload` present |
| Watchdog | `Watchdog check passed` every 5 min |
| WU feed | station page on wunderground.com showing current data |
| Power | `vcgencmd get_throttled` → `throttled=0x0` |
| Time | `timedatectl` → correct zone, `System clock synchronized: yes` |

Also confirm supervision actually works, rather than assuming:

```bash
sudo systemctl kill -s KILL weather_uploader; sleep 8; systemctl is-active weather_uploader
```

Should print `active` — systemd restarted it within `RestartSec=5`.

> This does **not** prove recovery from a *hang*, only from a crash. Nothing in the current build
> detects a hung main loop; that is `temp/AUDIT_PLAN.md` item 29 and is the top of the remediation plan.

---

## 8. Routine operations

**Deploy a change:**

```bash
cd /home/pi/weather/Weather_Upload_RPi && git pull && sudo systemctl restart weather_uploader && sleep 5 && systemctl status weather_uploader --no-pager
```

If a unit file changed, `sudo systemctl daemon-reload` first.

**Watch it live:**

```bash
journalctl -u weather_uploader -f
```

**Diagnostic tooling** — install once, at build time, so it is present when needed:

```bash
./venv/bin/pip install py-spy
```

Dump the stack of the *running* process (needs root; Debian sets `ptrace_scope=1`):

```bash
sudo /home/pi/weather/Weather_Upload_RPi/venv/bin/py-spy dump --pid $(pgrep -f Weather_Station.py)
```

A healthy idle process shows `<module> (Weather_Station.py:615)` — the `time.sleep(0.1)` branch.
**Capture this baseline at build time.** Anything else during an incident is the answer.

---

## 9. Troubleshooting

| Symptom | Likely cause | Check |
|---|---|---|
| CRC failure on every packet | Wrong UART, or A/B swapped | §4.3; try swapping green/red |
| No serial data at all | Login shell still on the port; wrong `/dev` node; wiring | `§4.2`; `ls -l /dev/serial0` |
| `Failed to initialize BME280` | I2C off, wrong address, wiring | `i2cdetect -y 1`; address at `Weather_Station.py:77` |
| `Permission denied` on `/dev/serial0` | User not in `dialout` | `groups` |
| Service dead at boot, fine manually | Serial port not ready, or the venv path is wrong | §6.1, `journalctl -u weather_uploader` |
| Uploads fail, decoding fine | Credentials, or no internet | `curl -sS https://rtupdate.wunderground.com` |
| No watchdog emails | Mailgun unconfigured or failing | §10 test command |
| Random reboots | Watchdog acting on stale uploads | `cat Logs/watchdog_state.json` |
| Logs dated wrongly / rain resets at odd hour | Timezone | §4.5 |

**Test Mailgun end to end** (sends one real email):

```bash
cd /home/pi/weather/Weather_Upload_RPi && /usr/bin/python3 -c "import watchdog_mailgun as w; print(w.send_mailgun_email('Watchdog test','Deployment verification - ignore.'))"
```

Importing is safe — `main()` is guarded, so nothing evaluates health or reboots.

---

## 10. Known deployment risks

Read before building a fresh Pi.

1. **`pip install board` is a trap — do not do it.** `Weather_Station.py:17` does `import board`,
   which is provided by **Adafruit-Blinka**. There is also an unrelated PyPI package named `board`,
   and the natural reaction to an `import board` failure is to install it. Both write
   `site-packages/board.py`, so whichever lands last wins.

   The production venv currently has both (`board==1.0` appears in `pip freeze`). It works only
   because Blinka's copy happens to be the surviving one. `requirements.lock.txt` deliberately
   excludes it. If `import board` fails on a new build, the cause is Blinka not installing or not
   detecting the platform — **not** a missing `board` package.

   Verify which one you have:

   ```bash
   ./venv/bin/python -c "import board; print(board.__file__); print(hasattr(board,'I2C'))"
   ```

   Wrong answer looks like a `board/` **directory**, or `hasattr → False`.

2. **Adafruit-Blinka platform support.** Blinka needs a working GPIO backend for its platform
   detection. On Pi 5 and recent Pi OS this is `lgpio` rather than the classic `RPi.GPIO`; install
   `rpi-lgpio` (a drop-in replacement) if `import board` fails. The production Pi 3B uses
   `RPi.GPIO==0.7.1` and is unaffected — but a fresh build on newer hardware may not be.

   Note this whole class of problem disappears if the Blinka stack is dropped (`temp/AUDIT_PLAN.md`
   item 32).
3. **One Python environment, now.** Both the uploader and the watchdog run on the project venv
   (item 30). A fresh build needs `requests` and `sdnotify` there and nowhere else — but check
   `systemctl cat weather-watchdog` on an upgraded Pi, because the old unit pointed at
   `/usr/bin/python3`.
4. **Watchdog privilege.** It runs as `pi` and needs `/etc/sudoers.d/weather-watchdog` to restart the
   uploader or reboot (item 39). Miss it and the watchdog alerts but cannot act — visible as
   `restart FAILED -- check the sudoers rule` in the alert email and the journal.
5. **No RTC.** A boot without internet runs on a `fake-hwclock` estimate until NTP corrects it. All
   intervals are measured on the monotonic clock so a step cannot stall uploads (item 27), but log
   filenames and line timestamps are wall-clock and can still be misdated.
6. **Watchdog can reboot the Pi.** Defaults are active unless `/etc/default/weather-watchdog` says
   otherwise. Disable rebooting during commissioning (§6.3).
7. **Tests cover the watchdog, not the station.** `pytest tests/` runs on any machine, but
   `Weather_Station.py` still opens the serial port and the BME280 at import, so its main loop has no
   coverage. Verify a change by watching the actual WU station page, not just the local logs.

---

## 11. Rebuild / disaster recovery

What is **not** in git and must be restored separately:

| Item | Source |
|---|---|
| `WU_credentials.py` | Your password manager. Keep a copy there — losing it means re-issuing the WU key and Mailgun credentials. |
| The three systemd units + sudoers rule | `deploy/` in this repo — see §6.1 |
| `/etc/default/weather-watchdog` | §6.3, if you created one |
| OS-level config (UART, I2C, timezone, journald) | §4 |
| `Logs/` history | Not recoverable. Back it up if the record matters. |

Minimum rebuild: §4 → §5 → §6 → §7. Budget an hour, most of it OS install and `apt full-upgrade`.

**Before wiping the old Pi**, take the §3 capture and copy `Logs/` off it. The audit was only possible
because those log files survived.
