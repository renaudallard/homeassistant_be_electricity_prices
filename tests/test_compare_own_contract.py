"""Quoting the household's own contract on the comparison page.

The picker offers the entry's own contract so a household can ask what the
same contract costs on another meter or solar regime. Quoted against itself
with nothing changed, the two sides are one bill and the difference is zero.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

from tests import make_entry
from tests.test_options_flow import (
    _compare_placeholders,
    _real_coordinator,
    _stub_snapshot,
)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        # Signed at 0,12 where today's card prints 0,18.
        {"contract_start_date": "2025-06-01", "manual_energy_single": 0.12},
    ],
)
async def test_the_own_contract_against_itself_costs_nothing_more(
    hass: HomeAssistant, freezer: Any, extra: dict[str, Any]
) -> None:
    """The target side is the contract the household signed, at the rates it
    locked in, not today's card for a new signer: 210 EUR a year apart at
    3500 kWh before."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = make_entry(**extra)
    entry.add_to_hass(hass)
    card = _stub_snapshot("eneco", "power_fix", 0.18)
    entry.runtime_data = _real_coordinator(hass, entry, card)
    p = await _compare_placeholders(
        hass,
        entry,
        supplier="eneco",
        contract="power_fix",
        target_snapshot=card,
        meter="mono",
    )
    assert p["current_annual"] == p["compare_annual"]
    assert float(p["delta_annual"]) == 0.0
    assert float(p["delta_ytd"]) == 0.0


async def test_a_held_contract_is_not_granted_a_new_welcome_credit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Signed in 2024, so the first year is long over: the 100 EUR the card
    grants a new signer is not the household's to collect again."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = make_entry(contract_start_date="2024-01-01")
    entry.add_to_hass(hass)
    card = replace(_stub_snapshot("eneco", "power_fix", 0.18), welcome_credit_eur=100.0)
    entry.runtime_data = _real_coordinator(hass, entry, card)
    p = await _compare_placeholders(
        hass,
        entry,
        supplier="eneco",
        contract="power_fix",
        target_snapshot=card,
        meter="mono",
    )
    assert p["current_annual"] == p["compare_annual"]
    assert float(p["delta_annual"]) == 0.0
