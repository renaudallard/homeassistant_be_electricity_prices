# Copyright (c) 2026 Renaud Allard
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""The calendar year's bill as it will stand on 31 December.

The year-to-date walk run to 31 December: the days before today as metered
and billed on their own months' cards, and today onwards on last year's same
days, the kWh ``projected_year_consumption`` and ``projected_year_injection``
project, priced on the card as it prices today (:mod:`year_ahead`). One walk
of the whole year rather than the running bill plus a remainder, because the
bill is not a sum of its parts: under compensation the year is netted once
per register and floored once, so a summer surplus is spent on the winter
that follows, and adding a floored year to date to the rest would bill the
winter in full on top of a surplus left forfeit.

Unknown where the rest of the year has no rate: a dynamic contract, a feed-in
credit that follows the spot price per slot, and a month-indexed leg whose
running month has no index yet. A month-indexed card holds the running
month's index to 31 December, the way the rolling year cost holds a variable
card's printed rate.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_SOLAR_REGIME,
    SOLAR_REGIME_INJECTION,
)
from .energy_meters import _bills_injection, _kwh_sensor_ids, reading_year_ahead
from .injection import _compute_injection_price, _injection_is_spot_formula
from .projected_cost import _contract_basis, held_at_index
from .projected_volume import _last_year_window, _same_days_last_year
from .providers._rates import DynamicRates, SpotMonthlyRates
from .providers.base import SupplierExtractor, SupplierSnapshot
from .synergrid import RlpWeights, SppWeights
from .year_ahead import YearAhead
from .ytd_cost import _compute_current_year_cost


def _unpriceable(card: SupplierSnapshot, entry: ConfigEntry) -> str | None:
    """Why the months ahead cannot be priced on ``card``, or ``None``."""
    if isinstance(card.energy, DynamicRates):
        return (
            "not projected: a dynamic contract has no prices for the rest of the year"
        )
    if isinstance(card.energy, SpotMonthlyRates) and card.energy.index_realised is None:
        return "not projected: this month's index is not known yet"
    if (
        entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
        and card.injection is not None
        and _injection_is_spot_formula(card.injection, card.energy)
    ):
        return (
            "not projected: the feed-in credit follows the spot price per slot, "
            "which the rest of the year has none of"
        )
    if (
        entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
        and card.injection is not None
        and _compute_injection_price(card, entry, {}) is None
    ):
        # A month-indexed credit with no printed figure, early in a month
        # whose index is not out: the months ahead would bill no feed-in.
        return "not projected: the feed-in credit has no rate this month yet"
    return None


async def _compute_year_end_cost(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    card: SupplierSnapshot,
    today: date,
    *,
    energy_index: float | None,
    previous_eur: float,
    breakdown: dict[str, Any],
    historical_spots: dict[datetime, float] | None = None,
    spot_quarters: dict[datetime, list[float]] | None = None,
    spp_weights: SppWeights | None = None,
    rlp_weights: RlpWeights | None = None,
    billed_peak_kw: float = 0.0,
    cached_only: bool = False,
    window_start_override: date | None = None,
    snapshot_raw: SupplierSnapshot | None = None,
) -> float | None:
    """The calendar year's bill in EUR, or ``None`` with the reason in
    ``breakdown["energy_basis"]`` or ``breakdown["volume_basis"]``.

    ``snapshot`` is the entry's card as the year-to-date walk takes it, and
    ``card`` the one the live price is built from, the signing cohort spliced
    in and a month-indexed feed-in baked on the running month.
    ``snapshot_raw`` is the card ``snapshot`` was resolved from, which the
    year-to-date walk prices a month no archive holds on.
    ``energy_index`` is the running month's index for a month-indexed energy
    leg. ``previous_eur`` is what the contracts held earlier in the year cost.
    """
    card = held_at_index(card, energy_index)
    why = _unpriceable(card, entry)
    if why is not None:
        breakdown["energy_basis"] = why
        return None
    sides = ["consumption"]
    if _bills_injection(entry) and any(_kwh_sensor_ids(entry, "injection")):
        sides.append("injection")
    if not any(_kwh_sensor_ids(entry, "consumption")):
        breakdown["volume_basis"] = "not projected: no consumption meter is wired"
        return None
    for side in sides:
        if await _same_days_last_year(hass, entry, today, side=side) is None:
            start, end = _last_year_window(today)
            breakdown["volume_basis"] = (
                f"not projected: the {side} meter's history does not cover "
                f"{start} to {end}"
            )
            return None

    stats: dict[str, float] = {}
    with reading_year_ahead(YearAhead(today, card)):
        cost = await _compute_current_year_cost(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            historical_spots=historical_spots,
            spot_quarters=spot_quarters,
            spp_weights=spp_weights,
            rlp_weights=rlp_weights,
            breakdown=stats,
            billed_peak_kw=billed_peak_kw,
            cached_only=cached_only,
            window_start_override=window_start_override,
            window_end=date(today.year, 12, 31),
            snapshot_raw=snapshot_raw,
        )
    if cost is None:
        return None

    breakdown["energy_basis"] = (
        "each month's own card to date, then today's card to 31 December"
        + (
            ", its index held at this month's"
            if isinstance(card.energy, SpotMonthlyRates)
            else ""
        )
    )
    # On 1 January nothing of the year is metered yet: naming last year's
    # 31 December as the end of the metered part read as if it were.
    breakdown["volume_basis"] = (
        "nothing metered yet this year, last year's same days"
        if (today.month, today.day) == (1, 1)
        else f"metered to {today - timedelta(days=1)}, then last year's same days"
    )
    breakdown["contract_basis"] = _contract_basis(
        entry, today, date(today.year, 12, 31)
    )
    breakdown["consumption_kwh"] = stats.get("consumption_ytd_kwh", 0.0)
    breakdown["injection_kwh"] = stats.get("injection_ytd_kwh", 0.0)
    breakdown["fees_eur"] = stats.get("fees_ytd_eur", 0.0)
    breakdown["welcome_credit_eur"] = stats.get("welcome_credit_eur", 0.0)
    if previous_eur:
        breakdown["previous_contracts_eur"] = previous_eur
    if stats.get("injection_hours_uncredited"):
        breakdown["injection_hours_uncredited"] = stats["injection_hours_uncredited"]
    return cost + previous_eur
