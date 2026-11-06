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


"""Warnings name a meter by the setting that holds it, never by its entity id.

The meters are the household's own entity ids. Diagnostics redact them so they
do not travel with an issue report, and the log is what gets pasted into one.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    energy_meters,
    meter_daily,
    meter_hourly,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.contract_periods import previous_periods
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.coordinator_costs import _CostsMixin
from custom_components.be_electricity_prices.energy_meters import meter_setting
from custom_components.be_electricity_prices.providers._rates import FixedRates
from tests import let_the_fetch_interval_pass, make_entry, make_snapshot
from tests.test_setup_meter_reads import _a_frank_entry, _Recorder, _settle
from tests.test_supplier_switch import _held

_IDS = ("sensor.smith_day", "sensor.smith_night", "sensor.smith_total")


def _logged(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.DEBUG]


def _no_meter_ids(lines: list[str]) -> None:
    assert lines
    assert not [line for line in lines if "smith" in line], lines


def test_a_meter_is_named_by_the_setting_holding_it(hass: HomeAssistant) -> None:
    """An earlier contract's copy counts too: its days are read off the meters
    kept with it."""
    entry = make_entry(
        day_consumption_kwh=_IDS[0],
        previous_contracts=[
            {"until": "2026-03-31", "data": {"injection_kwh": "sensor.old_out"}}
        ],
    )
    entry.add_to_hass(hass)
    assert meter_setting(hass, _IDS[0]) == "day_consumption_kwh"
    assert meter_setting(hass, "sensor.old_out") == "injection_kwh"
    assert meter_setting(hass, "sensor.someone_else") == "unnamed"


async def test_the_peak_sensor_warning_names_its_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entry = make_entry(
        region="flanders",
        dso="fluvius_antwerpen",
        capacity_mode="sensor",
        capacity_peak_sensor="sensor.smith_peak",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set("sensor.smith_peak", "5", {"unit_of_measurement": "MW"})
    with caplog.at_level(logging.WARNING):
        await coord._track_monthly_peak()
    lines = [line for line in _logged(caplog) if "reports in" in line]
    assert lines == [
        "the capacity_peak_sensor sensor reports in 'MW'; expected kW/W/VA/kVA, "
        "ignoring this update"
    ]
    _no_meter_ids(lines)


async def test_the_recorder_warnings_name_the_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture
) -> None:
    """A first bucket carrying energy from before the window, and a negative
    change, both warn about the meter."""
    make_entry(consumption_kwh=_IDS[2]).add_to_hass(hass)
    first = dt_util.start_of_local_day(date(2026, 3, 1)).astimezone(UTC)
    rows = [
        {"start": (first + timedelta(hours=h)).timestamp(), "change": c, "sum": s}
        for h, c, s in ((0, 5.0, 100.0), (1, -40.0, 60.0), (2, 3.0, 63.0))
    ]

    async def _rows(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return rows

    with (
        patch.object(energy_meters, "_recorder_rows", new=_rows),
        caplog.at_level(logging.WARNING),
    ):
        got = await energy_meters._read_deltas(
            hass, _IDS[2], date(2026, 3, 1), date(2026, 3, 1), "hour"
        )
    assert [kwh for _, kwh in got] == [3.0]
    lines = _logged(caplog)
    assert lines[0].startswith(
        "the consumption_kwh meter has no statistics immediately before"
    )
    assert lines[1].startswith(
        "the consumption_kwh meter reported -40.0 kWh of negative change"
    )
    _no_meter_ids(lines)


async def test_a_broken_register_pair_names_the_settings(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    freezer.move_to("2026-09-25 12:00:00+02:00")
    entry = make_entry(
        meter="bi",
        day_consumption_kwh=_IDS[0],
        night_consumption_kwh=_IDS[1],
        consumption_kwh=_IDS[2],
    )
    entry.add_to_hass(hass)
    start, end = date(2026, 1, 1), date(2026, 9, 25)
    spans: dict[str, tuple[date, date, float] | None] = {
        _IDS[0]: (start, end, 6.0),
        _IDS[1]: None,
        _IDS[2]: (start, end, 10.0),
    }

    async def _daily(
        _hass: object, entity_id: str, first: date, last: date
    ) -> dict[date, float]:
        span = spans[entity_id]
        if span is None:
            return {}
        days = (min(last, span[1]) - max(first, span[0])).days + 1
        return {max(first, span[0]) + timedelta(days=i): span[2] for i in range(days)}

    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=_daily),
        caplog.at_level(logging.WARNING),
    ):
        await meter_daily._measured_kwh(hass, entry, start, end, warn=True)
        spans[_IDS[1]] = (start, date(2026, 6, 30), 4.0)
        await meter_daily._measured_kwh(hass, entry, start, end, warn=True)
    lines = _logged(caplog)
    assert lines[0].startswith(
        "the night_consumption_kwh meter returned no statistics between "
        "2026-01-01 and 2026-09-25 while the day_consumption_kwh meter did, so "
        "the consumption pair cannot be billed; its totals sensor, the "
        "consumption_kwh meter, is billed instead."
    )
    assert lines[1].startswith(
        "the day_consumption_kwh meter covers 267 days between 2026-01-01 and "
        "2026-09-25 while the night_consumption_kwh meter covers 181"
    )
    _no_meter_ids(lines)


async def test_a_meter_read_once_a_day_names_its_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    freezer.move_to("2026-03-31 12:00:00+02:00")
    entry = make_entry(consumption_kwh=_IDS[2])
    entry.add_to_hass(hass)
    days = [date(2026, 3, 21) + timedelta(days=i) for i in range(9)]
    per_hour: dict[datetime, float] = {}
    for day in days:
        hour = dt_util.start_of_local_day(day).astimezone(UTC)
        stop = dt_util.start_of_local_day(day + timedelta(days=1)).astimezone(UTC)
        while hour < stop:
            per_hour[hour] = 12.0 if dt_util.as_local(hour).hour == 7 else 0.0
            hour += timedelta(hours=1)

    async def _hourly(*_a: Any) -> dict[datetime, float]:
        return dict(per_hour)

    meter_hourly._READ_DAILY_LOGGED.clear()
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=_hourly),
        caplog.at_level(logging.WARNING),
    ):
        metered = await meter_hourly._metered_hourly_kwh(
            hass, entry, "consumption", days[0], days[-1]
        )
    assert metered is not None and metered.read_daily
    lines = _logged(caplog)
    assert lines[0].startswith(
        "the consumption_kwh meter: kWh reported once a day rather than hour by hour"
    )
    _no_meter_ids(lines)


def test_entries_are_found_by_domain(hass: HomeAssistant) -> None:
    """Only this integration's entries are searched."""
    MockConfigEntry(domain="other", data={"consumption_kwh": _IDS[2]}).add_to_hass(hass)
    assert meter_setting(hass, _IDS[2]) == "unnamed"
    MockConfigEntry(domain=DOMAIN, data={"consumption_kwh": _IDS[2]}).add_to_hass(hass)
    assert meter_setting(hass, _IDS[2]) == "consumption_kwh"


def _unanswered(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [line for line in _logged(caplog) if "did not answer" in line]


async def test_a_busy_recorder_on_the_yearly_volume_names_the_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    """The reads a busy recorder did not answer are listed at debug level,
    which is what people turn on before they file an issue."""
    freezer.move_to("2026-09-25 12:00:00+02:00")
    entry = make_entry(consumption_kwh=_IDS[2])
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _busy(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[date, float]:
        energy_meters._note_failed_read(entity_id)
        return {}

    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=_busy),
        patch.object(energy_meters, "_sum_hourly_kwh", AsyncMock(return_value={})),
        caplog.at_level(logging.DEBUG),
    ):
        await coord._ensure_annual_volume()
    lines = _unanswered(caplog)
    assert lines == [
        f"{entry.entry_id}: annual volume unavailable, the recorder did not "
        "answer for consumption_kwh"
    ]
    _no_meter_ids(lines)


async def test_a_busy_recorder_on_an_earlier_contract_names_the_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    freezer.move_to("2026-10-24 00:30:00+02:00")
    held = _held(
        "totalenergies", "totalenergies_electricite_fixe", consumption_kwh=_IDS[2]
    )
    entry = make_entry(
        consumption_kwh=_IDS[2],
        previous_contracts=[{"until": "2026-07-01", "data": held}],
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.25))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    today = date(2026, 10, 24)
    periods = previous_periods(entry.data, date(2026, 1, 1), today)

    async def _busy(*_a: Any, **_k: Any) -> list[Any]:
        energy_meters._note_failed_read(_IDS[2])
        return []

    with (
        patch(
            "custom_components.be_electricity_prices.coordinator_costs."
            "price_previous_periods",
            new=_busy,
        ),
        caplog.at_level(logging.DEBUG),
    ):
        coord._schedule_previous_pricing(periods, today)
        assert coord._previous_pricing is not None
        await coord._previous_pricing
    lines = _unanswered(caplog)
    assert lines == [
        f"{entry.entry_id}: earlier contracts not priced, the recorder did not "
        "answer for consumption_kwh"
    ]
    _no_meter_ids(lines)


async def test_a_busy_recorder_on_the_tick_names_the_setting(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, freezer: Any
) -> None:
    freezer.move_to("2026-06-20 10:30:00+02:00")
    with _a_frank_entry(hass, _Recorder()) as entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass, freezer)
        coord = entry.runtime_data
        priced = _CostsMixin._tick_costs

        async def _busy(self: Any, *args: Any) -> Any:
            costs = await priced(self, *args)
            energy_meters._note_failed_read("sensor.cons_day")
            energy_meters._note_failed_read("sensor.inj_night")
            return replace(costs, current_year_cost=1.0)

        freezer.tick(timedelta(minutes=5))
        caplog.clear()
        with (
            patch.object(_CostsMixin, "_tick_costs", _busy),
            caplog.at_level(logging.DEBUG),
        ):
            let_the_fetch_interval_pass(coord)
            await coord.async_refresh()
        lines = _unanswered(caplog)
        assert lines == [
            "Recorder did not answer for day_consumption_kwh, night_injection_kwh; "
            "keeping the last costs"
        ]
        assert not [line for line in lines if "sensor." in line]
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_backfill_the_recorder_did_not_answer_names_the_setting(
    hass: HomeAssistant, freezer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The backfill's reason for leaving the cost series alone named the
    household's meter, at WARNING from the service and at INFO from the
    automatic run, and the response carried it too."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from custom_components.be_electricity_prices import backfill as bf
    from custom_components.be_electricity_prices import backfill_cost as bc
    from custom_components.be_electricity_prices import energy_meters
    from tests.test_backfill import _make_coordinator, _register_sensors

    freezer.move_to("2026-01-03 12:00:00+01:00")
    meter = "sensor.house_consumption"
    entry = make_entry(title="Eneco Fix", solar_regime="none", consumption_kwh=meter)
    entry.add_to_hass(hass)
    _register_sensors(hass, entry, ["current_year_cost"])
    entry.runtime_data = await _make_coordinator(entry)

    async def _accrue(*_a: Any, **_kw: Any) -> float:
        energy_meters._note_failed_read(meter)
        return 0.0

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    instance.async_block_till_done = AsyncMock()
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch.object(bc, "_accrue_cost", _accrue),
        patch("homeassistant.components.recorder.statistics.async_import_statistics"),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        caplog.at_level(logging.INFO),
    ):
        result = await bf.backfill_range(hass, entry)
    assert "the recorder did not answer for consumption_kwh" in result["skipped"]
    assert meter not in result["skipped"]
    logged = [r for r in caplog.records if "did not answer" in r.getMessage()]
    assert logged
    assert all(meter not in r.getMessage() for r in logged)
