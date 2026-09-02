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

"""Voluptuous schema builders and validators for the config / options flow.

Split out of ``config_flow.py``. Every function here turns the current
``_data`` dict into one step's form, or validates what came back from it; none
of them touch flow state. ``config_flow.py`` keeps the step handlers that call
them.

Two conventions carry real weight here and are documented at their definitions:
a rate the pricing engine FALLS BACK for must be offered as a *suggestion*
rather than a default (a default is submitted verbatim and bills a zero), and a
blanked box has to be popped from the entry or the stored value survives.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
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

from .api import EntsoeAuthError, EntsoeClient, EntsoeError
from .providers import all_extractors
from .providers.base import Contract, ExtractorError
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
    CONF_YTD_FROM_CONTRACT_START,
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
    CONF_CUSTOM_TAX_REGIONAL_RENEWABLES,
    CONF_CUSTOM_TAX_REGION_CONNECTION_FEE,
    CONF_CUSTOM_VAT_RATE,
    CONF_CUSTOM_YEARLY_FIXED_FEE,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_DAILY_COMPARE,
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
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    CONNECTION_KVA_TIERS,
    CUSTOM_CONTRACT_DYNAMIC,
    CUSTOM_CONTRACT_FIXED,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_INJECTION_MODES,
    CUSTOM_INJECTION_MODE_CURRENT,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_CONNECTION_KVA_TIER,
    DEFAULT_CUSTOM_VAT_RATE,
    DEFAULT_DAILY_COMPARE,
    DEFAULT_INCLUDE_VAT,
    DSO_CHOICES,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    DSO_TARIFF_MODES,
    KIND_GROUP,
    METER_BI,
    METER_DYNAMIC,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    METER_TYPES,
    REGIONS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SMART_METER_CONTRACT_KINDS,
    SOLAR_REGIMES,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_NONE,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
    VREG_CAPACITY_FLOOR_KW,
)
from .providers import get as get_extractor


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


def _sweep_candidates(
    region: str, group: str, professional: bool, own_contract: str
) -> list[tuple[str, Contract]]:
    """Every contract the ranking page may quote for this household.

    The five conditions, and where each already existed for the 1:1 page:

    * region, per CONTRACT and never ``SupplierExtractor.regions()``, which is
      only the union across a supplier's products;
    * not the expert custom supplier, which has no fetchable card and can only
      ever be the current side of a quote;
    * not a supplier on its way out of the market, since quoting a household
      into a contract about to be transferred away is never useful;
    * the same professional segment, for the reason
      ``_compare_contract_schema`` gives at length;
    * the same kind group, which is the one condition the 1:1 page does NOT
      apply -- see ``KIND_GROUP``.

    ``own_contract`` is dropped because a ranking is a list of alternatives.
    That is the opposite of the 1:1 page, which keeps it on purpose so a
    household can ask what its own contract would cost on another meter, and
    the two are not in tension: the ranking's own row is printed from the
    baseline the household is already being quoted against, not fetched again
    as a candidate.

    Returns ``(supplier_id, Contract)`` pairs, because a contract does not
    carry its supplier and every caller needs both.
    """
    out: list[tuple[str, Contract]] = []
    for ext in all_extractors():
        if ext.id == SUPPLIER_CUSTOM or ext.deprecated_until is not None:
            continue
        for c in ext.contracts:
            if region not in c.regions:
                continue
            if c.professional != professional:
                continue
            # Subscripted, not .get(): KIND_GROUP is total over TariffKind and
            # a KeyError here is a new kind nobody grouped, which must fail
            # loudly in CI rather than quietly drop every contract of it.
            if KIND_GROUP[c.kind] != group:
                continue
            if c.id == own_contract:
                continue
            out.append((ext.id, c))
    return out


def _contract_group(supplier_id: str, contract_id: str) -> str:
    """The household's own kind group, or '' when it cannot be resolved.

    ``_contract_kind`` returns '' for an entry whose stored contract has left
    the catalogue, deliberately, so the meter step can still render. That
    empty string has no group, and the ranking page cannot be built for it:
    answer '' here too and let the caller say so, rather than raising out of a
    registry lookup or inventing a group the household is not on.

    The stale SUPPLIER is a second case ``_contract_kind`` does not cover -- it
    resolves the extractor first, and that raises for an id this build no
    longer ships. Caught here rather than there, because widening
    ``_contract_kind`` would change what every other caller sees for an entry
    whose supplier is gone, and this is the only caller that needs an answer
    rather than an exception.
    """
    try:
        kind = _contract_kind(supplier_id, contract_id)
    except ExtractorError:
        return ""
    return KIND_GROUP.get(kind, "")


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
    # Sits with the dates because it is meaningless without a start date, and
    # this is the only step that collects one. A plain default (rather than a
    # suggested_value) is right here: an unticked box DOES reach user_input as
    # False, so there is nothing to clear and nothing to re-inject.
    fields[
        vol.Optional(
            CONF_YTD_FROM_CONTRACT_START,
            default=bool(defaults.get(CONF_YTD_FROM_CONTRACT_START, False)),
        )
    ] = BooleanSelector()


def _validate_contract_dates(user_input: dict[str, Any]) -> dict[str, str]:
    """Reject a future start date or an end date not after the start.

    Both fields are independently optional: an end date without a start date is
    fine (a bare renewal reminder), so the ordering check only fires when both
    are present.
    """
    from .cohort import _parse_iso_date

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

# The custom-supplier rate boxes whose ABSENCE is meaningful: ``_routed_rate``
# and ``_network_rate`` fall back to the single rate when these are None, so a
# stored 0.0 is a different answer, not an empty box. Their steps have to pop a
# blanked one exactly like the signing-rate step does, or the value can be set
# but never cleared -- and 0.11.40/0.11.41 briefly shipped these boxes with a
# 0.0 default, so entries edited in that window hold a billed zero with no
# route out of it.
_CUSTOM_FALLBACK_KEYS: tuple[str, ...] = (
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
    # The CWaPE Impact triplet belongs here for the same reason and was the
    # one group left out. network_eur_per_kwh takes the Impact branch when all
    # three are non-None, so a defaulted 0,00 does not fall back to the single
    # rate: it BILLS zero distribution in every band, every hour. A Walloon
    # Impact entry that filled in only distribution_single lost 0,1198 EUR/kWh
    # of network, EUR 419/yr at 3500 kWh, on the live tick, the year-to-date
    # walk, the backfill and the compare quote at once, with no Repairs card
    # because _sync_impact_gap_issue tests for None and the zero defeats it.
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
)


def _drop_blanked(data: dict[str, Any], user_input: dict[str, Any]) -> None:
    """Remove any fallback key the user cleared from the form.

    ha-form omits a blanked selector from ``user_input`` entirely, so a bare
    ``data.update(user_input)`` leaves the stored number in place and the
    re-shown form pre-fills it again as a suggestion.
    """
    for key in _CUSTOM_FALLBACK_KEYS:
        if key not in user_input:
            data.pop(key, None)


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
    # Both spot-priced kinds sign a coefficient pair, not a rate: dynamic
    # resolves it per slot, spot-monthly against the delivery month's mean.
    if kind in SPOT_PRICED_CONTRACT_KINDS:
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


# Contracts whose card prints ONLY the CWaPE incitative bands for supplier
# energy, so the incitative network configuration is the overwhelmingly likely
# answer, but whose card does not actually SAY the product implies it.
#
# TotalEnergies Impact is the case. Mega and OCTA+ register their Impact
# products as tou_impact and are auto-selected on that; TE registers its as
# "variable", so the gate never fired and the user was offered bi_horaire
# pre-selected. Accepting that costs a 3500 kWh ORES household about EUR 29/yr
# on a bi meter and EUR 113 on a mono one, partly because the incitative
# configuration also exempts the Walloon terme fixe.
#
# Pre-selected rather than forced: unlike the Mega and OCTA+ cards, the TE one
# states only that a communicating digital meter is required, so a holder on
# the standard configuration exists and hard-forcing would under-bill them by
# the same amount in the other direction.
_IMPACT_DEFAULT_CONTRACTS: frozenset[str] = frozenset({"totalenergies_impact"})


def _dso_tariff_mode_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Wallonia-only step: which DSO-side billing mode applies?"""
    current = defaults.get(CONF_DSO_TARIFF_MODE)
    if not current and defaults.get(CONF_CONTRACT) in _IMPACT_DEFAULT_CONTRACTS:
        current = DSO_MODE_IMPACT
    current = current or DSO_MODE_BI_HORAIRE
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
        # Literally what _add_manual_num does, and for the same reason, so
        # call it rather than keep a second copy of the suggestion-not-default
        # idiom: violating that idiom is what shipped a billed 0,00 in
        # 0.11.40/0.11.41.
        _add_manual_num(fields, defaults, key, negative=negative)
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
            # Same fallback class as the peak / off-peak pair above and as its
            # own DSO counterpart: ``_routed_rate`` bills the single rate when
            # ``exclusive_night`` is None, so a 0.0 injected into an untouched
            # box is a DIFFERENT answer, not an absent one. This box was the
            # one left behind when the other five were fixed, and it is the
            # worst of them: an exclusive-night meter routes the whole entry
            # through this single rate, so the energy leg went to zero for
            # every hour, not just some.
            _add_custom_num(
                fields, defaults, CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT, fallback=True
            )
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
            # fallback=True: leaving these blank must mean "I am not on the
            # incitative bands", which falls back to the single rate. A
            # default would submit 0,00 and bill no distribution at all.
            _add_custom_num(
                fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_PIC, fallback=True
            )
            _add_custom_num(
                fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM, fallback=True
            )
            _add_custom_num(
                fields, defaults, CONF_CUSTOM_DSO_DISTRIBUTION_ECO, fallback=True
            )
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
    if kind in SMART_METER_CONTRACT_KINDS:
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
    rejects the token, and ``"cannot_connect"`` for transport / parse
    errors and for a document that parses but covers none of the
    window.

    An HTTP 200 carrying an Acknowledgement_MarketDocument with no
    TimeSeries counts as a rejection, not as unreachable:
    parse_day_ahead_xml raises EntsoeAuthError for that root element,
    so it lands on ``"invalid_api_key"`` and keeps the user on the
    form. Use a 24h window anchored on yesterday, which is what makes
    that safe: a quota-exhausted token returns exactly that empty
    Acknowledgement, and the BE bidding zone rarely (never, in
    practice) goes a full local day with no publication, so an empty
    24h response really does mean the token is not usable - whether
    quota or maintenance, better than letting the user finalise an
    entry that fails on first refresh.

    A blank key never gets this far. The step that requires one
    rejects an empty field itself, and the two that treat it as
    optional skip without calling, so an empty string here would be a
    caller's bug rather than an answer ENTSO-E gave.
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
    fields: dict[Any, Any] = {}
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
    # The last box on the last step, because it is the only one here that is
    # not about wiring a meter: turn it on and the entry ranks every contract
    # of its kind once a day and publishes the saving as a sensor.
    fields[
        vol.Optional(
            CONF_DAILY_COMPARE,
            default=bool(defaults.get(CONF_DAILY_COMPARE, DEFAULT_DAILY_COMPARE)),
        )
    ] = BooleanSelector()
    return vol.Schema(fields)


def _regime_options(region: Any) -> list[str]:
    """Solar regimes that can apply in ``region``.

    The compensation ("terugdraaiende teller" / net-metering) regime is
    Walloon-only: that meter pays the prosumer tariff and no capacity
    tariff, so offering it in Flanders would double-count the Flanders
    capaciteitstarief. Outside Wallonia only "none" / "injection" apply.

    Shared with the compare flow's what-if picker, which has to narrow the
    same way: a Flemish entry quoted on the compensation regime would net
    injection 1:1 against consumption while still paying the capacity
    tariff and no prosumer fee, a bill no Belgian contract can issue.
    """
    return [
        r
        for r in SOLAR_REGIMES
        if r != SOLAR_REGIME_COMPENSATION or region == REGION_WALLONIA
    ]


def _solar_schema(defaults: dict[str, Any]) -> vol.Schema:
    regimes = _regime_options(defaults.get(CONF_REGION))
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


def _compare_solar_schema(defaults: dict[str, Any], *, ask_volumes: bool) -> vol.Schema:
    """What-if solar picker for the compare branch.

    Same regime list as the install step, narrowed the same way, but
    nothing here is written back: it only re-prices the quote.

    Deliberately no inverter-kVA field. The kVA only reaches the bill
    through the Walloon prosumer fee, which only the compensation regime
    pays, so it could only matter for a what-if INTO compensation, and
    that regime is closed to installations certified after 2024: anyone
    eligible is already on it and has a kVA set. An entry that somehow
    reaches it without one is told so on the result page instead.

    The two volume fields appear only when the entry has no injection
    sensor to read. A compensation meter may net injection against
    consumption in a single register, and that reading is not what the
    injection tariff bills, so those users type the two gross yearly
    figures instead of having a netted one silently re-used.
    """
    regimes = _regime_options(defaults.get(CONF_REGION))
    stored = defaults.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_SOLAR_REGIME,
            default=stored if stored in regimes else SOLAR_REGIME_NONE,
        ): SelectSelector(
            SelectSelectorConfig(
                options=regimes,
                mode=SelectSelectorMode.LIST,
                translation_key="solar_regime",
            )
        ),
    }
    if ask_volumes:
        for key in (CONF_WHATIF_CONSUMPTION_KWH, CONF_WHATIF_INJECTION_KWH):
            selector = NumberSelector(
                NumberSelectorConfig(
                    min=0.0, max=200000.0, step=1.0, mode=NumberSelectorMode.BOX
                )
            )
            typed = defaults.get(key)
            # A figure already typed is a SUGGESTION, not a default: a
            # voluptuous default is re-injected on a blank submit, and the
            # "both volumes or none" check could then never fire. Same
            # shape the manual-rate and meter fields use. Without it, the
            # half a user did fill in is wiped by the error re-show.
            if typed is None:
                fields[vol.Optional(key)] = selector
            else:
                fields[vol.Optional(key, description={"suggested_value": typed})] = (
                    selector
                )
    return vol.Schema(fields)
