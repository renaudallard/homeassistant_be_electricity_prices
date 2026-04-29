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

"""Tests for the pricing engine working off SupplierSnapshot."""

from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.be_electricity_prices.pricing import (
    compute_breakdown,
    energy_eur_per_kwh,
    is_offpeak,
    network_eur_per_kwh,
    taxes_eur_per_kwh,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)


def _snapshot(energy: EnergyRates, vat: float = 0.0) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=energy,
        dsos={
            "fluvius": DsoOverlay(
                distribution_single=0.05,
                distribution_peak=0.06,
                distribution_offpeak=0.04,
                transport=0.015,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            regional_renewables=0.015,
            region_connection_fee=0.001,
            vat_rate=vat,
        ),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
    )


def test_offpeak_weekday_night() -> None:
    assert is_offpeak(datetime(2026, 4, 29, 23, 0))
    assert is_offpeak(datetime(2026, 4, 29, 6, 0))
    assert not is_offpeak(datetime(2026, 4, 29, 12, 0))


def test_offpeak_weekend_always_offpeak() -> None:
    assert is_offpeak(datetime(2026, 5, 2, 12, 0))


def test_energy_fixed_single() -> None:
    e = FixedRates(single=0.20)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None) == 0.20


def test_energy_fixed_bihourly_picks_offpeak() -> None:
    e = FixedRates(single=0.20, peak=0.22, offpeak=0.18)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 23), None, "bi") == 0.18
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None, "bi") == 0.22


def test_energy_variable_uses_current() -> None:
    e = VariableRates(current=0.139)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None) == 0.139


def test_energy_dynamic_combines_factor_base_and_spot() -> None:
    e = DynamicRates(factor=0.10, base=0.025)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), 0.10) == pytest.approx(
        0.035
    )


def test_energy_dynamic_requires_spot() -> None:
    e = DynamicRates(factor=0.10, base=0.025)
    with pytest.raises(ValueError):
        energy_eur_per_kwh(e, datetime(2026, 4, 29, 12), None)


def test_network_single_meter() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(overlay, datetime(2026, 4, 29, 12)) == pytest.approx(
        0.065
    )


def test_network_bihourly_at_night() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "bi"
    ) == pytest.approx(0.055)


def test_network_dynamic_meter_uses_single_rate() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "dynamic"
    ) == pytest.approx(0.065)


def test_compute_breakdown_meter_bi_picks_offpeak_at_night() -> None:
    snap = _snapshot(FixedRates(single=0.20, peak=0.22, offpeak=0.18), vat=0.0)
    night = compute_breakdown(
        snap, "fluvius", "flanders", datetime(2026, 4, 29, 23), meter="bi"
    )
    day = compute_breakdown(
        snap, "fluvius", "flanders", datetime(2026, 4, 29, 12), meter="bi"
    )
    assert night.energy == 0.18
    assert day.energy == 0.22


def test_taxes_brussels_excludes_regional() -> None:
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        regional_renewables=0.015,
    )
    assert taxes_eur_per_kwh(t, "brussels") == pytest.approx(0.052)


def test_taxes_wallonia_includes_connection_and_renewables() -> None:
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        regional_renewables=0.0313,
        region_connection_fee=0.00075,
    )
    assert taxes_eur_per_kwh(t, "wallonia") == pytest.approx(
        0.05 + 0.002 + 0.0313 + 0.00075
    )


def test_taxes_flanders_includes_renewables() -> None:
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        regional_renewables=0.015,
    )
    assert taxes_eur_per_kwh(t, "flanders") == pytest.approx(0.067)


def test_compute_breakdown_with_vat_inclusive_snapshot() -> None:
    snap = _snapshot(FixedRates(single=0.18), vat=0.0)
    bd = compute_breakdown(snap, "fluvius", "flanders", datetime(2026, 4, 29, 12))
    assert bd.energy == 0.18
    assert bd.network == pytest.approx(0.065)
    assert bd.taxes == pytest.approx(0.067)
    assert bd.all_in == pytest.approx(0.18 + 0.065 + 0.067)


def test_compute_breakdown_with_vat_exclusive_snapshot() -> None:
    snap = _snapshot(FixedRates(single=0.18), vat=0.06)
    bd = compute_breakdown(snap, "fluvius", "flanders", datetime(2026, 4, 29, 12))
    expected = (0.18 + 0.065 + 0.067) * 1.06
    assert bd.all_in == pytest.approx(expected)


def test_compute_breakdown_unknown_dso_raises() -> None:
    snap = _snapshot(FixedRates(single=0.18))
    with pytest.raises(KeyError):
        compute_breakdown(snap, "missing_dso", "flanders", datetime(2026, 4, 29, 12))
