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

"""Dots Energy extractor tests against its November 2026 cards."""

from __future__ import annotations

import time
from collections.abc import Callable
from unittest.mock import AsyncMock

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS, REGION_FLANDERS
from custom_components.be_electricity_prices.providers import EXTRACTORS, dots
from custom_components.be_electricity_prices.providers._rates import DynamicRates
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from tests import FIXTURES, fixture_text, make_text_session

_DYNAMIC = "dots_dynamic"
_INSIGHTS = "dots_dynamic_insights"
_FIXTURES = {
    _DYNAMIC: "dots_dynamic_2026-11.pdf",
    _INSIGHTS: "dots_dynamic_insights_2026-11.pdf",
}
# The VNR year prices the cards work their printed prices out at, c€/kWh.
_VNR_OFFTAKE = 18.003
_VNR_FEED_IN = 9.377


def _card(contract: str) -> str:
    return fixture_text(_FIXTURES[contract], aligned=True)


def _snap(contract: str) -> SupplierSnapshot:
    return dots.parse_snapshot(contract, _card(contract), "t://")


def test_dots_sells_two_flemish_dynamic_contracts() -> None:
    extractor = EXTRACTORS["dots"]
    assert extractor.label == "Dots Energy"
    by_id = {c.id: c for c in extractor.contracts}
    assert set(by_id) == {_DYNAMIC, _INSIGHTS}
    assert by_id[_DYNAMIC].label == "Dots Dynamic"
    assert by_id[_INSIGHTS].label == "Dots Dynamic Insights"
    for contract in by_id.values():
        assert contract.kind == "dynamic"
        assert contract.regions == frozenset({REGION_FLANDERS})
        assert not contract.quarter_hourly_option
    # No archive: the months before the running one answer 404.
    assert extractor.fetch_for_month is None
    assert extractor.probe is None


def test_the_dynamic_card() -> None:
    snap = _snap(_DYNAMIC)
    energy = snap.energy
    assert isinstance(energy, DynamicRates)
    # 1 * BELPEX + 1,25 c€/kWh before VAT, on a BELPEX in c€/kWh.
    assert energy.factor == pytest.approx(1.06)
    assert energy.base == pytest.approx(0.0125 * 1.06)
    assert energy.quarter_hourly
    # €5,3 a month and EAN, VAT included.
    assert energy.yearly_fixed_fee == pytest.approx(63.6)
    injection = snap.injection
    assert injection is not None
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-0.0125)
    assert injection.current is None
    assert not (injection.month_indexed or injection.spp_indexed)
    assert injection.formula == "1 x BELPEX - 1,25 c€/kWh"
    assert snap.publication_label == "november 2026"
    assert snap.valid_until is not None and snap.valid_until.isoformat() == "2026-11-30"


def test_the_insights_card_bills_the_balancing_contribution_at_its_bound() -> None:
    """0,005 * BELPEX + 0 on each leg, before VAT, folded in: 1,005 x BELPEX +
    1,5 for the offtake and 0,995 x BELPEX - 1,5 for the feed-in."""
    snap = _snap(_INSIGHTS)
    energy = snap.energy
    assert isinstance(energy, DynamicRates)
    assert energy.factor == pytest.approx(1.005 * 1.06)
    assert energy.base == pytest.approx(0.015 * 1.06)
    assert energy.quarter_hourly
    assert energy.yearly_fixed_fee == pytest.approx(7.95 * 12)
    injection = snap.injection
    assert injection is not None
    assert injection.factor == pytest.approx(0.995)
    assert injection.base == pytest.approx(-0.015)
    assert injection.formula == "0,995 x BELPEX - 1,5 c€/kWh"


@pytest.mark.parametrize(
    ("contract", "offtake", "feed_in"),
    [(_DYNAMIC, 19.253, 8.127), (_INSIGHTS, 19.503, 7.877)],
)
def test_the_formulas_give_back_the_prices_the_cards_print(
    contract: str, offtake: float, feed_in: float
) -> None:
    """Each card works its price out at the VNR year price it names, without
    the balancing contribution, which pins the units: a BELPEX in EUR/kWh
    and a base in EUR/kWh, the offtake grossed by 6%."""
    snap = _snap(contract)
    energy, injection = snap.energy, snap.injection
    assert isinstance(energy, DynamicRates) and injection is not None
    assert injection.factor is not None and injection.base is not None
    balancing = 0.005 if contract == _INSIGHTS else 0.0
    spot = _VNR_OFFTAKE / 100.0
    billed = (energy.factor * spot + energy.base) / 1.06 * 100.0
    assert billed == pytest.approx(offtake + balancing * _VNR_OFFTAKE, abs=5e-4)
    spot = _VNR_FEED_IN / 100.0
    credited = (injection.factor * spot + injection.base) * 100.0
    assert credited == pytest.approx(feed_in - balancing * _VNR_FEED_IN, abs=5e-4)


def test_the_balancing_contribution_follows_belpex_below_zero() -> None:
    """As the formula reads: on a negative price the contribution is negative
    too, so it lowers the offtake price and raises the feed-in credit."""
    plain, insights = _snap(_DYNAMIC), _snap(_INSIGHTS)
    assert isinstance(plain.energy, DynamicRates)
    assert isinstance(insights.energy, DynamicRates)
    assert insights.injection is not None and plain.injection is not None
    spot = -0.10
    margin = (0.015 - 0.0125) * 1.06
    offtake = insights.energy.factor * spot + insights.energy.base
    assert offtake == pytest.approx(
        plain.energy.factor * spot + plain.energy.base + margin - 0.0005 * 1.06
    )
    assert insights.injection.factor is not None
    assert insights.injection.base is not None
    credit = insights.injection.factor * spot + insights.injection.base
    assert credit == pytest.approx(-0.10 - 0.015 + 0.0005)


@pytest.mark.parametrize("contract", [_DYNAMIC, _INSIGHTS])
def test_the_second_page(contract: str) -> None:
    """The network table and the levies, every figure VAT-inclusive."""
    snap = _snap(contract)
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.data_management_per_year == pytest.approx(18.92)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.distribution_exclusive_night == pytest.approx(0.0481)
    assert antwerpen.transport == 0.0
    # The row the side label sits level with.
    imewo = snap.dsos["fluvius_imewo"]
    assert imewo.capacity_eur_per_kw_year == pytest.approx(57.45)
    assert imewo.distribution_exclusive_night == pytest.approx(0.0501)
    west = snap.dsos["fluvius_west"]
    assert west.distribution_single == pytest.approx(0.067)
    assert west.capacity_eur_per_kw_year == pytest.approx(60.53)
    taxes = snap.taxes
    # October's excise and the abolished contribution, which the law's
    # resolvers replace.
    assert taxes.federal_excise == pytest.approx(0.04876)
    assert taxes.federal_excise_bands is None
    assert taxes.energy_contribution == pytest.approx(0.002042)
    # Green power c€1,166 and WKK c€0,4304, VAT included.
    assert taxes.flanders_renewables == pytest.approx(0.015964)
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == pytest.approx(0.06)
    assert taxes.assumed_vat_rate is None


@pytest.mark.parametrize(
    "header",
    ["inclusief 6 % btw", "incl. 6% BTW", "inclusief btw (6%)"],
)
def test_a_reworded_vat_header_is_not_read_off_the_dongle(header: str) -> None:
    """Insights prints its dongle "inclusief 21% BTW". A header the reader no
    longer matches leaves the rate assumed, which the live check reports,
    rather than grossing the formula by 21%."""
    text = _card(_INSIGHTS)
    assert "Alle prijzen zijn inclusief 6% BTW" in text
    assert "inclusief 21% BTW" in text
    snap = dots.parse_snapshot(
        _INSIGHTS,
        text.replace(
            "Alle prijzen zijn inclusief 6% BTW", f"Alle prijzen zijn {header}"
        ),
        "t://",
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.005 * 1.06)
    assert snap.taxes.card_vat_rate is None
    assert snap.taxes.assumed_vat_rate == pytest.approx(0.06)


def test_the_plain_reading_is_refused() -> None:
    """pypdf detaches the price row's figures from each other, so the card is
    read word-aligned."""
    with pytest.raises(ExtractorError, match="price formulas not found"):
        dots.parse_snapshot(_DYNAMIC, fixture_text(_FIXTURES[_DYNAMIC]), "t://")


def test_a_card_for_the_other_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not the Dots Dynamic Insights card"):
        dots.parse_snapshot(_INSIGHTS, _card(_DYNAMIC), "t://")
    with pytest.raises(ExtractorError, match="not the Dots Dynamic card"):
        dots.parse_snapshot(_DYNAMIC, _card(_INSIGHTS), "t://")


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param(
            "afname: in c€/kWh excl. BTW",
            "afname: in €/MWh excl. BTW",
            "afname formula is not in",
            id="offtake unit",
        ),
        pytest.param(
            "afname: in c€/kWh excl. BTW",
            "afname: in c€/kWh incl. BTW",
            "afname formula is not in",
            id="offtake basis",
        ),
        pytest.param(
            "injectie: in c€/kWh",
            "injectie: in €/kWh",
            "injectie formula is not in",
            id="feed-in unit",
        ),
        pytest.param(
            "BELPEX + 1,25 19,253",
            "BELPEX + 12,5 19,253",
            "afname formula does not give the price printed",
            id="offtake base",
        ),
        pytest.param(
            "BELPEX - 1,25 8,127",
            "BELPEX - 1,25 81,27",
            "injectie formula does not give the price printed",
            id="feed-in price",
        ),
        pytest.param(
            "(11/2026: c€9,377/kWh)",
            "(11/2026: €93,77/MWh)",
            "injectie price check not found",
            id="feed-in check",
        ),
        pytest.param("€5,3/maand/EAN", "€63,6/jaar/EAN", "monthly fee", id="fee"),
        pytest.param(
            "Vlaamse Gewest", "Waalse Gewest", "not for Flemish households", id="region"
        ),
    ],
)
def test_a_card_the_reader_does_not_know_is_refused(
    old: str, new: str, error: str
) -> None:
    text = _card(_DYNAMIC)
    assert old in text
    with pytest.raises(ExtractorError, match=error):
        dots.parse_snapshot(_DYNAMIC, text.replace(old, new), "t://")


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        pytest.param(
            "Evenwichtsbijdrage injectie (4) 0,005 * BELPEX + 0",
            "Evenwichtsbijdrage injectie (4) 0,005 * EPEX + 0",
            "balancing contribution formula not found",
            id="formula",
        ),
        pytest.param(
            "wordt weergegeven exclusief BTW",
            "wordt weergegeven inclusief BTW",
            "balancing contribution basis not stated",
            id="basis",
        ),
    ],
)
def test_a_balancing_contribution_the_reader_cannot_bill_is_refused(
    old: str, new: str, error: str
) -> None:
    text = _card(_INSIGHTS)
    assert old in text
    with pytest.raises(ExtractorError, match=error):
        dots.parse_snapshot(_INSIGHTS, text.replace(old, new), "t://")


async def test_fetch_reads_the_product_address_held_to_the_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    render = AsyncMock(return_value=_card(_INSIGHTS))
    monkeypatch.setattr(dots, "fetch_pdf_text_aligned", render)
    snap = await dots.fetch(make_text_session(""), _INSIGHTS, REGION_FLANDERS)
    url = "https://www.dotsenergy.be/api/tariefkaarten/dots-dynamic-insights-elektriciteit"
    assert render.call_args.args[1] == url
    assert render.call_args.kwargs == {
        "x_join_threshold": 1.0,
        "hosts": ("www.dotsenergy.be",),
    }
    assert snap.source_url == url
    with pytest.raises(ExtractorError, match="not sold in region"):
        await dots.fetch(make_text_session(""), _DYNAMIC, "wallonia")
    with pytest.raises(ExtractorError, match="unknown Dots Energy contract"):
        await dots.fetch(make_text_session(""), "dots_other", REGION_FLANDERS)


async def test_a_live_network_table_that_does_not_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A card short of an area would leave an entry on it with no network
    figures; the reader leaves the area out and fetch refuses the card."""
    text = _card(_DYNAMIC).replace("Fluvius West ", "Fluvius Westhoek ")
    assert set(dots.parse_snapshot(_DYNAMIC, text, "t://").dsos) == set(
        FLUVIUS_KEYS
    ) - {"fluvius_west"}
    monkeypatch.setattr(dots, "fetch_pdf_text_aligned", AsyncMock(return_value=text))
    with pytest.raises(
        ExtractorError, match=r"DSO rows not found for \['fluvius_west'\]"
    ):
        await dots.fetch(make_text_session(""), _DYNAMIC, REGION_FLANDERS)


def _home_page() -> str:
    return (FIXTURES / "discover" / "dots.html").read_text(encoding="utf-8")


async def test_discover_names_the_electricity_cards_the_home_page_links() -> None:
    """The gas card is the gas integration's, and SEG - Dynamic Flex links no
    card at all."""
    page = _home_page()
    assert "dots-connect-gas-digital" in page and "SEG - Dynamic Flex" in page
    assert await dots.discover(make_text_session(page)) == {_DYNAMIC, _INSIGHTS}


async def test_discover_surfaces_a_new_card_and_ignores_one_off_the_site() -> None:
    page = (
        _home_page()
        + '<a href="/api/tariefkaarten/dots-dynamic-flex" download="">x</a>'
        + '<a href="https://cards.example/api/tariefkaarten/dots-fake">x</a>'
        + '<a href="/api/tariefkaarten/Dots Odd">x</a>'
    )
    assert await dots.discover(make_text_session(page)) == {
        _DYNAMIC,
        _INSIGHTS,
        "dots_dots_dynamic_flex",
    }


# Card text a host could serve instead, on which a pattern tried from every
# place it could start would cost the square of its length.
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            lambda: dots._FORMULAS_RE.search("Elektriciteit " + "1" * 200_000),
            id="formula digits",
        ),
        pytest.param(
            lambda: dots._FORMULAS_RE.search("\nElektriciteit 1 * BELPEX + 1" * 20_000),
            id="formula rows",
        ),
        pytest.param(
            lambda: dots._TITLE_RE.search("Dots Dynamic" + " " * 200_000 + "x"),
            id="title blanks",
        ),
        pytest.param(
            lambda: dots._BALANCING_EX_VAT_RE.search("evenwichtsbijdrage " * 30_000),
            id="balancing sentence",
        ),
        pytest.param(
            lambda: dots._EXCISE_RE.search(
                "Bijzondere accijns op Energie" + "\n" * 200_000 + "x"
            ),
            id="excise line breaks",
        ),
        pytest.param(
            lambda: dots._LINK_RE.findall('href="' + "x" * 200_000),
            id="unclosed link",
        ),
        pytest.param(
            lambda: dots._FEE_RE.search("€" + " " * 200_000),
            id="fee blanks",
        ),
    ],
)
def test_a_crafted_card_is_read_in_time_linear_in_its_length(
    read: Callable[[], object],
) -> None:
    started = time.perf_counter()
    read()
    assert time.perf_counter() - started < 2.0
