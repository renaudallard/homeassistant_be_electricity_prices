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

"""TotalEnergies: the units a card states its formulas in, and the price it
prints for its dynamic ones.

Split out of ``_totalenergies_cards.py``, whose readers call in here once
they have found a formula: a formula a card restates in another unit parses
clean ten or a hundred times out, the way OCTA+'s did in November 2026, so
each is read only under the unit its card has always stated.
"""

from __future__ import annotations

import re

from ._parse import LINE_BREAK, formula_gives, stated_unit, to_float

# Every formula is a row of the card's price table, and the table heads its
# column with the unit: "Prix de l'énergie" over "(c€/kWh)" on the product
# cards, "Prix de l'énergie injection (c€/kWh)" on the feed-in cards, and
# "Energieprijs" or "Prijs voor energie-injectie" over the same "(c€/kWh)" on
# the Dutch editions, the professional cards alike, every card from April to
# November 2026 spelling it so. The heading is found by its words because the
# unit printed nearest a formula is another column's: the yearly fee's
# "(€/an)", and up to September 2026 the green contribution's "(c€/kWh)". The
# words start 151 to 479 characters ahead of a formula, the feed-in rows of
# the September product cards being the farthest, and reach their unit in 36
# to 64; the next heading up, the feed-in cards' "Prix de l'énergie injectée",
# is over 1300 back.
_PRICE_UNIT_RE = re.compile(
    r"(?:Prix\s+de\s+l.énergie|Energieprijs|Prijs\s+voor\s+energie-injectie)"
    r"[^()]{0,120}\(([^()]{0,20})\)"
)
_PRICE_UNIT = "c€/kWh"
_PRICE_UNIT_REACH = 600

# The dynamic cards state the Belpex H their formulas multiply in a sentence
# under the table: "La valeur estimée de l'indice Belpex H pour l'énergie
# prélevée pour l'année à venir est de 173,23 €/MWh en novembre 2026", and for
# the feed-in "... pour l'énergie injectée du Belpex H pour l'année à venir
# est de 145,61€/MWh". The Dutch editions say "... voor de energiekost van de
# Belpex H-index voor het komende jaar is € 173,23 /MWh" and "... voor
# geïnjecteerde energie voor het komende jaar bedraagt 122,20€/MWh". The
# figure is read with its unit, which is compared with the blanks and the
# figure folded out, since the cards space it three ways and the Dutch
# product card puts the euro sign ahead of the figure. Each sentence ends 654
# to 1000 characters after its formula's factor. The monthly indices are
# never stated.
_ESTIMATE = (
    r"[^.\d]{0,100}(?:est\s+de|is|bedraagt)\s+"
    r"((?:€\s*)?([\d.,]+)\s*[^\d/\n]{0,10}/\s*[A-Za-z]+)"
    r"(?:\s+(?:en|in)\s+([a-zéû]+\s+\d{4}))?"
)
_CONSUMPTION_WORDS_RE = re.compile(r"énergie\s+prélevée|energiekost")
_CONSUMPTION_ESTIMATE_RE = re.compile(
    rf"(?:{_CONSUMPTION_WORDS_RE.pattern}){_ESTIMATE}"
)
_INJECTION_ESTIMATE_RE = re.compile(
    rf"(?:énergie\s+injectée|geïnjecteerde\s+energie){_ESTIMATE}"
)
_FIGURE_AND_BLANKS_RE = re.compile(r"[\d.,\s]+")
_INDEX_UNIT = "€/MWh"
_INDEX_UNIT_REACH = 1300

# The table prices each formula at the estimate the sentence states: the row's
# first figure is the formula at that Belpex H to within 0,95% on every card
# from April to November 2026, the October ones being the farthest (21,37
# c/kWh printed in Wallonia against 21,57). A formula restated with its
# heading left as it was misses by far more: one keeping its factor with a
# base a tenth the size, as OCTA+'s did, by 10,7% at the least, and one
# restated tenfold whole by nine times the price. The Brussels and Flemish cards of April 2026 state March's estimate
# under prices worked out at April's, 21 to 35% away, so a sentence naming
# another month than the card's checks nothing. The floor, above half the last
# printed decimal, keeps a feed-in price near zero from failing on its
# rounding.
_ESTIMATE_FIT = 0.03
_ESTIMATE_FLOOR = 0.006
_CARD_MONTH_RE = re.compile(
    rf"Total\s?Energies\s[^\n]{{1,40}}{LINE_BREAK}([a-zéû]+\s+\d{{4}})"
)
_PRINTED_RE = re.compile(r"(?<![\d.,])\d+,\d+")


def unit_refused(text: str, figure: tuple[int, int]) -> str | None:
    """Why the formula at ``figure`` is not read, or None where its column is
    headed in c€/kWh.

    The readers scale every formula on that unit. One restated in another
    parses clean ten or a hundred times out, as OCTA+'s did in November 2026,
    so a unit no card has printed, or none, refuses it.
    """
    unit = stated_unit(text, _PRICE_UNIT_RE, figure, before=_PRICE_UNIT_REACH)
    if unit != _PRICE_UNIT:
        return f"formula in {unit or 'no stated unit'}, not {_PRICE_UNIT}"
    return None


def _estimate_refused(
    text: str,
    figure: tuple[int, int],
    estimate: re.Pattern[str],
    priced: tuple[float | None, float, float],
    *,
    required: bool = True,
) -> str | None:
    """Why the myDynamic formula at ``figure`` is not read, or None.

    ``priced`` is the price the card prints for it and the formula's factor
    and base, all in c€/kWh on that price's VAT basis. The sentence stating
    the Belpex H must say €/MWh, the unit the factor is scaled on, and where
    it is of the card's month the formula at it must give that price back.
    Without ``required`` a card stating no estimate passes.
    """
    stated = stated_unit(text, estimate, figure, after=_INDEX_UNIT_REACH)
    if stated is None:
        return f"index in no stated unit, not {_INDEX_UNIT}" if required else None
    unit = _FIGURE_AND_BLANKS_RE.sub("", stated)
    if unit != _INDEX_UNIT:
        return f"index in {unit}, not {_INDEX_UNIT}"
    # The statement stated_unit has just read.
    match = estimate.search(text, figure[1])
    card = _CARD_MONTH_RE.search(text)
    if (
        match is None
        or match.group(3) is None
        or card is None
        or match.group(3).split() != card.group(1).split()
    ):
        return None
    printed, factor, base = priced
    index = to_float(match.group(2))
    if printed is None:
        return f"formula without a printed price at the Belpex H of {index:g}"
    if formula_gives(
        printed, factor, index, base, tolerance=_ESTIMATE_FIT, floor=_ESTIMATE_FLOOR
    ):
        return None
    return (
        f"formula gives {factor * index + base:.2f} c€/kWh at the Belpex H of "
        f"{index:g}, not the {printed:.2f} printed"
    )


def row_price(text: str, start: int) -> float | None:
    """The first figure of the "Consommation" row ahead of ``start``, the
    single meter's price in c€/kWh, or None where there is none."""
    head = text.rfind("Consommation", 0, start)
    row = _PRINTED_RE.search(text, head, start) if head >= 0 else None
    return to_float(row.group(0)) if row else None


def consumption_refused(
    text: str, figure: tuple[int, int], priced: tuple[float | None, float, float]
) -> str | None:
    """Why myDynamic's consumption formula at ``figure`` is not read, or None.

    The November 2026 card for Brussels, on myDrive's layout, states no
    estimate at all, so a card whose words for one are missing is read as
    that one; one stating it in a way this cannot read is refused.
    """
    stated = _CONSUMPTION_WORDS_RE.search(
        text, figure[1], figure[1] + _INDEX_UNIT_REACH
    )
    return unit_refused(text, figure) or _estimate_refused(
        text, figure, _CONSUMPTION_ESTIMATE_RE, priced, required=stated is not None
    )


def injection_refused(
    text: str, figure: tuple[int, int], priced: tuple[float | None, float, float]
) -> str | None:
    """Why myDynamic's feed-in formula at ``figure`` is not read, or None.
    Every card printing one states its estimate."""
    return unit_refused(text, figure) or _estimate_refused(
        text, figure, _INJECTION_ESTIMATE_RE, priced
    )
