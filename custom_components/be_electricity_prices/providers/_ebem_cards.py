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

"""EBEM: the product legs its cards print.

The energy and feed-in formulas of the variable card, its B@sic+ row and the
dynamic card, the prices each card prints beside them and the fees. Split out
of ``ebem.py`` when the formulas came to be worked out again at the index the
card prints, against the price it prints for them. ``ebem.py`` keeps the
listing, the archive, the settlement on the following card and
``parse_snapshot``, which calls into this.
"""

from __future__ import annotations

import re

from ._parse import (
    SIGN_CHARS,
    formula_gives,
    numeric_row,
    parse_sign,
    stated_unit,
    to_float,
)
from ._pdf import vat_multiplier
from ._rates import DynamicRates, EnergyRates, InjectionRates, RlpBlend, VariableRates
from .base import ExtractorError

# The index the card's printed rates were computed on, in EUR/MWh. EBEM says
# the delivery month's own value is not known yet and gives the month just
# closed: "Voor de huidige maand is deze index nog niet gekend, vorige maand
# bedroeg deze index 135,84." Unlike Eneco's footnote it names no month, so
# the card's own month is the anchor: whatever this card is for, the figure
# belongs to the month before it.
# One decimal group, not a run of digits and separators: the sentence ends in a
# full stop some months ("... deze index 98,25.") and a greedy class took it
# into the number. Either separator, since a re-render can flip the decimal.
_PREV_INDEX_RE = re.compile(
    r"vorige maand bedroeg deze index\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE
)
# The feed-in sibling of the line above, on the same card: "De SPP0 vorige
# maand bedroeg 79,11 In de simulator van Ebem ...". Bounded the same way,
# because the sentence that follows it runs straight on and a greedy
# ``[\d.,]+`` swallows the separator.
_PREV_SPP_RE = re.compile(
    r"SPP0 vorige maand bedroeg\s*(\d+(?:[.,]\d+)?)", re.IGNORECASE
)
# The dynamic card works its prices out at the regulator's monthly means
# instead: "maandrekenkundig gemiddeldes: 173,23014 voor de afname en 145,61121
# voor de injectie", the first figure on the line before in some months. A
# figure is read from the start of its digits: tried from each digit of a long
# run, the run was read again from every one.
_VNR_MEANS_RE = re.compile(
    r"(?<![\d.,])(\d++(?:[.,]\d++)?)\s+voor\s+de\s+afname\s+en\s+"
    r"(\d+(?:[.,]\d+)?)\s+voor\s+de\s+injectie",
    re.IGNORECASE,
)

# No EBEM card states the unit of a formula or of the index it multiplies: the
# column is headed "TARIEFFORMULE (variabel tarief)" alone and the index
# sentences above carry none, on every card from January 2025 (the dynamic one
# from October 2025) to November 2026. The formulas are read as c€/kWh
# excluding VAT on an index in EUR/MWh. What a card does print beside each
# formula is the price it gives at that index, excluding VAT: 0,110 x 182,45 +
# 2,2 is the 22,2695 of the November 2026 mono row, 0,0925 x 141,52 - 1,25 the
# 11,8406 of its feed-in. So each formula is worked out again, and the card is
# refused like one whose formula row does not read when its price does not
# come back.
#
# A price has to be its formula at an index within 0,25 EUR/MWh of the one
# printed. Every price on file comes back within its rounding to four
# decimals, 5e-5 c€/kWh where it falls on a tie (20,26255 printed 20,2626),
# but on April 2025's variable card, whose prices were worked out at 94,69
# where it names 94,61. Held on the index rather than as a share of the price,
# that slip passes a feed-in price near zero as it passes a large one, and
# the bound stays well clear of the rounding ties. A base read ten times out,
# as zero or with its sign flipped, moves the index by 10 EUR/MWh or more, a
# factor or an index ten times out by far more, so a slip on one leg misses
# while the other leg comes back. A slip on both legs misses on both, as a
# stale card does, and is told apart from one by ``_STALE_SPREAD`` below.
_INDEX_SLACK = 0.25
# On every dynamic card but February 2026's both prices come back within their
# rounding. That card kept January's prices beside February's means, worked
# out at indices about a tenth under them. A card left stale like that misses on
# both legs, so a card missing on both stands while each price is its formula
# at an index within twice or half the printed mean. Between consecutive months
# on file the means moved by 1,43 times at most (March to April 2026); a factor
# or the means restated in another unit miss by ten times or more.
_STALE_RATIO = 2.0
# A stale card's two prices are both worked out at one earlier month's means,
# and the two means move together: on February 2026's card January's prices sit
# 7,90 and 7,75 EUR/MWh under the printed means, and between consecutive months
# on file the consumption mean and the injection one moved 5,48 EUR/MWh apart
# at most (July to August 2026), 10,36 two months apart. A base read wrong on
# both legs moves the two the opposite ways instead, the consumption base being
# added and the feed-in one taken off: on the bases every card on file prints,
# both read ten times out, as zero or with their signs flipped land 24 EUR/MWh
# apart or more, whatever the means. So a card missing on both legs stands only
# while each is worked out off its mean by the same amount, within this.
_STALE_SPREAD = 12.0

# The unit over the price columns, the only one a card prints near its
# formulas: "(type verbruik) (c€/kWh) (c€/kWh)" over the variable card's EXCL.
# BTW and INCL. BTW columns, "TARIEFFORMULE (c€/kWh) (c€/kWh)" on B@sic+ and
# the dynamic card. Every card on file prints "c€/kWh" there, 9 to 349
# characters ahead of the rows it heads, and nothing else in brackets with a
# slash. The prices are read in c€/kWh on it, so a card restating a formula
# together with its prices, which would still give them back, is refused here.
_PRICE_HEADING_RE = re.compile(r"\(([^()\s/]*+/[^()\s]*+)\)")
_PRICE_HEADING_REACH = 450
_PRICE_UNITS = frozenset({"c€/kWh"})
# A price as the dynamic card prints one, the sign it takes below zero apart,
# taken only as a whole word: a start inside a run of blanks or digits fails at
# once rather than reading the run again.
_PRICE_RE = re.compile(rf"(?<!\S)(?:([{SIGN_CHARS}])\s*+)?(\d++[.,]\d++)(?!\S)")


def _require_price_unit(text: str, product: str, row: re.Match[str]) -> None:
    """Raise unless the prices of ``row`` are headed in c€/kWh."""
    unit = stated_unit(text, _PRICE_HEADING_RE, row.span(), before=_PRICE_HEADING_REACH)
    if unit not in _PRICE_UNITS:
        raise ExtractorError(
            f"{product}: prices headed in {unit or 'no unit'}, expected c€/kWh"
        )


def _gives_back(printed: float, factor: float, index: float, base: float) -> bool:
    """Whether ``printed`` is ``factor`` x ``index`` + ``base`` at an index
    within ``_INDEX_SLACK`` of the one printed, all four as the card prints
    them."""
    return formula_gives(
        printed, factor, index, base, tolerance=0.0, floor=abs(factor) * _INDEX_SLACK
    )


def _worked_out_near(printed: float, factor: float, index: float, base: float) -> bool:
    """Whether ``printed`` is the formula at an index within ``_STALE_RATIO``
    times ``index``, either way."""
    if not factor:
        return False
    worked_at = (printed - base) / factor
    return index / _STALE_RATIO <= worked_at <= index * _STALE_RATIO


def _require_one_month(legs: list[tuple[str, float, float, float, float]]) -> None:
    """Raise unless both legs of a dynamic card missing on both are worked out
    off their printed means by the same amount, within ``_STALE_SPREAD``, as
    prices kept from one earlier month are. Each leg is its name, its price,
    its factor, its mean and its base, and has passed ``_worked_out_near``."""
    off = [(printed - base) / factor - mean for _, printed, factor, mean, base in legs]
    if abs(off[0] - off[1]) > _STALE_SPREAD:
        raise ExtractorError(
            f"EBEM Dyn@mic: its prices are worked out {off[0]:+.2f} and "
            f"{off[1]:+.2f} EUR/MWh off the printed means, not at one month's"
        )


def _missed(
    product: str, row: str, printed: float, factor: float, index: float, base: float
) -> ExtractorError:
    return ExtractorError(
        f"{product}: the {row} formula gives {factor * index + base:.4f} at "
        f"the printed index {index}, the card prints {printed}"
    )


def _printed_index(
    text: str, pattern: re.Pattern[str], product: str, group: int = 1
) -> float:
    """The index the card's prices are worked out at, as ``pattern`` reads
    it in ``group``. Every card prints it, so one that does not is refused."""
    match = pattern.search(text)
    if match is None:
        raise ExtractorError(
            f"{product}: the index its prices are worked out at is not printed"
        )
    return to_float(match.group(group))


def _require_price(
    text: str,
    product: str,
    row: re.Match[str],
    label: str,
    printed: float,
    factor: float,
    base: float,
    index: re.Pattern[str],
) -> None:
    """Raise unless the variable card's ``row`` prints its price in c€/kWh
    and its formula gives that price back at the index ``index`` reads, the
    one the card says last month closed at."""
    _require_price_unit(text, product, row)
    at = _printed_index(text, index, product)
    if not _gives_back(printed, factor, at, base):
        raise _missed(product, label, printed, factor, at, base)


def _prices_beside(text: str, formula: re.Match[str], count: int) -> list[float]:
    """The ``count`` prices the dynamic card prints for ``formula``: after it
    on its line, or else alone on the line above it, the two ways its cards
    have laid them out."""
    after = text[formula.end() :].partition("\n")[0]
    above = text[: formula.start()].rpartition("\n")[0].rpartition("\n")[2]
    for line in (after, above):
        figures = _PRICE_RE.findall(line)
        if len(figures) == count and not _PRICE_RE.sub("", line).strip():
            return [
                parse_sign(sign or "+") * to_float(price) for sign, price in figures
            ]
    raise ExtractorError(f"EBEM Dyn@mic: no price printed beside {formula.group(0)!r}")


# The dynamic card's two formula rows, and what a card missing one says. The
# consumption row starts with "alle uren" but is NOT preceded by "injectie", so
# it does not pick the injection row by accident. Any sign char between the
# factor and the base, so a re-render that flips to a Unicode minus or a
# negative offset does not dead-end the parser. The quote after "15" and the
# blanks after it are one optional piece: as runs of blanks either side of an
# optional quote, a run of blanks was split between the two in every way.
_DYNAMIC_FORMULAS = {
    "consumption": (
        re.compile(
            rf"(?<!injectie\s)\balle uren\s+([\d,.]+)\s+Belpex\s*15\s*(?:[’']\s*)?"
            rf"([{SIGN_CHARS}])\s*([\d,.]+)",
            re.IGNORECASE,
        ),
        "EBEM Dyn@mic: consumption formula not found",
    ),
    # The injection row is on every EBEM card; a miss (e.g. a capitalization
    # re-render) would silently zero a prosumer's feed-in credit, so it fails
    # loud rather than return None.
    "feed-in": (
        re.compile(
            rf"injectie alle uren\s+([\d,.]+)\s+Belpex\s*15\s*(?:[’']\s*)?"
            rf"([{SIGN_CHARS}])\s*([\d,.]+)",
            re.IGNORECASE,
        ),
        "EBEM dynamic injection: 'injectie alle uren' row not found",
    ),
}


def _dynamic_formula(text: str, leg: str) -> tuple[re.Match[str], float, float]:
    """The dynamic card's ``leg`` row, its factor and its signed base, as the
    card prints them."""
    pattern, missing = _DYNAMIC_FORMULAS[leg]
    match = pattern.search(text)
    if match is None:
        raise ExtractorError(missing)
    return (
        match,
        to_float(match.group(1)),
        parse_sign(match.group(2)) * to_float(match.group(3)),
    )


def _require_dynamic_prices(text: str) -> None:
    """Raise unless both dynamic formulas give back the prices printed beside
    them, excluding VAT, at the regulator's means the card names, or both
    miss alike, as a card printed with an earlier month's prices does."""
    means = _VNR_MEANS_RE.search(text)
    if means is None:
        raise ExtractorError(
            "EBEM Dyn@mic: the index its prices are worked out at is not printed"
        )
    legs = []
    for leg, count, group in (("consumption", 2, 1), ("feed-in", 1, 2)):
        match, factor, base = _dynamic_formula(text, leg)
        _require_price_unit(text, "EBEM Dyn@mic", match)
        # The first of the prices beside it excludes VAT, as the formula.
        printed = _prices_beside(text, match, count)[0]
        legs.append((leg, printed, factor, to_float(means.group(group)), base))
    current = [_gives_back(*leg[1:]) for leg in legs]
    if any(current):
        missed = [leg for leg, ok in zip(legs, current, strict=True) if not ok]
    else:
        missed = [leg for leg in legs if not _worked_out_near(*leg[1:])]
        if not missed:
            _require_one_month(legs)
    if missed:
        raise _missed("EBEM Dyn@mic", *missed[0])


# Which blend the running month is estimated on, and the one value on this card
# that is a FIT rather than a settlement. EBEM weights "het gemiddelde van de
# RLP's in Vlaanderen van Synergrid", and none of the three blends reproduces
# the index it publishes: measured against the eight settled months of 2026,
# the mean absolute error is 2,40 EUR/MWh on the distinct curve, 1,80 on the
# columns one and 1,61 here, every blend under-stating in every month, and
# weighting at quarter-hour resolution rather than hourly moves it by a tenth
# of that. So this is the closest of the three, not EBEM's own construction.
#
# It is still worth having: the printed figure this replaces is the PREVIOUS
# month's index, 15,62 EUR/MWh from the delivered one on average, so the live
# rate goes from about 64 EUR/year out at 3.500 kWh to about 7. A closed month
# never uses it, because the following card publishes what the month actually
# settled at and that overrides everything.
_RLP_BLEND: RlpBlend = "flanders"


# Belgian residential VAT applied to the ex-VAT formula factors so the
# stored snapshot value matches the registry's VAT-incl convention. Identical
# to ``cociter.py``'s dynamic conversion: factor * vat * 10 (cents/kWh per
# €/MWh -> EUR/kWh per EUR/kWh), base * vat / 100. Read from the card so
# a future regulator-driven rate change propagates without a code change.
# "INCL. BTW 6%" over the price columns, else "BTW 6%".
_VAT_PATTERNS = (
    re.compile(r"INCL\.?\s*BTW\s*(\d+)\s*%", re.IGNORECASE),
    re.compile(r"BTW\s*(\d+)\s*%", re.IGNORECASE),
)


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(text, *_VAT_PATTERNS)


def _formula_to_dynamic(
    factor_pdf: float, base_pdf_cents: float, vat: float
) -> tuple[float, float]:
    return factor_pdf * vat * 10.0, base_pdf_cents * vat / 100.0


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    if contract_id == "ebem_dynamic":
        # The dynamic card prints "alle uren 0,108 Belpex15' + 1,625" for
        # consumption and "injectie alle uren 0,0925 Belpex15' - 1,10" for
        # injection (_DYNAMIC_FORMULAS).
        _match, factor_pdf, base_pdf_cents = _dynamic_formula(text, "consumption")
        _require_dynamic_prices(text)
        factor, base = _formula_to_dynamic(
            factor_pdf, base_pdf_cents, _vat_multiplier(text)
        )
        yearly_fee = _extract_yearly_fee_abonnement(text)
        # Groen Dyn@mic bills on the 15-minute Belpex spot (the card's
        # "Belpex 15" formula, SMR3 only), so keep the native 15-minute
        # slots like Engie rather than the hourly mean.
        return DynamicRates(
            factor=factor,
            base=base,
            yearly_fixed_fee=yearly_fee,
            quarter_hourly=True,
        )

    if contract_id == "ebem_basic_plus":
        match = re.search(
            rf"Verbruik alle uren\s+([\d,.]+)\s+Belpex\s*([{SIGN_CHARS}])\s*([\d,.]+)",
            text,
        )
        if not match:
            raise ExtractorError("EBEM B@sic+: 'Verbruik alle uren' formula not found")
        factor_pdf = to_float(match.group(1))
        base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
        _require_last_month_price(
            text, "EBEM B@sic+", match, "Verbruik alle uren", factor_pdf, base_pdf_cents
        )
        # B@sic+ has one energy rate for all hours (no peak / off-peak /
        # excl-night energy split), but an exclusive-night meter still bills
        # the shared card's dedicated night fixed fee, not the standard
        # abonnement, so surface it like the Variabel sibling does.
        # Numeric coefficients for signing-cohort re-pricing, in the same
        # VAT-baked EUR/kWh basis as the dynamic path (snapshot vat_rate is 0).
        # The index is the RLP-weighted Belpex; the coordinator applies these
        # against the month's mean weighted by Synergrid's Flemish profile
        # (rlp_blend below), and against the plain arithmetic mean only while
        # that profile is not loaded.
        f_factor, f_base = _formula_to_dynamic(
            factor_pdf, base_pdf_cents, _vat_multiplier(text)
        )
        return VariableRates(
            current=_indicative_from_row(text, "Verbruik alle uren"),
            yearly_fixed_fee=_extract_yearly_fee_abonnement(text),
            yearly_fixed_fee_exclusive_night=_extract_excl_night_fee_variable(text),
            formula=f"({factor_pdf} BelpexRLP0 + {base_pdf_cents}) c€/kWh ex-VAT",
            formula_factor=f_factor,
            formula_base=f_base,
            month_indexed=True,
            rlp_indexed=True,
            rlp_blend=_RLP_BLEND,
        )

    # ebem_variable: parse all four meter-type rows. Mirror the dynamic +
    # injection regexes by accepting any sign char between factor and
    # base; today every row prints '+', but a re-render to a Unicode
    # minus or to a negative offset would otherwise raise.
    rows = {
        "mono": "Enkelvoudige teller",
        "peak": "Dubbele teller piek",
        "offpeak": "Dubbele teller dal",
        "excl_night": "Exclusief nacht",
    }
    parsed: dict[str, tuple[float, float]] = {}
    for key, label in rows.items():
        m = re.search(
            rf"{label}\s+([\d,.]+)\s+Belpex\s*([{SIGN_CHARS}])\s*([\d,.]+)", text
        )
        if not m:
            raise ExtractorError(f"EBEM Groen Variabel: '{key}' formula row not found")
        parsed[key] = (
            to_float(m.group(1)),
            parse_sign(m.group(2)) * to_float(m.group(3)),
        )
        _require_last_month_price(text, "EBEM Groen Variabel", m, label, *parsed[key])
    yearly_fee = _extract_yearly_fee_variable(text)
    # Signing-cohort coefficients, VAT-baked to the EUR/kWh basis (as the
    # dynamic path does; snapshot vat_rate is 0). They resolve against the
    # RLP-weighted monthly mean, the plain one only while no profile is loaded.
    #
    # All four rows are converted, not just the mono one. They were parsed and
    # then dropped, so a bi-hourly cohort was billed the mono pair around the
    # clock: at the card's own May index that is 7,0% low at peak and 8,9% high
    # off-peak, and the SAME entry with no start date already billed both bands
    # correctly off the printed columns - so setting a start date, the feature
    # meant to price a cohort more accurately, destroyed the split.
    vat = _vat_multiplier(text)
    f_factor, f_base = _formula_to_dynamic(*parsed["mono"], vat)
    peak_pair = _formula_to_dynamic(*parsed["peak"], vat)
    offpeak_pair = _formula_to_dynamic(*parsed["offpeak"], vat)
    night_pair = _formula_to_dynamic(*parsed["excl_night"], vat)
    return VariableRates(
        current=_indicative_from_row(text, rows["mono"]),
        peak=_indicative_from_row(text, rows["peak"]),
        offpeak=_indicative_from_row(text, rows["offpeak"]),
        exclusive_night=_indicative_from_row(text, rows["excl_night"]),
        yearly_fixed_fee=yearly_fee,
        yearly_fixed_fee_exclusive_night=_extract_excl_night_fee_variable(text),
        formula=(
            f"mono ({parsed['mono'][0]} BelpexRLP0 + {parsed['mono'][1]}) "
            f"· peak ({parsed['peak'][0]} + {parsed['peak'][1]}) "
            f"· off-peak ({parsed['offpeak'][0]} + {parsed['offpeak'][1]}) "
            f"c€/kWh ex-VAT"
        ),
        formula_factor=f_factor,
        formula_base=f_base,
        formula_factor_peak=peak_pair[0],
        formula_base_peak=peak_pair[1],
        formula_factor_offpeak=offpeak_pair[0],
        formula_base_offpeak=offpeak_pair[1],
        # A separate night circuit is its own row on this card, not the
        # off-peak one; they coincide numerically and are distinct contracts.
        month_indexed=True,
        rlp_indexed=True,
        rlp_blend=_RLP_BLEND,
        formula_factor_exclusive_night=night_pair[0],
        formula_base_exclusive_night=night_pair[1],
    )


def _indicative_from_row(text: str, label: str) -> float:
    """Parse the printed ``INCL. BTW 6% (c€/kWh)`` indicative rate.

    EBEM's variable cards print four numeric columns per row after the
    formula expression: ``EXCL.BTW``, ``INCL.BTW 6%``, ``GESCHATTE
    JAARPRIJS EXCL.BTW``, ``GESCHATTE JAARPRIJS INCL.BTW 6%``. Columns
    1 + 2 are the per-kWh indicative at last-month's Belpex; columns 3 +
    4 use the VNR yearly-forecast Belpex. We surface column 2 (incl-VAT
    per-kWh at last-month's Belpex) as the snapshot's ``current``: it
    matches the value EBEM customers see on their bill more faithfully
    than recomputing against a placeholder spot.
    """
    # The row reads "<label> 0,110 Belpex + 2,2 11,6380 12,3363 13,2781
    # 14,0748": the formula's coefficient and offset, then the four
    # columns. Reading it by figure count rather than by a literal pattern
    # sidesteps the sign EBEM prints between Belpex and the offset, which
    # has been a U+2212 minus in some months, and keeps a short row from
    # borrowing a column off the row below.
    return to_float(_priced_row(text, label)[3]) / 100.0


def _priced_row(text: str, label: str) -> list[str]:
    """The six figures of the variable card's row ``label``: the formula's
    factor and offset, its price at last month's index excluding and including
    VAT, then the same at the VNR's estimate for the year."""
    row = numeric_row(text, label, 6)
    if not row:
        raise ExtractorError(
            f"EBEM Groen Variabel: indicative incl-VAT row for {label!r} not found"
        )
    return row


def _require_last_month_price(
    text: str,
    product: str,
    row: re.Match[str],
    label: str,
    factor: float,
    base: float,
) -> None:
    """Raise unless the consumption row ``label`` gives back the price
    printed beside it excluding VAT, at the index the card says last month
    closed at."""
    printed = to_float(_priced_row(text, label)[2])
    _require_price(text, product, row, label, printed, factor, base, _PREV_INDEX_RE)


def _extract_yearly_fee_variable(text: str) -> float:
    """``Vaste vergoeding (jaarlijkse vaste bijdrage) 80,19 €/jaar 85,00 €/jaar``.

    The card prints both ex-VAT and incl-VAT columns; the registry's
    convention is to store the incl-VAT value (the second number).
    """
    row = numeric_row(text, "Vaste vergoeding (jaarlijkse vaste bijdrage)", 2)
    if not row:
        raise ExtractorError("EBEM Groen Variabel: 'Vaste vergoeding' row not found")
    return to_float(row[1])


def _extract_excl_night_fee_variable(text: str) -> float | None:
    """``Vaste vergoeding exclusief nacht 33,06 €/jaar 35,04 €/jaar``.

    A dedicated yearly fixed fee for an exclusive-night meter circuit,
    billed instead of the standard 'Vaste vergoeding' on an
    exclusive-night config entry. Stores the incl-VAT (second) column.
    Returns None when the card doesn't print it, so the standard fee
    applies.
    """
    row = numeric_row(text, "Vaste vergoeding exclusief nacht", 2)
    return to_float(row[1]) if row else None


def _extract_yearly_fee_abonnement(text: str) -> float:
    """``Abonnement 66,04 €/jaar 70 €/jaar``: B@sic+ and Dyn@mic share the label."""
    row = numeric_row(text, "Abonnement", 2)
    if not row:
        raise ExtractorError("EBEM: 'Abonnement' yearly fee row not found")
    return to_float(row[1])


def _extract_injection(text: str, contract_id: str) -> InjectionRates | None:
    """Parse the injection formula. Belgian residential injection is VAT-exempt."""
    if contract_id == "ebem_dynamic":
        # Belpex 15' is a true hourly-spot formula priced from the
        # live/historical spot via the energy path; surface factor/base.
        row, factor_pdf, base_pdf_cents = _dynamic_formula(text, "feed-in")
        _require_dynamic_prices(text)
        return InjectionRates(
            current=None,
            factor=factor_pdf * 10.0,
            base=base_pdf_cents / 100.0,
            formula=(
                f"({factor_pdf} Belpex15' {row.group(2)} {row.group(3)}) c€/kWh ex-VAT"
            ),
        )

    # Both 'Variabel' and 'B@sic+' settle injection at a MONTHLY rate: the
    # factor/base weight the monthly Belpex-SPP0 average, not the hourly
    # spot. The card prints the realized monthly indicative right after the
    # formula (e.g. "... Belpex - 1,25 1,3354 4,3252", where 1,3354 =
    # factor*last_month_SPP0 + base and 4,3252 is the VNR yearly forecast).
    # Both are kept: the coefficients carry spp_indexed (below), which
    # resolves them against the month's SPP-weighted mean and never against
    # the hourly spot, and the printed indicative is the fallback.
    #
    # The indicative is read with its sign, since below an SPP0 of 13,5
    # EUR/MWh the formula gives a price under zero. The blanks before that
    # sign are taken whole: shared with those after it when no sign is
    # printed, a run of blanks was split between the two in every way.
    match = re.search(
        rf"Injectie alle uren\s+([\d,.]+)\s+Belpex\s*([{SIGN_CHARS}])\s*([\d,.]+)"
        rf"(?:\s++([{SIGN_CHARS}]?)\s*([\d,.]+))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        # Every variable/B@sic+ card prints this row; a miss is a layout
        # drift, not a fee-free contract. Fail loud rather than silently
        # zeroing the feed-in credit.
        raise ExtractorError(
            "EBEM variable injection: 'Injectie alle uren' row not found"
        )
    if not match.group(5):
        # The printed indicative is the fallback when the Synergrid profile is
        # unavailable, so a card that stops printing it is a layout drift, not
        # a fee-free contract. Fail loud rather than leave the credit resting
        # on a formula that cannot always be resolved.
        raise ExtractorError("EBEM variable injection: monthly indicative missing")
    # The card indexes the credit on BelpexSPP0, the SOLAR-weighted monthly
    # mean, and prints a figure computed from LAST month's value of it ("de
    # SPP0 vorige maand bedroeg 27,95"). Surfacing the coefficients with
    # spp_indexed lets the engine resolve the DELIVERY month's own SPP-weighted
    # mean instead, and keeps the printed figure as the fallback for an entry
    # with no Synergrid profile. The formula yields c/kWh from SPP0 in EUR/MWh,
    # so the factor scales by 10 to meet a spot in EUR/kWh and the base by 100.
    printed = parse_sign(match.group(4) or "+") * to_float(match.group(5))
    factor_pdf = to_float(match.group(1))
    base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
    _require_price(
        text,
        "EBEM variable injection",
        match,
        "Injectie alle uren",
        printed,
        factor_pdf,
        base_pdf_cents,
        _PREV_SPP_RE,
    )
    return InjectionRates(
        current=printed / 100.0,
        factor=factor_pdf * 10.0,
        base=base_pdf_cents / 100.0,
        spp_indexed=True,
        formula=(
            f"({to_float(match.group(1))} BelpexSPP0 {match.group(2)} "
            f"{match.group(3)}) c€/kWh ex-VAT"
        ),
    )
