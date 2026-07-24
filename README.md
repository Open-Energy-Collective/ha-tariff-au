# OEC - Tariff AU — Home Assistant Integration

Real-time Australian network tariff data in Home Assistant, powered by the [Tariff Data Service](https://api.openenergy.org.au/docs).

## Features

- **Current rate** sensor ($/kWh) — updates every 5 minutes
- **Current period** sensor — peak, off_peak, shoulder, solar_soak
- **Demand window** binary sensor — Active/Inactive for automation triggers
- **Daily supply charge** sensor
- **Demand rate** sensor with window/method attributes
- **Demand charge tracker** — monitors your grid power and calculates peak demand charges
- **Config flow UI** — pick your DNSP, tariff, and optionally configure demand tracking

## Supported DNSPs

All DNSPs served by the Tariff Data Service (currently 9 across NSW, VIC, QLD, SA, ACT, NT).

## Installation

### HACS (recommended)

1. Add this repository as a custom repository in HACS
2. Install "OEC - Tariff AU"
3. Restart Home Assistant
4. Go to Settings → Integrations → Add Integration → "OEC - Tariff AU"
5. Select your DNSP and tariff code
6. Optionally configure demand tracking (select power sensor + billing day)

### Manual

Copy `custom_components/oec_tariff_au/` to your HA config directory.

## Configuration

### Step 1: Select DNSP
Choose your electricity distribution network (e.g., Energex, Ausgrid, SA Power Networks).

### Step 2: Select Tariff
Choose your network tariff code. Check your bill or ask your retailer.

### Step 3: Demand Tracking (optional)
If your tariff has a demand charge component:
- **Grid power sensor**: Any entity that reports grid import power (kW or W)
- **Billing day**: Day of month your billing cycle resets (default: 1)

## Sensors Created

Entity IDs follow the pattern `sensor.tariff_{dnsp}_{tariff}_{name}`. For example, with Energex tariff 3900:

### Rate Sensors

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.tariff_energex_3900_current_rate` | $/kWh | Active energy rate now |
| `sensor.tariff_energex_3900_current_period` | — | peak, off_peak, shoulder, solar_soak |
| `sensor.tariff_energex_3900_daily_supply_charge` | $/day | Fixed daily network charge |
| `sensor.tariff_energex_3900_demand_rate` | $/kW/month | Demand charge rate |
| `sensor.tariff_energex_3900_tariff_name` | — | Human-readable tariff name |
| `binary_sensor.tariff_energex_3900_demand_window` | Active/Inactive | Whether demand is being measured |

### Demand Tracking Sensors (if power entity configured)

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.tariff_energex_3900_month_peak_demand` | kW | Highest measured demand this billing month |
| `sensor.tariff_energex_3900_monthly_demand_charge` | $ | peak_kW × demand_rate |
| `sensor.tariff_energex_3900_demand_surcharge` | $/kWh | Demand charge amortized per kWh |

### Demand Tracking Details

The tracker automatically uses the correct measurement methodology for your DNSP:

| Method | DNSPs | How it works |
|--------|-------|--------------|
| `30min_avg` | Energex | Average kW over highest 30-min block |
| `30min_max` | Ausgrid, Endeavour, Essential, Evoenergy, Power Water, SAPN | Max kW in any 30-min block |
| `monthly_peak` | AusNet | Absolute max during demand window |
| `rolling_12month_max` | Jemena | Max of last 12 monthly peaks |

Samples every 30 seconds. Only records during the demand window (time, days, and season as defined by your DNSP). Persists across restarts.

## Example Automations

```yaml
# Pause EV charging during demand window
automation:
  - alias: "Pause EV charging during demand window"
    trigger:
      - platform: state
        entity_id: binary_sensor.tariff_energex_3900_demand_window
        to: "Active"
    action:
      - service: switch.turn_off
        target:
          entity_id: switch.ev_charger

# Alert when demand charge is getting high
automation:
  - alias: "Demand charge warning"
    trigger:
      - platform: numeric_state
        entity_id: sensor.tariff_energex_3900_monthly_demand_charge
        above: 40
    action:
      - service: notify.mobile_app
        data:
          title: "Demand charge alert"
          message: "Monthly demand charge is now ${{ states('sensor.tariff_energex_3900_monthly_demand_charge') }}"
```

## Data Source

All tariff data sourced from official AER-approved DNSP Network Price Lists.
See [DATA_SOURCES.md](https://github.com/Open-Energy-Collective/tariff-service/blob/main/DATA_SOURCES.md).

## License

MIT
