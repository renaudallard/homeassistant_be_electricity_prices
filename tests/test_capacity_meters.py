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

"""The Flemish capacity charge per meter.

Fluvius bills a household with an exclusive-night meter one monthly peak, the
highest simultaneous sum of both meters' quarter-hour peaks, so the night
circuit's own entry is billed no charge: the main entry carries it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.button import (
    async_setup_entry as button_setup,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.coordinator_data import CoordinatorData
from custom_components.be_electricity_prices.fees import bills_capacity
from custom_components.be_electricity_prices.sensor import (
    async_setup_entry as sensor_setup,
)
from tests import make_entry
from tests.test_config_flow_energy_defaults import _add_dsmr_monthly_peak, _shown

_PEAK = "sensor.house_power"


def _flemish(meter: str, **extra: Any) -> MockConfigEntry:
    return make_entry(
        region="flanders",
        dso="fluvius_antwerpen",
        meter=meter,
        title=f"Eneco ({meter})",
        **extra,
    )


@pytest.mark.parametrize(
    ("region", "meter", "billed"),
    [
        ("flanders", "mono", True),
        ("flanders", "bi", True),
        ("flanders", "dynamic", True),
        ("flanders", "exclusive_night", False),
        ("wallonia", "mono", False),
        ("brussels", "bi", False),
    ],
)
def test_only_a_flemish_main_meter_is_billed_a_charge_of_its_own(
    region: str, meter: str, billed: bool
) -> None:
    assert bills_capacity({"region": region, "meter": meter}) is billed


async def test_a_night_circuit_tracks_no_peak_and_is_billed_none(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The night entry was offered the household's DSMR peak sensor and billed
    a second full charge on it, 272,32 EUR a year at 5,2 kW on Fluvius
    Antwerpen, and 130,92 on the 2,5 kW minimum with the sensor cleared or a
    fixed peak stated. Every path prices the charge off the billed peak."""
    freezer.move_to("2026-10-23 12:00:00+02:00")
    hass.states.async_set(_PEAK, "5.2", {"unit_of_measurement": "kW"})
    billed: dict[str, float] = {}
    for label, entry in (
        ("main", _flemish("mono", capacity_mode="sensor", capacity_peak_sensor=_PEAK)),
        (
            "night",
            _flemish(
                "exclusive_night", capacity_mode="sensor", capacity_peak_sensor=_PEAK
            ),
        ),
        ("night, sensor cleared", _flemish("exclusive_night", capacity_mode="sensor")),
        (
            "night, fixed",
            _flemish("exclusive_night", capacity_mode="fixed", capacity_fixed_kw=4.0),
        ),
    ):
        entry.add_to_hass(hass)
        coord = BePricesCoordinator(hass, entry)
        coord._peak_history = {"2026-09-01": 6.0}
        await coord._track_monthly_peak()
        billed[label] = coord._billed_peak_kw()
        if label != "main":
            assert coord._peak_kw == 0.0, label
            assert coord._peak_history == {}, label
    assert billed == {
        "main": pytest.approx((6.0 + 5.2) / 2),
        "night": 0.0,
        "night, sensor cleared": 0.0,
        "night, fixed": 0.0,
    }


def _sensor_keys(entry: MockConfigEntry) -> set[str]:
    entry.runtime_data = SimpleNamespace(  # type: ignore[misc]
        data=CoordinatorData(), entry=entry, intended_unique_ids={}
    )
    out: list[Any] = []

    async def _run() -> None:
        await sensor_setup(None, entry, out.extend)  # type: ignore[arg-type]

    asyncio.run(_run())
    return {e.entity_description.key for e in out if hasattr(e, "entity_description")}


def test_a_night_circuit_gets_no_capacity_sensors() -> None:
    capacity = {"capacity_cost", "monthly_peak_kw"}
    assert capacity <= _sensor_keys(_flemish("mono"))
    assert not capacity & _sensor_keys(_flemish("exclusive_night"))


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_night_circuit_gets_no_reset_peak_button(hass: HomeAssistant) -> None:
    for meter, count in (("mono", 1), ("exclusive_night", 0)):
        entry = _flemish(meter, capacity_mode="sensor", capacity_peak_sensor=_PEAK)
        entry.add_to_hass(hass)
        entry.runtime_data = BePricesCoordinator(hass, entry)  # type: ignore[misc]
        added: list[Any] = []
        await button_setup(hass, entry, added.extend)  # type: ignore[arg-type]
        assert len(added) == count, meter


async def _steps(
    hass: HomeAssistant, entry: MockConfigEntry, answers: dict[str, dict[str, Any]]
) -> list[str]:
    """The steps Edit settings walks, every form submitted as shown but for
    ``answers``, up to the energy meters (not submitted)."""
    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    result = await opts.async_init(entry.entry_id)
    result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
    steps: list[str] = []
    while result["type"] == FlowResultType.FORM and result["step_id"] != "meters":
        steps.append(result["step_id"])
        if result["step_id"] == "capacity":
            # What the step offers the entry before anything is submitted.
            steps.append(f"offered {_shown(result).get('capacity_peak_sensor')}")
        result = await opts.async_configure(
            result["flow_id"],
            {**_shown(result), **answers.get(result["step_id"], {})},
        )
    return steps


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_edit_onto_a_night_circuit_skips_the_capacity_step(
    hass: HomeAssistant,
) -> None:
    """Asked of the night entry, the step offered the household's own DSMR
    peak sensor, which is how the second charge was billed in full."""
    peak = _add_dsmr_monthly_peak(hass)
    entry = _flemish("mono", solar_regime="none")
    entry.add_to_hass(hass)
    main = await _steps(hass, entry, {})
    assert "capacity" in main, "a main meter still has a peak to give"
    night = await _steps(hass, entry, {"meter": {"meter": "exclusive_night"}})
    assert "capacity" not in night
    assert "solar" in night
    assert f"offered {peak}" not in night
