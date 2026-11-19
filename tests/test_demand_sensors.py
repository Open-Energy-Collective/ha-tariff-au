"""Tests for the demand sensors over a DemandManager: values, attributes and lifecycle."""

from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.oec_tariff_au.demand_manager import DemandManager
from custom_components.oec_tariff_au.demand_sensors import (
    OecDemandSurchargePerKwhSensor,
    OecMonthlyDemandChargeSensor,
    OecMonthPeakDemandSensor,
    async_setup_demand_sensors,
)
from custom_components.oec_tariff_au.demand_windows import charge_key
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import EVO_023, LBAD, N72, NASN12, windows_coordinator

POWER = "sensor.power"
ENTRY_ID = "test_entry"
MELBOURNE = ZoneInfo("Australia/Melbourne")


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator: only what the sensors read."""

    def __init__(self, demand_charges):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = {"demand_charges": demand_charges}
        self.data = {"detail": self.tariff_detail, "rate": {}}
        self.dnsp_time_zone = MELBOURNE

    def async_add_listener(self, update_callback, context=None):
        """CoordinatorEntity.async_added_to_hass() needs this to register."""
        return lambda: None


class FakeEntry:
    entry_id = ENTRY_ID

    def __init__(self, data=None):
        self.data = data or {}


class FakeAddEntities:
    def __init__(self):
        self.added = []

    def __call__(self, entities):
        self.added.extend(entities)


async def _manager(hass, tariff, now=None, coordinator=None, charges=None):
    """A manager on `tariff`; its cache is refreshed at `now` when one is given."""
    await hass.config.async_set_time_zone(tariff["timezone"])
    cache = DemandWindowCache(
        hass, coordinator or windows_coordinator(tariff), ENTRY_ID, 1
    )
    if now is not None:
        await cache.async_refresh(now)
    return DemandManager(hass, POWER, cache, charges or tariff["demand_charges"], 1)


def _set_peak(manager, charge, kw, recorded_at=None):
    tracker = manager.trackers[charge_key(charge)]
    tracker.month_peak_kw = kw
    tracker.month_peak_recorded_at = recorded_at
    return tracker


# --- Monthly Demand Charge --------------------------------------------------


async def test_monthly_charge_sums_charges(hass):
    summer = N72["demand_charges"][0]  # rate 5.454
    off_peak = {**EVO_023["demand_charges"][2], "rate": 1.0}
    manager = await _manager(hass, N72, charges=[summer, off_peak])
    _set_peak(manager, summer, 4.0)
    _set_peak(manager, off_peak, 2.0)
    sensor = OecMonthlyDemandChargeSensor(FakeCoordinator([summer, off_peak]), FakeEntry(), manager)

    assert sensor.native_value == 23.82
    assert sensor.extra_state_attributes["charges"] == [
        {"period": "peak", "months": [1, 2, 3, 11, 12], "amount": 21.82},
        {"period": "off_peak", "months": None, "amount": 2.0},
    ]


async def test_monthly_charge_is_zero_with_no_peak(hass):
    manager = await _manager(hass, N72)
    coordinator = FakeCoordinator(N72["demand_charges"])
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), manager)

    assert sensor.native_value == 0.0


async def test_lbad_monthly_charge_keeps_out_of_season_peak_across_reset(hass):
    adelaide = ZoneInfo("Australia/Adelaide")
    manager = await _manager(hass, LBAD, datetime(2027, 3, 31, 18, 0, tzinfo=adelaide))
    peak, anytime = LBAD["demand_charges"]
    _set_peak(manager, peak, 6.0)
    _set_peak(manager, anytime, 2.0)
    manager.cache.async_refresh = AsyncMock()
    sensor = OecMonthlyDemandChargeSensor(FakeCoordinator(LBAD["demand_charges"]), FakeEntry(), manager)

    manager._async_sample(datetime(2027, 4, 1, 0, 0, 30, tzinfo=adelaide))
    await hass.async_block_till_done()

    # Both charges bill on the last 12 months' peak: 6.0 * 10.224 + 2.0 * 1.89.
    assert sensor.native_value == 65.12
    assert sensor.extra_state_attributes["charges"] == [
        {"period": "peak", "months": [1, 2, 3, 11, 12], "amount": 61.34},
        {"period": "anytime", "months": None, "amount": 3.78},
    ]


# --- Demand Surcharge -------------------------------------------------------

SURCHARGE_CHARGE = {
    "period": "peak",
    "rate": 10.0,
    "unit": "kW",
    "start": "16:00",
    "end": "20:00",
    "days": "all",
    "months": None,
    "measurement_method": "30min_max",
    "peak_period": "month",
}
SURCHARGE_TARIFF = {**NASN12, "demand_charges": [SURCHARGE_CHARGE]}


def _first_twenty_days(charge: dict, day: date):
    """Four hours on each of the month's first 20 days: 80 window hours."""
    return (charge["start"], charge["end"]) if day.day <= 20 else None


async def test_surcharge_uses_exact_window_hours(hass, freezer):
    now = datetime(2027, 1, 10, 12, 0, tzinfo=MELBOURNE)
    coordinator = windows_coordinator(SURCHARGE_TARIFF, spans=_first_twenty_days)
    manager = await _manager(hass, SURCHARGE_TARIFF, now, coordinator)
    _set_peak(manager, SURCHARGE_CHARGE, 3.0)
    sensor = OecDemandSurchargePerKwhSensor(
        FakeCoordinator([SURCHARGE_CHARGE]), FakeEntry(), manager
    )

    freezer.move_to(now)
    assert sensor.native_value == 0.125


async def test_surcharge_skips_charges_without_peak(hass, freezer):
    now = datetime(2027, 1, 10, 12, 0, tzinfo=MELBOURNE)
    manager = await _manager(hass, NASN12, now)
    sensor = OecDemandSurchargePerKwhSensor(
        FakeCoordinator(NASN12["demand_charges"]), FakeEntry(), manager
    )

    freezer.move_to(now)
    assert sensor.native_value == 0.0


async def test_surcharge_is_zero_without_cover(hass):
    manager = await _manager(hass, NASN12)
    _set_peak(manager, NASN12["demand_charges"][0], 3.0)
    sensor = OecDemandSurchargePerKwhSensor(
        FakeCoordinator(NASN12["demand_charges"]), FakeEntry(), manager
    )

    assert sensor.native_value == 0.0


# --- Month Peak Demand ------------------------------------------------------


async def test_month_peak_is_highest_chargeable_peak(hass):
    manager = await _manager(hass, N72)
    summer, winter = N72["demand_charges"]
    _set_peak(manager, summer, 3.25, datetime(2027, 1, 12, 17, 0, tzinfo=MELBOURNE))
    _set_peak(manager, winter, 1.5)
    sensor = OecMonthPeakDemandSensor(FakeCoordinator(N72["demand_charges"]), FakeEntry(), manager)

    assert sensor.native_value == 3.25
    attributes = sensor.extra_state_attributes
    assert attributes["demand_recorded_at"] == "2027-01-12T17:00:00+11:00"
    assert attributes["charges"] == [
        {
            "period": "peak",
            "months": [1, 2, 3, 11, 12],
            "unit": "kW",
            "measurement_method": "30min_max",
            "peak_period": "month",
            "peak": 3.25,
            "demand_recorded_at": "2027-01-12T17:00:00+11:00",
        },
        {
            "period": "peak",
            "months": [4, 5, 6, 7, 8, 9, 10],
            "unit": "kW",
            "measurement_method": "30min_max",
            "peak_period": "month",
            "peak": 1.5,
            "demand_recorded_at": None,
        },
    ]
    assert attributes["billing_day"] == 1
    assert attributes["power_entity"] == POWER
    assert attributes["last_reset"] is None
    for dropped in ("demand_window", "demand_days", "season_months", "measurement_method"):
        assert dropped not in attributes


async def test_kva_charge_marked_measured_as_kw(hass):
    manager = await _manager(hass, LBAD)
    sensor = OecMonthPeakDemandSensor(FakeCoordinator(LBAD["demand_charges"]), FakeEntry(), manager)

    charges = sensor.extra_state_attributes["charges"]
    assert [c["unit"] for c in charges] == ["kVA", "kVA"]
    assert all(c["measured_as"] == "kW" for c in charges)


async def test_kw_charge_has_no_measured_as(hass):
    manager = await _manager(hass, N72)
    sensor = OecMonthPeakDemandSensor(FakeCoordinator(N72["demand_charges"]), FakeEntry(), manager)

    assert all("measured_as" not in c for c in sensor.extra_state_attributes["charges"])


async def _added_month_peak(hass, manager):
    sensor = OecMonthPeakDemandSensor(
        FakeCoordinator(N72["demand_charges"]), FakeEntry(), manager
    )
    sensor.hass = hass
    sensor.entity_id = "sensor.month_peak_demand"
    await sensor.async_added_to_hass()
    return sensor


async def test_month_peak_sensor_starts_manager_with_no_prior_state(hass):
    manager = await _manager(hass, N72)
    sensor = await _added_month_peak(hass, manager)
    try:
        assert sensor.native_value == 0.0
        assert manager._unsub_timer is not None
    finally:
        await sensor.async_will_remove_from_hass()


async def test_month_peak_sensor_restores_a_1_2_blob_onto_the_first_charge(hass):
    saved_data = {
        "month_peak_kw": 6.75,
        "previous_month_peak_kw": 5.5,
        "monthly_peaks_12": [5.5, 6.0],
        "last_reset": "2027-01-01T00:00:00+11:00",
    }
    mock_restore_cache_with_extra_data(
        hass, [(State("sensor.month_peak_demand", "6.75"), saved_data)]
    )
    manager = await _manager(hass, N72)
    sensor = await _added_month_peak(hass, manager)
    try:
        first, second = (manager.trackers[charge_key(c)] for c in N72["demand_charges"])
        assert first.month_peak_kw == 6.75
        assert first.monthly_peaks_12 == [(5.5, None), (6.0, None)]
        assert second.month_peak_kw == 0.0
        assert manager.last_reset == datetime.fromisoformat("2027-01-01T00:00:00+11:00")
    finally:
        await sensor.async_will_remove_from_hass()


async def test_month_peak_sensor_restores_before_starting(hass):
    order = []
    manager = await _manager(hass, N72)
    manager.restore_state = lambda data: order.append("restore")
    manager.start = lambda: order.append("start")
    manager.stop = lambda: None
    mock_restore_cache_with_extra_data(
        hass, [(State("sensor.month_peak_demand", "1.0"), {"month_peak_kw": 1.0})]
    )

    sensor = await _added_month_peak(hass, manager)
    await sensor.async_will_remove_from_hass()

    assert order == ["restore", "start"]


async def test_month_peak_sensor_stops_manager_on_removal(hass):
    manager = await _manager(hass, N72)
    sensor = await _added_month_peak(hass, manager)
    assert manager._unsub_timer is not None

    await sensor.async_will_remove_from_hass()
    assert manager._unsub_timer is None


async def test_month_peak_extra_restore_state_data_is_manager_state(hass):
    manager = await _manager(hass, N72)
    _set_peak(manager, N72["demand_charges"][0], 2.0)
    sensor = OecMonthPeakDemandSensor(FakeCoordinator(N72["demand_charges"]), FakeEntry(), manager)

    assert sensor.extra_restore_state_data.as_dict() == manager.save_state()


# --- async_setup_demand_sensors ---------------------------------------------


def _cache(hass):
    return DemandWindowCache(hass, windows_coordinator(N72), ENTRY_ID, 1)


async def test_setup_skips_without_power_entity(hass):
    add_entities = FakeAddEntities()

    manager = await async_setup_demand_sensors(
        hass, FakeEntry({}), FakeCoordinator(N72["demand_charges"]), _cache(hass), add_entities
    )

    assert manager is None
    assert add_entities.added == []


async def test_setup_skips_without_demand_charges(hass):
    add_entities = FakeAddEntities()

    manager = await async_setup_demand_sensors(
        hass,
        FakeEntry({"power_entity": POWER}),
        FakeCoordinator([]),
        _cache(hass),
        add_entities,
    )

    assert manager is None
    assert add_entities.added == []


async def test_setup_builds_manager_and_three_entities(hass):
    add_entities = FakeAddEntities()
    cache = _cache(hass)

    manager = await async_setup_demand_sensors(
        hass,
        FakeEntry({"power_entity": POWER, "billing_day": 5}),
        FakeCoordinator(N72["demand_charges"]),
        cache,
        add_entities,
    )

    assert isinstance(manager, DemandManager)
    assert manager.power_entity_id == POWER
    assert manager.billing_day == 5
    assert manager.cache is cache
    assert list(manager.trackers) == [charge_key(c) for c in N72["demand_charges"]]
    assert [type(e) for e in add_entities.added] == [
        OecMonthPeakDemandSensor,
        OecMonthlyDemandChargeSensor,
        OecDemandSurchargePerKwhSensor,
    ]


async def test_setup_defaults_billing_day_when_unset(hass):
    manager = await async_setup_demand_sensors(
        hass,
        FakeEntry({"power_entity": POWER}),
        FakeCoordinator(N72["demand_charges"]),
        _cache(hass),
        FakeAddEntities(),
    )

    assert manager.billing_day == 1


async def test_setup_manager_reads_the_latest_tariff_detail(hass):
    coordinator = FakeCoordinator(N72["demand_charges"])
    manager = await async_setup_demand_sensors(
        hass, FakeEntry({"power_entity": POWER}), coordinator, _cache(hass), FakeAddEntities()
    )

    coordinator.tariff_detail = {"demand_charges": N72["demand_charges"][:1]}

    assert manager.detail_charges() == N72["demand_charges"][:1]
