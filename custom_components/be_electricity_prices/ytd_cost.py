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

"""The year-to-date cost engine.

Split out of coordinator.py. Walks the year month by month, pricing each with
that month's own archived card, and sums the energy, the standing charges, the
capacity tariff, the prosumer forfait and the feed-in credit into the running
bill the current_year_cost sensor publishes.

The same figure is built by two other paths -- backfill.py per hour and the
options flow's compare quote -- so a change here that is not mirrored there
shows up as a seam, not an exception."""

from __future__ import annotations

import logging

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import date
from datetime import datetime
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp
import calendar

from .cohort import (
    _cohort_energy_leg,
    _effective_snapshot_for_month,
    _month_snapshot_cache,
)
from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .energy_meters import (
    _hourly_consumption_sensors,
    _hourly_injection_sensors,
    _partial_register_pair,
    _resolve_daily_kwh,
    _sum_hourly_kwh,
    _top_up_today_hourly,
)
from .fees import (
    _annual_static_fees,
    _capacity_monthly_eur,
    _compensation_kva,
    _prosumer_monthly_fee,
)
from .injection import (
    _historical_injection_rate,
    _injection_hourly_on_cohort,
)
from .pricing import (
    MeterType,
    compute_breakdown,
    compute_network_and_taxes,
    static_breakdown,
)
from .providers.base import (
    DynamicRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TimeOfUseRates,
)
from .spot_stats import (
    _bucket_by_local_month,
    _injection_is_spp_indexed,
    _covered_month_mean,
    _injection_on_month_mean,
    _spp_injection_spot,
)
from .synergrid import (
    SppWeights,
)


_LOGGER = logging.getLogger(__name__)


def _days_through(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end`` (local calendar)."""
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


async def _walk_ytd_months(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
) -> AsyncIterator[tuple[SupplierSnapshot, date, int, int]]:
    """Yield ``(snap_m, month_first, days_in_full_month, days_in_ytd)``
    for each month from Jan 1 of today's year up through today.

    Centralises the per-month walk shared by every YTD accumulator so
    the proration formula and the per-month archive lookup stay in one
    place. ``snap_m`` falls back to the current snapshot for months
    with no archive (see :func:`_snapshot_for_month`).

    ``contract`` overrides the entry's stored contract id; the
    OptionsFlow compare path uses this to walk months for an
    alternative supplier without mutating the live entry.
    """
    region = entry.data.get(CONF_REGION, "")
    contract = contract or entry.data[CONF_CONTRACT]
    cur = date(today.year, 1, 1)
    while cur <= today:
        month_first = date(cur.year, cur.month, 1)
        snap_m = await _effective_snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot, entry
        )
        if cur.month == 12:
            next_first = date(cur.year + 1, 1, 1)
        else:
            next_first = date(cur.year, cur.month + 1, 1)
        days_in_full_month = (next_first - month_first).days
        month_end_in_ytd = min(next_first - timedelta(days=1), today)
        days_in_ytd = (month_end_in_ytd - cur).days + 1
        yield snap_m, month_first, days_in_full_month, days_in_ytd
        cur = next_first


async def _ytd_static_fees(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
    meter: MeterType | None = None,
) -> float:
    """Pro-rated YTD total of yearly_fixed_fee + 12*energy_fund using each
    month's archived snapshot.

    ``meter`` defaults to the entry's meter; the compare flow passes a
    meter override so the fixed fee is billed at the same meter the energy
    is billed at (e.g. an exclusive-night override).

    Uses the uniform days_in_year proration but reads the rate from the
    archived snapshot for each past month, so a supplier indexation
    that lands mid-year is honoured for the months it applies to.
    Falls back to the current snapshot for months with no archive.
    """
    days_in_year = 366 if calendar.isleap(today.year) else 365
    total = 0.0
    async for snap_m, _, _, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        annual = _annual_static_fees(
            snap_m, meter or entry.data.get(CONF_METER, METER_MONO), entry
        )
        total += annual * (days_in_ytd / days_in_year)
    return total


async def _ytd_prosumer(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
) -> float:
    """Sum the monthly prosumer fee across YTD using each month's archived
    snapshot's DSO overlay, so a CWaPE indexation that lands mid-year is
    honoured for the months it applies to."""
    kva = _compensation_kva(entry)
    if not kva:
        return 0.0
    dso = entry.data.get(CONF_DSO, "")

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        overlay = snap_m.dsos.get(dso)
        monthly_fee = _prosumer_monthly_fee(overlay, snap_m, kva)
        if monthly_fee == 0.0:
            continue
        total += monthly_fee * (days_in_ytd / days_in_full_month)
    return total


async def _ytd_capacity(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    billed_peak_kw: float,
    *,
    contract: str | None = None,
) -> float:
    """Sum the monthly Flemish capacity charge across YTD, reading each
    month's archived DSO overlay so a VREG indexation landing mid-year is
    honoured for the months it applies to.

    ``billed_peak_kw`` is the CURRENT gemiddelde maandpiek, applied to every
    month of the year rather than reconstructed per month. Reconstruction is
    not available in general: the rolling window holds at most twelve months
    and an entry installed mid-year has no history for the months before it,
    where Fluvius billed against meter history we never saw. The current mean
    is the honest stand-in precisely because it is a twelve-month mean, so it
    moves slowly and is close to what each month of this year was billed on.
    """
    if entry.data.get(CONF_REGION) != REGION_FLANDERS:
        return 0.0
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        monthly = _capacity_monthly_eur(snap_m.dsos.get(dso), billed_peak_kw)
        total += monthly * (days_in_ytd / days_in_full_month)
    return total


async def _ytd_hourly_energy(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
    meter: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    spot_quarters: dict[datetime, list[float]] | None = None,
    monthly_mean: bool = False,
    spp_weights: SppWeights | None = None,
    breakdown: dict[str, float] | None = None,
) -> float | None:
    """YTD energy cost for hourly-billed contracts (TOU + dynamic).

    Bins the recorder's hourly kWh deltas through ``compute_breakdown``
    at each local hour, picking up the TOU slot rate (or the dynamic
    factor*spot+base) from the supplier and the bi-hourly / Impact
    distribution band from the user's DSO mode in one call. Reads from
    ``CONF_CONSUMPTION_KWH`` (single totals) when available, else sums
    the four day/night register sensors at hourly granularity. Each
    side -- consumption, injection -- is resolved independently,
    mirroring the static-path behaviour: a user with only injection
    wired (e.g. an inverter exposing solar export but no smart-meter
    consumption sensor) still gets the injection credit recognised.

    ``historical_spots`` is required for dynamic contracts (factor*spot+
    base needs a spot per hour). An hour the cache cannot price is NOT
    dropped: its network and tax legs are known from the month's snapshot
    regardless of the day-ahead price, so it bills those and loses the
    energy term alone. A partial backfill therefore understates the YTD
    by the commodity it could not price, rather than by the whole hour.
    TOU callers pass ``None`` and every hour gets billed at the slot rate.

    Quarter-hourly dynamic contracts (Engie, Cociter, EBEM, Ecofix,
    OCTA+, Ecopower DBS) bill the live price on 15-minute slots, but the
    recorder only retains hourly long-term statistics, so this YTD replay
    aggregates consumption / injection to the clock hour and prices each
    hour at its hourly spot. When intra-hour load correlates with the
    intra-hour price the YTD total is a close approximation, not a
    bit-exact reconciliation with the live 15-minute sensor.

    Solar handling is uniform across both paths:
      - ``compensation``: per-hour ``(cons - inj) * all_in``, summed
        and clamped at zero (Walloon meter forfeits surplus).
      - ``injection``: per-hour ``cons * all_in - inj * inj_rate``
        where ``inj_rate`` is the supplier's monthly indicative for TOU
        and ``factor*spot+base`` for dynamic at that hour's spot.
      - ``none``: per-hour ``cons * all_in``.

    Returns ``None`` only when neither side has any meters wired (the
    caller surfaces the fees-only floor).
    """
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    contract = contract or entry.data[CONF_CONTRACT]
    meter = meter or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    if _partial_register_pair(entry, "consumption") or _partial_register_pair(
        entry, "injection"
    ):
        # Same rule the static per-day path applies: a half-wired pair means
        # the missing band's kWh are unavailable, so bill nothing rather than
        # bill the wired half. Without this the empty side vanished silently
        # and any wired injection was credited against zero consumption.
        return None
    cons_ids = _hourly_consumption_sensors(entry)
    inj_ids = _hourly_injection_sensors(entry)
    if not cons_ids and not inj_ids:
        return None

    jan1 = date(today.year, 1, 1)
    cons_per_hour = await _sum_hourly_kwh(hass, cons_ids, jan1, today)
    inj_per_hour = await _sum_hourly_kwh(hass, inj_ids, jan1, today)
    # Statistics only carry the last COMPILED hour, so top today up from the
    # live meters the way the per-day branch has since 0.11.9. Without this
    # every hourly-billed contract stepped once an hour at best and froze
    # outright whenever compilation lagged or stalled.
    await _top_up_today_hourly(hass, cons_ids, cons_per_hour, today)
    await _top_up_today_hourly(hass, inj_ids, inj_per_hour, today)

    _snap_for = _month_snapshot_cache(
        hass, session, extractor, contract, region, snapshot, entry
    )

    # Spot-monthly contracts bill every hour of a delivery month at that
    # month's mean spot (energy and mean-indexed injection alike); cache the
    # mean per month so it's computed once.
    month_means: dict[tuple[int, int], float | None] = {}
    # SPP-weighted per-month injection means, when the entry opted in. Energy
    # keeps the flat mean above; only the injection credit uses these.
    month_spp: dict[tuple[int, int], float | None] = {}
    # Bucket the year's spots by local month once so each month's mean is a
    # lookup rather than a full-year rescan (the loop reads up to twelve
    # distinct months). Only the spot-monthly path reads it; a dynamic
    # contract prices per hour, so skip the bucketing there entirely.
    month_bucket = (
        _bucket_by_local_month(historical_spots)
        if monthly_mean and historical_spots
        else {}
    )
    # A static card whose injection is a per-hour spot formula with no printed
    # indicative (Cociter Tarif Variable) keeps that hourly index even on the
    # monthly-mean path, which it reaches only via a signing-cohort re-price of
    # the ENERGY leg. Same gate the live tick applies before baking.
    hourly_injection = monthly_mean and _injection_hourly_on_cohort(snapshot, entry)
    # The hour's own 15-minute spots, for the one feed-in formula that is not
    # linear in the spot and so is not priced by their mean (see
    # _injection_needs_spot_quarters). Empty for every other entry, which is
    # what makes reading them a no-op there. A credit that settles on a month
    # mean is deliberately excluded: a mean of means says nothing about what
    # one hour's quarters did.
    quarters: dict[datetime, list[float]] = (
        spot_quarters or {} if hourly_injection or not monthly_mean else {}
    )

    energy_cost = 0.0
    # How much of the window actually got an energy price. A YTD that is low
    # because the spot cache is thin looks identical to a low one that is
    # correct, so report the coverage instead of leaving the user to guess.
    hours_seen = 0
    hours_priced = 0
    # Iterate the union of both sides so an injection-only wiring
    # still contributes its credit (mirroring _resolve_daily_kwh).
    for utc_hour in cons_per_hour.keys() | inj_per_hour.keys():
        hours_seen += 1
        local = dt_util.as_local(utc_hour)
        spot: float | None = None
        # Distinguishes "this contract needs no spot" (TOU, Impact,
        # exclusive-night: neither branch below runs) from "it needs one and
        # the cache has none", which are billed differently.
        spot_missing = False
        if monthly_mean:
            key = (local.year, local.month)
            if key not in month_means:
                # Gated on coverage: a closed month cached too thinly would
                # otherwise price every one of its hours off an
                # unrepresentative handful.
                month_means[key] = _covered_month_mean(month_bucket, *key, today)
            spot = month_means[key]
            spot_missing = spot is None
        elif historical_spots is not None:
            spot = historical_spots.get(utc_hour)
            spot_missing = spot is None
        snap_h = await _snap_for(date(local.year, local.month, 1))
        try:
            if spot_missing:
                # No spot for this hour. Bill the two legs that do not depend
                # on one instead of dropping the hour whole; the energy term is
                # the only part actually unknown.
                bd = compute_network_and_taxes(
                    snap_h, dso, region, local, meter, dso_mode
                )
            else:
                bd = compute_breakdown(
                    snap_h, dso, region, local, spot, meter, dso_mode
                )
                hours_priced += 1
        except (KeyError, ValueError):
            # Missing DSO row or non-static rate kind: skip this hour.
            continue
        kwh_cons = cons_per_hour.get(utc_hour, 0.0)
        kwh_inj = inj_per_hour.get(utc_hour, 0.0)
        if regime == SOLAR_REGIME_COMPENSATION:
            d_cost = (kwh_cons - kwh_inj) * bd.all_in
        elif regime == SOLAR_REGIME_INJECTION:
            d_cost = kwh_cons * bd.all_in
            # Energy bills at the flat month-mean (spot); the injection credit
            # uses the SPP-weighted month-mean when the entry opted in, falling
            # back to the flat mean when the profile is missing for the month
            # - unless the CARD indexes on Belpex_SPP, where the flat mean is
            # a different index rather than a coarser one and the card's own
            # indicative is credited instead.
            inj_spot = _spp_injection_spot(
                spot,
                monthly_mean=monthly_mean,
                strict=_injection_is_spp_indexed(snap_h),
                spp_weights=spp_weights,
                bucket=month_bucket,
                year=local.year,
                month=local.month,
                today=today,
                cache=month_spp,
                hourly=hourly_injection,
                hourly_spot=(
                    historical_spots.get(utc_hour)
                    if historical_spots is not None
                    else None
                ),
            )
            inj_rate = _historical_injection_rate(
                snap_h.injection,
                inj_spot,
                quarters=quarters.get(utc_hour),
                energy=snap_h.energy,
                when=local,
            )
            if inj_rate is not None:
                d_cost -= kwh_inj * inj_rate
        else:
            d_cost = kwh_cons * bd.all_in
        energy_cost += d_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        energy_cost = max(energy_cost, 0.0)
    if breakdown is not None:
        breakdown["hours_seen"] = float(hours_seen)
        breakdown["hours_priced"] = float(hours_priced)
        # And what the window SHOULD hold. hours_seen counts only the buckets
        # the recorder returned, so it shrinks with a gap and hours_priced
        # shrinks with it: the pair reads a confident 100% while hundreds of
        # hours are missing entirely. Comparing against elapsed is the only
        # way that failure is visible from the sensor.
        elapsed = dt_util.now() - dt_util.start_of_local_day(date(today.year, 1, 1))
        breakdown["hours_elapsed"] = float(int(elapsed.total_seconds() // 3600))
        breakdown["consumption_ytd_kwh"] = sum(cons_per_hour.values())
        breakdown["injection_ytd_kwh"] = sum(inj_per_hour.values())
    return energy_cost


async def _ytd_spot_injection_credit(
    hass: HomeAssistant,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    historical_spots: dict[datetime, float] | None,
    snap_for: Callable[[date], Awaitable[SupplierSnapshot]] | None = None,
) -> float:
    """YTD solar-injection credit (EUR) for a contract whose injection is
    a per-hour spot formula with no monthly indicative.

    Sums per-hour injected kWh * (factor*spot + base) from the recorder's
    hourly statistics and the persistent historical-spot cache, for
    Cociter Variable -- a static-energy card that publishes an hourly
    BELPEX injection formula but no fixed credit. The static per-day YTD
    path can't price these (no spot per
    day), so this isolated term replays the spots the same way the
    dynamic energy path does, and the caller subtracts it from the bill.

    Returns 0.0 (a no-op) unless the injection is exactly that shape
    (``factor``/``base`` set, ``current is None``), spots are cached, and
    an injection sensor is wired. Hours with no cached spot are skipped.

    ``snap_for`` resolves each hour to its own delivery month's card, the way
    the sibling walks and the backfill already do. Without it every past hour
    was credited at TODAY's coefficients, so a contract whose feed-in formula
    moved during the year was re-credited for the whole year at its newest
    terms. An hour whose month printed an indicative is skipped here, because
    the walk this term is added to already credited that month off it, and
    crediting it twice would double the feed-in.
    """
    inj = snapshot.injection
    if (
        inj is None
        or inj.factor is None
        or inj.base is None
        or inj.current is not None
        or not historical_spots
    ):
        return 0.0
    inj_ids = _hourly_injection_sensors(entry)
    if not inj_ids:
        return 0.0
    jan1 = date(today.year, 1, 1)
    per_hour = await _sum_hourly_kwh(hass, inj_ids, jan1, today)
    # Topped up from the live meter, exactly as both sibling paths do: the
    # daily branch through _recorder_daily_kwh and the hourly branch through
    # its own two _top_up_today_hourly calls. Without it the consumption leg
    # of one bill was live to the minute while its offsetting feed-in credit
    # trailed the last COMPILED hour, so current_year_cost over-stated the
    # bill by whatever of today's injection statistics had not booked yet, and
    # did not heal at all while compilation was stalled.
    await _top_up_today_hourly(hass, inj_ids, per_hour, today)
    credit = 0.0
    for utc_hour, kwh in per_hour.items():
        spot = historical_spots.get(utc_hour)
        if spot is None:
            continue
        inj_h: InjectionRates | None = inj
        if snap_for is not None:
            local = dt_util.as_local(utc_hour)
            inj_h = (await snap_for(date(local.year, local.month, 1))).injection
            if (
                inj_h is None
                or inj_h.factor is None
                or inj_h.base is None
                or inj_h.current is not None
            ):
                # That month is not this shape, so its own card was already
                # credited by the walk this term is added to.
                continue
        # Route through the shared helper so the floor_at_zero clamp the live
        # scalar and array apply is honoured here too, rather than summing the
        # raw factor*spot+base and diverging on a negative-spot hour.
        #
        # No quarters here, and none can exist: this term serves a card whose
        # ENERGY leg is static, and only DynamicRates carries quarter_hourly,
        # so the hour's spot IS the hour's price.
        credit += kwh * (_historical_injection_rate(inj_h, spot) or 0.0)
    return credit


async def _compute_current_year_cost(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    contract_override: str | None = None,
    meter_override: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    spot_quarters: dict[datetime, list[float]] | None = None,
    spp_weights: SppWeights | None = None,
    breakdown: dict[str, float] | None = None,
    billed_peak_kw: float = 0.0,
) -> float | None:
    """Time-correct yearly bill from HA recorder + per-month tariff cards.

    For every day from Jan 1 of the current local year up to today,
    pull that day's kWh from the recorder and multiply by the tariff
    of the month the day belongs to (archived snapshot when the
    supplier exposes one, else the current snapshot as a proxy).
    Per-day kWh × per-day tariff handles tariff transitions inside a
    month (e.g. the supplier rotates a monthly card mid-month) without
    re-querying the recorder, and matches what the user reads on a
    smart meter day by day.

    Math per day, after looking up the snapshot for that day's month:

      regime=none, mono : (d_cons + n_cons) * single
      regime=none, bi   : d_cons * peak + n_cons * offpeak
      regime=injection,
        mono : (d_cons + n_cons) * single - (d_inj + n_inj) * inj_m
      regime=injection,
        bi   : d_cons * peak + n_cons * offpeak
               - (d_inj + n_inj) * inj_m
      regime=compensation, mono :
               (d_cons + n_cons - d_inj - n_inj) * single
      regime=compensation, bi :
               (d_cons - d_inj) * peak + (n_cons - n_inj) * offpeak

    Compensation netting happens once over the YTD total at the end
    (clamped at zero), matching how the Walloon annual meter readout
    actually settles -- a day of over-injection can offset a later day
    of higher consumption.

    Plus fees: the supplier yearly fixed fee and the Flemish energy
    fund are summed per archived month using each month's snapshot
    (so a supplier indexation that lands mid-year is honoured for the
    months it applies to), pro-rated by ``days_in_month_in_ytd /
    days_in_year`` so the YTD total still grows uniformly across the
    calendar year. The Walloon prosumer fee follows the same per-month
    walk against each month's DSO overlay. The running bill grows day
    by day instead of jumping to the full annual on Jan 1.

    ``inj_m`` is each month's snapshot's ``injection.current`` (the
    printed monthly indicative).

    **Time-of-Use contracts** (Engie Empower Flextime, Luminus
    SmartFlex) take a per-hour path: the recorder's hourly kWh deltas
    are billed against ``compute_breakdown`` at each local hour, so
    the energy component picks the supplier's TOU slot rate while the
    network component still follows the user's DSO mode. Reads either
    ``CONF_CONSUMPTION_KWH`` (single totals) or the day+night register
    pair via the recorder's hourly statistics; partial register
    wiring is rejected so a missing band can't silently undercount.

    **Dynamic contracts** (Cociter Dynamique, Eneco Power Dynamic,
    OCTA+ Dynamic, etc.) replay historical hourly ENTSO-E spots from
    the coordinator's persistent cache (filled lazily by
    ``_ensure_historical_spots``). Each past kWh is then billed at
    its actual ``factor*spot+base`` rate via ``compute_breakdown``,
    same code path as the live current_price. Hours with no spot in
    the cache (cold start before the backfill, or a gap left by an
    ENTSO-E publication outage) still bill their network and tax legs
    and forfeit only the energy term, so an entirely empty cache lands
    on the fees floor plus the grid and tax cost of every metered kWh
    rather than on fees alone.

    Returns ``None`` only when there is no meter input wired at all
    AND no snapshot to show fees against. In every other case the
    function returns a number, falling back to the fees-only floor
    rather than exposing ``unknown`` to the user.

    The whole year is recomputed from scratch on every coordinator tick
    by design: today's cost grows each hour, and prior days are NOT safely
    immutable between ticks (a late ENTSO-E spot fill or a backfill
    correction changes a past day's rate). Memoizing prior-day totals
    would risk serving a stale YTD; the full replay is O(hours-in-year)
    pure arithmetic (~100 ms by December), which is negligible at the
    hourly update cadence, so keep it simple.

    ``breakdown`` is an optional diagnostic out-dict. When passed (only the
    live coordinator does; the compare / backfill callers leave it ``None``),
    the static per-day branch records the YTD and today kWh totals, the
    pre-clamp raw energy term and the fees floor into it, so the
    current_year_cost sensor can surface them as attributes. This piggybacks
    on the walk already happening here rather than reading the recorder twice.
    It stays empty for the dynamic / spot-monthly / TOU (hourly) branches,
    which don't produce daily kWh totals.
    """
    today = dt_util.now().date()
    # contract / meter overrides let the OptionsFlow's compare path run
    # this same engine against an alternative supplier's snapshot
    # without mutating the live entry. The user's region / DSO / regime /
    # solar_kva always come from the entry: those are the user's setup,
    # not the alternative's.
    contract = contract_override or entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    meter = meter_override or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    # Dispatch on the EFFECTIVE energy leg. A variable contract with a start
    # date re-prices its signing cohort to a SpotMonthlyRates leg, which bills
    # on the monthly-mean hourly path rather than the variable static daily
    # path. _cohort_energy_leg returns None for the compare flow and for
    # contracts without a start date, leaving the current card's kind. The
    # per-month walk resolves the same cohort leg through
    # _effective_snapshot_for_month, so dispatch and per-month pricing agree.
    cohort_energy = await _cohort_energy_leg(
        hass, session, extractor, contract, region, entry, snapshot
    )
    eff_energy = snapshot.energy if cohort_energy is None else cohort_energy

    jan1 = date(today.year, 1, 1)

    static_fees = await _ytd_static_fees(
        hass, session, extractor, snapshot, entry, today, contract=contract, meter=meter
    )
    prosumer_ytd = await _ytd_prosumer(
        hass, session, extractor, snapshot, entry, today, contract=contract
    )
    capacity_ytd = await _ytd_capacity(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        today,
        billed_peak_kw,
        contract=contract,
    )
    fees = static_fees + prosumer_ytd + capacity_ytd
    if breakdown is not None:
        # Reported on every contract kind, not just the static path: the fees
        # floor is what a low bill rests on whichever way energy is priced.
        breakdown["fees_ytd_eur"] = fees
        # And split, because the lump hid the leg most able to move it. The
        # Flanders capacity tariff is billed per kW of monthly peak per year
        # (52 to 60 EUR/kW across the Fluvius areas), so two entries reading
        # the same meter and the same card still differ by hundreds of euro
        # when they resolve different peaks. None of that shows on the price
        # graph, which is per kWh, so the only way a user could see it was to
        # download diagnostics. One comparison of this attribute now answers
        # "why do my two entries disagree".
        breakdown["capacity_ytd_eur"] = capacity_ytd
        breakdown["prosumer_ytd_eur"] = prosumer_ytd
        breakdown["standing_charges_ytd_eur"] = static_fees
        breakdown["billed_peak_kw"] = billed_peak_kw

    # Dynamic contracts replay historical hourly ENTSO-E spots so each
    # past kWh hits its actual factor*spot+base rate. Caller passes the
    # spot cache (the coordinator persists it between runs); an hour the
    # cache cannot price still gets its network and tax legs.
    if isinstance(eff_energy, DynamicRates):
        # An empty spot cache is not a reason to bill nothing. Every hour still
        # carries a network and a tax leg, so pass {} rather than bailing to the
        # fees floor and let the replay price those and drop the energy term
        # alone (same rule the per-hour gap follows).
        dyn_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
            breakdown=breakdown,
            historical_spots=historical_spots or {},
            spot_quarters=spot_quarters,
        )
        if dyn_energy is None:
            return fees
        return dyn_energy + fees

    # Spot-monthly contracts bill each past hour at its delivery month's mean
    # spot (a flat rate within the month); the hourly replay threads that mean
    # in place of the live spot and credits mean-indexed injection the same way.
    if isinstance(eff_energy, SpotMonthlyRates):
        monthly_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
            breakdown=breakdown,
            historical_spots=historical_spots or {},
            spot_quarters=spot_quarters,
            monthly_mean=True,
            spp_weights=spp_weights,
        )
        if monthly_energy is None:
            return fees
        return monthly_energy + fees

    # Per-hour billing is required when the supplier's energy rates
    # vary by hour (TOU + Impact energy contracts), when the DSO bills
    # per Impact band (PIC / MEDIUM / ECO change with hour-of-day), or
    # for an exclusive_night meter (its energy + distribution use the
    # dedicated exclusive-night rates, which the static per-day branch's
    # single/peak/offpeak breakdowns don't carry -- so without this it
    # would bill the YTD at the day rate while the live sensor uses the
    # cheaper exclusive-night rate). All go through the same hourly path,
    # which routes the meter through compute_breakdown.
    needs_hourly = (
        isinstance(eff_energy, (TimeOfUseRates, ImpactRates))
        or dso_mode == DSO_MODE_IMPACT
        or meter == METER_EXCLUSIVE_NIGHT
    )
    if needs_hourly:
        hourly_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
            breakdown=breakdown,
        )
        if hourly_energy is None:
            return fees
        if regime == SOLAR_REGIME_INJECTION:
            # _ytd_hourly_energy here runs without historical spots, so a
            # spot-indexed injection (Cociter Variable) credited nothing
            # above. Apply the same per-hour spot-replayed credit the
            # daily path uses; a no-op for monthly-indicative injection.
            hourly_energy -= await _ytd_spot_injection_credit(
                hass,
                snapshot,
                entry,
                today,
                historical_spots,
                _month_snapshot_cache(
                    hass, session, extractor, contract, region, snapshot, entry
                ),
            )
        return hourly_energy + fees

    daily_kwh = await _resolve_daily_kwh(hass, entry, today)
    if daily_kwh is None:
        # No meter inputs at all - fees-only floor.
        return fees

    # Precompute the snapshot + breakdowns for each month touched, so
    # the per-day loop stays O(days) without repeating the breakdown
    # math for every day in a month.
    month_breakdowns: dict[date, tuple[Any, Any, Any, "SupplierSnapshot"] | None] = {}

    async def _resolve_month(
        month_first: date,
    ) -> tuple[Any, Any, Any, "SupplierSnapshot"] | None:
        if month_first in month_breakdowns:
            return month_breakdowns[month_first]
        snap_m = await _effective_snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot, entry
        )
        try:
            single_bd = static_breakdown(snap_m, dso, region, "single", dso_mode)
            peak_bd = static_breakdown(snap_m, dso, region, "peak", dso_mode)
            offpeak_bd = static_breakdown(snap_m, dso, region, "offpeak", dso_mode)
        except KeyError:
            # An archived snapshot can lose the user's DSO key when the
            # supplier renames a row or a regex misses for that month.
            # Treating the month as "no rate to apply" matches dynamic
            # / TOU behaviour and keeps the YTD loop running instead of
            # tearing the whole tick down with UpdateFailed.
            _LOGGER.debug(
                "static_breakdown missing DSO %s for %s/%s/%s; falling back",
                dso,
                snap_m.supplier,
                snap_m.contract,
                month_first,
            )
            month_breakdowns[month_first] = None
            return None
        if single_bd is None or peak_bd is None or offpeak_bd is None:
            month_breakdowns[month_first] = None
            return None
        bundle = (single_bd, peak_bd, offpeak_bd, snap_m)
        month_breakdowns[month_first] = bundle
        return bundle

    energy_cost = 0.0
    # A flat energy leg can still carry a monthly-indexed feed-in credit
    # (energie.be Vast). The daily walk has no spot of its own, so resolve the
    # delivery month's SPP-weighted mean here, memoised per month, and let the
    # shared helper fall back to the card's indicative when it is missing.
    day_spp: dict[tuple[int, int], float | None] = {}
    day_bucket = _bucket_by_local_month(historical_spots) if historical_spots else {}
    for day in _days_through(jan1, today):
        bundle = await _resolve_month(date(day.year, day.month, 1))
        if bundle is None:
            # Dynamic / TOU month: no stable rate to apply for any of
            # its days.
            continue
        single_bd, peak_bd, offpeak_bd, snap_d = bundle

        d_cons, n_cons, d_inj, n_inj = daily_kwh.get(day, (0.0, 0.0, 0.0, 0.0))
        total_cons = d_cons + n_cons
        total_inj = d_inj + n_inj

        bi_capable = meter in ("bi", "dynamic")
        if regime == SOLAR_REGIME_COMPENSATION:
            if bi_capable:
                d_cost = (d_cons - d_inj) * peak_bd.all_in + (
                    n_cons - n_inj
                ) * offpeak_bd.all_in
            else:
                d_cost = (total_cons - total_inj) * single_bd.all_in
        elif regime == SOLAR_REGIME_INJECTION:
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in
            inj_rate = _historical_injection_rate(
                snap_d.injection,
                _spp_injection_spot(
                    None,
                    monthly_mean=_injection_on_month_mean(snap_d),
                    strict=_injection_is_spp_indexed(snap_d),
                    spp_weights=spp_weights,
                    bucket=day_bucket,
                    year=day.year,
                    month=day.month,
                    today=today,
                    cache=day_spp,
                ),
            )
            if inj_rate is not None:
                d_cost -= total_inj * inj_rate
        else:  # none
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in

        energy_cost += d_cost

    # Raw energy term before the compensation zero-floor: a negative value
    # here is what the clamp below hides, so surface it for diagnostics.
    energy_ytd_raw = energy_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        # YTD clamp at zero: the bill never goes negative, surplus
        # injection past consumption is forfeited (by most Walloon
        # suppliers).
        energy_cost = max(energy_cost, 0.0)

    if regime == SOLAR_REGIME_INJECTION:
        # Spot-indexed injection on a static-energy contract (Cociter
        # Variable): the daily loop above credited nothing for it (its
        # injection has no monthly indicative), so subtract the per-hour
        # spot-replayed credit
        # here. A no-op (0.0) for every other contract.
        energy_cost -= await _ytd_spot_injection_credit(
            hass,
            snapshot,
            entry,
            today,
            historical_spots,
            _month_snapshot_cache(
                hass, session, extractor, contract, region, snapshot, entry
            ),
        )
        # This regime has no compensation clamp, so the billed energy is
        # already the raw energy term.
        energy_ytd_raw = energy_cost

    if breakdown is not None:
        breakdown["consumption_ytd_kwh"] = sum(r[0] + r[1] for r in daily_kwh.values())
        # The per-day counterpart of hours_seen / hours_elapsed above: the
        # static branch reported no coverage at all, so a gap here was
        # invisible even in principle.
        breakdown["days_seen"] = float(len(daily_kwh))
        breakdown["days_elapsed"] = float((today - date(today.year, 1, 1)).days + 1)
        breakdown["injection_ytd_kwh"] = sum(r[2] + r[3] for r in daily_kwh.values())
        today_kwh = daily_kwh.get(today, (0.0, 0.0, 0.0, 0.0))
        breakdown["consumption_today_kwh"] = today_kwh[0] + today_kwh[1]
        breakdown["injection_today_kwh"] = today_kwh[2] + today_kwh[3]
        breakdown["energy_ytd_raw_eur"] = energy_ytd_raw

    return energy_cost + fees
