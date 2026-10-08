"""The archive's VAT table: the consensus written from the cards
(``scripts/archive_cards.py``) and the integration reading it
(``vat_rates.py``)."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import vat_rates
from custom_components.be_electricity_prices.const import DOMAIN, VAT_RATE_REDUCED
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers._rates import DynamicRates
from custom_components.be_electricity_prices.providers.base import ExtractorError
from tests import make_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import archive_cards as ac  # type: ignore[import-not-found]

NOV = date(2026, 11, 1)


# The consensus.


def test_three_suppliers_agreeing_make_the_months_rate() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}, "luminus": {0.06}}
    assert ac._vat_consensus(stated) == {
        "rate": 0.06,
        "suppliers": ["eneco", "engie", "luminus"],
    }


def test_a_disagreement_is_no_rate() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}, "luminus": {0.06}, "mega": {0.07}}
    assert ac._vat_consensus(stated) == {
        "disputed": {"0.06": ["eneco", "engie", "luminus"], "0.07": ["mega"]}
    }
    # One supplier stating two rates in a month disagrees with itself.
    two = {"eneco": {0.06, 0.07}, "engie": {0.06}, "luminus": {0.06}}
    assert "disputed" in ac._vat_consensus(two)


def test_two_suppliers_are_too_few() -> None:
    stated = {"eneco": {0.06}, "engie": {0.06}}
    assert ac._vat_consensus(stated) == {"too_few": {"0.06": ["eneco", "engie"]}}


def _row(
    out: Path, supplier: str, contract: str, month: str, rate: float | None
) -> None:
    folder = out / "cards" / supplier / contract / "flanders"
    folder.mkdir(parents=True, exist_ok=True)
    taxes: dict[str, Any] = {"federal_excise": 0.04876, "energy_contribution": 0.0}
    if rate is not None:
        taxes["card_vat_rate"] = rate
    (folder / f"{month}.json").write_text(json.dumps({"taxes": taxes}))


def test_the_table_is_written_from_the_rows_by_card_month(tmp_path: Path) -> None:
    for supplier in ("eneco", "luminus", "ebem"):
        _row(tmp_path, supplier, "x", "2026-09", 0.06)
    _row(tmp_path, "bolt", "bolt_variable", "2026-09", None)
    for supplier in ("eneco", "luminus"):
        _row(tmp_path, supplier, "x", "2026-10", 0.06)
    # A professional card counts toward the standard rate, not the reduced one.
    _row(tmp_path, "engie", "engie_pro_dynamic", "2026-09", 0.21)
    ac._write_vat(tmp_path)
    table = json.loads((tmp_path / "vat.json").read_text())
    assert table["residential"]["2026-09"] == {
        "rate": 0.06,
        "suppliers": ["ebem", "eneco", "luminus"],
    }
    assert table["residential"]["2026-10"] == {
        "too_few": {"0.06": ["eneco", "luminus"]}
    }
    assert table["standard"]["2026-09"] == {"too_few": {"0.21": ["engie"]}}
    before = (tmp_path / "vat.json").read_bytes()
    ac._write_vat(tmp_path)
    assert (tmp_path / "vat.json").read_bytes() == before


# The reader.


def test_only_agreed_months_within_bounds_are_held() -> None:
    vat_rates.hold(
        {
            "residential": {
                "2026-10": {"rate": 0.06},
                "2026-11": {"disputed": {"0.06": ["a"], "0.07": ["b"]}},
                "2026-12": {"rate": 6},
                "2027-13": {"rate": 0.07},
                "not a month": {"rate": 0.07},
            },
            "standard": {"2026-10": {"rate": 0.21}},
        }
    )
    assert vat_rates.held_table() == {
        "residential": {"2026-10": {"rate": 0.06}},
        "standard": {"2026-10": {"rate": 0.21}},
    }
    # A disputed month keeps the last month that agreed.
    assert vat_rates.residential_vat(NOV) == 0.06


def test_an_empty_section_does_not_wipe_a_held_one() -> None:
    vat_rates.hold({"residential": {"2026-10": {"rate": 0.06}}})
    vat_rates.hold({"residential": {"2026-11": {"disputed": {}}}})
    assert vat_rates.residential_vat(NOV) == 0.06


def test_a_stored_table_never_replaces_a_fetched_one() -> None:
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.07}}})
    vat_rates.restore({"residential": {"2026-11": {"rate": 0.06}}})
    assert vat_rates.residential_vat(NOV) == 0.07
    vat_rates._RESIDENTIAL.clear()
    vat_rates.restore({"residential": {"2026-11": {"rate": 0.06}}})
    assert vat_rates.residential_vat(NOV) == 0.06


async def test_the_table_is_read_once_a_day_and_a_failure_waits(
    freezer: Any,
) -> None:
    freezer.move_to("2026-11-02 08:00:00+01:00")
    vat_rates._fetched_at = None
    vat_rates._failed_at = None
    body = json.dumps({"residential": {"2026-11": {"rate": 0.07, "suppliers": []}}})
    fetch = AsyncMock(return_value=body)
    with patch.object(vat_rates, "fetch_text", fetch):
        await vat_rates.ensure_vat_rates(None)  # type: ignore[arg-type]
        await vat_rates.ensure_vat_rates(None)  # type: ignore[arg-type]
        assert fetch.await_count == 1
        assert vat_rates.residential_vat(NOV) == 0.07
        freezer.tick(timedelta(hours=25))
        fetch.side_effect = ExtractorError("HTTP 503")
        await vat_rates.ensure_vat_rates(None)  # type: ignore[arg-type]
        assert fetch.await_count == 2
        # Kept through the failure, and not asked again for six hours.
        assert vat_rates.residential_vat(NOV) == 0.07
        freezer.tick(timedelta(hours=5))
        await vat_rates.ensure_vat_rates(None)  # type: ignore[arg-type]
        assert fetch.await_count == 2
        freezer.tick(timedelta(hours=2))
        await vat_rates.ensure_vat_rates(None)  # type: ignore[arg-type]
        assert fetch.await_count == 3
    vat_rates._fetched_at = None
    vat_rates._failed_at = None


# The coordinator.


async def test_a_new_table_resolves_the_live_card_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A card grossed on an assumed rate moves when the month's rate does,
    without waiting for the card itself to change."""
    freezer.move_to("2026-11-02 08:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "bolt",
            "contract": "bolt_variable",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    card = make_snapshot(energy=DynamicRates(factor=1.06, base=0.0106))
    card = card.__class__(
        **{
            **card.__dict__,
            "taxes": card.taxes.__class__(
                **{**card.taxes.__dict__, "assumed_vat_rate": VAT_RATE_REDUCED}
            ),
        }
    )
    coord._set_snapshot(card)
    assert coord._snapshot is not None
    assert isinstance(coord._snapshot.energy, DynamicRates)
    assert coord._snapshot.energy.factor == pytest.approx(1.06)
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.07}}})
    coord._reresolve_snapshot()
    assert isinstance(coord._snapshot.energy, DynamicRates)
    assert coord._snapshot.energy.factor == pytest.approx(1.07)


async def test_the_table_travels_through_the_store(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"supplier": "eneco", "contract": "power_fix", "region": "wallonia"},
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.07}}})
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert saved[-1]["vat_rates"]["residential"] == {"2026-11": {"rate": 0.07}}
    vat_rates._RESIDENTIAL.clear()
    coord._store.async_load = AsyncMock(return_value=saved[-1])  # type: ignore[method-assign]
    await coord.async_load_persistent()
    assert vat_rates.residential_vat(NOV) == 0.07


_ = dt_util
