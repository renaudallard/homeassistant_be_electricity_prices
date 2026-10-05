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
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_API_KEY,
    CONF_CONNECTION_KVA_TIER,
    CONF_CONTRACT,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE,
    CONF_CUSTOM_DSO_TRANSPORT,
    CONF_CUSTOM_ENERGY_BASE,
    CONF_CUSTOM_ENERGY_FACTOR,
    CONF_CUSTOM_ENERGY_SINGLE,
    CONF_CUSTOM_INJECTION_BASE,
    CONF_CUSTOM_INJECTION_CURRENT,
    CONF_CUSTOM_INJECTION_FACTOR,
    CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION,
    CONF_CUSTOM_TAX_FEDERAL_EXCISE,
    CONF_CUSTOM_TAX_REGION_CONNECTION_FEE,
    CONF_CUSTOM_TAX_REGIONAL_RENEWABLES,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INCLUDE_VAT,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_DIRECT_DEBIT,
    CONF_MANUAL_YEARLY_FEE,
    CONF_METER,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SUPPLIER,
    CONNECTION_KVA_TIERS,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DEFAULT_DIRECT_DEBIT,
    DEFAULT_CONNECTION_KVA_TIER,
    DEFAULT_INCLUDE_VAT,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    DSO_TARIFF_MODES,
    METER_DYNAMIC,
    METER_MONO,
    METER_TYPES,
    REGIONS,
    SMART_METER_CONTRACT_KINDS,
    SPOT_PRICED_CONTRACT_KINDS,
)
from .flow_contracts import (
    _contract_kind,
    _contracts_for,
    _region_dso_options,
    _region_dso_slugs,
    _supplier_options,
)
from .flow_switch import _add_contract_date_fields


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
    current = defaults.get(CONF_CONTRACT)
    contracts = _contracts_for(supplier_id, region, keep=current)
    options = [SelectOptionDict(value=c.id, label=c.label) for c in contracts]
    valid_ids = {c.id for c in contracts}
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
    selector = _custom_num(negative=negative, key=key)
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
    cannot take another meter, Mega Off-peak Impact and the OCTA+ Impact cards,
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
    TimeSeries counts as a rejection, not as unreachable, unless it reads
    "No matching data": parse_day_ahead_xml raises EntsoeAuthError for
    the others, so they land on ``"invalid_api_key"`` and keep the user
    on the form. A no-data answer is EntsoeNoDataError, an EntsoeError,
    so it lands on ``"cannot_connect"``: ENTSO-E answered without saying
    anything about the key. A 24h window anchored on yesterday keeps that
    rare, since the BE bidding zone does not go a full local day with no
    publication.

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


# The largest a hand-entered per-kWh figure can be, in EUR/kWh, or for a
# formula's factor the multiplier itself. Each sits well above any Belgian
# figure, crisis prices included, and well below the same figure typed in
# c€/kWh, which is what the box would otherwise take and bill a hundred times
# over. The small levies matter most, because typed in cents they still look
# like a plausible price: a Walloon connection fee of 0,075 c€/kWh typed as
# 0.075 added 260 EUR a year at 3500 kWh.
_PER_KWH_BOUND: dict[str, float] = {
    CONF_CUSTOM_ENERGY_SINGLE: 2.0,
    CONF_CUSTOM_ENERGY_PEAK: 2.0,
    CONF_CUSTOM_ENERGY_OFFPEAK: 2.0,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT: 2.0,
    CONF_MANUAL_ENERGY_SINGLE: 2.0,
    CONF_MANUAL_ENERGY_PEAK: 2.0,
    CONF_MANUAL_ENERGY_OFFPEAK: 2.0,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT: 2.0,
    CONF_CUSTOM_ENERGY_FACTOR: 10.0,
    CONF_CUSTOM_INJECTION_FACTOR: 10.0,
    CONF_MANUAL_ENERGY_FACTOR: 10.0,
    CONF_CUSTOM_ENERGY_BASE: 0.5,
    CONF_CUSTOM_INJECTION_BASE: 0.5,
    CONF_MANUAL_ENERGY_BASE: 0.5,
    CONF_CUSTOM_INJECTION_CURRENT: 1.0,
    CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM: 0.5,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO: 0.5,
    CONF_CUSTOM_DSO_TRANSPORT: 0.1,
    CONF_CUSTOM_TAX_FEDERAL_EXCISE: 0.1,
    CONF_CUSTOM_TAX_REGIONAL_RENEWABLES: 0.1,
    CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION: 0.01,
    CONF_CUSTOM_TAX_REGION_CONNECTION_FEE: 0.01,
}


def _custom_num(*, negative: bool = False, key: str = "") -> NumberSelector:
    """Number selector for a hand-entered EUR/kWh rate or coefficient.

    ``negative=True`` for values a Belgian formula can legitimately drive
    below zero (an injection factor/base, a spot multiplier/offset); the
    rest are floored at 0. ``key`` bounds the figure by ``_PER_KWH_BOUND``,
    on both sides when it may be negative.
    """
    bound = _PER_KWH_BOUND.get(key)
    config = NumberSelectorConfig(step="any", mode=NumberSelectorMode.BOX)
    if not negative:
        config["min"] = 0.0
    elif bound is not None:
        config["min"] = -bound
    if bound is not None:
        config["max"] = bound
    return NumberSelector(config)
