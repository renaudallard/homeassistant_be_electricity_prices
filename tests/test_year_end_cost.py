"""The calendar year's bill as it will stand on 31 December: the year-to-date
walk run to the year's end over last year's same days (``year_end_cost.py``,
``year_ahead.py``)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import cohort, energy_meters, ytd_cost
from custom_components.be_electricity_prices.injection import _bake_monthly_injection
from custom_components.be_electricity_prices.pricing import static_breakdown
from custom_components.be_electricity_prices.projected_volume import (
    _compute_projected_year_kwh,
)
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.year_ahead import YearAhead
from custom_components.be_electricity_prices.year_end_cost import (
    _compute_year_end_cost,
)
from tests import make_entry, make_snapshot, make_stub_extractor

_PerDay = Callable[[str, date], float | None]


def _hours_of(day: date) -> list[datetime]:
    """The UTC starts of a local day's hours: 23, 24 or 25 of them."""
    when = dt_util.start_of_local_day(day).astimezone(UTC)
    stop = dt_util.start_of_local_day(day + timedelta(days=1)).astimezone(UTC)
    out = []
    while when < stop:
        out.append(when)
        when += timedelta(hours=1)
    return out


def _recorder(per_day: _PerDay) -> Any:
    """``_read_deltas`` fake: ``per_day`` gives a day's kWh, spread evenly
    over its hours for an hourly read, ``None`` for no bucket."""

    async def _fake(
        _hass: object, entity_id: str, start: date, end: date, period: str
    ) -> list[tuple[datetime, float]]:
        out: list[tuple[datetime, float]] = []
        day = start
        while day <= end:
            kwh = per_day(entity_id, day)
            if kwh is not None:
                if period == "day":
                    out.append((dt_util.start_of_local_day(day).astimezone(UTC), kwh))
                else:
                    hours = _hours_of(day)
                    out.extend((h, kwh / len(hours)) for h in hours)
            day += timedelta(days=1)
        return out

    return _fake


def _as_if_recorded(per_day: _PerDay, today: date) -> _PerDay:
    """The same readings with last year's remaining days already recorded
    under this year's dates: what the recorder will hold on 31 December if
    the rest of the year repeats the last."""

    def _read(entity_id: str, day: date) -> float | None:
        if day.year == today.year and day >= today:
            return per_day(entity_id, day.replace(year=day.year - 1))
        return per_day(entity_id, day)

    return _read


async def _year_end(
    hass: HomeAssistant,
    entry: Any,
    snap: Any,
    per_day: _PerDay,
    *,
    card: Any = None,
    **kw: Any,
) -> tuple[float | None, dict[str, Any]]:
    diag: dict[str, Any] = {}
    kw.setdefault("energy_index", None)
    kw.setdefault("previous_eur", 0.0)
    with patch.object(energy_meters, "_read_deltas", new=_recorder(per_day)):
        got = await _compute_year_end_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            snap if card is None else card,
            dt_util.now().date(),
            breakdown=diag,
            cached_only=True,
            **kw,
        )
    return got, diag


async def _oracle(
    hass: HomeAssistant, entry: Any, snap: Any, per_day: _PerDay, **kw: Any
) -> tuple[float | None, dict[str, float]]:
    """The plain year-to-date walk to 31 December over
    :func:`_as_if_recorded`, with no read ahead."""
    stats: dict[str, float] = {}
    today = dt_util.now().date()
    with patch.object(
        energy_meters, "_read_deltas", new=_recorder(_as_if_recorded(per_day, today))
    ):
        got = await ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            breakdown=stats,
            cached_only=True,
            window_end=date(today.year, 12, 31),
            **kw,
        )
    return got, stats


def _seasonal(entity_id: str, day: date) -> float | None:
    """Consumption heavier in winter, feed-in heavier in summer, and a year
    apart different enough that reading the wrong one shows."""
    winter = day.month in (1, 2, 11, 12)
    summer = day.month in (5, 6, 7, 8)
    bump = 1.0 if day.year == 2025 else 0.0
    if entity_id == "sensor.cons":
        return (14.0 if winter else 9.0) + bump + day.day % 3
    if entity_id == "sensor.inj":
        return (22.0 if summer else 4.0) + bump
    return None


# The recorder read.


async def test_days_from_the_pivot_are_last_years_same_days(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    ahead = YearAhead(date(2026, 9, 26), make_snapshot())
    with (
        patch.object(energy_meters, "_read_deltas", new=_recorder(_seasonal)),
        energy_meters.reading_year_ahead(ahead),
    ):
        got = await energy_meters._recorder_daily_kwh(
            hass, "sensor.cons", date(2026, 9, 20), date(2026, 12, 31)
        )
    assert len(got) == 103
    assert got[date(2026, 9, 25)] == _seasonal("sensor.cons", date(2026, 9, 25))
    for day in (date(2026, 9, 26), date(2026, 11, 7), date(2026, 12, 31)):
        assert got[day] == _seasonal("sensor.cons", day.replace(year=2025))


async def test_no_live_reading_on_the_last_day_read_ahead(
    hass: HomeAssistant, freezer: Any
) -> None:
    """On 31 December the window ends today, and today is last year's too."""
    freezer.move_to("2026-12-31 12:00:00+01:00")
    live = AsyncMock(return_value=99.0)
    with (
        patch.object(energy_meters, "_read_deltas", new=_recorder(_seasonal)),
        patch.object(energy_meters, "_live_today_kwh", new=live),
        energy_meters.reading_year_ahead(
            YearAhead(date(2026, 12, 31), make_snapshot())
        ),
    ):
        got = await energy_meters._recorder_daily_kwh(
            hass, "sensor.cons", date(2026, 12, 1), date(2026, 12, 31)
        )
    live.assert_not_awaited()
    assert got[date(2026, 12, 31)] == _seasonal("sensor.cons", date(2025, 12, 31))


async def test_an_hour_shifted_onto_a_shorter_day_keeps_its_energy(
    hass: HomeAssistant, freezer: Any
) -> None:
    """26 October 2025 had 25 hours and 26 October 2026 has 24: the repeated
    hour lands on one slot, summed rather than overwritten."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    with (
        patch.object(energy_meters, "_read_deltas", new=_recorder(_seasonal)),
        energy_meters.reading_year_ahead(YearAhead(date(2026, 9, 26), make_snapshot())),
    ):
        got = await energy_meters._recorder_hourly_kwh(
            hass, "sensor.cons", date(2026, 10, 26), date(2026, 10, 26)
        )
    assert len(got) == 24
    assert sum(got.values()) == pytest.approx(
        _seasonal("sensor.cons", date(2025, 10, 26))
    )
    local = {dt_util.as_local(h).hour for h in got}
    assert local == set(range(24))


async def test_the_leap_day_reads_the_28th(hass: HomeAssistant, freezer: Any) -> None:
    freezer.move_to("2028-02-20 12:00:00+01:00")
    with (
        patch.object(energy_meters, "_read_deltas", new=_recorder(_seasonal)),
        energy_meters.reading_year_ahead(YearAhead(date(2028, 2, 20), make_snapshot())),
    ):
        got = await energy_meters._recorder_daily_kwh(
            hass, "sensor.cons", date(2028, 2, 27), date(2028, 3, 1)
        )
    assert got[date(2028, 2, 29)] == _seasonal("sensor.cons", date(2027, 2, 28))
    assert got[date(2028, 3, 1)] == _seasonal("sensor.cons", date(2027, 3, 1))


async def test_the_months_after_the_pivot_bill_on_todays_card(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing is fetched for a month that has no card yet; the running month
    and those before it are looked up as ever."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    today_card = make_snapshot(energy=FixedRates(single=0.31))
    lookup = AsyncMock(return_value=make_snapshot())
    with (
        patch.object(cohort, "_snapshot_for_month", new=lookup),
        energy_meters.reading_year_ahead(YearAhead(date(2026, 9, 26), today_card)),
    ):
        for month in (10, 11, 12):
            got = await cohort._effective_snapshot_for_month(
                hass,
                None,  # type: ignore[arg-type]
                make_stub_extractor(),
                "test",
                "wallonia",
                date(2026, month, 1),
                make_snapshot(),
                make_entry(),
            )
            assert got is today_card
        lookup.assert_not_awaited()
        await cohort._effective_snapshot_for_month(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            "test",
            "wallonia",
            date(2026, 9, 1),
            make_snapshot(),
            make_entry(),
        )
    lookup.assert_awaited_once()


# The bill.


@pytest.mark.parametrize(
    ("regime", "meter"),
    [("none", "mono"), ("injection", "bi"), ("compensation", "bi")],
)
async def test_the_year_end_is_the_walk_over_the_year_to_come(
    hass: HomeAssistant, freezer: Any, regime: str, meter: str
) -> None:
    """Exactly the bill the year-to-date walk gives once the recorder holds
    last year's remaining days under this year's dates, fees and netting
    included, and over the kWh the volume projection projects.

    From November, with no clock change ahead: the fake below spreads a day
    evenly over its hours, which on a 25-hour day is not where last year's
    hours land (see the shorter-day test above), so a bi-hourly split would
    differ by a few tenths of a cent for a reason that is the fake's."""
    freezer.move_to("2026-11-02 12:00:00+01:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.25, peak=0.28, offpeak=0.21, yearly_fixed_fee=60.0),
        injection=InjectionRates(current=0.04),
    )
    entry = make_entry(
        meter=meter,
        solar_regime=regime,
        solar_kva=4.0,
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
    )
    got, diag = await _year_end(hass, entry, snap, _seasonal)
    want, stats = await _oracle(hass, entry, snap, _seasonal)
    assert got is not None and want is not None
    assert got == pytest.approx(want, abs=1e-9)
    assert diag["consumption_kwh"] == pytest.approx(stats["consumption_ytd_kwh"])
    projected: dict[str, Any] = {}
    with patch.object(energy_meters, "_read_deltas", new=_recorder(_seasonal)):
        kwh = await _compute_projected_year_kwh(
            hass, entry, date(2026, 11, 2), side="consumption", breakdown=projected
        )
    assert diag["consumption_kwh"] == pytest.approx(kwh)
    assert diag["volume_basis"] == "metered to 2026-11-01, then last year's same days"


async def test_on_1_january_nothing_is_metered_yet(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No day of the new year has closed, so the basis names no metered end:
    last year's 31 December is not part of this year's bill."""
    freezer.move_to("2027-01-01 12:00:00+01:00")
    snap = make_snapshot(energy=FixedRates(single=0.25))
    got, diag = await _year_end(
        hass, make_entry(consumption_kwh="sensor.cons"), snap, _seasonal
    )
    assert got is not None, diag
    assert diag["volume_basis"] == (
        "nothing metered yet this year, last year's same days"
    )


async def test_the_contract_end_is_measured_against_the_calendar_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The year-end prices to 31 December, so the days its contract basis
    counts are the ones left this year, where the rolling year cost counts
    the 365 days it prices."""
    freezer.move_to("2026-11-02 12:00:00+01:00")
    snap = make_snapshot(energy=FixedRates(single=0.25))
    for end, want in (
        ("2026-11-30", "28 of the 59 days left this year (47%)"),
        ("2027-02-01", "runs past this year (ends 2027-02-01)"),
    ):
        entry = make_entry(consumption_kwh="sensor.cons", contract_end_date=end)
        got, diag = await _year_end(hass, entry, snap, _seasonal)
        assert got is not None, diag
        assert want in diag["contract_basis"]


async def test_a_day_the_recorder_lost_shows_in_the_coverage(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Ten days of this year with no bucket are billed without their energy,
    as current_year_cost bills them, and the year-end says so the way the
    year to date does: days seen and priced against the days walked."""
    freezer.move_to("2026-11-02 12:00:00+01:00")
    snap = make_snapshot(energy=FixedRates(single=0.25))
    entry = make_entry(consumption_kwh="sensor.cons")

    def gapped(entity_id: str, day: date) -> float | None:
        if date(2026, 2, 1) <= day <= date(2026, 2, 10):
            return None
        return _seasonal(entity_id, day)

    got, diag = await _year_end(hass, entry, snap, gapped)
    assert got is not None, diag
    assert diag["days_elapsed"] == 365.0
    assert diag["days_seen"] == 355.0
    assert diag["days_priced"] == 355.0


async def test_a_summer_surplus_is_spent_on_the_winter_after_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Netted once over the year and floored once: not the floored year to date
    plus the rest billed in full."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    snap = make_snapshot(energy=FixedRates(single=0.30))
    entry = make_entry(
        solar_regime="compensation",
        solar_kva=4.0,
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
    )

    def surplus(entity_id: str, day: date) -> float | None:
        ahead = day.year == 2025 and day >= date(2025, 9, 26)
        if entity_id == "sensor.cons":
            return 12.0 if ahead else 5.0
        if entity_id == "sensor.inj":
            return 0.0 if ahead else 8.0
        return None

    got, diag = await _year_end(hass, entry, snap, surplus)
    stats: dict[str, float] = {}
    with patch.object(energy_meters, "_read_deltas", new=_recorder(surplus)):
        ytd = await ytd_cost._compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            make_stub_extractor(),
            snap,
            entry,
            breakdown=stats,
            cached_only=True,
            window_end=date(2026, 9, 25),
        )
    assert got is not None and ytd is not None
    all_in = static_breakdown(snap, "ores", "wallonia", "single", "bi_horaire")
    assert all_in is not None
    # 268 days banking 3 kWh each, 97 days drawing 12: the year nets 360 kWh.
    assert got == pytest.approx(diag["fees_eur"] + 360.0 * all_in.all_in)
    # The year to date alone rests on its fees, its surplus forfeit.
    assert ytd == pytest.approx(stats["fees_ytd_eur"])


async def test_a_month_indexed_card_holds_this_months_index(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The months after the running one bill at the index handed in, the way
    a variable card holds its printed rate; the running month bills its own."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    snap = make_snapshot(energy=SpotMonthlyRates(factor=1.1, base=0.02))
    entry = make_entry(meter="dynamic", consumption_kwh="sensor.cons")
    september = {h: 0.08 for d in range(1, 31) for h in _hours_of(date(2026, 9, d))}

    async def run(index: float) -> float:
        got, diag = await _year_end(
            hass,
            entry,
            snap,
            _seasonal,
            energy_index=index,
            historical_spots=september,
        )
        assert got is not None, diag
        return got

    low, high = await run(0.05), await run(0.15)
    later = sum(
        _seasonal("sensor.cons", date(2025, 10, 1) + timedelta(days=d)) or 0.0
        for d in range(92)
    )
    per_kwh = (high - low) / later
    # 1,1 x 0,10 EUR/kWh of index; the card's formula is VAT-inclusive.
    assert per_kwh == pytest.approx(0.11, rel=1e-6)


async def test_no_index_for_the_running_month_is_no_number(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-01 01:00:00+02:00")
    snap = make_snapshot(energy=SpotMonthlyRates(factor=1.1, base=0.02))
    got, diag = await _year_end(
        hass, make_entry(consumption_kwh="sensor.cons"), snap, _seasonal
    )
    assert got is None
    assert diag["energy_basis"] == "not projected: this month's index is not known yet"
    quarterly = make_snapshot(
        energy=SpotMonthlyRates(factor=1.1, base=0.02, quarter_indexed=True)
    )
    _, diag = await _year_end(
        hass, make_entry(consumption_kwh="sensor.cons"), quarterly, _seasonal
    )
    assert (
        diag["energy_basis"] == "not projected: this quarter's index is not known yet"
    )


async def test_a_dynamic_contract_is_not_projected(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    snap = make_snapshot(energy=DynamicRates(factor=1.0, base=0.02))
    got, diag = await _year_end(
        hass, make_entry(consumption_kwh="sensor.cons"), snap, _seasonal
    )
    assert got is None
    assert diag["energy_basis"].startswith("not projected: a dynamic contract")


async def test_a_feed_in_credit_per_slot_is_not_projected(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.25),
        injection=InjectionRates(factor=0.9, base=-0.01),
    )
    entry = make_entry(
        solar_regime="injection",
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
    )
    got, diag = await _year_end(hass, entry, snap, _seasonal)
    assert got is None
    assert "follows the spot price per slot" in diag["energy_basis"]


async def test_a_feed_in_credit_with_no_rate_yet_is_not_projected(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A month-indexed credit that prints no figure, baked with no index: the
    live injection_price has none either, and the months ahead would bill
    the year with no feed-in."""
    freezer.move_to("2026-09-26 12:00:00+02:00")
    snap = make_snapshot(
        energy=FixedRates(single=0.25),
        injection=InjectionRates(factor=0.9, base=-0.01, month_indexed=True),
    )
    entry = make_entry(
        solar_regime="injection",
        consumption_kwh="sensor.cons",
        injection_kwh="sensor.inj",
    )
    got, diag = await _year_end(
        hass, entry, snap, _seasonal, card=_bake_monthly_injection(snap, None)
    )
    assert got is None
    assert diag["energy_basis"] == (
        "not projected: the feed-in credit has no rate this month yet"
    )
    got, diag = await _year_end(
        hass, entry, snap, _seasonal, card=_bake_monthly_injection(snap, 0.07)
    )
    assert got is not None, diag


async def test_without_last_years_days_there_is_no_number(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A meter wired this spring has no autumn to repeat."""
    freezer.move_to("2026-09-26 12:00:00+02:00")

    def spring(entity_id: str, day: date) -> float | None:
        return _seasonal(entity_id, day) if day >= date(2026, 3, 1) else None

    got, diag = await _year_end(
        hass, make_entry(consumption_kwh="sensor.cons"), make_snapshot(), spring
    )
    assert got is None
    assert diag["volume_basis"] == (
        "not projected: the consumption meter's history does not cover "
        "2025-09-26 to 2025-12-31"
    )


async def test_the_earlier_contracts_are_added(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-26 12:00:00+02:00")
    entry = make_entry(consumption_kwh="sensor.cons")
    alone, _ = await _year_end(hass, entry, make_snapshot(), _seasonal)
    got, diag = await _year_end(
        hass, entry, make_snapshot(), _seasonal, previous_eur=123.0
    )
    assert alone is not None and got == pytest.approx(alone + 123.0)
    assert diag["previous_contracts_eur"] == 123.0


def test_the_year_end_cost_is_always_created() -> None:
    from tests.test_bi_hourly_sensors import _added

    assert "projected_year_end_cost" in _added(make_entry())


async def test_the_tick_walks_the_year_end_only_when_its_inputs_move(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Reused while nothing it reads changed; a new card, a new day and the
    first tick after 01:00, when yesterday's last hour has compiled, each
    walk again. The first tick after a start prices on the month cards
    already held, and the second walks again once they may have filled."""
    from custom_components.be_electricity_prices import coordinator_costs
    from custom_components.be_electricity_prices.coordinator import (
        BePricesCoordinator,
    )

    freezer.move_to("2026-09-26 12:30:00+02:00")
    entry = make_entry(consumption_kwh="sensor.cons")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(supplier="eneco", contract="power_fix")
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(return_value={})  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    walk = AsyncMock(return_value=812.5)

    async def tick() -> float | None:
        data = await coord._async_update_data()
        return data.year_end_cost_eur

    with (
        patch(
            "custom_components.be_electricity_prices.coordinator_costs."
            "_compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coordinator_costs, "_compute_year_end_cost", walk),
    ):
        await tick()
        freezer.tick(timedelta(minutes=5))
        assert await tick() == 812.5
        assert walk.await_count == 2
        freezer.tick(timedelta(hours=1))
        assert await tick() == 812.5
        assert walk.await_count == 2
        coord._snapshot = make_snapshot(
            supplier="eneco", contract="power_fix", energy=FixedRates(single=0.31)
        )
        await tick()
        assert walk.await_count == 3
        freezer.move_to("2026-09-27 00:20:00+02:00")
        await tick()
        assert walk.await_count == 4
        freezer.move_to("2026-09-27 01:20:00+02:00")
        await tick()
        freezer.move_to("2026-09-27 02:20:00+02:00")
        assert await tick() == 812.5
        assert walk.await_count == 5
