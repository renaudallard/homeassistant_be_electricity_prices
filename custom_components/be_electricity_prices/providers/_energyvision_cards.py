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

"""EnergyVision: the product legs its Dutch cards print.

The fixed and dynamic rates, the first-N-kWh tiers that fold into a single
coefficient set, the direct-debit discount and the SPP-weighted feed-in leg.
"""

from __future__ import annotations

from ._parse import SIGN_CHARS, parse_sign, tier_bound_kwh, to_float
from ._pdf import NUM_NO_THOUSANDS, vat_multiplier
from ._rates import DynamicRates, FixedRates, InjectionRates, SpotMonthlyRates
from .base import ExtractorError
import re

_NUM = NUM_NO_THOUSANDS


def _direct_debit_discount(text: str, fee: float) -> float | None:
    """What the card takes off the standing charge for a direct-debit payer.

    ``None`` where the card offers none, which is every EnergyVision card but
    Brusol's Groene stroom: an absent footnote is a product that charges the
    same however it is paid, not a layout drift.

    The card states the charge, the reduction and the reduced total in one
    sentence, so the reading is checked against its own arithmetic rather
    than trusted. A sentence that stops adding up is a re-render this parser
    has misread, and billing a household a discount taken from the wrong
    number is worse than billing it none.
    """
    m = _DIRECT_DEBIT_RE.search(text)
    if m is None:
        return None
    printed_fee = to_float(m.group(1))
    discount = to_float(m.group(2))
    reduced = to_float(m.group(3))
    if abs(printed_fee - fee) > 0.005 or abs(printed_fee - discount - reduced) > 0.005:
        raise ExtractorError(
            f"EnergyVision: direct-debit footnote does not add up "
            f"({printed_fee} - {discount} != {reduced}, fee row says {fee})"
        )
    return discount


def _extract_dynamic(text: str) -> tuple[DynamicRates, InjectionRates]:
    fee = _fee(text)
    # The card quotes the formula "(exclusief btw)"; every printed price is
    # VAT-inclusive, so the energy leg is scaled to the same basis (vat_rate
    # then stays 0.0, matching Frank / Bolt).
    vat = vat_multiplier(text, _VAT_RE)
    energy: DynamicRates | None = None
    injection: InjectionRates | None = None
    for word, factor_s, sign, base_s in _DYN_FORMULA_RE.findall(text):
        base_eur_mwh = parse_sign(sign) * to_float(base_s)
        if word.lower() == "afname":
            # EUR/MWh HTVA -> EUR/kWh incl VAT: the coefficient is a
            # dimensionless Belpex multiplier (* VAT, NO * 10), the base goes
            # EUR/MWh -> EUR/kWh (/1000 * VAT).
            energy = DynamicRates(
                factor=to_float(factor_s) * vat,
                base=base_eur_mwh / 1000.0 * vat,
                yearly_fixed_fee=fee,
                quarter_hourly=True,
            )
        else:
            # Injection is VAT-exempt: factor as-is (exactly 1,0 here), base
            # EUR/MWh -> EUR/kWh, no VAT.
            injection = InjectionRates(
                factor=to_float(factor_s),
                base=base_eur_mwh / 1000.0,
                formula=f"{factor_s} x Belpex {sign} {base_s} EUR/MWh",
            )
    if energy is None:
        raise ExtractorError("EnergyVision: could not parse dynamic afname formula")
    if injection is None:
        # Every dynamic card prints an injection formula; a miss is a layout
        # drift, not a fee-free contract. Raise rather than silently credit 0.
        raise ExtractorError("EnergyVision: could not parse dynamic injectie formula")
    return energy, injection


def _spp_injection(text: str, indicative: float) -> InjectionRates:
    """Injection leg for a fixed card, off its monthly Belpex-SPP-M formula.

    The printed c/kWh figure cannot be the delivery month's rate: the card
    says so itself, *"De waarde van Belpex-SPP-M van de lopende maand is pas
    gekend aan het einde van de maand"*. Surface the formula's coefficients
    with ``spp_indexed`` so the coordinator fetches the Synergrid profile and
    resolves the credit against the delivery month's own solar-weighted mean,
    and keep the printed figure as ``current`` for the months where that mean
    is not available yet.

    ``spp_indexed`` also keeps the coefficients away from the hourly spot:
    they are month coefficients, and the energy leg here is a flat rate that
    fetches no spots of its own.
    """
    formula = _SPP_FORMULA_RE.search(text)
    guarantee = _GUARANTEE_RE.search(text)
    factor: float | None = None
    base: float | None = None
    if formula is not None:
        # Injection is VAT-exempt, so neither coefficient is grossed. The
        # factor is a dimensionless multiplier on the index; the base is
        # EUR/MWh and divides by 1000, as on the dynamic card.
        factor = to_float(formula.group(1))
        base = parse_sign(formula.group(2)) * to_float(formula.group(3)) / 1000.0
    return InjectionRates(
        current=indicative,
        factor=factor,
        base=base,
        formula=formula.group(0) if formula else None,
        spp_indexed=factor is not None,
        minimum=to_float(guarantee.group(1)) / 100.0 if guarantee else None,
    )


def _extract_fixed(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _fee(text)
    m = _FIXED_ENERGY_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse fixed energy price")
    # The fixed rate is printed VAT-inclusive, so it is used as-is.
    energy = FixedRates(single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=fee)
    inj = _FIXED_INJECTION_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse fixed injection price")
    injection = _spp_injection(text, to_float(inj.group(1)) / 100.0)
    return energy, injection


def _tiered_legs(
    text: str,
    *,
    fee: float,
    vat_re: re.Pattern[str],
    tier_re: re.Pattern[str],
    injection_re: re.Pattern[str],
    tranche: bool,
) -> tuple[SpotMonthlyRates, InjectionRates]:
    """Energy + feed-in for a monthly-indexed card, tranche or not.

    The anchors come from the caller, because the Flemish and Walloon cards
    share no wording and merging them into bilingual alternations would cost
    the fail-loud guarantee each set gives on its own publication. What they
    do share is everything below: the formula, the VAT basis, the sign and
    the feed-in, which is why this body is one and not two.

    The tranche and the formula are carried side by side rather than blended
    here: which of them a household actually pays depends on its yearly
    volume, which is entry data and not card data, so ``resolve_volume_tier``
    folds them together when the snapshot is read for an entry.

    ``tranche=False`` is Brusol's "Groene stroom", which prints the same
    monthly formula with nothing in front of it and so bills it from the
    first kWh. The flag comes from the contract rather than from whether the
    row was found, so a tiered card that stops printing its tranche still
    fails loud instead of quietly billing every kWh at the indexed rate.

    ``rlp_blend`` is the Flanders curve because that is what the Dutch cards
    name, "het rekenkundig gemiddelde van de RLP-verbruiksprofielen stroom
    van de verschillende distributienetbeheerders van Vlaanderen". Every
    Flemish sub-area shares one Synergrid curve, so the mean over them IS
    that curve.

    The French card says only "les différents gestionnaires de réseau de
    distribution", naming no region, so the blend is not read off it. It is
    read off the figures instead: EnergyVision publishes its Belpex-RLP-M
    month table in both languages and the two are identical value for value
    (60,280 / 55,349 / ... / 135,655 from June 2024 to August 2026), so
    there is one index for the country and the Dutch card is what defines
    it. A Walloon household on this product is billed on the Flemish curve.
    """
    tier = tier_re.search(text) if tranche else None
    if tranche and tier is None:
        raise ExtractorError("EnergyVision: could not parse the fixed tranche row")
    formula = _RLP_FORMULA_RE.search(text)
    if formula is None:
        raise ExtractorError("EnergyVision: could not parse the Belpex-RLP-M formula")
    # The card quotes the formula "(exclusief btw)" against VAT-inclusive
    # printed prices, so the coefficients are scaled to the same basis the way
    # the dynamic leg's are. The tranche's own rate is printed inclusive and
    # is used as-is.
    vat = vat_multiplier(text, vat_re)
    energy = SpotMonthlyRates(
        factor=to_float(formula.group(1)) * vat,
        base=parse_sign(formula.group(2)) * to_float(formula.group(3)) / 1000.0 * vat,
        tier_kwh=tier_bound_kwh(tier.group(1)) if tier else None,
        tier_rate=to_float(tier.group(2)) / 100.0 if tier else None,
        rlp_indexed=True,
        rlp_blend="flanders",
        yearly_fixed_fee=fee,
    )
    inj = injection_re.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse the injection price")
    # A card that fixes its feed-in price for the term (GSVI3) prints no SPP
    # formula, so this returns the printed figure as a flat credit; the two
    # that index it get the coefficients and the monthly guarantee.
    return energy, _spp_injection(text, to_float(inj.group(1)) / 100.0)


def _extract_tiered(
    text: str, *, tranche: bool = True
) -> tuple[SpotMonthlyRates, InjectionRates]:
    """The Dutch monthly cards: the Flemish tiered range and Brusol's two."""
    return _tiered_legs(
        text,
        fee=_fee(text),
        vat_re=_VAT_RE,
        tier_re=_TIER_FIXED_RE,
        injection_re=_TIER_INJECTION_RE,
        tranche=tranche,
    )


# Dynamic card: "afnametarief ... formule (exclusief btw): 1,05 x Belpex per
# kwartier + 15 EUR/MWh" and "injectietarief ... formule: 1 x Belpex per
# kwartier - 15 EUR/MWh". One findall yields both rows; group 1 keys which.
_DYN_FORMULA_RE = re.compile(
    rf"(afname|injectie)tarief\b[^:]*?:\s*"
    rf"{_NUM}\s*x\s*Belpex\s+per\s+kwartier\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_RE = re.compile(
    rf"Injectie\s*[{SIGN_CHARS}]\s*variabel\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# Tiered cards, page 1: "Groene stroom (<1.800 kWh - vast tarief) 10,60
# €cent/kWh" and its ">" twin for the remainder. The bound's dot is a
# thousands separator, so it goes through tier_bound_kwh rather than to_float,
# which would read 1.800 kWh as one point eight.
_TIER_FIXED_RE = re.compile(
    rf"Groene\s+stroom[^(\n]*\(\s*<\s*([\d.,]+)\s*kWh\s*[{SIGN_CHARS}]\s*"
    rf"vast\s+tarief\s*\)\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# Their feed-in row is either indexed ("variabel") or fixed for the term
# ("vast", GSVI3). Both print one figure; which of the two it is decides
# whether _spp_injection finds a formula to index it on.
# The Brusol "Groene stroom" card qualifies the row, "Injectie - variabel
# (indien van toepassing) 1,28€cent/kWh", so the parenthetical is tolerated.
# It cannot swallow a figure: the group still has to be the next number.
_TIER_INJECTION_RE = re.compile(
    rf"Injectie\s*[{SIGN_CHARS}]\s*(?:variabel|vast)\s*(?:\([^)]*\))?\s+"
    rf"{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# The tranche's remainder: "1,12 x Belpex-RLP-M + 20 EUR/MWh". Same shape as
# the SPP formula below and matched the same way, with the index name's
# hyphens literal so the sign group cannot bind one of them.
_RLP_FORMULA_RE = re.compile(
    rf"{_NUM}\s*x\s*Belpex[\s{SIGN_CHARS}]*RLP[\s{SIGN_CHARS}]*M\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)
# The fixed cards state the injection formula in prose, identically in both
# languages: "0,6 x Belpex-SPP-M - 15 EUR/MWh". The separators inside the
# index name are hyphens, so they are matched literally rather than through
# SIGN_CHARS, which would let the sign group bind one of them.
_SPP_FORMULA_RE = re.compile(
    rf"{_NUM}\s*x\s*Belpex[\s{SIGN_CHARS}]*SPP[\s{SIGN_CHARS}]*M\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)
# "dan garanderen wij in elk geval 1 EURcent/kWh" / "nous garantissons en tout
# etat de cause 1 EURcent/kWh". Parsed rather than hardcoded so a change to the
# guarantee is picked up instead of silently under-crediting.
_GUARANTEE_RE = re.compile(
    r"(?:garanderen\s+wij\s+in\s+elk\s+geval"
    r"|garantissons\s+en\s+tout\s+(?:é|e)tat\s+de\s+cause)"
    rf"\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# Brusol's "Groene stroom" footnote, which prices a direct-debit payer: "De
# vaste vergoeding bedraagt € 250 . Indien je kiest voor domiciliëring dan
# krijg je een extra korting van € 20 , zodat je totale vaste vergoeding
# € 230 bedraagt." All three figures are captured so the reduction can be
# checked against the total the same sentence states.
_DIRECT_DEBIT_RE = re.compile(
    rf"vaste\s+vergoeding\s+bedraagt\s*€?\s*{_NUM}\s*€?[\s.]*"
    rf"Indien\s+je\s+kiest\s+voor\s+domicili[eë]ring[^0-9]*{_NUM}\s*€?"
    rf"[^0-9]*?vaste\s*\n?\s*vergoeding\s*€?\s*{_NUM}",
    re.IGNORECASE,
)


def _fee(text: str) -> float:
    m = _FEE_RE.search(text)
    if m is None:
        # The vaste vergoeding standing charge is mandatory; fail loud rather
        # than silently bill a zero yearly fee on a layout drift.
        raise ExtractorError("EnergyVision: vaste vergoeding row not found")
    return to_float(m.group(1))


# The card header prints "Alle prijzen en tarieven zijn inclusief 6% BTW".
_VAT_RE = re.compile(r"(\d+)\s*%\s*BTW", re.IGNORECASE)
# Fixed card energy + its printed monthly injection indicative (page 1).
_FIXED_ENERGY_RE = re.compile(
    rf"Groene\s+stroom\s*[{SIGN_CHARS}]\s*vast\s+tarief\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FEE_RE = re.compile(rf"Vaste\s+vergoeding\s+{_NUM}\s*€\s*/\s*jaar", re.IGNORECASE)
