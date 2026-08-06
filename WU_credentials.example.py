# Template for WU_credentials.py
#
#   cp WU_credentials.example.py WU_credentials.py
#   # then fill in the real values
#
# WU_credentials.py is gitignored and must never be committed.
#
# Every name below is read somewhere in the codebase. A missing name surfaces as
# an AttributeError at startup with no useful message, so define all of them --
# use an empty string for anything you are not using.

# --- Weather Underground ----------------------------------------------------

# PWS upload password (NOT your wunderground.com account password).
# Found at: wunderground.com -> My Profile -> My Devices -> the station -> Key.
# Used by: WU_upload.py
WU_PASSWORD = "your-pws-key-here"

# API key for the api.weather.com observations endpoint (used to recover the
# day's rain accumulation on restart, so a reboot mid-day doesn't zero it).
# Same page as above.
# Used by: WU_download.py
WU_API_KEY = "your-api-key-here"

# Your station's WU ID, e.g. "KXXNNNN1234".
# Used by: Weather_Station.py, WU_download.py
WU_STATION_ID_SUNTEC = "KXXNNNN1234"

# Optional second station ID for testing without polluting the live feed.
# Swap it in at Weather_Station.py:47-48.
WU_STATION_ID_TEST = ""

# Nearby PWS IDs, formerly used to borrow barometric pressure before the BME280
# was fitted. Only read by WU_download.getPressure(), which is now dead code
# (see AUDIT_PLAN.md item 24). Kept so the import does not fail; set to () once
# getPressure() is deleted.
WU_LOCAL_STATIONS = ()


# --- Mailgun (watchdog alerting) --------------------------------------------
#
# Read by watchdog_mailgun.py via get_credential(), which checks environment
# variables FIRST and falls back to these names. Either the bare name or the
# WU_-prefixed variant works; see watchdog_mailgun.py:37-52.
#
# Leave all four empty to disable email alerting -- the watchdog will log
# "Mailgun is not configured; skipping email send" and carry on.

MAILGUN_API_KEY = ""
MAILGUN_DOMAIN = ""                       # e.g. "mg.example.com"
MAILGUN_FROM = ""                         # e.g. "Weather <weather@example.com>"
MAILGUN_TO = ""                           # e.g. "you@example.com"
