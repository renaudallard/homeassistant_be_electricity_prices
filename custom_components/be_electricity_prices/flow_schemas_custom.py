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

"""The forms behind the custom supplier.

The escape hatch for a contract this integration has no extractor for: the
household types the card in itself, leg by leg, and these are the four forms
it does that through. Every field is optional and blank means "not on my
card", so the schemas carry the defaults rather than the steps.
"""

from __future__ import annotations

from .const import CONF_CONTRACT
from .const import CONF_CUSTOM_DSO_BRUSSELS_OSP
from .const import CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR
from .const import CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_ECO
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_PEAK
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_PIC
from .const import CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE
from .const import CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR
from .const import CONF_CUSTOM_DSO_TRANSPORT
from .const import CONF_CUSTOM_ENERGY_BASE
from .const import CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT
from .const import CONF_CUSTOM_ENERGY_FACTOR
from .const import CONF_CUSTOM_ENERGY_OFFPEAK
from .const import CONF_CUSTOM_ENERGY_PEAK
from .const import CONF_CUSTOM_ENERGY_QUARTER_HOURLY
from .const import CONF_CUSTOM_ENERGY_SINGLE
from .const import CONF_CUSTOM_INJECTION_BASE
from .const import CONF_CUSTOM_INJECTION_CURRENT
from .const import CONF_CUSTOM_INJECTION_FACTOR
from .const import CONF_CUSTOM_INJECTION_FLOOR
from .const import CONF_CUSTOM_INJECTION_MODE
from .const import CONF_CUSTOM_INJECTION_SPP_WEIGHTED
from .const import CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION
from .const import CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH
from .const import CONF_CUSTOM_TAX_FEDERAL_EXCISE
from .const import CONF_CUSTOM_TAX_REGIONAL_RENEWABLES
from .const import CONF_CUSTOM_TAX_REGION_CONNECTION_FEE
from .const import CONF_CUSTOM_VAT_RATE
from .const import CONF_CUSTOM_YEARLY_FIXED_FEE
from .const import CONF_DSO_TARIFF_MODE
from .const import CONF_METER
from .const import CONF_REGION
from .const import CUSTOM_CONTRACT_DYNAMIC
from .const import CUSTOM_CONTRACT_FIXED
from .const import CUSTOM_CONTRACT_MONTHLY
from .const import CUSTOM_INJECTION_MODES
from .const import CUSTOM_INJECTION_MODE_CURRENT
from .const import DEFAULT_CUSTOM_VAT_RATE
from .const import DSO_MODE_IMPACT
from .const import METER_BI
from .const import METER_DYNAMIC
from .const import METER_EXCLUSIVE_NIGHT
from .const import METER_MONO
from .const import REGION_BRUSSELS
from .const import REGION_FLANDERS
from .const import REGION_WALLONIA
from homeassistant.helpers.selector import BooleanSelector
from homeassistant.helpers.selector import NumberSelector
from homeassistant.helpers.selector import NumberSelectorConfig
from homeassistant.helpers.selector import NumberSelectorMode
from homeassistant.helpers.selector import SelectSelector
from homeassistant.helpers.selector import SelectSelectorConfig
from homeassistant.helpers.selector import SelectSelectorMode
from typing import Any
import voluptuous as vol
from .flow_schemas import _add_manual_num
from .flow_schemas import _custom_num


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
        # Same rule as the DSO step and as the ``bi_capable`` test in
        # ``pricing.energy_eur_per_kwh``: a dynamic (SMR3) meter registers the day/night
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
    component (injection stays exempt).

    The connection-fee box is Walloon only. The only Belgian levy of that
    shape is the redevance de raccordement, and the pricing engine adds
    ``region_connection_fee`` for Wallonia alone, so on a Flemish or Brussels
    entry the box was stored and never priced: a Flanders customer put the
    WKK levy in it (which belongs in the renewables box, with GSC) and saw 0
    and 100 behave the same. Same region gate the DSO step already applies
    to the prosumer and capacity boxes.
    """
    fields: dict[Any, Any] = {}
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_FEDERAL_EXCISE)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION)
    _add_custom_num(fields, defaults, CONF_CUSTOM_TAX_REGIONAL_RENEWABLES)
    if defaults.get(CONF_REGION) == REGION_WALLONIA:
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
