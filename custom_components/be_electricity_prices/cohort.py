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

"""Signing-cohort resolution: what a contract signed in a past month bills at.

Split out of coordinator.py. A fixed or dynamic contract is billed at the rate
it locked in at signing, not today's card; a variable one re-prices its own
coefficients against the current month's index. Resolution order is a
hand-entered signing rate, then the archived signing-month card, then the
current card: per field, because only the user knows whether they signed at
the card rate or a negotiated one."""

from __future__ import annotations

import logging

from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import date
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any, NamedTuple
import aiohttp

from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
    CONF_SUPPLIER,
    CONF_TARIFF_CARD_DATE,
    SUPPLIER_CUSTOM,
)
from .providers import takes_signing_rate
from .providers.base import (
    SupplierExtractor,
    SupplierSnapshot,
)
from .providers._resolve import without_welcome_credit
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
from .snapshot_months import _month_card_retrievable, _snapshot_for_month
from .snapshot_resolve import _include_vat


_LOGGER = logging.getLogger(__name__)


def _parse_iso_date(value: Any) -> date | None:
    """Parse a stored ISO ``YYYY-MM-DD`` date string, or ``None``.

    Accepts the DateSelector return value used for the contract lifecycle
    fields; returns ``None`` for a missing / malformed value.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _tariff_card_month(entry: ConfigEntry) -> date | None:
    """First-of-month of the card this contract is billed on, or ``None``.

    A fixed or dynamic contract is locked to the card in force when it was
    SIGNED, which is not always the month supply began: a supplier switch
    takes about a month to go through, so a customer who signed in June is
    supplied from July on June's card, and energie.be goes as far as printing
    that month in the product name ("... 06/26"). Reading the start date as
    the card month therefore billed a switcher one card too late, which on
    Eneco's fixed card between July and August 2026 is 0,1681 against 0,2028
    EUR/kWh, 121 EUR/yr at 3500 kWh (issue #96).

    So the card month is its own optional field, and the start date is the
    fallback for every entry that does not set one, which is the behaviour
    they already had. The day within the month is irrelevant to which monthly
    card applies, so normalise to the first.
    """
    d = _parse_iso_date(entry.data.get(CONF_TARIFF_CARD_DATE)) or _parse_iso_date(
        entry.data.get(CONF_CONTRACT_START_DATE)
    )
    if d is None:
        return None
    return date(d.year, d.month, 1)


def ytd_window_start(entry: ConfigEntry, today: date) -> date:
    """First day ``current_year_cost`` accumulates from, for ``today``'s year.

    1 January unless the entry opted into billing from its contract start date
    (CONF_YTD_FROM_CONTRACT_START) and carries one, in which case the later of
    the two. Everything that walks the year-to-date reads this: the per-hour
    and per-day energy walks, the fee proration, the historical spot fetch,
    the statistics backfill, and the ``last_reset`` the sensor publishes.

    Clamped to 1 January of ``today``'s year rather than returning the raw
    start date. The sensor is a TOTAL whose ``last_reset`` the recorder uses
    to bucket one period per calendar year, and a window reaching back into a
    previous year does not survive that: the compiler would see a reset that
    never happened and add a year's reading on top. So a contract signed in an
    earlier year is billed from 1 January exactly as it is today, and the
    option only changes the contract's first calendar year.

    Its datetime form, which the sensor publishes as ``last_reset``, is
    ``coordinator_data.ytd_window_reset``.
    """
    jan1 = date(today.year, 1, 1)
    if not entry.data.get(CONF_YTD_FROM_CONTRACT_START):
        return jan1
    start = _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE))
    if start is None:
        return jan1
    return max(jan1, start)


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


class _CohortLegs(NamedTuple):
    """What a signing cohort bills at, both halves of it.

    ``None`` on either means "no override, keep the current card's leg".
    ``card`` names the card they were read off, for the sensor attribute;
    empty when the entry names no cohort month and nothing was resolved.
    """

    energy: EnergyRates | None
    injection: InjectionRates | None
    card: str = ""

    def splice(self, snapshot: "SupplierSnapshot") -> "SupplierSnapshot":
        """``snapshot`` billed on this cohort: each leg it overrides replaced,
        the rest of the card kept. The same object back when there is nothing
        to override, so a caller can tell the no-op by identity."""
        if self.energy is None and self.injection is None:
            return snapshot
        return replace(
            snapshot,
            energy=snapshot.energy if self.energy is None else self.energy,
            injection=snapshot.injection if self.injection is None else self.injection,
        )


async def _cohort_legs(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: "SupplierSnapshot",
    month_snapshot: "SupplierSnapshot | None" = None,
) -> _CohortLegs:
    """Resolve the legs a contract actually bills at.

    A fixed / dynamic contract signed months ago is billed at the rate it
    locked in at signing, not today's card, and its feed-in formula locks with
    it (issue #85). This returns both legs off the signing-month card, for the
    caller to splice onto the delivery month's DSO / tax overlays
    (:meth:`_CohortLegs.splice`), and names the card they came off. A leg left
    ``None`` means "no cohort override, keep the card's own": nothing to
    re-price with (no signing rate typed and no archived card, or a variable
    cohort with no ENTSO-E key to resolve its monthly mean). With no cohort
    month there is no lock, so the feed-in leg is ``None`` and the energy leg
    is the delivery month's card re-priced on its monthly mean when that card
    is month indexed and the entry has a key (:func:`_month_indexed_leg`),
    ``None`` otherwise.

    Resolution order is a hand-entered signing rate, then the archive, then
    the current card. What the user typed wins per field: only they know
    whether they signed at the card rate or at a promotional, brokered or
    negotiated one, and the form that collected the value promises to price
    the contract with it. The archived signing-month card fills in every field
    left blank when an archive holds it: the supplier's own, or the
    repository's, which from August 2026 also holds the cards of suppliers
    that keep none (TotalEnergies, Ecofix). Asking the supplier's alone left
    those cohorts billed on each month's card. The current card fills in
    otherwise.

    Both legs are ``None`` for a ``contract`` that isn't the entry's own (the
    OptionsFlow compare path walks an alternative contract with no signing
    history, so it must always price at the current card).
    """
    if contract != entry.data.get(CONF_CONTRACT):
        return _CohortLegs(None, None)
    if entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
        # The custom supplier's rate IS what the user typed on the
        # custom_energy step, so there is nothing for a signing rate to
        # improve on and the signing-rate step is never offered for it
        # (_needs_manual_rate). But an entry EDITED onto the custom supplier
        # keeps whatever manual rate and start date it carried from its
        # previous life, and nothing pops them: only async_step_signed_rate
        # does that, and it no longer runs. The overlay then quietly replaced
        # the typed formula with the old supplier's rate - measured at +0,09
        # EUR/kWh and +60 EUR of fee on one such entry, in whichever direction
        # the old supplier happened to charge.
        #
        # Guarding here rather than only popping the keys in the flow is what
        # heals the entries already holding them.
        return _CohortLegs(None, None)
    start = _tariff_card_month(entry)
    if start is None:
        # No signing month named, so there is no cohort lock and every month is
        # billed on its own card. ``month_snapshot`` is the card that was in
        # force for the delivery month; the live path leaves it unset, where
        # the current card IS that card.
        #
        # Taking the leg from the current card regardless billed a past month
        # on today's coefficients. They move every month on these products:
        # Engie's January card reads "2,4016 + (0,1200 x EPEXDAM)" against
        # September's "1,8432 + (0,1177 x EPEXDAM)", so a year-to-date walk
        # re-priced January on September's formula. Measured at about 12 EUR a
        # year on Direct Online and up to 29 on the professional Flow, and it
        # reached only entries that had typed an ENTSO-E key, which is to say
        # the option offered to make past months MORE accurate made them less.
        return _CohortLegs(
            _month_indexed_leg(month_snapshot or current_snapshot, entry), None
        )
    now = dt_util.now()
    this_month = date(now.year, now.month, 1)
    # Resolve the archived signing-month card first, as the base the typed
    # rate overlays onto. Fixed / dynamic re-price from its leg directly (the
    # locked value); variable re-prices from the cohort's parsed coefficients
    # against the current month's mean (see _cohort_energy_from_archived).
    # TOU / Impact are not re-priced yet. Signed this month (the step accepts
    # any date up to today) or dated in the future: the current card already
    # is the signing-month card, so there is nothing to retrieve. A typed
    # signing rate still applies, and used to sit unread until the month
    # rolled over and the price jumped under the user.
    archived: EnergyRates | None = None
    archived_snap: SupplierSnapshot | None = None
    if start < this_month and _month_card_retrievable(
        extractor, start, now.date(), entry
    ):
        snap_start = await _snapshot_for_month(
            hass,
            session,
            extractor,
            contract,
            region,
            start,
            current_snapshot,
            entry,
        )
        # _snapshot_for_month returns the SAME current_snapshot object when the
        # signing month has no archive; identity means "no archived card".
        if snap_start is not current_snapshot:
            archived_snap = snap_start
            cohort = _cohort_energy_from_archived(snap_start)
            # A SpotMonthlyRates leg bills at the current month's mean spot,
            # which needs an ENTSO-E key. Only the dynamic and spot-monthly
            # contract kinds are asked for one, so a variable cohort can reach
            # here without a key: keep the current card (priced off its own
            # resolved rate) instead of tearing the entry down over a key the
            # user was never prompted for.
            #
            # An archived DynamicRates leg needs a spot just as much, and is
            # deliberately not gated here: every extractor derives the energy
            # shape from the static catalogue kind rather than from the card
            # text, so a dynamic leg implies kind == "dynamic", which always
            # collected a key. Flipping an existing contract's kind in place,
            # or sniffing the shape out of the card, would break that and let
            # a keyless entry reach the spot fetch again.
            if isinstance(cohort, SpotMonthlyRates) and not entry.data.get(
                CONF_API_KEY
            ):
                cohort = None
            archived = cohort
    # The typed rate overlays whichever card was retrieved, so a user who
    # filled in only some boxes keeps the archived signing-month values for
    # the rest rather than today's. ``_manual_energy_leg`` returns None when
    # every box was left blank, which leaves the archive (or the current card)
    # billing as before.
    # The card's published rate travels ON the snapshot, so every caller of
    # this function gets it without threading a parameter through eight
    # signatures, which is how the conversion previously reached the live
    # tick only, leaving the year-to-date and monthly paths 21 EUR/yr adrift
    # on the same entry. Fall back to vat_rate for a raw (unresolved) card.
    #
    # Only on a contract the signing-rate step is asked for. Popping the keys
    # in the flow keeps a new one from being left behind, and this is what
    # heals the entries already holding one: a Mega Dynamic signer who moved
    # to Smart Flex kept the Dynamic coefficients, and a Smart Flex cohort
    # re-priced to a spot-monthly leg billed them, 113 to 130 EUR a year of
    # energy plus the old contract's standing charge.
    taxes = current_snapshot.taxes
    manual = (
        _manual_energy_leg(
            entry,
            current_snapshot.energy if archived is None else archived,
            taxes.published_vat_rate or taxes.vat_rate,
        )
        if takes_signing_rate(entry.data)
        else None
    )
    energy = manual if manual is not None else archived
    if energy is None:
        # Nothing to freeze a rate from. Four ways, not the three this used
        # to list: signed this month, a supplier with no archive, a month the
        # archive does not hold, and a card that WAS retrieved but exposes no
        # re-priceable leg (_cohort_energy_from_archived returns None for a
        # variable card whose coefficients would not parse and for a TOU or
        # Impact card printing resolved bands with no formula behind them, and
        # a spot-monthly leg is dropped above without a key).
        #
        # The current card either way, and for the same reason in all four:
        # this must not switch a month-indexed card onto its printed figure,
        # which is LAST month's index by the card's own words. The
        # coefficients the current card prints are the ones an archived card
        # would have been re-priced from anyway, so the delivery month keeps
        # its own mean, exactly as it does for an entry that names no cohort
        # month at all. In the keyless case _month_indexed_leg returns None
        # too, so the entry keeps the printed rate, which is the same answer
        # the gate above reached.
        energy = _month_indexed_leg(current_snapshot, entry)
    if manual is not None:
        source = "hand-entered signing rate"
    elif archived is not None:
        source = "archived signing-month card"
    elif energy is not None:
        source = "current card, re-priced on the delivery month's index"
    else:
        source = "current card (no cohort rate available)"
    # Which of the three resolutions won is otherwise invisible: the sensors
    # publish a price, not its provenance, so "my signing rate does nothing"
    # was unanswerable without reading the source.
    _LOGGER.debug(
        "%s: contract started %s, energy priced from the %s",
        contract,
        start,
        source,
    )
    # The feed-in leg locks with the offtake leg, so it is resolved from the
    # same archived card rather than left on the current one (issue #85), and
    # laid onto the card of the month being billed.
    injection = (
        None
        if archived_snap is None
        else _cohort_injection_from_archived(
            archived_snap, month_snapshot or current_snapshot
        )
    )
    return _CohortLegs(
        energy=energy,
        injection=injection,
        card=_cohort_card(start, this_month, archived_snap, current_snapshot),
    )


async def _cohort_energy_leg(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: "SupplierSnapshot",
) -> EnergyRates | None:
    """The energy half of :func:`_cohort_legs`, for callers that price only
    the offtake side."""
    legs = await _cohort_legs(
        hass, session, extractor, contract, region, entry, current_snapshot
    )
    return legs.energy


async def signing_month_snapshot(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: "SupplierSnapshot",
    *,
    cached_only: bool = False,
) -> "SupplierSnapshot":
    """The card published the month this contract was signed, or the current one.

    Rates are not the only thing that belongs to the version signed. A welcome
    credit does too, by its own terms ("enkel tijdens je eerste inschrijvingsjaar
    voor deze productversie"), and EnergyVision moved that figure four times
    between March and September 2026, so reading it off today's card credits a
    March cohort 200 EUR where its own card promised 300.

    The current snapshot comes back unchanged where there is nothing to
    retrieve: a contract that is not the entry's own, no cohort month, a
    cohort month inside the running month, or a month no archive can hold
    (a supplier that keeps none, before the repository's first month).
    Identity in those cases, so a caller can use the result unconditionally.

    The own-contract gate is the same one :func:`_cohort_legs` opens with, and
    for a sharper reason here. The compare sweep walks the year-to-date engine
    once per candidate, and this would then resolve an archived card for each
    of them, addressed by a signing month belonging to a different contract
    entirely. The credit is gated again where it is used, so every one of those
    lookups is discarded: a card fetch per candidate, off the sweep's budget,
    for an answer nothing reads.
    """
    if contract != entry.data.get(CONF_CONTRACT):
        return current_snapshot
    start = _tariff_card_month(entry)
    if start is None:
        return current_snapshot
    now = dt_util.now()
    if start >= date(now.year, now.month, 1):
        # The current card IS the signing-month card.
        return current_snapshot
    if not _month_card_retrievable(extractor, start, now.date(), entry):
        return current_snapshot
    resolved = await _snapshot_for_month(
        hass,
        session,
        extractor,
        contract,
        region,
        start,
        current_snapshot,
        entry,
        cached_only=cached_only,
    )
    if cached_only and resolved is current_snapshot:
        # The setup path forbids a fetch, so this is today's card standing in
        # for a month it is not the card of. That is the documented proxy and
        # it is sound for RATES, which move slowly and whose stand-in is at
        # worst a near miss. It is not sound for a welcome credit, which is
        # the property of one month's card: Luminus's campaign exists only on
        # the live card of the month it ran in, because the supplier archive
        # strips it, so crediting a May cohort off September's card invents
        # 100,43 EUR of campaign that cohort was never offered, and the figure
        # then vanishes on the next tick when the archive answers properly.
        #
        # Withhold it until the real card arrives, which makes the cold tick
        # agree with every tick after it. Only this branch is touched: a
        # supplier with no archive, a contract with no cohort month and a
        # signing month inside the running one all return above, so they keep
        # reading the credit off the card they already had.
        return without_welcome_credit(resolved)
    return resolved


async def _effective_snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry,
    *,
    cached_only: bool = False,
) -> "SupplierSnapshot":
    """Delivery-month snapshot with the signing cohort's energy leg spliced in.

    Every archive-walking cost path calls this instead of
    :func:`_snapshot_for_month`: it resolves the delivery month's regulated
    DSO / tax overlays as before, then overlays the frozen signing-month
    energy so a locked contract bills its own rate every month while network
    tariffs and taxes still track the delivery month. A no-op (returns the
    plain delivery-month snapshot) when there is no cohort override.

    ``cached_only`` is passed straight through to the delivery-month lookup
    (see :func:`_snapshot_for_month`). The cohort leg below is NOT gated by
    it: the signing month is what the live price table is already built from
    on the same tick, so its row is in the cache by the time this runs.
    """
    snap_m = await _snapshot_for_month(
        hass,
        session,
        extractor,
        contract,
        region,
        year_month,
        current_snapshot,
        entry,
        cached_only=cached_only,
    )
    legs = await _cohort_legs(
        hass,
        session,
        extractor,
        contract,
        region,
        entry,
        current_snapshot,
        month_snapshot=snap_m,
    )
    if legs.energy is None and legs.injection is None:
        return snap_m
    changes: dict[str, object] = {}
    if legs.energy is not None:
        energy = legs.energy
        if isinstance(energy, SpotMonthlyRates):
            # The month's own archived card may carry the value its index
            # settled at (Eneco prints it on the next card). The cohort leg
            # holds the contract's coefficients; the month supplies the index.
            energy = replace(
                energy,
                index_realised=getattr(snap_m.energy, "index_realised", None),
            )
        changes["energy"] = energy
    if legs.injection is not None:
        # The feed-in coefficients lock with the offtake ones, so the credit
        # for a past month is billed off the signing card too (issue #85).
        #
        # The INDEX and the printed figure still belong to the delivery month,
        # exactly as on the energy leg above: the signing card holds what the
        # contract pays per unit of index, the month holds what the index
        # settled at. _cohort_legs lays the coefficients onto this month's own
        # leg for that reason. Carrying the signing month's index across made a
        # cohort's credit swing on whether the Synergrid profile happened to be
        # loaded, 62 EUR on a Trevion LifePowr entry signed in the spring, and
        # carrying today's printed figure back credited every keyless month at
        # September's rate.
        changes["injection"] = legs.injection
    return replace(snap_m, **changes)  # type: ignore[arg-type]


def _month_snapshot_cache(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    cached_only: bool = False,
) -> Callable[[date], Awaitable[SupplierSnapshot]]:
    """Return a memoised ``snap_for(month_first)`` fetching each delivery
    month's effective snapshot once.

    The live YTD cost and both backfill passes walk the same months
    repeatedly; the per-call cache keeps archive fetches to at most one
    per month. ``cached_only`` forwards the no-network mode the first
    coordinator tick runs its year-to-date walk in.
    """
    cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in cache:
            cache[month_first] = await _effective_snapshot_for_month(
                hass,
                session,
                extractor,
                contract,
                region,
                month_first,
                snapshot,
                entry,
                cached_only=cached_only,
            )
        return cache[month_first]

    return _snap_for
