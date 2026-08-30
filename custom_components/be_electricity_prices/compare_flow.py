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

from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any, cast

import asyncio
import contextlib

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant
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
from .providers._pdf import memoise_text_fetches
from .providers.base import SpotMonthlyRates, SupplierSnapshot
from .spot_stats import _injection_is_spp_indexed, _spp_weighting_enabled

from .const import (
    COMPARE_SWEEP_BUDGET_S,
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
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
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
    DailyCompare,
    RankedRow,
    _annual_bill,
    _annual_volume,
    _card_caveats,
    _compare_injection_credit,
    _consumption_weighted_spot,
    _covers_a_year,
    _populate_charts,
    _ranking_table,
    _read_total_kwh,
    _row_label,
    _solar_note,
    _tou_weighted_per_kwh,
    _uncredited_note,
    _vintage_note,
    _whatif_note,
)
from .flow_schemas import (
    _compare_solar_schema,
    _contract_group,
    _contract_has_spot_injection,
    _contract_is_professional,
    _contract_kind,
    _contracts_for,
    _sweep_candidates,
    _validate_entsoe_key,
)


@contextmanager
def _borrowed_spot_cache(coord: Any, *, isolate: bool) -> Iterator[None]:
    """Put the coordinator's spot caches back after a compare-only fetch.

    The compare page borrows ``_ensure_historical_spots`` to price a target,
    and the next tick persists whatever that leaves behind
    (``_save_persistent``). A household with no stored key can otherwise seed
    its own persistent cache by opening this dialog and typing one, and then
    never refresh it, so a partial month mean gets baked over the card's
    printed indicative for the rest of the month.

    Three attributes are saved, not two. A day listed in
    ``_complete_spot_days`` is treated as fully present without consulting
    the hour dict at all, so it has to travel with them: emptying the dicts
    alone leaves the fetch believing every day the coordinator has already
    walked is covered, and it returns without fetching anything.

    Copied and restored in place rather than rebound, because
    ``_ensure_historical_spots`` merges each chunk into the attribute and
    re-resolves it after every await.

    ``isolate`` empties the caches first, for a caller that wants only the
    hours it fetched itself; without it the fetch merges into what is
    already there, which is what a month mean wants.
    """
    saved_spots = dict(coord._historical_spots)
    saved_quarters = dict(coord._historical_spot_quarters)
    saved_complete = set(coord._complete_spot_days)
    if isolate:
        coord._historical_spots.clear()
        coord._historical_spot_quarters.clear()
        coord._complete_spot_days.clear()
    try:
        yield
    finally:
        coord._historical_spots.clear()
        coord._historical_spots.update(saved_spots)
        coord._historical_spot_quarters.clear()
        coord._historical_spot_quarters.update(saved_quarters)
        coord._complete_spot_days.clear()
        coord._complete_spot_days.update(saved_complete)


def _compare_supplier_options(
    region: str, current_kind: str, professional: bool
) -> list[SelectOptionDict]:
    """Suppliers that have at least one contract available in the
    user's region. ``current_kind`` is kept in the signature for
    callers that may want to pre-filter, but the compare flow now
    accepts cross-kind quotes (static <-> dynamic) -- the dynamic
    side is priced from the user's spot cache or a fresh ENTSO-E
    fetch when crossing into dynamic territory.

    ``professional`` scopes the list to products the household could
    actually sign; see ``_compare_contract_schema`` for why."""
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
    so ``_resolve_snapshot`` grosses it at the entry's own rate -- 21% against
    a residential 6% -- while its excise is a fifth of the residential one and
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


def _chart_labels(
    current: Mapping[str, Any], compare: Mapping[str, Any]
) -> tuple[str, str]:
    """The two row labels for the comparison charts.

    The supplier name alone stops distinguishing the sides as soon as both
    contracts come from one supplier, which the picker now allows outright.
    Fall back through what actually differs: supplier, then contract, then
    neither - the same contract quoted against itself under a different meter
    or regime, where the only honest labels are which side is which.
    """
    cur_supplier = _label_for_supplier(current[CONF_SUPPLIER])
    cmp_supplier = _label_for_supplier(compare[CONF_SUPPLIER])
    if cur_supplier != cmp_supplier:
        return cur_supplier, cmp_supplier
    cur_contract = _label_for_contract(current[CONF_SUPPLIER], current[CONF_CONTRACT])
    cmp_contract = _label_for_contract(compare[CONF_SUPPLIER], compare[CONF_CONTRACT])
    if cur_contract != cmp_contract:
        return cur_contract, cmp_contract
    return "Your entry", "Quoted"


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


def _quote_entry(
    entry: ConfigEntry, regime: str, dso_mode: str | None = None
) -> ConfigEntry:
    """``entry`` itself when the what-if matches it, else a proxy holding
    the overridden regime and DSO tariff mode.

    Returning the real entry unchanged on the common path keeps every
    quote that does not use the what-if on exactly the code it ran
    before, proxy included.

    ``dso_mode`` is the target side's billing configuration, which is not
    always the household's: a Tarif Impact product is only sold on the
    incitative one. It rides the proxy rather than a parameter for the same
    reason the regime does, and it reaches further, because the fee leg and
    the year-to-date engine both read it straight off ``entry.data``.
    """
    overrides: dict[str, Any] = {}
    if regime != entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE):
        overrides[CONF_SOLAR_REGIME] = regime
    if dso_mode is not None and dso_mode != entry.data.get(
        CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE
    ):
        overrides[CONF_DSO_TARIFF_MODE] = dso_mode
    if not overrides:
        return entry
    # Only entry.data is ever read through this (audited across the quote,
    # fee, injection and year-to-date helpers), so the mapping is a
    # complete stand-in; the cast is what tells mypy that.
    return cast(ConfigEntry, _QuoteEntry({**entry.data, **overrides}))


def _kva(data: Mapping[str, Any]) -> float:
    """Configured inverter capacity, 0.0 when unset or unparseable."""
    try:
        return float(data.get(CONF_SOLAR_KVA, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class _HouseholdQuote:
    """The half of a quote that belongs to the household rather than to any
    contract it is being compared against.

    Wide on purpose: these are the values the compare arithmetic reads, and
    naming them here is what lets a ranking resolve them once for a whole
    cell instead of once per row. Three of the fields are callables closed
    over the rest -- the spot, SPP and export-rate resolvers -- because each
    memoises a fetch that must happen at most once per page.
    """

    region: str
    dso: str
    current_meter: str
    dso_mode: str
    peak_kw: float
    stored_regime: str
    regime: str
    quote_entry: Any
    overridden: bool
    now_utc: datetime
    today_local: date
    jan1: date
    fee_proration: float
    month_proration: float
    spot_dict: dict[datetime, float]
    current_kind: str
    avg_spot: float | None
    compare_spot_injection: bool
    ytd_kwh: float | None
    rolling_inj_kwh: float
    ytd_inj_kwh: float
    annual_kwh: float
    volumes_typed: bool
    placeholders: dict[str, str]
    current_snapshot: Any
    raw_snapshot: Any
    baseline_snapshot: Any
    hour_weights: Any
    inj_hour_weights: Any
    current_per_kwh: float | None
    current_export_per_kwh: float | None
    spot_for: Any
    spp_spot_for: Any
    export_rate_for: Any


_LOGGER = logging.getLogger(__name__)


async def async_run_daily_compare(
    hass: HomeAssistant, entry: ConfigEntry, coord: Any
) -> None:
    """Run the scheduled ranking and publish it through the coordinator.

    Swallows its own failures on purpose. This runs on a timer with nobody
    watching, and a supplier that changed its site overnight must not take an
    entry down with it: the ranking simply keeps yesterday's answer, which the
    sensor timestamps, rather than the whole entry going unavailable over a
    comparison nobody asked for at that moment.
    """
    engine = _SweepEngine(hass, entry, {})
    try:
        result = await engine.run_full_sweep(coord)
    except Exception:  # noqa: BLE001 - a timer job, not a user action
        _LOGGER.exception("Scheduled comparison failed for %s", entry.title)
        return
    if isinstance(result, str):
        # No cell to rank, which is an answer rather than a fault: the entry's
        # contract is the only one of its kind sold where it lives.
        _LOGGER.debug("Scheduled comparison skipped for %s: %s", entry.title, result)
        return
    coord.daily_compare = result
    coord.async_update_listeners()


class _SweepEngine:
    """The pricing engine behind both comparison pages.

    Holds only what pricing needs -- the household's entry, the hass it reads
    its meters and recorder through, and the what-if overrides the dialog
    collects -- so the same code prices a sweep the user is watching and one
    running on a schedule with nobody watching. It is deliberately not a flow:
    a scheduled sweep has no steps, no progress and no abort, and reaching
    into ``OptionsFlow`` for ``config_entry`` would tie a background job to
    flow-manager internals that move between releases.

    ``overrides`` is the dialog's ``_compare`` dict, shared by reference so a
    what-if picked on one step is seen by the pricing on the next. A scheduled
    sweep passes an empty one: there is no user to ask, so the entry's own
    settings are the only answer.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        overrides: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self._compare = overrides

    async def run_full_sweep(self, coord: Any) -> DailyCompare | str:
        """Price the whole cell with nobody watching, or say why not.

        No budget and no skipping, unlike the dialog. The wall-clock budget
        exists because somebody is staring at a progress bar; a scheduled run
        has all night, and stopping early would publish a ranking whose
        cheapest row is merely the cheapest one that fitted.

        Sequential rather than gathered, deliberately. Sixteen suppliers
        fetched at once is a burst on sixteen servers to save a couple of
        minutes nobody is waiting through, and the listing memo below only
        pays off when candidates sharing a listing page run one after another.
        """
        sweep = self.build_sweep()
        if isinstance(sweep, str):
            return sweep
        sweep["household"] = await self._resolve_household(
            coord,
            candidates=sweep["candidates"],
            meter=self.config_entry.data.get(CONF_METER, METER_MONO),
        )
        rows: list[RankedRow] = []
        own = await self._sweep_own_row(sweep["household"])
        if own is not None:
            rows.append(own)
        for supplier, contract in sweep["candidates"]:
            try:
                rows.append(await self._sweep_one(sweep, supplier, contract))
            except Exception as err:  # noqa: BLE001 - one row, not the sweep
                # Same rule as the dialog: a row that raised is still a row,
                # because dropping it would read as "not competitive".
                rows.append(
                    RankedRow(
                        label=_row_label(
                            _label_for_supplier(supplier),
                            _label_for_contract(supplier, contract),
                        ),
                        annual=None,
                        status=f"could not be priced: {err}",
                    )
                )
        return DailyCompare(
            rows=tuple(rows),
            own=own.annual if own is not None else None,
            priced=sum(1 for r in rows if r.annual is not None and not r.is_own),
            total=len(sweep["candidates"]),
            ran_at=dt_util.utcnow(),
        )

    def build_sweep(self) -> dict[str, Any] | str:
        """The sweep state for this entry's cell, or why there is none.

        Returns the reason string rather than raising, because the dialog
        turns it into an abort and the scheduled run into a log line, and the
        two disagree about what a missing cell means to the user.
        """
        current = self.config_entry.data
        region = current[CONF_REGION]
        group = _contract_group(current[CONF_SUPPLIER], current[CONF_CONTRACT])
        if not group:
            # The entry's contract has left the catalogue, so there is no
            # group to rank it within. Distinct from an empty cell: nothing is
            # missing from the market, we just cannot place this household.
            return "compare_all_unknown_contract"

        candidates = _sweep_candidates(
            region,
            group,
            _contract_is_professional(current[CONF_SUPPLIER], current[CONF_CONTRACT]),
            current[CONF_CONTRACT],
        )
        if not candidates:
            # A real answer, not a failure: a Brussels time-of-use household
            # has exactly one slot contract in the region and it is theirs.
            # Saying so is more use than an empty table.
            return "compare_all_no_alternatives"

        # Cheapest card first, so a budget buys many rows before few. Ties
        # broken on the label so the order is stable between opens and the
        # table does not reshuffle when a user reopens to finish it.
        candidates.sort(
            key=lambda pair: (get_extractor(pair[0]).sweep_cost_s, pair[0], pair[1].id)
        )
        return {
            "region": region,
            "group": group,
            "candidates": [(supplier, c.id) for supplier, c in candidates],
            "index": 0,
            "rows": [],
            # One listing memo for the whole sweep; see _sweep_one.
            "listings": {},
            # A row carries only its rendered label, so the year-to-date pass
            # needs a way back to the contract that produced it.
            "labels": {
                _row_label(
                    _label_for_supplier(supplier), _label_for_contract(supplier, c.id)
                ): (supplier, c.id)
                for supplier, c in candidates
            },
        }

    async def _resolve_household(
        self,
        coord: Any,
        *,
        candidates: Sequence[tuple[str, str]],
        meter: str,
    ) -> _HouseholdQuote:
        """Everything a quote needs that does not depend on which contract is
        being quoted.

        Resolved once, whatever the page above it is doing. The one-to-one
        compare passes a single candidate; a ranking passes the whole cell and
        pays for this exactly once rather than once per row, which is what
        makes a sweep affordable at all: the meter reads, the recorder walk,
        the measured hour shapes and the day-ahead window are all O(1) in the
        number of contracts being compared.

        ``candidates`` is read for one decision only -- whether any row will
        need day-ahead spots -- because that window is fetched here and shared.

        ``meter`` is the target's, not the household's: it lands in the
        rendered ``meter_used`` token, and a dynamic or slot contract forces
        its own. The household's real meter stays on ``current_meter``, so a
        mono household's own bill is never quoted at the target's rates.
        """
        current = self.config_entry.data
        region = current[CONF_REGION]
        dso = current[CONF_DSO]
        # Comparison may override the meter type for static contracts;
        # falls back to the current entry's setting.
        # The comparison may override the meter for the TARGET only (a
        # dynamic/TOU target forces METER_DYNAMIC). The user's current side
        # must keep its real meter, else a mono user's current bill gets
        # quoted at bi-horaire / dynamic rates and biases the decision.
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
        # The prosumer fee and the Flanders capacity tariff are both billed
        # per-month in the live sensor and backfill (each month's charge
        # prorated by its OWN days), not by the uniform days_in_year fraction,
        # so mirror that: every completed month counts as 1 plus the elapsed
        # fraction of the current one. _ytd_prosumer and _ytd_capacity sum
        # exactly this, which is why one number serves both.
        first_of_month = today_local.replace(day=1)
        next_month = date(
            today_local.year + today_local.month // 12,
            today_local.month % 12 + 1,
            1,
        )
        month_proration = (today_local.month - 1) + today_local.day / (
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
        # A Tarif Impact product is sold only on the CWaPE incitative
        # configuration: its energy carries three band rates and no
        # mono/bi structure at all, so the band schedule prices it whatever
        # the household is on, while the network leg and the Walloon terme
        # fixe both follow the mode. Quoting the TARGET on the household's
        # own mode therefore banded its energy, billed its network off the
        # standard jour/nuit columns and charged it a fixed term the tariff
        # does not have. The install flow forces the mode for exactly this
        # reason; mirror it here, for the target only, the same way the
        # meter override applies to the target only.
        #
        # Gated on the registered kind, which deliberately leaves
        # totalenergies_impact out: it is registered "variable" and its impact
        # bands are read only in impact mode, so a household on the standard
        # configuration quoting it still bills the target's network leg on the
        # jour/nuit columns, worth about EUR 29/yr on a bi meter and EUR 113 on
        # a mono one. Not forced, for the reason _IMPACT_DEFAULT_CONTRACTS
        # gives at flow_schemas.py:514: the TE card states only that a
        # communicating digital meter is required, so a holder on the standard
        # configuration genuinely exists and forcing would under-bill them by
        # the same amount in the other direction. The install flow pre-selects
        # the mode for that card and lets the user say otherwise, which is the
        # decision this flow has no step to ask about.
        # A spot-indexed-injection side (Cociter Variable) prices its feed-in
        # credit off the hourly day-ahead even though its energy kind is
        # "variable", so it needs spots just like a dynamic side. Asked across
        # every candidate rather than one, because the day-ahead window is
        # fetched once and shared by every row that reads it. For the
        # one-to-one page ``candidates`` is a list of one and the answer is
        # exactly what it was; for a ranking this is why the key is collected
        # once for the whole sweep rather than per row.
        compare_spot_injection = regime == SOLAR_REGIME_INJECTION and (
            _contract_has_spot_injection(current[CONF_SUPPLIER], current[CONF_CONTRACT])
            or any(
                _contract_has_spot_injection(supplier, contract)
                for supplier, contract in candidates
            )
        )
        need_spot = (
            current_kind in SPOT_PRICED_CONTRACT_KINDS
            or any(
                _contract_kind(supplier, contract) in SPOT_PRICED_CONTRACT_KINDS
                for supplier, contract in candidates
            )
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
            # Merged rather than isolated: a month mean is the same number
            # whoever asks, so whatever the entry has already cached is valid
            # input, and it is what still answers when the fetch fails.
            with _borrowed_spot_cache(coord, isolate=False):
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

        async def _spp_spot_for(
            snapshot: SupplierSnapshot | None, *, own: bool
        ) -> float | None:
            """The SPP month mean, for a card indexed on it OR, on the
            household's OWN side, an entry that opted into the weighting.

            Gating on the card flag alone silently excluded the one supplier
            the opt-in exists for: providers/custom.py never sets
            spp_indexed, because a hand-entered contract has no card to read
            it off, and the answer lives on the entry instead
            (CONF_CUSTOM_INJECTION_SPP_WEIGHTED). _spp_weighting_enabled is
            the predicate the live tick already uses for that question.

            But its custom-opt-in route reads the ENTRY and never looks at
            the snapshot, so asking it about a TARGET answered for the
            household instead of for the card. A custom monthly entry with
            the opt-in then re-priced every candidate's formula against
            Belpex_SPP, including cards that name a different index: on a
            real Eneco Power Fix shape the credit went from the card's
            printed 0,0476 to -0,02296 EUR/kWh, a sign flip worth about
            212 EUR a year at 3000 kWh exported.

            So the opt-in applies to the side it was made on, and a foreign
            card is judged only by what it prints. The custom supplier is
            never a compare target (_compare_supplier_options drops it), so
            no target can legitimately need the entry-side route.
            """
            if own:
                enabled = _spp_weighting_enabled(self.config_entry, snapshot)
            else:
                enabled = _injection_is_spp_indexed(snapshot)
            if not enabled:
                return None
            return await _spp_month_spot()

        async def _spot_for(snapshot: SupplierSnapshot | None) -> float | None:
            """The spot this side's energy shape actually bills on.

            A per-slot leg takes the mean weighted by when the household draws,
            because its bill is the sum over slots of kWh times that slot's
            rate, and consumption is evening-heavy while the day-ahead curve
            troughs at midday. A month-mean leg takes its delivery month's own
            index, which is a published number and not a shape question.
            """
            if _needs_month_mean(snapshot):
                return await _month_spot()
            return _consumption_weighted_spot(spot_dict, hour_weights) or avg_spot

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
            # The compare side is looked up leniently because the ranking
            # resolves the same household context with no single target: it
            # has a whole cell of them and never reads this dict. Keyed access
            # here would make the sweep fail on a placeholder it discards.
            "compare_supplier": _label_for_supplier(
                self._compare.get(CONF_SUPPLIER, "")
            ),
            "compare_contract": _label_for_contract(
                self._compare.get(CONF_SUPPLIER, ""),
                self._compare.get(CONF_CONTRACT, ""),
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
            "card_note": "",
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
        # Kept before any cohort splice: _compare_injection_credit has to ask
        # the RAW snapshot whether the CREDIT rides a month mean, because the
        # splice replaces the ENERGY leg only.
        raw_snapshot = coord._snapshot
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
        current_export_per_kwh: float | None = None

        async def _export_rate_for(
            snapshot: SupplierSnapshot | None, meter_type: Any, mode: str
        ) -> float | None:
            """All-in EUR/kWh weighted by when the panels EXPORT.

            Only a compensation meter needs it: it nets against the rate in
            force at the time, so the exported side has to be priced on its
            own shape rather than on the consumption one. Every other regime
            never reads it, so it is not worth the second pass.

            ``mode`` is the side's own DSO tariff mode, since a Tarif Impact
            target is billed on a configuration the household need not be on.
            """
            if regime != SOLAR_REGIME_COMPENSATION or snapshot is None:
                return None
            if inj_hour_weights is None:
                return None
            return _tou_weighted_per_kwh(
                snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                await _spot_for(snapshot),
                meter_type,
                mode,
                inj_hour_weights,
            )

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
            current_export_per_kwh = await _export_rate_for(
                current_snapshot, current_meter, dso_mode
            )
        return _HouseholdQuote(
            region=region,
            dso=dso,
            current_meter=current_meter,
            dso_mode=dso_mode,
            peak_kw=peak_kw,
            stored_regime=stored_regime,
            regime=regime,
            quote_entry=quote_entry,
            overridden=overridden,
            now_utc=now_utc,
            today_local=today_local,
            jan1=jan1,
            fee_proration=fee_proration,
            month_proration=month_proration,
            spot_dict=spot_dict,
            current_kind=current_kind,
            avg_spot=avg_spot,
            compare_spot_injection=compare_spot_injection,
            ytd_kwh=ytd_kwh,
            rolling_inj_kwh=rolling_inj_kwh,
            ytd_inj_kwh=ytd_inj_kwh,
            annual_kwh=annual_kwh,
            volumes_typed=volumes_typed,
            placeholders=placeholders,
            current_snapshot=current_snapshot,
            raw_snapshot=raw_snapshot,
            baseline_snapshot=baseline_snapshot,
            hour_weights=hour_weights,
            inj_hour_weights=inj_hour_weights,
            current_per_kwh=current_per_kwh,
            current_export_per_kwh=current_export_per_kwh,
            spot_for=_spot_for,
            spp_spot_for=_spp_spot_for,
            export_rate_for=_export_rate_for,
        )

    async def _sweep_own_row(self, hh: _HouseholdQuote) -> RankedRow | None:
        """The household's own contract, priced from the card it already has.

        Not a candidate and not re-fetched. ``_sweep_candidates`` drops it
        because a ranking is a list of alternatives, but the row still belongs
        in the table: it is what every gap is measured against, and it carries
        the signing-rate and cohort splice the household is actually billed
        on, which re-fetching it as though it were a stranger's card would
        silently discard.

        Returns None when the entry has no usable snapshot yet - a cold start
        - in which case the table ranks the alternatives against each other
        and simply has no "yours" row to point at.
        """
        current = self.config_entry.data
        if hh.current_snapshot is None or hh.current_per_kwh is None:
            return None
        label = _row_label(
            _label_for_supplier(current[CONF_SUPPLIER]),
            _label_for_contract(current[CONF_SUPPLIER], current[CONF_CONTRACT]),
        )
        try:
            annual = _annual_bill(
                hh.current_snapshot,
                hh.quote_entry,
                hh.peak_kw,
                hh.current_per_kwh,
                hh.annual_kwh,
                hh.rolling_inj_kwh,
                _compare_injection_credit(
                    hh.current_snapshot,
                    hh.quote_entry,
                    hh.spot_dict,
                    hh.avg_spot,
                    await hh.spp_spot_for(hh.current_snapshot, own=True),
                    hh.inj_hour_weights,
                    raw_snapshot=hh.raw_snapshot,
                ),
                export_per_kwh=hh.current_export_per_kwh,
                meter=hh.current_meter,
            )
        except Exception:  # noqa: BLE001 - the alternatives are still useful
            return None
        return RankedRow(label=label, annual=annual, is_own=True)

    async def _sweep_one(
        self, sweep: dict[str, Any], supplier: str, contract: str
    ) -> RankedRow:
        """Fetch and price one candidate.

        The sweep state is passed rather than held, so the same engine can
        price a dialog's sweep and a scheduled one without either owning it.
        """
        from .snapshot_store import _resolve_snapshot, fetch_shared

        label = _row_label(
            _label_for_supplier(supplier), _label_for_contract(supplier, contract)
        )
        region = sweep["region"]
        cached = _sweep_rows(self.hass, self.config_entry.entry_id, region)
        snap = cached.get((region, supplier, contract))
        if snap is None:
            # Share one listing memo across every candidate in this sweep.
            # Nine providers resolve a per-supplier listing page inside
            # fetch() and pick one product out of it, so a Flanders static
            # sweep would otherwise pull Mega's listing nine times, Engie's
            # eight and Luminus's eight - about 3 MB and 25 round trips that
            # buy nothing, spent out of a wall-clock budget that is measured
            # in rows.
            with memoise_text_fetches(sweep["listings"]):
                fetched = await fetch_shared(
                    self.hass,
                    async_get_clientsession(self.hass),
                    get_extractor(supplier),
                    contract,
                    region,
                    supplier=supplier,
                    # The sweep DOES adopt a cached card, unlike the one-off quote
                    # above: it is pricing fifty rows against a wall-clock budget,
                    # and re-downloading a card a sibling already holds is the
                    # whole cost it is trying to avoid. Still read-only, so still
                    # no negative-cache write.
                    record_failure=False,
                )
            if fetched.row is None:
                return RankedRow(
                    label=label,
                    annual=None,
                    status=fetched.error_message or "supplier unreachable",
                )
            snap = fetched.row.snapshot
            cached[(region, supplier, contract)] = snap

        hh = sweep["household"]
        kind = _contract_kind(supplier, contract)
        # The three target-side adjustments the one-to-one page makes, which a
        # ranking needs for exactly the same reasons. Left out, a sweep is a
        # second pricing path that quietly disagrees with the first.
        #
        # METER: the household's own meter is the right default, because a
        # ranking has no step to ask a what-if and the physical meter is a
        # fact. But where the target's KIND forces one, that is not an
        # override at all - it is the only meter the product is sold on, and
        # quoting a dynamic card on a mono meter routes distribution through
        # the bi-horaire split while the supplier bills energy by slot.
        meter = (
            METER_DYNAMIC if kind in SMART_METER_CONTRACT_KINDS else hh.current_meter
        )
        # DSO MODE: a Tarif Impact card carries three CWaPE band rates and no
        # mono/bi structure, so the band schedule prices its energy whatever
        # the household is on while the network leg and the Walloon terme fixe
        # follow the mode. Quoting it on the household's own mode bands the
        # energy and then bills the network off the standard columns.
        dso_mode = DSO_MODE_IMPACT if kind == "tou_impact" else hh.dso_mode
        target_entry = _quote_entry(self.config_entry, hh.regime, dso_mode)
        resolved = _resolve_snapshot(target_entry, snap)
        if hh.dso not in resolved.dsos:
            return RankedRow(
                label=label, annual=None, status=f"does not serve DSO {hh.dso}"
            )
        per_kwh = _tou_weighted_per_kwh(
            resolved,
            hh.dso,
            region,
            dt_util.as_local(hh.now_utc),
            await hh.spot_for(resolved),
            meter,
            dso_mode,
            hour_weights=hh.hour_weights,
        )
        if per_kwh is None:
            return RankedRow(label=label, annual=None, status="could not be priced")
        annual = _annual_bill(
            resolved,
            target_entry,
            hh.peak_kw,
            per_kwh,
            hh.annual_kwh,
            hh.rolling_inj_kwh,
            _compare_injection_credit(
                resolved,
                target_entry,
                hh.spot_dict,
                hh.avg_spot,
                await hh.spp_spot_for(resolved, own=False),
                hh.inj_hour_weights,
            ),
            # EXPORT RATE: under compensation the bill nets consumption
            # against injection, and each side has to be priced on its own
            # hour-of-day shape or the netting values exported kWh at the
            # hours the household draws them instead of the hours the panels
            # produce. Omitted, a compensation row came out 23% low.
            export_per_kwh=await hh.export_rate_for(resolved, meter, dso_mode),
            meter=meter,
        )
        return RankedRow(label=label, annual=annual)


class _CompareStepsMixin(OptionsFlow):
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
        current_kind = _contract_kind(current[CONF_SUPPLIER], current[CONF_CONTRACT])
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
        own_professional = _contract_is_professional(
            current[CONF_SUPPLIER], current[CONF_CONTRACT]
        )
        if user_input is not None:
            self._compare.update(user_input)
            return await self.async_step_compare_meter()
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
                # Where to go next is stored rather than hardcoded, because
                # the ranking needs the same prompt and the same live
                # validation but returns to its own sweep. Defaults to the
                # one-to-one result, so nothing about that path changes.
                nxt: Callable[[], Awaitable[ConfigFlowResult]] | None = getattr(
                    self, "_api_key_next_step", None
                )
                if nxt is not None:
                    return await nxt()
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
                "card_note": "",
                "error": "current entry is reloading; try again in a moment",
            }

        meter = self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
        hh = await self._engine._resolve_household(
            coord,
            candidates=[(self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT])],
            meter=meter,
        )
        region = hh.region
        dso = hh.dso
        current_meter = hh.current_meter
        dso_mode = hh.dso_mode
        peak_kw = hh.peak_kw
        stored_regime = hh.stored_regime
        regime = hh.regime
        quote_entry = hh.quote_entry
        overridden = hh.overridden
        now_utc = hh.now_utc
        today_local = hh.today_local
        jan1 = hh.jan1
        fee_proration = hh.fee_proration
        month_proration = hh.month_proration
        spot_dict = hh.spot_dict
        current_kind = hh.current_kind
        avg_spot = hh.avg_spot
        compare_spot_injection = hh.compare_spot_injection
        ytd_kwh = hh.ytd_kwh
        rolling_inj_kwh = hh.rolling_inj_kwh
        ytd_inj_kwh = hh.ytd_inj_kwh
        annual_kwh = hh.annual_kwh
        volumes_typed = hh.volumes_typed
        placeholders = hh.placeholders
        current_snapshot = hh.current_snapshot
        raw_snapshot = hh.raw_snapshot
        baseline_snapshot = hh.baseline_snapshot
        hour_weights = hh.hour_weights
        inj_hour_weights = hh.inj_hour_weights
        current_per_kwh = hh.current_per_kwh
        current_export_per_kwh = hh.current_export_per_kwh
        _spot_for = hh.spot_for
        _spp_spot_for = hh.spp_spot_for
        _export_rate_for = hh.export_rate_for

        # The target side. Unlike everything above, each of these is a
        # property of the ONE contract being quoted, and a ranking recomputes
        # them per row against the household context resolved once.
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER], self._compare[CONF_CONTRACT]
        )
        # A Tarif Impact product is sold only on the CWaPE incitative
        # configuration: its energy carries three band rates and no
        # mono/bi structure at all, so the band schedule prices it whatever
        # the household is on, while the network leg and the Walloon terme
        # fixe both follow the mode. Quoting the TARGET on the household's
        # own mode therefore banded its energy, billed its network off the
        # standard jour/nuit columns and charged it a fixed term the tariff
        # does not have. The install flow forces the mode for exactly this
        # reason; mirror it here, for the target only, the same way the
        # meter override applies to the target only.
        #
        # Gated on the registered kind, which deliberately leaves
        # totalenergies_impact out: it is registered "variable" and its impact
        # bands are read only in impact mode, so a household on the standard
        # configuration quoting it still bills the target's network leg on the
        # jour/nuit columns, worth about EUR 29/yr on a bi meter and EUR 113 on
        # a mono one. Not forced, for the reason _IMPACT_DEFAULT_CONTRACTS
        # gives at flow_schemas.py:514: the TE card states only that a
        # communicating digital meter is required, so a holder on the standard
        # configuration genuinely exists and forcing would under-bill them by
        # the same amount in the other direction. The install flow pre-selects
        # the mode for that card and lets the user say otherwise, which is the
        # decision this flow has no step to ask about.
        other_dso_mode = DSO_MODE_IMPACT if other_kind == "tou_impact" else dso_mode
        target_entry = _quote_entry(self.config_entry, regime, other_dso_mode)
        other_export_per_kwh: float | None = None

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
        from .snapshot_store import _resolve_snapshot, fetch_shared

        # Through the shared policy rather than extractor.fetch directly, but
        # asking for a fresh card: this is one quote the user explicitly asked
        # for, and three compare targets (Engie, Luminus, energie.be) publish
        # no probe, so adopting a cached row would quote them off a card up to
        # a day old where this page always downloaded. The other two wins of
        # going through the policy are kept: the per-key lock, so two dialogs
        # on one tuple do not both download, and the write into the shared
        # cache on success, so the coordinators can adopt what this fetched.
        #
        # record_failure=False because this is a read-only page. A background
        # tick's failure is evidence about the supplier and the negative row
        # exists so siblings back off; a dialog's failure is not, and writing
        # it here cancels a real entry's due download for five minutes and
        # inflates the counter the Repairs card is thresholded on.
        fetched = await fetch_shared(
            self.hass,
            session,
            other_extractor,
            self._compare[CONF_CONTRACT],
            region,
            supplier=self._compare[CONF_SUPPLIER],
            force=True,
            record_failure=False,
        )
        if fetched.row is None:
            # Includes the backoff arm, which carries a sibling's recent
            # failure and no exception of its own.
            placeholders["error"] = f"could not fetch quote: {fetched.error_message}"
        else:
            other_snap = _resolve_snapshot(quote_entry, fetched.row.snapshot)
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
                    other_dso_mode,
                    hour_weights,
                )
                other_export_per_kwh = await _export_rate_for(
                    other_snap, meter, other_dso_mode
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
                    await _spp_spot_for(current_snapshot, own=True),
                    inj_hour_weights,
                    raw_snapshot=raw_snapshot,
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
                    await _spp_spot_for(other_snap, own=False),
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
                export_per_kwh=current_export_per_kwh,
                meter=current_meter,
            )
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = f"{current_annual:.2f}"
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, target_entry, peak_kw, other_per_kwh, annual_kwh, rolling_inj_kwh, compare_inj_price, export_per_kwh=other_export_per_kwh, meter=meter):.2f}"
            )

        # A what-if moves BOTH sides together, so the printed supplier delta
        # barely shifts and the interesting number goes missing. Price the
        # user's own contract once more under the entry AS CONFIGURED: that
        # difference is the whole question a what-if is asking.
        #
        # It matters more now that the picker offers the user's own contract:
        # quoting that against itself with a different meter or regime makes
        # the supplier delta zero by construction, and this baseline is the
        # only line on the page that answers what the change is worth.
        baseline_annual: float | None = None
        if overridden and current_per_kwh is not None and baseline_snapshot is not None:
            baseline_inj_price = (
                _compare_injection_credit(
                    baseline_snapshot,
                    self.config_entry,
                    spot_dict,
                    avg_spot,
                    await _spp_spot_for(baseline_snapshot, own=True),
                    inj_hour_weights,
                    raw_snapshot=raw_snapshot,
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
                export_per_kwh=current_export_per_kwh,
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
        caveats: list[str] = []
        if current_snapshot is not None:
            caveats += _card_caveats(
                current_snapshot, _label_for_supplier(current[CONF_SUPPLIER])
            )
        if other_snap is not None:
            caveats += _card_caveats(
                other_snap, _label_for_supplier(self._compare[CONF_SUPPLIER])
            )
        vintage = _vintage_note(
            current_snapshot,
            _label_for_supplier(current[CONF_SUPPLIER]),
            other_snap,
            _label_for_supplier(self._compare[CONF_SUPPLIER]),
        )
        if vintage:
            caveats.append(vintage)
        placeholders["card_note"] = ("Note: " + "; ".join(caveats)) if caveats else ""
        if (
            current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and current_snapshot is not None
        ):
            delta = _annual_bill(
                other_snap,
                target_entry,
                peak_kw,
                other_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                compare_inj_price,
                export_per_kwh=other_export_per_kwh,
                meter=meter,
            ) - _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                export_per_kwh=current_export_per_kwh,
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
                current_label=_chart_labels(current, self._compare)[0],
                compare_label=_chart_labels(current, self._compare)[1],
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
        # spot_monthly is in that set for the same reason as dynamic, and it
        # is what holds archive_capable False for Energy Knights Essentia:
        # that contract DOES keep an archive now, so the fetch_for_month test
        # alone no longer excludes it and the kind test is the one doing the
        # work. Quoting it through the historical replay would need the same
        # spot cache the dynamic side needs and does not have here.
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
            # The slots go with them, or a floored feed-in formula would be
            # replayed here off the hour mean while the annual row printed
            # right above it credits each slot. Unreachable today, this
            # block needs an archive-capable pair and the only supplier
            # that floors exposes no archive, and threaded so it stays
            # unreachable rather than latent.
            hist_quarters = coord._historical_spot_quarters
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
                    # Isolated: this wants the target's own year, not whatever
                    # the entry happens to hold. Copied out before the context
                    # manager puts the entry's caches back, since it restores
                    # into the same dicts rather than rebinding them.
                    with _borrowed_spot_cache(coord, isolate=True):
                        await coord._ensure_historical_spots(
                            jan1, today_local, borrowed
                        )
                        hist_spots = dict(coord._historical_spots)
                        hist_quarters = dict(coord._historical_spot_quarters)
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
                    spot_quarters=hist_quarters,
                    billed_peak_kw=peak_kw,
                )
                compare_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    other_extractor,
                    other_snap,
                    target_entry,
                    contract_override=self._compare[CONF_CONTRACT],
                    meter_override=meter,
                    historical_spots=hist_spots,
                    spot_quarters=hist_quarters,
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
                    current_label=_chart_labels(current, self._compare)[0],
                    compare_label=_chart_labels(current, self._compare)[1],
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
            # the archive YTD path, both of which DO accrue the Flanders
            # capacity tariff, so it is kept here too and prorated the same
            # per-month way rather than by the uniform year fraction.
            current_ytd = _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                current_inj_price,
                export_per_kwh=current_export_per_kwh,
                fee_proration=fee_proration,
                prosumer_proration=month_proration,
                capacity_proration=month_proration,
                meter=current_meter,
            )
            compare_ytd = _annual_bill(
                other_snap,
                target_entry,
                peak_kw,
                other_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                compare_inj_price,
                export_per_kwh=other_export_per_kwh,
                fee_proration=fee_proration,
                prosumer_proration=month_proration,
                capacity_proration=month_proration,
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
            current_label=_chart_labels(current, self._compare)[0],
            compare_label=_chart_labels(current, self._compare)[1],
        )
        return placeholders


_YTD_FIELD = "with_ytd"


def _sweep_rows(
    hass: HomeAssistant, entry_id: str, region: str
) -> dict[tuple[str, str, str], Any]:
    """Snapshots this entry's sweep has already fetched, for the life of the
    process.

    Keyed by (supplier, contract) and holding the CARD rather than the priced
    row, so reopening after changing a household setting re-prices from what
    was already downloaded instead of re-downloading it. The expensive half of
    a sweep is the fetch and the parse; the arithmetic on top is free.

    Keyed by region as well as contract. A household that edits its region
    between two opens is asking about a different market with different DSOs,
    and a card fetched for the old one would be re-priced against the new one
    without being re-fetched.

    Deliberately not the shared snapshot cache: that one is keyed by tuple and
    shared between entries, and evicting it is the coordinator's business.
    This is scratch belonging to one dialog. It is dropped when the entry
    unloads (``evict_sweep_rows``), which is the only lifetime it needs: a
    ranking is read in one sitting, and the shared cache underneath it already
    applies the freshness rules.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    store: dict[str, dict[tuple[str, str, str], Any]] = bucket.setdefault(
        "sweep_rows", {}
    )
    return store.setdefault(entry_id, {})


def evict_sweep_rows(hass: HomeAssistant, entry_id: str) -> None:
    """Drop an entry's ranking scratch when it unloads.

    Without it the cards a sweep fetched outlive the entry that asked for
    them, for the life of the Home Assistant process.
    """
    bucket: dict[str, Any] = hass.data.get(DOMAIN, {})
    store: dict[str, Any] = bucket.get("sweep_rows", {})
    store.pop(entry_id, None)


class _SweepStepsMixin(_CompareStepsMixin):
    """The ranking page: every same-group contract in the region, sorted.

    Subclasses ``_CompareStepsMixin`` rather than sitting beside it: the
    sweep genuinely reuses its household resolution and its live-validated key
    prompt, and inheriting says so where a sibling mixin would only work
    because both happen to be mixed into the same flow.

    Its MENU ENTRY is separate, because the two answer different questions. The one-to-one page explains a single pair
    and has room to say why it crosses a kind boundary or quotes a different
    meter; a ranked table has neither, so its candidates are narrower and its
    output is one block of rows.

    The sweep is budgeted rather than timed out. A PDF parse runs in a worker
    thread and ``asyncio.wait_for`` cancels the await, not the thread, so
    nothing here can cut one short; the clock is checked BETWEEN candidates,
    which is the only place stopping is honest.
    """

    _sweep: dict[str, Any]
    _sweep_task: asyncio.Task[Any] | None = None

    async def async_step_compare_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve the cell, then hand off to the sweep."""
        sweep = self._engine.build_sweep()
        if isinstance(sweep, str):
            return self.async_abort(reason=sweep)
        self._sweep = sweep
        self._sweep_task = None
        if not hasattr(self, "_compare"):
            self._compare = {}
        return await self._sweep_start()

    async def _sweep_start(self) -> ConfigFlowResult:
        """Collect the ENTSO-E key once for the whole sweep, then begin.

        Once, not per row: on the injection regime about eight in ten static
        contracts carry a spot-indexed feed-in formula, so a per-target prompt
        would interrupt the sweep at nearly every row. The one-to-one page
        already owns the prompt and its live validation; this borrows both.
        """
        current = self.config_entry.data
        candidates = self._sweep["candidates"]
        needs_key = _effective_regime(current, {}) == SOLAR_REGIME_INJECTION and any(
            _contract_has_spot_injection(supplier, contract)
            for supplier, contract in candidates
        )
        needs_key = needs_key or any(
            _contract_kind(supplier, contract) in SPOT_PRICED_CONTRACT_KINDS
            for supplier, contract in candidates
        )
        if needs_key and not current.get(CONF_API_KEY):
            self._api_key_next_step = self.async_step_compare_all_progress
            return await self.async_step_compare_api_key()
        return await self.async_step_compare_all_progress()

    async def async_step_compare_all_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Price one candidate per task, re-showing progress between them.

        One task per candidate rather than one for the whole sweep, because
        Home Assistant only re-renders a progress step when the step returns a
        new result, and a step only returns when its task finishes. A single
        task spanning the sweep could never move the counter.
        """
        sweep = self._sweep
        if "household" not in sweep:
            from .coordinator import BePricesCoordinator

            coord = getattr(self.config_entry, "runtime_data", None)
            if not isinstance(coord, BePricesCoordinator):
                return self.async_abort(reason="compare_all_entry_reloading")
            # Once for the whole sweep. This is the half that makes a ranking
            # affordable: the meter reads, the recorder walk and the day-ahead
            # window are O(1) in the number of rows, and asking every
            # candidate up front is what lets the key be collected once.
            sweep["household"] = await self._engine._resolve_household(
                coord,
                candidates=sweep["candidates"],
                meter=self.config_entry.data.get(CONF_METER, METER_MONO),
            )
            own = await self._engine._sweep_own_row(sweep["household"])
            if own is not None:
                # Placed before the first candidate so the table has a
                # baseline from the very first render: every other row's gap
                # is measured against it, and a ranking that never shows the
                # household where it currently sits cannot answer "should I
                # switch" at all.
                sweep["rows"].append(own)
        if self._sweep_task is not None:
            if not self._sweep_task.done():
                # Re-show the SAME task. Creating a second one here is the
                # classic duplicate-task bug: the flow manager re-enters this
                # step on every frontend poll.
                return self._sweep_progress()
            task, self._sweep_task = self._sweep_task, None
            try:
                sweep["rows"].append(task.result())
            except Exception as err:  # noqa: BLE001 - one row, not the sweep
                # A row that raised is still a row: dropping it would read as
                # "not competitive". Recorded with its reason and moved past.
                supplier, contract = sweep["candidates"][sweep["index"]]
                sweep["rows"].append(
                    RankedRow(
                        label=_row_label(
                            _label_for_supplier(supplier),
                            _label_for_contract(supplier, contract),
                        ),
                        annual=None,
                        status=f"could not be priced: {err}",
                    )
                )
            sweep["index"] += 1

        nxt = self._sweep_next_index()
        if nxt is None:
            return self.async_show_progress_done(next_step_id="compare_all_result")
        # Anything skipped on the way here could not fit and is left pending.
        sweep["skipped"] = sweep.get("skipped", 0) + (nxt - sweep["index"])
        sweep["index"] = nxt
        supplier, contract = sweep["candidates"][sweep["index"]]
        self._sweep_task = self.hass.async_create_task(
            self._engine._sweep_one(sweep, supplier, contract),
            f"be_electricity_prices sweep {supplier}/{contract}",
            # Not eagerly: an eager start runs the coroutine up to its first
            # await inside the HTTP request the frontend is still waiting on.
            eager_start=False,
        )
        return self._sweep_progress()

    def _sweep_remaining_s(self) -> float:
        """Seconds of budget left, starting the clock on first call."""
        started = self._sweep.get("started_at")
        if started is None:
            self._sweep["started_at"] = dt_util.utcnow()
            return COMPARE_SWEEP_BUDGET_S
        elapsed: float = (dt_util.utcnow() - started).total_seconds()
        return COMPARE_SWEEP_BUDGET_S - elapsed

    def _sweep_next_index(self) -> int | None:
        """The next candidate that FITS in what is left, or None to stop.

        Asking only whether the budget is already spent is not enough, and
        this is what made the page look hung. Cheapest-first puts the
        expensive cards last, so the sweep would reach 110 s of a 120 s budget
        and then start a 45 s Bolt card because the budget was not YET spent -
        overrunning by most of a minute with the counter frozen on one row,
        which from the outside is indistinguishable from stuck. Measured on
        Wallonia: rows 1-37 cost 110 s together, then five TotalEnergies cards
        at 12,8 s and six Bolt at 45,3 s.

        So a candidate that cannot fit is skipped rather than started, and the
        sweep keeps taking cheaper ones behind it. Skipped rows are reported
        as still pending, exactly like the ones never reached.

        The FIRST candidate always runs whatever it costs: a household whose
        whole cell is expensive should get a row, not an empty page.
        """
        sweep = self._sweep
        remaining = self._sweep_remaining_s()
        for index in range(sweep["index"], len(sweep["candidates"])):
            supplier, _contract = sweep["candidates"][index]
            if not sweep["rows"]:
                return index
            if get_extractor(supplier).sweep_cost_s <= remaining:
                return index
        return None

    def _sweep_progress(self) -> ConfigFlowResult:
        """Re-show the running sweep, with the table so far.

        The counter alone is what made this feel hung: cheapest-first means
        the tail is the expensive cards, so it races to the high thirties and
        then sits on one row for up to 45 s with nothing moving. The rows are
        already priced by then, so show them - the table fills and reorders as
        each card lands, and a stall reads as one slow supplier rather than as
        a broken dialog.

        Home Assistant re-renders a progress step when its placeholders
        change, and they change here because each candidate is its own task;
        that is the whole reason the sweep is built one task per row.
        """
        sweep = self._sweep
        # Counted over CANDIDATES only. The household's own row rides in the
        # same list so it can be ranked and compared against, but it was never
        # fetched and counting it would report one more than the sweep did.
        priced = sum(1 for r in sweep["rows"] if r.annual is not None and not r.is_own)
        return self.async_show_progress(
            step_id="compare_all_progress",
            progress_action="sweeping",
            description_placeholders={
                "total": str(len(sweep["candidates"])),
                "priced": str(priced),
                # Deferred is not reported mid-sweep: rows behind the current
                # one are still candidates, and printing a pending count that
                # only grows reads as failure rather than as progress.
                "ranking": _ranking_table(sweep["rows"]),
            },
            progress_task=self._sweep_task,
        )

    async def async_step_compare_all_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render the ranked table; submitting closes the dialog.

        The year-to-date column is offered here rather than computed with the
        annual figures, because it is a different order of cost: every
        archived month is another fetch and parse PER CANDIDATE, and inline it
        would spend the whole budget on history and drop candidate rows. A
        ranking over an incomplete candidate set is wrong rather than
        unfinished, so the annual table is completed first and history is a
        second, deliberate pass.
        """
        if user_input is not None:
            if user_input.get(_YTD_FIELD):
                return await self.async_step_compare_all_ytd()
            return self.async_abort(reason="compare_done")
        sweep = self._sweep
        attempted = sum(1 for r in sweep["rows"] if not r.is_own)
        deferred = len(sweep["candidates"]) - attempted
        schema: dict[Any, Any] = {}
        if not sweep.get("ytd_done") and sweep["rows"]:
            schema[vol.Optional(_YTD_FIELD, default=False)] = bool
        return self.async_show_form(
            step_id="compare_all_result",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "region": sweep["region"],
                "group": sweep["group"],
                "ranking": _ranking_table(sweep["rows"], deferred=max(deferred, 0)),
            },
            last_step=True,
        )

    async def async_step_compare_all_ytd(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fill the year-to-date column, for the rows that can honestly carry one.

        A row prints a figure only when it replayed the SAME real archived
        months the household's own side did. ``fetch_for_month is not None``
        is a property of the supplier and not of the contract: seventeen
        candidates pass it and then have no month-addressable card, and the
        year-to-date walk quietly substitutes the current one for every past
        month. That prints 8,5% to 23,3% high next to a real figure, in a
        column the user can sort, with nothing to tell the two apart.

        A candidate is abandoned at the first month it cannot supply, so a
        contract with no archive costs one month rather than eight.
        """
        from .snapshot_store import _snapshot_for_month, archived_months_present
        from .ytd_cost import _compute_current_year_cost

        sweep = self._sweep
        hh = sweep["household"]
        current = self.config_entry.data
        today = hh.today_local
        months = [date(today.year, m, 1) for m in range(1, today.month + 1)]

        # The household's own side sets the standard, so it is walked first -
        # again, before anything asks about coverage. Its own snapshot is the
        # fallback the walk needs, and _compute_current_year_cost is what
        # fills every month of the cache read below.
        session = async_get_clientsession(self.hass)
        if hh.current_snapshot is not None:
            with contextlib.suppress(Exception):
                await _compute_current_year_cost(
                    self.hass,
                    session,
                    get_extractor(current[CONF_SUPPLIER]),
                    hh.current_snapshot,
                    hh.quote_entry,
                    billed_peak_kw=hh.peak_kw,
                )
        baseline = archived_months_present(
            self.hass,
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            sweep["region"],
            months,
        )
        cached = _sweep_rows(self.hass, self.config_entry.entry_id, sweep["region"])
        rows: list[RankedRow] = []
        for row in sweep["rows"]:
            pair = sweep["labels"].get(row.label)
            snap = cached.get((sweep["region"], *pair)) if pair is not None else None
            if row.annual is None or pair is None or snap is None or not baseline:
                rows.append(row)
                continue
            supplier, contract = pair
            # Spot-priced kinds are excluded for the same reason the
            # one-to-one page excludes them: the archive engine bills each
            # past hour at factor*spot+base and needs the historical spot
            # cache, which this pass does not carry. Called without it the
            # energy leg silently vanishes -- measured 33,7% low on a dynamic
            # card -- in a column the table sorts.
            if _contract_kind(supplier, contract) in SPOT_PRICED_CONTRACT_KINDS:
                rows.append(row)
                continue
            # January first, and BEFORE asking about coverage. The coverage
            # cache is only ever written by this walk, so checking it up front
            # answers "nothing is covered" for every candidate and the whole
            # pass becomes a no-op that hides its own checkbox. One month is
            # also the cheap reject: a contract with no month-addressable card
            # costs one fetch here rather than a full year of them.
            try:
                await _snapshot_for_month(
                    self.hass,
                    session,
                    get_extractor(supplier),
                    contract,
                    sweep["region"],
                    months[0],
                    snap,
                    hh.quote_entry,
                )
            except Exception:  # noqa: BLE001 - one row loses its history
                rows.append(row)
                continue
            if not archived_months_present(
                self.hass, supplier, contract, sweep["region"], months[:1]
            ):
                rows.append(row)
                continue
            try:
                value = await _compute_current_year_cost(
                    self.hass,
                    session,
                    get_extractor(supplier),
                    snap,
                    hh.quote_entry,
                    contract_override=contract,
                    billed_peak_kw=hh.peak_kw,
                )
            except Exception:  # noqa: BLE001 - one row loses its history
                rows.append(row)
                continue
            # Coverage is judged AFTER the walk, which is what filled the
            # cache. Equal to the baseline's, not merely non-empty: a row
            # replaying nine of the baseline's twelve months is not a smaller
            # figure in the same column, it is a different question answered
            # in it, and the walk quietly proxies the current card for the
            # months it could not fetch.
            covered = archived_months_present(
                self.hass, supplier, contract, sweep["region"], months
            )
            if covered != baseline:
                rows.append(row)
                continue
            rows.append(replace(row, ytd=value))
        sweep["rows"] = rows
        sweep["ytd_done"] = True
        return await self.async_step_compare_all_result()
