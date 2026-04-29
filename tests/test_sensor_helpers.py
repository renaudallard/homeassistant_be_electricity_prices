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

"""Tests for the cheapest_4h_today / most_expensive_4h_today attribute helper."""

from __future__ import annotations

from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.pricing import PriceBreakdown
from custom_components.be_electricity_prices.sensor import _today_ranked


def _today_data(prices: list[float]) -> CoordinatorData:
    """Build a CoordinatorData whose hourly map covers today, hour by hour."""
    today_local = dt_util.now().replace(minute=0, second=0, microsecond=0)
    today_midnight = today_local.replace(hour=0)
    hourly: dict[datetime, PriceBreakdown] = {}
    for hour, price in enumerate(prices):
        local = today_midnight + timedelta(hours=hour)
        hourly[dt_util.as_utc(local)] = PriceBreakdown(
            energy=price, network=0.0, taxes=0.0, all_in=price
        )
    return CoordinatorData(hourly=hourly)


def test_today_ranked_picks_n_cheapest_and_n_most_expensive() -> None:
    # 24 today-hours with strictly increasing prices: cheapest = first 4,
    # most-expensive = last 4. Both lists must come back in chronological
    # order.
    prices = [0.10 + 0.01 * i for i in range(24)]
    cheapest, most_expensive = _today_ranked(_today_data(prices), 4)

    assert [c["price"] for c in cheapest] == [0.10, 0.11, 0.12, 0.13]
    assert [c["price"] for c in most_expensive] == [0.30, 0.31, 0.32, 0.33]
    assert all(
        cheapest[i]["start"] < cheapest[i + 1]["start"]
        for i in range(len(cheapest) - 1)
    )
    assert all(
        most_expensive[i]["start"] < most_expensive[i + 1]["start"]
        for i in range(len(most_expensive) - 1)
    )


def test_today_ranked_returns_empty_lists_when_no_hours_today() -> None:
    cheapest, most_expensive = _today_ranked(CoordinatorData(), 4)
    assert cheapest == []
    assert most_expensive == []


def test_today_ranked_with_fewer_hours_than_count_overlaps() -> None:
    # Right after midnight on a static contract there may be only a couple
    # of today-hours. Both lists then describe the same hours - that's an
    # accepted property, not a bug, but pin it down so future refactors
    # don't quietly change the contract.
    cheapest, most_expensive = _today_ranked(_today_data([0.20, 0.10]), 4)
    assert len(cheapest) == 2
    assert len(most_expensive) == 2
    assert {c["price"] for c in cheapest} == {0.10, 0.20}
    assert {c["price"] for c in most_expensive} == {0.10, 0.20}
