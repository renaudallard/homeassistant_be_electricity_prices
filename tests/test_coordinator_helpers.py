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

"""Tests for the pure helper functions in coordinator.py."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import (
    _compute_capacity,
    _compute_prosumer,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
)


def _snapshot(prosumer: float | None, capacity: float | None) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=prosumer,
                capacity_eur_per_kw_year=capacity,
            )
        },
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002, vat_rate=0.0),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
    )


def _entry(**data: object) -> MockConfigEntry:
    # Default to compensation regime so tests focus on math; override
    # with solar_regime= when testing the gating logic.
    base = {"dso": "ores", "solar_kva": 0.0, "solar_regime": "compensation"}
    base.update(data)
    return MockConfigEntry(domain=DOMAIN, data=base)


def test_prosumer_zero_kva_returns_zero() -> None:
    assert _compute_prosumer(_snapshot(prosumer=85.0, capacity=None), _entry()) == 0.0


def test_prosumer_compensation_regime_monthly_cost() -> None:
    # ORES rate ~85 EUR/kVA/yr, 5 kVA inverter -> 5 * 85 / 12 = 35.42 EUR/month.
    cost = _compute_prosumer(
        _snapshot(prosumer=85.0, capacity=None),
        _entry(solar_kva=5.0),
    )
    assert cost == pytest.approx(5.0 * 85.0 / 12.0)


def test_prosumer_no_rate_in_dso_overlay_returns_zero() -> None:
    # Flemish digital meter / Cociter SMR3: no compensation regime.
    cost = _compute_prosumer(
        _snapshot(prosumer=None, capacity=60.0),
        _entry(solar_kva=5.0),
    )
    assert cost == 0.0


def test_prosumer_unknown_dso_returns_zero() -> None:
    cost = _compute_prosumer(
        _snapshot(prosumer=85.0, capacity=None),
        _entry(dso="missing_dso", solar_kva=5.0),
    )
    assert cost == 0.0


def test_prosumer_ignores_negative_kva() -> None:
    cost = _compute_prosumer(
        _snapshot(prosumer=85.0, capacity=None),
        _entry(solar_kva=-3.0),
    )
    assert cost == 0.0


def test_prosumer_injection_regime_returns_zero() -> None:
    # Post-2024 Walloon installations are on the injection tariff and pay
    # no compensation-regime per-kVA fee, even if the DSO publishes one.
    cost = _compute_prosumer(
        _snapshot(prosumer=85.0, capacity=None),
        _entry(solar_kva=5.0, solar_regime="injection"),
    )
    assert cost == 0.0


def test_prosumer_no_regime_set_returns_zero() -> None:
    cost = _compute_prosumer(
        _snapshot(prosumer=85.0, capacity=None),
        _entry(solar_kva=5.0, solar_regime="none"),
    )
    assert cost == 0.0


def test_capacity_returns_zero_when_no_capacity_rate() -> None:
    # Wallonia DSOs have no capacity tariff.
    cost = _compute_capacity(_snapshot(prosumer=85.0, capacity=None), _entry(), 5.0)
    assert cost == 0.0


def test_capacity_monthly_cost() -> None:
    # 60 EUR/kW/yr x 4 kW peak = 240 EUR/yr -> 20 EUR/month.
    cost = _compute_capacity(_snapshot(prosumer=None, capacity=60.0), _entry(), 4.0)
    assert cost == pytest.approx(20.0)
