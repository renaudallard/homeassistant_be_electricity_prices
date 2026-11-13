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

"""Sharing a meter outage's energy out over its days."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import energy_meters
from custom_components.be_electricity_prices.meter_daily import _daily_kwh
from custom_components.be_electricity_prices.meter_outages import (
    LEAD_DAYS,
    carries_outages,
    note_restart,
    noting_restarts,
    spread_outages,
)
from custom_components.be_electricity_prices.year_ahead import YEAR_AHEAD, YearAhead


def test_the_first_day_back_is_shared_over_the_outage() -> None:
    days = {date(2026, 1, n): 10.0 for n in range(1, 11) if n not in (4, 5)}
    days[date(2026, 1, 6)] = 30.0
    assert spread_outages(days, set()) == {date(2026, 1, n): 10.0 for n in range(1, 11)}


def test_an_outage_whose_sum_started_over_stays_missing() -> None:
    """A restart inside the outage lost its energy, and the days before the
    first one on record are none of an outage's."""
    days = {date(2026, 1, n): 10.0 for n in range(3, 11) if n not in (5, 6, 7)}
    assert spread_outages(days, {date(2026, 1, 7)}) == days


async def test_only_a_total_increasing_meter_carries_an_outage(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.a", "1", {"state_class": "total_increasing"})
    hass.states.async_set("sensor.b", "1", {"state_class": "total"})
    assert carries_outages(hass, "sensor.a")
    assert not carries_outages(hass, "sensor.b")
    assert not carries_outages(hass, "sensor.c")


async def test_a_meter_not_loaded_yet_is_judged_on_its_registry_entry(
    hass: HomeAssistant,
) -> None:
    """At startup the meter's integration may not have written a state yet:
    the class its registry entry kept says whether it carries an outage."""
    registry = er.async_get(hass)
    carrying = registry.async_get_or_create(
        "sensor", "p1", "import", capabilities={"state_class": "total_increasing"}
    )
    cycling = registry.async_get_or_create(
        "sensor", "p1", "cycle", capabilities={"state_class": "total"}
    )
    assert hass.states.get(carrying.entity_id) is None
    assert carries_outages(hass, carrying.entity_id)
    assert not carries_outages(hass, cycling.entity_id)
    # A state, once written, is what the meter says now.
    hass.states.async_set(carrying.entity_id, "1", {"state_class": "total"})
    assert not carries_outages(hass, carrying.entity_id)


async def test_a_restart_read_off_last_year_lands_on_the_day_read(
    hass: HomeAssistant,
) -> None:
    """On a read ahead, 3 January 2027 is read off 3 January 2026, and a sum
    started over on the latter starts it over on the former."""

    async def read(
        _hass: Any, _entity_id: str, start: date, _end: date, _period: str
    ) -> list[tuple[datetime, float]]:
        if start.year == 2026:
            note_restart(date(2026, 1, 3))
        return []

    ahead = YearAhead(pivot=date(2027, 1, 1), card=cast(Any, None))
    token = YEAR_AHEAD.set(ahead)
    try:
        with (
            patch.object(energy_meters, "_read_deltas", new=read),
            patch.object(energy_meters, "_read_ahead", return_value=True),
            noting_restarts() as restarts,
        ):
            await energy_meters._recorder_deltas(
                hass, "sensor.a", date(2027, 1, 1), date(2027, 1, 5), "day"
            )
    finally:
        YEAR_AHEAD.reset(token)
    assert restarts == {date(2027, 1, 3)}


async def test_a_carrying_meter_is_read_from_before_its_window(
    hass: HomeAssistant,
) -> None:
    """The meter went away during 31 December and came back on 4 January: a
    window from 1 January is read from LEAD_DAYS before it, so the 31st is
    on record and the 4th is shared out over the three days without rows.
    Only the window's days come back. A meter that does not carry an outage
    is read from the window's own first day."""
    asked: list[date] = []

    async def read(
        _hass: Any, _entity_id: str, start: date, _end: date
    ) -> dict[date, float]:
        asked.append(start)
        days = {date(2025, 12, n): 10.0 for n in range(25, 32)}
        days |= {date(2026, 1, 4): 40.0, date(2026, 1, 5): 10.0}
        return {day: kwh for day, kwh in days.items() if day >= start}

    hass.states.async_set("sensor.a", "1", {"state_class": "total_increasing"})
    hass.states.async_set("sensor.b", "1", {"state_class": "total"})
    window = (date(2026, 1, 1), date(2026, 1, 5))
    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=read),
        patch.object(dt_util, "now", return_value=datetime(2026, 2, 1, tzinfo=UTC)),
    ):
        carried = await _daily_kwh(hass, "sensor.a", *window)
        as_read = await _daily_kwh(hass, "sensor.b", *window)
    assert asked == [date(2026, 1, 1) - timedelta(days=LEAD_DAYS), date(2026, 1, 1)]
    assert carried == {date(2026, 1, n): 10.0 for n in range(1, 6)}
    assert as_read == {date(2026, 1, 4): 40.0, date(2026, 1, 5): 10.0}


async def test_a_first_bucket_dropped_for_what_it_carries_is_noted(
    hass: HomeAssistant,
) -> None:
    """A daily read whose first row holds energy from before the read drops
    it. The days before it are then no outage to share a later day over,
    which matters where rows of another read come ahead of it, as on a read
    ahead."""
    first = dt_util.start_of_local_day(date(2026, 1, 9)).astimezone(UTC)
    rows = [
        {"start": first.timestamp(), "sum": 5_072.0, "change": 72.0},
        {
            "start": (first + timedelta(days=1)).timestamp(),
            "sum": 5_096.0,
            "change": 24.0,
        },
    ]

    async def recorder_rows(*_args: object, **_kwargs: object) -> list[Any]:
        return rows

    with (
        patch.object(energy_meters, "_recorder_rows", new=recorder_rows),
        noting_restarts() as restarts,
    ):
        read = await energy_meters._read_deltas(
            hass, "sensor.a", date(2026, 1, 9), date(2026, 1, 10), "day"
        )
    assert [kwh for _when, kwh in read] == [24.0]
    assert restarts == {date(2026, 1, 9)}
    days = {date(2026, 1, 6): 24.0, date(2026, 1, 10): 24.0}
    assert spread_outages(days, restarts) == days
