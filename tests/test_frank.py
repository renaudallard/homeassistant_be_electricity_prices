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

"""Fixture-based tests for the Frank Energie extractor."""

from __future__ import annotations

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.frank import (
    _matches_suffix,
    parse_snapshot,
)
from tests import fixture_text


def _text() -> str:
    return fixture_text("frank_dynamic_apr.pdf", layout=True)


def _snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text(),
        "test://frank-apr",
        "frank_dynamic",
        "april 2026",
    )


def test_energy_is_dynamic_rates() -> None:
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)


def test_energy_formula_factor() -> None:
    """(0,1068 x BELPEX + 1,500) x 1,06 => factor = 0.1068 * 1.06 * 10."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(0.1068 * 1.06 * 10.0)


def test_energy_formula_base() -> None:
    """base = 1.500 * 1.06 / 100."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.base == pytest.approx(1.500 * 1.06 / 100.0)


def test_yearly_fixed_fee() -> None:
    """2,92 EUR/month => 35.04 EUR/year."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(2.92 * 12.0)


def test_dsos_cover_all_eight_fluvius_subareas() -> None:
    snap = _snap()
    expected = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    assert set(snap.dsos) == expected


def test_dso_antwerpen_distribution() -> None:
    """Antwerpen digital meter: normaal 5,35 ct/kWh, excl nacht 4,81 ct/kWh."""
    snap = _snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(5.35 / 100.0)
    assert a.distribution_exclusive_night == pytest.approx(4.81 / 100.0)


def test_dso_antwerpen_capacity_and_databeheer() -> None:
    snap = _snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_dso_transport_is_zero() -> None:
    """Transport is bundled into distribution on Frank Energie's card."""
    snap = _snap()
    for overlay in snap.dsos.values():
        assert overlay.transport == 0.0


def test_dso_kempen_despite_bracket_artifact() -> None:
    """The PDF renders 'Fluvius [Kempen)' with a mismatched bracket."""
    snap = _snap()
    assert "fluvius_iveka" in snap.dsos
    k = snap.dsos["fluvius_iveka"]
    assert k.distribution_single == pytest.approx(6.34 / 100.0)


def test_taxes_federal_excise() -> None:
    """5,0328 EURct/kWh VAT-inclusive."""
    snap = _snap()
    assert snap.taxes.federal_excise == pytest.approx(5.0328 / 100.0)


def test_taxes_energy_contribution() -> None:
    snap = _snap()
    assert snap.taxes.energy_contribution == pytest.approx(0.2042 / 100.0)


def test_taxes_flanders_renewables_gsc_plus_wkk() -> None:
    """GSC 1,166 + WKK 0,371 = 1,537 EURct/kWh."""
    snap = _snap()
    assert snap.taxes.flanders_renewables == pytest.approx((1.166 + 0.371) / 100.0)


def test_taxes_energy_fund_residential_zero() -> None:
    snap = _snap()
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(0.0)


def test_taxes_vat_rate_zero() -> None:
    """All values on the card are already VAT-inclusive."""
    snap = _snap()
    assert snap.taxes.vat_rate == 0.0


def test_injection_factor() -> None:
    """(0,1 x BELPEX - 1,150) VAT-exempt => factor = 0.1 * 10 = 1.0."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_injection_base() -> None:
    """base = -1.150 / 100 = -0.01150."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.base == pytest.approx(-1.150 / 100.0)


def test_supplier_and_contract_metadata() -> None:
    snap = _snap()
    assert snap.supplier == "frank"
    assert snap.contract == "frank_dynamic"
    assert snap.publication_label == "april 2026"


def test_valid_until_is_end_of_april() -> None:
    snap = _snap()
    assert snap.valid_until is not None
    assert snap.valid_until.month == 4
    assert snap.valid_until.year == 2026


# ---- registration ---------------------------------------------------------------


def test_frank_is_registered() -> None:
    assert "frank" in EXTRACTORS
    assert EXTRACTORS["frank"].label == "Frank Energie"
    contract_ids = {c.id for c in EXTRACTORS["frank"].contracts}
    assert contract_ids == {
        "frank_dynamic",
        "frank_dynamic_hv",
        "frank_dynamic_korting",
        "frank_dynamic_jn",
        "frank_dynamic_slim",
    }


def test_all_contracts_are_dynamic_and_flanders_only() -> None:
    for c in EXTRACTORS["frank"].contracts:
        assert c.kind == "dynamic"
        assert c.regions == frozenset({"flanders"})


# ---- _matches_suffix -------------------------------------------------------------


def test_matches_suffix_standard_accepts_month_name() -> None:
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Mei 2026.pdf", None
    )


def test_matches_suffix_standard_rejects_tier_suffix() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch HV Mei 2026.pdf", None
    )


def test_matches_suffix_hv_accepts_hv() -> None:
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch HV Mei 2026.pdf", "HV"
    )


def test_matches_suffix_hv_rejects_standard() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Mei 2026.pdf", "HV"
    )


def test_matches_suffix_rejects_variable_filename() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Variabel Mei 2026.pdf", None
    )
