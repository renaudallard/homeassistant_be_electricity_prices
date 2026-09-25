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


"""A meter that resets during the day reads today what the statistics read
for it after midnight.

Home Assistant applies its reset rule to each state against the one before it:
a fall below 0.9 x the previous reading starts a new cycle counted from zero,
and the energy counted before the reset stays in the day. The live read used
to compare the current reading with the midnight one only, so a reset that
climbed back to within 10% of the midnight reading read as a dip, and one that
climbed past it read as the difference.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices import energy_meters as em

SENSOR = "sensor.daily_yield"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}

# Hours after local midnight and the reading; the first one is the day before.
CASES = {
    "climbs back within 10% of midnight": ([(-8, "20"), (7, "0"), (12, "19")], 19.0),
    "climbs back past midnight": ([(-8, "20"), (7, "0"), (9, "10"), (12, "25")], 25.0),
    "counts the energy before the reset": (
        [(-8, "20"), (5, "23"), (7, "0"), (12, "19")],
        22.0,
    ),
    "a dip and a recovery": ([(-8, "500"), (3, "499.5"), (6, "503")], 3.0),
    # A reset is judged against the reading before it, not against midnight:
    # 50 is no reset against 10 but is one against 100.
    "a reset judged against the previous reading": (
        [(-8, "10"), (3, "100"), (5, "50"), (12, "60")],
        150.0,
    ),
    # A real reset rarely lands on 0, and the new cycle counts from zero
    # whatever it lands on.
    "a reset that lands above zero": ([(-8, "20"), (7, "1.5"), (12, "4")], 4.0),
}


@pytest.mark.parametrize("name", list(CASES))
async def test_a_reset_reads_the_same_today_and_tomorrow(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any, name: str
) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    parsed = dt_util.parse_datetime("2026-07-15 00:00:00+02:00")
    assert parsed is not None
    midnight = dt_util.as_utc(parsed)
    points, expected = CASES[name]
    for hour, value in points:
        freezer.move_to(midnight + timedelta(hours=hour, minutes=1))
        hass.states.async_set(SENSOR, value, ATTRS)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
    freezer.move_to(midnight + timedelta(hours=points[-1][0], minutes=30))
    day = dt_util.as_local(midnight).date()
    today = await em._live_today_kwh(hass, SENSOR, day)

    freezer.move_to(midnight + timedelta(hours=23, minutes=59))
    start = midnight - timedelta(hours=9)
    while start < midnight + timedelta(hours=23, minutes=55):
        do_adhoc_statistics(hass, start=start)
        await async_wait_recording_done(hass)
        start += timedelta(minutes=5)
    freezer.move_to(midnight + timedelta(days=1, hours=1))
    after = (await em._recorder_daily_kwh(hass, SENSOR, day, day)).get(day)

    assert today == pytest.approx(expected)
    assert after == pytest.approx(expected)
