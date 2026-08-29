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
from .providers import get as get_extractor
from .providers.custom import build_snapshot as build_custom_snapshot
from .providers._pdf import is_transient_fetch_error
from .providers.base import CardNotReadableError

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
    _SharedSnapshot,
    fetch_shared,
    _resolve_snapshot,
    _shared_failed_fetches,
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
            self,
            message: str | None,
            *,
            transient: bool = False,
            unreadable: bool = False,
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
        result = await fetch_shared(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data[CONF_REGION],
            supplier=self.entry.data[CONF_SUPPLIER],
            # Our own row is offered as a cache entry of equal standing, so the
            # freshness rule lives in one place instead of here as well. Built
            # from _snapshot_raw, never _snapshot: the resolved copy carries
            # this entry's VAT preference, and seeding the shared cache from it
            # would mis-price every sibling on the tuple.
            local=(
                _SharedSnapshot(
                    snapshot=self._snapshot_raw,
                    fetched_at=self._snapshot_fetched_at,
                    probe_key=self._snapshot_probe_key,
                )
                if self._snapshot_raw is not None
                and self._snapshot_fetched_at is not None
                else None
            ),
            force=self._force_refresh,
        )

        if result.source == "backoff":
            # A sibling failed on this tuple moments ago. Take its reason, so a
            # cold-start coordinator reports the real failure rather than "cold
            # start", and leave the snapshot alone.
            self._last_error = result.error_message
            return

        if result.source == "local" and result.row is not None:
            # Our own row stood. Nothing to re-resolve: the snapshot already IS
            # this entry's, and putting it back through _set_snapshot would
            # resolve VAT a second time on every quiet tick. Only the clock
            # moves, and only when a probe actually answered -- stamping it on
            # a TTL match would push the expiry out every tick and the supplier
            # would never be re-fetched at all.
            self._snapshot_fetched_at = result.row.fetched_at
            # Clearing a stale error here is gated on the probe for the same
            # reason: a TTL match says our row has not expired, not that the
            # supplier is reachable, and a failed or absent probe is not proof
            # of recovery. Without this a single-entry install would keep a
            # "could not reach the supplier" card until the published card
            # changed; with it relaxed, one would clear while the supplier was
            # still down.
            if result.probe_confirmed:
                self._last_error = ""
                _shared_failed_fetches(self.hass).pop(self._shared_key(), None)
            return

        if result.row is not None:
            self._set_snapshot(result.row.snapshot)
            self._snapshot_fetched_at = result.row.fetched_at
            self._snapshot_probe_key = result.row.probe_key
            self._last_error = ""
            # No pop here. A successful fetch already clears the negative row
            # inside fetch_shared, and the ADOPT arm must not: adopting a
            # sibling's card says nothing about whether the supplier answered
            # us, so resetting the consecutive-failure counter there delays
            # the "could not reach the supplier" card, or suppresses it while
            # a quiet sibling keeps re-adopting. Clearing _last_error is
            # right, and is what the arm this replaced did.
            if result.source == "fetch":
                # Only a real fetch satisfies a forced refresh, and only a real
                # fetch clears the extractor issue.
                self._force_refresh = False
                self._sync_extractor_issue(None)
            return

        err = result.error
        assert err is not None
        self._last_error = result.error_message
        # A transient network failure (timeout / reset / 5xx / anti-bot 403)
        # usually recovers on the next tick, so defer its softer "could not
        # reach the supplier" card until it has crossed the threshold. A parse
        # error / 404 / non-PDF payload will not self-heal, so raise the
        # actionable "extractor failed" card on the first failure.
        transient = isinstance(err, asyncio.TimeoutError) or is_transient_fetch_error(
            result.error_message
        )
        # A card with no text layer is a third case: it downloaded fine and no
        # parser change can read it, so the user needs the workaround rather
        # than a request to report a layout change. Derived from THIS download,
        # so it stops by itself when the supplier publishes text again.
        unreadable = isinstance(err, CardNotReadableError)
        if not transient:
            self._sync_extractor_issue(
                result.error_message, transient=False, unreadable=unreadable
            )
        elif result.fail_count >= _EXTRACTOR_ISSUE_THRESHOLD:
            self._sync_extractor_issue(result.error_message, transient=True)
        _LOGGER.warning(
            "snapshot refresh failed for %s/%s: %s; keeping cached"
            " (consecutive failure %d)",
            self.entry.data.get(CONF_SUPPLIER),
            self.entry.data.get(CONF_CONTRACT),
            result.error_message,
            result.fail_count,
        )
        if not isinstance(err, (ExtractorError, asyncio.TimeoutError)):
            raise err

    def _snapshot_age_hours(self) -> float:
        if self._snapshot_fetched_at is None:
            return float("inf")
        return (dt_util.utcnow() - self._snapshot_fetched_at).total_seconds() / 3600.0
