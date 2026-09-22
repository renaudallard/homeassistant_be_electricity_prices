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

The energy formula and its per-slot coefficients, the feed-in leg and the
signing campaign whose credit rides on the contract for a year. This is what
Luminus charges for power, as opposed to what it collects for someone else.
"""

from __future__ import annotations

from ..const import WELCOME_CREDIT_ANNIVERSARY, WELCOME_CREDIT_PRO_RATA
from ._parse import SIGN_CHARS, numeric_row, parse_sign, tier_bound_kwh, to_float
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
from dataclasses import replace
import re
from ._luminus_overlays import _NUM
from ._pdf import vat_multiplier


def _extract_promo(text: str) -> dict[str, object]:
    """Luminus's new-customer campaign, or ``{}`` when the card runs none.

    Returned as the snapshot fields it fills, so the caller does not restate
    which is which. Two shapes are read, both verified against the September
    2026 cards and against Luminus's own published conditions: a PERCENTAGE of
    the energy cost paid across the invoices it covers (33% on Comfy, 29% on
    ComfyFlex), and a VOLUME of free energy paid as a cashback after a stated
    wait (750 kWh on MaxxFix, MaxxFlex and both Plus cards).

    Scoped to the campaign SENTENCE, not the card. The cards print standing
    loyalty discounts in the same block and in nearly the same words, and
    those are a different thing: "12 mois apres la date de debut, une
    reduction de 5 % sur les couts energetiques ... pendant 12 mois ... au
    pro rata de la consommation de votre 2e annee". They are a year-2 and
    year-3 benefit this does not model, they also say "pendant 12 mois", and
    they carry their own exclusive-night exclusion. Reading the card as a
    whole picked that exclusion up and attached it to the campaign, and a
    looser amount pattern would have read 5% as a welcome credit. The gate
    phrase is what separates them, because only a campaign is tied to the
    month of signing.

    A promo stated without that gate is left alone rather than guessed at:
    Luminus Dynamic prints "Reduction unique Cashback de 130 EUR TVA incl.
    apres 12 mois" with no signing condition, so nothing on the card says
    whether an existing customer gets it too.
    """
    flat = re.sub(r"\s+", " ", text)
    # The curly apostrophe matters as much as the pairing: April's Comfy closes
    # its flat sentence with a curly gate and its percentage one with a
    # straight gate, so a plain string search for one form skipped the near
    # gate entirely and scoped the campaign across 11.512 characters.
    #
    # ONE sentence supplies everything: the amount, the payout wording, the
    # wait and the night exclusion. A card can carry two of these sentences,
    # and April's Comfy does (60,00 EUR flat at 1.283, 11% of the energy cost
    # at 12.513, both naming the same product and signing month), but the
    # snapshot holds ONE payout kind and one wait, so taking the amount from
    # one sentence and the conditions from another is how the 11% campaign
    # came out marked as a cashback at the wait quoted after the flat one.
    #
    # The percentage and volume shapes are preferred where a card states one,
    # because they are the shapes that ride a year and carry the exclusions.
    # A card whose only campaign is a flat amount is read on its own terms.
    # Each GATE with the LAST anchor before it, not each anchor with the next
    # gate. An anchor whose own sentence carries no gate would otherwise borrow
    # the next sentence's, and its span then runs across everything between the
    # two: on real wording that reads the standing "remise de 5 %" loyalty
    # clause as the campaign where the card grants 33%, 215,99 EUR a year on a
    # 3.500 kWh Comfy, and picks up that clause's exclusive-night exclusion on
    # the way, which takes a night-metered household to nothing. Pairing from
    # the gate backwards cannot span a sentence boundary that way, because the
    # nearest anchor is by definition the one the gate belongs to.
    starts = [m.start() for m in _PROMO_ANCHOR_RE.finditer(flat)]
    spans: list[tuple[int, str]] = []
    for gate_at in _PROMO_GATE_RE.finditer(flat):
        before = [s for s in starts if s < gate_at.start()]
        if not before:
            continue
        start = before[-1]
        spans.append((start, flat[start : flat.find(".", gate_at.start()) + 1]))

    sentence = payout = ""
    pct = kwh = eur = None
    for want_flat in (False, True):
        for start, span in spans:
            found_pct = _PROMO_PCT_RE.search(span)
            found_kwh = _PROMO_KWH_RE.search(span)
            found_eur = _PROMO_EUR_RE.search(span)
            if want_flat:
                if found_eur is None:
                    continue
            elif found_pct is None and found_kwh is None:
                continue
            pct, kwh, eur = found_pct, found_kwh, found_eur
            sentence = span
            payout = flat[start + len(span) :][:240]
            break
        if sentence:
            break

    out: dict[str, object] = {}
    if pct is not None:
        out["welcome_credit_pct_of_energy"] = to_float(pct.group(1)) / 100.0
    if kwh is not None:
        out["welcome_credit_kwh"] = tier_bound_kwh(kwh.group(1))
    if eur is not None:
        # to_float, not tier_bound_kwh: this is money with a decimal comma
        # ("60,00"), where a volume bound is an integer with a thousands dot
        # ("1.000 kWh"). Reading one with the other's reader gives 6000,00.
        out["welcome_credit_eur"] = to_float(eur.group(1))
    if not out:
        return {}
    cashback = _PROMO_CASHBACK_RE.search(payout)
    if cashback is not None:
        out["welcome_credit_kind"] = WELCOME_CREDIT_ANNIVERSARY
        out["welcome_credit_after_months"] = int(cashback.group(1))
    else:
        # "repartie au pro rata sur vos prochains decomptes": paid across the
        # invoices of the period it covers, not at its end.
        out["welcome_credit_kind"] = WELCOME_CREDIT_PRO_RATA
    if _PROMO_NIGHT_RE.search(sentence):
        out["welcome_credit_excludes_night_meter"] = True
    return out


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


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee = _extract_yearly_fee(text)
    if kind == "tou":
        # SmartFlex's TOU table prints exactly three rates on the first
        # "Énergie fournie" row, e.g. "(c€/kWh) 15,54 13,29 6,72". The
        # second occurrence later in the PDF is the bi-horaire fallback
        # for non-SMR3 customers; we anchor on the first match.
        tou_row = numeric_row(text, "Énergie fournie (c€/kWh)", 3)
        if not tou_row:
            raise ExtractorError("could not parse Luminus TOU energy block")
        peak = to_float(tou_row[0]) / 100.0
        transition = to_float(tou_row[1]) / 100.0
        offpeak = to_float(tou_row[2]) / 100.0
        # SmartFlex's three bands (pleines / creuses / super-creuses) use
        # SEASONAL windows: peak (pleines) is 07-11 + 17-22 all year, the
        # cheapest super-creuses band applies 11-17 only in spring/summer
        # (21/03-20/09), and 22-07 is always creuses. The weekend_rule
        # "smartflex_seasonal" tells pricing.tou_slot to bill those windows
        # (the "free Sundays" first-year promo is not modelled).
        tou_rates = TimeOfUseRates(
            peak=peak,
            transition=transition,
            offpeak=offpeak,
            yearly_fixed_fee=fee,
            weekend_rule="smartflex_seasonal",
        )
        # SmartFlex indexes each band on the delivery month, same sentence as
        # its bi-hourly siblings: "Votre tarif sera indexe tous les mois. La
        # valeur Belpex du mois en cours n'est connue qu'a la fin du mois."
        # So the printed triplet is the previous month's.
        coefs = _monthly_tou_coefficients(text)
        if not coefs:
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
        )

    if kind == "dynamic":
        match = _DYNAMIC_FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Luminus dynamic formula")
        factor_pdf = to_float(match.group(1))
        base_pre_vat_cents = parse_sign(match.group(2)) * to_float(match.group(3))
        vat = _vat_multiplier(text)
        # PDF formula: c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents.
        # Spot in EUR/kWh = Belpex_eur_mwh / 1000. Convert to:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=fee,
        )

    energy_row = numeric_row(text, "Énergie fournie (c€/kWh)", 4)
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
    coefs = _monthly_energy_coefficients(text)
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
        formula_factor_exclusive_night=coefs.get("exclusive_night", none2)[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", none2)[1],
    )


def _monthly_tou_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """The three SmartFlex per-slot monthly coefficients, or ``{}``.

    Gated on the energy block attributing its index to a MONTH, which is what
    separates this card from ComfyFlex's quarterly one, and scoped to that
    block so the card's second MaxxFlex-identical section (the non-SMR3
    fallback) cannot supply them.

    All three bands are required. A partial match would leave one slot on the
    printed rate and the others on the index, which is worse than either.
    """
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}
    if _MONTHLY_INDEX_ATTRIBUTION_RE.search(block.group(0)) is None:
        return {}
    vat = _vat_multiplier(text)
    out: dict[str, tuple[float, float]] = {}
    for key, label in _TOU_BANDS:
        match = _band_formula_re(label).search(block.group(0))
        if match is None:
            continue
        out[key] = (
            to_float(match.group(1)) * 10.0 * vat,
            parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0 * vat,
        )
    return out if len(out) == len(_TOU_BANDS) else {}


def _monthly_energy_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """Per-meter ``(factor, base)`` for a card that indexes energy monthly.

    Empty unless the card carries the arithmetic-mean sentence AND its energy
    block yields a mono row. Both halves matter: ComfyFlex has the block but
    quotes a QUARTERLY index, SmartFlex has a MaxxFlex-identical block for a
    non-SMR3 meter but not the sentence, and a fixed card has neither.
    """
    if _MONTHLY_ARITHMETIC_RE.search(text) is None:
        return {}
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}
    # The row prints c/kWh HTVA against an index in EUR/MWh, while the energy
    # row itself is TVAC, so both coefficients take the x10 / 100 conversion
    # and the VAT multiplier. Round-trips to the printed 14,41 at the card's
    # own 92,61 index.
    vat = _vat_multiplier(text)
    out: dict[str, tuple[float, float]] = {}
    for key, label in _ENERGY_BANDS:
        match = _band_formula_re(label).search(block.group(0))
        if match is None:
            continue
        out[key] = (
            to_float(match.group(1)) * 10.0 * vat,
            parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0 * vat,
        )
    return out if "single" in out else {}


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
    # ("Tarif de l'énergie \ninjectée").
    indicative = re.search(
        rf"Tarif\s+de\s+l[\"'’©]énergie\s+injectée"
        rf"[^0-9-]*(?:\d+\s+)?({_NUM})",
        text,
        re.S,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    month_indexed = False
    if kind == "dynamic":
        match = _INJECTION_FORMULA_RE.search(text)
        if match:
            factor_pdf = to_float(match.group(1))
            base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
            # Residential injection is VAT-exempt in Belgium.
            factor = factor_pdf * 10.0
            base = base_pdf_cents / 100.0
            formula = match.group(0)
    else:
        # A non-dynamic card indexes the credit on the delivery month and says
        # the printed figure is not it: "Votre tarif sera indexe tous les mois.
        # La valeur Belpex du mois en cours n'est connue qu'a la fin du mois.
        # Les prix affiches sont calcules sur la base de la derniere valeur
        # Belpex connue (mois precedent)."
        monthly = _INJECTION_MONTHLY_RE.search(text)
        if monthly is not None:
            # c/kWh per EUR/MWh of index, and the block is HTVA with the card
            # noting "La TVA s'eleve a 0%", so nothing is grossed.
            factor = to_float(monthly.group("factor")) * 10.0
            base = (
                parse_sign(monthly.group("sign"))
                * to_float(monthly.group("base"))
                / 100.0
            )
            formula = " ".join(monthly.group(1).split())
            month_indexed = True

    if current is None and factor is None:
        # Both Luminus card families always publish injection: the
        # applicable indicative on fixed/variable/TOU cards, the spot
        # formula on dynamic. A miss is a layout drift, not a fee-free
        # contract; fail loud rather than silently crediting an injection
        # user nothing, mirroring the consumption helpers that raise.
        raise ExtractorError("Luminus: could not parse injection rate")
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
    )


# Luminus runs occasional new-customer campaigns and prints them in one
# regular sentence:
#
#   "(***) En tant que nouveau client, vous beneficiez d'une remise de 33% sur
#    les couts energetiques, non-valable sur un compteur exclusif nuit, sur
#    votre consommation annuel en heures pleines et creuses pendant 12 mois
#    pour la conclusion d'un contrat Luminus Comfy Electricite en septembre
#    2026."
#
# followed by how it is paid: "repartie au pro rata sur vos prochains
# decomptes" for a percentage, or "accordee 12 mois apres la date de debut via
# un cashback" for a volume.
#
# These are SIGNING-MONTH offers, which is what "pour la conclusion d'un
# contrat ... en septembre 2026" says, so an entry is credited off the card of
# the month it SIGNED, which `signing_month_snapshot` already resolves. Across
# the nine archived months of 2026 exactly one carried a campaign, on six
# contracts, so the normal answer here is no promo at all.
# Both apostrophes, because the cards print both. The April Comfy card uses a
# straight one in this sentence and a curly one elsewhere, and a plain string
# search then bound the gate to whichever occurrence came first in the file
# rather than to the campaign's own: the scope ran from the first anchor to a
# gate 11.000 characters later, which is the whole card, pulling both standing
# loyalty clauses and their exclusive-night exclusion inside it. A card
# printing this sentence curly read as no campaign at all.
_PROMO_GATE_RE = re.compile(r"pour la conclusion d['\u2019]un contrat", re.IGNORECASE)
_PROMO_ANCHOR_RE = re.compile(r"En\s+tant\s+que\s+nouveau\s+client", re.IGNORECASE)
_PROMO_PCT_RE = re.compile(r"remise\s+de\s+(\d+(?:[,.]\d+)?)\s*%", re.IGNORECASE)
# A campaign stated as a flat amount: April's Comfy card prints "une remise de
# 60,00 EUR TVA incl." in its own sentence, under the same signing gate as the
# percentage one lower down. TVA incl. on a residential card is the basis the
# credit is already carried on, so nothing is scaled here.
#
# Up to four digits and an optional decimal comma, which is every amount these
# cards print. A thousands separator is deliberately NOT accepted: "1.250,00"
# matches nothing and the campaign is left unread, where a reader that took it
# would have to guess whether the dot separates thousands or decimals and bill
# 1,25 EUR or 125.000 when it guessed wrong. An unread campaign is a card to
# look at; an invented figure is a wrong bill.
_PROMO_EUR_RE = re.compile(r"remise\s+de\s+(\d{1,4}(?:,\d{1,2})?)\s*EUR", re.IGNORECASE)
# The volume carries a thousands separator on the cards that print one:
# May 2026 Comfy says "une remise de 1.000 kWh", where a plain decimal
# read gives 1,0 kWh. tier_bound_kwh is the shared reader for exactly
# that, written for the excise tranche bounds ("20.000 kWh").
_PROMO_KWH_RE = re.compile(r"remise\s+de\s+(\d[\d\s.,]*\d|\d)\s*kWh", re.IGNORECASE)
_PROMO_NIGHT_RE = re.compile(
    r"non[-\s]valable\s+sur\s+un\s+compteur\s+exclusif\s+nuit", re.IGNORECASE
)
_PROMO_CASHBACK_RE = re.compile(
    r"accord[ée]e?\s+(\d+)\s+mois\s+apr[eè]s[^.]{0,60}?cashback", re.IGNORECASE
)
_DYNAMIC_FORMULA_RE = re.compile(
    rf"Prélèvement\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)
# A non-dynamic card's feed-in formula, accepted ONLY when that same block
# says the tariff is indexed monthly.
#
# The cadence sentence is the whole point. ComfyFlex and ComfyFlex+ print the
# IDENTICAL "0,0481 x Belpex - 0,6392" and index it QUARTERLY ("Votre tarif
# sera indexe tous les trimestres", against "du 1re trimestre 2026"). Keying on
# the formula would sweep them onto a monthly mean the contract never mentions:
# measured, their printed quarterly figure is 1,9% off the truth while April's
# month mean is 16,3% off it in the other direction, so the "fix" would be
# strictly worse than the lag it replaces.
#
# The leading guard keeps the scan inside the injection block: the SmartFlex
# card carries a second, MaxxFlex-identical block for a non-SMR3 meter.
_INJECTION_MONTHLY_RE = re.compile(
    r"Formule\s+tarifaire\s+de\s+l['\u2019\u00a9]énergie\s+injectée"
    r"(?:(?!Formule\s+tarifaire).)*?"
    rf"=\s*((?P<factor>{_NUM})\s*x\s*Belpex\s*"
    rf"(?P<sign>[{SIGN_CHARS}])\s*(?P<base>{_NUM}))"
    r"(?:(?!Formule\s+tarifaire|Votre\s+tarif\s+sera\s+indexé).){0,400}?"
    r"Votre\s+tarif\s+sera\s+indexé\s+(?:chaque|tous\s+les)\s+mois\b",
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
# The ENERGY block only. Scoping matters: searched over the whole document the
# mono pattern also finds the INJECTION formula ("0,0481 x Belpex - 0,6392"),
# and a fixed card would gain an energy formula it does not have.
_ENERGY_FORMULA_BLOCK_RE = re.compile(
    r"Formules\s+tarifaires\s+pour\s+le\s+co[uû]t\s+de\s+l['\u2019\u00a9]\s*[ée]nergie"
    r"(?:(?!Formule\s+tarifaire\s+de).)*",
    re.S,
)
_ENERGY_BANDS: tuple[tuple[str, str], ...] = (
    ("single", r"Compteur\s+mono-horaire"),
    ("peak", r"Heures\s+pleines(?:\s*\([^)]*\))?"),
    ("offpeak", r"Heures\s+creuses"),
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
# "(valeur de l'indice de mars 2026)" against ComfyFlex's "(valeur de l'indice
# du 1re trimestre 2026)". A MONTH attribution is the discriminator that works
# on the SmartFlex card, whose energy block carries no cadence sentence of its
# own: the sentence sits under the injection block and governs that tariff.
_MONTHLY_INDEX_ATTRIBUTION_RE = re.compile(
    r"valeur\s+de\s+l['\u2019\u00a9]indice\s+de\s+(?!\s*\d)\w+", re.IGNORECASE
)


_INJECTION_FORMULA_RE = re.compile(
    rf"Injection\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(
        text,
        re.compile(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", re.S),
        r"TVA\s*(\d+)\s*%",
    )


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
