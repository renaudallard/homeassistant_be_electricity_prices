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
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .const import DOMAIN, PLATFORMS
from .coordinator import BePricesCoordinator
from .pricing import PriceBreakdown

type BePricesConfigEntry = ConfigEntry[BePricesCoordinator]

SERVICE_REFRESH = "refresh"
SERVICE_CHEAPEST_WINDOW = "cheapest_window"

CHEAPEST_WINDOW_SCHEMA = vol.Schema(
    {
        vol.Required("duration_hours"): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=48.0)
        ),
        vol.Optional("entry_id"): cv.string,
        vol.Optional("earliest_start"): cv.datetime,
        vol.Optional("latest_end"): cv.datetime,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Set up one config entry."""
    coordinator = BePricesCoordinator(hass, entry)
    await coordinator.async_load_persistent()
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH):
        hass.services.async_register(DOMAIN, SERVICE_REFRESH, _async_refresh_service)
    if not hass.services.has_service(DOMAIN, SERVICE_CHEAPEST_WINDOW):
        hass.services.async_register(
            DOMAIN,
            SERVICE_CHEAPEST_WINDOW,
            _async_cheapest_window_service,
            schema=CHEAPEST_WINDOW_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Unload one config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH)
        hass.services.async_remove(DOMAIN, SERVICE_CHEAPEST_WINDOW)
    return unloaded


async def _async_options_updated(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_refresh_service(call: ServiceCall) -> None:
    """Force every loaded entry to re-fetch its supplier snapshot now."""
    for entry in call.hass.config_entries.async_loaded_entries(DOMAIN):
        coordinator: BePricesCoordinator = entry.runtime_data
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
        raise ValueError(
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
    duration_slots = max(1, -(-int(duration_hours * 2 + 0.0001) // 2))  # ceil to 1h

    entries = call.hass.config_entries.async_loaded_entries(DOMAIN)
    target_id = call.data.get("entry_id")
    if target_id is not None:
        entries = [e for e in entries if e.entry_id == target_id]
    if not entries:
        raise ValueError(
            f"no loaded {DOMAIN} entry" + (f" with id {target_id}" if target_id else "")
        )
    coordinator: BePricesCoordinator = entries[0].runtime_data
    data = coordinator.data
    if data is None or not data.hourly:
        raise ValueError("price table is empty; refresh the entry first")

    earliest = call.data.get("earliest_start") or dt_util.utcnow()
    latest = call.data.get("latest_end")
    earliest_utc = (earliest if earliest.tzinfo else earliest.astimezone()).astimezone(
        dt_util.UTC
    )
    latest_utc = (
        (latest if latest.tzinfo else latest.astimezone()).astimezone(dt_util.UTC)
        if latest is not None
        else None
    )
    return data.hourly, duration_slots, earliest_utc, latest_utc


async def _async_cheapest_window_service(call: ServiceCall) -> ServiceResponse:
    """Find the cheapest contiguous N-hour window in the upcoming price table."""
    hourly, slots, earliest, latest = _resolve_window_inputs(call)
    return _find_window(hourly, slots, earliest, latest, minimize=True)
