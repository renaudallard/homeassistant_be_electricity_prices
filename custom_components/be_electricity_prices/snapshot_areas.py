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

"""A network area a month's card lacks, from the nearest card of its year
that prints it (``_with_nearest_area``, applied by ``snapshot_months.month_card``).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date

from homeassistant.util import dt as dt_util

from .providers.base import SupplierSnapshot

_LOGGER = logging.getLogger(__name__)


def _nearest_months(year_month: date, last: date) -> list[date]:
    """The months asked for a network area ``year_month``'s card lacks, in
    order: the other months of its calendar year, nearest first and the
    earlier of two equally near first, none after ``last``, which has no card
    yet.

    Only its own year: network tariffs change on 1 January, and Wallonia's of
    2026 are not even shaped like those of 2025, so another year's card would
    bill the month on a network it was never charged.
    """
    months = [
        date(year_month.year, month, 1)
        for away in range(1, 12)
        for month in (year_month.month - away, year_month.month + away)
        if 1 <= month <= 12
    ]
    return [month for month in months if month <= last]


# Whether the month cache holds a row for a month, and the row: None both for
# a month it does not hold and for one held as having no card.
type _Held = tuple[bool, SupplierSnapshot | None]


def _short_of(card: SupplierSnapshot, dso: str) -> bool:
    """Whether ``card`` prints a network table that lacks ``dso``.

    Not a card with no table at all: the Walloon cards of October to
    December 2025 were read before their layout was, and searching their
    year for the area asked the supplier's archive for each month before
    them, nine downloads for an Engie October, to bill a month whose own
    network was never read. Such a month is dropped as it always was.
    """
    return bool(dso) and bool(card.dsos) and dso not in card.dsos


def _ends_search(dso: str, held: _Held) -> bool:
    """Whether the search for ``dso`` stops on a month: one not held once
    asked, or one whose card prints the area."""
    known, row = held
    return not known or (row is not None and dso in row.dsos)


def _search_end(
    dso: str, year_month: date, last: date, held: Callable[[date], _Held]
) -> date | None:
    """The month the search for ``dso`` stopped on, over what the month cache
    holds once ``_with_nearest_area`` has run, or None where it found no card
    of the year printing the area. ``nearer_card_not_read`` reads it to tell a
    search that stopped on a failed read."""
    return next(
        (m for m in _nearest_months(year_month, last) if _ends_search(dso, held(m))),
        None,
    )


async def _with_nearest_area(
    card: SupplierSnapshot,
    dso: str,
    year_month: date,
    row: Callable[[date], Awaitable[SupplierSnapshot | None]],
    held: Callable[[date], _Held],
) -> SupplierSnapshot:
    """``card`` with the network figures of ``dso`` taken from the nearest
    month's card that prints them, where its own table lacks the area
    (``_short_of``).

    A closed month's card keeps a table short of an area
    (``require_every_area``): its energy, taxes and fees are still what the
    month is billed on, and every cost path dropped the month whole on the
    missing area. Only that area's overlay comes from the other card, of the
    same calendar year, and where no card of that year prints the area the
    month is still dropped.

    The running month takes part both ways. Its own card, where the month
    walk reads it short (Bolt's Walloon card can lose a row; the suppliers
    on ``running_month_short_of_an_area`` refuse it), takes the area from a
    closed card of its year, while the price of the current hour still
    comes from the live card as fetched, which nothing here completes. And
    it lends its area to a closed month, though it is never written to the
    store, so the first tick after a restart drops that month until the
    warm-up refresh.

    The months are asked in a fixed order (``_nearest_months``), each
    through the month cache, which decides what it asks again. One whose row
    is not held once asked, a failed read or a month ``cached_only`` may not
    fetch, ends the search with the card as it is: a farther card taken there
    would bill the month differently once the nearer one is read, so the
    month is dropped as before until a walk can read it, and
    ``nearer_card_not_read`` names the month whose read failed.
    """
    if not _short_of(card, dso):
        return card
    for month in _nearest_months(year_month, dt_util.now().date().replace(day=1)):
        await row(month)
        ended = held(month)
        if not _ends_search(dso, ended):
            continue
        other = ended[1]
        if other is None:
            return card
        _LOGGER.debug(
            "the %s/%s card for %s has no row for %s; billed on %s's",
            card.supplier,
            card.contract,
            year_month,
            dso,
            month,
        )
        return replace(card, dsos={**card.dsos, dso: other.dsos[dso]})
    return card
