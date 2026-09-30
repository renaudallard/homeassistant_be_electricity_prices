"""A supplier switch recorded on the entry: the contracts held earlier in the
year, the days each covers and what it cost (``contract_periods.py``), the
window end that prices them, the options step that records one and the
backfill that splits at it."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import (
    backfill_window,
    cohort,
    contract_periods,
    energy_meters,
    ytd_cost,
    snapshot_months,
    ytd_energy,
)
from custom_components.be_electricity_prices import backfill as bf
from custom_components.be_electricity_prices.cohort import _CohortLegs
from custom_components.be_electricity_prices.compare_inputs import _QuoteEntry
from custom_components.be_electricity_prices.const import (
    CONF_CONTRACT_END_DATE,
    CONF_CONTRACT_START_DATE,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_PREVIOUS_CONTRACTS,
    CONF_SOLAR_REGIME,
    CONF_SWITCH_DATE,
    CONF_TARIFF_CARD_DATE,
    CONF_YTD_FROM_CONTRACT_START,
    DOMAIN,
)
from custom_components.be_electricity_prices.contract_periods import (
    ContractPeriod,
    PricedPeriod,
    PricedPeriods,
    current_period_start,
    keep_settled,
    periods_key,
    previous_costs,
    previous_periods,
    previous_rows,
    priced_from_dict,
    priced_to_dict,
    with_previous_contracts,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.flow_switch import (
    _record_switch,
    _remove_last_switch,
    _removable_switch,
    _validate_contract_dates,
    _validate_switch_date,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    InjectionRates,
)
from custom_components.be_electricity_prices.providers.base import (
    CardNotReadableError,
    ExtractorError,
)
from custom_components.be_electricity_prices.snapshot_months import ArchivedCard
from tests import make_entry, make_snapshot, make_stub_extractor

_TICK = "custom_components.be_electricity_prices.coordinator_tick"
_COSTS = "custom_components.be_electricity_prices.coordinator_costs"


def _held(supplier: str, contract: str, **extra: Any) -> dict[str, Any]:
    """An earlier contract's settings, as the switch step keeps them."""
    return {**make_entry(supplier=supplier, contract=contract, **extra).data}


# The days each contract covers.


def test_each_earlier_contract_covers_the_days_before_its_successor() -> None:
    data = {
        "supplier": "eneco",
        "contract": "power_fix",
        CONF_PREVIOUS_CONTRACTS: [
            {"until": "2026-07-01", "data": _held("bolt", "bolt_variable")},
            {"until": "2026-03-01", "data": _held("engie", "engie_easy_fixed")},
        ],
    }
    periods = previous_periods(data, date(2026, 1, 1), date(2026, 9, 24))
    assert [(p.start, p.end, p.data["supplier"]) for p in periods] == [
        (date(2026, 1, 1), date(2026, 2, 28), "engie"),
        (date(2026, 3, 1), date(2026, 6, 30), "bolt"),
    ]
    assert current_period_start(data, date(2026, 1, 1)) == date(2026, 7, 1)


def test_an_earlier_contract_billed_from_its_start_keeps_that_start() -> None:
    """A household whose first contract began on 1 March ticked the box to bill
    the year from then. Recording a switch unticks it on the entry, which is
    about the new contract, and the copy of the contract left still says it
    billed from 1 March: January and February belong to no contract."""
    before = dict(
        make_entry(contract_start_date="2026-03-01", ytd_from_contract_start=True).data
    )
    data = _record_switch(before, date(2026, 7, 1))
    periods = previous_periods(data, date(2026, 1, 1), date(2026, 9, 24))
    assert [(p.start, p.end) for p in periods] == [
        (date(2026, 3, 1), date(2026, 6, 30))
    ]
    # Without the box, the same contract bills from the window's first day.
    held = data[CONF_PREVIOUS_CONTRACTS][0]["data"]
    held.pop(CONF_YTD_FROM_CONTRACT_START)
    periods = previous_periods(data, date(2026, 1, 1), date(2026, 9, 24))
    assert periods[0].start == date(2026, 1, 1)


def test_a_switch_outside_the_window_prices_nothing() -> None:
    """Last year's switch, a record that does not read back, and every earlier
    contract on an entry billing the year from its own contract's start."""
    data = {
        CONF_PREVIOUS_CONTRACTS: [
            {"until": "2025-11-01", "data": _held("bolt", "bolt_variable")},
            {"until": "2026-04-01", "data": _held("engie", "engie_easy_fixed")},
            {"until": "not a date", "data": _held("mega", "mega_fix")},
            {"until": "2026-05-01"},
            "junk",
        ]
    }
    periods = previous_periods(data, date(2026, 1, 1), date(2026, 9, 24))
    assert [(p.start, p.end, p.data["supplier"]) for p in periods] == [
        (date(2026, 1, 1), date(2026, 3, 31), "engie")
    ]
    assert previous_periods(data, date(2026, 4, 1), date(2026, 9, 24)) == []
    assert previous_periods({}, date(2026, 1, 1), date(2026, 9, 24)) == []
    assert current_period_start({}, date(2026, 1, 1)) == date(2026, 1, 1)


def test_the_key_moves_with_the_periods_it_was_priced_for() -> None:
    one = [ContractPeriod(date(2026, 1, 1), date(2026, 3, 31), _held("engie", "x"))]
    two = [
        *one,
        ContractPeriod(date(2026, 4, 1), date(2026, 6, 30), _held("bolt", "y")),
    ]
    assert periods_key(one) == periods_key(list(one))
    assert periods_key(one) != periods_key(two)


# The window end.


async def test_a_year_split_at_a_switch_adds_up_to_the_unsplit_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Same contract both sides, so the split must be exact: every day, every
    fee and every month card on exactly one side of the switch."""
    freezer.move_to("2026-03-20 12:00:00+01:00")
    snap = make_snapshot(energy=FixedRates(single=0.25, yearly_fixed_fee=60.0))
    entry = make_entry(consumption_kwh="sensor.cons")
    days = {date(2026, 1, 1) + timedelta(days=d): 8.0 + d % 5 for d in range(79)}

    async def fake_daily(
        _h: Any, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        if entity_id != "sensor.cons":
            return {}
        return {d: kwh for d, kwh in days.items() if start <= d <= end}

    def price(**window: Any) -> Any:
        return ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            **window,
        )

    with patch.object(energy_meters, "_recorder_daily_kwh", new=fake_daily):
        whole = await price()
        old = await price(
            window_start_override=date(2026, 1, 1), window_end=date(2026, 2, 14)
        )
        new = await price(window_start_override=date(2026, 2, 15))
    assert whole is not None and old is not None and new is not None
    assert old > 0 and new > 0
    assert whole == pytest.approx(old + new, abs=1e-9)


async def test_an_hourly_year_splits_the_same_way_and_the_closed_side_reads_no_live_meter(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-02-10 12:00:00+01:00")
    snap = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.02, yearly_fixed_fee=48.0)
    )
    entry = make_entry(meter="dynamic", consumption_kwh="sensor.cons")
    start = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    stop = dt_util.start_of_local_day(date(2026, 2, 10)).astimezone(UTC)
    hours: list[datetime] = []
    when = start
    while when < stop:
        hours.append(when)
        when += timedelta(hours=1)
    spots = {h: 0.05 + (h.hour % 7) * 0.01 for h in hours}
    per_hour = {h: 0.3 + (h.hour % 3) * 0.1 for h in hours}

    async def fake_hourly(
        _h: Any, entity_id: str, first: date, last: date
    ) -> dict[datetime, float]:
        if entity_id != "sensor.cons":
            return {}
        return {
            h: kwh
            for h, kwh in per_hour.items()
            if first <= dt_util.as_local(h).date() <= last
        }

    top_up = AsyncMock()

    def price(**window: Any) -> Any:
        return ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            historical_spots=spots,
            **window,
        )

    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=fake_hourly),
        patch.object(ytd_energy, "_top_up_today_hourly", new=top_up),
    ):
        whole = await price()
        top_up.reset_mock()
        old = await price(
            window_start_override=date(2026, 1, 1), window_end=date(2026, 1, 20)
        )
        assert top_up.await_count == 0, "a closed window read the live meter"
        new = await price(window_start_override=date(2026, 1, 21))
    assert whole is not None and old is not None and new is not None
    assert whole == pytest.approx(old + new, abs=1e-9)


async def test_compensation_nets_each_contract_on_its_own(
    hass: HomeAssistant, freezer: Any
) -> None:
    """CWaPE CD-14d03, section 5.1.2: a change of supplier splits the year and
    each part nets its own injection. A surplus banked before the switch
    cannot pay for consumption after it, so the two contracts together cost
    more than one year netted as a whole."""
    freezer.move_to("2026-03-20 12:00:00+01:00")
    snap = make_snapshot(energy=FixedRates(single=0.30))
    entry = make_entry(
        solar_regime="compensation",
        solar_kva=4.0,
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
    )
    switch = date(2026, 2, 15)

    async def fake_daily(
        _h: Any, entity_id: str, start: date, end: date
    ) -> dict[date, float]:
        out: dict[date, float] = {}
        day = max(start, date(2026, 1, 1))
        while day <= end and day <= date(2026, 3, 19):
            before = day < switch
            if entity_id == "sensor.cons":
                out[day] = 5.0 if before else 10.0
            elif entity_id == "sensor.inj":
                out[day] = 10.0 if before else 0.0
            day += timedelta(days=1)
        return out

    def price(**window: Any) -> Any:
        return ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            **window,
        )

    with patch.object(energy_meters, "_recorder_daily_kwh", new=fake_daily):
        whole = await price()
        old = await price(
            window_start_override=date(2026, 1, 1),
            window_end=switch - timedelta(days=1),
        )
        new = await price(window_start_override=switch)
    assert whole is not None and old is not None and new is not None
    # 225 kWh banked before the switch, worth at least 0,30 EUR/kWh of energy.
    assert (old + new) - whole > 60.0


# Recording a switch.


def test_recording_a_switch_keeps_the_contract_and_clears_its_answers() -> None:
    data = dict(
        make_entry(
            contract_start_date="2025-03-01",
            tariff_card_date="2025-02-01",
            contract_end_date="2027-02-28",
            ytd_from_contract_start=True,
            manual_energy_single=0.2,
            previous_contracts=[
                {"until": "2025-06-01", "data": _held("bolt", "bolt_variable")},
                {"until": "2026-02-01", "data": _held("engie", "engie_easy_fixed")},
            ],
        ).data
    )
    out = _record_switch(data, date(2026, 6, 15))
    records = out[CONF_PREVIOUS_CONTRACTS]
    # Last year's switch prices nothing this year and goes.
    assert [r["until"] for r in records] == ["2026-02-01", "2026-06-15"]
    held = records[-1]["data"]
    assert held["supplier"] == data["supplier"]
    assert held[CONF_CONTRACT_START_DATE] == "2025-03-01"
    assert CONF_PREVIOUS_CONTRACTS not in held
    assert out[CONF_CONTRACT_START_DATE] == "2026-06-15"
    for key in (
        CONF_TARIFF_CARD_DATE,
        CONF_CONTRACT_END_DATE,
        CONF_YTD_FROM_CONTRACT_START,
        CONF_MANUAL_ENERGY_SINGLE,
    ):
        assert key not in out, key


@pytest.mark.parametrize(
    ("switch_date", "error"),
    [
        ("2026-09-25", "switch_date_outside_year"),
        ("2026-01-01", "switch_date_outside_year"),
        ("2025-12-31", "switch_date_outside_year"),
        ("2026-06-30", "switch_date_before_last"),
        ("2026-07-01", "switch_date_before_last"),
        ("2026-07-02", None),
        ("2026-09-24", None),
        # The contract being left started on 10 July.
        ("2026-07-10", "switch_date_before_start"),
    ],
)
def test_a_switch_date_prices_days_this_year_after_the_last_switch(
    freezer: Any, switch_date: str, error: str | None
) -> None:
    freezer.move_to("2026-09-24 12:00:00+02:00")
    data: dict[str, Any] = {
        CONF_PREVIOUS_CONTRACTS: [
            {"until": "2026-07-01", "data": _held("engie", "engie_easy_fixed")}
        ]
    }
    if error == "switch_date_before_start":
        data[CONF_CONTRACT_START_DATE] = "2026-07-10"
    errors = _validate_switch_date(data, {CONF_SWITCH_DATE: switch_date})
    assert errors == ({} if error is None else {CONF_SWITCH_DATE: error})


@pytest.mark.parametrize(
    ("start", "error"),
    [
        ("2026-06-01", "start_date_before_switch"),
        ("2026-08-31", "start_date_before_switch"),
        ("2026-09-01", None),
        (None, None),
    ],
)
def test_the_contract_cannot_start_before_the_last_switch(
    freezer: Any, start: str | None, error: str | None
) -> None:
    """The entry's contract is the one supplying since the last switch, so
    a start date before it would price the contract left only from that
    date and look the new contract's signing card up a month too early."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    data: dict[str, Any] = {
        CONF_PREVIOUS_CONTRACTS: [
            {"until": "2026-09-01", "data": _held("engie", "engie_easy_fixed")}
        ]
    }
    user_input = {} if start is None else {CONF_CONTRACT_START_DATE: start}
    errors = _validate_contract_dates(user_input, data)
    assert errors == ({} if error is None else {CONF_CONTRACT_START_DATE: error})
    # Nothing to hold it to without a recorded switch.
    assert _validate_contract_dates(user_input, {}) == {}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_options_menu_records_a_switch_and_sets_up_the_new_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(ytd_from_contract_start=True, contract_start_date="2025-03-01")
    entry.add_to_hass(hass)
    old_supplier = entry.data["supplier"]
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "switch" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "switch"}
    )
    assert result["step_id"] == "switch"
    bad = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SWITCH_DATE: "2026-10-01"}
    )
    assert bad["errors"] == {CONF_SWITCH_DATE: "switch_date_outside_year"}
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SWITCH_DATE: "2026-06-15"}
    )
    assert result["step_id"] == "edit"
    answers: dict[str, dict[str, Any]] = {
        "edit": {"supplier": "cociter", "region": "wallonia"},
        "contract": {
            "contract": "cociter_variable",
            CONF_CONTRACT_START_DATE: "2026-06-15",
        },
        "dso": {"dso": "ores"},
        "meter": {"meter": "bi"},
        "dso_tariff_mode": {"dso_tariff_mode": "bi_horaire"},
        "solar": {"solar_kva": 0.0, "solar_regime": "none"},
    }
    for _ in range(12):
        if result["type"] != data_entry_flow.FlowResultType.FORM:
            break
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], answers.get(result["step_id"], {})
        )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.data["supplier"] == "cociter"
    assert entry.data[CONF_CONTRACT_START_DATE] == "2026-06-15"
    # Unticked by the switch, so the contract step shows it off and stores that.
    assert not entry.data.get(CONF_YTD_FROM_CONTRACT_START)
    (record,) = entry.data[CONF_PREVIOUS_CONTRACTS]
    assert record["until"] == "2026-06-15"
    assert record["data"]["supplier"] == old_supplier
    assert record["data"][CONF_CONTRACT_START_DATE] == "2025-03-01"


def test_removing_the_last_switch_puts_the_settings_back_as_they_were() -> None:
    first = _held("bolt", "bolt_variable")
    data = dict(
        make_entry(
            contract_start_date="2025-03-01",
            tariff_card_date="2025-02-01",
            ytd_from_contract_start=True,
            manual_energy_single=0.2,
            previous_contracts=[{"until": "2026-02-01", "data": first}],
        ).data
    )
    recorded = _record_switch(data, date(2026, 6, 15))
    # What the edit chain changes after the switch step.
    recorded.update(supplier="cociter", contract="cociter_variable")
    out = contract_periods.recorded_contracts(recorded)
    assert len(out) == 2
    back = _remove_last_switch(recorded)
    assert back == data
    # The earlier switch stays, and removing it too leaves none.
    assert [r["until"] for r in back[CONF_PREVIOUS_CONTRACTS]] == ["2026-02-01"]
    assert CONF_PREVIOUS_CONTRACTS not in _remove_last_switch(back)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_options_menu_removes_the_last_switch(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-24 12:00:00+02:00")
    plain = make_entry()
    plain.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(plain.entry_id)
    assert "remove_switch" not in result["menu_options"]

    before = dict(make_entry(contract_start_date="2025-03-01").data)
    after = {
        **_record_switch(before, date(2026, 6, 15)),
        "supplier": "cociter",
        "contract": "cociter_variable",
    }
    entry = MockConfigEntry(domain=DOMAIN, data=after, title="Cociter")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "remove_switch" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "remove_switch"}
    )
    assert result["step_id"] == "remove_switch"
    placeholders = result["description_placeholders"]
    assert placeholders is not None
    assert placeholders["until"] == "2026-06-15"
    assert placeholders["supplier"] == "Eneco"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert dict(entry.data) == before


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_switch_from_an_earlier_year_is_not_offered_for_removal(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A switch recorded last year prices nothing this year and is a real
    change of supplier, long settled. Removing it would put back a contract
    the household left before the year began, so the menu does not offer it
    and the step refuses it."""
    freezer.move_to("2027-02-10 12:00:00+01:00")
    before = dict(make_entry(contract_start_date="2025-03-01").data)
    after = {
        **_record_switch(before, date(2026, 6, 15)),
        "supplier": "cociter",
        "contract": "cociter_variable",
    }
    entry = MockConfigEntry(domain=DOMAIN, data=after, title="Cociter")
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "remove_switch" not in result["menu_options"]
    # The step asks the same question, so it refuses the record too.
    assert _removable_switch(after, date(2027, 2, 10)) is None
    assert _removable_switch(after, date(2026, 9, 24)) is not None


def test_the_reload_signature_takes_a_list_of_earlier_contracts() -> None:
    """The coordinator compares entry.data to decide on a reload, and a
    frozenset of the raw values cannot hold the switch record's list."""
    entry = make_entry(
        previous_contracts=[{"until": "2026-06-15", "data": _held("engie", "x")}]
    )
    other = make_entry(
        previous_contracts=[{"until": "2026-06-16", "data": _held("engie", "x")}]
    )
    signature = BePricesCoordinator._compute_data_signature(entry)
    assert signature == BePricesCoordinator._compute_data_signature(entry)
    assert signature != BePricesCoordinator._compute_data_signature(other)


# The day's pricing.


def _priced(
    periods: list[ContractPeriod], *rows: PricedPeriod, month: date
) -> PricedPeriods:
    return PricedPeriods(
        key=periods_key(periods), day=date(2026, 9, 24), month=month, rows=rows
    )


def test_the_earlier_share_is_unknown_until_priced_for_these_periods() -> None:
    period = ContractPeriod(date(2026, 1, 1), date(2026, 6, 14), _held("engie", "x"))
    row = PricedPeriod(
        start=period.start,
        end=period.end,
        supplier="engie",
        contract="x",
        cost=250.0,
        month_cost=None,
        stand_in=False,
    )
    september = date(2026, 9, 1)
    assert previous_costs(None, [], september) == (0.0, 0.0)
    # Unpriced: the year waits, the month does not, since June is not in it.
    assert previous_costs(None, [period], september) == (None, 0.0)
    priced = _priced([period], row, month=september)
    assert previous_costs(priced, [period], september) == (250.0, 0.0)
    other = [ContractPeriod(date(2026, 1, 1), date(2026, 6, 20), _held("engie", "x"))]
    assert previous_costs(priced, other, september) == (None, 0.0)
    failed = _priced(
        [period], PricedPeriod(**{**row.__dict__, "cost": None}), month=september
    )
    assert previous_costs(failed, [period], september) == (None, 0.0)
    # A switch this month: the part of the old contract inside it counts too,
    # and waits on the pricing like the year does.
    june = ContractPeriod(date(2026, 1, 1), date(2026, 9, 14), _held("engie", "x"))
    assert previous_costs(None, [june], september) == (None, None)
    in_month = PricedPeriod(**{**row.__dict__, "end": june.end, "month_cost": 40.0})
    assert previous_costs(
        _priced([june], in_month, month=september), [june], september
    ) == (250.0, 40.0)
    stored = priced_from_dict(priced_to_dict(priced))
    assert stored == priced
    assert priced_from_dict({"key": "x"}) is None


async def test_the_tick_adds_the_earlier_contract_and_reads_unknown_until_priced(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        contract_start_date="2026-06-15",
        previous_contracts=[
            {"until": "2026-06-15", "data": _held("engie", "engie_easy_fixed")}
        ],
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    windows: list[date | None] = []

    async def own(*_a: Any, **kwargs: Any) -> float:
        windows.append(kwargs.get("window_start_override"))
        return 100.0

    release = asyncio.Event()
    row = PricedPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 6, 14),
        supplier="engie",
        contract="engie_easy_fixed",
        cost=250.0,
        month_cost=None,
        stand_in=False,
    )

    async def pricing(*_a: Any, **_k: Any) -> list[PricedPeriod]:
        await release.wait()
        return [row]

    with (
        patch(f"{_COSTS}._compute_current_year_cost", new=own),
        patch(f"{_COSTS}.price_previous_periods", new=pricing),
        patch(f"{_TICK}._cohort_legs", AsyncMock(return_value=_CohortLegs(None, None))),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        first = await coord._update_body()
        assert first.current_year_cost_eur is None
        assert first.current_month_cost_eur == pytest.approx(100.0)
        assert first.previous_contracts == ()
        release.set()
        assert coord._previous_pricing is not None
        await coord._previous_pricing
        second = await coord._update_body()
    # The entry's own contract from the day it started, the month from its 1st.
    assert windows[:2] == [date(2026, 6, 15), date(2026, 9, 1)]
    assert second.current_year_cost_eur == pytest.approx(350.0)
    assert second.current_month_cost_eur == pytest.approx(100.0)
    assert second.ytd_diagnostics is not None
    assert second.ytd_diagnostics["previous_contracts_eur"] == pytest.approx(250.0)
    assert second.previous_contracts[0]["supplier"] == "engie"
    assert second.previous_contracts[0]["to"] == "2026-06-14"
    coord.async_request_refresh.assert_awaited()


async def test_a_pricing_that_missed_a_contract_is_asked_again_next_tick(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Kept for the day only when every contract priced: the year reads
    unknown while one did not, and that should last an hour, not a day."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        previous_contracts=[
            {"until": "2026-06-15", "data": _held("engie", "engie_easy_fixed")}
        ]
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    row = PricedPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 6, 14),
        supplier="engie",
        contract="engie_easy_fixed",
        cost=None,
        month_cost=None,
        stand_in=False,
    )
    coord._previous_priced = _priced(periods, row, month=date(2026, 9, 1))
    priced_again = AsyncMock()
    coord._price_previous = priced_again  # type: ignore[method-assign]
    coord._schedule_previous_pricing(periods, date(2026, 9, 24))
    assert coord._previous_pricing is not None
    await coord._previous_pricing
    priced_again.assert_awaited_once()
    # Whole, the same day's pricing is kept.
    coord._previous_priced = _priced(
        periods, PricedPeriod(**{**row.__dict__, "cost": 250.0}), month=date(2026, 9, 1)
    )
    coord._previous_pricing = None
    coord._schedule_previous_pricing(periods, date(2026, 9, 24))
    assert coord._previous_pricing is None


async def test_a_pricing_that_keeps_failing_waits_for_the_next_hourly_tick(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The pricing asks for a refresh when it lands, and that refresh is a
    tick, which asked for the pricing again: a period that cannot be priced
    was fetched and walked again about every 10 s, all day."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    held = {**_held("eneco", "power_fix"), "supplier": "gone_supplier"}
    entry = make_entry(previous_contracts=[{"until": "2026-06-15", "data": held}])
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    runs = 0

    async def refresh() -> None:
        # All a refresh does here: the tick asks for the pricing again.
        nonlocal runs
        runs += 1
        coord._schedule_previous_pricing(periods, date(2026, 9, 24))

    coord.async_request_refresh = refresh  # type: ignore[method-assign]
    coord._schedule_previous_pricing(periods, date(2026, 9, 24))
    for _ in range(5):
        task = coord._previous_pricing
        if task is not None and not task.done():
            await task
    assert runs == 1
    assert coord._previous_priced is not None
    assert coord._previous_priced.rows[0].cost is None
    # The next hourly tick asks again.
    freezer.tick(timedelta(hours=1))
    before = coord._previous_pricing
    coord._schedule_previous_pricing(periods, date(2026, 9, 24))
    assert coord._previous_pricing is not None
    assert coord._previous_pricing is not before
    await coord._previous_pricing
    assert runs == 2


async def test_an_old_dynamic_contract_fills_the_spots_with_the_key_it_kept(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A household that left a dynamic contract for a fixed one may hold no
    ENTSO-E key any more, and the walk fetches nothing without one: the old
    contract would then bill no energy for its whole period."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        previous_contracts=[
            {
                "until": "2026-06-15",
                "data": _held(
                    "engie", "engie_dynamic", meter="dynamic", api_key="OLDKEY"
                ),
            }
        ]
    )
    entry.add_to_hass(hass)
    assert "api_key" not in entry.data
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    fill = AsyncMock()
    coord._ensure_historical_spots = fill  # type: ignore[method-assign]
    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    with patch(f"{_COSTS}.price_previous_periods", AsyncMock(return_value=[])):
        await coord._price_previous(periods, date(2026, 9, 24))
    fill.assert_awaited_once_with(date(2026, 1, 1), date(2026, 6, 14), "OLDKEY")


@pytest.mark.parametrize(
    ("held", "fetched"),
    [
        # A card indexed on the delivery month's mean, re-priced by the walk
        # whenever the settings hold a key: August on Mega Online Flex billed
        # 53,42 EUR of its 108,27 with no spots.
        (_held("engie", "engie_direct_online", api_key="OLDKEY"), True),
        (_held("mega", "mega_online_flex", api_key="OLDKEY"), True),
        # A variable signing cohort, re-priced off its archived card's formula.
        (
            _held(
                "bolt",
                "bolt_variable",
                api_key="OLDKEY",
                contract_start_date="2026-02-10",
            ),
            True,
        ),
        # The walk keeps the printed figure without a key, and a variable card
        # that names no cohort month and is not month indexed prints its rate.
        (_held("engie", "engie_direct_online"), False),
        (_held("bolt", "bolt_variable", api_key="OLDKEY"), False),
        (_held("engie", "engie_easy_fixed", api_key="OLDKEY"), False),
    ],
)
async def test_an_old_contract_the_walk_reprices_on_the_day_ahead_fills_the_spots(
    hass: HomeAssistant, freezer: Any, held: dict[str, Any], fetched: bool
) -> None:
    """Asked of what the walk does with the old contract, not of its kind:
    most month-indexed cards are registered variable, and the reload that
    follows a switch drops the spots the old contract had collected."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(previous_contracts=[{"until": "2026-06-15", "data": held}])
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    fill = AsyncMock()
    coord._ensure_historical_spots = fill  # type: ignore[method-assign]
    coord._ensure_rlp_weights = AsyncMock()  # type: ignore[method-assign]
    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    with patch(f"{_COSTS}.price_previous_periods", AsyncMock(return_value=[])):
        await coord._price_previous(periods, date(2026, 9, 24))
    if fetched:
        fill.assert_awaited_once_with(date(2026, 1, 1), date(2026, 6, 14), "OLDKEY")
    else:
        fill.assert_not_awaited()


@pytest.mark.parametrize(
    ("held", "loaded"),
    [
        # Mega's flex cards settle on the RLP-weighted month mean, and so do
        # TotalEnergies' variables and Eneco Flex One: all registered variable.
        (_held("mega", "mega_online_flex", api_key="OLDKEY"), True),
        (_held("eneco", "power_flex_one", api_key="OLDKEY"), True),
        # Keyless, the walk keeps the printed figure and weights nothing.
        (_held("mega", "mega_online_flex"), False),
        (_held("engie", "engie_easy_fixed", api_key="OLDKEY"), False),
    ],
)
async def test_an_old_contract_settled_on_a_weighted_mean_loads_the_profile(
    hass: HomeAssistant, freezer: Any, held: dict[str, Any], loaded: bool
) -> None:
    """Without the profile the walk falls back to the plain month mean, about
    1 to 1,5 EUR a month off on an RLP-indexed card. Loaded before pricing,
    in the entry's own blend: the old card's index is reduced from the same
    workbook read, and moving the live coordinator's blend is not ours to do."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(previous_contracts=[{"until": "2026-06-15", "data": held}])
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    rlp = AsyncMock()
    coord._ensure_rlp_weights = rlp  # type: ignore[method-assign]
    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    assert contract_periods.periods_need_rlp(periods) is loaded
    with patch(f"{_COSTS}.price_previous_periods", AsyncMock(return_value=[])):
        await coord._price_previous(periods, date(2026, 9, 24))
    if loaded:
        rlp.assert_awaited_once_with(coord._rlp_blend)
    else:
        rlp.assert_not_awaited()


async def test_an_old_feed_in_settled_on_the_solar_profile_loads_it_before_pricing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """energie.be's feed-in settles on the month's Belpex_SPP. Without the
    profile the walk credits the printed forecast (10,38 EUR for 300 kWh in
    April where the realised month gives 5,71), while the backfill of the
    same days loads it, so the sensor and the statistics disagreed. Loaded by
    the daily pricing only: the compare dialog never downloads 52 MB."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    held = _held("energiebe", "energiebe_fixed", solar_regime="injection")
    entry = make_entry(
        solar_regime="injection",
        previous_contracts=[{"until": "2026-05-01", "data": held}],
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.30))
    coord._historical_spots = {datetime(2026, 4, 1, 10, tzinfo=UTC): 0.02}
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    profile = {(4, 1, 12): 1.0}

    async def _load() -> None:
        coord._spp_weights = profile  # type: ignore[assignment]

    spp = AsyncMock(side_effect=_load)
    coord._ensure_spp_weights = spp  # type: ignore[method-assign]
    card = make_snapshot(
        energy=FixedRates(single=0.15),
        injection=InjectionRates(
            current=0.0346, factor=0.6, base=-0.008, spp_indexed=True
        ),
    )
    seen: list[Any] = []

    async def _cost(*args: Any, **kwargs: Any) -> float:
        seen.append(kwargs["spp_weights"])
        return 10.0

    async def _card(
        hass_: Any, session: Any, coordinator: Any, period: Any, overrides: Any = None
    ) -> Any:
        proxy = _QuoteEntry(data=dict(period.data), runtime_data=coordinator)
        return proxy, make_stub_extractor(), card, False, False, False

    periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    with (
        patch.object(contract_periods, "period_card", new=_card),
        patch.object(contract_periods, "_compute_current_year_cost", new=_cost),
    ):
        await contract_periods.price_previous_periods(
            hass,
            None,  # type: ignore[arg-type]
            coord,
            periods,
            month_start=date(2026, 9, 1),  # type: ignore[arg-type]
        )
        spp.assert_not_awaited()
        assert seen == [None]
        await coord._price_previous(periods, date(2026, 9, 24))
    spp.assert_awaited_once()
    assert seen[1:] == [profile]


async def test_pricing_closes_each_window_on_the_day_before_the_switch(
    hass: HomeAssistant, freezer: Any
) -> None:
    """And falls back to the entry's own card, flagged, for a supplier that can
    be reached no more and whose cards no archive kept."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    own_card = make_snapshot(energy=FixedRates(single=0.30))
    coordinator = SimpleNamespace(
        _snapshot=own_card,
        entry=make_entry(),
        _historical_spots={},
        _historical_spot_quarters={},
        _spp_weights={},
        _rlp_weights={},
        _billed_peak_kw=lambda: 0.0,
    )
    periods = [
        ContractPeriod(
            date(2026, 1, 1), date(2026, 2, 28), _held("engie", "engie_easy_fixed")
        ),
        ContractPeriod(
            date(2026, 3, 1), date(2026, 9, 14), _held("bolt", "bolt_variable")
        ),
    ]
    calls: list[dict[str, Any]] = []

    async def fake(*args: Any, **kwargs: Any) -> float:
        calls.append({"card": args[3], **kwargs})
        return 10.0

    with (
        patch.object(
            contract_periods,
            "_current_card",
            AsyncMock(return_value=(None, False, False)),
        ),
        patch.object(
            contract_periods,
            "_latest_archived_card",
            AsyncMock(return_value=(None, False)),
        ),
        patch.object(contract_periods, "_compute_current_year_cost", new=fake),
    ):
        rows = await contract_periods.price_previous_periods(
            hass,
            None,  # type: ignore[arg-type]
            coordinator,
            periods,
            month_start=date(2026, 9, 1),
        )
    assert [(r.supplier, r.cost, r.month_cost, r.stand_in) for r in rows] == [
        ("engie", 10.0, None, True),
        ("bolt", 10.0, 10.0, True),
    ]
    windows = [(c["window_start_override"], c["window_end"]) for c in calls]
    assert windows == [
        (date(2026, 1, 1), date(2026, 2, 28)),
        (date(2026, 3, 1), date(2026, 9, 14)),
        # Only the contract that reached into September has a month part.
        (date(2026, 9, 1), date(2026, 9, 14)),
    ]
    assert all(c["card"] is own_card for c in calls)


async def test_a_stand_in_card_grants_the_old_contract_no_welcome_credit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The entry's own card stands in for a supplier with no card and no
    archive (DATS 24 today), and it was walked with the OLD contract's start
    date, so the old contract was credited whatever the current card grants a
    new customer: 259 EUR of Mega ristourne on a DATS 24 year, plus the
    first-year feed-in bonus. The stand-in prices the days, not the offer."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    own = make_entry(
        consumption_kwh="sensor.cons",
        contract_start_date="2026-09-01",
        previous_contracts=[
            {
                "until": "2026-09-01",
                "data": _held(
                    "dats24",
                    "dats24_groen_variabel",
                    consumption_kwh="sensor.cons",
                    contract_start_date="2025-04-01",
                ),
            }
        ],
    )
    periods = previous_periods(own.data, date(2026, 1, 1), date(2026, 9, 24))

    async def _daily(_h: Any, eid: str, start: date, end: date) -> dict[date, float]:
        if eid != "sensor.cons":
            return {}
        return {start + timedelta(days=i): 10.0 for i in range((end - start).days + 1)}

    async def _priced_on(card: Any) -> float | None:
        coordinator = SimpleNamespace(
            _snapshot=card,
            entry=own,
            _historical_spots={},
            _historical_spot_quarters={},
            _spp_weights={},
            _rlp_weights={},
            _billed_peak_kw=lambda: 0.0,
        )
        with (
            patch.object(energy_meters, "_recorder_daily_kwh", new=_daily),
            patch.object(
                contract_periods,
                "_current_card",
                AsyncMock(return_value=(None, False, False)),
            ),
            patch.object(
                contract_periods,
                "_latest_archived_card",
                AsyncMock(return_value=(None, False)),
            ),
        ):
            rows = await contract_periods.price_previous_periods(
                hass,
                None,  # type: ignore[arg-type]
                coordinator,
                periods,
                month_start=date(2026, 9, 1),
            )
        assert rows[0].stand_in
        return rows[0].cost

    plain = make_snapshot(energy=FixedRates(single=0.30))
    offered = make_snapshot(
        energy=FixedRates(single=0.30),
        welcome_credit_eur=200.0,
        welcome_credit_after_months=12,
    )
    without = await _priced_on(plain)
    assert without is not None
    assert await _priced_on(offered) == pytest.approx(without)


async def test_an_old_card_published_as_page_images_prices_on_the_archive_reading(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Ecofix publishes its cards as page images, so every fetch of an earlier
    Ecofix contract's card raised and the period was priced on the NEW
    supplier's card, every day. The live tick and the compare page price such
    a card on the archive's OCR reading, and so does an earlier contract."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    ocr_card = make_snapshot(
        supplier="ecofix", contract="ecofix_flexy", energy=FixedRates(single=0.15)
    )
    coordinator = SimpleNamespace(
        _snapshot=make_snapshot(energy=FixedRates(single=0.25)),
        entry=make_entry(),
        _billed_peak_kw=lambda: 0.0,
    )
    period = ContractPeriod(
        date(2026, 1, 1), date(2026, 7, 31), _held("ecofix", "ecofix_flexy")
    )

    async def images(*_a: Any) -> Any:
        raise CardNotReadableError("Ecofix card has no text layer")

    asked: list[date] = []

    async def archive(*args: Any) -> ArchivedCard:
        asked.append(args[4])
        return ArchivedCard(snapshot=ocr_card, read_by_ocr=True)

    async def cost(*args: Any, **_k: Any) -> float:
        return round(args[3].energy.single * 2000, 2)

    ecofix = dataclasses.replace(EXTRACTORS["ecofix"], fetch=images)
    with (
        patch.object(contract_periods, "get_extractor", lambda _id: ecofix),
        patch.object(snapshot_months, "_archived_card_from_github", new=archive),
        patch.object(contract_periods, "_compute_current_year_cost", new=cost),
    ):
        rows = await contract_periods.price_previous_periods(
            hass,
            None,  # type: ignore[arg-type]
            coordinator,
            [period],
            month_start=date(2026, 9, 1),
        )
    assert asked == [date(2026, 9, 1)]
    assert (rows[0].cost, rows[0].stand_in, rows[0].read_by_ocr) == (300.0, False, True)
    priced = PricedPeriods(
        key=periods_key([period]),
        day=date(2026, 9, 24),
        month=date(2026, 9, 1),
        rows=(rows[0],),
    )
    assert previous_rows(priced, [period])[0]["card_read_by_ocr"] is True
    assert priced_from_dict(priced_to_dict(priced)) == priced


async def test_a_stand_in_keeps_the_pricing_on_its_own_cards_and_is_asked_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One timeout on the old supplier's site priced the period on the new
    supplier's card, and that was kept for the whole day and written over
    yesterday's pricing on its own cards in the store."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        previous_contracts=[
            {
                "until": "2026-08-01",
                "data": _held("totalenergies", "totalenergies_electricite_fixe"),
            }
        ]
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.25))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    today = date(2026, 9, 24)
    periods = previous_periods(entry.data, date(2026, 1, 1), today)
    own = PricedPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 7, 31),
        supplier="totalenergies",
        contract="totalenergies_electricite_fixe",
        cost=300.0,
        month_cost=None,
        stand_in=False,
    )
    stand_in = PricedPeriod(
        **{**own.__dict__, "cost": 500.0, "stand_in": True, "read_failed": True}
    )
    # Yesterday's pricing, as the store restores it after a restart.
    coord._previous_priced = PricedPeriods(
        key=periods_key(periods),
        day=date(2026, 9, 23),
        month=date(2026, 9, 1),
        rows=(own,),
    )
    with patch(f"{_COSTS}.price_previous_periods", AsyncMock(return_value=[stand_in])):
        coord._schedule_previous_pricing(periods, today)
        assert coord._previous_pricing is not None
        await coord._previous_pricing
    assert coord._previous_priced.rows == (own,)
    assert coord._previous_priced.day == today
    # With nothing better to keep, the stand-in is served, and asked again on
    # the next hourly tick rather than kept for the day.
    coord._previous_priced = PricedPeriods(
        key=periods_key(periods), day=today, month=date(2026, 9, 1), rows=(stand_in,)
    )
    coord._previous_pricing = None
    coord._previous_tried = None
    priced_again = AsyncMock()
    coord._price_previous = priced_again  # type: ignore[method-assign]
    coord._schedule_previous_pricing(periods, today)
    assert coord._previous_pricing is not None
    await coord._previous_pricing
    priced_again.assert_awaited_once()


async def test_a_stand_in_for_cards_no_archive_kept_is_priced_once_a_day(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A supplier that dropped the product and whose cards no archive kept
    cannot be priced on its own cards within the hour, and the stand-in was
    fetched and walked again every 50 minutes, for good. It settles for the
    day as the live sensor bills it, and still gives way to a pricing on its
    own cards."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        previous_contracts=[
            {
                "until": "2026-08-01",
                "data": _held("totalenergies", "totalenergies_electricite_fixe"),
            }
        ]
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=FixedRates(single=0.25))
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    today = date(2026, 9, 24)
    periods = previous_periods(entry.data, date(2026, 1, 1), today)
    stand_in = PricedPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 7, 31),
        supplier="totalenergies",
        contract="totalenergies_electricite_fixe",
        cost=500.0,
        month_cost=None,
        stand_in=True,
    )
    priced = AsyncMock(return_value=[stand_in])
    with patch(f"{_COSTS}.price_previous_periods", priced):
        coord._schedule_previous_pricing(periods, today)
        assert coord._previous_pricing is not None
        await coord._previous_pricing
        assert coord._previous_priced is not None
        assert coord._previous_priced.rows == (stand_in,)
        freezer.tick(timedelta(hours=1))
        coord._schedule_previous_pricing(periods, today)
        await coord._previous_pricing
        assert priced.await_count == 1
    own = PricedPeriod(**{**stand_in.__dict__, "cost": 300.0, "stand_in": False})
    assert keep_settled(
        PricedPeriods(key=periods_key(periods), day=today, month=today, rows=(own,)),
        periods_key(periods),
        [stand_in],
    ) == (own,)


async def test_the_backfill_bills_a_stand_in_as_the_live_sensor_does(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An earlier contract none of whose cards any archive kept was left out
    of the price series, the running bill with it, on every run: the anchor
    probe never found its row, so each restart ran the whole year again, and
    the cost series was never imported. Its days bill on the stand-in the live
    sensor shows, and the response says so. A read that failed just now is
    not a stand-in: the live sensor keeps what it last priced on the period's
    own cards, so those days are left out of every run, a service call's too,
    and the running bill is imported whole all the same."""
    freezer.move_to("2026-05-02 12:00:00+02:00")
    held = _held("totalenergies", "totalenergies_electricite_fixe", solar_regime="none")
    entry = make_entry(
        solar_regime="none", previous_contracts=[{"until": "2026-05-01", "data": held}]
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=f"switch_{key}",
            config_entry=entry,
        ).entity_id
        for key in ("current_price", "current_year_cost")
    }
    card = make_snapshot(energy=FixedRates(single=0.25))
    coordinator = SimpleNamespace(
        hass=hass,
        entry=entry,
        _snapshot=card,
        _session=None,
        _historical_spots={},
        _historical_spot_quarters={},
        _ensure_historical_spots=AsyncMock(),
        _billed_peak_kw=lambda: 0.0,
        _spot_prune_holds=0,
    )
    entry.runtime_data = coordinator
    proxy = _QuoteEntry(data=held, runtime_data=coordinator)
    captured: dict[str, list[Any]] = {}

    def fake_import(_h: Any, metadata: Any, stats: Any) -> None:
        captured[metadata["statistic_id"]] = list(stats)

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})

    async def run(read_failed: bool, retry_later: bool) -> dict[str, Any]:
        captured.clear()
        with (
            patch.object(bf, "BePricesCoordinator", SimpleNamespace),
            patch.object(
                backfill_window,
                "period_card",
                AsyncMock(
                    return_value=(
                        proxy,
                        make_stub_extractor(),
                        card,
                        True,
                        read_failed,
                        False,
                    )
                ),
            ),
            patch(
                "homeassistant.components.recorder.statistics.async_import_statistics",
                new=fake_import,
            ),
            patch(
                "homeassistant.components.recorder.get_instance", return_value=instance
            ),
        ):
            return await bf.backfill_range(
                hass,
                entry,
                datetime(2026, 4, 30, tzinfo=dt_util.get_default_time_zone()),
                datetime(2026, 5, 2, tzinfo=dt_util.get_default_time_zone()),
                retry_later=retry_later,
            )

    def days() -> set[date]:
        return {
            dt_util.as_local(r["start"]).date() for r in captured[ids["current_price"]]
        }

    # A lasting stand-in is imported at once, on either path.
    for retry_later in (False, True):
        result = await run(False, retry_later)
        assert days() == {date(2026, 4, 30), date(2026, 5, 1)}
        assert captured[ids["current_year_cost"]]
        assert "totalenergies" in result["stand_in"][0]
        assert "retry" not in result
        assert "skipped" not in result
    # A read that failed just now waits for a later run on either path, and a
    # service call is told to call again.
    for retry_later in (False, True):
        result = await run(True, retry_later)
        assert days() == {date(2026, 5, 1)}
        assert captured[ids["current_year_cost"]]
        assert "totalenergies" in result["retry"][0]
        assert ("call the service again" in result["retry"][0]) is not retry_later
        assert "stand_in" not in result


async def test_the_automatic_backfill_imports_the_year_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An earlier contract whose product is gone and whose cards no archive
    kept, and closed months the card archive cannot serve (GitHub unreachable
    from the house for a while). The cost series was never imported, and
    since the price series had no row at 1 January either, every restart ran
    the whole year twice. One run, a second once the archive answers again
    for the failed reads, and the year is in, as the live sensor bills it."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    held = _held("totalenergies", "totalenergies_pixel", solar_regime="none")
    entry = make_entry(
        solar_regime="none", previous_contracts=[{"until": "2026-07-01", "data": held}]
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    ids = {
        key: registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{entry.entry_id}_{key}",
            suggested_object_id=f"switch_{key}",
            config_entry=entry,
        ).entity_id
        for key in ("current_price", "current_year_cost")
    }
    entry.runtime_data = SimpleNamespace(
        hass=hass,
        entry=entry,
        _snapshot=make_snapshot(energy=FixedRates(single=0.25)),
        _session=None,
        _historical_spots={},
        _historical_spot_quarters={},
        _ensure_historical_spots=AsyncMock(),
        _billed_peak_kw=lambda: 0.0,
        _spot_prune_holds=0,
        _backfill_retry_from=None,
        _save_persistent=AsyncMock(),
    )

    async def gone(*_a: Any) -> Any:
        raise ExtractorError("HTTP 404 fetching the totalenergies card")

    up = False

    async def blocked(*_a: Any) -> Any:
        if not up:
            raise aiohttp.ClientConnectionError("Cannot connect to host")
        return ArchivedCard(
            snapshot=make_snapshot(energy=FixedRates(single=0.2)), read_by_ocr=False
        )

    real_sleep = asyncio.sleep

    async def answers_again(delay: float, *args: Any) -> None:
        # The retry's wait, past the half hour a failed month read is kept;
        # the day-by-day turns the passes hand the loop still pass.
        nonlocal up
        if delay < 60:
            await real_sleep(delay, *args)
            return
        up = True
        freezer.tick(timedelta(hours=1))

    extractors = {
        "totalenergies": dataclasses.replace(EXTRACTORS["totalenergies"], fetch=gone),
        "eneco": dataclasses.replace(
            EXTRACTORS["eneco"], fetch_for_month=AsyncMock(return_value=None)
        ),
    }
    store: dict[str, list[Any]] = {}

    def fake_import(_h: Any, metadata: Any, stats: Any) -> None:
        store.setdefault(metadata["statistic_id"], []).extend(stats)

    async def executor(
        _fn: Any, _h: Any, start: Any, end: Any, sids: Any, *_r: Any
    ) -> Any:
        sid = next(iter(sids))
        return {sid: [r for r in store.get(sid, []) if start <= r["start"] < end]}

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(side_effect=executor)
    runs = AsyncMock(side_effect=bf.backfill_range)
    with (
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch.object(contract_periods, "get_extractor", extractors.__getitem__),
        patch.object(backfill_window, "get_extractor", extractors.__getitem__),
        patch.object(snapshot_months, "_archived_card_from_github", new=blocked),
        patch.object(bf.asyncio, "sleep", answers_again),
        patch.object(bf, "backfill_range", runs),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=fake_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
    ):
        result = await bf.backfill_if_missing(hass, entry)
        assert runs.await_count == 2
        for _restart in range(2):
            assert await bf.backfill_if_missing(hass, entry) is None
    assert runs.await_count == 2
    assert result is not None
    assert "retry" not in result
    assert "totalenergies" in " ".join(result["stand_in"])
    days = {dt_util.as_local(r["start"]).date() for r in store[ids["current_price"]]}
    assert min(days) == date(2026, 1, 1)
    assert len(days) == (date(2026, 9, 26) - date(2026, 1, 1)).days + 1
    assert store[ids["current_year_cost"]]


async def test_a_stand_in_says_whether_a_read_failed_or_the_cards_are_gone(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A timeout on the old supplier's site may clear within the hour; a card
    withdrawn with no archive holding any month of the period never will."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    period = ContractPeriod(
        date(2026, 1, 1),
        date(2026, 6, 30),
        _held("totalenergies", "totalenergies_pixel"),
    )
    coordinator = SimpleNamespace(_snapshot=make_snapshot(), entry=make_entry())

    async def answer(message: str) -> tuple[bool, bool]:
        async def fetch(*_a: Any) -> Any:
            raise ExtractorError(message)

        te = dataclasses.replace(EXTRACTORS["totalenergies"], fetch=fetch)
        with patch.object(contract_periods, "get_extractor", lambda _id: te):
            got = await contract_periods.period_card(
                hass,
                None,  # type: ignore[arg-type]
                coordinator,
                period,
            )
        return got[3], got[4]

    assert await answer("network error fetching the card: timeout") == (True, True)
    assert await answer("HTTP 404 fetching the card") == (True, False)


async def test_the_compare_page_adds_the_earlier_contracts_under_its_what_if(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-24 12:00:00+02:00")
    entry = make_entry(
        previous_contracts=[
            {"until": "2026-06-15", "data": _held("engie", "engie_easy_fixed")}
        ]
    )
    period = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    row = PricedPeriod(
        start=date(2026, 1, 1),
        end=date(2026, 6, 14),
        supplier="engie",
        contract="engie_easy_fixed",
        cost=250.0,
        month_cost=None,
        stand_in=False,
    )
    coordinator = SimpleNamespace(
        _previous_priced=_priced(period, row, month=date(2026, 9, 1))
    )
    as_it_is = cast(ConfigEntry, _QuoteEntry(data=entry.data))
    fresh = AsyncMock(return_value=[PricedPeriod(**{**row.__dict__, "cost": 90.0})])
    with patch.object(contract_periods, "price_previous_periods", new=fresh):
        total = await with_previous_contracts(
            hass,
            None,  # type: ignore[arg-type]
            coordinator,
            entry,
            as_it_is,
            100.0,
            window_start=date(2026, 1, 1),
            today=date(2026, 9, 24),
        )
        assert total == pytest.approx(350.0)
        assert fresh.await_count == 0, "the household as it is reads the day's pricing"
        what_if = cast(
            ConfigEntry,
            _QuoteEntry(data={**entry.data, CONF_SOLAR_REGIME: "none"}),
        )
        entry_with_solar = make_entry(
            solar_regime="compensation",
            previous_contracts=entry.data[CONF_PREVIOUS_CONTRACTS],
        )
        total = await with_previous_contracts(
            hass,
            None,  # type: ignore[arg-type]
            coordinator,
            entry_with_solar,
            what_if,
            100.0,
            window_start=date(2026, 1, 1),
            today=date(2026, 9, 24),
        )
    assert total == pytest.approx(190.0)
    assert fresh.await_args is not None
    assert fresh.await_args.kwargs["overrides"] == {CONF_SOLAR_REGIME: "none"}


# The backfill.


async def test_the_backfilled_year_ends_on_what_the_two_contracts_cost_live(
    hass: HomeAssistant,
) -> None:
    """Each hour on the contract that supplied it, one running total across the
    switch, and the last row equal to the two live windows added up."""
    old_card = make_snapshot(
        energy=DynamicRates(factor=1.2, base=0.03, yearly_fixed_fee=60.0)
    )
    new_card = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.02, yearly_fixed_fee=48.0)
    )
    switch = date(2026, 2, 15)
    entry = make_entry(
        meter="dynamic",
        consumption_kwh="sensor.cons_total",
        previous_contracts=[
            {
                "until": switch.isoformat(),
                "data": _held(
                    "eneco",
                    "power_fix",
                    meter="dynamic",
                    consumption_kwh="sensor.cons_total",
                ),
            }
        ],
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_current_year_cost",
        suggested_object_id="switch_current_year_cost",
        config_entry=entry,
    )
    start = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    stop = (
        dt_util.start_of_local_day(date(2026, 3, 31)) + timedelta(days=1)
    ).astimezone(UTC)
    hours: list[datetime] = []
    when = start
    while when < stop:
        hours.append(when)
        when += timedelta(hours=1)
    spots = {h: 0.06 for h in hours}
    per_hour = {h: 0.4 for h in hours}
    coordinator = SimpleNamespace(
        hass=hass,
        entry=entry,
        _snapshot=new_card,
        _session=None,
        _historical_spots=dict(spots),
        _historical_spot_quarters={},
        _spp_weights={},
        _rlp_weights={},
        _ensure_historical_spots=AsyncMock(),
        _ensure_spp_weights=AsyncMock(),
        _ensure_rlp_weights=AsyncMock(),
        _billed_peak_kw=lambda: 0.0,
    )
    entry.runtime_data = coordinator
    old_entry = cast(
        ConfigEntry,
        _QuoteEntry(
            data=entry.data[CONF_PREVIOUS_CONTRACTS][0]["data"],
            runtime_data=coordinator,
        ),
    )

    async def fake_hourly(
        _h: Any, entity_id: str, first: date, last: date
    ) -> dict[datetime, float]:
        if entity_id != "sensor.cons_total":
            return {}
        return {
            h: kwh
            for h, kwh in per_hour.items()
            if first <= dt_util.as_local(h).date() <= last
        }

    def own_card(*args: Any, **_k: Any) -> Any:
        # Every month on the card of the contract being walked.
        return args[6]

    captured: list[list[dict[str, Any]]] = []

    def fake_import(_h: Any, _meta: Any, stats: Any) -> None:
        captured.append(list(stats))

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=fake_hourly),
        patch.object(ytd_energy, "_top_up_today_hourly", new=AsyncMock()),
        patch.object(
            cohort, "_effective_snapshot_for_month", new=AsyncMock(side_effect=own_card)
        ),
        patch.object(
            ytd_cost,
            "_effective_snapshot_for_month",
            new=AsyncMock(side_effect=own_card),
        ),
        patch.object(ytd_cost, "_cohort_energy_leg", AsyncMock(return_value=None)),
        patch.object(
            backfill_window,
            "period_card",
            AsyncMock(
                return_value=(
                    old_entry,
                    make_stub_extractor(),
                    old_card,
                    False,
                    False,
                    False,
                )
            ),
        ),
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=fake_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.util.dt.now",
            lambda: (
                dt_util.start_of_local_day(date(2026, 3, 31))
                + timedelta(hours=23, minutes=59)
            ),
        ),
    ):
        await bf._backfill_cost_sensor(
            hass,
            entry,
            coordinator,  # type: ignore[arg-type]
            hours,
            dict(spots),
            {},
        )
        old_live = await ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            old_card,
            old_entry,
            historical_spots=dict(spots),
            window_start_override=date(2026, 1, 1),
            window_end=switch - timedelta(days=1),
        )
        new_live = await ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            new_card,
            entry,
            historical_spots=dict(spots),
            window_start_override=switch,
        )
    rows = [row for batch in captured for row in batch]
    assert len(rows) == len(hours)
    assert old_live is not None and new_live is not None
    sums = [row["sum"] for row in rows]
    # One running total: nothing falls back at the switch.
    assert all(later >= earlier for earlier, later in zip(sums, sums[1:], strict=False))
    assert rows[-1]["sum"] == pytest.approx(old_live + new_live, abs=1e-3)


async def test_the_backfilled_cost_leaves_out_days_no_contract_supplied(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The first contract began on 1 March and billed its year from then. The
    sensor bills January and February to nobody, so the cost series must not
    put them on the entry's current contract either."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    before = dict(
        make_entry(contract_start_date="2026-03-01", ytd_from_contract_start=True).data
    )
    entry = MockConfigEntry(
        domain=DOMAIN, data=_record_switch(before, date(2026, 7, 1)), title="x"
    )
    start = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    hours = [start + timedelta(hours=h) for h in range(24 * 250)]
    stand_in = make_entry(supplier="bolt", contract="bolt_variable")
    card = make_snapshot()
    with patch.object(
        backfill_window,
        "period_card",
        AsyncMock(return_value=(stand_in, None, card, False, False, False)),
    ):
        billed = await backfill_window._contract_segments(
            hass,
            entry,
            SimpleNamespace(_session=None),  # type: ignore[arg-type]
            hours,
            billed_only=True,
        )
        every = await backfill_window._contract_segments(
            hass,
            entry,
            SimpleNamespace(_session=None),  # type: ignore[arg-type]
            hours,
        )

    def _first_day(segment: Any) -> date:
        return dt_util.as_local(segment[2][0]).date()

    assert [_first_day(s) for s in billed] == [date(2026, 3, 1), date(2026, 7, 1)]
    assert billed[0][1] is card and billed[1][1] is None
    # The price rows still cover every hour.
    assert _first_day(every[0]) == date(2026, 1, 1)
    assert sum(len(s[2]) for s in every) == len(hours)


def test_an_earlier_contract_takes_only_the_household_facts_it_left_blank() -> None:
    """The inverter's kVA entered when the Repairs card asked for it, after
    the switch was recorded, never reached the contract held before it, which
    billed no prosumer fee; nor did a day-ahead key taken out for the new
    contract. A value the earlier contract kept is its own, and so are the
    meter and direct debit, which a switch is often when they change."""
    held = _held(
        "mega",
        "mega_online_flex",
        solar_regime="compensation",
        solar_kva=0.0,
        direct_debit=False,
    )
    entry = make_entry(
        supplier="engie",
        contract="engie_dynamic",
        meter="bi",
        solar_regime="compensation",
        solar_kva=5.0,
        api_key="LIVEKEY",
        connection_kva_tier="le9_6",
        direct_debit=True,
        previous_contracts=[{"until": "2026-06-15", "data": held}],
    )
    [period] = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    assert period.data["solar_kva"] == 5.0
    assert period.data["api_key"] == "LIVEKEY"
    assert period.data["connection_kva_tier"] == "le9_6"
    assert period.data["direct_debit"] is False
    assert period.data["meter"] == "mono"
    # With the key the walk re-prices the old flex card on its month's mean,
    # so the old contract's spots are fetched.
    assert contract_periods.periods_need_spots([period])
    kept = {**held, "solar_kva": 3.0, "api_key": "OLDKEY"}
    entry = make_entry(
        solar_kva=5.0,
        api_key="LIVEKEY",
        previous_contracts=[{"until": "2026-06-15", "data": kept}],
    )
    [period] = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
    assert (period.data["solar_kva"], period.data["api_key"]) == (3.0, "OLDKEY")


async def test_an_earlier_mono_contract_stays_mono_after_the_entry_moves_to_bi(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The meter is the contract's own: moving to a day/night meter with the
    new supplier must not re-bill the months before it on two registers."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    held = _held("eneco", "power_fix", consumption_kwh="sensor.total")
    card = make_snapshot(energy=FixedRates(single=0.25, peak=0.40, offpeak=0.10))

    async def daily(_h: Any, entity_id: str, start: date, end: date) -> Any:
        out: dict[date, float] = {}
        day = start
        while day <= end and day < date(2026, 9, 24):
            out[day] = 10.0
            day += timedelta(days=1)
        return out

    async def cost_with(entry_meter: dict[str, Any]) -> float | None:
        entry = make_entry(
            previous_contracts=[{"until": "2026-06-15", "data": held}], **entry_meter
        )
        coordinator = SimpleNamespace(
            _snapshot=card,
            entry=entry,
            _historical_spots=None,
            _rlp_weights=None,
            _spp_weights=None,
            _billed_peak_kw=lambda: 0.0,
        )
        periods = previous_periods(entry.data, date(2026, 1, 1), date(2026, 9, 24))
        assert periods[0].data["meter"] == "mono"
        with (
            patch.object(energy_meters, "_recorder_daily_kwh", new=daily),
            patch.object(
                contract_periods,
                "_current_card",
                AsyncMock(return_value=(card, False, False)),
            ),
            patch.object(
                contract_periods, "get_extractor", lambda _s: make_stub_extractor()
            ),
        ):
            rows = await contract_periods.price_previous_periods(
                hass,
                None,  # type: ignore[arg-type]
                coordinator,
                periods,
                month_start=date(2026, 9, 1),
            )
        return rows[0].cost

    mono = await cost_with({"consumption_kwh": "sensor.total"})
    bi = await cost_with(
        {
            "meter": "bi",
            "day_consumption_kwh": "sensor.p1_t1",
            "night_consumption_kwh": "sensor.p1_t2",
        }
    )
    assert mono is not None and mono > 0
    assert bi == pytest.approx(mono)


@contextlib.contextmanager
def _spans(spans: dict[str, tuple[date, date]]) -> Iterator[None]:
    """Patch the daily and hourly readers with 5 kWh a day per sensor over
    its (first, last) days, nothing outside them."""

    async def daily(_h: Any, entity_id: str, start: date, end: date) -> Any:
        first, last = spans.get(entity_id, (end, start))
        out: dict[date, float] = {}
        day = max(start, first)
        while day <= min(end, last):
            out[day] = 5.0
            day += timedelta(days=1)
        return out

    async def hourly(_h: Any, entity_id: str, start: date, end: date) -> Any:
        return {
            datetime.combine(day, datetime.min.time(), UTC): 5.0
            for day in (await daily(_h, entity_id, start, end))
        }

    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=daily),
        patch.object(contract_periods, "_recorder_daily_kwh", new=daily),
        patch.object(energy_meters, "_recorder_hourly_kwh", new=hourly),
    ):
        yield


async def test_the_register_card_reads_the_entry_from_its_own_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A night register rewired at the switch: the earlier contract billed
    its own sensor until then and the current one the new sensor since, both
    whole. The entry's wiring was read from 1 January, over the earlier
    contract's days as well, and named the new register for not reporting
    before it was wired."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    held = _held(
        "engie",
        "engie_easy_fixed",
        meter="bi",
        day_consumption_kwh="sensor.t1",
        night_consumption_kwh="sensor.t2_old",
    )
    entry = make_entry(
        meter="bi",
        day_consumption_kwh="sensor.t1",
        night_consumption_kwh="sensor.t2",
        previous_contracts=[{"until": "2026-06-01", "data": held}],
    )
    yesterday = date(2026, 9, 23)
    coord = BePricesCoordinator(hass, entry)
    with _spans(
        {
            "sensor.t1": (date(2025, 1, 1), yesterday),
            "sensor.t2_old": (date(2025, 1, 1), date(2026, 5, 31)),
            "sensor.t2": (date(2026, 6, 1), yesterday),
        }
    ):
        await coord._find_meter_faults(date(2026, 9, 24))
    assert coord._register_pair_fault == ""


async def test_an_earlier_contract_names_only_the_meters_it_can_fix(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A dead night register made the earlier contract's pair record nothing,
    and the check for a meter that recorded nothing named its healthy day
    register beside it. A meter whose statistics begin after the earlier
    contract ended, one added to Home Assistant since, was named too, though
    no sensor holds those days and no rewiring clears the card, and beside a
    feed-in meter the comparison of the two sides named it again. A feed-in
    meter added since, because the panels came later, was named as the
    silent side; one that never recorded still is."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    yesterday = date(2026, 9, 23)
    pair = make_entry(
        meter="bi",
        day_consumption_kwh="sensor.t1",
        night_consumption_kwh="sensor.t2",
        previous_contracts=[
            {
                "until": "2026-06-01",
                "data": _held(
                    "engie",
                    "engie_easy_fixed",
                    meter="bi",
                    day_consumption_kwh="sensor.t1",
                    night_consumption_kwh="sensor.t2_old",
                ),
            }
        ],
    )
    younger = make_entry(
        consumption_kwh="sensor.total",
        previous_contracts=[
            {
                "until": "2026-06-01",
                "data": _held(
                    "engie", "engie_easy_fixed", consumption_kwh="sensor.total"
                ),
            }
        ],
    )
    solar: dict[str, Any] = {
        "consumption_kwh": "sensor.total",
        "injection_kwh": "sensor.inj",
        "solar_regime": "injection",
    }
    younger_solar = make_entry(
        **solar,
        previous_contracts=[
            {"until": "2026-06-01", "data": _held("engie", "engie_easy_fixed", **solar)}
        ],
    )

    def _feed_in(injection: str) -> Any:
        wiring: dict[str, Any] = {
            "consumption_kwh": "sensor.t1",
            "injection_kwh": injection,
            "solar_regime": "injection",
        }
        return make_entry(
            **wiring,
            previous_contracts=[
                {
                    "until": "2026-06-01",
                    "data": _held("engie", "engie_easy_fixed", **wiring),
                }
            ],
        )

    with _spans(
        {
            "sensor.t1": (date(2025, 1, 1), yesterday),
            "sensor.t2": (date(2026, 6, 1), yesterday),
            "sensor.total": (date(2026, 6, 5), yesterday),
            "sensor.inj": (date(2025, 1, 1), yesterday),
            "sensor.inj_new": (date(2026, 6, 5), yesterday),
        }
    ):
        assert await contract_periods.previous_meter_faults(
            hass, pair, date(2026, 9, 24)
        ) == ["sensor.t2_old (engie, 2026-01-01 to 2026-05-31)"]
        assert (
            await contract_periods.previous_meter_faults(
                hass, younger, date(2026, 9, 24)
            )
            == []
        )
        assert (
            await contract_periods.previous_meter_faults(
                hass, younger_solar, date(2026, 9, 24)
            )
            == []
        )
        assert (
            await contract_periods.previous_meter_faults(
                hass, _feed_in("sensor.inj_new"), date(2026, 9, 24)
            )
            == []
        )
        assert await contract_periods.previous_meter_faults(
            hass, _feed_in("sensor.inj_dead"), date(2026, 9, 24)
        ) == ["sensor.inj_dead (engie, 2026-01-01 to 2026-05-31)"]


async def test_an_earlier_contracts_dead_meters_raise_the_register_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A rename moves the statistics to the new sensor, so the earlier
    contract's own sensors record nothing and it was billed its fees alone,
    with no card: the register checks read the entry's wiring only."""
    freezer.move_to("2026-09-24 12:00:00+02:00")
    new = ("sensor.p1_t1", "sensor.p1_t2")
    old = ("sensor.old_t1", "sensor.old_t2")
    held = _held(
        "engie",
        "engie_easy_fixed",
        meter="bi",
        day_consumption_kwh=old[0],
        night_consumption_kwh=old[1],
    )
    entry = make_entry(
        meter="bi",
        day_consumption_kwh=new[0],
        night_consumption_kwh=new[1],
        previous_contracts=[{"until": "2026-06-15", "data": held}],
    )
    live = set(new)

    async def daily(_h: Any, entity_id: str, start: date, end: date) -> Any:
        if entity_id not in live:
            return {}
        out: dict[date, float] = {}
        day = start
        while day <= end and day < date(2026, 9, 24):
            out[day] = 5.0
            day += timedelta(days=1)
        return out

    async def hourly(_h: Any, entity_id: str, start: date, end: date) -> Any:
        if entity_id not in live:
            return {}
        return {
            datetime.combine(day, datetime.min.time(), UTC): 5.0
            for day in (await daily(_h, entity_id, start, end))
        }

    with (
        patch.object(energy_meters, "_recorder_daily_kwh", new=daily),
        patch.object(energy_meters, "_recorder_hourly_kwh", new=hourly),
    ):
        faults = await contract_periods.previous_meter_faults(
            hass, entry, date(2026, 9, 24)
        )
        assert faults == [
            "sensor.old_t1, sensor.old_t2 (engie, 2026-01-01 to 2026-06-14)"
        ]
        # The period keeps its own sensors; they only need to report.
        live |= set(old)
        assert (
            await contract_periods.previous_meter_faults(hass, entry, date(2026, 9, 24))
            == []
        )
        coord = BePricesCoordinator(hass, entry)
        live -= set(old)
        await coord._ensure_annual_volume()
    assert "sensor.old_t1" in coord._register_pair_fault
