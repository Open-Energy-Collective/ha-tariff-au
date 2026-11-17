"""Billing-month demand window cache, kept in an HA Store per config entry."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import DemandWindowsUnavailable
from .demand_windows import ChargeKey, ChargeWindows, billing_month_range, parse_charges

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
REFRESH_AGE = timedelta(hours=24)
MIN_SERVICE_VERSION = "0.10.0"


@dataclass
class _Month:
    """One cached range: [start, end) and the raw body with its parsed charges."""

    start: datetime
    end: datetime
    body: dict
    charges: dict[ChargeKey, ChargeWindows]

    def holds(self, when: datetime) -> bool:
        return _utc(self.start) <= _utc(when) < _utc(self.end)


def _utc(when: datetime) -> datetime:
    return dt_util.as_utc(when)


def _store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, STORAGE_VERSION, f"{DOMAIN}.demand_windows.{entry_id}")


async def async_remove_store(hass: HomeAssistant, entry_id: str) -> None:
    """Delete a config entry's cached windows from storage."""
    await _store(hass, entry_id).async_remove()


class DemandWindowCache:
    """The per-charge demand windows of the current (and, near its end, next) billing month."""

    def __init__(self, hass: HomeAssistant, coordinator, entry_id: str, billing_day: int) -> None:
        self._hass = hass
        self._coordinator = coordinator
        self._billing_day = billing_day
        self._store: Store = _store(hass, entry_id)
        self._months: list[_Month] = []
        self._listeners: list[Callable[[], None]] = []
        self._outage = False
        self._old_service_warned = False
        self._refresh_lock = asyncio.Lock()
        self.fetched_at: datetime | None = None

    async def async_load(self) -> None:
        """Restore the cached months from the Store. Sends no request."""
        data = await self._store.async_load()
        if not data:
            return
        fetched_at = data.get("fetched_at")
        self.fetched_at = datetime.fromisoformat(fetched_at) if fetched_at else None
        for saved in data.get("months", []):
            charges = parse_charges(saved["body"])
            if charges is None:
                continue
            self._months.append(
                _Month(
                    datetime.fromisoformat(saved["start"]),
                    datetime.fromisoformat(saved["end"]),
                    saved["body"],
                    charges,
                )
            )

    @callback
    def schedule_refresh(self, now: datetime) -> None:
        """Start `async_refresh_logged(now)` as a task."""
        self._hass.async_create_task(self.async_refresh_logged(now))

    async def async_refresh_logged(self, now: datetime) -> None:
        """Refresh, logging any failure; `_check_cover` warns while there is no cover."""
        try:
            await self.async_refresh(now)
        except Exception:
            _LOGGER.exception("Demand windows refresh failed")

    async def async_refresh(self, now: datetime) -> None:
        """Fetch the billing month holding `now` when stale or uncovered, and the one holding
        `now + 24 h` when that instant is uncovered. One refresh runs at a time."""
        async with self._refresh_lock:
            await self._async_refresh(now)

    async def _async_refresh(self, now: datetime) -> None:
        changed = self._prune(now)
        start, end = billing_month_range(dt_util.as_local(now), self._billing_day)
        stale = self.fetched_at is None or _utc(now) - _utc(self.fetched_at) >= REFRESH_AGE

        ranges = []
        if stale or not self.covers(now):
            ranges.append((start, end))
        ahead = now + REFRESH_AGE
        if not self.covers(ahead):
            ahead_range = billing_month_range(dt_util.as_local(ahead), self._billing_day)
            if ahead_range not in ranges:
                ranges.append(ahead_range)

        for range_start, range_end in ranges:
            month = await self._async_fetch(range_start, range_end, now)
            if month is None:
                continue
            self._months = [m for m in self._months if _utc(m.start) != _utc(month.start)]
            self._months.append(month)
            self._months.sort(key=lambda m: _utc(m.start))
            # Only the month holding `now` is kept fresh, so only its fetch restarts the 24 h.
            if (range_start, range_end) == (start, end):
                self.fetched_at = now
            changed = True

        if changed:
            await self._store.async_save(self._serialise())
            for listener in list(self._listeners):
                listener()
        self._check_cover(now)

    def covers(self, when: datetime) -> bool:
        """Return True when a cached month holds `when` (start inclusive, end exclusive)."""
        return self._month_holding(when) is not None

    def charges(self, when: datetime) -> dict[ChargeKey, ChargeWindows]:
        """Return the charges of the cached month holding `when`, {} when none does."""
        month = self._month_holding(when)
        return month.charges if month is not None else {}

    def month_end(self, when: datetime) -> datetime | None:
        """Return the end of the cached month holding `when`, None when none does."""
        month = self._month_holding(when)
        return month.end if month is not None else None

    def add_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` after the cache changes. Returns a function that removes it."""
        self._listeners.append(callback)

        def remove() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return remove

    def _month_holding(self, when: datetime) -> _Month | None:
        return next((m for m in self._months if m.holds(when)), None)

    def _prune(self, now: datetime) -> bool:
        """Drop months that ended at or before `now`. Returns True when any was dropped."""
        kept = [m for m in self._months if _utc(m.end) > _utc(now)]
        dropped = len(kept) != len(self._months)
        self._months = kept
        return dropped

    async def _async_fetch(self, start: datetime, end: datetime, now: datetime) -> _Month | None:
        """Fetch [start, end); on a 404, retry once ending at the next 1 July. None on failure.

        The retry is skipped when that 1 July is not after `now`, since its range could not
        cover anything still to come.
        """
        try:
            body = await self._coordinator.async_fetch_demand_windows(start, end)
        except DemandWindowsUnavailable as err:
            retry_end = self._next_first_july(start)
            retry_in_range = _utc(now) < _utc(retry_end) < _utc(end)
            if err.status != 404 or not retry_in_range:
                _LOGGER.debug("Demand windows %s to %s unavailable: %s", start, end, err)
                return None
            end = retry_end
            try:
                body = await self._coordinator.async_fetch_demand_windows(start, end)
            except DemandWindowsUnavailable as retry_err:
                _LOGGER.debug("Demand windows %s to %s unavailable: %s", start, end, retry_err)
                return None

        charges = parse_charges(body)
        if charges is None:
            if not self._old_service_warned:
                _LOGGER.warning(
                    "tariff-service returned demand windows without charges; "
                    "per-charge demand needs tariff-service %s or later",
                    MIN_SERVICE_VERSION,
                )
                self._old_service_warned = True
            return None
        self._old_service_warned = False
        return _Month(start, end, body, charges)

    def _next_first_july(self, start: datetime) -> datetime:
        """The first 1 July 00:00 strictly after `start`, on the DNSP's clock."""
        zone = self._coordinator.dnsp_time_zone or start.tzinfo
        local = start.astimezone(zone)
        candidate = datetime(local.year, 7, 1, tzinfo=zone)
        if _utc(candidate) <= _utc(start):
            candidate = datetime(local.year + 1, 7, 1, tzinfo=zone)
        return candidate

    def _check_cover(self, now: datetime) -> None:
        """Log one warning when cover is lost, and not again until it has been regained."""
        if self.covers(now):
            self._outage = False
            return
        if not self._outage:
            _LOGGER.warning(
                "No cached demand windows cover %s; demand is not measured until they do",
                now.isoformat(),
            )
            self._outage = True

    def _serialise(self) -> dict:
        return {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "months": [
                {"start": m.start.isoformat(), "end": m.end.isoformat(), "body": m.body}
                for m in self._months
            ],
        }
