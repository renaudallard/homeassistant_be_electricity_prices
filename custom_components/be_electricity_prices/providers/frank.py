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

"""Frank Energie Belgium dynamic tariff extractor.

Frank Energie Belgium publishes monthly tariff cards as PDFs hosted on a
Sanity CMS CDN.  Five dynamic contract tiers share the same PDF layout
with different formula parameters (factor, base, monthly fee):

  - Dynamisch (standard)
  - Dynamisch HV (higher subscription, lower per-kWh margin)
  - Dynamisch Korting (120 EUR cashback after 1 year)
  - Dynamisch JN (lower subscription, different formula)
  - Dynamisch Slim (requires smart devices: solar, EV, battery, heat pump)

Data source: Sanity file asset API (public, no auth required).
Region: Flanders only (all 8 Fluvius sub-areas).
"""

from __future__ import annotations

import json
import logging
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
    NUM_NO_THOUSANDS,
    flanders_tax_overlay,
    NL_MONTHS,
    SIGN_CHARS,
    archive_validity_check,
    fetch_pdf_text_layout,
    fetch_text,
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

_SANITY_API = "https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be"

# Frank prints the month title-cased ("Januari"); keep a title tuple for
# indexing and the header regex, plus a lowercase set for membership.
_NL_MONTHS = NL_MONTHS
_NL_MONTHS_TITLE = tuple(m.capitalize() for m in NL_MONTHS)
_NL_MONTHS_LOWER: frozenset[str] = frozenset(NL_MONTHS)

# (contract_id, label, sanity filename suffix after "Dynamisch")
_TIERS: tuple[tuple[str, str, str | None], ...] = (
    ("frank_dynamic", "Frank Energie Dynamisch", None),
    ("frank_dynamic_hv", "Frank Energie Dynamisch HV", "HV"),
    ("frank_dynamic_korting", "Frank Energie Dynamisch Korting", "VT"),
    ("frank_dynamic_jn", "Frank Energie Dynamisch JN", "JN"),
    ("frank_dynamic_slim", "Frank Energie Dynamisch Slim", "SL"),
)
_TIER_SUFFIX: dict[str, str | None] = {t[0]: t[2] for t in _TIERS}
_VALID_IDS: frozenset[str] = frozenset(_TIER_SUFFIX)

# discover() reverse maps: a filename suffix back to its contract id, and
# the bare-month (suffixless) filename to the standard tier.
_SUFFIX_TO_ID: dict[str, str] = {v: k for k, v in _TIER_SUFFIX.items() if v is not None}
_DEFAULT_TIER_ID: str = next(t[0] for t in _TIERS if t[2] is None)

# Frank alternates the Slim tier's filename token between the abbreviation
# "SL" and the full word "Slim" from one month to the next (both are live in
# the CMS), so treat them as aliases when matching a card to its tier.
_SUFFIX_ALIASES: dict[str, tuple[str, ...]] = {"SL": ("SL", "Slim")}

_FLUVIUS_LABELS: dict[str, str] = {
    "Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Imewo": DSO_FLUVIUS_IMEWO,
    "Kempen": DSO_FLUVIUS_IVEKA,
    "Limburg": DSO_FLUVIUS_LIMBURG,
    "Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "West": DSO_FLUVIUS_WEST,
    "Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}

_FRANK_REGIONS = frozenset({REGION_FLANDERS})


# ---- Sanity CMS API helpers --------------------------------------------------


def _matches_suffix(filename: str, suffix: str | None) -> bool:
    """True when *filename* belongs to the tier identified by *suffix*."""
    m = re.search(r"Dynamisch\s+(\S+)", filename)
    if not m:
        return False
    word = m.group(1)
    if suffix is None:
        return word.lower() in _NL_MONTHS_LOWER
    return word in _SUFFIX_ALIASES.get(suffix, (suffix,))


async def _sanity_query(
    session: aiohttp.ClientSession,
    query: str,
) -> list[dict[str, str]]:
    body = await fetch_text(session, _SANITY_API, params={"query": query}, timeout=15)
    try:
        result = json.loads(body).get("result", [])
        if isinstance(result, dict):
            return [result]
        return list(result)
    except (json.JSONDecodeError, AttributeError, TypeError) as err:
        raise ExtractorError(f"Sanity API response parse error: {err}") from err


async def _resolve_pdf_url(
    session: aiohttp.ClientSession,
    contract_id: str,
    target_month: date | None = None,
) -> tuple[str, str]:
    """Return (pdf_url, publication_label) for the requested tier and month.

    When *target_month* is None the latest available card is returned.
    """
    suffix = _TIER_SUFFIX.get(contract_id)
    if suffix is None and contract_id not in _VALID_IDS:
        raise ExtractorError(f"unknown Frank Energie contract {contract_id!r}")

    if target_month is not None:
        month_name = _NL_MONTHS_TITLE[target_month.month - 1]
        q = (
            '*[_type=="sanity.fileAsset"'
            ' && originalFilename match "*Elektriciteit Dynamisch*"'
            f' && originalFilename match "*{month_name}*"'
            f' && originalFilename match "*{target_month.year}*"'
            "]{originalFilename,url,_createdAt}"
        )
    else:
        q = (
            '*[_type=="sanity.fileAsset"'
            ' && originalFilename match "*Elektriciteit Dynamisch*"'
            "]{originalFilename,url,_createdAt}"
            " | order(_createdAt desc)[0..29]"
        )

    rows = await _sanity_query(session, q)
    matches = [
        r for r in rows if _matches_suffix(r.get("originalFilename", ""), suffix)
    ]
    if not matches:
        raise ExtractorError(
            f"no Frank Energie tariff card found for {contract_id}"
            + (f" ({target_month})" if target_month else "")
        )
    matches.sort(key=lambda r: r.get("_createdAt", ""), reverse=True)
    best = matches[0]
    url = best["url"]
    fname = best.get("originalFilename", "")
    m = re.search(
        r"(" + "|".join(re.escape(n) for n in _NL_MONTHS_TITLE) + r")\s+(\d{4})",
        fname,
    )
    label = f"{m.group(1).lower()} {m.group(2)}" if m else ""
    return url, label


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id not in _VALID_IDS:
        raise ExtractorError(f"unknown Frank Energie contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("Frank Energie only operates in Flanders")
    pdf_url, label = await _resolve_pdf_url(session, contract_id)
    text = await fetch_pdf_text_layout(session, pdf_url)
    return parse_snapshot(text, pdf_url, contract_id, label)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001
    year_month: date,
) -> SupplierSnapshot | None:
    if contract_id not in _VALID_IDS:
        return None
    try:
        pdf_url, label = await _resolve_pdf_url(
            session, contract_id, target_month=year_month
        )
        text = await fetch_pdf_text_layout(session, pdf_url)
        snap = parse_snapshot(text, pdf_url, contract_id, label)
    except ExtractorError:
        return None
    return archive_validity_check(snap, text, year_month, month_names=_NL_MONTHS)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001
) -> str | None:
    if contract_id not in _VALID_IDS:
        return None
    q = (
        '*[_type=="sanity.fileAsset"'
        ' && originalFilename match "*Elektriciteit Dynamisch*"'
        "] | order(_createdAt desc)[0]{_createdAt}"
    )
    try:
        rows = await _sanity_query(session, q)
    except ExtractorError:
        return None
    if rows and rows[0]:
        return str(rows[0].get("_createdAt", ""))
    return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Surface the dynamic tiers visible in Frank Energie's Sanity CMS.

    Each tier publishes one PDF per month named
    "... Elektriciteit Dynamisch[ <suffix>] <Month> <Year>". Map the word
    after "Dynamisch" back to our contract id - a bare month name is the
    standard tier - and surface an unrecognised suffix as
    ``frank_dynamic_<suffix>`` so the catalog drift detector flags a new
    tier instead of silently ignoring it.
    """
    q = (
        '*[_type=="sanity.fileAsset"'
        ' && originalFilename match "*Elektriciteit Dynamisch*"'
        "]{originalFilename,_createdAt}"
        " | order(_createdAt desc)[0..59]"
    )
    try:
        rows = await _sanity_query(session, q)
    except ExtractorError:
        return set()
    out: set[str] = set()
    for row in rows:
        m = re.search(r"Dynamisch\s+(\S+)", row.get("originalFilename", ""))
        if not m:
            continue
        word = m.group(1)
        if word.lower() in _NL_MONTHS_LOWER:
            out.add(_DEFAULT_TIER_ID)
        elif word in _SUFFIX_TO_ID:
            out.add(_SUFFIX_TO_ID[word])
        else:
            out.add(f"frank_dynamic_{word.lower()}")
    return out


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    text: str,
    source_url: str,
    contract_id: str,
    publication_label: str = "",
) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="frank",
        contract=contract_id,
        energy=_extract_dynamic(text),
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
    )


# ---- energy ------------------------------------------------------------------

# Accept both decimal separators: to_float normalizes either, and the
# sibling extractors (luminus, eneco) already do. A dot-decimal re-render
# of the card would otherwise truncate values to the integer part - a
# mandatory tax row silently dropping to 0, or the VAT multiplier 1,06
# collapsing to 1 - instead of failing loud.
_NUM = NUM_NO_THOUSANDS

_FORMULA_RE = re.compile(
    rf"\({_NUM}\s*x\s*BELPEX\s*per\s*uur\*?\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\)\s*x\s*{_NUM}",
    re.IGNORECASE,
)

_MONTHLY_FEE_RE = re.compile(
    r"Abonnementskost\s*\(EUR/maand\)\s*" + _NUM,
)


def _extract_dynamic(text: str) -> DynamicRates:
    formula = _FORMULA_RE.search(text)
    if not formula:
        raise ExtractorError("could not parse Frank Energie energy formula")
    factor_pdf = to_float(formula.group(1))
    sign = parse_sign(formula.group(2))
    base_pre_vat_cents = sign * to_float(formula.group(3))
    vat_mult = to_float(formula.group(4))

    # PDF formula: (factor_pdf * BELPEX_EUR_MWh + base_cents) * vat_mult
    # in EURct/kWh.  ENTSO-E returns spot in EUR/kWh = EUR/MWh / 1000.
    # => factor = factor_pdf * vat_mult * 1000 / 100 = factor_pdf * vat_mult * 10
    # => base   = base_cents * vat_mult / 100
    factor = factor_pdf * vat_mult * 10.0
    base = base_pre_vat_cents * vat_mult / 100.0

    fee_match = _MONTHLY_FEE_RE.search(text)
    if fee_match is None:
        # The monthly standing charge (~35 EUR/yr) is mandatory; the
        # adjacent tax block already fails loud, so do the same here
        # rather than silently bill a zero standing charge on drift.
        raise ExtractorError("Frank Energie: monthly fixed fee row not found")
    yearly_fee = to_float(fee_match.group(1)) * 12.0

    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=yearly_fee,
    )


# ---- injection ---------------------------------------------------------------

_INJECTION_RE = re.compile(
    r"[Tt]e?rugleveringsvergoeding[:\s]*"
    rf"\({_NUM}\s*x\s*BELPEX\s*per\s*uur\*?\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\)",
    re.IGNORECASE,
)


def _extract_injection(text: str) -> InjectionRates:
    m = _INJECTION_RE.search(text)
    if not m:
        # Every Frank dynamic card prints a terugleveringsvergoeding
        # formula; a miss is a layout drift, not a fee-free contract.
        # Raise like the monthly-fee and GSC/WKK rows rather than
        # silently crediting a solar user 0 EUR/kWh.
        raise ExtractorError("Frank Energie: injection formula row not found")
    factor_pdf = to_float(m.group(1))
    # The sign between BELPEX and the base is mandatory in the regex
    # (matching the energy formula), so a sign-less or reworded formula
    # misses and raises above rather than silently defaulting to minus.
    sign = parse_sign(m.group(2))
    base_cents = sign * to_float(m.group(3))
    # Injection is VAT-exempt: no vat_mult scaling.
    # factor_pdf * BELPEX_EUR_MWh in EURct/kWh => factor = factor_pdf * 10
    # base_cents in EURct/kWh => base = base_cents / 100
    return InjectionRates(
        factor=factor_pdf * 10.0,
        base=base_cents / 100.0,
        formula=m.group(0),
    )


# ---- taxes -------------------------------------------------------------------

_EXCISE_RE = re.compile(
    r"Bijzondere\s+accijns\s+op\s+Energie\s*\(EURct/kWh\)\s*\*{0,2}\s*" + _NUM
)
_ENERGY_CONTRIB_RE = re.compile(r"Bijdrage\s+op\s+Energie\s*\(EURct/kWh\)\s*" + _NUM)
_GSC_RE = re.compile(r"GSC\s*\(EURct/kWh\)\s*" + _NUM)
_WKK_RE = re.compile(r"WKK\s*\(EURct/kWh\)\s*" + _NUM)
_FUND_RE = re.compile(
    r"Bijdrage\s+Energiefonds\s+Residentieel\s*\(EUR/maand\)\s*\*?\s*" + _NUM
)


def _extract_taxes(text: str) -> TaxOverlay:
    """All values on the card are VAT-inclusive (6% BTW)."""
    return flanders_tax_overlay(
        text,
        supplier="Frank Energie",
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_ENERGY_CONTRIB_RE,
        fund=_FUND_RE,
    )


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    section_start = text.find("Digitale meter")
    section_end = text.find("Klassieke meter")
    if section_start < 0:
        raise ExtractorError("could not locate Frank Energie DSO table")
    section = text[section_start : section_end if section_end > section_start else None]

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLUVIUS_LABELS.items():
        escaped = re.escape(label).replace(r"\-", r"[\s\-]*")
        row = re.search(
            rf"Fluvius\s*[\[\(]\s*{escaped}\s*[\]\)]\s*\n"
            rf"\s*{_NUM}\s*\n\s*{_NUM}\s*\n\s*{_NUM}\s*\n\s*{_NUM}",
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
    id="frank",
    label="Frank Energie",
    contracts=tuple(
        Contract(
            id=cid,
            label=clabel,
            kind="dynamic",
            regions=_FRANK_REGIONS,
        )
        for cid, clabel, _ in _TIERS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
