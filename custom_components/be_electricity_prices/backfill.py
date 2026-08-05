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
from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

if TYPE_CHECKING:
    # Only for annotations: the recorder models are imported inside the
    # functions that use them, so the module still loads without a recorder.
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
from homeassistant.exceptions import ServiceValidationError
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
    REGION_FLANDERS,
    REGION_WALLONIA,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .coordinator import (
    BePricesCoordinator,
    _annual_static_fees,
    _cohort_energy_leg,
    _contract_start_month,
    _historical_injection_rate,
    _capacity_monthly_eur,
    _hourly_consumption_sensors,
    _hourly_injection_sensors,
    _injection_hourly_on_cohort,
    _injection_needs_spot,
    _partial_register_pair,
    _mean_of_month,
    _month_snapshot_cache,
    _prosumer_monthly_fee,
    _spp_injection_spot,
    _spp_weighting_enabled,
    _sum_hourly_kwh,
)
from .pricing import compute_breakdown
from .providers import DynamicRates, SpotMonthlyRates, get as get_extractor

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
    # Clamp the end to the current hour. compute_breakdown happily evaluates a
    # future hour for a fixed / variable / TOU / Impact contract, so an end
    # date past now (a mistyped year on the backfill_statistics service, whose
    # schema has no upper bound) wrote a full year of phantom price rows and
    # kept the cost sensor's fee, capacity and prosumer accrual running into
    # hours that have not happened. The None default already stopped at now;
    # an explicit end now gets the same bound.
    end_utc = min(_floor_to_hour_utc(end_local), _floor_to_hour_utc(now_local))
    return start_utc, end_utc


async def _existing_stat_window(
    hass: HomeAssistant, statistic_id: str, anchor: datetime
) -> bool:
    """Return True when at least one statistic row exists in a short
    window from ``anchor``.

    Used by :func:`backfill_if_missing` to derive the "is the recorder
    already populated" signal directly from the recorder, so we never
    need to persist a separate "backfill done" flag that would go
    stale across DB resets or supplier changes.

    Probes a 2-day window rather than the single anchor hour: a dynamic
    contract whose Jan 1 00:00 spot is genuinely missing skips that hour
    during backfill, so a single-hour probe would read empty and re-run
    the whole-year backfill on every restart. A short window still reads
    empty after a real DB reset (self-healing preserved) but tolerates a
    legitimately-absent leading hour.
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
            anchor + timedelta(days=2),
            {statistic_id},
            "hour",
            None,
            {"mean"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return False
    return bool(rows.get(statistic_id))


async def _clear_all(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Delete every statistic row for ``statistic_ids`` -- the WHOLE series.

    The recorder's ``clear_statistics`` is the only public primitive
    here and it is series-scoped, not range-scoped. Callers must
    therefore restrict the use of ``clear=True`` to full-year re-runs;
    a narrower window with ``clear=True`` would wipe rows OUTSIDE the
    requested range and leave them gone. The user-facing service
    description in services.yaml + every locale's strings warn about
    this destructive scope.
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
    coordinator: BePricesCoordinator,
    entry: ConfigEntry,
    start: datetime,
    end: datetime,
) -> dict[datetime, float]:
    """Make sure ``coordinator._historical_spots`` covers [start, end] for a
    dynamic supplier, then return the spot dict.

    Reuses the coordinator's existing ENTSO-E backfill helper so the
    bulk-fetch logic (week-sized chunks, partial-day tolerance, negative
    cache) stays in one place. Returns an empty dict when no spot is
    needed (static energy with a monthly or no injection); callers should
    not look up spots in that case. A static-energy contract whose
    injection is itself spot-indexed (Cociter Variable) still needs spots
    so its feed-in credit lands in the backfilled cost and price rows,
    matching the live coordinator's gate; otherwise the backfill would
    drop that credit and leave a sum-chain step at the backfill->live
    seam.
    """
    snap = coordinator._snapshot
    if snap is None:
        return {}
    # A variable contract with a contract start date re-prices to a
    # SpotMonthlyRates cohort, which needs spots for its monthly mean just like
    # a dynamic contract. Resolve the effective (cohort) energy only when a
    # start date is set (the common path never fetches), so the backfill fetches
    # spots for the cohort too, matching the live coordinator (which gates the
    # historical-spot fetch on ``priced.energy``); otherwise the cohort hours
    # get no spot and are dropped, leaving a fees-only backfill.
    eff_energy = snap.energy
    if _contract_start_month(entry) is not None:
        cohort = await _cohort_energy_leg(
            coordinator.hass,
            coordinator._session,
            get_extractor(entry.data[CONF_SUPPLIER]),
            entry.data[CONF_CONTRACT],
            entry.data.get(CONF_REGION, ""),
            entry,
            snap,
        )
        if cohort is not None:
            eff_energy = cohort
    if not isinstance(
        eff_energy, (DynamicRates, SpotMonthlyRates)
    ) and not _injection_needs_spot(snap, entry):
        return {}
    # _ensure_historical_spots anchors each fetched day on LOCAL midnight,
    # so feed it LOCAL dates: passing the UTC date of end (which lands on
    # the previous local day when the backfill runs in the 00:00-01:59
    # local window) would leave the final UTC hour _hour_iter requests
    # unfetched, re-introducing a one-hour sum step at the seam. Matches
    # the live coordinator, which fetches through dt_util.now().date().
    await coordinator._ensure_historical_spots(
        dt_util.as_local(start).date(), dt_util.as_local(end).date()
    )
    return coordinator._historical_spots


def _hour_spot(
    energy: Any,
    local: datetime,
    utc_hour: datetime,
    spots: dict[datetime, float],
    mean_cache: dict[tuple[int, int], float | None],
) -> float | None:
    """The spot value to price ``energy`` at for one hour.

    A ``SpotMonthlyRates`` leg (a variable contract re-priced at its signing
    cohort's coefficients) bills the delivery month's arithmetic mean, matching
    the live price table (``_build_hourly``) and the YTD walk
    (``_ytd_hourly_energy`` with ``monthly_mean=True``); every other kind uses
    the per-hour spot. The month mean is memoised so a 365-day window computes
    at most 12 means.
    """
    if isinstance(energy, SpotMonthlyRates):
        key = (local.year, local.month)
        if key not in mean_cache:
            mean_cache[key] = _mean_of_month(spots, *key) if spots else None
        return mean_cache[key]
    return spots.get(utc_hour) if spots else None


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
    _snap_for = _month_snapshot_cache(
        hass, coordinator._session, extractor, contract, region, snap, entry
    )

    # Custom monthly entries that opted into SPP-weighted injection price
    # the mean-indexed credit off the Synergrid solar profile; mirror the
    # live YTD credit so the backfilled injection_price meets it at the seam.
    spp_weights = None
    if _spp_weighting_enabled(entry):
        await coordinator._ensure_spp_weights()
        spp_weights = coordinator._spp_weights
    month_spp_cache: dict[tuple[int, int], float | None] = {}

    rows_per_key: dict[str, list[Any]] = {key: [] for key in stat_ids}
    month_mean_cache: dict[tuple[int, int], float | None] = {}
    # A card whose injection is a per-hour spot formula with no printed
    # indicative (Cociter Tarif Variable) keeps that hourly index even when a
    # signing cohort re-prices its ENERGY leg to a monthly mean. Same gate the
    # live tick and the YTD walk apply.
    hourly_injection = _injection_hourly_on_cohort(snap, entry)
    for utc_hour in hours:
        local = dt_util.as_local(utc_hour)
        snap_h = await _snap_for(date(local.year, local.month, 1))
        spot = _hour_spot(snap_h.energy, local, utc_hour, spots, month_mean_cache)
        # Dynamic / spot-monthly without a spot for this hour: nothing to
        # write, the formula factor*spot+base (or factor*mean+base) needs both.
        # Fixed / variable pass spot=None and ignore it in compute_breakdown.
        if isinstance(snap_h.energy, (DynamicRates, SpotMonthlyRates)) and spot is None:
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
                inj_spot = _spp_injection_spot(
                    spot,
                    monthly_mean=isinstance(snap_h.energy, SpotMonthlyRates),
                    spp_weights=spp_weights,
                    historical_spots=spots,
                    year=local.year,
                    month=local.month,
                    cache=month_spp_cache,
                    hourly=hourly_injection,
                    hourly_spot=spots.get(utc_hour),
                )
                inj_rate = _historical_injection_rate(
                    snap_h.injection, inj_spot, energy=snap_h.energy, when=local
                )
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
    emit_from: datetime | None = None,
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

    ``current_year_cost`` is a cumulative ``TOTAL`` sensor that resets on
    Jan 1. ``hours`` MUST stay within a single calendar year, anchored at
    that year's Jan 1: the loop accumulates monotonically from the first
    hour and (when ``emit_from`` is set) only writes rows on/after it, so
    a mid-year backfill still carries the correct year-to-date sum. The
    sum must not be reset mid-series -- the recorder derives the Energy
    dashboard's change as ``sum - prev_sum`` and ignores ``last_reset``
    for imported statistics, so a drop back to ~0 would render as a large
    spurious negative cost. The caller therefore anchors on Jan 1 of the
    *end* year and never spans a year boundary here.

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
    # The kW the Flemish capacity tariff is charged on. Read from the live
    # coordinator so the backfilled series accrues it exactly as the live
    # _ytd_capacity does; the rolling mean is not reconstructable per past
    # month, so both use the current one (see _ytd_capacity).
    billed_peak_kw = coordinator._billed_peak_kw() if region == REGION_FLANDERS else 0.0

    # One bulk fetch per recorder entity; bin into UTC-hour totals.
    # _recorder_rows treats the start/end arguments as local-day
    # boundaries; pass the local dates of the first / last UTC hour so
    # the recorder query window aligns with the backfill's _hour_iter.
    # Passing UTC dates here would shift the window by 1-2h vs local
    # midnight and either drop or double-include the end-of-range hour.
    cons_per_hour: dict[datetime, float] = {}
    inj_per_hour: dict[datetime, float] = {}
    # Mirror the live paths: a half-wired day/night pair cannot be billed, so
    # accrue fees only rather than bill the wired half and credit injection
    # against a consumption side that silently resolved to nothing.
    half_wired = _partial_register_pair(entry, "consumption") or (
        _partial_register_pair(entry, "injection")
    )
    if hours and not half_wired:
        start_d = dt_util.as_local(hours[0]).date()
        end_d = dt_util.as_local(hours[-1]).date()
        cons_per_hour = await _sum_hourly_kwh(
            hass, _hourly_consumption_sensors(entry), start_d, end_d
        )
        inj_per_hour = await _sum_hourly_kwh(
            hass, _hourly_injection_sensors(entry), start_d, end_d
        )

    _snap_for = _month_snapshot_cache(
        hass, coordinator._session, extractor, contract, region, snap, entry
    )

    # UTC-hour count per local day so the static fee accrues smoothly per
    # hour yet each local day sums to exactly annual/days_in_year, even on
    # the DST seam days (23 or 25 UTC hours).
    hours_per_local_date: dict[date, int] = {}
    for h in hours:
        d = dt_util.as_local(h).date()
        hours_per_local_date[d] = hours_per_local_date.get(d, 0) + 1

    # SPP-weighted injection for a custom monthly entry that opted in;
    # mirrors the live YTD credit so the backfilled cost meets it at the seam.
    spp_weights = None
    if _spp_weighting_enabled(entry):
        await coordinator._ensure_spp_weights()
        spp_weights = coordinator._spp_weights
    month_spp_cache: dict[tuple[int, int], float | None] = {}

    rows: list[Any] = []
    running_energy = 0.0
    running_fees = 0.0
    month_mean_cache: dict[tuple[int, int], float | None] = {}
    # Same per-hour-injection gate as the price-sensor pass above.
    hourly_injection = _injection_hourly_on_cohort(snap, entry)
    for utc_hour in hours:
        local = dt_util.as_local(utc_hour)
        month_first = date(local.year, local.month, 1)
        snap_h = await _snap_for(month_first)
        spot = _hour_spot(snap_h.energy, local, utc_hour, spots, month_mean_cache)

        # Energy term: skipped when the supplier is dynamic / spot-monthly and
        # we have no spot (or month mean) for this hour (the formula needs it),
        # or when compute_breakdown can't evaluate the hour.
        if not (
            isinstance(snap_h.energy, (DynamicRates, SpotMonthlyRates)) and spot is None
        ):
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
                    inj_spot = _spp_injection_spot(
                        spot,
                        monthly_mean=isinstance(snap_h.energy, SpotMonthlyRates),
                        spp_weights=spp_weights,
                        historical_spots=spots,
                        year=local.year,
                        month=local.month,
                        cache=month_spp_cache,
                        hourly=hourly_injection,
                        hourly_spot=spots.get(utc_hour),
                    )
                    inj_rate = _historical_injection_rate(
                        snap_h.injection, inj_spot, energy=snap_h.energy, when=local
                    )
                    if inj_rate is not None:
                        running_energy -= inj * inj_rate
                else:
                    running_energy += cons * bd.all_in

        # Fee accrual: spread each local day's annual/days_in_year share
        # evenly over that day's actual UTC hours, so the YTD line grows
        # smoothly yet every day (including the 23/25-hour DST seam days)
        # totals exactly annual/days_in_year, matching the live YTD per-day
        # proration (annual * days_in_ytd / days_in_year). A flat
        # annual/(days_in_year*24) rate accrued 23 or 25 hours' worth on
        # the seam days, drifting from the live sensor at the seam.
        days_in_year = 366 if calendar.isleap(local.year) else 365
        annual_static = _annual_static_fees(snap_h, meter, entry)
        running_fees += (
            annual_static / days_in_year / hours_per_local_date[local.date()]
        )

        # Flemish capacity tariff, spread per local day like the prosumer fee
        # below (its monthly charge over that month's days), so the backfill
        # meets the live _ytd_capacity proration (days_in_ytd /
        # days_in_full_month) at the seam rather than trailing it.
        if billed_peak_kw:
            monthly = _capacity_monthly_eur(snap_h.dsos.get(dso), billed_peak_kw)
            if monthly:
                days_in_full_month = calendar.monthrange(
                    month_first.year, month_first.month
                )[1]
                running_fees += (
                    monthly / days_in_full_month / hours_per_local_date[local.date()]
                )

        # Compensation is Walloon-only (see coordinator._compute_prosumer):
        # gate the prosumer accrual to Wallonia so a Flanders entry never
        # backfills prosumer on top of the capacity tariff.
        if is_compensation and kva > 0.0 and region == REGION_WALLONIA:
            overlay = snap_h.dsos.get(dso)
            monthly_fee = _prosumer_monthly_fee(overlay, snap_h, kva)
            if monthly_fee:
                # Prorate the monthly prosumer fee per local day, the same way
                # the static fee above is spread, so both reach a full daily
                # share on the current in-progress day and the backfill meets
                # the live _ytd_prosumer (days_in_ytd / days_in_full_month)
                # proration at the seam instead of trailing it by a partial
                # day. Dividing by that day's actual UTC-hour count makes each
                # day (including the 23/25-hour DST seam days) sum to exactly
                # monthly_fee / days_in_full_month.
                days_in_full_month = calendar.monthrange(
                    month_first.year, month_first.month
                )[1]
                running_fees += (
                    monthly_fee
                    / days_in_full_month
                    / hours_per_local_date[local.date()]
                )

        # Compensation regime clamps the YTD energy term at zero
        # (Walloon meter forfeits surplus injection past
        # consumption); injection / none never go negative through
        # the energy term alone.
        displayed_energy = (
            max(running_energy, 0.0) if is_compensation else running_energy
        )
        state = round(displayed_energy + running_fees, 4)
        # Accumulate from Jan 1 (the caller anchors ``hours`` there) but
        # only emit rows inside the requested window, so a mid-year
        # ``start`` still carries the correct year-to-date sum instead of
        # restarting from zero and clashing with the pre-existing series.
        if emit_from is None or utc_hour >= emit_from:
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
    _seed_short_term_sum(hass, metadata, rows[-1])
    return {sid: len(rows)}


def _seed_short_term_sum(
    hass: HomeAssistant, metadata: StatisticMetaData, last: StatisticData
) -> None:
    """Continue the imported ``sum`` chain into the live one.

    The cost sensor is ``state_class: TOTAL``, so the recorder's own sensor
    platform compiles statistics for the same id we import into, and it seeds
    its running sum from ``statistics_short_term`` alone
    (``sensor/recorder.py``: ``_sum = last_stat.get("sum") or 0.0``).
    ``async_import_statistics`` writes only the long-term table, so without
    this the live chain restarts at zero directly after a backfilled row
    carrying the whole year: the first compiled hour then reports
    ``change = 0 - <year to date>``, and the Energy dashboard's Cost card
    shows roughly minus one annual bill for that day.

    Writing one short-term row at the last backfilled instant hands the
    platform the running total to resume from. It has to carry ``last_reset``
    as well as ``state`` and ``sum``: the compiler reads all three off that
    row, and a row without one looks like a fresh cycle against the sensor's
    own Jan-1 ``last_reset``, which takes the meter-reset branch and adds the
    whole live reading on top of the resumed sum instead of the delta. Match
    the sensor's ``last_reset_fn`` exactly (local Jan 1 of the current year).

    Best effort: a recorder that refuses the write leaves the seam, which is
    no worse than not trying, so it must never take the backfill down with it.
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.db_schema import StatisticsShortTerm
    except ImportError:  # pragma: no cover - recorder always ships with HA
        return
    seed: StatisticData = {
        **last,
        "last_reset": dt_util.now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        ),
    }
    try:
        get_instance(hass).async_import_statistics(
            metadata, [seed], StatisticsShortTerm
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        _LOGGER.debug(
            "could not seed the short-term sum for %s; the first compiled "
            "hour will show a one-off negative change",
            metadata["statistic_id"],
        )


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

    # The cost sensor is a cumulative TOTAL that resets each Jan 1, and
    # the recorder renders the Energy dashboard's cost change as
    # (sum - prev_sum), ignoring last_reset for imported stats. So the
    # cost series must stay within ONE calendar year: anchor it on Jan 1
    # of the END year and accumulate forward from there. A mid-year start
    # in the same year still gets the correct YTD because we accumulate
    # from Jan 1 and only emit from the requested start; a multi-year
    # request simply backfills the current (end) year's cost, never
    # crossing a boundary that would drop the sum to ~0 and paint a
    # spurious negative cost. The price (mean) sensors are unaffected by
    # this and keep the full requested window.
    # Anchor on the LAST hour actually backfilled, not on ``end_utc``, which
    # is exclusive. services.yaml documents ``end`` as "first hour NOT to
    # backfill", so the canonical way to rebuild a whole year is
    # start = 1 Jan YYYY, end = 1 Jan YYYY+1 -- and taking the year off that
    # end lands on the NEXT year's anchor, which equals end_utc itself. The
    # cost window was then empty and the service reported success having
    # written 8760 price rows and zero cost rows.
    cost_anchor_utc = _floor_to_hour_utc(
        dt_util.as_local(end_utc - timedelta(hours=1)).replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    )
    # A window that ends on or before 1 January of the CURRENT year rebuilds a
    # past year's cost, and that year's series would sit immediately before the
    # current year's in the same statistic id. The recorder renders change as
    # (sum - prev_sum) and ignores last_reset on imported rows even when it is
    # set (measured: a boundary row carrying the new year's last_reset still
    # reported change = -1197), so the join would paint roughly minus one
    # annual bill onto the Energy dashboard's Cost card at 1 January.
    #
    # There is no representation that avoids it while the cost sum restarts at
    # each 1 January, so skip the cost leg rather than corrupt the card. The
    # price series carry no sum, cross no boundary, and are still rebuilt over
    # the whole requested window, which is most of what a past-year request is
    # for. Report the skip: silently writing zero cost rows here is the bug
    # this window used to have.
    this_year_anchor_utc = _floor_to_hour_utc(
        dt_util.now().replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    )
    skip_cost = end_utc <= this_year_anchor_utc
    if skip_cost:
        _LOGGER.warning(
            "backfill for %s covers %s..%s, which ends on or before 1 January of "
            "the current year: rebuilding the cost sensor there would paint a "
            "large negative cost onto the Energy dashboard at the year boundary, "
            "so only the price sensors were rebuilt",
            entry.entry_id,
            start_utc.isoformat(),
            end_utc.isoformat(),
        )
    if clear and not skip_cost and start_utc > cost_anchor_utc:
        # clear=True wipes the WHOLE series (clear_statistics is
        # series-scoped), but a sub-year window only repopulates
        # [start, end]; everything outside it -- including the
        # Jan 1..start head of the current year -- would be gone for
        # good. Refuse the narrow-window + clear combination so the
        # destructive wipe can only run when the re-import covers the
        # cleared rows (start on or before the year anchor).
        #
        # Only when the cost leg is in play. A window ending in a finished
        # year leaves the cost series out of both the wipe and the re-import,
        # so nothing destructive is left to guard: refusing there denied the
        # price rebuild the user asked for, with a message that was false for
        # exactly that window (a start in the year BEFORE the one they typed
        # as `end` is not "after 1 January of the end year", and the remedy it
        # suggests is already satisfied).
        raise ServiceValidationError(
            "clear=True deletes the entire statistics series, but this "
            "window starts after 1 January of the end year, so the cleared "
            "rows before the start would not be re-imported. Re-run with a "
            "window starting on or before 1 January, or leave clear off (a "
            "re-import already overwrites the requested hours)."
        )
    # Fetch spots over the union of the price window and the cost window
    # so the dynamic price rows AND the cost sensor's pre-start
    # accumulation both have spots (a no-op for non-dynamic suppliers).
    spots = await _ensure_dynamic_spots(
        coordinator, entry, min(start_utc, cost_anchor_utc), end_utc
    )
    hours = _hour_iter(start_utc, end_utc)
    cost_hours = _hour_iter(cost_anchor_utc, end_utc)
    cost_emit_from = max(start_utc, cost_anchor_utc)

    if clear:
        ids: list[str] = []
        keys = list(_PRICE_SENSOR_KEYS)
        # The price series are re-imported over the WHOLE requested window, so
        # wiping them is always matched by the re-import. The cost series is
        # not: it is deliberately re-imported only over the end year
        # (cost_hours above), while _clear_all is series-scoped and deletes
        # every row it has. On a window that reaches back past 1 January of the
        # end year that combination permanently destroyed prior years' cost
        # history. Only wipe it when the request IS exactly the end year, which
        # (given the guard above rejects a later start) means start == anchor.
        # Skipping the wipe is safe: async_import_statistics upserts on
        # (statistic_id, start), so the re-imported year still lands.
        if start_utc == cost_anchor_utc and not skip_cost:
            keys.append(_COST_SENSOR_KEY)
        if entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION:
            keys.append(_INJECTION_PRICE_SENSOR_KEY)
        for key in keys:
            sid = _stat_id(hass, entry, key)
            if sid is not None:
                ids.append(sid)
        if ids:
            await _clear_all(hass, ids)

    counts = await _backfill_price_sensors(hass, entry, coordinator, hours, spots)
    if not skip_cost:
        counts.update(
            await _backfill_cost_sensor(
                hass, entry, coordinator, cost_hours, spots, emit_from=cost_emit_from
            )
        )
    total = sum(counts.values())
    _LOGGER.info(
        "backfill wrote %d statistic rows for %s over %s..%s",
        total,
        entry.entry_id,
        start_utc.isoformat(),
        end_utc.isoformat(),
    )
    result: dict[str, Any] = {
        "rows_written": total,
        "sensors": counts,
        "range": [start_utc.isoformat(), end_utc.isoformat()],
    }
    if skip_cost:
        result["skipped"] = (
            "cost: a window ending on or before 1 January of the current year "
            "would paint a large negative cost at the year boundary, because "
            "the recorder ignores last_reset on imported statistics"
        )
    return result


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
