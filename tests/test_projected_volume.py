"""Projected calendar-year consumption and injection."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import energy_meters
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.projected_volume import (
    _compute_projected_year_kwh,
    _elapsed_share,
)
from tests import make_entry
from tests.test_bi_hourly_sensors import _added


def _entry(**data: object) -> MockConfigEntry:
    base: dict[str, object] = {
        "consumption_kwh": "sensor.cons",
        "injection_kwh": "sensor.inj",
    }
    base.update(data)
    return MockConfigEntry(domain=DOMAIN, data=base)


def _recorder(per_day: Callable[[str, date], float | None]) -> Any:
    """Recorder fake: ``per_day`` gives a day's kWh, ``None`` for no bucket."""

    async def _fake(
        _hass: object, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        out: dict[date, float] = {}
        day = start
        while day <= end:
            kwh = per_day(entity_id, day)
            if kwh is not None:
                out[day] = kwh
            day += timedelta(days=1)
        return out

    return _fake


async def _project(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    per_day: Callable[[str, date], float | None],
    *,
    side: str = "consumption",
    **kw: Any,
) -> tuple[float | None, dict[str, Any]]:
    diag: dict[str, Any] = {}
    with patch.object(energy_meters, "_recorder_daily_kwh", new=_recorder(per_day)):
        got = await _compute_projected_year_kwh(
            hass, entry, dt_util.now().date(), side=side, breakdown=diag, **kw
        )
    return got, diag


def _this_and_last(this: float, last: float) -> Callable[[str, date], float | None]:
    return lambda _e, day: this if day.year == 2026 else last


async def test_the_rest_of_the_year_is_last_years_same_days(
    hass: HomeAssistant, freezer: Any
) -> None:
    """268 closed days this year, plus 26 September to 31 December of 2025.

    Today is part of the rest, so the figure does not move with the live
    reading.
    """
    freezer.move_to("2026-09-26 12:00:00+02:00")
    got, diag = await _project(hass, _entry(), _this_and_last(10.0, 15.0))
    assert got == pytest.approx(268 * 10.0 + 97 * 15.0)
    assert diag["ytd_kwh"] == pytest.approx(2680.0)
    assert diag["remaining_kwh"] == pytest.approx(1455.0)
    assert diag["volume_basis"] == (
        "measured: 268 days this year, and the same 97 days of last year"
    )


async def test_a_few_missing_days_are_scaled_across(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A handful of absent recorder buckets is a rounding error, not a
    reason to drop the measurement, on either half."""
    freezer.move_to("2026-09-26 12:00:00+02:00")

    def per_day(_e: str, day: date) -> float | None:
        if date(2026, 3, 1) <= day <= date(2026, 3, 5):
            return None
        if date(2025, 11, 1) <= day <= date(2025, 11, 3):
            return None
        return 10.0 if day.year == 2026 else 15.0

    got, diag = await _project(hass, _entry(), per_day)
    assert diag["ytd_kwh"] == pytest.approx(2680.0)
    assert diag["remaining_kwh"] == pytest.approx(1455.0)
    assert got == pytest.approx(4135.0)


async def test_a_meter_wired_mid_year_is_not_projected(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    got, diag = await _project(
        hass,
        _entry(),
        lambda _e, day: 10.0 if day >= date(2026, 6, 1) else None,
    )
    assert got is None
    assert diag["volume_basis"] == (
        "not projected: the consumption meter recorded 117 of the 268 days "
        "since 1 January"
    )


async def test_no_meter_is_named(hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    got, diag = await _project(
        hass,
        MockConfigEntry(domain=DOMAIN, data={}),
        _this_and_last(10.0, 15.0),
        side="injection",
    )
    assert got is None
    assert diag["volume_basis"] == "not projected: no injection meter is wired"


async def test_the_profile_extrapolates_when_last_year_is_missing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Days from today carry twice the weight of the days before, so the rest
    is twice this year's daily mean per day rather than the day count."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    profile = {
        (day.month, day.day, 0): 1.0 if day < date(2026, 9, 26) else 2.0
        for day in (date(2026, 1, 1) + timedelta(days=i) for i in range(365))
    }
    got, diag = await _project(
        hass,
        _entry(),
        lambda _e, day: 10.0 if day.year == 2026 else None,
        profile=profile,
    )
    assert diag["remaining_kwh"] == pytest.approx(97 * 20.0)
    assert got == pytest.approx(2680.0 + 1940.0)
    assert diag["volume_basis"] == (
        "measured: 268 days this year, the rest extrapolated on Synergrid's "
        "residential load profile"
    )


async def test_without_last_year_or_a_profile_there_is_no_number(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    got, diag = await _project(
        hass, _entry(), lambda _e, day: 10.0 if day.year == 2026 else None
    )
    assert got is None
    assert diag["volume_basis"] == (
        "not projected: last year's history does not cover 2025-09-26 to "
        "2025-12-31, and no Synergrid residential load profile is held"
    )


async def test_a_short_year_is_not_extrapolated(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Six weeks of winter on a profile is the same guess the trailing-year
    volume refuses below ``MEASURED_MIN_DAYS``."""
    freezer.move_to("2026-02-15 12:00:00+01:00")
    got, diag = await _project(
        hass,
        _entry(),
        lambda _e, day: 10.0 if day.year == 2026 else None,
        profile={(1, 1, 0): 1.0, (6, 1, 0): 1.0},
    )
    assert got is None
    assert diag["volume_basis"].endswith(
        "and 45 days of this year are too few to extrapolate"
    )


async def test_on_new_years_day_the_whole_year_is_last_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-01-01 12:00:00+01:00")
    got, diag = await _project(hass, _entry(), _this_and_last(99.0, 10.0))
    assert got == pytest.approx(3650.0)
    assert diag["ytd_kwh"] == 0.0


async def test_the_leap_day_reads_the_28th(hass: HomeAssistant, freezer: Any) -> None:
    """``date.replace`` raises on 29 February of a common year."""
    freezer.move_to("2028-02-29 12:00:00+01:00")
    got, diag = await _project(
        hass, _entry(), lambda _e, day: 10.0 if day.year == 2028 else 5.0
    )
    assert diag["remaining_kwh"] == pytest.approx(307 * 5.0)
    assert got == pytest.approx(59 * 10.0 + 307 * 5.0)


async def test_injection_reads_its_own_meter(hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    got, diag = await _project(
        hass,
        _entry(),
        lambda entity, day: {"sensor.cons": 10.0, "sensor.inj": 4.0}[entity],
        side="injection",
    )
    assert got == pytest.approx(365 * 4.0)
    assert diag["ytd_kwh"] == pytest.approx(268 * 4.0)


def test_the_solar_profile_is_cut_on_its_own_clock(freezer: Any) -> None:
    """The SPP is keyed on UTC, where local midnight of 26 September is 22:00
    on the 25th; the RLP is keyed on local time."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    weights = {(9, 25, 21): 1.0, (9, 25, 22): 1.0}
    today = date(2026, 9, 26)
    assert _elapsed_share(weights, today, utc=True) == pytest.approx(0.5)
    assert _elapsed_share(weights, today, utc=False) == pytest.approx(1.0)


def test_the_injection_projection_follows_the_solar_regime() -> None:
    assert "projected_year_consumption" in _added(make_entry())
    assert "projected_year_injection" not in _added(make_entry())
    for regime in ("injection", "compensation"):
        keys = _added(make_entry(solar_regime=regime, solar_kva=5.0))
        assert "projected_year_injection" in keys
