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

"""Mega Belgium tariff card extractor.

Mega publishes monthly tariff cards under predictable filenames at:

    https://my.mega.be/resources/tarif/Mega-FR-EL-B2C-<REGION>-<MMYYYY>-<SUFFIX>.pdf

The MMYYYY rolls every month and the product SUFFIX (e.g. ``Smart0104``,
``Smart2204-Fixed``, ``Cap0104``) carries an internal launch-date code
that drifts when Mega launches new product variants. To resolve a stable
``(contract, region)`` pair to its current PDF without hardcoding any
suffix, the extractor scrapes the public listing page at
``mega.be/fr/energie/cartes-tarifaires``: every product card carries a
``data-product-element="<Product Name>"`` anchor pointing at that
month's PDF, so finding the right URL is a simple regex match. When the
listing drops one product's block for a single region while the card
itself stays published (as it did for Dynamic Wallonia in July 2026,
#42), the resolver rewrites a sibling region's URL, since the three
regional editions differ only by the ``-B2C-<REGION>-`` filename
segment; parse_snapshot then re-checks the card's own region header so a
wrong guess fails loud instead of mis-pricing.

All eleven residential electricity products are registered. Mega
serves all three regions (Flanders, Wallonia, Brussels) for every
product except Off-peak Impact, which is Wallonia-only because it
requires the CWaPE Tarif réseau IMPACT plus an SMR3 smart meter
(both Wallonia-specific). The Tarif Social variant is omitted on
purpose, same reasoning as Engie/Luminus (regulated CREG tariff,
auto-assigned, no DSO breakdown).

The Dynamic formula uses a different convention than Engie/Luminus:
``Day Ahead Epex Spot * 1.05 + 1.35 c€/kWh`` where the spot is already
in c€/kWh and the result is TVAC, so factor and base are scaled
straight to EUR/kWh without a VAT multiplier.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from datetime import date, timedelta

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    VAT_RATE_STANDARD,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    require_contract,
    archive_validity_check,
    fetch_pdf_text,
    fetch_text,
    fold_accents,
    parse_valid_until,
    tier_bound_kwh,
    to_float,
)
from ._mega_overlays import (
    _extract_brussels_dsos,
    _extract_connection_fee,
    _extract_energy_contribution,
    _extract_energy_fund,
    _extract_federal_excise,
    _extract_flanders_dsos,
    _extract_flanders_renewables,
    _extract_pro_excise_tiers,
    _extract_renewables,
    _extract_supplier_prosumer,
    _extract_wallonia_dsos,
)
from ._mega_cards import (
    _FR_MONTH_NAMES,
    _extract_energy,
    _extract_injection,
    _extract_publication_month,
    _extract_valid_until,
    _realized_rates,
)
from .base import (
    CardNotReadableError,
    ALL_REGIONS,
    Contract,
    ExtractorError,
    ImpactRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)

_LISTING_URL = "https://www.mega.be/fr/energie/cartes-tarifaires"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WL",
    REGION_BRUSSELS: "BX",
}

# The header label each regional edition prints under "Carte tarifaire".
# _assert_card_region uses it to reject a card whose region does not match
# the one requested, since parse_snapshot applies region-specific DSO and
# levy overlays.
_REGION_CARD_LABELS: dict[str, str] = {
    REGION_FLANDERS: "Flandre",
    REGION_WALLONIA: "Wallonie",
    REGION_BRUSSELS: "Bruxelles",
}

# The region code is the only part of a card's filename that differs
# between the three regional editions of one product, so a missing listing
# block can be resolved off a sibling region's URL. Anchor on the fixed
# -B2C-<CODE>-<MMYYYY>- shape, the same grammar fetch_for_month's month
# rewrite relies on.
_REGION_SEGMENT_RE = re.compile(r"(?<=-B2C-)(?:BX|VL|WL)(?=-\d{6}-)")


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    product_name: str  # the data-product-element value Mega uses on its site
    # Regions the product is actually published in. Defaults to all
    # three; Off-peak Impact is Wallonia-only because it requires the
    # CWaPE IMPACT DSO tariff (Wallonia-specific).
    regions: frozenset[str] = ALL_REGIONS
    # B2C (residential) or B2B (professional), the segment in the card's
    # filename. The professional cards are absent from the public listing,
    # so a B2B contract also carries the filename tokens needed to build
    # its URL directly; see _pro_pdf_url.
    segment: str = "B2C"
    file_family: str = ""  # "Smart", "Cosy", "Dynamic", ...
    file_variant: str = ""  # "-Fixed" or empty

    @property
    def professional(self) -> bool:
        return self.segment == "B2B"


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "mega_smart_fixed", "Mega Smart Fixed (2 years)", "fixed", "Smart Fixed"
    ),
    _ContractDef(
        "mega_smart_flex", "Mega Smart Flex (2 years)", "variable", "Smart Flex"
    ),
    # Mega discontinued "Zen Fixed" (August 2026): the listing dropped the
    # product block in all three regions at once, so the sibling-region
    # rewrite below has nothing to borrow from. The card is not simply
    # mislaid - Wallonia resolves to the CDN's HTML stub, i.e. it was never
    # published there this month. discover() re-surfaces it if Mega revives
    # it. Same treatment as "Off-peak Fixed" below.
    _ContractDef("mega_online_fixed", "Mega Online Fixed", "fixed", "Online Fixed"),
    _ContractDef("mega_online_flex", "Mega Online Flex", "variable", "Online Flex"),
    _ContractDef("mega_cosy_fixed", "Mega Cosy Fixed", "fixed", "Cosy Fixed"),
    _ContractDef("mega_cosy_flex", "Mega Cosy Flex", "variable", "Cosy Flex"),
    # Mega pulled "Off-peak Fixed" in July 2026 and brought it back for the
    # August 2026 card, in all three regions and with a B2B edition, which is
    # the catalog check doing exactly what it exists for. The card parses on
    # the existing fixed path with no parser change.
    _ContractDef(
        "mega_offpeak_fixed", "Mega Off-peak Fixed", "fixed", "Off-peak Fixed"
    ),
    _ContractDef(
        "mega_offpeak_flex", "Mega Off-peak Flex", "variable", "Off-peak Flex"
    ),
    _ContractDef(
        "mega_offpeak_impact_var",
        "Mega Off-peak Impact",
        "tou_impact",
        "Off-peak Impact",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef("mega_dynamic", "Mega Dynamic", "dynamic", "Dynamic"),
    _ContractDef("mega_cap", "Mega Cap", "variable", "Mega Cap"),
    # The professional editions. Mega publishes these to the same CDN but
    # never links them from the public listing, so they are addressed by
    # building the filename. Online Flex, Off-peak Flex and Off-peak Impact
    # have no B2B card; Off-peak Fixed gained one when it returned in August
    # 2026, and Zen Fixed has one even though Mega retired the residential
    # edition that month.
    _ContractDef(
        "mega_pro_offpeak_fixed",
        "Mega Off-peak Fixed (pro)",
        "fixed",
        "Off-peak Fixed",
        segment="B2B",
        file_family="Offpeak-Bi",
        file_variant="-Fix",
    ),
    _ContractDef(
        "mega_pro_smart_fixed",
        "Mega Smart Fixed (pro)",
        "fixed",
        "Smart Fixed",
        segment="B2B",
        file_family="Smart",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_smart_flex",
        "Mega Smart Flex (pro)",
        "variable",
        "Smart Flex",
        segment="B2B",
        file_family="Smart",
    ),
    _ContractDef(
        "mega_pro_online_fixed",
        "Mega Online Fixed (pro)",
        "fixed",
        "Online Fixed",
        segment="B2B",
        file_family="Online",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_cosy_fixed",
        "Mega Cosy Fixed (pro)",
        "fixed",
        "Cosy Fixed",
        segment="B2B",
        file_family="Cosy",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_cosy_flex",
        "Mega Cosy Flex (pro)",
        "variable",
        "Cosy Flex",
        segment="B2B",
        file_family="Cosy",
    ),
    _ContractDef(
        "mega_pro_dynamic",
        "Mega Dynamic (pro)",
        "dynamic",
        "Dynamic",
        segment="B2B",
        file_family="Dynamic",
    ),
    _ContractDef(
        "mega_pro_cap",
        "Mega Cap (pro)",
        "variable",
        "Mega Cap",
        segment="B2B",
        file_family="Cap",
    ),
    _ContractDef(
        "mega_pro_zen_fixed",
        "Mega Zen Fixed (pro)",
        "fixed",
        "Zen Fixed",
        segment="B2B",
        file_family="Zen",
        file_variant="-Fixed",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Product names Mega lists on the public catalog page that this
# integration intentionally does not model. The daily live-check
# subtracts both _CONTRACTS and this set from the discovered list, so
# truly new residential electricity products surface as actionable
# signal while these stay quiet.
#
#   * Prepaid Fixed / Prepaid Flex -- topup-card products with a
#     different billing model (no monthly invoice, no recorder-backed
#     consumption sensors), out of scope for the Energy-dashboard
#     integration.
_KNOWN_UNSUPPORTED_PRODUCTS: frozenset[str] = frozenset(
    {
        "Prepaid Fixed",
        "Prepaid Flex",
    }
)


# ---- listing HTML -> PDF URL --------------------------------------------------


def _find_pdf_url(listing_html: str, product_name: str, region_code: str) -> str | None:
    """Find the current month's electricity PDF URL for product+region.

    The listing HTML structure repeats per product: each `<a data-product-
    element="<Product Name>" ... href="<PDF URL>">` carries an electricity
    or gas link. We pin the regex to ``Mega-FR-EL-B2C-<REGION>-`` so the
    gas links (``Mega-FR-NG-...``) and other-region links don't match.
    """
    pattern = re.compile(
        r'data-product-element="' + re.escape(product_name) + r'"[^>]*?'
        r'href="(https://my\.mega\.be/resources/tarif/'
        r"Mega-FR-EL-B2C-" + region_code + r"-\d{6}-[^\"]+\.pdf)\"",
        re.S,
    )
    match = pattern.search(listing_html)
    return match.group(1) if match else None


def _resolve_pdf_url(
    listing_html: str, product_name: str, region_code: str
) -> str | None:
    """The current PDF URL for product+region, via the listing.

    Mega intermittently drops a single (product, region) block from the
    listing while still publishing the card: in July 2026 the Dynamic
    Wallonia block vanished overnight and its PDF was untouched (#42). The
    three regional editions differ only by the -B2C-<CODE>- segment, so
    rewrite a sibling's URL rather than dead-ending a healthy entry.

    The rewrite is only a guess at the URL. parse_snapshot re-checks the
    card's own region header, and a product genuinely not published in the
    region resolves to the CDN's HTML stub, which fetch_pdf_text rejects,
    so a wrong guess fails loud instead of mis-pricing.
    """
    url = _find_pdf_url(listing_html, product_name, region_code)
    if url is not None:
        return url
    for sibling_code in _REGION_TO_CODE.values():
        if sibling_code == region_code:
            continue
        sibling_url = _find_pdf_url(listing_html, product_name, sibling_code)
        if sibling_url is None:
            continue
        rewritten, count = _REGION_SEGMENT_RE.subn(region_code, sibling_url, count=1)
        if count:
            _LOGGER.warning(
                "Mega listing has no %s block for %r; resolved %s from the %s edition",
                region_code,
                product_name,
                rewritten,
                sibling_code,
            )
            return rewritten
    return None


async def _fetch_listing_html(session: aiohttp.ClientSession) -> str:
    return await fetch_text(session, _LISTING_URL)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every ``data-product-element`` value from Mega's listing,
    minus the products this integration deliberately doesn't model.

    Best-effort catalog discovery for the daily live-check: the diff
    against ``{c.product_name for c in _CONTRACTS}`` flags any new Mega
    product that should be added to the registry. Filtering out
    :data:`_KNOWN_UNSUPPORTED_PRODUCTS` keeps prepaid (topup-card)
    products from re-opening the same catalog issue every day.
    """
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return set()
    found = set(re.findall(r'data-product-element="([^"]+)"', listing))
    return found - _KNOWN_UNSUPPORTED_PRODUCTS


# ---- top-level fetch + parser -------------------------------------------------


_CDN_BASE = "https://my.mega.be/resources/tarif/"


def _pro_pdf_url(c: _ContractDef, region_code: str, year_month: date) -> str:
    """Build the professional card's URL for a month.

    Mega serves the B2B cards from the same CDN as the residential ones
    but never links them from the public listing, so there is nothing to
    scrape: the filename is the residential grammar with the B2B segment,

        Mega-FR-EL-B2B-<REGION>-<MMYYYY>-<Family>01<MM>[-<Variant>].pdf

    where the 01<MM> is the card's validity start, always the first of its
    month. A month Mega has not published resolves to the CDN's HTML stub,
    which fetch_pdf_text rejects, so a wrong guess fails loud.
    """
    mm = f"{year_month.month:02d}"
    return (
        f"{_CDN_BASE}Mega-FR-EL-B2B-{region_code}-{mm}{year_month.year}-"
        f"{c.file_family}01{mm}{c.file_variant}.pdf"
    )


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: the resolved PDF URL for (contract, region).

    Mega's listing has neither Last-Modified nor ETag, so the cheapest
    reliable probe is a listing GET + filename match. The URL contains
    the publication month (MMYYYY) so it changes whenever Mega rotates.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    region_code = _REGION_TO_CODE.get(region)
    if contract is None or region_code is None:
        return None
    if contract.professional:
        # No listing carries the professional cards, and the built URL
        # only changes at a month boundary - returning it would pin the
        # snapshot for a whole month and swallow a mid-month re-publish.
        # Fall back to the time-based TTL instead.
        return None
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return None
    return _resolve_pdf_url(listing, contract.product_name, region_code)


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Mega")
    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        raise ExtractorError(f"Mega: unknown region {region!r}")

    if contract.professional:
        today = dt_util.now().date()
        pdf_url = _pro_pdf_url(contract, region_code, today)
        try:
            text = await fetch_pdf_text(session, pdf_url)
        except CardNotReadableError:
            # Downloaded fine but has no text layer: not an unpublished
            # card, so it must surface rather than silently roll back a
            # month. Three of these professional contracts are variable
            # and one is dynamic, so last month's card carries last
            # month's index -- the prices would be wrong, not just old.
            raise
        except ExtractorError:
            # Early in a month Mega can lag a day or two before the new
            # card lands; the one still in force is last month's.
            previous = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
            pdf_url = _pro_pdf_url(contract, region_code, previous)
            text = await fetch_pdf_text(session, pdf_url)
        return parse_snapshot(contract_id, text, region, pdf_url)

    listing = await _fetch_listing_html(session)
    listed_url = _resolve_pdf_url(listing, contract.product_name, region_code)
    if listed_url is None:
        raise ExtractorError(
            f"Mega {contract_id}: no listing entry for region {region!r}"
        )
    text = await fetch_pdf_text(session, listed_url)
    return parse_snapshot(contract_id, text, region, listed_url)


async def _archive_pdf_url(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region_code: str,
    year_month: date,
    *,
    allow_current: bool = False,
) -> str | None:
    """The CDN URL of ``contract``'s card for one month, or None.

    Professional cards never appear in the public listing, so they take the
    same built filename ``fetch`` uses with the requested month. Residential
    ones resolve the current URL from the listing and rewrite BOTH month
    placeholders -- the ``-MMYYYY-`` segment and the ``<MM>`` half of the
    product's effective-date ``<DD><MM>`` suffix -- while preserving the
    effective day, which is not the 1st for every product.
    """
    if contract.professional:
        return _pro_pdf_url(contract, region_code, year_month)
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return None
    current_url = _resolve_pdf_url(listing, contract.product_name, region_code)
    if current_url is None:
        return None
    mmyyyy_re = re.compile(r"-(\d{2})\d{4}-(?=[^/]*\.pdf$)")
    mmyyyy_match = mmyyyy_re.search(current_url)
    if mmyyyy_match is None:
        return None
    current_mm = mmyyyy_match.group(1)
    target_mm = f"{year_month.month:02d}"
    historical_mmyyyy = f"{target_mm}{year_month.year}"
    new_url = mmyyyy_re.sub(f"-{historical_mmyyyy}-", current_url, count=1)
    # Cap0106 -> Cap0105, Online0106-Fixed -> Online0105-Fixed, Cosy1306 ->
    # Cosy1305. A product whose publication day varies month to month
    # resolves to the CDN's HTML stub, which the PDF magic-byte check
    # rejects, so the walk falls back to the proxy rather than mis-billing.
    prefix, sep, tail = new_url.partition(f"-{historical_mmyyyy}-")
    if sep:
        tail = re.sub(
            rf"(\d{{2}}){current_mm}(?=[-.])",
            rf"\g<1>{target_mm}",
            tail,
            count=1,
        )
        new_url = prefix + sep + tail
    # A rewrite that lands back on the listing's own URL means the requested
    # month IS the current one. fetch_for_month refuses that, so a current
    # card can never be served as a historical month. The realized-rate
    # lookup wants it though: for the most recently completed month, the
    # card carrying its billed figures is precisely the current one.
    if new_url == current_url and not allow_current:
        return None
    return new_url


def _next_month(year_month: date) -> date:
    """First day of the month after ``year_month``."""
    if year_month.month == 12:
        return date(year_month.year + 1, 1, 1)
    return date(year_month.year, year_month.month + 1, 1)


async def _realized_rates_for_month(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region: str,
    region_code: str,
    year_month: date,
) -> dict[str, float]:
    """Month ``year_month``'s BILLED rates, read off the NEXT month's card.

    A variable or Impact card's headline table is a 12-month simulation, and
    the "derniers prix constates ... pour le mois de <month>" sentence that
    overrides it names the month BEFORE the card's own: the June card reports
    May's regularisation figures. That is the only choice on the live path,
    where the current month's index does not exist yet -- but on the archive
    path it shifted every past month of the year-to-date walk by one, billing
    June at May's rate while June's real rate sat unread on the July card.

    So read the sentence from the M+1 card. Returns an empty mapping when that
    card is not out yet (M is the current month) or does not resolve, and the
    caller then keeps the M card's own figures, which is what it did before.
    """
    following = _next_month(year_month)
    if following > date(dt_util.now().year, dt_util.now().month, 1):
        return {}
    url = await _archive_pdf_url(
        session, contract, region_code, following, allow_current=True
    )
    if url is None:
        return {}
    try:
        text = await fetch_pdf_text(session, url)
        following_snap = parse_snapshot(contract.contract_id, text, region, url)
    except ExtractorError:
        return {}
    # Confirm the fetched card really is the following month's before trusting
    # its sentence: the same validity check the main path runs, so a CDN stub
    # or a stale issue served under a historical URL cannot shift the rates by
    # another month instead of simply not applying.
    if (
        archive_validity_check(
            following_snap, text, following, month_names=_FR_MONTH_NAMES
        )
        is None
    ):
        return {}
    return _realized_rates(text)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Mega card for a specific ``(year, month)``.

    Mega's CDN keeps every monthly issue under a stable URL pattern:
    ``Mega-FR-EL-B2C-<REGION>-<MMYYYY>-<Product><DD><MM>[-<Variant>].pdf``.
    The month appears twice -- the ``<MMYYYY>`` segment and the ``<MM>``
    half of the product's effective-date ``<DD><MM>`` suffix -- and both
    must rotate while the effective day ``<DD>`` is preserved (most
    products publish on the 1st, but some, e.g. Cosy, use another day).
    The suffix can sit mid-token before a ``-Fixed`` / ``-Green`` /
    ``-Fix`` variant, so the rewrite can't anchor on ``.pdf``. Resolve
    the current URL via the listing first, then swap both month
    placeholders.

    The professional cards never appear in that listing, so there is no
    URL to rewrite: they take the same built filename ``fetch`` uses,
    with the requested month. Resolving them through the listing matched
    the residential card of the same product name and billed a B2B
    contract at residential rates.

    Returns ``None`` when the URL 404s (or returns the CDN's HTML stub
    for a non-archived effective day, which ``_is_pdf_payload`` rejects),
    the parse fails, or the requested month falls outside the archive.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    contract = _CONTRACTS_BY_ID[contract_id]
    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        return None
    url = await _archive_pdf_url(session, contract, region_code, year_month)
    if url is None:
        return None
    try:
        text = await fetch_pdf_text(session, url)
        snap = parse_snapshot(contract_id, text, region, url)
    except ExtractorError:
        # Deliberately no previous-month retry, unlike fetch(): a month Mega
        # never published must resolve to None so the caller falls back to the
        # current-card proxy, not to a neighbouring month's card silently
        # billed as this one's.
        return None
    # Cross-check the parsed card actually covers the requested month; if Mega
    # ever serves a current PDF under a historical URL, the validity / title
    # check rejects it instead of mis-billing past consumption at current
    # rates. Same shape as eneco / cociter / ebem.
    checked = archive_validity_check(
        snap, text, year_month, month_names=_FR_MONTH_NAMES
    )
    if checked is None:
        return None
    return await _apply_realized_for_month(
        session, contract, region, region_code, year_month, checked
    )


async def _apply_realized_for_month(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region: str,
    region_code: str,
    year_month: date,
    snap: SupplierSnapshot,
) -> SupplierSnapshot:
    """Swap in the rates Mega actually billed for ``year_month``.

    The card FOR a month reports the previous month's regularisation figures,
    so the archive walk was billing each past month at the month before it.
    The figures that bill month M are printed on the M+1 card; take them from
    there and splice them onto M's own DSO and tax overlays.

    Only the energy and injection legs move. The overlays, the yearly fee and
    the cohort coefficients stay M's, because those really are properties of
    M's card. When the M+1 card is not out yet, or is missing a label, the
    mapping comes back empty and M keeps its own figures -- the behaviour
    before this, and still the best available for the newest month.
    """
    if contract.kind not in ("variable", "tou_impact"):
        return snap
    realized = await _realized_rates_for_month(
        session, contract, region, region_code, year_month
    )
    if not realized:
        return snap
    energy = snap.energy
    if isinstance(energy, VariableRates):
        energy = replace(
            energy,
            current=realized.get("mono", energy.current),
            peak=realized.get("peak", energy.peak),
            offpeak=realized.get("offpeak", energy.offpeak),
            exclusive_night=realized.get("exclusive_night", energy.exclusive_night),
        )
    elif isinstance(energy, ImpactRates):
        energy = replace(
            energy,
            pic=realized.get("pic", energy.pic),
            medium=realized.get("medium", energy.medium),
            eco=realized.get("eco", energy.eco),
        )
    injection = snap.injection
    if injection is not None and realized.get("injection") is not None:
        injection = replace(injection, current=realized["injection"])
    return replace(snap, energy=energy, injection=injection)


def _assert_card_region(text: str, region: str) -> None:
    """Reject a card that is not the requested region's edition.

    parse_snapshot applies region-specific DSO and levy overlays, so a
    wrong-region card mis-prices silently. Every card prints "Carte
    tarifaire / Client résidentiel - <Flandre|Wallonie|Bruxelles>"; the
    three region names also all appear in the cross-region "Cotisation
    Verte" table on every card, so anchor on that header label rather than
    on a bare region name. Fold accents and collapse whitespace so a
    re-render that splits or de-accents the line still matches.
    """
    label = fold_accents(_REGION_CARD_LABELS[region])
    haystack = fold_accents(re.sub(r"\s+", " ", text))
    if not re.search(
        rf"client\s+(?:residentiel|professionnel)\s*[-–]\s*{label}", haystack
    ):
        raise ExtractorError(f"Mega: card is not the {region} edition")


_INJECTION_VAT_EXEMPT = re.compile(
    r"prix d[\u2019']injection sont exempt", re.IGNORECASE
)
_INJECTION_VAT_DUE = re.compile(
    r"prix d[\u2019']injection sont [\u00e0a] majorer de la TVA", re.IGNORECASE
)


def _injection_vat_applies(text: str, professional: bool) -> bool:
    """Whether this card's feed-in prices are quoted excluding VAT.

    Read off the card rather than assumed from the edition. Every Mega card
    prints one of two sentences, in French even on the Flemish ones, and they
    do not split the way the edition does: the residential cards are all
    exempt, the professional FIXED and SMART cards are "a majorer de la TVA,
    sauf si vous etes soumis au regime d'exoneration", and the professional
    DYNAMIC card is exempt like its residential twin. Keying on
    ``professional`` grossed that one card's credit by 21%.

    Falls back to the edition when a card prints neither sentence, which is
    what this did for every card before, so a redesign that drops the wording
    degrades to the old assumption rather than to a silent zero.
    """
    if _INJECTION_VAT_EXEMPT.search(text):
        return False
    if _INJECTION_VAT_DUE.search(text):
        return True
    return professional


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _LISTING_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Mega")

    _assert_card_region(text, region)

    professional = contract.professional
    energy = _extract_energy(text, contract.kind, professional=professional)
    injection = _extract_injection(text, contract.kind)
    if injection is not None:
        injection = replace(
            injection, vat_applies=_injection_vat_applies(text, professional)
        )
    publication_label = _extract_publication_month(text)
    excise_bands: tuple[tuple[float, float], ...] | None = None
    if professional:
        tiers = _extract_pro_excise_tiers(text)
        excise_bands = tuple(
            (tier_bound_kwh(upper), to_float(rate) / 100.0)
            for _lower, upper, rate, _contrib in tiers
        )
        federal_excise = excise_bands[0][1]
        energy_contribution = to_float(tiers[0][3]) / 100.0
    else:
        federal_excise = _extract_federal_excise(text)
        energy_contribution = _extract_energy_contribution(text)
    region_connection_fee = (
        _extract_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        wallonia_renewables = _extract_renewables(text, "Wallonie")
        dsos = _extract_wallonia_dsos(text)
    else:
        brussels_renewables = _extract_renewables(text, "Bruxelles")
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="mega",
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
            energy_fund_eur_per_month=_extract_energy_fund(
                text, region, professional=professional
            ),
            # The professional card prints HTVA throughout; base.apply_vat
            # resolves it for the entry.
            vat_rate=VAT_RATE_STANDARD if professional else 0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text) or _extract_valid_until(text),
        injection=injection,
        supplier_prosumer_eur_per_kva_year=_extract_supplier_prosumer(
            text, region, contract.kind
        ),
    )


# ---- energy block -------------------------------------------------------------


# ---- taxes --------------------------------------------------------------------


# ---- DSO row parsers ----------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    id="mega",
    label="Mega",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            professional=c.professional,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
