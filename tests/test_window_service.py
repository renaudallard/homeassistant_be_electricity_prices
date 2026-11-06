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

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import async_setup
from custom_components.be_electricity_prices.const import DOMAIN, RESOLUTION_QUARTER
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.pricing import PriceBreakdown
from custom_components.be_electricity_prices.services import _find_window, _to_utc
from tests import make_entry, make_snapshot


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


def test_find_window_does_not_span_a_gap() -> None:
    # 06:00 is missing. The two cheapest slots (05:00, 07:00) are not
    # adjacent, so a 2h window must not pair them across the gap; the
    # cheapest contiguous 2h is 04:00-06:00.
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    table = _hourly([0.20, 0.20, 0.20, 0.20, 0.20, 0.01, 0.99, 0.01], start)
    del table[start + timedelta(hours=6)]
    result = _find_window(table, 2, start, None, minimize=True)
    hours = [datetime.fromisoformat(h["hour"]) for h in result["hours"]]
    assert hours[1] - hours[0] == timedelta(hours=1)  # contiguous, not gap-spanning
    assert result["average_eur_per_kwh"] == pytest.approx((0.20 + 0.01) / 2, rel=1e-6)


def test_find_window_raises_when_no_contiguous_run() -> None:
    # Every other slot missing: plenty of slots exist but no contiguous
    # pair, so a 2h request can't be satisfied.
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    table = _hourly([0.10, 0.10, 0.10, 0.10, 0.10], start)
    for h in (1, 3):
        del table[start + timedelta(hours=h)]
    with pytest.raises(ServiceValidationError, match="contiguous") as err:
        _find_window(table, 2, start, None, minimize=True)
    # Its own message: {available} is the longest run here, not the number of
    # slots in the window, which the plain one counts.
    assert err.value.translation_key == "not_enough_contiguous_hours"
    assert err.value.translation_placeholders == {"available": "1", "needed": "2"}


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
    with pytest.raises(ServiceValidationError, match="only 2 slots available"):
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


def _quarter(
    prices: list[float], start: datetime | None = None
) -> dict[datetime, PriceBreakdown]:
    """Build a contiguous 15-minute table (default start 2026-04-30 00:00 UTC)."""
    if start is None:
        start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    return {
        start + timedelta(minutes=15 * i): PriceBreakdown(
            energy=p, network=0.0, taxes=0.0, all_in=p
        )
        for i, p in enumerate(prices)
    }


def test_find_window_quarter_hour_block() -> None:
    # Eight 15-minute slots (two hours); duration_slots=4 is one hour.
    # The cheapest contiguous hour is the last four slots (0.05 each).
    prices = [0.20, 0.20, 0.20, 0.20, 0.05, 0.05, 0.05, 0.05]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    result = _find_window(
        _quarter(prices, start),
        4,
        start,
        None,
        minimize=True,
        resolution=RESOLUTION_QUARTER,
    )
    assert result["duration_hours"] == 1
    assert result["average_eur_per_kwh"] == pytest.approx(0.05)
    assert len(result["hours"]) == 4
    win_start = datetime.fromisoformat(result["start"])
    win_end = datetime.fromisoformat(result["end"])
    assert (win_end - win_start) == timedelta(hours=1)


def test_find_window_quarter_hour_starts_on_quarter_boundary() -> None:
    # Cheapest one-hour (4-slot) run is 00:45..01:45 (indices 3-6); the
    # window must be allowed to start mid-hour, which an hourly grid
    # could not express.
    prices = [0.20, 0.20, 0.20, 0.01, 0.02, 0.03, 0.04, 0.50]
    start = datetime(2026, 4, 30, 0, 0, tzinfo=UTC)
    result = _find_window(
        _quarter(prices, start),
        4,
        start,
        None,
        minimize=True,
        resolution=RESOLUTION_QUARTER,
    )
    win_start = datetime.fromisoformat(result["start"]).astimezone(UTC)
    assert win_start == start + timedelta(minutes=45)


@pytest.mark.parametrize(
    "value",
    [
        datetime(1, 1, 1),
        datetime(1, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        datetime(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12))),
    ],
)
def test_a_date_with_no_utc_time_is_refused(value: datetime) -> None:
    """1 January of year 1 in Brussels, or east of UTC, and 31 December 9999
    west of it have no UTC time. The conversion raised OverflowError, which
    Home Assistant logs with a traceback for any user calling the action."""
    with pytest.raises(ServiceValidationError) as raised:
        _to_utc(value)
    assert raised.value.translation_key == "datetime_out_of_range"
    assert raised.value.translation_placeholders == {"value": value.isoformat()}


def test_dates_within_range_convert_as_before() -> None:
    naive = datetime(2026, 4, 30, 20, 0)
    assert _to_utc(naive) == datetime(2026, 4, 30, 18, 0, tzinfo=UTC)
    edge = datetime(1, 1, 1, 12, 0, tzinfo=UTC)
    assert _to_utc(edge) == edge


@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_the_window_actions_refuse_a_year_1_bound(
    hass: HomeAssistant, hass_read_only_user: Any
) -> None:
    """Called by a read-only user, as the dashboards do: the bound that has no
    UTC time is a validation error, not an unexpected one."""
    assert await async_setup(hass, {})
    entry = make_entry(contract="power_fix_one")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(make_snapshot())
    start = dt_util.start_of_local_day().astimezone(UTC)
    coord.data = SimpleNamespace(  # type: ignore[assignment]
        hourly=_hourly([0.1 + i / 1000 for i in range(48)], start),
        resolution="hourly",
    )
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = coord
    context = Context(user_id=hass_read_only_user.id)
    for service, field, value in (
        ("cheapest_window", "earliest_start", "0001-01-01T00:00:00"),
        ("most_expensive_window", "latest_end", "9999-12-31T23:59:00-12:00"),
    ):
        with pytest.raises(ServiceValidationError) as raised:
            await hass.services.async_call(
                DOMAIN,
                service,
                {"duration_hours": 2, field: value},
                blocking=True,
                context=context,
                return_response=True,
            )
        assert raised.value.translation_key == "datetime_out_of_range"
    answer = await hass.services.async_call(
        DOMAIN,
        "cheapest_window",
        {"duration_hours": 2, "earliest_start": start.isoformat()},
        blocking=True,
        context=context,
        return_response=True,
    )
    assert answer is not None
    assert answer["duration_hours"] == 2
