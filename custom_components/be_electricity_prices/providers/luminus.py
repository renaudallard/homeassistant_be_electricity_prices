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
    fetch_pdf_text,
    fetch_text,
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
    TimeOfUseRates,
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
    _ContractDef(
        "luminus_comfyflex_plus", "Luminus ComfyFlex+", "variable", "comfyflex-plus"
    ),
    _ContractDef("luminus_maxxfix", "Luminus MaxxFix", "fixed", "maxxfix"),
    _ContractDef("luminus_maxxflex", "Luminus MaxxFlex", "variable", "maxxflex"),
    _ContractDef("luminus_basicfix", "Luminus BasicFix", "fixed", "basicfix"),
    _ContractDef("luminus_basicflex", "Luminus BasicFlex", "variable", "basicflex"),
    _ContractDef("luminus_smartflex", "Luminus SmartFlex", "tou", "smartflex"),
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
        xml = await fetch_text(session, _SITEMAP_URL)
    except ExtractorError:
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
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy + tax block -------------------------------------------------------


# Numeric token: digits optionally followed by a single decimal separator
# + digits. Anchors on starting + ending digit so a trailing sentence
# punctuation can't be captured (e.g. '0,1019 x Belpex H + 2,4591.\n'
# from luminus_dynamic_w would otherwise grab the final '.' if the
# regex were the lazier '[\d,.]+').
_NUM = r"\d+(?:[,.]\d+)?"

_DYNAMIC_FORMULA_RE = re.compile(
    rf"Prélèvement\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)
_INJECTION_FORMULA_RE = re.compile(
    rf"Injection\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(
        text,
        re.compile(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", re.S),
        r"TVA\s*(\d+)\s*%",
    )


def _extract_yearly_fee(text: str) -> float:
    """Capture the 'Redevance fixe' line.

    Every Luminus residential card the integration covers prints this
    line (~65 EUR for static, ~75 EUR for dynamic). A regex miss is a
    layout drift, not a fee-free contract; raise rather than default to
    0 so the coordinator surfaces the failure instead of silently
    dropping ~70 EUR/year from the user's annual estimate.
    """
    match = re.search(rf"Redevance fixe\s*\(€/an\)\s+({_NUM})", text)
    if match is None:
        raise ExtractorError("Luminus: yearly fee (Redevance fixe) not found")
    return to_float(match.group(1))


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee = _extract_yearly_fee(text)
    if kind == "tou":
        # SmartFlex's TOU table prints exactly three rates on the first
        # "Énergie fournie" row, e.g. "(c€/kWh) 15,54 13,29 6,72". The
        # second occurrence later in the PDF is the bi-horaire fallback
        # for non-SMR3 customers; we anchor on the first match.
        tou_match = re.search(
            rf"Énergie fournie\s*\(c€/kWh\)\s+({_NUM})\s+({_NUM})\s+({_NUM})(?!\s+\d)",
            text,
        )
        if not tou_match:
            raise ExtractorError("could not parse Luminus TOU energy block")
        peak = to_float(tou_match.group(1)) / 100.0
        transition = to_float(tou_match.group(2)) / 100.0
        offpeak = to_float(tou_match.group(3)) / 100.0
        return TimeOfUseRates(
            peak=peak,
            transition=transition,
            offpeak=offpeak,
            yearly_fixed_fee=fee,
        )

    if kind == "dynamic":
        match = _DYNAMIC_FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Luminus dynamic formula")
        factor_pdf = to_float(match.group(1))
        base_pre_vat_cents = parse_sign(match.group(2)) * to_float(match.group(3))
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
        rf"Énergie fournie\s*\(c€/kWh\)\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})",
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
    # The May 2026 cards started padding the inside of the parens with a
    # trailing space ("(mai 2026 )"), so tolerate optional whitespace
    # against future-similar formatting drift.
    match = re.search(
        r"\(\s*([a-zA-Zéèû]+\s+\d{4})\s*\)",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Some Luminus cards print a single-digit footnote ref right after
    # the unit ("(c€/kWh)2 9,37"). The previous `[^0-9-]*` skip stopped
    # at the footnote and captured it as the value, undercounting the
    # injection rate ~5x on dynamic_w/v fixtures. Skip an optional
    # digit-then-whitespace before the value capture.
    indicative = re.search(
        rf"Estimation annuelle du tarif\s+de l[\"'’©]énergie injectée"
        rf"[^0-9-]*(?:\d+\s+)?({_NUM})",
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
            base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
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
    return re.findall(rf"^\s*(-|{_NUM})\s*$", block.group(0), re.MULTILINE)


def _extract_per_kwh_taxes(text: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, connection_fee) in EUR/kWh.

    Federal excise + energy contribution are mandatory across regions;
    Walloon connection fee is mandatory in Wallonia (the
    'Redevance de raccordement' label is present iff the card is a
    Wallonia card). Raise on a layout drift that would otherwise zero
    out the regulated tax silently and underbill ~50 EUR/year per
    missed tier.
    """
    values = _tax_block_values(text)

    def _decimal(s: str | None) -> float:
        if s is None or s == "-":
            return 0.0
        return to_float(s) / 100.0

    if len(values) < 4:
        raise ExtractorError(
            f"Luminus: 'Taxes et redevances' block too short ({len(values)} values; "
            "expected ≥4 BTNR / BTR / excise / contribution)"
        )
    excise = _decimal(values[2])
    contribution = _decimal(values[3])
    has_connection = "Redevance de raccordement" in text
    if has_connection and len(values) < 5:
        raise ExtractorError(
            "Luminus: Walloon connection-fee row missing from tax block"
        )
    connection = _decimal(values[4]) if has_connection else 0.0
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

    Caller gates on REGION_FLANDERS so a miss is a layout drift, not a
    'no levy on this card' case. Raise rather than silently zero.
    """
    match = re.search(
        rf"Coûts énergie verte.*?Coûts cogénération.*?FL\s*\n?\s*"
        rf"({_NUM})\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match:
        return (to_float(match.group(1)) + to_float(match.group(2))) / 100.0
    # Some fixed cards may print only the green-energy line.
    fallback = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?FL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if fallback is None:
        raise ExtractorError(
            "Luminus: Flanders renewables (Coûts énergie verte) row not found"
        )
    return to_float(fallback.group(1)) / 100.0


def _extract_wallonia_renewables(text: str) -> float:
    """Mandatory in Wallonia (caller gates on REGION_WALLONIA); raise on
    miss rather than silently zero out."""
    match = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?WAL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match is None:
        raise ExtractorError(
            "Luminus: Wallonia renewables (Coûts énergie verte) row not found"
        )
    return to_float(match.group(1)) / 100.0


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
            rf"{re.escape(label)}\s+((?:{_NUM}\s+){{3,}}{_NUM})",
            text,
            re.IGNORECASE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) < 4:
            continue
        prosumer: float | None = nums[7] if len(nums) >= 8 else None
        out[key] = DsoOverlay(
            distribution_single=nums[2] / 100.0,
            distribution_exclusive_night=nums[3] / 100.0,
            transport=0.0,
            data_management_per_year=nums[0],
            capacity_eur_per_kw_year=nums[1],
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "TECTEO RESA": DSO_RESA,
    "WAVRE": DSO_REW,
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
            rf"{re.escape(label)}\s+((?:{_NUM}\s+){{6,}}{_NUM})",
            text,
            re.IGNORECASE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        eco = medium = pic = None
        if len(nums) >= 9:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            # Luminus prints ECO | MEDIUM | PIC in ascending order
            # (different from OCTA+/Bolt where the columns are PIC
            # first, descending). Map to the schema's distribution_*.
            eco, medium, pic = nums[3], nums[4], nums[5]
            excl_night = nums[6]
            transport = nums[7]
            data_mgmt = nums[8]
            prosumer: float | None = None
        elif len(nums) >= 7:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            excl_night = nums[3]
            transport = nums[4]
            data_mgmt = nums[5]
            prosumer = nums[6]
        else:
            continue
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=pleines / 100.0,
            distribution_offpeak=creuses / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            distribution_pic=pic / 100.0 if pic is not None else None,
            distribution_medium=medium / 100.0 if medium is not None else None,
            distribution_eco=eco / 100.0 if eco is not None else None,
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
)
