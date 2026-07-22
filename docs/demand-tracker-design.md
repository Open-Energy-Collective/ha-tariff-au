# Demand Charge Tracker — Design Document

## Overview

The OEC Tariff HA integration will track real-time grid power consumption and calculate demand charges based on the DNSP's specific methodology. This is independent of any other integration (PowerSync, Amber, etc.) — it only needs a power sensor entity.

## User Configuration

During config flow (new optional step after tariff selection):

```
Step 3: "Configure demand tracking (optional)"
  - Power entity: [entity picker — device_class: power, unit: kW or W]
  - Billing day: [1-28, default: 1] (day of month the billing cycle resets)
```

If the user skips this step (or their tariff has no demand component), demand tracking is disabled.

## Data Flow

```
HA power sensor (kW)
    ↓ sampled every 30 seconds
Integration demand tracker
    ↓ applies measurement_method logic
    ↓ only records during demand window (time/days/season from API)
Month peak demand (kW)
    ↓ sent to API
/calculate/demand-surcharge
    ↓ returns charge amounts
HA sensors (peak kW, monthly $, $/kWh surcharge)
```

## Measurement Methods

The integration reads `demand.measurement_method` from the tariff detail and applies the appropriate tracking logic:

### `30min_avg` (Energex)
- Maintain a rolling buffer of samples over 30 minutes
- Calculate average kW for each completed 30-min block
- Track the highest 30-min average this billing month
- This is typically the "gentlest" method — spikes are smoothed out

### `30min_max` (Ausgrid, Endeavour, Essential, Evoenergy, Power Water, SAPN)
- Within each 30-min block, track the maximum instantaneous kW reading
- Track the highest such maximum this billing month
- More punishing than avg — a single spike in a 30-min block counts

### `monthly_peak` (AusNet)
- Track the absolute maximum kW reading during any demand window this month
- Simplest logic — just `max(all readings during window)`

### `rolling_12month_max` (Jemena)
- Track peak demand per month (same as monthly_peak per month)
- The chargeable demand is the max of the last 12 monthly peaks
- Persist 12 months of history (survives restarts via restore_state)

## Window Filtering

Before recording any sample, check ALL of the following:
1. **Time**: current local time is within `window_start` to `window_end`
2. **Day**: current day matches `days` (all / weekdays / weekends)
3. **Season**: current month is in `season_months` (or null = all months)

If any check fails, the sample is ignored (not counted toward peak demand).

## 30-Minute Block Alignment

Blocks align to clock half-hours (XX:00 and XX:30), not from when the integration starts. This matches how meters and DNSPs measure:
- Block 1: 16:00:00 – 16:29:59
- Block 2: 16:30:00 – 16:59:59
- etc.

## Sampling

- Poll the power entity every **30 seconds** via HA state listener (not API polling)
- Convert to kW if entity reports in W
- Store samples in memory for current 30-min block
- At block boundary: calculate block result (avg or max depending on method), compare to month peak

## Persistence

- **Month peak demand**: stored via HA `RestoreEntity` (survives restarts)
- **Current 30-min block samples**: lost on restart (acceptable — max 30 min of data lost)
- **12-month history** (Jemena only): stored via `RestoreEntity`
- **Billing month reset**: on configured billing day (default: 1st), peak resets to 0

## Sensors Created

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.oec_tariff_month_peak_demand` | kW | Highest measured demand this billing month |
| `sensor.oec_tariff_monthly_demand_charge` | $ | peak_kW × demand_rate (from API) |
| `sensor.oec_tariff_demand_surcharge_per_kwh` | $/kWh | Amortized demand charge (from API) |

### Attributes on `month_peak_demand`:
- `measurement_method`: which method is being used
- `demand_window`: "16:00-21:00"
- `demand_days`: "all" / "weekdays"
- `season_months`: [1,2,3,...] or null
- `billing_day`: 1
- `last_reset`: ISO datetime of last billing reset
- `current_block_start`: current 30-min block start time
- `current_block_samples`: number of samples in current block

## API Interaction

On every coordinator update (every 5 min), if peak demand has changed:
- Call `GET /calculate/demand-surcharge?dnsp=X&tariff=Y&peak_demand_kw=Z`
- Update `monthly_demand_charge` and `demand_surcharge_per_kwh` sensors

## Edge Cases

- **Power entity unavailable**: skip sample, don't count as 0
- **HA restart mid-block**: lose current block samples, resume on next boundary
- **Tariff with no demand component**: demand tracking disabled, sensors not created
- **Season change mid-month**: window filtering handles this automatically (each sample checked individually)
- **Billing day reset**: peak goes to 0, previous month's peak available as attribute `previous_month_peak`
- **Unit conversion**: if entity unit is W, divide by 1000 for kW

## Not In Scope (Phase 1)

- Historical demand charge graphing
- Forecast/prediction of end-of-month demand charge
- Multiple demand windows per tariff (Endeavour has high/low season rates)
- kVA-based demand (some business tariffs) — kW only for now
