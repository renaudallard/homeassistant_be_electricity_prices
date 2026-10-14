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

"""A second recorded switch leaves the published reset and the bill alone.

A household that ticked "bill from contract start" after its first switch left
the first contract out of the year. Recording another switch unticks the box
on the entry; had the first contract come back, the bill would have grown by
its cost and `last_reset` would have moved back to 1 January while the sensor
did not reset, which the compiler reads as a new cycle. Uses the real
recorder, since the defect lives in how HA compiles what the sensor reports.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.cohort import ytd_window_start
from custom_components.be_electricity_prices.const import (
    CONF_YTD_FROM_CONTRACT_START,
    DOMAIN,
)
from custom_components.be_electricity_prices.contract_periods import (
    previous_periods,
)
from custom_components.be_electricity_prices.coordinator_data import (
    ytd_window_reset,
)
from custom_components.be_electricity_prices.flow_switch import _record_switch
from tests.recorder.test_backfill_year_join import _hours, _live

BXL = ZoneInfo("Europe/Brussels")
WHEN = datetime(2026, 9, 10, 12, tzinfo=BXL)
# The year to date with the first contract left out, and what that contract
# cost from 1 January to 31 March.
BILL = 600.0
FIRST = 300.0


def _published(data: dict[str, Any], drift: float) -> tuple[str, datetime]:
    """The sensor's state and reset for ``data``: the bill, with the first
    contract's cost when the periods priced bring it back, plus ``drift``."""
    entry = MockConfigEntry(domain=DOMAIN, data=data)
    today = WHEN.date()
    periods = previous_periods(data, ytd_window_start(entry, today), today)
    first = any(p.data["supplier"] == "eneco" for p in periods)
    bill = BILL + (FIRST if first else 0.0) + drift
    return f"{bill:.1f}", ytd_window_reset(entry, WHEN).astimezone(UTC)


async def test_a_second_switch_moves_neither_bill_nor_reset(
    recorder_mock: Any, hass: HomeAssistant, freezer: Any
) -> None:
    base = {
        "supplier": "eneco",
        "contract": "power_fix",
        "region": "wallonia",
        "dso": "ores",
        "meter": "mono",
    }
    one = {
        **_record_switch(base, date(2026, 4, 1)),
        "supplier": "engie",
        "contract": "engie_easy_fixed",
        CONF_YTD_FROM_CONTRACT_START: True,
    }
    two = {
        **_record_switch(one, date(2026, 9, 1)),
        "supplier": "luminus",
        "contract": "luminus_comfy",
    }
    assert await async_setup_component(hass, "sensor", {})
    await hass.async_block_till_done()
    t = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)
    value, reset = _published(one, 0.0)
    await _live(hass, freezer, t + timedelta(minutes=1), value, reset)
    value, reset = _published(one, 1.0)
    await _live(hass, freezer, t + timedelta(minutes=56), value, reset)
    value, reset = _published(two, 1.5)
    await _live(hass, freezer, t + timedelta(hours=1, minutes=1), value, reset)
    value, reset = _published(two, 2.0)
    await _live(hass, freezer, t + timedelta(hours=1, minutes=56), value, reset)
    got = await hass.async_add_executor_job(_hours, hass, t)
    assert [r[3] for r in got] == pytest.approx([1.0, 1.0]), got
