"""Tests for the demand manager: one ChargeTracker per charge, fed from the window cache."""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.oec_tariff_au.charge_tracker import ChargeTracker
from custom_components.oec_tariff_au.coordinator import DemandWindowsUnavailable
from custom_components.oec_tariff_au.demand_manager import DemandManager
from custom_components.oec_tariff_au.demand_windows import charge_key
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import EA302, EVO_023, LBAD, N72, windows_body
from .fixtures import in_season as _in_season
from .fixtures import windows_coordinator as _coordinator

POWER = "sensor.grid_power"
ENTRY_ID = "entry1"

# Endeavour N19's demand_charges, copied verbatim from tariff-service seed/endeavour.json.
N19_CHARGES = [
    {
        "period": "peak",
        "rate": 15.933,
        "unit": "kVA",
        "start": "16:00",
        "end": "20:00",
        "days": "weekdays",
        "public_holidays": "excluded",
        "months": [1, 2, 3, 11, 12],
        "measurement_method": "30min_max",
        "peak_period": "month",
    },
    {
        "period": "peak",
        "rate": 14.499,
        "unit": "kVA",
        "start": "16:00",
        "end": "20:00",
        "days": "weekdays",
        "public_holidays": "excluded",
        "months": [4, 5, 6, 7, 8, 9, 10],
        "measurement_method": "30min_max",
        "peak_period": "month",
    },
]


async def _setup(hass, tariff: dict, now: datetime, charges: list[dict] | None = None):
    """A manager on `tariff` with a cache covering `now`, already reset for its month."""
    await hass.config.async_set_time_zone(tariff["timezone"])
    coordinator = _coordinator(tariff, charges)
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)
    await cache.async_refresh(now)
    manager = DemandManager(hass, POWER, cache, tariff["demand_charges"], 1)
    manager.last_reset = now.replace(day=1, hour=0, minute=0)
    return manager, cache, coordinator


def _power(hass, kw: float) -> None:
    hass.states.async_set(POWER, str(kw * 1000), {"unit_of_measurement": "W"})


def _tracker(manager: DemandManager, charge: dict):
    return manager.trackers[charge_key(charge)]


async def test_sample_routes_to_charge_in_window(hass):
    sydney = ZoneInfo("Australia/Sydney")
    # 18:00 Evoenergy standard time is 19:00 AEDT in January.
    now = datetime(2027, 1, 15, 19, 0, tzinfo=sydney)
    manager, _, _ = await _setup(hass, EVO_023, now)
    _power(hass, 4.2)

    manager._async_sample(now)

    winter, other, off_peak = EVO_023["demand_charges"]
    assert _tracker(manager, other).current_block_samples == [4.2]
    assert _tracker(manager, winter).current_block_samples == []
    assert _tracker(manager, off_peak).current_block_samples == []


async def test_lbad_concurrent_charges_both_measure(hass):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    manager, _, _ = await _setup(hass, LBAD, now)
    _power(hass, 3.0)

    manager._async_sample(now)

    for charge in LBAD["demand_charges"]:
        assert _tracker(manager, charge).current_block_samples == [3.0]


async def test_no_cover_measures_nothing(hass):
    await hass.config.async_set_time_zone("Australia/Adelaide")
    adelaide = ZoneInfo("Australia/Adelaide")
    month_start = datetime(2027, 6, 15, tzinfo=adelaide)
    first_july = datetime(2027, 7, 1, tzinfo=adelaide)
    days = [month_start.date() + timedelta(days=n) for n in range(16)]
    # The 15 June billing month 404s; its retry ends at 1 July, so cover stops there.
    coordinator = SimpleNamespace(
        async_fetch_demand_windows=AsyncMock(
            side_effect=[
                DemandWindowsUnavailable("not found", 404),
                windows_body(
                    LBAD["demand_charges"],
                    days,
                    _in_season,
                    tz=adelaide,
                    start=month_start,
                    end=first_july,
                ),
            ]
        ),
        dnsp_time_zone=adelaide,
    )
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)
    await cache.async_refresh(datetime(2027, 6, 20, 12, 0, tzinfo=adelaide))
    manager = DemandManager(hass, POWER, cache, LBAD["demand_charges"], 15)
    manager.last_reset = month_start
    anytime = _tracker(manager, LBAD["demand_charges"][1])
    _power(hass, 3.0)

    manager._async_sample(first_july - timedelta(minutes=10))
    assert anytime.current_block_samples == [3.0]

    _power(hass, 7.0)
    manager._async_sample(first_july + timedelta(minutes=5))

    assert not cache.covers(first_july)
    assert anytime.month_peak_kw == 3.0
    assert all(not t.current_block_samples for t in manager.trackers.values())
    assert all(t.current_block_start is None for t in manager.trackers.values())


async def test_billing_reset_resets_all_and_refreshes_cache(hass):
    adelaide = ZoneInfo("Australia/Adelaide")
    now = datetime(2027, 1, 31, 18, 0, tzinfo=adelaide)
    manager, cache, _ = await _setup(hass, LBAD, now)
    _power(hass, 3.0)
    manager._async_sample(now)
    for tracker in manager.trackers.values():
        tracker.flush()
        assert tracker.month_peak_kw == 3.0
    cache.async_refresh = AsyncMock()

    reset_at = datetime(2027, 2, 1, 0, 0, 30, tzinfo=adelaide)
    manager._async_sample(reset_at)
    await hass.async_block_till_done()

    assert all(t.month_peak_kw == 0.0 for t in manager.trackers.values())
    assert all(t.previous_month_peak_kw == 3.0 for t in manager.trackers.values())
    assert manager.last_reset == reset_at
    cache.async_refresh.assert_awaited_once_with(reset_at)


async def test_billing_reset_logs_a_failed_refresh(hass, caplog):
    adelaide = ZoneInfo("Australia/Adelaide")
    manager, cache, _ = await _setup(hass, LBAD, datetime(2027, 1, 31, 18, 0, tzinfo=adelaide))
    cache.async_refresh = AsyncMock(side_effect=RuntimeError("boom"))

    manager._async_sample(datetime(2027, 2, 1, 0, 0, 30, tzinfo=adelaide))
    await hass.async_block_till_done()

    assert "Demand windows refresh failed" in caplog.text


@pytest.mark.parametrize(
    ("state", "attributes", "expected"),
    [
        ("unknown", {"unit_of_measurement": "W"}, []),
        ("unavailable", {"unit_of_measurement": "W"}, []),
        ("not a number", {"unit_of_measurement": "W"}, []),
        ("2500", {"unit_of_measurement": "W"}, [2.5]),
        ("2.5", {"unit_of_measurement": "kW"}, [2.5]),
    ],
)
async def test_power_reading_states(hass, state, attributes, expected):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    manager, _, _ = await _setup(hass, LBAD, now)
    hass.states.async_set(POWER, state, attributes)

    manager._async_sample(now)

    for charge in LBAD["demand_charges"]:
        assert _tracker(manager, charge).current_block_samples == expected


async def test_first_sample_without_last_reset_resets(hass):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    manager, cache, _ = await _setup(hass, LBAD, now)
    manager.last_reset = None
    cache.async_refresh = AsyncMock()
    _power(hass, 3.0)

    manager._async_sample(now)
    await hass.async_block_till_done()

    assert manager.last_reset == now
    cache.async_refresh.assert_awaited_once_with(now)
    for charge in LBAD["demand_charges"]:
        assert _tracker(manager, charge).current_block_samples == [3.0]


async def test_mid_cycle_sample_does_not_reset(hass):
    adelaide = ZoneInfo("Australia/Adelaide")
    now = datetime(2027, 1, 15, 18, 0, tzinfo=adelaide)
    manager, cache, _ = await _setup(hass, LBAD, now)
    last_reset = manager.last_reset
    anytime = _tracker(manager, LBAD["demand_charges"][1])
    anytime.month_peak_kw = 4.0
    cache.async_refresh = AsyncMock()
    _power(hass, 3.0)

    manager._async_sample(datetime(2027, 1, 31, 23, 59, 30, tzinfo=adelaide))
    await hass.async_block_till_done()

    assert manager.last_reset == last_reset
    assert anytime.month_peak_kw == 4.0
    assert anytime.previous_month_peak_kw == 0.0
    cache.async_refresh.assert_not_awaited()


async def test_billing_day_15_resets_across_a_calendar_month(hass):
    adelaide = ZoneInfo("Australia/Adelaide")
    await hass.config.async_set_time_zone("Australia/Adelaide")
    cache = DemandWindowCache(hass, _coordinator(LBAD), ENTRY_ID, 15)
    await cache.async_refresh(datetime(2027, 1, 20, 12, 0, tzinfo=adelaide))
    manager = DemandManager(hass, POWER, cache, LBAD["demand_charges"], 15)
    manager.last_reset = datetime(2027, 1, 15, tzinfo=adelaide)
    anytime = _tracker(manager, LBAD["demand_charges"][1])
    anytime.month_peak_kw = 4.0
    cache.async_refresh = AsyncMock()
    _power(hass, 1.0)

    # 1 February is a new calendar month but the same billing month (15 January on).
    manager._async_sample(datetime(2027, 2, 1, 12, 0, tzinfo=adelaide))
    await hass.async_block_till_done()
    assert anytime.month_peak_kw == 4.0
    cache.async_refresh.assert_not_awaited()

    reset_at = datetime(2027, 2, 15, 0, 0, 30, tzinfo=adelaide)
    manager._async_sample(reset_at)
    await hass.async_block_till_done()

    assert anytime.month_peak_kw == 0.0
    assert anytime.previous_month_peak_kw == 4.0
    assert manager.last_reset == reset_at
    cache.async_refresh.assert_awaited_once_with(reset_at)


async def test_dropped_key_bills_until_reset(hass):
    sydney = ZoneInfo("Australia/Sydney")
    now = datetime(2026, 11, 17, 17, 0, tzinfo=sydney)
    summer, winter = N72["demand_charges"]
    # A reform moves October into summer: the old summer key is dropped, a new one added.
    reformed = {**summer, "months": [1, 2, 3, 10, 11, 12], "rate": 3.0}
    charges = list(N72["demand_charges"])
    detail = list(N72["demand_charges"])
    manager, cache, _ = await _setup(hass, N72, now, charges)
    manager.detail_charges = lambda: detail
    manager.start()
    _power(hass, 5.0)
    manager._async_sample(now)
    _tracker(manager, summer).flush()

    # The next refresh, which also fetches December, and the tariff detail list the reform.
    charges[:] = [reformed, winter]
    detail[:] = [reformed, winter]
    await cache.async_refresh(datetime(2026, 11, 30, 12, 0, tzinfo=sydney))

    dropped = _tracker(manager, summer)
    assert dropped.month_peak_kw == 5.0
    assert dropped.charge["rate"] == 5.454
    assert _tracker(manager, reformed).charge["rate"] == 3.0

    manager._async_sample(datetime(2026, 12, 1, 0, 0, 30, tzinfo=sydney))
    await hass.async_block_till_done()

    assert charge_key(summer) not in manager.trackers
    assert list(manager.trackers) == [charge_key(winter), charge_key(reformed)]
    manager.stop()


async def _lbad_in_march(hass):
    """An LBAD manager on 31 March with April cached and a 6 kW peak on each charge."""
    adelaide = ZoneInfo("Australia/Adelaide")
    now = datetime(2027, 3, 31, 18, 0, tzinfo=adelaide)
    manager, cache, _ = await _setup(hass, LBAD, now)
    for tracker in manager.trackers.values():
        tracker.month_peak_kw = 6.0
    cache.async_refresh = AsyncMock()
    return manager, cache, datetime(2027, 4, 1, 0, 0, 30, tzinfo=adelaide)


async def test_lbad_out_of_season_peak_survives_billing_reset(hass):
    manager, cache, reset_at = await _lbad_in_march(hass)
    peak, anytime = LBAD["demand_charges"]
    assert charge_key(peak) not in cache.charges(reset_at)

    manager._async_sample(reset_at)
    await hass.async_block_till_done()

    kept = _tracker(manager, peak)
    assert kept.month_peak_kw == 0.0
    assert kept.chargeable_demand == 6.0
    assert list(manager.trackers) == [charge_key(peak), charge_key(anytime)]


async def test_key_absent_from_cache_and_detail_is_removed_at_reset(hass):
    manager, _, reset_at = await _lbad_in_march(hass)
    peak, anytime = LBAD["demand_charges"]
    manager.detail_charges = lambda: [anytime]

    manager._async_sample(reset_at)
    await hass.async_block_till_done()

    assert list(manager.trackers) == [charge_key(anytime)]


async def test_out_of_season_tracker_takes_the_detail_fields(hass):
    manager, _, reset_at = await _lbad_in_march(hass)
    peak, anytime = LBAD["demand_charges"]
    manager.detail_charges = lambda: [{**peak, "rate": 11.0}, anytime]

    manager._async_sample(reset_at)
    await hass.async_block_till_done()

    assert _tracker(manager, peak).charge["rate"] == 11.0


async def test_restore_v2_round_trip(hass):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    manager, cache, _ = await _setup(hass, LBAD, now)
    _power(hass, 3.0)
    manager._async_sample(now)
    _power(hass, 4.5)
    manager._async_sample(now + timedelta(minutes=30))
    for tracker in manager.trackers.values():
        tracker.flush()
    saved = manager.save_state()

    restored = DemandManager(hass, POWER, cache, LBAD["demand_charges"], 1)
    restored.restore_state(saved)

    assert saved["version"] == 2
    assert restored.last_reset == manager.last_reset
    assert {k: t.save_state() for k, t in restored.trackers.items()} == {
        k: t.save_state() for k, t in manager.trackers.items()
    }


async def test_restore_1_2_x_blob_goes_to_first_charge(hass):
    await hass.config.async_set_time_zone("Australia/Sydney")
    cache = DemandWindowCache(hass, _coordinator(N72), ENTRY_ID, 1)
    manager = DemandManager(hass, POWER, cache, N19_CHARGES, 1)
    blob = {
        "month_peak_kw": 6.2,
        "month_peak_recorded_at": "2026-11-10T17:30:00+11:00",
        "previous_month_peak_kw": 5.1,
        "monthly_peaks_12": [5.1],
        "last_reset": "2026-11-01T00:00:30+11:00",
    }

    manager.restore_state(blob)

    first, second = (manager.trackers[charge_key(c)] for c in N19_CHARGES)
    assert first.month_peak_kw == 6.2
    assert first.previous_month_peak_kw == 5.1
    assert second.month_peak_kw == 0.0
    assert manager.last_reset == datetime.fromisoformat("2026-11-01T00:00:30+11:00")


async def test_restore_1_2_x_blob_in_off_season_goes_to_charge_in_season(hass, freezer):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    manager, _, _ = await _setup(hass, N72, now)
    blob = {"month_peak_kw": 6.2, "last_reset": "2026-07-01T00:00:30+10:00"}
    freezer.move_to(now)

    manager.restore_state(blob)

    summer, winter = (manager.trackers[charge_key(c)] for c in N72["demand_charges"])
    assert winter.month_peak_kw == 6.2
    assert summer.month_peak_kw == 0.0


async def test_restore_1_2_x_blob_without_cover_goes_to_first_charge(hass, freezer):
    now = datetime(2026, 7, 10, 12, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    manager, _, _ = await _setup(hass, N72, now)
    freezer.move_to(datetime(2026, 9, 10, 12, 0, tzinfo=now.tzinfo))

    manager.restore_state({"month_peak_kw": 6.2})

    summer, winter = (manager.trackers[charge_key(c)] for c in N72["demand_charges"])
    assert summer.month_peak_kw == 6.2
    assert winter.month_peak_kw == 0.0


async def test_restore_v2_keeps_dropped_rolling_key(hass):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Sydney"))
    rolling = {**EA302["demand_charges"][0], "rate": 12.5}
    old, _, _ = await _setup(hass, EA302, now)
    old.trackers.clear()
    dropped = charge_key(rolling)
    old.trackers[dropped] = ChargeTracker(dropped, rolling)
    tracker = old.trackers[dropped]
    tracker.month_peak_kw = 4.0
    tracker.reset_billing_month(now)
    tracker.month_peak_kw = 1.5
    saved = old.save_state()

    manager, _, _ = await _setup(hass, N72, now)
    manager.restore_state(saved)

    restored = manager.trackers[dropped]
    assert restored.charge["rate"] == 12.5
    assert restored.charge["peak_period"] == "rolling_12_months"
    assert restored.chargeable_demand == 4.0
    assert [charge_key(c) for c in N72["demand_charges"]] == list(manager.trackers)[:2]


async def test_restore_v2_entry_without_charge_uses_state_rate(hass):
    await hass.config.async_set_time_zone("Australia/Sydney")
    cache = DemandWindowCache(hass, _coordinator(N72), ENTRY_ID, 1)
    manager = DemandManager(hass, POWER, cache, N72["demand_charges"], 1)
    state = {"month_peak_kw": 2.0, "rate": 9.9}

    entry = {"period": "off_peak", "months": None, "state": state}

    manager.restore_state({"version": 2, "last_reset": None, "trackers": [entry]})

    tracker = manager.trackers[("off_peak", None)]
    assert tracker.charge == {"period": "off_peak", "months": None, "rate": 9.9}
    assert tracker.month_peak_kw == 2.0


async def test_hass_stop_flushes_every_tracker(hass, caplog):
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    manager, _, _ = await _setup(hass, LBAD, now)
    manager.start()
    _power(hass, 2.5)
    manager._async_sample(now)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    for tracker in manager.trackers.values():
        assert tracker.month_peak_kw == 2.5
        assert tracker.current_block_samples == []
    manager.stop()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
