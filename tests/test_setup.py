"""Setup without waiting on tariff-service (#25, #27, #29).

With a saved tariff detail, setup loads the platforms from the Store and fetches in the
background. With none, setup fetches first and retries on a failure.
"""

import asyncio
from datetime import date, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    mock_restore_cache_with_extra_data,
)

from custom_components.oec_tariff_au.const import (
    CONF_BILLING_DAY,
    CONF_DNSP,
    CONF_POWER_ENTITY,
    CONF_TARIFF,
    DEFAULT_API_URL,
    DOMAIN,
)
from custom_components.oec_tariff_au.coordinator import OecTariffCoordinator
from custom_components.oec_tariff_au.demand_windows import charge_key
from custom_components.oec_tariff_au.window_cache import DemandWindowCache

from .fixtures import NASN12, in_season, windows_body

MELBOURNE = ZoneInfo("Australia/Melbourne")
SETUP_NOW = datetime(2027, 1, 15, 14, 0, tzinfo=MELBOURNE)
ENTRY_ID = "test_entry"
STORE_KEY = f"{DOMAIN}.demand_windows.{ENTRY_ID}"

RATE_URL = f"{DEFAULT_API_URL}/calculate/current-rate"
DNSPS_URL = f"{DEFAULT_API_URL}/dnsps"
DETAIL_URL = f"{DEFAULT_API_URL}/tariffs/ausnet/NASN12"
WINDOWS_URL = f"{DEFAULT_API_URL}/calculate/demand-windows"
URLS = (RATE_URL, DNSPS_URL, DETAIL_URL, WINDOWS_URL)

DNSPS = [{"code": "ausnet", "timezone": "Australia/Melbourne"}]
RATE = {"rate": 0.1343, "demand_charges": [{"rate": 11.473}]}
DETAIL = {"code": "NASN12", "name": "NASN12", "demand_charges": NASN12["demand_charges"]}

DEMAND_KEYS = (
    "demand_rate",
    "demand_window",
    "month_peak_demand",
    "monthly_demand_charge",
    "demand_surcharge_per_kwh",
)
MEASURED_KEYS = ("month_peak_demand", "monthly_demand_charge", "demand_surcharge_per_kwh")
RATE_KEYS = ("current_rate", "current_period", "daily_supply_charge", "tariff_name", "demand_rate")

FIRST_UPDATE = "oec_tariff_au first update"


def _month(first: date, next_first: date) -> dict:
    """A saved month [first, next_first) with NASN12's in-season windows."""
    days = [first + timedelta(days=n) for n in range((next_first - first).days)]
    start = datetime(first.year, first.month, first.day, tzinfo=MELBOURNE)
    end = datetime(next_first.year, next_first.month, next_first.day, tzinfo=MELBOURNE)
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "body": windows_body(NASN12["demand_charges"], days, in_season, tz=MELBOURNE),
    }


JANUARY = _month(date(2027, 1, 1), date(2027, 2, 1))


def _save_store(hass_storage, **data) -> None:
    hass_storage[STORE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"fetched_at": (SETUP_NOW - timedelta(hours=1)).isoformat(), **data},
    }


def _filled_store(hass_storage, detail=DETAIL, time_zone="Australia/Melbourne") -> None:
    _save_store(hass_storage, months=[JANUARY], detail=detail, dnsp_time_zone=time_zone)


def _all_fail(aioclient_mock) -> None:
    for url in URLS:
        aioclient_mock.get(url, status=503)


def _all_answer(aioclient_mock, detail=DETAIL, windows=None) -> None:
    aioclient_mock.get(RATE_URL, json=RATE)
    aioclient_mock.get(DNSPS_URL, json=DNSPS)
    aioclient_mock.get(DETAIL_URL, json=detail)
    aioclient_mock.get(WINDOWS_URL, json=windows or JANUARY["body"])


async def _entry(hass, freezer, now=SETUP_NOW) -> MockConfigEntry:
    freezer.move_to(now)
    await hass.config.async_set_time_zone("Australia/Melbourne")
    hass.states.async_set("sensor.power", "1500", {"unit_of_measurement": "W"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        data={
            CONF_DNSP: "ausnet",
            CONF_TARIFF: "NASN12",
            CONF_POWER_ENTITY: "sensor.power",
            CONF_BILLING_DAY: 1,
        },
    )
    entry.add_to_hass(hass)
    return entry


def _ids(hass, keys) -> dict:
    registry = er.async_get(hass)
    return {k: registry.async_get_entity_id("sensor", DOMAIN, f"{ENTRY_ID}_{k}") for k in keys}


def _state(hass, key) -> str:
    return hass.states.get(_ids(hass, [key])[key]).state


async def _unload(hass, entry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


@pytest.fixture
def stalled():
    """Every tariff-service fetch waits on an event the test sets; yields the event."""
    release = asyncio.Event()

    async def _wait(*_args, **_kwargs):
        await release.wait()

    with (
        patch.object(OecTariffCoordinator, "_async_update_data", _wait),
        patch.object(DemandWindowCache, "async_refresh", _wait),
    ):
        yield release


# --- Saved path ---------------------------------------------------------------


async def test_restart_in_an_outage_measures_demand_from_the_store(
    hass, hass_storage, aioclient_mock, freezer
):
    _filled_store(hass_storage)
    _all_fail(aioclient_mock)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert all(_ids(hass, DEMAND_KEYS).values())
    assert _state(hass, "current_rate") == "unavailable"
    assert _state(hass, "demand_rate") == "unavailable"
    manager = hass.data[DOMAIN][ENTRY_ID]["demand_manager"]
    assert manager is not None

    await _sample_5_kw_through_a_block(hass, freezer)

    summer = manager.trackers[charge_key(NASN12["demand_charges"][0])]
    assert summer.month_peak_kw == 5.0
    await _unload(hass, entry)


async def _sample_5_kw_through_a_block(hass, freezer) -> None:
    """5 kW in the 15:00 to 15:30 window block, then a sample past it closes the block."""
    hass.states.async_set("sensor.power", "5000", {"unit_of_measurement": "W"})
    for when in (
        datetime(2027, 1, 15, 15, 5, tzinfo=MELBOURNE),
        datetime(2027, 1, 15, 15, 31, tzinfo=MELBOURNE),
    ):
        freezer.move_to(when)
        async_fire_time_changed(hass, when)
        await hass.async_block_till_done()


async def test_restart_in_an_outage_demand_sensors_available(
    hass, hass_storage, aioclient_mock, freezer
):
    _filled_store(hass_storage)
    _all_fail(aioclient_mock)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    try:
        for key in (*MEASURED_KEYS, "demand_window"):
            assert _state(hass, key) != "unavailable", key
        # Failed updates after the first do not notify entities: the shown peak still moves.
        await hass.data[DOMAIN][ENTRY_ID]["coordinator"].async_refresh()
        await _sample_5_kw_through_a_block(hass, freezer)
        assert float(_state(hass, "month_peak_demand")) == 5.0
    finally:
        await _unload(hass, entry)


async def test_saved_setup_does_not_wait_on_tariff_service(
    hass, hass_storage, aioclient_mock, freezer, stalled
):
    _filled_store(hass_storage)
    entry = await _entry(hass, freezer)

    assert await asyncio.wait_for(hass.config_entries.async_setup(entry.entry_id), 5)

    assert entry.state is ConfigEntryState.LOADED
    assert all(_ids(hass, DEMAND_KEYS).values())
    stalled.set()
    await _unload(hass, entry)


async def test_unload_while_the_first_update_waits(
    hass, hass_storage, aioclient_mock, freezer, stalled
):
    _filled_store(hass_storage)
    entry = await _entry(hass, freezer)
    assert await asyncio.wait_for(hass.config_entries.async_setup(entry.entry_id), 5)

    assert await asyncio.wait_for(hass.config_entries.async_unload(entry.entry_id), 5)
    await hass.async_block_till_done()

    pending = [t for t in asyncio.all_tasks() if FIRST_UPDATE in t.get_name() and not t.done()]
    assert pending == []
    stalled.set()


async def test_saved_detail_without_demand_charges_creates_no_demand_entities(
    hass, hass_storage, aioclient_mock, freezer
):
    _filled_store(hass_storage, detail={**DETAIL, "demand_charges": []})
    _all_fail(aioclient_mock)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert _ids(hass, DEMAND_KEYS) == dict.fromkeys(DEMAND_KEYS)
    assert _ids(hass, ["current_rate"])["current_rate"]
    await _unload(hass, entry)


async def test_unknown_saved_zone_takes_the_saved_path(
    hass, hass_storage, aioclient_mock, freezer
):
    _filled_store(hass_storage, time_zone="Mars/Olympus")
    _all_answer(aioclient_mock)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert all(_ids(hass, DEMAND_KEYS).values())
    assert hass.data[DOMAIN][ENTRY_ID]["coordinator"].dnsp_time_zone == MELBOURNE
    assert hass_storage[STORE_KEY]["data"]["dnsp_time_zone"] == "Australia/Melbourne"
    await _unload(hass, entry)


async def test_first_good_update_rewrites_the_store_and_rates_turn_available(
    hass, hass_storage, aioclient_mock, freezer
):
    _filled_store(hass_storage)
    changed = {**DETAIL, "name": "NASN12 (revised)"}
    _all_answer(aioclient_mock, detail=changed)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert hass_storage[STORE_KEY]["data"]["detail"] == changed
    assert hass_storage[STORE_KEY]["data"]["months"] == [JANUARY]
    assert _state(hass, "current_rate") == "0.1343"
    await _unload(hass, entry)


async def test_outage_after_a_good_start(hass, hass_storage, aioclient_mock, freezer):
    _all_answer(aioclient_mock)
    entry = await _entry(hass, freezer)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _state(hass, "current_rate") == "0.1343"

    aioclient_mock.clear_requests()
    _all_fail(aioclient_mock)
    await hass.data[DOMAIN][ENTRY_ID]["coordinator"].async_refresh()
    await hass.async_block_till_done()

    for key in (*MEASURED_KEYS, "demand_window"):
        assert _state(hass, key) != "unavailable", key
    for key in RATE_KEYS:
        assert _state(hass, key) == "unavailable", key
    await _unload(hass, entry)


# --- First-start path ---------------------------------------------------------


async def test_first_start_detail_failure_retries_then_creates_demand_entities(
    hass, hass_storage, aioclient_mock, freezer
):
    aioclient_mock.get(RATE_URL, json=RATE)
    aioclient_mock.get(DNSPS_URL, json=DNSPS)
    aioclient_mock.get(DETAIL_URL, status=503)
    aioclient_mock.get(WINDOWS_URL, json=JANUARY["body"])
    entry = await _entry(hass, freezer)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _ids(hass, DEMAND_KEYS) == dict.fromkeys(DEMAND_KEYS)

    aioclient_mock.clear_requests()
    _all_answer(aioclient_mock)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert all(_ids(hass, DEMAND_KEYS).values())
    data = hass_storage[STORE_KEY]["data"]
    assert data["detail"] == DETAIL
    assert data["dnsp_time_zone"] == "Australia/Melbourne"
    await _unload(hass, entry)


async def test_1_3_0_store_takes_first_start_then_saves_detail(
    hass, hass_storage, aioclient_mock, freezer
):
    _save_store(hass_storage, months=[JANUARY])
    _all_answer(aioclient_mock)
    entry = await _entry(hass, freezer)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    data = hass_storage[STORE_KEY]["data"]
    assert data["detail"] == DETAIL
    assert data["dnsp_time_zone"] == "Australia/Melbourne"
    assert data["months"] == [JANUARY]
    await _unload(hass, entry)


async def test_1_2_x_restore_lands_on_the_charge_in_season(
    hass, hass_storage, aioclient_mock, freezer
):
    """July: NASN12's second (winter) charge is in season, not the first."""
    july = _month(date(2027, 7, 1), date(2027, 8, 1))
    _all_answer(aioclient_mock, windows=july["body"])
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.tariff_ausnet_nasn12_month_peak_demand", "6.75"),
                {"month_peak_kw": 6.75, "last_reset": "2027-07-01T00:00:00+10:00"},
            )
        ],
    )
    entry = await _entry(hass, freezer, now=datetime(2027, 7, 10, 12, 0, tzinfo=MELBOURNE))

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    manager = hass.data[DOMAIN][ENTRY_ID]["demand_manager"]
    summer, winter = (manager.trackers[charge_key(c)] for c in NASN12["demand_charges"])
    assert winter.month_peak_kw == 6.75
    assert summer.month_peak_kw == 0.0
    await _unload(hass, entry)
