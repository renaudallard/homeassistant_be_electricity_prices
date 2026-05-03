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

Both ConfigFlow and OptionsFlow walk the same chain of steps:

  user      -> supplier (registry) + region
  contract  -> contract (filtered by supplier)
  dso       -> DSO (filtered by region)
  meter     -> mono / bi / dynamic
  api_key   -> ENTSO-E key (only when chosen contract is dynamic)
  capacity  -> Flemish capacity peak source (only when region = flanders)

OptionsFlow pre-fills every field with the current value, so the user can
change anything (including supplier/contract/region) post-install. On
finalize, OptionsFlow writes back to ``entry.data`` and updates the entry
title.

No EUR values are asked. Energy + network + tax rates are fetched live by
the coordinator from each supplier's own publication.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .api import EntsoeAuthError, EntsoeClient, EntsoeError
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
    CONF_CONSUMPTION_KWH,
    CONF_CONTRACT,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INJECTION_KWH,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DSO_MODE_BI_HORAIRE,
    DSO_TARIFF_MODES,
    SOLAR_REGIME_NONE,
    SOLAR_REGIMES,
    VREG_CAPACITY_FLOOR_KW,
    DOMAIN,
    DSO_CHOICES,
    METER_DYNAMIC,
    METER_MONO,
    METER_TYPES,
    REGION_FLANDERS,
    REGION_WALLONIA,
    REGIONS,
)
from .providers import all_extractors, get as get_extractor
from .providers.base import Contract


# ---- shared schema builders ---------------------------------------------------


def _supplier_options(region: str | None = None) -> list[SelectOptionDict]:
    extractors = all_extractors()
    if region is not None:
        extractors = tuple(e for e in extractors if region in e.regions())
    return [SelectOptionDict(value=e.id, label=e.label) for e in extractors]


def _contracts_for(supplier_id: str, region: str | None = None) -> tuple[Contract, ...]:
    contracts = get_extractor(supplier_id).contracts
    if region is None:
        return contracts
    return tuple(c for c in contracts if region in c.regions)


def _region_dso_options(region: str) -> list[SelectOptionDict]:
    return [
        SelectOptionDict(value=slug, label=label)
        for slug, label in DSO_CHOICES.get(region, ())
    ]


def _region_dso_slugs(region: str) -> tuple[str, ...]:
    return tuple(slug for slug, _ in DSO_CHOICES.get(region, ()))


def _contract_kind(supplier_id: str, contract_id: str) -> str:
    """Return the TariffKind for a contract, or '' if it can't be resolved.

    OptionsFlow can re-open a stale entry whose stored ``contract`` is
    no longer in the supplier's catalogue (supplier dropped a product,
    or the catalogue moved). Returning empty instead of raising lets
    the meter step still render with a sensible default.
    """
    for c in _contracts_for(supplier_id):
        if c.id == contract_id:
            return c.kind
    return ""


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {}
    if (current := defaults.get(CONF_SUPPLIER)) is not None:
        fields[vol.Required(CONF_SUPPLIER, default=current)] = SelectSelector(
            SelectSelectorConfig(
                options=_supplier_options(), mode=SelectSelectorMode.DROPDOWN
            )
        )
    else:
        fields[vol.Required(CONF_SUPPLIER)] = SelectSelector(
            SelectSelectorConfig(
                options=_supplier_options(), mode=SelectSelectorMode.DROPDOWN
            )
        )
    if (region := defaults.get(CONF_REGION)) is not None:
        fields[vol.Required(CONF_REGION, default=region)] = SelectSelector(
            SelectSelectorConfig(
                options=list(REGIONS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
        )
    else:
        fields[vol.Required(CONF_REGION)] = SelectSelector(
            SelectSelectorConfig(
                options=list(REGIONS),
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
        )
    return vol.Schema(fields)


def _contract_schema(
    supplier_id: str, region: str, defaults: dict[str, Any]
) -> vol.Schema:
    contracts = _contracts_for(supplier_id, region)
    options = [SelectOptionDict(value=c.id, label=c.label) for c in contracts]
    valid_ids = {c.id for c in contracts}
    current = defaults.get(CONF_CONTRACT)
    selector = SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
    )
    if current in valid_ids:
        return vol.Schema({vol.Required(CONF_CONTRACT, default=current): selector})
    return vol.Schema({vol.Required(CONF_CONTRACT): selector})


def _dso_schema(region: str, defaults: dict[str, Any]) -> vol.Schema:
    options = _region_dso_options(region)
    valid = set(_region_dso_slugs(region))
    current = defaults.get(CONF_DSO)
    selector = SelectSelector(
        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
    )
    if current in valid:
        return vol.Schema({vol.Required(CONF_DSO, default=current): selector})
    return vol.Schema({vol.Required(CONF_DSO): selector})


def _dso_tariff_mode_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Wallonia-only step: which DSO-side billing mode applies?"""
    current = defaults.get(CONF_DSO_TARIFF_MODE) or DSO_MODE_BI_HORAIRE
    return vol.Schema(
        {
            vol.Required(CONF_DSO_TARIFF_MODE, default=current): SelectSelector(
                SelectSelectorConfig(
                    options=list(DSO_TARIFF_MODES),
                    mode=SelectSelectorMode.LIST,
                    translation_key="dso_tariff_mode",
                )
            ),
        }
    )


def _meter_schema(
    supplier_id: str, contract_id: str, defaults: dict[str, Any]
) -> vol.Schema:
    # Dynamic and TOU contracts both require a smart (SMR3) meter to
    # bill by quarter-hour or by hour-of-day; default the meter step
    # accordingly and restrict the choice list. Picking 'bi' on a TOU
    # contract would make compute_breakdown route distribution through
    # the bi-horaire DSO peak/offpeak split while the supplier still
    # billed energy by TOU slot -- two billing modes that don't mix.
    kind = _contract_kind(supplier_id, contract_id)
    if kind in ("dynamic", "tou"):
        options = [METER_DYNAMIC]
        fallback = METER_DYNAMIC
    else:
        options = list(METER_TYPES)
        fallback = METER_MONO
    current = defaults.get(CONF_METER) if defaults.get(CONF_METER) in options else None
    current = current or fallback
    return vol.Schema(
        {
            vol.Required(CONF_METER, default=current): SelectSelector(
                SelectSelectorConfig(
                    options=options,
                    mode=SelectSelectorMode.LIST,
                    translation_key="meter",
                )
            ),
        }
    )


def _api_key_schema(defaults: dict[str, Any]) -> vol.Schema:
    current = defaults.get(CONF_API_KEY, "")
    return vol.Schema({vol.Required(CONF_API_KEY, default=current): TextSelector()})


async def _validate_entsoe_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Test the ENTSO-E key with a small day-ahead query.

    Returns ``None`` on success, ``"invalid_api_key"`` when ENTSO-E
    rejects the token, or ``"cannot_connect"`` for transport / parse
    errors *or* an empty publication-document response (rate-limit
    /no-data path: ENTSO-E returns HTTP 200 with an
    Acknowledgement_MarketDocument that ``parse_day_ahead_xml`` flattens
    to an empty dict). Querying yesterday's window guarantees the
    bidding zone has actually published prices for that span, so an
    empty response really does mean the key/connection is bad rather
    than 'too early in the day'.
    """
    session = async_get_clientsession(hass)
    client = EntsoeClient(api_key, session)
    yesterday_noon = dt_util.utcnow().replace(
        hour=10, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    try:
        prices = await client.fetch_day_ahead(
            yesterday_noon, yesterday_noon + timedelta(hours=2)
        )
    except EntsoeAuthError:
        return "invalid_api_key"
    except EntsoeError:
        return "cannot_connect"
    if not prices:
        # 200 OK but no TimeSeries -> Acknowledgement document, treat
        # as connection failure so the wizard doesn't finalise an
        # entry that will fail on first refresh.
        return "cannot_connect"
    return None


def _capacity_schema(defaults: dict[str, Any]) -> vol.Schema:
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_CAPACITY_MODE,
            default=defaults.get(CONF_CAPACITY_MODE, CAPACITY_MODE_SENSOR),
        ): SelectSelector(
            SelectSelectorConfig(
                options=[CAPACITY_MODE_SENSOR, CAPACITY_MODE_FIXED],
                mode=SelectSelectorMode.LIST,
                translation_key="capacity_mode",
            )
        ),
    }
    if (sensor := defaults.get(CONF_CAPACITY_PEAK_SENSOR)) is not None:
        fields[vol.Optional(CONF_CAPACITY_PEAK_SENSOR, default=sensor)] = (
            EntitySelector(EntitySelectorConfig(domain="sensor"))
        )
    else:
        fields[vol.Optional(CONF_CAPACITY_PEAK_SENSOR)] = EntitySelector(
            EntitySelectorConfig(domain="sensor")
        )
    fields[
        vol.Optional(
            CONF_CAPACITY_FIXED_KW,
            default=defaults.get(CONF_CAPACITY_FIXED_KW, VREG_CAPACITY_FLOOR_KW),
        )
    ] = NumberSelector(
        NumberSelectorConfig(min=0.0, max=50.0, step=0.1, mode=NumberSelectorMode.BOX)
    )
    return vol.Schema(fields)


def _meters_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Cumulative-kWh sensors for the current_year_cost computation.

    Two ways to feed the sensor, both optional:

      * Direct day/night registers off the meter (4 fields). Used as-is
        when populated.
      * Single cumulative totals (2 fields). The coordinator splits
        deltas into day/night buckets via is_offpeak(now) and persists
        them, so the running current_year_cost survives restarts.

    When both are filled, the day/night registers win (more accurate;
    no warm-up period).
    """
    fields = {}
    for conf in (
        CONF_DAY_CONSUMPTION_KWH,
        CONF_NIGHT_CONSUMPTION_KWH,
        CONF_DAY_INJECTION_KWH,
        CONF_NIGHT_INJECTION_KWH,
        CONF_CONSUMPTION_KWH,
        CONF_INJECTION_KWH,
    ):
        default = defaults.get(conf)
        if default is not None:
            fields[vol.Optional(conf, default=default)] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
        else:
            fields[vol.Optional(conf)] = EntitySelector(
                EntitySelectorConfig(domain="sensor")
            )
    return vol.Schema(fields)


def _solar_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_SOLAR_KVA,
                default=defaults.get(CONF_SOLAR_KVA, 0.0),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0.0, max=50.0, step=0.1, mode=NumberSelectorMode.BOX
                )
            ),
            vol.Required(
                CONF_SOLAR_REGIME,
                default=defaults.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE),
            ): SelectSelector(
                SelectSelectorConfig(
                    options=list(SOLAR_REGIMES),
                    mode=SelectSelectorMode.LIST,
                    translation_key="solar_regime",
                )
            ),
        }
    )


def _entry_title(data: dict[str, Any]) -> str:
    extractor = get_extractor(data[CONF_SUPPLIER])
    contract_label = next(
        (c.label for c in extractor.contracts if c.id == data[CONF_CONTRACT]),
        data[CONF_CONTRACT],
    )
    return f"{extractor.label} - {contract_label} ({data[CONF_REGION].capitalize()})"


# ---- ConfigFlow ---------------------------------------------------------------


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
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(self._data)
        )

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        supplier = self._data[CONF_SUPPLIER]
        region = self._data[CONF_REGION]
        if not _contracts_for(supplier, region):
            return self.async_abort(reason="supplier_region_unavailable")
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_dso()
        return self.async_show_form(
            step_id="contract",
            data_schema=_contract_schema(supplier, region, self._data),
        )

    async def async_step_dso(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meter()
        return self.async_show_form(
            step_id="dso",
            data_schema=_dso_schema(self._data[CONF_REGION], self._data),
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_meter()
        return self.async_show_form(
            step_id="meter",
            data_schema=_meter_schema(
                self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT], self._data
            ),
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            err = await _validate_entsoe_key(self.hass, user_input[CONF_API_KEY])
            if err is None:
                self._data.update(user_input)
                return await self._after_api_key()
            errors[CONF_API_KEY] = err
        return self.async_show_form(
            step_id="api_key",
            data_schema=_api_key_schema(self._data),
            errors=errors,
        )

    async def async_step_capacity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solar()
        return self.async_show_form(
            step_id="capacity", data_schema=_capacity_schema(self._data)
        )

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meters()
        return self.async_show_form(
            step_id="solar", data_schema=_solar_schema(self._data)
        )

    async def async_step_meters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._finalize()
        return self.async_show_form(
            step_id="meters", data_schema=_meters_schema(self._data)
        )

    async def async_step_dso_tariff_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_dso_tariff_mode()
        return self.async_show_form(
            step_id="dso_tariff_mode",
            data_schema=_dso_tariff_mode_schema(self._data),
        )

    async def _after_meter(self) -> ConfigFlowResult:
        # Reject duplicate entries: the same (supplier, contract,
        # region, dso) tuple already running its own coordinator would
        # double-poll the supplier.
        unique = (
            f"{self._data[CONF_SUPPLIER]}:{self._data[CONF_CONTRACT]}"
            f":{self._data[CONF_REGION]}:{self._data[CONF_DSO]}"
        )
        await self.async_set_unique_id(unique)
        self._abort_if_unique_id_configured()
        # Tarif Impact is Wallonia-only; outside Wallonia the
        # distribution mode question doesn't apply (Brussels has only
        # Sibelga, Flanders bills via the capacity tariff).
        if self._data[CONF_REGION] == REGION_WALLONIA:
            return await self.async_step_dso_tariff_mode()
        return await self._after_dso_tariff_mode()

    async def _after_dso_tariff_mode(self) -> ConfigFlowResult:
        if (
            _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT])
            == "dynamic"
        ):
            return await self.async_step_api_key()
        if self._data[CONF_REGION] == REGION_FLANDERS:
            return await self.async_step_capacity()
        return await self.async_step_solar()

    async def _after_api_key(self) -> ConfigFlowResult:
        if self._data[CONF_REGION] == REGION_FLANDERS:
            return await self.async_step_capacity()
        return await self.async_step_solar()

    def _finalize(self) -> ConfigFlowResult:
        return self.async_create_entry(title=_entry_title(self._data), data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> BePricesOptionsFlow:
        return BePricesOptionsFlow()


# ---- OptionsFlow --------------------------------------------------------------


class BePricesOptionsFlow(OptionsFlow):
    """Walk every config step pre-filled, save back to entry.data."""

    _data: dict[str, Any]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not hasattr(self, "_data"):
            self._data = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_contract()
        return self.async_show_form(
            step_id="init", data_schema=_user_schema(self._data)
        )

    async def async_step_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        supplier = self._data[CONF_SUPPLIER]
        region = self._data[CONF_REGION]
        if not _contracts_for(supplier, region):
            return self.async_abort(reason="supplier_region_unavailable")
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_dso()
        return self.async_show_form(
            step_id="contract",
            data_schema=_contract_schema(supplier, region, self._data),
        )

    async def async_step_dso(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meter()
        return self.async_show_form(
            step_id="dso",
            data_schema=_dso_schema(self._data[CONF_REGION], self._data),
        )

    async def async_step_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_meter()
        return self.async_show_form(
            step_id="meter",
            data_schema=_meter_schema(
                self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT], self._data
            ),
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            err = await _validate_entsoe_key(self.hass, user_input[CONF_API_KEY])
            if err is None:
                self._data.update(user_input)
                return await self._after_api_key()
            errors[CONF_API_KEY] = err
        return self.async_show_form(
            step_id="api_key",
            data_schema=_api_key_schema(self._data),
            errors=errors,
        )

    async def async_step_capacity(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solar()
        return self.async_show_form(
            step_id="capacity", data_schema=_capacity_schema(self._data)
        )

    async def async_step_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meters()
        return self.async_show_form(
            step_id="solar", data_schema=_solar_schema(self._data)
        )

    async def async_step_meters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self._finalize()
        return self.async_show_form(
            step_id="meters", data_schema=_meters_schema(self._data)
        )

    async def async_step_dso_tariff_mode(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_dso_tariff_mode()
        return self.async_show_form(
            step_id="dso_tariff_mode",
            data_schema=_dso_tariff_mode_schema(self._data),
        )

    async def _after_meter(self) -> ConfigFlowResult:
        if self._data[CONF_REGION] == REGION_WALLONIA:
            return await self.async_step_dso_tariff_mode()
        return await self._after_dso_tariff_mode()

    async def _after_dso_tariff_mode(self) -> ConfigFlowResult:
        if (
            _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT])
            == "dynamic"
        ):
            return await self.async_step_api_key()
        if self._data[CONF_REGION] == REGION_FLANDERS:
            return await self.async_step_capacity()
        return await self.async_step_solar()

    async def _after_api_key(self) -> ConfigFlowResult:
        if self._data[CONF_REGION] == REGION_FLANDERS:
            return await self.async_step_capacity()
        return await self.async_step_solar()

    def _finalize(self) -> ConfigFlowResult:
        # Reject edits that collide with another existing entry. Two
        # coordinators on the same (supplier, contract, region, dso) tuple
        # would double-poll the supplier and break shared-snapshot dedup.
        new_unique = (
            f"{self._data[CONF_SUPPLIER]}:{self._data[CONF_CONTRACT]}"
            f":{self._data[CONF_REGION]}:{self._data[CONF_DSO]}"
        )
        if new_unique != self.config_entry.unique_id:
            for other in self.hass.config_entries.async_entries(DOMAIN):
                if (
                    other.entry_id != self.config_entry.entry_id
                    and other.unique_id == new_unique
                ):
                    return self.async_abort(reason="already_configured")
        # Persist back to entry.data so the new values are the baseline,
        # discard any stale options, and update the title to reflect the
        # current supplier / contract / region. Skip the write entirely
        # when nothing changed: HA's update listener would otherwise fire
        # a reload, tearing down all entities and the warmed snapshot for
        # no benefit.
        new_title = _entry_title(self._data)
        unchanged = (
            dict(self.config_entry.data) == self._data
            and not self.config_entry.options
            and self.config_entry.title == new_title
            and self.config_entry.unique_id == new_unique
        )
        if not unchanged:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data=self._data,
                options={},
                title=new_title,
                unique_id=new_unique,
            )
        return self.async_create_entry(title="", data={})
