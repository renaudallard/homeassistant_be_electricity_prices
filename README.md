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
- **Flanders capacity tariff** — monthly peak tracked from any kW sensor or a fixed value; billed against the configured Fluvius sub-area.
- **Solar** — prosumer fee for the Walloon compensation regime (until 2030-12-31), and a per-kWh injection price entity that plugs straight into HA Energy.
- **Translated UI** — English, French, Dutch and German.
- **Self-healing** — last-known prices keep serving on outage; a repair issue surfaces if the snapshot goes stale.

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Eneco** | Power Fix · Power Flex · Power Dynamic | [Eneco PDF tariff cards](https://eneco.be/nl/elektriciteit-gas/tariefkaarten/) |
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Flow · Empty House | [`providers/engie.py`](./custom_components/be_electricity_prices/providers/engie.py) — Engie's public REST endpoint at `engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF`, one PDF per (contract, region) |
| **TotalEnergies** | Electricité Fixe/Variable · Impact · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`providers/totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) — stable URLs at `totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/`, parsed via `pdfplumber` (rotated columns) |
| **Luminus** | Comfy · Comfy+ · ComfyFlex · MaxxFix · BasicFix · BasicFlex · Dynamic | [`providers/luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) — Luminus's public REST endpoint at `luminus.be/api-next/get-pricelist/`, V/W only (no Brussels for market products) |
| **Mega** | Smart Fixed/Flex · Zen Fixed · Online Fixed/Flex · Cosy Fixed/Flex · Prepaid Fixed/Flex · Off-peak Fixed/Flex · Dynamic · Cap | [`providers/mega.py`](./custom_components/be_electricity_prices/providers/mega.py) — scrapes the public listing at `mega.be/fr/cartes-tarifaires` to resolve each `(product, region)` to its current PDF on `my.mega.be` |
| **Cociter** | Tarif Variable (BELIX) · Tarif Dynamique (quarter-hourly BELPEX) | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) — monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf` |

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
| `current_price` | All-in EUR/kWh **now**. Attributes: hourly breakdown for today + tomorrow, snapshot age, last fetch error, `cheapest_4h_today` and `most_expensive_4h_today` (chronologically sorted, disjoint lists of `{start, price}`). |
| `next_hour_price` | All-in EUR/kWh for the next hour. |
| `today_average` | Daily average all-in EUR/kWh. |
| `today_min` / `today_max` | Daily extremes. |
| `energy_component` | Energy-only EUR/kWh now (VAT-inclusive). |
| `network_component` | Distribution + transport EUR/kWh now (VAT-inclusive). |
| `taxes_component` | Levies EUR/kWh now (VAT-inclusive). |

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

`pypdf` is the only extra runtime dependency; Home Assistant installs it
automatically from the manifest.

## Configuration

The UI walks **up to seven steps**, depending on contract type and region.
No EUR values are asked — energy, DSO and tax rates all come from the
supplier's tariff card.

1. **Supplier + Region** — Flanders / Wallonia / Brussels.
2. **Contract** — filtered by supplier (populated from the live registry).
3. **DSO** — filtered by region.
4. **Meter type** — *mono* (single rate), *bi* (peak / off-peak), or *dynamic*
   (smart meter). Drives whether energy and distribution are billed at single
   or time-of-use rates.
5. **ENTSO-E API key** — only when the chosen contract is dynamic.
6. **Capacity tariff peak source** *(Flanders only)* — either a power sensor
   reporting your live kW draw, or a fixed kW value (default 2.5 kW, the VREG
   regulated minimum).
7. **Solar panels** — inverter capacity in kVA + the regime that applies:
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
walks the same seven steps, pre-filled with the current values. Change
supplier, contract, region, DSO, meter, ENTSO-E API key, capacity peak
source, or solar parameters — anything. The integration reloads
automatically when you finish, picking the new tariff card on the next
refresh.

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

Tests run against fixture PDFs in [`tests/fixtures/`](./tests/fixtures/) (real
Eneco + Cociter cards from April 2026). Refresh the fixtures with the
supplier's current PDF to re-run against new data.

A daily GitHub Actions workflow
([`.github/workflows/live_check.yml`](./.github/workflows/live_check.yml))
exercises every extractor against its real publication, retries up to five
times with growing backoff, and opens or updates a GitHub issue titled
`[live-check] supplier extractor broken …` on persistent failure. This
catches supplier URL changes and PDF layout shifts that would silently
break parsing.

## License

BSD 2-Clause. See [LICENSE](./LICENSE).
