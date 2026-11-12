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

"""Luminus: the product legs its card prints.

The energy formula and its per-slot coefficients, and the feed-in leg. This
is what Luminus charges for power, as opposed to what it collects for someone
else. The signing campaign is read in ``_luminus_promo.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import date

from ._luminus_formulas import (
    _bands_give_printed,
    _expected_index,
    _gives_printed,
    _require_energy_unit,
    _require_unit,
    _signed,
    _signed_cents,
)
from ._luminus_overlays import _NUM
from ._parse import SIGN_CHARS, numeric_row, parse_sign, to_float
from ._pdf import _MONTH_NAMES, vat_multiplier
from ._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    InjectionRates,
    TariffKind,
    TimeOfUseRates,
    VariableRates,
)
from .base import ExtractorError


def _named_month(match: re.Match[str] | None) -> date | None:
    """The first of the month a "<mois> <annee>" match names, or ``None``."""
    if match is None:
        return None
    number = _MONTH_NAMES.get(match.group(1).lower())
    return None if number is None else date(int(match.group(2)), number, 1)


_TRANSITIONAL_HEADING = "Informations sur le prix transitoire"

# The cards reissued on 29 October 2026 name the energy row for what it
# carries: "Énergie fournie + énergie verte" in Wallonia and "Énergie
# fournie, énergie verte + cogénération" in Flanders, wrapped on most
# Flemish cards so that only "verte + cogénération" sits above the figures.
# The annual estimate below repeats the label, so the search stops there.
_ENERGY_LABELS: tuple[str, ...] = (
    "Énergie fournie + énergie verte",
    "Énergie fournie, énergie verte + cogénération",
    "verte + cogénération",
)
_ESTIMATE_HEADING = "Estimation annuelle"


def _energy_row(text: str, columns: int, before: str | None = None) -> list[str] | None:
    """The figures of the card's energy row, under either label."""
    row = numeric_row(text, "Énergie fournie (c€/kWh)", columns, before=before)
    for label in _ENERGY_LABELS:
        if row:
            break
        row = numeric_row(text, label, columns, before=_ESTIMATE_HEADING)
    if row:
        _require_energy_unit(text, row)
    return row


def _band_formula_re(label: str) -> re.Pattern[str]:
    """One per-meter row inside the energy block.

    The two tail guards are load-bearing. ComfyFlex prints a TWO-term formula,
    "x Belpex + 0,0000 x Endex 1-0-3 + 4,2102"; without them ``_NUM``
    backtracks and binds "0,000" as the base, which is how a first cut swept
    the quarterly cards in. A bare ``Belpex`` is also required, so "Belpex M",
    "Belpex RLP M" and the dynamic "Belpex H" cannot match.
    """
    return re.compile(
        rf"{label}\s*=\s*({_NUM})\s*x\s*Belpex\s+"
        rf"([{SIGN_CHARS}])\s*({_NUM})(?![\d,])(?!\s*x)",
        re.S,
    )


def _rlp_band_formula_re(label: str) -> re.Pattern[str]:
    """BasicFlex's per-meter row, "x Belpex RLP M", with the same tail guards.

    A pattern of its own rather than a looser :func:`_band_formula_re`, whose
    bare-Belpex requirement is what keeps the other indices out of MaxxFlex,
    SmartFlex and ComfyFlex.
    """
    return re.compile(
        rf"{label}\s*=\s*({_NUM})\s*x\s*Belpex\s+RLP\s+M\s*"
        rf"([{SIGN_CHARS}])\s*({_NUM})(?![\d,])(?!\s*x)",
        re.S,
    )


def _quarterly_band_formula_re(label: str) -> re.Pattern[str]:
    """ComfyFlex's per-meter row, "x Belpex + 0,0000 x Endex 1-0-3 + 4,5041".

    The second term is matched only at a zero factor, which every card from
    January 2026 prints, or absent, as on the cards reissued on 29 October
    2026: "0,1099 x Belpex + 4,5041". A card giving Endex weight prices on a
    forward index nothing here resolves, and its row then reads as no formula
    at all.
    """
    return re.compile(
        rf"{label}\s*=\s*({_NUM})\s*x\s*Belpex"
        rf"(?:\s*\+\s*0+(?:,0+)?\s*x\s*Endex\s+1-0-3)?"
        rf"\s*([{SIGN_CHARS}])\s*({_NUM})(?![\d,])(?!\s*x)",
        re.S,
    )


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee = _extract_yearly_fee(text)
    if kind == "tou":
        # SmartFlex's TOU table prints the three band rates on its
        # "Énergie fournie" row, e.g. "(c€/kWh) 15,54 13,29 6,72", and
        # since October 2026 a fourth, Happy Sunday. The same label comes
        # back under "Informations sur le prix transitoire" with the
        # MaxxFlex mono / jour / nuit rates billed while the SMR3
        # conditions are not met, so the lookup stops at that heading:
        # left unbounded, a four-figure TOU row sent the three-figure
        # search there.
        tou_row = _energy_row(text, 4, _TRANSITIONAL_HEADING) or _energy_row(
            text, 3, _TRANSITIONAL_HEADING
        )
        if not tou_row:
            raise ExtractorError("could not parse Luminus TOU energy block")
        peak = to_float(tou_row[0]) / 100.0
        transition = to_float(tou_row[1]) / 100.0
        offpeak = to_float(tou_row[2]) / 100.0
        # SmartFlex's three bands (pleines / creuses / super-creuses) use
        # SEASONAL windows: peak (pleines) is 07-11 + 17-22 all year, the
        # cheapest super-creuses band applies 11-17 only in spring/summer
        # (21/03-20/09), and 22-07 is always creuses. The weekend_rule
        # "smartflex_seasonal" tells pricing.tou_slot to bill those windows.
        # The October 2026 card adds Happy Sunday, Sundays 11-17 of the same
        # season "Du lundi au samedi" leaves out of super-creuses, priced
        # "0 x Belpex + 0" where the earlier cards ran it as a first-year
        # promotion. It is a rate of its own, not a credit.
        tou_rates = TimeOfUseRates(
            peak=peak,
            transition=transition,
            offpeak=offpeak,
            yearly_fixed_fee=fee,
            weekend_rule="smartflex_seasonal",
            sunday=to_float(tou_row[3]) / 100.0 if len(tou_row) == 4 else None,
        )
        # SmartFlex indexes each band on the delivery month, same sentence as
        # its bi-hourly siblings: "Votre tarif sera indexe tous les mois. La
        # valeur Belpex du mois en cours n'est connue qu'a la fin du mois."
        # So the printed triplet is the previous month's, the Belpex the card
        # names, and a formula that does not give it back there is dropped.
        coefs = _monthly_tou_coefficients(text)
        printed = {
            "peak": peak,
            "transition": transition,
            "offpeak": offpeak,
            "sunday": tou_rates.sunday,
        }
        if not coefs or not _bands_give_printed(
            coefs, printed, _named_index(text, "Belpex")
        ):
            return tou_rates
        return replace(
            tou_rates,
            month_indexed=True,
            formula_factor_peak=coefs["peak"][0],
            formula_base_peak=coefs["peak"][1],
            formula_factor_transition=coefs["transition"][0],
            formula_base_transition=coefs["transition"][1],
            formula_factor_offpeak=coefs["offpeak"][0],
            formula_base_offpeak=coefs["offpeak"][1],
            formula_factor_sunday=coefs["sunday"][0] if "sunday" in coefs else None,
            formula_base_sunday=coefs["sunday"][1] if "sunday" in coefs else None,
        )

    if kind == "dynamic":
        pair = _belpex_h_pair(text, _DYNAMIC_ENERGY_BLOCK_RE)
        if pair is None:
            raise ExtractorError("could not parse Luminus dynamic formula")
        factor_pdf, base_pre_vat_cents, _row = pair
        vat = _vat_multiplier(text)
        # PDF formula: c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents.
        # Spot in EUR/kWh = Belpex_eur_mwh / 1000. Convert to:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        dynamic = DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=fee,
        )
        # The estimate the card prints, VAT included, is this formula at the
        # Belpex H it expects for the year ahead.
        estimate = _ANNUAL_ESTIMATE_RE.search(text)
        if not _gives_printed(
            None if estimate is None else to_float(estimate.group(1)) / 100.0,
            (dynamic.factor, dynamic.base),
            _expected_index(text, _DYNAMIC_ENERGY_BLOCK_RE),
        ):
            raise ExtractorError(
                "Luminus: the dynamic formula does not give the estimate its "
                "card prints at the Belpex H it expects"
            )
        return dynamic

    energy_row = _energy_row(text, 4)
    if not energy_row:
        raise ExtractorError(f"could not parse Luminus {kind} energy block")
    mono = to_float(energy_row[0]) / 100.0
    peak = to_float(energy_row[1]) / 100.0
    offpeak = to_float(energy_row[2]) / 100.0
    excl_night = to_float(energy_row[3]) / 100.0

    excl_night_fee = _extract_excl_night_fee(text)
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=fee,
            yearly_fixed_fee_exclusive_night=excl_night_fee,
        )
    rates = VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=fee,
        yearly_fixed_fee_exclusive_night=excl_night_fee,
    )
    coefs, rlp = _monthly_energy_coefficients(text)
    # ComfyFlex and ComfyFlex+ index the same per-meter rows on the delivery
    # QUARTER instead. Quarter indexed and not month indexed, as Bolt's
    # variable cards are, so the running quarter resolves on its mean to date
    # and a closed one settles on the index the next quarter's card names.
    quarterly = not coefs
    if quarterly:
        coefs = _quarterly_energy_coefficients(text)
    # The printed rates are the formulas at the index the card names for the
    # period before, BasicFlex's RLP-weighted one where it prices on it. A
    # formula that does not give them back there is dropped, as one that does
    # not read.
    printed = {
        "single": mono,
        "peak": peak,
        "offpeak": offpeak,
        "exclusive_night": excl_night,
    }
    name = "Belpex RLP M" if rlp else "Belpex"
    if not coefs or not _bands_give_printed(
        coefs, printed, _named_index(text, name, quarterly=quarterly)
    ):
        return rates
    if rlp:
        # BasicFlex's index is the month's Belpex weighted by "la moyenne
        # arithmetique des profils de consommation RLP des differents
        # gestionnaires", every DSO column alike: the columns blend, which
        # reproduces the Belpex RLP M Luminus published for January to
        # September 2026 to 0,01 EUR/MWh where the distinct one is up to
        # 1,95 off.
        rates = replace(rates, rlp_indexed=True, rlp_blend="columns")
    none2: tuple[float | None, float | None] = (None, None)
    return replace(
        rates,
        month_indexed=not quarterly,
        quarter_indexed=quarterly,
        formula_factor=coefs["single"][0],
        formula_base=coefs["single"][1],
        formula_factor_peak=coefs.get("peak", none2)[0],
        formula_base_peak=coefs.get("peak", none2)[1],
        formula_factor_offpeak=coefs.get("offpeak", none2)[0],
        formula_base_offpeak=coefs.get("offpeak", none2)[1],
        formula_factor_exclusive_night=coefs.get("exclusive_night", none2)[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", none2)[1],
    )


def _monthly_tou_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """The SmartFlex per-slot monthly coefficients, or ``{}``.

    Gated on the energy block attributing its index to a MONTH, which is what
    separates this card from ComfyFlex's quarterly one, and scoped to that
    block so the card's second MaxxFlex-identical section (the non-SMR3
    fallback) cannot supply them.

    All three bands are required. A partial match would leave one slot on the
    printed rate and the others on the index, which is worse than either.
    Happy Sunday's formula joins them as ``sunday`` on a card printing one.
    """
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}
    if _MONTHLY_INDEX_ATTRIBUTION_RE.search(block.group(0)) is None:
        return {}
    out = _formula_pairs(
        block.group(0),
        (*_TOU_BANDS, ("sunday", _HAPPY_SUNDAY_LABEL)),
        _band_formula_re,
        _vat_multiplier(text),
    )
    return out if all(key in out for key, _ in _TOU_BANDS) else {}


def _monthly_energy_coefficients(
    text: str,
) -> tuple[dict[str, tuple[float, float]], bool]:
    """Per-meter ``(factor, base)`` for a card that indexes energy monthly,
    and whether the index is the RLP-weighted one.

    Empty unless the card carries a delivery-month sentence AND its energy
    block yields a mono row. Both halves matter: ComfyFlex has the block but
    quotes a QUARTERLY index, SmartFlex has a MaxxFlex-identical block for a
    non-SMR3 meter but not the sentence, and a fixed card has neither.

    Two sentences, two indices. MaxxFlex's arithmetic mean is the bare
    "Belpex"; BasicFlex's weighted one is "Belpex RLP M", each with its own
    row pattern, so neither card's rows can be read on the other's index.
    """
    if _MONTHLY_RLP_RE.search(text) is not None:
        row, rlp = _rlp_band_formula_re, True
    elif _MONTHLY_ARITHMETIC_RE.search(text) is not None:
        row, rlp = _band_formula_re, False
    else:
        return {}, False
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}, False
    out = _formula_pairs(block.group(0), _ENERGY_BANDS, row, _vat_multiplier(text))
    return (out, rlp) if "single" in out else ({}, False)


def _quarterly_energy_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """Per-meter ``(factor, base)`` for a card that indexes energy on the
    delivery QUARTER, or ``{}``.

    ComfyFlex and ComfyFlex+: "Votre tarif sera indexe tous les trimestres
    ... Les prix affiches sont calcules sur la base de la derniere valeur
    Belpex connue (trimestre precedent) ... Sur le decompte, Luminus
    appliquera les valeurs moyennes trimestrielles". So the printed rates are
    the previous quarter's index, the quarter is billed on its own, and the
    next quarter's card names it: "Belpex = 131,39 EUR/MWh (valeur de
    l'indice du 3ieme trimestre 2026)". That is the quarter's plain mean: the
    monthly values Luminus publishes for MaxxFlex average back to it for the
    second and third quarters of 2026.

    Gated on the cadence sentence and on the energy block naming a quarter,
    and empty unless the mono row reads, as for the monthly cards.
    """
    if _QUARTERLY_CADENCE_RE.search(text) is None:
        return {}
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None or _QUARTER_ATTRIBUTION_RE.search(block.group(0)) is None:
        return {}
    out = _formula_pairs(
        block.group(0),
        _ENERGY_BANDS,
        _quarterly_band_formula_re,
        _vat_multiplier(text),
    )
    return out if "single" in out else {}


def _formula_pairs(
    block: str,
    bands: Iterable[tuple[str, str]],
    row: Callable[[str], re.Pattern[str]],
    vat: float,
) -> dict[str, tuple[float, float]]:
    """``(factor, base)`` per band whose ``row`` reads in ``block``.

    The rows print c/kWh HTVA against an index in EUR/MWh, while the energy
    rows are TVAC, so both coefficients take the x10 / 100 conversion and the
    card's VAT multiplier, ``vat``: 1 for the feed-in, which is exempt.
    MaxxFlex round-trips to its printed 14,41 at its own 92,61 index.
    """
    out: dict[str, tuple[float, float]] = {}
    for key, label in bands:
        match = row(label).search(block)
        if match is None:
            continue
        out[key] = (
            to_float(match.group(1)) * 10.0 * vat,
            parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0 * vat,
        )
    return out


def _belpex_h_pair(
    text: str, block: re.Pattern[str]
) -> tuple[float, float, str] | None:
    """The hourly pair a dynamic card's ``block`` prints, as ``(factor, base,
    row)`` in the card's own units, c/kWh HTVA against Belpex H in EUR/MWh,
    or ``None`` when the block or its rows do not read.

    Every row is read, one per meter register on the cards before September
    2025, and a card whose registers differ is refused: a ``DynamicRates``
    leg bills one formula on every meter, and every card on file prints the
    same one on each.
    """
    found = block.search(text)
    if found is None:
        return None
    rows = [
        row
        for row in _BELPEX_H_ROW_RE.finditer(found.group(0))
        if row.group(1) is not None
    ]
    pairs = {
        (to_float(row.group(1)), parse_sign(row.group(2)) * to_float(row.group(3)))
        for row in rows
    }
    if not pairs:
        return None
    if len(pairs) > 1:
        raise ExtractorError("Luminus: the dynamic formula differs by meter register")
    factor, base = pairs.pop()
    return factor, base, rows[0].group(0)


def _named_index(text: str, name: str, *, quarterly: bool = False) -> float | None:
    """The value in EUR/kWh the card names for the index ``name``, the one its
    printed rates were worked out at, or None (``published_indices``)."""
    found = published_indices(text, quarterly=quarterly).get(name)
    return None if found is None else found[1]


def _feed_in_row(text: str) -> re.Match[str] | None:
    """The feed-in row, or None where the card prints none, refused where its
    label states another unit than c€/kWh (``_INJECTION_UNIT_RE``)."""
    row = _INJECTION_ROW_RE.search(text)
    if row is not None:
        _require_unit(
            text,
            _INJECTION_UNIT_RE,
            row.span("single"),
            _INJECTION_UNIT_REACH,
            "feed-in row",
        )
    return row


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Anchor on the applicable "Tarif de l'énergie injectée" row, not the
    # "Estimation annuelle du tarif de l'énergie injectée" forecast
    # printed just below it (footnote: a 12-month estimate, not the rate
    # in force). The two share the "de l'énergie injectée" tail, but only
    # the applicable row capitalises "Tarif" (the estimate has lowercase
    # "tarif" after "du"), so a case-sensitive "Tarif" binds to the
    # applicable rate. Mirrors the consumption side, which deliberately
    # takes the current month over the annual estimate.
    #
    # Some cards print a footnote digit right after the unit
    # ("(c€/kWh)2 3,81"); skip an optional digit-then-whitespace before
    # the value. Use \s+ between every word: the row wraps mid-phrase
    # ("Tarif de l'énergie \ninjectée"). The cards until December 2025
    # call the row "Compensation pour l'énergie injectée".
    indicative = _feed_in_row(text)
    current = _signed_cents(indicative, "single")

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    month_indexed = False
    monthly: re.Match[str] | None = None
    index: float | None = None
    if kind == "dynamic":
        pair = _belpex_h_pair(text, _DYNAMIC_INJECTION_BLOCK_RE)
        if pair is not None:
            factor_pdf, base_pdf_cents, row = pair
            # Residential injection is VAT-exempt in Belgium.
            hourly = (factor_pdf * 10.0, base_pdf_cents / 100.0)
            # Kept where it gives back the estimate the card prints at the
            # Belpex H it expects, and dropped as one that does not read.
            if _gives_printed(
                _signed_cents(_INJECTION_ESTIMATE_RE.search(text), "estimate"),
                hourly,
                _expected_index(text, _DYNAMIC_INJECTION_BLOCK_RE),
            ):
                (factor, base), formula = hourly, row
    else:
        # A non-dynamic card indexes the credit on the delivery month and says
        # the printed figure is not it: "Votre tarif sera indexe tous les mois.
        # La valeur Belpex du mois en cours n'est connue qu'a la fin du mois.
        # Les prix affiches sont calcules sur la base de la derniere valeur
        # Belpex connue (mois precedent)." ComfyFlex and ComfyFlex+ say it of
        # the quarter. Their credit is month indexed all the same, the one
        # period flag a feed-in leg has: the quarter's months settle on the
        # index the next quarter's card names, and until then an entry with
        # day-ahead prices is credited on the delivery month's mean, the one
        # Luminus states it falls back to for a period not known at
        # settlement, and one without on the printed figure.
        monthly = _INJECTION_INDEXED_RE.search(text)
        if monthly is not None:
            # c/kWh per EUR/MWh of index, and the block is HTVA with the card
            # noting "La TVA s'eleve a 0%", so nothing is grossed.
            indexed = (
                to_float(monthly.group("factor")) * 10.0,
                parse_sign(monthly.group("sign"))
                * to_float(monthly.group("base"))
                / 100.0,
            )
            # The printed figure is the formula at the index the block names,
            # of the month or the quarter before. A formula that does not give
            # it back there is dropped, as one that does not read.
            quarterly = monthly.group("cadence") == "trimestres"
            index = _named_index(text, "Belpex", quarterly=quarterly)
            if _gives_printed(current, indexed, index):
                factor, base = indexed
                formula = " ".join(monthly.group(1).split())
                month_indexed = True
            else:
                monthly = None

    if current is None and factor is None:
        # Both Luminus card families always publish injection: the
        # applicable indicative on fixed/variable/TOU cards, the spot
        # formula on dynamic. A miss is a layout drift, not a fee-free
        # contract; fail loud rather than silently crediting an injection
        # user nothing, mirroring the consumption helpers that raise.
        raise ExtractorError("Luminus: could not parse injection rate")
    leg = InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
    )
    if kind not in ("fixed", "variable") or indicative is None:
        return leg
    return _with_registers(leg, indicative, monthly, index)


def _with_registers(
    leg: InjectionRates,
    row: re.Match[str],
    block: re.Match[str] | None,
    index: float | None,
) -> InjectionRates:
    """``leg`` with a bi-hourly meter's day and night credit, where the card
    prices them apart from the single register.

    The feed-in row prints one figure per register, single, then day and
    night, and the formula block one row each. Every card since July 2025
    prints the three alike, and the leg is the one rate. The cards until June
    2025 do not: "Jour (compteur de jour) = 0,0644 x Belpex - 1,5900; Jour
    (compteur de jour et de nuit)) = 0,0794 x Belpex - 1,5900; Nuit = 0,0414 x
    Belpex - 1,5900", 6,71, 8,64 and 3,75 c/kWh in March 2025. Their day and
    night figures are the two registers' rates (``bi_hourly``), each on its
    own formula where the card indexes the credit, as the social tariff's
    cards are read; taken as one rate, a bi-hourly meter would be credited
    the single register's figure at every hour.

    Each register's formula gives back its own figure at ``index``, the one
    the block names, as the single register's does, or the card is refused
    as one missing it.
    """
    if row.group("day") is None:
        return leg
    day = _signed_cents(row, "day")
    night = _signed_cents(row, "night")
    if day == leg.current and night == leg.current:
        return leg
    leg = replace(leg, peak=day, offpeak=night, bi_hourly=True)
    if block is None:
        return leg
    pairs = _formula_pairs(block.group(0), _ENERGY_BANDS, _band_formula_re, 1.0)
    if "peak" not in pairs or "offpeak" not in pairs:
        raise ExtractorError("Luminus: a feed-in register's formula is missing")
    if not (
        _gives_printed(day, pairs["peak"], index)
        and _gives_printed(night, pairs["offpeak"], index)
    ):
        raise ExtractorError(
            "Luminus: a feed-in register's formula does not give the rate its "
            "card prints at the index it names"
        )
    return replace(
        leg,
        factor_peak=pairs["peak"][0],
        base_peak=pairs["peak"][1],
        factor_offpeak=pairs["offpeak"][0],
        base_offpeak=pairs["offpeak"][1],
    )


# The mono-hourly figure of the card's annual estimate, the first on the row:
# the price a campaign's volume is valued at (``_luminus_promo.py``), and on
# a dynamic card the one its formula gives at the Belpex H it expects. The
# label wraps and a footnote marker may sit against the closing parenthesis
# ("(c€/kWh)3 20,70 24,11").
# The cards reissued on 29 October 2026 head the row "Estimation annuelle
# du prix de l'énergie" and label it as the energy row above it is.
_ANNUAL_ESTIMATE_RE = re.compile(
    r"Estimation\s+annuelle\s+"
    r"(?:de\s+l['\u2019]énergie\s+fournie|du\s+prix\s+de\s+l['\u2019]énergie)"
    r"\s*\(c€/kWh\)\d*\s+"
    r"(?:Énergie\s+fournie(?:,\s*énergie\s+verte\s*\+\s*cogénération"
    r"|\s*\+\s*énergie\s+verte)\s+)?"
    r"(\d+,\d+)"
)
# The card's own "(octobre 2026)" label, read for the month it prices.
_LABEL_MONTH_RE = re.compile(r"([^\W\d_]+)\s+(20\d{2})")
# A dynamic card's two formula blocks, energy and feed-in, each from its
# heading to the "Les montants mentionnes sont arrondis" sentence closing it.
# The rows inside are one per meter register, in three spellings: since
# September 2025 one "Prelevement (Compteur mono-horaire et bi-horaire)" and
# one "Injection (...)" row; in August 2025 "Compteur mono-horaire", "Heures
# pleines (Compteur bi-horaire)" and "Heures creuses"; from January 2024 to
# July 2025 "Jour (compteur de jour)", "Jour (compteur de jour et de nuit)"
# and "nuit". The feed-in heading moved from "pour la compensation de" to
# "pour" to "de" over the same cards.
_DYNAMIC_ENERGY_BLOCK_RE = re.compile(
    r"Formules?\s+tarifaires?\s+pour\s+le\s+co[uû]t\s+de\s+l['\u2019\u00a9]\s*[ée]nergie"
    r"(?:(?!Formule\s+tarifaire|Les\s+montants).)*",
    re.S,
)
_DYNAMIC_INJECTION_BLOCK_RE = re.compile(
    r"Formule\s+tarifaire\s+(?:pour\s+la\s+compensation\s+de|pour|de)\s+"
    r"l['\u2019\u00a9]\s*[ée]nergie\s+inject[ée]e"
    r"(?:(?!Formule\s+tarifaire|Les\s+montants).)*",
    re.S,
)
# A dynamic card's feed-in estimate, "Estimation annuelle du tarif de l'énergie
# injectée (c€/kWh)3 13,56", "de la compensation de" until December 2025.
_INJECTION_ESTIMATE_RE = re.compile(
    r"Estimation\s+annuelle\s+(?:du\s+tarif|de\s+la\s+compensation)\s+de\s+"
    r"l['\u2019]énergie\s+injectée\s*\(c€/kWh\)\d*\s+" + _signed("estimate")
)
# The feed-in row, one figure per meter register: single, then a bi-hourly
# meter's day and night, all on the one line ("(c€/kWh)2 6,71 8,64 3,75 -").
# The label is skipped up to the first digit or sign, so a sign ahead of the
# single figure is kept with it. The gap to the figures stops at the next
# label, whose own gap reaches the same figures: run on to them from each, a
# run of labels with no figure after them was read to the end again from
# every one.
_INJECTION_ROW_LABEL = (
    r"(?:Tarif\s+de|Compensation\s+pour)\s+l[\"'’©]énergie\s+injectée"
)
_INJECTION_ROW_RE = re.compile(
    rf"{_INJECTION_ROW_LABEL}(?:(?!{_INJECTION_ROW_LABEL})[^0-9{SIGN_CHARS}])*"
    rf"(?:\d+\s+)?{_signed('single')}"
    rf"(?:[ \t]+{_signed('day')}[ \t]+{_signed('night')})?"
)
# The unit the feed-in row's label states, "Tarif de l'énergie injectée
# (c€/kWh)", "Compensation pour l'énergie injectée (c€/kWh)" until December
# 2025, the only unit any card on file prints there, ending 2 characters
# ahead of the single figure after the footnote marker. The label starts 48
# characters ahead at most, and no other statement of it lies within reach.
# A card stating the row in another unit, or in none, prints a figure read
# ten or a hundred times out, which the formula check passes when the
# formula is restated with it, and is refused.
_INJECTION_UNIT_RE = re.compile(rf"{_INJECTION_ROW_LABEL}\s*\(([^()]{{0,20}})\)")
_INJECTION_UNIT_REACH = 100
# One register's row with its label, which runs back to the block's colon or
# to the row before it: "Injection (Compteur mono-horaire et bi-horaire) =
# 0,1019 x Belpex H - 1,2737". Every place in a label reaches the same "=",
# so a label that leads to no row is taken whole by the second branch, which
# _belpex_h_pair drops: tried from each of its characters, it was read to
# that "=" from every one.
_BELPEX_H_ROW_RE = re.compile(
    rf"[^\s=;:.](?:[^=;:]*?=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})"
    r"|[^=;:]*)",
    re.S,
)
# A non-dynamic card's feed-in formula, accepted ONLY when that same block
# says the tariff is indexed, on the month or on the quarter. A formula with no
# cadence beside it is not read as one: the engine would take its coefficients
# for a per-hour formula.
#
# ComfyFlex and ComfyFlex+ print the IDENTICAL "0,0481 x Belpex - 0,6392" and
# index it QUARTERLY ("Votre tarif sera indexe tous les trimestres", against
# "du 1re trimestre 2026"). Read, with the printed figure kept as the
# fallback, its months settle on the quarter's own index once the next
# quarter's card names it; the printed figure alone credited the quarter
# before's for good, 1,79 c/kWh under in the third quarter of 2026.
#
# The leading guard keeps the scan inside the injection block: the SmartFlex
# card carries a second, MaxxFlex-identical block for a non-SMR3 meter. The
# cards until December 2025 head the block "pour la compensation de".
_INJECTION_INDEXED_RE = re.compile(
    r"Formule\s+tarifaire\s+(?:pour\s+la\s+compensation\s+)?de\s+"
    r"l['\u2019\u00a9]énergie\s+injectée"
    r"(?:(?!Formule\s+tarifaire).)*?"
    rf"=\s*((?P<factor>{_NUM})\s*x\s*Belpex\s*"
    rf"(?P<sign>[{SIGN_CHARS}])\s*(?P<base>{_NUM}))"
    r"(?:(?!Formule\s+tarifaire|Votre\s+tarif\s+sera\s+indexé).){0,400}?"
    r"Votre\s+tarif\s+sera\s+indexé\s+(?:chaque|tous\s+les)\s+"
    r"(?P<cadence>mois|trimestres)\b",
    re.S,
)
# MaxxFlex indexes the COMMODITY on the delivery month too: "Le parametre
# d'indexation est base sur la moyenne arithmetique des cotations journalieres
# Day Ahead Belpex Baseload ... pendant le mois de livraison. La valeur Belpex M
# du mois en cours n'est connue qu'a la fin du mois."
#
# That sentence is the gate. ComfyFlex quotes a QUARTERLY index and SmartFlex
# carries a second MaxxFlex-identical block for a non-SMR3 meter, so neither
# may be swept in by the formula shape.
_MONTHLY_ARITHMETIC_RE = re.compile(
    r"moyenne\s+arithm[ée]tique.{0,200}?pendant\s+le\s+mois\s+de\s+livraison",
    re.S,
)
# BasicFlex indexes on the delivery month as well, but weighted: "la moyenne
# ponderee des cotations journalieres Day Ahead Belpex Baseload ... pendant le
# mois de livraison avec une ponderation RLP". The arithmetic sentence above
# misses it, and its rows print "x Belpex RLP M". It printed the previous
# month's figure as its rate on every 2026 card, so the month was billed on
# the index before it: 3,26 c/kWh under in September.
_MONTHLY_RLP_RE = re.compile(
    r"moyenne\s+pond[ée]r[ée]e.{0,200}?pendant\s+le\s+mois\s+de\s+livraison"
    r"\s+avec\s+une\s+pond[ée]ration\s+RLP",
    re.S,
)
# The ENERGY block only. Scoping matters: searched over the whole document the
# mono pattern also finds the INJECTION formula ("0,0481 x Belpex - 0,6392"),
# and a fixed card would gain an energy formula it does not have. The feed-in
# block that ends it is headed "pour la compensation de" until December 2025.
# The cards reissued on 29 October 2026 print the block's "Index (HTVA)" line
# above its heading instead of below, so a line right above belongs to it.
# That line is read from its last "Index (HTVA)", which reaches the same line
# break: read on from each, a line of them was read to its end again from
# every one.
_ENERGY_FORMULA_BLOCK_RE = re.compile(
    r"(?:Index\s*\(HTVA\)(?:(?!Index\s*\(HTVA\))[^\n])*\n\s*)?"
    r"Formules\s+tarifaires\s+pour\s+le\s+co[uû]t\s+de\s+l['\u2019\u00a9]\s*[ée]nergie"
    r"(?:(?!Formule\s+tarifaire\s+(?:pour\s+la\s+compensation\s+)?de).)*",
    re.S,
)
# The rows of the feed-in block too, which prints no exclusive night.
_ENERGY_BANDS: tuple[tuple[str, str], ...] = (
    # The label wraps after its hyphen on the April to June 2026 ComfyFlex
    # cards: "Compteur mono-\nhoraire". The cards until July 2025 name each
    # row after its meter instead, "Jour (compteur de jour)", "Jour (compteur
    # de jour et de nuit)" and "et nuit" or "Nuit", the feed-in block closing
    # the day row's parenthesis twice. The peak label's bracket stops at the
    # next one, which closes on the same bracket: read on to it from each, a
    # run of open brackets was read to the end again from every one.
    ("single", r"(?:Compteur\s+mono-\s*horaire|Jour\s*\(compteur\s+de\s+jour\))"),
    (
        "peak",
        r"(?:Heures\s+pleines(?:\s*\((?:(?!Heures\s+pleines\s*\()[^)])*\))?"
        r"|Jour\s*\(compteur\s+de\s+jour\s+et\s+de\s+nuit\)+)",
    ),
    ("offpeak", r"(?:Heures\s+creuses|(?<!Exclusif\s)\b[Nn]uit)"),
    ("exclusive_night", r"Exclusif\s+nuit"),
)


def _extract_excl_night_fee(text: str) -> float | None:
    """Yearly fixed fee for an exclusive-night circuit.

    Static / variable cards print the Redevance fixe row with three columns
    (mono | bi | exclusif nuit), e.g. "65,00 65,00 -". The "-" means the
    exclusive-night circuit carries no separate abonnement (0) - it is billed
    once on the main connection - so a second exclusive-night entry must bill
    0, not the standard fee. Returns None when the row has no third column
    (dynamic cards print a single value and don't offer exclusive-night), so
    the standard fee applies.
    """
    match = re.search(
        rf"Redevance fixe\s*\(€/an\)\s+{_NUM}\s+{_NUM}\s+({_NUM}|-)", text
    )
    if match is None:
        return None
    col = match.group(1)
    return 0.0 if col == "-" else to_float(col)


_TOU_BANDS: tuple[tuple[str, str], ...] = (
    ("peak", r"Pr[ée]l[èe]vement\s+Heures\s+pleines"),
    ("transition", r"Pr[ée]l[èe]vement\s+Heures\s+creuses"),
    ("offpeak", r"Pr[ée]l[èe]vement\s+Heures\s+super[\s-]*creuses"),
)
_HAPPY_SUNDAY_LABEL = r"Pr[ée]l[èe]vement\s+Happy\s+Sunday"
# "(valeur de l'indice de mars 2026)" against ComfyFlex's "(valeur de l'indice
# du 1re trimestre 2026)". A MONTH attribution is the discriminator that works
# on the SmartFlex card, whose energy block carries no cadence sentence of its
# own: the sentence sits under the injection block and governs that tariff.
# The blanks before the month are taken whole: given back one at a time, each
# place in a long run of them looked ahead over the rest of it again.
_MONTHLY_INDEX_ATTRIBUTION_RE = re.compile(
    r"valeur\s+de\s+l['\u2019\u00a9]indice\s+de\s++(?!\s*\d)\w+", re.IGNORECASE
)
# ComfyFlex's: "(valeur de l'indice du 3ieme trimestre 2026)", and "1re",
# "2ieme" and "4ieme" on the cards of 2026.
_QUARTER_ATTRIBUTION_RE = re.compile(
    r"valeur\s+de\s+l['\u2019\u00a9]indice\s+du\s+[1-4][^\W\d_]*\s+trimestre\b",
    re.IGNORECASE,
)
# The sentence a quarterly card states its cadence in, under the feed-in
# block, where it governs both legs: "Votre tarif sera indexe tous les
# trimestres. Les trimestres commencent le 1er janvier, 1er avril, ..."
_QUARTERLY_CADENCE_RE = re.compile(
    r"Votre\s+tarif\s+sera\s+indexé\s+tous\s+les\s+trimestres\b"
)


# "Le taux de TVA sur les prix indiques ... est de 6 %", else "TVA 6%". Not the
# injection's "La TVA s'eleve a 0%", which no pattern here reads. The gap to
# the rate is bounded and holds no percent sign: left open, a sentence that
# lost its figure matched the next "N %" anywhere further down the card.
_VAT_PATTERNS = (
    re.compile(r"TVA\s*sur\s*les\s*prix[^%]{0,120}?(\d+)\s*%"),
    re.compile(r"TVA\s*(\d+)\s*%"),
)


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(text, *_VAT_PATTERNS)


# "Index (HTVA) : Belpex RLP M = 164,42 EUR/MWh (valeur de l'indice de
# septembre 2026)": the month the card's own figures were computed on. The
# SmartFlex card writes "de de septembre" and "156,4100000" in places.
_PUBLISHED_INDEX_RE = re.compile(
    r"Index\s*\(HTVA\)\s*:?\s*(Belpex(?:\s+RLP\s+M)?)\s*=\s*(\d+,\d+)\s*€/MWh\s*"
    r"\(valeur\s+de\s+l['\u2019]indice\s+(?:de\s+)+([^\W\d_]+)\s+(20\d{2})\)"
)
# ComfyFlex's: "Index (HTVA): Belpex = 131,39 EUR/MWh (valeur de l'indice du
# 3ieme trimestre 2026)", the quarter's number in its first group of digits.
_PUBLISHED_QUARTER_INDEX_RE = re.compile(
    r"Index\s*\(HTVA\)\s*:?\s*(Belpex)\s*=\s*(\d+,\d+)\s*€/MWh\s*"
    r"\(valeur\s+de\s+l['\u2019]indice\s+du\s+([1-4])[^\W\d_]*\s+trimestre\s+(20\d{2})\)"
)


def published_indices(
    text: str, *, quarterly: bool = False
) -> dict[str, tuple[date, float]]:
    """The indices a card names for the period before its own, by name
    ("Belpex", "Belpex RLP M"), as ``(first month of it, EUR/kWh)``.

    Luminus computes a card's figures on the last value known and says which:
    the previous month's, or with ``quarterly`` the previous quarter's on
    ComfyFlex, and that is what the period is billed on. Two different values
    under one name make the name unusable.
    """
    pattern = _PUBLISHED_QUARTER_INDEX_RE if quarterly else _PUBLISHED_INDEX_RE
    found: dict[str, set[tuple[date, float]]] = {}
    for match in pattern.finditer(" ".join(text.split())):
        if quarterly:
            number: int | None = (int(match.group(3)) - 1) * 3 + 1
        else:
            number = _MONTH_NAMES.get(match.group(3).lower())
        if number is None:
            continue
        found.setdefault(" ".join(match.group(1).split()), set()).add(
            (date(int(match.group(4)), number, 1), to_float(match.group(2)) / 1000.0)
        )
    return {
        name: next(iter(values)) for name, values in found.items() if len(values) == 1
    }


def _extract_yearly_fee(text: str) -> float:
    """Capture the 'Redevance fixe' line.

    Every Luminus residential card the integration covers prints this
    line (~65 EUR for static, ~75 EUR for dynamic). A regex miss is a
    layout drift, not a fee-free contract; raise rather than default to
    0 so the coordinator surfaces the failure instead of silently
    dropping ~70 EUR/year from the user's annual estimate.
    """
    match = re.search(rf"Redevance fixe\s*\(€/an\)\s+({_NUM})", text)
    if match is None:
        raise ExtractorError("Luminus: yearly fee (Redevance fixe) not found")
    return to_float(match.group(1))
