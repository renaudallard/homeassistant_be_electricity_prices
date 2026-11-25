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

"""The bill_cost action: a closed run of days priced as one window of the
year-to-date bill (``bill_cost.py``)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import bill_cost, energy_meters, ytd_cost
from custom_components.be_electricity_prices.const import (
    CONF_PREVIOUS_CONTRACTS,
    DOMAIN,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers._rates import FixedRates
from tests import make_entry, make_snapshot, make_stub_extractor

SNAP = make_snapshot(energy=FixedRates(single=0.30))

# December 2025 banks 5 kWh a day; January 2026 draws 10 kWh a day.
DECEMBER = date(2025, 12, 1)
JANUARY = date(2026, 1, 1)
LAST = date(2026, 1, 31)


async def _daily(_h: Any, entity_id: str, start: date, end: date) -> dict[date, float]:
    out: dict[date, float] = {}
    day = max(start, DECEMBER)
    while day <= min(end, LAST):
        december = day < JANUARY
        if entity_id == "sensor.cons":
            out[day] = 5.0 if december else 10.0
        elif entity_id == "sensor.inj":
            out[day] = 10.0 if december else 0.0
        day += timedelta(days=1)
    return out


async def _entry(hass: HomeAssistant, **extra: Any) -> MockConfigEntry:
    from custom_components.be_electricity_prices import async_setup

    assert await async_setup(hass, {})
    entry = make_entry(
        solar_regime="compensation",
        solar_kva=4.0,
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
        **extra,
    )
    entry.add_to_hass(hass)
    coordinator = BePricesCoordinator(hass, entry)
    coordinator._set_snapshot(SNAP)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    entry.runtime_data = coordinator
    return entry


@contextmanager
def _offline(
    curves: Mapping[int, dict[tuple[int, int, int], float] | None],
) -> Iterator[Any]:
    """No network: the stub supplier, the sheets and the profiles held, the
    load curve of each year ``curves`` gives."""
    holds: list[int] = []

    async def spots(coordinator: Any, *_a: Any) -> Any:
        holds.append(coordinator._spot_prune_holds)
        return {}, {}

    async def curve(_self: Any, year: int, _blend: str) -> Any:
        return curves.get(year)

    sheets = AsyncMock()
    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=_daily),
        patch.object(bill_cost, "get_extractor", return_value=make_stub_extractor()),
        patch.object(bill_cost, "_ensure_dynamic_spots", new=spots),
        patch.object(bill_cost, "ensure_year_sheets", new=sheets),
        patch.object(BePricesCoordinator, "_ensure_rlp_weights", AsyncMock()),
        patch.object(BePricesCoordinator, "rlp_weights_for_year", new=curve),
    ):
        yield holds, sheets


async def _cost(
    hass: HomeAssistant, entry_id: str, start: str, end: str, user: str | None = None
) -> dict[str, Any]:
    response = await hass.services.async_call(
        DOMAIN,
        "bill_cost",
        {"entry_id": entry_id, "start": start, "end": end},
        blocking=True,
        return_response=True,
        context=Context(user_id=user),
    )
    assert isinstance(response, dict)
    return response


def _walk(hass: HomeAssistant, entry: MockConfigEntry, **window: Any) -> Any:
    return ytd_cost._compute_current_year_cost(
        hass,
        cast(aiohttp.ClientSession, None),
        make_stub_extractor(),
        SNAP,
        entry,
        **window,
    )


async def test_a_bill_across_new_year_nets_once(
    hass: HomeAssistant, hass_admin_user: Any, freezer: Any
) -> None:
    """The surplus December banked pays for January, as the readout closing
    the bill settles it: one window, not a year each, and the day-ahead the
    walk reads is held from the prune while it runs."""
    freezer.move_to("2026-03-20 12:00:00+01:00")
    entry = await _entry(hass)
    with _offline({}) as (holds, sheets):
        got = await _cost(
            hass, entry.entry_id, "2025-12-01", "2026-01-31", hass_admin_user.id
        )
        whole = await _walk(
            hass, entry, window_start_override=DECEMBER, window_end=LAST
        )
        split = await _walk(
            hass, entry, window_start_override=DECEMBER, window_end=date(2025, 12, 31)
        ) + await _walk(hass, entry, window_start_override=JANUARY, window_end=LAST)
    assert got["cost"] == round(whole, 2)
    # 155 kWh banked in December, worth 0,30 EUR/kWh of energy at least.
    assert split - whole > 40.0
    assert got["covered"] is True
    assert got["days"] == 62
    assert got["load_profile"] is False
    assert holds == [1]
    assert entry.runtime_data._spot_prune_holds == 0
    sheets.assert_awaited_once()
    assert sheets.await_args.args[3] == [2025, 2026]


async def test_each_hour_is_weighted_on_its_own_years_curve(
    hass: HomeAssistant, hass_admin_user: Any, freezer: Any
) -> None:
    freezer.move_to("2026-03-20 12:00:00+01:00")
    entry = await _entry(hass)
    flat = {
        (m, d, h): 1.0 for m in range(1, 13) for d in range(1, 32) for h in range(24)
    }
    curves = {2025: flat, 2026: {key: 2.0 for key in flat}}
    seen: dict[str, Any] = {}
    walk = ytd_cost._compute_current_year_cost

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return await walk(*args, **kwargs)

    with _offline(curves), patch.object(bill_cost, "_compute_current_year_cost", spy):
        got = await _cost(
            hass, entry.entry_id, "2025-12-01", "2026-01-31", hass_admin_user.id
        )
    assert got["load_profile"] is True
    weights = seen["rlp_weights"]
    assert weights[(12, 31, 23)] == 1.0
    assert weights[(1, 1, 0)] == 2.0
    # Whole months only: nothing of November or February.
    assert (11, 30, 12) not in weights and (2, 1, 0) not in weights


async def test_a_year_without_a_curve_prices_as_metered(
    hass: HomeAssistant, hass_admin_user: Any, freezer: Any
) -> None:
    freezer.move_to("2026-03-20 12:00:00+01:00")
    entry = await _entry(hass)
    flat = {
        (m, d, h): 1.0 for m in range(1, 13) for d in range(1, 32) for h in range(24)
    }
    with _offline({2026: flat}):
        got = await _cost(
            hass, entry.entry_id, "2025-12-01", "2026-01-31", hass_admin_user.id
        )
    assert got["load_profile"] is False


async def test_the_curve_of_a_window_over_a_year() -> None:
    """Past a year a key names two hours, and the later one's year weights
    both. The rest of a month the window starts or ends in comes from the
    month's own year, and never over a day of the window."""

    def year(value: float) -> dict[tuple[int, int, int], float]:
        return {
            (m, d, h): value
            for m in range(1, 13)
            for d in range(1, 32)
            for h in range(24)
        }

    async def curve(y: int) -> dict[tuple[int, int, int], float]:
        return year(float(y - 2024))

    got = await bill_cost.window_curve(
        curve, date(2025, 2, 6), date(2026, 2, 5), bill_cost._local_key
    )
    assert got is not None
    # 10 February 2025 is in the window; 10 February 2026 only fills the month.
    assert got[(2, 10, 12)] == 1.0
    assert got[(2, 3, 12)] == 2.0
    assert got[(7, 1, 12)] == 1.0

    # Three days of March weight the whole of March, for its month mean.
    march = await bill_cost.window_curve(
        curve, date(2025, 3, 10), date(2025, 3, 12), bill_cost._local_key
    )
    assert march is not None
    assert (3, 1, 0) in march and (3, 31, 23) in march
    assert (4, 1, 0) not in march and (2, 28, 23) not in march

    async def none(_y: int) -> None:
        return None

    assert (
        await bill_cost.window_curve(
            none, date(2025, 2, 6), date(2025, 2, 7), bill_cost._local_key
        )
        is None
    )


async def test_the_counted_days_say_whether_the_meters_cover_the_bill(
    hass: HomeAssistant, hass_admin_user: Any, freezer: Any
) -> None:
    """November has no reading: the cost is the walk's, but not covered."""
    freezer.move_to("2026-03-20 12:00:00+01:00")
    entry = await _entry(hass)
    with _offline({}):
        got = await _cost(
            hass, entry.entry_id, "2025-11-01", "2026-01-31", hass_admin_user.id
        )
    assert got["covered"] is False


async def test_days_that_cannot_be_priced_are_refused(
    hass: HomeAssistant, hass_admin_user: Any, hass_read_only_user: Any, freezer: Any
) -> None:
    freezer.move_to("2026-03-20 12:00:00+01:00")
    entry = await _entry(
        hass,
        **{
            CONF_PREVIOUS_CONTRACTS: [
                {
                    "until": "2026-01-15",
                    "data": dict(make_entry(contract="power_flex").data),
                }
            ]
        },
    )
    admin = hass_admin_user.id
    for start, end, key in (
        ("2026-01-20", "2026-01-19", "window_reversed"),
        ("2026-03-01", "2026-03-20", "window_not_closed"),
        ("2022-01-31", "2022-02-28", "window_too_early"),
        ("2023-01-01", "2025-01-01", "window_too_long"),
        ("2026-01-14", "2026-01-31", "window_before_switch"),
    ):
        with pytest.raises(ServiceValidationError) as err:
            await _cost(hass, entry.entry_id, start, end, admin)
        assert err.value.translation_key == key, (start, end)
    with _offline({}):
        await _cost(hass, entry.entry_id, "2026-01-15", "2026-03-19", admin)
    with pytest.raises(ServiceValidationError) as err:
        await _cost(hass, "nope", "2026-01-15", "2026-01-31", admin)
    assert err.value.translation_key == "no_loaded_entry_with_id"
    # The meters' history says when a household is away.
    with pytest.raises(Unauthorized):
        await _cost(
            hass, entry.entry_id, "2026-01-15", "2026-01-31", hass_read_only_user.id
        )


async def test_the_floor_and_the_longest_window_are_allowed(freezer: Any) -> None:
    freezer.move_to("2026-03-20 12:00:00+01:00")
    today = date(2026, 3, 20)
    bill_cost.check_window({}, date(2022, 2, 1), date(2022, 2, 28), today)
    start = date(2024, 1, 1)
    bill_cost.check_window(
        {}, start, start + timedelta(days=bill_cost.MAX_DAYS - 1), today
    )
    bill_cost.check_window({}, date(2026, 3, 19), date(2026, 3, 19), today)


def test_the_hours_of_a_day_follow_the_clock() -> None:
    """Brussels' autumn change: 25 hours on 25 October 2026."""
    held = dt_util.DEFAULT_TIME_ZONE
    zone = dt_util.get_time_zone("Europe/Brussels")
    assert zone is not None
    dt_util.set_default_time_zone(zone)
    try:
        hours = bill_cost._hours(date(2026, 10, 25), date(2026, 10, 25))
    finally:
        dt_util.set_default_time_zone(held)
    assert len(hours) == 25
    assert all(isinstance(hour, datetime) for hour in hours)
