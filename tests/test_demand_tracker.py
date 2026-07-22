"""Tests for the demand window pure-logic helper in demand_tracker.py."""

from datetime import datetime, time, timedelta, timezone

from custom_components.oec_tariff.demand_tracker import DemandTracker, is_in_demand_window

AEST = timezone(timedelta(hours=10))
UTC = timezone.utc


def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=AEST)


def test_simple_daytime_window_inside():
    assert is_in_demand_window(dt(2026, 7, 22, 16, 30), time(16, 0), time(21, 0), "all", None)


def test_simple_daytime_window_before():
    assert not is_in_demand_window(dt(2026, 7, 22, 15, 59), time(16, 0), time(21, 0), "all", None)


def test_simple_daytime_window_at_start_inclusive():
    assert is_in_demand_window(dt(2026, 7, 22, 16, 0), time(16, 0), time(21, 0), "all", None)


def test_simple_daytime_window_at_end_exclusive():
    assert not is_in_demand_window(dt(2026, 7, 22, 21, 0), time(16, 0), time(21, 0), "all", None)


def test_weekdays_only_excludes_saturday():
    # 2026-07-25 is a Saturday
    assert not is_in_demand_window(
        dt(2026, 7, 25, 16, 30), time(16, 0), time(21, 0), "weekdays", None
    )


def test_weekdays_only_includes_friday():
    # 2026-07-24 is a Friday
    assert is_in_demand_window(
        dt(2026, 7, 24, 16, 30), time(16, 0), time(21, 0), "weekdays", None
    )


def test_weekends_only_includes_saturday():
    assert is_in_demand_window(
        dt(2026, 7, 25, 16, 30), time(16, 0), time(21, 0), "weekends", None
    )


def test_season_months_excludes_out_of_season():
    assert not is_in_demand_window(
        dt(2026, 7, 22, 16, 30), time(16, 0), time(21, 0), "all", [12, 1, 2]
    )


def test_season_months_includes_in_season():
    assert is_in_demand_window(
        dt(2026, 1, 22, 16, 30), time(16, 0), time(21, 0), "all", [12, 1, 2]
    )


def test_overnight_window_before_midnight():
    assert is_in_demand_window(dt(2026, 7, 22, 23, 0), time(22, 0), time(6, 0), "all", None)


def test_overnight_window_after_midnight():
    assert is_in_demand_window(dt(2026, 7, 23, 3, 0), time(22, 0), time(6, 0), "all", None)


def test_overnight_window_end_exclusive():
    assert not is_in_demand_window(dt(2026, 7, 23, 6, 0), time(22, 0), time(6, 0), "all", None)


def test_always_on_window():
    assert is_in_demand_window(dt(2026, 7, 22, 3, 17), time(0, 0), time(0, 0), "all", None)


class _FakeState:
    def __init__(self, value, unit="kW"):
        self.state = value
        self.attributes = {"unit_of_measurement": unit}


class _FakeStates:
    def __init__(self, value):
        self._state = _FakeState(value)

    def get(self, _entity_id):
        return self._state


class _FakeHass:
    def __init__(self, value):
        self.states = _FakeStates(value)


def test_async_sample_uses_local_time_not_utc(monkeypatch):
    """Regression test: async_track_time_interval fires with UTC `now`.

    Tariff 3900's demand window is 16:00-20:00 AEST. 2026-07-22 06:30 UTC is
    2026-07-22 16:30 AEST -- inside the window -- but its raw UTC clock time
    (06:30) falls outside 16:00-20:00. Before the as_local() fix, the sample
    was silently dropped instead of updating the peak.
    """
    from custom_components.oec_tariff import demand_tracker as demand_tracker_module

    monkeypatch.setattr(demand_tracker_module.dt_util, "as_local", lambda d: d.astimezone(AEST))

    hass = _FakeHass("5.0")
    tracker = DemandTracker(
        hass=hass,
        power_entity_id="sensor.power",
        measurement_method="monthly_peak",
        window_start="16:00",
        window_end="20:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.last_reset = datetime(2026, 7, 1, tzinfo=AEST)

    utc_now = datetime(2026, 7, 22, 6, 30, tzinfo=UTC)  # 16:30 AEST
    tracker._async_sample(utc_now)

    assert tracker.month_peak_kw == 5.0


def test_async_sample_out_of_window_by_local_clock_is_ignored(monkeypatch):
    """Same instant, but outside the window once correctly localized."""
    from custom_components.oec_tariff import demand_tracker as demand_tracker_module

    monkeypatch.setattr(demand_tracker_module.dt_util, "as_local", lambda d: d.astimezone(AEST))

    hass = _FakeHass("5.0")
    tracker = DemandTracker(
        hass=hass,
        power_entity_id="sensor.power",
        measurement_method="monthly_peak",
        window_start="16:00",
        window_end="20:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.last_reset = datetime(2026, 7, 1, tzinfo=AEST)

    utc_now = datetime(2026, 7, 22, 16, 30, tzinfo=UTC)  # 02:30 AEST next-ish, early morning
    tracker._async_sample(utc_now)

    assert tracker.month_peak_kw == 0.0
