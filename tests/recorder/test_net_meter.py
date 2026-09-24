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

"""A register that falls reads the same day the same way, today and after.

Past days come from the recorder's statistics, where a negative ``change`` is
dropped (a sum-chain restart looks exactly like one, see discussion #66).
Today comes off the live meter. A ``total`` register that nets export against
consumption falls whenever the site exports more than it draws, and the live
read used to bill that fall signed: the day read -3 kWh until midnight and 0
from then on, so current_year_cost stepped up overnight by the day's export.
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

SENSOR = "sensor.net_meter"
ATTRS = {
    "device_class": "energy",
    "state_class": "total",
    "unit_of_measurement": "kWh",
}


async def test_a_falling_register_reads_the_same_today_and_tomorrow(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    parsed = dt_util.parse_datetime("2026-07-15 07:00:00+02:00")
    assert parsed is not None
    base = dt_util.as_utc(parsed)
    # 100 at midnight, then +2 in one hour and -5 in the next: a net fall of 3.
    for when, value in (
        (base - timedelta(hours=8), "100"),
        (base - timedelta(hours=2, minutes=-1), "100"),
        (base - timedelta(hours=2, minutes=-30), "102"),
        (base - timedelta(hours=1, minutes=-1), "101"),
        (base - timedelta(hours=1, minutes=-30), "97"),
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
