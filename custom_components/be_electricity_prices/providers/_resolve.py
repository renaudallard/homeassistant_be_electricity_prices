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

"""Turning a parsed card into the one this household is billed on.

A card is published for a market, not for a customer: it prints figures
excluding or including VAT, quotes an excise band the household may not be
in, offers a direct-debit discount it may not have taken, and names a volume
tier or a settlement grid that depends on the meter. These functions apply
each of those, once, so the five paths that rebuild a bill cannot disagree
about what the card meant.
"""

from __future__ import annotations

from .base import DsoOverlay, SupplierSnapshot

from ..const import (
    DSO_SIBELGA,
    FEDERAL_CONTRIBUTION_ZEROED_FROM,
    FEDERAL_EXCISE_KNOWN_FROM,
    FEDERAL_EXCISE_KNOWN_UNTIL,
    FEDERAL_EXCISE_RESIDENTIAL_TVAC,
    FLUVIUS_KEYS,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    VAT_RATE_REDUCED,
    VREG_NETWORK_CEILING_HTVA,
    VREG_NETWORK_CEILING_KNOWN_FROM,
    VREG_NETWORK_CEILING_KNOWN_UNTIL,
)
from ._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
    VariableRates,
)
from dataclasses import replace
from datetime import date
from typing import Any


def _vat_energy(energy: EnergyRates, factor: float) -> EnergyRates:
    # Only these three carry a separate exclusive-night abonnement; the rest
    # bill the standard fee on every meter type.
    if isinstance(energy, (FixedRates, VariableRates, SpotMonthlyRates)):
        excl_night = energy.yearly_fixed_fee_exclusive_night
        return replace(
            energy,
            yearly_fixed_fee=energy.yearly_fixed_fee * factor,
            yearly_fixed_fee_exclusive_night=(
                None if excl_night is None else excl_night * factor
            ),
        )
    return replace(energy, yearly_fixed_fee=energy.yearly_fixed_fee * factor)


def _vat_dso(dso: DsoOverlay, factor: float) -> DsoOverlay:
    """Gross the overlay's EUR/year fees, and only those.

    The per-kWh rates stay as the card printed them: the pricing engine
    grosses them per component from ``vat_rate``. ``network_ceiling_eur_per_kwh``
    is one of those, and grossing it here put it on a different basis from the
    ``distribution_single + transport`` it is measured against, which
    overstated the VREG headroom by 21% of that term. It is grossed where the
    comparison happens instead.
    """
    return replace(
        dso,
        data_management_per_year=dso.data_management_per_year * factor,
        capacity_eur_per_kw_year=(
            None
            if dso.capacity_eur_per_kw_year is None
            else dso.capacity_eur_per_kw_year * factor
        ),
        prosumer_eur_per_kva_year=(
            None
            if dso.prosumer_eur_per_kva_year is None
            else dso.prosumer_eur_per_kva_year * factor
        ),
        brussels_osp_by_tier=(
            None
            if dso.brussels_osp_by_tier is None
            else {k: v * factor for k, v in dso.brussels_osp_by_tier.items()}
        ),
        brussels_power_term_above_13kva=(
            None
            if dso.brussels_power_term_above_13kva is None
            else dso.brussels_power_term_above_13kva * factor
        ),
    )


def _vat_injection(injection: InjectionRates, factor: float) -> InjectionRates:
    def scaled(value: float | None) -> float | None:
        return None if value is None else value * factor

    return replace(
        injection,
        current=scaled(injection.current),
        factor=scaled(injection.factor),
        base=scaled(injection.base),
        peak=scaled(injection.peak),
        transition=scaled(injection.transition),
        offpeak=scaled(injection.offpeak),
        factor_peak=scaled(injection.factor_peak),
        base_peak=scaled(injection.base_peak),
        factor_transition=scaled(injection.factor_transition),
        base_transition=scaled(injection.base_transition),
        factor_offpeak=scaled(injection.factor_offpeak),
        base_offpeak=scaled(injection.base_offpeak),
        # The guaranteed floor is a rate like the rest; left as printed it
        # would sit ex-VAT beside grossed coefficients.
        minimum=scaled(injection.minimum),
    )


def apply_vat(snapshot: SupplierSnapshot, *, include_vat: bool) -> SupplierSnapshot:
    """Resolve a snapshot against the entry's VAT preference.

    ``TaxOverlay.vat_rate == 0.0`` means the card printed VAT-inclusive
    numbers - the convention every residential card follows - so the
    snapshot is returned unchanged (identity, not a copy).

    A non-zero rate means the card printed everything excluding VAT, as
    professional cards do. Two kinds of value then need different handling
    and only one of them is covered by the pricing engine:

      - Per-kWh rates are grossed up per component in
        ``pricing._finalize_breakdown`` from ``vat_rate``, so they stay as
        printed here and only the rate itself is resolved.
      - Fixed and annual fees - the yearly fee, data management, capacity,
        the DSO and supplier prosumer forfaits and the Brussels OSP table -
        never reach that path: the live, year-to-date, backfill and compare
        paths each sum them raw. They are baked here so the choice lands
        exactly once whichever path bills them.

    ``include_vat=False`` serves a business that deducts VAT: the factor
    is 1.0 and the numbers stay as the card printed them.

    Two values are exempt outright and stay as parsed whatever the card's
    basis. Injection is baked only when the card taxes it
    (``InjectionRates.vat_applies``); residential injection is VAT-exempt.
    The Flemish energy fund is never baked at all: the cards say so in as
    many words, Engie footnoting its ``Cotisation Fonds Energie Region
    Flamande`` with "Vous ne payez pas de TVA sur ces couts" and DATS 24 its
    ``Bijdrage Energiefonds Vlaams Gewest`` with "Niet aan btw onderworpen".
    Grossing it charged a professional Flanders entry 12,18 EUR/month
    against an invoiced 10,07, about 25 EUR/yr.

    Call this per config entry, never before the shared snapshot cache:
    the cache is keyed on (supplier, contract, region) and shared between
    entries that may answer this question differently.
    """
    rate = snapshot.taxes.vat_rate
    if rate == 0.0:
        return snapshot
    factor = 1.0 + rate if include_vat else 1.0
    injection = snapshot.injection
    return replace(
        snapshot,
        energy=_vat_energy(snapshot.energy, factor),
        dsos={k: _vat_dso(v, factor) for k, v in snapshot.dsos.items()},
        injection=(
            _vat_injection(injection, factor)
            if injection is not None and injection.vat_applies
            else injection
        ),
        # energy_fund_eur_per_month is deliberately absent: the levy is
        # VAT-free, so it is billed exactly as the card prints it.
        taxes=replace(
            snapshot.taxes,
            vat_rate=rate if include_vat else 0.0,
            published_vat_rate=rate,
        ),
        supplier_prosumer_eur_per_kva_year=(
            None
            if snapshot.supplier_prosumer_eur_per_kva_year is None
            else snapshot.supplier_prosumer_eur_per_kva_year * factor
        ),
        # A credit against energy, the standing charge and the green
        # contribution, which are all billed with VAT, so it moves onto the
        # entry's basis with them rather than staying as the card printed it.
        #
        # ALL FOUR halves of it, because they are added together and then
        # compared against the ceiling: grossing the flat one alone left a
        # professional entry crediting a per-kWh term and a ceiling still as
        # the card printed them, 38 EUR short at 3500 kWh and 168 at 20.000
        # where the ceiling binds. The supplement travels with the base it is
        # added to, and resolve_direct_debit runs after this.
        welcome_credit_eur=(
            None
            if snapshot.welcome_credit_eur is None
            else snapshot.welcome_credit_eur * factor
        ),
        welcome_credit_eur_per_kwh=(
            None
            if snapshot.welcome_credit_eur_per_kwh is None
            else snapshot.welcome_credit_eur_per_kwh * factor
        ),
        welcome_credit_cap_eur=(
            None
            if snapshot.welcome_credit_cap_eur is None
            else snapshot.welcome_credit_cap_eur * factor
        ),
        welcome_credit_direct_debit_eur=(
            None
            if snapshot.welcome_credit_direct_debit_eur is None
            else snapshot.welcome_credit_direct_debit_eur * factor
        ),
    )


def blended_excise_rate(
    bands: tuple[tuple[float, float], ...], annual_kwh: float
) -> float:
    """Average EUR/kWh of a degressive excise schedule over ``annual_kwh``.

    The cards say what the schedule is: "un tarif degressif PAR TRANCHE de
    consommation, calcule sur une base annuelle". Each slice of the year's
    volume is billed at its own band's rate, so a site drawing 30 000 kWh
    pays the first 20 000 at the first band and only the remaining 10 000 at
    the second. Billing the whole volume at the band the total lands in is a
    different, always cheaper number, because the schedule decreases.

    The engine prices per hour and cannot know where in the year's cumulative
    volume an hour sits, but it does not have to: the charge is defined on an
    annual basis, so the honest per-kWh figure is the year's total divided by
    the year's volume. That is what this returns, and it makes the annual
    bill exact whenever the volume estimate is.

    A volume past the last band is billed at the last band's rate for the
    remainder: the schedule stops at the ceiling the card covers (1.000.000
    kWh/year on the current professional cards), and above that the
    connection is out of what these cards price at all, so extending the last
    rate keeps a plausible number rather than inventing one.
    """
    if annual_kwh <= 0.0:
        return bands[0][1]
    total = 0.0
    floor = 0.0
    for upper, rate in bands:
        slice_kwh = min(annual_kwh, upper) - floor
        if slice_kwh > 0.0:
            total += slice_kwh * rate
        floor = upper
        if annual_kwh <= upper:
            break
    if annual_kwh > floor:
        total += (annual_kwh - floor) * bands[-1][1]
    return total / annual_kwh


def resolve_excise_band(
    snapshot: SupplierSnapshot, annual_kwh: float
) -> SupplierSnapshot:
    """Resolve a degressive excise schedule to one rate, or leave the card alone.

    A card without ``federal_excise_bands`` prints one rate and is returned
    unchanged (identity), which is every residential card from August 2026 on,
    when the scheme flattened and the tranche table came off them. Before
    that the Engie and Mega residential cards printed the same four tranches
    the professional ones do, and they reach the blend below like any other
    schedule: 203 archived Engie rows and 151 Mega ones carry bands.

    A card that prints a schedule bills it per tranche, so the rate the engine
    reads is the blend over the entry's estimated annual volume rather than
    the band that volume lands in; see :func:`blended_excise_rate`. Resolving
    it once here keeps the pricing engine reading a single ``federal_excise``
    and knowing nothing about bands.
    """
    bands = snapshot.taxes.federal_excise_bands
    if not bands:
        return snapshot
    rate = blended_excise_rate(bands, annual_kwh)
    if rate == snapshot.taxes.federal_excise:
        return snapshot
    return replace(snapshot, taxes=replace(snapshot.taxes, federal_excise=rate))


def settled_injection(inj: InjectionRates, index: float) -> InjectionRates:
    """A feed-in leg recomputed at the index its month actually settled at.

    ``current`` is rebuilt from the card's own coefficients, so the printed
    estimate gives way to the arithmetic the supplier invoices, and
    ``index_realised`` carries the figure so the engine bills it rather than
    the weighted mean it would otherwise compute. A leg with no coefficients
    keeps its printed figure and records the index alone.

    Worth settling because that mean is not the same number for these two
    suppliers. It weights each hour's MEAN price by the hour's solar share,
    while EBEM's SPP0 and Trevion's Belpex_SPP_BE weight each QUARTER by its
    own, and over January to August 2026 the computed one ran about
    0,9 EUR/MWh above both published series in every month. It is not a bug in
    the mean: Energy Knights defines its Belpex-SPP-M on the hourly quotation
    and the same computation reproduces its published series to 0,007%, so the
    resolution is a property of the card. See ``spot_stats._spp_month_mean``.

    Shared by every provider whose card publishes the settled value: EBEM
    names it as "vorige maand", Trevion names the month outright.
    """
    if inj.factor is None or inj.base is None:
        return replace(inj, index_realised=index)
    return replace(inj, current=inj.factor * index + inj.base, index_realised=index)


def resolve_federal_contribution(
    snapshot: SupplierSnapshot, delivery_month: date, *, professional: bool
) -> SupplierSnapshot:
    """Drop the federal energy contribution from a month it is not levied in.

    The Belgian "bijdrage op de energie" was abolished as a line of its own on
    2026-08-01 and folded into the special excise, which was flattened in the
    same measure; Eneco's card states it outright. It is a federal levy on
    consumption rather than a contract term, so the DELIVERY month decides
    whether it is owed, not the month the card was written in: a July bill
    still owes it, a September one does not, whatever the card prints.

    Three residential card families went on printing it into September 2026
    (Cociter, Ecofix, TotalEnergies), two of them a stale block and one a
    figure its publisher has not withdrawn. A card printing a levy nobody owes
    billed it, about 7 EUR a year at 3.500 kWh. Resolving it here rather than
    in the parsers keeps the archive holding what each card actually printed,
    needs no schema bump, and reaches every path that rebuilds a bill, since
    they all price a resolved card.

    Professional cards are out of scope and keep whatever they print. Bolt,
    Engie and Mega have all gone on printing 0,0019261 ex-VAT on theirs
    through the change and every month since, which is the professional scheme
    keeping the levy rather than three suppliers being stale in lockstep.

    Open-ended, unlike the two levy corrections beside it, and deliberately so.
    Those encode a rate that is IN FORCE and expire into reading the card,
    because a rate goes out of date. This encodes an ABOLITION, which does
    not: the line was struck from the law and nothing schedules its return.
    Giving it an end date would be the harmful choice, because three
    residential card families still print the abolished line and would be
    billed it again the month the window closed, about 7 EUR a year each.

    The risk that comes with that is real and is accepted: if the levy were
    ever reinstated, this would go on striking it out, and nothing would say
    so. A rate of zero trips no bound, and the consensus check compares cards
    with each other rather than with the law, so a fleet that all printed the
    reinstated figure would agree with itself and still be zeroed here. The
    signal to watch for is the live check's excise window request, which comes
    up eight weeks before the excise this was folded into next steps; a
    reinstated contribution would arrive in the same measure.
    """
    taxes = snapshot.taxes
    if professional or not taxes.energy_contribution:
        return snapshot
    if (delivery_month.year, delivery_month.month) < FEDERAL_CONTRIBUTION_ZEROED_FROM:
        return snapshot
    return replace(snapshot, taxes=replace(taxes, energy_contribution=0.0))


def resolve_federal_excise(
    snapshot: SupplierSnapshot, delivery_month: date, *, professional: bool
) -> SupplierSnapshot:
    """Bill the special excise the law sets, not a stale card's copy of it.

    The excise is a federal levy on consumption: one residential rate for the
    whole country in any given month, so two cards disagreeing about it is one
    of them being out of date rather than a difference between suppliers. On
    the September 2026 cards thirteen suppliers print 4,876 c/kWh, TotalEnergies
    prints it rounded, and Ecofix prints July's 5,03288 because its card is a
    picture of July's card, which no parser change can read differently.

    Applied only inside the window the constants name, on the card's own VAT
    basis: most print the levy including VAT, Ecopower prints it excluding and
    the engine grosses it later, so writing one number into both would be
    wrong by 6% for one of them. A card that already prints the rate is
    returned unchanged, so this is identity for almost every entry.

    Left alone: a professional card, whose scheme bands the levy by annual
    volume and is a different rate entirely, and any card carrying
    ``federal_excise_bands``.

    Those two were the same card when this was written and are not any more.
    Mega's and Engie's RESIDENTIAL cards print a four-tier table too, for
    January to July 2026, and the bands guard now catches them. It is still
    the right answer and for a different reason: those months are outside the
    window below, where the rate above is the FLAT one the August measure
    set, so a banded card is a month this constant does not describe rather
    than a scheme it does not apply to. Measured over the 317 residential
    cards the archive holds for August to December 2026, none carries a band
    table, because the measure that flattened the levy took it off the card.
    """
    taxes = snapshot.taxes
    if professional or taxes.federal_excise_bands:
        return snapshot
    month = (delivery_month.year, delivery_month.month)
    if not FEDERAL_EXCISE_KNOWN_FROM <= month < FEDERAL_EXCISE_KNOWN_UNTIL:
        return snapshot
    rate = FEDERAL_EXCISE_RESIDENTIAL_TVAC / (1.0 + taxes.vat_rate)
    if abs(rate - taxes.federal_excise) < 5e-7:
        return snapshot
    return replace(snapshot, taxes=replace(taxes, federal_excise=rate))


def resolve_direct_debit(
    snapshot: SupplierSnapshot, *, direct_debit: bool
) -> SupplierSnapshot:
    """Take the card's direct-debit reduction off the standing charge, or not.

    Identity on a card that offers none, which is every card but Brusol's
    Groene stroom today, so this is free for every existing entry.

    Applied here rather than at the six places that read the standing charge
    (the live tick, the year-to-date, the backfill accrual, the config-flow
    estimate and both comparison quotes), for the reason
    :func:`resolve_excise_band` and :func:`resolve_volume_tier` are: a
    transform that has to reach every cost path is baked once into the
    snapshot the entry reads, and those paths keep reading one fee and
    knowing nothing about how it is paid.

    The reduction is CLEARED either way, the way the volume tranche is: it is
    a fact about the card that has now been answered for this entry, and a
    snapshot still carrying it would look unresolved to the next reader.

    The floor is zero. A reduction larger than the charge it comes off would
    otherwise pay the household to be supplied, which no card offers and
    which would flow straight into the yearly-cost sensors.
    """
    discount = snapshot.direct_debit_discount_eur
    supplement = snapshot.welcome_credit_direct_debit_eur
    conditional = snapshot.welcome_credit_requires_direct_debit
    if discount is None and supplement is None and not conditional:
        return snapshot
    if conditional and not direct_debit:
        # The whole offer was conditional, so nothing survives: clearing only
        # the supplement would leave the base and the per-kWh leg crediting a
        # household the card grants nothing, worth 522,58 EUR on Cosy Flex at
        # 3500 kWh. The cap goes with them so no later reader sees a ceiling
        # over an absent credit.
        #
        # All five amounts, the way resolve_welcome_credit_meter clears all
        # five: this listed three and left the share and the volume behind,
        # which a card both direct-debit-conditional and percentage-stated
        # would have credited to a household its own terms grant nothing. No
        # card is both shapes today, and the same gap existed for the per-kWh
        # leg until a card turned up that was.
        return replace(
            snapshot,
            direct_debit_discount_eur=None,
            welcome_credit_direct_debit_eur=None,
            welcome_credit_requires_direct_debit=False,
            welcome_credit_eur=None,
            welcome_credit_eur_per_kwh=None,
            welcome_credit_cap_eur=None,
            welcome_credit_pct_of_energy=None,
            welcome_credit_kwh=None,
        )
    # The welcome credit's own direct-debit part is settled here too: same
    # per-entry answer, same reason for baking it once, and Mega's card
    # states it the same way ("une reduction de base de 37.1 EUR + 5.3 EUR
    # supplementaires en cas de paiement par domiciliation bancaire").
    credit = snapshot.welcome_credit_eur or 0.0
    if direct_debit and supplement:
        credit += supplement
    energy = snapshot.energy
    if not direct_debit or discount is None:
        return replace(
            snapshot,
            direct_debit_discount_eur=None,
            welcome_credit_direct_debit_eur=None,
            welcome_credit_requires_direct_debit=False,
            welcome_credit_eur=credit or snapshot.welcome_credit_eur,
        )
    return replace(
        snapshot,
        direct_debit_discount_eur=None,
        welcome_credit_direct_debit_eur=None,
        welcome_credit_requires_direct_debit=False,
        welcome_credit_eur=credit or snapshot.welcome_credit_eur,
        energy=replace(
            energy,
            yearly_fixed_fee=max(0.0, energy.yearly_fixed_fee - discount),
        ),
    )


def resolve_welcome_credit_meter(
    snapshot: SupplierSnapshot, meter: str
) -> SupplierSnapshot:
    """Drop a welcome credit the card does not grant this meter.

    Luminus excludes an exclusive-night-only connection from its percentage
    campaign in as many words, and its published conditions repeat it. Which
    meter the entry has is a per-entry answer, so it is settled once here
    rather than at the places that spend the credit, for the reason
    :func:`resolve_direct_debit` is: a transform that has to reach every cost
    path is baked into the snapshot the entry reads.

    Identity on every card that states no such exclusion, and on every meter
    but the excluded one, so this is free for every existing entry. The flag
    is cleared either way, the way the direct-debit answer is, so no later
    reader can apply it twice.
    """
    if not snapshot.welcome_credit_excludes_night_meter:
        return snapshot
    if meter != METER_EXCLUSIVE_NIGHT:
        return replace(snapshot, welcome_credit_excludes_night_meter=False)
    # The whole credit, not just the percentage leg: the card's exclusion is
    # of the offer, and a card stating one has no other credit to keep.
    return replace(
        snapshot,
        welcome_credit_excludes_night_meter=False,
        welcome_credit_eur=None,
        welcome_credit_eur_per_kwh=None,
        welcome_credit_cap_eur=None,
        welcome_credit_pct_of_energy=None,
        welcome_credit_kwh=None,
    )


def without_welcome_credit(snapshot: SupplierSnapshot) -> SupplierSnapshot:
    """``snapshot`` with every shape of welcome credit taken off it.

    For a caller holding a card that is standing in for a month it is not the
    card of. Rates survive a stand-in and a credit does not: the credit is a
    property of the version signed, by its own terms, and Luminus's campaign
    is only ever printed on the live card of the month it ran in.

    All five amount fields, for the reason :func:`resolve_welcome_credit_meter`
    clears all five: a card's offer is one offer, and leaving one leg behind
    credits a household a fragment of something it was never granted.
    """
    if not (
        snapshot.welcome_credit_eur
        or snapshot.welcome_credit_eur_per_kwh
        or snapshot.welcome_credit_pct_of_energy
        or snapshot.welcome_credit_kwh
        or snapshot.welcome_credit_direct_debit_eur
    ):
        return snapshot
    return replace(
        snapshot,
        welcome_credit_eur=None,
        welcome_credit_eur_per_kwh=None,
        welcome_credit_cap_eur=None,
        welcome_credit_pct_of_energy=None,
        welcome_credit_kwh=None,
        welcome_credit_direct_debit_eur=None,
    )


def resolve_vreg_network_ceiling(
    snapshot: SupplierSnapshot, delivery_month: date
) -> SupplierSnapshot:
    """Bill the VREG's maximumtarief, not a stale card's copy of it.

    The cap is a rule about the Flemish DISTRIBUTION network: the capacity
    term plus the per-kWh network term together, excluding data management,
    may not exceed it times the volume. ``fees._capped_capacity_annual``
    applies it. One rate for the whole of Flanders, so a card stating another
    one is wrong rather than different, which is the same reasoning
    :func:`resolve_federal_excise` follows for the excise.

    Only the Fluvius overlays, because only Flanders has this instrument. A
    Wallonia or Brussels overlay is left alone even on a card that prints the
    footnote over all three regions, which Bolt's does.

    Put onto the CARD's own basis, not grossed: the ceiling is compared
    against ``distribution_single + transport`` as printed and the difference
    is grossed where that comparison happens (``_vat_dso`` says so), so a
    VAT-inclusive card takes the figure times 1,06 and an ex-VAT one takes it
    as the regulator publishes it. Same treatment, and for the same reason, as
    :func:`resolve_brussels_power_term`.

    Identity outside the window the constants name, and identity on a card
    already stating the figure, which four suppliers do (energie.be,
    EnergyVision, Frank and Luminus); it fills the figure for the ten that
    print none. Counted over the September 2026 Flanders cards. The two that
    print a DIFFERENT figure, Mega and Bolt, are a third case and are
    overwritten rather than filled.

    It moves a bill only where the cap binds, which is a low-volume connection
    on a high peak, and never at the 2,5 kW floor: see
    :func:`_capped_capacity_annual` for where that is and why the floor makes
    it so.
    """
    month = (delivery_month.year, delivery_month.month)
    if not (
        VREG_NETWORK_CEILING_KNOWN_FROM <= month < VREG_NETWORK_CEILING_KNOWN_UNTIL
    ):
        return snapshot
    ceiling = VREG_NETWORK_CEILING_HTVA
    if snapshot.taxes.vat_rate <= 0.0:
        # A VAT-inclusive card, so the regulator's ex-VAT figure is grossed.
        ceiling *= 1.0 + VAT_RATE_REDUCED
    changed = {
        key: replace(overlay, network_ceiling_eur_per_kwh=ceiling)
        for key, overlay in snapshot.dsos.items()
        if key in FLUVIUS_KEYS
        and (
            overlay.network_ceiling_eur_per_kwh is None
            or abs(overlay.network_ceiling_eur_per_kwh - ceiling) > 5e-7
        )
    }
    if not changed:
        return snapshot
    return replace(snapshot, dsos={**snapshot.dsos, **changed})


def _brussels_terms_on_card_basis(
    snapshot: SupplierSnapshot, terms: tuple[float, float]
) -> tuple[float, float]:
    """Brugel's ex-VAT pair put onto the basis this card prints on."""
    low, high = terms
    if snapshot.taxes.vat_rate <= 0.0:
        # A VAT-inclusive card, so the ex-VAT figures have to be grossed.
        low *= 1.0 + VAT_RATE_REDUCED
        high *= 1.0 + VAT_RATE_REDUCED
    return low, high


def omits_brussels_power_term(
    snapshot: SupplierSnapshot, *, terms: tuple[float, float] | None
) -> bool:
    """Whether this card prints the metering half of Sibelga's charge alone.

    TWO signals, and both are needed. The band above 13 kVA says the card
    already carries the power part, which is how the four suppliers printing
    the sum are recognised. The SIZE of the metering figure says the same thing
    for a card that prints one combined number and no band, which is the shape
    the band signal alone cannot see: 64,80 with no band is a complete charge,
    not a short one, and completing it would bill the power part twice.
    ``docs/providers/bolt.md`` records that both have to agree, and
    ``test_both_signals_have_to_agree_before_a_card_is_touched`` pins it.

    Shared with the Repairs card that discloses the gap, which asked the band
    alone and so told a correctly priced card it was 50 EUR a year short. One
    rule, one place, for the reason every other per-entry transform is baked
    once.

    ``terms`` is the figure to compare the metering half against. ``None``
    means nothing is known to compare it to, and the honest answer is then that
    we cannot tell, so this says no.
    """
    overlay = snapshot.dsos.get(DSO_SIBELGA)
    if overlay is None or overlay.brussels_power_term_above_13kva is not None:
        return False
    if terms is None:
        return False
    low, _high = _brussels_terms_on_card_basis(snapshot, terms)
    return overlay.data_management_per_year < low


def resolve_brussels_power_term(
    snapshot: SupplierSnapshot, *, terms: tuple[float, float] | None
) -> SupplierSnapshot:
    """Add Sibelga's "Puissance mise a disposition" to a card that omits it.

    Sibelga's fixed charge has two regulated parts, a metering one and a power
    one. Engie, Mega, TotalEnergies and EnergyVision print the sum and the
    band above 13 kVA beside it; Bolt's card prints the metering part alone,
    under a heading calling it the whole "Terme fixe GRD". The household pays
    the DSO either way, so a Brussels Bolt entry was about 50 EUR a year short
    with nothing on screen to say so.

    ``terms`` is the ``(at_or_below_13kva, above_13kva)`` pair Brugel
    publishes, EUR/year excluding VAT, from :mod:`..brugel`. ``None`` leaves
    the card exactly as it was, which is what happens before the sheet has
    been fetched and if it cannot be read at all.

    Applied only to a card that is missing the term, on two signals that have
    to agree: it prints no band above 13 kVA, which every card carrying the
    full charge does print, and its fixed term is smaller than the power part
    alone, so it cannot already contain it. A card that starts printing the
    sum therefore stops being adjusted without an edit here.

    The published figures are "prix hors TVA" and a residential card prints
    VAT-inclusive, so they are put onto the card's own basis before being
    added. That is the same 6% the card's own professional edition differs by.
    """
    if terms is None:
        return snapshot
    overlay = snapshot.dsos.get(DSO_SIBELGA)
    if not omits_brussels_power_term(snapshot, terms=terms):
        return snapshot
    assert overlay is not None
    low, high = _brussels_terms_on_card_basis(snapshot, terms)
    metering = overlay.data_management_per_year
    return replace(
        snapshot,
        dsos={
            **snapshot.dsos,
            DSO_SIBELGA: replace(
                overlay,
                data_management_per_year=metering + low,
                brussels_power_term_above_13kva=metering + high,
            ),
        },
    )


def resolve_volume_tier(
    snapshot: SupplierSnapshot, annual_kwh: float, meter: str = METER_MONO
) -> SupplierSnapshot:
    """Fold a volume-tiered energy leg into one formula, or leave it alone.

    A card without ``tier_kwh`` prices its whole volume one way and is returned
    unchanged (identity), which is every card but EnergyVision's tiered range.

    An exclusive-night entry gets the formula with the tranche REMOVED rather
    than folded in. The footnote that grants the tranche is also what withholds
    it from that register: *"is van toepassing op de eerste 1.800 kWh verbruik
    van je enkelvoudig tarief. Heb je een dag/nacht teller dan verdelen we de
    1.800 kWh als volgt, 900 kWh verbruik via je dag tarief en 900 kWh verbruik
    via je nacht tarief. Niet van toepassing op het exclusief nacht tarief."*
    The card prints no per-register formula, so a night circuit falls through
    to the mono pair, and folding the tranche into that pair credited it a
    discount it never receives: at the September card's rates and a 3.500 kWh
    entry that is 1,1 cent/kWh, roughly 9% under the billed rate.

    Such a card bills the year's first ``tier_kwh`` at ``tier_rate`` and the
    remainder on ``factor * mean + base``. A fixed tranche blended with a
    formula that is linear in the index is STILL a formula linear in the index:
    with ``w`` the tranche's share of the year,

        w * rate + (1 - w) * (factor * mean + base)
            == ((1 - w) * factor) * mean + ((1 - w) * base + w * rate)

    so the pair collapses into the coefficients the engine already prices and
    no new rate kind is needed. Resolving it once here keeps the pricing engine
    reading one formula and knowing nothing about tranches, exactly as
    :func:`resolve_excise_band` does for the degressive excise.

    The blend is the ANNUAL bill exactly, not an approximation of it, and the
    card is what makes that true: its Voordeelzekerheid clause settles the year
    so the full tranche is charged at the fixed rate whenever the volume
    allowed it ("Als je op je jaarlijkse afrekeningsfactuur geen 1.800 kWh aan
    het vast tarief kreeg aangerekend, terwijl je wel voldoende verbruik had
    doorheen het jaar, dan berekenen wij een Voordeelzekerheid"). So the year
    costs ``tier_kwh * rate + (rest) * formula`` however the pro-rata-per-day
    allowance fell across the months, and that is what this reproduces. It also
    makes the bi-hourly split (900 kWh on each register) free: the tranche and
    the remainder are billed at the same two rates in both bands, so splitting
    the allowance per register and blending once over the year reach the same
    annual total.

    What is NOT exact is ``annual_kwh`` itself, which is the household's own
    estimate. A wrong estimate moves the split proportionally, the same
    exposure the excise schedule already carries.

    A household inside the tranche has no variable leg at all, so it comes back
    as ``FixedRates``: leaving it here with a zeroed factor would price
    correctly but still demand a monthly mean, and an entry with no spot for
    the month would fail its tick over a coefficient that cannot matter.
    """
    energy = snapshot.energy
    if not isinstance(energy, SpotMonthlyRates):
        return snapshot
    if energy.tier_kwh is None or energy.tier_rate is None:
        return snapshot
    if meter == METER_EXCLUSIVE_NIGHT:
        # Cleared, not carried: every other consumer reads the coefficients
        # and nothing else, so a leg still holding the pair would look
        # unresolved to the next reader of it.
        return replace(snapshot, energy=replace(energy, tier_kwh=None, tier_rate=None))
    if annual_kwh <= 0.0:
        # Nothing to measure the tranche against. The config flow always
        # carries a positive estimate, so this is the degenerate guard rather
        # than a pricing decision.
        return snapshot
    within = min(energy.tier_kwh, annual_kwh)
    if within >= annual_kwh:
        return replace(
            snapshot,
            energy=FixedRates(
                single=energy.tier_rate,
                yearly_fixed_fee=energy.yearly_fixed_fee,
                yearly_fixed_fee_exclusive_night=(
                    energy.yearly_fixed_fee_exclusive_night
                ),
            ),
        )
    share = within / annual_kwh
    rest = 1.0 - share
    fixed_part = share * energy.tier_rate

    def blend(factor: float | None, base: float | None) -> tuple[float, float]:
        return rest * (factor or 0.0), rest * (base or 0.0) + fixed_part

    factor, base = blend(energy.factor, energy.base)
    changes: dict[str, Any] = {
        "factor": factor,
        "base": base,
        "tier_kwh": None,
        "tier_rate": None,
    }
    # Every band pair the leg happens to carry moves with the mono one. No
    # tiered card publishes a per-band formula today, and the blend is exact
    # only while the tranche is billed at one rate across the bands, which is
    # what the current range does; blending them is still nearer than leaving
    # a populated pair to price the remainder as though the tranche were not
    # there at all.
    for suffix in ("peak", "offpeak", "exclusive_night", "transition"):
        band_factor = getattr(energy, f"factor_{suffix}")
        band_base = getattr(energy, f"base_{suffix}")
        if band_factor is None and band_base is None:
            continue
        blended_factor, blended_base = blend(band_factor, band_base)
        changes[f"factor_{suffix}"] = blended_factor
        changes[f"base_{suffix}"] = blended_base
    return replace(snapshot, energy=replace(energy, **changes))


def resolve_settlement_grid(
    snapshot: SupplierSnapshot, *, quarter_hourly: bool
) -> SupplierSnapshot:
    """Move a dynamic card onto the 15-minute grid the entry settles on.

    Identity unless the household actually asked for it, which is every entry
    whose supplier does not offer the choice, so the cost is one boolean for
    all of them.

    Two legs can move, because the suppliers that offer the choice print two
    different defaults:

    * :class:`DynamicRates` on the hourly grid, which is Frank Energie. Its
      card prints one formula against ``BELPEX per uur``; ticking the box
      applies the same coefficients to the quarter-hourly index instead.
    * :class:`VariableRates` carrying the coefficients of its own indexation
      formula, which is Bolt. Its card prints a resolved monthly price AND the
      ``Belpex * factor + base`` formula behind it, and says the customer
      chooses whether that formula is settled per quarter-hour or against the
      RLP-weighted month. Ticking the box takes the second reading, so the leg
      becomes the dynamic one the card describes rather than the printed
      monthly figure.

    Anything else comes back untouched: a card that already prints a
    quarter-hourly index carries ``quarter_hourly`` from its own parser, a
    fixed leg has no formula to settle, and a variable card that exposes only
    a resolved rate has no coefficients to move onto the spot.

    Deliberately applied here rather than in the extractor. The grid is an
    account setting the supplier lets the customer flip (Frank monthly through
    its app, Bolt as a settlement option on the same contract), so the card
    cannot say which side a given household is on, and a second contract id
    per product would ask the user to re-pick their contract to change a
    billing preference. Resolving it beside the VAT treatment and the excise
    band keeps it on every path that produces a snapshot, the cached and
    archived ones included, so unticking the box takes effect without a
    refetch.

    Note this changes the leg's TYPE, which is why the contract's effective
    kind has to move with it: see ``offers_quarter_hourly`` and
    ``effective_kind``.
    """
    if not quarter_hourly:
        return snapshot
    energy = snapshot.energy
    if isinstance(energy, DynamicRates):
        if energy.quarter_hourly:
            return snapshot
        return replace(snapshot, energy=replace(energy, quarter_hourly=True))
    if isinstance(energy, VariableRates) and energy.formula_factor is not None:
        return replace(
            snapshot,
            energy=DynamicRates(
                factor=energy.formula_factor,
                base=energy.formula_base or 0.0,
                yearly_fixed_fee=energy.yearly_fixed_fee,
                quarter_hourly=True,
            ),
        )
    return snapshot
