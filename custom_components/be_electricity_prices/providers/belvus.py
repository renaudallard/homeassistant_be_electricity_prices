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

"""Belvus tariff card extractor.

Belvus sells three residential electricity products in Flanders, each with
one card per month at a path named by the price month:

    https://www.belvus.be/public/tariefkaarten/<YYYY-MM>/Tariefkaart_<Product>_EL.pdf

/diensten links every product's current card and /historische-tariefkaarten
the months before, each link carrying a "?v=<hash>" that changes when a card
is uploaded again. The cards are served as immutable for a year, so the card
is fetched at the link the pages carry, and the link is the probe's key. The
"Pro" twins are professional cards and are not read.

- Belvus Dynamisch, for a digital meter read per quarter hour (SMR3), since
  September 2026: ``(Belpex_15M x 1,01) + 7,50 EUR/MWh`` on each quarter's
  day-ahead price, the feed-in ``(Belpex_15M x 1) - 13 EUR/MWh``.
- Flex Online, ``(Belpex_RLP x 1,21) + 4 EUR/MWh``, and Smart Plus,
  ``(Belpex_RLP x 1,1) + 19 EUR/MWh``, on the delivery month's RLP-weighted
  mean, the feed-in ``(Belpex_SPP x 1) - 20 EUR/MWh`` on its SPP-weighted one.

Every formula is printed before VAT in EUR/MWh, beside the price it gives at
the month before's RLP-M or SPP-M, which the card names; each formula is held
to that price. The cards state no VAT rate, so the energy formulas are
grossed by the residential one. Every other figure is printed including VAT.

Each card names the RLP-M and SPP-M the month before it settled at, so a
closed month of Flex Online and Smart Plus is billed at the values the next
card names for it. Over January to October 2026 Belvus's RLP-M ran about
0,2 EUR/MWh above the integration's column-weighted mean, the closest of the
blends, and its SPP-M is the series EBEM, Elegant and Trevion publish.

Up to August 2026 the cards were a copy of the Energy Together template, in
another layout, read by ``_belvus_older.py``; the ones of January to April
print Fluvius's 2025 network table. Smart Plus's formula was
``(Belpex_RLP x 1,037) + 12,21 EUR/MWh`` in January and February 2026, and a
contract keeps the formula of its card for its year. The tax block of the
November 2026 cards still prints the excise and the energy contribution of
before August 2026; both levies are billed from the law.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

import aiohttp

from ..const import FLUVIUS_CARD_LABELS, REGION_FLANDERS, VAT_RATE_REDUCED
from ._belvus_older import (
    Card,
    Leg,
    held_to_price,
    is_older,
    named_index,
)
from ._belvus_older import blocks as older_blocks
from ._belvus_older import read as read_older
from ._parse import (
    SIGN_CHARS,
    numeric_row,
    parse_sign,
    regional_tax_overlay,
    require_contract,
    require_every_area,
    running_month_short_of_an_area,
    to_float,
)
from ._pdf import NL_MONTHS, fetch_pdf_text, fetch_text, is_transient_fetch_error
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    InjectionRates,
    TariffKind,
    VariableRates,
)
from ._settle import settled_energy, settled_injection
from ._validity import archive_validity_check, end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    with_vat_basis,
)

_SUPPLIER = "belvus"
_LABEL = "Belvus"
_HOST = "https://www.belvus.be"
_LISTING_URL = f"{_HOST}/historische-tariefkaarten"
# Every product's current card. On 3 November 2026 it linked the November
# cards while the history page still ended with October.
_CURRENT_URL = f"{_HOST}/diensten"


@dataclass(frozen=True)
class _Product:
    contract_id: str
    label: str
    # <Product> in the file name, which the card's header spells with spaces.
    file: str
    kind: TariffKind
    # The index each leg's formula names, energy then feed-in.
    indices: tuple[str, str]


_PRODUCTS = (
    _Product(
        "belvus_dynamisch",
        "Belvus Dynamisch",
        "Dynamisch",
        "dynamic",
        ("Belpex_15M", "Belpex_15M"),
    ),
    _Product(
        "belvus_flex_online",
        "Belvus Flex Online",
        "FlexOnline",
        "variable",
        ("Belpex_RLP", "Belpex_SPP"),
    ),
    _Product(
        "belvus_smart_plus",
        "Belvus Smart Plus",
        "SmartPlus",
        "variable",
        ("Belpex_RLP", "Belpex_SPP"),
    ),
)
_BY_ID = {p.contract_id: p for p in _PRODUCTS}
_BY_FILE = {p.file: p.contract_id for p in _PRODUCTS}

_NUM = r"\d+(?:,\d+)?"
# A card link as both pages print it. Only the path and the hash are taken,
# so a card is always fetched from Belvus's own host.
_LINK_RE = re.compile(
    r"/public/tariefkaarten/(\d{4})-(\d{2})/Tariefkaart_([A-Za-z]+)_EL\.pdf"
    r"(\?v=[0-9a-f]{1,32})?"
)
# The header of every page after the first: "Tariefkaart", a middle dot,
# "Particulier Elektriciteit Belvus Dynamisch", an em dash and "November
# 2026". The product is spelt "Dynamisch", "Belvus Dynamisch", "FlexOnline",
# "FlexOnline EL" and "Flex Online EL" on the September to November 2026
# cards.
_HEADER_RE = re.compile(
    r"^Tariefkaart \u00b7 (Particulier|Professioneel) Elektriciteit "
    r"(.+?) \u2014 ([a-z]+) (20\d{2})[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# "aan residentiële klanten met analoge of digitale meter (SMR1) in
# Vlaanderen", the sentence bounded so a page with no full stop is not read
# to its end again from every "klanten".
_REGION_RE = re.compile(r"residentiële\s+klanten\b[^.]{0,200}?\bin\s+Vlaanderen")
# "(Belpex_RLP × 1,21) + € 4/MWh" and "(Belpex_15M × 1) + € -13/MWh". The
# blanks after the euro sign are one run where the base has no sign.
_FORMULA_RE = re.compile(
    rf"\(\s*(Belpex_[A-Za-z0-9]+)\s*[×x*]\s*({_NUM})\s*\)\s*([{SIGN_CHARS}])"
    rf"\s*€\s*(?:([{SIGN_CHARS}])\s*)?({_NUM})\s*/MWh"
)
# The price the formula gives at last month's index, on its own line above
# "**".
_PRINTED_RE = re.compile(
    rf"^([{SIGN_CHARS}]?)(\d+,\d+)[ \t]*\n\*\*[ \t]*$", re.MULTILINE
)
# Where the energy block starts, and where the feed-in block starts and ends.
_ENERGY_START = "Tarieven excl. btw"
_FEED_IN_START = "Teruglevering (injectie"
_PAGE_END = "Belvus BV \u00b7"
# "Jaarlijkse abonnementskost *** (incl. btw) € 8/jaar". The blanks around
# the asterisks are one run where there are none, never split two ways.
_FEE_RE = re.compile(
    r"Jaarlijkse abonnementskost[ \t]*(?:\*+[ \t]*)?\(incl\. btw\)[ \t]*€[ \t]*"
    rf"({_NUM})[ \t]*/jaar"
)
_LEVIES_HEAD = "Kosten groene stroom en WKK incl. btw"
_GSC_RE = re.compile(r"^GSC\b[^\n€]*€c[ \t]*(\d+,\d+)[ \t]*/kWh", re.MULTILINE)
_WKK_RE = re.compile(r"^WKK\b[^\n€]*€c[ \t]*(\d+,\d+)[ \t]*/kWh", re.MULTILINE)
_EXCISE_RE = re.compile(
    r"^Bijzondere accijns[ \t]*(?:\*+[ \t]*)?€c[ \t]*(\d+,\d+)[ \t]*/kWh",
    re.MULTILINE,
)
_CONTRIBUTION_RE = re.compile(
    r"^Bijdrage op de energie[ \t]*€c[ \t]*(\d+,\d+)[ \t]*/kWh", re.MULTILINE
)
# The household row; the row above it is the non-residential one.
_FUND_RE = re.compile(
    rf"^Bijzondere Energiefonds[^\n]*?laagspanning residentieel[ \t]*€[ \t]*({_NUM})[ \t]*$",
    re.MULTILINE,
)
_DIGITAL_HEAD = "Nettarieven \u00b7 Digitale meter met piekmeting"
_CLASSIC_HEAD = "Nettarieven \u00b7 Klassieke meter incl. btw"
_CLASSIC_END = "Energiedelen"
# "databeheertarief ¹: € 18,92/jaar" in the classic table's heading, the one
# figure for every meter, the gap to the colon bounded.
_DATA_RE = re.compile(
    rf"databeheertarief[^\n€:]{{0,20}}:[ \t]*€[ \t]*({_NUM})[ \t]*/jaar"
)


def _product(contract_id: str, region: str) -> _Product:
    product = require_contract(_BY_ID, contract_id, _LABEL)
    if region != REGION_FLANDERS:
        raise ExtractorError(f"{_LABEL} {contract_id}: not sold in region {region!r}")
    return product


def _card_url(product: _Product, month: date) -> str:
    return (
        f"{_HOST}/public/tariefkaarten/{month.year}-{month.month:02d}/"
        f"Tariefkaart_{product.file}_EL.pdf"
    )


def listed_cards(page: str) -> dict[str, dict[date, str]]:
    """Every card a page links, by product file name and month, each at the
    link it carries. A folder whose name is no month is skipped."""
    out: dict[str, dict[date, str]] = {}
    for match in _LINK_RE.finditer(page):
        year, month, name, _version = match.groups()
        try:
            day = date(int(year), int(month), 1)
        except ValueError:
            continue
        out.setdefault(name, {})[day] = f"{_HOST}{match.group(0)}"
    return out


async def _listed(session: aiohttp.ClientSession) -> dict[str, dict[date, str]]:
    """The cards both pages link, the current cards page winning a month
    both list. A current cards page that cannot be read leaves the history
    page's."""
    cards = listed_cards(await fetch_text(session, _LISTING_URL))
    try:
        current = listed_cards(await fetch_text(session, _CURRENT_URL))
    except ExtractorError:
        current = {}
    for name, months in current.items():
        cards.setdefault(name, {}).update(months)
    return cards


def _newest(cards: dict[str, dict[date, str]], product: _Product) -> str:
    months = cards.get(product.file)
    if not months:
        raise ExtractorError(f"{_LABEL}: no {product.label} card listed")
    return months[max(months)]


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """The newest card either page links for ``contract_id``."""
    product = _product(contract_id, region)
    url = _newest(await _listed(session), product)
    snap = parse_snapshot(contract_id, await fetch_pdf_text(session, url), url)
    # Here and not in the reader: a past month's card keeps a short table.
    require_every_area(_LABEL, snap, FLUVIUS_CARD_LABELS.values())
    return snap


async def _read(
    session: aiohttp.ClientSession,
    product: _Product,
    cards: dict[str, dict[date, str]],
    month: date,
) -> tuple[SupplierSnapshot, str] | None:
    """The month's card at the link a page carries, else at its path, or
    None where the card is not the month's."""
    url = cards.get(product.file, {}).get(month) or _card_url(product, month)
    text = await fetch_pdf_text(session, url)
    snap = archive_validity_check(
        parse_snapshot(product.contract_id, text, url), text, month
    )
    return None if snap is None else (snap, text)


async def fetch_for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, year_month: date
) -> SupplierSnapshot | None:
    """The card in the month's folder, a closed month settled on the next
    card, or None."""
    product = _BY_ID.get(contract_id)
    if product is None or region != REGION_FLANDERS:
        return None
    month = year_month.replace(day=1)
    try:
        cards = await _listed(session)
        found = await _read(session, product, cards, month)
        if found is None or running_month_short_of_an_area(
            found[0].dsos, FLUVIUS_CARD_LABELS.values(), month
        ):
            return None
        return await _settle_on_next_card(session, product, cards, found[0], month)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None


async def _settle_on_next_card(
    session: aiohttp.ClientSession,
    product: _Product,
    cards: dict[str, dict[date, str]],
    snap: SupplierSnapshot,
    month: date,
) -> SupplierSnapshot:
    """Bill a closed month at the RLP-M and SPP-M the next card names for it.

    Only the month-indexed legs ask, so Dynamisch pays for no second card.
    While the next card is not out, or names another month or no index for
    a leg, the month is flagged ``provisional``, so the month cache asks
    again rather than filing an estimate as a closed month's fact.
    """
    energy, injection = snap.energy, snap.injection
    settles_energy = isinstance(energy, VariableRates) and energy.month_indexed
    settles_injection = injection is not None and injection.spp_indexed
    if not settles_energy and not settles_injection:
        return snap
    following = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    try:
        found = await _read(session, product, cards, following)
    except ExtractorError as err:
        # A timeout or a 5xx says nothing about the month, so let the month
        # cache retry it rather than freeze an estimate behind it.
        if is_transient_fetch_error(str(err)):
            raise
        found = None
    changed: dict[str, Any] = {}
    if found is not None:
        energy_block, feed_in_block = _price_blocks(found[1])
        rlp = named_index(energy_block, "RLP")
        spp = named_index(feed_in_block, "SPP")
        # The month is checked, not assumed: a card that skipped a month
        # would otherwise settle this one on another month's index.
        if settles_energy and rlp is not None and rlp[0] == month:
            changed["energy"] = settled_energy(energy, rlp[1] / 1000.0)
        if (
            injection is not None
            and settles_injection
            and spp is not None
            and spp[0] == month
        ):
            changed["injection"] = settled_injection(injection, spp[1] / 1000.0)
    if (settles_energy and "energy" not in changed) or (
        settles_injection and "injection" not in changed
    ):
        return replace(snap, provisional=True, **changed)
    return replace(snap, **changed)


async def probe(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> str | None:
    """The link the newest card is listed at, whose hash changes when the
    card is uploaded again. None on a failed read, so the TTL takes over."""
    product = _BY_ID.get(contract_id)
    if product is None or region != REGION_FLANDERS:
        return None
    try:
        return _newest(await _listed(session), product)
    except ExtractorError:
        return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The residential products the current cards page links, by contract id
    where one is registered and as ``belvus_<file>`` where none is."""
    try:
        page = await fetch_text(session, _CURRENT_URL)
    except ExtractorError:
        return set()
    return {
        _BY_FILE.get(name, f"{_SUPPLIER}_{name.lower()}")
        for name in listed_cards(page)
        if not name.endswith("Pro")
    }


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, source_url: str = _CURRENT_URL
) -> SupplierSnapshot:
    """Parse one card's text as pypdf extracts it, in either layout."""
    product = _product(contract_id, REGION_FLANDERS)
    if is_older(text):
        card = read_older(text, product.file, product.indices)
    else:
        card = _read_card(text, product)
    if _REGION_RE.search(text) is None:
        raise ExtractorError(f"{_LABEL}: card is not for Flemish households")
    energy, injection = _rates(product, card)
    month = card.month
    return with_vat_basis(
        SupplierSnapshot(
            supplier=_SUPPLIER,
            contract=contract_id,
            energy=energy,
            injection=injection,
            dsos=card.dsos,
            taxes=card.taxes,
            source_url=source_url,
            publication_label=f"{NL_MONTHS[month.month - 1]} {month.year}",
            valid_until=end_of_month(month.year, month.month),
            network_year=card.network_year,
        ),
        # The cards state no rate; the formulas are grossed by the
        # residential one, recorded as assumed.
        None,
    )


def _read_card(text: str, product: _Product) -> Card:
    """A card of the layout used since September 2026."""
    month = _card_month(text, product)
    if _LEVIES_HEAD not in text:
        raise ExtractorError(f"{_LABEL}: levies not stated including VAT")
    energy, feed_in, fee = _legs(text, product)
    return Card(
        month=month,
        fee=fee,
        energy=energy,
        feed_in=feed_in,
        dsos=_dsos(text),
        taxes=regional_tax_overlay(
            text,
            supplier=_LABEL,
            region=REGION_FLANDERS,
            excise=(_EXCISE_RE,),
            renewables=(_GSC_RE, _WKK_RE),
            contribution=_CONTRIBUTION_RE,
            fund=_FUND_RE,
        ),
    )


def _price_blocks(text: str) -> tuple[str, str]:
    """A card's energy block and its feed-in block, in either layout."""
    return older_blocks(text) if is_older(text) else _blocks(text)


def _card_month(text: str, product: _Product) -> date:
    """The month of the card, once it is shown to be the product's own
    residential card. Every page after the first repeats the header."""
    headers = {
        (segment.lower(), " ".join(name.split()), word.lower(), int(year))
        for segment, name, word, year in _HEADER_RE.findall(text)
    }
    if len(headers) != 1:
        raise ExtractorError(f"{_LABEL}: card header not found")
    segment, name, word, year = headers.pop()
    key = name.removeprefix("Belvus ").removesuffix(" EL").replace(" ", "")
    if segment != "particulier" or key.casefold() != product.file.casefold():
        raise ExtractorError(f"{_LABEL}: card is for {name}, not {product.label}")
    if word not in NL_MONTHS:
        raise ExtractorError(f"{_LABEL}: unknown month {word!r}")
    return date(year, NL_MONTHS.index(word) + 1, 1)


def _blocks(text: str) -> tuple[str, str]:
    """The energy block of the first page and its feed-in block."""
    start = text.find(_ENERGY_START)
    feed_in = text.find(_FEED_IN_START, max(start, 0))
    end = text.find(_PAGE_END, max(feed_in, 0))
    if start < 0 or feed_in < 0 or end < 0:
        raise ExtractorError(f"{_LABEL}: price blocks not found")
    return text[start:feed_in], text[feed_in:end]


def _formula(match: re.Match[str]) -> tuple[str, float, float]:
    """A formula's index, factor and base, the base in EUR/MWh."""
    index, factor, sign, inner, base = match.groups()
    return (
        index,
        to_float(factor),
        parse_sign(sign) * parse_sign(inner or "") * to_float(base),
    )


def _leg(block: str, index: str, named: str, what: str) -> Leg:
    """A block's formula and its printed price, before VAT, once the formula
    gives back the printed price at the index the block names."""
    match = _FORMULA_RE.search(block)
    if match is None:
        raise ExtractorError(f"{_LABEL}: {what} formula not found")
    found, factor, base = _formula(match)
    if found != index:
        raise ExtractorError(f"{_LABEL}: {what} formula is on {found}, not {index}")
    printed = _PRINTED_RE.search(block)
    at = named_index(block, named)
    if printed is None or at is None:
        raise ExtractorError(f"{_LABEL}: {what} price check not found")
    leg = Leg(
        indexed_on=found,
        factor=factor,
        base=base,
        price=parse_sign(printed.group(1)) * to_float(printed.group(2)),
        written=" ".join(match.group(0).split()),
    )
    held_to_price(leg, at[1], what)
    return leg


def _legs(text: str, product: _Product) -> tuple[Leg, Leg, float]:
    """The energy and feed-in legs and the yearly fee."""
    energy_block, feed_in_block = _blocks(text)
    energy = _leg(energy_block, product.indices[0], "RLP", "energy")
    # The third page restates the energy formula; any formula outside the
    # feed-in block that says something else is a card to look at.
    head, tail = text.split(feed_in_block, 1)
    formula = (energy.indexed_on, energy.factor, energy.base)
    if {_formula(m) for m in _FORMULA_RE.finditer(head + tail)} != {formula}:
        raise ExtractorError(f"{_LABEL}: the card prints more than one energy formula")
    fee = _FEE_RE.search(text)
    if fee is None:
        raise ExtractorError(f"{_LABEL}: yearly fee not found")
    feed_in = _leg(feed_in_block, product.indices[1], "SPP", "feed-in")
    return energy, feed_in, to_float(fee.group(1))


def _rates(product: _Product, card: Card) -> tuple[EnergyRates, InjectionRates]:
    # EUR/MWh against an index in EUR/MWh is EUR/kWh against one in EUR/kWh
    # for the factor; the base goes from EUR/MWh to EUR/kWh.
    energy_leg, feed_in = card.energy, card.feed_in
    factor, base = energy_leg.factor, energy_leg.base / 1000.0
    feed_factor, feed_base = feed_in.factor, feed_in.base / 1000.0
    vat = 1.0 + VAT_RATE_REDUCED
    energy: EnergyRates
    if product.kind == "dynamic":
        energy = DynamicRates(
            factor=factor * vat,
            base=base * vat,
            yearly_fixed_fee=card.fee,
            quarter_hourly=True,
        )
        # Per quarter hour like the energy, so the printed price is no rate.
        return energy, InjectionRates(
            factor=feed_factor, base=feed_base, formula=feed_in.written
        )
    energy = VariableRates(
        current=energy_leg.price / 100.0 * vat,
        yearly_fixed_fee=card.fee,
        formula=f"{energy_leg.written} excl. btw",
        formula_factor=factor * vat,
        formula_base=base * vat,
        month_indexed=True,
        rlp_indexed=True,
        rlp_blend="columns",
    )
    # Residential feed-in is VAT-exempt, so neither figure is grossed.
    return energy, InjectionRates(
        current=feed_in.price / 100.0,
        factor=feed_factor,
        base=feed_base,
        formula=feed_in.written,
        spp_indexed=True,
    )


def _dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital meter's network table, and the classic meter's prosumer
    rate.

    A digital row reads the capacity, the normal and the exclusive-night
    rate, a classic row the prosumer rate first. An area whose digital row
    does not read is left out, which ``fetch`` refuses for the live card and
    ``fetch_for_month`` for the running month's (``require_every_area``).
    """
    data = _DATA_RE.search(text)
    classic_start = text.find(_CLASSIC_HEAD)
    classic_end = text.find(_CLASSIC_END, max(classic_start, 0))
    if data is None or classic_start < 0 or classic_end < 0:
        raise ExtractorError(f"{_LABEL}: network table not found")
    # "Fluvius Midden-" wraps its label onto the line of its figures.
    classic = text[classic_start:classic_end].replace("-\n", "-")
    out: dict[str, DsoOverlay] = {}
    for label, key in FLUVIUS_CARD_LABELS.items():
        # The labels share all but the area, and pypdf reads them whole.
        row = numeric_row(
            text, label, 3, after=_DIGITAL_HEAD, before=_CLASSIC_HEAD, threshold=0.9
        )
        if row is None:
            continue
        capacity, single, night = (to_float(value) for value in row)
        prosumer = numeric_row(classic, label, 4, threshold=0.9)
        out[key] = DsoOverlay(
            distribution_single=single / 100.0,
            distribution_exclusive_night=night / 100.0,
            # One rate holding transport, as on every Flemish card.
            transport=0.0,
            data_management_per_year=to_float(data.group(1)),
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=(
                to_float(prosumer[0]) if prosumer is not None else None
            ),
        )
    if not out:
        raise ExtractorError(f"{_LABEL}: no network row read")
    return out


EXTRACTOR = SupplierExtractor(
    id=_SUPPLIER,
    label=_LABEL,
    # 0,48 s to read and parse the slowest of the November 2026 cards on a
    # Raspberry Pi 4, plus 10%.
    sweep_cost_s=0.55,
    contracts=tuple(
        Contract(
            id=p.contract_id,
            label=p.label,
            kind=p.kind,
            regions=frozenset({REGION_FLANDERS}),
            spot_indexed_injection=p.kind == "variable",
            month_indexed_energy=p.kind == "variable",
            # The older cards of January to April 2026 (_belvus_older.py).
            earlier_network_cards=p.kind == "variable",
        )
        for p in _PRODUCTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
    settles_on_next_card=True,
)

__all__ = [
    "EXTRACTOR",
    "discover",
    "fetch",
    "fetch_for_month",
    "listed_cards",
    "named_index",
    "parse_snapshot",
    "probe",
]
