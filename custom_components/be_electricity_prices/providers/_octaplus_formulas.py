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

from ._parse import SIGN_CHARS

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
# Stated in EUR/MWh either way. The value part is anchored rather than left as
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
# Stated in EUR/MWh HTVA. The meter label sits before the formula and the two
# generations word it differently, so each meter gets its own lead-in.
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
# after it.
_CONSUMPTION_LEAD = (
    r"La\s+formule\s+(?:tarifaire\s+HTVA(?:\s*\(en\s*€\s*/\s*MWh\))?|de\s+prix)"
    r"\s+est\s+la\s+suivante\s*(?:,\s*en\s+(?:EUR|€)\s*/\s*MWh\s+HTVA)?\s*:"
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
    EUR/MWh against €/MWh. The lead accepts either spelling, and the match
    that is the feed-in formula is skipped, so a card that prints the feed-in
    paragraph first, or spells both units alike, still binds the consumption
    formula rather than raising or billing the feed-in one as energy.
    """
    feed_in = _injection_formula(text)
    for match in re.finditer(
        rf"{_CONSUMPTION_LEAD}.{{0,100}}?{_EPEX_FORMULA}", text, re.S
    ):
        if feed_in is None or match.start(1) != feed_in.start(1):
            return match
    return None
