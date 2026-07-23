"""Entry-level setup: the current-year-cost unique_id migration and the
time listeners ``async_setup_entry`` registers."""

from __future__ import annotations

import zlib
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import (
    _migrate_current_year_cost_unique_id,
    async_setup_entry,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from tests import make_entry


def _register(hass: HomeAssistant, entry: object, unique_id: str) -> str:
    registry = er.async_get(hass)
    return registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        config_entry=entry,  # type: ignore[arg-type]
        suggested_object_id="eneco_yearly_cost",
    ).entity_id


def test_migrate_yearly_cost_renames_unique_id(hass: HomeAssistant) -> None:
    """The pre-0.5.2 ``yearly_cost`` entity is adopted under the new key,
    keeping its entity_id (and therefore its history and dashboard refs)."""
    entry = make_entry()
    entry.add_to_hass(hass)
    entity_id = _register(hass, entry, f"{entry.entry_id}_yearly_cost")

    _migrate_current_year_cost_unique_id(hass, entry)

    registry = er.async_get(hass)
    # Same entity_id, new unique_id.
    assert registry.async_get(entity_id) is not None
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_year_cost"
        )
        == entity_id
    )
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_yearly_cost")
        is None
    )


def test_migrate_yearly_cost_noop_when_absent(hass: HomeAssistant) -> None:
    """A fresh install (no legacy entity) migrates nothing."""
    entry = make_entry()
    entry.add_to_hass(hass)

    _migrate_current_year_cost_unique_id(hass, entry)

    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_year_cost"
        )
        is None
    )


def test_migrate_yearly_cost_skips_on_collision(hass: HomeAssistant) -> None:
    """When the new id already exists (the entry ran past the rename), the
    legacy orphan is left untouched rather than colliding with the live one."""
    entry = make_entry()
    entry.add_to_hass(hass)
    old_id = _register(hass, entry, f"{entry.entry_id}_yearly_cost")
    registry = er.async_get(hass)
    new_id = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_current_year_cost",
        config_entry=entry,  # type: ignore[arg-type]
    ).entity_id

    _migrate_current_year_cost_unique_id(hass, entry)

    # Both survive unchanged; no rename attempted onto the live id.
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_yearly_cost")
        == old_id
    )
    assert (
        registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_year_cost"
        )
        == new_id
    )


_MODULE = "custom_components.be_electricity_prices"


async def _setup_capturing_time_listeners(
    hass: HomeAssistant,
) -> tuple[list[tuple[dict[str, Any], Any]], AsyncMock]:
    """Run ``async_setup_entry`` with the network and platform work stubbed,
    capturing every ``async_track_time_change`` registration.

    ``async_request_refresh`` is stubbed on the coordinator INSTANCE rather
    than the class, so it stays stubbed after the patch context closes: the
    listeners are invoked by the tests, and letting the real refresh run there
    would reach for the network.
    """
    registered: list[tuple[dict[str, Any], Any]] = []

    def _capture(
        _hass: HomeAssistant,
        action: Any,
        hour: Any = None,
        minute: Any = None,
        second: Any = None,
    ) -> Any:
        registered.append(({"hour": hour, "minute": minute, "second": second}, action))
        return lambda: None

    async def _no_backfill(*_a: object, **_kw: object) -> None:
        return None

    entry = make_entry()
    entry.add_to_hass(hass)
    with (
        patch.object(BePricesCoordinator, "async_load_persistent", AsyncMock()),
        patch.object(
            BePricesCoordinator, "async_config_entry_first_refresh", AsyncMock()
        ),
        patch.object(hass.config_entries, "async_forward_entry_setups", AsyncMock()),
        patch(f"{_MODULE}.async_track_time_change", _capture),
        patch(f"{_MODULE}.backfill_if_missing", _no_backfill),
    ):
        assert await async_setup_entry(hass, entry) is True
    refresh = AsyncMock()
    entry.runtime_data.async_request_refresh = refresh  # type: ignore[method-assign]
    return registered, refresh


async def test_setup_registers_a_local_midnight_rebuild(hass: HomeAssistant) -> None:
    """Crossing local midnight leaves the price table anchored on the previous
    day, so the tomorrow_* sensors read unknown and tomorrow_prices_available
    drops off until the next (non-clock-aligned) tick. The day boundary must
    therefore request a rebuild, which re-reading the same data cannot do."""
    registered, refresh = await _setup_capturing_time_listeners(hass)

    midnight = [(spec, fn) for spec, fn in registered if spec["hour"] == 0]
    assert len(midnight) == 1
    spec, action = midnight[0]
    assert spec["hour"] == 0 and spec["minute"] == 0
    # Spread over the first minute so the whole (single-timezone) user base
    # does not probe its supplier on the same second, but stable per entry.
    assert spec["second"] in range(60)

    assert refresh.await_count == 0
    await action(dt_util.now())
    assert refresh.await_count == 1


async def test_midnight_rebuild_second_is_stable_and_spread(
    hass: HomeAssistant,
) -> None:
    """The per-entry offset must survive a restart (so it is derived from the
    entry id, not a per-process hash) and must differ between entries."""
    seconds = []
    for _ in range(2):
        registered, _ = await _setup_capturing_time_listeners(hass)
        seconds.append(
            next(spec["second"] for spec, _fn in registered if spec["hour"] == 0)
        )
    # Two separate entries, so two independently derived offsets; each run is
    # deterministic for its own entry id.
    assert all(s in range(60) for s in seconds)
    assert all(
        zlib.crc32(e.entry_id.encode()) % 60 == s
        for e, s in zip(hass.config_entries.async_entries(DOMAIN), seconds, strict=True)
    )


async def test_setup_keeps_the_hourly_slot_boundary_push(hass: HomeAssistant) -> None:
    """The slot-boundary push stays a plain listener notification on every
    other hour: the price sensors read the wall clock themselves, so they need
    no refetch, and the midnight rebuild must not replace that."""
    registered, refresh = await _setup_capturing_time_listeners(hass)

    boundary = [(spec, fn) for spec, fn in registered if spec["hour"] is None]
    assert len(boundary) == 1
    spec, action = boundary[0]
    # Eneco power_fix is an hourly contract, so the push fires only at :00.
    assert spec == {"hour": None, "minute": 0, "second": 0}

    action(dt_util.now().replace(hour=13, minute=0))
    assert refresh.await_count == 0
