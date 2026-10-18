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

"""Fluvius's yearly tariff sheet for the Flemish distribution network.

The periodic distribution tariffs the Vlaamse Nutsregulator approves are
published by Fluvius as one small workbook per network area and year,
"Tarieflijst periodieke distributienettarieven <year> - Afname", excluding
VAT. vreg.be answers nothing from this host, so this is the official copy that
can be read.

Two figures are taken from it, both rules for the whole of Flanders, so the
first sheet the year's listing links to answers for every area:

- the "Maximumtarief" of a low-voltage connection with peak metering, the
  ceiling the capacity term and the per-kWh network term together may not
  exceed per kWh, which every area's sheet prints identically (0,3276168 in
  all eight for 2025 and 2026);
- the minimum monthly peak the capacity term is billed on. The sheet states it
  as the "minimale bijdrage" in EUR a year, which divided by the area's
  capacity rate gives 2,50 kW on every sheet from 2023 to 2026.

Never raises, for the reason ``brugel.py`` gives: the fetch runs inside the
coordinator tick, whose only handler is for ``UpdateFailed``. A download or
parse failure logs and leaves the year unknown. The card is then billed with
the ceiling it prints and the floor falls back to ``VREG_CAPACITY_FLOOR_KW``,
as they were before the sheet was read.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import aiohttp
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from homeassistant.util import dt as dt_util

from .const import VREG_CAPACITY_FLOOR_KW
from .providers._parse import to_float
from .providers._pdf import USER_AGENT, fetch_text, guard_redirect, read_capped
from .synergrid import _NS, _column_of, _resolve_sheet_path, _shared_strings

_LOGGER = logging.getLogger(__name__)

_SITE: Final = "https://www.fluvius.be"
_LISTING_URL: Final = (
    f"{_SITE}/nl/factuur-en-tarieven/netkosten/netkosten-afname-elektriciteit"
)
# The listing links each area's sheet through a page of its own, named after
# the year it covers: ...-distributienettarief-afname-elektriciteit-01012026-31122026.
_PUBLICATION_RE: Final = (
    r'href="(/nl/publicatie/[a-z0-9-]+-afname-elektriciteit-0101{year}-3112{year})"'
)
_SHEET_RE: Final = re.compile(r'href="(/sites/fluvius/files/[^"]+\.xlsx)"')
_TIMEOUT: Final = 30
_FAILURE_RETRY_S: Final = 6 * 3600
# A sheet is about 25 KB zipped and a few hundred KB unpacked. The cap on the
# unpacked size refuses a zip bomb before anything is read out of it.
_MAX_UNPACKED_BYTES: Final = 4 * 1024 * 1024
# Sized on the slip it catches, a factor of ten or a column read as a rate,
# not on what the tariff ought to be.
_MIN_CEILING: Final = 0.1
_MAX_CEILING: Final = 1.0
_MIN_FLOOR_KW: Final = 0.5
_MAX_FLOOR_KW: Final = 10.0
# The capacity rate's row. Its one figure sits in the column a peak-metered
# low-voltage connection is billed on, which is N from 2025 and O before,
# where a second "piekmeting" column for the transformer stations came first.
_CAPACITY_LABEL: Final = "Gemiddelde maandpiek"
_CEILING_LABEL: Final = "Maximumtarief"
# "Laagspanningsklanten met piekmeting betalen een minimale bijdrage van 140,31
# euro/jaar", in the remarks under the table.
_MINIMUM_RE: Final = re.compile(r"minimale bijdrage van (\d+(?:,\d+)?) euro/jaar")


@dataclass(frozen=True)
class SheetTerms:
    """What a year's sheet gives: the ceiling in EUR/kWh excl. VAT, and the
    minimum monthly peak in kW, ``None`` where the sheet states no minimum
    contribution to derive it from."""

    ceiling: float
    floor_kw: float | None


# Successes are kept for the life of the process: the figures are set per
# calendar year. A failure is kept only briefly, so a blocked or slow Fluvius
# does not cost three downloads every tick while a transient one still heals.
_cache: dict[int, SheetTerms] = {}
_failed_at: dict[int, datetime] = {}
# One fetch per year at a time, for the reason brugel.py gives.
_locks: dict[int, asyncio.Lock] = {}


def _lock(year: int) -> asyncio.Lock:
    """The lock for ``year``, created on first use, on the event loop."""
    lock = _locks.get(year)
    if lock is None:
        lock = _locks[year] = asyncio.Lock()
    return lock


def cached_network_ceiling(year: int) -> float | None:
    """The year's ceiling if it has been read, EUR/kWh excl. VAT, else ``None``.

    Synchronous on purpose: the snapshot resolver runs in the pricing path and
    cannot await. :func:`ensure_tariff_sheet` is what puts a value here.
    """
    terms = _cache.get(year)
    return None if terms is None else terms.ceiling


def capacity_floor_kw(year: int | None = None) -> float:
    """The minimum monthly peak the capacity term is billed on, in kW.

    The year's sheet (this year's by default), else the latest earlier one
    held, else ``VREG_CAPACITY_FLOOR_KW``: never a later year's, since the rule
    is applied to the months it was set for.
    """
    year = dt_util.now().year if year is None else year
    held = {
        held_year: terms.floor_kw
        for held_year, terms in _cache.items()
        if held_year <= year and terms.floor_kw is not None
    }
    floor = held[max(held)] if held else None
    return VREG_CAPACITY_FLOOR_KW if floor is None else floor


async def ensure_tariff_sheet(
    session: aiohttp.ClientSession, year: int
) -> SheetTerms | None:
    """Read the year's sheet unless it is held, and return it or ``None``."""
    hit = _cache.get(year)
    if hit is not None:
        return hit
    async with _lock(year):
        hit = _cache.get(year)
        if hit is not None:
            return hit
        failed = _failed_at.get(year)
        if failed is not None:
            if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
                return None
            del _failed_at[year]
        try:
            terms = await _fetch_sheet(session, year)
        except Exception as err:  # the docstring promises no raise
            _LOGGER.warning("Fluvius %d tariff sheet could not be read: %s", year, err)
            terms = None
        if terms is None:
            _failed_at[year] = dt_util.utcnow()
            return None
        _cache[year] = terms
        _LOGGER.debug(
            "Fluvius %d maximumtarief %.7f EUR/kWh excl. VAT, minimum peak %s kW",
            year,
            terms.ceiling,
            terms.floor_kw,
        )
        return terms


async def _fetch_sheet(session: aiohttp.ClientSession, year: int) -> SheetTerms | None:
    """The listing, the year's first publication page, then its workbook."""
    listing = await fetch_text(session, _LISTING_URL, timeout=_TIMEOUT)
    publication = re.search(_PUBLICATION_RE.format(year=year), listing)
    if publication is None:
        _LOGGER.debug("Fluvius lists no %d tariff sheet yet", year)
        return None
    page = await fetch_text(session, _SITE + publication.group(1), timeout=_TIMEOUT)
    sheet = _SHEET_RE.search(page)
    if sheet is None:
        _LOGGER.debug("Fluvius %d publication page links no workbook", year)
        return None
    url = _SITE + sheet.group(1)
    async with session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
    ) as resp:
        guard_redirect(url, resp)
        resp.raise_for_status()
        payload = await read_capped(resp, url)
    return parse_sheet(payload, year)


def _rows(payload: bytes) -> list[dict[str, str]]:
    """The first worksheet's cells as text, one ``{column: text}`` per row."""
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        if sum(info.file_size for info in z.infolist()) > _MAX_UNPACKED_BYTES:
            raise ValueError("workbook unpacks past the size cap")
        strings = _shared_strings(z)
        sheet = ET.fromstring(z.read(_resolve_sheet_path(z, "")))
    rows: list[dict[str, str]] = []
    for row in sheet.iter(_NS + "row"):
        cells: dict[str, str] = {}
        for cell in row.iter(_NS + "c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(_NS + "t"))
            else:
                value = cell.find(_NS + "v")
                if value is None or value.text is None:
                    continue
                text = strings[int(value.text)] if kind == "s" else value.text
            cells[_column_of(cell.get("r", ""))] = text.strip()
        rows.append(cells)
    return rows


def parse_sheet(payload: bytes, year: int) -> SheetTerms | None:
    """The maximumtarief and minimum peak a sheet gives a peak-metered
    connection.

    ``None`` when the workbook is not the year's tariff list, prints no single
    capacity rate to find the column by or no maximumtarief, or prints a
    ceiling outside the bounds. A minimum that cannot be read or lands outside
    its own bounds leaves ``floor_kw`` at ``None`` and the ceiling stands.
    Raises on a payload that is not a workbook; the caller logs it.
    """
    rows = _rows(payload)
    title = f"periodieke distributienettarieven {year}"
    if not any(title in text for row in rows for text in row.values()):
        return None
    capacity = _labelled_row(rows, _CAPACITY_LABEL)
    ceiling_row = _labelled_row(rows, _CEILING_LABEL)
    if capacity is None or ceiling_row is None:
        return None
    column = _number_column(capacity)
    if column is None:
        return None
    try:
        ceiling = float(ceiling_row[column])
        rate = float(capacity[column])
    except (KeyError, ValueError):
        return None
    if not _MIN_CEILING <= ceiling <= _MAX_CEILING:
        return None
    return SheetTerms(ceiling, _floor_kw(rows, rate))


def _floor_kw(rows: list[dict[str, str]], rate: float) -> float | None:
    """The minimum contribution over the capacity rate, to the hundredth of a
    kW the contribution's cents allow, or ``None``."""
    for row in rows:
        for text in row.values():
            match = _MINIMUM_RE.search(text)
            if match is None or rate <= 0.0:
                continue
            floor = round(to_float(match.group(1)) / rate, 2)
            return floor if _MIN_FLOOR_KW <= floor <= _MAX_FLOOR_KW else None
    return None


def _labelled_row(rows: list[dict[str, str]], label: str) -> dict[str, str] | None:
    """The first row a cell of which starts with ``label``."""
    return next(
        (row for row in rows if any(text.startswith(label) for text in row.values())),
        None,
    )


def _number_column(row: dict[str, str]) -> str | None:
    """The column of the only number in ``row``, or ``None``."""
    numbers = []
    for col, text in row.items():
        try:
            float(text)
        except ValueError:
            continue
        numbers.append(col)
    return numbers[0] if len(numbers) == 1 else None
