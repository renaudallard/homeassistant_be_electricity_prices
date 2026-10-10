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

"""What setup reads from the recorder (issue #107).

Home Assistant waits on the first refresh of every entry, inside a startup
stage it allows 300 s for all integrations together, and each meter read
covers a year of hours: on a MariaDB on a NAS one entry took 287 s. These
run the real setup against a recorder that logs every read, answering from
a synthetic hourly series for four registers.
"""

from __future__ import annotations

import asyncio
import functools
import traceback
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.be_electricity_prices import snapshot_months
from custom_components.be_electricity_prices.api import EntsoeError
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._rates import (
    FixedRates,
    InjectionRates,
)
from custom_components.be_electricity_prices.providers.frank import parse_snapshot
from tests import fixture_text, make_snapshot

_SPOTS = "custom_components.be_electricity_prices.coordinator_spots"
_METERS = {
    "sensor.cons_day": ("cons", "day"),
    "sensor.cons_night": ("cons", "night"),
    "sensor.inj_day": ("inj", "day"),
    "sensor.inj_night": ("inj", "night"),
}
_FIGURES = (
    "current_year_cost_eur",
    "current_month_cost_eur",
    "projected_year_cost_eur",
    "projected_year_consumption_kwh",
    "projected_year_injection_kwh",
    "rolling_year_consumption_kwh",
    "rolling_year_injection_kwh",
    "year_end_cost_eur",
)


class _Recorder:
    """A recorder answering from a synthetic hourly series, logging reads.

    Each read records whether ``async_setup_entry`` is on the stack, which
    is what tells a read setup waits on from one a background task makes.
    The executor hop yields first, as the real one does, so a background
    task started inside setup runs as its own task.
    """

    def __init__(self) -> None:
        self.reads: list[dict[str, Any]] = []
        self._series: dict[str, tuple[list[float], list[float]]] = {}
        self.statistics_meta_manager = None

    def series(self, eid: str) -> tuple[list[float], list[float]]:
        if eid not in self._series:
            side, band = _METERS[eid]
            t = dt_util.start_of_local_day(date(2025, 1, 1)).astimezone(UTC)
            end = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            starts: list[float] = []
            sums: list[float] = []
            total = 1000.0
            while t < end:
                loc = dt_util.as_local(t)
                if side == "cons":
                    kwh = 0.35 + 0.1 * (loc.hour % 3)
                else:
                    kwh = 1.2 if 9 <= loc.hour < 17 else 0.0
                if (band == "day") == (loc.weekday() < 5 and 7 <= loc.hour < 22):
                    total += kwh
                starts.append(t.timestamp())
                sums.append(total)
                t += timedelta(hours=1)
            self._series[eid] = (starts, sums)
        return self._series[eid]

    def _log(self, fn: str, ids: set[str] | list[str]) -> None:
        stack = [frame.name for frame in traceback.extract_stack()]
        self.reads.append(
            {
                "fn": fn,
                "ids": sorted(ids),
                "in_setup": "async_setup_entry" in stack,
                "year_end": "_compute_year_end_cost" in stack,
            }
        )

    def statistics_during_period(
        self,
        hass: HomeAssistant,
        start_time: datetime,
        end_time: datetime | None,
        statistic_ids: set[str],
        period: str,
        units: Any,
        types: set[str],
    ) -> dict[str, list[dict[str, Any]]]:
        self._log("statistics", statistic_ids)
        start = start_time.timestamp()
        stop = end_time.timestamp() if end_time is not None else float("inf")
        out: dict[str, list[dict[str, Any]]] = {}
        for eid in statistic_ids:
            if eid not in _METERS:
                # The integration's own sensors (the backfill's probe): held.
                out[eid] = [{"start": start, "end": start + 3600, "mean": 0.3}]
                continue
            starts, sums = self.series(eid)
            idx = [i for i, ts in enumerate(starts) if start <= ts < stop]
            prev = sums[idx[0] - 1] if idx and idx[0] > 0 else 0.0
            rows: list[dict[str, Any]] = []
            if period == "hour":
                rows = [
                    {"start": starts[i], "end": starts[i] + 3600, "sum": sums[i]}
                    for i in idx
                ]
            else:
                days: dict[date, dict[str, Any]] = {}
                for i in idx:
                    local = dt_util.as_local(datetime.fromtimestamp(starts[i], UTC))
                    first = local.replace(hour=0, minute=0, second=0, microsecond=0)
                    days[local.date()] = {"start": first.timestamp(), "sum": sums[i]}
                rows = list(days.values())
            for row in rows:
                row["change"] = row["sum"] - prev
                prev = row["sum"]
            if "sum" not in types:
                for row in rows:
                    row.pop("sum")
            if rows:
                out[eid] = rows
        return out

    def get_significant_states(
        self,
        hass: HomeAssistant,
        start_time: datetime,
        end_time: datetime | None = None,
        entity_ids: list[str] | None = None,
        *_a: Any,
        **_k: Any,
    ) -> dict[str, list[State]]:
        self._log("states", entity_ids or [])
        out: dict[str, list[State]] = {}
        for eid in entity_ids or []:
            if eid in _METERS:
                starts, sums = self.series(eid)
                prior = [
                    s
                    for ts, s in zip(starts, sums, strict=True)
                    if ts <= start_time.timestamp()
                ]
                out[eid] = [State(eid, str(prior[-1] if prior else sums[0]))]
        return out

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        await asyncio.sleep(0)
        if isinstance(func, functools.partial):
            return func.func(*func.args, *args, **func.keywords)
        return func(*args)

    def async_import_statistics(self, *_a: Any, **_k: Any) -> None:
        return None

    def meter_reads(self, *, in_setup: bool | None = None) -> list[dict[str, Any]]:
        return [
            read
            for read in self.reads
            if set(read["ids"]) & set(_METERS)
            and (in_setup is None or read["in_setup"] is in_setup)
        ]


@contextmanager
def _a_frank_entry(
    hass: HomeAssistant, recorder: _Recorder
) -> Iterator[MockConfigEntry]:
    snap = parse_snapshot(
        fixture_text("frank_dynamic_korting_jun.pdf", layout=True),
        "test://frank",
        "frank_dynamic_korting",
        "juni 2026",
    )

    async def _fetch(*_a: Any) -> Any:
        return snap

    async def _probe(*_a: Any) -> str:
        return "probe-key"

    async def _month(*_a: Any) -> None:
        return None

    async def _archive(*_a: Any, **_k: Any) -> None:
        return None

    def _spots(
        start: datetime, end: datetime, quarter_hourly: bool
    ) -> dict[datetime, float]:
        step = timedelta(minutes=15 if quarter_hourly else 60)
        out: dict[datetime, float] = {}
        while start < end:
            out[start] = 0.08
            start += step
        return out

    async def _wrapper(
        _k: Any,
        _s: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> tuple[dict[datetime, float], str]:
        return _spots(start, end, quarter_hourly), "entsoe"

    async def _method(
        _self: Any, start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        return _spots(start, end, quarter_hourly)

    async def _no_fallback(*_a: Any, **_k: Any) -> dict[datetime, float]:
        raise EntsoeError("disabled")

    frank = replace(
        EXTRACTORS["frank"], fetch=_fetch, probe=_probe, fetch_for_month=_month
    )
    _set_meter_states(hass, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frank Energie Dynamisch Korting (Flanders)",
        data={
            "supplier": "frank",
            "contract": "frank_dynamic_korting",
            "region": "flanders",
            "dso": "fluvius_imewo",
            "meter": "dynamic",
            "solar_regime": "injection",
            "api_key": "KEY",
            "day_consumption_kwh": "sensor.cons_day",
            "night_consumption_kwh": "sensor.cons_night",
            "day_injection_kwh": "sensor.inj_day",
            "night_injection_kwh": "sensor.inj_night",
        },
    )
    entry.add_to_hass(hass)
    with ExitStack() as stack:
        for cm in (
            patch.object(snapshot_months, "_archived_card_from_github", _archive),
            patch.dict(EXTRACTORS, {"frank": frank}),
            *_recorder_patches(recorder),
            patch(_SPOTS + ".fetch_day_ahead_or_fallback", _wrapper),
            patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _method),
            patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _no_fallback),
        ):
            stack.enter_context(cm)
        yield entry


def _recorder_patches(recorder: _Recorder) -> tuple[Any, ...]:
    return (
        patch(
            "homeassistant.components.recorder.get_instance",
            lambda _h: recorder,
        ),
        patch(
            "homeassistant.components.recorder.statistics.statistics_during_period",
            recorder.statistics_during_period,
        ),
        patch(
            "homeassistant.components.recorder.history.get_significant_states",
            recorder.get_significant_states,
        ),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            recorder.async_import_statistics,
        ),
    )


def _set_meter_states(hass: HomeAssistant, recorder: _Recorder) -> None:
    for eid in _METERS:
        hass.states.async_set(
            eid,
            str(recorder.series(eid)[1][-1]),
            {
                "device_class": "energy",
                "state_class": "total_increasing",
                "unit_of_measurement": "kWh",
            },
        )


@contextmanager
def _a_fixed_entry(
    hass: HomeAssistant, recorder: _Recorder
) -> Iterator[MockConfigEntry]:
    """A static contract, whose year-end cost is walked every day."""
    snap = make_snapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.12, peak=0.14, offpeak=0.10),
        injection=InjectionRates(current=0.03),
    )

    async def _fetch(*_a: Any) -> Any:
        return snap

    async def _probe(*_a: Any) -> str:
        return "probe-key"

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    eneco = replace(
        EXTRACTORS["eneco"], fetch=_fetch, probe=_probe, fetch_for_month=_none
    )
    _set_meter_states(hass, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Eneco Zon & Wind Vast (Wallonia)",
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "bi",
            "solar_regime": "injection",
            "card_archive": False,
            "day_consumption_kwh": "sensor.cons_day",
            "night_consumption_kwh": "sensor.cons_night",
            "day_injection_kwh": "sensor.inj_day",
            "night_injection_kwh": "sensor.inj_night",
        },
    )
    entry.add_to_hass(hass)
    with ExitStack() as stack:
        for cm in (
            patch.object(snapshot_months, "_archived_card_from_github", _none),
            patch.dict(EXTRACTORS, {"eneco": eneco}),
            *_recorder_patches(recorder),
        ):
            stack.enter_context(cm)
        yield entry


async def _settle(hass: HomeAssistant, freezer: Any) -> None:
    """Run what setup left behind, the refresh setup asked for included.

    A background fill that asks for a refresh while setup's own one runs
    leaves the debouncer's cooldown running, and the request setup makes
    after it lands at the end of that cooldown, so the clock is moved past
    it.
    """
    for _ in range(3):
        async with asyncio.timeout(60):
            await hass.async_block_till_done(wait_background_tasks=True)
        freezer.tick(timedelta(seconds=11))
        async_fire_time_changed(hass)
    async with asyncio.timeout(60):
        await hass.async_block_till_done(wait_background_tasks=True)


def _figures(entry: MockConfigEntry) -> dict[str, Any]:
    data = entry.runtime_data.data
    return {name: getattr(data, name) for name in _FIGURES}


async def test_setup_reads_no_meter_and_the_figures_follow(
    hass: HomeAssistant, freezer: Any, hass_storage: dict[str, Any]
) -> None:
    """Setup reads no meter, the refresh after it reads them and publishes
    what a refresh always did, and a restart serves those figures again
    without a read until its own refresh lands."""
    freezer.move_to("2026-06-20 10:30:00+02:00")
    recorder = _Recorder()
    with _a_frank_entry(hass, recorder) as entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert recorder.meter_reads(in_setup=True) == []
        assert recorder.reads, "the recording stub is not reached"
        # Nothing priced yet: no figure is made up while the reads are due.
        assert all(value is None for value in _figures(entry).values())

        await _settle(hass, freezer)
        # Once per meter: every window the refresh asks for is answered from
        # one hourly read of each register.
        read = recorder.meter_reads(in_setup=False)
        assert sorted(r["ids"][0] for r in read if r["fn"] == "statistics") == sorted(
            _METERS
        )
        assert not entry.runtime_data.meter_reads_pending
        after_setup = _figures(entry)
        assert after_setup["current_year_cost_eur"] is not None
        assert after_setup["rolling_year_consumption_kwh"] is not None

        # What an ordinary hourly refresh publishes at the same instant.
        await entry.runtime_data.async_refresh()
        assert _figures(entry) == after_setup

        # A restart: setup reads nothing again and serves the same figures.
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        recorder.reads.clear()
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert recorder.meter_reads(in_setup=True) == []
        assert entry.runtime_data.meter_reads_pending
        assert _figures(entry) == after_setup
        await _settle(hass, freezer)
        assert _figures(entry) == after_setup

        # Their store goes with the entry.
        assert f"{DOMAIN}_costs_{entry.entry_id}" in hass_storage
        assert await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert f"{DOMAIN}_costs_{entry.entry_id}" not in hass_storage


async def test_the_year_end_walk_reads_no_meter_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The year-end cost walks the year to date and last year's same days,
    both inside the hours the tick read once per meter: served from them, as
    every other figure is, rather than read a second and a third time."""
    freezer.move_to("2026-06-20 10:30:00+02:00")
    recorder = _Recorder()
    with _a_fixed_entry(hass, recorder) as entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass, freezer)
        assert entry.runtime_data.data.year_end_cost_eur is not None
        read = [r for r in recorder.meter_reads() if r["fn"] == "statistics"]
        assert sorted(r["ids"][0] for r in read) == sorted(_METERS)
        # The first tick of a day walks the year end again.
        recorder.reads.clear()
        freezer.move_to("2026-06-21 01:40:00+02:00")
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
        assert entry.runtime_data.data.year_end_cost_eur is not None
        assert recorder.meter_reads()
        assert [r for r in recorder.meter_reads() if r["year_end"]] == []
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_a_held_figure_covers_only_its_own_window(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A restart in a new month serves no month-to-date cost from the last
    one, and one in a new year no year-to-date cost: the sensor publishes
    the window beside the figure, and the old figure would not cover it."""
    freezer.move_to("2026-06-30 22:30:00+02:00")
    recorder = _Recorder()
    with _a_frank_entry(hass, recorder) as entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass, freezer)
        june = _figures(entry)
        assert june["current_month_cost_eur"] is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

        freezer.move_to("2026-07-01 00:30:00+02:00")
        assert await hass.config_entries.async_setup(entry.entry_id)
        held = _figures(entry)
        assert held["current_month_cost_eur"] is None
        assert held["current_year_cost_eur"] == june["current_year_cost_eur"]
        await _settle(hass, freezer)
        assert _figures(entry)["current_month_cost_eur"] is not None
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_costs_priced_before_an_edit_are_held_under_their_settings(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An edit saved while a refresh runs reloads the entry, but the refresh
    finishes first. Its costs were held under a digest of the settings as
    they stood once it had priced them, the new ones, so a restart served
    the old wiring's figures as if priced under the new. They carry the
    settings the refresh started under."""
    from custom_components.be_electricity_prices.coordinator_costs import (
        _CostsMixin,
    )
    from custom_components.be_electricity_prices.coordinator_persist import (
        settings_digest,
    )

    freezer.move_to("2026-06-20 10:30:00+02:00")
    recorder = _Recorder()
    with _a_frank_entry(hass, recorder) as entry:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await _settle(hass, freezer)
        coord = entry.runtime_data
        before = settings_digest(entry)
        priced = _CostsMixin._tick_costs

        async def _priced_then_edited(self: Any, *args: Any) -> Any:
            costs = await priced(self, *args)
            # The user rewires the night register while the refresh runs.
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, "night_consumption_kwh": "sensor.new"}
            )
            return costs

        with patch.object(_CostsMixin, "_tick_costs", _priced_then_edited):
            await coord.async_refresh()
        assert settings_digest(entry) != before
        assert coord._held_costs is not None
        assert coord._held_costs["inputs"] == before
        await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def test_the_costs_alone_moving_leaves_the_entry_blob_unwritten(
    hass: HomeAssistant,
) -> None:
    """The costs move every tick, and the entry's blob is written only when
    something in it moved: an hourly entry used to rewrite 342 KB an hour
    for one changed timestamp. So the costs are kept in a store of their
    own, written when they move, and the blob is not."""
    from unittest.mock import AsyncMock

    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    blob = AsyncMock()
    costs = AsyncMock()
    coord._store.async_save = blob  # type: ignore[method-assign]
    coord._costs_store.async_save = costs  # type: ignore[method-assign]
    coord._held_costs = {"current_year_cost": 120.0}
    await coord._save_persistent()
    coord._held_costs = {"current_year_cost": 120.4}
    await coord._save_persistent()
    await coord._save_persistent()
    assert blob.await_count == 1
    assert costs.await_count == 2


async def test_the_first_year_walk_after_setup_bills_each_month_on_its_own_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Setup defers its meter reads and publishes the held costs, so the first
    walk of the year runs in the background refresh that follows, which
    nothing waits on. It fetches the month cards there rather than billing
    every month it holds no card for on the current one first."""
    from custom_components.be_electricity_prices import coordinator_costs
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    freezer.move_to("2026-06-20 10:30:00+02:00")
    recorder = _Recorder()
    current = make_snapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.20, peak=0.22, offpeak=0.18),
        injection=InjectionRates(current=0.03),
    )
    past = make_snapshot(
        supplier="eneco",
        contract="power_fix",
        energy=FixedRates(single=0.12, peak=0.14, offpeak=0.10),
        injection=InjectionRates(current=0.03),
    )

    async def _fetch(*_args: Any) -> Any:
        return current

    async def _probe(*_args: Any) -> str:
        return "k"

    async def _month(*_args: Any) -> Any:
        return past

    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    eneco = replace(
        EXTRACTORS["eneco"], fetch=_fetch, probe=_probe, fetch_for_month=_month
    )
    _set_meter_states(hass, recorder)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="t",
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "bi",
            "solar_regime": "none",
            "card_archive": False,
            "day_consumption_kwh": "sensor.cons_day",
            "night_consumption_kwh": "sensor.cons_night",
        },
    )
    entry.add_to_hass(hass)
    cached_only: list[bool] = []
    real = coordinator_costs._compute_current_year_cost

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        cached_only.append(bool(kwargs.get("cached_only")))
        return await real(*args, **kwargs)

    with ExitStack() as stack:
        for cm in (
            patch.object(snapshot_months, "_archived_card_from_github", _none),
            patch.dict(EXTRACTORS, {"eneco": eneco}),
            patch.object(coordinator_costs, "_compute_current_year_cost", _spy),
            *_recorder_patches(recorder),
        ):
            stack.enter_context(cm)
        assert await hass.config_entries.async_setup(entry.entry_id)
        coord = entry.runtime_data
        assert isinstance(coord, BePricesCoordinator)
        await _settle(hass, freezer)
        assert cached_only
        assert not any(cached_only)
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
