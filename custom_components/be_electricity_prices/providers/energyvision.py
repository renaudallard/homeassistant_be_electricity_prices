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

"""EnergyVision tariff extractor.

EnergyVision publishes its "Goedkope stroom" residential cards as monthly
PDFs named ``EV-<MMYY>-<CODE>-<lang>.pdf`` under
``/sites/default/files/inline-files/``. The filenames carry the pricing
month (``EV-0726-...`` = July 2026) and the server adds Drupal dedup
suffixes (the fixed card ships as ``EV-0726-GS3JV-nl_0.pdf``), so a
constructed URL would miss it. The fetch therefore scrapes the current
card href off the tariefkaart listing page (the Mega / Frank shape).

A product is published per region, one card each: the Flemish cards are
``-nl``, the Walloon ones ``-WAL-fr`` and the Brussels ones ``-BXL-nl``.
So ``_ContractDef`` holds a ``_CardDef`` per region rather than one filename
token, and that card says which site, which index page and which archive
layout the region's publication uses.

Brussels is not on this site at all. EnergyVision sells there as **Brusol**,
on ``brusol.be``, which advertises each current card on the page you sign up
from and files the archived ones under the month it uploaded them rather than
the month they price. The Brussels cards are published in both languages and
the Dutch one is worded exactly like the Flemish card of the same product, so
the energy leg needs no second parser; only the Sibelga network row and the
Brussels tax block are the region's own.

The residential electricity products supported:

* ``GSDYN`` (Goedkope Stroom Dynamisch, Flanders): quarter-hourly Belpex
  formula, the same EUR/MWh HTVA axis as Bolt / Frank. The coefficient is a
  dimensionless Belpex multiplier (NOT scaled by ten the way Frank's
  cents-output coefficient is), the base goes EUR/MWh to EUR/kWh, and 6%
  VAT is baked into both. The injection coefficient is exactly 1,0.
* ``GS3JV`` (Goedkope stroom 3 jaar vast, Flanders): a flat fixed rate for
  3 years; its injection is indexed monthly (Belpex-SPP-M, known at
  month-end), so the printed monthly indicative is billed rather than a
  live spot formula.
* ``GS1JV`` (Électricité bon marché 1 an fixe, Wallonia): the same fixed
  shape on a 1-year lock, off a French card that shares no wording with the
  Dutch ones. Parsed by the ``*_fr`` helpers below. This is where DATS 24's
  Walloon customers land after the 2026-08-31 transfer.

* ``GS1800V`` (Flanders and Brussels) / ``GSVI3`` / ``GSLP`` (Flanders):
  the tiered range, which bills a first tranche of the YEAR at a flat rate
  and the remainder on ``factor x Belpex-RLP-M + 20 EUR/MWh``. Parsed as a
  ``SpotMonthlyRates`` leg carrying the tranche, which ``resolve_volume_tier``
  folds into the coefficients against the entry's annual volume. GSVI3 fixes
  its feed-in price instead of indexing it, which is the only shape
  difference. The two GS1800V cards print the same energy leg figure for
  figure and differ only below it.

Out of scope: gas (``GSG``, ``GS1JVG``) and the two tiered products that also
price self-consumed solar (``GSEZ``, ``GSEZLP``): their "Groene stroom uit
zonnepanelen op je dak" row is a third energy leg with no representation in
the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

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
)
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from ._parse import (
    to_float,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)
from ._rates import (
    Contract,
    EnergyRates,
    TariffKind,
)
from ._energyvision_overlays import (
    _extract_brussels_dsos,
    _extract_brussels_taxes,
    _extract_dsos,
    _extract_taxes,
)
from ._energyvision_wallonia import (
    _parse_wallonia,
)
from ._energyvision_cards import (
    _direct_debit_discount,
    _extract_dynamic,
    _extract_fixed,
    _extract_tiered,
)
from .energiebe import _NUM

_SITE_BASE = "https://www.energyvision.be"
# One listing page carries every card on the EnergyVision site, Flemish and
# Walloon alike, so the freshness probe covers both.
_LISTING_URL = f"{_SITE_BASE}/nl-be/tariefkaart"

# EnergyVision sells in Brussels under the Brusol brand, off its own site.
# Nothing on energyvision.be links to it.
_BRUSOL_SITE = "https://www.brusol.be"
# Brusol has no equivalent of the tariefkaart listing: each product's current
# card is advertised on the page you sign up for it from. It does publish an
# archive page, but that one stopped being updated in May 2026 and lists every
# product it has ever sold, so it is the wrong page to read for either job.
# The FR path segment on the NL page is Brusol's own alias, not a typo.
_BRUSOL_GS1800V_URL = (
    f"{_BRUSOL_SITE}/nl/%C3%A9lectricit%C3%A9-et-gaz"
    "/schrijf-je-in-voor-goedkope-stroom-van-brusol"
)
_BRUSOL_GRS_URL = f"{_BRUSOL_SITE}/nl/schrijf-je-in-voor-groene-stroom-van-brusol"


@dataclass(frozen=True)
class _CardDef:
    """Where one product's card for one region is published.

    A product is published per region, and the publication is not one
    catalogue: the region decides the filename token, the site and the page
    the current card is resolved off.
    """

    # Filename language / region token, the part between the product code and
    # the Drupal dedup suffix: "nl" for the Flemish cards, "WAL-fr" for the
    # Walloon ones.
    token: str
    # Site the card and the page that advertises it live on.
    site: str = _SITE_BASE
    # Page whose HTML carries the current card's href.
    index_url: str = _LISTING_URL
    # How an archived card is located under <site>/sites/default/files/.
    # False is EnergyVision's flat "inline-files" folder, where the filename
    # alone locates every month; True is Brusol, which files each card under
    # the month it uploaded it.
    archive_by_upload_month: bool = False


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    code: str  # EV filename product code (GSDYN, GS3JV, ...)
    # One card per region this product is sold in. Most products are sold in
    # one region only, but not all: GS1800V is published for Flanders and,
    # under the Brusol brand, for Brussels, off two different sites. The
    # mapping makes this class unhashable, which nothing needs it to be.
    cards: Mapping[str, _CardDef]
    # Whether a spot_monthly card bills a first tranche of the year at a flat
    # rate before the indexed remainder. Declared rather than discovered: a
    # tiered card whose tranche row went missing has drifted and must fail
    # loud, not quietly bill every kWh at the indexed rate.
    tranche: bool = True

    @property
    def regions(self) -> frozenset[str]:
        return frozenset(self.cards)

    def card(self, region: str) -> _CardDef | None:
        return self.cards.get(region)


_FLANDERS_CARD = _CardDef(token="nl")


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "energyvision_dynamic",
        "EnergyVision Dynamisch",
        "dynamic",
        "GSDYN",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    _ContractDef(
        "energyvision_fixed_3y",
        "EnergyVision 3 jaar vast",
        "fixed",
        "GS3JV",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    # Wallonia's own fixed product, on a French card. It is a 1-year lock
    # where Flanders gets 3, so it is a distinct contract rather than the same
    # one in another region.
    #
    # It used to say here that this is where DATS 24's Walloon customers land
    # after the 2026-08-31 transfer. Nothing sourced that: DATS 24's own site
    # named EnergyVision and no product. Issue #100's reporter, who was
    # actually transferred, landed on the legacy DATS 24 card continued under
    # EnergyVision's name, and that card covers Wallonia on its face, so the
    # Walloon customers plausibly went the same way. Unknown either way, and
    # the Repairs card no longer tells anyone which product to pick.
    _ContractDef(
        "energyvision_fixed_1y",
        "EnergyVision 1 an fixe",
        "fixed",
        "GS1JV",
        {REGION_WALLONIA: _CardDef(token="WAL-fr")},
    ),
    # The tiered range. All three share one shape and differ only in the
    # tranche, the flat rate, the coefficient, the standing charge and how the
    # feed-in is priced, so one parser reads the three of them.
    # The one product sold in all three regions. The Dutch Brussels card is
    # worded exactly like the Flemish one and prints the same energy leg
    # figure for figure, so it goes through the same parser; only the network
    # and tax blocks are the region's own. The Walloon card is the separate
    # French publication and needs the *_fr anchors, but the same body behind
    # them, and it charges NO standing charge where the other two charge 50.
    #
    # Who may take it differs too, which the config flow cannot express and
    # the README says instead: Flanders and Wallonia sell it to any
    # residential customer, Brussels only to roofs already carrying
    # EnergyVision/Brusol panels.
    _ContractDef(
        "energyvision_tiered_1800",
        "EnergyVision 1.800 kWh vast",
        "spot_monthly",
        "GS1800V",
        {
            REGION_FLANDERS: _FLANDERS_CARD,
            REGION_WALLONIA: _CardDef(token="WAL-fr"),
            REGION_BRUSSELS: _CardDef(
                token="BXL-nl",
                site=_BRUSOL_SITE,
                index_url=_BRUSOL_GS1800V_URL,
                archive_by_upload_month=True,
            ),
        },
    ),
    _ContractDef(
        "energyvision_fixed_injection_3y",
        "EnergyVision vaste injectieprijs 3 jaar",
        "spot_monthly",
        "GSVI3",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    _ContractDef(
        "energyvision_laadpunt",
        "EnergyVision Laadpunt",
        "spot_monthly",
        "GSLP",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    # Brusol's other Brussels product, and the one any household can sign:
    # GS1800V is sold only to roofs carrying EnergyVision/Brusol panels,
    # while this card's condition 3 asks for nothing but a residential
    # connection. Same monthly RLP index as the tiered range with no tranche
    # in front of it, so it is a spot_monthly card that bills the formula
    # from the first kWh.
    #
    # It names Belpex-RLP-M and Belpex-SPP-M without defining either, where
    # the GS1800V card spells out that the weighting is the mean of the
    # Flemish DSOs' profiles. Same supplier, same index names, same published
    # parameter page, so the blend is read off that card rather than guessed;
    # if EnergyVision ever defines a Brussels profile, this is what changes.
    _ContractDef(
        "energyvision_groene_stroom",
        "EnergyVision Groene stroom",
        "spot_monthly",
        "GRS",
        {
            REGION_BRUSSELS: _CardDef(
                token="BXL-nl",
                site=_BRUSOL_SITE,
                index_url=_BRUSOL_GRS_URL,
                archive_by_upload_month=True,
            )
        },
        tranche=False,
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Every residential electricity product code EnergyVision currently lists,
# across both regions, so discover() flags only a genuinely new SKU. All of
# them are implemented except the catalogued-but-declined ones: GSEZ / GSEZLP
# price self-consumed solar as a third energy leg, GSG / GS1JVG are gas, and
# GRSO is a transient group-buy SKU.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "GSDYN",
        "GS3JV",
        "GS1JV",
        "GSVI3",
        "GRS",
        "GS1800V",
        "GSLP",
        "GSEZ",
        "GSEZLP",
        "GSG",
        "GS1JVG",
        "GRSO",
    }
)


# "Eenmalige welkomstkorting  200 €", the one-off first-year credit, printed
# above the standing charge on four of the six cards. Optional on purpose:
# Laadpunt prints no such row, and the Walloon card carries footnote e
# describing the credit while printing no amount for it, so there is nothing
# to grant. Only the Dutch wording is matched, because that is the only one
# any card has ever printed a figure next to.
_WELCOME_RE = re.compile(rf"Eenmalige\s+welkomstkorting\s+{_NUM}\s*€", re.IGNORECASE)


_LABEL_RE = re.compile(r"Tariefkaart\s+([A-Za-z]+\s+20\d{2})", re.IGNORECASE)

# ---- Wallonia (French card) --------------------------------------------------
#
# The Walloon cards are a separate publication in French, so none of the
# patterns above match them: every one was verified to miss. They are kept as
# a parallel set rather than widened into bilingual alternations, because the
# two cards also differ in structure (no digital/analog meter split, a ten-
# column DSO table, CV instead of GSC/WKC, no energiefonds).


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    card = contract.card(region)
    if card is None:
        raise ExtractorError(
            f"EnergyVision {contract_id} is not sold in {region!r}; "
            f"published for {sorted(contract.regions)}"
        )
    url = await _resolve_card_url(session, contract, card)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url, region=region)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card EnergyVision published for one past month, or ``None``.

    The live fetch has to scrape the listing because the CURRENT card carries
    Drupal's dedup suffix (``EV-0726-GS3JV-nl_0.pdf``), but a past month is not
    on that listing at all and its plain filename resolves directly: every
    product answered 200 for every month it existed, measured across GSDYN /
    GS3JV / GS1800V / GSVI3 / GSLP and March to September 2026.

    Each product has its own horizon rather than a shared one (GS1800V reaches
    back to March 2026, GSDYN only to June), and there is nothing on the site
    that states it. A month before it answers Drupal's 404 page, which is HTML
    rather than a PDF, so ``fetch_pdf_text_layout`` rejects it on the magic
    bytes and the ``except`` below turns that into "no archive here". Letting
    the 404 be the horizon keeps a constant from going stale behind the site.

    Every failure of the card itself is swallowed: this runs inside the
    year-to-date walk, and one unpublished month must not take the whole year
    down. A transient fetch failure is raised, so the month cache retries the
    month rather than caching it as absent.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    card = contract.card(region)
    if card is None:
        return None
    first = date(year_month.year, year_month.month, 1)
    for url in _archive_card_urls(contract, card, first):
        try:
            text = await fetch_pdf_text_layout(session, url)
            snap = parse_snapshot(contract_id, text, url, region=region)
        except ExtractorError as err:
            # A timeout, a reset or a 5xx says nothing about the month: raise,
            # so the month cache retries it instead of caching it as absent.
            if is_transient_fetch_error(str(err)):
                raise
            continue
        # Every card prints "geldig ... tot en met" so valid_until is parsed and
        # the authoritative tier of the cross-check applies. It is what catches
        # a CDN serving the current card under an archived name. A candidate
        # that turns out to hold another month's card is not the end of the
        # search: try the next one before giving the month up.
        checked = archive_validity_check(snap, text, first)
        if checked is not None:
            return checked
    return None


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness key: HEAD the page this card is advertised on. Its
    ETag / Last-Modified flips when EnergyVision rotates the monthly cards,
    which is exactly when the resolved PDF URL changes.

    The page is per region, not per supplier: the Brussels cards are
    advertised on the Brusol site and rotate on their own schedule.

    Brusol's pages are Drupal dynamic pages and answer with neither header,
    so Brussels has no probe key and falls back to the 24h TTL, the path
    Engie and Luminus take. Returning None for that is the documented way to
    say so; it does not refetch the card every tick.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    card = None if contract is None else contract.card(region)
    if card is None:
        return None
    return await head_freshness_key(
        session, card.index_url, prefer=("ETag", "Last-Modified")
    )


def _index_urls() -> tuple[str, ...]:
    """Every page a current card is advertised on, in registration order and
    without repeats."""
    seen: dict[str, None] = {}
    for contract in _CONTRACTS:
        for card in contract.cards.values():
            seen.setdefault(card.index_url, None)
    return tuple(seen)


def _card_href_re(code: str, token: str) -> re.Pattern[str]:
    """Match one card's href on an index page.

    The href is site-relative on the EnergyVision listing and absolute on the
    Brusol pages, so the site prefix is optional. The directory is not
    anchored: EnergyVision keeps every card in one ``inline-files`` folder
    while Brusol files each one under the month it uploaded it.
    """
    return re.compile(
        rf'href="((?:https?://[^"/]+)?/sites/default/files/[^"]*?'
        rf'EV-\d{{4}}-{re.escape(code)}-{re.escape(token)}[^"]*\.pdf)"',
        re.IGNORECASE,
    )


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the residential electricity product codes currently advertised,
    so live_check can flag a new SKU. Diffed against :data:`DISCOVER_IDS`.

    Every index page is walked and so is every token in use: a product is
    published for one region in one language, so matching one page or one
    token would silently drop a whole region's catalogue from the drift
    check. The tokens come from the registered cards rather than a literal,
    so a card added with a new token extends the check with it.

    Only pages advertising the CURRENT cards are read. Brusol also publishes
    an archive page, and reading that would report every product it has ever
    sold as a new SKU.
    """
    tokens = "|".join(
        sorted(
            {re.escape(card.token) for c in _CONTRACTS for card in c.cards.values()},
            key=lambda token: (-len(token), token),
        )
    )
    found: set[str] = set()
    for url in _index_urls():
        try:
            html = await fetch_text(session, url)
        except ExtractorError:
            continue
        found |= set(re.findall(rf"EV-\d{{4}}-([A-Z0-9]+)-(?:{tokens})", html))
    return found


def _archive_card_urls(
    contract: _ContractDef, card: _CardDef, first: date
) -> tuple[str, ...]:
    """The URLs an archived card for ``first`` could sit at, in order.

    EnergyVision keeps every month in one ``inline-files`` folder, so the
    filename locates the card on its own. Brusol files each card under the
    month it UPLOADED it, which is usually the month before delivery and
    sometimes the delivery month itself, so both are tried; measured over
    March to September 2026, the pair covers every card published.
    """
    stamp = f"{first.month:02d}{first.year % 100:02d}"
    name = f"EV-{stamp}-{contract.code}-{card.token}.pdf"
    if not card.archive_by_upload_month:
        return (f"{card.site}/sites/default/files/inline-files/{name}",)
    previous = date(first.year, first.month, 1) - timedelta(days=1)
    return tuple(
        f"{card.site}/sites/default/files/{folder:%Y-%m}/{name}"
        for folder in (previous, first)
    )


async def _resolve_card_url(
    session: aiohttp.ClientSession, contract: _ContractDef, card: _CardDef
) -> str:
    html = await fetch_text(session, card.index_url)
    match = _card_href_re(contract.code, card.token).search(html)
    if not match:
        raise ExtractorError(
            f"EnergyVision: no listing entry for card {contract.code} "
            f"({card.token}) on {card.index_url}"
        )
    href = match.group(1)
    return href if href.startswith("http") else card.site + href


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str,
    publication_label: str = "",
    *,
    region: str | None = None,
) -> SupplierSnapshot:
    """Parse one card. ``region`` says which of the contract's cards this is.

    Keyword-only, and defaulted to the contract's own region where it has
    exactly one: ``region`` and ``source_url`` are both ``str``, so a
    positional argument in the wrong slot would parse a Brussels card as a
    Flemish one rather than raise. A contract sold in more than one region
    has to be told, because the card decides the DSO and tax block.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    if region is None:
        if len(contract.regions) != 1:
            raise ExtractorError(
                f"EnergyVision {contract_id} is sold in "
                f"{sorted(contract.regions)}; parse_snapshot needs the region"
            )
        region = next(iter(contract.regions))
    elif region not in contract.regions:
        raise ExtractorError(
            f"EnergyVision {contract_id} is not sold in {region!r}; "
            f"published for {sorted(contract.regions)}"
        )
    if region == REGION_WALLONIA:
        return _parse_wallonia(contract_id, text, source_url, publication_label)
    energy: EnergyRates
    if contract.kind == "dynamic":
        energy, injection = _extract_dynamic(text)
    elif contract.kind == "spot_monthly":
        energy, injection = _extract_tiered(text, tranche=contract.tranche)
    else:
        energy, injection = _extract_fixed(text)
    # The energy leg is worded identically in both regions and needs no
    # branch; the network and tax blocks are each region's own.
    brussels = region == REGION_BRUSSELS
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_brussels_dsos(text) if brussels else _extract_dsos(text),
        taxes=_extract_brussels_taxes(text) if brussels else _extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label(text),
        valid_until=parse_valid_until(text),
        injection=injection,
        welcome_credit_eur=_welcome_credit(text),
        direct_debit_discount_eur=_direct_debit_discount(text, energy.yearly_fixed_fee),
    )


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return m.group(1).lower() if m else ""


def _welcome_credit(text: str) -> float | None:
    """The one-off welcome credit in EUR, or ``None`` where the card prints none.

    Optional where the standing charge is mandatory: a missing row here is a
    card that grants no credit, not a layout drift, so it must not raise.
    """
    m = _WELCOME_RE.search(text)
    return None if m is None else to_float(m.group(1))


# ---- Wallonia parsers --------------------------------------------------------


# ---- EXTRACTOR ---------------------------------------------------------------


# Contracts whose feed-in credit indexes on a MONTHLY mean. The credit
# resolves against ENTSO-E spots the energy leg never fetches, so the config
# flow has to offer the optional key or the formula can never resolve and every
# path falls back to the card's printed figure. See
# ``Contract.spot_indexed_injection``.
_MONTH_INDEXED_INJECTION = frozenset({"energyvision_fixed_3y", "energyvision_fixed_1y"})

# Contracts whose card prices a direct-debit payer differently, so the config
# flow asks how the household pays. Only Brusol's Groene stroom does, at
# 20 EUR/yr off a 250 EUR standing charge. A registry flag beside the parsed
# figure, and they must agree: with this unset no step ever asks, nothing is
# stored and the discount is billed to nobody. ``test_direct_debit_registry_
# matches_the_cards`` holds the two against each other.
_DIRECT_DEBIT = frozenset({"energyvision_groene_stroom"})


EXTRACTOR = SupplierExtractor(
    # Re-measured when the tiered range landed: those three cards take 6,4 to
    # 8,2 s to lay out and parse against 4,3 to 5,2 s for the older pair, in
    # both measurement orders, so warm-up does not explain the gap. The budget
    # reserves the worst card of the supplier plus 10%, and leaving it at the
    # old 5,7 would let the sweep start a laadpunt card it cannot finish,
    # which is the one thing the reservation exists to prevent.
    sweep_cost_s=9.1,
    id="energyvision",
    label="EnergyVision",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            spot_indexed_injection=c.contract_id in _MONTH_INDEXED_INJECTION,
            direct_debit_discount=c.contract_id in _DIRECT_DEBIT,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)


__all__ = [
    "DISCOVER_IDS",
    "EXTRACTOR",
    "discover",
    "fetch",
    "fetch_for_month",
    "parse_snapshot",
    "probe",
]
