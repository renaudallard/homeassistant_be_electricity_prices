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

"""Tests for the cheapest_window / most_expensive_window helper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.be_electricity_prices import _find_window
from custom_components.be_electricity_prices.pricing import PriceBreakdown


def _hourly(
    prices: list[float], start: datetime | None = None
) -> dict[datetime, PriceBreakdown]:
    """Build a contiguous hourly table starting at ``start`` (default: 2026-04-30 00:00 UTC)."""
    if start is None:
        start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    return {
        start + timedelta(hours=i): PriceBreakdown(
            energy=p, network=0.0, taxes=0.0, all_in=p
        )
        for i, p in enumerate(prices)
    }


def test_find_window_picks_cheapest_3h_block() -> None:
    # Strictly increasing then decreasing prices: cheapest 3h is the
    # decreasing tail (hours 5-7 with values 0.06, 0.05, 0.04).
    prices = [0.10, 0.12, 0.14, 0.16, 0.18, 0.06, 0.05, 0.04]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    result = _find_window(_hourly(prices, start), 3, start, None, minimize=True)
    assert result["duration_hours"] == 3
    assert result["average_eur_per_kwh"] == pytest.approx(
        (0.06 + 0.05 + 0.04) / 3, rel=1e-6
    )
    assert len(result["hours"]) == 3


def test_find_window_picks_most_expensive_3h_block() -> None:
    prices = [0.10, 0.12, 0.14, 0.16, 0.18, 0.06, 0.05, 0.04]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    result = _find_window(_hourly(prices, start), 3, start, None, minimize=False)
    assert result["average_eur_per_kwh"] == pytest.approx(
        (0.14 + 0.16 + 0.18) / 3, rel=1e-6
    )


def test_find_window_respects_earliest_start() -> None:
    # Hours 0-3 are cheapest (0.05 each); earliest=hour 4 forces the
    # later, more expensive 0.10-block to win.
    prices = [0.05, 0.05, 0.05, 0.05, 0.10, 0.10, 0.10, 0.10]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    earliest = start + timedelta(hours=4)
    result = _find_window(_hourly(prices, start), 3, earliest, None, minimize=True)
    assert result["average_eur_per_kwh"] == pytest.approx(0.10)


def test_find_window_respects_latest_end() -> None:
    # Hours 4-7 are cheapest (0.05 each); latest_end at hour 4 (exclusive)
    # forces the earlier 0.10-block.
    prices = [0.10, 0.10, 0.10, 0.10, 0.05, 0.05, 0.05, 0.05]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    latest = start + timedelta(hours=4)
    result = _find_window(_hourly(prices, start), 3, start, latest, minimize=True)
    assert result["average_eur_per_kwh"] == pytest.approx(0.10)


def test_find_window_raises_when_too_few_hours() -> None:
    from homeassistant.exceptions import ServiceValidationError

    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    with pytest.raises(ServiceValidationError, match="only 2 hours available"):
        _find_window(_hourly([0.10, 0.10], start), 4, start, None, minimize=True)


def test_find_window_truncates_earliest_to_top_of_hour() -> None:
    # An earliest_utc with minutes set still lets the matching hour bucket
    # in (we anchor by truncating to :00).
    prices = [0.10, 0.05, 0.10]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    result = _find_window(
        _hourly(prices, start),
        1,
        start + timedelta(minutes=45),
        None,
        minimize=True,
    )
    assert result["average_eur_per_kwh"] == pytest.approx(0.05)
