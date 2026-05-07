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


async def test_diagnostics_includes_consumption_and_monthly_labels(
    hass: HomeAssistant,
) -> None:
    """The new top-level keys (consumption / monthly_snapshot_labels /
    shared_failure) must appear in the dump so a bug reporter can see
    whether the recorder has data, which past months are cached, and
    whether sibling-coordinator backoff is currently active."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    from custom_components.be_electricity_prices.coordinator import (
        _monthly_snapshots,
        _shared_failed_fetches,
    )
    from tests import make_snapshot

    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(data=_coordinator_data())

    # Seed the per-month archive cache for this entry's tuple so the
    # diagnostics dump should surface its publication label.
    archived = make_snapshot(
        supplier="eneco",
        contract="power_dynamic",
        source_url="test://archived",
        publication_label="march 2026",
    )
    _monthly_snapshots(hass)[("eneco", "power_dynamic", "wallonia", "2026-03")] = (
        archived
    )
    # And one for a different tuple that must NOT leak into our dump.
    _monthly_snapshots(hass)[("bolt", "bolt_fix", "wallonia", "2026-03")] = archived
    # Seed a shared-failure marker for our tuple.
    _shared_failed_fetches(hass)[("eneco", "power_dynamic", "wallonia")] = (
        datetime(2026, 5, 4, 10, 0, tzinfo=UTC),
        "transient HTTP 503 from supplier",
    )

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: object, end: object
    ) -> dict[object, float]:
        # No kWh sensors configured on the test entry, so this won't
        # be called. Patch returns empty defensively.
        return {}

    with patch(
        "custom_components.be_electricity_prices.diagnostics._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        dump = await async_get_config_entry_diagnostics(hass, entry)

    # Consumption block always present, values None when no sensor wired.
    assert dump["consumption"]["rolling_year_kwh"] is None
    assert dump["consumption"]["ytd_kwh"] is None
    # Per-month archive labels: only this entry's tuple, not bolt's.
    assert dump["monthly_snapshot_labels"] == {"2026-03": "march 2026"}
    # Shared-failure marker round-tripped.
    assert dump["shared_failure"]["error"] == "transient HTTP 503 from supplier"


async def test_diagnostics_returns_placeholder_when_runtime_data_undefined(
    hass: HomeAssistant,
) -> None:
    """A user clicking 'Download diagnostics' mid-reload (entry.runtime_data
    is HA's UNDEFINED singleton) must get a structured placeholder rather
    than an AttributeError on coordinator.data."""
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    # Don't assign runtime_data: HA returns UNDEFINED for unset attributes.
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump == {"status": "coordinator_not_ready"}
