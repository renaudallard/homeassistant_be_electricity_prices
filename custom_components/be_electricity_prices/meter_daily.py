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

"""Per-day kWh and the measured volume over a window.

``_resolve_daily_kwh`` is what the static-contract bill reads: each day's
consumption and feed-in per register, with the stand-ins and the band ratio
a missing register needs. ``_measured_kwh`` totals a window and says how
much of it the meters cover. The reads themselves are ``energy_meters``,
called through the module so a test that patches one there reaches this
code too.
"""

from __future__ import annotations


from dataclasses import dataclass
from datetime import date, datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONSUMPTION_KWH,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_INJECTION_KWH,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_REGION,
    METER_MONO,
)
from .pricing import (
    MeterType,
    is_offpeak,
)
from . import energy_meters
from .energy_meters import (
    _LOGGER,
    _MEMO_METER_KEYS,
    _METER_MEMO,
    _bills_injection,
    _kwh_sensor_ids,
    _partial_register_pair,
)
from .meter_faults import (
    _broken_registers,
    _live_only,
    _paired_keys,
    _silent_periods,
    _silent_sensors,
    _split_today,
    _started_late,
    _stopped,
    _total_stands_in,
    _without_today,
)
from .meter_hourly import _metered_hourly_kwh


async def _recorder_daily_band_ratio(
    hass: HomeAssistant, entity_id: str, start: date, end: date, region: str
) -> dict[date, tuple[float, float]]:
    """Per-day (day_ratio, night_ratio) for ``entity_id``.

    Used for the totals-only + bi-hourly path: we don't have separate
    day / night registers, so we recover the band split from hourly
    statistics by binning each hour on ``is_offpeak``. The two ratios
    sum to 1.0 (or default to a day-of-week split for days with no
    accumulation, so a Sunday isn't billed at peak rate just because
    the hourly stats are flat).
    """
    per_day_day: dict[date, float] = {}
    per_day_night: dict[date, float] = {}
    moving_hours: dict[date, int] = {}
    for when, delta in await energy_meters._recorder_deltas(
        hass, entity_id, start, end, "hour"
    ):
        local = dt_util.as_local(when)
        bucket = local.date()
        if delta > 0.0:
            moving_hours[bucket] = moving_hours.get(bucket, 0) + 1
        if is_offpeak(local, region):
            per_day_night[bucket] = per_day_night.get(bucket, 0.0) + delta
        else:
            per_day_day[bucket] = per_day_day.get(bucket, 0.0) + delta
    out: dict[date, tuple[float, float]] = {}
    for day in set(per_day_day) | set(per_day_night):
        d = per_day_day.get(day, 0.0)
        n = per_day_night.get(day, 0.0)
        total = d + n
        if total > 0 and moving_hours.get(day, 0) > 1:
            out[day] = (d / total, n / total)
        else:
            # Either nothing moved, or it all moved in a single hour. The
            # second case is a sensor that is READ once a day rather than a
            # meter that ran for one hour: a supplier-portal poller or a
            # nightly fetch. Home Assistant still emits an hourly row either
            # way, so the daily total is right while every kWh lands in the
            # hour the reading arrived, and this would hand back (1, 0) or
            # (0, 1) for that day, every day, all year. Measured on a 2415 kWh
            # year against a true off-peak share of 0,457: a 04:00 poll billed
            # the distribution leg 31,7% low, a 13:00 poll 26,7% high, and the
            # bi-hourly energy rate splits on this same ratio so it compounds.
            # The day-of-week default is a poor estimate; a single hour is a
            # confident wrong one.
            out[day] = _default_band_ratio_for(day, region)
    return out


async def _resolve_daily_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    today: date,
    start: date | None = None,
    *,
    meter: MeterType | None = None,
    billed: dict[str, tuple[str, ...]] | None = None,
) -> dict[date, tuple[float, float, float, float]] | None:
    """Per-day (day_cons, night_cons, day_inj, night_inj) from recorder.

    ``meter`` overrides the entry's own meter type, and must be the meter the
    caller is about to BILL these kWh at. The comparison page quotes a target
    contract on a meter the household does not have ("what if I moved to a
    bi-hourly meter?"), and the split has to follow the rate that will be
    applied to it: a totals sensor left on the mono branch puts the whole day
    in the peak slot, and a caller billing peak / offpeak then charges every
    kWh of the year at the peak rate.

    ``start`` is the first day to read, defaulting to 1 January. The
    year-to-date caller passes the window it actually bills, which for an
    entry billing from its contract start date is later than 1 January: the
    days outside it are not only fetched for nothing, they are summed into
    the ``consumption_ytd_kwh`` and ``days_seen`` attributes, which then
    describe a different period from the cost printed beside them.

    Each side (consumption, injection) is resolved independently from
    one of three configurations:

      * **Day + night register pair** (``CONF_DAY_*_KWH`` +
        ``CONF_NIGHT_*_KWH``): the recorder gives one delta per day per
        register, fanned out into the corresponding band slots.

      * **Single totals sensor** (``CONF_CONSUMPTION_KWH`` /
        ``CONF_INJECTION_KWH``): one daily total per side, split by
        the ``meter`` setting (mono keeps everything in the "day" slot
        and lets the math sum it; bi/dynamic recovers the per-day
        band ratio from hourly statistics binned on ``is_offpeak``).

      * **Nothing**: that side contributes zero. So does the injection side
        of an entry with no solar regime, whatever is wired there
        (:func:`_bills_injection`).

    A side that has only one half of its register pair (e.g.
    ``CONF_DAY_CONSUMPTION_KWH`` set, ``CONF_NIGHT_CONSUMPTION_KWH``
    missing) *and no totals sensor* returns ``None`` so the caller falls
    back to the fees-only floor instead of silently undercounting the
    missing band. With a totals sensor the odd half is simply ignored and
    the side bills off the total, which is the rule the meters form
    enforces too (``flow_schemas_meters._incomplete_register_pairs``).

    ``billed``, when given, is filled with the sensors each side was billed
    off, under ``consumption`` and ``injection``, and those of a side that
    went silent under the other under ``silent``, of a pair the registers
    that did (:func:`_silent_sensors`): what the diagnostics name beside a
    per-day bill. Such a call reads the recorder rather than the
    memo, which keeps only the kWh.

    Returns ``None`` when neither side has any meter inputs at all
    or when either side has an uncovered partial register wiring.
    """
    meter = meter or entry.data.get(CONF_METER, METER_MONO)
    region = entry.data.get(CONF_REGION, "")
    window_start = start or date(today.year, 1, 1)
    # Keyed on the entry DATA this reads, never on the entry object: the
    # compare page passes a proxy carrying only .data, and reaching for an
    # entry_id here would break that page from a distance. The EFFECTIVE
    # meter goes in rather than the entry's, so one sweep can memoise the
    # household on its own meter and on a what-if meter side by side.
    memo = _METER_MEMO.get()
    key = (
        "daily",
        meter,
        region,
        tuple(entry.data.get(k) for k in _MEMO_METER_KEYS),
        # The regime decides whether the injection side is read, and a
        # what-if row quoting one shares the household's meter keys.
        _bills_injection(entry),
        window_start,
        today,
    )
    if memo is not None and key in memo and billed is None:
        cached = memo[key]
        return None if cached is None else dict(cached)
    out: dict[date, list[float]] = {}
    # The days only one half of a register pair reported, keyed by the side's
    # day slot. On the consumption side they leave both sides: a day whose
    # consumption is unknown must not be credited its feed-in, or counted in
    # days_seen as billed. On the injection side the pair already leaves them
    # out, and the day is billed on its consumption (_silent_periods).
    pair_gaps: dict[int, set[date]] = {}
    # The days each wired side reported before today, keyed by its day slot,
    # for the check that one side did not go silent under the other, and the
    # last day each register of a pair reported, to name the one that did.
    reported: dict[int, set[date]] = {}
    last: dict[int, tuple[tuple[str, date], ...]] = {}

    async def _side(
        day_id: str | None,
        night_id: str | None,
        total_id: str | None,
        slot_day: int,
        slot_night: int,
    ) -> bool:
        """Resolve one side (consumption or injection) into ``out``.

        Returns False when this side has a partial register wiring and no
        totals sensor to fall back on (caller surfaces the fees-only floor);
        True otherwise.
        """
        if bool(day_id) ^ bool(night_id) and not total_id:
            return False
        side = "consumption" if slot_day == 0 else "injection"
        per_day: dict[date, float] | None = None
        if day_id and night_id:
            d, n, (day_today, night_today) = _split_today(
                await energy_meters._recorder_daily_kwh(
                    hass, day_id, window_start, today
                ),
                await energy_meters._recorder_daily_kwh(
                    hass, night_id, window_start, today
                ),
                today,
            )
            days = _paired_keys(d, n)
            # The totals sensor bills the side instead where the pair cannot
            # be billed whole or falls short of it (_total_stands_in): the
            # total covers both bands on every day, where the pair can only
            # bill the days both halves report, or none at all. Today is left
            # out of the comparison, as it is of the pair's.
            if total_id:
                read = await energy_meters._recorder_daily_kwh(
                    hass, total_id, window_start, today
                )
                if _total_stands_in(_without_today(read, today), d, n):
                    per_day = read
            if per_day is None:
                if days is None:
                    # A dead half is refused like a missing one: billing the
                    # surviving band alone read a silent night register as a
                    # year that used no night energy, 32% under the real bill
                    # with every day counted as seen.
                    return False
                for day in days:
                    row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                    row[slot_day] += d[day]
                    row[slot_night] += n[day]
                gaps = pair_gaps[slot_day] = set(d) ^ set(n)
                reported[slot_day] = days
                if d:
                    last[slot_day] = ((day_id, max(d)), (night_id, max(n)))
                if (
                    day_today is not None
                    and night_today is not None
                    and not _stopped(((day_id, d), (night_id, n)))
                ):
                    row = out.setdefault(today, [0.0, 0.0, 0.0, 0.0])
                    row[slot_day] += day_today
                    row[slot_night] += night_today
                elif day_today is not None or night_today is not None:
                    # Read, but not billable as a pair: today goes the way of
                    # any day one half did not report, or it would be billed
                    # now and taken back at midnight.
                    gaps.add(today)
                if billed is not None:
                    billed[side] = (day_id, night_id)
                return True
        if not total_id:
            return True  # nothing wired on this side; contributes zero
        if billed is not None:
            billed[side] = (total_id,)
        if per_day is None:
            per_day = await energy_meters._recorder_daily_kwh(
                hass, total_id, window_start, today
            )
        reported[slot_day] = set(per_day) - {today}
        if meter in ("bi", "dynamic"):
            ratios = await _recorder_daily_band_ratio(
                hass, total_id, window_start, today, region
            )
            for day, total in per_day.items():
                d_ratio, n_ratio = ratios.get(day, _default_band_ratio_for(day, region))
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += total * d_ratio
                row[slot_night] += total * n_ratio
        else:  # mono: route everything into the "day" slot
            for day, total in per_day.items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += total
        return True

    cons_ok = await _side(
        entry.data.get(CONF_DAY_CONSUMPTION_KWH),
        entry.data.get(CONF_NIGHT_CONSUMPTION_KWH),
        entry.data.get(CONF_CONSUMPTION_KWH),
        slot_day=0,
        slot_night=1,
    )
    inj_ok = not _bills_injection(entry) or await _side(
        entry.data.get(CONF_DAY_INJECTION_KWH),
        entry.data.get(CONF_NIGHT_INJECTION_KWH),
        entry.data.get(CONF_INJECTION_KWH),
        slot_day=2,
        slot_night=3,
    )
    if today in out and today == dt_util.now().date():
        # A side with no day before today in the window has nothing but
        # today's live reading, which comes off the state history, and a meter
        # compiling no statistics still has that. Today's compiled hours say
        # whether it records, as they do for the hourly walks (_metered_sides).
        # On the first day of the window that is both sides: comparing nothing
        # credited a dead feed-in meter all day, or billed a dead consumption
        # meter, until midnight took it back. On a later day it is a meter
        # whose statistics start today: judged on no past day, a new feed-in
        # meter was called silent and a new consumption meter took the day to
        # the fees floor, while the hourly walks billed both.
        for slot, days in reported.items():
            if days:
                continue
            side = "consumption" if slot == 0 else "injection"
            hours = await _metered_hourly_kwh(hass, entry, side, today, today)
            if hours is None:
                # One half of a pair compiled hours today and the other none:
                # the dead half every later day refuses, so today does too.
                out.clear()
            elif hours.kwh:
                reported[slot] = {today}
    silent, both, feed_in = _silent_periods(
        reported.get(0), reported.get(2), out.keys()
    )
    if billed is not None and silent is not None:
        slot, other = (0, 2) if silent == "consumption" else (2, 0)
        billed["silent"] = _silent_sensors(
            billed.get(silent, ()), last.get(slot, ()), reported.get(other, set())
        )
    for day in pair_gaps.get(0, set()) | both:
        out.pop(day, None)
    for day in feed_in:
        if day in out:
            out[day][2] = out[day][3] = 0.0
    if not (cons_ok and inj_ok):
        resolved = None
    elif not out:
        resolved = None
    else:
        resolved = {day: (r[0], r[1], r[2], r[3]) for day, r in out.items()}
    if memo is not None:
        # None is memoised too: "this household has no usable meter wiring"
        # costs the same recorder round trips to establish as a reading does,
        # and it does not change between two candidates either.
        memo[key] = None if resolved is None else dict(resolved)
    return resolved


def _default_band_ratio_for(day: date, region: str) -> tuple[float, float]:
    """Time-weighted (day_ratio, night_ratio) fallback for a day with no
    hourly recorder stats yet.

    Assumes uniform consumption across the day's 24 hours (the most
    neutral guess without a usage profile) and uses the region's
    bi-horaire schedule (so a Wallonia day picks up the 11-17 off-peak
    window, a Flanders weekday holiday stays peak). Replaces a previous
    hardcoded (1.0, 0.0) default that systematically pushed totals into
    the peak band when hourly stats lagged daily stats."""
    # Construct each local clock hour directly instead of advancing an
    # aware datetime by a fixed UTC timedelta: the latter shifts by one
    # hour on each DST transition, mislabelling one hour twice a year.
    # is_offpeak only reads the local hour + weekday, both of which are
    # well-defined per local clock hour even on DST days.
    peak_hours = 0
    for hour in range(24):
        when = datetime(
            day.year,
            day.month,
            day.day,
            hour,
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        )
        if not is_offpeak(when, region):
            peak_hours += 1
    if peak_hours == 0:
        return (0.0, 1.0)
    return (peak_hours / 24.0, (24 - peak_hours) / 24.0)


@dataclass(frozen=True)
class MeasuredKwh:
    """A metered kWh total together with how much of the window it covers.

    ``days_with_data`` is what separates "no sensor wired" from "wired and it
    genuinely reads zero", which a bare float cannot express: a net-metered
    consumption register whose year nets to zero and an entry with no meters
    at all both sum to 0,0. Callers that turn a window into a yearly volume
    need the day count to decide whether the total is worth believing.
    """

    kwh: float
    days_with_data: int
    # The register(s) of a day/night pair that record nothing, or stopped
    # while the other half carries on, and a meter that reads live today with
    # no statistics before it, comma-separated; empty when the side is whole
    # or none is wired. What the Repairs card names, since the figure itself
    # only says that less was billed, not which sensor to look at. A register
    # that STARTED late is named only while the pair covers clearly less of
    # the window than its twin (:func:`_started_late`).
    pair_fault: str = ""
    # Whether the side's totals sensor bills it in full instead of the pair,
    # so the registers named above leave the cost as it is.
    covered: bool = False


async def _measured_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
    warn: bool = False,
) -> MeasuredKwh:
    """Metered kWh for ``side`` over ``[start, end]``, with its coverage.

    The day/night register pair wins when both halves are wired, matching
    :func:`_resolve_daily_kwh`, :func:`_hourly_consumption_sensors` and the
    rule written down in ``const.py``. A half-wired pair with no totals sensor
    is refused through :func:`_partial_register_pair` rather than billing the
    wired band alone; the old totals-first ordering here was incidental (the
    chain simply fell off its end) and this makes the refusal deliberate.

    Coverage counts the local days BOTH bands of a wired pair reported
    (:func:`_paired_keys`), and the kWh are those days' only. Counting the days
    either band reported let a register producing no statistics hold coverage
    at a full year on the surviving half, so a half-total came back labelled
    "measured (365 days)" at 30 to 37% of the real bill. The same shape at lower
    amplitude when one register merely stops mid-year, which needs no
    misconfiguration at all: a rename, an integration swap or a meter
    replacement is enough.

    A register can be wired, valid, and silent: device_class=energy with no
    state_class compiles no long-term statistics at all, and neither does
    state_class=measurement. Nothing upstream of here rejects either.

    ``warn`` logs what a broken pair did to the figure. Only the daily meter
    check passes it, which reads the window the bill reads: the warnings came
    from the yearly volume, over the trailing year, and named a window, and at
    times a sensor, that the year to date did not bill; the compare page and
    the projections logged them again on every read.
    """
    if _partial_register_pair(entry, side):
        return MeasuredKwh(0.0, 0)
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    if day_id and night_id:
        d, n, (day_today, night_today) = _split_today(
            await energy_meters._recorder_daily_kwh(hass, day_id, start, end),
            await energy_meters._recorder_daily_kwh(hass, night_id, start, end),
            end,
        )
        stopped = _stopped(((day_id, d), (night_id, n)))
        today_kwh, today_days = (
            (day_today + night_today, 1)
            if day_today is not None and night_today is not None and not stopped
            else (0.0, 0)
        )
        days = _paired_keys(d, n)
        # A wired totals sensor measures what the pair cannot, on the rule
        # every billing path follows, and the broken registers are still
        # named, so the Repairs card gets them fixed.
        total = (
            await energy_meters._recorder_daily_kwh(hass, total_id, start, end)
            if total_id
            else {}
        )
        past_total = _without_today(total, end)
        use_total = bool(total_id) and _total_stands_in(past_total, d, n)
        if warn and days is None:
            # One half of the pair is wired but produced nothing whatsoever.
            # That is a broken pair rather than a band that used no energy, and
            # billing the surviving half alone is a wrong bill, not a partial
            # one, so refuse it the way a half-wired pair is already refused.
            dead, alive = (night_id, day_id) if d else (day_id, night_id)
            _LOGGER.warning(
                "%s returned no statistics between %s and %s while %s did, so "
                "the %s pair cannot be billed%s. Check that sensor has a "
                "state_class of total_increasing and still exists",
                dead,
                start,
                end,
                alive,
                side,
                f"; its totals sensor {total_id} is billed instead"
                if use_total
                else "",
            )
        elif warn and days is not None and set(d) != set(n):
            # Both halves report, but not on the same days: one stopped, or
            # started late. The figure over the days both cover is then short
            # enough to be labelled scaled rather than measured, which is
            # disclosed to the user instead of silent.
            _LOGGER.warning(
                "%s covers %d days between %s and %s while %s covers %d, so "
                "the %s pair has diverged; %s",
                day_id,
                len(d),
                start,
                end,
                night_id,
                len(n),
                side,
                f"its totals sensor {total_id} is billed instead"
                if use_total
                else f"only the {len(days)} days both report are billed",
            )
        if use_total:
            return MeasuredKwh(
                sum(total.values()),
                len(total),
                pair_fault=_broken_registers(day_id, d, night_id, n, past_total),
                covered=True,
            )
        if days is None:
            return MeasuredKwh(0.0, 0, pair_fault=night_id if d else day_id)
        if not d:
            # Neither half has recorded anything in the window yet, today
            # aside. A half that reads live today all the same compiles no
            # statistics, and the year is billed on today's reading alone.
            live_only = [
                entity_id
                for entity_id, live in ((day_id, day_today), (night_id, night_today))
                if live is not None and start < end
            ]
            if total_id and _live_only(total, start, end):
                live_only.append(total_id)
            return MeasuredKwh(today_kwh, today_days, pair_fault=", ".join(live_only))
        return MeasuredKwh(
            sum(d[x] + n[x] for x in days) + today_kwh,
            len(days) + today_days,
            pair_fault=", ".join(stopped + _started_late(day_id, d, night_id, n)),
        )
    if total_id:
        return await _measured_total(hass, total_id, start, end)
    return MeasuredKwh(0.0, 0)


async def _measured_total(
    hass: HomeAssistant, total_id: str, start: date, end: date
) -> MeasuredKwh:
    """A totals sensor's kWh over ``[start, end]`` and the days it covers,
    naming it when it has only today's live reading (:func:`_live_only`)."""
    d = await energy_meters._recorder_daily_kwh(hass, total_id, start, end)
    return MeasuredKwh(
        sum(d.values()),
        len(d),
        pair_fault=total_id if _live_only(d, start, end) else "",
    )
