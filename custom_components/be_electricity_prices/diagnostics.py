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

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_API_KEY
from .coordinator import BePricesCoordinator

TO_REDACT = {CONF_API_KEY}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for one config entry."""
    coordinator: BePricesCoordinator = entry.runtime_data
    data = coordinator.data

    hourly = sorted(data.hourly.items())
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
            "last_error": data.last_error,
            "monthly_peak_kw": data.monthly_peak_kw,
            "monthly_peak_month": (
                data.monthly_peak_month.isoformat() if data.monthly_peak_month else None
            ),
            "capacity_cost_eur": data.capacity_cost_eur,
            "prosumer_cost_eur": data.prosumer_cost_eur,
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
    }
