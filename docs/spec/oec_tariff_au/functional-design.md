# OEC Tariff integration: functional design

## 1. Overview

`oec_tariff_au` is a Home Assistant custom integration that shows an Australian network
tariff's prices and measures the demand charge a site is billed on that tariff. It backs
PRICE-05 (the home's network tariffs are known) and PRICE-06 (demand windows come from the
tariff).

- **Boundary.** It owns its config entries, one HA Store per entry, and the sensors of each
  entry's device. It does not own tariff data: every rate, window and charge comes from
  tariff-service. It does not meter power: it samples one power entity the site already has.
- **Reads.** tariff-service over HTTPS (default `https://api.openenergy.org.au/api/v1`), and the
  configured power entity's state.
- **Writes.** Sensor states, the entry's Store, and RestoreEntity extra data on the Month
  Peak Demand sensor.
- **Independence.** No pip requirements; HA's aiohttp session only. Business logic
  (`demand_windows.py`, `charge_tracker.py`) imports no `hass`.

## 2. Data Model

### Config entry data

| Key | Type | Purpose |
|---|---|---|
| `dnsp` | str | DNSP code, as tariff-service lists it |
| `tariff` | str | tariff code within the DNSP |
| `api_url` | str | tariff-service base URL; default above |
| `power_entity` | str, optional | entity id of a `power` sensor in W or kW; demand is measured only when set |
| `billing_day` | int 1 to 28 | day the billing month starts; default 1 |

### Store `oec_tariff_au.demand_windows.<entry_id>`, version 1

| Key | Type | Purpose |
|---|---|---|
| `fetched_at` | ISO datetime or null | when the month holding now was last fetched |
| `months` | list of `{start, end, body}` | cached ranges `[start, end)`; `body` is the `/calculate/demand-windows` response verbatim |
| `detail` | object, optional | the last tariff detail (`/tariffs/<dnsp>/<tariff>`) |
| `dnsp_time_zone` | str, optional | the DNSP's IANA time zone |

`detail` and `dnsp_time_zone` are written only when held, so a Store without them reads as
one with neither saved. The Store is deleted when the entry is deleted.

### Month Peak Demand restore data (state version 2)

`{version: 2, last_reset, trackers: [{period, months, charge, state}]}`, where `state` holds
`month_peak_kw`, `month_peak_recorded_at`, `previous_month_peak_kw`, `monthly_peaks_12`
(list of `[kW, ISO time or null]`, at most 12), `last_reset` and `rate`. A blob without
`version` is a single-tracker blob and restores onto one charge (rule 3.12).

## 3. Business Rules

### Setup and refresh

3.1 **Setup with a saved detail.** When the Store holds a `detail`, setup seeds the
coordinator with it and with the saved time zone, loads the platforms, and starts the rate
update and the windows refresh as entry background tasks. Setup does not wait on
tariff-service. A saved zone that is not a valid IANA key counts as missing and is resolved
by the first update.

3.2 **First start.** With no saved detail, setup fetches the rate, the DNSP time zone, the
tariff detail and the windows before the platforms load. A failed rate or time zone request,
a 429, an active backoff, or a detail still missing raises `ConfigEntryNotReady`, so HA
retries setup.

3.3 **Saving service data.** After every coordinator update, the detail and the time zone
name are saved to the Store when both are known and either changed.

3.4 **Rate update.** Every 300 s the coordinator fetches `/calculate/current-rate` for now.
The DNSP time zone is fetched from `/dnsps` when not yet known. The tariff detail is fetched
when missing or 24 h old; a failed detail fetch keeps the previous detail and is retried on
the next update. A seeded detail counts as never fetched, so the first update refetches it.

3.5 **Shared backoff.** A 429 from tariff-service starts a backoff, keyed by the API base URL
and shared by every entry and the config flow on the instance. Its length is the
`Retry-After` header (seconds or HTTP date), else 60 s. No request goes to that URL until it
ends; the config flow shows `rate_limited`.
Check: `Retry-After: 120` at 10:00:00 gives no request before 10:02:00.

### Demand windows

3.6 **Billing month.** The billing month holding `now` is `[start, end)`, where `start` is
local midnight on `billing_day` on or before `now` and `end` is the same day a month later.
Check: billing day 15, now 2026-11-10 12:00 gives `[2026-10-15 00:00, 2026-11-15 00:00)`.

3.7 **Cache refresh.** A refresh fetches the billing month holding now when it is 24 h old
or does not cover now, and the month holding `now + 24 h` when that instant is uncovered.
One refresh runs at a time. Months that ended, or that start off the current billing day,
are dropped. A refresh runs at setup, after each coordinator update, and at each billing
reset.

3.8 **1 July cut.** A 404 for a range crossing 1 July (DNSP clock) is retried once ending at
that 1 July, provided it is still ahead. The cached month is then partial. A partial month
holding now is refetched at most once an hour.

3.9 **Service version.** A demand-windows body with no `charges` key (tariff-service before
0.10.0) is not cached, and one warning is logged until a body with `charges` arrives.

3.10 **Charges.** A charge's identity is `(period, sorted months)`. Entries sharing a key
merge their windows; the charge fields come from the entry whose first window starts last.
A window is half-open, `start <= t < end`, compared as UTC instants.

### Demand measurement

3.11 **Sampling.** Every 30 s, with a power entity configured and the tariff detail listing
`demand_charges`, the manager:
1. resets the billing month when its start has passed since `last_reset` (rule 3.13);
2. aligns the trackers with the cached charges (a new key gets a tracker; an out-of-season
   key takes the detail's charge);
3. reads the power entity: `unknown`, `unavailable` or a non-number is skipped; a `W` unit is
   divided by 1000;
4. when no cached month covers now, flushes every pending block and records nothing;
5. otherwise feeds each tracker the reading, in window when its charge's windows hold now.

Check: `4500` with unit `W` is sampled as `4.5` kW.

3.12 **Trackers.** Each charge key has one tracker. An out-of-window reading flushes a
pending block and is dropped. Blocks are clock-aligned 30 min (`:00`, `:30`). Per
`measurement_method` (default `30min_avg`):

| Method | Block value | Month peak |
|---|---|---|
| `30min_avg` | mean of the block's readings | highest block value |
| `30min_max`, `rolling_12month_max` | highest reading in the block | highest block value |
| `monthly_peak` | none | highest single reading |

Peaks are rounded to 3 decimals. `peak_period: rolling_12_months` makes the chargeable
demand the highest of this month's peak and the last 12 closed months; otherwise it is this
month's peak. A single-tracker restore blob goes to the first detail charge the cache lists
now, else the first detail charge.
Check: readings 4.0 and 6.0 kW in one block give 5.0 (`30min_avg`) or 6.0 (`30min_max`).

3.13 **Billing reset.** At the first sample on or after local midnight of `billing_day`, each
tracker flushes, records its peak as the previous month's and in its 12-month history, and
starts at 0. A key neither the cached month nor the latest detail lists is removed, unless
there is no cover or no detail listing charges, when none is removed. A restored key the
tariff no longer lists bills at its saved rate until then.

3.14 **Stop.** On HA stop, every pending block is flushed before RestoreEntity saves state.

3.15 **kVA charges.** A charge with `unit: kVA` is measured from the kW power entity, as a
stand-in, and marked `measured_as: kW`.

### Entities and availability

3.16 **Tariff sensors** (Current Rate, Current Period, Daily Supply Charge, Tariff Name, and,
when the detail lists demand charges, Demand Rate) are unavailable until tariff-service has
answered since this start, and while the last update failed. Demand Rate is the highest rate
among the charges in season now, `0.0` when none is.

3.17 **Demand Window** is `active` while any cached charge's window holds now, else
`inactive`, with `active_charges` listing the periods. It is unavailable while no cached
month covers now, whatever tariff-service's state. It writes its state at each window bound
and at the end of the cached month.

3.18 **Demand sensors** (Month Peak Demand, Monthly Demand Charge, Demand Surcharge) are
always available and write their state whenever a sample changes a peak or resets the
month, through a tariff-service outage too.
- Month Peak Demand is the highest chargeable demand across the charges, in kW.
- Monthly Demand Charge is the sum over charges of chargeable demand times rate, rounded to
  cents. Check: 5.0 kW at 10 $/kW plus 2.0 kW at 5 $/kW gives `60.00`.
- Demand Surcharge is the sum, over charges with a peak and cached windows, of rate divided
  by that charge's window hours in the cached month. It is unknown while the month holding
  now is partial (`partial_month: true`). Check: 10 $/kW over 100 window hours gives `0.1`.

3.19 **Entity set.** The entity set is fixed at setup from the detail then held: a tariff
that gains demand charges creates their entities at the next reload. A billing day change
reloads the entry.

## 4. API Specification

### Consumed (tariff-service, external)

| Request | Used for |
|---|---|
| `GET /dnsps` | DNSP list (config flow) and the DNSP's `timezone` |
| `GET /tariffs/<dnsp>` | tariff list (config flow) |
| `GET /tariffs/<dnsp>/<tariff>` | tariff detail: `daily_supply_charge`, `demand_charges` (coordinator and config flow) |
| `GET /calculate/current-rate?dnsp&tariff&datetime` | the rate now, its period, in-season `demand_charges` |
| `GET /calculate/demand-windows?dnsp&tariff&start&end` | per-charge windows for a range; needs 0.10.0 or later |

Every request has a 10 s timeout and honours the shared backoff (rule 3.5).

### Served (Home Assistant, internal)

One device per entry, `Tariff (<dnsp>/<tariff>)`, holding the sensors of rules 3.16 to 3.18.
The config flow has three steps: DNSP, tariff, and the optional power entity and billing
day. The options flow changes the power entity and billing day.

## 5. Non-Functional Notes

- Requests per entry: one rate request per 5 min, one detail request per 24 h, and per windows refresh
  at most two ranges, each retried once on a 404. tariff-service rate-limits per remote IP, hence the
  shared backoff.
- An entry with a saved detail starts with no network request; its sensors read from the
  Store and restore data until tariff-service answers.
- Demand is measured through a tariff-service outage for as long as the cached months cover
  now (up to the end of the next billing month near a month end).
- No new infrastructure.

## 6. Project Structure

```
custom_components/oec_tariff_au/
├── __init__.py         setup paths, save of service data, unload, Store removal
├── config_flow.py      config and options flow
├── const.py            domain, config keys, defaults
├── coordinator.py      tariff-service requests, shared backoff
├── window_cache.py     the Store: cached months, detail, time zone
├── demand_windows.py   pure: charge keys, window membership, billing month
├── charge_tracker.py   pure: one charge's blocks and peaks
├── demand_manager.py   30 s sampling, billing reset, restore
├── sensor.py           tariff sensors, Demand Window
└── demand_sensors.py   demand sensors
```

`demand_windows.py` and `charge_tracker.py` import nothing from `homeassistant`.

## 7. Contract changes

None. This document states the integration as built; the Store layout above (version 1,
optional `detail` and `dnsp_time_zone`) and the restore data (state version 2) are the
current formats.
