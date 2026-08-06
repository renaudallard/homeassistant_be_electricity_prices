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
one concern cut by size, not two layers. Everything here is reachable only
from the compare branch.

Deliberately NOT folded into ``pricing.py``. That module is a leaf the
coordinator imports at load time; several functions here reach back into
``coordinator`` and one does recorder I/O, so folding them in would invert the
dependency direction.

The function-local imports are kept verbatim for the same reason they were
local before: they close what would otherwise be an import cycle.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_REGION,
    CONF_SOLAR_REGIME,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_NONE,
)


def _tou_slot_weights(weekend_rule: str) -> tuple[float, float, float]:
    """Hours-per-week each CWaPE TOU slot (peak, transition, offpeak) is
    active, from the published rules and a 5-weekday / 2-weekend split.

    Engie Empower Flextime keeps the weekday transition/offpeak windows on
    weekends (``weekend_no_peak``); Luminus SmartFlex makes weekends fully
    off-peak (``weekend_offpeak``, the default).
    """
    if weekend_rule == "weekend_no_peak":
        return 45.0, 69.0, 54.0
    return 45.0, 45.0, 78.0


def _compare_injection_credit(
    snapshot: Any,
    entry: Any,
    spot_dict: dict[datetime, float],
    avg_spot: float | None,
) -> float | None:
    """Injection credit (EUR/kWh) for the compare flow's annual estimate.

    A per-slot TOU injection (Engie Empower Flextime) is time-averaged over
    the published slot durations, mirroring how the consumption side is
    weighted in ``_tou_weighted_per_kwh``; delegating to the live helper
    would return the dialog-open slot rate and bias the credit. A
    spot-indexed injection (Cociter Variable, or any dynamic-energy
    contract) is priced off the window MEAN spot, consistent with the
    energy term (which also uses ``avg_spot``); pricing it off the live
    current slot would make the solar credit and the energy cost reflect
    different instants. Monthly-indexed injection is spot-independent (uses
    the realized monthly value), so delegate that to the live helper.
    """
    from .injection import _compute_injection_price, _floor_injection
    from .providers.base import DynamicRates, TimeOfUseRates

    inj = getattr(snapshot, "injection", None)
    energy = getattr(snapshot, "energy", None)
    if (
        inj is not None
        and isinstance(energy, TimeOfUseRates)
        and inj.peak is not None
        and inj.transition is not None
        and inj.offpeak is not None
    ):
        wp, wt, wo = _tou_slot_weights(energy.weekend_rule)
        return float(
            (inj.peak * wp + inj.transition * wt + inj.offpeak * wo) / (wp + wt + wo)
        )
    if (
        inj is not None
        and inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    ):
        # Floor the formula result like the live and historical paths so the
        # compare estimate doesn't count a negative feed-in as extra cost when
        # the contract clamps injection at zero.
        return (
            _floor_injection(inj.factor * avg_spot + inj.base, inj)
            if avg_spot is not None
            else None
        )
    return _compute_injection_price(snapshot, entry, spot_dict)


def _period_avg_all_in(
    snapshot: Any,
    dso: str,
    region: str,
    start: datetime,
    num_days: int,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
) -> float | None:
    """Mean all-in EUR/kWh over ``num_days`` from ``start`` under uniform
    hourly consumption.

    Sampling every hour lets each hour carry its true energy slot AND network
    band, so the TOU energy windows and the bi-horaire network bands - which
    don't align, and both differ on weekends - are each weighted correctly.
    A three-sample-per-slot weighting instead assigns one network band to a
    whole energy slot and mis-prices it. Returns None on any compute failure.
    """
    from .pricing import compute_breakdown

    total = 0.0
    count = 0
    for hour in range(num_days * 24):
        try:
            bd = compute_breakdown(
                snapshot,
                dso,
                region,
                start + timedelta(hours=hour),
                spot,
                meter,
                dso_mode,
            )
        except Exception:  # noqa: BLE001
            return None
        total += bd.all_in
        count += 1
    return total / count if count else None


def _tou_weighted_per_kwh(
    snapshot: Any,
    dso: str,
    region: str,
    when_now: datetime,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
) -> float | None:
    """Per-kWh EUR/kWh for the compare flow's annual estimate, with a
    TOU-aware time-weighted average when the snapshot's energy rate
    splits by hour-of-day.

    For Fixed / Variable the breakdown is spot-independent. For Dynamic
    the breakdown is linear in ``spot``, so the caller passes the MEAN
    spot over the fetched day window (not the instantaneous one) to get a
    time-averaged annual figure. For TOU contracts (Luminus SmartFlex, Engie
    Empower Flextime) and Impact contracts (Mega Off-peak Impact)
    ``compute_breakdown`` returns one of three slot rates depending on
    the hour the user opens the dialog -- biased. Compute breakdowns
    at three representative weekday hours (one per slot) and weight by
    the published slot durations across a week, so the annual estimate
    isn't dragged toward whichever slot the user happens to be in.

    Returns ``None`` on compute failure so the caller can render '-'
    on the result page rather than tear the flow down.
    """
    from .pricing import (
        compute_breakdown,
        impact_band_hours,
        is_belgian_holiday,
        is_offpeak,
    )
    from .providers.base import ImpactRates, TimeOfUseRates

    try:
        bd = compute_breakdown(snapshot, dso, region, when_now, spot, meter, dso_mode)
    except Exception:  # noqa: BLE001
        return None
    # The all-in is time-of-day dependent not only for TOU/Impact energy
    # but also when the meter routes a bi-horaire peak/offpeak split
    # (Fixed/Variable on a bi-hourly or dynamic meter) or when the DSO
    # tariff mode is Impact (network varies by CWaPE band). Returning the
    # single dialog-open-time rate for those biased the annual estimate by
    # whichever slot the user happened to be in.
    overlay = snapshot.dsos.get(dso)
    bi_split = meter in ("bi", "dynamic") and (
        (
            getattr(snapshot.energy, "peak", None) is not None
            and getattr(snapshot.energy, "offpeak", None) is not None
        )
        or (
            overlay is not None
            and getattr(overlay, "distribution_peak", None) is not None
            and getattr(overlay, "distribution_offpeak", None) is not None
        )
    )
    impact_network = dso_mode == "impact"
    if (
        not isinstance(snapshot.energy, (TimeOfUseRates, ImpactRates))
        and not bi_split
        and not impact_network
    ):
        return bd.all_in
    # Pick a recent non-holiday weekday so each slot lookup hits the
    # weekday rule. Walk back from today's local date.
    weekday = when_now.date()
    for _ in range(8):
        if not is_belgian_holiday(weekday) and weekday.weekday() < 5:
            break
        weekday -= timedelta(days=1)
    base = datetime.combine(weekday, time(), tzinfo=when_now.tzinfo)
    if isinstance(snapshot.energy, ImpactRates) or impact_network:
        # Weight each CWaPE Impact band by its share of the day. Both the
        # representative hour and the weight come from `impact_band_hours`,
        # which counts them off `dso_impact_band`: the schedule is regulated
        # and belongs in one place, and writing the hours (19 / 9 / 3) and the
        # weights (35 / 49 / 84) out here meant a band boundary could move in
        # pricing.py while this estimate kept the old weighting silently.
        bands = impact_band_hours()
        try:
            weighted = 0.0
            hours = 0
            for band_hours in bands.values():
                if not band_hours:
                    continue
                band_bd = compute_breakdown(
                    snapshot,
                    dso,
                    region,
                    base.replace(hour=band_hours[0]),
                    spot,
                    meter,
                    dso_mode,
                )
                weighted += band_bd.all_in * len(band_hours)
                hours += len(band_hours)
        except Exception:  # noqa: BLE001
            return bd.all_in
        if not hours:
            return bd.all_in
        return weighted / hours
    if not isinstance(snapshot.energy, TimeOfUseRates):
        # Fixed/Variable on a bi-hourly/dynamic meter: weight the peak and
        # off-peak all-in by the region's bi-horaire hour split (uniform
        # consumption across a representative week, region-aware via
        # is_offpeak so the Wallonia 11-17 off-peak window and the Brussels
        # holiday rule are honoured). Any peak/off-peak hour is a valid
        # sample since the rate is constant within each band.
        peak_when: datetime | None = None
        off_when: datetime | None = None
        peak_hours = 0
        for day_offset in range(7):
            for hour in range(24):
                when = base + timedelta(days=day_offset, hours=hour)
                if is_offpeak(when, region):
                    off_when = off_when or when
                else:
                    peak_hours += 1
                    peak_when = peak_when or when
        if peak_when is None or off_when is None:
            return bd.all_in
        try:
            bd_peak = compute_breakdown(
                snapshot, dso, region, peak_when, spot, meter, dso_mode
            )
            bd_off = compute_breakdown(
                snapshot, dso, region, off_when, spot, meter, dso_mode
            )
        except Exception:  # noqa: BLE001
            return bd.all_in
        return (
            bd_peak.all_in * peak_hours + bd_off.all_in * (168 - peak_hours)
        ) / 168.0

    # Weekday holidays bill under the weekend rule, so a single week that
    # happens to contain one would skew the slot mix. Walk back to a
    # holiday-free Mon-Sun week (matches the prior clean-week assumption).
    def _holiday_free_week(anchor: date) -> date:
        mon = anchor - timedelta(days=anchor.weekday())
        for _ in range(12):
            if not any(is_belgian_holiday(mon + timedelta(days=d)) for d in range(7)):
                return mon
            mon -= timedelta(days=7)
        return mon

    if snapshot.energy.weekend_rule == "smartflex_seasonal":
        # SmartFlex bills seasonal bands, so blend a summer and a winter
        # representative WEEK by season length (21/03-20/09 = 184 days, the
        # rest 181). A full week captures both the seasonal energy bands and
        # any weekday/weekend network split.
        acc = 0.0
        wsum = 0.0
        for probe, days in (
            (date(when_now.year, 7, 1), 184.0),
            (date(when_now.year, 1, 15), 181.0),
        ):
            season_monday = datetime.combine(
                _holiday_free_week(probe), time(), tzinfo=when_now.tzinfo
            )
            avg = _period_avg_all_in(
                snapshot, dso, region, season_monday, 7, spot, meter, dso_mode
            )
            if avg is None:
                return bd.all_in
            acc += avg * days
            wsum += days
        return acc / wsum
    # A TOU energy slot spans hours with different bi-horaire network bands
    # (and the weekend rule shifts hours between energy slots), so weighting
    # one sample per slot mis-prices the network. Average a full
    # representative week (Mon-Sun) so each hour carries its true energy slot
    # and network band.
    week_start = datetime.combine(
        _holiday_free_week(when_now.date()), time(), tzinfo=when_now.tzinfo
    )
    week_avg = _period_avg_all_in(
        snapshot, dso, region, week_start, 7, spot, meter, dso_mode
    )
    return week_avg if week_avg is not None else bd.all_in


def _populate_charts(
    placeholders: dict[str, str], *, current_label: str, compare_label: str
) -> None:
    """Render the annual / YTD bars from the numeric placeholders.

    Reads the ``current_annual`` / ``compare_annual`` (and YTD pair)
    placeholders and replaces ``annual_chart`` / ``ytd_chart`` with a
    two-row bar visualisation. Leaves them empty when either side is
    "-" so the result page still looks clean for the no-quote-yet
    case (e.g. fetch failed)."""
    for prefix, chart_key in (("annual", "annual_chart"), ("ytd", "ytd_chart")):
        cur = placeholders.get(f"current_{prefix}", "-")
        cmp_ = placeholders.get(f"compare_{prefix}", "-")
        if cur == "-" or cmp_ == "-":
            continue
        try:
            cur_v = float(cur)
            cmp_v = float(cmp_)
        except ValueError:
            continue
        placeholders[chart_key] = _bar_chart(
            {current_label: cur_v, compare_label: cmp_v}
        )


def _bar_chart(values: dict[str, float], width: int = 20) -> str:
    """Two-row unicode bar chart, both rows scaled against the larger
    value so the visual ratio matches the numeric one. Labels are
    padded so the bars line up. Returns ``""`` when any input is non-
    finite (negative-billing cases are clamped to zero for the bar
    only; the EUR values still render to keep the sign visible)."""
    if not values:
        return ""
    max_v = max(max(values.values(), default=0.0), 1.0)
    label_w = max(len(k) for k in values)
    rows: list[str] = []
    for label, v in values.items():
        bar_v = max(v, 0.0)  # negative annuals (huge solar credit) clamp to empty
        filled = round((bar_v / max_v) * width)
        filled = max(0, min(width, filled))
        bar = "█" * filled + "░" * (width - filled)
        rows.append(f"  {label.ljust(label_w)} {bar} {v:.0f} EUR")
    return "\n".join(rows)


def _solar_note(regime: str, rolling_inj_kwh: float) -> str:
    """One-line description of how solar is folded into the comparison.

    Renders into the result form's description placeholder. Empty for
    the no-solar case so the page doesn't show a misleading label."""
    if regime == "compensation":
        if rolling_inj_kwh > 0:
            return f"compensation regime: meter netted (consumption -= {rolling_inj_kwh:.0f} kWh, surplus forfeited)"
        return "compensation regime configured but no injection sensor wired - net = consumption"
    if regime == "injection":
        if rolling_inj_kwh > 0:
            return f"injection regime: {rolling_inj_kwh:.0f} kWh credited at each supplier's injection price"
        return "injection regime configured but no injection sensor wired - no injection credit applied"
    return ""


def _annual_bill(
    snapshot: Any,
    entry: ConfigEntry,
    peak_kw: float,
    per_kwh: float,
    consumption_kwh: float,
    injection_kwh: float = 0.0,
    injection_price: float | None = None,
    fee_proration: float = 1.0,
    prosumer_proration: float | None = None,
    meter: Any = METER_MONO,
    include_capacity: bool = True,
) -> float:
    """Estimated EUR bill for ``snapshot`` over the period that produced
    ``consumption_kwh`` and ``injection_kwh``.

    ``fee_proration`` scales the EUR/year fee components (1.0 for a
    full year, ``days_elapsed/days_in_year`` for YTD). ``prosumer_proration``,
    when given, overrides that for the prosumer term only: the live sensor and
    backfill prorate the prosumer fee per-month (each month's fee by its own
    days), so the YTD what-if passes the same per-month factor there to keep
    its absolute figure equal to the live ``current_year_cost`` sensor.
    ``include_capacity`` is forwarded to :func:`_annual_fees`; it exists for
    callers that want the per-kWh and fee terms without the Flanders capacity
    charge, and both the annual estimate and the YTD what-if keep it on so
    they match the live ``current_year_cost`` sensor.

    Solar handling honours the entry's configured regime:

    - ``"none"``: ``cost = consumption_kwh * per_kwh + fees``
    - ``"compensation"``: meter is netted 1:1 (Walloon pre-2024
      installations until 2030). The billable kWh is
      ``max(consumption - injection, 0)``; surplus injection is
      forfeited, never paid out. Fees include the prosumer charge.
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
        from .fees import _compute_prosumer

        prosumer_annual = 12.0 * _compute_prosumer(snapshot, entry)
        fees += prosumer_annual * (prosumer_proration / 12.0 - fee_proration)
    regime = entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    if regime == "compensation":
        billable = max(consumption_kwh - injection_kwh, 0.0)
        return fees + per_kwh * billable
    if regime == "injection" and injection_price is not None:
        return fees + per_kwh * consumption_kwh - injection_price * injection_kwh
    return fees + per_kwh * consumption_kwh


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
    lower bill than the sensor it sits next to."""
    from .fees import (
        _annual_static_fees,
        _compute_capacity,
        _compute_prosumer,
    )

    static = _annual_static_fees(snapshot, meter, entry)
    capacity = 0.0
    if include_capacity and entry.data.get(CONF_REGION) == REGION_FLANDERS:
        capacity = 12.0 * _compute_capacity(snapshot, entry, peak_kw)
    prosumer = 12.0 * _compute_prosumer(snapshot, entry)
    return static + capacity + prosumer


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

    Prefers the 4-register day/night wiring when both are filled (more
    accurate when the meter exposes them directly); falls back to the
    single cumulative sensor. Returns ``None`` when no sensor is wired
    or the recorder has nothing in the requested window -- the caller
    falls back to a default consumption assumption in that case so the
    quote page still renders."""
    from .energy_meters import _kwh_sensor_ids, _recorder_daily_kwh

    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    if day_id and night_id:
        d = await _recorder_daily_kwh(hass, day_id, start, end)
        n = await _recorder_daily_kwh(hass, night_id, start, end)
        total = sum(d.values()) + sum(n.values())
        return total if total > 0 else None
    if total_id:
        d = await _recorder_daily_kwh(hass, total_id, start, end)
        total = sum(d.values())
        return total if total > 0 else None
    return None
