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

"""Cociter: the BELIX the next card prints for a month, which settles it."""

from __future__ import annotations

import re
from datetime import date

from ._parse import stated_unit, to_float
from ._pdf import FR_MONTHS

# The BELIX column of the variable and trihoraire cards, and the month it
# belongs to: "Compteur monohoraire (0,075 x BELIX + 5) + 6% TVA 156,41
# 17,7346 c€/kWh", then "* L'indice BELIX indiqué est celui du mois
# précédent, dans ce cas-ci septembre 2026." Every row prints the same value.
# The gap to the bracket is one run, which stops at the next "x BELIX". Spelt
# as blanks and then the rest, a run of blanks was split between the two in
# every way; run past the next "x BELIX", whose own gap reaches the same
# bracket, a row of them was read again from each.
_BELIX_VALUE_RE = re.compile(
    r"x\s*BELIX(?:(?!x\s*BELIX)[^)])*\)\s*\+\s*\d+\s*%\s*TVA\s+(\d+(?:,\d+)?)\s"
)
_BELIX_MONTH_RE = re.compile(r"dans\s+ce\s+cas-ci\s+(\w+)\s+(\d{4})", re.IGNORECASE)
# The column's unit, which the same footnote goes on to state, "Il est exprimé
# en €/MWh.", on every variable and trihoraire card since September 2024. It
# ends 350 to 406 characters past the first row's BELIX, and it is the only
# such sentence on the card; note (7)'s "exprimées en €/MWh" is thousands of
# characters further. The unit is compared with its whitespace
# folded, since the notes print it "€/ MWh" too. The run to the full stop is
# bounded, so a sentence with none is not read to the end of the card again
# from each copy.
_BELIX_UNIT_RE = re.compile(r"Il\s+est\s+exprimé\s+en\s+([^.\n]{1,20})\.")
_BELIX_UNIT_REACH = 600
# The BELIX the cards have printed runs from 61,07 to 174,19 €/MWh, September
# 2024 to November 2026. Restated in c€/kWh with the footnote left as it was,
# it would print a tenth of that, and factors ten times their own would still
# give back every printed rate, so only the figure's size gives it away. One
# under half the lowest printed is not taken for a €/MWh figure.
_BELIX_FLOOR = 30.0


def belix_sized(belix: float) -> bool:
    """Whether ``belix``, as a row prints it, is the size of a BELIX in
    €/MWh rather than in c€/kWh."""
    return belix >= _BELIX_FLOOR


def belix_in_eur_per_mwh(text: str) -> bool:
    """Whether the card states its BELIX in €/MWh, as every card has, and
    prints one of that size.

    The settlement reads the figure as EUR/MWh and the formula readers scale
    their factors for it, so a BELIX restated in another unit, or no longer
    stated in any, is read by neither.
    """
    value = _BELIX_VALUE_RE.search(text)
    if value is None or not belix_sized(to_float(value.group(1))):
        return False
    unit = stated_unit(text, _BELIX_UNIT_RE, value.span(1), after=_BELIX_UNIT_REACH)
    return unit is not None and "".join(unit.split()) == "€/MWh"


def published_belix(text: str) -> tuple[date, float] | None:
    """The BELIX a card prints and the month it names, as ``(month, EUR/kWh)``.

    The card for a month prints the BELIX of the month before, which is that
    month's settled index: note (7) bills the delivery month on its own BELIX.
    A BELIX the card no longer states in €/MWh settles nothing.
    """
    value = _BELIX_VALUE_RE.search(text)
    month = _BELIX_MONTH_RE.search(text)
    if value is None or month is None or not belix_in_eur_per_mwh(text):
        return None
    name = month.group(1).lower()
    if name not in FR_MONTHS:
        return None
    return (
        date(int(month.group(2)), FR_MONTHS.index(name) + 1, 1),
        to_float(value.group(1)) / 1000.0,
    )
