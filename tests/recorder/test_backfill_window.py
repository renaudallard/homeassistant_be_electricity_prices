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

"""Where a backfill window ends, against the recorder's own compile.

The recorder inserts an hour's rows when it compiles that hour, without an
upsert, so a row a backfill got in first makes the insert fail and that
period is lost for every entity. These run on the real recorder, with a
second sensor standing for everything else the household records.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import CompileMissingStatisticsTask
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices import backfill_window
from custom_components.be_electricity_prices.backfill_window import (
    _compiled_until,
    _hour_iter,
)

SID = "sensor.be_current_price"
TEMPERATURE = "sensor.temperature"
BLOCKED = "Blocked attempt to insert duplicated statistic rows"


async def _compiled(hass: HomeAssistant, freezer: Any, periods: int) -> datetime:
    """Five-minute periods compiled from the hour after the clock's, a price
    sensor and a temperature reporting in each: that hour."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    # Past the run marker the recorder stamps on a new database.
    zero = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    for period in range(periods):
        moment = zero + timedelta(minutes=5 * period)
        freezer.move_to(moment)
        hass.states.async_set(
            SID, "0.3", {"state_class": "measurement", "unit_of_measurement": "EUR/kWh"}
        )
        hass.states.async_set(
            TEMPERATURE,
            "20.0",
            {"state_class": "measurement", "unit_of_measurement": "°C"},
        )
        await async_wait_recording_done(hass)
        do_adhoc_statistics(hass, start=moment)
        await async_wait_recording_done(hass)
    return zero


async def _backfill(hass: HomeAssistant, start: datetime) -> datetime:
    """A price series written from ``start`` to where the window ends."""
    end = await _compiled_until(hass)
    rows = [
        StatisticData(start=hour, mean=0.3, min=0.3, max=0.3)
        for hour in _hour_iter(start, end)
    ]
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.ARITHMETIC,
        has_sum=False,
        name=None,
        source="recorder",
        statistic_id=SID,
        unit_class=None,
        unit_of_measurement="EUR/kWh",
    )
    if rows:
        async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)
    return end


async def _temperature(
    hass: HomeAssistant, zero: datetime, hours: int
) -> list[float | None]:
    got = await hass.async_add_executor_job(
        lambda: statistics_during_period(
            hass,
            zero,
            zero + timedelta(hours=hours),
            {TEMPERATURE},
            "hour",
            None,
            {"mean"},
        )
    )
    return [row["mean"] for row in got.get(TEMPERATURE, [])]


async def test_the_window_leaves_the_downtime_to_the_catch_up(
    recorder_mock: Any,
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Off for three hours after half an hour of the first: setup backfills
    before the recorder's catch-up, which waits for startup to finish. Ending
    at the current hour wrote the hours the catch-up was still to compile,
    and every entity lost them, the temperature here included."""
    zero = await _compiled(hass, freezer, 6)
    freezer.move_to(zero + timedelta(hours=3, minutes=1))
    assert await _backfill(hass, zero - timedelta(hours=3)) <= zero
    get_instance(hass).queue_task(CompileMissingStatisticsTask())
    await async_wait_recording_done(hass)
    assert BLOCKED not in caplog.text
    assert await _temperature(hass, zero, 3) == [20.0, 20.0, 20.0]


async def test_the_window_stays_behind_the_hour_being_compiled(
    recorder_mock: Any,
    hass: HomeAssistant,
    freezer: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Five seconds past the hour, before the recorder compiles it at ten:
    ending at the current hour wrote the hour that had just ended."""
    zero = await _compiled(hass, freezer, 11)
    freezer.move_to(zero + timedelta(hours=1, seconds=5))
    assert await _backfill(hass, zero - timedelta(hours=3)) <= zero
    freezer.move_to(zero + timedelta(hours=1, seconds=10))
    do_adhoc_statistics(hass, start=zero + timedelta(minutes=55))
    await async_wait_recording_done(hass)
    assert BLOCKED not in caplog.text
    assert await _temperature(hass, zero, 1) == [20.0]


@pytest.mark.parametrize(
    ("newest", "end"),
    [
        (datetime(2026, 5, 1, 7, 25, tzinfo=UTC), datetime(2026, 5, 1, 7, tzinfo=UTC)),
        (datetime(2026, 5, 1, 7, 55, tzinfo=UTC), datetime(2026, 5, 1, 8, tzinfo=UTC)),
        (None, datetime(2026, 5, 1, 7, tzinfo=UTC)),
        (RuntimeError("database is locked"), datetime(2026, 5, 1, 7, tzinfo=UTC)),
    ],
    ids=["within_the_hour", "the_hours_last", "no_run", "unanswered"],
)
async def test_the_window_ends_with_the_hour_the_newest_run_closes(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any, newest: Any, end: datetime
) -> None:
    """The newest five minutes compiled close the hour they end in: those
    from 07:55 close 07:00 to 08:00 itself. With no run to go by, the hour
    that has just ended is left to the compile, ten seconds after it."""
    freezer.move_to(datetime(2026, 5, 1, 8, 0, 5, tzinfo=UTC))
    if isinstance(newest, Exception):
        read = MagicMock(side_effect=newest)
    else:
        read = MagicMock(return_value=newest)
    with patch.object(backfill_window, "_newest_statistics_run", read):
        assert await _compiled_until(hass) == end
    read.assert_called_once_with(hass)
