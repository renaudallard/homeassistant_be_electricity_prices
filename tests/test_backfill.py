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

"""Tests for the long-term-statistics backfill module."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import backfill as bf
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
)


# ---- pure helpers -------------------------------------------------------------


def test_hour_iter_inclusive_start_exclusive_end() -> None:
    start = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    assert bf._hour_iter(start, end) == [
        datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        datetime(2026, 5, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
    ]


def test_hour_iter_aligns_unaligned_start_up_to_next_hour() -> None:
    # A start that lands at :30 must not generate a :30 row; round up.
    start = datetime(2026, 5, 1, 0, 30, tzinfo=UTC)
    end = datetime(2026, 5, 1, 3, 0, tzinfo=UTC)
    assert bf._hour_iter(start, end) == [
        datetime(2026, 5, 1, 1, 0, tzinfo=UTC),
        datetime(2026, 5, 1, 2, 0, tzinfo=UTC),
    ]


def test_hour_iter_empty_when_start_equals_end() -> None:
    when = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    assert bf._hour_iter(when, when) == []


def test_hours_in_month_handles_leap_and_non_leap_february() -> None:
    assert bf._hours_in_month(date(2024, 2, 1)) == 29 * 24  # leap
    assert bf._hours_in_month(date(2025, 2, 1)) == 28 * 24
    assert bf._hours_in_month(date(2026, 4, 1)) == 30 * 24
    assert bf._hours_in_month(date(2026, 12, 1)) == 31 * 24  # rolls into next year


def test_solar_kva_invalid_inputs_clamp_to_zero() -> None:
    # Each branch of the helper: missing key, non-numeric, negative.
    e_missing = SimpleNamespace(data={})
    e_bad = SimpleNamespace(data={"solar_kva": "not-a-number"})
    e_neg = SimpleNamespace(data={"solar_kva": -2.5})
    e_ok = SimpleNamespace(data={"solar_kva": 5.0})
    assert bf._solar_kva(e_missing) == 0.0  # type: ignore[arg-type]
    assert bf._solar_kva(e_bad) == 0.0  # type: ignore[arg-type]
    assert bf._solar_kva(e_neg) == 0.0  # type: ignore[arg-type]
    assert bf._solar_kva(e_ok) == 5.0  # type: ignore[arg-type]


def test_normalize_window_defaults_to_jan1_through_now() -> None:
    fixed_now = datetime(2026, 5, 4, 13, 30, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    with patch.object(dt_util, "now", return_value=fixed_now):
        start_utc, end_utc = bf._normalize_window(None, None)
    assert start_utc == datetime(
        2026, 1, 1, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE
    ).astimezone(UTC)
    # End is floored to the top of the current hour, exclusive of the
    # in-progress hour.
    assert end_utc == datetime(
        2026, 5, 4, 13, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE
    ).astimezone(UTC)


def test_normalize_window_treats_naive_datetime_as_local_tz() -> None:
    naive = datetime(2026, 3, 1, 6, 0)  # no tzinfo
    fixed_now = datetime(2026, 5, 4, 13, 30, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    with patch.object(dt_util, "now", return_value=fixed_now):
        start_utc, _ = bf._normalize_window(naive, None)
    expected = naive.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE).astimezone(UTC)
    assert start_utc == expected


# ---- existing-stat probe ------------------------------------------------------


async def test_existing_stat_window_true_when_recorder_returns_rows(
    hass: HomeAssistant,
) -> None:
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(
        return_value={"sensor.x": [{"start": 0.0, "mean": 0.18}]}
    )
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        present = await bf._existing_stat_window(
            hass, "sensor.x", datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert present is True


async def test_existing_stat_window_false_when_recorder_returns_empty(
    hass: HomeAssistant,
) -> None:
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        present = await bf._existing_stat_window(
            hass, "sensor.x", datetime(2026, 1, 1, tzinfo=UTC)
        )
    assert present is False


async def test_existing_stat_window_swallows_recorder_exceptions(
    hass: HomeAssistant,
) -> None:
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=RuntimeError("boom"))
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        present = await bf._existing_stat_window(
            hass, "sensor.x", datetime(2026, 1, 1, tzinfo=UTC)
        )
    # Errors collapse to "no rows" so the auto path retries; swallowing
    # them avoids a recorder hiccup blocking entry setup.
    assert present is False


# ---- end-to-end backfill_range ------------------------------------------------


def _fixed_snapshot() -> SupplierSnapshot:
    # yearly_fixed_fee=72 + energy_fund=1.5/month gives a clearly
    # non-zero fee accrual so the cost-backfill series can be checked
    # for strict monotonic growth even with no kWh sensors wired.
    return SupplierSnapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.18, yearly_fixed_fee=72.0),
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            energy_fund_eur_per_month=1.5,
        ),
        source_url="test://",
    )


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
        },
        title="Eneco Fix",
    )


def _register_sensors(
    hass: HomeAssistant, entry: MockConfigEntry, keys: list[str]
) -> dict[str, str]:
    """Pre-create entity-registry rows so _stat_id finds the entity ids."""
    reg = er.async_get(hass)
    out: dict[str, str] = {}
    for key in keys:
        e = reg.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=f"eneco_fix_{key}",
            config_entry=entry,
        )
        out[key] = e.entity_id
    return out


async def _make_coordinator(entry: MockConfigEntry) -> Any:
    """Minimal coordinator stand-in -- just the attributes backfill reads."""
    return SimpleNamespace(
        _snapshot=_fixed_snapshot(),
        _session=None,
        _historical_spots={},
        _ensure_historical_spots=AsyncMock(),
    )


async def test_backfill_range_writes_one_mean_row_per_hour_per_price_sensor(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    sensor_keys = [
        "current_price",
        "energy_component",
        "network_component",
        "taxes_component",
    ]
    ids = _register_sensors(hass, entry, sensor_keys + ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)
    # The coordinator stand-in doesn't subclass BePricesCoordinator, so
    # patch the isinstance check used by backfill_range to gate on it.
    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    start = datetime(2026, 5, 1, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    end = datetime(2026, 5, 1, 3, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=_fake_import,
        ),
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=instance,
        ),
    ):
        result = await bf.backfill_range(hass, entry, start, end)

    written = {sid: rows for sid, rows in captured}
    # Three hours -> three rows for each price sensor; cost sensor also
    # receives three rows.
    for key in sensor_keys:
        assert len(written[ids[key]]) == 3
    assert len(written[ids["current_year_cost"]]) == 3
    # current_price all_in for a fixed supplier with the test snapshot:
    # 0.18 (energy) + 0.10 + 0.0145 (network) + 0.05 + 0.002 (taxes)
    # = 0.3465 EUR/kWh -- compute_breakdown rounds, but should be close.
    cur_rows = written[ids["current_price"]]
    means = [r["mean"] for r in cur_rows]
    assert all(m == pytest.approx(means[0]) for m in means)
    assert means[0] > 0.30
    # rows_written reported in the response is the sum across statistic ids.
    assert result["rows_written"] == sum(len(r) for r in written.values())


async def test_backfill_if_missing_skips_when_recorder_already_has_data(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    _register_sensors(hass, entry, ["current_price"])
    entry.runtime_data = await _make_coordinator(entry)

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(
        return_value={"sensor.eneco_fix_current_price": [{"start": 0.0, "mean": 0.3}]}
    )
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        out = await bf.backfill_if_missing(hass, entry)
    # Recorder reported a row at the Jan 1 anchor -- backfill must not run.
    assert out is None


async def test_cost_backfill_running_sum_is_monotonic_for_non_compensation(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    ids = _register_sensors(hass, entry, ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    start = datetime(2026, 5, 1, 0, 0, tzinfo=dt_util.DEFAULT_TIME_ZONE)
    end = start + timedelta(hours=4)
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=_fake_import,
        ),
        patch(
            "homeassistant.components.recorder.get_instance",
            return_value=instance,
        ),
    ):
        await bf.backfill_range(hass, entry, start, end)

    cost_rows = next(rows for sid, rows in captured if sid == ids["current_year_cost"])
    states = [r["state"] for r in cost_rows]
    sums = [r["sum"] for r in cost_rows]
    # No kWh sensors wired -> energy term is 0; only the prorated fees
    # accrue. State / sum must therefore be a strictly increasing series
    # that mirrors the per-hour fee accrual.
    assert states == sums  # within-year, state == sum for a TOTAL stat
    assert all(states[i] < states[i + 1] for i in range(len(states) - 1))


async def test_backfill_range_without_runtime_data_raises(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    # No runtime_data assigned -- the helper must refuse rather than
    # crash mid-way through statistic writes.
    with pytest.raises(RuntimeError, match="no live coordinator"):
        await bf.backfill_range(hass, entry)
