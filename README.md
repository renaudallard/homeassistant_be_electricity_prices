<p align="center">
  <img src="logo.svg" alt="BE electricity - real-time prices" width="640"/>
</p>

<p align="center">
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/releases/latest">
    <img src="https://img.shields.io/github/v/release/renaudallard/homeassistant_be_electricity_prices?label=version&style=flat-square&sort=semver" alt="Latest release"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/actions/workflows/validate.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_electricity_prices/validate.yml?style=flat-square&label=hacs%20%2F%20hassfest" alt="Validate"/>
  </a>
  <a href="https://github.com/renaudallard/homeassistant_be_electricity_prices/actions/workflows/test.yml">
    <img src="https://img.shields.io/github/actions/workflow/status/renaudallard/homeassistant_be_electricity_prices/test.yml?style=flat-square&label=tests" alt="Tests"/>
  </a>
  <a href="https://www.home-assistant.io/">
    <img src="https://img.shields.io/badge/Home%20Assistant-2026.4%2B-41BDF5?logo=home-assistant&logoColor=white&style=flat-square" alt="Home Assistant"/>
  </a>
  <a href="https://hacs.xyz">
    <img src="https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=flat-square" alt="HACS"/>
  </a>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/renaudallard/homeassistant_be_electricity_prices?style=flat-square" alt="License"/>
  </a>
  <a href="https://www.paypal.me/RenaudAllard">
    <img src="https://img.shields.io/badge/PayPal-Donate-blue.svg?logo=paypal&style=flat-square" alt="PayPal"/>
  </a>
</p>

---

Home Assistant integration that exposes the **all-in real EUR/kWh paid** for
Belgian electricity, taking into account every component of a Belgian bill
(energy + transport + distribution + levies + VAT) plus the Flanders
capacity tariff billed on the monthly peak.

Energy prices are fetched **live** from each supplier's own published
tariff card. **No EUR values are hardcoded in the source.** Add a supplier
by writing one Python module that knows where to find that supplier's
publication and how to parse it.

> Targets Home Assistant **2026.4 or newer**.

## Highlights

- **Live tariff cards** — prices come straight from the supplier's published PDF; no EUR values live in this repo.
- **Whole-bill view** — energy, transport, distribution, regional levies and VAT all add up to a single EUR/kWh sensor.
- **Dynamic contracts** — `factor × spot + base` per hour, where `spot` is the Belgian day-ahead price from ENTSO-E.
- **Time-of-Use contracts** — Luminus SmartFlex and Engie Empower Flextime: 3 hour-of-day bands (peak / transition / offpeak) with the supplier's published rates per slot.
- **Tarif Impact (Wallonia)** — opt-in CWaPE 3-band distribution pricing (PIC 17–22, MEDIUM 7–11 + 22–1, ECO 1–7 + 11–17), orthogonal to the supplier tariff.
- **Flanders capacity tariff** — monthly peak tracked from any kW sensor or a fixed value; billed against the configured Fluvius sub-area.
- **Solar** — prosumer fee for the Walloon compensation regime (until 2030-12-31), and a per-kWh injection price entity that plugs straight into HA Energy.
- **Translated UI** — English, French, Dutch and German.
- **Self-healing** — last-known prices keep serving on outage; a repair issue surfaces if the snapshot goes stale.
- **Catalog drift detection** — the daily live-check diffs each supplier's public catalog against the registry and opens a GitHub issue when a new product appears.

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Eneco** | Power Fix · Power Flex · Power Dynamic | [`providers/eneco.py`](./custom_components/be_electricity_prices/providers/eneco.py) — stable URLs at `cdn.eneco.be/downloads/nl/general/tk/BC_032_012604_NL_ENECO_POWER_<FIX\|FLEX\|DYNAMIC>.pdf`, V/W only (no Brussels) |
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Empower Flextime *(TOU)* · Flow · Empty House | [`providers/engie.py`](./custom_components/be_electricity_prices/providers/engie.py) — Engie's public REST endpoint at `engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF`, one PDF per (contract, region) |
| **TotalEnergies** | Electricité Fixe/Variable · Impact · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`providers/totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) — stable URLs at `totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/`, parsed via `pdfplumber` (rotated columns) |
| **Luminus** | Comfy · Comfy+ · ComfyFlex · ComfyFlex+ · MaxxFix · MaxxFlex · BasicFix · BasicFlex · SmartFlex *(TOU)* · Dynamic | [`providers/luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) — Luminus's public REST endpoint at `luminus.be/api-next/get-pricelist/`, V/W only (no Brussels for market products) |
| **Mega** | Smart Fixed/Flex · Zen Fixed · Online Fixed/Flex · Cosy Fixed/Flex · Prepaid Fixed/Flex · Off-peak Fixed/Flex · Dynamic · Cap | [`providers/mega.py`](./custom_components/be_electricity_prices/providers/mega.py) — scrapes the public listing at `mega.be/fr/cartes-tarifaires` to resolve each `(product, region)` to its current PDF on `my.mega.be` |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Plenty · Bolt Online · Bolt Plenty Online | [`providers/bolt.py`](./custom_components/be_electricity_prices/providers/bolt.py) — stable URLs at `files.boltenergie.be/pricelists/<fix\|var>/`, parsed via `pdfplumber` (rotated columns + Unicode line-separators) |
| **Cociter** | Tarif Variable (BELIX) · Tarif Dynamique (quarter-hourly BELPEX) | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) — monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf` |
| **OCTA+** | Fixed · Eco Fixed · Smart Variable · Flux · Eco Flux · Dynamic · Eco Dynamic | [`providers/octaplus.py`](./custom_components/be_electricity_prices/providers/octaplus.py) — stable URLs at `files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<VL\|WL>_FR.pdf`, parsed via word-coordinate alignment (heavy character spacing in the tax block) — Flanders + Wallonia only |

Adding another supplier is a self-contained PR: drop a new module under
[`custom_components/be_electricity_prices/providers/`](./custom_components/be_electricity_prices/providers/),
register it in [`providers/__init__.py`](./custom_components/be_electricity_prices/providers/__init__.py),
and ship a fixture-based unit test. The Eneco module is the reference.

## What the integration computes

For every hour, an all-in EUR/kWh built up as

```
all_in = (energy + distribution + transport + levies) × (1 + VAT)
```

Each component comes from the supplier's tariff card and the configured DSO.
For dynamic contracts the energy term is `factor × spot + base`, where `spot`
is the Belgian day-ahead price from the ENTSO-E Transparency Platform.

VAT spreads uniformly across components, so `energy_component +
network_component + taxes_component` always equals `current_price` to the cent.

## Sensors

All sensors share one device per config entry.

### Always created

| Sensor | Description |
| --- | --- |
| `current_price` | All-in EUR/kWh **now**. Attributes: `today` and `tomorrow` (chronological lists of `{start, energy, network, taxes, all_in}`), snapshot age, last fetch error, `cheapest_4h_today` and `most_expensive_4h_today` (chronologically sorted, disjoint lists of `{start, price}`). |
| `next_hour_price` | All-in EUR/kWh for the next hour. |
| `today_average` | Daily average all-in EUR/kWh. |
| `today_min` / `today_max` | Daily extremes. |
| `tomorrow_average` | Average all-in EUR/kWh for tomorrow. Empty until ENTSO-E publishes the next-day curve (~13:00 CET) for dynamic contracts; available all day for fixed/variable contracts. |
| `tomorrow_min` / `tomorrow_max` | Tomorrow's extremes. Same availability as `tomorrow_average`. |
| `energy_component` | Energy-only EUR/kWh now (VAT-inclusive). |
| `network_component` | Distribution + transport EUR/kWh now (VAT-inclusive). |
| `taxes_component` | Levies EUR/kWh now (VAT-inclusive). |
| `yearly_cost` | Running bill **since Jan 1 of the current year** (or since first refresh, whichever is later). Configure once in the **Energy meters** step, two ways: (a) point at the four day/night register sensors directly (preferred when available — anchored to a Jan-1 baseline so `current − baseline` resets every year); or (b) point at single cumulative consumption / injection sensors (the integration splits deltas into day/night buckets via the bi-hourly schedule, persists them across restarts, and zeroes them every Jan 1). Resets to ~`fixed_fee + 12 × energy_fund` on Jan 1 (annual fees stay full-year). Goes negative under Walloon compensation when injection > consumption (uncapped — surplus is theoretically credited at the consumption rate, even though most suppliers floor the actual bill at zero). Always unavailable on dynamic / TOU contracts (no stable rate). |
| `tomorrow_prices_available` | Binary sensor. ON once the price table covers at least one hour with tomorrow's local date. Useful as a trigger for dynamic-tariff automations that should only fire after ENTSO-E publishes the next-day curve (~13:00 CET); always ON for fixed/variable contracts. |

### Conditional

| Sensor | Created when | Description |
| --- | --- | --- |
| `capacity_cost` | Region = Flanders | Current monthly capacity cost in EUR (`peak_kw × DSO_capacity_rate / 12`). |
| `monthly_peak_kw` | Region = Flanders | Running monthly peak power in kW (resets the 1st). |
| `prosumer_cost` | Compensation regime + `solar_kva > 0` | Monthly DSO compensation fee in EUR (`solar_kva × DSO_prosumer_rate / 12`). Only valid for Walloon installations certified before 2024-01-01; ends 2030-12-31. |
| `injection_price` | Injection regime | EUR/kWh paid for energy fed back to the grid. Dynamic contracts get `factor × spot + base` from the supplier's PDF using the live ENTSO-E spot; static contracts get the supplier's printed monthly indicative. Plug into HA Energy's *Solar production* → *I receive variable compensation based on a tariff* slot. Can go negative at low spot (you pay to inject). |

## Installation

### HACS (recommended)

1. Open HACS, three-dot menu → **Custom repositories**.
2. Add `https://github.com/renaudallard/homeassistant_be_electricity_prices` as type **Integration**.
3. Install **Belgian Electricity Prices** and restart Home Assistant.
4. **Settings → Devices & services → Add integration → Belgian Electricity Prices**.

### Manual

Download the latest [release zip](https://github.com/renaudallard/homeassistant_be_electricity_prices/releases),
extract it under `<config>/custom_components/be_electricity_prices/`, and
restart Home Assistant.

`pypdf` and `pdfplumber` are the only extra runtime dependencies; Home
Assistant installs them automatically from the manifest.

## Configuration

The UI walks **up to eight steps**, depending on contract type and region.
No EUR values are asked — energy, DSO and tax rates all come from the
supplier's tariff card.

1. **Supplier + Region** — Flanders / Wallonia / Brussels. Suppliers that
   don't sell in your region are filtered out.
2. **Contract** — filtered by supplier *and* region (e.g., TotalEnergies
   Impact only appears in Wallonia).
3. **DSO** — filtered by region.
4. **Meter type** — *mono* (single rate), *bi* (peak / off-peak), or
   *dynamic* (smart meter). TOU contracts (Luminus SmartFlex, Engie
   Empower Flextime) default to *dynamic*.
5. **DSO billing mode** *(Wallonia only)* — *Simple* / *Bi-horaire* / *Tarif
   Impact*. Tarif Impact uses the CWaPE 3-band hour-of-day rates and
   requires a smart meter; Simple and Bi-horaire follow the existing
   meter convention.
6. **ENTSO-E API key** — only when the chosen contract is dynamic.
7. **Capacity tariff peak source** *(Flanders only)* — either a power sensor
   reporting your live kW draw, or a fixed kW value (default 2.5 kW, the VREG
   regulated minimum).
8. **Solar panels** — inverter capacity in kVA + the regime that applies:
   - **No solar panels** *(default)* — no extra sensors.
   - **Compensation regime** — Wallonia only, installations **certified before
     2024-01-01**, valid until 2030-12-31. Creates `prosumer_cost`.
   - **Injection tariff** — post-2024 Walloon installations and Flemish smart
     meters. Creates `injection_price`, ready for HA Energy.

### Getting an ENTSO-E API key

Required only for dynamic contracts. Register on the
[ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) and email
`transparency@entsoe.eu` from your registration address with the subject
`Restful API access`.

### Reconfiguring later

**Settings → Devices & services → Belgian Electricity Prices → Configure**
walks the same chain of steps, pre-filled with the current values. Change
supplier, contract, region, DSO, meter, DSO billing mode, ENTSO-E API
key, capacity peak source, or solar parameters — anything. The integration
reloads automatically when you finish, picking the new tariff card on the
next refresh.

## Daily operation

### Refresh cadence

- **Supplier snapshot** — refreshed every 24 h.
- **Spot prices** *(dynamic only)* — hourly via ENTSO-E; tomorrow's curve picked up after publication around midday CET.
- **Monthly capacity peak** *(Flanders)* — tracked continuously, resets on the 1st of each local month.

### Failure mode

If a refresh fails, the coordinator keeps serving the last known snapshot
and exposes `snapshot_age_hours`, `snapshot_stale` and `last_error` as
attributes on `sensor.<...>_current_price`. Snapshots older than **7 days**
surface a repair issue under **Settings → System → Repairs**, so the
warning is visible without inspecting attributes; the issue auto-clears on
the next successful refresh.

### `be_electricity_prices.refresh` service

Drops the cached supplier snapshot **and** the ENTSO-E spot cache for every
loaded entry, then re-fetches both immediately. Handy after a tariff card
update or to clear a transient fetch error without waiting for the next 24 h
tick. No fields.

### `be_electricity_prices.cheapest_window` / `most_expensive_window` services

Return the cheapest (or most expensive) contiguous N-hour window in the
upcoming price table. Both services share the same fields:

| Field | Default | Description |
| --- | --- | --- |
| `duration_hours` | _required_ | Window length (0.5-48 h, rounded up to whole hours). |
| `entry_id` | first loaded | Optional config entry to target. |
| `earliest_start` | now | Don't consider windows starting before this time. |
| `latest_end` | end of the cached table | Don't consider windows ending after this time. |

Response shape:

```yaml
start: "2026-04-30T03:00:00+02:00"
end:   "2026-04-30T06:00:00+02:00"
duration_hours: 3
average_eur_per_kwh: 0.184372
hours:
  - hour: "2026-04-30T03:00:00+02:00"
    all_in: 0.18012
  - hour: "2026-04-30T04:00:00+02:00"
    all_in: 0.18391
  - hour: "2026-04-30T05:00:00+02:00"
    all_in: 0.18908
```

Example automation that starts EV charging at the cheapest 4 h block of the
night:

```yaml
trigger:
  - platform: time
    at: "13:30:00"  # ENTSO-E next-day curve is published around 13:00 CET
condition:
  - condition: state
    entity_id: binary_sensor.eneco_zon_wind_dynamisch_wallonia_tomorrow_prices_available
    state: "on"
action:
  - service: be_electricity_prices.cheapest_window
    data:
      duration_hours: 4
      earliest_start: "{{ today_at('22:00') }}"
      latest_end: "{{ (today_at('06:00') + timedelta(days=1)) }}"
    response_variable: window
  - service: switch.turn_on
    target:
      entity_id: switch.ev_charger
    # Schedule the rest of the automation at window.start.
```

### Diagnostics

**Settings → Devices & services → Belgian Electricity Prices →** three-dot
menu **→ Download diagnostics** dumps the active config (with the ENTSO-E
API key redacted), the snapshot metadata, and the full hourly breakdown
for today + tomorrow. Attach it when reporting an issue.

## Development

```bash
ruff check .
ruff format --check .
mypy --strict custom_components/be_electricity_prices
pytest tests/
python scripts/live_check.py    # hits real supplier endpoints
```

Tests run against fixture PDFs and HTML snippets in
[`tests/fixtures/`](./tests/fixtures/) (real April 2026 cards from every
registered supplier, plus tiny HTML snippets under
`tests/fixtures/discover/` for catalog-discovery tests). Refresh a
fixture with the supplier's current PDF to re-run against new data.

A daily GitHub Actions workflow
([`.github/workflows/live_check.yml`](./.github/workflows/live_check.yml))
runs two phases against the live supplier endpoints:

- **Extractor phase** — every (contract, region) tuple is fetched and
  parsed; retries up to five times with exponential backoff. Persistent
  failures open or update a GitHub issue titled
  `[live-check] supplier extractor broken …`.
- **Catalog phase** — each supplier's `discover()` is run against its
  public listing page; any product visible at the supplier but missing
  from the registry opens a separate issue
  `[live-check] new supplier products detected …` so a parser regression
  and a catalogue addition stay in distinct threads.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).
