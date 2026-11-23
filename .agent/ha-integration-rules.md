# Home Assistant Integration Development Rules

## Overrides

| Org rule | Replaced here by |
|---|---|
| `branching-strategy.md#milestone-releases`: tag the merge commit `vX.Y.Z` | Tag `X.Y.Z` without `v`, continuing tags `1.0.1` to `1.2.4`: `governance/adr/cross-project.md#2026-11-17-ha-tariff-au-keeps-xyz-tags` |

## Naming Convention
- Repo: `ha-{name}` (e.g., `ha-tariff-au`). Per `naming-standards.md`, the `ha-` prefix
  is required by HACS convention even though it looks like it violates the no-prefix rule
- HA domain: `oec_tariff_au` (underscores, no hyphens)
- Entity prefix: derived from domain (HA handles this automatically)
- Friendly names: NO "OEC" prefix. Use descriptive names only (e.g., "Current Rate", "Demand Window")
- Device name: `Tariff ({dnsp}/{tariff})`. It is contextual, not branded

## File Structure
```
custom_components/{domain}/
├── __init__.py           # async_setup_entry / async_unload_entry
├── manifest.json         # Integration metadata
├── const.py              # Domain, config keys, defaults
├── config_flow.py        # UI configuration (multi-step)
├── coordinator.py        # DataUpdateCoordinator (API polling)
├── sensor.py             # Sensor entities
├── demand_windows.py     # Pure functions over the cached per-charge windows
├── window_cache.py       # Billing-month demand window cache (Store per entry)
├── charge_tracker.py     # Per-charge peak accumulator (business logic, no hass)
├── demand_manager.py     # Samples power, feeds one ChargeTracker per charge
├── demand_sensors.py     # Demand-specific sensor entities
├── strings.json          # UI strings for config flow
└── brand/
    ├── icon.png          # 256px square icon
    ├── icon@2x.png       # Retina icon
    └── logo.png          # Wide logo for detail view
```

```
docs/
├── spec/oec_tariff_au/functional-design.md   # Current behaviour: rules, Store, API (governs)
└── design/oec_tariff_au/oec_tariff_au.md     # Design view: diagrams drawn from the spec
```

Specs and plans for a change live in the org repo, not here (`CLAUDE.md` "Specs and Plans").

## Architecture Principles
- **Coordinator pattern**: All API communication through DataUpdateCoordinator
- **Separation of concerns**: Business logic (e.g., charge_tracker.py, demand_manager.py) separate from entity classes
- **RestoreEntity**: Any stateful tracking must survive HA restarts
- **No external dependencies**: Use HA's built-in aiohttp, don't add pip requirements unless essential
- **Config flow only**: No YAML configuration. This is the modern HA standard
- **Entity selectors**: Use HA's native selector UI for entity pickers

## Config Flow Design
- Multi-step: each step gets one decision from the user
- Fetch options live from API (DNSPs, tariffs). Don't hardcode them
- Optional steps: clearly marked, user can skip
- Validate connectivity before proceeding (show errors if API unreachable)

## Sensor Design
- Use appropriate `device_class` and `state_class` for HA statistics
- `suggested_display_precision` for monetary values
- Expose context via `extra_state_attributes` (not extra entities)
- Group under a single Device per config entry

## Testing

### Requirements
- **All tests must pass before every commit**. Run `.venv/bin/python -m pytest tests/ -v`
- Use the project `.venv` (has `homeassistant` + `pytest-homeassistant-custom-component`)
- System Python does NOT have `homeassistant` installed. Always use `.venv`

### Test Structure
```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures (enable_custom_integrations)
├── fixtures.py              # Seed demand_charges, the demand-windows body generator, RECORDED
├── recorded/                # Live tariff-service demand-windows bodies, verbatim
├── test_fixtures.py         # The generator reproduces the recorded live windows
├── test_demand_windows.py   # Pure logic: in_windows(), next_transition(), window_hours()
├── test_window_cache.py     # Window cache: Store, refresh triggers, 404 retry, no cover
├── test_charge_tracker.py   # Per-charge blocks, methods, rolling 12 months
├── test_demand_manager.py   # Sample routing, billing reset, 1.2.x and v2 restore
├── test_demand_sensors.py   # Demand sensor values and attributes over the manager
├── test_demand_window_status_sensor.py  # Demand Window transitions from cached bounds
├── test_setup.py            # Entry setup: saved path, first start, outage, unload
├── test_config_flow.py      # Config flow steps (TODO)
├── test_sensor.py           # Sensor value mapping (TODO)
└── test_coordinator.py      # API response handling (TODO)
```

### Test Principles
- **Pure logic tests**: Test functions like `in_windows()` directly, with no mocking
- **Sensor tests**: Use fake coordinator/entry classes to test sensor properties without HA runtime
- **Config flow tests**: Use `pytest-homeassistant-custom-component` fixtures for full integration tests
- **Edge cases**: Always test boundary conditions (window start inclusive, end exclusive, overnight spans, season boundaries)

### Coverage Targets
- `demand_windows.py`: window membership, transitions, window hours, billing month range
- `window_cache.py`: refresh triggers, Store restore, 404 retry, no cover
- `charge_tracker.py`: measurement methods, peak period, restore
- `demand_manager.py`: sample routing, billing day reset, restore
- `config_flow.py`: each step, error handling, options flow
- `sensor.py`: value mapping from coordinator data
- `coordinator.py`: API response parsing, error handling

### Pre-commit Checklist
1. `.venv/bin/python -m pytest tests/ -v`. ALL PASS
2. No new code without corresponding test for testable logic
3. Don't commit broken tests "to fix later"

## Deployment to HA
A release installs from its GitHub Release through HACS (the Marketplace from HA 2026.11).
Before a release PR merges, its tip is installed on the HA instance by hand for the live check:

1. `.venv/bin/python -m pytest tests/ -v`: all pass.
2. On the HA instance, fetch the release branch:
   `git clone --depth 1 --branch feat/X.Y.Z https://github.com/Open-Energy-Collective/ha-tariff-au.git`,
   or its archive `https://github.com/Open-Energy-Collective/ha-tariff-au/archive/refs/heads/feat/X.Y.Z.tar.gz`.
3. Copy `/config/custom_components/oec_tariff_au` to a backup outside `custom_components/`.
4. Replace `/config/custom_components/oec_tariff_au` with the fetched `custom_components/oec_tariff_au`.
5. `ha core restart`.
6. Delete and re-add the integration when the config structure changes.
7. Settings, System, Logs: no `oec_tariff_au` errors after the restart.
8. After the Release is published, update through HACS and delete the backup.

## HACS Compatibility
- Include `hacs.json` at repo root
- Include `brand/` directory for icons (HA 2026.3+)
- README with installation instructions
- Single integration per repo

## Third-Party IP & License Compliance
Before adding any dependency: verify it declares an approved license (MIT, Apache-2.0,
BSD-2/3-Clause, ISC, LGPL). Anything else, unclear, or missing? Stop and ask.

## Security / PII review before a public push, release or HACS submission
This repo is public (`github.com/Open-Energy-Collective/ha-tariff-au`). No
security-scan script or checklist exists in the OEC layout. Until one is restored, a push
to a public remote, a public release or a HACS submission needs a manual security and
PII review against `governance/security-standards.md` and
`governance/security-privacy-principles.md`, and the maintainer's go-ahead. Never write an
exact leaked value or exact commit hash into a commit message, even when documenting a fix.
