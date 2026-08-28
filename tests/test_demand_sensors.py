"""Tests for demand charge sensors — in particular the surcharge-per-kWh
amortization formula.
"""

from homeassistant.core import State
from pytest_homeassistant_custom_component.common import mock_restore_cache_with_extra_data

from custom_components.oec_tariff_au.demand_sensors import (
    OecDemandSurchargePerKwhSensor,
    OecMonthlyDemandChargeSensor,
    OecMonthPeakDemandSensor,
    async_setup_demand_sensors,
)
from custom_components.oec_tariff_au.demand_tracker import DemandTracker


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator — only what the sensor reads."""

    def __init__(self, demand):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = {"demand": demand}
        self.data = {"detail": {"demand": demand}}

    def async_add_listener(self, update_callback, context=None):
        """CoordinatorEntity.async_added_to_hass() needs this to register."""
        return lambda: None


class FakeEntry:
    entry_id = "test_entry"

    def __init__(self, data=None):
        self.data = data or {}


def make_tracker(window_start, window_end, month_peak_kw, measurement_method="30min_max"):
    tracker = DemandTracker(
        hass=None,
        power_entity_id="sensor.power",
        measurement_method=measurement_method,
        window_start=window_start,
        window_end=window_end,
        days="all",
        season_months=None,
        billing_day=1,
    )
    tracker.month_peak_kw = month_peak_kw
    return tracker


def test_surcharge_per_kwh_matches_tariff_service_formula():
    """4.5 kW peak demand at $7.434/kW over a 5-hour daily window should
    amortize to 0.22302 $/kWh — same figures/expected value as
    tariff-service's own test_demand_surcharge (dnsp=energex, tariff=T12A),
    since both implement the same amortization formula independently."""
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    tracker = make_tracker("16:00", "21:00", month_peak_kw=4.5)
    sensor = OecDemandSurchargePerKwhSensor(FakeCoordinator(demand), FakeEntry(), tracker)

    assert abs(sensor.native_value - 0.22302) < 0.001


def test_surcharge_per_kwh_all_day_window_no_division_by_zero():
    """An all-day (00:00-00:00) demand window — the real Jemena A230 /
    AusNet NASN12 convention — must not divide by zero and must treat the
    window as the full 24 hours."""
    demand = {"rate": 11.473, "window_start": "00:00", "window_end": "00:00"}
    tracker = make_tracker("00:00", "00:00", month_peak_kw=5.0, measurement_method="monthly_peak")
    sensor = OecDemandSurchargePerKwhSensor(FakeCoordinator(demand), FakeEntry(), tracker)

    # 5.0 * 11.473 / (24 * 30) = 0.07967
    assert abs(sensor.native_value - 0.07967) < 0.001


def test_surcharge_per_kwh_zero_peak_demand_returns_zero():
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    tracker = make_tracker("16:00", "21:00", month_peak_kw=0.0)
    sensor = OecDemandSurchargePerKwhSensor(FakeCoordinator(demand), FakeEntry(), tracker)

    assert sensor.native_value == 0.0


# --- OecMonthlyDemandChargeSensor: None branches ----------------------------


def test_monthly_demand_charge_returns_none_without_coordinator_data():
    coordinator = FakeCoordinator(demand=None)
    coordinator.data = None
    tracker = make_tracker("16:00", "21:00", month_peak_kw=4.5)
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), tracker)

    assert sensor.native_value is None


def test_monthly_demand_charge_returns_none_without_demand_detail():
    coordinator = FakeCoordinator(demand=None)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=4.5)
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), tracker)

    assert sensor.native_value is None


def test_monthly_demand_charge_computes_from_tracker_and_rate():
    coordinator = FakeCoordinator(demand={"rate": 7.434})
    tracker = make_tracker("16:00", "21:00", month_peak_kw=4.5)
    sensor = OecMonthlyDemandChargeSensor(coordinator, FakeEntry(), tracker)

    assert sensor.native_value == round(4.5 * 7.434, 2)


# --- async_setup_demand_sensors: wiring / skip conditions -------------------


class FakeAddEntities:
    def __init__(self):
        self.added = []

    def __call__(self, entities):
        self.added.extend(entities)


async def test_async_setup_demand_sensors_skips_without_power_entity(hass):
    coordinator = FakeCoordinator(demand={"rate": 1.0, "window_start": "16:00", "window_end": "21:00"})
    entry = FakeEntry(data={})
    add_entities = FakeAddEntities()

    tracker = await async_setup_demand_sensors(hass, entry, coordinator, add_entities)

    assert tracker is None
    assert add_entities.added == []


async def test_async_setup_demand_sensors_skips_without_demand_in_tariff_detail(hass):
    coordinator = FakeCoordinator(demand=None)
    coordinator.tariff_detail = {}  # no "demand" key at all
    entry = FakeEntry(data={"power_entity": "sensor.power"})
    add_entities = FakeAddEntities()

    tracker = await async_setup_demand_sensors(hass, entry, coordinator, add_entities)

    assert tracker is None
    assert add_entities.added == []


async def test_async_setup_demand_sensors_happy_path_builds_tracker_and_three_entities(hass):
    demand = {
        "rate": 7.434,
        "window_start": "16:00",
        "window_end": "21:00",
        "days": "all",
        "measurement_method": "30min_max",
        "season_months": None,
    }
    coordinator = FakeCoordinator(demand=demand)
    entry = FakeEntry(data={"power_entity": "sensor.power", "billing_day": 5})
    add_entities = FakeAddEntities()

    tracker = await async_setup_demand_sensors(hass, entry, coordinator, add_entities)

    assert isinstance(tracker, DemandTracker)
    assert tracker.power_entity_id == "sensor.power"
    assert tracker.billing_day == 5
    assert tracker.measurement_method == "30min_max"
    assert len(add_entities.added) == 3
    assert isinstance(add_entities.added[0], OecMonthPeakDemandSensor)


async def test_async_setup_demand_sensors_defaults_billing_day_when_unset(hass):
    demand = {
        "rate": 7.434,
        "window_start": "16:00",
        "window_end": "21:00",
        "days": "all",
        "measurement_method": "30min_max",
        "season_months": None,
    }
    coordinator = FakeCoordinator(demand=demand)
    entry = FakeEntry(data={"power_entity": "sensor.power"})  # no billing_day set
    add_entities = FakeAddEntities()

    tracker = await async_setup_demand_sensors(hass, entry, coordinator, add_entities)

    assert tracker.billing_day == 1  # DEFAULT_BILLING_DAY


# --- OecMonthPeakDemandSensor: restore-on-startup lifecycle -----------------


async def test_month_peak_sensor_starts_tracker_fresh_with_no_prior_state(hass):
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    coordinator = FakeCoordinator(demand=demand)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=0.0)
    sensor = OecMonthPeakDemandSensor(coordinator, FakeEntry(), tracker)
    tracker.hass = hass
    sensor.hass = hass
    sensor.entity_id = "sensor.month_peak_demand"

    await sensor.async_added_to_hass()

    try:
        assert tracker.month_peak_kw == 0.0  # nothing restored, still default
        assert tracker._unsub_timer is not None  # tracker.start() was called
    finally:
        await sensor.async_will_remove_from_hass()


async def test_month_peak_sensor_restores_saved_tracker_state_on_startup(hass):
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    coordinator = FakeCoordinator(demand=demand)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=0.0)
    sensor = OecMonthPeakDemandSensor(coordinator, FakeEntry(), tracker)
    tracker.hass = hass
    sensor.hass = hass
    sensor.entity_id = "sensor.month_peak_demand"

    saved_data = {
        "month_peak_kw": 6.75,
        "previous_month_peak_kw": 5.5,
        "monthly_peaks_12": [5.5, 6.0],
        "last_reset": "2026-07-01T00:00:00+10:00",
    }
    mock_restore_cache_with_extra_data(
        hass, [(State("sensor.month_peak_demand", "6.75"), saved_data)]
    )

    await sensor.async_added_to_hass()

    try:
        assert tracker.month_peak_kw == 6.75
        assert tracker.previous_month_peak_kw == 5.5
        assert tracker.monthly_peaks_12 == [5.5, 6.0]
    finally:
        await sensor.async_will_remove_from_hass()


async def test_month_peak_sensor_stops_tracker_on_removal(hass):
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    coordinator = FakeCoordinator(demand=demand)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=0.0)
    sensor = OecMonthPeakDemandSensor(coordinator, FakeEntry(), tracker)
    tracker.hass = hass
    sensor.hass = hass
    sensor.entity_id = "sensor.month_peak_demand"

    await sensor.async_added_to_hass()
    assert tracker._unsub_timer is not None

    await sensor.async_will_remove_from_hass()
    assert tracker._unsub_timer is None


def test_month_peak_sensor_native_value_reflects_chargeable_demand():
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    coordinator = FakeCoordinator(demand=demand)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=3.25)
    sensor = OecMonthPeakDemandSensor(coordinator, FakeEntry(), tracker)

    assert sensor.native_value == 3.25


def test_month_peak_sensor_extra_restore_state_data_round_trips_tracker_save_state():
    demand = {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"}
    coordinator = FakeCoordinator(demand=demand)
    tracker = make_tracker("16:00", "21:00", month_peak_kw=2.0)
    sensor = OecMonthPeakDemandSensor(coordinator, FakeEntry(), tracker)

    assert sensor.extra_restore_state_data.as_dict() == tracker.save_state()
