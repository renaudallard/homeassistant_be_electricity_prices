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

"""The legs of the running bill that are charged per day, not per kWh.

The subscription and network standing charges, the Walloon prosumer fee and
the Flemish capacity term. Each is pro-rated over the days the contract
actually covered inside the year-to-date window, which is why they all walk
the same month list rather than each deciding what a year is.
"""

from __future__ import annotations

from .cohort import _effective_snapshot_for_month
from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_METER,
    CONF_REGION,
    METER_MONO,
    REGION_FLANDERS,
)
from .fees import (
    _annual_static_fees,
    _capped_capacity_monthly_eur,
    _compensation_kva,
    _prosumer_monthly_fee,
)
from .pricing import MeterType, yearly_fixed_fee_for_meter
from .providers.base import SupplierExtractor, SupplierSnapshot
from collections.abc import AsyncIterator
from datetime import date, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from typing import NamedTuple
import aiohttp
import calendar


def _days_through(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end`` (local calendar)."""
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


async def _walk_ytd_months(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    window_start: date,
    contract: str | None = None,
    cached_only: bool = False,
) -> AsyncIterator[tuple[SupplierSnapshot, date, int, int]]:
    """Yield ``(snap_m, month_first, days_in_full_month, days_in_ytd)``
    for each month from the year-to-date window start up through today.

    Centralises the per-month walk shared by every YTD accumulator so
    the proration formula and the per-month archive lookup stay in one
    place. ``snap_m`` falls back to the current snapshot for months
    with no archive (see :func:`_snapshot_for_month`).

    The window starts at 1 January unless the entry bills from its contract
    start date, and the walk starts at whichever it is: ``days_in_ytd`` for
    the first month then counts from that day rather than from the 1st, so
    every fee this feeds (the annual standing charges, the Walloon prosumer
    fee, the Flemish capacity term) prorates over the days the contract
    actually covers. Without that a contract signed on 30 June still billed
    twelve months of standing charges against six months of energy, which is
    a worse number than the one the option was turned on to fix.

    ``month_first`` and ``days_in_full_month`` stay the calendar month's, not
    the window's: they address the month's archived card and divide a MONTHLY
    fee, and neither of those is about how much of the month was billed.

    ``contract`` overrides the entry's stored contract id; the
    OptionsFlow compare path uses this to walk months for an
    alternative supplier without mutating the live entry.

    ``cached_only`` walks the same months without fetching any of them: every
    month the cache does not already hold falls back to the current card. The
    first coordinator tick uses it to stay off the network while it is running
    inside config-entry setup.
    """
    region = entry.data.get(CONF_REGION, "")
    contract = contract or entry.data[CONF_CONTRACT]
    cur = window_start
    while cur <= today:
        month_first = date(cur.year, cur.month, 1)
        snap_m = await _effective_snapshot_for_month(
            hass,
            session,
            extractor,
            contract,
            region,
            month_first,
            snapshot,
            entry,
            cached_only=cached_only,
        )
        if cur.month == 12:
            next_first = date(cur.year + 1, 1, 1)
        else:
            next_first = date(cur.year, cur.month + 1, 1)
        days_in_full_month = (next_first - month_first).days
        month_end_in_ytd = min(next_first - timedelta(days=1), today)
        days_in_ytd = (month_end_in_ytd - cur).days + 1
        yield snap_m, month_first, days_in_full_month, days_in_ytd
        cur = next_first


class _StaticFees(NamedTuple):
    """The pro-rated year-to-date static fees, and the supplier's share of them."""

    total: float
    supplier_fee: float


async def _ytd_static_fees(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    window_start: date,
    contract: str | None = None,
    meter: MeterType | None = None,
    cached_only: bool = False,
) -> _StaticFees:
    """Pro-rated YTD total of yearly_fixed_fee + 12*energy_fund using each
    month's archived snapshot.

    ``meter`` defaults to the entry's meter; the compare flow passes a
    meter override so the fixed fee is billed at the same meter the energy
    is billed at (e.g. an exclusive-night override).

    Uses the uniform days_in_year proration but reads the rate from the
    archived snapshot for each past month, so a supplier indexation
    that lands mid-year is honoured for the months it applies to.
    Falls back to the current snapshot for months with no archive.

    The second figure is the SUPPLIER's standing charge alone, out of a total
    that also carries the energy fund, the data-management charge and the
    Brussels OSP fee. A welcome credit may come off the standing charge and
    off none of the other three, so it needs them told apart; the same walk
    answers both rather than a second one drifting from this.
    """
    days_in_year = 366 if calendar.isleap(today.year) else 365
    total = 0.0
    supplier_fee = 0.0
    for_meter = meter or entry.data.get(CONF_METER, METER_MONO)
    async for snap_m, _, _, days_in_ytd in _walk_ytd_months(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        today,
        window_start=window_start,
        contract=contract,
        cached_only=cached_only,
    ):
        share = days_in_ytd / days_in_year
        total += _annual_static_fees(snap_m, for_meter, entry) * share
        supplier_fee += (
            float(yearly_fixed_fee_for_meter(snap_m.energy, for_meter) or 0.0) * share
        )
    return _StaticFees(total, supplier_fee)


async def _ytd_prosumer(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    window_start: date,
    contract: str | None = None,
    cached_only: bool = False,
) -> float:
    """Sum the monthly prosumer fee across YTD using each month's archived
    snapshot's DSO overlay, so a CWaPE indexation that lands mid-year is
    honoured for the months it applies to."""
    kva = _compensation_kva(entry)
    if not kva:
        return 0.0
    dso = entry.data.get(CONF_DSO, "")

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        today,
        window_start=window_start,
        contract=contract,
        cached_only=cached_only,
    ):
        overlay = snap_m.dsos.get(dso)
        monthly_fee = _prosumer_monthly_fee(overlay, snap_m, kva)
        if monthly_fee == 0.0:
            continue
        total += monthly_fee * (days_in_ytd / days_in_full_month)
    return total


async def _ytd_capacity(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    billed_peak_kw: float,
    *,
    window_start: date,
    contract: str | None = None,
    meter: MeterType | None = None,
    cached_only: bool = False,
) -> float:
    """Sum the monthly Flemish capacity charge across YTD, reading each
    month's archived DSO overlay so a VREG indexation landing mid-year is
    honoured for the months it applies to.

    Each month's charge is held under the VREG network ceiling the Flemish
    cards print as ``maximumtarief``, which caps the capacity term plus the
    per-kWh network term together against the household's yearly volume. The
    ceiling used to be applied on the quote paths only, so the compare page
    and the projection honoured a cap this sensor billed straight through.

    ``meter`` defaults to the entry's, and the comparison page overrides it for
    the same reason it overrides the supplier fee: the VREG ceiling is measured
    against the per-kWh network term, and an exclusive-night circuit has its
    own. Quoting a meter the household need not have has to cap it on the meter
    the rest of the quote is priced on.

    ``billed_peak_kw`` is the CURRENT gemiddelde maandpiek, applied to every
    month of the year rather than reconstructed per month. Reconstruction is
    not available in general: the rolling window holds at most twelve months
    and an entry installed mid-year has no history for the months before it,
    where Fluvius billed against meter history we never saw. The current mean
    is the honest stand-in precisely because it is a twelve-month mean, so it
    moves slowly and is close to what each month of this year was billed on.
    """
    if entry.data.get(CONF_REGION) != REGION_FLANDERS:
        return 0.0
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        today,
        window_start=window_start,
        contract=contract,
        cached_only=cached_only,
    ):
        monthly = _capped_capacity_monthly_eur(
            snap_m.dsos.get(dso),
            entry,
            billed_peak_kw,
            meter,
            snap_m.taxes.vat_rate,
        )
        total += monthly * (days_in_ytd / days_in_full_month)
    return total
