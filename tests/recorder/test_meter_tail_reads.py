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

"""A tick that reads only the last days again bills what a full read bills.

The coordinator keeps each meter's hourly rows and an ordinary tick reads
only the days from the day before yesterday on (``meter_warm``, issue #107).
This asks a real recorder both ways over a run of ticks: a new day, hours
compiled late inside the re-read days, an hour corrected further back (which
shows at the next day's full read, as documented), a sum chain moved under the
kept rows, a meter reset, 1 January, both clock changes, a gap with no rows
and a meter recorded in Wh. Every window the tick derives has to match.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.components.recorder import get_instance
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
from custom_components.be_electricity_prices import meter_warm as mw
from custom_components.be_electricity_prices.const import (
    CONF_DAY_CONSUMPTION_KWH,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_SOLAR_REGIME,
    SOLAR_REGIME_NONE,
)

# Two tests record a year and a half of hourly statistics into a real
# recorder: 206 s and 126 s on a loaded Raspberry Pi 4, past the suite's 60 s.
pytestmark = pytest.mark.timeout(600)

DAY = "sensor.import_day"
NIGHT = "sensor.import_night_wh"
FIRST = date(2024, 9, 20)
LAST = date(2026, 4, 5)
FIELDS = frozenset({"change", "sum"})
ENTRY = SimpleNamespace(
    data={
        CONF_DAY_CONSUMPTION_KWH: DAY,
        CONF_NIGHT_CONSUMPTION_KWH: NIGHT,
        CONF_SOLAR_REGIME: SOLAR_REGIME_NONE,
    }
)


def _meta(entity_id: str, unit: str) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=entity_id,
        unit_class="energy",
        unit_of_measurement=unit,
    )


def _hour(day: date, hour: int) -> datetime:
    return dt_util.start_of_local_day(day).astimezone(UTC) + timedelta(hours=hour)


def _series(seed: int, scale: float) -> list[StatisticData]:
    """A year and a half of hours with a gap and a meter reset in it."""
    rng = random.Random(seed)
    gap = (_hour(date(2025, 12, 20), 0), _hour(date(2025, 12, 22), 6))
    reset = _hour(date(2026, 1, 1), 5)
    rows: list[StatisticData] = []
    when = _hour(FIRST, 0)
    stop = _hour(LAST + timedelta(days=1), 0)
    total = 4321.5 * scale
    while when < stop:
        if not gap[0] <= when < gap[1]:
            total += rng.choice((0.0, 0.12, 0.31, 0.87, 1.4)) * scale
            if when == reset:
                total = 0.25 * scale
            rows.append(StatisticData(start=when, state=total, sum=total))
        when += timedelta(hours=1)
    return rows


async def _setup(hass: HomeAssistant) -> dict[str, list[StatisticData]]:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    series = {DAY: _series(1, 1.0), NIGHT: _series(2, 1000.0)}
    for entity_id, unit in ((DAY, "kWh"), (NIGHT, "Wh")):
        hass.states.async_set(
            entity_id,
            "0",
            {
                "device_class": "energy",
                "state_class": "total_increasing",
                "unit_of_measurement": unit,
            },
        )
        async_import_statistics(hass, _meta(entity_id, unit), series[entity_id])
    await async_wait_recording_done(hass)
    return series


async def _rewrite(
    hass: HomeAssistant, entity_id: str, rows: list[StatisticData]
) -> None:
    unit = "Wh" if entity_id == NIGHT else "kWh"
    async_import_statistics(hass, _meta(entity_id, unit), rows)
    await async_wait_recording_done(hass)


def _windows(today: date) -> list[tuple[date, date]]:
    year = date(today.year, 1, 1)
    return [
        (today - timedelta(days=364), today),
        (year, today),
        (year, today - timedelta(days=1)),
        (today.replace(day=1), today),
        (today - timedelta(days=3), today),
        (today - timedelta(days=1), today),
        (today, today),
        (today - timedelta(days=40), today - timedelta(days=10)),
    ]


async def _tick(
    hass: HomeAssistant,
    freezer: Any,
    today: date,
    kept: dict[str, mw.KeptRows] | None,
) -> tuple[dict[str, list[Any]], dict[Any, Any], list[tuple[date, date]]]:
    """One tick's rows and every window it derives from them, and the
    windows the recorder was asked for."""
    freezer.move_to(
        dt_util.start_of_local_day(today).astimezone(UTC) + timedelta(hours=10)
    )
    asked: list[tuple[date, date]] = []
    real = em._query_rows

    async def _counted(*args: Any) -> list[Any] | None:
        asked.append((args[2], args[3]))
        return await real(*args)

    memo: dict[Any, Any] = {}
    derived: dict[Any, Any] = {}
    with patch.object(em, "_query_rows", _counted), em.memoise_meter_reads(memo):
        await mw.warm_meter_reads(
            hass,
            ENTRY,  # type: ignore[arg-type]
            today - timedelta(days=370),
            today,
            kept,
        )
        warmed = list(asked)
        for entity_id in (DAY, NIGHT):
            for start, end in _windows(today):
                for period in ("hour", "day"):
                    derived[(entity_id, start, end, period)] = await em._recorder_rows(
                        hass, entity_id, start, end, period, set(FIELDS)
                    )
                derived[(entity_id, start, end, "deltas")] = await em._read_deltas(
                    hass, entity_id, start, end, "hour"
                )
    rows = {e: memo[("rows", e, FIELDS)][0][3] for e in (DAY, NIGHT)}
    return rows, derived, warmed


async def _both(
    hass: HomeAssistant,
    freezer: Any,
    today: date,
    kept: dict[str, mw.KeptRows],
) -> list[tuple[date, date]]:
    """Tick with the kept rows and without, and require the same."""
    full_rows, full, _ = await _tick(hass, freezer, today, None)
    rows, derived, asked = await _tick(hass, freezer, today, kept)
    assert rows == full_rows, today
    assert derived == full, today
    return asked


async def test_reading_the_last_days_again_bills_what_a_full_read_bills(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    series = await _setup(hass)
    kept: dict[str, mw.KeptRows] = {}

    async def both(today: date) -> list[tuple[date, date]]:
        return await _both(hass, freezer, today, kept)

    tail = (date(2025, 12, 28), date(2025, 12, 30))
    year = (date(2025, 12, 30) - timedelta(days=370), date(2025, 12, 30))

    # The first tick reads the year; a later one the same day only the tail.
    assert await both(date(2025, 12, 30)) == [year, year]
    assert await both(date(2025, 12, 30)) == [tail, tail]

    # An hour compiled late inside the re-read days is read again.
    late = _hour(date(2025, 12, 29), 10)
    rows = [r for r in series[DAY] if r["start"] >= late]
    bumped = [
        StatisticData(start=r["start"], state=r["sum"] + 2.0, sum=r["sum"] + 2.0)
        for r in rows
    ]
    await _rewrite(hass, DAY, bumped)
    series[DAY] = [r for r in series[DAY] if r["start"] < late] + bumped
    assert await both(date(2025, 12, 30)) == [tail, tail]

    # An hour corrected further back leaves the chain joined, so it is not
    # seen until the next day's full read: the one documented difference.
    old = _hour(date(2025, 11, 15), 8)
    fixed = [r for r in series[DAY] if r["start"] == old][0]
    await _rewrite(
        hass,
        DAY,
        [StatisticData(start=old, state=fixed["sum"] - 0.5, sum=fixed["sum"] - 0.5)],
    )
    today = date(2025, 12, 30)
    full_rows, _, _ = await _tick(hass, freezer, today, None)
    spliced, _, _ = await _tick(hass, freezer, today, kept)
    moved = {r["start"] for r in full_rows[DAY]} - {
        r["start"] for a, r in zip(spliced[DAY], full_rows[DAY], strict=False) if a == r
    }
    assert moved == {old.timestamp(), (old + timedelta(hours=1)).timestamp()}
    assert spliced[NIGHT] == full_rows[NIGHT]
    assert await both(date(2025, 12, 31))  # the next day reads the year again

    # A chain moved under the kept rows does not join them: read in full.
    shift = _hour(date(2025, 12, 1), 0)
    moved_rows = [
        StatisticData(start=r["start"], state=r["sum"] + 500.0, sum=r["sum"] + 500.0)
        for r in series[NIGHT]
        if r["start"] >= shift
    ]
    await _rewrite(hass, NIGHT, moved_rows)
    series[NIGHT] = [r for r in series[NIGHT] if r["start"] < shift] + moved_rows
    asked = await both(date(2025, 12, 31))
    tail = (date(2025, 12, 29), date(2025, 12, 31))
    year = (date(2025, 12, 31) - timedelta(days=370), date(2025, 12, 31))
    assert asked == [tail, tail, year]

    # 1 January with the meter reset in the re-read days, and both clock
    # changes, each a new day and then a tail tick.
    for today in (
        date(2026, 1, 1),
        date(2026, 1, 2),
        date(2025, 10, 26),
        date(2025, 10, 27),
        date(2026, 3, 29),
        date(2026, 3, 30),
    ):
        await both(today)
        await both(today)


async def test_a_meter_with_no_row_in_the_last_days_is_read_in_full(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """Re-read days holding no row say nothing about the kept ones.

    Taken as joined, they kept a deleted statistic billing until midnight,
    the old id of a renamed one, and missed hours an importer filled in
    behind the window while the meter was silent.
    """
    series = await _setup(hass)
    kept: dict[str, mw.KeptRows] = {}
    instance = get_instance(hass)
    # The meters stopped after LAST: from LAST + 3 the re-read days are empty.
    today = LAST + timedelta(days=4)
    year = (today - timedelta(days=370), today)
    tail = (today - timedelta(days=2), today)
    assert await _both(hass, freezer, today, kept) == [year, year]
    assert await _both(hass, freezer, today, kept) == [tail, year, tail, year]

    # Hours imported behind the window while the meter was silent, all of
    # them before the re-read days.
    behind = [
        StatisticData(start=_hour(LAST + timedelta(days=1), h), state=s, sum=s)
        for h, s in enumerate(series[DAY][-1]["sum"] + 0.5 * (n + 1) for n in range(24))
    ]
    await _rewrite(hass, DAY, behind)
    await _both(hass, freezer, today, kept)

    # A statistic deleted.
    instance.async_clear_statistics([NIGHT])
    await async_wait_recording_done(hass)
    await _both(hass, freezer, today, kept)

    # A statistic renamed: the entry still names the old id.
    instance.async_update_statistics_metadata(
        DAY, new_statistic_id="sensor.import_day_renamed"
    )
    await async_wait_recording_done(hass)
    await _both(hass, freezer, today, kept)
