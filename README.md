# OEC Tariff — Home Assistant Integration

Real-time Australian network tariff data in Home Assistant, powered by the [OEC Tariff Data Service](https://api.openenergy.org.au/docs).

## Features

- **Current rate** sensor ($/kWh) — updates every 5 minutes
- **Current period** sensor — peak, off_peak, shoulder, solar_soak
- **In demand window** binary sensor — automations can react to demand measurement periods
- **Daily supply charge** sensor
- **Demand rate** sensor with window/method attributes
- **Config flow UI** — pick your DNSP and tariff from a dropdown (fetched live from API)

## Supported DNSPs

All DNSPs served by the OEC Tariff API (currently 9 across NSW, VIC, QLD, SA, ACT, NT).

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "OEC Tariff"
3. Restart Home Assistant
4. Go to Settings → Integrations → Add Integration → "OEC Tariff"
5. Select your DNSP and tariff code

### Manual

Copy `custom_components/oec_tariff/` to your HA config directory.

## Sensors Created

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.oec_tariff_current_rate` | $/kWh | Active energy rate now |
| `sensor.oec_tariff_current_period` | — | peak, off_peak, shoulder, etc. |
| `sensor.oec_tariff_daily_supply_charge` | $/day | Fixed daily network charge |
| `sensor.oec_tariff_demand_rate` | $/kW/month | Demand charge rate |
| `sensor.oec_tariff_tariff_name` | — | Human-readable tariff name |
| `binary_sensor.oec_tariff_in_demand_window` | Active/Inactive | Whether demand is being measured |

## Example Automations

```yaml
# Don't run high-power appliances during demand window
automation:
  - alias: "Pause EV charging during demand window"
    trigger:
      - platform: state
        entity_id: binary_sensor.oec_tariff_in_demand_window
        to: "Active"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ev_charger
```

## Data Source

All tariff data sourced from official AER-approved DNSP Network Price Lists.
See [DATA_SOURCES.md](https://github.com/Open-Energy-Collective/tariff-service/blob/main/DATA_SOURCES.md).

## License

MIT
