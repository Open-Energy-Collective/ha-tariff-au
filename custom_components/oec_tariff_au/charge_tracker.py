"""Per-charge demand accumulator. Pure state: no hass, timers or billing-day logic."""

import logging
from datetime import datetime

from .demand_windows import ChargeKey

_LOGGER = logging.getLogger(__name__)


class ChargeTracker:
    """Accumulates peak demand for one demand charge."""

    def __init__(self, key: ChargeKey, charge: dict) -> None:
        """Initialize."""
        self.key = key
        self.charge = charge

        self.month_peak_kw: float = 0.0
        self.month_peak_recorded_at: datetime | None = None
        self.previous_month_peak_kw: float = 0.0
        self.current_block_samples: list[float] = []
        self.current_block_start: datetime | None = None
        self.last_reset: datetime | None = None
        self.monthly_peaks_12: list[tuple[float, datetime | None]] = []

    def update_charge(self, charge: dict) -> None:
        """Replace the charge definition (rate, method, period)."""
        self.charge = charge

    @staticmethod
    def _get_block_start(now: datetime) -> datetime:
        """Get the start of the clock-aligned 30-min block."""
        minute = 0 if now.minute < 30 else 30
        return now.replace(minute=minute, second=0, microsecond=0)

    @property
    def _method(self) -> str:
        return self.charge.get("measurement_method", "30min_avg")

    def _process_block(self) -> None:
        """Process a completed 30-min block and update peak if needed."""
        if not self.current_block_samples:
            return

        if self._method in ("30min_max", "monthly_peak", "rolling_12month_max"):
            block_value = max(self.current_block_samples)
        else:
            block_value = sum(self.current_block_samples) / len(self.current_block_samples)

        if block_value > self.month_peak_kw:
            self.month_peak_kw = round(block_value, 3)
            self.month_peak_recorded_at = self.current_block_start
            _LOGGER.debug("New month peak demand: %.3f kW", self.month_peak_kw)

    def flush(self) -> None:
        """Process and clear any in-progress block."""
        self._process_block()
        self.current_block_samples = []
        self.current_block_start = None

    def sample(self, now: datetime, power_kw: float, in_window: bool) -> None:
        """Record one reading. An out-of-window reading flushes a pending block and is dropped."""
        if not in_window:
            if self.current_block_samples:
                self.flush()
            return

        block_start = self._get_block_start(now)
        if self.current_block_start != block_start:
            self._process_block()
            self.current_block_samples = []
            self.current_block_start = block_start

        if self._method == "monthly_peak":
            if power_kw > self.month_peak_kw:
                self.month_peak_kw = round(power_kw, 3)
                self.month_peak_recorded_at = now
        else:
            self.current_block_samples.append(power_kw)

    def reset_billing_month(self, now: datetime) -> None:
        """Roll the month peak into history and start a new billing month."""
        self.previous_month_peak_kw = self.month_peak_kw
        self.monthly_peaks_12.append((self.month_peak_kw, self.month_peak_recorded_at))
        if len(self.monthly_peaks_12) > 12:
            self.monthly_peaks_12 = self.monthly_peaks_12[-12:]
        self.month_peak_kw = 0.0
        self.month_peak_recorded_at = None
        self.last_reset = now
        _LOGGER.info(
            "Demand charge %s billing reset. Previous peak: %.3f kW",
            self.key,
            self.previous_month_peak_kw,
        )

    def _chargeable_peak(self) -> tuple[float, datetime | None]:
        """Return (value, recorded_at) for whichever peak is chargeable."""
        if self.charge.get("peak_period") == "rolling_12_months":
            candidates = [*self.monthly_peaks_12, (self.month_peak_kw, self.month_peak_recorded_at)]
            return max(candidates, key=lambda peak: peak[0])
        return self.month_peak_kw, self.month_peak_recorded_at

    @property
    def chargeable_demand(self) -> float:
        """Return the demand value used for charging."""
        return self._chargeable_peak()[0]

    @property
    def chargeable_demand_recorded_at(self) -> datetime | None:
        """Return when the chargeable peak was recorded."""
        return self._chargeable_peak()[1]

    def restore_state(self, data: dict) -> None:
        """Restore state from a saved blob. The rate in the blob is not applied."""
        self.month_peak_kw = data.get("month_peak_kw", 0.0)
        self.previous_month_peak_kw = data.get("previous_month_peak_kw", 0.0)

        recorded_at = data.get("month_peak_recorded_at")
        self.month_peak_recorded_at = datetime.fromisoformat(recorded_at) if recorded_at else None

        # monthly_peaks_12 entries are [value, iso-timestamp]; older blobs hold plain floats.
        peaks = []
        for entry in data.get("monthly_peaks_12", []):
            if isinstance(entry, (list, tuple)):
                value, ts = entry[0], entry[1] if len(entry) > 1 else None
            else:
                value, ts = entry, None
            peaks.append((value, datetime.fromisoformat(ts) if ts else None))
        self.monthly_peaks_12 = peaks

        last_reset = data.get("last_reset")
        if last_reset:
            self.last_reset = datetime.fromisoformat(last_reset)

    def save_state(self) -> dict:
        """Return state for HA storage."""
        return {
            "month_peak_kw": self.month_peak_kw,
            "month_peak_recorded_at": (
                self.month_peak_recorded_at.isoformat() if self.month_peak_recorded_at else None
            ),
            "previous_month_peak_kw": self.previous_month_peak_kw,
            "monthly_peaks_12": [
                [value, ts.isoformat() if ts else None] for value, ts in self.monthly_peaks_12
            ],
            "last_reset": self.last_reset.isoformat() if self.last_reset else None,
            "rate": self.charge.get("rate"),
        }
