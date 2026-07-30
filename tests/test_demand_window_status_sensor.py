"""Tests for the Demand Window Status enum sensor and its shared DemandWindowScheduler."""

from datetime import datetime, timedelta, timezone

from custom_components.oec_tariff_au.demand_tracker import DemandWindowScheduler
from custom_components.oec_tariff_au.sensor import OecDemandWindowStatusSensor

AEST = timezone(timedelta(hours=10))


def dt(y, m, d, hh, mm, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=AEST)


def make_scheduler(window_start, window_end, days="all", season_months=None):
    scheduler = DemandWindowScheduler(hass=None, on_transition=lambda is_active: None)
    scheduler._load_window_config(
        {
            "window_start": window_start,
            "window_end": window_end,
            "days": days,
            "season_months": season_months,
        }
    )
    return scheduler


# --- Pure logic: transition search (shared with the binary sensor) ---------


def test_finds_window_start_today():
    scheduler = make_scheduler("16:00", "21:00")
    transition = scheduler._find_next_transition(dt(2026, 7, 22, 15, 0))
    assert transition == dt(2026, 7, 22, 16, 0)


def test_finds_window_end_today():
    scheduler = make_scheduler("16:00", "21:00")
    transition = scheduler._find_next_transition(dt(2026, 7, 22, 18, 0))
    assert transition == dt(2026, 7, 22, 21, 0)


def test_status_at_matches_window_config():
    scheduler = make_scheduler("16:00", "21:00")
    assert scheduler.status_at(dt(2026, 7, 22, 17, 0)) is True
    assert scheduler.status_at(dt(2026, 7, 22, 22, 0)) is False


def test_no_demand_config_returns_false_from_load():
    scheduler = DemandWindowScheduler(hass=None, on_transition=lambda is_active: None)
    assert scheduler._load_window_config(None) is False


# --- Enum mapping ------------------------------------------------------------


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator — only what the sensor reads."""

    def __init__(self, tariff_detail):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = tariff_detail
        self.data = None


class FakeEntry:
    entry_id = "test_entry"


def test_options_are_active_inactive():
    sensor = OecDemandWindowStatusSensor(FakeCoordinator({"demand": None}), FakeEntry())
    assert sensor.options == ["active", "inactive"]


def test_native_value_before_scheduler_starts_is_none():
    sensor = OecDemandWindowStatusSensor(FakeCoordinator({"demand": None}), FakeEntry())
    assert sensor.native_value is None


def test_native_value_reflects_scheduler_status():
    sensor = OecDemandWindowStatusSensor(FakeCoordinator({"demand": None}), FakeEntry())
    sensor._scheduler = make_scheduler("16:00", "21:00")

    sensor._scheduler.is_active = True
    assert sensor.native_value == "active"

    sensor._scheduler.is_active = False
    assert sensor.native_value == "inactive"
