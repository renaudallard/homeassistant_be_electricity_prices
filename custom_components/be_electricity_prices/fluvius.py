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

One figure is taken from it: the "Maximumtarief" of a low-voltage connection
with peak metering, the ceiling the capacity term and the per-kWh network term
together may not exceed per kWh. It is one rate for the whole of Flanders,
which every area's sheet prints identically (0,3276168 in all eight for 2025
and 2026), so the first sheet the year's listing links to answers for all of
them.

Never raises, for the reason ``brugel.py`` gives: the fetch runs inside the
coordinator tick, whose only handler is for ``UpdateFailed``. A download or
parse failure logs and leaves the year unknown, and the card is then billed
with the ceiling it prints, as it was before the sheet was read.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from datetime import datetime
from typing import Final

import aiohttp
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from homeassistant.util import dt as dt_util

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
# The capacity rate's row. Its one figure sits in the column a peak-metered
# low-voltage connection is billed on, which is N from 2025 and O before,
# where a second "piekmeting" column for the transformer stations came first.
_CAPACITY_LABEL: Final = "Gemiddelde maandpiek"
_CEILING_LABEL: Final = "Maximumtarief"

# Successes are kept for the life of the process: the figure is set per
# calendar year. A failure is kept only briefly, so a blocked or slow Fluvius
# does not cost three downloads every tick while a transient one still heals.
_cache: dict[int, float] = {}
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
    cannot await. :func:`ensure_network_ceiling` is what puts a value here.
    """
    return _cache.get(year)


async def ensure_network_ceiling(
    session: aiohttp.ClientSession, year: int
) -> float | None:
    """Read the year's ceiling unless it is held, and return it or ``None``."""
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
            ceiling = await _fetch_ceiling(session, year)
        except Exception as err:  # the docstring promises no raise
            _LOGGER.warning("Fluvius %d tariff sheet could not be read: %s", year, err)
            ceiling = None
        if ceiling is None:
            _failed_at[year] = dt_util.utcnow()
            return None
        _cache[year] = ceiling
        _LOGGER.debug("Fluvius %d maximumtarief: %.7f EUR/kWh excl. VAT", year, ceiling)
        return ceiling


async def _fetch_ceiling(session: aiohttp.ClientSession, year: int) -> float | None:
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
    return parse_network_ceiling(payload, year)


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


def parse_network_ceiling(payload: bytes, year: int) -> float | None:
    """The maximumtarief a sheet prints for a peak-metered connection.

    ``None`` when the workbook is not the year's tariff list, prints no single
    capacity rate to find the column by or no maximumtarief, or prints a figure
    outside the bounds. Raises on a payload that is not a workbook; the caller
    logs it.
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
    except (KeyError, ValueError):
        return None
    return ceiling if _MIN_CEILING <= ceiling <= _MAX_CEILING else None


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
