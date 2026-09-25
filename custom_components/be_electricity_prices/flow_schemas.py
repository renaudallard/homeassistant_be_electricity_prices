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

from collections.abc import Mapping
from datetime import date, timedelta
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
    CONF_PREVIOUS_CONTRACTS,
    CONF_SWITCH_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_CARD_ARCHIVE,
    CONF_DAILY_COMPARE,
    CONF_EV_HOME_CHARGING_RATE,
    CONF_INCLUDE_VAT,
    CONF_INJECTION_KWH,
    METER_SENSOR_KEYS,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_DIRECT_DEBIT,
    CONF_MANUAL_YEARLY_FEE,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CONF_TARIFF_CARD_DATE,
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    CONNECTION_KVA_TIERS,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_DIRECT_DEBIT,
    DEFAULT_CONNECTION_KVA_TIER,
    DEFAULT_CARD_ARCHIVE,
    DEFAULT_DAILY_COMPARE,
    DEFAULT_EV_HOME_CHARGING_RATE,
    DEFAULT_INCLUDE_VAT,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    DSO_TARIFF_MODES,
    METER_DYNAMIC,
    METER_MONO,
    METER_TYPES,
    REGIONS,
    REGION_WALLONIA,
    SMART_METER_CONTRACT_KINDS,
    SOLAR_REGIMES,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_NONE,
    SPOT_PRICED_CONTRACT_KINDS,
    VREG_CAPACITY_FLOOR_KW,
)
from .flow_contracts import (
    _contract_kind,
    _contracts_for,
    _region_dso_options,
    _region_dso_slugs,
    _supplier_options,
)


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
    """Append the optional contract start / tariff card / end date pickers.

    Pre-filled with the stored value as a *suggestion* (not a default) on the
    options / reconfigure pass, so blanking the picker truly omits the key from
    ``user_input``: the step handler then pops it, which is how a date is
    cleared. A ``default`` would re-inject the stored value on a blank submit,
    making the date unclearable.
    """
    date_selector = DateSelector()
    for key in (
        CONF_CONTRACT_START_DATE,
        CONF_TARIFF_CARD_DATE,
        CONF_CONTRACT_END_DATE,
    ):
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
    """Reject a future start or card date, or an end date not after the start.

    All three fields are independently optional: an end date without a start
    date is fine (a bare renewal reminder), so the ordering check only fires
    when both are present.

    The card date is checked only against today. It is NOT required to fall on
    or before the start date, which looks like the obvious guard and is wrong:
    a renewal re-signs a supply that began years ago onto this month's card, so
    a card date after the start date is as ordinary as one before it.
    """
    from .cohort import _parse_iso_date

    errors: dict[str, str] = {}
    start = _parse_iso_date(user_input.get(CONF_CONTRACT_START_DATE))
    card = _parse_iso_date(user_input.get(CONF_TARIFF_CARD_DATE))
    end = _parse_iso_date(user_input.get(CONF_CONTRACT_END_DATE))
    today = dt_util.now().date()
    if start is not None and start > today:
        errors[CONF_CONTRACT_START_DATE] = "start_date_in_future"
    if card is not None and card > today:
        errors[CONF_TARIFF_CARD_DATE] = "card_date_in_future"
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


def _switch_schema(today: date) -> vol.Schema:
    """The one question a supplier switch asks: the new contract's first day."""
    return vol.Schema(
        {vol.Required(CONF_SWITCH_DATE, default=today.isoformat()): DateSelector()}
    )


def _validate_switch_date(
    data: Mapping[str, Any], user_input: dict[str, Any]
) -> dict[str, str]:
    """Refuse a switch date that prices nothing, or that overlaps a recorded one.

    It has to fall after 1 January, so the contract being left has at least one
    day this year, and not after today, since the day is when the new contract
    started supplying, not when it will. And after the last switch recorded,
    because each contract ends where the next begins, and after the start date
    of the contract being left, which cannot end before it began.
    """
    from .cohort import _parse_iso_date
    from .contract_periods import recorded_contracts

    until = _parse_iso_date(user_input.get(CONF_SWITCH_DATE))
    today = dt_util.now().date()
    if until is None or until > today or until <= date(today.year, 1, 1):
        return {CONF_SWITCH_DATE: "switch_date_outside_year"}
    records = recorded_contracts(data)
    if records and until <= records[-1][0]:
        return {CONF_SWITCH_DATE: "switch_date_before_last"}
    # The contract being left cannot end before it began.
    started = _parse_iso_date(data.get(CONF_CONTRACT_START_DATE))
    if started is not None and until <= started:
        return {CONF_SWITCH_DATE: "switch_date_before_start"}
    return {}


def _record_switch(data: Mapping[str, Any], until: date) -> dict[str, Any]:
    """The entry's settings with its current contract kept as the one held until
    the day before ``until``, ready for the new contract to be picked.

    The settings are kept whole, so the old contract is priced on exactly what
    was configured for it. A switch from an earlier year prices nothing this
    year and is dropped. The start date moves to the switch day, since the
    contract now being set up is the new one, and the answers that belonged to
    the old contract go: its card month, a typed signing rate and its end date,
    none of which describe the new one. So does the box that bills the year
    from the contract start, which would leave the old contract out of the year.
    """
    from .contract_periods import recorded_contracts

    held = {key: value for key, value in data.items() if key != CONF_PREVIOUS_CONTRACTS}
    kept = [
        {"until": when.isoformat(), "data": dict(settings)}
        for when, settings in recorded_contracts(data)
        if when > date(until.year, 1, 1)
    ]
    out = {
        **data,
        CONF_PREVIOUS_CONTRACTS: [*kept, {"until": until.isoformat(), "data": held}],
        CONF_CONTRACT_START_DATE: until.isoformat(),
    }
    for key in (
        CONF_TARIFF_CARD_DATE,
        CONF_CONTRACT_END_DATE,
        CONF_YTD_FROM_CONTRACT_START,
        *_MANUAL_RATE_KEYS,
    ):
        out.pop(key, None)
    return out


# The custom-supplier rate boxes whose ABSENCE is meaningful: ``_routed_rate``
# and ``_network_rate`` fall back to the single rate when these are None, so a
# stored 0.0 is a different answer, not an empty box. Their steps have to pop a
# blanked one exactly like the signing-rate step does, or the value can be set
# but never cleared, and 0.11.40/0.11.41 briefly shipped these boxes with a
# 0.0 default, so entries edited in that window hold a billed zero with no
# route out of it.
#
# One tuple per step. Each step pops its own boxes, shown or not (a box a meter
# or mode change hid is stale), and never the other step's: popping every
# absent key made the network step throw away the energy split the step
# before had just stored.
_CUSTOM_ENERGY_FALLBACK_KEYS: tuple[str, ...] = (
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
)
_CUSTOM_DSO_FALLBACK_KEYS: tuple[str, ...] = (
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


def _drop_blanked(
    data: dict[str, Any], user_input: dict[str, Any], keys: tuple[str, ...]
) -> None:
    """Remove any of the submitting step's fallback ``keys`` the user cleared.

    ha-form omits a blanked selector from ``user_input`` entirely, so a bare
    ``data.update(user_input)`` leaves the stored number in place and the
    re-shown form pre-fills it again as a suggestion.
    """
    for key in keys:
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
        defaults.get(CONF_SUPPLIER, ""),
        defaults.get(CONF_CONTRACT, ""),
        quarter_hourly=bool(defaults.get(CONF_QUARTER_HOURLY, False)),
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
    """Wallonia-only step: which DSO-side billing mode applies?

    Every mode is offered whatever the meter step answered, and a mono meter
    beside Impact is not the contradiction it looks like: the two describe
    different sides of the bill, the meter being the SUPPLIER's register
    configuration and the mode the DSO's. TotalEnergies Impact is exactly that
    pair, and is the reason the pre-selection below exists: the product
    registers as ``variable``, so ``_meter_schema`` offers all four meters and
    defaults to mono, while its card prints only the CWaPE bands.

    Filtering Impact out for a mono meter was tried and reverted. It removed
    the mode from the one product the pre-selection is for, landing a TE
    Impact entry on bi_horaire, which that card costs EUR 113 a year at
    3500 kWh, the figure the comment above measures. The products that truly
    cannot take another meter, Mega Off-peak Impact and OCTA+ Fixed Impact,
    register as ``tou_impact`` and ``_meter_schema`` already offers them the
    dynamic meter alone.
    """
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


def _meter_schema(
    supplier_id: str, contract_id: str, defaults: dict[str, Any]
) -> vol.Schema:
    # Dynamic, TOU, and TOU Impact contracts all require a smart (SMR3)
    # meter to bill by quarter-hour or by hour-of-day; default the meter
    # step accordingly and restrict the choice list. Picking 'bi' on a
    # TOU contract would make compute_breakdown route distribution
    # through the bi-horaire DSO peak/offpeak split while the supplier
    # still billed energy by TOU slot: two billing modes that don't
    # mix. Off-peak Impact additionally requires the user to have the
    # CWaPE Tarif réseau IMPACT subscription on the DSO side.
    #
    # Read through the settlement answer, not off the registry: a Bolt
    # variable card settled per quarter-hour IS one of those contracts, and
    # offering it a mono meter would bill a 15-minute energy leg against the
    # bi-horaire distribution split. That is why the settlement step runs
    # before this one.
    kind = _contract_kind(
        supplier_id,
        contract_id,
        quarter_hourly=bool(defaults.get(CONF_QUARTER_HOURLY, False)),
    )
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


def _settlement_schema(defaults: dict[str, Any]) -> vol.Schema:
    """The one box of the settlement step.

    Its own step, directly after the contract, rather than a field on one of
    the steps around it. It cannot sit on the CONTRACT step, whose schema is
    built before the user has picked the contract the question depends on, and
    it cannot sit on the METER step, because on Bolt the answer decides what
    that step may offer: a quarter-hourly settlement requires an SMR3 meter,
    so the option list is narrowed by the answer. It also has to be asked
    before the signing-rate step, which offers a coefficient pair for a
    dynamic settlement and per-meter rates for a monthly one.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_QUARTER_HOURLY,
                default=bool(defaults.get(CONF_QUARTER_HOURLY, False)),
            ): BooleanSelector()
        }
    )


def _direct_debit_schema(defaults: dict[str, Any]) -> vol.Schema:
    """The one box of the direct-debit step.

    Its own step for the reason the settlement box has one: the contract
    step's schema is built before the contract it depends on has been picked.
    It sits after the meter step, where nothing downstream reads it, because
    unlike the settlement answer it narrows no later option: it moves one
    yearly figure and nothing else.
    """
    return vol.Schema(
        {
            vol.Optional(
                CONF_DIRECT_DEBIT,
                default=bool(defaults.get(CONF_DIRECT_DEBIT, DEFAULT_DIRECT_DEBIT)),
            ): BooleanSelector()
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


def _injection_api_key_schema(defaults: dict[str, Any]) -> vol.Schema:
    """The optional twin of :func:`_api_key_schema`, for the injection-only
    key step, which may be left blank to skip. The re-check path re-shows
    whichever key step is pending and has to keep this one optional: shown
    as required, the documented "leave blank to skip" exit disappeared.

    The stored key is a suggestion, not a default: a default is re-injected
    on a blank submit, so a stored key could never be removed here."""
    current = defaults.get(CONF_API_KEY, "")
    return vol.Schema(
        {
            vol.Optional(
                CONF_API_KEY, description={"suggested_value": current}
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
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
# Shared by the schema and the step handler, which pops any the user blanked,
# and with energy_meters, which memoises reads keyed on the same six.
_METER_SENSOR_KEYS: tuple[str, ...] = METER_SENSOR_KEYS


def _incomplete_register_pairs(data: dict[str, Any]) -> dict[str, str]:
    """Report a day/night register pair that has only one half filled.

    A half-wired pair with nothing else covering that side is fatal:
    ``_resolve_daily_kwh`` and ``_hourly_consumption_sensors`` both give up on
    it, and ``current_year_cost`` then collapses to the fees-only floor without
    an error, a repair or any log line the user would look at. The form is the
    one place the mistake is visible, so refuse it there.

    A totals sensor rescues it, though, and the reader says so
    (``energy_meters._resolve_daily_kwh``): the odd register half is ignored and the side
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
    # And the one box that is about where past cards come from rather than
    # about a meter: on by default, and the only way to keep the integration
    # from contacting GitHub for a month the supplier no longer serves.
    fields[
        vol.Optional(
            CONF_CARD_ARCHIVE,
            default=bool(defaults.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)),
        )
    ] = BooleanSelector()
    # Off by default: only a household reimbursed for charging a company car
    # at home has a use for the CREG rate, and ticking it is what lets the
    # entry contact creg.be at all.
    fields[
        vol.Optional(
            CONF_EV_HOME_CHARGING_RATE,
            default=bool(
                defaults.get(CONF_EV_HOME_CHARGING_RATE, DEFAULT_EV_HOME_CHARGING_RATE)
            ),
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
