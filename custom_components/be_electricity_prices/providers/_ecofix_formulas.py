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

"""Ecofix: the formulas its cards print for energy and feed-in, Motion's on
the 15-minute Belpex and Flexy's on the monthly BELPEX-RLP-M and
BELPEX-SPP-M, read in the unit the card states for them.

Split out of ``ecofix.py`` so that module stays under a thousand lines.
"""

from __future__ import annotations

import re
from dataclasses import replace

from ._parse import SIGN_CHARS, numeric_row, parse_sign, stated_unit, to_float
from ._rates import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    RlpBlend,
    TariffKind,
    VariableRates,
)
from .base import ExtractorError


def _dynamic_formula_match(text: str, label: str) -> re.Match[str] | None:
    """Return the ``(factor x Belpex 15M) <sign> base`` formula that
    follows ``label`` (``Afname`` for consumption, ``Injectie`` for
    injection). Anchoring each role on its own label rather than indexing
    into a document-order ``findall`` keeps consumption and injection from
    silently swapping if Ecofix reorders the two blocks. The fill between
    the label and its formula is tempered so it can't cross the
    ``Injectie`` label, otherwise a reworded/absent ``Afname`` formula
    would let the Afname anchor reach forward and bind the injection
    formula to consumption.
    """
    return re.search(
        rf"{label}(?:(?![Ii]njectie)[\s\S]){{0,200}}?\(([\d,]+)\s*x\s*Belpex\s*15M\)\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)",
        text,
    )


# The unit the Motion formulas give, from the energy price box they sit in:
# "Energieprijs" over "(€ cent/kWh)", read through the headings of the boxes
# beside it, which the layout puts between the two ("vergoeding Bijdrage
# groene energie"). On the Motion and Motion Online cards of May and of August
# to November 2026 the word starts 159 to 184 characters ahead of the Afname
# formula and 266 to 316 ahead of the Injectie one, with 36 characters of the
# neighbouring headings between it and its bracket. The WKK column's and the
# Walloon renewables' own "(€ cent/kWh)" sit closer to each, so the box is
# matched by its word, capitalised, which nothing else on the card is. As the
# card's only statement it is looked for well past what the cards print: the
# headings between may grow to 200 characters and the box come 600 ahead of
# a formula before a correct card is refused.
_MOTION_UNIT_RE = re.compile(r"Energieprijs[^(]{0,200}\(([^()]*)\)")
_MOTION_UNIT_REACH = 600
# Belpex 15M's own unit, from its definition below the formulas: "de Day-Ahead
# notering per 15 minuten van Nordpool Belgium (uitgedrukt in €/MWh)", on
# every Motion and Motion Online card, the card's only "uitgedrukt". It ends
# 3852 to 4867 characters after a formula. The factor is scaled for that
# unit, so an index restated in c€/kWh under a factor restated to match would
# bill ten times out unless it is read. The blanks after "in" are taken
# whole, or a run of them would be split with the unit every way there is.
_MOTION_INDEX_RE = re.compile(r"\(uitgedrukt\s+in\s++([^()]*)\)")
_MOTION_INDEX = "€/MWh"
_MOTION_INDEX_REACH = 6000
# Flexy's formulas sit under a heading of their own, "Formules voor verbruik &
# injectie (€cent/kWh excl BTW)" on the Flexy cards of May and of August to
# November 2026 and the Flexy Online ones since September, 91 to 305
# characters ahead of a formula, the feed-in one the farthest. The words in
# brackets are taken whole, as they state the VAT basis the formulas are
# grossed from as well as their unit.
_FLEXY_UNIT_RE = re.compile(r"Formules\s+voor\s+verbruik\s*&\s*injectie\s*\(([^()]*)\)")
# Both are compared with their whitespace folded: every card prints the unit
# both as "€ cent/kWh" and as "€cent/kWh" ("Toeslagen (€cent/kWh)" on the
# Motion card), and Ecofix writes the VAT basis "excl BTW" on Flexy's heading
# and "excl. BTW" on Motion's formulas.
_MOTION_UNIT = "€cent/kWh"
_FLEXY_UNITS = frozenset({"€cent/kWhexclBTW", "€cent/kWhexcl.BTW"})
# Nothing ahead of Flexy's heading within this reach states a unit.
_UNIT_REACH = 400


def _formula_unit(
    text: str,
    formula: re.Match[str],
    statement: re.Pattern[str],
    *,
    before: int = _UNIT_REACH,
    after: int = 0,
) -> str | None:
    """The unit ``statement`` states for the formula ``formula`` holds from
    group 1 on, its whitespace folded."""
    unit = stated_unit(
        text,
        statement,
        (formula.start(1), formula.end()),
        before=before,
        after=after,
    )
    return None if unit is None else "".join(unit.split())


def _require_motion_unit(text: str, formula: re.Match[str], label: str) -> None:
    """Raise where the box above the Motion formula under ``label`` states
    another unit than its cards have, or none, and likewise the definition of
    the Belpex 15M it multiplies: read as c€/kWh on a Belpex in EUR/MWh, a
    formula restated in another unit would bill ten or a hundred times out."""
    unit = _formula_unit(text, formula, _MOTION_UNIT_RE, before=_MOTION_UNIT_REACH)
    if unit != _MOTION_UNIT:
        raise ExtractorError(
            f"Ecofix dynamic: {label} formula in {unit or 'no stated unit'}, "
            f"expected € cent/kWh"
        )
    index = _formula_unit(
        text, formula, _MOTION_INDEX_RE, before=0, after=_MOTION_INDEX_REACH
    )
    if index != _MOTION_INDEX:
        raise ExtractorError(
            f"Ecofix dynamic: {label} formula on a Belpex 15M in "
            f"{index or 'no stated unit'}, expected €/MWh"
        )


# Flexy's formulas, one per meter register under "Formules voor verbruik &
# injectie (€cent/kWh excl BTW)":
#     Enkelvoudige meter: (BELPEX-RLP-M * 0,1020) + 1,2000
_RLP_FORMULA = rf"\(BELPEX-RLP-M\s*\*\s*([\d,]+)\)\s*([{SIGN_CHARS}])\s*([\d,]+)"
_REGISTER_FORMULAS = {
    "mono": re.compile(rf"Enkelvoudige meter:\s*{_RLP_FORMULA}"),
    "peak": re.compile(rf"Tweevoudige meter dag:\s*{_RLP_FORMULA}"),
    "offpeak": re.compile(rf"Tweevoudige meter nacht:\s*{_RLP_FORMULA}"),
    "excl_night": re.compile(rf"Uitsluitend nachtmeter:\s*{_RLP_FORMULA}"),
}
# And the feed-in one under the same heading, on the solar-weighted index,
# "Injectie: (BELPEX-SPP-M * 0,0884) - 0,5000".
_SPP_FORMULA_RE = re.compile(
    rf"Injectie:\s*\(BELPEX-SPP-M\s*\*\s*([\d,]+)\)\s*([{SIGN_CHARS}])\s*([\d,]+)"
)


def _flexy_formula(text: str, pattern: re.Pattern[str]) -> re.Match[str] | None:
    """The Flexy formula ``pattern`` finds, or None where the card prints none
    under a heading stating what its cards have stated. Read as c€/kWh before
    VAT, a formula restated in another unit would bill ten or a hundred times
    out, and one stated with VAT would be grossed twice."""
    match = pattern.search(text)
    if match is None or _formula_unit(text, match, _FLEXY_UNIT_RE) not in _FLEXY_UNITS:
        return None
    return match


# The card defines Belpex_RLP as the RLP-weighted mean "dat het rekenkundige
# gemiddelde is van de profielen van de verschillende distributienetbeheerders
# in België", words Eneco, energie.be and DATS 24 use for two different
# blends, so the blend is the one the card's own figures give back: the
# October 2026 card's 19,04 is the formula at 164,34 EUR/MWh, September's
# mean over every DSO column of the Synergrid sheet at 164,42, against 165,19
# on the Flemish curve alone and 162,88 on the equal mean of the three
# regional curves. The May and August cards agree, at March's and June's.
_RLP_BLEND: RlpBlend = "columns"


def _register_formulas(text: str, vat: float) -> dict[str, tuple[float, float]] | None:
    """Each register's ``(factor, base)`` onto a EUR/kWh spot, VAT included,
    or ``None`` when the card does not print all four under the heading
    stating their unit (:func:`_flexy_formula`).

    The card states c€/kWh excluding VAT per EUR/MWh of index, so the factor
    takes a x10 and the base a /100, both grossed by the VAT the card's
    printed figures include.
    """
    out: dict[str, tuple[float, float]] = {}
    for register, pattern in _REGISTER_FORMULAS.items():
        match = _flexy_formula(text, pattern)
        if match is None:
            return None
        factor_c = to_float(match.group(1))
        base_c = parse_sign(match.group(2)) * to_float(match.group(3))
        out[register] = (factor_c * 10.0 * vat, base_c / 100.0 * vat)
    return out


def _extract_energy(
    text: str, kind: TariffKind, yearly_fee: float, vat: float
) -> EnergyRates:
    if kind == "dynamic":
        # Dynamic cards print the consumption formula on the line directly
        # after "Afname <indicative>" e.g.:
        #   Afname 11,74
        #   Prijsformule excl. BTW (0,1010 x Belpex 15M) + 0,9
        # The injection block carries the same shape under "Injectie", so
        # anchor on the "Afname" label rather than taking the first
        # formula in document order.
        formula = _dynamic_formula_match(text, "Afname")
        if formula is None:
            raise ExtractorError("Ecofix dynamic: Afname Belpex 15M formula not found")
        _require_motion_unit(text, formula, "Afname")
        factor_pdf = to_float(formula.group(1))
        base_pdf_cents = parse_sign(formula.group(2)) * to_float(formula.group(3))
        # PDF formula is c€/kWh ex-VAT against Belpex in €/MWh. The card
        # banner prints "Prijzen inclusief X% BTW", which ``parse_snapshot``
        # reads to track future VAT changes without a code update. Conversion
        # to EUR/kWh-against-EUR/kWh-spot: factor stays unitless (x1000/100
        # = x10) and base divides cents->EUR (/100).
        # Motion / Motion Online bill on the 15-minute Belpex spot (the
        # card's "Belpex 15M" formula), so keep the native 15-minute
        # slots like Engie rather than the hourly mean.
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pdf_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
            quarter_hourly=True,
        )

    # Variable (Flexy): formulas on page 4, indicative monthly rate on page 1.
    #   "Maandprijs: 11,81 11,81 11,81 11,81"
    # The four columns are (mono, peak, off-peak, exclusive_night) at the
    # same rate for every meter type today, so we surface them all.
    # Bounded to the Verbruik block: the Injectie block below prints its own
    # Maandprijs row, and a lookup that reaches it bills a household's
    # consumption at the injection credit.
    consumption = numeric_row(
        text, "Maandprijs", 4, after="Verbruik", before="Injectie"
    )
    if not consumption:
        raise ExtractorError("Ecofix Flexy: consumption Maandprijs row not found")
    mono = to_float(consumption[0]) / 100.0
    peak = to_float(consumption[1]) / 100.0
    offpeak = to_float(consumption[2]) / 100.0
    excl = to_float(consumption[3]) / 100.0

    formula_match = _flexy_formula(text, _REGISTER_FORMULAS["mono"])
    formula_str: str | None = None
    if formula_match:
        formula_str = (
            f"(BELPEX-RLP-M * {formula_match.group(1)}) "
            f"{formula_match.group(2)} {formula_match.group(3)} c€/kWh ex-VAT"
        )

    rates = VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl,
        yearly_fixed_fee=yearly_fee,
        formula=formula_str,
    )
    # The contract bills "op basis van de index die van toepassing is tijdens
    # de periode waarvoor je wordt gefactureerd", so a month settles on its
    # own BELPEX-RLP-M. The Maandprijs is the same formula at an earlier
    # month's index, and which one varies: March's on the May 2026 card,
    # June's on August's and still on September's, September's on October's.
    # The formulas are kept with ``month_indexed`` and resolved against the
    # delivery month's mean, the printed figures staying what an entry with
    # no day-ahead prices is billed. A card missing one of the four keeps its
    # printed figures alone, which the live check reports against the
    # registry flag.
    formulas = _register_formulas(text, vat)
    if formulas is None:
        return rates
    return replace(
        rates,
        formula_factor=formulas["mono"][0],
        formula_base=formulas["mono"][1],
        formula_factor_peak=formulas["peak"][0],
        formula_base_peak=formulas["peak"][1],
        formula_factor_offpeak=formulas["offpeak"][0],
        formula_base_offpeak=formulas["offpeak"][1],
        formula_factor_exclusive_night=formulas["excl_night"][0],
        formula_base_exclusive_night=formulas["excl_night"][1],
        month_indexed=True,
        rlp_indexed=True,
        rlp_blend=_RLP_BLEND,
    )


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates:
    """Parse the injection formula + indicative rate.

    Belgian residential injection is VAT-exempt, so the formula is
    surfaced as-is from the ex-VAT card values. Convention matches
    Cociter / OCTA+: factor scaled to per-EUR/kWh-spot.
    """
    if kind == "dynamic":
        # Anchor on the "Injectie" label rather than taking the second
        # Belpex 15M formula in document order, so a reordered card can't
        # bind the consumption formula to the injection role.
        inj_formula = _dynamic_formula_match(text, "Injectie")
        if inj_formula is None:
            # Every Ecofix dynamic card prints the injection Belpex 15M
            # formula; a miss is a layout drift. Raise rather than return
            # None (which the pipeline treats as a zero credit), and do
            # not fall back to the indicative alone, which would freeze
            # this spot-indexed injection at a flat rate. Matches the
            # Flexy branch and the dynamic consumption raise.
            raise ExtractorError("Ecofix dynamic injection: Belpex 15M formula missing")
        _require_motion_unit(text, inj_formula, "Injectie")
        factor_pdf = to_float(inj_formula.group(1))
        base_pdf_cents = parse_sign(inj_formula.group(2)) * to_float(
            inj_formula.group(3)
        )
        # Injection indicative rate ("Injectie 4,83") sits next to the
        # formula; surfaced as ``current`` so consumers without a live
        # spot still get a plausible value.
        current_row = numeric_row(text, "Injectie", 1)
        current = to_float(current_row[0]) / 100.0 if current_row else None
        return InjectionRates(
            current=current,
            factor=factor_pdf * 10.0,
            base=base_pdf_cents / 100.0,
            formula=(
                f"({inj_formula.group(1)} x Belpex 15M) {inj_formula.group(2)} "
                f"{inj_formula.group(3)} c€/kWh ex-VAT"
            ),
        )

    # Flexy variable: injection settles on BELPEX-SPP-M, the solar-weighted
    # MONTHLY index, and the card says which month: "worden berekend op basis
    # van de index die van toepassing is tijdens de periode waarvoor je wordt
    # gefactureerd bij de afrekening van je reele verbruik en desgevallend
    # injectie". The printed Maandprijs is not that month's: on the Mei 2026
    # card 4,32 c/kWh inverts through the card's own formula to an index of
    # 54,52, which is MARCH's, two months back.
    #
    # The coefficients are surfaced with spp_indexed, which routes them to the
    # delivery month's own weighted mean and keeps them away from the hourly
    # spot. Emitting them without that flag is what the old comment feared,
    # and it was right to.
    # The injection row prints a "/" where the card has no exclusive-night
    # credit, so its width is the card's to choose; only the first column is
    # read. The block bound is what keeps this off the consumption row above.
    current_block = numeric_row(text, "Maandprijs", after="Injectie")
    if not current_block:
        # Every Flexy card prints the monthly indicative; a miss is a
        # layout drift, not a fee-free contract. Fail loud rather than
        # emit a spot-shaped credit the pipeline cannot price.
        raise ExtractorError("Ecofix Flexy injection: monthly indicative missing")
    formula_match = _flexy_formula(text, _SPP_FORMULA_RE)
    factor: float | None = None
    base: float | None = None
    if formula_match is not None:
        # The card states c/kWh per EUR/MWh of index, so the factor carries a
        # x10 onto a EUR/kWh spot and the base a /100. Injection is VAT-exempt,
        # so neither is grossed.
        factor = to_float(formula_match.group(1)) * 10.0
        base = (
            parse_sign(formula_match.group(2))
            * to_float(formula_match.group(3))
            / 100.0
        )
    return InjectionRates(
        current=to_float(current_block[0]) / 100.0,
        factor=factor,
        base=base,
        spp_indexed=factor is not None,
        formula=formula_match.group(0) if formula_match else None,
    )
