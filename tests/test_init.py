"""Entry-level setup helpers: the current-year-cost unique_id migration."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.be_electricity_prices import (
    _migrate_current_year_cost_unique_id,
)
from custom_components.be_electricity_prices.const import DOMAIN
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
