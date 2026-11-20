# OEC Tariff (AU)
# Home Assistant Integration

Real-time Australian network tariff data in Home Assistant, powered by the [Tariff Data Service](https://api.openenergy.org.au/docs).

## Features

- **Current rate** sensor ($/kWh) — updates every 5 minutes
- **Current period** sensor — peak, off_peak, shoulder, solar_soak
- **Demand window** enum sensor — `active` or `inactive`, selectable as a dropdown in automation triggers
- **Daily supply charge** sensor
- **Demand rate** sensor listing every demand charge, with its windows and method, in a `charges` attribute
- **Demand charge tracker** — monitors your grid power and calculates peak demand charges, with a `demand_recorded_at` attribute giving the time of the current chargeable peak
- **Example dashboard card** — `dashboards/demand-card.yaml` shows demand-tracking status at a glance
- **Config flow UI** — pick your DNSP, tariff, and optionally configure demand tracking

## Supported DNSPs

All DNSPs served by the Tariff Data Service (currently 12 across NSW, VIC, QLD, SA, ACT, NT).

## Installation

### Home Assistant Marketplace (recommended)

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=Open-Energy-Collective&repository=ha-tariff-au&category=integration)

1. Go to Settings → Marketplace, search for "OEC Tariff (AU)" and select **Install**
2. Restart Home Assistant
3. Go to Settings → Devices & services → Add Integration → "OEC Tariff (AU)"
4. Select your DNSP and tariff code
5. Optionally configure demand tracking (select power sensor + billing day)

Home Assistant 2026.3 to 2026.10: install through HACS, searching for "OEC Tariff (AU)".

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
| `sensor.tariff_energex_3900_current_period` | - | peak, off_peak, shoulder, solar_soak |
| `sensor.tariff_energex_3900_daily_supply_charge` | $/day | Fixed daily network charge |
| `sensor.tariff_energex_3900_demand_rate` | $/kW/month | Highest rate among the demand charges in season now |
| `sensor.tariff_energex_3900_tariff_name` | - | Human-readable tariff name |
| `sensor.tariff_energex_3900_demand_window` | - | Whether demand is being measured: `active`, `inactive` |

### Demand Tracking Sensors (if power entity configured)

| Entity | Unit | Description |
|--------|------|-------------|
| `sensor.tariff_energex_3900_month_peak_demand` | kW | Highest chargeable peak across the demand charges: this billing month, or the last 12 months for a rolling charge |
| `sensor.tariff_energex_3900_monthly_demand_charge` | $ | Sum over the demand charges of chargeable peak times rate |
| `sensor.tariff_energex_3900_demand_surcharge` | $/kWh | Demand charge amortized per kWh |

### Demand Tracking Details

The tracker automatically uses the correct measurement methodology for your DNSP:

| Method | DNSPs | How it works |
|--------|-------|--------------|
| `30min_avg` | Energex | Average kW over highest 30-min block |
| `30min_max` | Ausgrid, Endeavour, Essential, Evoenergy, Power Water, SAPN | Max kW in any 30-min block |
| `monthly_peak` | AusNet | Absolute max during demand window |
| `rolling_12month_max` | Jemena | Max of last 12 monthly peaks |

Samples every 30 seconds. Each demand charge on the tariff is measured on its own, only inside the windows the tariff service lists for it (time, days, season, time zone and public holidays). Persists across restarts. Needs tariff-service 0.10.0 or later; without demand windows covering now, nothing is measured and Demand Window is unavailable.

| Entity | Meaning |
|--------|---------|
| Demand Rate | Highest rate among the charges in season now, `0.0` when none is. The `charges` attribute lists every charge. |
| Demand Window | `active` when any charge's window covers now. `active_charges` lists which. |
| Month Peak Demand | Highest chargeable peak across the charges. `charges` gives each charge's peak. |
| Monthly Demand Charge | Sum over the charges of chargeable peak times rate. `charges` gives each amount. |
| Demand Surcharge | Sum over the charges with a recorded peak and window hours this month of rate divided by that charge's window hours in the billing month. Unknown while tariff-service has not yet published the next financial year and the billing month crosses 1 July; `partial_month` is then `true`. |

Charges that bill the highest of the last 12 months (`rolling_12_months`) use that peak. Charges in kVA are measured from your kW sensor as a stand-in, so they read low, and their `charges` entry carries `"measured_as": "kW"`.

Each demand tracking sensor carries a `demand_recorded_at` attribute: the ISO timestamp at which the current chargeable peak was recorded, or unset if nothing has been recorded this billing month.

### Example Dashboard Card

`dashboards/demand-card.yaml` is a ready-made Lovelace card showing the demand window, current peak and projected charge for one tariff. It needs the Mushroom and card-mod custom cards from the Marketplace (HACS before 2026.11); the file header lists the entity IDs to substitute.

## Example Automations

```yaml
# Pause EV charging during demand window
automation:
  - alias: "Pause EV charging during demand window"
    trigger:
      - platform: state
        entity_id: sensor.tariff_energex_3900_demand_window
        to: "active"
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
