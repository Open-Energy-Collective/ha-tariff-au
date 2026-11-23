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
# A month cut at an unseeded 1 July leaves the next 24 h uncovered; its range is refetched
# at most this often, so each update does not repeat the 404 and its retry.
CUT_MONTH_RETRY_AGE = timedelta(hours=1)
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
        self._month_attempted_at: datetime | None = None
        self._dropped_on_load = False
        # The tariff detail and DNSP time zone last fetched, so setup can start without them.
        self.saved_detail: dict | None = None
        self.saved_time_zone: str | None = None

    async def async_load(self) -> None:
        """Restore the cached months, tariff detail and time zone from the Store.

        Sends no request. A detail that is not a dict, or a zone that is not a non-empty
        string, is treated as not saved.
        """
        data = await self._store.async_load()
        if not data:
            return
        detail = data.get("detail")
        self.saved_detail = detail if isinstance(detail, dict) else None
        zone = data.get("dnsp_time_zone")
        self.saved_time_zone = zone if isinstance(zone, str) and zone else None
        fetched_at = data.get("fetched_at")
        self.fetched_at = datetime.fromisoformat(fetched_at) if fetched_at else None
        for saved in data.get("months", []):
            charges = parse_charges(saved["body"])
            if charges is None:
                continue
            start = datetime.fromisoformat(saved["start"])
            if not self._starts_a_billing_month(start):
                # Cached on an earlier billing day; the next refresh saves the Store without it.
                self._dropped_on_load = True
                continue
            self._months.append(
                _Month(start, datetime.fromisoformat(saved["end"]), saved["body"], charges)
            )

    async def async_save_service_data(self, detail: dict, time_zone: str) -> None:
        """Hold and save the tariff detail and DNSP time zone; no save when both are unchanged."""
        if detail == self.saved_detail and time_zone == self.saved_time_zone:
            return
        self.saved_detail = detail
        self.saved_time_zone = time_zone
        await self._store.async_save(self._serialise())

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
        changed = self._prune(now) or self._dropped_on_load
        self._dropped_on_load = False
        start, end = billing_month_range(dt_util.as_local(now), self._billing_day)
        stale = self.fetched_at is None or _utc(now) - _utc(self.fetched_at) >= REFRESH_AGE

        ranges = []
        if stale or not self.covers(now):
            ranges.append((start, end))
        ahead = now + REFRESH_AGE
        if not self.covers(ahead):
            ahead_range = billing_month_range(dt_util.as_local(ahead), self._billing_day)
            recently_tried = (
                ahead_range == (start, end)
                and self._month_attempted_at is not None
                and _utc(now) - _utc(self._month_attempted_at) < CUT_MONTH_RETRY_AGE
            )
            if ahead_range not in ranges and not recently_tried:
                ranges.append(ahead_range)

        for range_start, range_end in ranges:
            if (range_start, range_end) == (start, end):
                self._month_attempted_at = now
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

    def month_is_partial(self, when: datetime) -> bool:
        """Return True when the cached month holding `when` ends before its billing month does.

        A billing month crossing an unseeded 1 July is cached only up to that 1 July.
        """
        month = self._month_holding(when)
        if month is None:
            return False
        _, billing_end = billing_month_range(dt_util.as_local(when), self._billing_day)
        return _utc(month.end) < _utc(billing_end)

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

    def _starts_a_billing_month(self, start: datetime) -> bool:
        """Return True when `start` begins a billing month for the current billing day."""
        month_start, _ = billing_month_range(dt_util.as_local(start), self._billing_day)
        return _utc(month_start) == _utc(start)

    def _prune(self, now: datetime) -> bool:
        """Drop months that ended at or before `now`, or that start off the billing day.

        Returns True when any was dropped.
        """
        kept = [
            m
            for m in self._months
            if _utc(m.end) > _utc(now) and self._starts_a_billing_month(m.start)
        ]
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
        data = {
            "fetched_at": self.fetched_at.isoformat() if self.fetched_at else None,
            "months": [
                {"start": m.start.isoformat(), "end": m.end.isoformat(), "body": m.body}
                for m in self._months
            ],
        }
        # Written only when held, so a Store without them stays as 1.3.0 wrote it.
        if self.saved_detail is not None:
            data["detail"] = self.saved_detail
        if self.saved_time_zone is not None:
            data["dnsp_time_zone"] = self.saved_time_zone
        return data
