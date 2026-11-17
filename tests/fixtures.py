"""Hand-written tariff-service fixtures for the per-charge demand tests.

Each tariff dict holds its `demand_charges` list copied verbatim from tariff-service
`seed/*.json`, with the DNSP's time zone and the tariff's time basis. `windows_body`
builds a `/calculate/demand-windows` body by hand from tariff-service #80's schema.
"""

from collections.abc import Callable, Sequence
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

NASN12 = {'tariff': 'NASN12',
 'dnsp': 'ausnet',
 'timezone': 'Australia/Melbourne',
 'time_basis': 'local',
 'demand_charges': [{'period': 'peak',
                     'rate': 11.473,
                     'unit': 'kW',
                     'start': '15:00',
                     'end': '21:00',
                     'days': 'weekdays',
                     'public_holidays': 'included',
                     'months': [1, 2, 3, 12],
                     'measurement_method': 'monthly_peak',
                     'peak_period': 'month'},
                    {'period': 'peak',
                     'rate': 2.862,
                     'unit': 'kW',
                     'start': '15:00',
                     'end': '21:00',
                     'days': 'weekdays',
                     'public_holidays': 'included',
                     'months': [4, 5, 6, 7, 8, 9, 10, 11],
                     'measurement_method': 'monthly_peak',
                     'peak_period': 'month'}]}

N72 = {'tariff': 'N72',
 'dnsp': 'endeavour',
 'timezone': 'Australia/Sydney',
 'time_basis': 'local',
 'demand_charges': [{'period': 'peak',
                     'rate': 5.454,
                     'unit': 'kW',
                     'start': '16:00',
                     'end': '20:00',
                     'days': 'weekdays',
                     'public_holidays': 'excluded',
                     'months': [1, 2, 3, 11, 12],
                     'measurement_method': '30min_max',
                     'peak_period': 'month'},
                    {'period': 'peak',
                     'rate': 2.781,
                     'unit': 'kW',
                     'start': '16:00',
                     'end': '20:00',
                     'days': 'weekdays',
                     'public_holidays': 'excluded',
                     'months': [4, 5, 6, 7, 8, 9, 10],
                     'measurement_method': '30min_max',
                     'peak_period': 'month'}]}

EVO_023 = {'tariff': '023',
 'dnsp': 'evoenergy',
 'timezone': 'Australia/Sydney',
 'time_basis': 'standard',
 'demand_charges': [{'period': 'peak',
                     'rate': 6.5424,
                     'unit': 'kW',
                     'start': '17:00',
                     'end': '21:00',
                     'days': 'all',
                     'months': [6, 7, 8],
                     'measurement_method': '30min_max',
                     'peak_period': 'month'},
                    {'period': 'peak',
                     'rate': 3.9249,
                     'unit': 'kW',
                     'start': '17:00',
                     'end': '21:00',
                     'days': 'all',
                     'months': [1, 2, 3, 4, 5, 9, 10, 11, 12],
                     'measurement_method': '30min_max',
                     'peak_period': 'month'},
                    {'period': 'off_peak',
                     'rate': 0.6228,
                     'unit': 'kW',
                     'start': '21:00',
                     'end': '09:00',
                     'days': 'all',
                     'months': None,
                     'measurement_method': '30min_max',
                     'peak_period': 'month'}]}

EA302 = {'tariff': 'EA302',
 'dnsp': 'ausgrid',
 'timezone': 'Australia/Sydney',
 'time_basis': 'local',
 'demand_charges': [{'period': 'peak',
                     'rate': 14.50191,
                     'unit': 'kW',
                     'start': '15:00',
                     'end': '21:00',
                     'days': 'weekdays',
                     'public_holidays': 'excluded',
                     'months': None,
                     'measurement_method': '30min_max',
                     'peak_period': 'rolling_12_months'}]}

A230 = {'tariff': 'A230',
 'dnsp': 'jemena',
 'timezone': 'Australia/Melbourne',
 'time_basis': 'local',
 'demand_charges': [{'period': 'anytime',
                     'rate': 6.49075,
                     'unit': 'kW',
                     'start': '00:00',
                     'end': '00:00',
                     'days': 'all',
                     'months': None,
                     'measurement_method': 'rolling_12month_max',
                     'peak_period': 'rolling_12_months'}]}

LBAD = {'tariff': 'LBAD',
 'dnsp': 'sapn',
 'timezone': 'Australia/Adelaide',
 'time_basis': 'local',
 'demand_charges': [{'period': 'peak',
                     'rate': 10.224,
                     'unit': 'kVA',
                     'start': '17:00',
                     'end': '21:00',
                     'days': 'all',
                     'months': [11, 12, 1, 2, 3],
                     'measurement_method': '30min_max',
                     'peak_period': 'rolling_12_months'},
                    {'period': 'anytime',
                     'rate': 1.89,
                     'unit': 'kVA',
                     'start': '00:00',
                     'end': '00:00',
                     'days': 'all',
                     'months': None,
                     'measurement_method': '30min_max',
                     'peak_period': 'rolling_12_months'}]}

TARIFFS = {t["tariff"]: t for t in (NASN12, N72, EVO_023, EA302, A230, LBAD)}

Span = tuple[str, str] | None
SpanFn = Callable[[dict, date], Span]


def tariff_zone(tariff: dict) -> tzinfo:
    """Return the zone a tariff's windows are stated on.

    `local` is the DNSP's own zone; `standard` is its fixed standard-time offset all year.
    """
    zone = ZoneInfo(tariff["timezone"])
    if tariff["time_basis"] == "standard":
        return timezone(zone.utcoffset(datetime(2026, 6, 1)))
    return zone


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def windows_body(
    charges: list[dict],
    days: Sequence[date],
    spans: Sequence[Span] | SpanFn | None = None,
    *,
    tz: tzinfo,
    dnsp: str = "ausnet",
    tariff: str = "NASN12",
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    """windows_body(charges, days, spans=None, *, tz, dnsp, tariff, start, end) -> dict

    Build a `/calculate/demand-windows` body: `{"dnsp", "tariff", "start", "end",
    "windows": [], "charges": [charge + {"windows": [...]}]}` with offset ISO strings.

    - `charges`: the charge dicts, e.g. `NASN12["demand_charges"]`.
    - `days`: the dates each charge gets a window on.
    - `spans`: per-charge `("HH:MM", "HH:MM")` or None (no windows), aligned with
      `charges`; or a callable `(charge, day) -> span or None`; or None for each
      charge's own `start` and `end`. An end at or before the start is on the next day,
      so `("00:00", "00:00")` is the whole day.
    - `tz`: the zone the spans are on (see `tariff_zone`).
    - `start`, `end`: the body's range; default the first day's midnight to the
      midnight after the last day.
    """

    def span_for(index: int, charge: dict, day: date) -> Span:
        if spans is None:
            return charge["start"], charge["end"]
        if callable(spans):
            return spans(charge, day)
        return spans[index]

    entries = []
    for index, charge in enumerate(charges):
        windows = []
        for day in days:
            span = span_for(index, charge, day)
            if span is None:
                continue
            opens = datetime.combine(day, _clock(span[0]), tzinfo=tz)
            closes = datetime.combine(day, _clock(span[1]), tzinfo=tz)
            if closes <= opens:
                closes = datetime.combine(day + timedelta(days=1), _clock(span[1]), tzinfo=tz)
            windows.append({"start": opens.isoformat(), "end": closes.isoformat()})
        entries.append({**charge, "windows": windows})

    if start is None:
        start = datetime.combine(min(days), time(0), tzinfo=tz)
    if end is None:
        end = datetime.combine(max(days) + timedelta(days=1), time(0), tzinfo=tz)
    return {
        "dnsp": dnsp,
        "tariff": tariff,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "windows": [],
        "charges": entries,
    }
