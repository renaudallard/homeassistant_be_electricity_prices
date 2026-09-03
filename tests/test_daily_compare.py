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

"""Tests for the opt-in daily supplier ranking and the sensor it feeds."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.compare_quote import (
    DailyCompare,
    RankedRow,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.sensor import (
    PotentialSavingSensor,
    async_setup_entry,
)
from tests import make_entry

_RAN_AT = datetime(2026, 8, 30, 3, 17, tzinfo=dt_util.UTC)


def _result(**kw: Any) -> DailyCompare:
    rows = kw.pop(
        "rows",
        (
            RankedRow("Mega Online Fixed", 1102.75),
            RankedRow("Eneco Zon & Wind Flex", 1272.75, is_own=True),
            RankedRow("Luminus Comfy Fixed", 1310.20),
            RankedRow("Ecofix Fix 1 jaar", None, None, "card not readable"),
        ),
    )
    return DailyCompare(
        rows=rows,
        own=kw.pop("own", 1272.75),
        priced=kw.pop("priced", 2),
        total=kw.pop("total", 3),
        ran_at=kw.pop("ran_at", _RAN_AT),
    )


def _coord(
    entry: MockConfigEntry, result: DailyCompare | None = None
) -> SimpleNamespace:
    return SimpleNamespace(data=CoordinatorData(), entry=entry, daily_compare=result)


def test_saving_is_measured_against_the_household_not_the_field() -> None:
    """The saving is own minus cheapest ALTERNATIVE. Ranking the own row as
    the cheapest and subtracting it from itself would report zero on exactly
    the household that has nothing to gain, which is indistinguishable from
    a sweep that failed."""
    result = _result()
    assert result.cheapest is not None
    assert result.cheapest.label == "Mega Online Fixed"
    assert result.saving == 1272.75 - 1102.75


def test_own_row_is_never_offered_as_the_cheapest() -> None:
    """A household already on the best contract in its region: the cheapest
    ALTERNATIVE is the runner-up, and the saving goes negative to say so."""
    result = _result(
        rows=(
            RankedRow("Eneco Zon & Wind Flex", 900.00, is_own=True),
            RankedRow("Mega Online Fixed", 1102.75),
        ),
        own=900.00,
    )
    assert result.cheapest is not None
    assert result.cheapest.label == "Mega Online Fixed"
    assert result.saving == 900.00 - 1102.75
    assert result.saving < 0


def test_no_own_row_reports_unknown_rather_than_zero() -> None:
    """A cold entry whose own card has not resolved has no baseline. Zero
    would read as "nothing to save" when the truth is "not known yet"."""
    result = _result(
        rows=(RankedRow("Mega Online Fixed", 1102.75),),
        own=None,
    )
    assert result.cheapest is not None
    assert result.saving is None


def test_nothing_priced_reports_unknown() -> None:
    result = _result(rows=(RankedRow("Ecofix", None, None, "unreachable"),), own=1000.0)
    assert result.cheapest is None
    assert result.saving is None


def test_sensor_publishes_the_saving_and_the_ranking() -> None:
    entry = make_entry(daily_compare=True)
    sensor = PotentialSavingSensor(_coord(entry, _result()))  # type: ignore[arg-type]
    assert sensor.native_value == 170.0
    attrs = sensor.extra_state_attributes
    assert attrs["cheapest"] == "Mega Online Fixed"
    assert attrs["cheapest_annual_eur"] == 1102.75
    assert attrs["own_annual_eur"] == 1272.75
    assert attrs["priced"] == 2
    assert attrs["total"] == 3
    assert attrs["last_run"] == _RAN_AT.isoformat()

    # Cheapest first, and a row that could not be priced sorts last with its
    # reason rather than being dropped: a missing row reads as "not
    # competitive", which is the one thing it does not mean.
    labels = [r["label"] for r in attrs["ranking"]]
    assert labels[0] == "Mega Online Fixed"
    assert labels[-1] == "Ecofix Fix 1 jaar"
    assert attrs["ranking"][-1]["status"] == "card not readable"
    assert attrs["ranking"][-1]["annual_eur"] is None
    # The own row is flagged so a dashboard can pick it out.
    assert [r["label"] for r in attrs["ranking"] if r["is_own"]] == [
        "Eneco Zon & Wind Flex"
    ]


def test_the_ranking_carries_the_year_to_date_and_is_not_recorded() -> None:
    """The nightly sweep fills a year-to-date figure per row, and it has to
    reach somewhere a user can read it.

    It used to exist only on the options page while that page was open: the
    scheduled sweep never computed it, the dialog that did never stored what
    it computed, and the sensor dropped the field even when it was set. All
    three had to change together for the number to be visible at all.
    """
    rows = (
        RankedRow("Mega Online Fixed", 1102.75, ytd=612.40),
        RankedRow("Eneco Zon & Wind Flex", 1272.75, ytd=708.11, is_own=True),
        # No archive deep enough to answer honestly: the column stays absent
        # rather than carrying a figure built on different months.
        RankedRow("Luminus Comfy Fixed", 1310.20),
    )
    entry = make_entry(daily_compare=True)
    sensor = PotentialSavingSensor(_coord(entry, _result(rows=rows)))  # type: ignore[arg-type]
    ranking = sensor.extra_state_attributes["ranking"]
    assert [r.get("ytd_eur") for r in ranking] == [612.40, 708.11, None]

    # And the table stays out of the recorder. It is replaced wholesale every
    # night, so storing a snapshot of every row daily forever answers nothing,
    # and excluding it is also what keeps the small keys beside it safe from
    # the 16 KB cap, which an over-cap state would cost their history entirely.
    assert "ranking" in PotentialSavingSensor._unrecorded_attributes
    assert "cheapest_annual_eur" not in PotentialSavingSensor._unrecorded_attributes


def test_sensor_reads_unknown_before_the_first_sweep() -> None:
    entry = make_entry(daily_compare=True)
    sensor = PotentialSavingSensor(_coord(entry, None))  # type: ignore[arg-type]
    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_sensor_survives_a_failed_price_fetch() -> None:
    """The ranking comes from the nightly sweep, not the hourly price fetch,
    so an hour where the supplier was unreachable must not hide last night's
    answer behind "unavailable"."""
    entry = make_entry(daily_compare=True)
    coord = _coord(entry, _result())
    coord.last_update_success = False
    sensor = PotentialSavingSensor(coord)  # type: ignore[arg-type]
    assert sensor.available is True
    assert sensor.native_value == 170.0


def test_metadata_matches_conventions() -> None:
    entry = make_entry(daily_compare=True)
    sensor = PotentialSavingSensor(_coord(entry))  # type: ignore[arg-type]
    assert sensor.device_class == SensorDeviceClass.MONETARY
    assert sensor.translation_key == "potential_saving"
    assert sensor.unique_id == f"{entry.entry_id}_potential_saving"
    assert sensor.device_info is not None
    assert (DOMAIN, entry.entry_id) in sensor.device_info["identifiers"]
    # No state_class: a standing comparison is not metered, and a monthly
    # mean of it would be meaningless in long-term statistics.
    assert sensor.state_class is None


async def test_sensor_appears_only_when_the_option_is_on() -> None:
    on = make_entry(daily_compare=True)
    on.runtime_data = _coord(on)
    added: list[Any] = []
    await async_setup_entry(
        None,  # type: ignore[arg-type]
        on,
        lambda entities: added.extend(entities),  # type: ignore[arg-type]
    )
    assert sum(isinstance(e, PotentialSavingSensor) for e in added) == 1

    # Default off: an entry that never opted in gets no sensor, and no daily
    # fetching either.
    off = make_entry()
    off.runtime_data = _coord(off)
    added_off: list[Any] = []
    await async_setup_entry(
        None,  # type: ignore[arg-type]
        off,
        lambda entities: added_off.extend(entities),  # type: ignore[arg-type]
    )
    assert not any(isinstance(e, PotentialSavingSensor) for e in added_off)


async def test_dialog_shows_the_stored_ranking_without_sweeping(
    hass: Any,
) -> None:
    """The point of the daily option: opening the page answers immediately
    instead of moving the two-minute wait somewhere else. It must land on the
    result step directly, never on a progress step."""
    from homeassistant import data_entry_flow

    from tests.test_options_flow import _make_entry, _real_coordinator, _stub_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    coord.daily_compare = _result()
    entry.runtime_data = coord

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare_all"}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "compare_all_result"
    ranking = result["description_placeholders"]["ranking"]
    # The stored rows, and the table dates itself so a reader can tell a
    # night-old answer from one priced just now.
    assert "Mega Online Fixed" in ranking
    assert "Ranked " in ranking
    # A stored ranking priced the whole cell, so nothing is reported pending.
    assert "not priced yet" not in ranking


async def test_refresh_box_reprices_instead_of_serving_the_stored_rows(
    hass: Any,
) -> None:
    """Ticking refresh must actually sweep, not re-render what was stored."""
    from dataclasses import replace
    from unittest.mock import AsyncMock, patch

    from homeassistant import data_entry_flow

    from custom_components.be_electricity_prices.providers import EXTRACTORS
    from tests.test_options_flow import _make_entry, _real_coordinator, _stub_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    coord.daily_compare = _result()
    entry.runtime_data = coord

    patched = {
        sid: replace(
            ext,
            fetch=AsyncMock(return_value=_stub_snapshot(sid, "x", 0.16)),
            probe=None,
        )
        for sid, ext in EXTRACTORS.items()
    }
    with patch.dict(EXTRACTORS, patched):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "compare_all"}
        )
        assert result["step_id"] == "compare_all_result"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"refresh": True}
        )
        # It really swept: a progress step is the proof, since the stored
        # path never reaches one.
        saw_progress = result["type"] is data_entry_flow.FlowResultType.SHOW_PROGRESS
        for _ in range(400):
            if result["type"] is not data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
        assert saw_progress
        assert result["step_id"] == "compare_all_result"
        # Freshly priced rows, so the stored run's timestamp is gone.
        assert "Ranked " not in result["description_placeholders"]["ranking"]


async def test_year_to_date_works_on_a_stored_ranking(hass: Any) -> None:
    """The year-to-date box is offered on a stored ranking, so it has to work
    there. The progress step used to be the only place the household was
    resolved, and a stored ranking skips it: ticking the box raised
    KeyError('household') on the one page whose whole job is to be slow but
    correct."""
    from homeassistant import data_entry_flow

    from tests.test_options_flow import _make_entry, _real_coordinator, _stub_snapshot

    entry = _make_entry()
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    coord.daily_compare = _result()
    entry.runtime_data = coord

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "compare_all"}
    )
    assert result["step_id"] == "compare_all_result"
    # The box is on offer, so it must not blow up when ticked.
    assert "with_ytd" in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"with_ytd": True}
    )
    assert result["type"] is data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "compare_all_result"
    # Having run, the pass does not offer itself again.
    assert "with_ytd" not in result["data_schema"].schema


async def test_the_sweep_persists_its_ranking_at_once(
    hass: HomeAssistant,
) -> None:
    """The sweep runs once a day and takes minutes. Left for the next hourly
    tick to write, a restart inside that window threw away a ranking that had
    just been built."""
    from custom_components.be_electricity_prices.compare_flow import (
        async_run_daily_compare,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    saved: list[str] = []

    class _Coord:
        daily_compare: Any = None

        def async_update_listeners(self) -> None:
            saved.append("published")

        async def _save_persistent(self) -> None:
            saved.append("persisted")

    coord = _Coord()
    ranking = _result(
        rows=(RankedRow(label="Mine", annual=1400.0, is_own=True),), own=1400.0
    )
    with patch(
        "custom_components.be_electricity_prices.compare_flow._SweepEngine"
    ) as engine:
        engine.return_value.run_full_sweep = AsyncMock(return_value=ranking)
        await async_run_daily_compare(hass, entry, coord)

    assert coord.daily_compare is ranking
    assert saved == ["published", "persisted"]


async def test_the_scheduled_sweep_fills_the_year_to_date_column(
    hass: HomeAssistant,
) -> None:
    """The column was reachable only by clicking through the options page.

    The scheduled run is the right place for a pass that replays a year per
    candidate: it is the one with all night and nobody watching a progress
    bar, which is the same argument run_full_sweep's own docstring makes for
    doing the expensive thing there.
    """
    from custom_components.be_electricity_prices.compare_flow import _SweepEngine

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    engine = _SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    priced = RankedRow(label="Mega Online Fixed", annual=1102.75)

    async def _fake_fill(_self: Any, sweep: Any) -> list[RankedRow]:
        # The pass sees the rows the sweep just priced, and enriches them.
        assert [r.label for r in sweep["rows"]] == ["Mega Online Fixed"]
        return [
            RankedRow(label=r.label, annual=r.annual, ytd=612.40) for r in sweep["rows"]
        ]

    with (
        patch.object(
            _SweepEngine,
            "build_sweep",
            return_value={"candidates": [("mega", "x")], "rows": []},
        ),
        patch.object(_SweepEngine, "_resolve_household", AsyncMock(return_value=None)),
        patch.object(_SweepEngine, "_sweep_own_row", AsyncMock(return_value=None)),
        patch.object(_SweepEngine, "_sweep_one", AsyncMock(return_value=priced)),
        patch.object(_SweepEngine, "fill_ytd_column", _fake_fill),
    ):
        result = await engine.run_full_sweep(_coord(entry))
    assert not isinstance(result, str)
    assert [r.ytd for r in result.rows] == [612.40]


async def test_a_failed_year_to_date_pass_keeps_the_annual_ranking(
    hass: HomeAssistant,
) -> None:
    """The column is an extra, not the ranking. A pass that raises must leave
    the annual figures standing rather than cost the whole night's sweep,
    which async_run_daily_compare would otherwise discard wholesale."""
    from custom_components.be_electricity_prices.compare_flow import _SweepEngine

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    engine = _SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    priced = RankedRow(label="Mega Online Fixed", annual=1102.75)

    async def _boom(_self: Any, sweep: Any) -> list[RankedRow]:
        raise RuntimeError("archive went away mid-pass")

    with (
        patch.object(
            _SweepEngine,
            "build_sweep",
            return_value={"candidates": [("mega", "x")], "rows": []},
        ),
        patch.object(_SweepEngine, "_resolve_household", AsyncMock(return_value=None)),
        patch.object(_SweepEngine, "_sweep_own_row", AsyncMock(return_value=None)),
        patch.object(_SweepEngine, "_sweep_one", AsyncMock(return_value=priced)),
        patch.object(_SweepEngine, "fill_ytd_column", _boom),
    ):
        result = await engine.run_full_sweep(_coord(entry))
    assert not isinstance(result, str)
    assert [r.annual for r in result.rows] == [1102.75]
    assert [r.ytd for r in result.rows] == [None]


async def test_a_failed_save_does_not_undo_the_published_ranking(
    hass: HomeAssistant,
) -> None:
    """The ranking is live in this session either way and the next tick writes
    it again, so a Store that will not write must not cost the run its
    result."""
    from custom_components.be_electricity_prices.compare_flow import (
        async_run_daily_compare,
    )

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    class _Coord:
        daily_compare: Any = None

        def async_update_listeners(self) -> None:
            return None

        async def _save_persistent(self) -> None:
            raise OSError("disk full")

    coord = _Coord()
    ranking = _result(
        rows=(RankedRow(label="Mine", annual=1400.0, is_own=True),), own=1400.0
    )
    with patch(
        "custom_components.be_electricity_prices.compare_flow._SweepEngine"
    ) as engine:
        engine.return_value.run_full_sweep = AsyncMock(return_value=ranking)
        await async_run_daily_compare(hass, entry, coord)

    assert coord.daily_compare is ranking
