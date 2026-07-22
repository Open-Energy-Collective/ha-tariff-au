"""Config flow for OEC Tariff integration."""

import logging

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_API_URL, CONF_BILLING_DAY, CONF_DNSP, CONF_POWER_ENTITY, CONF_TARIFF, DEFAULT_API_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class OecTariffConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OEC Tariff."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._dnsps: list[dict] = []
        self._tariffs: list[dict] = []
        self._selected_dnsp: str = ""
        self._selected_tariff: str = ""

    async def async_step_user(self, user_input=None):
        """Handle the initial step — select DNSP."""
        errors = {}

        if user_input is not None:
            self._selected_dnsp = user_input[CONF_DNSP]
            return await self.async_step_tariff()

        # Fetch available DNSPs from API
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{DEFAULT_API_URL}/dnsps", timeout=10
            ) as resp:
                if resp.status == 200:
                    self._dnsps = await resp.json()
                else:
                    errors["base"] = "cannot_connect"
        except (aiohttp.ClientError, TimeoutError):
            errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(
                step_id="user",
                data_schema=vol.Schema({vol.Required(CONF_DNSP): str}),
                errors=errors,
            )

        dnsp_options = {
            d["code"]: f"{d['name']} ({d['state']})" for d in self._dnsps
        }

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_DNSP): vol.In(dnsp_options)}
            ),
            errors=errors,
        )

    async def async_step_tariff(self, user_input=None):
        """Handle tariff selection step."""
        errors = {}

        if user_input is not None:
            self._selected_tariff = user_input[CONF_TARIFF]
            return await self.async_step_demand()

        # Fetch tariffs for selected DNSP
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                f"{DEFAULT_API_URL}/tariffs/{self._selected_dnsp}", timeout=10
            ) as resp:
                if resp.status == 200:
                    self._tariffs = await resp.json()
                else:
                    errors["base"] = "cannot_connect"
        except (aiohttp.ClientError, TimeoutError):
            errors["base"] = "cannot_connect"

        if errors:
            return self.async_show_form(
                step_id="tariff",
                data_schema=vol.Schema({vol.Required(CONF_TARIFF): str}),
                errors=errors,
            )

        tariff_options = {
            t["code"]: f"{t['code']} — {t['name']}" for t in self._tariffs
        }

        return self.async_show_form(
            step_id="tariff",
            data_schema=vol.Schema(
                {vol.Required(CONF_TARIFF): vol.In(tariff_options)}
            ),
            errors=errors,
        )

    async def async_step_demand(self, user_input=None):
        """Handle optional demand tracking configuration."""
        from homeassistant.helpers import selector

        if user_input is not None:
            # Create the config entry
            await self.async_set_unique_id(
                f"{self._selected_dnsp}_{self._selected_tariff}"
            )
            self._abort_if_unique_id_configured()

            data = {
                CONF_DNSP: self._selected_dnsp,
                CONF_TARIFF: self._selected_tariff,
                CONF_API_URL: DEFAULT_API_URL,
            }
            if user_input.get(CONF_POWER_ENTITY):
                data[CONF_POWER_ENTITY] = user_input[CONF_POWER_ENTITY]
                data[CONF_BILLING_DAY] = user_input.get(CONF_BILLING_DAY, 1)

            return self.async_create_entry(
                title=f"{self._selected_dnsp}/{self._selected_tariff}",
                data=data,
            )

        return self.async_show_form(
            step_id="demand",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_POWER_ENTITY): selector.EntitySelector(
                        selector.EntitySelectorConfig(
                            device_class="power",
                        )
                    ),
                    vol.Optional(CONF_BILLING_DAY, default=1): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=28)
                    ),
                }
            ),
        )
