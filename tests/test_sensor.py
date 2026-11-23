"""Regression tests for HA's device_class/state_class validation.

ha-tariff-au#6: five monetary sensors set state_class MEASUREMENT alongside
device_class MONETARY, which HA's core sensor platform rejects (monetary only
permits None or 'total') -- logged as a WARNING the first time each entity's
.state is read, not raised as an exception, so it's easy to miss outside of
watching live logs. These tests exercise the same code path
(SensorEntity.state, homeassistant/components/sensor/__init__.py) against a
real hass instance and fail if that warning reappears for any sensor.

ha-tariff-au#17: the fix for #6 dropped state_class from the price sensors, so
HA stopped their long-term statistics and raised a repair on entities that
had them. $/kWh, $/day and $/kW/month are prices per unit, not amounts of
money: those four take state_class MEASUREMENT and no device_class. Monthly
Demand Charge ($) is an amount of money: it stays MONETARY and takes TOTAL,
the one state class HA allows with it.
"""

import logging
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.oec_tariff_au.const import (
    CONF_BILLING_DAY,
    CONF_DNSP,
    CONF_POWER_ENTITY,
    CONF_TARIFF,
    DEFAULT_API_URL,
    DOMAIN,
)
from custom_components.oec_tariff_au.demand_manager import DemandManager
from custom_components.oec_tariff_au.demand_sensors import (
    OecDemandSurchargePerKwhSensor,
    OecMonthlyDemandChargeSensor,
    OecMonthPeakDemandSensor,
)
from custom_components.oec_tariff_au.sensor import (
    OecCurrentRateSensor,
    OecDailySupplyChargeSensor,
    OecDemandRateSensor,
    OecDemandWindowStatusSensor,
)
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import N72, NASN12, in_season, windows_body, windows_coordinator

_INVALID_STATE_CLASS_MSG = "is impossible considering device class"


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator: only what the sensors read."""

    def __init__(self, rate=None, detail=None):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = detail or {}
        self.data = {"rate": rate or {}, "detail": detail or {}}


class FakeEntry:
    entry_id = "test_entry"


def make_manager(hass, month_peak_kw=4.5):
    cache = DemandWindowCache(hass, windows_coordinator(N72), "test_entry", 1)
    manager = DemandManager(hass, "sensor.power", cache, N72["demand_charges"], 1)
    next(iter(manager.trackers.values())).month_peak_kw = month_peak_kw
    return manager


def _assert_state_class_valid(hass, caplog, entity, entity_id):
    """Give the entity a real hass + entity_id and read .state, the same way
    HA core does once an entity is added to a platform -- this is what
    actually triggers the state_class/device_class validation warning."""
    entity.hass = hass
    entity.entity_id = entity_id
    with caplog.at_level(logging.WARNING):
        entity.state  # noqa: B018 -- accessing the property is the point
    assert _INVALID_STATE_CLASS_MSG not in caplog.text


def _price_sensors(hass):
    detail = {"daily_supply_charge": 1.10, "demand_charges": N72["demand_charges"]}
    coordinator = FakeCoordinator(rate={"rate": 0.35, "demand_charges": []}, detail=detail)
    entry = FakeEntry()
    return {
        "current_rate": OecCurrentRateSensor(coordinator, entry),
        "daily_supply_charge": OecDailySupplyChargeSensor(coordinator, entry),
        "demand_rate": OecDemandRateSensor(coordinator, entry),
        "demand_surcharge_per_kwh": OecDemandSurchargePerKwhSensor(
            coordinator, entry, make_manager(hass)
        ),
    }


@pytest.mark.parametrize(
    "key", ["current_rate", "daily_supply_charge", "demand_rate", "demand_surcharge_per_kwh"]
)
def test_price_sensor_keeps_long_term_statistics(hass, caplog, key):
    sensor = _price_sensors(hass)[key]
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_class is None
    _assert_state_class_valid(hass, caplog, sensor, f"sensor.{key}")


def test_monthly_demand_charge_stays_monetary_and_keeps_statistics(hass):
    sensor = OecMonthlyDemandChargeSensor(FakeCoordinator(), FakeEntry(), make_manager(hass))
    assert sensor.device_class == SensorDeviceClass.MONETARY
    assert sensor.state_class == SensorStateClass.TOTAL


def test_monthly_demand_charge_sensor_has_valid_state_class(hass, caplog):
    sensor = OecMonthlyDemandChargeSensor(FakeCoordinator(), FakeEntry(), make_manager(hass))
    _assert_state_class_valid(hass, caplog, sensor, "sensor.monthly_demand_charge")


# --- Demand Rate ------------------------------------------------------------


def _in_force(charge, active=False):
    """A current-rate `demand_charges` entry for a tariff detail charge."""
    return {
        "period": charge["period"],
        "rate": charge["rate"],
        "unit": charge["unit"],
        "peak_period": charge["peak_period"],
        "active": active,
    }


def test_demand_rate_follows_in_season_list():
    summer = NASN12["demand_charges"][0]
    detail = {"demand_charges": NASN12["demand_charges"]}

    july = FakeCoordinator(rate={"rate": 0.1, "demand_charges": []}, detail=detail)
    assert OecDemandRateSensor(july, FakeEntry()).native_value == 0.0

    january = FakeCoordinator(
        rate={"rate": 0.1, "demand_charges": [_in_force(summer, active=True)]}, detail=detail
    )
    assert OecDemandRateSensor(january, FakeEntry()).native_value == 11.473


def test_demand_rate_takes_the_highest_in_season_rate():
    low, high = {"rate": 1.89}, {"rate": 10.224}
    coordinator = FakeCoordinator(rate={"demand_charges": [low, high]}, detail={})
    assert OecDemandRateSensor(coordinator, FakeEntry()).native_value == 10.224


def test_demand_rate_none_without_rate_data():
    coordinator = FakeCoordinator()
    coordinator.data = None
    assert OecDemandRateSensor(coordinator, FakeEntry()).native_value is None


def test_demand_rate_attributes_list_every_charge():
    detail = {"demand_charges": NASN12["demand_charges"]}
    coordinator = FakeCoordinator(rate={"demand_charges": []}, detail=detail)
    sensor = OecDemandRateSensor(coordinator, FakeEntry())

    attributes = sensor.extra_state_attributes
    assert list(attributes) == ["charges"]
    assert attributes["charges"][0] == {
        "period": "peak",
        "rate": 11.473,
        "unit": "kW",
        "months": [1, 2, 3, 12],
        "start": "15:00",
        "end": "21:00",
        "days": "weekdays",
        "public_holidays": "included",
        "measurement_method": "monthly_peak",
        "peak_period": "month",
    }
    assert [c["rate"] for c in attributes["charges"]] == [11.473, 2.862]


# --- Unique IDs and setup ---------------------------------------------------


def test_unique_ids_unchanged(hass):
    coordinator = FakeCoordinator(detail={"demand_charges": N72["demand_charges"]})
    entry = FakeEntry()
    manager = make_manager(hass)
    cache = manager.cache
    sensors = [
        OecMonthPeakDemandSensor(coordinator, entry, manager),
        OecMonthlyDemandChargeSensor(coordinator, entry, manager),
        OecDemandSurchargePerKwhSensor(coordinator, entry, manager),
        OecDemandRateSensor(coordinator, entry),
        OecDemandWindowStatusSensor(coordinator, entry, cache),
    ]
    assert [s.unique_id for s in sensors] == [
        "test_entry_month_peak_demand",
        "test_entry_monthly_demand_charge",
        "test_entry_demand_surcharge_per_kwh",
        "test_entry_demand_rate",
        "test_entry_demand_window",
    ]


DNSPS = [{"code": "ausnet", "timezone": "Australia/Melbourne"}]
MELBOURNE = ZoneInfo("Australia/Melbourne")
SETUP_NOW = datetime(2027, 1, 15, 14, 0, tzinfo=MELBOURNE)
WINDOWS_URL = f"{DEFAULT_API_URL}/calculate/demand-windows"
SENSOR_KEYS = (
    "demand_rate",
    "demand_window",
    "month_peak_demand",
    "monthly_demand_charge",
    "demand_surcharge_per_kwh",
)


async def _setup_entry(hass, aioclient_mock, freezer, demand_charges=NASN12["demand_charges"]):
    """Set up a NASN12 entry with a power entity while the demand-windows request fails."""
    freezer.move_to(SETUP_NOW)
    await hass.config.async_set_time_zone("Australia/Melbourne")
    aioclient_mock.get(
        f"{DEFAULT_API_URL}/calculate/current-rate",
        json={"rate": 0.1343, "demand_charges": [{"rate": 11.473}]},
    )
    aioclient_mock.get(f"{DEFAULT_API_URL}/dnsps", json=DNSPS)
    aioclient_mock.get(
        f"{DEFAULT_API_URL}/tariffs/ausnet/NASN12",
        json={"code": "NASN12", "name": "NASN12", "demand_charges": demand_charges},
    )
    aioclient_mock.get(WINDOWS_URL, status=503)
    hass.states.async_set("sensor.power", "1500", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_DNSP: "ausnet",
            CONF_TARIFF: "NASN12",
            CONF_POWER_ENTITY: "sensor.power",
            CONF_BILLING_DAY: 1,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}")
        for key in SENSOR_KEYS
    }
    return entry, ids


async def test_setup_with_no_cover_creates_entities_unavailable(hass, aioclient_mock, freezer):
    entry, ids = await _setup_entry(hass, aioclient_mock, freezer)

    assert all(ids.values()), ids
    assert hass.states.get(ids["demand_window"]).state == "unavailable"
    assert hass.states.get(ids["demand_rate"]).state == "11.473"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_coordinator_update_refreshes_the_windows(hass, aioclient_mock, freezer):
    entry, ids = await _setup_entry(hass, aioclient_mock, freezer)
    month = [date(2027, 1, 1) + timedelta(days=n) for n in range(31)]
    body = windows_body(NASN12["demand_charges"], month, in_season, tz=MELBOURNE)
    aioclient_mock.clear_requests()
    aioclient_mock.get(f"{DEFAULT_API_URL}/calculate/current-rate", json={"rate": 0.1343})
    aioclient_mock.get(WINDOWS_URL, json=body)

    await hass.data[DOMAIN][entry.entry_id]["coordinator"].async_refresh()
    await hass.async_block_till_done()

    assert hass.states.get(ids["demand_window"]).state == "inactive"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_setup_without_demand_charges_creates_no_demand_entities(
    hass, aioclient_mock, freezer
):
    entry, ids = await _setup_entry(hass, aioclient_mock, freezer, demand_charges=[])

    assert ids == dict.fromkeys(SENSOR_KEYS)
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_current_rate")

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_failed_window_refresh_is_logged(hass, aioclient_mock, freezer, caplog):
    entry, _ = await _setup_entry(hass, aioclient_mock, freezer)
    data = hass.data[DOMAIN][entry.entry_id]
    data["window_cache"].async_refresh = AsyncMock(side_effect=RuntimeError("boom"))

    await data["coordinator"].async_refresh()
    await hass.async_block_till_done()

    assert "Demand windows refresh failed" in caplog.text
    assert "boom" in caplog.text

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def test_rate_sensor_unavailable_before_first_answer():
    """last_update_success starts True; with no data yet the rate is not known."""
    coordinator = FakeCoordinator()
    coordinator.data = None
    coordinator.last_update_success = True

    assert OecCurrentRateSensor(coordinator, FakeEntry()).available is False


def test_rate_sensor_available_with_an_answer():
    coordinator = FakeCoordinator(rate={"rate": 0.35})
    coordinator.last_update_success = True

    assert OecCurrentRateSensor(coordinator, FakeEntry()).available is True
