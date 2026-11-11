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

"""Belvus extractor tests against its January to November 2026 cards."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import fluvius
from custom_components.be_electricity_prices.const import (
    DOMAIN,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
)
from custom_components.be_electricity_prices.providers import (
    EXTRACTORS,
    _belvus_older,
    belvus,
)
from custom_components.be_electricity_prices.providers._pdf import NL_MONTHS
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_resolve import _resolve_snapshot
from tests import FIXTURES, fixture_text, make_text_session

_DYNAMISCH = "belvus_dynamisch"
_FLEX = "belvus_flex_online"
_SMART = "belvus_smart_plus"
_CARDS = {
    (_DYNAMISCH, "2026-11"): "belvus_dynamisch_2026-11.pdf",
    (_DYNAMISCH, "2026-09"): "belvus_dynamisch_2026-09.pdf",
    (_FLEX, "2026-11"): "belvus_flex_online_2026-11.pdf",
    (_FLEX, "2026-10"): "belvus_flex_online_2026-10.pdf",
    (_SMART, "2026-11"): "belvus_smart_plus_2026-11.pdf",
    (_FLEX, "2026-09"): "belvus_flex_online_2026-09.pdf",
    # The older layout: January to March, April, and May to August.
    (_SMART, "2026-01"): "belvus_smart_plus_2026-01.pdf",
    (_SMART, "2026-02"): "belvus_smart_plus_2026-02.pdf",
    (_SMART, "2026-04"): "belvus_smart_plus_2026-04.pdf",
    (_FLEX, "2026-08"): "belvus_flex_online_2026-08.pdf",
}
_HOST = "https://www.belvus.be"


def _card(contract: str, month: str = "2026-11") -> str:
    return fixture_text(_CARDS[(contract, month)])


def _snap(contract: str, month: str = "2026-11") -> SupplierSnapshot:
    return belvus.parse_snapshot(contract, _card(contract, month), "t://")


def test_belvus_sells_three_flemish_contracts() -> None:
    extractor = EXTRACTORS["belvus"]
    assert extractor.label == "Belvus"
    by_id = {c.id: c for c in extractor.contracts}
    assert set(by_id) == {_DYNAMISCH, _FLEX, _SMART}
    assert by_id[_DYNAMISCH].label == "Belvus Dynamisch"
    assert by_id[_FLEX].label == "Belvus Flex Online"
    assert by_id[_SMART].label == "Belvus Smart Plus"
    assert by_id[_DYNAMISCH].kind == "dynamic"
    assert not by_id[_DYNAMISCH].month_indexed_energy
    assert not by_id[_DYNAMISCH].spot_indexed_injection
    for contract in (by_id[_FLEX], by_id[_SMART]):
        assert contract.kind == "variable"
        assert contract.month_indexed_energy
        assert contract.spot_indexed_injection
    for contract in by_id.values():
        assert contract.regions == frozenset({REGION_FLANDERS})
        assert not contract.quarter_hourly_option
    # The older cards of January to April 2026 print Fluvius's 2025 table.
    assert by_id[_FLEX].earlier_network_cards
    assert by_id[_SMART].earlier_network_cards
    assert not by_id[_DYNAMISCH].earlier_network_cards
    assert extractor.fetch_for_month is not None
    assert extractor.probe is not None
    assert extractor.settles_on_next_card


def test_the_dynamisch_card() -> None:
    snap = _snap(_DYNAMISCH)
    energy = snap.energy
    assert isinstance(energy, DynamicRates)
    # (Belpex_15M x 1,01) + 7,50 EUR/MWh before VAT, grossed by 6%.
    assert energy.factor == pytest.approx(1.01 * 1.06)
    assert energy.base == pytest.approx(0.0075 * 1.06)
    assert energy.quarter_hourly
    # € 8/jaar, VAT included.
    assert energy.yearly_fixed_fee == pytest.approx(8.0)
    injection = snap.injection
    assert injection is not None
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-0.013)
    assert injection.current is None
    assert not (injection.month_indexed or injection.spp_indexed)
    assert injection.formula == "(Belpex_15M × 1) + € -13/MWh"
    assert snap.publication_label == "november 2026"
    assert snap.valid_until == date(2026, 11, 30)


@pytest.mark.parametrize(
    ("contract", "factor", "base", "fee", "printed"),
    [(_FLEX, 1.21, 4.0, 50.0, 22.40), (_SMART, 1.1, 19.0, 130.0, 21.90)],
)
def test_the_monthly_cards(
    contract: str, factor: float, base: float, fee: float, printed: float
) -> None:
    """Indexed on the delivery month's RLP-weighted mean, the printed price
    being the formula at October's RLP-M, the fallback for an entry with no
    day-ahead prices."""
    snap = _snap(contract)
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.formula_factor == pytest.approx(factor * 1.06)
    assert energy.formula_base == pytest.approx(base / 1000.0 * 1.06)
    assert energy.current == pytest.approx(printed / 100.0 * 1.06)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.month_indexed and energy.rlp_indexed
    assert energy.rlp_blend == "columns"
    # One formula for every meter.
    assert energy.peak is None and energy.offpeak is None
    assert energy.exclusive_night is None
    injection = snap.injection
    assert injection is not None
    assert injection.spp_indexed and not injection.month_indexed
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-0.020)
    # (141,521 - 20) EUR/MWh, VAT-exempt.
    assert injection.current == pytest.approx(0.1215)


@pytest.mark.parametrize(
    ("contract", "month"),
    [(c, m) for c, m in _CARDS],
)
def test_each_formula_gives_back_the_price_its_card_prints(
    contract: str, month: str
) -> None:
    """Each card works its "**" price out at the month before's RLP-M and
    SPP-M, which it names: that pins the units, EUR/MWh on an index in
    EUR/MWh, before VAT."""
    text = _card(contract, month)
    energy_block, feed_in_block = belvus._price_blocks(text)
    rlp = belvus.named_index(energy_block, "RLP")
    spp = belvus.named_index(feed_in_block, "SPP")
    assert rlp is not None and spp is not None
    year, number = (int(part) for part in month.split("-"))
    before = date(year - (number == 1), (number - 2) % 12 + 1, 1)
    assert rlp[0] == spp[0] == before
    snap = belvus.parse_snapshot(contract, text, "t://")
    energy, injection = snap.energy, snap.injection
    assert injection is not None
    assert injection.factor is not None and injection.base is not None
    if isinstance(energy, VariableRates):
        assert energy.formula_factor is not None and energy.formula_base is not None
        billed = energy.formula_factor * rlp[1] / 1000.0 + energy.formula_base
        assert billed == pytest.approx(energy.current, abs=1e-4)
        credited = injection.factor * spp[1] / 1000.0 + injection.base
        assert credited == pytest.approx(injection.current, abs=1e-4)
    assert snap.publication_label.endswith(str(year))


def test_the_september_card_names_the_product_without_the_brand() -> None:
    """The September header names "Dynamisch" where October's says "Belvus
    Dynamisch"."""
    text = _card(_DYNAMISCH, "2026-09")
    assert "Elektriciteit Dynamisch \u2014 September 2026" in text
    snap = belvus.parse_snapshot(_DYNAMISCH, text, "t://")
    assert snap.publication_label == "september 2026"
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.01 * 1.06)


@pytest.mark.parametrize("contract", [_DYNAMISCH, _FLEX, _SMART])
def test_the_network_table_and_the_levies(contract: str) -> None:
    """Every figure VAT-inclusive, the network rows read as printed."""
    snap = _snap(contract)
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.data_management_per_year == pytest.approx(18.92)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.distribution_exclusive_night == pytest.approx(0.0481)
    assert antwerpen.prosumer_eur_per_kva_year == pytest.approx(54.63)
    assert antwerpen.transport == 0.0
    # The classic table wraps this label onto the line of its figures.
    assert snap.dsos["fluvius_intergem"].prosumer_eur_per_kva_year == pytest.approx(
        59.23
    )
    # Two rows Fluvius's sheet and every other card print as 5,99 and 5,54;
    # billed as printed, and allowed in the live check.
    assert snap.dsos["fluvius_west"].distribution_exclusive_night == pytest.approx(
        0.056
    )
    assert snap.dsos["fluvius_imewo"].distribution_single == pytest.approx(0.0542)
    taxes = snap.taxes
    # The excise and the contribution of before August 2026, which the law's
    # resolvers replace.
    assert taxes.federal_excise == pytest.approx(0.050329)
    assert taxes.federal_excise_bands is None
    assert taxes.energy_contribution == pytest.approx(0.002042)
    # GSC c€1,18 and WKK c€0,42, VAT included.
    assert taxes.flanders_renewables == pytest.approx(0.016)
    # The household row, not the non-residential one above it.
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.vat_rate == 0.0
    # The cards state no rate, so the formulas are grossed by the assumed one.
    assert taxes.card_vat_rate is None
    assert taxes.assumed_vat_rate == pytest.approx(0.06)


def test_a_card_for_another_product_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not Belvus Flex Online"):
        belvus.parse_snapshot(_FLEX, _card(_SMART), "t://")
    with pytest.raises(ExtractorError, match="not Belvus Dynamisch"):
        belvus.parse_snapshot(_DYNAMISCH, _card(_FLEX), "t://")


def test_the_professional_card_is_refused() -> None:
    text = (
        _card(_DYNAMISCH)
        .replace("Particulier Elektriciteit", "Professioneel Elektriciteit")
        .replace("Belvus Dynamisch \u2014", "Belvus Dynamisch Pro \u2014")
    )
    with pytest.raises(ExtractorError, match="card is for Belvus Dynamisch Pro"):
        belvus.parse_snapshot(_DYNAMISCH, text, "t://")
    text = _card(_FLEX).replace(
        "Particulier Elektriciteit", "Professioneel Elektriciteit"
    )
    with pytest.raises(ExtractorError, match="card is for Flex Online EL"):
        belvus.parse_snapshot(_FLEX, text, "t://")


@pytest.mark.parametrize(
    ("contract", "old", "new", "error"),
    [
        pytest.param(
            _FLEX,
            "(Belpex_RLP × 1,21) + € 4/MWh",
            "(Belpex_15M × 1,21) + € 4/MWh",
            "energy formula is on Belpex_15M, not Belpex_RLP",
            id="index",
        ),
        pytest.param(
            _FLEX,
            "F O R M U L E\n(Belpex_RLP × 1,21) + € 4/MWh",
            "F O R M U L E\n(Belpex_RLP × 1,21) + € 4/kWh",
            "energy formula not found",
            id="unit",
        ),
        pytest.param(
            _FLEX,
            "F O R M U L E\n(Belpex_RLP × 1,21) + € 4/MWh",
            "F O R M U L E\n(Belpex_RLP × 1,21) + € 40/MWh",
            "energy formula does not give the price printed",
            id="base",
        ),
        pytest.param(
            _DYNAMISCH,
            "12,85\n**",
            "12,85\n*",
            "feed-in price check not found",
            id="feed-in price",
        ),
        pytest.param(
            _DYNAMISCH,
            "12,85\n**",
            "12,58\n**",
            "feed-in formula does not give the price printed",
            id="feed-in figure",
        ),
        pytest.param(
            _DYNAMISCH,
            "(Belpex_15M × 1) + € -13/MWh",
            "(Belpex_SPP × 1) + € -13/MWh",
            "feed-in formula is on Belpex_SPP, not Belpex_15M",
            id="feed-in index",
        ),
        pytest.param(
            _SMART,
            "F O R M U L E (Belpex_RLP × 1,1) + € 19/MWh",
            "F O R M U L E (Belpex_RLP × 1,1) + € 21/MWh",
            "more than one energy formula",
            id="third page",
        ),
        pytest.param(
            _SMART, "(incl. btw) € 130/jaar", "€ 130/jaar", "yearly fee", id="fee"
        ),
        pytest.param(
            _FLEX,
            "Tarieven excl. btw",
            "Tarieven incl. btw",
            "price blocks",
            id="basis",
        ),
        pytest.param(
            _FLEX,
            "Kosten groene stroom en WKK incl. btw",
            "Kosten groene stroom en WKK excl. btw",
            "levies not stated including VAT",
            id="levies basis",
        ),
        pytest.param(
            _DYNAMISCH,
            "SMR3-meetregime in Vlaanderen",
            "SMR3-meetregime in Wallonië",
            "not for Flemish households",
            id="region",
        ),
    ],
)
def test_a_card_the_reader_does_not_know_is_refused(
    contract: str, old: str, new: str, error: str
) -> None:
    text = _card(contract)
    assert old in text
    with pytest.raises(ExtractorError, match=error):
        belvus.parse_snapshot(contract, text.replace(old, new), "t://")


def test_a_card_in_neither_layout_is_refused() -> None:
    """A card with no page header and none of the older layout's sections is
    refused rather than misread."""
    text = "\n".join(
        line
        for line in _card(_FLEX).splitlines()
        if not line.startswith("Tariefkaart \u00b7")
    )
    with pytest.raises(ExtractorError, match="card header not found"):
        belvus.parse_snapshot(_FLEX, text, "t://")


@pytest.mark.parametrize(
    ("block", "name", "expected"),
    [
        (
            "laatst gekende waarde van Belpex RLP-M 10/2026: €181,836/MWh",
            "RLP",
            (date(2026, 10, 1), 181.836),
        ),
        (
            "historische maandwaarde\nBelpex SPP-M augustus\n2026 van 79,108 EUR/MWh",
            "SPP",
            (date(2026, 8, 1), 79.108),
        ),
        ("Belpex RLP-M 13/2026: €1/MWh", "RLP", None),
        ("Belpex RLP-M brumaire 2026 van 1 EUR/MWh", "RLP", None),
        ("Belpex SPP-M 10/2026: €141,521/MWh", "RLP", None),
    ],
)
def test_named_index(block: str, name: str, expected: object) -> None:
    assert belvus.named_index(block, name) == expected


# ---- the older layout, January to August 2026 ------------------------------


@pytest.mark.parametrize(
    ("contract", "month", "factor", "base", "fee", "printed", "feed_in", "year"),
    [
        # January to March: Smart Plus's first formula, and Fluvius's 2025
        # network table.
        (_SMART, "2026-01", 1.037, 12.21, 130.0, 10.27, 6.73, 2025),
        # April: spaced wider, the year split over two lines and "EL" on a
        # line of its own, the 2025 table still.
        (_SMART, "2026-04", 1.1, 19.0, 130.0, 12.64, 3.32, 2025),
        # May to August: "€4MWh" and 2026's table.
        (_FLEX, "2026-08", 1.21, 4.0, 50.0, 14.25, 4.26, None),
    ],
)
def test_the_older_cards(
    contract: str,
    month: str,
    factor: float,
    base: float,
    fee: float,
    printed: float,
    feed_in: float,
    year: int | None,
) -> None:
    """Read like the later cards: the formula before VAT on the delivery
    month's RLP-weighted mean, the printed price at last month's RLP-M as the
    fallback, the feed-in on the SPP-weighted one. The cards of January to
    April name the 2025 network table they print."""
    snap = _snap(contract, month)
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.formula_factor == pytest.approx(factor * 1.06)
    assert energy.formula_base == pytest.approx(base / 1000.0 * 1.06)
    assert energy.current == pytest.approx(printed / 100.0 * 1.06)
    assert energy.yearly_fixed_fee == pytest.approx(fee)
    assert energy.month_indexed and energy.rlp_indexed
    assert energy.rlp_blend == "columns"
    injection = snap.injection
    assert injection is not None and injection.spp_indexed
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-0.020)
    assert injection.current == pytest.approx(feed_in / 100.0)
    assert snap.network_year == year
    number = int(month[5:])
    assert snap.publication_label == f"{NL_MONTHS[number - 1]} 2026"
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    taxes = snap.taxes
    assert taxes.federal_excise == pytest.approx(0.050329)
    assert taxes.energy_contribution == pytest.approx(0.002042)
    assert taxes.flanders_renewables == pytest.approx(0.016)
    # The household's row, € 0 a month, not the non-residential one.
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.card_vat_rate is None
    assert taxes.assumed_vat_rate == pytest.approx(0.06)


@pytest.mark.parametrize(
    ("contract", "month", "rows"),
    [
        (
            _SMART,
            "2026-01",
            {
                # Fluvius's 2025 figures, the fee and Antwerpen's first.
                "fluvius_antwerpen": (18.56, 53.26, 0.0599, 0.0498, 58.42),
                # The label wraps as "MIDDEN-" and "VLAANDEREN".
                "fluvius_intergem": (18.56, 51.99, 0.0561, 0.0473, 59.75),
                "fluvius_halle_vilvoorde": (18.56, 59.56, 0.062, 0.0523, 65.45),
                # 7,47 and 6,14 on the 2025 sheet: the normal rate misprinted.
                "fluvius_west": (18.56, 60.35, 0.0561, 0.0614, 72.92),
            },
        ),
        (
            _FLEX,
            "2026-08",
            {
                "fluvius_antwerpen": (18.92, 52.37, 0.0535, 0.0481, 54.63),
                # "MIDDENVLAANDEREN" on a line of its own.
                "fluvius_intergem": (18.92, 53.13, 0.0528, 0.0479, 59.23),
                # "ZENNE-" and "DIJLE" in the classic table.
                "fluvius_zenne_dijle": (18.92, 59.49, 0.0587, 0.0532, 65.51),
                "fluvius_west": (18.92, 60.52, 0.067, 0.056, 69.59),
            },
        ),
    ],
)
def test_the_older_network_tables(
    contract: str, month: str, rows: dict[str, tuple[float, ...]]
) -> None:
    snap = _snap(contract, month)
    for key, (fee, capacity, single, night, prosumer) in rows.items():
        overlay = snap.dsos[key]
        assert overlay.data_management_per_year == pytest.approx(fee), key
        assert overlay.capacity_eur_per_kw_year == pytest.approx(capacity), key
        assert overlay.distribution_single == pytest.approx(single), key
        assert overlay.distribution_exclusive_night == pytest.approx(night), key
        assert overlay.prosumer_eur_per_kva_year == pytest.approx(prosumer), key
        assert overlay.transport == 0.0
    assert all(o.prosumer_eur_per_kva_year is not None for o in snap.dsos.values())


@pytest.mark.parametrize(
    ("contract", "old", "new", "error"),
    [
        pytest.param(
            _FLEX, "", "", "card is for SMART PLUS, not FlexOnline", id="product"
        ),
        pytest.param(
            _SMART,
            "Particulier Elektriciteit \n",
            "Professioneel Elektriciteit \n",
            "card is for professionals",
            id="professional",
        ),
        pytest.param(
            _SMART,
            "**€c 10,27/kWh",
            "**€c 10,37/kWh",
            "energy formula does not give the price printed",
            id="price check",
        ),
        pytest.param(
            _SMART,
            "€12,21/MWh",
            "€12,21/kWh",
            "energy formula not found",
            id="unit",
        ),
        pytest.param(
            _SMART,
            "Belpex_SPP x 1",
            "Belpex_RLP x 1",
            "feed-in formula is on Belpex_RLP",
            id="index",
        ),
        pytest.param(
            _SMART,
            "Alle prijzen zijn inclusief BTW",
            "Alle prijzen zijn exclusief BTW",
            "does not state its prices with VAT",
            id="basis",
        ),
        pytest.param(
            _SMART,
            "GSC €c 1,18",
            "(Belpex_RLP x 1,2) + €1/MWh\nGSC €c 1,18",
            "more than one energy formula",
            id="second formula",
        ),
        pytest.param(
            _SMART,
            "Jaarlijkse abonnementskost €130",
            "Jaarlijkse abonnementskost",
            "yearly fee not found",
            id="fee",
        ),
    ],
)
def test_an_older_card_the_reader_does_not_know_is_refused(
    contract: str, old: str, new: str, error: str
) -> None:
    text = _card(_SMART, "2026-01")
    assert old in text
    with pytest.raises(ExtractorError, match=error):
        belvus.parse_snapshot(contract, text.replace(old, new, 1), "t://")


@pytest.mark.parametrize(
    ("contract", "month", "following", "rlp", "spp", "factor", "base"),
    [
        # Between two older cards.
        (_SMART, "2026-01", "2026-02", 0.11104, 0.11416, 1.037, 0.01221),
        # August's older card settled on September's.
        (_FLEX, "2026-08", "2026-09", 0.135111, 0.079108, 1.21, 0.004),
    ],
)
async def test_an_older_month_settles_on_the_next_card(
    monkeypatch: pytest.MonkeyPatch,
    contract: str,
    month: str,
    following: str,
    rlp: float,
    spp: float,
    factor: float,
    base: float,
) -> None:
    file = "SmartPlus" if contract == _SMART else "FlexOnline"
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    monkeypatch.setattr(
        belvus,
        "fetch_pdf_text",
        _cards_served(
            {
                f"{month}/Tariefkaart_{file}": _card(contract, month),
                f"{following}/Tariefkaart_{file}": _card(contract, following),
            }
        ),
    )
    first = date(2026, int(month[5:]), 1)
    snap = await belvus.fetch_for_month(
        make_text_session(""), contract, REGION_FLANDERS, first
    )
    assert snap is not None and not snap.provisional
    energy, injection = snap.energy, snap.injection
    assert isinstance(energy, VariableRates) and injection is not None
    assert energy.index_realised == pytest.approx(rlp)
    assert energy.current == pytest.approx((factor * rlp + base) * 1.06)
    assert injection.index_realised == pytest.approx(spp)
    assert injection.current == pytest.approx(spp - 0.020)


def test_an_older_card_of_2026_bills_fluvius_s_2026_sheet() -> None:
    """January's card prints Zenne-Dijle's 2025 rate, 6,42 c€/kWh; the month
    is billed Fluvius's 2026 sheet, 5,87 to its last decimal. August's card
    prints 2026's table and keeps it."""
    terms = fluvius.parse_area_terms(
        (FIXTURES / "fluvius_zenne_dijle_2026.xlsx").read_bytes(), 2026
    )
    assert terms is not None
    fluvius._area[(2026, _ZENNE_DIJLE)] = terms
    january = _snap(_SMART, "2026-01")
    assert january.dsos[_ZENNE_DIJLE].distribution_single == pytest.approx(0.0642)
    billed = _resolve_snapshot(
        _entry(_SMART), january, delivery_month=date(2026, 1, 1)
    ).dsos[_ZENNE_DIJLE]
    assert billed.distribution_single == pytest.approx(terms.kwh_normal * 1.06)
    assert billed.capacity_eur_per_kw_year == pytest.approx(
        terms.capacity_per_kw_year * 1.06
    )
    august = _snap(_FLEX, "2026-08")
    kept = _resolve_snapshot(
        _entry(_FLEX), august, delivery_month=date(2026, 8, 1)
    ).dsos[_ZENNE_DIJLE]
    assert kept.distribution_single == pytest.approx(0.0587)


_ZENNE_DIJLE = "fluvius_zenne_dijle"


def _entry(contract: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "belvus",
            "contract": contract,
            "region": REGION_FLANDERS,
            "dso": _ZENNE_DIJLE,
            "meter": "mono",
        },
    )


def _pages(current: str, history: str = "") -> AsyncMock:
    async def fetch_text(_session: object, url: str, **_kwargs: object) -> str:
        if url == f"{_HOST}/diensten":
            return current
        if url == f"{_HOST}/historische-tariefkaarten":
            return history
        raise AssertionError(url)

    return AsyncMock(side_effect=fetch_text)


def _link(month: str, name: str, version: str = "") -> str:
    tail = f"?v={version}" if version else ""
    return (
        f'<a href="/public/tariefkaarten/{month}/Tariefkaart_{name}_EL.pdf{tail}">x</a>'
    )


async def test_fetch_reads_the_newest_card_at_the_link_the_page_carries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cards are served immutable for a year, so the link's hash is
    what reaches a card uploaded again under the same name."""
    history = _link("2026-10", "Dynamisch", "b244472dfa8f")
    current = _link("2026-11", "Dynamisch", "22a16b22aaf1") + _link(
        "2026-11", "DynamischPro", "00593b0c0c79"
    )
    monkeypatch.setattr(belvus, "fetch_text", _pages(current, history))
    render = AsyncMock(return_value=_card(_DYNAMISCH))
    monkeypatch.setattr(belvus, "fetch_pdf_text", render)
    session = make_text_session("")
    url = f"{_HOST}/public/tariefkaarten/2026-11/Tariefkaart_Dynamisch_EL.pdf?v=22a16b22aaf1"
    snap = await belvus.fetch(session, _DYNAMISCH, REGION_FLANDERS)
    assert render.call_args.args[1] == url
    assert snap.source_url == url
    assert await belvus.probe(session, _DYNAMISCH, REGION_FLANDERS) == url
    with pytest.raises(ExtractorError, match="not sold in region"):
        await belvus.fetch(session, _DYNAMISCH, "wallonia")
    with pytest.raises(ExtractorError, match="unknown Belvus contract"):
        await belvus.fetch(session, "belvus_other", REGION_FLANDERS)
    with pytest.raises(ExtractorError, match="no Belvus Smart Plus card listed"):
        await belvus.fetch(session, _SMART, REGION_FLANDERS)
    assert await belvus.probe(session, _SMART, REGION_FLANDERS) is None


async def test_a_current_cards_page_that_does_not_answer_leaves_the_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fetch_text(_session: object, url: str, **_kwargs: object) -> str:
        if url.endswith("/diensten"):
            raise ExtractorError("HTTP 500")
        return _link("2026-10", "FlexOnline")

    monkeypatch.setattr(belvus, "fetch_text", AsyncMock(side_effect=fetch_text))
    assert await belvus.probe(make_text_session(""), _FLEX, REGION_FLANDERS) == (
        f"{_HOST}/public/tariefkaarten/2026-10/Tariefkaart_FlexOnline_EL.pdf"
    )


def test_listed_cards_reads_only_the_path_and_a_month() -> None:
    """A link to another host is read on Belvus's own, and a folder whose
    name is no month is skipped."""
    page = (
        '<a href="https://cards.example/public/tariefkaarten/2026-12/'
        'Tariefkaart_Dynamisch_EL.pdf">x</a>'
        + _link("2026-13", "Dynamisch")
        + _link("2026-11", "SmartPlus", "aef82a651eb1")
        + '<a href="/public/tariefkaarten/2026-11/Tariefkaart_SmartPlus_GAS.pdf">x</a>'
    )
    assert belvus.listed_cards(page) == {
        "Dynamisch": {
            date(2026, 12, 1): (
                f"{_HOST}/public/tariefkaarten/2026-12/Tariefkaart_Dynamisch_EL.pdf"
            )
        },
        "SmartPlus": {
            date(2026, 11, 1): (
                f"{_HOST}/public/tariefkaarten/2026-11/"
                "Tariefkaart_SmartPlus_EL.pdf?v=aef82a651eb1"
            )
        },
    }


async def test_a_live_network_table_short_of_an_area_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    text = _card(_FLEX).replace("Fluvius West 60,52", "Fluvius Westhoek 60,52")
    assert set(belvus.parse_snapshot(_FLEX, text, "t://").dsos) == set(FLUVIUS_KEYS) - {
        "fluvius_west"
    }
    monkeypatch.setattr(belvus, "fetch_text", _pages(_link("2026-11", "FlexOnline")))
    monkeypatch.setattr(belvus, "fetch_pdf_text", AsyncMock(return_value=text))
    with pytest.raises(
        ExtractorError, match=r"DSO rows not found for \['fluvius_west'\]"
    ):
        await belvus.fetch(make_text_session(""), _FLEX, REGION_FLANDERS)


def _cards_served(served: dict[str, str]) -> AsyncMock:
    async def fetch_pdf_text(_session: object, url: str, **_kwargs: object) -> str:
        for path, text in served.items():
            if path in url:
                return text
        raise ExtractorError(f"HTTP 404 for {url}")

    return AsyncMock(side_effect=fetch_pdf_text)


async def test_a_closed_month_settles_on_the_indices_the_next_card_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """October is billed at the RLP-M and SPP-M November's card names for
    it, 181,836 and 141,521 EUR/MWh, on October's own formulas."""
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    monkeypatch.setattr(
        belvus,
        "fetch_pdf_text",
        _cards_served(
            {
                "2026-10/Tariefkaart_FlexOnline": _card(_FLEX, "2026-10"),
                "2026-11/Tariefkaart_FlexOnline": _card(_FLEX),
            }
        ),
    )
    snap = await belvus.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 10, 1)
    )
    assert snap is not None and not snap.provisional
    assert snap.publication_label == "oktober 2026"
    energy, injection = snap.energy, snap.injection
    assert isinstance(energy, VariableRates) and injection is not None
    assert energy.index_realised == pytest.approx(0.181836)
    assert energy.current == pytest.approx((1.21 * 0.181836 + 0.004) * 1.06)
    assert injection.index_realised == pytest.approx(0.141521)
    assert injection.current == pytest.approx(0.141521 - 0.020)


async def test_a_month_whose_next_card_is_not_out_stays_provisional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    monkeypatch.setattr(
        belvus,
        "fetch_pdf_text",
        _cards_served({"2026-11/Tariefkaart_FlexOnline": _card(_FLEX)}),
    )
    snap = await belvus.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 11, 1)
    )
    assert snap is not None and snap.provisional
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.index_realised is None


async def test_a_next_card_naming_another_month_settles_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """November's card standing as December's names October, not November."""
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    november = _card(_FLEX)
    december = november.replace("EL \u2014 November 2026", "EL \u2014 December 2026")
    monkeypatch.setattr(
        belvus,
        "fetch_pdf_text",
        _cards_served(
            {
                "2026-11/Tariefkaart_FlexOnline": november,
                "2026-12/Tariefkaart_FlexOnline": december,
            }
        ),
    )
    snap = await belvus.fetch_for_month(
        make_text_session(""), _FLEX, REGION_FLANDERS, date(2026, 11, 1)
    )
    assert snap is not None and snap.provisional
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.index_realised is None
    assert snap.injection is not None and snap.injection.index_realised is None


async def test_the_dynamisch_month_asks_for_no_second_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    render = _cards_served(
        {"2026-09/Tariefkaart_Dynamisch": _card(_DYNAMISCH, "2026-09")}
    )
    monkeypatch.setattr(belvus, "fetch_pdf_text", render)
    snap = await belvus.fetch_for_month(
        make_text_session(""), _DYNAMISCH, REGION_FLANDERS, date(2026, 9, 1)
    )
    assert snap is not None and not snap.provisional
    assert render.await_count == 1


async def test_fetch_for_month_answers_none_and_raises_only_on_a_transient_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = make_text_session("")
    monkeypatch.setattr(belvus, "fetch_text", _pages(""))
    monkeypatch.setattr(belvus, "fetch_pdf_text", _cards_served({}))
    # A month whose folder holds no card.
    assert (
        await belvus.fetch_for_month(session, _FLEX, REGION_FLANDERS, date(2026, 8, 1))
        is None
    )
    # October's card filed under November's folder.
    monkeypatch.setattr(
        belvus,
        "fetch_pdf_text",
        _cards_served({"2026-11/Tariefkaart_FlexOnline": _card(_FLEX, "2026-10")}),
    )
    assert (
        await belvus.fetch_for_month(session, _FLEX, REGION_FLANDERS, date(2026, 11, 1))
        is None
    )
    assert (
        await belvus.fetch_for_month(session, _FLEX, "wallonia", date(2026, 11, 1))
        is None
    )
    monkeypatch.setattr(
        belvus, "fetch_pdf_text", AsyncMock(side_effect=ExtractorError("HTTP 503"))
    )
    with pytest.raises(ExtractorError, match="503"):
        await belvus.fetch_for_month(session, _FLEX, REGION_FLANDERS, date(2026, 10, 1))


def _current_page() -> str:
    return (FIXTURES / "discover" / "belvus.html").read_text(encoding="utf-8")


async def test_discover_names_the_residential_cards_the_page_links() -> None:
    """The Pro cards are professional, and the gas cards the gas
    integration's."""
    page = _current_page()
    assert "DynamischPro_EL" in page and "FlexOnline_GAS" in page
    assert await belvus.discover(make_text_session(page)) == {_DYNAMISCH, _FLEX, _SMART}


async def test_discover_surfaces_a_new_card() -> None:
    page = _current_page() + _link("2026-11", "FlowPlus")
    assert await belvus.discover(make_text_session(page)) == {
        _DYNAMISCH,
        _FLEX,
        _SMART,
        "belvus_flowplus",
    }


# Card text a host could serve instead, on which a pattern tried from every
# place it could start would cost the square of its length.
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            lambda: belvus._REGION_RE.search("residentiële klanten " * 30_000),
            id="region sentence",
        ),
        pytest.param(
            lambda: belvus._HEADER_RE.search(
                "Tariefkaart \u00b7 Particulier Elektriciteit " + "x \u2014 " * 50_000
            ),
            id="header dashes",
        ),
        pytest.param(
            lambda: belvus._FORMULA_RE.search("(Belpex_" + "x" * 200_000),
            id="formula index",
        ),
        pytest.param(
            lambda: belvus._FORMULA_RE.search("(Belpex_RLP × 1) + €" * 20_000),
            id="formula rows",
        ),
        pytest.param(
            lambda: belvus._FORMULA_RE.search("(Belpex_RLP × 1) + €" + " " * 200_000),
            id="formula blanks",
        ),
        pytest.param(
            lambda: belvus._EXCISE_RE.search("Bijzondere accijns" + " " * 200_000),
            id="excise blanks",
        ),
        pytest.param(
            lambda: _belvus_older.NAMED_INDEX_RE.search("Belpex RLP-M " * 30_000),
            id="named index",
        ),
        pytest.param(
            lambda: _belvus_older.NAMED_INDEX_RE.search("Belpex RLP " * 30_000),
            id="named index blanks",
        ),
        pytest.param(
            lambda: _belvus_older._COVER_RE.search(
                "Tariefkaart\nParticulier Elektriciteit\n" + "A " * 100_000
            ),
            id="older cover",
        ),
        pytest.param(
            lambda: _belvus_older._FORMULA_RE.search("(Belpex_RLP x 1) + €" * 20_000),
            id="older formula rows",
        ),
        pytest.param(
            lambda: _belvus_older._FORMULA_RE.search(
                "(Belpex_RLP x 1) + €" + " " * 200_000
            ),
            id="older formula blanks",
        ),
        pytest.param(
            lambda: _belvus_older._PRINTED_RE.match("*€c" + " " * 200_000),
            id="older estimates",
        ),
        pytest.param(
            lambda: _belvus_older._FEE_RE.search(
                "Jaarlijkse abonnementskost" + " " * 200_000
            ),
            id="older fee blanks",
        ),
        pytest.param(
            lambda: _belvus_older._GSC_RE.search("GSC" + " " * 200_000),
            id="older levy blanks",
        ),
        pytest.param(
            lambda: _belvus_older._EXCISE_RE.search(
                "Bijzondere accijns" + " " * 200_000
            ),
            id="older excise blanks",
        ),
        pytest.param(
            lambda: _belvus_older._CONTRIBUTION_RE.search(
                "Bijdrage op energie" + " " * 200_000
            ),
            id="older contribution blanks",
        ),
        pytest.param(
            lambda: _belvus_older._FUND_RE.search(
                "Laagspanning residentieel" + " " * 200_000
            ),
            id="older fund blanks",
        ),
        pytest.param(
            lambda: _belvus_older._ROW_RE.findall("FLUVIUS WEST " * 30_000),
            id="older network labels",
        ),
        pytest.param(
            lambda: _belvus_older._ROW_RE.findall(
                "FLUVIUS WEST " + "1,1 " * 100_000 + "x"
            ),
            id="older network figures",
        ),
        pytest.param(
            lambda: belvus._PRINTED_RE.search("1" * 200_000 + ",1\n"),
            id="printed digits",
        ),
        pytest.param(
            lambda: belvus._FUND_RE.search(
                "Bijzondere Energiefonds" + " laagspanning residentieel" * 20_000
            ),
            id="fund rows",
        ),
        pytest.param(
            lambda: belvus._LINK_RE.findall("/public/tariefkaarten/2026-11/" * 20_000),
            id="links",
        ),
        pytest.param(
            lambda: belvus._DATA_RE.search("databeheertarief" * 30_000),
            id="data management",
        ),
        pytest.param(
            lambda: belvus._FEE_RE.search("Jaarlijkse abonnementskost" + " " * 200_000),
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
