"""Tests for the pure per-charge window functions."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.oec_tariff_au.demand_windows import (
    billing_month_range,
    charge_key,
    in_windows,
    next_transition,
    parse_charges,
    window_hours,
)

SYDNEY = ZoneInfo("Australia/Sydney")
AEDT = timezone(timedelta(hours=11))
AEST = timezone(timedelta(hours=10))


def _entry(rate, months, windows):
    return {
        "period": "peak",
        "rate": rate,
        "unit": "kW",
        "start": "16:00",
        "end": "20:00",
        "days": "weekdays",
        "public_holidays": "excluded",
        "months": months,
        "measurement_method": "30min_max",
        "peak_period": "month",
        "windows": windows,
    }


def _win(day, tz="+11:00"):
    return {"start": f"2026-11-{day:02d}T16:00:00{tz}", "end": f"2026-11-{day:02d}T20:00:00{tz}"}


def _dt(day, hour, minute=0):
    return datetime(2026, 11, day, hour, minute, tzinfo=AEDT)


def test_charge_key_sorts_months():
    assert charge_key({"period": "peak", "months": [12, 1, 2]}) == ("peak", (1, 2, 12))
    assert charge_key({"period": "peak", "months": None}) == ("peak", None)


def test_parse_charges_keeps_seasonal_pair_apart():
    body = {
        "charges": [
            _entry(5.454, [1, 2, 3, 11, 12], [_win(17)]),
            _entry(2.781, [4, 5, 6, 7, 8, 9, 10], []),
        ]
    }
    parsed = parse_charges(body)
    assert set(parsed) == {
        ("peak", (1, 2, 3, 11, 12)),
        ("peak", (4, 5, 6, 7, 8, 9, 10)),
    }
    summer = parsed[("peak", (1, 2, 3, 11, 12))]
    assert summer.charge["rate"] == 5.454
    assert "windows" not in summer.charge
    assert len(summer.windows) == 1


def test_parse_charges_merges_versions_and_takes_latest_rate():
    body = {
        "charges": [
            _entry(6.0, [11], [_win(20), _win(19)]),
            _entry(5.0, [11], [_win(17), _win(18)]),
        ]
    }
    parsed = parse_charges(body)
    assert len(parsed) == 1
    merged = parsed[("peak", (11,))]
    assert merged.charge["rate"] == 6.0
    assert merged.windows == sorted(merged.windows)
    assert [w[0].day for w in merged.windows] == [17, 18, 19, 20]


def test_parse_charges_none_without_charges():
    assert parse_charges({"windows": []}) is None


def test_in_windows_start_inclusive_end_exclusive():
    windows = [(_dt(17, 16), _dt(17, 20))]
    assert in_windows(windows, _dt(17, 16))
    assert in_windows(windows, _dt(17, 19, 59))
    assert not in_windows(windows, _dt(17, 20))
    assert not in_windows(windows, _dt(17, 15, 59))


def test_in_windows_across_dst_end():
    windows = [
        (
            datetime(2026, 4, 5, 1, 0, tzinfo=AEDT),
            datetime(2026, 4, 5, 3, 0, tzinfo=AEST),
        )
    ]
    first = datetime(2026, 4, 5, 2, 30, tzinfo=AEDT)
    second = datetime(2026, 4, 5, 2, 30, tzinfo=AEST)
    assert in_windows(windows, first)
    assert in_windows(windows, second)
    # The Sydney zone resolves the same two instants by fold.
    assert in_windows(windows, datetime(2026, 4, 5, 2, 30, tzinfo=SYDNEY, fold=0))
    assert in_windows(windows, datetime(2026, 4, 5, 2, 30, tzinfo=SYDNEY, fold=1))
    assert not in_windows(windows, datetime(2026, 4, 5, 3, 0, tzinfo=AEST))


def test_next_transition():
    windows = [(_dt(17, 16), _dt(17, 20)), (_dt(18, 16), _dt(18, 20))]
    assert next_transition(windows, _dt(17, 17)) == _dt(17, 20)
    assert next_transition(windows, _dt(17, 21)) == _dt(18, 16)
    assert next_transition(windows, _dt(17, 16)) == _dt(17, 20)
    assert next_transition(windows, _dt(18, 20)) is None
    assert next_transition([], _dt(17, 12)) is None


def test_window_hours():
    windows = [(_dt(17, 16), _dt(17, 20)), (_dt(18, 16), _dt(18, 20))]
    assert window_hours(windows) == 8.0


def test_billing_month_range_day_1():
    now = datetime(2026, 11, 17, 9, 30, tzinfo=SYDNEY)
    start, end = billing_month_range(now, 1)
    assert start == datetime(2026, 11, 1, tzinfo=SYDNEY)
    assert end == datetime(2026, 12, 1, tzinfo=SYDNEY)


def test_billing_month_range_day_28():
    now = datetime(2027, 3, 1, 12, 0, tzinfo=SYDNEY)
    start, end = billing_month_range(now, 28)
    assert start == datetime(2027, 2, 28, tzinfo=SYDNEY)
    assert end == datetime(2027, 3, 28, tzinfo=SYDNEY)


def test_billing_month_range_year_rollover():
    now = datetime(2026, 12, 30, tzinfo=SYDNEY)
    start, end = billing_month_range(now, 15)
    assert start == datetime(2026, 12, 15, tzinfo=SYDNEY)
    assert end == datetime(2027, 1, 15, tzinfo=SYDNEY)
