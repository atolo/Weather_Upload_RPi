"""Query Weather Underground's PWS history API for what it actually stored.

    ./venv/bin/python tools/wu_history.py                              # today, all records
    ./venv/bin/python tools/wu_history.py --from "05:00 PM" --to "06:00 PM"
    ./venv/bin/python tools/wu_history.py --date 260806 --gaps

AUDIT_PLAN stage R0. Written to answer "did WU actually keep that observation?", since an
HTTP 200 from the upload endpoint means *accepted*, which is not the same as *retained*.

**This API lags the website.** Observed 2026-08-06 20:10: the station page showed rows through
8:09 PM while this endpoint returned nothing after 7:29 PM -- roughly 40 minutes behind, and
missing rows the page already had. So "absent here" is NOT evidence of "discarded" for anything
recent. Use the station's own history page for recent data, or wait out the lag.

Kept as a diagnostic. The question it was built for is closed: backdated uploads are discarded
regardless of age, and R5 was cancelled as a result -- see AUDIT_PLAN.

Endpoint:
  https://api.weather.com/v2/pws/history/all?stationId=..&format=json&units=e&date=YYYYMMDD&apiKey=..

Reads the station ID and API key from WU_credentials.py; the key is never printed.
Read-only -- this uploads nothing.
"""

import argparse
import datetime
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

ENDPOINT = "https://api.weather.com/v2/pws/history/all"


def redact(text):
    return re.sub(r"(?i)(apikey=)[^&\s]*", r"\1<redacted>", str(text))


def fetch(date_yyyymmdd):
    # Imported here, not at module scope, so --help works on a machine without credentials.
    import requests
    import WU_credentials

    api_key = getattr(WU_credentials, "WU_API_KEY", "")
    if not api_key:
        sys.exit("WU_API_KEY is empty in WU_credentials.py -- needed for the history API.")

    params = {
        "stationId": WU_credentials.WU_STATION_ID_SUNTEC,
        "format": "json",
        "units": "e",
        "date": date_yyyymmdd,
        "apiKey": api_key,
    }
    try:
        response = requests.get(ENDPOINT, params=params, timeout=(5, 15))
    except Exception as exc:
        sys.exit("request failed: %s" % redact(exc))

    if response.status_code == 204:
        sys.exit("HTTP 204 -- WU has no observations at all for %s." % date_yyyymmdd)
    if response.status_code != 200:
        sys.exit("HTTP %s: %s" % (response.status_code, redact(response.text[:300])))

    return response.json().get("observations", [])


def local_time(observation):
    stamp = observation.get("obsTimeLocal")
    if not stamp:
        return None
    try:
        return datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def describe(observation):
    imperial = observation.get("imperial", {})
    return "%-8s  temp %-6s  hum %-5s  press %-6s  rainRate %-5s  rainDay %-5s" % (
        (local_time(observation) or "?").strftime("%I:%M %p") if local_time(observation) else "?",
        imperial.get("tempAvg", "-"),
        observation.get("humidityAvg", "-"),
        imperial.get("pressureMax", imperial.get("pressureTrend", "-")),
        imperial.get("precipRate", "-"),
        imperial.get("precipTotal", "-"),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--date", default=datetime.date.today().strftime("%y%m%d"),
                        help="yymmdd (default: today)")
    parser.add_argument("--from", dest="start", help='local start time, e.g. "05:00 PM"')
    parser.add_argument("--to", dest="end", help='local end time, e.g. "06:00 PM"')
    parser.add_argument("--gaps", action="store_true",
                        help="report intervals longer than 6 minutes instead of listing rows")
    args = parser.parse_args()

    day = datetime.datetime.strptime(args.date, "%y%m%d").date()
    observations = fetch(day.strftime("%Y%m%d"))
    print("%d observations stored for %s\n" % (len(observations), day))

    def clock(text):
        return datetime.datetime.combine(
            day, datetime.datetime.strptime(text.strip(), "%I:%M %p").time())

    window = observations
    if args.start or args.end:
        start = clock(args.start) if args.start else datetime.datetime.combine(day, datetime.time.min)
        end = clock(args.end) if args.end else datetime.datetime.combine(day, datetime.time.max)
        window = [o for o in observations
                  if local_time(o) and start <= local_time(o) <= end]
        print("window %s to %s: %d observations\n"
              % (start.strftime("%I:%M %p"), end.strftime("%I:%M %p"), len(window)))

    if args.gaps:
        times = sorted(t for t in (local_time(o) for o in window) if t)
        for earlier, later in zip(times, times[1:]):
            minutes = (later - earlier).total_seconds() / 60.0
            if minutes > 6:
                print("GAP %5.0f min   %s -> %s"
                      % (minutes, earlier.strftime("%I:%M %p"), later.strftime("%I:%M %p")))
        return

    for observation in window:
        print(describe(observation))


if __name__ == "__main__":
    main()
