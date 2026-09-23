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

"""The options flow's "compare against another contract" branch.

Split out of ``config_flow.py`` with ``compare_quote.py``, which holds the
arithmetic these steps display.

``_CompareStepsMixin`` subclasses ``OptionsFlow`` rather than being a bare
mixin on purpose. A bare mixin declaring ``config_entry`` under TYPE_CHECKING
precedes ``OptionsFlow`` in the MRO and would shadow its read-only property
with a writable attribute, so mypy would bless an assignment that raises
AttributeError at runtime. ``--strict`` accepts the bare form; that is not the
argument for it.

``_compare`` stays a bare annotation with no value, so ``hasattr(self,
"_compare")`` is still False on first entry into the branch.
"""

from __future__ import annotations

import logging

from collections.abc import Awaitable, Callable
from typing import Any


import voluptuous as vol
from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .providers import all_extractors, offers_quarter_hourly, settlement_answer

from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_METER,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    METER_DYNAMIC,
    METER_MONO,
    METER_TYPES,
    SMART_METER_CONTRACT_KINDS,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_NONE,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
)
from .flow_schemas import (
    _compare_solar_schema,
    _settlement_schema,
    _validate_entsoe_key,
)
from .flow_contracts import (
    _contract_has_spot_injection,
    _contract_is_professional,
    _contract_kind,
    _contracts_for,
)
from .compare_placeholders import _PlaceholdersMixin
from .compare_inputs import (
    _effective_regime,
    _kva,
    _label_for_contract,
    _label_for_supplier,
)
from .compare_engine import (
    _SweepEngine,
)


def _compare_supplier_options(
    region: str, current_kind: str, professional: bool
) -> list[SelectOptionDict]:
    """Suppliers that have at least one contract available in the
    user's region. ``current_kind`` is kept in the signature for
    callers that may want to pre-filter, but the compare flow now
    accepts cross-kind quotes (static <-> dynamic): the dynamic
    side is priced from the user's spot cache or a fresh ENTSO-E
    fetch when crossing into dynamic territory.

    ``professional`` scopes the list to products the household could
    actually sign; see ``_compare_contract_schema`` for why."""
    out: list[SelectOptionDict] = []
    # By label, as the install picker lists them; the registry's import order
    # put a supplier added later wherever its import happened to land.
    for ext in sorted(all_extractors(), key=lambda e: e.label.casefold()):
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
        if not any(
            region in c.regions and c.professional == professional
            for c in ext.contracts
        ):
            continue
        out.append(SelectOptionDict(value=ext.id, label=ext.label))
    return out


def _compare_contract_schema(
    supplier_id: str,
    region: str,
    current_kind: str,
    exclude_contract: str,
    professional: bool,
) -> vol.Schema:
    """Contract picker scoped to the user's region and segment.

    Includes both static and dynamic contracts so the user can ask "should I
    switch from fixed to dynamic", and the user's OWN contract so they can ask
    "what would this same contract cost me on a bi-hourly meter, or on the
    injection tariff instead of compensation" - the two switches a household
    can make without changing supplier. ``exclude_contract`` is kept for
    callers that do want a strict alternative; pass "" for none.

    It does NOT cross the residential/professional line. A professional card
    is published excluding VAT and bands the federal excise by annual volume,
    so ``_resolve_snapshot`` grosses it at the entry's own rate: 21% against
    a residential 6%, while its excise is a fifth of the residential one and
    it carries a monthly energy-fund charge the residential card zeroes. The
    row that comes out is neither the price the household would pay nor a
    contract it could sign, and nothing on the page says so beyond the
    supplier's own "(pro)" label.
    """
    contracts = [
        c
        for c in _contracts_for(supplier_id, region)
        if c.id != exclude_contract and c.professional == professional
    ]
    options = [SelectOptionDict(value=c.id, label=c.label) for c in contracts]
    return vol.Schema(
        {
            vol.Required(CONF_CONTRACT): SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.LIST)
            )
        }
    )


_LOGGER = logging.getLogger(__name__)


class _CompareStepsMixin(_PlaceholdersMixin, OptionsFlow):
    """The compare branch, mixed into BePricesOptionsFlow."""

    _compare: dict[str, Any]
    _engine_obj: _SweepEngine | None = None

    @property
    def _engine(self) -> _SweepEngine:
        """The pricing engine for this dialog, built on first use.

        Built lazily rather than in a constructor: the mixin has none, and
        ``config_entry`` is an ``OptionsFlow`` property that is not resolvable
        until the flow manager has set the handler. The overrides dict is
        shared by reference, so a what-if collected on a later step is seen by
        pricing that already holds the engine.
        """
        if not hasattr(self, "_compare"):
            self._compare = {}
        if self._engine_obj is None:
            self._engine_obj = _SweepEngine(self.hass, self.config_entry, self._compare)
        return self._engine_obj

    async def async_step_compare(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.data
        current_kind = _contract_kind(
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            quarter_hourly=settlement_answer(self.config_entry.data),
        )
        own_professional = _contract_is_professional(
            current[CONF_SUPPLIER], current[CONF_CONTRACT]
        )
        if not hasattr(self, "_compare"):
            self._compare = {}
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_contract()
        options = _compare_supplier_options(
            current[CONF_REGION], current_kind, own_professional
        )
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
                        )
                    ),
                }
            ),
        )

    async def async_step_compare_contract(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        current = self.config_entry.data
        current_kind = _contract_kind(
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            quarter_hourly=settlement_answer(self.config_entry.data),
        )
        own_professional = _contract_is_professional(
            current[CONF_SUPPLIER], current[CONF_CONTRACT]
        )
        if user_input is not None:
            self._compare.update(user_input)
            return await self._after_compare_contract()
        # The contract picker spans both static and dynamic kinds (the
        # compare flow supports cross-kind quotes) and includes the user's
        # OWN contract.
        #
        # It used to exclude it, on the grounds that quoting a contract
        # against itself is a no-op. It is not: the meter and solar steps
        # that follow default to the entry's own settings but can be changed,
        # so picking your own contract answers "what would I pay on this same
        # contract with a bi-hourly meter", or "on the injection tariff
        # instead of compensation". Those are the two switches a household
        # can actually make without changing supplier, and they were the only
        # comparison the page could not do.
        remaining = [
            c
            for c in _contracts_for(self._compare[CONF_SUPPLIER], current[CONF_REGION])
            if c.professional == own_professional
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
                "",
                own_professional,
            ),
        )

    async def _after_compare_contract(self) -> ConfigFlowResult:
        if offers_quarter_hourly(
            self._compare.get(CONF_SUPPLIER), self._compare.get(CONF_CONTRACT)
        ):
            return await self.async_step_compare_settlement()
        self._compare.pop(CONF_QUARTER_HOURLY, None)
        return await self.async_step_compare_meter()

    async def async_step_compare_settlement(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Which settlement to quote the TARGET on.

        Asked on the target's own side, defaulted from the household's answer
        only where their own contract offers the same choice. Reading it off
        the entry unconditionally would quote a Bolt card per quarter-hour
        because the user happens to be on Frank's quarter-hourly settlement,
        which is the target-side hazard this page keeps having to relearn.
        """
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_meter()
        current = self.config_entry.data
        own_answer = (
            bool(current.get(CONF_QUARTER_HOURLY, False))
            if offers_quarter_hourly(
                current.get(CONF_SUPPLIER), current.get(CONF_CONTRACT)
            )
            else False
        )
        defaults = {
            CONF_QUARTER_HOURLY: self._compare.get(CONF_QUARTER_HOURLY, own_answer)
        }
        return self.async_show_form(
            step_id="compare_settlement",
            description_placeholders={
                "contract": _label_for_contract(
                    self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
                )
            },
            data_schema=_settlement_schema(defaults),
        )

    async def async_step_compare_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally override the meter type for the comparison.

        Static contracts (fixed / variable) can be quoted at mono or
        bi-hourly billing: some users want to know "what would I pay
        if I switched billing mode AND supplier". Dynamic / TOU
        contracts skip this step: their distribution requires a smart
        meter, picking bi-hourly would route distribution one way and
        energy another.
        """
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_solar()
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER],
            self._compare[CONF_CONTRACT],
            quarter_hourly=settlement_answer(self._compare),
        )
        # Dynamic, TOU and TOU-Impact contracts all require a smart
        # meter, so don't offer mono/bi for them: matching the install
        # flow's _meter_schema, which gates the same three kinds. (Mega
        # Off-peak Impact is "tou_impact"; omitting it here let the
        # compare flow show an impossible mono/bi meter for it.)
        if other_kind in SMART_METER_CONTRACT_KINDS:
            self._compare[CONF_METER] = METER_DYNAMIC
            return await self.async_step_compare_solar()
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

    async def async_step_compare_solar(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Optionally quote both sides under a different solar regime.

        The regime is a property of the grid connection, not of the
        supplier: two suppliers at one address are necessarily on the same
        one. So unlike the meter type, which is a billing mode the target
        contract can differ on, this override applies to BOTH sides. A
        target-only version would fold hundreds of euros of connection-side
        change into what reads as a supplier-vs-supplier delta.

        Runs before ``_after_compare_meter`` because that step decides
        whether the quote needs an ENTSO-E key, and on the injection regime
        a spot-indexed feed-in needs one. Deciding that on the stored
        regime would send a compensation entry quoting Cociter Variable
        straight to the result page with no key and silently credit zero.

        Skipped entirely for an entry with no solar, so the common case
        gains no click.
        """
        current = self.config_entry.data
        stored = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
        if stored == SOLAR_REGIME_NONE and _kva(current) <= 0.0:
            return await self._after_compare_meter()
        # A netted register cannot be told apart from a gross one by
        # reading it, so the volumes are asked for on the wiring that
        # cannot supply them rather than on the reading that comes back.
        from .energy_meters import _kwh_sensor_ids

        day_id, night_id, total_id = _kwh_sensor_ids(self.config_entry, "injection")
        ask_volumes = not ((day_id and night_id) or total_id)
        errors: dict[str, str] = {}
        if user_input is not None:
            picked = user_input.get(CONF_SOLAR_REGIME, stored)
            typed = (
                user_input.get(CONF_WHATIF_CONSUMPTION_KWH),
                user_input.get(CONF_WHATIF_INJECTION_KWH),
            )
            if picked != stored and ask_volumes and any(v is None for v in typed):
                # Refuse rather than quietly quoting the override off a
                # possibly-netted register: the error names what is
                # missing, where silently dropping the override would look
                # exactly like the picker not working.
                errors[CONF_WHATIF_CONSUMPTION_KWH] = "whatif_volumes_required"
            else:
                self._compare.update(user_input)
                return await self._after_compare_meter()
        # No {stored_regime} placeholder: the picker is a LIST selector with
        # the entry's own regime preselected and translated, so naming it in
        # prose would only interpolate an English label into the nl / fr / de
        # descriptions.
        return self.async_show_form(
            step_id="compare_solar",
            data_schema=_compare_solar_schema(
                {**current, **(user_input or {})}, ask_volumes=ask_volumes
            ),
            errors=errors,
        )

    async def _after_compare_meter(self) -> ConfigFlowResult:
        """Hand off to compare_result, prompting for an ENTSO-E key first
        when either side needs spot data the user's current entry doesn't
        already carry: a spot-priced target (dynamic per slot, spot-monthly
        per delivery month), or (on the injection regime) a
        spot-indexed-injection contract on EITHER side: the target like
        Cociter Variable, or the user's own keyless Cociter Variable entry,
        whose feed-in credit is priced off the hourly day-ahead. Keep
        this symmetric with the compare_spot_injection check in
        _build_compare_placeholders, which values both sides."""
        current = self.config_entry.data
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER],
            self._compare[CONF_CONTRACT],
            quarter_hourly=settlement_answer(self._compare),
        )
        needs_spot = other_kind in SPOT_PRICED_CONTRACT_KINDS or (
            _effective_regime(current, self._compare) == SOLAR_REGIME_INJECTION
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
        live endpoint before reaching the result page.

        Skippable, like the injection key step: a comparison is a one-off
        quote, so a user without a token should still see every other line
        of it rather than be stopped at a page they cannot get past. Blank
        leaves the key unset, and every reader of it already falls back to
        the entry's own key with ``or``.
        """
        errors: dict[str, str] = {}
        if user_input is not None:
            # Same idiom as the injection key step: a blanked password field
            # can be absent from user_input rather than present and empty.
            key = (user_input.get(CONF_API_KEY) or "").strip()
            if not key:
                return await self._after_compare_api_key()
            err = await _validate_entsoe_key(self.hass, key)
            if err is None:
                self._compare[CONF_API_KEY] = key
                return await self._after_compare_api_key()
            errors[CONF_API_KEY] = err
        return self.async_show_form(
            step_id="compare_api_key",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_API_KEY, default=""): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD)
                    )
                }
            ),
            errors=errors,
        )

    async def _after_compare_api_key(self) -> ConfigFlowResult:
        """Where the key step goes once it is done, keyed or not.

        Stored rather than hardcoded, because the ranking needs the same
        prompt but returns to its own sweep. Defaults to the one-to-one
        result, so nothing about that path changes.
        """
        nxt: Callable[[], Awaitable[ConfigFlowResult]] | None = getattr(
            self, "_api_key_next_step", None
        )
        if nxt is not None:
            return await nxt()
        return await self.async_step_compare_result()

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


_YTD_FIELD = "with_ytd"
_REFRESH_FIELD = "refresh"
