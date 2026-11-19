"""The hand-built demand-windows bodies against bodies recorded from tariff-service 0.10.0.

Most tests need a body for an arbitrary billing month, some outside the 2026-27 price
lists, so they keep `windows_body`. These tests hold it to the recorded live bodies over
`RECORDED_RANGE`, compared the way the integration reads them: per charge key, as UTC
instants, clipped to the body's range, with touching windows merged.

#80 as built differs from `windows_body` only in form the integration does not read:
- live fills the top-level `windows`; `windows_body` leaves it empty;
- live states every window on the DNSP's local clock; `windows_body` states a
  `standard` tariff's windows on the fixed standard offset (the same instants);
- live clips windows to the range and merges touching ones, so an `anytime` charge
  is one window and an overnight window is cut at the range ends;
- live gives a charge without `public_holidays` the field as null.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from custom_components.oec_tariff_au.demand_windows import charge_key, parse_charges

from .fixtures import RECORDED, RECORDED_RANGE, TARIFFS, in_season, tariff_zone, windows_body

Interval = tuple[datetime, datetime]


def _on_charge_days(charge: dict, day: date):
    """A charge's own span on its months and days. The range holds no public holiday."""
    if charge["days"] == "weekdays" and day.weekday() >= 5:
        return None
    return in_season(charge, day)


def _normalised(body: dict) -> dict:
    """Per charge key: the windows as UTC intervals, clipped to the range, touching merged."""
    lower = datetime.fromisoformat(body["start"]).astimezone(timezone.utc)
    upper = datetime.fromisoformat(body["end"]).astimezone(timezone.utc)
    result = {}
    for key, cw in parse_charges(body).items():
        merged: list[Interval] = []
        for start, end in cw.windows:
            start = max(start.astimezone(timezone.utc), lower)
            end = min(end.astimezone(timezone.utc), upper)
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((start, end))
        if merged:
            result[key] = merged
    return result


def _generated(tariff: dict) -> dict:
    """`windows_body` for the recorded range, from the day before so overnight windows reach in."""
    recorded = RECORDED[tariff["tariff"]]
    first, end = RECORDED_RANGE
    days = [first - timedelta(days=1) + timedelta(days=n) for n in range((end - first).days + 1)]
    return windows_body(
        tariff["demand_charges"],
        days,
        _on_charge_days,
        tz=tariff_zone(tariff),
        dnsp=tariff["dnsp"],
        tariff=tariff["tariff"],
        start=datetime.fromisoformat(recorded["start"]),
        end=datetime.fromisoformat(recorded["end"]),
    )


@pytest.mark.parametrize("code", list(TARIFFS))
def test_recorded_body_covers_the_recorded_range(code):
    tariff, recorded = TARIFFS[code], RECORDED[code]
    zone = tariff_zone(tariff) if tariff["time_basis"] == "local" else None
    assert recorded["dnsp"] == tariff["dnsp"]
    assert recorded["tariff"] == tariff["tariff"]
    start = datetime.fromisoformat(recorded["start"])
    end = datetime.fromisoformat(recorded["end"])
    assert (start.date(), end.date()) == RECORDED_RANGE
    assert start.time() == end.time() == datetime.min.time()
    if zone is not None:
        assert start.utcoffset() == zone.utcoffset(start.replace(tzinfo=None))


@pytest.mark.parametrize("code", list(TARIFFS))
def test_recorded_charges_match_the_fixture_charges(code):
    """Each live entry carries the seed charge's fields, `public_holidays` null when unset."""
    fixture = {charge_key(c): c for c in TARIFFS[code]["demand_charges"]}
    for entry in RECORDED[code]["charges"]:
        fields = {k: v for k, v in entry.items() if k != "windows"}
        expected = {"public_holidays": None, **fixture[charge_key(entry)]}
        assert fields == expected


@pytest.mark.parametrize("code", list(TARIFFS))
def test_windows_body_reproduces_the_recorded_windows(code):
    tariff = TARIFFS[code]
    generated = _generated(tariff)
    recorded = RECORDED[code]
    assert {k: generated[k] for k in ("dnsp", "tariff", "start", "end")} == {
        k: recorded[k] for k in ("dnsp", "tariff", "start", "end")
    }
    assert _normalised(generated) == _normalised(recorded)
