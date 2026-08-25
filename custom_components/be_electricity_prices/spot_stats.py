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

"""Spot-price statistics: monthly means, the SPP weighting and the slot lookup.

Split out of coordinator.py. A true leaf. The future-drop rule lives with the
means it protects: a mean that includes tomorrow's published curve is pulled
toward a day that has not been billed."""

from __future__ import annotations

from datetime import UTC
from datetime import date
from datetime import datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from statistics import fmean

from .const import (
    CONF_CONTRACT,
    CONF_CUSTOM_INJECTION_MODE,
    CONF_CUSTOM_INJECTION_SPP_WEIGHTED,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_INJECTION_MODE_FORMULA,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SOLAR_REGIME_INJECTION,
    SUPPLIER_CUSTOM,
)
from .pricing import (
    slot_start,
)
from .providers.base import (
    DynamicRates,
    EnergyRates,
    SpotMonthlyRates,
    SupplierSnapshot,
)
from .synergrid import (
    SppWeights,
)


def _energy_is_quarter_hourly(energy: EnergyRates) -> bool:
    """True when the energy model bills on the native 15-minute grid.

    Engie, Cociter, EBEM, Ecofix, OCTA+ and Ecopower (Dynamische
    Burgerstroom) dynamic contracts set ``quarter_hourly`` (their cards
    price on the 15-minute Belpex / eSpot_15 / Epex 15 / EPEX DA spot);
    every other contract (static, TOU, hourly-billed dynamic such as
    Eneco, Frank, Luminus, Mega, TotalEnergies) stays hourly.
    """
    return isinstance(energy, DynamicRates) and energy.quarter_hourly


def _group_spot_quarters_by_hour(
    spots: dict[datetime, float],
) -> dict[datetime, list[float]]:
    """Group a fetched spot curve by clock hour, keeping each slot's own price.

    The hour is the key because that is what every reader asks for: the
    recorder keeps hourly consumption, so a replay can only ever ask what a
    whole hour cost. Keying the slots by hour rather than by slot is also what
    makes the quarter cache affordable to persist, since the ISO timestamp
    costs more than the four numbers it carries.

    An hour holds between one and four values. ENTSO-E answers a PT15M request
    with the PT60M series where no 15-minute one was published (``api.py:159``),
    a week-chunk can stop mid-hour, and the carry-forward rule can leave a slot
    unspecified. Nothing here requires four: the mean of whatever the hour
    holds is still the best answer about that hour, and a single value
    degenerates to the plain hourly price.

    Iteration follows ``spots`` rather than sorting it, so the grouped means
    are bit-identical to the ones this package has always computed.
    """
    out: dict[datetime, list[float]] = {}
    for when, value in spots.items():
        out.setdefault(when.replace(minute=0, second=0, microsecond=0), []).append(
            value
        )
    return out


def _bucket_spots_by_hour(spots: dict[datetime, float]) -> dict[datetime, float]:
    """Collapse a fetched spot curve onto one price per clock hour, by mean.

    A quarter-hourly contract settles on the 15-minute series, so that is what
    its replay has to be priced off. The recorder only keeps hourly
    consumption, though, so the replay can only ever ask what a whole hour
    cost. The mean of that hour's quarters is the exact answer to that question
    for a formula that is LINEAR in the spot, which every energy formula and
    every unfloored injection formula is: pricing the hour's mean is then
    identical to replaying each quarter against a quarter of the hour's kWh.

    Collapsing at the fetch keeps the historical cache hourly, which is what
    its 20-of-24 completeness test, its persisted form and every reader in the
    year-to-date walk and the backfill already assume.

    One formula is not linear. ``floor_at_zero`` makes the injection rate
    convex, so the mean of the floored quarters is at least the floored mean,
    and flooring once at the hour credits less than the live per-slot array
    whenever the spot crosses the floor inside the hour. That entry keeps the
    hour's own quarters alongside this mean, in
    ``coordinator._historical_spot_quarters``, and replays the credit off them;
    see :func:`~.injection._injection_needs_spot_quarters`.
    """
    return {
        hour: fmean(values)
        for hour, values in _group_spot_quarters_by_hour(spots).items()
    }


# A spot cache grouped by local (year, month) so several months' means can be
# read without rescanning the whole year (and re-localising every hour) once
# per month. Each bucket keeps (utc_ts, value) pairs so the SPP-weighted mean
# can still resolve each hour's Synergrid weight.
_SpotMonthBucket = dict[tuple[int, int], list[tuple[datetime, float]]]


def _bucket_by_local_month(spots: dict[datetime, float]) -> _SpotMonthBucket:
    """Group ``spots`` by their local ``(year, month)``, doing the timezone
    conversion once per entry.

    The YTD walk reads up to twelve months' means from the same year long
    cache; without bucketing each month rescans the whole dict and calls
    ``dt_util.as_local`` on every hour. Bucketing once turns each month into a
    dict lookup. Insertion order follows ``spots`` so a mean over a bucket
    matches the pre-bucket scan bit for bit.
    """
    buckets: _SpotMonthBucket = {}
    for ts, value in spots.items():
        local = dt_util.as_local(ts)
        buckets.setdefault((local.year, local.month), []).append((ts, value))
    return buckets


def _month_mean(bucket: _SpotMonthBucket, year: int, month: int) -> float | None:
    """Arithmetic mean of the (year, month) bucket, or ``None`` if empty."""
    entries = bucket.get((year, month))
    return fmean([value for _, value in entries]) if entries else None


# A closed month must be this well covered before its mean is billable. The
# mean of a sparsely cached month is applied to EVERY hour of it, so a thin
# sample is not a slightly noisier number -- it is a confident wrong one. The
# threshold mirrors the day-level rule in _ensure_historical_spots (20 of 24
# hours present); a month fetched in week-sized chunks is either nearly whole
# or missing whole weeks, and missing weeks are seasonally biased.
# Fewest cached hours a CLOSED month needs before its mean is billed. This is
# an absolute count, not a fraction of the month, because the question is not
# "how complete is the cache" but "is this sample big enough to average".
#
# Refusing costs the WHOLE commodity leg for that month, about 40% of the
# all-in rate, so the mean only has to beat a 100% error to be worth billing.
# Measured against real Belgian day-ahead prices (Jan, Feb, Apr and Jul 2026,
# via energy-charts.info), sampling the shape the fetch actually produces:
#
#   whole days missing   21 of 28 days  p95 6,6%   worst 14,3%
#                         7 of 28 days  p95 19,8%  worst 43,1%
#                         1 of 28 days  p95 53,8%  worst 93,9%
#   scattered hours              24 h   p95 22,8%  worst 61,7%
#                                12 h   p95 32,1%  worst 109,7%
#                                 1 h   p95 100,9% worst 612,1%
#
# So the mean beats dropping the leg everywhere down to about a day's worth of
# hours, and only the handful-of-hours tail can exceed what refusing costs.
# The previous rule required 80% of the month, which refused where the error
# was around 5% and forfeited 40% of the bill instead.
_MIN_MONTH_HOURS = 24


def _covered_month_mean(
    bucket: _SpotMonthBucket, year: int, month: int, today: date
) -> float | None:
    """Month mean, or ``None`` when a CLOSED month is too sparse to trust.

    ``_month_mean`` averages whatever hours happen to be cached and the caller
    applies that to every hour of the month. For the running month that is
    correct: it is partial by definition and the cached hours are the best
    estimate of it that exists. For a month that has already closed, a thin
    cache means the average is drawn from an unrepresentative slice -- one
    cached hour priced a whole January in testing -- and the result is a wrong
    rate rather than a missing one.

    Returning ``None`` hands the hour to the network-and-taxes path, which
    bills what is known and forfeits only the commodity.
    """
    mean = _month_mean(bucket, year, month)
    if mean is None:
        return None
    return None if _month_is_thinly_cached(bucket, year, month, today) else mean


def _month_is_thinly_cached(
    bucket: _SpotMonthBucket, year: int, month: int, today: date
) -> bool:
    """Whether a CLOSED month holds too few hours to average honestly.

    The running month is partial by definition and never counts as thin: the
    hours cached so far are the best estimate of it that exists. Shared by the
    energy leg's gate and the SPP-weighted injection one, which were applied
    to the same bucket in the same loop iteration and disagreed, so an hour
    could bill no commodity and still credit feed-in off the unrepresentative
    sample the gate exists to refuse.
    """
    if (year, month) >= (today.year, today.month):
        return False
    return len(bucket.get((year, month), ())) < _MIN_MONTH_HOURS


def _mean_of_month(spots: dict[datetime, float], year: int, month: int) -> float | None:
    """Arithmetic mean of the spot values whose local timestamp falls in
    (year, month). Returns ``None`` when that month has no cached hours.

    Convenience wrapper for callers holding a raw spot dict; the per-tick hot
    paths bucket once up front and call :func:`_month_mean` directly.
    """
    return _month_mean(_bucket_by_local_month(spots), year, month)


def _drop_future_spots(
    spots: dict[datetime, float], today: date
) -> dict[datetime, float]:
    """Keep only spots whose local date is ``today`` or earlier.

    The live monthly mean must average the same [Jan 1 .. today] window the
    YTD path bills on. Tomorrow's day-ahead curve is present in the fetched
    ``spot_prices`` after ~13:00 CET; leaving it in would nudge the flat
    spot-monthly rate and the mean-baked injection above what
    ``current_year_cost`` charges for the same month."""
    return {ts: v for ts, v in spots.items() if dt_util.as_local(ts).date() <= today}


def _spp_month_mean(
    bucket: _SpotMonthBucket,
    weights: SppWeights,
    year: int,
    month: int,
) -> float | None:
    """SPP-weighted mean of the (year, month) bucket's prices, or ``None``.

    Weights each price by the Synergrid profile weight for its UTC hour. The
    weights span the whole year, so a boundary hour (local month != UTC month)
    still finds its weight. Returns ``None`` when no weighted hour is available.
    """
    num = 0.0
    den = 0.0
    for ts, price in bucket.get((year, month), ()):
        utc = ts.astimezone(UTC)
        weight = weights.get((utc.month, utc.day, utc.hour))
        if weight is None:
            continue
        num += price * weight
        den += weight
    return num / den if den else None


def _spp_weighted_month_mean(
    spots: dict[datetime, float],
    weights: SppWeights,
    year: int,
    month: int,
) -> float | None:
    """SPP-weighted mean of the (year, month)'s prices, or ``None``.

    Convenience wrapper over :func:`_spp_month_mean` for callers holding a raw
    spot dict; selects the same local-delivery-month hours as
    :func:`_mean_of_month`. The per-tick hot path buckets once and calls
    :func:`_spp_month_mean` directly.
    """
    return _spp_month_mean(_bucket_by_local_month(spots), weights, year, month)


def _injection_is_spp_indexed(snapshot: SupplierSnapshot | None) -> bool:
    """True when the CARD says its injection formula indexes on Belpex_SPP.

    A property of the published card, not of a user preference: energie.be
    Variabel prices consumption on Belpex_RLP and injection on the
    solar-weighted Belpex_SPP, so its formula may only ever be resolved
    against an SPP-weighted mean.
    """
    inj = getattr(snapshot, "injection", None)
    return bool(getattr(inj, "spp_indexed", False))


def _injection_on_month_mean(snapshot: SupplierSnapshot | None) -> bool:
    """True when the injection formula resolves against a MONTH mean.

    Three ways in: the energy leg is itself month-mean priced, so the credit
    rides the same mean; the card indexes the credit on the monthly Belpex_SPP
    while pricing energy some other way (energie.be Vast, a flat rate with a
    monthly-indexed feed-in credit); or it indexes it on the month's plain
    arithmetic mean (Eneco's Belpex-injectie). Shared by the live tick, the YTD
    walk and the backfill so all three resolve the credit identically.
    """
    if isinstance(getattr(snapshot, "energy", None), SpotMonthlyRates):
        return True
    inj = getattr(snapshot, "injection", None)
    if bool(getattr(inj, "month_indexed", False)):
        return True
    return _injection_is_spp_indexed(snapshot)


def _spp_weighting_enabled(
    entry: ConfigEntry, snapshot: SupplierSnapshot | None = None
) -> bool:
    """True when this entry actually uses SPP-weighted injection.

    Two ways in, both requiring the injection regime (nothing else reads the
    credit, and the profile is a 52 MB download):

      * the CARD indexes its injection on Belpex_SPP (energie.be Variabel).
        Not optional: resolving that formula against any other mean roughly
        doubles the credit in a sunny month.
      * the expert custom monthly-average contract opted in by hand.

    For the custom route the contract + injection-mode conditions matter:
    without them a stale flag left after switching a monthly entry to
    fixed/dynamic (the options flow reseeds it), or a monthly entry on
    flat-rate injection, would trigger the download even though the weights
    are then discarded."""
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    if _injection_is_spp_indexed(snapshot):
        return True
    return (
        entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM
        and entry.data.get(CONF_CONTRACT) == CUSTOM_CONTRACT_MONTHLY
        and bool(entry.data.get(CONF_CUSTOM_INJECTION_SPP_WEIGHTED))
        and entry.data.get(CONF_CUSTOM_INJECTION_MODE) == CUSTOM_INJECTION_MODE_FORMULA
    )


def _now_slot_spot(
    energy: EnergyRates, spot_prices: dict[datetime, float]
) -> float | None:
    """ENTSO-E spot for the current billing slot, matching the grid the
    contract bills on so an Engie injection price tracks the current
    quarter-hour, not the hourly mean. Falls back to the nearest cached spot
    within one billing slot (15 min quarter-hourly, 1 h otherwise); returns
    None when none are cached or none are within range."""
    if not spot_prices:
        return None
    resolution = (
        RESOLUTION_QUARTER if _energy_is_quarter_hourly(energy) else RESOLUTION_HOURLY
    )
    now_slot = slot_start(dt_util.utcnow(), resolution)
    spot = spot_prices.get(now_slot)
    if spot is None:
        nearest = min(
            spot_prices.keys(),
            key=lambda h: abs((h - now_slot).total_seconds()),
        )
        # A fixed 1 h window let a quarter-hourly injection price use a spot
        # up to four slots away.
        max_gap = 900.0 if resolution == RESOLUTION_QUARTER else 3600.0
        if abs((nearest - now_slot).total_seconds()) > max_gap:
            return None
        spot = spot_prices[nearest]
    return spot


def _spp_injection_spot(
    spot: float | None,
    *,
    monthly_mean: bool,
    spp_weights: SppWeights | None,
    historical_spots: dict[datetime, float] | None = None,
    bucket: _SpotMonthBucket | None = None,
    year: int,
    month: int,
    today: date,
    cache: dict[tuple[int, int], float | None],
    hourly_spot: float | None = None,
    hourly: bool = False,
    strict: bool = False,
) -> float | None:
    """The spot value to price mean-indexed injection at.

    ``hourly`` short-circuits the whole month-mean question and returns
    ``hourly_spot``: a card whose injection carries its own per-hour index
    keeps it even when the ENERGY leg was re-priced to a monthly signing
    cohort. Cociter Tarif Variable is the case - its card indexes the two
    legs on different periods, note (7) "indexe mensuellement ... (BELIX)
    durant le mois de fourniture" for consumption against note (9) "le prix
    de l'injection varie chaque heure". The cohort re-price freezes the
    commodity coefficients, not the feed-in formula, and because PV output
    peaks exactly when the day-ahead price troughs, pricing that credit off
    a flat month mean systematically over-pays. Deciding it here keeps the
    live tick, the YTD walk and the backfill on one rule.

    Energy bills at the flat month-mean (``spot``); when the entry uses
    SPP-weighted injection and the Synergrid profile is available, the
    injection credit instead uses the SPP-weighted month-mean.

    What happens when the profile is missing depends on WHY it was wanted.
    The custom contract opted in as a refinement, so it falls back to
    ``spot``. A card that INDEXES on Belpex_SPP cannot: that mean is a
    different number, not a coarser one, so ``strict`` returns ``None`` and
    the caller credits the card's printed indicative instead.
    ``cache`` memoises the per-month weighted mean.

    Callers that already bucketed the spot cache for the tick pass ``bucket``;
    the rest pass the raw ``historical_spots`` and it is bucketed here on the
    first miss for a month. Shared by the live YTD credit and the backfill
    accrual so the two price mean-indexed injection identically.
    """
    if hourly:
        return hourly_spot
    if not (monthly_mean and spp_weights is not None):
        # ``strict`` means the formula indexes on Belpex_SPP and nothing else
        # will do, so answer "no spot" rather than the energy leg's mean and
        # let the caller fall back to the card's printed indicative.
        return None if strict else spot
    key = (year, month)
    if key not in cache:
        if bucket is None:
            if historical_spots is None:
                return None if strict else spot
            bucket = _bucket_by_local_month(historical_spots)
        cache[key] = (
            None
            if _month_is_thinly_cached(bucket, year, month, today)
            else _spp_month_mean(bucket, spp_weights, year, month)
        )
    weighted = cache[key]
    if weighted is not None:
        return weighted
    return None if strict else spot
