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

"""Ecopower (Flemish citizen cooperative) tariff extractor.

Ecopower sells two residential electricity products in Flanders only:

1. "Groene burgerstroom" (green citizen power): a half-fixed,
   half-indexed tariff against the monthly RLP-weighted Belpex
   Day-Ahead average:

       energy = 0.5 * 0.17 + 0.5 * Belpex_DA  (EUR/kWh, HTVA)

   A new card is published every month at a CDN URL that rotates each
   month (``cdn.nimbu.io/.../<YYYYMM>_gbs_tariefkaart.pdf``); the public
   price page at ``ecopower.be/groene-stroom/prijs-nieuw`` lists the
   most recent four or so months. We scrape that page to find the latest
   definitive card (Ecopower also publishes an "inschatting" / estimation
   card for the running month that we deliberately ignore). A definitive
   card is published in arrears, once its month is over, and prints that
   month's settled index, so the newest one stands in for the running
   month.

2. "Dynamische burgerstroom": a quarter-hourly EPEX Day-Ahead dynamic
   tariff (quarter-hourly since the SDAC 15-minute market switch of
   2025-10-01). The card prints the consumer formula directly:

       afname    = 1.02 * EPEX_DA + 4  EUR/MWh  (HTVA)
       injectie  = 0.98 * EPEX_DA - 15 EUR/MWh  (VAT-exempt)

   The dynamic card lives on the product page
   ``ecopower.be/groene-stroom/dynamische-burgerstroom`` as
   ``<YYYYMM>_dbs_tariefkaart.pdf``, or ``<YYYYMMDD>_...`` from the
   August 2026 card onwards. Unlike the monthly gbs card, the dynamic
   card is republished only when the formula, DSO or tax rates change,
   so the latest card is the one in effect today, which is why a
   pattern that cannot see the newer filename goes unnoticed: the older
   card it keeps resolving is a real card that still parses.

All amounts on both cards are HTVA. Residential customers pay 6% VAT;
the snapshot's ``TaxOverlay.vat_rate=0.06`` instructs ``compute_breakdown``
to scale up to TVAC, matching every other supplier's all-in number.
Injection is VAT-exempt for residential customers, so its formula is
stored unscaled.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date
from typing import Any

import aiohttp

from ..const import REGION_FLANDERS
from ._ecopower_cards import (
    _NL_MONTHS,
    _extract_dbs_energy,
    _extract_dbs_injection,
    _extract_energy,
    _extract_injection,
    printed_rlp_index,
    printed_spp_index,
)
from ._ecopower_overlays import (
    _extract_dbs_dsos,
    _extract_dsos,
    _extract_taxes,
)
from ._pdf import (
    extract_pdf_text_layout,
    fetch_pdf_text_layout,
    fetch_text,
    is_transient_fetch_error,
)
from ._rates import Contract, VariableRates
from ._settle import settled_energy, settled_injection
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://ecopower.be"
_PRICE_PAGE = f"{_BASE_URL}/groene-stroom/prijs-nieuw"

# Card filenames look like 202604_gbs_tariefkaart.pdf for a definitive
# April 2026 card, or 202605_gbs_inschatting_tariefkaart_ecopower.pdf
# for a next-month "inschatting" (estimation) that gets replaced by the
# definitive card on the 1st. Match only the definitive form.
_CARD_RE = re.compile(
    r'(https?://[^"]+/(?P<stamp>20\d{4}(?:\d{2})?)_gbs_tariefkaart\.pdf[^"]*)"',
    re.IGNORECASE,
)


def _card_stamp_keys(stamp: str) -> tuple[str, str]:
    """Return ``(sort_key, yyyymm)`` for a tariff-card filename stamp.

    Ecopower named every card ``YYYYMM_...`` until the August 2026
    dynamic card arrived as ``YYYYMMDD_...``. Both forms sit on the page
    at once, so they have to order against each other, and a six-digit
    pattern silently skips the eight-digit ones: that is how the January
    card kept billing after the August one shipped. Padding the
    month-only form to eight digits sorts it before a dated card in the
    same month, which is the right precedence: a card dated the 1st
    supersedes a bare month card for that month, while the month key
    stays the first six digits for either form.
    """
    return stamp.ljust(8, "0"), stamp[:6]


_CONTRACT_ID = "ecopower_burgerstroom"
_CONTRACT_LABEL = "Ecopower Groene Burgerstroom"

_DBS_CONTRACT_ID = "ecopower_dynamische_burgerstroom"
_DBS_CONTRACT_LABEL = "Ecopower Dynamische Burgerstroom"
_DBS_PAGE = f"{_BASE_URL}/groene-stroom/dynamische-burgerstroom"

# Dynamic card filenames look like 202601_dbs_tariefkaart.pdf, with older
# variants carrying a letter suffix (202501b_dbs_tariefkaart.pdf) or a
# trailing brand token (202406_dbs_tariefkaart_ecopower.pdf), and from the
# August 2026 card a full date (20260801_dbs_tariefkaart.pdf). The stamp
# group captures six or eight digits: pinned at six, this pattern could
# not see the dated card and kept resolving January's; the optional letter
# is consumed but not captured so ordering stays numeric.
_DBS_CARD_RE = re.compile(
    r'(https?://[^"]+/(?P<stamp>20\d{4}(?:\d{2})?)[a-z]?_dbs_tariefkaart[^"]*\.pdf[^"]*)"',
    re.IGNORECASE,
)

# Identifier discover() emits for the dynamic card. Ecopower keys its
# tariefkaart PDFs by filename family ("dbs"), not by contract id, so the
# catalog drift detector diffs on this family-based vocabulary.
_DBS_DISCOVER_ID = "ecopower_dbs"

# The ids discover() emits for the registered products. The live-check
# diffs the live catalogue against this baseline; deriving it here keeps
# the CI baseline from drifting away from the scraper.
DISCOVER_IDS: frozenset[str] = frozenset({_CONTRACT_ID, _DBS_DISCOVER_ID})


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if region != REGION_FLANDERS:
        raise ExtractorError("Ecopower only sells residential electricity in Flanders")
    if contract_id == _CONTRACT_ID:
        pdf_url, label = await _resolve_latest_pdf(session)
        text = await fetch_pdf_text_layout(session, pdf_url)
        return parse_snapshot(text, pdf_url, label)
    if contract_id == _DBS_CONTRACT_ID:
        pdf_url, label = await _resolve_latest_dbs_pdf(session)
        text = await fetch_pdf_text_layout(session, pdf_url)
        return parse_dbs_snapshot(text, pdf_url, label)
    raise ExtractorError(f"unknown Ecopower contract {contract_id!r}")


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # Ecopower is Flanders-only.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Ecopower card for a specific (year, month).

    The price page lists the last few months' definitive cards. Find
    the one whose YYYYMM filename prefix matches the requested month
    and parse it. Returns None when the listing doesn't carry the
    month (Ecopower only retains ~4 months back), the URL 404s, or the
    PDF doesn't parse.
    """
    if contract_id == _DBS_CONTRACT_ID:
        return await _fetch_dbs_for_month(session, year_month)
    if contract_id != _CONTRACT_ID:
        return None
    target = f"{year_month.year:04d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _PRICE_PAGE)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    # Highest stamp wins rather than first match: a month can carry both a
    # bare YYYYMM card and a dated YYYYMMDD reissue, and the reissue is the
    # one that billed.
    in_month = sorted(
        (sort_key, url)
        for sort_key, yyyymm, url in (
            (*_card_stamp_keys(m.group("stamp")), m.group(1))
            for m in _CARD_RE.finditer(html)
        )
        if yyyymm == target and "inschatting" not in url.lower()
    )
    if not in_month:
        return None
    pdf_url = in_month[-1][1]
    try:
        text = await fetch_pdf_text_layout(session, pdf_url)
        label = f"{target[:4]}-{target[4:]}"
        snap = parse_snapshot(text, pdf_url, label)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    # Cross-check the parsed card actually covers the requested month;
    # if the CDN ever serves the current card under a historical URL
    # the validity / title check rejects it instead of mis-billing past
    # consumption at current rates.
    checked = archive_validity_check(snap, text, year_month, month_names=_NL_MONTHS)
    return None if checked is None else _settled(checked, text, year_month)


def _settled(snap: SupplierSnapshot, text: str, year_month: date) -> SupplierSnapshot:
    """The month's card settled on the indices it prints for that month.

    A definitive card is published once its month has ended and prints the
    month's settled RLP-weighted mean in its energy formula and its settled
    SPP-weighted mean in its feed-in formula, so a closed month is billed on
    the figures Ecopower invoices rather than the engine's own means: the
    hourly SPP mean ran 0,04 to 0,06 c€/kWh above the card's credit over July
    to September 2026. Only here: the live fetch serves that same card for
    the running month, which settles on its own indices.

    A leg indexed on the month whose figure the card does not tie to the
    month is left as parsed and the card flagged ``provisional``, so the
    month is asked again rather than filed unsettled.
    """
    changed: dict[str, Any] = {}
    unsettled = False
    energy = snap.energy
    if (
        isinstance(energy, VariableRates)
        and energy.month_indexed
        and energy.formula_factor is not None
        and energy.formula_base is not None
    ):
        rlp = printed_rlp_index(text)
        if rlp is None or rlp[0] != year_month.month:
            unsettled = True
        else:
            changed["energy"] = settled_energy(energy, rlp[1])
    injection = snap.injection
    if injection is not None and injection.spp_indexed:
        spp = printed_spp_index(text)
        if spp is None or spp[0] != year_month.month:
            unsettled = True
        else:
            changed["injection"] = settled_injection(injection, spp[1])
    if unsettled:
        return replace(snap, provisional=True)
    return replace(snap, **changed) if changed else snap


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # Ecopower is Flanders-only, but signature is shared.
) -> str | None:
    """Cheap freshness probe: the card URL the fetcher would resolve.

    The pages' Last-Modified is the time of the request, not of a change, so a
    header key never matched and every tick downloaded and parsed the card.
    The URL is the signal instead: each upload gets its own file id and query
    token, and the stamp moves with each publication. ``None`` on a failed
    resolve, so the coordinator's TTL takes over.
    """
    resolve = {
        _CONTRACT_ID: _resolve_latest_pdf,
        _DBS_CONTRACT_ID: _resolve_latest_dbs_pdf,
    }.get(contract_id)
    if resolve is None:
        return None
    try:
        url, _label = await resolve(session)
    except ExtractorError:
        return None
    return url


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the family ids visible across Ecopower's price pages.

    Ecopower sells two residential products, each keyed by its
    tariefkaart filename family: the static "Groene burgerstroom" (gbs)
    on the price page and the dynamic "Dynamische burgerstroom" (dbs) on
    its own page. Both pages are scraped and matched together, so a new
    ``..._tariefkaart.pdf`` family on *either* page is surfaced verbatim
    as ``ecopower_<family>`` for the catalog drift detector. The gbs
    family is skipped here (the bare card is already registered and
    ``gbs_inschatting`` is the next-month preview the fetcher ignores) and
    dbs is matched by its dedicated regex.

    A page that fails to fetch is logged rather than swallowed: otherwise
    a partial failure would drop that page's family from a still-non-empty
    result and slip past live_check's empty-result warning.
    """
    bodies: list[str] = []
    for page in (_PRICE_PAGE, _DBS_PAGE):
        try:
            bodies.append(await fetch_text(session, page))
        except ExtractorError as err:
            _LOGGER.warning("Ecopower discover: %s unreachable: %s", page, err)
    combined = "\n".join(bodies)
    out: set[str] = set()
    if _CARD_RE.search(combined):
        out.add(_CONTRACT_ID)
    if _DBS_CARD_RE.search(combined):
        out.add(_DBS_DISCOVER_ID)
    # Six OR eight digits, matching the card patterns above: pinned at six,
    # a family published under the YYYYMMDD naming would be invisible to
    # catalog drift detection, which is the one thing meant to notice a new
    # Ecopower product.
    for other in re.findall(
        r'/(20\d{4}(?:\d{2})?[a-z]?_(?:[a-z_]+_)?tariefkaart[^"]*)\.pdf',
        combined,
        re.IGNORECASE,
    ):
        family = re.sub(r"^20\d{4}(?:\d{2})?[a-z]?_", "", other)
        family = re.sub(r"_tariefkaart.*$", "", family)
        if family and not family.startswith(("gbs", "dbs")):
            out.add(f"ecopower_{family}")
    return out


def parse_snapshot(
    text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    return SupplierSnapshot(
        supplier="ecopower",
        contract=_CONTRACT_ID,
        energy=_extract_energy(text),
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
    )


def parse_dbs_snapshot(
    text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser for the Dynamische burgerstroom card, exposed for tests.

    The tax block (GSC/WKK renewables, federal excise, energy
    contribution, energy fund, 6% VAT) is identical in layout to the
    gbs card, so ``_extract_taxes`` is reused as-is. Only the energy
    (dynamic formula) and DSO row layouts differ.
    """
    return SupplierSnapshot(
        supplier="ecopower",
        contract=_DBS_CONTRACT_ID,
        energy=_extract_dbs_energy(text),
        dsos=_extract_dbs_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_dbs_injection(text),
    )


# ---- catalog page scraping ---------------------------------------------------


async def _resolve_latest_pdf(
    session: aiohttp.ClientSession,
) -> tuple[str, str]:
    """Find the latest definitive tariff card PDF on the public price page.

    Ecopower's price page lists the definitive cards of the last few
    closed months and an "inschatting" (estimate) card for the running one,
    whose URL contains ``inschatting``. We strip those and pick the highest
    YYYYMM among the definitive cards. A definitive card is published only
    once its month has ended, so in October this is September's card: it
    stands in for the running month, whose month-indexed legs resolve
    against the running month's own means.
    """
    html = await fetch_text(session, _PRICE_PAGE)

    matches = [
        (sort_key, yyyymm, url)
        for sort_key, yyyymm, url in (
            (*_card_stamp_keys(m.group("stamp")), m.group(1))
            for m in _CARD_RE.finditer(html)
        )
        if "inschatting" not in url.lower()
    ]
    if not matches:
        raise ExtractorError(f"no Ecopower tariefkaart link found on {_PRICE_PAGE}")
    matches.sort()
    _sort_key, yyyymm, url = matches[-1]
    label = f"{yyyymm[:4]}-{yyyymm[4:]}"
    return url, label


async def _resolve_latest_dbs_pdf(
    session: aiohttp.ClientSession,
) -> tuple[str, str]:
    """Find the latest Dynamische burgerstroom card on the product page.

    The page lists the current dynamic card plus a few historical ones.
    The dynamic formula is stable across months, so the highest YYYYMM
    is the card billing today.
    """
    html = await fetch_text(session, _DBS_PAGE)
    matches = sorted(
        (*_card_stamp_keys(m.group("stamp")), m.group(1))
        for m in _DBS_CARD_RE.finditer(html)
    )
    if not matches:
        raise ExtractorError(f"no Ecopower dbs tariefkaart link found on {_DBS_PAGE}")
    _sort_key, yyyymm, url = matches[-1]
    return url, f"{yyyymm[:4]}-{yyyymm[4:]}"


async def _fetch_dbs_for_month(
    session: aiohttp.ClientSession, year_month: date
) -> SupplierSnapshot | None:
    """Return the dynamic card in effect for ``year_month``.

    Dynamic cards don't rotate monthly; Ecopower republishes one only
    when the formula, DSO or tax rates change (typically at a year
    boundary). Pick the most recent card whose YYYYMM prefix is not after
    the requested month: that's the card that was billing then. Falls
    back to None (coordinator uses the proxy snapshot) when the page omits
    the month or the PDF doesn't parse.
    """
    target = f"{year_month.year:04d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _DBS_PAGE)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    eligible = sorted(
        (sort_key, yyyymm, url)
        for sort_key, yyyymm, url in (
            (*_card_stamp_keys(m.group("stamp")), m.group(1))
            for m in _DBS_CARD_RE.finditer(html)
        )
        if yyyymm <= target
    )
    if not eligible:
        return None
    _sort_key, yyyymm, url = eligible[-1]
    try:
        text = await fetch_pdf_text_layout(session, url)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    return parse_dbs_snapshot(text, url, f"{yyyymm[:4]}-{yyyymm[4:]}")


# Re-export the layout extractor for fixture-based tests so they can
# parse a local PDF without going through the network path.
__all__ = [
    "EXTRACTOR",
    "extract_pdf_text_layout",
    "fetch",
    "parse_dbs_snapshot",
    "parse_snapshot",
]


_ECOPOWER_REGIONS = frozenset({REGION_FLANDERS})

EXTRACTOR = SupplierExtractor(
    sweep_cost_s=8.6,
    id="ecopower",
    label="Ecopower",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="variable",
            regions=_ECOPOWER_REGIONS,
            # Half the feed-in credit indexes on the delivery month's
            # SPP-weighted EPEX DA mean, which the variable energy leg fetches
            # no spots for.
            spot_indexed_injection=True,
            # Half the energy price is the delivery month's RLP-weighted EPEX
            # mean, and the card served while a month runs prints last
            # month's.
            month_indexed_energy=True,
        ),
        Contract(
            id=_DBS_CONTRACT_ID,
            label=_DBS_CONTRACT_LABEL,
            kind="dynamic",
            regions=_ECOPOWER_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
    # The live capture of a closed month carries the estimate the running
    # month needs; fetch_for_month settles it on the card's own index.
    settles_on_next_card=True,
)
