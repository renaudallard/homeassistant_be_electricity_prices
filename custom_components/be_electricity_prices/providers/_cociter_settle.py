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

"""Cociter: settling a month on the BELIX the next card prints for it."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

from ._parse import to_float
from ._pdf import FR_MONTHS
from ._rates import ImpactRates, VariableRates

# The BELIX column of the variable and trihoraire cards, and the month it
# belongs to: "Compteur monohoraire (0,075 x BELIX + 5) + 6% TVA 156,41
# 17,7346 c€/kWh", then "* L'indice BELIX indiqué est celui du mois
# précédent, dans ce cas-ci septembre 2026." Every row prints the same value.
_BELIX_VALUE_RE = re.compile(
    r"x\s*BELIX\s*[^)]*\)\s*\+\s*\d+\s*%\s*TVA\s+(\d+(?:,\d+)?)\s"
)
_BELIX_MONTH_RE = re.compile(r"dans\s+ce\s+cas-ci\s+(\w+)\s+(\d{4})", re.IGNORECASE)


def published_belix(text: str) -> tuple[date, float] | None:
    """The BELIX a card prints and the month it names, as ``(month, EUR/kWh)``.

    The card for a month prints the BELIX of the month before, which is that
    month's settled index: note (7) bills the delivery month on its own BELIX.
    """
    value = _BELIX_VALUE_RE.search(text)
    month = _BELIX_MONTH_RE.search(text)
    if value is None or month is None:
        return None
    name = month.group(1).lower()
    if name not in FR_MONTHS:
        return None
    return (
        date(int(month.group(2)), FR_MONTHS.index(name) + 1, 1),
        to_float(value.group(1)) / 1000.0,
    )


def _at(
    factor: float | None, base: float | None, printed: float, index: float
) -> float:
    """A printed rate rebuilt through its formula, or kept with none behind it."""
    return printed if factor is None or base is None else factor * index + base


def _at_optional(
    factor: float | None, base: float | None, printed: float | None, index: float
) -> float | None:
    return None if printed is None else _at(factor, base, printed, index)


def settled_energy(
    energy: VariableRates | ImpactRates, index: float
) -> VariableRates | ImpactRates:
    """A month-indexed leg's printed rates rebuilt at its settled BELIX.

    Each row is recomputed through its own formula, and a row printed with no
    formula keeps its figure. The card's price ceiling is left to the engine,
    which applies it to whatever rate it bills.
    """
    if isinstance(energy, ImpactRates):
        return replace(
            energy,
            pic=_at(energy.pic_factor, energy.pic_base, energy.pic, index),
            medium=_at(energy.medium_factor, energy.medium_base, energy.medium, index),
            eco=_at(energy.eco_factor, energy.eco_base, energy.eco, index),
        )
    return replace(
        energy,
        current=_at(energy.formula_factor, energy.formula_base, energy.current, index),
        peak=_at_optional(
            energy.formula_factor_peak, energy.formula_base_peak, energy.peak, index
        ),
        offpeak=_at_optional(
            energy.formula_factor_offpeak,
            energy.formula_base_offpeak,
            energy.offpeak,
            index,
        ),
        exclusive_night=_at_optional(
            energy.formula_factor_exclusive_night,
            energy.formula_base_exclusive_night,
            energy.exclusive_night,
            index,
        ),
        index_realised=index,
    )
