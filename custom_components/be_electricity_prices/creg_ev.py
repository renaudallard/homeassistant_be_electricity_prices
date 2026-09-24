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

"""The CREG rate for reimbursing a company car charged at home.

Circular 2024/C/77 lets an employer repay the electricity an employee puts
into a company car at home at a flat rate per metered kWh, free of tax and
social contributions, up to a maximum the SPF Finances sets per region and
per quarter from the CREG's monthly prices. The CREG publishes those prices
as one CSV beside its page, and that file is what this reads.

One row per month, oldest last: ``Year;Month;`` then a monthly price and a
three-month mean per region, Flanders, Brussels, Wallonia in that order, in
cents with a decimal comma. A quarter's rate is the mean of the monthly
prices of the three months ending three months before it: May to July for
``Q4/2026``. It is computed here from the monthly prices, as each addendum
to the circular shows the SPF doing, rather than read off the mean column:
the CREG rounds that column from unrounded prices, and it once disagreed
with the circular, 36,18 against 36,17 for Wallonia in Q2/2025.

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

# The monthly price column of each region, by position. The headers name the
# meter each series is computed on ("Digital meter" in Flanders, "Classic
# meter" elsewhere), wording that is likely to change before the layout does.
_PRICE_COLUMN: Final[dict[str, int]] = {
    REGION_FLANDERS: 2,
    REGION_BRUSSELS: 4,
    REGION_WALLONIA: 6,
}
# Catches a column read in the wrong unit (EUR, or per MWh), not a market move.
_MIN_CENTS: Final = 5.0
_MAX_CENTS: Final = 100.0
# Months between the last month a mean covers and the first month it prices.
_LEAD_MONTHS: Final = 3

# The file gains a row once a quarter, so a file that prices the running
# quarter is kept for the rest of it. A failure, or a file the CREG has not
# added the quarter to yet, is retried after a few hours rather than every
# tick.
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

    A quarter counts as fetched only once the file prices it. A failed
    download, or a file that does not price the quarter yet, leaves the
    previous table, which may still answer, and is retried after the backoff.
    """
    current = quarter_start(today)
    if _fetched_quarter == current:
        return _has(_table, current)
    async with _get_lock():
        # Re-read under the lock: entries tick together.
        if _fetched_quarter == current:
            return _has(_table, current)
        await _fetch(session, current)
    return _has(_table, current)


def _has(table: dict[str, dict[date, float]], quarter: date) -> bool:
    return any(quarter in rows for rows in table.values())


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
    if not _has(table, quarter):
        # Read before the CREG added this quarter. Settling on it would leave
        # the sensor unavailable until the next quarter, so it is retried
        # after the backoff, and the previous table answers meanwhile.
        _LOGGER.debug("CREG home charging rates: nothing yet for %s", quarter)
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
    odd cell costs only the quarter it feeds. A quarter missing one of its
    three months is not priced. Empty when nothing fixes a quarter.
    """
    prices: dict[str, dict[date, float]] = {}
    for row in csv.reader(io.StringIO(text), delimiter=";"):
        if len(row) < 8:
            continue
        try:
            month = date(int(row[0]), int(row[1]), 1)
        except ValueError:
            continue
        for region, column in _PRICE_COLUMN.items():
            cents = _cents(row[column])
            if cents is not None:
                prices.setdefault(region, {})[month] = cents
    table: dict[str, dict[date, float]] = {}
    for region, months in prices.items():
        for last in months:
            quarter = _add_months(last, _LEAD_MONTHS)
            if quarter != quarter_start(quarter):
                continue
            window = [_add_months(last, -back) for back in (2, 1, 0)]
            if not all(month in months for month in window):
                continue
            # Rounded to the hundredth of a cent, as the circular prints it.
            # The file prints each price to that precision, and a mean of
            # three such figures never falls on a half, so the float rounding
            # cannot tip it either way.
            mean = round(sum(months[month] for month in window) / 3, 2)
            table.setdefault(region, {})[quarter] = round(mean / 100.0, 6)
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
