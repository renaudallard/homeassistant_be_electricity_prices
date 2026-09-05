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

"""Ecofix Gas & Power tariff card extractor.

Ecofix publishes residential electricity tariff cards at stable URLs:

    https://portal.ecofixgp.be/docs/prices/current/EL_Ecofix_<PRODUCT>_NL.pdf

Four products are sold today:

  - Motion         : dynamic, 15-min Belpex-indexed, phone customer service.
  - Motion Online  : dynamic, 15-min Belpex-indexed, online-only.
  - Flexy          : variable, monthly RLP-weighted Belpex average.
  - Flexy Online   : the same variable product, online-only.

Cards cover Flanders + Wallonia in one PDF (no Brussels rows). The same DSO
and tax overlay is repeated across the three monthly cards; only the
energy formula and yearly fixed fee differ between them.

The PDF text layout requires pdfplumber's row reconstruction
(``fetch_pdf_text_layout``); pypdf returns the Wallonia DSO block in
column-major order which can't be matched by row-anchored regex.
Filenames are overwrite-in-place: there is no public archive of past
months, so ``fetch_for_month`` is omitted and the coordinator's
proxy-forward fallback handles past consumption windows.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

import aiohttp

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    FLUVIUS_CARD_LABELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    parse_prosumer_column,
    require_contract,
    NL_MONTHS,
    SIGN_CHARS,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    head_ok,
    parse_sign,
    scan_month_end,
    to_float,
    vat_multiplier,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    VariableRates,
    walloon_dso_overlay,
)

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://portal.ecofixgp.be/docs/prices/current"
# The public tariff-card page server-renders one anchor per card, so it is a
# real discovery surface: measured 2026-09-05 it carries all four residential
# electricity cards plus the two gas cards. /tarieven is server-rendered too
# but links only Flexy and Motion, which is what the old docstring was
# describing when it concluded no listing existed.
_LISTING_URL = "https://www.ecofixgp.be/tarieven/tariefkaarten"


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # filename stem after EL_Ecofix_


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("ecofix_motion", "Ecofix Motion", "dynamic", "Motion"),
    _ContractDef(
        "ecofix_motion_online", "Ecofix Motion Online", "dynamic", "Motion_Online"
    ),
    _ContractDef("ecofix_flexy", "Ecofix Flexy", "variable", "Flexy"),
    _ContractDef(
        "ecofix_flexy_online", "Ecofix Flexy Online", "variable", "Flexy_Online"
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(contract: _ContractDef) -> str:
    return f"{_BASE_URL}/EL_Ecofix_{contract.slug}_NL.pdf"


# The same filename read back off the listing. ``EL_`` is what separates a card
# from the ``GAS_`` twin the listing links beside it, and the leading slash
# keeps the match on a path rather than on prose. Digits and hyphens are in the
# class deliberately: with a letters-only stem an Ecofix product named for its
# term (EL_Ecofix_Flexy_24_NL.pdf) matches nothing and the catalogue check goes
# green on it, which is the exact failure this replaces.
_CARD_URL_RE = re.compile(r"/EL_Ecofix_([A-Za-z0-9_-]+)_NL\.pdf")


_DUTCH_MONTHS: dict[str, int] = {name: i for i, name in enumerate(NL_MONTHS, 1)}


# ---- top-level fetch / probe / discover --------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch and parse the published Ecofix PDF for ``contract_id``.

    Same PDF carries Flanders + Wallonia overlays; the parser narrows
    the snapshot down to ``region``.
    """
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Ecofix")
    url = _document_url(contract)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, region, url)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - URL is region-agnostic.
) -> str | None:
    """HEAD the per-contract PDF and return its freshness key.

    Ecofix overwrites the card in place under a stable filename; the
    response's ``Last-Modified`` (or ``ETag``) flips when a new month
    is published.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    return await head_freshness_key(session, _document_url(contract))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return contract ids for every Ecofix PDF that currently 200s.

    Read the tariefkaarten page, which server-renders one anchor per card.
    HEAD-probing the registry's own URLs, which is what this did, cannot
    surface a product the registry does not already know: ``discovered -
    baseline`` was empty by construction, so the catalogue check reported
    green for the whole time Flexy Online was on sale. A listing the
    supplier maintains is the only thing that can answer the question.

    Falls back to the probe when the page is unreachable, so a listing
    outage degrades to the old behaviour instead of reporting that every
    product has been withdrawn.
    """
    try:
        listing = await fetch_text(session, _LISTING_URL)
    except ExtractorError as err:
        _LOGGER.warning("Ecofix discover: %s unreachable: %s", _LISTING_URL, err)
        return await _head_probe_ids(session)
    return {f"ecofix_{slug.lower()}" for slug in _CARD_URL_RE.findall(listing)}


async def _head_probe_ids(session: aiohttp.ClientSession) -> set[str]:
    """The registry's own URLs that still 200, the pre-listing behaviour."""
    out: set[str] = set()
    for contract in _CONTRACTS:
        if await head_ok(session, _document_url(contract), timeout=10):
            out.add(contract.contract_id)
    return out


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Parse one Ecofix tariff card into a region-narrowed snapshot."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Ecofix")

    yearly_fee, flanders_renewables_eur_per_kwh = _extract_fee_and_flanders_renewables(
        text, contract.kind
    )
    energy = _extract_energy(text, contract.kind, yearly_fee)
    injection = _extract_injection(text, contract.kind)
    publication_label, valid_until = _extract_publication(text)

    federal_excise, energy_contribution = _extract_federal_taxes(text)
    region_connection_fee = (
        _extract_wallonia_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )
    flanders_renewables = (
        flanders_renewables_eur_per_kwh if region == REGION_FLANDERS else 0.0
    )
    wallonia_renewables = (
        _extract_wallonia_renewables(text) if region == REGION_WALLONIA else 0.0
    )

    if region == REGION_FLANDERS:
        dsos = _extract_flanders_dsos(text, contract.kind)
    elif region == REGION_WALLONIA:
        dsos = _extract_wallonia_dsos(text)
    else:
        # Ecofix doesn't sell in Brussels; the registry's region filter
        # should already prevent this, but keep the snapshot well-formed.
        dsos = {}

    return SupplierSnapshot(
        supplier="ecofix",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            region_connection_fee=region_connection_fee,
            energy_fund_eur_per_month=0.0,
            vat_rate=0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=valid_until,
        injection=injection,
    )


# ---- energy + injection -----------------------------------------------------


def _flanders_energy_block(text: str) -> str:
    """The Vlaanderen energy block runs from the ``Vlaanderen`` heading
    down to ``Wallonië``; both yearly fee and FL renewables live there.
    pdfplumber lays the two numbers out in different relative orders
    across cards (Motion has ``60,00`` then ``Type gebruik 1,60``;
    Motion Online has ``1,60`` then ``10,00 Type gebruik``), so callers
    extract both numbers from this slice and disambiguate by magnitude.
    """
    match = re.search(r"Vlaanderen([\s\S]+?)Wallonië", text)
    if not match:
        raise ExtractorError("Ecofix: Vlaanderen / Wallonië energy block not found")
    return match.group(1)


def _extract_fee_and_flanders_renewables(
    text: str, kind: TariffKind
) -> tuple[float, float]:
    """Return ``(yearly_fee_eur, flanders_renewables_eur_per_kwh)``.

    The Vlaanderen block prints both values in c€/kWh (renewables) and
    €/jaar (fee) but with the relative order flipped between Motion
    and Motion Online and a third layout for Flexy. Disambiguate by
    magnitude: renewables on Belgian residential cards are < 5 c€/kWh
    and yearly fees are ≥ 10 €/jaar, so the smaller token is always
    the renewable and the larger one is the fee.
    """
    if kind == "variable":
        # Flexy: yearly fee precedes "meter Piekuren"; FL renewables
        # follow the Verbruik label on a single line:
        #     60,00 meter Piekuren ...
        #     Verbruik 1,60
        # Anchor on the "Vaste ... Vlaanderen" header before the
        # "meter Piekuren" hit, so a future stray integer earlier in
        # the document can't shadow the fee.
        fee_match = re.search(
            r"Vaste\s+Energieprijs\s+Vlaanderen[\s\S]{0,400}?"
            r"(\d+(?:,\d+)?)\s+meter Piekuren",
            text,
        )
        # The July 2026 Flexy card re-rendered the Vlaanderen renewable
        # onto its OWN line ABOVE the "Verbruik" label ("1,60\nVerbruik")
        # instead of after it ("Verbruik 1,60"), which stopped the old
        # same-line anchor from matching and took the whole card offline.
        # Accept either order, scoped to the Vlaanderen block so the later
        # federal "Verbruik tussen 0 & 3.000 kWh" row cannot shadow it.
        renew_match = re.search(
            r"Verbruik\s+(\d+,\d+)|(\d+,\d+)\s+Verbruik",
            _flanders_energy_block(text),
        )
        if not fee_match:
            raise ExtractorError("Ecofix Flexy: yearly fixed fee not found")
        if not renew_match:
            raise ExtractorError("Ecofix Flexy: Vlaanderen renewables not found")
        fee = to_float(fee_match.group(1))
        renewable_cents = to_float(renew_match.group(1) or renew_match.group(2))
        # Both anchors here are positional, and the Online twin of a product is
        # exactly what reflows this pair: Motion Online prints the fee and the
        # renewable in the opposite order to Motion. The dynamic branch below
        # absorbs that by taking max/min of one slice; two separate anchors
        # cannot. Read swapped, the Flexy card bills a 1,60 EUR/jaar fee with
        # 0,6000 EUR/kWh of Flemish renewables, measured at +1.985,60 EUR a
        # year at 3.500 kWh and +2.861,60 at 5.000. Bound the renewable rather
        # than compare it to the fee: the boundary this docstring already
        # documents is "renewables under 5 c/kWh", while a fee-relative test
        # would also reject a legitimate low or zero standing charge, which is
        # a real Belgian online-only offer.
        if renewable_cents >= 5.0:
            raise ExtractorError(
                "Ecofix Flexy: Vlaanderen renewable above 5 c/kWh, so the fee "
                "and renewable columns swapped"
            )
        return fee, renewable_cents / 100.0

    block = _flanders_energy_block(text)
    # Two numbers live between "(€ cent/kWh)" (closing the WKK header)
    # and the end of the Vlaanderen block: yearly fee + FL renewable.
    # Anchor on the SECOND "(€ cent/kWh)" inside the block to skip the
    # Energieprijs unit row, then collect both numbers.
    cent_marker = re.search(r"&\s*WKK[\s\S]+?\(€\s*cent/kWh\)([\s\S]+)", block)
    if not cent_marker:
        raise ExtractorError("Ecofix dynamic: '& WKK / (€ cent/kWh)' anchor missing")
    numbers = re.findall(r"\b\d+,\d+\b", cent_marker.group(1))
    if len(numbers) < 2:
        raise ExtractorError(
            "Ecofix dynamic: expected fee + FL renewables in the Vlaanderen block"
        )
    parsed = [to_float(n) for n in numbers[:2]]
    fee = max(parsed)
    renewable_cents = min(parsed)
    return fee, renewable_cents / 100.0


def _dynamic_formula_match(text: str, label: str) -> re.Match[str] | None:
    """Return the ``(factor x Belpex 15M) <sign> base`` formula that
    follows ``label`` (``Afname`` for consumption, ``Injectie`` for
    injection). Anchoring each role on its own label rather than indexing
    into a document-order ``findall`` keeps consumption and injection from
    silently swapping if Ecofix reorders the two blocks. The fill between
    the label and its formula is tempered so it can't cross the
    ``Injectie`` label -- otherwise a reworded/absent ``Afname`` formula
    would let the Afname anchor reach forward and bind the injection
    formula to consumption.
    """
    return re.search(
        rf"{label}(?:(?![Ii]njectie)[\s\S]){{0,200}}?\(([\d,]+)\s*x\s*Belpex\s*15M\)\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)",
        text,
    )


def _extract_energy(text: str, kind: TariffKind, yearly_fee: float) -> EnergyRates:
    if kind == "dynamic":
        # Dynamic cards print the consumption formula on the line directly
        # after "Afname <indicative>" e.g.:
        #   Afname 11,74
        #   Prijsformule excl. BTW (0,1010 x Belpex 15M) + 0,9
        # The injection block carries the same shape under "Injectie", so
        # anchor on the "Afname" label rather than taking the first
        # formula in document order.
        formula = _dynamic_formula_match(text, "Afname")
        if formula is None:
            raise ExtractorError("Ecofix dynamic: Afname Belpex 15M formula not found")
        factor_pdf = to_float(formula.group(1))
        base_pdf_cents = parse_sign(formula.group(2)) * to_float(formula.group(3))
        # PDF formula is c€/kWh ex-VAT against Belpex in €/MWh. The card
        # banner prints "Prijzen inclusief X% BTW"; read X to track future
        # VAT changes without a code update. Conversion to
        # EUR/kWh-against-EUR/kWh-spot: factor stays unitless (x1000/100
        # = x10) and base divides cents->EUR (/100).
        vat = vat_multiplier(
            text, re.compile(r"inclusief\s+(\d+)\s*%\s*BTW", re.IGNORECASE)
        )
        # Motion / Motion Online bill on the 15-minute Belpex spot (the
        # card's "Belpex 15M" formula), so keep the native 15-minute
        # slots like Engie rather than the hourly mean.
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pdf_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
            quarter_hourly=True,
        )

    # Variable (Flexy): formula on page 4, indicative monthly rate on page 1.
    #   "Maandprijs: 11,81 11,81 11,81 11,81"
    # The four columns are (mono, peak, off-peak, exclusive_night) at the
    # same rate for every meter type today, so we surface them all.
    consumption = re.search(
        r"Verbruik[\s\S]+?Maandprijs:\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        text,
    )
    if not consumption:
        raise ExtractorError("Ecofix Flexy: consumption Maandprijs row not found")
    mono = to_float(consumption.group(1)) / 100.0
    peak = to_float(consumption.group(2)) / 100.0
    offpeak = to_float(consumption.group(3)) / 100.0
    excl = to_float(consumption.group(4)) / 100.0

    # Surface the BELPEX-RLP-M indexation formula as a diagnostic string
    # alongside the printed indicative rates. This is informational only
    # (no cross-check against the rates is performed); a miss just leaves
    # ``formula`` None.
    formula_match = re.search(
        rf"Enkelvoudige meter:\s*\(BELPEX-RLP-M\s*\*\s*([\d,]+)\)\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)",
        text,
    )
    formula_str: str | None = None
    if formula_match:
        formula_str = (
            f"(BELPEX-RLP-M * {formula_match.group(1)}) "
            f"{formula_match.group(2)} {formula_match.group(3)} c€/kWh ex-VAT"
        )

    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl,
        yearly_fixed_fee=yearly_fee,
        formula=formula_str,
    )


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates:
    """Parse the injection formula + indicative rate.

    Belgian residential injection is VAT-exempt, so the formula is
    surfaced as-is from the ex-VAT card values. Convention matches
    Cociter / OCTA+: factor scaled to per-EUR/kWh-spot.
    """
    if kind == "dynamic":
        # Anchor on the "Injectie" label rather than taking the second
        # Belpex 15M formula in document order, so a reordered card can't
        # bind the consumption formula to the injection role.
        inj_formula = _dynamic_formula_match(text, "Injectie")
        if inj_formula is None:
            # Every Ecofix dynamic card prints the injection Belpex 15M
            # formula; a miss is a layout drift. Raise rather than return
            # None (which the pipeline treats as a zero credit), and do
            # not fall back to the indicative alone, which would freeze
            # this spot-indexed injection at a flat rate. Matches the
            # Flexy branch and the dynamic consumption raise.
            raise ExtractorError("Ecofix dynamic injection: Belpex 15M formula missing")
        factor_pdf = to_float(inj_formula.group(1))
        base_pdf_cents = parse_sign(inj_formula.group(2)) * to_float(
            inj_formula.group(3)
        )
        # Injection indicative rate ("Injectie 4,83") sits next to the
        # formula; surfaced as ``current`` so consumers without a live
        # spot still get a plausible value.
        current_match = re.search(r"Injectie\s+([\d,]+)", text)
        current = to_float(current_match.group(1)) / 100.0 if current_match else None
        return InjectionRates(
            current=current,
            factor=factor_pdf * 10.0,
            base=base_pdf_cents / 100.0,
            formula=(
                f"({inj_formula.group(1)} x Belpex 15M) {inj_formula.group(2)} "
                f"{inj_formula.group(3)} c€/kWh ex-VAT"
            ),
        )

    # Flexy variable: injection settles on BELPEX-SPP-M, the solar-weighted
    # MONTHLY index, and the card says which month: "worden berekend op basis
    # van de index die van toepassing is tijdens de periode waarvoor je wordt
    # gefactureerd bij de afrekening van je reele verbruik en desgevallend
    # injectie". The printed Maandprijs is not that month's: on the Mei 2026
    # card 4,32 c/kWh inverts through the card's own formula to an index of
    # 54,52, which is MARCH's, two months back.
    #
    # The coefficients are surfaced with spp_indexed, which routes them to the
    # delivery month's own weighted mean and keeps them away from the hourly
    # spot. Emitting them without that flag is what the old comment feared,
    # and it was right to.
    current_block = re.search(
        r"Injectie[\s\S]+?Maandprijs:\s+([\d,]+)",
        text,
    )
    if not current_block:
        # Every Flexy card prints the monthly indicative; a miss is a
        # layout drift, not a fee-free contract. Fail loud rather than
        # emit a spot-shaped credit the pipeline cannot price.
        raise ExtractorError("Ecofix Flexy injection: monthly indicative missing")
    formula_match = re.search(
        rf"Injectie:\s*\(BELPEX-SPP-M\s*\*\s*([\d,]+)\)\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)",
        text,
    )
    factor: float | None = None
    base: float | None = None
    if formula_match is not None:
        # The card states c/kWh per EUR/MWh of index, so the factor carries a
        # x10 onto a EUR/kWh spot and the base a /100. Injection is VAT-exempt,
        # so neither is grossed.
        factor = to_float(formula_match.group(1)) * 10.0
        base = (
            parse_sign(formula_match.group(2))
            * to_float(formula_match.group(3))
            / 100.0
        )
    return InjectionRates(
        current=to_float(current_block.group(1)) / 100.0,
        factor=factor,
        base=base,
        spp_indexed=factor is not None,
        formula=formula_match.group(0) if formula_match else None,
    )


# ---- publication / validity --------------------------------------------------


def _extract_publication(text: str) -> tuple[str, date | None]:
    """Return (publication_label, valid_until_last_day_of_month).

    The card prints e.g. "Mei 2026" right under the product name. It has
    no validity-keyword anchor (``geldig`` / ``valable``) so the shared
    helper in ``_pdf.parse_valid_until`` would return ``None``; parse
    the Dutch month name + year directly. ``valid_until`` is the last
    day of that month so the binary sensor reflects monthly rotation.
    """
    # scan_month_end skips a shadowing edition marker ("... Versie 2026")
    # that the header prints above the month line, returning the first
    # real month token.
    d = scan_month_end(text, _DUTCH_MONTHS, limit=1000)
    if d is None:
        return "", None
    return f"{d.year}-{d.month:02d}", d


# ---- taxes ------------------------------------------------------------------


def _extract_federal_taxes(text: str) -> tuple[float, float]:
    """Return (federal_excise, energy_contribution) in EUR/kWh.

    The card's federal block prints residential excise across four kWh
    bands; the 0-3.000 kWh tier is what residential customers pay.
    Energy contribution (Energiebijdrage) is single-rate.
    """
    excise = re.search(r"Verbruik tussen 0\s*&\s*3\.000\s*kWh\s+([\d,]+)", text)
    contribution = re.search(r"Energiebijdrage\s+([\d,]+)", text)
    if excise is None:
        raise ExtractorError("Ecofix: federal excise (0-3.000 kWh) row not found")
    if contribution is None:
        raise ExtractorError("Ecofix: federal energy contribution row not found")
    return to_float(excise.group(1)) / 100.0, to_float(contribution.group(1)) / 100.0


def _extract_wallonia_connection_fee(text: str) -> float:
    # Called only for Wallonia, where the raccordement is mandatory; raise
    # on a miss rather than silently zero it (matching the federal block).
    match = re.search(r"Aansluitingsvergoeding\s+([\d,]+)", text)
    if match is None:
        raise ExtractorError("Ecofix: Wallonia connection fee not found")
    return to_float(match.group(1)) / 100.0


def _extract_wallonia_renewables(text: str) -> float:
    """Wallonia ``Bijdrage groene energie`` value.

    pdfplumber's row reconstruction can co-locate the bare value line
    with an unrelated left-column label (e.g. on Motion the right-side
    ``3,05`` lands on the same line as the left-side
    ``Verwachte jaarprijs:`` placeholder). Iterate lines after the WAL
    ``Bijdrage groene energie`` / ``(€ cent/kWh)`` anchor, skipping the
    consumption / injection / formula rows, and return the first
    remaining numeric token.
    """
    anchor = re.search(
        r"Wallonië[\s\S]+?Bijdrage groene energie[\s\S]+?\(€\s*cent/kWh\)",
        text,
    )
    if not anchor:
        raise ExtractorError(
            "Ecofix: Wallonia 'Bijdrage groene energie' anchor not found"
        )
    skip_prefixes = (
        "Afname",
        "Injectie",
        "Maandprijs",
        "Prijsformule",
        "Enkelvoudige",
        "Tweevoudige",
        "Uitsluitend",
    )
    stop_markers = ("Distributie", "Ecofix Digi", "Friends with benefits", "Netwerk")
    for raw_line in text[anchor.end() :].splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(line.startswith(p) for p in skip_prefixes):
            continue
        if any(marker in line for marker in stop_markers):
            break
        match = re.search(r"\b(\d+,\d+)\b", line)
        if match:
            return to_float(match.group(1)) / 100.0
    raise ExtractorError("Ecofix: Wallonia renewables value not found")


# ---- DSO row parsers --------------------------------------------------------


# Ecofix prints the eight Fluvius areas exactly as the shared map spells them,
# so alias it rather than restating it -- the spelling belongs to the card, and
# a supplier that abbreviates ("Fluvius Midden-Vl") keeps its own map instead.
# Same form luminus, mega, octaplus and ecopower already use.
_FLANDERS_LABELS = FLUVIUS_CARD_LABELS


def _extract_flanders_dsos(text: str, kind: TariffKind) -> dict[str, DsoOverlay]:
    """Read the Flanders Fluvius rows.

    pdfplumber places each digital-meter row on a single line:
        Fluvius Antwerpen 52,3679 5,35329 4,81301 18,92 18,92
    The five numbers are: capacity (€/kW/jaar), kWh-tarief total (c€/kWh),
    kWh-tarief excl. nacht (c€/kWh), data-mgmt per-kwartier (€/jaar),
    data-mgmt monthly/yearly (€/jaar). A handful of Fluvius West /
    Zenne-Dijle rows are line-broken between label and numbers; ``\\s+``
    matches the newline.

    ``kind`` selects the data-management column: dynamic contracts meter
    quarter-hourly (the per-kwartier column), Flexy meters monthly (the
    monthly/yearly column).

    A second analog-meter table appears below; its 5th column is the
    prosumer rate in €/jaar, which we attach as
    ``prosumer_eur_per_kva_year`` (analog-meter holdouts only).
    """
    out: dict[str, DsoOverlay] = {}
    digital_section = re.search(
        r"Vlaams gewest\s+Digitale meter([\s\S]+?)Vlaams gewest\s+Analoge meter",
        text,
    )
    analog_section = re.search(
        r"Vlaams gewest\s+Analoge meter([\s\S]+?)Ecofix Gas\s*&\s*Power",
        text,
    )
    if not digital_section:
        raise ExtractorError("Ecofix: Flanders 'Digitale meter' table not found")

    prosumer_by_key: dict[str, float] = {}
    if analog_section:
        prosumer_by_key = parse_prosumer_column(
            analog_section.group(1), _FLANDERS_LABELS
        )

    for label, key in _FLANDERS_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            digital_section.group(1),
        )
        if not row:
            continue
        capacity = to_float(row.group(1))
        kwh_total = to_float(row.group(2)) / 100.0
        kwh_excl_night = to_float(row.group(3)) / 100.0
        # The row carries two data-management columns: group 4 is the
        # per-kwartier (quarter-hourly) regime, group 5 the monthly/yearly
        # one. Bill the column matching the metering regime: dynamic
        # contracts read quarter-hourly (group 4), Flexy reads monthly
        # (group 5). They are equal today, so a single column was masking
        # the mismatch until Fluvius diverges the two regimes.
        data_mgmt_year = to_float(row.group(4 if kind == "dynamic" else 5))
        out[key] = DsoOverlay(
            distribution_single=kwh_total,
            distribution_exclusive_night=kwh_excl_night,
            transport=0.0,
            data_management_per_year=data_mgmt_year,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
        )
    return out


_WALLONIA_LABELS: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    ("WAVRE", DSO_REW),
    (r"TECTEO\s*-\s*RESA", DSO_RESA),
)
_ORES_PATTERN = re.compile(
    r"^ORES\s*\(([^)]+)\)\s+"
    + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
    + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
    + r"([\d.,]+)\s+([\d.,]+)\s*$",
    re.MULTILINE,
)


def _wallonia_row(label_pattern: str, text: str) -> tuple[float, ...] | None:
    row = re.search(
        rf"^{label_pattern}\s+"
        + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        + r"([\d.,]+)\s+([\d.,]+)\s*$",
        text,
        re.MULTILINE,
    )
    if not row:
        return None
    return tuple(to_float(g) for g in row.groups())


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Wallonia rows.

    Each Wallonian DSO row carries 10 numbers in this order:
        Enkelvoudig | Piek | Dal | PIC | MEDIUM | ECO |
        Excl. nacht | Jaarlijkse meteropname (€/jaar) |
        Prosumenten tarief (€/kWe/jaar) | Transport (c€/kWh)

    The card lists 9 ORES sub-areas (Brab. Wal., Est, Hainaut,
    Luxembourg, Mouscron, Namur, Verviers + Mouscron) — every row is
    numerically identical. ``_extract_ores`` collapses them to a single
    ``ores`` key and raises on numeric drift between rows so a future
    sub-area split doesn't silently bill at the first sub-area's rates.
    """
    out: dict[str, DsoOverlay] = {}

    # Non-ORES rows.
    for label_pattern, key in _WALLONIA_LABELS:
        nums = _wallonia_row(label_pattern, text)
        if nums is None:
            continue
        out[key] = _build_wallonia_overlay(nums)

    ores = _extract_ores(text)
    if ores is not None:
        out[DSO_ORES] = ores
    return out


def _extract_ores(text: str) -> DsoOverlay | None:
    rows = list(_ORES_PATTERN.finditer(text))
    if not rows:
        return None
    first = tuple(to_float(g) for g in rows[0].groups()[1:])
    for row in rows[1:]:
        following = tuple(to_float(g) for g in row.groups()[1:])
        if following != first:
            sub_area = row.group(1).strip()
            raise ExtractorError(
                f"Ecofix: ORES sub-area '{sub_area}' numbers diverged from "
                "the first ORES row; sub-area split needs an explicit DSO key"
            )
    return _build_wallonia_overlay(first)


def _build_wallonia_overlay(nums: tuple[float, ...]) -> DsoOverlay:
    (
        mono,
        peak,
        offpeak,
        pic,
        medium,
        eco,
        excl_night,
        terme_fixe,
        prosumer,
        transport,
    ) = nums
    return walloon_dso_overlay(
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


# ---- registry entry ---------------------------------------------------------


_ECOFIX_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    sweep_cost_s=10.1,
    id="ecofix",
    label="Ecofix",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_ECOFIX_REGIONS,
            # Flexy indexes injection on the monthly BELPEX-SPP-M, which its
            # variable energy leg fetches no spots for. The dynamic pair
            # collects the key through their own energy formula.
            spot_indexed_injection=c.kind != "dynamic",
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
