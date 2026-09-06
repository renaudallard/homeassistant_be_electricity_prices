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

Residential values are 6% VAT inclusive, and the Dynamic formula is
printed pre-VAT, so the extractor scales factor and base by the parsed VAT
multiplier. Engie also publishes a professional edition of most families
(``segment=P``, ``_P_`` in the slug): the same layout priced excluding
VAT at 21%, keeping the degressive excise schedule the residential cards
lost in August 2026 and printing the professional energy-fund row. Those
snapshots carry ``vat_rate`` and their per-kWh values as printed;
``base.apply_vat`` resolves them per config entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

import aiohttp

from ..const import (
    VAT_RATE_STANDARD,
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
    require_contract,
    SIGN_CHARS,
    fetch_pdf_text,
    fetch_text,
    parse_brussels_osp,
    parse_sign,
    parse_valid_until,
    tier_bound_kwh,
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
    TimeOfUseRates,
    VariableRates,
    brussels_sibelga_overlay,
    fixed_or_variable_rates,
    walloon_dso_overlay,
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
    # R (residential) or P (professional), in both the slug and the
    # segment query parameter. The professional edition of a product is a
    # different card: same layout, but priced excluding VAT, with the
    # degressive excise schedule and the professional energy-fund row.
    segment: str = "R"

    @property
    def professional(self) -> bool:
        return self.segment == "P"


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
    #
    # The professional editions. Engie publishes one for every family
    # except Direct Online and Basic Online, which are residential-only.
    # Note the Brussels term differs from the residential catalogue: the
    # pro cards run 12 / 24 months there too, not 36 / 48.
    _ContractDef(
        contract_id="engie_pro_easy_fixed",
        label="Engie Easy Fixed (pro)",
        kind="fixed",
        family="EASY",
        color="GREEN",
        rate="F",
        months_per_region={_V: "12", _W: "12", _B: "12"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_easy_variable",
        label="Engie Easy Variable (pro)",
        kind="variable",
        family="EASY",
        color="GREEN",
        rate="I",
        months_per_region={_V: "12", _W: "12", _B: "12"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_dynamic",
        label="Engie Dynamic (pro)",
        kind="dynamic",
        family="DYNAMIC",
        color="GREY",
        rate="I",
        months_per_region={_V: "12", _W: "12", _B: "12"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_empower_fixed",
        label="Engie Empower Fixed (pro)",
        kind="fixed",
        family="EMPOWER",
        color="GREEN",
        rate="F",
        months_per_region={_V: "00", _W: "00", _B: "00"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_empower_variable",
        label="Engie Empower Variable (pro)",
        kind="variable",
        family="EMPOWER",
        color="GREEN",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_empower_flextime",
        label="Engie Empower Flextime (pro)",
        kind="tou",
        family="EMPOWER",
        color="GREEN",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_flow",
        label="Engie Flow (pro)",
        kind="variable",
        family="FLOW",
        color="GREEN",
        rate="I",
        months_per_region={_V: "24", _W: "24", _B: "24"},
        segment="P",
    ),
    _ContractDef(
        contract_id="engie_pro_empty_house",
        label="Engie Empty House (pro)",
        kind="variable",
        family="EMPTYHOUSE",
        color="GREY",
        rate="I",
        months_per_region={_V: "00", _W: "00", _B: "00"},
        segment="P",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _slug(c: _ContractDef, region_code: str) -> str:
    months = c.months_per_region[region_code]
    return f"E_{c.family}_{c.segment}_{c.color}_C_{c.rate}_{months}_{region_code}_F"


def _document_url(c: _ContractDef, region_code: str) -> str:
    return (
        f"{_API_URL}?document={_slug(c, region_code)}"
        f"&monthOffset=0&segment={c.segment}&language=F"
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
        xml = await fetch_text(session, _SITEMAP_URL)
    except ExtractorError:
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
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Engie")

    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        raise ExtractorError(f"Engie: unknown region {region!r}")
    if region_code not in contract.months_per_region:
        raise ExtractorError(f"Engie {contract_id}: not available in region {region!r}")

    text = await fetch_pdf_text(session, _document_url(contract, region_code))
    return parse_snapshot(contract_id, {region: text})


def parse_snapshot(contract_id: str, region_texts: dict[str, str]) -> SupplierSnapshot:
    """Pure parser used by tests; takes already-extracted PDF text."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Engie")

    # Energy formula, injection, federal excise and energy contribution
    # are supplier-set or federal and identical across regions, so we
    # read them from any one PDF.
    any_text = next(iter(region_texts.values()))
    professional = contract.professional
    energy = _extract_energy(any_text, contract.kind, professional=professional)
    injection = _extract_injection(any_text, contract.kind, professional=professional)
    publication_label = _extract_publication_month(any_text)
    federal_excise, excise_bands = _extract_federal_excise(
        any_text, professional=professional
    )
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
            energy_fund = _extract_energy_fund(
                text,
                sans_domicile=contract_id == "engie_empty_house",
                professional=professional,
            )
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
            federal_excise_bands=excise_bands,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            brussels_renewables=brussels_renewables,
            region_connection_fee=region_connection_fee,
            energy_fund_eur_per_month=energy_fund,
            # The professional card prints everything excluding VAT at
            # 21%; base.apply_vat resolves it for the entry.
            vat_rate=VAT_RATE_STANDARD if professional else 0.0,
        ),
        source_url=_API_URL,
        publication_label=publication_label,
        valid_until=parse_valid_until(any_text),
        injection=injection,
    )


# ---- energy + tax block -------------------------------------------------------


_VAT_RE = re.compile(r"(\d+)\s*%\s*de\s*tva\s*comprise", re.IGNORECASE)
_VAT_EXCLUDED_RE = re.compile(r"Prix\s+tva\s+exclue", re.IGNORECASE)


def _vat_multiplier(text: str, *, professional: bool = False) -> float:
    # The Dynamic formula is printed pre-VAT and scaled by this multiplier
    # (the only caller), so the phrase is mandatory: a reworded header
    # would otherwise fall back to vat_multiplier's 6% default silently
    # and mask a VAT-rate or wording change. Fail loud instead.
    if professional:
        # The professional card prices everything excluding VAT, so the
        # formula needs no scaling here; the snapshot carries vat_rate and
        # base.apply_vat resolves it for the entry. Assert the header all
        # the same, so a card that starts printing VAT-inclusive numbers
        # fails loudly instead of silently under-pricing by 21%.
        if _VAT_EXCLUDED_RE.search(text) is None:
            raise ExtractorError("Engie: professional card is not marked tva exclue")
        return 1.0
    if _VAT_RE.search(text) is None:
        raise ExtractorError("could not parse Engie dynamic VAT multiplier")
    return vat_multiplier(text, _VAT_RE)


# Engie's formula prints either "Formule de prix hors TVA -1,3135 + (0,1095 x
# eSpot_15)" (consumption: positive base usually, negative for injection),
# but a future re-render could flip either side to a Unicode minus or any
# of the dashes from _pdf.SIGN_CHARS. Accept the full sign-class on both
# the base and the factor so the regex doesn't silently miss after a
# punctuation drift, and route through parse_sign for the magnitude.
_FORMULA_RE = re.compile(
    rf"Formule de prix\s+hors\s+TVA\s+([{SIGN_CHARS}]?)\s*([\d,.]+)\s*\+\s*"
    rf"\(([{SIGN_CHARS}]?)\s*([\d,.]+)\s*x\s*eSpot_15\)"
)


# Empower Variable / Empty House index BOTH legs on the monthly EPEXDAM and say
# so: "Le prix de l'electricite est indexe mensuellement. Le parametre
# d'indexation est la moyenne arithmetique des cotations journalieres Day Ahead
# EPEX SPOT Belgium (ci-apres EPEXDAM) durant le mois de fourniture", and "La
# valeur du EPEXDAM du mois en cours ne sera connue qu'en fin de mois. A titre
# informatif, les prix indiques sont bases sur la derniere valeur du EPEXDAM
# connue (Mars 2026: 92,57 EUR/MWh)."
#
# Two spellings, both on cards in the tree:
#   Empower     "- Normal = 2,1552 + (0,1171 x EPEXDAM)", one row per band
#   Empty House "3,2150 + (0,2150 x EPEXDAM)", bare, one row full stop
_EPEXDAM_FORMULA_RE = re.compile(
    rf"(?:[-\u2013]\s*([^=\n]{{1,44}}?)\s*=\s*)?"
    rf"([{SIGN_CHARS}]?)\s*(\d+[,.]\d+)\s*\+\s*\((\d+[,.]\d+)\s*x\s*EPEXDAM\)",
    re.IGNORECASE,
)
# Exact labels, never a substring. The Empower card prints SEVEN energy rows,
# and "Flextime Heures pleines" contains "heures pleines": a substring match
# binds the Flextime band to the bi-hourly meter, 0,1388 against 0,1264 on the
# April card, ~10% high. An unmapped label is dropped rather than guessed - the
# Flextime triplet belongs to the tou kind, which carries no coefficients at
# all. The empty key is the Empty House card, whose single formula is printed
# bare with no "- <label> =" prefix.
_EPEXDAM_BAND: dict[str, str] = {
    "": "single",
    "normal": "single",
    "tarif bihoraire heures pleines": "peak",
    "tarif bihoraire heures creuses": "offpeak",
    "exclusif nuit": "exclusive_night",
    # The Empower card's three Flextime rows, on both legs. They ride in the
    # same block as the bi-hourly rows and are bound by the same Normal row.
    "flextime heures pleines": "flex_peak",
    "flextime heures creuses": "flex_transition",
    "flextime heures super-creuses": "flex_offpeak",
}
_FLEXTIME_BANDS: tuple[str, ...] = ("flex_peak", "flex_transition", "flex_offpeak")
# "(Mars 2026: 92,57 EUR/MWh)" - the index the printed prices were computed at.
_EPEXDAM_INDEX_RE = re.compile(
    r"EPEXDAM\s+connue\s*\([^)]*?(\d+[,.]\d+)\s*€?\s*/\s*MWh", re.IGNORECASE
)


def _epexdam_index(text: str) -> float | None:
    """The EUR/MWh index the card's printed prices were computed at."""
    match = _EPEXDAM_INDEX_RE.search(text)
    return to_float(match.group(1)) if match else None


def _epexdam_formulas(
    text: str, printed: float, vat: float
) -> dict[str, tuple[float, float]]:
    """Per-band ``(factor, base)`` in EUR/kWh for the leg printing ``printed``.

    Both legs' formula blocks live on the same card and the two-column PDF
    interleaves them, so binding by document order is exactly the mistake the
    OCTA+ card punished. Bind by ARITHMETIC instead: evaluate each candidate at
    the index the card states and keep the block whose unlabelled-or-Normal row
    reproduces the price this leg actually prints. That is self-verifying, it
    survives a reordering, and it fails closed - no match means the caller
    keeps the printed value rather than billing a formula bound to the wrong
    leg.

    ``vat`` is the multiplier this leg is printed on: 1.06 for a residential
    consumption row, 1.0 for injection, which is VAT-exempt.
    """
    index = _epexdam_index(text)
    if index is None:
        return {}
    # Group into contiguous blocks. Each block opens on its Normal / bare row
    # and its band rows follow, so a band can never be lifted out of the other
    # leg's block - which is exactly what a flat scan did, pairing this leg's
    # Normal with the other leg's Heures pleines.
    blocks: list[dict[str, tuple[float, float]]] = []
    for label, sign, base_s, factor_s in _EPEXDAM_FORMULA_RE.findall(text):
        # The card states c/kWh per EUR/MWh of index, so onto a EUR/kWh spot
        # the factor carries a x10 and the base a /100.
        factor = to_float(factor_s) * 10.0 * vat
        base = parse_sign(sign or "+") * to_float(base_s) / 100.0 * vat
        slot = " ".join((label or "").split()).lower()
        key = _EPEXDAM_BAND.get(slot)
        if key is None:
            # A label this card generation invented.
            continue
        if key == "single" or not blocks:
            blocks.append({})
        blocks[-1].setdefault(key, (factor, base))
    for block in blocks:
        single = block.get("single")
        if single is None:
            continue
        if abs((single[0] * index / 1000.0 + single[1]) - printed) <= 1e-4:
            return block
    # No block reproduces this leg's printed price: the caller keeps that
    # price rather than billing a formula bound to the wrong leg.
    return {}


def _flextime_coefficients(
    text: str, printed_single: float, vat: float, printed_slots: tuple[float, ...]
) -> tuple[tuple[float, float], ...] | None:
    """The three Flextime ``(factor, base)`` pairs of the leg printing
    ``printed_single`` on its Normal row, or ``None``.

    Bound through :func:`_epexdam_formulas`, so the block is the one whose
    Normal row reproduces this leg's printed figure, and then held to the
    same test per slot: each Flextime pair has to reproduce the slot's own
    printed figure at the index the card states. A row that does not is a
    layout the card has not printed before, and the answer is the printed
    triplet, not two formulas and a guess.
    """
    coefs = _epexdam_formulas(text, printed_single, vat)
    index = _epexdam_index(text)
    if index is None or any(band not in coefs for band in _FLEXTIME_BANDS):
        return None
    pairs = tuple(coefs[band] for band in _FLEXTIME_BANDS)
    for (factor, base), printed in zip(pairs, printed_slots, strict=True):
        if abs((factor * index / 1000.0 + base) - printed) > 1e-4:
            return None
    return pairs


def _extract_energy(
    text: str, kind: TariffKind, *, professional: bool = False
) -> EnergyRates:
    # Engie prints the yearly fee in two different layouts:
    #
    # 1. Standard cards (Easy / Dynamic / Empty House): the fee sits on
    #    the same logical row as "Type d'usage", e.g. "65,00 €/an Type
    #    d'usage".
    # 2. Empower variants (Variable / Flextime): the fee is the first
    #    number on the "Prix mensuels" row, just before "Consommation(2)".
    #    The card has no "Type d'usage" anchor at all.
    #
    # Try the standard anchor first, fall back to the Empower layout, and
    # raise if neither matches: every residential Engie card the
    # integration covers carries a yearly fee, so a miss is a layout drift
    # rather than a fee-free contract.
    fee_match = re.search(r"(\d+[,.]\d+)\s*€/an\s*\n?\s*Type\s*\n?\s*d[©']usage", text)
    if fee_match is None:
        fee_match = re.search(
            r"Prix\s+mensuels\s*\n\s*(\d+[,.]\d+)\s+Consommation", text
        )
    if fee_match is None:
        raise ExtractorError("Engie: yearly fee row not found")
    yearly_fee = to_float(fee_match.group(1))

    if kind == "dynamic":
        match = _FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Engie dynamic consumption formula")
        # Groups: (base_sign, base_magnitude, factor_sign, factor_magnitude).
        base_pre_vat_cents = parse_sign(match.group(1)) * to_float(match.group(2))
        factor_pdf = parse_sign(match.group(3)) * to_float(match.group(4))
        vat = _vat_multiplier(text, professional=professional)
        # PDF formula yields c€/kWh hors TVA from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        # Engie Dynamic bills per quarter-hour: the consumer formula is
        # (B x eSpot_15) + A, where eSpot_15 is the Belgian day-ahead EPEX
        # price for that specific quarter-hour (engie.be/dynamic-tarief).
        # Keep the native 15-minute slots rather than the hourly mean.
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
            quarter_hourly=True,
        )

    # Capture the whole Consommation(2) row up to the newline. Most
    # contracts have 4 prices + 1 trailing renewables column; Empty House
    # and similar mono-only tariffs have just 1 price + 1 renewables.
    consumption = re.search(r"Consommation\(2\)([^\n]+)", text)
    if not consumption:
        raise ExtractorError(f"could not parse Engie {kind} consumption block")
    nums = [to_float(n) for n in re.findall(r"[\d,.]+", consumption.group(1))]
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
            tou = TimeOfUseRates(
                peak=prices[3] / 100.0,
                transition=prices[4] / 100.0,
                offpeak=prices[5] / 100.0,
                yearly_fixed_fee=yearly_fee,
                weekend_rule="weekend_no_peak",
            )
            # The same EPEXDAM sentence as Empower Variable, printed on the
            # same card: each Flextime band is a formula on the DELIVERY
            # month's mean and the triplet above is last month's. Bound by
            # the Normal row this leg prints, like the bi-hourly pairs.
            flex = _flextime_coefficients(
                text,
                prices[0] / 100.0,
                _vat_multiplier(text, professional=professional),
                (tou.peak, tou.transition, tou.offpeak),
            )
            if flex is None:
                return tou
            (f_peak, b_peak), (f_trans, b_trans), (f_off, b_off) = flex
            return replace(
                tou,
                month_indexed=True,
                formula_factor_peak=f_peak,
                formula_base_peak=b_peak,
                formula_factor_transition=f_trans,
                formula_base_transition=b_trans,
                formula_factor_offpeak=f_off,
                formula_base_offpeak=b_off,
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

    rates = fixed_or_variable_rates(
        kind,
        single=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )
    if not isinstance(rates, VariableRates) or mono is None:
        return rates
    # Empower Variable and Empty House index consumption on the DELIVERY
    # month's EPEXDAM and print a price computed from the last month whose
    # value is known. Easy Variable indexes on ENDEX101, which is published in
    # advance and correct as printed, and its card names no EPEXDAM at all, so
    # gating on the formula being present is what keeps it out.
    coefs = _epexdam_formulas(
        text, mono, _vat_multiplier(text, professional=professional)
    )
    if not coefs:
        return rates
    none2: tuple[float | None, float | None] = (None, None)
    return replace(
        rates,
        month_indexed=True,
        formula_factor=coefs["single"][0],
        formula_base=coefs["single"][1],
        formula_factor_peak=coefs.get("peak", none2)[0],
        formula_base_peak=coefs.get("peak", none2)[1],
        formula_factor_offpeak=coefs.get("offpeak", none2)[0],
        formula_base_offpeak=coefs.get("offpeak", none2)[1],
        # The card prices a night circuit separately, "Exclusif nuit = 2,4510
        # + (0,1005 x EPEXDAM)", between the mono 0,1171 and the off-peak
        # 0,0988. Routing it onto either neighbour is wrong on the meter that
        # draws the volume.
        formula_factor_exclusive_night=coefs.get("exclusive_night", none2)[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", none2)[1],
    )


def _extract_publication_month(text: str) -> str:
    match = re.search(
        r"contrats conclus en\s+([A-Za-zéûÉÛ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(
    text: str, kind: TariffKind, *, professional: bool = False
) -> InjectionRates | None:
    # The first "Injection(3)" row is the applicable rate (a second row is
    # the annual estimate). Its columns mirror the consumption row:
    #   normal | bi-pleines | bi-creuses | flextime pleines |
    #   flextime creuses | flextime super-creuses | ...
    row = re.search(r"Injection\(3\)\s+([^\n]+)", text)
    nums = (
        [to_float(t) for t in row.group(1).split() if re.fullmatch(r"[\d,.]+", t)]
        if row
        else []
    )
    current = nums[0] / 100.0 if nums else None

    peak: float | None = None
    transition: float | None = None
    offpeak: float | None = None
    slot_coefs: tuple[tuple[float, float], ...] | None = None
    if kind == "tou" and len(nums) >= 6:
        # Empower Flextime: the feed-in tariff varies by slot, so surface
        # the per-slot triplet (columns 4-6) the pricing engine selects via
        # tou_slot(). Columns 1-3 are the single / bi-horaire rates the
        # non-Flextime variants use; they're identical to each other here.
        peak = nums[3] / 100.0
        transition = nums[4] / 100.0
        offpeak = nums[5] / 100.0
        # And each slot is its own EPEXDAM formula, printed at the previous
        # month's index like the rest of the card. Not grossed: injection is
        # exempt on the residential edition and HTVA throughout on the
        # professional one, where vat_applies carries the 21% to apply_vat.
        if current is not None:
            slot_coefs = _flextime_coefficients(
                text, current, 1.0, (peak, transition, offpeak)
            )

    formulas = list(_FORMULA_RE.finditer(text))
    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    # Only Dynamic cards carry a spot injection formula (the second
    # BELPEX formula on the card). Gate on kind so a future indexed or
    # variable card that happens to print a price formula can't flip the
    # injection taxonomy to a spot factor/base shape.
    if kind == "dynamic" and len(formulas) >= 2:
        injection_match = formulas[1]
        # Groups: (base_sign, base_magnitude, factor_sign, factor_magnitude).
        base_pdf_cents = parse_sign(injection_match.group(1)) * to_float(
            injection_match.group(2)
        )
        factor_pdf = parse_sign(injection_match.group(3)) * to_float(
            injection_match.group(4)
        )
        # Both sides are printed on the card's own VAT basis: residential
        # injection is VAT-exempt, professional injection is grossed later
        # by apply_vat off vat_applies.
        factor = factor_pdf * 10.0
        base = base_pdf_cents / 100.0
        formula = injection_match.group(0)
    month_indexed = False
    if kind == "variable" and current is not None:
        # The same EPEXDAM story as the energy leg: "Les prix d'injection sont
        # indexes en utilisant le parametre EPEXDAM. La valeur du EPEXDAM du
        # mois en cours ne sera connue qu'en fin de mois", so the printed
        # Injection(3) figure is the formula on the PREVIOUS month.
        #
        # Restricted to the variable kind on purpose. Flextime matches the
        # same anchor but prints THREE distinct injection coefficient pairs,
        # one per TOU slot, which travel on the per-slot fields above: writing
        # its Normal row here as well would store a fourth formula that
        # nothing reads, because the per-slot rates win. The ENDEX101 cards
        # are excluded by the formula regex itself, which requires the literal
        # EPEXDAM; their index is a futures average published in ADVANCE, so
        # their printed figure is the billed rate with no lag to correct.
        #
        # Injection is not grossed on either edition: residential is exempt,
        # and a professional card is HTVA throughout with vat_applies carrying
        # the 21% for apply_vat. The card proves it - its injection formula
        # reproduces the printed figure with no 1,06 while the energy one
        # needs it.
        inj_coefs = _epexdam_formulas(text, current, 1.0)
        single = inj_coefs.get("single")
        if single is not None:
            factor, base = single
            month_indexed = True
            formula = f"{base * 100.0:.4f} + ({factor / 10.0:.4f} x EPEXDAM)"
    if slot_coefs is not None:
        month_indexed = True
        formula = "Flextime: " + "; ".join(
            f"{slot} {b * 100.0:.4f} + ({f / 10.0:.4f} x EPEXDAM)"
            for slot, (f, b) in zip(("peak", "transition", "offpeak"), slot_coefs)
        )
    if current is None and factor is None and peak is None:
        return None
    none2: tuple[float | None, float | None] = (None, None)
    peak_c, trans_c, off_c = slot_coefs if slot_coefs is not None else (none2,) * 3
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
        peak=peak,
        transition=transition,
        offpeak=offpeak,
        factor_peak=peak_c[0],
        base_peak=peak_c[1],
        factor_transition=trans_c[0],
        base_transition=trans_c[1],
        factor_offpeak=off_c[0],
        base_offpeak=off_c[1],
        # "Le prix d'injection est soumis a la TVA (21%)" on the
        # professional card, against "n'est pas soumis a la TVA" on the
        # residential one.
        vat_applies=professional,
    )


def _extract_consumption_renewables(text: str) -> float:
    """Pick the trailing 'Coûts énergie verte' value off the Consommation row.

    The row carries 3 (dynamic) or 5 (fixed/variable) numbers and the last
    one is always the regional renewable surcharge: Flanders cogen + green,
    Wallonia green-energy contribution, or Brussels green-energy levy.

    Mandatory in every region (~1.5-3 c€/kWh); raise on miss so a layout
    drift surfaces as an extractor failure rather than silently dropping
    the levy from the user's bill.
    """
    match = re.search(r"Consommation\(2\)\s+((?:[\d,.]+\s+)+[\d,.]+)", text)
    if not match:
        raise ExtractorError("Engie: Consommation(2) row (renewables) not found")
    nums = match.group(1).split()
    return to_float(nums[-1]) / 100.0


_EXCISE_TIER_RE = re.compile(
    r"Consommation entre\s+([\d.]+)\s+et\s+([\d.]+)\s+kWh\s+([\d,.]+)"
)


def _extract_federal_excise(
    text: str, *, professional: bool = False
) -> tuple[float, tuple[tuple[float, float], ...] | None]:
    """Federal excise, mandatory across regions.

    Returns ``(rate, bands)``. ``bands`` is None whenever the card prices
    one rate, which is every residential card; the coordinator resolves a
    banded card against the entry's annual volume.

    Three card shapes. Until July 2026 the residential excise was
    degressive and printed as four consumption tiers, of which the
    residential one is 0-3.000 kWh. From 1 August 2026 the federal scheme
    folded the separate energy contribution into the excise and flattened
    it, so the residential card prints one rate under "Toutes
    consommations". Try the flat form first: a card that carries both would
    be the tiered one being phased out, and the flat row is the
    authoritative single rate when it is present.

    Professional cards kept the schedule the residential ones lost, in
    three bands (0-20.000 / 20.000-50.000 / 50.000-1.000.000 kWh), and the
    card says so: "calcule sur base annuelle, suivant un tarif degressif
    par tranche de consommation". Read the whole table.
    """
    if professional:
        tiers = _EXCISE_TIER_RE.findall(text)
        if not tiers:
            raise ExtractorError("Engie: professional federal excise tiers not found")
        bands = tuple(
            (tier_bound_kwh(upper), to_float(rate) / 100.0)
            for _lower, upper, rate in tiers
        )
        return bands[0][1], bands
    flat = re.search(
        r"Accise\s+f[ée]d[ée]rale[^\n]*\n\s*Toutes\s+consommations\s+([\d,.]+)",
        text,
    )
    if flat:
        return to_float(flat.group(1)) / 100.0, None
    match = re.search(
        r"Consommation entre\s+0\s+et\s+3\.000\s+kWh\s+([\d,.]+)",
        text,
    )
    if not match:
        raise ExtractorError("Engie: federal excise (0-3000 kWh tier) not found")
    return to_float(match.group(1)) / 100.0, None


def _extract_energy_contribution(text: str) -> float:
    """Engie's PDF strips the comma: ``0,20417`` renders as ``020417``.

    Match either shape and reconstruct the decimal value as
    ``0.<digits>``. The regulated rate has 5-6 fractional digits so the
    quantifier ``\\d{4,6}`` covers it without picking up unrelated
    integers.

    The levy went to zero on 2026-08-01 and was folded into the special
    excise, so the August cards drop the row entirely. An absent row is the
    abolished levy, not a layout drift: return 0 rather than failing the
    fetch and taking every Engie contract offline.
    """
    match = re.search(
        r"Cotisation sur l['©]énergie\s+0\s*[,.]?\s*(\d{4,6})",
        text,
    )
    if not match:
        return 0.0
    return float(f"0.{match.group(1)}") / 100.0


def _extract_energy_fund(
    text: str, *, sans_domicile: bool = False, professional: bool = False
) -> float:
    """Flemish energy fund. Optional outside Flanders, so a miss
    legitimately means 'no fund on this card' -- keep the silent default.

    The residential card prints two sub-cases: 'avec domicile' (0 for most
    products) and 'sans domicile' (a positive fee). The Empty House product
    is created for vacant homes, which by definition have no registered
    domicile, so it bills the 'sans domicile' rate rather than 0.

    The professional card has neither: it prints one 'Professionnel (basse
    tension)' row, which applies to every professional product including
    the Empty House one."""
    if professional:
        match = re.search(r"Professionnel\s+\(basse\s+tension\)\s+([\d,.]+)", text)
        return to_float(match.group(1)) if match else 0.0
    label = "sans" if sans_domicile else "avec"
    match = re.search(
        rf"Résidentiel\s+\({label}\s+domicile\)\s+([\d,.]+)",
        text,
    )
    return to_float(match.group(1)) if match else 0.0


def _extract_connection_fee(text: str) -> float:
    """Walloon connection fee (0,075 c€/kWh).

    Caller gates the invocation on REGION_WALLONIA so a miss here is a
    layout drift on a Wallonia card; raise rather than zero out.
    """
    match = re.search(r"Redevance raccordement\(\d+\)\s+([\d,.]+)", text)
    if not match:
        raise ExtractorError("Engie: Wallonia connection fee row not found")
    return to_float(match.group(1)) / 100.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "FLUVIUS ANTWERPEN": DSO_FLUVIUS_ANTWERPEN,
    "FLUVIUS HALLE-VILVOORDE": DSO_FLUVIUS_HALLE_VILVOORDE,
    "FLUVIUS IMEWO": DSO_FLUVIUS_IMEWO,
    "FLUVIUS KEMPEN": DSO_FLUVIUS_IVEKA,
    "FLUVIUS LIMBURG": DSO_FLUVIUS_LIMBURG,
    "FLUVIUS MIDDEN-VLAANDEREN": DSO_FLUVIUS_INTERGEM,
    "FLUVIUS WEST": DSO_FLUVIUS_WEST,
    "FLUVIUS ZENNE-DIJLE": DSO_FLUVIUS_ZENNE_DIJLE,
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
            rf"{re.escape(label)}\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)",
            block_text,
            re.IGNORECASE,
        )
        if not row:
            continue
        capacity = to_float(row.group(1))
        dist_normal = to_float(row.group(2))
        dist_excl = to_float(row.group(3))
        data_qh = to_float(row.group(4))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            # Group 3 is the "tarif-kWh exclusif nuit" column, lower than
            # the single rate; bill a dedicated night meter at it instead
            # of falling back to the day rate.
            distribution_exclusive_night=dist_excl / 100.0,
            transport=0.0,
            data_management_per_year=data_qh,
            capacity_eur_per_kw_year=capacity,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brab. Wal.)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "TECTEO - RESA": DSO_RESA,
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static-contract rows have 10 numbers (with prosumer column) and
    dynamic-contract rows have 9 (the prosumer column is replaced with
    nothing). Last column is always the c€/kWh transport rate.

    Layout (c€/kWh except where noted):
        single | peak | offpeak | PIC | MEDIUM | ECO | excl_night |
        data_mgmt (€/an) | [prosumer (€/kVA/an)?] | transport
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        # Horizontal whitespace only ([^\S\n] = whitespace minus newline)
        # between the numbers so a greedy match can't span a blank line
        # and pull the next row's (or a footnote's) leading number into
        # this row -- that shifted every column right and billed
        # transport at a stray value while dropping the real rate.
        row = re.search(
            rf"^{re.escape(label)}[^\S\n]+((?:[\d,.]+[^\S\n]+){{8,}}[\d,.]+)",
            text,
            re.MULTILINE | re.IGNORECASE,
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
        out[key] = walloon_dso_overlay(
            mono=nums[0],
            peak=nums[1],
            offpeak=nums[2],
            pic=nums[3],
            medium=nums[4],
            eco=nums[5],
            excl_night=nums[6],
            transport=transport,
            terme_fixe=data_mgmt,
            prosumer=prosumer,
        )

    # The card lists ~7 ORES sub-areas (Brab. Wal., Est, Hainaut, ...),
    # numerically identical today; the loop above maps only the
    # "ORES (Brab. Wal.)" row into the single ORES key. Assert the other
    # sub-areas match it so a future sub-area tariff split is caught here
    # rather than silently billing every ORES customer at the Brab. Wal.
    # rate (mirrors the Ecofix ORES guard).
    ores_rows = re.findall(
        r"^ORES\s*\([^)]+\)[^\S\n]+((?:[\d,.]+[^\S\n]+){8,}[\d,.]+)",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    first = ores_rows[0].split() if ores_rows else None
    for other in ores_rows[1:]:
        if other.split() != first:
            raise ExtractorError(
                "Engie: ORES sub-area tariffs diverged from the first ORES "
                "row; a sub-area split needs an explicit DSO key"
            )
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Sibelga row.

    Layout: distribution Normal | Pleines | Creuses | Excl Nuit (c€/kWh) |
            Activité de mesure (€/an) | Puissance ≤13kVA (€/an) |
            Puissance >13kVA (€/an) | Transport (c€/kWh)
    """
    row = re.search(
        r"^SIBELGA\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
        r"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)",
        text,
        re.MULTILINE,
    )
    if not row:
        return {}
    nums = [to_float(row.group(i)) for i in range(1, 9)]
    # A residential <=13kVA Brussels connection is billed both the metering
    # fee (Activite de mesure, nums[4]) and the Sibelga <=13kVA power term
    # (nums[5]). Brussels has no separate capacity charge (capacity is
    # Flanders-only), so fold both flat annual euros into the DSO fee.
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=nums[0],
            peak=nums[1],
            offpeak=nums[2],
            excl_night=nums[3],
            transport=nums[7],
            # Columns 5 and 6 are the power term's two bands, at or below
            # 13 kVA and above it; the mesure fee (4) is billed either way.
            data_management_per_year=nums[4] + nums[5],
            power_term_above_13kva=nums[4] + nums[6],
            osp_by_tier=parse_brussels_osp(text),
        )
    }


_LETTER_TO_REGION = {_V: REGION_FLANDERS, _W: REGION_WALLONIA, _B: REGION_BRUSSELS}


def _contract_regions(c: _ContractDef) -> frozenset[str]:
    return frozenset(_LETTER_TO_REGION[k] for k in c.months_per_region)


# The variable contracts whose card indexes the feed-in credit on the monthly
# EPEXDAM. The credit resolves against ENTSO-E spots their variable energy leg
# never fetches, so the flow has to offer the optional key or the formula can
# never resolve.
#
# Flextime carries the same sentence with one coefficient pair per TOU slot,
# on the per-slot fields of InjectionRates, and its ENERGY bands are indexed
# the same way, so it needs the key twice over. The ENDEX101 products are
# absent because their index is a futures average published in ADVANCE, so
# their printed figure is the billed rate.
_EPEXDAM_INJECTION_CONTRACTS: frozenset[str] = frozenset(
    {
        "engie_empower_variable",
        "engie_empower_flextime",
        "engie_flow",
        "engie_direct_online",
        "engie_basic_online",
        "engie_empty_house",
        "engie_pro_empower_variable",
        "engie_pro_empower_flextime",
        "engie_pro_flow",
        "engie_pro_empty_house",
    }
)


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=0.3,
    id="engie",
    label="Engie",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_contract_regions(c),
            professional=c.professional,
            spot_indexed_injection=c.contract_id in _EPEXDAM_INJECTION_CONTRACTS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
)
