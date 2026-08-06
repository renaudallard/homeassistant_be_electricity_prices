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


def _spp_weighting_enabled(entry: ConfigEntry) -> bool:
    """True when this entry actually uses SPP-weighted injection: a custom
    monthly-average contract, injection regime, formula injection, flag set.

    The contract + injection-mode conditions matter: without them a stale flag
    left after switching a monthly entry to fixed/dynamic (the options flow
    reseeds it), or a monthly entry on flat-rate injection, would trigger the
    52 MB profile download even though the weights are then discarded."""
    return (
        entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM
        and entry.data.get(CONF_CONTRACT) == CUSTOM_CONTRACT_MONTHLY
        and bool(entry.data.get(CONF_CUSTOM_INJECTION_SPP_WEIGHTED))
        and entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
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
    cache: dict[tuple[int, int], float | None],
    hourly_spot: float | None = None,
    hourly: bool = False,
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

    Energy bills at the flat month-mean (``spot``); when the entry opted
    into SPP-weighted injection (a custom monthly contract) and the
    Synergrid profile is available, the injection credit instead uses the
    SPP-weighted month-mean, falling back to ``spot`` when the profile is
    missing for the month. ``cache`` memoises the per-month weighted mean.

    Callers that already bucketed the spot cache for the tick pass ``bucket``;
    the rest pass the raw ``historical_spots`` and it is bucketed here on the
    first miss for a month. Shared by the live YTD credit and the backfill
    accrual so the two price mean-indexed injection identically.
    """
    if hourly:
        return hourly_spot
    if not (monthly_mean and spp_weights is not None):
        return spot
    key = (year, month)
    if key not in cache:
        if bucket is None:
            if historical_spots is None:
                return spot
            bucket = _bucket_by_local_month(historical_spots)
        cache[key] = _spp_month_mean(bucket, spp_weights, year, month)
    weighted = cache[key]
    return weighted if weighted is not None else spot
