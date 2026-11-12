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

"""Energy Knights formula rows and the units the cards state them in.

Split out of ``energyknights.py``, as ``_energyknights_overlays.py`` was. The
row patterns moved unchanged; ``energyknights.py`` keeps the readers that
turn a row into rates.
"""

from __future__ import annotations

import re

from ._energyknights_overlays import _H, _NUM
from ._parse import SIGN_CHARS, stated_unit

# The four consumption rows and the injection row all print as
#   <label> (c€/kWh) <indicative> <index> * <coefficient> <sign> <offset>
# with the injection row carrying its VAT-exemption footnote between the unit
# and the number. Each register is anchored on its own label rather than read
# positionally: "Verbruik nacht" and "Verbruik exclusief nacht" are distinct
# rows carrying distinct coefficients on this supplier's variable card, and
# every card published before December 2025 printed all four identically, so
# a parser that read one row and fanned it out would have passed every fixture
# it was likely to be given.
# (snapshot register, the card's own wording, the row anchor). The wording is
# carried so a failure names the row a maintainer has to go and look at rather
# than this module's internal register key.
_ROW_LABELS: tuple[tuple[str, str, str], ...] = (
    ("single", "enkelvoudig", r"Verbruik\s+enkelvoudig"),
    ("peak", "dag", r"Verbruik\s+dag"),
    ("offpeak", "nacht", r"Verbruik\s+nacht"),
    ("exclusive_night", "exclusief nacht", r"Verbruik\s+exclusief\s+nacht"),
)
_FORMULA_TAIL = (
    rf"\){_H}*(?:\(\d\){_H}*)?{_NUM}{_H}+"
    rf"(Belpex[A-Za-z_0-9]*){_H}*\*{_H}*{_NUM}{_H}*([{SIGN_CHARS}]){_H}*{_NUM}"
)


def _formula_row(head: str) -> re.Pattern[str]:
    """The row ``head`` opens, ``head`` running up to the opening bracket of
    the row's unit, which is read as the green row's: up to the next ``head``
    at most."""
    return re.compile(rf"{head}(?:(?!{head})[^)])*{_FORMULA_TAIL}", re.IGNORECASE)


_ROW_RES: dict[str, re.Pattern[str]] = {
    slot: _formula_row(rf"{anchor}{_H}*\(c") for slot, _wording, anchor in _ROW_LABELS
}
_ROW_CARD_LABELS: dict[str, str] = {
    slot: wording for slot, wording, _anchor in _ROW_LABELS
}
# 'optie "solar" (c€/kWh) (1) 5,85 Belpex_15 * 1 - 12'. The quotes are
# straight on every card seen, but a renderer that curls them would otherwise
# take the whole injection leg offline. Each quote and the blanks beside it
# are one optional piece: spelt as runs of blanks either side of an optional
# quote, a run of blanks was split between the two in every way.
_INJECTION_RE = _formula_row(rf"optie\s*(?:[\"“”]\s*)?solar\s*(?:[\"“”]{_H}*)?\(c")

# The unit of every formula, stated once beside the energy rows as
# "(*)Tariefformule in EUR/MWh excl BTW", and that of the index its coefficient
# multiplies, stated with the VREG price each indicative is worked out at:
# "jaarprijs voor afname. (2026-11 = 180,03 €/MWh)" among the consumption rows
# and "voor teruglevering. (2026-11 = 93,78 €/MWh)" under the solar one. All
# 142 cards on file, January 2025 to November 2026, spell them "EUR/MWh" and
# "€/MWh". Restated in c€/kWh a formula would parse clean with every offset
# ten times too small, as OCTA+'s did in November 2026, so one stated in
# anything else, or in nothing, is taken for one that does not read. The
# price's figure is read whole and never given back, so a price printed
# without its unit states none rather than the figure's last digits, and the
# figure is read once per statement.
_FORMULA_UNIT_RE = re.compile(r"Tariefformule\s+in\s+(\S+)", re.IGNORECASE)
_VREG_PRICE = r"\.?\s*+\(\d{4}-\d{2}\s*=\s*(?>\d+(?:[.,]\d+)?)\s*+([^\s)]++)\)"
_OFFTAKE_INDEX_RE = re.compile(r"voor\s+afname" + _VREG_PRICE, re.IGNORECASE)
_FEED_IN_INDEX_RE = re.compile(r"voor\s+teruglevering" + _VREG_PRICE, re.IGNORECASE)
# Per leg: what each statement gives, its pattern, the spelling the cards
# print, and how far before and after the formula it is looked for. On those
# cards the column statement sits 25 to 28 characters after the mono formula,
# 69 to 273 before the other three and 398 to 511 before the solar one; the
# consumption price 48 to 235 after a formula, or 76 to 78 before the
# exclusief nacht one, and the feed-in price 121 to 122 after the solar one.
_Units = tuple[tuple[str, re.Pattern[str], str, int, int], ...]
_OFFTAKE_UNITS: _Units = (
    ("unit", _FORMULA_UNIT_RE, "EUR/MWh", 340, 40),
    ("index unit", _OFFTAKE_INDEX_RE, "€/MWh", 100, 300),
)
_FEED_IN_UNITS: _Units = (
    ("unit", _FORMULA_UNIT_RE, "EUR/MWh", 640, 0),
    ("index unit", _FEED_IN_INDEX_RE, "€/MWh", 0, 160),
)


def _misstated_unit(text: str, match: re.Match[str], units: _Units) -> str | None:
    """How the units the card states for the formula ``match`` holds, a match
    of one of the row patterns above, differ from the ones its cards have
    always stated, or None where they do not."""
    formula = (match.start(2), match.end(5))
    for what, pattern, expected, before, after in units:
        unit = stated_unit(text, pattern, formula, before=before, after=after)
        if unit != expected:
            return f"{what} is {unit!r}, expected {expected!r}"
    return None
