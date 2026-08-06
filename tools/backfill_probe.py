"""Send one backdated observation to Weather Underground, to measure the backfill window.

    ./venv/bin/python tools/backfill_probe.py "05:25:00 PM"          # dry run, prints the URL
    ./venv/bin/python tools/backfill_probe.py "05:25:00 PM" --send   # actually sends it

AUDIT_PLAN stage R0. WU documents no maximum age for a backdated `dateutc`, and R5's shape
depends on the answer: if the window is an hour, a 31-minute outage is recoverable and a
47-hour one never is.

The observation is read from today's local data log, so what gets sent is real data this
station recorded and failed to publish -- not fabricated. Credentials are read from
WU_credentials.py the same way WU_upload does; nothing secret is passed on the command line or
printed. The URL is shown redacted.

Defaults to a dry run. `--send` performs a single upload and nothing else.

Verify afterwards on the station's history page for the day: if the row appears at the
requested time, backfill of that age works.
"""

import argparse
import datetime
import os
import sys
import urllib.parse

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

import requests

import WU_credentials
import WU_upload

# Deliberately NOT rtupdate: rapidfire exists to drive the live display. Backdated observations
# belong on the standard endpoint.
ENDPOINT = "https://weatherstation.wunderground.com/weatherstation/updateweatherstation.php"

LOGS_DIR = os.path.join(REPO_ROOT, "Logs")

# Column order written by printWeatherDataTable(). Index 6 is the running average wind
# direction, which is what upload2WU sends as winddir; index 5 is the instantaneous value.
COL_TEMP, COL_HUMIDITY, COL_PRESSURE = 0, 1, 2
COL_WIND, COL_GUST, COL_WINDDIR = 3, 4, 6
COL_RAINRATE, COL_RAINTODAY, COL_DEWPOINT = 7, 8, 9
COL_TIMESTAMP = 10


def load_row(when, path, tolerance_seconds=30):
    """The logged observation closest to `when`, or None if nothing is near enough."""
    best, best_delta = None, None
    with open(path) as handle:
        next(handle, None)  # header
        for line in handle:
            fields = line.rstrip("\n").split("\t")
            if len(fields) <= COL_TIMESTAMP:
                continue
            try:
                stamp = datetime.datetime.strptime(fields[COL_TIMESTAMP].strip(),
                                                   "%m/%d/%Y %I:%M:%S %p")
            except ValueError:
                continue
            delta = abs((stamp - when).total_seconds())
            if best_delta is None or delta < best_delta:
                best, best_delta = (stamp, fields), delta

    if best is None or best_delta > tolerance_seconds:
        return None
    return best


def build_url(stamp, fields):
    """Same field set as upload2WU, with a real dateutc instead of 'now'."""
    utc = stamp.astimezone(datetime.timezone.utc)

    params = [
        ("ID", WU_credentials.WU_STATION_ID_SUNTEC),
        ("PASSWORD", WU_credentials.WU_PASSWORD),
        ("dateutc", utc.strftime("%Y-%m-%d %H:%M:%S")),
        ("winddir", "%.0f" % float(fields[COL_WINDDIR])),
        ("windspeedmph", "%.1f" % float(fields[COL_WIND])),
        ("windgustmph", "%.1f" % float(fields[COL_GUST])),
        ("tempf", "%.1f" % float(fields[COL_TEMP])),
        ("rainin", "%.2f" % float(fields[COL_RAINRATE])),
        ("dailyrainin", "%.2f" % float(fields[COL_RAINTODAY])),
        ("baromin", "%.2f" % float(fields[COL_PRESSURE])),
        ("dewptf", "%.1f" % float(fields[COL_DEWPOINT])),
        ("humidity", "%.1f" % float(fields[COL_HUMIDITY])),
        ("softwaretype", "RaspberryPi"),
        ("action", "updateraw"),
    ]
    return ENDPOINT + "?" + urllib.parse.urlencode(params), utc


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("time", help='local time of the observation, e.g. "05:25:00 PM"')
    parser.add_argument("--date", default=datetime.date.today().strftime("%y%m%d"),
                        help="log date as yymmdd (default: today)")
    parser.add_argument("--send", action="store_true",
                        help="actually upload. Without this, prints what would be sent and exits.")
    args = parser.parse_args()

    log_path = os.path.join(LOGS_DIR, "Upload Data_%s.txt" % args.date)
    if not os.path.exists(log_path):
        sys.exit("No data log at %s" % log_path)

    day = datetime.datetime.strptime(args.date, "%y%m%d").date()
    try:
        clock = datetime.datetime.strptime(args.time.strip(), "%I:%M:%S %p").time()
    except ValueError:
        sys.exit('Could not parse time %r -- expected something like "05:25:00 PM"' % args.time)

    match = load_row(datetime.datetime.combine(day, clock), log_path)
    if match is None:
        sys.exit("No logged observation within 30s of %s on %s" % (args.time, args.date))

    stamp, fields = match
    url, utc = build_url(stamp, fields)
    age = datetime.datetime.now(datetime.timezone.utc) - utc.replace(tzinfo=datetime.timezone.utc) \
        if utc.tzinfo is None else datetime.datetime.now(datetime.timezone.utc) - utc

    print("observation : %s local  ->  %s UTC" % (stamp.strftime("%Y-%m-%d %I:%M:%S %p"),
                                                  utc.strftime("%Y-%m-%d %H:%M:%S")))
    print("age         : %.1f hours" % (age.total_seconds() / 3600.0))
    print("temp %s F   humidity %s%%   pressure %s inHg   rain today %s in"
          % (fields[COL_TEMP], fields[COL_HUMIDITY], fields[COL_PRESSURE], fields[COL_RAINTODAY]))
    print("url         : %s" % WU_upload.redact(url))

    if not args.send:
        print("\nDry run. Re-run with --send to upload this single observation.")
        return

    try:
        response = requests.get(url, timeout=(5, 10))
    except Exception as exc:
        sys.exit("request failed: %s" % WU_upload.redact(exc))

    print("\nHTTP %s" % response.status_code)
    print("body: %r" % response.text.strip()[:200])
    if response.status_code == 200 and "success" in response.text.lower():
        print("\nAccepted. Now check the station's history page for the day and confirm a row")
        print("appears at %s local. Acceptance is not the same as retention." % stamp.strftime("%I:%M %p"))
    else:
        print("\nRejected -- record the status and body in AUDIT_PLAN R0.")


if __name__ == "__main__":
    main()
