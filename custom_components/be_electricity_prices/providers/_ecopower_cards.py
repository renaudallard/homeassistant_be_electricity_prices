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
"""Ecopower card parsers: the energy leg and the injection leg.

Split out of ``ecopower.py``, as ``_mega_cards.py`` was out of ``mega.py``.
These are the parsers that read an Ecopower card's own product figures, for
both the Groene and the Dynamische burgerstroom cards: the resolved rate or
the spot formula, the monthly subscription and the feed-in credit with its
fixed-price note. The grid and levy overlays live in
``_ecopower_overlays.py``; ``ecopower.py`` keeps the URL resolution, the
archive and the two ``parse_*`` entry points, which call into both.

No behaviour change: every function here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, parse_sign, to_float
from ._pdf import NL_MONTHS
from ._rates import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    VariableRates,
)
from .base import ExtractorError

# Dutch month names for archive_validity_check; the helper indexes into
# this tuple as month_names[year_month.month - 1].
_NL_MONTHS = NL_MONTHS


# ---- energy ------------------------------------------------------------------


# Anchor on the start of the consumption line ("Groene burgerstroom" or
# "Afname Groene burgerstroom"). Without the line anchor this also matched
# the "Injectie Groene Burgerstroom ... euro/kWh" line, so a card that
# printed split-layout energy (value below the label) together with a
# same-line injection value would bind the energy rate to the injection
# figure instead of falling through to _ENERGY_SPLIT_RE. The leading
# ``[^\w\n]*`` tolerates a bullet or other punctuation prefix a re-render
# might add; it cannot consume the leading word of "Injectie", so that line
# stays excluded.
_ENERGY_RE = re.compile(
    r"^[^\w\n]*(?:Afname\s+)?Groene\s+burgerstroom[^\n]*?([\d,]+)\s*euro/kWh",
    re.IGNORECASE | re.MULTILINE,
)

# Mid-2026 cards moved the resolved rate onto the line *below* the
# "Afname Groene burgerstroom (50% vast ... + 50% variabel ...)" label
# instead of trailing it on the same line. Fall back to this when the
# same-line form misses.
_ENERGY_SPLIT_RE = re.compile(
    r"Afname\s+Groene\s+burgerstroom[^\n]*\n\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# The July 2026 card broke the 50/50 split onto its own two lines, with the
# resolved rate trailing the VARIABEL half:
#     Afname Groene Burgerstroom
#     VAST 50% x 0,17 euro
#     VARIABEL +50% x 0,11444616 euro deze waarde is gelijk aan [EPEX RLP]. 0,1422 euro/kWh
# Anchor on the literal VAST / VARIABEL rows rather than "skip a line": a
# looser next-line-or-two fallback matched the "Kost WKK 0,00392 euro/kWh"
# row two lines below the label on the older same-line cards, which would
# bill the cogeneration levy as the commodity rate if the same-line regex
# ever missed on one of them.
_ENERGY_VARIABEL_RE = re.compile(
    r"Afname\s+Groene\s+burgerstroom[^\n]*\n"
    r"\s*VAST[^\n]*\n"
    r"\s*VARIABEL[^\n]*?([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


def _extract_energy(text: str) -> EnergyRates:
    """Parse the "Groene burgerstroom" effective rate (HTVA, EUR/kWh).

    The card prints the formula breakdown
    ``(50% vast aan 0,17 euro + 50% variabel aan 0,08472117 euro)``
    followed by the resolved ``0,1274 euro/kWh`` figure. We use the
    resolved number because (a) we don't have a Belpex feed at parse
    time, and (b) supporting Ecopower's variable cost without a live
    spot is exactly what ``VariableRates`` is for.
    """
    match = (
        _ENERGY_RE.search(text)
        or _ENERGY_VARIABEL_RE.search(text)
        or _ENERGY_SPLIT_RE.search(text)
    )
    if not match:
        raise ExtractorError("could not parse Ecopower 'Groene burgerstroom' rate")
    return VariableRates(current=to_float(match.group(1)))


# ---- dynamic energy ----------------------------------------------------------

# The dynamic card prints the consumer formula in EUR/kWh terms, with the
# EPEX DA spot expressed in EUR/MWh:
#   "Dynamische burgerstroom elk kwartier 0,00102 × EPEX DA +0,004 euro/kWh"
# The '×' is U+00D7 but a re-render could swap it; accept the common
# multiplication glyphs. SIGN_CHARS covers every minus/plus variant for the
# additive base so a punctuation drift never flips the sign silently.
# The cards up to September 2025 say "elk uur": the product settled on the
# hourly EPEX DA price until the day-ahead market moved to quarter-hours, and
# a past month is stored as the product it was.
_DBS_ENERGY_RE = re.compile(
    r"Dynamische\s+burgerstroom\s+elk\s+(kwartier|uur)\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

_ABONNEMENT_RE = re.compile(r"Abonnementskost\s+([\d,]+)\s*euro/maand", re.IGNORECASE)


def _extract_dbs_energy(text: str) -> DynamicRates:
    """Parse the dynamic ``factor x spot + base`` consumption formula (HTVA).

    The card multiplies the EPEX DA price in EUR/MWh, so ``factor`` is
    scaled by 1000 to act on the spot price the pricing engine feeds in
    EUR/kWh: ``0,00102 × MWh = 1.02 × kWh``. Values are HTVA;
    ``vat_rate=0.06`` in the tax overlay scales the energy component to
    TVAC in ``compute_breakdown`` (the same convention as the gbs card),
    so they are NOT pre-scaled here.

    The monthly subscription (``Abonnementskost``) maps to
    ``yearly_fixed_fee``, which is consumed as the actual annual euros
    without further VAT scaling, so the 6% residential VAT is baked in.
    """
    match = _DBS_ENERGY_RE.search(text)
    if not match:
        raise ExtractorError(
            "could not parse Ecopower 'Dynamische burgerstroom' formula"
        )
    factor = to_float(match.group(2)) * 1000.0
    base = parse_sign(match.group(3)) * to_float(match.group(4))
    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=_extract_dbs_abonnement(text),
        quarter_hourly=match.group(1).lower() == "kwartier",
    )


def _extract_dbs_abonnement(text: str) -> float:
    """Yearly subscription fee in EUR, VAT-inclusive.

    Printed HTVA as ``Abonnementskost 5,00 euro/maand``, so multiply out the
    12 months and leave it HTVA: this card declares ``vat_rate=0.06`` and
    ``_resolve.apply_vat`` grosses every flat annual fee once, per entry. Baking
    the 6% here as well billed it twice.
    """
    match = _ABONNEMENT_RE.search(text)
    if not match:
        raise ExtractorError("could not parse Ecopower Abonnementskost")
    return to_float(match.group(1)) * 12.0


# ---- injection ---------------------------------------------------------------


_INJECTION_RE = re.compile(
    # The injection row was relabelled on the May 2026 card. Match both:
    #   <= Apr 2026:  "Terugleververgoeding (digitale meter) 2 -0,0200 euro/kWh"
    #   >= May 2026:  "Injectie Groene Burgerstroom (terugleververgoeding)2 -0,0200 euro/kWh"
    # SIGN_CHARS covers every minus glyph (hyphen, figure/en/em dash, U+2212)
    # a PDF re-render might swap in, so the sign never flips silently.
    r"(?:Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)"
    r"|Terugleververgoeding[^\n]*digitale\s+meter)"
    rf"[^\n]*?([{SIGN_CHARS}]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# Split-layout fallback: mid-2026 cards print the resolved injection
# value on the formula line *below* the label rather than on the label
# line. Anchor on the label, then take the value on the next line.
_INJECTION_SPLIT_RE = re.compile(
    r"Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)[^\n]*\n"
    rf"[^\n]*?([{SIGN_CHARS}]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# The July 2026 layout, mirroring _ENERGY_VARIABEL_RE on the injection side:
#     Injectie Groene Burgerstroom (terugleververgoeding)
#     VAST 50% x 0,02 euro
#     VARIABEL +50% x 0,04638137 euro ... 0,9 x ... [EPEX SPP] - 0,01. -0,0332 euro/kWh
# Without this the whole block missed and _extract_injection returned None,
# which costs a solar user their entire feed-in credit without raising.
_INJECTION_VARIABEL_RE = re.compile(
    r"Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)[^\n]*\n"
    r"\s*VAST[^\n]*\n"
    rf"\s*VARIABEL[^\n]*?([{SIGN_CHARS}]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# Authoritative current-month statement on the split-layout cards that
# show a 50% fixed + 50% variable injection formula: an
# "OPGELET t.e.m. <date> is de terugleververgoeding <value> euro/kWh en
# 100% vast" note pins the actually-applied fixed credit. The formula
# line below the label resolves the *variable* half, which only kicks in
# once the note's date passes (Ecopower flips injection to 50% variable
# from 1 July 2026), so this fixed value must win while it's printed.
_INJECTION_FIXED_RE = re.compile(
    r"terugleververgoeding\s+([\d,]+)\s*euro/kWh\s+en\s+100\s*%\s*vast",
    re.IGNORECASE,
)

_NL_MONTH_INDEX = {name: i + 1 for i, name in enumerate(_NL_MONTHS)}
_MONTH_ALT = "|".join(_NL_MONTHS)
_FIXED_NOTE_EXPIRY_RE = re.compile(rf"t\.e\.m\.\s+\d+\s+({_MONTH_ALT})", re.IGNORECASE)
_CARD_MONTH_RE = re.compile(rf"Tariefkaart\s+({_MONTH_ALT})\s+\d{{4}}", re.IGNORECASE)


def _fixed_note_in_effect(text: str) -> bool:
    """Whether the ``... 100% vast`` injection note still applies to this
    card's pricing month.

    The note declares its own expiry (``OPGELET t.e.m. 30 juni ...``).
    Honour the fixed value for cards up to that month, but ignore a stale
    note carried onto a later month's card (e.g. a July card that already
    prints the 50%-variable formula but still carries the old June note),
    which would otherwise credit users the wrong fixed rate. Returns True
    when staleness can't be established, so a card that doesn't print a
    parseable month still trusts the note it shows.
    """
    note = _FIXED_NOTE_EXPIRY_RE.search(text)
    card = _CARD_MONTH_RE.search(text)
    if note is None or card is None:
        return True
    return (
        _NL_MONTH_INDEX[card.group(1).lower()] <= _NL_MONTH_INDEX[note.group(1).lower()]
    )


# The July 2026 generation split the feed-in credit 50/50 between a fixed half
# and a half indexed on the delivery month's SPP-weighted EPEX DA mean.
# Anchored on the literal VAST / VARIABEL rows: the pre-July cards, and the
# June split card, print the same words in prose without those rows, and the
# June one pins an actual 100%-fixed credit that must keep winning.
_INJECTION_SPP_SPLIT_RE = re.compile(
    r"Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)[^\n]*\n"
    r"\s*VAST\s+(\d+)\s*%\s*[×xX*]\s*([\d,]+)\s*euro[^\n]*\n"
    r"\s*VARIABEL\s*\+?\s*(\d+)\s*%[^\n]*?formule\s+"
    r"([\d,]+)\s*[×xX*]\s*[\d,]+\s*\[[^\]]*SPP[^\]]*\]\s*"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)",
    re.IGNORECASE,
)
_INJECTION_NEVER_NEGATIVE_RE = re.compile(
    r"terugleververgoeding\s+kan\s+nooit\s+negatief\s+zijn", re.IGNORECASE
)


def _extract_injection(text: str) -> InjectionRates | None:
    """Parse the injection (terugleververgoeding) price.

    The terugleververgoeding is a feed-in credit the customer
    *receives* ("de vergoeding die klanten ... krijgen voor hun
    injectie"; Ecopower states the price is never negative). The card
    prints it as a negative EUR/kWh figure (``-0,0200 euro/kWh``) only
    because it sits in the energy/cost column, where a credit shows as
    a negative cost. Negate it so ``current`` holds the compensation as
    a positive number, matching every other supplier's injection sign.
    """
    # Prefer the explicit current-month fixed credit when the card
    # prints the 100%-vast note: on split-layout cards the label line
    # carries only the 50/50 formula and the line below resolves the
    # variable half, which doesn't apply yet. Falling through to that
    # line credited users the variable value (e.g. 0,0329) instead of
    # the fixed 0,020 they actually receive this month.
    fixed = _INJECTION_FIXED_RE.search(text)
    if fixed is not None and _fixed_note_in_effect(text):
        return InjectionRates(current=abs(to_float(fixed.group(1))))
    match = (
        _INJECTION_RE.search(text)
        or _INJECTION_VARIABEL_RE.search(text)
        or _INJECTION_SPLIT_RE.search(text)
    )
    if not match:
        return None
    # The credit is never negative (Ecopower states this); the card merely
    # prints it in the energy/cost column as a negative figure. Strip any
    # leading sign glyph: the regex admits every SIGN_CHARS minus, so the
    # hand-rolled variant list missed U+2010 / U+2011 and to_float raised
    # ValueError on them, and take the magnitude.
    raw = match.group(1).replace(" ", "").lstrip(SIGN_CHARS)
    current = abs(to_float(raw))
    # From the July 2026 card the credit is half fixed and half indexed on the
    # DELIVERY month's SPP-weighted EPEX DA mean: "VAST 50% x 0,02 euro /
    # VARIABEL +50% x 0,04638137 euro deze waarde volgt de formule 0,9 x
    # 0,06264597 [EPEX SPP 2] - 0,01", with footnote 2 naming the index as
    # "het werkelijke SPP gewogen gemiddelde van de Day Ahead EPEX (EPEX DA)
    # voor de maand juli". Ecopower publishes definitive cards in ARREARS, so
    # the printed figure is always a settled past month.
    #
    # Blending the two halves gives one pair:
    #   credit = 0,50 x 0,02 + 0,50 x (0,9 x SPP - 0,01)
    #          = 0,45 x SPP + 0,005
    # No unit conversion: this card's index is already EUR/kWh, unlike the
    # dbs sibling which prints EUR/MWh and scales by 1000.
    split = _INJECTION_SPP_SPLIT_RE.search(text)
    if split is None:
        return InjectionRates(current=current)
    vast_share = to_float(split.group(1)) / 100.0
    vast_value = to_float(split.group(2))
    var_share = to_float(split.group(3)) / 100.0
    multiplier = to_float(split.group(4))
    var_base = parse_sign(split.group(5) or "+") * to_float(split.group(6))
    return InjectionRates(
        current=current,
        factor=var_share * multiplier,
        base=vast_share * vast_value + var_share * var_base,
        formula=" ".join(split.group(0).split()),
        spp_indexed=True,
        # "De terugleververgoeding kan nooit negatief zijn." Stated on this
        # card generation and this one only.
        floor_at_zero=_INJECTION_NEVER_NEGATIVE_RE.search(text) is not None,
    )


# The dynamic card prints the injection formula like the consumption one,
# on the same grid:
#   "Terugleververgoeding elk kwartier 0,00098 × EPEX DA - 0,015 euro/kWh"
_DBS_INJECTION_RE = re.compile(
    r"Terugleververgoeding\s+elk\s+(?:kwartier|uur)\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


def _extract_dbs_injection(text: str) -> InjectionRates | None:
    """Parse the dynamic injection (terugleververgoeding) formula.

    Like the consumption formula, the EPEX DA factor is in EUR/MWh, so
    it is scaled by 1000 to act on the EUR/kWh spot
    (``0,00098 × MWh = 0.98 × kWh``). The card's base is signed
    (``- 0,015``); a negative base means the credit drops below zero at
    low spot, which the pricing engine respects. Residential injection
    is VAT-exempt, so no scaling is applied.
    """
    match = _DBS_INJECTION_RE.search(text)
    if not match:
        return None
    factor = to_float(match.group(1)) * 1000.0
    base = parse_sign(match.group(2)) * to_float(match.group(3))
    return InjectionRates(factor=factor, base=base, formula=match.group(0).strip())
