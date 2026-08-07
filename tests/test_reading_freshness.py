"""Tests for reading timestamps and publisher age-gating (AUDIT_PLAN item A3, stage R2).

Run from the repo root:  python3 -m pytest tests/ -q

The bug: every reading was a bare float that persisted for the life of the process. If one
sensor stopped while the others kept reporting, the station republished that sensor's last
value to Weather Underground indefinitely, presented as a current observation, and nothing in
the code could detect it.

The fix is in two halves -- weatherStation stamps each reading as it arrives, and upload2WU
omits anything older than MAX_READING_AGE_SECONDS.
"""

import pytest

import weatherData_cls
import WU_upload

MAX_AGE = WU_upload.MAX_READING_AGE_SECONDS


@pytest.fixture
def station():
    return weatherData_cls.weatherStation(1)


def age_field(station, field, seconds):
    """Backdate a field's timestamp, as though its sensor stopped `seconds` ago."""
    station.__dict__["_stamps"][field] = station.__dict__["_stamps"][field] - seconds


class TestStamping:
    def test_assignment_records_a_timestamp(self, station):
        station.outsideTemp = 74.5
        assert station.reading_age("outsideTemp") is not None
        assert station.reading_age("outsideTemp") < 1.0

    def test_initialisation_does_not_count_as_a_reading(self, station):
        """__init__ assigns NO_DATA_YET to every field. A station that has never heard from
        the ISS must not look freshly updated."""
        assert station.reading_age("outsideTemp") is None
        assert not station.is_fresh("outsideTemp", MAX_AGE)

    def test_cumulative_and_identity_fields_are_not_tracked(self, station):
        """rainToday is a daily total, not a reading -- see the class comment for why gating
        it would zero the published daily rain on a dry afternoon."""
        station.rainToday = 1.82
        station.stationID = 1
        assert station.reading_age("rainToday") is None
        assert station.reading_age("stationID") is None

    def test_reassignment_refreshes_the_stamp(self, station):
        station.humidity = 90.0
        age_field(station, "humidity", 400)
        assert not station.is_fresh("humidity", MAX_AGE)
        station.humidity = 91.0
        assert station.is_fresh("humidity", MAX_AGE)

    def test_computed_fields_are_stamped_too(self, station):
        """calcDewPoint assigns self.dewPoint, so it picks up a stamp without special-casing."""
        station.outsideTemp = 74.5
        station.humidity = 94.0
        station.calcDewPoint()
        assert station.is_fresh("dewPoint", MAX_AGE)

    def test_wind_direction_stamped_by_the_averager(self, station):
        """avgWindDir assigns self.windDir only once its 30-point buffer is full."""
        for _ in range(weatherData_cls.weatherStation.AVG_WIND_DIR_NUM_DATA_POINTS + 1):
            station.avgWindDir(90.0)
        assert station.is_fresh("windDir", MAX_AGE)

    def test_untracked_field_is_never_fresh(self, station):
        assert not station.is_fresh("stationID", MAX_AGE)


class TestFreshness:
    """These pass an explicit `now`. Relying on the wall clock would make the boundary cases
    race a few microseconds of test execution and fail intermittently."""

    def test_boundary_is_inclusive(self, station):
        station.outsideTemp = 74.5
        stamp = station.__dict__["_stamps"]["outsideTemp"]
        assert station.is_fresh("outsideTemp", MAX_AGE, now=stamp + MAX_AGE)

    def test_just_past_the_boundary_is_stale(self, station):
        station.outsideTemp = 74.5
        stamp = station.__dict__["_stamps"]["outsideTemp"]
        assert not station.is_fresh("outsideTemp", MAX_AGE, now=stamp + MAX_AGE + 0.001)

    def test_explicit_now_is_honoured(self, station):
        station.outsideTemp = 74.5
        stamp = station.__dict__["_stamps"]["outsideTemp"]
        assert station.reading_age("outsideTemp", now=stamp + 42) == pytest.approx(42)


class TestUploadUrlOmitsStaleFields:
    """What actually reaches Weather Underground. A fully-populated station is set up, then one
    sensor is aged out, and the URL is inspected."""

    @pytest.fixture
    def populated(self, station):
        station.outsideTemp = 74.5
        station.humidity = 94.0
        station.pressure = 30.15
        station.windSpeed = 3.0
        station.windGust = 5.0
        station.rainRate = 0.06
        station.rainToday = 1.82
        for _ in range(weatherData_cls.weatherStation.AVG_WIND_DIR_NUM_DATA_POINTS + 1):
            station.avgWindDir(180.0)
        station.calcDewPoint()
        return station

    @staticmethod
    def build(station):
        """The URL upload2WU would send, without sending it."""
        sent = {}

        class Recorder:
            def get(self, url, **kwargs):
                sent["url"] = url
                raise RuntimeError("stop before the network")

        original = WU_upload._session
        WU_upload._session = Recorder()
        try:
            WU_upload.upload2WU(station, "KTEST1")
        finally:
            WU_upload._session = original
        return sent["url"]

    def test_healthy_station_sends_every_field(self, populated):
        url = self.build(populated)
        for parameter in ("winddir=", "windspeedmph=", "windgustmph=", "tempf=",
                          "rainin=", "dailyrainin=", "baromin=", "dewptf=", "humidity="):
            assert parameter in url, parameter

    def test_dead_temperature_sensor_stops_being_republished(self, populated):
        """The A3 scenario exactly: temperature stops, humidity keeps arriving."""
        age_field(populated, "outsideTemp", MAX_AGE + 60)
        url = self.build(populated)
        assert "tempf=" not in url
        assert "humidity=" in url, "the working sensors must keep publishing"

    def test_dew_point_goes_stale_on_its_own_stamp(self, populated):
        age_field(populated, "dewPoint", MAX_AGE + 60)
        assert "dewptf=" not in self.build(populated)

    def test_dew_point_dies_with_its_inputs_even_when_freshly_recomputed(self, populated):
        """Caught by running the scenario end to end, not by the first version of these tests.

        decodeRawData recomputes the dew point whenever EITHER input arrives. So a humidity
        packet re-stamps dewPoint using a dead temperature sensor's last value, and gating on
        its own stamp alone would drop tempf while still publishing a dew point derived from
        exactly that reading -- a subtler version of the bug A3 is about."""
        age_field(populated, "outsideTemp", MAX_AGE + 60)
        populated.humidity = 95.0          # a live humidity packet arrives...
        populated.calcDewPoint()           # ...which re-stamps dewPoint as fresh
        assert populated.is_fresh("dewPoint", MAX_AGE), "precondition: its own stamp is fresh"

        url = self.build(populated)
        assert "tempf=" not in url
        assert "dewptf=" not in url, "a derived value is only as fresh as its inputs"
        assert "humidity=" in url

    def test_dew_point_dies_with_a_stale_humidity_sensor_too(self, populated):
        age_field(populated, "humidity", MAX_AGE + 60)
        url = self.build(populated)
        assert "humidity=" not in url
        assert "dewptf=" not in url
        assert "tempf=" in url

    def test_daily_rain_survives_an_arbitrarily_long_dry_spell(self, populated):
        """rainToday is untracked, so no amount of elapsed time can drop it. Without this,
        the published daily total would fall back to zero on any dry afternoon."""
        url = self.build(populated)
        assert "dailyrainin=1.82" in url

    def test_a_station_that_never_heard_from_the_iss_sends_no_readings(self, station):
        station.rainToday = 0.0
        url = self.build(station)
        for parameter in ("tempf=", "humidity=", "baromin=", "windspeedmph="):
            assert parameter not in url

    def test_gating_is_purely_subtractive(self, populated):
        """It can only remove a field that would previously have been sent, never add one --
        which is what makes it safe to deploy without integration coverage of the main loop."""
        healthy = self.build(populated)
        age_field(populated, "windGust", MAX_AGE + 60)
        degraded = self.build(populated)
        healthy_params = set(healthy.split("&"))
        assert set(degraded.split("&")) <= healthy_params
