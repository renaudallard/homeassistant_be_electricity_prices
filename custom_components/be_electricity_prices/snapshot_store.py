# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Snapshot persistence and the cross-entry caches.

Split out of coordinator.py. Holds the on-disk Store and its schema version,
the shared snapshot / lock / failed-fetch dicts that let several entries on the
same (supplier, contract, region) tuple share one fetch, and the per-entry VAT
and excise-band resolution applied on load.

_SNAPSHOT_SCHEMA_VERSION lives here with the (de)serialisation it guards: the
persisted snapshot holds the card AS PARSED, so any change to what an extractor
produces has to move this number with it."""

from __future__ import annotations

import logging

from dataclasses import dataclass
from collections.abc import Sequence
from datetime import date
from datetime import datetime
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp
import asyncio

from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_INCLUDE_VAT,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_INCLUDE_VAT,
    DOMAIN,
    STORAGE_VERSION,
)
from .providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
    apply_vat,
    resolve_excise_band,
)

# Coordinator probes the supplier on every update tick (UPDATE_INTERVAL_MINUTES);
# SNAPSHOT_REFRESH_HOURS is the fallback TTL for suppliers that have no probe
# path. With a probe, the snapshot stays cached until the probe key changes.
SNAPSHOT_REFRESH_HOURS = 24
SNAPSHOT_STALE_DAYS = 7

# Process-wide snapshot sharing across config entries. Two entries that
# point at the same (supplier, contract, region) share their freshly
# fetched SupplierSnapshot, so we never poll the same PDF twice. Each
# key also has an asyncio.Lock so concurrent first-fetches deduplicate.
_SHARED_SNAPSHOTS_KEY = "snapshot_cache"
_SHARED_LOCKS_KEY = "snapshot_locks"

# Negative cache for fetch failures: when extractor.fetch raises, a
# sibling coordinator on the same (supplier, contract, region) shouldn't
# repeat the same failing network round-trip on the very next tick.
# The stored timestamp is the last failure; siblings skip retrying for
# _SHARED_FAILURE_TTL after that. Long enough to dedupe a tight burst of
# update ticks, short enough that a real recovery is picked up the next
# minute.
_SHARED_FAILED_FETCHES_KEY = "snapshot_failed_fetches"
_SHARED_FAILURE_TTL = timedelta(minutes=5)

# Per-(supplier, contract, region, YYYY-MM) cache of historical snapshots
# the time-correct yearly-cost flow uses to bill each past month at its
# own rate. ``None`` is a negative cache so a probe-less supplier or a
# month outside the supplier's archive horizon doesn't refetch every
# refresh. Lives in-memory only; rebuilt fresh on HA restart.
_MONTHLY_SNAPSHOTS_KEY = "monthly_snapshot_cache"

# Per-(supplier, contract, region, YYYY-MM) timestamp of the last
# transient ``fetch_for_month`` failure. ``_snapshot_for_month``
# deliberately does NOT cache a transient error as a negative result
# (cached None means "no archive for this month"), so without this
# secondary marker every hourly tick would re-attempt every still-
# uncached past month against a flaky CDN. The TTL matches the live
# TTL: long enough to dedupe one hour of update ticks, short enough
# that a real recovery is picked up promptly.
_MONTHLY_FAILED_FETCHES_KEY = "monthly_snapshot_failed_fetches"
_MONTHLY_FETCHED_AT_KEY = "monthly_snapshot_fetched_at"
_MONTHLY_FAILURE_TTL = timedelta(minutes=30)
# How long a per-month archive row that can still MOVE is trusted. A card for
# a closed month is immutable once parsed and is cached for the process life;
# everything else is provisional and re-asked on the same clock the live card
# uses. Two rows are provisional: the running month, whose card a supplier can
# republish or correct mid-month, and a cached "no archive here", which for a
# supplier publishing in arrears (Ecopower) only means "not out yet" and turns
# into a real card days later.
_MONTHLY_PROVISIONAL_TTL = timedelta(hours=SNAPSHOT_REFRESH_HOURS)


@dataclass
class _SharedSnapshot:
    snapshot: "SupplierSnapshot"
    fetched_at: datetime
    # Last probe key seen when this snapshot was fetched. ``None`` for
    # suppliers without a probe path - those fall back to the time-based
    # TTL alone.
    probe_key: str | None = None


@dataclass(frozen=True)
class SharedFetch:
    """What one trip through the shared-snapshot policy produced.

    Deliberately does not raise. The coordinator wants a failed fetch to leave
    the previous snapshot in place and turn into a Repairs card; a ranking
    sweep wants it to print one row as unreachable and carry on with the next
    contract. Neither is served by an exception unwinding the caller, so the
    exception rides back as a value. It is the object rather than just its
    text, because the coordinator classifies on the type -- transient against
    unreadable -- and re-raises the ones it did not expect with their original
    traceback.
    """

    row: _SharedSnapshot | None
    # Which arm answered: shared | local | fetch | backoff | failed. The
    # caller needs it because the arms are not interchangeable -- see
    # ``probe_confirmed``.
    source: str
    probe_key: str | None
    # A probe match said the card is current, not merely that the TTL has not
    # expired. Only this is proof the supplier is reachable, which is why the
    # local arm clears a stale error on it and not on a TTL hit.
    probe_confirmed: bool
    error: BaseException | None = None
    error_message: str = ""
    # Consecutive failures on this key, carried on the negative row so a lone
    # transient timeout does not raise a repair issue on its own.
    fail_count: int = 0


def _row_is_fresh(
    row: _SharedSnapshot,
    probe_key: str | None,
    now: datetime,
    ttl: timedelta,
) -> bool:
    """Whether a cached row can be reused without re-fetching.

    One rule for a sibling's row and for the caller's own: a probe key that
    matches proves the supplier has not republished, and without a probe the
    row stands until the TTL runs out. Two copies of this drifted apart once
    already, which is what ``fetch_shared`` exists to prevent.
    """
    if probe_key is not None:
        return row.probe_key == probe_key
    return now - row.fetched_at < ttl


def _adopted(
    row: _SharedSnapshot, probe_key: str | None, now: datetime
) -> _SharedSnapshot:
    """The row to keep after adopting ``row``, restamped only if a probe said so.

    A probe match means "checked just now, still current", so the age clock
    restarts and the snapshot_age sensor reads honestly. A TTL match means only
    that the row has not expired yet: restamping there would push the expiry
    out on every tick and the supplier would never be re-fetched at all.
    """
    if probe_key is None:
        return row
    return _SharedSnapshot(snapshot=row.snapshot, fetched_at=now, probe_key=probe_key)


async def fetch_shared(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    *,
    supplier: str,
    local: _SharedSnapshot | None = None,
    force: bool = False,
    record_failure: bool = True,
) -> SharedFetch:
    """Resolve one supplier card through the shared cache, probe and lock.

    The whole policy in one place: the cheap probe, a sibling's row, the
    caller's own row, the negative-cache backoff, the per-key lock and the
    fetch. It lives beside the caches it manipulates rather than on the
    coordinator, because the coordinator is not the only thing that needs a
    card any more -- a ranking sweep wants the same probe short-circuit, the
    same lock and the same backoff, and a second implementation of them would
    drift from this one the way the two freshness rules already had.

    ``local`` is the caller's own copy, offered as a cache row of equal
    standing and consulted after the shared one. It is what keeps this a
    single policy: without it the coordinator has to keep its own freshness
    rule and run its own probe.

    ``supplier`` is the registry id, passed rather than read off the extractor
    so the cache key is derived in exactly one place -- the caller that looked
    the extractor up. Two derivations of this key would not fail loudly: they
    would put the coordinator and the sweep in separate key spaces sharing
    nothing, and the symptom is a cache that simply never hits.

    ``force`` opts out of every adoption shortcut, for the user-facing refresh
    service. Without it a sibling that re-seeded the shared cache between the
    eviction and the next tick would silently satisfy the forced refresh.

    ``record_failure`` decides whether a failure here is written to the shared
    negative cache. It must be False for a read-only caller. A background
    tick's failure is evidence about the supplier, and the row exists so
    siblings back off instead of refiring a broken request; a dialog's failure
    is evidence about that dialog, and letting it write the row makes an
    interactive page cancel a real entry's due download for five minutes and
    inflate the consecutive-failure counter the Repairs card is thresholded on.

    Returns rather than raises; see ``SharedFetch``.
    """
    ttl = timedelta(hours=SNAPSHOT_REFRESH_HOURS)
    now = dt_util.utcnow()
    key = (supplier, contract, region)
    cache = _shared_snapshots(hass)
    failed = _shared_failed_fetches(hass)

    # Cheap probe first. None means the supplier has no probe path or the
    # probe failed; both fall through to the TTL-only flow.
    probe_key: str | None = None
    probe_fn = getattr(extractor, "probe", None)
    if probe_fn is not None:
        try:
            probe_key = await probe_fn(session, contract, region)
        except Exception as err:  # noqa: BLE001 - a probe is best-effort
            # Any failure at all, not just ExtractorError and TimeoutError.
            # The probe exists to SKIP work: falling back to the TTL path is
            # always correct, so its failure must never be worse than not
            # having a probe at all. Narrower here, this ran on a background
            # tick where an unexpected error could surface; it now also runs
            # on the compare page, where one would tear down a dialog the
            # user is looking at, and on a sweep, where it would end the
            # sweep at whichever row happened to hit it.
            _LOGGER.debug("probe failed for %s/%s: %s", supplier, contract, err)
            probe_key = None

    confirmed = probe_key is not None

    shared = cache.get(key)
    if not force and shared is not None and _row_is_fresh(shared, probe_key, now, ttl):
        row = _adopted(shared, probe_key, now)
        cache[key] = row
        return SharedFetch(row, "shared", probe_key, confirmed)

    if not force and local is not None and _row_is_fresh(local, probe_key, now, ttl):
        row = _adopted(local, probe_key, now)
        # Seed the shared cache when this caller is the first to verify a
        # disk-loaded row after a restart, so siblings adopt instead of each
        # re-running its own probe. Re-use the previous probe key when this
        # probe came back empty: probe-less suppliers stay None, and a
        # transiently-failing probe keeps the last known key.
        if cache.get(key) is None:
            cache[key] = _SharedSnapshot(
                snapshot=row.snapshot,
                fetched_at=row.fetched_at,
                probe_key=probe_key if probe_key is not None else local.probe_key,
            )
        return SharedFetch(row, "local", probe_key, confirmed)

    # Negative cache: a sibling that just failed on this key means back off
    # rather than refire the same broken request. ``force`` bypasses it, or the
    # refresh service silently no-ops when a sibling failed in the window.
    if not force:
        last_fail = failed.get(key)
        if (
            last_fail is not None
            and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
        ):
            return SharedFetch(
                None, "backoff", probe_key, confirmed, None, last_fail[1], last_fail[2]
            )

    gen_at_entry = _tuple_generation(hass, key)
    async with _shared_lock(hass, key):
        shared = cache.get(key)
        locked_now = dt_util.utcnow()
        if (
            not force
            and shared is not None
            and _row_is_fresh(shared, probe_key, locked_now, ttl)
        ):
            row = _adopted(shared, probe_key, locked_now)
            cache[key] = row
            return SharedFetch(row, "shared", probe_key, confirmed)
        # Re-check the backoff under the lock so the second waiter does not
        # repeat what the first just failed.
        if not force:
            last_fail = failed.get(key)
            if (
                last_fail is not None
                and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
            ):
                return SharedFetch(
                    None,
                    "backoff",
                    probe_key,
                    confirmed,
                    None,
                    last_fail[1],
                    last_fail[2],
                )
        try:
            snap = await extractor.fetch(session, contract, region)
            fetched_at = dt_util.utcnow()
            row = _SharedSnapshot(
                snapshot=snap, fetched_at=fetched_at, probe_key=probe_key
            )
            # Do not write the cache if the tuple was evicted mid-fetch (entry
            # removed, or supplier swapped). The row is still useful to the
            # caller for this tick.
            if _tuple_generation(hass, key) == gen_at_entry:
                cache[key] = row
                failed.pop(key, None)
            return SharedFetch(row, "fetch", probe_key, confirmed)
        except Exception as err:  # noqa: BLE001 - handed back as a value
            # Any failure populates the negative cache so siblings back off.
            # The third field counts consecutive failures on this key so a lone
            # transient timeout does not immediately raise a repair issue; it
            # rides the shared row and resets the moment a fetch succeeds.
            prev = failed.get(key)
            fail_count = (prev[2] if prev is not None else 0) + 1
            if record_failure and _tuple_generation(hass, key) == gen_at_entry:
                failed[key] = (dt_util.utcnow(), str(err), fail_count)
            return SharedFetch(
                None, "failed", probe_key, confirmed, err, str(err), fail_count
            )


def _shared_snapshots(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str], _SharedSnapshot]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_SHARED_SNAPSHOTS_KEY, {})  # type: ignore[no-any-return]


def _shared_failed_fetches(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str], tuple[datetime, str, int]]:
    """Per-key (timestamp, last-error-message, consecutive-count) of recent
    fetch failures.

    Storing the error message alongside the timestamp lets a sibling
    coordinator that hits the negative-cache short-circuit surface the
    real failure reason in its UpdateFailed instead of an opaque
    'cold start'. The third field counts consecutive failures on the key so
    the coordinator can defer the 'extractor failed' repair issue past a lone
    transient timeout (see _EXTRACTOR_ISSUE_THRESHOLD); it resets whenever a
    fetch succeeds and the row is popped.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_SHARED_FAILED_FETCHES_KEY, {})  # type: ignore[no-any-return]


def evict_shared_caches(
    hass: HomeAssistant, key: tuple[str, str, str], extractor_id: str
) -> None:
    """Drop every shared-cache entry pinned to the given supplier tuple.

    Called from ``async_unload_entry`` once the unloaded entry's
    (supplier, contract, region) is no longer referenced by any other
    loaded entry. Without this, removing the last entry on a given
    tuple leaks the snapshot, the per-month archive cache, the
    failed-fetch marker, and the asyncio.Lock into ``hass.data`` for
    the lifetime of the HA process.
    """
    # Bump the generation counter first so any in-flight cache
    # writer that resumes after this eviction can detect the change
    # and skip its write (the bucket row is gone, so a write would
    # re-create an orphaned row pointing at evicted-tuple data).
    _bump_tuple_generation(hass, key)
    for month_key in list(_monthly_snapshots(hass)):
        if month_key[0] == extractor_id and month_key[1:3] == key[1:3]:
            _bump_tuple_generation(hass, month_key)
    _shared_snapshots(hass).pop(key, None)
    _shared_failed_fetches(hass).pop(key, None)
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str], asyncio.Lock] = bucket.setdefault(
        _SHARED_LOCKS_KEY, {}
    )
    # Only drop the lock when it isn't currently held. If a coroutine
    # is mid-fetch (held lock) and a future entry on the same tuple
    # acquired a fresh lock through ``_shared_lock``, the dedup
    # property would silently break and both coroutines would fan out
    # the same network call. Leaving a locked lock in place defers
    # cleanup to the next eviction; the alternative (cancelling the
    # in-flight fetch) is more invasive than the leak it would
    # prevent.
    held = locks.get(key)
    if held is not None and not held.locked():
        locks.pop(key, None)
    monthly_locks: dict[tuple[str, str, str, str], asyncio.Lock] = bucket.setdefault(
        _MONTHLY_LOCKS_KEY, {}
    )
    for k in _drop_monthly_rows(hass, key, extractor_id):
        held_m = monthly_locks.get(k)
        if held_m is not None and not held_m.locked():
            monthly_locks.pop(k, None)


def _drop_monthly_rows(
    hass: HomeAssistant, key: tuple[str, str, str], extractor_id: str
) -> list[tuple[str, str, str, str]]:
    """Drop every per-month archive row pinned to one supplier tuple.

    Returns the keys removed so a caller can clean up alongside them. A closed
    month's parsed card has no TTL, so whoever wants that re-fetched has to say
    so: on unload that is eviction, on the refresh service it is the user
    asking for the current card again. Rows that can still move expire on
    their own (``_MONTHLY_PROVISIONAL_TTL``).
    """
    monthly = _monthly_snapshots(hass)
    monthly_failed = _monthly_failed_fetches(hass)
    monthly_stamped = _monthly_fetched_at(hass)
    _, contract, region = key
    stale = [
        k
        for k in monthly
        if k[0] == extractor_id and k[1] == contract and k[2] == region
    ]
    for k in stale:
        monthly.pop(k, None)
        monthly_failed.pop(k, None)
        monthly_stamped.pop(k, None)
    return stale


def _shared_lock(hass: HomeAssistant, key: tuple[str, str, str]) -> asyncio.Lock:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str], asyncio.Lock] = bucket.setdefault(
        _SHARED_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


def _monthly_snapshots(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str, str], "SupplierSnapshot | None"]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_MONTHLY_SNAPSHOTS_KEY, {})  # type: ignore[no-any-return]


def _monthly_fetched_at(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str, str], datetime]:
    """Per-(supplier, contract, region, YYYY-MM) time the row was cached.

    Only read for a provisional row; a closed month's card never expires, so
    its timestamp is written and then ignored.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_MONTHLY_FETCHED_AT_KEY, {})  # type: ignore[no-any-return]


def _month_row_is_provisional(
    snap: "SupplierSnapshot | None", year_month: date, today: date
) -> bool:
    """Whether a cached row for ``year_month`` can still change.

    A parsed card for a month that has closed is a historical fact and stays
    cached. A row for the running month is not: the supplier can republish or
    correct it, and this repo already treats that as normal. A cached ``None``
    is not either, whatever the month: it means the archive had nothing at the
    moment it was asked, which for a supplier publishing in arrears is a
    statement about the calendar rather than about the month.
    """
    return snap is None or (year_month.year, year_month.month) >= (
        today.year,
        today.month,
    )


def _monthly_failed_fetches(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str, str], datetime]:
    """Per-(supplier, contract, region, YYYY-MM) timestamp of the last
    transient ``fetch_for_month`` failure."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_MONTHLY_FAILED_FETCHES_KEY, {})  # type: ignore[no-any-return]


_MONTHLY_LOCKS_KEY = "monthly_snapshot_locks"

# Generation counter bumped by evict_shared_caches when a tuple's
# rows are dropped. Cache writers that may have been awaiting at the
# moment of eviction (held lock, mid-fetch) check the counter on
# resume and skip the write if it has advanced. Without this guard a
# slow fetcher would re-create an orphaned cache row that future
# entries on the same tuple could read as stale data.
_TUPLE_GENERATIONS_KEY = "tuple_generations"


def _tuple_generation(hass: HomeAssistant, key: tuple[str, ...]) -> int:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    gens: dict[tuple[str, ...], int] = bucket.setdefault(_TUPLE_GENERATIONS_KEY, {})
    return gens.get(key, 0)


def _bump_tuple_generation(hass: HomeAssistant, key: tuple[str, ...]) -> None:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    gens: dict[tuple[str, ...], int] = bucket.setdefault(_TUPLE_GENERATIONS_KEY, {})
    gens[key] = gens.get(key, 0) + 1


def _monthly_lock(hass: HomeAssistant, key: tuple[str, str, str, str]) -> asyncio.Lock:
    """Per-(supplier, contract, region, YYYY-MM) lock used to dedupe
    concurrent fetch_for_month calls. Without it, two coordinators on
    the same supplier tuple racing on first YTD evaluation each fan
    out 12 monthly fetches before either populates _monthly_snapshots."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str, str], asyncio.Lock] = bucket.setdefault(
        _MONTHLY_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


def archived_months_present(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: "Sequence[date]",
) -> set[tuple[int, int]]:
    """Which of ``months`` this contract has a REAL archived card for.

    Read-only over the cache ``_snapshot_for_month`` already fills, so it costs
    nothing beyond the walk that has happened anyway.

    The distinction matters because ``fetch_for_month is not None`` is a
    property of the SUPPLIER, not of the contract or the month, and seventeen
    candidate contracts pass it and then have no month-addressable card:
    Bolt's whole variable folder returns None before any I/O because its cards
    carry a version suffix rather than a date, and the year-to-date path
    silently substitutes the current card for every past month. That reads as
    a real figure and is 8,5% to 23,3% out - against a row-to-row gap of about
    16 EUR, enough to move a row several places in a column the user can sort,
    with nothing on screen to tell the two kinds of row apart.

    A month is present only when the cache holds an actual snapshot for it.
    A cached ``None`` is the proxy case and is deliberately not counted.
    """
    cache = _monthly_snapshots(hass)
    out: set[tuple[int, int]] = set()
    for month in months:
        key = (supplier, contract, region, f"{month.year:04d}-{month.month:02d}")
        if cache.get(key) is not None:
            out.add((month.year, month.month))
    return out


async def _snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry | None = None,
) -> "SupplierSnapshot":
    """Resolve the historical snapshot for ``year_month`` or fall back.

    Caches the result per (supplier, contract, region, YYYY-MM): a hit
    skips the network round-trip on subsequent refreshes. ``None`` is
    cached too -- "supplier doesn't archive this month" is a stable
    signal we shouldn't keep re-asking. The fallback is the current
    snapshot, used as a proxy for non-archive suppliers (OCTA+,
    TotalEnergies, Engie, Luminus, DATS 24, Mega, Bolt).

    The cache is shared across entries, so it holds archived cards exactly
    as parsed and each caller's own VAT / consumption facts are applied on
    the way out. ``current_snapshot`` is the caller's own and already
    resolved, so it is passed through untouched.
    """

    def resolved(snap: "SupplierSnapshot | None") -> "SupplierSnapshot":
        if snap is None:
            return current_snapshot
        return snap if entry is None else _resolve_snapshot(entry, snap)

    cache = _monthly_snapshots(hass)
    failed = _monthly_failed_fetches(hass)
    cache_key = (
        extractor.id,
        contract,
        region,
        f"{year_month.year:04d}-{year_month.month:02d}",
    )
    fetched_at = _monthly_fetched_at(hass)
    today = dt_util.now().date()
    if cache_key in cache:
        row = cache[cache_key]
        stamped = fetched_at.get(cache_key)
        if not _month_row_is_provisional(row, year_month, today) or (
            stamped is not None
            and dt_util.utcnow() - stamped < _MONTHLY_PROVISIONAL_TTL
        ):
            return resolved(row)
        # Provisional and past its TTL: drop the row and re-ask. Without this
        # a supplier correcting the running month's card moved current_price
        # within a day while the year-to-date and every backfilled row kept
        # billing the vintage first cached at startup, and a month whose card
        # had not published yet stayed "no archive" for the life of the HA
        # process even after the card appeared.
        cache.pop(cache_key, None)
        fetched_at.pop(cache_key, None)
    fetch_archived = extractor.fetch_for_month
    if fetch_archived is None:
        # Not an archive supplier at all, which is a property of the extractor
        # rather than of the month, so this row is never provisional.
        cache[cache_key] = None
        fetched_at[cache_key] = dt_util.utcnow()
        return current_snapshot
    # Negative cache: a transient fetch_for_month failure is intentionally
    # NOT written to ``cache`` (a cached None means "no archive for
    # this month"); without this secondary marker the hourly YTD walk
    # would re-attempt every uncached month against a flaky CDN. Skip
    # the retry while the marker is fresh; current_snapshot is the
    # documented proxy for non-archive months.
    last_fail = failed.get(cache_key)
    if last_fail is not None and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL:
        return current_snapshot
    gen_at_entry = _tuple_generation(hass, cache_key)
    async with _monthly_lock(hass, cache_key):
        # Re-check under the lock so the second waiter doesn't repeat
        # what the first just did.
        if cache_key in cache:
            return resolved(cache[cache_key])

        last_fail = failed.get(cache_key)
        if (
            last_fail is not None
            and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL
        ):
            return current_snapshot
        fetch_failed = False
        try:
            snap = await fetch_archived(session, contract, region, year_month)
        except Exception as err:  # noqa: BLE001 - per-month fetch must never break the year loop
            _LOGGER.debug(
                "fetch_for_month failed for %s/%s/%s/%s: %s",
                extractor.id,
                contract,
                region,
                cache_key[3],
                err,
            )
            snap = None
            fetch_failed = True
            failed[cache_key] = dt_util.utcnow()
        # Skip the cache write if eviction ran during the await: the
        # tuple is no longer this entry's, and re-creating the row
        # would orphan it for any future re-add of the same tuple.
        # Also skip when the fetch raised: a transient error must not
        # be cached as "supplier doesn't archive this month", which is
        # the meaning a cached None carries here. Leaving the key
        # absent lets the next refresh retry instead of locking in
        # stale "uncredited" output until the entry reloads.
        if not fetch_failed and _tuple_generation(hass, cache_key) == gen_at_entry:
            cache[cache_key] = snap
            fetched_at[cache_key] = dt_util.utcnow()
    return resolved(snap)


def _include_vat(entry: ConfigEntry) -> bool:
    """Whether this entry wants its prices VAT-inclusive.

    Inert on a residential card, which prints VAT-inclusive already; it
    only bites on a card published excluding VAT.
    """
    return bool(entry.data.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT))


def _resolve_snapshot(entry: ConfigEntry, snap: SupplierSnapshot) -> SupplierSnapshot:
    """Resolve a card against the site facts only this entry knows.

    Both steps are identity on a residential card, so this is free for
    every existing entry. Order is irrelevant: the excise band is a
    per-kWh rate and ``apply_vat`` never touches those.
    """
    resolved = apply_vat(snap, include_vat=_include_vat(entry))
    return resolve_excise_band(
        resolved,
        float(
            entry.data.get(CONF_ANNUAL_CONSUMPTION_KWH, DEFAULT_ANNUAL_CONSUMPTION_KWH)
        ),
    )


_LOGGER = logging.getLogger(__name__)


class _MigratingStore(Store[dict[str, Any]]):
    """Store subclass that drops blobs from a previous STORAGE_VERSION.

    Every field in the persisted snapshot is re-derivable from a fresh
    extractor fetch, so wiping the cache on a major-version mismatch is
    safe and avoids HA logging the default migrator's "missing migration
    function" warning. Returning an empty dict from
    ``_async_migrate_func`` makes ``async_load`` return ``{}`` and the
    coordinator re-fetches on its first refresh.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,  # noqa: ARG002 - HA signature.
        old_data: dict[str, Any],  # noqa: ARG002 - dropped wholesale.
    ) -> dict[str, Any]:
        if old_major_version < STORAGE_VERSION:
            return {}
        return old_data


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
# v9: DynamicRates gained ``quarter_hourly``. Bump so a cached dynamic
# snapshot from a pre-15-min release (Engie, Cociter, EBEM, Ecofix) is
# dropped and re-fetched with the flag set, rather than lingering on the
# hourly default until the snapshot next refreshes. The probe-based
# suppliers (Cociter, EBEM, Ecofix) would otherwise keep the stale flag
# for weeks, until their next monthly card changes the probe key.
# v10: OCTA+ Dynamic was missed by the v9 sweep; it indexes on the
# 15-minute Epex spot and now sets ``quarter_hourly`` too. Bump so a
# cached OCTA+ dynamic snapshot is dropped and re-fetched with the flag
# set rather than lingering on the hourly default.
# v11: snapshots gained supplier_prosumer_eur_per_kva_year (Cociter's
# compensation-regime PV forfait). Bump so a cached Cociter Variable
# snapshot is re-fetched with the forfait parsed instead of None.
# v12: InjectionRates gained per-slot peak/transition/offpeak (Engie
# Empower Flextime's per-slot feed-in tariff). Bump so a cached Flextime
# snapshot is re-fetched with the triplet instead of the flat single rate.
# v13: the July 2026 Eneco cards dropped the "/ VALORISATIE" suffix from
# the injection heading, so 0.8.3 parsed every Eneco injection to None and
# cached it. 0.8.4 fixed the anchor but probe-based freshness keeps serving
# that stale None until Eneco republishes. Bump so the mis-parsed snapshot
# is dropped and re-fetched with the injection block populated.
# v14: added the SpotMonthlyRates energy kind (expert custom monthly-average
# supplier) and the InjectionRates.floor_at_zero flag. Bump so a cached
# snapshot from before the field existed is dropped and rebuilt with it.
# v15: VariableRates gained formula_factor / formula_base (numeric BELIX-style
# coefficients) so a variable contract with a contract start date re-prices its
# signing cohort against the current month's mean. Bump so a cached variable
# snapshot from before the fields existed is dropped and re-parsed with them.
# v16: the persisted snapshot now holds the card as parsed rather than as
# priced, so the entry's VAT preference is re-applied on load, and TaxOverlay
# gained federal_excise_bands for cards that print the special excise as a
# degressive schedule by annual consumption, and InjectionRates gained
# vat_applies for cards that tax injection. Bump so a cache written under any
# of the old meanings is dropped.
# v17: TaxOverlay gained region_connection_fee_unavailable, for a Walloon card
# that stopped printing the connection-fee row. Bump so an EnergyVision Wallonia
# entry stranded on its July snapshot by the August tax-block change drops that
# cache and re-parses the current card instead of waiting for the probe key to
# move.
# v18: three extractor fixes changed what is parsed into the snapshot, and all
# three suppliers are probe-based, so without this bump an existing entry keeps
# serving the wrong figures until its supplier republishes a card, up to a month
# later. Ecopower stopped baking 6% VAT into databeheer / capacity / the
# subscription, Mega now parses the Flemish energy fund instead of hardcoding
# 0.0, and Bolt reads the non-residential fund row on professional contracts.
# The rule this keeps tripping over: the persisted snapshot holds the card AS
# PARSED, so any change to what an extractor produces needs this version moved
# with it. A change that only affects how a stored card is priced (apply_vat,
# resolve_excise_band) does not, since those run on load.
# v19: Mega's realized-rate parser dropped a negative injection rate, so a
# variable or Impact entry fell back to the 12-month simulation table and
# credited a rate the card charges. Mega is probe-based and the May cards are
# already published, so their probe key will not move again; without this bump
# an affected entry keeps the wrong sign indefinitely.
# v20: two extractors were resolving a superseded card URL, so the cache holds a
# card that parsed perfectly and is simply the wrong one. Bolt pinned the
# variable-family version suffix and served June's formula for ten weeks after
# the August revision shipped; Ecopower's six-digit filename pattern could not
# see the YYYYMMDD card that replaced it and kept serving January's tax block.
# Neither supplier's probe key moves on its own here -- the pinned URL's card is
# unchanged, which is exactly why nothing noticed -- so without this bump an
# existing entry keeps the stale prices indefinitely.
# v21: InjectionRates gained spp_indexed, and energie.be Variabel now parses
# its injection FORMULA rather than only the card's printed indicative. Two
# reasons to move the version with it. A snapshot written before this holds no
# factor/base for that contract, so the entry keeps crediting the VNR forecast
# instead of the realized Belpex_SPP month until the 24 h TTL happens to
# refetch; and every earlier InjectionRates field (per-slot rates, floor_at_zero,
# vat_applies) bumped for the same reason, since _snapshot_from_dict splats the
# stored dict straight into the dataclass and an unknown key is a TypeError.
# v22: energie.be Vast now parses the same injection formula, having only
# emitted the printed indicative before. Same reason as v21: a snapshot written
# earlier carries no factor/base for that contract, so the entry would keep
# crediting the VNR forecast (measured 3,6x the contractual credit in April
# 2026) until the 24 h TTL happened to refetch.
# v23: Mega Cap now parses the contractual ceiling on the energy component
# ("vous payez le minimum entre les prix variables mensuels et ce plafond").
# A snapshot written earlier carries no ceiling, so the entry would price
# straight through the cap the customer is protected by until the card
# happened to refetch, which is exactly when the cap matters.
# v24: the three Brussels extractors now carry Sibelga's power term for a
# connection ABOVE 13 kVA, and parse_brussels_osp reads every band the card
# prints rather than the four at or below 13 kVA. A snapshot written earlier
# has neither, so a connection above the line would keep being billed the
# smaller term and an OSP fee its tier does not have.
# v25: EnergyVision now parses the "maximumtarief" column, the VREG ceiling on
# capacity plus the per-kWh network term, which the card printed and nothing
# read. A snapshot written earlier carries no ceiling.
# v26: Mega's variable cards print one indexation formula per meter and only
# the mono one was parsed, so a bi-hourly signing cohort was re-priced onto it
# for every hour. A snapshot written earlier carries no band coefficients.
# v27: Cociter Tarif Variable now carries month_indexed, so its rate resolves
# against the DELIVERY month's BELIX rather than the printed indicative, which
# the card computes from the previous month's. A snapshot written earlier has
# the flag absent and would keep billing a month late.
# v28: Eneco Power Fix and Flex now surface their injection coefficients with
# InjectionRates.month_indexed, so the credit resolves against the DELIVERY
# month's Belpex-injectie instead of the printed indicative, which the card
# computes from the last known (previous) month's. A snapshot written earlier
# carries neither the coefficients nor the flag.
# v29: EBEM Groen Variabel and B@sic+ now surface their BelpexSPP0 injection
# coefficients with spp_indexed, so the credit resolves against the DELIVERY
# month's solar-weighted mean instead of the printed figure, which the card
# computes from "de SPP0 vorige maand". A snapshot written earlier has neither.
# v30: DATS 24 Groen Variabel now surfaces its BE_spotSPP injection
# coefficients with spp_indexed, for the same reason as v29: the card's printed
# figure is filled in from "de meest recente waarde" of that index, which is the
# previous month's. A snapshot written earlier has neither.
# v31: the EnergyVision fixed cards (Flanders 3-jaar, Wallonia 1-an) now
# surface their "0,6 x Belpex-SPP-M - 15 EUR/MWh" coefficients with
# spp_indexed and the card's 1 c/kWh guarantee as InjectionRates.minimum, so
# the credit resolves against the delivery month's solar-weighted mean instead
# of a printed figure the card says is not that month's. A snapshot written
# earlier has none of the three.
# v32: the six non-dynamic OCTA+ cards now surface their "Epex SPP x 0,852 -
# 13,39" coefficients with spp_indexed. Their printed c/kWh sits in the card's
# "Prix estimes" column and the card says the month's Epex is only known at
# month-end, so a snapshot written earlier carries the estimate and no formula.
# v33: the OCTA+ SPP injection regex now accepts the August 2026 card, which
# renamed the parameter to "Epex SPP M" and swapped the x for a star. v32 was
# written against April cards only, so every live card fell back to the printed
# V-test estimate and a cached v32 snapshot carries no coefficients at all.
# v34: the OCTA+ variable cards now carry their monthly Epex RLP coefficients
# per meter, including a separate night-circuit pair, and month_indexed. A v33
# snapshot holds only the card's V-test 12-month forward estimate.
# v35: every non-dynamic Bolt card now carries the quarter-hourly Belpex
# injection formula its own text describes, flagged slot_indexed, beside the
# illustrative figure it used to credit flat. A v34 snapshot holds the figure
# alone, which is a quarterly-lagged constant that can never go negative.
# v36: Ecofix Flexy now surfaces its BELPEX-SPP-M injection coefficients with
# spp_indexed. A v35 snapshot carries only the printed Maandprijs, which runs
# two months behind the index the card says it settles on.
# v37: Engie Empower Variable and Empty House (and their pro twins) now carry
# their monthly EPEXDAM consumption coefficients and month_indexed. A v36
# snapshot holds only the printed price, which the card itself labels as
# computed from the last KNOWN month rather than the delivery one.
# v38: the eight EPEXDAM-indexed Engie variable contracts now carry their
# monthly injection coefficients and month_indexed. A v37 snapshot holds only
# the printed Injection(3) figure, which is that formula at the PREVIOUS
# month's index.
# v39: the eight non-dynamic TotalEnergies contracts now carry their monthly
# Belpex_M injection coefficients and month_indexed. TotalEnergies is
# probe-based, so a cached v38 snapshot is not re-parsed without this and keeps
# serving the previous month's printed figure.
# v40: the nine Mega variable and Impact contracts now carry their monthly
# "Epex SPP * 0,85 - 2,2" injection coefficients with spp_indexed. A v39
# snapshot holds only the printed figure, which is the previous month's
# regularisation.
# v41: the seven monthly-indexed Luminus contracts now carry their injection
# coefficients and month_indexed. A v40 snapshot holds only the printed figure,
# which the card says is the previous month's.
# v42: Luminus MaxxFlex now carries its monthly Belpex ENERGY coefficients per
# meter, including the night circuit, and month_indexed. A v41 snapshot holds
# only the printed rate, which is the formula at the previous month's index.
# v43: Ecopower Groene Burgerstroom now carries the blended coefficients of its
# 50/50 fixed-plus-SPP feed-in credit, with spp_indexed and the card's
# never-negative floor. A v42 snapshot holds only the printed figure, which for
# an arrears publisher is always a settled past month.
# v44: Cociter Variable now carries a BELIX coefficient pair per meter, the
# night circuit included, where a v43 snapshot held only the mono pair and
# billed every meter on it.
# v45: EBEM Groen Variabel now converts all four of its per-meter formula rows
# into cohort coefficients. A v44 snapshot carries only the mono pair, which
# every meter was then billed on.
# v46: the Mega variable cards now carry the fourth per-meter formula, the
# dedicated night circuit, which a v45 snapshot billed on the mono pair.
# v47: energie.be, DATS 24 and Ecopower now carry the VREG maximumtarief on
# their Flemish overlays. All three printed it and none stored it, so a
# low-volume connection was quoted its uncapped network leg.
# v48: SpotMonthlyRates carries the four ceiling columns, so a Mega Cap
# signing cohort keeps the contractual cap its card guarantees for the year.
# v49: Luminus SmartFlex now carries a monthly coefficient pair per TOU band
# and month_indexed, and SpotMonthlyRates gained the third band plus the
# weekend rule to price them. A v48 snapshot holds only the printed triplet,
# which is the previous month's.
# v50: Bolt's Walloon variable cards now carry the CWaPE incitative supplier
# energy bands beside their standard rates, so an entry on the incitative DSO
# mode bills both halves on the same schedule. A v49 snapshot has the network
# side banded and the energy side flat.
_SNAPSHOT_SCHEMA_VERSION = 50

# The oldest stored schema a rejected blob may still be replayed from when no
# fetch can ever replace it (see _SnapshotMixin._replay_stale_snapshot). v16 is
# the floor because it is the one bump in fifty that changed what a stored
# field MEANS rather than adding one: before it the blob held the card as
# priced, so replaying it through _resolve_snapshot would gross a professional
# entry's rates by its VAT rate a second time. Every later bump either adds a
# field, which loads at the dataclass default, or corrects one supplier's
# parsed value, which is drift on a card already being served months late and
# is what the stale-snapshot card says out loud. Move this only for a bump that
# changes a meaning, never for one that adds a field or corrects a value, or
# the replay stops rescuing anybody.
_DEGRADED_MIN_SCHEMA_VERSION = 16


def _snapshot_to_dict(
    snap: SupplierSnapshot,
    fetched_at: datetime,
    probe_key: str | None = None,
    *,
    schema_version: int = _SNAPSHOT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Serialise one snapshot for the Store.

    ``schema_version`` is what gets stamped, and it is an argument only so a
    replayed blob is written back under the version it was parsed by rather
    than the running one. Stamping it current would launder a v16 card into
    looking freshly parsed, and the next release whose parser fix is meant to
    reach this user would have nothing left to invalidate. Keyword-only,
    because it is the one argument here that goes silently wrong rather than
    loudly wrong if a caller gets the order out.
    """
    return {
        "_cached_at": fetched_at.isoformat(),
        "_probe_key": probe_key,
        "_schema_version": schema_version,
        "supplier": snap.supplier,
        "contract": snap.contract,
        "energy_kind": _energy_kind(snap.energy),
        "energy": snap.energy.__dict__,
        "dsos": {k: v.__dict__ for k, v in snap.dsos.items()},
        "taxes": snap.taxes.__dict__,
        "source_url": snap.source_url,
        "publication_label": snap.publication_label,
        "valid_until": snap.valid_until.isoformat() if snap.valid_until else None,
        "injection": snap.injection.__dict__ if snap.injection else None,
        "supplier_prosumer_eur_per_kva_year": snap.supplier_prosumer_eur_per_kva_year,
    }


def _taxes_from_dict(data: dict[str, Any]) -> TaxOverlay:
    """Rebuild a TaxOverlay, restoring the excise bands' tuple shape.

    JSON has no tuples: a banded excise round-trips as a list of lists and
    has to be put back the way the dataclass declares it.
    """
    bands = data.get("federal_excise_bands")
    if bands is None:
        return TaxOverlay(**data)
    return TaxOverlay(
        **{**data, "federal_excise_bands": tuple((b[0], b[1]) for b in bands)}
    )


def _snapshot_from_dict(
    data: dict[str, Any], *, min_schema_version: int = _SNAPSHOT_SCHEMA_VERSION
) -> SupplierSnapshot:
    """Rebuild a snapshot from its stored dict.

    ``min_schema_version`` is the oldest schema the caller will read. The
    default is the running one, which is the healing gate: anything older is
    refused so the next refresh re-parses the card with the current extractor.
    The replay path lowers it to ``_DEGRADED_MIN_SCHEMA_VERSION``, because for
    a supplier publishing page images there is no next refresh to heal with.
    """
    if data.get("_schema_version", 1) < min_schema_version:
        raise ValueError(
            "snapshot schema is older than the running integration; "
            "discarding cache so the next refresh re-fetches"
        )
    energy_kind = data["energy_kind"]
    energy_args = data["energy"]
    energy: EnergyRates
    if energy_kind == "fixed":
        energy = FixedRates(**energy_args)
    elif energy_kind == "variable":
        energy = VariableRates(**energy_args)
    elif energy_kind == "dynamic":
        energy = DynamicRates(**energy_args)
    elif energy_kind == "tou":
        energy = TimeOfUseRates(**energy_args)
    elif energy_kind == "tou_impact":
        energy = ImpactRates(**energy_args)
    elif energy_kind == "spot_monthly":
        energy = SpotMonthlyRates(**energy_args)
    else:
        raise ValueError(f"unknown energy kind {energy_kind!r}")
    injection_data = data.get("injection")
    valid_until_iso = data.get("valid_until")
    valid_until: date | None = None
    if isinstance(valid_until_iso, str):
        try:
            valid_until = date.fromisoformat(valid_until_iso)
        except ValueError:
            valid_until = None
    return SupplierSnapshot(
        supplier=data["supplier"],
        contract=data["contract"],
        energy=energy,
        dsos={k: DsoOverlay(**v) for k, v in data["dsos"].items()},
        taxes=_taxes_from_dict(data["taxes"]),
        source_url=data["source_url"],
        publication_label=data.get("publication_label", ""),
        valid_until=valid_until,
        injection=InjectionRates(**injection_data) if injection_data else None,
        supplier_prosumer_eur_per_kva_year=data.get(
            "supplier_prosumer_eur_per_kva_year"
        ),
    )


def _energy_kind(energy: EnergyRates) -> str:
    if isinstance(energy, FixedRates):
        return "fixed"
    if isinstance(energy, VariableRates):
        return "variable"
    if isinstance(energy, DynamicRates):
        return "dynamic"
    if isinstance(energy, TimeOfUseRates):
        return "tou"
    if isinstance(energy, ImpactRates):
        return "tou_impact"
    if isinstance(energy, SpotMonthlyRates):
        return "spot_monthly"
    raise TypeError(f"unknown energy rates type {type(energy).__name__}")
