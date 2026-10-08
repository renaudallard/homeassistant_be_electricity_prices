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
  api_key   -> ENTSO-E key (when the contract is priced off the day-ahead
               market: dynamic per slot, spot-monthly on the month mean)
  capacity  -> Flemish capacity peak source (only when region = flanders)

OptionsFlow pre-fills every field with the current value, so the user can
change anything (including supplier/contract/region) post-install. On
finalize, OptionsFlow writes back to ``entry.data`` and updates the entry
title, unless the user renamed the entry.

No EUR values are asked. Energy + network + tax rates are fetched live by
the coordinator from each supplier's own publication.
"""

from __future__ import annotations


from collections.abc import Mapping
from datetime import date
from typing import Any

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .flow_switch import (
    _record_switch,
    _remove_last_switch,
    _removable_switch,
    _switch_schema,
    _validate_switch_date,
)
from .compare_sweep_flow import _SweepStepsMixin
from .const import (
    CONF_CONTRACT,
    CONF_SWITCH_DATE,
    CONF_DSO,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    METER_EXCLUSIVE_NIGHT,
)
from .providers import (
    get as get_extractor,
)
from .providers.base import ExtractorError
from .flow_wizard import _WizardStepsMixin


# ---- shared schema builders ---------------------------------------------------


def _entry_title(data: dict[str, Any]) -> str:
    extractor = get_extractor(data[CONF_SUPPLIER])
    contract_label = next(
        (c.label for c in extractor.contracts if c.id == data[CONF_CONTRACT]),
        data[CONF_CONTRACT],
    )
    return f"{extractor.label} - {contract_label} ({data[CONF_REGION].capitalize()})"


# The boxes of the signing-rate step, each against the field of the card's
# energy leg it overrides and the unit it is shown in.
_CARD_FIGURES = {
    "card_single": ("single", " EUR/kWh"),
    "card_peak": ("peak", " EUR/kWh"),
    "card_offpeak": ("offpeak", " EUR/kWh"),
    "card_exclusive_night": ("exclusive_night", " EUR/kWh"),
    "card_factor": ("factor", ""),
    "card_base": ("base", " EUR/kWh"),
}


def _card_figures(entry: ConfigEntry, data: Mapping[str, Any]) -> dict[str, str]:
    """The current card's figures, for the signing-rate step in Edit settings.

    Shown under each box so the household sees whether its contract states
    anything else, and in which scale and VAT basis to type it: a per-kWh
    figure as the entry holds it, which is how a typed one is read, and the
    fee including VAT, as its box asks. A deducting business holds its fee
    without VAT, so it is grossed back for the comparison. "-" where the
    entry has no card yet or the card has no such figure, so no box shows a
    raw placeholder.

    Only while the step is about the contract that card is for: ``data`` is
    what this edit has chosen so far, and an edit that moves the entry to
    another supplier, contract or region, which recording a switch always
    does, would otherwise show the card being left as the current one.
    """
    from .coordinator import BePricesCoordinator

    figures = dict.fromkeys((*_CARD_FIGURES, "card_fee", "card_month"), "-")
    coord = getattr(entry, "runtime_data", None)
    snapshot = coord._snapshot if isinstance(coord, BePricesCoordinator) else None
    if snapshot is None or any(
        data.get(key) != entry.data.get(key)
        for key in (CONF_SUPPLIER, CONF_CONTRACT, CONF_REGION)
    ):
        return figures
    energy = snapshot.energy
    for key, (field, unit) in _CARD_FIGURES.items():
        value = getattr(energy, field, None)
        if value is not None:
            figures[key] = f"{round(value, 6):g}{unit}"
    fee = energy.yearly_fixed_fee
    taxes = snapshot.taxes
    if not taxes.vat_rate and taxes.published_vat_rate:
        fee *= 1.0 + taxes.published_vat_rate
    figures["card_fee"] = f"{round(fee, 2):g} EUR"
    figures["card_month"] = snapshot.publication_label or "-"
    return figures


# ---- shared wizard steps ------------------------------------------------------


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

    def _signed_rate_placeholders(self) -> dict[str, str]:
        return _card_figures(self.config_entry, self._data)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        # Removing a switch is offered only to an entry that holds one from
        # this year.
        switched = (
            _removable_switch(self.config_entry.data, dt_util.now().date()) is not None
        )
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "edit",
                "switch",
                *(["remove_switch"] if switched else []),
                "compare",
                "compare_all",
            ],
        )

    async def async_step_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Record a change of supplier, then set up the new contract.

        Asks only for the new contract's first day. The entry's settings as
        they stand are kept as the contract held until the day before
        (``_record_switch``), and the edit chain that follows starts from them,
        so only what changed has to be picked again. The year-to-date then
        prices each contract on its own cards for its own days.
        """
        if not hasattr(self, "_data"):
            self._data = self._seed_data()
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_switch_date(self._data, user_input)
            if not errors:
                until = date.fromisoformat(user_input[CONF_SWITCH_DATE])
                self._data = _record_switch(self._data, until)
                return await self._async_entry_step()
        return self.async_show_form(
            step_id="switch",
            # A rejected date comes back as typed, not reset to today.
            data_schema=self.add_suggested_values_to_schema(
                _switch_schema(dt_util.now().date()), user_input
            ),
            errors=errors,
        )

    async def async_step_remove_switch(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Undo the last recorded switch, after saying which one it is.

        The entry goes back to the settings it had when that switch was
        recorded (``_remove_last_switch``): the contract left becomes the
        current one again. That is the way to correct a mistyped switch date,
        by removing it and recording it again, and to take back a switch
        recorded by mistake.
        """
        from .compare_inputs import _label_for_contract, _label_for_supplier

        last = _removable_switch(self.config_entry.data, dt_util.now().date())
        if last is None:
            return self.async_abort(reason="no_switch_recorded")
        if user_input is not None:
            self._data = _remove_last_switch(self._seed_data())
            return self._finalize()
        until, held = last
        supplier = str(held.get(CONF_SUPPLIER, ""))
        return self.async_show_form(
            step_id="remove_switch",
            description_placeholders={
                "until": until.isoformat(),
                "supplier": _label_for_supplier(supplier),
                "contract": _label_for_contract(
                    supplier, str(held.get(CONF_CONTRACT, ""))
                ),
            },
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
        # discard any stale options, and update a title the wizard made to
        # reflect the current supplier / contract / region; one the user
        # typed is theirs and stays. Skip the write entirely when nothing
        # changed: HA's update listener would otherwise fire a reload,
        # tearing down all entities and the warmed snapshot for no benefit.
        # ``self._data`` was seeded as ``{**entry.data, **entry.options}`` so
        # an entry that already carried options would otherwise miss this
        # shortcut on every re-edit (the merged dict can never equal
        # entry.data alone). Compare against the same merge so a no-op
        # re-edit really skips the reload.
        merged = {**self.config_entry.data, **self.config_entry.options}
        try:
            made = self.config_entry.title == _entry_title(merged)
        except ExtractorError:
            made = False
        new_title = _entry_title(self._data) if made else self.config_entry.title
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
    # and exits via async_abort: no entry, no options, nothing saved.
