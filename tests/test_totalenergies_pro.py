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
"""TotalEnergies' professional cards, against the November 2026 cards."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from custom_components.be_electricity_prices.const import DSO_SIBELGA
from custom_components.be_electricity_prices.providers import EXTRACTORS, totalenergies
from custom_components.be_electricity_prices.providers._rates import (
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers._resolve import (
    apply_vat,
    resolve_excise_band,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.totalenergies import (
    parse_injection_card,
    parse_snapshot,
)
from tests import fixture_text

_BANDS = ((20000.0, 0.0111), (50000.0, 0.009), (1000000.0, 0.0083))


def _card(name: str, contract: str, region: str) -> SupplierSnapshot:
    return parse_snapshot(contract, fixture_text(name, layout=True), region)


def _bands(snap: SupplierSnapshot) -> list[tuple[float, float]]:
    bands = snap.taxes.federal_excise_bands
    assert bands is not None
    return [(upper, round(rate, 6)) for upper, rate in bands]


def test_the_five_products_are_professional_contracts() -> None:
    contracts = {c.id: c for c in EXTRACTORS["totalenergies"].contracts}
    pro = {cid for cid, c in contracts.items() if c.professional}
    assert pro == {
        "totalenergies_pro_procomfort",
        "totalenergies_pro_procomfort_fixed",
        "totalenergies_pro_prodrive",
        "totalenergies_pro_proessential",
        "totalenergies_pro_proessential_fixed",
    }
    assert all(len(contracts[cid].regions) == 3 for cid in pro)
    assert contracts["totalenergies_pro_procomfort"].month_indexed_energy
    assert not contracts["totalenergies_pro_procomfort_fixed"].month_indexed_energy


def test_the_cards_are_read_from_the_professional_folder() -> None:
    by_id = totalenergies._CONTRACTS_BY_ID
    pro = by_id["totalenergies_pro_procomfort"]
    assert totalenergies._document_url(pro, "flanders") == (
        "https://totalenergies.be/static/marketing-documents/b2b/tariff-card/"
        "latest/PROCOMFORT_ELECTRICITY_VL_FR.pdf"
    )
    assert totalenergies._injection_url(pro, "brussels", "NL").endswith(
        "/b2b/tariff-card/latest/INJECTION_PROCOMFORT_ELECTRICITY_BXL_NL.pdf"
    )
    residential = by_id["totalenergies_mycomfort"]
    assert "/b2c/" in totalenergies._document_url(residential, "flanders")


def test_procomfort_flanders_reads_whole() -> None:
    """Every figure of the French card, excluding VAT. The green energy
    contribution is billed apart from the rates (1,382 c€/kWh), the excise is
    the professional schedule from 0 kWh although the card heads its first
    tranche 3.000 to 20.000, and the energy fund is the low-voltage row."""
    snap = _card(
        "totalenergies_pro_procomfort_v_2026-11.pdf",
        "totalenergies_pro_procomfort",
        "flanders",
    )
    energy = snap.energy
    taxes = snap.taxes
    assert isinstance(energy, VariableRates)
    assert energy.yearly_fixed_fee == 90.0
    # The monthly block at the last known index, not the regulator's
    # estimate in the table (21,54 / 23,08 / 20,02 / 20,76).
    assert (energy.current, energy.peak, energy.offpeak, energy.exclusive_night) == (
        pytest.approx(0.2173),
        pytest.approx(0.2329),
        pytest.approx(0.2020),
        pytest.approx(0.2094),
    )
    # 0.1064 * BELPEXM_RLP + 2.38 c€/kWh, not grossed by any VAT.
    assert energy.month_indexed
    assert energy.formula_factor == pytest.approx(1.064)
    assert energy.formula_base == pytest.approx(0.0238)
    assert energy.formula_base_exclusive_night == pytest.approx(0.0232)
    assert taxes.flanders_renewables == pytest.approx(0.01382)
    assert _bands(snap) == list(_BANDS)
    assert taxes.federal_excise == pytest.approx(0.0111)
    assert taxes.energy_contribution == 0.0
    assert taxes.energy_fund_eur_per_month == 10.07
    assert taxes.vat_rate == 0.21
    assert taxes.card_vat_rate is None
    assert taxes.assumed_vat_rate is None
    assert snap.publication_label == "novembre 2026"
    assert snap.injection is None
    antwerp = snap.dsos["fluvius_antwerpen"]
    assert antwerp.distribution_single == pytest.approx(0.0505)
    assert antwerp.capacity_eur_per_kw_year == 49.40
    assert antwerp.data_management_per_year == 17.85
    assert len(snap.dsos) == 8


def test_the_rates_solve_to_the_index_the_residential_cards_print_at() -> None:
    """Read excluding VAT, the monthly rates are the formula at about 181,8
    EUR/MWh, the index the residential cards of the month print at. With the
    formula grossed by the residential 6% they solved to 170,3, and a month
    re-priced on it billed 6% over the card."""
    snap = _card(
        "totalenergies_pro_procomfort_v_2026-11.pdf",
        "totalenergies_pro_procomfort",
        "flanders",
    )
    index = totalenergies._printed_energy_index(snap.energy)
    assert index == pytest.approx(0.18184, abs=5e-5)


def test_the_dutch_card_reads_like_the_french_one() -> None:
    french = asdict(
        _card(
            "totalenergies_pro_procomfort_v_2026-11.pdf",
            "totalenergies_pro_procomfort",
            "flanders",
        )
    )
    dutch = asdict(
        _card(
            "totalenergies_pro_procomfort_v_2026-11_nl.pdf",
            "totalenergies_pro_procomfort",
            "flanders",
        )
    )
    assert dutch == french


def test_procomfort_wallonia() -> None:
    snap = _card(
        "totalenergies_pro_procomfort_w_2026-11.pdf",
        "totalenergies_pro_procomfort",
        "wallonia",
    )
    taxes = snap.taxes
    assert taxes.wallonia_renewables == pytest.approx(0.0302)
    assert taxes.flanders_renewables == 0.0
    assert taxes.region_connection_fee == pytest.approx(0.0007)
    assert taxes.energy_fund_eur_per_month == 0.0
    assert _bands(snap) == list(_BANDS)
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1025)
    assert aieg.data_management_per_year == 18.39
    assert len(snap.dsos) == 5


def test_procomfort_brussels_in_dutch() -> None:
    """The Sibelga row carries transport, metering and both capacity terms,
    and the energy contribution heads the excise columns."""
    snap = _card(
        "totalenergies_pro_procomfort_b_2026-11_nl.pdf",
        "totalenergies_pro_procomfort",
        "brussels",
    )
    taxes = snap.taxes
    assert taxes.brussels_renewables == pytest.approx(0.023134)
    assert taxes.energy_contribution == 0.0
    assert _bands(snap) == list(_BANDS)
    sibelga = snap.dsos[DSO_SIBELGA]
    assert sibelga.distribution_single == pytest.approx(0.0939)
    assert sibelga.distribution_exclusive_night == pytest.approx(0.0710)
    assert sibelga.transport == pytest.approx(0.0214)
    # Metering 13,90 and the capacity term up to 13 kVA, 47,24, or above it,
    # 94,48.
    assert sibelga.data_management_per_year == pytest.approx(61.14)
    assert sibelga.brussels_power_term_above_13kva == pytest.approx(108.38)
    assert sibelga.brussels_osp_by_tier is not None
    assert sibelga.brussels_osp_by_tier["le13"] == 25.20


def test_the_fixed_card_prints_its_fee_first() -> None:
    snap = _card(
        "totalenergies_pro_procomfort_fixed_b_2026-11.pdf",
        "totalenergies_pro_procomfort_fixed",
        "brussels",
    )
    energy = snap.energy
    assert isinstance(energy, FixedRates)
    assert energy.yearly_fixed_fee == 90.0
    assert (energy.single, energy.peak, energy.offpeak, energy.exclusive_night) == (
        pytest.approx(0.2373),
        pytest.approx(0.2522),
        pytest.approx(0.2240),
        pytest.approx(0.2305),
    )
    assert snap.taxes.brussels_renewables == pytest.approx(0.023134)


def test_the_title_spelled_apart_still_gives_the_month() -> None:
    """The Flemish proComfort Fixe card titles itself "pro Comfort Fixe"."""
    snap = _card(
        "totalenergies_pro_procomfort_fixed_v_2026-11.pdf",
        "totalenergies_pro_procomfort_fixed",
        "flanders",
    )
    assert snap.publication_label == "novembre 2026"
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.2373)


def test_proessential_fixed_and_prodrive() -> None:
    essential = _card(
        "totalenergies_pro_proessential_fixed_w_2026-11_nl.pdf",
        "totalenergies_pro_proessential_fixed",
        "wallonia",
    )
    assert isinstance(essential.energy, FixedRates)
    assert essential.energy.yearly_fixed_fee == 35.0
    assert essential.energy.single == pytest.approx(0.2332)
    drive = _card(
        "totalenergies_pro_prodrive_b_2026-11.pdf",
        "totalenergies_pro_prodrive",
        "brussels",
    )
    assert isinstance(drive.energy, VariableRates)
    assert drive.energy.current == pytest.approx(0.2173)
    assert drive.energy.month_indexed


def test_a_blank_first_tranche_is_refused() -> None:
    """The Dutch proEssential card in Flanders leaves its 0 to 20.000 kWh
    figure blank. Read from the next row, every kWh up to 50 MWh would be
    billed at 0,90."""
    with pytest.raises(ExtractorError, match="first excise tranche"):
        _card(
            "totalenergies_pro_proessential_v_2026-11_nl.pdf",
            "totalenergies_pro_proessential",
            "flanders",
        )


def test_a_residential_card_is_not_taken_for_a_professional_one() -> None:
    with pytest.raises(ExtractorError, match="proComfort Variable"):
        _card(
            "totalenergies_mycomfort_v.pdf", "totalenergies_pro_procomfort", "flanders"
        )


@pytest.mark.parametrize(
    "fixture",
    [
        "totalenergies_injection_pro_procomfort_v_2026-11.pdf",
        "totalenergies_injection_pro_procomfort_v_2026-11_nl.pdf",
    ],
)
def test_the_feed_in_card_is_exempt_from_vat(fixture: str) -> None:
    """ "Injection Variable Pro", 0.04288 * BELPEXM - 0.502, "exonérée de TVA":
    so apply_vat leaves it as printed."""
    leg, month = parse_injection_card(
        "totalenergies_pro_procomfort", fixture_text(fixture, layout=True), "flanders"
    )
    assert month == "novembre 2026"
    assert leg is not None
    assert leg.current == pytest.approx(0.0697)
    assert leg.factor == pytest.approx(0.4288)
    assert leg.base == pytest.approx(-0.00502)
    assert leg.month_indexed
    assert not leg.vat_applies


def test_a_residential_feed_in_card_is_refused() -> None:
    with pytest.raises(ExtractorError, match="Injection Variable Pro"):
        parse_injection_card(
            "totalenergies_pro_procomfort",
            fixture_text(
                "totalenergies_injection_mycomfort_v_2026-10.pdf", layout=True
            ),
            "flanders",
        )


def test_an_entry_bills_the_card_with_vat_and_the_blended_excise() -> None:
    """At 30 000 kWh a year the excise is 20 000 kWh at 1,11 and 10 000 at
    0,90; with VAT the fee is 90 x 1,21 and the energy fund stays as printed."""
    snap = _card(
        "totalenergies_pro_procomfort_v_2026-11.pdf",
        "totalenergies_pro_procomfort",
        "flanders",
    )
    billed = resolve_excise_band(apply_vat(snap, include_vat=True), 30000.0)
    assert billed.taxes.federal_excise == pytest.approx(
        (20000 * 0.0111 + 10000 * 0.009) / 30000
    )
    assert billed.energy.yearly_fixed_fee == pytest.approx(108.9)
    assert billed.taxes.energy_fund_eur_per_month == 10.07
    assert billed.taxes.vat_rate == 0.21
    deducting = apply_vat(snap, include_vat=False)
    assert deducting.energy.yearly_fixed_fee == 90.0
    assert deducting.taxes.vat_rate == 0.0
