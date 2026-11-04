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

"""A card that left its usual address, read where its supplier lists it.

Each case is a move of November 2026, replayed on the registry as it stood
before the fix, the real page that listed the new address and the real card
there."""

from __future__ import annotations

import inspect
from dataclasses import replace
from typing import Any

import pytest

from custom_components.be_electricity_prices.const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_electricity_prices.providers import (
    EXTRACTORS,
    _moved,
    elegant,
    eneco,
    energyvision,
    engie,
    luminus,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from tests import FIXTURES, fixture_text, make_snapshot

PAGES = FIXTURES / "card_finder"


def _pages(monkeypatch: pytest.MonkeyPatch, pages: dict[str, str]) -> list[str]:
    """Serve the supplier pages the candidates read, by the part of their
    address given; anything else is the 404 it would be. Returns what was
    asked."""
    asked: list[str] = []

    async def fetch_text(_session: Any, url: str, **_kw: Any) -> str:
        asked.append(url)
        for part, page in pages.items():
            if part in url:
                return page
        raise ExtractorError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(_moved, "fetch_text", fetch_text)
    return asked


def _cards(
    monkeypatch: pytest.MonkeyPatch, module: Any, reader: str, cards: dict[str, str]
) -> list[str]:
    """Replace ``module``'s ``reader`` with one serving the texts given, by
    the part of the address; anything else is a 404. Returns what was
    asked."""
    asked: list[str] = []

    async def read(_session: Any, url: str, **_kw: Any) -> str:
        asked.append(url)
        for part, text in cards.items():
            if part in url:
                return text
        raise ExtractorError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(module, reader, read)
    return asked


async def test_elegant_is_read_under_the_name_its_listing_gives(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """On 2 November 2026 Elegant's card answered 404 under the name the
    registry asked, "FlexElectricity", and its listing linked it as "Flex
    Electricity". The registry is put back as it was before the fix."""
    freezer.move_to("2026-11-03 12:00:00+01:00")
    contract = elegant._CONTRACTS_BY_ID["elegant_flex"]
    monkeypatch.setitem(
        elegant._CONTRACTS_BY_ID,
        "elegant_flex",
        replace(contract, file="FlexElectricity"),
    )
    _pages(
        monkeypatch,
        {
            "elegant.be/tariefkaarten": (
                PAGES / "elegant_tariefkaarten.html"
            ).read_text()
        },
    )
    asked = _cards(
        monkeypatch,
        elegant,
        "fetch_pdf_text_layout",
        {
            "/Flex%20Electricity_Residential.pdf": fixture_text(
                "elegant_flex_2026-11.pdf", layout=True
            ),
        },
    )
    snap = await EXTRACTORS["elegant"].fetch(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.publication_label == "november 2026"
    assert snap.source_url.endswith("/Current/Flex%20Electricity_Residential.pdf")
    assert asked[0].endswith("/Current/FlexElectricity_Residential.pdf")


async def test_engie_is_read_under_the_code_its_prices_page_gives(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """Empty House's November 2026 card answered 404 under "_00_" and its
    prices page named "_36_". The registry asks "_00_", as before the fix."""
    freezer.move_to("2026-11-03 12:00:00+01:00")
    contract = engie._CONTRACTS_BY_ID["engie_empty_house"]
    monkeypatch.setitem(
        engie._CONTRACTS_BY_ID,
        "engie_empty_house",
        replace(
            contract,
            months_per_region={"V": "00", "W": "00", "B": "00"},
            renumbered=None,
        ),
    )
    _pages(
        monkeypatch,
        {"/fr/energie/": (PAGES / "engie_prix_conditions.html").read_text()},
    )
    _cards(
        monkeypatch,
        engie,
        "fetch_pdf_text",
        {
            "E_EMPTYHOUSE_R_GREY_C_I_36_W_F&": fixture_text(
                "engie_empty_house_w_2026-11.pdf"
            ),
        },
    )
    snap = await EXTRACTORS["engie"].fetch(None, "engie_empty_house", REGION_WALLONIA)  # type: ignore[arg-type]
    assert snap.publication_label == "Novembre 2026"
    assert "E_EMPTYHOUSE_R_GREY_C_I_36_W_F" in snap.source_url


async def test_a_previous_months_product_id_is_never_read(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """On 1 November 2026 BasicFlex's address answered 404, its November
    product id had no card yet and its October id served an early copy
    titled November with October's prices (#147). That copy is not read: the
    404 stands, as it did."""
    freezer.move_to("2026-11-01 12:00:00+01:00")

    async def fetch_text(_session: Any, url: str, **_kw: Any) -> str:
        month = "2026-11" if "signing=2026-11" in url else "2026-10"
        return (PAGES / f"luminus_products_{month}.json").read_text()

    monkeypatch.setattr(luminus, "fetch_text", fetch_text)
    asked: list[str] = []

    async def fetch_pdf_text(_session: Any, url: str, **_kw: Any) -> str:
        asked.append(url)
        if "productId=a1paa000002QbZ3AAK" in url:
            raise ExtractorError(
                f"expected a PDF at {url}, payload starts with b'\"Pdf not available.\"'"
            )
        raise ExtractorError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(luminus, "fetch_pdf_text", fetch_pdf_text)
    with pytest.raises(ExtractorError, match="HTTP 404"):
        await EXTRACTORS["luminus"].fetch(None, "luminus_basicflex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert not [url for url in asked if "a1p0800000CEbf3AAD" in url]
    assert [url for url in asked if "a1paa000002QbZ3AAK" in url]


async def test_a_card_older_than_the_month_is_looked_for(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """A usual address that still serves the month before's card is no
    error, yet it sends the search to the supplier's pages as a 404 does,
    here Elegant's listing and the name it gave November's card."""
    freezer.move_to("2026-11-03 12:00:00+01:00")

    async def fetch(_session: Any, contract_id: str, region: str) -> SupplierSnapshot:
        return make_snapshot(
            supplier="elegant", contract=contract_id, publication_label="oktober 2026"
        )

    _pages(
        monkeypatch,
        {
            "elegant.be/tariefkaarten": (
                PAGES / "elegant_tariefkaarten.html"
            ).read_text()
        },
    )
    _cards(
        monkeypatch,
        elegant,
        "fetch_pdf_text_layout",
        {
            "/Flex%20Electricity_Residential.pdf": fixture_text(
                "elegant_flex_2026-11.pdf", layout=True
            ),
        },
    )
    fetch_or_moved = _moved._with_moved_fallback("elegant", fetch)
    snap = await fetch_or_moved(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.publication_label == "november 2026"
    assert snap.source_url.endswith("/Current/Flex%20Electricity_Residential.pdf")


async def test_a_card_of_another_month_at_the_new_address_is_not_taken(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """The listing's card is read only when it names the running month: an
    October card there leaves the 404 standing."""
    freezer.move_to("2026-11-03 12:00:00+01:00")
    contract = elegant._CONTRACTS_BY_ID["elegant_flex"]
    monkeypatch.setitem(
        elegant._CONTRACTS_BY_ID,
        "elegant_flex",
        replace(contract, file="FlexElectricity"),
    )
    _pages(
        monkeypatch,
        {
            "elegant.be/tariefkaarten": (
                PAGES / "elegant_tariefkaarten.html"
            ).read_text()
        },
    )
    _cards(
        monkeypatch,
        elegant,
        "fetch_pdf_text_layout",
        {
            "/Flex%20Electricity_Residential.pdf": fixture_text(
                "elegant_flex_2026-10.pdf", layout=True
            ),
        },
    )
    with pytest.raises(ExtractorError, match="HTTP 404"):
        await EXTRACTORS["elegant"].fetch(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "error",
    ["could not parse Elegant energy formula", "HTTP 503 fetching x", "timeout"],
)
async def test_a_card_that_fails_otherwise_is_not_searched_for(
    monkeypatch: pytest.MonkeyPatch, freezer: Any, error: str
) -> None:
    """A layout the parser does not know or a network failure says nothing
    of the card's address: the error stands and no page is read."""
    freezer.move_to("2026-11-03 12:00:00+01:00")

    async def fetch(_session: Any, contract_id: str, region: str) -> SupplierSnapshot:
        raise ExtractorError(error)

    asked = _pages(monkeypatch, {})
    fetch_or_moved = _moved._with_moved_fallback("elegant", fetch)
    with pytest.raises(ExtractorError, match=error):
        await fetch_or_moved(None, "elegant_flex", REGION_FLANDERS)  # type: ignore[arg-type]
    assert asked == []


def test_only_the_registry_falls_back() -> None:
    """Home Assistant and the card archive read the registry, and fall back.
    The live check asks each module's own fetch, so it keeps failing at the
    usual address until the extractor reads the card there. The wrapper
    keeps the module's name, which tells the suppliers apart, and gives its
    fetch back."""
    for supplier, module in (
        ("elegant", elegant),
        ("engie", engie),
        ("luminus", luminus),
    ):
        wrapped = EXTRACTORS[supplier].fetch
        assert module.EXTRACTOR.fetch is module.fetch
        assert wrapped is not module.fetch
        assert inspect.unwrap(wrapped) is module.fetch
        assert inspect.getmodule(wrapped) is module
    assert EXTRACTORS["energyvision"].fetch is energyvision.fetch
    assert EXTRACTORS["eneco"].fetch is eneco.fetch
