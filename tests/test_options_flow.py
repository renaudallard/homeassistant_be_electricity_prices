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

from custom_components.be_electricity_prices import snapshot_store
from custom_components.be_electricity_prices.compare_quote import RankedRow

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.flow_schemas import (
    _validate_contract_dates,
)
from custom_components.be_electricity_prices.const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_INCLUDE_VAT,
    DOMAIN,
)
from custom_components.be_electricity_prices.cohort import _parse_iso_date
from tests import make_entry


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
    # Patch BOTH bindings. flow_schemas defines the validator, but config_flow
    # and compare_flow each imported it BY VALUE, so patching the definition
    # site reaches neither: the install step would go unpatched and the compare
    # API-key step would make a live request to transparency.entsoe.eu.
    with (
        patch(
            "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
            return_value=None,
        ) as mock,
        patch(
            "custom_components.be_electricity_prices.compare_flow._validate_entsoe_key",
            return_value=None,
        ),
    ):
        yield mock


def _make_entry() -> MockConfigEntry:
    return make_entry()


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
async def test_edit_branch_offers_a_withdrawn_supplier_it_already_has(
    hass: HomeAssistant,
) -> None:
    """A withdrawn supplier is hidden from new setups but must stay in the
    dropdown of an entry that already uses it: HA's SelectSelector rejects a
    default outside its options, which would make the entry uneditable."""
    entry = make_entry(
        supplier="dats24", contract="dats24_groen_variabel", region="flanders"
    )
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    marker = next(k for k in schema if str(k) == "supplier")
    selector = schema[marker]
    assert marker.default() == "dats24"
    assert "dats24" in {o["value"] for o in selector.config["options"]}
    # The selector must accept its own default. This is the real failure
    # mode: an out-of-options default raises InInvalid and the step dies.
    assert selector(marker.default()) == "dats24"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_impact_product_is_not_asked_which_tariff_mode(
    hass: HomeAssistant,
) -> None:
    """An Impact card bands its ENERGY on the CWaPE incitative schedule, which
    exists only under that tariff configuration. Offering the standard mode
    pre-selected let a user bill the two legs off different structures: the
    energy routed by Impact band while the network took the standard jour and
    nuit columns, and the Walloon fixed term charged on top of both."""
    entry = _make_entry()
    entry.add_to_hass(hass)

    result = await _enter_edit_branch(hass, entry)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "octaplus", "region": "wallonia"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"contract": "octaplus_fixed_impact"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"dso": "ores"}
    )
    # An Impact card requires a smart meter, so the meter step offers one.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "dynamic"}
    )
    # Straight past the question to the solar step, with the mode decided.
    assert result["step_id"] == "solar"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"solar_kva": 0.0, "solar_regime": "none"}
    )
    assert result["step_id"] == "meters"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert entry.data["dso_tariff_mode"] == "impact"


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
    # Power Fix indexes its feed-in credit on the monthly Belpex-injectie, so
    # the injection regime now offers the optional ENTSO-E key. Skipped here.
    assert result["step_id"] == "injection_api_key"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"api_key": ""}
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


async def _pass_compare_solar(
    hass: HomeAssistant, entry: MockConfigEntry, result: Any
) -> Any:
    """Submit the entry's own solar settings on the what-if step, so a
    test that is not about the regime override walks straight through it.
    The step is only shown for an entry that has solar at all."""
    if result["step_id"] != "compare_solar":
        return result
    return await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"solar_regime": entry.data.get("solar_regime", "none")},
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
    fake_cociter = replace(
        cociter_ext, fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
    dynamic), so the supplier picker is filtered by region, by 'has at least
    one contract here', and by segment -- not by kind. The kind switch happens
    at the contract picker (via _compare_contract_schema) and the api_key step
    kicks in when the user crosses into dynamic territory without a saved
    key."""
    from custom_components.be_electricity_prices.compare_flow import (
        _compare_supplier_options,
    )

    # Static-side caller still gets every Walloon supplier.
    static_options = _compare_supplier_options("wallonia", "fixed", False)
    static_ids = {o["value"] for o in static_options}
    assert "eneco" in static_ids
    assert "cociter" in static_ids
    # Dynamic-side caller gets the same set: cross-kind is allowed.
    dynamic_options = _compare_supplier_options("wallonia", "dynamic", False)
    dynamic_ids = {o["value"] for o in dynamic_options}
    assert dynamic_ids == static_ids


def _static_to_dynamic_compare(hass: HomeAssistant) -> tuple[Any, Any]:
    """A static entry and a patched cociter_dynamic target to compare it to.

    Returns the entry and the not-yet-entered EXTRACTORS patch, so the caller
    keeps the fake extractor in place for as long as it drives the flow.
    """
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
    fake = replace(
        EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
    return entry, patch.dict(EXTRACTORS, {"cociter": fake})


async def _drive_to_compare_api_key(hass: HomeAssistant, entry: Any) -> Any:
    """Walk the compare branch as far as the ENTSO-E key prompt."""
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
    # Dynamic locks the meter to dynamic and skips compare_meter, then routes
    # to compare_api_key because the static entry has no saved api_key. The
    # solar what-if step sits in between: it is shown for a dynamic target
    # too, so the key gate can see the regime the quote will be priced on.
    result = await _pass_compare_solar(hass, entry, result)
    assert result["step_id"] == "compare_api_key"
    return result


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_static_to_dynamic_prompts_for_api_key(
    hass: HomeAssistant,
) -> None:
    """A static-contract user comparing against a dynamic contract
    needs an ENTSO-E spot for the dynamic side. When their entry has
    no api_key yet, the compare flow detours through compare_api_key
    after the contract pick (meter is auto-locked to dynamic)."""
    entry, extractors = _static_to_dynamic_compare(hass)
    with extractors:
        result = await _drive_to_compare_api_key(hass, entry)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"api_key": "valid-token"}
        )
        # _validate_entsoe_key is auto-bypassed by the test fixture; the
        # next step is the result page.
        assert result["step_id"] == "compare_result"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_api_key_is_skippable_when_left_blank(
    hass: HomeAssistant,
) -> None:
    """Blank means skip, like the injection key step, and asks ENTSO-E nothing.

    A comparison is a one-off quote, so a user without a token should still
    see every other line of it rather than be stopped on a page they cannot
    get past. Before this the step was Required and always validated, which
    while ENTSO-E is unreachable meant a cannot_connect the user could do
    nothing about.
    """
    entry, extractors = _static_to_dynamic_compare(hass)
    with extractors:
        result = await _drive_to_compare_api_key(hass, entry)
        with patch(
            "custom_components.be_electricity_prices.compare_flow._validate_entsoe_key",
            side_effect=AssertionError("a blank key must not be validated"),
        ):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"api_key": "   "}
            )
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
    fake = replace(
        EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
        result = await _pass_compare_solar(hass, entry, result)
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
        # Simulate a fetch populating the (temporary) caches. Both of them: a
        # fetch fills whichever the borrowing entry replays from, and an
        # assertion against a dict the fake never touches proves nothing.
        coord._historical_spots[datetime(2026, 1, 1, tzinfo=UTC)] = 0.05
        coord._historical_spot_quarters[datetime(2026, 1, 1, tzinfo=UTC)] = [
            0.04,
            0.05,
            0.05,
            0.06,
        ]

    fake = replace(
        EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
    with (
        patch.dict(EXTRACTORS, {"cociter": fake}),
        patch.object(coord, "_ensure_historical_spots", _fake_ensure),
        patch(
            "custom_components.be_electricity_prices.ytd_cost._compute_current_year_cost",
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
        result = await _pass_compare_solar(hass, entry, result)
        assert result["step_id"] == "compare_api_key"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"api_key": "TESTKEY"}
        )
        assert result["step_id"] == "compare_result"

    # The throwaway quote borrowed spots into a local dict; the live
    # coordinator cache must be left empty so the next tick won't persist
    # them into the user's store. Both caches: the fetch fills whichever the
    # borrowing entry needs, and leaving one behind persists a year of slots
    # onto an entry whose hourly cache is back to empty.
    assert coord._historical_spots == {}
    assert coord._historical_spot_quarters == {}


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
    fake = replace(
        EXTRACTORS["mega"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
        result = await _pass_compare_solar(hass, entry, result)
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
    regime: str | None = None,
    whatif_kwh: tuple[float, float] | None = None,
) -> dict[str, str]:
    """Walk the compare flow end-to-end and return the result form's
    description placeholders. Replaces the alternative supplier's
    fetch with a stub returning ``other_snap`` (SupplierExtractor is
    a frozen dataclass, so we swap the registry entry instead of
    patching .fetch directly).

    ``regime`` / ``whatif_kwh`` drive the solar what-if step;
    left unset it submits the entry's own values, which quotes exactly as
    the flow did before that step existed."""
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    fake = replace(
        EXTRACTORS[other_supplier], fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
        if result["step_id"] == "compare_solar":
            payload: dict[str, Any] = {
                "solar_regime": regime or entry.data.get("solar_regime", "none")
            }
            if whatif_kwh is not None:
                payload["whatif_consumption_kwh"] = whatif_kwh[0]
                payload["whatif_injection_kwh"] = whatif_kwh[1]
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], payload
            )
        if result["step_id"] == "compare_api_key":
            # A target whose feed-in credit indexes on a monthly mean is now
            # offered the optional ENTSO-E key, like a spot-indexed one always
            # was. Skipped here: these cases assert the quote, not the key.
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"api_key": ""}
            )
        assert result["step_id"] == "compare_result"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    return dict(placeholders)


@pytest.mark.usefixtures("enable_custom_integrations")
def _spread(total: float, start: Any, end: Any) -> dict[Any, float]:
    """Spread a period total evenly across every day of ``[start, end]``.

    The compare path judges a window by how many distinct days the recorder
    returned a bucket for, so the single synthetic day these fakes used to
    return now reads as one day of history and is refused as too thin to
    annualise. Spreading keeps every sum identical while giving the window
    real coverage.
    """
    days = (end - start).days + 1
    per_day = total / days
    return {start + timedelta(days=i): per_day for i in range(days)}


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
            return _spread(measured_rolling, start, end)
        return _spread(measured_ytd, start, end)

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
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
async def test_compare_rolling_window_is_365_days_not_366(
    hass: HomeAssistant,
) -> None:
    """The rolling window must cover 365 days, not 366.

    ``energy_meters._recorder_rows`` anchors its end on the NEXT local
    midnight, so the window is end-inclusive. Subtracting a full 365 days from
    today therefore read 366 buckets under a "365 days" label, quoting one
    extra day of consumption every year."""
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
    seen: list[int] = []

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id != "sensor.house_total":
            return {}
        seen.append((end - start).days + 1)
        return _spread(3500.0, start, end)

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        await _drive_compare(
            hass, entry, other_snap=_stub_snapshot("cociter", "cociter_variable", 0.16)
        )
    assert max(seen) == 365


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_refuses_to_annualise_a_thin_window(
    hass: HomeAssistant,
) -> None:
    """A few weeks of history must not be presented as a year.

    A six-week sum used as the annual volume understates the bill by roughly
    the ratio of the window to the year, and it looked measured while doing
    it. Below the floor the quote falls back to the household default and the
    source line says how little history there actually is."""
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

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id != "sensor.house_total":
            return {}
        # 42 days of history, wherever the window starts.
        return {end - timedelta(days=i): 10.0 for i in range(42)}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass, entry, other_snap=_stub_snapshot("cociter", "cociter_variable", 0.16)
        )
    # 420 kWh of real history, but the year is not 420 kWh.
    assert ph["annual_kwh"] == "3500"
    assert "42 days" in ph["consumption_source"]
    assert "measured" not in ph["consumption_source"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_scales_a_partial_window_and_says_so(
    hass: HomeAssistant,
) -> None:
    """Between the floor and a full year the window is scaled up, and the
    source line says it is scaled rather than measured, because the result
    carries whichever season the window happened to cover."""
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

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id != "sensor.house_total":
            return {}
        # 200 days at 10 kWh: 2000 measured, 3650 annualised.
        return {end - timedelta(days=i): 10.0 for i in range(200)}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass, entry, other_snap=_stub_snapshot("cociter", "cociter_variable", 0.16)
        )
    assert ph["annual_kwh"] == "3650"
    assert "scaled from 200 days" in ph["consumption_source"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_falls_back_to_the_entry_typed_annual_volume(
    hass: HomeAssistant,
) -> None:
    """A professional entry types its own yearly volume for the excise band.
    With too little history to annualise, that figure beats the household
    default, which is a residential assumption."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "consumption_kwh": "sensor.house_total",
            "annual_consumption_kwh": 12000.0,
        },
        title="Eneco - Wallonia",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass, entry, other_snap=_stub_snapshot("cociter", "cociter_variable", 0.16)
        )
    assert ph["annual_kwh"] == "12000"
    assert "entered on the entry" in ph["consumption_source"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_credits_a_short_injection_window(
    hass: HomeAssistant,
) -> None:
    """A recently commissioned injection sensor must still be credited.

    The consumption leg is annualised from its coverage; the injection leg is
    deliberately NOT. Routing injection through the same resolver zeroed any
    window under the floor, which made the page print "no injection sensor
    wired" for a sensor that was wired and producing, drop the credit from both
    quoted sides, and contradict the YTD injected figure printed just above it.
    Scaling it instead is no better: PV is far more seasonal than consumption,
    so a spring window scaled by a bare day count can over-credit enough to
    drive the compensation net to its zero clamp."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
            "solar_kva": 5.0,
            "consumption_kwh": "sensor.cons",
            "injection_kwh": "sensor.inj",
        },
        title="Eneco - Wallonia",
    )
    entry.add_to_hass(hass)
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )
    from tests import make_snapshot

    current_snap = _stub_snapshot("eneco", "power_fix", 0.18)
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)
    # A fixed target with a monthly indicative credit: a spot-indexed one
    # would detour through the api-key gate instead of the result page.
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        dsos=current_snap.dsos,
        taxes=current_snap.taxes,
        injection=InjectionRates(current=0.10),
        source_url="test://stub",
        publication_label="april 2026",
    )

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(4000.0, start, end)
        if entity_id == "sensor.inj":
            # Wired 60 days ago, well under the annualisation floor.
            return {end - timedelta(days=i): 15.0 for i in range(60)}
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
    # The 900 kWh actually measured is credited, not discarded.
    assert "900 kWh credited" in ph["solar_note"]
    assert "no injection sensor wired" not in ph["solar_note"]


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
            return _spread(cons, start, end)
        if entity_id == "sensor.inj":
            return _spread(inj, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
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
            return _spread(cons, start, end)
        if entity_id == "sensor.inj":
            return _spread(inj, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
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
async def test_compare_names_the_side_that_credits_no_injection(
    hass: HomeAssistant,
) -> None:
    """A card that publishes no injection tariff credits nothing, and
    _annual_bill folds that into the same branch as the no-solar case. The
    page stated the injected kWh were "credited at each supplier's
    injection price" either way, so a supplier that genuinely pays nothing
    was indistinguishable from one the quote could not price. The note now
    names the side and the reason."""
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

    current_snap = _stub_snapshot("eneco", "power_fix", 0.20)
    object.__setattr__(current_snap, "injection", InjectionRates(current=0.05))
    # No injection block at all on the target's card.
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        dsos=current_snap.dsos,
        taxes=current_snap.taxes,
        injection=None,
        source_url="test://stub",
        publication_label="april 2026",
    )
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(5000.0, start, end)
        if entity_id == "sensor.inj":
            return _spread(4000.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
    assert "Mega publishes no injection tariff" in ph["solar_note"]
    # The user's own side prices its injection fine, so it must not be
    # named alongside it.
    assert "Eneco publishes no injection tariff" not in ph["solar_note"]
    # The credit really is absent on that side: same energy rate, same
    # fees, so the alternative costs exactly the credit the current side
    # gets (0.05 * 4000 = 200 EUR).
    diff = float(ph["compare_annual"]) - float(ph["current_annual"])
    assert abs(diff - 200.0) < 1.0


def _prosumer_entry_and_snapshots(hass: HomeAssistant) -> tuple[Any, Any, Any]:
    """A Walloon compensation entry whose DSO actually publishes a prosumer
    rate, plus both sides' snapshots. The stubs used elsewhere leave that
    rate None, which zeroes the term the regime what-if turns on and off."""
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
        InjectionRates,
        TaxOverlay,
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
            "solar_regime": "compensation",
            "solar_kva": 5.0,
        },
        title="Eneco - Wallonia compensation",
    )
    entry.add_to_hass(hass)
    dsos = {
        "ores": DsoOverlay(
            distribution_single=0.10,
            transport=0.0145,
            prosumer_eur_per_kva_year=82.0,
        )
    }
    taxes = TaxOverlay(federal_excise=0.0, energy_contribution=0.0)
    current_snap = make_snapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.20, yearly_fixed_fee=0.0),
        dsos=dsos,
        taxes=taxes,
        injection=InjectionRates(current=0.05),
        source_url="test://stub",
        publication_label="april 2026",
    )
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=0.0),
        dsos=dsos,
        taxes=taxes,
        injection=InjectionRates(current=0.05),
        source_url="test://stub",
        publication_label="april 2026",
    )
    return entry, current_snap, other_snap


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_whatif_reprices_both_sides(
    hass: HomeAssistant,
) -> None:
    """Quoting a compensation entry on the injection tariff has to move
    three things at once: the Walloon prosumer fee stops being billed, the
    meter stops netting, and the injected kWh start earning a credit. The
    regime reaches those through entry.data, so a picker that only changed
    a local variable would leave every euro exactly where it was."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    cons = 5000.0
    inj = 4000.0

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(cons, start, end)
        if entity_id == "sensor.inj":
            return _spread(inj, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        as_configured = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
        whatif = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            regime="injection",
        )

    # The regime is an arithmetic wrapper around the bill, never a
    # re-pricing: the all-in EUR/kWh must not move.
    assert whatif["current_per_kwh"] == as_configured["current_per_kwh"]
    assert whatif["compare_per_kwh"] == as_configured["compare_per_kwh"]

    per_kwh = float(whatif["current_per_kwh"])
    # Leaving compensation drops 5 kVA * 82 EUR/kVA/year of prosumer fee,
    # stops netting the 4000 injected kWh off the consumption, and credits
    # them at 0.05 instead.
    expected = -5.0 * 82.0 + inj * per_kwh - 0.05 * inj
    moved = float(whatif["current_annual"]) - float(as_configured["current_annual"])
    assert moved == pytest.approx(expected, abs=0.5)
    # Both sides move together: the regime belongs to the connection, so
    # the supplier-vs-supplier delta is what must stay put.
    assert whatif["delta_annual"] == as_configured["delta_annual"]
    # The year-to-date legs price through the same proxy, so they move with
    # the regime as well. Prorated by the elapsed year, hence compared
    # against the other run rather than a fixed figure.
    assert whatif["current_ytd"] != as_configured["current_ytd"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_whatif_prints_the_own_contract_baseline(
    hass: HomeAssistant,
) -> None:
    """The compare branch cannot quote a user against their own contract,
    and a regime override moves both sides equally, so the printed supplier
    delta answers nothing about the regime. The note has to carry the
    user's own contract priced both ways or the question is unanswerable
    on the page."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(5000.0, start, end)
        if entity_id == "sensor.inj":
            return _spread(4000.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        as_configured = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
        whatif = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            regime="injection",
        )

    note = whatif["solar_note"]
    assert note.startswith("what-if: both sides quoted on the injection tariff")
    assert "your entry is on the compensation regime" in note
    assert "Your entry is unchanged." in note
    # The baseline is the same contract, same volumes, under the entry's
    # own regime, so it must equal what the unmodified quote printed.
    assert f"{as_configured['current_annual']} EUR/year as configured" in note
    assert f"{whatif['current_annual']} EUR/year under the injection tariff" in note
    # An unmodified quote keeps the plain note it always had.
    assert as_configured["solar_note"].startswith("compensation regime:")


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_requires_volumes_without_an_injection_meter(
    hass: HomeAssistant,
) -> None:
    """A compensation meter may net injection against consumption in one
    register, and that reading is not what the injection tariff bills. With
    no injection sensor to separate the two, the step must refuse rather
    than quote the override off the netted figure. Silently dropping the
    override instead would look exactly like the picker not working, which
    is the complaint this step exists to answer."""
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
            "solar_regime": "compensation",
            "solar_kva": 5.0,
        },
        title="Eneco - no injection meter",
    )
    entry.add_to_hass(hass)
    current_snap = _stub_snapshot("eneco", "power_fix", 0.20)
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        dsos=current_snap.dsos,
        taxes=current_snap.taxes,
        injection=InjectionRates(current=0.05),
        source_url="test://stub",
        publication_label="april 2026",
    )

    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    fake = replace(
        EXTRACTORS["mega"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_solar"
        # The volume fields are offered precisely because no injection
        # sensor is wired.
        data_schema = result["data_schema"]
        assert data_schema is not None
        schema = data_schema.schema
        assert {"whatif_consumption_kwh", "whatif_injection_kwh"} <= {
            str(k) for k in schema
        }
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"solar_regime": "injection"}
        )
        assert result["step_id"] == "compare_solar"
        assert result["errors"] == {"whatif_consumption_kwh": "whatif_volumes_required"}
        # Keeping the entry's own regime needs no volumes.
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"solar_regime": "compensation"}
        )
        assert result["step_id"] == "compare_result"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_typed_volumes_blank_the_year_to_date(
    hass: HomeAssistant,
) -> None:
    """Typed volumes are a yearly hypothesis with no meter history behind
    them, and the year-to-date legs replay exactly that history, recorded
    under the configured regime. Mixing the two would print a what-if
    annual next to a measured YTD and invite reading them as one bill."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    # Drop the injection sensor: this is the netted-register wiring.
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != "injection_kwh"}
    )
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(3500.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            regime="injection",
            whatif_kwh=(6160.0, 2660.0),
        )

    assert ph["annual_kwh"] == "6160"
    assert ph["consumption_source"] == "entered for the what-if"
    assert ph["current_ytd"] == "-"
    assert ph["compare_ytd"] == "-"
    assert ph["delta_ytd"] == "-"
    assert ph["ytd_kwh"] == "-"
    assert ph["ytd_injection_kwh"] == "-"
    assert ph["ytd_chart"] == ""
    assert "year-to-date rows are left blank" in ph["solar_note"]
    # The gross pair is what got billed, not the netted 3500 the sensor
    # reports: consumption at the full rate, injection credited.
    per_kwh = float(ph["current_per_kwh"])
    expected = 6160.0 * per_kwh - 2660.0 * 0.05
    assert float(ph["current_annual"]) == pytest.approx(expected, abs=0.5)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_whatif_to_none_keeps_the_baseline_netted(
    hass: HomeAssistant,
) -> None:
    """Quoting "no solar" must not zero the injected volume for the
    BASELINE leg, which is still priced on the entry's own regime. Reading
    the injection sensor only when the quoted regime has solar un-netted
    the compensation baseline and printed the user's own contract as
    costing hundreds more than an ordinary quote says it does."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(5000.0, start, end)
        if entity_id == "sensor.inj":
            return _spread(4000.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        as_configured = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
        whatif = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            regime="none",
        )

    # The baseline is the same contract under the same regime as the plain
    # quote, so it has to print the same euro figure.
    assert (
        f"{as_configured['current_annual']} EUR/year as configured"
        in (whatif["solar_note"])
    )
    # And the what-if leg really did drop both the netting and the prosumer
    # fee: full consumption billed, no 5 kVA * 82 EUR/kVA/year.
    per_kwh = float(whatif["current_per_kwh"])
    assert float(whatif["current_annual"]) == pytest.approx(5000.0 * per_kwh, abs=0.5)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_typed_volumes_explain_themselves_without_a_flip(
    hass: HomeAssistant,
) -> None:
    """Typed volumes blank the year-to-date rows on their own, with no
    regime change involved, so the sentence explaining the blank rows must
    not hang off the regime having moved."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != "injection_kwh"}
    )
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(3500.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        ph = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            whatif_kwh=(6160.0, 2660.0),
        )

    assert ph["current_ytd"] == "-"
    assert "year-to-date rows are left blank" in ph["solar_note"]
    # No regime moved, so no what-if framing.
    assert "what-if:" not in ph["solar_note"]


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_error_reshow_keeps_a_typed_volume(
    hass: HomeAssistant,
) -> None:
    """Filling one volume box and submitting must not wipe it: the form
    comes back with the error, and a user who has to retype what they
    already entered reasonably concludes the step is broken."""
    entry, current_snap, other_snap = _prosumer_entry_and_snapshots(hass)
    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != "injection_kwh"}
    )
    entry.runtime_data = _real_coordinator(hass, entry, current_snap)

    from dataclasses import replace as dc_replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    fake = dc_replace(
        EXTRACTORS["mega"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
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
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_solar"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"solar_regime": "injection", "whatif_consumption_kwh": 6160.0},
        )
        assert result["step_id"] == "compare_solar"
        assert result["errors"]
        data_schema = result["data_schema"]
        assert data_schema is not None
        marker = next(
            k for k in data_schema.schema if str(k) == "whatif_consumption_kwh"
        )
        assert marker.description == {"suggested_value": 6160.0}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_step_narrows_compensation_to_wallonia(
    hass: HomeAssistant,
) -> None:
    """Same regional narrowing as the install step. A Flemish entry quoted
    on the compensation regime would net injection 1:1 while still paying
    the capacity tariff and no prosumer fee: a bill no Belgian contract can
    issue."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius",
            "meter": "mono",
            "solar_regime": "injection",
            "solar_kva": 5.0,
        },
        title="Eneco - Flanders injection",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.20)
    )

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
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"meter": "mono"}
    )
    assert result["step_id"] == "compare_solar"
    data_schema = result["data_schema"]
    assert data_schema is not None
    schema = data_schema.schema
    marker = next(k for k in schema if str(k) == "solar_regime")
    options = set(schema[marker].config["options"])
    assert options == {"none", "injection"}
    # No injection sensor is wired, so the volume fields are offered.
    assert "whatif_consumption_kwh" in {str(k) for k in schema}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_whatif_resolves_the_custom_rebuild(
    hass: HomeAssistant,
) -> None:
    """The expert custom supplier is the one whose snapshot is built out of
    entry.data, so it is the only one a regime what-if has to rebuild. Two
    things have to hold for that rebuild: it goes through the same resolver
    the coordinator applies (build_snapshot returns the card ex-VAT and
    nothing else grosses the fixed fees), and the baseline leg keeps being
    priced on the card the entry is configured on, which is the only one
    that still carries the injection block."""
    from custom_components.be_electricity_prices.providers.base import FixedRates
    from custom_components.be_electricity_prices.providers.custom import build_snapshot
    from custom_components.be_electricity_prices.snapshot_store import _resolve_snapshot
    from tests import make_snapshot

    data = {
        "supplier": "custom",
        "contract": "custom_fixed",
        "region": "wallonia",
        "dso": "ores",
        "meter": "mono",
        "consumption_kwh": "sensor.cons",
        "injection_kwh": "sensor.inj",
        "solar_regime": "injection",
        "solar_kva": 5.0,
        "custom_energy_single": 0.20,
        "custom_yearly_fixed_fee": 100.0,
        "custom_dso_distribution_single": 0.10,
        "custom_injection_mode": "current",
        "custom_injection_current": 0.05,
        "custom_vat_rate": 0.06,
    }
    entry = MockConfigEntry(domain=DOMAIN, data=data, title="custom injection")
    entry.add_to_hass(hass)
    # What the live coordinator holds: built from entry.data, then resolved.
    stored_snap = _resolve_snapshot(entry, build_snapshot(data, "wallonia", "ores"))
    entry.runtime_data = _real_coordinator(hass, entry, stored_snap)
    other_snap = make_snapshot(
        supplier="mega",
        contract="mega_online_fixed",
        energy=FixedRates(single=0.20, yearly_fixed_fee=100.0),
        dsos=stored_snap.dsos,
        taxes=stored_snap.taxes,
        injection=None,
        source_url="test://stub",
        publication_label="april 2026",
    )

    async def _fake_recorder_daily_kwh(
        _hass: HomeAssistant, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        if entity_id == "sensor.cons":
            return _spread(5000.0, start, end)
        if entity_id == "sensor.inj":
            return _spread(4000.0, start, end)
        return {}

    with patch(
        "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh",
        new=_fake_recorder_daily_kwh,
    ):
        as_configured = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
        )
        whatif = await _drive_compare(
            hass,
            entry,
            other_snap=other_snap,
            other_supplier="mega",
            other_contract="mega_online_fixed",
            regime="compensation",
        )

    # The rebuilt card is VAT-resolved: the entered 100 EUR yearly fee is
    # billed at 106, not 100. An unresolved rebuild shows up here directly.
    per_kwh = float(whatif["current_per_kwh"])
    # Compensation nets 5000 - 4000 and adds no prosumer fee (the custom
    # DSO overlay publishes no prosumer rate).
    assert float(whatif["current_annual"]) == pytest.approx(
        106.0 + 1000.0 * per_kwh, abs=0.5
    )
    # And the baseline still credits the injection the configured card
    # carries, which the rebuilt one drops (it is not on that regime).
    assert (
        f"{as_configured['current_annual']} EUR/year as configured"
        in (whatif["solar_note"])
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_solar_step_skipped_without_solar(
    hass: HomeAssistant,
) -> None:
    """An entry with no panels gains no click: the what-if step is for
    households that have a regime to wonder about."""
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    assert entry.data.get("solar_regime") in (None, "none")

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
    assert result["step_id"] == "compare_result"


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
async def test_compare_prices_a_tarif_impact_target_on_its_own_configuration(
    hass: HomeAssistant,
) -> None:
    """A Tarif Impact product is sold only on the CWaPE incitative
    configuration, so it must be quoted on that whatever the household is on.

    Its energy carries three band rates and no mono/bi structure, so the band
    schedule prices it regardless of the mode. The network leg and the Walloon
    terme fixe do follow the mode, so quoting the target on the household's
    own bi-horaire settings banded the energy while billing the network off
    the standard jour/nuit columns and charging a fixed term the tariff does
    not have. The install flow forces the mode for exactly this reason.

    The invariant: the target's price does not depend on what the household
    next door is configured as."""
    from custom_components.be_electricity_prices.compare_quote import (
        _tou_weighted_per_kwh,
    )

    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        ImpactRates,
    )
    from tests import make_snapshot

    # A Walloon overlay carrying BOTH structures, as the real cards do: the
    # jour/nuit pair the standard tariff bills and the CWaPE triplet the
    # incitative one bills, plus a terme fixe that Impact does not charge.
    overlay = DsoOverlay(
        distribution_single=0.10,
        distribution_peak=0.12,
        distribution_offpeak=0.08,
        distribution_pic=0.15,
        distribution_medium=0.10,
        distribution_eco=0.06,
        transport=0.0145,
        data_management_per_year=19.49,
    )
    # The OCTA+ Wallonia card's bands, which tests/test_octaplus.py pins.
    impact_snap = make_snapshot(
        supplier="octaplus",
        contract="octaplus_fixed_impact",
        energy=ImpactRates(
            pic=0.1972, medium=0.1683, eco=0.1284, yearly_fixed_fee=65.0
        ),
        dsos={"ores": overlay},
        source_url="test://stub",
        publication_label="april 2026",
    )

    on_bi = _make_entry()
    on_bi.add_to_hass(hass)
    on_bi.runtime_data = _real_coordinator(
        hass, on_bi, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    quoted_from_bi = await _drive_compare(
        hass,
        on_bi,
        other_snap=impact_snap,
        other_supplier="octaplus",
        other_contract="octaplus_fixed_impact",
    )

    # The same target, quoted by a household already on the incitative
    # configuration. Nothing about the product changed, so nothing about its
    # price may either.
    on_impact = make_entry(dso_tariff_mode="impact")
    on_impact.add_to_hass(hass)
    on_impact.runtime_data = _real_coordinator(
        hass, on_impact, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    quoted_from_impact = await _drive_compare(
        hass,
        on_impact,
        other_snap=impact_snap,
        other_supplier="octaplus",
        other_contract="octaplus_fixed_impact",
    )

    assert quoted_from_bi["compare_per_kwh"] == quoted_from_impact["compare_per_kwh"]
    assert quoted_from_bi["compare_annual"] == quoted_from_impact["compare_annual"]

    # And it is the incitative price that both quote, not the standard one.
    # Without this the assertion above would pass on two identically wrong
    # numbers if the modes ever priced alike.
    now = dt_util.as_local(dt_util.utcnow())
    on_bands = _tou_weighted_per_kwh(
        impact_snap, "ores", "wallonia", now, None, "dynamic", "impact"
    )
    off_bands = _tou_weighted_per_kwh(
        impact_snap, "ores", "wallonia", now, None, "dynamic", "bi_horaire"
    )
    assert on_bands is not None and off_bands is not None
    assert on_bands != pytest.approx(off_bands)
    assert float(quoted_from_bi["compare_per_kwh"]) == pytest.approx(on_bands, abs=5e-5)


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
    from custom_components.be_electricity_prices.compare_quote import (
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


def test_compare_ytd_prorates_capacity_per_month_like_the_live_sensor() -> None:
    """The Flanders capacity leg accrues per month, not by day-of-year.

    _ytd_capacity sums each month's charge prorated by its OWN days, so at a
    month end the accrual is a whole number of monthly charges. The what-if
    scaled the entire fee block by days_elapsed / days_in_year instead, which
    drifts inside the year against the very sensor it is meant to sit beside.
    The prosumer term already had this override; capacity was left behind."""
    from custom_components.be_electricity_prices.compare_quote import _annual_bill
    from custom_components.be_electricity_prices.providers.base import DsoOverlay
    from tests import make_entry, make_snapshot

    snap = make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                capacity_eur_per_kw_year=50.0,
            )
        },
    )
    entry = make_entry(region="flanders", dso="fluvius_antwerpen", solar_regime="none")

    def _bill(fee_proration: float, capacity_proration: float | None) -> float:
        return _annual_bill(
            snap,
            entry,
            4.5,  # billed peak kW
            0.0,  # per-kWh isolated away
            0.0,
            capacity_proration=capacity_proration,
            fee_proration=fee_proration,
        )

    # End of February: two whole monthly charges, 2 * 50 * 4.5 / 12 = 37.50.
    days_elapsed, days_in_year = 59, 365
    uniform = _bill(days_elapsed / days_in_year, None)
    per_month = _bill(days_elapsed / days_in_year, 2.0)
    assert per_month - uniform == pytest.approx(
        225.0 * (2.0 / 12.0 - days_elapsed / days_in_year)
    )
    # And the corrected figure is the live sensor's whole-month accrual.
    assert per_month == pytest.approx(37.50, abs=0.01)


def test_compare_injection_credit_weights_slots_by_export_shape() -> None:
    """A per-slot feed-in credit must be weighted by what the panels export.

    Slot duration is the right weighting for a quantity that flows evenly
    through the day and the wrong one for solar. The 01:00-07:00 off-peak
    block is about a third of the clock and exports nothing, so averaging the
    triplet by hours always credits less than the year-to-date walk pays,
    which resolves each hour's own slot and multiplies by that hour's exported
    kWh. Measured over a year of modelled Brussels export on this card the gap
    was 11,22 EUR on 3500 kWh, and always in the same direction."""
    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        TimeOfUseRates,
    )
    from tests import make_entry, make_snapshot

    # The Engie Empower Flextime triplet tests/test_engie.py asserts.
    snap = make_snapshot(
        supplier="engie",
        contract="engie_empower_flextime",
        energy=TimeOfUseRates(
            peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_no_peak"
        ),
        injection=InjectionRates(peak=0.08417, transition=0.04834, offpeak=0.01465),
    )
    entry = make_entry(solar_regime="injection", injection_kwh="sensor.inj")

    by_duration = _compare_injection_credit(snap, entry, {}, None)
    # A daylight export shape: nothing overnight, concentrated around midday.
    daylight = {
        h: w
        for h, w in {
            7: 0.02,
            8: 0.05,
            9: 0.08,
            10: 0.11,
            11: 0.13,
            12: 0.14,
            13: 0.13,
            14: 0.11,
            15: 0.08,
            16: 0.06,
            17: 0.05,
            18: 0.03,
            19: 0.01,
        }.items()
    }
    by_export = _compare_injection_credit(snap, entry, {}, None, None, daylight)

    assert by_duration is not None and by_export is not None
    # The duration mean is dragged down by an off-peak block that exports
    # nothing, so it must credit strictly less.
    assert by_export > by_duration
    # An empty profile is refused rather than dividing by zero.
    assert _compare_injection_credit(snap, entry, {}, None, None, {}) == pytest.approx(
        by_duration
    )


def test_compare_tou_weights_by_measured_consumption_not_clock_hours() -> None:
    """A time-of-use estimate must weight the slots by the kWh they bill.

    Averaging the slot rates over clock hours assumes a household that
    consumes uniformly around the clock. None does: an evening-heavy
    residential profile puts far more of its kWh in the peak band than the
    share of the week those hours occupy, so a peak-expensive card was quoted
    well under what the sensor beside it bills. The live year-to-date has
    always weighted each hour by its own kWh."""
    from custom_components.be_electricity_prices.compare_quote import (
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
    when = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    args = (snap, "ores", "wallonia", when, None, "dynamic", "bi_horaire")

    flat = _tou_weighted_per_kwh(*args)
    # All consumption in the evening peak block.
    evening = _tou_weighted_per_kwh(*args, {h: 0.25 for h in (18, 19, 20, 21)})
    # All consumption overnight.
    night = _tou_weighted_per_kwh(*args, {h: 0.25 for h in (1, 2, 3, 4)})

    assert flat is not None and evening is not None and night is not None
    # The profile has to move the number, and in the right direction.
    assert night < flat < evening
    # A uniform profile is the clock-hour weighting, so it must not move it.
    uniform = _tou_weighted_per_kwh(*args, {h: 1.0 / 24 for h in range(24)})
    assert uniform == pytest.approx(flat)


def test_compare_tou_weights_bihoraire_network_over_full_week() -> None:
    # For a TOU contract on a bi-horaire DSO network, the annual estimate
    # must stay independent of the dialog-open hour and blend the network
    # bands across the week (the energy TOU slots and the bi-horaire network
    # bands don't align, so a single sample per energy slot mis-prices the
    # network).
    from custom_components.be_electricity_prices.compare_quote import (
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
    from custom_components.be_electricity_prices.compare_quote import (
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
    from custom_components.be_electricity_prices.compare_quote import (
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
    from custom_components.be_electricity_prices.flow_schemas import _solar_schema
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


def test_compare_spot_indexed_injection_weights_the_window_by_export() -> None:
    """A spot-indexed credit is priced over the whole window, never at the slot
    the dialog happened to open in, and averaged by when the panels export.

    The annual bill multiplies one rate by the year's exported kWh, and export
    is not spread evenly around the clock: it is nothing all night and peaks
    at midday, which on a day-ahead curve is the trough. Weighting by the
    household's own measured shape is the basis the TOU branch beside it uses
    and the one current_year_cost bills on."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
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
    curve = [
        0.078, 0.070, 0.065, 0.062, 0.065, 0.072, 0.085, 0.095,
        0.080, 0.040, 0.020, 0.010, 0.008, 0.006, 0.010, 0.020,
        0.035, 0.050, 0.070, 0.095, 0.120, 0.135, 0.110, 0.090,
    ]  # fmt: skip
    spot_dict = {
        datetime(2026, 4, 29, h, 0, tzinfo=UTC): v for h, v in enumerate(curve)
    }
    avg_spot = sum(curve) / len(curve)

    # No measured export shape: every slot weighs the same, which for this
    # affine formula is exactly the formula at the window mean, so an entry
    # with no injection history is quoted what it always was.
    assert _compare_injection_credit(
        snap, entry, spot_dict, avg_spot=avg_spot
    ) == pytest.approx(0.97 * avg_spot - 0.021)

    # A daylight export shape, keyed by LOCAL hour (April is CEST, so local
    # hour h reads UTC h-2).
    shape = {8: 0.1, 10: 0.2, 12: 0.4, 14: 0.2, 16: 0.1}
    weighted = sum(w * (0.97 * curve[h - 2] - 0.021) for h, w in shape.items())
    credit = _compare_injection_credit(
        snap, entry, spot_dict, avg_spot=avg_spot, inj_hour_weights=shape
    )
    assert credit is not None
    assert credit == pytest.approx(weighted)
    # Midday is the cheap end of the curve, so the shape has to pull the
    # credit below the clock mean rather than leave it alone.
    assert credit < 0.97 * avg_spot - 0.021
    # And never the slot the dialog opened in.
    assert credit != pytest.approx(0.97 * curve[0] - 0.021)


def test_compare_prices_a_dynamic_energy_leg_on_the_consumption_shape() -> None:
    """A dynamic bill is the sum over slots of kWh times that slot's rate, so
    the one spot that stands for the year is the mean weighted by when the
    household actually draws.

    The clock mean assumes a household that consumes uniformly around the
    clock. It does not: consumption is evening-heavy while the day-ahead curve
    troughs at midday, so the clock mean under-quotes the energy leg."""
    from custom_components.be_electricity_prices.compare_quote import (
        _consumption_weighted_spot,
    )

    curve = [
        0.078, 0.070, 0.065, 0.062, 0.065, 0.072, 0.085, 0.095,
        0.080, 0.040, 0.020, 0.010, 0.008, 0.006, 0.010, 0.020,
        0.035, 0.050, 0.070, 0.095, 0.120, 0.135, 0.110, 0.090,
    ]  # fmt: skip
    spot_dict = {
        datetime(2026, 4, 29, h, 0, tzinfo=UTC): v for h, v in enumerate(curve)
    }
    clock_mean = sum(curve) / len(curve)

    # No measured shape: the clock mean, exactly as every quote had it.
    assert _consumption_weighted_spot(spot_dict, None) == pytest.approx(clock_mean)

    # An evening-heavy residential shape, keyed by LOCAL hour (April is CEST,
    # so local hour h reads UTC h-2), landing on the evening peak of the curve.
    shape = {21: 0.3, 22: 0.4, 23: 0.3}
    weighted = sum(w * curve[h - 2] for h, w in shape.items())
    got = _consumption_weighted_spot(spot_dict, shape)
    assert got == pytest.approx(weighted)
    # Those are the expensive hours, so the leg is quoted ABOVE the clock mean.
    assert got is not None and got > clock_mean

    # An empty window still has no answer.
    assert _consumption_weighted_spot({}, shape) is None


def test_compare_floored_injection_averages_the_slot_rates() -> None:
    """A never-negative feed-in formula is convex, so the window mean does not
    price it.

    Flooring once at the mean pays nothing for a day whose spot only went
    under the floor around midday, which is when the panels export, while the
    entry's own injection_price sensor and its year-to-date credit both floor
    per slot. The page has to quote the number the entry bills."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DynamicRates,
        InjectionRates,
        SpotMonthlyRates,
    )
    from tests import make_snapshot

    inj = InjectionRates(
        current=None, factor=0.96, base=-0.009, formula="x", floor_at_zero=True
    )
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    # A plain spring day: a negative midday block, positive shoulders.
    curve = [
        0.078, 0.070, 0.065, 0.062, 0.065, 0.072, 0.085, 0.095,
        0.080, 0.040, 0.005, -0.010, -0.025, -0.030, -0.028, -0.015,
        0.002, 0.025, 0.060, 0.095, 0.120, 0.135, 0.110, 0.090,
    ]  # fmt: skip
    spot_dict = {
        datetime(2026, 4, 29, h, 0, tzinfo=UTC): v for h, v in enumerate(curve)
    }
    avg_spot = sum(curve) / len(curve)
    per_slot = sum(max(0.96 * v - 0.009, 0.0) for v in curve) / len(curve)

    snap = make_snapshot(energy=DynamicRates(factor=1.0, base=0.0), injection=inj)
    credit = _compare_injection_credit(snap, entry, spot_dict, avg_spot=avg_spot)
    assert credit is not None
    assert credit == pytest.approx(per_slot)
    # Strictly better than flooring the mean, which is the bug being fixed.
    assert credit > max(0.96 * avg_spot - 0.009, 0.0)

    # A month-mean contract keeps the other order: its card publishes ONE
    # tariff for the delivery month and the guarantee is written against that
    # number, so flooring after the mean is what it actually bills.
    monthly = make_snapshot(
        energy=SpotMonthlyRates(factor=1.0, base=0.0), injection=inj
    )
    assert _compare_injection_credit(
        monthly, entry, spot_dict, avg_spot=avg_spot
    ) == pytest.approx(max(0.96 * avg_spot - 0.009, 0.0))

    # Without the floor the formula is affine and both orders agree, so every
    # existing quote is unchanged.
    unfloored = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0),
        injection=InjectionRates(current=None, factor=0.96, base=-0.009, formula="x"),
    )
    assert _compare_injection_credit(
        unfloored, entry, spot_dict, avg_spot=avg_spot
    ) == pytest.approx(0.96 * avg_spot - 0.009)

    # No curve and no mean: the credit stays unresolved rather than guessed.
    assert _compare_injection_credit(snap, entry, {}, avg_spot=None) is None

    # And the export shape applies to the clamped rates too, which is where it
    # matters most: the clamp bites in the midday hours the panels export into.
    shape = {8: 0.1, 10: 0.2, 12: 0.4, 14: 0.2, 16: 0.1}
    weighted = sum(w * max(0.96 * curve[h - 2] - 0.009, 0.0) for h, w in shape.items())
    assert _compare_injection_credit(
        snap, entry, spot_dict, avg_spot=avg_spot, inj_hour_weights=shape
    ) == pytest.approx(weighted)
    assert weighted < per_slot


def test_compare_prices_a_slot_indexed_credit_off_the_window_not_the_clock(
    freezer: Any,
) -> None:
    """A card that settles per slot beside a printed illustration is quoted
    from the window, not from whichever hour the dialog opened in.

    Every Bolt fixed and variable card carries this shape: a printed
    indicative kept only as the fallback for an entry with no ENTSO-E key,
    beside the Belpex formula the card says it really bills. The branch that
    prices a spot formula used to ask only "is the energy dynamic, or is
    there no printed figure", so these cards fell past it into the live
    helper, which resolves the credit at the current slot. That valued a
    whole year of export at one hour's spot, and the answer moved every time
    the page was reopened."""
    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import InjectionRates
    from tests import make_entry, make_snapshot

    # The Bolt shape tests/test_bolt.py pins: factor < 1, a negative base and
    # the printed figure kept beside them.
    inj = InjectionRates(
        current=0.0531,
        factor=0.94,
        base=-0.01133,
        formula="Belpex * 0,94 - 11,33",
        slot_indexed=True,
    )
    snap = make_snapshot(supplier="bolt", contract="bolt_variable", injection=inj)
    entry = make_entry(solar_regime="injection", injection_kwh="sensor.inj")

    curve = [
        0.078, 0.070, 0.065, 0.062, 0.065, 0.072, 0.085, 0.095,
        0.080, 0.040, 0.005, -0.010, -0.025, -0.030, -0.028, -0.015,
        0.002, 0.025, 0.060, 0.095, 0.120, 0.135, 0.110, 0.090,
    ]  # fmt: skip
    spot_dict = {
        datetime(2026, 4, 29, h, 0, tzinfo=UTC): v for h, v in enumerate(curve)
    }
    avg_spot = sum(curve) / len(curve)

    # Unfloored and unweighted, the window mean of the slot rates is exactly
    # the formula at the mean spot.
    expected = 0.94 * avg_spot - 0.01133
    freezer.move_to("2026-04-29 13:00:00+02:00")
    midday = _compare_injection_credit(snap, entry, spot_dict, avg_spot=avg_spot)
    assert midday is not None
    assert midday == pytest.approx(expected)

    # The whole point: reopening the page at a different hour must not move
    # the yearly credit. The midday block of this curve is negative and the
    # evening peak is the day's highest, so before the fix these two differed
    # by more than 15 c/kWh.
    freezer.move_to("2026-04-29 21:00:00+02:00")
    evening = _compare_injection_credit(snap, entry, spot_dict, avg_spot=avg_spot)
    assert evening == pytest.approx(midday)

    # And it is the formula that is quoted, not the illustration beside it.
    assert midday != pytest.approx(0.0531)

    # The export shape still applies, as it does for every other spot formula.
    shape = {12: 0.5, 13: 0.5}
    weighted = sum(w * (0.94 * curve[h - 2] - 0.01133) for h, w in shape.items())
    assert _compare_injection_credit(
        snap, entry, spot_dict, avg_spot=avg_spot, inj_hour_weights=shape
    ) == pytest.approx(weighted)


def test_compare_prices_an_spp_indexed_credit_on_the_solar_weighted_mean() -> None:
    """energie.be Variabel and Vast index the feed-in on Belpex_SPP.

    Quoting the card's printed indicative here made the compare page
    contradict the user's own injection_price sensor, which resolves the
    formula against the solar-weighted month mean.
    """
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )
    from tests import make_snapshot

    snap = make_snapshot(
        energy=FixedRates(single=0.1826),
        injection=InjectionRates(
            current=0.0343, factor=0.6, base=-0.008, spp_indexed=True
        ),
    )
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    spot_dict = {datetime(2026, 4, 29, h, 0, tzinfo=UTC): 0.20 for h in range(24)}
    credit = _compare_injection_credit(
        snap, entry, spot_dict, avg_spot=0.20, spp_spot=0.0292
    )
    assert credit == pytest.approx(0.6 * 0.0292 - 0.008)


def test_compare_keeps_the_indicative_when_the_spp_profile_is_missing() -> None:
    """The plain window mean is a DIFFERENT index, not a coarser one: pricing
    an SPP formula off it roughly doubles the credit in a sunny month. With no
    profile the card's own printed indicative is the honest answer."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )
    from tests import make_snapshot

    snap = make_snapshot(
        energy=FixedRates(single=0.1826),
        injection=InjectionRates(
            current=0.0343, factor=0.6, base=-0.008, spp_indexed=True
        ),
    )
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    credit = _compare_injection_credit(snap, entry, {}, avg_spot=0.20, spp_spot=None)
    assert credit == pytest.approx(0.0343)


def test_compare_tou_injection_uses_weighted_average_across_slots() -> None:
    # A per-slot TOU injection credit (Engie Empower Flextime) must be
    # time-averaged over the published slot durations, not returned as the
    # live current-slot rate the way the live helper would.
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
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
        "custom_components.be_electricity_prices.compare_flow._compare_supplier_options",
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
    from custom_components.be_electricity_prices.compare_quote import _annual_fees
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
    from custom_components.be_electricity_prices.compare_quote import _annual_fees
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


async def test_compare_prosumer_term_matches_the_live_ytd_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The compare quote pro-rates every EUR/year fee by a fraction of a year,
    then corrects the prosumer term onto the per-month shape the live sensor
    uses. ``prosumer_proration`` counts MONTHS, so it has to be scaled to a
    year fraction first; without that the already-annual prosumer fee was
    multiplied by a month count and the quote came out 12x over.

    Pinned against ``_ytd_prosumer`` itself rather than a hardcoded number, so
    the two stay tied together. The stub DSO must publish a prosumer rate:
    the pre-existing options-flow stubs leave it None, which zeroes the whole
    term and is exactly why this went unnoticed.
    """
    from custom_components.be_electricity_prices.compare_quote import _annual_bill
    from custom_components.be_electricity_prices.ytd_cost import _ytd_prosumer
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
        TaxOverlay,
    )
    from tests import make_snapshot

    freezer.move_to("2026-07-31 12:00:00+02:00")
    today = date(2026, 7, 31)

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_kva": 5.0,
            "solar_regime": "compensation",
        },
        title="prosumer compare",
    )
    entry.add_to_hass(hass)

    snapshot = make_snapshot(
        energy=FixedRates(single=0.18, yearly_fixed_fee=0.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=82.0,
            )
        },
        taxes=TaxOverlay(federal_excise=0.0, energy_contribution=0.0),
    )

    jan1 = date(2026, 1, 1)
    days_in_year = (date(2027, 1, 1) - jan1).days
    fee_proration = ((today - jan1).days + 1) / days_in_year
    prosumer_proration = (today.month - 1) + today.day / 31

    # No consumption and no other fee, so the quote is the prosumer term alone.
    quoted = _annual_bill(
        snapshot,
        entry,
        per_kwh=0.0,
        consumption_kwh=0.0,
        injection_kwh=0.0,
        peak_kw=0.0,
        meter="mono",
        fee_proration=fee_proration,
        prosumer_proration=prosumer_proration,
    )

    live = await _ytd_prosumer(hass, MagicMock(), MagicMock(), snapshot, entry, today)
    assert live > 0.0
    assert quoted == pytest.approx(live, rel=1e-9)


async def test_editing_out_of_wallonia_drops_the_impact_tariff_mode(
    hass: HomeAssistant,
) -> None:
    """Tarif Impact is Wallonia-only and the step is skipped elsewhere, but
    nothing popped the key and the options flow writes its data verbatim, so a
    Walloon entry edited to Flanders kept dso_tariff_mode='impact'. The network
    side falls through harmlessly (no Impact triplet outside Wallonia), but
    _routed_rate still routes the ENERGY leg through dso_impact_band, which
    bills 11:00-17:00 off-peak where Flanders says peak and 22:00-01:00 peak
    where it says off-peak."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "bi",
            "dso_tariff_mode": "impact",
            "solar_kva": 0.0,
            "solar_regime": "none",
        },
        title="was walloon",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
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
        result["flow_id"], {"meter": "bi"}
    )
    # Flanders skips the dso_tariff_mode step entirely.
    assert result["step_id"] != "dso_tariff_mode"
    # Walk whatever remains (Flanders adds a capacity step) to the end.
    answers: dict[str, dict[str, Any]] = {
        "capacity": {"capacity_mode": "fixed", "capacity_fixed_kw": 0.0},
        "solar": {"solar_kva": 0.0, "solar_regime": "none"},
        "meters": {},
    }
    while result["type"] == data_entry_flow.FlowResultType.FORM:
        step = result["step_id"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], answers.get(step, {})
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert entry.data["region"] == "flanders"
    assert "dso_tariff_mode" not in entry.data


async def test_blanking_a_meter_picker_actually_clears_it(hass: HomeAssistant) -> None:
    """ha-form omits a blanked selector from user_input entirely, and
    voluptuous then re-injects a `default`, so a wired kWh or capacity-peak
    sensor came straight back and could never be unwired. The stored id is a
    suggestion now, and the step handler pops whatever the user cleared."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "dso_tariff_mode": "simple",
            "solar_kva": 0.0,
            "solar_regime": "none",
            "consumption_kwh": "sensor.old_total",
            "injection_kwh": "sensor.old_inj",
        },
        title="wired meters",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "edit"}
    )
    answers: dict[str, dict[str, Any]] = {
        "edit": {"supplier": "eneco", "region": "wallonia"},
        "contract": {"contract": "power_fix"},
        "dso": {"dso": "ores"},
        "meter": {"meter": "mono"},
        "dso_tariff_mode": {"dso_tariff_mode": "simple"},
        "solar": {"solar_kva": 0.0, "solar_regime": "none"},
        "meters": {},  # every picker blanked
    }
    while result["type"] == data_entry_flow.FlowResultType.FORM:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], answers.get(result["step_id"], {})
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    assert "consumption_kwh" not in entry.data
    assert "injection_kwh" not in entry.data


async def test_compare_contract_step_fills_its_supplier_placeholder(
    hass: HomeAssistant,
) -> None:
    """The compare_contract description reads 'Pick a contract from {supplier}.'
    but the step passed no description_placeholders, so HA handed the frontend
    None and the user saw the literal token."""
    import json
    from pathlib import Path

    strings = json.loads(
        (Path("custom_components/be_electricity_prices/strings.json")).read_text(
            encoding="utf-8"
        )
    )
    desc = strings["options"]["step"]["compare_contract"]["description"]
    assert "{supplier}" in desc

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "solar_kva": 0.0,
            "solar_regime": "none",
        },
        title="compare placeholders",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare"}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"supplier": "bolt"}
    )
    assert result["step_id"] == "compare_contract"
    placeholders = result.get("description_placeholders") or {}
    assert placeholders.get("supplier"), "step must fill {supplier}"
    assert "{" not in placeholders["supplier"]


async def test_professional_step_only_shows_for_a_pro_contract(
    hass: HomeAssistant,
) -> None:
    """The VAT treatment and the yearly volume are questions only a
    professional card raises; a residential entry must never be asked."""
    from custom_components.be_electricity_prices.config_flow import (
        _contract_is_professional,
        _professional_schema,
    )

    assert _contract_is_professional("engie", "engie_pro_easy_variable") is True
    assert _contract_is_professional("engie", "engie_easy_variable") is False
    assert _contract_is_professional("engie", "no_such_contract") is False
    assert _contract_is_professional(None, None) is False

    keys = {str(k.schema) for k in _professional_schema({}).schema}
    assert keys == {CONF_INCLUDE_VAT, CONF_ANNUAL_CONSUMPTION_KWH}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_resolves_the_quote_through_the_shared_resolver(
    hass: HomeAssistant,
) -> None:
    """The quoted card must be resolved against this entry's own site facts,
    not just its VAT preference.

    The compare flow called `apply_vat` alone, so a banded professional card
    was priced at its FIRST excise tier however much the household actually
    uses: 1,421 c€/kWh instead of 1,139 at 60 000 kWh/yr, about 169 EUR/yr
    against the alternative. The user's own side comes off the coordinator and
    IS fully resolved, so the comparison was biased. Both transforms live in
    `snapshot_store._resolve_snapshot`; compare must go through it.
    """
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    other_snap = _stub_snapshot("cociter", "cociter_variable", 0.16)
    fake = replace(
        EXTRACTORS["cociter"], fetch=AsyncMock(return_value=other_snap), probe=None
    )

    seen: list[Any] = []
    real = snapshot_store._resolve_snapshot

    def _spy(cfg_entry: Any, snap: Any) -> Any:
        seen.append((cfg_entry, snap))
        return real(cfg_entry, snap)

    with (
        patch.dict(EXTRACTORS, {"cociter": fake}),
        patch.object(snapshot_store, "_resolve_snapshot", _spy),
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
        assert result["step_id"] == "compare_meter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        assert result["step_id"] == "compare_result"

    assert seen, "compare did not resolve the quote through _resolve_snapshot"
    used_entry, used_snap = seen[-1]
    assert used_entry is entry
    assert used_snap is other_snap


def test_region_mismatch_is_a_form_error_not_an_abort() -> None:
    """Supplier and region are picked on the same step, so the mismatch can
    only be judged once both are in.

    Detecting it a step later and aborting ended the flow, and in the options
    flow that discarded every other change made in the same run. The abort
    text even said "go back and pick a different combination", which HA gives
    no way to do from an abort.
    """
    from custom_components.be_electricity_prices.config_flow import (
        _region_mismatch_error,
    )
    from custom_components.be_electricity_prices.const import CONF_REGION, CONF_SUPPLIER

    # Eneco publishes no Brussels contract.
    assert _region_mismatch_error(
        {CONF_SUPPLIER: "eneco", CONF_REGION: "brussels"}
    ) == {CONF_SUPPLIER: "supplier_region_unavailable"}
    # A combination that exists is fine.
    assert (
        _region_mismatch_error({CONF_SUPPLIER: "eneco", CONF_REGION: "wallonia"})
        is None
    )
    # Half-filled data cannot be judged yet.
    assert _region_mismatch_error({CONF_SUPPLIER: "eneco"}) is None
    assert _region_mismatch_error({}) is None
    # An unknown supplier is not this check's business.
    assert (
        _region_mismatch_error({CONF_SUPPLIER: "nope", CONF_REGION: "wallonia"}) is None
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_prices_a_spot_monthly_side_on_the_delivery_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A monthly-indexed contract bills one flat rate per delivery month.

    Quoting it at the mean of the fetched day-ahead window instead is not an
    approximation of what it bills, it is a different number - and one that
    moves day to day while the contract's does not, so the page contradicts
    the user's own current_price sensor and the annual delta swings with
    whichever day the dialog happened to be opened on.

    The month is seeded at 0.10 EUR/kWh and today's curve at 0.02 so the two
    candidate answers are far apart and the wrong one is unmistakable.
    """
    from dataclasses import replace
    from datetime import timedelta

    from custom_components.be_electricity_prices.pricing import compute_breakdown
    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
    )
    from tests import make_entry, make_snapshot

    # The user is on the expert custom monthly-average contract; the target is
    # an ordinary static one. Only the current side is spot-monthly here, which
    # is exactly the case a shared day-ahead mean gets wrong: the other side
    # does not move with spot, so the delta is not self-cancelling.
    entry = make_entry(supplier="custom", contract="custom_monthly")
    entry.add_to_hass(hass)
    own = make_snapshot(
        supplier="custom",
        contract="custom_monthly",
        energy=SpotMonthlyRates(factor=1.10, base=0.01, yearly_fixed_fee=50.0),
    )
    entry.runtime_data = _real_coordinator(hass, entry, own)

    # Pinned mid-month: the month-to-date window below runs from the 1st up to
    # today, so on the 1st it is empty and the "month-to-date dominates" check
    # reads today's day-ahead window alone.
    freezer.move_to("2026-08-14 12:00:00+02:00")
    now_local = dt_util.now()
    month_start = now_local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    today_start = dt_util.start_of_local_day().astimezone(UTC)
    coord = entry.runtime_data
    hist: dict[datetime, float] = {}
    t = month_start
    while t < today_start:
        hist[t] = 0.10
        t += timedelta(hours=1)
    coord._historical_spots = hist
    coord._spot_cache = {today_start + timedelta(hours=h): 0.02 for h in range(24)}
    month_mean = coord._monthly_spot_mean(
        now_local.year, now_local.month, coord._spot_cache
    )
    assert month_mean is not None and month_mean > 0.05

    other = _stub_snapshot("eneco", "power_fix", 0.18)
    fake = replace(EXTRACTORS["eneco"], fetch=AsyncMock(return_value=other), probe=None)
    with patch.dict(EXTRACTORS, {"eneco": fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "eneco"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "power_fix"}
        )
        if result["step_id"] == "compare_meter":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"meter": "mono"}
            )
        result = await _pass_compare_solar(hass, entry, result)
        if result["step_id"] == "compare_api_key":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"api_key": "valid-token"}
            )
        assert result["step_id"] == "compare_result"
        ph = result["description_placeholders"]
        assert ph is not None
        at_month_mean = compute_breakdown(
            own, "ores", "wallonia", now_local, month_mean, "mono"
        ).all_in
        at_day_mean = compute_breakdown(
            own, "ores", "wallonia", now_local, 0.02, "mono"
        ).all_in
        assert ph["current_per_kwh"] != "-"
        assert float(ph["current_per_kwh"]) == pytest.approx(at_month_mean, abs=2e-3)
        assert float(ph["current_per_kwh"]) != pytest.approx(at_day_mean, abs=2e-3)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_branch_static_to_spot_monthly_prompts_for_api_key(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A spot-monthly target needs a spot just as much as a dynamic one.

    Its energy leg is `factor x monthly_mean(spot) + base` with no resolved
    rate on the card, so without a spot the quote has nothing to price and
    the result page would render a bare "-" for the contract the user asked
    about. The gate keyed on the dynamic kind alone until energie.be
    Variabel became the first scraped spot-monthly contract.
    """
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        FixedRates,
        InjectionRates,
        SpotMonthlyRates,
    )
    from custom_components.be_electricity_prices.pricing import compute_breakdown
    from tests import make_entry, make_snapshot

    # energie.be sells in Flanders only, so the current side has to be a
    # Flemish entry for it to appear in the compare supplier picker at all.
    flemish_dsos = {
        "fluvius_antwerpen": DsoOverlay(distribution_single=0.10, transport=0.0145)
    }
    entry = make_entry(region="flanders", dso="fluvius_antwerpen")
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass,
        entry,
        make_snapshot(
            supplier="eneco",
            contract="power_fix",
            energy=FixedRates(single=0.18, yearly_fixed_fee=60.0),
            dsos=flemish_dsos,
            source_url="test://stub",
            publication_label="april 2026",
        ),
    )
    other_snap = make_snapshot(
        supplier="energiebe",
        contract="energiebe_variable",
        energy=SpotMonthlyRates(factor=1.1872, base=0.00848, yearly_fixed_fee=35.0),
        dsos=flemish_dsos,
        injection=InjectionRates(current=0.0343),
        source_url="test://stub",
        publication_label="augustus 2026",
    )
    # Month-to-date spots at 0.10 EUR/kWh, today's day-ahead window at 0.02:
    # the delivery-month mean and the day-ahead mean must not be confusable.
    from datetime import timedelta

    # Pinned mid-month: the month-to-date window below runs from the 1st up to
    # today, so on the 1st it is empty and the "month-to-date dominates" check
    # reads today's day-ahead window alone.
    freezer.move_to("2026-08-14 12:00:00+02:00")
    now_local = dt_util.now()
    month_start = now_local.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(UTC)
    today_start = dt_util.start_of_local_day().astimezone(UTC)
    coord = entry.runtime_data
    hist: dict[datetime, float] = {}
    t = month_start
    while t < today_start:
        hist[t] = 0.10
        t += timedelta(hours=1)
    coord._historical_spots = hist
    coord._spot_cache = {today_start + timedelta(hours=h): 0.02 for h in range(24)}
    # What the coordinator itself would bill this month at, and what quoting
    # off the day-ahead window alone would produce instead.
    month_mean = coord._monthly_spot_mean(
        now_local.year, now_local.month, coord._spot_cache
    )
    assert month_mean is not None and month_mean > 0.05  # month-to-date dominates

    fake = replace(
        EXTRACTORS["energiebe"], fetch=AsyncMock(return_value=other_snap), probe=None
    )
    with patch.dict(EXTRACTORS, {"energiebe": fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": "energiebe"}
        )
        assert result["step_id"] == "compare_contract"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": "energiebe_variable"}
        )
        # Unlike a dynamic target this one does NOT lock the meter: a
        # monthly-indexed rate is flat across the day, so mono / bi-hourly
        # billing stays a real choice.
        assert result["step_id"] == "compare_meter"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"meter": "mono"}
        )
        result = await _pass_compare_solar(hass, entry, result)
        assert result["step_id"] == "compare_api_key"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"api_key": "valid-token"}
        )
        assert result["step_id"] == "compare_result"
        # Reaching the page is not the point: the key is collected so the
        # spot-monthly side can actually be PRICED. Without the widened
        # need_spot gate the quote arrives with no spot at all and the target
        # renders a bare "-", which is the failure this whole branch exists to
        # avoid.
        ph = result["description_placeholders"]
        assert ph is not None
        assert ph["compare_per_kwh"] != "-"
        assert ph["compare_annual"] != "-"
        assert ph["delta_annual"] != "-"
        # And priced on the DELIVERY MONTH's mean, not on the day-ahead window.
        # The month is seeded at 0.10 EUR/kWh and today's curve at 0.02, so the
        # two answers are far apart and the wrong one is unmistakable. A
        # spot-monthly contract bills one flat rate per month; quoting it off a
        # single day makes the page swing with the day it was opened and
        # contradict the user's own current_price sensor.
        at_month_mean = compute_breakdown(
            other_snap, "fluvius_antwerpen", "flanders", now_local, month_mean, "mono"
        ).all_in
        at_day_mean = compute_breakdown(
            other_snap, "fluvius_antwerpen", "flanders", now_local, 0.02, "mono"
        ).all_in
        assert float(ph["compare_per_kwh"]) == pytest.approx(at_month_mean, abs=2e-3)
        assert float(ph["compare_per_kwh"]) != pytest.approx(at_day_mean, abs=2e-3)


def test_compensation_prices_each_side_of_the_netting_on_its_own_shape() -> None:
    """A reversing meter nets against the rate in force at the time, which is
    what the live sensor bills: it nets each hour and clamps the year once.

    Netting the two annual totals first and pricing the residue at the
    CONSUMPTION-weighted rate prices exported kWh at hours they were never
    produced in, and the two shapes are opposites: evening-heavy consumption
    against a midday export bell."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import _annual_bill
    from tests import make_snapshot

    snap = make_snapshot()
    entry = SimpleNamespace(
        data={"solar_regime": "compensation", "dso": "ores", "region": "wallonia"}
    )
    # Evening-heavy draw is dearer than the midday export it nets against.
    per_kwh, export_per_kwh = 0.36, 0.30
    cons, inj = 3500.0, 2500.0

    split = _annual_bill(
        snap,
        entry,  # type: ignore[arg-type]
        0.0,
        per_kwh,
        cons,
        inj,
        export_per_kwh=export_per_kwh,
    )
    lumped = _annual_bill(snap, entry, 0.0, per_kwh, cons, inj)  # type: ignore[arg-type]
    fees = _annual_bill(snap, entry, 0.0, per_kwh, 0.0, 0.0)  # type: ignore[arg-type]

    # Each side on its own rate: 3500 x 0,36 - 2500 x 0,30.
    assert split - fees == pytest.approx(3500 * 0.36 - 2500 * 0.30)
    # Netting first and pricing the residue at the consumption rate is a
    # different, lower number: 1000 x 0,36.
    assert lumped - fees == pytest.approx(1000 * 0.36)
    assert split > lumped

    # Surplus export is still forfeited, never paid out.
    surplus = _annual_bill(
        snap,
        entry,  # type: ignore[arg-type]
        0.0,
        per_kwh,
        1000.0,
        9000.0,
        export_per_kwh=export_per_kwh,
    )
    assert surplus == pytest.approx(fees)

    # Without a measured export shape the quote is exactly what it was.
    assert _annual_bill(
        snap,
        entry,  # type: ignore[arg-type]
        0.0,
        per_kwh,
        cons,
        inj,
    ) == pytest.approx(lumped)


def test_compare_asks_the_raw_snapshot_whether_the_credit_is_monthly() -> None:
    """The compare page splices a cohort's SpotMonthlyRates ENERGY leg onto
    the current side, so a Cociter Variable entry arrives looking month-mean
    priced while its injection is still the hourly BELPEX formula - note (9)
    "le prix de l'injection varie chaque heure" against note (7)'s monthly
    consumption.

    Judged on the spliced snapshot the credit falls onto the flat window mean,
    which weighs every hour of the clock alike and ignores that panels export
    into the midday trough. That understates the user's OWN bill and so biases
    the comparison toward staying put.
    """
    from datetime import UTC, datetime as dt
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        InjectionRates,
        SpotMonthlyRates,
        SupplierSnapshot,
        TaxOverlay,
        VariableRates,
    )

    def _snap(energy: Any) -> SupplierSnapshot:
        return SupplierSnapshot(
            supplier="cociter",
            contract="cociter_variable",
            energy=energy,
            injection=InjectionRates(current=None, factor=0.97, base=-0.021),
            dsos={
                "ores": DsoOverlay(
                    distribution_single=0.1,
                    transport=0.02,
                    data_management_per_year=0.0,
                )
            },
            taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
            source_url="t://x",
        )

    raw = _snap(VariableRates(current=0.12))
    spliced = _snap(SpotMonthlyRates(factor=0.795, base=0.053))
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    # A midday trough with an export shape that lives in it.
    curve = {
        dt(2026, 4, 15, h, tzinfo=UTC): (0.02 if 10 <= h <= 15 else 0.14)
        for h in range(24)
    }
    weights = {h: (3.0 if 10 <= h <= 15 else 0.1) for h in range(24)}
    avg = sum(curve.values()) / len(curve)

    on_window_mean = _compare_injection_credit(
        spliced,
        entry,  # type: ignore[arg-type]
        curve,
        avg,
        None,
        weights,
    )
    export_weighted = _compare_injection_credit(
        spliced,
        entry,  # type: ignore[arg-type]
        curve,
        avg,
        None,
        weights,
        raw_snapshot=raw,
    )
    assert on_window_mean is not None and export_weighted is not None
    # Weighting by when the panels actually export lands well below the flat
    # window mean, because the export sits in the midday trough. The exact
    # figure depends on the local-hour mapping; the direction and the size of
    # the gap are the point.
    assert export_weighted < on_window_mean
    assert on_window_mean - export_weighted > 0.02
    # And it equals what the shared export-weighting helper computes, so the
    # raw snapshot really did route through that branch.
    from custom_components.be_electricity_prices.compare_quote import (
        _export_weighted_credit,
    )

    assert spliced.injection is not None
    assert export_weighted == pytest.approx(
        _export_weighted_credit(spliced.injection, curve, weights)
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_offers_your_own_contract(hass: HomeAssistant) -> None:
    """The picker used to exclude the user's own contract, on the grounds
    that quoting it against itself is a no-op. It is not: the meter and solar
    steps default to the entry's own settings and can be changed, so picking
    your own contract answers "what would this same contract cost me on a
    bi-hourly meter", or "on the injection tariff instead of compensation".

    Those are the two switches a household can make WITHOUT changing
    supplier, and they were the only comparison the page could not do.
    """
    from custom_components.be_electricity_prices.compare_flow import (
        _compare_contract_schema,
    )

    schema = _compare_contract_schema("eneco", "wallonia", "fixed", "", False)
    options = schema.schema[  # the SelectSelector's option list
        next(k for k in schema.schema if str(k) == "contract")
    ].config["options"]
    ids = [o["value"] for o in options]
    assert "power_fix" in ids, ids
    # And an explicit exclusion still works for callers that want one.
    excluded = _compare_contract_schema(
        "eneco", "wallonia", "fixed", "power_fix", False
    )
    excluded_ids = [
        o["value"]
        for o in excluded.schema[
            next(k for k in excluded.schema if str(k) == "contract")
        ].config["options"]
    ]
    assert "power_fix" not in excluded_ids


def test_the_comparison_chart_keeps_both_rows_for_one_supplier() -> None:
    """The chart took a dict keyed by label, so two sides carrying the same
    label collapsed into ONE row - and into the wrong one, because the second
    value overwrote the first while the first label survived. Comparing two
    contracts from a single supplier did exactly that.
    """
    from custom_components.be_electricity_prices.compare_quote import (
        _populate_charts,
    )

    base = {
        "current_annual": "1200",
        "compare_annual": "1050",
        "current_ytd": "800",
        "compare_ytd": "700",
        "annual_chart": "",
        "ytd_chart": "",
    }
    same = dict(base)
    _populate_charts(same, current_label="Eneco", compare_label="Eneco")
    rows = same["annual_chart"].splitlines()
    assert len(rows) == 2, same["annual_chart"]
    # Each row carries its OWN value, in order.
    assert "1200" in rows[0]
    assert "1050" in rows[1]

    different = dict(base)
    _populate_charts(different, current_label="Eneco", compare_label="Bolt")
    assert len(different["annual_chart"].splitlines()) == 2


def test_chart_labels_fall_back_to_what_actually_differs() -> None:
    """Supplier name, then contract name, then which side is which. The last
    case is the same contract quoted against itself under a different meter
    or regime, where nothing about the product distinguishes the two."""
    from custom_components.be_electricity_prices.compare_flow import _chart_labels

    own = {"supplier": "eneco", "contract": "power_fix"}
    assert _chart_labels(own, {"supplier": "bolt", "contract": "bolt_fix"}) == (
        "Eneco",
        "Bolt",
    )
    same_supplier = _chart_labels(own, {"supplier": "eneco", "contract": "power_flex"})
    assert same_supplier[0] != same_supplier[1]
    assert "Vast" in same_supplier[0] and "Flex" in same_supplier[1]
    assert _chart_labels(own, dict(own)) == ("Your entry", "Quoted")


def test_borrowed_spot_cache_puts_every_attribute_back() -> None:
    """The compare page reads the coordinator's spot caches but must leave
    nothing behind: the next tick persists them, so a key typed into this
    dialog could otherwise seed a cache the entry can never refresh."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_flow import (
        _borrowed_spot_cache,
    )

    hour = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    later = datetime(2026, 3, 2, 10, 0, tzinfo=UTC)
    coord = SimpleNamespace(
        _historical_spots={hour: 0.10},
        _historical_spot_quarters={hour: [0.1, 0.2, 0.3, 0.4]},
        _complete_spot_days={date(2026, 3, 1)},
    )

    # Merging: the fetch sees what is already cached, and adds to it.
    with _borrowed_spot_cache(coord, isolate=False):
        assert coord._historical_spots == {hour: 0.10}
        coord._historical_spots[later] = 0.20
        coord._complete_spot_days.add(date(2026, 3, 2))
    assert coord._historical_spots == {hour: 0.10}
    assert coord._complete_spot_days == {date(2026, 3, 1)}

    # Isolating: the fetch starts empty, and the entry's cache still returns.
    with _borrowed_spot_cache(coord, isolate=True):
        assert coord._historical_spots == {}
        assert coord._historical_spot_quarters == {}
        assert coord._complete_spot_days == set()
        coord._historical_spots[later] = 0.20
    assert coord._historical_spots == {hour: 0.10}
    assert coord._historical_spot_quarters == {hour: [0.1, 0.2, 0.3, 0.4]}
    assert coord._complete_spot_days == {date(2026, 3, 1)}


def test_borrowed_spot_cache_restores_in_place() -> None:
    """Restored into the same dicts rather than rebound.
    ``_ensure_historical_spots`` merges each fetched chunk into the attribute
    and re-resolves it after every await, so a caller holding the old object
    must still see the restored contents."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_flow import (
        _borrowed_spot_cache,
    )

    hour = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    coord = SimpleNamespace(
        _historical_spots={hour: 0.10},
        _historical_spot_quarters={},
        _complete_spot_days=set(),
    )
    held = coord._historical_spots
    with _borrowed_spot_cache(coord, isolate=True):
        coord._historical_spots[hour] = 0.99
    assert held is coord._historical_spots
    assert held == {hour: 0.10}


def test_isolating_the_cache_clears_the_completeness_set() -> None:
    """A day in ``_complete_spot_days`` is treated as fully present without
    consulting the hour dict, so it has to be emptied with them. Leaving it
    behind makes an isolated fetch skip every day the coordinator has already
    walked and return without fetching anything."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_flow import (
        _borrowed_spot_cache,
    )

    walked = {date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)}
    coord = SimpleNamespace(
        _historical_spots={},
        _historical_spot_quarters={},
        _complete_spot_days=set(walked),
    )
    with _borrowed_spot_cache(coord, isolate=True):
        # What _ensure_historical_spots reads to decide whether to fetch.
        assert coord._complete_spot_days == set()
    assert coord._complete_spot_days == walked


def test_compare_pickers_do_not_cross_the_professional_line() -> None:
    """A professional card is published excluding VAT and bands the excise by
    annual volume, so _resolve_snapshot grosses it at the entry's own rate --
    21% against a residential 6% -- while its excise is a fifth of the
    residential one. The row is neither the price the household would pay nor
    a contract it could sign, and only the supplier's own "(pro)" label said
    so."""
    from custom_components.be_electricity_prices.compare_flow import (
        _compare_contract_schema,
        _compare_supplier_options,
    )

    def _offered(professional: bool) -> list[str]:
        out: list[str] = []
        for sup in _compare_supplier_options("flanders", "variable", professional):
            schema = _compare_contract_schema(
                sup["value"], "flanders", "variable", "", professional
            )
            selector = list(schema.schema.values())[0]
            out += [o["value"] for o in selector.config["options"]]
        return out

    residential = _offered(False)
    professional = _offered(True)

    assert residential and professional
    # The two sides partition the catalogue: nothing is lost, nothing doubled.
    assert not set(residential) & set(professional)
    assert "engie_pro_dynamic" in professional
    assert "engie_pro_dynamic" not in residential
    assert all(not c.startswith("engie_pro_") for c in residential)


def test_compare_supplier_list_drops_a_segment_only_supplier() -> None:
    """A supplier whose only in-region cards are professional must not reach
    the contract picker either, or it renders an empty list."""
    from custom_components.be_electricity_prices.compare_flow import (
        _compare_contract_schema,
        _compare_supplier_options,
    )

    for professional in (False, True):
        for sup in _compare_supplier_options("flanders", "variable", professional):
            schema = _compare_contract_schema(
                sup["value"], "flanders", "variable", "", professional
            )
            selector = list(schema.schema.values())[0]
            assert selector.config["options"], (sup["value"], professional)


def test_card_caveat_names_the_supplier_whose_card_omits_the_walloon_fee() -> None:
    """A Walloon card that prints no connection-fee row still leaves the
    household paying the fee, so a target carrying that flag bills short and
    ranks cheaper for a reason that is not its offer. The coordinator raises a
    repair issue about it for the user's OWN entry, which says nothing about a
    contract they are being quoted."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import _card_caveats

    missing = SimpleNamespace(
        taxes=SimpleNamespace(region_connection_fee_unavailable=True)
    )
    printed = SimpleNamespace(
        taxes=SimpleNamespace(region_connection_fee_unavailable=False)
    )

    notes = _card_caveats(missing, "EnergyVision")
    assert len(notes) == 1
    assert "EnergyVision" in notes[0]
    assert "connection-fee" in notes[0]

    assert _card_caveats(printed, "Eneco") == []
    # A side that failed to fetch carries no snapshot and must not raise.
    assert _card_caveats(None, "Bolt") == []
    assert _card_caveats(SimpleNamespace(), "Bolt") == []


def test_vintage_note_names_the_older_card_only_when_they_differ() -> None:
    """Suppliers agree on the regulated overlays to four decimals while both
    cards were published under the same rules, and diverge by about four times
    that the moment a levy change lands between them. That gap belongs to the
    calendar rather than the offer, so it is disclosed, not corrected."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import _vintage_note

    april = SimpleNamespace(
        valid_until=date(2026, 4, 30), publication_label="Avril 2026"
    )
    august = SimpleNamespace(valid_until=date(2026, 8, 31), publication_label="08/2026")
    undated = SimpleNamespace(valid_until=None, publication_label="")

    note = _vintage_note(april, "Engie", august, "energie.be")
    assert "Engie" in note and "energie.be" in note
    assert "Avril 2026" in note
    # Whichever side is older gets named, not whichever side is ours.
    assert _vintage_note(august, "energie.be", april, "Engie").startswith("Engie")

    # Same vintage, or a card that publishes no validity date: nothing to say.
    assert _vintage_note(april, "Engie", april, "Eneco") == ""
    assert _vintage_note(april, "Engie", undated, "Bolt") == ""
    assert _vintage_note(undated, "Bolt", april, "Engie") == ""
    assert _vintage_note(None, "Bolt", april, "Engie") == ""


def test_vintage_note_prints_no_stamp_when_the_older_card_has_no_label() -> None:
    """publication_label is free text off the card ('Avril 2026', 'augustus
    2026', '04/2026') and does not sort, so it is only ever printed. Four of
    the twelve Flemish cards leave valid_until unset and some leave the label
    empty too, and neither may render as an empty bracket."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import _vintage_note

    older = SimpleNamespace(valid_until=date(2026, 4, 30), publication_label="")
    newer = SimpleNamespace(valid_until=date(2026, 8, 31), publication_label="08/2026")
    note = _vintage_note(older, "Bolt", newer, "Engie")
    assert note.startswith("Bolt's card is older")
    assert "()" not in note


def test_month_indexed_side_is_labelled_as_last_months_index() -> None:
    """A card whose rate IS the delivery month's index prints one computed
    from the PREVIOUS month's and says so: 8,1% under in May and 15,4% over in
    February on the 2026 cards' energy leg. Only the entry's own side is ever
    re-resolved against the current month, and _cohort_energy_leg returns None
    for any other contract by design, so a candidate is quoted a month stale
    against a baseline that is not."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.compare_quote import _card_caveats
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
    )

    taxes = SimpleNamespace(region_connection_fee_unavailable=False)
    stale = SimpleNamespace(energy=SimpleNamespace(month_indexed=True), taxes=taxes)
    notes = _card_caveats(stale, "OCTA+")
    assert len(notes) == 1
    assert "OCTA+" in notes[0] and "last month's index" in notes[0]

    # A leg the cohort splice refreshed comes back as SpotMonthlyRates, which
    # carries no month_indexed field at all -- that is what keeps the label
    # off the side that did get the current month's mean.
    assert "month_indexed" not in SpotMonthlyRates.__dataclass_fields__
    refreshed = SimpleNamespace(
        energy=SpotMonthlyRates(factor=1.0, base=0.0), taxes=taxes
    )
    assert _card_caveats(refreshed, "Cociter") == []

    # A fixed card is not indexed on anything.
    flat = SimpleNamespace(energy=SimpleNamespace(), taxes=taxes)
    assert _card_caveats(flat, "Ecofix") == []


def test_spp_opt_in_does_not_reach_a_foreign_card() -> None:
    """_spp_weighting_enabled has two ways in: the CARD indexes injection on
    Belpex_SPP, or the expert custom monthly contract opted in by hand. The
    second reads the ENTRY and never looks at the snapshot, so asking it about
    a TARGET answered for the household instead of for the card.

    A custom monthly entry with the opt-in then re-priced every candidate's
    formula against Belpex_SPP, including cards naming a different index. On a
    real Eneco Power Fix shape the credit went from the card's printed 0,0476
    to -0,02296 EUR/kWh -- a sign flip, about 212 EUR a year at 3000 kWh
    exported."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices import const
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )
    from custom_components.be_electricity_prices.spot_stats import (
        _injection_is_spp_indexed,
        _spp_weighting_enabled,
    )

    entry = SimpleNamespace(
        data={
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: True,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
        }
    )
    foreign = SimpleNamespace(
        energy=FixedRates(single=0.16),
        injection=InjectionRates(
            factor=0.84,
            base=-0.028,
            current=0.0476,
            month_indexed=True,
            spp_indexed=False,
        ),
    )

    # The entry-side predicate still says yes about a card that is not ours,
    # which is exactly why the target side must not consult it.
    assert _spp_weighting_enabled(entry, foreign) is True  # type: ignore[arg-type]
    # The card-side predicate, which the target side uses instead, says no.
    assert _injection_is_spp_indexed(foreign) is False  # type: ignore[arg-type]

    # And it still says yes for a card that really is SPP-indexed, so the
    # narrowing does not cost a legitimate target its resolution.
    spp_card = SimpleNamespace(
        energy=FixedRates(single=0.16),
        injection=InjectionRates(
            factor=0.84, base=-0.028, current=0.0476, spp_indexed=True
        ),
    )
    assert _injection_is_spp_indexed(spp_card) is True  # type: ignore[arg-type]


def test_spp_month_mean_would_invert_a_foreign_credit() -> None:
    """Why the gate matters: branch 2 of _compare_injection_credit is not
    itself gated on inj.spp_indexed, deliberately, because the custom
    supplier's answer lives on the entry rather than on any card. So whatever
    spp_spot the caller hands it is applied, and handing one to a card that
    names a different index turns its credit negative."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices import const
    from custom_components.be_electricity_prices.compare_quote import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )

    foreign = SimpleNamespace(
        energy=FixedRates(single=0.16),
        injection=InjectionRates(
            factor=0.84,
            base=-0.028,
            current=0.0476,
            month_indexed=True,
            spp_indexed=False,
        ),
    )
    # The household this actually bites: a custom monthly entry on the
    # injection regime that ticked the SPP box for its OWN formula.
    entry = SimpleNamespace(
        data={
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: True,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
        }
    )

    printed = _compare_injection_credit(foreign, entry, {}, 0.0950, None)
    assert printed == pytest.approx(0.0476)

    leaked = _compare_injection_credit(foreign, entry, {}, 0.0950, 0.0060)
    assert leaked == pytest.approx(0.84 * 0.0060 - 0.028)
    assert printed is not None and leaked is not None
    assert leaked < 0 < printed


def test_kind_group_is_total_over_tariff_kind() -> None:
    """KIND_GROUP must answer for every kind the registry can produce. A kind
    missing from it is not a wrong group, it is a household whose own contract
    belongs to no group and whose ranking page cannot be built at all, so this
    fails at the moment a new kind is added rather than in a user's dialog."""
    from typing import get_args

    from custom_components.be_electricity_prices.const import KIND_GROUP
    from custom_components.be_electricity_prices.providers.base import TariffKind

    assert set(KIND_GROUP) == set(get_args(TariffKind))
    # Every registered contract resolves, which is the same claim from the
    # other end: a kind reachable from the catalogue but absent here.
    from custom_components.be_electricity_prices.providers import all_extractors

    for ext in all_extractors():
        for contract in ext.contracts:
            assert contract.kind in KIND_GROUP, (ext.id, contract.id, contract.kind)


def test_sweep_candidate_counts_per_cell() -> None:
    """The 18 (region, group, segment) cells, pinned as data.

    Not a tautology against the registry: these are the numbers the ranking's
    cost model and its empty-page handling were designed around, so a new
    supplier or a withdrawn product that moves a cell should surface here and
    be re-costed deliberately rather than silently changing what a sweep does.
    """
    from custom_components.be_electricity_prices.flow_schemas import _sweep_candidates

    expected = {
        ("flanders", "static", False): 51,
        ("flanders", "static", True): 20,
        ("flanders", "spot", False): 26,
        ("flanders", "spot", True): 3,
        ("flanders", "slot", False): 2,
        ("flanders", "slot", True): 1,
        ("wallonia", "static", False): 49,
        ("wallonia", "static", True): 20,
        ("wallonia", "spot", False): 10,
        ("wallonia", "spot", True): 3,
        ("wallonia", "slot", False): 4,
        ("wallonia", "slot", True): 1,
        ("brussels", "static", False): 29,
        ("brussels", "static", True): 20,
        ("brussels", "spot", False): 4,
        ("brussels", "spot", True): 3,
        ("brussels", "slot", False): 1,
        ("brussels", "slot", True): 1,
    }
    actual = {
        (region, group, professional): len(
            _sweep_candidates(region, group, professional, "")
        )
        for region in ("flanders", "wallonia", "brussels")
        for group in ("static", "spot", "slot")
        for professional in (False, True)
    }
    assert actual == expected


def test_sweep_candidates_apply_every_condition() -> None:
    """Region per contract, segment, group, no custom supplier, no supplier on
    its way out, and the household's own contract dropped."""
    from custom_components.be_electricity_prices.const import (
        KIND_GROUP,
        SUPPLIER_CUSTOM,
    )
    from custom_components.be_electricity_prices.flow_schemas import _sweep_candidates

    rows = _sweep_candidates("flanders", "static", False, "power_fix")
    ids = {contract.id for _supplier, contract in rows}

    assert "power_fix" not in ids, "own contract must not be a candidate"
    assert all(sup != SUPPLIER_CUSTOM for sup, _c in rows)
    # dats24 is deprecated_until 2026-08-31 and must not be offered.
    assert all(sup != "dats24" for sup, _c in rows)
    for _sup, contract in rows:
        assert "flanders" in contract.regions
        assert contract.professional is False
        assert KIND_GROUP[contract.kind] == "static"


def test_contract_group_is_empty_for_a_contract_that_left_the_catalogue() -> None:
    """_contract_kind returns '' for a stored contract no longer published, so
    the meter step can still render. That empty string has no group, and the
    ranking page must be told so rather than raising out of a lookup or
    inventing a group the household is not on."""
    from custom_components.be_electricity_prices.flow_schemas import _contract_group

    assert _contract_group("eneco", "power_fix") == "static"
    assert _contract_group("engie", "engie_dynamic") == "spot"
    assert _contract_group("engie", "engie_empower_flextime") == "slot"
    assert _contract_group("eneco", "a_product_eneco_withdrew") == ""
    assert _contract_group("no_such_supplier", "whatever") == ""


async def _compare_placeholders(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    *,
    supplier: str,
    contract: str,
    target_snapshot: Any,
    meter: str | None = "mono",
    api_key: str | None = None,
    solar_regime: str | None = None,
) -> dict[str, str]:
    """Drive the 1:1 compare branch to its result step and return every
    placeholder it produced.

    The whole dict, not a chosen field: this is the net under the split of
    _build_compare_placeholders, and a refactor that silently drops or
    reshapes a token is exactly what a per-field assertion misses.
    """
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    fake = replace(
        EXTRACTORS[supplier], fetch=AsyncMock(return_value=target_snapshot), probe=None
    )
    with patch.dict(EXTRACTORS, {supplier: fake}):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare"}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"supplier": supplier}
        )
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"contract": contract}
        )
        if result["step_id"] == "compare_meter":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"meter": meter}
            )
        if result["step_id"] == "compare_solar":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"],
                {
                    "solar_regime": solar_regime
                    or entry.data.get("solar_regime", "none")
                },
            )
        if result["step_id"] == "compare_api_key":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"api_key": api_key or "test-token"}
            )
        assert result["step_id"] == "compare_result", result["step_id"]
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        return dict(placeholders)


# Golden characterization of _build_compare_placeholders, captured on the tree
# BEFORE the household/target split. Every token is asserted, not a chosen few:
# the fifty-odd sibling compare tests each check one or two, which is exactly
# what lets a refactor move money without any of them noticing. Written first
# so it is a net rather than a rationalization; if a value here has to change,
# the change belongs in its own commit with its own reason.
_GOLDEN_BASE = {
    "annual_chart": "  Eneco   ████████████████████ 1273 EUR\n  Cociter ███████████████████░ 1203 EUR",
    "annual_kwh": "3500",
    "card_note": "",
    "compare_annual": "1202.75",
    "compare_contract": "Cociter Tarif Variable",
    "compare_per_kwh": "0.3265",
    "compare_supplier": "Cociter",
    "compare_ytd": "19.56",
    "consumption_source": "default 3500 kWh - wire a kWh sensor for a measured estimate",
    "current_annual": "1272.75",
    "current_contract": "Eneco Zon & Wind Vast",
    "current_per_kwh": "0.3465",
    "current_supplier": "Eneco",
    "current_ytd": "19.56",
    "delta_annual": "-70.00",
    "delta_ytd": "+0.00",
    "error": "",
    "meter_used": "mono",
    "solar_note": "",
    "ytd_chart": "  Eneco   ████████████████████ 20 EUR\n  Cociter ████████████████████ 20 EUR",
    "ytd_injection_kwh": "-",
    "ytd_kwh": "-",
}

_GOLDEN_DYNAMIC_TARGET = {
    "annual_chart": "",
    "annual_kwh": "3500",
    "card_note": "",
    "compare_annual": "-",
    "compare_contract": "Cociter Tarif Dynamique",
    "compare_per_kwh": "-",
    "compare_supplier": "Cociter",
    "compare_ytd": "-",
    "consumption_source": "default 3500 kWh - wire a kWh sensor for a measured estimate",
    "current_annual": "1272.75",
    "current_contract": "Eneco Zon & Wind Vast",
    "current_per_kwh": "0.3465",
    "current_supplier": "Eneco",
    "current_ytd": "-",
    "delta_annual": "-",
    "delta_ytd": "-",
    "error": "compute failed",
    "meter_used": "dynamic",
    "solar_note": "",
    "ytd_chart": "",
    "ytd_injection_kwh": "-",
    "ytd_kwh": "-",
}

_GOLDEN_METER_OVERRIDE = {
    "annual_chart": "  Eneco   ████████████████████ 1273 EUR\n  Cociter ███████████████████░ 1203 EUR",
    "annual_kwh": "3500",
    "card_note": "",
    "compare_annual": "1202.75",
    "compare_contract": "Cociter Tarif Variable",
    "compare_per_kwh": "0.3265",
    "compare_supplier": "Cociter",
    "compare_ytd": "19.56",
    "consumption_source": "default 3500 kWh - wire a kWh sensor for a measured estimate",
    "current_annual": "1272.75",
    "current_contract": "Eneco Zon & Wind Vast",
    "current_per_kwh": "0.3465",
    "current_supplier": "Eneco",
    "current_ytd": "19.56",
    "delta_annual": "-70.00",
    "delta_ytd": "+0.00",
    "error": "",
    "meter_used": "bi",
    "solar_note": "",
    "ytd_chart": "  Eneco   ████████████████████ 20 EUR\n  Cociter ████████████████████ 20 EUR",
    "ytd_injection_kwh": "-",
    "ytd_kwh": "-",
}

_GOLDEN_ENTRY_RELOADING = {
    "annual_chart": "",
    "annual_kwh": "3500",
    "card_note": "",
    "compare_annual": "-",
    "compare_contract": "cociter_variable",
    "compare_per_kwh": "-",
    "compare_supplier": "cociter",
    "compare_ytd": "-",
    "consumption_source": "default (entry reloading)",
    "current_annual": "-",
    "current_contract": "power_fix",
    "current_per_kwh": "-",
    "current_supplier": "eneco",
    "current_ytd": "-",
    "delta_annual": "-",
    "delta_ytd": "-",
    "error": "current entry is reloading; try again in a moment",
    "meter_used": "mono",
    "solar_note": "",
    "ytd_chart": "",
    "ytd_injection_kwh": "-",
    "ytd_kwh": "-",
}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_golden_placeholders_static_target(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The ordinary quote: static contract against static contract."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    assert (
        await _compare_placeholders(
            hass,
            entry,
            supplier="cociter",
            contract="cociter_variable",
            target_snapshot=_stub_snapshot("cociter", "cociter_variable", 0.16),
        )
        == _GOLDEN_BASE
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_golden_placeholders_meter_override(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The target quoted on a meter the household is not on. Same money here,
    because the stub card prints one rate and a bi meter splits nothing, but a
    distinct path: the override reaches meter_used and must not leak onto the
    household side."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    assert (
        await _compare_placeholders(
            hass,
            entry,
            supplier="cociter",
            contract="cociter_variable",
            target_snapshot=_stub_snapshot("cociter", "cociter_variable", 0.16),
            meter="bi",
        )
        == _GOLDEN_METER_OVERRIDE
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_golden_placeholders_dynamic_target_degrades(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A spot-priced target with no spot data reachable. The page keeps the
    household's own figures and says so in `error` rather than printing a
    number it cannot stand behind; the charts go empty rather than one-sided."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from custom_components.be_electricity_prices.providers.base import DynamicRates
    from tests import make_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    target = make_snapshot(
        supplier="cociter",
        contract="cociter_dynamic",
        energy=DynamicRates(factor=1.0, base=0.02),
        source_url="test://stub",
        publication_label="april 2026",
    )
    assert (
        await _compare_placeholders(
            hass,
            entry,
            supplier="cociter",
            contract="cociter_dynamic",
            target_snapshot=target,
        )
        == _GOLDEN_DYNAMIC_TARGET
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_golden_placeholders_entry_reloading(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The short-circuit taken when runtime_data is not a coordinator yet.
    Every token still has to be populated or HA renders the raw literal, and
    this is the path where that is easiest to forget."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = _make_entry()
    entry.add_to_hass(hass)
    assert (
        await _compare_placeholders(
            hass,
            entry,
            supplier="cociter",
            contract="cociter_variable",
            target_snapshot=_stub_snapshot("cociter", "cociter_variable", 0.16),
        )
        == _GOLDEN_ENTRY_RELOADING
    )


def test_golden_dicts_cover_every_template_token() -> None:
    """The golden dicts are only a net if they span the template. A token
    added to strings.json without a default lands here first."""
    import json
    import string
    from pathlib import Path

    desc = json.loads(
        Path("custom_components/be_electricity_prices/strings.json").read_text(
            encoding="utf-8"
        )
    )["options"]["step"]["compare_result"]["description"]
    tokens = {f for _lit, f, _spec, _conv in string.Formatter().parse(desc) if f}
    for golden in (
        _GOLDEN_BASE,
        _GOLDEN_DYNAMIC_TARGET,
        _GOLDEN_METER_OVERRIDE,
        _GOLDEN_ENTRY_RELOADING,
    ):
        assert set(golden) == tokens


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_compare_quote_always_asks_for_a_fresh_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The one-off quote goes through the shared policy but does not adopt
    from it.

    Three compare targets publish no probe (Engie, Luminus, energie.be), so a
    cached row would quote them off a card up to a day old where this page
    always downloaded. What going through the policy buys instead is the
    per-key lock and the write into the shared cache, so a coordinator can
    adopt what the dialog fetched."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.snapshot_store import (
        _shared_snapshots,
    )

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    target = _stub_snapshot("cociter", "cociter_variable", 0.16)
    fetch = AsyncMock(return_value=target)
    fake = replace(EXTRACTORS["cociter"], fetch=fetch, probe=None)

    async def _open() -> dict[str, str]:
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
        if result["step_id"] == "compare_meter":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"meter": "mono"}
            )
        assert result["step_id"] == "compare_result"
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        return dict(placeholders)

    with patch.dict(EXTRACTORS, {"cociter": fake}):
        first = await _open()
        assert fetch.await_count == 1
        second = await _open()

    # Fresh every open, as it was before the page went through the policy.
    assert fetch.await_count == 2
    assert second["compare_annual"] == first["compare_annual"]
    assert second["error"] == ""
    # But what it fetched IS left where a coordinator can adopt it.
    assert ("cociter", "cociter_variable", "wallonia") in _shared_snapshots(hass)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_failed_compare_quote_does_not_poison_the_shared_caches(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A read-only dialog must not write the shared negative cache.

    That row exists so sibling coordinators back off instead of refiring a
    request the supplier just refused, and its third field is the consecutive-
    failure count the "could not reach the supplier" card is thresholded on.
    A dialog writing it cancels a real entry's due download for five minutes
    and raises that card a failure early - on a single-entry install too,
    because the picker offers the household its own supplier."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from custom_components.be_electricity_prices.providers.base import ExtractorError
    from custom_components.be_electricity_prices.snapshot_store import (
        _shared_failed_fetches,
    )

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    fake = replace(
        EXTRACTORS["cociter"],
        fetch=AsyncMock(side_effect=ExtractorError("supplier said no")),
        probe=None,
    )
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
        if result["step_id"] == "compare_meter":
            result = await hass.config_entries.options.async_configure(
                result["flow_id"], {"meter": "mono"}
            )

    placeholders = result["description_placeholders"]
    assert placeholders is not None
    # The page says so...
    assert "supplier said no" in placeholders["error"]
    # ...and leaves nothing behind for the coordinators to trip over.
    assert _shared_failed_fetches(hass) == {}


def test_row_label_dedupes_the_supplier() -> None:
    """Some suppliers put their own name in the product and some do not, so
    joining unconditionally reads as "Eneco Eneco Zon & Wind Vast" for half
    the table.

    No truncation any more: the name used to be elided to a fixed width so
    columns lined up inside a code fence, which cost exactly the tails these
    names disambiguate on - Agilior Online GREEN against Agilior Online."""
    from custom_components.be_electricity_prices.compare_quote import _row_label

    assert _row_label("Eneco", "Eneco Zon & Wind Vast") == "Eneco Zon & Wind Vast"
    assert _row_label("Bolt", "Fix") == "Bolt Fix"
    long = _row_label("Energy Knights", "Energy Knights Essentia Online Green")
    assert long == "Energy Knights Essentia Online Green"
    assert "…" not in long


def _ranked(table: str) -> list[tuple[str, float]]:
    """(label, annual) for each numbered row of a rendered ranking."""
    import re as _re

    out: list[tuple[str, float]] = []
    for line in table.splitlines():
        m = _re.match(r"\d+\. ([\d  ,]+) EUR - (.+?)(?: ·|$| `)", line)
        if m:
            out.append(
                (m.group(2), float(m.group(1).replace(" ", "").replace(",", ".")))
            )
    return out


def test_ranking_table_keeps_every_row_visible_and_wraps() -> None:
    """A dropped row reads as "not competitive", which is the one thing it
    does not mean, so a row that failed to price leaves the numbered list for
    a named block rather than disappearing.

    Emitted as wrapping markdown, not an aligned block: a Home Assistant
    dialog renders a code fence monospace and never wraps, so 68 columns
    meant scrolling sideways to read a row."""
    from custom_components.be_electricity_prices.compare_quote import (
        RankedRow,
        _ranking_table,
    )

    table = _ranking_table(
        [
            RankedRow("Eneco Zon & Wind Vast", 1141.50, is_own=True),
            RankedRow("Ecofix Flexy", 900.01),
            RankedRow("Ecofix Motion", None, None, "card unreadable"),
        ],
        deferred=6,
    )
    rows = _ranked(table)
    assert [r[0] for r in rows] == ["Ecofix Flexy", "Eneco Zon & Wind Vast"]
    assert "`YOUR CONTRACT`" in table
    # No emphasis markers on a row: they were rendering as literal stars
    # around the amounts.
    assert "*" not in table
    assert any("card unreadable" in ln for ln in table.splitlines())
    assert "6 more not priced yet" in table
    # No code fence anywhere: that is what forced the horizontal scroll.
    assert "```" not in table


def test_ranking_row_carries_ytd_only_when_it_has_one() -> None:
    """A row that cannot honestly carry a year-to-date figure says nothing
    rather than printing a proxied number beside a real one."""
    from custom_components.be_electricity_prices.compare_quote import (
        RankedRow,
        _ranking_table,
    )

    table = _ranking_table([RankedRow("A", 900.0, 12.0), RankedRow("B", 950.0)])
    lines = [ln for ln in table.splitlines() if ln.startswith(("1.", "2."))]
    assert "YTD" in lines[0]
    assert "YTD" not in lines[1]
    assert "YTD" not in _ranking_table([RankedRow("A", 900.0)])


def test_ranking_table_is_empty_for_no_rows() -> None:
    """An empty cell renders nothing here and is explained by the step above,
    which knows the region and the group; this helper does not."""
    from custom_components.be_electricity_prices.compare_quote import _ranking_table

    assert _ranking_table([]) == ""


def test_every_supplier_declares_what_a_card_costs_to_sweep() -> None:
    """The ranking orders by sweep_cost_s so a wall-clock budget buys many
    cheap rows before a few expensive ones. A provider added without one
    inherits a middling default and is scheduled somewhere sane rather than
    first, but it should be measured rather than guessed, so this fails when a
    new supplier lands without a figure.

    The custom supplier keeps the default deliberately: it has no card to
    fetch and is never a sweep candidate."""
    from custom_components.be_electricity_prices.const import SUPPLIER_CUSTOM
    from custom_components.be_electricity_prices.providers import all_extractors
    from custom_components.be_electricity_prices.providers.base import (
        SupplierExtractor,
    )

    default = SupplierExtractor.__dataclass_fields__["sweep_cost_s"].default
    undeclared = [
        e.id
        for e in all_extractors()
        if e.id != SUPPLIER_CUSTOM and e.sweep_cost_s == default
    ]
    assert not undeclared, f"no measured sweep cost: {undeclared}"
    assert all(e.sweep_cost_s > 0 for e in all_extractors())


def test_sweep_cost_ordering_front_loads_the_cheap_cards() -> None:
    """The property the budget relies on: ordering by declared cost fills far
    more of the table early than the registry's own order does."""
    from custom_components.be_electricity_prices.flow_schemas import _sweep_candidates
    from custom_components.be_electricity_prices.providers import get as get_extractor

    rows = _sweep_candidates("flanders", "static", False, "")
    costs = [get_extractor(supplier).sweep_cost_s for supplier, _c in rows]

    def priced_within(budget: float, order: list[float]) -> int:
        spent = 0.0
        for n, cost in enumerate(order):
            if spent + cost > budget and n:
                return n
            spent += cost
        return len(order)

    cheapest_first = priced_within(60.0, sorted(costs))
    registry_order = priced_within(60.0, costs)
    assert cheapest_first > registry_order
    # And the whole cell is reachable, so the budget is a pace, not a cap.
    assert priced_within(10**6, sorted(costs)) == len(rows)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_ranks_every_alternative_in_the_cell(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The ranking page end to end: menu -> sweep -> table."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    with patch.dict(EXTRACTORS, patched):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert "compare_all" in result["menu_options"]
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        # The sweep runs as a chain of progress steps, one task per candidate.
        for _ in range(400):
            if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )

    assert result["step_id"] == "compare_all_result", result
    ph = result["description_placeholders"]
    assert ph is not None
    ranking = ph["ranking"]
    assert ranking, "the table must not be empty"
    # Numbered, cheapest first, and the household's own contract is not a row.
    assert ranking.lstrip().startswith("1.")
    assert "power_fix" not in ranking
    # Every priced row is a real candidate of the household's own group.
    assert ph["group"] == "static"
    assert ph["region"] == "wallonia"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_never_starts_a_second_task_for_one_candidate(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The duplicate-task trap. The flow manager re-enters a progress step on
    every frontend poll, so a step that creates a task without first checking
    for a live one fires the same fetch repeatedly."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    started = 0

    async def _slow_fetch(*_a: object, **_k: object) -> Any:
        nonlocal started
        started += 1
        return _stub_snapshot("engie", "x", 0.16)

    patched = {
        sid: replace(ext, fetch=_slow_fetch, probe=None)
        for sid, ext in EXTRACTORS.items()
    }
    with patch.dict(EXTRACTORS, patched):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        assert result["type"] == data_entry_flow.FlowResultType.SHOW_PROGRESS
        # Poll the SAME step repeatedly without letting the task finish.
        for _ in range(5):
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
        assert started <= 1, f"{started} fetches started for one candidate"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_aborts_when_the_region_holds_no_alternative(
    hass: HomeAssistant,
) -> None:
    """A Brussels time-of-use household has exactly one slot contract in the
    region and it is theirs. Saying so is more use than an empty table, and it
    is a different message from the one-to-one page's."""
    from tests import make_entry

    entry = make_entry(
        supplier="engie",
        contract="engie_empower_flextime",
        region="brussels",
        dso="sibelga",
        meter="dynamic",
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("engie", "engie_empower_flextime", 0.18)
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare_all"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "compare_all_no_alternatives"


def test_every_sweep_string_exists_in_all_five_files() -> None:
    """A step description naming a token Home Assistant cannot resolve renders
    the literal, and a missing translation renders the key. Both are silent."""
    import json
    import string
    from pathlib import Path

    base = Path("custom_components/be_electricity_prices")
    files = ["strings.json"] + [
        f"translations/{c}.json" for c in ("en", "fr", "nl", "de")
    ]
    wanted_steps = {"compare_all_progress", "compare_all_result"}
    wanted_aborts = {
        "compare_all_no_alternatives",
        "compare_all_unknown_contract",
        "compare_all_entry_reloading",
    }
    for name in files:
        options = json.loads((base / name).read_text(encoding="utf-8"))["options"]
        assert "compare_all" in options["step"]["init"]["menu_options"], name
        assert wanted_steps <= set(options["step"]), name
        assert wanted_aborts <= set(options["abort"]), name
        assert "sweeping" in options["progress"], name
        tokens = {
            f
            for _l, f, _s, _c in string.Formatter().parse(
                options["step"]["compare_all_result"]["description"]
            )
            if f
        }
        assert tokens == {"region", "group", "ranking"}, (name, tokens)
        prog = {
            f
            for _l, f, _s, _c in string.Formatter().parse(
                options["progress"]["sweeping"]
            )
            if f
        }
        assert prog == {"priced", "total", "ranking"}, (name, prog)


def test_archived_months_present_ignores_a_proxied_month() -> None:
    """`fetch_for_month is not None` is a property of the SUPPLIER. Seventeen
    candidate contracts pass it and then have no month-addressable card - the
    whole Bolt variable folder returns None before any I/O - and the
    year-to-date walk substitutes the current card for every past month. That
    prints 8,5-23,3% high next to a real figure, in a sortable column, with
    nothing to tell the two apart.

    A cached None IS that case, so it must not count as coverage."""
    from datetime import date as _date

    from custom_components.be_electricity_prices.snapshot_store import (
        _monthly_snapshots,
        archived_months_present,
    )

    hass_stub = SimpleNamespace(data={})
    months = [_date(2026, 1, 1), _date(2026, 2, 1), _date(2026, 3, 1)]
    cache = _monthly_snapshots(hass_stub)  # type: ignore[arg-type]
    snap = _stub_snapshot("bolt", "bolt_fix", 0.2)
    cache[("bolt", "bolt_fix", "flanders", "2026-01")] = snap
    cache[("bolt", "bolt_fix", "flanders", "2026-02")] = snap
    # March had no month-addressable card: the walk cached the proxy marker.
    cache[("bolt", "bolt_fix", "flanders", "2026-03")] = None

    present = archived_months_present(
        hass_stub,  # type: ignore[arg-type]
        "bolt",
        "bolt_fix",
        "flanders",
        months,
    )
    assert present == {(2026, 1), (2026, 2)}
    # A contract the walk never touched has no coverage at all, rather than
    # inheriting its supplier's.
    assert (
        archived_months_present(
            hass_stub,  # type: ignore[arg-type]
            "bolt",
            "bolt_variable",
            "flanders",
            months,
        )
        == set()
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_and_one_to_one_agree_on_the_same_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The two pricing paths must not disagree.

    The sweep reuses the one-to-one page's household context but does its own
    target-side work, which is exactly where a second path drifts. It dropped
    three adjustments and disagreed by 116,81 EUR/yr on a compensation
    household (no export weighting), 26,87 on a dynamic target (no forced
    smart meter) and 12,19 on a Tarif Impact target (no forced CWaPE mode)."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    for extra in (
        {},
        {"solar_regime": "compensation", "solar_kva": 5.0},
    ):
        from tests import make_entry

        entry = make_entry(**extra)  # type: ignore[arg-type]
        entry.add_to_hass(hass)
        entry.runtime_data = _real_coordinator(
            hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
        )
        target = _stub_snapshot("cociter", "cociter_variable", 0.16)
        fake = replace(
            EXTRACTORS["cociter"], fetch=AsyncMock(return_value=target), probe=None
        )
        with patch.dict(EXTRACTORS, {"cociter": fake}):
            one = await _compare_placeholders(
                hass,
                entry,
                supplier="cociter",
                contract="cociter_variable",
                target_snapshot=target,
                meter=entry.data.get("meter", "mono"),
            )
            patched = {
                sid: replace(
                    ext,
                    fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
                    probe=None,
                )
                for sid, ext in EXTRACTORS.items()
            }
            patched["cociter"] = fake
            with patch.dict(EXTRACTORS, patched):
                result = await hass.config_entries.options.async_init(entry.entry_id)
                result = await hass.config_entries.options.async_configure(
                    result["flow_id"], {"next_step_id": "compare_all"}
                )
                for _ in range(400):
                    if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                        break
                    await hass.async_block_till_done()
                    result = await hass.config_entries.options.async_configure(
                        result["flow_id"]
                    )
                sweep_ph = result["description_placeholders"]
                assert sweep_ph is not None
                ranking = sweep_ph["ranking"]

        swept = next(
            v for label, v in _ranked(ranking) if "Cociter Tarif Variable" in label
        )
        assert abs(swept - float(one["compare_annual"])) < 0.01, (
            f"regime={extra.get('solar_regime', 'none')}: "
            f"sweep {swept} vs one-to-one {one['compare_annual']}\n{ranking}"
        )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_ytd_pass_walks_before_it_judges_coverage(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The ordering that made the pass a no-op.

    archived_months_present reads a cache written only by the walk it gates,
    so asking first answers "nothing is covered" for every candidate: the
    engine is never called, no column appears, and the checkbox then hides
    itself as though the work had been done. The walk has to come first.

    Asserted as an order of operations rather than as fetches, because in a
    test the year-to-date engine has no recorder history to price and so never
    reaches a month fetch - which is exactly how the vacuous version passed
    for its whole life."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices import compare_flow
    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    trace: list[str] = []
    real_cov = compare_flow_module_coverage()

    def _cov(hass_, supplier, contract, region, months):
        trace.append(f"coverage:{supplier}")
        return real_cov(hass_, supplier, contract, region, months)

    async def _walk(hass_, session, extractor, contract, region, ym, current, ent=None):
        trace.append(f"walk:{extractor.id}")
        return current

    async def _engine(hass_, session, extractor, snap, ent, **kw):
        # A real walk fills the month cache on its way through; the stub must
        # too, or the household's own coverage comes back empty and every
        # candidate is correctly skipped for want of a baseline to match.
        from custom_components.be_electricity_prices.snapshot_store import (
            _monthly_snapshots,
        )

        trace.append(f"engine:{extractor.id}")
        contract = kw.get("contract_override") or ent.data["contract"]
        for month in range(1, 5):
            _monthly_snapshots(hass_)[
                (extractor.id, contract, "wallonia", f"2026-{month:02d}")
            ] = snap
        return 42.0

    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    with (
        patch.dict(EXTRACTORS, patched),
        patch(
            "custom_components.be_electricity_prices.snapshot_store"
            ".archived_months_present",
            _cov,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_store"
            "._snapshot_for_month",
            _walk,
        ),
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _engine,
        ),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        for _ in range(400):
            if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
        assert result["step_id"] == "compare_all_result"
        schema = result["data_schema"]
        assert schema is not None
        assert compare_flow._YTD_FIELD in {str(k) for k in schema.schema}
        await hass.config_entries.options.async_configure(
            result["flow_id"], {compare_flow._YTD_FIELD: True}
        )

    assert trace, "the year-to-date pass did nothing at all"
    # The household's own side is walked before its coverage is read: that
    # baseline gates every candidate, so reading it first zeroed the column.
    first_engine = next((i for i, e in enumerate(trace) if e.startswith("engine:")), -1)
    first_cov = next((i for i, e in enumerate(trace) if e.startswith("coverage:")), -1)
    assert first_engine != -1, f"the engine was never called: {trace[:8]}"
    assert first_engine < first_cov, (
        f"coverage was read before anything filled the cache it reads: {trace[:8]}"
    )
    # And candidates are reached, not skipped wholesale by an empty baseline.
    assert any(e.startswith("walk:") for e in trace), trace[:8]


def compare_flow_module_coverage() -> Any:
    """The real archived_months_present, captured before patching."""
    from custom_components.be_electricity_prices.snapshot_store import (
        archived_months_present,
    )

    return archived_months_present


async def test_sweep_scratch_is_region_keyed_and_evicted(hass: HomeAssistant) -> None:
    """The ranking's scratch holds fetched cards for the life of the process.

    Keyed by region as well as contract, because a household that edits its
    region is asking about a different market with different DSOs and a card
    fetched for the old one would be re-priced against the new one without
    being re-fetched. Dropped when the entry unloads, or the cards outlive the
    entry that asked for them."""
    from custom_components.be_electricity_prices.compare_flow import (
        _sweep_rows,
        evict_sweep_rows,
    )

    wallonia = _sweep_rows(hass, "entry-1", "wallonia")
    wallonia[("wallonia", "cociter", "cociter_variable")] = "card-w"
    flanders = _sweep_rows(hass, "entry-1", "flanders")
    # Same dict, but the region is part of the key, so nothing collides.
    assert flanders.get(("flanders", "cociter", "cociter_variable")) is None
    assert flanders[("wallonia", "cociter", "cociter_variable")] == "card-w"

    # Another entry has its own scratch.
    other = _sweep_rows(hass, "entry-2", "wallonia")
    assert other == {}

    evict_sweep_rows(hass, "entry-1")
    assert _sweep_rows(hass, "entry-1", "wallonia") == {}
    # And evicting an entry that never swept is not an error.
    evict_sweep_rows(hass, "entry-never-swept")


async def test_text_memo_collapses_repeat_listing_fetches() -> None:
    """Nine providers resolve a per-supplier listing inside fetch() and pick
    one product out of it, so pricing a whole supplier re-downloads the same
    page once per contract. A Flanders static sweep would pull Mega's listing
    nine times, Engie's eight and Luminus's eight.

    Opt-in and scoped: outside the block every caller still gets a live read,
    because the coordinator and the one-off quote both want one."""
    from custom_components.be_electricity_prices.providers import _pdf

    calls: list[str] = []

    class _Resp:
        status = 200

        def __init__(self, url: str) -> None:
            self._url = url

        async def text(self) -> str:
            return f"body of {self._url}"

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _Session:
        def get(self, url: str, **_kw: object) -> _Resp:
            calls.append(url)
            return _Resp(url)

    session = _Session()

    # Unmemoised: every call is a round trip, which is today's behaviour and
    # must stay that way for the coordinator.
    for _ in range(3):
        await _pdf.fetch_text(session, "https://x/listing")  # type: ignore[arg-type]
    assert len(calls) == 3

    calls.clear()
    store: dict[str, str] = {}
    with _pdf.memoise_text_fetches(store):
        bodies = [
            await _pdf.fetch_text(session, "https://x/listing")  # type: ignore[arg-type]
            for _ in range(9)
        ]
    assert len(calls) == 1, f"listing fetched {len(calls)} times inside the memo"
    assert len(set(bodies)) == 1

    # A second URL is still its own fetch, and the memo does not leak out.
    calls.clear()
    with _pdf.memoise_text_fetches(store):
        await _pdf.fetch_text(session, "https://x/other")  # type: ignore[arg-type]
    assert calls == ["https://x/other"]
    calls.clear()
    await _pdf.fetch_text(session, "https://x/listing")  # type: ignore[arg-type]
    assert calls == ["https://x/listing"], "the memo leaked past its block"


async def test_text_memo_is_shared_across_the_sweep_s_tasks() -> None:
    """A sweep prices one candidate per asyncio.Task. A Task copies the
    context at creation, which copies the reference and not the dict, so every
    candidate must see what the first one fetched - which is the whole reason
    the store is passed in rather than created inside the helper."""
    import asyncio

    from custom_components.be_electricity_prices.providers import _pdf

    calls: list[str] = []

    class _Resp:
        status = 200

        async def text(self) -> str:
            return "listing"

        async def __aenter__(self) -> "_Resp":
            return self

        async def __aexit__(self, *_a: object) -> None:
            return None

    class _Session:
        def get(self, url: str, **_kw: object) -> _Resp:
            calls.append(url)
            return _Resp()

    session = _Session()
    store: dict[str, str] = {}

    async def _one() -> str:
        with _pdf.memoise_text_fetches(store):
            return await _pdf.fetch_text(session, "https://x/listing")  # type: ignore[arg-type]

    results = await asyncio.gather(*(asyncio.create_task(_one()) for _ in range(6)))
    assert set(results) == {"listing"}
    # Concurrent tasks may race the first fetch, but nothing like six.
    assert len(calls) < 6, f"the memo was not shared across tasks: {len(calls)}"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_shows_the_table_while_it_is_still_filling(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A counter alone reads as hung. Cheapest-first puts the expensive cards
    last, so the sweep reaches the high thirties quickly and then spends up to
    45 s on one row; the rows already priced are shown so a pause looks like
    one slow supplier rather than a broken dialog."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.compare_flow import (
        _SweepStepsMixin,
    )
    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    # Every render the sweep produces, not only the ones the outer
    # async_configure hands back: with instant stubs the flow manager runs the
    # whole sweep inside one call, so polling the returned result observes a
    # single frame of something the user sees dozens of.
    seen_mid_sweep: list[str] = []
    real_progress = _SweepStepsMixin._sweep_progress

    def _spy(self: Any) -> Any:
        out = real_progress(self)
        ranking = (out.get("description_placeholders") or {}).get("ranking") or ""
        if ranking:
            seen_mid_sweep.append(ranking)
        return out

    with (
        patch.dict(EXTRACTORS, patched),
        patch.object(_SweepStepsMixin, "_sweep_progress", _spy),
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        for _ in range(400):
            if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )

    assert seen_mid_sweep, "the progress step never carried a table"
    # It grows as the sweep runs, rather than appearing only at the end.
    assert len(seen_mid_sweep[-1].splitlines()) > len(seen_mid_sweep[0].splitlines()), (
        "the table did not fill while the sweep ran"
    )
    # And it is ranked at every step, not appended in arrival order.
    for table in seen_mid_sweep[1:]:
        values = [v for _label, v in _ranked(table)]
        assert values == sorted(values), f"rows out of order mid-sweep:\n{table}"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_sweep_does_not_start_a_card_that_cannot_fit(hass: HomeAssistant) -> None:
    """Asking only whether the budget is already spent let the sweep reach
    110 s of a 120 s budget and then start a 45 s Bolt card, overrunning by
    most of a minute with the counter frozen on one row - which from outside
    is indistinguishable from stuck.

    A candidate that cannot fit is skipped, and cheaper ones behind it are
    still taken."""
    from custom_components.be_electricity_prices.compare_flow import _SweepStepsMixin

    flow = _SweepStepsMixin()
    flow._sweep = {
        "candidates": [
            ("bolt", "bolt_fix"),  # 45.3s
            ("engie", "engie_easy_fixed"),  # 0.3s
        ],
        "index": 0,
        "rows": [RankedRow("already priced", 900.0)],
        "started_at": dt_util.utcnow() - timedelta(seconds=110),
    }
    # 10s left: Bolt cannot fit, Engie can, so the cheap one is still taken.
    assert flow._sweep_next_index() == 1

    # With nothing priced yet the first candidate runs whatever it costs: a
    # household whose whole cell is expensive gets a row, not an empty page.
    flow._sweep["rows"] = []
    assert flow._sweep_next_index() == 0

    # And when nothing left fits, the sweep stops rather than overrunning.
    flow._sweep["rows"] = [RankedRow("already priced", 900.0)]
    flow._sweep["candidates"] = [("bolt", "bolt_fix")]
    flow._sweep["index"] = 0
    assert flow._sweep_next_index() is None


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_ranking_shows_your_own_contract_and_measures_against_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Your own contract is not a candidate - a ranking is a list of
    alternatives - but it belongs in the table, because it is what every gap
    is measured against and a ranking that never shows where you sit cannot
    answer "should I switch".

    It is priced from the card the entry already holds, not re-fetched: the
    signing-rate and cohort splice the household is actually billed on would
    be discarded by fetching it as though it were a stranger's card."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    from dataclasses import replace

    from custom_components.be_electricity_prices.providers import EXTRACTORS

    entry = _make_entry()
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(
        hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
    )
    # Every candidate is cheaper than the household's own 0.18 card.
    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    own_fetch = AsyncMock(return_value=_stub_snapshot("eneco", "power_fix", 0.18))
    patched["eneco"] = replace(EXTRACTORS["eneco"], fetch=own_fetch, probe=None)

    with patch.dict(EXTRACTORS, patched):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        for _ in range(400):
            if result["type"] != data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
        placeholders = result["description_placeholders"]
        assert placeholders is not None
        ranking = placeholders["ranking"]

    assert "`YOUR CONTRACT`" in ranking, ranking[:400]
    assert "*" not in ranking, ranking[:400]
    own_line = next(ln for ln in ranking.splitlines() if "YOUR CONTRACT" in ln)
    assert "power_fix" not in ranking, "the own contract must not also be a candidate"

    rows = _ranked(ranking)
    own_value = next(v for label, v in rows if label in own_line)
    others = [(label, v) for label, v in rows if label not in own_line]
    assert others, "no alternatives were priced"

    # Every other row states its gap against the household's own figure, and a
    # cheaper alternative reads as money saved rather than as a bare number.
    for label, value in others:
        line = next(ln for ln in ranking.splitlines() if label in ln)
        gap = line.split("·")[1]
        expected = value - own_value
        if expected > 0:
            assert gap.strip().startswith("+"), (label, line)
        elif expected < 0:
            assert gap.strip().startswith("-"), (label, line)
        # A contract priced level with the household's own reads 0,00 and
        # carries no sign, which is the honest rendering of no difference.
        assert f"{abs(expected):,.2f}".replace(",", " ").replace(".", ",") in gap, (
            label,
            line,
            expected,
        )
    assert any(v < own_value for _label, v in others), "no cheaper alternative"
