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
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import EntsoeAuthError, EntsoeClient, EntsoeError
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


def _compare_supplier_options(region: str, current_kind: str) -> list[SelectOptionDict]:
    """Suppliers that have at least one contract available in the
    user's region. ``current_kind`` is kept in the signature for
    callers that may want to pre-filter, but the compare flow now
    accepts cross-kind quotes (static <-> dynamic) -- the dynamic
    side is priced from the user's spot cache or a fresh ENTSO-E
    fetch when crossing into dynamic territory."""
    out: list[SelectOptionDict] = []
    for ext in all_extractors():
        if region not in ext.regions():
            continue
        if not any(region in c.regions for c in ext.contracts):
            continue
        out.append(SelectOptionDict(value=ext.id, label=ext.label))
    return out


def _compare_contract_schema(
    supplier_id: str, region: str, current_kind: str, exclude_contract: str
) -> vol.Schema:
    """Contract picker scoped to the user's region, minus the user's
    current contract (so they don't quote against themselves).
    Includes both static and dynamic contracts so the user can ask
    'should I switch from fixed to dynamic'."""
    contracts = [
        c for c in _contracts_for(supplier_id, region) if c.id != exclude_contract
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
    return vol.Schema(
        {
            vol.Required(CONF_API_KEY, default=current): TextSelector(
                TextSelectorConfig(type=TextSelectorType.PASSWORD)
            )
        }
    )


async def _validate_entsoe_key(hass: HomeAssistant, api_key: str) -> str | None:
    """Test the ENTSO-E key with a day-ahead query.

    Returns ``None`` on success, ``"invalid_api_key"`` when ENTSO-E
    rejects the token, or ``"cannot_connect"`` for transport / parse
    errors and for HTTP 200 responses that come back as an
    Acknowledgement_MarketDocument with no TimeSeries. Use a 24h
    window anchored on yesterday: a quota-exhausted token returns
    HTTP 200 + an empty Acknowledgement, and the BE bidding zone
    rarely (never, in practice) goes a full local day with no
    publication, so an empty 24h response really does mean "the
    server can't fulfil the request" - whether quota or maintenance,
    the right answer is "key not usable" rather than letting the
    user finalise an entry that fails on first refresh.
    """
    session = async_get_clientsession(hass)
    client = EntsoeClient(api_key, session)
    yesterday = dt_util.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0
    ) - timedelta(days=1)
    try:
        prices = await client.fetch_day_ahead(yesterday, yesterday + timedelta(days=1))
    except EntsoeAuthError:
        return "invalid_api_key"
    except EntsoeError:
        return "cannot_connect"
    if not prices:
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

    Walks two paths:

    1. ``utility_meter`` config entries (modern UI-configured helpers).
       These store ``source`` + ``tariffs`` in entry options and their
       per-tariff child sensors share the entry's config_entry_id.

    2. Entity-registry entries with ``platform == "utility_meter"`` and
       no config_entry_id (YAML-configured helpers; common in older
       HA installs). The source + tariff name come from the live
       state attributes set by the utility_meter component.

    Bails on any ambiguity rather than guessing -- a wrong day/night
    pick mis-bills the year cost.
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

    # YAML-rooted helpers: walk the entity registry for utility_meter
    # children whose runtime ``source`` attribute matches our grid
    # sensor. The ``tariff`` attribute carries the configured tariff
    # name, which we classify the same way as UI-configured tariffs.
    ent_reg = er.async_get(hass)
    yaml_slot_to_entity: dict[str, str] = {}
    for re_entry in ent_reg.entities.values():
        if re_entry.platform != "utility_meter":
            continue
        if re_entry.config_entry_id is not None:
            continue  # UI-configured, already handled above
        state = hass.states.get(re_entry.entity_id)
        if state is None:
            continue
        if state.attributes.get("source") != source_entity_id:
            continue
        tariff_name = str(state.attributes.get("tariff") or "")
        slot = _classify_tariff(tariff_name)
        if slot is None:
            continue
        if slot in yaml_slot_to_entity:
            return {}  # ambiguous: two YAML children for the same slot
        yaml_slot_to_entity[slot] = re_entry.entity_id
    if "day" in yaml_slot_to_entity and "night" in yaml_slot_to_entity:
        return yaml_slot_to_entity
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


# ---- shared wizard steps ------------------------------------------------------


class _WizardStepsMixin:
    """Wizard steps shared by ``BePricesConfigFlow`` and ``BePricesOptionsFlow``.

    Both flows walk supplier -> contract -> dso -> meter -> ... -> meters; only
    the entry step and ``_finalize`` differ. ``_after_meter`` is overridden in
    ``BePricesConfigFlow`` to add the install-time unique-id reject.
    """

    _data: dict[str, Any]

    if TYPE_CHECKING:
        hass: HomeAssistant

        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult: ...
        def async_abort(self, **kwargs: Any) -> ConfigFlowResult: ...

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
        raise NotImplementedError


# ---- ConfigFlow ---------------------------------------------------------------


class BePricesConfigFlow(_WizardStepsMixin, ConfigFlow, domain=DOMAIN):
    """Multi-step config flow."""

    VERSION = 1

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
        return await super()._after_meter()

    def _finalize(self) -> ConfigFlowResult:
        return self.async_create_entry(title=_entry_title(self._data), data=self._data)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> BePricesOptionsFlow:
        return BePricesOptionsFlow()


# ---- OptionsFlow --------------------------------------------------------------


class BePricesOptionsFlow(_WizardStepsMixin, OptionsFlow):
    """Walk every config step pre-filled, save back to entry.data.

    Two top-level paths from the init menu: edit the existing entry
    (the original options flow) or run a one-off comparison quote
    against a different supplier (no save, no extra entry).
    """

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
        # ``self._data`` was seeded as ``{**entry.data, **entry.options}`` so
        # an entry that already carried options would otherwise miss this
        # shortcut on every re-edit (the merged dict can never equal
        # entry.data alone). Compare against the same merge so a no-op
        # re-edit really skips the reload.
        merged = {**self.config_entry.data, **self.config_entry.options}
        unchanged = (
            merged == self._data
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
            return await self.async_step_compare_meter()
        # Only show same-kind contracts (filter built into the schema)
        # and exclude the user's current contract iff the picked supplier
        # is the user's current one.
        exclude = (
            current[CONF_CONTRACT]
            if self._compare[CONF_SUPPLIER] == current[CONF_SUPPLIER]
            else ""
        )
        # Picking yourself when the supplier only has one contract in
        # your region leaves the dropdown empty with nothing to confirm.
        # Abort with the same reason as "no alternative supplier" so
        # the user knows there's nothing to compare against.
        remaining = [
            c
            for c in _contracts_for(self._compare[CONF_SUPPLIER], current[CONF_REGION])
            if c.id != exclude
        ]
        if not remaining:
            return self.async_abort(reason="compare_no_alternative")
        return self.async_show_form(
            step_id="compare_contract",
            data_schema=_compare_contract_schema(
                self._compare[CONF_SUPPLIER],
                current[CONF_REGION],
                current_kind,
                exclude,
            ),
        )

    async def async_step_compare_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally override the meter type for the comparison.

        Static contracts (fixed / variable) can be quoted at mono or
        bi-hourly billing -- some users want to know "what would I pay
        if I switched billing mode AND supplier". Dynamic / TOU
        contracts skip this step: their distribution requires a smart
        meter, picking bi-hourly would route distribution one way and
        energy another.
        """
        if user_input is not None:
            self._compare.update(user_input)
            return await self._after_compare_meter()
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        if other_kind in ("dynamic", "tou"):
            self._compare[CONF_METER] = METER_DYNAMIC
            return await self._after_compare_meter()
        current_meter = self.config_entry.data.get(CONF_METER, METER_MONO)
        return self.async_show_form(
            step_id="compare_meter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_METER, default=current_meter): SelectSelector(
                        SelectSelectorConfig(
                            options=list(METER_TYPES),
                            mode=SelectSelectorMode.LIST,
                            translation_key="meter",
                        )
                    )
                }
            ),
        )

    async def _after_compare_meter(self) -> ConfigFlowResult:
        """Hand off to compare_result, prompting for an ENTSO-E key
        first if the alternative is a dynamic contract and the user's
        current entry doesn't already carry one (i.e. they're on a
        static contract today and we have no spot data to price the
        dynamic side)."""
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        if other_kind == "dynamic" and not self.config_entry.data.get(CONF_API_KEY):
            return await self.async_step_compare_api_key()
        return await self.async_step_compare_result()

    async def async_step_compare_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Static-vs-dynamic compare needs an ENTSO-E key for the
        dynamic side's hour rate. Borrow the user's existing key when
        their entry already has one (handled in _after_compare_meter);
        otherwise prompt and validate against the live endpoint
        before reaching the result page."""
        errors: dict[str, str] = {}
        if user_input is not None:
            err = await _validate_entsoe_key(self.hass, user_input[CONF_API_KEY])
            if err is None:
                self._compare[CONF_API_KEY] = user_input[CONF_API_KEY]
                return await self.async_step_compare_result()
            errors[CONF_API_KEY] = err
        return self.async_show_form(
            step_id="compare_api_key",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_API_KEY): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
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

        DEFAULT_ANNUAL_KWH = 3500.0  # fallback when no consumption sensor is wired

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
                "current_ytd": "-",
                "compare_ytd": "-",
                "delta_ytd": "-",
                "annual_kwh": f"{DEFAULT_ANNUAL_KWH:.0f}",
                "ytd_kwh": "-",
                "consumption_source": "default (entry reloading)",
                "annual_chart": "",
                "ytd_chart": "",
                "error": "current entry is reloading; try again in a moment",
            }

        region = current[CONF_REGION]
        dso = current[CONF_DSO]
        # Comparison may override the meter type for static contracts;
        # falls back to the current entry's setting.
        meter = self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
        dso_mode = current.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        peak_kw = max(coord._peak_kw or 0.0, VREG_CAPACITY_FLOOR_KW)
        regime = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)

        now_utc = dt_util.utcnow()
        now_hour = now_utc.replace(minute=0, second=0, microsecond=0)
        today_local = dt_util.now().date()
        jan1 = today_local.replace(month=1, day=1)
        year_ago = today_local - timedelta(days=365)
        # Inclusive of today: leap years -> 366. Compute via
        # (Jan 1 next year - Jan 1 this year) so today=Feb 29 doesn't
        # raise (year+1 has no Feb 29).
        days_in_year = (date(today_local.year + 1, 1, 1) - jan1).days
        days_elapsed = (today_local - jan1).days + 1
        fee_proration = days_elapsed / days_in_year
        spot_dict: dict[datetime, float] = (
            dict(coord._spot_cache) if coord._spot_cache else {}
        )
        # Cross-kind comparisons (static <-> dynamic) need spot data
        # for the dynamic side. The user's coordinator already has
        # spots when they're on dynamic; otherwise borrow the api key
        # they just typed in compare_api_key (or the one already on
        # their entry) and fetch the day-ahead window for today.
        current_kind = _contract_kind(current[CONF_SUPPLIER], current[CONF_CONTRACT])
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        need_spot = "dynamic" in (current_kind, other_kind)
        if need_spot and not spot_dict:
            api_key = self._compare.get(CONF_API_KEY) or current.get(CONF_API_KEY)
            if api_key:
                from .api import EntsoeClient

                try:
                    client = EntsoeClient(api_key, async_get_clientsession(self.hass))
                    day_start = now_utc.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    spot_dict = await client.fetch_day_ahead(
                        day_start, day_start + timedelta(days=1)
                    )
                except Exception:  # noqa: BLE001 - degrade to '-' for the dynamic side
                    pass
        spot = spot_dict.get(now_hour)

        # Measured consumption / injection from the user's kWh sensors.
        # Injection is only relevant when a solar regime is configured;
        # for the "none" regime it stays 0 even if a sensor is wired.
        rolling_year_kwh = await _read_total_kwh(
            self.hass, self.config_entry, year_ago, today_local
        )
        ytd_kwh = await _read_total_kwh(self.hass, self.config_entry, jan1, today_local)
        rolling_inj_kwh = 0.0
        ytd_inj_kwh = 0.0
        if regime != SOLAR_REGIME_NONE:
            r = await _read_total_kwh(
                self.hass, self.config_entry, year_ago, today_local, side="injection"
            )
            y = await _read_total_kwh(
                self.hass, self.config_entry, jan1, today_local, side="injection"
            )
            rolling_inj_kwh = r or 0.0
            ytd_inj_kwh = y or 0.0
        if rolling_year_kwh is not None:
            annual_kwh = rolling_year_kwh
            consumption_source = "measured (last 365 days)"
        else:
            annual_kwh = DEFAULT_ANNUAL_KWH
            consumption_source = (
                "default 3500 kWh - wire a kWh sensor for a measured estimate"
            )

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
            "current_ytd": "-",
            "compare_ytd": "-",
            "delta_ytd": "-",
            "annual_kwh": f"{annual_kwh:.0f}",
            "ytd_kwh": f"{ytd_kwh:.0f}" if ytd_kwh is not None else "-",
            "annual_chart": "",
            "ytd_chart": "",
            "ytd_injection_kwh": (
                f"{ytd_inj_kwh:.0f}" if regime != SOLAR_REGIME_NONE else "-"
            ),
            "solar_note": _solar_note(regime, rolling_inj_kwh),
            "consumption_source": consumption_source,
            "meter_used": meter,
            "error": "",
        }

        current_per_kwh: float | None = None
        if coord._snapshot is not None:
            current_per_kwh = _tou_weighted_per_kwh(
                coord._snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                spot,
                meter,
                dso_mode,
            )

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
                other_per_kwh = _tou_weighted_per_kwh(
                    other_snap,
                    dso,
                    region,
                    dt_util.as_local(now_utc),
                    spot,
                    meter,
                    dso_mode,
                )
                if other_per_kwh is None:
                    placeholders["error"] = "compute failed"

        # Per-supplier injection price (only used in the "injection"
        # regime; compensation regime nets at the meter, none has
        # nothing to credit). Compute from each snapshot via the
        # coordinator's existing helper, which returns None when the
        # snapshot has no injection data or the user isn't on the
        # injection regime.
        from .coordinator import _compute_injection_price

        current_inj_price: float | None = None
        compare_inj_price: float | None = None
        if regime == "injection":
            if coord._snapshot is not None:
                current_inj_price = _compute_injection_price(
                    coord._snapshot, self.config_entry, spot_dict
                )
            if other_snap is not None:
                compare_inj_price = _compute_injection_price(
                    other_snap, self.config_entry, spot_dict
                )

        if current_per_kwh is not None:
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = (
                f"{_annual_bill(coord._snapshot, self.config_entry, peak_kw, current_per_kwh, annual_kwh, rolling_inj_kwh, current_inj_price):.2f}"
            )
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, self.config_entry, peak_kw, other_per_kwh, annual_kwh, rolling_inj_kwh, compare_inj_price):.2f}"
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
                annual_kwh,
                rolling_inj_kwh,
                compare_inj_price,
            ) - _annual_bill(
                coord._snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
            )
            placeholders["delta_annual"] = f"{'+' if delta >= 0 else ''}{delta:.2f}"

        # Year-to-date what-if. Two paths:
        #   1. Archive-capable suppliers (Eneco / Cociter / Ecopower):
        #      reuse the coordinator's _compute_current_year_cost engine
        #      against each snapshot chain, so per-month tariff transitions
        #      and the same proration model the user's actual bill uses
        #      apply to both sides. Most accurate.
        #   2. Suppliers without an archive (Bolt / Mega / OCTA+ / Engie /
        #      Luminus / DATS 24 / TotalEnergies): fall back to the simple
        #      "current rate * ytd_kwh + pro-rated fees" model. Same per_kwh
        #      and same proration on both sides, so the delta still isolates
        #      the supplier-driven difference.
        from .coordinator import _compute_current_year_cost

        current_extractor = get_extractor(current[CONF_SUPPLIER])
        archive_capable = (
            current_extractor.fetch_for_month is not None
            and other_extractor.fetch_for_month is not None
        )
        if archive_capable and other_snap is not None and coord._snapshot is not None:
            try:
                current_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    current_extractor,
                    coord._snapshot,
                    self.config_entry,
                )
                compare_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    other_extractor,
                    other_snap,
                    self.config_entry,
                    contract_override=self._compare[CONF_CONTRACT],
                    meter_override=meter,
                )
            except Exception:  # noqa: BLE001 - degrade to '-'
                current_ytd_val = None
                compare_ytd_val = None
            if current_ytd_val is not None and compare_ytd_val is not None:
                placeholders["current_ytd"] = f"{current_ytd_val:.2f}"
                placeholders["compare_ytd"] = f"{compare_ytd_val:.2f}"
                ytd_delta = compare_ytd_val - current_ytd_val
                placeholders["delta_ytd"] = (
                    f"{'+' if ytd_delta >= 0 else ''}{ytd_delta:.2f}"
                )
                _populate_charts(
                    placeholders,
                    current_label=_label_for_supplier(current[CONF_SUPPLIER]),
                    compare_label=_label_for_supplier(self._compare[CONF_SUPPLIER]),
                )
                return placeholders
            # Fall through to the simple model on engine failure.

        if (
            ytd_kwh is not None
            and current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and coord._snapshot is not None
        ):
            current_ytd = _annual_bill(
                coord._snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                current_inj_price,
                fee_proration=fee_proration,
            )
            compare_ytd = _annual_bill(
                other_snap,
                self.config_entry,
                peak_kw,
                other_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                compare_inj_price,
                fee_proration=fee_proration,
            )
            placeholders["current_ytd"] = f"{current_ytd:.2f}"
            placeholders["compare_ytd"] = f"{compare_ytd:.2f}"
            ytd_delta = compare_ytd - current_ytd
            placeholders["delta_ytd"] = (
                f"{'+' if ytd_delta >= 0 else ''}{ytd_delta:.2f}"
            )
        _populate_charts(
            placeholders,
            current_label=_label_for_supplier(current[CONF_SUPPLIER]),
            compare_label=_label_for_supplier(self._compare[CONF_SUPPLIER]),
        )
        return placeholders


def _tou_weighted_per_kwh(
    snapshot: Any,
    dso: str,
    region: str,
    when_now: datetime,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
) -> float | None:
    """Per-kWh EUR/kWh for the compare flow's annual estimate, with a
    TOU-aware time-weighted average when the snapshot's energy rate
    splits by hour-of-day.

    For Fixed / Variable / Dynamic the live breakdown at ``when_now``
    is the right number. For TOU contracts (Luminus SmartFlex, Engie
    Empower Flextime) ``compute_breakdown`` returns one of three slot
    rates depending on the hour the user opens the dialog -- biased.
    Compute breakdowns at three representative weekday hours (one per
    slot) and weight by the standard CWaPE-defined slot durations
    across a week, so the annual estimate isn't dragged toward
    whichever slot the user happens to be in.

    Returns ``None`` on compute failure so the caller can render '-'
    on the result page rather than tear the flow down.
    """
    from .pricing import compute_breakdown, is_belgian_holiday
    from .providers.base import TimeOfUseRates

    try:
        bd = compute_breakdown(snapshot, dso, region, when_now, spot, meter, dso_mode)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(snapshot.energy, TimeOfUseRates):
        return bd.all_in
    # Pick a recent non-holiday weekday so each slot lookup hits the
    # weekday rule. Walk back from today's local date.
    weekday = when_now.date()
    for _ in range(8):
        if not is_belgian_holiday(weekday) and weekday.weekday() < 5:
            break
        weekday -= timedelta(days=1)
    base = datetime.combine(weekday, time(), tzinfo=when_now.tzinfo)
    # CWaPE weekday TOU windows (shared across products):
    #   peak       07-11 + 17-22
    #   transition 11-17 + 22-01
    #   offpeak    01-07
    # Pick one hour comfortably inside each window so the slot lookup
    # is unambiguous regardless of off-by-one boundary handling.
    try:
        bd_peak = compute_breakdown(
            snapshot, dso, region, base.replace(hour=9), spot, meter, dso_mode
        )
        bd_trans = compute_breakdown(
            snapshot, dso, region, base.replace(hour=13), spot, meter, dso_mode
        )
        bd_offpeak = compute_breakdown(
            snapshot, dso, region, base.replace(hour=3), spot, meter, dso_mode
        )
    except Exception:  # noqa: BLE001
        return bd.all_in  # fall back to live slot rate
    # Slot weights = hours-per-week the slot is active, derived from
    # the published TOU rules and a 5-weekday / 2-weekend split.
    if snapshot.energy.weekend_rule == "weekend_no_peak":
        # Engie Empower Flextime: weekend is transition (07-01) +
        # offpeak (01-07 + 11-17). Weekday rule applies on weekdays.
        wp, wt, wo = 45.0, 69.0, 54.0
    else:
        # weekend_offpeak (Luminus SmartFlex, default): weekends are
        # entirely off-peak.
        wp, wt, wo = 45.0, 45.0, 78.0
    total = wp + wt + wo
    return (bd_peak.all_in * wp + bd_trans.all_in * wt + bd_offpeak.all_in * wo) / total


def _populate_charts(
    placeholders: dict[str, str], *, current_label: str, compare_label: str
) -> None:
    """Render the annual / YTD bars from the numeric placeholders.

    Reads the ``current_annual`` / ``compare_annual`` (and YTD pair)
    placeholders and replaces ``annual_chart`` / ``ytd_chart`` with a
    two-row bar visualisation. Leaves them empty when either side is
    "-" so the result page still looks clean for the no-quote-yet
    case (e.g. fetch failed)."""
    for prefix, chart_key in (("annual", "annual_chart"), ("ytd", "ytd_chart")):
        cur = placeholders.get(f"current_{prefix}", "-")
        cmp_ = placeholders.get(f"compare_{prefix}", "-")
        if cur == "-" or cmp_ == "-":
            continue
        try:
            cur_v = float(cur)
            cmp_v = float(cmp_)
        except ValueError:
            continue
        placeholders[chart_key] = _bar_chart(
            {current_label: cur_v, compare_label: cmp_v}
        )


def _bar_chart(values: dict[str, float], width: int = 20) -> str:
    """Two-row unicode bar chart, both rows scaled against the larger
    value so the visual ratio matches the numeric one. Labels are
    padded so the bars line up. Returns ``""`` when any input is non-
    finite (negative-billing cases are clamped to zero for the bar
    only; the EUR values still render to keep the sign visible)."""
    if not values:
        return ""
    max_v = max(max(values.values(), default=0.0), 1.0)
    label_w = max(len(k) for k in values)
    rows: list[str] = []
    for label, v in values.items():
        bar_v = max(v, 0.0)  # negative annuals (huge solar credit) clamp to empty
        filled = round((bar_v / max_v) * width)
        filled = max(0, min(width, filled))
        bar = "█" * filled + "░" * (width - filled)
        rows.append(f"  {label.ljust(label_w)} {bar} {v:.0f} EUR")
    return "\n".join(rows)


def _solar_note(regime: str, rolling_inj_kwh: float) -> str:
    """One-line description of how solar is folded into the comparison.

    Renders into the result form's description placeholder. Empty for
    the no-solar case so the page doesn't show a misleading label."""
    if regime == "compensation":
        if rolling_inj_kwh > 0:
            return f"compensation regime: meter netted (consumption -= {rolling_inj_kwh:.0f} kWh, surplus forfeited)"
        return "compensation regime configured but no injection sensor wired - net = consumption"
    if regime == "injection":
        if rolling_inj_kwh > 0:
            return f"injection regime: {rolling_inj_kwh:.0f} kWh credited at each supplier's injection price"
        return "injection regime configured but no injection sensor wired - no injection credit applied"
    return ""


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
    consumption_kwh: float,
    injection_kwh: float = 0.0,
    injection_price: float | None = None,
    fee_proration: float = 1.0,
) -> float:
    """Estimated EUR bill for ``snapshot`` over the period that produced
    ``consumption_kwh`` and ``injection_kwh``.

    ``fee_proration`` scales the EUR/year fee components (1.0 for a
    full year, ``days_elapsed/days_in_year`` for YTD).

    Solar handling honours the entry's configured regime:

    - ``"none"``: ``cost = consumption_kwh * per_kwh + fees``
    - ``"compensation"``: meter is netted 1:1 (Walloon pre-2024
      installations until 2030). The billable kWh is
      ``max(consumption - injection, 0)``; surplus injection is
      forfeited, never paid out. Fees include the prosumer charge.
    - ``"injection"``: consumption is billed at ``per_kwh`` AND
      injection is credited at ``injection_price``; the credit is
      subtracted from the cost and can drive the bill negative when
      injection income exceeds consumption + fees.
    """
    fees = _annual_fees(snapshot, entry, peak_kw) * fee_proration
    regime = entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    if regime == "compensation":
        billable = max(consumption_kwh - injection_kwh, 0.0)
        return fees + per_kwh * billable
    if regime == "injection" and injection_price is not None:
        return fees + per_kwh * consumption_kwh - injection_price * injection_kwh
    return fees + per_kwh * consumption_kwh


def _annual_fees(snapshot: Any, entry: ConfigEntry, peak_kw: float) -> float:
    """Just the EUR/year fee components (no per-kWh term).

    Pulled out so the YTD comparison can pro-rate fees by the elapsed
    fraction of the year without re-computing the per-kWh part."""
    from .coordinator import _compute_capacity, _compute_prosumer

    yearly_fixed = float(getattr(snapshot.energy, "yearly_fixed_fee", 0.0) or 0.0)
    energy_fund = 12.0 * float(snapshot.taxes.energy_fund_eur_per_month or 0.0)
    capacity = 0.0
    if entry.data.get(CONF_REGION) == REGION_FLANDERS:
        capacity = 12.0 * _compute_capacity(snapshot, entry, peak_kw)
    prosumer = 12.0 * _compute_prosumer(snapshot, entry)
    return yearly_fixed + energy_fund + capacity + prosumer


async def _read_total_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    start: date,
    end: date,
    *,
    side: str = "consumption",
) -> float | None:
    """Sum of consumption (or injection) kWh between ``start`` and ``end``
    from the entry's configured kWh sensors.

    Prefers the 4-register day/night wiring when both are filled (more
    accurate when the meter exposes them directly); falls back to the
    single cumulative sensor. Returns ``None`` when no sensor is wired
    or the recorder has nothing in the requested window -- the caller
    falls back to a default consumption assumption in that case so the
    quote page still renders."""
    from .coordinator import _recorder_daily_kwh

    if side == "injection":
        day_id = entry.data.get(CONF_DAY_INJECTION_KWH)
        night_id = entry.data.get(CONF_NIGHT_INJECTION_KWH)
        total_id = entry.data.get(CONF_INJECTION_KWH)
    else:
        day_id = entry.data.get(CONF_DAY_CONSUMPTION_KWH)
        night_id = entry.data.get(CONF_NIGHT_CONSUMPTION_KWH)
        total_id = entry.data.get(CONF_CONSUMPTION_KWH)
    if day_id and night_id:
        d = await _recorder_daily_kwh(hass, day_id, start, end)
        n = await _recorder_daily_kwh(hass, night_id, start, end)
        total = sum(d.values()) + sum(n.values())
        return total if total > 0 else None
    if total_id:
        d = await _recorder_daily_kwh(hass, total_id, start, end)
        total = sum(d.values())
        return total if total > 0 else None
    return None
