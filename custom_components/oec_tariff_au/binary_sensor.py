"""Binary sensor entities for OEC Tariff."""

from datetime import datetime, time as time_cls, timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import OecTariffCoordinator
from .demand_tracker import is_in_demand_window

# How far ahead to search for the next window transition.
_SEARCH_HORIZON_DAYS = 400


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up OEC Tariff binary sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: OecTariffCoordinator = data["coordinator"]
    if coordinator.tariff_detail and coordinator.tariff_detail.get("demand"):
        async_add_entities([OecInDemandWindowSensor(coordinator, entry)])


class OecInDemandWindowSensor(CoordinatorEntity, BinarySensorEntity):
    """Binary sensor indicating if currently in the demand measurement window.

    Computes the window locally and schedules a callback at the exact
    transition instant, rather than relying on the coordinator's poll
    cadence (which is not clock-aligned and can lag the true boundary
    by up to its full update interval).
    """

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

        self._window_start = None
        self._window_end = None
        self._days = None
        self._season_months = None
        self._is_on: bool | None = None
        self._unsub_next_transition = None

    def _load_window_config(self) -> bool:
        """Parse demand window config from cached tariff detail. Returns True if present."""
        detail = self.coordinator.tariff_detail
        if not detail or not detail.get("demand"):
            return False

        demand = detail["demand"]
        parts_start = demand["window_start"].split(":")
        parts_end = demand["window_end"].split(":")
        self._window_start = time_cls(int(parts_start[0]), int(parts_start[1]))
        self._window_end = time_cls(int(parts_end[0]), int(parts_end[1]))
        self._days = demand["days"]
        self._season_months = demand.get("season_months")
        return True

    def _status_at(self, when: datetime) -> bool:
        """Return whether `when` is inside the demand window."""
        return is_in_demand_window(
            when, self._window_start, self._window_end, self._days, self._season_months
        )

    def _find_next_transition(self, now: datetime) -> datetime | None:
        """Find the exact instant the window status next flips, searching forward."""
        current_status = self._status_at(now)
        # Boundary times to probe each day: midnight, window start, window end.
        daily_boundaries = sorted({time_cls(0, 0), self._window_start, self._window_end})

        for day_offset in range(_SEARCH_HORIZON_DAYS):
            day = (now + timedelta(days=day_offset)).date()
            for boundary in daily_boundaries:
                candidate = datetime.combine(day, boundary, tzinfo=now.tzinfo)
                if candidate <= now:
                    continue
                if self._status_at(candidate) != current_status:
                    return candidate
        return None

    @callback
    def _schedule_next_transition(self) -> None:
        """Schedule a callback at the exact next window transition instant."""
        if self._unsub_next_transition:
            self._unsub_next_transition()
            self._unsub_next_transition = None

        if self._window_start is None:
            return

        now = dt_util.now()
        next_transition = self._find_next_transition(now)
        if next_transition is None:
            return

        self._unsub_next_transition = async_track_point_in_time(
            self.hass, self._async_handle_transition, next_transition
        )

    @callback
    def _async_handle_transition(self, now: datetime) -> None:
        """Handle the exact-instant window transition."""
        self._is_on = self._status_at(now)
        self.async_write_ha_state()
        self._schedule_next_transition()

    async def async_added_to_hass(self) -> None:
        """Start tracking the window boundary once added."""
        await super().async_added_to_hass()
        if self._load_window_config():
            self._is_on = self._status_at(dt_util.now())
            self._schedule_next_transition()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending scheduled transition."""
        if self._unsub_next_transition:
            self._unsub_next_transition()
            self._unsub_next_transition = None

    @property
    def is_on(self) -> bool | None:
        """Return true if in demand window."""
        return self._is_on

    @property
    def state(self) -> str | None:
        """Return Active/Inactive instead of on/off."""
        if self.is_on is None:
            return None
        return "Active" if self.is_on else "Inactive"
