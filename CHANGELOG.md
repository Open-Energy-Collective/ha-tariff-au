# Changelog

Versions follow SemVer. Entries start at 1.2.2; earlier releases are on the GitHub Releases page.

## [1.4.0] - 2026-11-23

### Fixed
- A restart while the tariff service is down no longer stops demand measurement. The integration starts from the tariff data it saved at its last start and measures demand from the cached windows while it waits for the service.
- Month Peak Demand, Monthly Demand Charge and Demand Surcharge stay available, and keep updating, through a tariff service outage. Demand Window stays available while the cached windows cover now, whatever the service's state.
- Setup no longer waits on the tariff service once the integration has started successfully before, so Home Assistant no longer waits about 40 seconds on it during startup near 1 July.
- A first start while the tariff service is down, or while its tariff detail fails, is retried by Home Assistant until the service answers. It no longer loads without demand entities until a reload.
- Demand Surcharge is unknown, not high, while the billing month is cut short at 1 July because the next financial year's windows are not published yet. A new `partial_month` attribute is `true` while that holds.
- Adding the integration while the tariff service has asked for a pause (a 429) waits out the pause, and the form shows that the service is rate limited.
- Demand Window's `active_charges` attribute changes at every window bound when charges overlap, instead of up to 5 minutes late.

### Changed
- Current Rate, Current Period, Daily Supply Charge, Tariff Name and Demand Rate are unavailable after a restart until the tariff service answers, and while it is unreachable.
- The integration saves the tariff detail and the distributor's time zone with its cached demand windows. Downgrading to 1.3.0 still reads that storage.

## [1.3.0] - 2026-11-19

### Fixed
- Seasonal demand tariffs follow their season. A tariff such as NASN12 no longer shows its summer rate all year, and its Demand Window is active in the right months.
- Public holidays are excluded from the demand window where the distributor excludes them (Ausgrid and Endeavour).
- Tariffs on standard time (Evoenergy 023) track the demand window at the right hour.
- Tariffs with several demand charges measure each one with its own windows, method and rate. SAPN LBAD (peak and anytime) and Evoenergy 023 (peak and off-peak) are no longer reduced to one charge. Monthly Demand Charge is the sum over the charges.
- Demand Surcharge sums, over the charges with a recorded peak and window hours this month, each charge's rate divided by that charge's exact window hours in the billing month, not the 5/7 and 2/7 estimate. It changes value after this update.
- A 429 from the tariff service is honoured. The integration waits for the time its `Retry-After` gives, or 60 seconds when it gives none, before sending any request, and every entry on the same service address waits together. The 429 is logged once per wait.

### Changed
- Requires tariff-service 0.10.0 or later. An older service gives no demand windows, so no demand is measured and Demand Window is unavailable. The integration logs a warning naming the minimum version.
- The integration no longer reads the tariff detail's `demand` object. Demand windows come from the tariff service, and the last demand-windows response is cached. Measuring continues while the demand-windows request fails, including across a restart, as long as the tariff service otherwise answers. A restart while the whole tariff service is down waits for it, as before.
- Demand Rate attributes: `window_start`, `window_end`, `days`, `season_months` and `measurement_method` are removed. `charges` is added, listing each charge's `period`, `rate`, `unit`, `months`, `start`, `end`, `days`, `public_holidays`, `measurement_method` and `peak_period`. Its value is the highest rate among the charges in season, or `0.0` when none is.
- Month Peak Demand attributes: `demand_window`, `demand_days`, `season_months` and `measurement_method` are removed. `charges` is added, with each charge's peak, `demand_recorded_at`, `measurement_method` and `peak_period`. Its value is the highest chargeable peak across the charges.
- Monthly Demand Charge gains a `charges` attribute with each charge's amount.
- Demand Window gains an `active_charges` attribute listing the periods active now. It is unavailable while the integration holds no windows covering now.
- Charges with `peak_period: rolling_12_months` bill the highest of the last 12 billing months' peaks.
- A charge out of season keeps its peak history. SAPN LBAD's rolling 12-month peak charge keeps billing on the last 12 months' peak from April to October. A charge is removed at the billing reset only once the tariff no longer lists it. The tariff detail is fetched again once a day.
- kVA charges are still measured from the kW power entity, so their charge reads low. Their `charges` entries carry `"measured_as": "kW"`.
- Entity IDs and unique IDs are unchanged. Saved demand state from 1.2.x moves to the first charge in the tariff's list that is in season at the update (the first charge when no windows are cached), and other charges start at zero.
- Deleting the integration removes its cached demand windows from storage.

## [1.2.4] - 2026-11-17

### Fixed
- Current Rate, Daily Supply Charge, Demand Rate, Demand Surcharge and Monthly Demand Charge record long-term statistics again. Home Assistant's "no longer has a state class" repair clears after the update, and existing statistics continue.

## [1.2.3] - 2026-11-09

### Fixed
- Demand window status and peak demand tracking now follow the distributor's time zone, not the Home Assistant instance's. An instance in a different zone, or across a daylight saving boundary, no longer tracks the window at the wrong hour.
- The Demand Surcharge sensor now matches the tariff service: rate divided by window hours per month, counting weekday windows as 5/7 of the month and weekend windows as 2/7. Its value no longer scales with peak demand, so it drops to a lower figure after this update.

### Changed
- The integration reads the distributor's time zone from the tariff service when it starts. If that call fails, setup is retried.

## [1.2.2] - 2026-10-27

### Fixed
- The README's example automation for the Demand Window sensor now triggers. It matches the sensor's state `active`, not the display text "Active". The sensor table and feature list name the states `active` and `inactive`.

### Changed
- README install steps: OEC Tariff (AU) is in the HACS default store. Search for it in HACS and select Download; adding a custom repository is no longer needed.
