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

import json
import logging

from dataclasses import dataclass
from collections.abc import Sequence
from datetime import date
from datetime import datetime
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp
import asyncio

from .const import (
    CARD_ARCHIVE_FIRST_MONTH,
    CARD_ARCHIVE_URL,
    CONF_CARD_ARCHIVE,
    DEFAULT_CARD_ARCHIVE,
    DOMAIN,
    SUPPLIER_CUSTOM,
)
from .providers._pdf import fetch_text, is_transient_fetch_error
from .snapshot_resolve import _resolve_snapshot
from .snapshot_codec import (
    _DEGRADED_MIN_SCHEMA_VERSION,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from .providers.base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)

_LOGGER = logging.getLogger(__name__)

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
    text, because the coordinator classifies on the type: transient against
    unreadable, and re-raises the ones it did not expect with their original
    traceback.
    """

    row: _SharedSnapshot | None
    # Which arm answered: shared | local | fetch | backoff | failed. The
    # caller needs it because the arms are not interchangeable: see
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
    card any more: a ranking sweep wants the same probe short-circuit, the
    same lock and the same backoff, and a second implementation of them would
    drift from this one the way the two freshness rules already had.

    ``local`` is the caller's own copy, offered as a cache row of equal
    standing and consulted after the shared one. It is what keeps this a
    single policy: without it the coordinator has to keep its own freshness
    rule and run its own probe.

    ``supplier`` is the registry id, passed rather than read off the extractor
    so the cache key is derived in exactly one place: the caller that looked
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


def cached_month_card(
    hass: HomeAssistant, supplier: str, contract: str, region: str, year_month: date
) -> "SupplierSnapshot | None":
    """The archived card the cache holds for that month, without fetching;
    None when the month was never resolved or resolved to nothing."""
    key = (supplier, contract, region, f"{year_month:%Y-%m}")
    return _monthly_snapshots(hass).get(key)


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
    statement about the calendar rather than about the month. Nor is a row the
    extractor itself flagged ``provisional``: Eneco settles a month on the
    index printed on the NEXT card, and a month fetched before that card is
    out still carries the printed estimate.
    """
    return (
        snap is None
        or snap.provisional
        or (year_month.year, year_month.month) >= (today.year, today.month)
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


def monthly_rows_to_store(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: "Sequence[date]",
) -> dict[str, dict[str, Any]]:
    """Serialise this contract's SETTLED per-month archive rows for the Store.

    The per-month cache lives in memory, so every Home Assistant restart used
    to re-fetch one archived card per elapsed month. That is one PDF apiece,
    about 25 s each for Frank Energie on a Raspberry Pi, and it is what issue
    #88 spent its bootstrap budget on. A closed month's card is a historical
    fact, so writing it to disk retires that cost for good rather than merely
    moving it off the setup path.

    Only rows ``_month_row_is_provisional`` calls settled are written: the
    running month's card can still be corrected, and a row the extractor
    flagged provisional is waiting on the index the next card prints. Neither
    is a fact worth outliving the process.

    A CLOSED month that came back with no card is written too, as a marker
    carrying the instant it was asked. Establishing that costs the same
    download and parse as a card does: Frank Energie publishes nothing for
    March 2026 and spends 24 s saying so, and dropping the marker made every
    restart pay it again, for ever. It is not a permanent answer: a supplier
    publishing in arrears turns "not out yet" into a real card days later, so
    the marker expires on the same ``_MONTHLY_PROVISIONAL_TTL`` it does in
    memory, which the restore applies.

    ``months`` bounds the write to the months the year-to-date walk actually
    asks for, so a blob does not accumulate every month an entry has ever
    walked (about 5 KB per row).
    """
    today = dt_util.now().date()
    running = (today.year, today.month)
    cache = _monthly_snapshots(hass)
    stamped = _monthly_fetched_at(hass)
    out: dict[str, dict[str, Any]] = {}
    for month in months:
        month_id = f"{month.year:04d}-{month.month:02d}"
        cache_key = (supplier, contract, region, month_id)
        if cache_key not in cache:
            continue
        snap = cache[cache_key]
        stamp = stamped.get(cache_key)
        if snap is None:
            # Written on its stamp alone, so a marker with no stamp, which
            # nothing writes today, is skipped rather than restored as
            # ageless. Never for the running month: that one is re-asked on
            # every tick anyway.
            if stamp is None or (month.year, month.month) >= running:
                continue
            out[month_id] = {"_cached_at": stamp.isoformat(), "_absent": True}
            continue
        if _month_row_is_provisional(snap, month, today):
            continue
        out[month_id] = _snapshot_to_dict(snap, stamp or dt_util.utcnow())
    return out


def restore_monthly_rows(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    rows: dict[str, Any],
) -> int:
    """Seed the per-month archive cache from a stored blob, returning the
    number of months restored.

    A row already in the cache is left alone: this process fetched it, which
    outranks what the last one wrote. A row that no longer parses, or that was
    written under an older snapshot schema, is dropped rather than migrated,
    the same healing gate the live snapshot uses, so a parser fix reaches
    these months as soon as ``_SNAPSHOT_SCHEMA_VERSION`` moves. And a row that
    is no longer settled is dropped too: the file may be older than the clock
    is, and the running month's card must be re-asked whatever the disk says.
    """
    today = dt_util.now().date()
    cache = _monthly_snapshots(hass)
    stamped = _monthly_fetched_at(hass)
    restored = 0
    for month_id, data in rows.items():
        if not isinstance(month_id, str) or not isinstance(data, dict):
            continue
        try:
            month = date(int(month_id[:4]), int(month_id[5:7]), 1)
        except ValueError:
            continue
        cache_key = (supplier, contract, region, month_id)
        if cache_key in cache:
            continue
        if data.get("_absent"):
            # "The archive had nothing when asked", and asking again is what
            # it costs to find out. Honour it only while the in-memory rule
            # would have, so a card published in arrears is still picked up a
            # day later rather than never.
            try:
                stamp = datetime.fromisoformat(data["_cached_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if dt_util.utcnow() - stamp >= _MONTHLY_PROVISIONAL_TTL:
                continue
            if (month.year, month.month) >= (today.year, today.month):
                # A marker for the running month is never written, but a blob
                # written before midnight on the 1st carries one for what has
                # since become the running month.
                continue
            cache[cache_key] = None
            stamped[cache_key] = stamp
            restored += 1
            continue
        try:
            snap = _snapshot_from_dict(data)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("discarding stored archive row %s: %s", month_id, err)
            continue
        if _month_row_is_provisional(snap, month, today):
            continue
        cache[cache_key] = snap
        try:
            stamped[cache_key] = datetime.fromisoformat(data["_cached_at"])
        except (KeyError, TypeError, ValueError):
            stamped[cache_key] = dt_util.utcnow()
        restored += 1
    return restored


def _row_read_by_ocr(row: dict[str, Any]) -> bool:
    """Whether any document this row read had to be read off its pixels.

    The archive marks the SOURCE, not the row: ``ocr`` sits beside the pdf
    digest it describes, so a row that reads a readable card and an unreadable
    one says which was which. This is the row-level question the Repairs card
    asks, answered by looking.
    """
    sources = row.get("_sources")
    if not isinstance(sources, list):
        return False
    return any(isinstance(source, dict) and source.get("ocr") for source in sources)


@dataclass(frozen=True)
class ArchivedCard:
    """One month's row off the repository's card archive.

    ``read_by_ocr`` says the card that month carried no text layer and the
    archive walk read it off its pixels. It rides beside the snapshot because
    the figures are the same shape either way and only the user needs to know
    the difference.

    Read off the sources rather than a mark on the row, because the fact
    belongs to a document: a row can read two, and only one of them need be
    the unreadable one (137 of the archive's rows read two cards). The row
    once carried its own derived copy as ``_ocr``; nothing kept it in step
    with the sources, and it is gone.
    """

    snapshot: SupplierSnapshot
    read_by_ocr: bool


async def _archived_card_from_github(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    year_month: date,
) -> ArchivedCard | None:
    """The card the repository's own archive holds for this month, or None.

    ``archive_cards.yml`` stores what every extractor parsed, daily, in
    the ``be_price_cards`` repository (``scripts/archive_cards.py``), and
    mirrors the supplier archives into it, so a closed month is one small
    JSON there against a PDF download and a parse from the supplier: that is
    why the month cache asks here first. The row was parsed by the extractor
    of its day and re-parsed by the daily walk the day after a parser change,
    so it is read at the degraded schema floor, the page-image replay's
    position: for a past month a row parsed under an older schema beats the
    current card as a proxy, and the supplier's own archive still answers for
    a month the project's archive does not hold.

    None on a 404, which is a month the archive predates, a contract it does
    not cover or a row never written, and on a row that no longer
    decodes. A transient failure propagates so the caller's negative cache
    treats it like a supplier archive blip and asks again later.
    """
    url = (
        f"{CARD_ARCHIVE_URL}/{supplier}/{contract}/{region}/"
        f"{year_month.year:04d}-{year_month.month:02d}.json"
    )
    try:
        body = await fetch_text(session, url)
    except ExtractorError as err:
        if is_transient_fetch_error(str(err)):
            raise
        return None
    try:
        row = json.loads(body)
        return ArchivedCard(
            snapshot=_snapshot_from_dict(
                row, min_schema_version=_DEGRADED_MIN_SCHEMA_VERSION
            ),
            read_by_ocr=_row_read_by_ocr(row),
        )
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("card archive row %s does not decode: %s", url, err)
        return None


async def card_for_unreadable_month(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the RUNNING month, for a card nobody can read.

    The last resort, and reached only on a card that downloaded fine and
    carries no text layer. A supplier publishing page images leaves a parser
    nothing to work with, but the repository's daily walk reads those with an
    OCR engine and files what it gets, so the row is the one place a price
    for this month exists at all.

    Asking for the running month is exactly what ``_card_archive_may_hold``
    refuses, and rightly: while a card can be read the live parse is the
    better answer and the archive's copy is a day behind at best. That
    reasoning runs out when the card cannot be read, which is the only door
    into this function.

    Honours the entry's card-archive box, which exists so a household can
    keep the integration from contacting GitHub at all.
    """
    if entry is not None and not entry.data.get(
        CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE
    ):
        return None
    return await _archived_card_from_github(
        session, supplier, contract, region, today.replace(day=1)
    )


def _card_archive_may_hold(
    extractor: "SupplierExtractor",
    year_month: date,
    today: date,
    entry: ConfigEntry | None,
) -> bool:
    """Whether the repository's card archive may be asked for this month.

    Not when the entry has switched the archive off: that box exists so a
    household can keep the integration from contacting GitHub, and a caller
    with no entry in hand (the shared cache's own bookkeeping) keeps the
    default. Only a closed month: the running month's card is the one being
    served live, which is what the current snapshot holds, and the
    repository's copy of it is a day behind at best. And a supplier with no
    archive of its own has nothing in the project's archive from before
    ``CARD_ARCHIVE_FIRST_MONTH``: a backfill only mirrors a supplier's own
    archive, so asking for an earlier month is a 404 a day for nothing.
    """
    if entry is not None and not entry.data.get(
        CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE
    ):
        return False
    month = (year_month.year, year_month.month)
    if month >= (today.year, today.month):
        return False
    return extractor.fetch_for_month is not None or month >= CARD_ARCHIVE_FIRST_MONTH


async def _snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry | None = None,
    *,
    cached_only: bool = False,
) -> "SupplierSnapshot":
    """Resolve the historical snapshot for ``year_month`` or fall back.

    Three tiers, in order. The repository's card archive
    (``_archived_card_from_github``) comes first for a closed month it can
    hold (``_card_archive_may_hold``): one small JSON per month, holding the
    supplier archives mirrored and every card captured live, against a PDF
    download and a parse per month from the supplier, which is what made
    the first year-to-date fill of a Frank or Bolt entry minutes on a
    Raspberry Pi. The supplier's own archive (``fetch_for_month``) answers
    for what the project's archive does not hold: the running month, a
    month before its horizon, a row it cannot serve. The current snapshot is
    the proxy when neither has the month, and it is the running month's card
    by definition, so that month never reaches the repository.
    A blip reading the archive is not "no card": the supplier is still
    asked, and a month neither could give is retried on the failure marker
    rather than cached.

    Caches the result per (supplier, contract, region, YYYY-MM): a hit
    skips the network round-trip on subsequent refreshes. ``None`` is
    cached too: "no archive has this month" is a stable signal we
    shouldn't keep re-asking.

    The cache is shared across entries, so it holds archived cards exactly
    as parsed and each caller's own VAT / consumption facts are applied on
    the way out. ``current_snapshot`` is the caller's own and already
    resolved, so it is passed through untouched.

    ``cached_only`` answers from the cache and never reaches the network: a
    month with no row falls back to the current snapshot, the same proxy a
    supplier without an archive gets. The first coordinator tick asks for it
    because that tick runs inside config-entry setup, and one archived card
    per elapsed month is not something setup can afford (Frank Energie's
    cards take ~25 s each to lay out on a Raspberry Pi, so a September start
    spent ~226 s there and Home Assistant cancelled the whole of bootstrap
    stage 2 over it). The warm-up that follows fills the cache off the setup
    path and asks for a refresh.
    """

    def resolved(snap: "SupplierSnapshot | None") -> "SupplierSnapshot":
        if snap is None:
            return current_snapshot
        return (
            snap
            if entry is None
            else _resolve_snapshot(entry, snap, delivery_month=year_month)
        )

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
        if (
            cached_only
            or not _month_row_is_provisional(row, year_month, today)
            or (
                stamped is not None
                and dt_util.utcnow() - stamped < _MONTHLY_PROVISIONAL_TTL
            )
        ):
            # A caller that cannot fetch keeps the row it has, expired or not:
            # dropping it here would forfeit a month it is already holding and
            # hand back the current card in its place.
            return resolved(row)
        # Provisional and past its TTL: drop the row and re-ask. Without this
        # a supplier correcting the running month's card moved current_price
        # within a day while the year-to-date and every backfilled row kept
        # billing the vintage first cached at startup, and a month whose card
        # had not published yet stayed "no archive" for the life of the HA
        # process even after the card appeared.
        cache.pop(cache_key, None)
        fetched_at.pop(cache_key, None)
    if extractor.id == SUPPLIER_CUSTOM:
        # Assembled from the entry rather than published: no archive holds
        # a card for it, whatever the month, so this row is never provisional.
        cache[cache_key] = None
        fetched_at[cache_key] = dt_util.utcnow()
        return current_snapshot
    if cached_only:
        # Uncached and no fetch allowed: the documented fallback. Nothing is
        # written to the cache, so the warm-up still asks the archives.
        return current_snapshot
    # Negative cache: a transient archive failure is intentionally NOT
    # written to ``cache`` (a cached None means "no archive has this
    # month"); without this secondary marker the hourly YTD walk would
    # re-attempt every uncached month against a flaky CDN. Skip the retry
    # while the marker is fresh; current_snapshot is the documented proxy
    # for non-archive months.
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
        archive_failed = False
        snap: SupplierSnapshot | None = None
        if _card_archive_may_hold(extractor, year_month, today, entry):
            try:
                archived = await _archived_card_from_github(
                    session, extractor.id, contract, region, year_month
                )
                snap = archived.snapshot if archived is not None else None
            except Exception as err:  # noqa: BLE001 - a blip on the archive must not cost the supplier tier
                _LOGGER.debug(
                    "card archive read failed for %s/%s/%s/%s: %s",
                    extractor.id,
                    contract,
                    region,
                    cache_key[3],
                    err,
                )
                archive_failed = True
        if snap is None and extractor.fetch_for_month is not None:
            try:
                snap = await extractor.fetch_for_month(
                    session, contract, region, year_month
                )
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
        if snap is None and archive_failed:
            # The archive may well hold the month; ask again on the failure
            # marker rather than cache a None the TTL would hold for a day.
            fetch_failed = True
        if fetch_failed:
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
