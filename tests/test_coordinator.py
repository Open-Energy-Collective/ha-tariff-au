"""Tests for the coordinator's API handling."""

from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.oec_tariff_au.const import CONF_DNSP, CONF_TARIFF, DEFAULT_API_URL, DOMAIN
from custom_components.oec_tariff_au.coordinator import OecTariffCoordinator

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
