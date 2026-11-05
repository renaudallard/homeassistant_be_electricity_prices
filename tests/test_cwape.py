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

"""The CWaPE's approved Walloon grids, and the custom entry that takes them."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from custom_components.be_electricity_prices import cwape
from custom_components.be_electricity_prices.const import DSO_ORES, DSO_RESA, DSO_REW
from tests import FakeBody

_FIXTURES = Path(__file__).parent / "fixtures"
# The service's listing of 5 November 2026, trimmed to the records of the
# five operators and the transport grid for 2025 to 2027.
_LISTING = (_FIXTURES / "cwape_documents.json").read_text(encoding="utf-8")
_TRANSPORT_2026 = (_FIXTURES / "cwape_transport_2026.xlsx").read_bytes()

# The CWaPE's own summary of the 2026 tariffs, with 6% VAT: the fixed term in
# EUR a year, then c/kWh single, off-peak, peak, ECO, MEDIUM, PIC and
# exclusive night. Its transport rate is 2,74252 c/kWh for every operator.
_SUMMARY_2026 = {
    "aieg": (19.50, 10.86965, 6.66291, 12.05279, 4.55953, 9.81796, 15.07640, 6.66291),
    "aiesh": (17.93, 13.63965, 8.21312, 15.16586, 5.49986, 12.28302, 19.06618, 8.21312),
    "ores": (14.10, 11.98070, 7.38666, 13.27278, 5.08965, 10.83219, 16.57474, 7.38666),
    "resa": (26.50, 11.06315, 7.01273, 12.20232, 4.98753, 10.05054, 15.11356, 7.01273),
    "rew": (26.44, 12.47514, 7.83587, 13.77995, 5.51623, 11.31533, 17.11443, 7.83587),
}
# The prosumer rate the cards printed for January 2026, with VAT, EUR/kVA.
_PROSUMER_2026 = {
    "aieg": 81.03,
    "aiesh": 99.29,
    "ores": 85.84,
    "resa": 84.22,
    "rew": 93.00,
}
_GRID_2026 = {
    "aieg": "cwape_aieg_2026.xlsx",
    "aiesh": "cwape_aiesh_2026.xlsx",
    "ores": "cwape_ores_2026.xlsx",
    "resa": "cwape_resa_2026.xlsx",
    "rew": "cwape_rew_2026.pdf",
}


def _grid(dso: str, year: int = 2026) -> cwape.GridTerms:
    name = _GRID_2026[dso] if year == 2026 else f"cwape_{dso}_{year}.xlsx"
    terms = cwape.parse_grid((_FIXTURES / name).read_bytes(), pdf=name.endswith(".pdf"))
    assert terms is not None
    return terms


@pytest.fixture(autouse=True)
def _forget() -> Iterator[None]:
    """The module holds what it read for the life of the process."""

    def clear() -> None:
        cwape._grids.clear()
        cwape._transport.clear()
        cwape._failed_at.clear()
        cwape._locks.clear()
        cwape._listing = None

    clear()
    yield
    clear()


@pytest.mark.parametrize("dso", sorted(_SUMMARY_2026))
def test_each_operator_s_2026_grid_is_the_cwape_s_summary(dso: str) -> None:
    """With 6% VAT, every rate per kWh of the five operators' grids is the
    summary's to the rounding it prints at, and the fixed term to the cent:
    AIEG's and AIESH's fixed terms are 19,49 and 17,92 on the grid and on all
    but one of the cards, a cent under the summary. REW's grid is read off its
    PDF, its workbook being the old binary format."""
    terms = _grid(dso)
    assert (terms.valid_from, terms.valid_to) == (date(2026, 1, 1), date(2026, 12, 31))
    fixed, *rates = _SUMMARY_2026[dso]
    read = (
        terms.single,
        terms.offpeak,
        terms.peak,
        terms.eco,
        terms.medium,
        terms.pic,
        terms.exclusive_night,
    )
    for got, want in zip(read, rates, strict=True):
        assert abs(got * 1.06 * 100 - want) < 2e-5, (dso, got, want)
    assert abs(terms.fixed_per_year * 1.06 - fixed) <= 0.011
    assert round(terms.prosumer_per_kva_year * 1.06, 2) == _PROSUMER_2026[dso]


def test_a_rate_set_by_municipality_bills_nothing() -> None:
    """AIEG's grid prints its other local taxes as "V", variable by
    municipality, and the summary's single rate is what the other charges add
    up to without them: 8,43334 + 0,64001 + 0,35565 + 0,38043 + 0,44496
    c/kWh."""
    terms = _grid("aieg")
    assert terms.single == pytest.approx(
        0.0843334 + 0.0064001 + 0.0035565 + 0.0038043 + 0.0044496, abs=1e-7
    )


def test_the_transport_grid_is_the_summary_s() -> None:
    """The "tous les raccordements" rate and the three rates below it: 2,74252
    c/kWh with VAT, the summary's for every operator."""
    terms = cwape.parse_transport(_TRANSPORT_2026)
    assert terms is not None
    assert terms.rate == pytest.approx(0.0243981 + 0.0007764 + 0.000331 + 0.0003673)
    assert round(terms.rate * 106, 5) == 2.74252
    assert (terms.valid_from, terms.valid_to) == (date(2026, 1, 1), date(2026, 12, 31))


@pytest.mark.parametrize(
    "dropped", ["Redevance de voirie", "Heures ECO", "Tarif prosumer"]
)
def test_a_grid_missing_a_rate_gives_nothing(dropped: str) -> None:
    """A row the grid stops printing refuses it: the entry keeps what it
    typed rather than billing a network short of a charge."""
    from custom_components.be_electricity_prices.providers._pdf_render import (
        extract_pdf_text_layout,
    )

    text = extract_pdf_text_layout((_FIXTURES / "cwape_rew_2026.pdf").read_bytes())
    assert dropped in text
    kept = "\n".join(line for line in text.splitlines() if dropped not in line)
    with patch.object(cwape, "extract_pdf_text_layout", return_value=kept):
        assert cwape.parse_grid(b"", pdf=True) is None
    with patch.object(cwape, "extract_pdf_text_layout", return_value=text):
        assert cwape.parse_grid(b"", pdf=True) is not None


def test_only_the_low_voltage_page_is_read() -> None:
    """The grid's first page is the higher voltages', with the same labels
    and codes: REW's fixed term there is 480,95 EUR a year for a T-BT
    connection, a household's is 24,94."""
    assert _grid("rew").fixed_per_year == pytest.approx(24.94)


def _held(dso: str, *grids: cwape.GridTerms) -> None:
    cwape._grids[dso] = {f"grid-{i}": grid for i, grid in enumerate(grids)}


def test_the_grid_valid_on_the_month_is_billed_and_the_year_before_stands_in() -> None:
    """From 1 January until the year's grid is read the year before's stands
    in; once read, the year's own; never a later year's for an earlier month."""
    old, new = _grid("ores", 2026), _grid("ores", 2027)
    _held(DSO_ORES, old)
    assert cwape.cached_network(date(2027, 1, 1), DSO_ORES)[0] == old
    _held(DSO_ORES, old, new)
    assert cwape.cached_network(date(2027, 1, 1), DSO_ORES)[0] == new
    assert cwape.cached_network(date(2026, 12, 1), DSO_ORES)[0] == old
    _held(DSO_ORES, new)
    assert cwape.cached_network(date(2026, 12, 1), DSO_ORES) == (None, None)


def test_a_grid_still_valid_wins_over_a_later_one_that_has_ended() -> None:
    """A grid published for a few months inside another's year bills those
    months, and the year's own the months after it."""
    from dataclasses import replace

    year = _grid("ores", 2026)
    march = replace(
        year, valid_from=date(2026, 3, 1), valid_to=date(2026, 3, 31), single=0.2
    )
    _held(DSO_ORES, year, march)
    assert cwape.cached_network(date(2026, 3, 1), DSO_ORES)[0] == march
    assert cwape.cached_network(date(2026, 4, 1), DSO_ORES)[0] == year


def test_a_grid_whose_period_ends_early_still_bills_until_another_begins() -> None:
    """RESA's 2027 workbook says its low-voltage page is valid until 31
    January, its other page and its PDF until 31 December: the grid is billed
    every month as the latest begun, and the year counts as not covered, so
    the listing is asked again for a grid that would take over."""
    old, new = _grid("resa", 2026), _grid("resa", 2027)
    assert new.valid_to == date(2027, 1, 31)
    _held(DSO_RESA, old, new)
    assert cwape.cached_network(date(2027, 6, 1), DSO_RESA)[0] == new
    assert cwape._covered(DSO_RESA, 2027) == (False, False)


def test_the_listing_gives_each_document_its_year_operator_and_address() -> None:
    documents = cwape.parse_listing(_LISTING)
    assert len(documents) == 36, "the 2026 to 2029 record spans years"
    wanted = cwape._wanted(documents, 2026, cwape._OPERATORS[DSO_ORES], False, False)
    assert [doc.url.rsplit("/", 1)[-1] for doc in wanted] == [
        "ORES%20-%20Tarifs%20p%C3%A9riodiques%20de%20pr%C3%A9l%C3%A8vement%202026_0.xlsx",
        "GRD%27s-Tarifs%20de%20refacturation%20du%20transport%202026.xlsx",
    ]
    # REW's workbook is the old binary format: its PDF is read instead.
    rew = cwape._wanted(documents, 2026, cwape._OPERATORS[DSO_REW], False, True)
    assert [doc.url.rsplit("/", 1)[-1] for doc in rew] == [
        "REW%20-%20Tarifs%20p%C3%A9riodiques%20de%20pr%C3%A9l%C3%A8vement%202026.pdf"
    ]
    # An id that came to name another operator's files is not taken for it.
    assert cwape._wanted(documents, 2026, ("453", "RESA"), False, True) == []


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
    """The listing and the documents the tests hold, by address."""

    def __init__(self, listing: str = _LISTING) -> None:
        self.pages: dict[str, bytes] = {cwape._LISTING_URL: listing.encode()}
        for doc in cwape.parse_listing(listing):
            file = doc.url.rsplit("/", 1)[-1]
            if doc.kind == cwape._TRANSPORT_TYPE and file.endswith("2026.xlsx"):
                self.pages[doc.url] = _TRANSPORT_2026
            elif file.startswith("ORES") and file.endswith("2026_0.xlsx"):
                self.pages[doc.url] = (_FIXTURES / "cwape_ores_2026.xlsx").read_bytes()
            elif file.startswith("ORES") and file.endswith("2027_1.xlsx"):
                self.pages[doc.url] = (_FIXTURES / "cwape_ores_2027.xlsx").read_bytes()
        self.asked: list[str] = []

    def get(self, url: str, *_a: object, **_k: object) -> _Response:
        self.asked.append(url)
        body = self.pages.get(url)
        return _Response(b"", 404) if body is None else _Response(body)


async def test_a_year_is_read_once_off_one_listing() -> None:
    session: Any = _Session()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert len(session.asked) == 3, session.asked
    assert session.asked[0] == cwape._LISTING_URL
    grid, transport = cwape.cached_network(date(2026, 11, 1), DSO_ORES)
    assert grid is not None and grid.fixed_per_year == pytest.approx(13.30)
    assert transport is not None and round(transport.rate * 106, 5) == 2.74252
    session.asked.clear()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked == [], "a year held was read again"


async def test_a_year_without_its_transport_grid_reads_the_year_before_s() -> None:
    """The 2027 grids are out, the 2027 transport grid is not: the 2026 one is
    read to stand in, and nothing else of 2026."""
    session: Any = _Session()
    await cwape.ensure_network(session, 2027, DSO_ORES)
    files = [url.rsplit("/", 1)[-1] for url in session.asked[1:]]
    assert files == [
        "ORES%20-%20Tarifs%20p%C3%A9riodiques%20de%20pr%C3%A9l%C3%A8vement%202027_1.xlsx",
        "GRD%27s-Tarifs%20de%20refacturation%20du%20transport%202026.xlsx",
    ]
    grid, transport = cwape.cached_network(date(2027, 1, 1), DSO_ORES)
    assert grid is not None and grid.valid_from == date(2027, 1, 1)
    assert transport is not None and transport.valid_to == date(2026, 12, 31)


async def test_nothing_is_read_before_2026_or_off_another_operator() -> None:
    session: Any = _Session()
    await cwape.ensure_network(session, 2025, DSO_ORES)
    await cwape.ensure_network(session, 2026, "sibelga")
    assert session.asked == []


async def test_a_document_that_does_not_read_waits_for_the_backoff(
    freezer: Any,
) -> None:
    session: Any = _Session()
    for url in list(session.pages):
        if url != cwape._LISTING_URL:
            session.pages[url] = b"<html>maintenance</html>"
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert cwape.cached_network(date(2026, 11, 1), DSO_ORES) == (None, None)
    session.asked.clear()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked == [], "the backoff did not hold"
    freezer.tick(timedelta(seconds=cwape._FAILURE_RETRY_S + 1))
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked[0] == cwape._LISTING_URL
    assert len(session.asked) == 3


async def test_a_listing_that_does_not_answer_never_raises() -> None:
    session: Any = _Session()
    session.pages[cwape._LISTING_URL] = b"<html>not json</html>"
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert cwape.cached_network(date(2026, 11, 1), DSO_ORES) == (None, None)
    session.asked.clear()
    await cwape.ensure_network(session, 2026, DSO_ORES)
    assert session.asked == [], "a listing that failed was asked again at once"


def test_the_listing_is_parsed_off_the_event_loop() -> None:
    """Close to 2 MB of JSON: the HA family rule for big bodies."""
    import inspect

    source = inspect.getsource(cwape._listed)
    assert "asyncio.to_thread(parse_listing" in source
