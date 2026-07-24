"""Tests for the Demand Window binary sensor."""

from datetime import datetime, timedelta, timezone

from custom_components.oec_tariff_au.binary_sensor import OecInDemandWindowSensor

AEST = timezone(timedelta(hours=10))


def dt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=AEST)


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator — only what the sensor reads."""

    def __init__(self, tariff_detail):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = tariff_detail
        self.data = None


class FakeEntry:
    entry_id = "test_entry"


def make_sensor(window_start, window_end, days="all", season_months=None):
    detail = {
        "demand": {
            "window_start": window_start,
            "window_end": window_end,
            "days": days,
            "season_months": season_months,
        }
    }
    sensor = OecInDemandWindowSensor(FakeCoordinator(detail), FakeEntry())
    sensor._load_window_config()
    return sensor


# --- Pure logic: transition search -----------------------------------------


def test_finds_window_start_today():
    sensor = make_sensor("16:00", "21:00")
    transition = sensor._find_next_transition(dt(2026, 7, 22, 15, 0))
    assert transition == dt(2026, 7, 22, 16, 0)


def test_finds_window_end_today():
    sensor = make_sensor("16:00", "21:00")
    transition = sensor._find_next_transition(dt(2026, 7, 22, 18, 0))
    assert transition == dt(2026, 7, 22, 21, 0)


def test_finds_next_day_start_when_after_window():
    sensor = make_sensor("16:00", "21:00")
    transition = sensor._find_next_transition(dt(2026, 7, 22, 22, 0))
    assert transition == dt(2026, 7, 23, 16, 0)


def test_weekdays_skips_weekend_to_monday():
    sensor = make_sensor("16:00", "21:00", days="weekdays")
    # Friday 2026-07-24, after window closes -> next start is Monday 2026-07-27
    transition = sensor._find_next_transition(dt(2026, 7, 24, 22, 0))
    assert transition == dt(2026, 7, 27, 16, 0)


def test_overnight_window_transition_at_end():
    sensor = make_sensor("22:00", "06:00")
    transition = sensor._find_next_transition(dt(2026, 7, 22, 23, 0))
    assert transition == dt(2026, 7, 23, 6, 0)


def test_status_at_matches_window_config():
    sensor = make_sensor("16:00", "21:00")
    assert sensor._status_at(dt(2026, 7, 22, 17, 0)) is True
    assert sensor._status_at(dt(2026, 7, 22, 22, 0)) is False


def test_no_demand_config_returns_false_from_load():
    sensor = OecInDemandWindowSensor(FakeCoordinator(None), FakeEntry())
    assert sensor._load_window_config() is False
