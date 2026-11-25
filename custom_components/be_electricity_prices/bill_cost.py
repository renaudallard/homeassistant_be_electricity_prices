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

"""What the meters recorded over a closed run of days cost, priced here.

For the bill_cost action, which a supplier's bill is held against. The days
are one window of the year-to-date bill (``_compute_current_year_cost``), so
each month bills on its own card and a compensation net is settled over the
whole window, as the meter readout that closes a bill settles it. A cut at
1 January, as the first contract year's walk makes, would net each side on
its own.

What the walk is handed is what the live tick hands it, for the days asked:
the day-ahead those days need, held from the tick's prune while it runs, the
sheets of each year they fall in, and the load and solar profiles. Those are
keyed by (month, day, hour) without the year, so the window's are put
together hour by hour, each from the curve of the year the hour falls in.

Only the current contract is priced: days before the last recorded switch
are refused rather than billed on the wrong supplier.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from homeassistant.util.json import JsonValueType

from .backfill import _ensure_dynamic_spots
from .backfill_window import _BACKFILL_FIRST_MONTH, ensure_year_sheets
from .const import CONF_SOLAR_REGIME, CONF_SUPPLIER, DOMAIN, SOLAR_REGIME_COMPENSATION
from .contract_periods import recorded_contracts
from .fees import bills_capacity
from .providers import get as get_extractor
from .spot_stats import (
    _energy_is_rlp_indexed,
    _rlp_blend_for,
    _spp_weighting_enabled,
    own_day_ahead,
)
from .ytd_cost import _compute_current_year_cost

if TYPE_CHECKING:
    from .coordinator import BePricesCoordinator

# The longest window priced: a bill of an intermediary and a yearly reading
# together. Every hour of it is walked on the event loop, and past a year a
# profile key names two hours, both weighted on the later one's curve.
MAX_DAYS = 731

type Curve = dict[tuple[int, int, int], float]


def _invalid(key: str, **placeholders: str) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key=key,
        translation_placeholders=placeholders,
    )


def check_window(data: Any, start: date, end: date, today: date) -> None:
    """Refuse a window that is not a closed run of days of the current
    contract the cards and the law reach back to."""
    if end < start:
        raise _invalid("window_reversed")
    if end >= today:
        raise _invalid("window_not_closed")
    if start < _BACKFILL_FIRST_MONTH:
        raise _invalid("window_too_early", earliest=_BACKFILL_FIRST_MONTH.isoformat())
    if (end - start).days + 1 > MAX_DAYS:
        raise _invalid("window_too_long", days=str(MAX_DAYS))
    records = recorded_contracts(data)
    if records and start < records[-1][0]:
        raise _invalid("window_before_switch", switched=records[-1][0].isoformat())


def _hours(first: date, last: date) -> list[datetime]:
    """Every UTC hour of the local days ``first`` to ``last``."""
    hour = dt_util.as_utc(dt_util.start_of_local_day(first))
    stop = dt_util.as_utc(dt_util.start_of_local_day(last + timedelta(days=1)))
    out: list[datetime] = []
    while hour < stop:
        out.append(hour)
        hour += timedelta(hours=1)
    return out


def _month_end(day: date) -> date:
    return (day.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)


async def window_curve(
    curve_for: Callable[[int], Awaitable[Curve | None]],
    start: date,
    end: date,
    key_of: Callable[[datetime], tuple[int, int, int]],
) -> Curve | None:
    """A profile for the days ``start`` to ``end``, each hour weighted on
    the curve of its own local year (``curve_for``), or None where a year
    has none. The rest of the months the days fall in are filled first,
    from their own year's curve, for a month mean weighted on it; the
    window's hours then take their keys over."""
    window = _hours(start, end)
    months = _hours(start.replace(day=1), _month_end(end))
    curves: dict[int, Curve | None] = {}
    for year in sorted({dt_util.as_local(hour).year for hour in months}):
        curves[year] = await curve_for(year)
    if any(curve is None for curve in curves.values()):
        return None
    out: Curve = {}
    for hour in (*months, *window):
        curve = curves[dt_util.as_local(hour).year]
        key = key_of(hour)
        if curve is not None and key in curve:
            out[key] = curve[key]
    return out


def _local_key(hour: datetime) -> tuple[int, int, int]:
    local = dt_util.as_local(hour)
    return local.month, local.day, local.hour


def _utc_key(hour: datetime) -> tuple[int, int, int]:
    return hour.month, hour.day, hour.hour


async def window_cost(
    coordinator: BePricesCoordinator, start: date, end: date
) -> dict[str, JsonValueType]:
    """What the meters recorded from ``start`` to ``end`` cost, VAT
    included, with what the walk reports of its days and kWh."""
    entry = coordinator.entry
    today = dt_util.now().date()
    check_window(entry.data, start, end, today)
    snapshot = coordinator._snapshot
    if snapshot is None:
        raise _invalid("snapshot_not_loaded")
    # The leg the last tick priced, the signing cohort's spliced onto the
    # card, which is what it decided the profiles on.
    priced = coordinator._priced or snapshot
    allocating = entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_COMPENSATION
    # The day-ahead of whole months, for a month mean the days are priced on.
    fetched_to = min(_month_end(end), today)
    coordinator._spot_prune_holds += 1
    try:
        spots, quarters = await _ensure_dynamic_spots(
            coordinator,
            entry,
            dt_util.as_utc(dt_util.start_of_local_day(start)),
            dt_util.as_utc(dt_util.start_of_local_day(fetched_to)),
        )
        rlp = None
        if _energy_is_rlp_indexed(priced.energy) or allocating:
            blend = _rlp_blend_for(priced.energy)
            await coordinator._ensure_rlp_weights(blend)

            async def rlp_of(year: int) -> Curve | None:
                return await coordinator.rlp_weights_for_year(year, blend)

            rlp = await window_curve(rlp_of, start, end, _local_key)
        spp = None
        if _spp_weighting_enabled(entry, priced):
            await coordinator._ensure_spp_weights()
            spp = await window_curve(
                coordinator.spp_weights_for_year, start, end, _utc_key
            )
        raw = coordinator._snapshot_raw
        await ensure_year_sheets(
            coordinator._session, entry, raw, list(range(start.year, end.year + 1))
        )
        breakdown: dict[str, float] = {}
        cost = await _compute_current_year_cost(
            coordinator.hass,
            coordinator._session,
            get_extractor(entry.data[CONF_SUPPLIER]),
            snapshot,
            entry,
            historical_spots=own_day_ahead(entry.data, spots),
            spot_quarters=own_day_ahead(entry.data, quarters),
            spp_weights=spp,
            rlp_weights=rlp,
            billed_peak_kw=(
                coordinator._billed_peak_kw() if bills_capacity(entry.data) else 0.0
            ),
            breakdown=breakdown,
            window_start_override=start,
            window_end=end,
            snapshot_raw=raw,
        )
    finally:
        coordinator._spot_prune_holds -= 1
    days = (end - start).days + 1
    # The walk counts hours on its hourly branches and days on the daily one.
    seen = breakdown.get("days_seen", breakdown.get("hours_seen"))
    elapsed = breakdown.get("days_elapsed", breakdown.get("hours_elapsed"))
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cost": None if cost is None else round(cost, 2),
        "covered": seen is not None and seen == elapsed,
        "days": days,
        # Whether a compensation net was spread on the load profile, or an
        # RLP-indexed month weighted on it; else priced as metered.
        "load_profile": rlp is not None,
        "breakdown": {key: round(value, 4) for key, value in sorted(breakdown.items())},
    }
