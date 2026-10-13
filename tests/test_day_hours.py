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

"""The Fluvius municipalities whose day register runs from 06:00 to 21:00
(issue #131)."""

from __future__ import annotations

import ast
import inspect
import pathlib
from datetime import date, datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.compare_weighting import _register_hours
from custom_components.be_electricity_prices.const import DSO_CHOICES
from custom_components.be_electricity_prices.contract_periods import (
    _with_household_facts,
)
from custom_components.be_electricity_prices.day_night import day_start_for, is_offpeak
from custom_components.be_electricity_prices.flow_schemas import _meter_schema
from custom_components.be_electricity_prices.pricing import compute_breakdown
from custom_components.be_electricity_prices.providers._rates import FixedRates
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
)
from tests import make_snapshot

_PACKAGE = pathlib.Path(inspect.getfile(const)).parent

# Wednesday 2026-04-29, Friday 2026-05-01 is Labour Day, Saturday 2026-05-02.
_WEDNESDAY = date(2026, 4, 29)


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def test_the_early_schedule_moves_both_switches_an_hour_earlier() -> None:
    early = const.DAY_START_EARLY
    assert is_offpeak(_at(_WEDNESDAY, 5, 59), day_start=early)
    assert not is_offpeak(_at(_WEDNESDAY, 6), day_start=early)
    assert not is_offpeak(_at(_WEDNESDAY, 20, 59), day_start=early)
    assert is_offpeak(_at(_WEDNESDAY, 21), day_start=early)
    # The standard schedule is untouched.
    assert is_offpeak(_at(_WEDNESDAY, 6))
    assert not is_offpeak(_at(_WEDNESDAY, 21))
    assert is_offpeak(_at(_WEDNESDAY, 22))
    # Weekends stay off-peak throughout, and a weekday holiday stays a
    # weekday on a Flemish meter.
    assert is_offpeak(_at(date(2026, 5, 2), 12), day_start=early)
    assert not is_offpeak(_at(date(2026, 5, 1), 12), day_start=early)


def test_every_weekday_still_has_fifteen_day_hours() -> None:
    for start in (const.DAY_START, const.DAY_START_EARLY):
        day = [
            h for h in range(24) if not is_offpeak(_at(_WEDNESDAY, h), day_start=start)
        ]
        assert day == list(range(start, start + 15))


def test_only_a_flemish_entry_takes_the_early_schedule() -> None:
    early = {const.CONF_DAY_HOURS: const.DAY_HOURS_EARLY}
    assert day_start_for({const.CONF_REGION: const.REGION_FLANDERS, **early}) == 6
    assert (
        day_start_for(
            {
                const.CONF_REGION: const.REGION_FLANDERS,
                const.CONF_DAY_HOURS: const.DAY_HOURS_STANDARD,
            }
        )
        == 7
    )
    # An entry created before the question existed has no answer.
    assert day_start_for({const.CONF_REGION: const.REGION_FLANDERS}) == 7
    # A stray answer outside Flanders is inert.
    assert day_start_for({const.CONF_REGION: const.REGION_BRUSSELS, **early}) == 7
    assert day_start_for({const.CONF_REGION: const.REGION_WALLONIA, **early}) == 7


def test_the_bill_follows_the_meter_on_both_legs() -> None:
    snap = make_snapshot(
        energy=FixedRates(single=0.20, peak=0.22, offpeak=0.18),
        dsos={
            "fluvius_zenne_dijle": DsoOverlay(
                distribution_single=0.05,
                distribution_peak=0.06,
                distribution_offpeak=0.04,
                transport=0.0,
            )
        },
        taxes=TaxOverlay(federal_excise=0.0, energy_contribution=0.0),
    )

    def _bill(hour: int, start: int) -> tuple[float, float]:
        bd = compute_breakdown(
            snap,
            "fluvius_zenne_dijle",
            const.REGION_FLANDERS,
            _at(_WEDNESDAY, hour),
            None,
            "bi",
            day_start=start,
        )
        return round(bd.energy, 6), round(bd.network, 6)

    assert _bill(21, const.DAY_START) == (0.22, 0.06)
    assert _bill(21, const.DAY_START_EARLY) == (0.18, 0.04)
    assert _bill(6, const.DAY_START) == (0.18, 0.04)
    assert _bill(6, const.DAY_START_EARLY) == (0.22, 0.06)


def test_the_register_year_moves_with_the_schedule() -> None:
    day, night = _register_hours(
        const.REGION_FLANDERS,
        2026,
        "bi",
        "bi_horaire",
        day_start=const.DAY_START_EARLY,
    )
    # 261 weekdays in 2026.
    assert day[6] == 261.0
    assert day[21] == 0.0
    assert night[21] == 365.0
    assert night[6] == 365.0 - 261.0


def _calls_missing_day_start() -> list[str]:
    """Every call in the package that hands a function taking ``day_start``
    its region and not its schedule. A default left in place there bills the
    standard hours whatever the household answered, and nothing fails."""
    trees = {
        path: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_PACKAGE.rglob("*.py"))
    }
    region_at: dict[str, set[int | None]] = {}
    for tree in trees.values():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            positional = [a.arg for a in args.posonlyargs + args.args]
            names = positional + [a.arg for a in args.kwonlyargs]
            if "day_start" in names and "region" in names:
                region_at.setdefault(node.name, set()).add(
                    positional.index("region") if "region" in positional else None
                )
    missing = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.id
                if isinstance(func, ast.Name)
                else func.attr
                if isinstance(func, ast.Attribute)
                else None
            )
            if name not in region_at:
                continue
            keywords = {k.arg for k in node.keywords}
            binds_region = "region" in keywords or any(
                index is not None and len(node.args) > index
                for index in region_at[name]
            )
            if binds_region and "day_start" not in keywords:
                missing.append(f"{path.name}:{node.lineno} {name}")
    return missing


def test_every_call_that_passes_a_region_passes_the_schedule() -> None:
    assert _calls_missing_day_start() == []


def test_the_meter_step_asks_a_flemish_entry_only() -> None:
    def _field(region: str, **defaults: Any) -> Any:
        schema = _meter_schema(
            "eneco", "eneco_power_flex", {const.CONF_REGION: region, **defaults}
        )
        for key, value in schema.schema.items():
            if str(key) == const.CONF_DAY_HOURS:
                return key.default(), list(value.config["options"])
        return None

    assert _field(const.REGION_FLANDERS) == (
        const.DAY_HOURS_STANDARD,
        list(const.DAY_HOURS),
    )
    assert _field(
        const.REGION_FLANDERS, **{const.CONF_DAY_HOURS: const.DAY_HOURS_EARLY}
    ) == (const.DAY_HOURS_EARLY, list(const.DAY_HOURS))
    assert _field(const.REGION_BRUSSELS) is None
    assert _field(const.REGION_WALLONIA) is None


@pytest.fixture
def _no_setup() -> Any:
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        yield


@pytest.mark.usefixtures("enable_custom_integrations", "_no_setup")
@pytest.mark.parametrize(
    ("region", "kept"),
    [(const.REGION_FLANDERS, True), (const.REGION_BRUSSELS, False)],
)
async def test_the_answer_is_dropped_outside_flanders(
    hass: HomeAssistant, region: str, kept: bool
) -> None:
    from custom_components.be_electricity_prices.flow_wizard import (
        _WizardStepsMixin,
    )

    flow: Any = _WizardStepsMixin()
    flow.hass = hass
    flow._data = {
        const.CONF_SUPPLIER: "eneco",
        const.CONF_CONTRACT: "eneco_power_flex",
        const.CONF_REGION: region,
        const.CONF_DSO: DSO_CHOICES[region][0][0],
        const.CONF_DAY_HOURS: const.DAY_HOURS_EARLY,
    }

    async def _next() -> Any:
        return None

    flow._ask_professional = _next
    await flow.async_step_meter({const.CONF_METER: "bi"})
    assert (const.CONF_DAY_HOURS in flow._data) is kept


def test_an_earlier_contract_recorded_before_the_question_takes_the_entry_answer() -> (
    None
):
    entry = {
        const.CONF_REGION: const.REGION_FLANDERS,
        const.CONF_DAY_HOURS: const.DAY_HOURS_EARLY,
    }
    old = {const.CONF_REGION: const.REGION_FLANDERS, const.CONF_SUPPLIER: "eneco"}
    assert _with_household_facts(old, entry)[const.CONF_DAY_HOURS] == (
        const.DAY_HOURS_EARLY
    )
    answered = {**old, const.CONF_DAY_HOURS: const.DAY_HOURS_STANDARD}
    assert _with_household_facts(answered, entry)[const.CONF_DAY_HOURS] == (
        const.DAY_HOURS_STANDARD
    )
