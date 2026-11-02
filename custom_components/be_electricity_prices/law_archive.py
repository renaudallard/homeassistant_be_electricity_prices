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

"""The federal excise on electricity the card archive kept, by delivery month.

The integration reads article 419 on Justel itself (excise_law.py), and
Justel's consolidated text prints only the wording in force, so the months
of a wording an amendment replaced drop out of it. The card archive reads
the law the same way every day and keeps what the integration bills each
month on from it in ``law.json`` (``scripts/archive_cards.py``): the
household excise and the protected customer's. A month the law still gives
is written again, one it no longer gives keeps what was written.

It answers only for a month the law an entry holds does not give
(excise_law.py), ahead of the rates typed beside the law. Read daily by an
entry that lets the card archive be read, held for the process, and kept in
every entry's store so a restart without the network still knows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import LAW_TABLE_URL

# The kinds excise_law.py bills, which imports this module.
_KINDS: Final = ("standard", "protected")
# A rate outside these is a misread, not a rate (EUR/kWh excluding VAT).
_MAX_EUR_PER_KWH: Final = 0.2

# (year, month) -> kind -> the rate the archive kept for it.
_HELD: dict[tuple[int, int], dict[str, float]] = {}


def excise_for(kind: str, month: date) -> float | None:
    """The ``kind`` excise the archive kept for ``month``, EUR/kWh excluding
    VAT, or ``None``."""
    return _HELD.get((month.year, month.month), {}).get(kind)


def _entry(raw: Any) -> dict[str, float]:
    """The rates of one month that read, each on its own."""
    out: dict[str, float] = {}
    if not isinstance(raw, Mapping):
        return out
    for kind in _KINDS:
        value = raw.get(kind)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if 0.0 <= float(value) < _MAX_EUR_PER_KWH:
            out[kind] = float(value)
    return out


def _months(section: Any) -> dict[tuple[int, int], dict[str, float]]:
    """The months of the table that hold a rate that reads."""
    out: dict[tuple[int, int], dict[str, float]] = {}
    if not isinstance(section, Mapping):
        return out
    for label, raw in section.items():
        try:
            year, month = (int(part) for part in str(label).split("-"))
        except ValueError:
            continue
        entry = _entry(raw)
        if 1 <= month <= 12 and entry:
            out[(year, month)] = entry
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``law.json``), replacing what was held,
    unless it carries no month while something is held."""
    if "residential" not in table:
        return
    months = _months(table["residential"])
    if months or not _HELD:
        _HELD.clear()
        _HELD.update(months)


def restore(table: Any) -> None:
    """Hold ``table`` from an entry's store, when nothing is held yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched a newer table, so a stored copy never replaces one.
    """
    if not _HELD and isinstance(table, Mapping) and "residential" in table:
        _HELD.update(_months(table["residential"]))


def held_table() -> dict[str, dict[str, dict[str, float]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        "residential": {
            f"{year:04d}-{month:02d}": dict(entry)
            for (year, month), entry in sorted(_HELD.items())
        }
    }


_TABLE: Final = ArchiveTable(LAW_TABLE_URL, hold)


async def ensure_law_archive(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
