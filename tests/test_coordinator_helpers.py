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

import calendar
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import (
    _compute_capacity,
    _compute_current_year_cost,
    _compute_injection_price,
    _compute_prosumer,
    _days_through,
    _energy_kind,
    _monthly_snapshots,
    _read_kwh,
    _recorder_daily_kwh,
    _snapshot_for_month,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    DynamicRates,
    FixedRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    TimeOfUseRates,
)
from tests import make_snapshot


def _snapshot(
    prosumer: float | None,
    capacity: float | None,
    injection: InjectionRates | None = None,
) -> SupplierSnapshot:
    return make_snapshot(
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


def test_injection_price_uses_formula_when_spot_available(freezer: Any) -> None:
    snap = _snapshot(
        prosumer=None,
        capacity=None,
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


def test_injection_price_returns_none_when_no_data() -> None:
    snap = _snapshot(prosumer=None, capacity=None, injection=None)
    entry = _entry(solar_regime="injection")
    assert _compute_injection_price(snap, entry, {}) is None


def test_injection_price_dynamic_returns_none_without_spot() -> None:
    """Dynamic-style injection (factor + base set) must surface None
    when no spot is available -- falling back to the snapshot's static
    `current` would be the wrong rate for a dynamic contract."""
    snap = _snapshot(
        prosumer=None,
        capacity=None,
        injection=InjectionRates(factor=0.97, base=-0.021, current=0.05),
    )
    entry = _entry(solar_regime="injection")
    # Spot cache empty -> sensor goes unknown rather than show 0.05.
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
    from custom_components.be_electricity_prices.coordinator import (
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
    return SupplierExtractor(
        id="test",
        label="Test",
        contracts=(),
        fetch=AsyncMock(),
    )


def _patch_recorder_per_entity(
    per_entity_per_day: dict[str, dict[date, float]],
) -> Any:
    """Patch _recorder_daily_kwh to return the configured per-day
    sums per entity_id; raise via empty dict for unmapped entities."""
    from custom_components.be_electricity_prices import coordinator

    async def _fake(
        hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        return dict(per_entity_per_day.get(entity_id, {}))

    return patch.object(coordinator, "_recorder_daily_kwh", new=_fake)


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
    from custom_components.be_electricity_prices import coordinator

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

    with patch.object(coordinator, "_recorder_hourly_kwh", new=_fake_hourly):
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


async def test_year_cost_tou_recognises_injection_only_wiring(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Injection-regime user on a TOU contract who only wired the
    injection sensor (e.g. inverter exposing solar export but no smart-
    meter consumption sensor) must still see their solar credit
    accrue, mirroring the static-path behaviour."""
    from custom_components.be_electricity_prices import coordinator

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

    with patch.object(coordinator, "_recorder_hourly_kwh", new=_fake_hourly):
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
    from custom_components.be_electricity_prices import coordinator

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

    with patch.object(coordinator, "_recorder_hourly_kwh", new=_fake_hourly):
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


async def test_year_cost_dynamic_falls_back_to_fees_when_no_spots(
    hass: HomeAssistant, freezer: Any
) -> None:
    """When the historical-spots cache is empty (cold start, ENTSO-E
    fetch failed entirely), the dynamic YTD must still produce the
    fees-only floor rather than crashing or returning None."""
    from custom_components.be_electricity_prices import coordinator

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
    with patch.object(coordinator, "_recorder_hourly_kwh", new=_fake_hourly):
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


def test_read_kwh_passes_native_kwh_unchanged(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "sensor.house_total_kwh", "1234.5", {"unit_of_measurement": "kWh"}
    )
    assert _read_kwh(hass, "sensor.house_total_kwh") == pytest.approx(1234.5)


def test_read_kwh_treats_missing_unit_as_kwh(hass: HomeAssistant) -> None:
    """Pre-existing entries that picked a sensor with no
    unit_of_measurement (rare, but exists) keep working."""
    hass.states.async_set("sensor.house_unitless", "42", {})
    assert _read_kwh(hass, "sensor.house_unitless") == pytest.approx(42.0)


def test_read_kwh_scales_wh_to_kwh(hass: HomeAssistant) -> None:
    """A meter exporting Wh would otherwise read 1 234 500 as kWh and
    inflate current_year_cost by 1000x."""
    hass.states.async_set("sensor.house_wh", "1234500", {"unit_of_measurement": "Wh"})
    assert _read_kwh(hass, "sensor.house_wh") == pytest.approx(1234.5)


def test_read_kwh_scales_mwh_to_kwh(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.house_mwh", "1.2345", {"unit_of_measurement": "MWh"})
    assert _read_kwh(hass, "sensor.house_mwh") == pytest.approx(1234.5)


def test_read_kwh_rejects_non_energy_unit(hass: HomeAssistant) -> None:
    """A user that mistakenly picked a power sensor (W) for the
    cumulative kWh slot must NOT have its instantaneous reading folded
    into the year cost."""
    hass.states.async_set("sensor.house_w", "1500", {"unit_of_measurement": "W"})
    assert _read_kwh(hass, "sensor.house_w") is None
