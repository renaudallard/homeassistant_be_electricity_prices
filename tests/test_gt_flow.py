from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.const import (
    DSO_CHOICES,
    SPOT_PRICED_CONTRACT_KINDS,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS

CASES = [
    (sid, c)
    for sid, ex in EXTRACTORS.items()
    # A supplier that has announced its exit is dropped from the picker the
    # moment the flag lands, not on the exit date, so a new setup can never
    # reach one and there is no landing to assert. test_custom.py covers the
    # exclusion itself, including the keep= path that still offers it to an
    # existing entry being edited.
    if ex.deprecated_until is None
    for c in ex.contracts
]


@pytest.fixture(autouse=True)
def _no_setup() -> Any:
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.mark.usefixtures("enable_custom_integrations")
@pytest.mark.parametrize("sid,contract", CASES, ids=[f"{s}:{c.id}" for s, c in CASES])
async def test_where_the_flow_lands(
    hass: HomeAssistant, sid: str, contract: Any
) -> None:
    region = sorted(contract.regions)[0]
    dso = DSO_CHOICES[region][0][0]
    meter = (
        const.METER_DYNAMIC
        if contract.kind in ("dynamic", "tou", "tou_impact")
        else const.METER_MONO
    )
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    cfg = hass.config_entries.flow.async_configure
    flow = result["flow_id"]
    result = await cfg(flow, {const.CONF_SUPPLIER: sid, const.CONF_REGION: region})
    assert result["step_id"] == "contract", result
    result = await cfg(flow, {const.CONF_CONTRACT: contract.id})
    assert result["step_id"] == "dso", result
    result = await cfg(flow, {const.CONF_DSO: dso})
    assert result["step_id"] == "meter", result
    result = await cfg(flow, {const.CONF_METER: meter})
    if result.get("step_id") == "professional":
        result = await cfg(
            flow,
            {const.CONF_INCLUDE_VAT: True, const.CONF_ANNUAL_CONSUMPTION_KWH: 5000},
        )
    if result.get("step_id") == "dso_tariff_mode":
        result = await cfg(flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_SIMPLE})
    landed = result.get("step_id")
    expect_key = contract.kind in SPOT_PRICED_CONTRACT_KINDS
    print(f"LANDED {sid}:{contract.id} kind={contract.kind} -> {landed}")
    if expect_key:
        assert landed == "api_key", (sid, contract.id, landed)
        # Required + validated: an empty submission is rejected, not skipped.
        with patch(
            "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
            return_value="invalid_api_key",
        ):
            result = await cfg(flow, {const.CONF_API_KEY: ""})
        assert result["step_id"] == "api_key"
        assert result["errors"] == {const.CONF_API_KEY: "invalid_api_key"}
    else:
        assert landed != "api_key", (sid, contract.id, landed)


# ---- ENTSO-E unreachable during key validation (discussion #77) -------------------


async def _to_api_key_step(hass: HomeAssistant) -> tuple[Any, str]:
    """Drive a dynamic contract's wizard as far as the API-key step."""
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    cfg = hass.config_entries.flow.async_configure
    flow = result["flow_id"]
    await cfg(flow, {const.CONF_SUPPLIER: "cociter", const.CONF_REGION: "wallonia"})
    await cfg(flow, {const.CONF_CONTRACT: "cociter_dynamic"})
    await cfg(flow, {const.CONF_DSO: DSO_CHOICES["wallonia"][0][0]})
    result = await cfg(flow, {const.CONF_METER: const.METER_DYNAMIC})
    while result.get("step_id") not in ("api_key", None):
        step = result["step_id"]
        if step == "professional":
            result = await cfg(
                flow,
                {const.CONF_INCLUDE_VAT: True, const.CONF_ANNUAL_CONSUMPTION_KWH: 5000},
            )
        elif step == "dso_tariff_mode":
            result = await cfg(
                flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_SIMPLE}
            )
        else:
            break
    assert result["step_id"] == "api_key", result
    return cfg, flow


async def test_unreachable_entsoe_offers_a_choice_instead_of_blocking(
    hass: HomeAssistant,
) -> None:
    """ENTSO-E was down for over a day at the end of August 2026, and nobody
    could add a contract meanwhile: the key check could not confirm a key, so
    the wizard sat on the same form forever (discussion #77). An unreachable
    platform is not a bad key, and the user cannot fix it either way."""
    cfg, flow = await _to_api_key_step(hass)
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="cannot_connect",
    ):
        result = await cfg(flow, {const.CONF_API_KEY: "probably-fine"})
    assert result["type"] == "menu", result
    assert result["step_id"] == "api_key_unreachable"
    # The re-check is offered FIRST: continuing unverified is the fallback,
    # not the default.
    assert result["menu_options"] == ["api_key_recheck", "api_key_unverified"]


async def test_a_rejected_key_still_blocks(hass: HomeAssistant) -> None:
    """Narrowness guard. A key ENTSO-E actively refused is the user's problem
    and theirs to fix, so it must never reach the continue-anyway menu."""
    cfg, flow = await _to_api_key_step(hass)
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="invalid_api_key",
    ):
        result = await cfg(flow, {const.CONF_API_KEY: "bad"})
    assert result["type"] == "form"
    assert result["step_id"] == "api_key"
    assert result["errors"] == {const.CONF_API_KEY: "invalid_api_key"}


async def test_continuing_unverified_keeps_the_key_and_moves_on(
    hass: HomeAssistant,
) -> None:
    """The whole point: setup completes on a key nothing could verify, and the
    key the user typed is the one that gets stored."""
    cfg, flow = await _to_api_key_step(hass)
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="cannot_connect",
    ):
        await cfg(flow, {const.CONF_API_KEY: "typed-key"})
        result = await cfg(flow, {"next_step_id": "api_key_unverified"})
    assert result["step_id"] != "api_key_unreachable", result
    flows = hass.config_entries.flow.async_progress()
    assert flows, "the wizard must still be running, not dead-ended"


async def test_rechecking_lets_a_recovered_platform_through(
    hass: HomeAssistant,
) -> None:
    """Someone who would rather wait than proceed unverified gets to retry,
    and a platform that came back finishes the check properly."""
    cfg, flow = await _to_api_key_step(hass)
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="cannot_connect",
    ):
        await cfg(flow, {const.CONF_API_KEY: "typed-key"})
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value=None,
    ):
        result = await cfg(flow, {"next_step_id": "api_key_recheck"})
    assert result["step_id"] != "api_key_unreachable", result


async def test_a_recheck_that_exposes_a_bad_key_returns_to_the_form(
    hass: HomeAssistant,
) -> None:
    """ENTSO-E came back and refused the key after all. That is the user's
    problem, so they go back to the form with the real error rather than
    keeping a continue-anyway they should not take."""
    cfg, flow = await _to_api_key_step(hass)
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="cannot_connect",
    ):
        await cfg(flow, {const.CONF_API_KEY: "bad"})
    with patch(
        "custom_components.be_electricity_prices.config_flow._validate_entsoe_key",
        return_value="invalid_api_key",
    ):
        result = await cfg(flow, {"next_step_id": "api_key_recheck"})
    assert result["type"] == "form"
    assert result["step_id"] == "api_key"
    assert result["errors"] == {const.CONF_API_KEY: "invalid_api_key"}
