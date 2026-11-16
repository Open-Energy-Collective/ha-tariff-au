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

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass

from custom_components.oec_tariff_au.demand_sensors import (
    OecDemandSurchargePerKwhSensor,
    OecMonthlyDemandChargeSensor,
)
from custom_components.oec_tariff_au.demand_tracker import DemandTracker
from custom_components.oec_tariff_au.sensor import (
    OecCurrentRateSensor,
    OecDailySupplyChargeSensor,
    OecDemandRateSensor,
)

_INVALID_STATE_CLASS_MSG = "is impossible considering device class"


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator -- only what the sensors read."""

    def __init__(self, rate=None, detail=None):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = detail or {}
        self.data = {"rate": rate or {}, "detail": detail or {}}


class FakeEntry:
    entry_id = "test_entry"


def make_tracker(month_peak_kw=4.5):
    tracker = DemandTracker(
        hass=None,
        power_entity_id="sensor.power",
        measurement_method="30min_max",
        window_start="16:00",
        window_end="21:00",
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.month_peak_kw = month_peak_kw
    return tracker


def _assert_state_class_valid(hass, caplog, entity, entity_id):
    """Give the entity a real hass + entity_id and read .state, the same way
    HA core does once an entity is added to a platform -- this is what
    actually triggers the state_class/device_class validation warning."""
    entity.hass = hass
    entity.entity_id = entity_id
    with caplog.at_level(logging.WARNING):
        entity.state  # noqa: B018 -- accessing the property is the point
    assert _INVALID_STATE_CLASS_MSG not in caplog.text


def _price_sensors():
    detail = {
        "daily_supply_charge": 1.10,
        "demand": {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"},
    }
    coordinator = FakeCoordinator(rate={"rate": 0.35}, detail=detail)
    entry = FakeEntry()
    return {
        "current_rate": OecCurrentRateSensor(coordinator, entry),
        "daily_supply_charge": OecDailySupplyChargeSensor(coordinator, entry),
        "demand_rate": OecDemandRateSensor(coordinator, entry),
        "demand_surcharge_per_kwh": OecDemandSurchargePerKwhSensor(
            coordinator, entry, make_tracker()
        ),
    }


@pytest.mark.parametrize(
    "key", ["current_rate", "daily_supply_charge", "demand_rate", "demand_surcharge_per_kwh"]
)
def test_price_sensor_keeps_long_term_statistics(hass, caplog, key):
    sensor = _price_sensors()[key]
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_class is None
    _assert_state_class_valid(hass, caplog, sensor, f"sensor.{key}")


def test_monthly_demand_charge_stays_monetary_and_keeps_statistics():
    coordinator = FakeCoordinator(detail={"demand": {"rate": 7.434}})
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), make_tracker())
    assert sensor.device_class == SensorDeviceClass.MONETARY
    assert sensor.state_class == SensorStateClass.TOTAL


def test_current_rate_sensor_has_valid_state_class(hass, caplog):
    coordinator = FakeCoordinator(rate={"rate": 0.35})
    sensor = OecCurrentRateSensor(coordinator, FakeEntry())
    _assert_state_class_valid(hass, caplog, sensor, "sensor.current_rate")


def test_daily_supply_charge_sensor_has_valid_state_class(hass, caplog):
    coordinator = FakeCoordinator(detail={"daily_supply_charge": 1.10})
    sensor = OecDailySupplyChargeSensor(coordinator, FakeEntry())
    _assert_state_class_valid(hass, caplog, sensor, "sensor.daily_supply_charge")


def test_demand_rate_sensor_has_valid_state_class(hass, caplog):
    coordinator = FakeCoordinator(detail={"demand": {"rate": 7.434}})
    sensor = OecDemandRateSensor(coordinator, FakeEntry())
    _assert_state_class_valid(hass, caplog, sensor, "sensor.demand_rate")


def test_monthly_demand_charge_sensor_has_valid_state_class(hass, caplog):
    coordinator = FakeCoordinator(detail={"demand": {"rate": 7.434}})
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), make_tracker())
    _assert_state_class_valid(hass, caplog, sensor, "sensor.monthly_demand_charge")


def test_demand_surcharge_per_kwh_sensor_has_valid_state_class(hass, caplog):
    coordinator = FakeCoordinator(
        detail={"demand": {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}}
    )
    sensor = OecDemandSurchargePerKwhSensor(coordinator, FakeEntry(), make_tracker())
    _assert_state_class_valid(hass, caplog, sensor, "sensor.demand_surcharge_per_kwh")
