"""A withdrawn product's network once the months run past its last card's year.

Its last card prints that year's network tariffs and no later card comes, so a
month of a later year takes its network off the regulator's sheet of its own
year, the year before standing in until that one is out
(``snapshot_resolve.past_last_card``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, call, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    brugel,
    cwape,
    fluvius,
    network_archive,
    snapshot_resolve,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers import mega, trevion
from custom_components.be_electricity_prices.providers._pdf_render import (
    extract_pdf_text,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _proxy_for_month,
    _resolve_snapshot,
)

from . import fixture_text

_FIXTURES = Path(__file__).parent / "fixtures"
_ZENNE_DIJLE = "fluvius_zenne_dijle"


def _entry(supplier: str, contract: str, region: str, dso: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": supplier,
            "contract": contract,
            "region": region,
            "dso": dso,
            "meter": "mono",
        },
    )


def _network(overlay: DsoOverlay) -> tuple[float | None, ...]:
    """The figures a network sheet sets, leaving out the VREG ceiling, which
    the resolver lays on every Flemish card by the month's year."""
    return (
        overlay.distribution_single,
        overlay.distribution_peak,
        overlay.distribution_offpeak,
        overlay.distribution_exclusive_night,
        overlay.transport,
        overlay.capacity_eur_per_kw_year,
        overlay.data_management_per_year,
        overlay.prosumer_eur_per_kva_year,
    )


def _lifepowr() -> SupplierSnapshot:
    """Trevion's LifePowr, withdrawn from October 2026, on its last card."""
    return trevion.parse_snapshot(
        "lifepowr", fixture_text("trevion_lifepowr_2026-09.pdf", layout=True)
    )


def _hold_zenne_dijle_2026() -> fluvius.AreaTerms:
    terms = fluvius.parse_area_terms(
        (_FIXTURES / "fluvius_zenne_dijle_2026.xlsx").read_bytes(), 2026
    )
    assert terms is not None
    fluvius._area[(2026, _ZENNE_DIJLE)] = terms
    return terms


@pytest.fixture
def _cwape_held() -> Iterator[None]:
    yield
    cwape._grids.clear()
    cwape._transport.clear()


@pytest.fixture
def _brugel_2026() -> Iterator[brugel.NetworkTerms]:
    sheet = (_FIXTURES / "brugel_elec_2026.pdf").read_bytes()
    terms = brugel._parse_network(extract_pdf_text(sheet))
    assert terms is not None
    brugel._network[2026] = terms
    yield terms
    brugel._network.clear()


def test_a_withdrawn_card_bills_a_later_year_on_the_sheet_before_it() -> None:
    """LifePowr's September 2026 card prints Zenne-Dijle's 2026 rates rounded
    to 5,87 c/kWh. A 2027 month has no card of its own and Fluvius has no
    2027 sheet yet, so the 2026 sheet stands in, to its last decimal; a month
    of 2026 keeps the card's table."""
    terms = _hold_zenne_dijle_2026()
    snap = _lifepowr()
    entry = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE)

    own = _resolve_snapshot(entry, snap, delivery_month=date(2026, 10, 1))
    assert _network(own.dsos[_ZENNE_DIJLE]) == _network(snap.dsos[_ZENNE_DIJLE])
    assert own.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(0.0587)

    later = _resolve_snapshot(entry, snap, delivery_month=date(2027, 2, 1))
    overlay = later.dsos[_ZENNE_DIJLE]
    assert overlay.distribution_single == pytest.approx(terms.kwh_normal * 1.06)
    assert overlay.distribution_single == pytest.approx(0.0553921 * 1.06)
    assert overlay.distribution_exclusive_night == pytest.approx(
        terms.kwh_exclusive_night * 1.06
    )
    assert overlay.capacity_eur_per_kw_year == pytest.approx(
        terms.capacity_per_kw_year * 1.06
    )
    assert overlay.data_management_per_year == pytest.approx(
        terms.data_management_per_year * 1.06
    )
    # The other areas keep the card's figures: only the entry's own is billed.
    assert _network(later.dsos["fluvius_antwerpen"]) == _network(
        snap.dsos["fluvius_antwerpen"]
    )


def test_a_product_still_sold_keeps_its_card_s_network() -> None:
    """The card of a product still on sale is its month's own, whatever year
    it is asked for: Trevion's Flex prints the same table and is left alone."""
    _hold_zenne_dijle_2026()
    snap = trevion.parse_snapshot(
        "groene_stroom_flex", fixture_text("trevion_flex_2026-09.pdf", layout=True)
    )
    entry = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE)
    later = _resolve_snapshot(entry, snap, delivery_month=date(2027, 2, 1))
    assert _network(later.dsos[_ZENNE_DIJLE]) == _network(snap.dsos[_ZENNE_DIJLE])


def test_a_card_printing_an_earlier_year_s_table_bills_the_sheet() -> None:
    """Belvus's cards of January to April 2026 print Fluvius's 2025 network
    table, which its reader marks. A 2026 month on such a card bills the
    area's 2026 sheet, to its last decimal; a month of the year the table is
    of keeps it, and so does a card of the same product carrying no mark."""
    terms = _hold_zenne_dijle_2026()
    card = trevion.parse_snapshot(
        "groene_stroom_flex", fixture_text("trevion_flex_2026-09.pdf", layout=True)
    )
    snap = replace(card, network_year=2025)
    entry = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE)

    billed = _resolve_snapshot(entry, snap, delivery_month=date(2026, 2, 1))
    overlay = billed.dsos[_ZENNE_DIJLE]
    assert overlay.distribution_single == pytest.approx(terms.kwh_normal * 1.06)
    assert overlay.capacity_eur_per_kw_year == pytest.approx(
        terms.capacity_per_kw_year * 1.06
    )
    assert _network(billed.dsos["fluvius_antwerpen"]) == _network(
        snap.dsos["fluvius_antwerpen"]
    )
    own = _resolve_snapshot(entry, snap, delivery_month=date(2025, 12, 1))
    assert _network(own.dsos[_ZENNE_DIJLE]) == _network(snap.dsos[_ZENNE_DIJLE])
    unmarked = _resolve_snapshot(entry, card, delivery_month=date(2026, 2, 1))
    assert _network(unmarked.dsos[_ZENNE_DIJLE]) == _network(card.dsos[_ZENNE_DIJLE])
    # Only the region the entry is in: a Walloon entry's card is not touched.
    walloon = _entry("trevion", "groene_stroom_flex", "wallonia", _ZENNE_DIJLE)
    assert not snapshot_resolve.past_last_card(
        walloon, snap, date(2026, 2, 1), "flanders"
    )


async def test_a_product_flagged_for_earlier_tables_reads_its_area_s_sheet() -> None:
    """An entry on a product some of whose cards print an earlier year's
    table reads its area's sheet, as a withdrawn product's does, so the
    months those cards bill find it held (``Contract.earlier_network_cards``);
    the hold that waits for a withdrawn product's sheet does not apply."""
    session = object()
    area = AsyncMock()
    data = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE).data
    with (
        patch.object(snapshot_resolve, "ensure_area_terms", area),
        patch.object(snapshot_resolve, "prints_earlier_network", return_value=True),
    ):
        await snapshot_resolve.ensure_custom_network(session, data, 2026)  # type: ignore[arg-type]
        area.assert_awaited_once_with(session, 2026, _ZENNE_DIJLE)
        assert snapshot_resolve.reads_network_sheet(data)
        assert not snapshot_resolve.network_sheet_unread(data, date(2026, 2, 1))


@pytest.mark.usefixtures("_cwape_held")
def test_a_withdrawn_walloon_card_bills_the_cwape_grid_of_the_year() -> None:
    """Mega's Off-peak Fixed left the market on 30 October 2026. A 2027 month
    on its August card bills ORES's 2027 grid, and with only 2026's held the
    2026 one stands in; the card's own year keeps the card's table."""
    snap = mega.parse_snapshot(
        "mega_offpeak_fixed", fixture_text("mega_offpeak_fixed_w.pdf"), "wallonia"
    )
    entry = _entry("mega", "mega_offpeak_fixed", "wallonia", "ores")
    grid_2026 = cwape.parse_grid((_FIXTURES / "cwape_ores_2026.xlsx").read_bytes())
    grid_2027 = cwape.parse_grid((_FIXTURES / "cwape_ores_2027.xlsx").read_bytes())
    transport = cwape.parse_transport(
        (_FIXTURES / "cwape_transport_2026.xlsx").read_bytes()
    )
    assert grid_2026 is not None and grid_2027 is not None and transport is not None
    cwape._grids["ores"] = {"2026": grid_2026}
    cwape._transport["2026"] = transport
    february = date(2027, 2, 1)

    own = _resolve_snapshot(entry, snap, delivery_month=date(2026, 10, 1))
    assert _network(own.dsos["ores"]) == _network(snap.dsos["ores"])

    stand_in = _resolve_snapshot(entry, snap, delivery_month=february).dsos["ores"]
    assert stand_in.distribution_single == pytest.approx(grid_2026.single * 1.06)
    assert stand_in.transport == pytest.approx(transport.rate * 1.06)

    cwape._grids["ores"]["2027"] = grid_2027
    billed = _resolve_snapshot(entry, snap, delivery_month=february).dsos["ores"]
    assert billed.distribution_single == pytest.approx(grid_2027.single * 1.06)
    assert billed.distribution_single == pytest.approx(0.123307, abs=1e-6)
    assert billed.distribution_peak == pytest.approx(grid_2027.peak * 1.06)
    assert billed.data_management_per_year == pytest.approx(
        grid_2027.fixed_per_year * 1.06
    )
    assert billed.prosumer_eur_per_kva_year == pytest.approx(
        grid_2027.prosumer_per_kva_year * 1.06
    )


def test_a_withdrawn_brussels_card_bills_brugel_s_sheet(
    _brugel_2026: brugel.NetworkTerms,
) -> None:
    """Mega's Off-peak Fixed, on its October 2026 Brussels card, rounds
    Sibelga's rates to 9,96 and 7,53 c/kWh. A 2027 month bills Brugel's 2026
    sheet standing in, to its last decimal, with the power term of each band;
    the card's own year keeps its table."""
    terms = _brugel_2026
    text = extract_pdf_text(
        (_FIXTURES / "mega_offpeak_fixed_b_2026-10.pdf").read_bytes()
    )
    snap = mega.parse_snapshot("mega_offpeak_fixed", text, "brussels")
    entry = _entry("mega", "mega_offpeak_fixed", "brussels", "sibelga")
    own = _resolve_snapshot(entry, snap, delivery_month=date(2026, 10, 1))
    assert own.dsos["sibelga"].distribution_single == pytest.approx(0.0996)
    billed = _resolve_snapshot(entry, snap, delivery_month=date(2027, 2, 1))
    overlay = billed.dsos["sibelga"]
    assert overlay.distribution_single == pytest.approx(terms.day * 1.06)
    assert overlay.distribution_offpeak == pytest.approx(terms.night * 1.06)
    assert overlay.transport == pytest.approx(terms.transport * 1.06)
    assert overlay.data_management_per_year == pytest.approx(terms.fixed_le13 * 1.06)
    assert overlay.brussels_power_term_above_13kva == pytest.approx(
        terms.fixed_gt13 * 1.06
    )
    assert overlay.distribution_single != pytest.approx(0.0996)


def test_a_walk_bills_each_month_on_its_own_year_s_network(freezer: Any) -> None:
    """In January 2027 the card held is resolved on the sheet, and a walk
    standing it in for December 2026 bills that month on the card's own
    table, as the month's own card did; a 2027 month keeps the sheet. A
    product still sold stands in unchanged."""
    freezer.move_to("2027-01-15 12:00:00+01:00")
    _hold_zenne_dijle_2026()
    raw = _lifepowr()
    entry = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE)
    current = _resolve_snapshot(entry, raw)
    assert current.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        0.0553921 * 1.06
    )
    december = _proxy_for_month(entry, current, raw, date(2026, 12, 1))
    assert december.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(0.0587)
    assert december.dsos["fluvius_antwerpen"] == current.dsos["fluvius_antwerpen"]
    march = _proxy_for_month(entry, current, raw, date(2027, 3, 1))
    assert march.dsos == current.dsos

    flex_raw = trevion.parse_snapshot(
        "groene_stroom_flex", fixture_text("trevion_flex_2026-09.pdf", layout=True)
    )
    flex_entry = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE)
    flex = _resolve_snapshot(flex_entry, flex_raw)
    assert (
        _proxy_for_month(flex_entry, flex, flex_raw, date(2026, 12, 1)).dsos
        == flex.dsos
    )


async def test_the_sheet_of_a_withdrawn_product_s_region_is_read() -> None:
    """The tick, the backfill and an earlier contract read the sheet for a
    withdrawn product as for a custom entry taking its network from one:
    Fluvius's area sheet, Brugel's with the year before, or the operator's
    grid. Nothing for a product still sold."""
    session = object()
    area = AsyncMock()
    power = AsyncMock()
    grid = AsyncMock()
    with (
        patch.object(snapshot_resolve, "ensure_area_terms", area),
        patch.object(snapshot_resolve, "ensure_power_term", power),
        patch.object(snapshot_resolve, "ensure_cwape_network", grid),
    ):
        flemish = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE).data
        await snapshot_resolve.ensure_custom_network(session, flemish, 2027)  # type: ignore[arg-type]
        area.assert_awaited_once_with(session, 2027, _ZENNE_DIJLE)
        brussels = _entry("mega", "mega_offpeak_fixed", "brussels", "sibelga").data
        await snapshot_resolve.ensure_custom_network(session, brussels, 2027)  # type: ignore[arg-type]
        assert power.await_args_list == [call(session, 2027), call(session, 2026)]
        walloon = _entry("mega", "mega_offpeak_fixed", "wallonia", "ores").data
        await snapshot_resolve.ensure_custom_network(session, walloon, 2027)  # type: ignore[arg-type]
        grid.assert_awaited_once_with(session, 2027, "ores")
        for data in (flemish, brussels, walloon):
            assert snapshot_resolve.reads_network_sheet(data)
        area.reset_mock()
        sold = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE).data
        await snapshot_resolve.ensure_custom_network(session, sold, 2027)  # type: ignore[arg-type]
        area.assert_not_awaited()
        assert not snapshot_resolve.reads_network_sheet(sold)


@pytest.mark.parametrize(
    ("contract", "kept"), [("mega_offpeak_fixed", True), ("mega_smart_fixed", False)]
)
async def test_a_withdrawn_entry_keeps_what_the_archive_held_of_the_sheets(
    hass: HomeAssistant, contract: str, kept: bool
) -> None:
    """What the card archive kept of the network sheets stands in for a month
    no sheet was read for, so an entry that may take its network from one
    keeps it over a restart, as a custom entry taking a sheet does, and a
    product still sold does not."""
    entry = _entry("mega", contract, "wallonia", "ores")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    network_archive.hold({"months": {"2027-02": {"ores": {"transport": 0.026}}}})
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert ("network_archive" in saved[-1]) is kept


async def test_a_sheet_read_after_the_card_is_resolved_reaches_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A card resolved before its year's sheet was held, after a restart or a
    read that failed, takes the sheet once it is read: the tick re-resolves
    when what the sheet gives the entry moves, as it does for the ceiling."""
    freezer.move_to("2027-02-15 12:00:00+01:00")
    entry = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(_lifepowr())
    assert coord._snapshot is not None
    assert coord._snapshot.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        0.0587
    )
    _hold_zenne_dijle_2026()
    coord._reresolve_snapshot()
    assert coord._snapshot.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        0.0553921 * 1.06
    )


def test_a_withdrawn_month_waits_for_its_sheet(freezer: Any) -> None:
    """A backfill leaves out a month of this year that a withdrawn product's
    card does not cover while no sheet of the year or the one before is held,
    as it does for a custom entry, rather than writing it on the card's
    table for good. The card's own year, and a product still sold, never
    wait."""
    freezer.move_to("2027-02-15 12:00:00+01:00")
    february = date(2027, 2, 1)
    lifepowr = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE).data
    sold = _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE).data
    assert snapshot_resolve.network_sheet_unread(lifepowr, february)
    assert not snapshot_resolve.network_sheet_unread(sold, february)
    _hold_zenne_dijle_2026()
    assert not snapshot_resolve.network_sheet_unread(lifepowr, february)
    fluvius._area.clear()
    freezer.move_to("2026-11-15 12:00:00+01:00")
    assert not snapshot_resolve.network_sheet_unread(lifepowr, date(2026, 11, 1))


def test_a_flemish_withdrawn_entry_never_takes_brugel_s_sheet(
    _brugel_2026: brugel.NetworkTerms,
) -> None:
    """The sheets are held per process, so a Brussels entry beside this one
    holds Brugel's. A Flemish withdrawn entry takes its own region's sheet
    and nothing else: with no Fluvius sheet held its card's table stands."""
    snap = _lifepowr()
    entry = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE)
    later = _resolve_snapshot(entry, snap, delivery_month=date(2027, 2, 1))
    assert _network(later.dsos[_ZENNE_DIJLE]) == _network(snap.dsos[_ZENNE_DIJLE])


async def test_what_the_archive_kept_of_the_sheet_reaches_a_resolved_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """With no sheet read, what the card archive kept of the month stands in
    for it, and it can come in after the card was resolved: the tick reads
    network.json once a day. The stamp holds both, so the card takes it."""
    freezer.move_to("2027-02-15 12:00:00+01:00")
    entry = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(_lifepowr())
    assert coord._snapshot is not None
    assert coord._snapshot.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        0.0587
    )
    sheet = fluvius.parse_area_terms(
        (_FIXTURES / "fluvius_zenne_dijle_2026.xlsx").read_bytes(), 2026
    )
    assert sheet is not None
    network_archive.hold(
        {"months": {"2027-02": {_ZENNE_DIJLE: sheet.overlay_fields()}}}
    )
    assert not fluvius._area
    coord._reresolve_snapshot()
    assert coord._snapshot.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        sheet.kwh_normal * 1.06
    )


_AREA_OF = {"flanders": _ZENNE_DIJLE, "brussels": "sibelga", "wallonia": "ores"}


@pytest.mark.usefixtures("_cwape_held")
def test_the_hold_and_the_resolver_agree_on_every_withdrawn_card_s_year(
    freezer: Any,
) -> None:
    """Both read the last card's year off one answer (``last_card_year``),
    so for every withdrawn product registered, in every region it is sold
    in, the first month after that year is billed on the sheet and held for
    it when none is read, and the last month of that year is neither."""
    from custom_components.be_electricity_prices.providers import EXTRACTORS

    from . import make_snapshot

    cases = [
        (ex.id, c.id, region)
        for ex in EXTRACTORS.values()
        for c in ex.contracts
        if c.withdrawn is not None
        for region in sorted(c.regions)
    ]
    assert len(cases) >= 10
    for supplier, contract, region in cases:
        year = snapshot_resolve.last_card_year(supplier, contract)
        assert year is not None
        entry = _entry(supplier, contract, region, _AREA_OF[region])
        snap = make_snapshot(supplier=supplier, contract=contract)
        for month, past in ((date(year + 1, 1, 1), True), (date(year, 12, 1), False)):
            freezer.move_to(f"{month.isoformat()} 12:00:00+01:00")
            billed = snapshot_resolve.past_last_card(entry, snap, month, region)
            held = snapshot_resolve.network_sheet_unread(entry.data, month)
            assert (billed, held) == (past, past), (supplier, contract, region, month)
