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

"""Rebuilding the cost statistics the recorder should already hold.

The prices are a per-hour value the compiler can be handed directly. A cost
is a running sum, so a row written into the middle of a year has to continue
the total that was there before it and leave the total after it consistent,
or the energy dashboard reads a step that never happened. That is what makes
this the long half of a backfill.
"""

from __future__ import annotations

from .backfill_window import (
    _COST_SENSOR_KEY,
    _build_context,
    _recorder_models,
    _stat_id,
)
from .cohort import _parse_iso_date, ytd_window_start
from .const import (
    CONF_CONTRACT_START_DATE,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .coordinator import BePricesCoordinator
from .coordinator_data import ytd_window_reset
from .energy_meters import _metered_hourly_kwh
from .fees import (
    _annual_static_fees,
    _capped_capacity_monthly_eur,
    _compensation_kva,
    _prosumer_monthly_fee,
    _welcome_credit_eur,
    first_year_net_kwh,
    window_energy_rate,
)
from .injection import _historical_injection_rate, _injection_is_spot_formula
from .pricing import (
    MeterType,
    compute_breakdown,
    compute_network_and_taxes,
    renewables_eur_per_kwh,
    yearly_fixed_fee_for_meter,
)
from .spot_stats import (
    _NetAllocation,
    _bucket_by_local_month,
    _energy_needs_spot,
    _hour_spot,
    _injection_is_spp_indexed,
    _injection_on_month_mean,
    _register_for,
    _rlp_hour_weight,
    _spp_injection_spot,
)
from .synergrid import SppWeights
from datetime import date, datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import TYPE_CHECKING, Any
import calendar
import logging

if TYPE_CHECKING:
    # Annotations only, as _recorder_models says: the package has to load
    # without the recorder, and the calls take the lazy copies it returns.
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )

_LOGGER = logging.getLogger(__name__)


def _injection_rate_for_hour(
    snap_h: Any,
    *,
    spot: float | None,
    spots: dict[datetime, float],
    quarters: dict[datetime, list[float]],
    utc_hour: datetime,
    local: datetime,
    spp_weights: SppWeights | None,
    month_spp_cache: dict[tuple[int, int, bool], float | None],
    hourly_injection: bool,
    today: date,
    meter: MeterType = METER_MONO,
    region: str = REGION_FLANDERS,
) -> float | None:
    """The feed-in rate for one backfilled hour, or None when it has none.

    Both backfill passes resolved this identically: the same nine-keyword
    _spp_injection_spot call followed by the same _historical_injection_rate.

    ``monthly_mean`` stays derived here from THIS hour's snapshot rather than
    being hoisted by the caller: an archived month can carry a different
    energy kind from the cohort leg, so the flag is per-hour, not per-run.
    That is also why the hour's own 15-minute slots are only handed on when
    this hour is priced off its own spot: a credit settling on a month mean is
    not priced by what one hour's quarters did. ``quarters`` is empty except
    on an entry whose feed-in formula is floored.
    """
    monthly_mean = _injection_on_month_mean(snap_h)
    inj_spot = _spp_injection_spot(
        # The hour's spot goes in only when the CREDIT is the one that replays
        # it, judged by the same predicate the live scalar and the year-to-date
        # walk use: a card that prints a monthly indicative beside its formula
        # bills the indicative, and handing this the energy's spot priced a
        # whole year of feed-in off a formula the card calls an illustration.
        (
            spot
            if snap_h.injection is not None
            and _injection_is_spot_formula(snap_h.injection, snap_h.energy)
            else None
        ),
        monthly_mean=monthly_mean,
        # An SPP-indexed formula may only resolve against the SPP-weighted
        # mean; without one _historical_injection_rate falls through to the
        # card's printed indicative rather than the energy leg's mean.
        strict=_injection_is_spp_indexed(snap_h),
        # The month's own settled index when the supplier has published it,
        # which makes every source below moot.
        index_realised=getattr(snap_h.injection, "index_realised", None),
        spp_weights=spp_weights,
        historical_spots=spots,
        year=local.year,
        month=local.month,
        today=today,
        cache=month_spp_cache,
        hourly=hourly_injection,
        hourly_spot=spots.get(utc_hour),
    )
    return _historical_injection_rate(
        snap_h.injection,
        inj_spot,
        quarters=(
            quarters.get(utc_hour) if hourly_injection or not monthly_mean else None
        ),
        energy=snap_h.energy,
        when=local,
        meter=meter,
        region=region,
    )


async def _backfill_cost_sensor(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
    spots: dict[datetime, float],
    quarters: dict[datetime, list[float]],
    emit_from: datetime | None = None,
) -> dict[str, int]:
    """Write cumulative state/sum rows for ``current_year_cost`` over ``hours``.

    Mirrors the live :func:`_compute_current_year_cost` engine but
    produces one running-total point per hour instead of one
    end-of-day number, so the recorder can render the YTD bill as a
    growing line on the Energy dashboard / Statistics card.

    Per-hour fee proration uses ``annual_for_this_month / hours_in_year``
    (vs. the live ``days_in_ytd / days_in_year`` per-day proration);
    the two converge at end-of-day, but the hourly variant gives a
    smoother in-day curve. Per-month tariff archives are honoured the
    same way as in the live path. So is a welcome credit: subtracted per
    day off the signing month's card through the same ``_welcome_credit_eur``
    the live walk calls, capped against the same three running components,
    so the imported series and the sensor agree at the end of every day
    rather than meeting at a step of everything credited so far.

    ``current_year_cost`` is a cumulative ``TOTAL`` sensor that resets on
    Jan 1. ``hours`` MUST stay within a single calendar year, anchored at
    that year's Jan 1: the loop accumulates monotonically from the first
    hour and (when ``emit_from`` is set) only writes rows on/after it, so
    a mid-year backfill still carries the correct year-to-date sum. The
    sum must not be reset mid-series: the recorder derives the Energy
    dashboard's change as ``sum - prev_sum`` and ignores ``last_reset``
    for imported statistics, so a drop back to ~0 would render as a large
    spurious negative cost. The caller therefore anchors on Jan 1 of the
    *end* year and never spans a year boundary here.

    Returns a per-statistic-id row count (one entry max). Skips
    silently when the sensor isn't registered (auto path firing
    before platform setup completes).
    """
    sid = _stat_id(hass, entry, _COST_SENSOR_KEY)
    if sid is None:
        return {}

    (
        StatisticData,
        StatisticMetaData,
        StatisticMeanType,
        async_import_statistics,
    ) = _recorder_models()
    ctx = await _build_context(hass, entry, coordinator, hours)
    region = ctx.region
    dso = ctx.dso
    meter = ctx.meter
    dso_mode = ctx.dso_mode
    regime = ctx.regime
    is_compensation = regime == SOLAR_REGIME_COMPENSATION
    # Already includes the regime and Wallonia halves of the gate.
    kva = _compensation_kva(entry)
    # The kW the Flemish capacity tariff is charged on. Read from the live
    # coordinator so the backfilled series accrues it exactly as the live
    # _ytd_capacity does; the rolling mean is not reconstructable per past
    # month, so both use the current one (see _ytd_capacity).
    billed_peak_kw = coordinator._billed_peak_kw() if region == REGION_FLANDERS else 0.0

    # One bulk fetch per recorder entity; bin into UTC-hour totals.
    # _recorder_rows treats the start/end arguments as local-day
    # boundaries; pass the local dates of the first / last UTC hour so
    # the recorder query window aligns with the backfill's _hour_iter.
    # Passing UTC dates here would shift the window by 1-2h vs local
    # midnight and either drop or double-include the end-of-range hour.
    cons_per_hour: dict[datetime, float] = {}
    inj_per_hour: dict[datetime, float] = {}
    if hours:
        start_d = dt_util.as_local(hours[0]).date()
        end_d = dt_util.as_local(hours[-1]).date()
        metered_cons = await _metered_hourly_kwh(
            hass, entry, "consumption", start_d, end_d
        )
        metered_inj = await _metered_hourly_kwh(
            hass, entry, "injection", start_d, end_d
        )
        # Mirror the live paths: a pair that cannot be billed (half-wired, or
        # one half recording nothing) accrues fees only rather than bill the
        # wired half and credit injection against a consumption side that
        # silently resolved to nothing.
        if metered_cons is not None and metered_inj is not None:
            cons_per_hour, inj_per_hour = metered_cons.kwh, metered_inj.kwh

    _snap_for = ctx.snap_for
    spp_weights = ctx.spp_weights
    month_spp_cache = ctx.month_spp_cache
    month_mean_cache = ctx.month_mean_cache
    hourly_injection = ctx.hourly_injection
    # Bucketed once, like the live walk: the closed-month coverage gate needs
    # to know how much of a month is actually cached, not just its mean.
    month_bucket = _bucket_by_local_month(spots) if spots else {}
    today = dt_util.now().date()

    # UTC-hour count per local day so the static fee accrues smoothly per
    # hour yet each local day sums to exactly annual/days_in_year, even on
    # the DST seam days (23 or 25 UTC hours).
    hours_per_local_date: dict[date, int] = {}
    for h in hours:
        d = dt_util.as_local(h).date()
        hours_per_local_date[d] = hours_per_local_date.get(d, 0) + 1

    rows: list[Any] = []
    running_energy = 0.0
    running_fees = 0.0
    # The three components a welcome credit may come off, kept beside the bill
    # the way the live walk keeps them: the supplier's energy component of the
    # consumption (gross of any feed-in credit), the supplier's own standing
    # charge and the green electricity / CHP contribution. The credit is
    # capped against their running sum, so a series that omitted it met the
    # live sensor at a step of everything credited so far.
    running_energy_component = 0.0
    running_supplier_fee = 0.0
    running_green = 0.0
    # What the window drew, and what it drew less what it put back. The
    # welcome credit's per-kWh term rides a YEAR rather than this window, so
    # the two together give the export share to apply to the entry's yearly
    # volume (first_year_net_kwh).
    running_consumption_kwh = 0.0
    running_net_kwh = 0.0
    # The window the credit accrues over is the sensor's own, whichever year
    # the caller anchored the hours on, and the first year it counts from is
    # the entry's own start date, as on the live side.
    credit_window_start = ytd_window_start(entry, dt_util.as_local(hours[0]).date())
    credit_start = _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE))
    netting = _NetAllocation()
    allocated = ctx.rlp_weights is not None
    for utc_hour in hours:
        local = dt_util.as_local(utc_hour)
        month_first = date(local.year, local.month, 1)
        snap_h = await _snap_for(month_first)
        spot = _hour_spot(
            snap_h.energy,
            local,
            utc_hour,
            spots,
            month_bucket,
            month_mean_cache,
            today,
            ctx.rlp_weights,
        )

        # Energy term: an hour the spot cache cannot price is NOT dropped.
        # It still has a network leg and a tax leg, both known from that
        # month's snapshot and neither depending on the day-ahead price, and on
        # a Belgian residential card those two are the larger half of the
        # all-in rate. The live walk bills them through compute_network_and_taxes
        # for exactly this reason; skipping the hour whole here instead made the
        # persisted cost series drop grid and taxes on every metered kWh in an
        # ENTSO-E gap, so the imported rows and the compiled ones disagreed at
        # the seam by more than the energy nobody could price.
        no_spot = spot is None and _energy_needs_spot(snap_h.energy)
        try:
            bd = (
                compute_network_and_taxes(snap_h, dso, region, local, meter, dso_mode)
                if no_spot
                else compute_breakdown(
                    snap_h, dso, region, local, spot, meter, dso_mode
                )
            )
        except (KeyError, ValueError):
            bd = None
        if bd is not None:
            cons = cons_per_hour.get(utc_hour, 0.0)
            inj = inj_per_hour.get(utc_hour, 0.0)
            # An unpriced hour has a zero energy component, as in the live
            # walk: nothing was charged, so nothing can be credited against.
            running_energy_component += cons * bd.energy
            running_green += cons * renewables_eur_per_kwh(snap_h.taxes, region)
            running_consumption_kwh += cons
            running_net_kwh += cons - inj
            if is_compensation:
                netting.add(
                    _register_for(local, meter, dso_mode, region),
                    cons - inj,
                    bd.all_in,
                    _rlp_hour_weight(ctx.rlp_weights, local),
                )
            elif regime == SOLAR_REGIME_INJECTION:
                running_energy += cons * bd.all_in
                inj_rate = _injection_rate_for_hour(
                    snap_h,
                    spot=spot,
                    spots=spots,
                    quarters=quarters,
                    utc_hour=utc_hour,
                    local=local,
                    spp_weights=spp_weights,
                    month_spp_cache=month_spp_cache,
                    hourly_injection=hourly_injection,
                    today=today,
                    meter=meter,
                    region=region,
                )
                if inj_rate is not None:
                    running_energy -= inj * inj_rate
            else:
                running_energy += cons * bd.all_in

        # Fee accrual: spread each local day's annual/days_in_year share
        # evenly over that day's actual UTC hours, so the YTD line grows
        # smoothly yet every day (including the 23/25-hour DST seam days)
        # totals exactly annual/days_in_year, matching the live YTD per-day
        # proration (annual * days_in_ytd / days_in_year). A flat
        # annual/(days_in_year*24) rate accrued 23 or 25 hours' worth on
        # the seam days, drifting from the live sensor at the seam.
        days_in_year = 366 if calendar.isleap(local.year) else 365
        annual_static = _annual_static_fees(snap_h, meter, entry)
        running_fees += (
            annual_static / days_in_year / hours_per_local_date[local.date()]
        )
        # The supplier's share of that, spread the same way, because a welcome
        # credit may come off the standing charge and off none of the energy
        # fund, data-management or OSP fees accrued beside it.
        running_supplier_fee += (
            float(yearly_fixed_fee_for_meter(snap_h.energy, meter) or 0.0)
            / days_in_year
            / hours_per_local_date[local.date()]
        )

        # Flemish capacity tariff, spread per local day like the prosumer fee
        # below (its monthly charge over that month's days), so the backfill
        # meets the live _ytd_capacity proration (days_in_ytd /
        # days_in_full_month) at the seam rather than trailing it.
        if billed_peak_kw:
            # With the card's VAT basis, as the live walk passes it: the
            # ceiling headroom is grossed by the same rate as the charge,
            # and without it a professional card billed VAT-inclusive had its
            # headroom a fifth short here and its capped months lower than
            # the live sensor's.
            monthly = _capped_capacity_monthly_eur(
                snap_h.dsos.get(dso),
                entry,
                billed_peak_kw,
                vat_rate=snap_h.taxes.vat_rate,
            )
            if monthly:
                days_in_full_month = calendar.monthrange(
                    month_first.year, month_first.month
                )[1]
                running_fees += (
                    monthly / days_in_full_month / hours_per_local_date[local.date()]
                )

        # Compensation is Walloon-only (see fees._compute_prosumer):
        # gate the prosumer accrual to Wallonia so a Flanders entry never
        # backfills prosumer on top of the capacity tariff.
        if kva:
            overlay = snap_h.dsos.get(dso)
            monthly_fee = _prosumer_monthly_fee(overlay, snap_h, kva)
            if monthly_fee:
                # Prorate the monthly prosumer fee per local day, the same way
                # the static fee above is spread, so both reach a full daily
                # share on the current in-progress day and the backfill meets
                # the live _ytd_prosumer (days_in_ytd / days_in_full_month)
                # proration at the seam instead of trailing it by a partial
                # day. Dividing by that day's actual UTC-hour count makes each
                # day (including the 23/25-hour DST seam days) sum to exactly
                # monthly_fee / days_in_full_month.
                days_in_full_month = calendar.monthrange(
                    month_first.year, month_first.month
                )[1]
                running_fees += (
                    monthly_fee
                    / days_in_full_month
                    / hours_per_local_date[local.date()]
                )

        # Compensation regime clamps the YTD energy term at zero
        # (Walloon meter forfeits surplus injection past
        # consumption); injection / none never go negative through
        # the energy term alone.
        displayed_energy = (
            netting.billed(allocated=allocated) if is_compensation else running_energy
        )
        # Credited by the DAY, as the live walk does, against what the window
        # has charged so far: the two agree at the end of every local day and
        # the backfill runs at most a day's share ahead inside one, the same
        # kind of intra-day lead the fee proration above carries.
        credit = _welcome_credit_eur(
            ctx.signing,
            credit_start,
            credit_window_start,
            local.date(),
            running_energy_component + running_supplier_fee + running_green,
            first_year_net_kwh(
                ctx.annual_kwh,
                running_consumption_kwh,
                running_consumption_kwh - running_net_kwh,
                compensation=is_compensation,
            ),
            window_energy_rate(running_energy_component, running_consumption_kwh),
        )
        state = round(displayed_energy + running_fees - credit, 4)
        # Accumulate from Jan 1 (the caller anchors ``hours`` there) but
        # only emit rows inside the requested window, so a mid-year
        # ``start`` still carries the correct year-to-date sum instead of
        # restarting from zero and clashing with the pre-existing series.
        if emit_from is None or utc_hour >= emit_from:
            rows.append(StatisticData(start=utc_hour, state=state, sum=state))

    if not rows:
        return {sid: 0}

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=sid,
        unit_class=None,
        unit_of_measurement="EUR",
    )
    async_import_statistics(hass, metadata, rows)
    _seed_short_term_sum(hass, metadata, rows[-1], ytd_window_reset(entry))
    return {sid: len(rows)}


def _seed_short_term_sum(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    last: StatisticData,
    last_reset: datetime,
) -> None:
    """Continue the imported ``sum`` chain into the live one.

    The cost sensor is ``state_class: TOTAL``, so the recorder's own sensor
    platform compiles statistics for the same id we import into, and it seeds
    its running sum from ``statistics_short_term`` alone
    (``sensor/recorder.py``: ``_sum = last_stat.get("sum") or 0.0``).
    ``async_import_statistics`` writes only the long-term table, so without
    this the live chain restarts at zero directly after a backfilled row
    carrying the whole year: the first compiled hour then reports
    ``change = 0 - <year to date>``, and the Energy dashboard's Cost card
    shows roughly minus one annual bill for that day.

    Writing one short-term row at the last backfilled instant hands the
    platform the running total to resume from. It has to carry ``last_reset``
    as well as ``state`` and ``sum``: the compiler reads all three off that
    row, and a row without one looks like a fresh cycle against the sensor's
    own Jan-1 ``last_reset``, which takes the meter-reset branch and adds the
    whole live reading on top of the resumed sum instead of the delta.
    ``last_reset`` is passed in rather than computed here so it can only ever
    be what the caller resolved through ``ytd_window_reset``, which is the same
    function the sensor's ``last_reset_fn`` is: local Jan 1, or the contract
    start date on an entry that bills its year-to-date from there.

    Best effort: a recorder that refuses the write leaves the seam, which is
    no worse than not trying, so it must never take the backfill down with it.
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    except ImportError:  # pragma: no cover - recorder always ships with HA
        return
    seed: StatisticData = {
        **last,
        # Must be the SAME instant the current_year_cost sensor reports as its
        # last_reset, or the compiler takes the meter-reset branch.
        "last_reset": last_reset,
    }
    try:
        get_instance(hass).async_import_statistics(
            metadata, [seed], StatisticsShortTerm
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        _LOGGER.debug(
            "could not seed the short-term sum for %s; the first compiled "
            "hour will show a one-off negative change",
            metadata["statistic_id"],
        )
