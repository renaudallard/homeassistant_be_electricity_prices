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

"""The legs a signing cohort is priced on, one builder each.

``_cohort_legs`` in ``cohort.py`` decides which card a signing cohort bills
on; these build the legs it splices in: the rate the household typed, the
energy and injection legs read off the archived signing card, a
month-indexed leg re-priced on its delivery month, and the archived card
itself. Split out of ``cohort.py``, which keeps the decision.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_API_KEY,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
)
from .providers.base import SupplierSnapshot
from .providers._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    TimeOfUseRates,
    VariableRates,
)
from .injection import _slot_coefficients
from .snapshot_resolve import _include_vat


def _manual_energy_leg(
    entry: ConfigEntry, card: EnergyRates, card_vat_rate: float = 0.0
) -> EnergyRates | None:
    """Overlay a hand-entered signing rate onto ``card``, or ``None``.

    The user typed the rate they signed at, so it wins over anything the
    integration can retrieve: ``card`` is the leg that would otherwise bill
    (the archived signing-month card when the supplier keeps one, else the
    current card) and every box the user filled replaces its field. Shaped to
    match the contract's kind (dynamic and spot-monthly -> factor / base,
    fixed -> single / peak / offpeak / exclusive night). Per-kWh values are
    stored as entered
    (grossed by compute_breakdown at the current card's VAT rate). ``None``
    when every box was left blank, or the contract carries none of those
    shapes (TOU / Impact / a plain variable card).

    The fee box is labelled "incl. VAT" and the typed figure is taken at that
    word, so it has to be put onto whatever basis this entry bills on. That is
    VAT-inclusive for every residential entry and for a business that does not
    deduct, and it is EX-VAT for one that does: ``apply_vat`` leaves such an
    entry's card fees as the professional card printed them. Without the
    conversion the typed fee stayed gross while every other fee on the same
    entry was net, so a 121,00 EUR signed fee sat next to a 100,00 EUR card
    fee. ``card_vat_rate`` is the rate the card was published at, read before
    ``apply_vat`` resolves it away; 0.0 (the default) means a VAT-inclusive
    card, where the two bases coincide and nothing changes.
    """
    energy = card
    # Every box on the signed_rate step is optional and the step tells the
    # user "leave blank to keep the retrieved card's value". Honour that PER
    # FIELD: a blank box falls back to that card's value, not to zero.
    # Substituting 0.0 made a user who typed just their locked energy rate
    # lose the standing charge entirely, and on a dynamic contract silently
    # zeroed the formula's base. Only an entirely blank step means "no
    # override at all": no single box is a master switch, so a bi-hourly
    # customer who has no mono rate to type keeps their day / night rates,
    # and a fee-only override applies on its own.
    fee_raw = entry.data.get(CONF_MANUAL_YEARLY_FEE)
    fee = float(fee_raw) if fee_raw is not None else energy.yearly_fixed_fee
    if fee_raw is not None and card_vat_rate and not _include_vat(entry):
        # This entry bills ex-VAT; the box asked for a gross figure.
        fee /= 1.0 + card_vat_rate
    if isinstance(energy, DynamicRates):
        factor = entry.data.get(CONF_MANUAL_ENERGY_FACTOR)
        base = entry.data.get(CONF_MANUAL_ENERGY_BASE)
        if factor is None and base is None and fee_raw is None:
            return None
        return DynamicRates(
            factor=float(factor) if factor is not None else energy.factor,
            base=float(base) if base is not None else energy.base,
            yearly_fixed_fee=fee,
            quarter_hourly=energy.quarter_hourly,
        )
    if isinstance(energy, SpotMonthlyRates):
        # Same two boxes as the dynamic leg: this kind is a coefficient pair
        # too, just resolved against the delivery month's mean instead of the
        # slot price. Without this branch a spot-monthly customer who signed
        # at a negotiated coefficient had nowhere to put it.
        factor = entry.data.get(CONF_MANUAL_ENERGY_FACTOR)
        base = entry.data.get(CONF_MANUAL_ENERGY_BASE)
        if factor is None and base is None and fee_raw is None:
            return None
        # Everything the step cannot collect is carried through, the same rule
        # the fixed branch below follows: a typed box replaces its own field
        # and nothing else. Rebuilding from the two typed values alone dropped
        # the rest, and this leg is rarely the bare pair the two shipped
        # spot-monthly cards produce - _cohort_energy_from_archived converts a
        # month-indexed variable or TOU card into it and fills in the bands,
        # the night circuit, the TOU schedule and the cohort's ceiling. On a
        # Mega Flex bi-hourly cohort, typing only a yearly fee billed peak
        # hours at the mono coefficient (1,1095 against 1,3275, 16% low),
        # off-peak 18% high, and silently discarded the Mega Cap ceiling.
        overrides: dict[str, Any] = {
            "factor": float(factor) if factor is not None else energy.factor,
            "base": float(base) if base is not None else energy.base,
            "yearly_fixed_fee": fee,
        }
        if (
            factor is not None or base is not None
        ) and energy.factor_transition is None:
            # A typed coefficient has to reach the meter the entry bills on.
            # The step offers this kind ONE factor and ONE base, so the pairs
            # the card printed per meter would shadow them on a bi-hourly or
            # night-circuit entry and the typed value would price nothing at
            # all: the same shadowing the fixed branch below clears when a
            # general fee is typed over a night-circuit one. Measured on
            # Energy Knights Essentia, a typed 0,95 moved the mono leg by
            # 0,0202 EUR/kWh and the other three meters by zero.
            #
            # A TOU-scheduled leg is left alone. One number cannot express
            # three bands, and dropping them would not fall back to the mono
            # pair, it would re-band the contract onto the plain bi-hourly
            # clock instead of the schedule it is sold on.
            overrides.update(
                factor_peak=None,
                base_peak=None,
                factor_offpeak=None,
                base_offpeak=None,
                factor_exclusive_night=None,
                base_exclusive_night=None,
            )
        return replace(energy, **overrides)
    if isinstance(energy, FixedRates):
        single = entry.data.get(CONF_MANUAL_ENERGY_SINGLE)
        peak = entry.data.get(CONF_MANUAL_ENERGY_PEAK)
        offpeak = entry.data.get(CONF_MANUAL_ENERGY_OFFPEAK)
        night = entry.data.get(CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT)
        if (
            single is None
            and peak is None
            and offpeak is None
            and night is None
            and fee_raw is None
        ):
            return None
        return FixedRates(
            single=float(single) if single is not None else energy.single,
            peak=float(peak) if peak is not None else energy.peak,
            offpeak=float(offpeak) if offpeak is not None else energy.offpeak,
            exclusive_night=(
                float(night) if night is not None else energy.exclusive_night
            ),
            yearly_fixed_fee=fee,
            # A card that prints a separate night-circuit standing charge
            # bills it instead of the standard one (yearly_fixed_fee_for_meter),
            # which would swallow a typed fee on an exclusive-night entry. A
            # signed fee is the whole standing charge, whatever the circuit.
            yearly_fixed_fee_exclusive_night=(
                None if fee_raw is not None else energy.yearly_fixed_fee_exclusive_night
            ),
        )
    return None


def _cohort_energy_from_archived(
    archived: "SupplierSnapshot",
) -> EnergyRates | None:
    """The energy leg a signing cohort bills at, from its archived card.

    Fixed / dynamic / spot-monthly: the archived leg is exactly the locked
    rate or coefficient pair. Variable: re-price the cohort's numeric formula
    coefficients against the CURRENT month's mean (a SpotMonthlyRates leg)
    rather than freeze the archived card's stale resolved rate, which would
    pin the signing-month index.

    A spot-monthly leg is returned whole, ``index_realised`` included, and
    that value is the SIGNING month's. It never reaches a bill:
    :func:`_effective_snapshot_for_month` replaces it with the delivery
    month's before pricing, which is where that split belongs, and every
    other consumer ignores the field. Said out loud because the paragraph
    above reads as though the leg carried no index at all.
    ``None`` when the archived card exposes no re-priceable rate (a variable
    card whose coefficients couldn't be parsed, or a TOU / Impact card that
    prints resolved bands without a monthly formula behind them).
    """
    energy = archived.energy
    if isinstance(energy, ImpactRates) and energy.month_indexed:
        # A Tarif Impact card indexed per band monthly (Cociter trihoraire)
        # becomes the three-band monthly leg on the CWaPE schedule, carrying
        # each band's cap, so the same month-mean gates price it.
        return SpotMonthlyRates(
            factor=energy.pic_factor or 0.0,
            base=energy.pic_base or 0.0,
            factor_pic=energy.pic_factor,
            base_pic=energy.pic_base,
            factor_medium=energy.medium_factor,
            base_medium=energy.medium_base,
            factor_eco=energy.eco_factor,
            base_eco=energy.eco_base,
            ceiling_pic=energy.ceiling_pic,
            ceiling_medium=energy.ceiling_medium,
            ceiling_eco=energy.ceiling_eco,
            rlp_indexed=energy.rlp_indexed,
            rlp_blend=energy.rlp_blend,
            yearly_fixed_fee=energy.yearly_fixed_fee,
        )
    if isinstance(energy, TimeOfUseRates) and energy.month_indexed:
        # A TOU card that indexes each band monthly becomes the three-band
        # monthly leg, so every existing month-mean gate keeps working.
        return SpotMonthlyRates(
            factor=energy.formula_factor_peak or 0.0,
            base=energy.formula_base_peak or 0.0,
            factor_peak=energy.formula_factor_peak,
            base_peak=energy.formula_base_peak,
            factor_transition=energy.formula_factor_transition,
            base_transition=energy.formula_base_transition,
            factor_offpeak=energy.formula_factor_offpeak,
            base_offpeak=energy.formula_base_offpeak,
            factor_sunday=energy.formula_factor_sunday,
            base_sunday=energy.formula_base_sunday,
            weekend_rule=energy.weekend_rule,
            yearly_fixed_fee=energy.yearly_fixed_fee,
        )
    if isinstance(energy, (FixedRates, DynamicRates, SpotMonthlyRates)):
        # SpotMonthlyRates is already the right shape to carry forward: the
        # archived card's coefficients are exactly what the cohort signed, and
        # the leg re-resolves against the CURRENT month's mean every tick, so
        # nothing pins the signing-month index. Reachable since Energy Knights
        # Essentia, the first spot-monthly contract with a wired-up archive;
        # before that no supplier of this kind kept one.
        return energy
    if isinstance(energy, VariableRates) and energy.formula_factor is not None:
        return SpotMonthlyRates(
            factor=energy.formula_factor,
            base=energy.formula_base if energy.formula_base is not None else 0.0,
            # And the bands' own coefficients when the card printed them per
            # meter, or a bi-hourly cohort is billed the mono formula around
            # the clock: Mega's peak factor is a fifth above its mono one.
            factor_peak=energy.formula_factor_peak,
            base_peak=energy.formula_base_peak,
            factor_offpeak=energy.formula_factor_offpeak,
            base_offpeak=energy.formula_base_offpeak,
            factor_exclusive_night=energy.formula_factor_exclusive_night,
            base_exclusive_night=energy.formula_base_exclusive_night,
            # From the SIGNING-month card, which is the one that priced this
            # cohort's guarantee.
            ceiling_single=energy.ceiling_single,
            ceiling_peak=energy.ceiling_peak,
            ceiling_offpeak=energy.ceiling_offpeak,
            ceiling_exclusive_night=energy.ceiling_exclusive_night,
            # Which mean the coefficients resolve against travels with them;
            # the realised index does not, it belongs to a delivery month and
            # is spliced on per month by _effective_snapshot_for_month.
            rlp_indexed=energy.rlp_indexed,
            rlp_blend=energy.rlp_blend,
            yearly_fixed_fee=energy.yearly_fixed_fee,
            # Carry the dedicated exclusive-night standing fee so an
            # exclusive-night meter keeps its own fee instead of falling back to
            # the standard abonnement (yearly_fixed_fee_for_meter reads it).
            yearly_fixed_fee_exclusive_night=energy.yearly_fixed_fee_exclusive_night,
        )
    return None


def _cohort_injection_from_archived(
    archived: "SupplierSnapshot", delivery: "SupplierSnapshot"
) -> InjectionRates | None:
    """The feed-in leg a signing cohort bills at, or ``None``.

    A contract that locks its offtake formula for the term locks the feed-in
    formula with it: the customer on issue #85 confirmed both from his own
    bill, twelve months on the June coefficients while the integration
    credited him at September's. The energy leg has always been re-priced
    this way and the feed-in leg never was, so the two halves of one contract
    were read off two different cards.

    Only the COEFFICIENTS move, on the same principle as
    ``_cohort_energy_from_archived``: ``factor`` and ``base`` are the
    contract, while ``current`` is the illustration the supplier printed for
    that month's new customers and is stale the moment the month turns. They
    are laid onto ``delivery``, the card of the month being billed: today's
    on the live tick, the month's own on a year-to-date walk. So a keyless
    entry, which can only credit the printed figure, credits each month the
    figure that month's card printed, and a month keeps its own settled index.

    The coefficients are the single pair and, on a card that prints one
    formula per band (Engie Empower Flextime), the six slot coefficients: the
    card fixes those per signing month just like the energy formulas beside
    them, and freezing the pair alone billed an August signer on August's
    energy and September's feed-in.

    The formula also brings what it is a formula OF: the index it reads and
    any floor it promises. Trevion LifePowr moved its feed-in from the
    quarter-hour Belpex to the month's Belpex_SPP in June 2026, and copying an
    April signer's coefficients onto September's leg credited them on an index
    neither card names. A month's settled index is then kept only for a
    formula that reads a month.

    ``None`` when the archived leg carries no coefficients: a card that
    publishes only a printed monthly figure re-prices every month by its own
    terms, and freezing it would invent a lock the contract does not have.
    Unless the card says the figure IS the contract for the term
    (``fixed_for_term``: Mega's fixed range, Trevion Groene Energie Vast,
    EnergyVision's fixed-injection card): then the signing card's leg stands
    whole, printed figure included. A January Mega Online Fixed signer was
    otherwise credited September's 3,56 c/kWh where the contract pays 0,98.
    """
    old = archived.injection
    leg = delivery.injection
    if old is None or leg is None:
        return None
    if old.fixed_for_term:
        return None if old == leg else old
    slots = _slot_coefficients(old)
    if slots is None and old.factor is None and old.base is None:
        return None
    (f_peak, b_peak), (f_trans, b_trans), (f_off, b_off) = slots or (
        (None, None),
        (None, None),
        (None, None),
    )
    frozen = replace(
        leg,
        factor=old.factor,
        base=old.base,
        factor_peak=f_peak,
        base_peak=b_peak,
        factor_transition=f_trans,
        base_transition=b_trans,
        factor_offpeak=f_off,
        base_offpeak=b_off,
        formula=old.formula,
        spp_indexed=old.spp_indexed,
        month_indexed=old.month_indexed,
        slot_indexed=old.slot_indexed,
        floor_at_zero=old.floor_at_zero,
        minimum=old.minimum,
        index_realised=(
            leg.index_realised if old.spp_indexed or old.month_indexed else None
        ),
    )
    return None if frozen == leg else frozen


def _month_indexed_leg(
    snapshot: "SupplierSnapshot", entry: ConfigEntry
) -> EnergyRates | None:
    """The monthly-mean leg for a card whose rate IS the delivery month's index.

    Such a card prints a rate computed from the PREVIOUS month's index and says
    so. Cociter Tarif Variable is the shape: the footnote reads "le prix
    indique est calcule avec l'indice BELIX du mois precedent ... renseigne a
    titre indicatif", while note (7) indexes the contract on "la moyenne
    arithmetique des cotations journalieres Day Ahead EPEX SPOT Belgium durant
    le mois de fourniture" and settles the volume retroactively. Billing the
    printed indicative therefore bills last month's index: measured on the 2026
    cards, 8,1% under in May and 15,4% over in February on the energy leg.

    BELIX is exactly the arithmetic monthly mean the coordinator already
    computes, so the coefficients resolve against it without approximation.
    The trihoraire card is the same contract on the three CWaPE bands, one
    formula each, and its September 2026 card printed August's index too, so
    it takes the same leg with the bands in place of the mono pair.

    Returns ``None`` without an ENTSO-E key, which keeps the printed
    indicative: the key is offered as optional to every contract flagged
    ``month_indexed_energy`` in the registry, and an entry that skipped it is
    better served by a rate a month stale than by no energy leg at all. Same
    reasoning, and the same guard, as the variable cohort below.
    """
    energy = snapshot.energy
    if not isinstance(energy, (VariableRates, TimeOfUseRates, ImpactRates)):
        return None
    if not energy.month_indexed:
        return None
    if not entry.data.get(CONF_API_KEY):
        return None
    return _cohort_energy_from_archived(snapshot)


def _cohort_card(
    start: date,
    month_now: date,
    archived: "SupplierSnapshot | None",
    current: "SupplierSnapshot",
) -> str:
    """Which card a contract that names a cohort month ends up billing on.

    The price a cohort entry publishes says nothing about where it came
    from: ``snapshot_publication`` names the card that was fetched today
    whether or not the signing month's card was retrieved and spliced in, so
    "did my start date do anything?" could only be answered from a
    diagnostics dump. Issue #96 is that question asked from the outside, on
    a supplier that happened to print the same formula four months running.

    Three answers, one string. The archived card by name when it was
    retrieved; the current card by name for a contract signed this month,
    where the two are the same card; and the current card WITH the month
    that could not be retrieved for a past signing the archive has nothing
    for, which is the case the entry otherwise hides.
    """
    if archived is not None:
        return archived.publication_label or f"{start:%Y-%m}"
    label = current.publication_label or "the current card"
    if start >= month_now:
        return label
    return f"{label} (no archived card for {start:%Y-%m})"
