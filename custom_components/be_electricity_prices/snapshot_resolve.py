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

from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util

from .brugel import cached_power_term
from .const import (
    CONF_ANALOG_METER,
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CUSTOM_PROFESSIONAL,
    CONF_CUSTOM_VAT_RATE,
    CONF_DIRECT_DEBIT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INCLUDE_VAT,
    CONF_METER,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_DIRECT_DEBIT,
    DEFAULT_INCLUDE_VAT,
    DSO_MODE_IMPACT,
    METER_BI,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_INJECTION,
    SUPPLIER_CUSTOM,
    SUPPLIER_SOCIAL,
)
from .excise_law import household_schedule
from .fluvius import cached_analog_surcharge, cached_network_ceiling
from .providers import is_professional, offers_direct_debit, offers_quarter_hourly
from .providers._resolve import (
    apply_vat,
    on_card_basis,
    resolve_brussels_power_term,
    resolve_direct_debit,
    resolve_excise_band,
    resolve_federal_contribution,
    resolve_federal_excise,
    resolve_vat_rate,
    resolve_volume_tier,
    resolve_vreg_network_ceiling,
    resolve_welcome_credit_meter,
)
from .providers._settlement_grid import resolve_settlement_grid
from .providers.base import SupplierSnapshot

# Where a compare-page proxy (``compare_inputs._quote_entry``) freezes the
# household's measured yearly export. The proxy carries no coordinator, and no
# key the user can type holds the figure, so it gets one of its own.
QUOTED_ANNUAL_INJECTION_KWH = "quoted_annual_injection_kwh"


def _include_vat(entry: ConfigEntry) -> bool:
    """Whether this entry wants its prices VAT-inclusive.

    Inert on a residential card, which prints VAT-inclusive already; it
    only bites on a card published excluding VAT.

    The expert custom supplier never asks: the VAT rate typed on its tax step
    answers instead, 0 for a business that deducts it. Its own card bills the
    same either way, since apply_vat leaves a 0 rate alone and grosses a
    positive one as the default did, but a professional card quoted for it on
    the compare page is put onto the basis the entry bills on.
    """
    data = entry.data
    if CONF_INCLUDE_VAT not in data and data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
        # Unset, the rate is the month's residential one, never zero.
        rate = data.get(CONF_CUSTOM_VAT_RATE)
        return rate is None or float(rate) > 0.0
    return bool(data.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT))


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


def _custom_professional(entry: ConfigEntry, snap: SupplierSnapshot) -> bool:
    """Whether a custom formula entry is a business keeping its typed levies.

    The registry says which supplier cards are professional, and the expert
    custom supplier has no card for it to describe: the household says so on
    the tax step instead. A business there owes the professional excise and
    energy contribution it typed, which the residential corrections below
    would otherwise replace. Asked about the CARD in hand for the reason
    :func:`_quarter_hourly` gives: the compare page resolves another
    supplier's card through a proxy carrying this entry's data, and the
    answer describes the levies typed here, not that card's.
    """
    if snap.supplier != SUPPLIER_CUSTOM:
        return False
    return bool(entry.data.get(CONF_CUSTOM_PROFESSIONAL, False))


def offers_analog_meter(data: Mapping[str, Any]) -> bool:
    """Whether this entry's meter may be one Fluvius still reads as analog.

    A Flemish mono, bi-hourly or exclusive-night meter: a dynamic contract is
    billed off a digital meter's quarter-hours, and only Fluvius bills the two
    apart. Not on the custom supplier, whose network figures are the ones the
    household typed for its own meter, nor on the social tariff, whose network
    component is the CREG's.
    """
    return (
        data.get(CONF_REGION) == REGION_FLANDERS
        and data.get(CONF_METER, METER_MONO)
        in (METER_MONO, METER_BI, METER_EXCLUSIVE_NIGHT)
        and data.get(CONF_SUPPLIER) not in (SUPPLIER_CUSTOM, SUPPLIER_SOCIAL)
    )


def analog_meter(data: Mapping[str, Any]) -> bool:
    """Whether this entry is on a meter Fluvius still reads as analog.

    Fluvius cannot measure such a meter's peak: "Je betaalt dan een forfaitair
    bedrag voor 2.5 kW als maandpiek en een aanvullend tarief voor je verbruik
    in kWh". So the capacity tariff is billed on the minimum peak
    (``_billed_peak_kw``), the maximumtarief does not apply, and every kWh,
    normal or exclusive-night, carries the analog column's kWh rate in place of
    the peak-metered one (:func:`_resolve_analog_meter`).

    Asked of the entry's data, which a comparison proxy carries with the meter
    the quote is priced on: a dynamic target is sold on a digital meter, so it
    is quoted digital, on the minimum peak the household would start from.
    """
    return offers_analog_meter(data) and bool(data.get(CONF_ANALOG_METER, False))


def entry_analog_surcharge(entry: ConfigEntry, year: int) -> float | None:
    """What this entry's analog meter pays per kWh over a peak-metered one in
    ``year``, EUR/kWh excl. VAT, or ``None`` on a digital meter or before the
    area's sheet has been read (``fluvius.cached_analog_surcharge``)."""
    if not analog_meter(entry.data):
        return None
    return cached_analog_surcharge(year, str(entry.data.get(CONF_DSO, "")))


def _resolve_analog_meter(
    entry: ConfigEntry, snap: SupplierSnapshot, month: date
) -> SupplierSnapshot:
    """The entry's own network overlay as an analog meter is billed.

    Every distribution rate, the exclusive-night one included, takes the
    analog surcharge of the delivery year, put onto the basis the card prints
    on (``on_card_basis``), and the maximumtarief goes, since the regulator's
    sheet sets none for an analog meter. The capacity rate stays: the charge is
    billed on the minimum peak, the sheet's "Vaste term" to the cent.

    Identity on a digital meter, before the sheet is read, and on a card whose
    network is not Fluvius's tariffs (custom, social). Only the entry's own
    area, since the surcharge is that area's.
    """
    surcharge = entry_analog_surcharge(entry, month.year)
    if surcharge is None or snap.supplier in (SUPPLIER_CUSTOM, SUPPLIER_SOCIAL):
        return snap
    dso = str(entry.data.get(CONF_DSO, ""))
    overlay = snap.dsos.get(dso)
    if overlay is None:
        return snap
    extra = on_card_basis(snap, month, surcharge)

    def plus(rate: float | None) -> float | None:
        return None if rate is None else rate + extra

    analog = replace(
        overlay,
        distribution_single=overlay.distribution_single + extra,
        distribution_peak=plus(overlay.distribution_peak),
        distribution_offpeak=plus(overlay.distribution_offpeak),
        distribution_exclusive_night=plus(overlay.distribution_exclusive_night),
        network_ceiling_eur_per_kwh=None,
    )
    return replace(snap, dsos={**snap.dsos, dso: analog})


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
    A compare-page proxy has no coordinator and reads the figure its real
    entry answered, frozen under :data:`QUOTED_ANNUAL_INJECTION_KWH`.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return 0.0
    if coordinator is None:
        coordinator = getattr(entry, "runtime_data", None)
    measured = getattr(coordinator, "_annual_injection_kwh", None)
    if measured is None:
        measured = entry.data.get(QUOTED_ANNUAL_INJECTION_KWH)
    return float(measured) if measured else 0.0


def _stand_in_levies(snap: SupplierSnapshot, month: date) -> SupplierSnapshot:
    """The federal levies of a month ``snap`` stands in for, where the law's
    archived household wording covers it (``household_schedule``): its
    excise schedule and energy contribution, on the card's basis, since the
    card prints another month's. Not for a protected customer, whose rates
    are its own, and not for a month's own card, which is billed as printed:
    that wording leaves out the rates a transition or a quarterly notice set.
    """
    schedule = household_schedule(month)
    if schedule is None or snap.supplier == SUPPLIER_SOCIAL:
        return snap
    bands = tuple(
        (upper, on_card_basis(snap, month, rate)) for upper, rate in schedule.bands
    )
    taxes = replace(
        snap.taxes,
        federal_excise=bands[0][1],
        federal_excise_bands=bands,
        energy_contribution=on_card_basis(snap, month, schedule.contribution),
    )
    return replace(snap, taxes=taxes)


def _resolve_snapshot(
    entry: ConfigEntry,
    snap: SupplierSnapshot,
    *,
    annual_kwh: float | None = None,
    delivery_month: date | None = None,
    stand_in: bool = False,
) -> SupplierSnapshot:
    """Resolve a card against the site facts only this entry knows.

    ``annual_kwh`` is the yearly volume to resolve the excise band, the network
    ceiling and a volume tranche against, ``entry_annual_kwh(entry)`` when
    omitted. The coordinator passes the figure it resolved itself, because on
    its first tick the entry cannot lead to the coordinator yet (see
    :func:`entry_annual_kwh`); every other caller leaves it to the entry.

    Every step is identity on a card that carries none of them, so this is
    free for every existing entry. Order matters only around ``apply_vat``,
    which settles the card's VAT basis for the entry: what has to be put onto
    the basis the card printed on goes before it, and a step after it that
    needs that basis reads ``published_vat_rate``, which ``apply_vat`` keeps
    when it zeroes ``vat_rate``. The excise band and the volume tier are both
    per-kWh rates, which ``apply_vat`` never touches (it grosses the fees and
    the feed-in leg), and the settlement grid moves no rate at all.

    ``delivery_month`` is the month being billed, which decides both federal
    levies: whether the energy contribution is owed at all, and what the
    special excise is. Today's when omitted, since every caller but the month
    rows prices the running month. ``stand_in`` says the card is not that
    month's own but stands in for it (``_proxy_for_month``), so the levies it
    prints are another month's (``_stand_in_levies``).

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
    professional = is_professional(snap.supplier, snap.contract)
    # First: every figure below is put onto the card's VAT basis, which is
    # the month's rate where the card's own was an assumption.
    resolved = resolve_vat_rate(snap, month, professional=professional)
    resolved = resolve_brussels_power_term(
        resolved, terms=cached_power_term(month.year), month=month
    )
    # Before apply_vat for the same reason, and the month decides it the way
    # it decides the two federal levies below: the VREG sets one ceiling for
    # all of Flanders per calendar year, so a card stating another one is out
    # of date rather than different.
    resolved = resolve_vreg_network_ceiling(
        resolved, month, cached_network_ceiling(month.year)
    )
    # After the ceiling, which an analog meter does not have, and before
    # apply_vat for the reason Brugel's term goes first.
    resolved = _resolve_analog_meter(entry, resolved, month)
    resolved = apply_vat(resolved, include_vat=_include_vat(entry))
    # The two federal levies, both defined by the month being billed rather
    # than by the card that prints them. A business on the custom supplier
    # keeps the professional ones it typed; its VAT rate is its own and stays
    # out of resolve_vat_rate's professional branch above.
    levies_professional = professional or _custom_professional(entry, snap)
    resolved = resolve_federal_contribution(
        resolved, month, professional=levies_professional
    )
    resolved = resolve_federal_excise(resolved, month, professional=levies_professional)
    if stand_in and not levies_professional:
        resolved = _stand_in_levies(resolved, month)
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
        resolved,
        quarter_hourly=_quarter_hourly(entry, snap),
        impact=entry.data.get(CONF_DSO_TARIFF_MODE) == DSO_MODE_IMPACT,
    )
