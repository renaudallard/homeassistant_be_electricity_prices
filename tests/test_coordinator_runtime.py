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

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
    _monthly_snapshots,
    _shared_failed_fetches,
    _shared_lock,
    _shared_snapshots,
    evict_shared_caches,
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


async def test_probe_match_skips_fetch(hass: HomeAssistant) -> None:
    """When extractor.probe returns the same key on a subsequent refresh,
    the coordinator must NOT call extractor.fetch again."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0
    probe_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return "key-stable"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # first call - fetch
        await coord._maybe_refresh_snapshot()  # probe says unchanged - no fetch
        await coord._maybe_refresh_snapshot()  # idem
    assert fetch_calls == 1
    assert probe_calls == 3
    assert coord._snapshot_probe_key == "key-stable"


async def test_probe_mismatch_triggers_fetch(hass: HomeAssistant) -> None:
    """When extractor.probe returns a different key, the coordinator
    must refetch even if the snapshot is still within TTL."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0
    keys = iter(["key-A", "key-B"])

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return next(keys)

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # key-A, fetch
        await coord._maybe_refresh_snapshot()  # key-B, refetch
    assert fetch_calls == 2
    assert coord._snapshot_probe_key == "key-B"


async def test_probe_none_falls_back_to_ttl(hass: HomeAssistant) -> None:
    """A None probe (extractor doesn't expose one, or probe failed) keeps
    the existing 24h-TTL behaviour: don't refetch within the window."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return None  # no probe available

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # fetch, fresh
        await coord._maybe_refresh_snapshot()  # within TTL, no fetch
        # Hand-age past TTL: must refetch even though probe returned None.
        coord._snapshot_fetched_at = dt_util.utcnow().replace(year=2020)
        _shared_snapshots(hass)[
            coord._shared_key()
        ].fetched_at = coord._snapshot_fetched_at
        await coord._maybe_refresh_snapshot()
    assert fetch_calls == 2


async def test_probe_match_on_shared_cache_avoids_fetch(hass: HomeAssistant) -> None:
    """A second coordinator with the same shared key must adopt the
    sibling's snapshot when its probe returns the matching key, even
    if its own snapshot is None."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "shared-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()  # populates cache + probe key
        await coord_b._maybe_refresh_snapshot()  # adopts via probe-key match
    assert fetch_calls == 1
    assert coord_b._snapshot_probe_key == "shared-key"


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


async def test_evict_shared_caches_drops_rows_for_tuple(hass: HomeAssistant) -> None:
    """evict_shared_caches must remove every cache row pinned to the
    given (supplier, contract, region) tuple, leaving rows for other
    tuples untouched."""
    from custom_components.be_electricity_prices.coordinator import _SharedSnapshot

    key_us = ("eneco", "power_fix", "wallonia")
    key_other = ("bolt", "bolt_fix", "wallonia")

    snap = _fake_snapshot()
    fetched_at = dt_util.utcnow()
    _shared_snapshots(hass)[key_us] = _SharedSnapshot(
        snapshot=snap, fetched_at=fetched_at, probe_key="ours"
    )
    _shared_snapshots(hass)[key_other] = _SharedSnapshot(
        snapshot=snap, fetched_at=fetched_at, probe_key="theirs"
    )
    _shared_failed_fetches(hass)[key_us] = (fetched_at, "ours-error")
    _shared_failed_fetches(hass)[key_other] = (fetched_at, "theirs-error")
    monthly = _monthly_snapshots(hass)
    monthly[("eneco", "power_fix", "wallonia", "2026-01")] = snap
    monthly[("bolt", "bolt_fix", "wallonia", "2026-01")] = snap

    evict_shared_caches(hass, key_us, "eneco")

    assert key_us not in _shared_snapshots(hass)
    assert key_other in _shared_snapshots(hass)  # other tuple preserved
    assert key_us not in _shared_failed_fetches(hass)
    assert key_other in _shared_failed_fetches(hass)
    assert ("eneco", "power_fix", "wallonia", "2026-01") not in _monthly_snapshots(hass)
    assert ("bolt", "bolt_fix", "wallonia", "2026-01") in _monthly_snapshots(hass)


async def test_evict_shared_caches_keeps_held_lock(hass: HomeAssistant) -> None:
    """A held lock must NOT be popped during eviction; otherwise a
    re-created entry on the same tuple would get a fresh lock and the
    dedup property would silently break."""
    key = ("eneco", "power_fix", "wallonia")
    lock = _shared_lock(hass, key)
    await lock.acquire()
    try:
        evict_shared_caches(hass, key, "eneco")
        # Held lock stays in the bucket: future _shared_lock(hass, key)
        # must return the same Lock object.
        assert _shared_lock(hass, key) is lock
    finally:
        lock.release()


async def test_async_remove_entry_clears_stale_issue(hass: HomeAssistant) -> None:
    """async_remove_entry must drop the per-entry repair issue so it
    doesn't linger after the entry that owns it is gone."""
    from custom_components.be_electricity_prices import async_remove_entry

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._sync_stale_issue(True)
    registry = ir.async_get(hass)
    issue_id = f"snapshot_stale_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    await async_remove_entry(hass, entry)

    assert registry.async_get_issue(DOMAIN, issue_id) is None
