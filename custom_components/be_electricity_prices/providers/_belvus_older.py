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

"""Belvus: the cards of January to August 2026, in the older layout.

Up to August 2026 the cards copied the Energy Together template: numbered
sections, the product and the month on the cover, each formula on one line
with its two estimates, and a network table whose area labels wrap. It came
in three variants: January to March, April, spaced wider, and May to
August, whose cover splits the year over two lines, whose energy fund has
rows of its own and whose Flex Online formula reads "€4MWh". Dynamisch was
first sold in September, so only Flex Online and Smart Plus have such cards.

The cards of January to April 2026 print Fluvius's 2025 network table, and
three of their figures do not match even that one: Limburg's and West's
normal rates, 5,63 and 5,61 c€/kWh where the sheet has 6,80 and 7,47, and
Imewo's exclusive-night rate, 4,91 for 5,21. Every other supplier's card of
those months prints 2026's, so these name 2025 (``Card.network_year``) and
their months are billed on Fluvius's 2026 sheet.

The shapes both layouts are read into, the index a block names and the check
every formula is held to live here too, so ``belvus.py`` reads either layout
the same way.
"""

from __future__ import annotations

import re
from datetime import date
from typing import NamedTuple

from ..const import FLUVIUS_AREA_LABELS_UPPER, REGION_FLANDERS
from ._parse import SIGN_CHARS, parse_sign, regional_tax_overlay, to_float
from ._pdf import NL_MONTHS
from .base import DsoOverlay, ExtractorError, TaxOverlay

_LABEL = "Belvus"
_NUM = r"\d+(?:,\d+)?"


class Leg(NamedTuple):
    """One block's formula, before VAT: the index, the factor and the base in
    EUR/MWh it names, the price in c€/kWh the card prints beside it, and the
    formula as written."""

    indexed_on: str
    factor: float
    base: float
    price: float
    written: str


class Card(NamedTuple):
    """What either layout of a card is read into."""

    month: date
    fee: float
    energy: Leg
    feed_in: Leg
    dsos: dict[str, DsoOverlay]
    taxes: TaxOverlay
    # The year of the network table, where it is not the card's own.
    network_year: int | None = None


# The index a block's printed price is worked out at: "Belpex RLP-M oktober
# 2026 van 181,836 EUR/MWh" on Dynamisch's card, "Belpex RLP-M 10/2026:
# €181,836/MWh" on the others, and "Belpex RLP -M 12/2025: €87,23/MWh" on
# the older cards up to March 2026.
NAMED_INDEX_RE = re.compile(
    r"Belpex\s+(RLP|SPP)[ \t]?-M\s+"
    r"(?:(\d{1,2})/(20\d{2}):\s*€\s*|([a-z]+)\s+(20\d{2})\s+van\s+)"
    rf"({_NUM})\s*(?:EUR)?/MWh",
    re.IGNORECASE,
)


def named_index(block: str, name: str) -> tuple[date, float] | None:
    """The month and the value in EUR/MWh a block names for Belpex
    ``name``-M, the index its printed price is worked out at, or None."""
    for match in NAMED_INDEX_RE.finditer(block):
        kind, number, year, word, word_year, value = match.groups()
        if kind.upper() != name:
            continue
        if number is not None:
            month, at = int(number), int(year)
        elif word.lower() in NL_MONTHS:
            month, at = NL_MONTHS.index(word.lower()) + 1, int(word_year)
        else:
            return None
        if not 1 <= month <= 12:
            return None
        return date(at, month, 1), to_float(value)
    return None


def held_to_price(leg: Leg, at: float, what: str) -> None:
    """Refuse a formula that does not give back, at the index in EUR/MWh its
    block names, the price the card prints beside it to the cent: that pins
    the unit and the scale."""
    if abs((leg.factor * at + leg.base) / 10.0 - leg.price) > 0.006:
        raise ExtractorError(
            f"{_LABEL}: {what} formula does not give the price printed"
        )


# ---- the older layout ------------------------------------------------------

_ENERGY_START = "2. Energieprijsberekening"
_FEED_IN_START = "3. Terugleveringsvergoeding"
_FEED_IN_END = "4. Kosten groene stroom"
# The cover: "Tariefkaart", "Particulier Elektriciteit", "SMART PLUS EL" and
# "Januari 2026", the year split as "202" and "6" in April to July and "EL"
# on a line of its own on April's Smart Plus card.
_COVER_RE = re.compile(
    r"^Tariefkaart\s{1,12}(Particulier|Professioneel) Elektriciteit\s{1,12}"
    r"([A-Z][A-Z ]{0,30}?)\s{1,12}EL\s{1,12}([A-Za-z]+)\s{1,12}"
    r"(2\s{0,6}0\s{0,6}2\s{0,6}\d)\b",
    re.MULTILINE,
)
# Said once, on the cover, of every figure the card does not mark otherwise.
_INCLUSIVE_RE = re.compile(r"Alle prijzen zijn\s{1,8}inclusief BTW")
# "(Belpex_RLP x 1,037) + €12,21/MWh", "(Belpex_SPP x 1) + €-20/MWh" and,
# from May 2026 on Flex Online, "(Belpex_RLP x 1,21) + €4MWh".
_FORMULA_RE = re.compile(
    rf"\(\s*(Belpex_[A-Za-z0-9]+)\s*x\s*({_NUM})\s*\)\s*([{SIGN_CHARS}])"
    rf"\s*€\s*(?:([{SIGN_CHARS}])\s*)?({_NUM})\s*/?MWh"
)
# The two estimates after the formula, "*€c 10,49/kWh **€c 10,95/kWh": the
# VREG's twelve-month one and the price at last month's index, the second
# of which the formula is held to. March's feed-in prints "3,01kWh".
_PRINTED_RE = re.compile(
    rf"[ \t]*\*[ \t]*€c[ \t]*{_NUM}[ \t]*/?kWh[ \t]*\*\*[ \t]*€c[ \t]*"
    rf"([{SIGN_CHARS}]?)({_NUM})[ \t]*/?kWh"
)
_FEE_RE = re.compile(
    rf"^Jaarlijkse abonnementskost[ \t]*€[ \t]*({_NUM})[ \t]*$", re.MULTILINE
)
# "GSC €c 1,18" and "WKK €c 0,42", on two lines or one.
_GSC_RE = re.compile(r"\bGSC[ \t]+€c[ \t]*(\d+,\d+)")
_WKK_RE = re.compile(r"\bWKK[ \t]+€c[ \t]*(\d+,\d+)")
# "Bijzondere accijns op Energie1 €c5,0329/kWh" up to April, "Bijzondere
# accijns (**) €c5,0329" from May.
_EXCISE_RE = re.compile(
    r"^Bijzondere accijns(?: op Energie\d?| \(\*+\))?[ \t]*€c[ \t]*(\d+,\d+)",
    re.MULTILINE,
)
_CONTRIBUTION_RE = re.compile(
    r"^Bijdrage op (?:de )?energie[ \t]*€c[ \t]*(\d+,\d+)",
    re.MULTILINE | re.IGNORECASE,
)
# The household's energy fund, € a month: "Bijzondere accijns Residentieel2
# €0/maand" up to April, "Laagspanning residentieel € 0" from May.
_FUND_RE = re.compile(
    rf"^(?:Bijzondere accijns Residentieel\d?|Laagspanning residentieel)[ \t]*€"
    rf"[ \t]*({_NUM})",
    re.MULTILINE,
)
_DIGITAL_START = "Digitale meter met piekmeting1"
_DIGITAL_END = "1 Digitale meter met piekmeting:"
_CLASSIC_START = "Klassieke meter"
# An area's row: "FLUVIUS" and its name, which wraps as "MIDDEN-" or
# "MIDDENVLAANDEREN" on a line of its own, then its figures. Antwerpen's
# carries the data management fee first, the one figure for every area.
_ROW_RE = re.compile(
    r"FLUVIUS\s+([A-Z][A-Z \t\n-]{2,40}?)\s+((?:\d+,\d+\s+){2,4}\d+,\d+)"
)
_AREAS = {
    re.sub("[^A-Z]", "", label): key for label, key in FLUVIUS_AREA_LABELS_UPPER.items()
}
# Fluvius's 2025 table, on the cards of January to April 2026.
_TABLE_OF_2025 = frozenset(date(2026, month, 1) for month in range(1, 5))


def is_older(text: str) -> bool:
    """Whether a card's text is of the older layout."""
    return _ENERGY_START in text and _FEED_IN_START in text


def blocks(text: str) -> tuple[str, str]:
    """The older layout's energy block and its feed-in block."""
    start = text.find(_ENERGY_START)
    feed_in = text.find(_FEED_IN_START, max(start, 0))
    end = text.find(_FEED_IN_END, max(feed_in, 0))
    if start < 0 or feed_in < 0 or end < 0:
        raise ExtractorError(f"{_LABEL}: price blocks not found")
    return text[start:feed_in], text[feed_in:end]


def read(text: str, file: str, indices: tuple[str, str]) -> Card:
    """An older card of the product whose file name is ``file``, its legs on
    ``indices``, energy then feed-in."""
    month = _card_month(text, file)
    if _INCLUSIVE_RE.search(text) is None:
        raise ExtractorError(f"{_LABEL}: card does not state its prices with VAT")
    energy_block, feed_in_block = blocks(text)
    energy = _leg(energy_block, indices[0], "RLP", "energy")
    feed_in = _leg(feed_in_block, indices[1], "SPP", "feed-in")
    # One formula in each block and none anywhere else.
    if len(_FORMULA_RE.findall(text)) != 2:
        raise ExtractorError(f"{_LABEL}: the card prints more than one energy formula")
    fee = _FEE_RE.search(text)
    if fee is None:
        raise ExtractorError(f"{_LABEL}: yearly fee not found")
    return Card(
        month=month,
        fee=to_float(fee.group(1)),
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
        network_year=2025 if month in _TABLE_OF_2025 else None,
    )


def _card_month(text: str, file: str) -> date:
    """The month of the card, once its cover shows it to be the product's
    own residential card."""
    covers = {
        (segment.lower(), name, word.lower(), re.sub(r"\s", "", year))
        for segment, name, word, year in _COVER_RE.findall(text)
    }
    if len(covers) != 1:
        raise ExtractorError(f"{_LABEL}: card cover not found")
    segment, name, word, year = covers.pop()
    if segment != "particulier":
        raise ExtractorError(f"{_LABEL}: card is for professionals")
    if name.replace(" ", "").casefold() != file.casefold():
        raise ExtractorError(f"{_LABEL}: card is for {name}, not {file}")
    if word not in NL_MONTHS:
        raise ExtractorError(f"{_LABEL}: unknown month {word!r}")
    return date(int(year), NL_MONTHS.index(word) + 1, 1)


def _leg(block: str, index: str, named: str, what: str) -> Leg:
    match = _FORMULA_RE.search(block)
    if match is None:
        raise ExtractorError(f"{_LABEL}: {what} formula not found")
    found, factor, sign, inner, base = match.groups()
    if found != index:
        raise ExtractorError(f"{_LABEL}: {what} formula is on {found}, not {index}")
    printed = _PRINTED_RE.match(block, match.end())
    at = named_index(block, named)
    if printed is None or at is None:
        raise ExtractorError(f"{_LABEL}: {what} price check not found")
    leg = Leg(
        indexed_on=found,
        factor=to_float(factor),
        base=parse_sign(sign) * parse_sign(inner or "") * to_float(base),
        price=parse_sign(printed.group(1)) * to_float(printed.group(2)),
        written=" ".join(match.group(0).split()),
    )
    held_to_price(leg, at[1], what)
    return leg


def _rows(
    table: str, figures: tuple[int, int]
) -> tuple[dict[str, list[float]], float | None]:
    """Each area's figures in a table, and the data management fee its first
    row carries before them. A row of another count than ``figures``, the
    count without and with the fee, is left out."""
    rows: dict[str, list[float]] = {}
    fee: float | None = None
    for match in _ROW_RE.finditer(table):
        key = _AREAS.get(re.sub("[^A-Z]", "", match.group(1)))
        values = [to_float(value) for value in match.group(2).split()]
        if key is None or key in rows or len(values) not in figures:
            continue
        if len(values) == figures[1]:
            fee, values = values[0], values[1:]
        rows[key] = values
    return rows, fee


def _dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital meter's network table, and the classic meter's prosumer
    rate, its first figure after the fee."""
    start = text.find(_DIGITAL_START)
    end = text.find(_DIGITAL_END, max(start, 0))
    classic = text.find(_CLASSIC_START, max(end, 0))
    if start < 0 or end < 0 or classic < 0:
        raise ExtractorError(f"{_LABEL}: network table not found")
    digital, fee = _rows(text[start:end], (3, 4))
    prosumer, _fee = _rows(text[classic:], (4, 5))
    if fee is None or not digital:
        raise ExtractorError(f"{_LABEL}: no network row read")
    out: dict[str, DsoOverlay] = {}
    for key, (capacity, single, night) in digital.items():
        out[key] = DsoOverlay(
            distribution_single=single / 100.0,
            distribution_exclusive_night=night / 100.0,
            # One rate holding transport, as on every Flemish card.
            transport=0.0,
            data_management_per_year=fee,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=(prosumer[key][0] if key in prosumer else None),
        )
    return out
