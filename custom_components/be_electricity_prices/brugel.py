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

"""Brugel's published Brussels distribution tariffs.

Sibelga's regulated network charges are set by Brugel and published once a
year as one small PDF, "Grille tarifaire - Electricite / Distribution
Electricite Annee <year>", which states "prix hors TVA" in its header.

Only one figure is read from it, and only because no supplier card carries it
reliably: the "Puissance mise a disposition" annual term, which is half of
Sibelga's fixed charge. Engie, Mega, TotalEnergies and EnergyVision print the
sum of that term and the metering one; Bolt's card prints the metering term
alone under a heading calling it the whole "Terme fixe GRD", so a Brussels
Bolt entry was billed about 50 EUR a year short of what Sibelga charges it.

The rate cannot be substituted from another supplier's card without guessing
that the two are the same, and it may not be written into Python source:
``providers/base.py`` is explicit that every number in a ``SupplierSnapshot``
comes from a live fetch. The regulator publishes it, so the regulator is what
this asks. ``docs/providers/bolt.md`` named this as the fix that would be a
real one.

Until Brugel publishes a year's final sheet, the year is read off the 2025 to
2027 tariff grid it published with the tariff methodology, whose later pages it
calls indicative ("fournis ici a titre indicatif"). Such a figure is flagged
(``power_term_is_indicative``) so the Repairs card can say so, and the final
sheet replaces it as soon as it can be read.

Never raises, and that is load-bearing: the fetch runs inside the coordinator
tick, whose only handler is for ``UpdateFailed``, so anything escaping here
takes every entity on the device unavailable and turns a first refresh into
ConfigEntryNotReady. A download or parse failure logs and leaves the term
unknown, and the caller then bills exactly what it billed before.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Final

import aiohttp
from homeassistant.util import dt as dt_util

from .providers._pdf import extract_pdf_text

_LOGGER = logging.getLogger(__name__)

# The sheet for year N is published in N-1 and addressed by both years.
_URL = (
    "https://brugel.brussels/publication/document/notype/{published}"
    "/fr/Tarif-distribution-Elec-{year}.pdf"
)
# Brugel's multi-year grid: one page per year, 2025 to 2027, the later two
# indicative. Read only for a year whose final sheet is not served.
_GRID_URL: Final = (
    "https://brugel.brussels/publication/document/notype/2024"
    "/fr/Grille-tarifaire-2025-2027-Electricite.pdf"
)
_GRID_YEARS: Final = range(2025, 2028)
_TIMEOUT: Final = 30
# A regulated annual term is tens of euro. The bound is sized on the slip it
# catches, a factor of ten or a column read as a rate, not on what the charge
# ought to cost: 1 to 500 EUR/year leaves every plausible tariff alone and
# still refuses 0,1294270, the per-DAY figure printed on the row below.
_MIN_EUR: Final = 1.0
_MAX_EUR: Final = 500.0

# The two rows, in the "Sans mesure de pointe" block a residential connection
# is billed on. Each ends in the same value repeated once per BT column, so
# the LAST number on the line is the one either column charges.
_LE13_RE = re.compile(
    r"Puissance mise [àa] disposition inf[ée]rieure ou [ée]gale [àa] 13\s*kVA"
    r"[^\n]*?([\d]+,[\d]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_GT13_RE = re.compile(
    r"Puissance mise [àa] disposition sup[ée]rieure [àa] 13\s*kVA"
    r"[^\n]*?([\d]+,[\d]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# The grid's year headings, "Annee 2026", and on its later pages the value
# rows printed apart from their labels: three dashes for the MT columns, then
# the figure once per BT column.
_YEAR_RE = re.compile(r"Ann[ée]e\s+(\d{4})")
_VALUE_ROW_RE = re.compile(r"^-\s+-\s+-\s+(\d+,\d+)\s+(\d+,\d+)\s*$", re.MULTILINE)

# Successes are kept for the life of the process: the figure is annual and
# the sheet does not change under us. A failure is kept only briefly, so a
# blocked or slow Brugel does not cost a download every tick while a
# transient one still heals without a restart.
_FAILURE_RETRY_S: Final = 6 * 3600
_cache: dict[int, tuple[float, float]] = {}
_failed_at: dict[int, datetime] = {}
# Years whose cached term is the grid's indicative figure. The final sheet is
# still asked for on the failure schedule, and replaces it when it answers.
_indicative: set[int] = set()
# One fetch per year at a time. Entries tick together and a backfill asks for
# several years at once, so without this a cold start opens the same download
# once per Brussels entry: the answer is identical and idempotent, but it is
# paid out of the setup budget, which is the one place this integration cannot
# afford a duplicate.
_locks: dict[int, asyncio.Lock] = {}


def _lock(year: int) -> asyncio.Lock:
    """The lock for ``year``, created on first use.

    Safe to build lazily because every caller is on the event loop: there is
    no await between the lookup and the insert, so two callers cannot both
    miss.
    """
    lock = _locks.get(year)
    if lock is None:
        lock = _locks[year] = asyncio.Lock()
    return lock


def cached_power_term(year: int) -> tuple[float, float] | None:
    """The year's term if it has been fetched, ex-VAT EUR/year, else ``None``.

    Synchronous on purpose: the snapshot resolver runs in the pricing path
    and cannot await. :func:`ensure_power_term` is what puts a value here.
    """
    return _cache.get(year)


def power_term_is_indicative(year: int) -> bool:
    """Whether the year's cached term is Brugel's indicative figure."""
    return year in _indicative and year in _cache


def any_cached_power_term() -> tuple[float, float] | None:
    """The most recent term this process holds, for any year, or ``None``.

    Not for pricing, which must use the delivery year's own figure and nothing
    else. This is for asking whether a card's printed fixed charge looks like
    the metering half alone, a question whose answer needs SOME figure of the
    right order and does not change between adjacent years: the term is set per
    calendar year and moves by a few percent, where the gap between a metering
    figure and a complete one is fourfold.
    """
    if not _cache:
        return None
    return _cache[max(_cache)]


async def ensure_power_term(
    session: aiohttp.ClientSession, year: int
) -> tuple[float, float] | None:
    """Fetch the year's "Puissance mise a disposition" pair once and cache it.

    Returns ``(at_or_below_13kva, above_13kva)`` in EUR/year excluding VAT, or
    ``None`` when the sheet cannot be read, which leaves every caller billing
    what it billed before.
    """
    hit = _cache.get(year)
    if hit is not None and year not in _indicative:
        return hit
    async with _lock(year):
        # Re-read under the lock: the entry that waited here wants the answer
        # the first one fetched, not a second download of it.
        hit = _cache.get(year)
        if hit is not None and year not in _indicative:
            return hit
        return await _fetch_power_term(session, year)


async def _fetch_power_term(
    session: aiohttp.ClientSession, year: int
) -> tuple[float, float] | None:
    """One attempt at the year's sheet, under the caller's lock.

    The final sheet first. Without it, the indicative figure already held, or
    else the grid's page for the year.
    """
    failed = _failed_at.get(year)
    if failed is not None:
        if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
            return _cache.get(year)
        del _failed_at[year]

    try:
        text = await _sheet_text(session, year)
        pair = _parse(text) if text else None
    except Exception as err:  # noqa: BLE001 - the docstring promises no raise
        # The backoff is recorded below whatever happened, so a failure that
        # reaches here still costs one attempt rather than one per tick.
        _LOGGER.warning("Brugel %d tariff sheet could not be read: %s", year, err)
        pair = None
    if pair is None:
        _failed_at[year] = dt_util.utcnow()
        if year in _cache:
            return _cache[year]
        pair = await _indicative_term(session, year)
        if pair is None:
            return None
        _indicative.add(year)
    else:
        _indicative.discard(year)
    _cache[year] = pair
    _LOGGER.debug(
        "Brugel %d power term: %.2f / %.2f EUR/year excl. VAT%s",
        year,
        *pair,
        " (indicative)" if year in _indicative else "",
    )
    return pair


async def _indicative_term(
    session: aiohttp.ClientSession, year: int
) -> tuple[float, float] | None:
    """The year's term off the 2025 to 2027 grid, or ``None``."""
    if year not in _GRID_YEARS:
        return None
    try:
        text = await _pdf_text(session, _GRID_URL)
        return _parse_grid(text, year) if text else None
    except Exception as err:  # noqa: BLE001 - the docstring promises no raise
        _LOGGER.debug("Brugel tariff grid unreadable for %d: %s", year, err)
        return None


async def _sheet_text(session: aiohttp.ClientSession, year: int) -> str | None:
    """The year's tariff sheet as text, by its published address.

    One address; the only fallback is the multi-year grid, read by
    ``_indicative_term`` for its years. A search of Brugel's own theme page
    for the year's link was tried: the page is rendered client side and carries no
    PDF href at all, to curl or to a browser user agent, so the pattern
    could never match and every cold start paid 71 KB to find that out on a
    request the setup budget could least afford. A future address change
    wants a source that actually serves links, not this one.
    """
    text = await _pdf_text(session, _URL.format(published=year - 1, year=year))
    if text is None:
        _LOGGER.warning("Brugel %d distribution tariff sheet could not be read", year)
    return text


async def _pdf_text(session: aiohttp.ClientSession, url: str) -> str | None:
    """A Brugel PDF as text, or ``None`` when it is not served or not a PDF."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(_TIMEOUT)) as r:
            if r.status == 200:
                payload = await r.read()
                return await asyncio.to_thread(extract_pdf_text, payload)
            _LOGGER.debug("Brugel sheet %s answered %d", url, r.status)
    except Exception as err:  # noqa: BLE001 - see ensure_power_term
        # Broad on purpose. The reader raises ExtractorError for a body
        # that is not a PDF, which is what a maintenance page or a
        # captive portal answers with a 200, and ExtractorError is not a
        # ValueError: catching a tuple let it escape to the coordinator
        # tick, where the only handler is for UpdateFailed, so one bad
        # response took every entity on the device unavailable.
        _LOGGER.debug("Brugel sheet %s unreadable: %s", url, err)
    return None


def _parse_grid(text: str, year: int) -> tuple[float, float] | None:
    """The year's pair off the multi-year grid, or ``None``.

    The grid's first page prints its rows like the yearly sheet, label and
    figures on one line, and is read as one. The later pages print the labels
    and then the figures apart from them, so ``_parse`` on the whole file read
    the first page, 2025's figures, whatever year was asked. Within the year's
    page the first four value rows are the two terms, each followed by the
    same charge per day, and both have to agree before either is taken.
    """
    heads = list(_YEAR_RE.finditer(text))
    for index, head in enumerate(heads):
        if int(head.group(1)) != year:
            continue
        end = heads[index + 1].start() if index + 1 < len(heads) else len(text)
        page = text[head.end() : end]
        inline = _parse(page)
        if inline is not None:
            return inline
        rows = [
            (float(a.replace(",", ".")), float(b.replace(",", ".")))
            for a, b in _VALUE_ROW_RE.findall(page)[:4]
        ]
        if len(rows) < 4 or any(a != b for a, b in rows):
            return None
        (low, _), (low_day, _), (high, _), (high_day, _) = rows
        if abs(low_day * 365 - low) > 0.01 * low or abs(high_day * 365 - high) > (
            0.01 * high
        ):
            return None
        return _checked(low, high)
    return None


def _parse(text: str) -> tuple[float, float] | None:
    le13 = _LE13_RE.search(text)
    gt13 = _GT13_RE.search(text)
    if le13 is None or gt13 is None:
        return None
    try:
        low = float(le13.group(1).replace(",", "."))
        high = float(gt13.group(1).replace(",", "."))
    except ValueError:
        return None
    return _checked(low, high)


def _checked(low: float, high: float) -> tuple[float, float] | None:
    """The pair, when both figures are of the size of an annual term."""
    if not (_MIN_EUR <= low <= _MAX_EUR and _MIN_EUR <= high <= _MAX_EUR):
        return None
    if high < low:
        # The band above 13 kVA is never the cheaper of the two; a pair that
        # says otherwise is two columns read out of order.
        return None
    return low, high
