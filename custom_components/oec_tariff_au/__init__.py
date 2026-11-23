"""OEC Tariff integration for Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import CONF_BILLING_DAY, DEFAULT_BILLING_DAY, DOMAIN
from .coordinator import OecTariffCoordinator
from .window_cache import DemandWindowCache, async_remove_store

PLATFORMS = [Platform.SENSOR]
FIRST_UPDATE = f"{DOMAIN} first update"
FIRST_WINDOWS_REFRESH = f"{DOMAIN} first windows refresh"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OEC Tariff from a config entry.

    With a saved tariff detail the platforms load from the Store and every fetch runs in
    the background. With none, the rate, time zone, detail and windows are fetched first,
    and a failure raises ConfigEntryNotReady so HA retries setup.
    """
    coordinator = OecTariffCoordinator(hass, entry)
    billing_day = entry.data.get(CONF_BILLING_DAY, DEFAULT_BILLING_DAY)
    cache = DemandWindowCache(hass, coordinator, entry.entry_id, billing_day)
    await cache.async_load()

    saved = cache.saved_detail is not None
    if saved:
        zone = None
        if cache.saved_time_zone:
            try:
                zone = await dt_util.async_get_time_zone(cache.saved_time_zone)
            except ValueError:
                # A malformed key; the background update resolves the zone from /dnsps.
                zone = None
        coordinator.seed(cache.saved_detail, zone)
    else:
        await coordinator.async_first_start()
        await cache.async_refresh_logged(dt_util.utcnow())
        await _async_save_service_data(coordinator, cache)

    @callback
    def _on_update() -> None:
        """Refresh the windows on every coordinator update (the cache skips it when fresh)
        and save the detail and time zone when they changed."""
        cache.schedule_refresh(dt_util.utcnow())
        hass.async_create_task(_async_save_service_data(coordinator, cache))

    entry.async_on_unload(coordinator.async_add_listener(_on_update))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "window_cache": cache,
        "demand_manager": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_listener))

    if saved:
        # Background tasks: HA does not wait on them at startup, and cancels them on unload.
        entry.async_create_background_task(hass, coordinator.async_refresh(), FIRST_UPDATE)
        entry.async_create_background_task(
            hass, cache.async_refresh_logged(dt_util.utcnow()), FIRST_WINDOWS_REFRESH
        )
    return True


async def _async_save_service_data(
    coordinator: OecTariffCoordinator, cache: DemandWindowCache
) -> None:
    """Save the coordinator's tariff detail and time zone when both are known."""
    time_zone = coordinator.time_zone_name()
    if coordinator.tariff_detail is not None and time_zone is not None:
        await cache.async_save_service_data(coordinator.tariff_detail, time_zone)


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
