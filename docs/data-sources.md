# Data sources: ENTSO-E and backfill

This document covers the out-of-band data sources the integration reads
besides the supplier tariff cards: the ENTSO-E day-ahead spot client
(`api.py`), which supplies the `spot` term that dynamic and spot-indexed
contracts multiply into their formula; the recorder backfill (`backfill.py`),
which writes historical price and cost statistics into Home Assistant's
long-term-statistics store so the Energy dashboard and Statistics card can show
history that predates the entry's first live tick; and the Synergrid solar
production profile (`synergrid.py`, Part 3), for the optional SPP-weighted
custom injection. The first two feed the same `pricing.compute_breakdown` engine
the live coordinator uses, so a backfilled past hour is priced exactly as the
live tick would have priced it at the time.

Related documents:

- [coordinator.md](coordinator.md): the refresh lifecycle that calls the spot
  client every hour and owns the spot caches this document references.
- [pricing-model.md](pricing-model.md): how `compute_breakdown` consumes the
  `spot` value and the injection rate.
- [provider-framework.md](provider-framework.md): `SupplierSnapshot`,
  `DynamicRates`, and `fetch_for_month` (the per-month archive path backfill
  reuses).
- [entities.md](entities.md): the price and cost sensors whose entity ids become
  the statistic ids backfill writes to, and the `backfill_statistics` service.
- [architecture.md](architecture.md): where these two modules sit in the module
  map.

## Part 1: the ENTSO-E day-ahead client (`api.py`)

### Why it exists

Dynamic contracts price energy as `factor * spot + base` against the ENTSO-E
Belgian day-ahead (SDAC) spot curve, and a handful of static contracts index
only their injection credit to the same curve (see the spot-indexed injection
invariant in [pricing-model.md](pricing-model.md)). `api.py` is the single
place that turns the ENTSO-E REST endpoint into a `dict[datetime, float]` of
slot-start (UTC) to EUR/kWh. It holds no caching, no scheduling, and no
knowledge of contracts: the coordinator owns all of that and calls this client.

### Endpoint, domain, and auth

| Constant | Value | Source |
| --- | --- | --- |
| `ENTSOE_BASE_URL` | `https://web-api.tp.entsoe.eu/api` | `const.py:299` |
| `ENTSOE_BE_DOMAIN` | `10YBE----------2` (BE bidding zone EIC) | `const.py:300` |

The client is constructed with the user's ENTSO-E API key and Home Assistant's
shared `aiohttp` session (`api.py:69`). The key is passed on every request as
the `securityToken` query parameter (`api.py:107`). There is no separate login
step; ENTSO-E authenticates per request.

### The query it builds

`fetch_day_ahead(period_start, period_end, *, quarter_hourly=False)` builds a
GET against `ENTSOE_BASE_URL` with these parameters (`api.py:101`):

```
documentType = A44          # day-ahead prices publication
in_Domain    = 10YBE----------2
out_Domain   = 10YBE----------2   # in == out for a price document
periodStart  = YYYYMMDDhhmm  (UTC)
periodEnd    = YYYYMMDDhhmm  (UTC)
securityToken = <api key>
```

`_fmt` (`api.py:342`) converts the caller's datetimes to UTC and formats them as
`%Y%m%d%H%M`, the compact stamp ENTSO-E expects. The request carries a 30-second
total timeout (`api.py:111`).

### Response handling and error modes

```
resp.status == 401          -> EntsoeAuthError("ENTSO-E rejected the API key")
resp.status >= 400          -> EntsoeError("ENTSO-E HTTP <status>: <body[:200]>")  (redacted)
aiohttp.ClientError         -> EntsoeError(str(err))  (redacted)
TimeoutError                -> EntsoeError(str(err))  (redacted)
```

Two exception types are defined (`api.py:58`, `api.py:62`):

- `EntsoeAuthError`: the key was rejected or exhausted. The coordinator raises
  the "rotate your token" Repairs card on this.
- `EntsoeError`: any transport or parsing failure. The coordinator keeps serving
  cached spots on this rather than tearing the tick down.

Two subtleties are load-bearing:

- `TimeoutError` is caught explicitly alongside `aiohttp.ClientError`
  (`api.py:121`). On Python 3.11+ the `aiohttp.ClientTimeout` fires
  `asyncio.TimeoutError`, which is no longer an `aiohttp.ClientError`; without
  the second alternative a slow ENTSO-E response would bubble a bare
  `TimeoutError` through the config wizard and the coordinator's error
  categorisation.
- Credential redaction. `aiohttp` client errors stringify with the full request
  URL, which carries `securityToken=<api_key>`, and that message reaches
  user-visible surfaces (the `current_price` `last_error` attribute, the
  `snapshot_stale` Repairs card, the HA log). `_redact` (`api.py:73`) replaces
  the literal key and applies `_TOKEN_RE` (`api.py:55`) to scrub the token from
  any URL text before the error is raised.

### Why `defusedxml`

XML parsing goes through `defusedxml.ElementTree` rather than the stdlib
`xml.etree`, in BOTH `api.py` (`api.py:44`) and `synergrid.py`, which parses a
remote workbook. `defusedxml>=0.7` is declared in `manifest.json` requirements.

Be precise about what this buys, because it is easy to overstate: the stdlib
parser already refuses an EXTERNAL entity, raising `ParseError` rather than
fetching the URL, so the classic XXE file-read is not the exposure. What the
stdlib DOES do is expand nested INTERNAL entities, which is a memory DoS on a
document we do not control. `defusedxml` refuses the DTD outright.
`test_workbook_xml_entity_expansion_is_refused` pins that distinction: on the
stdlib parser the payload expands and parsing continues, so a test written
against an external entity would have passed either way and proved nothing.
`defusedxml` rejects hostile constructs with `DefusedXmlException`, which is not
a `ParseError` subclass, so `parse_day_ahead_xml` catches it separately and wraps
it as `EntsoeError` (`api.py:62`) so a hostile payload surfaces as a categorised
error instead of an unhandled exception out of the coordinator tick.

### The SPP download never touches the loop

`synergrid._download` streams a ~52 MB workbook to a temp file. Every filesystem
call on that path goes through `asyncio.to_thread`: creating the temp file,
writing, closing (which flushes) and unlinking. Chunks accumulate into a 4 MB
buffer first, so the offload happens roughly a dozen times rather than once per
64 KB network read.

This matters on the SD-card installs Home Assistant is commonly deployed to: once
the kernel starts throttling dirty pages a write can block long enough for HA to
log a blocking-call warning and for every other integration's callbacks to stall
behind it.

### Parsing is offloaded to a thread

After the HTTP body is read, parsing runs under `asyncio.to_thread`
(`api.py:133`). The A44 document is small today (roughly 100 KB hourly, larger
under PT15M), but offloading guarantees XML parsing can never stall HA's event
loop during a coordinator tick.

### The Acknowledgement trap (HTTP 200 with no data)

ENTSO-E answers a rejected or quota-exhausted token with HTTP 200 and an
`Acknowledgement_MarketDocument` (no `TimeSeries`), not a 401. Returning `{}`
here would silently blank the dynamic price table with no Repairs guidance. The
runtime always requests a window that includes today, and the BE zone always
publishes today's curve, so a document carrying zero matching data really means
the request was refused. `parse_day_ahead_xml` detects the acknowledgement root
(`api.py:180`) and raises `EntsoeAuthError` with a best-effort reason extracted
from the document's `Reason` block by `_ack_reason` (`api.py:325`).

### Resolution handling: PT60M vs PT15M and aggregation

ENTSO-E publishes the Belgian curve at 15-minute granularity since the SDAC
15-minute MTU go-live (2025-10-01; see the note at `const.py:247`). The parser
handles three resolutions via `_resolution_to_timedelta` (`api.py:364`):

| Token | Step |
| --- | --- |
| `PT60M` | 1 hour |
| `PT15M` | 15 minutes |
| `PT30M` | 30 minutes |

Any other resolution (for example PT5M) is skipped for that `TimeSeries`
rather than aborting the whole document, because other series in the same
publication may still be usable (`api.py:203`).

Points are accumulated into `(sum, count)` buckets keyed first by the resolution
step in seconds, then by slot key (`api.py:196`). Bucketing per resolution is
critical: in 15-minute day-ahead zones ENTSO-E returns both a PT60M and a PT15M
series for the same delivery period, and blending "1 hourly point + 4 quarter
points" into one unweighted mean would mis-price every hour (`api.py:189`).
Within a single resolution, duplicate points still average, which is the correct
handling of a corrected re-publication.

The slot key depends on the caller's `quarter_hourly` flag (`api.py:258`):

- `quarter_hourly=False` (default): each point is folded into its UTC
  hour-start (`replace(minute=0, second=0, microsecond=0)`). Sub-hourly points
  in the same hour are averaged. Most consumers (YTD billing, hourly-billed
  suppliers) assume hourly keys.
- `quarter_hourly=True`: each point is keyed by its own native start instant.
  Used for suppliers that bill per quarter-hour (Engie Dynamic). A PT60M source
  still yields hourly keys either way.

Final assembly prefers the native resolution for the requested grid
(`api.py:305`): in hourly mode it iterates resolutions largest-step-first and
uses `dict.setdefault`, so the hourly product wins and a finer series only fills
keys the hourly one does not cover; in quarter mode it iterates smallest-step
first. Overlapping series therefore never blend into one key.

### Carry-forward fill

IEC 62325-451-3 / A44 lets a publication omit any `Point` whose price is
unchanged from the previous position ("carry forward"). The parser collects only
the explicit points first, then forward-fills across the whole interval
(`api.py:211`, `api.py:251`) so the caller never sees a gap it would interpolate
as a stale neighbour hour. Fill is forward-only: if position 1 itself is missing,
every position before the first explicit point contributes nothing, and the
affected hours simply do not appear in the output dict. Downstream code treats a
missing key as "no data for that hour" (`current_price` falls back to the
nearest hour; sensors go unknown), which is the correct degradation when the
upstream document is genuinely unspecified for the slot.

Three things bound what a document can make the parser do, because both the
point count and the prices come from the document itself:

- **Every `Period` is read.** `findall`, not `find`: a `TimeSeries` that splits
  its window into consecutive `Period` blocks used to lose every slot after the
  first one.
- **The span is capped** at 31 days' worth of slots for the resolution
  (`_MAX_PERIOD_SLOTS`). The `timeInterval` end drives the forward-fill loop, so
  an out-of-range end allocated without limit: a 100-year PT15M interval
  produced 3.5 million slots and about a gigabyte of RSS, an OOM on typical
  hardware. A day-ahead publication covers a day or two.

  The cap has to bound the **loop**, not just the interval. It first applied
  only to the count inferred from `timeInterval`, while the loop ran to
  `max(inferred, max(explicit))` — so a single `<Point>` with an out-of-range
  `<position>` walked straight past it. Measured on an otherwise ordinary
  document carrying one `<position>3000000</position>`: 3 000 000 slots,
  870 MB of peak memory and 163 s of CPU, versus 744 slots, 0,21 MB and 0,04 s
  once the total itself is clamped. Whatever the document claims, one parse
  must cost a bounded amount of work.
- **Non-finite prices are rejected.** `float()` accepts `NaN`, `Infinity` and
  `-Infinity`, and overflows a long literal such as `1e400` to `inf`, so a
  malformed price entered the spot cache looking real. It then spreads:
  `factor*spot + base` is `nan`, the month mean propagates it so a spot-monthly
  contract's flat rate goes `nan`, and the backfill writes it into recorder
  statistics where it outlives the document. `1e400` is the case to care about:
  a plausible upstream typo rather than a hostile literal.

The interval length is inferred from `timeInterval` end minus start, rounded up
so a window that is not an exact multiple of the resolution keeps its trailing
sub-hour slot, with the explicit positions used as a floor (`api.py:231`).

### Timezone handling

`_parse_iso_utc` (`api.py:346`) parses each `timeInterval` boundary with
`datetime.fromisoformat` (after normalising a trailing `Z`). A44 timestamps are
UTC by spec, but if a document ever omits the zone, a naive value is treated as
UTC rather than the HA host's local time (`api.py:352`). Everything in this
module works in UTC; conversion to Europe/Brussels local time (and the DST-aware
day boundaries) happens in the coordinator and backfill, never here. A malformed
timestamp is wrapped as `EntsoeError` (`api.py:62`) so the coordinator keeps
serving cached spots instead of the `ValueError` escaping uncategorised.

### Unit conversion and return shape

ENTSO-E publishes prices in EUR/MWh. The parser divides each `price.amount` by
1000 to get EUR/kWh at the point where it is read (`api.py:228`). The return
shape is:

```python
fetch_day_ahead(...) -> dict[datetime, float]
    # key:   slot-start, timezone-aware UTC
    #        (top-of-hour by default; native 15-min instant if quarter_hourly)
    # value: EUR/kWh (already converted from the source EUR/MWh)
```

A malformed `price.amount` or `position` raises `EntsoeError`
("malformed point in document", `api.py:226`).

### How the coordinator drives this client (caching and dedup)

`api.py` itself is stateless. All caching lives in the coordinator, which
constructs a fresh `EntsoeClient` per call (`api.py:66`,
`coordinator_spots.py:198`). Two paths use it:

- Live curve, `_fetch_spot_prices` (`coordinator_spots.py:240`). Windows the request
  on the local (Europe/Brussels) day so a 00:00 to 02:00 local query does not
  drop yesterday's UTC tail; anchors both endpoints on local midnight converted
  to UTC so the fetched window matches the actual local-day hour count, which
  matters on the DST fall-back Sunday (25 local hours). It requests tomorrow only
  once `now_local.hour >= 11` (ENTSO-E publishes the day-ahead curve around 12 to
  13 CET). Results are cached in `_spot_cache` keyed by `_spot_cache_day`, with a
  separate `_spot_cache_includes_tomorrow` flag set from what the response
  actually carries, not what was requested, so a pre-publication tick that came
  back with today only will retry tomorrow on the next hourly tick
  (`coordinator_spots.py:253`, `coordinator_spots.py:286`). `quarter_hourly` is derived from
  the loaded snapshot's energy kind (`coordinator_spots.py:274`).
- Historical backfill, `_ensure_historical_spots` (`coordinator_spots.py:123`).
  Ensures `self._historical_spots` covers every hour of the local days in a range,
  fetching only the missing spans. It considers a day "present" when at least 20
  of its 24 hours are cached (`coordinator_spots.py:179`), tolerating both the
  carry-forward gaps ENTSO-E occasionally leaves and the 23/25-hour DST seam days
  without re-fetching every tick. Missing spans are fetched in week-sized chunks
  (`coordinator_spots.py:204`). A negative cache, `_short_spot_days` with a TTL, marks
  stable past days that stay short after a fetch so subsequent ticks skip them
  (`coordinator_spots.py:234`); today and yesterday are always re-fetched.

`_historical_spots` is persisted to HA storage (`STORAGE_VERSION = 2`,
`const.py:260`) and reloaded on restart (`coordinator.py:438`). The reload is
gated on a "tuple" match (supplier / contract / region): spots collected while
the entry was dynamic are dropped after an options-flow swap to a static supplier
rather than being re-saved indefinitely (`coordinator.py:439`). Persisted keys
are ISO strings; a naive one is treated as UTC on load (`coordinator.py:447`).

## Part 2: recorder backfill (`backfill.py`)

### Why it exists

A freshly configured entry only starts writing statistics from its first live
coordinator tick, so the Energy dashboard and Statistics graph card would show no
price history before that moment. `backfill.py` reconstructs the missing history:
for each past hour it looks up the tariff card that applied to that month, prices
the hour with `compute_breakdown` exactly as the live tick would have, and pushes
the result into the recorder's long-term statistics store. It reads the same
sources as the live coordinator: per-month tariff cards via `_snapshot_for_month`
and ENTSO-E historical spots via the coordinator's persistent cache
(`backfill.py:34`).

### Two entry points

| Function | Trigger | Behaviour |
| --- | --- | --- |
| `backfill_range` (`backfill.py:877`) | `backfill_statistics` service | Always runs over the requested range; `clear=True` deletes the series first. |
| `backfill_if_missing` (`backfill.py:1036`) | fire-and-forget task from `async_setup_entry` | Probes the recorder at the Jan 1 anchor and runs only when nothing exists. |

There is no backfill button. The only button in the integration is
`reset_monthly_peak` (`button.py:41`). Backfill is reached either automatically
at setup or through the `backfill_statistics` service, wired in `__init__.py`
(service handler `_async_backfill_service` at `__init__.py:593`, one-shot
scheduling at `__init__.py:233`). The service handler validates that a snapshot
is loaded and raises a localized `ServiceValidationError` otherwise, matching the
window services (`__init__.py:525`).

`backfill_if_missing` tolerates entry removal mid-flight: because it runs as a
background task the user can delete the entry between scheduling and execution,
so it bails when `async_get_entry` returns `None` or `runtime_data` is no longer
a coordinator (`backfill.py:879`).

### Statistic ids and the two statistic shapes

The statistic id is the sensor's entity id, resolved from the entity registry by
unique id `f"{entry_id}_{key}"` via `_stat_id` (`backfill.py:141`). When the
entity is not registered yet (the auto path can fire before platform setup
completes), the sensor is skipped silently and reported with a 0 count rather
than fabricating a slug that would diverge from a user-renamed entity.

Two families of statistics are written:

| Sensor keys | Kind | Metadata | Row fields | Unit |
| --- | --- | --- | --- | --- |
| `current_price`, `energy_component`, `network_component`, `taxes_component`, and `injection_price` (injection regime only) | `mean` | `mean_type=ARITHMETIC`, `has_sum=False` | `StatisticData(start, mean, min, max)` | `EUR/kWh` |
| `current_year_cost` | `sum` | `mean_type=NONE`, `has_sum=True` | `StatisticData(start, state, sum)` | `EUR` |

These are `async_import_statistics` external statistics (`source="recorder"`),
not internal long-term statistics derived from a live sensor state. The key list
(`_PRICE_SENSOR_KEYS`, `backfill.py:131`) is maintained by hand in lockstep with
`sensor.py`, deliberately, because the backfilled values come straight out of
`compute_breakdown`, not from the live entities, so coupling this module to the
entity-construction tuples would buy nothing.

The `min`/`max` on the price rows are set equal to the `mean`: each backfilled
hour carries a single computed price, so the arithmetic mean statistic has no
intra-hour spread to record.

### Reconstructing past consumption from the recorder

Only the price (`mean`) sensors are pure functions of the tariff and spot. The
`current_year_cost` sensor also needs how many kWh the household consumed and
injected each past hour. `_backfill_cost_sensor` (`backfill.py:601`) recovers
that from the recorder: it reads hourly kWh for every configured consumption
sensor (`_hourly_consumption_sensors`) and injection sensor
(`_hourly_injection_sensors`) through `_recorder_hourly_kwh`, binned into
UTC-hour totals (`backfill.py:586`). The recorder helpers treat their date
arguments as local-day boundaries, so the code passes the local dates of the
first and last UTC hour, keeping the query window aligned with the backfill's
`_hour_iter` grid (`backfill.py:154`).

### Billing each past hour at its historical rate

Both backfill passes cache one `SupplierSnapshot` per month via
`_month_snapshot_cache` (`cohort.py:370`, called at `backfill.py:431`), so a 365-day window
touches at most 12 archive fetches. `_snapshot_for_month` reuses the extractor's
`fetch_for_month` archive path (see [provider-framework.md](provider-framework.md)),
falling back to the current live snapshot when a supplier publishes no archive.
For each hour, the code converts the UTC hour to local time, picks that month's
snapshot, looks up the hour's spot (or `None`), and calls
`compute_breakdown(snap, dso, region, local, spot, meter, dso_mode)`
(`backfill.py:440`, `backfill.py:632`). A dynamic supplier with no spot for the
hour is skipped, because `factor * spot + base` needs both terms
(`backfill.py:437`). `KeyError` / `ValueError` (a missing DSO row for an archived
month, or a non-static rate kind reaching the static path) skips just that hour
rather than tearing the whole backfill down (`backfill.py:441`).

The injection credit reuses `_historical_injection_rate` (`injection.py:251`,
called at `backfill.py:479`), the same coordinator helper the live YTD path uses, so a
monthly-indexed, spot-indexed, or fixed injection rate is resolved identically in
both places.

### Spot provisioning for backfill

`_ensure_dynamic_spots` (`backfill.py:281`) reuses the coordinator's
`_ensure_historical_spots` so the bulk-fetch logic (week-sized chunks, present
threshold, negative cache) stays in one place. It returns an empty dict when no
spot is needed (static energy with a monthly or no injection). The gate is
`isinstance(snap.energy, DynamicRates) or _injection_needs_spot(snap, entry)`
(`backfill.py:312`): a static-energy contract whose injection is itself
spot-indexed (Cociter Variable) still needs spots so its feed-in credit lands in
the backfilled rows and no sum-chain step appears at the backfill-to-live seam.
It feeds `_ensure_historical_spots` local dates (`dt_util.as_local(...).date()`,
`backfill.py:323`) to match the live coordinator's local-day anchoring.

### The `current_year_cost` cumulative-sum invariant

`current_year_cost` is a cumulative `TOTAL` sensor that resets on Jan 1. The
recorder renders the Energy dashboard's cost change as `sum - prev_sum` and
ignores `last_reset` for imported statistics, so the sum must never drop
mid-series: a drop back toward zero would render as a large spurious negative
cost. Two rules enforce this:

- The cost series must stay within a single calendar year. `backfill_range`
  anchors the cost accumulation on Jan 1 of the end year (`cost_anchor_utc`,
  `backfill.py:787`) and never crosses a year boundary; a multi-year request only
  backfills the end year's cost (`backfill.py:814`). The price `mean` sensors are
  unaffected and keep the full requested window.
- The accumulator starts from Jan 1 but only emits rows on or after
  `emit_from` (`backfill.py:731`), so a mid-year `start` still carries the correct
  year-to-date sum instead of restarting from zero and clashing with the existing
  head of the series.
- A window ending on or before Jan 1 of the *current* year rebuilds the price
  sensors only. The two rules above keep one request inside one year, but a
  finished past year's series would then sit immediately before the current
  year's in the same statistic id, and the join is exactly the drop the
  invariant forbids. Setting `last_reset` on the imported boundary row does not
  help: it was measured, and a row carrying the new year's `last_reset` still
  reported `change = -1197`. So the cost leg is skipped and the service response
  carries a `skipped` note saying why (`backfill.py:874`). Representing a past
  year would mean abandoning the per-year restart and importing a
  lifetime-cumulative sum instead, which is a different design.

**The `sum` chain has to be handed over to the live compile.** `current_year_cost`
is `state_class: TOTAL`, so HA's own sensor platform compiles statistics under the
same statistic id the backfill imports into — and it seeds its running sum from
`statistics_short_term` alone (`sensor/recorder.py`: `_sum = last_stat.get("sum")
or 0.0`), a table `async_import_statistics` never writes. Left alone, the live
chain restarts at zero directly after a backfilled row carrying the whole year, so
the first compiled hour reports `change = 0 - <year to date>` and the Energy
dashboard's Cost card shows roughly **minus one annual bill** for that day.
`_seed_short_term_sum` (`backfill.py:826`) writes one short-term row at the last
backfilled instant to hand the platform its resume point. That row must carry
`last_reset` as well as `state` and `sum`: the compiler reads all three, and a row
missing `last_reset` looks like a fresh cycle against the sensor's Jan-1
`last_reset`, taking the meter-reset branch and adding the whole live reading on
top of the resumed sum (observed: `sum` 1000.4 instead of 500.3). The seed is best
effort and swallows recorder errors, since failing to seed is no worse than not
trying. `tests/recorder/test_backfill_seam.py` pins all three states against a
real recorder.

`_backfill_cost_sensor` runs one running total per hour (`backfill.py:601`)
rather than one end-of-day number, so the recorder draws a smoothly growing YTD
line. Fixed fees (the supplier's yearly fixed fee, the energy-fund monthly charge
times 12, the DSO data-management annual charge, and the Brussels Brugel OSP fee)
are prorated per hour as `annual_static / days_in_year / hours_per_local_date`
(`backfill.py:671`, `backfill.py:673`). Dividing by that day's actual UTC-hour
count makes every local day, including the 23-hour and 25-hour DST seam days, sum
to exactly `annual / days_in_year`, matching the live per-day proration at the
seam. The Walloon prosumer fee (compensation regime, gated to Wallonia at
`backfill.py:698`) is prorated the same way against `days_in_full_month`. The
compensation regime clamps the displayed energy term at zero (`backfill.py:724`),
because a Walloon reversing meter forfeits surplus injection past consumption.

### The requested window is clamped to now

`_normalize_window` floors both ends to whole hours and caps the end at the
current hour. `compute_breakdown` evaluates any hour for a fixed / variable /
TOU / Impact contract, so an end date in the future used to write real-looking
price rows for hours that have not happened, and kept the cost sensor's fee,
capacity and prosumer accrual running through them. The `backfill_statistics`
service schema puts no upper bound on `end`, so a mistyped year was enough: an
end one year out produced 8760 phantom hours. The `end=None` default always
stopped at now; an explicit end now gets the same bound.

### Idempotency and replay

`async_import_statistics` upserts on `(statistic_id, start)`, so re-running
`backfill_range` over a window simply overwrites those hours; a re-run is always
safe (`backfill.py:761`). `backfill_if_missing` avoids redundant work by probing
the recorder itself rather than persisting a separate "backfill done" flag that
would go stale across DB resets or supplier changes. `_existing_stat_window`
(`backfill.py:204`) queries `statistics_during_period` over a 2-day window from
the Jan 1 anchor: a single-hour probe could read empty when a dynamic contract
genuinely lacks the Jan 1 00:00 spot and would then re-run the whole-year
backfill on every restart, whereas a short window still reads empty after a real
DB reset (self-healing preserved) but tolerates a legitimately-absent leading
hour.

### `clear=True` is series-scoped and guarded

The recorder's only public deletion primitive here is `clear_statistics`, which
is series-scoped, not range-scoped: `_clear_all` (`backfill.py:259`) deletes the
entire series for the given statistic ids. `backfill_range` therefore refuses the
narrow-window-plus-clear combination: if `clear=True` and the window starts after
Jan 1 of the end year, it raises `ServiceValidationError` (`backfill.py:800`),
because the wipe would remove the Jan 1 to start head of the year while the
re-import only repopulates the requested range, leaving those rows gone for good.
The `services.yaml` description and every locale's strings warn about this
destructive scope. Without `clear`, no deletion happens and the upsert alone
overwrites the requested hours.

That guard only covers windows starting AFTER the anchor. The mirror case is a
window reaching back BEFORE it: the price series are re-imported over the whole
requested range, but the cost series is deliberately re-imported over the end
year only, so a series-scoped wipe destroyed every prior year of cost history
and never put it back. The cost sensor is therefore excluded from the wipe
unless the window IS exactly the end year (`start_utc == cost_anchor_utc`).
Skipping it is safe: `async_import_statistics` upserts on
`(statistic_id, start)`, so the re-imported year still lands over whatever was
there.

### `after_dependencies` on `energy` and `recorder`

`manifest.json` declares `"after_dependencies": ["energy", "recorder"]`. Unlike
`dependencies`, this does not force those integrations to exist; it only orders
setup so that when they are configured they load before this integration. Both
matter to this module:

- `recorder` provides every statistics primitive backfill uses:
  `async_import_statistics` and `StatisticData` / `StatisticMetaData` /
  `StatisticMeanType` (`backfill.py:367`, `backfill.py:535`),
  `statistics_during_period` for the missing-probe (`backfill.py:227`),
  `clear_statistics` for the destructive path (`backfill.py:262`), and the
  hourly-kWh reconstruction that reads past consumption. Loading after the
  recorder ensures its statistics tables are ready when the one-shot backfill
  task fires at setup. The recorder imports are still wrapped in
  `try/except ImportError` (`backfill.py:222`, `backfill.py:258`) so a bare HA
  without the recorder degrades gracefully instead of crashing.
- `energy` is the consumer: the Energy dashboard reads the `current_year_cost`
  sum series and the price means this module writes. Ordering after it keeps the
  dashboard's expectations satisfied on first load.

## Part 3: the Synergrid SPP profile (`synergrid.py`)

### Why it exists

Belgian SPP-indexed injection contracts do not pay the plain monthly average of
the day-ahead price; they pay the **SPP-weighted** average, weighting each
quarter-hour by the national solar production profile (SPP) Synergrid publishes.
The plain mean over-credits injection, because solar produces (and the meter
injects) mostly in the cheap midday hours the SPP down-weights: measured over a
high-solar month the plain mean ran ~22% above the SPP-weighted value. When the
expert custom monthly-average mode opts into SPP-weighted injection, the
coordinator computes that weighted average itself from the profile plus the
ENTSO-E prices it already caches.

### What it fetches and how

Synergrid publishes the profile as a public, no-login workbook at
`synergrid.be/images/downloads/SLP-RLP-SPP/<year>/SPP_ex-ante_and_ex-post_<year>.xlsx`.
The file is ~52 MB, almost entirely the ex-post sheet the fetcher never touches:
`fetch_spp_weights` streams the download to a temp file (never into memory) and
parses only the small `SPP_ex-ante` sheet with the stdlib `zipfile` +
`ElementTree` (no new dependency), keeping peak memory around 20 MB. The sheet is
resolved by name-prefix and the value column by header text, so a minor layout
change degrades to an empty result rather than a wrong one. The 15-minute weights
are aggregated to hourly, keyed by UTC `(month, day, hour)` to line up with the
coordinator's hourly spot cache. Any failure (download, format drift, a 404 for a
not-yet-published year) returns `{}` so the caller falls back to the plain mean.

### Caching and use

The coordinator refreshes the profile at most monthly (`_SPP_REFRESH_DAYS`; the
ex-ante file is revised in-year) via `_ensure_spp_weights`, and persists the
weights in the entry's Store blob so a restart does not force a fresh download.
`_spp_weighted_month_mean` then computes `sum(price * weight) / sum(weight)` over
the delivery month for the injection index, while energy keeps the plain mean.
Both the live injection sensor and the year-to-date credit use it.

### The ex-ante caveat

Only the **ex-ante** (forecast) profile is public for the running year; the
realized ex-post lags and is not published for the current month. So the
SPP-weighted value is much closer than the plain mean but not the exact settled
figure a supplier prints on its card. Scraped SPP-indexed suppliers (DATS 24,
Ecofix Flexy, EBEM) are unaffected: they print the realized value and the
integration reads it into `InjectionRates.current` directly.
