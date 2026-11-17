"""OEC Tariff integration for Home Assistant."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import CONF_BILLING_DAY, DEFAULT_BILLING_DAY, DOMAIN
from .coordinator import OecTariffCoordinator
from .window_cache import DemandWindowCache, async_remove_store

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OEC Tariff from a config entry."""
    coordinator = OecTariffCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    billing_day = entry.data.get(CONF_BILLING_DAY, DEFAULT_BILLING_DAY)
    cache = DemandWindowCache(hass, coordinator, entry.entry_id, billing_day)
    await cache.async_load()

    async def _async_refresh_windows() -> None:
        """Refresh the windows, logging any failure; the cache warns while it has no cover."""
        try:
            await cache.async_refresh(dt_util.utcnow())
        except Exception:
            _LOGGER.exception("Demand windows refresh failed")

    await _async_refresh_windows()

    @callback
    def _refresh_windows() -> None:
        """Refresh the windows on every coordinator update; the cache skips it when fresh."""
        hass.async_create_task(_async_refresh_windows())

    entry.async_on_unload(coordinator.async_add_listener(_refresh_windows))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "window_cache": cache,
        "demand_manager": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))
    return True


async def async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when its options/data are updated (e.g. billing day change)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data[DOMAIN].get(entry.entry_id, {})
    manager = data.get("demand_manager")
    if manager:
        manager.stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete the entry's cached demand windows when the entry is deleted."""
    await async_remove_store(hass, entry.entry_id)
