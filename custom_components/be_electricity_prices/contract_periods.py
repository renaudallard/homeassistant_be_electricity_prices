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

"""The contracts a household held earlier in the year, and what they cost.

Changing supplier during the year means two bills for it: the old supplier's
up to the switch and the new one's from it. An entry prices one contract at a
time, so the switch is recorded on it from the options menu, as a copy of the
settings as they stood and the first day the next contract supplied
(``CONF_PREVIOUS_CONTRACTS``). ``current_year_cost`` then prices each earlier
contract on its own supplier's cards for its own days, through the same walk
the entry's own contract goes through, closed on the day before the switch.

One window per contract is also what the Walloon compensation rule asks for:
a change of supplier splits the year and each part nets its own injection
(CWaPE communication CD-14d03, section 5.1.2).
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .cohort import ytd_window_start
from .compare_inputs import _coordinator_rlp_index_weights, _QuoteEntry
from .cohort import _tariff_card_month
from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_DSO,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
)
from .flow_contracts import _contract_is_month_indexed
from .providers import effective_kind, get as get_extractor, settlement_answer
from .providers.base import ExtractorError, SupplierExtractor, SupplierSnapshot
from .providers.custom import build_snapshot as build_custom_snapshot
from .snapshot_months import _snapshot_for_month
from .snapshot_resolve import _resolve_snapshot, entry_annual_kwh
from .snapshot_store import fetch_shared
from .spot_stats import _energy_is_rlp_indexed, _spp_weighting_enabled
from .ytd_cost import _compute_current_year_cost

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContractPeriod:
    """An earlier contract's days inside a window, both ends included."""

    start: date
    end: date
    data: Mapping[str, Any]


@dataclass(frozen=True)
class PricedPeriod:
    """What one earlier contract cost over its days.

    ``month_cost`` is the part inside the month that was asked about, for a
    contract that ended in it, and ``None`` for one that ended before. ``stand_in``
    says the supplier could not be reached and no archive held any of its cards,
    so the period was priced on the entry's current card instead, which is what
    the whole year was priced on before switches could be recorded.
    """

    start: date
    end: date
    supplier: str
    contract: str
    cost: float | None
    month_cost: float | None
    stand_in: bool


@dataclass(frozen=True)
class PricedPeriods:
    """One day's pricing of the earlier contracts, and what it was priced for.

    ``key`` is ``periods_key`` of the periods priced, ``day`` the local day the
    pricing ran and ``month`` the first day of the month the ``month_cost``
    parts belong to. Kept by the coordinator and in its store, so a restart the
    same day serves it rather than fetching the old supplier's cards again.
    """

    key: str
    day: date
    month: date
    rows: tuple[PricedPeriod, ...]


def priced_to_dict(priced: PricedPeriods) -> dict[str, Any]:
    return {
        "key": priced.key,
        "day": priced.day.isoformat(),
        "month": priced.month.isoformat(),
        "rows": [
            {
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "supplier": row.supplier,
                "contract": row.contract,
                "cost": row.cost,
                "month_cost": row.month_cost,
                "stand_in": row.stand_in,
            }
            for row in priced.rows
        ],
    }


def priced_from_dict(blob: Mapping[str, Any]) -> PricedPeriods | None:
    """The stored pricing, or ``None`` for a blob that does not read back whole."""
    try:
        rows = tuple(
            PricedPeriod(
                start=date.fromisoformat(row["start"]),
                end=date.fromisoformat(row["end"]),
                supplier=str(row["supplier"]),
                contract=str(row["contract"]),
                cost=None if row["cost"] is None else float(row["cost"]),
                month_cost=(
                    None if row["month_cost"] is None else float(row["month_cost"])
                ),
                stand_in=bool(row["stand_in"]),
            )
            for row in blob["rows"]
        )
        return PricedPeriods(
            key=str(blob["key"]),
            day=date.fromisoformat(blob["day"]),
            month=date.fromisoformat(blob["month"]),
            rows=rows,
        )
    except (KeyError, TypeError, ValueError):
        return None


def recorded_contracts(data: Mapping[str, Any]) -> list[tuple[date, Mapping[str, Any]]]:
    """The recorded switches, oldest first: the next contract's first day and
    the settings the household held until then. A malformed record is skipped
    rather than trusted."""
    out: list[tuple[date, Mapping[str, Any]]] = []
    for record in data.get(CONF_PREVIOUS_CONTRACTS) or ():
        if not isinstance(record, Mapping):
            continue
        settings = record.get("data")
        if not isinstance(settings, Mapping) or not settings.get(CONF_SUPPLIER):
            continue
        try:
            until = date.fromisoformat(str(record.get("until")))
        except ValueError:
            continue
        out.append((until, settings))
    out.sort(key=lambda record: record[0])
    return out


def previous_periods(
    data: Mapping[str, Any], window_start: date, today: date
) -> list[ContractPeriod]:
    """The earlier contracts' days inside ``[window_start, today]``, oldest first.

    Each runs from the day the one before it ended, or the window's first day,
    or its own start date when it billed the year from there, to the day before
    its successor started. A contract that ended before the
    window opens has no days in it: last year's switches, and every switch on
    an entry billing the year from its current contract's start date, which is
    how that option keeps meaning "this contract only".
    """
    periods: list[ContractPeriod] = []
    first = window_start
    for until, settings in recorded_contracts(data):
        # A contract that billed its year from its own start date keeps doing
        # so. Recording the switch unticks the box on the entry, which then
        # describes the new contract, and the days before the first contract
        # began belong to no contract at all.
        begins = max(
            first, ytd_window_start(cast(ConfigEntry, _QuoteEntry(settings)), today)
        )
        last = min(until - timedelta(days=1), today)
        if last >= begins:
            periods.append(ContractPeriod(begins, last, settings))
        first = max(first, until)
    return periods


def current_period_start(data: Mapping[str, Any], window_start: date) -> date:
    """The first day the entry's own contract bills inside the window."""
    records = recorded_contracts(data)
    return max(window_start, records[-1][0]) if records else window_start


def billed_from(data: Mapping[str, Any], window_start: date, today: date) -> date:
    """The first day any contract bills inside ``[window_start, today]``.

    ``window_start`` itself, unless the first earlier contract billed its year
    from its own start date: recording the switch unticks that box on the
    entry, whose window then opens on 1 January, while the old contract's days
    still begin on its start date. A comparison measured from 1 January against
    a year billed from March set the quoted side's months before March against
    nothing.
    """
    periods = previous_periods(data, window_start, today)
    return periods[0].start if periods else current_period_start(data, window_start)


def periods_key(periods: list[ContractPeriod]) -> str:
    """What a priced result was priced for: every period's days and settings.

    Recording another switch, or a year rolling over, changes it, and a result
    stored under another key is never served for these periods.
    """
    blob = json.dumps(
        [[p.start.isoformat(), p.end.isoformat(), dict(p.data)] for p in periods],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def previous_costs(
    priced: PricedPeriods | None, periods: list[ContractPeriod], month_start: date
) -> tuple[float | None, float | None]:
    """The earlier contracts' share of the year and of the running month.

    ``(0.0, 0.0)`` with no earlier contract in the window. The year's share is
    ``None`` while no pricing matches these periods, or when one of them could
    not be priced: the figure is then unknown rather than short by a whole
    contract, which on the recorder would read as a large negative change and
    then a positive one. The month's share waits on the pricing only in the
    month of a switch, the one month an earlier contract reaches into; in any
    other it is zero whatever the pricing says.
    """
    if not periods:
        return 0.0, 0.0
    matched = priced is not None and priced.key == periods_key(periods)
    rows = priced.rows if priced is not None and matched else ()
    year: float | None = None
    if matched:
        costs = [row.cost for row in rows]
        if all(cost is not None for cost in costs):
            year = sum(cost for cost in costs if cost is not None)
    if all(period.end < month_start for period in periods):
        return year, 0.0
    if not matched or priced is None or priced.month != month_start:
        return year, None
    parts = [row.month_cost for row in rows if row.end >= month_start]
    if any(part is None for part in parts):
        return year, None
    return year, sum(part for part in parts if part is not None)


def previous_rows(
    priced: PricedPeriods | None, periods: list[ContractPeriod]
) -> tuple[dict[str, Any], ...]:
    """The earlier contracts as the ``current_year_cost`` sensor lists them."""
    if not periods or priced is None or priced.key != periods_key(periods):
        return ()
    return tuple(
        {
            "supplier": row.supplier,
            "contract": row.contract,
            "from": row.start.isoformat(),
            "to": row.end.isoformat(),
            "cost_eur": None if row.cost is None else round(row.cost, 2),
            # Priced on the entry's current card: the old supplier could not be
            # reached and no archive held any of its cards for those days.
            "priced_on_current_card": row.stand_in,
        }
        for row in priced.rows
    )


def _kind(period: ContractPeriod) -> str:
    return effective_kind(
        period.data.get(CONF_SUPPLIER),
        period.data.get(CONF_CONTRACT),
        quarter_hourly=settlement_answer(period.data),
    )


def _reprices_on_spots(period: ContractPeriod) -> bool:
    """Whether the walk re-prices a variable contract's energy off the day-ahead.

    Two ways, and only with an ENTSO-E key in the settings, which is the walk's
    own guard (``cohort._month_indexed_leg`` and the variable cohort in
    ``cohort._cohort_legs``): a card indexed on the delivery month's mean, which
    the registry flags ``month_indexed_energy`` and mostly registers as variable,
    and a signing cohort re-priced off its archived variable card's formula.
    """
    data = period.data
    if not data.get(CONF_API_KEY):
        return False
    if _contract_is_month_indexed(data.get(CONF_SUPPLIER), data.get(CONF_CONTRACT)):
        return True
    return (
        _kind(period) == "variable"
        and _tariff_card_month(cast(ConfigEntry, _QuoteEntry(data=dict(data))))
        is not None
    )


def periods_need_spots(periods: list[ContractPeriod]) -> bool:
    """Whether an earlier contract bills off the day-ahead the entry's own may not.

    Asked of the registry rather than of the card, because the tick decides on
    the spots before anything has priced an old contract. A dynamic or monthly
    spot contract needs them for its energy, a variable one whenever the walk
    re-prices it on the month's mean, and a feed-in credit may settle on them
    whatever the energy does, so an earlier contract on the injection regime
    asks for them too. The spots are the Belgian day-ahead, the same for every
    supplier, so this only ever fetches what the year already holds.
    """
    return any(
        _kind(p) in SPOT_PRICED_CONTRACT_KINDS
        or _reprices_on_spots(p)
        or p.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
        for p in periods
    )


def periods_need_rlp(periods: list[ContractPeriod]) -> bool:
    """Whether an earlier contract wants the residential load profile: a card
    re-priced on the month's mean may settle on its weighted mean, and a
    compensation net is spread over the profile.

    Whether a variable card weights its mean is on the card, not in the
    registry (Mega's flex cards, TotalEnergies' variables and Eneco Flex One
    do, Engie's EPEXDAM cards do not), so every card the walk re-prices asks.
    The profile is one download shared by every entry."""
    return any(
        _kind(p) == "spot_monthly"
        or _reprices_on_spots(p)
        or p.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_COMPENSATION
        for p in periods
    )


async def _current_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    proxy: ConfigEntry,
) -> SupplierSnapshot | None:
    """The old contract's card as its supplier publishes it today, resolved for
    this household, or ``None`` when the supplier cannot be reached."""
    data = proxy.data
    region = data.get(CONF_REGION, "")
    if extractor.id == SUPPLIER_CUSTOM:
        raw: SupplierSnapshot | None = build_custom_snapshot(
            data, region, data.get(CONF_DSO, "")
        )
    else:
        fetched = await fetch_shared(
            hass,
            session,
            extractor,
            data.get(CONF_CONTRACT, ""),
            region,
            supplier=extractor.id,
            record_failure=False,
        )
        raw = fetched.row.snapshot if fetched.row is not None else None
    if raw is None:
        return None
    return _resolve_snapshot(proxy, raw, annual_kwh=entry_annual_kwh(proxy))


async def _latest_archived_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    proxy: ConfigEntry,
    period: ContractPeriod,
    fallback: SupplierSnapshot,
) -> SupplierSnapshot | None:
    """The newest card an archive holds for the old contract inside its days.

    For a supplier that has left the market and no longer publishes one: DATS
    24's cards answer 404 since September 2026, and the project's archive keeps
    the months it captured. ``_snapshot_for_month`` hands back the fallback it
    is given, the very object, for a month no archive holds, which is how a
    real card is told from none.
    """
    data = proxy.data
    month = date(period.end.year, period.end.month, 1)
    first = date(period.start.year, period.start.month, 1)
    while month >= first:
        card = await _snapshot_for_month(
            hass,
            session,
            extractor,
            data.get(CONF_CONTRACT, ""),
            data.get(CONF_REGION, ""),
            month,
            fallback,
            proxy,
        )
        if card is not fallback:
            return card
        month = (month - timedelta(days=1)).replace(day=1)
    return None


async def period_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    period: ContractPeriod,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[ConfigEntry, SupplierExtractor, SupplierSnapshot | None, bool]:
    """The stand-in entry, extractor and card an earlier contract is priced on.

    The card is the one its supplier publishes today, resolved for this
    household, and each month still bills on its own archived card through the
    walk: this one only stands in for a month no archive holds, exactly as the
    entry's current card does for its own contract. A supplier that no longer
    publishes one falls back to the newest card an archive kept inside the
    period, and one with neither to the entry's current card, which the last
    element flags. ``None`` only when the entry has no card of its own either.

    Raises ``ExtractorError`` for a supplier the registry no longer knows.
    """
    proxy = cast(
        ConfigEntry,
        _QuoteEntry(
            data={**period.data, **(overrides or {})}, runtime_data=coordinator
        ),
    )
    extractor = get_extractor(str(period.data.get(CONF_SUPPLIER, "")))
    fallback = getattr(coordinator, "_snapshot", None)
    card = await _current_card(hass, session, extractor, proxy)
    if card is None and fallback is not None:
        card = await _latest_archived_card(
            hass, session, extractor, proxy, period, fallback
        )
    if card is None:
        return proxy, extractor, fallback, True
    return proxy, extractor, card, False


async def price_previous_periods(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    periods: list[ContractPeriod],
    *,
    month_start: date,
    overrides: Mapping[str, Any] | None = None,
    load_profiles: bool = False,
) -> list[PricedPeriod]:
    """Price every earlier contract over its own days, on its own cards.

    Each period is one ``_compute_current_year_cost`` window, closed on the day
    before the switch, so it bills exactly as the entry's own contract does:
    each month on that month's archived card, fees prorated over the days held,
    feed-in credited on the contract's own terms and a compensation net settled
    within the period. The household's own inputs come from ``coordinator``:
    the year's day-ahead spots, the load and solar profiles and the Flemish
    capacity peak, none of which depend on the supplier.

    ``overrides`` re-prices the periods under a what-if the compare page quotes
    both sides on, a solar regime or a DSO tariff mode. Network access is
    expected, so neither the tick nor setup calls this: the coordinator runs it
    in the background once a day and keeps the result.

    ``load_profiles`` is that daily run. A card whose feed-in settles on the
    month's Belpex_SPP is only known once fetched, so the solar profile is
    loaded here for it, as the backfill does for the same days; otherwise the
    walk credits the card's printed forecast. The compare dialog leaves it
    off, and reads the profile the daily run left behind.
    """
    out: list[PricedPeriod] = []
    for period in periods:
        supplier = str(period.data.get(CONF_SUPPLIER, ""))
        contract = str(period.data.get(CONF_CONTRACT, ""))
        cost: float | None = None
        month_cost: float | None = None
        stand_in = False
        try:
            proxy, extractor, card, stand_in = await period_card(
                hass, session, coordinator, period, overrides
            )
            if card is not None:
                regime = proxy.data.get(CONF_SOLAR_REGIME, "none")
                allocating = regime == SOLAR_REGIME_COMPENSATION
                # Only with spots to weight, as the tick asks for its own.
                if (
                    load_profiles
                    and getattr(coordinator, "_historical_spots", None)
                    and _spp_weighting_enabled(proxy, card)
                ):
                    await coordinator._ensure_spp_weights()
                rlp = getattr(coordinator, "_rlp_weights", None) or None
                inputs: dict[str, Any] = {
                    "historical_spots": getattr(coordinator, "_historical_spots", None),
                    "spot_quarters": getattr(
                        coordinator, "_historical_spot_quarters", None
                    ),
                    "spp_weights": getattr(coordinator, "_spp_weights", None) or None,
                    "rlp_weights": (
                        rlp
                        if _energy_is_rlp_indexed(card.energy) or allocating
                        else None
                    ),
                    "rlp_index_weights": _coordinator_rlp_index_weights(
                        coordinator.entry, card
                    ),
                    "billed_peak_kw": (
                        coordinator._billed_peak_kw()
                        if proxy.data.get(CONF_REGION) == REGION_FLANDERS
                        else 0.0
                    ),
                }
                cost = await _compute_current_year_cost(
                    hass,
                    session,
                    extractor,
                    card,
                    proxy,
                    window_start_override=period.start,
                    window_end=period.end,
                    **inputs,
                )
                if period.end >= month_start:
                    month_cost = await _compute_current_year_cost(
                        hass,
                        session,
                        extractor,
                        card,
                        proxy,
                        window_start_override=max(period.start, month_start),
                        window_end=period.end,
                        **inputs,
                    )
        except (ExtractorError, KeyError, ValueError) as err:
            # A supplier the registry no longer knows, or a card that cannot
            # price the household's DSO. Reported, not raised: the entry's own
            # contract still prices, and the sensor says which period is missing.
            _LOGGER.warning(
                "Could not price the %s contract held from %s to %s: %s",
                supplier,
                period.start,
                period.end,
                err,
            )
            cost = None
            month_cost = None
        out.append(
            PricedPeriod(
                start=period.start,
                end=period.end,
                supplier=supplier,
                contract=contract,
                cost=cost,
                month_cost=month_cost,
                stand_in=stand_in,
            )
        )
    return out


async def with_previous_contracts(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    entry: ConfigEntry,
    quote_entry: ConfigEntry,
    own: float | None,
    *,
    window_start: date,
    today: date,
) -> float | None:
    """The household's own year on the compare page, with any earlier contract.

    ``own`` is the entry's current contract priced from the day it started
    (``current_period_start``), and the contracts held before it in the window
    are added so the figure reads what the ``current_year_cost`` sensor beside
    it reads. Served from the coordinator's daily pricing while the page quotes
    the household as it is. A what-if the page quotes both sides on, a solar
    regime or a DSO tariff mode, has to reach the earlier contracts as well, so
    those are priced again under it. ``None`` when one cannot be priced.
    """
    periods = previous_periods(entry.data, window_start, today)
    if own is None or not periods:
        return own
    overrides = {
        key: value
        for key, value in quote_entry.data.items()
        if entry.data.get(key) != value
    }
    month_start = today.replace(day=1)
    if not overrides:
        year, _month = previous_costs(
            getattr(coordinator, "_previous_priced", None), periods, month_start
        )
        if year is not None:
            return own + year
    rows = await price_previous_periods(
        hass,
        session,
        coordinator,
        periods,
        month_start=month_start,
        overrides=overrides,
    )
    costs = [row.cost for row in rows]
    if any(cost is None for cost in costs):
        return None
    return own + sum(cost for cost in costs if cost is not None)
