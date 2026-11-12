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


"""Luminus: the signing campaign whose credit rides on the contract for a year.

A new-customer offer the card prints in one sentence, tied to the month it
is signed in, as opposed to the standing loyalty discounts beside it.
"""

from __future__ import annotations

import re
from bisect import bisect_left

from ..const import WELCOME_CREDIT_ANNIVERSARY, WELCOME_CREDIT_PRO_RATA
from ._luminus_cards import _ANNUAL_ESTIMATE_RE, _LABEL_MONTH_RE, _named_month
from ._parse import tier_bound_kwh, to_float

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
#
# The October 2026 ComfyFlex card drops the signing phrase and names the
# product in the anchor instead: "En tant que nouveau client pour Luminus
# ComfyFlex Electricite, vous beneficiez pour chaque kWh consomme d'une remise
# de 27 % ... pendant 12 mois". That is as plainly a new-customer offer, so the
# product name is a gate of its own. The loyalty clauses open "Si vous etes
# client ... depuis au moins" and match neither form.
_PROMO_GATE_RE = re.compile(
    r"pour la conclusion d['\u2019]un contrat|nouveau\s+client\s+pour\s+Luminus\b",
    re.IGNORECASE,
)
_PROMO_ANCHOR_RE = re.compile(r"En\s+tant\s+que\s+nouveau\s+client", re.IGNORECASE)
# The longest span read as a campaign sentence (``_extract_promo``).
_PROMO_SPAN_MAX = 2_000
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
# "Ce montant est calcule en multipliant par 675 kWh le prix unitaire en
# EUR/kWh TVAC de l'estimation annuelle de l'energie fournie applicable aux
# compteurs mono-horaires" (October 2026 MaxxFlex and ComfyFlex+). MaxxFix
# says "des couts energetiques" there, its printed rate, and earlier cards
# "du cout de l'energie".
_PROMO_VALUED_AT_ESTIMATE_RE = re.compile(
    r"Ce\s+montant\s+est\s+calcul[ée][^.]*?estimation\s+annuelle\s+de\s+l['\u2019]",
    re.IGNORECASE,
)
# "pour la conclusion d'un contrat Luminus MaxxFix Electricite en octobre
# 2026".
_PROMO_MONTH_RE = re.compile(r"\ben\s+([^\W\d_]+)\s+(20\d{2})\b")
_PROMO_NIGHT_RE = re.compile(
    r"non[-\s]valable\s+sur\s+un\s+compteur\s+exclusif\s+nuit", re.IGNORECASE
)
_PROMO_CASHBACK_RE = re.compile(
    r"accord[ée]e?\s+(\d+)\s+mois\s+apr[eè]s[^.]{0,60}?cashback", re.IGNORECASE
)


def _extract_promo(text: str, card_month: str = "") -> dict[str, object]:
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

    The campaign is tied to the month it is signed in, which the sentence
    names ("en octobre 2026") or, where it drops that phrase, the card the
    campaign is printed on does: ``card_month`` is that card's own label
    ("octobre 2026"). It lands in ``welcome_credit_signing_month``, so a
    household signed in another month is not credited with it, a compare
    candidate included.
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
    #
    # Read so that a card costs its length. The anchor before each gate is
    # found by bisection, and a span is kept only when it is a sentence's
    # length and does not end where the span kept before it ends: such a span
    # is a tail of that one, so none of the amounts can read in it where they
    # read nothing in the longer one. A sentence on every card on file is at
    # most 290 characters (November 2026); a span past _PROMO_SPAN_MAX is no
    # sentence and is left unread. Without both, a card of anchors and gates
    # with one full stop at its end kept a span from every anchor to that
    # stop and read each one: the square of its length.
    starts = [m.start() for m in _PROMO_ANCHOR_RE.finditer(flat)]
    spans: list[tuple[int, str]] = []
    kept_end = -1
    for gate_at in _PROMO_GATE_RE.finditer(flat):
        before = bisect_left(starts, gate_at.start())
        if not before:
            continue
        start = starts[before - 1]
        end = flat.find(".", gate_at.start()) + 1
        if not end or end == kept_end or end - start > _PROMO_SPAN_MAX:
            continue
        kept_end = end
        spans.append((start, flat[start:end]))

    sentence = payout = valuation = ""
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
            end = start + len(span)
            payout = flat[end : end + 240]
            valuation = flat[end : end + 600]
            break
        if sentence:
            break

    out: dict[str, object] = {}
    if pct is not None:
        out["welcome_credit_pct_of_energy"] = to_float(pct.group(1)) / 100.0
    if kwh is not None:
        out["welcome_credit_kwh"] = tier_bound_kwh(kwh.group(1))
        # The rate the volume is valued at, when the clause after the
        # campaign names the card's yearly estimate rather than its printed
        # single rate. Left unset if the row is missing, which values the
        # volume at the printed rate as every earlier card asked.
        valued = _PROMO_VALUED_AT_ESTIMATE_RE.search(valuation)
        estimate = _ANNUAL_ESTIMATE_RE.search(flat) if valued is not None else None
        if estimate is not None:
            out["welcome_credit_kwh_rate"] = to_float(estimate.group(1)) / 100.0
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
    signing = _named_month(_PROMO_MONTH_RE.search(sentence)) or _named_month(
        _LABEL_MONTH_RE.fullmatch(card_month.strip())
    )
    if signing is not None:
        out["welcome_credit_signing_month"] = signing
    return out
