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

"""Bolt: the product legs its card prints.

The energy formula and its coefficients, the older layout still held in the
archive, the CWaPE band rates and the feed-in leg. This is what Bolt charges
for power, as opposed to what it collects on someone else's behalf.
"""

from __future__ import annotations

from ._parse import SIGN_CHARS, parse_sign, to_float
from ._pdf import vat_multiplier
from ._rates import EnergyRates, FixedRates, InjectionRates, TariffKind, VariableRates
from .base import ExtractorError
import re


def _consumption_formula(
    text: str, *, professional: bool = False
) -> tuple[float, float] | None:
    """The card's consumption formula as (factor, base) in the EUR/kWh basis.

    Bolt prints one tariff formula per card, ``Belpex * <factor> <sign>
    <base>`` in EUR/MWh HTVA, and settles it either per quarter-hour or
    against the RLP-weighted month depending on what the customer chose. Both
    readings want the same pair, so it is parsed once here: the factor is a
    dimensionless ratio (* VAT), the base goes EUR/MWh -> EUR/kWh (/1000 *
    VAT). VAT is baked because the snapshot's vat_rate is 0.

    ``None`` when the card carries no formula table, which is not an error:
    the printed monthly rate is a complete variable card on its own, and
    ``_with_slot_formula`` already handles the same absence on the injection
    side ("no formula on this card generation: the printed figure is all
    there is"). Raising here instead would fail the WHOLE snapshot for every
    Bolt variable contract the day the row moved, turning a card that still
    prices perfectly into a dead entry; the quarter-hourly settlement is the
    only thing that actually needs the pair, and it goes inert without it
    while the live check reports the loss.

    The professional card prices everything excluding VAT, so there is
    nothing to bake and the snapshot's vat_rate carries the 21% instead. It
    also drops the "N% TVA" phrase the multiplier reads, which would
    otherwise fall back to the residential 6% default and scale the formula
    twice over. That one IS an error, because a formula was found and read.
    """
    matches = _BELPEX_FORMULA_RE.findall(text)
    if not matches:
        return None
    factor_s, sign, base_s = matches[0]
    if professional:
        if "HTVA" not in text:
            raise ExtractorError("Bolt: professional card is not marked HTVA")
        vat = 1.0
    else:
        # No residential Bolt card prints a "N% TVA" phrase: they read "TVAC"
        # and mark the settlement formula "HTVA", so the multiplier is the
        # residential rate by default rather than by reading, and it IS
        # load-bearing here, since the settlement leg and the Impact bands
        # below are grossed by it.
        vat = vat_multiplier(text, _VAT_PHRASE_RE, default=_RESIDENTIAL_VAT)
    base_eur_mwh = parse_sign(sign) * to_float(base_s)
    return to_float(factor_s) * vat, base_eur_mwh / 1000.0 * vat


def _extract_legacy_energy(
    text: str, kind: TariffKind, yearly_fee: float
) -> EnergyRates | None:
    """Read a pre-April-2026 Bolt card, or ``None`` if this is not one.

    Bolt redesigned its cards between March and April 2026. The archive PDFs
    for the earlier months are still served, and ``fetch_for_month`` reaches
    for them whenever a year-to-date walk crosses Q1 or a contract was signed
    then, but they carry no ``Prix mensuel`` row: the rates sit under
    ``Coût de l'énergie`` with one labelled line per meter type,

        Coût de l'énergie Simple
         c€13,27/kWh
                       Jour
         c€13,27/kWh
                       Nuit
         c€13,27/kWh
                       Excl. nuit c€13,27/kWh

    so ``parse_snapshot`` raised, ``fetch_for_month`` swallowed it, and every
    Q1 month silently billed at the CURRENT card's rate instead.

    Keyed on which anchor the card actually carries rather than on a date, so
    it neither guesses at a boundary nor needs touching when Bolt redesigns
    again. Returns ``None`` when the old anchor is absent too, leaving the
    caller to raise its own error.
    """
    if not re.search(r"Co[ûu]t de l['’]énergie", text):
        return None

    def _rate(label: str) -> float | None:
        # Values render as "c€13,27/kWh", the label sometimes on the line
        # above and sometimes inline, so allow a bounded gap but never cross
        # into another label's value.
        m = re.search(
            rf"{label}[^\n]*\n?[^c\n]*c€\s*([\d.,]+)\s*/\s*kWh", text, re.IGNORECASE
        )
        return to_float(m.group(1)) / 100.0 if m else None

    mono = _rate(r"Co[ûu]t de l['’]énergie\s+Simple")
    if mono is None:
        return None
    peak = _rate(r"\bJour\b") or mono
    offpeak = _rate(r"\bNuit\b") or mono
    excl = _rate(r"Excl\.?\s*nuit") or mono
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    if kind == "variable":
        return VariableRates(
            current=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    # Only the archived fix / variable families use this layout; a dynamic
    # card is handled by its own formula branch before we get here.
    return None


def _extract_energy(
    text: str, kind: TariffKind, *, professional: bool = False
) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    # Bolt's 'Prix mensuel' line is the current month's price for all
    # contract kinds. Static cards have only this; variable cards also
    # show 'Prix annuel estimé' which we ignore.
    #
    # The row renders in one of two shapes, and which one is a property of
    # the individual card render rather than of the product: either two
    # numbers (mono then Exclusif nuit, with the bi-horaire pair left to
    # the "Prix de l'électricité verte" block below), or all four columns
    # inline (mono, Jour, Nuit, Exclusif nuit). Reading the four-number
    # shape with the two-number rule would take Jour for the
    # exclusive-night rate and bill a night circuit at the day price.
    match = re.search(r"Prix mensuel([^\n]*)", text)
    if match is None:
        legacy = _extract_legacy_energy(text, kind, yearly_fee)
        if legacy is not None:
            return legacy
    numbers = re.findall(r"[\d.,]+", match.group(1)) if match else []
    inline_bihourly = len(numbers) >= 4
    if len(numbers) < 2:
        raise ExtractorError(f"could not parse Bolt {kind} consumption block")
    mono = to_float(numbers[0]) / 100.0
    excl = to_float(numbers[3] if inline_bihourly else numbers[1]) / 100.0
    # The "Prix de l'électricité verte" block prints two "Jour Nuit"
    # subheads: the first is for consumption (with our bi-horaire
    # row), the second is for injection. The bi-horaire row is always
    # the LAST same-line adjacent-number pair between them. pdfplumber
    # sometimes renders the annual-estimate column vertically above
    # that row (variable cards) and sometimes drops it entirely (fixed
    # cards), so we can't anchor on a fixed offset; restricting to the
    # span between the two subheads is the stable invariant.
    span = re.search(
        r"Prix de l'électricité verte.*?Jour\s+Nuit(.*?)Jour\s+Nuit",
        text,
        re.S,
    )
    pairs = (
        re.findall(
            r"^[ \t]*([\d.,]+)[ \t]+([\d.,]+)[ \t]*$",
            span.group(1),
            re.MULTILINE,
        )
        if span
        else []
    )
    if inline_bihourly:
        # The row already carried them, and is the more reliable source:
        # the span anchor below walks into the DSO table on the renders
        # that inline the pair.
        peak = to_float(numbers[1]) / 100.0
        offpeak = to_float(numbers[2]) / 100.0
    elif pairs:
        peak = to_float(pairs[-1][0]) / 100.0
        offpeak = to_float(pairs[-1][1]) / 100.0
    elif kind == "fixed":
        # Bolt fixed cards are mono == peak == offpeak and sometimes omit
        # the bi-horaire row entirely; the single rate is the right value.
        peak = offpeak = mono
    else:
        # Variable cards always publish distinct Jour / Nuit rates; a miss
        # is a layout drift, not a mono contract. Fail loud rather than
        # silently bill a bi-hourly user at the mono rate.
        raise ExtractorError(f"could not parse Bolt {kind} bi-hourly Jour/Nuit rates")

    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    if kind == "variable":
        # A Walloon card prices this product two ways and lets the customer
        # pick; the incitative bands ride on the same contract, selected by
        # dso_tariff_mode, exactly as the DSO side already is.
        # The Impact block is residential and Walloon; a professional card
        # is HTVA and carries no such block.
        bands = _impact_energy_bands(
            text,
            1.0
            if professional
            else vat_multiplier(text, _VAT_PHRASE_RE, default=_RESIDENTIAL_VAT),
        )
        # ``current`` is the printed Prix mensuel, which is what a household
        # settling against the RLP-weighted month is billed. The coefficients
        # beside it are the same formula read per quarter-hour, which is the
        # other settlement the card sells; resolve_settlement_grid builds the
        # dynamic leg out of them when the entry says so. Carried on every
        # variable card, not only where the box is ticked, because the parser
        # has no entry to consult and the pair is free to read.
        #
        # A card with no formula table still prices: the printed rate is the
        # whole variable contract, and only the quarter-hourly settlement
        # needs the pair. It goes inert rather than taking the entry down.
        coefficients = _consumption_formula(text, professional=professional)
        factor, base = coefficients or (None, None)
        return VariableRates(
            current=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
            formula=(
                None if coefficients is None else f"Belpex * {factor:.6g} + {base:.6g}"
            ),
            formula_factor=factor,
            formula_base=base,
            impact_pic=bands.get("pic"),
            impact_medium=bands.get("medium"),
            impact_eco=bands.get("eco"),
        )
    # Bolt sells no tou / tou_impact product, and its dynamic settlement is a
    # per-entry reading of the variable card rather than a kind of its own, so
    # anything else here is a registry mistake.
    raise ExtractorError(f"Bolt: unexpected contract kind {kind!r}")


def _impact_energy_bands(text: str, vat: float) -> dict[str, float]:
    """The three CWaPE supplier-energy rates, or ``{}``.

    Derived from each band's formula and its own index rather than read off
    the printed column, because the card's own arithmetic disagrees with one
    of them: at the Q2 2026 indices Eco resolves to 9,912 against a printed
    9,91 and Pic to 19,232 against 19,23, but Medium resolves to 15,636
    against a printed 14,64. One digit, and taking the printed value would
    bake a supplier typo into every Medium hour.
    """
    out: dict[str, float] = {}
    for band, _printed, index, factor, sign, base in _IMPACT_ROW_RE.findall(text):
        # EUR/MWh HTVA throughout, so /1000 to EUR/kWh, then the card's VAT.
        rate = (
            (to_float(factor) * to_float(index) + parse_sign(sign) * to_float(base))
            / 1000.0
            * vat
        )
        out[band.lower()] = rate
    return out if len(out) == 3 else {}


def _with_slot_formula(text: str, current: float) -> InjectionRates:
    """Injection leg for a non-dynamic card: the printed figure AND the
    quarter-hourly formula stated beside it.

    Bolt's fixed and variable cards carry the same Belpex formula table the
    dynamic card does, and say the printed column is only an illustration:
    *"Le tableau ci-dessus indique le prix de vente base sur la valeur Belpex
    la plus recente. Dans la facturation, l'injection par quart d'heure est
    multipliee par la valeur Belpex pour ce quart d'heure."* The FIXED card
    adds *"Contrairement au prix fixe de consommation pour l'electricite, le
    prix pour l'injection est quant a lui variable selon l'indice Belpex."*

    That printed value moves only when the QUARTERLY index does, and the
    archive shows it: 202604, 202605 and 202606 all print 5,31; 202607 and
    202608 both print 3,40. Crediting it flat misses every negative quarter
    the contract really pays, and 15% of Apr-Aug 2026 quarters are negative.

    The formula row is picked out by ``factor < 1``, the same discriminator
    the dynamic branch uses: Bolt redistributes a fraction of the spot on the
    injection side and marks it up on every consumption row. ``current`` is
    kept as the fallback for an entry with no ENTSO-E key.
    """
    matches = _BELPEX_FORMULA_RE.findall(text)
    inj = next((m for m in matches if to_float(m[0]) < 1.0), None)
    if inj is None:
        # No formula on this card generation: the printed figure is all there
        # is, and crediting it is better than crediting nothing.
        return InjectionRates(current=current, factor=None, base=None, formula=None)
    return InjectionRates(
        current=current,
        # Feed-in is VAT-exempt for residential, so no VAT bake; base goes
        # EUR/MWh -> EUR/kWh.
        factor=to_float(inj[0]),
        base=parse_sign(inj[1]) * to_float(inj[2]) / 1000.0,
        formula=f"Belpex * {inj[0]} {inj[1]} {inj[2]}",
        slot_indexed=True,
    )


def _extract_injection(text: str) -> InjectionRates | None:
    """The feed-in leg, which is the same on either settlement.

    Bolt's card says the injection formula is applied per quarter-hour
    whatever the consumption side settles on ("la consommation ou l'injection
    enregistree est multipliee, pour chaque quart d'heure"), so there is one
    reading here and no branch on the contract kind. ``_with_slot_formula``
    keeps both halves: the printed indicative, which is the fallback for an
    entry with no ENTSO-E key, and the formula that is actually billed.

    Injection is a flat monthly indicative ("Prix mensuel 5,31 4,03")
    in the block that follows the "Injection" header, on both fix and
    variable cards (the consumption "Prix mensuel" sits above it).
    Anchored on the header rather than counting "Prix mensuel"
    occurrences, so a third consumption-side row cannot shift the match.
    The July 2026 fix cards print a NEGATIVE second ("Exclusif nuit")
    column ("Prix mensuel 3,40 -0,43"); only the first column is billed
    but the second is a required anchor token, so its optional minus sign
    is allowed. The billed first column carries an optional minus too, so
    a month that ever prints a negative feed-in indicative is captured
    instead of failing the match and dropping the credit.
    """
    m = re.search(r"Injection\b.*?Prix mensuel\s+(-?[\d.,]+)\s+-?[\d.,]+", text, re.S)
    if m:
        current = to_float(m.group(1)) / 100.0
        return _with_slot_formula(text, current)
    # A pre-April-2026 archive card has no "Prix mensuel" row. It prints the
    # indicative under the "Tarif d'injection (HTVA)" heading as
    # "Injection (c€/kWh) 5,87 6,69 3,78". Missing it dropped the feed-in
    # credit entirely for every archived month a year-to-date walk crosses.
    #
    # Those three columns are METER REGISTERS, not regions. Their header is
    # "(*) TVA non applicable. Simple Jour Nuit", a few lines up, and the
    # Belpex row above them uses the same three. The VL/WAL/BX headers on that
    # page govern the TAX rows only. Reading them as regions credited Wallonia
    # the Jour rate and Brussels the Nuit one, which is why the first column
    # is taken here regardless of region: exactly what the current card's
    # "Prix mensuel" branch above does with its own Simple / Exclusif-nuit
    # pair. peak / offpeak stay None: they are consulted only for a
    # TimeOfUseRates energy leg, and these are fixed and variable cards.
    legacy = re.search(
        r"Injection\s*\(c€/kWh\)\s*(-?[\d.,]+)",
        text,
    )
    if not legacy:
        return None
    return _with_slot_formula(text, to_float(legacy.group(1)) / 100.0)


# Bolt's variable card prints its tariff formula for both consumption and
# injection as "Belpex * <factor> <sign> <base>" in EUR/MWh (HTVA), one row per
# meter type. The dynamic contract applies the same coefficients to the live
# quarter-hourly Belpex spot. The consumption formula is the first match; the
# injection formula is the first match that differs from it (Bolt lists all
# consumption rows, then all injection rows).
_BELPEX_FORMULA_RE = re.compile(
    rf"Belpex\s*\*\s*([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)"
)
# The Walloon "Tarif Impact (Wallonie)" block, one row per CWaPE band:
#   "Eco consommation 9,91 65,59 Belpex * 1,168 + 16,90"
# printed price, that band's own quarterly index, then the shared formula.
_IMPACT_ROW_RE = re.compile(
    rf"(Eco|Medium|Pic)\s+consommation\s+([\d,]+)\s+([\d,]+)\s+"
    rf"Belpex\s*\*\s*([\d,]+)\s*([{SIGN_CHARS}])\s*([\d,]+)",
    re.IGNORECASE,
)
# The residential VAT the settlement formula and the Impact bands are grossed
# by. Named rather than left to the helper's default: the residential cards
# print no "N% TVA" phrase for the multiplier to read, so this is the value
# every residential entry bills on, and a helper default is where a reader
# would not look for it.
_RESIDENTIAL_VAT = 1.06
_VAT_PHRASE_RE = re.compile(r"(\d+)\s*%\s*(?:TVA|BTW)", re.IGNORECASE)


def _extract_yearly_fee(text: str) -> float:
    """Bolt prints a monthly platform fee (``€ 10,99 / mois``); convert to /year.

    The platform fee is the entire Bolt monetisation; a missing match is
    a layout drift that would silently undercount the user's bill by
    ~130 EUR/year, so raise instead of returning 0.

    Make the decimal portion optional so a future round fee like
    ``€ 11 / mois`` still parses; today every Bolt card prints two
    decimals, but the strictness was a footgun rather than a feature.
    """
    match = re.search(r"€\s*(\d+(?:[.,]\d+)?)\s*/\s*mois", text)
    if match is None:
        raise ExtractorError("Bolt: '€ N[,NN] / mois' platform fee not found")
    return to_float(match.group(1)) * 12.0
