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

"""Tests for force-refresh and the stale-snapshot repair issue."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
    _shared_snapshots,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
        title="Eneco - Eneco Zon & Wind Vast (Wallonia)",
    )


async def test_force_refresh_drops_caches_and_requests_update(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot_fetched_at = object()  # type: ignore[assignment]
    coord._spot_cache = {object(): 0.10}  # type: ignore[dict-item]
    coord._spot_cache_day = date(2026, 4, 29)
    coord._spot_cache_includes_tomorrow = True
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.async_force_refresh()

    assert coord._snapshot_fetched_at is None
    assert coord._spot_cache == {}
    assert coord._spot_cache_day is None
    assert coord._spot_cache_includes_tomorrow is False
    coord.async_request_refresh.assert_awaited_once()


def _fake_snapshot(supplier: str = "eneco") -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier=supplier,
        contract="power_fix",
        energy=FixedRates(single=0.18),
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://",
        fetched_at_iso="2026-04-30T12:00:00+00:00",
    )


async def test_two_coordinators_share_snapshot_and_only_fetch_once(
    hass: HomeAssistant,
) -> None:
    """Two entries pointing at the same (supplier, contract, region) must
    share the snapshot — extractor.fetch may run for the first one only."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)

    fetched = _fake_snapshot()
    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return fetched

    extractor = type("E", (), {"fetch": staticmethod(_fake_fetch)})
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()
        await coord_b._maybe_refresh_snapshot()

    assert fetch_calls == 1
    assert coord_a._snapshot is fetched
    assert coord_b._snapshot is fetched


async def test_force_refresh_evicts_shared_cache_for_other_coordinator(
    hass: HomeAssistant,
) -> None:
    """async_force_refresh on entry A must evict the shared (supplier,
    contract, region) entry, so entry B's next refresh re-fetches."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)
    coord_a.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = type("E", (), {"fetch": staticmethod(_fake_fetch)})
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()  # populates the shared cache
        assert fetch_calls == 1
        await coord_a.async_force_refresh()  # evicts; calls async_request_refresh
        await coord_b._maybe_refresh_snapshot()  # must re-fetch
        assert fetch_calls == 2


async def test_shared_cache_expires_after_ttl(hass: HomeAssistant) -> None:
    """Snapshots older than SNAPSHOT_REFRESH_HOURS (24h) must be re-fetched."""
    entry_a = _entry()
    entry_a.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry_a)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = type("E", (), {"fetch": staticmethod(_fake_fetch)})
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()
        # Hand-age the shared entry past the TTL.
        cache = _shared_snapshots(hass)
        key = coord._shared_key()
        cache[key].fetched_at = dt_util.utcnow().replace(year=2020)
        coord._snapshot_fetched_at = cache[key].fetched_at
        await coord._maybe_refresh_snapshot()
        assert fetch_calls == 2


async def test_sync_stale_issue_creates_and_clears(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"snapshot_stale_{entry.entry_id}"

    coord._sync_stale_issue(True)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    coord._sync_stale_issue(False)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


# ---- kWh state listener / bucket splitting ---------------------------------


def _entry_with_totals() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "bi",
            "consumption_kwh": "sensor.total_cons",
            "injection_kwh": "sensor.total_inj",
        },
        title="Eneco - Eneco Zon & Wind Vast (Wallonia)",
    )


async def test_kwh_listener_first_state_only_records_baseline(
    hass: HomeAssistant,
) -> None:
    """The first state event after setup is just a baseline -- no delta
    is bucketed because we have no previous value to subtract from."""
    entry = _entry_with_totals()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.async_setup_kwh_listeners()

    hass.states.async_set("sensor.total_cons", "1000.0")
    await hass.async_block_till_done()

    assert coord._kwh_baselines["sensor.total_cons"] == 1000.0
    assert coord._kwh_buckets["consumption_day"] == 0.0
    assert coord._kwh_buckets["consumption_night"] == 0.0


async def test_kwh_listener_routes_delta_to_correct_band(
    hass: HomeAssistant,
) -> None:
    """A delta during a bi-hourly peak hour goes to the day bucket; a
    delta during off-peak goes to the night bucket."""
    entry = _entry_with_totals()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.async_setup_kwh_listeners()

    # Seed baselines.
    hass.states.async_set("sensor.total_cons", "1000.0")
    hass.states.async_set("sensor.total_inj", "200.0")
    await hass.async_block_till_done()

    # Patch is_offpeak so the test is independent of wall-clock time.
    with patch(
        "custom_components.be_electricity_prices.coordinator.is_offpeak",
        return_value=False,
    ):
        hass.states.async_set("sensor.total_cons", "1010.0")
        hass.states.async_set("sensor.total_inj", "205.0")
        await hass.async_block_till_done()
    with patch(
        "custom_components.be_electricity_prices.coordinator.is_offpeak",
        return_value=True,
    ):
        hass.states.async_set("sensor.total_cons", "1014.0")
        hass.states.async_set("sensor.total_inj", "210.0")
        await hass.async_block_till_done()

    assert coord._kwh_buckets["consumption_day"] == pytest.approx(10.0)
    assert coord._kwh_buckets["consumption_night"] == pytest.approx(4.0)
    assert coord._kwh_buckets["injection_day"] == pytest.approx(5.0)
    assert coord._kwh_buckets["injection_night"] == pytest.approx(5.0)


async def test_kwh_listener_handles_counter_reset(hass: HomeAssistant) -> None:
    """A counter going backwards (utility_meter reset, sensor swap) must
    re-baseline silently -- no negative bucket entry."""
    entry = _entry_with_totals()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.async_setup_kwh_listeners()

    hass.states.async_set("sensor.total_cons", "1000.0")
    await hass.async_block_till_done()
    with patch(
        "custom_components.be_electricity_prices.coordinator.is_offpeak",
        return_value=False,
    ):
        hass.states.async_set("sensor.total_cons", "1050.0")
        await hass.async_block_till_done()
        hass.states.async_set("sensor.total_cons", "5.0")  # reset
        await hass.async_block_till_done()
        hass.states.async_set("sensor.total_cons", "12.0")  # post-reset delta
        await hass.async_block_till_done()

    # 50 from before the reset, 7 after -> 57 total in the day bucket.
    assert coord._kwh_buckets["consumption_day"] == pytest.approx(57.0)


async def test_kwh_listener_ignores_unavailable_states(hass: HomeAssistant) -> None:
    entry = _entry_with_totals()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.async_setup_kwh_listeners()

    hass.states.async_set("sensor.total_cons", "unavailable")
    hass.states.async_set("sensor.total_cons", "unknown")
    hass.states.async_set("sensor.total_cons", "")
    await hass.async_block_till_done()

    assert "sensor.total_cons" not in coord._kwh_baselines
    assert coord._kwh_buckets["consumption_day"] == 0.0


async def test_kwh_listener_teardown_unsubscribes(hass: HomeAssistant) -> None:
    entry = _entry_with_totals()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord.async_setup_kwh_listeners()
    assert coord._kwh_unsub  # subscribed

    coord.async_teardown_kwh_listeners()
    assert not coord._kwh_unsub

    # Subsequent state changes do not feed the bucket.
    hass.states.async_set("sensor.total_cons", "100")
    await hass.async_block_till_done()
    assert "sensor.total_cons" not in coord._kwh_baselines
