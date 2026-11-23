"""Tests for the Demand Window enum sensor over the cached per-charge windows."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.oec_tariff_au.coordinator import DemandWindowsUnavailable
from custom_components.oec_tariff_au.sensor import OecDemandWindowStatusSensor
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import NASN12, windows_coordinator

MELBOURNE = ZoneInfo("Australia/Melbourne")
ENTRY_ID = "test_entry"
# Friday 15 January 2027: NASN12's summer peak runs 15:00 to 21:00 on weekdays.
BEFORE_WINDOW = datetime(2027, 1, 15, 14, 0, tzinfo=MELBOURNE)
WINDOW_START = datetime(2027, 1, 15, 15, 0, tzinfo=MELBOURNE)
WINDOW_END = datetime(2027, 1, 15, 21, 0, tzinfo=MELBOURNE)


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator: only what the sensor reads."""

    def __init__(self):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = {"demand_charges": NASN12["demand_charges"]}
        self.data = None
        self.last_update_success = True

    def async_add_listener(self, update_callback, context=None):
        """CoordinatorEntity.async_added_to_hass() needs this to register."""
        return lambda: None


class FakeEntry:
    entry_id = ENTRY_ID


async def _cache(hass, now=None):
    await hass.config.async_set_time_zone("Australia/Melbourne")
    cache = DemandWindowCache(hass, windows_coordinator(NASN12), ENTRY_ID, 1)
    if now is not None:
        await cache.async_refresh(now)
    return cache


async def _added_sensor(hass, cache):
    """The sensor added to hass, recording its value at each state write."""
    sensor = OecDemandWindowStatusSensor(FakeCoordinator(), FakeEntry(), cache)
    sensor.hass = hass
    sensor.entity_id = "sensor.demand_window"
    sensor.writes = []
    sensor.availability = []
    sensor.async_write_ha_state = lambda: (
        sensor.writes.append((sensor.native_value, sensor.extra_state_attributes)),
        sensor.availability.append(sensor.available),
    )
    await sensor.async_added_to_hass()
    return sensor


async def test_options_are_active_inactive(hass):
    sensor = OecDemandWindowStatusSensor(FakeCoordinator(), FakeEntry(), await _cache(hass))
    assert sensor.options == ["active", "inactive"]


async def test_demand_window_schedules_from_cache_bounds(hass, freezer):
    freezer.move_to(BEFORE_WINDOW)
    cache = await _cache(hass, BEFORE_WINDOW)
    sensor = await _added_sensor(hass, cache)
    try:
        assert sensor.native_value == "inactive"
        assert sensor.extra_state_attributes == {"active_charges": []}

        freezer.move_to(WINDOW_START)
        async_fire_time_changed(hass, WINDOW_START)
        await hass.async_block_till_done()

        assert sensor.writes[-1] == ("active", {"active_charges": ["peak"]})

        freezer.move_to(WINDOW_END)
        async_fire_time_changed(hass, WINDOW_END)
        await hass.async_block_till_done()

        assert sensor.writes[-1] == ("inactive", {"active_charges": []})
    finally:
        await sensor.async_will_remove_from_hass()


async def test_demand_window_reschedules_on_cache_change(hass, freezer):
    freezer.move_to(BEFORE_WINDOW)
    cache = await _cache(hass)
    sensor = await _added_sensor(hass, cache)
    try:
        assert sensor.native_value is None

        await cache.async_refresh(BEFORE_WINDOW)
        assert sensor.writes[-1][0] == "inactive"

        freezer.move_to(WINDOW_START)
        async_fire_time_changed(hass, WINDOW_START)
        await hass.async_block_till_done()

        assert sensor.writes[-1][0] == "active"
    finally:
        await sensor.async_will_remove_from_hass()


async def test_demand_window_unavailable_without_cover(hass, freezer):
    freezer.move_to(BEFORE_WINDOW)
    sensor = await _added_sensor(hass, await _cache(hass))
    try:
        assert sensor.available is False
        assert sensor.native_value is None
        assert sensor.extra_state_attributes == {"active_charges": []}
    finally:
        await sensor.async_will_remove_from_hass()


async def test_removal_cancels_the_pending_transition(hass, freezer):
    freezer.move_to(BEFORE_WINDOW)
    sensor = await _added_sensor(hass, await _cache(hass, BEFORE_WINDOW))
    await sensor.async_will_remove_from_hass()
    writes = len(sensor.writes)

    freezer.move_to(WINDOW_START + timedelta(minutes=1))
    async_fire_time_changed(hass, WINDOW_START + timedelta(minutes=1))
    await hass.async_block_till_done()

    assert len(sensor.writes) == writes


async def test_demand_window_unavailable_when_cover_runs_out(hass, freezer):
    """January is cached, February is not: the state goes unavailable at 1 February."""
    late = datetime(2027, 1, 31, 22, 0, tzinfo=MELBOURNE)
    month_end = datetime(2027, 2, 1, tzinfo=MELBOURNE)
    january = windows_coordinator(NASN12)
    fetch = january.async_fetch_demand_windows.side_effect

    def january_only(start, end):
        if start.month != 1:
            raise DemandWindowsUnavailable("unavailable", 503)
        return fetch(start, end)

    coordinator = SimpleNamespace(
        async_fetch_demand_windows=AsyncMock(side_effect=january_only),
        dnsp_time_zone=MELBOURNE,
    )
    await hass.config.async_set_time_zone("Australia/Melbourne")
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)
    freezer.move_to(late)
    await cache.async_refresh(late)
    sensor = await _added_sensor(hass, cache)
    try:
        assert sensor.available is True
        assert sensor.native_value == "inactive"

        freezer.move_to(month_end)
        async_fire_time_changed(hass, month_end)
        await hass.async_block_till_done()

        assert sensor.availability[-1] is False
        assert sensor.writes[-1] == (None, {"active_charges": []})
    finally:
        await sensor.async_will_remove_from_hass()


class _OverlapCache:
    """Two charges whose windows overlap: peak 15:00 to 17:00, shoulder 16:00 to 18:00."""

    def __init__(self):
        from custom_components.oec_tariff_au.demand_windows import ChargeWindows

        def at(hour):
            return datetime(2027, 1, 15, hour, 0, tzinfo=MELBOURNE)

        self._charges = {
            ("peak", None): ChargeWindows({"period": "peak"}, [(at(15), at(17))]),
            ("shoulder", None): ChargeWindows({"period": "shoulder"}, [(at(16), at(18))]),
        }

    def charges(self, when):
        return self._charges

    def covers(self, when):
        return True

    def month_end(self, when):
        return datetime(2027, 2, 1, tzinfo=MELBOURNE)

    def add_listener(self, callback):
        return lambda: None


async def test_overlapping_charges_write_at_every_window_bound(hass, freezer):
    """active_charges changes at 16:00 and 17:00 although the state stays active."""
    await hass.config.async_set_time_zone("Australia/Melbourne")
    freezer.move_to(BEFORE_WINDOW)
    sensor = await _added_sensor(hass, _OverlapCache())
    try:
        expected = [
            (15, ("active", {"active_charges": ["peak"]})),
            (16, ("active", {"active_charges": ["peak", "shoulder"]})),
            (17, ("active", {"active_charges": ["shoulder"]})),
            (18, ("inactive", {"active_charges": []})),
        ]
        for hour, write in expected:
            when = datetime(2027, 1, 15, hour, 0, tzinfo=MELBOURNE)
            freezer.move_to(when)
            async_fire_time_changed(hass, when)
            await hass.async_block_till_done()
            assert sensor.writes[-1] == write, hour
    finally:
        await sensor.async_will_remove_from_hass()
