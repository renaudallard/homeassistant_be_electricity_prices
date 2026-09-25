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

"""Resolving a parsed card against the facts only one entry knows.

A card is published for everyone; a bill is not. VAT treatment, the meter, how
the household pays, the settlement grid and the yearly volume are answers this
entry gave, and every one of them changes what the same card costs. They are
applied ONCE here and baked into the snapshot each cost path then reads, for
the reason each resolver's docstring gives: a transform that has to reach the
live tick, the year-to-date walk, the backfill, both comparison quotes and the
projection is a transform that will miss one of them if it is applied at the
call sites.

Split out of ``snapshot_store`` because it depends on nothing else there, and
because the month cache below it needs to call it without importing the cache
back.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .brugel import cached_power_term
from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_DIRECT_DEBIT,
    CONF_INCLUDE_VAT,
    CONF_METER,
    CONF_QUARTER_HOURLY,
    CONF_SOLAR_REGIME,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_DIRECT_DEBIT,
    DEFAULT_INCLUDE_VAT,
    METER_MONO,
    SOLAR_REGIME_INJECTION,
)
from .providers import is_professional, offers_direct_debit, offers_quarter_hourly
from .providers.base import SupplierSnapshot
from .providers._resolve import (
    apply_vat,
    resolve_brussels_power_term,
    resolve_direct_debit,
    resolve_excise_band,
    resolve_federal_contribution,
    resolve_federal_excise,
    resolve_settlement_grid,
    resolve_volume_tier,
    resolve_vreg_network_ceiling,
    resolve_welcome_credit_meter,
)


def _include_vat(entry: ConfigEntry) -> bool:
    """Whether this entry wants its prices VAT-inclusive.

    Inert on a residential card, which prints VAT-inclusive already; it
    only bites on a card published excluding VAT.
    """
    return bool(entry.data.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT))


def _quarter_hourly(entry: ConfigEntry, snap: SupplierSnapshot) -> bool:
    """Whether this snapshot should settle per quarter-hour for this entry.

    Both halves have to agree, exactly as the injection and month-index
    registry flags do. The stored answer alone would keep billing per quarter
    after a supplier withdrew the option; the registry flag alone knows the
    product offers a choice but not which side this household took.

    The registry half is asked about the CARD in hand, not about the entry's
    own contract, because the compare page resolves an alternative supplier's
    snapshot through a proxy that carries the user's ``supplier`` /
    ``contract``. Reading those would take a Frank customer's answer and
    settle a Mega card per quarter-hour, which Mega does not sell. Asking the
    snapshot keeps the entry's own card on exactly the same path (the two
    contracts are the same one there) and carries the preference across only
    where the target really offers the choice.
    """
    if not bool(entry.data.get(CONF_QUARTER_HOURLY, False)):
        return False
    return offers_quarter_hourly(snap.supplier, snap.contract)


def _direct_debit(entry: ConfigEntry, snap: SupplierSnapshot) -> bool:
    """Whether this entry's standing charge takes the card's direct-debit cut.

    Both halves have to agree, for the reason :func:`_quarter_hourly` gives,
    and the registry half is asked about the CARD in hand for the same one:
    the compare page resolves an alternative supplier's snapshot through a
    proxy carrying the user's own supplier and contract, and how a household
    pays is a fact about the household, so it should follow it onto a target
    whose card prices it, and nowhere else.
    """
    if not bool(entry.data.get(CONF_DIRECT_DEBIT, DEFAULT_DIRECT_DEBIT)):
        return False
    return offers_direct_debit(snap.supplier, snap.contract)


def entry_annual_kwh(entry: ConfigEntry, coordinator: Any = None) -> float:
    """How much this household uses in a year, in kWh. One answer for every leg.

    Three legs resolve against a yearly volume: the degressive excise band,
    the Flemish network ceiling and EnergyVision's volume tranche, and they
    have to agree, or one card is priced against two different households.

    Four answers in order, and the order is the point:

    1. a FULL YEAR of meter, which beats anything stated;
    2. the volume typed on the entry, which only a professional card asks for;
    3. a shorter measurement scaled up to a year (90 days at least);
    4. the 3.500 kWh household default.

    The metered figure comes first because the typed one barely exists: the
    config flow asks for a yearly volume on a PROFESSIONAL card only, and drops
    the key again on a residential one, so every residential entry fell through
    to the default whatever it really used. On a plain card that is harmless
    (the excise has been flat since August 2026 and the ceiling only binds on a
    very low-volume connection), but a volume-tiered card splits its tranche
    against it: measured on the September GS1800V card at August's index, a
    6.000 kWh household was billed 88 EUR/year under its own card and a 2.000
    kWh one 53 EUR over it.

    A typed volume sits ABOVE the scaled band rather than below it, which is
    the one place a business notices. Its card bands the excise, and a
    seasonally uncorrected extrapolation of a winter quarter can cross a band:
    a 30.000 kWh entry whose 90 days scale to 52.000 crosses the 50.000 one and
    pays about 19 EUR a year less excise than the figure it actually stated. A
    full year of meter still wins, because that is a measurement of the year
    the excise is billed on rather than an estimate of it.

    The coordinator measures it once a day through ``_annual_volume``, the same
    read the compare page quotes its rows from, so the price here and the
    ranking beside it are built on one volume. Read off the entry the way the
    RLP and SPP profiles are (``_coordinator_rlp_weights``), which is what
    keeps the compare page's read-only entry proxy working: it carries no
    coordinator, so a what-if falls back to the typed figure and then to the
    default exactly as before.

    ``coordinator`` is for the coordinator resolving its OWN card, and dates
    from when setup assigned ``entry.runtime_data`` only after the first
    refresh had returned: during that refresh the entry could not lead here,
    so the first tick resolved a volume-tiered card against the household
    default while believing it had used the measurement, and nothing
    re-resolved it until the trailing-year figure next moved. Setup now
    assigns the attribute before the first refresh, which is what lets the
    month rows, the cohort card and the network ceiling, all of which arrive
    here through the entry alone, see the measurement on that tick too.
    """
    if coordinator is None:
        coordinator = getattr(entry, "runtime_data", None)
    measured = getattr(coordinator, "_annual_kwh", None)
    if measured and getattr(coordinator, "_annual_kwh_full_year", False):
        return float(measured)
    typed = entry.data.get(CONF_ANNUAL_CONSUMPTION_KWH)
    if typed:
        try:
            return float(typed)
        except (TypeError, ValueError):
            pass
    if measured:
        return float(measured)
    return float(DEFAULT_ANNUAL_CONSUMPTION_KWH)


def entry_annual_injection_kwh(entry: ConfigEntry, coordinator: Any = None) -> float:
    """How much this household SELLS back in a year, in kWh, or 0.0.

    What a first-year feed-in bonus multiplies (Mega's "bonus ... pour votre
    injection sur le reseau de distribution pour votre premiere annee de
    souscription"). Only the injection regime sells its export; under
    compensation it nets against the draw and there is no feed-in price for a
    bonus to add to, and without solar there is nothing to sell.

    A FULL trailing year of meter or nothing, the rule the projection already
    holds its feed-in to: PV is too seasonal for a shorter window scaled by a
    day count, and no typed figure or default exists for the export, so an
    entry without a year of it is credited no bonus rather than a guess.
    Measured once a day by the coordinator, beside the consumption volume.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return 0.0
    if coordinator is None:
        coordinator = getattr(entry, "runtime_data", None)
    measured = getattr(coordinator, "_annual_injection_kwh", None)
    return float(measured) if measured else 0.0


def _resolve_snapshot(
    entry: ConfigEntry,
    snap: SupplierSnapshot,
    *,
    annual_kwh: float | None = None,
    delivery_month: date | None = None,
) -> SupplierSnapshot:
    """Resolve a card against the site facts only this entry knows.

    ``annual_kwh`` is the yearly volume to resolve the excise band, the network
    ceiling and a volume tranche against, ``entry_annual_kwh(entry)`` when
    omitted. The coordinator passes the figure it resolved itself, because on
    its first tick the entry cannot lead to the coordinator yet (see
    :func:`entry_annual_kwh`); every other caller leaves it to the entry.

    All four steps are identity on a card that carries none of them, so this
    is free for every existing entry. Order is irrelevant: the excise band and
    the volume tier are both per-kWh rates, ``apply_vat`` never touches those
    (it grosses the fees and the feed-in leg), and the settlement grid moves
    no rate at all.

    ``delivery_month`` is the month being billed, which decides both federal
    levies: whether the energy contribution is owed at all, and what the
    special excise is. Today's when omitted, since every caller but the month
    rows prices the running month.

    The tranche is dropped rather than folded on an exclusive-night entry: the
    cards that carry one put it on the single register, or split it 900/900
    across a day/night pair, and say in the same footnote that it is "niet van
    toepassing op het exclusief nacht tarief". Folding it there billed a night
    circuit a share of a tranche it never receives.
    """
    # Before apply_vat, which resolves the card's own basis away: the term
    # Brugel publishes is stated excluding VAT and has to be put onto the
    # basis the card printed on before anything else moves it.
    month = delivery_month or dt_util.now().date()
    resolved = resolve_brussels_power_term(snap, terms=cached_power_term(month.year))
    # Before apply_vat for the same reason, and the month decides it the way
    # it decides the two federal levies below: the VREG sets one ceiling for
    # all of Flanders per calendar year, so a card stating another one is out
    # of date rather than different.
    resolved = resolve_vreg_network_ceiling(resolved, month)
    resolved = apply_vat(resolved, include_vat=_include_vat(entry))
    # The two federal levies, both defined by the month being billed rather
    # than by the card that prints them.
    professional = is_professional(snap.supplier, snap.contract)
    resolved = resolve_federal_contribution(resolved, month, professional=professional)
    resolved = resolve_federal_excise(resolved, month, professional=professional)
    if annual_kwh is None:
        annual_kwh = entry_annual_kwh(entry)
    # Before the tranche, which can turn a spot-monthly leg into a fixed one:
    # the fee travels across that conversion but a reduction still waiting to
    # be applied would not.
    resolved = resolve_direct_debit(resolved, direct_debit=_direct_debit(entry, snap))
    # Beside the direct-debit answer and for the same reason: which meter the
    # entry has decides whether the card grants the credit at all, and baking
    # it here keeps the six cost paths reading one credit.
    resolved = resolve_welcome_credit_meter(
        resolved, entry.data.get(CONF_METER, METER_MONO)
    )
    resolved = resolve_volume_tier(
        resolve_excise_band(resolved, annual_kwh),
        annual_kwh,
        meter=entry.data.get(CONF_METER, METER_MONO),
    )
    return resolve_settlement_grid(
        resolved, quarter_hourly=_quarter_hourly(entry, snap)
    )
