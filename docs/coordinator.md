# Coordinator

This document covers `coordinator.py`, the `DataUpdateCoordinator` that drives the integration. It fetches the supplier tariff snapshot (with a cheap freshness probe, an on-disk cache, and a fallback TTL), fetches the ENTSO-E day-ahead spot curve for spot-indexed contracts, calls `pricing.compute_breakdown` to build the hour-by-hour (or quarter-hour) price table, computes the year-to-date bill from the HA recorder, and publishes a single `CoordinatorData` object that every entity reads. It also owns the Repairs issues, the shared cross-entry caches, and the persistence layer. Line references are into `coordinator.py` unless another file is named.

Related docs:

- [architecture.md](architecture.md) - module map and end-to-end data flow
- [pricing-model.md](pricing-model.md) - `compute_breakdown`, tax/injection/capacity math the coordinator calls
- [data-sources.md](data-sources.md) - the ENTSO-E spot client and recorder backfill
- [provider-framework.md](provider-framework.md) - the extractor protocol and snapshot dataclasses the coordinator consumes
- [entities.md](entities.md) - the sensors, binary sensor, and button that read `CoordinatorData`
- [glossary.md](glossary.md) - DSO, SMR3, TVAC, prosumer, and other Belgian-energy terms

## 1. Construction and lifecycle

### 1.1 Where the coordinator is built

The coordinator is instantiated once per config entry in `async_setup_entry` (`__init__.py:135`). The setup order is load-bearing:

```
coordinator = BePricesCoordinator(hass, entry)      # __init__.py:137
await coordinator.async_load_persistent()           # __init__.py:138  restore cache from disk
await coordinator.async_config_entry_first_refresh()# __init__.py:139  first tick, may raise ConfigEntryNotReady
entry.runtime_data = coordinator                    # __init__.py:141  ASSIGNED ONLY AFTER first refresh
entry.async_on_unload(entry.add_update_listener(_async_options_updated))
await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
```

`BePricesCoordinator.__init__` (`coordinator.py:545`) chains to `DataUpdateCoordinator.__init__` with `update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES)` (`coordinator.py:762`). `UPDATE_INTERVAL_MINUTES` is `60` (`const.py:258`): the coordinator ticks hourly for every contract kind, and the dynamic branch piggybacks the ENTSO-E refresh onto the same tick rather than running a second timer.

### 1.2 The runtime_data ordering trap

`entry.runtime_data` is assigned *after* `async_config_entry_first_refresh` returns (`__init__.py:141`). During that very first refresh `runtime_data` is HA's `UNDEFINED` sentinel, not this coordinator. Two guards depend on this:

- `_save_persistent` reads `runtime_data` defensively (`coordinator.py:1757`) and only skips the write when it has been explicitly assigned to a *different* `BePricesCoordinator`. It must not skip during first refresh (when the attribute is `UNDEFINED`), or the first snapshot would never persist.
- `async_unload_entry` (`__init__.py:191`) reads `runtime_data` with `getattr(..., None)` and an `isinstance` check, because a setup that raised before line 141 leaves the sentinel in place; a bare `is not None` test would pass and then `AttributeError` on `._supplier_tuple`, masking the real setup failure.

Never read `entry.runtime_data` as "this coordinator" during first refresh.

### 1.3 State captured at construction

`__init__` snapshots two things at construction time so later reload races resolve correctly:

- `self._supplier_tuple` (`coordinator.py:746`): the `(supplier, contract, region)` triple frozen at build time. `async_unload_entry` (`__init__.py:194`) and `_save_persistent` (`coordinator.py:1778`) target this *original* tuple even after an OptionsFlow edit has mutated `entry.data`, because HA mutates `entry.data` before firing the reload.
- `self._entry_data_signature` (`coordinator.py:550`): a `frozenset` of every `entry.data` item, built by `_compute_data_signature` (`coordinator.py:1224`). `_async_options_updated` (`__init__.py:249`) compares it against the current entry to skip a needless reload when only `entry.options` changed (an OptionsFlow no-op `options = {}` finalize). Every load-bearing field lives in `entry.data`, so an options-only delta is safe to ignore.

Other important instance fields set in `__init__`:

| Field | Purpose | Line |
|-------|---------|------|
| `_store` | `_MigratingStore` on-disk cache, keyed `be_electricity_prices_cache_<entry_id>` | 564 |
| `_snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key` | current in-memory snapshot and its provenance | 567-569 |
| `_force_refresh` | one-shot flag set by the refresh service to bypass freshness checks | 576 |
| `_spot_cache`, `_spot_cache_day`, `_spot_cache_includes_tomorrow` | today/tomorrow ENTSO-E curve cache | 577-579 |
| `_historical_spots` | UTC-hour -> EUR/kWh for past hours, replayed for YTD; persisted | 583 |
| `_short_spot_days` | past days whose last spot fetch came back short of 20 h | 587 |
| `_peak_kw`, `_peak_month` | Flanders monthly capacity peak (rolling max) | 588-589 |
| `_last_error` | last human-readable failure, surfaced in `last_error` and Repairs | 590 |

### 1.4 Restoring from disk

`async_load_persistent` (`coordinator.py:821`) runs before the first refresh and rehydrates `self._snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key`, the monthly peak, and `_historical_spots` from the Store. Two guards apply:

- **Tuple mismatch** (`coordinator.py:822`): if the persisted blob's stamped `(supplier, contract, region)` differs from the current entry, the snapshot and the historical spots are discarded (the peak is supplier-agnostic and kept). This handles a slow tick that saved a pre-OptionsFlow blob after the reload swapped the entry.
- **Corrupt blob** (`coordinator.py:832`): a `KeyError`/`ValueError`/`TypeError` while decoding drops the cached snapshot and logs a warning; the next refresh repopulates.

Loading an offline boot from disk lets the entry serve last-known prices before any network call succeeds.

## 2. The refresh path

The base class calls `_async_update_data` (`coordinator.py:884`) every tick. It wraps `_update_body` (`coordinator.py:921`) and, on `UpdateFailed`, refreshes the stale-snapshot Repairs placeholder with the current `_last_error` before re-raising (`coordinator.py:714`). The body runs these steps in order.

```
_update_body (coordinator.py:921)
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

`_maybe_refresh_snapshot` (`coordinator.py:1339`) decides whether to re-fetch the full tariff card. It never fetches unconditionally; a full PDF/HTML fetch happens only when a cheap check says the published card changed.

The cheap check is the extractor **probe** (`SnapshotProbe`, `providers/base.py:469`): a `HEAD` or small listing `GET` that returns a freshness key. Same key across calls means the snapshot is still valid; a changed key means re-fetch. The probe is optional; `None` means the supplier has no reliable probe path (Engie/Luminus API endpoints, DATS 24 single PDF) and the time-based TTL takes over.

Decision order in `_maybe_refresh_snapshot`:

1. Run `extractor.probe` if present (`coordinator.py:1284`); a failed or absent probe yields `probe_key = None`.
2. **Adopt a sibling** (`coordinator.py:1384`): if a shared-cache entry for this `(supplier, contract, region)` tuple is fresh against the probe/TTL, adopt it with `_adopt_shared` and return, doing zero network work.
3. **Reuse our own snapshot** (`coordinator.py:1304`): `_self_is_fresh` (`coordinator.py:1438`) returns True when, with a probe, `self._snapshot_probe_key == probe_key`, or without a probe, `now - fetched_at < ttl`. On a probe match it restamps `_snapshot_fetched_at = now` (so the age sensor reads "just checked") and clears any stale failure marker. Note: the restamp only happens on a positive probe (`probe_key is not None`); stamping on a TTL pass would reset the TTL clock and the supplier would never re-fetch.
4. **Negative-cache short-circuit** (`coordinator.py:1438`): if a sibling just failed on this key within `_SHARED_FAILURE_TTL` (5 minutes), skip the retry and adopt the sibling's error message. Bypassed when `_force_refresh` is set.
5. **Fetch under the shared lock** (`coordinator.py:1449`): re-check the sibling cache and negative cache under the lock, then call `extractor.fetch`. On success populate the shared cache, clear the failure marker, and clear the extractor Repairs issue.

`SNAPSHOT_REFRESH_HOURS` is `24` (`coordinator.py:177`): the TTL used only by probe-less suppliers. `SNAPSHOT_STALE_DAYS` is `7` (`coordinator.py:178`): once the snapshot is older than 7 days, `_sync_stale_issue` raises a Repairs warning.

### 2.2 Cross-entry sharing and dedup

Two config entries on the same `(supplier, contract, region)` share one fetched snapshot so the same card is never polled twice. The process-wide state lives in `hass.data[DOMAIN]`:

| Key | Shape | Meaning | Line |
|-----|-------|---------|------|
| `snapshot_cache` | `dict[tuple, _SharedSnapshot]` | latest shared snapshot per tuple | 177 |
| `snapshot_locks` | `dict[tuple, asyncio.Lock]` | dedup lock for first fetch per tuple | 178 |
| `snapshot_failed_fetches` | `dict[tuple, (ts, err, count)]` | negative cache of recent fetch failures | 187 |
| `monthly_snapshot_cache` | `dict[(sup,con,reg,YYYY-MM), Snapshot | None]` | archived per-month snapshots for YTD | 205 |
| `monthly_snapshot_failed_fetches` | `dict[key, ts]` | negative marker for transient `fetch_for_month` | 215 |
| `monthly_snapshot_locks` | `dict[key, asyncio.Lock]` | dedup lock per month key | 344 |
| `tuple_generations` | `dict[tuple, int]` | generation counter for eviction races | 352 |

`_SharedSnapshot` (`coordinator.py:234`) carries the snapshot, `fetched_at`, and the `probe_key` seen at fetch. `evict_shared_caches` (`coordinator.py:269`) is called from `async_unload_entry` (`__init__.py:216`) only when no other loaded entry still references the tuple; it bumps the generation counter first so an in-flight fetch that resumes after eviction detects the change and skips its write (`coordinator.py:1389`), preventing an orphaned cache row.

### 2.3 The on-disk Store and cache invalidation

The Store is `_MigratingStore` (`coordinator.py:709`), a `Store[dict]` subclass whose `_async_migrate_func` returns `{}` for any blob written under an older `STORAGE_VERSION` (`coordinator.py:726`). Every persisted field is re-derivable from a fresh fetch, so dropping the cache on a major-version mismatch is safe and avoids HA's "missing migration function" warning. `STORAGE_VERSION` is `2` (`const.py:260`).

There is a **second**, finer version inside the serialized snapshot: `_SNAPSHOT_SCHEMA_VERSION`, currently `15` (`coordinator.py:3492`). `_snapshot_to_dict` stamps it (`coordinator.py:3501`); `_snapshot_from_dict` raises `ValueError` when a loaded blob's `_schema_version` is below it (`coordinator.py:3517`), which `async_load_persistent` catches and treats as "discard and re-fetch".

This is the mechanism that lets a parser fix reach already-cached users. A probe-based supplier keeps serving its cached snapshot until the probe key changes (often the next monthly card, weeks away), so a code fix that adds or corrects a snapshot field does not heal existing users unless `_SNAPSHOT_SCHEMA_VERSION` is also bumped to invalidate their cache. The comment block above the constant records why each bump happened (v9/v10 for `DynamicRates.quarter_hourly`, v11 for `supplier_prosumer_eur_per_kva_year`, v12 for per-slot injection, v13 for the mis-parsed Eneco injection snapshot, v14 for the `SpotMonthlyRates` energy kind and the `InjectionRates.floor_at_zero` flag, v15 for the `VariableRates.formula_factor` / `formula_base` coefficients used to re-price a signing cohort). **When you change what an extractor parses into the snapshot, bump this constant.**

## 3. ENTSO-E spot integration

Spots are fetched only for two shapes, and the dispatch reads the *effective*
(cohort-spliced) energy `priced.energy`, not `self._snapshot.energy`:

1. **Dynamic or spot-monthly energy** (`isinstance(priced.energy, (DynamicRates, SpotMonthlyRates))`, `coordinator.py:1046`): dynamic prices each slot at `factor*spot + base`, spot-monthly bills a flat `factor*mean + base` off the month's mean, so both need a spot and share the hard-fail path. `_fetch_spot_prices` is called; `EntsoeAuthError` raises `UpdateFailed` and sets the `entsoe_auth_failed` Repairs issue (`coordinator.py:1054`), while a transient `EntsoeError` degrades to the last good `_spot_cache` and only fails the tick if nothing is cached (`coordinator.py:1064`).
2. **Spot-indexed injection on a static-energy contract** (`_injection_needs_spot`, `coordinator.py:2340`): here the energy is priced without a spot, so a spot failure must not tear the entry down. The fetch is soft: on any ENTSO-E error it falls back to the cached curve, then to no injection price (`coordinator.py:1078`). This is the Cociter Variable case (see section 8).

Note the asymmetry: branch 1 tests `priced.energy` while branch 2 tests the
un-spliced `self._snapshot.energy`, so a cohort leg reaches branch 1 and never
falls through to branch 2's soft path. Because only the dynamic and spot-monthly
*contract kinds* are asked for an API key, a variable cohort that re-prices to
`SpotMonthlyRates` would otherwise hard-fail an entry over a key the user was
never prompted for; `_cohort_energy_leg` therefore drops the cohort leg when no
key is configured (`coordinator.py:641`), keeping the current card instead.

### 3.1 Resolution selection (hourly vs quarter-hourly)

`_energy_is_quarter_hourly` (`coordinator.py:162`) returns True only for `DynamicRates` with `quarter_hourly=True`. Those extractors (Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower Dynamische Burgerstroom) bill on the native 15-minute Belpex/eSpot_15/Epex/EPEX DA grid; every other contract stays hourly. `_fetch_spot_prices` passes this as `quarter_hourly=` to `client.fetch_day_ahead` (`coordinator.py:1613`). The constants are `RESOLUTION_HOURLY = "PT60M"` and `RESOLUTION_QUARTER = "PT15M"` (`const.py:253`), matching ENTSO-E's resolution tokens. YTD billing stays hourly regardless (section 7).

### 3.2 The today/tomorrow spot cache

`_fetch_spot_prices` (`coordinator.py:1578`) windows the request on the *local* (Europe/Brussels) day, anchored on local midnight converted to UTC, so a 00:00-02:00 local query doesn't drop yesterday's UTC tail and the fall-back Sunday's 25th local hour is not lost (`coordinator.py:1402`). It requests tomorrow only when `now_local.hour >= 11` (`coordinator.py:1386`), because ENTSO-E publishes the day-ahead curve around 12-13 CET. The cache is keyed on `_spot_cache_day` and `_spot_cache_includes_tomorrow`; the latter records what the response *actually* carried, not what was asked for (`coordinator.py:1624`), so a pre-publication request for tomorrow doesn't lock the flag and block the next tick from retrying.

### 3.3 Historical spots for YTD

`_ensure_historical_spots` (`coordinator.py:1472`) fills `self._historical_spots` for every local hour in `[Jan 1, today]`, fetching missing week-sized chunks from ENTSO-E. It runs only for dynamic or spot-indexed-injection contracts (`coordinator.py:1033`) and needs the entry's API key (`coordinator.py:1499`), returning early if there is none. Details:

- A day counts as "present" when at least 20 of its 24 hours are cached (`coordinator.py:1525`), tolerating ENTSO-E source gaps and DST seams (23/25-hour days).
- Stable past days that stay short after a fetch get a `_short_spot_days` marker with the attempt time; `_SHORT_SPOT_DAY_TTL` is 12 hours (`coordinator.py:231`), so a genuinely-gappy past day is retried twice a day rather than every tick.
- Day boundaries anchor on local midnight in UTC, matching the recorder window and the persistence cut-off (`coordinator.py:1569`); a UTC anchor would leave the first hour or two of the local year unfetched.
- Fetch failures are logged and skipped; absent hours are treated as "no data" for the YTD, never a tick failure.

## 4. The published data dict

`CoordinatorData` (`coordinator.py:471`) is the contract with the entity platforms. Entities read fields either directly (via a description `value_fn`) or from the current-slot `PriceBreakdown`. Every field:

| Key | Type | Meaning | Read by |
|-----|------|---------|---------|
| `hourly` | `dict[datetime, PriceBreakdown]` | UTC-keyed price table (48-ish slots covering today+tomorrow); keys are hour or quarter-hour boundaries per `resolution` | current/next/today/tomorrow price sensors and window services; `tomorrow_prices_available` binary sensor (`sensor.py:109`, `binary_sensor.py:63`) |
| `resolution` | `str` | `RESOLUTION_HOURLY` or `RESOLUTION_QUARTER`; slot width of `hourly` keys | slot truncation in `sensor.py:99`; window sizing in `__init__.py:463` |
| `snapshot_publication` | `str` | supplier's publication label for the current card | `current_price` sensor attribute (`sensor.py:567`) |
| `snapshot_age_hours` | `float` | hours since `_snapshot_fetched_at` (`inf` if never) | `current_price` sensor attribute (`sensor.py:568`) |
| `snapshot_stale` | `bool` | True when age > 7 days | `current_price` sensor attribute (`sensor.py:569`) |
| `snapshot_valid_until` | `date \| None` | last calendar day the rates apply; `None` = unknown | `tomorrow_prices_available` binary sensor (`binary_sensor.py:66`) |
| `last_error` | `str` | last human-readable failure reason | `current_price` sensor attribute (`sensor.py:570`) |
| `monthly_peak_kw` | `float` | Flanders rolling monthly peak in kW (>= VREG floor) | `monthly_peak_kw` sensor (`sensor.py:462`) |
| `monthly_peak_month` | `date \| None` | month the peak belongs to | diagnostics (`diagnostics.py:169`) |
| `capacity_cost_eur` | `float` | monthly Flemish capacity cost estimate | `capacity_cost` sensor (`sensor.py:444`) |
| `prosumer_cost_eur` | `float` | monthly Walloon compensation-regime prosumer fee | `prosumer_cost` sensor (`sensor.py:377`) |
| `injection_price_eur_per_kwh` | `float \| None` | injection price for the slot the tick ran in; `None` off the injection regime or when a needed spot is missing | `injection_price` sensor fallback for contracts with no `injection_hourly` (`sensor.py:381`) |
| `injection_hourly` | `dict[datetime, float]` | per-slot injection price over the same today+tomorrow grid as `hourly`; empty unless on the injection regime with an intra-day-varying injection (spot-indexed or TOU) | `injection_price` sensor state at the current slot, plus its `today`/`tomorrow` arrays |
| `yearly_fixed_fee_eur` | `float` | supplier flat annual subscription for the configured meter | `fixed_fee_eur_per_year` sensor (`sensor.py:394`) |
| `energy_fund_eur_per_month` | `float` | Flemish Energiefonds monthly charge | `energy_fund_eur_per_month` sensor (`sensor.py:405`) |
| `current_year_cost_eur` | `float \| None` | running YTD bill since Jan 1; fees-only floor when no meters wired | `current_year_cost` sensor (`sensor.py:425`) |
| `ytd_diagnostics` | `dict[str, float] \| None` | optional breakdown behind the bill: YTD + today consumption/injection kWh, `energy_ytd_raw_eur` (pre-clamp energy term) and `fees_ytd_eur`; static per-day contracts only, `None` for hourly-billed contracts and when no meter is wired | `current_year_cost` sensor attributes (`sensor.py`) |

The current-slot sensors (`current_price`, `energy_component`, `network_component`, `taxes_component`, and the today/tomorrow min/avg/max) do not read a top-level field: they index `hourly` at the current slot and read a `PriceBreakdown` attribute (`all_in`, `energy`, `network`, `taxes`). `resolution` populated as `RESOLUTION_QUARTER` only when `_energy_is_quarter_hourly(self._snapshot.energy)` (`coordinator.py:1056`); everything else is hourly.

`yearly_fixed_fee_eur` and `energy_fund_eur_per_month` are parsed from the card but do NOT enter the per-kWh all-in number (`coordinator.py:685`); they are surfaced separately so users can compute a total monthly cost.

## 5. Slot selection and the live price table

`_build_hourly` (`coordinator.py:1688`) builds the UTC-keyed `hourly` table:

- **Dynamic** (`coordinator.py:1699`): one breakdown per spot returned by ENTSO-E; the table's resolution follows the spot grid (15-minute for quarter-hourly suppliers).
- **Static/TOU/Impact** (`coordinator.py:1706`): iterate UTC from local midnight to the start of the day after tomorrow, one slot per clock hour, so DST seams keep the wall-clock gap correct (47 slots spring-forward, 49 fall-back, 48 otherwise). The local-midnight anchor makes `today_min`/`today_max`/`today_average` cover the full local day rather than "now to midnight".

The entities, not the coordinator, do the current/next-slot lookup. `sensor.py:99` truncates `utcnow()` to the slot with `slot_start(..., data.resolution)`, reads the exact slot, and if it is missing accepts the nearest slot within one slot width (`max_gap` 3600 s hourly, 900 s quarter-hourly, `sensor.py:103`). `next_hour_price` targets `slot_start(now) + 1h` (`sensor.py:140`).

### 5.1 Slot-boundary push

The coordinator's 60-minute tick is not clock-aligned. `async_setup_entry` registers an `async_track_time_change` callback (`__init__.py:162`) that fires `coordinator.async_update_listeners()` at `:00` (and `:15/:30/:45` for a quarter-hourly supplier, `__init__.py:156`) so the live price sensors re-read the wall clock at the exact slot boundary without re-fetching.

The push only helps a sensor whose `value_fn` reads the clock: re-evaluating a value baked into `CoordinatorData` yields the same value. That is why every per-slot number a user sees has to come out of a per-slot table indexed at `utcnow()`, not out of a scalar the tick resolved. `injection_price` was the exception until issue #44 and now goes through `_current_injection`.

### 5.2 Cheapest / most-expensive window

The window computation is *not* owned by the coordinator. `_find_window` (`__init__.py:286`) is a pure helper behind the `cheapest_window` and `most_expensive_window` services (`__init__.py:493`, `__init__.py:501`). It reads `coordinator.data.hourly` and `.resolution`, scales the requested `duration_hours` to slots via `slots_per_hour(resolution)` (`__init__.py:463`), and only considers strictly time-contiguous runs (`__init__.py:338`) so a gap in a dynamic table can't stretch a window past its duration. `_today_ranked` in `sensor.py` computes the `cheapest_4h_today` / `most_expensive_4h_today` attributes on the `current_price` sensor.

## 6. Monthly capacity peak (Flanders)

`_track_monthly_peak` (`coordinator.py:1629`) maintains `_peak_kw`/`_peak_month` for the Flemish capaciteitstarief:

- Outside Flanders it resets both to 0/`None` (`coordinator.py:1720`) so a stale peak from a former Flanders config doesn't linger.
- It rolls over on the local 1st of the month (`coordinator.py:1640`); UTC would lag CET/CEST users at the boundary.
- `CAPACITY_MODE_FIXED` uses the configured value directly (`coordinator.py:1646`); `CAPACITY_MODE_SENSOR` takes a rolling max of the peak-power sensor (`coordinator.py:1653`), scaling W/VA to kW (`coordinator.py:1670`, issue #19: an unscaled 4481 W stored as 4481 kW inflated capacity cost 1000x).
- The regulated VREG floor `VREG_CAPACITY_FLOOR_KW = 2.5` (`const.py:242`) is applied regardless of mode (`coordinator.py:1686`): Fluvius bills `max(measured, floor)`.

`_compute_capacity` (`coordinator.py:1830`) then returns `peak_kw * capacity_eur_per_kw_year / 12` from the configured DSO overlay, or 0 when the overlay omits a capacity rate.

## 7. Year-to-date / current-year cost

`_compute_current_year_cost` (`coordinator.py:3152`) computes the running bill from Jan 1 of the local year to today. It bills each past day at the tariff of the month that day belongs to, using an archived snapshot when the supplier exposes `fetch_for_month` (`providers/base.py:547`) and the current snapshot as a proxy otherwise (`_snapshot_for_month`, `coordinator.py:404`). When a contract start date is set it routes every past month through `_effective_snapshot_for_month` (`coordinator.py:641`) instead, which splices the signing cohort's energy leg onto each delivery month's overlays, and dispatches on that cohort's effective energy kind so a re-priced variable contract takes the monthly-mean path. The whole year is recomputed from scratch each tick by design (`coordinator.py:3231`): prior days are not immutable (a late ENTSO-E fill or a backfill correction changes a past rate), and the full replay is cheap pure arithmetic.

Fees are always summed first and act as the floor: `_ytd_static_fees` (`coordinator.py:2504`, the supplier yearly fee, energy fund, DSO data-management fee, and Brussels OSP fee, pro-rated per archived month) plus `_ytd_prosumer` (`coordinator.py:2843`, the Walloon compensation fee). If no meters are wired the function returns fees only, never `unknown` (`coordinator.py:2699`).

Three energy paths, chosen by contract shape:

1. **Dynamic** (`coordinator.py:2533`): replay `_historical_spots` through `_ytd_hourly_energy` (`coordinator.py:2650`), billing each recorded hour at its actual `factor*spot+base`. Empty spot cache -> fees only.
2. **Per-hour billing needed** (`coordinator.py:2990`): TOU or Impact energy, or DSO Impact mode, or an `exclusive_night` meter. These also go through `_ytd_hourly_energy` (without spots), because their energy/distribution rates vary by hour-of-day or use the dedicated exclusive-night column the static per-day branch doesn't carry.
3. **Static per-day** (`coordinator.py:3018`): `_resolve_daily_kwh` (`coordinator.py:2266`) gives per-day `(day_cons, night_cons, day_inj, night_inj)`, each day billed against `static_breakdown` for its month.

### 7.1 Day/night register vs single-total reconstruction

`_resolve_daily_kwh` resolves the consumption and injection sides independently from one of three wirings (`const.py:197-193`):

- **Day + night register pair** (`CONF_DAY_*_KWH` + `CONF_NIGHT_*_KWH`): one recorder delta per day per register, fanned into band slots.
- **Single totals sensor** (`CONF_CONSUMPTION_KWH` / `CONF_INJECTION_KWH`): for mono meters the total goes to the day slot and the math sums it; for bi/dynamic meters `_recorder_daily_band_ratio` (`coordinator.py:1976`) recovers the day/night split from hourly recorder statistics binned on `is_offpeak`, defaulting to a time-weighted `_default_band_ratio_for` (`coordinator.py:2440`) for days with no accumulation so a flat Sunday isn't billed all-peak.
- **Partial pair** (one register half missing): returns `None`, so the caller falls back to the fees-only floor rather than silently undercounting a band (`coordinator.py:2620`).

The recorder is read via `_recorder_rows` (`coordinator.py:2606`), which requests the `change` field (delta of the cumulative `sum`, not the all-time total) with `units={"energy": "kWh"}` so a Wh/MWh sensor is normalised rather than billed 1000x wrong.

**Today is read live, not from statistics.** `_recorder_daily_kwh` (`coordinator.py:2745`) takes past days from the daily statistics but overrides the current day with `_live_today_kwh` (`coordinator.py:2674`): the meter's current cumulative state minus its reading at local midnight (from `get_significant_states`), converted to kWh. Long-term daily statistics only reflect the last *compiled* hour, so relying on them for today made `current_year_cost` step once an hour at best and freeze entirely if statistics compilation lagged or stalled while the meter state kept updating. The live read tracks today's usage in real time and survives a statistics stall; it falls back to the daily statistic when the meter is unavailable, non-numeric, carries an unconvertible unit, or has no reading at midnight yet, and only fires when the requested window ends on the actual current day (so the compare / diagnostics callers that pass historical ranges are unaffected).

### 7.2 Injection credit and regime math

Per-regime day math is documented at `coordinator.py:2836`. For `compensation` the injection nets 1:1 against consumption (per band when bi) and the YTD energy term is clamped at zero at the end (`coordinator.py:2871`): surplus injection past consumption is forfeited by most Walloon suppliers, and the clamp happens once over the whole YTD so a day of over-injection can offset a later high-consumption day. For `injection` each side uses its own rate and the total can dip negative; the running `current_year_cost` value dipping day-over-day is why the sensor is `TOTAL`, not `TOTAL_INCREASING` (`sensor.py:416`). The pre-clamp energy term is exported to the `energy_ytd_raw_eur` attribute (via the optional `breakdown` out-dict `_compute_current_year_cost` fills on the live tick), alongside the YTD/today kWh totals and the fees floor, so a sensor resting on the compensation zero-floor (negative raw energy, value `= fees_ytd_eur`) can be told apart from a stalled meter input (a today kWh that never grows). The historical injection rate is chosen by `_historical_injection_rate` (`coordinator.py:2381`), which mirrors the live priority (per-slot TOU, then `factor*spot+base`, then the monthly `current`) so the YTD credit and the live `injection_price` sensor never diverge.

### 7.3 Why YTD stays hourly for quarter-hourly contracts

`DynamicRates.quarter_hourly` keeps the *live* table on 15-minute slots, but the HA recorder only retains **hourly** long-term statistics (`providers/base.py:150`). So `_ytd_hourly_energy` aggregates consumption/injection to the clock hour and prices each hour at its hourly spot (`coordinator.py:2681`). When intra-hour load correlates with intra-hour price this is a close approximation, not a bit-exact reconciliation with the live 15-minute sensor. This is a deliberate constraint, not a bug.

## 8. Injection taxonomy and the spot-gating invariant

Belgian residential injection is VAT-exempt, so `InjectionRates` values are never VAT-scaled (`providers/base.py:216`). `InjectionRates` (`providers/base.py:219`) can carry a monthly indicative (`current`), an hourly formula (`factor`/`base`), a per-slot TOU triplet (`peak`/`transition`/`offpeak`), and an opt-in `floor_at_zero` flag. The coordinator distinguishes three shapes:

| Shape | Fields | Live price source | Example |
|-------|--------|-------------------|---------|
| (a) monthly-indicative only | `current` set, no usable `factor`/`base` for pricing | the printed `current` value, no spot | Eneco Fix/Flex, EBEM Variabel/B@sic+, DATS 24 |
| (b) hourly `factor*spot+base` | `factor`+`base`, energy is dynamic | `factor*spot+base` at the current slot | Engie, OCTA+, TotalEnergies, Luminus, Mega dynamic |
| (c) spot-indexed on static energy | `factor`+`base`, `current is None`, energy NOT dynamic | `factor*spot+base`, but the energy path fetches no spot | Cociter Variable |

`_compute_injection_price` implements the live selection: per-slot TOU rate first, then the spot formula when the energy is dynamic OR `current is None`, otherwise the static `current`. When a formula needs a spot but none is available it returns `None` rather than fabricating a value. The per-slot core is factored into `_injection_price_for_slot(inj, energy, spot, when)`, which the scalar calls with the now-slot spot (resolved by `_now_slot_spot`) and which `_build_injection_hourly` reuses to price every today+tomorrow slot for the sensor's `today`/`tomorrow` arrays. Only a contract flagged by `_injection_varies_intraday` (spot-indexed or TOU) gets an array; a flat contract would just repeat its scalar. Both paths share the same guard, so the array can never flip a flat monthly-indicative credit into a spot-varying one. Note the subtlety: a contract that has both a monthly `current` and a `factor`/`base` (shape (a) with a formula, e.g. Ecofix Flexy, EBEM SPP0) uses the realized monthly `current`, not the spot, keeping the live sensor consistent with the YTD credit. When a contract sets `floor_at_zero` (the expert custom monthly-average mode), `_floor_injection` clamps the resolved rate at 0 in both the live and historical paths. A `SpotMonthlyRates` energy contract's mean-indexed injection is baked into a flat `current` for the tick by `_bake_monthly_injection` so it prices off the delivery month's mean, not the live hourly spot.

When the custom monthly entry opts into **SPP-weighted** injection (`_spp_weighting_enabled`), the injection month-mean is the day-ahead prices weighted by the Synergrid solar production profile (`_spp_weighted_month_mean`) rather than the plain arithmetic mean, while energy keeps the plain mean. The profile is fetched by `synergrid.fetch_spp_weights` (`_ensure_spp_weights`, re-fetched monthly, cached in the Store) and used for both the live injection bake and the YTD credit (`_ytd_hourly_energy` threads the SPP month-mean into the injection line while energy stays on the flat mean). It uses the ex-ante (forecast) profile and falls back to the plain mean whenever the profile is unavailable.

### 8.1 The shape (c) invariant

`_injection_needs_spot` (`coordinator.py:1890`) is the gate for shape (c): injection regime, `inj.current is None`, `inj.factor`/`inj.base` set, and the energy is not `DynamicRates`. Because such a card never fetches ENTSO-E through the energy path, shape (c) needs an ENTSO-E spot fetched *specifically for the injection*, gated on `_injection_needs_spot` in **every** path or the credit silently drifts:

- Live spot fetch, softly (`coordinator.py:965`): a spot failure must only drop the injection, never the energy tick.
- Historical spot backfill (`coordinator.py:1033`): the `or _injection_needs_spot(...)` clause triggers `_ensure_historical_spots` for these contracts too.
- YTD credit (`_ytd_spot_injection_credit`, `coordinator.py:2767`): an isolated term that replays hourly spots for the injection side only, subtracted from the bill in both the hourly path (`coordinator.py:3013`) and the static per-day path (`coordinator.py:2863`). Its own guard (`coordinator.py:2565`) fires only for the exact shape (`factor`/`base` set, `current is None`, spots cached, an injection sensor wired).

The config-flow consequence: because shape (c) needs a key that the dynamic energy path would otherwise collect, `Contract.spot_indexed_injection` (`providers/base.py:77`) makes the config flow offer the API-key step on the injection regime for these static-energy contracts.

## 9. Error handling, backoff, and Repairs

The fail policy is "keep serving the cached snapshot, surface a Repairs issue". `_maybe_refresh_snapshot` catches every fetch exception (`coordinator.py:1400`), records `_last_error`, populates the shared negative cache with an incremented consecutive-failure count, and re-raises only non-`ExtractorError`/non-`TimeoutError` types (`coordinator.py:1435`); a bad card thus keeps the last good data alive.

Repairs issues, all keyed by `entry_id`:

| Issue | Raised by | When | Line |
|-------|-----------|------|------|
| `snapshot_stale` | `_sync_stale_issue` | age > `SNAPSHOT_STALE_DAYS` (7 d) | 849 |
| `extractor_failed` | `_sync_extractor_issue(transient=False)` | parse error / 404 / non-PDF; on the first failure | 870 |
| `extractor_unreachable` | `_sync_extractor_issue(transient=True)` | network timeout / reset / 5xx / anti-bot 403; only after `_EXTRACTOR_ISSUE_THRESHOLD` consecutive failures | 870 |
| `entsoe_auth_failed` | `_sync_entsoe_auth_issue` | ENTSO-E returns 401 for the API key | 915 |

`_EXTRACTOR_ISSUE_THRESHOLD` is `2` (`coordinator.py:214`): a lone transient CDN timeout does not raise the softer "unreachable" card, because a single failure almost always recovers on the next hourly tick and a false alarm wrongly tells the user the supplier changed its layout. `is_transient_fetch_error` (from `providers._pdf`) classifies the failure (`coordinator.py:1420`); actionable failures raise on the first occurrence, transient ones only after the threshold. The consecutive count rides the shared negative-cache row and resets to zero on the first success (`failed.pop`, `coordinator.py:1393`). The `extractor_failed`/`extractor_unreachable` slots are mutually exclusive; raising one clears the other (`coordinator.py:928`).

Negative-cache TTLs: `_SHARED_FAILURE_TTL` is 5 minutes (`coordinator.py:188`, dedupes a burst of update ticks across siblings), `_MONTHLY_FAILURE_TTL` is 30 minutes (`coordinator.py:223`, for `fetch_for_month`). A transient `fetch_for_month` failure is deliberately NOT written to `monthly_snapshot_cache` as a `None` (a cached `None` means "no archive for this month"); the separate failure marker (`coordinator.py:450`) prevents re-attempting every uncached month each tick while still letting a real recovery repopulate.

### 9.1 Forcing a refresh

`async_force_refresh` (`coordinator.py:1268`) backs the `be_electricity_prices.refresh` service (`__init__.py:273`). It sets the one-shot `_force_refresh` flag (honoured by `_self_is_fresh` and `_shared_is_fresh`), clears the spot cache, and pops the shared snapshot and negative-fetch rows so a sibling on the same tuple also re-fetches. It intentionally keeps `self._snapshot`/`_snapshot_fetched_at` so a transient failure during the forced refresh doesn't blank the entry. `reset_monthly_peak` (`coordinator.py:1296`), behind the diagnostic Reset-peak button, drops `_peak_kw` and persists immediately.

## 10. Persistence

`_save_persistent` (`coordinator.py:1745`) writes `entry_supplier`/`entry_contract`/`entry_region` (the frozen `_supplier_tuple`, not live `entry.data`), the peak, the serialized snapshot, and `historical_spots` pruned to the current YTD window. Two guards prevent a slow tick from clobbering a reloaded entry's state:

- **Identity guard** (`coordinator.py:1757`): skip when `runtime_data` is a *different* coordinator (must not skip during first refresh, when it is `UNDEFINED`).
- **Tuple guard** (`coordinator.py:1778`): skip when live `entry.data` has drifted from `_supplier_tuple` (the OptionsFlow window where `entry.data` changed but `runtime_data` is still swapping).

Serialization is `_snapshot_to_dict` / `_snapshot_from_dict` (`coordinator.py:3495`, `3302`), which stamp and check `_SNAPSHOT_SCHEMA_VERSION` as described in section 2.3. Historical spots are pruned with a local-midnight Jan 1 anchor (`coordinator.py:1819`) so a Brussels restart in early January doesn't drop the first hour or two of YTD. On entry removal, `async_remove_entry` (`__init__.py:224`) deletes the four Repairs issues and removes the Store file so nothing outlives the entry.
