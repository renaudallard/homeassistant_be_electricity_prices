# Coordinator

This document covers `coordinator.py`, the `DataUpdateCoordinator` that drives the integration. It fetches the supplier tariff snapshot (with a cheap freshness probe, an on-disk cache, and a fallback TTL), fetches the ENTSO-E day-ahead spot curve for spot-indexed contracts, calls `pricing.compute_breakdown` to build the hour-by-hour (or quarter-hour) price table, computes the year-to-date bill from the HA recorder, and publishes a single `CoordinatorData` object that every entity reads. It also owns the Repairs issues, the shared cross-entry caches, and the persistence layer. Line references are into `coordinator.py` unless another file is named.

`BePricesCoordinator` is composed from four mixins, split out purely for file size along seams the class already had:

```
class BePricesCoordinator(
    _SnapshotMixin,     coordinator_snapshot.py   probe / TTL / shared cache
    _IssuesMixin,       coordinator_issues.py     the Repairs handlers
    _SpotsMixin,        coordinator_spots.py      ENTSO-E fetching
    _PeakMixin,         coordinator_peak.py       Flemish capacity peak
    DataUpdateCoordinator[CoordinatorData],
)
```

No mixin defines `__init__`, so `super().__init__` still resolves to `DataUpdateCoordinator`, and none of them inherits `DataUpdateCoordinator` itself: that needs `CoordinatorData`, which stays here because `sensor`, `binary_sensor` and `diagnostics` import it from this module and inheriting would close a cycle. Cross-mixin calls are satisfied by `TYPE_CHECKING` stubs, and entry-owned state is declared as bare annotations with no value, so `hasattr` and the instance dict behave exactly as they did on the single class. Below the mixins sit plain-function leaf modules the tick calls: `snapshot_store`, `cohort`, `injection`, `fees`, `ytd_cost`, `energy_meters` and `spot_stats`.

Related docs:

- [architecture.md](architecture.md) - module map and end-to-end data flow
- [pricing-model.md](pricing-model.md) - `compute_breakdown`, tax/injection/capacity math the coordinator calls
- [data-sources.md](data-sources.md) - the ENTSO-E spot client and recorder backfill
- [provider-framework.md](provider-framework.md) - the extractor protocol and snapshot dataclasses the coordinator consumes
- [entities.md](entities.md) - the sensors, binary sensor, and button that read `CoordinatorData`
- [glossary.md](glossary.md) - DSO, SMR3, TVAC, prosumer, and other Belgian-energy terms

## 1. Construction and lifecycle

### 1.1 Where the coordinator is built

The coordinator is instantiated once per config entry in `async_setup_entry` (`__init__.py:171`). The setup order is load-bearing:

```
coordinator = BePricesCoordinator(hass, entry)      # __init__.py:173
await coordinator.async_load_persistent()           # __init__.py:174  restore cache from disk
await coordinator.async_config_entry_first_refresh()# __init__.py:175  first tick, may raise ConfigEntryNotReady
entry.runtime_data = coordinator                    # __init__.py:177  ASSIGNED ONLY AFTER first refresh
entry.async_on_unload(entry.add_update_listener(_async_options_updated))
await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
```

`BePricesCoordinator.__init__` (`coordinator.py:875`) chains to `DataUpdateCoordinator.__init__` with `update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES)` (`coordinator.py:897`). `UPDATE_INTERVAL_MINUTES` is `60` (`const.py:348`): the coordinator ticks hourly for every contract kind, and the dynamic branch piggybacks the ENTSO-E refresh onto the same tick rather than running a second timer.

### 1.2 The runtime_data ordering trap

`entry.runtime_data` is assigned *after* `async_config_entry_first_refresh` returns (`__init__.py:177`). During that very first refresh `runtime_data` is HA's `UNDEFINED` sentinel, not this coordinator. Two guards depend on this:

- `_save_persistent` reads `runtime_data` defensively (`coordinator.py:1029`) and only skips the write when it has been explicitly assigned to a *different* `BePricesCoordinator`. It must not skip during first refresh (when the attribute is `UNDEFINED`), or the first snapshot would never persist.
- `async_unload_entry` (`__init__.py:247`) reads `runtime_data` with `getattr(..., None)` and an `isinstance` check, because a setup that raised before line 172 leaves the sentinel in place; a bare `is not None` test would pass and then `AttributeError` on `._supplier_tuple`, masking the real setup failure.

Never read `entry.runtime_data` as "this coordinator" during first refresh.

### 1.3 State captured at construction

`__init__` snapshots two things at construction time so later reload races resolve correctly:

- `self._supplier_tuple` (`coordinator.py:881`): the `(supplier, contract, region)` triple frozen at build time. `async_unload_entry` (`__init__.py:247`) and `_save_persistent` (`coordinator.py:1029`) target this *original* tuple even after an OptionsFlow edit has mutated `entry.data`, because HA mutates `entry.data` before firing the reload.
- `self._entry_data_signature` (`coordinator.py:890`): a `frozenset` of every `entry.data` item, built by `_compute_data_signature` (`coordinator.py:916`). `_async_options_updated` (`__init__.py:338`) compares it against the current entry to skip a needless reload when only `entry.options` changed (an OptionsFlow no-op `options = {}` finalize). Every load-bearing field lives in `entry.data`, so an options-only delta is safe to ignore.

Other important instance fields set in `__init__`:

| Field | Purpose | Line |
|-------|---------|------|
| `_store` | `_MigratingStore` on-disk cache, keyed `be_electricity_prices_cache_<entry_id>` | 319 |
| `_snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key` | current in-memory snapshot and its provenance | 326-329 |
| `_force_refresh` | one-shot flag set by the refresh service to bypass freshness checks | 336 |
| `_spot_cache`, `_spot_cache_day`, `_spot_cache_includes_tomorrow` | today/tomorrow ENTSO-E curve cache | 337-339 |
| `_historical_spots` | UTC-hour -> EUR/kWh for past hours, replayed for YTD; persisted | 343 |
| `_historical_spot_quarters` | the same hours -> their individual 15-minute slots, for a floored feed-in formula only; persisted | 349 |
| `_short_spot_days` | past days whose last spot fetch came back short of 20 h | 361 |
| `_peak_kw`, `_peak_month` | Flanders monthly capacity peak (rolling max) | 368-369 |
| `_last_error` | last human-readable failure, surfaced in `last_error` and Repairs | 374 |

### 1.4 Restoring from disk

`async_load_persistent` (`coordinator.py:383`) runs before the first refresh and rehydrates `self._snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key`, the monthly peak, `_historical_spots` and `_historical_spot_quarters` from the Store. An hour carrying any impossible quarter loses the whole list, not the offending slot, because a short list would silently re-weight the hour's mean; its hourly value stays if that passed its own check, so the hour prices energy as it always did and credits feed-in off the mean the slots refine. Neither `STORAGE_VERSION` nor `_SNAPSHOT_SCHEMA_VERSION` moved for the new key: a version mismatch discards the whole blob, the load path reads named keys and ignores unknown ones, and a missing key simply refills. Two guards apply:

- **Tuple mismatch** (`coordinator.py:982`): if the persisted blob's stamped `(supplier, contract, region)` differs from the current entry, the snapshot and the historical spots are discarded (the peak is supplier-agnostic and kept). This handles a slow tick that saved a pre-OptionsFlow blob after the reload swapped the entry.
- **Corrupt blob** (`coordinator.py:992`): a `KeyError`/`ValueError`/`TypeError` while decoding drops the cached snapshot and logs a warning; the next refresh repopulates.

Loading an offline boot from disk lets the entry serve last-known prices before any network call succeeds.

## 2. The refresh path

The base class calls `_async_update_data` (`coordinator.py:529`) every tick. It wraps `_update_body` (`coordinator.py:566`) and, on `UpdateFailed`, refreshes the stale-snapshot Repairs placeholder with the current `_last_error` before re-raising (`coordinator.py:566`). The body runs these steps in order.

```
_update_body (coordinator.py:566)
 ├─ _maybe_refresh_snapshot()            probe / TTL / fetch, may adopt sibling cache
 ├─ _track_monthly_peak()                Flanders capacity peak (rolling max)
 ├─ if self._snapshot is None: raise UpdateFailed("no supplier snapshot ...")
 ├─ clear entsoe_auth issue; clear extractor issue if _last_error empty
 ├─ if energy is DynamicRates:           fetch ENTSO-E spot (hard: auth fails the tick)
 │    elif _injection_needs_spot(...):   fetch ENTSO-E spot (soft: failure only drops injection)
 ├─ hourly = _build_hourly(spot_prices)  KeyError(DSO) -> UpdateFailed
 ├─ capacity_cost   = _compute_capacity(...)   (Flanders only)
 ├─ prosumer_cost   = _compute_prosumer(...)
 ├─ injection_price = _compute_injection_price(...)
 ├─ injection_hourly = _build_injection_hourly(...)   (varying injection only)
 ├─ if dynamic or _injection_needs_spot: _ensure_historical_spots(Jan1, today)
 ├─ current_year_cost = _compute_current_year_cost(...)
 ├─ _save_persistent()
 ├─ _sync_stale_issue(age > 7 days)
 └─ return CoordinatorData(...)
```

### 2.1 Snapshot freshness: probe vs stored key vs TTL

**A probe match restamps the age clock on every path.** `_maybe_refresh_snapshot`
tries the shared-cache shortcut before the self-fresh branch, and in steady state
the shared row is this coordinator's OWN row (its cold fetch wrote it), so the
shortcut is what actually runs each tick. Both paths now pass the probe key into
the adopt/restamp step: when freshness was decided by a PROBE rather than the TTL,
`fetched_at` moves to now on both the entry and the shared row. Only the self-fresh
branch used to do this, so an adopted snapshot kept the cold-fetch stamp for as long
as the supplier published the same card — monthly, in practice — and after seven days
every probe-based supplier raised a false `snapshot_stale` Repairs card with
`snapshot_age_hours` reading days while the card had been verified minutes earlier.
A TTL-based match must NOT restamp, or the TTL clock resets every tick and a
probe-less supplier is never re-fetched.

`_maybe_refresh_snapshot` (`coordinator_snapshot.py:194`) decides whether to re-fetch the full tariff card. It never fetches unconditionally; a full PDF/HTML fetch happens only when a cheap check says the published card changed.

The cheap check is the extractor **probe** (`SnapshotProbe`, `providers/base.py:799`): a `HEAD` or small listing `GET` that returns a freshness key. Same key across calls means the snapshot is still valid; a changed key means re-fetch. The probe is optional; `None` means the supplier has no reliable probe path (Engie/Luminus API endpoints, DATS 24 single PDF) and the time-based TTL takes over.

Decision order in `_maybe_refresh_snapshot`:

1. Run `extractor.probe` if present (`coordinator_snapshot.py:222`); a failed or absent probe yields `probe_key = None`.
2. **Adopt a sibling** (`coordinator_snapshot.py:236`): if a shared-cache entry for this `(supplier, contract, region)` tuple is fresh against the probe/TTL, adopt it with `_adopt_shared` and return, doing zero network work.
3. **Reuse our own snapshot** (`coordinator_snapshot.py:240`): `_self_is_fresh` (`coordinator_snapshot.py:392`) returns True when, with a probe, `self._snapshot_probe_key == probe_key`, or without a probe, `now - fetched_at < ttl`. On a probe match it restamps `_snapshot_fetched_at = now` (so the age sensor reads "just checked") and clears any stale failure marker. Note: the restamp only happens on a positive probe (`probe_key is not None`); stamping on a TTL pass would reset the TTL clock and the supplier would never re-fetch.
4. **Negative-cache short-circuit** (`coordinator_snapshot.py:297`): if a sibling just failed on this key within `_SHARED_FAILURE_TTL` (5 minutes), skip the retry and adopt the sibling's error message. Bypassed when `_force_refresh` is set.
5. **Fetch under the shared lock** (`coordinator_snapshot.py:303`): re-check the sibling cache and negative cache under the lock, then call `extractor.fetch`. On success populate the shared cache, clear the failure marker, and clear the extractor Repairs issue.

`SNAPSHOT_REFRESH_HOURS` is `24` (`snapshot_store.py:81`): the TTL used only by probe-less suppliers. `SNAPSHOT_STALE_DAYS` is `7` (`snapshot_store.py:82`): once the snapshot is older than 7 days, `_sync_stale_issue` raises a Repairs warning.

### 2.2 Cross-entry sharing and dedup

Two config entries on the same `(supplier, contract, region)` share one fetched snapshot so the same card is never polled twice. The process-wide state lives in `hass.data[DOMAIN]`:

| Key | Shape | Meaning | Line |
|-----|-------|---------|------|
| `snapshot_cache` | `dict[tuple, _SharedSnapshot]` | latest shared snapshot per tuple | 231 |
| `snapshot_locks` | `dict[tuple, asyncio.Lock]` | dedup lock for first fetch per tuple | 232 |
| `snapshot_failed_fetches` | `dict[tuple, (ts, err, count)]` | negative cache of recent fetch failures | 241 |
| `monthly_snapshot_cache` | `dict[(sup,con,reg,YYYY-MM), Snapshot | None]` | archived per-month snapshots for YTD | 259 |
| `monthly_snapshot_failed_fetches` | `dict[key, ts]` | negative marker for transient `fetch_for_month` | 269 |
| `monthly_snapshot_locks` | `dict[key, asyncio.Lock]` | dedup lock per month key | 405 |
| `tuple_generations` | `dict[tuple, int]` | generation counter for eviction races | 413 |

`_SharedSnapshot` (`snapshot_store.py:130`) carries the snapshot, `fetched_at`, and the `probe_key` seen at fetch. `evict_shared_caches` (`snapshot_store.py:164`) is called from `async_unload_entry` (`__init__.py:247`) only when no other loaded entry still references the tuple; it bumps the generation counter first so an in-flight fetch that resumes after eviction detects the change and skips its write (`coordinator_snapshot.py:335`), preventing an orphaned cache row.

### 2.3 The on-disk Store and cache invalidation

The Store is `_MigratingStore` (`snapshot_store.py:472`), a `Store[dict]` subclass whose `_async_migrate_func` returns `{}` for any blob written under an older `STORAGE_VERSION` (`snapshot_store.py:483`). Every persisted field is re-derivable from a fresh fetch, so dropping the cache on a major-version mismatch is safe and avoids HA's "missing migration function" warning. `STORAGE_VERSION` is `2` (`const.py:350`).

There is a **second**, finer version inside the serialized snapshot: `_SNAPSHOT_SCHEMA_VERSION`, currently `22` (`snapshot_store.py:579`). `_snapshot_to_dict` stamps it (`snapshot_store.py:582`); `_snapshot_from_dict` raises `ValueError` when a loaded blob's `_schema_version` is below it (`snapshot_store.py:617`), which `async_load_persistent` catches and treats as "discard and re-fetch".

This is the mechanism that lets a parser fix reach already-cached users. A probe-based supplier keeps serving its cached snapshot until the probe key changes (often the next monthly card, weeks away), so a code fix that adds or corrects a snapshot field does not heal existing users unless `_SNAPSHOT_SCHEMA_VERSION` is also bumped to invalidate their cache. The comment block above the constant records why each bump happened (v9/v10 for `DynamicRates.quarter_hourly`, v11 for `supplier_prosumer_eur_per_kva_year`, v12 for per-slot injection, v13 for the mis-parsed Eneco injection snapshot, v14 for the `SpotMonthlyRates` energy kind and the `InjectionRates.floor_at_zero` flag, v15 for the `VariableRates.formula_factor` / `formula_base` coefficients used to re-price a signing cohort, v16 for the persisted snapshot switching to the card as parsed rather than as priced, and for `TaxOverlay.federal_excise_bands`, v17 for `TaxOverlay.region_connection_fee_unavailable`, which also drops the July snapshot an EnergyVision Wallonia entry was stranded on by the August tax-block rewrite, and v18 for three extractor value fixes that shipped in 0.11.37 without it -- Ecopower's double-baked VAT, Mega's hardcoded energy fund, Bolt's residential fund row on professional cards -- which left those entries serving the pre-fix figures after upgrading, and v19 for Mega's realized-rate parser dropping a negative injection rate, which made a variable or Impact entry credit a rate the card charges, v20 for two extractors resolving a *superseded card URL* -- Bolt's pinned variable-card version and Ecopower's six-digit filename pattern -- where the cached snapshot is a perfectly well-parsed copy of the wrong card and neither supplier's probe key moves to dislodge it, and v21 for `InjectionRates.spp_indexed` plus energie.be Variabel parsing its injection formula rather than only the printed indicative, which an older cache cannot carry, and v22 for energie.be Vast parsing that same formula, having emitted only the card's printed indicative before). **When you change what an extractor parses into the snapshot, or which card it resolves, bump this constant.** A change that only affects how a stored card is *priced* (`apply_vat`, `resolve_excise_band`) does not need it, because those run on load; `test_a_cache_from_an_older_schema_is_discarded` pins the discard behaviour itself.

## 3. ENTSO-E spot integration

Spots are fetched only for two shapes, and the dispatch reads the *effective*
(cohort-spliced) energy `priced.energy`, not `self._snapshot.energy`:

1. **Dynamic or spot-monthly energy** (`isinstance(priced.energy, (DynamicRates, SpotMonthlyRates))`, `coordinator.py:573`): dynamic prices each slot at `factor*spot + base`, spot-monthly bills a flat `factor*mean + base` off the month's mean, so both need a spot and share the hard-fail path. `_fetch_spot_prices` is called; `EntsoeAuthError` raises `UpdateFailed` and sets the `entsoe_auth_failed` Repairs issue (`api.py:58`), while a transient `EntsoeError` degrades to the last good `_spot_cache` and only fails the tick if nothing is cached (`coordinator_spots.py:148`).
2. **Spot-indexed injection on a static-energy contract** (`_injection_needs_spot`, `injection.py:94`): here the energy is priced without a spot, so a spot failure must not tear the entry down. The fetch is soft: on any ENTSO-E error it falls back to the cached curve, then to no injection price (`injection.py:94`). This is the Cociter Variable case (see section 8).

`_ensure_historical_spots` runs immediately after these branches and BEFORE the
delivery-month mean is taken. That ordering is load-bearing: `_monthly_spot_mean`
averages `self._historical_spots`, and `_ensure_historical_spots` is the only
thing that fills it. Taking the mean first made a tick that started with an empty
cache average today's curve alone and call it the month, which on a cold start
came out roughly 46% off and is the flat rate the whole today+tomorrow table and
the baked injection credit then use until the next tick.
`test_spot_monthly_mean_waits_for_the_historical_spot_fill` pins the order.

A cached spot is only ever written once. `_ensure_historical_spots` fetches a day
holding fewer than 20 of its 24 hours, so a day that is COMPLETE but wrong is
never revisited, and nothing clears the cache before the year-end prune. That
made a single bad value permanent for the life of an entry, and on a dynamic
contract it skews every hour of the year-to-date bill it touches.
`async_load_persistent` therefore drops any persisted value outside
`_SPOT_SANE_MIN`..`_SPOT_SANE_MAX` (-1.0 to 5.0 EUR/kWh, against harmonised EU
clearing limits of -500 to +4000 EUR/MWh) and logs how many it discarded.
Dropping rather than clamping is what repairs the cache: the day falls under the
refetch threshold and the next tick replaces those hours from ENTSO-E. The band
is deliberately wide, since negative prices are ordinary in Belgium and scarcity
hours run into thousands of EUR/MWh; it catches a value on the wrong SCALE, not
one that merely looks expensive.

Note the asymmetry: branch 1 tests `priced.energy` while branch 2 tests the
un-spliced `self._snapshot.energy`, so a cohort leg reaches branch 1 and never
falls through to branch 2's soft path. Because only the dynamic and spot-monthly
*contract kinds* are asked for an API key, a variable cohort that re-prices to
`SpotMonthlyRates` would otherwise hard-fail an entry over a key the user was
never prompted for; `_cohort_energy_leg` therefore drops the cohort leg when no
key is configured (`coordinator.py:766`), keeping the current card instead.

**Cohort resolution order** (`_cohort_energy_leg`, `cohort.py:241`): the
hand-entered signing rate first, then the archived signing-month card, then the
current card. `_manual_energy_leg` (`cohort.py:106`) overlays what the user
typed onto whichever card was retrieved, **per field**, so a half-filled form
keeps the archived signing-month values for the boxes left blank rather than
today's. The archive is authoritative only about the *published* card; a
promotional, brokered or negotiated rate exists nowhere online, so the typed
value has to win. It used to lose, which made the signing-rate step a no-op on
exactly the seven suppliers that keep an archive (issue #54).

A typed yearly fee is entered as the card prints it, so on a card published
ex-VAT it has to be un-grossed for an entry that deducts VAT. The rate to
un-gross by is `TaxOverlay.published_vat_rate` (`providers/base.py:528`), read
as `published_vat_rate or vat_rate`. It has to ride on the snapshot rather than
be passed in, because `apply_vat` zeroes `vat_rate` on an ex-VAT resolve, and
every `_cohort_energy_leg` call site hands in an already-resolved snapshot: the
live tick (`coordinator.py:539`), the monthly walk (`cohort.py:362`), the
year-to-date walk (`ytd_cost.py:606`), the backfill accrual
(`backfill.py:313`), and the compare quote (`compare_flow.py:553`).
`_set_snapshot` (`coordinator_snapshot.py:146`) is the only writer of
`self._snapshot` and always routes through `_resolve_snapshot`, so by the time
the cohort leg reads the taxes there is no other surviving record of the basis
the card published at. Threading it as a parameter reached the live tick alone
and left the rest 21 EUR/yr adrift on the same entry. The `or` fallback covers
the two snapshots that never went through `apply_vat`: a card printed
VAT-inclusive (`vat_rate` is already `0.0`, so both halves agree) and a probe
cache written before the field existed, so no schema bump was needed.
`test_cohort_leg_bills_the_same_fee_on_every_call_path` pins the agreement.

### 3.1 Resolution selection (hourly vs quarter-hourly)

`_energy_is_quarter_hourly` (`spot_stats.py:68`) returns True only for `DynamicRates` with `quarter_hourly=True`. Those extractors (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom, Bolt Dynamisch, energie.be, EnergyVision) bill on the native 15-minute Belpex/eSpot_15/Epex/EPEX DA grid; every other contract stays hourly. `_fetch_spot_prices` passes this as `quarter_hourly=` to `client.fetch_day_ahead` (`coordinator_spots.py:356`). The constants are `RESOLUTION_HOURLY = "PT60M"` and `RESOLUTION_QUARTER = "PT15M"` (`const.py:320`), matching ENTSO-E's resolution tokens. YTD billing stays hourly regardless (section 7).

### 3.2 The today/tomorrow spot cache

`_fetch_spot_prices` (`coordinator_spots.py:356`) windows the request on the *local* (Europe/Brussels) day, anchored on local midnight converted to UTC, so a 00:00-02:00 local query doesn't drop yesterday's UTC tail and the fall-back Sunday's 25th local hour is not lost (`coordinator_spots.py:356`). It requests tomorrow only when `now_local.hour >= 11` (`coordinator_spots.py:356`), because ENTSO-E publishes the day-ahead curve around 12-13 CET. The cache is keyed on `_spot_cache_day` and `_spot_cache_includes_tomorrow`; the latter records what the response *actually* carried, not what was asked for (`coordinator_spots.py:150`), so a pre-publication request for tomorrow doesn't lock the flag and block the next tick from retrying.

### 3.3 Historical spots for YTD

`_ensure_historical_spots` (`coordinator_spots.py:192`) fills `self._historical_spots` for every local hour in `[Jan 1, today]`, fetching missing week-sized chunks from ENTSO-E. It runs only for dynamic or spot-indexed-injection contracts (`coordinator_spots.py:192`) and needs the entry's API key (`coordinator_spots.py:192`), returning early if there is none. Details:

- A day counts as "present" when at least 20 of its 24 hours are cached (`coordinator_spots.py:261`), tolerating ENTSO-E source gaps and DST seams (23/25-hour days).
- Stable past days that stay short after a fetch get a `_short_spot_days` marker with the attempt time; `_SHORT_SPOT_DAY_TTL` is 12 hours (`coordinator_spots.py:82`), so a genuinely-gappy past day is retried twice a day rather than every tick.
- Day boundaries anchor on local midnight in UTC, matching the recorder window and the persistence cut-off (`coordinator_spots.py:256`); a UTC anchor would leave the first hour or two of the local year unfetched.
- Fetch failures are logged and skipped; absent hours are treated as "no data" for the YTD, never a tick failure. A failure leaves the day as short as it was, so an `EntsoeAuthError` marks the chunk's stable past days too: without that, a revoked token re-pulled every week-chunk of the year on every hourly tick. That class covers a rejected key, an exhausted daily quota, and an acknowledgement carrying no matching data, which for a past chunk can simply mean the data does not exist; none of the three is fixed by asking again in an hour, and the cost is that a transient one holds its days for the TTL. A plain `EntsoeError` (timeout, 5xx) is left unmarked so the next tick retries promptly.
- The fetch asks for the same grid the contract settles on, via the same `_energy_is_quarter_hourly` test the live fetch uses (`coordinator_spots.py:222`). ENTSO-E publishes Belgium as two products, a PT60M and a PT15M series covering the same delivery period, and `parse_day_ahead_xml` deliberately refuses to blend them (`api.py:146`): requesting without the flag silently takes the hourly product, so a quarter-hourly contract's whole replay was priced off a different auction than its live bill.
- Whatever grid comes back is stored by clock hour, collapsed on the mean of the slots inside it (`_bucket_spots_by_hour`, `spot_stats.py:109`). The recorder only keeps hourly consumption, so an hour is the finest thing a replay can price, and the mean is exact for every formula that is LINEAR in the spot: pricing the hour's mean equals replaying each quarter against a quarter of that hour's kWh. Keeping the cache hourly is also what its 20-of-24 completeness test, its persisted form and every reader already assume.
- One formula is not linear. `floor_at_zero` makes the feed-in rate `max(0, factor*spot + base)` convex, so the mean of the floored quarters is at least the floored mean, and flooring once at the hour credits less than the live per-slot array whenever the spot crosses the floor inside the hour. Such an entry keeps that hour's own slots beside the mean, in `_historical_spot_quarters`, grouped by hour (`_group_spot_quarters_by_hour`, `spot_stats.py:80`) and gated on `_injection_needs_spot_quarters` (`injection.py:217`): injection regime, floored formula, quarter-hourly energy. Only the expert custom supplier sets `floor_at_zero`, so every other entry grows nothing. An hour holds one to four values (ENTSO-E answers a PT15M request with the PT60M series where no 15-minute one exists), and the replay averages whatever it holds.
- The same gate CLEARS the cache when the entry stops needing it. Unticking the quarter-hourly box or the never-negative one, or leaving the injection regime, changes none of the (supplier, contract, region) tuple the reload is gated on, so the cached year would otherwise be restored and re-persisted for the life of the entry while the replay went on crediting those hours per slot and the sensor beside it credited the hour.
- Coverage is measured against whichever cache the entry replays from, through one shared `_cached_spot_hours` (`coordinator_spots.py:173`) used by both the pre-fetch scan and the post-fetch recount. That is what refills an existing entry once after an upgrade: its hourly days are already complete, so nothing would ever be re-fetched otherwise. The two counts must stay one function, or a day reads short before a fetch and complete after it and is re-fetched every tick.

## 4. The published data dict

`CoordinatorData` (`coordinator.py:173`) is the contract with the entity platforms. Entities read fields either directly (via a description `value_fn`) or from the current-slot `PriceBreakdown`. Every field:

| Key | Type | Meaning | Read by |
|-----|------|---------|---------|
| `hourly` | `dict[datetime, PriceBreakdown]` | UTC-keyed price table (48-ish slots covering today+tomorrow); keys are hour or quarter-hour boundaries per `resolution` | current/next/today/tomorrow price sensors and window services; `tomorrow_prices_available` binary sensor (`sensor.py:262`, `binary_sensor.py:67`) |
| `resolution` | `str` | `RESOLUTION_HOURLY` or `RESOLUTION_QUARTER`; slot width of `hourly` keys | slot truncation in `sensor.py:262`; window sizing in `__init__.py:559` |
| `snapshot_publication` | `str` | supplier's publication label for the current card | `current_price` sensor attribute (`sensor.py:594`) |
| `snapshot_age_hours` | `float` | hours since `_snapshot_fetched_at` (`inf` if never) | `current_price` sensor attribute (`sensor.py:595`) |
| `snapshot_stale` | `bool` | True when age > 7 days | `current_price` sensor attribute (`sensor.py:596`) |
| `snapshot_valid_until` | `date \| None` | last calendar day the rates apply; `None` = unknown | `tomorrow_prices_available` binary sensor (`binary_sensor.py:65`) |
| `last_error` | `str` | last human-readable failure reason | `current_price` sensor attribute (`sensor.py:597`) |
| `monthly_peak_kw` | `float` | Flanders running monthly peak in kW, as measured (NOT floored) | `monthly_peak_kw` sensor (`sensor.py:487`) |
| `capacity_billed_peak_kw` / `capacity_peak_months` | `float` / `int` | the twelve-month mean the tariff is charged on, and how many months it covers. Both read `_peak_terms()`, which leaves the in-progress month out until it has a reading; deriving the count separately as `len(_peak_history) + 1` claimed a month the mean had not taken | `capacity_cost` sensor attributes |
| `monthly_peak_month` | `date \| None` | month the peak belongs to | diagnostics (`diagnostics.py:173`) |
| `capacity_cost_eur` | `float` | monthly Flemish capacity cost estimate | `capacity_cost` sensor (`sensor.py:469`) |
| `prosumer_cost_eur` | `float` | monthly Walloon compensation-regime prosumer fee | `prosumer_cost` sensor (`sensor.py:401`) |
| `injection_price_eur_per_kwh` | `float \| None` | injection price for the slot the tick ran in; `None` off the injection regime or when a needed spot is missing | `injection_price` sensor fallback for contracts with no `injection_hourly` (`sensor.py:409`) |
| `injection_hourly` | `dict[datetime, float]` | per-slot injection price over the same today+tomorrow grid as `hourly`; empty unless on the injection regime with an intra-day-varying injection (spot-indexed or TOU) | `injection_price` sensor state at the current slot, plus its `today`/`tomorrow` arrays |
| `yearly_fixed_fee_eur` | `float` | supplier flat annual subscription for the configured meter | `fixed_fee_eur_per_year` sensor (`sensor.py:418`) |
| `energy_fund_eur_per_month` | `float` | Flemish Energiefonds monthly charge | `energy_fund_eur_per_month` sensor (`sensor.py:429`) |
| `current_year_cost_eur` | `float \| None` | running YTD bill since Jan 1; fees-only floor when no meters wired | `current_year_cost` sensor (`sensor.py:449`) |
| `ytd_diagnostics` | `dict[str, float] \| None` | optional breakdown behind the bill. Static path: YTD + today consumption/injection kWh, `energy_ytd_raw_eur` (pre-clamp energy term). Hourly path (TOU / dynamic / spot-monthly): `hours_seen` + `hours_priced`, which say how much of the window the spot cache could price, plus YTD consumption/injection kWh. `fees_ytd_eur` on both, split into `capacity_ytd_eur` + `prosumer_ytd_eur` + `standing_charges_ytd_eur` with the `billed_peak_kw` they were billed on, since the capacity leg is per kW of monthly peak per year and is the leg most able to separate two entries reading one meter; `None` when no meter is wired | `current_year_cost` sensor attributes (`sensor.py`) |
| `projected_year_cost_eur` | `float \| None` | a full year priced at today's tariffs against the entry's own metered yearly volume, computed in one pass rather than as elapsed plus remainder. `None` for a dynamic or spot-monthly leg, whose future months have no knowable rate | `projected_year_cost` sensor |
| `projection_diagnostics` | `dict[str, Any] \| None` | the basis behind that number: `energy_basis`, `fee_basis`, `volume_basis`, `injection_basis` and `contract_basis` as strings, plus `annual_kwh` and `annual_injection_kwh` | attributes of the same sensor |

The current-slot sensors (`current_price`, `energy_component`, `network_component`, `taxes_component`, and the today/tomorrow min/avg/max) do not read a top-level field: they index `hourly` at the current slot and read a `PriceBreakdown` attribute (`all_in`, `energy`, `network`, `taxes`). `resolution` populated as `RESOLUTION_QUARTER` only when `_energy_is_quarter_hourly(self._snapshot.energy)` (`coordinator.py:182`); everything else is hourly.

`yearly_fixed_fee_eur` and `energy_fund_eur_per_month` are parsed from the card but do NOT enter the per-kWh all-in number (`coordinator.py:229`); they are surfaced separately so users can compute a total monthly cost.

## 5. Slot selection and the live price table

`_build_hourly` (`coordinator.py:929`) builds the UTC-keyed `hourly` table:

- **Dynamic** (`coordinator.py:826`): one breakdown per spot returned by ENTSO-E; the table's resolution follows the spot grid (15-minute for quarter-hourly suppliers).
- **Static/TOU/Impact** (`coordinator.py:887`): iterate UTC from local midnight to the start of the day after tomorrow, one slot per clock hour, so DST seams keep the wall-clock gap correct (47 slots spring-forward, 49 fall-back, 48 otherwise). The local-midnight anchor makes `today_min`/`today_max`/`today_average` cover the full local day rather than "now to midnight".

The entities, not the coordinator, do the current/next-slot lookup. `sensor.py:100` truncates `utcnow()` to the slot with `slot_start(..., data.resolution)`, reads the exact slot, and if it is missing accepts the nearest slot within one slot width (`max_gap` 3600 s hourly, 900 s quarter-hourly, `sensor.py:107`). `next_hour_price` targets `slot_start(now) + 1h` (`sensor.py:107`).

### 5.1 Slot-boundary push

The coordinator's 60-minute tick is not clock-aligned. `async_setup_entry` registers an `async_track_time_change` callback (`__init__.py:171`) that fires `coordinator.async_update_listeners()` at `:00` (and `:15/:30/:45` for a quarter-hourly supplier, `__init__.py:171`) so the live price sensors re-read the wall clock at the exact slot boundary without re-fetching.

The push only helps a sensor whose `value_fn` reads the clock: re-evaluating a value baked into `CoordinatorData` yields the same value. That is why every per-slot number a user sees has to come out of a per-slot table indexed at `utcnow()`, not out of a scalar the tick resolved. `injection_price` was the exception until issue #44 and now goes through `_current_injection`.

### 5.1.1 Local-day rollover

One boundary the push cannot cover is midnight, because there the *table* goes stale rather than the reading of it. `_build_hourly` anchors its today + tomorrow span at `dt_util.start_of_local_day()` as of the tick that built it, so once the date rolls over the table describes yesterday + today: `tomorrow_average` / `tomorrow_min` / `tomorrow_max` have no rows to reduce and read `unknown`, and `tomorrow_prices_available` drops off. Since the coordinator's tick is not clock-aligned, that lasted until whenever the next tick landed, up to an hour, every night.

A second `async_track_time_change` listener at `hour=0, minute=0` therefore calls `coordinator.async_request_refresh()`, which re-anchors the table on the new local day. `async_track_time_change` matches *local* time, so this follows Europe/Brussels across both DST seams; local midnight exists on each (unlike 02:00 on the spring-forward Sunday). The cost is one extra tick per day on top of the 24 hourly ones, and that tick goes through the same probe / TTL path, so it usually costs a freshness probe rather than a card fetch.

The listener's `second` is not 0 but `zlib.crc32(entry.entry_id.encode()) % 60`. Every install of a Belgian integration shares one timezone, so a fixed second would land the entire user base on a supplier's doorstep simultaneously once a night; the hourly tick is already spread because it is anchored on each install's setup time. `crc32` rather than `hash()` because the latter is salted per process and would move the entry to a different second on every restart. The worst-case staleness this leaves is 59 seconds, against the 59 minutes it replaces.

Widening the table to three local days would also have papered over the symptom, and was rejected: the window services search the whole table when `latest_end` is omitted, so a third day changes their answers. On a Luminus SmartFlex contract on 2026-03-19, the cheapest 4 h window moves from `2026-03-19T22:00` to `2026-03-21T11:00` (the first spring/summer day, whose 11:00-17:00 band drops to super-creuses) purely because the extra day is in range.

### 5.2 Cheapest / most-expensive window

The window computation is *not* owned by the coordinator. `_find_window` (`__init__.py:382`) is a pure helper behind the `cheapest_window` and `most_expensive_window` services (`__init__.py:382`, `__init__.py:382`). It reads `coordinator.data.hourly` and `.resolution`, scales the requested `duration_hours` to slots via `slots_per_hour(resolution)` (`__init__.py:539`), and only considers strictly time-contiguous runs (`__init__.py:539`) so a gap in a dynamic table can't stretch a window past its duration. `_today_ranked` in `sensor.py` computes the `cheapest_4h_today` / `most_expensive_4h_today` attributes on the `current_price` sensor.

## 6. Monthly capacity peak (Flanders)

`_track_monthly_peak` (`coordinator_peak.py:111`) maintains `_peak_kw`/`_peak_month` for the Flemish capaciteitstarief:

- Outside Flanders it resets both to 0/`None` (`coordinator_peak.py:112`) so a stale peak from a former Flanders config doesn't linger.
- It rolls over on the local 1st of the month (`coordinator_peak.py:127`); UTC would lag CET/CEST users at the boundary.
- `CAPACITY_MODE_FIXED` uses the configured value directly (`const.py:326`); `CAPACITY_MODE_SENSOR` takes a rolling max of the peak-power sensor (`const.py:325`), scaling W/VA to kW (`const.py:325`, issue #19: an unscaled 4481 W stored as 4481 kW inflated capacity cost 1000x).
- On rollover the closing month is banked into `_peak_history` and the window is pruned to the eleven most recent completed months, so with the running one the mean covers twelve. A month still at `0.0` is not banked: no reading was ever collected, which is not a measured zero.

`_billed_peak_kw` turns that window into the quantity Fluvius actually charges on, the "gemiddelde maandpiek". Its methodology gives the formula outright: `Rekenkundig gemiddelde van de Max (Maandpiek (m), 2.5) voor elke maand (m)`, i.e. the floor lands on each month BEFORE the mean, not on the mean. Every term is then at least the floor, so the mean is too and no outer clamp is needed. `CAPACITY_MODE_FIXED` bypasses the window and floors the configured value directly. `_peak_kw` itself is left raw, so `monthly_peak_kw` reports a measurement rather than a billing figure. The in-progress month only joins the mean once it HAS a reading: it is reset to 0 on the local 1st, and a zero floored to 2,5 kW is not a measured peak, so counting it stepped the mean (and with it `capacity_cost` and `current_year_cost`) down at every rollover and back up as the month accrued. This is the same estimate-the-gap rule already applied to a month that was never measured.

`_compute_capacity` (`fees.py:77`) then returns `billed_peak_kw * capacity_eur_per_kw_year / 12` from the configured DSO overlay, or 0 when the overlay omits a capacity rate. That feeds the `capacity_cost` sensor.

The same charge is accrued into the running bill by `_ytd_capacity`, which walks the year month by month like `_ytd_prosumer` and prorates each month by `days_in_ytd / days_in_full_month`, reading each month's archived overlay so a VREG indexation landing mid-year applies only to the months it covers. It applies the CURRENT gemiddelde maandpiek to every month rather than reconstructing one per month: the rolling window holds at most twelve months, and an entry installed mid-year has no history for the months before it, where Fluvius billed against meter history the integration never saw. Because the quantity is itself a twelve-month mean it moves slowly, so the current value is close to what each month of this year was billed on. All three cost paths use it: the live sensor, `backfill.py` (per local day, divided by that day's real UTC-hour count so the DST seam days still total a full daily share) and the OptionsFlow compare what-if. The one-month formula itself lives in `_capacity_monthly_eur`, which all three call: `peak x rate / 12` plus the two "nothing to bill" cases (no overlay for this DSO, no capacity row on the card). It was written out three times with three spellings of those guards, which is exactly the drift `_annual_static_fees` is shared to prevent -- capacity was the fee left out of it. The helper is deliberately region-agnostic: each caller keeps its own Flanders gate.

## 7. Year-to-date / current-year cost

`_compute_current_year_cost` (`ytd_cost.py:581`) computes the running bill from Jan 1 of the local year to today. It bills each past day at the tariff of the month that day belongs to, using an archived snapshot when the supplier exposes `fetch_for_month` (`providers/base.py:829`) and the current snapshot as a proxy otherwise (`_snapshot_for_month`, `snapshot_store.py:330`). When a contract start date is set it routes every past month through `_effective_snapshot_for_month` (`cohort.py:356`) instead, which splices the signing cohort's energy leg onto each delivery month's overlays, and dispatches on that cohort's effective energy kind so a re-priced variable contract takes the monthly-mean path. The whole year is recomputed from scratch each tick by design (`ytd_cost.py:581`): prior days are not immutable (a late ENTSO-E fill or a backfill correction changes a past rate), and the full replay is cheap pure arithmetic.

Fees are always summed first and act as the floor: `_ytd_static_fees` (`ytd_cost.py:171`, the supplier yearly fee, energy fund, DSO data-management fee, and Brussels OSP fee, pro-rated per archived month) plus `_ytd_prosumer` (`ytd_cost.py:206`, the Walloon compensation fee). If no meters are wired the function returns fees only, never `unknown` (`ytd_cost.py:206`).

Three energy paths, chosen by contract shape:

1. **Dynamic** (`ytd_cost.py:635`): replay `_historical_spots` through `_ytd_hourly_energy` (`ytd_cost.py:274`), billing each recorded hour at its actual `factor*spot+base`. An hour the cache cannot price is not dropped: `compute_network_and_taxes` bills its network and tax legs, which do not depend on the day-ahead price, and only the energy term is forfeited. An empty cache therefore lands on the fees floor plus the grid and tax cost of every metered kWh, not on fees alone. `hours_seen` / `hours_priced` in `ytd_diagnostics` report how much of the window got an energy price.

   On the spot-monthly variant the mean is taken per delivery month, so coverage is gated: `_covered_month_mean` (`spot_stats.py:197`) refuses a CLOSED month holding fewer than `_MIN_MONTH_HOURS` (24) cached hours, because that mean is applied to every hour of the month and a handful of hours yields a confident wrong rate rather than a noisier one. The running month keeps its mean: it is partial by definition. The threshold is an absolute count rather than a fraction because refusing forfeits the whole commodity leg (about 40% of the all-in rate), so the mean only has to beat a 100% error to be worth billing, and measured against real Belgian day-ahead prices it does so everywhere down to about a day's worth of hours. The same gate gates the SPP-weighted injection mean (`_month_is_thinly_cached`).
2. **Per-hour billing needed** (`ytd_cost.py:685`): TOU or Impact energy, or DSO Impact mode, or an `exclusive_night` meter. These also go through `_ytd_hourly_energy` (without spots), because their energy/distribution rates vary by hour-of-day or use the dedicated exclusive-night column the static per-day branch doesn't carry.
3. **Static per-day** (`ytd_cost.py:713`): `_resolve_daily_kwh` (`energy_meters.py:508`) gives per-day `(day_cons, night_cons, day_inj, night_inj)`, each day billed against `static_breakdown` for its month.

### 7.1 Day/night register vs single-total reconstruction

`_resolve_daily_kwh` resolves the consumption and injection sides independently from one of three wirings (`const.py:279-298`):

- **Day + night register pair** (`CONF_DAY_*_KWH` + `CONF_NIGHT_*_KWH`): one recorder delta per day per register, fanned into band slots.
- **Single totals sensor** (`CONF_CONSUMPTION_KWH` / `CONF_INJECTION_KWH`): for mono meters the total goes to the day slot and the math sums it; for bi/dynamic meters `_recorder_daily_band_ratio` (`energy_meters.py:460`) recovers the day/night split from hourly recorder statistics binned on `is_offpeak`, defaulting to a time-weighted `_default_band_ratio_for` (`energy_meters.py:611`) for days with no accumulation so a flat Sunday isn't billed all-peak.
- **Partial pair** (one register half missing): returns `None`, so the caller falls back to the fees-only floor rather than silently undercounting a band (`energy_meters.py:552`).

The recorder is read via `_recorder_rows` (`energy_meters.py:167`), which requests the `change` field (delta of the cumulative `sum`, not the all-time total) with `units={"energy": "kWh"}` so a Wh/MWh sensor is normalised rather than billed 1000x wrong.

**Today is read live, not from statistics.** `_recorder_daily_kwh` (`energy_meters.py:358`) takes past days from the daily statistics but overrides the current day with `_live_today_kwh` (`energy_meters.py:261`): the meter's current cumulative state minus its reading at local midnight (from `get_significant_states`), converted to kWh. Long-term daily statistics only reflect the last *compiled* hour, so relying on them for today made `current_year_cost` step once an hour at best and freeze entirely if statistics compilation lagged or stalled while the meter state kept updating. The live read tracks today's usage in real time and survives a statistics stall; it falls back to the daily statistic when the meter is unavailable, non-numeric, carries an unconvertible unit, or has no reading at midnight yet, and only fires when the requested window ends on the actual current day (so the compare / diagnostics callers that pass historical ranges are unaffected).

**The hourly branch gets the same guarantee.** Every hourly-billed contract (dynamic, spot-monthly, TOU, Impact, exclusive-night) takes `_ytd_hourly_energy`, which reads long-term HOURLY statistics and so reflects only the last compiled hour: it stepped once an hour at best and froze outright whenever compilation lagged or stalled, while the meter kept updating. `_top_up_today_hourly` closes that: it reads each configured meter live, subtracts what statistics already carry for today, and attributes the shortfall to the CURRENT hour. That is where the missing energy was (statistics trail real time, so what they have not booked yet is the most recent consumption) and it prices the top-up at the hour the user is living through, which is the point of a live read on a dynamic contract. Statistics that have caught up, a meter that ran backwards, or a meter with no reliable live reading all leave the statistics figure standing.

A reading below midnight's is a reset when the meter published a `last_reset` later than local midnight, and otherwise only when `state_class` is `total_increasing`. `last_reset` is the signal that generalises: HA's `utility_meter` reports `TOTAL` when `net_consumption` is set and `TOTAL_INCREASING` otherwise, and it cycles either way, so a `net_consumption` helper on a monthly cycle is a falling `TOTAL` meter that genuinely does reset. Gating on the class alone read its rollover as an ordinary fall and returned minus the whole previous cycle as today's kWh (312,4 kWh at midnight, 4,2 after the reset, reported as -308,2). A `total` register is allowed to fall, and the picker accepts any `device_class=energy` sensor, so a `utility_meter` with `net_consumption` or a bidirectional meter is a legitimate choice that goes backwards whenever the site exports more than it draws; it gets the signed delta, which is also exactly what the recorder reports as that day's `change`, keeping today and past days on one basis. Reading it as a reset instead billed the meter's **whole lifetime total** as a single day (a 12350 kWh register that had exported 4.5 kWh reported 12345.6 kWh, roughly 4300 EUR onto `current_year_cost`). A sensor publishing no state class gets the signed delta too.

### 7.2 Injection credit and regime math

Per-regime day math is documented at `ytd_cost.py:403`. For `compensation` the injection nets 1:1 against consumption (per band when bi) and the YTD energy term is clamped at zero at the end (`ytd_cost.py:436`): surplus injection past consumption is forfeited by most Walloon suppliers, and the clamp happens once over the whole YTD so a day of over-injection can offset a later high-consumption day. For `injection` each side uses its own rate and the total can dip negative; the running `current_year_cost` value dipping day-over-day is why the sensor is `TOTAL`, not `TOTAL_INCREASING` (`sensor.py:449`). The pre-clamp energy term is exported to the `energy_ytd_raw_eur` attribute (via the optional `breakdown` out-dict `_compute_current_year_cost` fills on the live tick), alongside the YTD/today kWh totals and the fees floor, so a sensor resting on the compensation zero-floor (negative raw energy, value `= fees_ytd_eur`) can be told apart from a stalled meter input (a today kWh that never grows). The historical injection rate is chosen by `_historical_injection_rate` (`coordinator.py:792`), which mirrors the live priority (per-slot TOU, then `factor*spot+base`, then the monthly `current`) so the YTD credit and the live `injection_price` sensor never diverge.

### 7.3 Why YTD stays hourly for quarter-hourly contracts

`DynamicRates.quarter_hourly` keeps the *live* table on 15-minute slots, but the HA recorder only retains **hourly** long-term statistics (`providers/base.py:152`). So `_ytd_hourly_energy` aggregates consumption/injection to the clock hour and prices each hour at its hourly spot (`ytd_cost.py:274`). When intra-hour load correlates with intra-hour price this is a close approximation, not a bit-exact reconciliation with the live 15-minute sensor. This is a deliberate constraint, not a bug.

## 8. Injection taxonomy and the spot-gating invariant

Belgian residential injection is VAT-exempt, so `InjectionRates` values are never VAT-scaled (`providers/base.py:307`). `InjectionRates` (`providers/base.py:307`) can carry a monthly indicative (`current`), an hourly formula (`factor`/`base`), a per-slot TOU triplet (`peak`/`transition`/`offpeak`), and an opt-in `floor_at_zero` flag. The coordinator distinguishes three shapes:

| Shape | Fields | Live price source | Example |
|-------|--------|-------------------|---------|
| (a) monthly-indicative only | `current` set, no usable `factor`/`base` for pricing | the printed `current` value, no spot | Eneco Fix/Flex, EBEM Variabel/B@sic+, DATS 24, EnergyVision fixed |
| (b) hourly `factor*spot+base` | `factor`+`base`, energy is dynamic | `factor*spot+base` at the current slot | Engie, OCTA+, TotalEnergies, Luminus, Mega dynamic |
| (c) spot-indexed on static energy | `factor`+`base`, `current is None`, energy NOT dynamic | `factor*spot+base`, but the energy path fetches no spot | Cociter Variable |

`_compute_injection_price` implements the live selection: per-slot TOU rate first, then the spot formula when the energy is dynamic OR `current is None`, otherwise the static `current`. When a formula needs a spot but none is available it returns `None` rather than fabricating a value. The per-slot core is factored into `_injection_price_for_slot(inj, energy, spot, when)`, which the scalar calls with the now-slot spot (resolved by `_now_slot_spot`) and which `_build_injection_hourly` reuses to price every today+tomorrow slot for the sensor's `today`/`tomorrow` arrays. Only a contract flagged by `_injection_varies_intraday` (spot-indexed or TOU) gets an array; a flat contract would just repeat its scalar. Both paths share the same guard, so the array can never flip a flat monthly-indicative credit into a spot-varying one. Note the subtlety: a contract that has both a monthly `current` and a `factor`/`base` (shape (a) with a formula, e.g. Ecofix Flexy, EBEM SPP0) uses the realized monthly `current`, not the spot, keeping the live sensor consistent with the YTD credit. When a contract sets `floor_at_zero` (the expert custom monthly-average mode), `_floor_injection` clamps the resolved rate at 0 in both the live and historical paths. A `SpotMonthlyRates` energy contract's mean-indexed injection is baked into a flat `current` for the tick by `_bake_monthly_injection` so it prices off the delivery month's mean, not the live hourly spot. The bake is skipped when `_injection_hourly_on_cohort` is true: a card that reached the monthly-mean path only through a signing-cohort re-price of its ENERGY leg keeps whatever index its injection carries. Cociter Tarif Variable is the case and its card is explicit about the split, note (7) "le prix ... est indexe mensuellement ... moyenne arithmetique ... (BELIX) durant le mois de fourniture" for consumption against note (9) "le prix de l'injection varie chaque heure". The cohort freezes the commodity coefficients the customer signed, not the feed-in formula. Baking it flattened the credit onto an index the contract never mentions, and since PV output peaks when the day-ahead price troughs, a flat mean systematically over-credits. A card that is ITSELF monthly-indexed (the custom monthly contract, the Mega groepsaankoop) indexes its injection on the month too and still bakes, which is why the snapshot's own energy kind decides rather than the effective one. A monthly-indexed card whose injection indexes on a DIFFERENT monthly parameter says so with `InjectionRates.spp_indexed`: energie.be Variabel indexes consumption on Belpex_RLP and injection on the solar-weighted Belpex_SPP, which sat at 6,34 against 11,42 c€/kWh in July 2026. The flag makes `_spp_weighting_enabled` fetch the Synergrid profile for the entry with no user opt-in, and makes the fallback STRICT: with no weighted mean available the formula is not resolved at all (`_spp_injection_spot(strict=True)` returns `None`, the live bake is skipped) and the card's printed `current` is credited instead. Resolving it against the energy leg's mean would pay 6,05 c€/kWh where the contract owes 3,00, and would do so silently; `_INJECTION_SHAPE`'s `spp` shape pins all three parts so a regression fails in the live check.

When the custom monthly entry opts into **SPP-weighted** injection (`_spp_weighting_enabled`), the injection month-mean is the day-ahead prices weighted by the Synergrid solar production profile (`_spp_weighted_month_mean`) rather than the plain arithmetic mean, while energy keeps the plain mean. The profile is fetched by `synergrid.fetch_spp_weights` (`_ensure_spp_weights`, re-fetched monthly, cached in the Store) and used for both the live injection bake and the YTD credit (`_ytd_hourly_energy` threads the SPP month-mean into the injection line while energy stays on the flat mean). It uses the ex-ante (forecast) profile and falls back to the plain mean whenever the profile is unavailable.

### 8.1 The shape (c) invariant

`_injection_needs_spot` (`injection.py:94`) is the gate for shape (c): injection regime, `inj.current is None`, `inj.factor`/`inj.base` set, and the energy is not `DynamicRates`. Because such a card never fetches ENTSO-E through the energy path, shape (c) needs an ENTSO-E spot fetched *specifically for the injection*, gated on `_injection_needs_spot` in **every** path or the credit silently drifts:

- Live spot fetch, softly (`coordinator.py:592`): a spot failure must only drop the injection, never the energy tick.
- Historical spot backfill (`coordinator.py:633`): the `or _injection_needs_spot(...)` clause triggers `_ensure_historical_spots` for these contracts too.
- YTD credit (`_ytd_spot_injection_credit`, `ytd_cost.py:499`): an isolated term that replays hourly spots for the injection side only, subtracted from the bill in both the hourly path (`ytd_cost.py:499`) and the static per-day path (`ytd_cost.py:499`). Its own guard (`ytd_cost.py:499`) fires only for the exact shape (`factor`/`base` set, `current is None`, spots cached, an injection sensor wired). Each hour is priced off ITS OWN delivery month's card, resolved through the same `_month_snapshot_cache` the sibling walks and the backfill use: it used to take today's coefficients and apply them to every hour since 1 January, so a contract whose feed-in formula moved during the year was re-credited for the whole year at its newest terms while the backfill replayed each month's own. An hour whose month printed an indicative is skipped, because the walk this term is added to already credited that month off it.

The config-flow consequence: because shape (c) needs a key that the dynamic energy path would otherwise collect, `Contract.spot_indexed_injection` (`providers/base.py:77`) makes the config flow offer the API-key step on the injection regime for these static-energy contracts.

## 9. Error handling, backoff, and Repairs

The fail policy is "keep serving the cached snapshot, surface a Repairs issue". `_maybe_refresh_snapshot` catches every fetch exception (`coordinator_snapshot.py:194`), records `_last_error`, populates the shared negative cache with an incremented consecutive-failure count, and re-raises only non-`ExtractorError`/non-`TimeoutError` types (`base.py:856`); a bad card thus keeps the last good data alive.

Repairs issues, all keyed by `entry_id`:

| Issue | Raised by | When | Line |
|-------|-----------|------|------|
| `snapshot_stale` | `_sync_stale_issue` | age > `SNAPSHOT_STALE_DAYS` (7 d) | 154 |
| `extractor_failed` | `_sync_extractor_issue(transient=False)` | parse error / 404 / non-PDF; on the first failure | 264 |
| `extractor_unreachable` | `_sync_extractor_issue(transient=True)` | network timeout / reset / 5xx / anti-bot 403; only after `_EXTRACTOR_ISSUE_THRESHOLD` consecutive failures | 264 |
| `extractor_unreadable` | `_sync_extractor_issue(unreadable=True)` | same, but the fetch raised `CardNotReadableError` (`providers/base.py:860`): the card downloaded fine and carries no text layer, so it names the custom-supplier workaround instead of asking for a GitHub issue | 279 |
| `entsoe_auth_failed` | `_sync_entsoe_auth_issue` | ENTSO-E returns 401 for the API key | 322 |
| `supplier_deprecated` | `_sync_deprecated_supplier_issue` | the entry's supplier carries `deprecated_until` in the registry (`providers/base.py`) AND the successor has a contract in the entry's region | 338 |
| `supplier_deprecated_no_successor` | `_sync_deprecated_supplier_issue` | same, but the successor is unset, unknown to this build, or has no contract in the entry's region | 338 |
| `supplier_deprecated_ended` | `_sync_deprecated_supplier_issue` | same as `supplier_deprecated`, but the local date is past `deprecated_until`: the transfer has happened and this entry has stopped updating | 338 |
| `supplier_deprecated_ended_no_successor` | `_sync_deprecated_supplier_issue` | same, past the date, with no usable successor | 338 |
| `connection_fee_missing` | `_sync_connection_fee_issue` | the snapshot carries `TaxOverlay.region_connection_fee_unavailable`, i.e. a Walloon card that stopped printing the connection-fee row | 233 |

The first four are failure states and clear on a successful refresh, as does
`connection_fee_missing` once the supplier prints the row again.
`supplier_deprecated` is not: it is a lifecycle notice, evaluated first on every
tick (`coordinator.py:566`) straight off the registry flag, and it clears only
when the entry is re-pointed at a supplier that has not
announced its exit. All four variants share one issue id, so an entry only ever
carries one of them; `_successor_for` decides whether a successor can be named
by checking it actually serves the entry's region, and `_supply_ended`
(`coordinator_issues.py:269`) picks the tense. That date comparison is the only
clock read in the deprecation path, and it is on the LOCAL date: the withdrawal
is a Belgian calendar event, so a UTC comparison flips a day late for CET/CEST
users. Past that date the extractor cards are suppressed too
(`coordinator_issues.py:322`); the supplier has stopped publishing, so a failing
fetch is the expected end state rather than a fault, and stacking a "could not
reach the supplier" card on top of this one would leave the user to work out
that the two describe a single event. `snapshot_stale` is deliberately NOT
suppressed: it states a true fact, that the prices being shown are old.
Prices are deliberately untouched while it is up -- a user
still being supplied must still be billed correctly for the months they are
supplied.

`_EXTRACTOR_ISSUE_THRESHOLD` is `2` (`coordinator_snapshot.py:81`): a lone transient CDN timeout does not raise the softer "unreachable" card, because a single failure almost always recovers on the next hourly tick and a false alarm wrongly tells the user the supplier changed its layout. `is_transient_fetch_error` (from `providers._pdf`) classifies the failure (`coordinator_snapshot.py:368`); actionable failures raise on the first occurrence, transient ones only after the threshold. The consecutive count rides the shared negative-cache row and resets to zero on the first success (`failed.pop`, `coordinator_snapshot.py:339`). The `extractor_failed`, `extractor_unreachable` and `extractor_unreadable` slots are mutually exclusive; raising any one clears the other two (`coordinator_issues.py:333`). A fetch that raises `CardNotReadableError` takes the third slot in place of the actionable one, because "the supplier changed its layout, open a GitHub issue" is advice nobody can act on when the card has no text layer at all. That signal is DERIVED from the download (`providers/_pdf.py:70`), not declared per supplier: the first version was a registry flag, which froze one month of observation into source and would have kept claiming a supplier was unreadable after it went back to publishing text, until someone shipped a release to clear it. Deriving it self-heals on the next fetch and covers any supplier that starts rasterizing. A transient network error still reports as transient.

Negative-cache TTLs: `_SHARED_FAILURE_TTL` is 5 minutes (`snapshot_store.py:99`, dedupes a burst of update ticks across siblings), `_MONTHLY_FAILURE_TTL` is 30 minutes (`snapshot_store.py:118`, for `fetch_for_month`). A transient `fetch_for_month` failure is deliberately NOT written to `monthly_snapshot_cache` as a `None` (a cached `None` means "no archive for this month"); the separate failure marker (`snapshot_store.py:339`) prevents re-attempting every uncached month each tick while still letting a real recovery repopulate.

### 9.1 Forcing a refresh

`async_force_refresh` (`coordinator.py:856`) backs the `be_electricity_prices.refresh` service (`__init__.py:362`). It sets the one-shot `_force_refresh` flag (honoured by `_self_is_fresh` and `_shared_is_fresh`), clears the spot cache, and pops the shared snapshot and negative-fetch rows so a sibling on the same tuple also re-fetches. It **also drops this tuple's per-month archive rows** via `_drop_monthly_rows` (`coordinator.py:856`): the YTD walk runs Jan 1 through today inclusive, so the current delivery month sits in that cache too, with no TTL. Without the drop, a supplier that re-issues the current month's card under the same month (Eneco publishes corrected volumes) went on being billed from the first card fetched for the life of the HA process, and this service — whose whole purpose is picking up a corrected card — could not clear it. It intentionally keeps `self._snapshot`/`_snapshot_fetched_at` so a transient failure during the forced refresh doesn't blank the entry. `reset_monthly_peak` (`coordinator.py:856`), behind the diagnostic Reset-peak button, drops `_peak_kw` and persists immediately.

## 10. Persistence

`_save_persistent` (`coordinator.py:1029`) writes `entry_supplier`/`entry_contract`/`entry_region` (the frozen `_supplier_tuple`, not live `entry.data`), the peak, the serialized snapshot, and `historical_spots` pruned to the current YTD window. Two guards prevent a slow tick from clobbering a reloaded entry's state:

- **Identity guard** (`coordinator.py:948`): skip when `runtime_data` is a *different* coordinator (must not skip during first refresh, when it is `UNDEFINED`).
- **Tuple guard** (`coordinator.py:968`): skip when live `entry.data` has drifted from `_supplier_tuple` (the OptionsFlow window where `entry.data` changed but `runtime_data` is still swapping).

Serialization is `_snapshot_to_dict` (`snapshot_store.py:582`) and `_snapshot_from_dict` (`snapshot_store.py:617`), which stamp and check `_SNAPSHOT_SCHEMA_VERSION` as described in section 2.3. What is persisted is the card **as parsed**, not as priced: `_set_snapshot` keeps both, and the per-entry VAT and excise-band resolution is re-applied on load. Historical spots are pruned with a local-midnight Jan 1 anchor (`coordinator_snapshot.py:146`) so a Brussels restart in early January doesn't drop the first hour or two of YTD. On entry removal, `async_remove_entry` (`__init__.py:300`) deletes every Repairs issue id and removes the Store file so nothing outlives the entry; `test_repair_issue_kinds_match_the_declared_strings` pins that list against `strings.json` so a newly added issue cannot skip it.
