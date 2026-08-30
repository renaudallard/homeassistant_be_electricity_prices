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
