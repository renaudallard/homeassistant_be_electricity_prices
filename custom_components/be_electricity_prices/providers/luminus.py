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
and Wallonia; Brussels carries only the regulated Social tariff, which
is priced by the social tariff supplier (social.py), not here.

Energy prices, distribution rows and renewables surcharges all vary
between V and W on every product, so the extractor fetches exactly the
configured region's PDF and never merges. Prices are 6% VAT inclusive
in the printed values; the Dynamic formula is hors TVA so factor and
base are scaled by the parsed VAT multiplier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import date

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._luminus_cards import (
    _VAT_PATTERNS,
    _extract_energy,
    _extract_injection,
    _extract_promo,
    published_indices,
)
from ._luminus_overlays import (
    _extract_energy_fund,
    _extract_flanders_dsos,
    _extract_flanders_renewables,
    _extract_per_kwh_taxes,
    _extract_wallonia_dsos,
    _extract_wallonia_renewables,
)
from ._parse import (
    require_contract,
)
from ._pdf import (
    FR_MONTHS,
    fetch_pdf_text,
    fetch_text,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    TariffKind,
)
from ._settle import settled_energy, settled_injection
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
)

_API_URL = "https://www.luminus.be/api-next/get-pricelist/"

# The price-list archive behind the site's "Archives de listes de prix" app:
# one call names the products a month had (with the Salesforce-style product
# id the PDF endpoint wants), the other serves the PDF for one product, month
# and region. Sixty-one months on the app's own picker, back to September
# 2021. The current month is on it too, but it is not asked for: see
# fetch_for_month.
_ARCHIVE_PRODUCTS_URL = "https://www.luminus.be/api/pricelist/products"
_ARCHIVE_PDF_URL = "https://www.luminus.be/api/pricelist/pdf"

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
    # Luminus Sociaal/Social is not a product here: the social tariff is the
    # CREG's, the same at every supplier, and is priced by the social tariff
    # supplier (social.py), which reads this card only for its feed-in.
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

# A <loc> in a sitemap or a sitemap index. Luminus re-sharded: what used to be
# one flat sitemap is now a 709-byte <sitemapindex> naming five children, and
# the product pages moved into sitemap-products.xml. One level is followed, not
# recursed: the index names sitemaps and a sitemap names pages, so a second
# level would be a sitemap of sitemaps, which no generator emits.
_SITEMAP_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SITEMAP_INDEX_RE = re.compile(r"<sitemapindex\b", re.IGNORECASE)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Discover Luminus products from the public sitemap.

    The /fr/particuliers/tarifs-energie/<slug>/ structure is the
    canonical product directory. Every slug there is a product
    (residential + market only). Excludes the regulated social
    tariff, which the social tariff supplier prices.

    Follows a sitemap INDEX one level, because Luminus re-sharded and
    ``sitemap.xml`` is now 709 bytes naming five children. Against a flat
    sitemap that check is false and nothing changes; against the index it is
    the difference between eleven product slugs and none. Discovery returning
    nothing is only a warning in the catalog check, so this went unnoticed:
    measured on the released 0.27.2 as well, it has been blind for as long as
    the split has been live.
    """
    try:
        xml = await fetch_text(session, _SITEMAP_URL)
    except ExtractorError:
        return set()
    documents = [xml]
    if _SITEMAP_INDEX_RE.search(xml):
        documents = []
        for child in _SITEMAP_LOC_RE.findall(xml):
            try:
                documents.append(await fetch_text(session, child))
            except ExtractorError:
                # One unreadable child is not a reason to report no products
                # at all; the others still name theirs.
                continue
    return {
        slug
        for document in documents
        for slug in _PRODUCT_PAGE_RE.findall(document)
        if slug not in _EXCLUDED_SLUGS
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Luminus")
    if region not in _REGION_TO_TAB:
        raise ExtractorError(
            f"Luminus {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text(session, url)

    async def _current(sibling: _ContractDef) -> str | None:
        return await fetch_pdf_text(session, _document_url(sibling.slug, region))

    return await _with_sibling_prosumer(
        parse_snapshot(contract_id, text, region, url), text, region, _current
    )


# Cards asked, in order, for the Walloon prosumer rate a card names but does
# not print. Each prints the static seven-column table with the rate in its
# last column, and the rate is the DSO's, not the product's.
_PROSUMER_SIBLINGS: tuple[str, ...] = (
    "luminus_comfyflex",
    "luminus_maxxflex",
    "luminus_basicflex",
)
_PROSUMER_NAMED_RE = re.compile(r"tarif\s+prosumer", re.IGNORECASE)


async def _with_sibling_prosumer(
    snap: SupplierSnapshot,
    text: str,
    region: str,
    load: Callable[[_ContractDef], Awaitable[str | None]],
) -> SupplierSnapshot:
    """Fill the Walloon prosumer rate a card names but does not print.

    SmartFlex's October 2026 card in Wallonia prints the Impact triplet where
    its September card printed the prosumer column, yet its footnote still
    bills the tariff: "si vous disposez d'un compteur bidirectionnel, la
    redevance reseau peut egalement etre calculee sur la base du prelevement
    brut d'electricite, si ce calcul est plus avantageux que la facturation du
    tarif prosumer". So a household under compensation still owes it, and an
    overlay without it billed that household nothing. The rate is the DSO's,
    so it is taken from a sibling card of the same region and month. Dynamic's
    card names no prosumer tariff at all and is left as it is.

    A transient failure on a sibling propagates, so the fetch is retried; a
    sibling that is missing or unreadable passes to the next, and the card
    fails to parse when none of them carries the rate.
    """
    if (
        region != REGION_WALLONIA
        or not snap.dsos
        or any(o.prosumer_eur_per_kva_year is not None for o in snap.dsos.values())
        or _PROSUMER_NAMED_RE.search(text) is None
    ):
        return snap
    for sibling_id in _PROSUMER_SIBLINGS:
        try:
            sibling_text = await load(_CONTRACTS_BY_ID[sibling_id])
            if sibling_text is None:
                continue
            rates = {
                key: overlay.prosumer_eur_per_kva_year
                for key, overlay in _extract_wallonia_dsos(sibling_text).items()
            }
        except ExtractorError as err:
            if is_transient_fetch_error(str(err)):
                raise
            continue
        if all(rates.get(key) is not None for key in snap.dsos):
            return replace(
                snap,
                dsos={
                    key: replace(overlay, prosumer_eur_per_kva_year=rates[key])
                    for key, overlay in snap.dsos.items()
                },
            )
    raise ExtractorError(
        "Luminus: no sibling card prints the Walloon prosumer rate this card bills"
    )


def _archive_product_name(name: str) -> str:
    """Fold an archive product label onto a contract label.

    The archive names products "Luminus Comfy Electricité", "Luminus BasicFix
    Online Electricité" or "Luminus Dynamic Online Electricité" where the
    catalogue says "Luminus Comfy", "Luminus BasicFix" and "Luminus Dynamic":
    the energy word and the online marker are the only differences, and the
    ids are opaque, so the label is the join.
    """
    folded = name.lower()
    for word in (
        " electricité",
        " électricité",
        " electricite",
        " elektriciteit",
        " online",
    ):
        folded = folded.replace(word, "")
    return " ".join(folded.split())


async def _resolve_archive_product_id(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region: str,
    month_first: date,
) -> str | None:
    """The archive's product id for ``contract`` in ``region`` for one month,
    or ``None`` when that month lists no such product.

    Every shape the endpoint can answer with is funnelled into ExtractorError,
    for the reason the current-card resolver of a sibling extractor gives: a
    payload that is JSON but not the expected shape must read as a failed
    fetch rather than escape as a TypeError.
    """
    url = (
        f"{_ARCHIVE_PRODUCTS_URL}?language=FR&customerSegment=Residential"
        f"&energyType=Electricity&region={_REGION_TO_TAB[region]}"
        f"&signing={month_first.year:04d}-{month_first.month:02d}"
    )
    body = await fetch_text(session, url, timeout=15)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise ExtractorError(f"Luminus archive parse error: {err}") from err
    if not isinstance(payload, list):
        raise ExtractorError("Luminus archive parse error: expected a product list")
    wanted = _archive_product_name(contract.label)
    for row in payload:
        if not isinstance(row, dict):
            continue
        if _archive_product_name(str(row.get("Product", ""))) != wanted:
            continue
        product_id = row.get("ProductId")
        if isinstance(product_id, str) and product_id:
            return product_id
    return None


def _archive_pdf_url(product_id: str, first: date, region: str) -> str:
    return (
        f"{_ARCHIVE_PDF_URL}?language=FR&productId={product_id}"
        f"&date={first.year:04d}-{first.month:02d}"
        f"&region={_REGION_TO_TAB[region]}&inline=true"
    )


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card Luminus published for one past month, or ``None``.

    Behind the site's price-list archive: the month's product list gives the
    id, the PDF endpoint serves that product's card for the month and region.
    The PDF comes with a UTF-8 byte-order mark in front of the magic bytes,
    which the shared fetch helper already tolerates. The cards print a
    validity date, which is the authoritative cross-check; the month name in
    the title is the fallback.

    Until this existed a contract start date did nothing on a Luminus entry:
    the signing-cohort splice had no card to read, and every past month of the
    year-to-date billed on the current card as a proxy. Every failure comes
    back as ``None``, the one-month answer the month cache expects.

    The running month is refused, as Mega's archive refuses it. The archive
    keeps serving a month's first edition after the live card is corrected:
    on 5 October 2026 it still had the October cards of the 1st, priced on
    August's Belpex, where the live ones had been reissued on the 2nd on
    September's, 4,3 c/kWh apart on ComfyFlex. Answered from here, the month
    cost billed that first edition while current_price billed the reissue.
    ``None`` makes the live card the running month's, which it is.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in _REGION_TO_TAB:
        return None
    first = date(year_month.year, year_month.month, 1)
    if first >= dt_util.now().date().replace(day=1):
        return None
    try:
        product_id = await _resolve_archive_product_id(session, contract, region, first)
        if product_id is None:
            return None
        url = _archive_pdf_url(product_id, first, region)
        text = await fetch_pdf_text(session, url)

        async def _archived(sibling: _ContractDef) -> str | None:
            sibling_id = await _resolve_archive_product_id(
                session, sibling, region, first
            )
            if sibling_id is None:
                return None
            return await fetch_pdf_text(
                session, _archive_pdf_url(sibling_id, first, region)
            )

        snap = await _with_sibling_prosumer(
            parse_snapshot(contract_id, text, region, url), text, region, _archived
        )
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    return archive_validity_check(snap, text, first, month_names=FR_MONTHS)


async def settle_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
    held: SupplierSnapshot,
) -> SupplierSnapshot:
    """``held``, the card of a closed month, re-priced on the index the next
    card names for it.

    The cards print their month-indexed rates at the previous month's
    index, "Les prix affiches sont calcules sur la base de la derniere
    valeur Belpex connue (mois precedent)", and the next card names the
    month's own: "Belpex = 156,41 EUR/MWh (valeur de l'indice de septembre
    2026)", and "Belpex RLP M" for BasicFlex. A keyless entry billed every
    closed month a month behind: BasicFlex 3,26 c/kWh under in September
    2026, 2,61 over in February.

    In place, never by replacing the card: Luminus's archive serves a month
    without its new-customer campaign, and on 5 October 2026 it still served
    the first October edition the live card had corrected. So the energy
    formula rows (MaxxFlex, SmartFlex, BasicFlex) and the monthly feed-in
    are re-priced, and every other field of ``held`` stays.

    The next card is the live one while its month runs and the archive's
    after. Not out yet, or naming another month while it is the live one
    (the live card of the 1st was a month behind in October 2026), and the
    month is ``provisional`` so it is asked again. A next card of a closed
    month that names another month, or no index a leg needs, settles nothing.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in _REGION_TO_TAB:
        return held
    first = date(year_month.year, year_month.month, 1)
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    running = dt_util.now().date().replace(day=1)
    if following > running:
        return replace(held, provisional=True)
    live = following == running
    try:
        if live:
            text = await fetch_pdf_text(session, _document_url(contract.slug, region))
        else:
            product_id = await _resolve_archive_product_id(
                session, contract, region, following
            )
            if product_id is None:
                return held
            text = await fetch_pdf_text(
                session, _archive_pdf_url(product_id, following, region)
            )
    except ExtractorError as err:
        if is_transient_fetch_error(str(err)):
            raise
        return replace(held, provisional=True) if live else held
    named = {
        name: value
        for name, (month, value) in published_indices(text).items()
        if month == first
    }
    energy, injection = held.energy, held.injection
    energy_index = injection_index = None
    if getattr(energy, "month_indexed", False):
        rlp = getattr(energy, "rlp_indexed", False)
        energy_index = named.get("Belpex RLP M" if rlp else "Belpex")
        if energy_index is None:
            return replace(held, provisional=True) if live else held
    if injection is not None and injection.month_indexed:
        injection_index = named.get("Belpex")
        if injection_index is None:
            return replace(held, provisional=True) if live else held
    return replace(
        held,
        energy=energy if energy_index is None else settled_energy(energy, energy_index),
        injection=(
            injection
            if injection is None or injection_index is None
            else settled_injection(injection, injection_index)
        ),
    )


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _API_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Luminus")

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, connection_fee = _extract_per_kwh_taxes(text)
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text, contract.kind)
    else:
        wallonia_renewables = _extract_wallonia_renewables(text)
        dsos = _extract_wallonia_dsos(text)

    return with_vat_basis(
        SupplierSnapshot(
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
            **_extract_promo(text, publication_label),  # type: ignore[arg-type]
        ),
        printed_vat_rate(text, *_VAT_PATTERNS),
    )


# ---- promotional credits -----------------------------------------------------


# ---- energy + tax block -------------------------------------------------------


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


# ---- DSO row parsers ----------------------------------------------------------


_LUMINUS_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

# Contracts whose card indexes the feed-in credit MONTHLY, so the credit needs
# ENTSO-E spots their own energy leg does not fetch. ComfyFlex and ComfyFlex+
# are absent on purpose: they print the same formula and index it quarterly,
# and there is no quarterly mean here to resolve it against. Dynamic collects
# its key through its own Belpex H energy formula.
_MONTHLY_INJECTION_CONTRACTS: frozenset[str] = frozenset(
    {
        "luminus_comfy",
        "luminus_comfy_plus",
        "luminus_maxxfix",
        "luminus_maxxflex",
        "luminus_basicfix",
        "luminus_basicflex",
        "luminus_smartflex",
    }
)
# Contracts whose ENERGY is a monthly formula too: MaxxFlex prints one per
# meter and SmartFlex one per band, both on the delivery month's Belpex, and
# BasicFlex one per meter on the delivery month's Belpex RLP M. ComfyFlex and
# ComfyFlex+ index theirs on a quarter, so their energy is billed as printed.
_MONTHLY_ENERGY_CONTRACTS: frozenset[str] = frozenset(
    {"luminus_maxxflex", "luminus_smartflex", "luminus_basicflex"}
)


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=1.1,
    id="luminus",
    label="Luminus",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_LUMINUS_REGIONS,
            spot_indexed_injection=c.contract_id in _MONTHLY_INJECTION_CONTRACTS,
            month_indexed_energy=c.contract_id in _MONTHLY_ENERGY_CONTRACTS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    settles_on_next_card=True,
    settle_month=settle_month,
)
