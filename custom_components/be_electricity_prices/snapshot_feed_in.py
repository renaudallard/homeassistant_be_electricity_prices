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

"""A closed month billed on the current card standing in for it, credited
the feed-in figures its own quarter settled at.

``snapshot_months._snapshot_for_month`` hands back the current card for a
month no archive holds a card of. A card crediting its feed-in at figures per
register that its quarter settles (Bolt's variable cards,
``InjectionRates.spp_redistributed``) prints the last closed quarter's, which
are not that month's. Where another card the supplier archives prints the
month's own (``SupplierExtractor.stand_in_reference``), they are rebuilt on
its indices.

The month cache is imported from ``snapshot_months`` at call time, since that
module imports this one.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_SOLAR_REGIME, SOLAR_REGIME_INJECTION
from .providers import EXTRACTORS
from .providers._settle import settled_stand_in
from .providers.base import SupplierExtractor, SupplierSnapshot
from .snapshot_resolve import _resolve_snapshot


def _reference(
    extractor: SupplierExtractor,
    contract: str,
    year_month: date,
    current: SupplierSnapshot,
    entry: ConfigEntry | None,
) -> tuple[str, date] | None:
    """The card a stand-in month's feed-in settles on, for an entry credited
    figures per register on its variable settlement, or None."""
    inj = current.injection
    if (
        extractor.stand_in_reference is None
        or entry is None
        or entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION
        or inj is None
        or not inj.spp_redistributed
        or inj.slot_indexed
    ):
        return None
    return extractor.stand_in_reference(contract, year_month, dt_util.now().date())


async def stand_in_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    year_month: date,
    current: SupplierSnapshot,
    entry: ConfigEntry | None,
    *,
    cached_only: bool = False,
    current_raw: SupplierSnapshot | None = None,
) -> SupplierSnapshot:
    """``current`` standing in for ``year_month``.

    Given the card it was resolved from as ``current_raw``, with the federal
    levies the month owes (``_proxy_for_month``) and the feed-in figures of
    its own quarter, rebuilt on the reference card's indices. A reference
    that cannot be read leaves the month credited per slot on the card's
    formula, as the dynamic settlement is, rather than at the current card's
    figures, another quarter's: a household's day-ahead prices then price it,
    and without them it goes uncredited, while the backfill waits for the
    read (``reference_failed``). The reference is read even without
    ``current_raw``, so the warm-up fills it for the walks that bill.
    """
    from . import snapshot_months

    ref = _reference(extractor, contract, year_month, current, entry)
    reference = None
    if ref is not None:
        reference = await snapshot_months._month_row(
            hass,
            session,
            extractor,
            ref[0],
            region,
            ref[1],
            entry,
            cached_only=cached_only,
        )
    if entry is None or current_raw is None:
        return current
    proxy = snapshot_months._proxy_for_month(entry, current, current_raw, year_month)
    inj = current_raw.injection
    if ref is None or inj is None:
        return proxy
    settled = None
    if reference is not None and reference.injection is not None:
        settled = settled_stand_in(inj, reference.injection)
    leg = settled if settled is not None else replace(inj, spp_redistributed=False)
    card = _resolve_snapshot(
        entry,
        replace(current_raw, injection=leg),
        delivery_month=year_month,
        stand_in=True,
    )
    return replace(proxy, injection=card.injection)


def reference_failed(
    hass: HomeAssistant, supplier: str, contract: str, region: str, year_month: date
) -> bool:
    """Whether a month no archive holds waits for the card its feed-in
    settles on, whose read failed just now (``month_card_failed``)."""
    from . import snapshot_months

    extractor = EXTRACTORS.get(supplier)
    if extractor is None or extractor.stand_in_reference is None:
        return False
    ref = extractor.stand_in_reference(contract, year_month, dt_util.now().date())
    return ref is not None and snapshot_months.month_card_failed(
        hass, supplier, ref[0], region, ref[1]
    )
