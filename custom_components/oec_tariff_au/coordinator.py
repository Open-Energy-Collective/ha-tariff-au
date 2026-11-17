"""Data update coordinator for OEC Tariff."""

import logging
import math
from datetime import UTC, datetime, timedelta, tzinfo
from email.utils import parsedate_to_datetime

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_URL,
    CONF_DNSP,
    CONF_TARIFF,
    DEFAULT_API_URL,
    DEFAULT_RETRY_AFTER,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# hass.data key: API base URL (no trailing "/") -> UTC datetime before which no entry may
# call it. Keyed by URL because tariff-service rate-limits per remote IP, so every entry on
# this instance shares the budget. Held outside the coordinator so a failed first refresh,
# which sets the entry up again with a new coordinator, still honours it.
BACKOFF_UNTIL = f"{DOMAIN}_backoff_until"

# The tariff detail lists every demand charge, in season or not; it is refetched this often.
DETAIL_REFRESH_AGE = timedelta(hours=24)


def parse_retry_after(value: str | None, now: datetime) -> float | None:
    """Seconds to wait from a Retry-After header (delay-seconds or HTTP-date), None if unusable."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now).total_seconds())


class DemandWindowsUnavailable(Exception):
    """Demand windows could not be fetched. status is None for backoff and network errors."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OecTariffCoordinator(DataUpdateCoordinator):
    """Fetch tariff data from OEC API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.api_url = entry.data.get(CONF_API_URL, DEFAULT_API_URL)
        self.dnsp = entry.data[CONF_DNSP]
        self.tariff = entry.data[CONF_TARIFF]
        self._session = async_get_clientsession(hass)
        self.tariff_detail: dict | None = None
        self._detail_fetched_at: datetime | None = None
        self.dnsp_time_zone: tzinfo | None = None
        self._backoff_key = self.api_url.rstrip("/")
        self._backoff_until: dict[str, datetime] = hass.data.setdefault(BACKOFF_UNTIL, {})

        super().__init__(
            hass,
            _LOGGER,
            name=f"OEC Tariff ({self.dnsp}/{self.tariff})",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )

    def _backoff_remaining(self) -> float:
        """Seconds left of this entry's rate-limit backoff, 0 when none is active."""
        until = self._backoff_until.get(self._backoff_key)
        if until is None:
            return 0.0
        remaining = (until - dt_util.utcnow()).total_seconds()
        if remaining <= 0:
            del self._backoff_until[self._backoff_key]
            return 0.0
        return remaining

    def _rate_limited(self, remaining: float) -> UpdateFailed:
        # retry_after schedules the next refresh; +1 s covers the scheduler's sub-second rounding.
        return UpdateFailed(
            f"Rate limited by tariff-service, next request in {math.ceil(remaining)} s",
            retry_after=math.ceil(remaining) + 1,
        )

    def _start_backoff(self, resp) -> float:
        """Record the backoff a 429 asks for and log it. Returns its length in seconds."""
        now = dt_util.utcnow()
        seconds = parse_retry_after(resp.headers.get("Retry-After"), now)
        if seconds is None:
            seconds = DEFAULT_RETRY_AFTER
        self._backoff_until[self._backoff_key] = now + timedelta(seconds=seconds)
        _LOGGER.warning(
            "tariff-service at %s returned 429 (%s/%s), no requests from any entry for %d s",
            self._backoff_key,
            self.dnsp,
            self.tariff,
            math.ceil(seconds),
        )
        return seconds

    async def _async_resolve_time_zone(self) -> None:
        """Set dnsp_time_zone from the API. Demand windows are stated on the DNSP's clock."""
        try:
            async with self._session.get(f"{self.api_url}/dnsps", timeout=10) as resp:
                if resp.status == 429:
                    raise self._rate_limited(self._start_backoff(resp))
                if resp.status != 200:
                    raise UpdateFailed(f"API returned {resp.status}: {await resp.text()}")
                dnsps = await resp.json()
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error fetching DNSPs: {err}") from err

        name = next((d.get("timezone") for d in dnsps if d.get("code") == self.dnsp), None)
        time_zone = await dt_util.async_get_time_zone(name) if name else None
        if time_zone is None:
            raise UpdateFailed(f"No time zone for DNSP {self.dnsp}")
        self.dnsp_time_zone = time_zone

    async def async_fetch_demand_windows(self, start: datetime, end: datetime) -> dict:
        """Fetch the demand windows covering start to end (offset-aware datetimes)."""
        if (remaining := self._backoff_remaining()) > 0:
            raise DemandWindowsUnavailable(
                f"Rate limited by tariff-service, next request in {math.ceil(remaining)} s"
            )
        url = f"{self.api_url}/calculate/demand-windows"
        params = {
            "dnsp": self.dnsp,
            "tariff": self.tariff,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        try:
            async with self._session.get(url, params=params, timeout=10) as resp:
                if resp.status == 429:
                    self._start_backoff(resp)
                    raise DemandWindowsUnavailable("Rate limited by tariff-service", 429)
                if resp.status != 200:
                    raise DemandWindowsUnavailable(
                        f"API returned {resp.status}: {await resp.text()}", resp.status
                    )
                return await resp.json()
        except DemandWindowsUnavailable:
            raise
        except Exception as err:
            raise DemandWindowsUnavailable(f"Error fetching demand windows: {err}") from err

    async def _async_refresh_tariff_detail(self) -> None:
        """Fetch the tariff detail when it is missing or 24 h old.

        A failed fetch keeps the previous detail, logs a warning and is retried on the
        next update.
        """
        now = dt_util.utcnow()
        if self._detail_fetched_at is not None and now - self._detail_fetched_at < DETAIL_REFRESH_AGE:
            return
        detail_url = f"{self.api_url}/tariffs/{self.dnsp}/{self.tariff}"
        try:
            async with self._session.get(detail_url, timeout=10) as resp:
                if resp.status == 429:
                    self._start_backoff(resp)
                    return
                if resp.status != 200:
                    _LOGGER.warning(
                        "Failed to fetch tariff detail (API returned %s), will retry next update",
                        resp.status,
                    )
                    return
                self.tariff_detail = await resp.json()
                self._detail_fetched_at = now
        except Exception as err:
            _LOGGER.warning("Failed to fetch tariff detail (%s), will retry next update", err)

    async def _async_update_data(self) -> dict:
        """Fetch current rate from OEC API."""
        from datetime import datetime, timezone

        if (remaining := self._backoff_remaining()) > 0:
            raise self._rate_limited(remaining)

        now = datetime.now(timezone.utc).astimezone()
        dt_str = now.isoformat()

        # Fetch current rate
        url = f"{self.api_url}/calculate/current-rate"
        params = {"dnsp": self.dnsp, "tariff": self.tariff, "datetime": dt_str}

        try:
            async with self._session.get(url, params=params, timeout=10) as resp:
                if resp.status == 429:
                    raise self._rate_limited(self._start_backoff(resp))
                if resp.status != 200:
                    raise UpdateFailed(f"API returned {resp.status}: {await resp.text()}")
                rate_data = await resp.json()
        except UpdateFailed as err:
            if err.retry_after is not None:
                raise
            raise UpdateFailed(f"Error fetching current rate: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Error fetching current rate: {err}") from err

        if self.dnsp_time_zone is None:
            await self._async_resolve_time_zone()

        await self._async_refresh_tariff_detail()

        return {
            "rate": rate_data,
            "detail": self.tariff_detail,
        }
