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

"""The two readers Frank Energie's variable electricity cards need.

The Variabel cards come out of the same CMS as the dynamic ones and print the
same title, subscription row, levies and network table, which ``frank.py``
reads for both. Their energy and their feed-in differ:

    Formule Stroomprijs: (0,1068 x Belpex RLP_BE + 1,500) x 1,06 in EURct/kWh
    Formule Terugleveringsvergoeding: (0,1 x M BELPEX SPP_BE -1,150), in
    EURct/kWh (vrijgesteld van btw)
    Verwacht volgens methode VNR voor november 2026 (EURct/kWh)  8,2277

The energy is indexed on the delivery month's RLP-weighted mean of the hourly
day-ahead price, in EUR/MWh. The card calls RLP_BE the mean of the Flemish
operators' profiles, but the values Frank publishes for it are the mean of
every column of Synergrid's sheet, the "columns" blend energie.be uses: within
0,09 EUR/MWh in each month of 2026 up to October, where the Fluvius curve alone
misses by up to 1,09.

The feed-in is indexed on the month's SPP-weighted mean, the same index Frank
publishes as M BELPEX SPP_BE, and the card prints its value at VNR's estimate
of that index (VREG's until 2025), which is credited while the profile is not
held. Up to February 2025 the cards credited the feed-in per hour instead
("BELPEX per uur", the dynamic cards' wording), which ``frank.py`` reads with
its dynamic reader.

Every formula on either product says what it gives in the words after it,
"in EURct/kWh", which ``require_formula_unit`` holds all four formula readers
to.
"""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, UNIT_WORD, parse_sign, stated_unit, to_float
from ._pdf import NUM_NO_THOUSANDS
from ._rates import InjectionRates, SpotMonthlyRates
from .base import ExtractorError

_NUM = NUM_NO_THOUSANDS

# The HV card of September 2026 writes "BELPEX RLP_BE", and the Plus cards of
# October and November 2026 print a stray bracket after the factor
# ("(0,10100] x Belpex RLP_BE + 0,485) x 1,06"). The bracket and the blanks
# after it are one optional piece: as runs of blanks either side of an
# optional bracket, a run of blanks was split between the two in every way.
_ENERGY_RE = re.compile(
    rf"Formule\s+Stroomprijs:\s*\(\s*{_NUM}\s*(?:\]\s*)?x\s*Belpex\s*RLP_BE\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*\)\s*x\s*{_NUM}",
    re.IGNORECASE,
)
_INJECTION_RE = re.compile(
    rf"Formule\s+Terugleveringsvergoeding:\s*\(\s*{_NUM}\s*x\s*M\s*BELPEX\s*SPP_BE\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*\)",
    re.IGNORECASE,
)
# The estimate printed under the feed-in formula, looked for in the stretch of
# text that follows it: the energy's own estimate sits above the formula.
_ESTIMATE_RE = re.compile(
    rf"Verwacht\s+volgens\s+methode\s+\w+\s+voor\s+[A-Za-z]+\s+\d{{4}}\s*"
    rf"\(EURct/kWh\)\s*([{SIGN_CHARS}]?)\s*{_NUM}",
    re.IGNORECASE,
)
_ESTIMATE_REACH = 600

# The unit a formula gives, stated right after it: "x 1,06 in EURct/kWh" after
# the energy formula and "-1,150), in EURct/kWh (vrijgesteld van btw)" after
# the feed-in one, on the dynamic and the variable cards alike, in those words
# on every card held from January 2025 to November 2026 (none of March 2026
# is). The statement ends 13 or 14 characters past the formula; the next
# unit, the expected price's "(EURct/kWh)" on the line below, starts 58 or
# more past it, out of reach. No card states the unit of BELPEX itself, which
# the factors take in EUR/MWh, so there is none to hold. The unit runs to the
# next blank, less a full stop or comma ending the sentence.
_UNIT_RE = re.compile(rf"\bin\s++{UNIT_WORD}")
_UNIT = "EURct/kWh"
_UNIT_REACH = 30


def require_formula_unit(text: str, formula: re.Match[str], what: str) -> None:
    """Refuse the ``what`` formula ``formula`` matched unless the card states
    it in EURct/kWh, the unit its reader scales the factor and base from.

    A card restating its formulas in another unit still matches the formula
    patterns and would bill ten or a hundred times out, as OCTA+'s November
    2026 cards did. One that stops stating a unit is refused as well: every
    card read so far states it.
    """
    unit = stated_unit(text, _UNIT_RE, formula.span(), after=_UNIT_REACH)
    if unit != _UNIT:
        raise ExtractorError(
            f"Frank Energie: {what} formula stated in {unit or 'no unit'}, "
            f"expected {_UNIT}"
        )


def variable_energy(text: str, yearly_fee: float) -> SpotMonthlyRates:
    """The energy leg of a variable card, VAT included by the multiplier its
    formula prints. The card's expected price is not read: it sits on VNR's
    estimate of the index, where the bill uses the month's own."""
    m = _ENERGY_RE.search(text)
    if m is None:
        raise ExtractorError("could not parse Frank Energie variable energy formula")
    require_formula_unit(text, m, "variable energy")
    factor_pdf = to_float(m.group(1))
    base_cents = parse_sign(m.group(2)) * to_float(m.group(3))
    vat_mult = to_float(m.group(4))
    # (factor_pdf x index in EUR/MWh + base in c/kWh) x VAT, as the dynamic
    # formula: the index in EUR/kWh is a thousandth of it, the price in EUR/kWh
    # a hundredth, so the factor takes ten and the base a hundredth.
    return SpotMonthlyRates(
        factor=factor_pdf * vat_mult * 10.0,
        base=base_cents * vat_mult / 100.0,
        yearly_fixed_fee=yearly_fee,
        rlp_indexed=True,
        rlp_blend="columns",
    )


def variable_injection(text: str) -> InjectionRates | None:
    """The feed-in of a variable card on the month's solar index, or None on
    a card that credits it per hour. VAT-exempt, so the printed figures
    convert as they are, and the sign is kept: the formula settles below zero
    in a month whose index is under 11,50 EUR/MWh."""
    m = _INJECTION_RE.search(text)
    if m is None:
        return None
    require_formula_unit(text, m, "variable feed-in")
    estimate = _ESTIMATE_RE.search(text, m.end(), m.end() + _ESTIMATE_REACH)
    if estimate is None:
        # Every card prints one; without it a month whose profile is not
        # held would credit nothing.
        raise ExtractorError("Frank Energie: variable feed-in estimate not found")
    indicative = parse_sign(estimate.group(1)) * to_float(estimate.group(2))
    return InjectionRates(
        current=indicative / 100.0,
        factor=to_float(m.group(1)) * 10.0,
        base=parse_sign(m.group(2)) * to_float(m.group(3)) / 100.0,
        formula=m.group(0),
        spp_indexed=True,
    )
