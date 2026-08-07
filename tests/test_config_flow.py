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

from unittest.mock import patch

from custom_components.oec_tariff_au.config_flow import OecTariffConfigFlow


def _make_flow(hass, dnsp="energex", tariff="3900"):
    flow = OecTariffConfigFlow()
    flow.hass = hass
    flow._selected_dnsp = dnsp
    flow._selected_tariff = tariff
    return flow


# --- _tariff_has_demand_component: real HTTP mocking, no flow manager ------


async def test_has_demand_component_true_when_detail_includes_demand(hass, aioclient_mock):
    aioclient_mock.get(
        "https://api.openenergy.org.au/api/v1/tariffs/energex/3900",
        json={"code": "3900", "demand": {"window_start": "16:00", "window_end": "21:00"}},
    )
    flow = _make_flow(hass)

    assert await flow._tariff_has_demand_component() is True


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
