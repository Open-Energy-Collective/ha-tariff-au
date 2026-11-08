"""Tests for the Demand Window Status enum sensor and its shared DemandWindowScheduler."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

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


# --- Pure logic: transition search ------------------------------------------


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
        self.dnsp_time_zone = ZoneInfo("Australia/Melbourne")

    def async_add_listener(self, update_callback, context=None):
        """CoordinatorEntity.async_added_to_hass() needs this to register."""
        return lambda: None


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


# --- Windows resolve in the DNSP's time zone (ha-tariff-au#13) ---------------

MELBOURNE = ZoneInfo("Australia/Melbourne")
BRISBANE = ZoneInfo("Australia/Brisbane")


def make_melbourne_scheduler():
    scheduler = DemandWindowScheduler(
        hass=None, on_transition=lambda is_active: None, time_zone=MELBOURNE
    )
    scheduler._load_window_config(
        {"window_start": "15:00", "window_end": "21:00", "days": "weekdays", "season_months": [12]}
    )
    return scheduler


def test_status_follows_the_dnsp_clock():
    scheduler = make_melbourne_scheduler()
    assert scheduler.status_at(datetime(2026, 12, 9, 14, 30, tzinfo=BRISBANE)) is True
    assert scheduler.status_at(datetime(2026, 12, 9, 20, 30, tzinfo=BRISBANE)) is False


def test_next_transition_is_the_window_start_on_the_dnsp_clock():
    scheduler = make_melbourne_scheduler()
    found = scheduler._find_next_transition(datetime(2026, 12, 9, 12, 0, tzinfo=BRISBANE))
    assert found == datetime(2026, 12, 9, 14, 0, tzinfo=BRISBANE)


async def test_sensor_gives_the_scheduler_the_dnsp_time_zone(hass):
    demand = {"window_start": "15:00", "window_end": "21:00", "days": "all", "season_months": None}
    sensor = OecDemandWindowStatusSensor(FakeCoordinator({"demand": demand}), FakeEntry())
    sensor.hass = hass

    await sensor.async_added_to_hass()
    try:
        assert sensor._scheduler.time_zone == MELBOURNE
    finally:
        await sensor.async_will_remove_from_hass()
