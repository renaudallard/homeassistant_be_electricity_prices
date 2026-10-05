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
needed. All nine residential electricity products are registered, and
every one but Impact (Wallonia only) is published in all three regions.

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
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._parse import require_contract
from ._validity import parse_valid_until
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
)
from ._rates import (
    ALL_REGIONS,
    Contract,
    TariffKind,
)
from ._totalenergies_cards import (
    _IMPACT_BANDS,
    _VAT_RE,
    _extract_connection_fee,
    _extract_energy,
    _extract_injection,
    _extract_publication_month,
    _without_renewables,
    in_french,
    is_dutch_card,
)
from ._totalenergies_overlays import (
    _energy_contribution_from_table,
    _extract_brussels_dsos,
    _extract_energy_contribution,
    _extract_energy_fund,
    _extract_federal_excise,
    _extract_flanders_dsos,
    _extract_renewables,
    _extract_wallonia_dsos,
    cev_included,
)

_BASE_URL = "https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WAL",
    REGION_BRUSSELS: "BXL",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # the file prefix in TotalEnergies's URL
    # The product name each edition of the card prints in its title. The
    # labels above are not it: "myComfort" is also how "myComfort Fixe" begins.
    french_title: str
    dutch_title: str
    # Regions the product is actually published in. TotalEnergies's
    # listing page advertises every product in V/W/B but a few only
    # have a Wallonia PDF; the others return a 200 OK HTML 404 page.
    regions: frozenset[str] = ALL_REGIONS


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "totalenergies_electricite_fixe",
        "TotalEnergies Electricité Fixe",
        "fixed",
        "ELECTRICITE-FIXE",
        "Electricité Fixe",
        "Elektriciteit Vast",
    ),
    _ContractDef(
        "totalenergies_electricite_variable",
        "TotalEnergies Electricité Variable",
        "variable",
        "ELECTRICITE-VARIABLE",
        "Electricité Variable",
        "Elektriciteit Variabel",
    ),
    _ContractDef(
        "totalenergies_impact",
        "TotalEnergies Impact",
        "variable",
        "IMPACT",
        "Impact Variable",
        "Impact Variabel",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef(
        "totalenergies_mycomfort",
        "TotalEnergies myComfort",
        "variable",
        "MYCOMFORT",
        "myComfort Variable",
        "myComfort Variabel",
    ),
    _ContractDef(
        "totalenergies_mycomfort_fixed",
        "TotalEnergies myComfort Fixe",
        "fixed",
        "MYCOMFORT-FIXED",
        "myComfort Fixe",
        "myComfort Vast",
    ),
    _ContractDef(
        "totalenergies_mydrive",
        "TotalEnergies myDrive",
        "variable",
        "MYDRIVE",
        "myDrive",
        "myDrive",
    ),
    _ContractDef(
        "totalenergies_mydynamic",
        "TotalEnergies myDynamic",
        "dynamic",
        "MYDYNAMIC",
        "myDynamic",
        "myDynamic",
    ),
    _ContractDef(
        "totalenergies_myessential",
        "TotalEnergies myEssential",
        "variable",
        "MYESSENTIAL",
        "myEssential Variable",
        "myEssential Variabel",
    ),
    _ContractDef(
        "totalenergies_myessential_fixed",
        "TotalEnergies myEssential Fixe",
        "fixed",
        "MYESSENTIAL-FIXED",
        "myEssential Fixe",
        "myEssential Vast",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


_LISTING_URL = (
    "https://totalenergies.be/fr/particuliers/electricite-et-gaz/cartes-tarifaires"
)


def _document_url(slug: str, region: str, language: str = "FR") -> str:
    region_code = _REGION_TO_CODE[region]
    return f"{_BASE_URL}/{slug}_ELECTRICITY_{region_code}_{language}.pdf"


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
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "TotalEnergies")
    if region not in _REGION_TO_CODE:
        raise ExtractorError(f"TotalEnergies: unknown region {region!r}")
    if region not in contract.regions:
        raise ExtractorError(
            f"TotalEnergies {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text_layout(session, url)
    try:
        return parse_snapshot(contract_id, text, region, url)
    except ExtractorError as err:
        # The French address can serve the wrong document: in October 2026
        # myComfort Fixe in Brussels served the injection card and myEssential
        # in Flanders a card with its green contribution left blank, while
        # the Dutch address served the right card. The Dutch card is read
        # when the French one does not parse, and the French error stands
        # when neither does. A Dutch fetch that failed transiently is
        # reported as such instead: it says nothing about either card, and
        # the French parse error would ask for a layout report on one blip.
        dutch = _document_url(contract.slug, region, "NL")
        try:
            text = await fetch_pdf_text_layout(session, dutch)
            return parse_snapshot(contract_id, text, region, dutch)
        except ExtractorError as dutch_err:
            if is_transient_fetch_error(str(dutch_err)):
                raise
            raise err from None


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "TotalEnergies")
    if is_dutch_card(text):
        _check_card(text, contract.dutch_title, _DUTCH_REGIONS[region], region)
        text = in_french(text)
    else:
        _check_card(text, contract.french_title, _FRENCH_REGIONS[region], region)

    columns = _meter_columns(text, contract)
    energy = _extract_energy(text, contract.kind, columns)
    included = cev_included(text)
    if included is not None:
        energy = _without_renewables(energy, included)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise = _extract_federal_excise(text)
    energy_contribution = _extract_energy_contribution(text)
    if energy_contribution is None:
        energy_contribution = _energy_contribution_from_table(text, region)
    if energy_contribution is None:
        # Neither the labelled line nor the DSO table fallback exposed the
        # row, so the card drifted. Fail loud rather than silently dropping
        # the contribution. A row that is PRESENT and reads zero is a valid
        # value since the levy fell to zero on 2026-08-01, so only a real
        # miss (None) raises here.
        raise ExtractorError("TotalEnergies: federal energy contribution not found")
    region_connection_fee = (
        _extract_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_renewables(text)
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        wallonia_renewables = _extract_renewables(text)
        dsos = _extract_wallonia_dsos(text)
    else:
        brussels_renewables = _extract_renewables(text)
        dsos = _extract_brussels_dsos(text)

    return with_vat_basis(
        SupplierSnapshot(
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
        ),
        printed_vat_rate(text, _VAT_RE),
    )


# How each edition names the region in its "Elektriciteit in het ..." or
# "Électricité en Région ..." line.
_DUTCH_REGIONS: dict[str, str] = {
    REGION_FLANDERS: r"Elektriciteit\s+in\s+het\s+Vlaamse\s+Gewest",
    REGION_WALLONIA: r"Elektriciteit\s+in\s+het\s+Waalse\s+Gewest",
    REGION_BRUSSELS: r"Elektriciteit\s+in\s+het\s+Brussels\s+Hoofdstedelijk\s+Gewest",
}
_FRENCH_REGIONS: dict[str, str] = {
    REGION_FLANDERS: r"[ÉE]lectricit[ée]\s+en\s+R[ée]gion\s+flamande",
    REGION_WALLONIA: r"[ÉE]lectricit[ée]\s+en\s+R[ée]gion\s+wallonne",
    REGION_BRUSSELS: r"[ÉE]lectricit[ée]\s+en\s+R[ée]gion\s+de\s+Bruxelles-Capitale",
}


def _check_card(text: str, title: str, place: str, region: str) -> None:
    """Refuse a card that is not this product's electricity card for this
    region.

    TotalEnergies' October 2026 uploads put cards at the wrong address: the
    Dutch Brussels address of Electricité Variable served myEssential
    Variabel, the Dutch Flemish one of myComfort served myComfort Vast and
    the Dutch Brussels one of myEssential a gas card. The layout is shared,
    so such a card would parse, and a French address serving a sibling's
    card would bill that product's rates and fee without a word. Its title
    and region line say what it is.
    """
    words = r"\s+".join(map(re.escape, title.split()))
    if not re.search(rf"Total\s?Energies\s+{words}(?!\w)", text):
        raise ExtractorError(f"TotalEnergies: the card is not {title}")
    if not re.search(place, text):
        raise ExtractorError(
            f"TotalEnergies: the card is not the {region} electricity card"
        )


# The meter columns' header, ending on the exclusive-night one where the card
# prints it. The October 2026 myDynamic cards drop that column and print
# "19,37 19,37 19,37 Tarif mensuel" with the fee on the next line.
_EXCL_NIGHT_HEADER_RE = re.compile(
    r"Heures\s+creuses\s+excl\.\s*nuit\s*\n\s*Consommation"
)


def _meter_columns(text: str, contract: _ContractDef) -> int:
    """How many rates the consumption row prints: three CWaPE bands on
    Impact, three meter columns on a dynamic card without the exclusive-night
    one, four otherwise."""
    if contract.slug == "IMPACT":
        return _IMPACT_BANDS
    if contract.kind == "dynamic" and not _EXCL_NIGHT_HEADER_RE.search(text):
        return 3
    return 4


# The variable products whose energy leg is a BELPEXM_RLP formula the parser
# reads, so the re-price needs spots the kind never collects a key for. Every
# variable card is one, Impact included: it prints the pair once per CWaPE
# band rather than once per meter column, and solves to the same index as the
# other four. The fixed cards and myDynamic are neither.
_MONTH_INDEXED_ENERGY: frozenset[str] = frozenset(
    {
        "totalenergies_electricite_variable",
        "totalenergies_impact",
        "totalenergies_mycomfort",
        "totalenergies_mydrive",
        "totalenergies_myessential",
    }
)


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=12.8,
    id="totalenergies",
    label="TotalEnergies",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            # Every non-dynamic card indexes its feed-in credit on the
            # monthly Belpex_M, which the fixed / variable energy leg never
            # fetches spots for. myDynamic collects the key via its own
            # BELPEXH energy formula.
            spot_indexed_injection=c.kind != "dynamic",
            month_indexed_energy=c.contract_id in _MONTH_INDEXED_ENERGY,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
