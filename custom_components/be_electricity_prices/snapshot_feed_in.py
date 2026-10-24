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
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_QUARTER_HOURLY, CONF_SOLAR_REGIME, SOLAR_REGIME_INJECTION
from .providers import EXTRACTORS
from .providers._settle import settled_stand_in
from .providers.base import SupplierExtractor, SupplierSnapshot
from .snapshot_resolve import _resolve_snapshot
from .spot_stats import spots_available


def _reads_reference(entry: ConfigEntry | None) -> bool:
    """Whether an entry is credited the feed-in figures a card prints per
    register: one on the injection regime, and on the variable settlement
    rather than the quarter-hourly one, which credits each quarter at its own
    spot."""
    return (
        entry is not None
        and entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
        and not entry.data.get(CONF_QUARTER_HOURLY)
    )


def _reference(
    extractor: SupplierExtractor,
    contract: str,
    year_month: date,
    current: SupplierSnapshot | None,
    entry: ConfigEntry | None,
) -> tuple[str, date] | None:
    """The card a stand-in month's feed-in settles on, for an entry credited
    figures per register on its variable settlement, or None."""
    inj = None if current is None else current.injection
    if (
        extractor.stand_in_reference is None
        or not _reads_reference(entry)
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
    that cannot be read leaves an entry with a day-ahead source credited per
    slot on the card's formula, as the dynamic settlement is, rather than at
    the current card's figures, another quarter's, while the backfill waits
    for the read (``reference_failed``). One without a source is credited
    the current card's figures, as such an entry is wherever a card prints
    one. The reference is read even without ``current_raw``, so the warm-up
    fills it for the walks that bill.
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
    if settled is None and not spots_available(entry.data):
        return proxy
    leg = settled if settled is not None else replace(inj, spp_redistributed=False)
    card = _resolve_snapshot(
        entry,
        replace(current_raw, injection=leg),
        delivery_month=year_month,
        stand_in=True,
    )
    return replace(proxy, injection=card.injection)


def reference_failed(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    year_month: date,
    entry: ConfigEntry,
) -> bool:
    """Whether ``entry``'s month no archive holds waits for the card its
    feed-in settles on, whose read failed just now (``month_card_failed``).
    Only an entry whose stand-in reads that card waits for it: another entry
    on the same contract may have failed to read it."""
    from . import snapshot_months

    extractor = EXTRACTORS.get(supplier)
    if (
        extractor is None
        or extractor.stand_in_reference is None
        or not _reads_reference(entry)
    ):
        return False
    ref = extractor.stand_in_reference(contract, year_month, dt_util.now().date())
    return ref is not None and snapshot_months.month_card_failed(
        hass, supplier, ref[0], region, ref[1]
    )


def reference_rows_to_store(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: list[date],
    current: SupplierSnapshot | None,
    entry: ConfigEntry,
) -> dict[str, dict[str, dict[str, Any]]]:
    """The rows of the cards the entry's stand-in ``months`` settle their
    feed-in on, by contract, for the entry's own blob.

    They are another contract's rows, which ``monthly_rows_to_store`` does not
    write, so every restart read them again: two Bolt fixed cards in 2026,
    5 MB of PDF apiece with the card archive box off. Only rows the month
    cache holds are written.
    """
    from . import snapshot_months

    extractor = EXTRACTORS.get(supplier)
    if extractor is None:
        return {}
    wanted: dict[str, set[date]] = {}
    for month in months:
        ref = _reference(extractor, contract, month, current, entry)
        if ref is not None:
            wanted.setdefault(ref[0], set()).add(ref[1])
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for ref_contract, ref_months in wanted.items():
        rows = snapshot_months.monthly_rows_to_store(
            hass, supplier, ref_contract, region, sorted(ref_months)
        )
        if rows:
            out[ref_contract] = rows
    return out


def restore_reference_rows(
    hass: HomeAssistant, supplier: str, region: str, stored: dict[str, Any]
) -> int:
    """Seed the month cache with the rows :func:`reference_rows_to_store`
    wrote, returning the number restored (``restore_monthly_rows``)."""
    from . import snapshot_months

    restored = 0
    for contract, rows in stored.items():
        if isinstance(contract, str) and isinstance(rows, dict):
            restored += snapshot_months.restore_monthly_rows(
                hass, supplier, contract, region, rows
            )
    return restored
