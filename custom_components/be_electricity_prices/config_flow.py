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


from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .flow_schemas import (
    _METER_SENSOR_KEYS,
    _api_key_schema,
    _capacity_schema,
    _connection_power_schema,
    _contract_has_spot_injection,
    _contract_is_professional,
    _contract_kind,
    _contract_schema,
    _contracts_for,
    _custom_dso_schema,
    _custom_energy_schema,
    _custom_injection_schema,
    _custom_tax_schema,
    _drop_blanked,
    _dso_schema,
    _dso_tariff_mode_schema,
    _incomplete_register_pairs,
    _meter_schema,
    _meters_schema,
    _professional_schema,
    _region_mismatch_error,
    _signed_rate_schema,
    _solar_schema,
    _user_schema,
    _validate_contract_dates,
    _validate_entsoe_key,
    _MANUAL_RATE_KEYS,
)
from .compare_flow import _SweepStepsMixin
from .flow_prefill import (
    _apply_energy_manager_capacity_default,
    _apply_energy_manager_defaults,
)
from .const import (
    CONF_ANNUAL_CONSUMPTION_KWH,
    CONF_API_KEY,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONTRACT,
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    DSO_MODE_IMPACT,
    CONF_INCLUDE_VAT,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    SOLAR_REGIME_INJECTION,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
    DOMAIN,
    METER_EXCLUSIVE_NIGHT,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from .providers import get as get_extractor


# ---- shared schema builders ---------------------------------------------------


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
    # An API key ENTSO-E could not confirm because it was unreachable, held
    # only for the length of the menu that offers to re-check it. Bare
    # annotations: a valued class attribute would be shared across flows, and
    # a key typed in one setup must never reappear in the next.
    _pending_key: str
    _pending_key_step: str
    # The translation key of this flow's entry step. It stays per-flow because
    # the two are separate strings: config.step.user and options.step.edit.
    _entry_step_id = "user"

    if TYPE_CHECKING:
        hass: HomeAssistant

        def async_show_form(self, **kwargs: Any) -> ConfigFlowResult: ...
        def async_abort(self, **kwargs: Any) -> ConfigFlowResult: ...
        def async_show_menu(self, **kwargs: Any) -> ConfigFlowResult: ...

    def _seed_data(self) -> dict[str, Any]:
        """What ``_data`` starts as. Install starts empty; the OptionsFlow
        starts from the stored entry."""
        return {}

    async def _async_entry_step(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The supplier / region entry step, shared by both flows.

        The two spelled out the same handler, differing only in the seed and
        the step id -- which is exactly what the mixin's own docstring says
        distinguishes them.
        """
        if not hasattr(self, "_data"):
            self._data = self._seed_data()
        if user_input is not None:
            self._data.update(user_input)
            errors = _region_mismatch_error(self._data)
            if errors:
                return self.async_show_form(
                    step_id=self._entry_step_id,
                    data_schema=_user_schema(self._data),
                    errors=errors,
                )
            return await self.async_step_contract()
        return self.async_show_form(
            step_id=self._entry_step_id, data_schema=_user_schema(self._data)
        )

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
            # A spot-monthly card is a coefficient pair like a dynamic one, so
            # a negotiated factor / base is just as typeable - and just as
            # invisible to the integration otherwise.
            "spot_monthly",
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

    async def _offer_unverified_key(self, key: str, came_from: str) -> ConfigFlowResult:
        """Stash a key ENTSO-E could not confirm and offer a way forward.

        A rejected key and an unreachable ENTSO-E are different answers and
        only the first is the user's problem. Blocking setup on the second
        makes a platform outage look like a bad key, and ENTSO-E was down for
        over a day at the end of August 2026 with nobody able to add a
        contract in the meantime (discussion #77).

        Deliberately a menu rather than a silent pass. The user is choosing to
        finish setup on a key nothing has verified, so the choice is put in
        front of them with the re-check offered first; if the key really is
        bad, the coordinator's own auth Repairs card says so on the first
        refresh.
        """
        self._pending_key = key
        self._pending_key_step = came_from
        return await self.async_step_api_key_unreachable()

    async def async_step_api_key_unreachable(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="api_key_unreachable",
            menu_options=["api_key_recheck", "api_key_unverified"],
        )

    async def async_step_api_key_recheck(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu
    ) -> ConfigFlowResult:
        """Ask ENTSO-E again, for a user who would rather wait than proceed."""
        err = await _validate_entsoe_key(self.hass, self._pending_key)
        if err is None:
            return await self._accept_pending_key()
        if err == "cannot_connect":
            return await self.async_step_api_key_unreachable()
        # Still reachable and the key was refused after all: that IS the
        # user's problem, so put them back on the form with the real error
        # rather than leaving them a "continue anyway" they should not take.
        return self.async_show_form(
            step_id=self._pending_key_step,
            data_schema=_api_key_schema(self._data),
            errors={CONF_API_KEY: err},
        )

    async def async_step_api_key_unverified(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002 - menu
    ) -> ConfigFlowResult:
        """Accept the key unverified and carry on with setup."""
        return await self._accept_pending_key()

    async def _accept_pending_key(self) -> ConfigFlowResult:
        self._data[CONF_API_KEY] = self._pending_key
        if self._pending_key_step == "injection_api_key":
            return await self.async_step_meters()
        return await self._after_api_key()

    async def async_step_api_key(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            key = user_input[CONF_API_KEY].strip()
            if not key:
                # A blank field must not reach the validator. While ENTSO-E is
                # unreachable every answer is "cannot_connect", so the empty
                # string would be offered the unverified branch and stored, and
                # an empty key is the one value nothing recovers from:
                # _fetch_spot_prices raises "missing ENTSO-E API key" before
                # fetch_day_ahead_or_fallback runs, so the keyless
                # energy-charts source is never even tried. A typed-but-wrong
                # key at least prices the entry off the fallback until ENTSO-E
                # is back to reject it.
                errors[CONF_API_KEY] = "empty_api_key"
            else:
                err = await _validate_entsoe_key(self.hass, key)
                if err is None:
                    user_input[CONF_API_KEY] = key
                    self._data.update(user_input)
                    return await self._after_api_key()
                if err == "cannot_connect":
                    return await self._offer_unverified_key(key, "api_key")
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
            if err == "cannot_connect":
                return await self._offer_unverified_key(key, "injection_api_key")
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
            if (
                _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT])
                == "tou_impact"
            ):
                # Not a question for these products. An Impact card bands its
                # ENERGY on the CWaPE incitative schedule, which exists only
                # under that configuration, so offering the standard mode
                # pre-selected let a user bill the two legs off different
                # tariff structures: the energy routed by Impact band while
                # the network took the standard jour/nuit columns.
                self._data[CONF_DSO_TARIFF_MODE] = DSO_MODE_IMPACT
                return await self._after_dso_tariff_mode()
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
        # Dynamic and spot-monthly energy both price off ENTSO-E spots, so
        # both collect the API key first.
        if (
            _contract_kind(self._data[CONF_SUPPLIER], self._data[CONF_CONTRACT])
            in SPOT_PRICED_CONTRACT_KINDS
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
            _drop_blanked(self._data, user_input)
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
            _drop_blanked(self._data, user_input)
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
        return await self._async_entry_step(user_input)

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


class BePricesOptionsFlow(_WizardStepsMixin, _SweepStepsMixin, OptionsFlow):
    """Walk every config step pre-filled, save back to entry.data.

    Two top-level paths from the init menu: edit the existing entry
    (the original options flow) or run a one-off comparison quote
    against a different supplier (no save, no extra entry).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="init",
            menu_options=["edit", "compare", "compare_all"],
        )

    _entry_step_id = "edit"

    def _seed_data(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        return await self._async_entry_step(user_input)

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
