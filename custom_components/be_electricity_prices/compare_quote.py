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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from homeassistant.util import dt as dt_util
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


def _tou_slot_weights(
    weekend_rule: str, hour_weights: dict[int, float] | None = None
) -> tuple[float, float, float]:
    """Weight of each CWaPE TOU slot (peak, transition, offpeak).

    Without ``hour_weights``, hours-per-week each slot is active, from the
    published rules and a 5-weekday / 2-weekend split. Engie Empower Flextime
    keeps the weekday transition/offpeak windows on weekends
    (``weekend_no_peak``); Luminus SmartFlex makes weekends fully off-peak
    (``weekend_offpeak``, the default).

    Duration is the right weighting for a quantity that flows evenly through
    the day and the wrong one for solar export, which is zero for the whole
    01:00-07:00 off-peak block. That block carries about a third of the clock
    weight, so a per-slot feed-in credit averaged this way always under-credits
    against what the year-to-date walk pays, which resolves each hour's own
    slot and multiplies by that hour's exported kWh. Measured over a year of
    modelled Brussels export on an Engie Empower Flextime card, the gap was
    11,22 EUR on 3500 kWh, one-directional.

    ``hour_weights`` is the household's own measured export shape per hour of
    the day, which replaces the duration mean with the same basis the live
    credit uses.
    """
    if hour_weights is None:
        if weekend_rule == "weekend_no_peak":
            return 45.0, 69.0, 54.0
        return 45.0, 45.0, 78.0
    from .pricing import tou_slot

    # Walk one representative week so each hour lands in the slot the weekday
    # and weekend rules actually put it in, then carry that hour's share of
    # the household's export.
    monday = datetime.combine(
        date(2026, 1, 5), time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
    )
    acc = {"peak": 0.0, "transition": 0.0, "offpeak": 0.0}
    for hour in range(7 * 24):
        when = monday + timedelta(hours=hour)
        acc[tou_slot(when, weekend_rule)] += hour_weights.get(when.hour, 0.0)
    total = sum(acc.values())
    if total <= 0:
        # A wired meter that exported nothing: fall back rather than divide by
        # zero or return a credit built from an empty profile.
        return _tou_slot_weights(weekend_rule)
    return acc["peak"], acc["transition"], acc["offpeak"]


def _export_weighted_credit(
    inj: Any,
    spot_dict: dict[datetime, float],
    hour_weights: dict[int, float] | None,
) -> float | None:
    """Spot-indexed feed-in credit averaged over the window's slots by the
    household's own export shape.

    ``_annual_bill`` multiplies ONE rate by the whole year's exported kWh, so
    the rate that stands for the bill is the export-weighted mean of the slot
    rates. Solar exports nothing through the night and peaks at midday, which
    on a day-ahead curve is where the price troughs and where a never-negative
    clamp bites, so the clock mean and the export mean are far apart: on a
    spring curve with a negative midday block, 4,75 c/kWh against 1,73. It is
    the argument the TOU branch below already makes, applied to the shape that
    the rate varies on here.

    It is also the basis ``current_year_cost`` bills on, since that multiplies
    each hour's own kWh by that hour's own rate, so the estimate and the
    sensor printed beside it stop answering different questions.

    Without a measured shape every slot weighs the same, and for an unclamped
    formula that is exactly ``factor * the window mean + base``, so an entry
    with no injection history is quoted what it always was. Returns ``None``
    when there is nothing to average.
    """
    from .injection import _floor_injection

    if inj.factor is None or inj.base is None:
        return None
    num = 0.0
    den = 0.0
    for when, spot in spot_dict.items():
        weight = (
            1.0
            if hour_weights is None
            else hour_weights.get(dt_util.as_local(when).hour, 0.0)
        )
        if weight <= 0.0:
            continue
        rate = _floor_injection(inj.factor * spot + inj.base, inj)
        if rate is None:
            continue
        num += weight * rate
        den += weight
    if den > 0.0:
        return num / den
    if hour_weights is None:
        return None
    # A measured shape that exports in none of the hours this window covers.
    # Fall back to the clock mean rather than to no credit at all.
    return _export_weighted_credit(inj, spot_dict, None)


def _compare_injection_credit(
    snapshot: Any,
    entry: Any,
    spot_dict: dict[datetime, float],
    avg_spot: float | None,
    spp_spot: float | None = None,
    inj_hour_weights: dict[int, float] | None = None,
) -> float | None:
    """Injection credit (EUR/kWh) for the compare flow's annual estimate.

    A per-slot TOU injection (Engie Empower Flextime) is averaged over the
    slots by the household's own measured export shape, which is the basis the
    live credit uses; delegating to the live helper would instead return the
    dialog-open slot rate and bias the credit. Without a measurement it falls
    back to the published slot durations, which under-credit because the
    overnight off-peak block occupies a third of the clock and exports nothing. A
    spot-indexed injection (Cociter Variable, or any dynamic-energy
    contract) is priced per slot over the window and averaged by the
    household's export shape, the same basis as the TOU branch and the same
    one ``current_year_cost`` bills on. It deliberately does NOT follow the
    energy term onto the plain window mean: the credit multiplies exported
    kWh, and export is not spread evenly around the clock. Pricing it off the
    live current slot would be worse still, since the credit and the energy
    cost would reflect different instants.

    An SPP-INDEXED credit (energie.be Variabel and Vast) resolves against
    ``spp_spot``, the solar-weighted month mean, because that is the index
    its card names and the number the live sensor and the YTD walk use;
    quoting the card's printed indicative here instead made the page
    contradict the user's own injection_price sensor. Without the Synergrid
    profile there is no honest resolution -- the plain window mean is a
    DIFFERENT index, not a coarser one -- so that case falls through to the
    printed indicative below. Every other monthly-indexed injection is
    spot-independent and delegates to the live helper too.
    """
    from .injection import _compute_injection_price, _floor_injection
    from .providers.base import DynamicRates, TimeOfUseRates
    from .spot_stats import _injection_on_month_mean

    inj = getattr(snapshot, "injection", None)
    energy = getattr(snapshot, "energy", None)
    if (
        inj is not None
        and isinstance(energy, TimeOfUseRates)
        and inj.peak is not None
        and inj.transition is not None
        and inj.offpeak is not None
    ):
        wp, wt, wo = _tou_slot_weights(energy.weekend_rule, inj_hour_weights)
        return float(
            (inj.peak * wp + inj.transition * wt + inj.offpeak * wo) / (wp + wt + wo)
        )
    if (
        inj is not None
        and inj.spp_indexed
        and inj.factor is not None
        and inj.base is not None
        and spp_spot is not None
    ):
        return _floor_injection(inj.factor * spp_spot + inj.base, inj)
    if (
        inj is not None
        and inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    ):
        if avg_spot is None:
            return None
        if spot_dict and not _injection_on_month_mean(snapshot):
            # Priced per slot and averaged by when the panels export, because
            # that is what the year's exported kWh is billed at. Evaluating
            # the formula once at the window mean instead answers a different
            # question twice over: it weighs every hour of the clock alike,
            # and for a clamped formula, which is convex, the rate of the mean
            # is not even the mean of the rates.
            credit = _export_weighted_credit(inj, spot_dict, inj_hour_weights)
            if credit is not None:
                return credit
        # A month-mean index keeps the single evaluation on purpose: such a
        # contract publishes ONE tariff for the delivery month and the
        # never-negative guarantee is written against that number, not against
        # each hour, so there is no per-slot rate to average.
        return _floor_injection(inj.factor * avg_spot + inj.base, inj)
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
    hour_weights: dict[int, float] | None = None,
) -> float | None:
    """Mean all-in EUR/kWh over ``num_days`` from ``start``.

    Sampling every hour lets each hour carry its true energy slot AND network
    band, so the TOU energy windows and the bi-horaire network bands - which
    don't align, and both differ on weekends - are each weighted correctly.
    A three-sample-per-slot weighting instead assigns one network band to a
    whole energy slot and mis-prices it. Returns None on any compute failure.

    ``hour_weights`` is the household's measured share of consumption per hour
    of the day. With it, each hour carries the kWh actually recorded in it,
    which is how the bill beside this figure is computed. Without it the hours
    weigh equally, which assumes a household that consumes uniformly around
    the clock.
    """
    from .pricing import compute_breakdown

    total = 0.0
    count = 0.0
    for hour in range(num_days * 24):
        when = start + timedelta(hours=hour)
        try:
            bd = compute_breakdown(snapshot, dso, region, when, spot, meter, dso_mode)
        except Exception:  # noqa: BLE001
            return None
        weight = 1.0 if hour_weights is None else hour_weights.get(when.hour, 0.0)
        total += bd.all_in * weight
        count += weight
    return total / count if count else None


def _tou_weighted_per_kwh(
    snapshot: Any,
    dso: str,
    region: str,
    when_now: datetime,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
    hour_weights: dict[int, float] | None = None,
) -> float | None:
    """Per-kWh EUR/kWh for the compare flow's annual estimate, with a
    TOU-aware weighted average when the snapshot's energy rate splits by
    hour-of-day.

    ``hour_weights`` is the household's measured share of consumption per hour
    of the day (:func:`energy_meters._measured_hour_weights`). Weighting the
    slot rates by CLOCK hours instead assumes a household that consumes
    uniformly around the clock, which none does: measured on a residential
    profile the peak band carried 0,56 of the kWh against the 0,38 of the week
    its hours occupy, so a peak-expensive card was quoted well under what that
    same household is billed by the sensor sitting next to this figure. Absent
    a measurement the hours weigh equally, which is the old behaviour and the
    only honest fallback.

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
            hours = 0.0
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
                weight = (
                    float(len(band_hours))
                    if hour_weights is None
                    else sum(hour_weights.get(h, 0.0) for h in band_hours)
                )
                weighted += band_bd.all_in * weight
                hours += weight
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
        peak_weight = 0.0
        off_weight = 0.0
        for day_offset in range(7):
            for hour in range(24):
                when = base + timedelta(days=day_offset, hours=hour)
                w = 1.0 if hour_weights is None else hour_weights.get(hour, 0.0)
                if is_offpeak(when, region):
                    off_when = off_when or when
                    off_weight += w
                else:
                    peak_when = peak_when or when
                    peak_weight += w
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
        total_weight = peak_weight + off_weight
        if total_weight <= 0:
            return bd.all_in
        return (
            bd_peak.all_in * peak_weight + bd_off.all_in * off_weight
        ) / total_weight

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
                snapshot,
                dso,
                region,
                season_monday,
                7,
                spot,
                meter,
                dso_mode,
                hour_weights,
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
        snapshot, dso, region, week_start, 7, spot, meter, dso_mode, hour_weights
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


_VOLUMES_CLAUSE = (
    " Yearly volumes entered by hand, so the year-to-date rows are left "
    "blank: they replay measured meter history, not the figures typed."
)


def _regime_label(regime: str) -> str:
    """Solar regime in the words the result page and the what-if step use.

    Deliberately not the selector's own translated option: these render
    inside a sentence, where "Compensation regime (Wallonia, certified
    before 2024-01-01, until 2030)" does not fit. Every call site prefixes
    a definite article, so each label has to read after "the".
    """
    if regime == SOLAR_REGIME_COMPENSATION:
        return "compensation regime"
    if regime == SOLAR_REGIME_INJECTION:
        return "injection tariff"
    return "no-solar regime"


def _whatif_note(
    base_note: str,
    *,
    stored_regime: str,
    regime: str,
    baseline_eur: float | None,
    whatif_eur: float | None,
    volumes_typed: bool,
    missing_kva: bool = False,
) -> str:
    """The solar note, prefixed and qualified when a what-if regime is in
    play.

    Returns ``base_note`` untouched when the quote runs on the entry's own
    regime, so an ordinary comparison reads exactly as before.

    The baseline clause exists because the compare branch cannot quote a
    user against their own contract: the picker excludes it. With the
    regime moving both sides together, the printed supplier delta barely
    shifts, and the number the user actually came for, their own contract
    under the other regime, would otherwise appear nowhere.
    """
    if regime == stored_regime:
        # Typed volumes blank the year-to-date rows on their own, without
        # any regime change, so the sentence that explains the blank rows
        # cannot hang off the regime having moved.
        return f"{base_note}{_VOLUMES_CLAUSE}".lstrip() if volumes_typed else base_note
    note = (
        f"what-if: both sides quoted on the {_regime_label(regime)} "
        f"(your entry is on the {_regime_label(stored_regime)})."
    )
    if base_note:
        note += f" {base_note}."
    if baseline_eur is not None and whatif_eur is not None:
        delta = whatif_eur - baseline_eur
        note += (
            f" On your own contract that is {baseline_eur:.2f} EUR/year as "
            f"configured versus {whatif_eur:.2f} EUR/year under the "
            f"{_regime_label(regime)} ({'+' if delta >= 0 else ''}{delta:.2f} "
            "EUR/year)."
        )
    if volumes_typed:
        note += _VOLUMES_CLAUSE
    if missing_kva and regime == SOLAR_REGIME_COMPENSATION:
        # The prosumer fee is billed per kVA of inverter, so an entry that
        # never set one quotes the compensation regime without it. Say so
        # rather than print a figure that is short by a few hundred euros.
        note += (
            " No inverter capacity is set on this entry, so no Walloon "
            "prosumer fee is included and the figure is that much too low."
        )
    return note + " Your entry is unchanged."


def _uncredited_note(snapshot: Any, label: str) -> str:
    """Why one side of the quote credits nothing for the injected kWh.

    ``_compare_injection_credit`` returns None for two different reasons:
    the card publishes no injection tariff at all, or a spot-indexed
    injection had no day-ahead window to price against. The first is the
    true bill with that supplier; the second understates the credit. Both
    fall through to the no-credit branch of ``_annual_bill``, so without a
    note on the page the two are indistinguishable from a supplier that
    genuinely pays nothing.
    """
    if getattr(snapshot, "injection", None) is None:
        return f"{label} publishes no injection tariff, so nothing is credited there"
    return (
        f"{label}'s injection is spot-indexed and no day-ahead price was "
        "available, so nothing is credited there"
    )


def _solar_note(
    regime: str, rolling_inj_kwh: float, uncredited: Sequence[str] = ()
) -> str:
    """One-line description of how solar is folded into the comparison.

    Renders into the result form's description placeholder. Empty for
    the no-solar case so the page doesn't show a misleading label.
    ``uncredited`` carries one clause per side whose injection could not
    be priced, so the page never claims a credit it did not apply."""
    if regime == "compensation":
        if rolling_inj_kwh > 0:
            return f"compensation regime: meter netted (consumption -= {rolling_inj_kwh:.0f} kWh, surplus forfeited)"
        return "compensation regime configured but no injection sensor wired - net = consumption"
    if regime == "injection":
        if rolling_inj_kwh > 0:
            note = f"injection regime: {rolling_inj_kwh:.0f} kWh credited at each supplier's injection price"
            for reason in uncredited:
                note += f" - {reason}"
            return note
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
    capacity_proration: float | None = None,
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
    ``capacity_proration`` does the same for the Flanders capacity tariff,
    which the live sensor also accrues per month rather than uniformly.

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
    if capacity_proration is not None and include_capacity:
        # Same correction for the Flanders capacity tariff, and for the same
        # reason: _ytd_capacity accrues each month by its OWN length
        # (days_in_ytd / days_in_full_month), while fee_proration is a uniform
        # days_elapsed / days_in_year. The two drift inside the year -- a
        # February close measured 36,37 against the live sensor's 37,50 -- and
        # the what-if is meant to be comparable to that sensor to the cent.
        # Counted in MONTHS (0..12), like prosumer_proration.
        from .fees import _compute_capacity

        if entry.data.get(CONF_REGION) == REGION_FLANDERS:
            capacity_annual = 12.0 * _compute_capacity(snapshot, entry, peak_kw)
            fees += capacity_annual * (capacity_proration / 12.0 - fee_proration)
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

    Thin wrapper over :func:`energy_meters._measured_kwh` so there is one
    recorder-read shape rather than two that can drift. Returns ``None`` for a
    total of zero or less, which is what the year-to-date and injection call
    sites treat as "nothing to bill". That conflates "no sensor wired" with
    "wired and reads zero"; callers that need to tell those apart go through
    :func:`_annual_volume` instead, which carries the coverage."""
    from .energy_meters import _measured_kwh

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

    Below the floor it falls back to the volume typed on the entry, which only
    professional entries carry, and finally to the household default.

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
    from .energy_meters import _measured_kwh

    measured = await _measured_kwh(hass, entry, start, end)
    days = measured.days_with_data
    if measured.kwh > 0 and _covers_a_year(days):
        # Scaled across whatever few days are missing. At this coverage the
        # correction is under 5% and carries no seasonal bias worth the name.
        kwh = measured.kwh * MEASURED_FULL_YEAR_DAYS / days
        return _AnnualVolume(kwh, days, f"measured ({days} days)")
    if measured.kwh > 0 and days >= MEASURED_MIN_DAYS:
        return _AnnualVolume(
            measured.kwh * MEASURED_FULL_YEAR_DAYS / days,
            days,
            f"scaled from {days} days, not seasonally corrected",
        )
    typed = entry.data.get(CONF_ANNUAL_CONSUMPTION_KWH)
    if typed:
        return _AnnualVolume(
            float(typed), days, f"entered on the entry ({float(typed):.0f} kWh/year)"
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
