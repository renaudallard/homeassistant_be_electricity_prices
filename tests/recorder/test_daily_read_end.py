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


"""A daily read stops at the day it was asked to end on.

Home Assistant runs a daily statistics query one day further than the end it
is handed, so a read up to the day before a supplier switch also returned the
switch day. On a consumption totals sensor that day became the last day the
meter reported, the contract's real last day looked like a gap before it, and
the gap rule left it out of the bill on both sides (an earlier contract's
running cost and the compare page's own row lost a day the backfill billed).
"""

from __future__ import annotations

from datetime import UTC, date, timedelta
from types import SimpleNamespace
from typing import Any

from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_import_statistics,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.be_electricity_prices import energy_meters as em
from custom_components.be_electricity_prices.meter_daily import _resolve_daily_kwh

CONSUMPTION = "sensor.grid_import"
INJECTION = "sensor.grid_export"
FIRST = date(2026, 6, 20)
LAST = date(2026, 7, 10)
END = date(2026, 6, 30)


async def _import(hass: HomeAssistant, entity_id: str, per_hour: float) -> None:
    hass.states.async_set(
        entity_id,
        "0",
        {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
        },
    )
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_class="energy",
        unit_of_measurement="kWh",
    )
    rows: list[StatisticData] = []
    when = dt_util.start_of_local_day(FIRST).astimezone(UTC)
    stop = dt_util.start_of_local_day(LAST).astimezone(UTC)
    total = 1000.0
    while when < stop:
        total += per_hour
        rows.append(StatisticData(start=when, state=total, sum=total))
        when += timedelta(hours=1)
    async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)


async def _setup(hass: HomeAssistant) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    await _import(hass, CONSUMPTION, 0.5)
    await _import(hass, INJECTION, 0.25)


async def test_a_daily_read_ends_on_its_last_day(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    await _setup(hass)
    start = END - timedelta(days=4)
    plain = await em._recorder_daily_kwh(hass, CONSUMPTION, start, END)
    with em.memoise_meter_reads({}):
        await em._recorder_rows(
            hass, CONSUMPTION, FIRST, LAST, "hour", {"change", "sum"}
        )
        served = await em._recorder_daily_kwh(hass, CONSUMPTION, start, END)
    assert max(plain) == END
    assert plain == served
    assert plain[END] == 12.0


async def test_an_earlier_contract_bills_its_last_day(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    """The window an earlier contract is priced over ends the day before the
    switch, a past day, and that day is billed like the others."""
    await _setup(hass)
    entry = SimpleNamespace(
        entry_id="earlier",
        data={
            "region": "wallonia",
            "meter": "mono",
            "solar_regime": "compensation",
            "consumption_kwh": CONSUMPTION,
            "injection_kwh": INJECTION,
        },
    )
    start = END - timedelta(days=4)
    days = await _resolve_daily_kwh(
        hass,
        entry,  # type: ignore[arg-type]
        END,
        start,
    )
    assert days is not None
    assert sorted(days) == [start + timedelta(days=n) for n in range(5)]
    assert days[END] == (12.0, 0.0, 6.0, 0.0)
