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

"""The hourly kWh series an hourly-billed contract is walked on.

Per side and per register, read off the recorder's hourly statistics, with
the hours a meter only reports once a day spread from its daily reads, the
running day topped up from the live state, and the hour-of-day shares the
projections weight on. The reads themselves are ``energy_meters``, called
through the module so a test that patches one there reaches this code too.
"""

from __future__ import annotations


from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any

from . import energy_meters
from .energy_meters import (
    _LOGGER,
    _bills_injection,
    _hourly_kwh_sensors,
    _kwh_sensor_ids,
    _partial_register_pair,
)
from .meter_faults import (
    _SHORT_BELOW,
    _paired_keys,
    _silent_periods,
    _silent_sensors,
    _stopped,
    _total_stands_in,
)


# The fewest past days a side must have moved on before it can count as read
# once a day. A real hourly feed-in meter moves in a single hour on a dull
# winter day, and on 2 January, or the day after a recorded switch, the window
# holds one past day: that one day called the meter read once a day, spread its
# export over the night and logged the warning. A week of such days, each with
# a row for every hour, is a poller rather than the weather.
_READ_DAILY_MIN_DAYS = 7

# The days before the window that make up _READ_DAILY_MIN_DAYS while the window
# holds fewer of its own (_read_daily_before).
_READ_DAILY_SPAN_DAYS = 28

# What _read_daily_before found, for the day: those days are past and do not
# change, and every walk of every tick asks again.
_READ_DAILY_BEFORE: dict[tuple[Any, ...], tuple[int, int]] = {}

# The sensors already said to report once a day, so the warning is logged
# once per run rather than on every hourly walk.
_READ_DAILY_LOGGED: set[tuple[str, ...]] = set()


async def _metered_hourly_kwh(
    hass: HomeAssistant, entry: ConfigEntry, side: str, start: date, end: date
) -> MeteredHours | None:
    """Per-UTC-hour kWh for one side of the entry's metering, or ``None`` when
    the wiring cannot be billed.

    ``None`` for a half-wired day/night pair with no totals sensor, and for a
    wired pair one half of which produced nothing at all. A pair otherwise
    counts each half over its own hours on the days both halves report
    (:func:`_paired_keys`), the days the per-day walk and the yearly volume
    bill. A half that misses an hour books its energy on its next row, so the
    hours both halves report dropped what the other half booked in the missed
    ones, while the day rows the per-day walk bills hold all of it. The side's
    totals sensor, when one is wired beside the pair, bills it instead where
    the pair cannot be billed whole or falls short of it
    (:func:`_total_stands_in`), since the total covers both bands on every
    hour. An empty map when nothing is wired on this side.

    A side read once a day has each day spread over its hours
    (:func:`_spread_daily_readings`), which is said once in the log and
    carried as ``read_daily``.
    """
    metered = await _side_hourly_kwh(hass, entry, side, start, end)
    if metered is None:
        return None
    today = dt_util.now().date()
    moving, polled = _polled_days(metered.kwh, today)
    if not polled:
        return metered
    polled_count = len(polled)
    if moving < _READ_DAILY_MIN_DAYS:
        # Too few days of its own yet: the days before the window make up the
        # minimum. Only then, or a meter that turned into a poller in the
        # weeks before 1 January or a switch was outvoted by its earlier
        # hourly days for most of a year. And only when its own days look
        # polled too: a meter that is hourly since the window opened, with
        # one dull single-hour day, was spread on the strength of the poller
        # it used to be, then put back on the seventh day. A dull first day
        # still is: one single-hour day is all a real poller shows on
        # 2 January too, and that one has to be spread.
        if polled_count < _SHORT_BELOW * moving:
            return metered
        moved_before, polled_before = await _read_daily_before(hass, entry, side, start)
        moving += moved_before
        polled_count += polled_before
    if moving < _READ_DAILY_MIN_DAYS or polled_count < _SHORT_BELOW * moving:
        return metered
    _spread_daily_readings(metered.kwh, polled)
    if metered.sensors not in _READ_DAILY_LOGGED:
        _READ_DAILY_LOGGED.add(metered.sensors)
        _LOGGER.warning(
            "%s reports its kWh once a day rather than hour by hour, so each "
            "day is spread evenly over its hours: a contract priced by the "
            "hour bills it at the day's average rather than at the hour the "
            "reading arrived",
            ", ".join(metered.sensors),
        )
    return replace(metered, read_daily=True)


def _per_local_day(hourly: Mapping[datetime, float]) -> dict[date, float]:
    """An hourly map summed into local days."""
    out: dict[date, float] = {}
    for hour, kwh in hourly.items():
        day = dt_util.as_local(hour).date()
        out[day] = out.get(day, 0.0) + kwh
    return out


async def _side_hourly_kwh(
    hass: HomeAssistant, entry: ConfigEntry, side: str, start: date, end: date
) -> MeteredHours | None:
    """:func:`_metered_hourly_kwh` before a once-a-day meter is spread."""
    if _partial_register_pair(entry, side):
        return None
    ids = _hourly_kwh_sensors(entry, side)
    if len(ids) != 2:
        return MeteredHours(
            await energy_meters._sum_hourly_kwh(hass, ids, start, end), tuple(ids)
        )
    day = await energy_meters._sum_hourly_kwh(hass, ids[:1], start, end)
    night = await energy_meters._sum_hourly_kwh(hass, ids[1:], start, end)
    total_id = _kwh_sensor_ids(entry, side)[2]
    if total_id:
        total = await energy_meters._sum_hourly_kwh(hass, [total_id], start, end)
        # Judged on local days, as the per-day walk judges it: the pair bills
        # each register over its own hours, so counting only the hours both
        # report let a totals sensor that started a few days late outnumber
        # a pair missing a few rows, and lose those days.
        if _total_stands_in(
            _per_local_day(total), _per_local_day(day), _per_local_day(night)
        ):
            return MeteredHours(total, (total_id,))
    if _paired_keys(day, night) is None:
        return None
    days = {dt_util.as_local(hour).date() for hour in day}
    days &= {dt_util.as_local(hour).date() for hour in night}
    kwh: dict[datetime, float] = {}
    for half in (day, night):
        for hour, reading in half.items():
            if dt_util.as_local(hour).date() in days:
                kwh[hour] = kwh.get(hour, 0.0) + reading
    return MeteredHours(
        kwh,
        tuple(ids),
        frozenset((day.keys() | night.keys()) - kwh.keys()),
        today_ok=not _stopped(((ids[0], day), (ids[1], night))),
        last=((ids[0], max(day)), (ids[1], max(night))) if day else (),
    )


@dataclass(frozen=True)
class MeteredSides:
    """Both sides' hourly kWh, after the rule that compares them.

    An hour consumption did not report is taken out of both sides: the days
    one half of its register pair did not report, the hours before its first
    reading and those after it went silent (:func:`_silent_periods`). An hour
    injection did not report stays billed on consumption without its feed-in.
    ``silent`` names the sensors of the side that went silent, for the
    Repairs card: of a register pair, the halves that did
    (:func:`_silent_sensors`).

    ``injection_as_read`` is the injection side before the comparison, for
    the feed-in credit added to the per-day walk, which compares the two
    sides day by day: a day it bills keeps its whole feed-in, the hours
    consumption did not report included.
    """

    consumption: MeteredHours
    injection: MeteredHours
    silent: tuple[str, ...] = ()
    injection_as_read: MeteredHours = field(
        default_factory=lambda: MeteredHours({}, ())
    )


async def _metered_sides(
    hass: HomeAssistant, entry: ConfigEntry, start: date, end: date
) -> MeteredSides | None:
    """Both sides of the entry's metering hour by hour, or ``None`` when
    either cannot be billed (:func:`_metered_hourly_kwh`).

    The one place the hourly walks, the backfill and the diagnostics read the
    two sides, so the hours left out are left out the same way everywhere.
    Today follows the same rule: consumption keeps ``today_ok`` unless it went
    silent, and injection only where consumption keeps it and injection did
    not go silent, since today is after the silence.
    """
    cons = await _metered_hourly_kwh(hass, entry, "consumption", start, end)
    inj = (
        await _metered_hourly_kwh(hass, entry, "injection", start, end)
        if _bills_injection(entry)
        else MeteredHours({}, ())
    )
    if cons is None or inj is None:
        return None
    side, both, feed_in = _silent_periods(
        cons.kwh if cons.sensors else None,
        inj.kwh if inj.sensors else None,
        cons.kwh.keys() | inj.kwh.keys(),
    )
    unknown = cons.unknown | both
    cons_today = cons.today_ok and side != "consumption"
    silent: tuple[str, ...] = ()
    if side == "consumption":
        silent = _silent_sensors(cons.sensors, cons.last, inj.kwh.keys())
    elif side == "injection":
        silent = _silent_sensors(inj.sensors, inj.last, cons.kwh.keys())
    return MeteredSides(
        replace(
            cons,
            kwh={h: kwh for h, kwh in cons.kwh.items() if h not in unknown},
            unknown=unknown,
            today_ok=cons_today,
        ),
        replace(
            inj,
            kwh={
                h: kwh
                for h, kwh in inj.kwh.items()
                if h not in unknown and h not in feed_in
            },
            unknown=unknown,
            today_ok=inj.today_ok and cons_today and side != "injection",
        ),
        silent,
        inj,
    )


async def _top_up_today_hourly(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    per_hour: dict[datetime, float],
    today: date,
) -> None:
    """Add today's not-yet-compiled kWh to the hourly map, in place.

    The hourly branch reads long-term hourly statistics only, which reflect
    the last COMPILED hour. Every hourly-billed contract (dynamic,
    spot-monthly, TOU, Impact, exclusive-night) therefore stepped
    ``current_year_cost`` once an hour at best and froze outright when
    statistics compilation lagged or stalled, while the meter kept updating.
    The per-day branch has read today off the live meter since 0.11.9; this
    gives the hourly branch the same guarantee.

    The shortfall (live total for today minus what statistics already carry
    for today) is attributed to the CURRENT hour. That is where the missing
    energy actually was: statistics trail real time, so what they have not
    booked yet is the most recent consumption. It also prices the top-up at
    the hour the user is living through, which is the point of a live read on
    a dynamic contract.

    All or nothing: when any of the sensors has no reliable live reading the
    statistics figure is left standing, exactly as the per-day path degrades.
    A side is read off one totals sensor or off a register pair, and a pair
    topped up off the one half that reads added that band's whole day to the
    current hour, which the next midnight took back.
    """
    live_total = 0.0
    have_live = False
    for entity_id in entity_ids:
        live = await energy_meters._live_today_kwh(hass, entity_id, today)
        if live is None:
            return
        live_total += live
        have_live = True
    if not have_live:
        return
    midnight = dt_util.start_of_local_day(today).astimezone(UTC)
    compiled_today = sum(kwh for hour, kwh in per_hour.items() if hour >= midnight)
    missing = live_total - compiled_today
    if missing <= 0.0:
        # Statistics have caught up (or overshot on a meter that ran
        # backwards); leave them alone rather than inventing a negative hour.
        return
    current_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    per_hour[current_hour] = per_hour.get(current_hour, 0.0) + missing


def _polled_days(
    kwh: Mapping[datetime, float], until: date
) -> tuple[int, dict[date, tuple[datetime, int]]]:
    """How many days before ``until`` a side moved on, and those of them it
    moved in a single hour, with the first hour of each and its hour count.

    A sensor fed by a supplier portal or a nightly fetch moves once a day,
    and Home Assistant still writes a row every hour: 23 of change zero and
    one holding the whole day. A day has that shape when it holds a row for
    every one of its hours, 23 or 25 on a DST seam day, and moved in exactly
    one.
    """
    rows: dict[date, int] = {}
    moving: dict[date, list[datetime]] = {}
    for hour, value in kwh.items():
        day = dt_util.as_local(hour).date()
        if day >= until:
            continue
        rows[day] = rows.get(day, 0) + 1
        if value > 0.0:
            moving.setdefault(day, []).append(hour)
    polled: dict[date, tuple[datetime, int]] = {}
    for day, hours in moving.items():
        first = dt_util.start_of_local_day(day).astimezone(UTC)
        after = dt_util.start_of_local_day(day + timedelta(days=1)).astimezone(UTC)
        count = round((after - first) / timedelta(hours=1))
        if len(hours) == 1 and rows[day] == count:
            polled[day] = (first, count)
    return len(moving), polled


async def _read_daily_before(
    hass: HomeAssistant, entry: ConfigEntry, side: str, start: date
) -> tuple[int, int]:
    """How many of the ``_READ_DAILY_SPAN_DAYS`` days before ``start`` the
    side moved on, and on how many of them in a single hour
    (:func:`_polled_days`).

    Judged on the window alone, a meter read once a day was priced at the
    hour its reading arrived for the first six days of every year and of
    every contract after a switch, then spread after the fact on the
    seventh, which moved a cost already shown, and a closed contract shorter
    than a week was never spread at all. The same sensor's days before the
    window say what it is from the window's first day. Kept for the day.
    """
    today = dt_util.now().date()
    key = (today, side, _kwh_sensor_ids(entry, side), start)
    if key not in _READ_DAILY_BEFORE:
        if _READ_DAILY_BEFORE and next(iter(_READ_DAILY_BEFORE))[0] != today:
            _READ_DAILY_BEFORE.clear()
        metered = await _side_hourly_kwh(
            hass,
            entry,
            side,
            start - timedelta(days=_READ_DAILY_SPAN_DAYS),
            start - timedelta(days=1),
        )
        moving, polled = _polled_days(metered.kwh if metered else {}, start)
        _READ_DAILY_BEFORE[key] = (moving, len(polled))
    return _READ_DAILY_BEFORE[key]


def _spread_daily_readings(
    kwh: dict[datetime, float], polled: Mapping[date, tuple[datetime, int]]
) -> None:
    """Spread, in place, the ``polled`` days of a side read once a day over
    their hours (:func:`_polled_days`).

    Every path that prices by the hour (dynamic energy, a per-slot feed-in
    credit, time of use, Impact) otherwise billed the day at the hour the
    reading arrived; a feed-in read at midnight was credited about 11 times
    what it earned. :func:`_recorder_daily_band_ratio` already refuses that
    shape for the band split.

    The side counts as read once a day (:func:`_metered_hourly_kwh`) when at
    least ``_SHORT_BELOW`` of the past days it moved on have that shape, so a
    real meter that moved in one hour on a dull day is left alone, and it
    moved on at least ``_READ_DAILY_MIN_DAYS`` of them, so a day or two says
    nothing yet. Those days are the window's, and while it holds fewer than
    ``_READ_DAILY_MIN_DAYS`` of its own also the ``_READ_DAILY_SPAN_DAYS``
    before it (:func:`_read_daily_before`). Each
    such day's kWh is spread evenly over its hours, the neutral guess without
    a profile. Today is left as it is: it is not over.
    """
    for first, count in polled.values():
        whole = sum(kwh[first + timedelta(hours=i)] for i in range(count))
        for i in range(count):
            kwh[first + timedelta(hours=i)] = whole / count


@dataclass(frozen=True)
class MeteredHours:
    """One side's per-UTC-hour kWh and the sensors it was read from.

    The sensors are what today's live top-up has to read. A pair that a wired
    total stands in for is billed off the total, and topping it up off the
    registers would add one meter's live reading to another's statistics.
    """

    kwh: dict[datetime, float]
    sensors: tuple[str, ...]
    # The hours of the days only one half of a register pair reported. The
    # side's own figure already leaves them out; on the consumption side a
    # walk that bills both sides must leave them out of the feed-in too, or it
    # credits the feed-in of an hour whose consumption it did not bill.
    unknown: frozenset[datetime] = frozenset()
    # True when the side reports once a day and each day was spread over its
    # hours (:func:`_spread_daily_readings`).
    read_daily: bool = False
    # False when one half of the pair stopped: today cannot be billed then,
    # since the day drops out at midnight with the hours the stopped half
    # never reports, so a live top-up billed today would be taken back
    # tomorrow.
    today_ok: bool = True
    # The last hour each register of a pair reported, for naming the one that
    # went silent when the side did (:func:`_silent_sensors`). Empty for a
    # side read off one sensor.
    last: tuple[tuple[str, datetime], ...] = ()


async def _measured_hour_weights(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
) -> dict[int, float] | None:
    """Share of ``side``'s metered kWh falling in each hour of the local day.

    An annual estimate that averages time-of-use slot rates by CLOCK hours
    assumes the household consumes uniformly around the clock. It does not: on
    a residential profile the peak band carried 0,56 of the kWh against the
    0,38 of the week its hours occupy, so a card that is expensive at peak was
    quoted well under what that same household is actually billed. The live
    year-to-date has always weighted each hour by the kWh recorded in it; this
    is what lets the estimate beside it do the same.

    On the injection side the same argument is sharper still. Solar export is
    zero through the whole 01:00-07:00 off-peak block, which carries about a
    third of the clock weight, so a per-slot feed-in credit averaged by slot
    duration always under-credits.

    Returns ``None`` when nothing is wired, the pair is half-wired or has a
    dead half, or the window recorded nothing. The caller then stays on the
    clock-hour weighting rather than inventing a profile from one band.
    """
    return _hour_of_day_shares(
        await _measured_hourly(hass, entry, start, end, side=side)
    )


async def _measured_hourly(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
) -> dict[datetime, float] | None:
    """``side``'s metered kWh per UTC hour, or ``None`` when there is none.

    The read :func:`_measured_hour_weights` folds into an hour-of-day shape,
    for a caller that also needs each hour's own kWh: a spot-indexed feed-in
    credit weighs every hour of the year by what was exported in it, so the
    one read serves both.
    """
    metered = await _metered_hourly_kwh(hass, entry, side, start, end)
    if metered is None or not metered.kwh:
        return None
    return metered.kwh


def _hour_of_day_shares(
    kwh_by_hour: Mapping[datetime, float] | None,
) -> dict[int, float] | None:
    """Share of the kWh falling in each hour of the local day, or ``None``."""
    if not kwh_by_hour:
        return None
    per_hour: dict[int, float] = {}
    for when, kwh in kwh_by_hour.items():
        hour = dt_util.as_local(when).hour
        per_hour[hour] = per_hour.get(hour, 0.0) + kwh
    total = sum(per_hour.values())
    if total <= 0:
        return None
    return {hour: kwh / total for hour, kwh in per_hour.items()}
