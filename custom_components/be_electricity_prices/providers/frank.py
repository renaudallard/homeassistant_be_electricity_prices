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

"""Frank Energie Belgium tariff extractor, dynamic and variable.

Frank Energie Belgium publishes monthly tariff cards as PDFs hosted on a
Sanity CMS CDN.  Six dynamic contract tiers share the same PDF layout
with different formula parameters (factor, base, monthly fee):

  - Dynamisch (standard)
  - Dynamisch HV
  - Dynamisch VT (titled "Korting" until September 2026)
  - Dynamisch JN (lower subscription, different formula)
  - Dynamisch Slim (requires smart devices: solar, EV, battery, heat pump)
  - Dynamisch Plus (since October 2026, lowest subscription and margin)

Since October 2026 HV and VT print the same formula and subscription and
neither grants a cashback: the VT card differs only in billing a "dubbel
voorschot". Until September HV had the higher subscription and the lower
margin, and the Korting card the 120 EUR cashback that gave it its name.

The November 2026 JN card prints its subscription as 0,00 and waives it for
a contract's first twelve months, after which "gelden de abonnementskosten
volgens de tariefkaart Dynamisch - JN - Oktober 2026". The subscription is
billed at the one the named card prints, and the waiver is granted back to a
contract signed on the card as a first-year credit, pro rata like
EnergyVision's welcome credit; see ``_with_waived_fee``.

Every tier prices "BELPEX per uur" and carries the same footnote offering
the 15-minute grid instead:

    *Afrekening uur- of kwartierprijzen
    De energieprijs wordt standaard berekend op basis van uurprijzen (EPEX
    Spot Belgie / Belpex). Je kan er via de Frank Energie app voor kiezen om
    de energieprijs te laten berekenen op basis van kwartierprijzen. In dat
    geval wordt het verbruik per kwartier afgerekend volgens de bijbehorende
    kwartiermarktprijzen [Quarter Hourly BELPEX]. De keuze voor uurprijzen of
    kwartierprijzen kan maandelijks aangepast worden.

The formula is the same on either grid, so the card cannot say which side a
household is on and nothing here can read it. The tiers carry
``quarter_hourly_option`` and the config flow asks; see ``_TIERS``.

The same tiers are sold as Variabel cards ("Stroom Variabel"), priced on the
month's RLP-weighted index and crediting the feed-in on its SPP-weighted one,
the kind energie.be's Variabel is. They share the dynamic cards' subscription
row, waiver, levies, network table and title, read here for both; their
energy and feed-in readers are in ``_frank_variable.py``. A file is told from
the other product's by the word after which its tier stands, "Dynamisch" or
"Variabel".

Data source: Sanity file asset API (public, no auth required). The terms
page (https://www.frankenergie.be/nl/voorwaarden) links each tier's current
card, which discover reads for the live check.
Region: Flanders only (all 8 Fluvius sub-areas).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import date

import aiohttp

from ..const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    REGION_FLANDERS,
    WELCOME_CREDIT_ANNIVERSARY,
    WELCOME_CREDIT_PRO_RATA,
)
from ._frank_variable import variable_energy, variable_injection
from ._links import on_hosts
from ._parse import (
    LINE_BREAK,
    SIGN_CHARS,
    parse_sign,
    parse_vreg_network_ceiling,
    regional_tax_overlay,
    require_every_area,
    running_month_short_of_an_area,
    to_float,
)
from ._pdf import (
    _MONTH_NAMES,
    NL_MONTHS,
    NUM_NO_THOUSANDS,
    fetch_pdf_text_layout,
    fetch_text,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    DynamicRates,
    InjectionRates,
    SpotMonthlyRates,
)
from ._validity import (
    archive_validity_check,
    parse_valid_until,
    scan_month_end,
)
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
)

_LOGGER = logging.getLogger(__name__)

_SANITY_API = "https://8navd656.api.sanity.io/v2023-01-01/data/query/production-be"
# Where the files Sanity names sit: every card in the archive and every one
# the API named on 2026-11-06 was on Sanity's file host.
_CARD_HOSTS = frozenset({"cdn.sanity.io"})


# The GROQ predicate every lookup of a product's cards filters on. Frank
# dropped the "Elektriciteit" token from the September 2026 filenames ("...
# Tariefkaart Elektriciteit Dynamisch HV Augustus 2026" became "...
# Tariefkaart Dynamisch HV September 2026", the variable cards of September
# and April 2026 alike), which left a filter keyed on it matching nothing
# newer than August and every tier quietly serving the previous month's card.
# Matching on the product word alone is what survives that rename; gas is
# excluded explicitly rather than relied on to keep its own naming, since the
# same release renamed "Gas ZTP" to "Gas", and the gas cards of 2023 were
# named "Gas Variabel" or "Gas variabel".
def _card_select(product: str) -> str:
    return (
        '*[_type=="sanity.fileAsset"'
        f' && originalFilename match "*{product}*"'
        ' && !(originalFilename match "*Gas*")'
    )


# Frank prints the month title-cased ("Januari"); keep a title tuple for
# indexing and the header regex, plus a lowercase set for membership.
_NL_MONTHS = NL_MONTHS
_NL_MONTHS_TITLE = tuple(m.capitalize() for m in NL_MONTHS)
_NL_MONTHS_LOWER: frozenset[str] = frozenset(NL_MONTHS)

# (contract_id, label, sanity filename suffix after "Dynamisch")
_TIERS: tuple[tuple[str, str, str | None], ...] = (
    ("frank_dynamic", "Frank Energie Dynamisch", None),
    ("frank_dynamic_hv", "Frank Energie Dynamisch HV", "HV"),
    # The id keeps the tier's old name so existing entries stay on it; the card
    # has been titled "VT" since October 2026.
    ("frank_dynamic_korting", "Frank Energie Dynamisch VT (Korting)", "VT"),
    ("frank_dynamic_jn", "Frank Energie Dynamisch JN", "JN"),
    ("frank_dynamic_slim", "Frank Energie Dynamisch Slim", "SL"),
    ("frank_dynamic_plus", "Frank Energie Dynamisch Plus", "Plus"),
)
# The variable tiers, by the suffix their file names carry after "Variabel".
# VT's cards were once titled "Korting" too ("Frank Energie Variabel -
# Korting - juni 2025", "VT" by October 2026), but no entry predates this
# tier, so its id takes the name its file names have always carried.
# "Variabel JN Pro" is the business card.
_VARIABLE_TIERS: tuple[tuple[str, str, str | None], ...] = (
    ("frank_variable", "Frank Energie Variabel", None),
    ("frank_variable_hv", "Frank Energie Variabel HV", "HV"),
    ("frank_variable_vt", "Frank Energie Variabel VT", "VT"),
    ("frank_variable_jn", "Frank Energie Variabel JN", "JN"),
    ("frank_variable_slim", "Frank Energie Variabel Slim", "SL"),
    ("frank_variable_plus", "Frank Energie Variabel Plus", "Plus"),
)
# The first day Frank no longer sold a tier (``Contract.withdrawn``). VT had
# no November 2026 card while the five others had theirs on 30 October, and
# Frank's conditions page, which still linked VT's October card on 3 November,
# lists the five others alone from 4 November. Its variable twin went the same
# way: no November card, no link on the page.
_WITHDRAWN: dict[str, date] = {
    "frank_dynamic_korting": date(2026, 11, 1),
    "frank_variable_vt": date(2026, 11, 1),
}
_TIER_SUFFIX: dict[str, str | None] = {t[0]: t[2] for t in (*_TIERS, *_VARIABLE_TIERS)}
# The word a tier's file names and terms-page link carry before its suffix.
_PRODUCT: dict[str, str] = {
    **{t[0]: "Dynamisch" for t in _TIERS},
    **{t[0]: "Variabel" for t in _VARIABLE_TIERS},
}
_VALID_IDS: frozenset[str] = frozenset(_TIER_SUFFIX)

# discover() reads the words the terms page links each tier's card under:
# the product, then the file names' suffix, none for the standard tier, and
# "Slim" where the files say "SL".
_PAGE_TIER_IDS: dict[tuple[str, str], str] = {
    **{
        (_PRODUCT[cid].casefold(), (suffix or "").casefold()): cid
        for cid, _label, suffix in (*_TIERS, *_VARIABLE_TIERS)
    },
    ("dynamisch", "slim"): "frank_dynamic_slim",
    ("variabel", "slim"): "frank_variable_slim",
}

# Frank alternates the Slim tier's filename token between the abbreviation
# "SL" and the full word "Slim" from one month to the next (both are live in
# the CMS), so treat them as aliases when matching a card to its tier. JN was
# named June until May 2025: its cards of February and May 2025 are titled
# "June" and print JN's formula and feed-in in both products, May's the very
# terms of JN's first card in June.
_SUFFIX_ALIASES: dict[str, tuple[str, ...]] = {
    "SL": ("SL", "Slim"),
    "JN": ("JN", "June"),
}
# Uploads a later one replaced under a name no month lookup finds. The
# variable June card first uploaded for February 2025 printed the standard
# tier's formula and 2,08 a month; the one Frank put up on 6 February in its
# place, filed as "... Variabel June 2025.pdf", prints JN's formula, 2,75 a
# month and the 35 EUR cashback, as the dynamic card re-uploaded that day
# does. Left out, that month has no card and falls back as any month
# without one does, rather than bill the terms Frank withdrew.
_SUPERSEDED: frozenset[str] = frozenset(
    {"Frank Energie Tariefkaart Elektriciteit Variabel June Februari 2025.pdf"}
)

_FLUVIUS_LABELS: dict[str, str] = {
    "Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Imewo": DSO_FLUVIUS_IMEWO,
    "Kempen": DSO_FLUVIUS_IVEKA,
    "Limburg": DSO_FLUVIUS_LIMBURG,
    "Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "West": DSO_FLUVIUS_WEST,
    "Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}
# The January 2026 card printed the last area as "Zenne-Dijke", its figures
# in place under the misprint. One letter is a card to read, not to refuse.
_LABEL_PATTERNS: dict[str, str] = {"Zenne-Dijle": r"Zenne[\s\-]*Dij[lk]e"}

_FRANK_REGIONS = frozenset({REGION_FLANDERS})


# ---- Sanity CMS API helpers --------------------------------------------------


def _matches_suffix(
    filename: str, suffix: str | None, product: str = "Dynamisch"
) -> bool:
    """True when *filename* belongs to the tier identified by *suffix* of
    *product*.

    A tier's token is followed by the month, "Juli.pdf" and "Oktober'24"
    included. Another product sharing the token is not the tier's: "Dynamisch
    JN Pro November 2026", the business card, was uploaded after the
    household JN card of that month and priced JN entries on its figures,
    as "Dynamisch VT Slim" did VT entries in December 2024 and March 2025.
    "Variabel JN Pro" came out beside the household card the same way.
    """
    m = re.search(rf"{product}\s+(\S+)(?:\s+([A-Za-z]+))?", filename)
    if not m:
        return False
    word, after = m.group(1), m.group(2) or ""
    if suffix is None:
        return word.lower() in _NL_MONTHS_LOWER
    return (
        word in _SUFFIX_ALIASES.get(suffix, (suffix,))
        and after.lower() in _NL_MONTHS_LOWER
    )


async def _sanity_query(
    session: aiohttp.ClientSession,
    query: str,
) -> list[dict[str, str]]:
    body = await fetch_text(session, _SANITY_API, params={"query": query}, timeout=15)
    try:
        result = json.loads(body).get("result", [])
        if isinstance(result, dict):
            return [result]
        return list(result)
    except (json.JSONDecodeError, AttributeError, TypeError) as err:
        raise ExtractorError(f"Sanity API response parse error: {err}") from err


async def _resolve_pdf_url(
    session: aiohttp.ClientSession,
    contract_id: str,
    target_month: date | None = None,
) -> tuple[str, str]:
    """Return (pdf_url, publication_label) for the requested tier and month.

    When *target_month* is None the latest available card is returned.
    """
    suffix = _TIER_SUFFIX.get(contract_id)
    if suffix is None and contract_id not in _VALID_IDS:
        raise ExtractorError(f"unknown Frank Energie contract {contract_id!r}")
    product = _PRODUCT[contract_id]

    if target_month is not None:
        month_name = _NL_MONTHS_TITLE[target_month.month - 1]
        q = (
            _card_select(product)
            + f' && originalFilename match "*{month_name}*"'
            + f' && originalFilename match "*{target_month.year}*"'
            + "]{originalFilename,url,_createdAt}"
        )
    else:
        q = (
            _card_select(product)
            + "]{originalFilename,url,_createdAt}"
            + " | order(_createdAt desc)[0..29]"
        )

    rows = await _sanity_query(session, q)
    matches = [
        r
        for r in rows
        if _matches_suffix(r.get("originalFilename", ""), suffix, product)
        and r.get("originalFilename") not in _SUPERSEDED
    ]
    if not matches:
        raise ExtractorError(
            f"no Frank Energie tariff card found for {contract_id}"
            + (f" ({target_month})" if target_month else "")
        )
    matches.sort(key=lambda r: r.get("_createdAt", ""), reverse=True)
    best = matches[0]
    url = best.get("url")
    if not isinstance(url, str) or on_hosts(url, _CARD_HOSTS) is None:
        raise ExtractorError(
            f"Frank Energie tariff card for {contract_id} is not on Sanity's file host"
        )
    fname = best.get("originalFilename", "")
    m = re.search(
        r"(" + "|".join(re.escape(n) for n in _NL_MONTHS_TITLE) + r")\s+(\d{4})",
        fname,
    )
    label = f"{m.group(1).lower()} {m.group(2)}" if m else ""
    return url, label


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id not in _VALID_IDS:
        raise ExtractorError(f"unknown Frank Energie contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("Frank Energie only operates in Flanders")
    pdf_url, label = await _resolve_pdf_url(session, contract_id)
    text = await fetch_pdf_text_layout(session, pdf_url)
    snap = await _with_waived_fee(
        session, contract_id, text, parse_snapshot(text, pdf_url, contract_id, label)
    )
    # Here and not in the reader: a past month's card keeps a short table.
    require_every_area("Frank Energie", snap, _FLUVIUS_LABELS.values())
    return snap


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    if contract_id not in _VALID_IDS:
        return None
    try:
        pdf_url, label = await _resolve_pdf_url(
            session, contract_id, target_month=year_month
        )
        text = await fetch_pdf_text_layout(session, pdf_url)
        snap = await _with_waived_fee(
            session,
            contract_id,
            text,
            parse_snapshot(text, pdf_url, contract_id, label),
        )
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    if running_month_short_of_an_area(snap.dsos, _FLUVIUS_LABELS.values(), year_month):
        return None
    return archive_validity_check(snap, text, year_month, month_names=_NL_MONTHS)


# "Gedurende de eerste 12 maanden van het contract ontvang je een maandelijkse
# korting op de abonnementskosten, waardoor deze worden verlaagd naar € 0,00
# per maand. Na afloop van deze periode gelden de abonnementskosten volgens de
# tariefkaart Dynamisch - JN - Oktober 2026", under the JN card's "0,00*". The
# variable JN card of the same month names "Variabel - JN - Oktober 2026".
_FEE_WAIVER_RE = re.compile(
    r"eerste\s+(\d+)\s+maanden\s+van\s+het\s+contract\s+ontvang\s+je\s+een\s+"
    r"maandelijkse\s+korting\s+op\s+de\s+abonnementskosten",
    re.IGNORECASE,
)
_FEE_CARD_RE = re.compile(
    r"abonnementskosten\s+volgens\s+de\s+tariefkaart\s+(Dynamisch|Variabel)\s*-\s*"
    r"\S+\s*-\s*([A-Za-z]+)\s+(\d{4})",
    re.IGNORECASE,
)

# The yearly subscription of the cards a waiver names, by (contract, month):
# a published card's subscription does not change, and reading one costs a
# whole card's layout, about 25 s of CPU on a Raspberry Pi.
_NAMED_FEES: dict[tuple[str, date], float] = {}


async def _with_waived_fee(
    session: aiohttp.ClientSession,
    contract_id: str,
    text: str,
    snap: SupplierSnapshot,
) -> SupplierSnapshot:
    """``snap`` with the subscription its card waives for a contract's first
    year: billed at the one the card it names prints, and granted back to a
    contract signed on it as a first-year credit of the same amount, pro rata
    over the year. A card without a waiver is returned as it is; one waiving
    for another term than a year, or naming no card, or another product's,
    fails loud, since a credit over the first year would misprice it."""
    waiver = _FEE_WAIVER_RE.search(text)
    if waiver is None:
        return snap
    named = _FEE_CARD_RE.search(text)
    month_name = "" if named is None else named.group(2).lower()
    if (
        named is None
        or month_name not in _NL_MONTHS_LOWER
        or waiver.group(1) != "12"
        or named.group(1).casefold() != _PRODUCT[contract_id].casefold()
    ):
        raise ExtractorError(
            "Frank Energie: the card waives its subscription in a way not priced here"
        )
    month = date(int(named.group(3)), _NL_MONTHS.index(month_name) + 1, 1)
    yearly = _NAMED_FEES.get((contract_id, month))
    if yearly is None:
        url, _ = await _resolve_pdf_url(session, contract_id, target_month=month)
        fee = _MONTHLY_FEE_RE.search(await fetch_pdf_text_layout(session, url))
        if fee is None:
            raise ExtractorError(
                f"Frank Energie: the {month:%m/%Y} card a waiver names prints no subscription"
            )
        yearly = to_float(fee.group(1)) * 12.0
        _NAMED_FEES[(contract_id, month)] = yearly
    return replace(
        snap,
        energy=replace(snap.energy, yearly_fixed_fee=yearly),
        welcome_credit_eur=yearly,
        welcome_credit_kind=WELCOME_CREDIT_PRO_RATA,
    )


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    if contract_id not in _VALID_IDS:
        return None
    q = (
        _card_select(_PRODUCT[contract_id])
        + "] | order(_createdAt desc)[0]{_createdAt}"
    )
    try:
        rows = await _sanity_query(session, q)
    except ExtractorError:
        return None
    if rows and rows[0]:
        return str(rows[0].get("_createdAt", ""))
    return None


_TERMS_URL = "https://www.frankenergie.be/nl/voorwaarden"
# The terms page links the card of every product Frank sells today, the
# electricity tiers as "Actuele Tariefkaart Stroom Dynamisch" or "... Stroom
# Variabel" followed by the tier's word but for the standard tier. On 5
# November 2026 they were the standard, HV, JN, Slim and Plus tiers of each:
# VT's links left the page with the tier. A link's text stands in the HTML,
# ended by "<", and in the page's script payload, ended by an escaped quote.
_CATALOG_RE = re.compile(
    r"Actuele Tariefkaart Stroom (Dynamisch|Variabel)\b([^<\\\"]*)"
)
_CATALOG_PREFIX = {"dynamisch": "frank_dynamic_", "variabel": "frank_variable_"}


def parse_catalog(page: str) -> set[str]:
    """The electricity tiers the terms page links a card for, by contract
    id; a tier no contract is reads as ``frank_dynamic_<words>`` or
    ``frank_variable_<words>``."""
    out: set[str] = set()
    for match in _CATALOG_RE.finditer(page):
        product = match.group(1).casefold()
        words = re.findall(r"\w+", match.group(2).casefold())
        out.add(
            _PAGE_TIER_IDS.get(
                (product, " ".join(words)), _CATALOG_PREFIX[product] + "_".join(words)
            )
        )
    return out


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The electricity tiers Frank sells today, as its terms page links them.

    The 60 newest uploads this read before still held VT's October card
    days after the tier was withdrawn on 1 November 2026, so VT read as sold
    and would have read as gone once that card left them. The page dropped
    VT's link on 4 November. An empty set when the page does not read.
    """
    try:
        page = await fetch_text(session, _TERMS_URL)
    except ExtractorError:
        return set()
    return parse_catalog(page)


# ---- snapshot parser ---------------------------------------------------------


# "De tarieven (incl. 6% btw, tenzij anders vermeld)".
_VAT_RE = re.compile(r"incl\.?\s*(\d+)\s*%\s*btw", re.IGNORECASE)


def parse_snapshot(
    text: str,
    source_url: str,
    contract_id: str,
    publication_label: str = "",
) -> SupplierSnapshot:
    energy: DynamicRates | SpotMonthlyRates
    if _PRODUCT.get(contract_id) == "Variabel":
        energy = variable_energy(text, _yearly_fee(text))
        # Credited per hour up to February 2025, in the dynamic cards' words.
        injection = variable_injection(text) or _extract_injection(text)
    else:
        energy = _extract_dynamic(text)
        injection = _extract_injection(text)
    return with_vat_basis(
        SupplierSnapshot(
            supplier="frank",
            contract=contract_id,
            energy=energy,
            dsos=_extract_dsos(text),
            taxes=_extract_taxes(text),
            source_url=source_url,
            publication_label=publication_label,
            valid_until=_valid_until(text),
            injection=injection,
            welcome_credit_eur=_welcome_credit(text),
            welcome_credit_kind=WELCOME_CREDIT_ANNIVERSARY,
        ),
        printed_vat_rate(text, _VAT_RE),
        # Grossed by the multiplier its formula prints, not by this rate.
        grossed=False,
    )


def _valid_until(text: str) -> date | None:
    """The last day of the month the card's title names.

    Every card opens on a title ending "<maand> <jaar>", and its file name in
    the CMS and its expected-price lines name the same month. So does the
    validity sentence under the title, on every card but four: the HV, JN and
    Slim cards for February 2026 say their formula is for contracts signed in
    January, and the JN card for September 2026 says August, each dating the
    card a month early. The title is read first, and the sentence only when
    the title names no month.
    """
    title = text.lstrip().partition("\n")[0]
    in_title = scan_month_end(title, _MONTH_NAMES, limit=len(title))
    return in_title or parse_valid_until(text)


# ---- energy ------------------------------------------------------------------

# Accept both decimal separators: to_float normalizes either, and the
# sibling extractors (luminus, eneco) already do. A dot-decimal re-render
# of the card would otherwise truncate values to the integer part - a
# mandatory tax row silently dropping to 0, or the VAT multiplier 1,06
# collapsing to 1 - instead of failing loud.
_NUM = NUM_NO_THOUSANDS

# "Korting 120 EUR (incl. btw)". Until September 2026 three of the five tiers
# granted one and they did not agree on the amount: 120 on Korting, 115 on HV,
# 35 on JN, and nothing on the standard and Slim tiers. From October only JN
# does. The "(incl. btw)" suffix is part of the anchor because "Korting" on its
# own was also the Korting tier's TITLE ("Frank Energie Dynamisch - Korting -
# juni 2026"), and the two sentences under the amount open with it as well:
# four occurrences, one figure.
#
# A cohort signed on one of those cards keeps its credit, read off its own
# month's card. It was the whole reason the Korting tier existed: its formula
# and its subscription were both worse than JN's, so without the credit the
# ranking called it the cheaper tier's loser while in year one it was about 93
# EUR better.
_WELCOME_RE = re.compile(
    rf"Korting\s+{_NUM}\s*EUR\s*\(\s*incl\.?\s*btw\s*\)", re.IGNORECASE
)


def _welcome_credit(text: str) -> float | None:
    """The cashback in EUR, or ``None`` on a card that prints none.

    Optional: since October 2026 only JN grants one, so a missing row is the
    ordinary case rather than a layout drift.

    Every card that grants one makes it conditional, and the three tiers do not
    attach the same conditions. All of them require a year of uninterrupted
    supply ("na een jaar ononderbroken verbruik op dit contract"), which is the
    one condition the entry itself stands in for: a household that left stops
    being priced on this card at all.

    HV and JN attach three more that nothing here can check:

      - "op voorwaarde dat je jouw facturen tijdens de volledige duur van jouw
        contract steeds op tijd hebt betaald" - every invoice paid on time,
        which is about the household rather than the tariff;
      - "Deze korting is niet cumuleerbaar met andere promoties of kortingen" -
        only one credit is ever applied per contract here, so this holds within
        the integration, but a promotion taken outside it is invisible;
      - "Op een sociaal tarief wordt door ons geen korting toegepast" - a
        household on the social tariff is priced by the social tariff
        supplier, never on this card, so an entry here is not one.

    The Korting tier attaches none of those three: it says only that the credit
    goes to new customers and to existing ones switching to it.

    All four are assumed to hold, which is the same ordinary case every other
    leg of the bill assumes, and the assumption is disclosed in the README and
    the provider doc rather than left implied. A household that fails one sees
    a credit it will not be granted, which is still nearer than ranking a tier
    as though its whole reason for existing were not there.
    """
    m = _WELCOME_RE.search(text)
    return None if m is None else to_float(m.group(1))


_FORMULA_RE = re.compile(
    rf"\({_NUM}\s*x\s*BELPEX\s*per\s*uur\*?\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\)\s*x\s*{_NUM}",
    re.IGNORECASE,
)

_MONTHLY_FEE_RE = re.compile(
    r"Abonnementskost\s*\(EUR/maand\)\s*" + _NUM,
)


def _extract_dynamic(text: str) -> DynamicRates:
    formula = _FORMULA_RE.search(text)
    if not formula:
        raise ExtractorError("could not parse Frank Energie energy formula")
    factor_pdf = to_float(formula.group(1))
    sign = parse_sign(formula.group(2))
    base_pre_vat_cents = sign * to_float(formula.group(3))
    vat_mult = to_float(formula.group(4))

    # PDF formula: (factor_pdf * BELPEX_EUR_MWh + base_cents) * vat_mult
    # in EURct/kWh.  ENTSO-E returns spot in EUR/kWh = EUR/MWh / 1000.
    # => factor = factor_pdf * vat_mult * 1000 / 100 = factor_pdf * vat_mult * 10
    # => base   = base_cents * vat_mult / 100
    factor = factor_pdf * vat_mult * 10.0
    base = base_pre_vat_cents * vat_mult / 100.0

    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=_yearly_fee(text),
    )


def _yearly_fee(text: str) -> float:
    """The subscription a card prints per month, by the year."""
    fee_match = _MONTHLY_FEE_RE.search(text)
    if fee_match is None:
        # The monthly standing charge (~35 EUR/yr) is mandatory; the
        # adjacent tax block already fails loud, so do the same here
        # rather than silently bill a zero standing charge on drift.
        raise ExtractorError("Frank Energie: monthly fixed fee row not found")
    return to_float(fee_match.group(1)) * 12.0


# ---- injection ---------------------------------------------------------------

_INJECTION_RE = re.compile(
    r"[Tt]e?rugleveringsvergoeding[:\s]*"
    rf"\({_NUM}\s*x\s*BELPEX\s*per\s*uur\*?\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\)",
    re.IGNORECASE,
)


def _extract_injection(text: str) -> InjectionRates:
    m = _INJECTION_RE.search(text)
    if not m:
        # Every Frank dynamic card prints a terugleveringsvergoeding
        # formula; a miss is a layout drift, not a fee-free contract.
        # Raise like the monthly-fee and GSC/WKK rows rather than
        # silently crediting a solar user 0 EUR/kWh.
        raise ExtractorError("Frank Energie: injection formula row not found")
    factor_pdf = to_float(m.group(1))
    # The sign between BELPEX and the base is mandatory in the regex
    # (matching the energy formula), so a sign-less or reworded formula
    # misses and raises above rather than silently defaulting to minus.
    sign = parse_sign(m.group(2))
    base_cents = sign * to_float(m.group(3))
    # Injection is VAT-exempt: no vat_mult scaling.
    # factor_pdf * BELPEX_EUR_MWh in EURct/kWh => factor = factor_pdf * 10
    # base_cents in EURct/kWh => base = base_cents / 100
    return InjectionRates(
        factor=factor_pdf * 10.0,
        base=base_cents / 100.0,
        formula=m.group(0),
    )


# ---- taxes -------------------------------------------------------------------

# The footnote stars of the excise and fund rows, with the blanks after them,
# are one optional piece: as runs of blanks either side of optional stars, a
# run of blanks was split between the two in every way before the figure
# failed.
_EXCISE_RE = re.compile(
    r"Bijzondere\s+accijns\s+op\s+Energie\s*\(EURct/kWh\)\s*(?:\*{1,2}\s*)?" + _NUM
)
_ENERGY_CONTRIB_RE = re.compile(r"Bijdrage\s+op\s+Energie\s*\(EURct/kWh\)\s*" + _NUM)
_GSC_RE = re.compile(r"GSC\s*\(EURct/kWh\)\s*" + _NUM)
_WKK_RE = re.compile(r"WKK\s*\(EURct/kWh\)\s*" + _NUM)
_FUND_RE = re.compile(
    r"Bijdrage\s+Energiefonds\s+Residentieel\s*\(EUR/maand\)\s*(?:\*\s*)?" + _NUM
)


def _extract_taxes(text: str) -> TaxOverlay:
    """All values on the card are VAT-inclusive (6% BTW)."""
    return regional_tax_overlay(
        text,
        supplier="Frank Energie",
        region=REGION_FLANDERS,
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_ENERGY_CONTRIB_RE,
        fund=_FUND_RE,
    )


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital-meter network table.

    An area whose row does not read is left out. ``fetch`` refuses a live
    card short of an area and ``fetch_for_month`` the running month's
    (``require_every_area``), while a past month's card keeps its energy and
    the month cache bills the missing area on the nearest card of the same
    calendar year that prints it. A table nothing could be read from raises.
    """
    section_start = text.find("Digitale meter")
    section_end = text.find("Klassieke meter")
    if section_start < 0:
        raise ExtractorError("could not locate Frank Energie DSO table")
    section = text[section_start : section_end if section_end > section_start else None]

    # Read off the WHOLE card, not the digital-meter section: the footnote
    # stating it sits below the table.
    ceiling = parse_vreg_network_ceiling(text)
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLUVIUS_LABELS.items():
        escaped = _LABEL_PATTERNS.get(label) or re.escape(label).replace(
            r"\-", r"[\s\-]*"
        )
        row = re.search(
            rf"Fluvius\s*[\[\(]\s*{escaped}\s*[\]\)]{LINE_BREAK}"
            rf"{_NUM}{LINE_BREAK}{_NUM}{LINE_BREAK}{_NUM}{LINE_BREAK}{_NUM}",
            section,
            re.IGNORECASE,
        )
        if not row:
            continue
        databeheer = to_float(row.group(1))
        capacity = to_float(row.group(2))
        normal = to_float(row.group(3)) / 100.0
        excl_night = to_float(row.group(4)) / 100.0
        out[key] = DsoOverlay(
            distribution_single=normal,
            distribution_exclusive_night=excl_night,
            transport=0.0,
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
            # One VREG figure for the whole region, stated once in a footnote
            # rather than per area, so every Fluvius overlay carries it.
            network_ceiling_eur_per_kwh=ceiling,
        )
    if not out:
        raise ExtractorError("Frank Energie: no DSO row read")
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=28.4,
    id="frank",
    label="Frank Energie",
    contracts=(
        *(
            Contract(
                id=cid,
                label=clabel,
                kind="dynamic",
                regions=_FRANK_REGIONS,
                # The asterisk on both printed formulas points at the
                # uur- / kwartierprijzen footnote (module docstring): the
                # customer picks the grid in Frank's app, so the entry
                # answers for the card. Set on every tier because every
                # tier prints it.
                quarter_hourly_option=True,
                withdrawn=_WITHDRAWN.get(cid),
            )
            for cid, clabel, _ in _TIERS
        ),
        # "spot_monthly", as energie.be's Variabel: the card prices the month
        # on an index it names and prints only an estimate of it, and the
        # kind is what collects the ENTSO-E key the index needs.
        *(
            Contract(
                id=cid,
                label=clabel,
                kind="spot_monthly",
                regions=_FRANK_REGIONS,
                withdrawn=_WITHDRAWN.get(cid),
            )
            for cid, clabel, _ in _VARIABLE_TIERS
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
