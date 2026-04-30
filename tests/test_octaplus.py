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

"""OCTA+ PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text_aligned,
)
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.octaplus import parse_snapshot

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text_aligned((FIX / name).read_bytes(), x_join_threshold=1.0)


def test_octaplus_is_registered() -> None:
    assert "octaplus" in EXTRACTORS
    assert EXTRACTORS["octaplus"].label == "OCTA+"
    contract_ids = {c.id for c in EXTRACTORS["octaplus"].contracts}
    assert "octaplus_fixed" in contract_ids
    assert "octaplus_dynamic" in contract_ids
    assert len(contract_ids) == 7


def test_fixed_wallonia_extracts_meter_rates() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1586)
    assert snap.energy.peak == pytest.approx(0.1867)
    assert snap.energy.offpeak == pytest.approx(0.1377)
    assert snap.energy.exclusive_night == pytest.approx(0.1485)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_fixed_flanders_extracts_meter_rates() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1589)


def test_smart_variable_returns_variable_rates() -> None:
    snap = parse_snapshot(
        "octaplus_smartvariable",
        _text("octaplus_smartvariable_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1516)


def test_dynamic_parses_smr3_formula() -> None:
    # OCTA+ Dynamic prints the consumption formula as prose:
    # "Epex 15' * 1,083 + 4,17". The factor and base must be VAT-adjusted
    # (6% residential) and the base converted from EUR/MWh to EUR/kWh.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    # Literal pinning: a unit-conversion bug (e.g. dropping the *1000
    # EUR/MWh→EUR/kWh divide and the 1.06 VAT) could still pass an
    # `approx(1.083 * 1.06)` style assertion. Keep the derivation in
    # the comment, the expected number in the assertion.
    assert snap.energy.factor == pytest.approx(1.14798)
    assert snap.energy.base == pytest.approx(0.0044202)


def test_dynamic_extracts_injection_formula() -> None:
    # The injection formula sits later in the prose, anchored on
    # "Le prix de votre injection ... Epex 15' * 1 - 13,89 €/MWh".
    # Injection is VAT-exempt so the factor / base are not VAT-adjusted.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)
    assert snap.injection.base == pytest.approx(-0.01389)


def test_federal_taxes_use_first_tier() -> None:
    # OCTA+ tax page renders each character as its own pdfplumber word
    # ("5 ,0 3 2 9 0 ,2 0 4 2"); the aligned helper's gap-aware merge
    # must reassemble the values before we read tier 1 (0-3000 kWh).
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.energy_contribution == pytest.approx(0.002042)


def test_taxes_split_correctly_per_region() -> None:
    wa = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    fl = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
    # Wallonia: green-energy + connection fee.
    assert wa.taxes.wallonia_renewables == pytest.approx(0.03095)
    assert wa.taxes.region_connection_fee == pytest.approx(0.00075)
    assert wa.taxes.flanders_renewables == 0.0
    # Flanders: green-energy + WKK, no connection fee.
    assert fl.taxes.flanders_renewables == pytest.approx((1.166 + 0.430) / 100.0)
    assert fl.taxes.region_connection_fee == 0.0


def test_wallonia_dsos_extract_full_set() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= set(snap.dsos)
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0667)
    assert aieg.transport == pytest.approx(0.0275)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.04)


def test_dynamic_pdf_uses_spaced_dso_label() -> None:
    # The Dynamic card renders "REGIE DE WAVRE" with regular spaces
    # (vs. the Fixed card's "REGIEDEWAVRE"); the label regex tolerates
    # both, so REW is still picked up here.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert "rew" in snap.dsos


def test_flanders_dsos_extract_full_set() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
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
    assert expected <= set(snap.dsos)
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.prosumer_eur_per_kva_year == pytest.approx(54.63)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown OCTA"):
            await EXTRACTORS["octaplus"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_brussels_region_rejected() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="not available in region"):
            await EXTRACTORS["octaplus"].fetch(None, "octaplus_fixed", "brussels")  # type: ignore[arg-type]

    asyncio.run(_run())
