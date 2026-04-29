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

"""Cociter PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import extract_pdf_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.cociter import parse_snapshot

FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text((FIX / name).read_bytes())


def test_cociter_is_registered() -> None:
    assert "cociter" in EXTRACTORS
    assert EXTRACTORS["cociter"].label == "Cociter"
    contract_ids = {c.id for c in EXTRACTORS["cociter"].contracts}
    assert contract_ids == {"cociter_variable", "cociter_dynamic"}


def test_variable_extracts_indicative_rates() -> None:
    snap = parse_snapshot(
        _text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert isinstance(snap.energy, VariableRates)
    # Indicative rates printed in the PDF (TVAC).
    assert snap.energy.current == pytest.approx(0.126625)
    assert snap.energy.peak == pytest.approx(0.136442)
    assert snap.energy.offpeak == pytest.approx(0.116808)
    assert snap.energy.exclusive_night == pytest.approx(0.116808)
    assert snap.energy.yearly_fixed_fee == pytest.approx(53.0)
    assert snap.energy.formula is not None and "BELIX" in snap.energy.formula


def test_variable_extracts_dso_overlay() -> None:
    snap = parse_snapshot(
        _text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274252)
    assert aieg.data_management_per_year == pytest.approx(19.49)


def test_variable_extracts_taxes() -> None:
    snap = parse_snapshot(
        _text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.00204167)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    # Cociter only operates in Wallonia.
    assert snap.taxes.wallonia_renewables == pytest.approx(0.02968)
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot(
        _text("cociter_dyn_2604.pdf"),
        "cociter_dynamic",
        "test://dyn",
        "2026-04",
    )
    assert isinstance(snap.energy, DynamicRates)
    # PDF: (0.103 x QUARTER_HOURLY_BELPEX_eur_per_mwh + 3) x 1.06 c€/kWh
    # -> factor = 0.103 * 10.6 = 1.0918, base = 3 * 1.06 / 100 = 0.0318
    assert snap.energy.factor == pytest.approx(0.103 * 10.6, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.0318, rel=1e-4)
    # At spot = 100 EUR/MWh = 0.10 EUR/kWh, all-in energy is ~0.14098 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.14098)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Cociter contract"):
            await EXTRACTORS["cociter"].fetch(None, "bogus")  # type: ignore[arg-type]

    asyncio.run(_run())
