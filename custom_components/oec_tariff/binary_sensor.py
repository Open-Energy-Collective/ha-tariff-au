"""Binary sensor entities for OEC Tariff."""

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OecTariffCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OEC Tariff binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: OecTariffCoordinator = data["coordinator"]
    async_add_entities([OecInDemandWindowSensor(coordinator, entry)])


class OecInDemandWindowSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating if currently in the demand measurement window."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_in_demand_window"
        self._attr_name = "Demand Window"
        self._attr_device_class = None
        self._attr_icon = "mdi:flash-alert"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Tariff ({coordinator.dnsp}/{coordinator.tariff})",
            "manufacturer": "Open Energy Collective",
            "model": "Network Tariff",
        }

    @property
    def is_on(self) -> bool | None:
        """Return true if in demand window."""
        if self.coordinator.data and self.coordinator.data.get("rate"):
            return self.coordinator.data["rate"].get("in_demand_window", False)
        return None

    @property
    def state(self) -> str | None:
        """Return Active/Inactive instead of on/off."""
        if self.is_on is None:
            return None
        return "Active" if self.is_on else "Inactive"
