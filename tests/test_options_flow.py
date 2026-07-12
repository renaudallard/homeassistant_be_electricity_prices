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

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.config_flow import (
    _validate_contract_dates,
)
from custom_components.be_electricity_prices.coordinator import _parse_iso_date
from custom_components.be_electricity_prices.const import (
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def _bypass_setup() -> Iterator[MagicMock]:
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ) as mock:
        yield mock


@pytest.fixture(autouse=True)
def _bypass_entsoe_validation() -> Iterator[MagicMock]:
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


async def _enter_edit_branch(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
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
    _bypass_entsoe_validation: MagicMock,
) -> None:
    """A bad token from ENTSO-E shows an error and reopens the same step."""
    _bypass_entsoe_validation.return_value = "invalid_api_key"

    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "engie", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "engie_dynamic"}
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
        result["flow_id"], {"supplier": "engie", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "engie_dynamic"}
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


async def _walk_to_solar_cociter_variable(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
    """Drive the edit flow to the solar step for Cociter Variable
    (Wallonia, variable energy, spot-indexed injection)."""
    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "cociter", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "cociter_variable"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "bi_horaire"}
    )
    assert result["step_id"] == "solar"
    return result


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_spot_injection_offers_optional_api_key(
    hass: HomeAssistant,
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    result = await _walk_to_solar_cociter_variable(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 2.0, "solar_regime": "injection"}
    )
    # Variable energy + spot-indexed injection on the injection regime ->
    # the optional ENTSO-E key step appears (no key collected earlier).
    assert result["step_id"] == "injection_api_key"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_key": "inj-key-789"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["api_key"] == "inj-key-789"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_spot_injection_api_key_is_skippable(
    hass: HomeAssistant,
) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)
    result = await _walk_to_solar_cociter_variable(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 2.0, "solar_regime": "injection"}
    )
    assert result["step_id"] == "injection_api_key"
    # Submit blank -> skip; setup completes without a key.
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert not entry.data.get("api_key")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_spot_injection_skipped_when_not_injection_regime(
    hass: HomeAssistant,
) -> None:
    # Same contract on the 'none' regime must NOT ask for a key.
    entry = _make_entry()
    entry.add_to_hass(hass)
    result = await _walk_to_solar_cociter_variable(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"


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


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_brussels_branch_asks_connection_power(
    hass: HomeAssistant,
) -> None:
    # A Brussels connection pays a Brugel OSP fee scaled by connection power,
    # so the flow must ask the tier between the meter and solar steps.
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "mega",
            "contract": "mega_smart_fixed",
            "region": "brussels",
            "dso": "sibelga",
            "meter": "mono",
        },
        title="Mega - Smart Fixed (Brussels)",
    )
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "mega", "region": "brussels"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "mega_smart_fixed"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "sibelga"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    # Brussels has no Wallonia tariff-mode / Flanders capacity step; the
    # connection-power step comes straight after the meter step.
    assert result["step_id"] == "connection_power"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"connection_kva_tier": "le9_6"}
    )
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["connection_kva_tier"] == "le9_6"


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
    from custom_components.be_electricity_prices.providers.base import FixedRates

    from tests import make_snapshot

    return make_snapshot(
        supplier=supplier,
        contract=contract,
        energy=FixedRates(single=single_rate, yearly_fixed_fee=60.0),
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
        assert ph is not None
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
        InjectionRates,
    )

    from tests import make_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = make_snapshot(
        supplier="cociter",
        contract="cociter_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0, yearly_fixed_fee=60.0),
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


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_spot_injection_target_prompts_for_api_key(
    hass: HomeAssistant,
) -> None:
    """On the injection regime, comparing a non-spot static contract
    against a spot-indexed-injection target (Cociter Variable) needs an
    ENTSO-E spot for the target's feed-in credit. When the user's entry
    has no api_key, the compare flow detours through compare_api_key
    after the meter step instead of dropping the credit silently."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )

    from tests import make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
        },
        title="Eneco injection",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = make_snapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=VariableRates(current=0.17),
        injection=InjectionRates(current=None, factor=0.925, base=-0.0125),
        source_url="test://stub",
        publication_label="april 2026",
    )
    fake = replace(EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap))
    with patch.dict(EXTRACTORS, {"cociter": fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "cociter"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "cociter_variable"}
        )
        # Variable contract shows the meter step; the api-key gate fires
        # after it because the target's injection is spot-indexed.
        assert result["step_id"] == "compare_meter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_api_key"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_does_not_mutate_live_historical_spots(
    hass: HomeAssistant,
) -> None:
    """Quoting a spot-indexed-injection target with a borrowed key must
    not leave the borrowed historical spots on the live coordinator (the
    next tick would persist them), since the user's own entry never
    needed them."""
    from dataclasses import replace
    from datetime import UTC, datetime

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )

    from tests import make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
        },
        title="Eneco injection",
    )
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    coord._historical_spots = {}
    entry.runtime_data = coord
    other_snap = make_snapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=VariableRates(current=0.17),
        injection=InjectionRates(current=None, factor=0.925, base=-0.0125),
        source_url="test://stub",
        publication_label="april 2026",
    )

    async def _fake_ensure(start: Any, end: Any, api_key: Any = None) -> None:
        # Simulate a fetch populating the (temporary) cache.
        coord._historical_spots[datetime(2026, 1, 1, tzinfo=UTC)] = 0.05

    fake = replace(EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap))
    with (
        patch.dict(EXTRACTORS, {"cociter": fake}),
        patch.object(coord, "_ensure_historical_spots", _fake_ensure),
        patch(
            "custom_components.be_electricity_prices.coordinator._compute_current_year_cost",
            AsyncMock(return_value=123.0),
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "cociter"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "cociter_variable"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_api_key"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"api_key": "TESTKEY"}
        )
        assert result["step_id"] == "compare_result"

    # The throwaway quote borrowed spots into a local dict; the live
    # coordinator cache must be left empty so the next tick won't persist
    # them into the user's store.
    assert coord._historical_spots == {}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_spot_injection_current_prompts_for_api_key(
    hass: HomeAssistant,
) -> None:
    """The gate must be symmetric: when the user's OWN entry is a keyless
    spot-indexed-injection contract (Cociter Variable on the injection
    regime) and they compare against a plain static target, the flow must
    still prompt for a key so the current side's feed-in credit is valued,
    not silently dropped (which would bias the quote toward switching)."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )

    from tests import make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_variable",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
        },
        title="Cociter Variable injection (no key)",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("cociter", "cociter_variable", 0.17)
    )
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        injection=InjectionRates(current=0.05),
        source_url="test://stub",
        publication_label="april 2026",
    )
    fake = replace(EXTRACTORS["mega"], fetch=AsyncMock(return_value=other_snap))
    with patch.dict(EXTRACTORS, {"mega": fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "mega"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "mega_online_fixed"}
        )
        assert result["step_id"] == "compare_meter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        # The target is a plain static contract, but the CURRENT entry is
        # spot-indexed Cociter Variable with no saved key -> still prompt.
        assert result["step_id"] == "compare_api_key"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_result_renders_when_coordinator_not_ready(
    hass: HomeAssistant,
) -> None:
    """If the user opens 'compare' while the entry is mid-reload,
    runtime_data is HA's UNDEFINED sentinel and _build_compare_placeholders
    short-circuits. Every placeholder the result template references must
    still be set; otherwise HA renders raw '{token}' literals."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    # Deliberately do NOT assign entry.runtime_data: the isinstance
    # check in _build_compare_placeholders falls through to the
    # entry-reloading branch.
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "cociter"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "cociter_variable"}
    )
    # Static contracts add a meter step before the result.
    if result["step_id"] == "compare_meter":
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
    assert result["step_id"] == "compare_result"
    ph = result["description_placeholders"]
    assert ph is not None
    # Every token referenced by the result template must be set.
    for key in (
        "ytd_injection_kwh",
        "solar_note",
        "meter_used",
        "annual_kwh",
        "ytd_kwh",
        "consumption_source",
        "current_supplier",
        "compare_supplier",
        "current_per_kwh",
        "compare_per_kwh",
        "current_annual",
        "compare_annual",
        "delta_annual",
        "current_ytd",
        "compare_ytd",
        "delta_ytd",
        "annual_chart",
        "ytd_chart",
        "error",
    ):
        assert key in ph, f"missing placeholder: {key}"
    assert ph["error"].startswith("current entry is reloading")


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
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    return dict(placeholders)


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
    )

    from tests import make_snapshot

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
    # Target is a non-spot fixed contract so its injection is the printed
    # monthly indicative; Cociter Variable is spot-indexed and would
    # instead detour through the api-key gate (tested separately).
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
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
        ph = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
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
    )

    from tests import make_snapshot

    # Snapshot with distinct peak / offpeak rates so meter=bi yields a
    # different per-kWh than meter=mono.
    bi_aware_snap = make_snapshot(
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
    from custom_components.be_electricity_prices.providers.base import TimeOfUseRates

    from tests import make_snapshot

    snap = make_snapshot(
        supplier="luminus",
        contract="luminus_smartflex",
        energy=TimeOfUseRates(
            peak=0.30,
            transition=0.20,
            offpeak=0.10,
            yearly_fixed_fee=60.0,
            weekend_rule="weekend_offpeak",
        ),
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


def test_compare_tou_weights_bihoraire_network_over_full_week() -> None:
    # For a TOU contract on a bi-horaire DSO network, the annual estimate
    # must stay independent of the dialog-open hour and blend the network
    # bands across the week (the energy TOU slots and the bi-horaire network
    # bands don't align, so a single sample per energy slot mis-prices the
    # network).
    from custom_components.be_electricity_prices.config_flow import (
        _tou_weighted_per_kwh,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        TimeOfUseRates,
    )

    from tests import make_snapshot

    snap = make_snapshot(
        supplier="engie",
        contract="engie_empower_flextime",
        energy=TimeOfUseRates(
            peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_no_peak"
        ),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                distribution_peak=0.14,
                distribution_offpeak=0.06,
                transport=0.0145,
            )
        },
    )
    at_night = datetime(2026, 4, 29, 3, 0, tzinfo=UTC)
    at_peak = datetime(2026, 4, 29, 18, 0, tzinfo=UTC)
    a = _tou_weighted_per_kwh(
        snap, "ores", "wallonia", at_night, None, "dynamic", "bi_horaire"
    )
    b = _tou_weighted_per_kwh(
        snap, "ores", "wallonia", at_peak, None, "dynamic", "bi_horaire"
    )
    assert a is not None and b is not None
    assert a == pytest.approx(b)  # independent of the dialog-open hour
    # The network band is blended, not pinned to one sample: the result sits
    # strictly between the all-offpeak and all-peak network extremes.
    taxes = 0.05 + 0.002 + 0.015  # federal + contribution + wallonia renewables
    lo = 0.10 + (0.06 + 0.0145) + taxes  # cheapest hour (offpeak energy+net)
    hi = 0.30 + (0.14 + 0.0145) + taxes  # dearest hour (peak energy+net)
    assert lo < a < hi


def test_compare_smartflex_seasonal_is_dialog_time_invariant() -> None:
    # SmartFlex's seasonal per-kWh estimate must not depend on the hour the
    # user opened the dialog, and must sit between the pure-offpeak and
    # pure-peak all-in (a season/hour-blended average).
    from custom_components.be_electricity_prices.config_flow import (
        _tou_weighted_per_kwh,
    )
    from custom_components.be_electricity_prices.providers.base import TimeOfUseRates

    from tests import make_snapshot

    snap = make_snapshot(
        supplier="luminus",
        contract="luminus_smartflex",
        energy=TimeOfUseRates(
            peak=0.30,
            transition=0.20,
            offpeak=0.10,
            yearly_fixed_fee=60.0,
            weekend_rule="smartflex_seasonal",
        ),
        source_url="test://stub",
        publication_label="april 2026",
    )
    at_night = datetime(2026, 2, 1, 3, 0, tzinfo=UTC)
    at_peak = datetime(2026, 2, 1, 18, 0, tzinfo=UTC)
    a = _tou_weighted_per_kwh(
        snap, "ores", "wallonia", at_night, None, "dynamic", "bi_horaire"
    )
    b = _tou_weighted_per_kwh(
        snap, "ores", "wallonia", at_peak, None, "dynamic", "bi_horaire"
    )
    assert a is not None and b is not None
    assert a == pytest.approx(b)  # independent of the dialog-open hour
    # Constants (dist + transport + taxes, no VAT in the stub) common to
    # every hour; the blended energy term must land strictly between the
    # cheapest and dearest band.
    constants = 0.10 + 0.0145 + (0.05 + 0.002)
    assert 0.10 < (a - constants) < 0.30


def test_compare_bihourly_meter_weights_peak_offpeak() -> None:
    # A Fixed/Variable contract compared on a bi-hourly meter must time-
    # weight peak vs off-peak, not return whichever slot the dialog opened
    # in, so the per-kWh is independent of when_now.
    from custom_components.be_electricity_prices.config_flow import (
        _tou_weighted_per_kwh,
    )
    from custom_components.be_electricity_prices.providers.base import FixedRates

    from tests import make_snapshot

    snap = make_snapshot(
        energy=FixedRates(single=0.20, peak=0.30, offpeak=0.10, yearly_fixed_fee=60.0),
    )
    at_peak = _tou_weighted_per_kwh(
        snap,
        "ores",
        "wallonia",
        datetime(2026, 4, 29, 9, 0, tzinfo=UTC),
        None,
        "bi",
        "",
    )
    at_offpeak = _tou_weighted_per_kwh(
        snap,
        "ores",
        "wallonia",
        datetime(2026, 4, 29, 3, 0, tzinfo=UTC),
        None,
        "bi",
        "",
    )
    assert at_peak is not None and at_offpeak is not None
    # Time-invariant: the weighted average does not depend on the slot the
    # dialog opened in (the bug returned the single-instant rate).
    assert at_peak == pytest.approx(at_offpeak)
    # Strictly between the pure-offpeak (~0.267) and pure-peak (~0.467)
    # instant all-in, i.e. a genuine weighted average.
    assert 0.267 < at_peak < 0.467


def test_solar_schema_offers_compensation_only_in_wallonia() -> None:
    # Compensation is a Walloon-only regime; offering it in Flanders/Brussels
    # would let a user double-count the capacity tariff with the prosumer fee.
    from custom_components.be_electricity_prices.config_flow import _solar_schema
    from custom_components.be_electricity_prices.const import CONF_SOLAR_REGIME

    def _regimes(region: str) -> list[str]:
        schema = _solar_schema({"region": region})
        for key, sel in schema.schema.items():
            if getattr(key, "schema", key) == CONF_SOLAR_REGIME:
                return list(sel.config["options"])
        raise AssertionError("solar_regime key missing from schema")

    assert "compensation" in _regimes("wallonia")
    assert "compensation" not in _regimes("flanders")
    assert "compensation" not in _regimes("brussels")


def test_compare_spot_indexed_injection_uses_mean_spot() -> None:
    # A spot-indexed injection credit must be priced off the window mean
    # (consistent with the energy term), not the live current slot.
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.config_flow import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )

    from tests import make_snapshot

    snap = make_snapshot(
        energy=VariableRates(current=0.20),
        injection=InjectionRates(current=None, factor=0.97, base=-0.021, formula="x"),
    )
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    # The live helper would pick a current-slot spot from spot_dict (0.20);
    # the compare must use avg_spot (0.08) instead.
    spot_dict = {datetime(2026, 4, 29, h, 0, tzinfo=UTC): 0.20 for h in range(24)}
    credit = _compare_injection_credit(snap, entry, spot_dict, avg_spot=0.08)
    assert credit == pytest.approx(0.97 * 0.08 - 0.021)


def test_compare_tou_injection_uses_weighted_average_across_slots() -> None:
    # A per-slot TOU injection credit (Engie Empower Flextime) must be
    # time-averaged over the published slot durations, not returned as the
    # live current-slot rate the way the live helper would.
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.config_flow import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        TimeOfUseRates,
    )

    from tests import make_snapshot

    snap = make_snapshot(
        energy=TimeOfUseRates(
            peak=0.20, transition=0.16, offpeak=0.12, weekend_rule="weekend_no_peak"
        ),
        injection=InjectionRates(peak=0.06, transition=0.04, offpeak=0.02),
    )
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    # weekend_no_peak weights: peak 45h, transition 69h, offpeak 54h per week.
    expected = (0.06 * 45.0 + 0.04 * 69.0 + 0.02 * 54.0) / (45.0 + 69.0 + 54.0)
    credit = _compare_injection_credit(snap, entry, {}, avg_spot=None)
    assert credit == pytest.approx(expected)
    # The credit reflects the weighted mix, never a single slot rate.
    assert credit not in (0.06, 0.04, 0.02)


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


def test_annual_fees_include_data_management() -> None:
    # The digital-meter data-management fee (databeheer) is a fixed
    # EUR/year DSO charge that must be billed alongside the supplier
    # subscription (re-audit F22).
    from custom_components.be_electricity_prices.config_flow import _annual_fees
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
    )
    from tests import make_snapshot

    snap = make_snapshot(
        energy=FixedRates(single=0.20, yearly_fixed_fee=70.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                data_management_per_year=15.0,
            )
        },
    )
    # _make_entry is Wallonia / mono / no solar -> only the yearly fee and
    # the databeheer fee contribute.
    fees = _annual_fees(snap, _make_entry(), 0.0, "mono")
    assert fees == pytest.approx(70.0 + 15.0)


def test_annual_fees_exclude_capacity_for_ytd() -> None:
    # The YTD what-if excludes the Flanders capacity tariff (billed as a
    # separate sensor by current_year_cost); the full annual estimate keeps
    # it. include_capacity toggles just that term.
    from custom_components.be_electricity_prices.config_flow import _annual_fees
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
    )
    from tests import make_snapshot

    snap = make_snapshot(
        energy=FixedRates(single=0.20, yearly_fixed_fee=70.0),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                capacity_eur_per_kw_year=40.0,
            )
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
        },
    )
    with_cap = _annual_fees(snap, entry, 5.0, "mono", include_capacity=True)
    without_cap = _annual_fees(snap, entry, 5.0, "mono", include_capacity=False)
    # 5 kW * 40 EUR/kW/yr = 200 EUR/yr of capacity present only in the full
    # annual figure.
    assert with_cap - without_cap == pytest.approx(200.0)
    assert without_cap == pytest.approx(70.0)


# --- Contract start/end date (discussion #38) ---------------------------------


def test_parse_iso_date_helper() -> None:
    assert _parse_iso_date("2025-11-15") == date(2025, 11, 15)
    assert _parse_iso_date(None) is None
    assert _parse_iso_date("") is None
    assert _parse_iso_date("not-a-date") is None


def test_validate_contract_dates_helper() -> None:
    # Nothing entered, or a plainly-past start, is fine.
    assert _validate_contract_dates({}) == {}
    assert _validate_contract_dates({CONF_CONTRACT_START_DATE: "2020-01-01"}) == {}
    # A future start date is rejected.
    assert _validate_contract_dates({CONF_CONTRACT_START_DATE: "2099-01-01"}) == {
        CONF_CONTRACT_START_DATE: "start_date_in_future"
    }
    # End must be strictly after start.
    assert _validate_contract_dates(
        {CONF_CONTRACT_START_DATE: "2026-01-01", CONF_CONTRACT_END_DATE: "2025-12-01"}
    ) == {CONF_CONTRACT_END_DATE: "end_before_start"}
    assert _validate_contract_dates(
        {CONF_CONTRACT_START_DATE: "2026-01-01", CONF_CONTRACT_END_DATE: "2026-01-01"}
    ) == {CONF_CONTRACT_END_DATE: "end_before_start"}
    # An end date without a start date is a bare renewal reminder, allowed.
    assert _validate_contract_dates({CONF_CONTRACT_END_DATE: "2025-12-01"}) == {}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_contract_dates_round_trip(hass: HomeAssistant) -> None:
    """Start/end dates entered at the contract step persist on the entry."""
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "cociter", "region": "wallonia"}
    )
    assert result["step_id"] == "contract"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "contract": "cociter_variable",
            "contract_start_date": "2025-11-15",
            "contract_end_date": "2027-11-14",
        },
    )
    assert result["step_id"] == "dso"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "bi"}
    )
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "bi_horaire"}
    )
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert entry.data["contract_start_date"] == "2025-11-15"
    assert entry.data["contract_end_date"] == "2027-11-14"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_contract_step_rejects_future_start_date(hass: HomeAssistant) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    assert result["step_id"] == "contract"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"contract": "power_fix", "contract_start_date": "2099-01-01"},
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "contract"
    assert result["errors"] == {"contract_start_date": "start_date_in_future"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_contract_step_rejects_end_before_start(hass: HomeAssistant) -> None:
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    assert result["step_id"] == "contract"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "contract": "power_fix",
            "contract_start_date": "2026-01-01",
            "contract_end_date": "2025-12-01",
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "contract"
    assert result["errors"] == {"contract_end_date": "end_before_start"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_signed_rate_step_for_fixed_with_start_date(
    hass: HomeAssistant,
) -> None:
    """A start date on a fixed contract inserts the optional signing-rate step,
    and the typed rate round-trips onto the entry."""
    entry = _make_entry()  # eneco / power_fix (fixed) / wallonia
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    assert result["step_id"] == "contract"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"contract": "power_fix", "contract_start_date": "2025-11-10"},
    )
    # Fixed + start date -> the signing-rate override step.
    assert result["step_id"] == "signed_rate"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"manual_energy_single": 0.22, "manual_yearly_fee": 60.0},
    )
    assert result["step_id"] == "dso"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "simple"}
    )
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert entry.data["contract_start_date"] == "2025-11-10"
    assert entry.data["manual_energy_single"] == 0.22
    assert entry.data["manual_yearly_fee"] == 60.0


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_no_signed_rate_step_without_start_date(
    hass: HomeAssistant,
) -> None:
    """No start date -> the signing-rate step is skipped entirely."""
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "power_fix"}
    )
    # Straight to the DSO step, no signing-rate detour.
    assert result["step_id"] == "dso"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_clears_contract_dates_when_blanked(
    hass: HomeAssistant,
) -> None:
    """Blanking the start/end date pickers on the options edit flow removes the
    stored dates (turns signing-cohort pricing / the reminder back off), rather
    than re-injecting the old value."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "contract_start_date": "2025-11-15",
            "contract_end_date": "2027-11-14",
        },
        title="Eneco - power_fix (Wallonia)",
    )
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "eneco", "region": "wallonia"}
    )
    assert result["step_id"] == "contract"
    # Submit the contract with the date pickers left blank (cleared): the keys
    # are absent from user_input, so the flow must drop them.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "power_fix"}
    )
    # Start date cleared -> no signing-rate step; straight to DSO.
    assert result["step_id"] == "dso"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    assert result["step_id"] == "dso_tariff_mode"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso_tariff_mode": "simple"}
    )
    assert result["step_id"] == "solar"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert "contract_start_date" not in entry.data
    assert "contract_end_date" not in entry.data
