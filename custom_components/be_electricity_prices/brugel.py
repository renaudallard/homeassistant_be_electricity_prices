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
# Where the year's sheet is linked when the direct address stops working. The
# numeric suffix belongs to Brugel's CMS and will change with the regulatory
# period, so the theme page is the stable half and the link is found by name.
_INDEX_URL = "https://brugel.brussels/themes/tarifs-de-distribution-12"
_LINK_RE = re.compile(
    r'href="([^"]*Tarif-distribution-Elec-(\d{4})\.pdf)"', re.IGNORECASE
)

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

# Successes are kept for the life of the process: the figure is annual and
# the sheet does not change under us. A failure is kept only briefly, so a
# blocked or slow Brugel does not cost a download every tick while a
# transient one still heals without a restart.
_FAILURE_RETRY_S: Final = 6 * 3600
_cache: dict[int, tuple[float, float]] = {}
_failed_at: dict[int, datetime] = {}


def cached_power_term(year: int) -> tuple[float, float] | None:
    """The year's term if it has been fetched, ex-VAT EUR/year, else ``None``.

    Synchronous on purpose: the snapshot resolver runs in the pricing path
    and cannot await. :func:`ensure_power_term` is what puts a value here.
    """
    return _cache.get(year)


async def ensure_power_term(
    session: aiohttp.ClientSession, year: int
) -> tuple[float, float] | None:
    """Fetch the year's "Puissance mise a disposition" pair once and cache it.

    Returns ``(at_or_below_13kva, above_13kva)`` in EUR/year excluding VAT, or
    ``None`` when the sheet cannot be read, which leaves every caller billing
    what it billed before.
    """
    hit = _cache.get(year)
    if hit is not None:
        return hit
    failed = _failed_at.get(year)
    if failed is not None:
        if (dt_util.utcnow() - failed).total_seconds() < _FAILURE_RETRY_S:
            return None
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
        return None
    _cache[year] = pair
    _LOGGER.debug("Brugel %d power term: %.2f / %.2f EUR/year excl. VAT", year, *pair)
    return pair


async def _sheet_text(session: aiohttp.ClientSession, year: int) -> str | None:
    """The year's tariff sheet as text, by direct address then by name."""
    for url in await _candidate_urls(session, year):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(_TIMEOUT)) as r:
                if r.status != 200:
                    continue
                payload = await r.read()
            return await asyncio.to_thread(extract_pdf_text, payload)
        except Exception as err:  # noqa: BLE001 - see ensure_power_term
            # Broad on purpose. The reader raises ExtractorError for a body
            # that is not a PDF, which is what a maintenance page or a
            # captive portal answers with a 200, and ExtractorError is not a
            # ValueError: catching a tuple let it escape to the coordinator
            # tick, where the only handler is for UpdateFailed, so one bad
            # response took every entity on the device unavailable.
            _LOGGER.debug("Brugel sheet %s unreadable: %s", url, err)
    _LOGGER.warning("Brugel %d distribution tariff sheet could not be read", year)
    return None


async def _candidate_urls(session: aiohttp.ClientSession, year: int) -> list[str]:
    direct = _URL.format(published=year - 1, year=year)
    try:
        async with session.get(
            _INDEX_URL, timeout=aiohttp.ClientTimeout(_TIMEOUT)
        ) as r:
            html = await r.text() if r.status == 200 else ""
    except (aiohttp.ClientError, TimeoutError, OSError, UnicodeDecodeError):
        html = ""
    named = [
        href if href.startswith("http") else f"https://brugel.brussels{href}"
        for href, found in _LINK_RE.findall(html)
        if int(found) == year
    ]
    return [direct, *[u for u in named if u != direct]]


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
    if not (_MIN_EUR <= low <= _MAX_EUR and _MIN_EUR <= high <= _MAX_EUR):
        return None
    if high < low:
        # The band above 13 kVA is never the cheaper of the two; a pair that
        # says otherwise is two columns read out of order.
        return None
    return low, high
