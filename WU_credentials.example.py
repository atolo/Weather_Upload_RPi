# Template for WU_credentials.py
#
#   cp WU_credentials.example.py WU_credentials.py
#   # then fill in the real values
#
# WU_credentials.py is gitignored and must never be committed.
#
# Define every name below, even the ones you are not using: a missing name surfaces
# as an AttributeError at startup with no useful message. An empty string is fine.
# Where a name is no longer read by any code, it says so.

# --- Weather Underground ----------------------------------------------------

# PWS upload password (NOT your wunderground.com account password).
# Found at: wunderground.com -> My Profile -> My Devices -> the station -> Key.
# Used by: WU_upload.py
WU_PASSWORD = "your-pws-key-here"

# API key for the api.weather.com observations endpoint.
# No longer read by anything: the only caller was WU_download.getDailyRain(), which is
# gone (AUDIT_PLAN item 37 -- the day's rain total is now kept locally in
# Logs/rain_state.json, which works with the WAN down). Keep the value if you intend to
# query the API by hand; it is safe to leave empty.
WU_API_KEY = ""

# Your station's WU ID, e.g. "KXXNNNN1234".
# Used by: Weather_Station.py
WU_STATION_ID_SUNTEC = "KXXNNNN1234"

# Optional second station ID for testing without polluting the live feed.
# Swap it in at Weather_Station.py:55-56.
WU_STATION_ID_TEST = ""


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
