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

"""Diagnostics support for the Belgian Electricity Prices integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_API_KEY,
    CONF_CONSUMPTION_KWH,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_INJECTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
)
from .coordinator import (
    BePricesCoordinator,
    _monthly_snapshots,
    _recorder_daily_kwh,
    _shared_failed_fetches,
)

TO_REDACT = {CONF_API_KEY}


async def _kwh_window(
    hass: HomeAssistant, entry: ConfigEntry, days: int, *, side: str
) -> float | None:
    """Sum of kWh for ``side`` (``consumption`` or ``injection``) over
    the last ``days`` days from the entry's configured sensors.

    Returns ``None`` when no sensor is wired or the recorder has no
    data; the diagnostics blob renders that as a missing field rather
    than zero so a bug-report reader can tell the difference."""
    if side == "injection":
        day_id = entry.data.get(CONF_DAY_INJECTION_KWH)
        night_id = entry.data.get(CONF_NIGHT_INJECTION_KWH)
        total_id = entry.data.get(CONF_INJECTION_KWH)
    else:
        day_id = entry.data.get(CONF_DAY_CONSUMPTION_KWH)
        night_id = entry.data.get(CONF_NIGHT_CONSUMPTION_KWH)
        total_id = entry.data.get(CONF_CONSUMPTION_KWH)
    today = dt_util.now().date()
    start = today - timedelta(days=days)
    if day_id and night_id:
        d = await _recorder_daily_kwh(hass, day_id, start, today)
        n = await _recorder_daily_kwh(hass, night_id, start, today)
        total = sum(d.values()) + sum(n.values())
        return total if total > 0 else None
    if total_id:
        d = await _recorder_daily_kwh(hass, total_id, start, today)
        total = sum(d.values())
        return total if total > 0 else None
    return None


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    coordinator: BePricesCoordinator = entry.runtime_data
    data = coordinator.data

    hourly = sorted(data.hourly.items())

    # Recorder-backed consumption + injection roll-ups so the bug
    # reporter can see at a glance whether their kWh sensors are wired
    # up and feeding the recorder; mirrors what current_year_cost reads.
    today = dt_util.now().date()
    jan1 = today.replace(month=1, day=1)
    cons_year = await _kwh_window(hass, entry, 365, side="consumption")
    cons_ytd = await _kwh_window(hass, entry, (today - jan1).days, side="consumption")
    inj_year = await _kwh_window(hass, entry, 365, side="injection")
    inj_ytd = await _kwh_window(hass, entry, (today - jan1).days, side="injection")

    # Per-month archived snapshot publication labels: the YTD path
    # caches one snapshot per (supplier, contract, region, YYYY-MM).
    # Surfacing the labels makes "did the right cards land for past
    # months?" a one-glance check in a diagnostics dump.
    monthly_labels: dict[str, str | None] = {}
    extractor_id = entry.data.get("supplier")
    contract_id = entry.data.get("contract")
    region = entry.data.get("region")
    if extractor_id and contract_id and region:
        for key, snap in sorted(_monthly_snapshots(hass).items()):
            if key[0] == extractor_id and key[1] == contract_id and key[2] == region:
                monthly_labels[key[3]] = (
                    snap.publication_label if snap is not None else None
                )

    # Sibling-coordinator negative-fetch markers for this supplier
    # tuple; lets a bug reporter see whether the integration backed
    # off and why, without having to grep logs.
    failed_marker = None
    if extractor_id and contract_id and region:
        rec = _shared_failed_fetches(hass).get((extractor_id, contract_id, region))
        if rec is not None:
            ts, msg = rec
            failed_marker = {"at": ts.isoformat(), "error": msg}

    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "snapshot_publication": data.snapshot_publication,
            "snapshot_age_hours": round(data.snapshot_age_hours, 2),
            "snapshot_stale": data.snapshot_stale,
            "snapshot_valid_until": (
                data.snapshot_valid_until.isoformat()
                if data.snapshot_valid_until
                else None
            ),
            "last_error": data.last_error,
            "monthly_peak_kw": data.monthly_peak_kw,
            "monthly_peak_month": (
                data.monthly_peak_month.isoformat() if data.monthly_peak_month else None
            ),
            "capacity_cost_eur": data.capacity_cost_eur,
            "prosumer_cost_eur": data.prosumer_cost_eur,
            "yearly_fixed_fee_eur": data.yearly_fixed_fee_eur,
            "energy_fund_eur_per_month": data.energy_fund_eur_per_month,
            "injection_price_eur_per_kwh": data.injection_price_eur_per_kwh,
            "current_year_cost_eur": data.current_year_cost_eur,
            "hourly": [
                {
                    "start": dt_util.as_local(h).isoformat(),
                    "energy": round(bd.energy, 6),
                    "network": round(bd.network, 6),
                    "taxes": round(bd.taxes, 6),
                    "all_in": round(bd.all_in, 6),
                }
                for h, bd in hourly
            ],
        },
        "consumption": {
            "rolling_year_kwh": cons_year,
            "ytd_kwh": cons_ytd,
            "rolling_year_injection_kwh": inj_year,
            "ytd_injection_kwh": inj_ytd,
        },
        "monthly_snapshot_labels": monthly_labels,
        "shared_failure": failed_marker,
    }
