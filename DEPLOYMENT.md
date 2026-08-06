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
at `Weather_Station.py:77`; some breakouts ship as 0x77 and will need that line changed.

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
local day-of-month (`Weather_Station.py:633`). A wrong timezone rolls the rain counter at the wrong
hour and misdates every log file.

```bash
sudo timedatectl set-timezone America/New_York && timedatectl
```

The Pi has **no RTC**. On boot it restores the clock from `fake-hwclock` and corrects it once NTP
syncs. See `AUDIT_PLAN.md` item 27 — all program timers must be monotonic for this to be safe.

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

If a `requirements.lock.txt` exists, install from that instead — the pinned versions are known-good
and this repo has no tests to catch an upgrade regression.

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
cd /home/pi/weather/Weather_Upload_RPi && ./venv/bin/python Weather_Station.py
```

Expect: `BME280 sensor initialized successfully`, then a stream of `Successfully decoded:` lines and
a tabular weather summary. `Ctrl-C` to stop.

If you see CRC failures on every packet, the UART is wrong (§4.3) or the A/B pair is swapped.

---

## 6. systemd units

> **Not yet in the repo.** These are transcribed from the running Pi as of 2026-08-06. Committing
> them under `deploy/` is item 4 of `AUDIT_PLAN.md` and should be done as part of the refactor;
> until then this section is the only record.

### 6.1 `/etc/systemd/system/weather_uploader.service`

```ini
[Unit]
Description=Weather_Upload_RPi service
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/weather/Weather_Upload_RPi
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/weather/Weather_Upload_RPi/venv/bin/python /home/pi/weather/Weather_Upload_RPi/Weather_Station.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`WorkingDirectory` is **load-bearing**: the program writes logs to the relative path `Logs/`
(`AUDIT_PLAN.md` item 6). Do not omit it until that item is fixed.

### 6.2 `/etc/systemd/system/weather-watchdog.service`

```ini
[Unit]
Description=Weather Station Watchdog (Mailgun)

[Service]
Type=oneshot
User=root
WorkingDirectory=/home/pi/weather/Weather_Upload_RPi
ExecStart=/usr/bin/python3 /home/pi/weather/Weather_Upload_RPi/watchdog_mailgun.py
EnvironmentFile=-/etc/default/weather-watchdog
```

Runs as **root** because it may call `/sbin/reboot`.

> Note `/usr/bin/python3`, not the venv — so `requests` must also be installed system-wide. This
> split is `AUDIT_PLAN.md` item 30; when fixed, point this at the venv interpreter and drop the
> system-wide install.

On a fresh Pi, satisfy the system-Python dependency:

```bash
sudo apt install -y python3-requests
```

(Use the Debian package, not `pip --break-system-packages` — Debian 12+ marks the system interpreter
as externally managed for good reason.)

### 6.3 `/etc/systemd/system/weather-watchdog.timer`

```ini
[Unit]
Description=Run weather station watchdog every 5 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=5min
Unit=weather-watchdog.service

[Install]
WantedBy=timers.target
```

### 6.4 Optional tuning — `/etc/default/weather-watchdog`

Absent by default, which means all defaults apply **and rebooting is enabled**. Create it only to
override:

```bash
WATCHDOG_STALE_UPLOAD_SECONDS=900
WATCHDOG_STALE_HEARTBEAT_SECONDS=180
WATCHDOG_MAX_FAILURES_BEFORE_REBOOT=3
WATCHDOG_ALERT_COOLDOWN_SECONDS=1800
WATCHDOG_BOOT_GRACE_SECONDS=300
WATCHDOG_REBOOT_ENABLED=true
```

With a 5-minute timer, the defaults reboot the Pi roughly 30 minutes into a sustained upload failure.
Set `WATCHDOG_REBOOT_ENABLED=false` during commissioning so a misconfiguration cannot put the new Pi
into a reboot cycle while you are still working on it.

### 6.5 Enable

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now weather_uploader.service weather-watchdog.timer
```

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
> detects a hung main loop; that is `AUDIT_PLAN.md` item 29 and is the top of the remediation plan.

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
| Service dead at boot, fine manually | `WorkingDirectory` missing → `Logs/` unwritable | §6.1 |
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

   Note this whole class of problem disappears if the Blinka stack is dropped (`AUDIT_PLAN.md`
   item 32).
3. **Two Python environments.** The uploader uses the venv; the watchdog uses system Python. Both
   need `requests`. Easy to satisfy one and forget the other — the failure is a watchdog that dies on
   import every 5 minutes and silently stops guarding (`AUDIT_PLAN.md` items 30, 31).
4. **`WorkingDirectory` dependency.** Log paths are relative; the service will not start correctly
   without it (item 6).
5. **No RTC.** A boot without internet runs on a `fake-hwclock` estimate until NTP corrects it, which
   corrupts elapsed-time calculations and can misdate logs (item 27).
6. **Watchdog can reboot the Pi.** Defaults are active unless `/etc/default/weather-watchdog` says
   otherwise. Disable rebooting during commissioning (§6.4).
7. **No tests.** Nothing catches a regression before it reaches the live feed. Verify by watching the
   actual WU station page, not just the local logs.

---

## 11. Rebuild / disaster recovery

What is **not** in git and must be restored separately:

| Item | Source |
|---|---|
| `WU_credentials.py` | Your password manager. Keep a copy there — losing it means re-issuing the WU key and Mailgun credentials. |
| The three systemd units | §6 of this document |
| `/etc/default/weather-watchdog` | §6.4, if you created one |
| OS-level config (UART, I2C, timezone, journald) | §4 |
| `Logs/` history | Not recoverable. Back it up if the record matters. |

Minimum rebuild: §4 → §5 → §6 → §7. Budget an hour, most of it OS install and `apt full-upgrade`.

**Before wiping the old Pi**, take the §3 capture and copy `Logs/` off it. The audit was only possible
because those log files survived.
