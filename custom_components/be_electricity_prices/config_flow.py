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

"""Config flow for the Belgian Electricity Prices integration.

Steps:

  user      -> supplier (registry) + region
  contract  -> contract (filtered by supplier)
  dso       -> DSO (filtered by region)
  api_key   -> ENTSO-E key (only when chosen contract is dynamic)
  capacity  -> Flemish capacity peak source (only when region = flanders)

No EUR values are asked. Energy + network + tax rates are fetched live by
the coordinator from each supplier's own publication.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CAPACITY_MODE_FIXED,
    CAPACITY_MODE_SENSOR,
    CONF_API_KEY,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONTRACT,
    CONF_DSO,
    CONF_REGION,
    CONF_SUPPLIER,
    DEFAULT_CAPACITY_FIXED_KW,
    DOMAIN,
    REGION_FLANDERS,
    REGIONS,
)
from .providers import all_extractors, get as get_extractor
from .providers.base import Contract


def _supplier_options() -> list[SelectOptionDict]:
    return [SelectOptionDict(value=e.id, label=e.label) for e in all_extractors()]


def _region_dsos(region: str) -> tuple[str, ...]:
    if region == REGION_FLANDERS:
        return ("fluvius",)
    if region == "wallonia":
        return ("ores", "resa", "aieg", "aiesh", "rew")
    return ("sibelga",)


def _contracts_for(supplier_id: str) -> tuple[Contract, ...]:
    return get_extractor(supplier_id).contracts


class BePricesConfigFlow(ConfigFlow, domain=DOMAIN):
    """Multi-step config flow."""

    VERSION = 1

    _data: dict[str, Any]

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not hasattr(self, "_data"):
            self._data = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_contract()

        schema = vol.Schema(
            {
                vol.Required(CONF_SUPPLIER): SelectSelector(
                    SelectSelectorConfig(
                        options=_supplier_options(),
                        mode=SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Required(CONF_REGION): SelectSelector(
                    SelectSelectorConfig(
                        options=list(REGIONS),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="region",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_dso()

        contracts = _contracts_for(self._data[CONF_SUPPLIER])
        options = [SelectOptionDict(value=c.id, label=c.label) for c in contracts]
        schema = vol.Schema(
            {
                vol.Required(CONF_CONTRACT): SelectSelector(
                    SelectSelectorConfig(
                        options=options,
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }
        )
        return self.async_show_form(step_id="contract", data_schema=schema)

    async def async_step_dso(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if self._is_dynamic_contract():
                return await self.async_step_api_key()
            if self._data[CONF_REGION] == REGION_FLANDERS:
                return await self.async_step_capacity()
            return self._finalize()

        dsos = _region_dsos(self._data[CONF_REGION])
        schema = vol.Schema(
            {
                vol.Required(CONF_DSO): SelectSelector(
                    SelectSelectorConfig(
                        options=list(dsos),
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key="dso",
                    )
                ),
            }
        )
        return self.async_show_form(step_id="dso", data_schema=schema)

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            if self._data[CONF_REGION] == REGION_FLANDERS:
                return await self.async_step_capacity()
            return self._finalize()
        schema = vol.Schema({vol.Required(CONF_API_KEY): TextSelector()})
        return self.async_show_form(step_id="api_key", data_schema=schema)

    async def async_step_capacity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._finalize()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_CAPACITY_MODE, default=CAPACITY_MODE_SENSOR
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=[CAPACITY_MODE_SENSOR, CAPACITY_MODE_FIXED],
                        mode=SelectSelectorMode.LIST,
                        translation_key="capacity_mode",
                    )
                ),
                vol.Optional(CONF_CAPACITY_PEAK_SENSOR): EntitySelector(
                    EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    CONF_CAPACITY_FIXED_KW, default=DEFAULT_CAPACITY_FIXED_KW
                ): NumberSelector(
                    NumberSelectorConfig(
                        min=0.0, max=50.0, step=0.1, mode=NumberSelectorMode.BOX
                    )
                ),
            }
        )
        return self.async_show_form(step_id="capacity", data_schema=schema)

    def _is_dynamic_contract(self) -> bool:
        for c in _contracts_for(self._data[CONF_SUPPLIER]):
            if c.id == self._data[CONF_CONTRACT]:
                return c.kind == "dynamic"
        return False

    def _finalize(self) -> ConfigFlowResult:
        extractor = get_extractor(self._data[CONF_SUPPLIER])
        contract_label = next(
            (c.label for c in extractor.contracts if c.id == self._data[CONF_CONTRACT]),
            self._data[CONF_CONTRACT],
        )
        title = (
            f"{extractor.label} - {contract_label}"
            f" ({self._data[CONF_REGION].capitalize()})"
        )
        return self.async_create_entry(title=title, data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> BePricesOptionsFlow:
        return BePricesOptionsFlow()


class BePricesOptionsFlow(OptionsFlow):
    """Update the API key (no other knobs - prices come from the live source)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        merged = {**self.config_entry.data, **self.config_entry.options}
        fields: dict[Any, Any] = {}
        if _is_dynamic(merged):
            fields[vol.Required(CONF_API_KEY, default=merged.get(CONF_API_KEY, ""))] = (
                TextSelector()
            )
        if not fields:
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))


def _is_dynamic(merged: dict[str, Any]) -> bool:
    try:
        contracts = _contracts_for(merged[CONF_SUPPLIER])
    except Exception:
        return False
    for c in contracts:
        if c.id == merged.get(CONF_CONTRACT):
            return c.kind == "dynamic"
    return False
