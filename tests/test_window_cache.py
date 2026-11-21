"""Tests for the billing-month demand window cache."""

import asyncio
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.oec_tariff_au.coordinator import DemandWindowsUnavailable
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import NASN12, TARIFFS, tariff_zone, windows_body

MELBOURNE = ZoneInfo("Australia/Melbourne")
SAPN = TARIFFS["LBAD"]
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


async def test_404_retry_follows_the_distributors_zone_not_homes(hass, hass_storage):
    sydney, adelaide = ZoneInfo("Australia/Sydney"), tariff_zone(SAPN)
    await hass.config.async_set_time_zone("Australia/Sydney")
    june_now = datetime(2027, 6, 20, 12, 0, tzinfo=sydney)
    start = datetime(2027, 6, 15, tzinfo=sydney)
    coordinator = SimpleNamespace(
        async_fetch_demand_windows=AsyncMock(
            side_effect=[DemandWindowsUnavailable("not found", 404), _body(start, start)]
        ),
        dnsp_time_zone=adelaide,
    )
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)

    await cache.async_refresh(june_now)

    retried_end = _ranges(coordinator)[1][1]
    assert retried_end == datetime(2027, 7, 1, tzinfo=adelaide)
    assert retried_end.utcoffset() == timedelta(hours=9, minutes=30)


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



async def test_billing_day_change_drops_months_cached_on_the_old_day(hass, hass_storage):
    await DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1).async_refresh(NOW)
    nov_15 = datetime(2026, 11, 15, tzinfo=MELBOURNE)
    dec_15 = datetime(2026, 12, 15, tzinfo=MELBOURNE)
    coordinator = _coordinator()
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)

    await cache.async_load()
    assert not cache.covers(NOW)

    await cache.async_refresh(NOW + timedelta(hours=1))

    assert _ranges(coordinator) == [(nov_15, dec_15)]
    assert cache.month_end(datetime(2026, 11, 30, 12, 0, tzinfo=MELBOURNE)) == dec_15
    months = hass_storage[STORE_KEY]["data"]["months"]
    assert [(m["start"], m["end"]) for m in months] == [(nov_15.isoformat(), dec_15.isoformat())]


async def test_billing_day_change_rewrites_the_store_when_the_fetch_fails(hass, hass_storage):
    await DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1).async_refresh(NOW)
    coordinator = _coordinator(DemandWindowsUnavailable("down"))
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)

    await cache.async_load()
    await cache.async_refresh(NOW + timedelta(hours=1))

    assert hass_storage[STORE_KEY]["data"]["months"] == []
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



async def test_fetch_before_an_unseeded_1_july_is_at_most_hourly(hass, hass_storage):
    """A fresh month cut at 1 July is refetched at most hourly in the last 24 h before it."""
    start = datetime(2027, 6, 15, tzinfo=MELBOURNE)
    first_july = datetime(2027, 7, 1, tzinfo=MELBOURNE)
    month_end = datetime(2027, 7, 15, tzinfo=MELBOURNE)
    fetched = datetime(2027, 6, 30, 1, 0, tzinfo=MELBOURNE)

    def unseeded_after_june(range_start, range_end):
        if range_end == month_end:
            raise DemandWindowsUnavailable("not found", 404)
        return _body(range_start, range_end)

    coordinator = _coordinator(unseeded_after_june)
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 15)
    await cache.async_refresh(fetched)
    assert _ranges(coordinator) == [(start, month_end), (start, first_july)]

    for minutes in range(5, 60, 5):
        await cache.async_refresh(fetched + timedelta(minutes=minutes))
    assert coordinator.async_fetch_demand_windows.call_count == 2

    await cache.async_refresh(fetched + timedelta(minutes=60))
    assert _ranges(coordinator)[2:] == [(start, month_end), (start, first_july)]

    for minutes in range(65, 23 * 60, 5):
        await cache.async_refresh(fetched + timedelta(minutes=minutes))
    # One fetch and its 404 retry per hour: the first, then hours 1 to 22.
    assert coordinator.async_fetch_demand_windows.call_count == 2 * 23
    assert cache.covers(first_july - timedelta(minutes=1))

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


async def test_removing_the_entry_deletes_the_store(hass, hass_storage):
    from custom_components.oec_tariff_au import async_remove_entry

    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    await cache.async_refresh(datetime(2027, 1, 10, 12, 0, tzinfo=MELBOURNE))
    assert STORE_KEY in hass_storage

    await async_remove_entry(hass, SimpleNamespace(entry_id=ENTRY_ID))

    assert STORE_KEY not in hass_storage


async def test_concurrent_refreshes_send_one_request(hass, hass_storage):
    async def slow_body(start, end):
        await asyncio.sleep(0)
        return _body(start, end)

    coordinator = _coordinator(slow_body)
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)
    now = datetime(2027, 1, 10, 12, 0, tzinfo=MELBOURNE)

    await asyncio.gather(cache.async_refresh(now), cache.async_refresh(now))

    assert coordinator.async_fetch_demand_windows.await_count == 1


async def test_scheduled_refresh_logs_a_failure(hass, hass_storage, caplog):
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    cache.async_refresh = AsyncMock(side_effect=RuntimeError("boom"))

    cache.schedule_refresh(datetime(2027, 1, 10, 12, 0, tzinfo=MELBOURNE))
    await hass.async_block_till_done()

    assert "Demand windows refresh failed" in caplog.text


async def test_failed_current_month_is_retried_when_next_month_succeeds(hass, hass_storage):
    jan_start = datetime(2027, 1, 1, tzinfo=MELBOURNE)
    first = DEC_START - timedelta(hours=30)  # November only: December is 30 h away
    stale = DEC_START - timedelta(hours=3)  # 27 h later: stale, December within 24 h
    coordinator = _coordinator(
        [
            _body(NOV_START, DEC_START),
            DemandWindowsUnavailable("x", None),
            _body(DEC_START, jan_start),
            _body(NOV_START, DEC_START),
        ]
    )
    cache = DemandWindowCache(hass, coordinator, ENTRY_ID, 1)
    await cache.async_refresh(first)

    await cache.async_refresh(stale)
    assert cache.fetched_at == first

    await cache.async_refresh(stale + timedelta(minutes=5))
    assert _ranges(coordinator)[-1] == (NOV_START, DEC_START)
    assert cache.fetched_at == stale + timedelta(minutes=5)


DETAIL = {"code": "NASN12", "demand_charges": NASN12["demand_charges"]}


def _saved(data: dict) -> dict:
    return {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": data}


async def test_load_reads_saved_detail_and_time_zone(hass, hass_storage):
    hass_storage[STORE_KEY] = _saved(
        {"fetched_at": None, "months": [], "detail": DETAIL, "dnsp_time_zone": "Australia/Melbourne"}
    )
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)

    await cache.async_load()

    assert cache.saved_detail == DETAIL
    assert cache.saved_time_zone == "Australia/Melbourne"


async def test_load_of_a_1_3_0_store_has_no_saved_detail(hass, hass_storage):
    await DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1).async_refresh(NOW)
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)

    await cache.async_load()

    assert cache.covers(NOW)
    assert cache.saved_detail is None
    assert cache.saved_time_zone is None


async def test_load_ignores_a_detail_that_is_not_a_dict(hass, hass_storage):
    hass_storage[STORE_KEY] = _saved(
        {"fetched_at": None, "months": [], "detail": "oops", "dnsp_time_zone": 7}
    )
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)

    await cache.async_load()

    assert cache.saved_detail is None
    assert cache.saved_time_zone is None


async def test_save_service_data_writes_both_keys_and_keeps_months(hass, hass_storage):
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    await cache.async_refresh(NOW)
    months = hass_storage[STORE_KEY]["data"]["months"]

    await cache.async_save_service_data(DETAIL, "Australia/Melbourne")

    data = hass_storage[STORE_KEY]["data"]
    assert data["detail"] == DETAIL
    assert data["dnsp_time_zone"] == "Australia/Melbourne"
    assert data["months"] == months
    assert hass_storage[STORE_KEY]["version"] == 1
    assert cache.saved_detail == DETAIL
    assert cache.saved_time_zone == "Australia/Melbourne"


async def test_save_service_data_unchanged_does_not_save(hass, hass_storage):
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    await cache.async_save_service_data(DETAIL, "Australia/Melbourne")

    with patch.object(cache._store, "async_save", wraps=cache._store.async_save) as save:
        await cache.async_save_service_data(dict(DETAIL), "Australia/Melbourne")

    save.assert_not_called()


async def test_months_refresh_keeps_saved_detail(hass, hass_storage):
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)
    await cache.async_save_service_data(DETAIL, "Australia/Melbourne")

    await cache.async_refresh(NOW)

    data = hass_storage[STORE_KEY]["data"]
    assert len(data["months"]) == 1
    assert data["detail"] == DETAIL
    assert data["dnsp_time_zone"] == "Australia/Melbourne"


async def test_billing_day_change_drops_months_keeps_detail(hass, hass_storage):
    old = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 15)
    await old.async_refresh(NOW)
    await old.async_save_service_data(DETAIL, "Australia/Melbourne")
    cache = DemandWindowCache(hass, _coordinator(), ENTRY_ID, 1)

    await cache.async_load()

    assert not cache.covers(NOW)
    assert cache.saved_detail == DETAIL
    await cache.async_refresh(NOW + timedelta(hours=1))
    data = hass_storage[STORE_KEY]["data"]
    assert [m["start"] for m in data["months"]] == [NOV_START.isoformat()]
    assert data["detail"] == DETAIL
    assert data["dnsp_time_zone"] == "Australia/Melbourne"
