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

"""Ecofix PDF extractor tests against May 2026 fixtures."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.ecofix import (
    discover,
    parse_snapshot,
)
from tests import fixture_text

_MOTION_ONLINE = "ecofix_motion_online.pdf"
_MOTION = "ecofix_motion.pdf"
_FLEXY = "ecofix_flexy.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


# ---- registry ---------------------------------------------------------------


def test_ecofix_is_registered() -> None:
    assert "ecofix" in EXTRACTORS
    extractor = EXTRACTORS["ecofix"]
    assert extractor.label == "Ecofix"
    assert {c.id for c in extractor.contracts} == {
        "ecofix_motion",
        "ecofix_motion_online",
        "ecofix_flexy",
    }
    # Brussels is not on any current Ecofix card; the registry must
    # advertise Flanders + Wallonia only so config-flow doesn't offer
    # Ecofix to Brussels households where every fetch would fail.
    for contract in extractor.contracts:
        assert "brussels" not in contract.regions


# ---- Motion Online (dynamic, low yearly fee) --------------------------------


def test_motion_online_energy_formula() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(10.0)
    # PDF: (0.1010 x Belpex 15M) + 0,9  c€/kWh ex-VAT, 6% VAT applied.
    # factor_pdf * 1.06 * 10 = 0.1010 * 10.6 = 1.0706
    # base_pdf * 1.06 / 100  = 0.9   * 0.0106 = 0.009540
    assert snap.energy.factor == pytest.approx(1.0706, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.00954, rel=1e-4)
    # At spot 100 EUR/MWh = 0.10 EUR/kWh, all-in energy ~0.11660 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.1166)


def test_motion_online_injection() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    inj = snap.injection
    assert inj is not None
    # PDF: (0.0884 x Belpex 15M) - 0.5000 c€/kWh ex-VAT.
    # Injection is VAT-exempt for residential, so no VAT applied.
    assert inj.factor == pytest.approx(0.884, rel=1e-4)
    assert inj.base == pytest.approx(-0.005, rel=1e-4)
    # Indicative monthly average printed on the card.
    assert inj.current == pytest.approx(0.0483)


def test_motion_online_publication() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert snap.publication_label == "2026-05"
    # Last day of May 2026: Sunday 31st.
    assert snap.valid_until == date(2026, 5, 31)


def test_motion_online_taxes_flanders() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    assert snap.taxes.flanders_renewables == pytest.approx(0.016)
    # Flanders pays no Wallonia connection fee or Wallonia renewables.
    assert snap.taxes.wallonia_renewables == 0.0
    assert snap.taxes.region_connection_fee == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_motion_online_taxes_wallonia() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    assert snap.taxes.wallonia_renewables == pytest.approx(0.0305)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.flanders_renewables == 0.0


def test_motion_online_flanders_dsos() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
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
    # Issue reporter is on Fluvius Kempen (= fluvius_iveka in the
    # integration's DSO key namespace).
    iveka = snap.dsos["fluvius_iveka"]
    assert iveka.distribution_single == pytest.approx(0.0633708)
    assert iveka.distribution_exclusive_night == pytest.approx(0.056606)
    assert iveka.capacity_eur_per_kw_year == pytest.approx(59.5794)
    assert iveka.data_management_per_year == pytest.approx(18.92)
    # Analog-meter prosumer rate is attached even when the user has a
    # digital meter; the integration filters by meter type downstream.
    assert iveka.prosumer_eur_per_kva_year == pytest.approx(67.79)


def test_motion_online_wallonia_dsos() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.distribution_pic == pytest.approx(0.1508)
    assert aieg.distribution_medium == pytest.approx(0.0982)
    assert aieg.distribution_eco == pytest.approx(0.0456)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


# ---- Motion (dynamic, full yearly fee + Ecofix Digi) ------------------------


def test_motion_energy_formula() -> None:
    snap = parse_snapshot("ecofix_motion", _layout(_MOTION), "flanders", "test://m")
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)
    # PDF: (0.1000 x Belpex 15M) + 1.1020  c€/kWh ex-VAT.
    # factor = 0.1000 * 1.06 * 10 = 1.0600
    # base   = 1.1020 * 1.06 / 100 = 0.0116812
    assert snap.energy.factor == pytest.approx(1.06, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.0116812, rel=1e-4)


def test_motion_publication_and_renewables_match_motion_online() -> None:
    """Motion and Motion Online ship the same monthly DSO + tax overlay
    even though the energy formula and yearly fee differ. Pin the
    parser so a future supplier-side divergence raises a real test
    failure rather than silently changing the bill.
    """
    snap_m = parse_snapshot("ecofix_motion", _layout(_MOTION), "flanders", "x")
    snap_mo = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "x"
    )
    assert snap_m.taxes.flanders_renewables == snap_mo.taxes.flanders_renewables
    assert snap_m.taxes.federal_excise == snap_mo.taxes.federal_excise
    assert snap_m.taxes.energy_contribution == snap_mo.taxes.energy_contribution
    assert snap_m.publication_label == snap_mo.publication_label
    assert snap_m.dsos["fluvius_iveka"] == snap_mo.dsos["fluvius_iveka"]


# ---- Flexy (variable, RLP-M monthly) ----------------------------------------


def test_flexy_is_variable_with_indicative_monthly_rate() -> None:
    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "flanders", "test://f")
    assert isinstance(snap.energy, VariableRates)
    # PDF prints "Maandprijs: 11,81 11,81 11,81 11,81" - same value
    # across mono / peak / off-peak / exclusive night today.
    assert snap.energy.current == pytest.approx(0.1181)
    assert snap.energy.peak == pytest.approx(0.1181)
    assert snap.energy.offpeak == pytest.approx(0.1181)
    assert snap.energy.exclusive_night == pytest.approx(0.1181)
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)
    assert snap.energy.formula is not None and "BELPEX-RLP-M" in snap.energy.formula


def test_flexy_injection_has_formula_and_indicative() -> None:
    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "wallonia", "test://f")
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.0432)
    # PDF: (BELPEX-SPP-M * 0.0884) - 0.5000  c€/kWh ex-VAT.
    assert inj.factor == pytest.approx(0.884, rel=1e-4)
    assert inj.base == pytest.approx(-0.005, rel=1e-4)
    assert inj.formula is not None and "BELPEX-SPP-M" in inj.formula


# ---- ORES sub-area drift detection -----------------------------------------


def test_ores_subarea_drift_is_rejected() -> None:
    """The Wallonia card lists 9 ORES sub-areas with identical numbers;
    the parser collapses them to one ``ores`` overlay. If a future card
    splits sub-areas (different numbers per row), the parser must raise
    rather than silently bill at the first sub-area's rate.
    """
    text = _layout(_MOTION_ONLINE)
    # Tweak one ORES row so its monohoraire rate diverges from the rest.
    bumped = text.replace(
        "ORES (Namur) 11,98 13,27 7,39",
        "ORES (Namur) 99,99 13,27 7,39",
        1,
    )
    with pytest.raises(ExtractorError, match="ORES sub-area .* diverged"):
        parse_snapshot("ecofix_motion_online", bumped, "wallonia", "x")


def test_unknown_contract_raises() -> None:
    with pytest.raises(ExtractorError, match="unknown Ecofix contract"):
        parse_snapshot("bogus", _layout(_MOTION_ONLINE), "wallonia", "x")


# ---- discover() -------------------------------------------------------------


class _HeadResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "_HeadResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubHeadSession:
    """Minimal session: returns 200 for the URLs in ``ok``, else 404."""

    def __init__(self, ok: set[str]) -> None:
        self._ok = ok

    def head(self, url: str, *_args: Any, **_kwargs: Any) -> _HeadResponse:
        return _HeadResponse(200 if url in self._ok else 404)


def test_discover_returns_all_three_contracts_when_each_url_200s() -> None:
    base = "https://portal.ecofixgp.be/docs/prices/current"
    session = _StubHeadSession(
        ok={
            f"{base}/EL_Ecofix_Motion_NL.pdf",
            f"{base}/EL_Ecofix_Motion_Online_NL.pdf",
            f"{base}/EL_Ecofix_Flexy_NL.pdf",
        }
    )
    discovered = asyncio.run(discover(session))  # type: ignore[arg-type]
    assert discovered == {"ecofix_motion", "ecofix_motion_online", "ecofix_flexy"}


def test_discover_drops_retired_product_when_url_404s() -> None:
    base = "https://portal.ecofixgp.be/docs/prices/current"
    # Simulate Ecofix retiring "Motion" while keeping the other two.
    session = _StubHeadSession(
        ok={
            f"{base}/EL_Ecofix_Motion_Online_NL.pdf",
            f"{base}/EL_Ecofix_Flexy_NL.pdf",
        }
    )
    discovered = asyncio.run(discover(session))  # type: ignore[arg-type]
    assert discovered == {"ecofix_motion_online", "ecofix_flexy"}
