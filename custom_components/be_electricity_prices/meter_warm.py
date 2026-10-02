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

"""The one read per meter a tick answers every window from, kept between ticks.

A tick reads each meter's trailing year hour by hour once and derives every
narrower window from those rows (``energy_meters._recorder_rows``). The rows
of the closed days do not change from one hour to the next, yet each tick read
the whole year again: about 29 s every hour on a MariaDB on a NAS (issue
#107). So the coordinator keeps the rows, and an ordinary tick reads only the
days from the day before yesterday on and lays them over the kept ones.

Past days are not fixed for good. The whole year is read again on the first
tick of each day, after a restart or a reload (a settings edit reloads the
entry), when a read fails, when the re-read days hold no row, and when they do
not join the kept ones (an adjusted or rebuilt sum chain, a meter whose
statistics changed unit). Hours corrected or filled in further back without
moving the chain (a single hour, a gap imported later) are seen at the next
day's full read: until then the totals are right and the energy of a filled
gap sits on the hour after it.
"""

from __future__ import annotations

import math
from array import array
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from . import energy_meters
from .energy_meters import _bills_injection, _kwh_sensor_ids

# Re-read on an ordinary tick from the start of the day before yesterday: an
# hour Home Assistant compiles late, or recompiles at the edge, is read again
# for at least two days.
_TAIL_DAYS = 2

_FIELDS = frozenset({"change", "sum"})


@dataclass
class KeptRows:
    """One meter's hourly rows, packed one column per field.

    A year of rows as dicts is about 2,5 MB a meter, held for the life of the
    entry; as columns of doubles it is a tenth of that. A missing value is
    packed as NaN, which no statistic carries.
    """

    day: date
    start: date
    keys: tuple[str, ...]
    columns: tuple[array[float], ...]

    def rows(self) -> list[dict[str, Any]]:
        return [
            {key: None if math.isnan(v) else v for key, v in zip(self.keys, values)}
            for values in zip(*self.columns)
        ]


def _pack(day: date, start: date, rows: list[Any]) -> KeptRows | None:
    """``rows`` packed, or ``None`` when they do not all carry the same
    fields, which a packing would lose."""
    keys = tuple(rows[0]) if rows else ("start",)
    columns = tuple(array("d") for _ in keys)
    for row in rows:
        if tuple(row) != keys:
            return None
        for column, key in zip(columns, keys):
            value = row[key]
            column.append(math.nan if value is None else float(value))
    return KeptRows(day, start, keys, columns)


def _joins(head: list[Any], first: dict[str, Any]) -> bool:
    """Whether the re-read days carry on from the kept ones.

    Home Assistant takes a window's first change from the last row before
    it, so the first re-read row, less its change, is the sum the kept row
    before it holds, unless the chain moved underneath: an adjusted sum, a
    rebuilt chain, a unit changed.
    """
    total, change = first.get("sum"), first.get("change")
    prev = head[-1].get("sum") if head else None
    if total is None or change is None or prev is None:
        return False
    return math.isclose(total - change, prev, rel_tol=0.0, abs_tol=1e-6)


async def _spliced(
    hass: HomeAssistant,
    entity_id: str,
    start: date,
    end: date,
    kept: dict[str, KeptRows],
) -> list[Any] | None:
    """The year's rows, the kept ones with the last days read again, or
    ``None`` when they must all be read again."""
    held = kept.get(entity_id)
    if held is None or held.day != end or held.start != start:
        return None
    first = end - timedelta(days=_TAIL_DAYS)
    tail = await energy_meters._query_rows(hass, entity_id, first, end, "hour", _FIELDS)
    if tail is None:
        return None
    cut = dt_util.start_of_local_day(first).timestamp()
    head = [row for row in held.rows() if row["start"] < cut]
    # No row in the re-read days says nothing about the kept ones: a meter
    # that stopped, a statistic deleted or renamed, hours imported behind
    # the window. Each was billed off the kept rows until midnight, so such
    # a meter is read in full, which costs a dead meter one read an hour.
    if not head or not tail or not _joins(head, tail[0]):
        return None
    rows = head + tail
    packed = _pack(held.day, start, rows)
    if packed is None:
        kept.pop(entity_id, None)
    else:
        kept[entity_id] = packed
    return rows


async def warm_meter_reads(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    kept: dict[str, KeptRows] | None = None,
) -> None:
    """Read every meter the bill reads once, hour by hour, over ``start`` to
    ``end``, for the reads of the block to come to be answered from.

    The hourly rows answer an hourly read of any window inside them and a
    daily one too (``energy_meters._days_from_hours``), so one read per meter
    serves the yearly volume, the register check, the year and the month to
    date and both volume projections. Outside a ``memoise_meter_reads`` block
    it would read for nothing, so it does not read at all.

    With ``kept``, the coordinator's rows from earlier ticks, only the last
    days are read again (see the module notes); the rows the block is handed
    are the same either way.
    """
    memo = energy_meters._METER_MEMO.get()
    if memo is None:
        return
    sides = (
        ("consumption", "injection") if _bills_injection(entry) else ("consumption",)
    )
    for side in sides:
        for entity_id in dict.fromkeys(_kwh_sensor_ids(entry, side)):
            if not entity_id:
                continue
            rows = None
            if kept is not None:
                rows = await _spliced(hass, entity_id, start, end, kept)
            if rows is None:
                rows = await energy_meters._query_rows(
                    hass, entity_id, start, end, "hour", _FIELDS
                )
                if rows is None:
                    energy_meters._note_failed_read(entity_id)
                    if kept is not None:
                        kept.pop(entity_id, None)
                    continue
                if kept is not None:
                    packed = _pack(end, start, rows)
                    if packed is None:
                        kept.pop(entity_id, None)
                    else:
                        kept[entity_id] = packed
            memo.setdefault(("rows", entity_id, _FIELDS), []).append(
                ("hour", start, end, rows)
            )
