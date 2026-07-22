# Home Assistant Integration Development Rules

## Naming Convention
- Repo: `integration_{platform}_{name}` (e.g., `integration_hass_oec-tariff`)
- HA domain: `oec_tariff` (underscores, no hyphens)
- Entity prefix: derived from domain (HA handles this automatically)
- Friendly names: NO "OEC" prefix — use descriptive names only (e.g., "Current Rate", "Demand Window")
- Device name: `Tariff ({dnsp}/{tariff})` — contextual, not branded

## File Structure
```
custom_components/{domain}/
├── __init__.py           # async_setup_entry / async_unload_entry
├── manifest.json         # Integration metadata
├── const.py              # Domain, config keys, defaults
├── config_flow.py        # UI configuration (multi-step)
├── coordinator.py        # DataUpdateCoordinator (API polling)
├── sensor.py             # Sensor entities
├── binary_sensor.py      # Binary sensor entities
├── demand_tracker.py     # Business logic (separated from entities)
├── demand_sensors.py     # Demand-specific sensor entities
├── strings.json          # UI strings for config flow
└── brand/
    ├── icon.png          # 256px square icon
    ├── icon@2x.png       # Retina icon
    └── logo.png          # Wide logo for detail view
```

## Architecture Principles
- **Coordinator pattern**: All API communication through DataUpdateCoordinator
- **Separation of concerns**: Business logic (e.g., demand_tracker.py) separate from entity classes
- **RestoreEntity**: Any stateful tracking must survive HA restarts
- **No external dependencies**: Use HA's built-in aiohttp, don't add pip requirements unless essential
- **Config flow only**: No YAML configuration — modern HA standard
- **Entity selectors**: Use HA's native selector UI for entity pickers

## Config Flow Design
- Multi-step: each step gets one decision from the user
- Fetch options live from API (DNSPs, tariffs) — don't hardcode
- Optional steps: clearly marked, user can skip
- Validate connectivity before proceeding (show errors if API unreachable)

## Sensor Design
- Use appropriate `device_class` and `state_class` for HA statistics
- `suggested_display_precision` for monetary values
- Expose context via `extra_state_attributes` (not extra entities)
- Group under a single Device per config entry

## Testing

### Requirements
- **All tests must pass before every commit** — run `.venv/bin/python -m pytest tests/ -v`
- Use the project `.venv` (has `homeassistant` + `pytest-homeassistant-custom-component`)
- System Python does NOT have `homeassistant` installed — always use `.venv`

### Test Structure
```
tests/
├── __init__.py
├── conftest.py              # Shared fixtures (enable_custom_integrations)
├── test_demand_tracker.py   # Pure logic: is_in_demand_window()
├── test_binary_sensor.py    # Binary sensor logic: transitions, _status_at()
├── test_config_flow.py      # Config flow steps (TODO)
├── test_sensor.py           # Sensor value mapping (TODO)
└── test_coordinator.py      # API response handling (TODO)
```

### Test Principles
- **Pure logic tests**: Test functions like `is_in_demand_window()` directly — no mocking needed
- **Sensor tests**: Use fake coordinator/entry classes to test sensor properties without HA runtime
- **Config flow tests**: Use `pytest-homeassistant-custom-component` fixtures for full integration tests
- **Edge cases**: Always test boundary conditions (window start inclusive, end exclusive, overnight spans, season boundaries)

### Coverage Targets
- `demand_tracker.py` — window logic, billing day reset, measurement methods
- `binary_sensor.py` — transition calculation, state at time, no-demand fallback
- `config_flow.py` — each step, error handling, options flow
- `sensor.py` — value mapping from coordinator data
- `coordinator.py` — API response parsing, error handling

### Pre-commit Checklist
1. `.venv/bin/python -m pytest tests/ -v` — ALL PASS
2. No new code without corresponding test for testable logic
3. Don't commit broken tests "to fix later"

## Deployment to HA
1. Develop in this repo's checkout
2. Run tests: `.venv/bin/python -m pytest tests/ -v` — must all pass
3. Rsync to `~/hass/Home_Assistant/custom_components/oec_tariff/`
4. Force-add new files: `git add -f custom_components/oec_tariff/` (HA `.gitignore` has `*.*`)
5. Commit and push HA repo
6. Git pull on HA device, restart
7. Delete and re-add integration when config structure changes
8. Check HA logs for import errors after restart

## HACS Compatibility
- Include `hacs.json` at repo root
- Include `brand/` directory for icons (HA 2026.3+)
- README with installation instructions
- Single integration per repo
