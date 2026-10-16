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

"""Elegant extractor tests against its cards of October, September, December
and November 2025."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS, REGION_FLANDERS
from custom_components.be_electricity_prices.providers import EXTRACTORS, elegant
from custom_components.be_electricity_prices.providers._rates import (
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import ExtractorError
from tests import FIXTURES, fixture_text, make_text_session

_FLEX = "elegant_flex"
_COMFORT = "elegant_comfortflex"
_VAST = "elegant_zeker_vast"
_CURRENT = "https://cdn.elegant.be/Pricing/TariffCharts/Current"


def _card(name: str) -> str:
    return fixture_text(name, layout=True)


def test_elegant_sells_three_flemish_contracts() -> None:
    extractor = EXTRACTORS["elegant"]
    assert extractor.label == "Elegant"
    by_id = {c.id: c for c in extractor.contracts}
    assert set(by_id) == {_FLEX, _COMFORT, _VAST}
    assert [by_id[c].label for c in (_FLEX, _COMFORT, _VAST)] == [
        "Flex",
        "ComfortFlex",
        "Zeker & Vast",
    ]
    for contract in by_id.values():
        assert contract.regions == frozenset({REGION_FLANDERS})
        assert contract.spot_indexed_injection
    assert by_id[_FLEX].kind == by_id[_COMFORT].kind == "variable"
    assert by_id[_FLEX].month_indexed_energy and by_id[_COMFORT].month_indexed_energy
    assert by_id[_VAST].kind == "fixed"
    assert not by_id[_VAST].month_indexed_energy


def test_october_flex_card() -> None:
    snap = elegant.parse_snapshot(_FLEX, _card("elegant_flex_2026-10.pdf"))
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    # The monthly prices, printed at September's EPEXDAMREK.
    assert energy.current == pytest.approx(0.1987)
    assert energy.peak == pytest.approx(0.2007)
    assert energy.offpeak == pytest.approx(0.1967)
    assert energy.exclusive_night == pytest.approx(0.1967)
    assert energy.yearly_fixed_fee == pytest.approx(75.0)
    assert energy.yearly_fixed_fee_exclusive_night is None
    assert energy.month_indexed
    assert not energy.rlp_indexed
    # (1,0800 x EPEXDAMREK + 1,850) x 1,06 c€/kWh on an index in c€/kWh.
    assert energy.formula_factor == pytest.approx(1.08 * 1.06)
    assert energy.formula_base == pytest.approx(0.0185 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(1.092 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(1.068 * 1.06)
    assert energy.formula_factor_exclusive_night == pytest.approx(1.068 * 1.06)
    for base in (
        energy.formula_base_peak,
        energy.formula_base_offpeak,
        energy.formula_base_exclusive_night,
    ):
        assert base == pytest.approx(0.0185 * 1.06)
    injection = snap.injection
    assert injection is not None
    # 0,8500 x EPEXDAMSPP - 1,500 c€/kWh, VAT-exempt.
    assert injection.current == pytest.approx(0.0704)
    assert injection.factor == pytest.approx(0.85)
    assert injection.base == pytest.approx(-0.015)
    assert injection.spp_indexed
    assert not injection.month_indexed
    taxes = snap.taxes
    assert taxes.federal_excise == pytest.approx(0.04876)
    assert taxes.federal_excise_bands is None
    assert taxes.energy_contribution == 0.0
    # Green power 1,17 and WKK 0,42 c€/kWh.
    assert taxes.flanders_renewables == pytest.approx(0.0159)
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == pytest.approx(0.06)
    assert taxes.assumed_vat_rate is None
    assert snap.supplier_prosumer_eur_per_kva_year == pytest.approx(48.0)
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    imewo = snap.dsos["fluvius_imewo"]
    assert imewo.distribution_single == pytest.approx(0.0554)
    assert imewo.distribution_exclusive_night == pytest.approx(0.0501)
    assert imewo.capacity_eur_per_kw_year == pytest.approx(57.45)
    assert imewo.data_management_per_year == pytest.approx(18.92)
    assert imewo.prosumer_eur_per_kva_year == pytest.approx(60.12)
    assert imewo.transport == 0.0
    # Printed as "Fluvius Midden-Vl." and "Fluvius Kempen".
    intergem = snap.dsos["fluvius_intergem"]
    assert intergem.distribution_single == pytest.approx(0.0528)
    assert intergem.capacity_eur_per_kw_year == pytest.approx(53.13)
    assert snap.dsos["fluvius_iveka"].distribution_single == pytest.approx(0.0634)
    assert snap.publication_label == "oktober 2026"
    assert snap.valid_until == date(2026, 10, 31)


def test_comfortflex_prints_the_flex_figures() -> None:
    flex = elegant.parse_snapshot(_FLEX, _card("elegant_flex_2026-10.pdf"))
    comfort = elegant.parse_snapshot(_COMFORT, _card("elegant_comfortflex_2026-10.pdf"))
    assert comfort.energy == flex.energy
    assert comfort.injection == flex.injection
    assert comfort.dsos == flex.dsos
    assert comfort.taxes == flex.taxes
    assert comfort.contract == _COMFORT


def test_october_zeker_vast_card() -> None:
    snap = elegant.parse_snapshot(_VAST, _card("elegant_zeker_vast_2026-10.pdf"))
    energy = snap.energy
    assert isinstance(energy, FixedRates)
    assert energy.single == pytest.approx(0.1860)
    assert energy.peak == pytest.approx(0.1898)
    assert energy.offpeak == pytest.approx(0.1823)
    assert energy.exclusive_night == pytest.approx(0.1823)
    assert energy.yearly_fixed_fee == pytest.approx(120.0)
    injection = snap.injection
    assert injection is not None
    assert injection.current == pytest.approx(0.0704)
    assert injection.factor == pytest.approx(0.85)
    assert injection.base == pytest.approx(-0.015)
    assert injection.spp_indexed
    assert not injection.fixed_for_term
    assert snap.valid_until == date(2026, 10, 31)


def test_the_formulas_reproduce_the_rates_the_card_prints() -> None:
    """At September's 15,641 and 10,053 c€/kWh, the values the card names,
    every register comes back to its printed rate to the cent it is printed
    to, which checks the scaling and the VAT together."""
    text = _card("elegant_flex_2026-10.pdf")
    assert "Deze bedroeg 15,641 c€/kWh voor afname en 10,053 c€/kWh" in text
    snap = elegant.parse_snapshot(_FLEX, text)
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    for factor, base, printed in (
        (energy.formula_factor, energy.formula_base, energy.current),
        (energy.formula_factor_peak, energy.formula_base_peak, energy.peak),
        (energy.formula_factor_offpeak, energy.formula_base_offpeak, energy.offpeak),
        (
            energy.formula_factor_exclusive_night,
            energy.formula_base_exclusive_night,
            energy.exclusive_night,
        ),
    ):
        assert factor is not None and base is not None
        assert factor * 0.15641 + base == pytest.approx(printed, abs=5e-5)
    injection = snap.injection
    assert injection is not None
    assert injection.factor is not None and injection.base is not None
    # 7,045 printed as 7,04.
    assert injection.factor * 0.10053 + injection.base == pytest.approx(
        injection.current, abs=6e-5
    )


def test_a_card_from_before_the_august_levies() -> None:
    """December 2025, as the archive serves it: the product carries its month
    ("Flex 12.25"), the excise is a table by volume and the energy
    contribution is still billed."""
    text = _card("elegant_flex_2025-12.pdf")
    assert "Flex 12.25" in text
    snap = elegant.parse_snapshot(_FLEX, text)
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.1254)
    assert energy.formula_factor == pytest.approx(1.1419 * 1.06)
    assert energy.formula_base == pytest.approx(0.01987 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(1.1632 * 1.06)
    injection = snap.injection
    assert injection is not None
    assert injection.current == pytest.approx(0.0471)
    assert injection.base == pytest.approx(-0.02)
    taxes = snap.taxes
    assert taxes.federal_excise == pytest.approx(0.05033)
    bands = taxes.federal_excise_bands
    assert bands is not None
    assert [upper for upper, _rate in bands] == [3000.0, 20000.0, 50000.0]
    assert [rate for _upper, rate in bands] == pytest.approx(
        [0.05033, 0.05033, 0.04819]
    )
    assert taxes.energy_contribution == pytest.approx(0.0020417)
    imewo = snap.dsos["fluvius_imewo"]
    assert imewo.distribution_single == pytest.approx(0.0624)
    assert imewo.data_management_per_year == pytest.approx(18.56)
    assert snap.publication_label == "december 2025"
    assert snap.valid_until == date(2025, 12, 31)


def test_the_cards_of_the_launch_day() -> None:
    """The cards of 8 November 2025, when the current products started, as
    the archive serves them: the product carries the day its offer started
    ("Flex 08/11/2025") and the rates are printed at October's indices,
    7,507 and 6,621 c€/kWh."""
    text = _card("elegant_flex_2025-11.pdf")
    assert "Flex 08/11/2025" in text
    snap = elegant.parse_snapshot(_FLEX, text)
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.1119)
    assert energy.peak == pytest.approx(0.1136)
    assert energy.offpeak == pytest.approx(0.1102)
    assert energy.exclusive_night == pytest.approx(0.1102)
    assert energy.formula_factor == pytest.approx(1.1419 * 1.06)
    assert energy.formula_base == pytest.approx(0.01987 * 1.06)
    assert energy.formula_factor is not None and energy.formula_base is not None
    assert energy.formula_factor * 0.07507 + energy.formula_base == pytest.approx(
        energy.current, abs=5e-5
    )
    injection = snap.injection
    assert injection is not None
    assert injection.current == pytest.approx(0.0363)
    assert injection.factor == pytest.approx(0.85)
    assert injection.base == pytest.approx(-0.02)
    assert snap.publication_label == "november 2025"
    assert snap.valid_until == date(2025, 11, 30)
    text = _card("elegant_zeker_vast_2025-11.pdf")
    assert "Zeker & Vast 08/11/2025" in text
    snap = elegant.parse_snapshot(_VAST, text)
    fixed = snap.energy
    assert isinstance(fixed, FixedRates)
    assert fixed.single == pytest.approx(0.1630)
    assert fixed.peak == pytest.approx(0.1874)
    assert fixed.offpeak == pytest.approx(0.1385)
    assert fixed.exclusive_night == pytest.approx(0.1385)
    assert fixed.yearly_fixed_fee == pytest.approx(120.0)
    injection = snap.injection
    assert injection is not None
    assert injection.current == pytest.approx(0.0363)
    assert injection.base == pytest.approx(-0.02)
    assert snap.publication_label == "november 2025"


def test_a_card_for_another_product_is_refused() -> None:
    """Flex and ComfortFlex print the same figures, so only the heading tells
    the two cards apart, and the professional edition prints the same
    layout."""
    text = _card("elegant_flex_2026-10.pdf")
    with pytest.raises(ExtractorError, match="card is for Flex Particulier"):
        elegant.parse_snapshot(_COMFORT, text)
    assert "Particulier Prijzen" in text
    with pytest.raises(ExtractorError, match="card is for Flex Professioneel"):
        elegant.parse_snapshot(
            _FLEX, text.replace("Particulier Prijzen", "Professioneel Prijzen")
        )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Fluvius Midden-Vl. ", "Fluvius Gent "),
        ("Fluvius West 60,53", "Fluvius West 0,00 60,53"),
    ],
)
def test_a_network_table_that_does_not_read_is_refused(old: str, new: str) -> None:
    """A partial table would be adopted as the card and an entry on the
    missing area would fail every tick until the next good one."""
    text = _card("elegant_flex_2026-10.pdf")
    assert old in text
    with pytest.raises(ExtractorError, match="network row"):
        elegant.parse_snapshot(_FLEX, text.replace(old, new))


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Dal 0,8500 x EPEXDAMSPP", "Dal 0,8000 x EPEXDAMSPP"),
        ("Injectie(2) 7,04 7,04 7,04", "Injectie(2) 7,04 7,24 6,94"),
    ],
)
def test_a_feed_in_credit_per_register_is_refused(old: str, new: str) -> None:
    text = _card("elegant_flex_2026-10.pdf")
    assert old in text
    with pytest.raises(ExtractorError, match="differs per register"):
        elegant.parse_snapshot(_FLEX, text.replace(old, new))


def test_a_missing_register_formula_is_refused() -> None:
    """Each register bills its own pair, so one gone is a re-price of that
    register rather than a card to adopt."""
    text = _card("elegant_flex_2026-10.pdf")
    assert "Piek (1,0920 x EPEXDAMREK" in text
    with pytest.raises(ExtractorError, match="peak consumption formula"):
        elegant.parse_snapshot(
            _FLEX, text.replace("Piek (1,0920 x EPEXDAMREK", "Piek (1,0920 x INDEX")
        )


async def test_fetch_reads_the_product_card(monkeypatch: pytest.MonkeyPatch) -> None:
    render = AsyncMock(return_value=_card("elegant_zeker_vast_2026-10.pdf"))
    monkeypatch.setattr(elegant, "fetch_pdf_text_layout", render)
    snap = await elegant.fetch(make_text_session(""), _VAST, REGION_FLANDERS)
    url = f"{_CURRENT}/ZekerVastElectricity_Residential.pdf"
    assert render.call_args.args[1] == url
    assert snap.source_url == url
    with pytest.raises(ExtractorError, match="not sold in region"):
        await elegant.fetch(make_text_session(""), _VAST, "wallonia")
    with pytest.raises(ExtractorError, match="unknown Elegant contract"):
        await elegant.fetch(make_text_session(""), "elegant_other", REGION_FLANDERS)


async def test_probe_asks_the_product_card(monkeypatch: pytest.MonkeyPatch) -> None:
    freshness = AsyncMock(return_value="Thu, 01 Oct 2026 04:22:19 GMT")
    monkeypatch.setattr(elegant, "head_freshness_key", freshness)
    session = make_text_session("")
    assert await elegant.probe(session, _FLEX, REGION_FLANDERS) == (
        "Thu, 01 Oct 2026 04:22:19 GMT"
    )
    assert freshness.call_args.args[1] == f"{_CURRENT}/FlexElectricity_Residential.pdf"
    assert await elegant.probe(session, _FLEX, "brussels") is None
    assert await elegant.probe(session, "elegant_other", REGION_FLANDERS) is None


async def test_discover_names_the_residential_electricity_cards() -> None:
    """The listing links gas and professional cards beside them, which are
    not products here."""
    html = (FIXTURES / "discover" / "elegant.html").read_text(encoding="utf-8")
    assert await elegant.discover(make_text_session(html)) == {_FLEX, _COMFORT, _VAST}
    new = html + f'<a href="{_CURRENT}/GroenFlexElectricity_Residential.pdf">x</a>'
    assert "GroenFlexElectricity" in await elegant.discover(make_text_session(new))


# ---- the tariff archive ------------------------------------------------------

_KEYS = {
    _FLEX: "FlexElectricity",
    _COMFORT: "ComfortFlexElectricity",
    _VAST: "ZekerVastElectricity",
}
_CHART = "https://www.elegant.be/api/tarief-archief/tariff-chart?tariffChartPath="


def _path(key: str, month: str, day: str = "01") -> str:
    return f"/api/v1/tariff-charts?productOfferKey={key}_{month}{day}&archive=true"


def _offers(month: str, *keys: str, day: str = "01") -> str:
    """An archive search answer offering ``keys`` from ``day`` of ``month``
    ("202609")."""
    offers = [
        {
            "productOfferKey": f"{key}_{month}{day}",
            "name": key,
            "tariffChartUrl": _path(key, month, day),
        }
        for key in keys
    ]
    return json.dumps([{"result": {"data": {"productOffers": offers}}}])


def _archive(
    monkeypatch: pytest.MonkeyPatch, answers: dict[str, str], cards: dict[str, str]
) -> list[str]:
    """Serve ``answers`` (search answer by date) and ``cards`` (card text by
    chart URL) to the archive reader; returns the dates asked for."""
    asked: list[str] = []

    async def search(_session: Any, url: str, *, params: dict[str, str]) -> str:
        assert url == "https://www.elegant.be/api/trpc/tariefArchief.search"
        query = json.loads(params["input"])["0"]
        assert query["energyType"] == "Electricity"
        assert query["customerType"] == "Residential"
        asked.append(query["date"])
        return answers[query["date"]]

    async def render(_session: Any, url: str) -> str:
        return cards[url]

    monkeypatch.setattr(elegant, "fetch_text", search)
    monkeypatch.setattr(elegant, "fetch_pdf_text_layout", render)
    return asked


def _chart(contract_id: str, month: str, day: str = "01") -> str:
    return _CHART + quote(_path(_KEYS[contract_id], month, day), safe="")


async def test_a_closed_month_settles_on_the_indices_the_next_card_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """September's card prints August's indices; October's names
    September's, 15,641 and 10,053 c€/kWh, and every register and the
    credit are rebuilt on them."""
    keys = tuple(_KEYS.values())
    asked = _archive(
        monkeypatch,
        {
            "2026-09-01": _offers("202609", *keys),
            "2026-10-01": _offers("202610", *keys),
        },
        {
            _chart(_FLEX, "202609"): _card("elegant_flex_2026-09.pdf"),
            _chart(_FLEX, "202610"): _card("elegant_flex_2026-10.pdf"),
        },
    )
    snap = await elegant.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 9, 14)
    )
    assert asked == ["2026-09-01", "2026-10-01"]
    assert snap is not None
    assert not snap.provisional
    assert snap.source_url == _chart(_FLEX, "202609")
    assert snap.publication_label == "september 2026"
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.index_realised == pytest.approx(0.15641)
    assert energy.current == pytest.approx(1.1448 * 0.15641 + 0.01961)
    assert energy.peak == pytest.approx(1.092 * 1.06 * 0.15641 + 0.01961)
    assert energy.offpeak == pytest.approx(1.068 * 1.06 * 0.15641 + 0.01961)
    assert energy.exclusive_night == energy.offpeak
    injection = snap.injection
    assert injection is not None
    assert injection.index_realised == pytest.approx(0.10053)
    assert injection.current == pytest.approx(0.85 * 0.10053 - 0.015)


async def test_zeker_vast_settles_its_credit_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its card names the feed-in index alone, and its energy is fixed."""
    _archive(
        monkeypatch,
        {
            "2026-09-01": _offers("202609", _KEYS[_VAST]),
            "2026-10-01": _offers("202610", _KEYS[_VAST]),
        },
        {
            _chart(_VAST, "202609"): _card("elegant_zeker_vast_2026-09.pdf"),
            _chart(_VAST, "202610"): _card("elegant_zeker_vast_2026-10.pdf"),
        },
    )
    snap = await elegant.fetch_for_month(
        make_text_session(""), _VAST, REGION_FLANDERS, date(2026, 9, 1)
    )
    assert snap is not None
    assert not snap.provisional
    energy = snap.energy
    assert isinstance(energy, FixedRates)
    assert energy.single == pytest.approx(0.1688)
    injection = snap.injection
    assert injection is not None
    assert injection.index_realised == pytest.approx(0.10053)
    assert injection.current == pytest.approx(0.85 * 0.10053 - 0.015)


async def test_the_running_month_stays_provisional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asked for a date ahead, the archive offers the card in force today,
    October's, which is no November card: October keeps its printed rates
    and is asked again."""
    answer = _offers("202610", _KEYS[_FLEX])
    _archive(
        monkeypatch,
        {"2026-10-01": answer, "2026-11-01": answer},
        {_chart(_FLEX, "202610"): _card("elegant_flex_2026-10.pdf")},
    )
    session = make_text_session("")
    snap = await elegant.fetch_for_month(
        session, _FLEX, REGION_FLANDERS, date(2026, 10, 1)
    )
    assert snap is not None and snap.provisional
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.1987)
    assert energy.index_realised is None
    assert snap.injection is not None and snap.injection.index_realised is None
    # A card of another month is no card for the month asked.
    assert (
        await elegant.fetch_for_month(
            session, _FLEX, REGION_FLANDERS, date(2026, 11, 1)
        )
        is None
    )


@pytest.mark.parametrize(
    "following",
    [
        # October's card naming August's indices, as a card that skipped a
        # month would.
        lambda: _card("elegant_flex_2026-10.pdf").replace(
            "de waarde van september 2026", "de waarde van augustus 2026"
        ),
        # An October card printing no index sentence at all.
        lambda: _card("elegant_flex_2026-10.pdf").replace("namelijk de waarde", ""),
    ],
    ids=["another month", "no index"],
)
async def test_a_next_card_not_naming_the_month_settles_nothing(
    monkeypatch: pytest.MonkeyPatch, following: Any
) -> None:
    _archive(
        monkeypatch,
        {
            "2026-09-01": _offers("202609", _KEYS[_FLEX]),
            "2026-10-01": _offers("202610", _KEYS[_FLEX]),
        },
        {
            _chart(_FLEX, "202609"): _card("elegant_flex_2026-09.pdf"),
            _chart(_FLEX, "202610"): following(),
        },
    )
    snap = await elegant.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 9, 1)
    )
    assert snap is not None and snap.provisional
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.index_realised is None
    assert energy.current == pytest.approx(0.1677)
    assert snap.injection is not None and snap.injection.index_realised is None


async def test_an_unreadable_archive_answer_is_no_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused rather than raised: the month falls back to the proxy, and a
    month whose next card does not read is asked again."""
    _archive(
        monkeypatch,
        {
            "2026-08-01": "<html>maintenance</html>",
            "2026-09-01": _offers("202609", _KEYS[_FLEX]),
            "2026-10-01": "[]",
        },
        {_chart(_FLEX, "202609"): _card("elegant_flex_2026-09.pdf")},
    )
    session = make_text_session("")
    assert (
        await elegant.fetch_for_month(session, _FLEX, REGION_FLANDERS, date(2026, 8, 1))
        is None
    )
    snap = await elegant.fetch_for_month(
        session, _FLEX, REGION_FLANDERS, date(2026, 9, 1)
    )
    assert snap is not None and snap.provisional


def test_published_indices_read_both_forms() -> None:
    flex = elegant.published_indices(_card("elegant_flex_2026-10.pdf"))
    assert flex is not None
    month, consumption, feed_in = flex
    assert month == date(2026, 9, 1)
    assert consumption == pytest.approx(0.15641)
    assert feed_in == pytest.approx(0.10053)
    vast = elegant.published_indices(_card("elegant_zeker_vast_2026-10.pdf"))
    assert vast is not None
    assert vast[0] == date(2026, 9, 1)
    assert vast[1] is None
    assert vast[2] == pytest.approx(0.10053)


async def test_a_variable_card_naming_one_index_settles_neither_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a Flex card a lone figure could be either index, so neither leg is
    settled on it."""
    following = _card("elegant_flex_2026-10.pdf").replace(
        "15,641 c€/kWh voor afname en 10,053 c€/kWh voor injectie", "10,053 c€/kWh"
    )
    _archive(
        monkeypatch,
        {
            "2026-09-01": _offers("202609", _KEYS[_FLEX]),
            "2026-10-01": _offers("202610", _KEYS[_FLEX]),
        },
        {
            _chart(_FLEX, "202609"): _card("elegant_flex_2026-09.pdf"),
            _chart(_FLEX, "202610"): following,
        },
    )
    snap = await elegant.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 9, 1)
    )
    assert snap is not None and snap.provisional
    assert snap.injection is not None and snap.injection.index_realised is None


async def test_a_month_without_the_product_is_not_archived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Before November 2025 the archive offers the products Elegant sold
    then, under other keys, from the first of the month to its last."""
    old = _offers("202111", "BEGreenFlexStroom", "BEComfortFlexStroom")
    asked = _archive(monkeypatch, {"2025-10-01": old, "2025-10-31": old}, {})
    session = make_text_session("")
    assert (
        await elegant.fetch_for_month(
            session, _FLEX, REGION_FLANDERS, date(2025, 10, 1)
        )
        is None
    )
    assert asked == ["2025-10-01", "2025-10-31"]
    assert (
        await elegant.fetch_for_month(session, _FLEX, "brussels", date(2025, 10, 1))
        is None
    )
    assert asked == ["2025-10-01", "2025-10-31"]


async def test_the_launch_month_reads_the_first_day_with_the_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The current products start on 8 November 2025, when the 1st still
    offers the old ones. A contract signed later that month is billed on
    the card of the 8th, settled on the 8,619 and 7,889 c€/kWh December's
    card names for November."""
    old = _offers("202111", "BEGreenFlexStroom", "BEComfortFlexStroom")
    new = _offers("202511", *_KEYS.values(), day="08")
    answers = {f"2025-11-{day:02d}": old if day < 8 else new for day in range(1, 31)}
    answers["2025-12-01"] = _offers("202512", *_KEYS.values())
    asked = _archive(
        monkeypatch,
        answers,
        {
            _chart(_FLEX, "202511", "08"): _card("elegant_flex_2025-11.pdf"),
            _chart(_FLEX, "202512"): _card("elegant_flex_2025-12.pdf"),
        },
    )
    snap = await elegant.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2025, 11, 20)
    )
    # The 1st, the last day, then halving the days between down to the 8th.
    assert asked == [
        "2025-11-01",
        "2025-11-30",
        "2025-11-15",
        "2025-11-08",
        "2025-11-04",
        "2025-11-06",
        "2025-11-07",
        "2025-12-01",
    ]
    assert snap is not None
    assert not snap.provisional
    assert snap.source_url == _chart(_FLEX, "202511", "08")
    assert snap.publication_label == "november 2025"
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.index_realised == pytest.approx(0.08619)
    assert energy.current == pytest.approx(1.1419 * 1.06 * 0.08619 + 0.01987 * 1.06)
    injection = snap.injection
    assert injection is not None
    assert injection.index_realised == pytest.approx(0.07889)
    assert injection.current == pytest.approx(0.85 * 0.07889 - 0.02)
