"""Entry-level setup: the current-year-cost unique_id migration and the
time listeners ``async_setup_entry`` registers."""

from __future__ import annotations

import zlib
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import (
    _migrate_bolt_dynamic_contract,
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


async def test_the_slot_push_follows_the_resolution_the_table_ends_up_with(
    hass: HomeAssistant,
) -> None:
    """The cadence was fixed once at setup from coordinator.data, which is
    None after a first refresh tolerated for an unreadable card. A
    quarter-hourly Ecofix entry set up that way was pushed hourly for as long
    as it lived, so once the archive's reading was adopted current_price sat
    on a stale quarter for up to 45 minutes. The push has to follow the
    resolution the table ends up with."""
    from custom_components.be_electricity_prices.const import RESOLUTION_QUARTER
    from custom_components.be_electricity_prices.coordinator_data import CoordinatorData

    registered: list[dict[str, Any]] = []
    cancelled: list[dict[str, Any]] = []

    def _capture(
        _hass: HomeAssistant,
        action: Any,
        hour: Any = None,
        minute: Any = None,
        second: Any = None,
    ) -> Any:
        spec = {"hour": hour, "minute": minute, "second": second}
        registered.append(spec)
        return lambda: cancelled.append(spec)

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
        # No table yet, so the push is hourly, as before.
        slot = [r for r in registered if r["hour"] is None]
        assert [r["minute"] for r in slot] == [0]

        # The first tick that prices the entry does so on the 15-minute grid.
        entry.runtime_data.async_set_updated_data(
            CoordinatorData(hourly={}, resolution=RESOLUTION_QUARTER)
        )
        slot = [r for r in registered if r["hour"] is None]
        assert [r["minute"] for r in slot] == [0, [0, 15, 30, 45]]
        assert cancelled == [slot[0]]

        # A tick that keeps the grid registers nothing new.
        entry.runtime_data.async_set_updated_data(
            CoordinatorData(hourly={}, resolution=RESOLUTION_QUARTER)
        )
        assert len([r for r in registered if r["hour"] is None]) == 2


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


# ---- the retired Bolt dynamic contracts ------------------------------------------


def _bolt_entry(hass: HomeAssistant, contract: str) -> Any:
    entry = make_entry(
        supplier="bolt",
        contract=contract,
        region="flanders",
        dso="fluvius_antwerpen",
        meter="dynamic",
        title=f"Bolt - {contract} (Flanders)",
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, unique_id=f"bolt:{contract}:flanders:fluvius_antwerpen"
    )
    return entry


def test_retired_bolt_dynamic_entry_moves_to_its_variable_card(
    hass: HomeAssistant,
) -> None:
    """The quarter-hourly settlement stopped being a product of its own, so an
    entry stored under the old id points at a contract the registry no longer
    knows and has no card to fetch. It moves to the variable card with the
    settlement box ticked, which bills exactly what it billed before."""
    entry = _bolt_entry(hass, "bolt_plenty_online_dynamic")

    _migrate_bolt_dynamic_contract(hass, entry)

    assert entry.data["contract"] == "bolt_plenty_online"
    assert entry.data["quarter_hourly"] is True
    # The unique id and the title both name the contract, so both move.
    assert entry.unique_id == "bolt:bolt_plenty_online:flanders:fluvius_antwerpen"
    assert entry.title == "Bolt - Bolt Plenty Online (Flanders)"


def test_migration_leaves_every_other_entry_alone(hass: HomeAssistant) -> None:
    """Only the eight retired ids move, and only on Bolt."""
    kept = _bolt_entry(hass, "bolt_variable")
    _migrate_bolt_dynamic_contract(hass, kept)
    assert kept.data["contract"] == "bolt_variable"
    assert "quarter_hourly" not in kept.data

    other = make_entry(supplier="frank", contract="bolt_dynamic")
    other.add_to_hass(hass)
    _migrate_bolt_dynamic_contract(hass, other)
    assert other.data["contract"] == "bolt_dynamic"


def test_migration_keeps_its_unique_id_when_the_target_is_taken(
    hass: HomeAssistant,
) -> None:
    """A household that deliberately ran both readings as two entries would
    otherwise have the second claim the first's key. The data migration still
    happens for both; only the id stays put, which costs nothing but a
    duplicate check the user has already passed."""
    existing = _bolt_entry(hass, "bolt_variable")
    entry = _bolt_entry(hass, "bolt_dynamic")

    _migrate_bolt_dynamic_contract(hass, entry)

    assert entry.data["contract"] == "bolt_variable"
    assert entry.data["quarter_hourly"] is True
    assert entry.unique_id == "bolt:bolt_dynamic:flanders:fluvius_antwerpen"
    assert existing.unique_id == "bolt:bolt_variable:flanders:fluvius_antwerpen"


async def test_a_setup_that_fails_before_the_first_refresh_leaves_no_coordinator(
    hass: HomeAssistant,
) -> None:
    """The attribute was taken back on ConfigEntryNotReady only. A store that
    cannot be read fails the setup before the first refresh, and Home
    Assistant deletes runtime_data only when unloading an entry that loaded,
    so a coordinator that never ran stayed on an entry in SETUP_ERROR."""
    entry = make_entry()
    entry.add_to_hass(hass)
    with patch.object(
        BePricesCoordinator,
        "async_load_persistent",
        AsyncMock(side_effect=OSError("store unreadable")),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hasattr(entry, "runtime_data")


# ---- entities an options change stops creating --------------------------------


async def _setup_with_ev_box(hass: HomeAssistant, entry: Any) -> Any:
    from custom_components.be_electricity_prices.providers.base import DsoOverlay
    from tests import make_snapshot, make_stub_extractor

    overlay = DsoOverlay(distribution_single=0.10, transport=0.0145)

    async def _fetch(*_a: Any, **_k: Any) -> Any:
        # Both regions' DSOs, so an entry may move between them.
        return make_snapshot(dsos={"ores": overlay, "fluvius_antwerpen": overlay})

    return (
        patch(
            "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
            return_value=make_stub_extractor(fetch=_fetch),
        ),
        patch(
            "custom_components.be_electricity_prices.backfill_if_missing",
            AsyncMock(return_value=None),
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_tick.ensure_ev_rates",
            AsyncMock(return_value=True),
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_tick.ev_rate_for",
            lambda *_a: 0.25,
        ),
    )


def _ev_entity(hass: HomeAssistant, entry: Any) -> er.RegistryEntry | None:
    registry = er.async_get(hass)
    return next(
        (
            e
            for e in er.async_entries_for_config_entry(registry, entry.entry_id)
            if e.unique_id == f"{entry.entry_id}_ev_home_charging_rate"
        ),
        None,
    )


async def test_an_entity_the_options_stop_creating_leaves_the_registry(
    hass: HomeAssistant,
) -> None:
    """Unticking the EV box stops creating its sensor, and the reload used to
    leave the registry row behind, restored as unavailable ("no longer
    provided") until the user deleted it by hand. The same happened to the
    band, capacity, solar, contract-end and saving sensors and to the peak
    reset button whenever an edit, or a recorded switch, moved the meter,
    region or regime."""
    from contextlib import ExitStack

    from custom_components.be_electricity_prices.const import (
        CONF_EV_HOME_CHARGING_RATE,
    )

    entry = make_entry(ev_home_charging_rate=True)
    entry.add_to_hass(hass)
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entry):
            stack.enter_context(p)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert _ev_entity(hass, entry) is not None

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_EV_HOME_CHARGING_RATE: False}
        )
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert _ev_entity(hass, entry) is None
        # Everything still provided keeps its row.
        registry = er.async_get(hass)
        assert registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_price"
        )


async def test_a_disabled_entity_keeps_its_row(hass: HomeAssistant) -> None:
    """A row the user disabled is their choice, even for an entity the
    settings no longer create: removing it would bring the entity back
    enabled if the setting is turned on again."""
    from contextlib import ExitStack

    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    disabled = registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_ev_home_charging_rate",
        config_entry=entry,
        disabled_by=er.RegistryEntryDisabler.USER,
    )
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entry):
            stack.enter_context(p)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(disabled.entity_id) is not None


async def test_a_platform_that_fails_to_set_up_keeps_its_rows(
    hass: HomeAssistant,
) -> None:
    """Home Assistant catches whatever a platform's setup raises and carries
    on, so a failed platform adds nothing. That is not the settings dropping
    its entities: removing its rows would lose every rename, area and icon
    the user gave them."""
    from contextlib import ExitStack

    from custom_components.be_electricity_prices import sensor

    entry = make_entry()
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entry):
            stack.enter_context(p)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        price = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}_current_price"
        )
        assert price is not None
        registry.async_update_entity(price, name="My price")

        async def _broken(*_a: Any, **_k: Any) -> None:
            raise RuntimeError("platform setup failed")

        stack.enter_context(patch.object(sensor, "async_setup_entry", _broken))
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    kept = registry.async_get(price)
    assert kept is not None
    assert kept.name == "My price"


async def test_leaving_flanders_removes_the_peak_reset_button(
    hass: HomeAssistant,
) -> None:
    """The button platform adds nothing outside Flanders, and records that it
    meant to, so the button of an entry that moved region goes with it."""
    from contextlib import ExitStack

    entry = make_entry(
        region="flanders", dso="fluvius_antwerpen", title="Eneco (Flanders)"
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    with ExitStack() as stack:
        for p in await _setup_with_ev_box(hass, entry):
            stack.enter_context(p)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        button = registry.async_get_entity_id(
            "button", DOMAIN, f"{entry.entry_id}_reset_monthly_peak"
        )
        assert button is not None

        hass.config_entries.async_update_entry(
            entry, data={**entry.data, "region": "wallonia", "dso": "ores"}
        )
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert registry.async_get(button) is None
