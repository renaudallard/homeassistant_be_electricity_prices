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

from dataclasses import replace

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
    BooleanSelector,
    DateSelector,
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
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_API_KEY,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONNECTION_KVA_TIER,
    CONF_CONSUMPTION_KWH,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_CUSTOM_DSO_BRUSSELS_OSP,
    CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR,
    CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE,
    CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR,
    CONF_CUSTOM_DSO_TRANSPORT,
    CONF_CUSTOM_ENERGY_BASE,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_ENERGY_FACTOR,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_CUSTOM_ENERGY_QUARTER_HOURLY,
    CONF_CUSTOM_ENERGY_SINGLE,
    CONF_CUSTOM_INJECTION_BASE,
    CONF_CUSTOM_INJECTION_CURRENT,
    CONF_CUSTOM_INJECTION_FACTOR,
    CONF_CUSTOM_INJECTION_FLOOR,
    CONF_CUSTOM_INJECTION_MODE,
    CONF_CUSTOM_INJECTION_SPP_WEIGHTED,
    CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION,
    CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH,
    CONF_CUSTOM_TAX_FEDERAL_EXCISE,
    CONF_CUSTOM_TAX_REGION_CONNECTION_FEE,
    CONF_CUSTOM_TAX_REGIONAL_RENEWABLES,
    CONF_CUSTOM_VAT_RATE,
    CONF_CUSTOM_YEARLY_FIXED_FEE,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INCLUDE_VAT,
    CONF_INJECTION_KWH,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CONNECTION_KVA_TIERS,
    CUSTOM_CONTRACT_DYNAMIC,
    CUSTOM_CONTRACT_FIXED,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_INJECTION_MODE_CURRENT,
    CUSTOM_INJECTION_MODES,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_CONNECTION_KVA_TIER,
    DEFAULT_CUSTOM_VAT_RATE,
    DEFAULT_INCLUDE_VAT,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    DSO_TARIFF_MODES,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_NONE,
    SOLAR_REGIMES,
    SUPPLIER_CUSTOM,
    VREG_CAPACITY_FLOOR_KW,
    DOMAIN,
    DSO_CHOICES,
    METER_BI,
    METER_DYNAMIC,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    METER_TYPES,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    REGIONS,
)
from .providers import ExtractorError, all_extractors, get as get_extractor
from .providers.base import Contract


# ---- shared schema builders ---------------------------------------------------


def _supplier_options(
    region: str | None = None, keep: str | None = None
) -> list[SelectOptionDict]:
    """Selectable suppliers, dropping any that has announced its exit.

    ``keep`` is the supplier already stored on the entry being edited. It
    must be passed on every edit path: a SelectSelector rejects a default
    that is not among its options, so filtering unconditionally would make
    an existing entry on a withdrawn supplier impossible to edit.
    """
    extractors = all_extractors()
    if region is not None:
        extractors = tuple(e for e in extractors if region in e.regions())
    return [
        SelectOptionDict(value=e.id, label=e.label)
        for e in extractors
        if e.deprecated_until is None or e.id == keep
    ]


def _region_mismatch_error(data: dict[str, Any]) -> dict[str, str] | None:
    """Report a supplier that sells nothing in the chosen region.

    Supplier and region are picked on the SAME step, so the mismatch can only
    be judged once both are in. Detecting it a step later and aborting ends
    the flow, and in the options flow that discards every other change made in
    the same run -- the user re-opens the dialog to find their edits gone. The
    abort text even says "go back and pick a different combination", which HA
    gives no way to do from an abort.

    Returning it as a form error re-shows the step with everything still
    filled in, which is what the text has always described.
    """
    supplier = data.get(CONF_SUPPLIER)
    region = data.get(CONF_REGION)
    if not supplier or not region:
        return None
    try:
        available = _contracts_for(str(supplier), str(region))
    except ExtractorError:
        # Not this check's business: an unknown supplier id is rejected by the
        # selector itself.
        return None
    if available:
        return None
    return {CONF_SUPPLIER: "supplier_region_unavailable"}


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


def _contract_is_professional(supplier_id: str | None, contract_id: str | None) -> bool:
    """True when the chosen contract is a professional product, whose card
    is published excluding VAT and may band the federal excise by annual
    volume. Resolved from the registry's ``Contract.professional`` flag.
    """
    if not supplier_id or not contract_id:
        return False
    try:
        contracts = get_extractor(supplier_id).contracts
    except ExtractorError:
        return False
    return any(c.id == contract_id and c.professional for c in contracts)


def _professional_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_INCLUDE_VAT,
                default=bool(defaults.get(CONF_INCLUDE_VAT, DEFAULT_INCLUDE_VAT)),
            ): BooleanSelector(),
            vol.Required(
                CONF_ANNUAL_CONSUMPTION_KWH,
                default=float(
                    defaults.get(
                        CONF_ANNUAL_CONSUMPTION_KWH, DEFAULT_ANNUAL_CONSUMPTION_KWH
                    )
                ),
            ): NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=1_000_000,
                    step=100,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="kWh",
                )
            ),
        }
    )


def _contract_has_spot_injection(
    supplier_id: str | None, contract_id: str | None
) -> bool:
    """True when the chosen contract's injection is a per-hour spot
    formula needing an ENTSO-E key even though the energy isn't dynamic
    (Cociter Variable). Resolved from the registry's
    ``Contract.spot_indexed_injection`` flag.
    """
    if not supplier_id or not contract_id:
        return False
    try:
        contracts = get_extractor(supplier_id).contracts
    except ExtractorError:
        return False
    return any(c.id == contract_id and c.spot_indexed_injection for c in contracts)


def _compare_supplier_options(region: str, current_kind: str) -> list[SelectOptionDict]:
    """Suppliers that have at least one contract available in the
    user's region. ``current_kind`` is kept in the signature for
    callers that may want to pre-filter, but the compare flow now
    accepts cross-kind quotes (static <-> dynamic) -- the dynamic
    side is priced from the user's spot cache or a fresh ENTSO-E
    fetch when crossing into dynamic territory."""
    out: list[SelectOptionDict] = []
    for ext in all_extractors():
        # The expert custom supplier has no fetchable card, so it can't be a
        # comparison target (only the current side of a quote).
        if ext.id == SUPPLIER_CUSTOM:
            continue
        # Nor can a supplier that is leaving the market: quoting a user into
        # a contract that is about to be transferred away is never useful.
        if ext.deprecated_until is not None:
            continue
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
                    # keep= so an entry already on a withdrawn supplier can
                    # still be edited; a fresh setup passes no default and
                    # therefore is not offered it.
                    options=_supplier_options(keep=defaults.get(CONF_SUPPLIER)),
                    mode=SelectSelectorMode.DROPDOWN,
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
    fields: dict[Any, Any] = (
        {vol.Required(CONF_CONTRACT, default=current): selector}
        if current in valid_ids
        else {vol.Required(CONF_CONTRACT): selector}
    )
    _add_contract_date_fields(fields, defaults)
    return vol.Schema(fields)


def _add_contract_date_fields(fields: dict[Any, Any], defaults: dict[str, Any]) -> None:
    """Append the optional contract start/end date pickers.

    Pre-filled with the stored value as a *suggestion* (not a default) on the
    options / reconfigure pass, so blanking the picker truly omits the key from
    ``user_input`` -- the step handler then pops it, which is how a date is
    cleared. A ``default`` would re-inject the stored value on a blank submit,
    making the date unclearable.
    """
    date_selector = DateSelector()
    for key in (CONF_CONTRACT_START_DATE, CONF_CONTRACT_END_DATE):
        stored = defaults.get(key)
        if stored:
            fields[vol.Optional(key, description={"suggested_value": stored})] = (
                date_selector
            )
        else:
            fields[vol.Optional(key)] = date_selector


def _validate_contract_dates(user_input: dict[str, Any]) -> dict[str, str]:
    """Reject a future start date or an end date not after the start.

    Both fields are independently optional: an end date without a start date is
    fine (a bare renewal reminder), so the ordering check only fires when both
    are present.
    """
    from .coordinator import _parse_iso_date

    errors: dict[str, str] = {}
    start = _parse_iso_date(user_input.get(CONF_CONTRACT_START_DATE))
    end = _parse_iso_date(user_input.get(CONF_CONTRACT_END_DATE))
    if start is not None and start > dt_util.now().date():
        errors[CONF_CONTRACT_START_DATE] = "start_date_in_future"
    if start is not None and end is not None and end <= start:
        errors[CONF_CONTRACT_END_DATE] = "end_before_start"
    return errors


_MANUAL_RATE_KEYS: tuple[str, ...] = (
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_YEARLY_FEE,
)


def _add_manual_num(
    fields: dict[Any, Any],
    defaults: dict[str, Any],
    key: str,
    *,
    negative: bool = False,
) -> None:
    """Append an optional manual signing-rate field, pre-filled on reconfigure.

    The stored value is a *suggestion*, not a default, so blanking the box omits
    the key and the step handler can pop it (how the override is cleared). A
    ``default`` would re-inject the value on a blank submit.
    """
    stored = defaults.get(key)
    selector = _custom_num(negative=negative)
    if stored is not None:
        fields[vol.Optional(key, description={"suggested_value": float(stored)})] = (
            selector
        )
    else:
        fields[vol.Optional(key)] = selector


def _signed_rate_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Optional signing-rate override fields, shaped by the contract kind.

    Dynamic contracts collect factor / base (which a Belgian formula can drive
    negative); fixed contracts collect single, peak, off-peak and the
    exclusive-night circuit's own rate. Every field is optional and overrides
    only itself: leave one blank to keep the retrieved card's value for it, or
    the whole step blank to price entirely off the card. The meter step comes
    later in the wizard, so the rate boxes for every meter shape are offered
    here regardless of which one the user will pick.
    """
    kind = _contract_kind(
        defaults.get(CONF_SUPPLIER, ""), defaults.get(CONF_CONTRACT, "")
    )
    fields: dict[Any, Any] = {}
    if kind == "dynamic":
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_FACTOR, negative=True)
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_BASE, negative=True)
    else:
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_SINGLE)
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_PEAK)
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_OFFPEAK)
        _add_manual_num(fields, defaults, CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT)
    _add_manual_num(fields, defaults, CONF_MANUAL_YEARLY_FEE)
    return vol.Schema(fields)


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


def _connection_power_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Brussels-only step: which connection-power tier for the Brugel OSP fee?"""
    current = defaults.get(CONF_CONNECTION_KVA_TIER) or DEFAULT_CONNECTION_KVA_TIER
    return vol.Schema(
        {
            vol.Required(CONF_CONNECTION_KVA_TIER, default=current): SelectSelector(
                SelectSelectorConfig(
                    options=list(CONNECTION_KVA_TIERS),
                    mode=SelectSelectorMode.LIST,
                    translation_key="connection_kva_tier",
                )
            ),
        }
    )


def _custom_num(*, negative: bool = False) -> NumberSelector:
    """Number selector for a hand-entered EUR/kWh rate or coefficient.

    ``negative=True`` for values a Belgian formula can legitimately drive
    below zero (an injection factor/base, a spot multiplier/offset); the
    rest are floored at 0.
    """
    if negative:
        return NumberSelector(
            NumberSelectorConfig(step="any", mode=NumberSelectorMode.BOX)
        )
    return NumberSelector(
        NumberSelectorConfig(min=0.0, step="any", mode=NumberSelectorMode.BOX)
    )


def _add_custom_num(
    fields: dict[Any, Any],
    defaults: dict[str, Any],
    key: str,
    default: float = 0.0,
    *,
    negative: bool = False,
    fallback: bool = False,
) -> None:
    """Append a hand-entered custom-supplier number.

    ``fallback=True`` marks a rate the pricing engine FALLS BACK for when it is
    absent (the bi-hourly peak / off-peak split and the exclusive-night
    distribution rate all fall back to the single rate). Those must never carry
    a ``default``: a default is submitted verbatim when the user leaves the box
    alone, so 0,00 lands in the entry and the engine bills zero instead of
    falling back. Use the stored value as a *suggestion* instead, exactly as
    ``_add_manual_num`` does, so a blank box omits the key.
    """
    if fallback:
        stored = defaults.get(key)
        selector = _custom_num(negative=negative)
        if stored is not None:
            fields[
                vol.Optional(key, description={"suggested_value": float(stored)})
            ] = selector
        else:
            fields[vol.Optional(key)] = selector
        return
    fields[vol.Optional(key, default=float(defaults.get(key, default)))] = _custom_num(
        negative=negative
    )


def _custom_energy_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Energy formula fields for the chosen custom mode.

    Coefficients are entered excluding VAT (as printed on a tariff sheet);
    the ``custom_tax`` step's VAT rate grosses them up.
    """
    contract = defaults.get(CONF_CONTRACT)
    fields: dict[Any, Any] = {}
    if contract == CUSTOM_CONTRACT_FIXED:
        meter = defaults.get(CONF_METER, METER_MONO)
        _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_SINGLE)
        # Same rule as the DSO step and as pricing's ``bi_capable``
        # (`pricing.py:291`): a dynamic (SMR3) meter registers the day/night
        # split exactly like a bi-hourly one and ``_routed_rate`` bills both
        # through ``peak`` / ``offpeak``. Gating on METER_BI alone left a
        # custom fixed contract on a smart meter unable to enter its own two
        # rates, so all 24 hours fell back to the single rate.
        if meter in (METER_BI, METER_DYNAMIC):
            _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_PEAK, fallback=True)
            _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_OFFPEAK, fallback=True)
        if meter == METER_EXCLUSIVE_NIGHT:
            _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT)
    else:
        _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_FACTOR, 1.0, negative=True)
        _add_custom_num(fields, defaults, CONF_CUSTOM_ENERGY_BASE, negative=True)
        if contract == CUSTOM_CONTRACT_DYNAMIC:
            fields[
                vol.Optional(
                    CONF_CUSTOM_ENERGY_QUARTER_HOURLY,
                    default=bool(
                        defaults.get(CONF_CUSTOM_ENERGY_QUARTER_HOURLY, False)
                    ),
                )
            ] = BooleanSelector()
    _add_custom_num(fields, defaults, CONF_CUSTOM_YEARLY_FIXED_FEE)
    return vol.Schema(fields)


def _custom_injection_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Injection formula fields (shown only on the injection regime).

    A fixed-rate contract can only quote a flat ``current`` credit; the
    spot-indexed modes also accept a ``factor``/``base`` formula applied to
    the live spot (dynamic) or the monthly mean (monthly-average).
    """
    contract = defaults.get(CONF_CONTRACT)
    modes = (
        [CUSTOM_INJECTION_MODE_CURRENT]
        if contract == CUSTOM_CONTRACT_FIXED
        else list(CUSTOM_INJECTION_MODES)
    )
    # Clamp the default to the narrowed list: a formula mode stored under a
    # wider contract kind must not be pre-selected once the contract narrows
    # to current-only (mirrors the guard in _dso_schema / _meter_schema).
    mode_default = defaults.get(CONF_CUSTOM_INJECTION_MODE, modes[0])
    if mode_default not in modes:
        mode_default = modes[0]
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_CUSTOM_INJECTION_MODE,
            default=mode_default,
        ): SelectSelector(
            SelectSelectorConfig(
                options=modes,
                mode=SelectSelectorMode.LIST,
                translation_key="custom_injection_mode",
            )
        ),
    }
    _add_custom_num(fields, defaults, CONF_CUSTOM_INJECTION_CURRENT)
    _add_custom_num(fields, defaults, CONF_CUSTOM_INJECTION_FACTOR, 1.0, negative=True)
    _add_custom_num(fields, defaults, CONF_CUSTOM_INJECTION_BASE, negative=True)
    fields[
        vol.Optional(
            CONF_CUSTOM_INJECTION_FLOOR,
            default=bool(
                defaults.get(
                    CONF_CUSTOM_INJECTION_FLOOR,
                    contract == CUSTOM_CONTRACT_MONTHLY,
                )
            ),
        )
    ] = BooleanSelector()
    # SPP-weighting only applies to the monthly-average mode's formula
    # injection (weighting the month-mean by the Synergrid solar profile).
    if contract == CUSTOM_CONTRACT_MONTHLY:
        fields[
            vol.Optional(
                CONF_CUSTOM_INJECTION_SPP_WEIGHTED,
                default=bool(defaults.get(CONF_CUSTOM_INJECTION_SPP_WEIGHTED, False)),
            )
        ] = BooleanSelector()
    return vol.Schema(fields)


def _custom_dso_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Hand-entered DSO network overlay, only the region/meter-relevant
    fields. Everything but distribution_single defaults to 0."""
    region = defaults.get(CONF_REGION)
    meter = defaults.get(CONF_METER, METER_MONO)
    dso_mode = defaults.get(CONF_DSO_TARIFF_MODE)
    fields: dict[Any, Any] = {}
    _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE)
    # METER_DYNAMIC belongs here as much as METER_BI: an SMR3 meter registers
    # the bi-horaire split the same way, and pricing.network_eur_per_kwh routes
    # both through distribution_peak / distribution_offpeak whenever the DSO
    # mode is not "simple". A dynamic / TOU contract also FORCES this meter
    # (_meter_schema), so without these boxes a custom entry could never
    # supply the two rates its own network leg is billed on, and every hour
    # silently fell back to distribution_single.
    if meter in (METER_BI, METER_DYNAMIC):
        _add_custom_num(
            fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_PEAK, fallback=True
        )
        _add_custom_num(
            fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK, fallback=True
        )
    if meter == METER_EXCLUSIVE_NIGHT:
        _add_custom_num(
            fields,
            defaults,
            CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
            fallback=True,
        )
    _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_TRANSPORT)
    _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR)
    if region == REGION_FLANDERS:
        _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR)
    if region == REGION_WALLONIA:
        _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR)
        if dso_mode == DSO_MODE_IMPACT:
            _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_PIC)
            _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM)
            _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_ECO)
    if region == REGION_BRUSSELS:
        _add_custom_num(fields, defaults, CONF_CUSTOM_DSO_BRUSSELS_OSP)
    return vol.Schema(fields)


def _custom_tax_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Hand-entered taxes/levies overlay. One regional-renewables field is
    routed to the region's slot at build time; VAT grosses up every
    component (injection stays exempt)."""
    fields: dict[Any, Any] = {}
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_FEDERAL_EXCISE)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_REGIONAL_RENEWABLES)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_REGION_CONNECTION_FEE)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH)
    fields[
        vol.Optional(
            CONF_CUSTOM_VAT_RATE,
            default=float(defaults.get(CONF_CUSTOM_VAT_RATE, DEFAULT_CUSTOM_VAT_RATE)),
        )
    ] = NumberSelector(
        NumberSelectorConfig(min=0.0, max=1.0, step=0.01, mode=NumberSelectorMode.BOX)
    )
    return vol.Schema(fields)


def _meter_schema(
    supplier_id: str, contract_id: str, defaults: dict[str, Any]
) -> vol.Schema:
    # Dynamic, TOU, and TOU Impact contracts all require a smart (SMR3)
    # meter to bill by quarter-hour or by hour-of-day; default the meter
    # step accordingly and restrict the choice list. Picking 'bi' on a
    # TOU contract would make compute_breakdown route distribution
    # through the bi-horaire DSO peak/offpeak split while the supplier
    # still billed energy by TOU slot -- two billing modes that don't
    # mix. Off-peak Impact additionally requires the user to have the
    # CWaPE Tarif réseau IMPACT subscription on the DSO side.
    kind = _contract_kind(supplier_id, contract_id)
    if kind in ("dynamic", "tou", "tou_impact"):
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
    # Restrict the picker to power sensors so the user can't accidentally
    # land on a kWh / unitless / temperature sensor and have it inflate
    # the capacity bill (issue #19). Coordinator-side scaling already
    # honours W / kW / VA / kVA, but cutting the long tail at the picker
    # is the only real "this bug class can't recur" guarantee.
    peak_selector = EntitySelectorConfig(
        domain="sensor",
        device_class=["power", "apparent_power"],
    )
    if (sensor := defaults.get(CONF_CAPACITY_PEAK_SENSOR)) is not None:
        # Suggestion, not default: see _meters_schema. A `default` re-injects
        # the old entity id when the user blanks the picker.
        fields[
            vol.Optional(
                CONF_CAPACITY_PEAK_SENSOR, description={"suggested_value": sensor}
            )
        ] = EntitySelector(peak_selector)
    else:
        fields[vol.Optional(CONF_CAPACITY_PEAK_SENSOR)] = EntitySelector(peak_selector)
    fields[
        vol.Optional(
            CONF_CAPACITY_FIXED_KW,
            default=defaults.get(CONF_CAPACITY_FIXED_KW, VREG_CAPACITY_FLOOR_KW),
        )
    ] = NumberSelector(
        NumberSelectorConfig(min=0.0, max=50.0, step=0.1, mode=NumberSelectorMode.BOX)
    )
    return vol.Schema(fields)


# The six kWh entity pickers, in the order the meters step renders them.
# Shared by the schema and the step handler, which pops any the user blanked.
_METER_SENSOR_KEYS: tuple[str, ...] = (
    CONF_DAY_CONSUMPTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_CONSUMPTION_KWH,
    CONF_INJECTION_KWH,
)


def _incomplete_register_pairs(data: dict[str, Any]) -> dict[str, str]:
    """Report a day/night register pair that has only one half filled.

    A half-wired pair with nothing else covering that side is fatal:
    ``_resolve_daily_kwh`` and ``_hourly_consumption_sensors`` both give up on
    it, and ``current_year_cost`` then collapses to the fees-only floor without
    an error, a repair or any log line the user would look at. The form is the
    one place the mistake is visible, so refuse it there.

    A totals sensor rescues it, though, and the coordinator says so
    (``coordinator.py:3570``): the odd register half is ignored and the side
    bills off the total. Refusing that combination too would lock an entry
    that has always billed correctly out of its own options flow over a field
    that never affected its bill, so this mirrors the coordinator's rule
    exactly rather than tightening it.

    Keyed on the NIGHT field of each side, which is where the message renders.
    """
    errors: dict[str, str] = {}
    for day_key, night_key, total_key in (
        (CONF_DAY_CONSUMPTION_KWH, CONF_NIGHT_CONSUMPTION_KWH, CONF_CONSUMPTION_KWH),
        (CONF_DAY_INJECTION_KWH, CONF_NIGHT_INJECTION_KWH, CONF_INJECTION_KWH),
    ):
        if bool(data.get(day_key)) != bool(data.get(night_key)) and not data.get(
            total_key
        ):
            errors[night_key] = "register_pair_incomplete"
    return errors


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
    # Restrict to energy-class (cumulative kWh) sensors so the user
    # cannot land on a power / temperature / unitless sensor and have
    # the year-cost engine read its raw value as kWh.
    kwh_selector = EntitySelectorConfig(
        domain="sensor",
        device_class="energy",
    )
    fields = {}
    for conf in _METER_SENSOR_KEYS:
        stored = defaults.get(conf)
        # A stored entity id is a SUGGESTION, not a default. ha-form omits a
        # blanked selector from user_input entirely, and voluptuous then
        # re-injects a `default`, so the cleared sensor came straight back and
        # a wired meter could never be unwired. Same shape the contract-date
        # and manual-rate fields already use; the step handler pops the key.
        if stored is not None:
            fields[vol.Optional(conf, description={"suggested_value": stored})] = (
                EntitySelector(kwh_selector)
            )
        else:
            fields[vol.Optional(conf)] = EntitySelector(kwh_selector)
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
            # Match the child id exactly. utility_meter builds them as
            # f"{entry_id}_{tariff}", so a suffix test binds the wrong child
            # whenever one tariff name ends with another across an
            # underscore: with the common ["off_peak", "peak"] pair,
            # "<entry>_off_peak" also ends with "_peak", and the day slot
            # would take the off-peak register, billing night kWh at the
            # day rate.
            expected = f"{entry.entry_id}_{tariff}"
            for re_entry in registry_entries:
                if re_entry.unique_id == expected:
                    out[slot] = re_entry.entity_id
                    break
        # Two slots resolving to one entity means the match was wrong, not
        # that the helper has one register; pre-filling both with it would
        # bill the same kWh twice at two different rates.
        if "day" in out and "night" in out and out["day"] != out["night"]:
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


def _dsmr_monthly_peak_sensor(hass: HomeAssistant) -> str | None:
    """The Belgian meter's own monthly peak entity, when the user has one.

    Fluvius bills the highest quarter-hour offtake of the month, and a DSMR
    5B meter publishes exactly that on the P1 port; Home Assistant's built-in
    ``dsmr`` integration surfaces it as ``maximum_demand_current_month``. That
    entity is strictly better than anything derived from an instantaneous
    power sensor: the meter computes the true quarter-hour average, so no
    sampling of ours can miss a peak between reads or mistake a momentary
    spike for a quarter-hour one.

    Matched on the registry's ``translation_key`` rather than the entity id,
    which the user is free to rename.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if (
            entry.domain == "sensor"
            and entry.platform == "dsmr"
            and entry.translation_key == "maximum_demand_current_month"
            and not entry.disabled
        ):
            return entry.entity_id
    return None


async def _apply_energy_manager_capacity_default(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> None:
    """Pre-fill the Flemish capacity peak sensor when nothing is already set.

    Prefer the meter's own monthly peak (see ``_dsmr_monthly_peak_sensor``).
    Only when there is none do we fall back to the Energy dashboard walk
    below, which yields an instantaneous power sensor: the coordinator then
    samples it once an hour and keeps a rolling max, which is a rough
    estimate of a quarter-hour peak rather than the billed quantity.

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
    if (meter_peak := _dsmr_monthly_peak_sensor(hass)) is not None:
        defaults[CONF_CAPACITY_PEAK_SENSOR] = meter_peak
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
    if not isinstance(source_sensor, str) or not source_sensor.startswith("sensor."):
        return
    # Validate the candidate is an actual power sensor before pre-filling.
    # A Riemann source can in principle be anything numeric (a flow rate,
    # a temperature delta...); pre-filling a non-power sensor used to put
    # the user one click away from issue #19. When unsure, leave the
    # field blank so the (now device_class-filtered) picker forces a
    # deliberate choice.
    state = hass.states.get(source_sensor)
    if state is None:
        return
    device_class = state.attributes.get("device_class")
    unit = (state.attributes.get("unit_of_measurement") or "").strip()
    if device_class not in ("power", "apparent_power") and unit not in (
        "W",
        "kW",
        "VA",
        "kVA",
    ):
        return
    defaults[CONF_CAPACITY_PEAK_SENSOR] = source_sensor


def _solar_schema(defaults: dict[str, Any]) -> vol.Schema:
    # The compensation ("terugdraaiende teller" / net-metering) regime is
    # Walloon-only: that meter pays the prosumer tariff and no capacity
    # tariff, so offering it in Flanders would double-count the Flanders
    # capaciteitstarief. Outside Wallonia only "none" / "injection" apply.
    regimes = [
        r
        for r in SOLAR_REGIMES
        if r != SOLAR_REGIME_COMPENSATION
        or defaults.get(CONF_REGION) == REGION_WALLONIA
    ]
    stored = defaults.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    default_regime = stored if stored in regimes else SOLAR_REGIME_NONE
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
                default=default_regime,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=regimes,
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
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_contract_dates(user_input)
            if not errors:
                # A cleared (blanked) optional date is absent from user_input;
                # drop it so "leave blank" removes the date instead of keeping
                # the previously stored one.
                for key in (CONF_CONTRACT_START_DATE, CONF_CONTRACT_END_DATE):
                    if key not in user_input:
                        self._data.pop(key, None)
                self._data.update(user_input)
                return await self._after_contract()
        return self.async_show_form(
            step_id="contract",
            data_schema=_contract_schema(supplier, region, self._data),
            errors=errors,
        )

    def _needs_manual_rate(self) -> bool:
        """Offer the signing-rate override for a start date on a fixed /
        dynamic contract of a real (non-custom) supplier.

        Offered whether or not the supplier keeps an archive: the archive only
        ever knew the published card, so a promotional, brokered or negotiated
        rate has to be typed. What the user types wins over the archived card
        (``_manual_energy_leg``), which is the only reason it is worth asking
        an archive supplier's customer at all.
        """
        if self._data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
            return False
        if not self._data.get(CONF_CONTRACT_START_DATE):
            return False
        return _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT]) in (
            "fixed",
            "dynamic",
        )

    async def _after_contract(self) -> ConfigFlowResult:
        if self._needs_manual_rate():
            return await self.async_step_signed_rate()
        return await self.async_step_dso()

    async def async_step_signed_rate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # A cleared manual-rate field is absent from user_input; drop it so
            # blanking the override removes it (and a kind switch drops the
            # now-irrelevant coefficients).
            for key in _MANUAL_RATE_KEYS:
                if key not in user_input:
                    self._data.pop(key, None)
            self._data.update(user_input)
            return await self.async_step_dso()
        return self.async_show_form(
            step_id="signed_rate",
            data_schema=_signed_rate_schema(self._data),
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
            return await self._ask_professional()
        return self.async_show_form(
            step_id="meter",
            data_schema=_meter_schema(
                self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT], self._data
            ),
        )

    async def _ask_professional(self) -> ConfigFlowResult:
        """Only a professional contract needs the VAT treatment and the
        annual volume; a residential card answers both by construction."""
        if not _contract_is_professional(
            self._data.get(CONF_SUPPLIER), self._data.get(CONF_CONTRACT)
        ):
            # Drop settings carried over from a professional edit, so a
            # switch back to a residential contract can't leave an
            # ex-VAT preference silently in force.
            self._data.pop(CONF_INCLUDE_VAT, None)
            self._data.pop(CONF_ANNUAL_CONSUMPTION_KWH, None)
            return await self._after_meter()
        return await self.async_step_professional()

    async def async_step_professional(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_meter()
        return self.async_show_form(
            step_id="professional", data_schema=_professional_schema(self._data)
        )

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            err = await _validate_entsoe_key(self.hass, key)
            if err is None:
                user_input[CONF_API_KEY] = key
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
            # A blanked picker is absent from user_input; pop it so clearing
            # the sensor really clears it (the schema only suggests it now).
            if CONF_CAPACITY_PEAK_SENSOR not in user_input:
                self._data.pop(CONF_CAPACITY_PEAK_SENSOR, None)
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
            return await self._after_solar()
        return self.async_show_form(
            step_id="solar", data_schema=_solar_schema(self._data)
        )

    def _needs_injection_api_key(self) -> bool:
        """An ENTSO-E key is offered after the solar step when the chosen
        contract prices injection off the spot (Cociter Variable) and the
        user picked the injection regime, unless a key was already
        collected (dynamic energy)."""
        return (
            self._data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
            and not self._data.get(CONF_API_KEY)
            and _contract_has_spot_injection(
                self._data.get(CONF_SUPPLIER), self._data.get(CONF_CONTRACT)
            )
        )

    async def _after_solar(self) -> ConfigFlowResult:
        if self._needs_injection_api_key():
            return await self.async_step_injection_api_key()
        if self._is_custom():
            return await self._custom_tail()
        return await self.async_step_meters()

    async def _custom_tail(self) -> ConfigFlowResult:
        # Collect the injection formula (injection regime only), then the
        # hand-entered DSO + tax overlays, before the meter-sensor step.
        if self._data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION:
            return await self.async_step_custom_injection()
        return await self.async_step_custom_dso()

    async def async_step_injection_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optional ENTSO-E key for spot-indexed injection.

        Unlike the dynamic-energy ``api_key`` step this one is skippable:
        the energy is priced without a spot, so leaving it blank just
        leaves the injection price unavailable until a key is added via
        Reconfigure. A typed key is validated against the live endpoint.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            key = (user_input.get(CONF_API_KEY) or "").strip()
            if not key:
                self._data.pop(CONF_API_KEY, None)
                return await self.async_step_meters()
            err = await _validate_entsoe_key(self.hass, key)
            if err is None:
                self._data[CONF_API_KEY] = key
                return await self.async_step_meters()
            errors[CONF_API_KEY] = err
        return self.async_show_form(
            step_id="injection_api_key",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_API_KEY, default=self._data.get(CONF_API_KEY, "")
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
                }
            ),
            errors=errors,
        )

    async def async_step_meters(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            # Same as the capacity step: a blanked picker never reaches
            # user_input, so drop it explicitly to allow unwiring a meter.
            for key in _METER_SENSOR_KEYS:
                if key not in user_input:
                    self._data.pop(key, None)
            self._data.update(user_input)
            # A day/night pair only works as a pair. _resolve_daily_kwh and
            # _hourly_consumption_sensors both give up when one half is
            # missing, and the year cost then silently collapses to the
            # fees-only floor with no error, no repair and nothing in the log
            # a user would see. Catch it at the point the mistake is made.
            errors = _incomplete_register_pairs(self._data)
            if errors:
                defaults = dict(self._data)
                return self.async_show_form(
                    step_id="meters",
                    data_schema=_meters_schema(defaults),
                    errors=errors,
                )
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
        # Drop a mode carried over from a Walloon edit. Nothing else pops it
        # and the options flow writes self._data verbatim, so an entry moved
        # to Flanders or Brussels kept dso_tariff_mode='impact'. The network
        # side shrugs that off (the overlay has no Impact triplet outside
        # Wallonia, so network_eur_per_kwh falls through), but _routed_rate
        # still sends the ENERGY leg through dso_impact_band: 11:00-17:00
        # then bills off-peak where the region's own schedule says peak, and
        # 22:00-01:00 bills peak where it says off-peak.
        self._data.pop(CONF_DSO_TARIFF_MODE, None)
        return await self._after_dso_tariff_mode()

    async def async_step_connection_power(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_solar()
        return self.async_show_form(
            step_id="connection_power",
            data_schema=_connection_power_schema(self._data),
        )

    async def _before_solar(self) -> ConfigFlowResult:
        # Brussels connections pay a Brugel OSP fee scaled by contractual
        # connection power, so ask the tier before the solar step. Other
        # regions have no such fee and go straight to solar.
        if self._data[CONF_REGION] == REGION_BRUSSELS:
            return await self.async_step_connection_power()
        return await self.async_step_solar()

    async def _after_dso_tariff_mode(self) -> ConfigFlowResult:
        # Dynamic and spot-monthly (custom) energy both price off ENTSO-E
        # spots, so both collect the API key first.
        if _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT]) in (
            "dynamic",
            "spot_monthly",
        ):
            return await self.async_step_api_key()
        return await self._after_energy_key()

    async def _after_api_key(self) -> ConfigFlowResult:
        return await self._after_energy_key()

    def _is_custom(self) -> bool:
        return self._data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM

    async def _after_energy_key(self) -> ConfigFlowResult:
        # The expert custom supplier types its formula before the network /
        # solar steps; every other supplier already carries its rates.
        if self._is_custom():
            return await self.async_step_custom_energy()
        return await self._after_energy_collected()

    async def _after_energy_collected(self) -> ConfigFlowResult:
        if self._data[CONF_REGION] == REGION_FLANDERS:
            return await self.async_step_capacity()
        return await self._before_solar()

    async def async_step_custom_energy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._after_energy_collected()
        return self.async_show_form(
            step_id="custom_energy",
            data_schema=_custom_energy_schema(self._data),
        )

    async def async_step_custom_injection(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_custom_dso()
        return self.async_show_form(
            step_id="custom_injection",
            data_schema=_custom_injection_schema(self._data),
        )

    async def async_step_custom_dso(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_custom_tax()
        return self.async_show_form(
            step_id="custom_dso",
            data_schema=_custom_dso_schema(self._data),
        )

    async def async_step_custom_tax(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_meters()
        return self.async_show_form(
            step_id="custom_tax",
            data_schema=_custom_tax_schema(self._data),
        )

    def _finalize(self) -> ConfigFlowResult:
        raise NotImplementedError


def _unique_id_for(data: dict[str, Any]) -> str:
    """The uniqueness key an entry claims.

    ``supplier:contract:region:dso``, plus the meter for an exclusive-night
    circuit. That circuit is a whole-entry meter type (both the energy and
    the network side route the entire entry through the exclusive-night
    rate), so it has to be its own entry, which is what ``const.py`` and the
    docs tell the user to create. A household has one contract on one DSO,
    so that second entry carried the same tuple as the first and always
    aborted ``already_configured``: the documented setup could not be
    performed at all.

    Only that meter extends the key. The standard meters keep claiming the
    exact string entries were created with, so an existing entry still
    matches and a real duplicate is still caught, and two night circuits on
    one tuple still collide with each other. It does not reintroduce the
    double poll the check exists to prevent either: the snapshot, archive
    and spot caches are shared per (supplier, contract, region) across
    entries.

    Install and edit must build the key the same way, or editing a
    night-circuit entry computes the plain tuple, finds the household's main
    entry holding exactly that, and aborts.
    """
    unique = (
        f"{data[CONF_SUPPLIER]}:{data[CONF_CONTRACT]}"
        f":{data[CONF_REGION]}:{data[CONF_DSO]}"
    )
    if data.get(CONF_METER) == METER_EXCLUSIVE_NIGHT:
        return f"{unique}:{METER_EXCLUSIVE_NIGHT}"
    return unique


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
            errors = _region_mismatch_error(self._data)
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_user_schema(self._data),
                    errors=errors,
                )
            return await self.async_step_contract()
        return self.async_show_form(
            step_id="user", data_schema=_user_schema(self._data)
        )

    async def _after_meter(self) -> ConfigFlowResult:
        # Reject duplicate entries: the same (supplier, contract,
        # region, dso) tuple already running its own coordinator would
        # double-poll the supplier.
        await self.async_set_unique_id(_unique_id_for(self._data))
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
            errors = _region_mismatch_error(self._data)
            if errors:
                return self.async_show_form(
                    step_id="edit",
                    data_schema=_user_schema(self._data),
                    errors=errors,
                )
            return await self.async_step_contract()
        return self.async_show_form(
            step_id="edit", data_schema=_user_schema(self._data)
        )

    def _finalize(self) -> ConfigFlowResult:
        # Reject edits that collide with another existing entry. Two
        # coordinators on the same (supplier, contract, region, dso) tuple
        # would double-poll the supplier and break shared-snapshot dedup.
        # Built the same way as on install, or editing a night-circuit
        # entry would compute the plain tuple, find the household's main
        # entry holding exactly that, and abort.
        new_unique = _unique_id_for(self._data)
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
        # The contract picker spans both static and dynamic kinds (the
        # compare flow supports cross-kind quotes); exclude only the
        # user's current contract, and iff the picked supplier is the
        # user's current one.
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
            description_placeholders={
                "supplier": _label_for_supplier(self._compare[CONF_SUPPLIER])
            },
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
        # Dynamic, TOU and TOU-Impact contracts all require a smart
        # meter, so don't offer mono/bi for them -- matching the install
        # flow's _meter_schema, which gates the same three kinds. (Mega
        # Off-peak Impact is "tou_impact"; omitting it here let the
        # compare flow show an impossible mono/bi meter for it.)
        if other_kind in ("dynamic", "tou", "tou_impact"):
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
        """Hand off to compare_result, prompting for an ENTSO-E key first
        when either side needs spot data the user's current entry doesn't
        already carry: a dynamic target, or (on the injection regime) a
        spot-indexed-injection contract on EITHER side -- the target like
        Cociter Variable, or the user's own keyless Cociter Variable entry
        -- whose feed-in credit is priced off the hourly day-ahead. Keep
        this symmetric with the compare_spot_injection check in
        _build_compare_placeholders, which values both sides."""
        current = self.config_entry.data
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        needs_spot = other_kind == "dynamic" or (
            current.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
            and (
                _contract_has_spot_injection(
                    self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
                )
                or _contract_has_spot_injection(
                    current[CONF_SUPPLIER], current[CONF_CONTRACT]
                )
            )
        )
        if needs_spot and not current.get(CONF_API_KEY):
            return await self.async_step_compare_api_key()
        return await self.async_step_compare_result()

    async def async_step_compare_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Compare against a dynamic (or spot-indexed-injection) target
        needs an ENTSO-E key for the spot rate. Borrow the user's existing
        key when their entry already has one (handled in
        _after_compare_meter); otherwise prompt and validate against the
        live endpoint before reaching the result page."""
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            err = await _validate_entsoe_key(self.hass, key)
            if err is None:
                self._compare[CONF_API_KEY] = key
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
        # mid-reload (UNDEFINED sentinel) or never finished setup. We
        # still need to populate every placeholder the result template
        # references; otherwise HA renders the missing ones as raw
        # ``{token}`` text.
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
                "ytd_injection_kwh": "-",
                "solar_note": "",
                "meter_used": str(
                    self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
                ),
                "consumption_source": "default (entry reloading)",
                "annual_chart": "",
                "ytd_chart": "",
                "error": "current entry is reloading; try again in a moment",
            }

        region = current[CONF_REGION]
        dso = current[CONF_DSO]
        # Comparison may override the meter type for static contracts;
        # falls back to the current entry's setting.
        # The comparison may override the meter for the TARGET only (a
        # dynamic/TOU target forces METER_DYNAMIC). The user's current side
        # must keep its real meter, else a mono user's current bill gets
        # quoted at bi-horaire / dynamic rates and biases the decision.
        meter = self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
        current_meter = current.get(CONF_METER, METER_MONO)
        dso_mode = current.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        # The quantity the capacity tariff is charged on, not this month's
        # reading: _billed_peak_kw applies the regulated floor per month and
        # means the rolling twelve, so the comparison quotes the same kW the
        # live sensor bills. Flooring _peak_kw here instead would quote a
        # seasonal household its winter peak against the year.
        peak_kw = coord._billed_peak_kw()
        regime = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)

        now_utc = dt_util.utcnow()
        today_local = dt_util.now().date()
        jan1 = today_local.replace(month=1, day=1)
        year_ago = today_local - timedelta(days=365)
        # Inclusive of today: leap years -> 366. Compute via
        # (Jan 1 next year - Jan 1 this year) so today=Feb 29 doesn't
        # raise (year+1 has no Feb 29).
        days_in_year = (date(today_local.year + 1, 1, 1) - jan1).days
        days_elapsed = (today_local - jan1).days + 1
        fee_proration = days_elapsed / days_in_year
        # The prosumer fee is billed per-month (each month's fee prorated by
        # its own days) in the live sensor and backfill, not by the uniform
        # days_in_year fraction, so mirror that: every completed month counts
        # as 1 plus the elapsed fraction of the current month.
        first_of_month = today_local.replace(day=1)
        next_month = date(
            today_local.year + today_local.month // 12,
            today_local.month % 12 + 1,
            1,
        )
        prosumer_proration = (today_local.month - 1) + today_local.day / (
            next_month - first_of_month
        ).days
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
        # A spot-indexed-injection side (Cociter Variable) prices its
        # feed-in credit off the hourly day-ahead even though its energy
        # kind is "variable", so it needs spots just like a dynamic side.
        compare_spot_injection = regime == SOLAR_REGIME_INJECTION and (
            _contract_has_spot_injection(current[CONF_SUPPLIER], current[CONF_CONTRACT])
            or _contract_has_spot_injection(
                self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
            )
        )
        need_spot = "dynamic" in (current_kind, other_kind) or compare_spot_injection
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
        # For the ANNUAL estimate a dynamic contract's all-in is
        # factor*spot + base, linear in spot, so the time-averaged yearly
        # bill equals the breakdown at the MEAN spot over the fetched
        # day-ahead window. Use that rather than an instantaneous spot so
        # the estimate doesn't reflect whichever minute the dialog opened
        # (Belgian day-ahead swings from negative to >0.30 EUR/kWh intraday).
        avg_spot = sum(spot_dict.values()) / len(spot_dict) if spot_dict else None

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

        # Price the user's CURRENT side off the leg the live sensors bill, not
        # the raw card. A fixed / dynamic contract with a signing start date is
        # billed at the rate it locked in, which _cohort_energy_leg resolves
        # and the coordinator splices on every tick. Reading coord._snapshot
        # here compared the alternative against today's published card instead,
        # so the quoted delta was wrong for exactly the users the start-date
        # feature exists for. _cohort_energy_leg returns None for a contract
        # that is not the entry's own, so it can never touch the other side.
        current_snapshot = coord._snapshot
        if current_snapshot is not None:
            from .coordinator import _cohort_energy_leg

            cohort = await _cohort_energy_leg(
                self.hass,
                async_get_clientsession(self.hass),
                get_extractor(current[CONF_SUPPLIER]),
                current[CONF_CONTRACT],
                region,
                self.config_entry,
                current_snapshot,
            )
            if cohort is not None:
                current_snapshot = replace(current_snapshot, energy=cohort)

        current_per_kwh: float | None = None
        if current_snapshot is not None:
            current_per_kwh = _tou_weighted_per_kwh(
                current_snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                avg_spot,
                current_meter,
                dso_mode,
            )

        # Other supplier: fetch + compute.
        session = async_get_clientsession(self.hass)
        other_extractor = get_extractor(self._compare[CONF_SUPPLIER])
        other_per_kwh: float | None = None
        other_snap = None
        # Resolve the quote against this entry's site facts through the same
        # helper the coordinator uses, not apply_vat alone. Both transforms are
        # per-entry, and skipping the excise band priced a professional quote
        # at the card's first tier however much the household actually uses:
        # 1,421 c€/kWh instead of 1,139 at 60 000 kWh/yr, overstating the
        # alternative by about 169 EUR/yr. The user's own side comes off the
        # coordinator and IS resolved, so the comparison was biased.
        from .coordinator import _resolve_snapshot

        try:
            other_snap = _resolve_snapshot(
                self.config_entry,
                await other_extractor.fetch(
                    session, self._compare[CONF_CONTRACT], region
                ),
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
                    avg_spot,
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
        current_inj_price: float | None = None
        compare_inj_price: float | None = None
        if regime == "injection":
            if current_snapshot is not None:
                current_inj_price = _compare_injection_credit(
                    current_snapshot, self.config_entry, spot_dict, avg_spot
                )
            if other_snap is not None:
                compare_inj_price = _compare_injection_credit(
                    other_snap, self.config_entry, spot_dict, avg_spot
                )

        if current_per_kwh is not None:
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = (
                f"{_annual_bill(current_snapshot, self.config_entry, peak_kw, current_per_kwh, annual_kwh, rolling_inj_kwh, current_inj_price, meter=current_meter):.2f}"
            )
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, self.config_entry, peak_kw, other_per_kwh, annual_kwh, rolling_inj_kwh, compare_inj_price, meter=meter):.2f}"
            )
        if (
            current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and current_snapshot is not None
        ):
            delta = _annual_bill(
                other_snap,
                self.config_entry,
                peak_kw,
                other_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                compare_inj_price,
                meter=meter,
            ) - _annual_bill(
                current_snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                meter=current_meter,
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
        # Exclude dynamic sides from the archive engine: it bills each
        # past hour at factor*spot+base and needs the historical spot
        # cache, which _compute_current_year_cost only receives on the
        # live coordinator path -- called without it here it returns the
        # fees-only floor (zero energy), so a fixed-vs-dynamic compare
        # would show the dynamic side missing its entire energy bill.
        # The simple per-kwh model below prices both sides off the same
        # current per-kwh rate and proration, so the delta stays honest.
        archive_capable = (
            current_extractor.fetch_for_month is not None
            and other_extractor.fetch_for_month is not None
            and current_kind != "dynamic"
            and other_kind != "dynamic"
        )
        if archive_capable and other_snap is not None and current_snapshot is not None:
            # Replay the coordinator's historical spot cache so a
            # spot-indexed injection (Cociter Variable) gets the same
            # per-hour feed-in credit the live YTD applies; spots are the
            # Belgian day-ahead, supplier-independent, so the same cache
            # prices both sides. A no-op for monthly-indicative contracts.
            hist_spots = coord._historical_spots
            if compare_spot_injection and not hist_spots:
                # The user's own entry isn't spot-needing, so the live
                # coordinator never backfilled its cache. Fetch into a
                # LOCAL dict for this throwaway quote with the key typed in
                # compare_api_key (or the entry's own); without it the
                # credit silently drops and the YTD overstates the
                # spot-indexed target's cost. Save/restore the coordinator
                # cache so a read-only comparison doesn't mutate (and have
                # the next tick persist) live coordinator state.
                borrowed = self._compare.get(CONF_API_KEY) or current.get(CONF_API_KEY)
                if borrowed:
                    saved = coord._historical_spots
                    coord._historical_spots = {}
                    try:
                        await coord._ensure_historical_spots(
                            jan1, today_local, borrowed
                        )
                        hist_spots = coord._historical_spots
                    finally:
                        coord._historical_spots = saved
            try:
                current_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    current_extractor,
                    # Already cohort-spliced, and _compute_current_year_cost
                    # re-resolves the cohort itself from the same entry, so
                    # this is idempotent; the DSO and tax overlays are the
                    # raw card's either way.
                    current_snapshot,
                    self.config_entry,
                    historical_spots=hist_spots,
                    billed_peak_kw=peak_kw,
                )
                compare_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    other_extractor,
                    other_snap,
                    self.config_entry,
                    contract_override=self._compare[CONF_CONTRACT],
                    meter_override=meter,
                    historical_spots=hist_spots,
                    billed_peak_kw=peak_kw,
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
            and current_snapshot is not None
        ):
            # The YTD what-if mirrors the live current_year_cost sensor and
            # the archive YTD path, both of which bill the Flanders capacity
            # tariff as a separate sensor, so exclude it here to keep the
            # three figures consistent (the full annual estimate above keeps
            # it).
            current_ytd = _annual_bill(
                current_snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                current_inj_price,
                fee_proration=fee_proration,
                prosumer_proration=prosumer_proration,
                meter=current_meter,
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
                prosumer_proration=prosumer_proration,
                meter=meter,
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


def _tou_slot_weights(weekend_rule: str) -> tuple[float, float, float]:
    """Hours-per-week each CWaPE TOU slot (peak, transition, offpeak) is
    active, from the published rules and a 5-weekday / 2-weekend split.

    Engie Empower Flextime keeps the weekday transition/offpeak windows on
    weekends (``weekend_no_peak``); Luminus SmartFlex makes weekends fully
    off-peak (``weekend_offpeak``, the default).
    """
    if weekend_rule == "weekend_no_peak":
        return 45.0, 69.0, 54.0
    return 45.0, 45.0, 78.0


def _compare_injection_credit(
    snapshot: Any,
    entry: Any,
    spot_dict: dict[datetime, float],
    avg_spot: float | None,
) -> float | None:
    """Injection credit (EUR/kWh) for the compare flow's annual estimate.

    A per-slot TOU injection (Engie Empower Flextime) is time-averaged over
    the published slot durations, mirroring how the consumption side is
    weighted in ``_tou_weighted_per_kwh``; delegating to the live helper
    would return the dialog-open slot rate and bias the credit. A
    spot-indexed injection (Cociter Variable, or any dynamic-energy
    contract) is priced off the window MEAN spot, consistent with the
    energy term (which also uses ``avg_spot``); pricing it off the live
    current slot would make the solar credit and the energy cost reflect
    different instants. Monthly-indexed injection is spot-independent (uses
    the realized monthly value), so delegate that to the live helper.
    """
    from .coordinator import _compute_injection_price, _floor_injection
    from .providers.base import DynamicRates, TimeOfUseRates

    inj = getattr(snapshot, "injection", None)
    energy = getattr(snapshot, "energy", None)
    if (
        inj is not None
        and isinstance(energy, TimeOfUseRates)
        and inj.peak is not None
        and inj.transition is not None
        and inj.offpeak is not None
    ):
        wp, wt, wo = _tou_slot_weights(energy.weekend_rule)
        return float(
            (inj.peak * wp + inj.transition * wt + inj.offpeak * wo) / (wp + wt + wo)
        )
    if (
        inj is not None
        and inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    ):
        # Floor the formula result like the live and historical paths so the
        # compare estimate doesn't count a negative feed-in as extra cost when
        # the contract clamps injection at zero.
        return (
            _floor_injection(inj.factor * avg_spot + inj.base, inj)
            if avg_spot is not None
            else None
        )
    return _compute_injection_price(snapshot, entry, spot_dict)


def _period_avg_all_in(
    snapshot: Any,
    dso: str,
    region: str,
    start: datetime,
    num_days: int,
    spot: float | None,
    meter: Any,
    dso_mode: Any,
) -> float | None:
    """Mean all-in EUR/kWh over ``num_days`` from ``start`` under uniform
    hourly consumption.

    Sampling every hour lets each hour carry its true energy slot AND network
    band, so the TOU energy windows and the bi-horaire network bands - which
    don't align, and both differ on weekends - are each weighted correctly.
    A three-sample-per-slot weighting instead assigns one network band to a
    whole energy slot and mis-prices it. Returns None on any compute failure.
    """
    from .pricing import compute_breakdown

    total = 0.0
    count = 0
    for hour in range(num_days * 24):
        try:
            bd = compute_breakdown(
                snapshot,
                dso,
                region,
                start + timedelta(hours=hour),
                spot,
                meter,
                dso_mode,
            )
        except Exception:  # noqa: BLE001
            return None
        total += bd.all_in
        count += 1
    return total / count if count else None


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

    For Fixed / Variable the breakdown is spot-independent. For Dynamic
    the breakdown is linear in ``spot``, so the caller passes the MEAN
    spot over the fetched day window (not the instantaneous one) to get a
    time-averaged annual figure. For TOU contracts (Luminus SmartFlex, Engie
    Empower Flextime) and Impact contracts (Mega Off-peak Impact)
    ``compute_breakdown`` returns one of three slot rates depending on
    the hour the user opens the dialog -- biased. Compute breakdowns
    at three representative weekday hours (one per slot) and weight by
    the published slot durations across a week, so the annual estimate
    isn't dragged toward whichever slot the user happens to be in.

    Returns ``None`` on compute failure so the caller can render '-'
    on the result page rather than tear the flow down.
    """
    from .pricing import compute_breakdown, is_belgian_holiday, is_offpeak
    from .providers.base import ImpactRates, TimeOfUseRates

    try:
        bd = compute_breakdown(snapshot, dso, region, when_now, spot, meter, dso_mode)
    except Exception:  # noqa: BLE001
        return None
    # The all-in is time-of-day dependent not only for TOU/Impact energy
    # but also when the meter routes a bi-horaire peak/offpeak split
    # (Fixed/Variable on a bi-hourly or dynamic meter) or when the DSO
    # tariff mode is Impact (network varies by CWaPE band). Returning the
    # single dialog-open-time rate for those biased the annual estimate by
    # whichever slot the user happened to be in.
    overlay = snapshot.dsos.get(dso)
    bi_split = meter in ("bi", "dynamic") and (
        (
            getattr(snapshot.energy, "peak", None) is not None
            and getattr(snapshot.energy, "offpeak", None) is not None
        )
        or (
            overlay is not None
            and getattr(overlay, "distribution_peak", None) is not None
            and getattr(overlay, "distribution_offpeak", None) is not None
        )
    )
    impact_network = dso_mode == "impact"
    if (
        not isinstance(snapshot.energy, (TimeOfUseRates, ImpactRates))
        and not bi_split
        and not impact_network
    ):
        return bd.all_in
    # Pick a recent non-holiday weekday so each slot lookup hits the
    # weekday rule. Walk back from today's local date.
    weekday = when_now.date()
    for _ in range(8):
        if not is_belgian_holiday(weekday) and weekday.weekday() < 5:
            break
        weekday -= timedelta(days=1)
    base = datetime.combine(weekday, time(), tzinfo=when_now.tzinfo)
    if isinstance(snapshot.energy, ImpactRates) or impact_network:
        # CWaPE Impact bands (every day, no weekend exception):
        #   pic    17-22                (35h/week)
        #   medium 07-11 + 22-01        (49h/week)
        #   eco    01-07 + 11-17        (84h/week)
        try:
            bd_pic = compute_breakdown(
                snapshot, dso, region, base.replace(hour=19), spot, meter, dso_mode
            )
            bd_med = compute_breakdown(
                snapshot, dso, region, base.replace(hour=9), spot, meter, dso_mode
            )
            bd_eco = compute_breakdown(
                snapshot, dso, region, base.replace(hour=3), spot, meter, dso_mode
            )
        except Exception:  # noqa: BLE001
            return bd.all_in
        wp, wm, we = 35.0, 49.0, 84.0
        total = wp + wm + we
        return (bd_pic.all_in * wp + bd_med.all_in * wm + bd_eco.all_in * we) / total
    if not isinstance(snapshot.energy, TimeOfUseRates):
        # Fixed/Variable on a bi-hourly/dynamic meter: weight the peak and
        # off-peak all-in by the region's bi-horaire hour split (uniform
        # consumption across a representative week, region-aware via
        # is_offpeak so the Wallonia 11-17 off-peak window and the Brussels
        # holiday rule are honoured). Any peak/off-peak hour is a valid
        # sample since the rate is constant within each band.
        peak_when: datetime | None = None
        off_when: datetime | None = None
        peak_hours = 0
        for day_offset in range(7):
            for hour in range(24):
                when = base + timedelta(days=day_offset, hours=hour)
                if is_offpeak(when, region):
                    off_when = off_when or when
                else:
                    peak_hours += 1
                    peak_when = peak_when or when
        if peak_when is None or off_when is None:
            return bd.all_in
        try:
            bd_peak = compute_breakdown(
                snapshot, dso, region, peak_when, spot, meter, dso_mode
            )
            bd_off = compute_breakdown(
                snapshot, dso, region, off_when, spot, meter, dso_mode
            )
        except Exception:  # noqa: BLE001
            return bd.all_in
        return (
            bd_peak.all_in * peak_hours + bd_off.all_in * (168 - peak_hours)
        ) / 168.0

    # Weekday holidays bill under the weekend rule, so a single week that
    # happens to contain one would skew the slot mix. Walk back to a
    # holiday-free Mon-Sun week (matches the prior clean-week assumption).
    def _holiday_free_week(anchor: date) -> date:
        mon = anchor - timedelta(days=anchor.weekday())
        for _ in range(12):
            if not any(is_belgian_holiday(mon + timedelta(days=d)) for d in range(7)):
                return mon
            mon -= timedelta(days=7)
        return mon

    if snapshot.energy.weekend_rule == "smartflex_seasonal":
        # SmartFlex bills seasonal bands, so blend a summer and a winter
        # representative WEEK by season length (21/03-20/09 = 184 days, the
        # rest 181). A full week captures both the seasonal energy bands and
        # any weekday/weekend network split.
        acc = 0.0
        wsum = 0.0
        for probe, days in (
            (date(when_now.year, 7, 1), 184.0),
            (date(when_now.year, 1, 15), 181.0),
        ):
            season_monday = datetime.combine(
                _holiday_free_week(probe), time(), tzinfo=when_now.tzinfo
            )
            avg = _period_avg_all_in(
                snapshot, dso, region, season_monday, 7, spot, meter, dso_mode
            )
            if avg is None:
                return bd.all_in
            acc += avg * days
            wsum += days
        return acc / wsum
    # A TOU energy slot spans hours with different bi-horaire network bands
    # (and the weekend rule shifts hours between energy slots), so weighting
    # one sample per slot mis-prices the network. Average a full
    # representative week (Mon-Sun) so each hour carries its true energy slot
    # and network band.
    week_start = datetime.combine(
        _holiday_free_week(when_now.date()), time(), tzinfo=when_now.tzinfo
    )
    week_avg = _period_avg_all_in(
        snapshot, dso, region, week_start, 7, spot, meter, dso_mode
    )
    return week_avg if week_avg is not None else bd.all_in


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
    prosumer_proration: float | None = None,
    meter: Any = METER_MONO,
    include_capacity: bool = True,
) -> float:
    """Estimated EUR bill for ``snapshot`` over the period that produced
    ``consumption_kwh`` and ``injection_kwh``.

    ``fee_proration`` scales the EUR/year fee components (1.0 for a
    full year, ``days_elapsed/days_in_year`` for YTD). ``prosumer_proration``,
    when given, overrides that for the prosumer term only: the live sensor and
    backfill prorate the prosumer fee per-month (each month's fee by its own
    days), so the YTD what-if passes the same per-month factor there to keep
    its absolute figure equal to the live ``current_year_cost`` sensor.
    ``include_capacity`` is forwarded to :func:`_annual_fees`; it exists for
    callers that want the per-kWh and fee terms without the Flanders capacity
    charge, and both the annual estimate and the YTD what-if keep it on so
    they match the live ``current_year_cost`` sensor.

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
    fees = (
        _annual_fees(snapshot, entry, peak_kw, meter, include_capacity) * fee_proration
    )
    if prosumer_proration is not None:
        # _annual_fees prorated the prosumer term uniformly by fee_proration;
        # swap in the per-month proration the live sensor and backfill use so
        # the YTD absolute matches. The delta is zero for a non-compensation
        # entry (prosumer fee is 0 there).
        #
        # ``prosumer_proration`` counts MONTHS (0..12) while ``fee_proration``
        # is a fraction of a year (0..1), so it has to be divided by 12 before
        # the two can be subtracted. Without that the correction multiplied an
        # already-annual fee by a month count and quoted the prosumer term 12x
        # over on every date. No test caught it because the options-flow stub
        # DSO publishes no prosumer rate, which zeroes the whole term.
        from .coordinator import _compute_prosumer

        prosumer_annual = 12.0 * _compute_prosumer(snapshot, entry)
        fees += prosumer_annual * (prosumer_proration / 12.0 - fee_proration)
    regime = entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    if regime == "compensation":
        billable = max(consumption_kwh - injection_kwh, 0.0)
        return fees + per_kwh * billable
    if regime == "injection" and injection_price is not None:
        return fees + per_kwh * consumption_kwh - injection_price * injection_kwh
    return fees + per_kwh * consumption_kwh


def _annual_fees(
    snapshot: Any,
    entry: ConfigEntry,
    peak_kw: float,
    meter: Any,
    include_capacity: bool = True,
) -> float:
    """Just the EUR/year fee components (no per-kWh term).

    Pulled out so the YTD comparison can pro-rate fees by the elapsed
    fraction of the year without re-computing the per-kWh part. ``meter``
    selects the supplier yearly fixed fee, so an exclusive-night meter
    gets its dedicated fee (EBEM) rather than the standard one.

    ``include_capacity`` can exclude the Flanders capacity tariff. It is on
    everywhere today: the live ``current_year_cost`` sensor accrues capacity
    through ``_ytd_capacity``, so a what-if that dropped it would quote a
    lower bill than the sensor it sits next to."""
    from .coordinator import (
        _annual_static_fees,
        _compute_capacity,
        _compute_prosumer,
    )

    static = _annual_static_fees(snapshot, meter, entry)
    capacity = 0.0
    if include_capacity and entry.data.get(CONF_REGION) == REGION_FLANDERS:
        capacity = 12.0 * _compute_capacity(snapshot, entry, peak_kw)
    prosumer = 12.0 * _compute_prosumer(snapshot, entry)
    return static + capacity + prosumer


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
    from .coordinator import _kwh_sensor_ids, _recorder_daily_kwh

    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
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
