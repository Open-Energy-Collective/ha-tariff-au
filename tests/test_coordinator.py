"""Tests for the coordinator's API handling."""

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMockResponse

from custom_components.oec_tariff_au.const import (
    CONF_API_URL,
    CONF_DNSP,
    CONF_TARIFF,
    DEFAULT_API_URL,
    DEFAULT_RETRY_AFTER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from custom_components.oec_tariff_au.coordinator import (
    BACKOFF_UNTIL,
    DemandWindowsUnavailable,
    OecTariffCoordinator,
)

DNSPS = [
    {"code": "energex", "timezone": "Australia/Brisbane"},
    {"code": "ausnet", "timezone": "Australia/Melbourne"},
]


def make_coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DNSP: "ausnet", CONF_TARIFF: "NASN12"})
    entry.add_to_hass(hass)
    return OecTariffCoordinator(hass, entry)


def mock_rate_and_detail(aioclient_mock):
    aioclient_mock.get(f"{DEFAULT_API_URL}/calculate/current-rate", json={"rate": 0.1343})
    aioclient_mock.get(f"{DEFAULT_API_URL}/tariffs/ausnet/NASN12", json={"demand": None})


async def test_update_resolves_the_dnsp_time_zone(hass, aioclient_mock):
    mock_rate_and_detail(aioclient_mock)
    aioclient_mock.get(f"{DEFAULT_API_URL}/dnsps", json=DNSPS)
    coordinator = make_coordinator(hass)

    await coordinator._async_update_data()

    assert coordinator.dnsp_time_zone == ZoneInfo("Australia/Melbourne")


async def test_time_zone_is_fetched_once(hass, aioclient_mock):
    mock_rate_and_detail(aioclient_mock)
    aioclient_mock.get(f"{DEFAULT_API_URL}/dnsps", json=DNSPS)
    coordinator = make_coordinator(hass)

    await coordinator._async_update_data()
    await coordinator._async_update_data()

    dnsp_calls = [c for c in aioclient_mock.mock_calls if str(c[1]).endswith("/dnsps")]
    assert len(dnsp_calls) == 1


async def test_update_fails_when_the_dnsps_call_fails(hass, aioclient_mock):
    """A window must never be matched on the wrong clock: no zone, no update."""
    mock_rate_and_detail(aioclient_mock)
    aioclient_mock.get(f"{DEFAULT_API_URL}/dnsps", status=500)

    with pytest.raises(UpdateFailed):
        await make_coordinator(hass)._async_update_data()


async def test_update_fails_when_the_dnsp_is_not_listed(hass, aioclient_mock):
    mock_rate_and_detail(aioclient_mock)
    aioclient_mock.get(f"{DEFAULT_API_URL}/dnsps", json=DNSPS[:1])

    with pytest.raises(UpdateFailed):
        await make_coordinator(hass)._async_update_data()


# Rate limiting (#18): tariff-service answers 429 with Retry-After past 20 requests a minute.

RATE_URL = f"{DEFAULT_API_URL}/calculate/current-rate"
DNSPS_URL = f"{DEFAULT_API_URL}/dnsps"
DETAIL_URL = f"{DEFAULT_API_URL}/tariffs/ausnet/NASN12"


def mock_all_ok(aioclient_mock):
    aioclient_mock.get(RATE_URL, json={"rate": 0.1343})
    aioclient_mock.get(DNSPS_URL, json=DNSPS)
    aioclient_mock.get(DETAIL_URL, json={"demand": None})


def calls_to(aioclient_mock, url):
    return [c for c in aioclient_mock.mock_calls if str(c[1]).split("?")[0] == url]


def rate_limit_warnings(caplog):
    return [r for r in caplog.records if r.levelno >= logging.WARNING and "429" in r.getMessage()]


async def test_429_with_retry_after_seconds_defers_the_next_request(hass, aioclient_mock, freezer):
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "45"})
    coordinator = make_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after is not None and err.value.retry_after >= 45

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(44)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert aioclient_mock.call_count == 0

    freezer.tick(2)
    await coordinator._async_update_data()
    assert len(calls_to(aioclient_mock, RATE_URL)) == 1


async def test_429_with_retry_after_http_date_defers_the_next_request(hass, aioclient_mock, freezer):
    freezer.move_to("2026-11-17 02:00:00+00:00")
    aioclient_mock.get(
        RATE_URL, status=429, headers={"Retry-After": "Tue, 17 Nov 2026 02:01:30 GMT"}
    )
    coordinator = make_coordinator(hass)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(89)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert aioclient_mock.call_count == 0

    freezer.tick(2)
    await coordinator._async_update_data()
    assert len(calls_to(aioclient_mock, RATE_URL)) == 1


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "soon"}])
async def test_429_without_a_usable_retry_after_uses_the_default(
    hass, aioclient_mock, freezer, headers
):
    aioclient_mock.get(RATE_URL, status=429, headers=headers)
    coordinator = make_coordinator(hass)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(DEFAULT_RETRY_AFTER - 1)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert aioclient_mock.call_count == 0

    freezer.tick(2)
    await coordinator._async_update_data()
    assert len(calls_to(aioclient_mock, RATE_URL)) == 1


def test_default_backoff_is_bounded():
    assert 0 < DEFAULT_RETRY_AFTER <= DEFAULT_SCAN_INTERVAL


async def test_429_on_the_dnsps_request_defers_the_next_request(hass, aioclient_mock, freezer):
    aioclient_mock.get(RATE_URL, json={"rate": 0.1343})
    aioclient_mock.get(DNSPS_URL, status=429, headers={"Retry-After": "30"})
    coordinator = make_coordinator(hass)

    with pytest.raises(UpdateFailed) as err:
        await coordinator._async_update_data()
    assert err.value.retry_after is not None and err.value.retry_after >= 30

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(29)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert aioclient_mock.call_count == 0

    freezer.tick(2)
    await coordinator._async_update_data()
    assert coordinator.dnsp_time_zone == ZoneInfo("Australia/Melbourne")


async def test_429_on_the_tariff_detail_request_defers_the_next_request(
    hass, aioclient_mock, freezer
):
    aioclient_mock.get(RATE_URL, json={"rate": 0.1343})
    aioclient_mock.get(DNSPS_URL, json=DNSPS)
    aioclient_mock.get(DETAIL_URL, status=429, headers={"Retry-After": "30"})
    coordinator = make_coordinator(hass)

    data = await coordinator._async_update_data()
    assert data["rate"] == {"rate": 0.1343}
    assert data["detail"] is None

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(29)
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert aioclient_mock.call_count == 0

    freezer.tick(2)
    data = await coordinator._async_update_data()
    assert data["detail"] == {"demand": None}


async def test_backoff_survives_a_new_coordinator_for_the_same_entry(
    hass, aioclient_mock, freezer
):
    """A failed first refresh sets the entry up again with a new coordinator, in 5 s."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_DNSP: "ausnet", CONF_TARIFF: "NASN12"})
    entry.add_to_hass(hass)
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})

    with pytest.raises(UpdateFailed):
        await OecTariffCoordinator(hass, entry)._async_update_data()

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(5)
    with pytest.raises(UpdateFailed):
        await OecTariffCoordinator(hass, entry)._async_update_data()
    assert aioclient_mock.call_count == 0


def make_entry_coordinator(hass, api_url=None, tariff="NASN12"):
    data = {CONF_DNSP: "ausnet", CONF_TARIFF: tariff}
    if api_url is not None:
        data[CONF_API_URL] = api_url
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    entry.add_to_hass(hass)
    return OecTariffCoordinator(hass, entry)


async def test_429_on_one_entry_blocks_another_entry_on_the_same_api_url(hass, aioclient_mock):
    """The limit is per remote IP: every entry calling the same service backs off."""
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})
    with pytest.raises(UpdateFailed):
        await make_entry_coordinator(hass)._async_update_data()

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    with pytest.raises(UpdateFailed, match="Rate limited"):
        await make_entry_coordinator(hass, tariff="NAST11")._async_update_data()
    assert aioclient_mock.call_count == 0


async def test_api_url_with_a_trailing_slash_shares_the_backoff(hass, aioclient_mock):
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})
    with pytest.raises(UpdateFailed):
        await make_entry_coordinator(hass)._async_update_data()

    with pytest.raises(UpdateFailed, match="Rate limited"):
        await make_entry_coordinator(hass, api_url=f"{DEFAULT_API_URL}/")._async_update_data()


async def test_429_does_not_block_an_entry_on_a_different_api_url(hass, aioclient_mock):
    other_url = "https://tariffs.example.test/api/v1"
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})
    with pytest.raises(UpdateFailed):
        await make_entry_coordinator(hass)._async_update_data()

    aioclient_mock.get(f"{other_url}/calculate/current-rate", json={"rate": 0.1343})
    aioclient_mock.get(f"{other_url}/dnsps", json=DNSPS)
    aioclient_mock.get(f"{other_url}/tariffs/ausnet/NASN12", json={"demand": None})
    await make_entry_coordinator(hass, api_url=other_url)._async_update_data()
    assert len(calls_to(aioclient_mock, f"{other_url}/calculate/current-rate")) == 1


async def test_429_is_logged_once_per_backoff_across_entries(hass, aioclient_mock, caplog):
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})
    coordinators = [make_entry_coordinator(hass, tariff=t) for t in ("NASN12", "NAST11", "NA2")]

    for coordinator in coordinators:
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
    assert len(rate_limit_warnings(caplog)) == 1


async def test_429_is_logged_once_per_backoff(hass, aioclient_mock, freezer, caplog):
    aioclient_mock.get(RATE_URL, status=429, headers={"Retry-After": "60"})
    coordinator = make_coordinator(hass)

    for _ in range(4):
        with pytest.raises(UpdateFailed):
            await coordinator._async_update_data()
        freezer.tick(10)
    assert len(rate_limit_warnings(caplog)) == 1

    freezer.tick(30)  # past the first backoff: the next request gets a second 429
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
    assert len(rate_limit_warnings(caplog)) == 2



def mock_rate_starting_a_backoff(aioclient_mock, hass, seconds=60):
    """A current-rate answer during which another entry's 429 starts the shared backoff."""

    async def rate_then_backoff(method, url, data):
        hass.data[BACKOFF_UNTIL][DEFAULT_API_URL.rstrip("/")] = dt_util.utcnow() + timedelta(
            seconds=seconds
        )
        return AiohttpClientMockResponse(method, url, json={"rate": 0.1343})

    aioclient_mock.get(RATE_URL, side_effect=rate_then_backoff)
    aioclient_mock.get(DNSPS_URL, json=DNSPS)
    aioclient_mock.get(DETAIL_URL, json={"demand": None})


async def test_no_time_zone_request_during_a_backoff_started_mid_update(
    hass, aioclient_mock, caplog
):
    mock_rate_starting_a_backoff(aioclient_mock, hass)
    coordinator = make_coordinator(hass)

    with pytest.raises(UpdateFailed, match="Rate limited") as err:
        await coordinator._async_update_data()

    assert err.value.retry_after is not None
    assert calls_to(aioclient_mock, DNSPS_URL) == []
    assert calls_to(aioclient_mock, DETAIL_URL) == []
    assert rate_limit_warnings(caplog) == []


async def test_no_tariff_detail_request_during_a_backoff_started_mid_update(
    hass, aioclient_mock, freezer, caplog
):
    mock_rate_starting_a_backoff(aioclient_mock, hass, seconds=60)
    coordinator = make_coordinator(hass)
    coordinator.dnsp_time_zone = ZoneInfo("Australia/Melbourne")

    data = await coordinator._async_update_data()

    assert data["rate"] == {"rate": 0.1343}
    assert data["detail"] is None
    assert calls_to(aioclient_mock, DETAIL_URL) == []
    assert rate_limit_warnings(caplog) == []

    aioclient_mock.clear_requests()
    mock_all_ok(aioclient_mock)
    freezer.tick(61)
    data = await coordinator._async_update_data()
    assert len(calls_to(aioclient_mock, DETAIL_URL)) == 1
    assert data["detail"] == {"demand": None}


# Demand windows (#16)

WINDOWS_URL = f"{DEFAULT_API_URL}/calculate/demand-windows"
WINDOW_START = datetime(2026, 11, 1, tzinfo=timezone(timedelta(hours=10)))
WINDOW_END = datetime(2026, 11, 8, tzinfo=timezone(timedelta(hours=10)))


async def test_fetch_demand_windows_sends_offset_range(hass, aioclient_mock):
    body = {"windows": []}
    aioclient_mock.get(WINDOWS_URL, json=body)

    result = await make_coordinator(hass).async_fetch_demand_windows(WINDOW_START, WINDOW_END)

    assert result == body
    params = aioclient_mock.mock_calls[0][1].query
    assert params["dnsp"] == "ausnet"
    assert params["tariff"] == "NASN12"
    assert params["start"] == "2026-11-01T00:00:00+10:00"
    assert params["end"] == "2026-11-08T00:00:00+10:00"


async def test_fetch_demand_windows_404_raises_with_status(hass, aioclient_mock):
    aioclient_mock.get(WINDOWS_URL, status=404)

    with pytest.raises(DemandWindowsUnavailable) as err:
        await make_coordinator(hass).async_fetch_demand_windows(WINDOW_START, WINDOW_END)

    assert err.value.status == 404


async def test_fetch_demand_windows_429_starts_shared_backoff(hass, aioclient_mock):
    aioclient_mock.get(WINDOWS_URL, status=429, headers={"Retry-After": "30"})

    with pytest.raises(DemandWindowsUnavailable):
        await make_coordinator(hass).async_fetch_demand_windows(WINDOW_START, WINDOW_END)

    assert DEFAULT_API_URL.rstrip("/") in hass.data[BACKOFF_UNTIL]


async def test_fetch_demand_windows_honours_shared_backoff(hass, aioclient_mock):
    aioclient_mock.get(WINDOWS_URL, json={})
    coordinator = make_coordinator(hass)
    hass.data.setdefault(BACKOFF_UNTIL, {})[DEFAULT_API_URL.rstrip("/")] = dt_util.utcnow() + timedelta(
        seconds=60
    )

    with pytest.raises(DemandWindowsUnavailable) as err:
        await coordinator.async_fetch_demand_windows(WINDOW_START, WINDOW_END)

    assert err.value.status is None
    assert calls_to(aioclient_mock, WINDOWS_URL) == []


# Tariff detail refetch (#16): an out-of-season charge is known only from the detail.


def mock_rate_and_detail_v2(aioclient_mock):
    aioclient_mock.get(RATE_URL, json={"rate": 0.1343})
    aioclient_mock.get(DETAIL_URL, json={"demand_charges": []})


async def test_tariff_detail_is_refetched_once_a_day(hass, aioclient_mock, freezer):
    mock_all_ok(aioclient_mock)
    coordinator = make_coordinator(hass)

    await coordinator._async_update_data()
    freezer.tick(timedelta(hours=23))
    await coordinator._async_update_data()
    assert len(calls_to(aioclient_mock, DETAIL_URL)) == 1

    aioclient_mock.clear_requests()
    mock_rate_and_detail_v2(aioclient_mock)
    freezer.tick(timedelta(hours=1))
    data = await coordinator._async_update_data()

    assert len(calls_to(aioclient_mock, DETAIL_URL)) == 1
    assert coordinator.tariff_detail == {"demand_charges": []}
    assert data["detail"] == {"demand_charges": []}


@pytest.mark.parametrize("status", [500, None])
async def test_failed_tariff_detail_refetch_keeps_the_previous_detail(
    hass, aioclient_mock, freezer, caplog, status
):
    mock_all_ok(aioclient_mock)
    coordinator = make_coordinator(hass)
    await coordinator._async_update_data()

    aioclient_mock.clear_requests()
    aioclient_mock.get(RATE_URL, json={"rate": 0.1343})
    if status is None:
        aioclient_mock.get(DETAIL_URL, exc=TimeoutError())
    else:
        aioclient_mock.get(DETAIL_URL, status=status)
    freezer.tick(timedelta(hours=24))
    data = await coordinator._async_update_data()

    assert len(calls_to(aioclient_mock, DETAIL_URL)) == 1
    assert data["detail"] == {"demand": None}
    assert any(
        r.levelno == logging.WARNING and "tariff detail" in r.getMessage() for r in caplog.records
    )
