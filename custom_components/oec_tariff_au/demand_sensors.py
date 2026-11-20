"""Demand charge sensors for OEC Tariff, over the per-charge trackers of a DemandManager."""

from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .charge_tracker import ChargeTracker
from .const import CONF_BILLING_DAY, CONF_POWER_ENTITY, DEFAULT_BILLING_DAY, DOMAIN
from .coordinator import OecTariffCoordinator
from .demand_manager import DemandManager
from .demand_windows import window_hours
from .window_cache import DemandWindowCache


async def async_setup_demand_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: OecTariffCoordinator,
    cache: DemandWindowCache,
    async_add_entities: AddEntitiesCallback,
) -> DemandManager | None:
    """Set up the demand tracking sensors when a power entity is configured."""
    power_entity = entry.data.get(CONF_POWER_ENTITY)
    if not power_entity:
        return None

    detail = coordinator.tariff_detail
    demand_charges = detail.get("demand_charges") if detail else None
    if not demand_charges:
        return None

    manager = DemandManager(
        hass=hass,
        power_entity_id=power_entity,
        cache=cache,
        demand_charges=demand_charges,
        billing_day=entry.data.get(CONF_BILLING_DAY, DEFAULT_BILLING_DAY),
        detail_charges=lambda: (coordinator.tariff_detail or {}).get("demand_charges"),
    )

    async_add_entities(
        [
            OecMonthPeakDemandSensor(coordinator, entry, manager),
            OecMonthlyDemandChargeSensor(coordinator, entry, manager),
            OecDemandSurchargePerKwhSensor(coordinator, entry, manager),
        ]
    )
    return manager


def _iso(when: datetime | None) -> str | None:
    return when.isoformat() if when else None


def _charge_id(tracker: ChargeTracker) -> dict:
    """The `period` and `months` naming a tracker's charge in the `charges` attributes."""
    period, months = tracker.key
    return {"period": period, "months": list(months) if months is not None else None}


def _rate(tracker: ChargeTracker) -> float:
    return tracker.charge.get("rate") or 0.0


class DemandStoredData(ExtraStoredData):
    """The demand manager's saved state, kept by RestoreEntity."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def as_dict(self) -> dict:
        return self._data

    @classmethod
    def from_dict(cls, data: dict) -> "DemandStoredData":
        return cls(data)


class OecDemandBaseSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """Base class for demand sensors with state restoration."""

    def __init__(
        self,
        coordinator: OecTariffCoordinator,
        entry: ConfigEntry,
        manager: DemandManager,
        key: str,
    ) -> None:
        """Initialize."""
        super().__init__(coordinator)
        self._manager = manager
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": f"Tariff ({coordinator.dnsp}/{coordinator.tariff})",
            "manufacturer": "Open Energy Collective",
            "model": "Network Tariff",
        }

    def _peak_tracker(self) -> ChargeTracker | None:
        """The tracker with the highest chargeable peak, the first one on a tie."""
        trackers = list(self._manager.trackers.values())
        return max(trackers, key=lambda t: t.chargeable_demand, default=None)

    def _demand_recorded_at_iso(self) -> str | None:
        """ISO timestamp of when the highest chargeable peak was recorded, if known."""
        tracker = self._peak_tracker()
        return _iso(tracker.chargeable_demand_recorded_at) if tracker else None


class OecMonthPeakDemandSensor(OecDemandBaseSensor):
    """Highest chargeable peak demand across the charges."""

    def __init__(
        self, coordinator: OecTariffCoordinator, entry: ConfigEntry, manager: DemandManager
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, manager, "month_peak_demand")
        self._attr_name = "Month Peak Demand"
        self._attr_native_unit_of_measurement = "kW"
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:chart-line-variant"

    async def async_added_to_hass(self) -> None:
        """Restore the manager's state, then start it."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_extra_data()
        if last_state:
            self._manager.restore_state(last_state.as_dict())
        self._manager.start()

    async def async_will_remove_from_hass(self) -> None:
        """Stop tracking on removal."""
        self._manager.stop()

    @property
    def native_value(self) -> float:
        """Return the highest chargeable peak across the charges."""
        tracker = self._peak_tracker()
        return tracker.chargeable_demand if tracker else 0.0

    @property
    def extra_state_attributes(self) -> dict:
        """Return each charge's peak and the tracking metadata."""
        trackers = list(self._manager.trackers.values())
        charges = []
        for tracker in trackers:
            entry = {
                **_charge_id(tracker),
                "unit": tracker.charge.get("unit"),
                "measurement_method": tracker.charge.get("measurement_method"),
                "peak_period": tracker.charge.get("peak_period"),
                "peak": tracker.chargeable_demand,
                "demand_recorded_at": _iso(tracker.chargeable_demand_recorded_at),
            }
            if tracker.charge.get("unit") == "kVA":
                entry["measured_as"] = "kW"
            charges.append(entry)
        return {
            "charges": charges,
            "billing_day": self._manager.billing_day,
            "last_reset": _iso(self._manager.last_reset),
            "previous_month_peak": max(
                (t.previous_month_peak_kw for t in trackers), default=0.0
            ),
            "current_block_samples": max(
                (len(t.current_block_samples) for t in trackers), default=0
            ),
            "power_entity": self._manager.power_entity_id,
            "demand_recorded_at": self._demand_recorded_at_iso(),
        }

    @property
    def extra_restore_state_data(self) -> DemandStoredData:
        """Return state to persist."""
        return DemandStoredData(self._manager.save_state())


class OecMonthlyDemandChargeSensor(OecDemandBaseSensor):
    """Monthly demand charge in dollars, summed over the charges."""

    def __init__(
        self, coordinator: OecTariffCoordinator, entry: ConfigEntry, manager: DemandManager
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, manager, "monthly_demand_charge")
        self._attr_name = "Monthly Demand Charge"
        self._attr_native_unit_of_measurement = "$"
        self._attr_device_class = SensorDeviceClass.MONETARY
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_suggested_display_precision = 2
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float:
        """Return the sum over charges of chargeable peak times rate."""
        return round(
            sum(t.chargeable_demand * _rate(t) for t in self._manager.trackers.values()), 2
        )

    @property
    def extra_state_attributes(self) -> dict:
        """Return each charge's amount."""
        return {
            "charges": [
                {**_charge_id(t), "amount": round(t.chargeable_demand * _rate(t), 2)}
                for t in self._manager.trackers.values()
            ],
            "demand_recorded_at": self._demand_recorded_at_iso(),
        }


class OecDemandSurchargePerKwhSensor(OecDemandBaseSensor):
    """Demand surcharge amortized per kWh."""

    def __init__(
        self, coordinator: OecTariffCoordinator, entry: ConfigEntry, manager: DemandManager
    ) -> None:
        """Initialize."""
        super().__init__(coordinator, entry, manager, "demand_surcharge_per_kwh")
        self._attr_name = "Demand Surcharge"
        self._attr_native_unit_of_measurement = "$/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_suggested_display_precision = 3
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float | None:
        """Return the sum over charges with a peak of rate / that charge's window hours.

        The window hours are the charge's cached windows in the billing month. While the
        cached month is cut short (#26) its window hours are too few, so a charge with a
        peak makes the value unknown rather than high.
        """
        now = dt_util.now()
        cached = self._manager.cache.charges(now)
        partial = self._manager.cache.month_is_partial(now)
        total = 0.0
        for key, tracker in self._manager.trackers.items():
            if tracker.chargeable_demand <= 0 or key not in cached:
                continue
            if partial:
                return None
            hours = window_hours(cached[key].windows)
            if hours > 0:
                total += _rate(tracker) / hours
        return round(total, 5)

    @property
    def extra_state_attributes(self) -> dict:
        """Return tracking metadata."""
        return {
            "demand_recorded_at": self._demand_recorded_at_iso(),
            "partial_month": self._manager.cache.month_is_partial(dt_util.now()),
        }
