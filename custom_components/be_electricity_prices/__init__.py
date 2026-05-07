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

"""Belgian Electricity Prices integration entry point."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ServiceValidationError
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from .backfill import backfill_if_missing, backfill_range
from .const import (
    CONF_CONTRACT,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    PLATFORMS,
    STORAGE_VERSION,
)
from .coordinator import BePricesCoordinator, evict_shared_caches
from .pricing import PriceBreakdown

type BePricesConfigEntry = ConfigEntry[BePricesCoordinator]

SERVICE_REFRESH = "refresh"
SERVICE_CHEAPEST_WINDOW = "cheapest_window"
SERVICE_MOST_EXPENSIVE_WINDOW = "most_expensive_window"
SERVICE_BACKFILL_STATISTICS = "backfill_statistics"

WINDOW_SCHEMA = vol.Schema(
    {
        # Whole hours only -- the price table is hourly. services.yaml
        # advertises step=1, min=1, and _resolve_window_inputs rejects
        # anything < 1 at runtime; keep the voluptuous bounds in sync
        # so a YAML-only call hits the same rule.
        vol.Required("duration_hours"): vol.All(
            vol.Coerce(float), vol.Range(min=1.0, max=48.0)
        ),
        vol.Optional("entry_id"): cv.string,
        vol.Optional("earliest_start"): cv.datetime,
        vol.Optional("latest_end"): cv.datetime,
    }
)

BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
        vol.Optional("clear", default=False): cv.boolean,
    }
)


CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:  # noqa: ARG001 - HA hook signature
    """Register the integration's services once at startup.

    Service handlers are domain-scoped, not entry-scoped, so they live
    here rather than in ``async_setup_entry``. Registering once
    eliminates the deregister-then-reregister window when the user
    reloads the only config entry: a ``be_electricity_prices.refresh``
    automation firing in that window used to fail with "service not
    found" until setup completed again.
    """
    hass.services.async_register(DOMAIN, SERVICE_REFRESH, _async_refresh_service)
    hass.services.async_register(
        DOMAIN,
        SERVICE_CHEAPEST_WINDOW,
        _async_cheapest_window_service,
        schema=WINDOW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MOST_EXPENSIVE_WINDOW,
        _async_most_expensive_window_service,
        schema=WINDOW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_STATISTICS,
        _async_backfill_service,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Set up one config entry."""
    coordinator = BePricesCoordinator(hass, entry)
    await coordinator.async_load_persistent()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # One-shot backfill: only fires when the recorder has no
    # statistics for current_price at the Jan 1 anchor, so a normal
    # restart adds zero work. Runs in a background task because the
    # ENTSO-E historical fetch can take tens of seconds for a fresh
    # install on a dynamic supplier and must not block setup.
    entry.async_create_background_task(
        hass,
        backfill_if_missing(hass, entry),
        f"{DOMAIN}_backfill_{entry.entry_id}",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Unload one config entry."""
    # Snapshot the supplier tuple from the coordinator BEFORE
    # async_unload_platforms tears it down. The coordinator stores
    # the tuple it was constructed with so an OptionsFlow edit that
    # mutated entry.data prior to reload can still evict the
    # *previous* tuple's cache rows.
    # entry.runtime_data may be HA's UNDEFINED sentinel rather than None
    # if async_setup_entry raised before the explicit assignment (e.g.
    # async_config_entry_first_refresh raised ConfigEntryNotReady). The
    # `is not None` test would still pass, then access to ._supplier_tuple
    # raises AttributeError and masks the real setup failure.
    coordinator = getattr(entry, "runtime_data", None)
    if not isinstance(coordinator, BePricesCoordinator):
        coordinator = None
    cached_key = coordinator._supplier_tuple if coordinator is not None else None
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Drop any shared-cache rows pinned to this entry's previous
        # (supplier, contract, region) tuple, but only when no other
        # loaded entry still references the same tuple. Without this
        # the snapshot, per-month archive, failed-fetch marker, and
        # asyncio.Lock leak into hass.data for the rest of the HA
        # process lifetime.
        if cached_key and all(cached_key):
            siblings = [
                other
                for other in hass.config_entries.async_loaded_entries(DOMAIN)
                if other.entry_id != entry.entry_id
                and (
                    other.data.get(CONF_SUPPLIER),
                    other.data.get(CONF_CONTRACT),
                    other.data.get(CONF_REGION),
                )
                == cached_key
            ]
            if not siblings:
                evict_shared_caches(hass, cached_key, cached_key[0])
    # Services live for the integration's lifetime (registered in
    # async_setup), so they're not torn down here. The previous
    # per-entry registration window briefly returned "service not
    # found" to in-flight automations during a single-entry reload.
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> None:
    """Drop per-entry state when the user removes the entry.

    The 'snapshot stale' issue id embeds the entry id; once the entry
    is gone the coordinator can't auto-resolve it, so it would linger
    in the Repairs panel forever. HA calls this hook after
    ``async_unload_entry`` for a removal (not for a reload). The
    persistent snapshot Store is also deleted so the JSON blob the
    coordinator writes under ``.storage/`` doesn't outlive the entry.
    """
    for issue_kind in ("snapshot_stale", "extractor_failed", "entsoe_auth_failed"):
        issue_registry.async_delete_issue(
            hass, DOMAIN, f"{issue_kind}_{entry.entry_id}"
        )
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}_cache_{entry.entry_id}"
    )
    await store.async_remove()


async def _async_options_updated(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    """Reload the entry only when entry.data actually changed.

    HA fires this listener whenever ``entry.options`` (or ``entry.data``)
    changes via ``async_update_entry``. The OptionsFlow's no-op
    ``async_create_entry(data={})`` finalize writes ``options = {}``;
    if the entry pre-existed with non-empty options (a hold-over from
    an older HA version's options-flow contract), HA sees options
    `{...} -> {}` as a real change and would otherwise trigger a
    needless reload that tears down the warmed snapshot. The
    integration carries every load-bearing field in ``entry.data``,
    not in ``entry.options``, so an options-only change can skip the
    reload safely.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if isinstance(coordinator, BePricesCoordinator):
        live_data = coordinator._entry_data_signature
        if BePricesCoordinator._compute_data_signature(entry) == live_data:
            return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_refresh_service(call: ServiceCall) -> None:
    """Force every loaded entry to re-fetch its supplier snapshot now."""
    for entry in call.hass.config_entries.async_loaded_entries(DOMAIN):
        # async_loaded_entries returns entries that have begun setup, but
        # a reload race can leave runtime_data as the UNDEFINED sentinel
        # between platform unload and the new coordinator assignment.
        # Skip those rather than raising AttributeError mid-iteration.
        coordinator = getattr(entry, "runtime_data", None)
        if not isinstance(coordinator, BePricesCoordinator):
            continue
        await coordinator.async_force_refresh()


def _find_window(
    hourly: dict[datetime, PriceBreakdown],
    duration_slots: int,
    earliest_utc: datetime,
    latest_utc: datetime | None,
    *,
    minimize: bool,
) -> dict[str, Any]:
    """Pure helper: locate the cheapest (minimize=True) or most-expensive
    contiguous ``duration_slots``-long window in the supplied hourly table.

    Bounds:
      earliest_utc   only hours on/after this UTC time are considered
                     (the hour bucket is found by truncating to :00).
      latest_utc     if set, only hours whose end (h + 1h) is on/before
                     this UTC time are considered.

    Raises ``ValueError`` when fewer than ``duration_slots`` hours match.
    """
    hours = sorted(hourly.items())
    earliest_anchor = earliest_utc.replace(minute=0, second=0, microsecond=0)
    candidates = [(h, bd) for h, bd in hours if h >= earliest_anchor]
    if latest_utc is not None:
        candidates = [
            (h, bd) for h, bd in candidates if h + timedelta(hours=1) <= latest_utc
        ]
    if len(candidates) < duration_slots:
        raise ServiceValidationError(
            f"only {len(candidates)} hours available in the requested window; "
            f"need {duration_slots}"
        )

    best_idx = 0
    best_avg = float("inf") if minimize else float("-inf")
    for i in range(len(candidates) - duration_slots + 1):
        avg = (
            sum(bd.all_in for _, bd in candidates[i : i + duration_slots])
            / duration_slots
        )
        if (minimize and avg < best_avg) or (not minimize and avg > best_avg):
            best_avg = avg
            best_idx = i

    win_start_utc = candidates[best_idx][0]
    win_end_utc = candidates[best_idx + duration_slots - 1][0] + timedelta(hours=1)
    return {
        "start": dt_util.as_local(win_start_utc).isoformat(),
        "end": dt_util.as_local(win_end_utc).isoformat(),
        "duration_hours": duration_slots,
        "average_eur_per_kwh": round(best_avg, 6),
        "hours": [
            {
                "hour": dt_util.as_local(h).isoformat(),
                "all_in": round(bd.all_in, 6),
            }
            for h, bd in candidates[best_idx : best_idx + duration_slots]
        ],
    }


def _resolve_window_inputs(
    call: ServiceCall,
) -> tuple[dict[datetime, PriceBreakdown], int, datetime, datetime | None]:
    """Parse a window-finding ServiceCall into pure-helper arguments."""
    duration_hours = float(call.data["duration_hours"])
    if duration_hours < 1:
        raise ServiceValidationError(
            "duration_hours must be at least 1 (price table is hourly)"
        )
    # The price table is hourly; round half-up so 1.5h becomes 2h windows
    # rather than silently widening to 1h. The service schema now exposes
    # whole-hour steps so callers shouldn't trip this branch from the UI.
    duration_slots = int(duration_hours + 0.5)
    if duration_slots < 1:
        duration_slots = 1

    entries = call.hass.config_entries.async_loaded_entries(DOMAIN)
    target_id = call.data.get("entry_id")
    if target_id is not None:
        entries = [e for e in entries if e.entry_id == target_id]
    if not entries:
        raise ServiceValidationError(
            f"no loaded {DOMAIN} entry" + (f" with id {target_id}" if target_id else "")
        )
    coordinator = getattr(entries[0], "runtime_data", None)
    if not isinstance(coordinator, BePricesCoordinator):
        raise ServiceValidationError("entry is reloading; try again in a moment")
    data = coordinator.data
    if data is None or not data.hourly:
        raise ServiceValidationError("price table is empty; refresh the entry first")

    earliest = call.data.get("earliest_start") or dt_util.utcnow()
    latest = call.data.get("latest_end")
    # Naive datetimes from YAML are interpreted as the user's HA time
    # zone (typically Europe/Brussels), not the host machine's tz.
    # Otherwise a HA install on a UTC server with a Brussels user shifts
    # the requested wall-clock hour by 1-2 hours.
    earliest_utc = _to_utc(earliest)
    latest_utc = _to_utc(latest) if latest is not None else None
    return data.hourly, duration_slots, earliest_utc, latest_utc


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
    hourly, slots, earliest, latest = _resolve_window_inputs(call)
    return _find_window(hourly, slots, earliest, latest, minimize=True)


async def _async_most_expensive_window_service(call: ServiceCall) -> ServiceResponse:
    """Find the most-expensive contiguous N-hour window in the upcoming price table."""
    hourly, slots, earliest, latest = _resolve_window_inputs(call)
    return _find_window(hourly, slots, earliest, latest, minimize=False)


async def _async_backfill_service(call: ServiceCall) -> ServiceResponse:
    """Backfill long-term statistics for the targeted entry over [start, end).

    Defaults match the auto-backfill path (Jan 1 of the current local
    year through "now"); pass ``start`` / ``end`` to redo a narrower
    window after fixing a tariff card. ``clear=True`` deletes the
    target series first; without it, ``async_import_statistics``
    upserts on (statistic_id, start) so a re-run silently overwrites
    the requested hours.
    """
    entries = call.hass.config_entries.async_loaded_entries(DOMAIN)
    target_id = call.data.get("entry_id")
    if target_id is not None:
        entries = [e for e in entries if e.entry_id == target_id]
    if not entries:
        raise ServiceValidationError(
            f"no loaded {DOMAIN} entry" + (f" with id {target_id}" if target_id else "")
        )
    return await backfill_range(
        call.hass,
        entries[0],
        call.data.get("start"),
        call.data.get("end"),
        clear=bool(call.data.get("clear", False)),
    )
