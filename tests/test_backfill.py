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

from custom_components.be_electricity_prices import energy_meters

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import backfill as bf
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers.base import (
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import make_entry, make_snapshot

# Belgian integration: tests pin Europe/Brussels via conftest, but
# tz-sensitive constants in this file spell it out so the intent is
# clear at the assertion site rather than implicit through the
# DEFAULT_TIME_ZONE indirection.
BRUSSELS = ZoneInfo("Europe/Brussels")


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


def test_compensation_kva_invalid_inputs_clamp_to_zero() -> None:
    """The kVA half of the eligibility gate: missing key, non-numeric,
    negative. backfill's own _solar_kva used to hold this branch while the
    regime and region halves lived 580 lines away and in coordinator; the
    whole gate is now one function that all three cost paths call."""
    from custom_components.be_electricity_prices.fees import _compensation_kva

    eligible = {"solar_regime": "compensation", "region": "wallonia"}
    e_missing = SimpleNamespace(data={**eligible})
    e_bad = SimpleNamespace(data={**eligible, "solar_kva": "not-a-number"})
    e_neg = SimpleNamespace(data={**eligible, "solar_kva": -2.5})
    e_ok = SimpleNamespace(data={**eligible, "solar_kva": 5.0})
    assert _compensation_kva(e_missing) == 0.0  # type: ignore[arg-type]
    assert _compensation_kva(e_bad) == 0.0  # type: ignore[arg-type]
    assert _compensation_kva(e_neg) == 0.0  # type: ignore[arg-type]
    assert _compensation_kva(e_ok) == 5.0  # type: ignore[arg-type]

    # The other two halves of the gate, which backfill used to apply
    # separately and 580 lines apart.
    for override in ({"solar_regime": "injection"}, {"region": "flanders"}):
        entry = SimpleNamespace(data={**eligible, "solar_kva": 5.0, **override})
        assert _compensation_kva(entry) == 0.0, override  # type: ignore[arg-type]


def test_normalize_window_defaults_to_jan1_through_now() -> None:
    fixed_now = datetime(2026, 5, 4, 13, 30, tzinfo=BRUSSELS)
    with patch.object(dt_util, "now", return_value=fixed_now):
        start_utc, end_utc = bf._normalize_window(None, None)
    assert start_utc == datetime(2026, 1, 1, 0, 0, tzinfo=BRUSSELS).astimezone(UTC)
    # End is floored to the top of the current hour, exclusive of the
    # in-progress hour.
    assert end_utc == datetime(2026, 5, 4, 13, 0, tzinfo=BRUSSELS).astimezone(UTC)


def test_normalize_window_treats_naive_datetime_as_local_tz() -> None:
    naive = datetime(2026, 3, 1, 6, 0)  # no tzinfo
    fixed_now = datetime(2026, 5, 4, 13, 30, tzinfo=BRUSSELS)
    with patch.object(dt_util, "now", return_value=fixed_now):
        start_utc, _ = bf._normalize_window(naive, None)
    expected = naive.replace(tzinfo=BRUSSELS).astimezone(UTC)
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


async def test_existing_stat_window_probes_a_multiday_window(
    hass: HomeAssistant,
) -> None:
    # The probe spans more than the single anchor hour so a dynamic
    # contract whose Jan 1 00:00 spot is genuinely missing (that hour
    # skipped during backfill) is not re-backfilled in full on every
    # restart.
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with patch(
        "homeassistant.components.recorder.get_instance",
        return_value=instance,
    ):
        await bf._existing_stat_window(
            hass, "sensor.x", datetime(2026, 1, 1, tzinfo=UTC)
        )
    # statistics_during_period args: (func, hass, start, end, ids, ...).
    call_args = instance.async_add_executor_job.call_args.args
    start, end = call_args[2], call_args[3]
    assert end - start >= timedelta(days=1)


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
    return make_snapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.18, yearly_fixed_fee=72.0),
        taxes=TaxOverlay(
            federal_excise=0.05,
            energy_contribution=0.002,
            energy_fund_eur_per_month=1.5,
        ),
    )


def _entry() -> MockConfigEntry:
    return make_entry(title="Eneco Fix", solar_regime="none")


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

    start = datetime(2026, 5, 1, 0, 0, tzinfo=BRUSSELS)
    end = datetime(2026, 5, 1, 3, 0, tzinfo=BRUSSELS)
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

    start = datetime(2026, 5, 1, 0, 0, tzinfo=BRUSSELS)
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


async def test_cost_backfill_midyear_start_anchors_sum_at_jan1(
    hass: HomeAssistant,
) -> None:
    # A mid-year start must still emit a year-to-date sum: the cost
    # series accumulates silently from Jan 1 and only writes rows inside
    # the requested window, so the first row carries the Jan 1 -> start
    # accrual instead of restarting at ~0 and clashing with the existing
    # cumulative series.
    entry = _entry()
    entry.add_to_hass(hass)
    ids = _register_sensors(hass, entry, ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    start = datetime(2026, 3, 1, 0, 0, tzinfo=BRUSSELS)
    end = start + timedelta(hours=3)
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
    # Only the three requested hours are written, not the whole Jan 1 ->
    # March accumulation.
    assert len(cost_rows) == 3
    # 90 EUR/year of fees (72 fixed + 12 * 1.5 fund) over 365*24 hours is
    # ~0.0103 EUR/h; Jan 1 -> March 1 is ~1416 h, so the first emitted
    # row must already carry well over 10 EUR rather than a single hour.
    assert cost_rows[0]["state"] > 10.0
    states = [r["state"] for r in cost_rows]
    assert all(states[i] < states[i + 1] for i in range(len(states) - 1))


async def test_a_year_crossing_window_says_it_skipped_the_cost_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """services.yaml documents `end` as the first hour NOT to backfill, so a
    whole year is start = 1 Jan YYYY, end = 1 Jan YYYY+1. `end` is clamped to
    now, so such a window is always a PAST year.

    Two bugs met here. Anchoring the cost window on the exclusive end took the
    year off the NEXT year and landed on end_utc itself, so the cost window
    came out empty and the service reported success having written a full year
    of price rows and zero cost rows. Anchoring on the last hour actually
    backfilled names the right year -- but that year's cost series would then
    sit immediately before the current year's under one statistic id, and the
    recorder ignores last_reset on imported rows, so the join paints roughly
    minus one annual bill onto the Energy dashboard.

    So the cost leg is skipped for these windows. What the original bug was
    really about survives as the requirement: the skip must not be silent.
    """
    freezer.move_to("2026-08-05 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    ids = _register_sensors(hass, entry, ["current_price", "current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    # A short year-crossing window keeps the test quick while still exercising
    # the anchor: end lands exactly on 1 January.
    start = datetime(2025, 12, 31, 21, 0, tzinfo=BRUSSELS)
    end = datetime(2026, 1, 1, 0, 0, tzinfo=BRUSSELS)
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

    cost_rows = next(
        (rows for sid, rows in captured if sid == ids["current_year_cost"]), []
    )
    assert not cost_rows, "a past year's cost series must not be imported"
    assert any(sid == ids["current_price"] for sid, _ in captured), (
        "the price sensors are unaffected by the year boundary and must still "
        "be rebuilt"
    )
    assert "skipped" in result and "cost" in result["skipped"], (
        "zero cost rows without a word is exactly the original bug"
    )

    # The guard is for finished years only. A current-year window still
    # rebuilds the cost sensor, including one that reaches back into last
    # year: its cost leg is anchored on this year's 1 January and crosses
    # nothing.
    for begin in (
        datetime(2026, 1, 1, tzinfo=BRUSSELS),
        datetime(2025, 12, 31, tzinfo=BRUSSELS),
    ):
        captured.clear()
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
            result = await bf.backfill_range(
                hass, entry, begin, datetime(2026, 3, 1, tzinfo=BRUSSELS)
            )
        assert any(sid == ids["current_year_cost"] for sid, _ in captured), begin
        assert "skipped" not in result, begin


async def test_clear_is_only_guarded_when_the_cost_leg_is_in_play(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The clear guard protects the cost series; without one there is nothing
    to protect.

    A window ending in a finished year leaves the cost series out of both the
    wipe and the re-import, so refusing it denied the price rebuild the user
    asked for -- and the message was false for exactly that window: a start in
    the year BEFORE the one typed as `end` is not "after 1 January of the end
    year", and the remedy it suggests is already satisfied.
    """
    freezer.move_to("2026-08-05 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    _register_sensors(hass, entry, ["current_price", "current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    cleared: list[list[str]] = []

    async def _spy(_hass: object, sids: list[str]) -> None:
        cleared.append(list(sids))

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})

    def _noop_import(_hass: HomeAssistant, _meta: Any, _stats: Any) -> None:
        return None

    async def _run(start: datetime, end: datetime) -> dict[str, Any]:
        with (
            patch.object(bf, "BePricesCoordinator", SimpleNamespace),
            patch.object(bf, "_clear_all", _spy),
            patch(
                "homeassistant.components.recorder.statistics.async_import_statistics",
                new=_noop_import,
            ),
            patch(
                "homeassistant.components.recorder.get_instance", return_value=instance
            ),
        ):
            return await bf.backfill_range(hass, entry, start, end, clear=True)

    # Second half of a finished year: allowed, prices wiped and re-imported,
    # the cost series left alone.
    result = await _run(
        datetime(2025, 7, 1, tzinfo=BRUSSELS), datetime(2026, 1, 1, tzinfo=BRUSSELS)
    )
    assert cleared, "the price series should still be wiped"
    assert not any("current_year_cost" in sid for sid in cleared[0])
    assert "skipped" in result

    # A narrow CURRENT-year window still refuses: there the wipe really would
    # delete cost rows the re-import does not put back.
    with pytest.raises(ServiceValidationError, match="clear=True"):
        await _run(
            datetime(2026, 6, 1, tzinfo=BRUSSELS), datetime(2026, 8, 1, tzinfo=BRUSSELS)
        )


async def test_backfill_range_rejects_clear_with_midyear_window(
    hass: HomeAssistant,
) -> None:
    # clear=True wipes the WHOLE series; a window starting after Jan 1
    # would leave the cleared Jan 1 -> start rows gone for good. The
    # combination must be refused rather than silently destroy data.
    entry = _entry()
    entry.add_to_hass(hass)
    _register_sensors(hass, entry, ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    start = datetime(2026, 3, 1, 0, 0, tzinfo=BRUSSELS)
    end = start + timedelta(hours=3)
    with patch.object(bf, "BePricesCoordinator", SimpleNamespace):
        with pytest.raises(ServiceValidationError, match="starts after 1 January"):
            await bf.backfill_range(hass, entry, start, end, clear=True)


async def test_cost_backfill_multiyear_stays_in_end_year_without_sum_drop(
    hass: HomeAssistant,
) -> None:
    # current_year_cost resets each Jan 1 and the recorder derives change
    # as sum - prev_sum (ignoring last_reset for imported stats), so a
    # multi-year request must backfill only the end year's cost -- never
    # crossing a Jan 1 boundary that would drop the sum to ~0 and paint a
    # large spurious negative cost on the Energy dashboard.
    entry = _entry()
    entry.add_to_hass(hass)
    ids = _register_sensors(hass, entry, ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    start = datetime(2024, 6, 1, 0, 0, tzinfo=BRUSSELS)
    end = datetime(2026, 3, 1, 0, 0, tzinfo=BRUSSELS)
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
    jan1_end_year = datetime(2026, 1, 1, tzinfo=BRUSSELS).astimezone(UTC)
    assert cost_rows  # the end year is backfilled
    # No row predates Jan 1 of the end year (earlier years aren't written).
    assert all(r["start"] >= jan1_end_year for r in cost_rows)
    # The sum never decreases -- no year-boundary drop -> no negative spike.
    sums = [r["sum"] for r in cost_rows]
    assert all(sums[i] <= sums[i + 1] for i in range(len(sums) - 1))


async def test_cost_backfill_injection_uses_spp_not_flat_mean(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A custom monthly entry that opted into SPP-weighted injection must
    have its backfilled cost credit injection at the SPP-weighted month
    mean, matching the live YTD credit, not the plain flat mean."""
    from custom_components.be_electricity_prices import const
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
    )

    freezer.move_to("2026-07-15 12:00:00+02:00")
    snap = make_snapshot(
        supplier="custom",
        contract=const.CUSTOM_CONTRACT_MONTHLY,
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        injection=InjectionRates(factor=0.5, base=0.0, floor_at_zero=False),
    )
    entry = make_entry(
        supplier=const.SUPPLIER_CUSTOM,
        contract=const.CUSTOM_CONTRACT_MONTHLY,
        region=const.REGION_WALLONIA,
        dso=const.DSO_ORES,
        meter=const.METER_MONO,
        title="Custom SPP",
        solar_regime=const.SOLAR_REGIME_INJECTION,
        injection_kwh="sensor.inj_total",
        dso_tariff_mode=const.DSO_MODE_BI_HORAIRE,
        custom_injection_spp_weighted=True,
        custom_injection_mode=const.CUSTOM_INJECTION_MODE_FORMULA,
    )
    entry.add_to_hass(hass)
    ids = _register_sensors(hass, entry, ["current_year_cost"])

    # Flat month mean = 0.20; SPP-weighted mean = 0.15 (hour 10 counts 3x).
    hour10 = datetime(2026, 6, 15, 10, tzinfo=UTC)
    hour11 = datetime(2026, 6, 15, 11, tzinfo=UTC)
    spots = {hour10: 0.10, hour11: 0.30}
    weights = {(6, 15, 10): 3.0, (6, 15, 11): 1.0}

    async def _ensure() -> None:
        return None

    coord = SimpleNamespace(_snapshot=snap, _session=None, _spp_weights=weights)
    coord._ensure_spp_weights = _ensure

    async def _snap_for(_month_first: object) -> Any:
        return snap

    def _fake_cache(*_a: object, **_k: object) -> Any:
        return _snap_for

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        return {hour10: 1.0} if entity_id == "sensor.inj_total" else {}

    captured: list[tuple[str, list[Any]]] = []

    def _fake_import(_hass: HomeAssistant, metadata: Any, statistics: Any) -> None:
        captured.append((metadata["statistic_id"], list(statistics)))

    with (
        patch.object(bf, "_month_snapshot_cache", _fake_cache),
        patch.object(energy_meters, "_recorder_hourly_kwh", new=_fake_hourly),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=_fake_import,
        ),
    ):
        await bf._backfill_cost_sensor(hass, entry, coord, [hour10, hour11], spots)  # type: ignore[arg-type]

    cost_rows = next(rows for sid, rows in captured if sid == ids["current_year_cost"])
    # 1 kWh injected, no consumption, no static fees on this snapshot: the
    # running cost is the injection credit alone. factor 0.5 x SPP mean 0.15
    # = -0.075; the flat mean 0.20 (the old behaviour) would give -0.10.
    assert cost_rows[-1]["state"] == pytest.approx(-(0.5 * 0.15))


async def test_backfill_range_without_runtime_data_raises(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    # No runtime_data assigned -- the helper must refuse rather than
    # crash mid-way through statistic writes.
    with pytest.raises(RuntimeError, match="no live coordinator"):
        await bf.backfill_range(hass, entry)


async def test_backfill_service_during_reload_raises_validation() -> None:
    """The backfill_statistics service must surface a localized
    ServiceValidationError, not the raw RuntimeError backfill_range raises,
    when called while the entry is reloading (runtime_data not a coordinator)."""
    import custom_components.be_electricity_prices as pkg

    entry = SimpleNamespace(entry_id="x", runtime_data=None)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_loaded_entries=lambda _domain: [entry])
    )
    call = SimpleNamespace(hass=hass, data={})
    with pytest.raises(ServiceValidationError, match="reloading"):
        await pkg._async_backfill_service(call)  # type: ignore[arg-type]


async def test_backfill_service_without_snapshot_raises_validation() -> None:
    """A coordinator that exists but has no snapshot yet (pre-first-refresh)
    also surfaces a localized ServiceValidationError."""
    import custom_components.be_electricity_prices as pkg

    coord = SimpleNamespace(_snapshot=None)
    entry = SimpleNamespace(entry_id="x", runtime_data=coord)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_loaded_entries=lambda _domain: [entry])
    )
    call = SimpleNamespace(hass=hass, data={})
    with (
        patch.object(pkg, "BePricesCoordinator", SimpleNamespace),
        pytest.raises(ServiceValidationError, match="snapshot"),
    ):
        await pkg._async_backfill_service(call)  # type: ignore[arg-type]


# ---- _ensure_dynamic_spots gate -----------------------------------------------


async def test_ensure_dynamic_spots_fetches_for_spot_indexed_injection() -> None:
    """A static-energy card whose injection is spot-indexed (Cociter
    Variable shape) must still trigger a spot backfill on the injection
    regime, so the feed-in credit lands in the backfilled cost and price
    rows instead of dropping at the backfill->live seam."""
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )

    snap = make_snapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=VariableRates(current=0.17),
        injection=InjectionRates(current=None, factor=0.925, base=-0.0125),
    )
    cache = {datetime(2026, 1, 1, tzinfo=UTC): 0.05}
    coordinator = SimpleNamespace(
        _snapshot=snap,
        _historical_spots=cache,
        _ensure_historical_spots=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_variable",
            "region": "flanders",
            "dso": "fluvius",
            "meter": "mono",
            "solar_regime": "injection",
        },
        title="Cociter Variable",
    )
    # end lands on a local-day boundary: 2026-03-31 23:00 UTC is
    # 2026-04-01 01:00 in Brussels (CEST). The spot fetch must use the
    # LOCAL date (2026-04-01) so the final UTC hour is covered, not the
    # UTC date (2026-03-31) which would leave it unfetched.
    spots = await bf._ensure_dynamic_spots(
        coordinator,  # type: ignore[arg-type]
        entry,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 3, 31, 23, 0, tzinfo=UTC),
    )
    coordinator._ensure_historical_spots.assert_awaited_once()
    assert coordinator._ensure_historical_spots.await_args.args == (
        date(2026, 1, 1),
        date(2026, 4, 1),
    )
    assert spots == cache


async def test_ensure_dynamic_spots_empty_for_static_non_spot_contract() -> None:
    """Static energy with no spot-indexed injection needs no spot fetch."""
    coordinator = SimpleNamespace(
        _snapshot=_fixed_snapshot(),
        _historical_spots={},
        _ensure_historical_spots=AsyncMock(),
    )
    spots = await bf._ensure_dynamic_spots(
        coordinator,  # type: ignore[arg-type]
        _entry(),
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert spots == {}
    coordinator._ensure_historical_spots.assert_not_awaited()


def test_hour_spot_uses_month_mean_for_spot_monthly() -> None:
    """A SpotMonthlyRates leg (a re-priced variable cohort) prices at the
    delivery-month arithmetic mean; every other kind uses the per-hour spot."""
    from custom_components.be_electricity_prices.providers.base import (
        DynamicRates,
        FixedRates,
        SpotMonthlyRates,
    )

    spots = {
        datetime(2026, 3, 10, 9, tzinfo=UTC): 0.10,
        datetime(2026, 3, 10, 10, tzinfo=UTC): 0.20,
    }
    hour = datetime(2026, 3, 10, 9, tzinfo=UTC)
    local = dt_util.as_local(hour)
    cache: dict[tuple[int, int], float | None] = {}
    # SpotMonthly -> month mean (0.15), NOT the per-hour 0.10.
    assert bf._hour_spot(
        SpotMonthlyRates(factor=1.0, base=0.0), local, hour, spots, cache
    ) == pytest.approx(0.15)
    # Dynamic / fixed -> per-hour spot.
    assert bf._hour_spot(
        DynamicRates(factor=1.0, base=0.0), local, hour, spots, cache
    ) == pytest.approx(0.10)
    assert bf._hour_spot(
        FixedRates(single=0.2), local, hour, spots, cache
    ) == pytest.approx(0.10)
    # No cached spots for the month -> None (the hour is then skipped).
    assert (
        bf._hour_spot(SpotMonthlyRates(factor=1.0, base=0.0), local, hour, {}, {})
        is None
    )


async def test_ensure_dynamic_spots_fetches_for_variable_cohort() -> None:
    """A variable contract with a start date re-prices to a SpotMonthly cohort,
    which needs spots for its monthly mean; the backfill must fetch them
    (return the cache) rather than return {} and drop every cohort hour."""
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
        VariableRates,
    )

    snap = make_snapshot(
        supplier="eneco", contract="power_flex", energy=VariableRates(current=0.14)
    )
    cache = {datetime(2026, 1, 1, tzinfo=UTC): 0.05}
    coordinator = SimpleNamespace(
        hass=MagicMock(),
        _session=MagicMock(),
        _snapshot=snap,
        _historical_spots=cache,
        _ensure_historical_spots=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_flex",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "contract_start_date": "2025-11-10",
        },
        title="Eneco Flex cohort",
    )

    async def _fake_cohort(*_a: object, **_k: object) -> SpotMonthlyRates:
        return SpotMonthlyRates(factor=1.05, base=0.01)

    with patch.object(bf, "_cohort_energy_leg", new=_fake_cohort):
        spots = await bf._ensure_dynamic_spots(
            coordinator,  # type: ignore[arg-type]
            entry,
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 6, 1, tzinfo=UTC),
        )
    assert spots == cache
    coordinator._ensure_historical_spots.assert_awaited()


async def test_ensure_dynamic_spots_empty_for_variable_without_start_date() -> None:
    """Same variable contract, no start date: no cohort, static energy, {}."""
    from custom_components.be_electricity_prices.providers.base import VariableRates

    snap = make_snapshot(
        supplier="eneco", contract="power_flex", energy=VariableRates(current=0.14)
    )
    coordinator = SimpleNamespace(
        hass=MagicMock(),
        _session=MagicMock(),
        _snapshot=snap,
        _historical_spots={},
        _ensure_historical_spots=AsyncMock(),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_flex",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
        title="Eneco Flex no start date",
    )
    spots = await bf._ensure_dynamic_spots(
        coordinator,  # type: ignore[arg-type]
        entry,
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
    )
    assert spots == {}
    coordinator._ensure_historical_spots.assert_not_awaited()


async def test_clear_over_a_multiyear_window_spares_the_cost_series(
    hass: HomeAssistant,
) -> None:
    """clear=True wipes a whole series, but the cost sensor is deliberately
    re-imported only over the END year. A window reaching back past 1 January
    of that year therefore destroyed every prior year's cost history and never
    put it back. The existing guard only caught windows starting AFTER the
    anchor, so the multi-year case sailed through.

    The price series stay in the wipe: they are re-imported over the whole
    requested window, so their wipe is always matched."""
    entry = _entry()
    entry.add_to_hass(hass)
    _register_sensors(hass, entry, ["current_price", "current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    cleared: list[list[str]] = []

    async def _spy(_hass: object, ids: list[str]) -> None:
        cleared.append(list(ids))

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})

    def _noop_import(_hass: HomeAssistant, _meta: Any, _stats: Any) -> None:
        return None

    start = datetime(2025, 6, 1, 0, 0, tzinfo=BRUSSELS)
    end = datetime(2026, 3, 1, 3, 0, tzinfo=BRUSSELS)
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch.object(bf, "_clear_all", _spy),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=_noop_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
    ):
        await bf.backfill_range(hass, entry, start, end, clear=True)

    assert cleared, "clear=True should still wipe something"
    wiped = cleared[0]
    assert any("current_price" in sid for sid in wiped)
    assert not any("current_year_cost" in sid for sid in wiped)

    # A window that IS exactly the end year still wipes the cost series, since
    # the re-import then covers every row the wipe removes.
    cleared.clear()
    start = datetime(2026, 1, 1, 0, 0, tzinfo=BRUSSELS)
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch.object(bf, "_clear_all", _spy),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=_noop_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
    ):
        await bf.backfill_range(hass, entry, start, end, clear=True)
    assert cleared and any("current_year_cost" in sid for sid in cleared[0])


def test_normalize_window_clamps_a_future_end_to_now() -> None:
    """compute_breakdown evaluates any hour for a static contract, so an end
    date past now wrote phantom price rows and kept the cost accrual running
    into hours that have not happened. The backfill_statistics schema has no
    upper bound, so a mistyped year was enough. The None default already
    stopped at now; an explicit end gets the same bound."""
    now = dt_util.now()
    _start, end = bf._normalize_window(None, now + timedelta(days=365))
    assert end <= bf._floor_to_hour_utc(now)

    # A past end is untouched.
    past = now - timedelta(days=30)
    _start, end = bf._normalize_window(None, past)
    assert end == bf._floor_to_hour_utc(past)


def test_backfill_credits_the_card_indicative_for_an_spp_card_without_a_profile() -> (
    None
):
    """The strict= wiring in the backfill's own injection helper.

    The backfill writes long-term statistics, so a wrong rate here is
    persisted history rather than a value that self-corrects on the next
    tick. An SPP-indexed card with no Synergrid profile must fall through to
    the card's printed indicative; resolving the formula against the ENERGY
    leg's mean would write 0,5 x 0,20 = 0,10 EUR/kWh into the recorder where
    the card says 0,03.
    """
    from datetime import UTC, datetime

    from custom_components.be_electricity_prices.backfill import (
        _injection_rate_for_hour,
    )
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
    )
    from tests import make_snapshot

    snap = make_snapshot(
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        injection=InjectionRates(current=0.03, factor=0.5, base=0.0, spp_indexed=True),
    )
    utc_hour = datetime(2026, 6, 15, 10, tzinfo=UTC)
    rate = _injection_rate_for_hour(
        snap,
        spot=0.20,  # the ENERGY leg's flat month mean - the wrong index here
        spots={utc_hour: 0.20},
        utc_hour=utc_hour,
        local=dt_util.as_local(utc_hour),
        spp_weights=None,  # no Synergrid profile
        month_spp_cache={},
        hourly_injection=False,
    )
    assert rate == pytest.approx(0.03)
