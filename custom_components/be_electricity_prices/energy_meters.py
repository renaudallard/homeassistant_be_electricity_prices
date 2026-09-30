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

Split out of coordinator.py. A true leaf, and the layer the other meter
modules read through: ``meter_faults`` judges the registers,
``meter_hourly`` builds the hourly series and ``meter_daily`` the per-day
and measured volumes. One rule is encoded here rather than at the call
sites: read change and never sum. The other, prefer a wired day/night
register pair over the totals sensor so the hourly and the per-day paths
bill off the same meter, is ``meter_faults``'s."""

from __future__ import annotations

import logging

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, date, datetime, timedelta
from functools import partial
from homeassistant.components.sensor import (
    ATTR_LAST_RESET,
    ATTR_STATE_CLASS,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter
from typing import Any, TypeVar

from .const import (
    CONF_CONSUMPTION_KWH,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_INJECTION_KWH,
    METER_SENSOR_KEYS,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_SOLAR_REGIME,
    SOLAR_REGIME_NONE,
)
from .year_ahead import YEAR_AHEAD, YearAhead, last_year


_LOGGER = logging.getLogger(__name__)

_K = TypeVar("_K")
_P = TypeVar("_P", date, datetime)


# A total_increasing reading below this share of the previous one is a reset,
# anything above it a dip: the line Home Assistant's own statistics draw
# (sensor/recorder.py, reset_detected).
_RESET_BELOW = 0.9


def _read_ahead(day: date) -> bool:
    """Whether ``day`` is read off last year (:mod:`year_ahead`)."""
    ahead = YEAR_AHEAD.get()
    return ahead is not None and day >= ahead.pivot


async def _recorder_deltas(
    hass: HomeAssistant, entity_id: str, start: date, end: date, period: str
) -> list[tuple[datetime, float]]:
    """:func:`_read_deltas`, with the days from the year-ahead pivot on read
    off last year's same days (:mod:`year_ahead`).

    Each slot keeps its local wall time on the day it lands on. A slot that
    lands twice, the repeated hour of an autumn change or 28 February read
    for a 29th, is summed rather than dropped, so a day keeps its energy.
    """
    ahead = YEAR_AHEAD.get()
    if ahead is None or not _read_ahead(end):
        return await _read_deltas(hass, entity_id, start, end, period)
    first = max(start, ahead.pivot)
    rows = (
        await _read_deltas(hass, entity_id, start, first - timedelta(days=1), period)
        if start < first
        else []
    )
    by_day: dict[date, list[date]] = {}
    day = first
    while day <= end:
        by_day.setdefault(last_year(day), []).append(day)
        day += timedelta(days=1)
    moved: dict[datetime, float] = {}
    for when, delta in await _read_deltas(
        hass, entity_id, last_year(first), last_year(end), period
    ):
        local = dt_util.as_local(when)
        for target in by_day.get(local.date(), ()):
            slot = datetime.combine(target, local.timetz()).astimezone(UTC)
            moved[slot] = moved.get(slot, 0.0) + delta
    return rows + sorted(moved.items())


async def _read_deltas(
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
    empty) list: every failure mode (recorder not ready, no
    statistics, transient DB error) collapses to ``[]`` so callers can
    fall back to the fees-only floor without raising.

    Reads the ``change`` field, which the recorder defines as the delta
    of the cumulative ``sum`` between the bucket's first and last
    sample. Reading ``sum`` directly would yield the all-time running
    total: summing those would multiply the bill by however many
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


def _cycled_total(readings: list[float]) -> float:
    """What a ``total_increasing`` meter counted over ``readings``, in order.

    Home Assistant's statistics walk the states one by one: a reading below
    0.9 x the one before it starts a new cycle counted from zero, and the
    cycle it ends keeps what it counted up to its last reading. A smaller fall
    is a dip and nets against the cycle it happens in. A negative reading is
    skipped, as the statistics skip it. The first reading is the opening one.
    """
    start = prev = readings[0]
    total = 0.0
    for value in readings[1:]:
        if value < 0.0:
            continue
        if value < _RESET_BELOW * prev:
            total += prev - start
            start = 0.0
        prev = value
    return total + prev - start


async def _live_today_kwh(
    hass: HomeAssistant, entity_id: str, today: date
) -> float | None:
    """Today's kWh for ``entity_id`` from the live meter, or ``None``.

    Reads ``current cumulative total - total at local midnight`` from the
    state machine and the recorder's state history, bypassing the long-term
    daily statistics the past-day path relies on. This keeps the running year
    cost tracking today's consumption in real time and, crucially, keeps it
    moving when statistics compilation lags or stalls: states are still
    recorded regardless. ``None`` means "no reliable live reading": the meter
    is unavailable / non-numeric, has no reading at midnight yet, or carries a
    unit that can't be converted to kWh; the caller then keeps the daily
    statistic as a fallback rather than risk a wrong figure.

    A ``total_increasing`` meter is walked through today's states the way
    Home Assistant's statistics walk them (:func:`_cycled_total`): a reading
    below 0.9 x the one before it is a reset, a smaller fall a dip. Any other
    meter reads ``current - midnight``, and a fall below the midnight reading
    is a reset only when the meter says so (``last_reset``). A day that nets
    below zero reads as zero, the same answer the past days get:
    :func:`_recorder_deltas` drops a negative ``change``, because a sum-chain
    restart looks exactly like one. A register netting export against
    consumption is therefore not supported, and today must not bill it signed
    only to have midnight take the figure back.

    Served from the memo inside a ``memoise_meter_reads`` block, keyed on the
    meter and the day: the walk reads every state since midnight, and the
    comparison sweep asked it again for each candidate billed per hour.
    """
    memo = _METER_MEMO.get()
    key = ("live_today", entity_id, today)
    if memo is not None and key in memo:
        cached: float | None = memo[key]
        return cached
    kwh = await _read_live_today_kwh(hass, entity_id, today)
    if memo is not None:
        memo[key] = kwh
    return kwh


async def _read_live_today_kwh(
    hass: HomeAssistant, entity_id: str, today: date
) -> float | None:
    """:func:`_live_today_kwh` without the memo."""
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
    # The whole day, not only the midnight reading: a reset is judged against
    # the reading before it, so it can only be found by walking the states.
    # The minimal response keeps the rows after the first one light.
    try:
        history = await get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                midnight,
                None,
                [entity_id],
                include_start_time_state=True,
                significant_changes_only=False,
                minimal_response=True,
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
    if state_class == SensorStateClass.TOTAL_INCREASING:
        # Walked state by state as the statistics walk it, so a reset that
        # climbed back to within 10% of the midnight reading, or past it, reads
        # today what it reads after midnight. Comparing the current reading
        # with the midnight one alone read the first as a dip and the second
        # as the difference. The class promises the meter cannot fall, so only
        # this class is walked for resets.
        #
        # A ``total`` register that nets injection against consumption (a
        # utility_meter with net_consumption, a bidirectional meter) can be
        # wired, since the picker accepts any device_class=energy sensor, and
        # it falls whenever the site exports more than it draws. Walking it for
        # resets would bill its whole lifetime total as one day.
        readings = [opening]
        for row in rows[1:]:
            raw = row.state if isinstance(row, State) else row.get("state")
            try:
                readings.append(float(str(raw)))
            except ValueError:
                continue
        readings.append(current)
        # A day that nets below zero reads as zero, as the past days drop a
        # negative change.
        delta = max(_cycled_total(readings), 0.0)
    elif delta < 0.0 and _reset_since(state, midnight):
        # The meter published a ``last_reset`` later than local midnight, so
        # it started a new cycle today and everything it has counted since is
        # today's consumption. This is the signal that actually generalises:
        # a utility_meter with net_consumption reports state_class TOTAL, not
        # TOTAL_INCREASING (HA returns one or the other on exactly that
        # option), and it still cycles. Gating on the class alone read its
        # monthly rollover as a genuine fall and returned minus the whole
        # previous cycle as today's kWh. A net_consumption cycle can itself
        # stand below zero after a day of export, and that reads as zero like
        # any other fall.
        delta = max(current, 0.0)
    elif delta < 0.0:
        # Any other fall, a dip included, is what the past days drop, so today
        # drops it too.
        delta = 0.0
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
    # Not on a 31 December read ahead, whose day is last year's like the rest.
    if end == dt_util.now().date() and not _read_ahead(end):
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
# Every entry key that changes what _resolve_daily_kwh reads, which is the
# same six the meters step renders. Shared from const, where flow_schemas takes
# them from too, rather than spelled out twice.
_MEMO_METER_KEYS: tuple[str, ...] = METER_SENSOR_KEYS

_METER_MEMO: ContextVar[dict[Any, Any] | None] = ContextVar("_METER_MEMO", default=None)


def _bills_injection(entry: ConfigEntry) -> bool:
    """Whether the bill reads the injection meters at all.

    Not without a solar regime: nothing is credited or netted then, so an
    injection pair wired for the Energy dashboard must not be able to refuse
    or shorten the year. One of them compiling no statistics took a
    household with no panels to the fees floor (discussion #66). Read off
    ``entry.data``, so a compare what-if quoting a regime reads them for its
    own row.
    """
    return bool(
        entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE) != SOLAR_REGIME_NONE
    )


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


@contextmanager
def reading_year_ahead(ahead: YearAhead) -> Iterator[None]:
    """Read the days from ``ahead.pivot`` off last year inside this block.

    Outside any memo: the memo keys on the window, which a read ahead
    answers differently.
    """
    ahead_token = YEAR_AHEAD.set(ahead)
    memo_token = _METER_MEMO.set(None)
    try:
        yield
    finally:
        _METER_MEMO.reset(memo_token)
        YEAR_AHEAD.reset(ahead_token)


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
    load-bearing part: checking the total first bills the hourly path off a
    different meter than the static per-day path for a user who wired both,
    and the two figures then drift against each other.
    """
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    if day_id and night_id:
        return [day_id, night_id]
    if total_id:
        return [total_id]
    return []
