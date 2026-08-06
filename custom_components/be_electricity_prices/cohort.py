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
current card -- per field, because only the user knows whether they signed at
the card rate or a negotiated one."""

from __future__ import annotations

from collections.abc import Awaitable
from collections.abc import Callable
from dataclasses import replace
from datetime import date
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp

from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
)
from .synergrid import (
    _LOGGER,
)
from .providers.base import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    VariableRates,
)
from .snapshot_store import (
    _include_vat,
    _snapshot_for_month,
)


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


def _contract_start_month(entry: ConfigEntry) -> date | None:
    """First-of-month of the configured contract start date, or ``None``.

    The signing month is what a fixed/dynamic contract's rate is locked
    against; the day within the month is irrelevant to which monthly card
    applies, so normalise to the first.
    """
    d = _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE))
    if d is None:
        return None
    return date(d.year, d.month, 1)


def _manual_energy_leg(
    entry: ConfigEntry, card: EnergyRates, card_vat_rate: float = 0.0
) -> EnergyRates | None:
    """Overlay a hand-entered signing rate onto ``card``, or ``None``.

    The user typed the rate they signed at, so it wins over anything the
    integration can retrieve: ``card`` is the leg that would otherwise bill
    (the archived signing-month card when the supplier keeps one, else the
    current card) and every box the user filled replaces its field. Shaped to
    match the contract's kind (dynamic -> factor / base, fixed -> single /
    peak / offpeak / exclusive night). Per-kWh values are stored as entered
    (grossed by compute_breakdown at the current card's VAT rate). ``None``
    when every box was left blank, or the contract is neither fixed nor
    dynamic.

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

    Fixed / dynamic: the archived leg is exactly the locked rate. Variable:
    re-price the cohort's numeric formula coefficients against the CURRENT
    month's mean (a SpotMonthlyRates leg) rather than freeze the archived
    card's stale resolved rate, which would pin the signing-month index.
    ``None`` when the archived card exposes no re-priceable rate (a variable
    card whose coefficients couldn't be parsed, or a TOU / Impact kind).
    """
    energy = archived.energy
    if isinstance(energy, (FixedRates, DynamicRates)):
        return energy
    if isinstance(energy, VariableRates) and energy.formula_factor is not None:
        return SpotMonthlyRates(
            factor=energy.formula_factor,
            base=energy.formula_base if energy.formula_base is not None else 0.0,
            yearly_fixed_fee=energy.yearly_fixed_fee,
            # Carry the dedicated exclusive-night standing fee so an
            # exclusive-night meter keeps its own fee instead of falling back to
            # the standard abonnement (yearly_fixed_fee_for_meter reads it).
            yearly_fixed_fee_exclusive_night=energy.yearly_fixed_fee_exclusive_night,
        )
    return None


async def _cohort_energy_leg(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: "SupplierSnapshot",
) -> EnergyRates | None:
    """Resolve the energy leg a contract actually bills at, or ``None``.

    A fixed / dynamic contract signed months ago is billed at the rate it
    locked in at signing, not today's card. This returns the signing-month
    card's energy leg so the caller can splice it onto the current
    delivery-month DSO / tax overlays; ``None`` means "no cohort override,
    keep the current energy" (no start date set, or nothing to re-price with:
    no signing rate typed and no archived card, or a variable cohort with no
    ENTSO-E key to resolve its monthly mean).

    Resolution order is a hand-entered signing rate, then the archive, then
    the current card. What the user typed wins per field: only they know
    whether they signed at the card rate or at a promotional, brokered or
    negotiated one, and the form that collected the value promises to price
    the contract with it. The archived signing-month card fills in every field
    left blank when the supplier keeps an archive; the current card does
    otherwise.

    ``None`` is also returned for a ``contract`` that isn't the entry's own
    (the OptionsFlow compare path walks an alternative contract with no
    signing history, so it must always price at the current card).
    """
    if contract != entry.data.get(CONF_CONTRACT):
        return None
    start = _contract_start_month(entry)
    if start is None:
        return None
    now = dt_util.now()
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
    if start < date(now.year, now.month, 1) and extractor.fetch_for_month is not None:
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
    # signatures -- which is how the conversion previously reached the live
    # tick only, leaving the year-to-date and monthly paths 21 EUR/yr adrift
    # on the same entry. Fall back to vat_rate for a raw (unresolved) card.
    taxes = current_snapshot.taxes
    manual = _manual_energy_leg(
        entry,
        current_snapshot.energy if archived is None else archived,
        taxes.published_vat_rate or taxes.vat_rate,
    )
    if manual is not None:
        source = "hand-entered signing rate"
    elif archived is not None:
        source = "archived signing-month card"
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
    return manual if manual is not None else archived


async def _effective_snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry,
) -> "SupplierSnapshot":
    """Delivery-month snapshot with the signing cohort's energy leg spliced in.

    Every archive-walking cost path calls this instead of
    :func:`_snapshot_for_month`: it resolves the delivery month's regulated
    DSO / tax overlays as before, then overlays the frozen signing-month
    energy so a locked contract bills its own rate every month while network
    tariffs and taxes still track the delivery month. A no-op (returns the
    plain delivery-month snapshot) when there is no cohort override.
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
    )
    cohort = await _cohort_energy_leg(
        hass, session, extractor, contract, region, entry, current_snapshot
    )
    if cohort is None:
        return snap_m
    return replace(snap_m, energy=cohort)


def _month_snapshot_cache(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
) -> Callable[[date], Awaitable[SupplierSnapshot]]:
    """Return a memoised ``snap_for(month_first)`` fetching each delivery
    month's effective snapshot once.

    The live YTD cost and both backfill passes walk the same months
    repeatedly; the per-call cache keeps archive fetches to at most one
    per month.
    """
    cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in cache:
            cache[month_first] = await _effective_snapshot_for_month(
                hass, session, extractor, contract, region, month_first, snapshot, entry
            )
        return cache[month_first]

    return _snap_for
