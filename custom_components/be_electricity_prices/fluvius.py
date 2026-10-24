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

A third is the area's own, and is read off the entry's area sheet only for an
entry on an analog meter: what such a meter pays per kWh over a peak-metered
one. Fluvius cannot measure an analog meter's peak, "Je betaalt dan een
forfaitair bedrag voor 2.5 kW als maandpiek en een aanvullend tarief voor je
verbruik in kWh": the capacity tariff on the minimum peak, which is the "Vaste
term" of the sheet's analog column to the cent on every sheet from 2023 to
2026, and a higher kWh rate. The public service and surcharge rates are the
same in both columns, so the difference of the two kWh rates is the whole of
it, on the normal and the exclusive-night kWh alike.

Never raises, for the reason ``brugel.py`` gives: the fetch runs inside the
coordinator tick, whose only handler is for ``UpdateFailed``. A download or
parse failure logs and leaves the year unknown. The card is then billed with
the ceiling it prints and the floor falls back to ``VREG_CAPACITY_FLOOR_KW``,
as they were before the sheet was read, and an analog meter is billed the
peak-metered kWh rate.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import zipfile
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import aiohttp
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from homeassistant.util import dt as dt_util

from .const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    VREG_CAPACITY_FLOOR_KW,
)
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
# One area's page, named the same way. Antwerpen, Limburg and West have one
# from 2023, the other five from 2025, when they took the names the DSO keys
# stand for: before it the listing names the merged operators (Iveka,
# Intergem, Iverlek, ...), which match no key one to one, so an analog meter
# there is billed the peak-metered kWh rate for those years.
_AREA_PUBLICATION_RE: Final = (
    r'href="(/nl/publicatie/fluvius-{area}-distributienettarief-afname-'
    r'elektriciteit-0101{year}-3112{year})"'
)
_AREA_SLUGS: Final[dict[str, str]] = {
    DSO_FLUVIUS_ANTWERPEN: "antwerpen",
    DSO_FLUVIUS_HALLE_VILVOORDE: "halle-vilvoorde",
    DSO_FLUVIUS_IMEWO: "imewo",
    DSO_FLUVIUS_INTERGEM: "midden-vlaanderen",
    DSO_FLUVIUS_IVEKA: "kempen",
    DSO_FLUVIUS_LIMBURG: "limburg",
    DSO_FLUVIUS_WEST: "west",
    DSO_FLUVIUS_ZENNE_DIJLE: "zenne-dijle",
}
_SHEET_RE: Final = re.compile(r'href="(/sites/fluvius/files/[^"]+\.xlsx)"')
_TIMEOUT: Final = 30
_FAILURE_RETRY_S: Final = 6 * 3600
# How long the listing every sheet is found from is kept, its failure too: the
# few minutes one tick or one backfill spends reading sheets. The year's sheet
# and an analog meter's area sheet then cost one listing download between
# them, and a Fluvius that does not answer one timeout rather than one a sheet.
_LISTING_TTL_S: Final = 300
# A sheet is about 25 KB zipped and a few hundred KB unpacked. The cap on the
# unpacked size refuses a zip bomb before anything is read out of it.
_MAX_UNPACKED_BYTES: Final = 4 * 1024 * 1024
# Sized on the slip it catches, a factor of ten or a column read as a rate,
# not on what the tariff ought to be.
_MIN_CEILING: Final = 0.1
_MAX_CEILING: Final = 1.0
_MIN_FLOOR_KW: Final = 0.5
_MAX_FLOOR_KW: Final = 10.0
_MAX_ANALOG_SURCHARGE: Final = 0.2
# The capacity rate's row. Its one figure sits in the column a peak-metered
# low-voltage connection is billed on, which is N from 2025 and O before,
# where a second "piekmeting" column for the transformer stations came first.
_CAPACITY_LABEL: Final = "Gemiddelde maandpiek"
_CEILING_LABEL: Final = "Maximumtarief"
# "Laagspanningsklanten met piekmeting betalen een minimale bijdrage van 140,31
# euro/jaar", in the remarks under the table.
_MINIMUM_RE: Final = re.compile(r"minimale bijdrage van (\d+(?:,\d+)?) euro/jaar")
# The analog meter's first row, whose figure sits in the analog column first
# and the reversing prosumer meter's after it, both "Vaste term" in EUR a year.
_ANALOG_LABEL: Final = "Vaste term"
# Two rows carry exactly this label: the peak-metered connection's kWh rate in
# its column and the analog meter's in its own.
_KWH_LABEL: Final = "kWh-tarief"
# Billed on every kWh of both meters, so they have to agree for the two kWh
# rates to be the whole difference.
_SHARED_LABELS: Final = (
    "kWh-tarief normaal",
    "kWh-tarief exclusief nacht",
    "Tarieven voor de toeslagen",
)


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
# The analog surcharge, per year and DSO key, kept the same way.
_analog: dict[tuple[int, str], float] = {}
_analog_failed_at: dict[tuple[int, str], datetime] = {}
# One fetch per sheet at a time, for the reason brugel.py gives.
_locks: dict[Hashable, asyncio.Lock] = {}
# The listing as last fetched, or what fetching it raised, and when.
_listing: tuple[datetime, str | Exception] | None = None


def _lock(key: Hashable) -> asyncio.Lock:
    """The lock for ``key``, created on first use, on the event loop."""
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
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


def cached_analog_surcharge(year: int, dso: str) -> float | None:
    """What an analog meter in ``dso``'s area pays per kWh over a peak-metered
    one, EUR/kWh excl. VAT, or ``None`` before any of its sheets is read.

    The year's sheet, else the latest earlier one held, never a later one, the
    rule :func:`capacity_floor_kw` follows: the year ahead is priced on the
    card in hand, and the sheet of a year Fluvius has not published yet is
    best stood in for by the one before it. Synchronous for the reason
    :func:`cached_network_ceiling` gives.
    """
    held = {
        held_year: surcharge
        for (held_year, area), surcharge in _analog.items()
        if area == dso and held_year <= year
    }
    return held[max(held)] if held else None


async def ensure_tariff_sheet(
    session: aiohttp.ClientSession, year: int
) -> SheetTerms | None:
    """Read the year's sheet unless it is held, and return it or ``None``."""
    return await _held_or_read(
        year,
        _cache,
        _failed_at,
        lambda: _fetch_sheet(session, year),
        f"{year} tariff sheet",
    )


async def ensure_analog_surcharge(
    session: aiohttp.ClientSession, year: int, dso: str
) -> float | None:
    """Read the analog surcharge off ``dso``'s own sheet for ``year`` unless it
    is held, and return it or ``None``, never raising.

    Only for an entry on an analog meter, which nothing else needs the area's
    own sheet for. ``None`` for a key that is not a Fluvius area. Where the
    listing has no sheet for the year yet, or the one it has no longer reads,
    the year before is read off the same listing to stand in
    (:func:`cached_analog_surcharge`); a network error asks for nothing more.
    """
    area = _AREA_SLUGS.get(dso)
    if area is None:
        return None
    return await _held_or_read(
        (year, dso),
        _analog,
        _analog_failed_at,
        lambda: _fetch_analog_surcharge(session, year, dso, area),
        f"{year} {area} tariff sheet",
    )


async def _held_or_read[K: Hashable, V](
    key: K,
    held: dict[K, V],
    failed_at: dict[K, datetime],
    read: Callable[[], Awaitable[V | None]],
    what: str,
) -> V | None:
    """``held[key]``, read once and kept, or ``None`` while the read fails,
    asking again only after the backoff."""
    hit = held.get(key)
    if hit is not None:
        return hit
    async with _lock(key):
        hit = held.get(key)
        if hit is not None:
            return hit
        failed = failed_at.get(key)
        if failed is not None:
            if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
                return None
            del failed_at[key]
        try:
            value = await read()
        except Exception as err:  # the docstring promises no raise
            _LOGGER.warning("Fluvius %s could not be read: %s", what, err)
            value = None
        if value is None:
            failed_at[key] = dt_util.utcnow()
            return None
        held[key] = value
        return value


async def _fetch_sheet(session: aiohttp.ClientSession, year: int) -> SheetTerms | None:
    """The year's first sheet, read for the rules of the whole of Flanders."""
    listing = await _fetch_listing(session)
    payload = await _fetch_workbook(session, listing, _PUBLICATION_RE.format(year=year))
    if payload is None:
        _LOGGER.debug("Fluvius gives no %d tariff sheet yet", year)
        return None
    terms = parse_sheet(payload, year)
    if terms is not None:
        _LOGGER.debug(
            "Fluvius %d maximumtarief %.7f EUR/kWh excl. VAT, minimum peak %s kW",
            year,
            terms.ceiling,
            terms.floor_kw,
        )
    return terms


async def _fetch_analog_surcharge(
    session: aiohttp.ClientSession, year: int, dso: str, area: str
) -> float | None:
    """The analog surcharge off one area's sheet for ``year``, the year before
    read in its place where the listing has no sheet for it yet or the one it
    has no longer reads. A network error asks for nothing more."""
    listing = await _fetch_listing(session)
    listed = (
        re.search(_AREA_PUBLICATION_RE.format(area=area, year=year), listing)
        is not None
    )
    if listed:
        surcharge = await read_analog_surcharge(session, year, area)
        if surcharge is not None:
            return surcharge
    stand_in = None
    if re.search(_AREA_PUBLICATION_RE.format(area=area, year=year - 1), listing):
        stand_in = await ensure_analog_surcharge(session, year - 1, dso)
    if listed:
        # The sheet is there and no longer reads: a reader to fix, whatever
        # stands in meanwhile.
        _LOGGER.warning(
            "Fluvius %d tariff sheet for %s gives no analog kWh rate: an analog "
            "meter there is billed %s",
            year,
            area,
            "the peak-metered one" if stand_in is None else f"the {year - 1} one",
        )
    elif stand_in is not None:
        _LOGGER.debug(
            "Fluvius gives no %d tariff sheet for %s yet: its %d one stands in",
            year,
            area,
            year - 1,
        )
    elif year < dt_util.now().year:
        # A closed year whose sheet is not coming: nothing heals, so a
        # backfill reaching it is not warned of every time it runs.
        _LOGGER.debug(
            "Fluvius has no %d tariff sheet for %s: an analog meter's kWh "
            "there are billed the peak-metered rate for that year",
            year,
            area,
        )
    else:
        _LOGGER.warning(
            "Fluvius gives no %d tariff sheet for %s: an analog meter there "
            "is billed the peak-metered kWh rate until it does",
            year,
            area,
        )
    return None


async def read_analog_surcharge(
    session: aiohttp.ClientSession, year: int, area: str
) -> float | None:
    """One area's analog surcharge for ``year`` as its sheet gives it, or
    ``None`` where the listing has no such sheet or it does not read. Raises on
    a network error, for the live check to tell the two apart."""
    listing = await _fetch_listing(session)
    payload = await _fetch_workbook(
        session, listing, _AREA_PUBLICATION_RE.format(area=area, year=year)
    )
    return None if payload is None else parse_analog_surcharge(payload, year)


async def _fetch_listing(session: aiohttp.ClientSession) -> str:
    """The listing every sheet is found from, fetched once in a while
    (``_LISTING_TTL_S``), and what fetching it raised kept as long."""
    global _listing
    async with _lock(_LISTING_URL):
        now = dt_util.utcnow()
        if (
            _listing is not None
            and (now - _listing[0]).total_seconds() < _LISTING_TTL_S
        ):
            held = _listing[1]
            if isinstance(held, Exception):
                raise held
            return held
        try:
            text = await fetch_text(session, _LISTING_URL, timeout=_TIMEOUT)
        except Exception as err:
            _listing = (now, err)
            raise
        _listing = (now, text)
        return text


async def _fetch_workbook(
    session: aiohttp.ClientSession, listing: str, publication_re: str
) -> bytes | None:
    """The publication page ``publication_re`` finds on ``listing``, then its
    workbook, or ``None`` where either link is missing."""
    publication = re.search(publication_re, listing)
    if publication is None:
        return None
    page = await fetch_text(session, _SITE + publication.group(1), timeout=_TIMEOUT)
    sheet = _SHEET_RE.search(page)
    if sheet is None:
        return None
    url = _SITE + sheet.group(1)
    async with session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
    ) as resp:
        guard_redirect(url, resp)
        resp.raise_for_status()
        return await read_capped(resp, url)


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
    if not _is_years_sheet(rows, year):
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


def parse_analog_surcharge(payload: bytes, year: int) -> float | None:
    """What an analog meter pays per kWh over a peak-metered one, EUR/kWh excl.
    VAT: the analog column's kWh rate less the peak-metered column's.

    ``None`` when the workbook is not the year's tariff list, when either kWh
    rate cannot be found by its column, when the public service or surcharge
    rates differ between the two columns (the difference would no longer be
    the kWh rates' alone), or when the figure lands outside its bounds. Raises
    on a payload that is not a workbook; the caller logs it.
    """
    rows = _rows(payload)
    if not _is_years_sheet(rows, year):
        return None
    capacity = _labelled_row(rows, _CAPACITY_LABEL)
    fixed = _labelled_row(rows, _ANALOG_LABEL)
    if capacity is None or fixed is None:
        return None
    digital = _number_column(capacity)
    analog = next(iter(_number_columns(fixed)), None)
    if digital is None or analog is None:
        return None
    for label in _SHARED_LABELS:
        row = _labelled_row(rows, label)
        if row is None or _value(row, digital) != _value(row, analog):
            return None
    kwh = [row for row in rows if _KWH_LABEL in row.values()]
    own = next((_value(row, analog) for row in kwh if analog in row), None)
    peak = next((_value(row, digital) for row in kwh if digital in row), None)
    if own is None or peak is None:
        return None
    # The sheet states its rates to the seventh decimal.
    surcharge = round(own - peak, 7)
    return surcharge if 0.0 < surcharge < _MAX_ANALOG_SURCHARGE else None


def _value(row: dict[str, str], column: str) -> float | None:
    """The number in ``row`` at ``column``, or ``None``."""
    try:
        return float(row[column])
    except (KeyError, ValueError):
        return None


def _is_years_sheet(rows: list[dict[str, str]], year: int) -> bool:
    """Whether ``rows`` are the tariff list of ``year``."""
    title = f"periodieke distributienettarieven {year}"
    return any(title in text for row in rows for text in row.values())


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
    numbers = _number_columns(row)
    return numbers[0] if len(numbers) == 1 else None


def _number_columns(row: dict[str, str]) -> list[str]:
    """The columns of the numbers in ``row``, left to right."""
    numbers = []
    for col, text in row.items():
        try:
            float(text)
        except ValueError:
            continue
        numbers.append(col)
    return sorted(numbers, key=lambda col: (len(col), col))
