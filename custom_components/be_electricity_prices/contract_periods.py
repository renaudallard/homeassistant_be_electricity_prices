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
from datetime import date, datetime, timedelta
from typing import Any, NamedTuple, cast

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .cohort import ytd_window_start
from .compare_inputs import _coordinator_rlp_index_weights, _QuoteEntry
from .cohort import _tariff_card_month
from .const import (
    CONF_API_KEY,
    CONF_CONNECTION_KVA_TIER,
    CONF_CONTRACT,
    CONF_DOUBLE_FLOW_METER,
    CONF_DSO,
    CONF_PREVIOUS_CONTRACTS,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
)
from .energy_meters import (
    _bills_injection,
    _kwh_sensor_ids,
    _recorder_daily_kwh,
)
from .meter_daily import _measured_kwh
from .meter_hourly import _metered_sides
from .meter_faults import _without_today
from .flow_contracts import _contract_is_month_indexed
from .providers import effective_kind, get as get_extractor, settlement_answer
from .providers._resolve import without_welcome_credit
from .providers.base import (
    CardNotReadableError,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)
from .providers._pdf import is_transient_fetch_error
from .providers.custom import build_snapshot as build_custom_snapshot
from .snapshot_months import (
    card_for_unreadable_month,
    month_card,
    month_card_failed,
)
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
    ``read_failed`` says that stand-in is there because a read failed just now,
    the old supplier's site or an archive not answering, rather than because no
    archive kept its cards: only such a row may price on its own cards later
    in the day. ``read_by_ocr`` says the old supplier's card carries no text
    layer and the period was priced on the archive's reading of its pixels.
    """

    start: date
    end: date
    supplier: str
    contract: str
    cost: float | None
    month_cost: float | None
    stand_in: bool
    read_failed: bool = False
    read_by_ocr: bool = False

    @property
    def settled(self) -> bool:
        """Whether the pricing stands for the day: on the period's own cards,
        or on the stand-in a period no archive kept is billed on for good."""
        return self.cost is not None and not self.read_failed


@dataclass(frozen=True)
class PricedPeriods:
    """One day's pricing of the earlier contracts, and what it was priced for.

    ``key`` is ``periods_key`` of the periods priced, ``day`` the local day the
    pricing ran and ``month`` the first day of the month the ``month_cost``
    parts belong to. Kept by the coordinator and in its store, so a restart the
    same day serves it rather than fetching the old supplier's cards again.
    ``peak_kw`` is the billed capacity peak a Flemish period was priced on,
    0.0 when none is: the day's pricing stands only while it holds.
    """

    key: str
    day: date
    month: date
    rows: tuple[PricedPeriod, ...]
    peak_kw: float = 0.0


def priced_to_dict(priced: PricedPeriods) -> dict[str, Any]:
    return {
        "key": priced.key,
        "day": priced.day.isoformat(),
        "month": priced.month.isoformat(),
        "peak_kw": priced.peak_kw,
        "rows": [
            {
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "supplier": row.supplier,
                "contract": row.contract,
                "cost": row.cost,
                "month_cost": row.month_cost,
                "stand_in": row.stand_in,
                "read_failed": row.read_failed,
                "read_by_ocr": row.read_by_ocr,
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
                read_failed=bool(row.get("read_failed", False)),
                read_by_ocr=bool(row.get("read_by_ocr", False)),
            )
            for row in blob["rows"]
        )
        return PricedPeriods(
            key=str(blob["key"]),
            day=date.fromisoformat(blob["day"]),
            month=date.fromisoformat(blob["month"]),
            rows=rows,
            peak_kw=float(blob.get("peak_kw", 0.0)),
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


# Facts about the house rather than the contract that a household may only
# have entered after recording a switch: the inverter's capacity, once the
# Repairs card asked for it, a day-ahead key taken out for the new contract,
# the connection's kVA tier. An earlier contract whose copy has none takes the
# entry's. One it holds is kept, since a switch is often when the house
# changes too; the meter, its wiring and direct debit are the contract's own.
_HOUSEHOLD_BLANKS = (CONF_SOLAR_KVA, CONF_API_KEY, CONF_CONNECTION_KVA_TIER)

# Questions a copy recorded before they were asked cannot answer. Whether the
# meter counts draw and injection apart is the meter's own, so a copy that
# says no keeps it; one that does not say at all takes the entry's answer.
_UNASKED_FACTS = (CONF_DOUBLE_FLOW_METER,)


def _with_household_facts(
    settings: Mapping[str, Any], data: Mapping[str, Any]
) -> Mapping[str, Any]:
    """``settings`` with each of ``_HOUSEHOLD_BLANKS`` it left blank, and each
    of ``_UNASKED_FACTS`` it does not hold, filled from the entry's
    ``data``."""
    filled = {
        key: data[key]
        for key in _HOUSEHOLD_BLANKS
        if not settings.get(key) and data.get(key)
    }
    filled.update(_unasked_facts(settings, data))
    return {**settings, **filled} if filled else settings


def _unasked_facts(
    settings: Mapping[str, Any], data: Mapping[str, Any]
) -> dict[str, Any]:
    """Each of ``_UNASKED_FACTS`` that ``settings`` does not hold, from the
    entry's ``data``: a copy kept before the question existed has no answer
    of its own, and the entry's is the household's."""
    return {
        key: data[key] for key in _UNASKED_FACTS if key not in settings and key in data
    }


class SpotCaches(NamedTuple):
    """Day-ahead by clock hour, and by quarter where an entry keeps them."""

    hours: Mapping[datetime, float] | None
    quarters: Mapping[datetime, list[float]] | None


def previous_periods(
    data: Mapping[str, Any], window_start: date, today: date
) -> list[ContractPeriod]:
    """The earlier contracts' days inside ``[window_start, today]``, oldest first.

    Each runs from the day the one before it ended, or the window's first day,
    or its own start date when it billed the year from there, to the day before
    its successor started, and carries the settings kept with it, blanks in
    the household's facts filled from the entry (``_HOUSEHOLD_BLANKS``, and
    ``_UNASKED_FACTS`` where the copy predates the question). A contract that ended before the
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
            periods.append(
                ContractPeriod(begins, last, _with_household_facts(settings, data))
            )
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
            # Priced on the archive's OCR reading of a card published as
            # page images, which no parser here can read.
            "card_read_by_ocr": row.read_by_ocr,
        }
        for row in priced.rows
    )


def keep_settled(
    previous: PricedPeriods | None, key: str, rows: list[PricedPeriod]
) -> tuple[PricedPeriod, ...]:
    """``rows``, keeping the last pricing's row for a period this one could
    not price on its own supplier's cards.

    A timeout on the old supplier's site priced the period on another
    supplier's card, and that replaced the pricing on its own cards for the
    day and in the store. The window is closed, so the last pricing on its
    own cards is the better figure whatever its age, and a settled stand-in
    beats a read that failed.
    """
    if previous is None or previous.key != key or len(previous.rows) != len(rows):
        return tuple(rows)
    return tuple(
        old
        if old.settled and (not new.settled or (new.stand_in and not old.stand_in))
        else new
        for old, new in zip(previous.rows, rows, strict=True)
    )


async def previous_meter_faults(
    hass: HomeAssistant, entry: ConfigEntry, today: date
) -> list[str]:
    """The meters of the earlier contracts that do not report over their own
    days, for the register Repairs card, one entry per contract.

    Each earlier contract keeps the wiring it had, because after an
    integration swap the old sensors hold the old contract's history. A
    rename moves that history to the new sensor instead, and the old contract
    was then billed its fees alone with nothing raised, since the checks ran
    on the entry's wiring only. The same ones run here over each contract's
    own days. A consumption meter that recorded nothing over a whole closed
    window is named too, where on the running contract that may only be a
    meter wired today, unless its statistics begin after that window closed,
    and so is a feed-in meter, as the silent side, on the same condition.
    """
    faults: list[str] = []
    for period in previous_periods(entry.data, ytd_window_start(entry, today), today):
        proxy = cast(ConfigEntry, _QuoteEntry(data=dict(period.data)))
        found: list[str | None] = []
        # A fault a totals sensor covers leaves that contract's bill whole.
        used = await _measured_kwh(hass, proxy, period.start, period.end)
        found.append(None if used.covered else used.pair_fault)
        younger: set[str] = set()
        if not used.days_with_data and not used.pair_fault:
            # Every consumption sensor recorded nothing over the contract's
            # days; a pair with one dead half is already named by its fault
            # alone.
            silent, added = await _added_since(
                hass, proxy, "consumption", period, today
            )
            found += silent
            younger |= added
        if _bills_injection(proxy):
            injected = await _measured_kwh(
                hass, proxy, period.start, period.end, side="injection"
            )
            found.append(None if injected.covered else injected.pair_fault)
            if not injected.days_with_data and not injected.pair_fault:
                # A feed-in meter that recorded nothing is named as the silent
                # side below, unless the panels came after the contract ended.
                _silent, added = await _added_since(
                    hass, proxy, "injection", period, today
                )
                younger |= added
        sides = await _metered_sides(hass, proxy, period.start, period.end)
        if sides is not None:
            # The comparison names that same younger meter as the silent side,
            # and the card then stayed up until the earlier contract left the
            # year.
            found += (name for name in sides.silent if name not in younger)
        # Named once each: a pair fault lists its sensors comma-separated.
        names = dict.fromkeys(
            name for fault in found if fault for name in fault.split(", ")
        )
        if names:
            faults.append(
                f"{', '.join(names)} ({period.data.get(CONF_SUPPLIER)},"
                f" {period.start} to {period.end})"
            )
    return faults


async def _added_since(
    hass: HomeAssistant,
    entry: ConfigEntry,
    side: str,
    period: ContractPeriod,
    today: date,
) -> tuple[list[str], set[str]]:
    """The sensors ``side`` is billed off that recorded nothing over
    ``period``, split into those with no statistics since either and those
    whose statistics begin after the period ended.

    A meter added to Home Assistant since holds no history of those days, and
    no rewiring the card advises can bill them.
    """
    day_id, night_id, total_id = _kwh_sensor_ids(entry, side)
    after = period.end + timedelta(days=1)
    silent: list[str] = []
    added: set[str] = set()
    for entity_id in (day_id, night_id) if day_id and night_id else (total_id,):
        if not entity_id:
            continue
        if _without_today(
            await _recorder_daily_kwh(hass, entity_id, after, today), today
        ):
            added.add(entity_id)
        else:
            silent.append(entity_id)
    return silent, added


def _kind(period: ContractPeriod) -> str:
    return effective_kind(
        period.data.get(CONF_SUPPLIER),
        period.data.get(CONF_CONTRACT),
        quarter_hourly=settlement_answer(period.data),
    )


def _reprices_on_spots(period: ContractPeriod) -> bool:
    """Whether the walk re-prices a variable contract's energy off the day-ahead.

    Two ways, and only with an ENTSO-E key in the settings, which is the walk's
    own guard (``cohort_legs._month_indexed_leg`` and the variable cohort in
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
) -> tuple[SupplierSnapshot | None, bool, bool]:
    """The old contract's card as its supplier publishes it today, resolved for
    this household, or ``None`` when the supplier cannot be reached, whether it
    was read off the archive's OCR reading, and whether a ``None`` is a read
    that failed just now (a timeout, a 5xx) rather than a card that is gone.

    A card published as page images downloads fine and no parser here can read
    it: Ecofix's, every day since August 2026. The repository's archive reads
    those off their pixels, and the live tick and the compare page price such
    a card on that row (``card_for_unreadable_month``), so an earlier contract
    on one is priced on it too rather than on another supplier's card."""
    data = proxy.data
    region = data.get(CONF_REGION, "")
    contract = data.get(CONF_CONTRACT, "")
    read_by_ocr = False
    read_failed = False
    if extractor.id == SUPPLIER_CUSTOM:
        raw: SupplierSnapshot | None = build_custom_snapshot(
            data, region, data.get(CONF_DSO, "")
        )
    else:
        fetched = await fetch_shared(
            hass,
            session,
            extractor,
            contract,
            region,
            supplier=extractor.id,
            record_failure=False,
        )
        raw = fetched.row.snapshot if fetched.row is not None else None
        # Classified as the coordinator classifies its own card's fetch.
        read_failed = isinstance(
            fetched.error, TimeoutError
        ) or is_transient_fetch_error(fetched.error_message)
        if raw is None and isinstance(fetched.error, CardNotReadableError):
            try:
                archived = await card_for_unreadable_month(
                    session, extractor.id, contract, region, dt_util.now().date(), proxy
                )
            except Exception as err:  # noqa: BLE001 - a blip on the archive is not this period's problem
                _LOGGER.debug(
                    "card archive read failed for %s/%s: %s",
                    extractor.id,
                    contract,
                    err,
                )
                archived = None
                read_failed = True
            if archived is not None:
                raw = archived.snapshot
                read_by_ocr = archived.read_by_ocr
    if raw is None:
        return None, False, read_failed
    return (
        _resolve_snapshot(proxy, raw, annual_kwh=entry_annual_kwh(proxy)),
        read_by_ocr,
        False,
    )


async def _latest_archived_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    proxy: ConfigEntry,
    period: ContractPeriod,
) -> tuple[SupplierSnapshot | None, bool]:
    """The newest card an archive holds for the old contract inside its days,
    and, when there is none, whether a month's read failed just now.

    For a supplier that has left the market and no longer publishes one: DATS
    24's cards answer 404 since September 2026, and the project's archive keeps
    the months it captured. ``month_card`` answers None for a month no archive
    holds and for a month whose read failed, which ``month_card_failed`` tells
    apart.
    """
    data = proxy.data
    contract = data.get(CONF_CONTRACT, "")
    region = data.get(CONF_REGION, "")
    month = date(period.end.year, period.end.month, 1)
    first = date(period.start.year, period.start.month, 1)
    failed = False
    while month >= first:
        card = await month_card(
            hass, session, extractor, contract, region, month, proxy
        )
        if card is not None:
            return card, False
        failed = failed or month_card_failed(
            hass, extractor.id, contract, region, month
        )
        month = (month - timedelta(days=1)).replace(day=1)
    return None, failed


async def period_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    period: ContractPeriod,
    overrides: Mapping[str, Any] | None = None,
) -> tuple[ConfigEntry, SupplierExtractor, SupplierSnapshot | None, bool, bool, bool]:
    """The stand-in entry, extractor and card an earlier contract is priced on,
    whether that card is the entry's own standing in, whether it stands in
    because a read failed just now (``PricedPeriod.read_failed``), and whether
    it was read off the archive's OCR reading.

    The card is the one its supplier publishes today, resolved for this
    household, and each month still bills on its own archived card through the
    walk: this one only stands in for a month no archive holds, exactly as the
    entry's current card does for its own contract. A card published as page
    images is the archive's reading of it (``_current_card``). A supplier that
    no longer publishes one falls back to the newest card an archive kept
    inside the period, and one with neither to the entry's current card, the
    last resort. ``None`` only when the entry has no card of its own either.

    A stand-in prices the days and nothing the card offers a new customer: it
    is walked with the old contract's start date, so a welcome credit left on
    it was credited to a contract that never signed that card, 259 EUR of Mega
    ristourne on a DATS 24 year.

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
    card, read_by_ocr, read_failed = await _current_card(
        hass, session, extractor, proxy
    )
    if card is None and fallback is not None:
        card, archive_failed = await _latest_archived_card(
            hass, session, extractor, proxy, period
        )
        read_failed = read_failed or archive_failed
    if card is None:
        stand_in = None if fallback is None else without_welcome_credit(fallback)
        return proxy, extractor, stand_in, True, read_failed, False
    return proxy, extractor, card, False, False, read_by_ocr


async def price_previous_periods(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    coordinator: Any,
    periods: list[ContractPeriod],
    *,
    month_start: date,
    overrides: Mapping[str, Any] | None = None,
    load_profiles: bool = False,
    spots: SpotCaches | None = None,
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

    ``spots`` stands in for the coordinator's day-ahead, hours and quarters,
    when the compare page fetched its own for the window: the earlier
    contracts are then priced on the same spots as the current one.
    """
    if spots is None:
        spots = SpotCaches(
            getattr(coordinator, "_historical_spots", None),
            getattr(coordinator, "_historical_spot_quarters", None),
        )
    out: list[PricedPeriod] = []
    for period in periods:
        supplier = str(period.data.get(CONF_SUPPLIER, ""))
        contract = str(period.data.get(CONF_CONTRACT, ""))
        cost: float | None = None
        month_cost: float | None = None
        stand_in = False
        read_failed = False
        read_by_ocr = False
        try:
            (
                proxy,
                extractor,
                card,
                stand_in,
                read_failed,
                read_by_ocr,
            ) = await period_card(hass, session, coordinator, period, overrides)
            if card is not None:
                regime = proxy.data.get(CONF_SOLAR_REGIME, "none")
                allocating = regime == SOLAR_REGIME_COMPENSATION
                # Only with spots to weight, as the tick asks for its own.
                if (
                    load_profiles
                    and spots.hours
                    and _spp_weighting_enabled(proxy, card)
                ):
                    await coordinator._ensure_spp_weights()
                rlp = getattr(coordinator, "_rlp_weights", None) or None
                inputs: dict[str, Any] = {
                    "historical_spots": spots.hours,
                    "spot_quarters": spots.quarters,
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
                read_failed=read_failed,
                read_by_ocr=read_by_ocr,
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
    spots: SpotCaches | None = None,
) -> float | None:
    """The household's own year on the compare page, with any earlier contract.

    ``own`` is the entry's current contract priced from the day it started
    (``current_period_start``), and the contracts held before it in the window
    are added so the figure reads what the ``current_year_cost`` sensor beside
    it reads. Served from the coordinator's daily pricing while the page quotes
    the household as it is. A what-if the page quotes both sides on, a solar
    regime or a DSO tariff mode, has to reach the earlier contracts as well, so
    those are priced again under it. So are they when the page fetched its own
    day-ahead for the window (``spots``), on those spots, or the earlier
    contracts would be priced on the entry's cache and the current one on the
    page's. ``None`` when one cannot be priced.
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
    if not overrides and spots is None:
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
        spots=spots,
    )
    costs = [row.cost for row in rows]
    if any(cost is None for cost in costs):
        return None
    return own + sum(cost for cost in costs if cost is not None)
