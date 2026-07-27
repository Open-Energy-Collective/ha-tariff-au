"""Tests for demand charge sensors — in particular the surcharge-per-kWh
amortization formula.
"""

from custom_components.oec_tariff_au.demand_sensors import OecDemandSurchargePerKwhSensor
from custom_components.oec_tariff_au.demand_tracker import DemandTracker


class FakeCoordinator:
    """Minimal stand-in for OecTariffCoordinator — only what the sensor reads."""

    def __init__(self, demand):
        self.dnsp = "test_dnsp"
        self.tariff = "test_tariff"
        self.tariff_detail = {"demand": demand}
        self.data = {"detail": {"demand": demand}}


class FakeEntry:
    entry_id = "test_entry"


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
