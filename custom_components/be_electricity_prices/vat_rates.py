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
"""The Belgian VAT rates, by delivery month.

The residential rate (the reduced one households pay on electricity) and the
standard one a professional connection pays. A card that states its own rate
keeps it for everything on its own basis; these answer for everything else:
a formula a parser grossed on an assumed rate, a regulated figure published
excluding VAT, a professional card's basis, and a signing cohort's leg billed
in a later month.

Keyed by the month billed, never applied backwards: every month bills at its
own rate. A month the table does not hold takes the last month before it
that it does, then the constants in ``const.py``.
"""

from __future__ import annotations

from datetime import date

from .const import VAT_RATE_REDUCED, VAT_RATE_STANDARD

# (year, month) -> (reduced, standard), as held. Empty until a table is held.
_HELD: dict[tuple[int, int], tuple[float, float]] = {}


def _held_for(month: date) -> tuple[float, float] | None:
    key = (month.year, month.month)
    if key in _HELD:
        return _HELD[key]
    earlier = [k for k in _HELD if k < key]
    return _HELD[max(earlier)] if earlier else None


def residential_vat(month: date) -> float:
    """The reduced rate a household pays on electricity delivered in ``month``."""
    held = _held_for(month)
    return VAT_RATE_REDUCED if held is None else held[0]


def standard_vat(month: date) -> float:
    """The standard rate a professional connection pays in ``month``."""
    held = _held_for(month)
    return VAT_RATE_STANDARD if held is None else held[1]
