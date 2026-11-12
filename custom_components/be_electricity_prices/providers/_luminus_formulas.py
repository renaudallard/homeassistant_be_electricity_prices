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


"""Luminus: whether a card's formulas still give the prices it prints.

No formula block states the unit of its result, so a formula is checked
against the price the card works out from it at an index it names, and a
printed price against the unit its row states.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from ._luminus_overlays import _NUM
from ._parse import SIGN_CHARS, formula_gives, parse_sign, stated_unit, to_float
from .base import ExtractorError

# No formula block states the unit of its result, only "(HTVA)", so a formula
# restated on another scale would read clean. What every card prints is the
# price the formula gives at an index the card names: its rates beside
# "Index (HTVA) : Belpex = 174,19 €/MWh", and Dynamic's estimates at the
# Belpex H the sentence after each block expects. A formula is kept only where
# it gives that price back. On every archived card, January 2024 to November
# 2026, the energy formulas do within 0,1 % and the feed-in ones within 0,5 %,
# the farthest a feed-in printed at 0,99 c/kWh; read ten times off, a factor
# misses by 40 % or more. A feed-in crossing zero in a month of low prices is
# printed too close to it for any share, so a gap of 0,01 c/kWh, the hundredth
# the cards print to, is allowed outright. The cards round the coefficients
# and the index they print as well ("Les montants mentionnes sont arrondis"):
# near zero the four roundings come to 0,0072 c/kWh at most, and the floor
# stays clear of the 0,005 a figure rounded to its hundredth can be out by.
_FORMULA_TOLERANCE = 0.02
_FORMULA_FLOOR = 0.0001


def _gives_printed(
    printed: float | None, pair: tuple[float, float], index: float | None
) -> bool:
    """Whether ``pair``, a ``(factor, base)`` in EUR/kWh on an index in
    EUR/kWh, gives back ``printed`` at ``index``, both as the card prints
    them. A card printing no price or no index to check it against answers
    False: every card on file prints both."""
    if printed is None or index is None:
        return False
    factor, base = pair
    return formula_gives(
        printed,
        factor,
        index,
        base,
        tolerance=_FORMULA_TOLERANCE,
        floor=_FORMULA_FLOOR,
    )


def _bands_give_printed(
    coefs: dict[str, tuple[float, float]],
    printed: Mapping[str, float | None],
    index: float | None,
) -> bool:
    """Whether the formula of every band in ``coefs`` gives back the rate the
    card prints for that band at ``index``."""
    return all(
        _gives_printed(printed.get(key), pair, index) for key, pair in coefs.items()
    )


# The Belpex H a dynamic block's estimate is worked out at, in the sentence
# after the block, the feed-in's in the same words: "le prix indique pour
# l'energie represente une estimation annuelle basee sur la valeur attendue de
# Belpex H pour l'annee a venir (173,23 €/MWh)". On every archived card it
# ends 183 to 203 characters after its block, and the next one 1.245 after.
_EXPECTED_INDEX_RE = re.compile(
    r"valeur\s+attendue\s+de\s+Belpex\s+H\s+pour\s+l['\u2019]ann[ée]e\s+[àa]\s+venir"
    r"\s*\((\d+,\d+)\s*€/MWh\)"
)
_EXPECTED_INDEX_REACH = 400


def _expected_index(text: str, block: re.Pattern[str]) -> float | None:
    """The Belpex H in EUR/kWh a dynamic card works the estimate of ``block``
    out at, from the sentence after the block, or None where it names none."""
    found = block.search(text)
    if found is None:
        return None
    stated = _EXPECTED_INDEX_RE.search(
        text, found.end(), found.end() + _EXPECTED_INDEX_REACH
    )
    return None if stated is None else to_float(stated.group(1)) / 1000.0


def _signed(name: str) -> str:
    """A feed-in figure with any sign printed ahead of it, in group ``name``
    and its sign in ``name``_sign. No card on file prints a negative one, but
    a feed-in formula under zero in a month of low prices gives one, which is
    compared with the formula as the negative figure it is, and credited as
    one where it is the rate in force, the feed-in row's. The sign and the
    blanks after it are one optional group, so a match starts only at a sign
    or a digit, and the blanks ahead of it are matched once, by the pattern
    around it."""
    return rf"(?:(?P<{name}_sign>[{SIGN_CHARS}])[ \t]*+)?(?P<{name}>{_NUM})"


def _signed_cents(match: re.Match[str] | None, name: str) -> float | None:
    """The c/kWh figure ``match`` holds in group ``name``, with the sign the
    card prints ahead of it, in EUR/kWh, or None where it holds none."""
    if match is None or match.group(name) is None:
        return None
    sign = parse_sign(match.group(f"{name}_sign") or "")
    return sign * to_float(match.group(name)) / 100.0


# The unit every price row of the cards on file states, January 2024 to
# November 2026: no other has been printed beside an energy or feed-in figure.
_CARD_UNIT = "c€/kWh"


def _require_unit(
    text: str, pattern: re.Pattern[str], figure: tuple[int, int], reach: int, row: str
) -> None:
    """Refuse a card whose ``row``, its first figure at ``figure``, is not
    stated in c€/kWh by the last statement ``pattern`` finds within ``reach``
    characters ahead of it. A price row restated in another unit together
    with its formula gives its printed price back, so the formula check
    cannot tell it apart, and a fixed card's has no formula at all."""
    unit = stated_unit(text, pattern, figure, before=reach)
    if unit != _CARD_UNIT:
        raise ExtractorError(
            f"Luminus: the {row} is stated in {unit or 'no unit'}, not {_CARD_UNIT}"
        )


# The energy row's unit, in its label on the cards until October 2026,
# "Énergie fournie (c€/kWh) 23,21", and in the heading above it on those
# reissued on 29 October 2026, "Prix de l’énergie (c€/kWh)2" then the label.
# It starts 26 to 76 characters ahead of the first figure on every card on
# file and is the first statement of either kind on the card: the table
# heads the card, and the annual estimate's, in lower case, follows it.
_ENERGY_UNIT_RE = re.compile(
    r"(?:Énergie\s+fournie|Prix\s+de\s+l['\u2019]énergie)\s*\(([^()]{0,20})\)"
)
_ENERGY_UNIT_REACH = 150


def _require_energy_unit(text: str, row: Sequence[str]) -> None:
    """Refuse an energy row, ``row`` its figures as printed, stated in
    another unit than c€/kWh. The row is where its figures first stand
    together on the card, which on every card on file is the row itself."""
    figures = re.compile(
        r"(?<![\d.,])" + r"[ \t]+".join(map(re.escape, row)) + r"(?![\d.,])"
    )
    found = figures.search(text)
    if found is None:
        raise ExtractorError("Luminus: the energy row's figures are not on one line")
    _require_unit(text, _ENERGY_UNIT_RE, found.span(), _ENERGY_UNIT_REACH, "energy row")
