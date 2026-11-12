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

"""The Belpex formulas Bolt prints on its electricity cards, the Tarif Impact
rows beside them, and the unit the card states each in."""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, stated_unit

# Bolt's variable card prints its tariff formula for both consumption and
# injection as "Belpex * <factor> <sign> <base>" in EUR/MWh (HTVA), one row per
# meter type. The dynamic contract applies the same coefficients to the live
# quarter-hourly Belpex spot. The consumption formula is the first match; the
# injection formula is the first match that differs from it (Bolt lists all
# consumption rows, then all injection rows).
_BELPEX_FORMULA_RE = re.compile(
    rf"Belpex\s*\*\s*([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)"
)
# The Walloon "Tarif Impact (Wallonie)" block, one row per CWaPE band:
#   "Eco consommation 9,91 65,59 Belpex * 1,168 + 16,90"
# printed price, that band's own quarterly index, then the shared formula.
_IMPACT_ROW_RE = re.compile(
    rf"(Eco|Medium|Pic)\s+consommation\s+([\d,]+)\s+([\d,]+)\s+"
    rf"Belpex\s*\*\s*([\d,]+)\s*([{SIGN_CHARS}])\s*([\d,]+)",
    re.IGNORECASE,
)
# The same block's feed-in rows, "Eco injection 4,41 58,97 Belpex * 0,94 -
# 11,33": printed figure and that band's SPP-weighted index, the formula
# beside them the leg's own.
_IMPACT_INJECTION_ROW_RE = re.compile(
    r"(Eco|Medium|Pic)\s+injection\s+(-?[\d,]+)\s+(-?[\d,]+)\s+Belpex\s*\*",
    re.IGNORECASE,
)
# The unit of every formula, which the card states once, in the heading of the
# column the formulas print in: "Type de compteur Belpex Q3 2026 Formule
# tarifaire (€/MWh, HTVA)" from April 2026, "(€/MWh - HTVA)" on the cards
# before it, back to January 2025, and "(€/MWh, HTVA)" over the Tarif Impact
# block's formula column. No card on file spaces either another way. Each row
# prints its index between the heading and the formula, "Simple 139,36 €/MWh
# Belpex * 1,168 + 16,90", so only a statement of the HTVA basis counts, never
# the nearest unit. A formula restated in c€/kWh, as OCTA+ restated its own in
# November 2026, keeps its factor and prints a base a tenth the size: read on
# this scale it passes check_quarter_table and the live check's bounds, and the
# feed-in figures are then worked out from it.
_FORMULA_UNIT_RE = re.compile(r"\((c?€/[kM]Wh)(?:, | - )HTVA\)")
# Every formula read on file is 109 to 616 characters after its heading, the
# feed-in rows the farthest, and the Tarif Impact block's are 1.326 or more
# after the heading of the table above it. The reach stays short of that, so a
# block whose own heading no longer reads cannot pass on the table's.
_FORMULA_REACH = 900
# The unit of the index each Tarif Impact row prints, which heads that column:
# "(€/MWh)" on every card printing the block, from April 2026. Each index is 61
# to 791 characters after it, the Pic injection row the farthest. The price
# column's "(c€/kWh)" heading prints farther back on every card, so it is taken,
# and refused, only where the index column states nothing, and no other
# "(€/MWh)" lies within 3.203 characters before a row.
_INDEX_UNIT_RE = re.compile(r"\((c?€/[kM]Wh)\)")
_INDEX_REACH = 1200
# The one unit either has been stated in.
_MWH = "€/MWh"


def _formula_in_mwh(text: str, formula: re.Match[str], group: int = 1) -> bool:
    """Whether the heading over ``formula``, its factor in ``group``, states it
    in €/MWh HTVA (``_FORMULA_UNIT_RE``)."""
    span = formula.span(group)
    return stated_unit(text, _FORMULA_UNIT_RE, span, before=_FORMULA_REACH) == _MWH


def _impact_rows(text: str, pattern: re.Pattern[str]) -> list[re.Match[str]]:
    """The Tarif Impact rows ``pattern`` matches, its band in group 1 and its
    index in group 3, whose index column is headed in €/MWh
    (``_INDEX_UNIT_RE``). A row under another unit is left out, as one the
    pattern does not match is."""
    return [
        m
        for m in pattern.finditer(text)
        if stated_unit(text, _INDEX_UNIT_RE, m.span(3), before=_INDEX_REACH) == _MWH
    ]
