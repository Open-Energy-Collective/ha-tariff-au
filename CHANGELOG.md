# Changelog

Versions follow SemVer. Entries start at 1.2.2; earlier releases are on the GitHub Releases page.

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
