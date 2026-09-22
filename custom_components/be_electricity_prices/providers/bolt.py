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

"""Bolt Belgium tariff card extractor.

Bolt publishes tariff cards at predictable URLs:

    https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
    https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_<version>.pdf

Fixed contracts roll monthly via the YYYYMM suffix. Variable contracts
carry a version-number suffix that Bolt bumps in place whenever it
revises the formula, on no fixed schedule; the superseded files stay
served, so pinning a version reads a stale card that still answers 200
and still parses. The version is therefore read from the listing page,
per (slug, segment), never hardcoded and never as one version across the
whole family: a pinned ``_11`` kept billing June's formula for ten
weeks after ``_13`` shipped, and a global maximum would 404 any slug
whose counter lagged.
Each PDF covers all three regions in one document - same convention as
Eneco.

Bolt's PDFs are visually rich (5 MB each) with rotated columns and a
column-major text layout that pypdf can't read. The extractor goes
through ``pdfplumber`` for layout-aware extraction.

Bolt's price model deviates from the rest in two ways: the fixed fee
is billed per MONTH (``Frais de plateforme 10,99 €/mois``) so the
extractor multiplies by 12 to fit the integration's annual fee
convention, and the Flanders renewables value is split across two
separate lines (``Certificats verts`` + ``WKK``).
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
    FR_MONTHS,
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
    require_contract,
)
from .base import (
    CardNotReadableError,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from ._rates import (
    Contract,
    TariffKind,
)
from ._bolt_overlays import (
    _extract_brussels_dsos,
    _extract_energy_fund,
    _extract_flanders_dsos,
    _extract_renewables,
    _extract_taxes,
    _extract_wallonia_dsos,
)
from ._bolt_cards import (
    _extract_energy,
    _extract_injection,
)

_LOGGER = logging.getLogger(__name__)


_BASE_URL = "https://files.boltenergie.be/pricelists"

_LISTING_URL = "https://www.boltenergie.be/fr/listes-des-prix"
# Every card URL on the listing page, as (folder, slug, segment, version).
# The version group is what keeps the variable family current; ``discover``
# ignores it, and filters to the residential segment, so it still diffs on
# folder/slug alone.
#
# The version is \w+ and the match is case-insensitive so that ``discover``
# keeps seeing a card whose suffix stops being purely numeric or whose
# extension is uppercased. Pinning \d+ here to serve the resolver narrowed
# discovery as a side effect, and a slug it cannot see is a new product the
# catalog diff reports as silence. _resolve_variable_suffix does its own
# numeric filtering instead, which it needs regardless for max(key=int).
_CARD_URL_RE = re.compile(
    r"pricelists/(fix|var)/([a-z_]+)_(res|pro)_el_fr_(\w+)\.pdf", re.IGNORECASE
)
# Used only when the listing page cannot be read at all. Serving a known
# version beats failing every Bolt entry on a listing outage, but it is a
# floor, not a pin: it is the newest version seen when this was last
# touched, and _resolve_variable_suffix overrides it on every good fetch.
_VARIABLE_SUFFIX_FALLBACK = "13"

# Bolt's fix cards print "Carte Tarifaire Bolt Fixe <Month> <Year>" in
# the header but never expose a parseable valid_until, so the archive
# cross-check falls back to a textual month match on these names.
_FR_MONTH_NAMES = FR_MONTHS


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    folder: str  # 'fix' or 'var'
    slug: str  # filename prefix
    # 'res' or 'pro' in the filename. Bolt publishes a professional
    # edition of every product at the same path with the segment
    # swapped: same layout, priced excluding VAT.
    segment: str = "res"
    # True when the customer chooses whether this card settles per quarter-hour
    # or against the RLP-weighted month. Every variable card, no fixed one.
    settlement: bool = False

    @property
    def professional(self) -> bool:
        return self.segment == "pro"


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("bolt_fix", "Bolt Fixe (1 year)", "fixed", "fix", "fix"),
    _ContractDef(
        "bolt_plenty_fix", "Bolt Plenty Fixe (1 year)", "fixed", "fix", "plenty_fix"
    ),
    # Every variable card is sold on either settlement, and says so in the same
    # paragraph: "Dans le cadre d'une facturation dynamique, la consommation ou
    # l'injection enregistree est multipliee, pour chaque quart d'heure, par la
    # valeur Belpex correspondante pour ce meme quart d'heure. En optant pour
    # une facturation variable, nous redistribuerons la consommation ponderee
    # RLP." One printed formula, two ways of settling it, and the card cannot
    # say which one a given account is on, so the entry answers, and
    # ``quarter_hourly_option`` is what puts the question in the flow.
    _ContractDef(
        "bolt_variable", "Bolt Variable", "variable", "var", "bolt", settlement=True
    ),
    _ContractDef(
        "bolt_plenty",
        "Bolt Plenty Variable",
        "variable",
        "var",
        "plenty",
        settlement=True,
    ),
    _ContractDef(
        "bolt_online", "Bolt Online", "variable", "var", "online", settlement=True
    ),
    _ContractDef(
        "bolt_plenty_online",
        "Bolt Plenty Online",
        "variable",
        "var",
        "plenty_online",
        settlement=True,
    ),
    # The professional editions: same paths with the segment swapped.
    _ContractDef(
        "bolt_pro_fix", "Bolt Fixe (pro, 1 year)", "fixed", "fix", "fix", segment="pro"
    ),
    _ContractDef(
        "bolt_pro_plenty_fix",
        "Bolt Plenty Fixe (pro, 1 year)",
        "fixed",
        "fix",
        "plenty_fix",
        segment="pro",
    ),
    _ContractDef(
        "bolt_pro_variable",
        "Bolt Variable (pro)",
        "variable",
        "var",
        "bolt",
        segment="pro",
        settlement=True,
    ),
    _ContractDef(
        "bolt_pro_plenty",
        "Bolt Plenty Variable (pro)",
        "variable",
        "var",
        "plenty",
        segment="pro",
        settlement=True,
    ),
    _ContractDef(
        "bolt_pro_online",
        "Bolt Online (pro)",
        "variable",
        "var",
        "online",
        segment="pro",
        settlement=True,
    ),
    _ContractDef(
        "bolt_pro_plenty_online",
        "Bolt Plenty Online (pro)",
        "variable",
        "var",
        "plenty_online",
        segment="pro",
        settlement=True,
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(contract: _ContractDef, suffix: str | None = None) -> str:
    if contract.folder == "fix":
        # Fixed cards roll monthly on the Belgian calendar boundary.
        # Use Brussels local time so the suffix flips at the same
        # instant the supplier rotates the URL; UTC would mis-key by
        # last month for the first 1-2 hours of every Brussels month.
        suffix = suffix or dt_util.now().strftime("%Y%m")
    else:
        suffix = suffix or _VARIABLE_SUFFIX_FALLBACK
    return (
        f"{_BASE_URL}/{contract.folder}/"
        f"{contract.slug}_{contract.segment}_el_fr_{suffix}.pdf"
    )


async def _resolve_variable_suffix(
    session: aiohttp.ClientSession, contract: _ContractDef
) -> str:
    """Return the current variable-card version for ``contract``.

    Bolt bumps this suffix in place and leaves every superseded file
    served, so the old URL keeps answering 200 with a stale card: there
    is no 404, no expiry and no error to notice. The listing page is the
    only thing that says which version is current, so read it.

    Resolved PER (slug, segment), not as one version across the whole
    variable family. The four slugs happen to move in lockstep today, but
    nothing makes them: taking a global maximum would point a slug whose
    counter lagged at a file that does not exist, turning a stale card
    into a 404 for that product.

    Compared numerically, not lexically: ``"9"`` must not outrank
    ``"13"``. Falls back to :data:`_VARIABLE_SUFFIX_FALLBACK` when the
    listing is unreachable or does not advertise this card, so a listing
    outage degrades to a known version instead of failing every Bolt
    entry.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        _LOGGER.warning(
            "Bolt: listing page unreadable; falling back to variable card "
            "version _%s for %s, which may be superseded",
            _VARIABLE_SUFFIX_FALLBACK,
            contract.contract_id,
        )
        return _VARIABLE_SUFFIX_FALLBACK
    versions: list[str] = [
        version
        for folder, slug, segment, version in _CARD_URL_RE.findall(html)
        # isdigit(): the URL builder and max(key=int) below both need a
        # number. A non-numeric suffix means Bolt reshaped the filename, so
        # fall back rather than crash, and the live-check freshness gate,
        # which scans with \w+, fails the run so the reshape gets noticed.
        if folder.lower() == "var"
        and slug.lower() == contract.slug
        and segment.lower() == contract.segment
        and version.isdigit()
    ]
    if not versions:
        _LOGGER.warning(
            "Bolt: listing page advertises no %s/%s variable card; falling back "
            "to version _%s, which may be superseded",
            contract.slug,
            contract.segment,
            _VARIABLE_SUFFIX_FALLBACK,
        )
        return _VARIABLE_SUFFIX_FALLBACK
    return max(versions, key=int)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Bolt's PDFs cover every region.
) -> str | None:
    """Cheap freshness probe: HEAD the listing page, return its ETag.

    Bolt's listing returns a stable ETag and the server honours
    ``If-None-Match`` with a 304 response. We just want a key that flips
    on supplier changes, so reading the ETag header on a HEAD round-trip
    is enough.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    return await head_freshness_key(
        session, _LISTING_URL, prefer=("ETag", "Last-Modified")
    )


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return ``{folder}/{slug}`` for every residential electricity card.

    Bolt's prices listing page links every PDF directly. Filter to
    residential electricity (``_res_el_fr_``) and extract the
    ``<folder>/<slug>`` prefix; live_check diffs against the registry's
    ``{c.folder + '/' + c.slug for c in _CONTRACTS}`` set.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return {
        f"{folder.lower()}/{slug.lower()}"
        for folder, slug, segment, _version in _CARD_URL_RE.findall(html)
        if segment.lower() == "res"
    }


# ---- top-level fetch + parser -------------------------------------------------


async def _fetch_pdf_text(
    session: aiohttp.ClientSession, contract: _ContractDef
) -> tuple[str, str]:
    """Fetch the latest PDF text for ``contract``, applying the
    fixed-card fallback. Returns ``(url, text)``.

    Lifted out of :func:`fetch` so the live-check script can fetch
    once per contract and parse three region-specific snapshots from
    the same text: Bolt's PDFs cover all regions, so doing it
    per-(contract, region) wastes a 5+ MB round-trip twice.
    """
    # Bolt's tariff PDFs are ~5 MB each and the CDN occasionally needs
    # well over the shared 30 s default to deliver one (issue #13:
    # all six fetches timed out for ~25 minutes on 2026-05-09 while
    # the URLs themselves were healthy). Use a 60 s budget so a 2-3x
    # CDN slowdown still yields a snapshot instead of UpdateFailed.
    pdf_timeout = 60
    if contract.folder == "var":
        # Resolved per fetch rather than cached: the listing is small
        # next to the 5 MB card that follows it, and the same
        # fetch-the-index-then-the-card shape is what Ecopower already
        # does. A cache here would only add a staleness window to the
        # very thing that exists to prevent staleness.
        suffix: str | None = await _resolve_variable_suffix(session, contract)
    else:
        suffix = None
    url = _document_url(contract, suffix=suffix)
    try:
        return url, await fetch_pdf_text_layout(session, url, timeout=pdf_timeout)
    except ExtractorError as primary_err:
        # Fixed cards may not be published yet on the 1st of the month;
        # fall back to the previous month so the user keeps seeing
        # plausible prices instead of UpdateFailed. Bolt cards expose no
        # parseable valid_until, so the fallback can't signal staleness
        # through it; the warning logged below is the only trace that
        # last month's card is being served.
        #
        # Which makes it critical that ONLY an unpublished card takes this
        # path. Two things that are not one:
        #  * a card that downloaded fine and carries no text layer, and
        #  * a fetch that failed transiently - a timeout, a 5xx, a 403.
        # Either would serve last month's prices with no Repairs card and no
        # staleness signal (the successful fallback fetch resets the snapshot
        # age), which is worse than the loud failure it replaces. A transient
        # error means THIS month's card is probably fine and simply did not
        # arrive, so the right move is to fail and let the coordinator keep
        # the snapshot it already has - which is this month's.
        # An unpublished card answers 404, which is classified permanent, so
        # the 1st-of-month case this exists for still works.
        # Live-check run 32223861276 is what this cost: a runner-wide network
        # slowdown timed out three fixed contracts, each silently fell back a
        # month, and the card-period gate reported nine stale-card failures
        # against a supplier that was publishing normally.
        if (
            contract.folder != "fix"
            or isinstance(primary_err, CardNotReadableError)
            or is_transient_fetch_error(str(primary_err))
        ):
            raise
        # Same Brussels-local anchor as ``_document_url``: the
        # "previous month" boundary follows local time so we don't
        # accidentally roll back two months on the new-month UTC seam.
        previous = (dt_util.now().replace(day=1) - timedelta(days=1)).strftime("%Y%m")
        fallback_url = _document_url(contract, suffix=previous)
        _LOGGER.warning(
            "Bolt %s: current-month PDF unavailable (%s); "
            "falling back to previous-month card %s",
            contract.contract_id,
            primary_err,
            fallback_url,
        )
        return fallback_url, await fetch_pdf_text_layout(
            session, fallback_url, timeout=pdf_timeout
        )


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the latest Bolt PDF for ``contract_id`` (covers every region)."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Bolt")
    url, text = await _fetch_pdf_text(session, contract)
    return parse_snapshot(contract_id, text, region, url)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch a past month's Bolt fix-family card (returns ``None`` for
    products without a date-keyed archive).

    Bolt's fix folder is archived monthly under the ``YYYYMM`` suffix going
    back to 2024-01, and that is the whole folder: ``fix`` and ``plenty_fix``,
    residential and professional alike, all four address their current card by
    month too. The variable folder uses a stable version-number suffix
    (``bolt_res_el_fr_13.pdf``), so older months can't be addressed there,
    those return ``None`` and the YTD path falls back to the current snapshot
    as a proxy.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    contract = _CONTRACTS_BY_ID[contract_id]
    if contract.folder != "fix":
        # The variable folder has no month-addressable card.
        return None
    suffix = year_month.strftime("%Y%m")
    url = _document_url(contract, suffix=suffix)
    try:
        text = await fetch_pdf_text_layout(session, url, timeout=60)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    try:
        snap = parse_snapshot(contract_id, text, region, url)
    except ExtractorError:
        return None
    # The month is URL-keyed, but guard against the CDN ever serving a
    # current card under a historical URL: Bolt cards carry no parseable
    # valid_until, so cross-check the printed "<Month> <Year>" header
    # against the requested month and fall back to the proxy snapshot on
    # a mismatch rather than mis-billing a past month at current rates.
    return archive_validity_check(snap, text, year_month, month_names=_FR_MONTH_NAMES)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Bolt")
    # Bolt's PDFs sprinkle U+2028 LINE SEPARATOR characters where one
    # would expect a newline; normalize to '\n' so a single set of
    # regexes covers every block.
    text = text.replace(" ", "\n")

    professional = contract.professional
    energy = _extract_energy(text, contract.kind, professional=professional)
    injection = _extract_injection(text)
    if professional and injection is not None:
        injection = replace(injection, vat_applies=True)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, region_connection_fee = _extract_taxes(
        text, region
    )
    energy_fund = (
        _extract_energy_fund(text, professional=professional)
        if region == REGION_FLANDERS
        else 0.0
    )
    flanders_renewables, wallonia_renewables, brussels_renewables = _extract_renewables(
        text
    )
    if region != REGION_FLANDERS:
        flanders_renewables = 0.0
    if region != REGION_WALLONIA:
        wallonia_renewables = 0.0
    if region != REGION_BRUSSELS:
        brussels_renewables = 0.0

    if region == REGION_FLANDERS:
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        dsos = _extract_wallonia_dsos(text)
    else:
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="bolt",
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
            # The professional card prices excluding VAT throughout - its
            # distribution block is still headed TTC, but the numbers match
            # the other suppliers' ex-VAT tables to the cent, so the label
            # is stale, not the values. base.apply_vat resolves it.
            vat_rate=VAT_RATE_STANDARD if professional else 0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


def _extract_publication_month(text: str) -> str:
    """The card's "<Month> <Year>" header, verbatim.

    The accented-letter classes span the whole Latin-1 range rather than
    the handful of accents French month names actually use. Bolt's August
    2026 fixed card prints "Aôut 2026" - the circumflex on the wrong vowel
    - and an exact `[a-zéèû]` class dropped the label to "" on a typo that
    changes nothing about the card's meaning. This value is a display
    label (diagnostics, the snapshot_publication attribute) and never
    feeds pricing, so tolerating a misspelling beats blanking it.
    """
    match = re.search(r"^([A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+\s+\d{4})\s*/", text, re.MULTILINE)
    return match.group(1) if match else ""


# ---- taxes --------------------------------------------------------------------


# ---- DSO row parsers ----------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=45.3,
    id="bolt",
    label="Bolt",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            professional=c.professional,
            # Every Bolt card bills injection per quarter-hour off the Belpex
            # index, so the credit needs spots the fixed or variable energy leg
            # never fetches. Set on the variable cards too, settlement box or
            # not: with it unticked the energy leg is a printed monthly rate
            # that asks for no spot, and the feed-in still needs one. With it
            # ticked the energy formula collects the key anyway and this is
            # merely redundant, which is the harmless direction.
            spot_indexed_injection=True,
            quarter_hourly_option=c.settlement,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
