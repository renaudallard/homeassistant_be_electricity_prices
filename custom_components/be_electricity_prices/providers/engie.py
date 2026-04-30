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

"""Engie Belgium tariff card extractor.

Engie publishes the current month's tariff card per (contract, region)
through a public REST endpoint:

    https://www.engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF
        ?document=<DOC_CODE>&monthOffset=0&segment=R&language=F

The DOC_CODE is built from the contract family + green/grey + fixed/
indexed + duration + region + language family. Engie ships up to three
regional documents per contract (V/W/B for Vlaanderen / Wallonie /
Bruxelles); the extractor fetches the configured region's PDF on demand,
since the energy formula is region-uniform but the DSO overlay is not.
``parse_snapshot`` still accepts a multi-region map so tests can exercise
the merge path.

All values are 6% VAT inclusive. The Dynamic formula is printed pre-VAT,
so the extractor scales factor and base by the parsed VAT multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from ..const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA
from ._pdf import USER_AGENT, fetch_pdf_text, parse_valid_until, to_float
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

_API_URL = (
    "https://www.engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF"
)

_V = "V"
_W = "W"
_B = "B"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: _V,
    REGION_WALLONIA: _W,
    REGION_BRUSSELS: _B,
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    family: str
    color: str  # GREEN or GREY in the slug
    rate: str  # F (fixed) or I (indexed) in the slug
    months_per_region: dict[str, str]


# Catalogue of every electricity contract Engie publishes on the public
# pricing page. Each entry maps to one of Engie's document slugs; the
# region letter chooses the regional PDF and the month suffix differs
# between the residential 12/24/00-month products (V/W) and the
# Brussels 36/48/00-month variants (B).
_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        contract_id="engie_easy_fixed",
        label="Engie Easy Fixed",
        kind="fixed",
        family="EASY",
        color="GREEN",
        rate="F",
        months_per_region={_V: "12", _W: "12", _B: "36"},
    ),
    _ContractDef(
        contract_id="engie_easy_variable",
        label="Engie Easy Variable",
        kind="variable",
        family="EASY",
        color="GREEN",
        rate="I",
        months_per_region={_V: "12", _W: "12", _B: "36"},
    ),
    _ContractDef(
        contract_id="engie_direct_online",
        label="Engie Direct Online",
        kind="variable",
        family="DIRECT_ONLINE",
        color="GREEN",
        rate="I",
        months_per_region={_V: "12", _W: "12", _B: "36"},
    ),
    _ContractDef(
        contract_id="engie_basic_online",
        label="Engie Basic Online",
        kind="variable",
        family="BASIC_ONLINE",
        color="GREY",
        rate="I",
        months_per_region={_V: "24", _W: "24"},
    ),
    _ContractDef(
        contract_id="engie_dynamic",
        label="Engie Dynamic",
        kind="dynamic",
        family="DYNAMIC",
        color="GREY",
        rate="I",
        months_per_region={_V: "12", _W: "12", _B: "36"},
    ),
    _ContractDef(
        contract_id="engie_empower_fixed",
        label="Engie Empower Fixed",
        kind="fixed",
        family="EMPOWER",
        color="GREEN",
        rate="F",
        months_per_region={_V: "00", _W: "00", _B: "00"},
    ),
    _ContractDef(
        contract_id="engie_empower_variable",
        label="Engie Empower Variable",
        kind="variable",
        family="EMPOWER",
        color="GREEN",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
    ),
    _ContractDef(
        # Empower Flextime is the SMR3-only TOU billing mode of the
        # Empower Variable product. Uses the same PDF; the parser
        # extracts the Flextime triplet (Heures pleines/creuses/super-
        # creuses) instead of the bi-horaire rates. Weekend rule is
        # weekend_no_peak per CWaPE Engie publication.
        contract_id="engie_empower_flextime",
        label="Engie Empower Flextime",
        kind="tou",
        family="EMPOWER",
        color="GREEN",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
    ),
    _ContractDef(
        contract_id="engie_flow",
        label="Engie Flow",
        kind="variable",
        family="FLOW",
        color="GREEN",
        rate="I",
        months_per_region={_V: "24", _W: "24", _B: "48"},
    ),
    _ContractDef(
        contract_id="engie_empty_house",
        label="Engie Empty House",
        kind="variable",
        family="EMPTYHOUSE",
        color="GREY",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
    ),
    # Engie's Tarif Social (E_SOCIAL_R_GREY_C_F) is omitted on purpose: the
    # social tariff is set quarterly by the CREG and is auto-assigned to
    # protected customers (they don't pick it from a list). Its PDF carries
    # an all-in regulated price with no DSO breakdown, so it doesn't fit
    # the integration's energy-plus-network-plus-tax model.
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _slug(c: _ContractDef, region_code: str) -> str:
    months = c.months_per_region[region_code]
    return f"E_{c.family}_R_{c.color}_C_{c.rate}_{months}_{region_code}_F"


def _document_url(c: _ContractDef, region_code: str) -> str:
    return (
        f"{_API_URL}?document={_slug(c, region_code)}"
        f"&monthOffset=0&segment=R&language=F"
    )


_SITEMAP_URL = "https://www.engie.be/sitemap.xml"

# URL-token -> registry family. The sitemap exposes product pages as
# /(fr|nl)/<token>(?:-tarief|-faq|-contract|-vast|-variable|-fixed|...);
# extract <token>, look it up here. Anything not in this map is a new
# product family and gets surfaced verbatim.
_URL_TOKEN_TO_FAMILY = {
    "easy": "EASY",
    "direct": "DIRECT_ONLINE",
    "basic": "BASIC_ONLINE",
    "dynamic": "DYNAMIC",
    "empower": "EMPOWER",
    "flow": "FLOW",
    "empty": "EMPTYHOUSE",
}

# Suffixes Engie uses on product page slugs. The token is the part
# before any of these.
_PRODUCT_SUFFIXES = (
    "tarief",
    "tariff",
    "faq",
    "contract",
    "vast",
    "variable",
    "fixed",
    "flex",
    "flextime",
    "online",
    "house",
)
_PRODUCT_PAGE_RE = re.compile(
    r"/(?:fr|nl)/([a-z]+)-(?:" + "|".join(_PRODUCT_SUFFIXES) + r")\b"
)

# Tokens that match _PRODUCT_PAGE_RE in non-product marketing pages
# (e.g. "uw-contract" = "your contract", "vragen-faq" = "questions").
# These are NL/FR common words the heuristic can't distinguish from a
# real product family without more signal. Filtered out before diff.
_NOISE_TOKENS = frozenset(
    {
        "uw",  # NL "your"
        "je",  # NL "your" (informal)
        "ton",  # FR "your"
        "vragen",  # NL "questions"
        "voordelig",  # NL "advantageous"
        "flextime",  # sub-variant of EMPOWER
    }
)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Best-effort family-level discovery via the public sitemap.

    Engie has no list endpoint on its tariff API, so this scrapes
    sitemap.xml for /<lang>/<token>-(tarief|faq|contract|...) URLs,
    maps each token to its registry family identifier, and surfaces
    anything unmapped. False positives are possible (marketing pages
    using a product token in a non-product context); the catalog
    issue is informational so a small amount of noise is fine.
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
    out: set[str] = set()
    for token in _PRODUCT_PAGE_RE.findall(xml):
        if token in _NOISE_TOKENS:
            continue
        out.add(_URL_TOKEN_TO_FAMILY.get(token, token))
    return out


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Engie contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        raise ExtractorError(f"Engie: unknown region {region!r}")
    if region_code not in contract.months_per_region:
        raise ExtractorError(f"Engie {contract_id}: not available in region {region!r}")

    text = await fetch_pdf_text(session, _document_url(contract, region_code))
    return parse_snapshot(contract_id, {region: text})


def parse_snapshot(contract_id: str, region_texts: dict[str, str]) -> SupplierSnapshot:
    """Pure parser used by tests; takes already-extracted PDF text."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Engie contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    # Energy formula, injection, federal excise and energy contribution
    # are supplier-set or federal and identical across regions, so we
    # read them from any one PDF.
    any_text = next(iter(region_texts.values()))
    energy = _extract_energy(any_text, contract.kind)
    injection = _extract_injection(any_text)
    publication_label = _extract_publication_month(any_text)
    federal_excise = _extract_federal_excise(any_text)
    energy_contribution = _extract_energy_contribution(any_text)

    dsos: dict[str, DsoOverlay] = {}
    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    energy_fund = 0.0
    region_connection_fee = 0.0
    for region_key, text in region_texts.items():
        renewables = _extract_consumption_renewables(text)
        if region_key == REGION_FLANDERS:
            dsos.update(_extract_flanders_dsos(text))
            flanders_renewables = renewables
            energy_fund = _extract_energy_fund(text)
        elif region_key == REGION_WALLONIA:
            dsos.update(_extract_wallonia_dsos(text))
            wallonia_renewables = renewables
            region_connection_fee = _extract_connection_fee(text)
        elif region_key == REGION_BRUSSELS:
            dsos.update(_extract_brussels_dsos(text))
            brussels_renewables = renewables

    return SupplierSnapshot(
        supplier="engie",
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
        source_url=_API_URL,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy + tax block -------------------------------------------------------


def _vat_multiplier(text: str) -> float:
    match = re.search(r"(\d+)\s*%\s*de\s*tva\s*comprise", text, re.IGNORECASE)
    return 1.0 + (int(match.group(1)) / 100.0) if match else 1.06


_FORMULA_RE = re.compile(
    r"Formule de prix\s+hors\s+TVA\s+(-?[\d,]+)\s*\+\s*"
    r"\((-?[\d,]+)\s*x\s*eSpot_15\)"
)


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee_match = re.search(r"(\d+,\d+)\s*€/an\s*\n?\s*Type\s*\n?\s*d[©']usage", text)
    yearly_fee = to_float(fee_match.group(1)) if fee_match else 0.0

    if kind == "dynamic":
        match = _FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Engie dynamic consumption formula")
        base_pre_vat_cents = to_float(match.group(1))
        factor_pdf = to_float(match.group(2))
        vat = _vat_multiplier(text)
        # PDF formula yields c€/kWh hors TVA from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
        )

    # Capture the whole Consommation(2) row up to the newline. Most
    # contracts have 4 prices + 1 trailing renewables column; Empty House
    # and similar mono-only tariffs have just 1 price + 1 renewables.
    consumption = re.search(r"Consommation\(2\)([^\n]+)", text)
    if not consumption:
        raise ExtractorError(f"could not parse Engie {kind} consumption block")
    nums = [to_float(n) for n in re.findall(r"[\d,]+", consumption.group(1))]
    # Last column is the regional renewables levy; drop it, what remains
    # is the price columns.
    prices = nums[:-1] if len(nums) >= 2 else nums
    peak: float | None
    offpeak: float | None
    excl_night: float | None
    if len(prices) == 4:
        # Standard layout: Normal | Bi-pleines | Bi-creuses | Excl. nuit
        mono, peak, offpeak, excl_night = (p / 100.0 for p in prices)
    elif len(prices) == 7:
        # Empower Variable with Flextime: Normal | Bi-pleines | Bi-creuses
        # | Flextime pleines | Flextime creuses | Flextime super-creuses |
        # Exclusif nuit. The variable contract uses the bi-horaire pair;
        # the Flextime contract returns the TOU triplet directly.
        if kind == "tou":
            return TimeOfUseRates(
                peak=prices[3] / 100.0,
                transition=prices[4] / 100.0,
                offpeak=prices[5] / 100.0,
                yearly_fixed_fee=yearly_fee,
                weekend_rule="weekend_no_peak",
            )
        mono = prices[0] / 100.0
        peak = prices[1] / 100.0
        offpeak = prices[2] / 100.0
        excl_night = prices[6] / 100.0
    elif len(prices) == 1:
        # Mono-only tariffs (e.g. Empty House for vacant properties).
        mono = prices[0] / 100.0
        peak = offpeak = excl_night = None
    else:
        raise ExtractorError(
            f"unexpected price column count for Engie {kind}: {len(prices)}"
        )

    if kind == "tou":
        # 7-price Empower Variable layout was the only path here; if we
        # arrive with kind="tou" but a 4-price row, the user picked
        # Flextime on a card that doesn't carry it.
        raise ExtractorError(
            "Engie Empower Flextime requires the 7-price Empower row "
            "(Flextime triplet); not present in this card."
        )

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


def _extract_publication_month(text: str) -> str:
    match = re.search(
        r"contrats conclus en\s+([A-Za-zéûÉÛ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(text: str) -> InjectionRates | None:
    indicative = re.search(r"Injection\(3\)\s+([\d,]+)", text)
    formulas = list(_FORMULA_RE.finditer(text))
    current = to_float(indicative.group(1)) / 100.0 if indicative else None
    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if len(formulas) >= 2:
        injection_match = formulas[1]
        base_pdf_cents = to_float(injection_match.group(1))
        factor_pdf = to_float(injection_match.group(2))
        # Residential injection is VAT-exempt.
        factor = factor_pdf * 10.0
        base = base_pdf_cents / 100.0
        formula = injection_match.group(0)
    if current is None and factor is None:
        return None
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


def _extract_consumption_renewables(text: str) -> float:
    """Pick the trailing 'Coûts énergie verte' value off the Consommation row.

    The row carries 3 (dynamic) or 5 (fixed/variable) numbers and the last
    one is always the regional renewable surcharge: Flanders cogen + green,
    Wallonia green-energy contribution, or Brussels green-energy levy.
    """
    match = re.search(r"Consommation\(2\)\s+((?:[\d,]+\s+)+[\d,]+)", text)
    if not match:
        return 0.0
    nums = match.group(1).split()
    return to_float(nums[-1]) / 100.0


def _extract_federal_excise(text: str) -> float:
    match = re.search(
        r"Consommation entre\s+0\s+et\s+3\.000\s+kWh\s+([\d,]+)",
        text,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_energy_contribution(text: str) -> float:
    """Engie's PDF strips the comma: ``0,20417`` renders as ``020417``.

    Match either shape and reconstruct the decimal value as
    ``0.<digits>``. The regulated rate has 5-6 fractional digits so the
    quantifier ``\\d{4,6}`` covers it without picking up unrelated
    integers.
    """
    match = re.search(
        r"Cotisation sur l['©]énergie\s+0\s*[,.]?\s*(\d{4,6})",
        text,
    )
    if not match:
        return 0.0
    return float(f"0.{match.group(1)}") / 100.0


def _extract_energy_fund(text: str) -> float:
    match = re.search(
        r"Résidentiel\s+\(avec\s+domicile\)\s+([\d,]+)",
        text,
    )
    return to_float(match.group(1)) if match else 0.0


def _extract_connection_fee(text: str) -> float:
    match = re.search(r"Redevance raccordement\(\d+\)\s+([\d,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "FLUVIUS ANTWERPEN": "fluvius_antwerpen",
    "FLUVIUS HALLE-VILVOORDE": "fluvius_halle_vilvoorde",
    "FLUVIUS IMEWO": "fluvius_imewo",
    "FLUVIUS KEMPEN": "fluvius_iveka",
    "FLUVIUS LIMBURG": "fluvius_limburg",
    "FLUVIUS MIDDEN-VLAANDEREN": "fluvius_intergem",
    "FLUVIUS WEST": "fluvius_west",
    "FLUVIUS ZENNE-DIJLE": "fluvius_zenne_dijle",
}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Compteur digital Fluvius table.

    Static cards include both a digital and an analog meter table; the
    integration only uses the digital one. Distribution rates already
    include transport ('incluant déjà les coûts de transport') so we set
    ``transport=0`` and put the full c€/kWh into ``distribution_single``.
    """
    digital_block = re.search(
        r"Compteur\s+digital(.+?)(?=Compteur\s+analogique|Suppléments)",
        text,
        re.S,
    )
    block_text = digital_block.group(1) if digital_block else text
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
            block_text,
        )
        if not row:
            continue
        capacity = to_float(row.group(1))
        dist_normal = to_float(row.group(2))
        data_qh = to_float(row.group(4))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            transport=0.0,
            data_management_per_year=data_qh,
            capacity_eur_per_kw_year=capacity,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": "aieg",
    "AIESH": "aiesh",
    "ORES (Brab. Wal.)": "ores",
    "REGIE DE WAVRE": "rew",
    "TECTEO - RESA": "resa",
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static-contract rows have 10 numbers (with prosumer column) and
    dynamic-contract rows have 9 (the prosumer column is replaced with
    nothing). Last column is always the c€/kWh transport rate.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        row = re.search(
            rf"^{re.escape(label)}\s+((?:[\d,]+\s+){{8,}}[\d,]+)",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) < 9:
            continue
        prosumer: float | None = None
        if len(nums) >= 10:
            data_mgmt = nums[7]
            prosumer = nums[8]
            transport = nums[9]
        else:
            data_mgmt = nums[7]
            transport = nums[8]
        out[key] = DsoOverlay(
            distribution_single=nums[0] / 100.0,
            distribution_peak=nums[1] / 100.0,
            distribution_offpeak=nums[2] / 100.0,
            distribution_pic=nums[3] / 100.0,
            distribution_medium=nums[4] / 100.0,
            distribution_eco=nums[5] / 100.0,
            transport=transport / 100.0,
            data_management_per_year=data_mgmt,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Sibelga row.

    Layout: distribution Normal | Pleines | Creuses | Excl Nuit (c€/kWh) |
            Activité de mesure (€/an) | Puissance ≤13kVA (€/an) |
            Puissance >13kVA (€/an) | Transport (c€/kWh)
    """
    row = re.search(
        r"^SIBELGA\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        text,
        re.MULTILINE,
    )
    if not row:
        return {}
    nums = [to_float(row.group(i)) for i in range(1, 9)]
    return {
        "sibelga": DsoOverlay(
            distribution_single=nums[0] / 100.0,
            distribution_peak=nums[1] / 100.0,
            distribution_offpeak=nums[2] / 100.0,
            transport=nums[7] / 100.0,
            data_management_per_year=nums[4],
        )
    }


_LETTER_TO_REGION = {_V: REGION_FLANDERS, _W: REGION_WALLONIA, _B: REGION_BRUSSELS}


def _contract_regions(c: _ContractDef) -> frozenset[str]:
    return frozenset(_LETTER_TO_REGION[k] for k in c.months_per_region)


EXTRACTOR = SupplierExtractor(
    id="engie",
    label="Engie",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_contract_regions(c),
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    dso_keys=(
        tuple(_FLANDERS_LABELS.values())
        + tuple(_WALLONIA_LABELS.values())
        + ("sibelga",)
    ),
)
