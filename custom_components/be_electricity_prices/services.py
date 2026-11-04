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

"""The integration's services: refreshing the cards, the cheapest and the
most expensive window of the prices ahead, and writing the year's
statistics back. ``async_setup`` registers them.
"""

from __future__ import annotations

import itertools
from datetime import datetime
from typing import TYPE_CHECKING, Any

from homeassistant.core import ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from .backfill import backfill_range
from .const import DOMAIN, RESOLUTION_HOURLY
from .coordinator import BePricesCoordinator
from .pricing import PriceBreakdown, slot_delta, slot_start, slots_per_hour

if TYPE_CHECKING:
    from . import BePricesConfigEntry


async def _async_refresh_service(call: ServiceCall) -> None:
    """Force every loaded entry, or the one ``entry_id`` names, to re-fetch
    its supplier snapshot now.

    ``clear_history`` also drops the cache of past hourly spot prices that the
    year-to-date walk replays, which nothing else can repair: a cached day that
    looks complete is never re-fetched. Off by default because refilling it
    re-fetches every day since 1 January.
    """
    clear_history = bool(call.data.get("clear_history", False))
    for entry in _target_entries(call):
        # async_loaded_entries returns entries that have begun setup, but
        # a reload race can leave runtime_data as the UNDEFINED sentinel
        # between platform unload and the new coordinator assignment.
        # Skip those rather than raising AttributeError mid-iteration.
        coordinator = getattr(entry, "runtime_data", None)
        if not isinstance(coordinator, BePricesCoordinator):
            continue
        await coordinator.async_force_refresh(clear_history=clear_history)


def _find_window(
    hourly: dict[datetime, PriceBreakdown],
    duration_slots: int,
    earliest_utc: datetime,
    latest_utc: datetime | None,
    *,
    minimize: bool,
    resolution: str = RESOLUTION_HOURLY,
) -> dict[str, Any]:
    """Pure helper: locate the cheapest (minimize=True) or most-expensive
    contiguous ``duration_slots``-long window in the supplied price table.

    ``duration_slots`` counts price slots, not hours: a 2-hour window is
    2 slots on an hourly table and 8 on a 15-minute one. ``resolution``
    sets the slot width used to anchor the earliest start and to compute
    each slot's end.

    Bounds:
      earliest_utc   only slots on/after this UTC time are considered
                     (truncated down to the slot boundary).
      latest_utc     if set, only slots whose end (slot + width) is
                     on/before this UTC time are considered.

    Raises ``ServiceValidationError`` (translation_key=``not_enough_hours``)
    when fewer than ``duration_slots`` slots match.
    """
    delta = slot_delta(resolution)
    slots = sorted(hourly.items())
    earliest_anchor = slot_start(earliest_utc, resolution)
    candidates = [(h, bd) for h, bd in slots if h >= earliest_anchor]
    if latest_utc is not None:
        candidates = [(h, bd) for h, bd in candidates if h + delta <= latest_utc]
    if len(candidates) < duration_slots:
        raise ServiceValidationError(
            f"only {len(candidates)} slots available in the requested window; "
            f"need {duration_slots}",
            translation_domain=DOMAIN,
            translation_key="not_enough_hours",
            translation_placeholders={
                "available": str(len(candidates)),
                "needed": str(duration_slots),
            },
        )

    best_idx: int | None = None
    best_avg = float("inf") if minimize else float("-inf")
    for i in range(len(candidates) - duration_slots + 1):
        # Only consider strictly time-contiguous runs: a gap in the price
        # table (an hour ENTSO-E omitted) would otherwise let the window
        # span more wall-clock than its duration and drop the gap hour
        # from the average. With sorted, slot-aligned entries, endpoints
        # exactly delta*(n-1) apart means there is no interior gap.
        if candidates[i + duration_slots - 1][0] - candidates[i][0] != delta * (
            duration_slots - 1
        ):
            continue
        avg = (
            sum(bd.all_in for _, bd in candidates[i : i + duration_slots])
            / duration_slots
        )
        if (
            best_idx is None
            or (minimize and avg < best_avg)
            or (not minimize and avg > best_avg)
        ):
            best_avg = avg
            best_idx = i

    if best_idx is None:
        # Slots exist but none form a contiguous run of the needed length
        # (a gappy dynamic table). Report the longest contiguous run.
        longest = 1
        run = 1
        for prev, cur in itertools.pairwise(candidates):
            run = run + 1 if cur[0] - prev[0] == delta else 1
            longest = max(longest, run)
        raise ServiceValidationError(
            f"only {longest} contiguous slots available in the requested "
            f"window; need {duration_slots}",
            translation_domain=DOMAIN,
            translation_key="not_enough_contiguous_hours",
            translation_placeholders={
                "available": str(longest),
                "needed": str(duration_slots),
            },
        )

    win_start_utc = candidates[best_idx][0]
    win_end_utc = candidates[best_idx + duration_slots - 1][0] + delta
    return {
        "start": dt_util.as_local(win_start_utc).isoformat(),
        "end": dt_util.as_local(win_end_utc).isoformat(),
        "duration_hours": duration_slots // slots_per_hour(resolution),
        # Each "hours" row is one price slot, which is an hour on most
        # contracts but a quarter-hour on a 15-minute (Engie Dynamic)
        # contract; expose the resolution so a consumer can tell.
        "resolution": resolution,
        "average_eur_per_kwh": round(best_avg, 6),
        "hours": [
            {
                "hour": dt_util.as_local(h).isoformat(),
                "all_in": round(bd.all_in, 6),
            }
            for h, bd in candidates[best_idx : best_idx + duration_slots]
        ],
    }


def _no_loaded_entry_error(target_id: str | None) -> ServiceValidationError:
    """Standard ServiceValidationError for a missing loaded entry.

    Two translation keys (with vs without id) so the localised message
    can phrase each variant naturally instead of injecting an empty
    string.
    """
    if target_id:
        return ServiceValidationError(
            f"no loaded {DOMAIN} entry with id {target_id}",
            translation_domain=DOMAIN,
            translation_key="no_loaded_entry_with_id",
            translation_placeholders={"entry_id": target_id},
        )
    return ServiceValidationError(
        f"no loaded {DOMAIN} entry",
        translation_domain=DOMAIN,
        translation_key="no_loaded_entry",
    )


def _target_entries(call: ServiceCall) -> list[BePricesConfigEntry]:
    """The loaded entries a service call is for: the one its ``entry_id``
    names, or all of them without one.

    An ``entry_id`` no loaded entry has is refused rather than answered with
    nothing, so a mistyped id is not taken for a call that did its work.
    """
    entries = call.hass.config_entries.async_loaded_entries(DOMAIN)
    target_id = call.data.get("entry_id")
    if target_id is None:
        return entries
    entries = [e for e in entries if e.entry_id == target_id]
    if not entries:
        raise _no_loaded_entry_error(target_id)
    return entries


def _resolve_target_coordinator(call: ServiceCall) -> BePricesCoordinator:
    """Resolve the target entry's live coordinator for a service call.

    Honours an optional ``entry_id`` (defaults to the sole loaded entry),
    raising a localized ServiceValidationError when no entry matches or
    the entry is mid-reload. Callers layer their own readiness checks
    (price table populated, snapshot loaded) on top.
    """
    entries = _target_entries(call)
    if not entries:
        raise _no_loaded_entry_error(None)
    coordinator = getattr(entries[0], "runtime_data", None)
    if not isinstance(coordinator, BePricesCoordinator):
        raise ServiceValidationError(
            "entry is reloading; try again in a moment",
            translation_domain=DOMAIN,
            translation_key="entry_reloading",
        )
    return coordinator


def _resolve_window_inputs(
    call: ServiceCall,
) -> tuple[dict[datetime, PriceBreakdown], int, datetime, datetime | None, str]:
    """Parse a window-finding ServiceCall into pure-helper arguments."""
    duration_hours = float(call.data["duration_hours"])
    if duration_hours < 1:
        raise ServiceValidationError(
            "duration_hours must be at least 1",
            translation_domain=DOMAIN,
            translation_key="duration_too_small",
        )

    coordinator = _resolve_target_coordinator(call)
    data = coordinator.data
    if data is None or not data.hourly:
        raise ServiceValidationError(
            "price table is empty; refresh the entry first",
            translation_domain=DOMAIN,
            translation_key="price_table_empty",
        )

    # Round the requested hours half-up, then scale to the table's slot
    # grid: a 2-hour window is 2 slots hourly, 8 slots on a 15-minute
    # contract, which lets the window start on any quarter boundary.
    duration_slots = int(duration_hours + 0.5) * slots_per_hour(data.resolution)

    earliest = call.data.get("earliest_start") or dt_util.utcnow()
    latest = call.data.get("latest_end")
    # Naive datetimes from YAML are interpreted as the user's HA time
    # zone (typically Europe/Brussels), not the host machine's tz.
    # Otherwise a HA install on a UTC server with a Brussels user shifts
    # the requested wall-clock hour by 1-2 hours.
    earliest_utc = _to_utc(earliest)
    latest_utc = _to_utc(latest) if latest is not None else None
    return data.hourly, duration_slots, earliest_utc, latest_utc, data.resolution


def _to_utc(value: datetime) -> datetime:
    """Coerce a service-call datetime to UTC, treating naive as HA tz.

    Naive inputs land on the HA-configured timezone. On DST seam days
    a non-existent local hour (Brussels 02:30 on the spring-forward
    Sunday) defers to ``zoneinfo``'s ``fold=0`` interpretation, which
    aligns with HA's convention everywhere else (recorder, automations,
    the price table). Ambiguous fall-back hours likewise pick the
    first occurrence. Document the choice rather than try to detect
    invalid wall-clock times - the affected window is one weekend per
    year and the price table is hourly anyway.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return value.astimezone(dt_util.UTC)


async def _async_cheapest_window_service(call: ServiceCall) -> ServiceResponse:
    """Find the cheapest contiguous N-hour window in the upcoming price table."""
    hourly, slots, earliest, latest, resolution = _resolve_window_inputs(call)
    return _find_window(
        hourly, slots, earliest, latest, minimize=True, resolution=resolution
    )


async def _async_most_expensive_window_service(call: ServiceCall) -> ServiceResponse:
    """Find the most-expensive contiguous N-hour window in the upcoming price table."""
    hourly, slots, earliest, latest, resolution = _resolve_window_inputs(call)
    return _find_window(
        hourly, slots, earliest, latest, minimize=False, resolution=resolution
    )


async def _async_backfill_service(call: ServiceCall) -> ServiceResponse:
    """Backfill long-term statistics for the targeted entry over [start, end).

    Defaults match the auto-backfill path (Jan 1 of the current local
    year through "now"); pass ``start`` / ``end`` to redo a narrower
    window after fixing a tariff card. ``clear=True`` deletes the
    target series first; without it, ``async_import_statistics``
    upserts on (statistic_id, start) so a re-run silently overwrites
    the requested hours.
    """
    # Validate the live state here so a call during a reload window or
    # before the first snapshot surfaces a localized ServiceValidationError,
    # matching the window services, rather than the raw RuntimeError that
    # backfill_range would otherwise raise.
    coordinator = _resolve_target_coordinator(call)
    if coordinator._snapshot is None:
        raise ServiceValidationError(
            "supplier snapshot not loaded; refresh the entry first",
            translation_domain=DOMAIN,
            translation_key="snapshot_not_loaded",
        )
    return await backfill_range(
        call.hass,
        coordinator.entry,
        call.data.get("start"),
        call.data.get("end"),
        clear=bool(call.data.get("clear", False)),
    )
