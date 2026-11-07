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
month (``EV-0726-...`` = July 2026) and Drupal adds a dedup suffix to a
card uploaded again (the fixed card ships as ``EV-0726-GS3JV-nl_0.pdf``),
so the plain name can be a draft. Since 30 October 2026 the tariefkaart
listing links no card: its script
(``/sites/default/files/ftp/tariefkaarten.min.js``) builds the names for
the month and shows the newest upload that answers, else last month's
card, and the fetch finds the current card the same way. From 2 November
2026 it asks the month's upload folder and the month before's ahead of
``inline-files`` (``_archive_card_urls``).

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
* ``GS3JV`` (Goedkope stroom 3 jaar vast, Flanders, and from November 2026
  Wallonia): a flat fixed rate for 3 years; its injection is indexed
  monthly (Belpex-SPP-M, known at month-end), so the credit is resolved
  against the delivery month's own SPP-weighted mean, and the printed
  monthly indicative is only the fallback for a month whose mean is not
  available.
* ``GS5JV`` (Goedkope stroom 5 jaar vast, Flanders and Wallonia, from
  November 2026): the same fixed shape on a 5-year lock.
* ``GS1JV`` (Électricité bon marché 1 an fixe, Wallonia, withdrawn from
  November 2026): the same fixed shape on a 1-year lock, off a French card
  that shares no wording with the Dutch ones. Parsed by the ``*_fr``
  helpers of ``_energyvision_wallonia``, which also read the Walloon cards
  of GS3JV and GS5JV.

* ``GS1800V`` (all three regions) / ``GSVI3`` / ``GSLP`` (Flanders):
  the tiered range, which bills a first tranche of the YEAR at a flat rate
  and the remainder on ``factor x Belpex-RLP-M + 20 EUR/MWh``. Parsed as a
  ``SpotMonthlyRates`` leg carrying the tranche, which ``resolve_volume_tier``
  folds into the coefficients against the entry's annual volume. GSVI3 fixes
  its feed-in price instead of indexing it, which is the only shape
  difference. The Flemish and Brussels GS1800V cards print the same energy
  leg figure for figure and differ only below it; the Walloon one is the
  French publication, read by the ``*_fr`` helpers.

Out of scope: gas (``GSG``, ``GS1JVG``) and the two tiered products that also
price self-consumed solar (``GSEZ``, ``GSEZLP``): their "Groene stroom uit
zonnepanelen op je dak" row is a third energy leg with no representation in
the model.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.util import dt as dt_util
from yarl import URL

from ..const import (
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_WALLONIA,
    WELCOME_CREDIT_ANNIVERSARY,
    WELCOME_CREDIT_PRO_RATA,
)
from ._energyvision_cards import (
    _VAT_RE,
    _direct_debit_discount,
    _extract_dynamic,
    _extract_fixed,
    _extract_tiered,
)
from ._energyvision_contracts import (
    _CONTRACTS,
    _CONTRACTS_BY_ID,
    _SITE_BASE,
    DISCOVER_IDS,
    _CardDef,
    _ContractDef,
)
from ._energyvision_overlays import (
    _DSO_ROWS,
    _extract_brussels_dsos,
    _extract_brussels_taxes,
    _extract_dsos,
    _extract_taxes,
)
from ._energyvision_wallonia import (
    _DSO_ROWS_FR,
    _parse_wallonia,
)
from ._parse import (
    require_every_area,
    running_month_short_of_an_area,
    to_float,
)
from ._pdf import (
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    head_or_raise,
    is_missing_card_error,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    EnergyRates,
)
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    with_vat_basis,
)
from .energiebe import _NUM

# The supplier's months end at midnight in Brussels, whatever zone Home
# Assistant runs in.
_BRUSSELS = ZoneInfo("Europe/Brussels")
# The listing of every card on the EnergyVision site, Flemish and Walloon
# alike. Since 30 October 2026 its script draws the cards and the page
# links none.
_LISTING_URL = f"{_SITE_BASE}/nl-be/tariefkaart"


# "Eenmalige welkomstkorting  200 €", the one-off first-year credit, printed
# above the standing charge on four of the six cards. Optional on purpose:
# Laadpunt prints no such row, and the Walloon card carries footnote e
# describing the credit while printing no amount for it, so there is nothing
# to grant. Only the Dutch wording is matched, because that is the only one
# any card has ever printed a figure next to.
_WELCOME_RE = re.compile(rf"Eenmalige\s+welkomstkorting\s+{_NUM}\s*€", re.IGNORECASE)

# The footnote stating how that credit is paid ("e/" on most cards, "d/" on
# the dynamic card and Brusol's Groene stroom), from its opening sentence to
# the next footnote's letter. The rule is read inside it alone: the "pro rata
# per dag" the same cards print in footnotes a and 4 are about the 1.800 kWh
# allowance and energy sharing.
_WELCOME_NOTE_RE = re.compile(
    r"We\s+kennen\s+je\s+een\s+eenmalige\s+welkomstkorting\s+toe"
    r".*?(?=\s[a-z]/\s|\sVoorwaarden\b|\Z)",
    re.DOTALL,
)
# Up to June 2026: "De korting wordt pas toegekend na één ononderbroken jaar
# verbruik en wordt toegepast op de afrekeningsfactuur".
_ANNIVERSARY_RE = re.compile(
    r"pas\s+toegekend\s+na\s+(?:één|een)\s+ononderbroken\s+jaar", re.IGNORECASE
)
# April to June 2026 add: "Indien de som van deze componenten lager is dan de
# vermelde welkomstkorting, wordt de korting beperkt tot maximaal het bedrag
# van deze som".
_LUMP_CAP_RE = re.compile(r"korting\s+beperkt\s+tot\s+maximaal", re.IGNORECASE)


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
    if card.index_url is None:
        url, _ = await _current_upload(session, contract, card)
    else:
        url = await _resolve_card_url(session, contract, card, card.index_url)
        # The page can lag the upload: Brusol's still linked the September
        # 2026 cards on 1 October while the October ones had sat in its
        # 2026-09 folder since the 30th. A card named for a month before
        # this one sends the fetch to where this month's card would be
        # filed, the archive's own lookup, and back to the linked card when
        # nothing is there yet or the lookup fails: the page's card is what
        # the fetch served before.
        linked = _card_month(url)
        today = dt_util.now().date()
        running = date(today.year, today.month, 1)
        if linked is not None and linked < running:
            try:
                newer = await _archived_card(
                    session, contract_id, card, region, running
                )
            except ExtractorError:
                newer = None
            if newer is not None:
                return newer
    text = await fetch_pdf_text_layout(session, url)
    snap = parse_snapshot(contract_id, text, url, region=region)
    # Here and not in the reader: a past month's card keeps a short table.
    require_every_area("EnergyVision", snap, _areas(region))
    return snap


def _areas(region: str) -> tuple[str, ...]:
    """The network areas a card of ``region`` prints a row for."""
    if region == REGION_BRUSSELS:
        return (DSO_SIBELGA,)
    rows = _DSO_ROWS_FR if region == REGION_WALLONIA else _DSO_ROWS
    return tuple(key for _, key in rows)


# The pricing month every card's file name starts with: "EV-0926-..." is
# September 2026.
_CARD_MONTH_RE = re.compile(r"EV-(\d{2})(\d{2})-")


def _card_month(url: str) -> date | None:
    """The month a card's file name says it prices, or None for a name
    that carries none."""
    match = _CARD_MONTH_RE.search(url)
    if match is None or not 1 <= int(match.group(1)) <= 12:
        return None
    return date(2000 + int(match.group(2)), int(match.group(1)), 1)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card EnergyVision published for one past month, or ``None``.

    The live fetch has to scrape the listing because the CURRENT card carries
    Drupal's dedup suffix (``EV-0726-GS3JV-nl_0.pdf``), but a past month is not
    on that listing at all. Its plain filename resolves directly (every
    product answered 200 for every month it existed, measured across GSDYN /
    GS3JV / GS1800V / GSVI3 / GSLP and March to September 2026), but that is
    the FIRST upload of the month's card, and EnergyVision corrects a card by
    uploading it again: the listing then links the reissue, and the plain
    file keeps the draft. ``_uploads_newest_first`` reads the reissues first.

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
    return await _archived_card(session, contract_id, card, region, first)


async def _archived_card(
    session: aiohttp.ClientSession,
    contract_id: str,
    card: _CardDef,
    region: str,
    first: date,
) -> SupplierSnapshot | None:
    """The card filed for the month starting ``first``, or None when none of
    the places it could be filed holds it."""
    contract = _CONTRACTS_BY_ID[contract_id]
    for plain in _archive_card_urls(contract, card, first):
        for url in await _uploads_newest_first(session, plain, first):
            try:
                text = await fetch_pdf_text_layout(session, url)
                snap = parse_snapshot(contract_id, text, url, region=region)
            except ExtractorError as err:
                # A timeout, a reset or a 5xx says nothing about the month:
                # raise, so the month cache retries it instead of caching it
                # as absent.
                if is_transient_fetch_error(str(err)):
                    raise
                continue
            # Every card prints "geldig ... tot en met" so valid_until is
            # parsed and the authoritative tier of the cross-check applies. It
            # is what catches a CDN serving the current card under an archived
            # name. A candidate that turns out to hold another month's card is
            # not the end of the search: try the next one before giving the
            # month up.
            checked = archive_validity_check(snap, text, first)
            if checked is None:
                continue
            # The running month's card is held to every area, as ``fetch``
            # holds the live one, and an earlier upload of it is tried as a
            # card that did not read would be.
            if running_month_short_of_an_area(checked.dsos, _areas(region), first):
                continue
            return checked
    return None


# Where the walk up the dedup suffixes gives up. The most measured is eight
# reissues of one card (EV-0126-GS1800V-nl_0 to _7).
_MAX_REUPLOADS = 20


async def _uploads_newest_first(
    session: aiohttp.ClientSession, url: str, first: date
) -> list[str]:
    """The uploads of the card filed at ``url`` that were online while the
    month starting ``first`` ran, newest first, then the plain name.

    Drupal never overwrites a file: uploading a card again stores it as
    ``<name>_0.pdf``, then ``_1``, ``_2`` and so on, and leaves the earlier
    files where they were. EnergyVision corrects a card that way, so the
    plain name is its first draft: the March 2026 GS3JV card is 285 EUR of
    welcome credit there and 300 on the reissue the listing linked. The
    suffixes are walked up with HEAD requests until one is missing, before
    any card is downloaded.

    An upload made after the month ended is left out, because it is not the
    card the month was sold on: on 1 April 2026 the Walloon March 1.800 kWh
    card was uploaded three more times with a 50 EUR standing charge, where
    the uploads made during March print none. What is left is the card the
    listing linked when the month closed. The month ends at midnight in
    Brussels, against the ``Last-Modified`` the server gives each upload;
    one it gives none for is kept, since nothing says it came late.

    A transient failure is raised, like a failed download, so the month is
    asked again rather than settled on a draft.
    """
    following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    month_end = datetime.combine(following, time(), _BRUSSELS)
    stem = url.removesuffix(".pdf")
    found: list[str] = []
    for n in range(_MAX_REUPLOADS):
        candidate = f"{stem}_{n}.pdf"
        try:
            modified = await head_or_raise(session, candidate)
        except ExtractorError as err:
            if is_transient_fetch_error(str(err)):
                raise
            break
        if modified is None or _http_date(modified) < month_end:
            found.append(candidate)
    return [*reversed(found), url]


def _http_date(value: str) -> datetime:
    """An HTTP date header, or the epoch for one that does not read, so an
    unreadable stamp counts as early rather than dropping the upload."""
    try:
        stamp = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(0, UTC)
    # HTTP dates are GMT; one written "-0000" parses without a zone.
    return stamp if stamp.tzinfo is not None else stamp.replace(tzinfo=UTC)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness key: the current card's URL and Last-Modified, which
    a new month and a card uploaded again both move, where the card is found
    by its name, else the ETag or Last-Modified of the page that links it.

    Brusol's pages are Drupal dynamic pages and answer with neither header,
    so Brussels has no probe key and falls back to the 24h TTL, the path
    Engie and Luminus take. Returning None for that is the documented way to
    say so; it does not refetch the card every tick.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    card = None if contract is None else contract.card(region)
    if contract is None or card is None:
        return None
    if card.index_url is not None:
        return await head_freshness_key(
            session, card.index_url, prefer=("ETag", "Last-Modified")
        )
    try:
        url, modified = await _current_upload(session, contract, card)
    except ExtractorError:
        return None
    return f"{url} {modified}"


def _index_urls() -> tuple[str, ...]:
    """Every page a current card is advertised on, in registration order and
    without repeats."""
    seen: dict[str, None] = {}
    for contract in _CONTRACTS:
        for card in contract.cards.values():
            if card.index_url is not None:
                seen.setdefault(card.index_url, None)
    return tuple(seen)


def _card_href_re(code: str, token: str) -> re.Pattern[str]:
    """Match one card's href on an index page.

    The href is absolute on the Brusol pages and the site prefix optional, so
    a site-relative one reads too, and Brusol has ended one in a space
    ("EV-1026-GSG-BXL-nl.pdf "). The directory is not anchored: Brusol files
    each card under the month it uploaded it. Nor is the host, which
    :func:`_on_page_site` holds to the page's. The card's name is looked for
    ahead and the value then read to its last ".pdf" once: split around the
    name, a value naming the card over and over with no closing quote was
    read to its end again from each.
    """
    return re.compile(
        rf'href="((?:https?://[^"/]+)?/sites/default/files/'
        rf'(?=[^"]*?EV-\d{{4}}-{re.escape(code)}-{re.escape(token)})[^"]*\.pdf)\s*"',
        re.IGNORECASE,
    )


def _on_page_site(page: str, href: str) -> str | None:
    """``href`` read as a link on ``page``, or None where it leaves the
    page's own site.

    Every card and script the pages have linked sits on the page's own
    site, Brusol's cards on www.brusol.be and the listing's script on
    www.energyvision.be, so a link anywhere else is one the page was changed
    to carry, and following it would fetch whatever that page points at.
    """
    url = urljoin(page, href)
    try:
        same = URL(url).origin() == URL(page).origin()
    except ValueError:
        return None
    return url if same else None


# The script the listing draws its cards with, as the listing names it, and
# one product of its catalogue: the code, then the fields before the
# product's first nested object (its legacy file map) or its end. Neither
# backtracks past the next quote or brace. The script's path is checked ahead
# and then taken whole: split around the folder name, a value naming it over
# and over was read to its end again from each.
_SCRIPT_SRC_RE = re.compile(r'src="((?=[^"]*/tariefkaarten)[^"]*)"')
_PRODUCT_RE = re.compile(r'\{code:"([A-Z0-9-]+)",([^{}]*)')
_SOLD_REGION_RE = re.compile(r'region:"(?:vlaanderen|wallonie)"')


async def _listed_codes(session: aiohttp.ClientSession) -> set[str]:
    """The product codes the listing's script sells: those of Flanders and
    Wallonia with no end month, a Walloon code without its "-WAL" (the card
    is ``EV-<MMYY>-GS1JV-WAL-fr.pdf``). The script is the one the listing
    names, so a renamed one is followed rather than an old copy read.
    Nothing when either cannot be read, which the live check reports as
    every product gone."""
    try:
        listing = await fetch_text(session, _LISTING_URL)
        src = _SCRIPT_SRC_RE.search(listing)
        url = None if src is None else _on_page_site(_LISTING_URL, src.group(1))
        if url is None:
            return set()
        script = await fetch_text(session, url)
    except ExtractorError:
        return set()
    return {
        code.removesuffix("-WAL")
        for code, fields in _PRODUCT_RE.findall(script)
        if 'end:"' not in fields and _SOLD_REGION_RE.search(fields)
    }


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the residential product codes currently advertised, so
    live_check can flag a new SKU. Diffed against :data:`DISCOVER_IDS`.

    EnergyVision's own codes come off its listing's script, and Brusol's off
    every page a registered card is advertised on, with every token in use:
    a product is published for one region in one language, so matching one
    page or one token would silently drop a whole region's catalogue from
    the drift check. The tokens come from the registered cards rather than a
    literal, so a card added with a new token extends the check with it.

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
    found = await _listed_codes(session)
    for url in _index_urls():
        try:
            html = await fetch_text(session, url)
        except ExtractorError:
            continue
        found |= set(re.findall(rf"EV-\d{{4}}-([A-Z0-9]+)-(?:{tokens})", html))
    return found


# EnergyVision's listing has asked the upload folders before ``inline-files``
# since its script of 2 November 2026, when the corrected Flemish 1.800 kWh
# card sat in 2026-11 alone. The folders also hold drafts of earlier months,
# which were sold off ``inline-files``.
_UPLOAD_FOLDERS_FROM = date(2026, 11, 1)


def _archive_card_urls(
    contract: _ContractDef, card: _CardDef, first: date
) -> tuple[str, ...]:
    """The plain URLs an archived card for ``first`` could sit at, in the
    order to ask them. Each is then walked for its reissues.

    Brusol files each card under the month it UPLOADED it, which is usually
    the month before delivery and sometimes the delivery month itself, so
    both are tried; measured over March to September 2026, the pair covers
    every card published. The delivery month's folder goes first: a card in
    both is one uploaded again, and the August 2026 cards in the July folder
    are the drafts. From ``_UPLOAD_FOLDERS_FROM`` EnergyVision's cards are
    asked in the same two folders, then in ``inline-files``; before it, in
    ``inline-files`` alone.
    """
    stamp = f"{first.month:02d}{first.year % 100:02d}"
    name = f"EV-{stamp}-{contract.code}-{card.token}.pdf"
    inline = f"{card.site}/sites/default/files/inline-files/{name}"
    if not card.archive_by_upload_month and first < _UPLOAD_FOLDERS_FROM:
        return (inline,)
    previous = date(first.year, first.month, 1) - timedelta(days=1)
    folders = tuple(
        f"{card.site}/sites/default/files/{folder:%Y-%m}/{name}"
        for folder in (first, previous)
    )
    return folders if card.archive_by_upload_month else (*folders, inline)


# The last re-upload the listing's script asks for ("maxSuffix": 9). The
# archive walk above goes further, to _MAX_REUPLOADS.
_MAX_SUFFIX = 9


async def _newest_upload(
    session: aiohttp.ClientSession, url: str
) -> tuple[str, str | None] | None:
    """The newest upload of the card filed at ``url`` and its
    Last-Modified, or None where none answers.

    The listing's script takes the newest of the plain name, ``_0`` and
    ``_1`` that answers and, past ``_1``, each next one for as long as it
    answers; the same names are asked here in the same order. A failure
    other than a name that is not there raises, so a site that is down is
    not taken for a month without a card.
    """
    stem = url.removesuffix(".pdf")
    names = (url, *(f"{stem}_{n}.pdf" for n in range(_MAX_SUFFIX + 1)))
    newest: tuple[str, str | None] | None = None
    for asked, name in enumerate(names):
        try:
            modified = await head_or_raise(session, name)
        except ExtractorError as err:
            if not is_missing_card_error(str(err)):
                raise
            # From _1 on, the first name missing ends the walk.
            if asked >= 2:
                break
            continue
        newest = (name, modified)
    return newest


async def _current_upload(
    session: aiohttp.ClientSession, contract: _ContractDef, card: _CardDef
) -> tuple[str, str | None]:
    """The card the listing shows today and its Last-Modified: the newest
    upload of this month's card, else of last month's, which the listing
    goes on showing until this month's is up.

    The month is Home Assistant's, which the coordinator holds a card's
    validity to; the listing's script takes the one in Brussels.

    With no upload at all every name answered 404, which is how the failure
    reads, so it is a card missing rather than a layout change: an entry is
    told its card is not out yet, and one on a withdrawn product keeps the
    last card it holds.
    """
    first = dt_util.now().date().replace(day=1)
    for month in (first, (first - timedelta(days=1)).replace(day=1)):
        for url in _archive_card_urls(contract, card, month):
            found = await _newest_upload(session, url)
            if found is not None:
                return found
    raise ExtractorError(
        f"HTTP 404 fetching {_archive_card_urls(contract, card, first)[0]}: "
        f"EnergyVision has no card {contract.code} ({card.token}) for "
        f"{first:%Y-%m} or the month before"
    )


async def _resolve_card_url(
    session: aiohttp.ClientSession, contract: _ContractDef, card: _CardDef, page: str
) -> str:
    """The newest card ``page``, the page ``card`` is advertised on, links,
    by the month its file name prices.

    A page can link more than one: from October 2026 Brusol's signup pages
    carry the current card on their button and last month's in the signup
    form's consent checkbox, which comes first in the HTML.
    """
    html = await fetch_text(session, page)
    urls = [
        url
        for href in _card_href_re(contract.code, card.token).findall(html)
        if (url := _on_page_site(page, href)) is not None
    ]
    if not urls:
        raise ExtractorError(
            f"EnergyVision: no listing entry for card {contract.code} "
            f"({card.token}) on {page}"
        )
    return max(urls, key=lambda url: _card_month(url) or date.min)


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
    kind, lump_capped = _welcome_credit_rule(text)
    return with_vat_basis(
        SupplierSnapshot(
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
            welcome_credit_kind=kind,
            welcome_credit_lump_capped=lump_capped,
            direct_debit_discount_eur=_direct_debit_discount(
                text, energy.yearly_fixed_fee
            ),
        ),
        printed_vat_rate(text, _VAT_RE),
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


def _welcome_credit_rule(text: str) -> tuple[str, bool]:
    """How the card pays its welcome credit, and whether a lump is capped.

    The cards up to June 2026 pay it at the anniversary, on the settlement
    invoice after a year of uninterrupted supply; from July 2026 it is
    "toegekend pro rata per dag over de facturatie periode". The October 2026
    1.800 kWh card states neither and is read as pro rata, the rule the cards
    before it stated, which is also what a card printing no footnote gets.
    """
    note = _WELCOME_NOTE_RE.search(text)
    if note is None or _ANNIVERSARY_RE.search(note.group(0)) is None:
        return WELCOME_CREDIT_PRO_RATA, False
    return WELCOME_CREDIT_ANNIVERSARY, _LUMP_CAP_RE.search(note.group(0)) is not None


# ---- Wallonia parsers --------------------------------------------------------


# ---- EXTRACTOR ---------------------------------------------------------------


# Contracts whose feed-in credit indexes on a MONTHLY mean. The credit
# resolves against ENTSO-E spots the energy leg never fetches, so the config
# flow has to offer the optional key or the formula can never resolve and every
# path falls back to the card's printed figure. See
# ``Contract.spot_indexed_injection``.
_MONTH_INDEXED_INJECTION = frozenset(
    {"energyvision_fixed_3y", "energyvision_fixed_5y", "energyvision_fixed_1y"}
)

# Contracts whose card prices a direct-debit payer differently, so the config
# flow asks how the household pays. Only Brusol's Groene stroom does, at
# 20 EUR/yr off a 250 EUR standing charge on its cards up to September 2026.
# The October card dropped that footnote, but the months before it still bill
# on cards that print it, so the flag stays. A registry flag beside the parsed
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
            withdrawn=c.withdrawn,
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
