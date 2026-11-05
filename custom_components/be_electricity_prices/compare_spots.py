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

"""The day-ahead means a household's quotes are priced on.

Each side of a comparison bills its energy, and a feed-in where it has one,
on a mean of the day-ahead curve, and its card says which: the hours the
household draws, the delivery month, a profile-weighted month, the quarter.
The month and the quarter are fetched on first use into a copy of the
coordinator's spots and kept, since every row of a ranking asks them of the
same household.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime
from typing import Any, NamedTuple

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .compare_inputs import (
    _credit_index_for,
    _detached_spot_view,
    _keyless_stale_spots,
    _needs_month_mean,
)
from .compare_table import IndexGaps
from .compare_weighting import _consumption_weighted_spot, _tou_weighted_per_kwh
from .const import SOLAR_REGIME_COMPENSATION, SOLAR_REGIME_INJECTION
from .providers.base import SupplierSnapshot
from .spot_stats import (
    _energy_is_rlp_indexed,
    _injection_is_spp_indexed,
    _rlp_blend_for,
    spots_available,
)


class _HouseholdSpots(NamedTuple):
    """What a household's quotes ask of the day-ahead curve."""

    spot_for: Callable[[SupplierSnapshot | None], Awaitable[float | None]]
    index_gaps: Callable[..., IndexGaps]
    credit_month_spot_for: Callable[..., Awaitable[float | None]]
    export_rate_for: Callable[
        [SupplierSnapshot | None, Any, str], Awaitable[float | None]
    ]


def _household_spots(
    coord: Any,
    current: Mapping[str, Any],
    *,
    today_local: date,
    now_utc: datetime,
    region: str,
    dso: str,
    day_start: int,
    regime: str,
    stored_regime: str,
    spot_dict: dict[datetime, float],
    avg_spot: float | None,
    spot_key: str | None,
    spot_keyless: bool,
    hour_weights: Any,
    inj_hour_weights: Any,
    rolling_inj_kwh: float,
) -> _HouseholdSpots:
    """The spot readers of one household (``_resolve_household``).

    ``spot_dict`` is the day-ahead window the page holds and ``avg_spot``
    its mean, ``spot_key`` and ``spot_keyless`` how a month is fetched,
    ``hour_weights`` and ``inj_hour_weights`` when the household draws and
    exports, and ``rolling_inj_kwh`` the year's export a feed-in is credited
    on. ``regime`` is the one quoted, ``stored_regime`` the entry's own.
    """
    # A SPOT-MONTHLY leg is different: it does not average out over a year,
    # it bills one flat rate per delivery month, and that month's mean is a
    # number the coordinator already computes. Pricing it off a single
    # day-ahead window instead makes the quote swing with the day the
    # dialog happened to open, and prints a "per kWh now" for the user's
    # OWN entry that contradicts their current_price sensor. Resolved
    # lazily and once: the month backfill is only worth its fetch when a
    # side actually bills on it, and a target's card is only known once its
    # quote asks.
    month_spot_resolved: list[float | None] = []
    # The copy _month_spot fetched the month into. The weighted means are
    # taken on it too: the coordinator never sees what it fetched, so on
    # an entry holding no spots of its own they averaged today alone.
    month_view: list[Any] = []

    async def _month_spot() -> float | None:
        if month_spot_resolved:
            return month_spot_resolved[0]
        value = avg_spot
        # Merged rather than isolated: a month mean is the same number
        # whoever asks, so whatever the entry has already cached is valid
        # input, and it is what still answers when the fetch fails. Not a
        # stale cache, one an entry with no source holds from a contract it
        # switched away from: read as the month, the two days before a
        # switch on the 3rd priced every side's feed-in.
        month_first = today_local.replace(day=1)
        view = _detached_spot_view(
            coord,
            isolate=_keyless_stale_spots(
                current, coord._historical_spots, month_first, today_local
            ),
        )
        month_view.append(view)
        # A failed fetch degrades to the day-ahead mean.
        with contextlib.suppress(Exception):
            await view._ensure_historical_spots(
                today_local.replace(day=1),
                today_local,
                spot_key,
                keyless=spot_keyless,
            )
        resolved = view._monthly_spot_mean(
            today_local.year, today_local.month, spot_dict
        )
        if resolved is not None:
            value = resolved
        month_spot_resolved.append(value)
        return value

    spp_spot_resolved: list[float | None] = []

    async def _spp_month_spot() -> float | None:
        """The SPP-weighted month mean an SPP-indexed credit bills on.

        One index further than _month_spot: a card that indexes its
        feed-in on Belpex_SPP may not be resolved against any other mean,
        so when no Synergrid solar profile is held this answers None and
        the caller keeps the card's printed indicative rather than
        quoting the consumption index. Reads the entry's profile, else
        the installation's (``quoted_spp_weights``); only the nightly
        ranking downloads one, never the dialog, and a row priced without
        it says so (``index_gaps``).
        """
        if spp_spot_resolved:
            return spp_spot_resolved[0]
        await _month_spot()  # fetches the month into month_view
        spp_spot_resolved.append(
            month_view[0]._spp_weighted_month_mean(
                today_local.year, today_local.month, spot_dict
            )
        )
        return spp_spot_resolved[0]

    async def _credit_month_spot_for(
        snapshot: SupplierSnapshot | None,
        *,
        entry: ConfigEntry,
        own: bool,
        raw: SupplierSnapshot | None = None,
    ) -> float | None:
        """The delivery month's mean this side's FEED-IN settles on.

        Two indices, and the card names which. A formula on Belpex_SPP
        takes the solar-weighted mean; one on the plain arithmetic mean
        takes that. Either way the answer is a month, never the two-day
        day-ahead window the energy leg is quoted at, and never the
        printed indicative, which is that formula on the PREVIOUS month.

        The plain branch is what the page was missing: it resolved the
        SPP cards and let every other month-indexed card (Eneco Fix, Flex
        and Flex One, Engie's and Luminus' EPEXDAM cards, TotalEnergies
        Impact) fall through to the live helper, which answers the
        printed figure unless the snapshot has been baked and only the
        coordinator bakes. The gap is ``factor`` times one month of index
        drift, about 25 EUR a year on an Eneco card at 10 EUR/MWh and
        3000 kWh exported.

        Which index applies is ``_credit_index_for``; this only turns
        that answer into a number, because resolving either mean needs the
        fetched month the closure holds.

        ``entry`` is the one the side's credit is priced under, and it has
        no default because the answer turns on its regime: the SPP gate
        and the per-hour cohort test both need the injection regime on
        the entry they are handed. A what-if into the injection tariff
        prices the own side on the quote proxy, and asked of the real
        entry, still on compensation, an SPP-indexed credit read as plain
        and was resolved on the arithmetic mean: 167 to 245 EUR a year
        too much credit on the own side alone. Only the as-configured
        baseline is asked of the real entry.
        """
        if own and not spots_available(entry.data):
            # The household's own credit on its own day-ahead, as the
            # sensor credits it: with no source, the page's month is an
            # earlier contract's days at best, and the card's figure
            # stands.
            return None
        index = _credit_index_for(entry, snapshot, own=own, raw=raw)
        if index == "spp":
            return await _spp_month_spot()
        if index == "plain":
            return await _month_spot()
        return None

    async def _spot_for(snapshot: SupplierSnapshot | None) -> float | None:
        """The spot this side's energy shape actually bills on.

        A per-slot leg takes the mean weighted by when the household draws,
        because its bill is the sum over slots of kWh times that slot's
        rate, and consumption is evening-heavy while the day-ahead curve
        troughs at midday. A month-mean leg takes its delivery month's own
        index, which is a published number and not a shape question.
        """
        if (
            snapshot is not None
            and _needs_month_mean(snapshot)
            and getattr(snapshot.energy, "quarter_indexed", False)
        ):
            # Bolt bills the quarter's index, so the quarter is fetched,
            # into a copy, and resolved the way the live price is.
            first = today_local.replace(
                month=(today_local.month - 1) // 3 * 3 + 1, day=1
            )
            view = _detached_spot_view(
                coord,
                isolate=_keyless_stale_spots(
                    current, coord._historical_spots, first, today_local
                ),
            )
            # A failed fetch degrades to the month mean below.
            with contextlib.suppress(Exception):
                await view._ensure_historical_spots(
                    first, today_local, spot_key, keyless=spot_keyless
                )
            quarter: float | None = view._quarter_index(
                snapshot.energy, today_local.year, today_local.month, spot_dict
            )
            if quarter is not None:
                return quarter
        if snapshot is not None and _needs_month_mean(snapshot):
            month = await _month_spot()
            if _energy_is_rlp_indexed(snapshot.energy):
                # Eneco's index is the RLP-weighted mean; the plain one
                # is the fallback while no profile is held. The entry's
                # profiles, else the installation's, laid on the view
                # (``_detached_spot_view``); never downloaded here.
                #
                # THIS side's blend, not the entry's. The three reductions
                # are three different indices, not one at three
                # resolutions: measured on the August 2026 Belgian
                # day-ahead curve they stood at 133,44 / 134,93 / 135,66
                # EUR/MWh, so pricing an Energy Knights card on Eneco's
                # curve moves it 2,2 EUR/MWh against what it bills, which
                # is enough to reorder neighbouring rows on a page whose
                # whole job is the order.
                weighted: float | None = month_view[0]._rlp_weighted_month_mean(
                    today_local.year,
                    today_local.month,
                    spot_dict,
                    blend=_rlp_blend_for(snapshot.energy),
                )
                if weighted is not None:
                    return weighted
            return month
        return _consumption_weighted_spot(spot_dict, hour_weights) or avg_spot

    def _index_gaps(
        snapshot: SupplierSnapshot | None, *, own: bool = False
    ) -> IndexGaps:
        """The index this side's card names that the page could not
        resolve, by cause (``IndexGaps``), so the page and the ranking say
        so rather than ranking the row as though its own index had been
        read.

        Without a day-ahead source no month index is resolved, whatever
        curve is held: a feed-in on one is credited at the figure its card
        prints, and an energy leg on one is not priced at all, so the cause
        is the source and never the profile. With one, a weighted index
        whose curve is not held is priced without it: the plain mean for
        the energy, the printed figure for the feed-in. A feed-in only
        counts where it is credited at a price on export the household
        measured, and where the card prints a figure to fall back on;
        without one the credit is left out, which ``feed_in_uncredited``
        says.

        ``own`` asks it of the household's own side, which reads only the
        entry's own source (:func:`_credit_month_spot_for`): a key typed
        on the page is not one.
        """
        if snapshot is None:
            return IndexGaps()
        energy = snapshot.energy
        inj = snapshot.injection
        credited = (
            regime == SOLAR_REGIME_INJECTION
            and rolling_inj_kwh > 0
            and getattr(inj, "current", None) is not None
        )
        source = spots_available(current) if own else spot_key or spot_keyless
        if not source:
            return IndexGaps(
                day_ahead=credited
                and (
                    bool(getattr(inj, "month_indexed", False))
                    or _injection_is_spp_indexed(snapshot)
                )
            )
        return IndexGaps(
            load=_needs_month_mean(snapshot)
            and _energy_is_rlp_indexed(energy)
            and not coord.quoted_rlp_weights(_rlp_blend_for(energy)),
            solar=credited
            and _injection_is_spp_indexed(snapshot)
            and not coord.quoted_spp_weights(),
        )

    async def _export_rate_for(
        snapshot: SupplierSnapshot | None, meter_type: Any, mode: str
    ) -> float | None:
        """All-in EUR/kWh weighted by when the panels EXPORT.

        Only a compensation meter needs it: it nets against the rate in
        force at the time, so the exported side has to be priced on its
        own shape rather than on the consumption one. Every other regime
        never reads it, so it is not worth the second pass. Asked when
        either the what-if or the stored regime nets: the note's figure
        as configured prices a compensation entry on its own regime, and
        without this rate it netted the annual totals instead.

        ``mode`` is the side's own DSO tariff mode, since a Tarif Impact
        target is billed on a configuration the household need not be on.
        """
        if SOLAR_REGIME_COMPENSATION not in (regime, stored_regime):
            return None
        if snapshot is None or inj_hour_weights is None:
            return None
        return _tou_weighted_per_kwh(
            snapshot,
            dso,
            region,
            dt_util.as_local(now_utc),
            await _spot_for(snapshot),
            meter_type,
            mode,
            inj_hour_weights,
            day_start=day_start,
        )

    return _HouseholdSpots(
        _spot_for, _index_gaps, _credit_month_spot_for, _export_rate_for
    )
