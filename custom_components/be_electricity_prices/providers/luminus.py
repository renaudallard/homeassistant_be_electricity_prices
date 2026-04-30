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

"""Luminus Belgium tariff card extractor.

Luminus publishes the current month's tariff card per (product, region)
through a public REST endpoint:

    https://www.luminus.be/api-next/get-pricelist/
        ?documentSlug=<slug>&energyType=electricity&language=fr
        &tabValue=<Wallonia|Flanders>

Each request returns a fresh PDF (e.g. April 2026 -> 202604 in the
filename). Luminus only sells residential market products in Flanders
and Wallonia; Brussels carries only the regulated Social tariff which
this extractor does not include (auto-assigned, no DSO breakdown).

Energy prices, distribution rows and renewables surcharges all vary
between V and W on every product, so the extractor fetches exactly the
configured region's PDF and never merges. Prices are 6% VAT inclusive
in the printed values; the Dynamic formula is hors TVA so factor and
base are scaled by the parsed VAT multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from ..const import REGION_FLANDERS, REGION_WALLONIA
from ._pdf import USER_AGENT, fetch_pdf_text, to_float
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

_API_URL = "https://www.luminus.be/api-next/get-pricelist/"

_REGION_TO_TAB: dict[str, str] = {
    REGION_FLANDERS: "Flanders",
    REGION_WALLONIA: "Wallonia",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # Luminus's documentSlug query parameter


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("luminus_comfy", "Luminus Comfy", "fixed", "comfy"),
    _ContractDef("luminus_comfy_plus", "Luminus Comfy+", "fixed", "comfy-plus"),
    _ContractDef("luminus_comfyflex", "Luminus ComfyFlex", "variable", "comfyflex"),
    _ContractDef("luminus_maxxfix", "Luminus MaxxFix", "fixed", "maxxfix"),
    _ContractDef("luminus_basicfix", "Luminus BasicFix", "fixed", "basicfix"),
    _ContractDef("luminus_basicflex", "Luminus BasicFlex", "variable", "basicflex"),
    _ContractDef("luminus_dynamic", "Luminus Dynamic", "dynamic", "dynamic"),
    # Luminus Sociaal/Social (regulated CREG tariff) is omitted on purpose:
    # it is auto-assigned to protected customers (not user-selectable) and
    # its PDF carries an all-in regulated price with no DSO breakdown -
    # same reasoning as Engie's Tarif Social.
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(slug: str, region: str) -> str:
    tab = _REGION_TO_TAB[region]
    return (
        f"{_API_URL}?documentSlug={slug}&energyType=electricity"
        f"&language=fr&tabValue={tab}"
    )


_SITEMAP_URL = "https://www.luminus.be/sitemap.xml"

# Luminus's sitemap exposes one product page per slug under the
# tariffs root, e.g. /fr/particuliers/tarifs-energie/comfyflex/.
_PRODUCT_PAGE_RE = re.compile(
    r"/(?:fr|nl)/particuliers/(?:tarifs-energie|onze-tarieven)/([a-z0-9\-]+)/"
)

# Excluded slugs: regulated tariffs not offered on the residential
# market, plus the parent index pages.
_EXCLUDED_SLUGS = frozenset({"tarif-social", "sociaal-tarief"})


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Discover Luminus products from the public sitemap.

    The /fr/particuliers/tarifs-energie/<slug>/ structure is the
    canonical product directory. Every slug there is a product
    (residential + market only). Excludes the regulated social
    tariff which is not user-selectable.
    """
    try:
        async with session.get(
            _SITEMAP_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                return set()
            xml = await resp.text()
    except aiohttp.ClientError:
        return set()
    return {
        slug for slug in _PRODUCT_PAGE_RE.findall(xml) if slug not in _EXCLUDED_SLUGS
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Luminus contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    if region not in _REGION_TO_TAB:
        raise ExtractorError(
            f"Luminus {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text(session, url)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _API_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Luminus contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, connection_fee = _extract_per_kwh_taxes(text)
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text)
    else:
        wallonia_renewables = _extract_wallonia_renewables(text)
        dsos = _extract_wallonia_dsos(text)

    return SupplierSnapshot(
        supplier="luminus",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            region_connection_fee=connection_fee,
            energy_fund_eur_per_month=energy_fund,
            vat_rate=0.0,
        ),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=publication_label,
        injection=injection,
    )


# ---- energy + tax block -------------------------------------------------------


_DYNAMIC_FORMULA_RE = re.compile(
    r"Prélèvement\s*\([^)]+\)\s*=\s*([\d,]+)\s*x\s*Belpex\s*H\s*([+\-–—])\s*([\d,]+)",
    re.S,
)
_INJECTION_FORMULA_RE = re.compile(
    r"Injection\s*\([^)]+\)\s*=\s*([\d,]+)\s*x\s*Belpex\s*H\s*([+\-–—])\s*([\d,]+)",
    re.S,
)


def _vat_multiplier(text: str) -> float:
    match = re.search(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", text, re.S)
    if not match:
        match = re.search(r"TVA\s*(\d+)\s*%", text)
    return 1.0 + (int(match.group(1)) / 100.0) if match else 1.06


def _extract_yearly_fee(text: str) -> float:
    match = re.search(r"Redevance fixe\s*\(€/an\)\s+(\d+,\d+)", text)
    return to_float(match.group(1)) if match else 0.0


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        match = _DYNAMIC_FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Luminus dynamic formula")
        factor_pdf = to_float(match.group(1))
        sign = -1.0 if match.group(2) in ("-", "–", "—") else 1.0
        base_pre_vat_cents = sign * to_float(match.group(3))
        vat = _vat_multiplier(text)
        # PDF formula: c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents.
        # Spot in EUR/kWh = Belpex_eur_mwh / 1000. Convert to:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=fee,
        )

    energy_match = re.search(
        r"Énergie fournie\s*\(c€/kWh\)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        text,
    )
    if not energy_match:
        raise ExtractorError(f"could not parse Luminus {kind} energy block")
    mono = to_float(energy_match.group(1)) / 100.0
    peak = to_float(energy_match.group(2)) / 100.0
    offpeak = to_float(energy_match.group(3)) / 100.0
    excl_night = to_float(energy_match.group(4)) / 100.0

    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=fee,
        )
    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=fee,
    )


def _extract_publication_month(text: str) -> str:
    # The first page usually says e.g. "Luminus Comfy Electricité (avril 2026)".
    match = re.search(
        r"\(([a-zA-Zéèû]+\s+\d{4})\)",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    indicative = re.search(
        r"Estimation annuelle du tarif\s+de l[\"'’©]énergie injectée[^0-9-]*([\d,]+)",
        text,
        re.S,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if kind == "dynamic":
        match = _INJECTION_FORMULA_RE.search(text)
        if match:
            factor_pdf = to_float(match.group(1))
            sign = -1.0 if match.group(2) in ("-", "–", "—") else 1.0
            base_pdf_cents = sign * to_float(match.group(3))
            # Residential injection is VAT-exempt in Belgium.
            factor = factor_pdf * 10.0
            base = base_pdf_cents / 100.0
            formula = match.group(0)

    if current is None and factor is None:
        return None
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


def _tax_block_values(text: str) -> list[str]:
    """Return the contiguous ['-', '5,0329', ...] run after the tax labels.

    The 'Taxes et redevances' section prints every label first then the
    matching values on their own lines, in the same order:

      [labels]
        Cotisation Fonds énergie (€/mois)
            Basse tension non résidentiel
            Basse tension résidentiel
        Droit d'accise spécial (c€/kWh)
        Cotisation sur l'énergie (c€/kWh)
        Redevance de raccordement (c€/kWh)        # Wallonia only
      [values]
        BTNR
        BTR
        Excise
        Cotisation
        Redevance                                  # Wallonia only

    Each value sits alone on its line - that's what tells us where the
    value list ends and the footnotes begin (the footnotes start with
    '(*) ...' and intermix numbers with text on the same line).
    """
    # 'Taxes et redevances' is mentioned twice in every PDF: once in the
    # 'Composition du prix' legend (no colon, no region) and once for the
    # actual tax table (`3 Taxes et redevances : WAL/FL`). Anchor on the
    # colon to only match the second one.
    block = re.search(
        r"3 Taxes et redevances\s*:\s*(?:WAL|FL|BRU).+?"
        r"(?=INFORMATION SUR VOTRE TARIF|Conditions\b)",
        text,
        re.S,
    )
    if not block:
        return []
    return re.findall(r"^\s*(-|\d+,\d+)\s*$", block.group(0), re.MULTILINE)


def _extract_per_kwh_taxes(text: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, connection_fee) in EUR/kWh."""
    values = _tax_block_values(text)

    def _decimal(s: str | None) -> float:
        if s is None or s == "-":
            return 0.0
        return to_float(s) / 100.0

    excise = _decimal(values[2]) if len(values) > 2 else 0.0
    contribution = _decimal(values[3]) if len(values) > 3 else 0.0
    has_connection = "Redevance de raccordement" in text
    connection = _decimal(values[4]) if has_connection and len(values) > 4 else 0.0
    return excise, contribution, connection


def _extract_energy_fund(text: str) -> float:
    """Pick the BTR (Basse tension résidentiel) value from the tax block.

    Flanders prints BTNR (non-residential) first then BTR (residential);
    the integration's residential users want BTR. A '-' means no fee.
    """
    values = _tax_block_values(text)
    if len(values) < 2 or values[1] == "-":
        return 0.0
    return to_float(values[1])


def _extract_flanders_renewables(text: str) -> float:
    """Flanders splits renewables across green energy + cogeneration.

    Layout:
        Coûts énergie verte (c€/kWh)
        Coûts cogénération (c€/kWh)
        FL
        <green>
        <cogen>
    """
    match = re.search(
        r"Coûts énergie verte.*?Coûts cogénération.*?FL\s*\n?\s*"
        r"(\d+,\d+)\s*\n?\s*(\d+,\d+)",
        text,
        re.S,
    )
    if match:
        return (to_float(match.group(1)) + to_float(match.group(2))) / 100.0
    # Some fixed cards may print only the green-energy line.
    fallback = re.search(
        r"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?FL\s*\n?\s*(\d+,\d+)",
        text,
        re.S,
    )
    return to_float(fallback.group(1)) / 100.0 if fallback else 0.0


def _extract_wallonia_renewables(text: str) -> float:
    match = re.search(
        r"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?WAL\s*\n?\s*(\d+,\d+)",
        text,
        re.S,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": "fluvius_antwerpen",
    "Fluvius Halle-Vilvoorde": "fluvius_halle_vilvoorde",
    "Fluvius Imewo": "fluvius_imewo",
    "Fluvius Kempen": "fluvius_iveka",
    "Fluvius Limburg": "fluvius_limburg",
    "Fluvius Midden-Vlaanderen": "fluvius_intergem",
    "Fluvius West": "fluvius_west",
    "Fluvius Zenne-Dijle": "fluvius_zenne_dijle",
}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Compteur digital columns from the Flanders DSO table.

    Static cards print 8 numbers per row (digital + classic + prosumer):
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal c€/kWh |
      dist_excl_night | capacity_classic €/yr | dist_classic_normal |
      dist_classic_excl | prosumer €/kW/yr

    Dynamic (SMR3) cards omit the analog-meter and prosumer columns and
    print only 4 numbers:
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal | dist_excl_night

    Distribution already includes transport (same convention as Engie's
    Flanders rows).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+((?:[\d,]+\s+){{3,}}[\d,]+)",
            text,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) < 4:
            continue
        prosumer: float | None = nums[7] if len(nums) >= 8 else None
        out[key] = DsoOverlay(
            distribution_single=nums[2] / 100.0,
            transport=0.0,
            data_management_per_year=nums[0],
            capacity_eur_per_kw_year=nums[1],
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": "aieg",
    "AIESH": "aiesh",
    "ORES (Brabant Wallon)": "ores",
    "TECTEO RESA": "resa",
    "WAVRE": "rew",
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static rows have 7 numbers:
      mono | pleines | creuses | excl_nuit | transport | data_mgmt | prosumer
    Dynamic rows have 9:
      mono | pleines | creuses | ECO | MEDIUM | PIC | excl_nuit |
      transport | data_mgmt
    The IMPACT triplet (ECO/MEDIUM/PIC) is unique to dynamic; its
    presence flips the prosumer column off (SMR3 has no compensation
    regime).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+((?:[\d,]+\s+){{6,}}[\d,]+)",
            text,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) >= 9:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            transport = nums[7]
            data_mgmt = nums[8]
            prosumer: float | None = None
        elif len(nums) >= 7:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            transport = nums[4]
            data_mgmt = nums[5]
            prosumer = nums[6]
        else:
            continue
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=pleines / 100.0,
            distribution_offpeak=creuses / 100.0,
            transport=transport / 100.0,
            data_management_per_year=data_mgmt,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_LUMINUS_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="luminus",
    label="Luminus",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_LUMINUS_REGIONS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    dso_keys=tuple(_FLANDERS_LABELS.values()) + tuple(_WALLONIA_LABELS.values()),
)
