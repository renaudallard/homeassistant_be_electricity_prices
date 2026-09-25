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

"""Standing charges: the capacity tariff, the Brussels OSP fee, the prosumer
forfait and the annual fixed fees.

Split out of coordinator.py. A true leaf: nothing here imports another module
of this package beyond const and providers, and the live sensor, the
year-to-date walk, the backfill and the compare quote all read these, which is
why they must not be duplicated per caller."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CONNECTION_KVA_TIER,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONNECTION_KVA_TIERS_ABOVE_13,
    DEFAULT_CONNECTION_KVA_TIER,
    DSO_MODE_IMPACT,
    MAX_QUOTED_WELCOME_WAIT_MONTHS,
    METER_MONO,
    REGION_WALLONIA,
    SOLAR_REGIME_COMPENSATION,
    VREG_CAPACITY_FLOOR_KW,
    WELCOME_CREDIT_ANNIVERSARY,
)
from .pricing import (
    MeterType,
    static_energy_eur_per_kwh,
    yearly_fixed_fee_for_meter,
)
from .providers.base import (
    DsoOverlay,
    SupplierSnapshot,
)
from .snapshot_resolve import entry_annual_kwh


def _capacity_monthly_eur(overlay: DsoOverlay | None, peak_kw: float) -> float:
    """One month of the Flemish capacity charge, or 0.0 when it isn't billed.

    The annual EUR/kW rate over twelve, with the two "nothing to bill" cases
    folded in: no overlay for this DSO, or a card that prints no capacity row.

    The raw rate, before the VREG ceiling. Every path that bills the charge
    goes through ``_capped_capacity_monthly_eur`` instead, which is the one
    that must agree across the live tick, the year-to-date walk and the
    backfill's per-hour accrual. ``_annual_static_fees`` is shared across the
    same three for the same reason; capacity is the fee that was left out of
    it, so it drifted here instead. Deliberately region-agnostic: each caller
    keeps its own Flanders gate.
    """
    if overlay is None or overlay.capacity_eur_per_kw_year is None:
        return 0.0
    return peak_kw * overlay.capacity_eur_per_kw_year / 12.0


def _annual_consumption_kwh(entry: ConfigEntry) -> float:
    """The household's yearly volume, in kWh.

    The VREG network ceiling is a rule about a YEAR: it caps the capacity
    charge plus the per-kWh network term against the volume the year carries,
    so it cannot be measured against a window.

    Delegates rather than reading ``entry.data`` itself, which is what it used
    to do. The comment here already claimed one answer to "how much does this
    household use", and there were two: this one saw only the estimate typed
    on a professional card, while the compare page beside it measured the
    meter. Now both go through :func:`entry_annual_kwh`.
    """
    return entry_annual_kwh(entry)


def _capped_capacity_monthly_eur(
    overlay: DsoOverlay | None,
    entry: ConfigEntry,
    peak_kw: float,
    meter: MeterType | None = None,
    vat_rate: float = 0.0,
) -> float:
    """One month of the Flemish capacity charge after the VREG ceiling.

    The cap belongs on every path that BILLS the charge, not only on the ones
    that quote it. It used to sit on the compare page and the projection
    alone, so a card printing a maximumtarief had its ceiling honoured in the
    what-if and ignored by the sensor the what-if is meant to match, and a
    low-volume connection was over-billed by the whole gap between them.

    Evaluated annually and divided back, because that is the shape of the
    rule; the caller then prorates the month it is accruing.

    ``meter`` overrides the entry's own, for the comparison page quoting a
    meter the household need not have: the headroom is measured against the
    per-kWh network term, and that term follows the meter the rest of the
    quote is priced on.
    """
    monthly = _capacity_monthly_eur(overlay, peak_kw)
    if not monthly:
        return 0.0
    capped = _capped_capacity_annual(
        overlay,
        12.0 * monthly,
        _annual_consumption_kwh(entry),
        meter or entry.data.get(CONF_METER, METER_MONO),
        vat_rate,
    )
    return capped / 12.0


def _compute_capacity(
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    peak_kw: float,
    meter: MeterType | None = None,
) -> float:
    # Read CONF_DSO defensively: a corrupt entry that lost the key
    # would otherwise KeyError here and tear the whole tick down via
    # UpdateFailed. _compute_prosumer already takes the same shape.
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0
    return _capped_capacity_monthly_eur(
        snapshot.dsos.get(dso), entry, peak_kw, meter, snapshot.taxes.vat_rate
    )


def _capped_capacity_annual(
    overlay: DsoOverlay | None,
    capacity_annual: float,
    annual_kwh: float,
    meter: MeterType,
    vat_rate: float = 0.0,
) -> float:
    """The Flemish capacity charge after the VREG ceiling, in EUR/year.

    Ecopower's card states the rule: "zou u met het capaciteitstarief en het
    nettarief per kWh meer nettarieven betalen dan met het maximumtarief? Dan
    betaalt u het maximumtarief. U betaalt dus nooit meer dan dat." So the
    capacity term plus the per-kWh network term may not exceed the ceiling
    times the volume, and the excess comes off the capacity term, which is the
    leg that produced it.

    The card states it as a SANDWICH, not a ceiling, and the second half
    matters as much as the first: "U betaalt dus nooit meer dan dat. U betaalt
    wel minstens de minimumbijdrage van 2,5 kW." So the capped figure may not
    fall below 2,5 kW of the capacity rate.

    That floor is not decoration, and it decides where the cap can bind at
    all. AT the floor it cannot: the minimum the sandwich guarantees is the
    same 2,5 kW of capacity the charge already is, so the ceiling has nothing
    to take off and the figure comes back unchanged at every volume. Above it
    the two diverge, and on Fluvius Zenne-Dijle at the 2026 rates the cap
    reduces the charge below roughly 600, 800, 1.225, 1.850 and 2.450 kWh a
    year at 3, 4, 6, 9 and 12 kW. Measured by driving this function, not from
    the inequality: the earlier note here put the region at the floor, where
    the floor itself rules it out.

    Without the floor the cap would reduce the charge below the regulated
    minimum and UNDER-bill, which is why it lands before the overlays that
    populate the ceiling rather than after them.

    Returns ``capacity_annual`` unchanged when the card prints no ceiling,
    when the volume is unknown, or when the total is already under it.

    ``vat_rate`` is the rate the engine still has to apply to a per-kWh rate,
    which is the card's on an entry billing VAT-inclusive and zero everywhere
    else. Both per-kWh terms here are as the card printed them, and the
    capacity charge they are measured against has already been grossed by
    ``apply_vat``, so the difference is put on that basis before the two are
    compared. On a residential card the factor is 1 and nothing moves.
    """
    if overlay is None or overlay.network_ceiling_eur_per_kwh is None:
        return capacity_annual
    if annual_kwh <= 0.0:
        return capacity_annual
    per_kwh = overlay.distribution_single + overlay.transport
    if meter == "exclusive_night" and overlay.distribution_exclusive_night is not None:
        per_kwh = overlay.distribution_exclusive_night + overlay.transport
    headroom = (
        (overlay.network_ceiling_eur_per_kwh - per_kwh) * (1.0 + vat_rate) * annual_kwh
    )
    capped = min(capacity_annual, max(headroom, 0.0))
    # ``_billed_peak_kw`` already floors the PEAK at 2,5 kW, so the charge
    # arrives at or above the minimum; the cap is the only thing that can push
    # it under, and the card forbids that.
    minimum = VREG_CAPACITY_FLOOR_KW * (overlay.capacity_eur_per_kw_year or 0.0)
    return max(capped, min(capacity_annual, minimum))


def _brussels_osp_fee(overlay: DsoOverlay | None, entry: ConfigEntry) -> float:
    """Brussels Brugel OSP annual fee (EUR/year) for the configured tier.

    The fee is a flat Sibelga charge scaled by contractual connection power;
    the user picks the tier in the config flow (default 1.44-6.00 kVA).
    Returns 0 outside Brussels or when the card omits the OSP table."""
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)


def _brussels_power_term(
    overlay: DsoOverlay | None, entry: ConfigEntry
) -> float | None:
    """Sibelga's power term for this entry's connection, in EUR/year.

    The card prints two columns, at or below 13 kVA and above it, and only the
    first was ever billed. A 3x400 V / 25 A residential connection is 17,3 kVA
    and belongs in the second, so a household with a heat pump or a charger was
    billed the smaller term.

    ``None`` when the card prints a single column or the entry is not above the
    line, and the caller keeps using ``data_management_per_year``.
    """
    if overlay is None or overlay.brussels_power_term_above_13kva is None:
        return None
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    if tier not in CONNECTION_KVA_TIERS_ABOVE_13:
        return None
    return overlay.brussels_power_term_above_13kva


def _walloon_fixed_term_applies(entry: ConfigEntry) -> bool:
    """Whether this entry pays the Walloon DSO's ``terme fixe``.

    The CWaPE tariff sheets print two configurations per DSO. The standard
    one carries a fixed term (row C, ``terme fixe``, in EUR/year); the
    incitative one, which the cards sell as the ``IMPACT`` tariff, carries a
    dash there and charges a capacity term instead. The suppliers say so on
    the cards: "le terme fixe n'est pas d'application pour le tarif IMPACT".

    Nothing offsets the difference, because CWaPE set that capacity term to
    0 EUR/kW for 2026 through 2029 on all five Walloon DSOs, so an entry on
    the incitative configuration simply has no fixed term to pay.

    Region-gated, because ``data_management_per_year`` is one field carrying
    three different charges: the Walloon terme fixe, the Flemish databeheer
    and the Brussels mesure plus fixed-term pair. Only the first one is tied
    to the tariff configuration.
    """
    if entry.data.get(CONF_REGION) != REGION_WALLONIA:
        return True
    return entry.data.get(CONF_DSO_TARIFF_MODE) != DSO_MODE_IMPACT


def _annual_static_fees(
    snapshot: SupplierSnapshot, meter: MeterType, entry: ConfigEntry
) -> float:
    """Fixed EUR/year fees that do not depend on consumption: the supplier
    yearly fixed fee (for ``meter``), twelve times the monthly energy-fund
    levy, the digital-meter data-management charge and the Brussels Brugel
    OSP fee.

    Shared by the live YTD sensor, the backfill accrual and the config-flow
    annual estimate so a new static-fee component is added in one place
    instead of drifting between the three paths. That is also why the
    Walloon IMPACT exemption belongs here: the per-kWh legs already read the
    tariff mode, and this was the one leg that did not, so the fixed term was
    billed on all four paths at once.
    """
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    fixed_term = (
        overlay.data_management_per_year
        if overlay is not None and _walloon_fixed_term_applies(entry)
        else 0.0
    )
    above_13 = _brussels_power_term(overlay, entry)
    if above_13 is not None:
        fixed_term = above_13
    return (
        float(yearly_fixed_fee_for_meter(snapshot.energy, meter) or 0.0)
        + 12.0 * float(snapshot.taxes.energy_fund_eur_per_month or 0.0)
        + fixed_term
        + _brussels_osp_fee(overlay, entry)
    )


def _prosumer_monthly_fee(
    overlay: DsoOverlay | None, snapshot: SupplierSnapshot, kva: float
) -> float:
    """Monthly prosumer (compensation-regime) fee for ``kva`` of inverter.

    Sums the DSO per-kVA/year tariff and the supplier-side compensation
    forfait (Cociter Variable), the latter already TVAC so it is summed
    raw, then divides to a monthly amount. Callers gate this to Walloon
    compensation installs; a missing rate contributes zero.
    """
    dso_rate = (
        overlay.prosumer_eur_per_kva_year
        if overlay is not None and overlay.prosumer_eur_per_kva_year is not None
        else 0.0
    )
    supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year or 0.0
    return kva * (dso_rate + supplier_rate) / 12.0


def _compensation_kva(entry: ConfigEntry) -> float:
    """Inverter kVA this entry bills the prosumer fee on, else 0.0.

    The whole eligibility gate in one place: the compensation regime, Wallonia,
    and a kVA that parses above zero. It was written out three times: the
    live tick, the year-to-date walk and the backfill, the last one with the
    kVA half in its own helper and the region half 580 lines away from it.

    Compensation is Walloon-only: a Flanders PV owner is either on net metering
    (not modelled here) or on a digital meter paying the capaciteitstarief, and
    billing the prosumer fee there too would double-count grid recovery.
    """
    kva = _walloon_compensation_kva(entry.data)
    return kva if kva is not None and kva > 0.0 else 0.0


def compensation_lacks_kva(data: Mapping[str, Any]) -> bool:
    """A Walloon compensation install with no inverter capacity entered.

    Always an omission: the regime exists for households with panels, and
    ``_compensation_kva`` then bills no prosumer fee at all, about 429 EUR a
    year at 5 kVA on ORES. The solar step refuses it, and the coordinator
    raises a Repairs card for an entry saved before it did.
    """
    kva = _walloon_compensation_kva(data)
    return kva is not None and not kva > 0.0


def _walloon_compensation_kva(data: Mapping[str, Any]) -> float | None:
    """The entered kVA of a Walloon compensation install, 0.0 when it does not
    parse, None for any other regime or region."""
    if data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_COMPENSATION:
        return None
    if data.get(CONF_REGION) != REGION_WALLONIA:
        return None
    try:
        return float(data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _compute_prosumer(snapshot: SupplierSnapshot, entry: ConfigEntry) -> float:
    """Monthly prosumer (compensation regime) cost in EUR.

    Only Walloon installations certified before 2024-01-01 are under the
    compensation regime, and only until 2030-12-31. Post-2024 installations
    are on the injection tariff (no per-kVA fee). Returns 0 when:
      - the user has no solar (kVA <= 0),
      - the regime is not 'compensation',
      - the configured DSO has no prosumer rate in the snapshot
        (Flemish digital meters, Cociter SMR3 dynamic).
    """
    kva = _compensation_kva(entry)
    if not kva:
        return 0.0
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    return _prosumer_monthly_fee(overlay, snapshot, kva)


# The first subscription year, counted as a flat 365 days from the start date
# so the span and the daily rate agree: a full year then accrues exactly the
# amount the card printed, whatever leap day it happened to span.
#
# The span a PRO-RATA credit accrues over, and nothing else. When an
# ANNIVERSARY card pays out is its own stated wait, which is not always a
# year: see ``welcome_credit_after_months``.
_WELCOME_YEAR_DAYS = 365


def _months_after(start: date, months: int) -> date:
    """``start`` advanced by whole calendar months.

    The card counts in months ("apres quatorze mois ininterrompus"), so this
    does too rather than multiplying out an average one: fourteen months from
    1 December is 1 February, not 1 February give or take two days. A day that
    the target month does not have (the 31st of a 30-day month) lands on its
    last, which is the only reading that keeps the result inside the month
    the card names.
    """
    total = start.month - 1 + months
    year = start.year + total // 12
    month = total % 12 + 1
    day = min(start.day, monthrange(year, month)[1])
    return date(year, month, day)


def first_year_net_kwh(
    annual_kwh: float,
    window_consumption_kwh: float,
    window_injection_kwh: float,
    *,
    compensation: bool,
) -> float:
    """The first contract year's NET consumption, in kWh.

    What a per-kWh welcome credit multiplies: Mega's ristourne is a reduction
    *"sur le prix de l'energie ... pour votre PREMIERE ANNEE de consommation
    nette d'electricite"*, so the quantity is a year's worth, and net because
    the card says *"nette"*.

    The three windowed callers were passing their own window's net volume
    instead. A year-to-date window is not the contract year and is usually
    shorter, so the per-kWh leg was billed on whatever share of a year had
    elapsed: at the March anniversary of a 3500 kWh Smart Flex entry the
    engine credited 114,03 EUR where the card grants 291,50, and a 20.000 kWh
    one took 326,78 against its own 848,00 ceiling. A pro-rata card is worse
    still, dividing the same partial volume by the days again, though no card
    in the registry is both pro-rata and per-kWh today.

    ``annual_kwh`` is the entry's own yearly volume, resolved by
    :func:`entry_annual_kwh` from a measured full year, then the figure typed
    on the card, then the household default. That cascade already answers
    "how much does this household use in a year" for the excise band, the
    volume tier and the compare page, and annualising the window instead
    would multiply up whatever season it happened to cover.

    "Nette" is about the volume the energy price was actually charged on,
    which is what a reduction "sur le prix de l'energie" can come off, and
    only the COMPENSATION regime bills a netted one: the Walloon reversing
    meter turns back, so the register that is billed already carries the
    export. On the injection regime the household is billed its gross draw
    and credited for what it put back on a separate line, and on no-solar
    there is nothing to net. Taking the export off on all three credited a
    3500 kWh site exporting 2500 on 1000 kWh of ristourne, 123,23 EUR less
    than the card grants it.

    Under compensation the export share is the window's own, because nothing
    else measures one: a site that put back a fifth of what it drew is
    credited on four fifths of its year. A window with no consumption in it
    has no share to take, so the gross year stands.
    """
    if not compensation or window_consumption_kwh <= 0.0:
        return max(annual_kwh, 0.0)
    net_share = (
        max(window_consumption_kwh - window_injection_kwh, 0.0) / window_consumption_kwh
    )
    return max(annual_kwh, 0.0) * net_share


def window_energy_rate(energy_component_eur: float, consumption_kwh: float) -> float:
    """The supplier's energy cost per kWh over a window, as it was billed.

    What a percentage credit is a percentage OF, and what a volume of free
    energy is worth. Derived from the window rather than read off the card on
    purpose: it is already blended across whatever registers, slots or spot
    hours the household actually drew on, so a bi-hourly card needs no
    register weights here and a variable or dynamic one needs no rate lookup.

    Zero when the window drew nothing, which leaves both credits at zero
    rather than dividing by it.
    """
    if consumption_kwh <= 0.0:
        return 0.0
    return max(energy_component_eur, 0.0) / consumption_kwh


def in_first_contract_year(start: date | None, day: date) -> bool:
    """Whether ``day`` falls in the first year from ``start``, the days a
    welcome credit is granted over.

    What a percentage credit's rate is measured on: the campaign is a share
    of what THIS contract charged for energy in its first year, so the
    window's months before the start, or after the year, carry a rate it
    was never a share of.
    """
    if start is None:
        return False
    return start <= day < start + timedelta(days=_WELCOME_YEAR_DAYS)


def grants_a_welcome_credit(snapshot: SupplierSnapshot) -> bool:
    """Whether the card grants a welcome credit at all, in ANY of its shapes.

    Lives beside :func:`_welcome_credit_eur` because it has to agree with it:
    the leaf prices four shapes and the callers that ask this first will skip
    it entirely on a False, so a shape the leaf can price and this cannot see
    is a credit the leaf is never asked for.

    That is exactly what happened twice. The flat half was tested alone until
    a card stating only a per-kWh reduction turned into no credit, and when
    0.27.2 added Luminus's percentage campaign and its kWh cashback the two
    gates still tested the two EUR halves, so every campaign card read as no
    credit on the live sensor, both comparison columns and the projection,
    while the backfill priced it because it asks nothing. A whole feature
    inert on every number a user reads, worth 234,12 EUR a year on a 3500 kWh
    Comfy.

    So this is the one place the question is answered, and a fifth shape field
    belongs in it on the same commit that teaches the leaf to price it. The
    backfill deliberately does not ask: the leaf returns 0.0 for a card that
    grants nothing, which makes the gate a short circuit rather than a rule.
    """
    return bool(
        getattr(snapshot, "welcome_credit_eur", None)
        or getattr(snapshot, "welcome_credit_eur_per_kwh", None)
        or getattr(snapshot, "welcome_credit_pct_of_energy", None)
        or getattr(snapshot, "welcome_credit_kwh", None)
        or getattr(snapshot, "welcome_credit_injection_eur_per_kwh", None)
    )


def _welcome_credit_eur(
    snapshot: SupplierSnapshot,
    start: date | None,
    window_start: date,
    today: date,
    eligible_eur: float,
    first_year_kwh: float = 0.0,
    energy_eur_per_kwh: float = 0.0,
    first_year_injection_kwh: float = 0.0,
) -> float:
    """The one-off welcome credit accrued over ``[window_start, today]``, in EUR.

    A welcome credit is an invoice line rather than a tariff, and the cards
    that grant one say exactly how: *"Dit geldt enkel tijdens je eerste
    inschrijvingsjaar voor deze productversie ... De korting wordt toegekend
    pro rata per dag over de facturatie periode"*. So it needs a contract start
    date, accrues by the day over the first year from it, and stops there on
    its own. ``start`` is that date: the entry's own for the contract the
    household signed, or the day a prospective customer would sign for a card
    being quoted against it. ``None`` means there is no first year to place
    the credit in and this returns 0.0, which is what every entry that sets
    no date keeps doing.

    ``eligible_eur`` is what the WINDOW charged for the three components the
    credit may come off: *"De korting heeft uitsluitend betrekking op de
    energiekost, de vaste vergoeding, de bijdrage groene stroom en WKK ... De
    korting is niet van toepassing op nettarieven, taksen en heffingen"*. The
    energiekost is the supplier's energy component of the consumption alone,
    gross of any feed-in credit: the network and tax legs that make up the
    rest of the all-in rate are excluded by the same sentence, and the
    feed-in is a separate invoice line rather than a reduction of it. The
    cap is prorated onto the credited days, so a running figure can never
    credit more than the same days were charged. It binds only on a very small
    connection: against a 200 EUR credit and a 50 EUR standing charge it needs
    a year under roughly 1.100 kWh.

    A card that grants its credit at the ANNIVERSARY instead (Frank Energie's
    Dynamisch Korting: *"De korting wordt toegekend via de factuur na een jaar
    ononderbroken verbruik"*) is a lump rather than an accrual, so it lands
    whole in the window the wait completes in and nothing before it. Frank's
    card states no ceiling and none is applied; Mega's is the same kind and
    does state one ("plafonne a 848 EUR"), which ``welcome_credit_cap_eur``
    carries and which binds above. The ELIGIBLE-charge cap is the one the
    anniversary shape has no use for, because there is no running figure to
    hold against the days it accrued over.

    A card may state the credit as a reduction on the ENERGY PRICE instead of
    a lump, or as both: Mega's ristourne is *"une reduction de 4.929 c EUR/kWh
    ... sur le prix de l'energie ... pour votre premiere annee de consommation
    nette d'electricite"* plus a flat cut off the standing charge, the whole
    thing *"plafonne a 848 EUR"*. ``first_year_kwh`` is that first year's NET
    consumption, which is what the per-kWh term multiplies and which
    :func:`first_year_net_kwh` resolves; it is a YEAR's volume, not this
    window's, and the accrual below is what places the result in the window.
    ``welcome_credit_cap_eur`` is the card's own ceiling on the total.

    A card may state the credit as a PERCENTAGE of the energy cost instead
    (Luminus Comfy: *"- 33,00 % De remise sur votre consommation annuel en
    heures pleines et creuses pendant 12 mois"*), or as a VOLUME of free
    energy (*"Cashback de 750 kWh apres 12 mois"*). Both need a rate, and
    ``energy_eur_per_kwh`` is the supplier's energy component per kWh over
    this window, as the caller billed it. A percentage then folds into the
    per-kWh leg and a volume into the flat one, so neither opens a path of
    its own and both inherit the cap, the accrual and the VAT basis.
    The ceiling is the card's and applies before ``eligible_eur`` prorates the
    running figure, because it caps the whole credit rather than this window's
    share of it.

    A card may add a bonus on the FEED-IN of the first year: Mega's *"bonus de
    1,06 c EUR/kWh ... pour votre injection sur le reseau de distribution pour
    votre premiere annee de souscription"*. ``first_year_injection_kwh`` is
    that year's export, zero unless the household's feed-in is sold (the
    injection regime) and a year of it has been measured. It joins the amount
    after the ceiling, which the card states for the ristourne alone, and is
    paid when and how the rest is.

    Returns a POSITIVE number; the caller subtracts it.
    """
    amount = snapshot.welcome_credit_eur or 0.0
    per_kwh = snapshot.welcome_credit_eur_per_kwh or 0.0
    # A percentage of the energy cost is a per-kWh credit once it meets a
    # rate, so it joins the leg above rather than opening a second path. The
    # rate is the household's OWN realised one over the window, which is what
    # makes this work on every rate shape: it blends a bi-hourly card by the
    # hours actually drawn instead of needing register weights, and a variable
    # or spot-priced card by what it really billed.
    if snapshot.welcome_credit_pct_of_energy and energy_eur_per_kwh > 0.0:
        per_kwh += snapshot.welcome_credit_pct_of_energy * energy_eur_per_kwh
    if per_kwh and first_year_kwh > 0.0:
        amount += per_kwh * first_year_kwh
    # A volume of free energy, at the rate the card says to value it at. Not
    # scaled by the year: the card grants 750 kWh once, not 750 kWh a year.
    #
    # The four cards granting one name their own rate for it, and it is not
    # the household's blended one: "le prix unitaire en EUR/kWh TTC du cout de
    # l'energie, applicable aux compteurs MONO-HORAIRES tel qu'indique dans
    # les presentes conditions particulieres, par 750 kWh". So it is the
    # card's single rate, and the realised rate is the fallback for a card
    # that publishes none. Valuing it at the blended rate instead short-changed
    # a bi-hourly household by 0,89 to 4,50 EUR and an exclusive-night one by
    # 16,28 to 18,52, and on the two variable cards it also floated with the
    # year where the clause pins the signing card.
    #
    # The percentage leg above keeps the realised rate, because its own
    # sentence names both registers ("en heures pleines et creuses").
    if snapshot.welcome_credit_kwh:
        # getattr for the reason grants_a_welcome_credit uses it: the compare
        # page hands this the card it read the amount off, typed Any, and a
        # caller holding one without rates still has to get the realised-rate
        # fallback rather than an AttributeError out of a fee helper.
        card_energy = getattr(snapshot, "energy", None)
        volume_rate = (
            static_energy_eur_per_kwh(card_energy, "single")
            if card_energy is not None
            else None
        )
        if volume_rate is None or volume_rate <= 0.0:
            volume_rate = energy_eur_per_kwh
        if volume_rate > 0.0:
            amount += snapshot.welcome_credit_kwh * volume_rate
    ceiling = snapshot.welcome_credit_cap_eur
    if ceiling is not None:
        amount = min(amount, ceiling)
    injection_bonus = getattr(snapshot, "welcome_credit_injection_eur_per_kwh", None)
    if injection_bonus and first_year_injection_kwh > 0.0:
        amount += injection_bonus * first_year_injection_kwh
    if amount <= 0.0:
        return 0.0
    if start is None:
        return 0.0
    if snapshot.welcome_credit_kind == WELCOME_CREDIT_ANNIVERSARY:
        # The day the wait the card states completes. Credited in whichever
        # window contains it and in no other, which is what stops a figure
        # that resets every 1 January from granting the same lump a second
        # time.
        anniversary = _months_after(start, snapshot.welcome_credit_after_months)
        if window_start <= anniversary <= today:
            return amount
        return 0.0
    first = max(start, window_start)
    last = min(today, start + timedelta(days=_WELCOME_YEAR_DAYS - 1))
    days = (last - first).days + 1
    if days <= 0:
        return 0.0
    accrued = amount * days / _WELCOME_YEAR_DAYS
    window_days = (today - window_start).days + 1
    if window_days <= 0:
        return 0.0
    cap = max(eligible_eur, 0.0) * days / window_days
    return min(accrued, cap)


def _year_ahead_welcome_credit(
    snapshot: SupplierSnapshot,
    start: date | None,
    today: date,
    eligible_eur: float,
    first_year_kwh: float = 0.0,
    energy_eur_per_kwh: float = 0.0,
    first_year_injection_kwh: float = 0.0,
) -> float:
    """The welcome credit the coming year takes off a bill quoted today, in EUR.

    The window is the 365 days from ``today`` plus the day after them, which
    is the day a card that grants its credit *"na een jaar ononderbroken
    verbruik"* pays it out to a customer who signs today: a strict 365-day
    window would drop that lump by one day and quote the tier as though its
    whole reason for existing were not there.

    And out to the card's OWN anniversary where it states a longer wait, for
    exactly that reason rather than a new one. Four Mega cards pay after
    fourteen months, whose anniversary is 426 days out, so Zen Fixed and its
    pro twin were quoted zero where all fifteen siblings got theirs: 320,65 and
    344,85 EUR withheld, silently, while the page ranked them against cards
    paying at twelve. The extra day was already an admission that the bill's
    window and the card's payout date are different things; a card that says
    fourteen months is owed the same reading as one that says twelve. A pro-rata card accrues its
    full year inside the same window, so a fresh signing is credited the
    printed amount and an existing customer whatever share of the first year
    is still ahead of them; ``eligible_eur`` caps it the way the first-year
    rule does (see :func:`_welcome_credit_eur`).

    ``start`` is ``today`` for a card the household has not signed, which is
    what a quote is, and the entry's own start date for the contract it holds.
    ``first_year_kwh`` already IS a year here, because the bill being quoted
    is an annual one, which is why this one never needed
    :func:`first_year_net_kwh`.
    """
    ends = today + timedelta(days=_WELCOME_YEAR_DAYS)
    wait = snapshot.welcome_credit_after_months
    if (
        snapshot.welcome_credit_kind == WELCOME_CREDIT_ANNIVERSARY
        and wait
        and wait <= MAX_QUOTED_WELCOME_WAIT_MONTHS
        and start is not None
    ):
        ends = max(ends, _months_after(start, wait))
    return _welcome_credit_eur(
        snapshot,
        start,
        today,
        ends,
        eligible_eur,
        first_year_kwh,
        energy_eur_per_kwh,
        first_year_injection_kwh,
    )
