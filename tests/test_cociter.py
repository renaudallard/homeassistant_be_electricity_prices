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

"""Cociter PDF extractor tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import extract_pdf_text
from custom_components.be_electricity_prices.providers.base import ExtractorError
from custom_components.be_electricity_prices.providers.cociter import parse_energy_block

FIX = Path(__file__).parent / "fixtures"


def test_cociter_is_registered() -> None:
    assert "cociter" in EXTRACTORS
    assert EXTRACTORS["cociter"].label == "Cociter"
    contract_ids = {c.id for c in EXTRACTORS["cociter"].contracts}
    assert contract_ids == {"cociter_variable"}


def test_parse_energy_block_extracts_rate_fee_and_renewables() -> None:
    text = extract_pdf_text((FIX / "cociter_2023_11.pdf").read_bytes())
    energy, renewables = parse_energy_block(text)
    # First "Coûts de l’énergie" line in the card is the single-meter rate.
    assert energy.current == pytest.approx(0.120888)
    assert energy.yearly_fixed_fee == pytest.approx(53.0)
    assert energy.formula is not None and "BELIX" in energy.formula
    # "Contribution énergie renouvelable ... 2,953160 c€/kWh"
    assert renewables == pytest.approx(0.0295316)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Cociter contract"):
            await EXTRACTORS["cociter"].fetch(None, "bogus")  # type: ignore[arg-type]

    asyncio.run(_run())
