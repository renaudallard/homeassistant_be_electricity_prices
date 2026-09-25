# Coordinator

This document covers the `DataUpdateCoordinator` that drives the integration, `coordinator.py` and the `coordinator_*.py` mixins it is composed from. It fetches the supplier tariff snapshot (with a cheap freshness probe, an on-disk cache, and a fallback TTL), fetches the ENTSO-E day-ahead spot curve for spot-indexed contracts, calls `pricing.compute_breakdown` to build the hour-by-hour (or quarter-hour) price table, computes the year-to-date bill from the HA recorder, and publishes a single `CoordinatorData` object that every entity reads. It also owns the Repairs issues, the shared cross-entry caches, and the persistence layer.

`BePricesCoordinator` is composed from six mixins, split out purely for file size along seams the class already had:

```
class BePricesCoordinator(
    _TickMixin,         coordinator_tick.py       one update tick, and the fills it defers
    _PersistMixin,      coordinator_persist.py    the Store, written and read back
    _SnapshotMixin,     coordinator_snapshot.py   probe / TTL / shared cache
    _IssuesMixin,       coordinator_issues.py     the Repairs handlers
    _SpotsMixin,        coordinator_spots.py      ENTSO-E fetching
    _PeakMixin,         coordinator_peak.py       Flemish capacity peak
    DataUpdateCoordinator[CoordinatorData],
)
```

`_SpotsMixin` in turn mixes in `_ProfilesMixin` (`coordinator_profiles.py`), which
holds the Synergrid load and production profiles and the weighted monthly means
they buy. It sits under the spots rather than beside them because the profiles
exist to weight a spot curve and nothing else reads them.

`CoordinatorData` lives in `coordinator_data.py`.

No mixin defines `__init__`, so `super().__init__` still resolves to `DataUpdateCoordinator`, and none of them inherits `DataUpdateCoordinator` itself: that would parametrise it with `CoordinatorData` and close a cycle back to this module. The record lives one module down precisely so the mixins, `sensor`, `binary_sensor` and `diagnostics` can all read it without depending on the class it is mixed into. Cross-mixin calls are satisfied by `TYPE_CHECKING` stubs, and entry-owned state is declared as bare annotations with no value, so `hasattr` and the instance dict behave exactly as they did on the single class. Below the mixins sit plain-function leaf modules the tick calls: `snapshot_store`, `snapshot_months`, `cohort`, `injection`, `fees`, `ytd_cost`, `energy_meters` and `spot_stats`.

Related docs:

- [architecture.md](architecture.md) - module map and end-to-end data flow
- [pricing-model.md](pricing-model.md) - `compute_breakdown`, tax/injection/capacity math the coordinator calls
- [data-sources.md](data-sources.md) - the ENTSO-E spot client and recorder backfill
- [provider-framework.md](provider-framework.md) - the extractor protocol and snapshot dataclasses the coordinator consumes
- [entities.md](entities.md) - the sensors, binary sensor, and button that read `CoordinatorData`
- [glossary.md](glossary.md) - DSO, SMR3, TVAC, prosumer, and other Belgian-energy terms

## 1. Construction and lifecycle

### 1.1 Where the coordinator is built

The coordinator is instantiated once per config entry in `async_setup_entry` (`__init__.py`). The setup order is load-bearing:

```
coordinator = BePricesCoordinator(hass, entry)      # __init__.py
entry.runtime_data = coordinator                    # __init__.py  BEFORE the first refresh, see 1.2
await coordinator.async_load_persistent()           # __init__.py  restore cache from disk
await coordinator.async_config_entry_first_refresh()# __init__.py  first tick, may raise ConfigEntryNotReady
entry.async_on_unload(entry.add_update_listener(_async_options_updated))
await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
```

`BePricesCoordinator.__init__` (`coordinator.py`) chains to `DataUpdateCoordinator.__init__` with `update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES)` (`coordinator.py`). `UPDATE_INTERVAL_MINUTES` is `60` (`const.py`): the coordinator ticks hourly for every contract kind, and the dynamic branch piggybacks the ENTSO-E refresh onto the same tick rather than running a second timer.

### 1.2 When runtime_data is there

`entry.runtime_data` is assigned *before* `async_config_entry_first_refresh` runs (`__init__.py`), not after it as the usual Home Assistant pattern has it. The tick resolves the archived month rows, the cohort card and the Flemish network ceiling against the yearly volume, and those read the measurement through `entry.runtime_data` (`entry_annual_kwh`, `snapshot_resolve.py`): assigned afterwards, the first tick of every restart could not see it and priced them on the typed figure or the household default, so a banded card billed the past months of the year-to-date on the wrong tier for an hour and `current_year_cost` stepped down at the second tick. The coordinator's own card was already resolved by handing the coordinator over explicitly (`_set_snapshot`); the early assignment covers every other reader at once.

The attribute is still absent, HA's `UNDEFINED` sentinel, in three windows: before that line, after a first refresh that raised `ConfigEntryNotReady` on anything but an unreadable card (setup deletes it again, the way HA drops it on unload), and after an unload or mid-reload. Every reader therefore keeps its guard:

- `_save_persistent` reads `runtime_data` defensively (`coordinator_persist.py`) and only skips the write when it has been explicitly assigned to a *different* `BePricesCoordinator`.
- `async_unload_entry` (`__init__.py`) reads `runtime_data` with `getattr(..., None)` and an `isinstance` check, because a setup that raised leaves the sentinel in place; a bare `is not None` test would pass and then `AttributeError` on `._supplier_tuple`, masking the real setup failure.

Never read `entry.runtime_data` as "this coordinator" without the type check.

### 1.3 State captured at construction

`__init__` snapshots two things at construction time so later reload races resolve correctly:

- `self._supplier_tuple` (`coordinator.py`): the `(supplier, contract, region)` triple frozen at build time. `async_unload_entry` (`__init__.py`) and `_save_persistent` (`coordinator_persist.py`) target this *original* tuple even after an OptionsFlow edit has mutated `entry.data`, because HA mutates `entry.data` before firing the reload.
- `self._entry_data_signature` (`coordinator.py`): a `frozenset` of every `entry.data` item, built by `_compute_data_signature` (`coordinator.py`). `_async_options_updated` (`__init__.py`) compares it against the current entry to skip a needless reload when only `entry.options` changed (an OptionsFlow no-op `options = {}` finalize). Every load-bearing field lives in `entry.data`, so an options-only delta is safe to ignore.

Other important instance fields set in `__init__`:

| Field | Purpose |
|-------|---------|
| `_store` | `_MigratingStore` on-disk cache, keyed `be_electricity_prices_cache_<entry_id>` |
| `_snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key` | current in-memory snapshot and its provenance |
| `_force_refresh` | one-shot flag set by the refresh service to bypass freshness checks |
| `_spot_cache`, `_spot_cache_day`, `_spot_cache_includes_tomorrow` | today/tomorrow ENTSO-E curve cache; the curve itself is persisted as an outage fallback, the two markers are not |
| `_historical_spots` | UTC-hour -> EUR/kWh for past hours, replayed for YTD; persisted |
| `_historical_spot_quarters` | the same hours -> their individual 15-minute slots, for a floored feed-in formula only; persisted |
| `daily_compare` | the last scheduled supplier ranking; persisted, so the potential-saving sensor keeps its figure across a restart instead of reading unknown until the next nightly sweep |
| `_spot_day_retry_at` | past days the spot walk may not ask for again yet, each holding the instant it may be retried at |
| `_peak_kw`, `_peak_month` | Flanders monthly capacity peak (rolling max) |
| `_last_error` | last human-readable failure, surfaced in `last_error` and Repairs |

### 1.4 Restoring from disk

`async_load_persistent` (`coordinator_persist.py`) runs before the first refresh and rehydrates `self._snapshot`, `_snapshot_fetched_at`, `_snapshot_probe_key`, the monthly peak, `_historical_spots`, `_historical_spot_quarters`, `daily_compare` and the archived per-month cards from the Store. An hour carrying any impossible quarter loses the whole list, not the offending slot, because a short list would silently re-weight the hour's mean; its hourly value stays if that passed its own check, so the hour prices energy as it always did and credits feed-in off the mean the slots refine. Neither `STORAGE_VERSION` nor `_SNAPSHOT_SCHEMA_VERSION` moved for the new key: a version mismatch discards the whole blob, the load path reads named keys and ignores unknown ones, and a missing key simply refills. Two guards apply:

- **Tuple mismatch** (`coordinator_persist.py`): if the persisted blob's stamped `(supplier, contract, region)` differs from the current entry, the snapshot and the historical spots are discarded (the peak is supplier-agnostic and kept). This handles a slow tick that saved a pre-OptionsFlow blob after the reload swapped the entry.
- **Corrupt blob** (`coordinator_persist.py`): a `KeyError`/`ValueError`/`TypeError` while decoding drops the cached snapshot and logs a warning; the next refresh repopulates.

Loading an offline boot from disk lets the entry serve last-known prices before any network call succeeds.

## 2. The refresh path

The base class calls `_async_update_data` (`coordinator.py`) every tick. It wraps `_update_body` (`coordinator_tick.py`) and, on `UpdateFailed`, refreshes the stale-snapshot Repairs placeholder with the current `_last_error` before re-raising (`coordinator_tick.py`). The body runs these steps in order.

```
_update_body (coordinator.py)
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

`_maybe_refresh_snapshot` (`coordinator_snapshot.py`) decides whether to re-fetch the full tariff card. It never fetches unconditionally; a full PDF/HTML fetch happens only when a cheap check says the published card changed.

The cheap check is the extractor **probe** (`SnapshotProbe`, `providers/base.py`): a `HEAD` or small listing `GET` that returns a freshness key. Same key across calls means the snapshot is still valid; a changed key means re-fetch. The probe is optional; `None` means the supplier has no reliable probe path (DATS 24 single PDF, energie.be/Engie/Luminus API endpoints) and the time-based TTL takes over.

Decision order in `_maybe_refresh_snapshot`:

1. Run `extractor.probe` if present (`snapshot_store.py`); a failed or absent probe yields `probe_key = None`.
2. **Adopt a sibling** (`snapshot_store.py`): if a shared-cache row for this `(supplier, contract, region)` tuple is fresh against the probe/TTL, adopt it and return, doing zero network work.
3. **Reuse the caller's own row** (`snapshot_store.py`): the caller passes its copy as `local`, a cache row of equal standing judged by the same `_row_is_fresh` (`snapshot_store.py`) - with a probe, `row.probe_key == probe_key`; without one, `now - fetched_at < ttl`. On a probe match `_adopted` (`snapshot_store.py`) restamps `fetched_at` so the age sensor reads "just checked"; on a TTL match it must not, or the expiry is pushed out every tick and the supplier is never re-fetched. The coordinator builds `local` from `_snapshot_raw`, never the VAT-resolved `_snapshot`, because the shared cache is seeded from it and a resolved card would mis-price every sibling on the tuple.
4. **Negative-cache short-circuit** (`snapshot_store.py`): if a sibling just failed on this key within `_SHARED_FAILURE_TTL` (5 minutes), skip the retry and hand back the sibling's error message. Bypassed when `force` is set.
5. **Fetch under the shared lock** (`snapshot_store.py`): re-check the sibling cache and negative cache under the lock, then call `extractor.fetch`. On success populate the shared cache and clear the failure marker.

All five live in `fetch_shared` (`snapshot_store.py`) rather than on the coordinator, because the coordinator is no longer the only caller that needs a card: it returns a `SharedFetch` (`snapshot_store.py`) rather than raising, so one caller can turn a failure into a Repairs card while another prints one row as unreachable. `_maybe_refresh_snapshot` (`coordinator_snapshot.py`) is what is left: build `local`, call, then map the result onto this entry's snapshot, error state and Repairs issues.

`SNAPSHOT_REFRESH_HOURS` is `24` (`snapshot_store.py`): the TTL used only by probe-less suppliers. `SNAPSHOT_STALE_DAYS` is `7` (`snapshot_store.py`): once the snapshot is older than 7 days, `_sync_stale_issue` raises a Repairs warning.

### 2.2 Cross-entry sharing and dedup

Two config entries on the same `(supplier, contract, region)` share one fetched snapshot so the same card is never polled twice. The process-wide state lives in `hass.data[DOMAIN]`:

| Key | Shape | Meaning |
|-----|-------|---------|
| `snapshot_cache` | `dict[tuple, _SharedSnapshot]` | latest shared snapshot per tuple |
| `snapshot_locks` | `dict[tuple, asyncio.Lock]` | dedup lock for first fetch per tuple |
| `snapshot_failed_fetches` | `dict[tuple, (ts, err, count)]` | negative cache of recent fetch failures |
| `monthly_snapshot_cache` | `dict[(sup,con,reg,YYYY-MM), Snapshot | None]` | archived per-month snapshots for YTD |
| `monthly_snapshot_failed_fetches` | `dict[key, ts]` | negative marker for a transient archive fetch, supplier or repository |
| `monthly_snapshot_locks` | `dict[key, asyncio.Lock]` | dedup lock per month key |
| `tuple_generations` | `dict[tuple, int]` | generation counter for eviction races |

`monthly_snapshot_cache` is the one row of that table with an on-disk half: settled months are written to the entry's Store and seeded back into it on load (section 10), because a closed month's card is a historical fact and re-fetching one PDF per elapsed month on every restart is what issue #88 spent its bootstrap budget on. The other rows stay process-local.

`_SharedSnapshot` (`snapshot_store.py`) carries the snapshot, `fetched_at`, and the `probe_key` seen at fetch. `evict_shared_caches` (`snapshot_store.py`) is called from `async_unload_entry` (`__init__.py`) only when no other loaded entry still references the tuple; it bumps the generation counter first so an in-flight fetch that resumes after eviction detects the change and skips its write (`__init__.py`), preventing an orphaned cache row.

### 2.3 The on-disk Store and cache invalidation

The Store is `_MigratingStore` (`snapshot_codec.py`), a `Store[dict]` subclass whose `_async_migrate_func` returns `{}` for any blob written under an older `STORAGE_VERSION` (`snapshot_codec.py`). Every persisted field is re-derivable from a fresh fetch, so dropping the cache on a major-version mismatch is safe and avoids HA's "missing migration function" warning. `STORAGE_VERSION` is `2` (`const.py`).

There is a **second**, finer version inside the serialized snapshot: `_SNAPSHOT_SCHEMA_VERSION`, currently `72` (`snapshot_codec.py`). `_snapshot_to_dict` stamps it (`snapshot_codec.py`); `_snapshot_from_dict` raises `ValueError` when a loaded blob's `_schema_version` is below the `min_schema_version` it is asked for (`snapshot_codec.py`), which `async_load_persistent` catches and treats as "discard and re-fetch", keeping the rejected dict in `_stale_snapshot`.

That gate assumes a next fetch exists to heal with. For a supplier publishing its card as page images there is none, and discarding the blob left every such entry with no prices at all from the first restart after the next bump. `_replay_stale_snapshot` (`coordinator_snapshot.py`) is the exception: when a fetch raises `CardNotReadableError` and nothing is being served, the rejected blob is re-read with `min_schema_version=_DEGRADED_MIN_SCHEMA_VERSION` (`16`, the boundary at which the stored card stopped meaning "as priced" and started meaning "as parsed"), keeps the timestamp it was fetched at so `snapshot_age` and the 7-day stale card still read honestly, and is written back under its own version rather than the running one, so a later parser fix can still invalidate it. `_set_snapshot` resets that stamp, which is what stops a recovered entry writing the old version for ever.

This is the mechanism that lets a parser fix reach already-cached users. A probe-based supplier keeps serving its cached snapshot until the probe key changes (often the next monthly card, weeks away), so a code fix that adds or corrects a snapshot field does not heal existing users unless `_SNAPSHOT_SCHEMA_VERSION` is also bumped to invalidate their cache. The comment block above the constant records why each bump happened (v9/v10 for `DynamicRates.quarter_hourly`, v11 for `supplier_prosumer_eur_per_kva_year`, v12 for per-slot injection, v13 for the mis-parsed Eneco injection snapshot, v14 for the `SpotMonthlyRates` energy kind and the `InjectionRates.floor_at_zero` flag, v15 for the `VariableRates.formula_factor` / `formula_base` coefficients used to re-price a signing cohort, v16 for the persisted snapshot switching to the card as parsed rather than as priced, and for `TaxOverlay.federal_excise_bands`, v17 for `TaxOverlay.region_connection_fee_unavailable`, which also drops the July snapshot an EnergyVision Wallonia entry was stranded on by the August tax-block rewrite, and v18 for three extractor value fixes that shipped in 0.11.37 without it -- Ecopower's double-baked VAT, Mega's hardcoded energy fund, Bolt's residential fund row on professional cards -- which left those entries serving the pre-fix figures after upgrading, and v19 for Mega's realized-rate parser dropping a negative injection rate, which made a variable or Impact entry credit a rate the card charges, v20 for two extractors resolving a *superseded card URL* -- Bolt's pinned variable-card version and Ecopower's six-digit filename pattern -- where the cached snapshot is a perfectly well-parsed copy of the wrong card and neither supplier's probe key moves to dislodge it, and v21 for `InjectionRates.spp_indexed` plus energie.be Variabel parsing its injection formula rather than only the printed indicative, which an older cache cannot carry, and v22 for energie.be Vast parsing that same formula, having emitted only the card's printed indicative before; v57 for Bolt's variable cards carrying the printed Belpex coefficients its quarter-hourly settlement is billed on; v58 for `welcome_credit_eur`, the one-off first-year credit four of EnergyVision's cards print; v59 for `welcome_credit_kind` and Frank Energie's three cashback cards; v60, which adds no field, drops the blobs 0.21.0 and 0.22.0 wrote with Trevion's formulas parsed a factor of ten small, the same value-fix case as v18; v61 drops every persisted EBEM month row, since those carry the estimate the card printed and a closed month is otherwise never asked for again; v62 does the same for those rows' FEED-IN leg, which now settles on the SPP0 the following card publishes and carries it as `InjectionRates.index_realised`; v63 drops Trevion's monthly rows, settled on both of the indices its following card names rather than computed from the spot cache on hourly prices the card defines on quarter-hour ones; v64 for Mega's variable cards carrying `rlp_indexed`, v65 for the VREG network ceiling Luminus and Frank state in a footnote, v66 for TotalEnergies' four per-meter `BELPEXM_RLP` coefficient pairs, v67 for Mega's ristourne, v68 for the four Mega cards granting the whole ristourne only to a direct-debit payer, v69 for the four that grant it after fourteen months rather than twelve, v70 for Luminus's new-customer campaign, which the cards state as a share of the energy cost or a volume of free energy and which only the live card of the signing month carries, v71 for OCTA+'s January and February 2026 dynamic cards, which name the quarter-hourly index Belpex rather than Epex and were read with no feed-in formula or with the AMR clause's formulas in its place, and the non-dynamic cards of those two months, which spell the monthly formula `Belpex SPP` and were read with none, and which also carries `InjectionRates.fixed_for_term` for the cards that fix their printed feed-in price for the term, and Frank's cards dated by the month their title names, the JN card for September 2026 naming August in its validity sentence, and the latest is v72 for Mega's September 2026 Cosy Flex card in Flanders, whose off-peak formula prints `€/kWh` where the three around it print `c€/kWh` and was read with none, so a bi-hourly entry was billed the mono formula every hour, and which also carries `welcome_credit_injection_eur_per_kwh`, the first-year feed-in bonus every Mega card with a feed-in formula prints beside its ristourne). **When you change what an extractor parses into the snapshot, or which card it resolves, bump this constant.** A change that only affects how a stored card is *priced* (`apply_vat`, `resolve_excise_band`) does not need it, because those run on load; `test_a_cache_from_an_older_schema_is_discarded` pins the discard behaviour itself.

## 3. ENTSO-E spot integration

Spots are fetched only for two shapes, and the dispatch reads the *effective*
(cohort-spliced) energy `priced.energy`, not `self._snapshot.energy`:

1. **Dynamic or spot-monthly energy** (`isinstance(priced.energy, (DynamicRates, SpotMonthlyRates))`, `coordinator_tick.py`): dynamic prices each slot at `factor*spot + base`, spot-monthly bills a flat `factor*mean + base` off the month's mean, so both need a spot and share the hard-fail path. `_fetch_spot_prices` is called; `EntsoeAuthError` raises `UpdateFailed` and sets the `entsoe_auth_failed` Repairs issue (`api.py`), while a transient `EntsoeError` degrades to `_fallback_spots` and only fails the tick if that comes back empty (`coordinator_spots.py`). `_fallback_spots` prefers `_spot_cache` (the contract's own resolution, and the only cache that ever holds tomorrow) and falls back to `_historical_spots` (hourly), never merging the two -- a quarter-hourly entry topped up with hourly means would price its slots off two different day-ahead products. A source that cannot price today is skipped outright, so a curve from an earlier day is never served as the current one.
2. **Spot-indexed injection on a static-energy contract** (`_injection_needs_spot`, `injection.py`): here the energy is priced without a spot, so a spot failure must not tear the entry down. The fetch is soft: on any ENTSO-E error it falls back to the cached curve, then to no injection price (`injection.py`). This is the Cociter variable-card case (see section 8).

The tick then computes TWO delivery-month means, because a card can name two
indices: Eneco's energy leg is on Belpex-RLP-M (the RLP-weighted mean, taken
through `_rlp_weighted_month_mean` when the Synergrid profile is loaded) while
its feed-in credit is on Belpex-injectie (the plain one). `energy_mean` feeds
`_build_hourly`; `plain_mean` feeds the injection bake; neither is ever handed
to the other leg.

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
falls through to branch 2's soft path. Only the dynamic and spot-monthly
*contract kinds* are asked for a mandatory API key; a month-indexed variable
contract is offered one as an optional step and may have skipped it, and would
otherwise hard-fail over a key it never held once its leg re-prices to
`SpotMonthlyRates`. `_cohort_legs` therefore drops the cohort leg when no
key is configured (`cohort.py`), keeping the current card instead.

**Which month the cohort asks for** (`_tariff_card_month`, `cohort.py`): the
entry's own tariff card month when it sets one, else its contract start date.
They differ by about a month for anyone who switched supplier, since a fixed or
dynamic contract is locked to the card in force at SIGNING and the switch takes
a month to go through, and reading the start date alone therefore billed a
switcher one card late (issue #96). Only this lookup reads the card month; the
year-to-date window, the fee proration and the welcome-credit window keep
reading the start date, which is about when the household began being supplied.

**Cohort resolution order** (`_cohort_legs`, `cohort.py`): the
hand-entered signing rate first, then the archived signing-month card, then the
current card. The signing-month card comes from the supplier's own archive or
from the repository's card archive, which from August 2026 holds the cards of
suppliers that keep none (`_month_card_retrievable`, `snapshot_months.py`); asking
the supplier's alone left a TotalEnergies or Ecofix cohort billed on each month's
card. When neither of the first two yields a rate (a contract signed
this month, a month neither archive holds, a month older than the archive
reaches), a month-indexed card still takes `_month_indexed_leg` (`cohort.py`),
exactly as it does with no cohort month at all: its printed figure is last
month's index by the card's own words, and a date the archive cannot serve is
no reason to bill it. `_manual_energy_leg` (`cohort.py`) overlays what the user
typed onto whichever card was retrieved, **per field**, so a half-filled form
keeps the archived signing-month values for the boxes left blank rather than
today's. The archive is authoritative only about the *published* card; a
promotional, brokered or negotiated rate exists nowhere online, so the typed
value has to win. It used to lose, which made the signing-rate step a no-op on
exactly the seven suppliers that keep an archive (issue #54).

A typed yearly fee is entered as the card prints it, so on a card published
ex-VAT it has to be un-grossed for an entry that deducts VAT. The rate to
un-gross by is `TaxOverlay.published_vat_rate` (`providers/base.py`), read
as `published_vat_rate or vat_rate`. It has to ride on the snapshot rather than
be passed in, because `apply_vat` zeroes `vat_rate` on an ex-VAT resolve, and
every `_cohort_legs` call site hands in an already-resolved snapshot: the
live tick (`coordinator_tick.py`), the monthly walk (`cohort.py`), the
year-to-date walk and the backfill accrual through `_cohort_energy_leg`
(`ytd_cost.py`, `backfill.py`), and the compare quote, which splices the
feed-in leg beside the energy one as the live tick does
(`compare_household.py`).
`_set_snapshot` (`coordinator_snapshot.py`) is the only writer of
`self._snapshot` and always routes through `_resolve_snapshot`, so by the time
the cohort leg reads the taxes there is no other surviving record of the basis
the card published at. Threading it as a parameter reached the live tick alone
and left the rest 21 EUR/yr adrift on the same entry. The `or` fallback covers
the two snapshots that never went through `apply_vat`: a card printed
VAT-inclusive (`vat_rate` is already `0.0`, so both halves agree) and a probe
cache written before the field existed, so no schema bump was needed.
`test_cohort_leg_bills_the_same_fee_on_every_call_path` pins the agreement.

### 3.1 Resolution selection (hourly vs quarter-hourly)

`_energy_is_quarter_hourly` (`spot_stats.py`) returns True only for `DynamicRates` with `quarter_hourly=True`. Those extractors (Cociter, EBEM, Ecofix, Ecopower Dynamische Burgerstroom, energie.be, Energy Knights Agilior Online, EnergyVision, Engie, OCTA+, Trevion) bill on the native 15-minute Belpex/eSpot_15/Epex/EPEX DA grid, and so does any Bolt or Frank Energie entry whose settlement step ticked the quarter-hour box, which `resolve_settlement_grid` applies before the coordinator ever sees the leg; every other contract stays hourly. `_fetch_spot_prices` passes this as `quarter_hourly=` to `client.fetch_day_ahead` (`coordinator_spots.py`), asked of the leg the tick prices on (`_billing_snapshot`), which is the cohort's where one is spliced in: a LifePowr entry on the May 2026 cohort bills a quarter-hourly formula under a card that is now monthly. The constants are `RESOLUTION_HOURLY = "PT60M"` and `RESOLUTION_QUARTER = "PT15M"` (`const.py`), matching ENTSO-E's resolution tokens. YTD billing stays hourly regardless (section 7).

The year's historical fetch (`_walk_historical_spots`) uses the same grid, widened per month: a
closed month whose archived card bills per quarter-hour while the entry's current card does not
(a product that changed grid mid-year, Trevion LifePowr before June 2026) is fetched on the
15-minute product, in its own run of chunks, so the year-to-date walk prices it off the auction it
was billed on. `_quarter_grid_days` remembers which local days came from that product and is
persisted beside the spot cache (`historical_spot_quarter_days`); a day fetched on the hourly
product before the month's card was known counts as missing and is fetched again. The grid is
never narrowed, and an entry on the 15-minute grid itself never fetched any other way, so its
cache is trusted as before and the marker only ever matters to an hourly entry's widened months.

### 3.2 The today/tomorrow spot cache

`_fetch_spot_prices` (`coordinator_spots.py`) windows the request on the *local* (Europe/Brussels) day, anchored on local midnight converted to UTC, so a 00:00-02:00 local query doesn't drop yesterday's UTC tail and the fall-back Sunday's 25th local hour is not lost (`coordinator_spots.py`). It requests tomorrow only when `now_local.hour >= 11` (`coordinator_spots.py`), because ENTSO-E publishes the day-ahead curve around 12-13 CET. The cache is keyed on `_spot_cache_day` and `_spot_cache_includes_tomorrow`; the latter records what the response *actually* carried, not what was asked for (`coordinator_spots.py`), so a pre-publication request for tomorrow doesn't lock the flag and block the next tick from retrying.

The curve is persisted under the `spot_cache` payload key and restored beside `historical_spots`, so an ENTSO-E outage spanning a restart still has something to price with. `_historical_spots` cannot stand in for it: it is only ever filled up to today, so it never carries tomorrow, and it is bucketed to the hour, so it cannot give a quarter-hourly contract its own slots back. It is restored as a *fallback* only -- `_spot_cache_day` deliberately stays `None`, so the first tick after a restart still fetches from ENTSO-E as usual and the stored curve is consulted only when that fetch fails, which also avoids adopting a partially-written curve as authoritative for the day. Slots that no longer fall on today or tomorrow are dropped on load; gating there rather than on save keeps the rule in one place, and the cache is two days of slots at most either way. The key is additive, so no `STORAGE_VERSION` bump is needed -- `_MigratingStore` only discards a blob on a version *decrease*.

### 3.3 Historical spots for YTD

`_ensure_historical_spots` (`coordinator_spots.py`) fills `self._historical_spots` for every local hour in `[Jan 1, today]`, fetching missing week-sized chunks from ENTSO-E and handing whatever ENTSO-E could not answer to the keyless fallback in a single request. It runs only for dynamic or spot-indexed-injection contracts (`coordinator_spots.py`) and needs the entry's API key (`coordinator_spots.py`), returning early if there is none. Details:

- **The FIRST tick asks for the delivery month, not the year.** `async_config_entry_first_refresh` runs inside setup, and setup is what the config flow's final step waits on, so a cold cache spent that step fetching 35 week-chunks: minutes of spinner on a fresh install, and far longer with ENTSO-E down. The first tick fetches `[month start, today]` -- the window the monthly mean computed straight after cannot do without -- clears `_year_spots_deferred` and schedules `_fill_year_spots` (`coordinator_tick.py`) as an entry-tied background task, which walks `[Jan 1, today]` and then requests a refresh. What the deferral costs is one tick of the year-to-date's past hours, which bill their network and tax legs and forfeit only the energy term exactly as a cold cache already makes them; the requested refresh puts them back. Entry-tied, so unloading cancels it and a user who walks away from a fresh install mid-backfill leaves no fetch running.
- **And bounded, because scoping the window is not the same as bounding the wait.** Each week-chunk carries a 30 s client timeout, and a chunk that times out is logged and followed by the next one rather than ending the walk, so the running month is about 180 s against a source that hangs instead of refusing -- inside the same 300 s bootstrap budget. The first tick's fill runs under `_FIRST_TICK_SPOT_BUDGET` (45 s, `coordinator_tick.py`); on the deadline it keeps every chunk that did land, since they are merged one at a time, logs what it gave up on and leaves the rest to `_fill_year_spots`. It only ever bites on a source that hangs, which is exactly the case where waiting buys nothing. The today/tomorrow curve is deliberately NOT bounded: it is what the live price table is built from, one request plus one fallback, and an entry that skipped it would publish no price at all.
- **One walk at a time.** `_ensure_historical_spots` is a thin wrapper holding `_spot_fetch_lock` around `_walk_historical_spots`. Three callers reach it from outside the tick -- the statistics backfill, the compare page, and that deferred year-fill -- and nothing serialised them, so two could walk the same empty cache at once and each fetch what the other was already fetching. Whichever arrives second finds the days present and returns without a request.

- A day counts as "present" when at least 20 of its 24 hours are cached (`coordinator_spots.py`), tolerating ENTSO-E source gaps and DST seams (23/25-hour days).
- Stable past days that stay short after a fetch get a `_spot_day_retry_at` marker holding the instant they may be retried at; `_SHORT_SPOT_DAY_TTL` is 12 hours (`coordinator_spots.py`), so a genuinely-gappy past day is retried twice a day rather than every tick.
- A window NEITHER source could serve gets the same marker on the shorter `_SPOT_OUTAGE_TTL`, 3 hours (`coordinator_spots.py`). Without it a double outage was self-sustaining: only an auth error marked anything, so an ENTSO-E 5xx with the fallback rate-limited behind it left every day as short as it was and the next tick re-walked all 35 chunks, hour after hour for as long as the outage lasted. Three hours rather than twelve because the distinction is real -- a source gap may be data that never existed, an outage is data behind a server that will come back, and holding it half a day would leave the year-to-date missing an energy term long after it did.
- Day boundaries anchor on local midnight in UTC, matching the recorder window and the persistence cut-off (`coordinator_spots.py`); a UTC anchor would leave the first hour or two of the local year unfetched.
- Fetch failures are logged and skipped; absent hours are treated as "no data" for the YTD, never a tick failure. A failure leaves the day as short as it was, so an `EntsoeAuthError` marks the chunk's stable past days too: without that, a revoked token re-pulled every week-chunk of the year on every hourly tick. That class covers a rejected key, an exhausted daily quota, and an acknowledgement carrying no matching data, which for a past chunk can simply mean the data does not exist; none of the three is fixed by asking again in an hour, and the cost is that a transient one holds its days for the TTL. An auth failure also skips the fallback entirely: a credential its owner has to renew must keep raising its Repairs card rather than being papered over by a keyless source.
- **The walk drives `EntsoeClient` directly, and the fallback runs once at the end.** A plain `EntsoeError` (timeout, 5xx) does not fail its chunk on the spot: the chunk is collected, and `_fill_spots_from_fallback` makes ONE energy-charts request spanning everything ENTSO-E refused. Routing each chunk through `fetch_day_ahead_or_fallback` instead asked that endpoint 35 times for a year, and it rate-limits `/price` to two requests per minute per client IP, so an ENTSO-E outage got two chunks answered and 33 HTTP 429s. The response is filtered back to the days that actually failed, so hours ENTSO-E did serve keep the source of record. One warning names the whole span instead of one per week, and `_spot_source` is still never written here -- it describes the live curve, which is fetched earlier in the same tick and may legitimately have a different source.
- The fetch asks for the same grid the contract settles on, via the same `_energy_is_quarter_hourly` test the live fetch uses (`coordinator_spots.py`). ENTSO-E publishes Belgium as two products, a PT60M and a PT15M series covering the same delivery period, and `parse_day_ahead_xml` deliberately refuses to blend them (`api.py`): requesting without the flag silently takes the hourly product, so a quarter-hourly contract's whole replay was priced off a different auction than its live bill.
- Whatever grid comes back is stored by clock hour, collapsed on the mean of the slots inside it (`_bucket_spots_by_hour`, `spot_stats.py`). The recorder only keeps hourly consumption, so an hour is the finest thing a replay can price, and the mean is exact for every formula that is LINEAR in the spot: pricing the hour's mean equals replaying each quarter against a quarter of that hour's kWh. Keeping the cache hourly is also what its 20-of-24 completeness test, its persisted form and every reader already assume.
- One formula is not linear. A floor, `floor_at_zero` or a stated `minimum` (both applied by `_floor_injection`), makes the feed-in rate `max(floor, factor*spot + base)` convex, so the mean of the floored quarters is at least the floored mean, and flooring once at the hour credits less than the live per-slot array whenever the spot crosses the floor inside the hour. Such an entry keeps that hour's own slots beside the mean, in `_historical_spot_quarters`, grouped by hour (`_group_spot_quarters_by_hour`, `spot_stats.py`) and gated on `_injection_needs_spot_quarters` (`injection.py`): injection regime, floored formula, quarter-hourly energy. A day remembered as complete (`_complete_spot_days`) was measured against one cache or the other, so the set is dropped whenever that answer flips inside one coordinator lifetime (`_complete_spot_days_quarters`): a card gaining a floor mid-year otherwise kept the shortcut answering 24 for days whose quarters were never fetched, until the next restart. Only the expert custom supplier sets `floor_at_zero`, so every other entry grows nothing. An hour holds one to four values (ENTSO-E answers a PT15M request with the PT60M series where no 15-minute one exists), and the replay averages whatever it holds.
- The same gate CLEARS the cache when the entry stops needing it. Unticking the quarter-hourly box or the never-negative one, or leaving the injection regime, changes none of the (supplier, contract, region) tuple the reload is gated on, so the cached year would otherwise be restored and re-persisted for the life of the entry while the replay went on crediting those hours per slot and the sensor beside it credited the hour.
- Coverage is measured against whichever cache the entry replays from, through one shared `_cached_spot_hours` (`coordinator_spots.py`) used by both the pre-fetch scan and the post-fetch recount. That is what refills an existing entry once after an upgrade: its hourly days are already complete, so nothing would ever be re-fetched otherwise. The two counts must stay one function, or a day reads short before a fetch and complete after it and is re-fetched every tick.

## 4. The published data dict

`CoordinatorData` (`coordinator_data.py`) is the contract with the entity platforms. Entities read fields either directly (via a description `value_fn`) or from the current-slot `PriceBreakdown`. Every field:

| Key | Type | Meaning | Read by |
|-----|------|---------|---------|
| `hourly` | `dict[datetime, PriceBreakdown]` | UTC-keyed price table (48-ish slots covering today+tomorrow); keys are hour or quarter-hour boundaries per `resolution` | current/next/today/tomorrow price sensors and window services; `tomorrow_prices_available` binary sensor (`sensor.py`, `binary_sensor.py`) |
| `resolution` | `str` | `RESOLUTION_HOURLY` or `RESOLUTION_QUARTER`; slot width of `hourly` keys | slot truncation in `sensor.py`; window sizing in `__init__.py` |
| `snapshot_publication` | `str` | supplier's publication label for the current card | `current_price` sensor attribute (`sensor.py`) |
| `signing_card` | `str` | the card the cohort month resolved to; empty when the entry names none | `current_price` sensor attribute, diagnostics `coordinator` block |
| `snapshot_age_hours` | `float` | hours since `_snapshot_fetched_at` (`inf` if never) | `current_price` sensor attribute (`sensor.py`) |
| `snapshot_stale` | `bool` | True when age > 7 days | `current_price` sensor attribute (`sensor.py`) |
| `snapshot_valid_until` | `date \| None` | last calendar day the rates apply; `None` = unknown | `tomorrow_prices_available` binary sensor (`binary_sensor.py`) |
| `last_error` | `str` | last human-readable failure reason | `current_price` sensor attribute (`sensor.py`) |
| `monthly_peak_kw` | `float` | Flanders running monthly peak in kW, as measured (NOT floored) | `monthly_peak_kw` sensor (`sensor.py`) |
| `capacity_billed_peak_kw` / `capacity_peak_months` | `float` / `int` | the twelve-month mean the tariff is charged on, and how many months it covers. Both read `_peak_terms()`, which leaves the in-progress month out until it has a reading; deriving the count separately as `len(_peak_history) + 1` claimed a month the mean had not taken | `capacity_cost` sensor attributes |
| `monthly_peak_month` | `date \| None` | month the peak belongs to | diagnostics (`diagnostics.py`) |
| `capacity_cost_eur` | `float` | monthly Flemish capacity cost estimate | `capacity_cost` sensor (`sensor.py`) |
| `prosumer_cost_eur` | `float` | monthly Walloon compensation-regime prosumer fee | `prosumer_cost` sensor (`sensor.py`) |
| `injection_price_eur_per_kwh` | `float \| None` | injection price for the slot the tick ran in; `None` off the injection regime or when a needed spot is missing | `injection_price` sensor fallback for contracts with no `injection_hourly` (`sensor.py`) |
| `injection_hourly` | `dict[datetime, float]` | per-slot injection price over the same today+tomorrow grid as `hourly`; empty unless on the injection regime with an intra-day-varying injection (spot-indexed or TOU) | `injection_price` sensor state at the current slot, plus its `today`/`tomorrow` arrays |
| `yearly_fixed_fee_eur` | `float` | supplier flat annual subscription for the configured meter | `fixed_fee_eur_per_year` sensor (`sensor.py`) |
| `energy_fund_eur_per_month` | `float` | Flemish Energiefonds monthly charge | `energy_fund_eur_per_month` sensor (`sensor.py`) |
| `ev_home_charging_rate_eur_per_kwh` | `float \| None` | the SPF's flat-rate ceiling for reimbursing home charging of a company car, for the entry's region this quarter (`creg_ev.py`); `None` until the CREG's file has been read | `ev_home_charging_rate` sensor (`sensor.py`) |
| `ev_home_charging_quarter_start` | `date \| None` | the quarter that rate is for, baked with it at the tick so the sensor never pairs one quarter's rate with the next quarter's start | `ev_home_charging_rate` sensor attribute (`sensor.py`) |
| `current_year_cost_eur` | `float \| None` | running YTD bill since Jan 1; fees-only floor when no meters wired | `current_year_cost` sensor (`sensor.py`) |
| `previous_contracts` | `tuple[dict, ...]` | the contracts held earlier in the year after a recorded supplier switch, one row each with its dates and cost (`contract_periods.previous_rows`); empty otherwise | `current_year_cost` sensor attribute (`sensor.py`) |
| `ytd_diagnostics` | `dict[str, float] \| None` | optional breakdown behind the bill. Static path: YTD + today consumption/injection kWh, `energy_ytd_raw_eur` (pre-clamp energy term). Hourly path (TOU / dynamic / spot-monthly): `hours_seen` + `hours_priced`, which say how much of the window the spot cache could price, plus YTD consumption/injection kWh, and `energy_ytd_raw_eur` too on the compensation regime. `fees_ytd_eur` on both, split into `capacity_ytd_eur` + `prosumer_ytd_eur` + `standing_charges_ytd_eur` with the `billed_peak_kw` they were billed on, since the capacity leg is per kW of monthly peak per year and is the leg most able to separate two entries reading one meter; `None` when no meter is wired | `current_year_cost` sensor attributes (`sensor.py`) |
| `projected_year_cost_eur` | `float \| None` | a full year priced at today's tariffs against the entry's own metered yearly volume, computed in one pass rather than as elapsed plus remainder. `None` for a dynamic or spot-monthly leg, whose future months have no knowable rate | `projected_year_cost` sensor |
| `projection_diagnostics` | `dict[str, Any] \| None` | the basis behind that number: `energy_basis`, `fee_basis`, `volume_basis`, `injection_basis` and `contract_basis` as strings, plus `annual_kwh`, `annual_injection_kwh` and `welcome_credit_eur` (what is left of the first-year welcome credit over the coming year, 0.0 without a start date) | attributes of the same sensor |

The current-slot sensors (`current_price`, `energy_component`, `network_component`, `taxes_component`, and the today/tomorrow min/avg/max) do not read a top-level field: they index `hourly` at the current slot and read a `PriceBreakdown` attribute (`all_in`, `energy`, `network`, `taxes`). `resolution` populated as `RESOLUTION_QUARTER` only when `_energy_is_quarter_hourly(priced.energy)` (`coordinator_tick.py`), the cohort-spliced leg, which is also the leg the spot fetch reads its grid off (`_billing_snapshot`, `coordinator_spots.py`) so the two cannot disagree; everything else is hourly.

`yearly_fixed_fee_eur` and `energy_fund_eur_per_month` are parsed from the card but do NOT enter the per-kWh all-in number (`coordinator_tick.py`); they are surfaced separately so users can compute a total monthly cost.

## 5. Slot selection and the live price table

`_build_hourly` (`coordinator_tick.py`) builds the UTC-keyed `hourly` table:

- **Dynamic** (`coordinator_tick.py`): one breakdown per spot returned by ENTSO-E; the table's resolution follows the spot grid (15-minute for quarter-hourly suppliers).
- **Static/TOU/Impact** (`coordinator_tick.py`): iterate UTC from local midnight to the start of the day after tomorrow, one slot per clock hour, so DST seams keep the wall-clock gap correct (47 slots spring-forward, 49 fall-back, 48 otherwise). The local-midnight anchor makes `today_min`/`today_max`/`today_average` cover the full local day rather than "now to midnight".

The entities, not the coordinator, do the current/next-slot lookup. `sensor.py` truncates `utcnow()` to the slot with `slot_start(..., data.resolution)`, reads the exact slot, and if it is missing accepts the nearest slot within one slot width (`max_gap` 3600 s hourly, 900 s quarter-hourly, `sensor.py`). `next_hour_price` targets `slot_start(now) + 1h` (`sensor.py`).

### 5.1 Slot-boundary push

The coordinator's 60-minute tick is not clock-aligned. `async_setup_entry` registers an `async_track_time_change` callback (`__init__.py`) that fires `coordinator.async_update_listeners()` at `:00` (and `:15/:30/:45` for a quarter-hourly supplier, `__init__.py`) so the live price sensors re-read the wall clock at the exact slot boundary without re-fetching. The cadence follows the table's `resolution` and is re-registered by a coordinator listener whenever that moves: fixed once from `coordinator.data` at setup, it stayed hourly on a quarter-hourly Ecofix entry whose first refresh was tolerated for an unreadable card, since that entry had no table yet.

The push only helps a sensor whose `value_fn` reads the clock: re-evaluating a value baked into `CoordinatorData` yields the same value. That is why every per-slot number a user sees has to come out of a per-slot table indexed at `utcnow()`, not out of a scalar the tick resolved. `injection_price` was the exception until issue #44 and now goes through `_current_injection`.

### 5.1.1 Local-day rollover

One boundary the push cannot cover is midnight, because there the *table* goes stale rather than the reading of it. `_build_hourly` anchors its today + tomorrow span at `dt_util.start_of_local_day()` as of the tick that built it, so once the date rolls over the table describes yesterday + today: `tomorrow_average` / `tomorrow_min` / `tomorrow_max` have no rows to reduce and read `unknown`, and `tomorrow_prices_available` drops off. Since the coordinator's tick is not clock-aligned, that lasted until whenever the next tick landed, up to an hour, every night.

A second `async_track_time_change` listener at `hour=0, minute=0` therefore calls `coordinator.async_request_refresh()`, which re-anchors the table on the new local day. `async_track_time_change` matches *local* time, so this follows Europe/Brussels across both DST seams; local midnight exists on each (unlike 02:00 on the spring-forward Sunday). The cost is one extra tick per day on top of the 24 hourly ones, and that tick goes through the same probe / TTL path, so it usually costs a freshness probe rather than a card fetch.

The listener's `second` is not 0 but `zlib.crc32(entry.entry_id.encode()) % 60`. Every install of a Belgian integration shares one timezone, so a fixed second would land the entire user base on a supplier's doorstep simultaneously once a night; the hourly tick is already spread because it is anchored on each install's setup time. `crc32` rather than `hash()` because the latter is salted per process and would move the entry to a different second on every restart. The worst-case staleness this leaves is 59 seconds, against the 59 minutes it replaces.

Widening the table to three local days would also have papered over the symptom, and was rejected: the window services search the whole table when `latest_end` is omitted, so a third day changes their answers. On a Luminus SmartFlex contract on 2026-03-19, the cheapest 4 h window moves from `2026-03-19T22:00` to `2026-03-21T11:00` (the first spring/summer day, whose 11:00-17:00 band drops to super-creuses) purely because the extra day is in range.

### 5.2 Cheapest / most-expensive window

The window computation is *not* owned by the coordinator. `_find_window` (`__init__.py`) is a pure helper behind the `cheapest_window` and `most_expensive_window` services (`__init__.py`). It reads `coordinator.data.hourly` and `.resolution`, scales the requested `duration_hours` to slots via `slots_per_hour(resolution)` (`__init__.py`), and only considers strictly time-contiguous runs (`__init__.py`) so a gap in a dynamic table can't stretch a window past its duration. `_today_ranked` in `sensor.py` computes the `cheapest_4h_today` / `most_expensive_4h_today` attributes on the `current_price` sensor.

## 6. Monthly capacity peak (Flanders)

`_track_monthly_peak` (`coordinator_peak.py`) maintains `_peak_kw`/`_peak_month` for the Flemish capaciteitstarief:

- Outside Flanders it resets both to 0/`None` (`coordinator_peak.py`) so a stale peak from a former Flanders config doesn't linger.
- It rolls over on the local 1st of the month (`coordinator_peak.py`); UTC would lag CET/CEST users at the boundary.
- `CAPACITY_MODE_FIXED` uses the configured value directly (`const.py`); `CAPACITY_MODE_SENSOR` takes a rolling max of the peak-power sensor (`const.py`), scaling W/VA to kW (`const.py`, issue #19: an unscaled 4481 W stored as 4481 kW inflated capacity cost 1000x). A reading whose `last_updated` precedes the local 1st is ignored: HA's dsmr integration writes at most every 30 s, so the rollover tick can still see last month's maximum, and a running max seeded with it would bill the old month's peak for the new one.
- On rollover the closing month is banked into `_peak_history` and the window is pruned to the eleven most recent completed months, so with the running one the mean covers twelve. A month still at `0.0` is not banked: no reading was ever collected, which is not a measured zero.

`_billed_peak_kw` turns that window into the quantity Fluvius actually charges on, the "gemiddelde maandpiek". Its methodology gives the formula outright: `Rekenkundig gemiddelde van de Max (Maandpiek (m), 2.5) voor elke maand (m)`, i.e. the floor lands on each month BEFORE the mean, not on the mean. Every term is then at least the floor, so the mean is too and no outer clamp is needed. `CAPACITY_MODE_FIXED` bypasses the window and floors the configured value directly. `_peak_kw` itself is left raw, so `monthly_peak_kw` reports a measurement rather than a billing figure. The in-progress month only joins the mean once it HAS a reading: it is reset to 0 on the local 1st, and a zero floored to 2,5 kW is not a measured peak, so counting it stepped the mean (and with it `capacity_cost` and `current_year_cost`) down at every rollover and back up as the month accrued. This is the same estimate-the-gap rule already applied to a month that was never measured.

`_compute_capacity` (`fees.py`) then returns `billed_peak_kw * capacity_eur_per_kw_year / 12` from the configured DSO overlay, or 0 when the overlay omits a capacity rate. That feeds the `capacity_cost` sensor.

The same charge is accrued into the running bill by `_ytd_capacity`, which walks the year month by month like `_ytd_prosumer` and prorates each month by `days_in_ytd / days_in_full_month`, reading each month's archived overlay so a VREG indexation landing mid-year applies only to the months it covers. It takes the meter the caller is pricing on, not the entry's, because the ceiling is measured against the per-kWh network term and an exclusive-night circuit bills its own: the comparison page's year-to-date what-if quotes a meter the household need not have. It applies the CURRENT gemiddelde maandpiek to every month rather than reconstructing one per month: the rolling window holds at most twelve months, and an entry installed mid-year has no history for the months before it, where Fluvius billed against meter history the integration never saw. Because the quantity is itself a twelve-month mean it moves slowly, so the current value is close to what each month of this year was billed on. All three cost paths use it: the live sensor, `backfill.py` (per local day, divided by that day's real UTC-hour count so the DST seam days still total a full daily share) and the OptionsFlow compare what-if. The one-month formula itself lives in `_capacity_monthly_eur`, which all three call: `peak x rate / 12` plus the two "nothing to bill" cases (no overlay for this DSO, no capacity row on the card). It was written out three times with three spellings of those guards, which is exactly the drift `_annual_static_fees` is shared to prevent -- capacity was the fee left out of it. The helper is deliberately region-agnostic: each caller keeps its own Flanders gate.

## 7. Year-to-date / current-year cost

`_compute_current_year_cost` (`ytd_cost.py`) computes the running bill from the year-to-date window start to today. That is 1 January of the local year unless the entry ticked `ytd_from_contract_start` beside a contract start date, in which case `ytd_window_start` (`cohort.py`) returns the later of the two -- clamped to 1 January, because the sensor is a TOTAL the recorder buckets per calendar year and a window reaching into a previous year would have the compiler see a reset that never happened. Every leg reads that one helper: the hourly and daily energy walks, `_walk_ytd_months` (so fees pro-rate over the days the contract actually covers rather than billing a full year against half of one), the historical spot fetch, the statistics backfill, and the `last_reset` the sensor publishes, which the tick bakes into `CoordinatorData.current_year_cost_reset` (and `current_month_cost_reset` for the month's window) from the same clock reading it prices the window with, so the hourly push at 00:00:00 on the 1st cannot pair last period's total with this period's reset. It bills each past day at the tariff of the month that day belongs to, using an archived snapshot when the supplier exposes `fetch_for_month` (`providers/base.py`) and the current snapshot as a proxy otherwise (`_snapshot_for_month`, `snapshot_months.py`). When a contract start date is set it routes every past month through `_effective_snapshot_for_month` (`cohort.py`) instead, which splices the signing cohort's energy leg AND its feed-in coefficients (the single pair, or one pair per band on Engie Empower Flextime) onto each delivery month's overlays (the coefficients, with the index the formula reads and any floor or minimum it promises, come from the signing card and are laid onto that month's own feed-in leg, so the printed figure a keyless entry is credited stays the month's, and so does the settled index when the signed formula reads a month; a card that fixes its printed feed-in price for the term, `InjectionRates.fixed_for_term`, keeps the signing card's figure instead), and dispatches on that cohort's effective energy kind so a re-priced variable contract takes the monthly-mean path. The whole year is recomputed from scratch each tick by design (`ytd_legs.py`): prior days are not immutable (a late ENTSO-E fill or a backfill correction changes a past rate), and the full replay is cheap pure arithmetic.

**The FIRST tick prices the year from the cards already in hand.** The walk above is one archived PDF per elapsed month, and it runs inside config-entry setup: a Frank Energie card takes about 25 s to lay out on a Raspberry Pi, so a September start spent 226 s there and Home Assistant cancelled the whole of bootstrap stage 2 over it (issue #88). The first tick therefore passes `cached_only` (`coordinator_tick.py`), which answers every month from the cache and never reaches the network (`snapshot_months.py`), clears `_month_cards_deferred` and schedules `_fill_month_cards` (`coordinator_tick.py`) as an entry-tied background task. That walks the same months for real and requests a refresh, but only when it retrieved a card the tick did not have -- a warm cache changes nothing and an extra full tick per entry per restart would buy nothing. What the deferral costs is the months the cache is missing: they bill their fees, network and tax legs off the current card rather than their own, which is what a supplier with no archive bills all year. A row the cache *does* hold is handed back even past its TTL, since a caller that cannot fetch keeps what it has rather than forfeiting a month it is already holding. `cached_only` stops at the year-to-date walk: `_cohort_legs` still resolves the signing month, because the live price table is built from it on the same tick and its row is in the cache by then.

Settled months are also written to disk (section 10), so the fill has nothing left to fetch after the first day and a restart costs one card, not one per month.

Fees are always summed first and act as the floor: `_ytd_static_fees` (`ytd_legs.py`, the supplier yearly fee, energy fund, DSO data-management fee, and Brussels OSP fee, pro-rated per archived month) plus `_ytd_prosumer` (`ytd_legs.py`, the Walloon compensation fee). If no meters are wired the function returns fees only, never `unknown` (`ytd_cost.py`).

Three energy paths, chosen by contract shape:

1. **Dynamic** (`ytd_energy.py`): replay `_historical_spots` through `_ytd_hourly_energy` (`ytd_energy.py`), billing each recorded hour at its actual `factor*spot+base`. An hour the cache cannot price is not dropped: `compute_network_and_taxes` bills its network and tax legs, which do not depend on the day-ahead price, and only the energy term is forfeited. An empty cache therefore lands on the fees floor plus the grid and tax cost of every metered kWh, not on fees alone. `hours_seen` / `hours_priced` in `ytd_diagnostics` report how much of the window got an energy price.

   On the spot-monthly variant the mean is taken per delivery month, so coverage is gated: `_covered_month_mean` (`spot_stats.py`) refuses a CLOSED month holding fewer than `_MIN_MONTH_HOURS` (24) cached hours, because that mean is applied to every hour of the month and a handful of hours yields a confident wrong rate rather than a noisier one. The running month keeps its mean: it is partial by definition. The threshold is an absolute count rather than a fraction because refusing forfeits the whole commodity leg (about 40% of the all-in rate), so the mean only has to beat a 100% error to be worth billing, and measured against real Belgian day-ahead prices it does so everywhere down to about a day's worth of hours. The same gate gates the SPP-weighted injection mean (`_month_is_thinly_cached`).
2. **Per-hour billing needed** (`ytd_energy.py`): TOU or Impact energy, or DSO Impact mode, or an `exclusive_night` meter. These also go through `_ytd_hourly_energy`, because their energy/distribution rates vary by hour-of-day or use the dedicated exclusive-night column the static per-day branch doesn't carry.

   All three branches are handed the same inputs: the spot cache, the hour's quarter slots, and the SPP and RLP profiles. This branch used to be called with none of them, on the reasoning that its ENERGY needs no spot, which is true and is not what the arguments are for. The feed-in credit and the compensation allocation ride on the same four, so a month-indexed credit resolved against nothing and fell back to the figure the card prints for the PREVIOUS month, and a reversing meter was settled as metered rather than on the load profile. Whether an hour's ENERGY leg needs a spot at all is now asked of the leg itself (`_energy_needs_spot`), not inferred from whether a cache was passed in, which is what let the arguments be dropped in the first place. Because the branch now holds the spot cache, it credits a per-hour feed-in formula inside the walk and adds no separate `_ytd_spot_injection_credit` term; only the per-day branch below still needs one.
3. **Static per-day** (`ytd_cost.py`): `_resolve_daily_kwh` (`energy_meters.py`) gives per-day `(day_cons, night_cons, day_inj, night_inj)`, each day billed against `static_breakdown` for its month.

### 7.1 Day/night register vs single-total reconstruction

`_resolve_daily_kwh` resolves the consumption and injection sides independently from one of three wirings, keyed by `CONF_CONSUMPTION_KWH` / `CONF_INJECTION_KWH` and the day/night register pair (`const.py`):

- **Day + night register pair** (`CONF_DAY_*_KWH` + `CONF_NIGHT_*_KWH`): one recorder delta per day per register, fanned into band slots, on the days BOTH registers report (`_paired_keys`, `energy_meters.py`). A live register writes a row for every day whether or not it moved, so a day only one half holds is missing data rather than a zero: a register that stopped mid-year is billed on the days both cover and `days_seen` says so, and such a day leaves the other side too (the hourly walks and the backfill drop the hours in `MeteredHours.unknown` from both maps, and the per-hour feed-in credit added to the per-day walk only credits the days that walk billed), since crediting the feed-in of a day whose consumption is not billed drives the year down on days nothing was charged, and one that records nothing at all is refused like a partial pair below. Both are judged on the days before today (`_split_today`): today's value is a live reading off the state history, which a register compiling no statistics still has, so with today in the pair such a register looked alive, the year to date was billed on today alone and no register was named for the Repairs card. Today is billed on top when both halves read it and neither has stopped (`_stopped`, relative to the pair, so a statistics stall that holds both back names neither); otherwise today leaves both sides like any day one half did not report, and the hourly walks skip the live top-up (`MeteredHours.today_ok`), which is all or nothing across a side's sensors, since a stopped pair's day drops out at midnight and billing it live meant taking it back the next morning. A pair that does not report the same days is billed off the side's totals sensor instead when one is wired, on the per-day walk, the hourly walks and the yearly volume alike, and the register is still named for the Repairs card; the hourly walks top today up off that same sensor (`MeteredHours.sensors`). The hourly walk and the backfill read the pair through `_metered_hourly_kwh` on the same rule, hour by hour.
- **Single totals sensor** (`CONF_CONSUMPTION_KWH` / `CONF_INJECTION_KWH`): for mono meters the total goes to the day slot and the math sums it; for bi/dynamic meters `_recorder_daily_band_ratio` (`energy_meters.py`) recovers the day/night split from hourly recorder statistics binned on `is_offpeak`, defaulting to a time-weighted `_default_band_ratio_for` (`energy_meters.py`) for days with no accumulation so a flat Sunday isn't billed all-peak.
- **Partial pair** (one register half missing): returns `None`, so the caller falls back to the fees-only floor rather than silently undercounting a band (`energy_meters.py`).

Which branch runs follows the **effective** meter, not the entry's: `_compute_current_year_cost` passes its `meter_override` down, so a comparison quoting a target contract on a meter the household does not have splits the kWh the way that contract will bill them. Without it a mono household's totals sensor stayed on the mono branch (everything in `day_cons`) while the pricing took the bi branch, charging the peak rate for every kWh of the year instead of the day/night blend.

The recorder is read via `_recorder_rows` (`energy_meters.py`), which requests the `change` field (delta of the cumulative `sum`, not the all-time total) with `units={"energy": "kWh"}` so a Wh/MWh sensor is normalised rather than billed 1000x wrong.

**Today is read live, not from statistics.** `_recorder_daily_kwh` (`energy_meters.py`) takes past days from the daily statistics but overrides the current day with `_live_today_kwh` (`energy_meters.py`): the meter's current cumulative state minus its reading at local midnight (from `get_significant_states`), converted to kWh. Long-term daily statistics only reflect the last *compiled* hour, so relying on them for today made `current_year_cost` step once an hour at best and freeze entirely if statistics compilation lagged or stalled while the meter state kept updating. The live read tracks today's usage in real time and survives a statistics stall; it falls back to the daily statistic when the meter is unavailable, non-numeric, carries an unconvertible unit, or has no reading at midnight yet, and only fires when the requested window ends on the actual current day (so the compare / diagnostics callers that pass historical ranges are unaffected).

**The hourly branch gets the same guarantee.** Every hourly-billed contract (dynamic, spot-monthly, TOU, Impact, exclusive-night) takes `_ytd_hourly_energy`, which reads long-term HOURLY statistics and so reflects only the last compiled hour: it stepped once an hour at best and froze outright whenever compilation lagged or stalled, while the meter kept updating. `_top_up_today_hourly` closes that: it reads each configured meter live, subtracts what statistics already carry for today, and attributes the shortfall to the CURRENT hour. That is where the missing energy was (statistics trail real time, so what they have not booked yet is the most recent consumption) and it prices the top-up at the hour the user is living through, which is the point of a live read on a dynamic contract. Statistics that have caught up, a meter that ran backwards, or a meter with no reliable live reading all leave the statistics figure standing.

A reading below midnight's is a reset when the meter published a `last_reset` later than local midnight, and otherwise only when `state_class` is `total_increasing` and the reading fell below 0.9 x midnight's (`_RESET_BELOW`), the line HA's own statistics draw in `reset_detected`; a smaller fall is a dip, which the statistics carry as a negative `change` the past days drop, so today reads it as zero too. Taking every fall of such a meter as a reset billed a 0,01 kWh dip of a 12345,60 register as 12345,59 kWh for the day (`tests/recorder/test_meter_dip.py`). `last_reset` is the signal that generalises: HA's `utility_meter` reports `TOTAL` when `net_consumption` is set and `TOTAL_INCREASING` otherwise, and it cycles either way, so a `net_consumption` helper on a monthly cycle is a falling `TOTAL` meter that genuinely does reset. Gating on the class alone read its rollover as an ordinary fall and returned minus the whole previous cycle as today's kWh (312,4 kWh at midnight, 4,2 after the reset, reported as -308,2). Any other fall reads as **zero**. The picker accepts any `device_class=energy` sensor, so a `utility_meter` with `net_consumption` or a bidirectional meter can be wired, and it goes backwards whenever the site exports more than it draws. Past days drop that fall: `_recorder_deltas` ignores every negative `change`, since a sum-chain restart looks exactly like one (discussion #66). Today drops it too, so the day reads the same before and after midnight; it used to bill the signed delta, and `current_year_cost` stepped up overnight by the day's export. A net register is therefore **not supported**: wire consumption and injection as separate climbing sensors. Reading a fall as a reset instead billed the meter's **whole lifetime total** as a single day (a 12350 kWh register that had exported 4.5 kWh reported 12345.6 kWh, roughly 4300 EUR onto `current_year_cost`), so a sensor publishing no state class reads a fall as zero too.

### 7.2 Injection credit and regime math

Per-regime day math is documented at `ytd_cost.py`. For `compensation` the injection nets 1:1 against consumption per meter register (single, day / night, the Impact band, the night circuit), each register's net for the window is spread over the elapsed months by the RLP profile and priced at each month's rate (`_NetAllocation`; without the profile the metered slices are priced as they happen), and each register's YTD energy term is clamped at zero at the end (`ytd_cost.py`): surplus injection past consumption is forfeited by most Walloon suppliers, and the clamp happens once over the whole YTD so a day of over-injection can offset a later high-consumption day. For `injection` each side uses its own rate and the total can dip negative; the running `current_year_cost` value dipping day-over-day is why the sensor is `TOTAL`, not `TOTAL_INCREASING` (`sensor.py`). The pre-clamp energy term is exported to the `energy_ytd_raw_eur` attribute (via the optional `breakdown` out-dict `_compute_current_year_cost` fills on the live tick), alongside the YTD/today kWh totals and the fees floor, so a sensor resting on the compensation zero-floor (negative raw energy, value `= fees_ytd_eur`) can be told apart from a stalled meter input (a today kWh that never grows). The historical injection rate is chosen by `_historical_injection_rate` (`injection.py`), which mirrors the live priority (per-slot TOU, then `factor*spot+base`, then the monthly `current`) so the YTD credit and the live `injection_price` sensor never diverge.

### 7.3 Why YTD stays hourly for quarter-hourly contracts

`DynamicRates.quarter_hourly` keeps the *live* table on 15-minute slots, but the HA recorder only retains **hourly** long-term statistics (`providers/_rates.py`). So `_ytd_hourly_energy` aggregates consumption/injection to the clock hour and prices each hour at its hourly spot (`ytd_energy.py`). When intra-hour load correlates with intra-hour price this is a close approximation, not a bit-exact reconciliation with the live 15-minute sensor. This is a deliberate constraint, not a bug.

### 7.4 Contracts held earlier in the year

A household that changed supplier during the year records the switch from the
options menu (see [config-flow.md](config-flow.md)), which keeps the settings
it held as `{"until": <first day of the next contract>, "data": {...}}` in
`CONF_PREVIOUS_CONTRACTS`. `previous_periods` (`contract_periods.py`) turns the
records into the days each earlier contract covers inside the year-to-date
window, and `current_period_start` into the first day of the entry's own. A
contract left that billed its year from its own start date (its kept copy has
`CONF_YTD_FROM_CONTRACT_START`) starts its days there rather than on the
window's first day, and the days before it belong to no contract. The
tick prices its own contract from that day (`window_start_override`) and adds
the earlier contracts' share (`previous_costs`); `current_month_cost` does the
same for the running month, which only an earlier contract that ended in it
reaches into.

An earlier contract is priced with the same engine closed on the day before
the switch: `_compute_current_year_cost(..., window_start_override=start,
window_end=end)`, handed a `_QuoteEntry` holding its settings and the
coordinator as `runtime_data`, so the card is split against the household's
measured volume. `window_end` is resolved once beside the window start and
handed to every leg that took `today` as the window's last day: the fee walks
(`_walk_ytd_months`), the welcome credit, the daily and hourly kWh reads and
the spot-indexed feed-in credit. A closed window never reads the live meter:
the daily read only overrides the running day, and the hourly top-ups are
skipped when an end is passed. `today` stays the calendar's in the two places
that ask whether a month is still running (`_hour_spot`,
`_spp_injection_spot`). One window per contract is also what the Walloon rule
asks for: a change of supplier splits the compensation year and each part nets
its own injection (CWaPE CD-14d03, section 5.1.2).

The card each earlier contract stands on (`period_card`) is the one its
supplier publishes today, through `fetch_shared`; each month still bills on
its own archived card through the walk. A supplier that no longer publishes one
falls back to the newest card an archive kept inside the period, found by
asking `_snapshot_for_month` for each month with the entry's card as the
fallback and watching for a different object, and one with neither to the
entry's current card, flagged `stand_in`.

Pricing fetches the old supplier's cards, so the tick never does it. It runs
in the background once a day (`_schedule_previous_pricing`,
`_price_previous`), or on the next hourly tick while a contract could not be
priced (`_PREVIOUS_RETRY`: not on the tick its own refresh asks for, which
would price it again every few seconds),
after filling the year's day-ahead when an earlier contract settles on it, with
the ENTSO-E key the old contract's settings kept when the entry holds none, and
asks for a refresh when it lands. The result is kept as
`PricedPeriods` with `periods_key`, the days and settings it was priced for,
and served only for those. Until one lands, usually the minutes after a switch
is recorded, and while any contract fails to price, the year reads unknown rather than short by a whole contract,
which on the recorder would read as a large negative change and then the same
positive one. The spot and load-profile gates ask `periods_need_spots` and
`periods_need_rlp` of the registry, not of a card, because they decide before
anything has priced an old contract. The spot gate asks what the walk will do
with the old contract rather than its kind alone: most cards indexed on the
delivery month's mean are registered variable, and the walk re-prices them on
that mean whenever the old settings hold an ENTSO-E key
(`month_indexed_energy`, or a variable signing cohort), which the reload after
a switch leaves with no spots at all. The profile gate asks the same of those
cards, since whether one weights its mean on the load profile is printed on the
card rather than kept in the registry, and `_price_previous` loads the profile
in the entry's own blend before pricing, as it fills the spots, because the
first tick after a switch fetches it in the background. The solar profile is
decided on the fetched card instead, since no registry flag says a feed-in
settles on Belpex_SPP: the daily pricing (`load_profiles=True`) loads it for
such a card on the injection regime, as the backfill does for the same days,
and the compare dialog only reads what that run left behind.

The comparison pages price the household's own year the same way
(`with_previous_contracts`): the day's pricing while they quote the household
as it is, a fresh pricing under a what-if regime or DSO mode, which has to
reach the earlier contracts too. That holds on the page's one-rate model as
well (`compare_placeholders.py`): one rate times the window's kWh cannot say
what two contracts cost, so after a switch the own row there is priced on the
engine like the sensor, and only the quoted side stays on the one-rate model.
The backfill cuts its hours at each switch
(`_contract_segments`, `backfill_window.py`), builds a context per contract
and accrues the cost series across them as one running total
(`_accrue_cost`, `backfill_cost.py`), leaving out of the cost series the days
no contract supplied (`billed_only`) as the sensor does.

## 8. Injection taxonomy and the spot-gating invariant

Belgian residential injection is VAT-exempt, so `InjectionRates` values are never VAT-scaled (`providers/_rates.py`). `InjectionRates` (`providers/_rates.py`) can carry a monthly indicative (`current`), a formula (`factor`/`base`) that resolves either per hour or on a monthly mean depending on the `spp_indexed` / `month_indexed` flags, a per-slot TOU triplet (`peak`/`transition`/`offpeak`), and a guaranteed floor (`floor_at_zero`, or `minimum` for a card that promises more than non-negative). The coordinator distinguishes four shapes:

| Shape | Fields | Live price source | Example |
|-------|--------|-------------------|---------|
| (a) monthly-indicative only | `current` set, no usable `factor`/`base` for pricing | the printed `current` value, no spot | Ecofix Flexy, Engie/Luminus/Mega fixed and variable |
| (b) hourly `factor*spot+base` | `factor`+`base`, energy is dynamic | `factor*spot+base` at the current slot | Engie, Luminus, Mega, OCTA+, TotalEnergies dynamic |
| (c) spot-indexed on static energy | `factor`+`base`, `current is None`, energy NOT dynamic | `factor*spot+base`, but the energy path fetches no spot | Cociter Variable, Cociter Variable Trihoraire |
| (d) month-indexed formula | `current` + `factor`/`base` + `spp_indexed` or `month_indexed`, or the TOU triplet with its per-slot `factor_*`/`base_*` pairs | `factor*month_mean+base` for the DELIVERY month (per slot for the triplet), `current` or the printed triplet only while that mean is unpublished | DATS 24, EBEM Variabel/B@sic+, Eneco Fix/Flex/Flex One, energie.be, Energy Knights Essentia, EnergyVision fixed, Trevion Flex/LifePowr; Engie Empower Flextime per slot |
| (e) register pair | `current` + `peak`/`offpeak` with `bi_hourly` | `peak` or `offpeak` by the entry's meter registers on the region's day/night schedule (`is_offpeak`), `current` on a single-register meter | Trevion Groene Energie Vast |

Shape (d) is what several cards used to be read as shape (a). They print a
figure AND a formula, and say in their own footnotes that the figure is the
last published value of an index the contract settles monthly and
retroactively. Reading only the figure bills last month's rate every month.
The two flags say which monthly mean resolves the formula, and they are also
what keeps month coefficients out of the hourly path.

`_compute_injection_price` implements the live selection: per-slot TOU rate first, then the spot formula when the energy is dynamic OR `current is None`, otherwise the static `current`. When a formula needs a spot but none is available it returns `None` rather than fabricating a value. The per-slot core is factored into `_injection_price_for_slot(inj, energy, spot, when)`, which the scalar calls with the now-slot spot (resolved by `_now_slot_spot`) and which `_build_injection_hourly` reuses to price every today+tomorrow slot for the sensor's `today`/`tomorrow` arrays. Only a contract flagged by `_injection_varies_intraday` (spot-indexed or TOU) gets an array; a flat contract would just repeat its scalar. Both paths share the same guard, so the array can never flip a flat monthly-indicative credit into a spot-varying one. Note the subtlety: a contract that has both a monthly `current` and a `factor`/`base` (shape (a) with a formula, e.g. Ecofix Flexy, EBEM SPP0) uses the realized monthly `current`, not the spot, keeping the live sensor consistent with the YTD credit. When a contract sets `floor_at_zero` (the expert custom monthly-average mode), `_floor_injection` clamps the resolved rate at 0 in both the live and historical paths, per slot and per register as well, and the compare page's credit and the two band sensors (`_static_injection_bands`) floor each slot or register rate the same way before they average or publish it. A `SpotMonthlyRates` energy contract's mean-indexed injection is baked into a flat `current` for the tick by `_bake_monthly_injection` so it prices off the delivery month's mean, not the live hourly spot. The bake is skipped when `_injection_hourly_on_cohort` is true: a card that reached the monthly-mean path only through a signing-cohort re-price of its ENERGY leg keeps whatever index its injection carries. It is asked of today's card's own energy kind with the feed-in leg actually credited, the signed formula on a cohort, by the live tick and the compare page alike (`_injection_bakes_to_month_mean`, `injection.py`) and, hour by hour with that month's credited leg, by the year-to-date walk and the backfill. Cociter Tarif Variable is the case and its card is explicit about the split, note (7) "le prix ... est indexe mensuellement ... moyenne arithmetique ... (BELIX) durant le mois de fourniture" for consumption against note (9) "le prix de l'injection varie chaque heure". The cohort freezes the formula the customer signed on BOTH legs (issue #85), with the index that formula reads; what this skip turns on is the ENERGY leg moving onto a month mean while the feed-in formula keeps its hour. Baking it flattened the credit onto an index the contract never mentions, and since PV output peaks when the day-ahead price troughs, a flat mean systematically over-credits. A card that is ITSELF monthly-indexed (the custom monthly contract, the Mega groepsaankoop) indexes its injection on the month too and still bakes, which is why the snapshot's own energy kind decides rather than the effective one. A monthly-indexed card whose injection indexes on a DIFFERENT monthly parameter says so with `InjectionRates.spp_indexed`: energie.be Variabel and Energy Knights Essentia Online index consumption on Belpex_RLP and injection on the solar-weighted Belpex_SPP, which sat at 6,34 against 11,42 c€/kWh in July 2026. The flag makes `_spp_weighting_enabled` fetch the Synergrid profile for the entry with no user opt-in, and makes the fallback STRICT: with no weighted mean available the formula is not resolved at all (`_spp_injection_spot(strict=True)` returns `None`, the live bake is skipped) and the card's printed `current` is credited instead. Resolving it against the energy leg's mean would pay 6,05 c€/kWh where the contract owes 3,00, and would do so silently; `_INJECTION_SHAPE`'s `spp` shape pins all three parts so a regression fails in the live check.

The month bake asks one question, and it is about the FEED-IN leg: does this credit settle on a month? Not whether the energy does. A dynamic contract fetches its own spots through the energy path, which is what excluded it from `_injection_needs_month_spot`, and the gate used to borrow that predicate: a month-indexed credit on such a card went unbaked and the sensor showed the figure printed for the previous month while the year-to-date credited the month's own. `_injection_is_spot_formula` carries the mirror of the same rule, refusing BOTH month flags, because on a dynamic leg its branch fires on the energy kind alone.

When the custom monthly entry opts into **SPP-weighted** injection (`_spp_weighting_enabled`), the injection month-mean is the day-ahead prices weighted by the Synergrid solar production profile (`_spp_weighted_month_mean`) rather than the plain arithmetic mean, while energy keeps the plain mean. The profile is fetched by `synergrid.fetch_spp_weights` (`_ensure_spp_weights`, re-fetched monthly, kept in the installation-wide profile store) and used for both the live injection bake and the YTD credit (`_ytd_hourly_energy` threads the SPP month-mean into the injection line while energy stays on the flat mean). It uses the ex-ante (forecast) profile and falls back to the plain mean whenever the profile is unavailable.

### 8.1 The shape (c) invariant

`_injection_needs_spot` (`injection.py`) is the gate for shape (c): injection regime, `inj.current is None`, `inj.factor`/`inj.base` set, the energy is not `DynamicRates`, and the leg carries no MONTH index. That last clause is not decoration: the absent-`current` test reads "the card printed no rate to prefer", which is a tell for a per-slot formula and says nothing about the period the credit settles on, and a month-indexed card that stops printing its indicative has both. Claimed as shape (c) it lost the month bake this predicate gates, so the `injection_price` sensor reported nothing while the running bill credited the month formula. `_injection_replays_hourly_spot` carries the same exclusion, and the two move together. Because such a card never fetches ENTSO-E through the energy path, shape (c) needs an ENTSO-E spot fetched *specifically for the injection*, gated on `_injection_needs_spot` in **every** path or the credit silently drifts:

- Live spot fetch, softly (`coordinator_tick.py`): a spot failure must only drop the injection, never the energy tick.
- Historical spot backfill (`coordinator_tick.py`): the `or _injection_needs_spot(...)` clause triggers `_ensure_historical_spots` for these contracts too.
- YTD credit (`_ytd_spot_injection_credit`, `ytd_energy.py`): an isolated term that replays hourly spots for the injection side only, subtracted from the bill on the static per-day path, which is the one walk with no per-hour spot of its own. The hourly branches hold the spot cache and credit the same formula inside the walk, so adding this term there would credit it twice. Its own guard (`ytd_energy.py`) fires only for shape (c) as `_injection_replays_hourly_spot` defines it (`factor`/`base` set, not month-indexed, and either no printed `current` or the card flags it `slot_indexed`), with spots cached and an injection sensor wired, judged on each month's own card rather than on today's: the early return read the current card's shape, so a month whose archived card replays the spot was credited by neither walk once the newest card printed only an indicative. Each hour is priced off ITS OWN delivery month's card, resolved through the same `_month_snapshot_cache` the sibling walks and the backfill use: it used to take today's coefficients and apply them to every hour since 1 January, so a contract whose feed-in formula moved during the year was re-credited for the whole year at its newest terms while the backfill replayed each month's own. An hour whose month printed an indicative is skipped, because the walk this term is added to already credited that month off it.

The config-flow consequence: because shape (c) needs a key that the dynamic energy path would otherwise collect, `Contract.spot_indexed_injection` (`providers/_rates.py`) makes the config flow offer the API-key step on the injection regime for these static-energy contracts.

## 9. Error handling, backoff, and Repairs

The fail policy is "keep serving the cached snapshot, surface a Repairs issue". `_maybe_refresh_snapshot` catches every fetch exception (`coordinator_snapshot.py`), records `_last_error`, populates the shared negative cache with an incremented consecutive-failure count, and re-raises only non-`ExtractorError`/non-`TimeoutError` types (`base.py`); a bad card thus keeps the last good data alive.

Repairs issues, all keyed by `entry_id`:

| Issue | Raised by | When |
|-------|-----------|------|
| `snapshot_stale` | `_sync_stale_issue` | age > `SNAPSHOT_STALE_DAYS` (7 d), and the supplier has not left the market (`_supply_ended`): past `deprecated_until` the final card is stale for good, the deprecation card says so, and `_maybe_refresh_snapshot` no longer asks the supplier at all |
| `extractor_failed` | `_sync_extractor_issue(transient=False)` | parse error / 404 / non-PDF; on the first failure |
| `extractor_unreachable` | `_sync_extractor_issue(transient=True)` | network timeout / reset / 5xx / anti-bot 403; only after `_EXTRACTOR_ISSUE_THRESHOLD` consecutive failures |
| `extractor_unreadable` | `_sync_extractor_issue(unreadable=True)` | same, but the fetch raised `CardNotReadableError` (`providers/base.py`): the card downloaded fine and carries no text layer, so it names the custom-supplier workaround instead of asking for a GitHub issue |
| `extractor_unreadable_no_prices` | `_sync_extractor_issue(unreadable=True)` with `_snapshot is None` | the same unreadable card on an entry with nothing cached to serve: a brand-new entry, or one whose blob fell below `_DEGRADED_MIN_SCHEMA_VERSION`. Every sensor reads unavailable, so it names the workaround and says nothing about drift |
| `card_read_by_ocr` | `_sync_card_read_by_ocr_issue` | the archive's OCR reading of an unreadable card was adopted (`_serve_card_read_by_ocr`, `coordinator_snapshot.py`): the entry is priced, off a picture of the card. Replaces the two unreadable cards above, and clears the moment a card with a text layer lands, ours or a sibling's |
| `entsoe_auth_failed` | `_sync_entsoe_auth_issue` | ENTSO-E returns 401 for the API key |
| `supplier_deprecated` | `_sync_deprecated_supplier_issue` | the entry's supplier carries `deprecated_until` in the registry (`providers/base.py`) AND the successor has a contract in the entry's region |
| `supplier_deprecated_no_successor` | `_sync_deprecated_supplier_issue` | same, but the successor is unset, unknown to this build, or has no contract in the entry's region |
| `supplier_deprecated_ended` | `_sync_deprecated_supplier_issue` | same as `supplier_deprecated`, but the local date is past `deprecated_until`: the transfer has happened and this entry has stopped updating |
| `supplier_deprecated_ended_no_successor` | `_sync_deprecated_supplier_issue` | same, past the date, with no usable successor |
| `connection_fee_missing` | `_sync_connection_fee_issue` | the snapshot carries `TaxOverlay.region_connection_fee_unavailable`, i.e. a Walloon card that stopped printing the connection-fee row |
| `prosumer_tariff_missing` | `_sync_prosumer_gap_issue` | the entry is a Walloon compensation install (`_compensation_kva` above zero) and its DSO overlay carries no `prosumer_eur_per_kva_year`, i.e. a card that omits the "Tarif prosumer" column |
| `compensation_kva_missing` | `_sync_compensation_kva_issue` | the entry is on the Walloon compensation regime with no inverter capacity above zero (`compensation_lacks_kva`, `fees.py`), so no prosumer fee is billed on any path. The solar step refuses that combination; this names an entry saved before it did, and clears once a capacity is entered |
| `register_pair_incomplete` | `_sync_register_pair_issue` | the daily volume read (`_ensure_annual_volume`, `coordinator_snapshot.py`) found a day/night register that records nothing or stopped while its twin carries on (`MeasuredKwh.pair_fault`), on the consumption pair or on a wired injection pair; a register that merely started late is not named |

The first four are failure states and clear on a successful refresh, as do
`connection_fee_missing` and `prosumer_tariff_missing` once the supplier prints
the row or the column again.
`supplier_deprecated` is not: it is a lifecycle notice, evaluated first on every
tick (`coordinator_tick.py`) straight off the registry flag, and it clears only
when the entry is re-pointed at a supplier that has not
announced its exit. All four variants share one issue id, so an entry only ever
carries one of them; `_successor_for` decides whether a successor can be named
by checking it actually serves the entry's region, and `_supply_ended`
(`coordinator_issues.py`) picks the tense. That date comparison is the only
clock read in the deprecation path, and it is on the LOCAL date: the withdrawal
is a Belgian calendar event, so a UTC comparison flips a day late for CET/CEST
users. Past that date the extractor cards are suppressed too
(`coordinator_issues.py`); the supplier has stopped publishing, so a failing
fetch is the expected end state rather than a fault, and stacking a "could not
reach the supplier" card on top of this one would leave the user to work out
that the two describe a single event. `snapshot_stale` is deliberately NOT
suppressed: it states a true fact, that the prices being shown are old.
Prices are deliberately untouched while it is up -- a user
still being supplied must still be billed correctly for the months they are
supplied.

`_EXTRACTOR_ISSUE_THRESHOLD` is `2` (`coordinator_snapshot.py`): a lone transient CDN timeout does not raise the softer "unreachable" card, because a single failure almost always recovers on the next hourly tick and a false alarm wrongly tells the user the supplier changed its layout. `is_transient_fetch_error` (from `providers._pdf`) classifies the failure (`coordinator_snapshot.py`); actionable failures raise on the first occurrence, transient ones only after the threshold. The consecutive count rides the shared negative-cache row and resets to zero on the first success (`failed.pop`, `coordinator_snapshot.py`). The `extractor_failed`, `extractor_unreachable`, `extractor_unreadable` and `extractor_unreadable_no_prices` slots are mutually exclusive; raising any one clears the other three (`coordinator_issues.py`). A fetch that raises `CardNotReadableError` takes the third slot in place of the actionable one, because "the supplier changed its layout, open a GitHub issue" is advice nobody can act on when the card has no text layer at all. That signal is DERIVED from the download (`providers/_pdf.py`), not declared per supplier: the first version was a registry flag, which froze one month of observation into source and would have kept claiming a supplier was unreadable after it went back to publishing text, until someone shipped a release to clear it. Deriving it self-heals on the next fetch and covers any supplier that starts rasterizing. A transient network error still reports as transient.

Negative-cache TTLs: `_SHARED_FAILURE_TTL` is 5 minutes (`snapshot_store.py`, dedupes a burst of update ticks across siblings), `_MONTHLY_FAILURE_TTL` is 30 minutes (`snapshot_store.py`, for the per-month archive fetch: the repository's card archive first through `_archived_card_from_github`, then the supplier's `fetch_for_month` for a month the project's archive does not hold (`snapshot_months.py`), for the months `_card_archive_may_hold` allows: closed ones, and for a supplier with no archive of its own none before `CARD_ARCHIVE_FIRST_MONTH`). A transient failure of either is deliberately NOT written to `monthly_snapshot_cache` as a `None` (a cached `None` means "no archive has this month"); the separate failure marker (`snapshot_months.py`) prevents re-attempting every uncached month each tick while still letting a real recovery repopulate.

### 9.1 Forcing a refresh

`async_force_refresh` (`coordinator.py`) backs the `be_electricity_prices.refresh` service (`__init__.py`). It sets the one-shot `_force_refresh` flag (passed to `fetch_shared` as `force`, `snapshot_store.py`, so neither a sibling's row nor the entry's own is adopted), clears the spot cache, and pops the shared snapshot and negative-fetch rows so a sibling on the same tuple also re-fetches. It **also drops this tuple's per-month archive rows** via `_drop_monthly_rows` (`snapshot_store.py`): the YTD walk runs Jan 1 through today inclusive, so the current delivery month sits in that cache too, with no TTL. Without the drop, a supplier that re-issues the current month's card under the same month (Eneco publishes corrected volumes) went on being billed from the first card fetched for the life of the HA process, and this service — whose whole purpose is picking up a corrected card — could not clear it. It intentionally keeps `self._snapshot`/`_snapshot_fetched_at` so a transient failure during the forced refresh doesn't blank the entry. `reset_monthly_peak` (`coordinator_peak.py`), behind the diagnostic Reset-peak button, drops `_peak_kw` and persists immediately.

## 10. Persistence

`_save_persistent` (`coordinator_persist.py`) writes `entry_supplier`/`entry_contract`/`entry_region` (the frozen `_supplier_tuple`, not live `entry.data`), the peak, the serialized snapshot, the settled archived month cards, `historical_spots` pruned to the current YTD window, and `previous_contracts`, the day's pricing of the contracts held earlier in the year (section 7.4), which is restored outside the tuple gate because it names the periods it was priced for. Two guards prevent a slow tick from clobbering a reloaded entry's state:

- **Identity guard** (`coordinator_persist.py`): skip when `runtime_data` is a *different* coordinator (must not skip during first refresh, when it is `UNDEFINED`).
- **Tuple guard** (`coordinator_persist.py`): skip when live `entry.data` has drifted from `_supplier_tuple` (the OptionsFlow window where `entry.data` changed but `runtime_data` is still swapping).

A third check is about cost rather than correctness: the blob is rebuilt whole on every tick and is mostly slow-changing, so it is compared with the last one written and the save is skipped when nothing moved. Measured before adding it, an hourly entry rewrote 342 KB an hour and a quarter-hourly one about 1 MB, identical on 23 ticks out of 24, which is 24 MB a day per entry through HA's JSON encoder and onto the disk. The comparison is on the payload object, before `async_save` serialises it, so a quiet tick pays one dict comparison and skips the encode as well as the write. The remembered copy is only updated after a successful write, so a failed save is retried rather than assumed done.

`monthly_cards` holds the archived cards each past month is billed with, keyed `YYYY-MM`. Only SETTLED rows are written (`monthly_rows_to_store`, `snapshot_months.py`): the running month's card can still be corrected, and a row the extractor flagged `provisional` is waiting on the index the next card prints, so neither outlives the process. `_month_row_is_provisional` (`snapshot_store.py`) is the single rule for that. A **closed month that came back with no card** is written too, as an `_absent` marker carrying the instant it was asked: establishing that a supplier publishes nothing for a month costs the same download and parse a real card does (Frank Energie spends 24 s saying so about March 2026), and dropping the marker made every restart pay it again. It is not a permanent answer -- a supplier publishing in arrears turns "not out yet" into a real card days later -- so the restore honours it only while `_MONTHLY_PROVISIONAL_TTL` would have in memory, and never for the running month. The write is bounded to the months the year-to-date window covers **plus the signing month** when the entry has a contract start date (`_persisted_months`, `coordinator_persist.py`), so a blob holds at most a year of them at roughly 5 KB apiece. The signing month is not part of the walk and can be years back, but `_cohort_legs` resolves it on every tick to freeze the rate the customer signed for, and it does so inside setup because the live price table is built from it; one row on disk is what keeps that from being a card fetch on every restart. A deadline around that fetch was the alternative and is the wrong tool: a slow supplier would fail setup, and the retry rebuilds the coordinator and meets the same deadline, so the entry would never come up at all. `restore_monthly_rows` (`snapshot_months.py`) seeds them back behind the same tuple gate as the snapshot, skipping any month this process has already fetched, any row that no longer parses or was written under an older `_SNAPSHOT_SCHEMA_VERSION` (so a parser fix reaches these months on the next bump, exactly as it does the live card), and any row the clock no longer considers settled. A row the card archive still serves under an older schema never gets that far: `_archived_card_from_github` (`snapshot_months.py`) marks it `provisional`, so it bills until the archive's next run re-parses it but is re-asked daily and never written. Settled, it was persisted under the running schema, and an entry that upgraded before the archive's run kept the old parse until the next bump. The key is additive, so no `STORAGE_VERSION` bump is needed.

Serialization is `_snapshot_to_dict` (`snapshot_codec.py`) and `_snapshot_from_dict` (`snapshot_codec.py`), which stamp and check `_SNAPSHOT_SCHEMA_VERSION` as described in section 2.3. What is persisted is the card **as parsed**, not as priced: `_set_snapshot` keeps both, and the per-entry VAT and excise-band resolution is re-applied on load. Historical spots are pruned with a local-midnight Jan 1 anchor (`_prune_historical_spots`, `coordinator_spots.py`) so a Brussels restart in early January doesn't drop the first hour or two of YTD. The prune waits while a backfill runs (`_spot_prune_holds`, set by `backfill_range`): the backfill reads the same dict between turns of the loop, and a window in a past year needs exactly the hours the prune drops. On entry removal, `async_remove_entry` (`__init__.py`) deletes every Repairs issue id and removes the Store file so nothing outlives the entry; `test_repair_issue_kinds_match_the_declared_strings` pins that list against `strings.json` so a newly added issue cannot skip it.
