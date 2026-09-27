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
"""The rest of the calendar year, read off the one before it.

The year-end cost bills the whole calendar year through the year-to-date
walk, with the days from ``pivot`` onwards taken from last year's same days
and the months after the pivot's priced on ``card``, the card as it prices
today. Set only around that one walk, and read at the two places everything
in the walk goes through: the recorder read (:mod:`energy_meters`) and the
month's card (:func:`cohort._effective_snapshot_for_month`). Every rule in
between, the register pairs, the silent sides and the netting, then applies
to the rest of the year as it does to the days already metered.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .providers.base import SupplierSnapshot


@dataclass(frozen=True)
class YearAhead:
    """The first day read off last year, and the card billing the months
    after its own."""

    pivot: date
    card: SupplierSnapshot


YEAR_AHEAD: ContextVar[YearAhead | None] = ContextVar("YEAR_AHEAD", default=None)


def last_year(day: date) -> date:
    """``day`` a year earlier; 29 February, which has none, reads the 28th."""
    if (day.month, day.day) == (2, 29):
        return date(day.year - 1, 2, 28)
    return day.replace(year=day.year - 1)
