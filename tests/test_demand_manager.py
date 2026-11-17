"""Tests for the demand manager: one ChargeTracker per charge, fed from the window cache."""

from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from homeassistant.const import EVENT_HOMEASSISTANT_STOP

from custom_components.oec_tariff_au.demand_manager import DemandManager
from custom_components.oec_tariff_au.demand_windows import charge_key
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import EVO_023, LBAD, N72, tariff_zone, windows_body

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


def _in_season(charge: dict, day: date):
    """Each charge's own span on the days of its months, none outside them."""
    months = charge["months"]
    if months is not None and day.month not in months:
        return None
    return charge["start"], charge["end"]


def _coordinator(tariff: dict, charges: list[dict] | None = None):
    """A fake coordinator whose bodies hold `charges` (default the tariff's own)."""
    zone = tariff_zone(tariff)

    def fetch(start: datetime, end: datetime) -> dict:
        days = [start.date() + timedelta(days=n) for n in range((end.date() - start.date()).days)]
        return windows_body(
            charges if charges is not None else tariff["demand_charges"],
            days,
            _in_season,
            tz=zone,
            start=start,
            end=end,
        )

    return SimpleNamespace(
        async_fetch_demand_windows=AsyncMock(side_effect=fetch),
        dnsp_time_zone=ZoneInfo(tariff["timezone"]),
    )


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
    now = datetime(2027, 1, 15, 18, 0, tzinfo=ZoneInfo("Australia/Adelaide"))
    cache = DemandWindowCache(hass, _coordinator(LBAD), ENTRY_ID, 1)
    manager = DemandManager(hass, POWER, cache, LBAD["demand_charges"], 1)
    manager.last_reset = now.replace(day=1, hour=0)
    before = [t.save_state() for t in manager.trackers.values()]
    _power(hass, 3.0)

    manager._async_sample(now)
    manager._async_sample(now + timedelta(minutes=30))

    assert [t.save_state() for t in manager.trackers.values()] == before
    assert all(not t.current_block_samples for t in manager.trackers.values())


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


async def test_dropped_key_bills_until_reset(hass):
    sydney = ZoneInfo("Australia/Sydney")
    now = datetime(2026, 11, 17, 17, 0, tzinfo=sydney)
    summer, winter = N72["demand_charges"]
    kept = [{**winter, "rate": 3.0}]
    charges = list(N72["demand_charges"])
    manager, cache, _ = await _setup(hass, N72, now, charges)
    manager.start()
    _power(hass, 5.0)
    manager._async_sample(now)
    _tracker(manager, summer).flush()

    # The tariff drops the summer charge; the next refresh, which also fetches
    # December, no longer holds it.
    charges[:] = kept
    await cache.async_refresh(datetime(2026, 11, 30, 12, 0, tzinfo=sydney))

    dropped = _tracker(manager, summer)
    assert dropped.month_peak_kw == 5.0
    assert dropped.charge["rate"] == 5.454
    assert _tracker(manager, winter).charge["rate"] == 3.0

    manager._async_sample(datetime(2026, 12, 1, 0, 0, 30, tzinfo=sydney))
    await hass.async_block_till_done()

    assert charge_key(summer) not in manager.trackers
    assert list(manager.trackers) == [charge_key(winter)]
    manager.stop()


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


async def test_hass_stop_flushes_every_tracker(hass):
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
