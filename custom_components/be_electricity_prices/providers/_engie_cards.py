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

"""Engie: the product legs its card prints.

The EPEX DAM formulas and their coefficients, the Flextime slot rates and the
feed-in leg. Engie names two different EPEX DAM indices on the same card and
they are not interchangeable, which is most of why this is the long half.
"""

from __future__ import annotations

from ._parse import SIGN_CHARS, parse_sign, to_float
from ._rates import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    TariffKind,
    TimeOfUseRates,
    VariableRates,
    fixed_or_variable_rates,
)
from .base import ExtractorError
from dataclasses import replace
import re
from ._pdf import vat_multiplier


def _epexdam_formulas(
    text: str, printed: float, vat: float
) -> dict[str, tuple[float, float]]:
    """Per-band ``(factor, base)`` in EUR/kWh for the leg printing ``printed``.

    Both legs' formula blocks live on the same card and the two-column PDF
    interleaves them, so binding by document order is exactly the mistake the
    OCTA+ card punished. Bind by ARITHMETIC instead: evaluate each candidate at
    the index the card states and keep the block whose unlabelled-or-Normal row
    reproduces the price this leg actually prints. That is self-verifying, it
    survives a reordering, and it fails closed - no match means the caller
    keeps the printed value rather than billing a formula bound to the wrong
    leg.

    ``vat`` is the multiplier this leg is printed on: 1.06 for a residential
    consumption row, 1.0 for injection, which is VAT-exempt.
    """
    index = _epexdam_index(text)
    if index is None:
        return {}
    # Group into contiguous blocks. Each block opens on its Normal / bare row
    # and its band rows follow, so a band can never be lifted out of the other
    # leg's block - which is exactly what a flat scan did, pairing this leg's
    # Normal with the other leg's Heures pleines.
    blocks: list[dict[str, tuple[float, float]]] = []
    for label, sign, base_s, factor_s in _EPEXDAM_FORMULA_RE.findall(text):
        # The card states c/kWh per EUR/MWh of index, so onto a EUR/kWh spot
        # the factor carries a x10 and the base a /100.
        factor = to_float(factor_s) * 10.0 * vat
        base = parse_sign(sign or "+") * to_float(base_s) / 100.0 * vat
        slot = " ".join((label or "").split()).lower()
        key = _EPEXDAM_BAND.get(slot)
        if key is None:
            # A label this card generation invented.
            continue
        if key == "single" or not blocks:
            blocks.append({})
        blocks[-1].setdefault(key, (factor, base))
    for block in blocks:
        single = block.get("single")
        if single is None:
            continue
        if abs((single[0] * index / 1000.0 + single[1]) - printed) <= 1e-4:
            return block
    # No block reproduces this leg's printed price: the caller keeps that
    # price rather than billing a formula bound to the wrong leg.
    return {}


def _flextime_coefficients(
    text: str, printed_single: float, vat: float, printed_slots: tuple[float, ...]
) -> tuple[tuple[float, float], ...] | None:
    """The three Flextime ``(factor, base)`` pairs of the leg printing
    ``printed_single`` on its Normal row, or ``None``.

    Bound through :func:`_epexdam_formulas`, so the block is the one whose
    Normal row reproduces this leg's printed figure, and then held to the
    same test per slot: each Flextime pair has to reproduce the slot's own
    printed figure at the index the card states. A row that does not is a
    layout the card has not printed before, and the answer is the printed
    triplet, not two formulas and a guess.
    """
    coefs = _epexdam_formulas(text, printed_single, vat)
    index = _epexdam_index(text)
    if index is None or any(band not in coefs for band in _FLEXTIME_BANDS):
        return None
    pairs = tuple(coefs[band] for band in _FLEXTIME_BANDS)
    for (factor, base), printed in zip(pairs, printed_slots, strict=True):
        if abs((factor * index / 1000.0 + base) - printed) > 1e-4:
            return None
    return pairs


def _extract_energy(
    text: str, kind: TariffKind, *, professional: bool = False
) -> EnergyRates:
    # Engie prints the yearly fee in two different layouts:
    #
    # 1. Standard cards (Easy / Dynamic / Empty House): the fee sits on
    #    the same logical row as "Type d'usage", e.g. "65,00 €/an Type
    #    d'usage".
    # 2. Empower variants (Variable / Flextime): the fee is the first
    #    number on the "Prix mensuels" row, just before "Consommation(2)".
    #    The card has no "Type d'usage" anchor at all.
    #
    # Try the standard anchor first, fall back to the Empower layout, and
    # raise if neither matches: every residential Engie card the
    # integration covers carries a yearly fee, so a miss is a layout drift
    # rather than a fee-free contract.
    fee_match = re.search(r"(\d+[,.]\d+)\s*€/an\s*\n?\s*Type\s*\n?\s*d[©']usage", text)
    if fee_match is None:
        fee_match = re.search(
            r"Prix\s+mensuels\s*\n\s*(\d+[,.]\d+)\s+Consommation", text
        )
    if fee_match is None:
        raise ExtractorError("Engie: yearly fee row not found")
    yearly_fee = to_float(fee_match.group(1))

    if kind == "dynamic":
        match = _FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Engie dynamic consumption formula")
        # Groups: (base_sign, base_magnitude, factor_sign, factor_magnitude).
        base_pre_vat_cents = parse_sign(match.group(1)) * to_float(match.group(2))
        factor_pdf = parse_sign(match.group(3)) * to_float(match.group(4))
        vat = _vat_multiplier(text, professional=professional)
        # PDF formula yields c€/kWh hors TVA from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        # Engie Dynamic bills per quarter-hour: the consumer formula is
        # (B x eSpot_15) + A, where eSpot_15 is the Belgian day-ahead EPEX
        # price for that specific quarter-hour (engie.be/dynamic-tarief).
        # Keep the native 15-minute slots rather than the hourly mean.
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
            quarter_hourly=True,
        )

    # Capture the whole Consommation(2) row up to the newline. Most
    # contracts have 4 prices + 1 trailing renewables column; Empty House
    # and similar mono-only tariffs have just 1 price + 1 renewables.
    consumption = re.search(r"Consommation\(2\)([^\n]+)", text)
    if not consumption:
        raise ExtractorError(f"could not parse Engie {kind} consumption block")
    nums = [to_float(n) for n in re.findall(r"[\d,.]+", consumption.group(1))]
    # Last column is the regional renewables levy; drop it, what remains
    # is the price columns.
    prices = nums[:-1] if len(nums) >= 2 else nums
    peak: float | None
    offpeak: float | None
    excl_night: float | None
    if len(prices) == 4:
        # Standard layout: Normal | Bi-pleines | Bi-creuses | Excl. nuit
        mono, peak, offpeak, excl_night = (p / 100.0 for p in prices)
    elif len(prices) == 7:
        # Empower Variable with Flextime: Normal | Bi-pleines | Bi-creuses
        # | Flextime pleines | Flextime creuses | Flextime super-creuses |
        # Exclusif nuit. The variable contract uses the bi-horaire pair;
        # the Flextime contract returns the TOU triplet directly.
        if kind == "tou":
            tou = TimeOfUseRates(
                peak=prices[3] / 100.0,
                transition=prices[4] / 100.0,
                offpeak=prices[5] / 100.0,
                yearly_fixed_fee=yearly_fee,
                weekend_rule="weekend_no_peak",
            )
            # The same EPEXDAM sentence as Empower Variable, printed on the
            # same card: each Flextime band is a formula on the DELIVERY
            # month's mean and the triplet above is last month's. Bound by
            # the Normal row this leg prints, like the bi-hourly pairs.
            flex = _flextime_coefficients(
                text,
                prices[0] / 100.0,
                _vat_multiplier(text, professional=professional),
                (tou.peak, tou.transition, tou.offpeak),
            )
            if flex is None:
                return tou
            (f_peak, b_peak), (f_trans, b_trans), (f_off, b_off) = flex
            return replace(
                tou,
                month_indexed=True,
                formula_factor_peak=f_peak,
                formula_base_peak=b_peak,
                formula_factor_transition=f_trans,
                formula_base_transition=b_trans,
                formula_factor_offpeak=f_off,
                formula_base_offpeak=b_off,
            )
        mono = prices[0] / 100.0
        peak = prices[1] / 100.0
        offpeak = prices[2] / 100.0
        excl_night = prices[6] / 100.0
    elif len(prices) == 1:
        # Mono-only tariffs (e.g. Empty House for vacant properties).
        mono = prices[0] / 100.0
        peak = offpeak = excl_night = None
    else:
        raise ExtractorError(
            f"unexpected price column count for Engie {kind}: {len(prices)}"
        )

    if kind == "tou":
        # 7-price Empower Variable layout was the only path here; if we
        # arrive with kind="tou" but a 4-price row, the user picked
        # Flextime on a card that doesn't carry it.
        raise ExtractorError(
            "Engie Empower Flextime requires the 7-price Empower row "
            "(Flextime triplet); not present in this card."
        )

    rates = fixed_or_variable_rates(
        kind,
        single=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )
    if not isinstance(rates, VariableRates) or mono is None:
        return rates
    # Empower Variable and Empty House index consumption on the DELIVERY
    # month's EPEXDAM and print a price computed from the last month whose
    # value is known. Easy Variable indexes on ENDEX101, which is published in
    # advance and correct as printed, and its card names no EPEXDAM at all, so
    # gating on the formula being present is what keeps it out.
    coefs = _epexdam_formulas(
        text, mono, _vat_multiplier(text, professional=professional)
    )
    if not coefs:
        return rates
    none2: tuple[float | None, float | None] = (None, None)
    return replace(
        rates,
        month_indexed=True,
        formula_factor=coefs["single"][0],
        formula_base=coefs["single"][1],
        formula_factor_peak=coefs.get("peak", none2)[0],
        formula_base_peak=coefs.get("peak", none2)[1],
        formula_factor_offpeak=coefs.get("offpeak", none2)[0],
        formula_base_offpeak=coefs.get("offpeak", none2)[1],
        # The card prices a night circuit separately, "Exclusif nuit = 2,4510
        # + (0,1005 x EPEXDAM)", between the mono 0,1171 and the off-peak
        # 0,0988. Routing it onto either neighbour is wrong on the meter that
        # draws the volume.
        formula_factor_exclusive_night=coefs.get("exclusive_night", none2)[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", none2)[1],
    )


def _extract_injection(
    text: str, kind: TariffKind, *, professional: bool = False
) -> InjectionRates | None:
    # The first "Injection(3)" row is the applicable rate (a second row is
    # the annual estimate). Its columns mirror the consumption row:
    #   normal | bi-pleines | bi-creuses | flextime pleines |
    #   flextime creuses | flextime super-creuses | ...
    row = re.search(r"Injection\(3\)\s+([^\n]+)", text)
    nums = (
        [to_float(t) for t in row.group(1).split() if re.fullmatch(r"[\d,.]+", t)]
        if row
        else []
    )
    current = nums[0] / 100.0 if nums else None

    peak: float | None = None
    transition: float | None = None
    offpeak: float | None = None
    slot_coefs: tuple[tuple[float, float], ...] | None = None
    if kind == "tou" and len(nums) >= 6:
        # Empower Flextime: the feed-in tariff varies by slot, so surface
        # the per-slot triplet (columns 4-6) the pricing engine selects via
        # tou_slot(). Columns 1-3 are the single / bi-horaire rates the
        # non-Flextime variants use; they're identical to each other here.
        peak = nums[3] / 100.0
        transition = nums[4] / 100.0
        offpeak = nums[5] / 100.0
        # And each slot is its own EPEXDAM formula, printed at the previous
        # month's index like the rest of the card. Not grossed: injection is
        # exempt on the residential edition and HTVA throughout on the
        # professional one, where vat_applies carries the 21% to apply_vat.
        if current is not None:
            slot_coefs = _flextime_coefficients(
                text, current, 1.0, (peak, transition, offpeak)
            )

    formulas = list(_FORMULA_RE.finditer(text))
    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    # Only Dynamic cards carry a spot injection formula (the second
    # BELPEX formula on the card). Gate on kind so a future indexed or
    # variable card that happens to print a price formula can't flip the
    # injection taxonomy to a spot factor/base shape.
    if kind == "dynamic" and len(formulas) >= 2:
        injection_match = formulas[1]
        # Groups: (base_sign, base_magnitude, factor_sign, factor_magnitude).
        base_pdf_cents = parse_sign(injection_match.group(1)) * to_float(
            injection_match.group(2)
        )
        factor_pdf = parse_sign(injection_match.group(3)) * to_float(
            injection_match.group(4)
        )
        # Both sides are printed on the card's own VAT basis: residential
        # injection is VAT-exempt, professional injection is grossed later
        # by apply_vat off vat_applies.
        factor = factor_pdf * 10.0
        base = base_pdf_cents / 100.0
        formula = injection_match.group(0)
    month_indexed = False
    if kind == "variable" and current is not None:
        # The same EPEXDAM story as the energy leg: "Les prix d'injection sont
        # indexes en utilisant le parametre EPEXDAM. La valeur du EPEXDAM du
        # mois en cours ne sera connue qu'en fin de mois", so the printed
        # Injection(3) figure is the formula on the PREVIOUS month.
        #
        # Restricted to the variable kind on purpose. Flextime matches the
        # same anchor but prints THREE distinct injection coefficient pairs,
        # one per TOU slot, which travel on the per-slot fields above: writing
        # its Normal row here as well would store a fourth formula that
        # nothing reads, because the per-slot rates win. The ENDEX101 cards
        # are excluded by the formula regex itself, which requires the literal
        # EPEXDAM; their index is a futures average published in ADVANCE, so
        # their printed figure is the billed rate with no lag to correct.
        #
        # Injection is not grossed on either edition: residential is exempt,
        # and a professional card is HTVA throughout with vat_applies carrying
        # the 21% for apply_vat. The card proves it - its injection formula
        # reproduces the printed figure with no 1,06 while the energy one
        # needs it.
        inj_coefs = _epexdam_formulas(text, current, 1.0)
        single = inj_coefs.get("single")
        if single is not None:
            factor, base = single
            month_indexed = True
            formula = f"{base * 100.0:.4f} + ({factor / 10.0:.4f} x EPEXDAM)"
    if slot_coefs is not None:
        month_indexed = True
        formula = "Flextime: " + "; ".join(
            f"{slot} {b * 100.0:.4f} + ({f / 10.0:.4f} x EPEXDAM)"
            for slot, (f, b) in zip(("peak", "transition", "offpeak"), slot_coefs)
        )
    if current is None and factor is None and peak is None:
        return None
    none2: tuple[float | None, float | None] = (None, None)
    peak_c, trans_c, off_c = slot_coefs if slot_coefs is not None else (none2,) * 3
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
        peak=peak,
        transition=transition,
        offpeak=offpeak,
        factor_peak=peak_c[0],
        base_peak=peak_c[1],
        factor_transition=trans_c[0],
        base_transition=trans_c[1],
        factor_offpeak=off_c[0],
        base_offpeak=off_c[1],
        # "Le prix d'injection est soumis a la TVA (21%)" on the
        # professional card, against "n'est pas soumis a la TVA" on the
        # residential one.
        vat_applies=professional,
    )


# Engie's formula prints either "Formule de prix hors TVA -1,3135 + (0,1095 x
# eSpot_15)" (consumption: positive base usually, negative for injection),
# but a future re-render could flip either side to a Unicode minus or any
# of the dashes from _parse.SIGN_CHARS. Accept the full sign-class on both
# the base and the factor so the regex doesn't silently miss after a
# punctuation drift, and route through parse_sign for the magnitude.
_FORMULA_RE = re.compile(
    rf"Formule de prix\s+hors\s+TVA\s+([{SIGN_CHARS}]?)\s*([\d,.]+)\s*\+\s*"
    rf"\(([{SIGN_CHARS}]?)\s*([\d,.]+)\s*x\s*eSpot_15\)"
)
# Empower Variable / Empty House index BOTH legs on the monthly EPEXDAM and say
# so: "Le prix de l'electricite est indexe mensuellement. Le parametre
# d'indexation est la moyenne arithmetique des cotations journalieres Day Ahead
# EPEX SPOT Belgium (ci-apres EPEXDAM) durant le mois de fourniture", and "La
# valeur du EPEXDAM du mois en cours ne sera connue qu'en fin de mois. A titre
# informatif, les prix indiques sont bases sur la derniere valeur du EPEXDAM
# connue (Mars 2026: 92,57 EUR/MWh)."
#
# Two spellings, both on cards in the tree:
#   Empower     "- Normal = 2,1552 + (0,1171 x EPEXDAM)", one row per band
#   Empty House "3,2150 + (0,2150 x EPEXDAM)", bare, one row full stop
_EPEXDAM_FORMULA_RE = re.compile(
    rf"(?:[-\u2013]\s*([^=\n]{{1,44}}?)\s*=\s*)?"
    rf"([{SIGN_CHARS}]?)\s*(\d+[,.]\d+)\s*\+\s*\((\d+[,.]\d+)\s*x\s*EPEXDAM\)",
    re.IGNORECASE,
)
# Exact labels, never a substring. The Empower card prints SEVEN energy rows,
# and "Flextime Heures pleines" contains "heures pleines": a substring match
# binds the Flextime band to the bi-hourly meter, 0,1388 against 0,1264 on the
# April card, ~10% high. An unmapped label is dropped rather than guessed - the
# Flextime triplet belongs to the tou kind, which carries no coefficients at
# all. The empty key is the Empty House card, whose single formula is printed
# bare with no "- <label> =" prefix.
_EPEXDAM_BAND: dict[str, str] = {
    "": "single",
    "normal": "single",
    "tarif bihoraire heures pleines": "peak",
    "tarif bihoraire heures creuses": "offpeak",
    "exclusif nuit": "exclusive_night",
    # The Empower card's three Flextime rows, on both legs. They ride in the
    # same block as the bi-hourly rows and are bound by the same Normal row.
    "flextime heures pleines": "flex_peak",
    "flextime heures creuses": "flex_transition",
    "flextime heures super-creuses": "flex_offpeak",
}
_FLEXTIME_BANDS: tuple[str, ...] = ("flex_peak", "flex_transition", "flex_offpeak")


def _epexdam_index(text: str) -> float | None:
    """The EUR/MWh index the card's printed prices were computed at."""
    match = _EPEXDAM_INDEX_RE.search(text)
    return to_float(match.group(1)) if match else None


# "(Mars 2026: 92,57 EUR/MWh)" - the index the printed prices were computed at.
_EPEXDAM_INDEX_RE = re.compile(
    r"EPEXDAM\s+connue\s*\([^)]*?(\d+[,.]\d+)\s*€?\s*/\s*MWh", re.IGNORECASE
)


def _vat_multiplier(text: str, *, professional: bool = False) -> float:
    # The Dynamic formula is printed pre-VAT and scaled by this multiplier
    # (the only caller), so the phrase is mandatory: a reworded header
    # would otherwise fall back to vat_multiplier's 6% default silently
    # and mask a VAT-rate or wording change. Fail loud instead.
    if professional:
        # The professional card prices everything excluding VAT, so the
        # formula needs no scaling here; the snapshot carries vat_rate and
        # _resolve.apply_vat resolves it for the entry. Assert the header all
        # the same, so a card that starts printing VAT-inclusive numbers
        # fails loudly instead of silently under-pricing by 21%.
        if _VAT_EXCLUDED_RE.search(text) is None:
            raise ExtractorError("Engie: professional card is not marked tva exclue")
        return 1.0
    if _VAT_RE.search(text) is None:
        raise ExtractorError("could not parse Engie dynamic VAT multiplier")
    return vat_multiplier(text, _VAT_RE)


_VAT_RE = re.compile(r"(\d+)\s*%\s*de\s*tva\s*comprise", re.IGNORECASE)
_VAT_EXCLUDED_RE = re.compile(r"Prix\s+tva\s+exclue", re.IGNORECASE)
