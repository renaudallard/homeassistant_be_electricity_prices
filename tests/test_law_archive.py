"""The card archive's law table: written from what the integration bills each
month on from the excise law (``scripts/archive_cards.py``), read back by an
installation (``law_archive.py``), and billed only for a month the law it
holds does not give (``excise_law.py``)."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    archive_table,
    coordinator_tick,
    excise_law,
    law_archive,
)
from custom_components.be_electricity_prices.const import CONF_CARD_ARCHIVE, DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers.base import ExtractorError
from tests import make_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import archive_cards as ac  # type: ignore[import-not-found]

SEP = date(2026, 9, 1)
KEPT = {"standard": 0.0461, "protected": 0.0011}


# The reader.


def test_only_rates_that_read_are_held() -> None:
    law_archive.hold(
        {
            "residential": {
                "2026-09": KEPT,
                "2026-10": {"standard": -0.1, "protected": 0.0},
                "2026-11": {"standard": 0.5, "protected": False},
                "2026-13": KEPT,
                "next": KEPT,
            }
        }
    )
    assert law_archive.excise_for("standard", SEP) == 0.0461
    assert law_archive.excise_for("protected", SEP) == 0.0011
    # Each rate on its own: October keeps its protected rate alone.
    assert law_archive.held_table() == {
        "residential": {"2026-09": KEPT, "2026-10": {"protected": 0.0}}
    }
    # A month the table does not hold is no month before or after it.
    assert law_archive.excise_for("standard", date(2026, 8, 1)) is None


def test_an_empty_table_does_not_wipe_a_held_one() -> None:
    law_archive.hold({"residential": {"2026-09": KEPT}})
    law_archive.hold({"residential": {}})
    law_archive.hold({"other": {}})
    assert law_archive.excise_for("standard", SEP) == 0.0461


def test_the_store_round_trips_and_never_replaces_a_read() -> None:
    law_archive.restore({"residential": {"2026-09": KEPT}})
    stored = law_archive.held_table()
    law_archive.restore({"residential": {"2026-09": {"standard": 0.03}}})
    assert law_archive.held_table() == stored
    law_archive._HELD.clear()
    law_archive.restore(stored)
    assert law_archive.held_table() == stored
    law_archive._HELD.clear()
    law_archive.restore(None)
    law_archive.restore({"residential": "no months"})
    assert law_archive.held_table() == {"residential": {}}


async def test_the_table_is_read_once_a_day_and_a_failure_waits(freezer: Any) -> None:
    freezer.move_to("2026-11-02 08:00:00+01:00")
    body = json.dumps({"residential": {"2026-09": KEPT}})
    fetch = AsyncMock(return_value=body)
    with patch.object(archive_table, "fetch_text", fetch):
        await law_archive.ensure_law_archive(None)  # type: ignore[arg-type]
        await law_archive.ensure_law_archive(None)  # type: ignore[arg-type]
        assert fetch.await_count == 1
        assert fetch.await_args is not None
        assert fetch.await_args.args[1].endswith(
            "/be_price_cards/main/electricity/law.json"
        )
        assert law_archive.excise_for("standard", SEP) == 0.0461
        freezer.tick(timedelta(hours=25))
        fetch.side_effect = ExtractorError("HTTP 503")
        await law_archive.ensure_law_archive(None)  # type: ignore[arg-type]
        assert fetch.await_count == 2
        # Kept through the failure, and not asked again for six hours.
        assert law_archive.excise_for("standard", SEP) == 0.0461
        freezer.tick(timedelta(hours=5))
        await law_archive.ensure_law_archive(None)  # type: ignore[arg-type]
        assert fetch.await_count == 2
        freezer.tick(timedelta(hours=2))
        await law_archive.ensure_law_archive(None)  # type: ignore[arg-type]
        assert fetch.await_count == 3


# The coordinator.


def _entry(hass: HomeAssistant, **data: Any) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            **data,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.parametrize("use_archive", [True, False])
async def test_only_an_entry_reading_the_archive_reads_its_tables(
    hass: HomeAssistant, use_archive: bool
) -> None:
    """The VAT and law tables are the card archive's: an entry that keeps
    the archive out asks GitHub for neither."""
    coord = BePricesCoordinator(hass, _entry(hass, **{CONF_CARD_ARCHIVE: use_archive}))
    coord._snapshot = make_snapshot()
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    await coord._async_update_data()
    for table in (
        coordinator_tick.ensure_vat_rates,
        coordinator_tick.ensure_law_archive,
    ):
        reads: Any = table
        assert (reads.await_count > 0) is use_archive


async def test_the_table_travels_through_the_store(hass: HomeAssistant) -> None:
    coord = BePricesCoordinator(hass, _entry(hass))
    law_archive.hold({"residential": {"2026-09": KEPT}})
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert saved[-1]["law_archive"] == {"residential": {"2026-09": KEPT}}
    law_archive._HELD.clear()
    coord._store.async_load = AsyncMock(return_value=saved[-1])  # type: ignore[method-assign]
    await coord.async_load_persistent()
    assert law_archive.excise_for("standard", SEP) == 0.0461


# Billed on it.


def test_the_law_is_billed_before_the_archive() -> None:
    """The law as read gives September 2026: what the archive kept of it is
    not billed."""
    law_archive.hold({"residential": {"2026-09": KEPT}})
    assert excise_law.standard_excise(SEP) == 0.046
    assert excise_law.protected_excise(SEP) == 0.001


def test_a_month_the_law_no_longer_gives_is_billed_on_the_archive() -> None:
    """Justel's text, once it starts on 1 November 2026, no longer gives
    September: the archive answers before the rates typed beside the law, and
    for the protected customer before its closed rates."""
    excise_law._HELD.clear()
    excise_law.hold_read(
        {
            excise_law.STANDARD: ((date(2026, 11, 1), 0.0422),),
            excise_law.PROTECTED: ((date(2026, 11, 1), 0.001),),
        }
    )
    july_2023 = date(2023, 7, 1)
    law_archive.hold(
        {"residential": {"2026-09": KEPT, "2023-07": {"protected": 0.0236}}}
    )
    assert excise_law.standard_excise(SEP) == 0.0461
    assert excise_law.protected_excise(SEP) == 0.0011
    assert excise_law.protected_excise(july_2023) == 0.0236
    # Without it, the typed rates answer as before.
    law_archive._HELD.clear()
    assert excise_law.standard_excise(SEP) == 0.046
    assert excise_law.protected_excise(SEP) == 0.001
    assert excise_law.protected_excise(july_2023) == 0.02362


# Written from the law.


def test_the_table_is_what_the_integration_bills_each_month_on(tmp_path: Path) -> None:
    held = ac._write_law(tmp_path, date(2026, 11, 2))
    table = json.loads((tmp_path / "law.json").read_text())["residential"]
    assert held == len(table) == 50
    assert (min(table), max(table)) == ("2022-10", "2026-11")
    assert table["2022-10"] == {"protected": 0.0}
    assert table["2023-07"] == {"protected": 0.02362}
    assert table["2026-08"] == {"standard": 0.046, "protected": 0.001}
    assert table["2026-11"] == {"standard": 0.0422, "protected": 0.001}
    before = (tmp_path / "law.json").read_bytes()
    ac._write_law(tmp_path, date(2026, 11, 2))
    assert (tmp_path / "law.json").read_bytes() == before


def test_a_month_the_law_no_longer_gives_keeps_what_was_written(tmp_path: Path) -> None:
    july = {"standard": 0.05, "protected": 0.0236}
    (tmp_path / "law.json").write_text(
        json.dumps(
            {"residential": {"2022-09": KEPT, "2026-09": KEPT, "2023-07": july}}
        ),
        encoding="utf-8",
    )
    excise_law._HELD.clear()
    ac._write_law(tmp_path, date(2026, 11, 2))
    table = json.loads((tmp_path / "law.json").read_text())["residential"]
    # Before the first month, and what the law no longer gives, stay.
    assert table["2022-09"] == KEPT
    assert table["2026-09"] == KEPT
    # What the integration still bills, the closed protected rates here, is
    # written again, rate by rate.
    assert table["2023-07"] == {"standard": 0.05, "protected": 0.02362}
