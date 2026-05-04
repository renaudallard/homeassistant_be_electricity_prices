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

from typing import Any
from unittest.mock import AsyncMock, patch

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


async def _enter_edit_branch(hass: HomeAssistant, entry: MockConfigEntry) -> dict:
    """Open OptionsFlow and select the 'edit' branch from the init menu.

    The menu is the new top-level surface that gates the existing
    edit flow vs the one-off compare quote. Returns the form result
    for the supplier+region step (step_id="edit"), which existing
    tests then drive as before.
    """
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "edit"
    return result


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_walks_every_step(hass: HomeAssistant) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)

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
    # Then the meters step (current_year_cost inputs); skipped here.
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

    result = await _enter_edit_branch(hass, entry)
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

    result = await _enter_edit_branch(hass, entry)
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

    result = await _enter_edit_branch(hass, entry)
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


# ---- compare-another-supplier branch ---------------------------------------


def _real_coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, snapshot: Any, peak_kw: float = 2.5
) -> Any:
    """A real BePricesCoordinator instance with attributes pre-set so the
    compare flow can read snapshot / peak_kw / spot cache without a
    real refresh tick. The compare path uses isinstance against the
    real class, so a SimpleNamespace doesn't suffice."""
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = snapshot
    coord._peak_kw = peak_kw
    coord._spot_cache = {}
    return coord


def _stub_snapshot(supplier: str, contract: str, single_rate: float) -> Any:
    """Minimal SupplierSnapshot the compare flow can run compute_breakdown
    on. Walloon DSO with a typical distribution / transport / tax stack
    so the all-in number is in a realistic range without depending on
    fixture PDFs."""
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
        SupplierSnapshot,
        TaxOverlay,
    )

    return SupplierSnapshot(
        supplier=supplier,
        contract=contract,
        energy=FixedRates(single=single_rate, yearly_fixed_fee=60.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
            )
        },
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://stub",
        publication_label="april 2026",
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_quotes_against_other_supplier(
    hass: HomeAssistant,
) -> None:
    """Picking 'compare' from the menu walks supplier -> contract ->
    result. The result form's description placeholders carry both the
    per-kWh and the projected annual bill for both suppliers."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )

    other_snap = _stub_snapshot("cociter", "cociter_variable", 0.16)

    # SupplierExtractor is a frozen dataclass, so we can't patch its
    # .fetch directly. Replace the registry entry with a clone whose
    # fetch returns our stub snapshot, and put it back on tear-down.
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    cociter_ext = EXTRACTORS["cociter"]
    fake_cociter = replace(cociter_ext, fetch=AsyncMock(return_value=other_snap))
    with patch.dict(EXTRACTORS, {"cociter": fake_cociter}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] == data_entry_flow.FlowResultType.MENU
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "compare"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "cociter"}
        )
        assert result["step_id"] == "compare_contract"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "cociter_variable"}
        )
        # Static contracts now ask for the meter type; default to mono.
        assert result["step_id"] == "compare_meter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_result"
        ph = result["description_placeholders"]
        assert ph["current_supplier"] == "Eneco"
        assert ph["compare_supplier"] == "Cociter"
        # Per-kWh non-trivial: stub eneco at 0.18 EUR/kWh + DSO + taxes;
        # stub cociter at 0.16 EUR/kWh same overlay.
        assert ph["current_per_kwh"] != "-"
        assert ph["compare_per_kwh"] != "-"
        assert float(ph["compare_per_kwh"]) < float(ph["current_per_kwh"])
        # Annual bill = per_kwh * 3500 + yearly_fixed_fee + ... ; cociter
        # cheaper energy => lower annual.
        assert float(ph["compare_annual"]) < float(ph["current_annual"])
        # Sign convention: delta = other - current; cociter < eneco => negative
        assert ph["delta_annual"].startswith("-")
        # Submitting the (empty) result form ends the flow without saving.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {}
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "compare_done"
    # Entry data must be untouched by the compare flow.
    assert entry.data["supplier"] == "eneco"
    assert entry.data["contract"] == "power_fix"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_filters_to_same_kind(hass: HomeAssistant) -> None:
    """A user on a static contract must NOT see dynamic-only suppliers
    in the compare picker, otherwise we'd quote a static-vs-dynamic
    pair and either fabricate spot data or show the supplier's monthly
    indicative as if it were today's price."""
    from custom_components.be_electricity_prices.config_flow import (
        _compare_supplier_options,
    )

    # Eneco offers both fixed and dynamic. From a static-eneco user's
    # perspective, compatible alternatives must include other static
    # suppliers; the picker is keyed on suppliers having at least one
    # same-kind contract. Eneco's own contracts include dynamic, but
    # dynamic-only suppliers (none in current registry) would be
    # excluded. Sanity-check that the function shape works and that
    # eneco's static-side compatibility includes typical Walloon
    # static suppliers.
    static_options = _compare_supplier_options("wallonia", "fixed")
    static_ids = {o["value"] for o in static_options}
    assert "eneco" in static_ids
    assert "cociter" in static_ids

    # From a dynamic user's perspective, only suppliers with a
    # dynamic-kind contract show up.
    dynamic_options = _compare_supplier_options("wallonia", "dynamic")
    dynamic_ids = {o["value"] for o in dynamic_options}
    # Eneco has power_dynamic.
    assert "eneco" in dynamic_ids
    # Sanity: no overlap-by-name with non-dynamic-only suppliers
    # depends on the registry; we only assert the function honours
    # the kind boundary.
    for sid in dynamic_ids:
        from custom_components.be_electricity_prices.providers import (
            get as get_extractor,
        )

        kinds = {c.kind for c in get_extractor(sid).contracts}
        assert "dynamic" in kinds


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_aborts_when_no_alternative(
    hass: HomeAssistant,
) -> None:
    """If the picked region+kind has no compatible supplier (degenerate
    case after a registry change), the compare flow aborts cleanly
    rather than rendering an empty dropdown the user can't submit."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )

    with patch(
        "custom_components.be_electricity_prices.config_flow._compare_supplier_options",
        return_value=[],
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.ABORT
        assert result["reason"] == "compare_no_alternative"
