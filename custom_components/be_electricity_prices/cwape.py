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

"""The CWaPE's approved Walloon distribution tariffs.

The CWaPE approves each Walloon operator's periodic tariffs and publishes them
year by year as a workbook and a PDF, beside one grid for the transport costs
every operator re-invoices. Read for a custom Walloon entry that takes its
network from its operator's grid instead of typing it (:func:`cached_network`).

Both are found through the documents service the CWaPE's tariff page is built
from (``/rest/documents``), which lists every document with its type, its
operator, the years it applies to and its file. An operator's grid is read off
its workbook, or off its PDF where the workbook is the old binary format no
reader here opens (REW's), and from its low-voltage page alone, a household's
connection being low voltage without a peak meter. The transport grid is read
off its workbook. Only the 2026 to 2029 tariff structure is read, the first
with the Impact bands; a year before it keeps the figures typed.

Every rate per kWh is the network use rate with the public service, surcharge
and regulatory balance rates added, which is how the cards print it, all
excluding VAT. With 6% VAT, each of the five operators' 2026 rates and the
transport rate are the CWaPE's own summary of that year's tariffs to the
rounding it prints at, and each fixed term to the cent.

A grid states the days it is valid. The one valid on a delivery month is
billed, else the latest that began before it, so from 1 January until the
year's grid is read the year before's stands in. Nothing read is kept over a
restart, so where a year's documents do not cover it the year before's are
read too.

Never raises, for the reason ``brugel.py`` gives: the fetch runs inside the
coordinator tick, and a failure leaves the entry on the figures it typed.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final
from urllib.parse import unquote

import aiohttp
from homeassistant.util import dt as dt_util

from .const import DSO_AIEG, DSO_AIESH, DSO_ORES, DSO_RESA, DSO_REW
from .providers._pdf import USER_AGENT, fetch_text, guard_redirect, read_capped
from .providers._pdf_render import extract_pdf_text_layout
from .synergrid import _shared_strings, sheet_paths, sheet_rows

_LOGGER = logging.getLogger(__name__)

_SITE: Final = "https://www.cwape.be"
# An operator's periodic tariffs for consumption, and the transport costs
# every operator re-invoices.
_GRID_TYPE: Final = "202"
_TRANSPORT_TYPE: Final = "210"
_LISTING_URL: Final = (
    f"{_SITE}/rest/documents?_format=json"
    f"&documents_types%5B%5D={_GRID_TYPE}&documents_types%5B%5D={_TRANSPORT_TYPE}"
)
# The CWaPE's id for each operator, and the name its files begin with, which
# is checked too: an id that came to mean another operator would otherwise
# bill its rates without a sign.
_OPERATORS: Final[Mapping[str, tuple[str, str]]] = {
    DSO_AIEG: ("213", "AIEG"),
    DSO_AIESH: ("214", "AIESH"),
    DSO_ORES: ("453", "ORES"),
    DSO_RESA: ("225", "RESA"),
    DSO_REW: ("224", "REW"),
}
FIRST_YEAR: Final = 2026
_TIMEOUT: Final = 30
_FAILURE_RETRY_S: Final = 6 * 3600
# RESA's workbook unpacks to 1,8 MB.
_MAX_UNPACKED_BYTES: Final = 8 * 1024 * 1024
# A rate per kWh is cents and a yearly term tens of euro: bounds sized on a
# column read as another, not on what the network ought to cost.
_MAX_KWH: Final = 0.5
_MAX_YEARLY: Final = 500.0

_PERIOD_RE: Final = re.compile(
    r"(\d{2})\.(\d{2})\.(\d{4})\s*(?:au|à)\s*(\d{2})\.(\d{2})\.(\d{4})"
)
_LOW_VOLTAGE_RE: Final = re.compile(r"Pr[ée]l[èe]vement basse tension", re.IGNORECASE)
_CODE_RE: Final = re.compile(r"E\d{3}")
_NUMBER_RE: Final = re.compile(r"\d+(?:[.,]\d+)?(?:[eE]-?\d+)?")
# What a grid prints for a rate set municipality by municipality rather than
# one figure: AIEG's local taxes. The CWaPE's summary bills it as nothing.
_VARIABLE: Final = "V"
# The rates per kWh every register pays on top of its own: public service,
# road fee, corporate tax, other local taxes, regulatory balances.
_PER_KWH_CODES: Final = ("E215", "E891", "E850", "E890", "E410")
# The transport grid's rates for every connection's kWh on top of the rate
# itself: renewables support, public domain surcharge, regulatory balances.
_TRANSPORT_CODES: Final = ("E976", "E930", "E650")
_TRANSPORT_RATE_CODE: Final = "E520"
_NETWORK_CODE: Final = "E210"


@dataclass(frozen=True, slots=True)
class GridTerms:
    """One operator's network for a household, as one approved grid gives it,
    excluding VAT, and the days the grid is valid.

    Every rate per kWh carries the charges every register pays. The fixed
    term is the standard configuration's, which the Impact tariff does not
    bill (``fees.py``); the prosumer rate is per kVA of inverter a year.
    """

    valid_from: date
    valid_to: date
    single: float
    peak: float
    offpeak: float
    exclusive_night: float
    pic: float
    medium: float
    eco: float
    fixed_per_year: float
    prosumer_per_kva_year: float


@dataclass(frozen=True, slots=True)
class TransportTerms:
    """The transport every operator re-invoices per kWh to a household,
    excluding VAT, and the days the grid is valid."""

    valid_from: date
    valid_to: date
    rate: float


@dataclass(frozen=True, slots=True)
class _Document:
    kind: str
    operator: str
    year: int
    url: str


@dataclass(frozen=True, slots=True)
class _Row:
    """A grid row: its label, its market code and the figures after it, one
    per column, as printed."""

    label: str
    code: str
    values: tuple[str, ...]


# Each document read, by its address, for the life of the process; one that
# did not read is asked again after the backoff. The listing is kept as long,
# answered or not: it is only asked while a year is not covered.
_grids: dict[str, dict[str, GridTerms]] = {}
_transport: dict[str, TransportTerms] = {}
_failed_at: dict[str, datetime] = {}
_listing: tuple[datetime, list[_Document]] | None = None
# One read at a time: entries tick together, and the answer is the same.
_locks: dict[str, asyncio.Lock] = {}


def _lock() -> asyncio.Lock:
    """The module's lock, created on first use on the event loop."""
    lock = _locks.get("cwape")
    if lock is None:
        lock = _locks["cwape"] = asyncio.Lock()
    return lock


def cached_network(
    day: date, dso: str
) -> tuple[GridTerms | None, TransportTerms | None]:
    """``dso``'s grid and the transport grid valid on ``day``, else the latest
    of each that began before it, never one that begins after, or ``None``
    for one not read yet. Synchronous: the snapshot resolver cannot await."""
    return _valid_on(_grids.get(dso, {}).values(), day), _valid_on(
        _transport.values(), day
    )


def _valid_on[T: (GridTerms, TransportTerms)](held: Iterable[T], day: date) -> T | None:
    started = [terms for terms in held if terms.valid_from <= day]
    valid = [terms for terms in started if day <= terms.valid_to]
    if not (valid or started):
        return None
    return max(valid or started, key=lambda terms: terms.valid_from)


def _covers[T: (GridTerms, TransportTerms)](held: Iterable[T], year: int) -> bool:
    """Whether every month of ``year`` has a grid valid on its first day."""
    periods = [(terms.valid_from, terms.valid_to) for terms in held]
    return all(
        any(start <= date(year, month, 1) <= end for start, end in periods)
        for month in range(1, 13)
    )


async def ensure_network(session: aiohttp.ClientSession, year: int, dso: str) -> None:
    """Read ``dso``'s grid and the transport grid for ``year`` unless held,
    and the year before's where those do not cover the year, never raising.

    Only for a custom Walloon entry that takes its network from its operator's
    grid. Nothing for another operator or a year before :data:`FIRST_YEAR`.
    """
    operator = _OPERATORS.get(dso)
    if operator is None or _covered(dso, year) == (True, True):
        return
    async with _lock():
        documents: list[_Document] | None = None
        for wanted in (year, year - 1):
            grid, transport = _covered(dso, year)
            if (grid and transport) or wanted < FIRST_YEAR:
                return
            if documents is None:
                documents = await _listed(session)
            for doc in _wanted(documents, wanted, operator, grid, transport):
                if not _held_or_backing_off(doc.url):
                    await _read(session, doc, dso)


def _covered(dso: str, year: int) -> tuple[bool, bool]:
    return _covers(_grids.get(dso, {}).values(), year), _covers(
        _transport.values(), year
    )


def _held_or_backing_off(url: str) -> bool:
    if url in _transport or any(url in held for held in _grids.values()):
        return True
    failed = _failed_at.get(url)
    if failed is None:
        return False
    if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
        return True
    del _failed_at[url]
    return False


def _wanted(
    documents: Sequence[_Document],
    year: int,
    operator: tuple[str, str],
    grid_held: bool,
    transport_held: bool,
) -> list[_Document]:
    """The documents to read for ``year``: the operator's grid off its
    workbook, else off its PDF, and the transport grid off its workbook, each
    unless what is held already covers the year asked."""
    grd, name = operator
    wanted: list[_Document] = []
    if not grid_held:
        grids = [
            doc
            for doc in documents
            if doc.kind == _GRID_TYPE
            and doc.operator == grd
            and doc.year == year
            and _names(doc.url, name)
        ]
        workbooks = [doc for doc in grids if doc.url.lower().endswith(".xlsx")]
        wanted += workbooks or [
            doc for doc in grids if doc.url.lower().endswith(".pdf")
        ]
    if not transport_held:
        wanted += [
            doc
            for doc in documents
            if doc.kind == _TRANSPORT_TYPE
            and doc.year == year
            and doc.url.lower().endswith(".xlsx")
        ]
    return wanted


def _names(url: str, name: str) -> bool:
    """Whether the file ``url`` points at is named after operator ``name``."""
    file = unquote(url.rsplit("/", 1)[-1])
    return re.match(rf"{re.escape(name)}(?![A-Za-z])", file, re.IGNORECASE) is not None


async def _listed(session: aiohttp.ClientSession) -> list[_Document]:
    """Every document the service lists, kept for the backoff, answered or
    not, so a CWaPE that does not answer costs one timeout per six hours."""
    global _listing
    now = dt_util.utcnow()
    if _listing is not None and (now - _listing[0]).total_seconds() < _FAILURE_RETRY_S:
        return _listing[1]
    try:
        text = await fetch_text(session, _LISTING_URL, timeout=_TIMEOUT)
        # Close to 2 MB of JSON, parsed off the event loop.
        documents = await asyncio.to_thread(parse_listing, text)
    except Exception as err:  # the docstring promises no raise
        _LOGGER.warning("CWaPE document listing could not be read: %s", err)
        documents = []
    _listing = (now, documents)
    return documents


def parse_listing(text: str) -> list[_Document]:
    """The listing's documents that apply to one year, each with its type,
    operator, year and address."""
    payload: Any = json.loads(text)
    documents: list[_Document] = []
    for item in payload if isinstance(payload, list) else ():
        if not isinstance(item, dict):
            continue
        kind, grd, years, path = (
            item.get(key)
            for key in (
                "field_document_type",
                "field_grd",
                "field_date_application",
                "field_file",
            )
        )
        if not all(isinstance(field, str) for field in (kind, grd, years, path)):
            continue
        span = re.fullmatch(r"(\d{4}) - (\d{4})", str(years).strip())
        if span is None or span[1] != span[2] or not str(path).startswith("/"):
            continue
        documents.append(
            _Document(str(kind), str(grd), int(span[1]), _SITE + str(path))
        )
    return documents


async def _read(session: aiohttp.ClientSession, doc: _Document, dso: str) -> None:
    """Read one document into the caches, or note it for the backoff."""
    terms: GridTerms | TransportTerms | None
    try:
        payload = await _download(session, doc.url)
        if doc.kind == _TRANSPORT_TYPE:
            terms = await asyncio.to_thread(parse_transport, payload)
        else:
            pdf = not doc.url.lower().endswith(".xlsx")
            terms = await asyncio.to_thread(parse_grid, payload, pdf=pdf)
    except Exception as err:  # the docstring promises no raise
        _LOGGER.warning("CWaPE %s could not be read: %s", doc.url, err)
        _failed_at[doc.url] = dt_util.utcnow()
        return
    if terms is None:
        _LOGGER.warning("CWaPE %s gives no household network rates", doc.url)
        _failed_at[doc.url] = dt_util.utcnow()
    elif isinstance(terms, TransportTerms):
        _transport[doc.url] = terms
    else:
        _grids.setdefault(dso, {})[doc.url] = terms


async def _download(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(
        url,
        headers={"User-Agent": USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
    ) as resp:
        guard_redirect(url, resp)
        resp.raise_for_status()
        return await read_capped(resp, url)


def parse_grid(payload: bytes, *, pdf: bool = False) -> GridTerms | None:
    """A household's network off an operator's grid, its workbook or with
    ``pdf`` its PDF, or ``None`` when the grid has no low-voltage page, a
    rate is missing or a figure is out of bounds. Raises on a payload that is
    neither; the caller logs it.

    Every rate is required: one the grid stops printing, or prints under
    another label, refuses it rather than billing a network short of a
    charge, and the entry keeps the figures it typed.
    """
    page = _pdf_page(payload) if pdf else _workbook_page(payload, _LOW_VOLTAGE_RE)
    if page is None:
        return None
    text, rows = page
    period = _period(text)
    if period is None:
        return None

    def standard(label: str) -> float | None:
        return _figure(_row(rows, label, _NETWORK_CODE), -1)

    def incitative(label: str) -> float | None:
        return _figure(_row(rows, label, _NETWORK_CODE), 0)

    rates = {
        "single": standard(r"heures normales"),
        "peak": standard(r"heures pleines"),
        "offpeak": standard(r"heures creuses"),
        "exclusive_night": incitative(r"exclusif de nuit"),
        "pic": incitative(r"\bPIC\b"),
        "medium": incitative(r"\bMEDIUM\b"),
        "eco": incitative(r"\bECO\b"),
    }
    fixed = _figure(_row(rows, r"terme fixe", "E270"), -1)
    prosumer = _figure(_row(rows, r"prosumer"), 0)
    charges = [_figure(_row(rows, "", code), 0) for code in _PER_KWH_CODES]
    if (
        fixed is None
        or prosumer is None
        or any(rate is None for rate in rates.values())
        or any(charge is None for charge in charges)
    ):
        return None
    per_kwh = sum(charge or 0.0 for charge in charges)
    billed = {key: (rate or 0.0) + per_kwh for key, rate in rates.items()}
    if not (
        all(0.0 < rate <= _MAX_KWH for rate in billed.values())
        and billed["offpeak"] <= billed["peak"]
        and billed["eco"] <= billed["medium"] <= billed["pic"]
        and all(0.0 <= (charge or 0.0) <= _MAX_KWH for charge in charges)
        and 0.0 <= fixed <= _MAX_YEARLY
        and 0.0 <= prosumer <= _MAX_YEARLY
    ):
        return None
    return GridTerms(
        valid_from=period[0],
        valid_to=period[1],
        fixed_per_year=fixed,
        prosumer_per_kva_year=prosumer,
        **billed,
    )


def parse_transport(payload: bytes) -> TransportTerms | None:
    """The transport a household pays per kWh off the transport grid's
    workbook, or ``None`` when a rate is missing or out of bounds.

    The rate is the "tous les raccordements" column, the last of its row,
    which every low-voltage connection without a peak meter is billed, with
    the three rates below it added, each the last of its row too.
    """
    page = _workbook_page(payload, None)
    if page is None:
        return None
    text, rows = page
    period = _period(text)
    if period is None:
        return None
    figures = [_figure(_row(rows, r"heures normales", _TRANSPORT_RATE_CODE), -1)]
    figures += [_figure(_row(rows, "", code), -1) for code in _TRANSPORT_CODES]
    if any(figure is None for figure in figures):
        return None
    rate = sum(figure or 0.0 for figure in figures)
    if not 0.0 < rate <= _MAX_KWH:
        return None
    return TransportTerms(valid_from=period[0], valid_to=period[1], rate=rate)


def _workbook_page(
    payload: bytes, title: re.Pattern[str] | None
) -> tuple[str, list[_Row]] | None:
    """The text and coded rows of the first sheet ``title`` is found on, or
    with no title of the first sheet holding coded rows."""
    with zipfile.ZipFile(io.BytesIO(payload)) as z:
        if sum(info.file_size for info in z.infolist()) > _MAX_UNPACKED_BYTES:
            raise ValueError("workbook unpacks past the size cap")
        strings = _shared_strings(z)
        sheets = [sheet_rows(z, path, strings) for _name, path in sheet_paths(z)]
    for cells in sheets:
        lines = [
            [row[column] for column in sorted(row, key=lambda c: (len(c), c))]
            for row in cells
        ]
        text = "\n".join(" ".join(line) for line in lines)
        rows = [coded for line in lines for coded in _coded(line)]
        if (title is None and rows) or (title is not None and title.search(text)):
            return text, rows
    return None


def _pdf_page(payload: bytes) -> tuple[str, list[_Row]] | None:
    """The text and coded rows of a grid's low-voltage page, off its PDF:
    from the page's title on. A row is looked up as the first that matches,
    so the page's own come before anything a later page prints."""
    lines = extract_pdf_text_layout(payload).splitlines()
    start = next(
        (i for i, line in enumerate(lines) if _LOW_VOLTAGE_RE.search(line)), None
    )
    if start is None:
        return None
    page = lines[start:]
    rows = [coded for line in page for coded in _coded(line.split())]
    return "\n".join(page), rows


def _coded(tokens: Sequence[str]) -> list[_Row]:
    """The row ``tokens`` print, split at its market code, or none without
    one."""
    for index, token in enumerate(tokens):
        if _CODE_RE.fullmatch(token):
            label = " ".join(tokens[:index])
            return [_Row(label, token, tuple(tokens[index + 1 :]))]
    return []


def _row(rows: Sequence[_Row], label: str, code: str | None = None) -> _Row | None:
    """The first row with ``code`` whose label ``label`` is found in."""
    pattern = re.compile(label, re.IGNORECASE)
    for row in rows:
        if (code is None or row.code == code) and pattern.search(row.label):
            return row
    return None


def _figure(row: _Row | None, index: int) -> float | None:
    """The figure in column ``index`` of ``row``: ``None`` for a dash, a
    missing row or a missing column, nothing for a rate marked variable."""
    if row is None or not row.values:
        return None
    try:
        value = row.values[index]
    except IndexError:
        return None
    if value == _VARIABLE:
        return 0.0
    if _NUMBER_RE.fullmatch(value) is None:
        return None
    return float(value.replace(",", "."))


def _period(text: str) -> tuple[date, date] | None:
    """The days a grid says it is valid, or ``None``."""
    found = _PERIOD_RE.search(text)
    if found is None:
        return None
    day, month, year, end_day, end_month, end_year = (
        int(part) for part in found.groups()
    )
    try:
        start, end = date(year, month, day), date(end_year, end_month, end_day)
    except ValueError:
        return None
    return (start, end) if start <= end else None
