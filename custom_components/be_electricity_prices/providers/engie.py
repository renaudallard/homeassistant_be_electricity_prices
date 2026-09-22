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
from datetime import date
from dataclasses import dataclass

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    VAT_RATE_STANDARD,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    FR_MONTHS,
    fetch_pdf_text,
    fetch_text,
    is_transient_fetch_error,
)
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from ._parse import (
    require_contract,
    to_float,
)
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from ._rates import (
    Contract,
    TariffKind,
)
from ._engie_overlays import (
    _extract_brussels_dsos,
    _extract_consumption_renewables,
    _extract_energy_contribution,
    _extract_energy_fund,
    _extract_federal_excise,
    _extract_flanders_dsos,
    _extract_wallonia_dsos,
)
from ._engie_cards import (
    _extract_energy,
    _extract_injection,
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


def _document_url(c: _ContractDef, region_code: str, month_offset: int = 0) -> str:
    """The document API URL for ``c`` in ``region_code``.

    ``month_offset`` counts months back from the current card: 0 is the card
    ``fetch`` reads, 1 the previous month's, and so on. The API keeps them
    well past two years, so this is also the archive (see
    :func:`fetch_for_month`).
    """
    return (
        f"{_API_URL}?document={_slug(c, region_code)}"
        f"&monthOffset={month_offset}&segment={c.segment}&language=F"
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


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card Engie published for one past month, or ``None``.

    The same document API serves them: its ``monthOffset`` parameter counts
    months back from the current card, and Engie keeps the run well past two
    years (September 2024 still answers, in the layout this parser knows;
    the 2023 cards predate it and fail, which comes back as None). A card is
    only ever addressed relative to the month the API is in, so the offset
    is the calendar distance from today, and a month ahead of today is
    refused up front rather than sent, since the API answers it with 404.

    Until this existed a contract start date did nothing on an Engie entry:
    the signing-cohort splice had no card to read and every past month of
    the year-to-date billed on the current card as a proxy. The month the
    card names is cross-checked (``contrats conclus en <mois> <annee>``),
    and every failure is swallowed, since this runs inside the year-to-date
    walk and one month must not take the whole year down.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    region_code = _REGION_TO_CODE.get(region)
    if (
        contract is None
        or region_code is None
        or region_code not in contract.months_per_region
    ):
        return None
    first = date(year_month.year, year_month.month, 1)
    # Home Assistant's own zone, like every other provider: the OS clock is
    # still yesterday between midnight and 02:00 Brussels on a UTC host, and
    # on the first of a month that made the offset for the month just closed
    # one short, so the archive answered with the card after it.
    today = dt_util.now().date()
    offset = (today.year - first.year) * 12 + (today.month - first.month)
    if offset < 0:
        return None
    url = _document_url(contract, region_code, month_offset=offset)
    try:
        text = await fetch_pdf_text(session, url)
        snap = parse_snapshot(contract_id, {region: text})
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    return archive_validity_check(snap, text, first, month_names=FR_MONTHS)


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


def _extract_publication_month(text: str) -> str:
    match = re.search(
        r"contrats conclus en\s+([A-Za-zéûÉÛ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


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

# The variable contracts whose ENERGY is indexed on the delivery month's mean.
# The same ten today, and written OUT rather than derived: the two are
# independent properties of a card and one set serving both says they cannot
# move apart. They can. A card may index the feed-in credit on the monthly
# EPEXDAM and still print an energy rate published in advance, which is
# exactly why the ENDEX101 products are in neither set, and the reverse is the
# ordinary shape at five other suppliers.
#
# Spelled out, because `frozenset(x)` on a frozenset returns THE SAME OBJECT.
# This was written as `frozenset(_EPEXDAM_INJECTION_CONTRACTS)` under a comment
# claiming the two could move apart, and they could not: adding an id to one
# literal moved both flags, and no test noticed. A second literal is the only
# form of this that is actually two sets.
_EPEXDAM_ENERGY_CONTRACTS: frozenset[str] = frozenset(
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
            # The EPEXDAM cards index BOTH legs on the delivery month, so the
            # one set drives both flags.
            spot_indexed_injection=c.contract_id in _EPEXDAM_INJECTION_CONTRACTS,
            month_indexed_energy=c.contract_id in _EPEXDAM_ENERGY_CONTRACTS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
)
