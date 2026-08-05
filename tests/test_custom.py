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

"""Expert custom-formula supplier: config flow, snapshot build, pricing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.config_flow import (
    _compare_supplier_options,
    _custom_dso_schema,
    _custom_energy_schema,
)
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
    _bake_monthly_injection,
    _floor_injection,
    _mean_of_month,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.custom import (
    EXTRACTOR,
    build_snapshot,
)

WHEN = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# ---- build_snapshot ----------------------------------------------------------


def test_build_snapshot_dynamic() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_DYNAMIC,
        const.CONF_CUSTOM_ENERGY_FACTOR: 1.0,
        const.CONF_CUSTOM_ENERGY_BASE: 0.02,
        const.CONF_CUSTOM_ENERGY_QUARTER_HOURLY: True,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
        const.CONF_CUSTOM_TAX_FEDERAL_EXCISE: 0.005,
        const.CONF_CUSTOM_VAT_RATE: 0.06,
    }
    snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == 1.0 and snap.energy.base == 0.02
    assert snap.energy.quarter_hourly is True
    assert snap.dsos[const.DSO_FLUVIUS_ANTWERPEN].distribution_single == 0.05
    assert snap.taxes.federal_excise == 0.005
    assert snap.taxes.vat_rate == 0.06
    assert snap.injection is None  # no injection regime


def test_build_snapshot_monthly_with_injection() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
        const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
        const.CONF_CUSTOM_ENERGY_BASE: 0.0,
        const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
        const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
        const.CONF_CUSTOM_INJECTION_BASE: -0.009,
        const.CONF_CUSTOM_INJECTION_FLOOR: True,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
    }
    snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == 1.0834
    assert snap.injection is not None
    assert snap.injection.factor == 0.96 and snap.injection.base == -0.009
    assert snap.injection.floor_at_zero is True
    # spot-monthly energy prices to factor * mean + base
    assert energy_eur_per_kwh(snap.energy, WHEN, 0.08) == pytest.approx(1.0834 * 0.08)


def test_build_snapshot_fixed_routes_regional_renewables() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
        const.CONF_CUSTOM_TAX_REGIONAL_RENEWABLES: 0.031,
    }
    snap = build_snapshot(data, const.REGION_WALLONIA, const.DSO_ORES)
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == 0.30
    # the single renewables field lands in the region's slot
    assert snap.taxes.wallonia_renewables == 0.031
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.brussels_renewables == 0.0


def test_build_snapshot_brussels_osp_tier() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CONNECTION_KVA_TIER: const.CONNECTION_KVA_TIER_LE13,
        const.CONF_CUSTOM_DSO_BRUSSELS_OSP: 42.0,
        const.CONF_CUSTOM_VAT_RATE: 0.0,  # isolate the tier routing from the VAT bake
    }
    snap = build_snapshot(data, const.REGION_BRUSSELS, const.DSO_SIBELGA)
    osp = snap.dsos[const.DSO_SIBELGA].brussels_osp_by_tier
    assert osp == {const.CONNECTION_KVA_TIER_LE13: 42.0}


# ---- helpers -----------------------------------------------------------------


def test_mean_of_month_filters_by_local_month() -> None:
    spots = {
        datetime(2026, 7, 1, 10, tzinfo=UTC): 0.10,
        datetime(2026, 7, 2, 10, tzinfo=UTC): 0.20,
        datetime(2026, 6, 30, 10, tzinfo=UTC): 99.0,  # excluded
    }
    assert _mean_of_month(spots, 2026, 7) == pytest.approx(0.15)
    assert _mean_of_month(spots, 2026, 5) is None


def test_bake_monthly_injection_and_floor() -> None:
    snap = build_snapshot(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
        const.REGION_FLANDERS,
        const.DSO_FLUVIUS_ANTWERPEN,
    )
    # mean 0.005 -> 0.96*0.005 - 0.009 = -0.0042, baked into a flat current
    baked = _bake_monthly_injection(snap, 0.005)
    assert baked.injection is not None
    assert baked.injection.factor is None and baked.injection.base is None
    raw = baked.injection.current
    assert raw == pytest.approx(0.96 * 0.005 - 0.009)
    # floor clamps the negative to 0
    assert _floor_injection(raw, baked.injection) == 0.0
    # a cold-start None mean bakes to None (injection unavailable this tick)
    cold = _bake_monthly_injection(snap, None)
    assert cold.injection is not None
    assert cold.injection.current is None


def test_floor_injection_passthrough_without_flag() -> None:
    inj = InjectionRates(current=-0.001, floor_at_zero=False)
    assert _floor_injection(-0.001, inj) == -0.001
    assert _floor_injection(None, inj) is None


# ---- serialization -----------------------------------------------------------


def test_spot_monthly_snapshot_round_trips() -> None:
    snap = build_snapshot(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
        const.REGION_FLANDERS,
        const.DSO_FLUVIUS_ANTWERPEN,
    )
    restored = _snapshot_from_dict(_snapshot_to_dict(snap, WHEN))
    assert isinstance(restored.energy, SpotMonthlyRates)
    assert restored.energy.factor == 1.0834
    assert restored.injection is not None
    assert restored.injection.floor_at_zero is True


# ---- provider registry -------------------------------------------------------


async def test_custom_fetch_stub_raises() -> None:
    with pytest.raises(ExtractorError):
        await EXTRACTOR.fetch(None, const.CUSTOM_CONTRACT_DYNAMIC, "flanders")  # type: ignore[arg-type]


def test_custom_excluded_from_compare_targets() -> None:
    options = _compare_supplier_options(const.REGION_FLANDERS, "dynamic")
    assert const.SUPPLIER_CUSTOM not in {o["value"] for o in options}


def test_custom_listed_last_in_supplier_dropdown() -> None:
    from custom_components.be_electricity_prices.config_flow import _supplier_options

    values = [o["value"] for o in _supplier_options()]
    assert values[-1] == const.SUPPLIER_CUSTOM


# ---- withdrawn suppliers -----------------------------------------------------


def test_withdrawn_supplier_not_offered_to_new_setups() -> None:
    from custom_components.be_electricity_prices.config_flow import _supplier_options

    assert "dats24" not in {o["value"] for o in _supplier_options()}
    assert "dats24" not in {
        o["value"] for o in _supplier_options(const.REGION_FLANDERS)
    }


def test_withdrawn_supplier_still_editable_on_an_existing_entry() -> None:
    """The load-bearing half: a SelectSelector rejects a default that is not
    among its options, so an entry already on a withdrawn supplier would
    become impossible to edit if the filter had no ``keep`` escape hatch."""
    from custom_components.be_electricity_prices.config_flow import _supplier_options

    assert "dats24" in {o["value"] for o in _supplier_options(keep="dats24")}
    # keep= is an exception for one entry, not a global switch-off.
    assert "dats24" not in {o["value"] for o in _supplier_options(keep="eneco")}


def test_withdrawn_supplier_not_a_comparison_target() -> None:
    for region in (const.REGION_FLANDERS, const.REGION_WALLONIA):
        for kind in ("variable", "dynamic"):
            assert "dats24" not in {
                o["value"] for o in _compare_supplier_options(region, kind)
            }


# ---- coordinator: flat monthly live table ------------------------------------


def _monthly_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=const.DOMAIN,
        data={
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_REGION: const.REGION_FLANDERS,
            const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN,
            const.CONF_METER: const.METER_MONO,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
            const.CONF_CUSTOM_ENERGY_BASE: 0.0,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
            const.CONF_CUSTOM_VAT_RATE: 0.06,
        },
        title="Custom monthly",
    )


async def test_build_hourly_spot_monthly_is_flat(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Every slot of the live table bills the same flat monthly rate."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _monthly_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = build_snapshot(
        dict(entry.data), const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN
    )
    hourly = coord._build_hourly(coord._snapshot, {}, 0.08)
    all_in = {bd.all_in for bd in hourly.values()}
    assert len(hourly) >= 24
    assert len(all_in) == 1  # perfectly flat
    # (energy 1.0834*0.08 + distribution 0.05) * 1.06 VAT
    expected = (1.0834 * 0.08 + 0.05) * 1.06
    assert next(iter(all_in)) == pytest.approx(expected)


async def test_build_hourly_spot_monthly_empty_without_mean(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No mean yet (cold start) leaves the table empty, not crashing."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _monthly_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = build_snapshot(
        dict(entry.data), const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN
    )
    assert coord._build_hourly(coord._snapshot, {}, None) == {}


# ---- config-flow walks -------------------------------------------------------


@pytest.fixture
def _no_setup() -> Any:
    """A completed config flow makes HA set the entry up, which spins a
    coordinator that needs a recorder. Stub it out for the flow walks."""
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        yield


def _mock_key() -> Any:
    return patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value=None,
    )


async def _start(hass: HomeAssistant, supplier: str, region: str) -> Any:
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {const.CONF_SUPPLIER: supplier, const.CONF_REGION: region},
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_dynamic_flanders(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Dynamic custom reaches the api-key step and collects the formula."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_FLANDERS)
    assert result["step_id"] == "contract"
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_DYNAMIC})
    assert result["step_id"] == "dso"
    result = await cfg(flow, {const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN})
    assert result["step_id"] == "meter"
    result = await cfg(flow, {const.CONF_METER: const.METER_DYNAMIC})
    assert result["step_id"] == "api_key"  # dynamic gates the key
    with _mock_key():
        result = await cfg(flow, {const.CONF_API_KEY: "k"})
    assert result["step_id"] == "custom_energy"
    result = await cfg(
        flow,
        {
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0,
            const.CONF_CUSTOM_ENERGY_BASE: 0.02,
            const.CONF_CUSTOM_ENERGY_QUARTER_HOURLY: False,
            const.CONF_CUSTOM_YEARLY_FIXED_FEE: 60.0,
        },
    )
    assert result["step_id"] == "capacity"  # Flanders
    result = await cfg(
        flow,
        {
            const.CONF_CAPACITY_MODE: const.CAPACITY_MODE_FIXED,
            const.CONF_CAPACITY_FIXED_KW: 4.0,
        },
    )
    assert result["step_id"] == "solar"
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05})
    assert result["step_id"] == "custom_tax"
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_ENERGY_FACTOR] == 1.0
    assert result["data"][const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE] == 0.05


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_monthly_injection(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Monthly custom also gates the key and inserts the injection step."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_FLANDERS)
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY})
    result = await cfg(flow, {const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN})
    result = await cfg(flow, {const.CONF_METER: const.METER_MONO})
    assert result["step_id"] == "api_key"  # spot_monthly gates the key too
    with _mock_key():
        result = await cfg(flow, {const.CONF_API_KEY: "k"})
    assert result["step_id"] == "custom_energy"
    result = await cfg(
        flow,
        {const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834, const.CONF_CUSTOM_ENERGY_BASE: 0.0},
    )
    result = await cfg(
        flow,
        {
            const.CONF_CAPACITY_MODE: const.CAPACITY_MODE_FIXED,
            const.CONF_CAPACITY_FIXED_KW: 4.0,
        },
    )
    assert result["step_id"] == "solar"
    result = await cfg(
        flow,
        {
            const.CONF_SOLAR_KVA: 5.0,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        },
    )
    assert result["step_id"] == "custom_injection"
    result = await cfg(
        flow,
        {
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05})
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_INJECTION_FLOOR] is True


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_fixed_wallonia_no_api_key(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Fixed custom skips the api-key step (no spot needed)."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_WALLONIA)
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED})
    result = await cfg(flow, {const.CONF_DSO: const.DSO_ORES})
    result = await cfg(flow, {const.CONF_METER: const.METER_MONO})
    assert result["step_id"] == "dso_tariff_mode"  # Wallonia
    result = await cfg(flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_SIMPLE})
    assert result["step_id"] == "custom_energy"  # no api-key step for fixed
    result = await cfg(flow, {const.CONF_CUSTOM_ENERGY_SINGLE: 0.30})
    assert result["step_id"] == "solar"  # no capacity outside Flanders
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.06})
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_ENERGY_SINGLE] == 0.30


def test_custom_dso_schema_offers_the_bihourly_split_to_a_dynamic_meter() -> None:
    """A dynamic / TOU contract FORCES METER_DYNAMIC (`_meter_schema`), and
    `pricing.network_eur_per_kwh` routes both `bi` and `dynamic` through
    `distribution_peak` / `distribution_offpeak` whenever the DSO mode is not
    "simple". Gating the two boxes on METER_BI alone meant a custom dynamic
    entry could never supply the rates its own network leg is billed on, so
    every hour silently fell back to `distribution_single`."""
    base = {
        const.CONF_REGION: const.REGION_WALLONIA,
        const.CONF_DSO_TARIFF_MODE: "bi_horaire",
    }

    def keys(meter: str) -> set[str]:
        return {
            str(k) for k in _custom_dso_schema({**base, const.CONF_METER: meter}).schema
        }

    for meter in (const.METER_BI, const.METER_DYNAMIC):
        got = keys(meter)
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK in got, meter
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK in got, meter

    # Meters that are never routed through the split must not be asked for it.
    for meter in (const.METER_MONO, const.METER_EXCLUSIVE_NIGHT):
        got = keys(meter)
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK not in got, meter
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK not in got, meter


def test_custom_energy_schema_offers_the_day_night_split_to_a_dynamic_meter() -> None:
    """`pricing.energy_eur_per_kwh` sets `bi_capable = meter in ("bi",
    "dynamic")` and routes both through `peak` / `offpeak`, so a custom fixed
    contract on a smart meter must be able to enter them. Gating on METER_BI
    alone left both None and billed all 24 hours at the single rate -- the
    same root cause as the DSO step, and together they cost such an entry both
    the supplier and the network day/night split."""
    base = {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED}

    def keys(meter: str) -> set[str]:
        return {
            str(k)
            for k in _custom_energy_schema({**base, const.CONF_METER: meter}).schema
        }

    for meter in (const.METER_BI, const.METER_DYNAMIC):
        got = keys(meter)
        assert const.CONF_CUSTOM_ENERGY_PEAK in got, meter
        assert const.CONF_CUSTOM_ENERGY_OFFPEAK in got, meter
    mono = keys(const.METER_MONO)
    assert const.CONF_CUSTOM_ENERGY_PEAK not in mono
    assert const.CONF_CUSTOM_ENERGY_OFFPEAK not in mono
