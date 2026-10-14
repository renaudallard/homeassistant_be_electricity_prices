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

"""A backfill run that outlasts an hour still joins the live chain.

The window ends on the hour the run began in. On a slow host the next hour can
close before the import, and the live compile then writes its long-term row on
the chain the sensor had, not the imported one: on a first backfill the bill
read as minus the whole year to date in that hour and plus it in the next. A
run crossing New Year's midnight also finds the new year's first rows there,
of another cycle. Uses the real recorder, since the defect lives in how HA
compiles against what we import.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.components.recorder.models import StatisticData
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.be_electricity_prices.backfill_cost import _import_cost_rows
from tests.recorder.test_backfill_year_join import META, NEW, OLD, _hours, _live


async def test_a_run_past_the_next_hour_moves_that_hours_row(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    h = NEW - timedelta(days=60)
    # The run began just after h + 2 and priced up to it; meanwhile the sensor
    # reported on, and h + 2 closed before the import.
    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), "500.2", OLD)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), "500.3", OLD)
    await _live(hass, freezer, h + timedelta(hours=3, minutes=1), "500.35", OLD)
    rows = [
        StatisticData(start=h, state=499.9, sum=499.9),
        StatisticData(start=h + timedelta(hours=1), state=500.0, sum=500.0),
    ]
    await _import_cost_rows(hass, META, rows, OLD, async_import_statistics)
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=3, minutes=56), "500.4", OLD)
    got = await hass.async_add_executor_job(_hours, hass, h)
    assert [r[2] for r in got] == pytest.approx([499.9, 500.0, 500.3, 500.4]), got
    assert [r[3] for r in got[1:]] == pytest.approx([0.1, 0.3, 0.1]), got


async def test_a_run_past_new_years_midnight_moves_the_new_years_rows(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    # The run began in the year's last hour and priced up to it; the sensor
    # went on into the new year, which reset it, before the import.
    last = NEW - timedelta(hours=1)
    await _live(hass, freezer, last + timedelta(minutes=1), "999.95", OLD)
    await _live(hass, freezer, last + timedelta(minutes=56), "1000.0", OLD)
    await _live(hass, freezer, NEW + timedelta(minutes=1), "0.5", NEW)
    await _live(hass, freezer, NEW + timedelta(minutes=56), "1.0", NEW)
    rows = [
        StatisticData(start=last - timedelta(hours=2), state=999.8, sum=999.8),
        StatisticData(start=last - timedelta(hours=1), state=999.9, sum=999.9),
    ]
    await _import_cost_rows(hass, META, rows, OLD, async_import_statistics)
    await async_wait_recording_done(hass)
    await _live(hass, freezer, NEW + timedelta(hours=1, minutes=56), "1.5", NEW)
    got = await hass.async_add_executor_job(_hours, hass, rows[0]["start"])
    assert [r[2] for r in got] == pytest.approx(
        [999.8, 999.9, 1000.0, 1001.0, 1001.5]
    ), got
    assert [r[3] for r in got[1:]] == pytest.approx([0.1, 0.1, 1.0, 0.5]), got
