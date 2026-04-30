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

from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import (
    _compute_capacity,
    _compute_injection_price,
    _compute_prosumer,
    _compute_current_year_cost,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    FixedRates,
    InjectionRates,
    SupplierSnapshot,
    TaxOverlay,
)


def _snapshot(
    prosumer: float | None,
    capacity: float | None,
    injection: InjectionRates | None = None,
) -> SupplierSnapshot:
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
        injection=injection,
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


def test_injection_price_returns_none_outside_injection_regime() -> None:
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        injection=InjectionRates(current=0.05),
    )
    # Compensation regime users don't get the injection sensor.
    entry = _entry(solar_regime="compensation")
    assert _compute_injection_price(snap, entry, {}) is None


def test_injection_price_static_fallback_when_no_spot() -> None:
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        injection=InjectionRates(current=0.0476),
    )
    entry = _entry(solar_regime="injection")
    # No spot prices passed -> static current is used.
    assert _compute_injection_price(snap, entry, {}) == pytest.approx(0.0476)


def test_injection_price_uses_formula_when_spot_available() -> None:
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    entry = _entry(solar_regime="injection")
    # 0.10 EUR/kWh spot (= 100 EUR/MWh) -> 0.97 * 0.10 - 0.021 = 0.076.
    from homeassistant.util import dt as dt_util

    now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    spot = {now_hour: 0.10}
    assert _compute_injection_price(snap, entry, spot) == pytest.approx(0.076)
    # And it can go negative at low spot - producer pays to inject.
    spot_low = {now_hour: 0.005}
    assert _compute_injection_price(snap, entry, spot_low) == pytest.approx(
        0.97 * 0.005 - 0.021
    )


def test_injection_price_returns_none_when_no_data() -> None:
    snap = _snapshot(prosumer=None, capacity=None, injection=None)
    entry = _entry(solar_regime="injection")
    assert _compute_injection_price(snap, entry, {}) is None


def test_brussels_sibelga_charges_no_prosumer_or_capacity() -> None:
    # Sibelga has no per-kVA prosumer fee and no per-kW capacity fee.
    # A Brussels prosumer (smart meter on injection regime) must therefore
    # pay nothing on those lines, regardless of inverter capacity or peak.
    sibelga = DsoOverlay(
        distribution_single=0.0996,
        distribution_peak=0.0996,
        distribution_offpeak=0.0753,
        transport=0.0227,
    )
    snap = SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18),
        dsos={"sibelga": sibelga},
        taxes=TaxOverlay(
            federal_excise=0.05, energy_contribution=0.002, brussels_renewables=0.0265
        ),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
        injection=InjectionRates(current=0.0476),
    )
    brussels_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "dso": "sibelga",
            "solar_kva": 5.0,
            "solar_regime": "injection",
        },
    )
    assert _compute_prosumer(snap, brussels_entry) == 0.0
    assert _compute_capacity(snap, brussels_entry, 4.0) == 0.0
    # Supplier-side injection tariff applies uniformly across regions.
    assert _compute_injection_price(snap, brussels_entry, {}) == pytest.approx(0.0476)


# ---- _compute_current_year_cost ---------------------------------------------------


# Snapshot used by the current_year_cost tests: 0.18 single energy, 0.10 dist,
# 0.0145 transport, Wallonia taxes (federal_excise + energy_contribution +
# wallonia_renewables) so the all-in single rate works out cleanly.
def _yearly_snapshot() -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18, peak=0.20, offpeak=0.16),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                distribution_peak=0.11,
                distribution_offpeak=0.09,
                transport=0.0145,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            wallonia_renewables=0.03,
            energy_fund_eur_per_month=0.0,
        ),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
    )


def _yearly_entry(**overrides: object) -> MockConfigEntry:
    base: dict[str, object] = {
        "supplier": "test",
        "contract": "test",
        "region": "wallonia",
        "dso": "ores",
        "meter": "mono",
        "solar_regime": "none",
        "day_consumption_kwh": "sensor.day_cons",
        "night_consumption_kwh": "sensor.night_cons",
        "day_injection_kwh": "sensor.day_inj",
        "night_injection_kwh": "sensor.night_inj",
    }
    base.update(overrides)
    return MockConfigEntry(domain=DOMAIN, data=base)


def _set_meters(
    hass: HomeAssistant,
    *,
    day_cons: float | None,
    night_cons: float | None,
    day_inj: float | None,
    night_inj: float | None,
) -> None:
    """Push the four sensor states the current_year_cost helper reads."""
    pairs = (
        ("sensor.day_cons", day_cons),
        ("sensor.night_cons", night_cons),
        ("sensor.day_inj", day_inj),
        ("sensor.night_inj", night_inj),
    )
    for entity_id, value in pairs:
        if value is None:
            hass.states.async_set(entity_id, "unavailable")
        else:
            hass.states.async_set(entity_id, str(value))


def _zero_baselines() -> dict[str, float]:
    """Year-start baselines treating Jan 1 as the install moment.

    Setting every register baseline to 0 makes ``current - baseline ==
    current``, so existing tests that check ``cumulative * rate`` math
    keep passing without per-test baseline values.
    """
    return {
        "sensor.day_cons": 0.0,
        "sensor.night_cons": 0.0,
        "sensor.day_inj": 0.0,
        "sensor.night_inj": 0.0,
    }


async def test_current_year_cost_treats_missing_meter_as_zero_kwh(
    hass: HomeAssistant,
) -> None:
    """The sensor must never report ``unknown``: an unavailable register
    is treated as 0 kWh on that side, so the value still reflects the
    other readings + the fees floor."""
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=None, night_inj=200)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # regime=none mono: only consumption matters; day_inj=None -> 0.
    assert cost == pytest.approx(1500 * 0.3765)


async def test_current_year_cost_no_solar_mono_uses_total_cons_x_single_rate(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=0, night_inj=0)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # all_in single = 0.18 + 0.10 + 0.0145 + 0.05 + 0.002 + 0.03 = 0.3765
    assert cost == pytest.approx(1500 * 0.3765)


async def test_current_year_cost_no_solar_bi_uses_band_rates(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="bi", solar_regime="none")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=0, night_inj=0)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # peak = 0.20 + 0.11 + 0.0145 + 0.082 = 0.4065
    # offpeak = 0.16 + 0.09 + 0.0145 + 0.082 = 0.3465
    peak = 0.20 + 0.11 + 0.0145 + 0.05 + 0.002 + 0.03
    offpeak = 0.16 + 0.09 + 0.0145 + 0.05 + 0.002 + 0.03
    assert cost == pytest.approx(1000 * peak + 500 * offpeak)


async def test_current_year_cost_compensation_mono_nets_injection(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="compensation")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=600, night_inj=400)
    # net = 1500 - 1000 = 500. Bill = 500 * single_all_in
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    assert cost == pytest.approx(500 * 0.3765)


async def test_current_year_cost_compensation_mono_can_go_negative(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="compensation")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=1200, night_inj=900)
    # net = 1500 - 2100 = -600 -> cost = -600 * 0.3765 = -225.90.
    # Surplus is theoretically credited at the consumption rate; the
    # actual bill is usually floored at zero, but we report the
    # uncapped value so users see the over-production margin.
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    assert cost == pytest.approx(-600 * 0.3765)


async def test_current_year_cost_compensation_bi_nets_per_band(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="bi", solar_regime="compensation")
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=300, night_inj=600)
    # day net = 700 (positive), night net = -100 (negative). Per-band
    # weighted: 700 * peak + (-100) * offpeak.
    peak = 0.20 + 0.11 + 0.0145 + 0.05 + 0.002 + 0.03
    offpeak = 0.16 + 0.09 + 0.0145 + 0.05 + 0.002 + 0.03
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    assert cost == pytest.approx(700 * peak + (-100) * offpeak)


async def test_current_year_cost_injection_regime_uses_supplier_injection_rate(
    hass: HomeAssistant,
) -> None:
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="injection")
    _set_meters(hass, day_cons=800, night_cons=200, day_inj=300, night_inj=200)
    # cost = 1000 * single_all_in - 500 * inj_rate
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=0.05,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    assert cost == pytest.approx(1000 * 0.3765 - 500 * 0.05)


async def test_current_year_cost_dynamic_contract_falls_back_to_fees_only(
    hass: HomeAssistant,
) -> None:
    """Dynamic contracts have no stable rate to apply to a daily total,
    so the energy term is dropped -- the sensor still returns the
    fees-only floor instead of going unknown."""
    snap = SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=DynamicRates(factor=1.0, base=0.02, yearly_fixed_fee=80.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            energy_fund_eur_per_month=2.0,
        ),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
    )
    entry = _yearly_entry(solar_regime="injection")
    _set_meters(hass, day_cons=100, night_cons=100, day_inj=0, night_inj=0)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=0.05,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # Fees-only: yearly_fixed_fee 80 + 12 * energy_fund 2 = 80 + 24 = 104.
    assert cost == pytest.approx(104.0)


async def test_current_year_cost_includes_fees_and_prosumer(
    hass: HomeAssistant,
) -> None:
    snap = SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18, yearly_fixed_fee=120.0),
        dsos={
            "ores": DsoOverlay(distribution_single=0.10, transport=0.0145),
        },
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            wallonia_renewables=0.03,
            energy_fund_eur_per_month=2.5,
        ),
        source_url="test://",
        fetched_at_iso="2026-04-29T12:00:00+00:00",
    )
    entry = _yearly_entry(meter="mono", solar_regime="none")
    _set_meters(hass, day_cons=500, night_cons=500, day_inj=0, night_inj=0)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=4.5,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # 1000 * 0.3765 + yearly_fee 120 + 12 * 2.5 (energy_fund) + 12 * 4.5 (prosumer)
    assert cost == pytest.approx(1000 * 0.3765 + 120 + 30 + 54)


# ---- bucket fallback (totals -> internal day/night split) ------------------


async def test_current_year_cost_bucket_fallback_when_only_totals_configured(
    hass: HomeAssistant,
) -> None:
    """When the user only configured the cumulative totals (CONF_*_KWH),
    the helper falls back to the coordinator's day/night kwh_buckets
    (filled by the state listener over time)."""
    snap = _yearly_snapshot()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "bi",
            "solar_regime": "compensation",
            # Only the totals are set, no day/night registers.
            "consumption_kwh": "sensor.total_cons",
            "injection_kwh": "sensor.total_inj",
        },
    )
    buckets = {
        "consumption_day": 700.0,
        "consumption_night": 300.0,
        "injection_day": 200.0,
        "injection_night": 100.0,
    }
    peak = 0.20 + 0.11 + 0.0145 + 0.05 + 0.002 + 0.03
    offpeak = 0.16 + 0.09 + 0.0145 + 0.05 + 0.002 + 0.03
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets=buckets,
        register_baselines={},
    )
    # bi compensation: (700 - 200) * peak + (300 - 100) * offpeak
    assert cost == pytest.approx(500 * peak + 200 * offpeak)


async def test_current_year_cost_falls_back_to_fees_when_no_meters_configured(
    hass: HomeAssistant,
) -> None:
    """Even without any meter sensors configured, the sensor stays
    numeric and reports the fees-only floor. Better to display a stable
    minimum than to go ``unknown``; the user can wire meters anytime to
    grow the value."""
    snap = _yearly_snapshot()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
        },
    )
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines=_zero_baselines(),
    )
    # _yearly_snapshot has no fees configured -> cost is 0.0, not unknown.
    assert cost == 0.0


async def test_current_year_cost_day_night_registers_win_over_buckets(
    hass: HomeAssistant,
) -> None:
    """Day/night register sensors override the bucket fallback even when
    both are configured (registers are exact; buckets only cover the
    period since integration setup)."""
    snap = _yearly_snapshot()
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "day_consumption_kwh": "sensor.day_cons",
            "night_consumption_kwh": "sensor.night_cons",
            "day_injection_kwh": "sensor.day_inj",
            "night_injection_kwh": "sensor.night_inj",
            "consumption_kwh": "sensor.total_cons",
            "injection_kwh": "sensor.total_inj",
        },
    )
    # Direct registers say 1500 total; buckets say 9999 (deliberately
    # different) -- registers must win.
    _set_meters(hass, day_cons=1000, night_cons=500, day_inj=0, night_inj=0)
    buckets = {
        "consumption_day": 9999.0,
        "consumption_night": 9999.0,
        "injection_day": 0.0,
        "injection_night": 0.0,
    }
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets=buckets,
        register_baselines=_zero_baselines(),
    )
    assert cost == pytest.approx(1500 * 0.3765)


async def test_current_year_cost_subtracts_year_start_baseline(
    hass: HomeAssistant,
) -> None:
    """current_year_cost reflects ``current - baseline``, not the lifetime
    counter. After a Jan 1 baseline of 30 000 kWh, hitting 30 200 must
    give the same number as 200 from a fresh meter."""
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    _set_meters(hass, day_cons=30100, night_cons=30100, day_inj=10000, night_inj=10000)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines={
            "sensor.day_cons": 30000.0,
            "sensor.night_cons": 30000.0,
            "sensor.day_inj": 10000.0,
            "sensor.night_inj": 10000.0,
        },
    )
    # this-year cons = (30100 - 30000) + (30100 - 30000) = 200 kWh
    assert cost == pytest.approx(200 * 0.3765)


async def test_current_year_cost_register_falls_back_to_fees_when_baseline_missing(
    hass: HomeAssistant,
) -> None:
    """If the rollover handler hasn't captured a baseline yet (e.g. the
    register sensor was unavailable at first refresh), we treat the
    register as 0 kWh and surface the fees-only floor. Far better than
    going ``unknown`` -- the rollover top-up loop catches the baseline
    on the next refresh and the value snaps back to reality."""
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    _set_meters(hass, day_cons=30100, night_cons=30100, day_inj=0, night_inj=0)
    cost = _compute_current_year_cost(
        hass,
        snap,
        entry,
        prosumer_cost_eur_per_month=0.0,
        injection_price_eur_per_kwh=None,
        kwh_buckets={},
        register_baselines={},  # no baseline captured yet
    )
    # _yearly_snapshot has zero fees -> cost is 0.0 (not unknown).
    assert cost == 0.0
