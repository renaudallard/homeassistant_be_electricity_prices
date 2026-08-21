# Architecture

This document is the big-picture map of the Belgian Electricity Prices integration: what it
computes, how its modules divide the work, the domain model it encodes (region, DSO sub-area,
supplier, contract, meter, plus the orthogonal DSO tariff mode and solar regime), and the
end-to-end path a value travels from a supplier's published tariff card to a Home Assistant
sensor. It is written for contributors who know Python and Home Assistant but not this codebase
or Belgian electricity billing. Read it first, then dive into the per-area docs it links.

Related deep-dive docs:

- [glossary.md](glossary.md): Belgian-energy and HA vocabulary used throughout.
- [coordinator.md](coordinator.md): the refresh lifecycle, caching, and data dict in full.
- [pricing-model.md](pricing-model.md): `compute_breakdown`, the tax, injection, and capacity math.
- [provider-framework.md](provider-framework.md): the extractor protocol, dataclasses, and registry.
- [config-flow.md](config-flow.md): the config and options wizard.
- [data-sources.md](data-sources.md): the ENTSO-E spot client and recorder backfill.
- [entities.md](entities.md): sensors, the binary sensor, the button, services, diagnostics, i18n.
- [ci-and-testing.md](ci-and-testing.md): the live-check harness, test suite, and CI workflows.
- Per-supplier notes live under [providers/](providers/) (one file per extractor).

## What the integration does

The integration exposes the true all-in residential price paid for electricity in Belgium, as a
single EUR/kWh value per price slot, plus a solar injection (feed-in) credit. A Belgian bill is
not one number from one party: it fuses three independently sourced inputs.

1. The supplier energy formula, fetched live from that supplier's own published tariff card (a
   PDF, an HTML listing, or a small API), never hardcoded. See `const.py:28`
   ("No prices live here") and `providers/base.py:38` ("No EUR values live in Python source").
2. The DSO (distribution grid operator) network and capacity overlay, parsed from the same card
   for the sub-area the user selected.
3. Federal and regional taxes and levies, and, for solar, the injection tariff.

The headline formula the coordinator builds for each slot is:

```
all_in = (energy + distribution + transport + levies) x (1 + VAT)
```

The design rule that shapes the whole codebase: no EUR value is ever stored in Python source.
Every rate comes from a live fetch of a supplier's card. Adding a supplier is therefore a
self-contained task, one new module plus a registry line, and the test suite is fixture-driven
against real card samples rather than hardcoded numbers.

### Home Assistant metadata

From `manifest.json`:

| Key | Value | Why it matters |
| --- | --- | --- |
| `domain` | `be_electricity_prices` | Config-entry namespace and service prefix; also the `DOMAIN` constant (`const.py:35`). |
| `name` | Belgian Electricity Prices | Display name. |
| `integration_type` | `service` | It provides derived data (prices), not a physical device. |
| `iot_class` | `cloud_polling` | It polls remote cards and ENTSO-E on a timer, no push. |
| `config_flow` | `true` | Set up entirely through the UI wizard (`config_flow.py`). |
| `requirements` | `pypdf>=4.0`, `pdfplumber>=0.11`, `defusedxml>=0.7` | PDF parsing (pypdf, pdfplumber) for tariff cards; defusedxml to parse the ENTSO-E XML safely. |
| `after_dependencies` | `energy`, `recorder` | The integration writes cost statistics into the recorder and plugs into the Energy dashboard, but must not hard-require them, so they load first when present. |
| `version` | `0.13.3` | Manifest version. CI auto-tags and publishes a release when this bumps on `main`. |

Home Assistant 2026.4 or newer is the declared minimum (README, `hacs.json`).

## The module map

Every Python module in `custom_components/be_electricity_prices/`. Paths in this table are
relative to that package directory.

| Module | Responsibility |
| --- | --- |
| `__init__.py` | Integration entry point. Registers domain services once at `async_setup`, sets up and tears down each config entry (`async_setup_entry` / `async_unload_entry` / `async_remove_entry`), owns the slot-boundary push and one-shot backfill scheduling, and implements the `refresh`, `cheapest_window`, `most_expensive_window`, and `backfill_statistics` service handlers. |
| `coordinator.py` | The `DataUpdateCoordinator` subclass and `CoordinatorData`. Owns `__init__`, the tick (`_async_update_data` / `_update_body`), the per-slot price table, and persistence. The four mixins below carry the rest of the class; the leaf modules under them are plain functions the tick calls. |
| `coordinator_snapshot.py` | `_SnapshotMixin`: the snapshot fetch / freshness state machine. Probe, TTL, the shared cross-entry cache and its adoption, and the negative-fetch cache. |
| `coordinator_issues.py` | `_IssuesMixin`: the seven Repairs handlers and the shared `_sync_issue` helper they all raise and clear through. A pure reader of coordinator state. |
| `coordinator_spots.py` | `_SpotsMixin`: ENTSO-E fetching. The live day-ahead curve, the historical spot cache and its week-sized backfill, and the Synergrid SPP refresh. |
| `coordinator_peak.py` | `_PeakMixin`: the Flemish capacity peak (`_track_monthly_peak`) and its 12-month history. |
| `snapshot_store.py` | Snapshot serialization to and from `.storage`, the schema-version gate, `_resolve_snapshot` (per-entry VAT and excise-band resolution), and the shared cross-entry snapshot cache with its lock, negative-fetch cache and eviction. |
| `cohort.py` | Signing-cohort pricing: retrieves the archived signing-month card and splices its energy leg onto the delivery month's overlays. |
| `injection.py` | The injection taxonomy: which shape a card is, the per-slot rate shared by the live scalar and the YTD walk, and the historical rate. |
| `fees.py` | Standing charges: capacity tariff, Brussels OSP, prosumer forfait, and the annual static-fee sum the three cost paths share. |
| `ytd_cost.py` | The year-to-date cost walk: per-month fees, the hourly and per-day energy paths, and the spot-injection credit. |
| `projected_cost.py` | The full-calendar-year projection behind `projected_year_cost`: one pass at today's tariffs over the entry's own metered yearly volume, plus the basis strings that say what was measured and what was assumed. |
| `energy_meters.py` | Reads the configured kWh entities out of the recorder and the live state machine, and fans register pairs into band slots. |
| `spot_stats.py` | Spot aggregates: the current billing slot's spot, monthly means, and the SPP-weighted variants. |
| `pricing.py` | Pure pricing engine. `compute_breakdown` fuses a `SupplierSnapshot`, the chosen `DsoOverlay`, the taxes, meter type, DSO tariff mode, and (for dynamic) the slot spot into a `PriceBreakdown`. Also the slot-grid helpers (`slot_start`, `slot_delta`, `slots_per_hour`), `is_offpeak`, and `tou_slot`. No I/O, no HA imports where avoidable, so it is trivially unit-testable. |
| `config_flow.py` | The config wizard's step handlers (supplier and region, contract, DSO sub-area, meter, DSO billing mode, ENTSO-E key, capacity, connection power, solar, energy meters) and the options flow. |
| `flow_schemas.py` | The voluptuous schema builders and validators each step calls, including the ENTSO-E key check against the live endpoint. |
| `flow_prefill.py` | Suggests meter and capacity defaults from Home Assistant's Energy dashboard and the entity registry. Every failure mode degrades to suggesting nothing. |
| `compare_flow.py` | The options flow's one-off "compare another supplier" branch, as a mixin. |
| `compare_quote.py` | The annual-cost arithmetic that branch displays. Kept out of `pricing.py`, which is a leaf the coordinator imports. |
| `api.py` | The ENTSO-E day-ahead spot client (`EntsoeClient`). Fetches the Belgian day-ahead curve (hourly or native 15-minute) and parses the XML with defusedxml. Raises `EntsoeError` / `EntsoeAuthError`. |
| `synergrid.py` | The Synergrid solar production profile (SPP) fetcher, for the optional SPP-weighted custom injection. Streams the annual ex-ante workbook and parses only its small sheet, via `defusedxml` (already a requirement) so a nested-entity payload cannot be expanded; returns hourly weights, or `{}` on any failure so the coordinator falls back to the plain mean. |
| `backfill.py` | Writes historical cost statistics into HA's recorder so the Energy dashboard shows price history immediately. `backfill_if_missing` runs once on install; `backfill_range` backs the `backfill_statistics` service. |
| `const.py` | All constants and config keys: `DOMAIN`, `PLATFORMS`, region and DSO keys, `CONF_*` option keys, meter types, DSO tariff modes, solar regimes, resolution tokens, TTLs, and the ENTSO-E endpoint. Intentionally holds zero prices. |
| `sensor.py` | The sensor platform: current price, next-hour price, year-to-date cost, injection price, fixed-fee and energy-fund sensors, and diagnostic sensors. |
| `binary_sensor.py` | The `tomorrow_prices_available` binary sensor (ON once ENTSO-E has published the next-day curve). |
| `button.py` | A refresh button entity that forces an immediate snapshot re-fetch for the entry. |
| `diagnostics.py` | The HA download-diagnostics payload for an entry (config, snapshot metadata, last error), redacting the ENTSO-E key. |
| `providers/base.py` | The extractor protocol and every shared dataclass: `SupplierExtractor`, `Contract`, `SupplierSnapshot`, the six `EnergyRates` shapes, `DsoOverlay`, `TaxOverlay`, `InjectionRates`, and the fetch / probe / archive callable types. |
| `providers/__init__.py` | The supplier registry: imports each module's `EXTRACTOR`, exposes the `EXTRACTORS` dict, and the `get()` / `all_extractors()` lookups. |
| `providers/_pdf.py` | Shared PDF and HTTP helpers used by the extractors (text extraction, transient-error classification via `is_transient_fetch_error`, and column-alignment utilities). |

In addition, fifteen scraped supplier modules live under `providers/`, each exposing a top-level
`EXTRACTOR`: `bolt.py`, `cociter.py`, `dats24.py`, `ebem.py`, `ecofix.py`, `ecopower.py`,
`eneco.py`, `energiebe.py`, `energyvision.py`, `engie.py`, `frank.py`, `luminus.py`, `mega.py`,
`octaplus.py`, and `totalenergies.py`. Each has its own page under [providers/](providers/). A
sixteenth module, `custom.py`, is the expert escape hatch: it is not scraped (its `fetch` is a
stub) and the
coordinator builds its snapshot from the config entry. The framework they implement is
documented in [provider-framework.md](provider-framework.md).

## The core domain model

A config entry pins one point in a small product space. The primary hierarchy narrows from a
region down to a meter; two further axes (the DSO tariff mode and the solar regime) are
orthogonal to it and to each other.

```
region  (flanders | wallonia | brussels)                     const.py:43
  |
  +-- DSO sub-area   (which grid operator's overlay applies)  const.py:101
  |     flanders : 8 Fluvius sub-areas (materially different rates)
  |     wallonia : AIEG | AIESH | ORES | RESA | REW
  |     brussels : Sibelga (only one)
  |
  +-- supplier   (which extractor's EXTRACTOR is used)        providers/__init__.py:65
        |
        +-- contract  (a Contract with a TariffKind)          providers/base.py:53
        |     fixed | variable | dynamic | tou | tou_impact | spot_monthly
        |
        +-- meter     (which register split is billed)        const.py:166
              mono | bi | dynamic | exclusive_night

orthogonal axes (independent of the above):

  DSO tariff mode   simple | bi_horaire | impact                const.py:177
  solar regime      none | compensation | injection             const.py:241
```

### Region and DSO sub-area

The three Belgian regions (`REGION_FLANDERS`, `REGION_WALLONIA`, `REGION_BRUSSELS`, `const.py:41`)
each have different regional levies and a different set of DSOs. `DSO_CHOICES` (`const.py:123`)
maps each region to its selectable sub-areas. Flanders is split into eight Fluvius sub-areas
because their distribution rates differ materially; Wallonia has five operators; Brussels has
only Sibelga. The canonical DSO keys (`const.py:49`) are stored verbatim in each user's
`CONF_DSO` and are also the keys of `SupplierSnapshot.dsos`, so they are stable forever: renaming
one would silently break every existing entry. Each extractor maps its card's own DSO labels
onto these canonical keys.

### Supplier and contract

A supplier is one registry entry, a `SupplierExtractor` (`providers/base.py:772`). It declares
the `Contract`s it sells (`providers/base.py:64`), each carrying a `TariffKind`
(`providers/base.py:53`):

| TariffKind | Energy model | Rates dataclass | Notes |
| --- | --- | --- | --- |
| `fixed` | Constant EUR/kWh, optionally bi-hourly | `FixedRates` (`providers/base.py:90`) | Optional `exclusive_night` rate for a dedicated night circuit. |
| `variable` | Current month's effective EUR/kWh (monthly-indexed) | `VariableRates` (`providers/base.py:112`) | May carry per-meter peak/offpeak; `formula` for diagnostics. |
| `dynamic` | `factor x spot + base` per slot | `DynamicRates` (`providers/base.py:149`) | `quarter_hourly` picks the 15-minute vs hourly billing grid. |
| `tou` | 3 hour-of-day bands (peak / transition / offpeak) | `TimeOfUseRates` (`providers/base.py:203`) | Weekday schedule shared; `weekend_rule` varies per product. Needs a smart meter. |
| `tou_impact` | Wallonia CWaPE 3-band (pic / medium / eco) | `ImpactRates` (`providers/base.py:241`) | CWaPE hour-of-day bands, every day; needs SMR3 and DSO Impact opt-in. |
| `spot_monthly` | Flat monthly rate `factor x monthly_mean(spot) + base` | `SpotMonthlyRates` (`providers/base.py:172`) | energie.be Variabel (Belpex_RLP) and the expert custom monthly-average mode; the coordinator averages the ENTSO-E spot cache per delivery month. Needs an ENTSO-E key. Distinct from `variable`, which reads a rate the card already resolved: this kind is for cards that name the index but publish only a forecast of it. |

A `Contract` also carries the `regions` it is actually published in (some products 404 outside
their home region) and `spot_indexed_injection` (`providers/base.py:86`), a flag for the one
non-dynamic case (Cociter Variable) where pricing the injection still needs an ENTSO-E spot.

One registry entry is not scraped: the expert **custom** supplier
(`providers/custom.py`, `SUPPLIER_CUSTOM`), an escape hatch for products with no public tariff
card. Its `fetch` is a stub; the coordinator builds the snapshot from the config entry the user
filled in (formula plus all regulated DSO + tax values) via `build_snapshot`.

### Meter

The meter type (`const.py:166`) selects which register split is billed: `mono` (single register),
`bi` (day/night bi-hourly), `dynamic` (per-slot), or `exclusive_night` (a dedicated night-circuit
meter for an electric water heater or night-storage heater, configured as a second config entry
pointing at that circuit's kWh sensor). The pricing engine routes `exclusive_night` through the
snapshot's dedicated exclusive-night rate and the DSO's `distribution_exclusive_night` column,
each falling back when the card does not publish a separate value.

### The two orthogonal axes

The DSO tariff mode (`CONF_DSO_TARIFF_MODE`, `const.py:252`) is a grid-side billing choice
independent of the supplier meter: `simple`, `bi_horaire`, or (Wallonia SMR3 opt-in) `impact`
(Tarif Impact, three distribution rates by CWaPE hour-of-day band). Outside Wallonia only
`simple` and `bi_horaire` are meaningful, and the coordinator falls back automatically when the
DSO does not publish Impact rates.

The solar regime (`CONF_SOLAR_REGIME`, `const.py:302`) is independent again: `none` (no panels),
`compensation` (the Walloon "meter runs backwards" regime, valid for pre-2024 installs until
2030-12-31), or `injection` (feed-in credited at the injection tariff). Belgian residential
injection is VAT-exempt, so `InjectionRates` values are never VAT-inclusive
(`providers/base.py:271`).

## End-to-end data flow

```
 config entry (region, dso, supplier, contract, meter, solar, api key)
        |
        v
 async_setup_entry            __init__.py:165
   |  _migrate_current_year_cost_unique_id(hass, entry)  # 0.5.2 key rename carry-over
   |  BePricesCoordinator(hass, entry)
   |  await coordinator.async_load_persistent()      # warm cache from .storage
   |  await coordinator.async_config_entry_first_refresh()
   |        |
   |        v
   |   _async_update_data                            coordinator.py:477
   |     |  probe() -> fresh?  yes: reuse cached snapshot
   |     |                     no : EXTRACTOR.fetch(session, contract, region)
   |     |        |
   |     |        v
   |     |   SupplierSnapshot (energy, dsos, taxes, injection, ...)  providers/base.py:478
   |     |     |
   |     |     |  dynamic / spot-indexed?  ->  EntsoeClient spot curve   api.py
   |     |     v
   |     |   for each slot: compute_breakdown(snapshot, dso_overlay,
   |     |                    taxes, meter, dso_mode, spot)  ->  PriceBreakdown   pricing.py
   |     |     v
   |     +-- CoordinatorData(hourly={slot: PriceBreakdown}, resolution, ...)  coordinator.py:712
   |
   entry.runtime_data = coordinator                  __init__.py:172
   async_forward_entry_setups(entry, PLATFORMS)      # sensor, binary_sensor, button
   async_track_time_change(...) -> push at slot boundaries   __init__.py:194
   async_create_background_task(backfill_if_missing) # one-shot recorder backfill
        |
        v
 sensor / binary_sensor / button read coordinator.data
```

Numbered walkthrough:

1. The user completes the config flow; HA stores the selections in `entry.data` and calls
   `async_setup_entry` (`__init__.py:166`).
2. The coordinator is constructed and immediately snapshots the `(supplier, contract, region)`
   tuple (`coordinator.py:846`) so a later options edit that mutates `entry.data` can still evict
   the previous tuple's cache.
3. `async_load_persistent` (`coordinator.py:376`) loads the last snapshot from `.storage` so an
   offline boot can still serve last-known prices.
4. `async_config_entry_first_refresh` runs `_async_update_data` (`coordinator.py:477`). It runs
   the supplier's cheap `probe()`; only when the probe key changed (or a probe-less supplier's
   24-hour TTL expired) does it call the extractor's `fetch`. Note the ordering gotcha:
   `entry.runtime_data` is assigned only after the first refresh completes (`__init__.py:172`),
   so the coordinator must not read `runtime_data` during first refresh.
5. `EXTRACTOR.fetch(session, contract, region)` returns a `SupplierSnapshot` (`providers/base.py:568`):
   the energy formula, a `DsoOverlay` per relevant DSO sub-area, the `TaxOverlay`, and optional
   `InjectionRates`.
6. For a dynamic contract (or a spot-indexed-injection one) the coordinator fetches the ENTSO-E
   day-ahead curve through `EntsoeClient` (`api.py`), at hourly or native 15-minute resolution.
7. For each slot the coordinator calls `compute_breakdown` (`pricing.py`), which fuses the chosen
   DSO overlay, the taxes, the meter type, the DSO tariff mode, and (for dynamic) the slot spot
   into a `PriceBreakdown`. See [pricing-model.md](pricing-model.md).
8. The result is packed into `CoordinatorData` (`coordinator.py:172`): the `hourly` table keyed by
   UTC slot start, the `resolution` (`RESOLUTION_QUARTER` only for quarter-hourly-billed dynamic
   suppliers, `coordinator.py:715`), plus snapshot metadata, the injection price, fees, and the
   running year-to-date cost.
9. `entry.runtime_data` is set to the coordinator, the three platforms are forwarded, and a
   slot-boundary push is registered (`__init__.py:194`). Because `current_price` and
   `next_hour_price` read the wall clock live, the push at each `:00` (and `:15/:30/:45` for a
   quarter-hourly supplier) re-evaluates the sensors without a re-fetch, keeping them aligned to
   the slot the user is actually billed for.
10. A one-shot backfill background task (`__init__.py:233`) populates the recorder only if it has
    no statistics at the Jan 1 anchor, so a normal restart adds no work.

## Freshness and caching, at a glance

The coordinator ticks hourly (`UPDATE_INTERVAL_MINUTES` = 60, `const.py:348`). Freshness has
three layers; the deep detail is in [coordinator.md](coordinator.md).

- Probe: each tick runs the supplier's cheap `probe()` (a HEAD or listing GET returning a
  freshness key like `Last-Modified`, `ETag`, or the resolved PDF URL). The full `fetch` runs
  only when the key changes, so a new publication is caught within an hour at near-zero
  bandwidth (`providers/base.py:541`).
- TTL fallback: suppliers with no usable probe (Engie, Luminus, DATS 24, where the only cheap
  response is the PDF itself) fall back to a 24-hour TTL (`SNAPSHOT_REFRESH_HOURS`,
  `coordinator.py:224`).
- On-disk cache: the latest snapshot is persisted to `.storage` (`STORAGE_VERSION`, `const.py:350`)
  so an offline boot serves last-known prices. A `STORAGE_VERSION` mismatch drops the blob rather
  than migrating it, since every field is re-derivable from a fresh fetch (`_MigratingStore`,
  `coordinator.py:815`).

Two further caching behaviors are worth knowing at the architecture level. First, snapshots are
shared process-wide across config entries keyed by `(supplier, contract, region)`
(`coordinator.py:299`), so two entries on the same product never poll the same card twice; the
shared rows are evicted on unload only when no sibling entry still references the tuple
(`__init__.py:286`, `evict_shared_caches`). Second, a failed fetch is negatively cached briefly
(`coordinator.py:242`) and the user-facing "extractor failed" repair issue is raised only after
the failure survives `_EXTRACTOR_ISSUE_THRESHOLD` consecutive attempts (`coordinator_snapshot.py:81`), so
a single transient CDN timeout does not false-alarm.

The ENTSO-E spot curve is fetched only for contracts that need it: dynamic contracts, and the
spot-indexed-injection case (Cociter Variable on the injection regime). Static, variable, and TOU
contracts never touch ENTSO-E for their consumption price.

## Adding a new supplier

A new supplier is a self-contained change; the contract is in
[provider-framework.md](provider-framework.md). In outline:

1. Add `providers/<supplier>.py` exposing a top-level `EXTRACTOR: SupplierExtractor`
   (`providers/base.py:531`, `SupplierProtocol` at `providers/base.py:809`). It declares the
   `contracts` it sells, a `fetch` that returns a `SupplierSnapshot`, and optionally a `probe`
   (for cheap freshness) and a `fetch_for_month` (for historical year-to-date billing). No EUR
   value goes in the module; everything comes from the live card.
2. Register it in `providers/__init__.py` by importing its `EXTRACTOR` and adding it to the
   `EXTRACTORS` dict (`providers/__init__.py:65`). The `Eneco` module is the reference
   implementation.
3. Ship a fixture-driven unit test against a real card sample (`tests/fixtures/*.pdf`), and add
   the supplier to the weekly `scripts/live_check.py` harness that fetches every real card and
   asserts the extractor still parses. See [ci-and-testing.md](ci-and-testing.md).

The extractor maps the card's own DSO labels onto the canonical DSO keys (`const.py:49`), sets a
per-contract `regions` set for products that are not sold everywhere, and, if the card ships
ex-VAT numbers, sets `TaxOverlay.vat_rate` explicitly (the default `0.0` means "already
VAT-inclusive", `providers/base.py:482`). An ex-VAT snapshot is left exactly as the card prints
it; `base.apply_vat` resolves it per config entry at the point the coordinator adopts it
(`coordinator.py:551`), because the snapshot caches above that point are shared between entries.

## Where to go next

- The refresh lifecycle, the full `CoordinatorData` shape, and every caching subtlety:
  [coordinator.md](coordinator.md).
- The tax, capacity, injection, and per-band math inside `compute_breakdown`:
  [pricing-model.md](pricing-model.md).
- The extractor protocol and dataclasses in depth: [provider-framework.md](provider-framework.md).
- The config and options wizard: [config-flow.md](config-flow.md).
- The ENTSO-E client and recorder backfill: [data-sources.md](data-sources.md).
- Sensors, services, and diagnostics: [entities.md](entities.md).
