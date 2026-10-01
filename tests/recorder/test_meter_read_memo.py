"""A window answered from a wider read is what the recorder itself returns.

A tick reads each meter once, hour by hour over the widest window it needs,
and answers every narrower hourly or daily read from those rows
(``energy_meters._recorder_rows``, issue #107). That is only right if the
rows match the recorder's own answer to the narrower query exactly, change
included, so this asks a real recorder both ways, over both clock changes,
a gap with no rows, a falling sum and a meter recorded in Wh.
"""

from __future__ import annotations

import random
from datetime import UTC, date, timedelta
from typing import Any
from unittest.mock import patch

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

SENSOR = "sensor.meter_wh"
FIRST = date(2025, 10, 20)
LAST = date(2026, 4, 5)


async def _import(hass: HomeAssistant) -> None:
    hass.states.async_set(
        SENSOR,
        "0",
        {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "Wh",
        },
    )
    meta = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=SENSOR,
        unit_class="energy",
        unit_of_measurement="Wh",
    )
    rng = random.Random(107)
    rows: list[StatisticData] = []
    when = dt_util.start_of_local_day(FIRST).astimezone(UTC)
    stop = dt_util.start_of_local_day(LAST + timedelta(days=1)).astimezone(UTC)
    total = 123456.789
    gap = (
        dt_util.start_of_local_day(date(2026, 1, 10)).astimezone(UTC),
        dt_util.start_of_local_day(date(2026, 1, 13)).astimezone(UTC),
    )
    fall = dt_util.start_of_local_day(date(2026, 2, 2)).astimezone(UTC) + timedelta(
        hours=9
    )
    while when < stop:
        if not gap[0] <= when < gap[1]:
            total += rng.choice((0.0, 0.0, 137.3, 411.7, 1023.9))
            if when == fall:
                total -= 50000.0
            rows.append(StatisticData(start=when, state=total, sum=total))
        when += timedelta(hours=1)
    async_import_statistics(hass, meta, rows)
    await async_wait_recording_done(hass)


def _windows() -> list[tuple[date, date]]:
    rng = random.Random(7)
    span = (LAST - FIRST).days
    out = [
        (date(2025, 10, 25), date(2025, 10, 27)),
        (date(2026, 3, 28), date(2026, 3, 30)),
        (date(2026, 1, 9), date(2026, 1, 14)),
        (date(2026, 1, 11), date(2026, 1, 12)),
        (date(2026, 2, 1), date(2026, 2, 3)),
        (FIRST + timedelta(days=1), LAST),
        (FIRST, LAST),
    ]
    for _ in range(40):
        a = FIRST + timedelta(days=rng.randrange(span))
        out.append((a, a + timedelta(days=rng.randrange((LAST - a).days + 1))))
    return out


async def test_a_narrower_window_reads_as_the_recorder_reads_it(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    await _import(hass)
    fields = {"change", "sum"}

    direct: dict[tuple[date, date, str], list[Any]] = {}
    for start, end in _windows():
        for period in ("hour", "day"):
            direct[(start, end, period)] = await em._recorder_rows(
                hass, SENSOR, start, end, period, fields
            )
    assert direct[(FIRST, LAST, "hour")], "the import is not read back"

    queried: list[tuple[date, date, str]] = []
    real = em._query_rows

    async def _counted(
        hass: HomeAssistant,
        entity_id: str,
        start: date,
        end: date,
        period: str,
        fields: frozenset[str],
    ) -> list[Any] | None:
        queried.append((start, end, period))
        return await real(hass, entity_id, start, end, period, fields)

    with patch.object(em, "_query_rows", _counted), em.memoise_meter_reads({}):
        await em._recorder_rows(hass, SENSOR, FIRST, LAST, "hour", fields)
        for (start, end, period), expected in direct.items():
            served = await em._recorder_rows(hass, SENSOR, start, end, period, fields)
            assert served == expected, (start, end, period)

    # Read once, but for a daily window starting on the read's first day,
    # whose first change only the recorder can seed, or ending on its last,
    # whose query runs a day past the hours held.
    assert queried[0] == (FIRST, LAST, "hour")
    assert set(queried[1:]) == {
        (start, end, "day")
        for start, end in _windows()
        if start == FIRST or end == LAST
    }


async def test_a_failed_read_is_not_kept_as_an_answer(
    recorder_mock: Any, hass: HomeAssistant
) -> None:
    """A recorder that could not answer is asked again, not believed empty
    for the rest of the tick."""
    await hass.config.async_set_time_zone("Europe/Brussels")
    calls: list[int] = []

    async def _fails(*_a: Any) -> list[Any] | None:
        calls.append(1)
        return None

    with patch.object(em, "_query_rows", _fails), em.memoise_meter_reads({}):
        assert await em._recorder_rows(hass, SENSOR, FIRST, LAST, "hour") == []
        assert await em._recorder_rows(hass, SENSOR, FIRST, LAST, "hour") == []
    assert len(calls) == 2
