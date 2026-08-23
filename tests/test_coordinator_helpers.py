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

from custom_components.be_electricity_prices import cohort
from custom_components.be_electricity_prices import snapshot_store
from custom_components.be_electricity_prices import ytd_cost

from custom_components.be_electricity_prices import energy_meters

import calendar
from datetime import UTC, date, datetime, timedelta
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_INCLUDE_VAT,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
)
from custom_components.be_electricity_prices.cohort import (
    _cohort_energy_from_archived,
    _cohort_energy_leg,
    _contract_start_month,
    _effective_snapshot_for_month,
    _manual_energy_leg,
)
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
)
from custom_components.be_electricity_prices.energy_meters import (
    _live_today_kwh,
    _recorder_daily_kwh,
)
from custom_components.be_electricity_prices.fees import (
    _compute_capacity,
    _compute_prosumer,
)
from custom_components.be_electricity_prices.injection import (
    _compute_injection_price,
    _historical_injection_rate,
    _injection_hourly_on_cohort,
    _injection_needs_month_spot,
    _injection_needs_spot,
    _injection_price_for_slot,
    _injection_varies_intraday,
)
from custom_components.be_electricity_prices.snapshot_store import (
    _SNAPSHOT_SCHEMA_VERSION,
    _energy_kind,
    _monthly_fetched_at,
    _monthly_snapshots,
    _snapshot_for_month,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from custom_components.be_electricity_prices.projected_cost import (
    _compute_projected_year_cost,
)
from custom_components.be_electricity_prices.spot_stats import (
    _bucket_by_local_month,
    _covered_month_mean,
)
from custom_components.be_electricity_prices.ytd_cost import (
    _compute_current_year_cost,
    _days_through,
    _ytd_spot_injection_credit,
    _ytd_static_fees,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.pricing import (
    energy_eur_per_kwh,
    yearly_fixed_fee_for_meter,
)
from tests import make_snapshot, make_stub_extractor


def _snapshot(
    prosumer: float | None,
    capacity: float | None,
    injection: InjectionRates | None = None,
    energy: EnergyRates | None = None,
) -> SupplierSnapshot:
    return make_snapshot(
        energy=energy,  # None -> make_snapshot's FixedRates default
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=prosumer,
                capacity_eur_per_kw_year=capacity,
            )
        },
        injection=injection,
    )


def _entry(**data: object) -> MockConfigEntry:
    # Default to a Walloon compensation entry (ores is a Walloon DSO) so
    # tests focus on math; compensation is Walloon-only, so the region must
    # be set for _compute_prosumer to bill. Override with region= /
    # solar_regime= when testing the gating logic.
    base = {
        "region": "wallonia",
        "dso": "ores",
        "solar_kva": 0.0,
        "solar_regime": "compensation",
    }
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


def test_prosumer_compensation_is_walloon_only() -> None:
    # Compensation is Walloon-only. A Flanders compensation entry must not
    # bill the prosumer fee on top of the always-billed capaciteitstarief
    # (that would double-count grid-recovery); it returns 0 regardless of the
    # (now unbilled) Flanders prosumer rate on the overlay.
    snap = _snapshot(prosumer=85.0, capacity=52.37)
    walloon = _compute_prosumer(snap, _entry(solar_kva=5.0, region="wallonia"))
    flemish = _compute_prosumer(snap, _entry(solar_kva=5.0, region="flanders"))
    assert walloon == pytest.approx(5.0 * 85.0 / 12.0)
    assert flemish == 0.0


def test_prosumer_adds_supplier_forfait_on_top_of_dso() -> None:
    # Cociter Variable bills the DSO prosumer tariff AND a supplier-side
    # compensation forfait (37,10 EUR/kVA/an); both apply per kVA per year.
    snap = make_snapshot(
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=81.03,
            )
        },
        supplier_prosumer_eur_per_kva_year=37.10,
    )
    cost = _compute_prosumer(snap, _entry(solar_kva=5.0))
    assert cost == pytest.approx(5.0 * (81.03 + 37.10) / 12.0)


def test_prosumer_supplier_forfait_billed_without_dso_rate() -> None:
    # The supplier forfait stands alone even when the DSO publishes no
    # prosumer tariff for the configured area.
    snap = make_snapshot(
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        supplier_prosumer_eur_per_kva_year=37.10,
    )
    cost = _compute_prosumer(snap, _entry(solar_kva=5.0))
    assert cost == pytest.approx(5.0 * 37.10 / 12.0)


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


def test_the_three_capacity_paths_share_one_formula() -> None:
    """peak x rate / 12 was written out in the live tick, the year-to-date walk
    and the backfill's per-hour accrual, each with its own spelling of the two
    "nothing to bill" guards.

    _annual_static_fees is shared across those same three paths precisely so a
    fee component cannot drift between them; capacity was the one left out.
    Pin that the shared helper answers identically for every shape, including
    a card with no capacity row and a DSO missing from the snapshot.
    """
    from custom_components.be_electricity_prices.fees import (
        _capacity_monthly_eur,
    )

    def overlay(rate: float | None) -> DsoOverlay:
        return DsoOverlay(
            distribution_single=0.1, transport=0.01, capacity_eur_per_kw_year=rate
        )

    assert _capacity_monthly_eur(overlay(43.5), 6.4) == pytest.approx(23.2)
    assert _capacity_monthly_eur(overlay(43.5), 2.5) == pytest.approx(9.0625)
    # Nothing to bill: no card row, a zero rate, no overlay at all, no peak.
    assert _capacity_monthly_eur(overlay(None), 6.4) == 0.0
    assert _capacity_monthly_eur(overlay(0.0), 6.4) == 0.0
    assert _capacity_monthly_eur(None, 6.4) == 0.0
    assert _capacity_monthly_eur(overlay(43.5), 0.0) == 0.0

    # The live wrapper keeps its own defensive read of a corrupt entry that
    # lost CONF_DSO -- that guard is the wrapper's, not the helper's.
    snap = make_snapshot(dsos={"ores": overlay(43.5)})
    assert _compute_capacity(snap, SimpleNamespace(data={}), 6.4) == 0.0  # type: ignore[arg-type]


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


def test_injection_price_uses_formula_when_spot_available(freezer: Any) -> None:
    # Per-hour formula injection only applies on a DynamicRates contract
    # (those are the only ones the coordinator fetches spots for).
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    entry = _entry(solar_regime="injection")
    # 0.10 EUR/kWh spot (= 100 EUR/MWh) -> 0.97 * 0.10 - 0.021 = 0.076.
    from homeassistant.util import dt as dt_util

    # Pin the wall clock so the test's now_hour key and the impl's own
    # dt_util.utcnow() lookup agree even when the suite straddles an
    # hour boundary.
    freezer.move_to("2026-05-15 12:00:00+02:00")
    now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    spot = {now_hour: 0.10}
    assert _compute_injection_price(snap, entry, spot) == pytest.approx(0.076)
    # And it can go negative at low spot - producer pays to inject.
    spot_low = {now_hour: 0.005}
    assert _compute_injection_price(snap, entry, spot_low) == pytest.approx(
        0.97 * 0.005 - 0.021
    )


def test_injection_price_quarter_hourly_rejects_spot_over_one_slot_away(
    freezer: Any,
) -> None:
    """On a quarter-hourly contract the substitute spot must be within one
    15-minute slot. A fixed 1 h window let the injection display use a spot
    up to four slots away; the window now scales to the billing grid."""
    from homeassistant.util import dt as dt_util

    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0, quarter_hourly=True),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    entry = _entry(solar_regime="injection")
    freezer.move_to("2026-05-15 12:00:00+02:00")
    now_slot = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    # A spot 30 min (two slots) away is rejected -> sensor unknown.
    assert (
        _compute_injection_price(snap, entry, {now_slot + timedelta(minutes=30): 0.10})
        is None
    )
    # The exact slot is still used.
    assert _compute_injection_price(snap, entry, {now_slot: 0.10}) == pytest.approx(
        0.97 * 0.10 - 0.021
    )


def test_injection_price_returns_none_when_no_data() -> None:
    snap = _snapshot(prosumer=None, capacity=None, injection=None)
    entry = _entry(solar_regime="injection")
    assert _compute_injection_price(snap, entry, {}) is None


def test_historical_injection_rate_prefers_formula_over_current() -> None:
    # Dynamic-injection cards (engie/octaplus/totalenergies/luminus/mega)
    # publish BOTH a flat `current` indicative and factor/base. The YTD
    # rate must use the spot formula when a spot is available, matching
    # the live _compute_injection_price, instead of the flat indicative.
    both = InjectionRates(current=0.045, factor=0.9, base=-0.01)
    assert _historical_injection_rate(both, 0.10) == pytest.approx(0.9 * 0.10 - 0.01)
    # No spot (static YTD path) -> fall back to the monthly indicative.
    assert _historical_injection_rate(both, None) == pytest.approx(0.045)
    # Pure static card (no formula) -> the indicative, with or without spot.
    static = InjectionRates(current=0.0476)
    assert _historical_injection_rate(static, 0.10) == pytest.approx(0.0476)
    assert _historical_injection_rate(None, 0.10) is None


def test_historical_injection_rate_picks_tou_slot() -> None:
    # Issue #34: Engie Empower Flextime's feed-in tariff varies by slot.
    # With the energy + hour given, the per-slot rate is selected with the
    # same tou_slot() rule as consumption; without them it falls back to
    # the single ``current``.
    energy = TimeOfUseRates(
        peak=0.20, transition=0.15, offpeak=0.10, weekend_rule="weekend_no_peak"
    )
    inj = InjectionRates(current=0.05, peak=0.084, transition=0.048, offpeak=0.015)
    wed = date(2026, 4, 29)  # a weekday
    peak_h = datetime.combine(wed, datetime.min.time()).replace(hour=9)
    trans_h = datetime.combine(wed, datetime.min.time()).replace(hour=13)
    off_h = datetime.combine(wed, datetime.min.time()).replace(hour=3)
    assert _historical_injection_rate(inj, energy=energy, when=peak_h) == pytest.approx(
        0.084
    )
    assert _historical_injection_rate(
        inj, energy=energy, when=trans_h
    ) == pytest.approx(0.048)
    assert _historical_injection_rate(inj, energy=energy, when=off_h) == pytest.approx(
        0.015
    )
    # No energy/when context -> single-rate fallback.
    assert _historical_injection_rate(inj) == pytest.approx(0.05)
    # A non-TOU contract ignores the per-slot fields entirely.
    assert _historical_injection_rate(
        inj, energy=FixedRates(single=0.20), when=peak_h
    ) == pytest.approx(0.05)


def test_injection_price_dynamic_returns_none_without_spot() -> None:
    """A DynamicRates contract's per-hour formula injection must surface
    None when no spot is available -- falling back to the snapshot's
    static `current` would be the wrong rate for a per-hour contract."""
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0),
        injection=InjectionRates(factor=0.97, base=-0.021, current=0.05),
    )
    entry = _entry(solar_regime="injection")
    # Spot cache empty -> sensor goes unknown rather than show 0.05.
    assert _compute_injection_price(snap, entry, {}) is None


def test_injection_price_static_energy_monthly_injection_uses_current() -> None:
    """A static-energy contract (Fixed/Variable) whose injection carries a
    MONTHLY index formula (Ecofix Flexy's BELPEX-SPP-M) never receives a
    per-hour spot, so the live sensor must show the realized monthly
    `current` rather than going unknown forever -- matching the YTD credit
    (_historical_injection_rate) for the same hour."""
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=VariableRates(current=0.16),
        injection=InjectionRates(current=0.0432, factor=0.884, base=-0.005),
    )
    entry = _entry(solar_regime="injection")
    # No spots are ever fetched for a variable contract; surface the
    # monthly indicative, not None.
    assert _compute_injection_price(snap, entry, {}) == pytest.approx(0.0432)


def test_injection_price_spot_indexed_static_uses_formula(freezer: Any) -> None:
    """A static-energy contract whose injection is an hourly spot formula
    with NO monthly indicative (Cociter Variable, EBEM Variabel/B@sic+)
    must price the injection from the spot when one is available, and go
    unknown when it isn't -- not fall back to a non-existent current."""
    from homeassistant.util import dt as dt_util

    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=VariableRates(current=0.16),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    entry = _entry(solar_regime="injection")
    freezer.move_to("2026-05-15 12:00:00+02:00")
    now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    assert _compute_injection_price(snap, entry, {now_hour: 0.10}) == pytest.approx(
        0.97 * 0.10 - 0.021
    )
    # No spot -> unknown (there is no monthly indicative to fall back to).
    assert _compute_injection_price(snap, entry, {}) is None


# --- today/tomorrow injection array (issue #40) -------------------------


def _slot(hour: int) -> datetime:
    """A UTC grid key at ``hour`` on the pinned synthetic day."""
    return datetime(2026, 5, 15, hour, tzinfo=UTC)


def test_injection_price_for_slot_spot_formula() -> None:
    inj = InjectionRates(factor=0.97, base=-0.021, current=None)
    energy = DynamicRates(factor=0.1, base=0.0)
    when = _slot(12)
    assert _injection_price_for_slot(inj, energy, 0.10, when) == pytest.approx(
        0.97 * 0.10 - 0.021
    )
    # Spot-indexed but no spot for the slot -> None (never fabricate a value).
    assert _injection_price_for_slot(inj, energy, None, when) is None


def test_injection_price_for_slot_flat_current_ignores_spot() -> None:
    inj = InjectionRates(current=0.0476)
    energy = FixedRates(single=0.20)
    when = _slot(12)
    assert _injection_price_for_slot(inj, energy, 0.10, when) == pytest.approx(0.0476)
    assert _injection_price_for_slot(inj, energy, None, when) == pytest.approx(0.0476)


def test_injection_price_for_slot_dual_published_static_stays_on_current() -> None:
    # Static energy + injection carrying BOTH a monthly `current` and
    # factor/base (Ecofix Flexy, EBEM B@sic+): the guard must keep it on the
    # flat monthly rate even when a spot is present, or the flat credit would
    # flip to a spot-varying one (the F41-class shape bug).
    inj = InjectionRates(current=0.0432, factor=0.884, base=-0.005)
    energy = VariableRates(current=0.16)
    assert _injection_price_for_slot(inj, energy, 0.10, _slot(12)) == pytest.approx(
        0.0432
    )


def test_injection_price_for_slot_tou_picks_slot_over_spot() -> None:
    energy = TimeOfUseRates(
        peak=0.20, transition=0.15, offpeak=0.10, weekend_rule="weekend_no_peak"
    )
    inj = InjectionRates(current=0.05, peak=0.084, transition=0.048, offpeak=0.015)
    wed = date(2026, 4, 29)
    peak_h = datetime.combine(wed, datetime.min.time()).replace(hour=9)
    off_h = datetime.combine(wed, datetime.min.time()).replace(hour=3)
    # The TOU rate wins even when a spot is passed; the slot is picked by hour.
    assert _injection_price_for_slot(inj, energy, 0.99, peak_h) == pytest.approx(0.084)
    assert _injection_price_for_slot(inj, energy, 0.99, off_h) == pytest.approx(0.015)


def test_injection_price_for_slot_floors_negative() -> None:
    inj = InjectionRates(factor=0.9, base=-0.5, current=None, floor_at_zero=True)
    energy = DynamicRates(factor=0.1, base=0.0)
    # 0.9 * 0.10 - 0.5 = -0.41 -> clamped to 0.
    assert _injection_price_for_slot(inj, energy, 0.10, _slot(12)) == 0.0


def test_injection_varies_intraday_true_for_spot_and_tou() -> None:
    # Dynamic energy + formula.
    assert _injection_varies_intraday(
        InjectionRates(factor=0.9, base=-0.01, current=0.05),
        DynamicRates(factor=0.1, base=0.0),
    )
    # Static energy + spot formula with no monthly indicative (Cociter Variable).
    assert _injection_varies_intraday(
        InjectionRates(factor=0.97, base=-0.021, current=None),
        VariableRates(current=0.16),
    )
    # TOU schedule (Engie Empower Flextime).
    assert _injection_varies_intraday(
        InjectionRates(current=0.05, peak=0.08, transition=0.05, offpeak=0.02),
        TimeOfUseRates(
            peak=0.2, transition=0.15, offpeak=0.1, weekend_rule="weekend_no_peak"
        ),
    )


def test_injection_varies_intraday_false_for_flat() -> None:
    # Flat monthly indicative only.
    assert not _injection_varies_intraday(
        InjectionRates(current=0.0476), FixedRates(single=0.20)
    )
    # Dual-published on static energy -> guard keeps it flat.
    assert not _injection_varies_intraday(
        InjectionRates(current=0.0432, factor=0.884, base=-0.005),
        VariableRates(current=0.16),
    )
    # Spot-monthly baked to a flat indicative (factor/base cleared).
    assert not _injection_varies_intraday(
        InjectionRates(current=0.05, factor=None, base=None),
        SpotMonthlyRates(factor=1.0, base=0.0),
    )


def _build_injection_hourly(
    entry: MockConfigEntry,
    snap: SupplierSnapshot,
    spot_prices: dict[datetime, float],
    grid_keys: list[datetime],
) -> dict[datetime, float]:
    """Call the coordinator method with a light stub (only ``entry`` is read)."""
    stub = SimpleNamespace(entry=entry)
    return BePricesCoordinator._build_injection_hourly(
        stub,  # type: ignore[arg-type]
        snap,
        snap.energy,
        spot_prices,
        grid_keys,
    )


def test_build_injection_hourly_prices_each_slot() -> None:
    entry = _entry(solar_regime="injection")
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    spots = {_slot(10): 0.10, _slot(11): 0.20}
    out = _build_injection_hourly(entry, snap, spots, [_slot(10), _slot(11)])
    assert out[_slot(10)] == pytest.approx(0.97 * 0.10 - 0.021)
    assert out[_slot(11)] == pytest.approx(0.97 * 0.20 - 0.021)


def test_build_injection_hourly_drops_slots_without_spot() -> None:
    # Tomorrow's slots have no spot until the day-ahead publishes -> dropped,
    # exactly like the consumption tomorrow array.
    entry = _entry(solar_regime="injection")
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    out = _build_injection_hourly(
        entry, snap, {_slot(10): 0.10}, [_slot(10), _slot(11)]
    )
    assert list(out) == [_slot(10)]


def test_build_injection_hourly_prices_each_tou_slot() -> None:
    # Engie Empower Flextime: no spot is involved, but the array must still
    # carry the per-band rate for every slot, because that array is what the
    # injection_price sensor reads for the current slot (issue #44). Without
    # it the sensor falls back to the scalar the coordinator baked at its last
    # tick and lags the band change.
    entry = _entry(solar_regime="injection")
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=TimeOfUseRates(
            peak=0.16738,
            transition=0.13072,
            offpeak=0.09796,
            weekend_rule="weekend_no_peak",
        ),
        injection=InjectionRates(
            current=0.04918, peak=0.08417, transition=0.04834, offpeak=0.01465
        ),
    )
    # The pinned day is a Friday, Brussels is UTC+2, so the UTC grid keys land
    # on 02:00 / 07:00 / 11:00 local.
    out = _build_injection_hourly(entry, snap, {}, [_slot(0), _slot(5), _slot(9)])
    assert out[_slot(0)] == pytest.approx(0.01465)  # super-creuses
    assert out[_slot(5)] == pytest.approx(0.08417)  # pleines
    assert out[_slot(9)] == pytest.approx(0.04834)  # creuses


def test_build_injection_hourly_empty_off_injection_regime() -> None:
    entry = _entry(solar_regime="compensation")
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=DynamicRates(factor=0.1, base=0.0),
        injection=InjectionRates(factor=0.97, base=-0.021, current=None),
    )
    assert _build_injection_hourly(entry, snap, {_slot(10): 0.10}, [_slot(10)]) == {}


def test_build_injection_hourly_empty_for_flat_contract() -> None:
    # A flat monthly-indicative injection would just repeat its scalar, so no
    # array is emitted.
    entry = _entry(solar_regime="injection")
    snap = _snapshot(
        prosumer=None, capacity=None, injection=InjectionRates(current=0.0476)
    )
    assert _build_injection_hourly(entry, snap, {}, [_slot(10), _slot(11)]) == {}


def test_injection_needs_spot_only_for_static_spot_indexed_injection() -> None:
    inj_regime = _entry(solar_regime="injection")
    none_regime = _entry(solar_regime="none")
    spot_inj = InjectionRates(factor=0.9, base=-0.01, current=None)
    monthly_inj = InjectionRates(factor=0.9, base=-0.01, current=0.04)
    # Static energy + spot-indexed injection (current None) on injection
    # regime -> needs a spot.
    snap = _snapshot(prosumer=None, capacity=None, energy=VariableRates(current=0.16))
    assert _injection_needs_spot(
        _snapshot(
            prosumer=None,
            capacity=None,
            energy=VariableRates(current=0.16),
            injection=spot_inj,
        ),
        inj_regime,
    )
    # Monthly-indexed (current set) -> no extra spot needed.
    assert not _injection_needs_spot(
        _snapshot(
            prosumer=None,
            capacity=None,
            energy=VariableRates(current=0.16),
            injection=monthly_inj,
        ),
        inj_regime,
    )
    # Dynamic energy already fetches spots via the energy path -> excluded.
    assert not _injection_needs_spot(
        _snapshot(
            prosumer=None,
            capacity=None,
            energy=DynamicRates(factor=0.1, base=0.0),
            injection=spot_inj,
        ),
        inj_regime,
    )
    # Not on the injection regime -> never.
    assert not _injection_needs_spot(
        _snapshot(
            prosumer=None,
            capacity=None,
            energy=VariableRates(current=0.16),
            injection=spot_inj,
        ),
        none_regime,
    )
    assert snap is not None  # silence unused in some linters


async def test_ytd_spot_injection_credit_replays_hourly_spots(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The isolated YTD credit sums per-hour injected kWh * (factor*spot +
    base) for a spot-indexed-injection contract; hours without a cached
    spot are skipped, and a contract with a monthly indicative is a
    no-op (handled by the daily path instead)."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    today = dt_util.now().date()
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=VariableRates(current=0.16),
        injection=InjectionRates(factor=0.9, base=-0.01, current=None),
    )
    entry = _entry(solar_regime="injection", injection_kwh="sensor.inj_total")
    h1 = dt_util.start_of_local_day(datetime(2026, 1, 6)).astimezone(UTC) + timedelta(
        hours=11
    )
    h2 = h1 + timedelta(hours=1)
    spots = {h1: 0.10, h2: 0.20}  # h3 below has no spot -> skipped

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.inj_total":
            return {h1: 2.0, h2: 1.0, h2 + timedelta(hours=1): 5.0}
        return {}

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        credit = await _ytd_spot_injection_credit(hass, snap, entry, today, spots)
    # 2*(0.9*0.10-0.01) + 1*(0.9*0.20-0.01); the 5 kWh hour has no spot.
    assert credit == pytest.approx(2 * (0.9 * 0.10 - 0.01) + 1 * (0.9 * 0.20 - 0.01))
    # Monthly-indicative injection -> no-op here (the daily path credits it).
    monthly = _snapshot(
        prosumer=None,
        capacity=None,
        energy=VariableRates(current=0.16),
        injection=InjectionRates(factor=0.9, base=-0.01, current=0.04),
    )
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        assert (
            await _ytd_spot_injection_credit(hass, monthly, entry, today, spots) == 0.0
        )


async def test_ytd_breakdown_splits_the_capacity_leg_out_of_fees(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The fee lump hid the leg most able to move the bill.

    The Flanders capacity tariff is charged per kW of monthly peak per year,
    52 to 60 EUR/kW across the Fluvius areas, so two entries reading the same
    meter and the same card still differ by hundreds of euro when they resolve
    different peaks. None of that appears on the price graph, which is per kWh,
    so a user comparing two entries had no way to see where the money went."""

    freezer.move_to("2026-08-22 12:00:00+02:00")
    snap = replace(
        _yearly_snapshot(),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                capacity_eur_per_kw_year=52.37,
            )
        },
    )
    entry = _entry(
        region="flanders",
        dso="fluvius_antwerpen",
        solar_regime="none",
        supplier="test",
        contract="test",
    )

    async def _run(peak: float) -> dict[str, float]:
        diag: dict[str, float] = {}
        await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            billed_peak_kw=peak,
            breakdown=diag,
        )
        return diag

    floor = await _run(2.5)
    high = await _run(12.0)

    # The capacity leg is reported on its own, and the peak it was billed on
    # alongside it, so the two can be compared without a diagnostics download.
    assert floor["billed_peak_kw"] == 2.5
    assert high["billed_peak_kw"] == 12.0
    assert high["capacity_ytd_eur"] > floor["capacity_ytd_eur"]
    # 9.5 kW at 52.37 EUR/kW/year, prorated over the elapsed year.
    elapsed = (date(2026, 8, 22) - date(2026, 1, 1)).days + 1
    expected = 9.5 * 52.37 * elapsed / 365
    assert high["capacity_ytd_eur"] - floor["capacity_ytd_eur"] == pytest.approx(
        expected, rel=0.02
    )
    # And the legs still add up to the lump they were split out of.
    for diag in (floor, high):
        assert diag["fees_ytd_eur"] == pytest.approx(
            diag["capacity_ytd_eur"]
            + diag["prosumer_ytd_eur"]
            + diag["standing_charges_ytd_eur"]
        )


async def test_ytd_spot_injection_credit_tops_up_today_from_the_meter(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Today's feed-in credit must read the same source today's charge does.

    The consumption leg has read the live meter for today since 0.11.9, and
    the hourly branch tops both of its sides up. This isolated credit stopped
    at compiled statistics, so within one current_year_cost the charge was
    live to the minute while the offsetting credit trailed the last compiled
    hour. That over-states the bill by the uncompiled part of today's
    injection, and does not heal while compilation is stalled, which is the
    failure the live read was added for."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    today = dt_util.now().date()
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        energy=VariableRates(current=0.16),
        injection=InjectionRates(factor=1.0, base=0.0, current=None),
    )
    entry = _entry(solar_regime="injection", injection_kwh="sensor.inj_total")
    # Statistics have compiled the first two hours of today; the meter says
    # 4 kWh more has been injected since.
    hour = dt_util.start_of_local_day(today).astimezone(UTC) + timedelta(hours=10)
    spots = {hour + timedelta(hours=h): 0.10 for h in range(4)}

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        return {hour: 4.0, hour + timedelta(hours=1): 4.0} if entity_id else {}

    hass.states.async_set("sensor.inj_total", "112.0", _meter_attrs("kWh"))
    inst = _midnight_instance(
        {"sensor.inj_total": [State("sensor.inj_total", "100.0")]}
    )
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly),
        patch("homeassistant.components.recorder.get_instance", return_value=inst),
    ):
        credit = await _ytd_spot_injection_credit(hass, snap, entry, today, spots)
    # The meter reports 12 kWh injected today, statistics carry 8. All 12 are
    # credited at 0.10; stopping at the compiled 8 would credit 0.80.
    assert credit == pytest.approx(1.20)


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
    snap = make_snapshot(
        dsos={"sibelga": sibelga},
        taxes=TaxOverlay(
            federal_excise=0.05, energy_contribution=0.002, brussels_renewables=0.0265
        ),
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


# ---- _recorder_daily_kwh ------------------------------------------------------


def _stat_row(year: int, month: int, day: int, kwh: float) -> dict[str, float]:
    """Build a fake StatisticsRow whose ``start`` is the UTC equivalent
    of local midnight on the given date -- the way HA's recorder
    actually surfaces daily buckets after timezone conversion. The
    helper reads ``change`` (per-period delta), not ``sum`` (cumulative)."""
    local_start = dt_util.start_of_local_day(datetime(year, month, day))
    return {"start": local_start.astimezone(UTC).timestamp(), "change": kwh}


async def test_recorder_daily_kwh_returns_per_day_sums(
    hass: HomeAssistant,
) -> None:
    """The helper unwraps the recorder's StatisticsRow list into a
    {local_day: kWh} dict the year-cost loop can iterate."""
    fake_stats = {
        "sensor.day_cons": [
            _stat_row(2026, 1, 1, 12.0),
            _stat_row(2026, 1, 2, 11.0),
            _stat_row(2026, 1, 3, 9.5),
        ]
    }
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=fake_stats)
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await _recorder_daily_kwh(
            hass, "sensor.day_cons", date(2026, 1, 1), date(2026, 1, 3)
        )
    assert out == {
        date(2026, 1, 1): 12.0,
        date(2026, 1, 2): 11.0,
        date(2026, 1, 3): 9.5,
    }


async def test_recorder_requests_change_normalised_to_kwh(
    hass: HomeAssistant,
) -> None:
    """The recorder must be asked for the change in kWh so a Wh / MWh
    meter sensor is normalised by HA's EnergyConverter rather than read
    as raw kWh and bill the user 1000x off."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        await _recorder_daily_kwh(
            hass, "sensor.day_cons", date(2026, 1, 1), date(2026, 1, 3)
        )
    # statistics_during_period is positional: (..., period, units, types).
    call = instance.async_add_executor_job.call_args
    assert call.args[-2] == {"energy": "kWh"}
    assert call.args[-1] == {"change"}


async def test_recorder_daily_kwh_uses_change_not_sum(
    hass: HomeAssistant,
) -> None:
    """Regression: ``sum`` is the cumulative running total since the
    recorder started; reading it as a per-day delta multiplies the
    bill by however many years of meter history exist. The helper must
    only read ``change`` (the within-period delta)."""
    fake_stats = {
        "sensor.day_cons": [
            {
                "start": dt_util.start_of_local_day(datetime(2026, 1, 1))
                .astimezone(UTC)
                .timestamp(),
                "sum": 50_000.0,
                "change": 12.0,
            },
            {
                "start": dt_util.start_of_local_day(datetime(2026, 1, 2))
                .astimezone(UTC)
                .timestamp(),
                "sum": 50_012.0,
                "change": 11.0,
            },
        ]
    }
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=fake_stats)
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await _recorder_daily_kwh(
            hass, "sensor.day_cons", date(2026, 1, 1), date(2026, 1, 2)
        )
    assert out == {date(2026, 1, 1): 12.0, date(2026, 1, 2): 11.0}


async def test_recorder_daily_kwh_handles_dst_transitions(
    hass: HomeAssistant,
) -> None:
    """Brussels DST seams: spring forward (2026-03-29: 23-hour local
    day) and fall back (2026-10-25: 25-hour local day) must surface
    as one-row-per-local-day in the recorder helper. _recorder_rows
    walks +1 calendar day off start_of_local_day; the local-day
    binning at line 1348 (datetime.fromtimestamp(ts, UTC).as_local())
    is what guarantees the bucket lands on the right date even
    when UTC and local diverge by 1 hour mid-day."""
    spring_row = _stat_row(2026, 3, 29, 18.0)
    fall_row = _stat_row(2026, 10, 25, 22.0)
    fake_stats = {"sensor.day_cons": [spring_row, fall_row]}
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=fake_stats)
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await _recorder_daily_kwh(
            hass, "sensor.day_cons", date(2026, 3, 29), date(2026, 10, 25)
        )
    # Each day of the DST transition still maps to its own local date
    # in the output dict; the 23-hour and 25-hour anomalies don't
    # collapse two days onto one or split one day across two.
    assert out == {date(2026, 3, 29): 18.0, date(2026, 10, 25): 22.0}


async def test_recorder_daily_kwh_unknown_entity_returns_empty(
    hass: HomeAssistant,
) -> None:
    """An entity that the recorder doesn't track surfaces as an empty
    dict; the caller falls back to a fees-only floor instead of
    raising."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await _recorder_daily_kwh(
            hass, "sensor.does_not_exist", date(2026, 1, 1), date(2026, 5, 1)
        )
    assert out == {}


async def test_recorder_daily_kwh_swallows_recorder_errors(
    hass: HomeAssistant,
) -> None:
    """If the recorder isn't ready or the DB query raises, the helper
    returns an empty dict rather than propagating the exception. The
    coordinator's update can still complete from cached snapshots."""
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=RuntimeError("db down"))
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await _recorder_daily_kwh(
            hass, "sensor.day_cons", date(2026, 1, 1), date(2026, 5, 1)
        )
    assert out == {}


# ---- _measured_kwh (metered total plus how much of the window it covers) -----


async def test_measured_kwh_counts_days_across_a_register_pair(
    hass: HomeAssistant,
) -> None:
    """A day/night pair contributes a day when EITHER band reports it.

    The pair is two independent recorder series. Counting them separately and
    adding would double-count a day both bands cover, and taking one band's
    count alone would undercount a day only the other saw."""
    from types import SimpleNamespace

    entry = SimpleNamespace(
        data={
            "day_consumption_kwh": "sensor.day",
            "night_consumption_kwh": "sensor.night",
        }
    )
    d0 = date(2026, 1, 1)

    async def _fake(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        if entity_id == "sensor.day":
            return {d0: 4.0, d0 + timedelta(days=1): 4.0}
        # Overlaps the first day, adds a third.
        return {d0: 1.0, d0 + timedelta(days=2): 1.0}

    with patch.object(energy_meters, "_recorder_daily_kwh", new=_fake):
        got = await energy_meters._measured_kwh(hass, entry, d0, d0 + timedelta(days=2))  # type: ignore[arg-type]
    assert got.kwh == pytest.approx(10.0)
    assert got.days_with_data == 3


async def test_measured_kwh_counts_days_for_a_totals_sensor(
    hass: HomeAssistant,
) -> None:
    """The single cumulative wiring reports its own bucket count."""
    from types import SimpleNamespace

    entry = SimpleNamespace(data={"consumption_kwh": "sensor.total"})
    d0 = date(2026, 1, 1)

    async def _fake(
        _hass: object, _entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        return {d0 + timedelta(days=i): 2.0 for i in range(5)}

    with patch.object(energy_meters, "_recorder_daily_kwh", new=_fake):
        got = await energy_meters._measured_kwh(hass, entry, d0, d0 + timedelta(days=4))  # type: ignore[arg-type]
    assert got.kwh == pytest.approx(10.0)
    assert got.days_with_data == 5


async def test_measured_kwh_refuses_a_half_wired_register_pair(
    hass: HomeAssistant,
) -> None:
    """One half of a pair with no totals sensor reads nothing.

    Billing the wired band alone silently drops the other band's kWh. The old
    reader landed on the same answer by falling off the end of its if-chain;
    going through _partial_register_pair makes the refusal deliberate and keeps
    it aligned with the daily and hourly paths."""
    from types import SimpleNamespace

    entry = SimpleNamespace(data={"day_consumption_kwh": "sensor.day"})
    d0 = date(2026, 1, 1)

    async def _fake(
        _hass: object, _entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        return {d0: 99.0}

    with patch.object(energy_meters, "_recorder_daily_kwh", new=_fake):
        got = await energy_meters._measured_kwh(hass, entry, d0, d0)  # type: ignore[arg-type]
    assert got.kwh == 0.0
    assert got.days_with_data == 0


async def test_measured_kwh_separates_no_wiring_from_a_zero_reading(
    hass: HomeAssistant,
) -> None:
    """Zero kWh and no meter both sum to 0,0, and callers must tell them apart.

    A net-metered consumption register whose window nets to zero is a real
    reading; an entry with nothing wired is not. The day count is the only
    thing that separates them."""
    from types import SimpleNamespace

    d0 = date(2026, 1, 1)

    async def _zeros(
        _hass: object, _entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        return {d0 + timedelta(days=i): 0.0 for i in range(3)}

    wired_entry = SimpleNamespace(data={"consumption_kwh": "sensor.total"})
    bare_entry = SimpleNamespace(data={})
    with patch.object(energy_meters, "_recorder_daily_kwh", new=_zeros):
        wired = await energy_meters._measured_kwh(hass, wired_entry, d0, d0)  # type: ignore[arg-type]
        bare = await energy_meters._measured_kwh(hass, bare_entry, d0, d0)  # type: ignore[arg-type]
    assert wired.kwh == 0.0 and wired.days_with_data == 3
    assert bare.kwh == 0.0 and bare.days_with_data == 0


# ---- _live_today_kwh (running-day read straight off the meter) ---------------


def _midnight_instance(history: dict[str, list[State]]) -> MagicMock:
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value=history)
    return instance


def _meter_attrs(unit: str) -> dict[str, str]:
    return {
        "unit_of_measurement": unit,
        "device_class": "energy",
        "state_class": "total_increasing",
    }


async def test_live_today_kwh_returns_state_delta(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Today's kWh is the live cumulative reading minus the reading at
    local midnight."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "150.0", _meter_attrs("kWh"))
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh == 50.0


async def test_live_today_kwh_converts_wh_to_kwh(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A Wh meter is normalised to kWh, not read 1000x too high."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "150000.0", _meter_attrs("Wh"))
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100000.0")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh == 50.0


async def test_live_today_kwh_handles_meter_reset(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A total_increasing meter that reset since midnight bills everything
    it has counted since the reset, not a negative delta."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "5.0", _meter_attrs("kWh"))
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh == 5.0


async def test_live_today_kwh_total_meter_may_run_backwards(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A ``total`` register is allowed to fall, so a reading below midnight's
    is a net export, not a counter reset.

    The picker accepts any device_class=energy sensor, so a utility_meter with
    net_consumption or a bidirectional register is a legitimate choice. Reading
    its decrease as a reset billed the meter's whole lifetime total as one
    day's consumption: a 12350 kWh register that exported 4.5 kWh reported
    12345.6 kWh, roughly 4300 EUR onto current_year_cost. The signed delta is
    also exactly what the recorder reports as that day's ``change``, so past
    days and today stay on the same basis."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "12345.6",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total",
        },
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "12350.1")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh == pytest.approx(-4.5)


async def test_live_today_kwh_reset_substitution_needs_total_increasing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A meter that publishes no state class gets the signed delta too: the
    reset reading is only safe for the one class that cannot decrease."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "5.0",
        {"unit_of_measurement": "kWh", "device_class": "energy"},
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh == pytest.approx(-95.0)


async def test_live_today_kwh_honours_a_cycle_reset_on_a_total_meter(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A utility_meter with net_consumption reports state_class TOTAL (HA
    returns one class or the other on exactly that option) and still cycles.

    On the rollover day its state at midnight is the whole previous cycle, so
    a plain delta returns minus that cycle as today's kWh. The published
    last_reset is what says a new cycle started, and it is the signal that
    survives both classes."""
    freezer.move_to("2026-08-01 10:00:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "4.2",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total",
            "last_reset": "2026-08-01T00:00:00+02:00",
        },
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "312.4")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 8, 1))
    assert kwh == pytest.approx(4.2)


async def test_live_today_kwh_ignores_a_stale_last_reset(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A last_reset from a previous cycle must not turn an ordinary net
    export into the whole meter reading."""
    freezer.move_to("2026-08-16 14:00:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "12345.6",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total",
            "last_reset": "2026-08-01T00:00:00+02:00",
        },
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "12350.1")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 8, 16))
    assert kwh == pytest.approx(-4.5)


async def test_top_up_today_hourly_adds_the_uncompiled_remainder(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The hourly branch reads compiled statistics only, so every
    hourly-billed contract stepped once an hour at best and froze when
    compilation stalled. Top today up from the live meter, attributing the
    shortfall to the current hour, where the missing energy actually was."""
    from custom_components.be_electricity_prices.energy_meters import (
        _top_up_today_hourly,
    )

    freezer.move_to("2026-07-16 14:30:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "160.0",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    midnight = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)  # 2026-07-16 local
    per_hour = {
        datetime(2026, 7, 14, 10, tzinfo=UTC): 5.0,  # yesterday, untouched
        midnight: 40.0,  # today, already compiled
    }
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        await _top_up_today_hourly(hass, ["sensor.meter"], per_hour, date(2026, 7, 16))
    # live today = 160 - 100 = 60; statistics carry 40; the missing 20 lands
    # on the current hour.
    current = datetime(2026, 7, 16, 12, tzinfo=UTC)
    assert per_hour[current] == pytest.approx(20.0)
    assert per_hour[midnight] == pytest.approx(40.0)
    assert per_hour[datetime(2026, 7, 14, 10, tzinfo=UTC)] == pytest.approx(5.0)


async def test_top_up_today_hourly_leaves_caught_up_statistics_alone(
    hass: HomeAssistant, freezer: Any
) -> None:
    """When statistics already carry today's whole total there is nothing to
    add, and a meter that ran backwards must not invent a negative hour."""
    from custom_components.be_electricity_prices.energy_meters import (
        _top_up_today_hourly,
    )

    freezer.move_to("2026-07-16 14:30:00+02:00")
    hass.states.async_set(
        "sensor.meter",
        "150.0",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total_increasing",
        },
    )
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    midnight = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
    per_hour = {midnight: 50.0}
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        await _top_up_today_hourly(hass, ["sensor.meter"], per_hour, date(2026, 7, 16))
    assert per_hour == {midnight: pytest.approx(50.0)}


async def test_top_up_today_hourly_without_a_live_reading_is_a_no_op(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An unavailable meter leaves the statistics figure standing, exactly as
    the per-day path degrades."""
    from custom_components.be_electricity_prices.energy_meters import (
        _top_up_today_hourly,
    )

    freezer.move_to("2026-07-16 14:30:00+02:00")
    hass.states.async_set("sensor.meter", "unavailable", _meter_attrs("kWh"))
    midnight = datetime(2026, 7, 15, 22, 0, tzinfo=UTC)
    per_hour = {midnight: 12.0}
    await _top_up_today_hourly(hass, ["sensor.meter"], per_hour, date(2026, 7, 16))
    assert per_hour == {midnight: pytest.approx(12.0)}


async def test_live_today_kwh_none_when_unavailable(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An unavailable meter yields None so the caller keeps the statistic."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "unavailable", _meter_attrs("kWh"))
    kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh is None


async def test_live_today_kwh_none_on_unconvertible_unit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An unknown unit falls back (None) rather than risk a wrong figure."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "150.0", _meter_attrs("widgets"))
    inst = _midnight_instance({"sensor.meter": [State("sensor.meter", "100.0")]})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh is None


async def test_live_today_kwh_none_without_midnight_reading(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No recorded reading at midnight yet -> None (fall back)."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    hass.states.async_set("sensor.meter", "150.0", _meter_attrs("kWh"))
    inst = _midnight_instance({})
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        kwh = await _live_today_kwh(hass, "sensor.meter", date(2026, 7, 16))
    assert kwh is None


async def test_recorder_daily_kwh_overrides_today_with_live(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Today is billed from the live meter delta, not the (lagging) daily
    statistic, so the running cost tracks today's usage in real time."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    today = date(2026, 7, 16)
    stats = {"sensor.meter": [_stat_row(2026, 7, 15, 8.0), _stat_row(2026, 7, 16, 2.0)]}
    hass.states.async_set("sensor.meter", "150.0", _meter_attrs("kWh"))
    inst = MagicMock()
    inst.async_add_executor_job = AsyncMock(
        side_effect=[stats, {"sensor.meter": [State("sensor.meter", "100.0")]}]
    )
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        out = await _recorder_daily_kwh(hass, "sensor.meter", date(2026, 1, 1), today)
    assert out[date(2026, 7, 15)] == 8.0  # settled past day from the statistic
    assert out[today] == 50.0  # today from the live 150 - 100 delta, not 2.0


async def test_recorder_daily_kwh_keeps_statistic_when_live_none(
    hass: HomeAssistant, freezer: Any
) -> None:
    """When the live read is unavailable, today falls back to the daily
    statistic rather than dropping to zero."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    today = date(2026, 7, 16)
    stats = {"sensor.meter": [_stat_row(2026, 7, 16, 2.0)]}
    hass.states.async_set("sensor.meter", "unavailable", _meter_attrs("kWh"))
    inst = MagicMock()
    inst.async_add_executor_job = AsyncMock(return_value=stats)
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        out = await _recorder_daily_kwh(hass, "sensor.meter", date(2026, 1, 1), today)
    assert out[today] == 2.0


async def test_recorder_daily_kwh_no_live_override_for_past_end(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A window that doesn't end today never reads the live meter (the
    compare / diagnostics callers pass historical ranges)."""
    freezer.move_to("2026-07-16 10:00:00+02:00")
    stats = {"sensor.meter": [_stat_row(2026, 5, 1, 3.0)]}
    hass.states.async_set("sensor.meter", "150.0", _meter_attrs("kWh"))
    inst = MagicMock()
    inst.async_add_executor_job = AsyncMock(return_value=stats)
    with patch("homeassistant.components.recorder.get_instance", return_value=inst):
        out = await _recorder_daily_kwh(
            hass, "sensor.meter", date(2026, 1, 1), date(2026, 5, 1)
        )
    assert out == {date(2026, 5, 1): 3.0}
    assert inst.async_add_executor_job.call_count == 1  # no second (history) call


# ---- _snapshot_for_month -----------------------------------------------------


def _archive_snapshot(label: str) -> SupplierSnapshot:
    return make_snapshot(
        energy=FixedRates(single=0.20),
        source_url=f"test://{label}",
        publication_label=label,
    )


async def test_snapshot_for_month_uses_archive_when_available(
    hass: HomeAssistant,
) -> None:
    """When an extractor exposes fetch_for_month and it returns a real
    snapshot, _snapshot_for_month must surface that snapshot and cache
    it - subsequent calls for the same month do not refetch."""

    archived = _archive_snapshot("2026-01")
    current = _archive_snapshot("2026-04")
    fetch_calls = 0

    async def _fake_fetch_for_month(*_args: object, **_kw: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return archived

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),  # unused
        fetch_for_month=_fake_fetch_for_month,
    )
    _monthly_snapshots(hass).clear()
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2026, 1, 1), current
    )
    assert snap is archived
    # Second call: cache hit, no extra fetch.
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2026, 1, 1), current
    )
    assert snap is archived
    assert fetch_calls == 1


async def test_snapshot_for_month_reasks_a_row_that_can_still_move(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A cached month that is not yet a historical fact must expire.

    Two rows can still change after they are cached. The running month's card
    can be republished or corrected mid-month, which this repo treats as
    normal. And a cached "no archive here" only means the card was not out at
    the moment it was asked: a supplier publishing in arrears turns that into
    a real card days later. Neither had a TTL, so the live price moved within
    a day while the year-to-date and every backfilled row kept billing the
    vintage first cached at startup, for the life of the HA process.

    A parsed card for a month that has CLOSED is a historical fact and must
    stay cached, which is what the archive cache exists for."""

    current = _archive_snapshot("current")
    published: list[SupplierSnapshot | None] = [None]
    fetch_calls = 0

    async def _fake_fetch(*_a: object, **_kw: object) -> SupplierSnapshot | None:
        nonlocal fetch_calls
        fetch_calls += 1
        return published[0]

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_fake_fetch,
    )

    async def _ask(month: date) -> SupplierSnapshot:
        return await _snapshot_for_month(
            hass, MagicMock(), extractor, "test", "wallonia", month, current
        )

    # 1. Not published yet -> falls back to the current card and caches that.
    freezer.move_to("2026-06-02 09:00:00+02:00")
    _monthly_snapshots(hass).clear()
    _monthly_fetched_at(hass).clear()
    assert await _ask(date(2026, 5, 1)) is current
    assert await _ask(date(2026, 5, 1)) is current
    assert fetch_calls == 1  # held inside the TTL

    # The definitive card lands a few days later, as Ecopower's do.
    published[0] = _archive_snapshot("2026-05")
    freezer.move_to("2026-06-04 09:00:00+02:00")
    assert await _ask(date(2026, 5, 1)) is published[0]
    assert fetch_calls == 2

    # 2. And once that month is closed and parsed, it never moves again.
    freezer.move_to("2026-06-20 09:00:00+02:00")
    assert await _ask(date(2026, 5, 1)) is published[0]
    assert fetch_calls == 2

    # 3. The RUNNING month is re-asked, because its card can be corrected.
    _monthly_snapshots(hass).clear()
    _monthly_fetched_at(hass).clear()
    fetch_calls = 0
    published[0] = _archive_snapshot("2026-06-draft")
    freezer.move_to("2026-06-20 09:00:00+02:00")
    assert await _ask(date(2026, 6, 1)) is published[0]
    corrected = _archive_snapshot("2026-06-corrected")
    published[0] = corrected
    freezer.move_to("2026-06-22 09:00:00+02:00")
    assert await _ask(date(2026, 6, 1)) is corrected
    assert fetch_calls == 2


async def test_snapshot_for_month_falls_back_to_current_when_no_archive(
    hass: HomeAssistant,
) -> None:
    """An extractor without fetch_for_month, or one whose fetch_for_month
    returns None for the requested month, must transparently fall back
    to the current snapshot as a proxy."""

    current = _archive_snapshot("2026-04")
    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=None,  # non-archive supplier
    )
    _monthly_snapshots(hass).clear()
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2026, 1, 1), current
    )
    assert snap is current

    async def _none_fetch(*_args: object, **_kw: object) -> SupplierSnapshot | None:
        return None

    extractor2 = SupplierExtractor(
        id="test2",
        label="Test2",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_none_fetch,
    )
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor2, "test", "wallonia", date(2025, 6, 1), current
    )
    assert snap is current


async def test_snapshot_for_month_caches_negative_results(
    hass: HomeAssistant,
) -> None:
    """A None response from fetch_for_month must be cached so we don't
    refetch the same missing month every coordinator tick."""

    current = _archive_snapshot("2026-04")
    fetch_calls = 0

    async def _none_fetch(*_args: object, **_kw: object) -> SupplierSnapshot | None:
        nonlocal fetch_calls
        fetch_calls += 1
        return None

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_none_fetch,
    )
    _monthly_snapshots(hass).clear()
    await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2024, 6, 1), current
    )
    await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2024, 6, 1), current
    )
    assert fetch_calls == 1


async def test_snapshot_for_month_does_not_cache_transient_failures(
    hass: HomeAssistant,
) -> None:
    """A raised exception during fetch_for_month must NOT poison the
    positive cache: caching the failure as None would mean the same
    wording as 'supplier doesn't archive this month' and the entry
    would serve uncredited rates for that month until the next reload.
    Once the negative-cache TTL elapses the next refresh retries and
    a successful retry populates the positive cache normally."""
    from custom_components.be_electricity_prices.snapshot_store import (
        _monthly_failed_fetches,
    )

    current = _archive_snapshot("2026-04")
    archived = _archive_snapshot("2024-06")
    call = 0

    async def _flaky(*_args: object, **_kw: object) -> SupplierSnapshot:
        nonlocal call
        call += 1
        if call == 1:
            raise RuntimeError("transient network blip")
        return archived

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_flaky,
    )
    _monthly_snapshots(hass).clear()
    _monthly_failed_fetches(hass).clear()
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2024, 6, 1), current
    )
    # First call raised: falls back to current snapshot, but the
    # positive cache must NOT have been populated.
    assert snap is current
    cache_key = ("test", "test", "wallonia", "2024-06")
    assert cache_key not in _monthly_snapshots(hass)
    # Failure marker WAS recorded; the next call returns the proxy
    # without re-attempting the fetch (avoids the hourly fan-out
    # against a flaky supplier).
    assert cache_key in _monthly_failed_fetches(hass)
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2024, 6, 1), current
    )
    assert snap is current
    assert call == 1
    # Clear the failure marker (TTL elapsed) and the next call
    # succeeds and is cached.
    _monthly_failed_fetches(hass).clear()
    snap = await _snapshot_for_month(
        hass, MagicMock(), extractor, "test", "wallonia", date(2024, 6, 1), current
    )
    assert snap is archived
    assert call == 2
    assert _monthly_snapshots(hass)[cache_key] is archived


# ---- _compute_current_year_cost (recorder-driven) -----------------------------


def _yearly_snapshot() -> SupplierSnapshot:
    """Snapshot with single=0.18 + dist=0.10 + transport=0.0145 + WAL taxes."""
    return make_snapshot(
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
        ),
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


def _year_fraction(today: date) -> float:
    """Mirror the production proration formula so tests don't have to
    re-derive it."""
    days_in_year = 366 if calendar.isleap(today.year) else 365
    elapsed_days = (today - date(today.year, 1, 1)).days + 1
    return elapsed_days / days_in_year


def _expected_prosumer_ytd(monthly_fee: float, today: date) -> float:
    """Mirror _ytd_prosumer's per-month sum so tests can assert it."""
    total = 0.0
    cur = date(today.year, 1, 1)
    while cur <= today:
        if cur.month == 12:
            next_first = date(cur.year + 1, 1, 1)
        else:
            next_first = date(cur.year, cur.month + 1, 1)
        days_in_full_month = (next_first - date(cur.year, cur.month, 1)).days
        month_end_in_ytd = min(next_first - timedelta(days=1), today)
        days_in_ytd = (month_end_in_ytd - cur).days + 1
        total += monthly_fee * (days_in_ytd / days_in_full_month)
        cur = next_first
    return total


def _stub_extractor() -> SupplierExtractor:
    return make_stub_extractor()


def _patch_recorder_per_entity(
    per_entity_per_day: dict[str, dict[date, float]],
) -> Any:
    """Patch _recorder_daily_kwh to return the configured per-day
    sums per entity_id; raise via empty dict for unmapped entities."""

    async def _fake(
        hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        return dict(per_entity_per_day.get(entity_id, {}))

    return patch.object(energy_meters, "_recorder_daily_kwh", new=_fake)


async def test_year_cost_recorder_driven_mono_no_solar(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Recorder returns 5 kWh / day from Jan 1 to today; mono no-solar
    bills it at the single all-in rate. Total = 5 * elapsed_days * 0.3765."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = _yearly_snapshot()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    today = dt_util.now().date()
    days = _days_through(date(today.year, 1, 1), today)
    per_day = {d: 5.0 for d in days}
    with _patch_recorder_per_entity(
        {
            "sensor.day_cons": per_day,
            "sensor.night_cons": {},
            "sensor.day_inj": {},
            "sensor.night_inj": {},
        }
    ):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
        )
    expected = 5.0 * len(days) * 0.3765
    assert cost == pytest.approx(expected)


async def test_year_cost_compensation_clamps_when_inj_exceeds_cons(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Compensation regime with day-by-day over-injection: YTD energy
    cost clamps at zero (no negative bill), so only the fees remain.
    The fixed yearly + energy-fund pieces pro-rate to the elapsed
    fraction of the year; the prosumer fee is summed per archived
    month."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.18, yearly_fixed_fee=65.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=24.0,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.0, energy_contribution=0.0, energy_fund_eur_per_month=2.5
        ),
    )
    entry = _yearly_entry(meter="mono", solar_regime="compensation", solar_kva=2.0)
    today = dt_util.now().date()
    days = _days_through(date(today.year, 1, 1), today)
    cons_per_day = {d: 5.0 for d in days}
    inj_per_day = {d: 25.0 for d in days}  # over-produces every day
    with _patch_recorder_per_entity(
        {"sensor.day_cons": cons_per_day, "sensor.day_inj": inj_per_day}
    ):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
        )
    # YTD energy cost = max(Σ(5 - 25) * X, 0) = 0. Fees only.
    fraction = _year_fraction(today)
    monthly_prosumer = 2.0 * 24.0 / 12.0  # = 4 EUR/month
    expected_prosumer = _expected_prosumer_ytd(monthly_prosumer, today)
    assert cost == pytest.approx((65.0 + 12 * 2.5) * fraction + expected_prosumer)


async def test_year_cost_breakdown_exposes_clamped_energy(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The optional ``breakdown`` out-dict records the YTD/today kWh, the
    pre-clamp raw energy term and the fees floor, so a flat sensor sitting on
    the compensation zero-floor can be told apart from a stalled meter: the
    raw energy is negative (hidden by the clamp) while the value equals fees."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.18, yearly_fixed_fee=65.0),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                prosumer_eur_per_kva_year=24.0,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.0, energy_contribution=0.0, energy_fund_eur_per_month=2.5
        ),
    )
    entry = _yearly_entry(meter="mono", solar_regime="compensation", solar_kva=2.0)
    today = dt_util.now().date()
    days = _days_through(date(today.year, 1, 1), today)
    cons_per_day = {d: 5.0 for d in days}
    inj_per_day = {d: 25.0 for d in days}  # over-produces every day
    breakdown: dict[str, float] = {}
    with _patch_recorder_per_entity(
        {"sensor.day_cons": cons_per_day, "sensor.day_inj": inj_per_day}
    ):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            breakdown=breakdown,
        )
    assert breakdown["consumption_ytd_kwh"] == pytest.approx(5.0 * len(days))
    assert breakdown["injection_ytd_kwh"] == pytest.approx(25.0 * len(days))
    assert breakdown["consumption_today_kwh"] == pytest.approx(5.0)
    assert breakdown["injection_today_kwh"] == pytest.approx(25.0)
    # Raw energy is negative (25 kWh injected vs 5 kWh drawn each day); the
    # clamp hides it, so the billed value is the fees floor.
    assert breakdown["energy_ytd_raw_eur"] < 0.0
    assert breakdown["fees_ytd_eur"] == pytest.approx(cost)


async def test_year_cost_uses_per_month_snapshot_when_archive_available(
    hass: HomeAssistant, freezer: Any
) -> None:
    """When fetch_for_month returns a different snapshot for a past
    month, the year-cost loop must apply that month's rate to **its**
    days -- not today's snapshot rate to everything."""

    # Pin a mid-year Brussels date so the test always covers at least
    # four past months regardless of when the suite is run; the
    # previous skip-on-January meant the per-month archive replay
    # branch was uncovered for an entire calendar month every year.
    freezer.move_to("2026-05-15 12:00:00+02:00")
    today = dt_util.now().date()

    cheap = make_snapshot(energy=FixedRates(single=0.10), source_url="test://cheap")
    expensive = make_snapshot(
        energy=FixedRates(single=0.30), source_url="test://expensive"
    )
    jan_first = date(today.year, 1, 1)

    async def _fake_fetch_for_month(
        _session: object, _contract: str, _region: str, year_month: date
    ) -> SupplierSnapshot:
        # January gets the cheap card, every later month falls back to
        # the proxy snapshot (the "expensive" one passed to the helper).
        return cheap if year_month == jan_first else None  # type: ignore[return-value]

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_fake_fetch_for_month,
    )
    _monthly_snapshots(hass).clear()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    days = _days_through(jan_first, today)
    cons_per_day = {d: 5.0 for d in days}
    with _patch_recorder_per_entity({"sensor.day_cons": cons_per_day}):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            extractor,
            expensive,
            entry,
        )
    cheap_all_in = 0.10 + 0.10 + 0.0145 + 0.05 + 0.002
    expensive_all_in = 0.30 + 0.10 + 0.0145 + 0.05 + 0.002
    jan_days = sum(1 for d in days if d.month == 1)
    other_days = len(days) - jan_days
    expected = 5.0 * cheap_all_in * jan_days + 5.0 * expensive_all_in * other_days
    assert cost == pytest.approx(expected)


async def test_year_cost_falls_back_to_fees_when_no_meters_configured(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A config without any meter sensors surfaces the fees-only
    floor instead of zero - the user has to wire up at least one
    consumption sensor for the recorder path to produce a number."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.18, yearly_fixed_fee=65.0),
        taxes=TaxOverlay(
            federal_excise=0.0, energy_contribution=0.0, energy_fund_eur_per_month=2.5
        ),
    )
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
    cost = await _compute_current_year_cost(
        hass,
        None,  # type: ignore[arg-type]
        _stub_extractor(),
        snap,
        entry,
    )
    # Fees-only: (65 + 12*2.5) * elapsed-year-fraction.
    fraction = _year_fraction(dt_util.now().date())
    assert cost == pytest.approx(95.0 * fraction)


async def test_year_cost_skips_month_when_archived_snapshot_lacks_dso(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An archived month-snapshot whose DSO row regex missed for the
    user's DSO must not crash the YTD tick. The month falls back to
    "no rate to apply" (like dynamic/TOU) and the loop keeps going."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    today = dt_util.now().date()
    jan_first = date(today.year, 1, 1)

    # Archived snapshot for January is missing the user's DSO key
    # entirely, simulating a regex drift on the historical card.
    bad_archive = make_snapshot(
        energy=FixedRates(single=0.10),
        dsos={},  # no DSO at all
        source_url="test://bad",
    )
    current = make_snapshot(energy=FixedRates(single=0.30), source_url="test://current")

    async def _fake_fetch_for_month(
        _session: object, _contract: str, _region: str, year_month: date
    ) -> SupplierSnapshot:
        return bad_archive if year_month == jan_first else None  # type: ignore[return-value]

    extractor = SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=_fake_fetch_for_month,
    )
    _monthly_snapshots(hass).clear()
    entry = _yearly_entry(meter="mono", solar_regime="none")
    days = _days_through(jan_first, today)
    cons_per_day = {d: 5.0 for d in days}
    with _patch_recorder_per_entity({"sensor.day_cons": cons_per_day}):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            extractor,
            current,
            entry,
        )
    # January's days are skipped (no DSO in bad archive), the rest
    # bills at the current snapshot's rate.
    current_all_in = 0.30 + 0.10 + 0.0145 + 0.05 + 0.002
    other_days = sum(1 for d in days if d.month != 1)
    expected = 5.0 * current_all_in * other_days
    assert cost == pytest.approx(expected)


async def test_year_cost_tou_bills_per_hourly_slot(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Time-of-Use contracts must read hourly recorder data and apply
    the supplier's slot rate per hour, not the fees-only floor (which
    is what the per-day path returned before)."""

    # Pin a date well past the 2026-01-06 peak hour the test injects so
    # the YTD window always covers it (early-January runs would
    # otherwise put today before that hour and the recorder data would
    # fall outside the [Jan 1, today] window).
    freezer.move_to("2026-05-15 12:00:00+02:00")
    today = dt_util.now().date()

    snap = make_snapshot(
        contract="test_tou",
        energy=TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10),
        source_url="test://tou",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_tou",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )

    # One hour at 09:00 local Tuesday (Jan 6 2026 is a Tuesday) -- TOU peak.
    peak_hour = dt_util.start_of_local_day(datetime(2026, 1, 6)) + timedelta(hours=9)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {peak_hour.astimezone(UTC): 1.0}
        return {}

    expected_all_in = (0.30 + 0.10 + 0.0145 + 0.05 + 0.002) * 1.0  # vat_factor 1.0

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
        )
    fraction = _year_fraction(today)
    # Energy = 1 kWh × all-in (peak slot); fees pro-rated (zero here).
    assert cost == pytest.approx(expected_all_in + 0.0 * fraction)


async def test_year_cost_exclusive_night_uses_exclusive_night_rate(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An exclusive_night meter on a static contract must bill the YTD at
    the dedicated exclusive-night energy + distribution rates (via the
    hourly path), not the higher day/single rate the static per-day
    branch would apply. The live current_price sensor already uses the
    exclusive-night rate, so the year-cost must match it."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_excl",
        energy=FixedRates(single=0.18, exclusive_night=0.12),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                distribution_exclusive_night=0.06,
                transport=0.0145,
            )
        },
        source_url="test://excl",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_excl",
            "region": "wallonia",
            "dso": "ores",
            "meter": "exclusive_night",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
        },
    )
    some_hour = dt_util.start_of_local_day(datetime(2026, 1, 6)) + timedelta(hours=3)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {some_hour.astimezone(UTC): 1.0}
        return {}

    # exclusive-night all-in: energy 0.12 + (dist_excl 0.06 + transport
    # 0.0145) + taxes (0.05 + 0.002) = 0.2465, well below the 0.3465 a
    # mono meter on the same card would bill.
    excl_all_in = 0.12 + 0.06 + 0.0145 + 0.05 + 0.002
    mono_all_in = 0.18 + 0.10 + 0.0145 + 0.05 + 0.002
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
        )
    assert cost == pytest.approx(excl_all_in)
    assert excl_all_in < mono_all_in


async def test_year_cost_tou_recognises_injection_only_wiring(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Injection-regime user on a TOU contract who only wired the
    injection sensor (e.g. inverter exposing solar export but no smart-
    meter consumption sensor) must still see their solar credit
    accrue, mirroring the static-path behaviour."""

    # Pin past the 2026-01-06 injection hour the fixture injects.
    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_tou",
        energy=TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10),
        source_url="test://tou",
        injection=InjectionRates(current=0.05),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_tou",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
            "injection_kwh": "sensor.inj_total",
            # No consumption sensor wired -- only injection.
            "dso_tariff_mode": "bi_horaire",
        },
    )

    # 1 kWh injected at 13:00 local Tuesday Jan 6 2026 (TOU transition).
    inj_hour = dt_util.start_of_local_day(datetime(2026, 1, 6)) + timedelta(hours=13)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.inj_total":
            return {inj_hour.astimezone(UTC): 1.0}
        return {}

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
        )
    # Energy = 0 (no consumption) - 1 kWh × 0.05 (injection rate).
    # Fees pro-rated to zero (no yearly_fixed_fee, no energy_fund,
    # not on compensation regime so no prosumer).
    assert cost == pytest.approx(-0.05)


async def test_year_cost_dynamic_replays_historical_spots(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Dynamic contracts must replay historical hourly ENTSO-E spots
    via the coordinator's persistent cache. With one consumed kWh at
    a known UTC hour and a known spot for that hour, the YTD cost
    must equal the ``factor*spot+base`` rate * 1 kWh + the DSO/tax
    overlay -- no longer the fees-only floor that v1 returned."""

    # Pin past the 2026-01-06 spot hour the fixture injects.
    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0),
        source_url="test://dyn",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )
    spot_hour = datetime(2026, 1, 6, 13, 0, tzinfo=UTC)
    historical_spots = {spot_hour: 0.20}

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {spot_hour: 1.0}
        return {}

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots=historical_spots,
        )
    # factor*spot + base = 1.0*0.20 + 0 = 0.20 EUR/kWh energy
    # + 0.10 distribution + 0.0145 transport + 0.05 + 0.002 taxes
    # = 0.3665 EUR/kWh on 1 kWh; no fees on the stub snapshot.
    assert cost == pytest.approx(0.20 + 0.10 + 0.0145 + 0.052)


async def test_year_cost_spot_injection_credited_on_needs_hourly_path(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A spot-indexed-injection contract (Cociter Variable) that also
    takes the needs_hourly path -- here via DSO Impact mode -- must still
    get its per-hour spot-replayed injection credit, not silently drop it
    as the daily-only credit would."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="cociter_variable",
        energy=VariableRates(current=0.16),
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                distribution_pic=0.10,
                distribution_medium=0.07,
                distribution_eco=0.03,
            )
        },
        injection=InjectionRates(factor=0.9, base=-0.01, current=None),
        source_url="test://cv",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_variable",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "dso_tariff_mode": "impact",  # -> needs_hourly path
            "solar_regime": "injection",
            "injection_kwh": "sensor.inj_total",
        },
    )
    inj_hour = dt_util.start_of_local_day(datetime(2026, 1, 6)).astimezone(
        UTC
    ) + timedelta(hours=13)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        return {inj_hour: 4.0} if entity_id == "sensor.inj_total" else {}

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots={inj_hour: 0.10},
        )
    # No consumption; pure injection credit 4 * (0.9*0.10 - 0.01) = 0.32,
    # so the YTD is -0.32 (a net credit). Without the needs_hourly fix it
    # would be 0.0 (credit dropped).
    assert cost == pytest.approx(-(4.0 * (0.9 * 0.10 - 0.01)))


async def test_year_cost_dynamic_falls_back_to_fees_when_no_spots(
    hass: HomeAssistant, freezer: Any
) -> None:
    """When the historical-spots cache is empty (cold start, ENTSO-E
    fetch failed entirely), the dynamic YTD must still produce the
    fees-only floor rather than crashing or returning None."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0, yearly_fixed_fee=120.0),
        source_url="test://dyn",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
        },
    )

    async def _fake_hourly(
        _hass: object, _entity: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        return {}

    today = dt_util.now().date()
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots={},
        )
    # Fees-only floor: yearly_fixed_fee=120 pro-rated by elapsed
    # fraction of year. Within a EUR rounding tolerance.
    assert cost is not None
    assert cost == pytest.approx(120.0 * _year_fraction(today), abs=0.01)


async def test_year_cost_bills_network_and_taxes_for_an_hour_with_no_spot(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An hour the spot cache cannot price keeps its network and tax legs.

    Those two are known from the month's snapshot and do not depend on the
    day-ahead price, so dropping the hour whole understated the bill by far
    more than the energy it could not resolve: on the stub overlay the
    non-energy legs are 0,1665 of a 0,3665 EUR/kWh all-in rate."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0),
        source_url="test://dyn",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )
    priced = datetime(2026, 1, 6, 13, 0, tzinfo=UTC)
    unpriced = datetime(2026, 1, 7, 13, 0, tzinfo=UTC)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {priced: 1.0, unpriced: 1.0}
        return {}

    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots={priced: 0.20},
        )
    overlay = 0.10 + 0.0145 + 0.052  # distribution + transport + taxes
    # The priced hour bills energy + overlay; the unpriced one bills the
    # overlay alone rather than nothing at all.
    assert cost == pytest.approx((0.20 + overlay) + overlay)


async def test_year_cost_spot_monthly_bills_overlay_for_an_uncached_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Same rule on the spot-monthly path, where a whole month goes at once.

    ``_month_mean`` returns None for a month with no cached hours, which used
    to discard every hour of it. energie.be Variabel and the Mega groepsaankoop
    both bill this way, and a fresh entry fetching a year of ENTSO-E hits it."""

    freezer.move_to("2026-03-15 12:00:00+01:00")
    snap = make_snapshot(
        contract="test_spot_monthly",
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        source_url="test://sm",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_spot_monthly",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    feb = datetime(2026, 2, 15, 12, 0, tzinfo=UTC)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {jan: 1.0, feb: 1.0}
        return {}

    # February fully cached (the coverage gate must be satisfied for its mean
    # to be billable); January absent entirely.
    feb_spots = {
        datetime(2026, 2, 1, 0, 0, tzinfo=UTC) + timedelta(hours=h): 0.20
        for h in range(28 * 24)
    }
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots=feb_spots,
        )
    overlay = 0.10 + 0.0145 + 0.052
    assert cost == pytest.approx((0.20 + overlay) + overlay)


async def test_year_cost_hourly_path_reports_spot_coverage(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The hourly path must say how much of the window it could price.

    A YTD that is low because the spot cache is thin is indistinguishable
    from a correct low one, and this path used to publish no diagnostics at
    all -- so the contracts that CAN under-report were the ones with nothing
    to inspect."""

    freezer.move_to("2026-05-15 12:00:00+02:00")
    snap = make_snapshot(
        contract="test_dynamic",
        energy=DynamicRates(factor=1.0, base=0.0),
        source_url="test://dyn",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )
    priced = datetime(2026, 1, 6, 13, 0, tzinfo=UTC)
    unpriced = datetime(2026, 1, 7, 13, 0, tzinfo=UTC)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {priced: 1.0, unpriced: 2.0}
        return {}

    diag: dict[str, float] = {}
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots={priced: 0.20},
            breakdown=diag,
        )
    assert diag["hours_seen"] == 2.0
    assert diag["hours_priced"] == 1.0
    assert diag["consumption_ytd_kwh"] == pytest.approx(3.0)
    # Reported on this path too, not just the static one.
    assert "fees_ytd_eur" in diag


async def test_year_cost_rejects_a_thinly_cached_closed_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A closed month cached too thinly must not price its every hour.

    ``_month_mean`` applies the mean of whatever is cached to the whole month,
    so one cheap January hour used to bill all of January at that rate -- a
    confident wrong number. Below the coverage floor the month falls through
    to the network-and-taxes path instead, which bills what is known."""

    freezer.move_to("2026-03-15 12:00:00+01:00")
    snap = make_snapshot(
        contract="test_spot_monthly",
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        source_url="test://sm",
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "test",
            "contract": "test_spot_monthly",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "consumption_kwh": "sensor.cons_total",
            "dso_tariff_mode": "bi_horaire",
        },
    )
    billed = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.cons_total":
            return {billed: 1.0}
        return {}

    # One cached January hour, far below 0.8 * 31 * 24.
    thin = {datetime(2026, 1, 2, 3, 0, tzinfo=UTC): 0.01}
    with patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots=thin,
        )
    overlay = 0.10 + 0.0145 + 0.052
    # Not 0.01 + overlay: the sparse mean is refused rather than believed.
    assert cost == pytest.approx(overlay)


def test_covered_month_mean_trusts_the_running_month(freezer: Any) -> None:
    """The current month is partial by definition, so it keeps its mean.

    Gating it on coverage would leave a fresh entry with no energy price at
    all for the month it is actually living in."""

    freezer.move_to("2026-03-15 12:00:00+01:00")
    today = dt_util.now().date()
    bucket = _bucket_by_local_month({datetime(2026, 3, 2, 3, 0, tzinfo=UTC): 0.05})
    # One hour of March, the running month: trusted.
    assert _covered_month_mean(bucket, 2026, 3, today) == pytest.approx(0.05)
    # The same sparseness in closed February: refused.
    feb = _bucket_by_local_month({datetime(2026, 2, 2, 3, 0, tzinfo=UTC): 0.05})
    assert _covered_month_mean(feb, 2026, 2, today) is None


def test_energy_kind_handles_tou() -> None:
    """Regression for Round-2 Bug 1: TimeOfUseRates was missing from the
    energy-kind classifier so persistence raised TypeError on TOU."""
    assert (
        _energy_kind(TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10)) == "tou"
    )


def test_snapshot_round_trip_for_tou_contract() -> None:
    """A TOU snapshot must serialize and deserialize without raising,
    so HA's Store can persist last-known prices for SmartFlex /
    Empower Flextime users."""
    snap = make_snapshot(
        supplier="luminus",
        contract="smartflex",
        energy=TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.10),
        source_url="test://tou",
    )
    fetched_at = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)
    payload = _snapshot_to_dict(snap, fetched_at, probe_key="abc")
    restored = _snapshot_from_dict(payload)
    assert isinstance(restored.energy, TimeOfUseRates)
    assert restored.energy.peak == pytest.approx(0.30)
    assert restored.energy.transition == pytest.approx(0.20)
    assert restored.energy.offpeak == pytest.approx(0.10)


def test_a_cache_from_an_older_schema_is_discarded() -> None:
    """The persisted snapshot holds the card AS PARSED, and the probe path has
    no TTL, so an extractor fix reaches an existing entry only when its
    supplier republishes -- up to a month later -- unless the schema version
    moves with it.

    This is the trap 0.11.37 fell into: three extractor value fixes shipped
    against an unchanged version 17, so Ecopower / Mega / Bolt entries kept
    serving the pre-fix figures after upgrading. Any change to what an
    extractor produces must bump the constant; a change to how a stored card
    is priced (apply_vat, resolve_excise_band) need not, since those run on
    load."""
    snap = make_snapshot(supplier="ecopower", contract="ecopower_burgerstroom")
    payload = _snapshot_to_dict(
        snap, datetime(2026, 8, 1, tzinfo=UTC), probe_key="unchanged"
    )
    assert payload["_schema_version"] == _SNAPSHOT_SCHEMA_VERSION

    stale = {**payload, "_schema_version": _SNAPSHOT_SCHEMA_VERSION - 1}
    with pytest.raises(ValueError, match="older than the running integration"):
        _snapshot_from_dict(stale)

    # The current version must of course still round-trip.
    assert _snapshot_from_dict(payload).supplier == "ecopower"


async def test_ytd_static_fees_honours_meter_override(hass: HomeAssistant) -> None:
    # The compare flow can override the meter; the YTD fixed fee must then
    # be billed at the override meter, not the entry meter, so an
    # exclusive-night override picks the exclusive-night yearly fee.
    snap = make_snapshot(
        energy=VariableRates(
            current=0.16,
            yearly_fixed_fee=85.0,
            yearly_fixed_fee_exclusive_night=35.04,
        ),
        dsos={},
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"meter": "mono", "dso": ""})

    async def _fake_walk(*_a: Any, **_k: Any):
        # One full-year month-equivalent: days_in_ytd == days_in_year, so
        # the fee accrues in full.
        yield snap, None, 365, 365

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._walk_ytd_months",
        new=_fake_walk,
    ):
        fee_entry = await _ytd_static_fees(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            snap,
            entry,
            date(2026, 12, 31),
        )
        fee_override = await _ytd_static_fees(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            snap,
            entry,
            date(2026, 12, 31),
            meter="exclusive_night",
        )
    assert fee_entry == pytest.approx(85.0)
    assert fee_override == pytest.approx(35.04)


def test_brussels_osp_fee_selects_configured_tier() -> None:
    from custom_components.be_electricity_prices.fees import _brussels_osp_fee
    from custom_components.be_electricity_prices.providers.base import DsoOverlay

    overlay = DsoOverlay(
        distribution_single=0.10,
        transport=0.02,
        brussels_osp_by_tier={
            "le1_44": 0.0,
            "le6": 13.36,
            "le9_6": 21.37,
            "le13": 26.71,
        },
    )

    def _entry(tier: str | None = None) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN, data={"connection_kva_tier": tier} if tier else {}
        )

    assert _brussels_osp_fee(overlay, _entry("le9_6")) == pytest.approx(21.37)
    # Existing entries without the field bill the default 1.44-6.00 kVA tier.
    assert _brussels_osp_fee(overlay, _entry()) == pytest.approx(13.36)
    # An overlay with no OSP table (non-Brussels) bills nothing.
    plain = DsoOverlay(distribution_single=0.1, transport=0.0)
    assert _brussels_osp_fee(plain, _entry("le13")) == 0.0


# ---- quarter-hourly spot caches in the YTD replay ---------------------------


def _projection_entry(**overrides: object) -> MockConfigEntry:
    base: dict[str, object] = {
        "supplier": "test",
        "contract": "test",
        "region": "wallonia",
        "dso": "ores",
        "meter": "mono",
        "solar_regime": "none",
        "consumption_kwh": "sensor.cons",
    }
    base.update(overrides)
    # A None override drops the key: entry.data is a mappingproxy on a real
    # entry, so "no meter wired" has to be built rather than deleted.
    return MockConfigEntry(
        domain=DOMAIN, data={k: v for k, v in base.items() if v is not None}
    )


def _daily(per_day: float, inj_per_day: float = 0.0) -> Any:
    """Recorder fake returning a flat kWh/day over whatever window is asked."""

    async def _fake(
        _hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        per = {"sensor.cons": per_day, "sensor.inj": inj_per_day}.get(entity_id)
        if per is None:
            return {}
        days = (end - start).days + 1
        return {start + timedelta(days=i): per for i in range(days)}

    return _fake


async def _project(hass: HomeAssistant, entry: Any, fake: Any, **kw: Any) -> Any:
    diag: dict[str, Any] = {}
    with patch.object(energy_meters, "_recorder_daily_kwh", new=fake):
        got = await _compute_projected_year_cost(
            hass,
            entry,
            kw.pop("snapshot", None) or _yearly_snapshot(),
            kw.pop("priced", None) or _yearly_snapshot(),
            billed_peak_kw=0.0,
            today=dt_util.now().date(),
            breakdown=diag,
            **kw,
        )
    return got, diag


async def test_projection_bills_the_cohort_yearly_fee_not_todays(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A signing-cohort entry must be projected entirely on its own card.

    The supplier's yearly fixed fee rides on the energy leg, which is exactly
    what the cohort splice replaces, so taking the per-kWh rate from the
    spliced card and the fee from today's put this figure half on one vintage
    and half on the other. It was the only full-year path doing that: the
    year-to-date walk and the compare page both bill the cohort fee, so the
    same entry showed two full-year numbers that disagreed by the difference
    between what the user signed and what a new customer is offered."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    signed = replace(
        _yearly_snapshot(),
        energy=FixedRates(single=0.18, peak=0.20, offpeak=0.16, yearly_fixed_fee=0.0),
    )
    todays = replace(
        _yearly_snapshot(),
        energy=FixedRates(single=0.18, peak=0.20, offpeak=0.16, yearly_fixed_fee=100.0),
    )
    got, _ = await _project(
        hass,
        _projection_entry(),
        _daily(10.0),
        snapshot=todays,
        priced=signed,
    )
    control, _ = await _project(
        hass,
        _projection_entry(),
        _daily(10.0),
        snapshot=signed,
        priced=signed,
    )
    assert got is not None and control is not None
    # Both bill the signed 0 EUR fee. Reading it off today's card instead
    # added its 100 EUR to a contract the user is not on.
    assert got == pytest.approx(control)


async def test_projection_prices_a_full_year_at_todays_tariffs(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A measured year is billed as a whole year, not as elapsed plus rest."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    got, diag = await _project(hass, _projection_entry(), _daily(10.0))
    assert got is not None
    assert diag["annual_kwh"] == pytest.approx(3650.0, abs=1.0)
    # 3650 kWh at the stub's 0.3665 all-in, no fees on the stub snapshot.
    assert got == pytest.approx(3650.0 * 0.3765, rel=0.01)


async def test_projection_does_not_decay_across_the_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The headline figure must not fall as the year runs.

    Composing this as current_year_cost plus a priced remainder did exactly
    that whenever the two legs did not measure the same thing. With no meter
    wired, the running bill is the fees-only floor while the remainder was
    priced off the household default, so the sensor slid from a plausible
    January figure to the bare fees floor in December. Pricing a whole year in
    one pass is what makes it stable."""

    entry = _projection_entry(consumption_kwh=None)
    seen = []
    for when in ("2026-02-01", "2026-07-01", "2026-12-20"):
        freezer.move_to(f"{when} 12:00:00+01:00")
        got, diag = await _project(hass, entry, _daily(10.0))
        seen.append(got)
        assert "default" in diag["volume_basis"]
    assert seen[0] is not None
    # Same number on every date, to the cent.
    assert seen[1] == pytest.approx(seen[0], abs=0.01)
    assert seen[2] == pytest.approx(seen[0], abs=0.01)

    # And on the METERED path, which the earlier version of this test never
    # exercised. A blended implementation decays here too, just less sharply,
    # because the elapsed leg is real while the remainder is annualised.
    metered = _projection_entry()
    seen_m = []
    for when in ("2026-02-01", "2026-07-01", "2026-12-20"):
        freezer.move_to(f"{when} 12:00:00+01:00")
        got, diag = await _project(hass, metered, _daily(10.0))
        seen_m.append(got)
        assert "measured" in diag["volume_basis"]
    assert seen_m[0] is not None
    assert seen_m[1] == pytest.approx(seen_m[0], abs=0.01)
    assert seen_m[2] == pytest.approx(seen_m[0], abs=0.01)


async def test_projection_uses_the_default_volume_without_a_meter(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No meter wired still yields a typical-household figure, labelled."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(consumption_kwh=None)
    got, diag = await _project(hass, entry, _daily(10.0))
    assert diag["annual_kwh"] == pytest.approx(3500.0)
    assert "wire a kWh sensor" in diag["volume_basis"]
    assert got == pytest.approx(3500.0 * 0.3765, rel=0.01)


@pytest.mark.parametrize(
    "energy",
    [DynamicRates(factor=1.0, base=0.0), SpotMonthlyRates(factor=1.0, base=0.0)],
)
async def test_projection_refuses_a_spot_priced_contract(
    hass: HomeAssistant, freezer: Any, energy: Any
) -> None:
    """A leg carried as a formula over an index nobody has yet reports no value.

    Both snapshots carry the same energy here, so this is a genuinely
    spot-priced card rather than a signing-cohort re-price."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    snap = replace(_yearly_snapshot(), energy=energy)
    got, diag = await _project(
        hass, _projection_entry(), _daily(10.0), snapshot=snap, priced=snap
    )
    assert got is None
    assert "no forward price exists" in diag["energy_basis"]


async def test_projection_blames_the_cohort_splice_when_that_is_the_cause(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A start date can move a Variable card onto a spot axis, and the reason
    the sensor went quiet has to name that.

    ``cohort._cohort_energy_from_archived`` rewrites a Variable card with
    parsed coefficients into SpotMonthlyRates, so filling in an optional
    renewal-reminder field turns the sensor off. Telling that user their card
    settles on a Belpex index reads as simply wrong: their card does not."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    card = replace(_yearly_snapshot(), energy=VariableRates(current=0.18))
    spliced = replace(_yearly_snapshot(), energy=SpotMonthlyRates(factor=1.0, base=0.0))
    got, diag = await _project(
        hass, _projection_entry(), _daily(10.0), snapshot=card, priced=spliced
    )
    assert got is None
    assert "contract start date" in diag["energy_basis"]


async def test_projection_prices_a_resolved_monthly_indexed_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A Variable or Impact card is monthly-indexed in the market sense, but
    its extractor has already resolved this month's rate, and holding a
    resolved rate flat is the assumption the whole sensor rests on. The gate
    tests the shape the rate is stored in, not the product's marketing."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    card = replace(_yearly_snapshot(), energy=VariableRates(current=0.18))
    got, diag = await _project(
        hass, _projection_entry(), _daily(10.0), snapshot=card, priced=card
    )
    assert got is not None
    assert "today's published rate" in diag["energy_basis"]


async def test_projection_scales_a_short_history_and_says_so(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Under a full year the volume is annualised and labelled as scaled."""

    freezer.move_to("2026-07-01 12:00:00+02:00")

    async def _short(
        _hass: object, entity_id: str, _start: date, end: date
    ) -> dict[date, float]:
        if entity_id != "sensor.cons":
            return {}
        return {end - timedelta(days=i): 10.0 for i in range(120)}

    _got, diag = await _project(hass, _projection_entry(), _short)
    assert "scaled from 120 days" in diag["volume_basis"]


async def test_projection_never_folds_in_a_partial_injection_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Feed-in counts only against a full trailing year of ITS OWN history.

    Gating on the consumption side's coverage credited a 45-day measurement as
    a year for a household that wired panels part-way through a metered
    year."""

    freezer.move_to("2026-07-01 12:00:00+02:00")

    async def _cons_year_inj_short(
        _hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        if entity_id == "sensor.cons":
            days = (end - start).days + 1
            return {start + timedelta(days=i): 10.0 for i in range(days)}
        if entity_id == "sensor.inj":
            return {end - timedelta(days=i): 5.0 for i in range(45)}
        return {}

    entry = _projection_entry(solar_regime="injection", injection_kwh="sensor.inj")
    _got, diag = await _project(hass, entry, _cons_year_inj_short)
    assert diag["annual_injection_kwh"] == 0.0
    assert "needs a full year of feed-in history" in diag["injection_basis"]


async def test_projection_clamps_the_compensation_net_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One clamp over the whole year, not one per half of it.

    This has to use a SEASONAL profile. Under a flat one the elapsed span and
    the remaining span net the same sign, so clamping each separately lands on
    the same number as clamping once and the test cannot see the difference:
    an implementation that split the year in two and clamped both halves passed
    the earlier version of this test while diverging by 75% on real data.

    Here the year nets positive overall (a bill is owed) while the summer half
    alone nets negative, so a per-half clamp would discard the banked surplus
    and quote materially more."""

    freezer.move_to("2026-10-01 12:00:00+02:00")
    entry = _projection_entry(solar_regime="compensation", injection_kwh="sensor.inj")

    async def _seasonal(
        _hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        days = (end - start).days + 1
        out: dict[date, float] = {}
        for i in range(days):
            day = start + timedelta(days=i)
            summer = 4 <= day.month <= 9
            if entity_id == "sensor.cons":
                out[day] = 10.0
            elif entity_id == "sensor.inj":
                out[day] = 18.0 if summer else 0.0
        return out

    got, diag = await _project(hass, entry, _seasonal)
    assert got is not None
    # 3650 kWh drawn, 3294 injected over the summer half: the year nets 356 kWh
    # billable. Clamping each half separately would forfeit the summer surplus
    # and bill the winter half gross, several times this.
    billable = diag["annual_kwh"] - diag["annual_injection_kwh"]
    assert billable > 0.0
    assert got == pytest.approx(billable * 0.3765, rel=0.02)


async def test_projection_tolerates_a_gap_in_the_feed_in_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A few missing recorder days must not move the figure.

    Requiring all 365 made the feed-in leg binary on a quantity that is
    routinely a day short (a restart, an inverter unavailable overnight, the
    hours before today's statistics compile). Under the netting regime that
    flipped a netted bill to a gross one: measured at ten times the correct
    figure on a single absent bucket."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(solar_regime="compensation", injection_kwh="sensor.inj")
    seen = []
    for gap in (0, 1, 3, 14):

        async def _gapped(
            _hass: object, entity_id: str, start: date, end: date, gap: int = gap
        ) -> dict[date, float]:
            days = (end - start).days + 1
            if entity_id == "sensor.cons":
                return {start + timedelta(days=i): 10.0 for i in range(days)}
            if entity_id == "sensor.inj":
                return {start + timedelta(days=i): 9.0 for i in range(days - gap)}
            return {}

        got, _diag = await _project(hass, entry, _gapped)
        seen.append(got)
    assert all(v is not None for v in seen)
    for v in seen[1:]:
        assert v == pytest.approx(seen[0], abs=0.01)


async def test_projection_refuses_an_unnettable_compensation_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Past the tolerance a netted meter reports nothing, not a gross bill.

    Zeroing the feed-in leg under compensation is not a missing credit, it is
    the wrong bill by an order of magnitude, and a partial window cannot be
    scaled because PV is seasonal enough that a summer sample nets the year to
    the zero clamp. So it refuses, the way a spot-priced contract does."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(solar_regime="compensation", injection_kwh="sensor.inj")

    async def _short_inj(
        _hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        days = (end - start).days + 1
        if entity_id == "sensor.cons":
            return {start + timedelta(days=i): 10.0 for i in range(days)}
        if entity_id == "sensor.inj":
            return {end - timedelta(days=i): 9.0 for i in range(60)}
        return {}

    got, diag = await _project(hass, entry, _short_inj)
    assert got is None
    assert "netted against its own feed-in" in diag["injection_basis"]


async def test_projection_says_when_a_measured_year_earns_no_credit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A spot-indexed feed-in cannot be priced without a forward spot.

    The volume is measured but no credit is applied, and the basis has to say
    so rather than reporting 'measured' next to a bill that ignored it."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    snap = replace(
        _yearly_snapshot(),
        injection=InjectionRates(current=None, factor=1.0, base=-0.02),
    )
    entry = _projection_entry(solar_regime="injection", injection_kwh="sensor.inj")
    _got, diag = await _project(
        hass, entry, _daily(10.0, inj_per_day=8.0), snapshot=snap, priced=snap
    )
    assert "not credited" in diag["injection_basis"]


async def test_projection_discloses_a_contract_ending_inside_the_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An end date inside the projected year is disclosed, not priced.

    The projection holds today's rate for a full year. When the contract runs
    out before December the later months are priced on a card the user will
    not be on. Nothing better exists to price them with, so the figure stands
    and the basis says how much of it is actually contracted."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(contract_end_date="2026-09-30")
    got, diag = await _project(hass, entry, _daily(10.0))
    assert got is not None  # still produces a number
    assert "91 of the 183 days" in diag["contract_basis"]
    assert "not signed yet" in diag["contract_basis"]


async def test_projection_says_when_the_contract_outlives_the_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An end date past 31 December leaves the whole projection contracted."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(contract_end_date="2027-06-30")
    _got, diag = await _project(hass, entry, _daily(10.0))
    assert "runs past this year" in diag["contract_basis"]
    assert "not signed yet" not in diag["contract_basis"]


async def test_projection_tolerates_a_stale_or_absent_end_date(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No end date, a past one, and a bad one all still produce a figure.

    The field was collected as an inert renewal reminder, so stored values are
    approximate and some have already expired. Making it load-bearing must
    never turn a working sensor into no value."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    for value, expected in (
        (None, "no end date set"),
        ("2026-01-15", "has passed"),
        ("not-a-date", "no end date set"),
    ):
        kw = {} if value is None else {"contract_end_date": value}
        got, diag = await _project(hass, _projection_entry(**kw), _daily(10.0))
        assert got is not None, value
        assert expected in diag["contract_basis"], value


async def test_projection_takes_an_end_date_without_a_start_date(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The two contract dates are independently optional.

    ``flow_schemas._validate_contract_dates`` only cross-checks them when both
    are present, so an end date with no start date is a valid stored state and
    must not need the cohort path to resolve."""

    freezer.move_to("2026-07-01 12:00:00+02:00")
    entry = _projection_entry(contract_end_date="2026-10-31")
    assert "contract_start_date" not in entry.data
    got, diag = await _project(hass, entry, _daily(10.0))
    assert got is not None
    assert "ends 2026-10-31" in diag["contract_basis"]


def test_projected_year_cost_sensor_metadata() -> None:
    """The projection is MEASUREMENT with no device class, on purpose.

    ``MONETARY`` admits only ``TOTAL``, which compiles a cumulative sum from
    state deltas. This figure is revised up and down as the year runs, so that
    sum would record the drift of the projection rather than money. The cost
    is that the Energy dashboard will not auto-suggest it, which is the right
    outcome for an estimate.

    The key is also permanent from first release: unique_id is derived from
    it, so renaming later orphans the entity and drops its history."""
    from homeassistant.components.sensor import SensorStateClass

    from custom_components.be_electricity_prices.sensor import FEE_SENSORS

    (desc,) = [d for d in FEE_SENSORS if d.key == "projected_year_cost"]
    assert desc.device_class is None
    assert desc.state_class == SensorStateClass.MEASUREMENT
    assert desc.native_unit_of_measurement == "EUR"
    assert desc.translation_key == "projected_year_cost"
    # A projection must never be compiled into a cumulative statistic.
    assert getattr(desc, "last_reset_fn", None) is None


def test_projection_attributes_are_not_recorded() -> None:
    """Every attribute the projection publishes stays out of the recorder.

    They are basis strings and slowly-moving figures re-emitted on every tick;
    recording them would bloat the database for no query anyone runs."""
    from custom_components.be_electricity_prices.sensor import BePriceSensor

    for name in (
        "energy_basis",
        "fee_basis",
        "volume_basis",
        "injection_basis",
        "annual_kwh",
        "annual_injection_kwh",
        "contract_basis",
    ):
        assert name in BePriceSensor._unrecorded_attributes, name


def test_config_flow_steps_are_fully_translated() -> None:
    """Every config-flow field label and help string exists in all four files.

    The translations carry RESOLVED literals where strings.json may carry a
    ``[%key:...%]`` reference, so a field added to one path in strings.json can
    reach users untranslated, or stale, on the other. That has happened: the
    contract end date's options-flow copy kept telling users the date did not
    affect pricing for a commit after it started to.
    """
    import json
    import pathlib

    base = pathlib.Path("custom_components/be_electricity_prices")
    src = json.loads(base.joinpath("strings.json").read_text(encoding="utf-8"))
    langs = {
        f.name: json.loads(f.read_text(encoding="utf-8"))
        for f in sorted(base.joinpath("translations").glob("*.json"))
    }
    for section in ("config", "options"):
        for step, body in src.get(section, {}).get("step", {}).items():
            for kind in ("data", "data_description"):
                want = set(body.get(kind, {}))
                if not want:
                    continue
                for name, doc in langs.items():
                    got = set(
                        doc.get(section, {}).get("step", {}).get(step, {}).get(kind, {})
                    )
                    missing = want - got
                    assert not missing, (
                        f"{name} {section}.{step}.{kind}: {sorted(missing)}"
                    )


def test_the_meters_step_explains_every_field() -> None:
    """The meters step must carry per-field help, not just labels.

    It is the step users get wrong. Six entity pickers with bare labels and one
    long paragraph above them led a user to wire a "this year" total, which
    resets every 1 January, into a field that bills the day-to-day change in
    the reading, and to fill the two totals fields that the four registers
    already override.
    """
    import json
    import pathlib

    base = pathlib.Path("custom_components/be_electricity_prices")
    src = json.loads(base.joinpath("strings.json").read_text(encoding="utf-8"))
    meters = src["config"]["step"]["meters"]
    fields = set(meters["data"])
    described = set(meters.get("data_description", {}))
    assert fields - described == set(), f"no help for {sorted(fields - described)}"
    # The two facts that were missing and cost a user an evening.
    joined = " ".join(meters["data_description"].values()).lower()
    assert "climb" in joined, "the cumulative requirement is not stated"
    assert "registers above are empty" in joined, "the precedence is not stated"


def test_every_sensor_name_exists_in_all_translations() -> None:
    """strings.json's entity names must exist in all four translation files.

    Nothing in CI compares them, and a missing key does not surface as a raw
    slug. With ``_attr_has_entity_name`` and no ``name`` on the description,
    the entity becomes the device's main feature and its friendly name
    collapses to the config entry title, while its entity_id is fixed at
    registration from that title. That is permanent and invisible."""
    import json
    import pathlib

    base = pathlib.Path("custom_components/be_electricity_prices")
    ref = set(json.loads(base.joinpath("strings.json").read_text())["entity"]["sensor"])
    assert ref, "strings.json declares no sensor names"
    # Every description actually created must have a name. Comparing the four
    # translation files against strings.json alone would pass a sensor that is
    # missing from all five.
    from custom_components.be_electricity_prices import sensor as sensor_mod

    declared = {
        d.translation_key
        for group in (
            sensor_mod.SENSORS,
            sensor_mod.FEE_SENSORS,
            sensor_mod.CAPACITY_SENSORS,
            sensor_mod.PROSUMER_SENSORS,
            sensor_mod.INJECTION_SENSORS,
        )
        for d in group
        if d.translation_key
    }
    assert declared - ref == set(), f"no name for {sorted(declared - ref)}"
    for path in sorted(base.joinpath("translations").glob("*.json")):
        got = set(json.loads(path.read_text())["entity"]["sensor"])
        assert ref - got == set(), f"{path.name} is missing {sorted(ref - got)}"
        assert got - ref == set(), f"{path.name} has stray {sorted(got - ref)}"


def test_the_compare_quote_helpers_read_only_entry_data() -> None:
    """The compare flow hands these helpers a stand-in ConfigEntry.

    ``compare_flow._QuoteEntry`` is a frozen dataclass carrying a single
    ``data`` mapping, cast to ConfigEntry. Any helper on that path that grows
    a read of ``entry.entry_id``, ``.options``, ``.runtime_data`` or ``.title``
    breaks the compare page from a distance, and nothing guarded it. The
    projection reuses the same helpers, which is what makes it worth pinning
    now rather than after the next AttributeError."""

    from custom_components.be_electricity_prices import compare_quote

    class _OnlyData:
        """Raises on any attribute except ``data``, the way the proxy would
        if someone reached past it."""

        def __init__(self, data: dict[str, object]) -> None:
            self.data = data

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"compare helper read entry.{name}")

    entry = _OnlyData(
        {
            "region": "wallonia",
            "solar_regime": "none",
            "annual_consumption_kwh": 4200.0,
        }
    )
    snap = _yearly_snapshot()
    # The three helpers the projection and the compare page share.
    assert compare_quote._annual_fees(snap, entry, 0.0, "mono") >= 0.0  # type: ignore[arg-type]
    assert (
        compare_quote._annual_bill(snap, entry, 0.0, 0.30, 1000.0) > 0.0  # type: ignore[arg-type]
    )
    assert (
        compare_quote._tou_weighted_per_kwh(
            snap, "ores", "wallonia", dt_util.now(), None, "mono", "bi_horaire"
        )
        is not None
    )
    # And the async resolvers, which the earlier version of this test never
    # reached: _annual_volume reads entry.data for its typed-volume fallback,
    # and _measured_kwh reads it for every sensor id.
    import asyncio

    from homeassistant.config_entries import ConfigEntry

    from custom_components.be_electricity_prices import energy_meters

    async def _no_rows(
        _hass: object, _entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        return {}

    async def _drive() -> None:
        with patch.object(energy_meters, "_recorder_daily_kwh", new=_no_rows):
            # cast rather than a per-line ignore: the stand-in deliberately is
            # not a ConfigEntry, which is the whole point of the guard.
            proxy = cast("ConfigEntry[Any]", entry)
            no_hass = cast("HomeAssistant", None)
            vol = await compare_quote._annual_volume(
                no_hass, proxy, date(2026, 1, 1), date(2026, 7, 1)
            )
            # The typed volume was read off entry.data, not defaulted.
            assert vol.kwh == pytest.approx(4200.0)
            await energy_meters._measured_kwh(
                no_hass, proxy, date(2026, 1, 1), date(2026, 7, 1)
            )

    asyncio.run(_drive())


# ---- contract start-date cohort pricing (discussion #38) ---------------------


def _fixed_extractor(fetch_for_month: Any) -> SupplierExtractor:
    return SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
        fetch_for_month=fetch_for_month,
    )


def test_contract_start_month_parses_to_first_of_month() -> None:
    assert _contract_start_month(_entry()) is None
    assert _contract_start_month(_entry(contract_start_date="2025-11-15")) == date(
        2025, 11, 1
    )
    assert _contract_start_month(_entry(contract_start_date="")) is None
    assert _contract_start_month(_entry(contract_start_date="not-a-date")) is None


async def test_cohort_energy_leg_fixed_uses_signing_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A fixed contract signed months ago bills at the signing-month rate,
    not today's card."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))
    archived = make_snapshot(energy=FixedRates(single=0.20))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg == FixedRates(single=0.20)


async def test_cohort_energy_leg_dynamic_uses_signing_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=DynamicRates(factor=1.10, base=0.02))
    archived = make_snapshot(energy=DynamicRates(factor=1.02, base=0.01))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg == DynamicRates(factor=1.02, base=0.01)


async def test_cohort_energy_leg_none_without_start_date(hass: HomeAssistant) -> None:
    current = make_snapshot(energy=FixedRates(single=0.30))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return make_snapshot(energy=FixedRates(single=0.20))

    _monthly_snapshots(hass).clear()
    leg = await _cohort_energy_leg(
        hass,
        MagicMock(),
        _fixed_extractor(_ffm),
        "test",
        "wallonia",
        _entry(contract="test"),
        current,
    )
    assert leg is None


async def test_cohort_energy_leg_none_for_this_month_or_future(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return make_snapshot(energy=FixedRates(single=0.20))

    _monthly_snapshots(hass).clear()
    ext = _fixed_extractor(_ffm)
    for start in ("2026-07-01", "2026-08-01"):
        entry = _entry(contract="test", contract_start_date=start)
        leg = await _cohort_energy_leg(
            hass, MagicMock(), ext, "test", "wallonia", entry, current
        )
        assert leg is None, start


async def test_cohort_energy_leg_manual_applies_from_a_start_date_this_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Issue #54: the step accepts any start date up to today, so a contract
    signed earlier this month collected a signing rate that was then ignored
    until the month rolled over -- at which point the price jumped."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        raise AssertionError("this month's card is the current one, do not fetch")

    _monthly_snapshots(hass).clear()
    entry = _entry(
        contract="test",
        contract_start_date="2026-07-01",
        **{CONF_MANUAL_ENERGY_SINGLE: 0.21},
    )
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg == FixedRates(single=0.21)


async def test_cohort_energy_leg_none_when_no_archive(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A non-archive supplier (_snapshot_for_month returns the current
    snapshot itself) must not pretend to have retrieved a cohort rate.
    Relies on the _snapshot_for_month fallback-returns-same-object contract
    pinned by test_snapshot_for_month_falls_back_to_current_when_no_archive."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))
    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(None), "test", "wallonia", entry, current
    )
    assert leg is None


async def test_cohort_energy_leg_skips_variable(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Variable contracts are not re-priced from an archived card here (their
    resolved rate would freeze the signing-month index); that lands later."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=VariableRates(current=0.22))
    archived = make_snapshot(energy=VariableRates(current=0.18))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg is None


async def test_cohort_energy_leg_none_for_compare_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The OptionsFlow compare path walks an alternative contract with no
    signing history, so cohort pricing must not leak onto it."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return make_snapshot(energy=FixedRates(single=0.20))

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="my_real_contract", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass,
        MagicMock(),
        _fixed_extractor(_ffm),
        "other_contract",
        "wallonia",
        entry,
        current,
    )
    assert leg is None


async def test_effective_snapshot_splices_cohort_energy_onto_current_overlays(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The energy leg is frozen to the signing month while the DSO / tax
    overlays keep tracking the delivery month."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))

    def _dso(rate: float) -> dict[str, DsoOverlay]:
        return {"ores": DsoOverlay(distribution_single=rate, transport=0.0145)}

    async def _ffm(
        _session: object, _contract: str, _region: str, year_month: date
    ) -> SupplierSnapshot:
        if (year_month.year, year_month.month) == (2025, 11):
            # Signing-month card: locked energy + that month's network rate.
            return make_snapshot(energy=FixedRates(single=0.20), dsos=_dso(0.05))
        # Delivery-month card: newer energy + newer network rate.
        return make_snapshot(energy=FixedRates(single=0.25), dsos=_dso(0.11))

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    eff = await _effective_snapshot_for_month(
        hass,
        MagicMock(),
        _fixed_extractor(_ffm),
        "test",
        "wallonia",
        date(2026, 3, 1),
        current,
        entry,
    )
    # Energy comes from the signing month, network from the delivery month.
    assert eff.energy == FixedRates(single=0.20)
    assert eff.dsos["ores"].distribution_single == 0.11


async def test_effective_snapshot_is_noop_without_start_date(
    hass: HomeAssistant,
) -> None:
    current = make_snapshot(energy=FixedRates(single=0.30))
    delivery = make_snapshot(energy=FixedRates(single=0.25))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return delivery

    _monthly_snapshots(hass).clear()
    eff = await _effective_snapshot_for_month(
        hass,
        MagicMock(),
        _fixed_extractor(_ffm),
        "test",
        "wallonia",
        date(2026, 3, 1),
        current,
        _entry(contract="test"),
    )
    # No start date -> plain delivery-month snapshot, untouched.
    assert eff is delivery


# ---- manual signing-rate fallback (discussion #38, commit 4) -----------------


def test_manual_energy_leg_fixed() -> None:
    current = make_snapshot(energy=FixedRates(single=0.30))
    entry = _entry(
        contract="test",
        **{
            CONF_MANUAL_ENERGY_SINGLE: 0.22,
            CONF_MANUAL_ENERGY_PEAK: 0.25,
            CONF_MANUAL_ENERGY_OFFPEAK: 0.18,
            CONF_MANUAL_YEARLY_FEE: 60.0,
        },
    )
    assert _manual_energy_leg(entry, current.energy) == FixedRates(
        single=0.22, peak=0.25, offpeak=0.18, yearly_fixed_fee=60.0
    )


def test_manual_energy_leg_dynamic_keeps_quarter_hourly() -> None:
    current = make_snapshot(
        energy=DynamicRates(factor=1.10, base=0.02, quarter_hourly=True)
    )
    entry = _entry(
        contract="test",
        **{
            CONF_MANUAL_ENERGY_FACTOR: 1.02,
            CONF_MANUAL_ENERGY_BASE: 0.01,
            CONF_MANUAL_YEARLY_FEE: 48.0,
        },
    )
    assert _manual_energy_leg(entry, current.energy) == DynamicRates(
        factor=1.02, base=0.01, yearly_fixed_fee=48.0, quarter_hourly=True
    )


def test_manual_energy_leg_spot_monthly() -> None:
    """A spot-monthly contract signs a coefficient pair, like a dynamic one.

    energie.be Variabel is the first scraped contract of that kind; before it
    existed the manual leg only shaped fixed and dynamic, so a customer who
    negotiated a factor had nowhere to type it and the step was never offered.
    """
    current = make_snapshot(energy=SpotMonthlyRates(factor=1.19, base=0.009))
    entry = _entry(
        contract="test",
        **{
            CONF_MANUAL_ENERGY_FACTOR: 1.08,
            CONF_MANUAL_ENERGY_BASE: 0.004,
            CONF_MANUAL_YEARLY_FEE: 30.0,
        },
    )
    assert _manual_energy_leg(entry, current.energy) == SpotMonthlyRates(
        factor=1.08, base=0.004, yearly_fixed_fee=30.0
    )


def test_manual_energy_leg_spot_monthly_blank_step_is_no_override() -> None:
    """No box filled means "price off the card", not "price off zero"."""
    current = make_snapshot(energy=SpotMonthlyRates(factor=1.19, base=0.009))
    assert _manual_energy_leg(_entry(contract="test"), current.energy) is None


def test_manual_energy_leg_day_night_without_a_mono_rate() -> None:
    """Issue #54: no box on the step is a master switch.

    A bi-hourly customer signs a day rate and a night rate; there is no mono
    rate on their contract to type. Requiring the single box threw away the
    day / night rates AND the fee they did type.
    """
    current = make_snapshot(
        energy=FixedRates(single=0.30, peak=0.33, offpeak=0.27, yearly_fixed_fee=99.0)
    )
    entry = _entry(
        contract="test",
        **{
            CONF_MANUAL_ENERGY_PEAK: 0.25,
            CONF_MANUAL_ENERGY_OFFPEAK: 0.18,
        },
    )
    assert _manual_energy_leg(entry, current.energy) == FixedRates(
        single=0.30, peak=0.25, offpeak=0.18, yearly_fixed_fee=99.0
    )


def test_manual_energy_leg_exclusive_night() -> None:
    """Issue #54: a dedicated night circuit bills its own rate and, on cards
    that print one, its own standing charge. Both used to be copied from the
    card whatever the user typed, so an exclusive-night entry was the one
    meter shape the signing rate could not reach."""
    current = make_snapshot(
        energy=FixedRates(
            single=0.30,
            exclusive_night=0.24,
            yearly_fixed_fee=99.0,
            yearly_fixed_fee_exclusive_night=45.0,
        )
    )
    entry = _entry(
        contract="test",
        **{
            CONF_MANUAL_ENERGY_EXCLUSIVE_NIGHT: 0.19,
            CONF_MANUAL_YEARLY_FEE: 60.0,
        },
    )
    leg = _manual_energy_leg(entry, current.energy)
    assert isinstance(leg, FixedRates)
    assert leg.exclusive_night == pytest.approx(0.19)
    assert energy_eur_per_kwh(
        leg, dt_util.now(), None, "exclusive_night"
    ) == pytest.approx(0.19)
    # The card's separate night standing charge would otherwise be billed
    # instead of the one the user signed for.
    assert yearly_fixed_fee_for_meter(leg, "exclusive_night") == pytest.approx(60.0)


def test_manual_energy_leg_fee_only() -> None:
    """A signed standing charge with an unchanged energy rate is a real case
    (the card rate plus a negotiated fee), and used to be dropped whole."""
    current = make_snapshot(energy=FixedRates(single=0.30, yearly_fixed_fee=99.0))
    entry = _entry(contract="test", **{CONF_MANUAL_YEARLY_FEE: 60.0})
    assert _manual_energy_leg(entry, current.energy) == FixedRates(
        single=0.30, yearly_fixed_fee=60.0
    )

    dyn = make_snapshot(energy=DynamicRates(factor=1.05, base=0.017))
    entry = _entry(contract="test", **{CONF_MANUAL_YEARLY_FEE: 48.0})
    assert _manual_energy_leg(entry, dyn.energy) == DynamicRates(
        factor=1.05, base=0.017, yearly_fixed_fee=48.0
    )


def test_manual_energy_leg_blank_returns_none() -> None:
    current = make_snapshot(energy=FixedRates(single=0.30))
    assert _manual_energy_leg(_entry(contract="test"), current.energy) is None


def test_manual_energy_leg_none_for_variable() -> None:
    # A single manual rate would freeze a monthly-reindexed contract, so the
    # override is not offered for variable and must not be applied.
    current = make_snapshot(energy=VariableRates(current=0.22))
    entry = _entry(contract="test", **{CONF_MANUAL_ENERGY_SINGLE: 0.20})
    assert _manual_energy_leg(entry, current.energy) is None


async def test_cohort_energy_leg_manual_when_no_archive(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))
    _monthly_snapshots(hass).clear()
    entry = _entry(
        contract="test",
        contract_start_date="2025-11-10",
        **{CONF_MANUAL_ENERGY_SINGLE: 0.20},
    )
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(None), "test", "wallonia", entry, current
    )
    assert leg == FixedRates(single=0.20)


async def test_cohort_energy_leg_manual_wins_over_archive(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Issue #54: a signing rate typed by the user must beat the archived card.

    The archive only knows the published card; a promotional, brokered or
    negotiated rate exists nowhere online, and the step that collected it
    promises to price the contract with it. Suppliers that keep an archive
    (Mega, Bolt, Eneco, ...) used to discard it silently, so the same form
    worked on Engie and did nothing on Mega.
    """
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30, peak=0.33, offpeak=0.27))
    archived = make_snapshot(energy=FixedRates(single=0.18, peak=0.20, offpeak=0.16))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(
        contract="test",
        contract_start_date="2025-11-10",
        **{CONF_MANUAL_ENERGY_SINGLE: 0.25},
    )
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    # The typed rate replaces the archived single; the boxes left blank keep
    # the ARCHIVED signing-month values, not today's card.
    assert leg == FixedRates(single=0.25, peak=0.20, offpeak=0.16)


async def test_cohort_energy_leg_archive_still_wins_when_nothing_typed(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(energy=FixedRates(single=0.30))
    archived = make_snapshot(energy=FixedRates(single=0.18))

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg == FixedRates(single=0.18)


# ---- variable signing-cohort re-price (discussion #38, commit 5) -------------


def test_cohort_energy_from_archived_fixed_and_dynamic() -> None:
    fixed = make_snapshot(energy=FixedRates(single=0.20))
    assert _cohort_energy_from_archived(fixed) == FixedRates(single=0.20)
    dyn = make_snapshot(energy=DynamicRates(factor=1.02, base=0.01))
    assert _cohort_energy_from_archived(dyn) == DynamicRates(factor=1.02, base=0.01)


def test_cohort_energy_from_archived_variable_builds_spot_monthly() -> None:
    """A variable card with parsed coefficients re-prices to a SpotMonthlyRates
    leg (factor * this month's mean + base), not the archived resolved rate."""
    archived = make_snapshot(
        energy=VariableRates(
            current=0.18,
            yearly_fixed_fee=53.0,
            formula_factor=1.05,
            formula_base=-0.005,
        )
    )
    assert _cohort_energy_from_archived(archived) == SpotMonthlyRates(
        factor=1.05, base=-0.005, yearly_fixed_fee=53.0
    )


def test_cohort_energy_from_archived_variable_without_coefficients() -> None:
    # No parsed coefficients -> not re-priceable here, keep the current card.
    archived = make_snapshot(energy=VariableRates(current=0.18))
    assert _cohort_energy_from_archived(archived) is None


def test_cohort_energy_from_archived_tou_not_repriced() -> None:
    archived = make_snapshot(
        energy=TimeOfUseRates(peak=0.30, transition=0.20, offpeak=0.12)
    )
    assert _cohort_energy_from_archived(archived) is None


async def test_cohort_energy_leg_variable_uses_signing_coefficients(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(
        energy=VariableRates(current=0.22, formula_factor=1.20, formula_base=0.03)
    )
    archived = make_snapshot(
        energy=VariableRates(
            current=0.18, yearly_fixed_fee=53.0, formula_factor=1.05, formula_base=0.01
        )
    )

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    # The monthly mean comes from ENTSO-E, so the re-price needs a key.
    entry = _entry(contract="test", contract_start_date="2025-11-10", api_key="k")
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    # Coefficients from the SIGNING month; the coordinator applies them to the
    # CURRENT month's mean via the SpotMonthlyRates path.
    assert leg == SpotMonthlyRates(factor=1.05, base=0.01, yearly_fixed_fee=53.0)


async def test_cohort_energy_leg_variable_keeps_current_card_without_key(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A variable contract is never asked for an ENTSO-E key, so its cohort
    re-price must degrade to the current card rather than hand the coordinator
    a SpotMonthlyRates leg it can only price by failing the entry."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    current = make_snapshot(
        energy=VariableRates(current=0.22, formula_factor=1.20, formula_base=0.03)
    )
    archived = make_snapshot(
        energy=VariableRates(
            current=0.18, yearly_fixed_fee=53.0, formula_factor=1.05, formula_base=0.01
        )
    )

    async def _ffm(*_a: object, **_k: object) -> SupplierSnapshot:
        return archived

    _monthly_snapshots(hass).clear()
    entry = _entry(contract="test", contract_start_date="2025-11-10")
    assert CONF_API_KEY not in entry.data
    leg = await _cohort_energy_leg(
        hass, MagicMock(), _fixed_extractor(_ffm), "test", "wallonia", entry, current
    )
    assert leg is None


def test_cohort_energy_from_archived_carries_exclusive_night_fee() -> None:
    """An EBEM variable card prints a dedicated exclusive-night standing fee;
    the SpotMonthly cohort must carry it so an exclusive-night meter is not
    billed the standard abonnement."""
    from custom_components.be_electricity_prices.pricing import (
        yearly_fixed_fee_for_meter,
    )

    archived = make_snapshot(
        energy=VariableRates(
            current=0.18,
            yearly_fixed_fee=85.0,
            yearly_fixed_fee_exclusive_night=35.04,
            formula_factor=1.10,
            formula_base=0.02,
        )
    )
    cohort = _cohort_energy_from_archived(archived)
    assert isinstance(cohort, SpotMonthlyRates)
    assert cohort.yearly_fixed_fee_exclusive_night == pytest.approx(35.04)
    assert yearly_fixed_fee_for_meter(cohort, "exclusive_night") == pytest.approx(35.04)
    assert yearly_fixed_fee_for_meter(cohort, "mono") == pytest.approx(85.0)


def _capacity_entry(region: str = "flanders") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": region,
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "capacity_mode": "sensor",
        },
    )


def _capacity_snapshot(rate: float | None = 52.37) -> SupplierSnapshot:
    return make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                capacity_eur_per_kw_year=rate,
            )
        }
    )


async def test_ytd_capacity_accrues_the_monthly_charge(hass: HomeAssistant) -> None:
    """The capacity term is a EUR/kW/year rate billed monthly on the
    gemiddelde maandpiek, so a full year at 4 kW accrues peak x rate."""
    from custom_components.be_electricity_prices.ytd_cost import _ytd_capacity

    snap = _capacity_snapshot()

    async def _fake_walk(*_a: Any, **_k: Any):
        # Twelve whole months, each fully elapsed.
        for month in range(1, 13):
            days = calendar.monthrange(2026, month)[1]
            yield snap, date(2026, month, 1), days, days

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._walk_ytd_months",
        new=_fake_walk,
    ):
        total = await _ytd_capacity(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            snap,
            _capacity_entry(),
            date(2026, 12, 31),
            4.0,
        )
    # 12 months x (4.0 * 52.37 / 12) == a full year of the annual rate.
    assert total == pytest.approx(4.0 * 52.37)


async def test_ytd_capacity_is_flanders_only(hass: HomeAssistant) -> None:
    """Wallonia and Brussels do not bill a capacity tariff; a leftover rate on
    the overlay must not accrue there."""
    from custom_components.be_electricity_prices.ytd_cost import _ytd_capacity

    snap = _capacity_snapshot()

    async def _fake_walk(*_a: Any, **_k: Any):
        yield snap, date(2026, 1, 1), 31, 31

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._walk_ytd_months",
        new=_fake_walk,
    ):
        total = await _ytd_capacity(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            snap,
            _capacity_entry(region="wallonia"),
            date(2026, 1, 31),
            4.0,
        )
    assert total == 0.0


async def test_ytd_capacity_skips_months_whose_card_omits_the_rate(
    hass: HomeAssistant,
) -> None:
    """Cards outside Flanders leave capacity_eur_per_kw_year None; such a month
    contributes nothing rather than raising."""
    from custom_components.be_electricity_prices.ytd_cost import _ytd_capacity

    priced, unpriced = _capacity_snapshot(), _capacity_snapshot(rate=None)

    async def _fake_walk(*_a: Any, **_k: Any):
        yield unpriced, date(2026, 1, 1), 31, 31
        yield priced, date(2026, 2, 1), 28, 28

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._walk_ytd_months",
        new=_fake_walk,
    ):
        total = await _ytd_capacity(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            priced,
            _capacity_entry(),
            date(2026, 2, 28),
            4.0,
        )
    assert total == pytest.approx(4.0 * 52.37 / 12.0)  # February only


async def test_ytd_capacity_prorates_the_running_month(hass: HomeAssistant) -> None:
    """A part-elapsed month accrues its own fraction, the same proration
    _ytd_prosumer uses, so the backfill can meet it at the seam."""
    from custom_components.be_electricity_prices.ytd_cost import _ytd_capacity

    snap = _capacity_snapshot()

    async def _fake_walk(*_a: Any, **_k: Any):
        yield snap, date(2026, 1, 1), 31, 31
        yield snap, date(2026, 2, 1), 28, 14  # half of February

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._walk_ytd_months",
        new=_fake_walk,
    ):
        total = await _ytd_capacity(
            hass,
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            snap,
            _capacity_entry(),
            date(2026, 2, 14),
            4.0,
        )
    monthly = 4.0 * 52.37 / 12.0
    assert total == pytest.approx(monthly * (1 + 14 / 28))


def test_manual_signing_rate_blank_fields_keep_the_current_card() -> None:
    """The signed_rate step is all-optional and tells the user "leave blank to
    keep using the current published card". Only the headline field means "no
    override"; every other blank box must fall back to the card's own value,
    not to zero. Substituting 0.0 wiped the standing charge for a user who
    typed only their locked energy rate, and zeroed a dynamic formula's base."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.cohort import _manual_energy_leg
    from custom_components.be_electricity_prices.providers.base import (
        DynamicRates,
        FixedRates,
    )
    from tests import make_snapshot

    card = make_snapshot(
        energy=FixedRates(single=0.20, peak=0.22, offpeak=0.18, yearly_fixed_fee=95.0)
    )
    entry = SimpleNamespace(data={"manual_energy_single": 0.17})
    leg = _manual_energy_leg(entry, card.energy)  # type: ignore[arg-type]
    assert isinstance(leg, FixedRates)
    assert leg.single == pytest.approx(0.17)  # the override
    assert leg.yearly_fixed_fee == pytest.approx(95.0)  # kept, not zeroed
    assert leg.peak == pytest.approx(0.22)
    assert leg.offpeak == pytest.approx(0.18)

    dyn = make_snapshot(
        energy=DynamicRates(factor=1.05, base=0.017, yearly_fixed_fee=60.0)
    )
    entry = SimpleNamespace(data={"manual_energy_factor": 1.12})
    leg = _manual_energy_leg(entry, dyn.energy)  # type: ignore[arg-type]
    assert isinstance(leg, DynamicRates)
    assert leg.factor == pytest.approx(1.12)
    assert leg.base == pytest.approx(0.017)  # kept, not zeroed
    assert leg.yearly_fixed_fee == pytest.approx(60.0)

    # An explicit 0 is still an override, distinct from a blank box.
    entry = SimpleNamespace(
        data={"manual_energy_single": 0.17, "manual_yearly_fee": 0.0}
    )
    leg = _manual_energy_leg(entry, card.energy)  # type: ignore[arg-type]
    assert isinstance(leg, FixedRates)
    assert leg.yearly_fixed_fee == 0.0


async def test_half_wired_registers_bill_nothing_on_every_ytd_path() -> None:
    """A day/night register pair with only one half wired cannot be billed: the
    missing band's kWh are simply absent. The static per-day path has always
    refused the whole year for that, but the hourly path (TOU / Impact /
    dynamic / exclusive-night) resolved each side independently and only bailed
    when BOTH were empty. So a half-wired consumption pair collapsed to "no
    consumption sensors" while a wired injection sensor kept crediting, billing
    the feed-in credit against zero consumption and driving the YTD negative.
    """
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, patch

    entry = SimpleNamespace(
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "injection",
            "day_consumption_kwh": "sensor.day_cons",
            # night register deliberately absent
            "injection_kwh": "sensor.inj",
        }
    )

    assert energy_meters._partial_register_pair(entry, "consumption") is True  # type: ignore[arg-type]
    assert energy_meters._partial_register_pair(entry, "injection") is False  # type: ignore[arg-type]

    today = date(2026, 8, 1)
    with patch.object(energy_meters, "_recorder_daily_kwh", AsyncMock(return_value={})):
        daily = await energy_meters._resolve_daily_kwh(None, entry, today)  # type: ignore[arg-type]
    assert daily is None, "static path must refuse a half-wired pair"

    hourly = await ytd_cost._ytd_hourly_energy(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        make_snapshot(),
        entry,  # type: ignore[arg-type]
        today,
        meter="dynamic",
    )
    assert hourly is None, "hourly path must refuse it the same way"

    # A fully wired pair is still accepted by the predicate.
    entry.data["night_consumption_kwh"] = "sensor.night_cons"
    assert energy_meters._partial_register_pair(entry, "consumption") is False  # type: ignore[arg-type]


async def test_a_totals_sensor_rescues_a_half_wired_pair() -> None:
    """A half-wired pair cannot be billed FROM THE REGISTERS, but a totals
    sensor on the same side covers both bands completely and the split is
    recovered from hourly statistics.

    Refusing regardless threw away a fully wired totals sensor and floored
    current_year_cost at fees only, which reads to the user as the
    integration not working at all.
    """

    entry = SimpleNamespace(
        data={
            "supplier": "test",
            "contract": "test",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "day_consumption_kwh": "sensor.day_cons",
            # night register absent, but a complete totals sensor IS wired
            "consumption_kwh": "sensor.grid_import_total",
        }
    )
    assert energy_meters._partial_register_pair(entry, "consumption") is False  # type: ignore[arg-type]

    today = date(2026, 8, 1)
    with patch.object(
        energy_meters, "_recorder_daily_kwh", AsyncMock(return_value={today: 10.0})
    ):
        daily = await energy_meters._resolve_daily_kwh(None, entry, today)  # type: ignore[arg-type]
    assert daily is not None, "the totals sensor must still be used"

    # With no totals sensor it is still refused.
    del entry.data["consumption_kwh"]
    assert energy_meters._partial_register_pair(entry, "consumption") is True  # type: ignore[arg-type]


def test_all_kwh_resolvers_agree_that_registers_win() -> None:
    """Three helpers answer "which entity holds this side's kWh", and they
    disagreed when BOTH wirings were configured: the static per-day path and
    diagnostics took the day/night registers, the hourly path (TOU / Impact /
    dynamic / exclusive-night) and the backfill took the totals sensor. The
    same user was then billed off a different meter depending on their contract
    kind, and the two figures drifted apart.

    const.py states the rule: "when both are configured, the day/night
    registers win"."""
    from types import SimpleNamespace

    both = SimpleNamespace(
        data={
            "day_consumption_kwh": "sensor.day",
            "night_consumption_kwh": "sensor.night",
            "consumption_kwh": "sensor.total",
            "day_injection_kwh": "sensor.dinj",
            "night_injection_kwh": "sensor.ninj",
            "injection_kwh": "sensor.tinj",
        }
    )
    # _kwh_sensor_ids feeds the daily path and diagnostics; registers first.
    day, night, total = energy_meters._kwh_sensor_ids(both, "consumption")  # type: ignore[arg-type]
    assert (day, night) == ("sensor.day", "sensor.night")
    assert total == "sensor.total"
    # The hourly path must pick the same meter.
    assert energy_meters._hourly_consumption_sensors(both) == [  # type: ignore[arg-type]
        "sensor.day",
        "sensor.night",
    ]  # type: ignore[arg-type]
    assert energy_meters._hourly_injection_sensors(both) == [  # type: ignore[arg-type]
        "sensor.dinj",
        "sensor.ninj",
    ]  # type: ignore[arg-type]

    # Totals-only still resolves to the total.
    totals_only = SimpleNamespace(
        data={"consumption_kwh": "sensor.total", "injection_kwh": "sensor.tinj"}
    )
    assert energy_meters._hourly_consumption_sensors(totals_only) == ["sensor.total"]  # type: ignore[arg-type]
    assert energy_meters._hourly_injection_sensors(totals_only) == ["sensor.tinj"]  # type: ignore[arg-type]

    # Nothing wired stays empty.
    empty = SimpleNamespace(data={})
    assert energy_meters._hourly_consumption_sensors(empty) == []  # type: ignore[arg-type]
    assert energy_meters._hourly_injection_sensors(empty) == []  # type: ignore[arg-type]


async def test_cohort_leg_bills_the_same_fee_on_every_call_path() -> None:
    """Every `_cohort_energy_leg` call site must resolve one fee.

    `apply_vat` zeroes `vat_rate` for an entry that deducts VAT, and that was
    the only record of the basis the card was published at. Every caller hands
    in an already-resolved snapshot, so threading the rate as a parameter
    reached the live tick alone and left the rest 21 EUR/yr adrift on the same
    entry. The rate now rides on the snapshot every caller already hands in.
    """
    from custom_components.be_electricity_prices import providers
    from custom_components.be_electricity_prices.cohort import (
        _cohort_energy_leg,
    )
    from custom_components.be_electricity_prices.providers.base import apply_vat

    extractor = providers.get("engie")
    contract = next(c.id for c in extractor.contracts if "fix" in c.id)
    entry = SimpleNamespace(
        data={
            CONF_SUPPLIER: "engie",
            CONF_CONTRACT: contract,
            CONF_REGION: "fluvius_imewo",
            CONF_CONTRACT_START_DATE: "2026-01-15",
            CONF_MANUAL_YEARLY_FEE: 121.0,
            CONF_INCLUDE_VAT: False,
        },
        options={},
        entry_id="e1",
    )
    raw = make_snapshot(
        energy=FixedRates(single=0.20, yearly_fixed_fee=100.0),
        taxes=TaxOverlay(federal_excise=0.0, energy_contribution=0.0, vat_rate=0.21),
    )

    async def fee(snapshot: Any) -> float:
        leg = await _cohort_energy_leg(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            extractor,
            contract,
            "fluvius_imewo",
            entry,  # type: ignore[arg-type]
            snapshot,
        )
        assert leg is not None
        return float(leg.yearly_fixed_fee)

    # The typed 121,00 is gross; an entry that deducts VAT bills 100,00. The
    # raw card (live tick) and the resolved card (YTD / monthly) must agree.
    assert await fee(raw) == pytest.approx(100.0)
    assert await fee(apply_vat(raw, include_vat=False)) == pytest.approx(100.0)


def test_published_vat_rate_round_trips_and_tolerates_an_old_cache() -> None:
    """Adding the field needs no schema bump: a cache written before it
    existed loads and falls back to the raw card's own vat_rate."""
    from custom_components.be_electricity_prices.providers.base import TaxOverlay

    raw = make_snapshot(
        taxes=TaxOverlay(federal_excise=0.0, energy_contribution=0.0, vat_rate=0.21)
    )
    payload = snapshot_store._snapshot_to_dict(
        raw, datetime(2026, 8, 5, tzinfo=UTC), probe_key="k"
    )
    assert "published_vat_rate" in payload["taxes"]
    assert snapshot_store._snapshot_from_dict(payload).taxes.vat_rate == pytest.approx(
        0.21
    )

    old = {
        **payload,
        "taxes": {
            k: v for k, v in payload["taxes"].items() if k != "published_vat_rate"
        },
    }
    restored = snapshot_store._snapshot_from_dict(old).taxes
    assert restored.published_vat_rate == 0.0
    assert (restored.published_vat_rate or restored.vat_rate) == pytest.approx(0.21)


def test_typed_signing_fee_lands_on_the_entry_basis() -> None:
    """The fee box is labelled "incl. VAT" and the typed figure is taken at
    that word, so it must be put onto whatever basis the entry bills on.

    A business that deducts VAT bills ex-VAT: apply_vat leaves its card fees
    as the professional card printed them, so a typed 121,00 sat next to a
    100,00 card fee on the same entry.
    """
    from custom_components.be_electricity_prices.providers.base import FixedRates

    card = FixedRates(single=0.20, yearly_fixed_fee=100.0)

    def _entry_with(include_vat: bool) -> Any:
        return SimpleNamespace(
            data={
                "supplier": "engie",
                "contract": "engie_pro_easy_fixed",
                "manual_yearly_fee": 121.0,
                "include_vat": include_vat,
            }
        )

    # Deducting business: the gross figure is converted to the card's basis.
    net = cohort._manual_energy_leg(_entry_with(False), card, 0.21)  # type: ignore[arg-type]
    assert net is not None
    assert net.yearly_fixed_fee == pytest.approx(100.0)

    # Not deducting: the entry bills gross, so the typed figure stands.
    gross = cohort._manual_energy_leg(_entry_with(True), card, 0.21)  # type: ignore[arg-type]
    assert gross is not None
    assert gross.yearly_fixed_fee == pytest.approx(121.0)

    # A residential (VAT-inclusive) card has no conversion to make either way.
    res = cohort._manual_energy_leg(_entry_with(False), card, 0.0)  # type: ignore[arg-type]
    assert res is not None
    assert res.yearly_fixed_fee == pytest.approx(121.0)


def _spp_snap() -> SupplierSnapshot:
    """A flat energy leg with a monthly Belpex_SPP-indexed feed-in credit
    (energie.be Vast)."""
    return _snapshot(
        prosumer=None,
        capacity=None,
        energy=FixedRates(single=0.1826),
        injection=InjectionRates(
            factor=0.6, base=-0.008, current=0.0343, spp_indexed=True
        ),
    )


def test_month_spot_needed_for_a_flat_card_with_an_spp_indexed_credit() -> None:
    """energie.be Vast: the energy leg needs no spot, the credit does.

    Without this the credit sits at the card's printed indicative forever,
    and that indicative is the formula on the VNR FORECAST rather than the
    realized month.
    """
    assert _injection_needs_month_spot(_spp_snap(), _entry(solar_regime="injection"))
    # Nothing reads the credit off the injection regime.
    assert not _injection_needs_month_spot(_spp_snap(), _entry(solar_regime="none"))


def test_month_spot_not_needed_when_the_energy_leg_already_fetches_spots() -> None:
    """A spot-monthly or dynamic leg resolves the credit through the energy
    path; claiming it here would fetch the same spots twice."""
    inj = InjectionRates(factor=0.6, base=-0.008, current=0.0343, spp_indexed=True)
    for energy in (
        SpotMonthlyRates(factor=1.19, base=0.008),
        DynamicRates(factor=1.1, base=0.005),
    ):
        assert not _injection_needs_month_spot(
            _snapshot(prosumer=None, capacity=None, energy=energy, injection=inj),
            _entry(solar_regime="injection"),
        )


def test_an_spp_indexed_credit_is_never_read_as_a_per_hour_one() -> None:
    """The reason _injection_needs_month_spot is its own predicate rather than
    a widening of _injection_needs_spot.

    _injection_hourly_on_cohort reads that one to conclude the injection keeps
    its OWN hourly formula and must not be baked to a month mean. Folding this
    monthly shape into it would skip the bake and credit the card's printed
    indicative forever, which is the bug this shape exists to fix.
    """
    entry = _entry(solar_regime="injection")
    assert not _injection_hourly_on_cohort(_spp_snap(), entry)
