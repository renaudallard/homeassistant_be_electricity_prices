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

"""OCTA+ Belgium tariff card extractor.

OCTA+ publishes residential electricity tariff cards at stable URLs:

    https://files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<WL|VL>_FR.pdf

OCTA+ only sells residential electricity in Wallonia and Flanders today
(the Brussels offers visible on their site are professional-only).

The PDFs visually present a clean energy + DSO + tax table, but pdfplumber's
default text extractor returns the DSO block in column-major order. The
extractor uses :func:`fetch_pdf_text_aligned` which reassembles each
visual row from word coordinates, giving e.g. ``AIEG 10,87 12,05 ...``
on a single line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    fetch_pdf_text_aligned,
    fetch_text,
    head_freshness_key,
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
)

_BASE_URL = "https://files.octaplus.be/tariffs"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WL",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # OCTA+'s product slug in the URL


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("octaplus_fixed", "OCTA+ Fixed", "fixed", "FIXED"),
    _ContractDef("octaplus_ecofixed", "OCTA+ Eco Fixed", "fixed", "ECOFIXED"),
    _ContractDef(
        "octaplus_smartvariable",
        "OCTA+ Smart Variable",
        "variable",
        "SMARTVARIABLE",
    ),
    _ContractDef("octaplus_flux", "OCTA+ Flux", "variable", "FLUX"),
    _ContractDef("octaplus_ecoflux", "OCTA+ Eco Flux", "variable", "ECOFLUX"),
    _ContractDef("octaplus_dynamic", "OCTA+ Dynamic", "dynamic", "DYNAMIC"),
    _ContractDef("octaplus_ecodynamic", "OCTA+ Eco Dynamic", "dynamic", "ECODYNAMIC"),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


_LISTING_URL = "https://www.octaplus.be/fr/electricite-gaz-naturel/tarifs"


def _document_url(contract: _ContractDef, region: str) -> str:
    return f"{_BASE_URL}/E_OCTA_{contract.slug}_RE_{_REGION_TO_CODE[region]}_FR.pdf"


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: HEAD the per-(contract, region) PDF.

    OCTA+ overwrites its tariff cards in place under stable filenames,
    so the file's Last-Modified header is the right freshness signal.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in _REGION_TO_CODE:
        return None
    return await head_freshness_key(session, _document_url(contract, region))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every residential electricity slug from OCTA+'s tarifs page.

    The listing links each card directly with the URL pattern
    ``E_OCTA_<SLUG>_RE_(VL|WL)_FR.pdf``. live_check diffs against
    ``{c.slug for c in _CONTRACTS}``.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return set(re.findall(r"E_OCTA_([A-Z]+)_RE_(?:VL|WL)_FR\.pdf", html))


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown OCTA+ contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    if region not in _REGION_TO_CODE:
        raise ExtractorError(f"OCTA+ {contract_id}: not available in region {region!r}")
    url = _document_url(contract, region)
    # 1.0pt threshold collapses OCTA+'s heavy character spacing in the
    # tax block ("5 ,0 3 2 9 0 ,2 0 4 2" -> "5,0329 0,2042") while
    # still keeping real word spacing intact.
    text = await fetch_pdf_text_aligned(session, url, x_join_threshold=1.0)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown OCTA+ contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, region_connection_fee = _extract_taxes(
        text, region
    )
    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text)
    else:
        wallonia_renewables = _extract_wallonia_renewables(text)
        dsos = _extract_wallonia_dsos(text)

    return SupplierSnapshot(
        supplier="octaplus",
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
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


def _extract_yearly_fee(text: str) -> float:
    match = re.search(r"Redevance fixe \(€/an\)\s+([\d.,]+)", text)
    return to_float(match.group(1)) if match else 0.0


def _vat_multiplier(text: str) -> float:
    """Read the VAT % from the card header ('Tarifs 6% TVAC')."""
    return vat_multiplier(text, r"Tarifs\s+(\d+(?:[.,]\d+)?)\s*%\s*TVAC")


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        # Dynamic cards bury the consumption formula in prose, e.g.:
        #   "La formule tarifaire HTVA (en €/MWh) est la suivante:
        #    Epex 15' * 1,083 + 4,17"
        # The first Epex 15' formula on the card is the consumption one;
        # the injection formula appears later, after "Le prix de votre
        # injection est indexé". Match the first occurrence.
        formula = re.search(
            rf"Epex\s*15\s*'?\s*\*\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}])\s*(\d+(?:[.,]\d+)?)",
            text,
        )
        if not formula:
            raise ExtractorError("could not parse OCTA+ dynamic formula")
        factor_pdf = to_float(formula.group(1))
        base_pdf_eur_mwh = parse_sign(formula.group(2)) * to_float(formula.group(3))
        vat = _vat_multiplier(text)
        # Formula is HTVA; the rest of the snapshot is TVAC, so apply
        # the parsed VAT multiplier. spot in our model is EUR/kWh so:
        #   factor_eur_kwh = factor_pdf * vat
        #   base_eur_kwh   = base_eur_mwh / 1000 * vat
        return DynamicRates(
            factor=factor_pdf * vat,
            base=base_pdf_eur_mwh / 1000.0 * vat,
            yearly_fixed_fee=yearly_fee,
        )

    # Static / variable: the energy table prints values column-major
    # so the aligned helper gives clean rows like:
    #   "Compteur monohoraire 15,86 4,72"
    #   "Compteur Heures pleines 18,67 4,72"  (or "Heures pleines 18,67 4,72")
    #   "Heures creuses 13,77 4,72"
    #   "Compteur exclusif nuit 14,85 -"
    mono = _meter_value(text, r"Compteur monohoraire")
    peak = _meter_value(text, r"Heures pleines")
    offpeak = _meter_value(text, r"Heures creuses")
    excl = _meter_value(text, r"Compteur exclusif nuit")
    if mono is None:
        raise ExtractorError(f"could not parse OCTA+ {kind} energy block")
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl,
        yearly_fixed_fee=yearly_fee,
    )


def _meter_value(text: str, label_pattern: str) -> float | None:
    match = re.search(rf"{label_pattern}\s+([\d.,]+)", text)
    if not match:
        return None
    return to_float(match.group(1)) / 100.0


def _extract_publication_month(text: str) -> str:
    match = re.search(r"-\s*(\d{1,2})/(\d{4})\s*-", text)
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Injection rate sits next to the consumption rate on the
    # 'Compteur monohoraire' line.
    match = re.search(r"Compteur monohoraire\s+[\d.,]+\s+([\d.,]+)", text)
    current = to_float(match.group(1)) / 100.0 if match else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if kind == "dynamic":
        # Injection formula appears after the prose
        # "Le prix de votre injection est indexé ..."
        # so we anchor on that lead-in to skip the consumption formula.
        inj = re.search(
            rf"Le\s+prix\s+de\s+votre\s+injection.*?"
            rf"Epex\s*15\s*'?\s*\*\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}])\s*(\d+(?:[.,]\d+)?)",
            text,
            re.S,
        )
        if inj is not None:
            f_pdf = to_float(inj.group(1))
            b_eur_mwh = parse_sign(inj.group(2)) * to_float(inj.group(3))
            factor = f_pdf  # injection is VAT-exempt
            base = b_eur_mwh / 1000.0
            formula = inj.group(0)

    if current is None and factor is None:
        return None
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


# ---- taxes --------------------------------------------------------------------


def _extract_taxes(text: str, region: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, region_connection_fee).

    OCTA+ prints four federal-tax tier rows on the second page:

      ``Consommation entre 0 & 3.000 kWh 5,0329 0,2042``

    The first tier (0-3.000 kWh) is the residential one we surface.
    Wallonia adds a one-line connection fee (``Redevance raccordement
    Wallonie (c€/kWh) 0,075``).
    """
    federal_excise = 0.0
    energy_contribution = 0.0
    # Anchor on the kWh range; the leading "Consommation" word can be
    # mangled on Flanders cards where the federal column shares its row
    # bucket with the Fonds Energie sidebar (e.g. "CCCConsommaaaation").
    tier1 = re.search(
        r"0\s*&\s*3\.000\s*kWh\s+([\d.,]+)\s+([\d.,]+)",
        text,
    )
    if tier1:
        federal_excise = to_float(tier1.group(1)) / 100.0
        energy_contribution = to_float(tier1.group(2)) / 100.0

    region_connection_fee = 0.0
    if region == REGION_WALLONIA:
        fee = re.search(
            r"Redevance\s+raccordement\s+Wallonie[^0-9]*([\d.,]+)",
            text,
        )
        if fee:
            region_connection_fee = to_float(fee.group(1)) / 100.0
    return federal_excise, energy_contribution, region_connection_fee


def _extract_wallonia_renewables(text: str) -> float:
    # Some cards (Smart Variable) put the value several lines below the
    # "Coûts énergie verte" header; anchor instead on "Région wallonne",
    # whose first numeric neighbour is always the green-energy rate.
    match = re.search(r"Région\s+wallonne[^\d]*?([\d.,]+)", text, re.S)
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_flanders_renewables(text: str) -> float:
    """Flanders cards split renewables across two rows:
    ``Coûts énergie verte`` and ``Coûts cogénération``.
    """
    green = re.search(r"Coûts énergie verte\s+(\d+(?:[.,]\d+)?)", text)
    cogen = re.search(r"Coûts cogénération\s+(\d+(?:[.,]\d+)?)", text)
    total = 0.0
    if green:
        total += to_float(green.group(1))
    if cogen:
        total += to_float(cogen.group(1))
    return total / 100.0


# ---- DSO row parsers ----------------------------------------------------------


_WALLONIA_LABELS: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    # Eight ORES sub-areas share the same tariff line; match the first.
    # ``ORES`` may or may not have a space before the opening paren
    # depending on which OCTA+ card we hit.
    (r"ORES\s*\(", DSO_ORES),
    (r"TECTEO\s*-\s*RESA", DSO_RESA),
    # Different OCTA+ cards print this either as "REGIEDEWAVRE",
    # "REGIE DE WAVRE", or other spacing combinations.
    (r"REGIE\s*DE\s*WAVRE", DSO_REW),
)


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Wallonia rows (10 numbers each) in the aligned output:

    mono | jour | nuit | PIC | MEDIUM | ECO | excl_nuit | terme_fixe
    (€/an) | prosumer (€/kVA/an) | transport (c€/kWh)
    """
    out: dict[str, DsoOverlay] = {}
    for pattern, key in _WALLONIA_LABELS:
        match = re.search(
            rf"{pattern}[^\n]*?"
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
        pic = to_float(match.group(4))
        medium = to_float(match.group(5))
        eco = to_float(match.group(6))
        excl_night = to_float(match.group(7))
        terme_fixe = to_float(match.group(8))
        prosumer = to_float(match.group(9))
        transport = to_float(match.group(10))
        out[key] = DsoOverlay(
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
    return out


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
    """Flanders rows. The digital-meter row carries:

      dist_normal | dist_excl_night | data_mgmt_qh (€/an) |
      data_mgmt_year (€/an) | capacity (€/kW/yr) | - | -

    A second analog-meter row carries the prosumer rate as the last
    column. Some cards (Dynamic Flanders) prepend extra column-header
    glyphs to one digital row and strip the leading 'F' from the rest,
    so the digital regex is anchored on the unique sub-area suffix
    rather than the full ``Fluvius X`` label and tolerates multi-glyph
    cell separators (``-------- --------``).
    """
    prosumer_by_key: dict[str, float] = {}
    # Second row: 'Fluvius X 8,09 7,55 18,92 - - 130,92 54,63'
    for m in re.finditer(
        r"^(Fluvius [^\n]+?)\s+"
        + r"[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+-+\s+-+\s+[\d.,]+\s+([\d.,]+)\s*$",
        text,
        re.MULTILINE,
    ):
        label = m.group(1).strip()
        if label in _FLANDERS_LABELS:
            prosumer_by_key[_FLANDERS_LABELS[label]] = to_float(m.group(2))

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        # Strip the "Fluvius " prefix so a digital row labelled "luvius
        # Halle-Vilvoorde" still matches against the suffix.
        suffix = label.removeprefix("Fluvius ")
        match = re.search(
            rf"{re.escape(suffix)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
            + r"(?:\s+-+\s+-+)?",
            text,
        )
        if not match:
            continue
        dist_normal = to_float(match.group(1))
        dist_excl_night = to_float(match.group(2))
        data_mgmt_year = to_float(match.group(4))
        capacity = to_float(match.group(5))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            distribution_exclusive_night=dist_excl_night / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt_year,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
        )
    return out


_OCTAPLUS_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="octaplus",
    label="OCTA+",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_OCTAPLUS_REGIONS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
