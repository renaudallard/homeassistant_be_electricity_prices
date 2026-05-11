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

"""End-to-end tests for the diagnostic Reset monthly peak button."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.button import (
    ResetMonthlyPeakButton,
    async_setup_entry,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator


def _flanders_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "capacity_mode": "sensor",
            "capacity_peak_sensor": "sensor.house_power",
        },
        title="Eneco (Flanders)",
    )


def _wallonia_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
        title="Eneco (Wallonia)",
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_button_registered_for_flanders_entry(hass: HomeAssistant) -> None:
    """A Flemish entry must produce exactly one diagnostic button so a
    user with an inflated peak (issue #19) can find the reset control
    on the device page without leaving the UI."""
    entry = _flanders_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord  # type: ignore[misc]
    added: list[ResetMonthlyPeakButton] = []

    def _add_entities(entities: list[ResetMonthlyPeakButton]) -> None:
        added.extend(entities)

    await async_setup_entry(hass, entry, _add_entities)  # type: ignore[arg-type]

    assert len(added) == 1
    button = added[0]
    assert isinstance(button, ResetMonthlyPeakButton)
    assert button.entity_description.key == "reset_monthly_peak"
    assert button.entity_description.translation_key == "reset_monthly_peak"
    assert button.entity_description.entity_category == EntityCategory.DIAGNOSTIC
    assert button.unique_id == f"{entry.entry_id}_reset_monthly_peak"
    assert button.device_info is not None
    assert (DOMAIN, entry.entry_id) in button.device_info["identifiers"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_button_skipped_for_non_flanders_entry(hass: HomeAssistant) -> None:
    """No capacity tariff outside Flanders, so no reset button: the
    UI shouldn't carry a control that would be a no-op."""
    entry = _wallonia_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = MagicMock()  # type: ignore[misc]
    added: list[ResetMonthlyPeakButton] = []

    await async_setup_entry(hass, entry, added.append)  # type: ignore[arg-type]

    assert added == []


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_button_press_invokes_coordinator_reset(hass: HomeAssistant) -> None:
    """Pressing the button must call coordinator.reset_monthly_peak so
    the inflated value clears immediately (no waiting for month
    rollover)."""
    entry = _flanders_entry()
    entry.add_to_hass(hass)
    coord = MagicMock(spec=BePricesCoordinator)
    coord.entry = entry
    coord.reset_monthly_peak = AsyncMock()
    entry.runtime_data = coord  # type: ignore[misc]

    button = ResetMonthlyPeakButton(coord)
    await button.async_press()

    coord.reset_monthly_peak.assert_awaited_once()
