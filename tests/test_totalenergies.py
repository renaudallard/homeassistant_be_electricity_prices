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

"""TotalEnergies PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text_layout,
)
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.totalenergies import (
    parse_snapshot,
)

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text_layout((FIX / name).read_bytes())


def test_totalenergies_is_registered() -> None:
    assert "totalenergies" in EXTRACTORS
    assert EXTRACTORS["totalenergies"].label == "TotalEnergies"
    contract_ids = {c.id for c in EXTRACTORS["totalenergies"].contracts}
    assert "totalenergies_mydynamic" in contract_ids
    assert "totalenergies_mycomfort" in contract_ids
    assert "totalenergies_mycomfort_fixed" in contract_ids
    assert len(contract_ids) == 9


def test_dynamic_wallonia_extracts_consumption_formula() -> None:
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    # PDF: 0.1034 * BELPEXH + 1.75 (HTVA, 6% VAT).
    assert snap.energy.factor == pytest.approx(0.1034 * 1.06 * 10.0)
    assert snap.energy.base == pytest.approx(1.75 * 1.06 / 100.0)
    assert snap.energy.yearly_fixed_fee == pytest.approx(90.0)


def test_dynamic_brussels_pulls_base_from_split_layout() -> None:
    # Brussels Dynamic prints the formula across two lines:
    #   "0.1034 * BELPEXH + 0.1034 * BELPEXH + ... + Formule tarifaire"
    #   "3.85 3.85 3.85 3.75"
    # The Wallonia/Flanders pattern (formula and base on one line) does
    # NOT match here. The parser must fall back to picking the base from
    # the line right after "Formule tarifaire".
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_b.pdf"), "brussels"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(0.1034 * 1.06 * 10.0)
    assert snap.energy.base == pytest.approx(3.85 * 1.06 / 100.0)


def test_dynamic_injection_formula_uses_distinct_anchor() -> None:
    # Both consumption and injection use the BELPEX formula, but the
    # injection block always prints cleanly. Anchor on the "Injection**"
    # block so the consumption formula above is never picked up by
    # mistake.
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_w.pdf"), "wallonia"
    )
    inj = snap.injection
    assert inj is not None
    # PDF: 0.1 * BELPEXH - 1.3 (HTVA, residential injection is VAT-exempt).
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.013)


def test_mycomfort_fixed_wallonia_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "totalenergies_mycomfort_fixed",
        _text("totalenergies_mycomfort_fixed_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 18.41 / 19.66 / 17.32 / 17.13 c€/kWh.
    assert snap.energy.single == pytest.approx(0.1841)
    assert snap.energy.peak == pytest.approx(0.1966)
    assert snap.energy.offpeak == pytest.approx(0.1732)
    assert snap.energy.exclusive_night == pytest.approx(0.1713)
    assert snap.energy.yearly_fixed_fee == pytest.approx(90.0)


def test_mycomfort_variable_flanders_handles_tarif_mensuel_label() -> None:
    # Variable cards put "Tarif mensuel" BETWEEN the "Consommation**"
    # label and the actual values; static cards put it AFTER the values.
    # The parser must accept both layouts.
    snap = parse_snapshot(
        "totalenergies_mycomfort", _text("totalenergies_mycomfort_v.pdf"), "flanders"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1562)
    assert snap.energy.peak == pytest.approx(0.1696)
    assert snap.energy.offpeak == pytest.approx(0.1447)


def test_brussels_extracts_sibelga_row() -> None:
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_b.pdf"), "brussels"
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    assert sibelga.data_management_per_year == pytest.approx(14.73)


def test_wallonia_dso_carries_full_row() -> None:
    # TotalEnergies's Wallonia rows have 12 numbers; the parser pulls
    # mono / jour / nuit (cols 0-2), data_mgmt (col 7), transport (col 8)
    # and prosumer (col 9) - the IMPACT triplet (cols 4-6) and capacity
    # cols 10-11 aren't surfaced.
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_w.pdf"), "wallonia"
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_flanders_dso_includes_transport_in_distribution() -> None:
    # Flanders rows print distribution that already include transport
    # (same convention as Engie/Luminus/Mega Flanders), so transport=0
    # and the c€/kWh value lands in distribution_single.
    snap = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_v.pdf"), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.data_management_per_year == pytest.approx(18.92)


def test_taxes_split_correctly_per_region() -> None:
    w = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_w.pdf"), "wallonia"
    )
    v = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_v.pdf"), "flanders"
    )
    b = parse_snapshot(
        "totalenergies_mydynamic", _text("totalenergies_dynamic_b.pdf"), "brussels"
    )
    # Federal excise (rounded to 2 decimals on TotalEnergies cards).
    assert w.taxes.federal_excise == pytest.approx(0.0503)
    assert v.taxes.federal_excise == pytest.approx(0.0503)
    assert b.taxes.federal_excise == pytest.approx(0.0503)
    # Wallonia: green energy + connection fee.
    assert w.taxes.wallonia_renewables == pytest.approx(0.032)
    assert w.taxes.region_connection_fee == pytest.approx(0.0007)
    # Flanders: green + cogen merged on one line.
    assert v.taxes.flanders_renewables == pytest.approx(0.0157)
    assert v.taxes.region_connection_fee == 0.0
    # Brussels: brussels_renewables only.
    assert b.taxes.brussels_renewables == pytest.approx(0.0285)
    assert b.taxes.flanders_renewables == 0.0
    assert b.taxes.wallonia_renewables == 0.0


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown TotalEnergies contract"):
            await EXTRACTORS["totalenergies"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_unknown_region_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown region"):
            await EXTRACTORS["totalenergies"].fetch(  # type: ignore[arg-type]
                None, "totalenergies_mydynamic", "atlantis"
            )

    asyncio.run(_run())
