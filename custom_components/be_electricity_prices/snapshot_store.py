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
from datetime import date
from datetime import datetime
from datetime import timedelta
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp
import asyncio

from .const import (
    DOMAIN,
)
from .providers.base import (
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
