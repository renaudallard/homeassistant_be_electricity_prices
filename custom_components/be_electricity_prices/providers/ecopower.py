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

Ecopower sells one residential electricity product, "Groene burgerstroom"
(green citizen power), in Flanders only. The energy formula is half-fixed,
half-indexed against the monthly RLP-weighted Belpex Day-Ahead average:

    energy = 0.5 * 0.17 + 0.5 * Belpex_DA  (EUR/kWh, HTVA)

A new tariff card is published every month. The card lives at a CDN URL
that rotates each month (``cdn.nimbu.io/.../<YYYYMM>_gbs_tariefkaart.pdf``);
the public price page at ``ecopower.be/groene-stroom/prijs-nieuw`` lists
the most recent four or so months. We scrape that page to find the
latest definitive card (Ecopower also publishes a *next-month*
"inschatting" / estimation card that we deliberately ignore until it's
finalized).

All amounts on the card are HTVA. Residential customers pay 6% VAT;
the snapshot's ``TaxOverlay.vat_rate=0.06`` instructs ``compute_breakdown``
to scale up to TVAC, matching every other supplier's all-in number.
"""

from __future__ import annotations

import re
from datetime import date

import aiohttp

from ..const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    REGION_FLANDERS,
)
from ._pdf import (
    extract_pdf_text_layout,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    parse_valid_until,
    to_float,
)
from .base import (
    Contract,
    DsoOverlay,
    EnergyRates,
    ExtractorError,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_BASE_URL = "https://ecopower.be"
_PRICE_PAGE = f"{_BASE_URL}/groene-stroom/prijs-nieuw"

# Card filenames look like 202604_gbs_tariefkaart.pdf for a definitive
# April 2026 card, or 202605_gbs_inschatting_tariefkaart_ecopower.pdf
# for a next-month "inschatting" (estimation) that gets replaced by the
# definitive card on the 1st. Match only the definitive form.
_CARD_RE = re.compile(
    r'(https?://[^"]+/(?P<yyyymm>20\d{4})_gbs_tariefkaart\.pdf[^"]*)"',
    re.IGNORECASE,
)

_DSO_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}


_CONTRACT_ID = "ecopower_burgerstroom"
_CONTRACT_LABEL = "Ecopower Groene Burgerstroom"


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown Ecopower contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("Ecopower only sells residential electricity in Flanders")
    pdf_url, label = await _resolve_latest_pdf(session)
    text = await fetch_pdf_text_layout(session, pdf_url)
    return parse_snapshot(text, pdf_url, label)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Ecopower is Flanders-only.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Ecopower card for a specific (year, month).

    The price page lists the last few months' definitive cards. Find
    the one whose YYYYMM filename prefix matches the requested month
    and parse it. Returns None when the listing doesn't carry the
    month (Ecopower only retains ~4 months back), the URL 404s, or the
    PDF doesn't parse.
    """
    if contract_id != _CONTRACT_ID:
        return None
    target = f"{year_month.year:04d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _PRICE_PAGE)
    except ExtractorError:
        return None
    pdf_url: str | None = None
    for match in _CARD_RE.finditer(html):
        if (
            match.group("yyyymm") == target
            and "inschatting" not in match.group(1).lower()
        ):
            pdf_url = match.group(1)
            break
    if pdf_url is None:
        return None
    try:
        text = await fetch_pdf_text_layout(session, pdf_url)
        label = f"{target[:4]}-{target[4:]}"
        return parse_snapshot(text, pdf_url, label)
    except ExtractorError:
        return None


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Ecopower is Flanders-only, but signature is shared.
) -> str | None:
    """Cheap freshness probe: HEAD the price page, return its Last-Modified.

    The page returns a stable Last-Modified header (server-side cache key),
    so a HEAD round-trip is enough to detect a publication. Falls back to
    None on transport / missing-header so the coordinator's TTL takes over.
    """
    if contract_id != _CONTRACT_ID:
        return None
    return await head_freshness_key(session, _PRICE_PAGE)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the set of contract ids visible at the public price page.

    Ecopower sells exactly one residential product. If they ever add a
    second card family ("groene_zakelijk_stroom_tariefkaart" or any
    ``..._tariefkaart.pdf`` other than ``gbs_``), live_check surfaces
    it via the unrecognised filename returned verbatim.
    """
    try:
        html = await fetch_text(session, _PRICE_PAGE)
    except ExtractorError:
        return set()
    out: set[str] = set()
    if _CARD_RE.search(html):
        out.add(_CONTRACT_ID)
    # Surface any *other* tariefkaart-style filename so a future product
    # (zakelijk, etc.) is caught by the catalog drift detector. Skip
    # every variant in the `gbs` family - the bare definitive card is
    # already registered and `gbs_inschatting` is the next-month preview
    # the fetcher deliberately ignores.
    for other in re.findall(
        r'/(20\d{4}_(?:[a-z_]+_)?tariefkaart[^"]*)\.pdf', html, re.IGNORECASE
    ):
        family = re.sub(r"^20\d{4}_", "", other)
        family = re.sub(r"_tariefkaart.*$", "", family)
        if family and not family.startswith("gbs"):
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


# ---- energy ------------------------------------------------------------------


_ENERGY_RE = re.compile(r"Groene burgerstroom[^\n]*?([\d,]+)\s*euro/kWh", re.IGNORECASE)


def _extract_energy(text: str) -> EnergyRates:
    """Parse the "Groene burgerstroom" effective rate (HTVA, EUR/kWh).

    The card prints the formula breakdown
    ``(50% vast aan 0,17 euro + 50% variabel aan 0,08472117 euro)``
    followed by the resolved ``0,1274 euro/kWh`` figure. We use the
    resolved number because (a) we don't have a Belpex feed at parse
    time, and (b) supporting Ecopower's variable cost without a live
    spot is exactly what ``VariableRates`` is for.
    """
    match = _ENERGY_RE.search(text)
    if not match:
        raise ExtractorError("could not parse Ecopower 'Groene burgerstroom' rate")
    return VariableRates(current=to_float(match.group(1)))


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the DIGITAL METER block.

    Ecopower's card lists two networks per Fluvius sub-area: digital
    meter rates (capacity tariff per kW/yr, lower per-kWh distribution)
    and analog meter rates (yearly fixed fee, higher distribution,
    spinning-back prosumer fee). The integration only models the
    digital path -- which is what the vast majority of Flemish
    residential is on post-2024-mandatory-rollout. Analog-meter users
    can still see realistic prices because Ecopower bills them at the
    SAME ENERGY rate, only the network costs differ.
    """
    section = _slice_between(text, "DIGITALE METER", "ANALOGE METER")
    if section is None:
        raise ExtractorError("could not locate Ecopower DIGITALE METER block")
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_LABELS.items():
        # Row layout in the digital block:
        #   <label> | databeheer EUR/yr | capacity EUR/kW/yr | -
        #           | enkelvoudig EUR/kWh | uitsluitend_nacht EUR/kWh | -
        #
        # An optional 7th column ("Maximumtarief") slides in between
        # uitsluitend_nacht and the trailing dash on rows where
        # Fluvius publishes a maximum (Imewo's Apr 2026 card has one).
        row = re.search(
            rf"^{re.escape(label)}\s+([\d,]+)\s+([\d,]+)\s+-\s+([\d,]+)\s+([\d,]+)"
            rf"(?:\s+[\d,]+)?\s+-",
            section,
            re.MULTILINE,
        )
        if not row:
            continue
        databeheer = to_float(row.group(1))
        capacity = to_float(row.group(2))
        single = to_float(row.group(3))
        # Group 4 is the exclusive-night meter rate (separate circuit
        # for an electric water heater / night-storage heater). It
        # used to be dropped because there was no DsoOverlay column
        # for it; now propagated for users on the exclusive_night
        # meter type. Same scaling as ``single``.
        excl_night = to_float(row.group(4))
        out[key] = DsoOverlay(
            distribution_single=single,
            distribution_exclusive_night=excl_night,
            transport=0.0,  # rolled into distribution on Ecopower's card
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
        )
    return out


def _slice_between(text: str, start: str, end: str) -> str | None:
    s = text.find(start)
    if s < 0:
        return None
    e = text.find(end, s + len(start))
    return text[s + len(start) : e] if e >= 0 else text[s + len(start) :]


# ---- taxes -------------------------------------------------------------------


_FEDERAL_EXCISE_RE = re.compile(
    r"Bijzondere accijns[^\n]*tussen 0\s+en\s+3\.000[^\n]*?([\d,]+)\s*euro/kWh"
)
_ENERGY_CONTRIB_RE = re.compile(r"Bijdrage op de energie\s+([\d,]+)\s*euro/kWh")
_GSC_RE = re.compile(r"Kost GSC\s+([\d,]+)\s*euro/kWh")
_WKK_RE = re.compile(r"Kost WKK\s+([\d,]+)\s*euro/kWh")
_FUND_RE = re.compile(r"Bijdrage Energiefonds\s+([\d,]+)\s*euro/maand", re.IGNORECASE)


def _extract_taxes(text: str) -> TaxOverlay:
    """Parse the federal/regional tax block.

    Ecopower prints all values HTVA. ``vat_rate=0.06`` tells the
    pricing engine to scale up to TVAC for residential customers --
    every other supplier publishes TVAC and uses ``vat_rate=0.0``, but
    Ecopower is the cooperative outlier.

    Flanders renewables: GSC + WKK certificate costs are the regional
    renewable surcharge in disguise. They're listed in the energy
    block but are passed straight through to the user (per-kWh), so
    they belong in ``flanders_renewables`` rather than baking them
    into ``energy.current`` (which would mean their value silently
    moved when Fluvius changes the certificate quota).
    """
    federal_match = _FEDERAL_EXCISE_RE.search(text)
    contrib_match = _ENERGY_CONTRIB_RE.search(text)
    gsc_match = _GSC_RE.search(text)
    wkk_match = _WKK_RE.search(text)
    fund_match = _FUND_RE.search(text)
    if not federal_match or not contrib_match:
        raise ExtractorError("could not parse Ecopower federal tax block")
    return TaxOverlay(
        federal_excise=to_float(federal_match.group(1)),
        energy_contribution=to_float(contrib_match.group(1)),
        flanders_renewables=(
            (to_float(gsc_match.group(1)) if gsc_match else 0.0)
            + (to_float(wkk_match.group(1)) if wkk_match else 0.0)
        ),
        energy_fund_eur_per_month=(
            to_float(fund_match.group(1)) if fund_match else 0.0
        ),
        vat_rate=0.06,
    )


# ---- injection ---------------------------------------------------------------


_INJECTION_RE = re.compile(
    # Accept ASCII hyphen plus en-dash, em-dash, and U+2212 minus so a
    # PDF re-render that swaps the glyph doesn't silently flip the sign.
    r"Terugleververgoeding[^\n]*digitale meter[^\n]*?"
    r"([\-–—−]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


def _extract_injection(text: str) -> InjectionRates | None:
    """Parse the digital-meter injection price.

    Ecopower currently CHARGES residential prosumers for grid use --
    the "terugleververgoeding" prints as a negative EUR/kWh value
    (``-0,02 euro/kWh`` for April 2026). The integration's
    InjectionRates accepts negative ``current`` natively, so the
    `injection_price` sensor will display a negative number for
    Ecopower customers (correct: you pay to inject).
    """
    match = _INJECTION_RE.search(text)
    if not match:
        return None
    raw = match.group(1).replace(" ", "")
    # Normalise non-ASCII minus glyphs to '-' so to_float can parse them.
    for variant in ("–", "—", "−"):
        if raw.startswith(variant):
            raw = "-" + raw[len(variant) :]
            break
    return InjectionRates(current=to_float(raw))


# ---- catalog page scraping ---------------------------------------------------


async def _resolve_latest_pdf(
    session: aiohttp.ClientSession,
) -> tuple[str, str]:
    """Find the latest definitive tariff card PDF on the public price page.

    Ecopower's price page lists the current month plus a few historical
    months, and (around end-of-month) a *next-month* "inschatting" card
    whose URL contains ``inschatting``. We strip those and pick the
    highest YYYYMM among the definitive cards; that's the card whose
    rates are actually being billed today.
    """
    html = await fetch_text(session, _PRICE_PAGE)

    matches = [
        (yyyymm, url)
        for url, yyyymm in (
            (m.group(1), m.group("yyyymm")) for m in _CARD_RE.finditer(html)
        )
        if "inschatting" not in url.lower()
    ]
    if not matches:
        raise ExtractorError(f"no Ecopower tariefkaart link found on {_PRICE_PAGE}")
    matches.sort()
    yyyymm, url = matches[-1]
    label = f"{yyyymm[:4]}-{yyyymm[4:]}"
    return url, label


# Re-export the layout extractor for fixture-based tests so they can
# parse a local PDF without going through the network path.
__all__ = ["EXTRACTOR", "extract_pdf_text_layout", "fetch", "parse_snapshot"]


_ECOPOWER_REGIONS = frozenset({REGION_FLANDERS})

EXTRACTOR = SupplierExtractor(
    id="ecopower",
    label="Ecopower",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="variable",
            regions=_ECOPOWER_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
