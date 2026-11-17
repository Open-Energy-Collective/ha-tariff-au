"""Samples the power entity and feeds one ChargeTracker per demand charge."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .charge_tracker import ChargeTracker
from .demand_windows import ChargeKey, charge_key, in_windows
from .window_cache import DemandWindowCache

_LOGGER = logging.getLogger(__name__)

SAMPLE_INTERVAL = timedelta(seconds=30)
STATE_VERSION = 2


class DemandManager:
    """Owns the per-charge trackers and routes each power sample by the cached windows."""

    def __init__(
        self,
        hass: HomeAssistant,
        power_entity_id: str,
        cache: DemandWindowCache,
        demand_charges: list[dict],
        billing_day: int,
        detail_charges: Callable[[], list[dict] | None] | None = None,
    ) -> None:
        """Create one tracker per charge key, in tariff detail order.

        `detail_charges` returns the latest tariff detail's `demand_charges` (None when
        unknown); it defaults to `demand_charges`.
        """
        self.hass = hass
        self.power_entity_id = power_entity_id
        self.cache = cache
        self.billing_day = billing_day
        self.trackers: dict[ChargeKey, ChargeTracker] = {}
        for charge in demand_charges:
            key = charge_key(charge)
            if key not in self.trackers:
                self.trackers[key] = ChargeTracker(key, charge)
        self._detail_keys: list[ChargeKey] = list(self.trackers)
        self.detail_charges: Callable[[], list[dict] | None] = detail_charges or (
            lambda: demand_charges
        )
        self.last_reset: datetime | None = None

        self._unsub_timer: Callable[[], None] | None = None
        self._unsub_hass_stop: Callable[[], None] | None = None
        self._unsub_cache: Callable[[], None] | None = None

    @callback
    def sync(self, now: datetime | None = None) -> None:
        """Align the trackers with the cached charges at `now`.

        Updates the charge of each cached key and adds a tracker for a new one. A key
        missing from the cache but listed by the tariff detail (out of season) takes the
        detail's charge. Any other key is left alone; the billing reset removes it.
        """
        when = now or dt_util.now()
        cached_charges = self.cache.charges(when)
        for key, cached in cached_charges.items():
            tracker = self.trackers.get(key)
            if tracker is None:
                self.trackers[key] = ChargeTracker(key, cached.charge)
            else:
                tracker.update_charge(cached.charge)
        for charge in self.detail_charges() or []:
            key = charge_key(charge)
            if key not in cached_charges and key in self.trackers:
                self.trackers[key].update_charge(charge)

    def _is_new_billing_month(self, now: datetime) -> bool:
        """Check if we've crossed a billing cycle boundary."""
        if self.last_reset is None:
            return True
        if now.day >= self.billing_day:
            reset_date = now.replace(day=self.billing_day)
        else:
            first_of_month = now.replace(day=1)
            prev_month = first_of_month - timedelta(days=1)
            reset_date = prev_month.replace(day=self.billing_day)
        return self.last_reset < reset_date.replace(
            hour=0, minute=0, second=0, microsecond=0
        ) <= now

    def _reset_billing_month(self, now: datetime) -> None:
        """Close the month on every tracker, drop keys the tariff no longer lists, refresh.

        A key is dropped when neither the cached month nor the latest tariff detail lists
        it. An out-of-season charge is absent from the cache but listed by the detail.
        """
        for tracker in self.trackers.values():
            tracker.flush()
            tracker.reset_billing_month(now)
        self.last_reset = now
        # Without cover or a detail, which keys are gone is unknown, so none are removed.
        detail = self.detail_charges()
        if self.cache.covers(now) and detail is not None:
            listed = set(self.cache.charges(now)) | {charge_key(c) for c in detail}
            for key in [k for k in self.trackers if k not in listed]:
                _LOGGER.info("Demand charge %s is no longer in the tariff; removed", key)
                del self.trackers[key]
        self.cache.schedule_refresh(now)

    def _read_power_kw(self) -> float | None:
        """Return the power entity's reading in kW, None when it has no number."""
        state = self.hass.states.get(self.power_entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            power_kw = float(state.state)
        except (ValueError, TypeError):
            return None
        if state.attributes.get("unit_of_measurement", "") == "W":
            power_kw = power_kw / 1000.0
        return power_kw

    @callback
    def _async_sample(self, now: datetime) -> None:
        """Sample the power entity and feed every tracker."""
        now = dt_util.as_local(now)

        if self._is_new_billing_month(now):
            self._reset_billing_month(now)
        self.sync(now)

        power_kw = self._read_power_kw()
        if power_kw is None:
            return

        if not self.cache.covers(now):
            self._flush_all()
            return

        cached = self.cache.charges(now)
        for key, tracker in self.trackers.items():
            charge = cached.get(key)
            in_window = charge is not None and in_windows(charge.windows, now)
            tracker.sample(now, power_kw, in_window)

    def _flush_all(self) -> None:
        for tracker in self.trackers.values():
            tracker.flush()

    @callback
    def _async_handle_hass_stop(self, event: Event) -> None:
        """Flush every pending block before RestoreEntity snapshots state on shutdown.

        Runs inline as a @callback during the stop event, so it completes before the
        scheduled RestoreEntity dump (see the ha-tariff-au#8 investigation).
        The listener removed itself on firing, so its removal function is spent.
        """
        self._unsub_hass_stop = None
        self._flush_all()

    def start(self) -> None:
        """Start the 30 s sampling, the stop-event flush and the cache listener."""
        self._unsub_timer = async_track_time_interval(
            self.hass, self._async_sample, SAMPLE_INTERVAL
        )
        self._unsub_hass_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, self._async_handle_hass_stop
        )
        self._unsub_cache = self.cache.add_listener(self.sync)
        _LOGGER.info(
            "Demand manager started: entity=%s charges=%s",
            self.power_entity_id,
            list(self.trackers),
        )

    def stop(self) -> None:
        """Flush every pending block, then unsubscribe."""
        self._flush_all()
        for name in ("_unsub_timer", "_unsub_hass_stop", "_unsub_cache"):
            unsub = getattr(self, name)
            if unsub:
                unsub()
                setattr(self, name, None)

    def _legacy_restore_key(self) -> ChargeKey | None:
        """The key a 1.2.x blob restores onto: the first detail key the cache lists now.

        Falls back to the first detail key when the cache lists none of them.
        """
        cached = self.cache.charges(dt_util.now())
        listed = [k for k in self._detail_keys if k in cached and k in self.trackers]
        if listed:
            return listed[0]
        return next((k for k in self._detail_keys if k in self.trackers), None)

    def save_state(self) -> dict:
        """Return state for HA storage."""
        return {
            "version": STATE_VERSION,
            "last_reset": self.last_reset.isoformat() if self.last_reset else None,
            "trackers": [
                {
                    "period": key[0],
                    "months": list(key[1]) if key[1] is not None else None,
                    "charge": tracker.charge,
                    "state": tracker.save_state(),
                }
                for key, tracker in self.trackers.items()
            ],
        }

    def restore_state(self, data: dict) -> None:
        """Restore a saved blob: version 2, or a 1.2.x single-tracker blob.

        A 1.2.x blob holds the single tracker's state, so it goes to the charge in season
        now (see `_legacy_restore_key`).
        """
        last_reset = data.get("last_reset")
        self.last_reset = datetime.fromisoformat(last_reset) if last_reset else None

        if "version" not in data:
            key = self._legacy_restore_key()
            if key is not None:
                self.trackers[key].restore_state(data)
            return

        for saved in data.get("trackers", []):
            months = saved.get("months")
            key: ChargeKey = (saved["period"], tuple(months) if months is not None else None)
            state = saved.get("state", {})
            tracker = self.trackers.get(key)
            if tracker is None:
                # A key the tariff no longer lists bills as last saved until the reset.
                charge = {
                    **saved.get("charge", {"period": key[0], "months": months}),
                    "rate": state.get("rate"),
                }
                tracker = self.trackers[key] = ChargeTracker(key, charge)
            tracker.restore_state(state)
