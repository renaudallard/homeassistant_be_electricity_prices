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

"""The card finder, on the moves of November 2026.

Each case is a card that moved that month, with the real page that listed
its new address and the real card found there: did the search find it?
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from tests import FIXTURES

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import card_finder as cf  # type: ignore[import-not-found]
import live_check as lc  # type: ignore[import-not-found]

NOVEMBER = date(2026, 11, 1)
PAGES = FIXTURES / "card_finder"


@pytest.fixture
def modules() -> dict[str, ModuleType]:
    loaded: dict[str, ModuleType] = lc._load_providers()
    return loaded


def _serve(
    monkeypatch: pytest.MonkeyPatch,
    modules: dict[str, ModuleType],
    pages: dict[str, str],
    cards: dict[str, bytes],
) -> list[str]:
    """Answer each page and card from the fixtures, by the part of its
    address given, and anything else as the 404 it would be. Returns the
    cards asked for."""
    pdf = cf._pdf()
    asked: list[str] = []

    def _match(table: dict[str, Any], url: str) -> Any:
        for part, value in table.items():
            if part in url:
                return value
        raise pdf.ExtractorError(f"HTTP 404 fetching {url}")

    async def fetch_text(_session: Any, url: str, **_kw: Any) -> str:
        return str(_match(pages, url))

    async def fetch_pdf_text(_session: Any, url: str, **_kw: Any) -> str:
        asked.append(url)
        payload = _match(cards, url)
        if not pdf._is_pdf_payload(payload):
            raise pdf.ExtractorError(
                f"expected a PDF at {url}, payload starts with {payload[:80]!r}"
            )
        return str(pdf.extract_pdf_text(pdf._strip_pdf_prefix(payload)))

    monkeypatch.setattr(pdf, "fetch_text", fetch_text)
    # The candidates read their pages through their own import of it.
    monkeypatch.setattr(cf._moved(), "fetch_text", fetch_text)
    monkeypatch.setattr(pdf, "fetch_pdf_text", fetch_pdf_text)
    # Luminus reads its product list through its own import of the helper.
    monkeypatch.setattr(modules["luminus"], "fetch_text", fetch_text)
    return asked


async def test_elegant_is_found_under_the_name_its_listing_gives(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """Elegant renamed its files for November 2026, "FlexElectricity" to
    "Flex Electricity", and the extractor kept asking the old name. The
    listing linked the new one, and the ComfortFlex card beside it is
    another product."""
    flex = "/Current/Flex%20Electricity_Residential.pdf"
    _serve(
        monkeypatch,
        modules,
        {
            "elegant.be/tariefkaarten": (
                PAGES / "elegant_tariefkaarten.html"
            ).read_text()
        },
        {flex: (FIXTURES / "elegant_flex_2026-11.pdf").read_bytes()},
    )
    found = await cf.find(
        None, modules, "elegant", "elegant_flex", "flanders", NOVEMBER
    )
    assert found == [
        (
            f"https://cdn.elegant.be/Pricing/TariffCharts{flex}",
            "Elegant's listing",
            None,
        )
    ]


async def test_engie_is_found_under_the_code_its_prices_page_gives(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """Engie gave Empty House a three-year term in November 2026 and a new
    document code with it, "_36_" for "_00_", which its prices page named
    and its sitemap did not. The registry is put back as it was before the
    fix, asking "_00_", since the search must not lean on the code it is
    there to find. Only the French page is served, so the Dutch one fails,
    as a page may, and the search goes on without it."""
    engie = modules["engie"]
    before = replace(
        engie._CONTRACTS_BY_ID["engie_empty_house"],
        months_per_region={"V": "00", "W": "00", "B": "00"},
        renumbered=None,
    )
    monkeypatch.setitem(engie._CONTRACTS_BY_ID, "engie_empty_house", before)
    code = "E_EMPTYHOUSE_R_GREY_C_I_36_W_F"
    _serve(
        monkeypatch,
        modules,
        {"/fr/energie/": (PAGES / "engie_prix_conditions.html").read_text()},
        {code: (FIXTURES / "engie_empty_house_w_2026-11.pdf").read_bytes()},
    )
    found = await cf.find(
        None, modules, "engie", "engie_empty_house", "wallonia", NOVEMBER
    )
    assert [
        (url.split("document=")[1].split("&")[0], reason) for url, _, reason in found
    ] == [(code, None)]


async def test_energyvision_is_found_in_its_upload_folder_when_the_page_link_is_dead(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """EnergyVision deleted the inline copy of its November 2026 tiered card
    on 3 November, while its tariefkaart page still linked it, and the
    re-upload sat in the month's upload folder."""
    name = "EV-1126-GS1800V-nl.pdf"
    _serve(
        monkeypatch,
        modules,
        {
            "/nl-be/tariefkaart-2": (
                PAGES / "energyvision_tariefkaart_2.html"
            ).read_text()
        },
        {
            f"/2026-11/{name}": (
                FIXTURES / "energyvision_tiered_1800_nov.pdf"
            ).read_bytes()
        },
    )
    found = await cf.find(
        None, modules, "energyvision", "energyvision_tiered_1800", "flanders", NOVEMBER
    )
    by_url = {url: (where, reason) for url, where, reason in found}
    files = "https://www.energyvision.be/sites/default/files"
    assert by_url[f"{files}/inline-files/{name}"][0] == "its card page"
    assert "404" in str(by_url[f"{files}/inline-files/{name}"][1])
    assert by_url[f"{files}/2026-11/{name}"] == ("its upload folders", None)


async def test_energyvision_looks_for_a_card_by_its_own_file_name(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """Goedkope stroom's cards are EV-GS-<MMYY>-01, the code first and a
    version after, so the search matches a page's link and asks
    inline-files under that name, not the EV-<MMYY>-<code>-<lang> of the
    products of 2026."""
    name = "EV-GS-0925-01"
    files = "https://www.energyvision.be/sites/default/files"
    _serve(
        monkeypatch,
        modules,
        {
            "/nl-be/tariefkaart-2": (
                f'<a href="/sites/default/files/inline-files/{name}_1.pdf">GS</a>'
            )
        },
        {
            f"/inline-files/{name}": (
                FIXTURES / "energyvision_goedkope_stroom_sep2025.pdf"
            ).read_bytes()
        },
    )
    found = await cf.find(
        None,
        modules,
        "energyvision",
        "energyvision_goedkope_stroom",
        "flanders",
        date(2025, 9, 1),
    )
    by_url = {url: (where, reason) for url, where, reason in found}
    assert by_url[f"{files}/inline-files/{name}_1.pdf"] == ("its card page", None)
    assert by_url[f"{files}/inline-files/{name}.pdf"] == ("its upload folders", None)


async def test_luminus_names_the_product_id_each_card_came_from(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """On 1 November 2026 Luminus listed BasicFlex under a new product id
    that had no card yet, "Pdf not available.", and its October id served a
    card titled November, which turned out to carry October's prices (#147).
    The search reports that card, and says it came by the month before's id:
    that it names the month does not make it the month's card, which is why
    the search only reports."""
    _serve(
        monkeypatch,
        modules,
        {
            "signing=2026-11": (PAGES / "luminus_products_2026-11.json").read_text(),
            "signing=2026-10": (PAGES / "luminus_products_2026-10.json").read_text(),
        },
        {
            "productId=a1paa000002QbZ3AAK": b'"Pdf not available."',
            "productId=a1p0800000CEbf3AAD": (
                PAGES / "luminus_basicflex_old_id_2026-11.pdf"
            ).read_bytes(),
        },
    )
    found = await cf.find(
        None, modules, "luminus", "luminus_basicflex", "flanders", NOVEMBER
    )
    assert [(where, reason is None) for _url, where, reason in found] == [
        ("the product id of 2026-11", False),
        ("the product id of 2026-10, not the product's own card", True),
    ]
    assert "expected a PDF" in str(found[0][2])


async def test_only_the_failing_rows_of_a_covered_card_are_annotated(
    monkeypatch: pytest.MonkeyPatch, modules: dict[str, ModuleType]
) -> None:
    """A row that passed, a row expected to fail, a supplier the search does
    not cover and a check that says nothing of the card's address are left
    as they were, and no row changes between failing and passing."""
    asked: list[tuple[str, str, str]] = []

    async def describe(
        _session: Any,
        _modules: Any,
        supplier: str,
        contract: str,
        region: str,
        month: date,
    ) -> str:
        asked.append((supplier, contract, region))
        assert month == NOVEMBER
        return "card finder: found"

    monkeypatch.setattr(cf, "describe", describe)
    checks = [
        lc.Check("elegant/elegant_flex/flanders: fetch", False, "HTTP 404"),
        lc.Check("elegant/elegant_flex/flanders: card is recent enough", False, ""),
        lc.Check("elegant/elegant_comfortflex/flanders: fetch", True, ""),
        lc.Check(
            "engie/engie_empty_house/wallonia: fetch",
            False,
            "SupplierWithdrawn: gone",
            expected=True,
        ),
        lc.Check("eneco/power_flex/flanders: fetch", False, "HTTP 404"),
        lc.Check("elegant/elegant_flex/flanders: DSO rows", False, "missing"),
    ]
    await cf.annotate(checks, None, modules, date(2026, 11, 6))
    assert asked == [("elegant", "elegant_flex", "flanders")]
    assert [c.detail for c in checks] == [
        "HTTP 404; card finder: found",
        "card finder: found",
        "",
        "SupplierWithdrawn: gone",
        "HTTP 404",
        "missing",
    ]
    assert [c.ok for c in checks] == [False, False, True, False, False, False]


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("Tarifs de novembre 2026", True),
        ("Tariefkaart November 2026", True),
        ("geldig 11/2026", True),
        ("octobre 2026", False),
        ("novembre 2025", False),
    ],
)
def test_the_month_is_read_as_cards_print_it(
    modules: dict[str, ModuleType], text: str, named: bool
) -> None:
    assert (cf._month_re(NOVEMBER).search(text) is not None) is named
