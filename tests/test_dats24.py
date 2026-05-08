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

"""Fixture-based tests for the DATS 24 extractor."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.be_electricity_prices.providers.base import (
    SupplierSnapshot,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.dats24 import parse_snapshot
from tests import fixture_text


def _text() -> str:
    return fixture_text("dats24_groen_variabel_apr.pdf", layout=True)


def _snap(region: str) -> SupplierSnapshot:
    return parse_snapshot(_text(), "test://dats24", region)


def test_april_card_publication_metadata() -> None:
    snap = _snap("flanders")
    assert snap.supplier == "dats24"
    assert snap.contract == "dats24_groen_variabel"
    assert snap.publication_label == "april 2026"
    # The card prints an explicit "GELDIG VAN 1 APRIL 2026 T.E.M 30 APRIL
    # 2026" header that parse_valid_until catches.
    assert snap.valid_until == date(2026, 4, 30)


def test_april_card_energy_uses_indicative_tvac_values() -> None:
    """The card prints "Afname1 12,18 13,48 10,97 10,97" -- the
    previous-month spot fed through the contract formula, including
    6% VAT. We use those resolved figures directly because spot data
    isn't available at parse time."""
    snap = _snap("flanders")
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1218)
    assert snap.energy.peak == pytest.approx(0.1348)
    assert snap.energy.offpeak == pytest.approx(0.1097)
    assert snap.energy.exclusive_night == pytest.approx(0.1097)
    # Vaste vergoeding 38,50 EUR/yr (residential base subscription).
    assert snap.energy.yearly_fixed_fee == pytest.approx(38.50)


def test_april_card_taxes_are_tvac() -> None:
    """All printed amounts (except where the card says otherwise)
    include 6% VAT, matching the project convention -- no extra
    scaling needed in compute_breakdown. Region-specific overlays are
    gated by the snapshot's region: a Flanders user does not see the
    Walloon connection fee or CV renewables, and a Wallonia user does
    not see the Flemish Energiefonds or GSC/WKC."""
    fl = _snap("flanders").taxes
    assert fl.vat_rate == 0.0
    assert fl.federal_excise == pytest.approx(0.0503288)
    assert fl.energy_contribution == pytest.approx(0.0020417)
    # Vlaams Gewest GSC + WKC = 1,183 + 0,378 = 1,561 c€/kWh.
    assert fl.flanders_renewables == pytest.approx(0.01561)
    assert fl.wallonia_renewables == 0.0
    assert fl.region_connection_fee == 0.0
    # "Hoofdverblijf (domicilie) 0,00 €/maand" -- residential default
    # is zero. Second-home users should override in OptionsFlow.
    assert fl.energy_fund_eur_per_month == 0.0

    wa = _snap("wallonia").taxes
    # Federal levies are region-agnostic.
    assert wa.federal_excise == pytest.approx(0.0503288)
    assert wa.energy_contribution == pytest.approx(0.0020417)
    # Walloon side fills CV renewables + connection fee, leaves Flemish
    # overlay at 0.
    assert wa.flanders_renewables == 0.0
    assert wa.wallonia_renewables == pytest.approx(0.03032)
    assert wa.region_connection_fee == pytest.approx(0.00075)
    assert wa.energy_fund_eur_per_month == 0.0


def test_april_card_injection_carries_formula_and_indicative() -> None:
    """The card prints both the BE_spotSPP-indexed formula and the
    indicative current value. Both surface, with factor / base in
    EUR/kWh-against-EUR/kWh-spot units."""
    snap = _snap("flanders")
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.0326)
    # 0.0766 c€/kWh per €/MWh -> 0.766 EUR/kWh per EUR/kWh.
    assert inj.factor == pytest.approx(0.766)
    # -1.11 c€/kWh -> -0.0111 EUR/kWh.
    assert inj.base == pytest.approx(-0.0111)


def test_injection_formula_handles_plus_operator() -> None:
    """A future card with '(BE_spotSPP x 0,0766 + 0,5)' must parse with
    a positive base, not silently lose the formula. Previously the
    regex hard-coded '-' and any other operator dropped factor/base to
    None."""
    from custom_components.be_electricity_prices.providers.dats24 import (
        _extract_injection,
    )

    text = "Teruglevering2 (c€/kWh) 3,26\nFormula: (BE_spotSPP x 0,0766 + 0,5) c€/kWh\n"
    inj = _extract_injection(text)
    assert inj is not None
    assert inj.factor == pytest.approx(0.766)
    assert inj.base == pytest.approx(0.005)


def test_april_card_flanders_dsos_cover_all_eight_fluvius() -> None:
    snap = _snap("flanders")
    assert set(snap.dsos) == {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    # Spot-check Antwerpen: capacity 52.37 EUR/kW/yr, distribution
    # 5.35 c€/kWh, data-management 18.92 EUR/yr (jaarlijks meteropname).
    a = snap.dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert a.distribution_single == pytest.approx(0.0535)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_april_card_wallonia_dsos_collapse_seven_ores_subareas_to_one() -> None:
    """DATS 24 lists seven ORES sub-areas (Brabant Wallon, Est, Hainaut,
    Luxembourg, Mouscron, Namur, Verviers) with byte-identical rates;
    the integration's DSO_CHOICES has only one ORES key, so we keep
    just the first."""
    snap = _snap("wallonia")
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    # Spot-check ORES (representative -- all sub-areas share these):
    # single 11,98 / day 13,27 / night 7,39 / PIC 16,57 / MED 10,83 /
    # ECO 5,09 / transport 2,74 / data 14,10 / prosumer 85,84.
    ores = snap.dsos["ores"]
    assert ores.distribution_single == pytest.approx(0.1198)
    assert ores.distribution_peak == pytest.approx(0.1327)
    assert ores.distribution_offpeak == pytest.approx(0.0739)
    assert ores.distribution_pic == pytest.approx(0.1657)
    assert ores.distribution_medium == pytest.approx(0.1083)
    assert ores.distribution_eco == pytest.approx(0.0509)
    assert ores.transport == pytest.approx(0.0274)
    assert ores.data_management_per_year == pytest.approx(14.10)
    assert ores.prosumer_eur_per_kva_year == pytest.approx(85.84)


def test_april_card_resa_uses_distinct_distribution_rates() -> None:
    """RESA's network is cheaper than ORES on most lines; this guards
    against a regex that would silently align all Walloon DSOs to the
    same row by accident."""
    snap = _snap("wallonia")
    resa = snap.dsos["resa"]
    assert resa.distribution_single == pytest.approx(0.1106)
    assert resa.prosumer_eur_per_kva_year == pytest.approx(84.22)


def test_may_card_uses_dot_decimal_separator() -> None:
    """The May 2026 card switched its decimal separator from ',' to '.'
    (Afname1 10.64 11.77 9.60 9.60 instead of 12,18 13,48 10,97 10,97).
    Without dot-tolerant regexes the parser raised "could not parse
    DATS 24 indicative afname row"."""
    text = fixture_text("dats24_groen_variabel_may.pdf", layout=True)
    snap = parse_snapshot(text, "test://may", "flanders")
    assert isinstance(snap.energy, VariableRates)
    assert snap.publication_label == "mei 2026"
    assert snap.valid_until == date(2026, 5, 31)
    assert snap.energy.current == pytest.approx(0.1064)
    assert snap.energy.peak == pytest.approx(0.1177)
    assert snap.energy.offpeak == pytest.approx(0.0960)
    assert snap.energy.exclusive_night == pytest.approx(0.0960)
    assert snap.energy.yearly_fixed_fee == pytest.approx(38.50)
    # DSOs and taxes also parse without comma-dependent regexes.
    assert len(snap.dsos) == 8
