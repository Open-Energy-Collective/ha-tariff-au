"""Tests for the config flow's demand-step skip logic.

Scope: only the new behavior (config_flow.py had zero tests before this --
flagged as a TODO in .agent/ha-integration-rules.md, not otherwise addressed
here). The tariff *list* endpoint only returns summary fields; whether a
tariff has a demand component is only known from the per-tariff detail
endpoint, so the demand-tracking (optional power sensor) step must be
skipped for tariffs with no demand component instead of always showing it.

Deliberately never drives a real hass.config_entries.flow.async_init/
async_configure -- that triggers real custom-component loading, which hits
a lingering-thread teardown assertion in the pinned
pytest-homeassistant-custom-component==0.13.205 that doesn't recognize a
thread name used by whatever newer `homeassistant` core is actually
installed (checked the plugin source directly: it's a hard assert with no
override fixture, unlike the adjacent tasks/timers checks). Non-deterministic
across otherwise-identical runs -- a real version-skew bug in the test
tooling, not something to chase down inline here, and not something to
leave in the suite either given this repo's zero-tolerance-for-flaky-tests
rule. Testing `_tariff_has_demand_component` directly (real HTTP mocking,
no flow manager) and `async_step_tariff`'s routing decision (mocked
downstream methods) gives full coverage without ever touching that path.
"""

import re
from pathlib import Path
from unittest.mock import patch

from custom_components.oec_tariff_au.config_flow import OecTariffConfigFlow


def _make_flow(hass, dnsp="energex", tariff="3900"):
    flow = OecTariffConfigFlow()
    flow.hass = hass
    flow._selected_dnsp = dnsp
    flow._selected_tariff = tariff
    return flow


# --- _tariff_has_demand_component: real HTTP mocking, no flow manager ------


async def test_has_demand_component_true_when_detail_lists_demand_charges(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.openenergy.org.au/api/v1/tariffs/energex/3900",
        json={"code": "3900", "demand_charges": [{"period": "peak", "rate": 7.434}]},
    )
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is True


async def test_config_flow_demand_step_uses_demand_charges(hass, aioclient_mock):
    """An empty `demand_charges` skips the demand step, whatever the legacy `demand` holds."""
    aioclient_mock.get(
        "https://api.openenergy.org.au/api/v1/tariffs/energex/3900",
        json={
            "code": "3900",
            "demand_charges": [],
            "demand": {"rate": 7.434, "window_start": "16:00", "window_end": "21:00"},
        },
    )
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is False


async def test_has_demand_component_false_when_detail_omits_demand(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.openenergy.org.au/api/v1/tariffs/energex/6900",
        json={"code": "6900", "name": "Flat Rate"},
    )
    flow = _make_flow(hass, tariff="6900")

    assert await flow._tariff_has_demand_component() is False


async def test_has_demand_component_fails_open_on_http_error(hass, aioclient_mock):
    """If the detail fetch itself errors, fail open (True) rather than
    silently skip setup for a tariff that might genuinely have a demand
    component."""
    aioclient_mock.get(
        "https://api.openenergy.org.au/api/v1/tariffs/energex/3900",
        status=500,
    )
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is True


# --- async_step_tariff: routing decision, downstream methods mocked -------


async def test_tariff_step_finishes_directly_when_no_demand_component(hass):
    """async_step_tariff must route straight to _async_finish_entry
    (skipping the demand-tracking step) when _tariff_has_demand_component()
    says the selected tariff has none."""
    flow = _make_flow(hass, tariff="6900")

    with (
        patch.object(flow, "_tariff_has_demand_component", return_value=False),
        patch.object(flow, "_async_finish_entry", return_value="FINISHED") as finish,
        patch.object(flow, "async_step_demand") as demand_step,
    ):
        result = await flow.async_step_tariff({"tariff": "6900"})

    assert result == "FINISHED"
    finish.assert_called_once_with()
    demand_step.assert_not_called()


async def test_tariff_step_shows_demand_form_when_demand_component_present(hass):
    """Symmetric case: routes to async_step_demand, not straight to finish,
    when the tariff does have a demand component."""
    flow = _make_flow(hass, tariff="3900")

    with (
        patch.object(flow, "_tariff_has_demand_component", return_value=True),
        patch.object(flow, "_async_finish_entry") as finish,
        patch.object(flow, "async_step_demand", return_value="DEMAND_FORM") as demand_step,
    ):
        result = await flow.async_step_tariff({"tariff": "3900"})

    assert result == "DEMAND_FORM"
    demand_step.assert_called_once_with()
    finish.assert_not_called()


# --- No code reads the tariff detail's legacy `demand` object -----------------

COMPONENT_DIR = Path(__file__).resolve().parent.parent / "custom_components"
LEGACY_DEMAND = re.compile(r'\["demand"\]|get\("demand"\)')


def test_no_code_reads_legacy_demand():
    hits = [
        f"{path.relative_to(COMPONENT_DIR)}:{number}"
        for path in COMPONENT_DIR.rglob("*.py")
        for number, line in enumerate(path.read_text().splitlines(), start=1)
        if LEGACY_DEMAND.search(line)
    ]
    assert hits == []


# --- #28: the flow honours the shared Retry-After backoff --------------------

from datetime import timedelta  # noqa: E402

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.oec_tariff_au.const import DEFAULT_API_URL  # noqa: E402
from custom_components.oec_tariff_au.coordinator import BACKOFF_UNTIL  # noqa: E402

DNSPS_URL = f"{DEFAULT_API_URL}/dnsps"
TARIFFS_URL = f"{DEFAULT_API_URL}/tariffs/energex"
DETAIL_URL = f"{DEFAULT_API_URL}/tariffs/energex/3900"


def _start_shared_backoff(hass, seconds=60):
    """An entry's coordinator took a 429: the backoff every client of this api_url shares."""
    hass.data.setdefault(BACKOFF_UNTIL, {})[DEFAULT_API_URL.rstrip("/")] = (
        dt_util.utcnow() + timedelta(seconds=seconds)
    )


async def test_user_step_during_backoff_sends_no_request_and_shows_rate_limited(
    hass, aioclient_mock
):
    aioclient_mock.get(DNSPS_URL, json=[])
    _start_shared_backoff(hass)
    flow = _make_flow(hass)

    result = await flow.async_step_user()

    assert aioclient_mock.call_count == 0
    assert result["type"] == "form"
    assert result["errors"] == {"base": "rate_limited"}


async def test_tariff_step_during_backoff_sends_no_request_and_shows_rate_limited(
    hass, aioclient_mock
):
    aioclient_mock.get(TARIFFS_URL, json=[])
    _start_shared_backoff(hass)
    flow = _make_flow(hass)

    result = await flow.async_step_tariff()

    assert aioclient_mock.call_count == 0
    assert result["errors"] == {"base": "rate_limited"}


async def test_user_step_429_starts_the_shared_backoff_and_shows_rate_limited(
    hass, aioclient_mock, freezer
):
    freezer.move_to("2026-11-21T02:00:00+00:00")
    aioclient_mock.get(DNSPS_URL, status=429, headers={"Retry-After": "45"})
    flow = _make_flow(hass)

    result = await flow.async_step_user()

    assert result["errors"] == {"base": "rate_limited"}
    until = hass.data[BACKOFF_UNTIL][DEFAULT_API_URL.rstrip("/")]
    assert until == dt_util.utcnow() + timedelta(seconds=45)


async def test_tariff_step_429_starts_the_shared_backoff_and_shows_rate_limited(
    hass, aioclient_mock
):
    aioclient_mock.get(TARIFFS_URL, status=429, headers={"Retry-After": "30"})
    flow = _make_flow(hass)

    result = await flow.async_step_tariff()

    assert result["errors"] == {"base": "rate_limited"}
    assert DEFAULT_API_URL.rstrip("/") in hass.data[BACKOFF_UNTIL]


async def test_demand_check_during_backoff_sends_no_request_and_fails_open(
    hass, aioclient_mock
):
    aioclient_mock.get(DETAIL_URL, json={"code": "3900", "demand_charges": []})
    _start_shared_backoff(hass)
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is True
    assert aioclient_mock.call_count == 0


async def test_demand_check_429_starts_the_shared_backoff_and_fails_open(
    hass, aioclient_mock
):
    aioclient_mock.get(DETAIL_URL, status=429, headers={"Retry-After": "30"})
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is True
    assert DEFAULT_API_URL.rstrip("/") in hass.data[BACKOFF_UNTIL]


async def test_flow_backoff_expiry_lets_the_request_through(hass, aioclient_mock, freezer):
    freezer.move_to("2026-11-21T02:00:00+00:00")
    aioclient_mock.get(DNSPS_URL, json=[{"code": "energex", "name": "Energex", "state": "QLD"}])
    _start_shared_backoff(hass, seconds=30)
    freezer.tick(31)
    flow = _make_flow(hass)

    result = await flow.async_step_user()

    assert aioclient_mock.call_count == 1
    assert result["errors"] == {}


def test_rate_limited_error_has_ui_text_in_strings_and_translations():
    import json

    base = Path(__file__).parent.parent / "custom_components" / "oec_tariff_au"
    for name in ("strings.json", "translations/en.json"):
        errors = json.loads((base / name).read_text())["config"]["error"]
        assert errors.get("rate_limited"), name
