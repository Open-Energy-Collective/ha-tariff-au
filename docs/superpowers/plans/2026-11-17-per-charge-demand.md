---
scope: ha-tariff-au
status: draft
priority: now
depends_on: []
tracking: [ha-tariff-au#16, tariff-service#80]
---

# Per-charge Demand Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure every demand charge on its own, in the windows tariff-service lists for it, and add the charges together.

**Architecture:**
- The coordinator gains a fetch for `/calculate/demand-windows`.
- A `DemandWindowCache` holds the billing month's per-charge windows in a `Store`.
- A `DemandManager` samples the power entity once every 30 s and feeds one `ChargeTracker` per charge key `(period, months)`.
- The sensors read the manager and the cache. The local window logic and every read of the tariff detail's `demand` object are deleted.

**Tech Stack:** Home Assistant custom integration (Python 3.13+), `DataUpdateCoordinator`, `RestoreEntity`, `homeassistant.helpers.storage.Store`, pytest with `pytest-homeassistant-custom-component`.

**Spec:** `docs/superpowers/specs/2026-11-17-per-charge-demand-design.md` (approved 2026-11-17).

**Starts from:** `feat/1.3.0` with #18 and #19 integrated (the coordinator's shared 429 backoff, `BACKOFF_UNTIL`, `_start_backoff`, `_backoff_remaining`, `_rate_limited`).

## Global Constraints

- No new pip requirement. `manifest.json` `requirements` stays empty.
- Entity IDs, unique IDs, units, device classes and state classes stay unchanged.
- Requires tariff-service `0.10.0` (`/calculate/demand-windows` `charges`, tariff-service #80).
- A charge key is `(period, months)`. `months` is `None` or a sorted `tuple[int, ...]`.
- No window cover means no measurement. Never assume "in window".
- `unit: kVA` charges are measured from the kW power entity and marked `"measured_as": "kW"`.
- The billing day stays on the HA instance's local clock, as now. Window instants are compared as aware datetimes.
- Every request goes through the coordinator's shared backoff: none is sent while `_backoff_remaining() > 0`.
- All tests pass before every commit: `.venv/bin/python -m pytest tests/ -q`.
- No em dashes in code, comments, docs or commit messages. Commits carry no Claude attribution.

## Review Focus

1. **Restart during an outage.** The Store holds the month and the service is down. Measuring continues and Demand Window keeps scheduling. Test: Task 3, `test_cache_loads_from_store_without_fetching`.
2. **Two entries during a 429 backoff.** A windows refresh during backoff sends no request and keeps the cached month. Test: Task 1, `test_fetch_demand_windows_honours_shared_backoff`.
3. **Daylight saving end inside a window.** The repeated hour on a local-clock tariff counts once per instant, and the block alignment does not double a block. Test: Task 2, `test_in_windows_across_dst_end`.
4. **First setup with the service down and no Store.** Setup completes, the demand entities exist, and Demand Window is unavailable. Test: Task 6, `test_setup_with_no_cover_creates_entities_unavailable`.
5. **Billing day 28, February.** The range runs from 28 February to 28 March. A sample on 1 March is in the February month. Test: Task 2, `test_billing_month_range_day_28`.

---

### Task 1: Coordinator fetches demand windows

**Files:**
- Modify: `custom_components/oec_tariff_au/coordinator.py`
- Test: `tests/test_coordinator.py`

**Interfaces:**
- Produces:
  - `class DemandWindowsUnavailable(Exception)`, with attribute `status: int | None` (`None` for network errors and backoff).
  - `async OecTariffCoordinator.async_fetch_demand_windows(start: datetime, end: datetime) -> dict`. It returns the parsed JSON body. It raises `DemandWindowsUnavailable` on backoff, 404, any other non-200 status or a network error. A 429 calls `_start_backoff(resp)`, then raises.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_fetch_demand_windows_sends_offset_range` | The GET to `{api_url}/calculate/demand-windows` has params `dnsp`, `tariff`, `start=start.isoformat()` and `end=end.isoformat()`, both carrying an offset. Returns the body. |
| `test_fetch_demand_windows_404_raises_with_status` | `DemandWindowsUnavailable.status == 404`. |
| `test_fetch_demand_windows_429_starts_shared_backoff` | `hass.data[BACKOFF_UNTIL][api_url.rstrip("/")]` is set and the call raises. |
| `test_fetch_demand_windows_honours_shared_backoff` | With a future deadline in `BACKOFF_UNTIL`, no request is sent and the call raises with `status is None`. |

Use the mocking pattern `tests/test_coordinator.py` already uses for `current-rate`.

- [ ] **Step 2: Run them, expect FAIL** (`AttributeError: async_fetch_demand_windows`)

Run: `.venv/bin/python -m pytest tests/test_coordinator.py -q -k demand_windows`

- [ ] **Step 3: Implement** `async_fetch_demand_windows` and `DemandWindowsUnavailable` in `coordinator.py`, with timeout 10 s, as `_async_resolve_time_zone` does.

- [ ] **Step 4: Run the full suite, expect PASS**

- [ ] **Step 5: Commit** `feat: coordinator fetches demand windows (#16)`

### Task 2: Pure window functions

**Files:**
- Create: `custom_components/oec_tariff_au/demand_windows.py`
- Test: `tests/test_demand_windows.py`

**Interfaces:**
- Produces:

```python
ChargeKey = tuple[str, tuple[int, ...] | None]
Window = tuple[datetime, datetime]          # aware, half-open

def charge_key(charge: dict) -> ChargeKey
def parse_charges(body: dict) -> dict[ChargeKey, ChargeWindows] | None
def in_windows(windows: list[Window], when: datetime) -> bool
def next_transition(windows: list[Window], now: datetime) -> datetime | None
def window_hours(windows: list[Window]) -> float
def billing_month_range(now: datetime, billing_day: int) -> tuple[datetime, datetime]

@dataclass
class ChargeWindows:
    charge: dict          # the latest entry's charge fields, without "windows"
    windows: list[Window] # every entry's windows for this key, sorted
```

- `parse_charges` returns `None` when `body` has no `charges` key, which marks a service before `0.10.0`. Several entries with one key (a version change in the range) merge their windows. `charge` is taken from the entry whose first window starts last.
- `next_transition` returns the earliest window bound after `now` where `in_windows` flips, and `None` past the last bound.
- `billing_month_range` works on `now`'s own zone. Start is local midnight on `billing_day`, on or before `now`. End is the next month's `billing_day`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_charge_key_sorts_months` | `charge_key({"period": "peak", "months": [12, 1, 2]}) == ("peak", (1, 2, 12))`; `months: None` gives `("peak", None)`. |
| `test_parse_charges_keeps_seasonal_pair_apart` | Endeavour N72's two `peak` entries (rates 5.454 and 2.781, months as seeded) give two keys. |
| `test_parse_charges_merges_versions_and_takes_latest_rate` | Two entries with one key. The windows are the union, sorted, and `charge["rate"]` comes from the later entry. |
| `test_parse_charges_none_without_charges` | `parse_charges({"windows": []}) is None`. |
| `test_in_windows_start_inclusive_end_exclusive` | True at `start`, false at `end`. |
| `test_in_windows_across_dst_end` | A window 2026-04-05 01:00 to 03:00 Australia/Sydney (spanning the repeated hour) contains both 02:30 instants. |
| `test_next_transition` | Inside a window, it returns that window's end. Between windows, it returns the next start. After the last window, `None`. |
| `test_window_hours` | Two 4-hour windows give `8.0`. |
| `test_billing_month_range_day_1` | 2026-11-17 local gives 2026-11-01 to 2026-12-01. |
| `test_billing_month_range_day_28` | 2027-03-01 gives 2027-02-28 to 2027-03-28. |

- [ ] **Step 2: Run them, expect FAIL** (`ModuleNotFoundError`)
- [ ] **Step 3: Implement** the module. It imports only from the standard library and `homeassistant.util.dt`.
- [ ] **Step 4: Run the full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: per-charge window functions (#16)`

### Task 3: Demand window cache

**Files:**
- Create: `custom_components/oec_tariff_au/window_cache.py`
- Create: `tests/fixtures.py`, holding the per-tariff charge lists and the window builder below
- Test: `tests/test_window_cache.py`

**Interfaces:**
- Consumes: Task 1 `async_fetch_demand_windows` and `DemandWindowsUnavailable`; Task 2 `parse_charges`, `billing_month_range`, `ChargeWindows`.
- Produces:

```python
class DemandWindowCache:
    def __init__(self, hass, coordinator, entry_id: str, billing_day: int) -> None
    async def async_load(self) -> None                  # Store only, no request
    async def async_refresh(self, now: datetime) -> None
    def covers(self, when: datetime) -> bool
    def charges(self, when: datetime) -> dict[ChargeKey, ChargeWindows]   # the month holding `when`
    def add_listener(self, callback) -> Callable[[], None]                 # called after a change
    fetched_at: datetime | None
```

- **Store:** key `oec_tariff_au.demand_windows.<entry_id>`, version `1`. It holds up to two months, each `{"start", "end", "body"}` with ISO strings and the raw body.
- **`async_refresh(now)`** fetches the billing month holding `now` when one of these holds:
  - it is not cached;
  - `fetched_at` is more than 24 h old;
  - the month's end is within 24 h, in which case it also fetches the next month.
- **404:** `async_refresh` retries once with `end` set to the next 1 July 00:00 on `coordinator.dnsp_time_zone` after `start`. A second 404 caches nothing for that month.
- **Other failures:** any other `DemandWindowsUnavailable` keeps what is cached.
- **Pre-0.10.0 service:** a body for which `parse_charges` gives `None` is not cached. It logs one warning naming tariff-service `0.10.0`.
- **No cover:** one warning per outage. Logged when `covers(now)` turns false, and not again until it has been true.
- **Pruning:** months ending before `now` are dropped.

`tests/fixtures.py` holds:
- `demand_charges` lists copied from tariff-service `seed/*.json` for NASN12, Endeavour N72, Evoenergy 023, Essential EA302, Jemena A230 and SAPN LBAD;
- `windows_body(charges, days, spans)`, which builds a `/calculate/demand-windows` body by hand from #80's schema.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_refresh_fetches_billing_month_and_saves` | One fetch for 2026-11-01 to 2026-12-01; the Store holds it; `covers(now)` is true. |
| `test_cache_loads_from_store_without_fetching` | After `async_load()` from saved data, `covers(now)` is true and the fetch mock is not called. |
| `test_refresh_within_24h_of_month_end_fetches_next_month` | Two fetches; `covers(month_end + 1 h)` is true. |
| `test_refresh_skips_when_fresh` | A second refresh within 24 h sends no request. |
| `test_404_retries_to_1_july` | `now` 2027-06-20, billing day 15, first call 2027-06-15 to 2027-07-15 answers 404; the second call's `end` is 2027-07-01T00:00 on the DNSP zone. |
| `test_second_404_leaves_no_cover` | `covers(now)` is false. |
| `test_body_without_charges_not_cached_and_warns` | Not covered; one warning containing `0.10.0`. |
| `test_no_cover_warns_once_per_outage` | Two refreshes while uncovered log one warning. |
| `test_failed_refresh_keeps_cached_month` | `covers(now)` stays true after a `DemandWindowsUnavailable(None)`. |

- [ ] **Step 2: Run them, expect FAIL**
- [ ] **Step 3: Implement** `window_cache.py`.
- [ ] **Step 4: Run the full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: billing-month demand window cache (#16)`

### Task 4: Per-charge tracker

**Files:**
- Create: `custom_components/oec_tariff_au/charge_tracker.py`. A per-charge accumulator, with block reduction and restore ported from `demand_tracker.py`. `demand_tracker.py` stays untouched until Task 6, so every commit passes.
- Test: `tests/test_charge_tracker.py`. Port the block, flush and restore tests from `tests/test_demand_tracker.py` to the new interface.

**Interfaces:**
- Produces:

```python
class ChargeTracker:
    def __init__(self, key: ChargeKey, charge: dict) -> None
    key: ChargeKey
    charge: dict                      # updated by the manager; charge["rate"] is the rate billed
    def update_charge(self, charge: dict) -> None
    def sample(self, now: datetime, power_kw: float, in_window: bool) -> None
    def flush(self) -> None
    def reset_billing_month(self, now: datetime) -> None
    chargeable_demand: float          # property
    chargeable_demand_recorded_at: datetime | None
    month_peak_kw: float
    previous_month_peak_kw: float
    monthly_peaks_12: list[tuple[float, datetime | None]]
    current_block_samples: list[float]
    def save_state(self) -> dict      # today's keys plus "rate"
    def restore_state(self, data: dict) -> None   # accepts today's 1.2.x blob
```

- **Block reduction:** `charge["measurement_method"]` picks the method, as today. `rolling_12month_max` reduces as `30min_max`.
- **Rolling 12 months:** decided by `charge["peak_period"] == "rolling_12_months"`, not by the method.
- **Out-of-window samples:** `sample(..., in_window=False)` flushes a pending block and discards the sample.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_out_of_window_sample_flushes_block_and_is_ignored` | The block counts; the out-of-window sample does not. |
| `test_rolling_12_months_follows_peak_period` | `measurement_method: "30min_max"` with `peak_period: "rolling_12_months"`: after resets with peaks 5.0 and 3.0 and a current 2.0, `chargeable_demand == 5.0`. |
| `test_rolling_method_with_month_peak_period_bills_month` | `measurement_method: "rolling_12month_max"`, `peak_period: "month"`: `chargeable_demand` is this month's peak. |
| `test_save_state_carries_rate` | `save_state()["rate"] == charge["rate"]`. |
| `test_restore_accepts_1_2_x_blob` | The existing blob shape (plain-float `monthly_peaks_12` too) restores. |
| Kept tests | The 30-min average, the block-boundary crossing, `monthly_peak` direct and flush tests, ported to `sample()`. |

- [ ] **Step 2: Run them, expect FAIL**
- [ ] **Step 3: Implement** the refactor.
- [ ] **Step 4: Run the full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: per-charge tracker (#16)`

### Task 5: Demand manager

**Files:**
- Create: `custom_components/oec_tariff_au/demand_manager.py`
- Test: `tests/test_demand_manager.py`

**Interfaces:**
- Consumes: Task 3 `DemandWindowCache`; Task 4 `ChargeTracker`; Task 2 `in_windows`.
- Produces:

```python
class DemandManager:
    def __init__(self, hass, power_entity_id: str, cache: DemandWindowCache,
                 demand_charges: list[dict], billing_day: int) -> None
    trackers: dict[ChargeKey, ChargeTracker]
    last_reset: datetime | None
    def start(self) -> None      # 30 s interval + EVENT_HOMEASSISTANT_STOP flush
    def stop(self) -> None       # flush, then unsubscribe
    def save_state(self) -> dict
    def restore_state(self, data: dict) -> None
```

- **Trackers:** one per key of `demand_charges` (tariff detail order), created at init. `sync()` runs after every cache change and on each sample. It:
  - calls `update_charge` for keys present in `cache.charges(now)`;
  - adds a tracker for a new key;
  - leaves a key missing from the cache alone until the billing reset, then removes it.
- **Sample (every 30 s, local `now`):**
  1. If the billing month changed, it resets every tracker, sets `last_reset`, removes keys missing from the cache and schedules `cache.async_refresh(now)`.
  2. It reads the power entity once (W to kW as today; unknown, unavailable or non-numeric skips).
  3. If `not cache.covers(now)`, it flushes every tracker and stops there.
  4. Otherwise each tracker gets `sample(now, power_kw, in_windows(cache.charges(now)[key].windows, now))`. A key missing from the cache gets `in_window=False`.
- **Saved state:** `{"version": 2, "last_reset": iso, "trackers": [{"period", "months", "state": tracker.save_state()}]}`.
- **1.2.x blob** (no `"version"`): it restores into the first tracker in `demand_charges` order, kW or kVA, and its `last_reset` becomes the manager's.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_sample_routes_to_charge_in_window` | Evoenergy 023 at 18:00 standard time feeds only the peak tracker. |
| `test_lbad_concurrent_charges_both_measure` | At 18:00 both LBAD trackers take the sample. |
| `test_no_cover_measures_nothing` | With the cache uncovered, no tracker changes. |
| `test_billing_reset_resets_all_and_refreshes_cache` | Every `month_peak_kw` is `0.0`; `async_refresh` is awaited once. |
| `test_dropped_key_bills_until_reset` | A key missing from a refreshed cache keeps its peak and rate until the reset, and is removed at it. |
| `test_restore_v2_round_trip` | `save_state` then `restore_state` gives equal tracker states. |
| `test_restore_1_2_x_blob_goes_to_first_charge` | For Endeavour N19 (kVA), the first tracker holds the old peak; the second is `0.0`. |
| `test_hass_stop_flushes_every_tracker` | The pending block counts in each tracker. |

- [ ] **Step 2: Run them, expect FAIL**
- [ ] **Step 3: Implement** `DemandManager`.
- [ ] **Step 4: Run the full suite, expect PASS**
- [ ] **Step 5: Commit** `feat: demand manager feeds one tracker per charge (#16)`

### Task 6: Sensors, setup and config flow

**Files:**
- Modify:
  - `custom_components/oec_tariff_au/__init__.py`: build the cache, `async_load`, then `async_refresh(now)` (failure logged, not raised); store it as `data["window_cache"]`; register a coordinator listener calling `async_refresh`.
  - `custom_components/oec_tariff_au/sensor.py`
  - `custom_components/oec_tariff_au/demand_sensors.py`
  - `custom_components/oec_tariff_au/config_flow.py`
  - `.agent/ha-integration-rules.md`: the File Structure and Test Structure listings name `demand_windows.py`, `window_cache.py`, `charge_tracker.py` and `demand_manager.py` in place of `demand_tracker.py`.
- Delete: `custom_components/oec_tariff_au/demand_tracker.py` (`is_in_demand_window`, `on_dnsp_clock`, `DemandWindowScheduler`, the old `DemandTracker`) and `tests/test_demand_tracker.py`.
- Test:
  - `tests/test_demand_sensors.py`, rewritten against `DemandManager`;
  - `tests/test_demand_window_status_sensor.py`;
  - `tests/test_sensor.py`;
  - `tests/test_config_flow.py`.

**Interfaces:**
- Consumes: Tasks 2 to 5.
- **Values:** spec "Sensors" table, with these sources:
  - **Demand Rate:** `max(rate)` over `coordinator.data["rate"]["demand_charges"]`, the service's in-season list; `0.0` when it is empty. Attribute `charges` holds the tariff detail's `demand_charges` entries with the spec's fields.
  - **Demand Window:** `active` when any `cache.charges(now)` window holds `now`. Transitions are scheduled with `async_track_point_in_time` at `next_transition` over all charges' windows, and re-scheduled on cache change. The value is `None` (unavailable) when `not cache.covers(now)`. Attribute `active_charges` is a list of `period`.
  - **Month Peak Demand:** max of `chargeable_demand` over trackers. Attribute `charges` gives each tracker's `period`, `months`, `unit`, `peak`, `demand_recorded_at` and, for kVA, `"measured_as": "kW"`. It keeps `billing_day`, `last_reset` and `power_entity`, and drops `demand_window`, `demand_days` and `season_months`. It owns restore and start or stop, as today, through the manager.
  - **Monthly Demand Charge:** `round(sum(t.chargeable_demand * t.charge["rate"]), 2)`. Attribute `charges` gives each tracker's `period`, `months` and `amount`.
  - **Demand Surcharge:** `round(sum(t.charge["rate"] / window_hours(...)), 5)` over trackers with `chargeable_demand > 0` and window hours above 0, using the month's windows; `0.0` when there are none.
- **Setup:** entities are created when `tariff_detail["demand_charges"]` is non-empty. The tracker sensors are created only with a power entity. `_tariff_has_demand_component` returns `bool(detail.get("demand_charges"))`. Nothing reads `detail["demand"]`.

- [ ] **Step 1: Write the failing tests**

| Test | Assertion |
|---|---|
| `test_demand_rate_follows_in_season_list` | NASN12 in July with the current-rate `demand_charges` empty gives `0.0`; in January it gives the summer rate. |
| `test_monthly_charge_sums_charges` | Two trackers, peaks 4.0 and 2.0, rates 5.454 and 1.0, give `23.82`. |
| `test_surcharge_uses_exact_window_hours` | One charge, rate 10.0, 80 window hours in the month, peak above 0, gives `0.125`. |
| `test_kva_charge_marked_measured_as_kw` | LBAD's `charges` attributes each carry `"measured_as": "kW"`. |
| `test_demand_window_schedules_from_cache_bounds` | Moving time to a window start flips the state to `active`; `active_charges == ["peak"]`. |
| `test_setup_with_no_cover_creates_entities_unavailable` | Fetch raises at setup; the entities exist; Demand Window's state is `unavailable` or `unknown`. |
| `test_unique_ids_unchanged` | The unique IDs are `{entry_id}_month_peak_demand`, `_monthly_demand_charge`, `_demand_surcharge_per_kwh`, `_demand_rate` and `_demand_window`. |
| `test_config_flow_demand_step_uses_demand_charges` | A detail with `demand_charges: []` and a legacy `demand` object skips the demand step. |
| `test_no_code_reads_legacy_demand` | `grep -rn '\["demand"\]\|get("demand")' custom_components/` finds nothing (asserted with `subprocess` or `pathlib` text search). |

- [ ] **Step 2: Run them, expect FAIL**
- [ ] **Step 3: Implement** the changes.
- [ ] **Step 4: Run the full suite, expect PASS, all files**
- [ ] **Step 5: Commit** `feat: demand sensors read every charge (#16)`

### Task 7: Docs and release notes

**Files:**
- Modify:
  - `CHANGELOG.md`: an `## [Unreleased]` entry with Fixed (seasons, holidays, time basis, multiple charges, Demand Surcharge exact hours) and Changed (requires tariff-service 0.10.0; the Demand Rate attributes change, listing the removed and added names; kVA charges are still measured from kW).
  - `README.md`: the demand section names the five entities' meanings and the kVA stand-in.
- Test: none. Run the full suite.

- [ ] **Step 1: Write the entries**, using "maintainer", not "user", per the public-repo rule.
- [ ] **Step 2: `grep -nP '\x{2014}' CHANGELOG.md README.md`**: no new em dash in the added lines.
- [ ] **Step 3: Commit** `docs: per-charge demand in CHANGELOG and README (#16)`

### Task 8: Re-record fixtures (after tariff-service 0.10.0 deploys)

**Blocked by:** tariff-service `0.10.0` live.

**Files:**
- Create: `tests/recorded/demand_windows_<dnsp>_<tariff>.json`, `tests/test_fixtures.py`
- Modify: `tests/fixtures.py`

- [ ] **Step 1:** For each fixture tariff, call the live `/calculate/demand-windows` for a 7-day range that holds the fixture's dates and commit the bodies verbatim under `tests/recorded/`. `windows_body` stays the generator, since the tests build whole billing months and use dates outside the price lists; `tests/test_fixtures.py` asserts it gives the recorded windows per charge key, and that the fixture `demand_charges` fields equal the live charge fields.
- [ ] **Step 2: Run the full suite, expect PASS.** A failure is a mismatch between the hand-built shape and #80 as built: report it to Architecture, don't edit the assertion.
- [ ] **Step 3: Commit** `test: demand window fixtures recorded from tariff-service 0.10.0 (#16)`
