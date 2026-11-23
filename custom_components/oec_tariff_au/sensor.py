"""Sensor entities for OEC Tariff."""

from collections.abc import Callable
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import OecTariffCoordinator
from .demand_sensors import async_setup_demand_sensors
from .demand_windows import in_windows, next_bound
from .window_cache import DemandWindowCache

# The tariff detail's `demand_charges` fields listed in Demand Rate's `charges` attribute.
CHARGE_FIELDS = (
    "period",
    "rate",
    "unit",
    "months",
    "start",
    "end",
    "days",
    "public_holidays",
    "measurement_method",
    "peak_period",
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OEC Tariff sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: OecTariffCoordinator = data["coordinator"]
    cache: DemandWindowCache = data["window_cache"]

    entities = [
        OecCurrentRateSensor(coordinator, entry),
        OecCurrentPeriodSensor(coordinator, entry),
        OecDailySupplyChargeSensor(coordinator, entry),
        OecTariffNameSensor(coordinator, entry),
    ]
    if coordinator.tariff_detail and coordinator.tariff_detail.get("demand_charges"):
        entities.append(OecDemandRateSensor(coordinator, entry))
        entities.append(OecDemandWindowStatusSensor(coordinator, entry, cache))
    async_add_entities(entities)

    # Demand tracking sensors, when a power entity is configured
    manager = await async_setup_demand_sensors(
        hass, entry, coordinator, cache, async_add_entities
    )
    if manager:
        data["demand_manager"] = manager


class OecBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for OEC Tariff sensors."""

    def __init__(
        self, coordinator: OecTariffCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        """Initialize sensor."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Tariff ({coordinator.dnsp}/{coordinator.tariff})",
            "manufacturer": "Open Energy Collective",
            "model": "Network Tariff",
        }

    @property
    def available(self) -> bool:
        """Return False until tariff-service has answered since this start.

        last_update_success starts True, so data is checked too.
        """
        return super().available and self.coordinator.data is not None


class OecCurrentRateSensor(OecBaseSensor):
    """Current energy rate sensor."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "current_rate")
        self._attr_name = "Current Rate"
        self._attr_native_unit_of_measurement = "$/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 5

    @property
    def native_value(self) -> float | None:
        """Return current rate."""
        if self.coordinator.data and self.coordinator.data.get("rate"):
            return self.coordinator.data["rate"].get("rate")
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return additional attributes."""
        if self.coordinator.data and self.coordinator.data.get("rate"):
            rate = self.coordinator.data["rate"]
            return {
                "period": rate.get("period"),
                "in_demand_window": rate.get("in_demand_window"),
                "dnsp": rate.get("dnsp"),
                "tariff": rate.get("tariff"),
                "datetime": rate.get("datetime"),
            }
        return {}


class OecCurrentPeriodSensor(OecBaseSensor):
    """Current tariff period sensor."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "current_period")
        self._attr_name = "Current Period"
        self._attr_icon = "mdi:clock-time-four-outline"

    @property
    def native_value(self) -> str | None:
        """Return current period name."""
        if self.coordinator.data and self.coordinator.data.get("rate"):
            return self.coordinator.data["rate"].get("period")
        return None


class OecDailySupplyChargeSensor(OecBaseSensor):
    """Daily supply charge sensor."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "daily_supply_charge")
        self._attr_name = "Daily Supply Charge"
        self._attr_native_unit_of_measurement = "$/day"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 4

    @property
    def native_value(self) -> float | None:
        """Return daily supply charge."""
        if self.coordinator.data and self.coordinator.data.get("detail"):
            return self.coordinator.data["detail"].get("daily_supply_charge")
        return None


class OecDemandRateSensor(OecBaseSensor):
    """Demand charge rate sensor."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "demand_rate")
        self._attr_name = "Demand Rate"
        self._attr_native_unit_of_measurement = "$/kW/month"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 2

    @property
    def native_value(self) -> float | None:
        """Return the highest rate among the charges in season now, 0.0 when none is."""
        if not self.coordinator.data or not self.coordinator.data.get("rate"):
            return None
        in_season = self.coordinator.data["rate"].get("demand_charges") or []
        return max((charge["rate"] for charge in in_season), default=0.0)

    @property
    def extra_state_attributes(self) -> dict:
        """Return every demand charge of the tariff detail."""
        detail = self.coordinator.data.get("detail") if self.coordinator.data else None
        if not detail:
            return {}
        return {
            "charges": [
                {field: charge.get(field) for field in CHARGE_FIELDS}
                for charge in detail.get("demand_charges") or []
            ]
        }


class OecDemandWindowStatusSensor(OecBaseSensor):
    """Enum sensor for demand window status (active/inactive).

    An enum sensor's `options` render as a fixed dropdown in the automation
    state trigger/condition UI, so "active"/"inactive" can be picked
    directly without a custom value.

    The stored/matched value is lowercase ("active"/"inactive", the HA
    enum-sensor convention for machine values); `translation_key` maps that
    to "Active"/"Inactive" for display, via the `entity.sensor` block in
    strings.json.

    The state follows the cached windows of every charge. It is unavailable while the
    cache does not cover now.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["active", "inactive"]
    _attr_translation_key = "demand_window"

    def __init__(
        self,
        coordinator: OecTariffCoordinator,
        entry: ConfigEntry,
        cache: DemandWindowCache,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "demand_window")
        self._attr_name = "Demand Window"
        self._attr_icon = "mdi:flash-alert"
        self._cache = cache
        self._unsub_transition: Callable[[], None] | None = None

    def _active_charges(self, now: datetime) -> list[str]:
        """Return the period of each cached charge with a window holding `now`."""
        return [
            cached.charge["period"]
            for cached in self._cache.charges(now).values()
            if in_windows(cached.windows, now)
        ]

    def _cancel_transition(self) -> None:
        if self._unsub_transition:
            self._unsub_transition()
            self._unsub_transition = None

    @callback
    def _schedule_transition(self) -> None:
        """Schedule a state write at the next bound of any window, or the end of the cached month.

        Any bound, not only where the state flips: `active_charges` changes at every bound.

        The month end is where cover runs out, or where the next cached month's windows
        take over.
        """
        self._cancel_transition()
        now = dt_util.now()
        windows = [w for cached in self._cache.charges(now).values() for w in cached.windows]
        candidates = [next_bound(windows, now), self._cache.month_end(now)]
        upcoming = [when for when in candidates if when is not None]
        if upcoming:
            self._unsub_transition = async_track_point_in_time(
                self.hass, self._handle_transition, min(upcoming, key=dt_util.as_utc)
            )

    @callback
    def _handle_transition(self, _now: datetime) -> None:
        """Write the new state at a window bound and schedule the next one."""
        self._unsub_transition = None
        self._schedule_transition()
        self.async_write_ha_state()

    @callback
    def _handle_cache_change(self) -> None:
        """Reschedule from the new windows and write the state."""
        self._schedule_transition()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Start tracking the window bounds once added."""
        await super().async_added_to_hass()
        self.async_on_remove(self._cache.add_listener(self._handle_cache_change))
        self._schedule_transition()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending scheduled transition."""
        self._cancel_transition()

    @property
    def available(self) -> bool:
        """Return False while no cached windows cover now, whatever tariff-service's state."""
        return self._cache.covers(dt_util.now())

    @property
    def native_value(self) -> str | None:
        """Return "active" or "inactive", None when no cached windows cover now."""
        now = dt_util.now()
        if not self._cache.covers(now):
            return None
        return "active" if self._active_charges(now) else "inactive"

    @property
    def extra_state_attributes(self) -> dict:
        """Return the periods active now."""
        return {"active_charges": self._active_charges(dt_util.now())}


class OecTariffNameSensor(OecBaseSensor):
    """Tariff name sensor."""

    def __init__(self, coordinator: OecTariffCoordinator, entry: ConfigEntry) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, "tariff_name")
        self._attr_name = "Tariff Name"
        self._attr_icon = "mdi:tag-text-outline"

    @property
    def native_value(self) -> str | None:
        """Return tariff name."""
        if self.coordinator.data and self.coordinator.data.get("detail"):
            return self.coordinator.data["detail"].get("name")
        return None
