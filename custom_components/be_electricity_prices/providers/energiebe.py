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

"""energie.be dynamic tariff extractor.

energie.be publishes its tariff cards as PDFs behind a small JSON document
API. The dynamic residential card (Elektriciteit dynamisch tarief particulier)
is served at one stable URL whose content is replaced monthly; a GET
302-redirects to the versioned Azure blob and aiohttp follows it, so the fetch
is the single-URL DATS 24 shape with no archive and no cheap probe.

The card bundles a residential and a professional block in one PDF; only the
residential ("particulier") section is parsed. Unlike Frank Energie and Bolt,
energie.be prints its energy and injection formulas against Belpex in c€/kWh
(not EUR/MWh), so the spot coefficient is NOT scaled by 10.

Region: Flanders only (all 8 Fluvius sub-areas).
"""

from __future__ import annotations

import logging
import re

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
    SIGN_CHARS,
    fetch_pdf_text_layout,
    parse_sign,
    parse_valid_until,
    to_float,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    ExtractorError,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_LOGGER = logging.getLogger(__name__)

# The document API 302-redirects this key to the current month's blob PDF;
# the content is replaced in place, so the URL is stable across months.
_CARD_URL = (
    "https://energie-production-api.azurewebsites.net"
    "/api/v1/data/document?key=DynamicTariffs"
)

_CONTRACT_ID = "energiebe_dynamic"
_CONTRACT_LABEL = "energie.be Dynamisch"
_ENERGIEBE_REGIONS = frozenset({REGION_FLANDERS})

# Belgian residential electricity VAT. energie.be quotes its energy and
# injection formulas "(excl. BTW)" while stating every other value on the
# card is VAT-inclusive, so only the energy leg is scaled to the same
# VAT-inclusive basis (vat_rate stays 0.0, matching Frank). Verified against
# the card's own printed price: 11,93 c€/kWh (incl. VAT) equals
# (1,04 x 10,34 + 0,50) x 1,06. The card's only printed percentage (21% on
# energiedelen) is unrelated, so the rate is a constant, not scraped.
_VAT_MULT = 1.06

# Only the residential block is priced; the card appends a professional block
# whose GSC/WKK, taxes and DSO rows differ. Cut at the professional section
# header so no professional row leaks into a residential snapshot.
_PROF_MARKER = "dynamisch tarief professioneel"

# Row label prefix (regex-safe, unique) -> DSO key. The card wraps two long
# labels across the number row ("Fluvius (Halle-\n<numbers>\nVilvoorde)" and
# the Midden-Vlaanderen row), so each entry anchors on the leading token and
# grabs the four digital-meter columns that follow, wherever they land.
_DSO_ROWS: tuple[tuple[str, str], ...] = (
    ("Antwerpen", DSO_FLUVIUS_ANTWERPEN),
    ("Halle", DSO_FLUVIUS_HALLE_VILVOORDE),
    ("Imewo", DSO_FLUVIUS_IMEWO),
    ("Kempen", DSO_FLUVIUS_IVEKA),
    ("Limburg", DSO_FLUVIUS_LIMBURG),
    ("Midden", DSO_FLUVIUS_INTERGEM),
    ("West", DSO_FLUVIUS_WEST),
    ("Zenne-Dijle", DSO_FLUVIUS_ZENNE_DIJLE),
)

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = r"([\d]+(?:[.,][\d]+)?)"

_ENERGY_RE = re.compile(
    rf"formule\s*\(excl\.?\s*BTW\)\s*:?\s*"
    rf"\(\s*{_NUM}\s*x\s*Belpex\s*([{SIGN_CHARS}])\s*{_NUM}\)",
    re.IGNORECASE,
)
# The unit label "(c€/kWh)" is interleaved between "de formule:" and the
# parenthesised injection formula, so anchor on the injectievergoeding row and
# skip to the first "(factor x Belpex +/- base)" that follows.
_INJECTION_RE = re.compile(
    rf"injectievergoeding.*?"
    rf"(\(\s*{_NUM}\s*x\s*Belpex\s*([{SIGN_CHARS}])\s*{_NUM}\))",
    re.IGNORECASE | re.DOTALL,
)
_FEE_RE = re.compile(rf"Vaste\s+vergoeding\s+{_NUM}\s+\([^)]*jaar", re.IGNORECASE)
_GSC_RE = re.compile(rf"\bGSC\b\s+{_NUM}")
_WKK_RE = re.compile(rf"\bWKK\b\s+{_NUM}")
_EXCISE_RE = re.compile(
    rf"Bijzondere\s+accijns\s+op\s+Energie\s*\([^)]*\)\s*\*{{0,2}}\s*{_NUM}"
)
_CONTRIB_RE = re.compile(rf"Bijdrage\s+op\s+de\s+Energie\s*\([^)]*\)\s*{_NUM}")
# Anchored on "Residentieel" right after "Energiefonds"; the sibling
# "Niet-Residentieel" row (VAT-exempt, non-residential) cannot match.
_FUND_RE = re.compile(
    rf"Bijdrage\s+Energiefonds\s+Residentieel\s*\([^)]*\)\s*\*?\s*{_NUM}"
)
_LABEL_RE = re.compile(
    r"particulier\s+online\s*[–\-]\s*([A-Za-z]+)\s*(20\d{2})", re.IGNORECASE
)


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown energie.be contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("energie.be only operates in Flanders")
    text = await fetch_pdf_text_layout(session, _CARD_URL)
    return parse_snapshot(text, _CARD_URL, contract_id)


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    text: str,
    source_url: str,
    contract_id: str = _CONTRACT_ID,
    publication_label: str = "",
) -> SupplierSnapshot:
    section = _residential(text)
    return SupplierSnapshot(
        supplier="energiebe",
        contract=contract_id,
        energy=_extract_energy(section),
        dsos=_extract_dsos(section),
        taxes=_extract_taxes(section),
        source_url=source_url,
        publication_label=publication_label or _publication_label(section),
        valid_until=parse_valid_until(section),
        injection=_extract_injection(section),
    )


def _residential(text: str) -> str:
    """Return the residential slice, dropping the professional block."""
    cut = text.find(_PROF_MARKER)
    return text[:cut] if cut > 0 else text


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return f"{m.group(1).lower()} {m.group(2)}" if m else ""


def _extract_energy(text: str) -> DynamicRates:
    m = _ENERGY_RE.search(text)
    if not m:
        raise ExtractorError("could not parse energie.be energy formula")
    factor_pdf = to_float(m.group(1))
    base_cents = parse_sign(m.group(2)) * to_float(m.group(3))
    # energie.be prints Belpex in c€/kWh (not EUR/MWh like Frank / Bolt) and
    # quotes the formula excl. BTW. ENTSO-E spot is EUR/kWh, and
    # Belpex_c€/kWh = spot_EUR/kWh * 100, so:
    #   price_c€/kWh (excl VAT)  = factor_pdf * (spot * 100) + base_cents
    #   price_EUR/kWh (incl VAT) = (factor_pdf * spot + base_cents / 100) * VAT
    # => factor = factor_pdf * VAT ; base = base_cents / 100 * VAT  (no * 10).
    factor = factor_pdf * _VAT_MULT
    base = base_cents / 100.0 * _VAT_MULT
    fee = _FEE_RE.search(text)
    if fee is None:
        # The vaste vergoeding standing charge is mandatory; fail loud rather
        # than silently bill a zero yearly fee on a layout drift.
        raise ExtractorError("energie.be: vaste vergoeding row not found")
    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=to_float(fee.group(1)),
        quarter_hourly=True,
    )


def _extract_injection(text: str) -> InjectionRates:
    m = _INJECTION_RE.search(text)
    if not m:
        # Every dynamic card prints a terugleveringsvergoeding formula; a miss
        # is a layout drift, not a fee-free contract. Raise rather than
        # silently credit a solar user 0 EUR/kWh.
        raise ExtractorError("energie.be: injection formula row not found")
    factor_pdf = to_float(m.group(2))
    base_cents = parse_sign(m.group(3)) * to_float(m.group(4))
    # Injection is VAT-exempt. Belpex is in c€/kWh here too, so
    # factor = factor_pdf and base = base_cents / 100 (no * 10, no VAT).
    return InjectionRates(
        factor=factor_pdf,
        base=base_cents / 100.0,
        formula=m.group(1),
    )


def _extract_taxes(text: str) -> TaxOverlay:
    excise = _EXCISE_RE.search(text)
    contrib = _CONTRIB_RE.search(text)
    if not excise or not contrib:
        raise ExtractorError("could not parse energie.be tax block")
    gsc = _GSC_RE.search(text)
    wkk = _WKK_RE.search(text)
    if not gsc or not wkk:
        # energie.be dynamic is Flanders-only, so GSC + WKK are mandatory
        # renewables levies; a miss would silently under-bill.
        raise ExtractorError("could not parse energie.be GSC/WKK levies")
    fund = _FUND_RE.search(text)
    # Every value on the card is VAT-inclusive (the federal excise and the
    # energy fund are VAT-exempt), so vat_rate stays 0.0, matching Frank.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        energy_contribution=to_float(contrib.group(1)) / 100.0,
        flanders_renewables=(
            to_float(gsc.group(1)) / 100.0 + to_float(wkk.group(1)) / 100.0
        ),
        energy_fund_eur_per_month=(to_float(fund.group(1)) if fund else 0.0),
        vat_rate=0.0,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find("Nettarieven")
    if start < 0:
        raise ExtractorError("could not locate energie.be DSO table")
    section = text[start:]
    out: dict[str, DsoOverlay] = {}
    for prefix, key in _DSO_ROWS:
        row = re.search(
            rf"Fluvius\s*\(\s*{re.escape(prefix)}[^\d]*"
            rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
            section,
            re.IGNORECASE,
        )
        if not row:
            continue
        databeheer = to_float(row.group(1))
        capacity = to_float(row.group(2))
        normal = to_float(row.group(3)) / 100.0
        excl_night = to_float(row.group(4)) / 100.0
        out[key] = DsoOverlay(
            distribution_single=normal,
            distribution_exclusive_night=excl_night,
            transport=0.0,
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
        )
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    id="energiebe",
    label="energie.be",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="dynamic",
            regions=_ENERGIEBE_REGIONS,
        ),
    ),
    fetch=fetch,
)


__all__ = ["EXTRACTOR", "fetch", "parse_snapshot"]
