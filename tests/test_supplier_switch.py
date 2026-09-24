"""A supplier switch recorded on the entry: the contracts held earlier in the
year, the days each covers and what it cost (``contract_periods.py``), the
window end that prices them, the options step that records one and the
backfill that splits at it."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import (
    backfill_window,
    cohort,
    contract_periods,
    energy_meters,
    ytd_cost,
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
    periods_key,
    previous_costs,
    previous_periods,
    priced_from_dict,
    priced_to_dict,
    with_previous_contracts,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.flow_schemas import (
    _record_switch,
    _validate_switch_date,
)
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
)
from tests import make_entry, make_snapshot, make_stub_extractor

_TICK = "custom_components.be_electricity_prices.coordinator_tick"


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
        patch(f"{_TICK}._compute_current_year_cost", new=own),
        patch(f"{_TICK}.price_previous_periods", new=pricing),
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
    with patch(f"{_TICK}.price_previous_periods", AsyncMock(return_value=[])):
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
    with patch(f"{_TICK}.price_previous_periods", AsyncMock(return_value=[])):
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
    with patch(f"{_TICK}.price_previous_periods", AsyncMock(return_value=[])):
        await coord._price_previous(periods, date(2026, 9, 24))
    if loaded:
        rlp.assert_awaited_once_with(coord._rlp_blend)
    else:
        rlp.assert_not_awaited()


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
        patch.object(contract_periods, "_current_card", AsyncMock(return_value=None)),
        patch.object(
            contract_periods, "_latest_archived_card", AsyncMock(return_value=None)
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
            AsyncMock(return_value=(old_entry, make_stub_extractor(), old_card, False)),
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
