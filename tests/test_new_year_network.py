"""A card held into a year after its own: the network of the new year.

A card prints its own year's network tariffs. Held into January while its
supplier's next card is not out, a month of the new year takes its network
off the regulator's sheet of that year once it is read, and keeps the card's
table until then (``network_year``).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    brugel,
    cwape,
    fluvius,
    network_year,
    snapshot_resolve,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers import mega, trevion
from custom_components.be_electricity_prices.providers._pdf_render import (
    extract_pdf_text,
)
from custom_components.be_electricity_prices.providers.base import SupplierSnapshot
from custom_components.be_electricity_prices.snapshot_resolve import (
    _proxy_for_month,
    _resolve_snapshot,
)

from . import fixture_text

_FIXTURES = Path(__file__).parent / "fixtures"
_ZENNE_DIJLE = "fluvius_zenne_dijle"
_FEBRUARY = date(2027, 2, 1)


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


def _flex() -> SupplierSnapshot:
    """Trevion's Groene Stroom Flex, still sold, on its September 2026 card."""
    return trevion.parse_snapshot(
        "groene_stroom_flex", fixture_text("trevion_flex_2026-09.pdf", layout=True)
    )


def _flex_entry() -> MockConfigEntry:
    return _entry("trevion", "groene_stroom_flex", "flanders", _ZENNE_DIJLE)


def _zenne_dijle(year: int) -> fluvius.AreaTerms:
    terms = fluvius.parse_area_terms(
        (_FIXTURES / "fluvius_zenne_dijle_2026.xlsx").read_bytes(), 2026
    )
    assert terms is not None
    return terms if year == 2026 else replace(terms, kwh_normal=0.0611)


@pytest.fixture
def _fluvius_held() -> Iterator[None]:
    yield
    fluvius._area.clear()


@pytest.fixture
def _cwape_held() -> Iterator[None]:
    yield
    cwape._grids.clear()
    cwape._transport.clear()


@pytest.fixture
def _brugel_held() -> Iterator[brugel.NetworkTerms]:
    sheet = (_FIXTURES / "brugel_elec_2026.pdf").read_bytes()
    terms = brugel._parse_network(extract_pdf_text(sheet))
    assert terms is not None
    yield terms
    brugel._network.clear()


@pytest.mark.usefixtures("_fluvius_held")
def test_a_flemish_card_bills_the_new_year_on_its_sheet_once_read() -> None:
    """Flex's September 2026 card held into 2027: with only Fluvius's 2026
    sheet held a 2027 month keeps the card's table, the sheet of the year
    before saying nothing the card does not. Once the 2027 sheet is read the
    entry's area bills it, on the card's VAT basis; the other areas and the
    months of 2026 keep the card's figures."""
    snap = _flex()
    entry = _flex_entry()
    fluvius._area[(2026, _ZENNE_DIJLE)] = _zenne_dijle(2026)
    held = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY)
    assert (
        held.dsos[_ZENNE_DIJLE]
        == _resolve_snapshot(entry, snap, delivery_month=date(2026, 10, 1)).dsos[
            _ZENNE_DIJLE
        ]
    )

    terms = _zenne_dijle(2027)
    fluvius._area[(2027, _ZENNE_DIJLE)] = terms
    billed = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY)
    overlay = billed.dsos[_ZENNE_DIJLE]
    assert overlay.distribution_single == pytest.approx(0.0611 * 1.06)
    assert overlay.capacity_eur_per_kw_year == pytest.approx(
        terms.capacity_per_kw_year * 1.06
    )
    assert overlay.data_management_per_year == pytest.approx(
        terms.data_management_per_year * 1.06
    )
    assert billed.dsos["fluvius_antwerpen"] == held.dsos["fluvius_antwerpen"]
    own = _resolve_snapshot(entry, snap, delivery_month=date(2026, 12, 1))
    assert own.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        snap.dsos[_ZENNE_DIJLE].distribution_single
    )


@pytest.mark.usefixtures("_fluvius_held")
def test_a_card_of_the_new_year_or_with_no_date_keeps_its_table() -> None:
    """The rule reads the card's own year: a card of 2027 keeps its table in
    2027, and one that states no date and names no month is left alone."""
    fluvius._area[(2027, _ZENNE_DIJLE)] = _zenne_dijle(2027)
    entry = _flex_entry()
    card = _flex()
    for snap in (
        replace(card, valid_until=date(2027, 1, 31)),
        replace(card, valid_until=None, publication_label=""),
    ):
        billed = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY)
        assert billed.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
            snap.dsos[_ZENNE_DIJLE].distribution_single
        )


def test_a_brussels_card_bills_brugel_s_sheet_of_the_new_year(
    _brugel_held: brugel.NetworkTerms,
) -> None:
    """Mega's Smart Fixed on its Brussels card: Brugel's 2026 sheet alone
    leaves a 2027 month on the card's table, and the 2027 sheet once read
    bills it."""
    terms = _brugel_held
    text = extract_pdf_text((_FIXTURES / "mega_smart_fixed_b.pdf").read_bytes())
    snap = replace(
        mega.parse_snapshot("mega_smart_fixed", text, "brussels"),
        valid_until=date(2026, 12, 31),
    )
    entry = _entry("mega", "mega_smart_fixed", "brussels", "sibelga")
    brugel._network[2026] = terms
    held = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY)
    assert held.dsos["sibelga"].distribution_single == pytest.approx(
        snap.dsos["sibelga"].distribution_single
    )
    brugel._network[2027] = replace(terms, day=0.1123)
    billed = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY)
    assert billed.dsos["sibelga"].distribution_single == pytest.approx(0.1123 * 1.06)
    assert billed.dsos["sibelga"].transport == pytest.approx(terms.transport * 1.06)


@pytest.mark.usefixtures("_cwape_held")
def test_a_walloon_card_bills_the_cwape_grid_that_began_in_the_new_year() -> None:
    """Mega Dynamic on its Walloon card: ORES's 2026 grid valid into 2027
    would bill what the card prints, so only the grid that began in 2027
    does, each grid standing on its own, the transport of 2026 included."""
    snap = replace(
        mega.parse_snapshot(
            "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
        ),
        valid_until=date(2026, 12, 31),
    )
    entry = _entry("mega", "mega_dynamic", "wallonia", "ores")
    grid_2026 = cwape.parse_grid((_FIXTURES / "cwape_ores_2026.xlsx").read_bytes())
    grid_2027 = cwape.parse_grid((_FIXTURES / "cwape_ores_2027.xlsx").read_bytes())
    transport = cwape.parse_transport(
        (_FIXTURES / "cwape_transport_2026.xlsx").read_bytes()
    )
    assert grid_2026 is not None and grid_2027 is not None and transport is not None
    cwape._grids["ores"] = {"2026": grid_2026}
    cwape._transport["2026"] = transport
    card = snap.dsos["ores"]
    held = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY).dsos["ores"]
    assert held.distribution_single == pytest.approx(card.distribution_single)

    cwape._grids["ores"]["2027"] = grid_2027
    billed = _resolve_snapshot(entry, snap, delivery_month=_FEBRUARY).dsos["ores"]
    assert billed.distribution_single == pytest.approx(grid_2027.single * 1.06)
    assert billed.transport == pytest.approx(held.transport)


@pytest.mark.usefixtures("_fluvius_held")
def test_the_coordinator_stamps_the_new_year_s_sheet(freezer: Any) -> None:
    """The figures the new year's sheet gives a card of an earlier year are
    in the stamp the coordinator re-resolves on, so a sheet read after the
    card was resolved reaches it on the next tick; nothing for a card of the
    running year."""
    freezer.move_to("2027-01-15 12:00:00+01:00")
    data = _flex_entry().data
    snap = _flex()
    today = date(2027, 1, 15)
    assert snapshot_resolve.sheet_network(data, today, snap) == {}
    fluvius._area[(2027, _ZENNE_DIJLE)] = _zenne_dijle(2027)
    stamp = snapshot_resolve.sheet_network(data, today, snap)
    assert stamp["distribution_single"] == pytest.approx(0.0611)
    january = replace(snap, valid_until=date(2027, 1, 31))
    assert snapshot_resolve.sheet_network(data, today, january) == {}
    assert snapshot_resolve.sheet_network(data, today) == {}


async def test_the_tick_reads_the_new_year_s_sheet_for_an_older_card() -> None:
    """The sheet of the running year is read for an entry whose card prices
    an earlier one, and not for a card of the running year, an entry with no
    card yet, or one that reads its sheet anyway."""
    session = object()
    area = AsyncMock()
    data = _flex_entry().data
    snap = _flex()
    with patch.object(snapshot_resolve, "ensure_area_terms", area):
        await snapshot_resolve.ensure_new_year_sheet(session, data, snap, 2027)  # type: ignore[arg-type]
        area.assert_awaited_once_with(session, 2027, _ZENNE_DIJLE)
        area.reset_mock()
        await snapshot_resolve.ensure_new_year_sheet(session, data, snap, 2026)  # type: ignore[arg-type]
        await snapshot_resolve.ensure_new_year_sheet(session, data, None, 2027)  # type: ignore[arg-type]
        withdrawn = _entry("trevion", "lifepowr", "flanders", _ZENNE_DIJLE).data
        await snapshot_resolve.ensure_new_year_sheet(session, withdrawn, snap, 2027)  # type: ignore[arg-type]
        area.assert_not_awaited()


@pytest.mark.usefixtures("_fluvius_held")
def test_a_walk_bills_a_month_of_the_card_s_year_on_its_table(freezer: Any) -> None:
    """In January 2027 the held card is resolved on the 2027 sheet, and a
    walk standing it in for December 2026 bills that month on the card's
    own table; a 2027 month keeps the sheet."""
    freezer.move_to("2027-01-15 12:00:00+01:00")
    fluvius._area[(2027, _ZENNE_DIJLE)] = _zenne_dijle(2027)
    raw = _flex()
    entry = _flex_entry()
    current = _resolve_snapshot(entry, raw)
    assert current.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        0.0611 * 1.06
    )
    december = _proxy_for_month(entry, current, raw, date(2026, 12, 1))
    assert december.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(
        raw.dsos[_ZENNE_DIJLE].distribution_single
    )
    march = _proxy_for_month(entry, current, raw, date(2027, 3, 1))
    assert march.dsos == current.dsos


def test_card_year_reads_the_date_or_else_the_title() -> None:
    card = _flex()
    assert network_year.card_year(replace(card, valid_until=date(2026, 9, 30))) == 2026
    titled = replace(card, valid_until=None, publication_label="Septembre 2026")
    assert network_year.card_year(titled) == 2026
    assert network_year.card_year(replace(titled, publication_label="")) is None
