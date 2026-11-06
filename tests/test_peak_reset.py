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


"""The Flemish monthly peak reset, an options-flow step for administrators."""

from __future__ import annotations

from contextlib import ExitStack
from http import HTTPStatus
from typing import Any

import pytest
from homeassistant import data_entry_flow
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import CLIENT_ID, MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from tests import make_entry
from tests.test_init import _setup_with_ev_box

_OPTIONS = "/api/config/config_entries/options/flow"
_BANKED = {f"2026-{m:02d}-01": 5.0 + m / 10 for m in range(1, 11)}


def _flemish() -> MockConfigEntry:
    return make_entry(
        region="flanders",
        dso="fluvius_antwerpen",
        title="Eneco (Flanders)",
        capacity_mode="sensor",
        capacity_peak_sensor="sensor.house_power",
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_menu_offers_the_reset_only_where_a_peak_is_billed(
    hass: HomeAssistant,
) -> None:
    """Outside Flanders every tick holds the peak at 0, so a reset there
    would do nothing and the menu does not offer one."""
    for entry, offered in ((_flemish(), True), (make_entry(), False)):
        entry.add_to_hass(hass)
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert ("reset_monthly_peak" in result["menu_options"]) is offered
        hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_reset_clears_the_banked_months_once_confirmed(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """The step names how many months go and clears nothing until it is
    submitted; then the running peak and every banked month are gone, on
    disk too, and there is no button entity left to press."""
    entry = _flemish()
    entry.add_to_hass(hass)
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entry):
            stack.enter_context(p)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert not hass.states.async_entity_ids("button")
        coord = entry.runtime_data
        coord._peak_history = dict(_BANKED)
        coord._peak_kw = 4.2

        opts = hass.config_entries.options
        result = await opts.async_init(entry.entry_id)
        result = await opts.async_configure(
            result["flow_id"], {"next_step_id": "reset_monthly_peak"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reset_monthly_peak"
        assert result["description_placeholders"] == {"months": "10"}
        assert coord._peak_history == _BANKED

        result = await opts.async_configure(result["flow_id"], {})
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "monthly_peak_reset"
        await hass.async_block_till_done()
        assert coord._peak_history == {}
        assert coord._peak_kw == 0.0
        stored = hass_storage[f"{DOMAIN}_cache_{entry.entry_id}"]["data"]["peak"]
        assert stored["history"] == {}
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_entry_that_is_not_loaded_resets_nothing(
    hass: HomeAssistant,
) -> None:
    """With no coordinator there is nothing to clear, and saying it was done
    would be false."""
    entry = _flemish()
    entry.add_to_hass(hass)
    opts = hass.config_entries.options
    result = await opts.async_init(entry.entry_id)
    result = await opts.async_configure(
        result["flow_id"], {"next_step_id": "reset_monthly_peak"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "compare_all_entry_reloading"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_only_an_administrator_reaches_the_reset(
    hass: HomeAssistant, hass_client: Any
) -> None:
    """The reason the reset moved: Home Assistant opens an options flow for
    administrators only, where any user allowed to press a button could
    press the old one."""
    assert await async_setup_component(hass, "config", {})
    entry = _flemish()
    entry.add_to_hass(hass)
    household = await hass.auth.async_create_user(
        "Household", group_ids=[GROUP_ID_USER]
    )
    refresh_token = await hass.auth.async_create_refresh_token(household, CLIENT_ID)
    token = hass.auth.async_create_access_token(refresh_token)

    refused = await (await hass_client(token)).post(
        _OPTIONS, json={"handler": entry.entry_id}
    )
    assert refused.status == HTTPStatus.UNAUTHORIZED

    admin = await hass_client()
    opened = await admin.post(_OPTIONS, json={"handler": entry.entry_id})
    assert opened.status == HTTPStatus.OK
    assert "reset_monthly_peak" in (await opened.json())["menu_options"]


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_setup_removes_the_old_reset_button(hass: HomeAssistant) -> None:
    """The reset used to be a button. Its registry row would stay behind,
    unavailable, or disabled where the user had disabled it, so setup drops
    it either way."""
    entries = [_flemish(), _flemish()]
    registry = er.async_get(hass)
    rows = []
    for entry, disabled_by in zip(
        entries, (None, er.RegistryEntryDisabler.USER), strict=True
    ):
        entry.add_to_hass(hass)
        rows.append(
            registry.async_get_or_create(
                "button",
                DOMAIN,
                f"{entry.entry_id}_reset_monthly_peak",
                config_entry=entry,
                disabled_by=disabled_by,
            )
        )
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entries[0]):
            stack.enter_context(p)
        # The first setup of the integration sets up both entries.
        assert await hass.config_entries.async_setup(entries[0].entry_id)
        await hass.async_block_till_done()
        assert all(entry.runtime_data for entry in entries)
        assert [registry.async_get(row.entity_id) for row in rows] == [None, None]
        assert not [e for e in registry.entities.values() if e.domain == "button"]
        for entry in entries:
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
