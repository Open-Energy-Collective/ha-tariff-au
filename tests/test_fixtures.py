"""The hand-built demand-windows bodies against bodies recorded from tariff-service 0.10.0.

Most tests need a body for an arbitrary billing month, some outside the 2026-27 price
lists, so they keep the generator: `windows_coordinator` serves `range_body`. These tests
hold `range_body` to the recorded live bodies over `RECORDED_RANGE`. The comparison
matches window membership: per charge key, the UTC instants each body's windows cover,
cut to the body's range with touching windows joined. The integration itself neither
cuts nor joins windows: `window_hours` sums the raw cached windows.

#80 as built differs from `range_body` in form:
- live fills the top-level `windows`; `range_body` leaves it empty, and the integration
  does not read it;
- live states every window on the DNSP's local clock; `range_body` states a `standard`
  tariff's windows on the fixed standard offset (the same instants);
- live joins touching windows, so an `anytime` charge is one window for the range where
  `range_body` gives one per day (the same hours);
- live gives a charge without `public_holidays` the field as null.
"""

from datetime import datetime, timezone

import pytest

from custom_components.oec_tariff_au.demand_windows import charge_key, parse_charges

from .fixtures import RECORDED, RECORDED_RANGE, TARIFFS, in_season, range_body, tariff_zone

Interval = tuple[datetime, datetime]


def _normalised(body: dict) -> dict:
    """Per charge key: the windows as UTC intervals, cut to the range, touching joined."""
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
    """`range_body` for the recorded range. The range holds no public holiday."""
    recorded = RECORDED[tariff["tariff"]]
    return range_body(
        tariff["demand_charges"],
        in_season,
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
