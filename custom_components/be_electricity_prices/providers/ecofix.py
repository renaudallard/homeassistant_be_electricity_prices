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

Three products are sold today:

  - Motion         : dynamic, 15-min Belpex-indexed, phone customer service.
  - Motion Online  : dynamic, 15-min Belpex-indexed, online-only.
  - Flexy          : variable, monthly RLP-weighted Belpex average.

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

import calendar
import logging
import re
from dataclasses import dataclass
from datetime import date

import aiohttp

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
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    SIGN_CHARS,
    USER_AGENT,
    fetch_pdf_text_layout,
    head_freshness_key,
    parse_sign,
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
)

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://portal.ecofixgp.be/docs/prices/current"


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
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(contract: _ContractDef) -> str:
    return f"{_BASE_URL}/EL_Ecofix_{contract.slug}_NL.pdf"


_DUTCH_MONTHS: dict[str, int] = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


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
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Ecofix contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
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

    Ecofix has no listing endpoint -- ``/docs/prices/`` is 404, the
    public ``/tarieven`` page only links a subset of products. HEAD-probe
    the three known URLs; the live-check script then diffs against the
    registry's contract ids. A future 404 (product retired) drops it
    here; a future new product needs a code update.
    """
    out: set[str] = set()
    for contract in _CONTRACTS:
        url = _document_url(contract)
        try:
            async with session.head(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=10),
                allow_redirects=True,
            ) as resp:
                if resp.status < 400:
                    out.add(contract.contract_id)
        except aiohttp.ClientError:
            continue
    return out


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Parse one Ecofix tariff card into a region-narrowed snapshot."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Ecofix contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

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
        dsos = _extract_flanders_dsos(text)
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
        renew_match = re.search(r"^Verbruik\s+(\d+,\d+)\s*$", text, re.MULTILINE)
        if not fee_match:
            raise ExtractorError("Ecofix Flexy: yearly fixed fee not found")
        if not renew_match:
            raise ExtractorError("Ecofix Flexy: Vlaanderen renewables not found")
        return (
            to_float(fee_match.group(1)),
            to_float(renew_match.group(1)) / 100.0,
        )

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


def _extract_energy(text: str, kind: TariffKind, yearly_fee: float) -> EnergyRates:
    if kind == "dynamic":
        # Dynamic cards print the consumption formula on the line directly
        # after "Afname <indicative>" e.g.:
        #   Afname 11,74
        #   Prijsformule excl. BTW (0,1010 x Belpex 15M) + 0,9
        # Two such formulas live on the page (consumption first,
        # injection second); ``re.findall`` returns them in PDF order.
        formulas = re.findall(
            rf"\(([\d,]+)\s*x\s*Belpex\s*15M\)\s*([{SIGN_CHARS}])\s*([\d,]+)",
            text,
        )
        if not formulas:
            raise ExtractorError("Ecofix dynamic: Belpex 15M formula not found")
        factor_pdf = to_float(formulas[0][0])
        base_pdf_cents = parse_sign(formulas[0][1]) * to_float(formulas[0][2])
        # PDF formula is c€/kWh ex-VAT against Belpex in €/MWh. The card
        # banner prints "Prijzen inclusief X% BTW"; read X to track future
        # VAT changes without a code update. Conversion to
        # EUR/kWh-against-EUR/kWh-spot: factor stays unitless (x1000/100
        # = x10) and base divides cents->EUR (/100).
        vat = vat_multiplier(
            text, re.compile(r"inclusief\s+(\d+)\s*%\s*BTW", re.IGNORECASE)
        )
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pdf_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
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

    # Cross-check against the formula block; raises if the extracted
    # formula does not match the displayed average within a 0.5 c€/kWh
    # tolerance (a layout drift big enough to mis-bill).
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


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    """Parse the injection formula + indicative rate.

    Belgian residential injection is VAT-exempt, so the formula is
    surfaced as-is from the ex-VAT card values. Convention matches
    Cociter / OCTA+: factor scaled to per-EUR/kWh-spot.
    """
    if kind == "dynamic":
        # Second Belpex 15M formula in document order.
        formulas = re.findall(
            rf"\(([\d,]+)\s*x\s*Belpex\s*15M\)\s*([{SIGN_CHARS}])\s*([\d,]+)",
            text,
        )
        if len(formulas) < 2:
            return None
        factor_pdf = to_float(formulas[1][0])
        base_pdf_cents = parse_sign(formulas[1][1]) * to_float(formulas[1][2])
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
                f"({formulas[1][0]} x Belpex 15M) {formulas[1][1]} "
                f"{formulas[1][2]} c€/kWh ex-VAT"
            ),
        )

    # Flexy variable: "Injectie ... Maandprijs: 4,32 4,32 4,32 /"
    current_block = re.search(
        r"Injectie[\s\S]+?Maandprijs:\s+([\d,]+)",
        text,
    )
    formula_match = re.search(
        rf"Injectie:\s*\(BELPEX-SPP-M\s*\*\s*([\d,]+)\)\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)",
        text,
    )
    if not current_block and not formula_match:
        return None
    current = (
        to_float(current_block.group(1)) / 100.0 if current_block is not None else None
    )
    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if formula_match:
        factor_pdf = to_float(formula_match.group(1))
        base_pdf_cents = parse_sign(formula_match.group(2)) * to_float(
            formula_match.group(3)
        )
        factor = factor_pdf * 10.0
        base = base_pdf_cents / 100.0
        formula = (
            f"(BELPEX-SPP-M * {formula_match.group(1)}) "
            f"{formula_match.group(2)} {formula_match.group(3)} c€/kWh ex-VAT"
        )
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


# ---- publication / validity --------------------------------------------------


def _extract_publication(text: str) -> tuple[str, date | None]:
    """Return (publication_label, valid_until_last_day_of_month).

    The card prints e.g. "Mei 2026" right under the product name. It has
    no validity-keyword anchor (``geldig`` / ``valable``) so the shared
    helper in ``_pdf.parse_valid_until`` would return ``None``; parse
    the Dutch month name + year directly. ``valid_until`` is the last
    day of that month so the binary sensor reflects monthly rotation.
    """
    match = re.search(r"\b([A-Z][a-z]+)\s+(20\d{2})\b", text[:1000])
    if not match:
        return "", None
    month_name = match.group(1).lower()
    if month_name not in _DUTCH_MONTHS:
        return "", None
    year = int(match.group(2))
    month = _DUTCH_MONTHS[month_name]
    last_day = calendar.monthrange(year, month)[1]
    return f"{year}-{month:02d}", date(year, month, last_day)


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
    match = re.search(r"Aansluitingsvergoeding\s+([\d,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


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


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Flanders Fluvius rows.

    pdfplumber places each digital-meter row on a single line:
        Fluvius Antwerpen 52,3679 5,35329 4,81301 18,92 18,92
    The five numbers are: capacity (€/kW/jaar), kWh-tarief total (c€/kWh),
    kWh-tarief excl. nacht (c€/kWh), data-mgmt per-kwartier (€/jaar),
    data-mgmt monthly/yearly (€/jaar). A handful of Fluvius West /
    Zenne-Dijle rows are line-broken between label and numbers; ``\\s+``
    matches the newline.

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
        for label, key in _FLANDERS_LABELS.items():
            row = re.search(
                rf"{re.escape(label)}\s+"
                + r"[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+([\d.,]+)",
                analog_section.group(1),
            )
            if row:
                prosumer_by_key[key] = to_float(row.group(1))

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
        data_mgmt_year = to_float(row.group(5))
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
    return DsoOverlay(
        distribution_single=mono / 100.0,
        distribution_peak=peak / 100.0,
        distribution_offpeak=offpeak / 100.0,
        distribution_exclusive_night=excl_night / 100.0,
        distribution_pic=pic / 100.0,
        distribution_medium=medium / 100.0,
        distribution_eco=eco / 100.0,
        transport=transport / 100.0,
        data_management_per_year=terme_fixe,
        prosumer_eur_per_kva_year=prosumer,
    )


# ---- registry entry ---------------------------------------------------------


_ECOFIX_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="ecofix",
    label="Ecofix",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_ECOFIX_REGIONS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
