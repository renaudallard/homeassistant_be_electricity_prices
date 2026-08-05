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

"""Energy Manager pre-fill on the meters config-flow step."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.config_flow import (
    _apply_energy_manager_capacity_default,
    _apply_energy_manager_defaults,
    _classify_tariff,
)


def _grid_prefs(
    consumption: str | None = None,
    injection: str | None = None,
) -> dict[str, Any]:
    flow_from = [{"stat_energy_from": consumption}] if consumption is not None else []
    flow_to = [{"stat_energy_to": injection}] if injection is not None else []
    return {
        "energy_sources": [{"type": "grid", "flow_from": flow_from, "flow_to": flow_to}]
    }


def _patch_manager(prefs: dict[str, Any] | None) -> Any:
    manager = AsyncMock()
    manager.data = prefs
    async_get_manager = AsyncMock(return_value=manager)
    return patch(
        "homeassistant.components.energy.data.async_get_manager",
        new=async_get_manager,
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_no_energy_manager_leaves_defaults_untouched(
    hass: HomeAssistant,
) -> None:
    """When the Energy dashboard isn't configured, the helper must
    leave the defaults dict alone -- nothing to pre-fill from."""
    defaults: dict[str, Any] = {}
    with _patch_manager(None):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults == {}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_grid_source_pre_fills_cumulative_sensors(
    hass: HomeAssistant,
) -> None:
    """A configured grid source pre-fills the 2-sensor cumulative
    consumption + injection fields, leaving the 4-sensor day/night
    pickers blank when no utility_meter helper is found."""
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(
        consumption="sensor.electricity_meter_total",
        injection="sensor.electricity_returned_total",
    )
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["consumption_kwh"] == "sensor.electricity_meter_total"
    assert defaults["injection_kwh"] == "sensor.electricity_returned_total"
    assert "day_consumption_kwh" not in defaults
    assert "night_consumption_kwh" not in defaults


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_existing_user_choice_is_never_overridden(
    hass: HomeAssistant,
) -> None:
    """OptionsFlow seeds defaults with whatever the user already
    saved. The Energy Manager pre-fill must not blow that away --
    re-running the helper would otherwise demote the user's manual
    pick on every options reopen."""
    defaults: dict[str, Any] = {"consumption_kwh": "sensor.user_pick"}
    prefs = _grid_prefs(consumption="sensor.energy_dashboard_pick")
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["consumption_kwh"] == "sensor.user_pick"
    assert "injection_kwh" not in defaults


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_recorder_only_statistic_id_is_skipped(
    hass: HomeAssistant,
) -> None:
    """The Energy dashboard accepts pure recorder statistic ids that
    aren't entity-backed (e.g. ``custom:water_total``). EntitySelector
    can't render those, so the pre-fill must skip them rather than
    seed a default that won't validate."""
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="custom:bare_statistic")
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert "consumption_kwh" not in defaults


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_classify_tariff_unambiguous_and_ambiguous_cases() -> None:
    assert _classify_tariff("peak") == "day"
    assert _classify_tariff("offpeak") == "night"
    assert _classify_tariff("Off-Peak") == "night"
    assert _classify_tariff("jour") == "day"
    assert _classify_tariff("nuit") == "night"
    assert _classify_tariff("dag") == "day"
    assert _classify_tariff("nacht") == "night"
    assert _classify_tariff("piek") == "day"
    assert _classify_tariff("dal") == "night"
    # Names that match neither slot (high/low) -> None.
    assert _classify_tariff("high") is None
    assert _classify_tariff("low") is None
    # Names that match both somehow -> None (defensive).
    assert _classify_tariff("peak_night_combined") is None


def _add_utility_meter_entry(
    hass: HomeAssistant,
    *,
    source: str,
    tariffs: list[str],
    entry_id: str,
    child_entity_ids: dict[str, str],
) -> None:
    """Register a fake utility_meter config entry with per-tariff child
    sensors in the entity registry, mirroring the unique_id shape HA
    uses (``{entry_id}_{tariff}``)."""
    entry = MockConfigEntry(
        domain="utility_meter",
        entry_id=entry_id,
        data={},
        options={"source": source, "tariffs": tariffs},
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    for tariff, entity_id in child_entity_ids.items():
        ent_reg.async_get_or_create(
            domain="sensor",
            platform="utility_meter",
            unique_id=f"{entry_id}_{tariff}",
            config_entry=entry,
            suggested_object_id=entity_id.removeprefix("sensor."),
        )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_utility_meter_helper_fills_day_night_registers(
    hass: HomeAssistant,
) -> None:
    """When a utility_meter helper splits the grid source into peak /
    offpeak tariffs, the helper must also pre-fill the 4-sensor
    day/night registers from the matching child entities."""
    _add_utility_meter_entry(
        hass,
        source="sensor.electricity_meter_total",
        tariffs=["peak", "offpeak"],
        entry_id="um_consumption",
        child_entity_ids={
            "peak": "sensor.consumption_peak",
            "offpeak": "sensor.consumption_offpeak",
        },
    )
    _add_utility_meter_entry(
        hass,
        source="sensor.electricity_returned_total",
        tariffs=["jour", "nuit"],
        entry_id="um_injection",
        child_entity_ids={
            "jour": "sensor.injection_jour",
            "nuit": "sensor.injection_nuit",
        },
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(
        consumption="sensor.electricity_meter_total",
        injection="sensor.electricity_returned_total",
    )
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["consumption_kwh"] == "sensor.electricity_meter_total"
    assert defaults["day_consumption_kwh"] == "sensor.consumption_peak"
    assert defaults["night_consumption_kwh"] == "sensor.consumption_offpeak"
    assert defaults["injection_kwh"] == "sensor.electricity_returned_total"
    assert defaults["day_injection_kwh"] == "sensor.injection_jour"
    assert defaults["night_injection_kwh"] == "sensor.injection_nuit"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_underscored_off_peak_tariff_does_not_capture_the_day_register(
    hass: HomeAssistant,
) -> None:
    """``off_peak`` and ``peak`` are both valid utility_meter tariff names,
    and HA builds children as ``{entry_id}_{tariff}``, so ``<entry>_off_peak``
    also ends with ``_peak``. A suffix match therefore bound the day slot to
    the off-peak register, and the user's night kWh got billed at the day
    rate while day kWh vanished."""
    _add_utility_meter_entry(
        hass,
        source="sensor.grid_total",
        tariffs=["off_peak", "peak"],
        entry_id="um_underscored",
        child_entity_ids={
            "off_peak": "sensor.grid_off_peak",
            "peak": "sensor.grid_peak",
        },
    )
    defaults: dict[str, Any] = {}
    with _patch_manager(_grid_prefs(consumption="sensor.grid_total")):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["day_consumption_kwh"] == "sensor.grid_peak"
    assert defaults["night_consumption_kwh"] == "sensor.grid_off_peak"


def _add_yaml_utility_meter(
    hass: HomeAssistant,
    *,
    source: str,
    children: dict[str, str],
) -> None:
    """Register fake YAML-rooted utility_meter children: entity-registry
    entries with platform=utility_meter and no config_entry_id, plus
    states carrying the runtime ``source`` and ``tariff`` attributes
    the YAML platform sets."""
    ent_reg = er.async_get(hass)
    for tariff, entity_id in children.items():
        ent_reg.async_get_or_create(
            domain="sensor",
            platform="utility_meter",
            unique_id=f"yaml_{tariff}",
            suggested_object_id=entity_id.removeprefix("sensor."),
        )
        hass.states.async_set(
            entity_id,
            "0",
            {"source": source, "tariff": tariff},
        )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_utility_meter_yaml_helper_fills_day_night_registers(
    hass: HomeAssistant,
) -> None:
    """YAML-configured utility_meter helpers don't appear under
    config_entries.async_entries('utility_meter') -- they live in the
    entity registry with platform=utility_meter and a runtime
    ``source`` attribute on each child sensor. Picker must walk both
    paths so older YAML installs aren't second-class."""
    _add_yaml_utility_meter(
        hass,
        source="sensor.electricity_meter_total",
        children={
            "peak": "sensor.yaml_consumption_peak",
            "offpeak": "sensor.yaml_consumption_offpeak",
        },
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["consumption_kwh"] == "sensor.electricity_meter_total"
    assert defaults["day_consumption_kwh"] == "sensor.yaml_consumption_peak"
    assert defaults["night_consumption_kwh"] == "sensor.yaml_consumption_offpeak"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_utility_meter_with_unrecognised_tariffs_skips_day_night(
    hass: HomeAssistant,
) -> None:
    """A utility_meter helper that uses tariff names we can't map
    (e.g. high/low) must NOT pre-fill the day/night fields. Picking
    the wrong slot would mis-bill year-cost; we'd rather leave it to
    the user than guess."""
    _add_utility_meter_entry(
        hass,
        source="sensor.electricity_meter_total",
        tariffs=["high", "low"],
        entry_id="um_unknown",
        child_entity_ids={
            "high": "sensor.consumption_high",
            "low": "sensor.consumption_low",
        },
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_defaults(hass, defaults)
    assert defaults["consumption_kwh"] == "sensor.electricity_meter_total"
    assert "day_consumption_kwh" not in defaults
    assert "night_consumption_kwh" not in defaults


def _add_integration_helper(
    hass: HomeAssistant,
    *,
    source_kw: str,
    output_kwh: str,
    entry_id: str,
) -> None:
    """Register a fake Riemann integration helper turning a kW power
    sensor into a kWh energy sensor, the typical bridge between a P1
    power reading and the Energy dashboard."""
    entry = MockConfigEntry(
        domain="integration",
        entry_id=entry_id,
        data={},
        options={"source": source_kw},
    )
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)
    ent_reg.async_get_or_create(
        domain="sensor",
        platform="integration",
        unique_id=entry_id,
        config_entry=entry,
        suggested_object_id=output_kwh.removeprefix("sensor."),
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_walks_integration_helper_to_kw_source(
    hass: HomeAssistant,
) -> None:
    """The Energy dashboard tracks kWh; the capacity tariff needs kW.
    When the dashboard sensor was produced by a Riemann integration
    helper, the helper's source is the kW reading we want."""
    _add_integration_helper(
        hass,
        source_kw="sensor.electricity_meter_power",
        output_kwh="sensor.electricity_meter_total",
        entry_id="riemann_kwh",
    )
    hass.states.async_set(
        "sensor.electricity_meter_power",
        "1234",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert defaults["capacity_peak_sensor"] == "sensor.electricity_meter_power"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_skips_when_no_integration_helper(
    hass: HomeAssistant,
) -> None:
    """Users with a native kWh sensor (no Riemann helper) keep the
    capacity peak sensor blank -- there's no automatic way to derive
    a kW source from a native kWh reading."""
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.native_kwh")
    with _patch_manager(prefs):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert "capacity_peak_sensor" not in defaults


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_does_not_override_existing_choice(
    hass: HomeAssistant,
) -> None:
    """OptionsFlow seeds defaults with whatever the user saved. The
    capacity helper must not blow that away."""
    _add_integration_helper(
        hass,
        source_kw="sensor.dashboard_power",
        output_kwh="sensor.electricity_meter_total",
        entry_id="riemann_kwh2",
    )
    defaults: dict[str, Any] = {"capacity_peak_sensor": "sensor.user_pick"}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert defaults["capacity_peak_sensor"] == "sensor.user_pick"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_rejects_non_power_riemann_source(
    hass: HomeAssistant,
) -> None:
    """A Riemann integration helper can in principle be fed any numeric
    sensor (flow rate, m3 of gas, etc.). Refuse to pre-fill the
    capacity peak from a source that isn't a power sensor: a wrong
    pre-fill is one click away from issue #19 again."""
    _add_integration_helper(
        hass,
        source_kw="sensor.gas_flow",
        output_kwh="sensor.electricity_meter_total",
        entry_id="riemann_kwh3",
    )
    hass.states.async_set(
        "sensor.gas_flow",
        "0.5",
        {"device_class": "gas", "unit_of_measurement": "m³/h"},
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert "capacity_peak_sensor" not in defaults


def _add_dsmr_monthly_peak(
    hass: HomeAssistant, *, entity_id: str = "sensor.p1_maximum_demand_current_month"
) -> str:
    """Register the entity HA's dsmr integration creates for a DSMR 5B meter."""
    registry = er.async_get(hass)
    return registry.async_get_or_create(
        "sensor",
        "dsmr",
        "1234_belgium_maximum_demand_current_month",
        translation_key="maximum_demand_current_month",
        suggested_object_id=entity_id.removeprefix("sensor."),
    ).entity_id


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_prefers_the_meters_own_monthly_peak(
    hass: HomeAssistant,
) -> None:
    """A DSMR 5B meter publishes the quarter-hour peak Fluvius actually bills.
    That beats the Riemann source, which is instantaneous power the coordinator
    can only sample hourly, so it must win even when both are available."""
    peak_id = _add_dsmr_monthly_peak(hass)
    _add_integration_helper(
        hass,
        source_kw="sensor.electricity_meter_power",
        output_kwh="sensor.electricity_meter_total",
        entry_id="riemann_kwh",
    )
    hass.states.async_set(
        "sensor.electricity_meter_power",
        "1234",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    defaults: dict[str, Any] = {}
    prefs = _grid_prefs(consumption="sensor.electricity_meter_total")
    with _patch_manager(prefs):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert defaults["capacity_peak_sensor"] == peak_id


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_keeps_a_manual_pick_over_the_meter_peak(
    hass: HomeAssistant,
) -> None:
    """The meter preference must not override a choice the user already made."""
    _add_dsmr_monthly_peak(hass)
    defaults: dict[str, Any] = {"capacity_peak_sensor": "sensor.user_pick"}
    with _patch_manager(_grid_prefs(consumption="sensor.whatever")):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert defaults["capacity_peak_sensor"] == "sensor.user_pick"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_capacity_pre_fill_ignores_a_disabled_meter_peak(
    hass: HomeAssistant,
) -> None:
    """A disabled entity has no state, so falling back to the Riemann walk
    beats pre-filling something that will never report."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        "sensor",
        "dsmr",
        "1234_belgium_maximum_demand_current_month",
        translation_key="maximum_demand_current_month",
        suggested_object_id="p1_maximum_demand_current_month",
    )
    registry.async_update_entity(
        entry.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    _add_integration_helper(
        hass,
        source_kw="sensor.electricity_meter_power",
        output_kwh="sensor.electricity_meter_total",
        entry_id="riemann_kwh",
    )
    hass.states.async_set(
        "sensor.electricity_meter_power",
        "1234",
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    defaults: dict[str, Any] = {}
    with _patch_manager(_grid_prefs(consumption="sensor.electricity_meter_total")):
        await _apply_energy_manager_capacity_default(hass, defaults)
    assert defaults["capacity_peak_sensor"] == "sensor.electricity_meter_power"


def test_half_wired_register_pair_is_rejected() -> None:
    """A day/night pair only works as a pair: the coordinator gives up on a
    half-wired one and current_year_cost collapses to the fees-only floor
    with no error, no repair and nothing in the log a user would see."""
    from custom_components.be_electricity_prices.config_flow import (
        _incomplete_register_pairs,
    )

    assert _incomplete_register_pairs({}) == {}
    both = {
        const.CONF_DAY_CONSUMPTION_KWH: "sensor.d",
        const.CONF_NIGHT_CONSUMPTION_KWH: "sensor.n",
    }
    assert _incomplete_register_pairs(both) == {}
    assert _incomplete_register_pairs({const.CONF_DAY_CONSUMPTION_KWH: "sensor.d"}) == {
        const.CONF_NIGHT_CONSUMPTION_KWH: "register_pair_incomplete"
    }
    assert _incomplete_register_pairs({const.CONF_NIGHT_INJECTION_KWH: "sensor.n"}) == {
        const.CONF_NIGHT_INJECTION_KWH: "register_pair_incomplete"
    }
    # Both sides half-wired reports both.
    assert (
        len(
            _incomplete_register_pairs(
                {
                    const.CONF_DAY_CONSUMPTION_KWH: "sensor.d",
                    const.CONF_DAY_INJECTION_KWH: "sensor.di",
                }
            )
        )
        == 2
    )


def test_the_meters_form_and_the_coordinator_share_one_wiring_rule() -> None:
    """The form must refuse exactly what the coordinator cannot bill.

    A totals sensor rescues a half-wired register pair: the odd half is
    ignored and the side bills off the total. The form used to refuse that
    anyway, which locked an entry that had always billed correctly out of its
    own options flow over a field that never affected its bill.
    """
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.config_flow import (
        _incomplete_register_pairs,
    )
    from custom_components.be_electricity_prices.coordinator import (
        _hourly_consumption_sensors,
        _hourly_injection_sensors,
    )

    cases = [
        {const.CONF_CONSUMPTION_KWH: "sensor.total"},
        {
            const.CONF_DAY_CONSUMPTION_KWH: "sensor.d",
            const.CONF_NIGHT_CONSUMPTION_KWH: "sensor.n",
        },
        {
            const.CONF_CONSUMPTION_KWH: "sensor.total",
            const.CONF_DAY_CONSUMPTION_KWH: "sensor.d",
        },
        {
            const.CONF_CONSUMPTION_KWH: "sensor.total",
            const.CONF_NIGHT_CONSUMPTION_KWH: "sensor.n",
        },
        {
            const.CONF_INJECTION_KWH: "sensor.it",
            const.CONF_DAY_INJECTION_KWH: "sensor.di",
        },
        {const.CONF_DAY_CONSUMPTION_KWH: "sensor.d"},
        {const.CONF_NIGHT_INJECTION_KWH: "sensor.ni"},
    ]
    for data in cases:
        entry = SimpleNamespace(data=data)
        billable = bool(
            _hourly_consumption_sensors(entry)  # type: ignore[arg-type]
            or _hourly_injection_sensors(entry)  # type: ignore[arg-type]
        )
        refused = bool(_incomplete_register_pairs(data))
        assert refused is not billable, data
