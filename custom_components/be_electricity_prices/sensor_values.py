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

"""What each price sensor reads off the coordinator's data.

The value functions the sensor descriptions point at: the current and
next slot, the day's and tomorrow's average, minimum and maximum, the
cheapest and dearest hours, and the today / tomorrow tables the attributes
carry. Pure functions of ``CoordinatorData`` and the clock, kept apart from
the entity classes that publish them.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

from homeassistant.util import dt as dt_util

from .binary_sensor import _card_covers_tomorrow, _has_tomorrow
from .const import RESOLUTION_HOURLY
from .coordinator_data import CoordinatorData
from .pricing import PriceBreakdown, breakdown_row, slot_start

# What one slot of a per-slot table holds: a PriceBreakdown for the price
# table, a plain EUR/kWh float for the injection one.
_SlotValue = TypeVar("_SlotValue")


def _current_slot_value(
    slots: dict[datetime, _SlotValue], resolution: str
) -> _SlotValue | None:
    """Look ``slots`` up at the slot the wall clock is in.

    Reading the clock here rather than at coordinator-refresh time is what
    keeps a sensor aligned to the slot the user is billed for: the
    coordinator's own tick is a plain 60-minute interval anchored on setup,
    so a value baked into it lags the boundary by however far the tick has
    drifted.

    On an exact miss the temporally nearest slot is substituted, but only
    within one billing slot of "now" (15 min on a quarter-hourly contract,
    1 h otherwise, the latter also absorbing the DST seam). That bound
    stops a stale spot cache from silently surfacing yesterday's last slot
    as "now"; a fixed 1 h window let a quarter-hourly sensor surface an
    up-to-45-min-stale slot as current. Returns ``None`` when the table is
    empty or nothing falls inside the window.
    """
    if not slots:
        return None
    now = slot_start(dt_util.utcnow(), resolution)
    if (exact := slots.get(now)) is not None:
        return exact
    nearest_slot = min(slots, key=lambda h: abs((h - now).total_seconds()))
    max_gap = 3600.0 if resolution == RESOLUTION_HOURLY else 900.0
    if abs((nearest_slot - now).total_seconds()) > max_gap:
        return None
    return slots[nearest_slot]


def _current(data: CoordinatorData) -> PriceBreakdown | None:
    return _current_slot_value(data.hourly, data.resolution)


def _current_injection(data: CoordinatorData) -> float | None:
    """Injection price for the slot the wall clock is in.

    ``injection_price_eur_per_kwh`` is resolved once per coordinator tick,
    so on a contract whose injection varies intra-day (Engie Empower
    Flextime's TOU schedule, every spot-indexed injection) the sensor kept
    the previous slot's rate until the next tick, while the consumption
    sensors moved on the boundary (issue #44). ``injection_hourly`` already
    holds the per-slot rate over the same grid as ``hourly``, so read the
    current slot out of it the way ``_current`` reads the price table.

    A single slot the coordinator could not price (a dynamic contract with
    a hole in the day-ahead curve) is covered by the shared nearest-slot
    rule, so the sensor shows an adjacent slot's rate. The tick's scalar is
    the last resort: the flat contracts that emit no array at all, and a
    table with nothing inside the window.
    """
    rate = _current_slot_value(data.injection_hourly, data.resolution)
    return data.injection_price_eur_per_kwh if rate is None else rate


def _next_hour(data: CoordinatorData) -> PriceBreakdown | None:
    if not data.hourly:
        return None
    # One hour ahead of the current slot. For a 15-minute contract this
    # is the same quarter in the next hour, so the sensor keeps its "next
    # hour" meaning rather than becoming "next 15 minutes".
    target = slot_start(dt_util.utcnow(), data.resolution) + timedelta(hours=1)
    return data.hourly.get(target)


def _bucket(
    data: CoordinatorData,
    when: date,
    reducer: Callable[[list[float]], float],
) -> float | None:
    values = [
        bd.all_in
        for hour, bd in data.hourly.items()
        if dt_util.as_local(hour).date() == when
    ]
    if not values:
        return None
    return reducer(values)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values)


def _avg_breakdown(bds: list[PriceBreakdown]) -> PriceBreakdown:
    """Mean of each component across a list of breakdowns."""
    n = len(bds)
    return PriceBreakdown(
        energy=sum(b.energy for b in bds) / n,
        network=sum(b.network for b in bds) / n,
        taxes=sum(b.taxes for b in bds) / n,
        all_in=sum(b.all_in for b in bds) / n,
    )


def _hourly_view(data: CoordinatorData) -> dict[datetime, PriceBreakdown]:
    """Hourly-resolution view of the price table, for the ranked lists.

    Returns ``data.hourly`` unchanged for hourly contracts; for a
    quarter-hourly one it averages each hour's four slots into one breakdown.

    Only ``cheapest_4h_today`` / ``most_expensive_4h_today`` read this, and
    they read it because they are counted in HOURS. Ranking the native slots
    and taking four of them would quietly turn "the cheapest four hours" into
    the cheapest one, which is not what either name says.

    The ``today`` / ``tomorrow`` curves do NOT come through here. They carry
    whatever grid the contract settles on, which is the whole curve a battery
    or an EV schedule needs to plan against.
    """
    if data.resolution == RESOLUTION_HOURLY:
        return data.hourly
    buckets: dict[datetime, list[PriceBreakdown]] = {}
    for slot, bd in data.hourly.items():
        hour = slot.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(bd)
    return {hour: _avg_breakdown(bds) for hour, bds in buckets.items()}


def _today_avg(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), _avg)


def _today_min(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), min)


def _today_max(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), max)


def _tomorrow_bucket(
    data: CoordinatorData, reducer: Callable[[list[float]], float]
) -> float | None:
    """Reduce tomorrow's slots, but only while the card actually covers them.

    The price table forward-fills 48 hours, so on the last day of a monthly
    card's validity the "tomorrow" rows are an extrapolation the supplier has
    not published: next month's rates do not exist yet. ``_has_tomorrow`` has
    always refused to claim those hours, so the binary sensor went off while
    these three reported the extrapolation as a number, and the two entities
    contradicted each other for a full day every month. Sharing the predicate
    makes the invariant explicit: a tomorrow_* sensor has a value exactly when
    tomorrow_prices_available is on.

    Only the tomorrow side is gated. An expired card still describes today
    better than nothing does, and a snapshot stale enough to worry about
    raises its own repair issue.
    """
    if not _has_tomorrow(data):
        return None
    return _bucket(data, dt_util.now().date() + timedelta(days=1), reducer)


def _tomorrow_avg(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, _avg)


def _tomorrow_min(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, min)


def _tomorrow_max(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, max)


def _today_ranked(
    data: CoordinatorData, count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the ``count`` cheapest and ``count`` most-expensive today-hours.

    The two lists are always disjoint: when fewer than ``2 * count`` today
    hours are populated (e.g. right after midnight on a static contract),
    the cheapest take their share first and the most-expensive list gets
    only what remains. Each list is returned in chronological order.

    On flat tariffs (every hour rounded to the same all-in price) the
    chronological tie-break makes the result fully deterministic: the
    cheapest list will always be the first ``count`` hours of the day
    and the most-expensive list will be the last ``count``. Automations
    keying on these attributes for "cheapest window" should treat the
    output as undefined when prices don't actually vary across the day.
    """
    hourly = _hourly_view(data)
    today = dt_util.now().date()
    pairs = [(h, bd) for h, bd in hourly.items() if dt_util.as_local(h).date() == today]
    if not pairs:
        return [], []
    # Secondary key on the hour breaks ties deterministically across
    # reloads. Without it, dict-insertion order leaks into the
    # cheapest_4h_today / most_expensive_4h_today attributes whenever
    # multiple hours share the same all-in price (common on static
    # contracts where every hour rounds to the same four decimals).
    by_price_asc = sorted(pairs, key=lambda x: (x[1].all_in, x[0]))
    cheapest_pairs = by_price_asc[:count]
    remaining = by_price_asc[count:]
    most_expensive_pairs = remaining[-count:] if remaining else []
    cheapest = sorted(cheapest_pairs, key=lambda x: x[0])
    most_expensive = sorted(most_expensive_pairs, key=lambda x: x[0])

    def _fmt(h: Any, bd: PriceBreakdown) -> dict[str, Any]:
        return {
            "start": dt_util.as_local(h).isoformat(),
            "price": round(bd.all_in, 6),
        }

    return (
        [_fmt(h, bd) for h, bd in cheapest],
        [_fmt(h, bd) for h, bd in most_expensive],
    )


def _split_hourly_today_tomorrow(
    hourly: dict[datetime, Any],
    row_fn: Callable[[datetime, Any], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group ``hourly`` into today and tomorrow buckets in chronological
    order, serialising each slot with ``row_fn(local, value)``. Slots
    outside the two-day window (typically none) are dropped."""
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    today_rows: list[dict[str, Any]] = []
    tomorrow_rows: list[dict[str, Any]] = []
    for h, value in sorted(hourly.items()):
        local = dt_util.as_local(h)
        row = row_fn(local, value)
        if local.date() == today:
            today_rows.append(row)
        elif local.date() == tomorrow:
            tomorrow_rows.append(row)
    return today_rows, tomorrow_rows


def _split_today_tomorrow(
    data: CoordinatorData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group the cached breakdowns into today and tomorrow buckets.

    At the grid the contract settles on: 24 rows per day on an hourly
    contract, 96 on a quarter-hourly one. Both lists are returned in
    chronological order, and slots outside the today/tomorrow window
    (typically there are none) are dropped.

    These used to be averaged down to hourly on the grounds that ~192 rows
    would exceed HA's 16 KB per-state-attribute limit. They would, but that
    limit never applied here: both keys are in ``_unrecorded_attributes``, and
    the recorder strips excluded attributes BEFORE it measures
    (``db_schema.shared_attrs_bytes_from_event``). So the downsampling bought
    nothing and cost the one thing a 15-minute contract is chosen for, which
    is knowing which quarter is cheap.

    Tomorrow is left empty while the card does not cover it
    (``_card_covers_tomorrow``), as the tomorrow_* sensors are: on the last
    day of a monthly card the table forward-fills rates the supplier has not
    published, and a chart drew them as tomorrow's prices.
    """
    today, tomorrow = _split_hourly_today_tomorrow(data.hourly, breakdown_row)
    return today, tomorrow if _card_covers_tomorrow(data) else []


def _split_injection_today_tomorrow(
    data: CoordinatorData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group the per-slot injection prices into today and tomorrow buckets.

    Each bucket is a chronological list of ``{start, injection}`` rows, at the
    grid the contract settles on. Both are empty for a contract whose
    injection doesn't vary intra-day (the coordinator emits no
    ``injection_hourly`` for it).

    Publishing the slots rather than an hourly mean of them also retires an
    approximation. A floored feed-in formula is convex, so the mean of four
    floored quarter rates is not the rate of their mean, and the hourly row
    this used to show was the former while the credit is earned per slot.

    Tomorrow is gated on the card covering it, as the price arrays are.
    """
    today, tomorrow = _split_hourly_today_tomorrow(
        data.injection_hourly,
        lambda local, rate: {"start": local.isoformat(), "injection": round(rate, 6)},
    )
    return today, tomorrow if _card_covers_tomorrow(data) else []


def _current_field(field: str) -> Callable[[CoordinatorData], float | None]:
    def _inner(data: CoordinatorData) -> float | None:
        bd = _current(data)
        return None if bd is None else getattr(bd, field)

    return _inner
