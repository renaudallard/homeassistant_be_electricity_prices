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

"""End-to-end test that the OptionsFlow can change every parameter."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN


@pytest.fixture(autouse=True)
def _bypass_setup() -> "patch":
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _bypass_entsoe_validation() -> "patch":
    """Default to a passing ENTSO-E key check so the dynamic flow doesn't
    actually hit transparency.entsoe.eu in tests. Individual tests can
    re-patch this to assert the error paths."""
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value=None,
    ) as mock:
        yield mock


def _make_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
        title="Eneco - Eneco Zon & Wind Vast (Wallonia)",
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_walks_every_step(hass: HomeAssistant) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "init"

    # Step 1: switch supplier to cociter, region to wallonia (kept).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"supplier": "cociter", "region": "wallonia"},
    )
    assert result["step_id"] == "contract"

    # Step 2: pick cociter's variable contract.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "cociter_variable"}
    )
    assert result["step_id"] == "dso"

    # Step 3: keep ores.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    assert result["step_id"] == "meter"

    # Step 4: switch to bi-hourly meter.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "bi"}
    )
    # Wallonia entries get a DSO tariff mode question after meter.
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "bi_horaire"}
    )
    # Solar step.
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    # Then the meters step (yearly_cost inputs); skipped here.
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    # Verify the entry was rewritten end-to-end.
    assert entry.data["supplier"] == "cociter"
    assert entry.data["contract"] == "cociter_variable"
    assert entry.data["meter"] == "bi"
    assert "Cociter" in entry.title


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_invalid_api_key_keeps_user_on_form(
    hass: HomeAssistant,
    _bypass_entsoe_validation: "patch",
) -> None:
    """A bad token from ENTSO-E shows an error and reopens the same step."""
    _bypass_entsoe_validation.return_value = "invalid_api_key"

    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "power_dynamic"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "dynamic"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "bi_horaire"}
    )
    assert result["step_id"] == "api_key"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_key": "wrong"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "api_key"
    assert result["errors"] == {"api_key": "invalid_api_key"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_dynamic_branch_asks_api_key(
    hass: HomeAssistant,
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "power_dynamic"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "dynamic"}
    )
    # Wallonia: DSO tariff mode question first.
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "impact"}
    )
    # Then dynamic contract -> api_key step.
    assert result["step_id"] == "api_key"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_key": "new-key-456"}
    )
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["api_key"] == "new-key-456"
    # The Wallonia DSO tariff mode chosen mid-flow is persisted on the
    # entry, ready for the coordinator to pass into compute_breakdown.
    assert entry.data["dso_tariff_mode"] == "impact"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_flanders_branch_asks_capacity(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "capacity_mode": "fixed",
            "capacity_fixed_kw": 2.5,
        },
        title="Eneco - Power Fix (Flanders)",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "flanders"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "power_fix"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "fluvius_antwerpen"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    assert result["step_id"] == "capacity"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "capacity_mode": "fixed",
            "capacity_fixed_kw": 4.0,
        },
    )
    assert result["step_id"] == "solar"
    # User has solar this time - 5 kVA inverter on the injection tariff (this
    # entry is in Flanders so compensation regime doesn't apply anyway).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 5.0, "solar_regime": "injection"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["capacity_fixed_kw"] == 4.0
    assert entry.data["solar_kva"] == 5.0
    assert entry.data["solar_regime"] == "injection"
