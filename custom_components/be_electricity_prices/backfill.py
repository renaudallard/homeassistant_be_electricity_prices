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

* :func:`backfill_range`: service-call path. Always runs over the
  requested range; with ``clear=True`` deletes the range first so a
  user who fixed their tariff card can redo a window.
* :func:`backfill_if_missing`: automatic one-shot called from
  ``async_setup_entry``. Probes the recorder for statistics at the Jan
  1 anchor and only runs when none exist, so we don't redo the work on
  every HA restart.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTRACT,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    SOLAR_REGIME_INJECTION,
)
from .cohort import (
    _cohort_energy_leg,
    ytd_window_start,
)
from .contract_periods import periods_need_spots, previous_periods
from .coordinator import BePricesCoordinator
from .coordinator_data import ytd_window_reset
from .injection import (
    _injection_needs_month_spot,
    _injection_needs_spot,
)
from .spot_stats import (
    _bucket_by_local_month,
    _energy_needs_spot,
    _hour_spot,
)
from .pricing import (
    compute_breakdown,
)
from .providers import get as get_extractor
from .backfill_window import (
    _COST_SENSOR_KEY,
    _build_context,
    _clear_all,
    _contract_segments,
    _existing_stat_window,
    _floor_to_hour_utc,
    _hour_iter,
    _in_turns,
    _normalize_window,
    _recorder_models,
    _stat_id,
)
from .backfill_cost import (
    _backfill_cost_sensor,
    _injection_rate_for_hour,
)

_LOGGER = logging.getLogger(__name__)

# Sensor description ``key`` values whose live ``native_value`` is a
# EUR/kWh price. Each one becomes one ``mean`` statistic id during
# backfill. Kept in sync by hand with sensor.py (small, stable list);
# pulling it from the SENSORS / INJECTION_SENSORS tuples would couple
# this module to the entity-construction path for no real win: the
# backfill values come straight out of compute_breakdown, not from the
# live entities.
_PRICE_SENSOR_KEYS: tuple[str, ...] = (
    "current_price",
    "energy_component",
    "network_component",
    "taxes_component",
)
_INJECTION_PRICE_SENSOR_KEY = "injection_price"


async def _ensure_dynamic_spots(
    coordinator: BePricesCoordinator,
    entry: ConfigEntry,
    start: datetime,
    end: datetime,
) -> tuple[dict[datetime, float], dict[datetime, list[float]]]:
    """Make sure ``coordinator._historical_spots`` covers [start, end] for a
    dynamic supplier, then return the hourly spots and the hour's own
    15-minute slots.

    The second dict is populated only for an entry whose feed-in formula is
    floored, whose hour its mean does not price (see
    ``_injection_needs_spot_quarters``); it is empty for everyone else. Both
    are returned together so the question "are spots wanted at all" is
    answered in exactly one place.

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
        return {}, {}
    # A variable contract with a contract start date re-prices to a
    # SpotMonthlyRates cohort, which needs spots for its monthly mean just like
    # a dynamic contract. Resolve the effective (cohort) energy here as well,
    # so the backfill fetches spots for the cohort too, matching the live
    # coordinator (which gates the historical-spot fetch on ``priced.energy``);
    # otherwise the cohort hours get no spot and are dropped, leaving a
    # fees-only backfill.
    # Resolved unconditionally rather than only for an entry with a start
    # date. Cociter Variable's month-indexed re-price fires for ANY entry
    # holding an ENTSO-E key, through _month_indexed_leg, so gating on the
    # start date left the pricing side resolving a SpotMonthlyRates leg while
    # this side had already decided no spots were needed and thrown the cache
    # away: measured, a backfilled April hour came out at 0,23003 EUR/kWh
    # instead of 0,34578, its energy term zeroed outright, and the persisted
    # year-to-date ran 36,6% low without ever self-healing.
    #
    # The common path still never fetches: with no cohort month at all, which
    # means neither a tariff card month nor a start date, _cohort_energy_leg
    # returns through _month_indexed_leg before any I/O.
    eff_energy = snap.energy
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
    # A contract the household held earlier in the year may settle on the
    # day-ahead when this one does not, and its hours are in the window too.
    today = dt_util.now().date()
    earlier = previous_periods(entry.data, ytd_window_start(entry, today), today)
    if (
        not _energy_needs_spot(eff_energy)
        and not _injection_needs_spot(snap, entry)
        and not _injection_needs_month_spot(snap, entry)
        and not periods_need_spots(earlier)
    ):
        return {}, {}
    # _ensure_historical_spots anchors each fetched day on LOCAL midnight,
    # so feed it LOCAL dates: passing the UTC date of end (which lands on
    # the previous local day when the backfill runs in the 00:00-01:59
    # local window) would leave the final UTC hour _hour_iter requests
    # unfetched, re-introducing a one-hour sum step at the seam. Matches
    # the live coordinator, which fetches through dt_util.now().date().
    await coordinator._ensure_historical_spots(
        dt_util.as_local(start).date(), dt_util.as_local(end).date()
    )
    return coordinator._historical_spots, coordinator._historical_spot_quarters


async def _backfill_price_sensors(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
    spots: dict[datetime, float],
    quarters: dict[datetime, list[float]],
) -> dict[str, int]:
    """Write ``mean`` rows for every price sensor across ``hours``.

    Returns a per-statistic-id row count for the service response so
    the caller (or a CLI user) can verify the backfill landed.
    Sensors that have no entity in the registry yet (auto path firing
    before platform setup completes) are skipped silently and reported
    with a 0 count.
    """
    (
        StatisticData,
        StatisticMetaData,
        StatisticMeanType,
        async_import_statistics,
    ) = _recorder_models()
    # Which series exist is the entry's own business: an earlier contract on
    # another regime prices its hours, it does not add or remove a sensor.
    keys = list(_PRICE_SENSOR_KEYS)
    if entry.data.get(CONF_SOLAR_REGIME, "none") == SOLAR_REGIME_INJECTION:
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

    # Bucketed once, like the live walk: the closed-month coverage gate needs
    # to know how much of a month is actually cached, not just its mean.
    month_bucket = _bucket_by_local_month(spots) if spots else {}
    today = dt_util.now().date()
    rows_per_key: dict[str, list[Any]] = {key: [] for key in stat_ids}
    # Each hour on the contract that supplied it: one piece for an entry that
    # recorded no switch, one per contract for one that did.
    for seg_entry, seg_snap, seg_hours in await _contract_segments(
        hass, entry, coordinator, hours
    ):
        ctx = await _build_context(
            hass, seg_entry, coordinator, seg_hours, snapshot=seg_snap
        )
        region = ctx.region
        dso = ctx.dso
        meter = ctx.meter
        dso_mode = ctx.dso_mode
        _snap_for = ctx.snap_for
        spp_weights = ctx.spp_weights
        month_spp_cache = ctx.month_spp_cache
        month_mean_cache = ctx.month_mean_cache
        hourly_injection = ctx.hourly_injection
        async for utc_hour in _in_turns(seg_hours):
            local = dt_util.as_local(utc_hour)
            snap_h = await _snap_for(date(local.year, local.month, 1))
            spot = _hour_spot(
                snap_h.energy,
                local,
                utc_hour,
                spots,
                month_bucket,
                month_mean_cache,
                today,
                ctx.rlp_weights,
            )
            # Dynamic / spot-monthly without a spot for this hour: nothing to
            # write, the formula factor*spot+base (or factor*mean+base) needs both.
            # Fixed / variable pass spot=None and ignore it in compute_breakdown.
            if spot is None and _energy_needs_spot(snap_h.energy):
                continue
            try:
                bd = compute_breakdown(
                    snap_h, dso, region, local, spot, meter, dso_mode
                )
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
                    inj_rate = _injection_rate_for_hour(
                        snap_h,
                        spot=spot,
                        spots=spots,
                        bucket=month_bucket,
                        quarters=quarters,
                        utc_hour=utc_hour,
                        local=local,
                        spp_weights=spp_weights,
                        month_spp_cache=month_spp_cache,
                        hourly_injection=hourly_injection,
                        today=today,
                        meter=meter,
                        region=region,
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
    # Both passes read the coordinator's own spot cache and yield to the loop
    # once a day, and every tick prunes that cache to the current year. A
    # window in a past year lost every hour the pass had not reached when a
    # tick landed, so the prune waits until the backfill is done.
    coordinator._spot_prune_holds += 1
    try:
        return await _backfill_range(hass, entry, coordinator, start, end, clear)
    finally:
        coordinator._spot_prune_holds -= 1


async def _backfill_range(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    start: datetime | date | None,
    end: datetime | date | None,
    clear: bool,
) -> dict[str, Any]:
    start_utc, end_utc = _normalize_window(start, end, ytd_window_reset(entry))
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
    # start = 1 Jan YYYY, end = 1 Jan YYYY+1, and taking the year off that
    # end lands on the NEXT year's anchor, which equals end_utc itself. The
    # cost window was then empty and the service reported success having
    # written 8760 price rows and zero cost rows.
    cost_anchor_utc = _floor_to_hour_utc(
        ytd_window_reset(entry, dt_util.as_local(end_utc - timedelta(hours=1)))
    )
    # A window that ends on or before the CURRENT accumulation window's start
    # (1 January, or the contract start date on an entry that bills from it)
    # rebuilds cost the sensor never accumulates there, and that series would
    # sit immediately before the current one in the same statistic id. The recorder renders change as
    # (sum - prev_sum) and ignores last_reset on imported rows even when it is
    # set (measured: a boundary row carrying the new year's last_reset still
    # reported change = -1197), so the join would paint roughly minus one
    # annual bill onto the Energy dashboard's Cost card at 1 January.
    #
    # There is no representation that avoids it while the cost sum restarts at
    # the window start, so skip the cost leg rather than corrupt the card. The
    # price series carry no sum, cross no boundary, and are still rebuilt over
    # the whole requested window, which is most of what a past-year request is
    # for. Report the skip: silently writing zero cost rows here is the bug
    # this window used to have.
    this_year_anchor_utc = _floor_to_hour_utc(ytd_window_reset(entry))
    skip_cost = end_utc <= this_year_anchor_utc
    if skip_cost:
        _LOGGER.warning(
            "backfill for %s covers %s..%s, which ends on or before %s, where "
            "the cost sensor starts accumulating: rebuilding the cost sensor "
            "there would paint a large negative cost onto the Energy dashboard "
            "at the boundary, so only the price sensors were rebuilt",
            entry.entry_id,
            start_utc.isoformat(),
            end_utc.isoformat(),
            this_year_anchor_utc.isoformat(),
        )
    if clear and not skip_cost and start_utc > cost_anchor_utc:
        # clear=True wipes the WHOLE series (clear_statistics is
        # series-scoped), but a sub-year window only repopulates
        # [start, end]; everything outside it, including the
        # Jan 1..start head of the current year, would be gone for
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
    spots, quarters = await _ensure_dynamic_spots(
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

    counts = await _backfill_price_sensors(
        hass, entry, coordinator, hours, spots, quarters
    )
    if not skip_cost:
        counts.update(
            await _backfill_cost_sensor(
                hass,
                entry,
                coordinator,
                cost_hours,
                spots,
                quarters,
                emit_from=cost_emit_from,
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
    if runtime._snapshot is None:
        # An entry can now be LOADED with no snapshot at all, because a
        # supplier publishing page images is not worth retrying setup over.
        # backfill_range raises for that, which is right for the service call
        # a user asked for and wrong for this fire-and-forget task: the
        # exception is never retrieved and lands in the log as a traceback on
        # every restart.
        _LOGGER.debug(
            "backfill skipped: no supplier snapshot for %s",
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
    anchor_local = ytd_window_reset(entry, now_local)
    anchor_utc = anchor_local.astimezone(UTC)
    if await _existing_stat_window(hass, sid, anchor_utc):
        _LOGGER.debug(
            "backfill skipped: statistics already present at %s for %s",
            anchor_utc.isoformat(),
            sid,
        )
        return None
    return await backfill_range(hass, entry, anchor_local, now_local)
