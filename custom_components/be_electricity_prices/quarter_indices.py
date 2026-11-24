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

"""The quarter indices a supplier publishes apart from its cards.

A contract signed on a quarter-indexed card is billed each month on its
quarter's index. Where the months' own cards are indexed on the quarter too
(Bolt, Luminus ComfyFlex) each carries it. Eneco's cards are monthly since
April 2023, and a contract signed on an earlier one is billed on the quarter's
Belpex-RLP, which only Eneco's indexation document lists
(``SupplierExtractor.quarter_indices``). That list is read here, at most once
a day while a month asked is missing from it, and held for every entry.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .providers._rates import EnergyRates, SpotMonthlyRates
from .providers.base import QuarterIndexFetcher, SupplierExtractor, SupplierSnapshot
from .snapshot_store import _MONTHLY_FAILURE_TTL, _MONTHLY_PROVISIONAL_TTL

_LOGGER = logging.getLogger(__name__)

_KEY = "published_quarter_indices"
# How far the month's index on the list may lie from the one its card names,
# as a share of it. The list rounds to 0,01 EUR/MWh what the cards footnote to
# 0,0001, and on 34 of the 35 Flex months archived from November 2023 to
# September 2026 that is all the two differ by. The January 2026 card footnotes
# December 2025's Belpex-RLP-M at 85,3520 and its Belpex-injectie at 87,1650,
# where the list has 87,16 and 85,35: 2,1 % apart. A list in another unit is
# ten times out or more.
_SAME_INDEX = 0.05


@dataclass
class _Held:
    """A supplier's list as last read, and when and whether it was."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    table: dict[date, tuple[float, float]] = field(default_factory=dict)
    asked_at: datetime | None = None
    read: bool = False


def on_published_quarters(
    extractor: SupplierExtractor, leg: EnergyRates, delivery: SupplierSnapshot
) -> bool:
    """Whether ``leg``, a cohort's, is billed on ``extractor``'s published
    quarter indices rather than on the index of ``delivery``, the card of the
    month billed: a quarter-indexed leg on a card that is not, of a supplier
    that publishes them. The month's own index is another one there."""
    return (
        extractor.quarter_indices is not None
        and isinstance(leg, SpotMonthlyRates)
        and leg.quarter_indexed
        and not getattr(delivery.energy, "quarter_indexed", False)
    )


async def published_quarter_index(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    delivery: SupplierSnapshot,
    month: date,
) -> float | None:
    """The value ``month``'s quarter settled at on ``extractor``'s list, in
    EUR/kWh, or None.

    Held to ``delivery``, the month's card: the month's own index on the list
    has to be the one the card says the month settled at (``_SAME_INDEX``),
    which ties the list to the unit the card states, "€/MWh". None while the
    quarter is not on the list, the card has not settled, or the two
    disagree; the leg is then billed as with nothing published, on the
    quarter's mean to date with a key, the card's printed rate without.
    """
    fetch = extractor.quarter_indices
    settled = getattr(delivery.energy, "index_realised", None)
    if fetch is None or settled is None:
        return None
    row = (await _table(hass, session, extractor.id, fetch, month)).get(month)
    if row is None or abs(row[1] - settled) > _SAME_INDEX * abs(settled):
        return None
    return row[0]


async def _table(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    supplier: str,
    fetch: QuarterIndexFetcher,
    month: date,
) -> dict[date, tuple[float, float]]:
    """The supplier's list, read again when ``month`` is not on it and the
    last read is a day old, or half an hour where it failed, which keeps the
    list it had."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    held: _Held = bucket.setdefault(_KEY, {}).setdefault(supplier, _Held())
    async with held.lock:
        wait = _MONTHLY_PROVISIONAL_TTL if held.read else _MONTHLY_FAILURE_TTL
        if month in held.table or (
            held.asked_at is not None and dt_util.utcnow() - held.asked_at < wait
        ):
            return held.table
        held.asked_at = dt_util.utcnow()
        try:
            held.table = await fetch(session)
        except Exception as err:  # a failed read must not break the year walk
            _LOGGER.debug("%s quarter indices not read: %s", supplier, err)
            held.read = False
        else:
            held.read = True
        return held.table
