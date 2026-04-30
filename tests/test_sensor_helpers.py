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
from custom_components.be_electricity_prices.sensor import (
    _split_today_tomorrow,
    _today_ranked,
)


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


def test_today_ranked_lists_are_disjoint_when_few_hours() -> None:
    # Right after midnight on a static contract there may be only a couple
    # of today-hours. The cheapest list takes its share first; the
    # most-expensive list gets only what remains, so the two lists never
    # share an hour.
    cheapest, most_expensive = _today_ranked(_today_data([0.20, 0.10]), 4)
    assert {c["price"] for c in cheapest} == {0.10, 0.20}
    assert most_expensive == []


def test_today_ranked_partitions_when_count_falls_in_middle() -> None:
    # 6 hours, count=4: cheapest takes the first 4, most-expensive takes
    # the remaining 2. Together they cover all hours exactly once.
    cheapest, most_expensive = _today_ranked(
        _today_data([0.10, 0.12, 0.14, 0.16, 0.18, 0.20]), 4
    )
    assert {c["price"] for c in cheapest} == {0.10, 0.12, 0.14, 0.16}
    assert {c["price"] for c in most_expensive} == {0.18, 0.20}
    starts = {c["start"] for c in cheapest} | {c["start"] for c in most_expensive}
    assert len(starts) == 6


def _today_and_tomorrow_data(
    today_prices: list[float], tomorrow_prices: list[float]
) -> CoordinatorData:
    """Build a CoordinatorData spanning both today and tomorrow."""
    midnight_today = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    hourly: dict[datetime, PriceBreakdown] = {}
    for hour, price in enumerate(today_prices):
        local = midnight_today + timedelta(hours=hour)
        hourly[dt_util.as_utc(local)] = PriceBreakdown(
            energy=price, network=0.0, taxes=0.0, all_in=price
        )
    for hour, price in enumerate(tomorrow_prices):
        local = midnight_today + timedelta(days=1, hours=hour)
        hourly[dt_util.as_utc(local)] = PriceBreakdown(
            energy=price, network=0.0, taxes=0.0, all_in=price
        )
    return CoordinatorData(hourly=hourly)


def test_split_today_tomorrow_buckets_hours_by_local_date() -> None:
    data = _today_and_tomorrow_data([0.10] * 24, [0.20] * 24)
    today, tomorrow = _split_today_tomorrow(data)
    assert len(today) == 24
    assert len(tomorrow) == 24
    assert {row["all_in"] for row in today} == {0.10}
    assert {row["all_in"] for row in tomorrow} == {0.20}
    # Both lists are chronological.
    assert all(today[i]["start"] < today[i + 1]["start"] for i in range(23))
    assert all(tomorrow[i]["start"] < tomorrow[i + 1]["start"] for i in range(23))


def test_split_today_tomorrow_returns_empty_tomorrow_before_publication() -> None:
    # Static contracts only have today's hours until ENTSO-E publishes
    # tomorrow at ~13:00 CET; tomorrow stays empty until then.
    data = _today_and_tomorrow_data([0.10] * 24, [])
    today, tomorrow = _split_today_tomorrow(data)
    assert len(today) == 24
    assert tomorrow == []


def test_split_today_tomorrow_handles_empty_data() -> None:
    today, tomorrow = _split_today_tomorrow(CoordinatorData())
    assert today == []
    assert tomorrow == []
