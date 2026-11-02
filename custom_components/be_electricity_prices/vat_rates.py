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

The table is the card archive's ``vat.json``: per month, the rate the cards
agree on, three suppliers or more stating one rate and none another
(``scripts/archive_cards.py``). A month they disagree on is not in it. Keyed
by the month billed and never applied backwards: a month the table does not
hold takes the last month before it that it does, then the constants in
``const.py``. Held for the process, refreshed daily, and kept in every
entry's store so a restart without the network still knows the last one.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import VAT_RATE_REDUCED, VAT_RATE_STANDARD, VAT_TABLE_URL

# A rate outside these is a misread, not a rate.
_MIN_RATE: Final = 0.0
_MAX_RATE: Final = 0.5

# (year, month) -> rate, as held.
_RESIDENTIAL: dict[tuple[int, int], float] = {}
_STANDARD: dict[tuple[int, int], float] = {}


def _rate_for(table: Mapping[tuple[int, int], float], month: date) -> float | None:
    key = (month.year, month.month)
    if key in table:
        return table[key]
    earlier = [k for k in table if k < key]
    return table[max(earlier)] if earlier else None


def residential_vat(month: date) -> float:
    """The reduced rate a household pays on electricity delivered in ``month``."""
    rate = _rate_for(_RESIDENTIAL, month)
    return VAT_RATE_REDUCED if rate is None else rate


def standard_vat(month: date) -> float:
    """The standard rate a professional connection pays in ``month``."""
    rate = _rate_for(_STANDARD, month)
    return VAT_RATE_STANDARD if rate is None else rate


def _months(section: Any) -> dict[tuple[int, int], float]:
    """The agreed months of one section of the table, bounded."""
    out: dict[tuple[int, int], float] = {}
    if not isinstance(section, Mapping):
        return out
    for label, entry in section.items():
        rate = entry.get("rate") if isinstance(entry, Mapping) else None
        try:
            year, month = (int(part) for part in str(label).split("-"))
            rate = float(rate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if 1 <= month <= 12 and _MIN_RATE <= rate < _MAX_RATE:
            out[(year, month)] = rate
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``vat.json``), replacing what was held for
    each section it carries. A section it lacks is left as it was."""
    for key, held in (("residential", _RESIDENTIAL), ("standard", _STANDARD)):
        if key in table:
            months = _months(table[key])
            if months or not held:
                held.clear()
                held.update(months)


def restore(table: Mapping[str, Any]) -> None:
    """Hold ``table`` from an entry's store, for a section nothing holds yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched a newer table, so a stored copy never replaces one.
    """
    for key, held in (("residential", _RESIDENTIAL), ("standard", _STANDARD)):
        if not held and key in table:
            held.update(_months(table[key]))


def held_table() -> dict[str, dict[str, dict[str, float]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        key: {
            f"{y:04d}-{m:02d}": {"rate": rate} for (y, m), rate in sorted(held.items())
        }
        for key, held in (("residential", _RESIDENTIAL), ("standard", _STANDARD))
    }


_TABLE: Final = ArchiveTable(VAT_TABLE_URL, hold)


async def ensure_vat_rates(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
