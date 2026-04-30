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
    dso_impact_band,
    energy_eur_per_kwh,
    is_offpeak,
    network_eur_per_kwh,
    taxes_eur_per_kwh,
    tou_slot,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
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
            flanders_renewables=0.015,
            wallonia_renewables=0.015,
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


# Wednesday 2026-04-29 is a non-holiday weekday for the boundary tests.
def test_tou_slot_weekday_morning_peak() -> None:
    assert tou_slot(datetime(2026, 4, 29, 7, 0)) == "peak"
    assert tou_slot(datetime(2026, 4, 29, 10, 59)) == "peak"


def test_tou_slot_weekday_midday_transition() -> None:
    assert tou_slot(datetime(2026, 4, 29, 11, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 16, 59)) == "transition"


def test_tou_slot_weekday_evening_peak() -> None:
    assert tou_slot(datetime(2026, 4, 29, 17, 0)) == "peak"
    assert tou_slot(datetime(2026, 4, 29, 21, 59)) == "peak"


def test_tou_slot_weekday_late_night_transition() -> None:
    # 22h-1h is transition (Heures creuses), not offpeak — both
    # SmartFlex and Empower Flextime documents state this.
    assert tou_slot(datetime(2026, 4, 29, 22, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 23, 59)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 0, 0)) == "transition"
    assert tou_slot(datetime(2026, 4, 29, 0, 59)) == "transition"


def test_tou_slot_weekday_morning_offpeak() -> None:
    # 1h-7h is offpeak (Heures super-creuses).
    assert tou_slot(datetime(2026, 4, 29, 1, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 4, 29, 6, 59)) == "offpeak"


def test_tou_slot_weekend_offpeak_default() -> None:
    # Default weekend_offpeak: Sat/Sun is entirely off-peak.
    assert tou_slot(datetime(2026, 5, 2, 9, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 5, 2, 19, 0)) == "offpeak"
    assert tou_slot(datetime(2026, 5, 3, 8, 0)) == "offpeak"


def test_tou_slot_weekend_no_peak_rule() -> None:
    # Engie Empower Flextime weekend rule:
    #   transition: 7-11 + 17-1 (so 17-22, 22-23, 0-1)
    #   offpeak:    1-7 + 11-17
    rule = "weekend_no_peak"
    # Saturday morning at 09:00: transition (would be peak on weekday).
    assert tou_slot(datetime(2026, 5, 2, 9, 0), rule) == "transition"
    # Saturday at 13:00: offpeak (weekend midday is offpeak under this rule).
    assert tou_slot(datetime(2026, 5, 2, 13, 0), rule) == "offpeak"
    # Saturday at 19:00: transition.
    assert tou_slot(datetime(2026, 5, 2, 19, 0), rule) == "transition"
    # Saturday at 23:30: transition (17-1 spans midnight).
    assert tou_slot(datetime(2026, 5, 2, 23, 30), rule) == "transition"
    # Saturday at 00:30: still transition (17-1 wraps).
    assert tou_slot(datetime(2026, 5, 2, 0, 30), rule) == "transition"
    # Saturday at 03:00: offpeak.
    assert tou_slot(datetime(2026, 5, 2, 3, 0), rule) == "offpeak"


def test_energy_tou_dispatches_by_slot() -> None:
    e = TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10)
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 9), None) == 0.30
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 13), None) == 0.20
    assert energy_eur_per_kwh(e, datetime(2026, 4, 29, 5), None) == 0.10
    assert energy_eur_per_kwh(e, datetime(2026, 5, 2, 9), None) == 0.10  # weekend


def test_energy_tou_respects_weekend_no_peak() -> None:
    # Same rates, but the weekend_rule changes the slot picked at 09:00.
    e_off = TimeOfUseRates(
        peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_offpeak"
    )
    e_no = TimeOfUseRates(
        peak=0.30, transition=0.20, offpeak=0.10, weekend_rule="weekend_no_peak"
    )
    sat_morning = datetime(2026, 5, 2, 9, 0)
    assert energy_eur_per_kwh(e_off, sat_morning, None) == 0.10  # offpeak
    assert energy_eur_per_kwh(e_no, sat_morning, None) == 0.20  # transition


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


def test_dso_impact_band_pic_evening() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 17, 0)) == "pic"
    assert dso_impact_band(datetime(2026, 4, 29, 21, 59)) == "pic"


def test_dso_impact_band_medium_morning_and_late_night() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 7, 0)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 10, 59)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 22, 0)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 23, 59)) == "medium"
    assert dso_impact_band(datetime(2026, 4, 29, 0, 30)) == "medium"


def test_dso_impact_band_eco_night_and_midday() -> None:
    assert dso_impact_band(datetime(2026, 4, 29, 1, 0)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 6, 59)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 11, 0)) == "eco"
    assert dso_impact_band(datetime(2026, 4, 29, 16, 59)) == "eco"


def test_dso_impact_band_no_weekend_exception() -> None:
    # Tarif Impact applies 7 days a week (unlike bi-horaire). A Saturday
    # 17h-22h block is still PIC.
    assert dso_impact_band(datetime(2026, 5, 2, 18, 0)) == "pic"


def test_network_impact_dispatches_by_band() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
        distribution_pic=0.10,
        distribution_medium=0.07,
        distribution_eco=0.03,
    )
    pic = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 18), "dynamic", "impact")
    medium = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 8), "dynamic", "impact")
    eco = network_eur_per_kwh(overlay, datetime(2026, 4, 29, 13), "dynamic", "impact")
    assert pic == pytest.approx(0.115)  # 0.10 + 0.015 transport
    assert medium == pytest.approx(0.085)
    assert eco == pytest.approx(0.045)


def test_network_impact_falls_back_when_dso_lacks_impact_rates() -> None:
    # Brussels Sibelga / Flanders Fluvius don't publish Impact rates.
    # Asking for "impact" mode there must degrade gracefully — fall back
    # to bi-horaire if peak/offpeak exist, else single. No KeyError.
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # Mid-day on a weekday with bi meter: same as bi_horaire peak path.
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 12), "bi", "impact"
    ) == pytest.approx(0.075)


def test_network_simple_mode_ignores_meter() -> None:
    overlay = DsoOverlay(
        distribution_single=0.05,
        distribution_peak=0.06,
        distribution_offpeak=0.04,
        transport=0.015,
    )
    # Even with bi meter at night, "simple" mode forces the single rate.
    assert network_eur_per_kwh(
        overlay, datetime(2026, 4, 29, 23), "bi", "simple"
    ) == pytest.approx(0.065)


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


def test_taxes_brussels_uses_brussels_renewables_only() -> None:
    # A Brussels entry must NOT pick up the Flemish/Walloon rate, even if
    # both are set; only its own brussels_renewables.
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.015,
        wallonia_renewables=0.0313,
        brussels_renewables=0.0265,
    )
    assert taxes_eur_per_kwh(t, "brussels") == pytest.approx(0.05 + 0.002 + 0.0265)


def test_taxes_wallonia_includes_connection_and_wallonia_renewables() -> None:
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.015,
        wallonia_renewables=0.0313,
        region_connection_fee=0.00075,
    )
    assert taxes_eur_per_kwh(t, "wallonia") == pytest.approx(
        0.05 + 0.002 + 0.0313 + 0.00075
    )


def test_taxes_flanders_uses_flanders_renewables_only() -> None:
    # Flanders entry must NOT pick up the Wallonia rate, even if both are set.
    t = TaxOverlay(
        federal_excise=0.05,
        energy_contribution=0.002,
        flanders_renewables=0.0152,
        wallonia_renewables=0.0313,
    )
    assert taxes_eur_per_kwh(t, "flanders") == pytest.approx(0.05 + 0.002 + 0.0152)


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
    # VAT must spread across every component, not get lumped into taxes.
    assert bd.energy == pytest.approx(0.18 * 1.06)
    assert bd.network == pytest.approx(0.065 * 1.06)
    assert bd.taxes == pytest.approx(0.067 * 1.06)
    # And the components must always sum to all_in to the cent.
    assert bd.energy + bd.network + bd.taxes == pytest.approx(bd.all_in)


def test_compute_breakdown_unknown_dso_raises() -> None:
    snap = _snapshot(FixedRates(single=0.18))
    with pytest.raises(KeyError):
        compute_breakdown(snap, "missing_dso", "flanders", datetime(2026, 4, 29, 12))
