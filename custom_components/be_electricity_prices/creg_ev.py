# Copyright (c) 2026, Nicolas Brainez <nicolas@brainez.net>
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

"""The CREG's published rate for reimbursing a company car charged at home.

Circular 2024/C/77 lets an employer repay the electricity an employee puts
into a company car at home at a flat rate per metered kWh, free of tax and
social contributions, up to the rate the CREG publishes per region and per
quarter. The CREG publishes the whole series as one CSV beside its page,
and that file is what this reads.

One row per month, oldest last: ``Year;Month;`` then a monthly price and a
three-month mean per region, Flanders, Brussels, Wallonia in that order, in
cents with a decimal comma. The mean is filled on one row in three, the last
month of the window it averages, and it is the rate of the quarter starting
three months later: the ``2026;7`` row averages May to July and is the rate
the page prints under ``Q4/2026``.

Never raises: the fetch runs inside the coordinator tick, whose only handler
is for ``UpdateFailed``. A failure logs and leaves the table as it was.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import date, datetime
from typing import Final

import aiohttp
from homeassistant.util import dt as dt_util

from .const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA

_LOGGER = logging.getLogger(__name__)

_URL: Final = "https://www.creg.be/sites/default/files/assets/Prices/CREG_Tariff_EV.csv"
_TIMEOUT: Final = 30
# The page these sit beside, for the attribution the sensor carries.
SOURCE_URL: Final = (
    "https://www.creg.be/fr/consommateurs/prix-et-tarifs/"
    "tarif-creg-pour-le-remboursement-de-la-recharge-a-domicile-des"
)

# The mean column of each region, by position. The headers name the meter
# each series is computed on ("Digital meter" in Flanders, "Classic meter"
# elsewhere), wording that is likely to change before the layout does.
_MEAN_COLUMN: Final[dict[str, int]] = {
    REGION_FLANDERS: 3,
    REGION_BRUSSELS: 5,
    REGION_WALLONIA: 7,
}
# Catches a column read in the wrong unit (EUR, or per MWh), not a market move.
_MIN_CENTS: Final = 5.0
_MAX_CENTS: Final = 100.0
# Months between the last month a mean covers and the first month it prices.
_LEAD_MONTHS: Final = 3

# The file gains a row once a quarter, so a success is kept for the quarter;
# a failure is retried after a few hours rather than every tick.
_FAILURE_RETRY_S: Final = 6 * 3600
# Rate per region per quarter start, EUR/kWh.
_table: dict[str, dict[date, float]] = {}
_fetched_quarter: date | None = None
_failed_at: datetime | None = None
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    """The module lock, created on first use."""
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def quarter_start(day: date) -> date:
    """The first day of the quarter ``day`` falls in."""
    return date(day.year, 3 * ((day.month - 1) // 3) + 1, 1)


def _add_months(day: date, months: int) -> date:
    """The first of the month ``months`` after ``day``'s month."""
    index = day.year * 12 + (day.month - 1) + months
    return date(index // 12, index % 12 + 1, 1)


def rate_for(region: str, day: date) -> float | None:
    """The rate for ``region`` in the quarter ``day`` falls in, EUR/kWh.

    Synchronous: :func:`ensure_rates` fills the table, this reads it. ``None``
    before the first successful fetch and for a quarter not published yet.
    """
    return _table.get(region, {}).get(quarter_start(day))


def history(region: str) -> list[tuple[date, float]]:
    """Every quarter the table holds for ``region``, oldest first."""
    return sorted(_table.get(region, {}).items())


async def ensure_rates(session: aiohttp.ClientSession, today: date) -> bool:
    """Fetch the CSV once per quarter; return whether ``today`` has a rate.

    A failed download leaves the previous table, which may still answer.
    """
    current = quarter_start(today)
    if _fetched_quarter == current:
        return _has(current)
    async with _get_lock():
        # Re-read under the lock: entries tick together.
        if _fetched_quarter == current:
            return _has(current)
        await _fetch(session, current)
    return _has(current)


def _has(quarter: date) -> bool:
    return any(quarter in rows for rows in _table.values())


async def _fetch(session: aiohttp.ClientSession, quarter: date) -> None:
    """One attempt at the file, under the module lock."""
    global _fetched_quarter, _failed_at
    if _failed_at is not None:
        if (dt_util.utcnow() - _failed_at).total_seconds() < _FAILURE_RETRY_S:
            return
        _failed_at = None
    try:
        text = await _csv_text(session)
        table = parse(text) if text else None
    except Exception as err:  # noqa: BLE001 - the docstring promises no raise
        _LOGGER.warning("CREG home charging rates could not be read: %s", err)
        table = None
    if not table:
        _failed_at = dt_util.utcnow()
        return
    _table.clear()
    _table.update(table)
    _fetched_quarter = quarter
    _LOGGER.debug(
        "CREG home charging rates: %s",
        {region: max(rows) for region, rows in table.items()},
    )


async def _csv_text(session: aiohttp.ClientSession) -> str | None:
    """The file as text, or ``None`` for anything but a 200."""
    try:
        async with session.get(_URL, timeout=aiohttp.ClientTimeout(_TIMEOUT)) as r:
            if r.status == 200:
                payload = await r.read()
                # The file opens with a byte-order mark.
                return payload.decode("utf-8-sig", "replace")
            _LOGGER.debug("CREG rates %s answered %d", _URL, r.status)
    except Exception as err:  # noqa: BLE001 - see ensure_rates
        _LOGGER.debug("CREG rates %s unreadable: %s", _URL, err)
    _LOGGER.warning("CREG home charging rate file could not be read")
    return None


def parse(text: str) -> dict[str, dict[date, float]]:
    """``{region: {quarter_start: eur_per_kwh}}`` from the CSV.

    Rows that do not read as ``Year;Month;...`` (the header, a footnote) and
    cells outside the plausible band are skipped one at a time, so a single
    odd cell does not empty the table. Empty when nothing fixes a quarter.
    """
    table: dict[str, dict[date, float]] = {}
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        if len(row) < 8:
            continue
        try:
            year, month = int(row[0]), int(row[1])
            last_month = date(year, month, 1)
        except ValueError:
            continue
        quarter = _add_months(last_month, _LEAD_MONTHS)
        if quarter != quarter_start(quarter):
            # A mean that lands on no quarter start is another layout.
            continue
        for region, column in _MEAN_COLUMN.items():
            cents = _cents(row[column])
            if cents is None:
                continue
            table.setdefault(region, {})[quarter] = round(cents / 100.0, 6)
    return table


def _cents(cell: str) -> float | None:
    """A cents figure with a decimal comma, or ``None`` for an empty or odd cell."""
    cell = cell.strip()
    if not cell:
        return None
    try:
        value = float(cell.replace(",", "."))
    except ValueError:
        return None
    if not (_MIN_CENTS <= value <= _MAX_CENTS):
        return None
    return value
