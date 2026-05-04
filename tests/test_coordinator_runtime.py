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

from datetime import date, timedelta
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
    ExtractorError,
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
    sentinel = object()
    coord._snapshot_fetched_at = sentinel  # type: ignore[assignment]
    coord._spot_cache = {object(): 0.10}  # type: ignore[dict-item]
    coord._spot_cache_day = date(2026, 4, 29)
    coord._spot_cache_includes_tomorrow = True
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.async_force_refresh()

    # fetched_at is intentionally preserved so _save_persistent can
    # still write the cached snapshot if the forced refresh fails.
    assert coord._snapshot_fetched_at is sentinel
    assert coord._force_refresh is True
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


async def test_force_refresh_keeps_snapshot_when_refetch_fails(
    hass: HomeAssistant,
) -> None:
    """A failing forced refetch must not blank the cached snapshot or
    its fetched_at marker, so _save_persistent can still write the
    blob to disk and survive an HA restart before the next attempt."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    initial_call = True

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        if initial_call:
            return _fake_snapshot()
        raise ExtractorError("boom")

    extractor = type("E", (), {"fetch": staticmethod(_fake_fetch)})
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()
    assert coord._snapshot is not None
    initial_fetched_at = coord._snapshot_fetched_at
    initial_snapshot = coord._snapshot

    await coord.async_force_refresh()
    assert coord._force_refresh is True

    initial_call = False
    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    # Force flag remains set so the next tick retries; cached snapshot
    # is intact so _save_persistent can still write it.
    assert coord._force_refresh is True
    assert coord._snapshot is initial_snapshot
    assert coord._snapshot_fetched_at is initial_fetched_at


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


async def test_probe_none_self_fresh_does_not_reset_fetched_at(
    hass: HomeAssistant,
) -> None:
    """The TTL fallback must elapse based on the *real* fetch time.

    A persisted snapshot loaded from disk with the shared cache empty
    (typical state right after an HA restart) hits the self-fresh
    branch in _maybe_refresh_snapshot. That branch used to stamp
    _snapshot_fetched_at = now on every tick that passed the TTL
    check, which reset the TTL clock and the supplier was never
    re-fetched. Probe-less suppliers must keep the original
    fetched_at so the 24h TTL actually triggers.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return None  # probe-less supplier (or probe failed)

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    # Simulate a post-restart state: snapshot loaded from disk, shared
    # cache (in-memory) empty. fetched_at is well within TTL.
    coord._snapshot = _fake_snapshot()
    original_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    coord._snapshot_fetched_at = original_fetched_at

    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    # The self-fresh return must not move fetched_at forward; doing so
    # resets the TTL clock and the snapshot would never expire.
    assert coord._snapshot_fetched_at == original_fetched_at


async def test_self_fresh_populates_empty_shared_cache(
    hass: HomeAssistant,
) -> None:
    """Post-restart, the shared cache is empty; the self-fresh return
    must populate it so a sibling coord on the same tuple can adopt
    without re-running its own probe / TTL check on every tick."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "stable-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    coord._snapshot = _fake_snapshot()
    coord._snapshot_probe_key = "stable-key"
    coord._snapshot_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    assert _shared_snapshots(hass).get(coord._shared_key()) is None

    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    shared = _shared_snapshots(hass).get(coord._shared_key())
    assert shared is not None
    assert shared.snapshot is coord._snapshot
    assert shared.probe_key == "stable-key"


async def test_probe_match_self_fresh_refreshes_fetched_at(
    hass: HomeAssistant,
) -> None:
    """Probe-based suppliers can stamp fetched_at on a probe match -- we
    just verified the supplier hasn't published a new card, so the
    snapshot_age sensor should reset to "just checked"."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "stable-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    coord._snapshot = _fake_snapshot()
    coord._snapshot_probe_key = "stable-key"
    old_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    coord._snapshot_fetched_at = old_fetched_at

    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    assert coord._snapshot_fetched_at is not None
    assert coord._snapshot_fetched_at > old_fetched_at


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


async def test_sync_extractor_failed_issue_creates_and_clears(
    hass: HomeAssistant,
) -> None:
    """A persistent ExtractorError from the supplier path must surface
    as a Repairs entry the user can act on, and clear the moment a
    refresh succeeds."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"extractor_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._sync_extractor_failed_issue("could not parse Eneco fixed energy block")
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "extractor_failed"
    assert "Eneco fixed" in (issue.translation_placeholders or {}).get("error", "")

    coord._sync_extractor_failed_issue(None)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_sync_entsoe_auth_issue_creates_and_clears(
    hass: HomeAssistant,
) -> None:
    """An ENTSO-E 401 must raise an ERROR-severity Repairs entry that
    points the user at rotating the API key, distinct from transient
    network issues which the coordinator absorbs silently."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"entsoe_auth_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._sync_entsoe_auth_issue(True, "ENTSO-E rejected the API key")
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "entsoe_auth_failed"

    coord._sync_entsoe_auth_issue(False)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_async_remove_entry_clears_all_repair_issues(
    hass: HomeAssistant,
) -> None:
    """All three issue kinds (snapshot_stale, extractor_failed,
    entsoe_auth_failed) embed the entry id, so async_remove_entry
    must clear each of them or they'd linger forever."""
    from custom_components.be_electricity_prices import async_remove_entry

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._sync_stale_issue(True)
    coord._sync_extractor_failed_issue("boom")
    coord._sync_entsoe_auth_issue(True, "401")

    registry = ir.async_get(hass)
    for kind in ("snapshot_stale", "extractor_failed", "entsoe_auth_failed"):
        assert registry.async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}") is not None

    await async_remove_entry(hass, entry)

    for kind in ("snapshot_stale", "extractor_failed", "entsoe_auth_failed"):
        assert registry.async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}") is None


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


async def test_save_persistent_skipped_after_runtime_data_swapped(
    hass: HomeAssistant,
) -> None:
    """A slow tick that finishes after the entry has been reloaded
    (runtime_data points at a fresh coordinator) must not overwrite
    the new coord's saved file."""
    entry = _entry()
    entry.add_to_hass(hass)
    old_coord = BePricesCoordinator(hass, entry)
    new_coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = new_coord  # simulate post-reload state

    saved = False

    async def _fake_save(_payload: object) -> None:
        nonlocal saved
        saved = True

    with patch.object(old_coord._store, "async_save", new=_fake_save):
        await old_coord._save_persistent()

    assert saved is False, "obsolete coordinator must not overwrite the cache file"


async def test_save_persistent_runs_during_first_refresh(
    hass: HomeAssistant,
) -> None:
    """Regression: BePricesCoordinator.async_config_entry_first_refresh
    triggers _save_persistent before HA's setup hook assigns
    entry.runtime_data. The identity guard must not raise (older
    runtime_data was unset; recent HA cores expose UNDEFINED) and
    must allow the save to proceed."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    # Do not assign entry.runtime_data — that's the pre-first-refresh
    # state. The coordinator's _snapshot is None too, so the file
    # carries only the peak/identity payload, but the call must not
    # raise.
    saved_payload: dict[str, object] | None = None

    async def _fake_save(payload: dict[str, object]) -> None:
        nonlocal saved_payload
        saved_payload = payload

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    assert saved_payload is not None, (
        "first-refresh save must succeed (runtime_data not yet assigned)"
    )
    assert saved_payload["entry_supplier"] == entry.data["supplier"]


async def test_save_persistent_stamps_construction_tuple_not_current_entry_data(
    hass: HomeAssistant,
) -> None:
    """OptionsFlow mutates entry.data via async_update_entry before the
    reload listener swaps runtime_data. A slow tick on the OLD
    coordinator that resumes in that window must stamp the OLD tuple
    (the snapshot's actual provenance) so the next load can detect the
    mismatch and discard, instead of stamping the NEW tuple over the
    OLD snapshot and serving it as fresh on next boot."""
    entry = _entry()
    entry.add_to_hass(hass)
    old_coord = BePricesCoordinator(hass, entry)

    # Simulate OptionsFlow: entry.data swapped to a different supplier.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "supplier": "bolt", "contract": "bolt_fix"},
    )

    saved_payload: dict[str, object] | None = None

    async def _fake_save(payload: dict[str, object]) -> None:
        nonlocal saved_payload
        saved_payload = payload

    with patch.object(old_coord._store, "async_save", new=_fake_save):
        await old_coord._save_persistent()

    assert saved_payload is not None
    assert saved_payload["entry_supplier"] == "eneco"
    assert saved_payload["entry_contract"] == "power_fix"


async def test_load_persistent_discards_blob_for_other_supplier(
    hass: HomeAssistant,
) -> None:
    """async_load_persistent must reject a cached snapshot whose
    persisted (supplier, contract, region) tuple differs from the
    entry's current data, so an OptionsFlow change followed by a
    restart does not serve the previous supplier's rates."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",  # entry currently configured for eneco
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    stale_payload = {
        "entry_supplier": "bolt",  # blob written under a different supplier
        "entry_contract": "bolt_fix",
        "entry_region": "wallonia",
        "snapshot": {
            "_cached_at": "2026-04-30T12:00:00+00:00",
            "_probe_key": "stale",
            "_schema_version": 7,
            "supplier": "bolt",
            "contract": "bolt_fix",
            "energy_kind": "fixed",
            "energy": {"single": 0.18},
            "dsos": {"ores": {"distribution_single": 0.10, "transport": 0.0145}},
            "taxes": {},
            "source_url": "test://",
            "publication_label": "april 2026",
            "valid_until": None,
            "injection": None,
        },
    }

    async def _fake_load() -> dict[str, object]:
        return stale_payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    assert coord._snapshot is None
    assert coord._snapshot_fetched_at is None


async def test_evict_bumps_tuple_generation_blocks_inflight_write(
    hass: HomeAssistant,
) -> None:
    """A coroutine mid-fetch when eviction runs must NOT re-create the
    cache row on resume, otherwise the row would orphan and a future
    re-add of the same tuple could read stale data."""
    from custom_components.be_electricity_prices.coordinator import (
        _bump_tuple_generation,
        _shared_failed_fetches,
        _tuple_generation,
        evict_shared_caches,
    )

    key = ("eneco", "power_fix", "wallonia")
    gen_before = _tuple_generation(hass, key)

    # Simulate an in-flight cache writer that captured the generation
    # at lock entry, then the user removed the entry mid-fetch.
    gen_at_entry = gen_before
    evict_shared_caches(hass, key, "eneco")
    gen_after = _tuple_generation(hass, key)

    assert gen_after > gen_at_entry, "eviction must bump the tuple generation"

    # The writer's resume-side guard would compare the generation;
    # confirm that comparison rejects the write.
    assert _tuple_generation(hass, key) != gen_at_entry

    # And the explicit bump helper increments by one.
    _bump_tuple_generation(hass, key)
    assert _tuple_generation(hass, key) == gen_after + 1

    # Sanity: the failed-fetch bucket can be empty without this
    # affecting the generation.
    assert _shared_failed_fetches(hass).get(key) is None


async def test_first_refresh_end_to_end_does_not_crash(hass: HomeAssistant) -> None:
    """Regression for the v0.5.14 production crash: drive the actual
    coordinator tick (refresh → update → save → load) with a mocked
    extractor while ``entry.runtime_data`` is unset. The chain must
    complete without raising AttributeError or comparing against an
    UNDEFINED sentinel.

    The previous regression test only called _save_persistent directly,
    which masked the production failure mode -- runtime_data being
    unset *because* we are inside the very first refresh entry-point."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    snap = SupplierSnapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.18),
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://",
    )

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        return snap

    extractor = type(
        "E",
        (),
        {
            "id": "eneco",
            "fetch": staticmethod(_fake_fetch),
            "probe": None,
            "fetch_for_month": None,
        },
    )
    # entry.runtime_data is intentionally NOT assigned -- this is the
    # state HA core is in before async_setup_entry's coordinator =
    # ... line completes.
    assert getattr(entry, "runtime_data", None) is None

    with patch(
        "custom_components.be_electricity_prices.coordinator.get_extractor",
        return_value=extractor,
    ):
        # async_refresh runs the same _async_update_data path as
        # async_config_entry_first_refresh; either would have crashed
        # under v0.5.14's bare runtime_data read at line 887. Use
        # async_refresh because the first-refresh helper requires
        # config_entry to be wired into DataUpdateCoordinator from
        # 2024.10+ -- and the bug manifests on the inner tick path
        # regardless of which entry-point invokes it.
        await coord.async_refresh()

    assert coord.last_update_success
    assert coord._snapshot is snap
