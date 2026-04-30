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

"""Luminus PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import extract_pdf_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.luminus import parse_snapshot

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text((FIX / name).read_bytes())


def _dynamic_w() -> object:
    return parse_snapshot("luminus_dynamic", _text("luminus_dynamic_w.pdf"), "wallonia")


def _dynamic_v() -> object:
    return parse_snapshot("luminus_dynamic", _text("luminus_dynamic_v.pdf"), "flanders")


def _comfy_w() -> object:
    return parse_snapshot("luminus_comfy", _text("luminus_comfy_w.pdf"), "wallonia")


def _comfyflex_v() -> object:
    return parse_snapshot(
        "luminus_comfyflex", _text("luminus_comfyflex_v.pdf"), "flanders"
    )


def test_luminus_is_registered() -> None:
    assert "luminus" in EXTRACTORS
    assert EXTRACTORS["luminus"].label == "Luminus"
    contract_ids = {c.id for c in EXTRACTORS["luminus"].contracts}
    assert "luminus_comfy" in contract_ids
    assert "luminus_comfyflex" in contract_ids
    assert "luminus_maxxfix" in contract_ids
    assert "luminus_dynamic" in contract_ids


def test_dynamic_wallonia_extracts_consumption_formula() -> None:
    snap = _dynamic_w()
    assert isinstance(snap.energy, DynamicRates)
    # PDF prints "hors TVA  0,1019 x Belpex H + 2,4591" at 6% VAT.
    # Literal pinning (vs `0.1019 * 1.06 * 10`) so a unit-conversion
    # swap of 1.06 ⇄ 10 can't cancel out and pass the assertion.
    assert snap.energy.factor == pytest.approx(1.08014)
    assert snap.energy.base == pytest.approx(0.02606646)
    assert snap.energy.yearly_fixed_fee == pytest.approx(75.0)


def test_dynamic_flanders_has_a_different_base() -> None:
    # Luminus's hourly formula has a region-specific base; Flanders is
    # 50 cents below Wallonia. This is the one fact that motivates the
    # whole region-aware fetcher signature - if we ever merged the two
    # regions into one snapshot, one of them would silently get the
    # wrong base.
    w = _dynamic_w()
    v = _dynamic_v()
    assert isinstance(w.energy, DynamicRates)
    assert isinstance(v.energy, DynamicRates)
    assert w.energy.factor == pytest.approx(v.energy.factor)
    assert w.energy.base != v.energy.base
    assert v.energy.base == pytest.approx(0.02076646)


def test_dynamic_extracts_injection_formula_with_negative_base() -> None:
    snap = _dynamic_w()
    inj = snap.injection
    assert inj is not None
    # PDF injection: hors TVA  0,1019 x Belpex H - 1,2737 (VAT-exempt).
    assert inj.factor == pytest.approx(1.019)
    assert inj.base == pytest.approx(-0.012737)


def test_comfy_wallonia_fixed_rates_and_dso() -> None:
    snap = _comfy_w()
    assert isinstance(snap.energy, FixedRates)
    # PDF:  20,38   23,74   17,71   17,71  (mono / pleines / creuses / excl_nuit).
    assert snap.energy.single == pytest.approx(0.2038)
    assert snap.energy.peak == pytest.approx(0.2374)
    assert snap.energy.offpeak == pytest.approx(0.1771)
    assert snap.energy.exclusive_night == pytest.approx(0.1771)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # All five Wallonia DSOs and a sanity-check on the AIEG row.
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_comfyflex_flanders_uses_current_monthly_not_annual_estimate() -> None:
    # ComfyFlex prints two energy rows: 'Énergie fournie' (current month)
    # and 'Estimation annuelle de l'énergie fournie'. Take the first or
    # we'd over- / under-bill users by ~5% in a moving market.
    snap = _comfyflex_v()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1558)
    assert snap.energy.peak == pytest.approx(0.1684)
    assert snap.energy.offpeak == pytest.approx(0.1366)


def test_flanders_dynamic_dso_table_is_smaller_than_static() -> None:
    # Dynamic (SMR3) cards print 4 numbers per Fluvius row (digital
    # meter only). Static cards add 4 more (analog + prosumer). The
    # parser handles both.
    dyn = _dynamic_v()
    flex = _comfyflex_v()
    antwerpen_dyn = dyn.dsos["fluvius_antwerpen"]
    antwerpen_static = flex.dsos["fluvius_antwerpen"]
    # Distribution + capacity should agree to within 2 decimals
    # (dynamic is rounded, static prints 4-decimal).
    assert antwerpen_dyn.distribution_single == pytest.approx(0.0535)
    assert antwerpen_static.distribution_single == pytest.approx(0.0535)
    # Only the static card carries a prosumer rate.
    assert antwerpen_dyn.prosumer_eur_per_kva_year is None
    assert antwerpen_static.prosumer_eur_per_kva_year == pytest.approx(54.63)


def test_taxes_split_correctly_per_region() -> None:
    w = _dynamic_w()
    v = _dynamic_v()
    # Federal excise is uniform across regions.
    assert w.taxes.federal_excise == pytest.approx(0.050329)
    assert v.taxes.federal_excise == pytest.approx(0.050329)
    # Energy contribution is uniform too.
    assert w.taxes.energy_contribution == pytest.approx(0.002042)
    assert v.taxes.energy_contribution == pytest.approx(0.002042)
    # Wallonia: green energy 3,03 c€/kWh, no Flanders renewables.
    assert w.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert w.taxes.flanders_renewables == 0.0
    # Wallonia: connection fee 0,075 c€/kWh.
    assert w.taxes.region_connection_fee == pytest.approx(0.00075)
    # Flanders: green 1,17 + cogen 0,39 = 1,56 c€/kWh, no connection fee.
    assert v.taxes.flanders_renewables == pytest.approx(0.0156)
    assert v.taxes.wallonia_renewables == 0.0
    assert v.taxes.region_connection_fee == 0.0
    # Energy fund is BTR (résidentiel) which is '-' for residential users
    # in both regions today.
    assert w.taxes.energy_fund_eur_per_month == 0.0
    assert v.taxes.energy_fund_eur_per_month == 0.0


def test_brussels_is_unsupported() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="not available in region"):
            await EXTRACTORS["luminus"].fetch(None, "luminus_comfy", "brussels")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Luminus contract"):
            await EXTRACTORS["luminus"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())
