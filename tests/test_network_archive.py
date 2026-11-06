"""The network sheets the card archive kept (network_archive.py)."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    const,
    coordinator_tick,
    network_archive,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator

_ORES = {
    "distribution_single": 0.113026,
    "distribution_peak": 0.125215,
    "transport": 0.0258728,
    "data_management_per_year": 13.3,
    "prosumer_eur_per_kva_year": 80.981334,
}


def test_a_month_is_read_by_dso_and_kept_until_a_table_replaces_it() -> None:
    """Each DSO's fields of a month, as the archive wrote them; a table with
    no month does not wipe what is held, a later one replaces it, and a
    stored copy never replaces a table held."""
    network_archive.hold({"months": {"2026-11": {"ores": _ORES}}})
    assert network_archive.fields_for(date(2026, 11, 1), "ores") == _ORES
    assert network_archive.fields_for(date(2026, 10, 1), "ores") == {}
    assert network_archive.fields_for(date(2026, 11, 1), "resa") == {}
    network_archive.hold({"months": {}})
    assert network_archive.fields_for(date(2026, 11, 1), "ores") == _ORES
    network_archive.restore({"months": {"2026-12": {"ores": _ORES}}})
    assert network_archive.fields_for(date(2026, 12, 1), "ores") == {}
    assert network_archive.held_table() == {"months": {"2026-11": {"ores": _ORES}}}
    network_archive.hold({"months": {"2026-12": {"resa": _ORES}}})
    assert network_archive.fields_for(date(2026, 11, 1), "ores") == {}
    assert network_archive.fields_for(date(2026, 12, 1), "resa") == _ORES


@pytest.mark.parametrize(
    "spoilt",
    [
        {"distribution_single": 13.3},
        {"data_management_per_year": 1300.0},
        {"transport": -0.01},
        {"transport": None},
        {"distribution_single": "0.11"},
        {"distribution_single": True},
        {"brussels_osp_by_tier": 1.0},
    ],
)
def test_a_dso_month_that_does_not_read_whole_is_left_out(
    spoilt: dict[str, object],
) -> None:
    """A figure out of bounds, of another type, unset where an overlay needs
    one, or a field no sheet sets drops the DSO's month whole, since half of
    it would bill a network short of a charge; the other DSOs stay."""
    network_archive.hold(
        {"months": {"2026-11": {"ores": {**_ORES, **spoilt}, "resa": _ORES}}}
    )
    assert network_archive.fields_for(date(2026, 11, 1), "ores") == {}
    assert network_archive.fields_for(date(2026, 11, 1), "resa") == _ORES


def test_a_field_a_sheet_leaves_unset_reads_as_such() -> None:
    """Fluvius's sheet leaves the day/night split unset, so the typed one
    goes with the typed figures."""
    flemish = {**_ORES, "distribution_peak": None, "distribution_offpeak": None}
    network_archive.hold({"months": {"2026-11": {"fluvius_west": flemish}}})
    assert network_archive.fields_for(date(2026, 11, 1), "fluvius_west") == flemish


# The coordinator.


def _entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    """A custom entry on ORES whose network was typed by hand."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
            const.CONF_REGION: const.REGION_WALLONIA,
            const.CONF_DSO: const.DSO_ORES,
            const.CONF_METER: const.METER_MONO,
            const.CONF_CUSTOM_ENERGY_SINGLE: 0.1,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.04,
            const.CONF_CUSTOM_VAT_RATE: 0.06,
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize(
    ("sheet", "use_archive", "reads"),
    [(True, True, True), (True, False, False), (False, True, False)],
)
async def test_only_an_entry_taking_a_sheet_reads_the_network_table(
    hass: HomeAssistant, sheet: bool, use_archive: bool, reads: bool
) -> None:
    """The table is the card archive's, and of use only to an entry taking
    its network from a sheet: an entry keeping the archive out, or typing its
    network, asks GitHub for none of it."""
    entry = _entry(
        hass,
        **{
            const.CONF_CUSTOM_DSO_FROM_CWAPE: sheet,
            const.CONF_CARD_ARCHIVE: use_archive,
        },
    )
    coord = BePricesCoordinator(hass, entry)
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    await coord._async_update_data()
    fetch: Any = coordinator_tick.ensure_network_archive
    assert (fetch.await_count > 0) is reads


@pytest.mark.parametrize("sheet", [True, False])
async def test_the_table_travels_through_a_sheet_entry_s_store(
    hass: HomeAssistant, sheet: bool
) -> None:
    """Kept by an entry taking its network from a sheet, so a restart without
    the network still knows it, and by no other."""
    coord = BePricesCoordinator(
        hass, _entry(hass, **{const.CONF_CUSTOM_DSO_FROM_CWAPE: sheet})
    )
    network_archive.hold({"months": {"2026-11": {"ores": _ORES}}})
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert ("network_archive" in saved[-1]) is sheet
    if not sheet:
        return
    network_archive._HELD.clear()
    coord._store.async_load = AsyncMock(return_value=saved[-1])  # type: ignore[method-assign]
    await coord.async_load_persistent()
    assert network_archive.fields_for(date(2026, 11, 1), "ores") == _ORES
