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

"""What the first contract year charged, for a welcome lump held to it.

EnergyVision's April to June 2026 cards cap the credit they pay at the
anniversary at what that first year charged for the components it may come
off (``fees.lump_held_to_first_year``). That year straddles 1 January, so it
is walked on its own by the year-to-date bill, and kept once it is over.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_CONTRACT, CONF_REGION
from .energy_meters import noting_failed_reads
from .providers.base import SupplierExtractor, SupplierSnapshot
from .snapshot_store import cached_month_card


async def first_year_charges(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    first: date,
    last: date,
    **walk: Any,
) -> float | None:
    """What ``[first, last]`` charged for the three components a welcome
    credit may come off, by the year-to-date bill over those days.

    The metered part is stretched over the days or hours the walk could not
    price (no meter reading, no spot), so a history starting part-way through
    the first year does not pass for a year of small bills; a walk that priced
    none answers None, as one that could not run at all does.

    Kept on the coordinator once those days are over and the last of them is
    in the recorder, from a walk the recorder answered in full and that priced
    something (``_first_year_memo``), for as long as the cards of their
    months and the profiles they were priced on stay the same: nothing else
    about a closed year moves, and walking it again cost every tick from the
    anniversary to 31 December 0,3 to 3,5 s on a Raspberry Pi. Two inputs
    are left out of what is compared on purpose. The day-ahead: the cache
    keeps the trailing year only, so a later walk would price fewer of those
    hours, not better. And the card held today, which the coordinator
    resolves again whenever the measured yearly volume moves and which stands
    in only for a month no archive holds. A compare-page proxy carries no
    coordinator and walks every time.
    """
    memo: dict[tuple[Any, ...], tuple[tuple[Any, ...], float | None]] | None = getattr(
        getattr(entry, "runtime_data", None), "_first_year_memo", None
    )
    contract = walk.get("contract_override") or entry.data.get(CONF_CONTRACT, "")
    slot = (contract, walk.get("meter_override"), first, last)
    months: list[date] = []
    month = date(first.year, first.month, 1)
    while month <= last:
        months.append(month)
        month = (month + timedelta(days=32)).replace(day=1)
    inputs = (
        tuple(
            cached_month_card(
                hass, extractor.id, contract, entry.data.get(CONF_REGION, ""), m
            )
            for m in months
        ),
        walk.get("rlp_weights"),
        walk.get("rlp_index_weights"),
        walk.get("spp_weights"),
        walk.get("cached_only", False),
    )
    now = dt_util.now()
    closed = now.date() > last + timedelta(days=1) or (
        now.date() > last and now.hour >= 1
    )
    if memo is not None and closed:
        held = memo.get(slot)
        if held is not None and held[0] == inputs:
            return held[1]
    # Kept only from a walk every recorder read answered, and that priced
    # something. A busy recorder answers each read with nothing, and the walk
    # then comes back with no figure, or a figure short of the year; kept, it
    # left the lump on the stretched estimate until 31 December.
    with noting_failed_reads() as failed:
        eligible = await _walked_charges(
            hass, session, extractor, snapshot, entry, first, last, **walk
        )
    if memo is not None and closed and not failed and eligible is not None:
        memo[slot] = (inputs, eligible)
    return eligible


async def _walked_charges(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    first: date,
    last: date,
    **walk: Any,
) -> float | None:
    """:func:`first_year_charges` with no memo: the walk itself."""
    # Late: the year-to-date engine is what asks for these charges.
    from .ytd_cost import _compute_current_year_cost

    year: dict[str, float] = {}
    if (
        await _compute_current_year_cost(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            breakdown=year,
            window_start_override=first,
            window_end=last,
            **walk,
        )
        is None
    ):
        return None
    eligible = year.get("welcome_credit_eligible_eur")
    priced = year.get("days_priced", year.get("hours_priced", 0.0))
    elapsed = year.get("days_elapsed", year.get("hours_elapsed", 0.0))
    if eligible is None or priced <= 0.0:
        return None
    metered = year.get("energy_component_ytd_eur", 0.0) + year.get(
        "green_component_ytd_eur", 0.0
    )
    return eligible + metered * (elapsed / priced - 1.0)
