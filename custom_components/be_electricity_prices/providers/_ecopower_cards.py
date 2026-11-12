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

The split itself changed no behaviour; the energy formula and the printed
indices were read here later.
"""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, formula_gives, parse_sign, stated_unit, to_float
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
_NL_MONTH_INDEX = {name: i + 1 for i, name in enumerate(_NL_MONTHS)}
_MONTH_ALT = "|".join(_NL_MONTHS)


# ---- energy ------------------------------------------------------------------


# The consumption and feed-in labels.
_ENERGY_LABEL = r"Afname\s+Groene\s+burgerstroom"
_INJECTION_LABEL = r"Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)"


def _rest_of_line(label: str) -> str:
    """``label`` and the rest of its line, its line break included.

    The rest stops at the next ``label`` on the line, which reads the same
    lines below: run on to the line's end from each, a line of labels was
    read again from every one.
    """
    return rf"{label}(?:(?!{label})[^\n])*\n"


# Anchor on the start of the consumption line ("Groene burgerstroom" or
# "Afname Groene burgerstroom"). Without the line anchor this also matched
# the "Injectie Groene Burgerstroom ... euro/kWh" line, so a card that
# printed split-layout energy (value below the label) together with a
# same-line injection value would bind the energy rate to the injection
# figure instead of falling through to _ENERGY_SPLIT_RE. The leading
# ``[^\w\n]*`` tolerates a bullet or other punctuation prefix a re-render
# might add; it cannot consume the leading word of "Injectie", so that line
# stays excluded. The rate is read from the start of its digits: tried from
# each digit of a long run, the run was read again from every one.
_ENERGY_RE = re.compile(
    r"^[^\w\n]*(?:Afname\s+)?Groene\s+burgerstroom[^\n]*?(?<![\d,])([\d,]+)\s*euro/kWh",
    re.IGNORECASE | re.MULTILINE,
)

# Mid-2026 cards moved the resolved rate onto the line *below* the
# "Afname Groene burgerstroom (50% vast ... + 50% variabel ...)" label
# instead of trailing it on the same line. Fall back to this when the
# same-line form misses.
_ENERGY_SPLIT_RE = re.compile(
    _rest_of_line(_ENERGY_LABEL) + r"\s*([\d,]+)\s*euro/kWh",
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
# ever missed on one of them. The rate is read from the start of its digits,
# as _ENERGY_RE's is.
_ENERGY_VARIABEL_RE = re.compile(
    _rest_of_line(_ENERGY_LABEL) + r"\s*VAST[^\n]*\n"
    r"\s*VARIABEL[^\n]*?(?<![\d,])([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


# The formula behind that rate: a fixed share at a fixed price and a variable
# share at the month's RLP-weighted EPEX mean, in EUR/kWh before VAT.
#   <= Jun 2026: "(50% vast aan 0,17 euro + 50% variabel aan 0,09810780 1 euro)"
#   >= Jul 2026: "VAST 50% x 0,17 euro" / "VARIABEL +50% x 0,11444616 euro
#                 deze waarde is gelijk aan [EPEX RLP 1]."
# The second figure on each is the index the card resolved the rate at. The
# RLP on the VARIABEL row keeps the feed-in rows, which print SPP, out. The
# VAST row's line stops at the next VAST row on it, as _rest_of_line does.
_ENERGY_FORMULA_RES = (
    re.compile(
        r"\(\s*(\d+)\s*%\s*vast\s+aan\s+([\d,]+)\s*euro\s*\+\s*"
        r"(\d+)\s*%\s*variabel\s+aan\s+([\d,]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"VAST\s+(\d+)\s*%\s*[×xX*]\s*([\d,]+)\s*euro"
        r"(?:(?!VAST\s+\d+\s*%\s*[×xX*]\s*[\d,]+\s*euro)[^\n])*\n"
        r"\s*VARIABEL\s*(?:\+\s*)?(\d+)\s*%\s*[×xX*]\s*([\d,]+)\s*euro[^\n]*RLP",
        re.IGNORECASE,
    ),
)
# Footnote 1, naming the month the printed index is for. A card resolved on
# an estimate says so instead ("Dit is de inschatting die de VNR ..."). The
# gap to the month stops at the next such footnote on the line: run on to the
# line's end from each, a line of them was read again from every one.
_RLP_MONTH_RE = re.compile(
    r"werkelijke\s+RLP\s+gewogen\s+gemiddelde"
    r"(?:(?!werkelijke\s+RLP\s+gewogen\s+gemiddelde)[^\n])*?"
    rf"voor\s+de\s+maand\s+({_MONTH_ALT})",
    re.IGNORECASE,
)

# The unit a burgerstroom card prints straight after a figure of its formulas,
# past the footnote mark the index carries on the bracket cards: "vast aan 0,17
# euro +", "variabel aan 0,09810780 1 euro)", "VAST 50% × 0,17 euro", "VARIABEL
# +50% × 0,18183624 euro deze waarde". Every card on file from February to
# October 2026 (none of March) prints "euro" there, five or seven characters
# on from the figure, and the formulas are read as EUR/kWh on it. The whole word is taken, so a card restated in "eurocent" or
# "euro/MWh", or one that drops the word, is not read as euro. The pattern
# starts nowhere inside a run of blanks: searched on from each, a run with no
# word after it was read again from every one.
_FIGURE_UNIT_RE = re.compile(r"(?<!\s)\s*+(?:\d\s++)?+([^\s),.;:]++)")
_FIGURE_UNIT_REACH = 12
_FIGURE_UNITS = frozenset({"euro"})


def _in_euro(text: str, match: re.Match[str], *groups: int) -> bool:
    """Whether the card prints "euro" straight after each figure ``match``
    holds in ``groups``."""
    return all(
        stated_unit(text, _FIGURE_UNIT_RE, match.span(group), after=_FIGURE_UNIT_REACH)
        in _FIGURE_UNITS
        for group in groups
    )


def _energy_formula(text: str) -> tuple[float, float, float] | None:
    """``(factor, base, printed index)`` of the card's formula, or None, also
    when the fixed price or the index is printed in anything but euro: the
    formula is then left out and the printed rate bills, as for a card whose
    formula does not read at all."""
    for pattern in _ENERGY_FORMULA_RES:
        match = pattern.search(text)
        if match is not None:
            if not _in_euro(text, match, 2, 4):
                return None
            vast, fixed, variabel, index = match.groups()
            return (
                to_float(variabel) / 100.0,
                to_float(vast) / 100.0 * to_float(fixed),
                to_float(index),
            )
    return None


def printed_rlp_index(text: str) -> tuple[int, float] | None:
    """``(month, EUR/kWh)`` of the settled RLP index the card prints, or None
    when the card does not say the figure is a month's settled value."""
    formula = _energy_formula(text)
    month = _RLP_MONTH_RE.search(text)
    if formula is None or month is None:
        return None
    return _NL_MONTH_INDEX[month.group(1).lower()], formula[2]


def _extract_energy(text: str) -> EnergyRates:
    """Parse the "Groene burgerstroom" rate (HTVA, EUR/kWh) and its formula.

    The card prints ``50% x 0,17 + 50% x <index>`` and the rate it resolves
    to, where the index is the RLP-weighted EPEX mean of the card's own
    month. Ecopower publishes a definitive card only once that month has
    ended, so the card served while a month runs is last month's, and its
    rate is last month's settled one. The formula is therefore carried
    ``month_indexed``, so the running month resolves against its own mean
    and the printed rate is the fallback for an entry without a key.

    The index is RLP-weighted on the "columns" blend: over July to September
    2026 it reproduces the printed values to within 0,3 EUR/MWh, where the
    Flemish curve alone runs 0,5 above and the distinct-curve mean 2 below.
    """
    match = (
        _ENERGY_RE.search(text)
        or _ENERGY_VARIABEL_RE.search(text)
        or _ENERGY_SPLIT_RE.search(text)
    )
    if not match:
        raise ExtractorError("could not parse Ecopower 'Groene burgerstroom' rate")
    current = to_float(match.group(1))
    formula = _energy_formula(text)
    if formula is None:
        return VariableRates(current=current)
    factor, base, _index = formula
    return VariableRates(
        current=current,
        formula_factor=factor,
        formula_base=base,
        month_indexed=True,
        rlp_indexed=True,
        rlp_blend="columns",
    )


# ---- dynamic energy ----------------------------------------------------------

# The dynamic card prints the consumer formula in EUR/kWh terms, with the
# EPEX DA spot expressed in EUR/MWh:
#   "Dynamische burgerstroom elk kwartier 0,00102 × EPEX DA +0,004 euro/kWh"
# The '×' is U+00D7 but a re-render could swap it; accept the common
# multiplication glyphs. SIGN_CHARS covers every minus/plus variant for the
# additive base so a punctuation drift never flips the sign silently.
# The cards up to September 2025 say "elk uur": the product settled on the
# hourly EPEX DA price until the day-ahead market moved to quarter-hours, and
# a past month is stored as the product it was. The blanks before the sign
# are taken whole: shared with those after it when no sign is printed, a run
# of blanks was split between the two in every way.
_DBS_ENERGY_RE = re.compile(
    r"Dynamische\s+burgerstroom\s+elk\s+(kwartier|uur)\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*+"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

_ABONNEMENT_RE = re.compile(r"Abonnementskost\s+([\d,]+)\s*euro/maand", re.IGNORECASE)

# The unit of the EPEX DA price both dynamic formulas multiply, which their own
# line leaves out. Page 2 states it once: "De EPEX DA notering is de dagelijks
# bepaalde kwartierprijs voor elektriciteit in België (uitgedrukt in
# euro/MWh)", "uurprijs" on the January 2025 card. It is "euro/MWh" on all
# five cards from January 2025 to November 2026, closing 3175 to 3456
# characters after a factor, and no other sentence on them says "uitgedrukt
# in". In the text the page's other column runs into it ("kwartierprijs voor
# Bijdrage op de energie"), so the subject may sit apart from the unit, under
# a hundred characters on those cards. The factors scale by 1000 on that
# statement.
_DBS_INDEX_UNIT_RE = re.compile(
    r"EPEX\s*DA\s+notering\b.{0,200}?\(uitgedrukt\s+in\s+([^)\s]++)\)", re.DOTALL
)
_DBS_INDEX_UNIT_REACH = 4500
_DBS_INDEX_UNITS = frozenset({"euro/MWh"})


def _dbs_index_unit(text: str, match: re.Match[str], factor: int) -> str | None:
    """The unit the dynamic card states for the EPEX DA price the factor in
    group ``factor`` of ``match`` multiplies, or None."""
    return stated_unit(
        text, _DBS_INDEX_UNIT_RE, match.span(factor), after=_DBS_INDEX_UNIT_REACH
    )


def _extract_dbs_energy(text: str) -> DynamicRates:
    """Parse the dynamic ``factor x spot + base`` consumption formula (HTVA).

    The card multiplies the EPEX DA price in EUR/MWh, so ``factor`` is
    scaled by 1000 to act on the spot price the pricing engine feeds in
    EUR/kWh: ``0,00102 × MWh = 1.02 × kWh``. Values are HTVA;
    ``vat_rate=0.06`` in the tax overlay scales the energy component to
    TVAC in ``compute_breakdown`` (the same convention as the gbs card),
    so they are NOT pre-scaled here.

    The monthly subscription (``Abonnementskost``) maps to
    ``yearly_fixed_fee``, twelve times the HTVA figure, which
    ``_resolve.apply_vat`` grosses once per entry.
    """
    match = _DBS_ENERGY_RE.search(text)
    if not match:
        raise ExtractorError(
            "could not parse Ecopower 'Dynamische burgerstroom' formula"
        )
    unit = _dbs_index_unit(text, match, 2)
    if unit not in _DBS_INDEX_UNITS:
        raise ExtractorError(
            "Ecopower 'Dynamische burgerstroom' formula: EPEX DA stated in "
            f"{unit or 'no unit'}, expected euro/MWh"
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
    """Yearly subscription fee in EUR, HTVA as the card prints it.

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


# The feed-in figure, read after a gap on its line: a sign glyph or blanks,
# the digits, the unit. A start inside a run of blanks or of digits is never
# the first that reads, the start of the run reads the same, so it is not
# tried: tried from every one, a long run was read again from each.
_INJECTION_FIGURE = rf"((?:[{SIGN_CHARS}]|(?<![^\S\n]))\s*(?<![\d,])[\d,]+)\s*euro/kWh"
# The injection row was relabelled on the May 2026 card. Read both:
#   <= Apr 2026: "Terugleververgoeding (digitale meter) 2 -0,0200 euro/kWh"
#   >= May 2026: "Injectie Groene Burgerstroom (terugleververgoeding)2 -0,0200 euro/kWh"
# SIGN_CHARS covers every minus glyph (hyphen, figure/en/em dash, U+2212) a PDF
# re-render might swap in, so the sign never flips silently.
#
# Each label is matched with the pattern one regex over both labels was, but
# tried only from the first label of its kind on a line: a later one on the
# same line reaches no "digitale meter" and no figure the first does not, so
# once the first fails the line is passed over. Tried from every label, a line
# of labels was read to its end again from each. The older label's gap to the
# figure stops at the next "digitale meter", the one tried before it, which
# reads what the open gap read: run on to the line's end from each, a line of
# "digitale meter" with no figure after them was read again from every one.
_INJECTION_LABEL_RE = re.compile(_INJECTION_LABEL, re.IGNORECASE)
_INJECTION_RE = re.compile(
    _INJECTION_LABEL + r"[^\n]*?" + _INJECTION_FIGURE, re.IGNORECASE
)
_OLD_INJECTION_LABEL_RE = re.compile("Terugleververgoeding", re.IGNORECASE)
_OLD_INJECTION_RE = re.compile(
    r"Terugleververgoeding[^\n]*digitale\s+meter"
    r"(?:(?!digitale\s+meter)[^\n])*?" + _INJECTION_FIGURE,
    re.IGNORECASE,
)


def _first_on_its_line(
    text: str, label: re.Pattern[str], row: re.Pattern[str]
) -> re.Match[str] | None:
    """``row`` matched at the first ``label`` that reads, each line tried at
    its first ``label`` only."""
    failed = -1
    for found in label.finditer(text):
        if found.end() <= failed:
            continue
        match = row.match(text, found.start())
        if match is not None:
            return match
        failed = text.find("\n", found.end())
        if failed < 0:
            return None
    return None


def _same_line_injection(text: str) -> re.Match[str] | None:
    """The feed-in figure on its label's line, under whichever label comes
    first, as one pattern over both labels read it, or ``None``."""
    found = [
        m
        for m in (
            _first_on_its_line(text, _INJECTION_LABEL_RE, _INJECTION_RE),
            _first_on_its_line(text, _OLD_INJECTION_LABEL_RE, _OLD_INJECTION_RE),
        )
        if m is not None
    ]
    return min(found, key=lambda match: match.start()) if found else None


# Split-layout fallback: mid-2026 cards print the resolved injection
# value on the formula line *below* the label rather than on the label
# line. Anchor on the label, then take the value on the next line.
_INJECTION_SPLIT_RE = re.compile(
    _rest_of_line(_INJECTION_LABEL) + r"[^\n]*?" + _INJECTION_FIGURE,
    re.IGNORECASE,
)

# The July 2026 layout, mirroring _ENERGY_VARIABEL_RE on the injection side:
#     Injectie Groene Burgerstroom (terugleververgoeding)
#     VAST 50% x 0,02 euro
#     VARIABEL +50% x 0,04638137 euro ... 0,9 x ... [EPEX SPP] - 0,01. -0,0332 euro/kWh
# Without this the whole block missed and _extract_injection returned None,
# which costs a solar user their entire feed-in credit without raising.
_INJECTION_VARIABEL_RE = re.compile(
    _rest_of_line(_INJECTION_LABEL)
    + r"\s*VAST[^\n]*\n\s*VARIABEL[^\n]*?"
    + _INJECTION_FIGURE,
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
# June one pins an actual 100%-fixed credit that must keep winning. The
# groups are the fixed share and value, the variable share, the variable
# value's sign and figure, then the SPP formula's multiplier, index, sign and
# base. The variable value takes a sign: under an SPP index of 11,1 EUR/MWh
# the formula gives one below zero.
#
# The bracket is checked for SPP ahead and then read whole, up to the next one
# that opens, the plus sign and its blanks are one optional piece, and the
# blanks before either sign are taken whole: scanned on from each SPP in it, an
# unclosed bracket was read to its end again from every one, a line of
# unclosed formulas was read to the end of the text from each, and the blanks
# were split between two runs in every way. The variable value is read whole too, as a shorter one leaves the
# same line to search for "formule".
_INJECTION_SPP_SPLIT_RE = re.compile(
    _rest_of_line(_INJECTION_LABEL)
    + r"\s*VAST\s+(\d+)\s*%\s*[×xX*]\s*([\d,]+)\s*euro[^\n]*\n"
    r"\s*VARIABEL\s*(?:\+\s*)?(\d+)\s*%\s*[×xX*]\s*+"
    rf"([{SIGN_CHARS}]?)\s*([\d,]++)[^\n]*?formule\s+"
    r"([\d,]+)\s*[×xX*]\s*([\d,]+)\s*\[(?=[^\][]*SPP)[^\][]*+\]\s*+"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)",
    re.IGNORECASE,
)
# No unit follows the SPP formula's own figures, but the card prints, in euro,
# the value it gives at the index printed in it: "VARIABEL +50% × 0,11736873
# euro deze waarde volgt de formule 0,9 × 0,14152081 [EPEX SPP 2] - 0,01". So
# the formula is worked out again. Both figures are printed to eight decimals,
# which puts the formula up to 0,9 x 5e-9 + 5e-9 off the value whatever its
# size, and on the four cards of July to October 2026 it lands within 9e-9 of
# it (September). Twice that is allowed, outright rather than as a share,
# since the rounding does not shrink with a value near zero. A figure read
# ten times out misses by far more.
_SPP_FORMULA_FLOOR = 2e-8
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
        _same_line_injection(text)
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
    multiplier = to_float(split.group(6))
    var_base = parse_sign(split.group(8) or "+") * to_float(split.group(9))
    # Both halves' values have to be in euro and the SPP formula has to give
    # the variable one back, or the formula is on another scale than it is
    # read on and only the printed credit is kept, as when it does not read.
    if not _in_euro(text, split, 2, 5) or not formula_gives(
        parse_sign(split.group(4) or "+") * to_float(split.group(5)),
        multiplier,
        to_float(split.group(7)),
        var_base,
        tolerance=0.0,
        floor=_SPP_FORMULA_FLOOR,
    ):
        return InjectionRates(current=current)
    vast_share = to_float(split.group(1)) / 100.0
    vast_value = to_float(split.group(2))
    var_share = to_float(split.group(3)) / 100.0
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


# The value a card prints for its feed-in index, and the footnote naming the
# month that value is for:
#   "VARIABEL +50% x 0,08046773 euro deze waarde volgt de formule 0,9 x
#    0,10051971 [EPEX SPP 2] - 0,01. -0,0502 euro/kWh"
#   "2 Dit is het werkelijke SPP gewogen gemiddelde van de Day Ahead EPEX
#    (EPEX DA) voor de maand september."
#
# The bracket is read up to the next one that opens, and the footnote's gap to
# its month up to the next such footnote, as _RLP_MONTH_RE's: run on from
# each, a run of formulas with no closing bracket, or a line of footnotes, was
# read again from every one.
_SPP_PRINTED_RE = re.compile(
    r"formule\s+[\d,]+\s*[×xX*]\s*([\d,]+)\s*\[[^\][]*SPP", re.IGNORECASE
)
_SPP_MONTH_RE = re.compile(
    r"werkelijke\s+SPP\s+gewogen\s+gemiddelde"
    r"(?:(?!werkelijke\s+SPP\s+gewogen\s+gemiddelde)[^\n])*?"
    rf"voor\s+de\s+maand\s+({_MONTH_ALT})",
    re.IGNORECASE,
)


def printed_spp_index(text: str) -> tuple[int, float] | None:
    """``(month, EUR/kWh)`` of the settled SPP index the card prints, or None.

    Ecopower publishes a definitive card only once its month has ended, so the
    figure in its feed-in formula is that month's settled SPP-weighted mean,
    and footnote 2 names the month. Both are needed: a figure whose month the
    card does not name settles nothing.
    """
    value = _SPP_PRINTED_RE.search(text)
    month = _SPP_MONTH_RE.search(text)
    if value is None or month is None:
        return None
    return _NL_MONTH_INDEX[month.group(1).lower()], to_float(value.group(1))


# The dynamic card prints the injection formula like the consumption one,
# on the same grid:
#   "Terugleververgoeding elk kwartier 0,00098 × EPEX DA - 0,015 euro/kWh"
_DBS_INJECTION_RE = re.compile(
    r"Terugleververgoeding\s+elk\s+(?:kwartier|uur)\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*+"
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
    # A card stating EPEX DA in another unit leaves the credit out, as one
    # whose formula does not read at all.
    if not match or _dbs_index_unit(text, match, 1) not in _DBS_INDEX_UNITS:
        return None
    factor = to_float(match.group(1)) * 1000.0
    base = parse_sign(match.group(2)) * to_float(match.group(3))
    return InjectionRates(factor=factor, base=base, formula=match.group(0).strip())
