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
compiles statistics under the same id the backfill imports into, and it seeds
its running sum from `statistics_short_term` alone. `async_import_statistics`
writes only the long-term table, so without a short-term seed the live chain
restarts at zero right after a backfilled row carrying the whole year: the
first compiled hour reported `change = 0 - <year to date>` and the Energy
dashboard's Cost card showed roughly minus one annual bill.

Uses the real recorder rather than the mocks the rest of test_backfill.py
relies on, because the defect lives in how HA compiles, not in what we write.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices.backfill import _seed_short_term_sum
from custom_components.be_electricity_prices.coordinator import ytd_window_reset


def jan1_reset() -> datetime:
    """The instant both halves of the seam have to agree on.

    Resolved through the production helper rather than spelled out again here.
    The seam only works while the seed and the sensor's ``last_reset`` are the
    same function's answer, so a test that recomputed it by hand would keep
    passing through exactly the divergence it exists to catch.
    """
    entry = SimpleNamespace(data={})
    return ytd_window_reset(entry)  # type: ignore[arg-type]


SID = "sensor.be_current_year_cost"


async def _run(hass: HomeAssistant, *, seed: bool) -> list[tuple]:
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    past = now - timedelta(hours=3)
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
        StatisticData(start=past, state=500.0, sum=500.0),
        StatisticData(start=past + timedelta(hours=1), state=500.1, sum=500.1),
    ]
    async_import_statistics(hass, meta, rows)
    if seed:
        _seed_short_term_sum(hass, meta, rows[-1], jan1_reset())
    await async_wait_recording_done(hass)

    jan1 = jan1_reset()
    attrs = {
        "device_class": "monetary",
        "state_class": "total",
        "unit_of_measurement": "EUR",
        "last_reset": jan1.isoformat(),
    }
    for value in ("500.2", "500.3"):
        hass.states.async_set(SID, value, attrs)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)

    n = dt_util.utcnow()
    period = n.replace(minute=n.minute - n.minute % 5, second=0, microsecond=0)
    do_adhoc_statistics(hass, start=period)
    await async_wait_recording_done(hass)
    do_adhoc_statistics(hass, start=period.replace(minute=55))
    await async_wait_recording_done(hass)

    stats = statistics_during_period(
        hass,
        past - timedelta(hours=1),
        None,
        {SID},
        "hour",
        None,
        {"sum", "state", "change"},
    )
    return [
        (
            datetime.fromtimestamp(r["start"], tz=UTC).strftime("%H:%M"),
            r.get("state"),
            r.get("sum"),
            r.get("change"),
        )
        for r in stats[SID]
    ]


async def test_seeding_continues_the_sum_chain(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    """The compiled hour must resume from the backfilled total.

    Both failure modes are pinned. Without any seed the chain restarted at
    zero (sum 0.1, change -500.0). Seeding sum and state but not last_reset
    looked like a fresh cycle against the sensor's Jan-1 last_reset and added
    the whole live reading on top (sum 1000.4, change +500.3). Only a seed
    carrying all three continues it (sum 500.3, change +0.2).
    """
    rows = await _run(hass, seed=True)
    assert rows, "no statistics rows produced"
    changes = [r[3] for r in rows if r[3] is not None]
    assert min(changes) > -1.0, f"negative seam still present: {rows}"
    last = rows[-1]
    assert last[2] == pytest.approx(500.3), f"sum did not continue the chain: {rows}"
    assert last[3] == pytest.approx(0.2, abs=1e-6), f"wrong change at seam: {rows}"
