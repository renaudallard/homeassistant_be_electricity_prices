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

from datetime import UTC, datetime
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
async def test_compare_branch_supplier_picker_lists_all_in_region(
    hass: HomeAssistant,
) -> None:
    """The compare flow now allows cross-kind quotes (static <->
    dynamic), so the supplier picker is filtered only by region and
    by 'has at least one contract here'. The kind switch happens at
    the contract picker (via _compare_contract_schema) and the
    api_key step kicks in when the user crosses into dynamic
    territory without a saved key."""
    from custom_components.be_electricity_prices.config_flow import (
        _compare_supplier_options,
    )

    # Static-side caller still gets every Walloon supplier.
    static_options = _compare_supplier_options("wallonia", "fixed")
    static_ids = {o["value"] for o in static_options}
    assert "eneco" in static_ids
    assert "cociter" in static_ids
    # Dynamic-side caller gets the same set: cross-kind is allowed.
    dynamic_options = _compare_supplier_options("wallonia", "dynamic")
    dynamic_ids = {o["value"] for o in dynamic_options}
    assert dynamic_ids == static_ids


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_static_to_dynamic_prompts_for_api_key(
    hass: HomeAssistant,
) -> None:
    """A static-contract user comparing against a dynamic contract
    needs an ENTSO-E spot for the dynamic side. When their entry has
    no api_key yet, the compare flow detours through compare_api_key
    after the contract pick (meter is auto-locked to dynamic)."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        DynamicRates,
        DsoOverlay,
        InjectionRates,
        SupplierSnapshot,
        TaxOverlay,
    )

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = SupplierSnapshot(
        supplier="cociter",
        contract="cociter_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0, yearly_fixed_fee=60.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
            )
        },
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        injection=InjectionRates(current=0.05),
        source_url="test://stub",
        publication_label="april 2026",
    )
    fake = replace(EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap))
    with patch.dict(EXTRACTORS, {"cociter": fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        assert result["step_id"] == "compare"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "cociter"}
        )
        assert result["step_id"] == "compare_contract"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "cociter_dynamic"}
        )
        # Dynamic locks the meter to dynamic and skips compare_meter,
        # then routes to compare_api_key because the static entry has
        # no saved api_key.
        assert result["step_id"] == "compare_api_key"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"api_key": "valid-token"}
        )
        # _validate_entsoe_key is auto-bypassed by the test fixture; the
        # next step is the result page.
        assert result["step_id"] == "compare_result"


async def _drive_compare(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    other_snap: Any,
    other_supplier: str = "cociter",
    other_contract: str = "cociter_variable",
    meter: str = "mono",
) -> dict[str, str]:
    """Walk the compare flow end-to-end and return the result form's
    description placeholders. Replaces the alternative supplier's
    fetch with a stub returning ``other_snap`` (SupplierExtractor is
    a frozen dataclass, so we swap the registry entry instead of
    patching .fetch directly)."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    fake = replace(EXTRACTORS[other_supplier], fetch=AsyncMock(return_value=other_snap))
    with patch.dict(EXTRACTORS, {other_supplier: fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": other_supplier}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": other_contract}
        )
        if result["step_id"] == "compare_meter":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"meter": meter}
            )
        assert result["step_id"] == "compare_result"
    return result["description_placeholders"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_uses_measured_rolling_year_kwh(
    hass: HomeAssistant,
) -> None:
    """When a consumption sensor is configured and the recorder has
    history, the annual estimate must use the measured rolling-year
    kWh instead of the 3500 kWh fallback."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "consumption_kwh": "sensor.house_total",
        },
        title="Eneco - Wallonia",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = _stub_snapshot("cociter", "cociter_variable", 0.16)

    measured_rolling = 7000.0  # double the 3500 default; isolates the path
    measured_ytd = 2400.0

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id != "sensor.house_total":
            return {}
        # Compress the period total into a single synthetic day so the
        # caller's sum() picks it up. The compare path scopes by
        # (rolling_year_start vs jan1) so we can branch on the gap.
        delta = (end - start).days
        if delta >= 360:
            return {start: measured_rolling}
        return {start: measured_ytd}

    with patch(
        "custom_components.be_electricity_prices.coordinator._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(hass, entry, other_snap=other_snap)
    # 7000 kWh, not 3500.
    assert ph["annual_kwh"] == "7000"
    assert ph["ytd_kwh"] == "2400"
    assert "measured" in ph["consumption_source"]
    # Bar chart placeholders are populated with both supplier labels
    # and unicode block characters; the result page renders them as a
    # side-by-side visual.
    assert "Eneco" in ph["annual_chart"]
    assert "Cociter" in ph["annual_chart"]
    assert "█" in ph["annual_chart"]
    # Annual at 7000 kWh > annual at 3500 kWh, sanity check the helper
    # actually used the measured value (compare_annual is rate * 7000
    # + fees, which for cociter@0.16 alone is > 1000 EUR).
    assert float(ph["compare_annual"]) > 1000.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_compensation_regime_nets_consumption(
    hass: HomeAssistant,
) -> None:
    """Walloon compensation regime users have their meter netted 1:1
    on consumption vs injection. The compare quote must reflect that:
    a household consuming 5000 kWh and injecting 5000 kWh pays for
    roughly zero net energy + fees, not 5000 kWh worth."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "consumption_kwh": "sensor.cons",
            "injection_kwh": "sensor.inj",
            "solar_regime": "compensation",
            "solar_kva": 5.0,
        },
        title="Eneco - Wallonia compensation",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = _stub_snapshot("cociter", "cociter_variable", 0.16)

    # Equal consumption and injection -> netted to 0 billable kWh; the
    # bill collapses to fees only.
    cons = 5000.0
    inj = 5000.0

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return {start: cons}
        if entity_id == "sensor.inj":
            return {start: inj}
        return {}

    with patch(
        "custom_components.be_electricity_prices.coordinator._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(hass, entry, other_snap=other_snap)
    # Per-kWh × annual_kwh is zero (netted), so the annual bill equals
    # the fees-only floor. For the stub eneco snapshot fees are
    # yearly_fixed_fee=60 + energy_fund=0 + capacity=0 + prosumer (no
    # prosumer_eur_per_kva_year on the stub DSO) = 60 EUR. Same for
    # cociter. The delta should be ~0.
    assert abs(float(ph["compare_annual"]) - 60.0) < 1.0
    assert abs(float(ph["current_annual"]) - 60.0) < 1.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_injection_regime_credits_injection_price(
    hass: HomeAssistant,
) -> None:
    """Injection regime users get a per-kWh credit for energy fed to
    the grid at each supplier's printed injection_price. The annual
    bill for the alternative must subtract that credit, so a
    higher-credit supplier shows a lower bill even at the same
    consumption rate."""
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
        SupplierSnapshot,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "consumption_kwh": "sensor.cons",
            "injection_kwh": "sensor.inj",
            "solar_regime": "injection",
            "solar_kva": 5.0,
        },
        title="Eneco - Wallonia injection",
    )
    entry.add_to_hass(hass)

    # Equal energy rates so the only difference is the injection
    # credit.
    current_snap = _stub_snapshot("eneco", "power_fix", 0.20)
    object.__setattr__(
        current_snap, "injection", InjectionRates(current=0.05)
    )  # 5 c€/kWh credited
    other_snap = SupplierSnapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        dsos=current_snap.dsos,
        taxes=current_snap.taxes,
        injection=InjectionRates(current=0.10),  # higher credit
        source_url="test://stub",
        publication_label="april 2026",
    )
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    cons = 5000.0
    inj = 4000.0

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return {start: cons}
        if entity_id == "sensor.inj":
            return {start: inj}
        return {}

    with patch(
        "custom_components.be_electricity_prices.coordinator._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(hass, entry, other_snap=other_snap)
    # Both suppliers price energy the same; alternative credits 0.10
    # vs current 0.05. Difference = (0.10 - 0.05) * 4000 = 200 EUR
    # cheaper for the alternative.
    diff = float(ph["current_annual"]) - float(ph["compare_annual"])
    assert abs(diff - 200.0) < 1.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_meter_override_changes_per_kwh(
    hass: HomeAssistant,
) -> None:
    """The compare flow lets static-contract users override the meter
    type. Picking 'bi' must route compute_breakdown through the
    peak/offpeak rates, producing a different per-kWh number than
    the user's mono setup would."""
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
        SupplierSnapshot,
        TaxOverlay,
    )

    # Snapshot with distinct peak / offpeak rates so meter=bi yields a
    # different per-kWh than meter=mono.
    bi_aware_snap = SupplierSnapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=FixedRates(single=0.20, peak=0.25, offpeak=0.10, yearly_fixed_fee=60.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                distribution_peak=0.12,
                distribution_offpeak=0.08,
                transport=0.0145,
            )
        },
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://stub",
        publication_label="april 2026",
    )
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    ph_mono = await _drive_compare(hass, entry, other_snap=bi_aware_snap, meter="mono")
    ph_bi = await _drive_compare(hass, entry, other_snap=bi_aware_snap, meter="bi")
    # Mono uses the single-rate column; bi routes through peak/offpeak
    # depending on the current hour. Either way the two should not
    # produce the same compare_per_kwh.
    assert ph_mono["meter_used"] == "mono"
    assert ph_bi["meter_used"] == "bi"
    assert ph_mono["compare_per_kwh"] != ph_bi["compare_per_kwh"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_tou_uses_weighted_average_across_slots(
    hass: HomeAssistant,
) -> None:
    """A TOU contract's per-kWh number for the annual estimate must
    be a time-weighted average across peak / transition / offpeak
    slots, not whichever slot the user happens to be in when they
    open the dialog. The helper computes breakdowns at three
    representative weekday hours and weights by the standard CWaPE
    slot durations."""
    from custom_components.be_electricity_prices.config_flow import (
        _tou_weighted_per_kwh,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        SupplierSnapshot,
        TaxOverlay,
        TimeOfUseRates,
    )

    snap = SupplierSnapshot(
        supplier="luminus",
        contract="luminus_smartflex",
        energy=TimeOfUseRates(
            peak=0.30,
            transition=0.20,
            offpeak=0.10,
            yearly_fixed_fee=60.0,
            weekend_rule="weekend_offpeak",
        ),
        dsos={
            "ores": DsoOverlay(distribution_single=0.10, transport=0.0145),
        },
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://stub",
        publication_label="april 2026",
    )
    # Run at 14:00 on a Wednesday so compute_breakdown's "live" call
    # would land in peak slot (0.30). The weighted average must come
    # out lower, between offpeak and peak.
    weekday_peak = datetime(2026, 4, 29, 14, 0, tzinfo=UTC)
    avg = _tou_weighted_per_kwh(
        snap, "ores", "wallonia", weekday_peak, None, "dynamic", "bi_horaire"
    )
    assert avg is not None
    # Energy weights for weekend_offpeak: peak=45h, transition=45h,
    # offpeak=78h, total 168h. Weighted-avg energy =
    # (45*0.30 + 45*0.20 + 78*0.10) / 168 = 30.30 / 168 = 0.1804 EUR.
    # Plus DSO + transport + taxes (no VAT in the stub) -> roughly
    # 0.1804 + 0.10 + 0.0145 + 0.052 = ~0.347 EUR/kWh.
    expected_energy = (45 * 0.30 + 45 * 0.20 + 78 * 0.10) / 168
    # Live peak rate would be 0.30 + ... ~0.466 EUR/kWh; weighted
    # average must be materially lower.
    assert avg < 0.40
    # And the energy component of the weighted avg matches our hand
    # calculation: avg minus the constants leaves the energy term.
    constants = 0.10 + 0.0145 + (0.05 + 0.002)
    assert abs((avg - constants) - expected_energy) < 0.001


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
