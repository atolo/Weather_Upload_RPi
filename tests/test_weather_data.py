"""Characterisation tests for weatherData_cls (AUDIT_PLAN stage R2).

Run from the repo root:  python3 -m pytest tests/ -q

Written BEFORE adding reading timestamps, to freeze the behaviour that must survive: the dew
point and wind chill formulas, and the 30-point wind direction average. Like the decoder
golden master, these record what the code does today rather than what it should do -- known
defects are pinned with a comment saying so, not fixed.

Nothing here is a specification. The formulas were inherited from the upstream fork and the
station has been publishing their output for months.
"""

import pytest

import weatherData_cls

NO_DATA = weatherData_cls.weatherStation.NO_DATA_YET


@pytest.fixture
def station():
    return weatherData_cls.weatherStation(1)


class TestInitialState:
    def test_every_reading_starts_as_no_data(self, station):
        for field in ("outsideTemp", "humidity", "pressure", "windSpeed", "windGust",
                      "windDir", "rainRate", "rainToday", "dewPoint", "windChill"):
            assert getattr(station, field) == NO_DATA

    def test_got_accessors_are_false_initially(self, station):
        assert not station.gotTemperatureData()
        assert not station.gotHumidityData()
        assert not station.gotPressureData()
        assert not station.gotDewPointData()

    def test_rain_today_uses_a_different_predicate(self, station):
        """gotRainTodayData is `>= 0.0` while every other accessor is `> -100.0`. That is not
        an accident to normalise away: rainToday is legitimately 0.00 all day when it is dry,
        and 0.0 must read as real data, not as absence."""
        station.rainToday = 0.0
        assert station.gotRainTodayData()
        assert not station.gotTemperatureData()


class TestDewPoint:
    def test_typical_value(self, station):
        station.outsideTemp = 74.5
        station.humidity = 94.0
        assert station.calcDewPoint() == pytest.approx(72.66, abs=0.01)

    def test_result_is_stored_on_the_instance(self, station):
        station.outsideTemp = 74.5
        station.humidity = 94.0
        computed = station.calcDewPoint()
        assert station.dewPoint == computed

    def test_saturated_air_gives_dew_point_equal_to_temperature(self, station):
        station.outsideTemp = 60.0
        station.humidity = 100.0
        assert station.calcDewPoint() == pytest.approx(60.0, abs=0.05)

    def test_returns_sentinel_without_both_inputs(self, station):
        station.outsideTemp = 74.5
        assert station.calcDewPoint() == NO_DATA

    def test_zero_humidity_raises(self, station):
        """AUDIT_PLAN item 11, pinned as-is. math.log(0) is a domain error, so 0% RH raises
        rather than returning a sentinel. Latent rather than live: Weather_Station only stores
        humidity when `newHumidity > 0`, so 0 never reaches here from the decoder. Any refactor
        that relaxes that guard makes it live."""
        station.outsideTemp = 74.5
        station.humidity = 0.0
        with pytest.raises(ValueError):
            station.calcDewPoint()


class TestWindChill:
    def test_typical_cold_value(self, station):
        station.outsideTemp = 30.0
        station.windSpeed = 15.0
        assert station.calcWindChill() == pytest.approx(19.027, abs=0.001)

    def test_computed_outside_its_valid_domain(self, station):
        """AUDIT_PLAN item 12, pinned as-is. The NWS formula is defined for T <= 50 F and wind
        > 3 mph; this applies it unconditionally. At 75 F and calm it returns a number warmer
        than the air temperature, which is physically meaningless."""
        station.outsideTemp = 75.0
        station.windSpeed = 0.0
        assert station.calcWindChill() == pytest.approx(82.35, abs=0.01)

    def test_falls_back_to_air_temperature_without_wind(self, station):
        """Note the asymmetry: it returns the sentinel but *assigns* the air temperature."""
        station.outsideTemp = 40.0
        assert station.calcWindChill() == NO_DATA
        assert station.windChill == 40.0


class TestAverageWindDirection:
    POINTS = weatherData_cls.weatherStation.AVG_WIND_DIR_NUM_DATA_POINTS

    def test_returns_sentinel_until_the_buffer_fills(self, station):
        for _ in range(self.POINTS):
            assert station.avgWindDir(90.0) == NO_DATA

    def test_averages_once_full(self, station):
        for _ in range(self.POINTS):
            station.avgWindDir(90.0)
        assert station.avgWindDir(90.0) == 90

    def test_vector_average_handles_the_north_wrap(self, station):
        """350 and 10 degrees average to 0, not 180. This is why it is a vector average."""
        for index in range(self.POINTS):
            station.avgWindDir(350.0 if index % 2 else 10.0)
        assert station.avgWindDir(10.0) in (0, 360, 1, 359)

    def test_counter_grows_without_bound(self, station):
        """AUDIT_PLAN item 24, pinned. self.c keeps incrementing after the buffer is full and
        is never reset; only the `< numPoints` comparison keeps it harmless."""
        for _ in range(self.POINTS + 10):
            station.avgWindDir(90.0)
        assert station.c > self.POINTS

    def test_returns_none_on_the_boundary_call(self, station):
        """Reading the code: at c == numPoints - 1 the first branch runs and returns the
        sentinel, and c becomes numPoints. There is no call that falls through both branches,
        so avgWindDir never implicitly returns None."""
        results = [station.avgWindDir(90.0) for _ in range(self.POINTS + 5)]
        assert all(r is not None for r in results)


class TestGotDewPointData:
    def test_requires_all_three_conditions(self, station):
        station.dewPoint = 70.0
        assert not station.gotDewPointData(), "dew point alone is not enough"
        station.outsideTemp = 74.5
        assert not station.gotDewPointData()
        station.humidity = 94.0
        assert station.gotDewPointData()

    def test_this_is_the_main_loop_upload_gate(self, station):
        """Weather_Station will not upload until this returns True, so its semantics decide
        how long after a restart the station stays silent."""
        station.outsideTemp = 74.5
        station.humidity = 94.0
        station.calcDewPoint()
        assert station.gotDewPointData()
