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

"""TotalEnergies Belgium tariff card extractor.

TotalEnergies publishes the current month's tariff card per (product,
region) at stable URLs:

    https://totalenergies.be/static/marketing-documents/b2c/tariff-card/
        latest/<PRODUCT>_ELECTRICITY_<REGION>_FR.pdf

The ``/latest/`` segment auto-rolls each month so no listing scrape is
needed. All nine residential electricity products are registered. Each
is available in V/W/B (TotalEnergies serves all three regions).

The PDFs include rotated DSO / tax columns that pypdf cannot extract
('Rotated text discovered. Output will be incomplete.'). The extractor
uses pdfplumber for the layout-aware extraction it needs to read those
cells; the other extractors keep using pypdf since their cards are
horizontal-text-only.

Dynamic formula format: ``0.1034 * BELPEXH + 1.75`` (HTVA, c€/kWh).
The parser scales factor and base by the parsed VAT multiplier - same
pattern as Engie/Luminus.
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
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    SIGN_CHARS,
    fetch_pdf_text_layout,
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

_BASE_URL = "https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WAL",
    REGION_BRUSSELS: "BXL",
}


_ALL_REGIONS: frozenset[str] = frozenset(
    {REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS}
)


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # the file prefix in TotalEnergies's URL
    # Regions the product is actually published in. TotalEnergies's
    # listing page advertises every product in V/W/B but a few only
    # have a Wallonia PDF; the others return a 200 OK HTML 404 page.
    regions: frozenset[str] = _ALL_REGIONS


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "totalenergies_electricite_fixe",
        "TotalEnergies Electricité Fixe",
        "fixed",
        "ELECTRICITE-FIXE",
    ),
    _ContractDef(
        "totalenergies_electricite_variable",
        "TotalEnergies Electricité Variable",
        "variable",
        "ELECTRICITE-VARIABLE",
    ),
    _ContractDef(
        "totalenergies_impact",
        "TotalEnergies Impact",
        "variable",
        "IMPACT",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef(
        "totalenergies_mycomfort",
        "TotalEnergies myComfort",
        "variable",
        "MYCOMFORT",
    ),
    _ContractDef(
        "totalenergies_mycomfort_fixed",
        "TotalEnergies myComfort Fixe",
        "fixed",
        "MYCOMFORT-FIXED",
    ),
    _ContractDef(
        "totalenergies_mydrive",
        "TotalEnergies myDrive",
        "variable",
        "MYDRIVE",
    ),
    _ContractDef(
        "totalenergies_mydynamic",
        "TotalEnergies myDynamic",
        "dynamic",
        "MYDYNAMIC",
    ),
    _ContractDef(
        "totalenergies_myessential",
        "TotalEnergies myEssential",
        "variable",
        "MYESSENTIAL",
    ),
    _ContractDef(
        "totalenergies_myessential_fixed",
        "TotalEnergies myEssential Fixe",
        "fixed",
        "MYESSENTIAL-FIXED",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


_LISTING_URL = (
    "https://totalenergies.be/fr/particuliers/electricite-et-gaz/cartes-tarifaires"
)


def _document_url(slug: str, region: str) -> str:
    region_code = _REGION_TO_CODE[region]
    return f"{_BASE_URL}/{slug}_ELECTRICITY_{region_code}_FR.pdf"


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: HEAD the per-(contract, region) PDF.

    TotalEnergies serves every card under ``/tariff-card/latest/<SLUG>_...``
    and overwrites in place, so the file's Last-Modified header is the
    right freshness signal.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if (
        contract is None
        or region not in _REGION_TO_CODE
        or region not in contract.regions
    ):
        return None
    return await head_freshness_key(session, _document_url(contract.slug, region))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every electricity-product slug from the cartes-tarifaires page.

    The listing page links each card as
    ``tariff-card/latest/<SLUG>_ELECTRICITY_<REGION>_FR.pdf``. Strip
    the regulated TARIFF_SOCIAL entry (not a residential-market product
    and excluded from the registry). live_check diffs the result
    against ``{c.slug for c in _CONTRACTS}``.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return {
        slug
        for slug in re.findall(
            r"tariff-card/latest/([A-Z0-9\-]+)_ELECTRICITY_(?:VL|WAL|BXL)_FR",
            html,
        )
        if slug != "TARIFF_SOCIAL"
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown TotalEnergies contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    if region not in _REGION_TO_CODE:
        raise ExtractorError(f"TotalEnergies: unknown region {region!r}")
    if region not in contract.regions:
        raise ExtractorError(
            f"TotalEnergies {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown TotalEnergies contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise = _extract_federal_excise(text)
    energy_contribution = _extract_energy_contribution(text)
    region_connection_fee = (
        _extract_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_renewables(text, "Flandre")
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        wallonia_renewables = _extract_renewables(text, "Wallonie")
        dsos = _extract_wallonia_dsos(text)
    else:
        brussels_renewables = _extract_renewables(text, "Bruxelles")
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="totalenergies",
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
            vat_rate=0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


# Brussels Dynamic prints "<factor> * BELPEXH +" on one line and the bases
# on the next: "0.1034 * BELPEXH + ... + Formule tarifaire\n3.85 3.85 ...".
# _resolve_consumption_formula handles both the same-line and split-line
# layouts via these two patterns.
_FACTOR_ONLY_RE = re.compile(rf"([\d.,]+)\s*\*\s*BELPEXH\s*([{SIGN_CHARS}])")
_BASE_AFTER_FORMULE_RE = re.compile(r"Formule tarifaire\s*\n\s*([\d.,]+)")


def _resolve_consumption_formula(text: str) -> tuple[float, float, float] | None:
    """Return ``(factor, sign, base_cents)`` for the consumption formula.

    The consumption formula always appears before the injection formula
    in TotalEnergies's PDFs, so the FIRST ``factor * BELPEXH`` match is
    always the consumption one. Wallonia and Flanders print the base on
    the same line (``0.1034 * BELPEXH + 1.75``); Brussels splits the
    formula across two lines (``0.1034 * BELPEXH +`` then ``3.85`` after
    ``Formule tarifaire``).
    """
    first_match = _FACTOR_ONLY_RE.search(text)
    if first_match is None:
        return None
    factor = to_float(first_match.group(1))
    sign = parse_sign(first_match.group(2))

    # Same-line base: a complete number, terminated by whitespace or
    # end-of-string, that is NOT followed by another ``* BELPEXH``
    # (which would be the next column's formula). The trailing
    # ``(?=\s|$)`` blocks the regex engine from backing off ``[\d.,]+``
    # to a shorter match (e.g. capturing ``0.103`` out of ``0.1034``).
    tail_re = re.compile(
        re.escape(first_match.group(0)) + r"\s*([\d.,]+)(?=\s|$)(?!\s*\*\s*BELPEXH)"
    )
    tail = tail_re.search(text)
    if tail is not None:
        return factor, sign, to_float(tail.group(1))

    after_formule = _BASE_AFTER_FORMULE_RE.search(text)
    if after_formule is None:
        return None
    return factor, sign, to_float(after_formule.group(1))


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(text, r"TVA\s*(\d+)\s*%")


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        consumption = _resolve_consumption_formula(text)
        if consumption is None:
            raise ExtractorError("could not parse TotalEnergies dynamic formula")
        factor_pdf, sign, base_pre_vat_cents_value = consumption
        vat = _vat_multiplier(text)
        # PDF formula yields c€/kWh (HTVA) from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=sign * base_pre_vat_cents_value * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
        )

    # Static / variable: the consumption row is 4 space-separated values
    # (mono / jour / nuit / excl_nuit). The layout drifts per contract:
    # asterisk count after "Consommation" varies (0-3); for static the
    # values follow directly, for variable a "Tarif mensuel" label sits
    # between. One regex covers all cases.
    consumption_match = re.search(
        r"Consommation\*{0,5}\s*\n(?:\s*Tarif\s+(?:annuel|mensuel)\s*\n)?\s*"
        r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
        text,
    )
    if not consumption_match:
        raise ExtractorError(f"could not parse TotalEnergies {kind} consumption block")
    mono = to_float(consumption_match.group(1)) / 100.0
    peak = to_float(consumption_match.group(2)) / 100.0
    offpeak = to_float(consumption_match.group(3)) / 100.0
    excl_night = to_float(consumption_match.group(4)) / 100.0
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=yearly_fee,
        )
    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_fee_and_renewables(text: str) -> tuple[float, float]:
    """Pull the (yearly_fee_eur, renewables_eur_per_kwh) pair.

    TotalEnergies prints them on a dedicated 2-number line in the energy
    block: ``90,00 1,57``. The position varies per contract (after the
    consumption row for variable/dynamic, between Tarif annuel and
    Injection for static), but every layout precedes the line with a
    ``Tarif (mensuel|annuel)`` header. Anchor on that header and require
    the following 2-number line; this rejects unrelated value pairs that
    happen to share the shape (e.g. footer rows).

    Both numbers are mandatory on every TE residential card (~90 EUR/yr
    yearly fee; regional renewables surcharge between 1.6 and 3.2
    c€/kWh). Raise on miss so a layout drift surfaces as an extractor
    failure instead of silently dropping ~90 EUR/year and the regional
    renewables levy from the bill.
    """
    match = re.search(
        r"Tarif\s+(?:mensuel|annuel)[\s\S]{0,400}?"
        r"^(\d{2,3}[.,]\d{2})\s+(\d[.,]\d{1,3})\s*$",
        text,
        re.MULTILINE,
    )
    if not match:
        raise ExtractorError("TotalEnergies: yearly fee + renewables row not found")
    return to_float(match.group(1)), to_float(match.group(2)) / 100.0


def _extract_yearly_fee(text: str) -> float:
    fee, _ = _extract_fee_and_renewables(text)
    return fee


def _extract_publication_month(text: str) -> str:
    match = re.search(
        r"TotalEnergies\s+(?:my\w+|Electricit[eé]\w*|Impact)[^\n]*\n([a-zéûÉ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    indicative = re.search(
        r"Injection\*{0,5}[^\n]*\n\s*([\d.,]+)",
        text,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if kind == "dynamic":
        # Injection block always prints the formula cleanly on one line
        # ("0.1 * BELPEXH -1.3 ..."). Anchor the search after "Injection"
        # so the consumption formula above can never be picked up.
        match = re.search(
            rf"Injection\*{{0,5}}[^\n]*\n[^\n]*\n\s*([\d.,]+)\s*\*\s*BELPEXH\s*"
            rf"([{SIGN_CHARS}])\s*([\d.,]+)",
            text,
        )
        if match is not None:
            f_pdf = to_float(match.group(1))
            b_cents = parse_sign(match.group(2)) * to_float(match.group(3))
            # Injection is VAT-exempt residential.
            factor = f_pdf * 10.0
            base = b_cents / 100.0
            formula = match.group(0)

    if current is None and factor is None:
        return None
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


# ---- taxes --------------------------------------------------------------------


def _extract_federal_excise(text: str) -> float:
    """First excise tier (0-3000 kWh)."""
    match = re.search(
        r"Consommation entre 0 et 3\.000 kWh\s+([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_energy_contribution(text: str) -> float:
    match = re.search(r"Cotisation sur l[\"'’]\s*énergie\s+([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_connection_fee(text: str) -> float:
    match = re.search(r"Redevance de raccordement\s+([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_energy_fund(text: str) -> float:
    """Flanders 'Cotisations Fonds Energie' line, principal-with-domicile entry."""
    match = re.search(
        r"Résidence principale\s+sans\s+tarif\s+social\s+([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) if match else 0.0


def _extract_renewables(text: str, region_label: str) -> float:
    """The renewables value is the second number on the fee+renewables line.

    Each PDF is region-specific so the label is informational only - we
    just pick the value next to the yearly fee.
    """
    del region_label
    _, renewables = _extract_fee_and_renewables(text)
    return renewables


# ---- DSO row parsers ----------------------------------------------------------


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
    """Flanders Fluvius rows (9 numbers each).

    Layout:
      dist_digital_mono | capacity_digital | dist_classic_mono |
      dist_classic_excl_night | data_mgmt_classic | data_mgmt_digital |
      tarif_capacity_max | cotisation_energie | prosumer

    Distribution already includes transport (same convention as
    Engie/Luminus/Mega Flanders).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            rf"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            rf"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        dist_digital = to_float(match.group(1))
        capacity = to_float(match.group(2))
        data_mgmt = to_float(match.group(6))  # digital meter column
        prosumer = to_float(match.group(9))
        out[key] = DsoOverlay(
            distribution_single=dist_digital / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Namur - Namen)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "RESA SA": DSO_RESA,
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Wallonia rows (12 numbers each).

    Layout:
      mono | jour | nuit | excl_nuit | PIC | MEDIUM | ECO |
      terme_fixe (€/an) | transport (c€/kWh) | prosumer (€/kVA/an) |
      cap_base | cap_supplementary
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            rf"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            rf"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            rf"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
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
        terme_fixe = to_float(match.group(8))
        transport = to_float(match.group(9))
        prosumer = to_float(match.group(10))
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


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Brussels Sibelga row (7 numbers).

    Layout: mono | jour | nuit | excl_nuit | mesure_comptage (€/an) |
            transport (c€/kWh) | cotisation_energie (c€/kWh)
    """
    match = re.search(
        r"SIBELGA\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
        text,
    )
    if not match:
        return {}
    mono = to_float(match.group(1))
    peak = to_float(match.group(2))
    offpeak = to_float(match.group(3))
    excl_night = to_float(match.group(4))
    mesure = to_float(match.group(5))
    transport = to_float(match.group(6))
    return {
        DSO_SIBELGA: DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            transport=transport / 100.0,
            data_management_per_year=mesure,
        )
    }


EXTRACTOR = SupplierExtractor(
    id="totalenergies",
    label="TotalEnergies",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
