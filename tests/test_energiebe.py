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

"""Fixture-based tests for the energie.be extractor."""

from __future__ import annotations

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.energiebe import parse_snapshot
from tests import fixture_text


def _text() -> str:
    return fixture_text("energiebe_dynamic_jul.pdf", layout=True)


def _snap() -> SupplierSnapshot:
    return parse_snapshot(_text(), "test://energiebe-jul")


def test_energy_is_dynamic_rates() -> None:
    assert isinstance(_snap().energy, DynamicRates)


def test_energy_is_quarter_hourly() -> None:
    """The card bills 'op kwartierbasis' on the EPEX day-ahead 15-minute curve."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True


def test_energy_formula_factor() -> None:
    """(1,04 x Belpex + 0,50) c€/kWh, Belpex in c€/kWh => factor = 1.04 * 1.06.

    energie.be prints Belpex in c€/kWh (not EUR/MWh like Frank), so the factor
    is NOT multiplied by 10.
    """
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.04 * 1.06)


def test_energy_formula_base() -> None:
    """base = 0.50 c€/kWh => 0.005 EUR/kWh, times the 1.06 VAT multiplier."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.base == pytest.approx(0.50 / 100.0 * 1.06)


def test_yearly_fixed_fee_is_already_annual() -> None:
    """Vaste vergoeding is quoted 25 EUR/jaar; carried through unscaled."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(25.0)


def test_dsos_cover_all_eight_fluvius_subareas() -> None:
    assert set(_snap().dsos) == set(FLUVIUS_KEYS)


def test_dso_antwerpen_distribution() -> None:
    """Antwerpen digital meter: normaal 5,35 ct/kWh, excl nacht 4,81 ct/kWh."""
    a = _snap().dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(5.35 / 100.0)
    assert a.distribution_exclusive_night == pytest.approx(4.81 / 100.0)


def test_dso_antwerpen_capacity_and_databeheer() -> None:
    a = _snap().dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_dso_transport_is_zero() -> None:
    """Transport is bundled into distribution on energie.be's card."""
    for overlay in _snap().dsos.values():
        assert overlay.transport == 0.0


def test_dso_wrapped_label_halle_vilvoorde() -> None:
    """The label wraps as 'Fluvius (Halle-\\n<numbers>\\nVilvoorde)'; the row
    numbers still bind (normaal 5,64, cap 59,41)."""
    hv = _snap().dsos["fluvius_halle_vilvoorde"]
    assert hv.distribution_single == pytest.approx(5.64 / 100.0)
    assert hv.capacity_eur_per_kw_year == pytest.approx(59.41)


def test_dso_wrapped_label_midden_vlaanderen() -> None:
    """Midden-Vlaanderen (Intergem key) also wraps across the number row."""
    mv = _snap().dsos["fluvius_intergem"]
    assert mv.distribution_single == pytest.approx(5.28 / 100.0)
    assert mv.capacity_eur_per_kw_year == pytest.approx(53.13)


def test_injection_factor() -> None:
    """(1 x Belpex - 0,98), Belpex in c€/kWh, VAT-exempt => factor = 1.0."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_injection_base() -> None:
    """base = -0.98 c€/kWh => -0.0098 EUR/kWh (no *10, no VAT)."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.base == pytest.approx(-0.98 / 100.0)


def test_injection_current_is_none() -> None:
    """Spot-indexed injection: priced off the live spot, no monthly indicative."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.current is None


def test_taxes_federal_excise() -> None:
    assert _snap().taxes.federal_excise == pytest.approx(5.0329 / 100.0)


def test_taxes_energy_contribution() -> None:
    """'Bijdrage op de Energie' 0,2042 c€/kWh."""
    assert _snap().taxes.energy_contribution == pytest.approx(0.2042 / 100.0)


def test_taxes_flanders_renewables_gsc_plus_wkk() -> None:
    """Residential GSC 1,17 + WKK 0,39 = 1,56 c€/kWh."""
    assert _snap().taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)


def test_taxes_energy_fund_residential_zero() -> None:
    assert _snap().taxes.energy_fund_eur_per_month == pytest.approx(0.0)


def test_taxes_vat_rate_zero() -> None:
    """The energy leg is pre-scaled to VAT-inclusive, so vat_rate stays 0.0."""
    assert _snap().taxes.vat_rate == 0.0


def test_only_residential_block_is_parsed() -> None:
    """The PDF appends a professional block (GSC 1,10 / WKK 0,36, databeheer
    17,85). None of it may leak into the residential snapshot."""
    snap = _snap()
    assert snap.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    for overlay in snap.dsos.values():
        assert overlay.data_management_per_year == pytest.approx(18.92)


def test_dot_decimal_render_matches_comma() -> None:
    # A dot-decimal PDF re-render must extract identical values, not truncate a
    # mandatory value to its integer part as a comma-only regex would.
    comma = _snap()
    dot = parse_snapshot(_text().replace(",", "."), "test://energiebe-jul")
    assert isinstance(comma.energy, DynamicRates)
    assert isinstance(dot.energy, DynamicRates)
    assert dot.energy.factor == pytest.approx(comma.energy.factor)
    assert dot.energy.base == pytest.approx(comma.energy.base)
    assert dot.taxes.federal_excise == pytest.approx(comma.taxes.federal_excise)
    assert dot.injection is not None and comma.injection is not None
    assert dot.injection.base == pytest.approx(comma.injection.base)


def test_missing_fee_is_fatal() -> None:
    # The vaste vergoeding standing charge is mandatory; a miss must raise.
    text = _text().replace("Vaste vergoeding", "XXX")
    with pytest.raises(ExtractorError, match="vaste vergoeding"):
        parse_snapshot(text, "test://energiebe-jul")


def test_missing_gsc_wkk_is_fatal() -> None:
    # energie.be dynamic is Flanders-only, so GSC + WKK are mandatory; a miss
    # must raise rather than silently zero the renewables levy.
    text = _text().replace("GSC", "XXX").replace("WKK", "YYY")
    with pytest.raises(ExtractorError, match="GSC/WKK"):
        parse_snapshot(text, "test://energiebe-jul")


def test_missing_injection_is_fatal() -> None:
    # Every dynamic card prints the terugleveringsvergoeding; a miss must raise
    # rather than silently zero the solar feed-in credit.
    text = _text().replace("injectievergoeding", "XXX")
    with pytest.raises(ExtractorError, match="injection formula"):
        parse_snapshot(text, "test://energiebe-jul")


def test_supplier_and_contract_metadata() -> None:
    snap = _snap()
    assert snap.supplier == "energiebe"
    assert snap.contract == "energiebe_dynamic"
    assert snap.publication_label == "juli 2026"


# ---- registration ---------------------------------------------------------------


def test_energiebe_is_registered() -> None:
    assert "energiebe" in EXTRACTORS
    assert EXTRACTORS["energiebe"].label == "energie.be"
    contract_ids = {c.id for c in EXTRACTORS["energiebe"].contracts}
    assert contract_ids == {"energiebe_dynamic"}


def test_contract_is_dynamic_and_flanders_only() -> None:
    for c in EXTRACTORS["energiebe"].contracts:
        assert c.kind == "dynamic"
        assert c.regions == frozenset({"flanders"})
