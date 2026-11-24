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

"""Hold the unit prices of an Eneco bill against this entry's own.

The eneco_be integration reads a meter's newest settlement bill and publishes
its lines on a last bill sensor: what was billed, over which days, at what
unit price before VAT. Each line is priced again here for the same days, on
the card or cohort this entry bills each of those months on, with the
household's own settings and the card archive for a month the supplier no
longer serves. Nothing comes from the recorder, whose history is younger than
most bills.

A line is matched on the line type the bill hides behind it where it carries
one, else on its printed label, and anything else is left out rather than
guessed. Where the entry bills the line's dates at more than one rate, each
rate is held to the bill: the line is off only when all of them are beyond
the bill's rounding on the same side, ok when all are within it, and left out
otherwise, since only a split by volume could tell more and the bill's
monthly volumes are profiled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started
from homeassistant.util import dt as dt_util

from .cohort import _effective_snapshot_for_month
from .const import (
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_ENECO_BILL,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_IMPACT,
    DSO_MODE_SIMPLE,
    METER_BI,
    METER_DYNAMIC,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_WALLONIA,
    SUPPLIER_ENECO,
)
from .contract_periods import current_period_start
from .coordinator import BePricesCoordinator, supplier_device_info
from .fees import _walloon_fixed_term_applies
from .markdown_text import escape_markdown
from .pricing import (
    StaticBand,
    renewables_eur_per_kwh,
    static_energy_eur_per_kwh,
    taxes_vat_exempt_eur_per_kwh,
    yearly_fixed_fee_for_meter,
)
from .providers import get as get_extractor
from .providers._resolve import card_residential_vat
from .providers.base import ExtractorError, SupplierSnapshot
from .snapshot_months import month_card

STATUS_OK: Final = "ok"
STATUS_OFF: Final = "off"
STATUS_NOT_COMPARED: Final = "not_compared"

ISSUE_KEY: Final = "bill_check_off"

# The bill's line types, which its electricity bills carry in hidden text the
# same in every language, and the printed labels of its French bills for the
# lines that carry none. Only labels read off real bills are here.
_BY_CODE: Final = {
    "Energiecomponent ElectricityAplusTH": "energy_single",
    "Vaste vergoeding": "standing_charge",
    "GSC administratiekost": "green",
    "Distributie variabel (per kWh)": "distribution",
    "Distributie vast (per jaar)": "distribution_fixed",
    "Transport variabel (per kWh)": "transport",
    "Prosumententarief plafonnering": "prosumer_cap",
}
_BY_LABEL: Final = {
    "Redevance fixe": "standing_charge",
    "Conso. simple": "energy_single",
    "Conso. heures pleines": "energy_peak",
    "Conso. heures creuses": "energy_offpeak",
    "Coûts énergie verte": "green",
    "Distribution variable (kWh)": "distribution",
    "Distribution fixe": "distribution_fixed",
    "Tarif prosumer plafonnement": "prosumer_cap",
    "Coûts de transmission (kWh)": "transport",
    "Coût de transmission": "transport",
    "Redevance de raccordement": "connection_fee",
    "Accise spéciale": "excise",
    "Cotisation sur l'énergie": "energy_contribution",
}

_BANDS: Final[dict[str, StaticBand]] = {
    "energy_single": "single",
    "energy_peak": "peak",
    "energy_offpeak": "offpeak",
}
# What each kind is priced per; every other kind is priced per kWh.
_PER_YEAR: Final = frozenset({"standing_charge", "distribution_fixed"})
# The network lines, and the connection fee, are only mapped as the Walloon
# bills print them.
_WALLOON: Final = frozenset(
    {"distribution", "distribution_fixed", "transport", "connection_fee"}
)

# The bill prints a price per kWh in c€ and one per year in €, each to two
# decimals, so its own rounding moves a price by up to half a unit of the
# last digit. The entry's figure comes off a card printed to the same digits
# including VAT, which divided by 1 + VAT moves it by less than another half
# unit. A gap past one unit is more than the printing can explain.
_TOLERANCE: Final = {"kWh": 0.0001, "year": 0.01}

# Bills come once a year; the months they cover settle on later cards and on
# the indices published after them, so a day is soon enough to look again.
CHECK_INTERVAL: Final = timedelta(days=1)

# Longer than any settlement bill, so a line past it is misread.
_LONGEST_LINE: Final = timedelta(days=3 * 366)

MonthSnapshot = Callable[[date], Awaitable[SupplierSnapshot | None]]


def bill_checked(data: Mapping[str, Any]) -> str | None:
    """The bill sensor an entry checks: the one picked, on an Eneco entry.
    One picked before a switch to another supplier checks nothing."""
    bill = data.get(CONF_ENECO_BILL)
    if data.get(CONF_SUPPLIER) != SUPPLIER_ENECO or not isinstance(bill, str):
        return None
    return bill or None


@dataclass(frozen=True, slots=True)
class _Line:
    """What a bill line says, as the eneco_be sensor publishes it."""

    section: str
    label: str
    code: str | None
    start: date
    end: date
    unit: str | None
    unit_price: float | None


def _read_line(raw: Any) -> _Line | None:
    """The line ``raw`` describes, or None when it does not read as one."""
    if not isinstance(raw, Mapping):
        return None
    label, section, code = raw.get("label"), raw.get("section"), raw.get("code")
    unit, price = raw.get("unit"), raw.get("unit_price")
    if not isinstance(label, str) or not isinstance(section, str):
        return None
    if code is not None and not isinstance(code, str):
        return None
    if unit is not None and not isinstance(unit, str):
        return None
    if price is not None and (
        isinstance(price, bool) or not isinstance(price, int | float)
    ):
        return None
    try:
        start = date.fromisoformat(raw["start"])
        end = date.fromisoformat(raw["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if end < start or end - start > _LONGEST_LINE:
        return None
    return _Line(
        section=section,
        label=label,
        code=code,
        start=start,
        end=end,
        unit=unit,
        unit_price=None if price is None else float(price),
    )


def _kind(line: _Line) -> str | None:
    """What the line bills, by its line type, else by its printed label."""
    if line.code is not None and line.code.strip() in _BY_CODE:
        return _BY_CODE[line.code.strip()]
    label = " ".join(line.label.replace("\u2019", "'").split())
    return _BY_LABEL.get(label)


def _months(start: date, end: date) -> Iterator[date]:
    """The first day of each month from ``start`` to ``end``, both included."""
    month = start.replace(day=1)
    while month <= end:
        yield month
        month = (month + timedelta(days=32)).replace(day=1)


def _vat(snapshot: SupplierSnapshot, month: date, *, fee: bool) -> float:
    """The VAT a figure of ``snapshot`` carries: the card's residential rate
    on a card priced including VAT; on one priced excluding it, none on a
    rate per kWh and the rate ``apply_vat`` baked into a yearly fee."""
    taxes = snapshot.taxes
    if taxes.published_vat_rate or taxes.vat_rate:
        return taxes.vat_rate if fee else 0.0
    return card_residential_vat(snapshot, month)


def _unfit(kind: str, line: _Line, data: Mapping[str, Any]) -> str | None:
    """Why the entry has no price to hold this line to, whatever the month."""
    if kind == "prosumer_cap":
        return "a flat amount set by the period's volumes"
    if line.unit_price is None:
        return "a flat amount"
    want = "year" if kind in _PER_YEAR else "kWh"
    if line.unit != want:
        return f"billed per {line.unit}, priced per {want}"
    if kind in _WALLOON and data.get(CONF_REGION) != REGION_WALLONIA:
        return "network lines are compared on Walloon bills only"
    if kind not in _BANDS and kind != "distribution":
        return None
    meter = data.get(CONF_METER, METER_MONO)
    if meter == METER_EXCLUSIVE_NIGHT:
        return "the entry bills an exclusive night circuit"
    if data.get(CONF_DSO_TARIFF_MODE) == DSO_MODE_IMPACT:
        return "the entry bills the CWaPE bands"
    pair = meter in (METER_BI, METER_DYNAMIC)
    if kind == "energy_single" and pair:
        return "the entry bills a day and a night register"
    if kind in ("energy_peak", "energy_offpeak") and not pair:
        return "the entry bills a single register"
    return None


def _rate(
    kind: str, snapshot: SupplierSnapshot, entry: ConfigEntry, day: date
) -> float | str:
    """What the entry bills for ``kind`` on ``day``, in EUR per unit before
    VAT, or why it bills nothing it can be held to."""
    data = entry.data
    month = day.replace(day=1)
    region = str(data.get(CONF_REGION, ""))
    taxes = snapshot.taxes
    per_kwh = 1.0 + _vat(snapshot, month, fee=False)
    if kind in _BANDS:
        rate = static_energy_eur_per_kwh(snapshot.energy, _BANDS[kind])
        if rate is None:
            return f"no settled energy price for {month:%Y-%m}"
        return rate / per_kwh
    if kind == "standing_charge":
        fee = yearly_fixed_fee_for_meter(
            snapshot.energy, data.get(CONF_METER, METER_MONO)
        )
        return fee / (1.0 + _vat(snapshot, month, fee=True))
    if kind == "green":
        return renewables_eur_per_kwh(taxes, region) / per_kwh
    if kind == "excise":
        return taxes.federal_excise / per_kwh
    if kind == "energy_contribution":
        return taxes.energy_contribution / per_kwh
    if kind == "connection_fee":
        # Exempt from VAT, so billed as the card prints it.
        return taxes_vat_exempt_eur_per_kwh(taxes, region)
    overlay = snapshot.dsos.get(str(data.get(CONF_DSO, "")))
    if overlay is None:
        return f"no network tariffs for {month:%Y-%m}"
    if kind == "transport":
        return overlay.transport / per_kwh
    if kind == "distribution":
        if (
            data.get(CONF_METER, METER_MONO) in (METER_BI, METER_DYNAMIC)
            and data.get(CONF_DSO_TARIFF_MODE) != DSO_MODE_SIMPLE
            and overlay.distribution_peak is not None
            and overlay.distribution_offpeak is not None
        ):
            return "the entry bills distribution per register"
        return overlay.distribution_single / per_kwh
    fixed = (
        overlay.data_management_per_year
        if _walloon_fixed_term_applies(entry, day)
        else 0.0
    )
    return fixed / (1.0 + _vat(snapshot, month, fee=True))


def _result(
    line: _Line,
    status: str,
    *,
    priced: float | list[float] | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "label": line.label,
        "section": line.section,
        "start": line.start.isoformat(),
        "end": line.end.isoformat(),
        "billed": line.unit_price,
        "priced": priced,
        "status": status,
        "reason": reason,
    }


async def _check_line(
    line: _Line, kind: str, snapshot_for: MonthSnapshot, entry: ConfigEntry
) -> dict[str, Any]:
    """One line held to the rate of each month its dates cover."""
    reason = _unfit(kind, line, entry.data)
    if reason is not None:
        return _result(line, STATUS_NOT_COMPARED, reason=reason)
    rates: list[float] = []
    for month in _months(line.start, line.end):
        snapshot = await snapshot_for(max(line.start, month))
        if snapshot is None:
            return _result(
                line,
                STATUS_NOT_COMPARED,
                reason=f"no tariff card for {month:%Y-%m}",
            )
        rate = _rate(kind, snapshot, entry, max(line.start, month))
        if isinstance(rate, str):
            return _result(line, STATUS_NOT_COMPARED, reason=rate)
        rates.append(rate)
    billed = line.unit_price
    assert billed is not None
    low, high = min(rates), max(rates)
    priced: float | list[float] = (
        round(low, 6) if high - low < 1e-9 else [round(low, 6), round(high, 6)]
    )
    # A hair over the unit, so a gap of exactly one is put down to rounding.
    tolerance = _TOLERANCE[line.unit or "kWh"] + 1e-9
    if all(abs(rate - billed) <= tolerance for rate in rates):
        return _result(line, STATUS_OK, priced=priced)
    if low - billed > tolerance or billed - high > tolerance:
        return _result(line, STATUS_OFF, priced=priced)
    return _result(
        line,
        STATUS_NOT_COMPARED,
        priced=priced,
        reason="the entry's rate changes within these dates",
    )


async def check_lines(
    raw_lines: list[Any], snapshot_for: MonthSnapshot, entry: ConfigEntry
) -> list[dict[str, Any]]:
    """Every line of a bill, held to what ``entry`` bills for it.

    ``snapshot_for`` gives the snapshot the entry bills a day of a month on,
    or None where no card of that month can be had. A day before the last
    supplier switch the entry recorded was billed on an earlier contract,
    which the walk below does not price.
    """
    since = current_period_start(entry.data, date.min)
    results: list[dict[str, Any]] = []
    for raw in raw_lines:
        line = _read_line(raw)
        if line is None:
            label = raw.get("label") if isinstance(raw, Mapping) else None
            results.append(
                dict.fromkeys(("section", "start", "end", "billed", "priced"))
                | {
                    "label": label if isinstance(label, str) else None,
                    "status": STATUS_NOT_COMPARED,
                    "reason": "the line does not read",
                }
            )
            continue
        kind = _kind(line)
        if kind is None:
            results.append(
                _result(line, STATUS_NOT_COMPARED, reason="no matching charge")
            )
            continue
        if line.start < since:
            results.append(
                _result(
                    line,
                    STATUS_NOT_COMPARED,
                    reason="billed under an earlier contract of this entry",
                )
            )
            continue
        results.append(await _check_line(line, kind, snapshot_for, entry))
    return results


def _month_snapshots(coordinator: BePricesCoordinator) -> MonthSnapshot:
    """The snapshot each day of a bill is billed on, as the year-to-date walk
    has it, or None for a month no card of its own can be had for: the
    current card would stand in for it, which is not what that month cost."""
    hass, entry = coordinator.hass, coordinator.entry
    supplier, contract, region = coordinator._supplier_tuple
    extractor = get_extractor(supplier)
    current = coordinator._snapshot
    raw = coordinator._snapshot_raw
    session = coordinator._session
    running = dt_util.now().date().replace(day=1)
    cache: dict[date, SupplierSnapshot | None] = {}

    async def snapshot_for(day: date) -> SupplierSnapshot | None:
        if day in cache:
            return cache[day]
        month = day.replace(day=1)
        snapshot = None
        if current is not None and (
            month == running
            or (
                month < running
                and await month_card(
                    hass, session, extractor, contract, region, month, entry
                )
                is not None
            )
        ):
            snapshot = await _effective_snapshot_for_month(
                hass,
                session,
                extractor,
                contract,
                region,
                day,
                current,
                entry,
                current_raw=raw,
            )
        cache[day] = snapshot
        return snapshot

    return snapshot_for


async def check_bill(
    coordinator: BePricesCoordinator, attributes: Mapping[str, Any]
) -> list[dict[str, Any]] | None:
    """The lines of the bill the attributes of a last bill sensor carry,
    each held to this entry's price, or None when there is nothing to check:
    no bill read yet, or no card in hand to price it on."""
    lines = attributes.get("lines")
    if not isinstance(lines, list) or not lines or coordinator._snapshot is None:
        return None
    return await check_lines(lines, _month_snapshots(coordinator), coordinator.entry)


@callback
def clear_unless_checked(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take the Repairs card down once no bill is compared any more."""
    if bill_checked(entry.data) is None:
        ir.async_delete_issue(hass, DOMAIN, f"{ISSUE_KEY}_{entry.entry_id}")


class BillCheckSensor(SensorEntity):
    """How many unit prices of the last Eneco bill this entry prices otherwise.

    Checked when Home Assistant has started, when the bill sensor changes,
    when the entry's card changes and once a day, since the months a bill
    covers settle after it. A Repairs card is up while any line is off.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "bill_check"
    _attr_should_poll = False
    # A line per billed charge and period: the same until the next bill.
    _unrecorded_attributes = frozenset({"lines"})

    def __init__(self, coordinator: BePricesCoordinator, bill: str) -> None:
        self.coordinator = coordinator
        self._bill = bill
        self._attr_unique_id = f"{coordinator.entry.entry_id}_bill_check"
        self._attr_device_info = supplier_device_info(coordinator)
        self._lines: list[dict[str, Any]] | None = None
        self._dates: dict[str, Any] = {}
        self._checked: SupplierSnapshot | None = None
        self._lock = asyncio.Lock()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_state_change_event(self.hass, [self._bill], self._changed)
        )
        self.async_on_remove(
            async_track_time_interval(self.hass, self._schedule, CHECK_INTERVAL)
        )
        self.async_on_remove(self.coordinator.async_add_listener(self._repriced))
        self.async_on_remove(async_at_started(self.hass, self._started))

    @callback
    def _changed(self, _event: Event[EventStateChangedData]) -> None:
        self._schedule()

    @callback
    def _repriced(self) -> None:
        # The listeners are told at every slot boundary too; only a new card
        # can change what the bill is held to.
        if self.coordinator._snapshot is not self._checked:
            self._checked = self.coordinator._snapshot
            self._schedule()

    async def _started(self, _hass: HomeAssistant) -> None:
        self._schedule()

    @callback
    def _schedule(self, *_: Any) -> None:
        self.coordinator.entry.async_create_background_task(
            self.hass,
            self._async_check(),
            f"{DOMAIN} bill check {self.coordinator.entry.entry_id}",
        )

    async def _async_check(self) -> None:
        async with self._lock:
            state = self.hass.states.get(self._bill)
            attributes = state.attributes if state is not None else {}
            try:
                lines = await check_bill(self.coordinator, attributes)
            except (ExtractorError, aiohttp.ClientError, TimeoutError):
                # Asked again at the next check; what was shown stays.
                return
            self._lines = lines
            self._dates = (
                {
                    key: attributes.get(key)
                    for key in ("issued", "period_start", "period_end")
                }
                if lines is not None
                else {}
            )
            self.async_write_ha_state()
            if lines is None:
                # Nothing read yet, as while the eneco integration starts: a
                # card taken down now would come back fresh, ignored or not.
                return
            off = [line for line in lines if line["status"] == STATUS_OFF]
            self.coordinator._sync_issue(
                ISSUE_KEY,
                bool(off),
                extra={
                    "issued": escape_markdown(
                        str(self._dates.get("issued") or self._dates.get("period_end"))
                    ),
                    "count": str(len(off)),
                    "lines": escape_markdown(
                        ", ".join(
                            f"{line['label']} {line['start']}/{line['end']}"
                            for line in off
                        )
                    ),
                },
            )

    @property
    def native_value(self) -> int | None:
        """How many compared lines are off, unknown before a bill is checked."""
        if self._lines is None:
            return None
        return sum(1 for line in self._lines if line["status"] == STATUS_OFF)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {**self._dates, "lines": self._lines or []}
