# OEC Tariff integration: design view

Intended design, for human readers. The specs cited below govern.

Drawn from: [functional design §1](../../spec/oec_tariff_au/functional-design.md#1-overview), [§2](../../spec/oec_tariff_au/functional-design.md#2-data-model), [§3](../../spec/oec_tariff_au/functional-design.md#3-business-rules), [§4](../../spec/oec_tariff_au/functional-design.md#4-api-specification)

Checked against `feat/1.4.0` `50dd5a4` on 2026-11-23.

## 1. The integration reads tariff-service and one power entity, and serves sensors

The coordinator is the only client of tariff-service. The window cache keeps what setup and
demand measurement need in the entry's Store
([§2](../../spec/oec_tariff_au/functional-design.md#2-data-model)).

```mermaid
flowchart LR
  TS["tariff-service"] -->|rate, detail, time zone, windows| CO["Coordinator<br/>(shared 429 backoff)"]
  CO -->|detail, time zone| WC["Window cache"]
  CO -->|windows| WC
  WC <-->|months, detail, time zone| ST[("Store<br/>per entry")]
  PE["Power entity"] -->|every 30 s| DM["Demand manager"]
  WC -->|charges and windows| DM
  DM <-->|tracker state| RS[("RestoreEntity data")]
  CO --> TSN["Tariff sensors"]
  WC --> DW["Demand Window"]
  DM --> DS["Demand sensors"]
```

## 2. Setup starts from the Store when it holds a detail

A saved detail means setup sends no request before the sensors load; with none, a failed
fetch has HA retry setup ([rules 3.1 to 3.3](../../spec/oec_tariff_au/functional-design.md#setup-and-refresh)).

```mermaid
flowchart TD
  L["Load the Store"] --> Q{"detail saved?"}
  Q -->|yes| SE["Seed coordinator:<br/>detail, time zone"]
  SE --> PL1["Load platforms"]
  PL1 --> BG["Background: rate update,<br/>windows refresh"]
  Q -->|no| FS["Fetch rate, time zone, detail"]
  FS --> OK{"all fetched?"}
  OK -->|no| NR["ConfigEntryNotReady:<br/>HA retries setup"]
  OK -->|yes| WR["Refresh windows,<br/>save detail and time zone"]
  WR --> PL2["Load platforms"]
```

## 3. Each sample reaches a charge's peak only inside that charge's window

One 30 s sample, end to end ([rules 3.11 to 3.13](../../spec/oec_tariff_au/functional-design.md#demand-measurement)).

```mermaid
flowchart TD
  T["30 s tick"] --> R{"billing day passed<br/>since last reset?"}
  R -->|yes| RS["Close the month on every tracker,<br/>drop unlisted charges, refresh windows"]
  R -->|no| SY
  RS --> SY["Align trackers with cached charges"]
  SY --> P{"power reading<br/>a number?"}
  P -->|no| E["Skip"]
  P -->|yes| C{"cached month<br/>covers now?"}
  C -->|no| F["Flush pending blocks"]
  C -->|yes| W{"per charge:<br/>in its window?"}
  W -->|no| FL["Flush that charge's block"]
  W -->|yes| B["Add to the 30 min block,<br/>or the peak for monthly_peak"]
  RS --> N["Notify demand sensors when a peak<br/>or the last reset changed"]
  F --> N
  FL --> N
  B --> N
```

## 4. Availability follows each entity's own source

Tariff sensors depend on tariff-service; Demand Window on the cached months; demand sensors
on neither ([rules 3.16 to 3.18](../../spec/oec_tariff_au/functional-design.md#entities-and-availability)).

```mermaid
stateDiagram-v2
  state "Tariff sensors" as TSN {
    [*] --> Unavailable
    Unavailable --> Available: tariff-service answered
    Available --> Unavailable: update failed
  }
  state "Demand Window" as DW {
    [*] --> Covered: cached month holds now
    Covered --> Uncovered: cover runs out
    Uncovered --> Covered: windows refreshed
  }
```
