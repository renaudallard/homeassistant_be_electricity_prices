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

"""A closed month's card short of the entry's network area, completed from
the nearest month's card that prints it (``_with_nearest_area`` in
``snapshot_areas.py``, applied by ``month_card``)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices import snapshot_months
from custom_components.be_electricity_prices.providers import (
    aspiravi,
    cociter,
    ebem,
    elegant,
    eneco,
    octaplus,
)
from custom_components.be_electricity_prices.providers import get as get_extractor
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_areas import _nearest_months
from custom_components.be_electricity_prices.snapshot_months import (
    ArchivedCard,
    _snapshot_for_month,
    month_card,
    month_card_failed,
    monthly_rows_to_store,
    restore_monthly_rows,
)
from custom_components.be_electricity_prices.snapshot_resolve import _resolve_snapshot
from custom_components.be_electricity_prices.snapshot_store import _monthly_snapshots
from tests import fixture_text, make_entry, make_snapshot, make_stub_extractor

_NOW = "2026-10-16 12:00:00+02:00"
_AUGUST = date(2026, 8, 1)
# The current card, which no test here should be billed on.
_NO_CARD = make_snapshot(dsos={})


def _served(
    rows: dict[date, SupplierSnapshot | Exception], asked: list[date]
) -> Callable[..., Any]:
    """A ``fetch_for_month`` answering ``rows``, None for any other month,
    and noting every month it is asked for."""

    async def fetch_for_month(
        _session: object, _contract: str, _region: str, month: date
    ) -> SupplierSnapshot | None:
        asked.append(month)
        row = rows.get(month)
        if isinstance(row, Exception):
            raise row
        return row

    return fetch_for_month


def _stub(rows: dict[date, SupplierSnapshot | Exception], asked: list[date]) -> Any:
    return replace(make_stub_extractor(), fetch_for_month=_served(rows, asked))


def _area(rate: float) -> DsoOverlay:
    return DsoOverlay(distribution_single=rate, transport=0.0145)


def _card(**areas: float) -> SupplierSnapshot:
    return make_snapshot(dsos={key: _area(rate) for key, rate in areas.items()})


async def _billed(
    hass: HomeAssistant,
    extractor: SupplierExtractor,
    month: date,
    *,
    dso: str = "resa",
    cached_only: bool = False,
) -> DsoOverlay | None:
    """The overlay ``month`` is billed on for an entry on ``dso``."""
    card = await _snapshot_for_month(
        hass,
        None,  # type: ignore[arg-type]
        extractor,
        "test",
        "wallonia",
        month,
        _NO_CARD,
        make_entry(dso=dso, card_archive=False),
        cached_only=cached_only,
    )
    assert card is not _NO_CARD
    return card.dsos.get(dso)


def test_the_months_asked_run_nearest_first_within_the_calendar_year() -> None:
    """Only the month's own calendar year, since network tariffs change on 1
    January, the earlier of two equally near first, and nothing after the
    running month, which has no card yet."""
    assert _nearest_months(date(2026, 3, 1), date(2026, 10, 1)) == [
        date(2026, 2, 1),
        date(2026, 4, 1),
        date(2026, 1, 1),
        date(2026, 5, 1),
        date(2026, 6, 1),
        date(2026, 7, 1),
        date(2026, 8, 1),
        date(2026, 9, 1),
        date(2026, 10, 1),
    ]
    assert _nearest_months(date(2025, 12, 1), date(2026, 10, 1)) == [
        date(2025, 12 - n, 1) for n in range(1, 12)
    ]


def _parsed(supplier: str, text: str) -> SupplierSnapshot:
    if supplier == "aspiravi":
        return aspiravi.parse_snapshot("aspiravi_eco_plus_flex", text)
    if supplier == "cociter":
        return cociter.parse_snapshot(text, "cociter_variable", "test://", "2026-04")
    if supplier == "ebem":
        return ebem.parse_snapshot("ebem_variable", text)
    if supplier == "elegant":
        return elegant.parse_snapshot("elegant_zeker_vast", text)
    if supplier == "eneco":
        return eneco.parse_snapshot(text, "power_fix", "test://", "wallonia")
    return octaplus.parse_snapshot("octaplus_fixed", text, "flanders")


# Per supplier: the contract and region, the card, the row relabelled so it
# no longer reads, and the area that leaves out.
_SHORT_CARDS = {
    "aspiravi": (
        ("aspiravi_eco_plus_flex", "flanders"),
        lambda: fixture_text("aspiravi_eco_plus_flex_2026-09.pdf"),
        ("Fluvius (Imewo)", "Fluvius (Gent)"),
        "fluvius_imewo",
    ),
    "cociter": (
        ("cociter_variable", "wallonia"),
        lambda: fixture_text("cociter_var_2604.pdf"),
        ("\nREW ", "\nR.E.W. "),
        "rew",
    ),
    "ebem": (
        ("ebem_variable", "flanders"),
        lambda: fixture_text("ebem_variable_2026-05.pdf", layout=True),
        ("Fluvius West", "Fluvius W."),
        "fluvius_west",
    ),
    "elegant": (
        ("elegant_zeker_vast", "flanders"),
        lambda: fixture_text("elegant_zeker_vast_2026-09.pdf", layout=True),
        ("Fluvius Midden-Vl.", "Fluvius Gent"),
        "fluvius_intergem",
    ),
    "eneco": (
        ("power_fix", "wallonia"),
        lambda: fixture_text("eneco_fix.pdf"),
        ("FLUVIUS WEST", "FLUVIUS W."),
        "fluvius_west",
    ),
    "octaplus": (
        ("octaplus_fixed", "flanders"),
        lambda: fixture_text("octaplus_fixed_v.pdf", aligned=True),
        ("Fluvius West", "X."),
        "fluvius_west",
    ),
}


@pytest.mark.parametrize("supplier", sorted(_SHORT_CARDS))
async def test_a_closed_month_short_of_an_area_is_billed_on_the_nearest_card(
    hass: HomeAssistant, freezer: Any, supplier: str
) -> None:
    """Each supplier's reader keeps a past card whose network table lost a
    row, and an entry on that area had the month dropped from every cost
    path. The month keeps its own card and takes that one area from the
    nearest month's card that prints it."""
    (contract, region), text, (old, new), dso = _SHORT_CARDS[supplier]
    full = _parsed(supplier, text())
    short = _parsed(supplier, text().replace(old, new))
    assert dso in full.dsos and set(short.dsos) == set(full.dsos) - {dso}
    freezer.move_to(_NOW)
    asked: list[date] = []
    extractor = replace(
        get_extractor(supplier),
        fetch_for_month=_served({_AUGUST: short, date(2026, 9, 1): full}, asked),
    )
    entry = make_entry(
        supplier=supplier,
        contract=contract,
        region=region,
        dso=dso,
        card_archive=False,
    )
    card = await _snapshot_for_month(
        hass,
        None,  # type: ignore[arg-type]
        extractor,
        contract,
        region,
        _AUGUST,
        _NO_CARD,
        entry,
    )
    whole = replace(short, dsos={**short.dsos, dso: full.dsos[dso]})
    assert card == _resolve_snapshot(entry, whole, delivery_month=_AUGUST)
    assert asked == [_AUGUST, date(2026, 7, 1), date(2026, 9, 1)]


async def test_no_card_of_another_year_is_taken(
    hass: HomeAssistant, freezer: Any
) -> None:
    """December is the nearest month to a short January, and its network is
    the year before's: March's card of the same year is taken, and with
    none in that year the month stays short."""
    freezer.move_to(_NOW)
    january = date(2026, 1, 1)
    rows: dict[date, SupplierSnapshot | Exception] = {
        january: _card(ores=0.10),
        date(2025, 12, 1): _card(ores=0.10, resa=0.07),
        date(2026, 3, 1): _card(ores=0.10, resa=0.09),
    }
    assert await _billed(hass, _stub(rows, []), january) == _area(0.09)
    del rows[date(2026, 3, 1)]
    _monthly_snapshots(hass).clear()
    asked: list[date] = []
    assert await _billed(hass, _stub(rows, asked), january) is None
    assert date(2025, 12, 1) not in asked


async def test_the_earlier_of_two_equally_near_months_is_taken(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to(_NOW)
    rows: dict[date, SupplierSnapshot | Exception] = {
        date(2026, 3, 1): _card(ores=0.10),
        date(2026, 2, 1): _card(ores=0.10, resa=0.08),
        date(2026, 4, 1): _card(ores=0.10, resa=0.09),
    }
    assert await _billed(hass, _stub(rows, []), date(2026, 3, 1)) == _area(0.08)


async def test_a_nearer_month_not_read_takes_no_farther_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """July is asked before September. While its read fails the month stays
    short, as it was before, rather than take September's area and bill
    differently once July is read."""
    freezer.move_to(_NOW)
    asked: list[date] = []
    rows: dict[date, SupplierSnapshot | Exception] = {
        _AUGUST: _card(ores=0.10),
        date(2026, 7, 1): ExtractorError("HTTP 503 fetching test://"),
        date(2026, 9, 1): _card(ores=0.10, resa=0.09),
    }
    extractor = _stub(rows, asked)
    assert await _billed(hass, extractor, _AUGUST) is None
    assert date(2026, 9, 1) not in asked
    # A failed read, as the month's own would be, so a backfill retries it.
    assert month_card_failed(hass, "test", "test", "wallonia", _AUGUST, "resa")
    assert not month_card_failed(hass, "test", "test", "wallonia", _AUGUST)
    rows[date(2026, 7, 1)] = _card(ores=0.10, resa=0.08)
    freezer.tick(timedelta(hours=1))
    assert await _billed(hass, extractor, _AUGUST) == _area(0.08)
    assert not month_card_failed(hass, "test", "test", "wallonia", _AUGUST, "resa")


async def test_cached_only_takes_no_farther_card_than_it_holds(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The setup path may not fetch. With July never fetched, September's
    area is not taken in its place: the month stays short until the warm-up
    reads July."""
    freezer.move_to(_NOW)
    asked: list[date] = []
    rows: dict[date, SupplierSnapshot | Exception] = {
        _AUGUST: _card(ores=0.10),
        date(2026, 9, 1): _card(ores=0.10, resa=0.09),
    }
    extractor = _stub(rows, asked)
    for month in (_AUGUST, date(2026, 9, 1)):
        assert await _billed(hass, extractor, month, dso="ores") == _area(0.10)
    asked.clear()
    assert await _billed(hass, extractor, _AUGUST, cached_only=True) is None
    assert asked == []
    assert await _billed(hass, extractor, _AUGUST) == _area(0.09)
    assert await _billed(hass, extractor, _AUGUST, cached_only=True) == _area(0.09)


async def test_a_card_read_for_its_energy_asks_no_other_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A cohort reads its signing month's card for the energy and a credit,
    which bill no network: the area it lacks is not looked for."""
    freezer.move_to(_NOW)
    asked: list[date] = []
    rows: dict[date, SupplierSnapshot | Exception] = {
        _AUGUST: _card(ores=0.10),
        date(2026, 9, 1): _card(ores=0.10, resa=0.09),
    }
    card = await month_card(
        hass,
        None,  # type: ignore[arg-type]
        _stub(rows, asked),
        "test",
        "wallonia",
        _AUGUST,
        make_entry(dso="resa", card_archive=False),
    )
    assert card is not None and "resa" not in card.dsos
    assert asked == [_AUGUST]


async def test_an_area_the_card_prints_asks_no_other_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to(_NOW)
    asked: list[date] = []
    rows: dict[date, SupplierSnapshot | Exception] = {
        _AUGUST: _card(ores=0.10, resa=0.07)
    }
    assert await _billed(hass, _stub(rows, asked), _AUGUST) == _area(0.07)
    assert asked == [_AUGUST]


async def test_no_card_of_its_year_leaves_the_month_short(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing of its year prints the area, the running month included: the
    month is dropped as before, and neither another year nor a month after
    the running one is asked."""
    freezer.move_to(_NOW)
    asked: list[date] = []
    rows: dict[date, SupplierSnapshot | Exception] = {
        _AUGUST: _card(ores=0.10),
        date(2025, 8, 1): _card(ores=0.10, resa=0.07),
    }
    assert await _billed(hass, _stub(rows, asked), _AUGUST) is None
    assert sorted(asked) == [date(2026, month, 1) for month in range(1, 11)]
    assert not month_card_failed(hass, "test", "test", "wallonia", _AUGUST, "resa")


async def test_a_card_with_no_network_table_is_not_searched_around(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Engie's Walloon cards of October to December 2025 were read before
    their layout was, and come back with no network table at all, while the
    card archive holds nothing of 2025 before them. Searching their year
    for the area asked Engie's own archive for each of the nine months
    before October, on a cold start and again after every restart, to bill
    a network never read. Such a month is dropped as it was, and is no
    failed read either."""
    freezer.move_to(_NOW)
    quarter = [date(2025, month, 1) for month in (10, 11, 12)]
    archive_asked: list[date] = []
    supplier_asked: list[date] = []
    empty = make_snapshot(supplier="engie", contract="engie_easy_fixed", dsos={})

    async def archive(*args: Any) -> ArchivedCard | None:
        archive_asked.append(args[4])
        if args[4] in quarter:
            return ArchivedCard(snapshot=empty, read_by_ocr=False)
        return None

    extractor = replace(
        get_extractor("engie"), fetch_for_month=_served({}, supplier_asked)
    )
    entry = make_entry(supplier="engie", contract="engie_easy_fixed", dso="ores")

    async def walk() -> None:
        for month in quarter:
            card = await _snapshot_for_month(
                hass,
                None,  # type: ignore[arg-type]
                extractor,
                "engie_easy_fixed",
                "wallonia",
                month,
                _NO_CARD,
                entry,
            )
            assert card is not _NO_CARD and "ores" not in card.dsos
            assert not month_card_failed(
                hass, "engie", "engie_easy_fixed", "wallonia", month, "ores"
            )

    with patch.object(snapshot_months, "_archived_card_from_github", archive):
        await walk()
        assert (archive_asked, supplier_asked) == (quarter, [])
        blob = monthly_rows_to_store(
            hass,
            "engie",
            "engie_easy_fixed",
            "wallonia",
            [date(2026, month, 1) for month in range(1, 11)],
        )
        _monthly_snapshots(hass).clear()
        restore_monthly_rows(hass, "engie", "engie_easy_fixed", "wallonia", blob)
        archive_asked.clear()
        await walk()
        assert (archive_asked, supplier_asked) == (quarter, [])
