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
- [data-sources.md](data-sources.md): the ENTSO-E spot client, recorder backfill, the Synergrid profiles and the CREG home charging rate.
- [entities.md](entities.md): sensors, the binary sensor, the button, services, diagnostics, i18n.
- [ci-and-testing.md](ci-and-testing.md): the live-check harness, test suite, and CI workflows.
- Per-supplier notes live under [providers/](providers/) (one file per extractor).

## What the integration does

The integration exposes the true all-in residential price paid for electricity in Belgium, as a
single EUR/kWh value per price slot, plus a solar injection (feed-in) credit. A Belgian bill is
not one number from one party: it fuses three independently sourced inputs.

1. The supplier energy formula, fetched live from that supplier's own published tariff card (a
   PDF, an HTML listing, or a small API), never hardcoded. See `const.py`
   ("No prices live here") and `providers/base.py` ("No SUPPLIER EUR values live in Python source").
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
| `domain` | `be_electricity_prices` | Config-entry namespace and service prefix; also the `DOMAIN` constant (`const.py`). |
| `name` | Belgian Electricity Prices | Display name. |
| `integration_type` | `service` | It provides derived data (prices), not a physical device. |
| `iot_class` | `cloud_polling` | It polls remote cards and ENTSO-E on a timer, no push. |
| `config_flow` | `true` | Set up entirely through the UI wizard (`config_flow.py`). |
| `requirements` | `pypdf>=4.0`, `pdfplumber>=0.11`, `defusedxml>=0.7`, `pyxlsb>=1.0` | PDF parsing (pypdf, pdfplumber) for tariff cards; defusedxml to parse the ENTSO-E XML safely. |
| `after_dependencies` | `energy`, `recorder` | The integration writes cost statistics into the recorder and plugs into the Energy dashboard, but must not hard-require them, so they load first when present. |
| `version` | `0.27.8` | Manifest version. CI auto-tags and publishes a release when this bumps on `main`. |

Home Assistant 2026.4 or newer is the declared minimum (README, `hacs.json`).

## The module map

Every Python module in `custom_components/be_electricity_prices/`. Paths in this table are
relative to that package directory.

| Module | Responsibility |
| --- | --- |
| `__init__.py` | Integration entry point. Registers domain services once at `async_setup`, sets up and tears down each config entry (`async_setup_entry` / `async_unload_entry` / `async_remove_entry`), owns the slot-boundary push and one-shot backfill scheduling, and implements the `refresh`, `cheapest_window`, `most_expensive_window`, and `backfill_statistics` service handlers. |
| `coordinator.py` | The `DataUpdateCoordinator` subclass. Owns `__init__`, the framework hook `_async_update_data` and the forced refresh; the six mixins below carry the rest of the class, and the leaf modules under them are plain functions the tick calls. |
| `coordinator_data.py` | `CoordinatorData`, the record every sensor reads, and the year and month window helpers that say which day a running total started from. A leaf, so a mixin that builds one can import it. |
| `coordinator_tick.py` | `_TickMixin`: one update tick start to finish, the per-slot price table it builds, and the background fills the first tick defers so setup fits Home Assistant's stage-2 budget. |
| `coordinator_persist.py` | `_PersistMixin`: what the entry keeps in its Store between restarts and how each row is re-checked against the schema version and the clock before it is trusted. |
| `coordinator_snapshot.py` | `_SnapshotMixin`: the snapshot fetch / freshness state machine. Probe, TTL, the shared cross-entry cache and its adoption, and the negative-fetch cache. |
| `coordinator_issues.py` | `_IssuesMixin`: the seven Repairs handlers and the shared `_sync_issue` helper they all raise and clear through. A pure reader of coordinator state. |
| `coordinator_spots.py` | `_SpotsMixin`: ENTSO-E fetching. The live day-ahead curve, the historical spot cache and its week-sized backfill. |
| `coordinator_profiles.py` | `_ProfilesMixin`: the Synergrid load and production profiles, shared across entries and persisted, and the weighted monthly means they buy. |
| `coordinator_peak.py` | `_PeakMixin`: the Flemish capacity peak (`_track_monthly_peak`) and its 12-month history. |
| `snapshot_store.py` | The shared cross-entry snapshot cache with its lock, negative-fetch cache and eviction, and the tuple generation counter both caches are gated on. |
| `snapshot_codec.py` | Snapshot serialization to and from `.storage`, `_SNAPSHOT_SCHEMA_VERSION` and the schema-version gate, and the Store that drops a blob written under an older storage version. |
| `snapshot_resolve.py` | `_resolve_snapshot`: the per-entry VAT, excise-band, settlement-grid and direct-debit resolution applied to a parsed card on load. |
| `snapshot_months.py` | One month's card: the archive reader, the per-month cache it fills and the blob those rows persist to. |
| `cohort.py` | Signing-cohort pricing: retrieves the archived signing-month card and splices its energy leg onto the delivery month's overlays. |
| `injection.py` | The injection taxonomy: which shape a card is, the per-slot rate shared by the live scalar and the YTD walk, and the historical rate. |
| `fees.py` | Standing charges: capacity tariff, Brussels OSP, prosumer forfait, and the annual static-fee sum the three cost paths share. |
| `ytd_cost.py` | The year-to-date cost walk itself: which months it covers, what each is billed on, and the total the sensor publishes. |
| `ytd_energy.py` | The two legs that have to replay the year hour by hour: the energy a spot-priced contract is billed at, and the feed-in credit settled the same way. |
| `ytd_legs.py` | The legs charged per day rather than per kWh: the standing charges, the Walloon prosumer fee and the Flemish capacity term, each pro-rated over the days the contract covered. |
| `projected_cost.py` | The full-calendar-year projection behind `projected_year_cost`: one pass at today's tariffs over the entry's own metered yearly volume, plus the basis strings that say what was measured and what was assumed. |
| `energy_meters.py` | Reads the configured kWh entities out of the recorder and the live state machine, and fans register pairs into band slots. |
| `spot_stats.py` | Spot aggregates: the current billing slot's spot, monthly means, the SPP-weighted variants, and the per-hour grouping of a quarter-hourly curve. |
| `pricing.py` | Pure pricing engine. `compute_breakdown` fuses a `SupplierSnapshot`, the chosen `DsoOverlay`, the taxes, meter type, DSO tariff mode, and (for dynamic) the slot spot into a `PriceBreakdown`. Also the slot-grid helpers (`slot_start`, `slot_delta`, `slots_per_hour`), `is_offpeak`, and `tou_slot`. No I/O, no HA imports where avoidable, so it is trivially unit-testable. |
| `config_flow.py` | The config wizard's step handlers (supplier and region, contract, DSO sub-area, meter, DSO billing mode, ENTSO-E key, capacity, connection power, solar, energy meters) and the options flow. |
| `flow_schemas.py` | The voluptuous schema builders and validators each step calls, including the ENTSO-E key check against the live endpoint. |
| `flow_contracts.py` | Which supplier and contract a household can pick, and what each one is: sold in this region, professional, spot-settled, index-tracking. Read-only over the registry. |
| `flow_schemas_custom.py` | The four forms behind the custom supplier, where a household types its own card in leg by leg. |
| `flow_prefill.py` | Suggests meter and capacity defaults from Home Assistant's Energy dashboard and the entity registry. Every failure mode degrades to suggesting nothing. |
| `compare_flow.py` | The options flow's one-off "compare another supplier" branch, as a mixin: the steps, their pickers and the step-to-step branching. |
| `compare_sweep_flow.py` | The compare-all branch: the progress step that sweeps the market a slice at a time, the parked ranking, and the daily job that enters the same sweep with nobody watching. |
| `compare_engine.py` | `_SweepEngine`: prices each candidate against the resolved household and ranks them, for the dialog and the schedule alike. |
| `compare_household.py` | `_HouseholdMixin`: resolves the one picture of the home every candidate is priced against, which is most of the work and none of the ranking. |
| `compare_inputs.py` | What the comparison reads off a household and what it calls things: volumes, regime, load-profile weights, the running welcome credit, and the registry's names. |
| `compare_placeholders.py` | `_PlaceholdersMixin`: everything the result page renders, from the side-by-side table to the charts and the caveats. |
| `compare_quote.py` | The annual-cost arithmetic that branch displays: the bill, the fees on it, the welcome credit and the volumes it is billed on. Kept out of `pricing.py`, which is a leaf the coordinator imports. |
| `compare_weighting.py` | Weighting a price curve by when the household uses and exports power, per hour, per register and per time-of-use slot. The bill and the comparison share these. |
| `compare_table.py` | The ranking as a record and as text: the rows, the table, the bar charts and the notes that qualify them. |
| `api.py` | The ENTSO-E day-ahead spot client (`EntsoeClient`). Fetches the Belgian day-ahead curve (hourly or native 15-minute) and parses the XML with defusedxml. Raises `EntsoeError` / `EntsoeAuthError`. |
| `synergrid.py` | The Synergrid profile fetchers. The solar production profile (SPP) for the SPP-weighted injection credit: streams the annual ex-ante workbook and parses only its small sheet, via `defusedxml` (already a requirement) so a nested-entity payload cannot be expanded. The residential load profile (RLP) for the RLP-indexed energy legs and the compensation allocation: a binary `.xlsb` read with `pyxlsb`, grouped into its distinct DSO curves in local time and reduced to every blend a card names from that one read, so the compare page can price each card on its own index. Both return hourly weights, or `{}` on any failure so the coordinator falls back to the plain mean or the metered slices. |
| `creg_ev.py` | The flat-rate ceiling for reimbursing a company car charged at home, computed from the CREG's monthly prices the way the SPF Finances does: one CSV, fetched once a quarter and cached for it, never raises. |
| `brugel.py` | Brugel's published Brussels distribution tariffs. One small PDF a year, stating "prix hors TVA", read for a single figure: Sibelga's `Puissance mise a disposition` annual term, the larger of the two parts of its fixed charge (50,07 of 64,80 EUR/year for a residential connection in 2026, the metering term being the rest) and the one Bolt's card is alone in not printing. Cached per year for the life of the process, with a short backoff on failure. Returns `None` on any failure, and `resolve_brussels_power_term` then leaves the card billing exactly what it billed before. Sibelga's own site answers 403, so the regulator is the source rather than the operator. One request and no fallback: Brugel's theme page is rendered client side and carries no PDF href at all, so searching it for the year's link could never match and cost 71 KB per cold start. Fetched in `_update_body` BEFORE the snapshot is resolved, beside the annual volume and for the same reason: `_resolve_snapshot` reads the cache synchronously and cannot await, so a fetch after it left the tick that retrieved the card billing without the term. The resolver asks for the DELIVERY month's year, so `_build_context` (`backfill_window.py`) fetches every year its window spans; the tick itself only ever needs this one. |
| `backfill.py` | Writes historical cost statistics into HA's recorder so the Energy dashboard shows price history immediately. `backfill_if_missing` runs once on install; `backfill_range` backs the `backfill_statistics` service. |
| `backfill_window.py` | What a run covers and what it writes into: the window clamped to what exists, the statistic ids, the recorder models and the one context the whole run is priced from. |
| `backfill_cost.py` | The cost half, which is the long one: a cost is a running sum, so a row written into the middle of a year has to continue the total before it and leave the total after it consistent. |
| `const.py` | All constants and config keys: `DOMAIN`, `PLATFORMS`, region and DSO keys, `CONF_*` option keys, meter types, DSO tariff modes, solar regimes, resolution tokens, TTLs, and the ENTSO-E endpoint. Intentionally holds zero prices. |
| `sensor.py` | The sensor platform: current price, next-hour price, year-to-date cost, injection price, fixed-fee and energy-fund sensors, and diagnostic sensors. |
| `binary_sensor.py` | The `tomorrow_prices_available` binary sensor (ON once ENTSO-E has published the next-day curve). |
| `button.py` | A refresh button entity that forces an immediate snapshot re-fetch for the entry. |
| `diagnostics.py` | The HA download-diagnostics payload for an entry (config, snapshot metadata, last error), redacting the ENTSO-E key. |
| `providers/base.py` | The extractor protocol and what a parsed card amounts to: `SupplierExtractor`, `SupplierSnapshot`, `DsoOverlay`, `TaxOverlay`, and the fetch / probe / archive callable types. |
| `providers/_rates.py` | The shapes a card can print: `Contract`, the six `EnergyRates` shapes and `InjectionRates`. Data only, so an extractor can build one without reaching into the pricing engine. |
| `providers/_resolve.py` | Turning a published card into the one a given household is billed on: VAT, the excise band, the direct-debit discount, the VREG ceiling, the Brussels power term, the volume tier and the settlement grid. |
| `providers/__init__.py` | The supplier registry: imports each module's `EXTRACTOR`, exposes the `EXTRACTORS` dict, and the `get()` / `all_extractors()` lookups. |
| `providers/_pdf.py` | Fetching a card and getting text out of it: the HTTP layer, transient-error classification via `is_transient_fetch_error`, plain and column-aligned extraction, and the per-tick memo. |
| `providers/_parse.py` | Reading a figure off a line of that text: the number formats Belgian cards print in, the sign words, the DSO table columns and the regional tax overlay. |
| `providers/_validity.py` | Which month a card is for and until when it is good: the validity sentences, the month-name headings and the archive's date check. |

In addition, seventeen scraped supplier modules live under `providers/`, each exposing a top-level
`EXTRACTOR`: `bolt.py`, `cociter.py`, `dats24.py`, `ebem.py`, `ecofix.py`, `ecopower.py`,
`eneco.py`, `energiebe.py`, `energyknights.py`, `energyvision.py`, `engie.py`, `frank.py`,
`luminus.py`, `mega.py`, `octaplus.py`, `totalenergies.py` and `trevion.py`. Each has its own page under
[providers/](providers/).

Seven of them carry their card readers in sibling modules, named
`_<supplier>_cards.py` for the product legs the supplier prices and
`_<supplier>_overlays.py` for the regulated ones it only reprints. EnergyVision
also has `_energyvision_wallonia.py`, because its Walloon card is a different
document in a different language rather than a variant of the Dutch one. The
supplier module keeps the urls, the archive and `parse_snapshot`, which calls
into them.

An eighteenth module, `custom.py`, is the expert escape hatch: it is not scraped (its `fetch` is a
stub) and the
coordinator builds its snapshot from the config entry. The framework they implement is
documented in [provider-framework.md](provider-framework.md).

## The core domain model

A config entry pins one point in a small product space. The primary hierarchy narrows from a
region down to a meter; two further axes (the DSO tariff mode and the solar regime) are
orthogonal to it and to each other.

```
region  (flanders | wallonia | brussels)                     const.py
  |
  +-- DSO sub-area   (which grid operator's overlay applies)  const.py
  |     flanders : 8 Fluvius sub-areas (materially different rates)
  |     wallonia : AIEG | AIESH | ORES | RESA | REW
  |     brussels : Sibelga (only one)
  |
  +-- supplier   (which extractor's EXTRACTOR is used)        providers/__init__.py
        |
        +-- contract  (a Contract with a TariffKind)          providers/base.py
        |     fixed | variable | dynamic | tou | tou_impact | spot_monthly
        |
        +-- meter     (which register split is billed)        const.py
              mono | bi | dynamic | exclusive_night

orthogonal axes (independent of the above):

  DSO tariff mode   simple | bi_horaire | impact                const.py
  solar regime      none | compensation | injection             const.py
```

### Region and DSO sub-area

The three Belgian regions (`REGION_FLANDERS`, `REGION_WALLONIA`, `REGION_BRUSSELS`, `const.py`)
each have different regional levies and a different set of DSOs. `DSO_CHOICES` (`const.py`)
maps each region to its selectable sub-areas. Flanders is split into eight Fluvius sub-areas
because their distribution rates differ materially; Wallonia has five operators; Brussels has
only Sibelga. The canonical DSO keys (`const.py`) are stored verbatim in each user's
`CONF_DSO` and are also the keys of `SupplierSnapshot.dsos`, so they are stable forever: renaming
one would silently break every existing entry. Each extractor maps its card's own DSO labels
onto these canonical keys.

### Supplier and contract

A supplier is one registry entry, a `SupplierExtractor` (`providers/base.py`). It declares
the `Contract`s it sells (`providers/_rates.py`), each carrying a `TariffKind`
(`providers/_rates.py`):

| TariffKind | Energy model | Rates dataclass | Notes |
| --- | --- | --- | --- |
| `fixed` | Constant EUR/kWh, optionally bi-hourly | `FixedRates` (`providers/_rates.py`) | Optional `exclusive_night` rate for a dedicated night circuit. |
| `variable` | Current month's effective EUR/kWh (monthly-indexed) | `VariableRates` (`providers/_rates.py`) | May carry per-meter peak/offpeak; `formula` for diagnostics. |
| `dynamic` | `factor x spot + base` per slot | `DynamicRates` (`providers/_rates.py`) | `quarter_hourly` picks the 15-minute vs hourly billing grid. |
| `tou` | 3 hour-of-day bands (peak / transition / offpeak) | `TimeOfUseRates` (`providers/_rates.py`) | Weekday schedule shared; `weekend_rule` varies per product. Needs a smart meter. |
| `tou_impact` | Wallonia CWaPE 3-band (pic / medium / eco) | `ImpactRates` (`providers/_rates.py`) | CWaPE hour-of-day bands, every day; needs SMR3 and DSO Impact opt-in. Cociter's card prints last month's BELIX per band and flags `month_indexed`, so `_month_indexed_leg` re-prices it through a banded `SpotMonthlyRates`. |
| `spot_monthly` | Flat monthly rate `factor x monthly_mean(spot) + base` | `SpotMonthlyRates` (`providers/_rates.py`) | energie.be Variabel, Energy Knights Essentia Online, Trevion Groene Stroom Flex / LifePowr (all Belpex_RLP), and the expert custom monthly-average mode; the coordinator averages the ENTSO-E spot cache per delivery month. Needs an ENTSO-E key. Distinct from `variable`, which reads a rate the card already resolved: this kind is for cards that name the index but publish only a forecast of it. Also the leg a month-indexed variable, TOU or Impact card re-prices through, carrying per-meter, per-slot or per-band coefficient pairs. |

A `Contract` also carries the `regions` it is actually published in (some products 404 outside
their home region) and `spot_indexed_injection` (`providers/_rates.py`), a flag for the
non-dynamic cards (the two Cociter variable ones, every Bolt fixed and variable card, and
every month-indexed card) where pricing the injection still needs an ENTSO-E spot.

One registry entry is not scraped: the expert **custom** supplier
(`providers/custom.py`, `SUPPLIER_CUSTOM`), an escape hatch for products with no public tariff
card. Its `fetch` is a stub; the coordinator builds the snapshot from the config entry the user
filled in (formula plus all regulated DSO + tax values) via `build_snapshot`.

### Meter

The meter type (`const.py`) selects which register split is billed: `mono` (single register),
`bi` (day/night bi-hourly), `dynamic` (per-slot), or `exclusive_night` (a dedicated night-circuit
meter for an electric water heater or night-storage heater, configured as a second config entry
pointing at that circuit's kWh sensor). The pricing engine routes `exclusive_night` through the
snapshot's dedicated exclusive-night rate and the DSO's `distribution_exclusive_night` column,
each falling back when the card does not publish a separate value.

### The two orthogonal axes

The DSO tariff mode (`CONF_DSO_TARIFF_MODE`, `const.py`) is a grid-side billing choice
independent of the supplier meter: `simple`, `bi_horaire`, or (Wallonia SMR3 opt-in) `impact`
(Tarif Impact, three distribution rates by CWaPE hour-of-day band). Outside Wallonia only
`simple` and `bi_horaire` are meaningful, and the coordinator falls back automatically when the
DSO does not publish Impact rates.

The solar regime (`CONF_SOLAR_REGIME`, `const.py`) is independent again: `none` (no panels),
`compensation` (the Walloon "meter runs backwards" regime, valid for pre-2024 installs until
2030-12-31), or `injection` (feed-in credited at the injection tariff). Belgian residential
injection is VAT-exempt, so `InjectionRates` values are never VAT-inclusive
(`providers/_rates.py`).

## End-to-end data flow

```
 config entry (region, dso, supplier, contract, meter, solar, api key)
        |
        v
 async_setup_entry            __init__.py
   |  _migrate_current_year_cost_unique_id(hass, entry)  # 0.5.2 key rename carry-over
   |  BePricesCoordinator(hass, entry)
   |  entry.runtime_data = coordinator               # before the first refresh, see coordinator.md 1.2
   |  await coordinator.async_load_persistent()      # warm cache from .storage
   |  await coordinator.async_config_entry_first_refresh()
   |        |
   |        v
   |   _async_update_data                            coordinator.py
   |     |  probe() -> fresh?  yes: reuse cached snapshot
   |     |                     no : EXTRACTOR.fetch(session, contract, region)
   |     |        |
   |     |        v
   |     |   SupplierSnapshot (energy, dsos, taxes, injection, ...)  providers/base.py
   |     |     |
   |     |     |  dynamic / spot-indexed?  ->  EntsoeClient spot curve   api.py
   |     |     v
   |     |   for each slot: compute_breakdown(snapshot, dso_overlay,
   |     |                    taxes, meter, dso_mode, spot)  ->  PriceBreakdown   pricing.py
   |     |     v
   |     +-- CoordinatorData(hourly={slot: PriceBreakdown}, resolution, ...)  coordinator.py
   |
   async_forward_entry_setups(entry, PLATFORMS)      # sensor, binary_sensor, button
   async_track_time_change(...) -> push at slot boundaries   __init__.py
   async_create_background_task(backfill_if_missing) # one-shot recorder backfill
        |
        v
 sensor / binary_sensor / button read coordinator.data
```

Numbered walkthrough:

1. The user completes the config flow; HA stores the selections in `entry.data` and calls
   `async_setup_entry` (`__init__.py`).
2. The coordinator is constructed and immediately snapshots the `(supplier, contract, region)`
   tuple (`coordinator.py`) so a later options edit that mutates `entry.data` can still evict
   the previous tuple's cache.
3. `async_load_persistent` (`coordinator_persist.py`) loads the last snapshot from `.storage` so an
   offline boot can still serve last-known prices.
4. `async_config_entry_first_refresh` runs `_async_update_data` (`coordinator.py`). It runs
   the supplier's cheap `probe()`; only when the probe key changed (or a probe-less supplier's
   24-hour TTL expired) does it call the extractor's `fetch`. `entry.runtime_data` is assigned
   before this refresh (`__init__.py`), so the yearly volume the month rows and the ceiling are
   resolved against is the measured one on the first tick too; readers still type-check it,
   since it is absent before setup, after a failed one and after an unload
   ([coordinator.md](coordinator.md), section 1.2).
5. `EXTRACTOR.fetch(session, contract, region)` returns a `SupplierSnapshot` (`providers/base.py`):
   the energy formula, a `DsoOverlay` per relevant DSO sub-area, the `TaxOverlay`, and optional
   `InjectionRates`.
6. For a dynamic contract (or a spot-indexed-injection one) the coordinator fetches the ENTSO-E
   day-ahead curve through `EntsoeClient` (`api.py`), at hourly or native 15-minute resolution.
7. For each slot the coordinator calls `compute_breakdown` (`pricing.py`), which fuses the chosen
   DSO overlay, the taxes, the meter type, the DSO tariff mode, and (for dynamic) the slot spot
   into a `PriceBreakdown`. See [pricing-model.md](pricing-model.md).
8. The result is packed into `CoordinatorData` (`coordinator_data.py`): the `hourly` table keyed by
   UTC slot start, the `resolution` (`RESOLUTION_QUARTER` only for quarter-hourly-billed dynamic
   suppliers, `coordinator_tick.py`), plus snapshot metadata, the injection price, fees, and the
   running year-to-date cost.
9. The three platforms are forwarded and a slot-boundary push is registered (`__init__.py`). Because `current_price` and
   `next_hour_price` read the wall clock live, the push at each `:00` (and `:15/:30/:45` for a
   quarter-hourly supplier) re-evaluates the sensors without a re-fetch, keeping them aligned to
   the slot the user is actually billed for.
10. A one-shot backfill background task (`__init__.py`) populates the recorder only if it has
    no statistics at the Jan 1 anchor, so a normal restart adds no work.

## Freshness and caching, at a glance

The coordinator ticks hourly (`UPDATE_INTERVAL_MINUTES` = 60, `const.py`). Freshness has
three layers; the deep detail is in [coordinator.md](coordinator.md).

- Probe: each tick runs the supplier's cheap `probe()` (a HEAD or listing GET returning a
  freshness key like `Last-Modified`, `ETag`, or the resolved PDF URL). The full `fetch` runs
  only when the key changes, so a new publication is caught within an hour at near-zero
  bandwidth (`providers/base.py`).
- TTL fallback: suppliers with no usable probe (DATS 24, energie.be, Engie, Luminus, where the
  only cheap response is the PDF itself) fall back to a 24-hour TTL (`SNAPSHOT_REFRESH_HOURS`,
  `snapshot_store.py`).
- On-disk cache: the latest snapshot is persisted to `.storage` (`STORAGE_VERSION`, `const.py`)
  so an offline boot serves last-known prices. A `STORAGE_VERSION` mismatch drops the blob rather
  than migrating it, since every field is re-derivable from a fresh fetch (`_MigratingStore`,
  `snapshot_codec.py`). That file is rewritten whole on every tick, so what goes in it has to be
  worth writing hourly: the two Synergrid profiles are national and change monthly, and live in
  one installation-wide store instead (`_profile_store`, `coordinator_profiles.py`).

Two further caching behaviors are worth knowing at the architecture level. First, snapshots are
shared process-wide across config entries keyed by `(supplier, contract, region)`
(`snapshot_store.py`), so two entries on the same product never poll the same card twice; the
shared rows are evicted on unload only when no sibling entry still references the tuple
(`__init__.py`, `evict_shared_caches`). Second, a failed fetch is negatively cached briefly
(`snapshot_store.py`) and the user-facing "extractor failed" repair issue is raised only after
the failure survives `_EXTRACTOR_ISSUE_THRESHOLD` consecutive attempts (`coordinator_snapshot.py`), so
a single transient CDN timeout does not false-alarm.

The ENTSO-E spot curve is fetched only for contracts that need it: dynamic contracts, and the
spot-indexed-injection case (the Cociter variable cards on the injection regime). Static, variable, and TOU
contracts never touch ENTSO-E for their consumption price.

## Adding a new supplier

A new supplier is a self-contained change; the contract is in
[provider-framework.md](provider-framework.md). In outline:

1. Add `providers/<supplier>.py` exposing a top-level `EXTRACTOR: SupplierExtractor`
   (`providers/base.py`). It declares the
   `contracts` it sells, a `fetch` that returns a `SupplierSnapshot`, and optionally a `probe`
   (for cheap freshness) and a `fetch_for_month` (for historical year-to-date billing). No EUR
   value goes in the module; everything comes from the live card.
2. Register it in `providers/__init__.py` by importing its `EXTRACTOR` and adding it to the
   `EXTRACTORS` dict (`providers/__init__.py`). The `Eneco` module is the reference
   implementation.
3. Ship a fixture-driven unit test against a real card sample (`tests/fixtures/*.pdf`), and add
   the supplier to the daily `scripts/live_check.py` harness that fetches every real card and
   asserts the extractor still parses. See [ci-and-testing.md](ci-and-testing.md). The daily
   card archiver (`scripts/archive_cards.py`) walks the registry and needs no change.

The extractor maps the card's own DSO labels onto the canonical DSO keys (`const.py`), sets a
per-contract `regions` set for products that are not sold everywhere, and, if the card ships
ex-VAT numbers, sets `TaxOverlay.vat_rate` explicitly (the default `0.0` means "already
VAT-inclusive", `providers/base.py`). An ex-VAT snapshot is left exactly as the card prints
it; `_resolve.apply_vat` resolves it per config entry at the point the coordinator adopts it
(`_resolve_snapshot`, `snapshot_resolve.py`, called from `coordinator_snapshot.py`), because the snapshot caches above that point are shared between entries.

## Where to go next

- The refresh lifecycle, the full `CoordinatorData` shape, and every caching subtlety:
  [coordinator.md](coordinator.md).
- The tax, capacity, injection, and per-band math inside `compute_breakdown`:
  [pricing-model.md](pricing-model.md).
- The extractor protocol and dataclasses in depth: [provider-framework.md](provider-framework.md).
- The config and options wizard: [config-flow.md](config-flow.md).
- The ENTSO-E client and recorder backfill: [data-sources.md](data-sources.md).
- Sensors, services, and diagnostics: [entities.md](entities.md).
