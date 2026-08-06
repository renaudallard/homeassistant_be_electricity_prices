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

"""The snapshot state machine: fetch, freshness, and the shared cache.

Split out of coordinator.py. Owns when a snapshot is refetched, when a
sibling entry's fetch can be adopted instead, and what counts as fresh --
a probe key match where the supplier offers one, a TTL otherwise."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from .providers import get as get_extractor
from .providers.custom import build_snapshot as build_custom_snapshot
from .providers._pdf import is_transient_fetch_error

import logging

from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_REGION,
    CONF_SUPPLIER,
)
from .providers.base import (
    ExtractorError,
)
from .snapshot_store import (
    SNAPSHOT_REFRESH_HOURS,
    _SHARED_FAILURE_TTL,
    _SharedSnapshot,
    _resolve_snapshot,
    _shared_failed_fetches,
    _shared_lock,
    _shared_snapshots,
    _tuple_generation,
)

from datetime import datetime
from typing import TYPE_CHECKING, Any
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .providers.base import SupplierSnapshot


# A single failed fetch is almost always a transient CDN timeout that the
# next hourly tick recovers. Raising the user-facing "extractor failed"
# repair issue on the very first failure produced false alarms that wrongly
# told the user the supplier had changed its tariff layout. Only raise the
# issue once a failure has survived this many consecutive fetch attempts.
# The shared negative-fetch row carries the running count and it resets the
# moment a fetch succeeds; the 7-day snapshot_stale issue stays the backstop
# for a breakage that outlives every threshold.
_EXTRACTOR_ISSUE_THRESHOLD = 2


_LOGGER = logging.getLogger(__name__)


class _SnapshotMixin:
    """Mixed into BePricesCoordinator."""

    # Entry-owned state, declared as BARE annotations with no value. A valued
    # class attribute would change hasattr() and instance-dict behaviour;
    # __init__ in the concrete class is what actually creates these.
    entry: ConfigEntry
    _session: aiohttp.ClientSession
    _force_refresh: bool
    _store: Any
    _unloaded: bool
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _snapshot_fetched_at: datetime | None
    _snapshot_probe_key: str | None
    _last_error: str | None
    _supplier_tuple: tuple[str, str, str]

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator, and by the sibling mixins. Stubs
        # rather than inheritance: a mixin inheriting
        # DataUpdateCoordinator[CoordinatorData] would need CoordinatorData,
        # which lives in coordinator.py and is imported from there by sensor,
        # binary_sensor and diagnostics -- a cycle.
        hass: HomeAssistant

        def _sync_extractor_issue(
            self, message: str | None, *, transient: bool = False
        ) -> None: ...
        def _sync_deprecated_supplier_issue(self) -> None: ...

    def _refresh_custom_snapshot(self) -> None:
        """Build the snapshot locally for the expert custom supplier.

        There is no card to fetch: the user typed the formula and all
        regulated values, so we assemble the snapshot from the config entry
        every tick. Always fresh (no probe / TTL), so it never goes stale.
        """
        self._set_snapshot(
            build_custom_snapshot(
                self.entry.data,
                self.entry.data.get(CONF_REGION, ""),
                self.entry.data.get(CONF_DSO, ""),
            )
        )
        self._snapshot_fetched_at = dt_util.utcnow()
        self._last_error = ""

    def _shared_key(self) -> tuple[str, str, str]:
        return (
            self.entry.data[CONF_SUPPLIER],
            self.entry.data[CONF_CONTRACT],
            self.entry.data[CONF_REGION],
        )

    def _set_snapshot(self, snap: SupplierSnapshot | None) -> None:
        """Keep the card as parsed and resolve this entry's VAT preference.

        Every path that produces a snapshot - a fetch, a sibling's shared
        copy, the persisted cache, the custom-formula build - lands here,
        so the VAT choice is applied exactly once and never leaks into
        what other entries on the same tuple see.
        """
        self._snapshot_raw = snap
        self._snapshot = None if snap is None else _resolve_snapshot(self.entry, snap)

    def _adopt_shared(
        self,
        shared: _SharedSnapshot,
        probe_key: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Take a fresh shared snapshot as our own.

        When the freshness decision came from a PROBE key rather than the
        TTL, restamp ``fetched_at`` to now, exactly as the self-fresh branch
        below does and for the same reason: the probe just verified the
        supplier has not published a new card, so the snapshot is "checked
        just now", not "fetched whenever the cold fetch happened".

        This path is the one that runs in steady state. The shared row is
        normally this coordinator's OWN row, written by its cold fetch, and
        the shortcut is tried before the self-fresh branch, so leaving the
        stamp alone pinned it at the cold-fetch instant for as long as the
        supplier kept publishing the same card. Cards are monthly, so after
        seven days every probe-based supplier raised a false "snapshot
        stale" Repairs card, with `snapshot_age_hours` reading days while the
        card had been verified minutes earlier. Restamp the shared row too so
        siblings agree.

        A TTL-based match must NOT restamp: that would reset the TTL clock on
        every tick and a probe-less supplier would never be re-fetched.
        """
        self._set_snapshot(shared.snapshot)
        if probe_key is not None and now is not None:
            shared.fetched_at = now
            self._snapshot_fetched_at = now
        else:
            self._snapshot_fetched_at = shared.fetched_at
        self._snapshot_probe_key = shared.probe_key
        self._last_error = ""
        self._force_refresh = False

    async def _maybe_refresh_snapshot(self) -> None:
        """Run a cheap probe; only refetch the full PDF when it says so.

        Two paths depending on what the supplier exposes:

          * **Probe available** — call ``extractor.probe`` (HEAD or small
            listing GET). If the returned key matches what we last saved,
            the snapshot is still valid; just stamp ``_snapshot_fetched_at``
            and return. If the key changed, fall through to a real fetch.

          * **No probe** — fall back to the time-based TTL: only refetch
            when the snapshot is older than ``SNAPSHOT_REFRESH_HOURS`` (24h).
            DATS 24, Engie and Luminus take this path.

        The shared (supplier, contract, region) cache short-circuits the
        same way: a probe-key match against a sibling coordinator's
        snapshot adopts it without doing any work.
        """
        ttl = timedelta(hours=SNAPSHOT_REFRESH_HOURS)
        now = dt_util.utcnow()

        extractor = get_extractor(self.entry.data[CONF_SUPPLIER])
        contract = self.entry.data[CONF_CONTRACT]
        region = self.entry.data[CONF_REGION]
        key = self._shared_key()
        cache = _shared_snapshots(self.hass)

        # Try a cheap probe first. None means the supplier has no probe
        # path or the probe failed; we fall through to the TTL-only flow.
        probe_key: str | None = None
        probe_fn = getattr(extractor, "probe", None)
        if probe_fn is not None:
            try:
                probe_key = await probe_fn(self._session, contract, region)
            except (ExtractorError, asyncio.TimeoutError) as err:
                _LOGGER.debug(
                    "probe failed for %s/%s: %s",
                    self.entry.data.get(CONF_SUPPLIER),
                    contract,
                    err,
                )
                probe_key = None

        # Free, non-blocking shortcut: a sibling coordinator may have a
        # fresh snapshot we can adopt directly.
        shared = cache.get(key)
        if shared is not None and self._shared_is_fresh(shared, probe_key, now, ttl):
            self._adopt_shared(shared, probe_key, now)
            return

        # Our own snapshot may already be valid against this probe.
        if self._snapshot is not None and self._self_is_fresh(probe_key, now, ttl):
            if probe_key is not None:
                # Probe verified the supplier hasn't published a new card,
                # so refresh the snapshot_age sensor's clock to "just
                # checked". The probe-less / probe-failed path keeps the
                # original fetched_at; otherwise stamping it on every
                # tick that passes the TTL check resets the TTL clock
                # and the supplier is never re-fetched.
                self._snapshot_fetched_at = now
                # A successful probe also confirms the supplier is
                # reachable again, so clear any stale failure left by an
                # earlier transient fetch error. This path never
                # re-fetches, so without it a single-entry install (no
                # sibling to trigger _adopt_shared) would keep the "could
                # not reach the supplier" Repairs card and _last_error
                # until the published card changed. Emptying _last_error
                # lets the caller's top-level clear drop the extractor
                # issue; pop the negative-cache row so siblings stop
                # backing off. Gated on probe_key is not None: a failed /
                # absent probe is not proof of recovery.
                self._last_error = ""
                _shared_failed_fetches(self.hass).pop(key, None)
            # Populate the shared cache when this tick is the first to
            # verify a disk-loaded snapshot after restart. Without this
            # every sibling on the same tuple would re-run its own
            # probe / TTL check on every tick instead of adopting.
            # Re-use the previous probe_key when the current probe
            # came back empty (probe-less suppliers stay None; a
            # transiently-failing probe keeps the last known key).
            if (
                cache.get(key) is None
                and self._snapshot_raw is not None
                and self._snapshot_fetched_at is not None
            ):
                cache[key] = _SharedSnapshot(
                    snapshot=self._snapshot_raw,
                    fetched_at=self._snapshot_fetched_at,
                    probe_key=probe_key
                    if probe_key is not None
                    else self._snapshot_probe_key,
                )
            return

        # Negative cache: if a sibling just failed on this same key,
        # don't retry until _SHARED_FAILURE_TTL has elapsed. Propagate
        # the sibling's error to ours so a cold-start coordinator sees
        # the real failure reason instead of "cold start".
        # ``async_force_refresh`` raises ``_force_refresh`` and clears
        # *its own* view of the marker, but a sibling failing in the
        # window between the clear and this tick re-populates the row;
        # bypassing the short-circuit when ``_force_refresh`` is set
        # keeps the user-facing refresh service from silently no-op'ing.
        failed = _shared_failed_fetches(self.hass)
        if not self._force_refresh:
            last_fail = failed.get(key)
            if (
                last_fail is not None
                and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
            ):
                self._last_error = last_fail[1]
                return

        gen_at_entry = _tuple_generation(self.hass, key)
        async with _shared_lock(self.hass, key):
            shared = cache.get(key)
            locked_now = dt_util.utcnow()
            if shared is not None and self._shared_is_fresh(
                shared, probe_key, locked_now, ttl
            ):
                self._adopt_shared(shared, probe_key, locked_now)
                return
            # Re-check the negative cache under the lock so the second
            # waiter doesn't repeat what the first just failed; same
            # _force_refresh bypass as above.
            if not self._force_refresh:
                last_fail = failed.get(key)
                if (
                    last_fail is not None
                    and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
                ):
                    self._last_error = last_fail[1]
                    return
            try:
                snap = await extractor.fetch(self._session, contract, region)
                fetched_at = dt_util.utcnow()
                # Don't write the shared cache if the tuple was evicted
                # mid-fetch (entry removed or supplier swapped). Our
                # local self._snapshot is still useful for this tick;
                # if runtime_data was swapped, _save_persistent will
                # skip the write.
                if _tuple_generation(self.hass, key) == gen_at_entry:
                    cache[key] = _SharedSnapshot(
                        snapshot=snap, fetched_at=fetched_at, probe_key=probe_key
                    )
                    failed.pop(key, None)
                self._set_snapshot(snap)
                self._snapshot_fetched_at = fetched_at
                self._snapshot_probe_key = probe_key
                self._last_error = ""
                self._force_refresh = False
                self._sync_extractor_issue(None)
            except Exception as err:  # noqa: BLE001 - re-raised below for non-extractor types
                # Any extractor failure (including unexpected aiohttp /
                # parser exceptions) must populate the negative cache so
                # sibling coordinators back off instead of refiring the
                # same broken request on the next tick. The third tuple
                # field counts consecutive failures on this key so a lone
                # transient timeout doesn't immediately raise a repair
                # issue; the count rides the shared row and resets the
                # moment a fetch succeeds (failed.pop above).
                prev = failed.get(key)
                fail_count = (prev[2] if prev is not None else 0) + 1
                if _tuple_generation(self.hass, key) == gen_at_entry:
                    failed[key] = (dt_util.utcnow(), str(err), fail_count)
                self._last_error = str(err)
                # A transient network failure (timeout / reset / 5xx /
                # anti-bot 403) usually recovers on the next tick, so defer
                # its softer "could not reach the supplier" card until it
                # has crossed the threshold. A parse error / 404 / non-PDF
                # payload won't self-heal, so raise the actionable
                # "extractor failed" card on the first failure.
                transient = isinstance(
                    err, asyncio.TimeoutError
                ) or is_transient_fetch_error(str(err))
                if not transient:
                    self._sync_extractor_issue(str(err), transient=False)
                elif fail_count >= _EXTRACTOR_ISSUE_THRESHOLD:
                    self._sync_extractor_issue(str(err), transient=True)
                _LOGGER.warning(
                    "snapshot refresh failed for %s/%s: %s; keeping cached"
                    " (consecutive failure %d)",
                    self.entry.data.get(CONF_SUPPLIER),
                    self.entry.data.get(CONF_CONTRACT),
                    err,
                    fail_count,
                )
                if not isinstance(err, (ExtractorError, asyncio.TimeoutError)):
                    raise

    def _self_is_fresh(
        self, probe_key: str | None, now: datetime, ttl: timedelta
    ) -> bool:
        """Whether our own snapshot can be reused without a refetch."""
        if self._force_refresh:
            return False
        if probe_key is not None:
            return self._snapshot_probe_key == probe_key
        if self._snapshot_fetched_at is None:
            return False
        return now - self._snapshot_fetched_at < ttl

    def _shared_is_fresh(
        self,
        shared: _SharedSnapshot,
        probe_key: str | None,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        """Whether a sibling's shared snapshot can be adopted as-is.

        ``async_force_refresh`` flips ``_force_refresh`` to opt the
        coordinator out of every adoption shortcut: without this guard
        a sibling that re-seeded the shared cache between the
        ``_shared_snapshots.pop`` and the next tick would silently
        satisfy the forced refresh, making the user-facing refresh
        service a no-op on multi-entry installs.
        """
        if self._force_refresh:
            return False
        if probe_key is not None:
            return shared.probe_key == probe_key
        return now - shared.fetched_at < ttl

    def _snapshot_age_hours(self) -> float:
        if self._snapshot_fetched_at is None:
            return float("inf")
        return (dt_util.utcnow() - self._snapshot_fetched_at).total_seconds() / 3600.0
