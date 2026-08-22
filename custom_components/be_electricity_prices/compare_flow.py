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

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, cast

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .providers import all_extractors, get as get_extractor
from .providers.base import SpotMonthlyRates, SupplierSnapshot
from .spot_stats import _injection_is_spp_indexed

from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    DEFAULT_ANNUAL_CONSUMPTION_KWH,
    DSO_MODE_BI_HORAIRE,
    MEASURED_FULL_YEAR_DAYS,
    METER_DYNAMIC,
    METER_MONO,
    METER_TYPES,
    SMART_METER_CONTRACT_KINDS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_NONE,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
)
from .energy_meters import _measured_hour_weights, _measured_kwh
from .compare_quote import (
    _annual_bill,
    _compare_injection_credit,
    _populate_charts,
    _annual_volume,
    _covers_a_year,
    _read_total_kwh,
    _solar_note,
    _tou_weighted_per_kwh,
    _uncredited_note,
    _whatif_note,
)
from .flow_schemas import (
    _compare_solar_schema,
    _contract_has_spot_injection,
    _contract_kind,
    _contracts_for,
    _validate_entsoe_key,
)


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


@dataclass(frozen=True)
class _QuoteEntry:
    """Read-only stand-in for the ConfigEntry, carrying a what-if regime.

    Every helper the quote reaches (the annual bill, the fee legs, the
    injection price, the year-to-date walk) reads the solar regime off
    ``entry.data`` and touches nothing else on the entry, so swapping the
    mapping is enough to price a what-if.

    The alternative, threading a regime override down as a parameter,
    would change signatures the live coordinator and the backfill share.
    A defaulted override falling back to ``entry.data`` in one of those
    three paths and not the others is exactly how the cost legs have
    drifted apart before, and none of that risk buys anything here: the
    compare branch is the only caller that needs a hypothetical regime.

    Purpose-built rather than a copy of the real entry: HA's ConfigEntry
    refuses to rebind ``data``, and a copy would carry the same entry id
    into anything that later looked at it.
    """

    data: Mapping[str, Any]


def _needs_month_mean(snapshot: SupplierSnapshot | None) -> bool:
    """True when this side's energy bills the delivery month's mean spot.

    A ``SpotMonthlyRates`` leg is flat for the whole month, so quoting it at a
    day-ahead window mean is not an approximation of what it bills, it is a
    different number - and one that moves day to day while the contract's does
    not.
    """
    return snapshot is not None and isinstance(snapshot.energy, SpotMonthlyRates)


def _effective_regime(current: Mapping[str, Any], compare: Mapping[str, Any]) -> str:
    """The solar regime this quote prices on: the what-if pick when the
    compare_solar step ran, else the entry's own."""
    stored = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    return str(compare.get(CONF_SOLAR_REGIME, stored))


def _quote_entry(entry: ConfigEntry, regime: str) -> ConfigEntry:
    """``entry`` itself when the what-if matches it, else a proxy holding
    the overridden regime.

    Returning the real entry unchanged on the common path keeps every
    quote that does not use the what-if on exactly the code it ran
    before, proxy included.
    """
    if regime == entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE):
        return entry
    # Only entry.data is ever read through this (audited across the quote,
    # fee, injection and year-to-date helpers), so the mapping is a
    # complete stand-in; the cast is what tells mypy that.
    return cast(ConfigEntry, _QuoteEntry({**entry.data, CONF_SOLAR_REGIME: regime}))


def _kva(data: Mapping[str, Any]) -> float:
    """Configured inverter capacity, 0.0 when unset or unparseable."""
    try:
        return float(data.get(CONF_SOLAR_KVA, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


class _CompareStepsMixin(OptionsFlow):
    """The compare branch, mixed into BePricesOptionsFlow."""

    _compare: dict[str, Any]

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
            return await self.async_step_compare_solar()
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        # Dynamic, TOU and TOU-Impact contracts all require a smart
        # meter, so don't offer mono/bi for them -- matching the install
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
        spot-indexed-injection contract on EITHER side -- the target like
        Cociter Variable, or the user's own keyless Cociter Variable entry
        -- whose feed-in credit is priced off the hourly day-ahead. Keep
        this symmetric with the compare_spot_injection check in
        _build_compare_placeholders, which values both sides."""
        current = self.config_entry.data
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
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

        Annual = per_kwh_now * the resolved yearly volume + yearly fees, where the
        yearly fees are yearly_fixed_fee + 12 * energy_fund + 12 *
        capacity (Flanders) + 12 * prosumer (Wallonia compensation +
        solar). Errors collapse to ``-`` so the page always renders.
        """

        from .coordinator import BePricesCoordinator

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
                "annual_kwh": f"{DEFAULT_ANNUAL_CONSUMPTION_KWH:.0f}",
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
        # The what-if regime, if the compare_solar step ran, and a proxy
        # entry carrying it. Everything downstream that prices money takes
        # the proxy; the real entry stays for runtime_data and for reading
        # the household's own meters, which are facts, not hypotheses.
        stored_regime = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
        regime = _effective_regime(current, self._compare)
        quote_entry = _quote_entry(self.config_entry, regime)
        overridden = quote_entry is not self.config_entry

        now_utc = dt_util.utcnow()
        today_local = dt_util.now().date()
        jan1 = today_local.replace(month=1, day=1)
        # 364, not 365: energy_meters._recorder_rows anchors end_dt on the next
        # local midnight, so the window is end-inclusive and today counts. The
        # old arithmetic read 366 buckets under a "365 days" label.
        year_ago = today_local - timedelta(days=MEASURED_FULL_YEAR_DAYS - 1)
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
        # Cross-kind comparisons (static <-> spot-priced) need spot data
        # for the spot-priced side. The user's coordinator already has
        # spots when they're on one; otherwise borrow the api key
        # they just typed in compare_api_key (or the one already on
        # their entry) and fetch the day-ahead window for today.
        # A spot-monthly side counts: without a spot its energy leg cannot be
        # priced at all and the quote renders a bare "-" for a contract the
        # user explicitly asked about.
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
        need_spot = (
            current_kind in SPOT_PRICED_CONTRACT_KINDS
            or other_kind in SPOT_PRICED_CONTRACT_KINDS
            or compare_spot_injection
        )
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
        # A SPOT-MONTHLY leg is different: it does not average out over a year,
        # it bills one flat rate per delivery month, and that month's mean is a
        # number the coordinator already computes. Pricing it off a single
        # day-ahead window instead makes the quote swing with the day the
        # dialog happened to open, and prints a "per kWh now" for the user's
        # OWN entry that contradicts their current_price sensor. Resolved
        # lazily and once: the month backfill is only worth its fetch when a
        # side actually bills on it, and the target's snapshot is not retrieved
        # until further down.
        month_spot_resolved: list[float | None] = []

        async def _month_spot() -> float | None:
            if month_spot_resolved:
                return month_spot_resolved[0]
            value = avg_spot
            key = self._compare.get(CONF_API_KEY) or current.get(CONF_API_KEY)
            try:
                await coord._ensure_historical_spots(
                    today_local.replace(day=1), today_local, key
                )
            except Exception:  # noqa: BLE001 - degrade to the day-ahead mean
                pass
            resolved = coord._monthly_spot_mean(
                today_local.year, today_local.month, spot_dict
            )
            if resolved is not None:
                value = resolved
            month_spot_resolved.append(value)
            return value

        spp_spot_resolved: list[float | None] = []

        async def _spp_month_spot() -> float | None:
            """The SPP-weighted month mean an SPP-indexed credit bills on.

            One index further than _month_spot: a card that indexes its
            feed-in on Belpex_SPP may not be resolved against any other mean,
            so when the Synergrid profile is not loaded this answers None and
            the caller keeps the card's printed indicative rather than
            quoting the consumption index. Reuses the profile the coordinator
            already holds; the dialog never triggers the 52 MB download
            itself.
            """
            if spp_spot_resolved:
                return spp_spot_resolved[0]
            await _month_spot()  # fills the month's spots in the cache
            spp_spot_resolved.append(
                coord._spp_weighted_month_mean(
                    today_local.year, today_local.month, spot_dict
                )
            )
            return spp_spot_resolved[0]

        async def _spp_spot_for(snapshot: SupplierSnapshot | None) -> float | None:
            """The SPP month mean, resolved only for a card indexed on it."""
            if not _injection_is_spp_indexed(snapshot):
                return None
            return await _spp_month_spot()

        async def _spot_for(snapshot: SupplierSnapshot | None) -> float | None:
            """The spot this side's energy shape actually bills on."""
            return await _month_spot() if _needs_month_mean(snapshot) else avg_spot

        # Measured consumption / injection from the user's kWh sensors.
        # Injection is only relevant when a solar regime is configured; for
        # the "none" regime it stays 0 even if a sensor is wired. Read it
        # when EITHER regime has solar, not just the quoted one: a what-if
        # into "none" still prices the baseline leg on the entry's own
        # regime, and zeroing the volume there would un-net a compensation
        # baseline (or drop an injection credit) and quote the user's own
        # contract as costing more than it does.
        ytd_kwh = await _read_total_kwh(self.hass, self.config_entry, jan1, today_local)
        rolling_inj_kwh = 0.0
        ytd_inj_kwh = 0.0
        inj_full_year = False
        if regime != SOLAR_REGIME_NONE or stored_regime != SOLAR_REGIME_NONE:
            # Injection stays on the raw window sum. Putting it through
            # _annual_volume looked symmetric and is wrong in both bands: below
            # the floor it discards a real feed-in measurement, and _solar_note
            # reads that zero as "no injection sensor wired" while the same page
            # prints the YTD injected kWh; above it, PV is far more seasonal
            # than consumption, so a 365/days factor on a spring window can
            # over-credit enough to drive the compensation net to its zero clamp
            # and quote both sides at fees only. Annualising this leg needs a
            # production profile, not a day count.
            measured_inj = await _measured_kwh(
                self.hass, self.config_entry, year_ago, today_local, side="injection"
            )
            y = await _read_total_kwh(
                self.hass, self.config_entry, jan1, today_local, side="injection"
            )
            rolling_inj_kwh = measured_inj.kwh if measured_inj.kwh > 0 else 0.0
            inj_full_year = _covers_a_year(measured_inj.days_with_data)
            ytd_inj_kwh = y or 0.0
        annual = await _annual_volume(
            self.hass, self.config_entry, year_ago, today_local
        )
        annual_kwh = annual.kwh
        consumption_source = annual.source
        # Under the netting regime the two legs are SUBTRACTED, so they have to
        # be on one basis. Annualising consumption while injection stays on the
        # raw window nets a whole year of draw against a fraction of a year of
        # feed-in: measured on a seasonal prosumer with 300 days of history that
        # quoted 434 EUR against a true 186, and the page printed its own
        # contradiction ("annual_kwh 3706" beside "netted, consumption -= 2625").
        # Scaling the feed-in leg to match is not the answer either, since PV is
        # seasonal enough that a summer window over-credits into the zero clamp.
        # So when the feed-in side cannot be annualised honestly, neither side
        # is, and the quote is the measured window on both legs, which is what
        # this page did before the volume resolver existed.
        if (
            regime == SOLAR_REGIME_COMPENSATION
            or stored_regime == SOLAR_REGIME_COMPENSATION
        ) and not inj_full_year:
            raw = await _read_total_kwh(
                self.hass, self.config_entry, year_ago, today_local
            )
            if raw is not None:
                annual_kwh = raw
                consumption_source = (
                    f"{annual.days_with_data} days measured, netted against the same "
                    "window's injection rather than annualised"
                )
        # Volumes typed on the what-if step replace the measured pair. The
        # step only offers them when no injection sensor is wired, which is
        # the wiring whose consumption register may already be netted, so
        # the typed figures are the only gross ones available.
        typed_cons = self._compare.get(CONF_WHATIF_CONSUMPTION_KWH)
        typed_inj = self._compare.get(CONF_WHATIF_INJECTION_KWH)
        volumes_typed = typed_cons is not None and typed_inj is not None
        if typed_cons is not None and typed_inj is not None:
            annual_kwh = float(typed_cons)
            rolling_inj_kwh = float(typed_inj)
            consumption_source = "entered for the what-if"

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
            # Typed volumes describe a full year, not the elapsed part of
            # this one, and the year-to-date legs replay meter history that
            # was recorded under the configured regime, so both are left
            # blank rather than mixing the two.
            "ytd_kwh": ("-" if volumes_typed or ytd_kwh is None else f"{ytd_kwh:.0f}"),
            "annual_chart": "",
            "ytd_chart": "",
            "ytd_injection_kwh": (
                f"{ytd_inj_kwh:.0f}"
                if regime != SOLAR_REGIME_NONE and not volumes_typed
                else "-"
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
        # The card the entry is actually configured on, which the baseline
        # leg prices. Only the expert custom supplier builds its snapshot
        # out of entry.data, so only there can the what-if card and the
        # configured one differ at all.
        baseline_snapshot = current_snapshot
        if overridden and current[CONF_SUPPLIER] == SUPPLIER_CUSTOM:
            # That supplier has no card to fetch, and its injection block is
            # dropped unless entry.data says injection, so a what-if has to
            # rebuild it from the proxy or the custom side credits nothing
            # whatever the user picks. Resolve it the way the coordinator
            # does: build_snapshot returns the card ex-VAT with the entered
            # rate on taxes, and nothing else grosses the fixed fees.
            from .providers.custom import build_snapshot
            from .snapshot_store import _resolve_snapshot

            try:
                current_snapshot = _resolve_snapshot(
                    quote_entry, build_snapshot(quote_entry.data, region, dso)
                )
            except Exception:  # noqa: BLE001 - keep the configured snapshot
                pass
        if current_snapshot is not None:
            from .cohort import _cohort_energy_leg

            cohort = await _cohort_energy_leg(
                self.hass,
                async_get_clientsession(self.hass),
                get_extractor(current[CONF_SUPPLIER]),
                current[CONF_CONTRACT],
                region,
                quote_entry,
                current_snapshot,
            )
            if cohort is not None:
                spliced = replace(current_snapshot, energy=cohort)
                # The splice carries the signed yearly fee, which the fee
                # legs read, so the baseline has to follow it whenever the
                # two are the same card.
                if baseline_snapshot is current_snapshot:
                    baseline_snapshot = spliced
                current_snapshot = spliced

        # The household's own hour-of-day consumption shape, so a time-of-use
        # card is quoted on the kWh it actually bills rather than on clock
        # hours. Reads only entry.data, so the _QuoteEntry proxy is safe here.
        hour_weights = await _measured_hour_weights(
            self.hass, self.config_entry, year_ago, today_local
        )
        # And the export shape, for a per-slot feed-in credit. Averaging those
        # slots by duration credits the overnight block, which is a third of
        # the clock and produces nothing.
        inj_hour_weights = await _measured_hour_weights(
            self.hass, self.config_entry, year_ago, today_local, side="injection"
        )
        current_per_kwh: float | None = None
        if current_snapshot is not None:
            current_per_kwh = _tou_weighted_per_kwh(
                current_snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                await _spot_for(current_snapshot),
                current_meter,
                dso_mode,
                hour_weights,
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
        from .snapshot_store import _resolve_snapshot

        try:
            other_snap = _resolve_snapshot(
                quote_entry,
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
                    await _spot_for(other_snap),
                    meter,
                    dso_mode,
                    hour_weights,
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
        # One clause per side that ends up crediting nothing. Both the
        # "no injection tariff on the card" and the "spot-indexed but no
        # spot" cases land on the same silent no-credit branch of
        # _annual_bill, so the page has to name them or it reads as if the
        # printed credit applied to both sides.
        uncredited: list[str] = []
        if regime == SOLAR_REGIME_INJECTION:
            if current_snapshot is not None:
                current_inj_price = _compare_injection_credit(
                    current_snapshot,
                    quote_entry,
                    spot_dict,
                    avg_spot,
                    await _spp_spot_for(current_snapshot),
                    inj_hour_weights,
                )
                if current_inj_price is None and rolling_inj_kwh > 0:
                    uncredited.append(
                        _uncredited_note(
                            current_snapshot,
                            _label_for_supplier(current[CONF_SUPPLIER]),
                        )
                    )
            if other_snap is not None:
                compare_inj_price = _compare_injection_credit(
                    other_snap,
                    quote_entry,
                    spot_dict,
                    avg_spot,
                    await _spp_spot_for(other_snap),
                    inj_hour_weights,
                )
                if compare_inj_price is None and rolling_inj_kwh > 0:
                    uncredited.append(
                        _uncredited_note(
                            other_snap,
                            _label_for_supplier(self._compare[CONF_SUPPLIER]),
                        )
                    )

        current_annual: float | None = None
        if current_per_kwh is not None:
            current_annual = _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                meter=current_meter,
            )
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = f"{current_annual:.2f}"
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, quote_entry, peak_kw, other_per_kwh, annual_kwh, rolling_inj_kwh, compare_inj_price, meter=meter):.2f}"
            )

        # The compare branch cannot quote the user against their own
        # contract (the picker excludes it), so a regime what-if that moves
        # both sides together barely moves the printed supplier delta. Price
        # the user's own contract once more under the entry as configured:
        # that difference is the whole question a what-if is asking.
        baseline_annual: float | None = None
        if overridden and current_per_kwh is not None and baseline_snapshot is not None:
            baseline_inj_price = (
                _compare_injection_credit(
                    baseline_snapshot,
                    self.config_entry,
                    spot_dict,
                    avg_spot,
                    await _spp_spot_for(baseline_snapshot),
                    inj_hour_weights,
                )
                if stored_regime == SOLAR_REGIME_INJECTION
                else None
            )
            baseline_annual = _annual_bill(
                baseline_snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                baseline_inj_price,
                meter=current_meter,
            )
        placeholders["solar_note"] = _whatif_note(
            _solar_note(regime, rolling_inj_kwh, uncredited),
            stored_regime=stored_regime,
            regime=regime,
            baseline_eur=baseline_annual,
            whatif_eur=current_annual,
            volumes_typed=volumes_typed,
            missing_kva=_kva(current) <= 0.0,
        )
        if (
            current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and current_snapshot is not None
        ):
            delta = _annual_bill(
                other_snap,
                quote_entry,
                peak_kw,
                other_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                compare_inj_price,
                meter=meter,
            ) - _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                meter=current_meter,
            )
            placeholders["delta_annual"] = f"{'+' if delta >= 0 else ''}{delta:.2f}"

        # Both year-to-date paths replay the household's own meter history,
        # which was recorded under the configured regime and whose
        # consumption register may already be netted. Typed volumes are a
        # yearly hypothesis with no history behind them, so the legs stay
        # blank rather than mixing a what-if with a measured past.
        if volumes_typed:
            _populate_charts(
                placeholders,
                current_label=_label_for_supplier(current[CONF_SUPPLIER]),
                compare_label=_label_for_supplier(self._compare[CONF_SUPPLIER]),
            )
            return placeholders

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
        from .ytd_cost import _compute_current_year_cost

        current_extractor = get_extractor(current[CONF_SUPPLIER])
        # Exclude spot-priced sides from the archive engine: it bills each
        # past hour at factor*spot+base (or the month's mean) and needs the
        # historical spot cache, which _compute_current_year_cost only
        # receives on the live coordinator path -- called without it here it
        # returns the fees-only floor (zero energy), so a fixed-vs-dynamic
        # compare would show the dynamic side missing its entire energy bill.
        # The simple per-kwh model below prices both sides off the same
        # current per-kwh rate and proration, so the delta stays honest.
        # spot_monthly is in that set for the same reason as dynamic. It
        # cannot be reached today (no spot-monthly supplier keeps an archive,
        # so the fetch_for_month test already fails), but energie.be does
        # publish one and wiring it up is a live proposal - which would arm
        # this the moment it lands.
        archive_capable = (
            current_extractor.fetch_for_month is not None
            and other_extractor.fetch_for_month is not None
            and current_kind not in SPOT_PRICED_CONTRACT_KINDS
            and other_kind not in SPOT_PRICED_CONTRACT_KINDS
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
                    quote_entry,
                    historical_spots=hist_spots,
                    billed_peak_kw=peak_kw,
                )
                compare_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    other_extractor,
                    other_snap,
                    quote_entry,
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
                quote_entry,
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
                quote_entry,
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
