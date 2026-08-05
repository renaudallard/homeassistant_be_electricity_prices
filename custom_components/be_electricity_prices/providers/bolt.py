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

"""Bolt Belgium tariff card extractor.

Bolt publishes tariff cards at predictable URLs:

    https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
    https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_11.pdf

Fixed contracts roll monthly via the YYYYMM suffix; variable contracts
use a stable version-number suffix (``_11`` today). Each PDF covers all
three regions in one document - same convention as Eneco.

Bolt's PDFs are visually rich (5 MB each) with rotated columns and a
column-major text layout that pypdf can't read. The extractor goes
through ``pdfplumber`` for layout-aware extraction.

Bolt's price model deviates from the rest in two ways: the fixed fee
is billed per MONTH (``Frais de plateforme 10,99 €/mois``) so the
extractor multiplies by 12 to fit the integration's annual fee
convention, and the Flanders renewables value is split across two
separate lines (``Certificats verts`` + ``WKK``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    FR_MONTHS,
    SIGN_CHARS,
    archive_validity_check,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    parse_brussels_osp,
    parse_sign,
    parse_valid_until,
    to_float,
    vat_multiplier,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    VariableRates,
    brussels_sibelga_overlay,
    walloon_dso_overlay,
)

_LOGGER = logging.getLogger(__name__)

# Process-wide latch for the RESA/REW invariant ERROR. Tripped on the
# first occurrence per HA boot and skipped thereafter so a long-lived
# Bolt regression doesn't repeatedly ring HA's notification bell.
_RESA_REW_LOGGED = False

_BASE_URL = "https://files.boltenergie.be/pricelists"

# Belgian standard rate, which the professional cards price excluding.
_PRO_VAT_RATE = 0.21
_LISTING_URL = "https://www.boltenergie.be/fr/listes-des-prix"
_VARIABLE_SUFFIX = "11"  # current variable-card version

# Bolt's fix cards print "Carte Tarifaire Bolt Fixe <Month> <Year>" in
# the header but never expose a parseable valid_until, so the archive
# cross-check falls back to a textual month match on these names.
_FR_MONTH_NAMES = FR_MONTHS


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    folder: str  # 'fix' or 'var'
    slug: str  # filename prefix
    # 'res' or 'pro' in the filename. Bolt publishes a professional
    # edition of every product at the same path with the segment
    # swapped: same layout, priced excluding VAT.
    segment: str = "res"

    @property
    def professional(self) -> bool:
        return self.segment == "pro"


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("bolt_fix", "Bolt Fixe (1 year)", "fixed", "fix", "fix"),
    _ContractDef(
        "bolt_plenty_fix", "Bolt Plenty Fixe (1 year)", "fixed", "fix", "plenty_fix"
    ),
    _ContractDef("bolt_variable", "Bolt Variable", "variable", "var", "bolt"),
    # Same card + formula as Bolt Variable, but the formula is applied to the
    # live quarter-hourly Belpex spot instead of the monthly average (Bolt's
    # dynamic option on the variable contract). Shares the var/bolt document.
    _ContractDef("bolt_dynamic", "Bolt Dynamisch", "dynamic", "var", "bolt"),
    _ContractDef("bolt_plenty", "Bolt Plenty Variable", "variable", "var", "plenty"),
    _ContractDef("bolt_online", "Bolt Online", "variable", "var", "online"),
    _ContractDef(
        "bolt_plenty_online",
        "Bolt Plenty Online",
        "variable",
        "var",
        "plenty_online",
    ),
    # The professional editions: same paths with the segment swapped.
    _ContractDef(
        "bolt_pro_fix", "Bolt Fixe (pro, 1 year)", "fixed", "fix", "fix", segment="pro"
    ),
    _ContractDef(
        "bolt_pro_plenty_fix",
        "Bolt Plenty Fixe (pro, 1 year)",
        "fixed",
        "fix",
        "plenty_fix",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_variable",
        "Bolt Variable (pro)",
        "variable",
        "var",
        "bolt",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_dynamic",
        "Bolt Dynamisch (pro)",
        "dynamic",
        "var",
        "bolt",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_plenty",
        "Bolt Plenty Variable (pro)",
        "variable",
        "var",
        "plenty",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_online",
        "Bolt Online (pro)",
        "variable",
        "var",
        "online",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_plenty_online",
        "Bolt Plenty Online (pro)",
        "variable",
        "var",
        "plenty_online",
        segment="pro",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(contract: _ContractDef, suffix: str | None = None) -> str:
    if contract.folder == "fix":
        # Fixed cards roll monthly on the Belgian calendar boundary.
        # Use Brussels local time so the suffix flips at the same
        # instant the supplier rotates the URL; UTC would mis-key by
        # last month for the first 1-2 hours of every Brussels month.
        suffix = suffix or dt_util.now().strftime("%Y%m")
    else:
        suffix = suffix or _VARIABLE_SUFFIX
    return (
        f"{_BASE_URL}/{contract.folder}/"
        f"{contract.slug}_{contract.segment}_el_fr_{suffix}.pdf"
    )


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Bolt's PDFs cover every region.
) -> str | None:
    """Cheap freshness probe: HEAD the listing page, return its ETag.

    Bolt's listing returns a stable ETag and the server honours
    ``If-None-Match`` with a 304 response. We just want a key that flips
    on supplier changes, so reading the ETag header on a HEAD round-trip
    is enough.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    return await head_freshness_key(
        session, _LISTING_URL, prefer=("ETag", "Last-Modified")
    )


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return ``{folder}/{slug}`` for every residential electricity card.

    Bolt's prices listing page links every PDF directly. Filter to
    residential electricity (``_res_el_fr_``) and extract the
    ``<folder>/<slug>`` prefix; live_check diffs against the registry's
    ``{c.folder + '/' + c.slug for c in _CONTRACTS}`` set.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return {
        f"{folder}/{slug}"
        for folder, slug in re.findall(
            r"pricelists/(fix|var)/([a-z_]+)_res_el_fr_", html
        )
    }


# ---- top-level fetch + parser -------------------------------------------------


async def _fetch_pdf_text(
    session: aiohttp.ClientSession, contract: _ContractDef
) -> tuple[str, str]:
    """Fetch the latest PDF text for ``contract``, applying the
    fixed-card fallback. Returns ``(url, text)``.

    Lifted out of :func:`fetch` so the live-check script can fetch
    once per contract and parse three region-specific snapshots from
    the same text -- Bolt's PDFs cover all regions, so doing it
    per-(contract, region) wastes a 5+ MB round-trip twice.
    """
    # Bolt's tariff PDFs are ~5 MB each and the CDN occasionally needs
    # well over the shared 30 s default to deliver one (issue #13:
    # all six fetches timed out for ~25 minutes on 2026-05-09 while
    # the URLs themselves were healthy). Use a 60 s budget so a 2-3x
    # CDN slowdown still yields a snapshot instead of UpdateFailed.
    pdf_timeout = 60
    url = _document_url(contract)
    try:
        return url, await fetch_pdf_text_layout(session, url, timeout=pdf_timeout)
    except ExtractorError as primary_err:
        # Fixed cards may not be published yet on the 1st of the month;
        # fall back to the previous month so the user keeps seeing
        # plausible prices instead of UpdateFailed. Bolt cards expose no
        # parseable valid_until, so the fallback can't signal staleness
        # through it; the warning logged below is the only trace that
        # last month's card is being served.
        if contract.folder != "fix":
            raise
        # Same Brussels-local anchor as ``_document_url``: the
        # "previous month" boundary follows local time so we don't
        # accidentally roll back two months on the new-month UTC seam.
        previous = (dt_util.now().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
        fallback_url = _document_url(contract, suffix=previous)
        _LOGGER.warning(
            "Bolt %s: current-month PDF unavailable (%s); "
            "falling back to previous-month card %s",
            contract.contract_id,
            primary_err,
            fallback_url,
        )
        return fallback_url, await fetch_pdf_text_layout(
            session, fallback_url, timeout=pdf_timeout
        )


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the latest Bolt PDF for ``contract_id`` (covers every region)."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Bolt contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    url, text = await _fetch_pdf_text(session, contract)
    return parse_snapshot(contract_id, text, region, url)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch a past month's Bolt fix-family card (returns ``None`` for
    products without a date-keyed archive).

    Bolt's fix family is archived monthly under the ``YYYYMM`` suffix
    going back to 2024-01. The variable family (and the ``plenty_fix``
    variant) uses a stable version-number suffix, so older months
    can't be addressed -- those return ``None`` and the YTD path falls
    back to the current snapshot as a proxy.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    contract = _CONTRACTS_BY_ID[contract_id]
    if contract.folder != "fix" or contract.slug != "fix":
        # Only the bolt_fix family carries a stable monthly archive.
        return None
    suffix = year_month.strftime("%Y%m")
    url = _document_url(contract, suffix=suffix)
    try:
        text = await fetch_pdf_text_layout(session, url, timeout=60)
    except ExtractorError:
        return None
    try:
        snap = parse_snapshot(contract_id, text, region, url)
    except ExtractorError:
        return None
    # The month is URL-keyed, but guard against the CDN ever serving a
    # current card under a historical URL: Bolt cards carry no parseable
    # valid_until, so cross-check the printed "<Month> <Year>" header
    # against the requested month and fall back to the proxy snapshot on
    # a mismatch rather than mis-billing a past month at current rates.
    return archive_validity_check(snap, text, year_month, month_names=_FR_MONTH_NAMES)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Bolt contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    # Bolt's PDFs sprinkle U+2028 LINE SEPARATOR characters where one
    # would expect a newline; normalize to '\n' so a single set of
    # regexes covers every block.
    text = text.replace(" ", "\n")

    professional = contract.professional
    energy = _extract_energy(text, contract.kind, professional=professional)
    injection = _extract_injection(text, contract.kind, region)
    if professional and injection is not None:
        injection = replace(injection, vat_applies=True)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, region_connection_fee = _extract_taxes(
        text, region
    )
    energy_fund = (
        _extract_energy_fund(text, professional=professional)
        if region == REGION_FLANDERS
        else 0.0
    )
    flanders_renewables, wallonia_renewables, brussels_renewables = _extract_renewables(
        text
    )
    if region != REGION_FLANDERS:
        flanders_renewables = 0.0
    if region != REGION_WALLONIA:
        wallonia_renewables = 0.0
    if region != REGION_BRUSSELS:
        brussels_renewables = 0.0

    if region == REGION_FLANDERS:
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        dsos = _extract_wallonia_dsos(text)
    else:
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="bolt",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            brussels_renewables=brussels_renewables,
            region_connection_fee=region_connection_fee,
            energy_fund_eur_per_month=energy_fund,
            # The professional card prices excluding VAT throughout - its
            # distribution block is still headed TTC, but the numbers match
            # the other suppliers' ex-VAT tables to the cent, so the label
            # is stale, not the values. base.apply_vat resolves it.
            vat_rate=_PRO_VAT_RATE if professional else 0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


def _extract_yearly_fee(text: str) -> float:
    """Bolt prints a monthly platform fee (``€ 10,99 / mois``); convert to /year.

    The platform fee is the entire Bolt monetisation; a missing match is
    a layout drift that would silently undercount the user's bill by
    ~130 EUR/year, so raise instead of returning 0.

    Make the decimal portion optional so a future round fee like
    ``€ 11 / mois`` still parses; today every Bolt card prints two
    decimals, but the strictness was a footgun rather than a feature.
    """
    match = re.search(r"€\s*(\d+(?:[.,]\d+)?)\s*/\s*mois", text)
    if match is None:
        raise ExtractorError("Bolt: '€ N[,NN] / mois' platform fee not found")
    return to_float(match.group(1)) * 12.0


# Bolt's variable card prints its tariff formula for both consumption and
# injection as "Belpex * <factor> <sign> <base>" in EUR/MWh (HTVA), one row per
# meter type. The dynamic contract applies the same coefficients to the live
# quarter-hourly Belpex spot. The consumption formula is the first match; the
# injection formula is the first match that differs from it (Bolt lists all
# consumption rows, then all injection rows).
_BELPEX_FORMULA_RE = re.compile(
    rf"Belpex\s*\*\s*([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)"
)


def _extract_dynamic_energy(
    text: str, yearly_fee: float, *, professional: bool = False
) -> DynamicRates:
    """Bolt Dynamic: factor * quarter-hourly Belpex spot + base.

    The card formula is EUR/MWh HTVA; convert to the EUR/kWh basis applied
    against the EUR/kWh spot and bake VAT (snapshot vat_rate is 0): the factor
    is a dimensionless ratio (* VAT), the base goes EUR/MWh -> EUR/kWh (/1000 *
    VAT). Bills per quarter-hour, so keep the native 15-minute grid.

    The professional card prices everything excluding VAT, so there is
    nothing to bake here and the snapshot's vat_rate carries the 21%
    instead. It also drops the "N% TVA" phrase the multiplier reads,
    which would otherwise fall back to the residential 6% default and
    scale the formula twice over.
    """
    matches = _BELPEX_FORMULA_RE.findall(text)
    if not matches:
        raise ExtractorError("Bolt: could not parse dynamic Belpex formula")
    factor_s, sign, base_s = matches[0]
    if professional:
        if "HTVA" not in text:
            raise ExtractorError("Bolt: professional card is not marked HTVA")
        vat = 1.0
    else:
        vat = vat_multiplier(
            text, re.compile(r"(\d+)\s*%\s*(?:TVA|BTW)", re.IGNORECASE)
        )
    base_eur_mwh = parse_sign(sign) * to_float(base_s)
    return DynamicRates(
        factor=to_float(factor_s) * vat,
        base=base_eur_mwh / 1000.0 * vat,
        yearly_fixed_fee=yearly_fee,
        quarter_hourly=True,
    )


def _extract_legacy_energy(
    text: str, kind: TariffKind, yearly_fee: float
) -> EnergyRates | None:
    """Read a pre-April-2026 Bolt card, or ``None`` if this is not one.

    Bolt redesigned its cards between March and April 2026. The archive PDFs
    for the earlier months are still served, and ``fetch_for_month`` reaches
    for them whenever a year-to-date walk crosses Q1 or a contract was signed
    then, but they carry no ``Prix mensuel`` row: the rates sit under
    ``Coût de l'énergie`` with one labelled line per meter type,

        Coût de l'énergie Simple
         c€13,27/kWh
                       Jour
         c€13,27/kWh
                       Nuit
         c€13,27/kWh
                       Excl. nuit c€13,27/kWh

    so ``parse_snapshot`` raised, ``fetch_for_month`` swallowed it, and every
    Q1 month silently billed at the CURRENT card's rate instead.

    Keyed on which anchor the card actually carries rather than on a date, so
    it neither guesses at a boundary nor needs touching when Bolt redesigns
    again. Returns ``None`` when the old anchor is absent too, leaving the
    caller to raise its own error.
    """
    if not re.search(r"Co[ûu]t de l['’]énergie", text):
        return None

    def _rate(label: str) -> float | None:
        # Values render as "c€13,27/kWh", the label sometimes on the line
        # above and sometimes inline, so allow a bounded gap but never cross
        # into another label's value.
        m = re.search(
            rf"{label}[^\n]*\n?[^c\n]*c€\s*([\d.,]+)\s*/\s*kWh", text, re.IGNORECASE
        )
        return to_float(m.group(1)) / 100.0 if m else None

    mono = _rate(r"Co[ûu]t de l['’]énergie\s+Simple")
    if mono is None:
        return None
    peak = _rate(r"\bJour\b") or mono
    offpeak = _rate(r"\bNuit\b") or mono
    excl = _rate(r"Excl\.?\s*nuit") or mono
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    if kind == "variable":
        return VariableRates(
            current=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    # Only the archived fix / variable families use this layout; a dynamic
    # card is handled by its own formula branch before we get here.
    return None


def _extract_energy(
    text: str, kind: TariffKind, *, professional: bool = False
) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        return _extract_dynamic_energy(text, yearly_fee, professional=professional)
    # Bolt's 'Prix mensuel' line is the current month's price for all
    # contract kinds. Static cards have only this; variable cards also
    # show 'Prix annuel estimé' which we ignore.
    #
    # The row renders in one of two shapes, and which one is a property of
    # the individual card render rather than of the product: either two
    # numbers (mono then Exclusif nuit, with the bi-horaire pair left to
    # the "Prix de l'électricité verte" block below), or all four columns
    # inline (mono, Jour, Nuit, Exclusif nuit). Reading the four-number
    # shape with the two-number rule would take Jour for the
    # exclusive-night rate and bill a night circuit at the day price.
    match = re.search(r"Prix mensuel([^\n]*)", text)
    if match is None:
        legacy = _extract_legacy_energy(text, kind, yearly_fee)
        if legacy is not None:
            return legacy
    numbers = re.findall(r"[\d.,]+", match.group(1)) if match else []
    inline_bihourly = len(numbers) >= 4
    if len(numbers) < 2:
        raise ExtractorError(f"could not parse Bolt {kind} consumption block")
    mono = to_float(numbers[0]) / 100.0
    excl = to_float(numbers[3] if inline_bihourly else numbers[1]) / 100.0
    # The "Prix de l'électricité verte" block prints two "Jour Nuit"
    # subheads -- the first is for consumption (with our bi-horaire
    # row), the second is for injection. The bi-horaire row is always
    # the LAST same-line adjacent-number pair between them. pdfplumber
    # sometimes renders the annual-estimate column vertically above
    # that row (variable cards) and sometimes drops it entirely (fixed
    # cards), so we can't anchor on a fixed offset; restricting to the
    # span between the two subheads is the stable invariant.
    span = re.search(
        r"Prix de l'électricité verte.*?Jour\s+Nuit(.*?)Jour\s+Nuit",
        text,
        re.S,
    )
    pairs = (
        re.findall(
            r"^[ \t]*([\d.,]+)[ \t]+([\d.,]+)[ \t]*$",
            span.group(1),
            re.MULTILINE,
        )
        if span
        else []
    )
    if inline_bihourly:
        # The row already carried them, and is the more reliable source:
        # the span anchor below walks into the DSO table on the renders
        # that inline the pair.
        peak = to_float(numbers[1]) / 100.0
        offpeak = to_float(numbers[2]) / 100.0
    elif pairs:
        peak = to_float(pairs[-1][0]) / 100.0
        offpeak = to_float(pairs[-1][1]) / 100.0
    elif kind == "fixed":
        # Bolt fixed cards are mono == peak == offpeak and sometimes omit
        # the bi-horaire row entirely; the single rate is the right value.
        peak = offpeak = mono
    else:
        # Variable cards always publish distinct Jour / Nuit rates; a miss
        # is a layout drift, not a mono contract. Fail loud rather than
        # silently bill a bi-hourly user at the mono rate.
        raise ExtractorError(f"could not parse Bolt {kind} bi-hourly Jour/Nuit rates")

    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    if kind == "variable":
        return VariableRates(
            current=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    # Dynamic is handled up front by _extract_dynamic_energy; any other kind is
    # a registry mistake.
    raise ExtractorError(f"Bolt: unexpected contract kind {kind!r}")


def _extract_publication_month(text: str) -> str:
    """The card's "<Month> <Year>" header, verbatim.

    The accented-letter classes span the whole Latin-1 range rather than
    the handful of accents French month names actually use. Bolt's August
    2026 fixed card prints "Aôut 2026" - the circumflex on the wrong vowel
    - and an exact `[a-zéèû]` class dropped the label to "" on a typo that
    changes nothing about the card's meaning. This value is a display
    label (diagnostics, the snapshot_publication attribute) and never
    feeds pricing, so tolerating a misspelling beats blanking it.
    """
    match = re.search(r"^([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+\s+\d{4})\s*/", text, re.MULTILINE)
    return match.group(1) if match else ""


def _extract_injection(
    text: str, kind: TariffKind, region: str
) -> InjectionRates | None:
    if kind == "dynamic":
        # Dynamic injection is spot-indexed: the same Belpex formula table
        # prints an injection row whose factor is < 1 (Bolt redistributes a
        # fraction of the spot), while every consumption row marks the spot up
        # with a factor > 1. Keying on factor < 1 rather than "the first row
        # that differs from consumption[0]" stays correct even if the card ever
        # prints per-meter-type consumption rows with differing factors.
        # Feed-in is VAT-exempt for residential, so no VAT bake; base goes
        # EUR/MWh -> EUR/kWh.
        matches = _BELPEX_FORMULA_RE.findall(text)
        inj = next((m for m in matches if to_float(m[0]) < 1.0), None)
        if inj is None:
            return None
        return InjectionRates(
            current=None,
            factor=to_float(inj[0]),
            base=parse_sign(inj[1]) * to_float(inj[2]) / 1000.0,
            formula=None,
        )
    # Injection is a flat monthly indicative ("Prix mensuel 5,31 4,03")
    # in the block that follows the "Injection" header, on both fix and
    # variable cards (the consumption "Prix mensuel" sits above it).
    # Anchor on the header rather than counting "Prix mensuel"
    # occurrences, so a third consumption-side row can't shift the match.
    # factor/base stay None: Bolt's feed-in is a printed indicative, not a
    # spot formula. The July 2026 fix cards print a NEGATIVE second
    # ("Exclusif nuit") column ("Prix mensuel 3,40 -0,43"); only the first
    # column is billed but the second is a required anchor token, so allow
    # its optional minus sign. The billed first column carries an optional
    # minus too, so a month that ever prints a negative feed-in indicative
    # is captured instead of failing the match and dropping the credit.
    m = re.search(r"Injection\b.*?Prix mensuel\s+(-?[\d.,]+)\s+-?[\d.,]+", text, re.S)
    if m:
        current = to_float(m.group(1)) / 100.0
        return InjectionRates(current=current, factor=None, base=None, formula=None)
    # A pre-April-2026 archive card has no "Prix mensuel" row. It prints the
    # indicative as a three-column FL/WAL/BX row like the tax block instead:
    # "Injection (c€/kWh) 5,87 6,69 3,78". Missing it dropped the feed-in
    # credit entirely for every archived month a year-to-date walk crosses.
    legacy = re.search(
        r"Injection\s*\(c€/kWh\)\s*(-?[\d.,]+)\s+(-?[\d.,]+)\s+(-?[\d.,]+)",
        text,
    )
    if not legacy:
        return None
    index = {REGION_FLANDERS: 1, REGION_WALLONIA: 2, REGION_BRUSSELS: 3}[region]
    token = legacy.group(index).strip()
    if token == "-" or not token:
        return None
    return InjectionRates(
        current=to_float(token) / 100.0, factor=None, base=None, formula=None
    )


# ---- taxes --------------------------------------------------------------------


def _extract_taxes(text: str, region: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, region_connection_fee).

    Bolt prints taxes as 3-column rows (Flandres / Wallonie / Bruxelles).
    Caller (parse_snapshot) already normalised any Unicode line
    separators (U+2028) to regular newlines, so the regexes below
    see a uniform layout.
    """
    # The three regional values sit on the lines below the label on a
    # post-April-2026 card and INLINE on the label line on the archived
    # pre-redesign ones:
    #   now: "Droit d'accise spécial (c€/kwh) (c€/kWh) 5\n 5,0329\n 5,0329\n 5,0329"
    #   old: "Droit d'accise spécial (c€/kwh) (**) 5,0329 5,0329 5,0329"
    # Skip everything up to the first DECIMAL number, then take three. The
    # decimal separator is what distinguishes a value from the bare footnote
    # marker ("5" above), which a plain [\d.,]+ captured as the Flanders
    # value and billed the excise at 5 c€/kWh instead of 5,0329.
    _THREE_COLS = r"(?:(?!\d+[.,]\d).)*?(\d+[.,]\d+)\s+(\d+[.,]\d+)\s+(\d+[.,]\d+)"
    excise_match = re.search(
        rf"Droit d['’]accise spécial{_THREE_COLS}",
        text,
        re.S,
    )
    contribution_match = re.search(
        rf"Contribution sur l['’]énergie{_THREE_COLS}",
        text,
        re.S,
    )
    # Federal excise and energy contribution are mandatory federal levies
    # printed on every Belgian supplier card; a regex miss means the
    # layout drifted and the snapshot would silently under-bill by
    # several c€/kWh. Raise so the coordinator falls back to the cached
    # snapshot instead.
    if excise_match is None:
        raise ExtractorError("Bolt: 'Droit d'accise spécial' row not found")
    if contribution_match is None:
        raise ExtractorError("Bolt: 'Contribution sur l'énergie' row not found")
    # Connection fee row prints footnote refs ahead of the values, as bare
    # integers on a current card and as parenthesised stars on an archived
    # pre-redesign one:
    #   now: "Redevance de raccordement (c€/kWh) 6 7 - 0,075 -"
    #   old: "Redevance de raccordement (c€/kWh) (*)(***) - 0,075 -"
    # Allow either form ahead of the values; the three trailing tokens are
    # FL/WAL/BX (some are "-" when not applicable). Capping the eater stops a
    # future card with an integer-only Flanders value from being mistaken for
    # a footnote and silently shifting the columns. Matching bare integers
    # only made every archived month bill Wallonia's connection fee at zero.
    connection_match = re.search(
        r"Redevance de raccordement[^\n]*?\(c€/kWh\)\s*"
        r"(?:(?:\(\*+\)|\d+)\s*){0,4}"
        r"(-|[\d.,]+)\s+(-|[\d.,]+)\s+(-|[\d.,]+)",
        text,
    )

    def _per_region(match: re.Match[str] | None, region: str) -> float:
        if match is None:
            return 0.0
        index = {REGION_FLANDERS: 1, REGION_WALLONIA: 2, REGION_BRUSSELS: 3}[region]
        token = match.group(index).strip()
        if token == "-" or not token:
            return 0.0
        return to_float(token) / 100.0

    excise = _per_region(excise_match, region)
    contribution = _per_region(contribution_match, region)
    connection = _per_region(connection_match, region)
    return excise, contribution, connection


def _extract_energy_fund(text: str, *, professional: bool = False) -> float:
    """Flemish energy fund in EUR/month, from the row this contract bills.

    Every card prints both categories. A domiciled residential connection
    pays the 'résidentiel' row, which is '-' (0); a business connection pays
    the 'non-résidentiel' one, which the same card fills in (10,07 on the
    August 2026 card). Reading the residential row for a professional
    contract dropped the levy entirely.

    The two rows are laid out differently and need their own patterns: the
    residential value sits after a U+2028 that the text layer normalises to
    a newline, while the non-residential values are inline on the label
    line after an optional footnote marker.
    """
    if professional:
        match = re.search(
            r"Cotisation Fond énergie, non-résidentiel\s*\(€/mois\)\s*"
            r"(?:\d+\s+)?([\d.,-]+)",
            text,
        )
    else:
        match = re.search(
            r"Cotisation Fond énergie, résidentiel[^\n]*\n\s*([\d.,-]+)",
            text,
        )
    if match is None or match.group(1).strip() == "-":
        return 0.0
    return to_float(match.group(1))


def _extract_renewables(text: str) -> tuple[float, float, float]:
    """Three columns under 'Certificats verts' + Flanders-only WKK row."""
    cert = re.search(
        r"Certificats verts\s*\(c€/kWh\)[^\n]*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    # WKK row: 'WKK (c€/kWh) 8 0,39 -' - skip the optional (multi-digit)
    # footnote ref before capturing the Flanders value. Require a real
    # whitespace separator so the greedy ``\d*`` can't swallow the
    # leading digits of a multi-digit value when the footnote is absent.
    # The trailing ' -' tokens are placeholders for Wallonia / Brussels.
    wkk = re.search(r"WKK\s*\(c€/kWh\)\s+(?:\d+\s+)?([\d.,]+)", text)
    if cert is None:
        # Renewables (certificats verts) are charged in every region; a
        # regex miss is a layout drift that would silently zero ~3 c€/kWh.
        raise ExtractorError("Bolt: 'Certificats verts' renewables row not found")
    fl_cents = to_float(cert.group(1))
    wal_cents = to_float(cert.group(2))
    bx_cents = to_float(cert.group(3))
    if wkk is not None:
        fl_cents += to_float(wkk.group(1))
    return fl_cents / 100.0, wal_cents / 100.0, bx_cents / 100.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vl": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Fluvius rows. Each has 8 numbers in this order:

      data_mgmt_digital | capacity_digital | dist_normal_digital |
      dist_excl_digital | terme_fixe_classic | dist_normal_classic |
      dist_excl_classic | prosumer

    We bill the digital (SMR3) block - columns 1-4 plus the prosumer
    column - and ignore the trailing classic columns.

    pdfplumber sometimes splits the row vertically (one number per line);
    ``\\s+`` matches any whitespace incl newlines, so a single regex
    handles both layouts.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        data_mgmt = to_float(match.group(1))
        capacity = to_float(match.group(2))
        dist_normal = to_float(match.group(3))
        dist_excl = to_float(match.group(4))
        prosumer = to_float(match.group(8))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            # Group 4 is the dedicated exclusive-night meter rate, lower
            # than the normal digital distribution; bill a night circuit
            # at it instead of falling back to the day rate.
            distribution_exclusive_night=dist_excl / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


# Bolt's PDFs have a pdfplumber row-alignment quirk: the rows labeled
# "TECTEO RESA" and "WAVRE" in the extracted text actually carry each
# other's values. Verified against the regulator's published rates and
# every other supplier's PDF. We swap the labels here so DSO lookups
# return the correct numbers. ``_extract_wallonia_dsos`` runs an
# additional runtime sanity check after parsing (RESA must remain
# cheaper than REW under the current Walloon tariff structure); if
# Bolt's PDF ever stops triggering the misalignment the check logs at
# ERROR level so the swap can be removed.
#
# Last manual re-validation against the live PDFs: 2026-05.
# Re-verify at least every 6 months (next: 2026-11) by parsing a
# current Bolt Wallonia card and confirming TECTEO RESA's printed
# distribution_single is HIGHER than WAVRE's (the swap target).
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "TECTEO RESA": DSO_REW,
    "WAVRE": DSO_RESA,
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia rows. Each has 10 numbers:

    mono | jour | nuit | excl_nuit | PIC | MEDIUM | ECO | transport |
    terme_fixe (€/an) | prosumer (€/kVA/an)
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        excl_night = to_float(match.group(4))
        pic = to_float(match.group(5))
        medium = to_float(match.group(6))
        eco = to_float(match.group(7))
        transport = to_float(match.group(8))
        terme_fixe = to_float(match.group(9))
        prosumer = to_float(match.group(10))
        out[key] = walloon_dso_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            pic=pic,
            medium=medium,
            eco=eco,
            transport=transport,
            terme_fixe=terme_fixe,
            prosumer=prosumer,
        )
    # Sanity check: under the swap, RESA's distribution_single must
    # remain strictly cheaper than REW's (regulator pattern that holds
    # for every Walloon tariff card we've parsed). If the inequality
    # ever flips, Bolt almost certainly fixed the upstream layout and
    # our compensating swap now inverts correct values -- log a
    # warning so the maintainer can drop the swap from
    # _WALLONIA_LABELS instead of silently mis-billing.
    global _RESA_REW_LOGGED
    resa = out.get(DSO_RESA)
    rew = out.get(DSO_REW)
    if resa is None and rew is None:
        # Both rows missing: regex drift covers the whole table; the
        # rest of the parser will already have raised. Stay quiet
        # here.
        return out
    if resa is None or rew is None:
        # Only one of the two parsed -- the more dangerous case for
        # the swap, since the surviving row may now be carrying the
        # other DSO's values without anything else to compare it
        # against. Log at ERROR (once per process) so it surfaces in
        # HA's notification bell without re-ringing on every snapshot
        # refresh.
        if not _RESA_REW_LOGGED:
            parsed = DSO_RESA if resa is not None else DSO_REW
            missing = DSO_REW if resa is not None else DSO_RESA
            _LOGGER.error(
                "Bolt RESA/REW row drift: only %s parsed; the label swap "
                "in _WALLONIA_LABELS may now be inverting %s's values",
                parsed,
                missing,
            )
            _RESA_REW_LOGGED = True
        return out
    if resa.distribution_single >= rew.distribution_single:
        if not _RESA_REW_LOGGED:
            _LOGGER.error(
                "Bolt RESA/REW post-swap invariant tripped "
                "(resa=%.4f rew=%.4f); the upstream PDF may have been "
                "fixed and the label swap in _WALLONIA_LABELS likely "
                "needs to be removed",
                resa.distribution_single,
                rew.distribution_single,
            )
            _RESA_REW_LOGGED = True
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Sibelga row: ``Sibelga 9,96 9,96 7,53 7,53 2,27 14,73 -``.

    Layout: mono | jour | nuit | excl_nuit | transport | terme_fixe | prosumer (-)

    Case-insensitive: the pre-April-2026 archive cards print ``SIBELGA``.
    Matching only the current spelling returned an EMPTY dso map for those
    months, and a Brussels entry's year-to-date then skipped every archived
    month outright (``static_breakdown`` raises ``KeyError`` on a missing DSO
    and the walk treats that as "no rate to apply"), billing Q1 at zero.
    """
    match = re.search(
        r"Sibelga\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        r"([\d.,]+)\s+([\d.,]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return {}
    mono = to_float(match.group(1))
    peak = to_float(match.group(2))
    offpeak = to_float(match.group(3))
    excl_night = to_float(match.group(4))
    transport = to_float(match.group(5))
    terme_fixe = to_float(match.group(6))
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            transport=transport,
            data_management_per_year=terme_fixe,
            osp_by_tier=parse_brussels_osp(text),
        )
    }


EXTRACTOR = SupplierExtractor(
    id="bolt",
    label="Bolt",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            professional=c.professional,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
