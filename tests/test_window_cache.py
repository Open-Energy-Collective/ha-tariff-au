"""Tests for the billing-month demand window cache."""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.oec_tariff_au.coordinator import DemandWindowsUnavailable
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import NASN12, windows_body

MELBOURNE = ZoneInfo("Australia/Melbourne")
ENTRY_ID = "entry1"
STORE_KEY = f"oec_tariff_au.demand_windows.{ENTRY_ID}"


@pytest.fixture(autouse=True)
async def _melbourne_clock(hass):
    """Put the HA instance on the DNSP's clock, as a Victorian install would be."""
    await hass.config.async_set_time_zone("Australia/Melbourne")


def _body(start: datetime, end: datetime) -> dict:
    """A NASN12 body with a 15:00 to 21:00 window on every day of [start, end)."""
    days = [start.date() + timedelta(days=n) for n in range((end.date() - start.date()).days)]
    return windows_body(NASN12["demand_charges"], days, tz=MELBOURNE, start=start, end=end)


def _coordinator(side_effect=_body):
    return SimpleNamespace(
        async_fetch_demand_windows=AsyncMock(side_effect=side_effect),
        dnsp_time_zone=MELBOURNE,
    )


def _ranges(coordinator):
    return [call.args for call in coordinator.async_fetch_demand_windows.call_args_list]


NOW = datetime(2026, 11, 17, 12, 0, tzinfo=MELBOURNE)
NOV_START = datetime(2026, 11, 1, tzinfo=MELBOURNE)
DEC_START = datetime(2026, 12, 1, tzinfo=MELBOURNE)


async def test_refresh_fetches_billing_month_and_saves(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)

    assert _ranges(coordinator) == [(NOV_START, DEC_START)]
    months = hass_storage[STORE_KEY]["data"]["months"]
    assert [(m["start"], m["end"]) for m in months] == [
        (NOV_START.isoformat(), DEC_START.isoformat())
    ]
    assert months[0]["body"] == _body(NOV_START, DEC_START)
    assert hass_storage[STORE_KEY]["version"] == 1
    assert cache.covers(NOW)
    assert cache.fetched_at == NOW
    assert len(cache.charges(NOW)) == 2


async def test_cache_loads_from_store_without_fetching(hass, hass_storage):
    await DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1).async_refresh(NOW)
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_load()

    assert cache.covers(NOW)
    assert cache.fetched_at == NOW
    assert len(cache.charges(NOW)) == 2
    coordinator.async_fetch_demand_windows.assert_not_called()


async def test_refresh_within_24h_of_month_end_fetches_next_month(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)
    now = DEC_START - timedelta(hours=3)

    await cache.async_refresh(now)

    assert _ranges(coordinator) == [
        (NOV_START, DEC_START),
        (DEC_START, datetime(2027, 1, 1, tzinfo=MELBOURNE)),
    ]
    assert cache.covers(DEC_START + timedelta(hours=1))
    assert len(hass_storage[STORE_KEY]["data"]["months"]) == 2


async def test_refresh_skips_when_fresh(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)
    await cache.async_refresh(NOW + timedelta(hours=23))

    assert coordinator.async_fetch_demand_windows.call_count == 1


async def test_refresh_refetches_after_24h(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)
    await cache.async_refresh(NOW + timedelta(hours=25))

    assert coordinator.async_fetch_demand_windows.call_count == 2
    assert cache.fetched_at == NOW + timedelta(hours=25)


async def test_404_retries_to_1_july(hass, hass_storage):
    june_now = datetime(2027, 6, 20, 12, 0, tzinfo=MELBOURNE)
    start = datetime(2027, 6, 15, tzinfo=MELBOURNE)
    first_july = datetime(2027, 7, 1, tzinfo=MELBOURNE)
    coordinator = _coordinator(
        [DemandWindowsUnavailable("not found", 404), _body(start, first_july)]
    )
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)

    await cache.async_refresh(june_now)

    assert _ranges(coordinator) == [
        (start, datetime(2027, 7, 15, tzinfo=MELBOURNE)),
        (start, first_july),
    ]
    retried_end = _ranges(coordinator)[1][1]
    assert retried_end.astimezone(MELBOURNE).hour == 0
    assert retried_end.utcoffset() == timedelta(hours=10)
    assert cache.covers(june_now)
    assert not cache.covers(first_july)


async def test_second_404_leaves_no_cover(hass, hass_storage):
    coordinator = _coordinator(
        [DemandWindowsUnavailable("not found", 404), DemandWindowsUnavailable("not found", 404)]
    )
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)
    june_now = datetime(2027, 6, 20, 12, 0, tzinfo=MELBOURNE)

    await cache.async_refresh(june_now)

    assert coordinator.async_fetch_demand_windows.call_count == 2
    assert not cache.covers(june_now)
    assert cache.charges(june_now) == {}


async def test_body_without_charges_not_cached_and_warns(hass, hass_storage, caplog):
    old = {k: v for k, v in _body(NOV_START, DEC_START).items() if k != "charges"}
    cache = DemandWindowCache(hass, _coordinator(lambda s, e: old), ENTRY_ID, 1)

    with caplog.at_level(logging.WARNING):
        await cache.async_refresh(NOW)

    assert not cache.covers(NOW)
    assert STORE_KEY not in hass_storage
    assert len([r for r in caplog.records if "0.10.0" in r.getMessage()]) == 1


async def test_no_cover_warns_once_per_outage(hass, hass_storage, caplog):
    outage = DemandWindowsUnavailable("down")
    coordinator = _coordinator([outage, outage, _body(NOV_START, DEC_START), outage])
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    def cover_warnings():
        return [r for r in caplog.records if "no cached demand windows" in r.getMessage().lower()]

    with caplog.at_level(logging.WARNING):
        await cache.async_refresh(NOW)
        await cache.async_refresh(NOW + timedelta(minutes=5))
        assert len(cover_warnings()) == 1

        await cache.async_refresh(NOW + timedelta(minutes=10))
        assert cache.covers(NOW)
        # Covered again until the month ends; a failed refresh after it starts a new outage.
        await cache.async_refresh(DEC_START + timedelta(hours=1))
        assert len(cover_warnings()) == 2


async def test_failed_refresh_keeps_cached_month(hass, hass_storage):
    coordinator = _coordinator([_body(NOV_START, DEC_START), DemandWindowsUnavailable("x", None)])
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)
    await cache.async_refresh(NOW + timedelta(hours=25))

    assert coordinator.async_fetch_demand_windows.call_count == 2
    assert cache.covers(NOW + timedelta(hours=25))
    assert cache.fetched_at == NOW


async def test_months_ended_before_now_are_pruned(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)
    await cache.async_refresh(DEC_START + timedelta(days=2))

    months = hass_storage[STORE_KEY]["data"]["months"]
    assert [m["start"] for m in months] == [DEC_START.isoformat()]
    assert not cache.covers(NOW)


async def test_listener_called_after_change(hass, hass_storage):
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    calls = []
    remove = cache.add_listener(lambda: calls.append(1))

    await cache.async_refresh(NOW)
    assert calls == [1]

    remove()
    await cache.async_refresh(NOW + timedelta(hours=25))
    assert calls == [1]


async def test_refresh_fetches_when_next_24h_uncovered(hass, hass_storage):
    start = datetime(2027, 6, 15, tzinfo=MELBOURNE)
    first_july = datetime(2027, 7, 1, tzinfo=MELBOURNE)
    now = datetime(2027, 6, 30, 12, 0, tzinfo=MELBOURNE)
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {
            "fetched_at": (now - timedelta(hours=1)).isoformat(),
            "months": [
                {
                    "start": start.isoformat(),
                    "end": first_july.isoformat(),
                    "body": _body(start, first_july),
                }
            ],
        },
    }
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)
    await cache.async_load()

    await cache.async_refresh(now)

    assert _ranges(coordinator) == [(start, datetime(2027, 7, 15, tzinfo=MELBOURNE))]
    assert cache.covers(first_july + timedelta(hours=1))


async def test_404_after_1_july_skips_retry_and_caches_nothing(hass, hass_storage):
    coordinator = _coordinator(DemandWindowsUnavailable("not found", 404))
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)
    calls = []
    cache.add_listener(lambda: calls.append(1))
    now = datetime(2027, 7, 2, 12, 0, tzinfo=MELBOURNE)

    await cache.async_refresh(now)

    assert _ranges(coordinator) == [
        (datetime(2027, 6, 15, tzinfo=MELBOURNE), datetime(2027, 7, 15, tzinfo=MELBOURNE))
    ]
    assert not cache.covers(now)
    assert cache.fetched_at is None
    assert STORE_KEY not in hass_storage
    assert calls == []


async def test_refresh_refetches_at_exactly_24h(hass, hass_storage):
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)

    await cache.async_refresh(NOW)
    await cache.async_refresh(NOW + timedelta(hours=24))

    assert coordinator.async_fetch_demand_windows.call_count == 2
