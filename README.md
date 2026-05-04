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
- **Year-to-date cost** — `current_year_cost` sensor reports your running bill in EUR since Jan 1, computed day by day (or hour by hour for TOU contracts) from HA's recorder (consumption × the tariff of the month that day/hour belongs to). Each day is billed at its own month's published rate when the supplier archives historical cards (Eneco / Cociter / Ecopower); other suppliers fall back to the current rate as a proxy. **TOU contracts** (Engie Empower Flextime, Luminus SmartFlex) use the per-hour path so each kWh hits its actual peak / transition / offpeak rate. **Dynamic contracts** report only the pro-rated fees for now — the live `current_price` sensor is unaffected, but YTD billing of past months would need historical hourly ENTSO-E spots that v1 does not replay. Compensation regime nets injection against consumption across the whole year (clamped at zero, since most Walloon suppliers forfeit surplus injection past consumption). Annual fees are pro-rated to the elapsed fraction of the year so the figure grows day by day instead of jumping to the full annual on Jan 1.
- **Cheapest / most-expensive window services** — find the optimal contiguous N-hour window in the upcoming price table for EV charging, heat-pump cycles, or peak avoidance.
- **Tomorrow-available trigger** — `tomorrow_prices_available` binary sensor flips ON once ENTSO-E publishes the next-day curve, so dynamic automations don't fire too early.
- **ENTSO-E key validated at setup** — the config flow hits the real endpoint with the entered token and rejects bad keys before the entry is saved.
- **Translated UI** — English, French, Dutch and German.
- **One-off supplier comparison** — the OptionsFlow has a *Compare another supplier* path that quotes a different supplier and contract against your current region / DSO / peak / solar settings. The annual estimate uses your **measured rolling-year consumption** (and, for solar users, injection) read from the same kWh sensors that feed `current_year_cost`, with a sensible 3500 kWh fallback when no sensor is wired. The result page also shows a **year-to-date what-if**: the actual kWh you've used since 1 January re-priced at each supplier's current rate. The meter type is overridable for static contracts (compare *what if I were on bi-hourly billing under supplier X*). Solar regimes are honoured: compensation nets consumption against injection, injection regime credits each supplier's own injection price. No second entry, no extra polling, nothing saved.
- **Self-healing** — last-known prices keep serving on outage. Three repair issues surface under **Settings → System → Repairs**: snapshot older than 7 days, supplier extractor parse failure, and ENTSO-E rejecting the API key. Each auto-clears on the next successful refresh.
- **Catalog drift detection** — the daily live-check diffs each supplier's public catalog against the registry and opens a GitHub issue when a new product appears, plus per-supplier wallclock + bytes-received telemetry to flag silent slowdowns and PDF size jumps.

## Supported providers

| Supplier | Contracts | Source |
| --- | --- | --- |
| **Bolt** | Bolt Fixe · Bolt Plenty Fixe · Bolt Variable · Bolt Plenty Variable · Bolt Online · Bolt Plenty Online | [`providers/bolt.py`](./custom_components/be_electricity_prices/providers/bolt.py) — stable URLs at `files.boltenergie.be/pricelists/<fix\|var>/`, parsed via `pdfplumber` (rotated columns + Unicode line-separators) |
| **Cociter** | Tarif Variable (BELIX) · Tarif Dynamique (quarter-hourly BELPEX) | [`providers/cociter.py`](./custom_components/be_electricity_prices/providers/cociter.py) — monthly cards `RCVar_YMR_Coop-YYMM-fr.pdf` / `RCDyn_SM3_Coop-YYMM-fr.pdf` |
| **DATS 24** | Elektriciteit Groen Variabel (BE_spotRLP-indexed monthly) | [`providers/dats24.py`](./custom_components/be_electricity_prices/providers/dats24.py) — stable URL at `profile.dats24.be/api/v1/ratecard?...` (returns a PDF despite the JSON-style query). Colruyt subsidiary; Flanders + Wallonia. Single product covers mono / bi / exclusive-night meter rates and includes the BE_spotSPP injection formula. |
| **Ecopower** | Groene Burgerstroom (50% fixed + 50% Belpex DA, indexed monthly) | [`providers/ecopower.py`](./custom_components/be_electricity_prices/providers/ecopower.py) — monthly cards scraped from `ecopower.be/groene-stroom/prijs-nieuw`; Flanders cooperative, Flanders only. Cards are HTVA so `vat_rate=0.06`. |
| **Eneco** | Zon & Wind Vast · Zon & Wind Flex · Zon & Wind Dynamisch | [`providers/eneco.py`](./custom_components/be_electricity_prices/providers/eneco.py) — monthly cards `cdn.eneco.be/downloads/nl/general/tk/BC_032_<NNNNNN>_NL_ENECO_POWER_<FIX\|FLEX\|DYNAMIC>.pdf` resolved from the public listing page each fetch (issue number rotates monthly), V/W only (no Brussels) |
| **Engie** | Easy Fixed · Easy Variable · Direct Online · Basic Online · Dynamic · Empower Fixed · Empower Variable · Empower Flextime *(TOU)* · Flow · Empty House | [`providers/engie.py`](./custom_components/be_electricity_prices/providers/engie.py) — Engie's public REST endpoint at `engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF`, one PDF per (contract, region) |
| **Luminus** | Comfy · Comfy+ · ComfyFlex · ComfyFlex+ · MaxxFix · MaxxFlex · BasicFix · BasicFlex · SmartFlex *(TOU)* · Dynamic | [`providers/luminus.py`](./custom_components/be_electricity_prices/providers/luminus.py) — Luminus's public REST endpoint at `luminus.be/api-next/get-pricelist/`, V/W only (no Brussels for market products) |
| **Mega** | Smart Fixed/Flex · Zen Fixed · Online Fixed/Flex · Cosy Fixed/Flex · Off-peak Fixed/Flex · Dynamic · Cap | [`providers/mega.py`](./custom_components/be_electricity_prices/providers/mega.py) — scrapes the public listing at `mega.be/fr/cartes-tarifaires` to resolve each `(product, region)` to its current PDF on `my.mega.be` |
| **OCTA+** | Fixed · Eco Fixed · Smart Variable · Flux · Eco Flux · Dynamic · Eco Dynamic | [`providers/octaplus.py`](./custom_components/be_electricity_prices/providers/octaplus.py) — stable URLs at `files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<VL\|WL>_FR.pdf`, parsed via word-coordinate alignment (heavy character spacing in the tax block) — Flanders + Wallonia only |
| **TotalEnergies** | Electricité Fixe/Variable · Impact · myComfort · myComfort Fixe · myDrive · myDynamic · myEssential · myEssential Fixe | [`providers/totalenergies.py`](./custom_components/be_electricity_prices/providers/totalenergies.py) — stable URLs at `totalenergies.be/static/marketing-documents/b2c/tariff-card/latest/`, parsed via `pdfplumber` (rotated columns) |

Adding another supplier is a self-contained PR: drop a new module under
[`custom_components/be_electricity_prices/providers/`](./custom_components/be_electricity_prices/providers/),
register it in [`providers/__init__.py`](./custom_components/be_electricity_prices/providers/__init__.py),
and ship a fixture-based unit test. The Eneco module is the reference.

### How often the integration polls

The coordinator ticks once an hour. On each tick it runs the supplier's
**`probe()`** — a cheap freshness check that returns a key (`Last-Modified`,
`ETag`, or the resolved PDF URL) — and only re-runs the full PDF fetch when
that key changes from what we last fetched. This catches a supplier
publication within an hour at near-zero ongoing bandwidth instead of a
fixed 24-hour schedule. Suppliers that have no usable probe (Engie,
Luminus and DATS 24, where the only cheap response is the PDF itself)
keep the time-based 24-hour TTL.

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
| `fixed_fee_eur_per_year` | Supplier's flat annual subscription fee (EUR/year), parsed from the tariff card. |
| `energy_fund_eur_per_month` | Flemish Energiefonds in EUR/month (€0 outside Flanders, and €0 in Flanders for domiciled customers). |
| `current_year_cost` | Running bill **since Jan 1 of the current year**, computed day by day against HA's recorder. Configure once in the **Energy meters** step, two ways: (a) point at the four day/night register sensors directly (preferred when available); or (b) point at single cumulative consumption / injection sensors (for bi-hourly meters the integration recovers the day/night split per past day from the recorder's hourly statistics binned by the bi-hourly schedule). Each day's kWh is multiplied by the tariff card published for the month it belongs to: when the supplier archives historical cards (Eneco / Cociter / Ecopower) past months use their own published rates; suppliers without an archive (Bolt / Mega / OCTA+ / TotalEnergies / Engie / Luminus / DATS 24) fall back to the current rate as a proxy. Annual fees (`yearly_fixed_fee + 12 × energy_fund_eur_per_month + 12 × prosumer_cost`) are summed per archived month using each month's snapshot, then pro-rated by `days_in_month_in_ytd / days_in_year` so the YTD running total still grows uniformly across the calendar year — on Jan 1 the sensor sits at ~0 and grows day by day, and on Dec 31 it carries the full annual amount. A supplier that re-indexes its fixed fee or energy fund mid-year is honoured for the months it applies to (same per-month snapshot path the prosumer fee already uses). Under Walloon compensation regime, injection is netted against consumption across the whole YTD and the energy term is clamped at zero (most suppliers forfeit surplus injection past consumption, so the bill never settles negative). Always numeric: a fresh install in May still produces a meaningful figure for the year so far, as long as the recorder has been collecting daily statistics for the configured kWh sensors. |
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

The UI walks **up to nine steps**, depending on contract type and region.
No EUR values are asked — energy, DSO and tax rates all come from the
supplier's tariff card.

1. **Supplier + Region** — Flanders / Wallonia / Brussels. Suppliers that
   don't sell in your region are filtered out.
2. **Contract** — filtered by supplier *and* region (e.g., TotalEnergies
   Impact only appears in Wallonia).
3. **DSO** — filtered by region.
4. **Meter type** — *mono* (single rate), *bi* (peak / off-peak), or
   *dynamic* (smart meter). Dynamic and TOU contracts (Luminus SmartFlex,
   Engie Empower Flextime) lock the picker to *dynamic* — the SMR3
   meter is required to bill by hour-of-day.
5. **DSO billing mode** *(Wallonia only)* — *Simple* / *Bi-horaire* / *Tarif
   Impact*. Tarif Impact uses the CWaPE 3-band hour-of-day rates and
   requires a smart meter; Simple and Bi-horaire follow the existing
   meter convention.
6. **ENTSO-E API key** *(dynamic contract only)* — validated against the
   real ENTSO-E endpoint at submission; bad keys are rejected before the
   entry is saved.
7. **Capacity tariff peak source** *(Flanders only)* — either a power sensor
   reporting your live kW draw, or a fixed kW value (default 2.5 kW, the VREG
   regulated minimum). The peak sensor field is auto-filled from the kW input
   of any Riemann `integration` helper that feeds the Energy dashboard's grid
   source, so users with the typical P1-power → kWh-Riemann → dashboard chain
   don't have to pick the same sensor twice.
8. **Solar panels** — inverter capacity in kVA + the regime that applies:
   - **No solar panels** *(default)* — no extra sensors.
   - **Compensation regime** — Wallonia only, installations **certified before
     2024-01-01**, valid until 2030-12-31. Creates `prosumer_cost`.
   - **Injection tariff** — post-2024 Walloon installations and Flemish smart
     meters. Creates `injection_price`, ready for HA Energy.
9. **Energy meters** *(optional, all four / two fields are skippable)* —
   feeds the `current_year_cost` sensor. Two ways to wire it:
   - **Day/night register sensors** (4 fields): point at the cumulative
     kWh registers from your meter. The integration reads each day's
     delta from HA's long-term statistics, so the sensor reflects
     metered totals exactly and resets cleanly on Jan 1.
   - **Cumulative total sensors** (2 fields): point at a single
     running consumption sensor and a single running injection sensor.
     The integration reads daily kWh from the recorder and recovers
     the day/night split per past day from the recorder's hourly
     statistics binned via the bi-hourly schedule (no in-process
     buckets). Useful when your P1 / digital-meter integration only
     exposes totals (the standard HA case).
   - **Mix and match**: each side (consumption, injection) is
     resolved independently. You can wire registers for consumption
     and a single total for injection, or vice-versa. Partial
     register-pair wiring on either side is rejected so a missing
     band can't silently undercount.
   - When both wirings are filled for the same side the day/night
     registers win. Missing inputs collapse to the fees-only floor —
     the sensor never goes unknown.
   - **Auto-fill from the Energy dashboard**: if you've already
     configured a grid source in HA's Energy dashboard, the cumulative
     consumption / injection fields are pre-selected from the
     dashboard's first grid source so you don't pick the same sensor
     twice. When a `utility_meter` helper rooted at that grid source
     splits it into peak / offpeak (or jour / nuit, dag / nacht, piek /
     dal — case-insensitive, separator-tolerant) child tariffs, the
     four day/night registers are pre-selected too. Tariffs whose
     names don't map unambiguously to a day/night slot are left blank
     so a misnamed helper can't silently mis-bill. Whatever is
     pre-filled stays editable; an existing manual pick is never
     overwritten.

### Getting an ENTSO-E API key

Required only for dynamic contracts. Register on the
[ENTSO-E Transparency Platform](https://transparency.entsoe.eu/) and email
`transparency@entsoe.eu` from your registration address with the subject
`Restful API access`.

### Reconfiguring later

**Settings → Devices & services → Belgian Electricity Prices → Configure**
opens a two-option menu:

- **Edit settings** — walks the same chain of steps, pre-filled with the
  current values. Change supplier, contract, region, DSO, meter, DSO
  billing mode, ENTSO-E API key, capacity peak source, or solar
  parameters — anything. The integration reloads automatically when you
  finish, picking the new tariff card on the next refresh.
- **Compare another supplier** — one-off price quote against a different
  supplier and contract, with your region / DSO / peak / solar
  settings held fixed for an apples-to-apples comparison. Only
  suppliers offering a contract of the same kind as yours (static
  vs dynamic) are shown. Static contracts also let you override the
  meter type (mono / bi) so you can quote *what if I were on bi-hourly
  billing under supplier X*. The result page lists per-kWh price now,
  a projected yearly bill computed from your **measured rolling-year
  kWh** (recorder data from the consumption sensor configured in the
  meters step, or a 3500 kWh fallback), and a **year-to-date what-if**
  that re-prices your actual YTD kWh at each supplier's current rate
  with pro-rated annual fees. Solar regimes are honoured: compensation
  nets consumption against injection, injection regime credits each
  supplier's own injection price against the bill.
  Submit closes the dialog without changing anything; nothing is saved.

## Daily operation

### Refresh cadence

- **Supplier snapshot** — the coordinator runs a cheap `probe()` every
  hour and only re-fetches the full PDF when the probe key changes
  (see *How often the integration polls* above). Suppliers without a
  probe (Engie, Luminus, DATS 24) fall back to a 24 h time-based TTL.
  Multiple entries pointing at the same
  `(supplier, contract, region)` tuple share their fetched snapshot
  through an in-memory cache, so the same PDF is never polled twice.
- **Spot prices** *(dynamic only)* — hourly via ENTSO-E; tomorrow's curve picked up after publication around midday CET.
- **Monthly capacity peak** *(Flanders)* — tracked continuously, resets on the 1st of each local month.
- **`current_year_cost`** — recomputed every coordinator tick from HA's
  recorder. The recorder's daily statistics are the source of truth for
  per-band kWh (no in-process counters that could drift across restarts);
  per-month tariff cards live in an in-memory cache keyed by
  `(supplier, contract, region, YYYY-MM)`, looked up once per month
  touched by the YTD window. Annual fees are pro-rated to the elapsed
  fraction of the year, so on Jan 1 the sensor sits at ~0 and grows day
  by day instead of jumping to the full annual upfront.

### Failure mode

If a refresh fails, the coordinator keeps serving the last known snapshot
and exposes `snapshot_age_hours`, `snapshot_stale` and `last_error` as
attributes on `sensor.<...>_current_price`. Three repair issues surface
under **Settings → System → Repairs** so problems are visible without
inspecting attributes; each auto-clears on the next successful refresh:

- **`snapshot_stale_<entry>`** — the cached snapshot is older than **7
  days**.
- **`extractor_failed_<entry>`** — the supplier extractor raised an
  error (typically a layout drift on the supplier's PDF/HTML); cached
  prices keep serving.
- **`entsoe_auth_failed_<entry>`** *(dynamic contracts only)* — ENTSO-E
  returned 401 for the configured API key. Edit the entry's options
  and replace the key with a fresh token from
  transparency.entsoe.eu.

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
| `duration_hours` | _required_ | Window length in whole hours (1-48; the price table is hourly). |
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
    entity_id: binary_sensor.<your_entry>_tomorrow_prices_available
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

## Exclusive-night meter circuit

Belgian households with an electric water heater or night-storage
heater often have a separate exclusive-night meter circuit billed at
the supplier's published `exclusive_night` rate. Configure it as a
**second config entry**:

1. Add a new Belgian Electricity Prices entry alongside your primary
   one.
2. On the meter step, pick **Exclusive-night circuit (separate
   meter)**.
3. On the energy meters step, point the cumulative-consumption sensor
   at your exclusive-night kWh sensor (the second register on a
   bi-hourly meter, or a dedicated kWh sensor on a separate circuit).

Energy is billed at the supplier's `exclusive_night` rate; distribution
falls back to the DSO's off-peak rate (closer to the real bill than
the day rate). The primary entry keeps your day-circuit consumption
on mono / bi / dynamic; YTD and capacity tracking work normally on
both entries.

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
