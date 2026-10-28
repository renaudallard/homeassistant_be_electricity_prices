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

"""Fluvius's yearly tariff sheet and the VREG network ceiling read off it."""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any

import pytest

from custom_components.be_electricity_prices import fluvius
from tests import FakeBody

_FIXTURES = Path(__file__).parent / "fixtures"
# The 2026 sheet of one area, as Fluvius serves it.
_SHEET_2026 = (_FIXTURES / "fluvius_zenne_dijle_2026.xlsx").read_bytes()
# A 2023 sheet, whose transformer-station column is headed "piekmeting" too and
# comes before the low-voltage one.
_SHEET_2023 = (_FIXTURES / "fluvius_antwerpen_2023.xlsx").read_bytes()

_PUBLICATION = (
    "/nl/publicatie/fluvius-zenne-dijle-distributienettarief-afname-elektriciteit"
    "-01012026-31122026"
)
_WORKBOOK = (
    "/sites/fluvius/files/2025-11/distributienettarieven-elektriciteit-afname-"
    "fluvius-zenne-dijle-01012026-31122026.xlsx"
)


@pytest.fixture(autouse=True)
def _nothing_held() -> None:
    """conftest holds the published figures; these tests read them."""
    fluvius._cache.clear()
    fluvius._failed_at.clear()
    fluvius._analog.clear()
    fluvius._analog_failed_at.clear()
    fluvius._stored.clear()
    fluvius._stored_analog.clear()
    fluvius._listing = None


def test_the_ceiling_and_floor_are_read_off_the_years_sheet() -> None:
    """The floor is the minimum contribution over the capacity rate: 140,31
    EUR a year at 56,1228635 EUR/kW on Zenne-Dijle in 2026."""
    assert fluvius.parse_sheet(_SHEET_2026, 2026) == fluvius.SheetTerms(0.3276168, 2.5)
    # Another year's sheet is not this year's.
    assert fluvius.parse_sheet(_SHEET_2026, 2027) is None


def test_the_low_voltage_column_is_found_by_its_capacity_rate() -> None:
    """Up to 2024 the first "piekmeting" column is the transformer stations',
    whose maximumtarief is 0,0905158; the household's is the capacity rate's."""
    assert fluvius.parse_sheet(_SHEET_2023, 2023) == fluvius.SheetTerms(0.1920264, 2.5)


def test_a_figure_outside_the_bounds_is_refused(tmp_path: Path) -> None:
    """A unit slip is not a ceiling: the same sheet printing ten times the
    figure yields nothing rather than a cap ten times too loose."""
    out = tmp_path / "slipped.xlsx"
    with (
        zipfile.ZipFile(_FIXTURES / "fluvius_zenne_dijle_2026.xlsx") as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                assert data.count(b"<v>0.32761679999999999</v>") == 1
                data = data.replace(
                    b"<v>0.32761679999999999</v>", b"<v>3.2761679999999999</v>"
                )
            dst.writestr(info, data)
    assert fluvius.parse_sheet(out.read_bytes(), 2026) is None


def test_the_analog_surcharge_is_the_two_kwh_rates_apart() -> None:
    """An analog meter's kWh rate less the peak-metered one, from the area's own
    sheet: 0,0631932 - 0,0271123 on Zenne-Dijle in 2026, and in 2023, when the
    analog column was headed "klassieke meter" and sat one column further,
    0,0276308 - 0,0090800 on Antwerpen. The public service and surcharge rates
    agree between the two columns on every sheet from 2023 to 2026."""
    assert fluvius.parse_analog_surcharge(_SHEET_2026, 2026) == 0.0360809
    assert fluvius.parse_analog_surcharge(_SHEET_2023, 2023) == 0.0185508
    assert fluvius.parse_analog_surcharge(_SHEET_2026, 2025) is None


def test_an_analog_column_billing_other_public_service_rates_is_refused(
    tmp_path: Path,
) -> None:
    """The kWh rates are the whole difference only while the rest of the
    per-kWh network term is the same for both meters."""
    out = tmp_path / "split.xlsx"
    with (
        zipfile.ZipFile(_FIXTURES / "fluvius_zenne_dijle_2026.xlsx") as src,
        zipfile.ZipFile(out, "w") as dst,
    ):
        for info in src.infolist():
            data = src.read(info)
            if info.filename == "xl/worksheets/sheet1.xml":
                cell = b'<c r="O30" s="'
                assert data.count(cell) == 1
                start = data.index(cell)
                value = data.index(b"<v>", start) + 3
                end = data.index(b"</v>", value)
                data = data[:value] + b"0.03" + data[end:]
            dst.writestr(info, data)
    assert fluvius.parse_analog_surcharge(out.read_bytes(), 2026) is None


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.status = status
        self.content_length = None
        self.charset = "utf-8"
        self.history = ()
        self.content = FakeBody(body)

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    """Fluvius's listing, publication page and workbook, by address."""

    def __init__(self, workbook: bytes = _SHEET_2026) -> None:
        self.pages = {
            fluvius._LISTING_URL: f'<a href="{_PUBLICATION}">Zenne-Dijle</a>'.encode(),
            fluvius._SITE + _PUBLICATION: f'<a href="{_WORKBOOK}">XLSX</a>'.encode(),
            fluvius._SITE + _WORKBOOK: workbook,
        }
        self.asked: list[str] = []

    def get(self, url: str, *_a: object, **_k: object) -> _Response:
        self.asked.append(url)
        body = self.pages.get(url)
        return _Response(b"", 404) if body is None else _Response(body)


async def test_the_sheet_is_found_from_the_listing_and_kept() -> None:
    session: Any = _Session()
    terms = await fluvius.ensure_tariff_sheet(session, 2026)
    assert terms is not None and terms.ceiling == 0.3276168
    assert session.asked == [
        fluvius._LISTING_URL,
        fluvius._SITE + _PUBLICATION,
        fluvius._SITE + _WORKBOOK,
    ]
    assert fluvius.cached_network_ceiling(2026) == 0.3276168
    session.asked.clear()
    assert await fluvius.ensure_tariff_sheet(session, 2026) == terms
    assert session.asked == [], "a held year was fetched again"


async def test_a_year_not_listed_yet_yields_nothing_until_the_backoff() -> None:
    session: Any = _Session()
    assert await fluvius.ensure_tariff_sheet(session, 2027) is None
    assert fluvius.cached_network_ceiling(2027) is None
    session.asked.clear()
    assert await fluvius.ensure_tariff_sheet(session, 2027) is None
    assert session.asked == [], "the backoff did not hold"


async def test_an_analog_meter_reads_its_own_areas_sheet() -> None:
    """Not the first one listed: the surcharge is the area's. Kept, standing
    in for a later year Fluvius has not published, never for an earlier one."""
    session: Any = _Session()
    # Antwerpen's page listed first, as Fluvius orders them: the first sheet is
    # not the one an analog meter on Zenne-Dijle reads.
    antwerpen = _PUBLICATION.replace("zenne-dijle", "antwerpen")
    session.pages[fluvius._LISTING_URL] = (
        f'<a href="{antwerpen}">Antwerpen</a><a href="{_PUBLICATION}">Zenne-Dijle</a>'
    ).encode()
    session.pages[fluvius._SITE + antwerpen] = b"<html></html>"
    surcharge = await fluvius.ensure_analog_surcharge(
        session, 2026, "fluvius_zenne_dijle"
    )
    assert surcharge == 0.0360809
    assert session.asked == [
        fluvius._LISTING_URL,
        fluvius._SITE + _PUBLICATION,
        fluvius._SITE + _WORKBOOK,
    ]
    session.asked.clear()
    assert (
        await fluvius.ensure_analog_surcharge(session, 2026, "fluvius_zenne_dijle")
        == surcharge
    )
    assert session.asked == [], "a held sheet was fetched again"
    assert fluvius.cached_analog_surcharge(2027, "fluvius_zenne_dijle") == surcharge
    assert fluvius.cached_analog_surcharge(2025, "fluvius_zenne_dijle") is None
    assert fluvius.cached_analog_surcharge(2026, "fluvius_antwerpen") is None
    # Not a Fluvius area: nothing to ask for.
    assert await fluvius.ensure_analog_surcharge(session, 2026, "ores") is None
    assert session.asked == []


async def test_an_area_sheet_that_cannot_be_read_never_raises() -> None:
    session: Any = _Session(b"<html>maintenance</html>")
    assert (
        await fluvius.ensure_analog_surcharge(session, 2026, "fluvius_zenne_dijle")
        is None
    )
    assert (2026, "fluvius_zenne_dijle") in fluvius._analog_failed_at
    session.asked.clear()
    assert (
        await fluvius.ensure_analog_surcharge(session, 2026, "fluvius_zenne_dijle")
        is None
    )
    assert session.asked == [], "the backoff did not hold"


async def test_one_listing_serves_every_sheet_a_tick_reads() -> None:
    """The year's sheet and an analog meter's area sheet are found from the
    same listing, downloaded once for both."""
    session: Any = _Session()
    assert await fluvius.ensure_tariff_sheet(session, 2026) is not None
    assert (
        await fluvius.ensure_analog_surcharge(session, 2026, "fluvius_zenne_dijle")
        == 0.0360809
    )
    assert session.asked.count(fluvius._LISTING_URL) == 1


async def test_a_listing_that_does_not_answer_is_asked_for_once() -> None:
    """A Fluvius that does not answer cost one 30 s timeout per sheet a tick
    read, three on an analog meter's first tick, with the year before asked as
    well; now one, and no year before after a network error."""
    from unittest.mock import patch

    from custom_components.be_electricity_prices.providers.base import (
        ExtractorError,
    )

    asked: list[str] = []

    async def _hangs(_session: Any, url: str, **_k: Any) -> str:
        asked.append(url)
        raise ExtractorError("network error fetching the listing: timeout")

    session: Any = _Session()
    with patch.object(fluvius, "fetch_text", _hangs):
        assert await fluvius.ensure_tariff_sheet(session, 2026) is None
        assert (
            await fluvius.ensure_analog_surcharge(session, 2026, "fluvius_zenne_dijle")
            is None
        )
    assert asked == [fluvius._LISTING_URL]
    assert set(fluvius._analog_failed_at) == {(2026, "fluvius_zenne_dijle")}


async def test_a_year_not_published_yet_stands_on_the_year_before() -> None:
    """In January, before Fluvius publishes the year, the year before is read
    off the same listing and answers for it; the year is asked again later."""
    session: Any = _Session()
    assert (
        await fluvius.ensure_analog_surcharge(session, 2027, "fluvius_zenne_dijle")
        is None
    )
    assert fluvius.cached_analog_surcharge(2027, "fluvius_zenne_dijle") == 0.0360809
    assert (2027, "fluvius_zenne_dijle") in fluvius._analog_failed_at
    assert session.asked.count(fluvius._LISTING_URL) == 1


async def test_a_sheet_that_no_longer_reads_stands_on_the_year_before(
    caplog: Any,
) -> None:
    """A layout change leaving the year's sheet unreadable dropped the analog
    rate to none, about 129 EUR a year short on Kempen at 3.500 kWh where the
    year before stands within some 15. It stands in, and the sheet that no
    longer reads is warned of. A network error still asks for nothing more."""
    session: Any = _Session()
    page_2027 = _PUBLICATION.replace("2026", "2027")
    workbook_2027 = _WORKBOOK.replace("2026", "2027")
    session.pages[fluvius._LISTING_URL] = (
        f'<a href="{page_2027}">2027</a><a href="{_PUBLICATION}">2026</a>'
    ).encode()
    session.pages[fluvius._SITE + page_2027] = (
        f'<a href="{workbook_2027}">XLSX</a>'.encode()
    )
    # The 2026 workbook under the 2027 page: it does not title itself 2027.
    session.pages[fluvius._SITE + workbook_2027] = _SHEET_2026
    assert (
        await fluvius.ensure_analog_surcharge(session, 2027, "fluvius_zenne_dijle")
        is None
    )
    assert fluvius.cached_analog_surcharge(2027, "fluvius_zenne_dijle") == 0.0360809
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("the 2026 one" in message for message in warned), warned
    # The workbook not fetched at all: that heals, and nothing more is asked.
    fluvius._analog.clear()
    fluvius._analog_failed_at.clear()
    fluvius._listing = None
    del session.pages[fluvius._SITE + workbook_2027]
    session.asked.clear()
    assert (
        await fluvius.ensure_analog_surcharge(session, 2027, "fluvius_zenne_dijle")
        is None
    )
    assert fluvius.cached_analog_surcharge(2027, "fluvius_zenne_dijle") is None
    assert fluvius._SITE + _PUBLICATION not in session.asked


async def test_a_closed_year_without_a_sheet_is_not_warned_of(
    caplog: Any, freezer: Any
) -> None:
    """Kempen had no sheet of its own before 2025, and none is coming: a
    backfill reaching 2024 is told quietly. This year's missing sheet, which
    heals once Fluvius publishes it, is warned of."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    session: Any = _Session()
    for year in (2024, 2026):
        assert (
            await fluvius.ensure_analog_surcharge(session, year, "fluvius_iveka")
            is None
        )
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warned) == 1 and "2026" in warned[0], warned


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("a maintenance page answered with 200", b"<html>maintenance</html>"),
        ("an empty body", b""),
        ("a truncated workbook", _SHEET_2026[:4000]),
    ],
)
async def test_a_body_that_is_not_a_sheet_never_raises(label: str, body: bytes) -> None:
    """The fetch runs inside the coordinator tick, whose only handler is for
    UpdateFailed, so anything escaping here would take the device down."""
    session: Any = _Session(body)
    assert await fluvius.ensure_tariff_sheet(session, 2026) is None, label
    assert 2026 in fluvius._failed_at


def test_the_sheet_is_read_before_the_card_is_resolved() -> None:
    """The resolver reads the cache synchronously and cannot await, so the
    year's ceiling has to be there before the tick resolves its card, as
    Brugel's term does. Held on the source: the defect would be an ordering
    one."""
    import inspect

    from custom_components.be_electricity_prices import backfill, snapshot_resolve
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    body = inspect.getsource(BePricesCoordinator._update_body)
    assert body.index("ensure_tariff_sheet(") < body.index("_maybe_refresh_snapshot()")
    # An analog meter's area sheet likewise.
    assert body.index("ensure_analog_surcharge(") < body.index(
        "_maybe_refresh_snapshot()"
    )
    resolver = inspect.getsource(snapshot_resolve._resolve_snapshot)
    assert "cached_network_ceiling(month.year)" in resolver
    # A backfill asks for every year its window prices, not just this one.
    context = inspect.getsource(backfill._build_context)
    assert "ensure_tariff_sheet(coordinator._session, year)" in context
    assert "coordinator._session, year, str(entry.data[CONF_DSO])" in context


async def test_a_restart_puts_the_ceiling_on_the_stored_card(hass: Any) -> None:
    """The cache is a module global, empty after every restart, and the stored
    card is resolved before the first refresh reads the sheet. Unless the
    ceiling is stamped, the tick that reads it keeps the card as it booted,
    on the ceiling the card prints, until the volume next moves."""
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.const import (
        DOMAIN,
        DSO_FLUVIUS_ANTWERPEN,
    )
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        TaxOverlay,
    )
    from tests import make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "mega",
            "contract": "mega_fix",
            "region": "flanders",
            "dso": DSO_FLUVIUS_ANTWERPEN,
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    card = make_snapshot(
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0),
        dsos={
            DSO_FLUVIUS_ANTWERPEN: DsoOverlay(
                distribution_single=0.0535,
                transport=0.0,
                capacity_eur_per_kw_year=52.37,
                network_ceiling_eur_per_kwh=0.2035480,
            )
        },
    )
    coord._set_snapshot(card)
    booted = coord._snapshot
    assert booted is not None
    coord._reresolve_snapshot()
    assert coord._snapshot is booted

    fluvius._cache[dt_util.now().year] = fluvius.SheetTerms(0.3276168, 2.5)
    coord._reresolve_snapshot()
    assert coord._snapshot is not booted
    overlay = coord._snapshot.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert overlay.network_ceiling_eur_per_kwh == pytest.approx(0.3276168 * 1.06)


def test_the_ceiling_is_the_years_or_the_last_sheet_before_it() -> None:
    """The rule the floor and the analog rate follow: a year Fluvius has not
    published yet, or whose sheet no longer reads, stands on the latest
    earlier sheet held, never on a later one. Nothing before any is read."""
    assert fluvius.cached_network_ceiling(2027) is None
    fluvius._cache[2023] = fluvius.SheetTerms(0.1920264, 2.5)
    fluvius._cache[2025] = fluvius.SheetTerms(0.3276168, 2.5)
    assert fluvius.cached_network_ceiling(2027) == 0.3276168
    assert fluvius.cached_network_ceiling(2025) == 0.3276168
    assert fluvius.cached_network_ceiling(2024) == 0.1920264
    assert fluvius.cached_network_ceiling(2022) is None


def test_a_card_printing_no_ceiling_stays_capped_into_the_next_year() -> None:
    """On 1 January the resolver asks for the new year's ceiling, which Fluvius
    lists only once it publishes that sheet. Answered for the year alone, a
    card printing no ceiling, as twelve suppliers' Flemish cards do, billed a
    low-volume connection on a high peak its capacity uncapped until then."""
    from datetime import date

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.const import (
        DOMAIN,
        DSO_FLUVIUS_ANTWERPEN,
    )
    from custom_components.be_electricity_prices.fees import _compute_capacity
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        TaxOverlay,
    )
    from custom_components.be_electricity_prices.snapshot_resolve import (
        _resolve_snapshot,
    )
    from tests import FLUVIUS_CEILINGS, make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "engie",
            "contract": "engie_easy_fixed",
            "region": "flanders",
            "dso": DSO_FLUVIUS_ANTWERPEN,
            "meter": "mono",
            "annual_consumption_kwh": 600.0,
        },
    )
    card = make_snapshot(
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0, vat_rate=0.06),
        dsos={
            DSO_FLUVIUS_ANTWERPEN: DsoOverlay(
                distribution_single=0.0535,
                transport=0.0,
                capacity_eur_per_kw_year=52.37,
            )
        },
    )
    fluvius._cache.update(
        {
            year: fluvius.SheetTerms(ceiling, 2.5)
            for year, ceiling in FLUVIUS_CEILINGS.items()
        }
    )

    def billed(month: date) -> tuple[float | None, float]:
        resolved = _resolve_snapshot(
            entry, card, annual_kwh=600.0, delivery_month=month
        )
        overlay = resolved.dsos[DSO_FLUVIUS_ANTWERPEN]
        return (
            overlay.network_ceiling_eur_per_kwh,
            12.0 * _compute_capacity(resolved, entry, 8.0),
        )

    ceiling, december = billed(date(2026, 12, 1))
    assert ceiling is not None
    # The cap binds: eight kW of capacity is far more than the ceiling allows
    # 600 kWh, and the 2,5 kW minimum is what it cannot cut below.
    assert december < 0.9 * 8.0 * 52.37
    january = billed(date(2027, 1, 1))
    assert january == (pytest.approx(ceiling), pytest.approx(december))


def test_the_store_keeps_the_figures_beneath_what_is_read() -> None:
    """The figures are module globals and the tick reads them again only while
    fluvius.be answers. What an entry's store kept stands in after a restart
    until a sheet is read again, which then answers its own figure."""
    from custom_components.be_electricity_prices.const import DSO_FLUVIUS_ANTWERPEN

    fluvius._cache[2026] = fluvius.SheetTerms(0.3276168, 3.0)
    fluvius._analog[(2026, DSO_FLUVIUS_ANTWERPEN)] = 0.0257935
    stored = fluvius.held_table()
    fluvius._cache.clear()
    fluvius._analog.clear()
    fluvius.restore(stored)
    assert fluvius.cached_network_ceiling(2027) == 0.3276168
    assert fluvius.capacity_floor_kw(2026) == 3.0
    assert fluvius.cached_analog_surcharge(2026, DSO_FLUVIUS_ANTWERPEN) == 0.0257935
    # A sheet read again answers its own figure, and is what is kept next.
    fluvius._cache[2026] = fluvius.SheetTerms(0.34, 2.5)
    assert fluvius.cached_network_ceiling(2026) == 0.34
    assert fluvius.held_table()["sheets"]["2026"] == {"ceiling": 0.34, "floor_kw": 2.5}
    # A second entry's copy does not replace the one restored first.
    fluvius._cache.clear()
    fluvius.restore({"sheets": {"2026": {"ceiling": 0.2, "floor_kw": 2.5}}})
    assert fluvius.cached_network_ceiling(2026) == 0.3276168
    # A copy that does not read as the sheet's figures is skipped.
    fluvius._stored.clear()
    fluvius._stored_analog.clear()
    fluvius.restore(
        {
            "sheets": {"2026": {"ceiling": 3.3, "floor_kw": 2.5}, "x": {}},
            "analog": {"2026": {DSO_FLUVIUS_ANTWERPEN: 2.0, "ores": 0.02}},
        }
    )
    fluvius.restore({"sheets": [0.3], "analog": "nonsense"})
    assert fluvius.held_table() == {"sheets": {}, "analog": {}}


async def test_a_restart_without_fluvius_bills_the_sheet_last_read(
    hass: Any,
) -> None:
    """After a restart the tick reads the sheet again before the card, and
    while fluvius.be does not answer it asks again only after six hours. The
    stored card was resolved meanwhile with no ceiling at all, so the entry's
    store keeps what was read, for the card to be resolved on it."""
    from unittest.mock import AsyncMock, patch

    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.const import (
        DOMAIN,
        DSO_FLUVIUS_ANTWERPEN,
    )
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        TaxOverlay,
    )
    from tests import make_snapshot

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "engie",
            "contract": "engie_easy_fixed",
            "region": "flanders",
            "dso": DSO_FLUVIUS_ANTWERPEN,
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    fluvius._cache[dt_util.now().year] = fluvius.SheetTerms(0.3276168, 2.5)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(
        make_snapshot(
            supplier="engie",
            contract="engie_easy_fixed",
            taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0),
            dsos={
                DSO_FLUVIUS_ANTWERPEN: DsoOverlay(
                    distribution_single=0.0535,
                    transport=0.0,
                    capacity_eur_per_kw_year=52.37,
                )
            },
        )
    )
    coord._snapshot_fetched_at = dt_util.utcnow()
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert coord._snapshot is not None
    read = coord._snapshot.dsos[DSO_FLUVIUS_ANTWERPEN].network_ceiling_eur_per_kwh
    assert read is not None

    fluvius._cache.clear()
    restarted = BePricesCoordinator(hass, entry)
    restarted._store.async_load = AsyncMock(return_value=saved[-1])  # type: ignore[method-assign]
    await restarted.async_load_persistent()
    assert restarted._snapshot is not None
    overlay = restarted._snapshot.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert overlay.network_ceiling_eur_per_kwh == pytest.approx(read)
    # The stamp is the figure the card was resolved on, so a sheet read since
    # that says otherwise resolves it again.
    fluvius._cache[dt_util.now().year] = fluvius.SheetTerms(0.4, 2.5)
    restarted._reresolve_snapshot()
    overlay = restarted._snapshot.dsos[DSO_FLUVIUS_ANTWERPEN]
    assert overlay.network_ceiling_eur_per_kwh == pytest.approx(read * 0.4 / 0.3276168)


def test_the_floor_is_the_years_or_the_last_sheet_before_it() -> None:
    """Never a later year's: the rule is billed for the months it was set for.
    Before any sheet is read, the figure every sheet from 2023 to 2026 gives."""
    from custom_components.be_electricity_prices.const import VREG_CAPACITY_FLOOR_KW

    assert fluvius.capacity_floor_kw(2026) == VREG_CAPACITY_FLOOR_KW
    fluvius._cache[2025] = fluvius.SheetTerms(0.3276168, 2.5)
    fluvius._cache[2026] = fluvius.SheetTerms(0.3276168, 3.0)
    assert fluvius.capacity_floor_kw(2026) == 3.0
    assert fluvius.capacity_floor_kw(2027) == 3.0
    assert fluvius.capacity_floor_kw(2025) == 2.5
    assert fluvius.capacity_floor_kw(2024) == VREG_CAPACITY_FLOOR_KW
    # A sheet whose minimum could not be read does not hide the one before.
    fluvius._cache[2027] = fluvius.SheetTerms(0.3276168, None)
    assert fluvius.capacity_floor_kw(2027) == 3.0
