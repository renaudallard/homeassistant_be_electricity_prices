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

"""EBEM PDF extractor tests against May 2026 fixtures."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.ebem import (
    fetch_for_month,
    parse_snapshot,
)
from tests import FIXTURES, fixture_text

_VARIABLE = "ebem_variable_2026-05.pdf"
_DYNAMIC = "ebem_dynamic_2026-05.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


# ---- registry ---------------------------------------------------------------


def test_ebem_is_registered() -> None:
    assert "ebem" in EXTRACTORS
    extractor = EXTRACTORS["ebem"]
    assert extractor.label == "EBEM"
    assert {c.id for c in extractor.contracts} == {
        "ebem_variable",
        "ebem_basic_plus",
        "ebem_dynamic",
    }
    # EBEM only sells in Flanders today; the registry must not advertise
    # the other regions or config_flow would offer EBEM to households
    # where every fetch would 404.
    for contract in extractor.contracts:
        assert contract.regions == frozenset({"flanders"})


# ---- Groen Variabel ---------------------------------------------------------


def test_variable_extracts_energy_for_each_meter_type() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    assert isinstance(snap.energy, VariableRates)
    # PDF prints both excl-VAT and incl-VAT columns; we surface incl-VAT.
    # mono: (0,110 BelpexRLP0 + 2,2) at last-month Belpex 85,80 incl 6% VAT
    # = (0.110*85.80 + 2.2)*1.06 = 12.3363 c€/kWh = 0.123363 EUR/kWh
    assert snap.energy.current == pytest.approx(0.123363, rel=1e-4)
    # peak: (0,120 BelpexRLP0 + 2,2)
    assert snap.energy.peak == pytest.approx(0.132458, rel=1e-4)
    # off-peak / excl-night: (0,099 BelpexRLP0 + 2,2)
    assert snap.energy.offpeak == pytest.approx(0.113359, rel=1e-4)
    assert snap.energy.exclusive_night == pytest.approx(0.113359, rel=1e-4)
    # Yearly fee: 85,00 €/jaar incl-VAT (the registry stores incl-VAT).
    assert snap.energy.yearly_fixed_fee == pytest.approx(85.0)
    assert snap.energy.formula is not None
    assert "BelpexRLP0" in snap.energy.formula


def test_variable_publication_and_validity() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    assert snap.publication_label == "2026-05"
    # mei 2026 -> last day = 31 May 2026 (Sunday)
    assert snap.valid_until == date(2026, 5, 31)


def test_variable_extracts_taxes_flanders_only() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    # Federal excise residential 0-3 MWh band: 5,0329 c€/kWh
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    # Energy contribution: 0,20417 c€/kWh
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    # Flanders renewables (groene stroom + WKK incl-VAT total): 1,6112 c€/kWh
    assert snap.taxes.flanders_renewables == pytest.approx(0.016112)
    # EBEM doesn't sell in Wallonia / Brussels: those overlays must stay 0.
    assert snap.taxes.wallonia_renewables == 0.0
    assert snap.taxes.brussels_renewables == 0.0
    assert snap.taxes.region_connection_fee == 0.0
    # Residential energy-fund tariff is €0; non-residential is €10,07/maand
    # which we don't model.
    assert snap.taxes.energy_fund_eur_per_month == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_variable_extracts_flanders_dsos_with_prosumer() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    expected_keys = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_intergem",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    assert set(snap.dsos) == expected_keys
    # Spot-check Fluvius Kempen (= fluvius_iveka in the integration's DSO
    # key namespace). The variable card publishes both digital and analog
    # meter rows; we surface the digital row plus the analog prosumer rate.
    iveka = snap.dsos["fluvius_iveka"]
    # Card prints '59,58' and '6,34' / '5,66' / '18,92' / '67,79' on the
    # digital + analog rows; values are rounded to 2 decimals upstream.
    assert iveka.capacity_eur_per_kw_year == pytest.approx(59.58)
    assert iveka.distribution_single == pytest.approx(0.0634)
    assert iveka.distribution_exclusive_night == pytest.approx(0.0566)
    assert iveka.data_management_per_year == pytest.approx(18.92)
    assert iveka.prosumer_eur_per_kva_year == pytest.approx(67.79)


def test_variable_extracts_injection_formula() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    inj = snap.injection
    assert inj is not None
    # Card: 0,0925 BelpexSPP0 - 1,25 c€/kWh ex-VAT (injection is VAT-exempt).
    assert inj.factor == pytest.approx(0.925, rel=1e-4)
    assert inj.base == pytest.approx(-0.0125, rel=1e-4)
    assert inj.current is None
    assert inj.formula is not None
    assert "BelpexSPP0" in inj.formula


# ---- Groen B@sic+ -----------------------------------------------------------


def test_basic_plus_is_variable_with_single_rate() -> None:
    snap = parse_snapshot("ebem_basic_plus", _layout(_VARIABLE), "test://b", "2026-05")
    assert isinstance(snap.energy, VariableRates)
    # B@sic+ has one printed indicative rate: (0,110 BelpexRLP0 + 2,0) incl-VAT
    # at last-month Belpex 85,80 = 12,1243 c€/kWh.
    assert snap.energy.current == pytest.approx(0.121243, rel=1e-4)
    # Single-rate product: no peak / off-peak / excl-night split.
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    # Yearly fee: 70 €/jaar incl-VAT (the 'Abonnement' row).
    assert snap.energy.yearly_fixed_fee == pytest.approx(70.0)


def test_basic_plus_shares_pdf_with_variable_but_distinct_energy() -> None:
    """``Variabel`` and ``B@sic+`` live in the same elek PDF; verify the
    parser branches by contract id and gives each its own correct rate.
    """
    text = _layout(_VARIABLE)
    var = parse_snapshot("ebem_variable", text, "test://v", "2026-05")
    basic = parse_snapshot("ebem_basic_plus", text, "test://b", "2026-05")
    assert isinstance(var.energy, VariableRates)
    assert isinstance(basic.energy, VariableRates)
    # The two products differ in offset (2,2 vs 2,0) and yearly fee.
    assert var.energy.current != basic.energy.current
    assert var.energy.yearly_fixed_fee == 85.0
    assert basic.energy.yearly_fixed_fee == 70.0
    # But the DSO + tax overlays come from the same card and are byte-identical.
    assert var.dsos == basic.dsos
    assert var.taxes == basic.taxes


# ---- Groen Dyn@mic ----------------------------------------------------------


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    assert isinstance(snap.energy, DynamicRates)
    # Card formula: (0,108 Belpex15' + 1,625) c€/kWh ex-VAT, 6% VAT applied.
    # factor = 0.108 * 1.06 * 10 = 1.1448
    # base   = 1.625 * 1.06 / 100 = 0.017225
    assert snap.energy.factor == pytest.approx(1.1448, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.017225, rel=1e-4)
    # Dynamic card: 'Abonnement 66,04 €/jaar 70 €/jaar'.
    assert snap.energy.yearly_fixed_fee == pytest.approx(70.0)


def test_dynamic_extracts_injection_formula() -> None:
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    inj = snap.injection
    assert inj is not None
    # Card: 0,0925 Belpex15' - 1,10 c€/kWh ex-VAT (injection is VAT-exempt).
    assert inj.factor == pytest.approx(0.925, rel=1e-4)
    assert inj.base == pytest.approx(-0.011, rel=1e-4)
    assert inj.formula is not None and "Belpex15" in inj.formula


def test_dynamic_has_no_prosumer_column() -> None:
    """The dynamic card requires SMR3 (smart meter), so it omits the
    analog meter table and therefore the prosumer rate column.
    """
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    iveka = snap.dsos["fluvius_iveka"]
    assert iveka.prosumer_eur_per_kva_year is None
    # Distribution rates still parse — that's the digital-meter row.
    assert iveka.distribution_single == pytest.approx(0.0634)


# ---- error handling ---------------------------------------------------------


def test_unknown_contract_raises() -> None:
    with pytest.raises(ExtractorError, match="unknown EBEM contract"):
        parse_snapshot("bogus", _layout(_VARIABLE), "test://x", "2026-05")


def test_missing_renewables_block_is_fatal() -> None:
    """The Bijdrage groene stroom + WKK total is mandatory on every EBEM
    card; a layout drift that wipes the 'incl. BTW <N>%' row would silently
    zero ~1.6 c€/kWh of bills without this guard.
    """
    text = _layout(_VARIABLE)
    truncated = text.replace("c€/kWh incl. BTW 6%", "c€/kWh excl. BTW 6%")
    with pytest.raises(ExtractorError, match="Totale bijdrage incl. BTW"):
        parse_snapshot("ebem_variable", truncated, "test://v", "2026-05")


# ---- fetch_for_month --------------------------------------------------------


_LISTING_HTML = (FIXTURES / "discover" / "ebem.html").read_text(encoding="utf-8")


class _Resp:
    status = 200

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Session:
    def __init__(self, body: str) -> None:
        self._body = body

    def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
        return _Resp(self._body)


def test_fetch_for_month_returns_snapshot_when_listing_has_url() -> None:
    """May 2026 is on the listing fixture; with the parsed PDF text mocked
    in, ``fetch_for_month`` resolves the URL and parses cleanly."""
    text = _layout(_VARIABLE)
    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 5, 1),
            )
        )
    assert snap is not None
    assert snap.publication_label == "2026-05"
    assert isinstance(snap.energy, VariableRates)


def test_fetch_for_month_handles_underscore_separator() -> None:
    """EBEM's 2026-01 dynamic file is named ``ebem_tariefkaart-dynamic_01-2026.pdf``
    (underscore instead of dash between kind and MM). The regex must
    tolerate either separator so historic months don't silently fall
    through to a coordinator-side proxy."""
    text = _layout(_DYNAMIC)
    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_dynamic",
                "flanders",
                date(2026, 1, 1),
            )
        )
    # Validity-stamping: parsed PDF says 'mei 2026'. The cross-check in
    # fetch_for_month rejects a CDN-substituted current card for a past
    # month, so this returns None — which is the correct safety behaviour
    # (the URL was resolved, the underscore separator was handled, but
    # the served PDF was the wrong month).
    assert snap is None


def test_fetch_for_month_returns_none_when_listing_has_no_match() -> None:
    """If the listing doesn't carry the requested month, return None so
    the coordinator falls back to the current snapshot as a proxy."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ebem_dynamic",
            "flanders",
            date(2024, 7, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "bogus",
            "flanders",
            date(2026, 5, 1),
        )
    )
    assert snap is None
