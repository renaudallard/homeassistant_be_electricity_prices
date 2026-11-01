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

"""The price formulas OCTA+ prints on its electricity cards: the patterns
for each spelling the cards have used, and the readers that find a formula
after its own lead-in rather than anywhere in the document."""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, parse_sign, to_float

# The January and February 2026 cards name both indices Belpex: "Belpex 15' * 1
# - 13,89" on the dynamic card, "Belpex SPP x 0,852 - 13,39" on the monthly
# ones. From March every card says Epex. The dynamic cards before 2026 index
# on the clock hour, "Belpex Hourly * 1,038 + 3,93", and without that spelling
# a contract signed on one could not read its own card.
_EPEX_FORMULA = (
    rf"(?:Bel|E)pex\s*(?:15\s*'?|Hourly)\s*\*\s*(\d+(?:[.,]\d+)?)\s*"
    rf"([{SIGN_CHARS}])\s*(\d+(?:[.,]\d+)?)"
)
# The 2026 template reworded the injection lead-in from "Le prix de votre
# injection est indexé ..." to "les prix de l'électricité injectée sont
# indexés ..."; accept either (with the curly apostrophe the card uses).
# The monthly index the non-dynamic cards settle their feed-in credit on. The
# August 2026 redesign renamed the parameter and changed the operator, and the
# first two cards of the year named the index Belpex, so all three spellings
# have to be accepted:
#   January "monohoraire : Belpex SPP x 0,852 - 13,39"  (one row per meter)
#   April  "monohoraire : Epex SPP x 0,852 - 13,39"     (one row per meter)
#   August "en EUR/MWh HTVA : Epex SPP M * 0,8560 - 16,20"  (one row)
# Stated in EUR/MWh until October 2026, in the unit its sentence states since
# (_formula_coefficients). The value part is anchored rather than left as
# a character class, or the sentence-final period is swallowed into the number.
# The optional M must be followed by the operator, so the prose mention of the
# parameter name on its own ("le parametre << Epex SPP M >>") cannot match.
_SPP_FORMULA_RE = re.compile(
    rf"(?:Bel|E)pex\s*SPP\s*M?\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}])\s*"
    rf"(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
# The monthly index the variable cards bill CONSUMPTION on, one row per meter
# configuration. Both card generations, same as the SPP sibling above:
#   April  "compteur monohoraire : Epex RLP * 1,15 + 10"
#   August "mono-horaire (simple) : Epex RLP M * 1,150 + 10,000"
# Stated in EUR/MWh HTVA until October 2026, in c€/kWh since. The meter label
# sits before the formula and the two generations word it differently, so each
# meter gets its own lead-in.
_RLP_FORMULA = (
    rf"Epex\s*RLP\s*M?\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}+])\s*"
    rf"(\d+(?:[.,]\d+)?)"
)
_RLP_METER_RES: dict[str, re.Pattern[str]] = {
    "single": re.compile(rf"mono[\s-]*horaire[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "peak": re.compile(rf"heures\s+pleines[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "offpeak": re.compile(rf"heures\s+creuses[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "exclusive_night": re.compile(
        rf"exclusi[fv]\s+nuit[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I
    ),
}

# The CWaPE bands the Walloon variable cards price on their own formula, one
# per band, after the per-meter ones: "Impact Eco : Epex RLP * 0,830 + 6,770 ;
# Impact Medium : Epex RLP * 1,142 + 6,770 ; Impact Pic : Epex RLP * 1,366 +
# 6,770". The lines wrap inside a formula, so the gaps take a newline. The
# feed-in paragraph names the same bands on "Epex SPP", which RLP keeps out.
_RLP_IMPACT_RES: dict[str, re.Pattern[str]] = {
    band: re.compile(rf"Impact\s+{label}\s*:\s*{_RLP_FORMULA}", re.I)
    for band, label in (("pic", "Pic"), ("medium", "Medium"), ("eco", "Eco"))
}

_INJECTION_LEAD = (
    r"(?:Le\s+prix\s+de\s+votre\s+injection"
    r"|prix\s+de\s+l['’]électricité\s+injectée\s+sont\s+indexés)"
)


def _injection_formula(text: str) -> re.Match[str] | None:
    """The feed-in formula a dynamic card prints after its lead-in, or None.

    It follows the lead-in within a sentence: 174 to 252 characters on every
    card archived since January 2026. The gap is bounded because the same card
    goes on to quote the formulas it would bill an AMR meter on, the
    CONSUMPTION one first, about 2.600 characters later. An open search walked
    into that clause whenever the real formula went unread and billed the
    consumption formula as the credit; bounded, such a card reads no formula,
    which the live check reports.
    """
    return re.search(rf"{_INJECTION_LEAD}.{{0,500}}?{_EPEX_FORMULA}", text, re.S)


# The sentence that introduces the consumption formula, once on every dynamic
# card: "La formule tarifaire HTVA (en €/MWh) est la suivante:" until the
# August 2026 redesign, "La formule de prix est la suivante, en EUR/MWh HTVA :"
# after it, with "en cEUR/kWh HTVA" since November 2026.
_CONSUMPTION_LEAD = (
    r"La\s+formule\s+(?:tarifaire\s+HTVA(?:\s*\(en\s*€\s*/\s*MWh\))?|de\s+prix)"
    r"\s+est\s+la\s+suivante\s*"
    r"(?:,\s*en\s+c?(?:EUR|€)\s*/\s*(?:MWh|kWh)\s+HTVA)?\s*:"
)


def _dynamic_consumption_formula(text: str) -> re.Match[str] | None:
    """The consumption formula a dynamic card prints after its lead-in, or None.

    The consumption and injection formulas share the same shape, and the AMR
    clause further down quotes two more. The formula directly follows its
    lead-in on every archived card, so the search is bounded the way the
    feed-in one is: an open search took the first formula that was not the
    feed-in one, which on a card printing its own in an unknown spelling was
    the AMR clause's.

    Since the June 2026 card the feed-in paragraph opens with the same
    sentence as the consumption one, told apart only by writing the unit
    EUR/MWh against €/MWh, and since November cEUR/kWh against €/kWh. The
    lead accepts every spelling, and the match that is the feed-in formula is
    skipped, so a card that prints the feed-in paragraph first, or spells both
    units alike, still binds the consumption formula rather than raising or
    billing the feed-in one as energy.
    """
    feed_in = _injection_formula(text)
    for match in re.finditer(
        rf"{_CONSUMPTION_LEAD}.{{0,100}}?{_EPEX_FORMULA}", text, re.S
    ):
        if feed_in is None or match.start(1) != feed_in.start(1):
            return match
    return None


# The unit a formula's sentence states ahead of it: "(en €/MWh)" up to May
# 2026, "en EUR/MWh HTVA" and "en €/MWh HTVA" from June, and since November
# 2026 "en cEUR/kWh HTVA" ahead of the consumption formulas and "en €/kWh
# HTVA" ahead of the feed-in ones. No formula on an archived card sits more
# than 333 characters after its statement, the last of the seven of a Walloon
# variable card being the farthest. Smart Variable's cards of January to May
# 2026 state none ahead of their meter formulas, which are in EUR/MWh.
_UNIT_RE = re.compile(
    r"\ben\s+(c?)(?:EUR|€)\s*/\s*(MWh|kWh)\s+HTVA|\(en\s*(c?)€\s*/\s*(MWh|kWh)\)"
)
_UNIT_REACH = 600
# Factor multiplier and base divisor per stated unit, keyed on whether it is
# in cents and what it is per; see _formula_coefficients.
_CONSUMPTION_SCALES: dict[tuple[bool, str], tuple[float, float]] = {
    (False, "MWh"): (1.0, 1000.0),
    (True, "kWh"): (1.0, 100.0),
}
_FEED_IN_SCALES = {**_CONSUMPTION_SCALES, (False, "kWh"): (10.0, 100.0)}


def _formula_coefficients(
    text: str, match: re.Match[str], *, feed_in: bool
) -> tuple[float, float] | None:
    """The factor and base of the formula ``match`` holds in groups 1 to 3,
    as a price in EUR/kWh on an index in EUR/kWh, before any VAT.

    A formula stated in EUR/MWh keeps its factor and divides its base by 1000.
    The November 2026 cards state c€/kWh and say that a parameter published in
    EUR/MWh is divided by 10 before the formula applies, so the factor stays
    as printed and the base divides by 100. Their feed-in formulas say €/kWh
    over a factor and a base both a tenth of October's, "Epex SPP M * 0,0879 –
    1,61" where October printed "0,8790 – 16,15" in €/MWh. The estimates on
    their first page come out of that factor applied to the parameter in
    EUR/MWh, giving c€/kWh, so that is how it is read: ten times the factor and
    the base over 100. A formula stated in any other unit answers None.
    """
    start = match.start(1)
    stated = list(_UNIT_RE.finditer(text, max(0, start - _UNIT_REACH), start))
    if stated:
        last = stated[-1]
        unit = (bool(last.group(1) or last.group(3)), last.group(2) or last.group(4))
    else:
        unit = (False, "MWh")
    scale = (_FEED_IN_SCALES if feed_in else _CONSUMPTION_SCALES).get(unit)
    if scale is None:
        return None
    times, per = scale
    return (
        to_float(match.group(1)) * times,
        parse_sign(match.group(2)) * to_float(match.group(3)) / per,
    )
