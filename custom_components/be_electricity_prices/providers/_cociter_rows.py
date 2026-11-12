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

"""Cociter's per-meter rows: the indicative rate, the BELIX formula and the
price ceiling the row of each meter prints, and the units the card states
for them.

Split out of ``cociter.py``, as ``_cociter_settle.py`` was, so that module
stays under a thousand lines. These are the leaf readers ``_extract_energy``
and ``_impact_energy`` call.
"""

from __future__ import annotations

import re

from ._cociter_settle import belix_in_eur_per_mwh, belix_sized
from ._parse import SIGN_CHARS, formula_gives, parse_sign, stated_unit, to_float
from .base import ExtractorError

# One BELIX row per meter. The label anchors are mandatory: an optional one
# lets the pattern wander onto the consumption formula of the July 2026
# dynamic re-upload, which is the trap the injection pattern's own mandatory
# "Compteur" label already exists for.
_BELIX_ROW_TAIL = (
    rf"\(([\d,]+)\s*x\s*BELIX\s*"
    rf"([{SIGN_CHARS}])\s*([\d,]+)\)\s*\+\s*(\d+)\s*%\s*TVA"
)


def _row(label: str, tail: str) -> str:
    """A pattern for ``label`` and the first ``tail`` after it on its line.

    The gap between them stops at the next ``label`` on the line. A ``tail``
    before that one is found as before; past it, the next ``label`` finds the
    same first ``tail``, so the groups are those an open gap read and only the
    match starts later. Open to the end of the line, a line of labels was read
    to its end again from each. Where a tail opens on a figure, the tail
    itself reads it from the start of its digits.
    """
    return rf"{label}(?:(?!{label})[^\n])*?{tail}"


_BELIX_BANDS: tuple[tuple[str, str], ...] = (
    ("peak", r"Heures pleines"),
    ("offpeak", r"Heures creuses"),
    ("exclusive_night", r"Compteur exclusif nuit"),
)


# The unit the energy formulas give, from the box the formula table hangs
# under: "Coût de l’énergie proportionnel à la consommation (en c€/kWh)", one
# word or two to a line, on every variable card since September 2024, every
# dynamic one since March 2026 and every trihoraire one. Its first word
# starts 461 characters ahead of the dynamic card's formula and 528 to 819
# ahead of the variable and trihoraire rows', the exclusive night row the
# farthest. The renewables contribution's own "c€/kWh" sits between, so the
# box is matched by its words; no other statement of them is on the card.
_FORMULA_UNIT_RE = re.compile(
    r"Coût\s+de\s+l['‘’ʼ]\s*énergie\s+proportionnel\s+à\s+la\s+consommation"
    r"\s*\(en\s+([^()]*)\)"
)
_FORMULA_UNIT_REACH = 950
_CENTS = "c€/kWh"
# After its formula a row prints the BELIX the card worked it out at (group
# 1), then the price that gives (group 2): "(0,075 x BELIX + 5) + 6% TVA
# 174,19 19,1481 c€/kWh 26,5 c€/kWh", the last figure being the "Prix
# maximum" column of the later cards. They are the row's first two
# figures with nothing but blanks between, whatever the formula and its VAT
# clause say, so a row whose formula no longer reads keeps its price.
_PRICE_TAIL = r"(?<![\d,])(\d+(?:,\d+)?)[^\S\n]+(\d+,\d+)"
# The same two figures right after the formula's "+ N% TVA".
_ROW_PRICE_RE = re.compile(rf"[^\S\n]+{_PRICE_TAIL}")
# The price's unit, one space after it on every row of every archived card. The reach ends before the "Prix maximum" column's own unit.
_PRICE_UNIT_RE = re.compile(r"[^\S\n]*(\S+)")
_PRICE_UNIT_REACH = 12
# The price a row prints is its formula at the BELIX beside it, rounded to
# four decimals. Over every archived row the two differ by at most 4,7e-6 of
# the price (the exclusive night row of September 2024), so a share four
# times that is kept, and below 7,5 c€/kWh a floor of three times the half
# of that last decimal, clear of the rounding tie. A formula read ten times
# out misses by far more.
_PRICE_TOLERANCE = 2e-5
_PRICE_FLOOR = 1.5e-4


def _formula_unit(text: str, formula: re.Match[str]) -> str | None:
    """The unit the box above the table states for the formula ``formula``
    holds from group 1 on."""
    return stated_unit(
        text,
        _FORMULA_UNIT_RE,
        (formula.start(1), formula.end()),
        before=_FORMULA_UNIT_REACH,
    )


def _priced(text: str, price: re.Match[str]) -> tuple[float, float, str | None]:
    """The BELIX and price ``price`` holds in groups 1 and 2, as printed, and
    the unit printed after the price."""
    unit = stated_unit(text, _PRICE_UNIT_RE, price.span(2), after=_PRICE_UNIT_REACH)
    return to_float(price.group(1)), to_float(price.group(2)), unit


def _optional_indicative(text: str, label: str) -> float | None:
    """The price the row ``label`` prints at its BELIX, in TVAC EUR/kWh, or
    None where the row prints none, or prints it in another unit than
    c€/kWh.

    The price is the figure after the BELIX whatever follows it. Read as the
    row's first figure in c€/kWh, a price restated in another unit was
    passed over for the "Prix maximum" column, and the cap billed.
    """
    row = re.search(_row(label, _PRICE_TAIL), text)
    if row is None:
        return None
    _, price, unit = _priced(text, row)
    return price / 100.0 if unit == _CENTS else None


def _indicative(text: str, label: str, reader: str) -> float:
    """The row ``label``'s price as :func:`_optional_indicative` reads it, for
    a row the card cannot do without: raised as missing, or as printed in
    another unit than c€/kWh."""
    row = re.search(_row(label, _PRICE_TAIL), text)
    if row is None:
        raise ExtractorError(f"could not parse Cociter {reader} indicative rate")
    _, price, unit = _priced(text, row)
    if unit != _CENTS:
        raise ExtractorError(
            f"Cociter {reader} indicative rate in {unit or 'no stated unit'}, "
            f"expected {_CENTS}"
        )
    return price / 100.0


def _coefficients(text: str, row: re.Match[str]) -> tuple[float, float] | None:
    """The ``(factor, base)`` of the BELIX formula ``row`` holds in groups 1
    to 4, onto a spot in EUR/kWh, or None where the card no longer states the
    units that scale is for, or the formula does not give the price its row
    prints.

    The coefficients give c€/kWh on a BELIX in EUR/MWh, so the factor takes a
    x10 and the base a /100, and the "+ N% TVA" printed outside the parens
    lands on both. That holds while the box above the table and the price the
    row gives are in c€/kWh and the BELIX in €/MWh, as on every card so far.
    A formula restated with those statements left as they were still has to
    give back the price at the BELIX beside it, and that BELIX has to be the
    size of one in €/MWh, since a row restating it along with its factor
    gives its price back too. A row failing any of these is dropped as an
    unreadable one is and its printed price bills: read on this scale it
    would bill ten or a hundred times out.
    """
    price = _ROW_PRICE_RE.match(text, row.end())
    if (
        price is None
        or _formula_unit(text, row) != _CENTS
        or not belix_in_eur_per_mwh(text)
    ):
        return None
    belix, printed, unit = _priced(text, price)
    if not belix_sized(belix):
        return None
    vat_mult = 1.0 + to_float(row.group(4)) / 100.0
    factor = to_float(row.group(1)) * vat_mult
    base = parse_sign(row.group(2)) * to_float(row.group(3)) * vat_mult
    if unit != _CENTS or not formula_gives(
        printed, factor, belix, base, tolerance=_PRICE_TOLERANCE, floor=_PRICE_FLOOR
    ):
        return None
    return factor * 10.0, base / 100.0


def _belix_band_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """Per-meter BELIX ``(factor, base)`` on a Cociter variable card, read as
    the mono row's are (:func:`_coefficients`)."""
    out: dict[str, tuple[float, float]] = {}
    for key, label in _BELIX_BANDS:
        match = re.search(_row(label, _BELIX_ROW_TAIL), text)
        coefficients = None if match is None else _coefficients(text, match)
        if coefficients is not None:
            out[key] = coefficients
    return out


# The "Prix maximum" column, printed on the same row as the indicative and
# in the same unit. Both figures end in "c€/kWh", so the ceiling is the
# SECOND one on the row.
_CEILING_TAIL = r"(?<!\d)\d+,\d+\s*c€/kWh[^\n]*?(?<!\d)(\d+,\d+)\s*c€/kWh"

_VARIABLE_CEILING_ROWS: tuple[tuple[str, str], ...] = (
    ("single", r"Compteur monohoraire"),
    ("peak", r"Heures pleines"),
    ("offpeak", r"Heures creuses"),
    ("exclusive_night", r"Compteur exclusif nuit"),
)


def _price_ceilings(
    text: str, rows: tuple[tuple[str, str], ...]
) -> dict[str, float | None]:
    """The card's "Prix maximum" column, per row, as TVAC EUR/kWh.

    Note (8): "COCITER a decide de plafonner le prix de fourniture de
    l'energie [...] si le Belix venait a augmenter au point que
    l'application du prix variable (indexe) donne une valeur superieure au
    prix maximum, c'est le prix maximum qui s'applique." The cap is on the
    supply price only; network, taxes and surcharges stay due in full, which
    is exactly what ``pricing`` already does with a ceiling.

    A row without the column yields None rather than 0.0, so a card that
    stops publishing the cap goes back to uncapped instead of pricing every
    kWh at zero. The dynamic card prints no cap at all and reaches this with
    no matching row.
    """
    out: dict[str, float | None] = {}
    for key, label in rows:
        match = re.search(_row(label, _CEILING_TAIL), text)
        out[key] = to_float(match.group(1)) / 100.0 if match else None
    return out
