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
from typing import Any, NamedTuple

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .cohort_legs import (
    _cohort_card,
    _cohort_energy_from_archived,
    _cohort_injection_from_archived,
    _manual_energy_leg,
    _month_indexed_leg,
)
from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_SUPPLIER,
    CONF_TARIFF_CARD_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    SUPPLIER_CUSTOM,
    SUPPLIER_SOCIAL,
)
from .providers import takes_signing_rate
from .providers._rates import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    SpotMonthlyRates,
    rescale_vat,
)
from .providers._resolve import card_residential_vat, without_welcome_credit
from .providers.base import (
    SupplierExtractor,
    SupplierSnapshot,
)
from .snapshot_months import (
    _month_card_retrievable,
    _snapshot_for_month,
    month_card,
    month_card_cached,
)
from .vat_rates import residential_vat
from .year_ahead import YEAR_AHEAD

_LOGGER = logging.getLogger(__name__)

# The resolution last logged per contract and start date. The legs are
# resolved once for every month a walk prices, about forty times a tick on a
# year to date, and the line is only news when its answer changes.
_LOGGED_SOURCE: dict[tuple[str, date | None], str] = {}


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


class _CohortLegs(NamedTuple):
    """What a signing cohort bills at, both halves of it.

    ``None`` on either means "no override, keep the current card's leg".
    ``card`` names the card they were read off, for the sensor attribute;
    empty when the entry names no cohort month and nothing was resolved.
    """

    energy: EnergyRates | None
    injection: InjectionRates | None
    card: str = ""
    # The residential VAT rate the energy leg's coefficients carry: the rate
    # of the card and month it was read off. None where it carries none to
    # move (a leg read off the card of the month being billed, or a card
    # priced excluding VAT, which the engine grosses at the month's rate).
    vat_rate: float | None = None

    def energy_on(
        self, snapshot: SupplierSnapshot, delivery_month: date
    ) -> EnergyRates | None:
        """The energy leg put onto ``snapshot``'s VAT basis and settlement
        grid for ``delivery_month``.

        A cohort keeps the coefficients it signed for, not the VAT of the
        month it signed in: VAT is owed at the rate of the month delivered,
        so a leg read off an earlier card is moved onto that one's rate.
        Identity while the two rates agree, which is every month today.

        Nor does it keep the market's settlement grid. Ecopower settled per
        hour until September 2025 and per quarter-hour since, and every
        signing month before the switch reads the January 2025 card, so a
        dynamic leg carried its grid across and priced today on the hour.
        """
        energy = self.energy
        if energy is None:
            return None
        card = snapshot.energy
        if (
            isinstance(energy, DynamicRates)
            and isinstance(card, DynamicRates)
            and energy.quarter_hourly != card.quarter_hourly
        ):
            energy = replace(energy, quarter_hourly=card.quarter_hourly)
        if self.vat_rate is None:
            return energy
        taxes = snapshot.taxes
        if taxes.published_vat_rate or taxes.vat_rate:
            return energy
        rate = card_residential_vat(snapshot, delivery_month)
        return rescale_vat(energy, (1.0 + rate) / (1.0 + self.vat_rate))

    def splice(
        self, snapshot: SupplierSnapshot, delivery_month: date | None = None
    ) -> SupplierSnapshot:
        """``snapshot`` billed on this cohort: each leg it overrides replaced,
        the rest of the card kept. The same object back when there is nothing
        to override, so a caller can tell the no-op by identity.
        ``delivery_month`` is the month billed, today's when None."""
        if self.energy is None and self.injection is None:
            return snapshot
        energy = self.energy_on(snapshot, delivery_month or dt_util.now().date())
        return replace(
            snapshot,
            energy=snapshot.energy if energy is None else energy,
            injection=snapshot.injection if self.injection is None else self.injection,
        )


async def _cohort_legs(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: SupplierSnapshot,
    month_snapshot: SupplierSnapshot | None = None,
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
    if entry.data.get(CONF_SUPPLIER) == SUPPLIER_SOCIAL:
        # The social tariff is one card for every protected customer each
        # month, whenever they signed: the CREG's quarter for the energy and
        # Engie's month formulas for the feed-in. Freezing the signing card's
        # feed-in coefficients credited a start-dated entry on a formula the
        # supplier no longer applies, and dropped the day and night pairs.
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
    # Why a retrieved signing card prices nothing, for its label.
    unbilled = "no formula on it to re-price"
    if start < this_month and _month_card_retrievable(
        extractor, start, now.date(), entry
    ):
        snap_start = await month_card(
            hass, session, extractor, contract, region, start, entry
        )
        # None when no archive holds the signing month's card.
        if snap_start is not None:
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
                unbilled = "no ENTSO-E key"
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
    if _LOGGED_SOURCE.get((contract, start)) != source:
        _LOGGED_SOURCE[(contract, start)] = source
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
    # The VAT the leg's coefficients carry: the signing month's, on the card
    # retrieved for it or, for a rate typed off the household's own contract
    # with no card retrieved, the month it was signed in whatever today's card
    # states; today's on the current card's own leg.
    vat_rate: float | None
    if archived_snap is not None and (manual is not None or archived is not None):
        vat_rate = _leg_vat(archived_snap, start)
    elif manual is not None:
        vat_rate = (
            None
            if _leg_vat(current_snapshot, start) is None
            else residential_vat(start)
        )
    else:
        vat_rate = _leg_vat(current_snapshot, now.date())
    # A frozen feed-in leg bills off the signing card only where it is not
    # the printed figure: fixed for the term, or resolved on spots.
    billed_off_archive = (
        manual is not None
        or archived is not None
        or (
            injection is not None
            and (injection.fixed_for_term or bool(entry.data.get(CONF_API_KEY)))
        )
    )
    return _CohortLegs(
        energy=energy,
        injection=injection,
        card=_cohort_card(
            start,
            this_month,
            archived_snap,
            current_snapshot,
            None if billed_off_archive else unbilled,
        ),
        vat_rate=vat_rate,
    )


def _leg_vat(snapshot: SupplierSnapshot, month: date) -> float | None:
    """The residential rate a leg read off ``snapshot`` carries, or None on a
    card priced excluding VAT, whose legs carry none."""
    taxes = snapshot.taxes
    if taxes.published_vat_rate or taxes.vat_rate:
        return None
    return card_residential_vat(snapshot, month)


async def _cohort_energy_leg(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: SupplierSnapshot,
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
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: SupplierSnapshot,
    *,
    cached_only: bool = False,
) -> SupplierSnapshot:
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
    start = _tariff_card_month(entry)
    if contract != entry.data.get(CONF_CONTRACT):
        return _signed_in(current_snapshot, start)
    if start is None:
        return current_snapshot
    now = dt_util.now()
    if start >= date(now.year, now.month, 1):
        # The current card IS the signing-month card.
        return _signed_in(current_snapshot, start)
    if not _month_card_retrievable(extractor, start, now.date(), entry):
        return _signed_in(current_snapshot, start)
    resolved = await month_card(
        hass,
        session,
        extractor,
        contract,
        region,
        start,
        entry,
        cached_only=cached_only,
    )
    if (
        cached_only
        and resolved is None
        and not month_card_cached(hass, extractor.id, contract, region, start)
    ):
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
        # agree with every tick after it. Only a month never fetched is
        # withheld: one cached as None is the archive's final "no card for
        # this month", which every warm tick answers off the current card
        # too. A supplier with no archive, a contract with no cohort month and
        # a signing month inside the running one all return above, so they
        # keep reading the credit off the card they already had.
        return without_welcome_credit(current_snapshot)
    return _signed_in(current_snapshot if resolved is None else resolved, start)


def _signed_in(snapshot: SupplierSnapshot, card_month: date | None) -> SupplierSnapshot:
    """``snapshot``, less a welcome credit its card ties to another month.

    A card that names the month a contract must be signed in grants nothing
    to one signed in any other, whichever card ends up standing in for it.
    That stand-in is the current card wherever the signing month's own cannot
    be had: a Bolt variable card is addressed by version rather than by month,
    so a Plenty Online contract signed in March read October's "reduction de
    9,0 c€/kWh ... au cours du mois d'octobre 2026", about 315 EUR it was never
    offered. A candidate on the compare page is credited as if signed when the
    household signed its own, so the same rule holds for it.

    No card month means no start date either, and the credit needs one, so
    there is nothing to withhold.
    """
    month = snapshot.welcome_credit_signing_month
    if month is None or card_month is None or month == card_month:
        return snapshot
    return without_welcome_credit(snapshot)


async def _effective_snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    cached_only: bool = False,
    current_raw: SupplierSnapshot | None = None,
) -> SupplierSnapshot:
    """Delivery-month snapshot with the signing cohort's energy leg spliced in.

    Every archive-walking cost path calls this instead of
    :func:`_snapshot_for_month`: it resolves the delivery month's regulated
    DSO / tax overlays as before, then overlays the frozen signing-month
    energy so a locked contract bills its own rate every month while network
    tariffs and taxes still track the delivery month. A no-op (returns the
    plain delivery-month snapshot) when there is no cohort override.

    ``cached_only`` and ``current_raw`` are passed straight through to the
    delivery-month lookup (see :func:`_snapshot_for_month`). The cohort leg
    below is NOT gated by ``cached_only``: the signing month is what the live
    price table is already built from on the same tick, so its row is in the
    cache by the time this runs.

    A month after the running one, read ahead for the year-end cost
    (:mod:`year_ahead`), has no card anywhere yet and bills on the card as it
    prices today, which nothing is fetched for.
    """
    ahead = YEAR_AHEAD.get()
    if ahead is not None and year_month > ahead.pivot.replace(day=1):
        return ahead.card
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
        current_raw=current_raw,
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
    energy = legs.energy_on(snap_m, year_month)
    if energy is not None:
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
    current_raw: SupplierSnapshot | None = None,
) -> Callable[[date], Awaitable[SupplierSnapshot]]:
    """Return a memoised ``snap_for(month_first)`` fetching each delivery
    month's effective snapshot once.

    The live YTD cost and both backfill passes walk the same months
    repeatedly; the per-call cache keeps archive fetches to at most one
    per month. ``cached_only`` forwards the no-network mode the first
    coordinator tick runs its year-to-date walk in, and ``current_raw`` the
    card ``snapshot`` was resolved from (``_snapshot_for_month``).
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
                current_raw=current_raw,
            )
        return cache[month_first]

    return _snap_for
