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

"""Weighting a price curve by when the household actually uses power.

A spot-indexed contract is not billed at the mean of the day's prices: it is
billed at the mean weighted by consumption, and fed in at the mean weighted
by export, which are different curves and give different answers. The same
applies per register and per time-of-use slot. These are the weightings the
comparison and the bill share.
"""

from __future__ import annotations

from .const import (
    CONF_METER,
    CONF_REGION,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_BI,
    METER_DYNAMIC,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_FLANDERS,
)
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from homeassistant.util import dt as dt_util
from typing import Any


@lru_cache(maxsize=8)
def _tou_slot_hours(weekend_rule: str, year: int) -> tuple[dict[int, float], ...]:
    """Per slot, how many hours of ``year`` each clock hour spends in it.

    Counted by asking ``tou_slot`` itself for every hour of the year rather
    than writing the published windows out a second time. Three things follow
    from that which a hand-built weekly triple got wrong:

    * a seasonal rule is covered. Luminus SmartFlex moves its 11:00-17:00 block
      between super-creuses and creuses on 21 March and 20 September, so no
      single week describes it, and a week anchored in January (which is what
      this walked) put the whole midday block in creuses all year. That block
      is when a solar household exports.
    * federal holidays land in the slot the cards give them, which is the
      weekend one. Ten days a year is 2,7 hours a week off the weekday slots.
    * a rule added later is weighted correctly without anyone remembering to
      come back here. The triple covered two of the three rules that existed.

    Wall-clock arithmetic on a naive datetime, so the year is exactly 24 hours
    a day: this is a shape over clock hours, not a real calendar to bill, and a
    DST seam would only lose an hour of it. Cached per rule and year, since the
    ranking asks once a row and the answer moves only when the holidays do.
    """
    from .pricing import tou_slot

    by_slot: dict[str, dict[int, float]] = {
        "peak": dict.fromkeys(range(24), 0.0),
        "transition": dict.fromkeys(range(24), 0.0),
        "offpeak": dict.fromkeys(range(24), 0.0),
    }
    when = datetime.combine(date(year, 1, 1), time())
    end = datetime.combine(date(year + 1, 1, 1), time())
    while when < end:
        by_slot[tou_slot(when, weekend_rule)][when.hour] += 1.0
        when += timedelta(hours=1)
    return by_slot["peak"], by_slot["transition"], by_slot["offpeak"]


def _tou_slot_weights(
    weekend_rule: str, hour_weights: dict[int, float] | None = None
) -> tuple[float, float, float]:
    """Weight of each CWaPE TOU slot (peak, transition, offpeak).

    Without ``hour_weights``, hours a year each slot is active, counted off the
    contract's own rule by :func:`_tou_slot_hours`. Only the ratio is read, so
    the scale does not matter.

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
    credit uses. It is keyed by clock hour, so it multiplies straight into the
    per-hour counts rather than needing a walk of its own.
    """
    peak, transition, offpeak = _slot_weights(
        _tou_slot_hours(weekend_rule, dt_util.now().year), hour_weights
    )
    return peak, transition, offpeak


def _slot_weights(
    slots: tuple[dict[int, float], ...], hour_weights: dict[int, float] | None
) -> tuple[float, ...]:
    """Weight of each slot: its hours a year, or those hours weighted by the
    household's own export shape when it has one (see :func:`_tou_slot_weights`
    for why the two differ)."""
    if hour_weights is None:
        return tuple(sum(hours.values()) for hours in slots)
    weighted = tuple(
        sum(hour_weights.get(hour, 0.0) * count for hour, count in hours.items())
        for hours in slots
    )
    if sum(weighted) <= 0:
        # A wired meter that exported nothing: fall back rather than divide by
        # zero or return a credit built from an empty profile.
        return _slot_weights(slots, None)
    return weighted


@lru_cache(maxsize=16)
def _register_hours(
    region: str, year: int, meter: str, dso_mode: str
) -> tuple[dict[int, float], ...]:
    """Per meter register, how many hours of ``year`` each clock hour spends
    in it, counted off the same rule the engine bills by.

    The registers are whatever :func:`spot_stats._register_for` would put an
    hour in, and that is the point: the clamp below forfeits a register that
    ends the year negative, so it has to divide the year the way the meter
    does. A dedicated night circuit is one register, Tarif Impact is the
    three CWaPE bands whatever meter the supplier registers (an SMR3 meter
    counts per band, and a mono meter beside Impact is a real pair, which is
    what TotalEnergies Impact is), a bi-hourly or digital meter is day and
    night, and anything else is one.

    Restating the meter gate here instead let a mono Impact entry take the
    annual clamp, which lets one band pay off another, and a bi-hourly one
    split its year day/night while the engine split it three ways.
    """
    from .pricing import dso_impact_band, is_offpeak

    days = float((date(year + 1, 1, 1) - date(year, 1, 1)).days)
    if meter != METER_EXCLUSIVE_NIGHT and dso_mode == DSO_MODE_IMPACT:
        # The CWaPE bands are the same every day of the week, so each clock
        # hour belongs to exactly one and carries the whole year of it.
        bands: dict[str, dict[int, float]] = {
            band: dict.fromkeys(range(24), 0.0) for band in ("pic", "medium", "eco")
        }
        for hour in range(24):
            bands[dso_impact_band(datetime.combine(date(year, 1, 1), time(hour)))][
                hour
            ] = days
        return tuple(bands.values())
    if meter not in (METER_BI, METER_DYNAMIC):
        return (dict.fromkeys(range(24), days),)
    day: dict[int, float] = dict.fromkeys(range(24), 0.0)
    night: dict[int, float] = dict.fromkeys(range(24), 0.0)
    when = datetime.combine(date(year, 1, 1), time())
    end = datetime.combine(date(year + 1, 1, 1), time())
    while when < end:
        (night if is_offpeak(when, region) else day)[when.hour] += 1.0
        when += timedelta(hours=1)
    return day, night


def _register_weights(
    region: str,
    hour_weights: dict[int, float] | None = None,
    *,
    meter: str = METER_BI,
    dso_mode: str = DSO_MODE_BI_HORAIRE,
) -> tuple[float, ...]:
    """Weight of each meter register, the way :func:`_tou_slot_weights`
    weights the TOU slots. Two on a bi-hourly meter, three under Tarif
    Impact, one otherwise."""
    return _slot_weights(
        _register_hours(region, dt_util.now().year, meter, dso_mode), hour_weights
    )


def _hour_weighted_mean(
    samples: Iterable[tuple[datetime, float]],
    hour_weights: dict[int, float] | None,
) -> float | None:
    """Mean of per-slot values weighted by the household's own hourly shape.

    ``hour_weights`` is the measured share of kWh falling in each hour of the
    local day, for whichever side is being priced. Without one every slot
    weighs the same, which assumes a household that draws, or exports,
    uniformly around the clock. Returns ``None`` when nothing carries weight,
    so a caller can fall back rather than divide by zero.
    """
    num = 0.0
    den = 0.0
    for when, value in samples:
        weight = (
            1.0
            if hour_weights is None
            else hour_weights.get(dt_util.as_local(when).hour, 0.0)
        )
        if weight <= 0.0:
            continue
        num += weight * value
        den += weight
    return num / den if den > 0.0 else None


def _consumption_weighted_spot(
    spot_dict: dict[datetime, float],
    hour_weights: dict[int, float] | None,
) -> float | None:
    """The spot to price a per-slot ENERGY leg at over the fetched window.

    A dynamic contract's bill is the sum over slots of ``kWh * (factor*spot +
    base)``, so the one spot that stands for the year is the mean weighted by
    when the household actually draws, not by the clock. The two differ
    because consumption is evening-heavy while the day-ahead curve troughs at
    midday: measured over Jan to Aug 2026 the clock mean was 100,24 EUR/MWh
    against 101,81 on a residential shape.

    Falls back to the clock mean without a measured shape, which is what every
    quote used before, and to ``None`` on an empty window.
    """
    if not spot_dict:
        return None
    weighted = _hour_weighted_mean(spot_dict.items(), hour_weights)
    if weighted is not None:
        return weighted
    return _hour_weighted_mean(spot_dict.items(), None)


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
    rates = [
        (when, rate)
        for when, spot in spot_dict.items()
        if (rate := _floor_injection(inj.factor * spot + inj.base, inj)) is not None
    ]
    weighted = _hour_weighted_mean(rates, hour_weights)
    if weighted is not None:
        return weighted
    # A measured shape that exports in none of the hours this window covers.
    # Fall back to the clock mean rather than to no credit at all.
    return _hour_weighted_mean(rates, None)


def _compare_injection_credit(
    snapshot: Any,
    entry: Any,
    spot_dict: dict[datetime, float],
    avg_spot: float | None,
    month_spot: float | None = None,
    inj_hour_weights: dict[int, float] | None = None,
    raw_snapshot: Any = None,
    meter: str | None = None,
    credit_spots: dict[datetime, float] | None = None,
) -> float | None:
    """Injection credit (EUR/kWh) for the compare flow's annual estimate.

    A per-slot TOU injection (Engie Empower Flextime) is averaged over the
    slots by the household's own measured export shape, which is the basis the
    live credit uses; delegating to the live helper would instead return the
    dialog-open slot rate and bias the credit. Without a measurement it falls
    back to the published slot durations, which under-credit because the
    overnight off-peak block occupies a third of the clock and exports nothing.
    A day/night REGISTER pair (Trevion Groene Energie Vast, flagged
    ``bi_hourly``) is averaged the same way over the two registers of the
    region's schedule, on the meter the quote is for: ``meter`` when the page
    overrides it, the entry's otherwise. It used to reach the live helper too,
    which answers the register the clock is in, so the credit for a whole
    year's export moved by a third between a weekday afternoon and a Sunday. A
    spot-indexed injection is priced per slot over the window and averaged by
    the household's export shape, the same basis as the TOU branch and the
    same one ``current_year_cost`` bills on. It deliberately does NOT follow
    the energy term onto the plain window mean: the credit multiplies exported
    kWh, and export is not spread evenly around the clock. Pricing it off the
    live current slot would be worse still, since the credit and the energy
    cost would reflect different instants. Three shapes qualify: any
    dynamic-energy contract, a card that prints no indicative at all (Cociter
    Variable), and a card that prints one but settles per slot anyway, which
    is every Bolt fixed and variable card.

    ``credit_spots`` is the closed days of day-ahead held for the year
    (``compare_inputs._credit_spots``). A static card's spot-indexed credit is
    priced on it when given, since the one rate multiplies a whole year of
    export; ``spot_dict`` is the day or two the page fetched, which moved a
    recorded projection by tens of euro a day. A dynamic card keeps
    ``spot_dict``, the window its energy leg is priced on.

    A MONTH-INDEXED credit resolves against ``month_spot``, the delivery
    month's mean: the solar-weighted one for a card that names Belpex_SPP
    (energie.be Variabel and Vast, Ecofix Flexy, EBEM Variabel and B@sic+,
    Ecopower, Energy Knights Essentia, EnergyVision, OCTA+ Fixed Impact,
    DATS 24), the plain arithmetic one for a card that names that instead
    (Eneco's Belpex-injectie, Engie's and Luminus' EPEXDAM cards,
    TotalEnergies Impact). WHICH mean is the caller's business, because only
    it knows the side and can resolve it; this only requires that a mean was
    named. Without one there is no honest resolution: for an SPP card the
    plain mean is a DIFFERENT index, not a coarser one, so that case falls
    through to the printed indicative below.

    Delegating a month-indexed credit to the live helper instead does NOT
    work, and reading that it does is what left this branch SPP-only: the
    helper answers the printed indicative unless the snapshot has already had
    the month baked into it, and only the coordinator bakes. The page then
    quoted last month's index beside a sensor showing this month's, by
    ``factor`` times the gap between the two.
    """
    from .injection import (
        _bake_monthly_injection,
        _compute_injection_price,
        _floor_injection,
        _injection_bakes_to_month_mean,
        _tou_weekend_rule,
    )
    from .providers._rates import DynamicRates, InjectionRates

    raw = snapshot if raw_snapshot is None else raw_snapshot
    # The tick's rule, on the priced leg with the raw card as today's.
    bakes = _injection_bakes_to_month_mean(snapshot, raw, entry)
    if month_spot is not None and bakes:
        # Resolve the month index the way the coordinator resolves it for the
        # live sensor, by calling the same helper, so the two agree band by
        # band rather than by a rule written out twice. Asked of the RAW card
        # for the reason the per-slot branch below is: a cohort re-price puts a
        # SpotMonthlyRates leg on a contract whose feed-in still varies per
        # hour, and baking that to a month mean is the one thing this must not
        # do. It leaves a per-slot triplet resolved on the month and a
        # coefficient pair collapsed into ``current``, which the branches below
        # then read as printed rates.
        snapshot = _bake_monthly_injection(snapshot, month_spot)
    inj: InjectionRates | None = getattr(snapshot, "injection", None)
    energy = getattr(snapshot, "energy", None)
    weekend_rule = _tou_weekend_rule(energy)
    if (
        inj is not None
        and weekend_rule is not None
        and inj.peak is not None
        and inj.transition is not None
        and inj.offpeak is not None
    ):
        wp, wt, wo = _tou_slot_weights(weekend_rule, inj_hour_weights)
        # Each slot floored before it is weighted, as the live and historical
        # credits floor it: the mean of floored rates, not the floor of theirs.
        rates = [
            _floor_injection(r, inj) for r in (inj.peak, inj.transition, inj.offpeak)
        ]
        return float((rates[0] * wp + rates[1] * wt + rates[2] * wo) / (wp + wt + wo))
    if (
        inj is not None
        and weekend_rule is None
        and inj.bi_hourly
        and inj.peak is not None
        and inj.offpeak is not None
    ):
        if meter is None:
            meter = entry.data.get(CONF_METER, METER_MONO)
        if meter not in (METER_BI, METER_DYNAMIC):
            # One register: the card's own rate for it, as the live path.
            return _floor_injection(inj.current, inj)
        # The CARD's own day and night injection rates, which follow the
        # day/night schedule whatever the DSO mode is, so this asks for the
        # two registers by name rather than taking whatever the meter has.
        wd, wn = _register_weights(
            entry.data.get(CONF_REGION, REGION_FLANDERS),
            inj_hour_weights,
            meter=METER_BI,
            dso_mode=DSO_MODE_BI_HORAIRE,
        )
        day, night = (_floor_injection(r, inj) for r in (inj.peak, inj.offpeak))
        return float((day * wd + night * wn) / (wd + wn))
    if (
        inj is not None
        and inj.factor is not None
        and inj.base is not None
        # The guard _injection_is_spot_formula opens with, and this branch
        # dropped: month and solar-weighted coefficients are never a per-hour
        # formula, whatever else is true. Without it a month-indexed leg whose
        # card stopped printing its indicative would be quoted at the plain
        # window mean, which is the substitution `strict` refuses on every
        # other path. Unreachable today, because all 772 month or SPP indexed
        # rows in the archive print a current, and that is exactly the state
        # the 0.6.7 mis-credit was silent in.
        and not inj.month_indexed
        and not inj.spp_indexed
        and (
            isinstance(energy, DynamicRates)
            or inj.current is None
            # A card that settles per slot prints its indicative as an
            # illustration, so the formula wins over it here just as it does
            # in ``_injection_is_spot_formula``, which this branch mirrors.
            # Without the clause every Bolt fixed and variable card fell past
            # this branch into the live helper at the bottom, which resolves
            # the credit at whichever slot the dialog happened to open in and
            # so valued a whole year of export at one hour's spot.
            or inj.slot_indexed
        )
    ):
        if credit_spots and not isinstance(energy, DynamicRates):
            spot_dict = credit_spots
            avg_spot = sum(credit_spots.values()) / len(credit_spots)
        if avg_spot is None:
            return None
        # Asked of the RAW, pre-splice snapshot when the caller has one. The
        # compare page splices a cohort's SpotMonthlyRates energy leg onto the
        # current side, so a Cociter Variable entry arrives here looking
        # month-mean priced while its injection is still the hourly BELPEX
        # formula the card describes - note (9) "le prix de l'injection varie
        # chaque heure" against note (7)'s monthly consumption. Judged on the
        # spliced snapshot the credit fell onto the window mean and the page
        # quoted 0,07959 EUR/kWh where the live tick, the year-to-date walk
        # and the backfill all say 0,05023: it understated the user's own bill
        # and so biased the comparison toward staying put.
        if spot_dict and not bakes:
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


def _year_avg_all_in(
    snapshot: Any,
    dso: str,
    region: str,
    first_day: date,
    num_days: int,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
    hour_weights: dict[int, float] | None = None,
    component: str = "all_in",
) -> float | None:
    """Mean all-in EUR/kWh over the ``num_days`` from ``first_day``, priced
    once per kind of day. ``component`` names another field of the breakdown
    to average instead, ``energy`` for the supplier's component alone.

    Every hour has to carry its true energy slot AND network band, since the
    TOU windows and the bi-horaire network bands do not align and both change
    on weekends; sampling a whole day does that, where one sample per slot
    assigned one network band to a whole energy slot and mis-priced it.

    Which days, and how many of each, is what this gets right that a
    representative week could not. The rate depends on the calendar only
    through the weekday, whether the day is a public holiday (billed under
    the weekend rule for both energy and network) and, for a seasonal card,
    the season. So the window is walked day by day, the days are grouped on
    exactly those three, one date per group is priced hour by hour, and each
    group counts for the days it holds. That is the full year priced in at
    most 28 days of breakdowns. A holiday-free week priced the ten weekday
    holidays Belgium bills as weekend days at their weekday rate, 0,33% high
    on a weekend-offpeak card against the hour-weighted year; a week that
    contained one was worse the other way, which is why the old code walked
    back until it found none.

    ``hour_weights`` is the household's measured share of consumption per hour
    of the day. With it, each hour carries the kWh actually recorded in it,
    which is how the bill beside this figure is computed. Without it the hours
    weigh equally, which assumes a household that consumes uniformly around
    the clock. Returns None on any compute failure so the caller can fall back.
    """
    from .pricing import _is_smartflex_summer, compute_breakdown, is_belgian_holiday

    counts: dict[tuple[bool, int, bool], int] = {}
    representative: dict[tuple[bool, int, bool], date] = {}
    for offset in range(num_days):
        day = first_day + timedelta(days=offset)
        key = (_is_smartflex_summer(day), day.weekday(), is_belgian_holiday(day))
        counts[key] = counts.get(key, 0) + 1
        representative.setdefault(key, day)
    total = 0.0
    weight_sum = 0.0
    for key, days in counts.items():
        midnight = datetime.combine(
            representative[key], time(), tzinfo=dt_util.get_default_time_zone()
        )
        for hour in range(24):
            # Wall-clock arithmetic on purpose: the breakdown reads the local
            # hour, and a seam day still yields 24 distinct ones this way.
            when = midnight + timedelta(hours=hour)
            try:
                bd = compute_breakdown(
                    snapshot, dso, region, when, spot, meter, dso_mode
                )
            except Exception:  # noqa: BLE001
                return None
            w = (1.0 if hour_weights is None else hour_weights.get(hour, 0.0)) * days
            total += float(getattr(bd, component)) * w
            weight_sum += w
    return total / weight_sum if weight_sum else None


def _tou_weighted_per_kwh(
    snapshot: Any,
    dso: str,
    region: str,
    when_now: datetime,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
    hour_weights: dict[int, float] | None = None,
    component: str = "all_in",
) -> float | None:
    """Per-kWh EUR/kWh for the compare flow's annual estimate, with a
    TOU-aware weighted average when the snapshot's energy rate splits by
    hour-of-day.

    ``component`` selects another field of the breakdown, weighted the same
    way: ``energy`` gives the supplier's component alone, which is what a
    welcome credit is capped against.

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
    the hour the user opens the dialog: biased. So the coming year is
    walked and priced per kind of day (``_year_avg_all_in``), which is the
    exact hour-weighted annual figure rather than a sample of it.

    Returns ``None`` on compute failure so the caller can render '-'
    on the result page rather than tear the flow down.
    """
    from .injection import _tou_weekend_rule
    from .pricing import (
        compute_breakdown,
    )
    from .providers._rates import ImpactRates

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
        # A monthly-indexed card splits by hour too, but it prints a
        # COEFFICIENT pair per meter rather than a rate pair, so it carries
        # factor_peak / factor_offpeak and has no peak / offpeak at all. Energy
        # Knights Essentia is the first card that reaches here with them: its
        # bands are 1,1077 against 1,05682, worth 0,0066 EUR/kWh, so quoting
        # whichever hour the dialog opened in swung the annual estimate by
        # 23 EUR at 3500 kWh. Fluvius publishes no day / night distribution
        # split either, so the overlay disjunct below cannot stand in for it.
        or (
            getattr(snapshot.energy, "factor_peak", None) is not None
            and getattr(snapshot.energy, "factor_offpeak", None) is not None
        )
        or (
            overlay is not None
            and getattr(overlay, "distribution_peak", None) is not None
            and getattr(overlay, "distribution_offpeak", None) is not None
        )
    )
    impact_network = dso_mode == "impact"
    # The card's TimeOfUseRates or the month-mean leg it re-prices through:
    # both carry the weekend rule the slot mix is weighted on.
    weekend_rule = _tou_weekend_rule(snapshot.energy)
    if (
        weekend_rule is None
        and not isinstance(snapshot.energy, ImpactRates)
        and not bi_split
        and not impact_network
    ):
        return float(getattr(bd, component))

    # A TOU energy slot spans hours with different bi-horaire network bands,
    # the weekend rule shifts hours between energy slots, a seasonal card
    # moves its bands twice a year, and an Impact connection bands the network
    # on three CWaPE bands every day of the week while the bi-horaire one has
    # two and rests on weekends. One walk over the coming year, priced per
    # kind of day, carries all of that at once; see _year_avg_all_in for why a
    # representative week could not. The bi-hourly split used to take a
    # two-sample shortcut here (one breakdown per band, weighted by band
    # hours), which is exact only while BOTH legs have two bands: put an
    # Impact connection under it and the three network bands were priced at
    # whichever two hours the samples fell in, 11% high on a Walloon fixed
    # card. The walk costs at most a few hundred breakdowns and needs no such
    # assumption.
    year_avg = _year_avg_all_in(
        snapshot,
        dso,
        region,
        when_now.date(),
        365,
        spot,
        meter,
        dso_mode,
        hour_weights,
        component=component,
    )
    return year_avg if year_avg is not None else float(getattr(bd, component))
