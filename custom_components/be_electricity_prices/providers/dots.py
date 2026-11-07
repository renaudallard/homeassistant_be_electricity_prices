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

"""Dots Energy tariff card extractor.

Dots Energy sells two residential electricity products in Flanders, both
dynamic and both for a digital meter read every quarter hour (meetregime 3):
Dots Dynamic, and Dots Dynamic Insights, which comes with a P1 dongle and an
app. Each card is served at one address, ``/api/tariefkaarten/<slug>`` on
www.dotsenergy.be, which answers with a redirect to the month's PDF
(``/documents/tariefkaarten/tariefkaart-<slug>-MM-YYYY.pdf``); every hop is
held to that site. The months before answer 404, so there is no archive. The
site's home page links both cards, which is what ``discover`` reads.

Both cards price each quarter hour on the EPEX SPOT BE day-ahead price,
``1 * BELPEX + base`` in c€/kWh before VAT, and credit the feed-in at
``1 * BELPEX - base`` in c€/kWh, VAT not applying. Insights adds an
evenwichtsbijdrage, a balancing contribution, to both legs:
``0,005 * BELPEX + 0``, printed before VAT and stated as the upper bound of
what is billed, a lower real cost coming back as a discount. It is billed at
that bound, folded into each formula: 1,005 x BELPEX + 1,5 for the offtake,
0,995 x BELPEX - 1,5 for the feed-in. It follows BELPEX through zero as the
formula reads: on a negative price the contribution is negative too, which
lowers the offtake price and raises the feed-in credit.

The card's own check pins the units. Each formula, at the VNR year price the
card names, has to give back the price it prints beside it (18,003 + 1,25 is
19,253 on Dynamic's November 2026 card), and a formula stated in another unit
than c€/kWh is refused.

Every other figure is printed including the 6% VAT the card states. The
monthly fees, €5,3 and €7,95, are 5,00 and 7,50 before it, and the
certificate costs are 11% and 14% of a certificate at €106 and €30,74, which
are 100 and 29 before it. The second page reads only word-aligned: the plain
reading detaches whole rows of its network table and levies from their
labels.

Not modelled: the Dynamic Green option (c€0,419/kWh on top, for guarantees
of origin), Insights' P1 dongle (€49,99 once), the Energiedelen fee (12,1 € a
month, only for energy sharing) and the Connecting the Dots referral credit.
SEG - Dynamic Flex, for Smart-E-Grid customers only, publishes no card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import aiohttp
from yarl import URL

from ..const import FLUVIUS_CARD_LABELS, REGION_FLANDERS
from ._links import on_page_site
from ._parse import (
    LINE_BREAK,
    SIGN_CHARS,
    numeric_row,
    parse_sign,
    regional_tax_overlay,
    require_contract,
    require_every_area,
    to_float,
)
from ._pdf import (
    NL_MONTHS,
    fetch_pdf_text_aligned,
    fetch_text,
    printed_vat_rate,
    vat_multiplier,
)
from ._rates import Contract, DynamicRates, InjectionRates
from ._validity import end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    with_vat_basis,
)

_SUPPLIER = "dots"
_LABEL = "Dots Energy"
_SITE = "https://www.dotsenergy.be"
# Where every card has been served from, the API address and the PDF it
# redirects to alike, on 7 November 2026.
_HOSTS = ("www.dotsenergy.be",)
_HOME_URL = f"{_SITE}/nl"
_CARD_PATH = "/api/tariefkaarten/"


@dataclass(frozen=True)
class _Product:
    contract_id: str
    label: str
    slug: str


_PRODUCTS = (
    _Product("dots_dynamic", "Dots Dynamic", "dots-dynamic"),
    _Product(
        "dots_dynamic_insights",
        "Dots Dynamic Insights",
        "dots-dynamic-insights-elektriciteit",
    ),
)
_BY_ID = {p.contract_id: p for p in _PRODUCTS}
_BY_SLUG = {p.slug: p.contract_id for p in _PRODUCTS}

_NUM = r"(\d+(?:,\d+)?)"
# "1 * BELPEX + 1,25": the factor, the sign and the base.
_FORMULA = rf"{_NUM}[ \t]*\*[ \t]*BELPEX[ \t]*([{SIGN_CHARS}])[ \t]*{_NUM}"
_PRICE = rf"([{SIGN_CHARS}]?)(\d+,\d+)"
# The price row, offtake then feed-in, each formula with the price the card
# works out at the VNR year price: "Elektriciteit 1 * BELPEX + 1,25 19,253
# Elektriciteit 1 * BELPEX - 1,25 8,127".
_FORMULAS_RE = re.compile(
    rf"^Elektriciteit[ \t]+{_FORMULA}[ \t]+{_PRICE}"
    rf"[ \t]+Elektriciteit[ \t]+{_FORMULA}[ \t]+{_PRICE}[ \t]*$",
    re.MULTILINE,
)
# "* Tariefformule afname: in c€/kWh excl. BTW." and "** Tariefformule
# injectie: in c€/kWh (BTW n.v.t.).": the unit and basis of each formula.
_UNIT_RE = re.compile(r"Tariefformule[ \t]+(afname|injectie):[ \t]*in[ \t]+([^\n]*)")
_UNITS = {"afname": "c€/kWh excl. BTW", "injectie": "c€/kWh (BTW n.v.t.)"}
# "voor afname (11/2026: c€18,003/kWh)": what each printed price is worked
# out at.
_VNR_RE = re.compile(
    r"VNR[ \t]+jaarprijs[ \t]+voor[ \t]+(afname|teruglevering)[ \t]+"
    r"\(\d{2}/\d{4}:[ \t]*c€[ \t]*(\d+,\d+)[ \t]*/kWh\)"
)
_VNR_LEG = {"afname": "afname", "teruglevering": "injectie"}
# "€5,3/maand/EAN", which Dynamic's card prints on the line above its label.
_FEE_RE = re.compile(r"€[ \t]*(\d+(?:,\d+)?)[ \t]*/maand/EAN")
# "Evenwichtsbijdrage afname (4) 0,005 * BELPEX + 0", Insights only.
_BALANCING_LABEL_RE = re.compile(r"Evenwichtsbijdrage[ \t]+(afname|injectie)\b")
_BALANCING_RE = re.compile(
    rf"Evenwichtsbijdrage[ \t]+(afname|injectie)[ \t]+(?:\(\d\)[ \t]+)?{_FORMULA}"
)
_BALANCING_EX_VAT_RE = re.compile(
    r"evenwichtsbijdrage\s+wordt\s+weergegeven\s+exclusief\s+BTW", re.IGNORECASE
)
_TITLE_RE = re.compile(r"^Dots[ \t]+(Dynamic(?:[ \t]+Insights)?)[ \t]*$", re.MULTILINE)
_CARD_MONTH_RE = re.compile(
    rf"Tariefkaart[ \t]+({'|'.join(NL_MONTHS)})[ \t]+(20\d{{2}})\b", re.IGNORECASE
)
_REGION_TEXT = "residentiële klanten in het Vlaamse Gewest"
# The header's "Alle prijzen zijn inclusief 6% BTW", and only that: Insights'
# dongle is "inclusief 21% BTW" further down, which a reworded header would
# otherwise leave as the only rate read.
_VAT_RE = re.compile(r"Alle prijzen zijn inclusief[ \t]+(\d+)[ \t]*%[ \t]*BTW")
_EXCISE_RE = re.compile(
    rf"Bijzondere accijns op Energie{LINE_BREAK}"
    r"Standaard tarief[ \t]+(\d+,\d+)[ \t]*c€/kWh"
)
_CONTRIBUTION_RE = re.compile(
    rf"Bijdrage op Energie{LINE_BREAK}Standaard tarief[ \t]+(\d+,\d+)[ \t]*c€/kWh"
)
# The energy fund's standard rate, the only standard rate in EUR a month,
# read on its own: the page's rotated side label can sit between it and its
# heading.
_FUND_RE = re.compile(r"^Standaard tarief[ \t]+(\d+,\d+)[ \t]*€/maand", re.MULTILINE)
_GSC_RE = re.compile(r"Kosten groene stroom[ \t]+c€[ \t]*(\d+,\d+)[ \t]*/kWh")
_WKK_RE = re.compile(r"Kosten WKK[ \t]+c€[ \t]*(\d+,\d+)[ \t]*/kWh")
# The page's rotated "Vlaanderen", which the reading spells backwards in front
# of the network row it sits level with.
_SIDE_LABEL = "nerednaalV"


def _product(contract_id: str, region: str) -> _Product:
    product = require_contract(_BY_ID, contract_id, _LABEL)
    if region != REGION_FLANDERS:
        raise ExtractorError(f"{_LABEL} {contract_id}: not sold in region {region!r}")
    return product


def _card_url(product: _Product) -> str:
    return f"{_SITE}{_CARD_PATH}{product.slug}"


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """The card the product's address serves today."""
    url = _card_url(_product(contract_id, region))
    text = await fetch_pdf_text_aligned(
        session, url, x_join_threshold=1.0, hosts=_HOSTS
    )
    snap = parse_snapshot(contract_id, text, url)
    require_every_area(_LABEL, snap, FLUVIUS_CARD_LABELS.values())
    return snap


# A link on the home page, held to the site by on_page_site.
_LINK_RE = re.compile(r'\bhref="([^"<>\s]+)"')
_SLUG_RE = re.compile(r"[a-z0-9-]+")


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The contract ids of every electricity card the home page links.

    A slug no product carries surfaces as ``dots_<slug>``. The gas card the
    page links too is the gas integration's.
    """
    try:
        page = await fetch_text(session, _HOME_URL)
    except ExtractorError:
        return set()
    out: set[str] = set()
    for href in _LINK_RE.findall(page):
        url = on_page_site(_HOME_URL, href)
        path = "" if url is None else URL(url).path
        slug = path.removeprefix(_CARD_PATH)
        if slug == path or not _SLUG_RE.fullmatch(slug) or "gas" in slug.split("-"):
            continue
        out.add(_BY_SLUG.get(slug, f"{_SUPPLIER}_{slug.replace('-', '_')}"))
    return out


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, source_url: str = _HOME_URL
) -> SupplierSnapshot:
    """Parse one card as read word-aligned."""
    product = _product(contract_id, REGION_FLANDERS)
    titles = {" ".join(m.split()) for m in _TITLE_RE.findall(text)}
    if titles != {product.label.removeprefix("Dots ")}:
        raise ExtractorError(f"{_LABEL}: card is not the {product.label} card")
    if _REGION_TEXT not in text:
        raise ExtractorError(f"{_LABEL}: card is not for Flemish households")
    month = _card_month(text)
    vat = vat_multiplier(text, _VAT_RE)
    energy, injection = _legs(text, vat)
    return with_vat_basis(
        SupplierSnapshot(
            supplier=_SUPPLIER,
            contract=contract_id,
            energy=energy,
            injection=injection,
            dsos=_extract_dsos(text),
            taxes=regional_tax_overlay(
                text,
                supplier=_LABEL,
                region=REGION_FLANDERS,
                excise=(_EXCISE_RE,),
                renewables=(_GSC_RE, _WKK_RE),
                contribution=_CONTRIBUTION_RE,
                fund=_FUND_RE,
            ),
            source_url=source_url,
            publication_label=f"{NL_MONTHS[month.month - 1]} {month.year}",
            valid_until=end_of_month(month.year, month.month),
        ),
        printed_vat_rate(text, _VAT_RE),
    )


def _card_month(text: str) -> date:
    match = _CARD_MONTH_RE.search(text)
    if match is None:
        raise ExtractorError(f"{_LABEL}: card month not found")
    return date(int(match.group(2)), NL_MONTHS.index(match.group(1).lower()) + 1, 1)


def _pair(factor: str, sign: str, base: str) -> tuple[float, float]:
    """A formula's factor and base as printed, the base in c€/kWh."""
    return to_float(factor), parse_sign(sign) * to_float(base)


def _legs(text: str, vat: float) -> tuple[DynamicRates, InjectionRates]:
    """The offtake and feed-in formulas, each with the balancing contribution
    folded in at its printed bound, in EUR/kWh on a spot in EUR/kWh."""
    row = _FORMULAS_RE.search(text)
    if row is None:
        raise ExtractorError(f"{_LABEL}: price formulas not found")
    groups = row.groups()
    legs = {"afname": _pair(*groups[0:3]), "injectie": _pair(*groups[5:8])}
    printed = {
        "afname": parse_sign(groups[3]) * to_float(groups[4]),
        "injectie": parse_sign(groups[8]) * to_float(groups[9]),
    }
    units = dict(_UNIT_RE.findall(text))
    for leg, unit in _UNITS.items():
        if not units.get(leg, "").startswith(unit):
            raise ExtractorError(f"{_LABEL}: the {leg} formula is not in {unit}")
    vnr = {_VNR_LEG[leg]: to_float(price) for leg, price in _VNR_RE.findall(text)}
    for leg, (factor, base) in legs.items():
        if leg not in vnr:
            raise ExtractorError(f"{_LABEL}: the {leg} price check not found")
        if abs(factor * vnr[leg] + base - printed[leg]) > 6e-4:
            raise ExtractorError(
                f"{_LABEL}: the {leg} formula does not give the price printed"
            )
    balance = _balancing(text)
    fa, ba = legs["afname"]
    fi, bi = legs["injectie"]
    ea, eb = balance.get("afname", (0.0, 0.0))
    ia, ib = balance.get("injectie", (0.0, 0.0))
    fee = _FEE_RE.search(text)
    if fee is None:
        raise ExtractorError(f"{_LABEL}: monthly fee not found")
    energy = DynamicRates(
        factor=(fa + ea) * vat,
        base=(ba + eb) / 100.0 * vat,
        yearly_fixed_fee=to_float(fee.group(1)) * 12.0,
        quarter_hourly=True,
    )
    factor, base = fi - ia, bi - ib
    sign = "-" if base < 0 else "+"
    injection = InjectionRates(
        factor=factor,
        base=base / 100.0,
        formula=f"{factor:g} x BELPEX {sign} {abs(base):g} c€/kWh".replace(".", ","),
    )
    return energy, injection


def _balancing(text: str) -> dict[str, tuple[float, float]]:
    """Insights' balancing contribution per leg, the factor and the base in
    c€/kWh, or nothing on a card that bills none.

    A card naming one and printing no formula for it is refused, as is one
    that does not say it is printed before VAT, which grossing it assumes.
    """
    named = set(_BALANCING_LABEL_RE.findall(text))
    if not named:
        return {}
    read = {m[0]: _pair(*m[1:]) for m in _BALANCING_RE.findall(text)}
    if named - set(read):
        raise ExtractorError(f"{_LABEL}: balancing contribution formula not found")
    if _BALANCING_EX_VAT_RE.search(text) is None:
        raise ExtractorError(f"{_LABEL}: balancing contribution basis not stated")
    return read


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital-meter network table: data management, capacity, then the
    normal and the exclusive-night rate, transport included in both.

    An area whose row does not read is left out, and ``fetch`` refuses a card
    short of one (``require_every_area``). A table nothing reads from is a
    layout change and raises.
    """
    table = text.replace(_SIDE_LABEL, "")
    out: dict[str, DsoOverlay] = {}
    for label, key in FLUVIUS_CARD_LABELS.items():
        # The labels share all but the area, as on Aspiravi's card.
        row = numeric_row(
            table,
            label,
            4,
            after="Netbeheerder",
            before="Heffingen en toeslagen",
            threshold=0.9,
        )
        if row is None:
            continue
        data, capacity, single, excl_night = (to_float(value) for value in row)
        out[key] = DsoOverlay(
            distribution_single=single / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            transport=0.0,
            data_management_per_year=data,
            capacity_eur_per_kw_year=capacity,
        )
    if not out:
        raise ExtractorError(f"{_LABEL}: no network row read")
    return out


EXTRACTOR = SupplierExtractor(
    id=_SUPPLIER,
    label=_LABEL,
    # 1,32 s to read and parse the slower of the two November 2026 cards on a
    # Raspberry Pi 4, plus 10%.
    sweep_cost_s=1.5,
    contracts=tuple(
        Contract(
            id=product.contract_id,
            label=product.label,
            kind="dynamic",
            regions=frozenset({REGION_FLANDERS}),
        )
        for product in _PRODUCTS
    ),
    fetch=fetch,
)
