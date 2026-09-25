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

"""A meter that dips below its midnight reading reads the same day the same way
today and after midnight.

Home Assistant calls a fall of a ``total_increasing`` sensor a reset only below
0.9 x the previous reading; a smaller fall is a dip, which the statistics carry
as a negative ``change`` that the past days drop. The live read used to take
any fall as a reset and billed the whole register as today's consumption.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices import energy_meters as em

SENSOR = "sensor.grid_meter"
ATTRS = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}


async def test_a_dip_reads_the_same_today_and_tomorrow(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    parsed = dt_util.parse_datetime("2026-07-15 07:00:00+02:00")
    assert parsed is not None
    base = dt_util.as_utc(parsed)
    # 12345,60 at midnight, then a 0,01 kWh dip.
    for when, value in (
        (base - timedelta(hours=8), "12345.60"),
        (base - timedelta(hours=2, minutes=-1), "12345.60"),
        (base - timedelta(hours=1, minutes=-30), "12345.59"),
    ):
        freezer.move_to(when)
        hass.states.async_set(SENSOR, value, ATTRS)
        await hass.async_block_till_done()
        await async_wait_recording_done(hass)
    freezer.move_to(base + timedelta(minutes=10))
    for hour in (base - timedelta(hours=2), base - timedelta(hours=1)):
        for minute in range(0, 60, 5):
            do_adhoc_statistics(hass, start=hour + timedelta(minutes=minute))
            await async_wait_recording_done(hass)
    day = dt_util.as_local(base).date()

    today = (await em._recorder_daily_kwh(hass, SENSOR, day, day)).get(day, 0.0)
    freezer.move_to(base + timedelta(days=1))
    after = (await em._recorder_daily_kwh(hass, SENSOR, day, day)).get(day, 0.0)

    assert after == 0.0
    assert today == after
