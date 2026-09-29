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

"""Aspiravi Energy extractor tests against the September and March 2026 cards."""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS, REGION_FLANDERS
from custom_components.be_electricity_prices.providers import EXTRACTORS, aspiravi
from custom_components.be_electricity_prices.providers._rates import VariableRates
from custom_components.be_electricity_prices.providers.base import ExtractorError
from tests import fixture_text, make_text_session

_CID = "aspiravi_eco_plus_flex"
_SEPTEMBER = "aspiravi_eco_plus_flex_2026-09.pdf"
_MARCH = "aspiravi_eco_plus_flex_2026-03.pdf"
_UPLOADS = "https://aspiravi-energy.be/wp-content/uploads"


def _link(path: str, label: str) -> str:
    return f'<a class="btn" href="{_UPLOADS}/{path}"><span>{label}</span></a>'


def test_aspiravi_sells_one_flemish_month_indexed_contract() -> None:
    extractor = EXTRACTORS["aspiravi"]
    assert extractor.label == "Aspiravi Energy"
    (contract,) = extractor.contracts
    assert contract.id == _CID
    assert contract.kind == "variable"
    assert contract.regions == frozenset({REGION_FLANDERS})
    assert contract.month_indexed_energy
    assert contract.spot_indexed_injection


def test_september_card() -> None:
    snap = aspiravi.parse_snapshot(_CID, fixture_text(_SEPTEMBER))
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    # The rates printed at August's mean, plus the 0,106 c€ charity
    # contribution the card leaves out of them.
    assert energy.current == pytest.approx(0.18127)
    assert energy.peak == pytest.approx(0.20523)
    assert energy.offpeak == pytest.approx(0.15734)
    assert energy.exclusive_night == pytest.approx(0.15369)
    assert energy.yearly_fixed_fee == pytest.approx(38.5)
    assert energy.yearly_fixed_fee_exclusive_night is None
    assert energy.month_indexed
    assert not energy.rlp_indexed
    # 0,116 x Belpex + 2 c€/kWh before VAT, onto VAT-inclusive EUR/kWh.
    assert energy.formula_factor == pytest.approx(0.116 * 10.6)
    assert energy.formula_base == pytest.approx(0.0212 + 0.00106)
    assert energy.formula_factor_peak == pytest.approx(0.1335 * 10.6)
    assert energy.formula_factor_offpeak == pytest.approx(0.09854 * 10.6)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.09588 * 10.6)
    injection = snap.injection
    assert injection is not None
    assert injection.current == pytest.approx(0.07052)
    assert injection.factor == pytest.approx(0.7)
    assert injection.base == pytest.approx(-0.02)
    assert injection.month_indexed
    assert not injection.spp_indexed
    taxes = snap.taxes
    assert taxes.federal_excise == pytest.approx(0.0503288)
    assert taxes.energy_contribution == pytest.approx(0.002042)
    # Green power and WKK, 1,078 + 0,406 c€ printed before VAT.
    assert taxes.flanders_renewables == pytest.approx(0.01484 * 1.06)
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == pytest.approx(0.06)
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    imewo = snap.dsos["fluvius_imewo"]
    assert imewo.distribution_single == pytest.approx(0.0554)
    assert imewo.distribution_exclusive_night == pytest.approx(0.0501)
    assert imewo.data_management_per_year == pytest.approx(18.92)
    assert imewo.capacity_eur_per_kw_year == pytest.approx(57.4530)
    assert imewo.prosumer_eur_per_kva_year == pytest.approx(60.12)
    assert imewo.transport == 0.0
    assert snap.publication_label == "september 2026"
    assert snap.valid_until == date(2026, 9, 30)


def test_the_formulas_reproduce_the_rates_the_card_prints() -> None:
    """At August's 129,32 EUR/MWh every register comes back to its printed
    rate, which checks the scaling, the VAT and the charity contribution
    together. The day rate is 3e-5 off: the card prints its coefficient
    rounded to 0,1335, and its own rates work back to 0,13348."""
    snap = aspiravi.parse_snapshot(_CID, fixture_text(_SEPTEMBER))
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    belpex = 0.12932
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
        assert factor * belpex + base == pytest.approx(printed, abs=5e-5)
    injection = snap.injection
    assert injection is not None and injection.factor is not None
    assert injection.base is not None
    # Printed to a thousandth of a cent, so within half of that.
    assert injection.factor * belpex + injection.base == pytest.approx(
        injection.current, abs=5e-6
    )


def test_a_card_is_dated_by_its_price_table() -> None:
    """The March 2026 card still says it is for contracts signed in February
    and that its formulas hold from 1 February. Its table of past rates ends
    on February, which is the month its printed rates are for, so the card
    is March's."""
    text = fixture_text(_MARCH)
    assert "afgesloten in februari 2026" in text
    snap = aspiravi.parse_snapshot(_CID, text)
    assert snap.publication_label == "maart 2026"
    assert snap.valid_until == date(2026, 3, 31)


def test_a_dual_meter_fee_of_its_own_is_refused() -> None:
    text = fixture_text(_SEPTEMBER).replace(
        "incl. BTW 38,5 38,5 38,5", "incl. BTW 38,5 42 38,5"
    )
    with pytest.raises(ExtractorError, match="dual meter"):
        aspiravi.parse_snapshot(_CID, text)


def test_an_exclusive_night_fee_of_its_own_is_kept() -> None:
    text = fixture_text(_SEPTEMBER).replace(
        "incl. BTW 38,5 38,5 38,5", "incl. BTW 38,5 38,5 30"
    )
    energy = aspiravi.parse_snapshot(_CID, text).energy
    assert isinstance(energy, VariableRates)
    assert energy.yearly_fixed_fee_exclusive_night == pytest.approx(30.0)


async def test_fetch_reads_the_card_linked_as_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _link("2025/12/voorwaarden.pdf", "Algemene voorwaarden") + _link(
        "2026/09/900027_card.pdf", "Huidige tariefkaart"
    )
    render = AsyncMock(return_value=fixture_text(_SEPTEMBER))
    monkeypatch.setattr(aspiravi, "fetch_pdf_text", render)
    snap = await aspiravi.fetch(make_text_session(page), _CID, REGION_FLANDERS)
    assert render.call_args.args[1] == f"{_UPLOADS}/2026/09/900027_card.pdf"
    assert snap.source_url == f"{_UPLOADS}/2026/09/900027_card.pdf"
    with pytest.raises(ExtractorError, match="no current card"):
        await aspiravi.fetch(make_text_session("<p>nothing</p>"), _CID, REGION_FLANDERS)


async def test_probe_asks_the_current_card(monkeypatch: pytest.MonkeyPatch) -> None:
    freshness = AsyncMock(return_value="Wed, 02 Sep 2026 08:43:42 GMT")
    monkeypatch.setattr(aspiravi, "head_freshness_key", freshness)
    page = _link("2026/09/900027_card.pdf", "Huidige tariefkaart")
    key = await aspiravi.probe(make_text_session(page), _CID, REGION_FLANDERS)
    assert key == "Wed, 02 Sep 2026 08:43:42 GMT"
    assert freshness.call_args.args[1] == f"{_UPLOADS}/2026/09/900027_card.pdf"
    assert await aspiravi.probe(make_text_session(""), _CID, REGION_FLANDERS) is None
    assert (
        await aspiravi.probe(make_text_session(page), "other", REGION_FLANDERS) is None
    )


async def test_fetch_for_month_takes_the_card_labelled_with_the_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive labels a second, older card with the same month twice: the
    link that does not prove to be the month's card is passed over."""
    texts = {
        f"{_UPLOADS}/2024/10/Eco-Plus-Flex-2023.pdf": "a card of another layout",
        f"{_UPLOADS}/2026/08/202609.pdf": fixture_text(_SEPTEMBER),
        f"{_UPLOADS}/2026/05/202603.pdf": fixture_text(_MARCH),
    }

    async def render(_session: Any, url: str) -> str:
        return texts[url]

    monkeypatch.setattr(aspiravi, "fetch_pdf_text", render)
    page = "".join(
        (
            _link("2026/08/202609.pdf", "900027 eco plus flex (huishoudelijk) sep 26"),
            _link(
                "2024/10/Eco-Plus-Flex-2023.pdf",
                "900027 eco plus flex (huishoudelijk) maart 26",
            ),
            _link(
                "2026/05/202603.pdf", "900027 eco plus flex (huishoudelijk) maart 26"
            ),
            _link(
                "2024/10/Eco-Plus-Flex-2022.pdf",
                "900027 eco plus flex (huishoudelijk) eco plus flex 2022",
            ),
        )
    )
    session = make_text_session(page)
    march = await aspiravi.fetch_for_month(
        session, _CID, REGION_FLANDERS, date(2026, 3, 15)
    )
    assert march is not None
    assert march.source_url == f"{_UPLOADS}/2026/05/202603.pdf"
    assert march.valid_until == date(2026, 3, 31)
    assert (
        await aspiravi.fetch_for_month(session, _CID, REGION_FLANDERS, date(2026, 4, 1))
        is None
    )
    assert (
        await aspiravi.fetch_for_month(
            session, "other", REGION_FLANDERS, date(2026, 3, 1)
        )
        is None
    )


async def test_fetch_for_month_refuses_a_card_of_another_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aspiravi, "fetch_pdf_text", AsyncMock(return_value=fixture_text(_SEPTEMBER))
    )
    page = _link("2026/05/202603.pdf", "900027 eco plus flex (huishoudelijk) maart 26")
    assert (
        await aspiravi.fetch_for_month(
            make_text_session(page), _CID, REGION_FLANDERS, date(2026, 3, 1)
        )
        is None
    )


async def test_discover_names_every_product_code() -> None:
    page = (
        _link("2026/09/900027_Eco_Plus_flex_huishoudelijk-formule.pdf", "Huidige")
        + _link("2025/06/900027-eco-plus-flex-huishoudelijk-juni-25.pdf", "juni 25")
        + _link("2027/01/900031_Eco_Plus_vast_huishoudelijk.pdf", "Vast")
        + _link("2024/10/GeneralConditions_current.pdf", "Algemene voorwaarden")
    )
    assert await aspiravi.discover(make_text_session(page)) == {
        _CID,
        "aspiravi_900031",
    }
