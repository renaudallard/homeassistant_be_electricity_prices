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

"""Tests for the diagnostics platform."""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.be_electricity_prices.pricing import PriceBreakdown


def _entry_with_data(api_key: str = "secret-token") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": api_key,
        },
        options={"api_key": api_key},
        title="Eneco - Eneco Zon & Wind Dynamisch (Wallonia)",
    )


def _coordinator_data() -> CoordinatorData:
    hour = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    return CoordinatorData(
        hourly={
            hour: PriceBreakdown(energy=0.18, network=0.065, taxes=0.067, all_in=0.312)
        },
        snapshot_publication="april 2026",
        snapshot_age_hours=1.5,
        snapshot_stale=False,
        snapshot_valid_until=date(2026, 4, 30),
        last_error="",
        monthly_peak_kw=3.2,
        monthly_peak_month=date(2026, 4, 1),
        capacity_cost_eur=12.34,
        prosumer_cost_eur=0.0,
        yearly_fixed_fee_eur=72.0,
        energy_fund_eur_per_month=0.0,
        injection_price_eur_per_kwh=0.045,
        current_year_cost_eur=345.67,
    )


async def test_diagnostics_redacts_api_key(hass: HomeAssistant) -> None:
    entry = _entry_with_data(api_key="THIS-IS-A-SECRET")
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(data=_coordinator_data())

    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["entry"]["data"]["api_key"] != "THIS-IS-A-SECRET"
    assert dump["entry"]["options"]["api_key"] != "THIS-IS-A-SECRET"
    assert "THIS-IS-A-SECRET" not in str(dump)


async def test_diagnostics_includes_snapshot_and_hourly(hass: HomeAssistant) -> None:
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(data=_coordinator_data())

    dump = await async_get_config_entry_diagnostics(hass, entry)
    coord = dump["coordinator"]
    assert coord["snapshot_publication"] == "april 2026"
    assert coord["snapshot_age_hours"] == 1.5
    assert coord["snapshot_valid_until"] == "2026-04-30"
    assert coord["monthly_peak_kw"] == 3.2
    assert coord["monthly_peak_month"] == "2026-04-01"
    assert coord["capacity_cost_eur"] == 12.34
    assert coord["yearly_fixed_fee_eur"] == 72.0
    assert coord["energy_fund_eur_per_month"] == 0.0
    assert coord["injection_price_eur_per_kwh"] == 0.045
    assert coord["current_year_cost_eur"] == 345.67
    assert len(coord["hourly"]) == 1
    assert coord["hourly"][0]["all_in"] == 0.312
