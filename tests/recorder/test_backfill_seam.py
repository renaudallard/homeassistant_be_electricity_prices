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

"""The backfill -> live seam in the cost sensor's `sum` chain.

The cost sensor is `state_class: TOTAL`, so the recorder's own sensor platform
compiles statistics under the same id the backfill imports into, and it
resumes its running sum from the newest `statistics_short_term` row. The
backfill writes only the long-term table, so the live rows are moved onto the
imported chain by the recorder (`async_adjust_statistics`), and the compile
carries on from a moved one. A sensor with no live row yet has nothing to
move, so nothing is imported until it has one: one compiled after the import
started its chain at zero right after a backfilled row carrying the whole
year, and the Energy dashboard's Cost card showed roughly minus one annual
bill for that hour.

Uses the real recorder rather than the mocks the rest of test_backfill.py
relies on, because the defect lives in how HA compiles, not in what we write.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    StatisticMeanType,
    async_import_statistics,
    statistics_during_period,
)
from homeassistant.components.recorder.tasks import RecorderTask
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices.backfill_cost import (
    UNJOINED,
    _import_cost_rows,
)
from custom_components.be_electricity_prices.coordinator_data import ytd_window_reset
from tests.recorder.test_backfill_year_join import META, NEW, OLD, _hours, _live


def jan1_reset() -> datetime:
    """The instant both halves of the seam have to agree on.

    Resolved through the production helper rather than spelled out again here.
    The seam only works while the imported rows and the sensor's
    ``last_reset`` are the same function's answer, so a test that recomputed
    it by hand would keep passing through exactly the divergence it exists to
    catch.
    """
    entry = SimpleNamespace(data={})
    return ytd_window_reset(entry)  # type: ignore[arg-type]


SID = "sensor.be_current_year_cost"


async def test_the_rows_wait_for_the_sensors_first_live_row(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """With no live row to move, nothing is imported, and the backfill says
    why. Once the sensor has compiled its first five minutes, the rows join
    them, and the compile carries on from the imported total rather than
    restart below it (sum 0.1, change -500.0)."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    h = NEW - timedelta(days=60)
    rows = [
        StatisticData(start=h, state=500.0, sum=500.0),
        StatisticData(start=h + timedelta(hours=1), state=500.1, sum=500.1),
    ]
    freezer.move_to(h + timedelta(hours=2, minutes=1))
    why = await _import_cost_rows(hass, META, rows, OLD, async_import_statistics)
    await async_wait_recording_done(hass)
    assert why == UNJOINED
    assert not await hass.async_add_executor_job(_hours, hass, h)

    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), "500.2", OLD)
    why = await _import_cost_rows(hass, META, rows, OLD, async_import_statistics)
    await async_wait_recording_done(hass)
    assert why is None
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), "500.3", OLD)
    got = await hass.async_add_executor_job(_hours, hass, h)
    assert [r[2] for r in got] == pytest.approx([500.0, 500.1, 500.3]), got
    assert [r[3] for r in got[1:]] == pytest.approx([0.1, 0.2]), got


async def test_a_window_that_starts_later_keeps_the_rows_before_it(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """A service call can start the window after the reset. The rows join the
    chain the hours before the window hold, so the live rows move from the
    window's first hour on: moved from the reset, the hours before it rose by
    the shift too and the window's first hour fell by it (change -500.1)."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    h = NEW - timedelta(days=60)
    before = [
        StatisticData(start=h - timedelta(hours=2), state=499.7, sum=499.7),
        StatisticData(start=h - timedelta(hours=1), state=499.8, sum=499.8),
    ]
    async_import_statistics(hass, META, before)
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), "500.2", OLD)
    rows = [
        StatisticData(start=h, state=499.9, sum=499.9),
        StatisticData(start=h + timedelta(hours=1), state=500.0, sum=500.0),
    ]
    assert (
        await _import_cost_rows(hass, META, rows, OLD, async_import_statistics) is None
    )
    await async_wait_recording_done(hass)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), "500.3", OLD)
    got = await hass.async_add_executor_job(_hours, hass, h - timedelta(hours=2))
    assert [r[2] for r in got] == pytest.approx([499.7, 499.8, 499.9, 500.0, 500.3])
    assert [r[3] for r in got[1:]] == pytest.approx([0.1, 0.1, 0.1, 0.3]), got


@dataclass(slots=True)
class _Held(RecorderTask):
    """Holds the recorder's queue until ``gate`` opens, as a busy database
    does."""

    gate: threading.Event

    def run(self, instance: Recorder) -> None:
        self.gate.wait(10)


async def test_two_joins_at_once_move_the_live_rows_once(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """The automatic backfill and a service call can join the same rows at
    once. Each moves the live rows by what it read lies between the two
    chains, so two that read before either was written moved them twice: on
    a busy recorder the next hour read sum 1000.5 and change +500.5. One join
    at a time, each reading what the last one wrote, moves them once."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    h = NEW - timedelta(days=60)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=1), "500.2", OLD)
    await _live(hass, freezer, h + timedelta(hours=2, minutes=56), "500.3", OLD)
    rows = [
        StatisticData(start=h, state=499.9, sum=499.9),
        StatisticData(start=h + timedelta(hours=1), state=500.0, sum=500.0),
    ]
    gate = threading.Event()
    get_instance(hass).queue_task(_Held(gate))
    opener = threading.Timer(0.5, gate.set)
    opener.start()
    try:
        got = await asyncio.gather(
            _import_cost_rows(hass, META, list(rows), OLD, async_import_statistics),
            _import_cost_rows(hass, META, list(rows), OLD, async_import_statistics),
        )
    finally:
        opener.cancel()
        gate.set()
    await async_wait_recording_done(hass)
    assert got == [None, None]
    await _live(hass, freezer, h + timedelta(hours=3, minutes=56), "500.4", OLD)
    hours = await hass.async_add_executor_job(_hours, hass, h)
    assert [r[2] for r in hours] == pytest.approx([499.9, 500.0, 500.3, 500.4])
    assert [r[3] for r in hours[1:]] == pytest.approx([0.1, 0.3, 0.1]), hours


async def test_a_live_compile_before_the_backfill_keeps_the_seam(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """The first compile of a setup can land before the backfill has written
    anything, and a service call always runs on a live entry. The compiler
    resumes from the newest short-term row, so a seed at the last backfilled
    hour alone left that newer row's own chain in charge: the next hour read
    sum 0.1 and change -499.9. The live rows after the backfill are moved onto
    its chain, so the seam holds in either order."""
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    h = datetime(2026, 10, 10, 11, tzinfo=UTC)
    freezer.move_to(h)
    reset = jan1_reset()
    attrs = {
        "device_class": "monetary",
        "state_class": "total",
        "unit_of_measurement": "EUR",
        "last_reset": reset.isoformat(),
    }

    async def live(when: datetime, value: str) -> None:
        freezer.move_to(when)
        hass.states.async_set(SID, value, attrs)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
        do_adhoc_statistics(
            hass, start=when.replace(minute=when.minute - when.minute % 5, second=0)
        )
        await async_wait_recording_done(hass)

    await live(h + timedelta(hours=2, minutes=21), "500.2")
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=SID,
        unit_class=None,
        unit_of_measurement="EUR",
    )
    rows = [
        StatisticData(start=h, state=499.9, sum=499.9),
        StatisticData(start=h + timedelta(hours=1), state=500.0, sum=500.0),
    ]
    assert (
        await _import_cost_rows(hass, meta, rows, reset, async_import_statistics)
        is None
    )
    await async_wait_recording_done(hass)
    await live(h + timedelta(hours=2, minutes=56), "500.3")

    def hours() -> list[Any]:
        return statistics_during_period(
            hass, h - timedelta(hours=1), None, {SID}, "hour", None, {"sum", "change"}
        )[SID]

    got = await hass.async_add_executor_job(hours)
    changes = [float(c) for r in got if (c := r.get("change")) is not None]
    assert min(changes) > -1.0, got
    assert got[-1].get("sum") == pytest.approx(500.3), got
