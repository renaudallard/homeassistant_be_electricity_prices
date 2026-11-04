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

"""A card that left its usual address, read where its supplier lists it now.

In November 2026 Elegant renamed its files and Engie gave Empty House a new
document code. Both new addresses stood on the supplier's own pages, but
the entries on those cards waited for a release to read them.

``with_moved_fallbacks`` wraps the registered ``fetch`` of the suppliers
below. When the usual address has no card, or it serves a card of a month
before the running one, it asks the supplier's pages for the product's card
of the running month and reads each address with the supplier's own reader,
keeping the first card that names that month. Only an address that is the
product's card by construction is read: the same product under its own
month's name, code or id. A sibling product is no such card, nor is the
month before's product id: Luminus's archive lists each month's id, and in
November 2026 BasicFlex's October id served a card titled November that
carried October's prices (#147). When nothing qualifies the usual answer
stands, the error or the older card.

EnergyVision is not among them. Its ``fetch`` already asks every folder its
listing's script asks, newest upload first, and its card page is no better
guide: in November 2026 it linked the first edition of a card a corrected
upload had replaced.

The wrapped ``fetch`` keeps the name and module of the supplier's own,
which ``inspect.unwrap`` gives back. The live check asks each module's own
``fetch``, so it keeps failing at the usual address until the extractor
reads the card there, and ``scripts/card_finder.py`` reports where the card
went, from the same candidates.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import date, timedelta
from html import unescape
from urllib.parse import quote

import aiohttp
from homeassistant.util import dt as dt_util

from . import elegant, engie, luminus
from ._pdf import fetch_text, is_missing_card_error
from ._validity import label_month_end
from .base import (
    ExtractorError,
    SnapshotFetcher,
    SupplierExtractor,
    SupplierSnapshot,
)

_LOGGER = logging.getLogger(__name__)

# An address, where it came from, and whether it is the product's card for the
# month by construction.
Candidate = tuple[str, str, bool]
Candidates = Callable[
    [aiohttp.ClientSession, str, str, date], Awaitable[list[Candidate]]
]
ReadAt = Callable[[aiohttp.ClientSession, str, str, str], Awaitable[SupplierSnapshot]]

# The addresses read for one card, best first.
_MAX_TRIED = 4

# Elegant's listing spells each card "/Current/<product> Electricity_
# Residential.pdf", with its gas cards beside them.
_ELEGANT_LISTED_RE = re.compile(r"/Current/([^\"'<>/]+?)_Residential\.pdf")

# Engie's residential prices pages, which name every document code the
# document API serves, "E_EMPTYHOUSE_R_GREY_C_I_36_V_F". The sitemap names
# product families only.
_ENGIE_PAGES = (
    "https://www.engie.be/fr/energie/electricite-gaz/prix-conditions/",
    "https://www.engie.be/nl/energie/elektriciteit-gas/prijzen-voorwaarden/",
)
_ENGIE_CODE_RE = re.compile(
    r"\bE_([A-Z0-9]+)_([RP])_([A-Z]+)_C_([A-Z])_(\d{2})_([VWB])_F\b"
)


def _squash(name: str) -> str:
    """A product name with its spaces, signs and case taken out."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


async def _page(session: aiohttp.ClientSession, url: str) -> str:
    """A page's text, or nothing when it cannot be read: the search goes on
    with the supplier's other pages."""
    try:
        return unescape(await fetch_text(session, url))
    except ExtractorError:
        return ""


async def _elegant(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    contract = elegant._CONTRACTS_BY_ID[contract_id]
    page = (await fetch_text(session, elegant._LISTING_URL)).replace("\\u0026", "&")
    # The key is the listed name with its spaces and "&" taken out.
    wanted = _squash(contract.key)
    return [
        (
            f"{elegant._CURRENT_URL}/{quote(name)}_Residential.pdf",
            "Elegant's listing",
            True,
        )
        for name in dict.fromkeys(_ELEGANT_LISTED_RE.findall(unescape(page)))
        if _squash(name) == wanted
    ]


async def _engie(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    contract = engie._CONTRACTS_BY_ID[contract_id]
    area = engie._REGION_TO_CODE[region]
    found: dict[str, bool] = {}
    for url in _ENGIE_PAGES:
        for m in _ENGIE_CODE_RE.finditer(await _page(session, url)):
            name, segment, color, rate, _months, where = m.groups()
            if (name, segment, where) != (contract.family, contract.segment, area):
                continue
            # The same colour and rate is the same product under a new term;
            # another is a sibling product.
            found[m.group(0)] = (color, rate) == (contract.color, contract.rate)
    return sorted(
        (
            (
                f"{engie._API_URL}?document={code}&monthOffset=0"
                f"&segment={contract.segment}&language=F",
                "Engie's prices page",
                same,
            )
            for code, same in found.items()
        ),
        key=lambda candidate: not candidate[2],
    )


async def _luminus(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> list[Candidate]:
    contract = luminus._CONTRACTS_BY_ID[contract_id]
    previous = (month - timedelta(days=1)).replace(day=1)
    found: dict[str, tuple[str, bool]] = {}
    for signing in (month, previous):
        try:
            product_id = await luminus._resolve_archive_product_id(
                session, contract, region, signing
            )
        except ExtractorError:
            continue
        if product_id is not None:
            url = luminus._archive_pdf_url(product_id, month, region)
            # The month before's id served an early copy titled November
            # that carried October's prices (#147).
            found.setdefault(
                url, (f"the product id of {signing:%Y-%m}", signing == month)
            )
    return [(url, where, exact) for url, (where, exact) in found.items()]


# The suppliers covered, with the reader that reads a card at a given address
# as their ``fetch`` reads it and the candidates their pages give.
FALLBACKS: dict[str, tuple[ReadAt, Candidates]] = {
    "elegant": (elegant._read_at, _elegant),
    "engie": (engie._read_at, _engie),
    "luminus": (luminus._read_at, _luminus),
}


def card_month(snap: SupplierSnapshot) -> date | None:
    """The first of the month a card names, by its title or else the last
    day it states, or None when it names neither."""
    end = label_month_end(snap.publication_label) or snap.valid_until
    return None if end is None else end.replace(day=1)


async def read_moved(
    session: aiohttp.ClientSession,
    supplier: str,
    contract_id: str,
    region: str,
    month: date,
) -> SupplierSnapshot | None:
    """The product's card for ``month`` where its supplier's pages put it,
    or None when no address the pages give is that card."""
    read_at, candidates = FALLBACKS[supplier]
    try:
        found = await candidates(session, contract_id, region, month)
    except ExtractorError:
        return None
    for url, where, _exact in [c for c in found if c[2]][:_MAX_TRIED]:
        try:
            snap = await read_at(session, contract_id, region, url)
        except ExtractorError:
            continue
        if card_month(snap) == month:
            _LOGGER.warning(
                "%s/%s/%s: the card is not at its usual address; read it at %s "
                "(found on %s) until a release reads it there",
                supplier,
                contract_id,
                region,
                url,
                where,
            )
            return snap
    return None


def _with_moved_fallback(supplier: str, fetch: SnapshotFetcher) -> SnapshotFetcher:
    # Under the supplier's own name and module, which tell the suppliers
    # apart, and with ``__wrapped__``, which gives its own fetch back.
    @functools.wraps(fetch)
    async def fetch_or_moved(
        session: aiohttp.ClientSession, contract_id: str, region: str
    ) -> SupplierSnapshot:
        month = dt_util.now().date().replace(day=1)
        try:
            snap = await fetch(session, contract_id, region)
        except ExtractorError as err:
            if not is_missing_card_error(str(err)):
                raise
            moved = await read_moved(session, supplier, contract_id, region, month)
            if moved is None:
                raise
            return moved
        named = card_month(snap)
        if named is None or named >= month:
            return snap
        return await read_moved(session, supplier, contract_id, region, month) or snap

    return fetch_or_moved


def with_moved_fallbacks(
    extractors: Mapping[str, SupplierExtractor],
) -> dict[str, SupplierExtractor]:
    """The registry with the covered suppliers' ``fetch`` wrapped."""
    return {
        key: (
            replace(extractor, fetch=_with_moved_fallback(key, extractor.fetch))
            if key in FALLBACKS
            else extractor
        )
        for key, extractor in extractors.items()
    }
