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
circuit's own entry is billed no charge: the main entry carries it. An analog
meter has no peak to measure: it is billed the charge on the minimum peak and
a higher kWh rate, read off its area's Fluvius sheet.
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
from custom_components.be_electricity_prices.fees import (
    _compute_capacity,
    bills_capacity,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
)
from custom_components.be_electricity_prices.sensor import (
    async_setup_entry as sensor_setup,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _resolve_snapshot,
    analog_meter,
)
from tests import make_entry, make_snapshot
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


# An analog meter: Fluvius "kan je pieken niet meten. Je betaalt dan een
# forfaitair bedrag voor 2.5 kW als maandpiek en een aanvullend tarief voor je
# verbruik in kWh". The surcharge is Fluvius Antwerpen's for 2026, 0,0492427
# less 0,0234492, and the card figures below are Luminus Comfy's October 2026
# row for the area, whose "Compteur classique" columns print 130,92 EUR a year,
# 8,09 c/kWh and 7,55 c/kWh on the exclusive-night circuit.
_ANTWERPEN_2026 = 0.0257935


def _card(supplier: str = "luminus") -> Any:
    return make_snapshot(
        supplier=supplier,
        contract="luminus_comfy",
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.0535,
                distribution_exclusive_night=0.0481,
                transport=0.0,
                capacity_eur_per_kw_year=52.37,
                network_ceiling_eur_per_kwh=0.3472738,
            ),
            "fluvius_west": DsoOverlay(distribution_single=0.0566, transport=0.0),
        },
    )


@pytest.mark.parametrize(
    ("data", "analog"),
    [
        ({}, True),
        ({"meter": "bi"}, True),
        ({"meter": "exclusive_night"}, True),
        ({"meter": "dynamic"}, False),
        ({"analog_meter": False}, False),
        ({"region": "wallonia", "dso": "ores"}, False),
        ({"supplier": "custom"}, False),
        ({"supplier": "social"}, False),
    ],
)
def test_an_analog_meter_is_a_flemish_one_billed_on_fluvius_tariffs(
    data: dict[str, Any], analog: bool
) -> None:
    """Only Fluvius bills it apart, a dynamic contract needs a digital meter,
    the custom supplier bills the network figures typed for the meter, and the
    social tariff's network component is the CREG's."""
    entry = _flemish("mono", analog_meter=True)
    assert analog_meter({**entry.data, **data}) is analog


def test_an_analog_meter_is_billed_the_areas_kwh_rate_and_no_ceiling() -> None:
    from datetime import date

    from custom_components.be_electricity_prices import fluvius

    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    october = date(2026, 10, 1)
    card = _card()
    analog = _resolve_snapshot(
        _flemish("mono", analog_meter=True),
        card,
        annual_kwh=3500.0,
        delivery_month=october,
    )
    overlay = analog.dsos["fluvius_antwerpen"]
    # On the card's VAT-inclusive basis: 8,08 and 7,54 c/kWh against the 8,09
    # and 7,55 the card rounds the sheet's own sums to.
    assert overlay.distribution_single == pytest.approx(0.0535 + _ANTWERPEN_2026 * 1.06)
    assert overlay.distribution_exclusive_night == pytest.approx(
        0.0481 + _ANTWERPEN_2026 * 1.06
    )
    assert overlay.network_ceiling_eur_per_kwh is None
    # The capacity rate stays, billed on the minimum peak: 2,5 x 52,37.
    assert overlay.capacity_eur_per_kw_year == 52.37
    # Another area's figures are its own.
    assert analog.dsos["fluvius_west"].distribution_single == 0.0566
    for label, entry, snap in (
        ("digital", _flemish("mono"), card),
        ("dynamic", _flemish("dynamic", analog_meter=True), card),
        ("custom card", _flemish("mono", analog_meter=True), _card("custom")),
    ):
        same = _resolve_snapshot(
            entry, snap, annual_kwh=3500.0, delivery_month=october
        ).dsos["fluvius_antwerpen"]
        assert same.distribution_single == 0.0535, label
        assert same.network_ceiling_eur_per_kwh is not None, label
    # Before the area's sheet is read the meter is billed as before.
    fluvius._analog.clear()
    before = _resolve_snapshot(
        _flemish("mono", analog_meter=True),
        card,
        annual_kwh=3500.0,
        delivery_month=october,
    )
    assert before.dsos["fluvius_antwerpen"].distribution_single == 0.0535


async def test_an_analog_meter_is_billed_the_minimum_peak(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing measured and nothing stated counts: 2,5 kW, the sheet's "Vaste
    term" of 130,92 EUR a year on Luminus's 52,37 per kW. An analog night
    meter pays it too; a digital one none (the main entry carries it)."""
    freezer.move_to("2026-10-23 12:00:00+02:00")
    hass.states.async_set(_PEAK, "5.2", {"unit_of_measurement": "kW"})
    billed: dict[str, float] = {}
    for label, entry in (
        (
            "analog",
            _flemish(
                "mono",
                analog_meter=True,
                capacity_mode="sensor",
                capacity_peak_sensor=_PEAK,
            ),
        ),
        (
            "analog, fixed",
            _flemish(
                "bi", analog_meter=True, capacity_mode="fixed", capacity_fixed_kw=6.0
            ),
        ),
        ("analog night", _flemish("exclusive_night", analog_meter=True)),
        ("digital night", _flemish("exclusive_night")),
    ):
        entry.add_to_hass(hass)
        coord = BePricesCoordinator(hass, entry)
        await coord._track_monthly_peak()
        assert coord._peak_kw == 0.0, label
        assert coord._peak_terms() == [], label
        billed[label] = coord._billed_peak_kw()
    assert billed == {
        "analog": 2.5,
        "analog, fixed": 2.5,
        "analog night": 2.5,
        "digital night": 0.0,
    }
    analog = _flemish("mono", analog_meter=True)
    assert 12 * _compute_capacity(_card(), analog, 2.5) == pytest.approx(130.925)


def test_an_analog_night_meter_keeps_its_capacity_sensors() -> None:
    """It is billed the flat charge, which capacity_cost shows."""
    assert {"capacity_cost", "monthly_peak_kw"} <= _sensor_keys(
        _flemish("exclusive_night", analog_meter=True)
    )


async def test_the_tick_reads_the_areas_sheet_and_the_year_before_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Until Fluvius publishes the year, the year before stands in, and the
    card in hand is re-resolved the moment the rate arrives. The tick bills
    the minimum peak's charge, 130,92 EUR a year."""
    from unittest.mock import AsyncMock, patch

    from custom_components.be_electricity_prices import fluvius
    from custom_components.be_electricity_prices.cohort import _CohortLegs

    freezer.move_to("2027-01-05 12:00:00+01:00")
    entry = _flemish("mono", analog_meter=True)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(_card())
    booted = coord._snapshot
    assert booted is not None
    assert booted.dsos["fluvius_antwerpen"].distribution_single == 0.0535

    async def _read(_session: Any, year: int, dso: str) -> float | None:
        if year == 2026:
            fluvius._analog[(year, dso)] = _ANTWERPEN_2026
            return _ANTWERPEN_2026
        return None

    read = AsyncMock(side_effect=_read)
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    with (
        patch(
            "custom_components.be_electricity_prices.coordinator_tick."
            "ensure_analog_surcharge",
            read,
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_tick._cohort_legs",
            AsyncMock(return_value=_CohortLegs(None, None)),
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_costs."
            "_compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        data = await coord._update_body()
    assert [call.args[1:] for call in read.await_args_list] == [
        (2027, "fluvius_antwerpen"),
        (2026, "fluvius_antwerpen"),
    ]
    resolved = coord._snapshot
    assert resolved is not None and resolved is not booted
    assert resolved.dsos["fluvius_antwerpen"].distribution_single == pytest.approx(
        0.0535 + _ANTWERPEN_2026 * 1.06
    )
    assert data.capacity_billed_peak_kw == 2.5
    assert 12 * data.capacity_cost_eur == pytest.approx(130.925)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_meter_step_asks_and_drops_the_analog_answer(
    hass: HomeAssistant,
) -> None:
    """Asked of a Flemish meter only, never kept on a dynamic one, and an
    analog meter has no peak source to give."""
    entry = _flemish("mono", solar_regime="none")
    entry.add_to_hass(hass)
    steps = await _steps(hass, entry, {"meter": {"analog_meter": True}})
    assert "capacity" not in steps
    assert "solar" in steps

    wallonia = make_entry(solar_regime="none")
    wallonia.add_to_hass(hass)
    shown = await _form(hass, wallonia, "meter")
    assert "analog_meter" not in shown
    shown = await _form(hass, entry, "meter")
    assert shown["analog_meter"] is False

    stored = _flemish("mono", solar_regime="none", analog_meter=True)
    stored.add_to_hass(hass)
    shown = await _form(hass, stored, "meter")
    assert shown["analog_meter"] is True
    data = await _saved(hass, stored, {"meter": {"meter": "dynamic"}})
    assert "analog_meter" not in data


async def _form(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> Any:
    """What Edit settings shows on ``step``, every form before it as shown."""
    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    result = await opts.async_init(entry.entry_id)
    result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
    while result["type"] == FlowResultType.FORM and result["step_id"] != step:
        result = await opts.async_configure(result["flow_id"], _shown(result))
    return _shown(result)


async def _saved(
    hass: HomeAssistant, entry: MockConfigEntry, answers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The entry's data once Edit settings is walked through with ``answers``."""
    from unittest.mock import patch

    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        result = await opts.async_init(entry.entry_id)
        result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
        while result["type"] == FlowResultType.FORM:
            result = await opts.async_configure(
                result["flow_id"],
                {**_shown(result), **answers.get(result["step_id"], {})},
            )
    return dict(entry.data)


def test_an_earlier_contract_takes_the_meters_analog_answer() -> None:
    """A contract recorded before the question was asked was on the same
    meter; one recorded after keeps the answer it was given."""
    from datetime import date

    from custom_components.be_electricity_prices.contract_periods import (
        previous_periods,
    )

    def _held(**extra: Any) -> dict[str, Any]:
        return {
            "previous_contracts": [
                {
                    "until": "2026-06-30",
                    "data": {
                        "supplier": "engie",
                        "contract": "engie_easy_fixed",
                        "region": "flanders",
                        "dso": "fluvius_antwerpen",
                        "meter": "mono",
                        **extra,
                    },
                }
            ]
        }

    entry = {**_flemish("mono", analog_meter=True).data}
    for held, analog in (({}, True), ({"analog_meter": False}, False)):
        periods = previous_periods(
            {**entry, **_held(**held)}, date(2026, 1, 1), date(2026, 10, 23)
        )
        assert [analog_meter(p.data) for p in periods] == [analog], held
