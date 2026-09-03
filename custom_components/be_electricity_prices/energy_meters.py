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

"""Reading consumed and injected kWh out of the recorder.

Split out of coordinator.py. A true leaf. Two rules are encoded here rather
than at the call sites: read change and never sum, and prefer a wired
day/night register pair over the totals sensor so the hourly and the per-day
paths bill off the same meter."""

from __future__ import annotations

import logging

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC
from datetime import date
from datetime import datetime
from datetime import timedelta
from functools import partial
from homeassistant.components.sensor import ATTR_LAST_RESET
from homeassistant.components.sensor import ATTR_STATE_CLASS
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.const import STATE_UNKNOWN
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.core import State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from typing import Any

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
    is_offpeak,
)


_LOGGER = logging.getLogger(__name__)


async def _recorder_deltas(
    hass: HomeAssistant, entity_id: str, start: date, end: date, period: str
) -> list[tuple[datetime, float]]:
    """Recorder rows as (UTC slot start, delta) pairs, skipping unusable ones.

    Three callers unpacked the same five lines. What they share is the rule,
    not the loop: read ``change``, never ``sum``. ``sum`` is the cumulative
    total of a TOTAL sensor, so using it here would bill a whole meter reading
    for one slot. A row missing either field is skipped rather than treated as
    zero, because a zero is a real measurement.

    Callers keep their own bucketing: one wants the local DAY, one the UTC
    hour, one the local hour so it can split on the off-peak schedule.
    """
    # Fetched from a day earlier than asked so the bucket immediately before
    # the window is visible. Home Assistant seeds the first bucket's change
    # from the last statistic strictly EARLIER than the window, with no
    # lookback limit (_statistics_at_time), so when the run-up to the window is
    # missing that first bucket absorbs everything consumed during the gap. On
    # a year-to-date window anchored at 1 January that means last year's energy
    # billed to this year: measured at 536 kWh charged to a day that used 24.
    # It over-bills, it never self-corrects, and hours_seen reads full coverage
    # while it happens.
    window_start = dt_util.start_of_local_day(start).astimezone(UTC)
    rows = await _recorder_rows(
        hass, entity_id, start - timedelta(days=1), end, period, {"change", "sum"}
    )
    before = [r for r in rows if (r.get("start") or 0) < window_start.timestamp()]
    out: list[tuple[datetime, float]] = []
    negative = 0.0
    skip_first = not before
    for row in rows:
        ts = row.get("start")
        if ts is None or ts < window_start.timestamp():
            continue
        if skip_first:
            skip_first = False
            # Only suspect when a cumulative total already existed: a sensor
            # whose statistics begin inside the window is seeded from zero, so
            # its first change is genuinely its own energy.
            total = row.get("sum")
            change = row.get("change")
            if (
                total is not None
                and change is not None
                and float(change) < float(total)
            ):
                _LOGGER.warning(
                    "%s has no statistics immediately before %s, so its first "
                    "bucket carries %.1f kWh accumulated before the window; "
                    "that bucket is ignored rather than billed to this period",
                    entity_id,
                    start,
                    float(change),
                )
                continue
        delta = row.get("change")
        if ts is None or delta is None:
            continue
        value = float(delta)
        if value < 0.0:
            # A consumption or injection meter cannot run backwards over a
            # bucket, so this is an artefact rather than a measurement. Home
            # Assistant restarts its cumulative sum chain when the short-term
            # anchor is purged (an outage longer than purge_keep_days), and the
            # restart lands as one large negative bucket that cancels real
            # energy elsewhere in the window and drags the whole bill down.
            # Measured on a real chain restart: 57% of what the meter moved.
            # Dropping it bills the rest honestly; billing it bills a fiction.
            negative += value
            continue
        out.append((datetime.fromtimestamp(ts, tz=UTC), value))
    if negative:
        # Said out loud, because every path downstream of here would otherwise
        # render this as an ordinary small bill with nothing to distinguish it
        # from genuinely low consumption.
        _LOGGER.warning(
            "%s reported %.1f kWh of negative change between %s and %s, which a "
            "meter cannot do; those buckets are ignored. This usually means the "
            "recorder restarted its running total after an outage, and the "
            "affected period will read low until the statistics are rebuilt",
            entity_id,
            negative,
            start,
            end,
        )
    return out


async def _recorder_rows(
    hass: HomeAssistant,
    entity_id: str,
    start: date,
    end: date,
    period: str,
    fields: set[str] | None = None,
) -> list[Any]:
    """Fetch HA recorder ``change`` rows for ``entity_id`` over ``[start, end]``.

    Wraps ``statistics_during_period`` via the recorder's executor so a
    SQLite query never runs on the event loop. Returns a (possibly
    empty) list -- every failure mode (recorder not ready, no
    statistics, transient DB error) collapses to ``[]`` so callers can
    fall back to the fees-only floor without raising.

    Reads the ``change`` field, which the recorder defines as the delta
    of the cumulative ``sum`` between the bucket's first and last
    sample. Reading ``sum`` directly would yield the all-time running
    total -- summing those would multiply the bill by however many
    years of statistics the meter has accumulated.

    Requests the ``change`` in kWh via ``units={"energy": "kWh"}`` so a
    meter sensor that stores its statistics in Wh or MWh is normalised by
    HA's EnergyConverter rather than read as raw kWh, which would bill the
    user 1000x too much (Wh) or too little (MWh). The OptionsFlow picker
    restricts the choice to device_class=energy but not the unit, so a
    Wh / MWh sensor is a legitimate, reachable selection.

    Pass the date directly: HA's start_of_local_day treats a naive
    datetime as UTC, which round-trips correctly only for tz east of
    the prime meridian. Hand it the date so the function takes its
    date-typed branch and produces the unambiguous local midnight.
    """
    try:
        # mypy --strict flags both names because the recorder module
        # does not re-export them via __all__; they're public per HA's
        # docs and import-time errors degrade gracefully via the
        # ImportError handler below.
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        return []
    start_dt = dt_util.start_of_local_day(start).astimezone(UTC)
    # Anchor end_dt on the next local midnight so the bucket containing
    # ``end`` is included. ``start_of_local_day(end).astimezone(UTC) +
    # timedelta(days=1)`` would be exactly 24 UTC hours later, which
    # mis-aligns by one hour on Brussels DST seam days (the next local
    # midnight is 23 or 25 UTC hours away). Computing
    # start_of_local_day(end + 1 day) keeps the cap on the right local
    # boundary year-round.
    end_dt = dt_util.start_of_local_day(end + timedelta(days=1)).astimezone(UTC)
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start_dt,
            end_dt,
            {entity_id},
            period,
            {"energy": "kWh"},
            fields or {"change"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return []
    rows: list[Any] = list(stats.get(entity_id, []))
    return rows


def _reset_since(state: State, midnight: datetime) -> bool:
    """Did this meter start a new cycle after ``midnight``?

    ``last_reset`` is what a cycling meter publishes to say "my total went
    back to zero at this instant", and it is the only signal that survives
    both state classes: HA's ``utility_meter`` reports ``TOTAL`` when
    ``net_consumption`` is set and ``TOTAL_INCREASING`` otherwise, and cycles
    either way. Anything unparseable reads as "no reset", which keeps the
    caller on the plain delta.
    """
    raw = state.attributes.get(ATTR_LAST_RESET)
    if raw is None:
        return False
    parsed = raw if isinstance(raw, datetime) else dt_util.parse_datetime(str(raw))
    if parsed is None:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= midnight


async def _live_today_kwh(
    hass: HomeAssistant, entity_id: str, today: date
) -> float | None:
    """Today's kWh for ``entity_id`` from the live meter, or ``None``.

    Reads ``current cumulative total - total at local midnight`` from the
    state machine and the recorder's state history, bypassing the long-term
    daily statistics the past-day path relies on. This keeps the running year
    cost tracking today's consumption in real time and, crucially, keeps it
    moving when statistics compilation lags or stalls -- states are still
    recorded regardless. ``None`` means "no reliable live reading": the meter
    is unavailable / non-numeric, has no reading at midnight yet, or carries a
    unit that can't be converted to kWh; the caller then keeps the daily
    statistic as a fallback rather than risk a wrong figure.

    A reading below the midnight one means different things per state class,
    so the class decides how it is read: only ``total_increasing`` can be
    read as a counter reset, matching how the recorder's own statistics
    engine treats that class. A ``total`` meter is allowed to fall, and the
    signed delta is exactly what the recorder reports as that day's
    ``change``, so it is returned as-is.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        current = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
    state_class = state.attributes.get(ATTR_STATE_CLASS)

    midnight = dt_util.start_of_local_day(today).astimezone(UTC)
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.history import (
            get_significant_states,
        )
    except ImportError:
        return None
    try:
        history = await get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                midnight,
                midnight + timedelta(seconds=1),
                [entity_id],
                include_start_time_state=True,
                significant_changes_only=False,
                no_attributes=True,
            )
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return None
    rows = history.get(entity_id, [])
    if not rows or not isinstance(rows[0], State):
        return None
    try:
        opening = float(rows[0].state)
    except (TypeError, ValueError):
        return None
    delta = current - opening
    if delta < 0.0 and _reset_since(state, midnight):
        # The meter published a ``last_reset`` later than local midnight, so
        # it started a new cycle today and everything it has counted since is
        # today's consumption. This is the signal that actually generalises:
        # a utility_meter with net_consumption reports state_class TOTAL, not
        # TOTAL_INCREASING (HA returns one or the other on exactly that
        # option), and it still cycles. Gating on the class alone read its
        # monthly rollover as a genuine fall and returned minus the whole
        # previous cycle as today's kWh.
        delta = current
    elif delta < 0.0 and state_class == SensorStateClass.TOTAL_INCREASING:
        # A ``total_increasing`` meter that reset since midnight without
        # publishing ``last_reset``: the class alone promises it cannot fall,
        # so a fall is a reset.
        #
        # Gated on the state class on purpose. The picker accepts any
        # device_class=energy sensor, so a ``total`` register that nets
        # injection against consumption (a utility_meter with
        # net_consumption, a bidirectional meter) is a legitimate choice,
        # and it falls whenever the site exports more than it draws. Reading
        # that as a reset would bill its whole lifetime total as one day.
        delta = current
    if unit == UnitOfEnergy.KILO_WATT_HOUR:
        return delta
    try:
        return EnergyConverter.convert(delta, unit, UnitOfEnergy.KILO_WATT_HOUR)
    except HomeAssistantError:
        # Unknown / non-energy unit: fall back to the normalized daily
        # statistic rather than risk a 1000x mis-bill from an assumed unit.
        return None


async def _recorder_daily_kwh(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[date, float]:
    """Per-day kWh deltas for ``entity_id`` keyed by local-day date.

    Past days come from the recorder's long-term daily statistics. When
    ``end`` is today, that day is overridden with a live meter reading (see
    :func:`_live_today_kwh`) so the running year cost tracks today's usage in
    real time and does not freeze if statistics compilation lags or stalls;
    it falls back to the daily statistic when no live reading is available.
    """
    out: dict[date, float] = {}
    for when, delta in await _recorder_deltas(hass, entity_id, start, end, "day"):
        out[dt_util.as_local(when).date()] = delta
    if end == dt_util.now().date():
        live_today = await _live_today_kwh(hass, entity_id, end)
        if live_today is not None:
            out[end] = live_today
    return out


async def _recorder_hourly_kwh(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[datetime, float]:
    """Per-hour kWh deltas for ``entity_id`` keyed by UTC hour.

    Used by the TOU year-cost path: TOU contracts have a different
    energy rate per hour-of-day, so day-level granularity is too coarse.
    """
    out: dict[datetime, float] = {}
    for when, delta in await _recorder_deltas(hass, entity_id, start, end, "hour"):
        out[when.replace(minute=0, second=0, microsecond=0)] = delta
    return out


# Repeat reads of the SAME meter window inside one block. Opt-in and scoped,
# never a global TTL cache: the coordinator tick and the one-off quote both
# want a live read, and guessing a TTL for them is how a stale meter reaches
# a bill.
# Every entry key that changes what _resolve_daily_kwh reads. Spelled here
# from the constants this module already imports: the same tuple exists in
# flow_schemas as _METER_SENSOR_KEYS, but importing that would close a cycle
# on a leaf module.
_MEMO_METER_KEYS: tuple[str, ...] = (
    CONF_DAY_CONSUMPTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_CONSUMPTION_KWH,
    CONF_INJECTION_KWH,
)

_METER_MEMO: ContextVar[dict[Any, Any] | None] = ContextVar("_METER_MEMO", default=None)


@contextmanager
def memoise_meter_reads(store: dict[Any, Any]) -> Iterator[None]:
    """Serve repeat reads of one meter window from ``store`` inside this block.

    The comparison sweep prices N contracts against ONE household, and the
    household's metered kWh is the same for all of them: it depends on the
    entry and the window, never on the supplier being priced. Without this the
    year-to-date pass re-read the recorder over the whole window once per
    candidate, N+1 identical queries in a job that runs nightly and unattended
    on hardware that is often a Pi.

    ``store`` is passed in rather than created here for the same reason
    ``memoise_text_fetches`` does it: an ``asyncio.Task`` copies the context at
    creation, which copies the reference and not the dict, so tasks under one
    sweep share what the first of them read.
    """
    token = _METER_MEMO.set(store)
    try:
        yield
    finally:
        _METER_MEMO.reset(token)


async def _sum_hourly_kwh(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    start: date,
    end: date,
) -> dict[datetime, float]:
    """Per-UTC-hour kWh summed across ``entity_ids`` into one dict.

    Served from the memo inside a ``memoise_meter_reads`` block, keyed on
    exactly the arguments that decide the answer.

    A house with several consumption (or injection) sensors totals them
    hour by hour; used by the live YTD cost, the injection-credit and the
    backfill accrual so the binning is written once.
    """
    ids = tuple(entity_ids)
    memo = _METER_MEMO.get()
    key = ("hourly", ids, start, end)
    if memo is not None and key in memo:
        # A copy: callers merge today's live top-up into what they get back,
        # and handing two of them the same dict would have the second read
        # the first one's additions.
        return dict(memo[key])
    entity_ids = ids
    out: dict[datetime, float] = {}
    for entity_id in entity_ids:
        for utc_hour, kwh in (
            await _recorder_hourly_kwh(hass, entity_id, start, end)
        ).items():
            out[utc_hour] = out.get(utc_hour, 0.0) + kwh
    if memo is not None:
        memo[key] = dict(out)
    return out


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

    A meter with no reliable live reading contributes nothing and leaves the
    statistics figure standing, exactly as the per-day path degrades.
    """
    live_total = 0.0
    have_live = False
    for entity_id in entity_ids:
        live = await _live_today_kwh(hass, entity_id, today)
        if live is not None:
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
    for when, delta in await _recorder_deltas(hass, entity_id, start, end, "hour"):
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
) -> dict[date, tuple[float, float, float, float]] | None:
    """Per-day (day_cons, night_cons, day_inj, night_inj) from recorder.

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

      * **Nothing**: that side contributes zero.

    A side that has only one half of its register pair (e.g.
    ``CONF_DAY_CONSUMPTION_KWH`` set, ``CONF_NIGHT_CONSUMPTION_KWH``
    missing) *and no totals sensor* returns ``None`` so the caller falls
    back to the fees-only floor instead of silently undercounting the
    missing band. With a totals sensor the odd half is simply ignored and
    the side bills off the total, which is the rule the meters form
    enforces too (``flow_schemas.py:866``).

    Returns ``None`` when neither side has any meter inputs at all
    or when either side has an uncovered partial register wiring.
    """
    meter = entry.data.get(CONF_METER, METER_MONO)
    region = entry.data.get(CONF_REGION, "")
    window_start = start or date(today.year, 1, 1)
    # Keyed on the entry DATA this reads, never on the entry object: the
    # compare page passes a proxy carrying only .data, and reaching for an
    # entry_id here would break that page from a distance.
    memo = _METER_MEMO.get()
    key = (
        "daily",
        meter,
        region,
        tuple(entry.data.get(k) for k in _MEMO_METER_KEYS),
        window_start,
        today,
    )
    if memo is not None and key in memo:
        cached = memo[key]
        return None if cached is None else dict(cached)
    out: dict[date, list[float]] = {}

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
        if day_id and night_id:
            for day, kwh in (
                await _recorder_daily_kwh(hass, day_id, window_start, today)
            ).items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += kwh
            for day, kwh in (
                await _recorder_daily_kwh(hass, night_id, window_start, today)
            ).items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_night] += kwh
            return True
        if not total_id:
            return True  # nothing wired on this side; contributes zero
        per_day = await _recorder_daily_kwh(hass, total_id, window_start, today)
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
    inj_ok = await _side(
        entry.data.get(CONF_DAY_INJECTION_KWH),
        entry.data.get(CONF_NIGHT_INJECTION_KWH),
        entry.data.get(CONF_INJECTION_KWH),
        slot_day=2,
        slot_night=3,
    )
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


async def _measured_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
) -> MeasuredKwh:
    """Metered kWh for ``side`` over ``[start, end]``, with its coverage.

    The day/night register pair wins when both halves are wired, matching
    :func:`_resolve_daily_kwh`, :func:`_hourly_consumption_sensors` and the
    rule written down in ``const.py``. A half-wired pair with no totals sensor
    is refused through :func:`_partial_register_pair` rather than billing the
    wired band alone; the old totals-first ordering here was incidental (the
    chain simply fell off its end) and this makes the refusal deliberate.

    Coverage counts local days EITHER band of a wired pair reported, because a
    band that genuinely used nothing that day still leaves the day covered.
    That union is only safe while both halves are alive. A register producing
    no statistics contributes 0.0 kWh while the surviving half holds coverage
    at a full year, so a half-total came back labelled "measured (365 days)"
    and every caller believed it, at 30 to 37% of the real bill. The same shape
    at lower amplitude when one register merely stops mid-year, which needs no
    misconfiguration at all: a rename, an integration swap or a meter
    replacement is enough. So the two halves are compared before they are
    trusted.

    A register can be wired, valid, and silent: device_class=energy with no
    state_class compiles no long-term statistics at all, and neither does
    state_class=measurement. Nothing upstream of here rejects either.
    """
    if _partial_register_pair(entry, side):
        return MeasuredKwh(0.0, 0)
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    if day_id and night_id:
        d = await _recorder_daily_kwh(hass, day_id, start, end)
        n = await _recorder_daily_kwh(hass, night_id, start, end)
        if bool(d) != bool(n):
            # One half of the pair is wired but produced nothing whatsoever.
            # That is a broken pair rather than a band that used no energy, and
            # billing the surviving half alone is a wrong bill, not a partial
            # one, so refuse it the way a half-wired pair is already refused.
            _LOGGER.warning(
                "%s returned no statistics between %s and %s while %s did, so "
                "the %s pair cannot be billed. Check that sensor has a "
                "state_class of total_increasing and still exists",
                night_id if d else day_id,
                start,
                end,
                day_id if d else night_id,
                side,
            )
            return MeasuredKwh(0.0, 0)
        days = set(d) | set(n)
        if min(len(d), len(n)) * 2 < max(len(d), len(n)):
            # Both halves report, but one covers less than half the span of the
            # other, so they have diverged: one stopped, or started late. The
            # union would carry the survivor's coverage and present a partial
            # total as a whole one. Fall back to the days both halves actually
            # cover, which is the only part that can be billed honestly. The
            # figure is then short enough to be labelled scaled rather than
            # measured, which is disclosed to the user instead of silent.
            _LOGGER.warning(
                "%s covers %d days between %s and %s while %s covers %d, so "
                "the %s pair has diverged; only the overlap is billed",
                day_id,
                len(d),
                start,
                end,
                night_id,
                len(n),
                side,
            )
            days = set(d) & set(n)
        return MeasuredKwh(sum(d.values()) + sum(n.values()), len(days))
    if total_id:
        d = await _recorder_daily_kwh(hass, total_id, start, end)
        return MeasuredKwh(sum(d.values()), len(d))
    return MeasuredKwh(0.0, 0)


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

    Returns ``None`` when nothing is wired, the pair is half-wired, or the
    window recorded nothing. The caller then stays on the clock-hour weighting
    rather than inventing a profile.
    """
    if _partial_register_pair(entry, side):
        return None
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    ids = [i for i in ((day_id, night_id) if day_id and night_id else (total_id,)) if i]
    if not ids:
        return None
    per_hour: dict[int, float] = {}
    for entity_id in ids:
        for when, delta in await _recorder_deltas(hass, entity_id, start, end, "hour"):
            hour = dt_util.as_local(when).hour
            per_hour[hour] = per_hour.get(hour, 0.0) + delta
    total = sum(per_hour.values())
    if total <= 0:
        return None
    return {hour: kwh / total for hour, kwh in per_hour.items()}


def _partial_register_pair(entry: ConfigEntry, side: str) -> bool:
    """True when exactly one half of ``side``'s day/night register pair is wired.

    A half-wired pair cannot be billed FROM THE REGISTERS: the missing band's
    kWh are simply absent, so every path must refuse rather than quietly bill
    the wired half. A totals sensor on the same side changes that: it covers
    both bands completely, and the band split is recovered from hourly
    statistics, so the half-wired pair is merely redundant and the computation
    should proceed. Refusing regardless threw away a fully wired totals sensor
    and floored the year cost at fees only. The static
    per-day path has always enforced this; the hourly path (TOU / Impact /
    dynamic / exclusive-night) resolved each side independently and only
    bailed when BOTH were empty, so a half-wired consumption pair collapsed to
    "no consumption sensors" while a wired injection sensor kept crediting.
    That billed the feed-in credit against zero consumption and drove the YTD
    negative. Shared here so the two paths cannot drift apart again.
    """
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    return bool(day_id) ^ bool(night_id) and not total_id


def _kwh_sensor_ids(
    entry: ConfigEntry, side: str
) -> tuple[str | None, str | None, str | None]:
    """The (day, night, total) recorder entity ids configured for ``side``
    ("injection" or "consumption"); any element may be ``None``."""
    if side == "injection":
        return (
            entry.data.get(CONF_DAY_INJECTION_KWH),
            entry.data.get(CONF_NIGHT_INJECTION_KWH),
            entry.data.get(CONF_INJECTION_KWH),
        )
    return (
        entry.data.get(CONF_DAY_CONSUMPTION_KWH),
        entry.data.get(CONF_NIGHT_CONSUMPTION_KWH),
        entry.data.get(CONF_CONSUMPTION_KWH),
    )


def _hourly_consumption_sensors(entry: ConfigEntry) -> list[str]:
    """Recorder entity ids whose hourly kWh sums add up to total
    consumption.

    Prefer the full day + night register pair when BOTH halves are wired,
    matching ``_resolve_daily_kwh`` and the diagnostics roll-up and the
    documented rule in ``const.py`` ("when both are configured, the
    day/night registers win"). This helper used to check the totals sensor
    first, so an entry with both wirings was billed off a different meter
    on the hourly path (TOU / Impact / dynamic / exclusive-night and the
    backfill) than on the static per-day path, and the two figures drifted
    against each other for the same user.

    Falls back to the single totals sensor. Returns an empty list when
    nothing is wired, or when only one register half is wired and no total
    covers it, so a partial wiring can't silently undercount the missing
    band (caller surfaces the fees-only floor).
    """
    return _hourly_kwh_sensors(entry, "consumption")


def _hourly_injection_sensors(entry: ConfigEntry) -> list[str]:
    """Mirror of ``_hourly_consumption_sensors`` for the injection side.

    Registers first when both halves are wired, then the totals sensor.
    Returns an empty list when neither is available, so a partial register
    wiring doesn't get counted as injection coverage."""
    return _hourly_kwh_sensors(entry, "injection")


def _hourly_kwh_sensors(entry: ConfigEntry, side: str) -> list[str]:
    """The registers-then-total preference, once, for either side.

    Both sides spelled this out separately while reading the same three keys
    ``_kwh_sensor_ids`` already returns, so the preference order existed in
    three places: here twice and in ``_side_is_half_wired``. The order is the
    load-bearing part -- checking the total first bills the hourly path off a
    different meter than the static per-day path for a user who wired both,
    and the two figures then drift against each other.
    """
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    if day_id and night_id:
        return [day_id, night_id]
    if total_id:
        return [total_id]
    return []
