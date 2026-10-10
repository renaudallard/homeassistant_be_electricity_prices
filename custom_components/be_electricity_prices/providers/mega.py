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
``Smart2204-Fixed``, ``Cosy1306``) carries an internal launch-date code
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
(both Wallonia-specific). The Tarif Social variant is not a product
here: the social tariff is the CREG's and is priced by the social tariff
supplier (social.py).

Ten professional editions are registered alongside them, all addressed by
built filename rather than by scraping: Mega links only the SME pair from
the listing, and the resolver pins the B2C segment, so even those two are
built. The listing entry still matters for discovery, which is what
``_ContractDef.advertised`` records.

The Dynamic formula uses a different convention than Engie/Luminus:
``Day Ahead Epex Spot * 1.05 + 1.35 c€/kWh`` where the spot is already
in c€/kWh and the result is TVAC, so factor and base are scaled
straight to EUR/kWh without a VAT multiplier.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from datetime import date, timedelta

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    VAT_RATE_STANDARD,
)
from ._mega_cards import (
    _FR_MONTH_NAMES,
    _VAT_PATTERNS,
    _extract_energy,
    _extract_injection,
    _extract_publication_month,
    _extract_valid_until,
    _injection_vat_applies,
    _realized_rates,
)
from ._mega_contracts import (
    _CONTRACTS,
    _CONTRACTS_BY_ID,
    _DIRECT_DEBIT_RISTOURNE,
    _KNOWN_UNSUPPORTED_PRODUCTS,
    _ContractDef,
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
    extract_injection_bonus,
    extract_ristourne,
    ristourne_kind,
    ristourne_requires_direct_debit,
    ristourne_wait_months,
)
from ._parse import (
    fold_accents,
    require_contract,
    tier_bound_kwh,
    to_float,
)
from ._pdf import (
    fetch_pdf_text,
    fetch_text,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    ImpactRates,
    VariableRates,
)
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    CardNotReadableError,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
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


# Days into a month during which a professional card still missing for it
# may be stood in for by last month's. Mega publishes them before the month
# opens (the October 2026 cards are dated 28 to 30 September) and its lag has
# been a day or two; past this, a missing card is reported, not rolled back.
# The live check reads the same figure.
_PRO_PUBLICATION_GRACE_DAYS = 5

# Last month's professional cards read during that grace, as (text, url) by
# contract, region and month. Superseded, so they cannot change, and a card
# fetched already expired is asked for again every hour to notice this
# month's: without this each of those asks downloaded last month's whole card
# again. Only the latest month is kept.
_ROLLED_BACK: dict[tuple[str, str, date], tuple[str, str]] = {}


def _pro_pdf_url(
    c: _ContractDef, region_code: str, year_month: date, variant: str | None = None
) -> str:
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
    if variant is None:
        variant = c.file_variant
    return (
        f"{_CDN_BASE}Mega-FR-EL-B2B-{region_code}-{mm}{year_month.year}-"
        f"{c.file_family}01{mm}{variant}.pdf"
    )


def _pro_pdf_urls(c: _ContractDef, region_code: str, year_month: date) -> list[str]:
    """Every URL the professional card for a month may sit under, likeliest first.

    Mega spells the fixed variant two ways and moves a product between them:
    Off-peak has always been "-Fix" and the rest "-Fixed", until the October
    2026 pro Zen Fixed card came out as "-Fix" while "-Fixed" answered the
    CDN's HTML stub. So the registry's spelling is tried first and the other
    one after it.
    """
    variants = [c.file_variant]
    if c.file_variant.endswith("-Fixed"):
        variants.append(c.file_variant[: -len("ed")])
    elif c.file_variant.endswith("-Fix"):
        variants.append(c.file_variant + "ed")
    return [_pro_pdf_url(c, region_code, year_month, v) for v in variants]


async def _fetch_first_card(
    session: aiohttp.ClientSession, urls: list[str]
) -> tuple[str, str]:
    """The text of the first of ``urls`` that serves a card, and that URL.

    Only a URL that is not there (a 404, or the CDN's HTML stub) moves on to
    the next spelling. A card that downloaded with no text layer, and any
    transient failure, raise at once: neither says the card lives elsewhere.
    ``urls`` is never empty, and the last one's failure is what surfaces.
    """
    for url in urls[:-1]:
        try:
            return await fetch_pdf_text(session, url), url
        except CardNotReadableError:
            raise
        except ExtractorError as err:
            if is_transient_fetch_error(str(err)):
                raise
    return await fetch_pdf_text(session, urls[-1]), urls[-1]


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
        try:
            text, pdf_url = await _fetch_first_card(
                session, _pro_pdf_urls(contract, region_code, today)
            )
        except CardNotReadableError:
            # Downloaded fine but has no text layer: not an unpublished
            # card, so it must surface rather than silently roll back a
            # month. Three of these professional contracts are variable
            # and one is dynamic, so last month's card carries last
            # month's index: the prices would be wrong, not just old.
            raise
        except ExtractorError as err:
            # Early in a month Mega can lag a day or two before the new
            # card lands; the one still in force is last month's. Only that
            # case, which answers 404, takes the previous month: a timeout,
            # a reset or a 5xx says nothing about which card is in force,
            # and falling back on it served last month's index, overlays
            # and taxes as this month's for the 24 h TTL, with no error
            # recorded, where every other supplier surfaces the failure.
            #
            # And only early: a card still missing past the grace has moved
            # rather than lagged, which is what the October 2026 pro Zen
            # Fixed did, and rolling back then served September's card all
            # month with nothing to show for it. Past the grace it raises,
            # so the entry keeps what it holds and says why.
            if is_transient_fetch_error(str(err)):
                raise
            if today.day > _PRO_PUBLICATION_GRACE_DAYS:
                raise
            previous = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
            key = (contract_id, region_code, previous)
            held = _ROLLED_BACK.get(key)
            if held is None:
                held = await _fetch_first_card(
                    session, _pro_pdf_urls(contract, region_code, previous)
                )
                for older in [k for k in _ROLLED_BACK if k[2] != previous]:
                    del _ROLLED_BACK[older]
                _ROLLED_BACK[key] = held
            text, pdf_url = held
        return parse_snapshot(contract_id, text, region, pdf_url)

    listing = await _fetch_listing_html(session)
    listed_url = _resolve_pdf_url(listing, contract.product_name, region_code)
    if listed_url is None:
        raise ExtractorError(
            f"Mega {contract_id}: no listing entry for region {region!r}"
        )
    text = await fetch_pdf_text(session, listed_url)
    return parse_snapshot(contract_id, text, region, listed_url)


async def _archive_pdf_urls(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region_code: str,
    year_month: date,
    *,
    allow_current: bool = False,
    first_day_first: bool = False,
) -> list[str]:
    """The CDN URLs ``contract``'s card for one month may sit under, or none.

    Professional cards never appear in the public listing, so they take the
    same built filenames ``fetch`` tries with the requested month. Residential
    ones resolve the current URL from the listing and rewrite BOTH month
    placeholders: the ``-MMYYYY-`` segment and the ``<MM>`` half of the
    product's effective-date ``<DD><MM>`` suffix, while preserving the
    effective day, which is not the 1st for every product.

    The day the listing shows today says nothing about another month,
    though. Mega publishes every card on the 1st and re-issues some on the
    8th: Cosy Flex has a ``Cosy0101`` and a ``Cosy0801`` for January 2026,
    and while the listing showed ``Cosy0809`` the February card was asked
    for as ``Cosy0802``, which does not exist. The card of the 1st is the
    second candidate, or the first under ``first_day_first``.
    """
    if contract.professional:
        return _pro_pdf_urls(contract, region_code, year_month)
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return []
    current_url = _resolve_pdf_url(listing, contract.product_name, region_code)
    if current_url is None:
        return []
    mmyyyy_re = re.compile(r"-(\d{2})\d{4}-(?=[^/]*\.pdf$)")
    mmyyyy_match = mmyyyy_re.search(current_url)
    if mmyyyy_match is None:
        return []
    current_mm = mmyyyy_match.group(1)
    target_mm = f"{year_month.month:02d}"
    historical_mmyyyy = f"{target_mm}{year_month.year}"
    new_url = mmyyyy_re.sub(f"-{historical_mmyyyy}-", current_url, count=1)
    # Online0106-Fixed -> Online0105-Fixed, Cosy1306 -> Cosy1305. A product
    # whose publication day varies month to month resolves to the CDN's HTML
    # stub, which the PDF magic-byte check rejects, so the card of the 1st is
    # tried next, and without one the walk falls back to the proxy rather
    # than mis-billing.
    prefix, sep, tail = new_url.partition(f"-{historical_mmyyyy}-")
    if sep:
        tail = re.sub(
            rf"(\d{{2}}){current_mm}(?=[-.])",
            rf"\g<1>{target_mm}",
            tail,
            count=1,
        )
        first_day = (
            prefix
            + sep
            + re.sub(rf"\d{{2}}({target_mm})(?=[-.])", r"01\g<1>", tail, count=1)
        )
        new_url = prefix + sep + tail
    else:
        first_day = new_url
    # A rewrite that lands back on the listing's own URL means the requested
    # month IS the current one. fetch_for_month refuses that, so a current
    # card can never be served as a historical month. The realized-rate
    # lookup wants it though: for the most recently completed month, the
    # card carrying its billed figures is precisely the current one.
    if new_url == current_url and not allow_current:
        return []
    urls = [new_url] if first_day == new_url else [new_url, first_day]
    return urls[::-1] if first_day_first else urls


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
) -> dict[str, float] | None:
    """Month ``year_month``'s BILLED rates, read off the NEXT month's card.

    A variable or Impact card's headline table is a 12-month simulation, and
    the "derniers prix constates ... pour le mois de <month>" sentence that
    overrides it names the month BEFORE the card's own: the June card reports
    May's regularisation figures. That is the only choice on the live path,
    where the current month's index does not exist yet, but on the archive
    path it shifted every past month of the year-to-date walk by one, billing
    June at May's rate while June's real rate sat unread on the July card.

    So read the sentence from the M+1 card. Returns None when that card is
    not out yet (M is the current month) or does not resolve, and an empty
    mapping when it is out and states no figure for M: the caller keeps the M
    card's own figures either way, but only the first can still change. A
    timeout or a 5xx says nothing about the card and is raised, so the month
    cache retries it rather than file the estimate.
    """
    following = _next_month(year_month)
    if following > date(dt_util.now().year, dt_util.now().month, 1):
        return None
    # The card of the 1st first: a re-issue later in the month states the
    # same figures for this month, and the 1st is the one always published.
    urls = await _archive_pdf_urls(
        session,
        contract,
        region_code,
        following,
        allow_current=True,
        first_day_first=True,
    )
    if not urls:
        return None
    try:
        text, url = await _fetch_first_card(session, urls)
        following_snap = parse_snapshot(contract.contract_id, text, region, url)
    except ExtractorError as err:
        if is_transient_fetch_error(str(err)):
            raise
        return None
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
        return None
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
    The month appears twice: the ``<MMYYYY>`` segment and the ``<MM>``
    half of the product's effective-date ``<DD><MM>`` suffix, and both
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
    the parse fails, or the requested month falls outside the archive. A
    transient fetch failure is raised instead, so the month cache retries
    the month rather than caching it as absent.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    contract = _CONTRACTS_BY_ID[contract_id]
    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        return None
    urls = await _archive_pdf_urls(session, contract, region_code, year_month)
    if not urls:
        return None
    try:
        text, url = await _fetch_first_card(session, urls)
        snap = parse_snapshot(contract_id, text, region, url)
    except ExtractorError as err:
        # Deliberately no previous-month retry, unlike fetch(): a month Mega
        # never published must resolve to None so the caller falls back to the
        # current-card proxy, not to a neighbouring month's card silently
        # billed as this one's. A timeout, a reset or a 5xx says nothing about
        # the month, though: raise, so the month cache retries it instead of
        # caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
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
    M's card. When the M+1 card is not out yet, M keeps its own figures, the
    best available for the newest month, and is flagged ``provisional`` so the
    month cache asks again rather than keep last month's figures as M's for
    good, which is what it did at midnight on the 1st. A next card that is out
    and states no figure for M leaves it alone: retrying cannot conjure a
    sentence the card does not print.

    A band the sentence leaves out is re-priced from one it states, through
    the card's own coefficients (``_with_unstated_bands``), rather than kept:
    on this path the M card's own figure for it is the month before's.
    """
    if contract.kind not in ("variable", "tou_impact"):
        return snap
    realized = await _realized_rates_for_month(
        session, contract, region, region_code, year_month
    )
    if realized is None:
        return replace(snap, provisional=True)
    if not realized:
        return snap
    energy = snap.energy
    realized = _with_unstated_bands(realized, energy)
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


# The coefficient pair each settled band is the formula of, per leg kind.
_BAND_COEFFICIENTS: dict[type, dict[str, tuple[str, str]]] = {
    VariableRates: {
        "mono": ("formula_factor", "formula_base"),
        "peak": ("formula_factor_peak", "formula_base_peak"),
        "offpeak": ("formula_factor_offpeak", "formula_base_offpeak"),
        "exclusive_night": (
            "formula_factor_exclusive_night",
            "formula_base_exclusive_night",
        ),
    },
    ImpactRates: {
        "pic": ("pic_factor", "pic_base"),
        "medium": ("medium_factor", "medium_base"),
        "eco": ("eco_factor", "eco_base"),
    },
}


def _with_unstated_bands(
    realized: dict[str, float], energy: EnergyRates
) -> dict[str, float]:
    """``realized`` with every band it leaves out re-priced at the month's
    index, solved from a band it states through the card's coefficients.

    The June 2026 Flanders Cosy Flex and Smart Flex cards print "Compteur
    mono- horaire : 16.76.38" for May, which is refused, beside a readable
    Jour and Nuit. Keeping the May card's own mono billed May at April's
    settled 13,81 c/kWh where the bands put it at 15,38. All bands solve to
    one index on every other month, so the missing one is the formula at it.
    A card printing no coefficients leaves ``realized`` as it is.
    """
    pairs = {
        band: (getattr(energy, factor), getattr(energy, base))
        for band, (factor, base) in _BAND_COEFFICIENTS.get(type(energy), {}).items()
    }
    pairs = {
        band: pair
        for band, pair in pairs.items()
        if pair[0] is not None and pair[1] is not None
    }
    index = next(
        (
            (realized[band] - base) / factor
            for band, (factor, base) in pairs.items()
            if band in realized and factor
        ),
        None,
    )
    if index is None:
        return realized
    return {
        **{band: factor * index + base for band, (factor, base) in pairs.items()},
        **realized,
    }


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
        federal_excise, excise_bands = _extract_federal_excise(text)
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

    # Mega grants a first-year ristourne on most of its range and states it in
    # prose under the tariff table. Until October 2026 it was granted "apres
    # douze mois ininterrompus", or fourteen on Zen Fixed and Smart Flex, and
    # paid on the first regularisation invoice after that, the anniversary
    # shape; the October residential cards pay it pro rata from the first
    # advance invoice. ristourne_kind reads which.
    ristourne = extract_ristourne(text)
    return with_vat_basis(
        SupplierSnapshot(
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
                # The professional card prints HTVA throughout; _resolve.apply_vat
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
            welcome_credit_eur=ristourne["welcome_credit_eur"],
            welcome_credit_eur_per_kwh=ristourne["welcome_credit_eur_per_kwh"],
            welcome_credit_injection_eur_per_kwh=extract_injection_bonus(text),
            welcome_credit_cap_eur=ristourne["welcome_credit_cap_eur"],
            welcome_credit_direct_debit_eur=ristourne[
                "welcome_credit_direct_debit_eur"
            ],
            welcome_credit_requires_direct_debit=ristourne_requires_direct_debit(text),
            welcome_credit_after_months=ristourne_wait_months(text),
            welcome_credit_kind=ristourne_kind(text),
        ),
        None if professional else printed_vat_rate(text, *_VAT_PATTERNS),
        professional=professional,
        # The dynamic formula is printed VAT-inclusive and grossed by nothing.
        grossed=not isinstance(energy, DynamicRates),
    )


# ---- energy block -------------------------------------------------------------


# ---- taxes --------------------------------------------------------------------


# ---- DSO row parsers ----------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=2.1,
    settles_on_next_card=True,
    id="mega",
    label="Mega",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            professional=c.professional,
            # The variable and Impact cards index the feed-in credit on the
            # monthly Epex SPP, which their own energy leg fetches no spots
            # for. Dynamic collects the key via its energy formula; the
            # fixed cards lock the credit for a year and index nothing.
            spot_indexed_injection=c.kind in ("variable", "tou_impact"),
            # And the same two kinds index their ENERGY on the delivery month.
            # Their cards print a formula per meter or per CWaPE band and,
            # beside it, "les derniers prix constates et utilises pour le
            # calcul de votre facture de regularisation pour le mois de
            # <MONTH>": a month they name, and it is the one before the
            # card's own: the April card settles March, the May card April.
            # Billing that figure bills last month's index, so the re-price
            # needs the optional key step on every solar regime.
            month_indexed_energy=c.kind in ("variable", "tou_impact"),
            # Nineteen of the cards price a direct-debit payer differently,
            # and say so in the ristourne paragraph: "soit une reduction de
            # base de 37.1 EUR + 5.3 EUR supplementaires en cas de paiement
            # par domiciliation bancaire", or, on four of them, by granting
            # the whole ristourne to nobody else. Which of the two is parsed
            # onto the snapshot, and the flow only asks how a household pays
            # when this flag is set, so without it resolve_direct_debit had
            # nothing to apply: 42,40 EUR of a 215,18 EUR credit on Cosy
            # Fixed, and the entire 522,58 EUR on Cosy Flex.
            direct_debit_discount=c.contract_id in _DIRECT_DEBIT_RISTOURNE,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
