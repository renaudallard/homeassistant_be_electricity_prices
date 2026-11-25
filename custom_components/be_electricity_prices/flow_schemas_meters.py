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

"""Schema builders for the capacity, energy-meter and solar steps.

The Flemish peak source, the six kWh meter pickers and the half-pair check
behind them, and the solar regime, for both the setup flow and the
one-off quote. Split out of ``flow_schemas.py`` as ``flow_schemas_custom.py``
was; ``config_flow.py`` and the compare flow keep the step handlers.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CAPACITY_MODE_FIXED,
    CAPACITY_MODE_SENSOR,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CARD_ARCHIVE,
    CONF_CONSUMPTION_KWH,
    CONF_DAILY_COMPARE,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DOUBLE_FLOW_METER,
    CONF_ENECO_BILL,
    CONF_EV_HOME_CHARGING_RATE,
    CONF_INJECTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    DEFAULT_CARD_ARCHIVE,
    DEFAULT_DAILY_COMPARE,
    DEFAULT_EV_HOME_CHARGING_RATE,
    ENECO_INTEGRATION,
    METER_SENSOR_KEYS,
    REGION_WALLONIA,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_NONE,
    SOLAR_REGIMES,
)
from .flow_decimal import DecimalSelector, decimal_text
from .fluvius import capacity_floor_kw


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
            default=decimal_text(
                defaults.get(CONF_CAPACITY_FIXED_KW, capacity_floor_kw())
            ),
        )
    ] = DecimalSelector(0.0, 50.0)
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
    (``meter_daily._resolve_daily_kwh``): the odd register half is ignored and the side
    bills off the total. Refusing that combination too would lock an entry
    that has always billed correctly out of its own options flow over a field
    that never affected its bill, so this mirrors the coordinator's rule
    exactly rather than tightening it.

    The injection side is read only with a solar regime
    (``energy_meters._bills_injection``), so without one a half-wired
    injection pair, wired for the Energy dashboard, never reaches the bill and
    is not refused either.

    Keyed on the NIGHT field of each side, which is where the message renders.
    """
    errors: dict[str, str] = {}
    sides = [
        (CONF_DAY_CONSUMPTION_KWH, CONF_NIGHT_CONSUMPTION_KWH, CONF_CONSUMPTION_KWH)
    ]
    if data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE) != SOLAR_REGIME_NONE:
        sides.append(
            (CONF_DAY_INJECTION_KWH, CONF_NIGHT_INJECTION_KWH, CONF_INJECTION_KWH)
        )
    for day_key, night_key, total_key in sides:
        if bool(data.get(day_key)) != bool(data.get(night_key)) and not data.get(
            total_key
        ):
            errors[night_key] = "register_pair_incomplete"
    return errors


def _reused_meter_sensors(data: dict[str, Any]) -> dict[str, str]:
    """Report a kWh sensor picked in more than one of the six fields.

    No wiring needs one entity twice, and every reader takes the fields at
    their word: the same sensor as day and night register is a healthy pair
    reporting the same days, so each day is billed twice, and the same sensor
    on both sides credits a feed-in equal to the consumption. Keyed on the
    later field, in the order the form renders them.
    """
    errors: dict[str, str] = {}
    seen: set[str] = set()
    for key in _METER_SENSOR_KEYS:
        entity_id = data.get(key)
        if not entity_id:
            continue
        if entity_id in seen:
            errors[key] = "meter_sensor_reused"
        seen.add(entity_id)
    return errors


def _meters_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Cumulative-kWh sensors for the current_year_cost computation.

    Two ways to feed the sensor, both optional:

      * Direct day/night registers off the meter (4 fields). Used as-is
        when populated.
      * Single cumulative totals (2 fields). On a bi-hourly or dynamic
        meter each day's total is split into day and night by binning the
        recorder's hourly statistics on the entry's day_start
        (meter_daily._recorder_daily_band_ratio); a day those statistics
        cannot split takes the region's schedule share. Nothing is
        persisted: the split is read back from the recorder every time.

    When both are filled, the day/night registers win while they report
    whole, since they are the meter's own split rather than one rebuilt
    from hourly statistics.
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


def _bill_schema(bill: Any) -> vol.Schema:
    """The picker of the Eneco bill an Eneco household holds against these
    prices, as the eneco_be integration reads it. A suggestion, so it can be
    cleared."""
    key = (
        vol.Optional(CONF_ENECO_BILL, description={"suggested_value": bill})
        if bill
        else vol.Optional(CONF_ENECO_BILL)
    )
    return vol.Schema(
        {
            key: EntitySelector(
                EntitySelectorConfig(domain="sensor", integration=ENECO_INTEGRATION)
            )
        }
    )


def _meter_energy(hass: HomeAssistant, entity: er.RegistryEntry) -> str | None:
    """The energy of the Eneco meter an eneco_be sensor belongs to, as its
    subentry names it ("Electricity" or "Gas"), or None where it cannot be
    told: the sensor's entry or subentry is gone, or the key is missing."""
    entry = (
        hass.config_entries.async_get_entry(entity.config_entry_id)
        if entity.config_entry_id
        else None
    )
    subentry = (
        entry.subentries.get(entity.config_subentry_id)
        if entry is not None and entity.config_subentry_id
        else None
    )
    energy = None if subentry is None else subentry.data.get("energy_type")
    return energy if isinstance(energy, str) else None


def _not_a_bill(hass: HomeAssistant, data: dict[str, Any]) -> dict[str, str]:
    """An error on the bill picker unless it names a last bill sensor of the
    eneco_be integration: any other of its sensors carries no bill lines.
    One of a gas meter is refused too, whose lines no electricity rate
    prices."""
    entity_id = data.get(CONF_ENECO_BILL)
    if not entity_id:
        return {}
    entity = er.async_get(hass).async_get(entity_id)
    if (
        entity is None
        or entity.platform != ENECO_INTEGRATION
        or not entity.unique_id.endswith("_last_bill")
    ):
        return {CONF_ENECO_BILL: "eneco_bill_not_a_bill"}
    if _meter_energy(hass, entity) == "Gas":
        return {CONF_ENECO_BILL: "eneco_bill_other_energy"}
    return {}


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
    fields: dict[Any, Any] = {
        vol.Optional(
            CONF_SOLAR_KVA,
            default=decimal_text(defaults.get(CONF_SOLAR_KVA, 0.0)),
        ): DecimalSelector(0.0, 50.0),
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
    # Only where the compensation regime is on offer, the one it bills under
    # (fees.bills_gross_network).
    if SOLAR_REGIME_COMPENSATION in regimes:
        fields[
            vol.Optional(
                CONF_DOUBLE_FLOW_METER,
                default=bool(defaults.get(CONF_DOUBLE_FLOW_METER, False)),
            )
        ] = BooleanSelector()
    return vol.Schema(fields)


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
