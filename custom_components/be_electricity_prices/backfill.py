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

"""Long-term-statistics backfill for Belgian Electricity Prices.

Populates the recorder's hourly statistics for this entry's price
sensors over an arbitrary date range so the Energy dashboard and the
Statistics graph card can show price history that predates the entry's
first live update tick.

Reads the same data sources as the live coordinator (per-month tariff
cards via :func:`_snapshot_for_month`, ENTSO-E historical spots via the
coordinator's persistent cache) and pushes ``mean`` rows through
:func:`async_import_statistics` keyed on each sensor's entity id.

Two entry points:

* :func:`backfill_range` -- service-call path. Always runs over the
  requested range; with ``clear=True`` deletes the range first so a
  user who fixed their tariff card can redo a window.
* :func:`backfill_if_missing` -- automatic one-shot called from
  ``async_setup_entry``. Probes the recorder for statistics at the Jan
  1 anchor and only runs when none exist, so we don't redo the work on
  every HA restart.
"""

from __future__ import annotations

import calendar
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .coordinator import (
    BePricesCoordinator,
    _historical_injection_rate,
    _hourly_consumption_sensors,
    _hourly_injection_sensors,
    _recorder_hourly_kwh,
    _snapshot_for_month,
)
from .pricing import compute_breakdown
from .providers import DynamicRates, get as get_extractor
from .providers.base import SupplierSnapshot

_LOGGER = logging.getLogger(__name__)

# Sensor description ``key`` values whose live ``native_value`` is a
# EUR/kWh price. Each one becomes one ``mean`` statistic id during
# backfill. Kept in sync by hand with sensor.py (small, stable list);
# pulling it from the SENSORS / INJECTION_SENSORS tuples would couple
# this module to the entity-construction path for no real win -- the
# backfill values come straight out of compute_breakdown, not from the
# live entities.
_PRICE_SENSOR_KEYS: tuple[str, ...] = (
    "current_price",
    "energy_component",
    "network_component",
    "taxes_component",
)
_INJECTION_PRICE_SENSOR_KEY = "injection_price"
_COST_SENSOR_KEY = "current_year_cost"


def _hours_in_month(month_first: date) -> int:
    if month_first.month == 12:
        next_first = date(month_first.year + 1, 1, 1)
    else:
        next_first = date(month_first.year, month_first.month + 1, 1)
    return (next_first - month_first).days * 24


def _solar_kva(entry: ConfigEntry) -> float:
    try:
        kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return kva if kva > 0.0 else 0.0


def _stat_id(hass: HomeAssistant, entry: ConfigEntry, key: str) -> str | None:
    """Resolve the entity id (== statistic id) for one of this entry's sensors.

    Looks up the entity registry by unique id. Returns ``None`` when
    the entity hasn't been registered yet -- callers skip silently
    rather than fabricating a slug from the description key, which
    would diverge from the user's renamed entity id.
    """
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}"
    )


def _hour_iter(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour anchors in [start, end), aligned to the top of each hour."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        cur += timedelta(hours=1)
    out: list[datetime] = []
    while cur < end:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def _floor_to_hour_utc(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _normalize_window(
    start: datetime | date | None, end: datetime | date | None
) -> tuple[datetime, datetime]:
    """Return aware UTC [start_utc, end_utc) clamped to whole-hour buckets.

    The default window is [Jan 1 00:00 local, current hour). End is
    exclusive so we don't write a row for the in-progress hour the
    live coordinator is about to fill itself.
    """
    now_local = dt_util.now()
    if start is None:
        start_local = now_local.replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    elif isinstance(start, datetime):
        start_local = (
            start
            if start.tzinfo is not None
            else start.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        )
    else:
        start_local = datetime.combine(
            start, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
    if end is None:
        end_local = now_local
    elif isinstance(end, datetime):
        end_local = (
            end
            if end.tzinfo is not None
            else end.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        )
    else:
        end_local = datetime.combine(
            end, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
    start_utc = _floor_to_hour_utc(start_local)
    end_utc = _floor_to_hour_utc(end_local)
    return start_utc, end_utc


async def _existing_stat_window(
    hass: HomeAssistant, statistic_id: str, anchor: datetime
) -> bool:
    """Return True when at least one statistic row exists at ``anchor``.

    Used by :func:`backfill_if_missing` to derive the "is the recorder
    already populated" signal directly from the recorder, so we never
    need to persist a separate "backfill done" flag that would go
    stale across DB resets or supplier changes.
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        return False
    try:
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            anchor,
            anchor + timedelta(hours=1),
            {statistic_id},
            "hour",
            None,
            {"mean"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return False
    return bool(rows.get(statistic_id))


async def _clear_range(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Delete every statistic row for ``statistic_ids``.

    The recorder API doesn't expose a per-range delete; this drops the
    full series and the next backfill repopulates it. Acceptable for
    our use case (one full year, one user-triggered call).
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import clear_statistics
    except ImportError:
        return
    instance = get_instance(hass)
    await instance.async_add_executor_job(clear_statistics, instance, statistic_ids)


async def _ensure_dynamic_spots(
    coordinator: BePricesCoordinator, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Make sure ``coordinator._historical_spots`` covers [start, end] for a
    dynamic supplier, then return the spot dict.

    Reuses the coordinator's existing ENTSO-E backfill helper so the
    bulk-fetch logic (week-sized chunks, partial-day tolerance, negative
    cache) stays in one place. Returns an empty dict for non-dynamic
    suppliers; callers should not look up spots in that case.
    """
    snap = coordinator._snapshot
    if snap is None or not isinstance(snap.energy, DynamicRates):
        return {}
    await coordinator._ensure_historical_spots(start.date(), end.date())
    return coordinator._historical_spots


async def _backfill_price_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
    spots: dict[datetime, float],
) -> dict[str, int]:
    """Write ``mean`` rows for every price sensor across ``hours``.

    Returns a per-statistic-id row count for the service response so
    the caller (or a CLI user) can verify the backfill landed.
    Sensors that have no entity in the registry yet (auto path firing
    before platform setup completes) are skipped silently and reported
    with a 0 count.
    """
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )

    # mypy --strict flags StatisticMeanType because the recorder module
    # doesn't re-export it via __all__; same shape coordinator.py uses
    # for statistics_during_period and get_instance.
    from homeassistant.components.recorder.statistics import (  # type: ignore[attr-defined]
        StatisticMeanType,
        async_import_statistics,
    )

    snap = coordinator._snapshot
    assert snap is not None
    extractor = get_extractor(entry.data[CONF_SUPPLIER])
    contract = entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data[CONF_DSO]
    meter = entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    keys = list(_PRICE_SENSOR_KEYS)
    if regime == SOLAR_REGIME_INJECTION:
        keys.append(_INJECTION_PRICE_SENSOR_KEY)

    # Resolve statistic ids up front; skip the whole pass if nothing
    # is registered yet.
    stat_ids: dict[str, str] = {}
    for key in keys:
        sid = _stat_id(hass, entry, key)
        if sid is not None:
            stat_ids[key] = sid
    if not stat_ids:
        _LOGGER.debug(
            "backfill: no price-sensor entities registered yet for %s",
            entry.entry_id,
        )
        return {}

    # Cache per-month snapshot lookups so a 365-day window touches at
    # most 12 archive fetches.
    month_cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in month_cache:
            month_cache[month_first] = await _snapshot_for_month(
                hass,
                coordinator._session,
                extractor,
                contract,
                region,
                month_first,
                snap,
            )
        return month_cache[month_first]

    rows_per_key: dict[str, list[Any]] = {key: [] for key in stat_ids}
    for utc_hour in hours:
        local = dt_util.as_local(utc_hour)
        snap_h = await _snap_for(date(local.year, local.month, 1))
        spot = spots.get(utc_hour) if spots else None
        # Dynamic supplier without a spot for this hour: nothing to
        # write, the formula factor*spot+base needs both. Fixed/var
        # contracts pass spot=None and ignore it inside compute_breakdown.
        if isinstance(snap_h.energy, DynamicRates) and spot is None:
            continue
        try:
            bd = compute_breakdown(snap_h, dso, region, local, spot, meter, dso_mode)
        except (KeyError, ValueError):
            # Missing DSO row for an archived month or non-static rate
            # kind in the static path; skip the hour rather than
            # tearing the whole backfill down.
            continue

        for key, sid in stat_ids.items():
            if key == "current_price":
                value = bd.all_in
            elif key == "energy_component":
                value = bd.energy
            elif key == "network_component":
                value = bd.network
            elif key == "taxes_component":
                value = bd.taxes
            elif key == _INJECTION_PRICE_SENSOR_KEY:
                inj_rate = _historical_injection_rate(snap_h.injection, spot)
                if inj_rate is None:
                    continue
                value = inj_rate
            else:  # pragma: no cover - guarded by _PRICE_SENSOR_KEYS
                continue
            rows_per_key[key].append(
                StatisticData(start=utc_hour, mean=value, min=value, max=value)
            )

    counts: dict[str, int] = {}
    for key, sid in stat_ids.items():
        rows = rows_per_key[key]
        counts[sid] = len(rows)
        if not rows:
            continue
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.ARITHMETIC,
            has_sum=False,
            name=None,
            source="recorder",
            statistic_id=sid,
            unit_class=None,
            unit_of_measurement="EUR/kWh",
        )
        async_import_statistics(hass, metadata, rows)
    return counts


async def _backfill_cost_sensor(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
    spots: dict[datetime, float],
) -> dict[str, int]:
    """Write cumulative state/sum rows for ``current_year_cost`` over ``hours``.

    Mirrors the live :func:`_compute_current_year_cost` engine but
    produces one running-total point per hour instead of one
    end-of-day number, so the recorder can render the YTD bill as a
    growing line on the Energy dashboard / Statistics card.

    Per-hour fee proration uses ``annual_for_this_month / hours_in_year``
    (vs. the live ``days_in_ytd / days_in_year`` per-day proration);
    the two converge at end-of-day, but the hourly variant gives a
    smoother in-day curve. Per-month tariff archives are honoured the
    same way as in the live path.

    Returns a per-statistic-id row count (one entry max). Skips
    silently when the sensor isn't registered (auto path firing
    before platform setup completes).
    """
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )

    # mypy --strict flags StatisticMeanType because the recorder module
    # doesn't re-export it via __all__; same shape coordinator.py uses
    # for statistics_during_period and get_instance.
    from homeassistant.components.recorder.statistics import (  # type: ignore[attr-defined]
        StatisticMeanType,
        async_import_statistics,
    )

    sid = _stat_id(hass, entry, _COST_SENSOR_KEY)
    if sid is None:
        return {}

    snap = coordinator._snapshot
    assert snap is not None
    extractor = get_extractor(entry.data[CONF_SUPPLIER])
    contract = entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data[CONF_DSO]
    meter = entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")
    is_compensation = regime == SOLAR_REGIME_COMPENSATION
    kva = _solar_kva(entry) if is_compensation else 0.0

    # One bulk fetch per recorder entity; bin into UTC-hour totals.
    # _recorder_rows treats the start/end arguments as local-day
    # boundaries; pass the local dates of the first / last UTC hour so
    # the recorder query window aligns with the backfill's _hour_iter.
    # Passing UTC dates here would shift the window by 1-2h vs local
    # midnight and either drop or double-include the end-of-range hour.
    cons_per_hour: dict[datetime, float] = {}
    inj_per_hour: dict[datetime, float] = {}
    if hours:
        start_d = dt_util.as_local(hours[0]).date()
        end_d = dt_util.as_local(hours[-1]).date()
        for cid in _hourly_consumption_sensors(entry):
            for k, v in (await _recorder_hourly_kwh(hass, cid, start_d, end_d)).items():
                cons_per_hour[k] = cons_per_hour.get(k, 0.0) + v
        for iid in _hourly_injection_sensors(entry):
            for k, v in (await _recorder_hourly_kwh(hass, iid, start_d, end_d)).items():
                inj_per_hour[k] = inj_per_hour.get(k, 0.0) + v

    month_cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in month_cache:
            month_cache[month_first] = await _snapshot_for_month(
                hass,
                coordinator._session,
                extractor,
                contract,
                region,
                month_first,
                snap,
            )
        return month_cache[month_first]

    rows: list[Any] = []
    running_energy = 0.0
    running_fees = 0.0
    for utc_hour in hours:
        local = dt_util.as_local(utc_hour)
        month_first = date(local.year, local.month, 1)
        snap_h = await _snap_for(month_first)
        spot = spots.get(utc_hour) if spots else None

        # Energy term: skipped when the supplier is dynamic and we
        # have no spot for this hour (formula factor*spot+base needs
        # both), or when compute_breakdown can't evaluate the hour.
        if not (isinstance(snap_h.energy, DynamicRates) and spot is None):
            try:
                bd = compute_breakdown(
                    snap_h, dso, region, local, spot, meter, dso_mode
                )
            except (KeyError, ValueError):
                bd = None
            if bd is not None:
                cons = cons_per_hour.get(utc_hour, 0.0)
                inj = inj_per_hour.get(utc_hour, 0.0)
                if is_compensation:
                    running_energy += (cons - inj) * bd.all_in
                elif regime == SOLAR_REGIME_INJECTION:
                    running_energy += cons * bd.all_in
                    inj_rate = _historical_injection_rate(snap_h.injection, spot)
                    if inj_rate is not None:
                        running_energy -= inj * inj_rate
                else:
                    running_energy += cons * bd.all_in

        # Fee accrual: matches the live ytd per-day fee proration when
        # summed over a full day (annual / days_in_year vs. annual /
        # hours_in_year * 24). The hourly variant keeps the YTD line
        # growing smoothly between live ticks.
        days_in_year = 366 if calendar.isleap(local.year) else 365
        annual_static = (
            getattr(snap_h.energy, "yearly_fixed_fee", 0.0)
            + snap_h.taxes.energy_fund_eur_per_month * 12.0
        )
        running_fees += annual_static / (days_in_year * 24)

        if is_compensation and kva > 0.0:
            overlay = snap_h.dsos.get(dso)
            if overlay is not None and overlay.prosumer_eur_per_kva_year is not None:
                monthly_fee = kva * overlay.prosumer_eur_per_kva_year / 12.0
                running_fees += monthly_fee / _hours_in_month(month_first)

        # Compensation regime clamps the YTD energy term at zero
        # (Walloon meter forfeits surplus injection past
        # consumption); injection / none never go negative through
        # the energy term alone.
        displayed_energy = (
            max(running_energy, 0.0) if is_compensation else running_energy
        )
        state = round(displayed_energy + running_fees, 4)
        rows.append(StatisticData(start=utc_hour, state=state, sum=state))

    if not rows:
        return {sid: 0}

    metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=sid,
        unit_class=None,
        unit_of_measurement="EUR",
    )
    async_import_statistics(hass, metadata, rows)
    return {sid: len(rows)}


async def backfill_range(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    *,
    clear: bool = False,
) -> dict[str, Any]:
    """Backfill long-term statistics for ``entry`` over ``[start, end)``.

    Always runs (even if statistics already exist in the range);
    ``async_import_statistics`` upserts on (statistic_id, start) so a
    re-run just overwrites. Pass ``clear=True`` to delete the existing
    series first when the underlying tariff or formula changed enough
    that the old rows would mislead.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if not isinstance(coordinator, BePricesCoordinator):
        raise RuntimeError("entry has no live coordinator; reload the entry first")
    if coordinator._snapshot is None:
        raise RuntimeError("supplier snapshot not loaded; refresh the entry first")

    start_utc, end_utc = _normalize_window(start, end)
    if start_utc >= end_utc:
        return {"rows_written": 0, "sensors": {}, "range": [None, None]}

    spots = await _ensure_dynamic_spots(coordinator, start_utc, end_utc)
    hours = _hour_iter(start_utc, end_utc)

    if clear:
        ids: list[str] = []
        keys = list(_PRICE_SENSOR_KEYS) + [_COST_SENSOR_KEY]
        if entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION:
            keys.append(_INJECTION_PRICE_SENSOR_KEY)
        for key in keys:
            sid = _stat_id(hass, entry, key)
            if sid is not None:
                ids.append(sid)
        if ids:
            await _clear_range(hass, ids)

    counts = await _backfill_price_sensors(hass, entry, coordinator, hours, spots)
    counts.update(await _backfill_cost_sensor(hass, entry, coordinator, hours, spots))
    total = sum(counts.values())
    _LOGGER.info(
        "backfill wrote %d statistic rows for %s over %s..%s",
        total,
        entry.entry_id,
        start_utc.isoformat(),
        end_utc.isoformat(),
    )
    return {
        "rows_written": total,
        "sensors": counts,
        "range": [start_utc.isoformat(), end_utc.isoformat()],
    }


async def backfill_if_missing(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any] | None:
    """Run :func:`backfill_range` only when no statistics exist at Jan 1.

    Probe is intentionally narrow (one hour at the year anchor) so a
    user who deletes their HA database mid-year still triggers a
    fresh backfill on next restart, while the steady-state restart
    path adds zero work.

    Tolerates entry removal mid-flight: this runs as a fire-and-forget
    background task, and the user can delete the entry between scheduling
    and execution. ``hass.config_entries.async_get_entry`` returns None
    when the entry is gone; ``runtime_data`` becomes UNDEFINED on unload.
    Bail in either case so the background task never writes statistics
    for an entry the user has removed.
    """
    if hass.config_entries.async_get_entry(entry.entry_id) is None:
        _LOGGER.debug(
            "backfill skipped: entry %s was removed before the task ran",
            entry.entry_id,
        )
        return None
    runtime = getattr(entry, "runtime_data", None)
    if not isinstance(runtime, BePricesCoordinator):
        _LOGGER.debug(
            "backfill skipped: coordinator not ready for %s",
            entry.entry_id,
        )
        return None
    sid = _stat_id(hass, entry, "current_price")
    if sid is None:
        _LOGGER.debug(
            "backfill skipped: current_price entity not registered for %s",
            entry.entry_id,
        )
        return None
    now_local = dt_util.now()
    jan1_local = now_local.replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    jan1_utc = jan1_local.astimezone(UTC)
    if await _existing_stat_window(hass, sid, jan1_utc):
        _LOGGER.debug(
            "backfill skipped: statistics already present at %s for %s",
            jan1_utc.isoformat(),
            sid,
        )
        return None
    return await backfill_range(hass, entry, jan1_local, now_local)
