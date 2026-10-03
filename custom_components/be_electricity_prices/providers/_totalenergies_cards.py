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

"""TotalEnergies card parsers: the energy leg, the injection leg and the card's
dates.

Split out of ``totalenergies.py``, which had reached the package's size
limit. These are the parsers that read a TotalEnergies card's own product
figures: the formulas and the index each billed figure solves to, the
consumption row and the indicative block, the green contribution a card
folds into its prices, the yearly fee, the feed-in leg, the publication month
and the Walloon connection fee. The grid and levy overlays live in
``_totalenergies_overlays.py``; ``totalenergies.py`` keeps the registry, the
URLs, the fetch and ``parse_snapshot``, which calls into both.

No behaviour change: every function here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

import re
from dataclasses import fields, replace

from ._pdf import vat_multiplier
from ._parse import SIGN_CHARS, parse_sign, to_float
from .base import ExtractorError
from ._rates import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    TariffKind,
    VariableRates,
    fixed_or_variable_rates,
)
from ._totalenergies_overlays import (
    _extract_fee_and_renewables,
    cev_included,
    consumption_row,
)


# ---- energy block -------------------------------------------------------------


# Brussels Dynamic prints "<factor> * BELPEXH +" on one line and the bases
# on the next: "0.1034 * BELPEXH + ... + Formule tarifaire\n3.85 3.85 ...".
# _resolve_consumption_formula handles both the same-line and split-line
# layouts via these two patterns.
_FACTOR_ONLY_RE = re.compile(rf"([\d.,]+)\s*\*\s*BELPEXH\s*([{SIGN_CHARS}])")
_BASE_AFTER_FORMULE_RE = re.compile(r"Formule tarifaire\s*\n\s*([\d.,]+)")


def _resolve_consumption_formula(text: str) -> tuple[float, float, float] | None:
    """Return ``(factor, sign, base_cents)`` for the consumption formula.

    The consumption formula always appears before the injection formula
    in TotalEnergies's PDFs, so the FIRST ``factor * BELPEXH`` match is
    always the consumption one. Wallonia and Flanders print the base on
    the same line (``0.1034 * BELPEXH + 1.75``); Brussels splits the
    formula across two lines (``0.1034 * BELPEXH +`` then ``3.85`` after
    ``Formule tarifaire``).
    """
    first_match = _FACTOR_ONLY_RE.search(text)
    if first_match is None:
        return None
    factor = to_float(first_match.group(1))
    sign = parse_sign(first_match.group(2))

    # Same-line base: a complete number, terminated by whitespace or
    # end-of-string, that is NOT followed by another ``* BELPEXH``
    # (which would be the next column's formula). The trailing
    # ``(?=\s|$)`` blocks the regex engine from backing off ``[\d.,]+``
    # to a shorter match (e.g. capturing ``0.103`` out of ``0.1034``).
    tail_re = re.compile(
        re.escape(first_match.group(0)) + r"\s*([\d.,]+)(?=\s|$)(?!\s*\*\s*BELPEXH)"
    )
    tail = tail_re.search(text)
    if tail is not None:
        return factor, sign, to_float(tail.group(1))

    after_formule = _BASE_AFTER_FORMULE_RE.search(text)
    if after_formule is None:
        return None
    return factor, sign, to_float(after_formule.group(1))


# Impact prints its energy rate once per CWaPE band, every other card once per
# meter reading.
_IMPACT_BANDS = 3

# "TVA 6 % incluse" under the prices, and "(hors TVA 6%)" on the formulas.
_VAT_RE = re.compile(r"TVA\s*(\d+)\s*%")


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(text, _VAT_RE)


def _extract_energy(text: str, kind: TariffKind, columns: int = 4) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text, columns)
    if kind == "dynamic":
        consumption = _resolve_consumption_formula(text)
        if consumption is None:
            raise ExtractorError("could not parse TotalEnergies dynamic formula")
        factor_pdf, sign, base_pre_vat_cents_value = consumption
        vat = _vat_multiplier(text)
        # PDF formula yields c€/kWh (HTVA) from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=sign * base_pre_vat_cents_value * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
        )

    # Variable cards index monthly: the price actually billed is the
    # realized monthly indicative ("prix mensuels calcules sur base de la
    # derniere valeur connue du BELPEX_M_RLP"), not the Vlaamse-Nutsregulator
    # annual ESTIMATE in the table below. The realized block also carries
    # the flat supplier energy of the 3-band Impact card (printed as Heures
    # PIC/MEDIUM/ECO), which the standard 4-column table layout does not
    # expose. Prefer it; fall back to the table estimate only when absent.
    pairs = _consumption_month_formula(text) if kind == "variable" else None
    if kind == "variable":
        if pairs is None and cev_included(text) is not None:
            # Every card stating the contribution in its footnote prints the
            # formula too, and without it no figure can be checked below.
            raise ExtractorError("TotalEnergies: variable formula not found")
        realized = _realized_monthly_consumption(text)
        if realized is not None and _priced_on_formula(realized, pairs, text):
            # The realized row is that indicative, so it is what a keyless
            # entry keeps; the formula beside it is what re-prices the
            # delivery month for one carrying an ENTSO-E key.
            return _with_month_formula(
                VariableRates(
                    current=realized[0],
                    peak=realized[1],
                    offpeak=realized[2],
                    exclusive_night=realized[3],
                    yearly_fixed_fee=yearly_fee,
                ),
                text,
            )

    # Static / variable table row: 4 space-separated values (mono / jour /
    # nuit / excl_nuit) on a single line. The layout drifts per contract:
    # asterisk count after "Consommation" varies (0-3); for static the
    # values follow directly, for variable a "Tarif mensuel" label sits
    # between. The four values are separated by [ \t]+ (never a newline)
    # and the row ends at the line break: a 3-column card must miss and
    # fail loud here rather than spanning the newline to grab the yearly
    # fee as exclusive_night. For fixed this is the actual fixed price; for
    # a variable card without a realized block it is the V-test fallback.
    #
    # The October 2026 fixed cards print the yearly fee as the row's first
    # figure and the header after the four rates, which the second pattern
    # reads; the first cannot match that row, since it ends at the fourth.
    consumption_match = re.search(
        r"Consommation\*{0,5}\s*\n(?:\s*Tarif\s+(?:annuel|mensuel)\s*\n)?[ \t]*"
        r"([\d.,]+)[ \t]+([\d.,]+)[ \t]+([\d.,]+)[ \t]+([\d.,]+)[ \t]*(?:\n|$)",
        text,
    )
    if consumption_match:
        mono, peak, offpeak, excl_night = (
            to_float(c) / 100.0 for c in consumption_match.groups()
        )
    elif row := consumption_row(text, columns):
        if columns == _IMPACT_BANDS:
            # Impact's energy leg does not band: the three bands are the
            # network side, and the card prints the one rate in each.
            if len(set(row[1])) != 1:
                raise ExtractorError(
                    "TotalEnergies: Impact prints different energy rates per band"
                )
            mono, peak, offpeak, excl_night = row[1][0] / 100.0, None, None, None
        else:
            mono, peak, offpeak, excl_night = (rate / 100.0 for rate in row[1])
    else:
        raise ExtractorError(f"could not parse TotalEnergies {kind} consumption block")
    if kind == "variable" and not _priced_on_formula(
        (mono, peak, offpeak, excl_night), pairs, text
    ):
        raise ExtractorError(
            "TotalEnergies: the printed rates are not the card's formula at any index"
        )
    rates = fixed_or_variable_rates(
        kind,
        single=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )
    return _with_month_formula(rates, text)


# The variable cards index on BELPEXM_RLP over the DELIVERY month and print,
# beside the formula, "les prix mensuels calcules sur base de la derniere
# valeur connue du BELPEX_M_RLP (du mois precedent)". So the printed row is an
# indicative at LAST month's index, the same shape Cociter, Engie and Mega
# print, and billing it bills a month behind.
#
# The table flattens into two lines, the four factors on the first and the
# four bases on the second, one column per meter reading:
#
#   0.1099 * 0.1212 * 0.1 * BELPEXM_RLP + 0.1056 * Formule tarifaire
#   BELPEXM_RLP + 2.26 BELPEXM_RLP + 2.26 2.26 BELPEXM_RLP + 2.16
#
# Every dot-decimal number in the formula block, in the order printed. Not a
# formula pattern despite where it is used, which is what the name used to
# claim, and energyvision.py has a real one under that name.
#
# A factor is below 1 and a base above it on every card seen, which is what
# separates the two runs without depending on where the index token lands.
# A count the caller does not recognise is a layout it cannot read, and it
# then keeps the printed row rather than billing a half-read formula.
_DOT_DECIMAL_RE = re.compile(r"\d+\.\d+")


def _consumption_month_formula(text: str) -> list[tuple[float, float]] | None:
    """The ``factor * BELPEXM_RLP + base`` pairs the card prints, in column
    order, or ``None`` for a layout this cannot read.

    Two layouts. The four meter columns of a standard variable card print
    four pairs, verified on the September 2026 cards by inverting each
    against its own printed rate: all four solve to the same index to within
    0,3 EUR/MWh, which four independent columns only do when the pairing is
    right. Impact prints ONE pair repeated once per CWaPE band, because its
    energy leg does not band at all (the bands are the network side), and it
    inverts to the same 135,07 EUR/MWh the four sibling cards do for the same
    month.

    Any other count is refused rather than guessed at: a layout that moved,
    or a four-column card of which one column was read, and billing a
    mispaired coefficient is worse than billing the printed row.
    """
    index = text.find("BELPEXM_RLP")
    if index < 0:
        return None
    line_start = text.rfind("\n", 0, text.rfind("\n", 0, index)) + 1
    # The formula can be the last thing on the page (Impact keeps it on one
    # line), and a missing newline after it is the end of the text, not a
    # layout this cannot read.
    after = text.find("\n", index)
    line_end = text.find("\n", after + 1) if after >= 0 else -1
    block = re.sub(r"\s+", " ", text[line_start : line_end if line_end >= 0 else None])
    numbers = [float(n) for n in _DOT_DECIMAL_RE.findall(block)]
    factors = [n for n in numbers if n < 1.0]
    bases = [n for n in numbers if n >= 1.0]
    if len(factors) != len(bases) or not factors:
        return None
    pairs = list(zip(factors, bases, strict=True))
    if len(pairs) == 4:
        return pairs
    if len(pairs) == 3 and len(set(pairs)) == 1:
        # Impact's three CWaPE bands, all printing the one formula its energy
        # leg has. Three IDENTICAL pairs and no other count: a single pair is
        # a four-column card of which one column was read, and returning it
        # would put one meter's coefficients on every meter.
        return pairs[:1]
    return None


def _with_month_formula(rates: EnergyRates, text: str) -> EnergyRates:
    """Attach the delivery-month formula to a variable leg, if the card has one.

    Returns ``rates`` untouched for any other shape, and for a layout
    :func:`_consumption_month_formula` cannot read, which leaves the card
    billing its printed row exactly as before.

    A card printing ONE formula gets it on the single rate and nothing else.
    Impact is that card: it repeats the pair once per CWaPE band because its
    ENERGY leg does not band at all, the bands being the network side, and
    its leg carries no peak, off-peak or night column to hold a coefficient
    for. It solves to the same index as the four-column cards, 135,07 EUR/MWh
    on the September 2026 Wallonia set, so leaving it out kept one product of
    the range a month behind the rest.
    """
    if not isinstance(rates, VariableRates):
        return rates
    pairs = _consumption_month_formula(text)
    if pairs is None:
        return rates
    # Same conversion the dynamic branch above states, because it is the same
    # card printing the same kind of formula: the PDF yields c EUR/kWh HTVA
    # from an index in EUR/MWh, while the engine holds spots in EUR/kWh.
    #
    #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
    #   base_eur_kwh   = base_cents * vat / 100
    #
    # Dividing both by 100 instead left the factor a thousand times too small
    # and dropped the VAT: the September card's mono column resolved to
    # 0,02275 EUR/kWh against the 0,18140 it prints, about 555 EUR a year at
    # 3500 kWh. With this conversion it reproduces the printed figure exactly,
    # which is what says both the scale and the VAT reading are right.
    vat = _vat_multiplier(text)
    # BELPEXM_RLP is the day-ahead weighted by the residual load profile,
    # which the injection leg's plain BELPEXM is not: the guard on
    # _MONTH_FORMULA_RE exists to keep the two apart.
    if len(pairs) == 1:
        factor, base = pairs[0]
        return replace(
            rates,
            month_indexed=True,
            rlp_indexed=True,
            formula_factor=factor * vat * 10.0,
            formula_base=base * vat / 100.0,
        )
    (fm, bm), (fp, bp), (fo, bo), (fn, bn) = pairs
    return replace(
        rates,
        month_indexed=True,
        rlp_indexed=True,
        formula_factor=fm * vat * 10.0,
        formula_base=bm * vat / 100.0,
        formula_factor_peak=fp * vat * 10.0,
        formula_base_peak=bp * vat / 100.0,
        formula_factor_offpeak=fo * vat * 10.0,
        formula_base_offpeak=bo * vat / 100.0,
        formula_factor_exclusive_night=fn * vat * 10.0,
        formula_base_exclusive_night=bn * vat / 100.0,
    )


# The per-kWh fields of an energy leg that carry the contribution when a card
# folds it in: every printed rate and every formula's base. A factor scales the
# index and carries none of it.
_RENEWABLES_CARRIERS = frozenset(
    {
        "single",
        "current",
        "peak",
        "offpeak",
        "exclusive_night",
        "base",
    }
)
_FORMULA_CARRIERS = frozenset(
    {
        "formula_base",
        "formula_base_peak",
        "formula_base_offpeak",
        "formula_base_exclusive_night",
    }
)

# How far apart, in EUR/kWh of index, the columns of one card solve. A card
# read the right way agrees to within 1,25 EUR/MWh on every TotalEnergies card
# of 1 October 2026, the figures being rounded to a hundredth of a cent; read
# the wrong way, with or without the contribution in its bases, the same cards
# miss by 3,04 EUR/MWh and more. A row the card prints as an estimate rather
# than at one index (the morning Electricité Variable card in Flanders, 1,77
# and 3,14) fits neither, and settles nothing.
_INDEX_FIT = 0.0013
_INDEX_MISFIT = 0.003


def _formulas_hold_contribution(rates: VariableRates, included: float) -> bool:
    """Whether the card's formula bases carry the contribution its footnote
    says they do.

    Every column of a card bills the formula at the same index, so the
    printed rates solve to one index under the right reading. The myEssential
    card in Brussels of 1 October 2026 says its formulas include the
    contribution and prints bases without it: its four columns solve to
    186,7 to 191,7 EUR/MWh with the contribution in the bases and to 164,7 to
    165,0 without, where every other card of the range agrees to within 1,3
    EUR/MWh with it in. The footnote stands unless the figures settle it the
    other way, and a card with one column cannot say.

    Nor can a Flemish card. The readings drift apart by the contribution times
    the spread of ``1 / factor`` across the columns, and Flanders' 1,57 c/kWh
    misses by only 2,65 to 2,69 EUR/MWh on the October 2026 cards, under
    ``_INDEX_MISFIT``: a Flemish formula printed without it would be read as
    the footnote says. None is printed that way.
    """
    columns = [
        (rate, factor, base)
        for rate, factor, base in (
            (rates.current, rates.formula_factor, rates.formula_base),
            (rates.peak, rates.formula_factor_peak, rates.formula_base_peak),
            (rates.offpeak, rates.formula_factor_offpeak, rates.formula_base_offpeak),
            (
                rates.exclusive_night,
                rates.formula_factor_exclusive_night,
                rates.formula_base_exclusive_night,
            ),
        )
        if rate is not None and factor and base is not None
    ]
    if len(columns) < 2:
        return True

    def spread(shift: float) -> float:
        indices = [(rate - shift - base) / factor for rate, factor, base in columns]
        return max(indices) - min(indices)

    return not (spread(0.0) >= _INDEX_MISFIT and spread(included) <= _INDEX_FIT)


def _without_renewables(energy: EnergyRates, included: float) -> EnergyRates:
    """``energy`` with the green energy contribution taken back out.

    A card that says its prices and formulas include the contribution bills
    it inside every kWh, while the snapshot holds it apart in
    ``TaxOverlay`` like every other card. Leaving it in both places would
    bill it twice; taking it out keeps it in the tax leg, where a change in
    the law reaches a fixed contract the way the card says it does. The
    figures are all VAT-inclusive EUR/kWh by then, the basis the footnote
    states it on. A formula printed without it, against its own footnote,
    is left as printed (:func:`_formulas_hold_contribution`).
    """
    carriers = _RENEWABLES_CARRIERS
    if not isinstance(energy, VariableRates) or _formulas_hold_contribution(
        energy, included
    ):
        carriers = carriers | _FORMULA_CARRIERS
    return replace(
        energy,
        **{
            f.name: getattr(energy, f.name) - included
            for f in fields(energy)
            if f.name in carriers and getattr(energy, f.name) is not None
        },
    )


def _extract_yearly_fee(text: str, columns: int = 4) -> float:
    fee, _ = _extract_fee_and_renewables(text, columns)
    return fee


def _extract_publication_month(text: str) -> str:
    # The October 2026 myComfort Fixe card in Flanders spells the brand
    # "Total Energies" in its title.
    match = re.search(
        r"Total\s?Energies\s+(?:my\w+|Electricit[eé]\w*|Impact)[^\n]*\n"
        r"([a-zéûÉ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


# The realized monthly indicative block: "A titre indicatif ... les prix
# mensuels calcules sur base de la derniere valeur connue du BELPEX_M_RLP".
# This is the price actually billed; the consumption/injection rows in the
# table above it are the Vlaamse-Nutsregulator ANNUAL ESTIMATE.
_MONTHLY_BLOCK_RE = re.compile(r"prix mensuels[\s\S]{0,420}")


def _realized_monthly_consumption(
    text: str,
) -> tuple[float, float | None, float | None, float | None] | None:
    """Realized monthly consumption rates (single/peak/offpeak/excl_night).

    In the block the consumption column is printed first and the injection
    column second, so the consumption value is the first match of each
    meter label. The Impact card prints a single flat supplier rate under
    Heures PIC/MEDIUM/ECO (the band split is DSO-side), so when the
    standard bi-hourly labels are absent the PIC value is the single rate.
    Returns None when the block is absent.
    """
    block = _MONTHLY_BLOCK_RE.search(text)
    if block is None:
        return None
    body = block.group(0)

    def first(label: str) -> float | None:
        m = re.search(label + r"\s*:\s*([\d.,]+)", body)
        return to_float(m.group(1)) / 100.0 if m else None

    mono = first(r"Compteur Simple")
    peak = first(r"Heures Pleines")
    offpeak = first(r"Heures Creuses")
    excl_night = first(r"Compteur Excl\.?\s*Nuit")
    if mono is not None and peak is not None and offpeak is not None:
        return mono, peak, offpeak, excl_night
    # Impact card: flat supplier energy printed as Heures PIC/MEDIUM/ECO.
    pic = first(r"Heures PIC")
    if pic is not None:
        return pic, None, None, None
    return None


# The lowest index a printed figure may solve to, in EUR/MWh. A figure is a
# price only when the card's formula yields it at a real index, and no month
# of the day-ahead market has averaged anywhere near zero; a figure printed
# at no index at all solves to zero give or take the rounding of its last
# digit, which is a few hundredths.
_MIN_INDEX = 1.0


def _priced_on_formula(
    rates: tuple[float | None, ...],
    pairs: list[tuple[float, float]] | None,
    text: str,
) -> bool:
    """Whether every one of ``rates`` is the card's formula at an index.

    ``rates`` are EUR/kWh as printed, VAT included; ``pairs`` are the
    ``factor * BELPEXM_RLP + base`` the card prints beside them, HTVA, in
    the same column order (one pair for Impact). Solving each rate for the
    index must give a positive one: a figure at or below its formula's base
    is no price at any index.

    The October 2026 cards printed their "A titre indicatif" block with the
    index term left out, the formula's base standing where the price should
    be (3,87 under "Compteur Simple" beside "0.1098 * BELPEXM_RLP + 3.87").
    Read as the price it billed 2,30 c/kWh where the card's own monthly rate
    is 22,87. The myComfort card in Brussels printed 7.01 under every meter
    where its exclusive-night base is 6.91, so no comparison of figures with
    bases could catch every variant; solving for the index does, since each
    of those figures is below its base once the VAT comes off.

    A card without a readable formula cannot be checked, and passes.
    """
    if pairs is None:
        return True
    vat = _vat_multiplier(text)
    indices = [
        (rate * 100.0 / vat - base) / factor
        for rate, (factor, base) in zip(rates, pairs, strict=False)
        if rate is not None
    ]
    return bool(indices) and min(indices) >= _MIN_INDEX


def _realized_monthly_injection(text: str) -> float | None:
    """Realized monthly injection indicative.

    Injection is the last "Compteur Simple" value in the block (the
    second/injection column on a variable card, the only one on a fixed
    card). Returns None when the block is absent.
    """
    block = _MONTHLY_BLOCK_RE.search(text)
    # The October 2026 variable cards offer no feed-in and head the block
    # with "Consommation" alone. Its last "Compteur Simple" is then a
    # consumption figure, which credited 3,87 c/kWh of feed-in no card offers.
    if block is None or "Injection" not in block.group(0):
        return None
    # Injection is the last "Compteur Simple" value (standard cards) or the
    # last "Heures PIC" value (Impact cards); both are the second/injection
    # column on the line, uniform across meters/bands.
    vals = re.findall(r"Compteur Simple\s*:\s*([\d.,]+)", block.group(0)) or re.findall(
        r"Heures PIC\s*:\s*([\d.,]+)", block.group(0)
    )
    if not vals:
        return None
    return to_float(vals[-1]) / 100.0


# The non-dynamic cards print the injection formula under the
# "Injection*** (Compensation ...)" heading. Anchoring on that heading is
# required rather than reusing the dynamic branch's column-1 prefix: on two of
# the three fixtures the formula lands in the LAST column, not the first.
#
# The two optional "/" allow for the "Compteur excl. nuit" column printing a
# literal slash between the factor and the index, or between the index and the
# sign, depending on the card. \s* spans the newline that carries all three
# layouts.
#
# BELPEXM(?!_) is the guard that matters. The CONSUMPTION formula on the same
# page reads "0.1099 * BELPEXM_RLP + 2.03", a DIFFERENT, load-profile-weighted
# index; without the lookahead the search returns that instead and credits
# injection at roughly six times the right coefficient.
_INJECTION_HEADING_RE = re.compile(r"Injection\*{0,5}\s*\(Compensation[^\n]*\n")
_MONTH_FORMULA_RE = re.compile(
    rf"([\d.,]+)\s*\*\s*/?\s*BELPEXM(?!_)\s*/?\s*([{SIGN_CHARS}])\s*([\d.,]+)"
)


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    indicative = re.search(
        r"Injection\*{0,5}[^\n]*\n\s*([\d.,]+)",
        text,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    month_indexed = False
    if kind == "dynamic":
        if "injection" not in text.lower():
            # The October 2026 myDynamic cards, like every other card
            # republished that month, offer no feed-in price and do not
            # mention injection anywhere: nothing to credit.
            return None
        # Injection block always prints the formula cleanly on one line
        # ("0.1 * BELPEXH -1.3 ..."). Anchor the search after "Injection"
        # so the consumption formula above can never be picked up.
        match = re.search(
            rf"Injection\*{{0,5}}[^\n]*\n[^\n]*\n\s*([\d.,]+)\s*\*\s*BELPEXH\s*"
            rf"([{SIGN_CHARS}])\s*([\d.,]+)",
            text,
        )
        if match is None:
            # A dynamic contract must price injection off the live spot via
            # factor*BELPEXH + base. Without the formula the snapshot would
            # silently fall back to the flat monthly indicative for every
            # hour - fail loud like the consumption side rather than ship a
            # wrong-shaped credit.
            raise ExtractorError(
                "TotalEnergies dynamic injection: BELPEXH formula not found"
            )
        f_pdf = to_float(match.group(1))
        b_cents = parse_sign(match.group(2)) * to_float(match.group(3))
        # Injection is VAT-exempt residential.
        factor = f_pdf * 10.0
        base = b_cents / 100.0
        formula = match.group(0)
    else:
        # Non-dynamic injection is monthly-indexed: the table value read
        # above is the Vlaamse-Nutsregulator ANNUAL ESTIMATE, while the
        # printed monthly figure is the formula at the LAST KNOWN value of
        # the index. The card says so: "Les prix mensuels de l'injection
        # calcules sur base de la derniere valeur connue du Belpex_M", and
        # the Impact card adds "La valeur exacte de l'indice repris dans
        # votre formule n'est connue qu'a la fin du mois en cours". Prefer
        # the printed figure over the annual estimate, then index it.
        realized = _realized_monthly_injection(text)
        if realized is not None:
            current = realized
        head = _INJECTION_HEADING_RE.search(text)
        match = _MONTH_FORMULA_RE.search(text, head.end()) if head else None
        if match is not None:
            # c/kWh per EUR/MWh of index, HTVA, and residential injection is
            # VAT-exempt, so neither coefficient is grossed.
            factor = to_float(match.group(1)) * 10.0
            base = parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0
            month_indexed = True
            formula = " ".join(match.group(0).split())

    if current is None and factor is None:
        return None
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
    )


# ---- taxes --------------------------------------------------------------------


def _extract_connection_fee(text: str) -> float:
    # Called only for Wallonia, where the raccordement is mandatory; raise
    # on a miss rather than silently zero it.
    match = re.search(r"Redevance de raccordement\s+([\d.,]+)", text)
    if match is None:
        raise ExtractorError("TotalEnergies: Wallonia connection fee not found")
    return to_float(match.group(1)) / 100.0


# TotalEnergies prints every card in French and in Dutch, on the same layout
# and with the same figures. The parsers above anchor on the French labels, so
# a Dutch card has those labels put in French before it is read: the October
# 2026 myComfort card for Wallonia was served in Dutch at its French address.
# Only the labels a parser anchors on are listed. A Dutch card using a label
# missing here fails the way a French card would.
_DUTCH_LABELS: tuple[tuple[str, str], ...] = (
    (
        r"omvatten de Bijdrage Groene Energie \(BGE\), waarvan het bedrag als "
        r"volgt wordt vastgesteld",
        "comprennent la Contribution Énergie Verte (CEV), dont le montant est fixé à :",
    ),
    (r"(?m)^Verbruik(?=\**$)", "Consommation"),
    (r"Maandelijkstarief", "Tarif mensuel"),
    (r"Jaarlijkstarief", "Tarif annuel"),
    (r"BTW\s*(\d+)\s*%\s*inbegrepen", r"TVA \1 % incluse"),
    (r"TotalEnergies Elektriciteit", "TotalEnergies Electricité"),
    (r"Verbruik tussen ([\d.]+) & ([\d.]+) kWh", r"Consommation entre \1 et \2 kWh"),
    (r"Bijdrage op de energie", "Cotisation sur l’énergie"),
    (r"Aansluitingsvergoeding", "Redevance de raccordement"),
    (r"Ter beschikking gesteld vermogen", "Terme de puissance"),
    (
        r"Bijdrage voor openbaredienstverplichtingen",
        "Droit pour le financement des Obligations de Service Public",
    ),
    (r"maandelijkse prijzen", "prix mensuels"),
    (r"Enkelvoudige Meter:", "Compteur Simple :"),
    (r"Piekuren:", "Heures Pleines :"),
    (r"Daluren:", "Heures Creuses :"),
    (r"Meter Excl\. Nacht:", "Compteur Excl. Nuit :"),
    (r"PIEKuren:", "Heures PIC :"),
)

_DUTCH_MONTHS: dict[str, str] = {
    "januari": "janvier",
    "februari": "février",
    "maart": "mars",
    "april": "avril",
    "mei": "mai",
    "juni": "juin",
    "juli": "juillet",
    "augustus": "août",
    "september": "septembre",
    "oktober": "octobre",
    "november": "novembre",
    "december": "décembre",
}
_DUTCH_MONTH_RE = re.compile(rf"\b({'|'.join(_DUTCH_MONTHS)})(?=\s+\d{{4}})")


def is_dutch_card(text: str) -> bool:
    """Whether the card is the Dutch edition: its title says Tariefkaart."""
    return "Tariefkaart" in text


def in_french(text: str) -> str:
    """The Dutch card's text with the labels the parsers read put in French,
    and its months, so the publication label reads as on a French card."""
    for pattern, french in _DUTCH_LABELS:
        text = re.sub(pattern, french, text)
    return _DUTCH_MONTH_RE.sub(lambda m: _DUTCH_MONTHS[m.group(1)], text)
