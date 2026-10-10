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

"""A backfill into a new year continues the recorded `sum` chain.

The live compile does not restart `sum` when `last_reset` moves to 1 January:
it carries the running total on. The backfill builds each row with
`sum = state`, the year-to-date bill, so imported as built, the first hour of
the new year read minus last year's whole bill. Uses the real recorder, since
the defect lives in how HA compiles against what we import.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices.backfill_cost import _import_cost_rows

SID = "sensor.be_current_year_cost"
OLD = datetime(2025, 12, 31, 23, tzinfo=UTC)  # 1 January 2026, Brussels
NEW = datetime(2026, 12, 31, 23, tzinfo=UTC)  # 1 January 2027, Brussels
META = StatisticMetaData(
    mean_type=StatisticMeanType.NONE,
    has_sum=True,
    name=None,
    source="recorder",
    statistic_id=SID,
    unit_class=None,
    unit_of_measurement="EUR",
)


async def _live(
    hass: HomeAssistant, freezer: Any, when: datetime, value: str, reset: datetime
) -> None:
    freezer.move_to(when)
    hass.states.async_set(
        SID,
        value,
        {
            "device_class": "monetary",
            "state_class": "total",
            "unit_of_measurement": "EUR",
            "last_reset": reset.isoformat(),
        },
    )
    await hass.async_block_till_done()
    await async_wait_recording_done(hass)
    do_adhoc_statistics(
        hass, start=when.replace(minute=when.minute - when.minute % 5, second=0)
    )
    await async_wait_recording_done(hass)


def _hours(
    hass: HomeAssistant, start: datetime
) -> list[tuple[str, float | None, float | None, float | None]]:
    got = statistics_during_period(
        hass, start, None, {SID}, "hour", None, {"sum", "state", "change"}
    )
    return [
        (
            datetime.fromtimestamp(r["start"], tz=UTC).strftime("%m-%d %H:%M"),
            r.get("state"),
            r.get("sum"),
            r.get("change"),
        )
        for r in got.get(SID, [])
    ]


async def test_a_new_year_backfill_continues_last_years_chain(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """The live chain holds last year's bill at 31 December; the imported
    first hour of the new year has to add to it, not restart below it."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    last_hour = NEW - timedelta(hours=2)
    await _live(hass, freezer, last_hour + timedelta(minutes=1), "1000.0", OLD)
    await _live(hass, freezer, last_hour + timedelta(minutes=56), "1200.0", OLD)
    live = await hass.async_add_executor_job(_hours, hass, last_hour)
    chain = live[-1][2]
    assert chain is not None, live
    # The sensor goes on reporting into the new year before any backfill runs.
    await _live(hass, freezer, NEW + timedelta(minutes=1), "3.0", NEW)
    await _live(hass, freezer, NEW + timedelta(hours=1, minutes=56), "5.0", NEW)

    # What the backfill builds for the new year's first two hours.
    rows = [
        StatisticData(start=NEW, state=3.0, sum=3.0),
        StatisticData(start=NEW + timedelta(hours=1), state=5.0, sum=5.0),
    ]
    freezer.move_to(NEW + timedelta(hours=2, minutes=1))
    await _import_cost_rows(hass, META, rows, NEW, async_import_statistics)
    await async_wait_recording_done(hass)
    got = await hass.async_add_executor_job(_hours, hass, last_hour)
    assert [r[3] for r in got[1:]] == pytest.approx([3.0, 2.0]), got
    assert got[-1][2] == pytest.approx(chain + 5.0), got

    # And the live compile carries on from the imported total.
    await _live(hass, freezer, NEW + timedelta(hours=2, minutes=56), "5.5", NEW)
    got = await hass.async_add_executor_job(_hours, hass, last_hour)
    assert got[-1][3] == pytest.approx(0.5), got


async def test_a_first_backfill_imports_the_bill_as_it_is(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """With nothing recorded before the window the rows are not shifted, which
    is every backfill of the year the entry was installed in."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    rows = [
        StatisticData(start=NEW, state=3.0, sum=3.0),
        StatisticData(start=NEW + timedelta(hours=1), state=5.0, sum=5.0),
    ]
    freezer.move_to(NEW + timedelta(hours=2, minutes=1))
    await _import_cost_rows(hass, META, rows, NEW, async_import_statistics)
    await async_wait_recording_done(hass)
    got = await hass.async_add_executor_job(_hours, hass, NEW - timedelta(hours=1))
    assert [r[2] for r in got] == pytest.approx([3.0, 5.0]), got
