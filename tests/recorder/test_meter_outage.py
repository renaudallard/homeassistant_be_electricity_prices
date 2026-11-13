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

"""A meter outage's energy is measured once.

Home Assistant resumes a total_increasing sensor's sum from the last
five-minute row it compiled, so the first day back holds the energy of the
days without rows. The measured window shares it out over them. Past
purge_keep_days the last row is gone and the sum starts over: the first day
back's negative change is dropped, its energy lost, and the days stay
missing.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.be_electricity_prices.meter_daily import _measured_total

METER = "sensor.grid_import"
ATTRIBUTES = {
    "device_class": "energy",
    "state_class": "total_increasing",
    "unit_of_measurement": "kWh",
}


async def _meter(
    hass: HomeAssistant, freezer: Any, days: int, down: range, *, purge: bool
) -> None:
    """24 kWh a day from 5 January, one compile an hour, unavailable on the
    days in ``down``, the recorder purged each evening when ``purge``."""
    await hass.config.async_set_time_zone("Europe/Brussels")
    assert await async_setup_component(hass, "sensor", {})
    start = dt_util.as_utc(dt_util.start_of_local_day(date(2026, 1, 5)))
    for hour in range(days * 24):
        slot = start + timedelta(hours=hour, minutes=55)
        freezer.move_to(slot + timedelta(seconds=30))
        value = "unavailable" if hour // 24 in down else str(5000.0 + hour)
        hass.states.async_set(METER, value, ATTRIBUTES)
        await async_wait_recording_done(hass)
        freezer.move_to(slot + timedelta(minutes=5, seconds=10))
        do_adhoc_statistics(hass, start=slot)
        await async_wait_recording_done(hass)
        if purge and hour % 24 == 20:
            await hass.services.async_call(
                "recorder", "purge", {"keep_days": 10, "repack": False}, blocking=True
            )
            await async_wait_recording_done(hass)
            await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(days=days + 2))


async def test_a_short_outage_is_measured_once(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """Down on 7 and 8 January: the 9th holds their 48 kWh, and the week from
    the 6th is 168 kWh over seven days, not over six scaled to seven."""
    await _meter(hass, freezer, 8, range(2, 4), purge=False)
    measured = await _measured_total(hass, METER, date(2026, 1, 6), date(2026, 1, 12))
    assert measured.kwh == 168.0
    assert measured.days_with_data == 7


async def test_a_window_beginning_in_an_outage_measures_it_once(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """Down on 7 and 8 January: the 7th keeps a row, at 0, for the hour the
    meter went away in, and the 8th has none. A window from the 8th read
    from the 8th itself found the 9th's 72 kWh with no day before it to
    share them from: 144 kWh over four days of five, scaled to 180 where
    120 were used. Read from before the outage, the 9th is shared with the
    8th, and the window holds five days. What the 7th used while the meter
    was away is in those two shares, its own row reading 0."""
    await _meter(hass, freezer, 8, range(2, 4), purge=False)
    measured = await _measured_total(hass, METER, date(2026, 1, 8), date(2026, 1, 12))
    assert measured.kwh == 144.0
    assert measured.days_with_data == 5
    # From the first day back, which a read from that day dropped whole.
    measured = await _measured_total(hass, METER, date(2026, 1, 9), date(2026, 1, 12))
    assert measured.kwh == 108.0
    assert measured.days_with_data == 4
    # A window past the outage reads its own days and nothing of the lead.
    measured = await _measured_total(hass, METER, date(2026, 1, 10), date(2026, 1, 12))
    assert measured.kwh == 72.0
    assert measured.days_with_data == 3


async def test_an_outage_past_the_purge_stays_missing(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    """Down from 7 to 18 January with the purge running: the sum starts over
    on the 19th, whose change is dropped, and the 20th's day is not shared
    over the lost ones."""
    await _meter(hass, freezer, 18, range(2, 14), purge=True)
    measured = await _measured_total(hass, METER, date(2026, 1, 6), date(2026, 1, 22))
    assert measured.kwh == 96.0
    assert measured.days_with_data == 5
