# Entities, diagnostics, services

This document covers the read-side of the integration: the sensor,
binary_sensor and button entities that surface `coordinator.data` to Home
Assistant, the diagnostics dump a bug reporter downloads, the four services
(`refresh`, `cheapest_window`, `most_expensive_window`, `backfill_statistics`),
and the `strings.json` translation catalog that names all of them. Everything
here reads the coordinator's `CoordinatorData` dataclass and never computes a
price itself; the arithmetic lives upstream in `pricing.compute_breakdown`. All
`EUR/kWh` figures shown below are illustrative and taken from source comments,
not real tariffs.

Related docs:

- [coordinator.md](coordinator.md) - where `CoordinatorData` and every data-dict key is produced.
- [pricing-model.md](pricing-model.md) - the `PriceBreakdown` (energy / network / taxes / all_in) the entities read.
- [config-flow.md](config-flow.md) - the config keys (`CONF_REGION`, `CONF_SOLAR_REGIME`, ...) that gate which entities are created.
- [data-sources.md](data-sources.md) - the recorder backfill that `backfill_statistics` drives.
- [architecture.md](architecture.md) - big-picture module map.

All entities share one device (`supplier_device_info(coordinator)`,
`coordinator.py`), set `_attr_has_entity_name = True`, and derive their
`unique_id` as `f"{coordinator.entry.entry_id}_{key}"`, so a config entry owns
exactly one device carrying every entity below. Names come from
`strings.json` under `entity.*` and are looked up by each description's
`translation_key` (which equals its `key`).

## Platform registration

`const.py:37` lists the three entity platforms HA forwards the entry to:

```python
PLATFORMS = ("sensor", "binary_sensor", "button")
```

Services are registered once per HA process in `async_setup` (not per entry);
see [Services](#services).

## Sensors (`sensor.py`)

### How a sensor is defined

Every sensor is one `BePriceSensor` (`sensor.py:522`) instance driven by a
frozen `BePriceSensorDescription` (`sensor.py:72`), which extends HA's
`SensorEntityDescription` with two pure callables:

```python
@dataclass(frozen=True, kw_only=True)
class BePriceSensorDescription(SensorEntityDescription):
    value_fn: Callable[[CoordinatorData], float | None]
    last_reset_fn: Callable[[], datetime] | None = None
```

`native_value` (`sensor.py:570`) calls `value_fn(coordinator.data)` and then
rounds to `suggested_display_precision + 2` decimals (or 6 when no precision is
set). The extra two decimals beyond what the UI shows exist to strip
float-representation noise (for example `0.35322099999999995`) that the recorder
would otherwise persist and chart, because `suggested_display_precision` only
affects the displayed string, not the stored `native_value`.

Most descriptions are built by the `_eur_per_kwh(key, value_fn)` helper
(`sensor.py:363`), which stamps `state_class=MEASUREMENT`,
`native_unit_of_measurement="EUR/kWh"` and `suggested_display_precision=4`.

### Which sensors exist for a given entry

`async_setup_entry` (`sensor.py:491`) assembles the entity list conditionally:

| Group | Source | Created when |
| --- | --- | --- |
| `SENSORS` (11 core price sensors) | `sensor.py:377` | always |
| `FEE_SENSORS` (3 fee/cost sensors) | `sensor.py:409` | always |
| `CAPACITY_SENSORS` (2) | `sensor.py:457` | `CONF_REGION == REGION_FLANDERS` |
| `PROSUMER_SENSORS` (1) | `sensor.py:394` | `solar_kva > 0` and `CONF_SOLAR_REGIME == SOLAR_REGIME_COMPENSATION` |
| `INJECTION_SENSORS` (1) | `sensor.py:405` | `CONF_SOLAR_REGIME == SOLAR_REGIME_INJECTION` |
| `ContractEndDateSensor` (1) | `sensor.py:631` | `CONF_CONTRACT_END_DATE` is set |

The capacity gate exists because the Flemish capacity tariff (introduced Jan
2023) is the only region that bills a monthly-peak term; outside Flanders
`_track_monthly_peak` forces the peak to 0 anyway (see the button section). The
two solar groups are mutually exclusive by regime: the Walloon compensation
regime ("compteur qui tourne a l'envers") yields a net-cost sensor, the
post-2024 injection tariff yields a per-kWh injection credit sensor.

### Sensor catalog

`unique_id suffix` is the description `key`; the full unique id is
`{entry_id}_{key}`. `device_class` is blank where none is set. Unit is
`EUR/kWh` unless noted. "Reads" is the `CoordinatorData` field the `value_fn`
pulls (all fields defined at `coordinator.py:738`).

| Name | key / suffix | device_class | state_class | unit | Reads (`CoordinatorData` field) |
| --- | --- | --- | --- | --- | --- |
| Current price | `current_price` | - | MEASUREMENT | EUR/kWh | current slot `PriceBreakdown.all_in` via `_current` |
| Next hour price | `next_hour_price` | - | MEASUREMENT | EUR/kWh | `PriceBreakdown.all_in` at now+1h via `_next_hour` |
| Today average | `today_average` | - | MEASUREMENT | EUR/kWh | mean of today's `all_in` (`_today_avg`) |
| Today minimum | `today_min` | - | MEASUREMENT | EUR/kWh | min of today's `all_in` |
| Today maximum | `today_max` | - | MEASUREMENT | EUR/kWh | max of today's `all_in` |
| Tomorrow average | `tomorrow_average` | - | MEASUREMENT | EUR/kWh | mean of tomorrow's `all_in` |
| Tomorrow minimum | `tomorrow_min` | - | MEASUREMENT | EUR/kWh | min of tomorrow's `all_in` |
| Tomorrow maximum | `tomorrow_max` | - | MEASUREMENT | EUR/kWh | max of tomorrow's `all_in` |
| Energy component | `energy_component` | - | MEASUREMENT | EUR/kWh | current slot `PriceBreakdown.energy` |
| Network component | `network_component` | - | MEASUREMENT | EUR/kWh | current slot `PriceBreakdown.network` |
| Taxes component | `taxes_component` | - | MEASUREMENT | EUR/kWh | current slot `PriceBreakdown.taxes` |
| Fixed fee per year | `fixed_fee_eur_per_year` | - | MEASUREMENT | EUR | `yearly_fixed_fee_eur` |
| Energy fund per month | `energy_fund_eur_per_month` | - | MEASUREMENT | EUR | `energy_fund_eur_per_month` |
| Current year cost | `current_year_cost` | MONETARY | TOTAL | EUR | `current_year_cost_eur` |
| Capacity cost | `capacity_cost` | - | MEASUREMENT | EUR | `capacity_cost_eur` (Flanders only); also `billed_peak_kw` / `months_counted` attributes |
| Monthly peak power | `monthly_peak_kw` | POWER | MEASUREMENT | kW | `monthly_peak_kw`, the running month as measured and NOT floored (Flanders only) |
| Prosumer cost | `prosumer_cost` | - | MEASUREMENT | EUR | `prosumer_cost_eur` (compensation regime) |
| Injection price | `injection_price` | - | MEASUREMENT | EUR/kWh | current slot of `injection_hourly`, else `injection_price_eur_per_kwh` (injection regime); also `today`/`tomorrow` arrays when the injection varies intra-day |
| Contract end date | `contract_end_date` | timestamp | - | - | `entry.data[CONF_CONTRACT_END_DATE]` (a config value, not `CoordinatorData`; standalone `ContractEndDateSensor`) |

### Current-slot selection and the nearest-slot guard

`_current_slot_value` (`sensor.py:79`) looks a per-slot table up at
`slot_start(utcnow, resolution)`. On an exact miss it falls back to the
temporally nearest slot but only within one billing slot of "now": `max_gap` is
3600 s on an hourly contract and 900 s on a quarter-hourly one
(`sensor.py:104`). This bound stops a stale spot cache from surfacing
yesterday's last slot as "current"; a fixed 1 h window used to let a
quarter-hourly sensor present an up-to-45-min-stale slot as current. The 1 h
hourly window also absorbs the DST seam.

Two sensors read the clock through it: `_current` (`sensor.py:110`) over
`data.hourly` for the price sensors, and `_current_injection`
(`sensor.py:114`) over `data.injection_hourly` for `injection_price`. Reading
the clock at state time rather than at refresh time is what keeps them on the
slot the user is billed for, since the coordinator's own tick is a plain
60-minute interval anchored on setup (`__init__.py:181`). `injection_price`
used to publish a scalar resolved at that tick and so lagged the boundary by
however far the tick had drifted, which is what issue #44 reported on Engie
Empower Flextime. A slot the coordinator could not price (a hole in the
day-ahead curve) is covered by the same nearest-slot rule the price sensors
use, so the state shows an adjacent slot's rate; the tick's scalar survives
only as the last resort, for the flat contracts that emit no array at all and
for a table with nothing inside the window.

`_next_hour` (`sensor.py:135`) targets `slot_start(now) + 1h`. On a 15-minute
contract that deliberately stays the same quarter one hour later, so the sensor
keeps its "next hour" meaning rather than becoming "next 15 minutes". If that
exact slot is absent the sensor is `None` (no nearest-slot fallback).

The today/tomorrow scalar sensors (`_bucket`, `sensor.py:145`) reduce over every
slot whose local date matches, so on a quarter-hourly contract they operate at
native 15-minute resolution.

The three tomorrow sensors go through `_tomorrow_bucket` (`sensor.py:207`),
which returns `None` unless `_has_tomorrow(data)` holds. Reusing the binary
sensor's own predicate rather than repeating its `snapshot_valid_until` check
makes the invariant exact: a `tomorrow_*` sensor has a value precisely when
`tomorrow_prices_available` is on. They used to skip that check, so on the last
day covered by a monthly card the binary sensor correctly went off while the
three scalars reported the forward-filled extrapolation of a card that no longer
applies, and the two entities contradicted each other for a whole day every
month. Only the tomorrow side is gated; an expired card still describes today
better than nothing, and a snapshot stale enough to matter raises its own repair
issue.

### extra_state_attributes

`current_price` always carries extra attributes, and `injection_price` carries
`today`/`tomorrow` arrays when its injection varies intra-day (`sensor.py:600`);
every other sensor returns `{}`.

#### `current_price`

The payload:

| Attribute | Source | Meaning |
| --- | --- | --- |
| `snapshot_publication` | `data.snapshot_publication` | supplier card's publication label |
| `snapshot_age_hours` | `round(data.snapshot_age_hours, 2)` | hours since the snapshot was fetched |
| `snapshot_stale` | `data.snapshot_stale` | true past the staleness threshold |
| `last_error` | `data.last_error` | last fetch/parse error string, or empty. A fetch failure always names the exception, so a CDN timeout reads `network error fetching <url>: TimeoutError` |
| `cheapest_4h_today` | `_today_ranked(data, 4)[0]` | 4 cheapest today-hours, chronological |
| `most_expensive_4h_today` | `_today_ranked(data, 4)[1]` | 4 dearest today-hours, chronological |
| `today` | `_split_today_tomorrow(data)[0]` | per-hour breakdown rows for today |
| `tomorrow` | `_split_today_tomorrow(data)[1]` | per-hour breakdown rows for tomorrow |

`today` / `tomorrow` rows are `{start, energy, network, taxes, all_in}` (each
rounded to 6 decimals, `pricing.py:400`). `cheapest_4h_today` /
`most_expensive_4h_today` rows are `{start, price}` (`sensor.py:276`).

Quarter-hourly vs hourly payloads: the `today`, `tomorrow`, `cheapest_4h_today`
and `most_expensive_4h_today` attributes are always hourly. `_hourly_view`
(`sensor.py:175`) returns `data.hourly` unchanged for an hourly contract but for
a quarter-hourly contract averages each hour's four slots into one breakdown.
A full 15-minute curve (~192 rows) would exceed HA's 16 KB per-state-attribute
recorder limit. Only these list attributes are downsampled; the scalar
today/tomorrow min/max/avg sensors keep native resolution.

`_today_ranked` (`sensor.py:242`) guarantees the cheapest and dearest lists are
disjoint (cheapest take their share first) and breaks price ties on the hour so
the result is deterministic across reloads. Gotcha for automation authors: on a
flat tariff where every hour rounds to the same all-in price the tie-break makes
"cheapest" simply the first N hours and "most expensive" the last N; the source
comment (`sensor.py:255`) says to treat the output as undefined when prices do
not actually vary across the day.

#### `injection_price`

When the injection price varies across the day, `injection_price` also carries
`today` and `tomorrow` arrays of `{start, injection}` rows (each rounded to 6
decimals) so a battery force-export automation can rank the day's injection
hours ahead of time (`_split_injection_today_tomorrow`). The coordinator fills
`data.injection_hourly` only for contracts whose injection actually varies:
every dynamic contract and Cociter Variable (spot-indexed `factor*spot+base`),
plus Engie Empower Flextime (a fixed TOU schedule). A flat or monthly-indexed
contract emits no array, so both lists come back empty and the sensor returns
`{}`. The same quarter->hour downsampling as `current_price` applies through
`_injection_hourly_view`, and `tomorrow` fills in once the day-ahead publishes
(~13:00 CET), like the consumption array.

`data.injection_hourly` is also where the sensor's own state comes from for
those contracts (`_current_injection`), read at native resolution rather than
through the downsampled `_injection_hourly_view`. On an hourly contract that
normally makes the state the `today` row for the current hour; on a
quarter-hourly one the state is the live quarter while the rows stay hourly
means, so the two differ by design. They also differ on an hour with no row at
all, where the nearest-slot guard substitutes a neighbour. What no longer happens is the state sitting
on a slot the clock has already left: it used to replay the coordinator tick's
scalar while the array had moved on (issue #44).

### Unrecorded attributes

`BePriceSensor._unrecorded_attributes` (`sensor.py:536`) excludes `today`,
`tomorrow`, `cheapest_4h_today` and `most_expensive_4h_today` from the recorder.
They change every hour and are live display helpers, not history, so keeping
them out of state-attribute storage stops long-term-database bloat.

### `current_year_cost`: state class and last_reset

`current_year_cost` (`sensor.py:432`) is the only sensor with a non-trivial
statistics setup, documented in its source comment:

- `device_class=MONETARY` so HA's Energy dashboard auto-suggests it in the
  "Cost" picker.
- `state_class=TOTAL` (not `TOTAL_INCREASING`): under the compensation regime a
  heavy-injection day can lower the running total day-over-day, which
  `TOTAL_INCREASING` forbids.
- `last_reset` (`sensor.py:565`) is pinned to Jan 1 00:00 local via
  `last_reset_fn`, so long-term statistics bucket each calendar year separately.

The value is always numeric: missing meter inputs collapse to the fees-only
floor, so the sensor never goes `unknown`.

### `monthly_peak_kw`: why MEASUREMENT

`monthly_peak_kw` (`sensor.py:471`) must use `state_class=MEASUREMENT` because
that is the only class HA accepts under the `POWER` device class
(`DEVICE_CLASS_STATE_CLASSES[POWER] == {MEASUREMENT}`); `TOTAL` would log a
"state class is impossible" warning on setup. The statistics graph defaults to
mean aggregation, so the source comment directs users to the developer-tools
statistics view's per-hour MAX to read the true running monthly peak.

## Binary sensor (`binary_sensor.py`)

One binary sensor per entry: `TomorrowPricesAvailable`
(`binary_sensor.py:86`), key `tomorrow_prices_available`, unique id
`{entry_id}_tomorrow_prices_available`.

Its truth value is `_has_tomorrow(data)` (`binary_sensor.py:45`), which is ON
only when both gates hold:

1. The price table contains at least one slot whose local date is tomorrow
   (`any(as_local(h).date() == tomorrow for h in data.hourly)`). For dynamic
   contracts this happens only after ENTSO-E publishes the next-day curve; for
   fixed / variable / TOU contracts the coordinator forward-fills 48 hours so
   this is normally already true. "Normally" because those 48 hours are
   anchored at the local midnight of the tick that built them: crossing
   midnight used to fail this gate, and the `tomorrow_*` scalar sensors along
   with it, until the next tick landed. A local-midnight refresh now re-anchors
   the table on the new day (see coordinator.md 5.1.1).
2. The snapshot's published validity covers tomorrow:
   `data.snapshot_valid_until is None or tomorrow <= data.snapshot_valid_until`.

```python
if not data.hourly:
    return False
tomorrow = dt_util.now().date() + timedelta(days=1)
if data.snapshot_valid_until is not None and tomorrow > data.snapshot_valid_until:
    return False
return any(dt_util.as_local(h).date() == tomorrow for h in data.hourly)
```

Gate 2 is the historical fix for monthly variable cards (Eneco, Mega, ...): at
month-end the previously extrapolated "tomorrow" hours are no longer billable
because the supplier has not yet published the new month's rates, so the sensor
must not claim they are available. When the extractor could not parse a validity
end (`valid_until is None`) gate 2 is skipped and the price table alone decides,
tying this sensor directly to `SupplierSnapshot.valid_until`.

## Button (`button.py`)

One button, and only in Flanders: `ResetMonthlyPeakButton`
(`button.py:65`), key `reset_monthly_peak`, `entity_category=DIAGNOSTIC`,
unique id `{entry_id}_reset_monthly_peak`.

`async_setup_entry` (`button.py:47`) returns early unless
`CONF_REGION == REGION_FLANDERS`, because outside Flanders the capacity tariff
is not billed and `_track_monthly_peak` forces `_peak_kw` to 0 every tick, so a
reset would do nothing.

Pressing it calls `await self.coordinator.reset_monthly_peak()`
(`button.py:76`), which drops the persisted monthly peak so the next coordinator
tick rebuilds it from the live peak source. This is the manual escape hatch for
a spurious peak spike that would otherwise inflate `capacity_cost` for the rest
of the month.

## Services

Registered once in `async_setup` (`__init__.py:102`), so they exist even before
any entry finishes loading. Names and field descriptions are declared in
`services.yaml` and localized under `services.*` in `strings.json`.

| Service | Handler | Response mode | Targets an entry? |
| --- | --- | --- | --- |
| `refresh` | `_async_refresh_service` (`__init__.py:344`) | none | no, hits every loaded entry |
| `cheapest_window` | `_async_cheapest_window_service` (`__init__.py:564`) | `ONLY` | optional `entry_id` |
| `most_expensive_window` | `_async_most_expensive_window_service` (`__init__.py:572`) | `ONLY` | optional `entry_id` |
| `backfill_statistics` | `_async_backfill_service` (`__init__.py:580`) | `OPTIONAL` | optional `entry_id` |

### `refresh`

No fields. Iterates `async_loaded_entries(DOMAIN)` and calls
`coordinator.async_force_refresh()` on each, skipping any entry whose
`runtime_data` is still the `UNDEFINED` sentinel mid-reload
(`__init__.py:352`). It drops the cached supplier snapshot and the ENTSO-E spot
cache and re-fetches both immediately, clearing a transient fetch error without
waiting for the next hourly tick.

### `cheapest_window` and `most_expensive_window`

Same shape; one minimizes the window average, the other maximizes. Fields
(`services.yaml:17`, `services.yaml:116`):

| Field | Required | Selector | Meaning |
| --- | --- | --- | --- |
| `duration_hours` | yes | number 1..48, step 1, unit h | window length in whole hours |
| `entry_id` | no | `config_entry` (integration `be_electricity_prices`) | target entry; defaults to the first loaded entry |
| `earliest_start` | no | datetime | earliest window start; defaults to now |
| `latest_end` | no | datetime | latest window end; defaults to the end of the cached table |

Both call `_resolve_window_inputs` (`__init__.py:510`) then `_find_window`
(`__init__.py:357`). Key behaviors:

- `duration_hours` is rounded half-up and scaled to the table's slot grid:
  `duration_slots = int(duration_hours + 0.5) * slots_per_hour(resolution)`
  (`__init__.py:534`). A 2-hour window is 2 slots on an hourly table, 8 on a
  quarter-hourly one, so on a 15-minute (Engie Dynamic) contract the window can
  start on any quarter-hour boundary.
- `earliest_start` is truncated down to its slot boundary
  (`slot_start`, `__init__.py:385`), so 14:30 still considers the 14:00 slot
  (14:30 on a 15-minute contract). A naive datetime from YAML is interpreted in
  the HA time zone (typically Europe/Brussels), not the host's tz
  (`_to_utc`, `__init__.py:547`).
- `latest_end` filters out any slot whose end (`slot + width`) falls after it.
- Only strictly time-contiguous runs are considered: a run must span exactly
  `delta * (duration_slots - 1)` so a gap ENTSO-E omitted cannot let the window
  silently drop an interior hour from its average (`__init__.py:409`).

Return value (`ServiceResponse`, `__init__.py:446`):

```python
{
  "start": "<local ISO 8601>",
  "end": "<local ISO 8601>",
  "duration_hours": <int>,
  "resolution": "PT60M" | "PT15M",
  "average_eur_per_kwh": <float, 6 dp>,   # e.g. 0.28 illustrative
  "hours": [ { "hour": "<local ISO>", "all_in": <float, 6 dp> }, ... ]
}
```

`resolution` is exposed so the caller can tell that each `hours` row is a
quarter-hour rather than an hour on a 15-minute contract. When too few slots
match, the handler raises `ServiceValidationError` with translation_key
`not_enough_hours` (`__init__.py:390`), or, when slots exist but none form a
contiguous run of the needed length, reports the longest available contiguous
run in the same error (`__init__.py:433`).

### `backfill_statistics`

Populates the recorder's long-term statistics for this entry's price sensors and
`current_year_cost` over a date range, so the Energy dashboard can show history
predating the entry's first live tick. Fields (`services.yaml:65`):

| Field | Required | Selector | Default |
| --- | --- | --- | --- |
| `entry_id` | no | `config_entry` | first loaded entry |
| `start` | no | datetime | Jan 1 00:00 of the current local year |
| `end` | no | datetime (exclusive) | the current hour |
| `clear` | no | boolean | false |

The handler `_async_backfill_service` (`__init__.py:580`) resolves the target
coordinator, then raises `ServiceValidationError` translation_key
`snapshot_not_loaded` if `coordinator._snapshot is None`, before delegating to
`backfill_range` (see [data-sources.md](data-sources.md)). It returns
(`backfill.py:855`):

```python
{ "rows_written": <int>, "sensors": { "<statistic_id>": <int>, ... },
  "range": ["<start UTC ISO>", "<end UTC ISO>"] }
```

`end` defaults to the current hour so the in-progress hour the live coordinator
is about to write itself stays untouched. Re-runs are safe: rows are upserted on
`(statistic_id, hour)`. `clear=true` is destructive: it deletes the ENTIRE
target series (not just the requested range) and then repopulates only the
requested range, so a window starting after Jan 1 of the end year is rejected
when `clear` is on. Use it only for a full-year re-run after a tariff card
changed; for a narrower window leave `clear` off and rely on the upsert.

### Service exceptions

All handlers raise localized `ServiceValidationError`s keyed under
`exceptions.*` in `strings.json` (`strings.json:514`):

| translation_key | Raised when |
| --- | --- |
| `not_enough_hours` | fewer matching (or contiguous) slots than requested |
| `duration_too_small` | `duration_hours < 1` |
| `no_loaded_entry` | no loaded entry and no `entry_id` given |
| `no_loaded_entry_with_id` | given `entry_id` matches no loaded entry |
| `entry_reloading` | target entry is mid-reload (`runtime_data` not a coordinator) |
| `price_table_empty` | `data.hourly` is empty |
| `snapshot_not_loaded` | backfill called before the first snapshot loaded |

## Diagnostics (`diagnostics.py`)

`async_get_config_entry_diagnostics` (`diagnostics.py:95`) returns a single
dict a contributor downloads via "Download diagnostics" on the entry. If the
entry is mid-reload (`runtime_data` is HA's `UNDEFINED` singleton, detected by
type name to avoid importing a HA-private symbol) it returns
`{"status": "coordinator_not_ready"}` instead of raising (`diagnostics.py:106`).

Top-level dump keys:

| Key | Contents |
| --- | --- |
| `entry.title` | entry title |
| `entry.data` / `entry.options` | config, with `CONF_API_KEY` redacted via `async_redact_data(..., TO_REDACT)` |
| `coordinator` | live snapshot metadata and the full hourly price table (see below) |
| `consumption.rolling_year_kwh` / `.ytd_kwh` | recorder-summed consumption over 365 days and year-to-date |
| `injection.rolling_year_kwh` / `.ytd_kwh` | same for injection |
| `monthly_snapshot_labels` | `{ "YYYY-MM": publication_label or null }` for this (supplier, contract, region) |
| `shared_failure` | sibling-coordinator negative-fetch marker, or null |

The `coordinator` block (`diagnostics.py:158`) mirrors the current-price
attributes plus every scalar `CoordinatorData` field:
`snapshot_publication`, `snapshot_age_hours`, `snapshot_stale`,
`snapshot_valid_until`, `last_error`, `monthly_peak_kw`, `monthly_peak_month`,
`capacity_cost_eur`, `prosumer_cost_eur`, `yearly_fixed_fee_eur`,
`energy_fund_eur_per_month`, `injection_price_eur_per_kwh`,
`current_year_cost_eur`, and `hourly` (every slot as
`{start, energy, network, taxes, all_in}` rounded to 6 dp).

It also carries `injection_price_current_slot`, which is not a `CoordinatorData`
field but the value the `injection_price` entity is showing at dump time. The two
injection numbers can differ once the dump falls in a later slot than the tick
(they still match when both slots price the same, as two consecutive off-peak
hours do), so reporting both keeps a triage dump comparable against the entity
the user is looking at.

### Redaction

`TO_REDACT = {CONF_API_KEY}` (`diagnostics.py:54`). `async_redact_data` masks
only known config keys, so free-text error fields (`last_error`,
`shared_failure.error`) get a second scrub via `_scrub_secret`
(`diagnostics.py:57`), which replaces the API key literal anywhere it appears
with `**REDACTED**`. This is defence-in-depth: an ENTSO-E transport error string
can, in narrow cases, embed the request URL and thus the `securityToken`.

### How a contributor uses the dump to debug a mis-parse

1. Read `coordinator.snapshot_publication` and `snapshot_valid_until` to confirm
   the extractor picked the expected card and validity period.
2. Read `coordinator.last_error`: empty means the extractor parsed cleanly; a
   non-empty string with a served price table means stale cached data.
3. Scan `coordinator.hourly` for the wrong `energy` / `network` / `taxes` split,
   which localizes a mis-parse to a pricing component.
4. Check `monthly_snapshot_labels` to confirm the right cards landed for past
   months (relevant to the YTD `current_year_cost` path).
5. `consumption` / `injection` roll-ups show whether the user's kWh sensors are
   wired: `null` means no sensor configured for that side, `0.0` means a wired
   sensor that genuinely reads zero (`_kwh_window`, `diagnostics.py:71`), which
   tells apart an unconfigured sensor from a zero-reading one.
6. `shared_failure` (`diagnostics.py:141`) shows whether sibling coordinators
   for the same (supplier, contract, region) backed off, with the scrubbed error
   and consecutive-failure count, without the reporter having to grep logs.

## Internationalization (`strings.json` and translations)

`strings.json` is the source of truth for every entity name, service name and
field, config/options step, selector option, exception message and repair-issue
text. `translations/en.json` mirrors it, and `translations/{de,fr,nl}.json`
mirror `en.json` (all four exist under `translations/`). When adding or
renaming an entity, service field or selector option, edit `strings.json` and
propagate the same keys to all four translation files.

Top-level keys in `strings.json`:

| Key | Contents |
| --- | --- |
| `config` | config-flow steps, `abort`, `error` (see [config-flow.md](config-flow.md)) |
| `options` | options/compare flow steps |
| `selector` | option labels for `region`, `capacity_mode`, `meter`, `dso_tariff_mode`, `connection_kva_tier`, `solar_regime` |
| `services` | names and field descriptions for the four services |
| `exceptions` | `ServiceValidationError` messages |
| `issues` | Repairs cards: `snapshot_stale`, `extractor_failed`, `extractor_unreachable`, `entsoe_auth_failed`, `supplier_deprecated`, `supplier_deprecated_no_successor`, `exclusive_night_rate_missing`, `impact_rates_missing` |
| `entity` | entity names under `sensor.*`, `binary_sensor.*`, `button.*` |

Entity names are resolved by `translation_key`, which each description sets equal
to its `key`, so a new sensor `key` must have a matching entry under
`entity.sensor.<key>.name` (`strings.json:564`) or HA falls back to the raw key.
The `entity.sensor` block lists all eighteen possible sensors even though a given
entry only instantiates the subset its region and solar regime allow.
