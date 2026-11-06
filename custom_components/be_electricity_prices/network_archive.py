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

"""The network sheets the card archive kept, by delivery month and DSO.

A custom entry can take its network off its regulator's sheet: its Fluvius
area sheet (fluvius.py), Brugel's (brugel.py) or its operator's CWaPE grid
with the transport grid (cwape.py). The card archive reads the same sheets
every day through the same readers and keeps, for each month and DSO, the
overlay fields the sheet of the month's own year gives, excluding VAT, in
``network.json`` (``scripts/archive_cards.py``): a month still read is
written again, one no longer read keeps what was written.

It answers only for the fields an entry's own reading leaves unset for a
month (snapshot_resolve.py), ahead of the figures typed. Read daily by an
entry that takes its network from a sheet and lets the card archive be read,
held for the process, and kept in such an entry's store so a restart without
the network still knows it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any, Final

import aiohttp

from .archive_table import ArchiveTable
from .const import NETWORK_TABLE_URL

# The overlay fields a sheet sets (``overlay_fields`` in each reader), and
# the ones a DSO overlay cannot leave unset.
_FIELDS: Final = frozenset(
    {
        "distribution_single",
        "distribution_peak",
        "distribution_offpeak",
        "distribution_exclusive_night",
        "distribution_pic",
        "distribution_medium",
        "distribution_eco",
        "transport",
        "data_management_per_year",
        "capacity_eur_per_kw_year",
        "prosumer_eur_per_kva_year",
        "brussels_power_term_above_13kva",
    }
)
_REQUIRED: Final = frozenset(
    {"distribution_single", "transport", "data_management_per_year"}
)
# A figure outside these is a misread, not a tariff: EUR/kWh, and EUR a year
# or a kW or kVA a year for the yearly terms, excluding VAT.
_YEARLY: Final = frozenset(
    {
        "data_management_per_year",
        "capacity_eur_per_kw_year",
        "prosumer_eur_per_kva_year",
        "brussels_power_term_above_13kva",
    }
)
_MAX_EUR_PER_KWH: Final = 1.0
_MAX_EUR_PER_YEAR: Final = 1000.0

# (year, month) -> DSO key -> overlay field -> figure, excluding VAT.
_HELD: dict[tuple[int, int], dict[str, dict[str, float | None]]] = {}


def fields_for(month: date, dso: str) -> dict[str, float | None]:
    """The overlay fields the archive kept for ``dso``'s sheet in ``month``,
    excluding VAT, or none."""
    return dict(_HELD.get((month.year, month.month), {}).get(dso, {}))


def _fields(raw: Any) -> dict[str, float | None]:
    """One DSO's month, or nothing when any of its fields does not read: half
    of it would bill a network short of a charge."""
    out: dict[str, float | None] = {}
    if not isinstance(raw, Mapping):
        return out
    for field, value in raw.items():
        if field not in _FIELDS:
            return {}
        if value is None and field not in _REQUIRED:
            out[field] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return {}
        bound = _MAX_EUR_PER_YEAR if field in _YEARLY else _MAX_EUR_PER_KWH
        if not 0.0 <= float(value) < bound:
            return {}
        out[field] = float(value)
    return out


def _months(section: Any) -> dict[tuple[int, int], dict[str, dict[str, float | None]]]:
    """The months of the table that hold a DSO that reads."""
    out: dict[tuple[int, int], dict[str, dict[str, float | None]]] = {}
    if not isinstance(section, Mapping):
        return out
    for label, raw in section.items():
        try:
            year, month = (int(part) for part in str(label).split("-"))
        except ValueError:
            continue
        if not 1 <= month <= 12 or not isinstance(raw, Mapping):
            continue
        dsos = {
            str(dso): fields for dso, item in raw.items() if (fields := _fields(item))
        }
        if dsos:
            out[(year, month)] = dsos
    return out


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape of ``network.json``), replacing what was
    held, unless it carries no month while something is held."""
    if "months" not in table:
        return
    months = _months(table["months"])
    if months or not _HELD:
        _HELD.clear()
        _HELD.update(months)


def restore(table: Any) -> None:
    """Hold ``table`` from an entry's store, when nothing is held yet, for the
    reason law_archive.py gives."""
    if not _HELD and isinstance(table, Mapping) and "months" in table:
        _HELD.update(_months(table["months"]))


def held_table() -> dict[str, dict[str, dict[str, dict[str, float | None]]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        "months": {
            f"{year:04d}-{month:02d}": {
                dso: dict(fields) for dso, fields in dsos.items()
            }
            for (year, month), dsos in sorted(_HELD.items())
        }
    }


_TABLE: Final = ArchiveTable(NETWORK_TABLE_URL, hold)


async def ensure_network_archive(session: aiohttp.ClientSession) -> None:
    """Refresh the table from the card archive once a day (archive_table.py)."""
    await _TABLE.ensure(session)
