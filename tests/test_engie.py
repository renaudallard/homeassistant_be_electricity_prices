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

"""Engie PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import extract_pdf_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.engie import parse_snapshot

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text((FIX / name).read_bytes())


def _dynamic_three_regions() -> dict[str, str]:
    return {
        REGION_FLANDERS: _text("engie_dynamic_v.pdf"),
        REGION_WALLONIA: _text("engie_dynamic_w.pdf"),
        REGION_BRUSSELS: _text("engie_dynamic_b.pdf"),
    }


def test_engie_is_registered() -> None:
    assert "engie" in EXTRACTORS
    assert EXTRACTORS["engie"].label == "Engie"
    contract_ids = {c.id for c in EXTRACTORS["engie"].contracts}
    assert "engie_dynamic" in contract_ids
    assert "engie_easy_fixed" in contract_ids
    assert "engie_easy_variable" in contract_ids


def test_dynamic_extracts_consumption_formula() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    assert isinstance(snap.energy, DynamicRates)
    # PDF: hors TVA  0,8702 + (0,1039 x eSpot_15) at 6% VAT.
    # spot in EUR/kWh, factor = 0.1039 * 1.06 * 10 = 1.10134
    # base   = 0.8702 * 1.06 / 100 = 0.00922412
    assert snap.energy.factor == pytest.approx(0.1039 * 1.06 * 10.0)
    assert snap.energy.base == pytest.approx(0.8702 * 1.06 / 100.0)
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.7)


def test_dynamic_extracts_injection_formula() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    inj = snap.injection
    assert inj is not None
    # PDF injection: hors TVA  -1,3135 + (0,1000 x eSpot_15)
    # Residential injection is VAT-exempt so factor stays at 0.1000 * 10.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.013135)
    # Indicative monthly rate also surfaced (from the Injection(3) row).
    assert inj.current == pytest.approx(0.09136)


def test_dynamic_merges_dsos_from_every_region() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    keys = set(snap.dsos)
    # 8 Fluvius sub-areas + 5 Wallonia + 1 Brussels = 14.
    assert {"fluvius_antwerpen", "fluvius_west", "fluvius_zenne_dijle"} <= keys
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= keys
    assert "sibelga" in keys


def test_dynamic_flanders_dso_includes_transport_in_distribution() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    antwerpen = snap.dsos["fluvius_antwerpen"]
    # Engie's V table prints distribution rates that already include the
    # Elia transport - "incluant déjà les coûts de transport". So the
    # parser sets transport=0 and rolls everything into distribution_single.
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535329)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.3679)


def test_dynamic_wallonia_dso_has_separate_transport_no_prosumer() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    # Dynamic SMR3 contracts have no compensation regime - the prosumer
    # column is replaced by IMPACT (PIC/MEDIUM/ECO) on the Wallonia card.
    assert aieg.prosumer_eur_per_kva_year is None


def test_dynamic_brussels_extracts_sibelga() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_peak == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    assert sibelga.data_management_per_year == pytest.approx(14.73)


def test_dynamic_extracts_taxes_for_every_region() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    # Federal: same value across regions, so any one PDF is canonical.
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    # Regional renewables: each pulled from its own region's PDF.
    assert snap.taxes.flanders_renewables == pytest.approx(0.01582)
    assert snap.taxes.wallonia_renewables == pytest.approx(0.03095)
    assert snap.taxes.brussels_renewables == pytest.approx(0.02652)
    # Wallonia connection fee + Flanders energy fund (with-domicile = 0).
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.energy_fund_eur_per_month == 0.0
    # Engie's PDF prints 6% VAT inclusive, so the snapshot is post-VAT.
    assert snap.taxes.vat_rate == 0.0


def test_easy_fixed_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "engie_easy_fixed",
        {REGION_FLANDERS: _text("engie_easy_fixed_v.pdf")},
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 18,938  20,197  17,176  17,176  (mono / day / night / excl_night).
    assert snap.energy.single == pytest.approx(0.18938)
    assert snap.energy.peak == pytest.approx(0.20197)
    assert snap.energy.offpeak == pytest.approx(0.17176)
    assert snap.energy.exclusive_night == pytest.approx(0.17176)
    assert snap.energy.yearly_fixed_fee == pytest.approx(69.0)
    # Fixed contracts have an indicative monthly injection price but no
    # formula.
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.03217)
    assert snap.injection.factor is None and snap.injection.base is None


def test_empower_variable_skips_flextime_tiers() -> None:
    # Empower Variable's Consommation row has 7 price columns: standard
    # mono / bi-pleines / bi-creuses, then three Flextime variants
    # (heures pleines / creuses / super-creuses), then exclusive-night.
    # The integration's pricing model only carries mono + bi + excl_night,
    # so the Flextime middle three are skipped on purpose.
    snap = parse_snapshot(
        "engie_empower_variable",
        {REGION_FLANDERS: _text("engie_empower_variable_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.13775)
    assert snap.energy.peak == pytest.approx(0.15058)
    assert snap.energy.offpeak == pytest.approx(0.11625)
    # Last price column on the 8-number row is exclusive-night, NOT the
    # Flextime super-creuses (9,796) which is the cheapest visible value.
    assert snap.energy.exclusive_night == pytest.approx(0.12460)


def test_empty_house_is_mono_only() -> None:
    # The 'Tarif bâtiment vide' card has a single rate (no bihoraire, no
    # exclusive-night) because vacant homes don't run time-of-use loads.
    # The parser must accept the 1-price-+-1-renewables row layout
    # instead of the standard 4-prices-+-1-renewables.
    snap = parse_snapshot(
        "engie_empty_house",
        {REGION_FLANDERS: _text("engie_empty_house_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.24505)
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    assert snap.taxes.flanders_renewables == pytest.approx(0.01582)


def test_easy_variable_uses_monthly_not_annual_estimate() -> None:
    snap = parse_snapshot(
        "engie_easy_variable",
        {REGION_FLANDERS: _text("engie_easy_indexed_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    # The Variable PDF prints two Consommation rows: 'Prix mensuels' (the
    # rate Engie is actually charging this month) and 'Prix annuels
    # estimés'. The integration must take the first - the second would
    # over-bill users by ~7% in a falling-price month.
    assert snap.energy.current == pytest.approx(0.16072)
    assert snap.energy.peak == pytest.approx(0.16992)
    assert snap.energy.offpeak == pytest.approx(0.14335)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Engie contract"):
            await EXTRACTORS["engie"].fetch(None, "bogus")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_parse_snapshot_with_partial_regions_still_works() -> None:
    # If Engie's API is down for one region, the coordinator should still
    # build a snapshot from the others. parse_snapshot accepts whatever
    # the caller provides, so a single-region map yields a working
    # snapshot with only that region's DSOs.
    snap = parse_snapshot(
        "engie_dynamic",
        {REGION_BRUSSELS: _text("engie_dynamic_b.pdf")},
    )
    assert set(snap.dsos) == {"sibelga"}
    assert snap.taxes.brussels_renewables == pytest.approx(0.02652)
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.wallonia_renewables == 0.0
