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

"""Annual-cost arithmetic behind the options flow's compare screen.

Split out of ``config_flow.py`` together with ``compare_flow.py``: the two are
one concern cut by size, not two layers. The projection and the coordinator's
yearly volume (``_ensure_annual_volume``) reuse the annual bill and the
measured volume from here.

Deliberately NOT folded into ``pricing.py``. That module is a leaf that
``fees`` and ``energy_meters`` import; the functions here call into both, and
two read the recorder through ``_measured_kwh``, so folding them in would
invert the dependency direction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    MEASURED_FULL_YEAR_DAYS,
    MEASURED_MIN_DAYS,
    MEASURED_YEAR_GAP_DAYS,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_NONE,
)
from .compare_weighting import (
    _tou_weighted_per_kwh,
)
from .energy_meters import MeasuredKwh, _measured_kwh
from .fees import (
    _annual_static_fees,
    _compute_capacity,
    _compute_prosumer,
    _welcome_credit_eur,
    _year_ahead_welcome_credit,
    first_year_net_kwh,
    grants_a_welcome_credit,
    window_energy_rate,
)
from .pricing import renewables_eur_per_kwh, yearly_fixed_fee_for_meter


def _annual_bill(
    snapshot: Any,
    entry: ConfigEntry,
    peak_kw: float,
    per_kwh: float,
    consumption_kwh: float,
    injection_kwh: float = 0.0,
    injection_price: float | None = None,
    export_per_kwh: float | None = None,
    fee_proration: float = 1.0,
    prosumer_proration: float | None = None,
    capacity_proration: float | None = None,
    meter: Any = METER_MONO,
    include_capacity: bool = True,
    welcome_credit_eur: float = 0.0,
    register_weights: tuple[tuple[float, ...], tuple[float, ...]] | None = None,
) -> float:
    """Estimated EUR bill for ``snapshot`` over the period that produced
    ``consumption_kwh`` and ``injection_kwh``.

    ``welcome_credit_eur`` is a one-off credit the period grants, subtracted
    last, after every regime's arithmetic: the card's welcome credit for a
    first subscription year (:func:`fees._year_ahead_welcome_credit`), which
    the caller resolves because only it knows whose first year the period is.

    ``fee_proration`` scales the EUR/year fee components (1.0 for a
    full year, ``days_elapsed/days_in_year`` for YTD). ``prosumer_proration``,
    when given, overrides that for the prosumer term only: the live sensor and
    backfill prorate the prosumer fee per-month (each month's fee by its own
    days), so the YTD what-if passes the same per-month factor there to keep
    its absolute figure equal to the live ``current_year_cost`` sensor.
    ``capacity_proration`` does the same for the Flanders capacity tariff,
    which the live sensor also accrues per month rather than uniformly.

    ``include_capacity`` is forwarded to :func:`_annual_fees`; it exists for
    callers that want the per-kWh and fee terms without the Flanders capacity
    charge, and both the annual estimate and the YTD what-if keep it on so
    they match the live ``current_year_cost`` sensor.

    Solar handling honours the entry's configured regime:

    - ``"none"``: ``cost = consumption_kwh * per_kwh + fees``
    - ``"compensation"``: meter is netted 1:1 (Walloon pre-2024
      installations until 2030); surplus injection is forfeited, never paid
      out, so the year is clamped at zero. With ``export_per_kwh``, the all-in
      rate weighted by the EXPORT shape, each side is priced on its own shape;
      without it the annual totals are netted first and the residue takes
      ``per_kwh``, which is what every quote did before. Fees include the
      prosumer charge.

      Matching the live sensor also needs ``register_weights``, and that is a
      separate condition this used to fold into the one above: the sensor
      forfeits each register on its own, so a quote given the export shape and
      no weights still clamps the year once and lets one register pay off
      another. ``test_compensation_clamps_each_register_not_the_annual_total``
      carries the measurement.
    - ``"injection"``: consumption is billed at ``per_kwh`` AND
      injection is credited at ``injection_price``; the credit is
      subtracted from the cost and can drive the bill negative when
      injection income exceeds consumption + fees.
    """
    fees = (
        _annual_fees(snapshot, entry, peak_kw, meter, include_capacity) * fee_proration
    )
    if prosumer_proration is not None:
        # _annual_fees prorated the prosumer term uniformly by fee_proration;
        # swap in the per-month proration the live sensor and backfill use so
        # the YTD absolute matches. The delta is zero for a non-compensation
        # entry (prosumer fee is 0 there).
        #
        # ``prosumer_proration`` counts MONTHS (0..12) while ``fee_proration``
        # is a fraction of a year (0..1), so it has to be divided by 12 before
        # the two can be subtracted. Without that the correction multiplied an
        # already-annual fee by a month count and quoted the prosumer term 12x
        # over on every date. No test caught it because the options-flow stub
        # DSO publishes no prosumer rate, which zeroes the whole term.
        prosumer_annual = 12.0 * _compute_prosumer(snapshot, entry)
        fees += prosumer_annual * (prosumer_proration / 12.0 - fee_proration)
    if capacity_proration is not None and include_capacity:
        # Same correction for the Flanders capacity tariff, and for the same
        # reason: _ytd_capacity accrues each month by its OWN length
        # (days_in_ytd / days_in_full_month), while fee_proration is a uniform
        # days_elapsed / days_in_year. The two drift inside the year: a
        # February close measured 36,37 against the live sensor's 37,50, and
        # the what-if is meant to be comparable to that sensor to the cent.
        # Counted in MONTHS (0..12), like prosumer_proration.
        #
        # The SAME figure _annual_fees prorated, ceiling included. Recomputing
        # it uncapped left the correction and the term it corrects on two
        # different numbers, so they no longer cancelled and a capped entry
        # was quoted about half its capacity leg.
        if entry.data.get(CONF_REGION) == REGION_FLANDERS:
            capacity_annual = 12.0 * _compute_capacity(snapshot, entry, peak_kw, meter)
            fees += capacity_annual * (capacity_proration / 12.0 - fee_proration)
    regime = entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    if regime == "compensation":
        if export_per_kwh is None:
            billable = max(consumption_kwh - injection_kwh, 0.0)
            return fees + per_kwh * billable - welcome_credit_eur
        # A reversing meter nets against the rate in force at the time, which
        # is what the live sensor bills. Netting the two annual totals first
        # and pricing the residue at the CONSUMPTION-weighted rate prices
        # exported kWh at hours they were never produced in, and the two
        # shapes are opposites, evening-heavy against a midday bell. So the
        # term is split, consumption at its own weighted rate and export
        # credited at its own.
        #
        # The clamp goes PER REGISTER, which is what the meter does and what
        # ``spot_stats._NetAllocation.billed`` bills: each register nets on its
        # own and a register that ends the year negative is forfeited, not
        # carried across to shelter the other one. Clamping the annual total
        # once instead let a day register running backwards pay off the night
        # register, which no Belgian meter does: measured at 345 EUR a year
        # too low on an ordinary pre-2024 net-metering install.
        #
        # ``register_weights`` is ((day, night) of consumption, (day, night) of
        # export) as :func:`_register_weights` returns them, which is hours a
        # year or those hours under the household's own shape, NOT shares: they
        # are normalised here so the caller can pass the helper's output
        # straight through. Without them, or on a single-register meter, the
        # two clamps are the same sum and the annual one stands.
        # Whatever registers the meter has, which _register_weights now
        # decides the same way _register_for does. Gating on the meter here
        # instead let a mono Impact entry (TotalEnergies Impact is one) take
        # the annual clamp, where a band running backwards pays off another.
        if register_weights is not None and len(register_weights[0]) > 1:
            cons_w, inj_w = register_weights
            cons_total = sum(cons_w)
            inj_total = sum(inj_w)
            if cons_total > 0.0 and inj_total > 0.0:
                billed = 0.0
                for cons_side, inj_side in zip(cons_w, inj_w, strict=True):
                    billed += max(
                        consumption_kwh * (cons_side / cons_total) * per_kwh
                        - injection_kwh * (inj_side / inj_total) * export_per_kwh,
                        0.0,
                    )
                return fees + billed - welcome_credit_eur
        netted = consumption_kwh * per_kwh - injection_kwh * export_per_kwh
        return fees + max(netted, 0.0) - welcome_credit_eur
    if regime == "injection" and injection_price is not None:
        return (
            fees
            + per_kwh * consumption_kwh
            - injection_price * injection_kwh
            - welcome_credit_eur
        )
    return fees + per_kwh * consumption_kwh - welcome_credit_eur


def _annual_fees(
    snapshot: Any,
    entry: ConfigEntry,
    peak_kw: float,
    meter: Any,
    include_capacity: bool = True,
) -> float:
    """Just the EUR/year fee components (no per-kWh term).

    Pulled out so the YTD comparison can pro-rate fees by the elapsed
    fraction of the year without re-computing the per-kWh part. ``meter``
    selects the supplier yearly fixed fee, so an exclusive-night meter
    gets its dedicated fee (EBEM) rather than the standard one.

    ``include_capacity`` can exclude the Flanders capacity tariff. It is on
    everywhere today: the live ``current_year_cost`` sensor accrues capacity
    through ``_ytd_capacity``, so a what-if that dropped it would quote a
    lower bill than the sensor it sits next to.

    The VREG ceiling is applied by ``_compute_capacity`` itself, against the
    year the entry states. It used to be applied here instead, against the
    volume the CALLER happened to hold, and the YTD what-if holds the window's
    kWh rather than the year's: in the early months that measured a full
    year's capacity charge against a quarter of a year's consumption, so the
    cap bit where it does not belong and the page quoted 11 to 18 EUR under
    the sensor it is meant to match, on an ordinary 3 500 kWh household."""
    static = _annual_static_fees(snapshot, meter, entry)
    capacity = 0.0
    if include_capacity and entry.data.get(CONF_REGION) == REGION_FLANDERS:
        capacity = 12.0 * _compute_capacity(snapshot, entry, peak_kw, meter)
    prosumer = 12.0 * _compute_prosumer(snapshot, entry)
    return static + capacity + prosumer


def _grants_a_welcome_credit(snapshot: Any) -> bool:
    """Whether the card grants a credit at all, in any of its shapes.

    Delegates rather than restating the test: this file spelled out the two
    EUR halves, and 0.27.2's percentage campaign and kWh cashback set neither,
    so both comparison columns and the projection quoted every campaign card
    at no credit. :func:`fees.grants_a_welcome_credit` is the one place that
    question is answered now, beside the leaf that has to agree with it.
    """
    return grants_a_welcome_credit(snapshot)


def _ytd_welcome_credit(
    snapshot: Any,
    credited: Any,
    start: date | None,
    when_now: datetime,
    dso: str,
    region: str,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
    hour_weights: dict[int, float] | None,
    consumption_kwh: float,
    injection_kwh: float = 0.0,
    *,
    annual_kwh: float,
    regime: str,
    window_start: date,
    fee_proration: float,
) -> float:
    """The welcome credit the year-to-date window has already accrued.

    The window-scoped sibling of :func:`_annual_welcome_credit`, for the simple
    model the compare page falls back to when the archive engine throws. That
    path carried no credit at all, so a row priced by the engine included one
    and the same row priced by the fallback did not, next to annual figures
    that always do.

    Same eligible base as the annual helper, over the window rather than the
    year: the standing charge is prorated the way the bill beside it prorates
    it, since the cap is what these days were actually charged.

    The per-kWh term is the exception and rides ``annual_kwh``, because the
    card measures it over the first contract YEAR whatever window is being
    shown. Passing the window's own volume here made this column disagree
    with the annual one beside it by most of the credit.
    """
    if not _grants_a_welcome_credit(credited):
        return 0.0
    energy_per_kwh = _tou_weighted_per_kwh(
        snapshot,
        dso,
        region,
        when_now,
        spot,
        meter,
        dso_mode,
        hour_weights,
        component="energy",
    )
    if energy_per_kwh is None:
        return 0.0
    eligible = (
        consumption_kwh * energy_per_kwh
        + float(yearly_fixed_fee_for_meter(snapshot.energy, meter) or 0.0)
        * fee_proration
        + consumption_kwh * renewables_eur_per_kwh(snapshot.taxes, region)
    )
    # "consommation nette d'electricite", which is the volume the energy
    # price was charged on: netted only where the meter nets it.
    return _welcome_credit_eur(
        credited,
        start,
        window_start,
        when_now.date(),
        eligible,
        first_year_net_kwh(
            annual_kwh,
            consumption_kwh,
            injection_kwh,
            compensation=regime == SOLAR_REGIME_COMPENSATION,
        ),
        window_energy_rate(consumption_kwh * energy_per_kwh, consumption_kwh),
    )


def _annual_welcome_credit(
    snapshot: Any,
    credited: Any,
    start: date | None,
    when_now: datetime,
    dso: str,
    region: str,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
    hour_weights: dict[int, float] | None,
    consumption_kwh: float,
    injection_kwh: float = 0.0,
    *,
    regime: str,
) -> float:
    """The welcome credit the coming year takes off ``snapshot``'s annual quote.

    ``credited`` is the card the amount and its rule are read off: the signing
    month's card for the household's own contract, and the card itself for a
    candidate, since a new customer signing today is granted what today's card
    prints. ``start`` is the entry's own start date for the own contract and
    the quote date for a candidate (see :func:`fees._year_ahead_welcome_credit`
    for the window). A card that grants nothing costs no walk at all.

    The cap is measured against what the year would charge for the supplier's
    energy component alone, the standing charge and the green contribution,
    so the energy leg is re-walked on its ``energy`` component with the same
    weights the all-in rate carries.
    """
    if not _grants_a_welcome_credit(credited):
        return 0.0
    energy_per_kwh = _tou_weighted_per_kwh(
        snapshot,
        dso,
        region,
        when_now,
        spot,
        meter,
        dso_mode,
        hour_weights,
        component="energy",
    )
    if energy_per_kwh is None:
        return 0.0
    eligible = (
        consumption_kwh * energy_per_kwh
        + float(yearly_fixed_fee_for_meter(snapshot.energy, meter) or 0.0)
        + consumption_kwh * renewables_eur_per_kwh(snapshot.taxes, region)
    )
    return _year_ahead_welcome_credit(
        credited,
        start,
        when_now.date(),
        eligible,
        # Netted only where the meter nets it. This open-coded the subtraction
        # and took the export off on every regime, so the rule that reached
        # the three windowed callers through first_year_net_kwh never arrived
        # here: a 3500 kWh site exporting 2500 was quoted on 1000 kWh of
        # ristourne, 259,70 EUR a year under what the card grants it, on the
        # projection sensor and every comparison row while the accrued sensor
        # beside them was right. The window IS the year here, so the annual
        # volume is passed as both.
        first_year_net_kwh(
            consumption_kwh,
            consumption_kwh,
            injection_kwh,
            compensation=regime == SOLAR_REGIME_COMPENSATION,
        ),
        window_energy_rate(consumption_kwh * energy_per_kwh, consumption_kwh),
        # The year's export, which a first-year feed-in bonus multiplies, and
        # only where it is sold: under compensation it nets against the draw.
        first_year_injection_kwh=(
            injection_kwh if regime == SOLAR_REGIME_INJECTION else 0.0
        ),
    )


async def _read_total_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
) -> float | None:
    """Sum of consumption (or injection) kWh between ``start`` and ``end``
    from the entry's configured kWh sensors.

    Thin wrapper over :func:`energy_meters._measured_kwh` so there is one
    recorder-read shape rather than two that can drift. Returns ``None`` for a
    total of zero or less, which is what the year-to-date and injection call
    sites treat as "nothing to bill". That conflates "no sensor wired" with
    "wired and reads zero"; callers that need to tell those apart go through
    :func:`_annual_volume` instead, which carries the coverage."""
    measured = await _measured_kwh(hass, entry, start, end, side=side)
    return measured.kwh if measured.kwh > 0 else None


def _covers_a_year(days_with_data: int) -> bool:
    """Whether a metered window counts as a full year.

    Not an equality test against 365. Recorder coverage is routinely a day or
    two short for reasons that say nothing about the meter, and every consumer
    of this predicate treats "a full year" as a mode switch, so an exact test
    turns a missing bucket into a cliff rather than a rounding error.
    """
    return days_with_data >= MEASURED_FULL_YEAR_DAYS - MEASURED_YEAR_GAP_DAYS


@dataclass(frozen=True)
class _AnnualVolume:
    """A yearly kWh figure with the days of history behind it and a label
    saying where it came from, for display next to the quote."""

    kwh: float
    days_with_data: int
    source: str
    # Whether the METER produced this figure, rather than the entry's typed
    # estimate or the household default. Stated here rather than re-derived
    # from the coverage: a wired meter reading zero clears both measured
    # bands while still carrying a full year of days, so a day count alone
    # cannot tell the two apart. The coordinator keys off this to decide
    # whether it has a volume worth pricing the tranche and the network
    # ceiling against.
    measured: bool = False
    # The day/night register the figure is short of, when the pair could only
    # be measured in part (``MeasuredKwh.pair_fault``); what the coordinator
    # raises a Repairs card over.
    pair_fault: str = ""


async def _annual_volume(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
) -> _AnnualVolume:
    """Yearly CONSUMPTION kWh, normalised from whatever the recorder covers.

    A quote needs a full year of volume, and the window it is handed rarely is
    one. Three bands, because the honest answer differs:

    - a window covering a full year is used as it stands;
    - a shorter one down to ``MEASURED_MIN_DAYS`` is scaled up to a year and
      SAID to be scaled, since it carries whichever season it covered;
    - below that the measurement is refused. Six weeks of winter scaled by 8,7
      is a worse annual figure than the household default, and presenting a
      six-week sum as a year (which is what this used to do) understates the
      bill by roughly the same factor.

    A volume typed on the entry, which only professional entries carry, sits
    between the two measured bands: a full year of meter beats it, a scaled
    quarter does not. That is the order ``entry_annual_kwh`` resolves the
    excise band, the network ceiling and a volume tranche in, and the two
    have to agree or the compare page prices a card on one volume and
    multiplies by another: a 30.000 kWh business whose 90 days scaled to
    52.000 had its rates resolved on the stated figure and its rows on the
    extrapolated one. Below everything, the household default.

    Consumption only, deliberately. The injection leg looked like it wanted the
    same treatment and is harmed by it in both bands: refusing a short window
    discards a real feed-in measurement (and ``_solar_note`` reads the
    resulting zero as "no injection sensor wired" while the same page prints
    the YTD injected kWh), while scaling a longer one by a bare day count
    ignores that PV output is far more seasonal than consumption, which can
    over-credit enough to drive the compensation net to its zero clamp. That
    leg needs a production profile, not a day count, so it stays on the raw
    window sum through :func:`_read_total_kwh`.

    Reads only ``entry.data``, so it stays usable with the compare flow's
    ``_QuoteEntry`` proxy.
    """
    measured = await _measured_kwh(hass, entry, start, end)
    return replace(_volume_of(measured, entry), pair_fault=measured.pair_fault)


def _volume_of(measured: MeasuredKwh, entry: ConfigEntry) -> _AnnualVolume:
    """The three bands of :func:`_annual_volume`, from a measurement in hand."""
    days = measured.days_with_data
    if measured.kwh > 0 and _covers_a_year(days):
        # Scaled across whatever few days are missing. At this coverage the
        # correction is under 5% and carries no seasonal bias worth the name.
        kwh = measured.kwh * MEASURED_FULL_YEAR_DAYS / days
        return _AnnualVolume(kwh, days, f"measured ({days} days)", measured=True)
    typed = entry.data.get(CONF_ANNUAL_CONSUMPTION_KWH)
    if typed:
        return _AnnualVolume(
            float(typed), days, f"entered on the entry ({float(typed):.0f} kWh/year)"
        )
    if measured.kwh > 0 and days >= MEASURED_MIN_DAYS:
        return _AnnualVolume(
            measured.kwh * MEASURED_FULL_YEAR_DAYS / days,
            days,
            f"scaled from {days} days, not seasonally corrected",
            measured=True,
        )
    if days:
        return _AnnualVolume(
            DEFAULT_ANNUAL_CONSUMPTION_KWH,
            days,
            f"default {DEFAULT_ANNUAL_CONSUMPTION_KWH:.0f} kWh"
            f" - only {days} day{'' if days == 1 else 's'} of history",
        )
    return _AnnualVolume(
        DEFAULT_ANNUAL_CONSUMPTION_KWH,
        0,
        f"default {DEFAULT_ANNUAL_CONSUMPTION_KWH:.0f} kWh"
        " - wire a kWh sensor for a measured estimate",
    )
