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

"""Bolt PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text_layout,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.bolt import parse_snapshot

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text_layout((FIX / name).read_bytes())


def test_bolt_is_registered() -> None:
    assert "bolt" in EXTRACTORS
    assert EXTRACTORS["bolt"].label == "Bolt"
    contract_ids = {c.id for c in EXTRACTORS["bolt"].contracts}
    assert "bolt_fix" in contract_ids
    assert "bolt_variable" in contract_ids
    assert len(contract_ids) == 6


def test_fix_yearly_fee_is_monthly_x_12() -> None:
    # Bolt prints the platform fee per month (€10,99/mois). The
    # integration's yearly_fixed_fee carries the EUR/year amount, so the
    # parser multiplies by 12.
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "wallonia")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(10.99 * 12.0)


def test_fix_extracts_consumption_rates() -> None:
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "wallonia")
    assert isinstance(snap.energy, FixedRates)
    # Bolt Fix prints all four meter rates as 16,71 c€/kWh.
    assert snap.energy.single == pytest.approx(0.1671)
    assert snap.energy.peak == pytest.approx(0.1671)
    assert snap.energy.offpeak == pytest.approx(0.1671)
    assert snap.energy.exclusive_night == pytest.approx(0.1671)


def test_variable_uses_current_monthly_not_annual_estimate() -> None:
    # The bihoraire block lists the annual estimate first
    # (15,20 / 15,20) then the current monthly (14,56 / 12,09). Anchor
    # on the trailing 'Jour Nuit' header to skip the annual values.
    snap = parse_snapshot("bolt_variable", _text("bolt_variable.pdf"), "wallonia")
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1325)
    assert snap.energy.peak == pytest.approx(0.1456)
    assert snap.energy.offpeak == pytest.approx(0.1209)
    assert snap.energy.exclusive_night == pytest.approx(0.1209)


def test_taxes_split_correctly_per_region() -> None:
    fl = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "flanders")
    wa = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "wallonia")
    bx = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "brussels")
    # Federal excise + energy contribution are nationwide.
    assert fl.taxes.federal_excise == pytest.approx(0.050329)
    assert fl.taxes.energy_contribution == pytest.approx(0.002042)
    # Flanders renewables: certificats verts + WKK. Bolt's WKK row prints
    # a single-digit footnote ref before the value ('WKK (c€/kWh) 8 0,39')
    # which the parser must skip.
    assert fl.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    assert fl.taxes.region_connection_fee == 0.0
    # Wallonia: green-energy + connection fee.
    assert wa.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert wa.taxes.region_connection_fee == pytest.approx(0.00075)
    # Brussels: green-energy only.
    assert bx.taxes.brussels_renewables == pytest.approx(0.0269)
    assert bx.taxes.region_connection_fee == 0.0


def test_wallonia_dso_handles_vertical_layout() -> None:
    # pdfplumber renders Bolt's Wallonia rows with each value on its
    # own line: "AIEG\n 10,58\n 11,77\n 6,38\n ...". The regex uses
    # `\s+` (which matches newlines) between values to handle this.
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "wallonia")
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1058)
    assert aieg.distribution_peak == pytest.approx(0.1177)
    assert aieg.distribution_offpeak == pytest.approx(0.0638)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_resa_is_cheaper_than_rew_after_label_swap() -> None:
    # Bolt's PDF renders the Liege (RESA / TECTEO) and Wavre (REW /
    # Régie de Wavre) rows under swapped labels in pdfplumber's text
    # extraction; bolt.py compensates with an inverted dict. Across
    # every other supplier in the registry, RESA's distribution_single
    # is consistently lower than REW's. If a future Bolt PDF or
    # pdfplumber release fixes the upstream layout silently, the swap
    # would invert correct pricing — this assertion catches that.
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "wallonia")
    assert snap.dsos["resa"].distribution_single < snap.dsos["rew"].distribution_single


def test_flanders_dso_includes_transport_in_distribution() -> None:
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "flanders")
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_brussels_extracts_sibelga() -> None:
    snap = parse_snapshot("bolt_fix", _text("bolt_fix.pdf"), "brussels")
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Bolt contract"):
            await EXTRACTORS["bolt"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())
