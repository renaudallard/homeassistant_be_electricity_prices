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
"""An analog meter Fluvius replaced during the year.

The analog answer is one per entry, so unticking it once the digital meter is
in place billed every day of the year on the digital one. The day it was
replaced is asked then, and every day before it stays analog: the flat 2,5 kW
capacity charge, no ceiling and the analog kWh rate.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    energy_meters,
    fluvius,
    ytd_cost,
    ytd_energy,
)
from custom_components.be_electricity_prices.cohort import _CohortLegs
from custom_components.be_electricity_prices.providers._rates import (
    FixedRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _resolve_snapshot,
    analog_meter,
)
from tests import make_entry, make_snapshot, make_stub_extractor
from tests.test_capacity_meters import _saved, _steps
from tests.test_config_flow_energy_defaults import _shown

# Fluvius Antwerpen's 2026 analog surcharge, as in test_capacity_meters.
_ANTWERPEN_2026 = 0.0257935
_KWH_PER_HOUR = 0.4


def _engie_basic_online(energy: Any = None) -> Any:
    """Engie Basic Online's October 2026 card for Fluvius Antwerpen, the
    figures the year-to-date bills: energy, standing charge, network,
    capacity rate, data management, excise and green certificates."""
    return make_snapshot(
        supplier="engie",
        contract="engie_basic_online",
        energy=energy or FixedRates(single=0.20982, yearly_fixed_fee=20.0),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.0535329,
                distribution_exclusive_night=0.0481301,
                transport=0.0,
                capacity_eur_per_kw_year=52.3679,
                data_management_per_year=18.92,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.04876,
            energy_contribution=0.0,
            flanders_renewables=0.01582,
        ),
    )


def _entry(**extra: Any) -> MockConfigEntry:
    base: dict[str, Any] = {
        "supplier": "engie",
        "contract": "engie_basic_online",
        "region": "flanders",
        "dso": "fluvius_antwerpen",
        "meter": "mono",
        "solar_regime": "none",
        "consumption_kwh": "sensor.cons_total",
    }
    return make_entry(**{**base, **extra})


async def _ytd(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    peak: float,
    *,
    start: date | None = None,
    end: date | None = None,
    energy: Any = None,
) -> tuple[float, dict[str, float]]:
    """The year to 24 October 2026 at 0,4 kWh an hour, or the part of it from
    ``start`` to ``end``, on the card resolved for ``entry``."""
    card = _engie_basic_online(energy)
    resolved = _resolve_snapshot(entry, card, annual_kwh=3504.0)
    first = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    stop = dt_util.start_of_local_day(date(2026, 10, 24)).astimezone(UTC)
    hours: dict[datetime, float] = {}
    when = first
    while when < stop:
        hours[when] = _KWH_PER_HOUR
        when += timedelta(hours=1)
    days: dict[date, float] = {}
    for hour, kwh in hours.items():
        day = dt_util.as_local(hour).date()
        days[day] = days.get(day, 0.0) + kwh

    async def fake_hourly(_h: Any, eid: str, s: date, e: date) -> Any:
        if eid != "sensor.cons_total":
            return {}
        return {h: v for h, v in hours.items() if s <= dt_util.as_local(h).date() <= e}

    async def fake_daily(_h: Any, eid: str, s: date, e: date) -> Any:
        if eid != "sensor.cons_total":
            return {}
        return {d: v for d, v in days.items() if s <= d <= e}

    breakdown: dict[str, float] = {}
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=fake_hourly),
        patch.object(energy_meters, "_recorder_daily_kwh", new=fake_daily),
        patch.object(ytd_energy, "_top_up_today_hourly", new=AsyncMock()),
        patch.object(
            ytd_cost, "_cohort_legs", AsyncMock(return_value=_CohortLegs(None, None))
        ),
        patch(
            "custom_components.be_electricity_prices.cohort._cohort_legs",
            AsyncMock(return_value=_CohortLegs(None, None)),
        ),
    ):
        total = await ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            resolved,
            entry,
            billed_peak_kw=peak,
            breakdown=breakdown,
            window_start_override=start,
            window_end=end,
            snapshot_raw=card,
        )
    assert total is not None
    return total, breakdown


async def test_a_meter_replaced_on_1_july_bills_the_half_year_before_as_analog(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Engie Basic Online in Antwerpen, 0,4 kWh an hour, a 5 kW mean on the
    digital meter placed on 1 July. Fluvius billed January to June on the
    analog meter. Unticked, the whole year was billed digital, 1176,66 EUR:
    17,96 more than the bill, the 6 months on 5 kW rather than 2,5
    (65,46) less the analog rate on their 1.737,2 kWh (47,50)."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    replaced = _entry(analog_meter=False, analog_meter_replaced="2026-07-01")
    total, breakdown = await _ytd(hass, replaced, 5.0)
    assert total == pytest.approx(701.94 + 456.76, abs=0.01)
    assert total == pytest.approx(1158.70, abs=0.01)
    # 2,5 kW on January to June, 5 kW on July to the 24th of October.
    rate = 52.3679 / 12.0
    assert breakdown["capacity_ytd_eur"] == pytest.approx(
        2.5 * rate * 6 + 5.0 * rate * (3 + 24 / 31)
    )
    unticked, _ = await _ytd(hass, _entry(analog_meter=False), 5.0)
    assert unticked == pytest.approx(1176.66, abs=0.01)
    surcharge = _ANTWERPEN_2026 * 1.06 * (181 * 24 - 1) * _KWH_PER_HOUR
    assert unticked - total == pytest.approx(2.5 * rate * 6 - surcharge)


@pytest.mark.parametrize(
    "energy",
    [
        FixedRates(single=0.20982, yearly_fixed_fee=20.0),
        # Billed hour by hour, which the fixed card is not.
        SpotMonthlyRates(factor=1.2391, base=0.0160, yearly_fixed_fee=20.0),
    ],
    ids=["per_day", "per_hour"],
)
async def test_a_month_the_meter_was_replaced_in_is_split_per_day(
    hass: HomeAssistant, freezer: Any, energy: Any
) -> None:
    """Replaced on 15 July: the 14 days before it are analog, the 17 from it
    digital, as the two windows billed apart on each meter give them."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    total, _ = await _ytd(
        hass,
        _entry(analog_meter=False, analog_meter_replaced="2026-07-15"),
        5.0,
        energy=energy,
    )
    before, _ = await _ytd(
        hass, _entry(analog_meter=True), 2.5, end=date(2026, 7, 14), energy=energy
    )
    after, _ = await _ytd(
        hass, _entry(analog_meter=False), 5.0, start=date(2026, 7, 15), energy=energy
    )
    assert total == pytest.approx(before + after, abs=1e-6)
    unticked, _ = await _ytd(hass, _entry(analog_meter=False), 5.0, energy=energy)
    assert total < unticked - 10.0


def test_a_quoted_card_is_billed_the_analog_days_too() -> None:
    """The comparison page quotes another card through a proxy carrying the
    entry's data, the day included, so its days before it are analog."""
    from custom_components.be_electricity_prices.compare_inputs import _quote_entry
    from custom_components.be_electricity_prices.snapshot_resolve import (
        meter_of_day,
    )

    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    entry = _entry(analog_meter=False, analog_meter_replaced="2026-07-15")
    proxy = _quote_entry(entry, "none", meter="bi")
    card = _resolve_snapshot(
        proxy, _engie_basic_online(), delivery_month=date(2026, 7, 1)
    )
    network = card.dsos["fluvius_antwerpen"].distribution_single
    before = meter_of_day(proxy, card, date(2026, 7, 14))
    assert before.dsos["fluvius_antwerpen"].distribution_single == pytest.approx(
        network + _ANTWERPEN_2026 * 1.06
    )
    assert meter_of_day(proxy, card, date(2026, 7, 15)) is card


def test_an_entry_without_the_day_is_billed_as_before() -> None:
    """Ticked or not, a day is on the meter the box says, and ticking it
    again leaves a stored day inert."""
    day = date(2026, 3, 1)
    assert analog_meter(_entry(analog_meter=True).data, day)
    assert not analog_meter(_entry(analog_meter=False).data, day)
    ticked = _entry(analog_meter=True, analog_meter_replaced="2026-07-01")
    assert analog_meter(ticked.data, date(2026, 8, 1))
    replaced = _entry(analog_meter=False, analog_meter_replaced="2026-07-01")
    assert analog_meter(replaced.data, date(2026, 6, 30))
    assert not analog_meter(replaced.data, date(2026, 7, 1))
    assert not analog_meter(replaced.data)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_unticking_the_box_asks_the_day_the_meter_was_replaced(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Never in the future, and ticking the box again drops the day. Shown
    again on every edit while a day is held, so a wrong one can be changed,
    and left blank for a box ticked by mistake, which bills the year as before
    the day was asked and is kept as the answer, so it is not asked again."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    analog = _entry(analog_meter=True)
    analog.add_to_hass(hass)
    opts = hass.config_entries.options
    result = await opts.async_init(analog.entry_id)
    result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
    while result["step_id"] != "meter":
        result = await opts.async_configure(result["flow_id"], _shown(result))
    result = await opts.async_configure(
        result["flow_id"], {**_shown(result), "analog_meter": False}
    )
    assert result["step_id"] == "analog_replaced"
    # Nothing suggested: accepted as shown, a box ticked by mistake bills
    # every earlier day of the year analog, 28,60 EUR under the bill here.
    assert "analog_meter_replaced" not in _shown(result)
    result = await opts.async_configure(
        result["flow_id"], {"analog_meter_replaced": "2026-11-02"}
    )
    assert result["step_id"] == "analog_replaced"
    assert result["errors"] == {"analog_meter_replaced": "analog_replaced_in_future"}
    opts.async_abort(result["flow_id"])

    mistaken = _entry(analog_meter=True, dso="fluvius_west")
    mistaken.add_to_hass(hass)
    await _walked(hass, mistaken, {"meter": {"analog_meter": False}})
    assert mistaken.data["analog_meter"] is False
    assert mistaken.data["analog_meter_replaced"] is None

    data = await _saved(
        hass,
        analog,
        {
            "meter": {"analog_meter": False},
            "analog_replaced": {"analog_meter_replaced": "2026-07-01"},
        },
    )
    assert data["analog_meter"] is False
    assert data["analog_meter_replaced"] == "2026-07-01"
    # Edited again with the box left unticked: the day held, shown to change.
    steps = await _walked(
        hass, analog, {"analog_replaced": {"analog_meter_replaced": "2026-07-15"}}
    )
    assert "analog_replaced" in steps
    assert analog.data["analog_meter_replaced"] == "2026-07-15"
    await _walked(hass, analog, {})
    assert analog.data["analog_meter_replaced"] == "2026-07-15"
    data = await _saved(hass, analog, {"meter": {"analog_meter": True}})
    assert "analog_meter_replaced" not in data

    # Ticked by mistake: unticked with the day left blank, no day is analog.
    await _walked(
        hass,
        analog,
        {
            "meter": {"analog_meter": False},
            "analog_replaced": {"analog_meter_replaced": None},
        },
    )
    assert analog.data["analog_meter"] is False
    assert analog.data["analog_meter_replaced"] is None
    assert not analog_meter(analog.data, date(2026, 1, 1))
    assert "analog_replaced" not in await _steps(hass, analog, {})


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_day_survives_a_restart_and_a_supplier_switch(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The day is entry data: a restart reads it back from the stored entry,
    a switch keeps it in the copy of the contract left, and a copy kept while
    the box was still ticked is analog only until the day."""
    from custom_components.be_electricity_prices.compare_household import (
        _meters_rewired,
    )
    from custom_components.be_electricity_prices.contract_periods import (
        previous_periods,
    )
    from custom_components.be_electricity_prices.flow_switch import _record_switch

    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    entry = _entry(analog_meter=True)
    entry.add_to_hass(hass)
    await _saved(
        hass,
        entry,
        {
            "meter": {"analog_meter": False},
            "analog_replaced": {"analog_meter_replaced": "2026-07-01"},
        },
    )
    stored = hass.config_entries.async_get_entry(entry.entry_id)
    assert stored is not None
    restarted = MockConfigEntry(
        domain=stored.domain, data=dict(stored.data), entry_id="restarted"
    )
    total, _ = await _ytd(hass, restarted, 5.0)
    assert total == pytest.approx(1158.70, abs=0.01)

    switched = _record_switch(stored.data, date(2026, 9, 1))
    periods = previous_periods(switched, date(2026, 1, 1), date(2026, 10, 24))
    assert [
        analog_meter(periods[0].data, d) for d in (date(2026, 6, 30), date(2026, 7, 1))
    ] == [True, False]
    # Quoted on the entry's data, which holds the same day: nothing to leave out.
    rewired = MockConfigEntry(domain=stored.domain, data=switched)
    assert _meters_rewired(rewired, date(2026, 10, 24)) is False

    # A switch recorded while the box was still ticked, after the meter was
    # replaced: the copy is the same meter, analog until the day only.
    analog = {**_entry().data, "analog_meter": True}
    later = _entry(
        analog_meter=False,
        analog_meter_replaced="2026-07-01",
        previous_contracts=[{"until": "2026-08-01", "data": analog}],
    )
    held = previous_periods(later.data, date(2026, 1, 1), date(2026, 10, 24))[0]
    assert analog_meter(held.data, date(2026, 6, 30))
    assert not analog_meter(held.data, date(2026, 7, 1))
    assert _meters_rewired(later, date(2026, 10, 24)) is False
    # One that answered digital keeps its answer, which the quote cannot replay.
    digital = {**_entry().data, "analog_meter": False}
    contradicted = _entry(
        analog_meter=False,
        analog_meter_replaced="2026-07-01",
        previous_contracts=[{"until": "2026-05-01", "data": digital}],
    )
    assert _meters_rewired(contradicted, date(2026, 10, 24)) is True


def test_removing_a_switch_keeps_a_day_entered_after_it() -> None:
    """Asked once of an entry that never answered it, the day often comes
    after a switch recorded on an entry stored with the box unticked, whose
    copy holds that answer and no day. Removing the switch put the copy back
    whole, and the months before the day billed digital again until the next
    edit. The day is the connection's, so it is kept, and so is a blank
    answer; a copy holding an answer of its own keeps that one, and one
    ticked analog is put back as it was."""
    from custom_components.be_electricity_prices.flow_switch import (
        _record_switch,
        _remove_last_switch,
    )

    unticked = {**_entry(analog_meter=False).data}
    data = {
        **_record_switch(unticked, date(2026, 6, 1)),
        "supplier": "luminus",
        "contract": "luminus_comfy",
        "analog_meter_replaced": "2026-03-01",
    }
    restored = _remove_last_switch(data)
    assert restored == {**unticked, "analog_meter_replaced": "2026-03-01"}
    assert analog_meter(restored, date(2026, 2, 10))
    assert not analog_meter(restored, date(2026, 3, 1))
    blank = _remove_last_switch({**data, "analog_meter_replaced": None})
    assert blank == {**unticked, "analog_meter_replaced": None}

    answered = {**unticked, "analog_meter_replaced": None}
    later = {
        **_record_switch(answered, date(2026, 6, 1)),
        "analog_meter_replaced": "2026-03-01",
    }
    assert _remove_last_switch(later) == answered
    ticked = {**_entry(analog_meter=True).data}
    cleared = {
        **_record_switch(ticked, date(2026, 6, 1)),
        "analog_meter": False,
        "analog_meter_replaced": None,
    }
    assert _remove_last_switch(cleared) == ticked


async def test_the_backfill_bills_the_days_before_the_replacement_as_analog(
    hass: HomeAssistant,
) -> None:
    """The backfilled series accrues per hour what the live walk bills per
    day, so a meter replaced on 15 January meets it at the end of the month:
    the analog kWh rate and the minimum peak on the first 14 days."""
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from homeassistant.helpers import entity_registry as er

    from custom_components.be_electricity_prices import backfill as bf
    from custom_components.be_electricity_prices.const import DOMAIN

    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    card = _engie_basic_online()
    entry = _entry(analog_meter=False, analog_meter_replaced="2026-01-15")
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_current_year_cost",
        suggested_object_id="replaced_current_year_cost",
        config_entry=entry,
    )
    snap = _resolve_snapshot(entry, card, annual_kwh=3504.0)
    start = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    stop = dt_util.start_of_local_day(date(2026, 2, 1)).astimezone(UTC)
    hours: list[datetime] = []
    when = start
    while when < stop:
        hours.append(when)
        when += timedelta(hours=1)
    per_hour = dict.fromkeys(hours, _KWH_PER_HOUR)
    per_day: dict[date, float] = {}
    for hour in hours:
        day = dt_util.as_local(hour).date()
        per_day[day] = per_day.get(day, 0.0) + _KWH_PER_HOUR
    coordinator = SimpleNamespace(
        hass=hass,
        _snapshot=snap,
        _snapshot_raw=card,
        _session=None,
        _historical_spots={},
        _historical_spot_quarters={},
        _spp_weights={},
        _rlp_weights={},
        _ensure_historical_spots=AsyncMock(),
        _ensure_spp_weights=AsyncMock(),
        _ensure_rlp_weights=AsyncMock(),
        _billed_peak_kw=lambda: 5.0,
    )
    entry.runtime_data = coordinator  # type: ignore[attr-defined]

    async def fake_hourly(_h: Any, eid: str, _s: Any, _e: Any) -> Any:
        return dict(per_hour) if eid == "sensor.cons_total" else {}

    async def fake_daily(_h: Any, eid: str, _s: Any, _e: Any) -> Any:
        return dict(per_day) if eid == "sensor.cons_total" else {}

    captured: list[dict[str, Any]] = []

    def fake_import(_h: Any, _meta: Any, stats: Any) -> None:
        captured.extend(stats)

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    legs = AsyncMock(return_value=_CohortLegs(None, None))
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=fake_hourly),
        patch.object(energy_meters, "_recorder_daily_kwh", new=fake_daily),
        patch.object(ytd_energy, "_top_up_today_hourly", new=AsyncMock()),
        patch.object(ytd_cost, "_cohort_legs", legs),
        patch("custom_components.be_electricity_prices.cohort._cohort_legs", legs),
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=fake_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.util.dt.now",
            lambda: (
                dt_util.start_of_local_day(date(2026, 1, 31))
                + timedelta(hours=23, minutes=59)
            ),
        ),
    ):
        await bf._backfill_cost_sensor(
            hass,
            entry,
            coordinator,  # type: ignore[arg-type]
            hours,
            {},
            {},
        )
        live = await _ytd_until(hass, entry, snap, card)
        digital = await _ytd_until(hass, _entry(analog_meter=False), snap, card)
    assert captured, "the backfill imported nothing"
    assert captured[-1]["sum"] == pytest.approx(live, abs=1e-3)
    # 14 days of the analog rate less 14 days of capacity on 2,5 kW fewer.
    surcharge = _ANTWERPEN_2026 * 1.06 * 14 * 24 * _KWH_PER_HOUR
    capacity = 2.5 * 52.3679 / 12.0 * 14 / 31
    assert live - digital == pytest.approx(surcharge - capacity, abs=1e-6)


async def _ytd_until(
    hass: HomeAssistant, entry: MockConfigEntry, snap: Any, card: Any
) -> float:
    """The live year to date on ``snap``, inside the caller's patches."""
    total = await ytd_cost._compute_current_year_cost(
        hass,
        None,  # type: ignore[arg-type]
        make_stub_extractor(),
        snap,
        entry,
        billed_peak_kw=5.0,
        snapshot_raw=card,
    )
    assert total is not None
    return total


async def _answered(
    manager: Any, result: Any, answers: dict[str, dict[str, Any]]
) -> tuple[Any, list[str]]:
    """Every form from ``result`` on submitted as shown but for ``answers``,
    and the steps walked. An answer of None leaves its field blank."""
    from homeassistant.data_entry_flow import FlowResultType

    steps: list[str] = []
    while result["type"] == FlowResultType.FORM:
        steps.append(result["step_id"])
        form = {**_shown(result), **answers.get(result["step_id"], {})}
        result = await manager.async_configure(
            result["flow_id"],
            {key: value for key, value in form.items() if value is not None},
        )
    return result, steps


async def _walked(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    answers: dict[str, dict[str, Any]],
    *,
    switch: str | None = None,
) -> list[str]:
    """The steps Edit settings, or a supplier switch recorded on ``switch``,
    walks with ``answers``, the entry saved at the end. An answer of None
    leaves its field blank."""
    opts = hass.config_entries.options
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        result = await opts.async_init(entry.entry_id)
        menu = {"next_step_id": "switch" if switch else "edit"}
        result = await opts.async_configure(result["flow_id"], menu)
        if switch:
            result = await opts.async_configure(
                result["flow_id"], {"switch_date": switch}
            )
        _, steps = await _answered(opts, result, answers)
    return steps


async def _set_up(
    hass: HomeAssistant, answers: dict[str, dict[str, Any]]
) -> tuple[list[str], dict[str, Any]]:
    """The steps setup walks with ``answers``, and the data it stores."""
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.be_electricity_prices.const import DOMAIN

    flow = hass.config_entries.flow
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        result = await flow.async_init(DOMAIN, context={"source": "user"})
        result, steps = await _answered(flow, result, answers)
    assert result["type"] == FlowResultType.CREATE_ENTRY, result
    return steps, dict(result["result"].data)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_meter_only_a_digital_one_carries_asks_the_day_too(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Picking the smart meter, or switching to a dynamic contract, drops the
    box with nothing asked, and the year billed all digital again: 17,96 EUR
    over the bill on the 1 July case, and a contract kept at the switch analog
    to its last day. A move to another region asks nothing and drops the day."""
    from custom_components.be_electricity_prices.contract_periods import (
        previous_periods,
    )

    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    day = {"analog_replaced": {"analog_meter_replaced": "2026-07-01"}}

    smart = _entry(analog_meter=True)
    smart.add_to_hass(hass)
    steps = await _walked(hass, smart, {"meter": {"meter": "dynamic"}, **day})
    assert "analog_replaced" in steps
    assert smart.data["meter"] == "dynamic"
    assert smart.data["analog_meter_replaced"] == "2026-07-01"
    total, _ = await _ytd(hass, smart, 5.0)
    assert total == pytest.approx(1158.70, abs=0.01)

    switched = _entry(analog_meter=True)
    switched.add_to_hass(hass)
    steps = await _walked(
        hass,
        switched,
        {
            "contract": {
                "contract": "engie_dynamic",
                "contract_start_date": "2026-09-01",
            },
            "meter": {"meter": "dynamic"},
            "api_key": {"keyless_spots": True},
            **day,
        },
        switch="2026-09-01",
    )
    assert "analog_replaced" in steps
    kept = previous_periods(switched.data, date(2026, 1, 1), date(2026, 10, 24))
    assert kept[0].end == date(2026, 8, 31)
    assert [
        analog_meter(kept[0].data, d)
        for d in (date(2026, 6, 30), date(2026, 7, 1), date(2026, 8, 31))
    ] == [True, False, False]

    moved = _entry(analog_meter=False, analog_meter_replaced="2026-07-01")
    moved.add_to_hass(hass)
    steps = await _walked(
        hass, moved, {"edit": {"region": "wallonia"}, "dso": {"dso": "ores"}}
    )
    assert "analog_replaced" not in steps
    assert "analog_meter_replaced" not in moved.data


@pytest.mark.parametrize(
    "answer", [{}, {"analog_meter": True}], ids=["never", "ticked"]
)
@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_social_entry_picking_the_smart_meter_keeps_its_day(
    hass: HomeAssistant, freezer: Any, answer: dict[str, Any]
) -> None:
    """The box is not offered on the smart meter, so it was dropped, and a
    social entry holding no answer takes the kept contract's again, which
    made the day it had just been given do nothing: the kept contract analog
    to its last day, and the step never shown again to put it right."""
    from custom_components.be_electricity_prices.contract_periods import (
        previous_periods,
    )

    freezer.move_to("2026-10-24 12:00:00+02:00")
    kept = {**_entry(analog_meter=True).data}
    days = (date(2026, 2, 28), date(2026, 3, 1), date(2026, 6, 30))
    social = _entry(
        supplier="social",
        contract="social_engie",
        previous_contracts=[{"until": "2026-07-01", "data": kept}],
        **answer,
    )
    social.add_to_hass(hass)
    await _walked(
        hass,
        social,
        {
            "meter": {"meter": "dynamic"},
            "analog_replaced": {"analog_meter_replaced": "2026-03-01"},
        },
    )
    assert social.data["analog_meter"] is False
    held = previous_periods(social.data, date(2026, 1, 1), date(2026, 10, 24))
    assert [analog_meter(held[0].data, d) for d in days] == [True, False, False]
    steps = await _walked(hass, social, {})
    assert "analog_replaced" in steps
    assert social.data["analog_meter_replaced"] == "2026-03-01"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_switch_off_a_social_entry_never_asked_keeps_it_analog(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The box read the kept contract's answer only while the entry was on
    the social tariff, so a switch to a commercial supplier showed it
    unticked and, submitted as shown, recorded a replacement on a meter still
    analog."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    kept = {**_entry(analog_meter=True).data}
    social = _entry(
        supplier="social",
        contract="social_engie",
        previous_contracts=[{"until": "2026-07-01", "data": kept}],
    )
    social.add_to_hass(hass)
    steps = await _walked(
        hass,
        social,
        {
            "edit": {"supplier": "engie"},
            "contract": {
                "contract": "engie_basic_online",
                "contract_start_date": "2026-09-01",
            },
        },
        switch="2026-09-01",
    )
    assert "analog_replaced" not in steps
    assert social.data["supplier"] == "engie"
    assert social.data["analog_meter"] is True
    assert "analog_meter_replaced" not in social.data


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_day_is_shown_again_only_while_it_is_still_billed(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Shown on every edit for good once a day was held. One of this year or
    the last still moves what the year-to-date walks and a backfill price;
    an older one stays stored for a backfill, without the step."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    for held, shown in (("2025-03-01", True), ("2024-12-31", False)):
        entry = _entry(analog_meter=False, analog_meter_replaced=held)
        entry.add_to_hass(hass)
        steps = await _walked(hass, entry, {"meter": {"meter": "bi"}})
        assert ("analog_replaced" in steps) is shown, held
        assert entry.data["analog_meter_replaced"] == held
        await hass.config_entries.async_remove(entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_asks_the_day_a_meter_was_replaced(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The day was asked only once the box had been ticked and saved, so a
    household set up after Fluvius replaced its meter on 1 July billed the
    half year before it digital: 1176,66 EUR to 24 October at a 5 kW mean,
    where the bill is 1158,70. Setup asks it wherever the meter is not
    analog, the smart meter of a dynamic contract included."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    flemish = {
        "user": {"supplier": "engie", "region": "flanders"},
        "dso": {"dso": "fluvius_antwerpen"},
        "analog_replaced": {"analog_meter_replaced": "2026-07-01"},
    }
    steps, data = await _set_up(
        hass, {**flemish, "contract": {"contract": "engie_basic_online"}}
    )
    assert steps[steps.index("meter") + 1] == "analog_replaced"
    assert data["analog_meter"] is False
    assert data["analog_meter_replaced"] == "2026-07-01"
    total, _ = await _ytd(hass, _entry(**data), 5.0)
    assert total == pytest.approx(1158.70, abs=0.01)

    steps, data = await _set_up(
        hass,
        {
            **flemish,
            "contract": {"contract": "engie_dynamic"},
            "api_key": {"keyless_spots": True},
        },
    )
    assert data["meter"] == "dynamic"
    assert steps[steps.index("meter") + 1] == "analog_replaced"
    assert analog_meter(data, date(2026, 6, 30))
    assert not analog_meter(data, date(2026, 7, 1))


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_entry_that_never_answered_is_asked_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One stored before the question existed holds no answer at all, and
    one set up unticked since holds the box alone; the only way to the day
    was to tick the box, save and untick it, which drops the peaks the new
    meter measured. Asked once on the next edit, and a blank answer is kept
    so the step does not come back on every edit."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    day = {"analog_replaced": {"analog_meter_replaced": "2026-07-01"}}
    for answered in ({}, {"analog_meter": False}):
        entry = _entry(**answered)
        entry.add_to_hass(hass)
        assert "analog_replaced" in await _walked(hass, entry, day)
        assert entry.data["analog_meter_replaced"] == "2026-07-01"
        total, _ = await _ytd(hass, entry, 5.0)
        assert total == pytest.approx(1158.70, abs=0.01)
        await hass.config_entries.async_remove(entry.entry_id)

    blank = _entry()
    blank.add_to_hass(hass)
    assert "analog_replaced" in await _walked(hass, blank, {})
    assert blank.data["analog_meter_replaced"] is None
    assert "analog_replaced" not in await _walked(hass, blank, {})
    total, _ = await _ytd(hass, blank, 5.0)
    assert total == pytest.approx(1176.66, abs=0.01)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_day_given_on_the_smart_meter_counts_on_a_social_entry(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The box is not offered on the smart meter, so the answer is dropped
    there, and a social entry holding none takes the kept contract's: an
    analog one made the day just given count for nothing."""
    freezer.move_to("2026-10-24 12:00:00+02:00")
    kept = {**_entry(analog_meter=True).data}
    social = _entry(
        supplier="social",
        contract="social_engie",
        meter="dynamic",
        previous_contracts=[{"until": "2026-07-01", "data": kept}],
    )
    social.add_to_hass(hass)
    steps = await _walked(
        hass, social, {"analog_replaced": {"analog_meter_replaced": "2026-08-01"}}
    )
    assert "analog_replaced" in steps
    assert social.data["analog_meter"] is False
    assert analog_meter(social.data, date(2026, 7, 31))
    assert not analog_meter(social.data, date(2026, 8, 1))


async def test_the_digital_meter_is_billed_on_its_own_months(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A sensor that measured the months before the replacement measured the
    analog meter, which Fluvius bills the minimum, and never ticking the box
    no longer clears them: the digital meter's mean takes its own months only.
    They are kept all the same, and count again once the day is cleared."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.coordinator_peak import _PeakMixin

    freezer.move_to("2026-10-24 12:00:00+02:00")
    history = {
        "2026-02-01": 7.5,
        "2026-06-01": 6.1,
        "2026-07-01": 4.6,
        "2026-08-01": 5.2,
        "2026-09-01": 4.9,
    }
    coord: Any = SimpleNamespace(
        hass=hass,
        entry=_entry(analog_meter=False, analog_meter_replaced="2026-07-15"),
        _peak_kw=3.8,
        _peak_month=date(2026, 10, 1),
        _peak_history=dict(history),
    )
    coord._measures_peak = lambda: _PeakMixin._measures_peak(coord)
    await _PeakMixin._track_monthly_peak(coord)
    assert coord._peak_history == history
    assert _PeakMixin._peak_terms(coord) == [4.6, 5.2, 4.9, 3.8]
    coord.entry = _entry(analog_meter=False, analog_meter_replaced=None)
    assert _PeakMixin._peak_terms(coord) == [7.5, 6.1, 4.6, 5.2, 4.9, 3.8]


def test_the_help_does_not_clear_a_day_of_last_year() -> None:
    """The step shows a day of this year or the last (``_day_still_billed``),
    and its help said to leave it empty if the meter was not analog this
    year: in January that cleared a correct December day, and a backfill into
    the year before then billed the days before it digital."""
    import json
    from pathlib import Path

    base = Path("custom_components/be_electricity_prices")
    window = {
        "strings.json": "this year or the last",
        "translations/en.json": "this year or the last",
        "translations/fr.json": "cette année ou l'année passée",
        "translations/nl.json": "in dit of vorig jaar",
        "translations/de.json": "in diesem oder im letzten Jahr",
    }
    for name, words in window.items():
        texts = json.loads((base / name).read_text(encoding="utf-8"))
        for flow in ("config", "options"):
            step = texts[flow]["step"]["analog_replaced"]
            text = step["data_description"]["analog_meter_replaced"]
            assert text.startswith("[%key:") or words in text, (name, flow)
