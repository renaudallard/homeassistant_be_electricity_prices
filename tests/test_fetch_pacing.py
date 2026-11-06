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

"""What a refresh nobody asked a tick of asks for: Home Assistant's
update_entity, which any user who may control an entity can call."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, date, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers.base import CardNotReadableError
from tests import make_entry, make_snapshot, make_stub_extractor

_SPOTS = "custom_components.be_electricity_prices.coordinator_spots"
_GET = "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor"
_BACKFILL = "custom_components.be_electricity_prices.backfill_if_missing"


async def _a_plain_user(hass: HomeAssistant) -> str:
    await hass.auth.async_create_user("Owner")
    user = await hass.auth.async_create_user("Household", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    return user.id


async def _update(hass: HomeAssistant, entry: MockConfigEntry, user_id: str) -> None:
    sensor = next(
        e.entity_id
        for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if e.domain == "sensor"
    )
    await hass.services.async_call(
        "homeassistant",
        "update_entity",
        {"entity_id": sensor},
        blocking=True,
        context=Context(user_id=user_id),
    )
    await hass.async_block_till_done()


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-06 11:30:00+01:00")
async def test_update_entity_asks_entsoe_nothing_within_a_quarter_hour(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """From 11:00 every tick asked ENTSO-E for today and tomorrow until
    tomorrow's prices were out, so a plain user updating a sensor every ten
    seconds sent a day-ahead request every ten seconds."""
    asked: list[Any] = []

    async def _day_ahead(
        _key: Any, _session: Any, start: Any, end: Any, *, quarter_hourly: bool = False
    ) -> Any:
        asked.append((start, end))
        tomorrow = dt_util.start_of_local_day(dt_util.now().date() + timedelta(days=1))
        prices = {}
        hour = start
        while hour < min(end, tomorrow.astimezone(UTC)):
            prices[hour] = 0.1
            hour += timedelta(hours=1)
        return prices, "entsoe"

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Expert dynamic",
        data={
            "supplier": "custom",
            "contract": "custom_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
            "custom_energy_factor": 1.0,
            "custom_energy_base": 0.01,
        },
    )
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with (
        patch(_SPOTS + ".fetch_day_ahead_or_fallback", _day_ahead),
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", AsyncMock(return_value={})),
        patch(_SPOTS + ".fill_missing_days", AsyncMock(return_value=({}, {}, []))),
        patch(_BACKFILL, AsyncMock(return_value=None)),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        user = await _a_plain_user(hass)
        start = len(asked)
        for _ in range(6):
            freezer.tick(timedelta(seconds=11))
            await _update(hass, entry, user)
        assert len(asked) == start
        freezer.tick(timedelta(minutes=14))
        await _update(hass, entry, user)
        assert len(asked) == start + 1
        freezer.tick(timedelta(hours=1))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert len(asked) == start + 2


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-06 09:00:00+01:00")
async def test_update_entity_probes_nothing_and_a_failed_tick_stands(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """Every tick probed the supplier, so a plain user updating a sensor
    every ten seconds probed it every ten seconds. And a refresh the quarter
    of an hour holds after a failed tick tried nothing: it does not read as
    a recovery."""
    probe = AsyncMock(return_value="etag-1")
    stub = replace(
        make_stub_extractor(fetch=AsyncMock(return_value=make_snapshot())), probe=probe
    )
    entry = make_entry(contract="power_fix_one")
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with patch(_GET, return_value=stub), patch(_BACKFILL, AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        coordinator = entry.runtime_data
        user = await _a_plain_user(hass)
        start = probe.await_count
        for _ in range(6):
            freezer.tick(timedelta(seconds=11))
            await _update(hass, entry, user)
        assert probe.await_count == start
        freezer.tick(timedelta(minutes=14))
        await _update(hass, entry, user)
        assert probe.await_count == start + 1

        freezer.tick(timedelta(minutes=15))
        with patch.object(
            type(coordinator),
            "_update_body",
            AsyncMock(side_effect=UpdateFailed("down")),
        ):
            await _update(hass, entry, user)
        assert not coordinator.last_update_success
        freezer.tick(timedelta(minutes=1))
        await _update(hass, entry, user)
        assert not coordinator.last_update_success
        freezer.tick(timedelta(minutes=14))
        await _update(hass, entry, user)
        assert coordinator.last_update_success


@asynccontextmanager
async def _a_probed_entry(
    hass: HomeAssistant, fetch: Any
) -> AsyncIterator[tuple[MockConfigEntry, AsyncMock]]:
    """An entry on a supplier with a probe, set up and kept on it; the probe
    counts the full ticks."""
    probe = AsyncMock(return_value="etag-1")
    stub = replace(make_stub_extractor(fetch=fetch), probe=probe)
    entry = make_entry(contract="power_fix_one")
    entry.add_to_hass(hass)
    assert await async_setup_component(hass, "homeassistant", {})
    with patch(_GET, return_value=stub), patch(_BACKFILL, AsyncMock(return_value=None)):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield entry, probe


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-06 09:00:00+01:00")
async def test_update_entity_is_held_on_an_entry_with_nothing_to_publish(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """An entry set up on a card it cannot read has no data, and the hold
    only held entries with data: every update a plain user asked for
    probed the supplier."""
    fetch = AsyncMock(side_effect=CardNotReadableError("no text layer"))
    async with _a_probed_entry(hass, fetch) as (entry, probe):
        coordinator = entry.runtime_data
        assert coordinator.data is None
        assert probe.await_count >= 1, "the first refresh is not held"
        user = await _a_plain_user(hass)
        start = probe.await_count
        for _ in range(6):
            freezer.tick(timedelta(seconds=11))
            await _update(hass, entry, user)
        assert probe.await_count == start
        assert not coordinator.last_update_success


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-06 09:00:00+01:00")
async def test_a_forced_refresh_within_the_quarter_hour_ticks(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The refresh action and the Repairs card's retry force a fetch: held,
    a retry moments after the tick that failed would ask nobody."""
    async with _a_probed_entry(hass, AsyncMock(return_value=make_snapshot())) as (
        entry,
        probe,
    ):
        start = probe.await_count
        freezer.tick(timedelta(minutes=1))
        await entry.runtime_data.async_force_refresh(wait=True)
        assert probe.await_count == start + 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-06 09:00:00+01:00")
async def test_a_reprice_within_the_quarter_hour_ticks(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """A tick the integration asks for itself, once something it prices with
    has landed, runs whatever the time since the last one."""
    async with _a_probed_entry(hass, AsyncMock(return_value=make_snapshot())) as (
        entry,
        probe,
    ):
        start = probe.await_count
        freezer.tick(timedelta(minutes=1))
        await entry.runtime_data.async_request_reprice()
        # Past the debouncer's cooldown from the setup's own reprice, which
        # holds a request back until it ends.
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert probe.await_count == start + 1


@pytest.mark.parametrize("expected_lingering_timers", [True])
@pytest.mark.freeze_time("2026-11-30 23:58:00+01:00")
async def test_the_midnight_rebuild_follows_the_months_close(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """The month's close ticks a minute before midnight and the rebuild a
    minute after: held behind the close, the table would describe
    yesterday and today until the next hourly tick."""
    async with _a_probed_entry(hass, AsyncMock(return_value=make_snapshot())) as (
        entry,
        probe,
    ):
        start = probe.await_count
        freezer.move_to("2026-11-30 23:59:59+01:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert probe.await_count == start + 1, "the month's close"
        freezer.move_to("2026-12-01 00:01:00+01:00")
        async_fire_time_changed(hass)
        await hass.async_block_till_done()
        assert probe.await_count == start + 2, "the midnight rebuild"
        first = dt_util.as_local(min(entry.runtime_data.data.hourly))
        assert first.date() == date(2026, 12, 1)
