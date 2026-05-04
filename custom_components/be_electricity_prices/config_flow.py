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

import re
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


def _compare_compatible(current_kind: str, other_kind: str) -> bool:
    """Whether ``other_kind`` can be quoted side-by-side with ``current_kind``.

    Dynamic contracts price each hour against an ENTSO-E spot, so a
    static-vs-dynamic quote would either need a fresh API call (hidden
    cost on a UI dialog) or fall back to the dynamic's printed
    monthly indicative (lies about what the user would actually pay).
    Refuse those crossings outright. Among static-ish kinds (fixed,
    variable, tou) the per-kWh number is directly comparable.
    """
    return (current_kind == "dynamic") == (other_kind == "dynamic")


def _compare_supplier_options(region: str, current_kind: str) -> list[SelectOptionDict]:
    """Suppliers that have at least one same-kind contract in the user's
    region. Filtering at the supplier level avoids the user picking a
    supplier and then seeing an empty contract dropdown."""
    out: list[SelectOptionDict] = []
    for ext in all_extractors():
        if region not in ext.regions():
            continue
        if not any(
            _compare_compatible(current_kind, c.kind) and region in c.regions
            for c in ext.contracts
        ):
            continue
        out.append(SelectOptionDict(value=ext.id, label=ext.label))
    return out


def _compare_contract_schema(
    supplier_id: str, region: str, current_kind: str, exclude_contract: str
) -> vol.Schema:
    """Contract picker scoped to same-kind contracts in the user's region,
    minus the user's current contract (so they don't quote against
    themselves)."""
    contracts = [
        c
        for c in _contracts_for(supplier_id, region)
        if _compare_compatible(current_kind, c.kind) and c.id != exclude_contract
    ]
    options = [SelectOptionDict(value=c.id, label=c.label) for c in contracts]
    return vol.Schema(
        {
            vol.Required(CONF_CONTRACT): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )
        }
    )


def _user_schema(defaults: dict[str, Any]) -> vol.Schema:
    supplier_default = defaults.get(CONF_SUPPLIER, vol.UNDEFINED)
    region_default = defaults.get(CONF_REGION, vol.UNDEFINED)
    return vol.Schema(
        {
            vol.Required(CONF_SUPPLIER, default=supplier_default): SelectSelector(
                SelectSelectorConfig(
                    options=_supplier_options(), mode=SelectSelectorMode.DROPDOWN
                )
            ),
            vol.Required(CONF_REGION, default=region_default): SelectSelector(
                SelectSelectorConfig(
                    options=list(REGIONS),
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="region",
                )
            ),
        }
    )


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


_DAY_TARIFF_TOKENS = frozenset({"peak", "day", "jour", "dag", "piek"})
_NIGHT_TARIFF_TOKENS = frozenset({"night", "nuit", "nacht", "dal"})
_TARIFF_SEPARATORS = re.compile(r"[_\-\s]+")


def _classify_tariff(name: str) -> str | None:
    """Map a utility_meter tariff name to ``"day"`` / ``"night"``.

    Belgian users mix English (peak/offpeak), French (jour/nuit), and
    Dutch (dag/nacht, piek/dal) when naming their utility_meter
    tariffs. Tokenize on ``_-`` and whitespace and match exactly so
    "offpeak" doesn't accidentally collide with "peak". Names with
    both a day and a night token (e.g. "peak_night_combined") return
    ``None`` so the caller can refuse to pre-fill rather than guess.
    """
    n = name.lower()
    # "offpeak" / "off_peak" / "off-peak" all collapse to a contiguous
    # "offpeak"; treat that as night regardless of token splitting.
    if "offpeak" in _TARIFF_SEPARATORS.sub("", n):
        return "night"
    tokens = set(_TARIFF_SEPARATORS.split(n))
    is_day = bool(tokens & _DAY_TARIFF_TOKENS)
    is_night = bool(tokens & _NIGHT_TARIFF_TOKENS)
    if is_day and not is_night:
        return "day"
    if is_night and not is_day:
        return "night"
    return None


def _utility_meter_day_night_children(
    hass: HomeAssistant, source_entity_id: str
) -> dict[str, str]:
    """Return ``{"day": ..., "night": ...}`` entity ids for a
    utility_meter helper splitting ``source_entity_id`` into a day /
    night pair, or ``{}`` if no unambiguous match is found.

    Walks ``utility_meter`` config entries (the modern UI-configured
    helpers; YAML-configured helpers don't appear here, so users with
    those keep manual selection). Bails on any ambiguity rather than
    guessing -- a wrong day/night pick mis-bills the year cost.
    """
    from homeassistant.helpers import entity_registry as er

    for entry in hass.config_entries.async_entries("utility_meter"):
        opts = {**entry.data, **entry.options}
        if opts.get("source") != source_entity_id:
            continue
        tariffs = opts.get("tariffs") or []
        slot_tariffs: dict[str, str] = {}
        ambiguous = False
        for tariff in tariffs:
            slot = _classify_tariff(tariff)
            if slot is None:
                continue
            if slot in slot_tariffs:
                ambiguous = True
                break
            slot_tariffs[slot] = tariff
        if ambiguous or "day" not in slot_tariffs or "night" not in slot_tariffs:
            continue
        ent_reg = er.async_get(hass)
        registry_entries = er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        out: dict[str, str] = {}
        for slot, tariff in slot_tariffs.items():
            for re_entry in registry_entries:
                if re_entry.unique_id.endswith(f"_{tariff}"):
                    out[slot] = re_entry.entity_id
                    break
        if "day" in out and "night" in out:
            return out
    return {}


async def _apply_energy_manager_defaults(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> None:
    """Pre-fill the cumulative consumption / injection sensors (and,
    when a utility_meter helper is wired up, the day/night registers)
    from the user's Energy dashboard when nothing is already set.

    The Energy dashboard's grid source records the same kind of
    cumulative-kWh totals the coordinator reads via the recorder, so
    treating it as the default saves the user from picking the same
    sensor twice. For the day/night split we follow utility_meter
    helpers rooted at the same source -- only when the tariff names
    map unambiguously to day/night.
    """
    if any(
        defaults.get(k) is not None
        for k in (
            CONF_CONSUMPTION_KWH,
            CONF_INJECTION_KWH,
            CONF_DAY_CONSUMPTION_KWH,
            CONF_NIGHT_CONSUMPTION_KWH,
            CONF_DAY_INJECTION_KWH,
            CONF_NIGHT_INJECTION_KWH,
        )
    ):
        return
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return
    try:
        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - energy may not be ready
        return
    prefs: dict[str, Any] | None = manager.data  # type: ignore[assignment]
    if not prefs:
        return
    sources: list[dict[str, Any]] = prefs.get("energy_sources") or []
    for source in sources:
        if source.get("type") != "grid":
            continue
        flow_from: list[dict[str, Any]] = source.get("flow_from") or []
        flow_to: list[dict[str, Any]] = source.get("flow_to") or []
        consumption_stat: str | None = None
        injection_stat: str | None = None
        if flow_from:
            stat = flow_from[0].get("stat_energy_from")
            # EntitySelector only accepts real entities; recorder-only
            # statistic ids (no leading "sensor.") would render as a
            # broken default.
            if isinstance(stat, str) and stat.startswith("sensor."):
                consumption_stat = stat
        if flow_to:
            stat = flow_to[0].get("stat_energy_to")
            if isinstance(stat, str) and stat.startswith("sensor."):
                injection_stat = stat
        if consumption_stat is not None:
            defaults[CONF_CONSUMPTION_KWH] = consumption_stat
            day_night = _utility_meter_day_night_children(hass, consumption_stat)
            if day_night:
                defaults[CONF_DAY_CONSUMPTION_KWH] = day_night["day"]
                defaults[CONF_NIGHT_CONSUMPTION_KWH] = day_night["night"]
        if injection_stat is not None:
            defaults[CONF_INJECTION_KWH] = injection_stat
            day_night = _utility_meter_day_night_children(hass, injection_stat)
            if day_night:
                defaults[CONF_DAY_INJECTION_KWH] = day_night["day"]
                defaults[CONF_NIGHT_INJECTION_KWH] = day_night["night"]
        return


async def _apply_energy_manager_capacity_default(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> None:
    """Pre-fill the Flemish capacity peak sensor from the Energy
    dashboard when nothing is already set.

    The dashboard tracks cumulative kWh, but the capacity tariff needs
    a kW power sensor. The common bridge is a Riemann ``integration``
    helper that turns a kW input into the kWh output the dashboard
    consumes. Walk back: dashboard kWh sensor -> integration helper
    config entry -> the helper's ``source`` (the kW sensor we want).

    Skipped when:
      - the user already picked a sensor (preserve manual choice),
      - the energy component isn't loaded,
      - the dashboard has no grid source,
      - the consumption sensor isn't a Riemann-integration child
        (no way to derive the kW source automatically).
    """
    if defaults.get(CONF_CAPACITY_PEAK_SENSOR) is not None:
        return
    try:
        from homeassistant.components.energy.data import async_get_manager
    except ImportError:
        return
    try:
        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - energy may not be ready
        return
    prefs: dict[str, Any] | None = manager.data  # type: ignore[assignment]
    if not prefs:
        return
    sources: list[dict[str, Any]] = prefs.get("energy_sources") or []
    consumption_stat: str | None = None
    for source in sources:
        if source.get("type") != "grid":
            continue
        flow_from: list[dict[str, Any]] = source.get("flow_from") or []
        if flow_from:
            stat = flow_from[0].get("stat_energy_from")
            if isinstance(stat, str) and stat.startswith("sensor."):
                consumption_stat = stat
        break
    if consumption_stat is None:
        return
    from homeassistant.helpers import entity_registry as er

    ent_reg = er.async_get(hass)
    re_entry = ent_reg.async_get(consumption_stat)
    if re_entry is None or re_entry.platform != "integration":
        return
    if re_entry.config_entry_id is None:
        return
    ce = hass.config_entries.async_get_entry(re_entry.config_entry_id)
    if ce is None:
        return
    opts = {**ce.data, **ce.options}
    source_sensor = opts.get("source")
    if isinstance(source_sensor, str) and source_sensor.startswith("sensor."):
        defaults[CONF_CAPACITY_PEAK_SENSOR] = source_sensor


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
        defaults = dict(self._data)
        await _apply_energy_manager_capacity_default(self.hass, defaults)
        return self.async_show_form(
            step_id="capacity", data_schema=_capacity_schema(defaults)
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
        defaults = dict(self._data)
        await _apply_energy_manager_defaults(self.hass, defaults)
        return self.async_show_form(
            step_id="meters", data_schema=_meters_schema(defaults)
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
    """Walk every config step pre-filled, save back to entry.data.

    Two top-level paths from the init menu: edit the existing entry
    (the original options flow) or run a one-off comparison quote
    against a different supplier (no save, no extra entry).
    """

    _data: dict[str, Any]
    _compare: dict[str, Any]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["edit", "compare"],
        )

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if not hasattr(self, "_data"):
            self._data = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_contract()
        return self.async_show_form(
            step_id="edit", data_schema=_user_schema(self._data)
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
        defaults = dict(self._data)
        await _apply_energy_manager_capacity_default(self.hass, defaults)
        return self.async_show_form(
            step_id="capacity", data_schema=_capacity_schema(defaults)
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
        defaults = dict(self._data)
        await _apply_energy_manager_defaults(self.hass, defaults)
        return self.async_show_form(
            step_id="meters", data_schema=_meters_schema(defaults)
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

    # ---- compare-another-supplier branch ---------------------------------
    #
    # Walks supplier -> contract -> result. Region, DSO, meter, peak,
    # solar etc. all stay the same as the current entry so the quote is
    # apples-to-apples. The result step shows a side-by-side breakdown
    # and exits via async_abort -- no entry, no options, nothing saved.

    async def async_step_compare(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.data
        current_kind = _contract_kind(current[CONF_SUPPLIER], current[CONF_CONTRACT])
        if not hasattr(self, "_compare"):
            self._compare = {}
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_contract()
        options = _compare_supplier_options(current[CONF_REGION], current_kind)
        if not options:
            return self.async_abort(reason="compare_no_alternative")
        return self.async_show_form(
            step_id="compare",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SUPPLIER): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            mode=SelectSelectorMode.DROPDOWN,
                            translation_key="supplier",
                        )
                    ),
                }
            ),
        )

    async def async_step_compare_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.data
        current_kind = _contract_kind(current[CONF_SUPPLIER], current[CONF_CONTRACT])
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_result()
        # Only show same-kind contracts (filter built into the schema)
        # and exclude the user's current contract iff the picked supplier
        # is the user's current one.
        exclude = (
            current[CONF_CONTRACT]
            if self._compare[CONF_SUPPLIER] == current[CONF_SUPPLIER]
            else ""
        )
        return self.async_show_form(
            step_id="compare_contract",
            data_schema=_compare_contract_schema(
                self._compare[CONF_SUPPLIER],
                current[CONF_REGION],
                current_kind,
                exclude,
            ),
        )

    async def async_step_compare_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_abort(reason="compare_done")
        placeholders = await self._build_compare_placeholders()
        return self.async_show_form(
            step_id="compare_result",
            data_schema=vol.Schema({}),
            description_placeholders=placeholders,
            last_step=True,
        )

    async def _build_compare_placeholders(self) -> dict[str, str]:
        """Fetch the picked supplier's snapshot and compute a side-by-side
        annual estimate against the user's current entry.

        Annual = per_kwh_now * DEFAULT_ANNUAL_KWH + yearly fees, where the
        yearly fees are yearly_fixed_fee + 12 * energy_fund + 12 *
        capacity (Flanders) + 12 * prosumer (Wallonia compensation +
        solar). Errors collapse to ``-`` so the page always renders.
        """
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        from .coordinator import BePricesCoordinator
        from .pricing import compute_breakdown

        DEFAULT_ANNUAL_KWH = 3500.0  # typical Belgian residential consumption

        current = self.config_entry.data
        coord = getattr(self.config_entry, "runtime_data", None)
        # Coordinator may not be a BePricesCoordinator if the entry is
        # mid-reload (UNDEFINED sentinel) or never finished setup.
        if not isinstance(coord, BePricesCoordinator):
            return {
                "current_supplier": str(current.get(CONF_SUPPLIER, "")),
                "current_contract": str(current.get(CONF_CONTRACT, "")),
                "compare_supplier": str(self._compare.get(CONF_SUPPLIER, "")),
                "compare_contract": str(self._compare.get(CONF_CONTRACT, "")),
                "current_per_kwh": "-",
                "compare_per_kwh": "-",
                "current_annual": "-",
                "compare_annual": "-",
                "delta_annual": "-",
                "annual_kwh": f"{DEFAULT_ANNUAL_KWH:.0f}",
                "error": "current entry is reloading; try again in a moment",
            }

        region = current[CONF_REGION]
        dso = current[CONF_DSO]
        meter = current.get(CONF_METER, METER_MONO)
        dso_mode = current.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        peak_kw = max(coord._peak_kw or 0.0, VREG_CAPACITY_FLOOR_KW)

        now_utc = dt_util.utcnow()
        now_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        spot = (coord._spot_cache or {}).get(now_hour)

        placeholders: dict[str, str] = {
            "current_supplier": _label_for_supplier(current[CONF_SUPPLIER]),
            "current_contract": _label_for_contract(
                current[CONF_SUPPLIER], current[CONF_CONTRACT]
            ),
            "compare_supplier": _label_for_supplier(self._compare[CONF_SUPPLIER]),
            "compare_contract": _label_for_contract(
                self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
            ),
            "current_per_kwh": "-",
            "compare_per_kwh": "-",
            "current_annual": "-",
            "compare_annual": "-",
            "delta_annual": "-",
            "annual_kwh": f"{DEFAULT_ANNUAL_KWH:.0f}",
            "error": "",
        }

        current_per_kwh: float | None = None
        if coord._snapshot is not None:
            try:
                bd = compute_breakdown(
                    coord._snapshot,
                    dso,
                    region,
                    dt_util.as_local(now_utc),
                    spot,
                    meter,
                    dso_mode,
                )
                current_per_kwh = bd.all_in
            except Exception:  # noqa: BLE001 - degrade to '-'
                pass

        # Other supplier: fetch + compute.
        session = async_get_clientsession(self.hass)
        other_extractor = get_extractor(self._compare[CONF_SUPPLIER])
        other_per_kwh: float | None = None
        other_snap = None
        try:
            other_snap = await other_extractor.fetch(
                session, self._compare[CONF_CONTRACT], region
            )
        except Exception as err:  # noqa: BLE001
            placeholders["error"] = f"could not fetch quote: {err}"
        else:
            if dso not in other_snap.dsos:
                placeholders["error"] = (
                    f"{self._compare[CONF_SUPPLIER]} doesn't serve DSO {dso}"
                )
            else:
                try:
                    bd = compute_breakdown(
                        other_snap,
                        dso,
                        region,
                        dt_util.as_local(now_utc),
                        spot,
                        meter,
                        dso_mode,
                    )
                    other_per_kwh = bd.all_in
                except Exception as err:  # noqa: BLE001
                    placeholders["error"] = f"compute failed: {err}"

        if current_per_kwh is not None:
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = (
                f"{_annual_bill(coord._snapshot, self.config_entry, peak_kw, current_per_kwh, DEFAULT_ANNUAL_KWH):.2f}"
            )
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, self.config_entry, peak_kw, other_per_kwh, DEFAULT_ANNUAL_KWH):.2f}"
            )
        if (
            current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and coord._snapshot is not None
        ):
            delta = _annual_bill(
                other_snap,
                self.config_entry,
                peak_kw,
                other_per_kwh,
                DEFAULT_ANNUAL_KWH,
            ) - _annual_bill(
                coord._snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                DEFAULT_ANNUAL_KWH,
            )
            placeholders["delta_annual"] = f"{'+' if delta >= 0 else ''}{delta:.2f}"
        return placeholders


def _label_for_supplier(supplier_id: str) -> str:
    try:
        return get_extractor(supplier_id).label
    except Exception:  # noqa: BLE001 - stale id
        return supplier_id


def _label_for_contract(supplier_id: str, contract_id: str) -> str:
    try:
        for c in get_extractor(supplier_id).contracts:
            if c.id == contract_id:
                return c.label
    except Exception:  # noqa: BLE001 - stale id
        pass
    return contract_id


def _annual_bill(
    snapshot: Any,
    entry: ConfigEntry,
    peak_kw: float,
    per_kwh: float,
    annual_kwh: float,
) -> float:
    """Estimated full-year EUR bill for ``snapshot`` at the current
    entry's region / DSO / solar / peak. Uses ``annual_kwh`` as the
    consumption assumption (3500 kWh = typical Belgian household).

    Composes:
      energy: per_kwh * annual_kwh
      yearly_fixed_fee
      energy_fund: 12 * snapshot.taxes.energy_fund_eur_per_month
      capacity (Flanders only): 12 * _compute_capacity(...)
      prosumer (Walloon compensation only): 12 * _compute_prosumer(...)
    """
    from .coordinator import _compute_capacity, _compute_prosumer

    yearly_fixed = float(getattr(snapshot.energy, "yearly_fixed_fee", 0.0) or 0.0)
    energy_fund = 12.0 * float(snapshot.taxes.energy_fund_eur_per_month or 0.0)
    capacity = 0.0
    if entry.data.get(CONF_REGION) == REGION_FLANDERS:
        capacity = 12.0 * _compute_capacity(snapshot, entry, peak_kw)
    prosumer = 12.0 * _compute_prosumer(snapshot, entry)
    return per_kwh * annual_kwh + yearly_fixed + energy_fund + capacity + prosumer
