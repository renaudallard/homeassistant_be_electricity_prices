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

"""ENTSO-E day-ahead fetching and the historical spot cache.

Split out of coordinator.py. Calls nothing it does not own, so it needs no
cross-mixin stubs.

Note the deliberate name shadow: the module-level _spp_weighted_month_mean in
spot_stats and the method of the same name here. It is imported plainly rather
than aliased so the call sites read unchanged."""

from __future__ import annotations

from datetime import UTC, timedelta

import logging

from .api import (
    EntsoeAuthError,
    EntsoeError,
    fetch_day_ahead_or_fallback,
)
from .const import (
    CONF_API_KEY,
)
from .injection import (
    _injection_needs_spot_quarters,
)
from .spot_stats import (
    _bucket_spots_by_hour,
    _drop_future_spots,
    _energy_is_quarter_hourly,
    _group_spot_quarters_by_hour,
    _mean_of_month,
    _spp_weighted_month_mean,
)
from .synergrid import (
    SppWeights,
    fetch_spp_weights,
)

from collections.abc import Mapping
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, TypeVar
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .providers.base import SupplierSnapshot


# Some past days genuinely have < 20 of 24 hourly day-ahead points at
# ENTSO-E (source gaps). Without a marker, _ensure_historical_spots
# re-pulls a whole week-chunk for such a day on every hourly tick for
# the rest of the year. Record the last attempt per stable past day and
# skip it for this long; 12 h re-attempts twice a day in case the data
# lands late, without hammering the rate-limited endpoint hourly.
_SHORT_SPOT_DAY_TTL = timedelta(hours=12)

# The Synergrid ex-ante SPP profile is revised within the year, so re-fetch the
# 52 MB workbook at most this often (weights survive restarts via the Store).
# A day-ahead price that could not have come from ENTSO-E, in EUR/kWh. The
# harmonised EU clearing limits are -500 to +4000 EUR/MWh, so anything outside
# this band did not come off the wire as published: the usual cause is a value
# stored in EUR/MWh where the rest of the cache is EUR/kWh, which lands three
# orders of magnitude out.
#
# This exists because a persisted spot was trusted forever. _ensure_historical_spots
# only fetches a day holding fewer than 20 of its 24 hours, so a day that is
# COMPLETE but wrong was never revisited, and nothing else clears the cache
# before the year-end prune. One bad value therefore skewed a dynamic
# contract's whole year-to-date bill for as long as the entry existed, with no
# way for the user to correct it short of deleting the entry.
_SPOT_SANE_MIN = -1.0
_SPOT_SANE_MAX = 5.0


_CacheValue = TypeVar("_CacheValue")


def _spots_for_local_days(
    spots: Mapping[datetime, float], days: set[date]
) -> dict[datetime, float]:
    """Keep only the slots whose LOCAL day is one of ``days``.

    Slot keys are UTC, but what makes a curve current is the Brussels day it
    prices, so the day is resolved locally before it is compared.
    """
    return {
        slot: value
        for slot, value in spots.items()
        if dt_util.as_local(slot).date() in days
    }


def _drop_hours_before(cache: dict[datetime, _CacheValue], cutoff: datetime) -> None:
    """Delete every hour older than ``cutoff`` from ``cache``, in place.

    In place rather than by rebuilding and rebinding: _ensure_historical_spots
    merges each fetched chunk into the attribute and re-resolves it after every
    await, so a prune landing between two chunks (the tick calls it from
    _save_persistent while a backfill is mid-fetch) would discard everything
    the earlier chunks had already merged into the old dict.
    """
    for stale_hour in [h for h in cache if h < cutoff]:
        del cache[stale_hour]


def _spot_is_sane(value: float) -> bool:
    """Whether a cached day-ahead price could have been published.

    Deliberately wide. The point is to catch a value on the wrong scale, not to
    second-guess the market: negative prices are ordinary in Belgium and
    scarcity hours run into thousands of EUR/MWh, and rejecting a real price
    would silently drop a genuinely expensive hour from the bill.
    """
    return _SPOT_SANE_MIN <= value <= _SPOT_SANE_MAX


_SPP_REFRESH_DAYS = 30
# Back off this long after a failed SPP fetch so a persistent problem (e.g. the
# new-year file not yet published) doesn't re-download 52 MB every hourly tick.
_SPP_RETRY_TTL = timedelta(hours=12)


_LOGGER = logging.getLogger(__name__)


class _SpotsMixin:
    """Mixed into BePricesCoordinator."""

    # Entry-owned state, declared as BARE annotations with no value. A valued
    # class attribute would change hasattr() and instance-dict behaviour;
    # __init__ in the concrete class is what actually creates these.
    entry: ConfigEntry
    _session: aiohttp.ClientSession
    _historical_spots: dict[datetime, float]
    _historical_spot_quarters: dict[datetime, list[float]]
    _spot_cache: dict[datetime, float]
    _spot_cache_day: date | None
    _spot_source: str
    _spot_cache_includes_tomorrow: bool
    _short_spot_days: dict[date, datetime]
    _spp_weights: Any
    _spp_fetched_at: datetime | None
    _spp_failed_at: datetime | None
    _spp_weights_year: int | None
    _complete_spot_days: set[date]
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

    def _cached_spot_hours(self, day_start_utc: datetime, want_quarters: bool) -> int:
        """How many of a local day's 24 UTC hours the replay can actually price.

        A floored feed-in formula replays off the hour's individual quarters,
        so for that entry a day is covered only once the quarter cache holds
        it: the hourly mean alone cannot say what the four quarters were. The
        quarter cache is only ever written beside the hourly one, so this is a
        subset test, never a wider one.

        Both the pre-fetch scan and the post-fetch recount call this. If the
        two ever measured different things a day could read short before a
        fetch and complete after it, and it would be re-fetched every tick
        forever.
        """
        cache: Mapping[datetime, object] = (
            self._historical_spot_quarters if want_quarters else self._historical_spots
        )
        return sum(1 for h in range(24) if day_start_utc + timedelta(hours=h) in cache)

    async def _ensure_historical_spots(
        self, start: date, end: date, api_key: str | None = None
    ) -> None:
        """Make sure ``self._historical_spots`` covers every hour of the
        local days in ``[start, end]``, fetching missing ranges from
        ENTSO-E.

        ``api_key`` overrides the entry's key, letting the compare flow
        backfill spots for a spot-indexed target with a key the user typed
        in the compare step even when their own entry carries none.

        Day boundaries are anchored on local midnight (converted to UTC),
        matching the recorder window (``_recorder_rows``) and the
        persistence cut-off (``_save_persistent``). Anchoring on UTC
        midnight instead would leave the first one or two hours of the
        local year (local Jan 1 00:00 falls on Dec 31 UTC in Brussels)
        unfetched, so the dynamic YTD would never credit them even though
        the recorder reports consumption there.

        Walks the day axis once. A day is considered "present" when at
        least 20 of its 24 hours are already cached -- ENTSO-E
        occasionally leaves gaps under the carry-forward rule (and DST
        seam days have 23/25 hours), and a few missing hours per day
        shouldn't trigger a re-fetch every coordinator tick. Failed
        fetches are logged and skipped; the caller treats absent hours as
        "no data" rather than tearing the YTD computation down.
        """
        snap = self._snapshot
        # Both decisions are read here, before the day walk, because the walk
        # measures coverage against whichever cache this entry replays from.
        quarter_hourly = snap is not None and _energy_is_quarter_hourly(snap.energy)
        want_quarters = snap is not None and _injection_needs_spot_quarters(
            snap, self.entry
        )
        if snap is not None and not want_quarters:
            # The entry stopped needing the slots. Unticking the quarter-hourly
            # box or the never-negative one, or leaving the injection regime,
            # changes none of the (supplier, contract, region) tuple the reload
            # is gated on, so a cached year would be restored and re-persisted
            # for as long as the entry lived, and the replay would keep
            # crediting those hours per slot while the sensor beside it
            # credits the hour. Cleared here, above the key check, because an
            # entry with no key still replays whatever is already cached.
            self._historical_spot_quarters.clear()
        api_key = api_key or self.entry.data.get(CONF_API_KEY)
        if not api_key:
            return
        now = dt_util.utcnow()
        # Days older than this are stable enough that a short fetch means
        # a genuine source gap, not data still being published; only those
        # get the "attempted, still short" skip marker. Today and yesterday
        # are always re-fetched so their hours fill in promptly.
        stable_before = dt_util.now().date() - timedelta(days=1)
        # Collect contiguous date ranges where the cache is sparse.
        missing_ranges: list[tuple[date, date]] = []
        range_start: date | None = None
        cur = start
        while cur <= end:
            if cur in self._complete_spot_days:
                # Confirmed fully covered on an earlier tick. Treat as present
                # (so it closes any open missing range) without redoing the tz
                # conversion and 24 dict lookups.
                present = 24
            else:
                day_start_utc = dt_util.start_of_local_day(cur).astimezone(UTC)
                present = self._cached_spot_hours(day_start_utc, want_quarters)
                # >= 20 is the same threshold the fetch decision below uses, so
                # a day recorded here is one that would never be re-fetched
                # anyway; caching it just skips the scan next tick.
                if present >= 20:
                    self._complete_spot_days.add(cur)
            last_attempt = self._short_spot_days.get(cur)
            recently_short = (
                present < 20
                and last_attempt is not None
                and now - last_attempt < _SHORT_SPOT_DAY_TTL
            )
            if present < 20 and not recently_short:
                if range_start is None:
                    range_start = cur
            elif range_start is not None:
                missing_ranges.append((range_start, cur))
                range_start = None
            cur += timedelta(days=1)
        if range_start is not None:
            missing_ranges.append((range_start, cur))
        if not missing_ranges:
            return
        # ``quarter_hourly`` asks for the same grid the contract settles on,
        # exactly as the live fetch does. ENTSO-E publishes Belgium as two
        # products, a PT60M and a PT15M series for the same delivery period,
        # and parse_day_ahead_xml deliberately refuses to blend them: omitting
        # the flag here silently took the hourly product, so a quarter-hourly
        # contract's whole replay was priced off a different auction than its
        # live bill.
        for r_start, r_end in missing_ranges:
            chunk_start = r_start
            while chunk_start < r_end:
                # Week-sized chunks: trade off per-request latency
                # against total round-trips for a 365-day backfill.
                chunk_end = min(chunk_start + timedelta(days=7), r_end)
                # Local-midnight anchors (in UTC) so the fetched window
                # lines up with the local-day grid the recorder and the
                # present-check above use.
                start_utc = dt_util.start_of_local_day(chunk_start).astimezone(UTC)
                end_utc = dt_util.start_of_local_day(chunk_end).astimezone(UTC)
                try:
                    prices, source = await fetch_day_ahead_or_fallback(
                        api_key,
                        self._session,
                        start_utc,
                        end_utc,
                        quarter_hourly=quarter_hourly,
                    )
                    if source != "entsoe":
                        self._spot_source = source
                except (EntsoeError, EntsoeAuthError) as err:
                    _LOGGER.warning(
                        "ENTSO-E historical fetch failed for %s..%s: %s",
                        chunk_start,
                        chunk_end,
                        err,
                    )
                    if isinstance(err, EntsoeAuthError):
                        # This class covers a rejected key, an exhausted daily
                        # quota, and a window ENTSO-E acknowledges with no
                        # matching data, which for a PAST chunk can simply mean
                        # the data does not exist. None of the three is fixed
                        # by asking again in an hour, and a failed fetch leaves
                        # each day exactly as short as it was, so with no marker
                        # the whole year is re-pulled on every hourly tick and
                        # logs a warning per chunk for as long as the entry
                        # exists. Mark this chunk's stable past days so the TTL
                        # backs that off to twice a day. Today and yesterday
                        # stay unmarked, their data is still landing. A plain
                        # EntsoeError is a timeout or a 5xx, which the next tick
                        # should retry promptly rather than sit out the TTL.
                        day = chunk_start
                        while day < chunk_end:
                            if day < stable_before:
                                self._short_spot_days[day] = now
                            day += timedelta(days=1)
                    chunk_start = chunk_end
                    continue
                # Stored by clock hour whichever grid came back: the
                # recorder only keeps hourly consumption, so an hour is the
                # finest thing the replay can ever price. An entry whose
                # feed-in formula is floored keeps the hour's own slots too,
                # because that formula is not linear and its mean does not
                # price it.
                self._historical_spots.update(_bucket_spots_by_hour(prices))
                if want_quarters:
                    self._historical_spot_quarters.update(
                        _group_spot_quarters_by_hour(prices)
                    )
                # Mark stable past days that are STILL short after this
                # fetch so the next ticks skip them until the TTL expires;
                # clear the marker for any day that is now complete.
                day = chunk_start
                while day < chunk_end:
                    ds_utc = dt_util.start_of_local_day(day).astimezone(UTC)
                    got = self._cached_spot_hours(ds_utc, want_quarters)
                    if got < 20 and day < stable_before:
                        self._short_spot_days[day] = now
                    else:
                        self._short_spot_days.pop(day, None)
                    day += timedelta(days=1)
                chunk_start = chunk_end

    async def _fetch_spot_prices(self) -> dict[datetime, float]:
        api_key = self.entry.data.get(CONF_API_KEY)
        if not api_key:
            raise EntsoeError("missing ENTSO-E API key")

        # Window the request on the *local* day (Europe/Brussels) so a
        # 00:00-02:00 local query doesn't drop yesterday's UTC tail or
        # miss tomorrow because UTC is still on the previous date.
        local_today = dt_util.now().date()
        now_local = dt_util.now()
        want_tomorrow = now_local.hour >= 11
        if (
            self._spot_cache_day == local_today
            and (not want_tomorrow or self._spot_cache_includes_tomorrow)
            and self._spot_cache
        ):
            return self._spot_cache

        # Anchor both endpoints on local midnight so the fetched UTC
        # window matches the actual local-day hour count. A naive
        # ``end = start + timedelta(days=N)`` adds 24 UTC hours and
        # falls one hour short on the fall-back Sunday (local day has
        # 25 hours), so the last local hour ends up missing from the
        # spot cache. Same anchoring as ``_recorder_rows`` uses for the
        # recorder window.
        start = dt_util.start_of_local_day(local_today).astimezone(UTC)
        days = 2 if want_tomorrow else 1
        end = dt_util.start_of_local_day(local_today + timedelta(days=days)).astimezone(
            UTC
        )
        # Keep the native 15-minute slots only for suppliers that bill on
        # them (Engie Dynamic); everyone else gets the hourly aggregate.
        snap = self._snapshot
        quarter_hourly = snap is not None and _energy_is_quarter_hourly(snap.energy)
        prices, self._spot_source = await fetch_day_ahead_or_fallback(
            api_key, self._session, start, end, quarter_hourly=quarter_hourly
        )
        self._spot_cache = prices
        self._spot_cache_day = local_today
        # Flag what the response actually carries, not what we asked
        # for: ENTSO-E publishes the day-ahead curve around 12-13 CET,
        # so a tick that requests tomorrow before publication comes
        # back with today only. Locking the flag to True on intent
        # would block the next hourly tick from retrying and tomorrow's
        # prices wouldn't surface until local midnight (reloading the
        # entry was the only way out).
        tomorrow = local_today + timedelta(days=1)
        self._spot_cache_includes_tomorrow = any(
            dt_util.as_local(h).date() == tomorrow for h in prices
        )
        return prices

    def _fallback_spots(self) -> dict[datetime, float]:
        """The best still-valid curve to price with when ENTSO-E is down.

        Prefers this entry's own day-ahead cache: it is at the resolution the
        contract bills on, and it is the only thing that ever holds tomorrow.
        Falls back to the persisted year-to-date cache, which is hourly. The
        two are never merged -- a quarter-hourly entry topped up with hourly
        means would price its slots off two different day-ahead products.

        Only today's and tomorrow's slots survive, and a source that cannot
        price today is skipped outright rather than contributing what it has,
        so a curve left over from an earlier day is never served as the
        current one. Returning empty is a real answer: the caller fails the
        tick instead of pricing off a stale number.
        """
        local_today = dt_util.now().date()
        wanted = {local_today, local_today + timedelta(days=1)}
        for source in (self._spot_cache, self._historical_spots):
            if not any(dt_util.as_local(slot).date() == local_today for slot in source):
                continue
            return _spots_for_local_days(source, wanted)
        return {}

    def _billable_spots(
        self, extra_spots: dict[datetime, float]
    ) -> dict[datetime, float]:
        """Persisted year-to-date spots merged with this tick's fresh curve,
        with anything past today dropped.

        The drop has to happen on the MERGED dict, not on either half: the
        freshly fetched curve is exactly where tomorrow's prices come from, and
        letting them into a month mean pulls the flat monthly rate toward a day
        that has not been billed. Both month means spelled this out.

        ``extra_spots`` is collapsed onto the hour first, because the two
        halves need not be on the same grid: the persisted cache is hourly by
        construction, while today's curve is whatever the contract settles on,
        and for a quarter-hourly one that is four keys an hour. Averaging the
        union unweighted then counts today four times over against every other
        day in the month. Measured from the compare page on a realistic curve,
        that pulled a month mean 5,2% toward whatever today happened to be.
        """
        merged = dict(self._historical_spots)
        merged.update(_bucket_spots_by_hour(extra_spots))
        return _drop_future_spots(merged, dt_util.now().date())

    def _monthly_spot_mean(
        self, year: int, month: int, extra_spots: dict[datetime, float]
    ) -> float | None:
        """Arithmetic mean of the (year, month)'s hourly Day-Ahead spots.

        Merges the persisted year-to-date cache with ``extra_spots`` (today's
        freshly fetched curve) so the current month's running mean stays up to
        date within a tick, and de-duplicates by timestamp. Returns ``None``
        when no spot for that month is available yet (cold start).
        """
        return _mean_of_month(self._billable_spots(extra_spots), year, month)

    def _restore_spp_weights(self, blob: dict[str, Any]) -> None:
        """Rehydrate the persisted SPP profile blob into ``_spp_weights``."""
        year = blob.get("year")
        raw = blob.get("weights")
        if not isinstance(year, int) or not isinstance(raw, dict):
            return
        parsed: SppWeights = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, (int, float)):
                continue
            try:
                month, day, hour = (int(x) for x in key.split(","))
            except ValueError:
                continue
            parsed[(month, day, hour)] = float(value)
        if not parsed:
            return
        self._spp_weights = parsed
        self._spp_weights_year = year
        fetched = blob.get("fetched_at")
        if isinstance(fetched, str):
            try:
                self._spp_fetched_at = datetime.fromisoformat(fetched)
            except ValueError:
                self._spp_fetched_at = None

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
        weights = await fetch_spp_weights(self._session, year)
        if weights:
            self._spp_weights = weights
            self._spp_weights_year = year
            self._spp_fetched_at = now
            self._spp_failed_at = None
        else:
            self._spp_failed_at = now

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

    def _prune_historical_spots(self) -> None:
        """Drop cached spots older than the current YTD window.

        Called each tick so the in-memory dict (and the persisted blob) do
        not grow unbounded across year boundaries. Anchor on local midnight:
        in Brussels (UTC+1/+2) the local Jan 1 00:00 falls one or two hours
        BEFORE UTC Jan 1 00:00, so a UTC anchor would silently drop the first
        hour or two of YTD. Prior-year keys are pure dead weight -- every
        consumer filters by the current (year, month) or an exact current-year
        hour key -- so removing them changes no result."""
        if not self._historical_spots:
            return
        today = dt_util.now().date()
        keep_after = dt_util.start_of_local_day(date(today.year, 1, 1)).astimezone(UTC)
        # Within a calendar year every cached hour already sits at or after the
        # cutoff, so skip rebuilding the whole dict every tick. Only rebuild
        # when a prior-year key actually needs dropping (the year boundary).
        # The min() scan is a cheap comparison; it avoids a full dict
        # reallocation on each of the other 364 days.
        if min(self._historical_spots) >= keep_after:
            return
        _drop_hours_before(self._historical_spots, keep_after)
        # The quarter cache is only ever written beside the hourly one, so it
        # holds no hour the hourly cache does not and the two early returns
        # above answer for it too.
        _drop_hours_before(self._historical_spot_quarters, keep_after)
        # Drop prior year days from the completeness set alongside their spots
        # so it doesn't grow without bound across years.
        self._complete_spot_days = {
            d for d in self._complete_spot_days if d.year >= today.year
        }
