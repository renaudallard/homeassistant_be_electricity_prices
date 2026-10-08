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

"""Telling a meter that stopped from a household that used nothing.

The register checks the per-day and the hourly reads share: a day/night
pair wired by halves, a register that went quiet or started late, a totals
sensor standing in for a pair it disagrees with, and the running day kept
apart from the closed ones. Pure functions of what the recorder returned;
the reading itself is ``energy_meters``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from datetime import date, timedelta

from homeassistant.util import dt as dt_util

from .energy_meters import (
    _K,
    _P,
)

# How far a register's latest day may trail its twin's before it counts as
# stopped. Statistics compile an hour behind, so a register a day behind is
# normal; two means it is no longer recording. Today never counts here (see
# _split_today).
_REGISTER_STOPPED_AFTER_DAYS = 2


# A register pair billing less than this share of its totals sensor over the
# same periods, or a totals sensor recording less than this share of the pair,
# is short rather than rounded: one of the two is frozen or stuck.
_SHORT_BELOW = 0.9


def _split_today(
    day: Mapping[date, float], night: Mapping[date, float], end: date
) -> tuple[dict[date, float], dict[date, float], tuple[float | None, float | None]]:
    """A register pair's daily readings with today taken out, and today's
    reading of each half, ``None`` where a half has none.

    Today's value is a live meter reading (:func:`_recorder_daily_kwh`), and
    the live reading comes off the state history, which a register compiling
    no statistics still has. Paired with today in, such a half looked alive:
    the pair was billed on today alone, and no register was named for the
    Repairs card, because each had reported today. Whether a half records is
    therefore decided on the days before today, and the caller bills today
    on top only when both halves read it and neither stopped.
    """
    if end != dt_util.now().date():
        return dict(day), dict(night), (None, None)
    return (
        _without_today(day, end),
        _without_today(night, end),
        (day.get(end), night.get(end)),
    )


def _without_today(readings: Mapping[date, float], end: date) -> dict[date, float]:
    """``readings`` with ``end`` taken out when it is today, whose value is
    a live reading rather than a statistic (:func:`_split_today`)."""
    if end != dt_util.now().date():
        return dict(readings)
    return {when: kwh for when, kwh in readings.items() if when != end}


def _stopped(halves: Iterable[tuple[str, Collection[_P]]]) -> list[str]:
    """The halves of a reporting pair whose latest day (or hour) trails the
    other's by more than ``_REGISTER_STOPPED_AFTER_DAYS``.

    Relative to the pair rather than to the clock, so a statistics stall that
    holds both halves back names neither.
    """
    last = [(entity_id, max(readings)) for entity_id, readings in halves if readings]
    if len(last) < 2:
        return []
    latest = max(when for _entity_id, when in last)
    limit = latest - timedelta(days=_REGISTER_STOPPED_AFTER_DAYS)
    return [entity_id for entity_id, when in last if when < limit]


def _started_late(
    day_id: str, day: Collection[date], night_id: str, night: Collection[date]
) -> list[str]:
    """The half of a reporting pair that started more than
    ``_REGISTER_STOPPED_AFTER_DAYS`` after its twin, while the days both
    report are under ``_SHORT_BELOW`` of the days its twin does.

    A register rewired after a replacement, as the Repairs card advises,
    reports from the day it was wired, so the pair bills only since then: a
    year to date of 917 EUR fell to 23,56 over 7 of 269 days, and the card
    cleared, since :func:`_stopped` rightly ignores a register that started
    late. It stays named until the pair covers most of the window again. A
    half that started a day or two late, as a meter added to Home Assistant
    one register at a time does, is not.
    """
    if not day or not night:
        return []
    paired = len(set(day) & set(night))
    if paired >= _SHORT_BELOW * max(len(day), len(night)):
        return []
    limit = min(min(day), min(night)) + timedelta(days=_REGISTER_STOPPED_AFTER_DAYS)
    return [
        entity_id
        for entity_id, readings in ((day_id, day), (night_id, night))
        if min(readings) > limit
    ]


def _paired_keys(day: Mapping[_K, float], night: Mapping[_K, float]) -> set[_K] | None:
    """The days (or hours) a day/night register pair can be billed on.

    ``None`` when one half produced nothing at all: a wired register that
    compiles no statistics is a broken pair, not a band that used no energy.
    Otherwise the keys BOTH halves report. A live register writes a row for
    every period whether or not it moved, so a key only one half holds is a
    period the other did not record, and billing it would bill that band at
    zero: a night register stopped at the end of February left the rest of the
    year on the day band alone, labelled as a whole year.
    """
    if bool(day) != bool(night):
        return None
    return set(day) & set(night)


def _silent_periods(
    cons: Collection[_P] | None, inj: Collection[_P] | None, periods: Iterable[_P]
) -> tuple[str | None, set[_P], set[_P]]:
    """Which of ``periods`` the two sides cannot both be billed on.

    Returns the side that went silent under the other, if any, the periods to
    leave out of both sides, and the periods to bill without their feed-in.
    ``cons`` and ``inj`` are the days (or hours) each side reported, ``None``
    for a side with nothing wired, and nothing is compared unless both are.

    A period consumption did not report is unknown, and leaves both sides:
    crediting its feed-in against no consumption drove the year down on days
    nothing was charged. That is every period before consumption's first one,
    since a consumption meter renamed in April credited the feed-in of January
    to March against nothing, every gap between its first and last period,
    which is where a negative bucket was dropped (:func:`_recorder_deltas`),
    and every period after it stopped, which is when its last period trails
    injection's by more than ``_REGISTER_STOPPED_AFTER_DAYS``
    (:func:`_stopped`). A register pair already leaves such a period out of
    both sides; a consumption totals sensor left it billed on its feed-in
    alone. A wired consumption
    meter that reported nothing at all is silent for the whole window: a live
    meter writes a row every hour, moved or not, so no row is missing data
    rather than a household that used nothing. Nothing is compared while
    neither side reported anything: on the first day of the window (1 January,
    or the day a recorded switch started the current contract) the days before
    today are none, and cutting today left the day billed on its fees alone.
    The per-day walk judges a side with no day before today on today's
    compiled hours instead, which is what the hourly walks see
    (:func:`_resolve_daily_kwh`).

    A period injection did not report while consumption did is still billed
    on its consumption, with the feed-in left out. Cutting it from both sides
    threw away a year of real consumption behind a dead feed-in meter. An
    injection meter that stopped, or that reported nothing at all in the
    window, is named as silent, since the bill now reads high; one that starts
    later, because the panels came later, is not, and cuts nothing.
    """
    if cons is None or inj is None or not (cons or inj):
        return None, set(), set()
    periods = set(periods)
    if not cons:
        return "consumption", periods, set()
    last = max(cons)
    both = {p for p in periods if p < last and p not in cons}
    if not inj:
        return "injection", both, periods - both
    stopped = _stopped((("consumption", cons), ("injection", inj)))
    if stopped == ["consumption"]:
        return "consumption", both | {p for p in periods if p > last}, set()
    if stopped == ["injection"]:
        return "injection", both, {p for p in periods if p > max(inj)}
    return None, both, set()


def _silent_sensors(
    sensors: tuple[str, ...], last: Iterable[tuple[str, _P]], other: Collection[_P]
) -> tuple[str, ...]:
    """The sensors to name for a side that went silent under ``other``
    (:func:`_silent_periods`), given the last period each register of its
    pair reported.

    Of a pair, the registers whose last period trails the other side's as
    :func:`_stopped` judges it: naming the side named the half that carries
    on beside the one that stopped. The whole side when no register stands
    out, as when it recorded nothing at all.
    """
    named = tuple(
        entity_id
        for entity_id, when in last
        if _stopped(((entity_id, (when,)), ("", other)))
    )
    return named or sensors


def _total_stands_in(
    total: Mapping[_K, float], day: Mapping[_K, float], night: Mapping[_K, float]
) -> bool:
    """Whether a wired totals sensor bills a side instead of its register pair.

    The total covers both bands, so it wins where the pair cannot be billed
    whole: a dead half, halves reporting different periods, or both halves
    empty, stopped or started late, which all leave periods the total reports
    and the pair cannot bill. It also wins over a pair reporting the same
    periods but billing less than ``_SHORT_BELOW`` of it, which is a half
    frozen at its last reading: the rows keep coming with a change of zero,
    so the pair looks whole. A healthy pair beside a healthy total keeps the
    pair.

    It never wins when it records less than ``_SHORT_BELOW`` of the pair over
    the periods both report: one frozen at its last reading writes a row of
    zero every period, so counting its periods let it bill the year at
    nothing. Nor, where the pair cannot be billed whole, when it reports
    fewer periods than the pair can still bill: a totals sensor with no
    statistics took a whole year to the fees floor on a pair whose registers
    disagreed on a single hour (discussion #66), and one that started late
    bills less of the window than the pair. One that started late but still
    covers more of it than a pair that stopped does win.
    """
    paired = _paired_keys(day, night) or set()
    shared = paired & total.keys()
    pair_kwh = sum(day[k] + night[k] for k in shared)
    total_kwh = sum(total[k] for k in shared)
    if total_kwh < _SHORT_BELOW * pair_kwh:
        return False
    if day.keys() != night.keys() or total.keys() - paired:
        return bool(total) and len(total) >= len(paired)
    return pair_kwh < _SHORT_BELOW * total_kwh


def _broken_registers(
    day_id: str,
    day: Mapping[date, float],
    night_id: str,
    night: Mapping[date, float],
    total: Mapping[date, float],
) -> str:
    """The registers of a pair its totals sensor stands in for that are
    broken, comma-separated, for the Repairs card.

    A half that recorded nothing, and a half that stopped while its twin or
    the total carries on. Both halves when they report the same days as the
    total and still fell short of it (:func:`_total_stands_in`): one of them
    is frozen, and the rows alone cannot say which. Halves that merely
    started late, or disagree on a day, are not named.
    """
    halves = ((day_id, day), (night_id, night))
    broken = [entity_id for entity_id, readings in halves if not readings]
    broken += [
        entity_id
        for entity_id in _stopped((*halves, ("total", total)))
        if entity_id != "total"
    ]
    if not broken and day.keys() == night.keys() and total.keys() <= day.keys():
        broken = [day_id, night_id]
    return ", ".join(broken)


def _live_only(readings: Mapping[date, float], start: date, end: date) -> bool:
    """Whether a meter read over ``[start, end]`` has today's live reading
    and no statistic before it.

    That is a sensor compiling no statistics at all (no ``state_class``, or
    ``measurement``) whose state still reads: the year to date is then billed
    on today alone, and nothing else says so, whatever the solar regime. Not
    for a window that opens today, where no meter has a day before it.
    """
    return start < end and end in readings and not _without_today(readings, end)
