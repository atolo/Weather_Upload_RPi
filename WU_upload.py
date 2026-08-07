# Weather Underground API - Response fileds
# https://www.wunderground.com/weather/api/d/docs?d=data/conditions


import re
import time

import requests        # Allows you to send HTTP/1.1 requests
import WU_credentials  # Weather underground password, station IDs and API key


# Item 7: one Session for the life of the process, so the TCP + TLS handshake is not repeated
# every 5 seconds. Split timeouts because the two failures are different: a dead WAN hangs the
# connect, a wedged server hangs the read.
#
# Note what timeout= still does NOT cover: DNS. getaddrinfo() is a blocking libc call and a
# resolver that is reachable but not answering -- a very common WAN-outage shape -- can hold the
# main loop far longer than these values suggest. The backstop for that is systemd's
# WatchdogSec=120 (item 29a), not anything in this file.
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 10
_session = requests.Session()

# Item A3: a reading older than this is omitted from the upload rather than republished.
#
# Every field here is a snapshot that persists in memory until the next packet of its type
# arrives. If one sensor stops while the others keep reporting, the station otherwise keeps
# publishing that sensor's last value to Weather Underground forever, presented as a current
# observation, with nothing in the code able to notice.
#
# 300 s is comfortable against how often each type actually arrives. Measured over the
# 2,125-packet corpus in tests/fixtures/, at roughly one packet every 2.5 s: temperature every
# ~8 s, humidity and rain rate every ~15 s, wind gust every ~38 s, wind speed and direction
# every packet, and pressure on every 5 s log tick. Nothing legitimate comes close to 300 s.
#
# Note this only bites on a PARTIAL failure. If the ISS goes silent entirely, Weather_Station
# stops calling this function at all, because uploading is gated on a fresh successful decode.
MAX_READING_AGE_SECONDS = 300


def redact(text):
    """Strip PASSWORD= from anything headed for a log, the status file, or an alert email.
    requests/urllib3 exception messages frequently embed the full request URL, and the WU
    realtime protocol requires the password to be a query parameter."""
    return re.sub(r'(?i)(password=)[^&\s\'"]*', r'\1<redacted>', str(text))


# This function uploads the weather data to Weather Underground
# weatherData parameter is an instance of the weatherStation class in weather_Data_cls.py
# stationID is the Weather Underground station ID
# uploadFreq is the upload frequency in seconds
# Note, if you don't send WU temperature and dewpoint, it will assume zero
def upload2WU(weatherData, stationID, uploadFreq=5):
    # create strings to hold various parts of upload URL
    WU_url = "https://rtupdate.wunderground.com/weatherstation/updateweatherstation.php?"
    WU_creds = 'ID={}&PASSWORD={}'.format(stationID, WU_credentials.WU_PASSWORD)
    WU_software = "&softwaretype=RaspberryPi"
    WU_action = "&action=updateraw&realtime=1&rtfreq={}".format(uploadFreq)

    # Item A3: a field is sent only if it is both present AND recent. `fresh()` is purely
    # subtractive -- it can drop a field that would previously have been sent, never add one --
    # which is what makes this safe to deploy without integration tests.
    now = time.time()

    def fresh(field):
        return weatherData.is_fresh(field, MAX_READING_AGE_SECONDS, now)

    # Assemble URL to send to WU
    full_URL = WU_url + WU_creds + "&dateutc=now"
    if weatherData.gotWindDirData() and fresh("windDir"):
        full_URL = full_URL + '&winddir={:.0f}'.format(weatherData.windDir)
    if weatherData.gotWindSpeedData() and fresh("windSpeed"):
        full_URL = full_URL + '&windspeedmph={:.1f}'.format(weatherData.windSpeed)
    if weatherData.gotWindGustData() and fresh("windGust"):
        full_URL = full_URL + '&windgustmph={:.1f}'.format(weatherData.windGust)
    if weatherData.gotTemperatureData() and fresh("outsideTemp"):
        full_URL = full_URL + '&tempf={:.1f}'.format(weatherData.outsideTemp)
    if weatherData.gotRainRateData() and fresh("rainRate"):
        full_URL = full_URL + '&rainin={:.2f}'.format(weatherData.rainRate)
    # dailyrainin is deliberately NOT age-gated: it is a cumulative total, not a reading, and
    # on a dry day it legitimately goes hours without changing. Dropping it would make the
    # published daily rain fall back to zero mid-day.
    if weatherData.gotRainTodayData():
        full_URL = full_URL + '&dailyrainin={:.2f}'.format(weatherData.rainToday)
    # Only include pressure if we have a valid value. Do not send a default or from another stations.
    if weatherData.gotPressureData() and fresh("pressure"):
        full_URL = full_URL + '&baromin={:.2f}'.format(weatherData.pressure)
    # Dew point is derived, so it is only as fresh as its inputs. Its own stamp is not enough:
    # decodeRawData recomputes it whenever EITHER input arrives, so a humidity packet refreshes
    # the dew point stamp using a dead temperature sensor's last value. Checking the stamp alone
    # would drop tempf while still publishing a dew point computed from that same reading.
    if (weatherData.gotDewPointData() and fresh("dewPoint")
            and fresh("outsideTemp") and fresh("humidity")):
        full_URL = full_URL + '&dewptf={:.1f}'.format(weatherData.dewPoint)
    if weatherData.gotHumidityData() and fresh("humidity"):
        full_URL = full_URL + '&humidity={:.1f}'.format(weatherData.humidity)

    # Item 14: WU has no wind chill field -- neither windchillf nor windchill_f appears in the
    # PWS protocol field list. It derives wind chill from temperature and wind speed.

    full_URL = full_URL + WU_software + WU_action

    try:
        r = _session.get(full_URL, timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)) # send data to WU

        # If uploaded successfully, website will reply with 200
        if r.status_code == 200:
            return([True, "No Errors"])

        else:
            uploadErrMsg = "HTTP Response:{},  {}".format(r.status_code, redact(r.text))
            return([False, uploadErrMsg])
        
    # Info on requests errors:
    #  http://docs.python-requests.org/en/master/_modules/requests/exceptions/
    except requests.exceptions.ConnectionError:
        uploadErrMsg = "ConnectionError"
        return([False, uploadErrMsg])

    except requests.exceptions.HTTPError:
        uploadErrMsg = "HTTPError"
        return([False, uploadErrMsg])

    except requests.exceptions.ConnectTimeout:
        uploadErrMsg = "ConnectTimeout"
        return([False, uploadErrMsg])

    except requests.exceptions.ReadTimeout:
        uploadErrMsg = "ReadTimeout"
        return([False, uploadErrMsg])

    except requests.exceptions.RetryError:
        uploadErrMsg = "RetryError"
        return([False, uploadErrMsg])

    except requests.exceptions.Timeout:
        uploadErrMsg = "Timeout"
        return([False, uploadErrMsg])

    except Exception as e:
        uploadErrMsg = f"Unexpected error: {redact(e)}"
        return([False, uploadErrMsg])
