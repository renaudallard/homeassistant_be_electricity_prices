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

"""The Synergrid load and production profiles, and the means they weight.

Two annual workbooks published per DSO blend, shared across every entry that
names the same blend and persisted so a restart does not re-download tens of
megabytes. What they buy is a weighted monthly mean: a household on an
RLP-indexed card is not billed the plain average of the month's spots but
the average weighted by when a Belgian home actually draws power.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from typing import Any
from .synergrid import RLP_BLENDS
from .synergrid import RlpWeights
from datetime import datetime
from homeassistant.util import dt as dt_util
from .synergrid import fetch_rlp_blends
from .synergrid import fetch_spp_weights
from datetime import timedelta
from .const import DOMAIN
from .spot_stats import _rlp_weighted_month_mean
from .spot_stats import _spp_weighted_month_mean
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import aiohttp
import asyncio


class _ProfilesMixin:
    """Mixed into _SpotsMixin."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _rlp_blend: str
    _rlp_blend_weights: dict[str, RlpWeights]
    _rlp_failed_at: datetime | None
    _rlp_fetched_at: datetime | None
    _rlp_weights: Any
    _rlp_weights_year: int | None
    _session: aiohttp.ClientSession
    _spp_failed_at: datetime | None
    _spp_fetched_at: datetime | None
    _spp_weights: Any
    _spp_weights_year: int | None
    hass: HomeAssistant

    if TYPE_CHECKING:
        # Provided by the concrete class; stubs rather than inheritance,
        # which would be a cycle.
        def _billable_spots(
            self, extra_spots: dict[datetime, float]
        ) -> dict[datetime, float]: ...

    async def _shared_profile(
        self,
        kind: str,
        year: int,
        blend: str,
        refresh_days: int,
        fetch: Any,
        *args: Any,
    ) -> tuple[Any, datetime]:
        """Fetch one Synergrid profile at most once per process, per key.

        The two callers own their own freshness and back-off state, which is
        per entry and persisted; this layer is only about not downloading the
        same national curve twice. A cached row is re-used while it is younger
        than the caller's own refresh window, so a second entry starting a
        month later still gets a fresh file rather than the first entry's.

        The lock is what makes the deferral in ``_update_body`` safe: several
        entries schedule their background fill at the same moment, and without
        it a Raspberry Pi would run N downloads and N xlsb parses at once.
        """
        key = (kind, year, blend)
        cache = _profile_cache(self.hass)
        now = dt_util.utcnow()
        async with _profile_lock(self.hass, key):
            row = cache.get(key)
            if row is not None and (now - row[1]) < timedelta(days=refresh_days):
                # Handed back with the row's own stamp: the caller stamped an
                # adopted row as fetched today, so a row adopted on its 29th
                # day was held for 30 more and a revised profile reached that
                # entry a month late.
                return row[0], row[1]
            weights = await fetch(self._session, *args)
            if weights:
                cache[key] = (weights, now)
                await _save_profile_cache(self.hass)
            return weights, now

    async def _ensure_spp_weights(self) -> None:
        """Refresh the Synergrid SPP profile for the current year if stale.

        Only called for an entry whose injection is SPP-weighted (a card
        indexed on Belpex_SPP, or a custom entry that opted in). The ex-ante
        file is revised in-year, so re-fetch monthly. Soft-fail: on error keep
        whatever we already have (the opt-in caller then degrades to the plain
        arithmetic mean, an SPP-indexed card to its printed indicative) and
        back off ``_SPP_RETRY_TTL`` so a persistent failure
        doesn't re-download the 52 MB workbook every tick.
        """
        now = dt_util.utcnow()
        year = dt_util.now().year
        fresh = (
            self._spp_weights_year == year
            and self._spp_fetched_at is not None
            and (now - self._spp_fetched_at) < timedelta(days=_SPP_REFRESH_DAYS)
        )
        if fresh:
            return
        if (
            self._spp_failed_at is not None
            and (now - self._spp_failed_at) < _SPP_RETRY_TTL
        ):
            return
        weights, fetched_at = await self._shared_profile(
            "spp", year, "", _SPP_REFRESH_DAYS, fetch_spp_weights, year
        )
        if weights:
            self._spp_weights = weights
            self._spp_weights_year = year
            self._spp_fetched_at = fetched_at
            self._spp_failed_at = None
        else:
            self._spp_failed_at = now

    async def _shared_rlp_blends(
        self, year: int
    ) -> tuple[dict[str, RlpWeights], dict[str, datetime]]:
        """Every RLP blend for ``year``, downloaded at most once per process.

        The sibling of :meth:`_shared_profile` for the one profile that has
        more than one reduction. All of them come out of a single download and
        a single workbook read (``fetch_rlp_blends``), so holding the two the
        entry does not itself bill on costs a few seconds of CPU rather than
        another 3,4 MB. The compare page needs them: it prices foreign cards,
        and a card is billed on the index its own card names.

        One lock for the file rather than one per blend, so two entries
        starting together still share the single download.
        """
        cache = _profile_cache(self.hass)
        now = dt_util.utcnow()
        async with _profile_lock(self.hass, ("rlp", year, "")):
            held: dict[str, RlpWeights] = {}
            stamps: dict[str, datetime] = {}
            missing: list[str] = []
            for blend in RLP_BLENDS:
                row = cache.get(("rlp", year, blend))
                if row is not None and (now - row[1]) < timedelta(
                    days=_RLP_REFRESH_DAYS
                ):
                    held[blend] = row[0]
                    stamps[blend] = row[1]
                else:
                    missing.append(blend)
            if missing:
                fetched = await fetch_rlp_blends(self._session, year, missing)
                for blend, weights in fetched.items():
                    cache[("rlp", year, blend)] = (weights, now)
                    held[blend] = weights
                    stamps[blend] = now
                if fetched:
                    await _save_profile_cache(self.hass)
            return held, stamps

    async def _ensure_rlp_weights(self, blend: str = "distinct") -> None:
        """Refresh the Synergrid RLP profile for the current year if stale.

        Called for an entry whose ENERGY leg resolves against the RLP-weighted
        month mean (Eneco Flex on the distinct-curve mean, Energy Knights on
        the Fluvius curve, energie.be on the column mean) and for every entry
        on the compensation regime, whose yearly net is spread over the year by
        the profile. ``blend`` names the entry's own reduction, which is the one
        every sensor beside it reads; the others come off the same read and are
        held for the compare page. Soft-fail like the SPP profile: on error keep
        what is held, back off ``_RLP_RETRY_TTL``, and the caller prices the
        plain mean, or the metered slices, meanwhile.
        """
        now = dt_util.utcnow()
        year = dt_util.now().year
        fresh = (
            self._rlp_weights_year == year
            and self._rlp_blend == blend
            and self._rlp_fetched_at is not None
            and (now - self._rlp_fetched_at) < timedelta(days=_RLP_REFRESH_DAYS)
        )
        if fresh:
            return
        if (
            self._rlp_blend == blend
            and self._rlp_failed_at is not None
            and (now - self._rlp_failed_at) < _RLP_RETRY_TTL
        ):
            return
        held, stamps = await self._shared_rlp_blends(year)
        if held.get(blend):
            self._rlp_blend_weights = held
            self._rlp_weights = held[blend]
            self._rlp_weights_year = year
            self._rlp_blend = blend
            # The row's own stamp, not now (see _shared_profile).
            self._rlp_fetched_at = stamps[blend]
            self._rlp_failed_at = None
        else:
            self._rlp_failed_at = now

    def rlp_weights_for_blend(self, blend: str) -> RlpWeights | None:
        """The held RLP curve for ``blend``, or ``None`` when it is not held.

        A card is billed on the index its own card names, so the compare page
        asks for the blend of the side it is pricing rather than reading the
        entry's own. ``None`` means this process has not loaded that reduction,
        and the caller falls back to the plain arithmetic mean exactly as an
        entry with no profile at all does: a foreign blend is a different index,
        not a coarser one, and standing in for it understates or overstates
        that card alone.

        The entry's own blend answers from ``_rlp_weights`` when the map has
        not been filled, so asking for it is never worse than reading that
        attribute directly.
        """
        held = self._rlp_blend_weights.get(blend)
        if not held and blend == self._rlp_blend:
            held = self._rlp_weights
        return held or None

    def _rlp_weighted_month_mean(
        self,
        year: int,
        month: int,
        extra_spots: dict[datetime, float],
        blend: str | None = None,
    ) -> float | None:
        """RLP-weighted mean of the delivery month's Day-Ahead spots, or None.

        Weights each hourly price by the residential load profile's share for
        its local clock hour, which is Eneco's Belpex-RLP-M. Same
        local-delivery-month filter as :meth:`_monthly_spot_mean`; ``None``
        when the profile or the month's spots are unavailable, and the caller
        falls back to the plain mean.

        ``blend`` names which reduction to weight by, for a caller pricing a
        card other than the entry's own; the default is the entry's.
        """
        weights = (
            self._rlp_weights if blend is None else self.rlp_weights_for_blend(blend)
        )
        if not weights:
            return None
        return _rlp_weighted_month_mean(
            self._billable_spots(extra_spots), weights, year, month
        )

    def _spp_weighted_month_mean(
        self, year: int, month: int, extra_spots: dict[datetime, float]
    ) -> float | None:
        """SPP-weighted mean of the delivery month's Day-Ahead spots, or None.

        Weights each hourly price by the Synergrid solar production profile so
        the injection index matches an SPP-indexed contract. Uses the same
        local-delivery-month filter as :meth:`_monthly_spot_mean`. Returns
        ``None`` (caller falls back to the plain mean) when the profile or the
        month's spots are unavailable.
        """
        if not self._spp_weights:
            return None
        return _spp_weighted_month_mean(
            self._billable_spots(extra_spots), self._spp_weights, year, month
        )


def _profile_cache(
    hass: HomeAssistant,
) -> dict[tuple[str, int, str], tuple[Any, datetime]]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_PROFILE_CACHE_KEY, {})  # type: ignore[no-any-return]


def _profile_lock(hass: HomeAssistant, key: tuple[str, int, str]) -> asyncio.Lock:
    """One lock per profile so concurrent entries fetch it once, not N times."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, int, str], asyncio.Lock] = bucket.setdefault(
        _PROFILE_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


def _profile_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """The one file both Synergrid profiles are kept in.

    Per HASS, not per entry, because the curves are national: every entry that
    wants one wants the same bytes. They used to ride the per-entry cache, which
    is rewritten whole on every hourly tick, so a 193 KB curve that changes once
    a month was written 24 times a day, once per entry that held it. With three
    RLP blends kept for the compare page that had reached 771 KB a tick and 18 MB
    a day on hardware that is usually a Raspberry Pi writing to an SD card.

    Here it is written only when a profile is actually fetched, which is monthly,
    and shared rather than copied per entry.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    store = bucket.get(_PROFILE_STORE_KEY)
    if store is None:
        store = Store(hass, _PROFILE_STORE_VERSION, f"{DOMAIN}_profiles")
        bucket[_PROFILE_STORE_KEY] = store
    return store


async def _load_profile_cache(hass: HomeAssistant) -> None:
    """Fill the in-process cache from the shared store, once per HASS.

    Under a lock, because entries load in parallel: without it the second entry
    reads the flag before the first has finished reading the file, finds an
    empty cache, and seeds its own legacy blob over rows the store was about to
    supply. The same curve either way, but the stamps differ.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    async with _profile_lock(hass, ("store", 0, "")):
        if bucket.get(_PROFILE_LOADED_KEY):
            return
        bucket[_PROFILE_LOADED_KEY] = True
        blob = await _profile_store(hass).async_load()
        if not blob:
            return
        cache = _profile_cache(hass)
        for row in blob.get("profiles", []):
            try:
                key = (str(row["kind"]), int(row["year"]), str(row["blend"]))
                fetched = datetime.fromisoformat(str(row["fetched_at"]))
            except (KeyError, TypeError, ValueError):
                continue
            weights = _parse_curve(row.get("weights"))
            if weights and key not in cache:
                cache[key] = (weights, fetched)


async def _save_profile_cache(hass: HomeAssistant) -> None:
    """Write the cache back. Called after a fetch, so monthly rather than
    hourly; the rows are national and there is one file for all entries."""
    cache = _profile_cache(hass)
    await _profile_store(hass).async_save(
        {
            "profiles": [
                {
                    "kind": kind,
                    "year": year,
                    "blend": blend,
                    "fetched_at": fetched.isoformat(),
                    "weights": {
                        ",".join(str(x) for x in slot): value
                        for slot, value in weights.items()
                    },
                }
                for (kind, year, blend), (weights, fetched) in cache.items()
                if weights
            ]
        }
    )


def _parse_curve(raw: Any) -> dict[tuple[int, ...], float]:
    """One persisted curve back into slot-keyed weights, skipping bad rows."""
    out: dict[tuple[int, ...], float] = {}
    if not isinstance(raw, dict):
        return out
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, (int, float)):
            continue
        try:
            out[tuple(int(x) for x in key.split(","))] = float(value)
        except ValueError:
            continue
    return out


def _seed_profile_cache(hass: HomeAssistant, kind: str, blob: dict[str, Any]) -> bool:
    """Adopt a profile from a per-entry blob written before the shared store.

    Carried for one release so an upgrade does not re-download what the entry
    already had. Two shapes have been written: one curve under ``weights``, and
    from 0.23.2 one per blend under ``blends``. Nothing writes either any more.

    A curve already in the cache wins, since that one came from the shared store
    and is what the whole installation agreed on; several entries seed this from
    their own blobs. Returns whether anything was added, so the caller can write
    the shared store once and stop the next restart from downloading again.
    """
    year = blob.get("year")
    fetched = blob.get("fetched_at")
    if not isinstance(year, int):
        return False
    try:
        stamp = datetime.fromisoformat(str(fetched))
    except (TypeError, ValueError):
        return False
    curves: dict[str, Any] = {}
    blends = blob.get("blends")
    if isinstance(blends, dict):
        curves = blends
    else:
        blend = blob.get("blend")
        curves = {blend if isinstance(blend, str) else "": blob.get("weights")}
    cache = _profile_cache(hass)
    seeded = False
    for blend, raw in curves.items():
        if not isinstance(blend, str):
            continue
        weights = _parse_curve(raw)
        if weights and (kind, year, blend) not in cache:
            cache[(kind, year, blend)] = (weights, stamp)
            seeded = True
    return seeded


_SPP_REFRESH_DAYS = 30
# Back off this long after a failed SPP fetch so a persistent problem (e.g. the
# new-year file not yet published) doesn't re-download 52 MB every hourly tick.
_SPP_RETRY_TTL = timedelta(hours=12)
# The RLP profile is one estimated curve per year, revised rarely; the same
# monthly refresh and failure back-off as the SPP profile apply.
_RLP_REFRESH_DAYS = 30
_RLP_RETRY_TTL = timedelta(hours=12)
# One Synergrid profile serves every entry: it is a national curve, keyed by
# year (and, for the RLP, by DSO blend), with nothing per-household in it.
# Each coordinator used to download and parse its own copy: 18 s for the
# 3,4 MB RLP workbook on a Raspberry Pi, and deferring that fetch to a
# background task made it worse rather than better, because every entry then
# started its copy at the same moment instead of one after another. Rows carry
# the instant they were fetched so the freshness rule below is applied to the
# row and a stale one is not handed on.
_PROFILE_CACHE_KEY = "synergrid_profile_cache"
_PROFILE_LOCKS_KEY = "synergrid_profile_locks"
_PROFILE_STORE_KEY = "synergrid_profile_store"
_PROFILE_LOADED_KEY = "synergrid_profile_loaded"
# Bumped only if the row shape below changes; a blob from another version is
# dropped and the profiles are downloaded again, which costs one file.
_PROFILE_STORE_VERSION = 1
