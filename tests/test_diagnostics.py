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

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.be_electricity_prices.pricing import PriceBreakdown
from tests import make_entry


def _entry_with_data(api_key: str = "secret-token") -> MockConfigEntry:
    return make_entry(
        contract="power_dynamic",
        meter="dynamic",
        title="Eneco - Eneco Zon & Wind Dynamisch (Wallonia)",
        options={"api_key": api_key},
        api_key=api_key,
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
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["entry"]["data"]["api_key"] != "THIS-IS-A-SECRET"
    assert dump["entry"]["options"]["api_key"] != "THIS-IS-A-SECRET"
    assert "THIS-IS-A-SECRET" not in str(dump)


async def test_diagnostics_keeps_contract_dates(hass: HomeAssistant) -> None:
    """Contract start/end dates are not secrets and must survive redaction."""
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "contract_start_date": "2025-11-15",
            "contract_end_date": "2027-11-14",
        },
    )
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["entry"]["data"]["contract_start_date"] == "2025-11-15"
    assert dump["entry"]["data"]["contract_end_date"] == "2027-11-14"


async def test_diagnostics_scrubs_api_key_from_last_error(hass: HomeAssistant) -> None:
    from dataclasses import replace

    secret = "TOKEN-IN-ERROR-TEXT"
    entry = _entry_with_data(api_key=secret)
    entry.add_to_hass(hass)
    data = replace(_coordinator_data(), last_error=f"ENTSO-E error url=...{secret}...")
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=data
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert secret not in str(dump)
    assert "**REDACTED**" in dump["coordinator"]["last_error"]


async def test_diagnostics_includes_snapshot_and_hourly(hass: HomeAssistant) -> None:
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )

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


async def test_diagnostics_reports_the_injection_price_the_entity_shows(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The dump carries both injection numbers: the one the last tick resolved
    and the one the entity is showing. They diverge as soon as the clock leaves
    the tick's slot, and a triage dump that only had the former would not match
    the sensor the reporter is complaining about."""
    from dataclasses import replace

    midnight = datetime(2026, 7, 22, tzinfo=UTC)
    inj = {midnight + timedelta(hours=h): 0.01 * h for h in range(24)}
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    data = replace(
        _coordinator_data(), injection_hourly=inj, injection_price_eur_per_kwh=0.045
    )
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=data
    )

    freezer.move_to("2026-07-22T09:30:00+00:00")
    coord = (await async_get_config_entry_diagnostics(hass, entry))["coordinator"]
    assert coord["injection_price_eur_per_kwh"] == 0.045  # what the tick resolved
    assert coord["injection_price_current_slot"] == pytest.approx(0.09)


async def test_diagnostics_includes_consumption_and_monthly_labels(
    hass: HomeAssistant,
) -> None:
    """The new top-level keys (consumption / monthly_snapshot_labels /
    shared_failure) must appear in the dump so a bug reporter can see
    whether the recorder has data, which past months are cached, and
    whether sibling-coordinator backoff is currently active."""
    from datetime import UTC, datetime
    from unittest.mock import patch

    from custom_components.be_electricity_prices.snapshot_store import (
        _monthly_snapshots,
        _shared_failed_fetches,
    )
    from tests import make_snapshot

    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )

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
        1,
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

    # Consumption + injection blocks always present, values None when no
    # sensor wired.
    assert dump["consumption"]["rolling_year_kwh"] is None
    assert dump["consumption"]["ytd_kwh"] is None
    assert dump["injection"]["rolling_year_kwh"] is None
    assert dump["injection"]["ytd_kwh"] is None
    # Per-month archive labels: only this entry's tuple, not bolt's.
    assert dump["monthly_snapshot_labels"] == {"2026-03": "march 2026"}
    # Shared-failure marker round-tripped.
    assert dump["shared_failure"]["error"] == "transient HTTP 503 from supplier"


async def test_diagnostics_wired_zero_kwh_reports_zero_not_missing(
    hass: HomeAssistant,
) -> None:
    # A wired consumption sensor whose window totals zero must report 0.0,
    # not None, so the dump distinguishes an unconfigured sensor (missing)
    # from a wired one that reads zero.
    from unittest.mock import patch

    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "consumption_kwh": "sensor.meter"}
    )

    async def _empty_recorder(
        _hass: HomeAssistant, entity_id: str, start: object, end: object
    ) -> dict[object, float]:
        return {}

    with patch(
        "custom_components.be_electricity_prices.diagnostics._recorder_daily_kwh",
        new=_empty_recorder,
    ):
        dump = await async_get_config_entry_diagnostics(hass, entry)

    # Wired sensor, zero rows -> 0.0 (not None).
    assert dump["consumption"]["rolling_year_kwh"] == 0.0
    assert dump["consumption"]["ytd_kwh"] == 0.0
    # No injection sensor wired -> still None.
    assert dump["injection"]["rolling_year_kwh"] is None


async def test_diagnostics_summarises_the_spot_cache_by_month(
    hass: HomeAssistant,
) -> None:
    """The replayed day-ahead cache has to be visible somewhere.

    current_year_cost is priced off this cache and nothing surfaced it, so a
    replay billing off wrong stored prices could not be told apart from a wrong
    tariff or wrong kWh without another round of screenshots. Counts and
    extremes rather than the hours themselves: a year is about 8760 values and
    the only question being asked is whether they look like the market."""
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    spots = {
        datetime(2026, 3, 1, 0, tzinfo=UTC) + timedelta(hours=i): 0.10 + i * 0.01
        for i in range(3)
    }
    spots[datetime(2026, 4, 2, 10, tzinfo=UTC)] = 0.40
    entry.runtime_data = SimpleNamespace(
        _historical_spots=spots, _historical_spot_quarters={}, data=_coordinator_data()
    )

    dump = await async_get_config_entry_diagnostics(hass, entry)
    by_month = dump["spot_cache_by_month"]
    assert set(by_month) == {"2026-03", "2026-04"}
    assert by_month["2026-03"] == {
        "hours": 3,
        "mean": 0.11,
        "min": 0.10,
        "max": 0.12,
    }
    # One outlying month stands out at a glance, which is the whole point.
    assert by_month["2026-04"]["mean"] == 0.40


async def test_diagnostics_spot_cache_empty_when_nothing_cached(
    hass: HomeAssistant,
) -> None:
    """A static contract caches no spots and must not grow a bogus row."""
    entry = _entry_with_data()
    entry.add_to_hass(hass)
    entry.runtime_data = SimpleNamespace(
        _historical_spots={}, _historical_spot_quarters={}, data=_coordinator_data()
    )
    dump = await async_get_config_entry_diagnostics(hass, entry)
    assert dump["spot_cache_by_month"] == {}


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
