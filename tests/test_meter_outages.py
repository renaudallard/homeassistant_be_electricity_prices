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

from datetime import date, datetime
from typing import Any, cast
from unittest.mock import patch

from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices import energy_meters
from custom_components.be_electricity_prices.meter_outages import (
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
