"""Tests for the demand window pure-logic helper in demand_tracker.py."""

from datetime import datetime, time, timedelta, timezone

import pytest

from custom_components.oec_tariff_au import demand_tracker as demand_tracker_module
from custom_components.oec_tariff_au.demand_tracker import DemandTracker, is_in_demand_window

AEST = timezone(timedelta(hours=10))
UTC = timezone.utc


def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=AEST)


@pytest.fixture(autouse=True)
def _treat_aest_as_local(monkeypatch):
    """Every `dt()` helper in this file produces an AEST-aware datetime, so
    treat AEST as "local" for dt_util.as_local() -- this is the same fix as
    the as_local regression tests above, applied file-wide so every new
    _async_sample() call below doesn't need to repeat it. A no-op for
    datetimes already in AEST (all of them, via dt())."""
    monkeypatch.setattr(demand_tracker_module.dt_util, "as_local", lambda d: d.astimezone(AEST))


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
    def __init__(self, value, unit="kW"):
        self._state = _FakeState(value, unit)

    def get(self, _entity_id):
        return self._state


class _FakeHass:
    def __init__(self, value, unit="kW"):
        self.states = _FakeStates(value, unit)


def test_async_sample_uses_local_time_not_utc(monkeypatch):
    """Regression test: async_track_time_interval fires with UTC `now`.

    Tariff 3900's demand window is 16:00-20:00 AEST. 2026-07-22 06:30 UTC is
    2026-07-22 16:30 AEST -- inside the window -- but its raw UTC clock time
    (06:30) falls outside 16:00-20:00. Before the as_local() fix, the sample
    was silently dropped instead of updating the peak.
    """
    from custom_components.oec_tariff_au import demand_tracker as demand_tracker_module

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
    from custom_components.oec_tariff_au import demand_tracker as demand_tracker_module

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


# --- _async_sample: unit conversion, bad states, block boundaries -----------


def make_tracker(hass, measurement_method="30min_avg"):
    tracker = DemandTracker(
        hass=hass,
        power_entity_id="sensor.power",
        measurement_method=measurement_method,
        window_start="16:00",
        window_end="21:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.last_reset = dt(2026, 7, 1, 0, 0)
    return tracker


def test_async_sample_converts_watts_to_kw():
    hass = _FakeHass("2500", unit="W")
    tracker = make_tracker(hass)
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.current_block_samples == [2.5]


def test_async_sample_ignores_unknown_state():
    hass = _FakeHass("unknown")
    tracker = make_tracker(hass)
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.current_block_samples == []


def test_async_sample_ignores_unavailable_state():
    hass = _FakeHass("unavailable")
    tracker = make_tracker(hass)
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.current_block_samples == []


def test_async_sample_ignores_non_numeric_state():
    hass = _FakeHass("not_a_number")
    tracker = make_tracker(hass)
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.current_block_samples == []


def test_async_sample_monthly_peak_updates_directly_without_blocking():
    """monthly_peak bypasses block buffering -- month_peak_kw should update
    immediately, and current_block_samples should stay empty."""
    hass = _FakeHass("3.0")
    tracker = make_tracker(hass, measurement_method="monthly_peak")
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.month_peak_kw == 3.0
    assert tracker.current_block_samples == []

    hass.states._state.state = "2.0"  # lower reading should not lower the peak
    tracker._async_sample(dt(2026, 7, 22, 17, 5))
    assert tracker.month_peak_kw == 3.0


def test_async_sample_processes_previous_block_on_boundary_crossing():
    """30min_avg: two samples in the same 30-min block average together;
    a sample in the next block flushes that average into month_peak_kw
    before starting a fresh block."""
    hass = _FakeHass("4.0")
    tracker = make_tracker(hass, measurement_method="30min_avg")

    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    hass.states._state.state = "6.0"
    tracker._async_sample(dt(2026, 7, 22, 17, 10))
    # Still same block (17:00-17:30) -- not flushed yet.
    assert tracker.month_peak_kw == 0.0
    assert tracker.current_block_samples == [4.0, 6.0]

    hass.states._state.state = "1.0"
    tracker._async_sample(dt(2026, 7, 22, 17, 31))
    # New block started -> previous block's average (5.0) is flushed.
    assert tracker.month_peak_kw == 5.0
    assert tracker.current_block_samples == [1.0]


def test_async_sample_leaving_window_flushes_in_progress_block():
    hass = _FakeHass("8.0")
    tracker = make_tracker(hass, measurement_method="30min_max")
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.current_block_samples == [8.0]

    # 22:00 is outside the 16:00-21:00 window.
    tracker._async_sample(dt(2026, 7, 22, 22, 0))
    assert tracker.month_peak_kw == 8.0
    assert tracker.current_block_samples == []
    assert tracker.current_block_start is None


# --- Billing month reset ------------------------------------------------


def test_first_sample_ever_triggers_initial_reset():
    """last_reset starts as None -- the very first sample must not crash and
    must establish a baseline instead of treating it as a rollover."""
    hass = _FakeHass("5.0")
    tracker = DemandTracker(
        hass=hass,
        power_entity_id="sensor.power",
        measurement_method="monthly_peak",
        window_start="16:00",
        window_end="21:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    assert tracker.last_reset is None
    tracker._async_sample(dt(2026, 7, 22, 17, 0))
    assert tracker.last_reset is not None
    assert tracker.month_peak_kw == 5.0


def test_crossing_billing_day_resets_month_peak_and_records_previous():
    hass = _FakeHass("9.0")
    tracker = make_tracker(hass, measurement_method="monthly_peak")
    tracker.last_reset = dt(2026, 7, 1, 0, 0)
    tracker.month_peak_kw = 12.0

    # billing_day=1: 2026-08-01 is a new cycle relative to last_reset in July.
    tracker._async_sample(dt(2026, 8, 1, 17, 0))

    assert tracker.previous_month_peak_kw == 12.0
    assert tracker.monthly_peaks_12 == [12.0]
    assert tracker.month_peak_kw == 9.0  # this sample's value, post-reset


def test_same_billing_cycle_does_not_reset():
    hass = _FakeHass("9.0")
    tracker = make_tracker(hass, measurement_method="monthly_peak")
    tracker.last_reset = dt(2026, 7, 1, 0, 0)
    tracker.month_peak_kw = 12.0

    tracker._async_sample(dt(2026, 7, 15, 17, 0))

    assert tracker.previous_month_peak_kw == 0.0
    assert tracker.month_peak_kw == 12.0  # 9.0 < 12.0, peak unchanged


def test_monthly_peaks_12_caps_at_twelve_entries():
    hass = _FakeHass("1.0")
    tracker = make_tracker(hass, measurement_method="monthly_peak")
    tracker.monthly_peaks_12 = list(range(12))
    tracker.last_reset = dt(2026, 7, 1, 0, 0)

    tracker._async_sample(dt(2026, 8, 1, 17, 0))

    assert len(tracker.monthly_peaks_12) == 12
    assert tracker.monthly_peaks_12 == list(range(1, 12)) + [0.0]


# --- chargeable_demand / restore-save round trip -----------------------


def test_chargeable_demand_rolling_12month_max_includes_current_month():
    tracker = make_tracker(_FakeHass("0"), measurement_method="rolling_12month_max")
    tracker.monthly_peaks_12 = [3.0, 7.0, 2.0]
    tracker.month_peak_kw = 5.0
    assert tracker.chargeable_demand == 7.0


def test_chargeable_demand_rolling_12month_max_current_month_is_new_high():
    tracker = make_tracker(_FakeHass("0"), measurement_method="rolling_12month_max")
    tracker.monthly_peaks_12 = [3.0, 7.0, 2.0]
    tracker.month_peak_kw = 9.0
    assert tracker.chargeable_demand == 9.0


def test_chargeable_demand_non_rolling_method_ignores_history():
    tracker = make_tracker(_FakeHass("0"), measurement_method="30min_max")
    tracker.monthly_peaks_12 = [99.0]
    tracker.month_peak_kw = 4.0
    assert tracker.chargeable_demand == 4.0


def test_restore_state_and_save_state_round_trip():
    tracker = make_tracker(_FakeHass("0"))
    tracker.month_peak_kw = 6.75
    tracker.previous_month_peak_kw = 5.5
    tracker.monthly_peaks_12 = [5.5, 6.0]
    tracker.last_reset = dt(2026, 7, 1, 0, 0)

    saved = tracker.save_state()

    restored = make_tracker(_FakeHass("0"))
    restored.restore_state(saved)

    assert restored.month_peak_kw == 6.75
    assert restored.previous_month_peak_kw == 5.5
    assert restored.monthly_peaks_12 == [5.5, 6.0]
    assert restored.last_reset == dt(2026, 7, 1, 0, 0)


def test_restore_state_defaults_when_data_missing_keys():
    """A fresh install / first-ever restore has no prior save_state() blob --
    restore_state() must not KeyError on an empty dict, and must leave
    last_reset as None so the next sample is treated as the initial reset."""
    tracker = DemandTracker(
        hass=_FakeHass("0"),
        power_entity_id="sensor.power",
        measurement_method="monthly_peak",
        window_start="16:00",
        window_end="21:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.restore_state({})
    assert tracker.month_peak_kw == 0.0
    assert tracker.previous_month_peak_kw == 0.0
    assert tracker.monthly_peaks_12 == []
    assert tracker.last_reset is None
