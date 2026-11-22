"""Pure functions over the per-charge windows from /calculate/demand-windows."""

from dataclasses import dataclass
from datetime import datetime, timezone

ChargeKey = tuple[str, tuple[int, ...] | None]
Window = tuple[datetime, datetime]  # aware, half-open [start, end)


@dataclass
class ChargeWindows:
    """One charge's fields and every window it covers, sorted."""

    charge: dict
    windows: list[Window]


def charge_key(charge: dict) -> ChargeKey:
    """Return the identity of a charge: its period and sorted months."""
    months = charge.get("months")
    return (charge["period"], tuple(sorted(months)) if months is not None else None)


def parse_charges(body: dict) -> dict[ChargeKey, ChargeWindows] | None:
    """Group a demand-windows response by charge key.

    Returns None when the body has no `charges` key (a service before 0.10.0).
    Entries sharing a key merge their windows; the charge fields come from the
    entry whose first window starts last.
    """
    if "charges" not in body:
        return None
    grouped: dict[ChargeKey, ChargeWindows] = {}
    latest_start: dict[ChargeKey, datetime | None] = {}
    for entry in body["charges"]:
        key = charge_key(entry)
        windows = [
            (datetime.fromisoformat(w["start"]), datetime.fromisoformat(w["end"]))
            for w in entry.get("windows", [])
        ]
        fields = {k: v for k, v in entry.items() if k != "windows"}
        first = min((_utc(w[0]) for w in windows), default=None)
        if key not in grouped:
            grouped[key] = ChargeWindows(fields, windows)
            latest_start[key] = first
            continue
        existing = grouped[key]
        existing.windows.extend(windows)
        prior = latest_start[key]
        if first is not None and (prior is None or first >= prior):
            existing.charge = fields
            latest_start[key] = first
    for cw in grouped.values():
        cw.windows.sort(key=lambda w: (_utc(w[0]), _utc(w[1])))
    return grouped


def _utc(when: datetime) -> datetime:
    """Return the instant as UTC, so same-zone fold and wall-clock comparison cannot apply."""
    return when.astimezone(timezone.utc)


def in_windows(windows: list[Window], when: datetime) -> bool:
    """Return True when `when` is in any window (start inclusive, end exclusive)."""
    instant = _utc(when)
    return any(_utc(start) <= instant < _utc(end) for start, end in windows)


def next_transition(windows: list[Window], now: datetime) -> datetime | None:
    """Return the earliest window bound after `now` where in_windows flips."""
    current = in_windows(windows, now)
    instant = _utc(now)
    bounds = sorted({b for w in windows for b in w}, key=_utc)
    for bound in bounds:
        if _utc(bound) > instant and in_windows(windows, bound) != current:
            return bound
    return None


def next_bound(windows: list[Window], now: datetime) -> datetime | None:
    """Return the earliest window start or end after `now`, whether or not in_windows flips there."""
    instant = _utc(now)
    return min((b for w in windows for b in w if _utc(b) > instant), key=_utc, default=None)


def window_hours(windows: list[Window]) -> float:
    """Return the total length of the windows in hours."""
    return sum((_utc(end) - _utc(start)).total_seconds() for start, end in windows) / 3600


def billing_month_range(now: datetime, billing_day: int) -> tuple[datetime, datetime]:
    """Return [start, end) of the billing month holding `now`, in now's own zone.

    Start is local midnight on `billing_day` on or before `now`; end is the same
    day of the next month. `billing_day` is 1 to 28.
    """
    start = now.replace(day=billing_day, hour=0, minute=0, second=0, microsecond=0, fold=0)
    if start > now:
        start = _shift_month(start, -1)
    return start, _shift_month(start, 1)


def _shift_month(when: datetime, months: int) -> datetime:
    index = when.year * 12 + (when.month - 1) + months
    return when.replace(year=index // 12, month=index % 12 + 1, fold=0)
