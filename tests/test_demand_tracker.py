"""Tests for the demand window pure-logic helper in demand_tracker.py."""

from datetime import datetime, time, timedelta, timezone

from custom_components.oec_tariff.demand_tracker import is_in_demand_window

AEST = timezone(timedelta(hours=10))


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
