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

"""The archive's row standing in for a card an entry cannot have.

A card published as page images is priced off the archive's row for the
running month, a withdrawn product off the last month it was sold, and an
entry whose card cannot be had or has gone stale off the newest row the
repository's card archive holds for it. All honour the entry's card-archive
box through ``_archive_allowed``.

The archive is read through ``snapshot_months`` at call time rather than
imported by name, so a patch on its reader there reaches these too.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from . import snapshot_months
from .const import CONF_DSO
from .providers.base import SupplierSnapshot
from .snapshot_months import ArchivedCard, _archive_allowed


def month_before(day: date) -> date:
    """The first day of the month before ``day``'s."""
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


async def _stand_in(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    month: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for ``month`` where it prints the entry's network
    area, or None.

    A closed month's row may be short of an area, since its card is still
    what that month is billed on and the month cache takes the area from
    another card of its year (``_with_nearest_area``). Standing in for the
    entry's own card it would leave an entry on that area with no network
    figures, and every tick failing on it, where the stale card held still
    prices it.

    The row takes the place of the entry's own card, which prices the
    running month. A row of an earlier month has what settled its own month
    taken off it (:func:`_unsettled`); the running month's row keeps it, since
    that month is its own: a card indexed on a forward price names its own
    month's value from the day it is out.
    """
    archived = await snapshot_months._archived_card_from_github(
        session, supplier, contract, region, month
    )
    dso = entry.data.get(CONF_DSO, "") if entry is not None else ""
    if archived is None or (dso and dso not in archived.snapshot.dsos):
        return None
    if month == dt_util.now().date().replace(day=1):
        return archived
    card = _unsettled(archived.snapshot)
    return archived if card is archived.snapshot else replace(archived, snapshot=card)


def _unsettled(snapshot: SupplierSnapshot) -> SupplierSnapshot:
    """``snapshot`` without what settled the month it is the card of: the
    index that month closed at (``index_realised``) and the mark that its
    figures are the ones the supplier stated it settled at
    (``settled_as_stated``), on both legs.

    Both describe that one month. A row adopted as the entry's card for a
    later month stands in for every month after its own, and carrying them
    across settled each of those on the row's month: a withdrawn Trevion
    LifePowr entry priced off its September 2026 row billed October's energy
    at September's index in the year to date, 0,20358 EUR/kWh where the live
    tick billed 0,12932 on October's own mean. The same object back when
    there is nothing to take.
    """

    def leg(rates: Any) -> Any:
        changes: dict[str, object] = {}
        if getattr(rates, "index_realised", None) is not None:
            changes["index_realised"] = None
        if getattr(rates, "settled_as_stated", False):
            changes["settled_as_stated"] = False
        return replace(rates, **changes) if changes else rates

    energy = leg(snapshot.energy)
    injection = None if snapshot.injection is None else leg(snapshot.injection)
    if energy is snapshot.energy and injection is snapshot.injection:
        return snapshot
    return replace(snapshot, energy=energy, injection=injection)


async def card_for_unreadable_month(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the RUNNING month, for a card nobody can read.

    The last resort, and reached only on a card that downloaded fine and
    carries no text layer. A supplier publishing page images leaves a parser
    nothing to work with, but the repository's daily walk reads those with an
    OCR engine and files what it gets, so the row is the one place a price
    for this month exists at all.

    Asking for the running month is exactly what ``_card_archive_may_hold``
    refuses, and rightly: while a card can be read the live parse is the
    better answer and the archive's copy is a day behind at best. That
    reasoning runs out when the card cannot be read, which is the only door
    into this function.

    Honours the entry's card-archive box, which exists so a household can
    keep the integration from contacting GitHub at all.
    """
    if not _archive_allowed(entry):
        return None
    return await _stand_in(
        session, supplier, contract, region, today.replace(day=1), entry
    )


async def card_of_month_before(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    day: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the month before ``day``'s, for an entry left
    with no card. Honours the card-archive box like
    ``card_for_unreadable_month``.

    A withdrawn product whose card the supplier took down is priced off the
    last month it was sold, the month before ``withdrawn``.
    """
    if not _archive_allowed(entry):
        return None
    return await _stand_in(
        session, supplier, contract, region, month_before(day), entry
    )


# How far back the card archive is asked for a card to stand in, a month at a
# time: a year, as gas does, though the archive keeps 36 months of rows.
_ARCHIVE_MONTHS_BACK = 12


async def newest_archived_card(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> tuple[date, ArchivedCard] | None:
    """The newest row the archive holds for this card, and its month.

    This month's row first, then back a month at a time, stopping at the
    first one that holds a card printing the entry's network area. For an entry whose supplier's card cannot be
    had or has gone stale: the archive walks every supplier daily, and its
    row may well be newer than what the supplier answered this entry. A
    transient failure propagates, since a month it could not read may still
    hold one. Honours the card-archive box like the reader above.
    """
    if not _archive_allowed(entry):
        return None
    month = today.replace(day=1)
    for _ in range(_ARCHIVE_MONTHS_BACK):
        archived = await _stand_in(session, supplier, contract, region, month, entry)
        if archived is not None:
            return month, archived
        month = month_before(month)
    return None
