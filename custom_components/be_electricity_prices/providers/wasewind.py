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

"""Wase Wind tariff card extractor.

Wase Wind is a cooperative selling wind power to households in eighteen
municipalities of the Waasland and around it, in Flanders. It publishes one
card a month for each of its 2026 products, all listed on one page per
product and year, ``/tariefkaarten-<year>-incl.-btw-<product>`` on
www.wasewind.be. The cards sit in several folders under
``/frontend/files/userfiles/files/`` and are named
``Tariefkaart WW-<code>-HH-<YYMM>.pdf``, where ``00`` is the card of a year
published before it starts (``2700`` in November 2026).

- Vol dynamisch (``DY``), for a digital meter read per quarter hour:
  ``0,106 x Belpex kwartier + 1,59`` c€/kWh, the feed-in
  ``0,10 x Belpex kwartier - 1,50``.
- Vast-dynamisch (``VD``), the same meter: ``0,106 x 50% x Belpex kwartier
  + 7,72``, the feed-in a fixed 2,0 c€/kWh.
- Vast-variabel (``VV``), any meter: ``0,122 x 50% x Belpex maand + 7,72`` on
  the single, day and night registers and ``0,106 x 50% x Belpex maand +
  6,10`` on an exclusive-night one, on the delivery month's mean of the
  day-ahead prices. January 2026's card printed 0,138 for the first. The
  feed-in is a fixed 2,0 c€/kWh.

Every figure is printed in c€/kWh including the 6% VAT the card states, the
feed-in without VAT. Each formula is held to the price the card prints for
it at the Belpex the VNR estimated for the month. The 2027 cards drop the
"50%", price the feed-in of the two fixed products on a formula as well and
are read the same way.

The cards print no network table and no levy but the certificates: "Tarieven
exclusief netkosten, heffingen en toeslagen". The network is read off
Fluvius's sheet of each area for the card's year, the one a custom entry can
take, grossed at the card's VAT rate; while Fluvius has not listed that
year's sheets, the year before's stands in and the card says so
(``network_year``), so a month of the later year is billed its own once
read. The federal levies are the law's (``federal_levies_unread``), and the
Flemish energy fund is not charged to a household in 2026.

Not modelled: the Energiedelen fee (136,98 € a year, only while sharing),
and the analog-meter forfait of Vast-variabel, which is read but billed only
under the Walloon compensation regime.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util
from yarl import URL

from ..const import FLUVIUS_KEYS, REGION_FLANDERS
from ..fluvius import AreaTerms, ensure_area_terms, held_area_terms
from ._parse import SIGN_CHARS, parse_sign, require_contract, to_float
from ._pdf import (
    NL_MONTHS,
    fetch_pdf_text_layout,
    fetch_text,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    InjectionRates,
    TariffKind,
    VariableRates,
)
from ._validity import archive_validity_check, end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
)

_SUPPLIER: Final = "wasewind"
_LABEL: Final = "Wase Wind"
_SITE: Final = "https://www.wasewind.be"


@dataclass(frozen=True)
class _Product:
    contract_id: str
    label: str
    # The listing's and the product page's name for it.
    slug: str
    # WW-<code>-HH in the card's file name.
    code: str
    kind: TariffKind


_PRODUCTS: Final = (
    _Product(
        "wasewind_vol_dynamisch", "Vol dynamisch", "vol-dynamisch", "DY", "dynamic"
    ),
    _Product(
        "wasewind_vast_dynamisch", "Vast-dynamisch", "vast-dynamisch", "VD", "dynamic"
    ),
    _Product(
        "wasewind_vast_variabel", "Vast-variabel", "vast-variabel", "VV", "variable"
    ),
)
_BY_ID: Final = {p.contract_id: p for p in _PRODUCTS}
_BY_SLUG: Final = {p.slug: p.contract_id for p in _PRODUCTS}

# A link as the pages print it, bounded, then held to the card's path.
_HREF_RE: Final = re.compile(r'href="([^"<>\s]{1,300})"')
_CARD_PATH_RE: Final = re.compile(
    r"/frontend/files/userfiles/files/(?:[A-Za-z0-9%._-]{1,40}/){0,3}"
    r"Tariefkaart%20WW-([A-Z]{1,2})-HH-(\d{2})(\d{2})(?:\(\d{1,2}\))?\.pdf"
)
# A product page the year's household tariffs page links.
_PRODUCT_PATH_RE: Final = re.compile(
    r"/klant-worden/gezin/tarieven/tarieven-(\d{4})-gezin/"
    r"([a-z]{1,20}(?:-[a-z]{1,20}){0,3})-tarief-(\d{4})-gezin"
)

_NUM: Final = r"\d+(?:,\d+)?"
# The prices worked out at the estimated Belpex print either separator.
_PRICE: Final = r"\d+[.,]\d+"
_LEGS: Final = r"Alle meetmomenten|Enkelvoudig, piek en dal|Uitsluitend nacht"
# "Alle meetmomenten 0,106 x 50% x Belpex kwartier + 7,72": the register,
# the factor, the share, the index and the signed base.
_FORMULA_RE: Final = re.compile(
    rf"^({_LEGS})[ \t]+({_NUM})[ \t]*x[ \t]*(?:({_NUM})[ \t]*%[ \t]*x[ \t]*)?"
    rf"Belpex[ \t]+(kwartier|maand)[ \t]*([{SIGN_CHARS}])[ \t]*({_NUM})[ \t]*$",
    re.MULTILINE,
)
# "Alle meetmomenten 20,6733": the price a formula gives at the estimate.
_PRINTED_RE: Final = re.compile(
    rf"^({_LEGS})[ \t]+([{SIGN_CHARS}]?)({_PRICE})[ \t]*$", re.MULTILINE
)
# "geschatte Belpex: 180,03122", the figure on the same line or the next.
_ESTIMATE_RE: Final = re.compile(rf"Belpex:[ \t]*\n?[ \t]*({_PRICE})")
# "Vlaamse nutsregulator in november 2026" or "in november,", the year on
# the next line on the 2027 cards.
_MONTH_RE: Final = re.compile(
    rf"Vlaamse nutsregulator in ({'|'.join(NL_MONTHS)})\b[ \t,]*\n?[ \t]*(20\d{{2}})?",
    re.IGNORECASE,
)
# "in de periode 1/1/2026 tot 1/1/2027" and "PERIODE VAN 01/01/27 TOT 01/01/28".
_PERIOD_RE: Final = re.compile(
    r"periode[ \t]+(?:van[ \t]+)?0?1/0?1/(\d{4}|\d{2})[ \t]+tot[ \t]+0?1/0?1/(\d{4}|\d{2})\b",
    re.IGNORECASE,
)
_TITLE_RE: Final = re.compile(
    r"^Tariefkaart(?:[ \t]+20\d{2})?\s+([A-Za-z][A-Za-z -]{2,30}?)[ \t]*$",
    re.MULTILINE,
)
# "Particulier incl. 6% btw", and only that: the Energiedelen fee is
# "incl. 21% btw" further down.
_VAT_RE: Final = re.compile(
    r"^Particulier[ \t]+incl\.[ \t]*(\d+)[ \t]*%[ \t]*btw", re.MULTILINE
)
_OFFTAKE_RE: Final = re.compile(r"^AFNAME\b", re.MULTILINE | re.IGNORECASE)
_FEED_IN_RE: Final = re.compile(r"^INJECTIE\b", re.MULTILINE | re.IGNORECASE)
# What follows the feed-in block on every layout.
_FEED_IN_END_RE: Final = re.compile(
    r"^(?:ANALOGE METER|ENERGIEDELEN|Administratieve kost|Tariefelement|Abonnement)",
    re.MULTILINE | re.IGNORECASE,
)
_NO_VAT_RE: Final = re.compile(
    r"btw[ \t]+n(?:iet van toepassing|\.v\.t\.)", re.IGNORECASE
)
_FIXED_FEED_IN_RE: Final = re.compile(
    rf"^Vaste injectievergoeding:?[ \t]*({_NUM})[ \t]*c€/kWh", re.MULTILINE
)
_FEE_RE: Final = re.compile(
    rf"^Abonnement:?[ \t]*€[ \t]*({_NUM})[ \t]*(?:per[ \t]+jaar|/[ \t]*jaar)",
    re.MULTILINE,
)
_CERTIFICATES_RE: Final = re.compile(
    r"^Bijdrage certificaten[^\n]*c€/kWh", re.MULTILINE
)
_GSC_RE: Final = re.compile(
    rf"^Groene stroom certificaten[ \t]+({_NUM})[ \t]*$", re.MULTILINE
)
_WKK_RE: Final = re.compile(
    rf"^Warmtekracht certificaten[ \t]+({_NUM})[ \t]*$", re.MULTILINE
)
_PV_RE: Final = re.compile(
    rf"^Tariefelement analoge meter[ \t]*\+[ \t]*pv[ \t]*€[ \t]*({_NUM})[ \t]*/?[ \t]*kva[ \t]*/[ \t]*jaar",
    re.MULTILINE | re.IGNORECASE,
)
# The index each formula names, defined in EUR/MWh in the card's footnote,
# on its line or the next.
_INDEX_UNIT_RE: Final = re.compile(
    r"^Belpex (kwartier|maand)[ \t]*=[^\n]*(?:\n[^\n]*)?€/MWh", re.MULTILINE
)
# How far a printed price may stand from its formula at the estimate, c€/kWh:
# the prices are printed to four decimals.
_PRICE_TOLERANCE: Final = 6e-4
# Opens the message of a card whose network could not be read, which a later
# read may heal.
_NETWORK_UNREAD: Final = "Fluvius network sheets not read"


def _product(contract_id: str, region: str) -> _Product:
    product = require_contract(_BY_ID, contract_id, _LABEL)
    if region != REGION_FLANDERS:
        raise ExtractorError(f"{_LABEL} {contract_id}: not sold in region {region!r}")
    return product


def _listing_url(product: _Product, year: int) -> str:
    return f"{_SITE}/tariefkaarten-{year}-incl.-btw-{product.slug}"


def listed_cards(page: str, product: _Product, year: int) -> dict[date, str]:
    """The product's cards of ``year`` a listing links, by the month each
    prices. The card of a year published before it starts prices January
    until that month's own is out."""
    out: dict[date, str] = {}
    early: str | None = None
    for href in _HREF_RE.findall(page):
        match = _CARD_PATH_RE.fullmatch(href)
        if match is None or match.group(1) != product.code:
            continue
        if 2000 + int(match.group(2)) != year:
            continue
        number = int(match.group(3))
        url = f"{_SITE}{href}"
        if number == 0:
            early = url
        elif number <= 12:
            out[date(year, number, 1)] = url
    if early is not None:
        out.setdefault(date(year, 1, 1), early)
    return out


async def _listed(
    session: aiohttp.ClientSession, product: _Product, year: int
) -> dict[date, str]:
    page = await fetch_text(session, _listing_url(product, year))
    return listed_cards(page, product, year)


def _running(cards: dict[date, str], product: _Product, month: date) -> date:
    """The newest card's month up to ``month``."""
    months = [m for m in cards if m <= month]
    if not months:
        raise ExtractorError(
            f"{_LABEL}: no {product.label} card listed for {month.year}"
        )
    return max(months)


async def _network(
    session: aiohttp.ClientSession, year: int
) -> tuple[dict[str, AreaTerms], int | None]:
    """Every area's network rates off Fluvius's sheets of ``year``, read or
    held, and the year of the sheets standing in where an earlier one's do.
    Raises while an area has none."""
    out: dict[str, AreaTerms] = {}
    years: set[int] = set()
    missing: list[str] = []
    for dso in sorted(FLUVIUS_KEYS):
        await ensure_area_terms(session, year, dso)
        held = held_area_terms(year, dso)
        if held is None:
            missing.append(dso)
            continue
        years.add(held[0])
        out[dso] = held[1]
    if missing:
        raise ExtractorError(f"{_NETWORK_UNREAD} for {year}: {', '.join(missing)}")
    earliest = min(years)
    return out, None if earliest == year else earliest


async def _read(
    session: aiohttp.ClientSession, product: _Product, url: str
) -> tuple[SupplierSnapshot, str]:
    text = await fetch_pdf_text_layout(session, url)
    year = _card_year(text)
    network, network_year = await _network(session, year)
    snap = parse_snapshot(
        product.contract_id, text, network, url, network_year=network_year
    )
    return snap, text


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """The newest card of the running year up to the running month."""
    product = _product(contract_id, region)
    month = dt_util.now().date().replace(day=1)
    cards = await _listed(session, product, month.year)
    snap, _text = await _read(session, product, cards[_running(cards, product, month)])
    return snap


async def fetch_for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, year_month: date
) -> SupplierSnapshot | None:
    """The month's card, or None where the listing has none for it."""
    product = _BY_ID.get(contract_id)
    if product is None or region != REGION_FLANDERS:
        return None
    month = year_month.replace(day=1)
    try:
        url = (await _listed(session, product, month.year)).get(month)
        if url is None:
            return None
        snap, text = await _read(session, product, url)
        return archive_validity_check(snap, text, month)
    except ExtractorError as err:
        # A timeout, a 5xx or an unread network sheet says nothing about the
        # month: raise, so the month cache asks again rather than file it as
        # absent.
        message = str(err)
        if is_transient_fetch_error(message) or message.startswith(_NETWORK_UNREAD):
            raise
        return None


async def probe(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> str | None:
    """The address of the card ``fetch`` reads, which a new month's card
    changes. None on a failed read, so the TTL takes over."""
    product = _BY_ID.get(contract_id)
    if product is None or region != REGION_FLANDERS:
        return None
    month = dt_util.now().date().replace(day=1)
    try:
        cards = await _listed(session, product, month.year)
        return cards[_running(cards, product, month)]
    except ExtractorError:
        return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The products the running year's household tariffs page links, by
    contract id where one is registered and as ``wasewind_<name>`` where
    none is."""
    year = dt_util.now().year
    try:
        page = await fetch_text(
            session, f"{_SITE}/klant-worden/gezin/tarieven/tarieven-{year}-gezin"
        )
    except ExtractorError:
        return set()
    out: set[str] = set()
    for href in _HREF_RE.findall(page):
        path = URL(href).path if href.startswith(_SITE) else href
        match = _PRODUCT_PATH_RE.fullmatch(path)
        if match is None or {int(match.group(1)), int(match.group(3))} != {year}:
            continue
        slug = match.group(2)
        out.add(_BY_SLUG.get(slug, f"{_SUPPLIER}_{slug.replace('-', '_')}"))
    return out


# ---- pure parser -------------------------------------------------------------


@dataclass(frozen=True)
class _Formula:
    """One register's formula in EUR/kWh on a spot in EUR/kWh, and the price
    the card prints for it at the estimate, in EUR/kWh."""

    index: str
    factor: float
    base: float
    printed: float
    text: str


def parse_snapshot(
    contract_id: str,
    text: str,
    network: dict[str, AreaTerms],
    source_url: str = _SITE,
    *,
    network_year: int | None = None,
) -> SupplierSnapshot:
    """Parse one card as read layout-preserving, its network the sheets'
    rates of each area, excluding VAT, ``network_year`` their year where it
    is an earlier one than the card's."""
    product = _product(contract_id, REGION_FLANDERS)
    titles = {_title(m) for m in _TITLE_RE.findall(text)}
    if titles != {_title(product.label)}:
        raise ExtractorError(f"{_LABEL}: card is not the {product.label} card")
    if "Vlaamse nutsregulator" not in text:
        raise ExtractorError(f"{_LABEL}: card is not for Flemish households")
    month = _card_month(text)
    rate = printed_vat_rate(text, _VAT_RE)
    if rate is None:
        raise ExtractorError(f"{_LABEL}: VAT rate not stated")
    missing = FLUVIUS_KEYS - set(network)
    if missing:
        raise ExtractorError(f"{_LABEL}: no network for {', '.join(sorted(missing))}")
    offtake, feed_in = _blocks(text)
    fee = _FEE_RE.search(text)
    if fee is None:
        raise ExtractorError(f"{_LABEL}: yearly fee not found")
    # The indices the footnote defines in EUR/MWh, which pins the factors.
    defined = set(_INDEX_UNIT_RE.findall(text))
    energy = _energy(product, offtake, to_float(fee.group(1)), defined)
    pv = _PV_RE.search(text)
    return with_vat_basis(
        SupplierSnapshot(
            supplier=_SUPPLIER,
            contract=contract_id,
            energy=energy,
            injection=_injection(feed_in, defined),
            dsos={dso: _overlay(terms, 1.0 + rate) for dso, terms in network.items()},
            taxes=_taxes(text),
            source_url=source_url,
            publication_label=f"{NL_MONTHS[month.month - 1]} {month.year}",
            valid_until=end_of_month(month.year, month.month),
            supplier_prosumer_eur_per_kva_year=to_float(pv.group(1)) if pv else None,
            network_year=network_year,
        ),
        rate,
    )


def _title(name: str) -> str:
    return " ".join(name.lower().replace("-", " ").split())


def _card_year(text: str) -> int:
    """The year the card prices, which its period runs over to the next."""
    match = _PERIOD_RE.search(text)
    if match is None:
        raise ExtractorError(f"{_LABEL}: card period not found")
    start, end = (int(y) + (2000 if len(y) == 2 else 0) for y in match.groups())
    if end != start + 1:
        raise ExtractorError(f"{_LABEL}: card period is not one calendar year")
    return start


def _card_month(text: str) -> date:
    """The month the card prices: that of the VNR estimate its offtake price
    is worked out at, or January for a card published the year before."""
    year = _card_year(text)
    offtake_at = _OFFTAKE_RE.search(text)
    match = _MONTH_RE.search(text, offtake_at.end() if offtake_at else 0)
    if match is None:
        raise ExtractorError(f"{_LABEL}: card month not found")
    named = match.group(2)
    if named is None or int(named) == year:
        return date(year, NL_MONTHS.index(match.group(1).lower()) + 1, 1)
    if int(named) == year - 1:
        return date(year, 1, 1)
    raise ExtractorError(f"{_LABEL}: card month {named} is outside its period")


def _blocks(text: str) -> tuple[str, str]:
    """The offtake and the feed-in block, each stating its unit."""
    start = _OFFTAKE_RE.search(text)
    middle = _FEED_IN_RE.search(text, start.end()) if start else None
    if start is None or middle is None:
        raise ExtractorError(f"{_LABEL}: offtake and feed-in blocks not found")
    end = _FEED_IN_END_RE.search(text, middle.end())
    offtake = text[start.start() : middle.start()]
    feed_in = text[middle.start() : end.start() if end else len(text)]
    if "c€/kWh" not in offtake or "c€/kWh" not in feed_in:
        raise ExtractorError(f"{_LABEL}: a block is not in c€/kWh")
    if _NO_VAT_RE.search(feed_in) is None:
        raise ExtractorError(f"{_LABEL}: feed-in VAT basis not stated")
    return offtake, feed_in


def _formulas(block: str, indices: set[str]) -> dict[str, _Formula]:
    """Each register's formula in ``block`` held to the price printed for it,
    the index one of ``indices``, which the caller keeps to those the card
    defines in EUR/MWh."""
    estimate = _ESTIMATE_RE.search(block)
    printed = {m.group(1): m for m in _PRINTED_RE.finditer(block)}
    out: dict[str, _Formula] = {}
    for match in _FORMULA_RE.finditer(block):
        leg, factor, share, index, sign, base = match.groups()
        if leg in out:
            raise ExtractorError(f"{_LABEL}: two formulas for {leg}")
        if index not in indices:
            raise ExtractorError(f"{_LABEL}: {leg} is indexed on Belpex {index}")
        if estimate is None or leg not in printed:
            raise ExtractorError(f"{_LABEL}: the price check for {leg} not found")
        multiplier = to_float(factor) * (to_float(share) / 100.0 if share else 1.0)
        added = parse_sign(sign) * to_float(base)
        price = printed[leg]
        stated = parse_sign(price.group(2)) * to_float(price.group(3))
        if (
            abs(multiplier * to_float(estimate.group(1)) + added - stated)
            > _PRICE_TOLERANCE
        ):
            raise ExtractorError(
                f"{_LABEL}: the {leg} formula does not give the price printed"
            )
        # c€/kWh on EUR/MWh is ten times the factor on EUR/kWh.
        out[leg] = _Formula(
            index, multiplier * 10.0, added / 100.0, stated / 100.0, match.group(0)
        )
    return out


def _energy(
    product: _Product, block: str, fee: float, defined: set[str]
) -> EnergyRates:
    if product.kind == "dynamic":
        formulas = _formulas(block, {"kwartier"} & defined)
        if len(formulas) != 1 or "Uitsluitend nacht" in formulas:
            raise ExtractorError(f"{_LABEL}: one offtake formula expected")
        (formula,) = formulas.values()
        return DynamicRates(
            factor=formula.factor,
            base=formula.base,
            yearly_fixed_fee=fee,
            quarter_hourly=True,
        )
    formulas = _formulas(block, {"maand"} & defined)
    single = formulas.get("Enkelvoudig, piek en dal")
    night = formulas.get("Uitsluitend nacht")
    if single is None or night is None or len(formulas) != 2:
        raise ExtractorError(
            f"{_LABEL}: the single and exclusive-night formulas expected"
        )
    return VariableRates(
        current=single.printed,
        exclusive_night=night.printed,
        yearly_fixed_fee=fee,
        formula=f"{single.text}; {night.text}",
        formula_factor=single.factor,
        formula_base=single.base,
        formula_factor_exclusive_night=night.factor,
        formula_base_exclusive_night=night.base,
        month_indexed=True,
    )


def _injection(block: str, defined: set[str]) -> InjectionRates:
    fixed = _FIXED_FEED_IN_RE.search(block)
    formulas = _formulas(block, {"kwartier", "maand"} & defined)
    if fixed is not None and not formulas:
        return InjectionRates(current=to_float(fixed.group(1)) / 100.0)
    formula = formulas.get("Alle meetmomenten")
    if fixed is not None or formula is None or len(formulas) != 1:
        raise ExtractorError(f"{_LABEL}: one feed-in price expected")
    if formula.index == "kwartier":
        return InjectionRates(
            factor=formula.factor, base=formula.base, formula=formula.text
        )
    return InjectionRates(
        current=formula.printed,
        factor=formula.factor,
        base=formula.base,
        formula=formula.text,
        month_indexed=True,
    )


def _overlay(terms: AreaTerms, vat: float) -> DsoOverlay:
    """An area's sheet rates on the card's basis, including VAT."""
    fields: dict[str, Any] = {
        field: None if value is None else value * vat
        for field, value in terms.overlay_fields().items()
    }
    return DsoOverlay(**fields)


def _taxes(text: str) -> TaxOverlay:
    """The certificates, the only levy the card prints. The federal ones are
    the law's, and the energy fund is not charged to a household."""
    if _CERTIFICATES_RE.search(text) is None:
        raise ExtractorError(f"{_LABEL}: certificates not in c€/kWh")
    green, chp = _GSC_RE.search(text), _WKK_RE.search(text)
    if green is None or chp is None:
        raise ExtractorError(f"{_LABEL}: certificates not found")
    return TaxOverlay(
        federal_excise=0.0,
        energy_contribution=0.0,
        flanders_renewables=(to_float(green.group(1)) + to_float(chp.group(1))) / 100.0,
        federal_levies_unread=True,
        vat_rate=0.0,
    )


EXTRACTOR = SupplierExtractor(
    id=_SUPPLIER,
    label=_LABEL,
    # 0,71 s to read the slowest of the cards, Vast-variabel's 2027 one, on a
    # Raspberry Pi 4, plus 10%.
    sweep_cost_s=0.8,
    contracts=tuple(
        Contract(
            id=product.contract_id,
            label=product.label,
            kind=product.kind,
            regions=frozenset({REGION_FLANDERS}),
            month_indexed_energy=product.kind == "variable",
            earlier_network_cards=True,
        )
        for product in _PRODUCTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
