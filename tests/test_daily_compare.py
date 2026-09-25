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

import inspect
from datetime import date, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.compare_table import (
    DailyCompare,
    RankedRow,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.pricing import MeterType
from custom_components.be_electricity_prices.coordinator_data import CoordinatorData
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
    return SimpleNamespace(
        data=CoordinatorData(),
        entry=entry,
        daily_compare=result,
        intended_unique_ids={},
    )


def _coord_with_spots(spots: dict[datetime, float]) -> SimpleNamespace:
    """The only thing the year-to-date pass asks a coordinator for."""
    return SimpleNamespace(
        _historical_spots=spots,
        _historical_spot_quarters={},
    )


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


async def test_the_own_row_year_to_date_is_priced_on_the_raw_card(
    hass: HomeAssistant,
) -> None:
    """The page's own row must answer the same question as the sensor.

    ``_compute_current_year_cost`` re-resolves the cohort itself, so the page
    handed it the already-spliced card and called that idempotent. It is not:
    the splice has turned a spot-monthly leg into a variable one by then, the
    month-indexed re-price finds nothing to do, and every past month falls
    back to the figure its card printed, which is the PREVIOUS month's index.
    The coordinator hands the same function ``coord._snapshot``, so the page
    hands it the raw card too.
    """
    from custom_components.be_electricity_prices import compare_engine

    entry = MockConfigEntry(domain=DOMAIN, data={"supplier": "eneco", "contract": "x"})
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    spliced = object()
    raw = object()
    sweep = {
        "region": "wallonia",
        "rows": [RankedRow(label="Eneco Zon & Wind Flex", annual=1272.75, is_own=True)],
        "labels": {},
        "household": SimpleNamespace(
            today_local=dt_util.now().date(),
            ytd_from=date(dt_util.now().year, 1, 1),
            current_snapshot=spliced,
            raw_snapshot=raw,
            quote_entry=entry,
            peak_kw=4.0,
        ),
    }
    seen: list[object] = []

    async def _capture(_hass, _session, _extractor, snapshot, *a, **k):  # type: ignore[no-untyped-def]
        seen.append(snapshot)
        return 708.11

    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _capture,
        ),
        patch.object(
            compare_engine, "_coordinator_rlp_weights", lambda *_a, **_k: None
        ),
        patch.object(
            compare_engine, "_coordinator_rlp_index_weights", lambda *_a, **_k: None
        ),
    ):
        await engine.fill_ytd_column(sweep, None)

    assert seen, "the own row's year-to-date was never computed"
    assert seen[0] is raw
    assert seen[0] is not spliced


async def test_the_own_row_carries_the_year_to_date_it_is_compared_against(
    hass: HomeAssistant,
) -> None:
    """Every other year-to-date figure is only readable against this one.

    The own row never carried it: the candidate list drops the household's own
    contract by design, so its label is absent from the label map the pass
    looks rows up in, and it fell through the same branch as a row that could
    not be priced. The number was being computed at the top of the pass, to
    warm the month cache, and thrown away.
    """
    from custom_components.be_electricity_prices import compare_engine

    entry = MockConfigEntry(domain=DOMAIN, data={"supplier": "eneco", "contract": "x"})
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    own = RankedRow(label="Eneco Zon & Wind Flex", annual=1272.75, is_own=True)
    other = RankedRow(label="Mega Online Fixed", annual=1102.75)
    sweep = {
        "region": "wallonia",
        "rows": [own, other],
        # The own label is deliberately NOT here, exactly as build_sweep
        # leaves it: that is the condition the bug lived in.
        "labels": {"Mega Online Fixed": ("mega", "online_fixed")},
        "household": SimpleNamespace(
            today_local=dt_util.now().date(),
            ytd_from=date(dt_util.now().year, 1, 1),
            current_snapshot=object(),
            raw_snapshot=object(),
            quote_entry=entry,
            peak_kw=4.0,
        ),
    }

    # The pass imports these inside the function, so they patch at the source.
    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            AsyncMock(return_value=708.11),
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            ".archived_months_present",
            return_value=[],
        ),
        patch.object(compare_engine, "get_extractor", return_value=object()),
        patch.object(compare_engine, "_sweep_rows", return_value={}),
    ):
        rows = await engine.fill_ytd_column(sweep, _coord_with_spots({}))

    by_label = {r.label: r for r in rows}
    assert by_label["Eneco Zon & Wind Flex"].ytd == 708.11
    # The alternative still answers to the coverage gate, which an empty
    # baseline closes: no archived months, no figure.
    assert by_label["Mega Online Fixed"].ytd is None


async def test_the_pass_prices_a_row_on_the_same_target_side_as_its_annual_figure(
    hass: HomeAssistant,
) -> None:
    """The annual row quotes a candidate on the meter its kind forces, on
    Tarif Impact mode for a Tarif Impact card, and on the card resolved per
    entry. The year-to-date pass handed the engine the household's own proxy
    entry and the raw card instead, so a Tarif Impact row was billed on the
    bi-horaire columns plus the terme fixe the tariff does not charge, and
    an ex-VAT card's running month carried fees short of VAT: one row, two
    answers, in a column the table sorts.
    """
    from custom_components.be_electricity_prices import compare_engine
    from custom_components.be_electricity_prices.providers.base import TaxOverlay
    from custom_components.be_electricity_prices.providers._rates import ImpactRates
    from custom_components.be_electricity_prices.snapshot_resolve import (
        _resolve_snapshot,
    )
    from tests import make_snapshot

    seen: dict[str, Any] = {}

    async def _capture(
        hass_: Any, session: Any, ext: Any, snap: Any, entry: Any, **kw: Any
    ) -> float:
        seen["snapshot"] = snap
        seen["entry"] = entry
        seen.update(kw)
        return 2386.52

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "luminus",
            "contract": "luminus_smartflex",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "dso_tariff_mode": "bi_horaire",
            "solar_regime": "none",
        },
    )
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    # A card printed ex-VAT, so resolving it per entry changes its figures.
    raw = make_snapshot(
        supplier="octaplus",
        contract="octaplus_fixed_impact",
        energy=ImpactRates(pic=0.30, medium=0.20, eco=0.10),
        taxes=TaxOverlay(federal_excise=0.04, energy_contribution=0.0, vat_rate=0.21),
    )
    household = SimpleNamespace(
        today_local=dt_util.now().date(),
        ytd_from=date(dt_util.now().year, 1, 1),
        current_snapshot=object(),
        raw_snapshot=object(),
        quote_entry=entry,
        peak_kw=4.0,
        current_meter="dynamic",
        dso_mode="bi_horaire",
        regime="none",
    )
    sweep = {
        "region": "wallonia",
        "rows": [RankedRow(label="OCTA+ Fixed Impact", annual=3376.46)],
        "labels": {"OCTA+ Fixed Impact": ("octaplus", "octaplus_fixed_impact", False)},
        "household": household,
    }
    months = [date(2026, m, 1) for m in range(1, 10)]
    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _capture,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            ".archived_months_present",
            return_value={(m.year, m.month) for m in months},
        ),
        # The January warm-up fetch the pass makes before asking about
        # coverage; its result is discarded, the cache it fills is stubbed.
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            "._snapshot_for_month",
            AsyncMock(return_value=raw),
        ),
        patch.object(compare_engine, "get_extractor", return_value=object()),
        patch.object(
            compare_engine,
            "_sweep_rows",
            return_value={
                ("wallonia", "octaplus", "octaplus_fixed_impact"): (raw, False)
            },
        ),
    ):
        rows = await engine.fill_ytd_column(sweep, _coord_with_spots({}))

    assert rows[0].ytd == 2386.52
    # Tarif Impact mode for a Tarif Impact card, on the meter its kind forces.
    assert seen["entry"].data["dso_tariff_mode"] == "impact"
    assert seen["meter_override"] == "dynamic"
    # And the card as this entry resolves it, not as the supplier printed it.
    assert seen["snapshot"] == _resolve_snapshot(seen["entry"], raw)
    assert seen["snapshot"] != raw


async def test_a_household_billing_from_its_start_date_gets_a_year_to_date_too(
    hass: HomeAssistant,
) -> None:
    """The pass measured coverage over January to today while the engine walks
    the entry's own window, which starts at the contract start date when the
    option is ticked. The candidate was fetched January as the cheap reject,
    so its covered set carried a month the baseline could never have, the
    two could never be equal, and no candidate row ever printed a figure for
    exactly the households the option exists for.
    """
    from custom_components.be_electricity_prices import compare_engine
    from custom_components.be_electricity_prices.cohort import ytd_window_start
    from tests import make_snapshot

    today = date(2026, 9, 16)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_flex",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "contract_start_date": "2026-04-15",
            "ytd_from_contract_start": True,
        },
    )
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    # A month cache the way the real one behaves: written by the walk and by
    # the pass's warm-up fetch, read back by the coverage check, per contract.
    cache: dict[str, set[date]] = {}

    async def _walk(
        hass_: Any, session: Any, ext: Any, snap: Any, e: Any, **kw: Any
    ) -> float:
        contract = kw.get("contract_override") or e.data["contract"]
        start = ytd_window_start(e, today)
        cache.setdefault(contract, set()).update(
            date(today.year, m, 1) for m in range(start.month, today.month + 1)
        )
        return 2426.56

    async def _warm(
        hass_: Any,
        session: Any,
        ext: Any,
        contract: str,
        region: str,
        month: date,
        *a: Any,
        **kw: Any,
    ) -> Any:
        cache.setdefault(contract, set()).add(month)
        return object()

    def _present(
        hass_: Any, supplier: str, contract: str, region: str, months: Any
    ) -> set[tuple[int, int]]:
        return {(m.year, m.month) for m in months if m in cache.get(contract, set())}

    household = SimpleNamespace(
        today_local=today,
        ytd_from=ytd_window_start(entry, today),
        current_snapshot=object(),
        raw_snapshot=object(),
        quote_entry=entry,
        peak_kw=4.0,
        current_meter="mono",
        dso_mode="bi_horaire",
        regime="none",
    )
    sweep = {
        "region": "wallonia",
        "rows": [
            RankedRow(label="Eneco Zon & Wind Flex", annual=1272.75, is_own=True),
            RankedRow(label="Engie Easy Fixed", annual=3432.93),
        ],
        "labels": {"Engie Easy Fixed": ("engie", "engie_easy_fixed", False)},
        "household": household,
    }
    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _walk,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            "._snapshot_for_month",
            _warm,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            ".archived_months_present",
            _present,
        ),
        patch.object(compare_engine, "get_extractor", return_value=object()),
        patch.object(
            compare_engine,
            "_sweep_rows",
            return_value={
                ("wallonia", "engie", "engie_easy_fixed"): (make_snapshot(), False)
            },
        ),
    ):
        rows = await engine.fill_ytd_column(sweep, _coord_with_spots({}))

    assert household.ytd_from == date(2026, 4, 15)
    assert [r.ytd for r in rows] == [2426.56, 2426.56]
    # And nothing before the window was ever asked for.
    assert min(cache["engie_easy_fixed"]) == date(2026, 4, 1)


async def test_a_household_that_switched_supplier_keeps_its_year_to_date(
    hass: HomeAssistant,
) -> None:
    """After a recorded switch the own walk starts on the switch day, so the
    own contract's cards are fetched only from that month on, while each
    candidate is walked from 1 January. Held to the own contract's months
    alone, no candidate could ever match and the whole column went blank. The
    months before the switch are the earlier contract's, priced on its own
    cards, and a candidate is asked to replay every one of them.
    """
    from custom_components.be_electricity_prices import (
        compare_engine,
        contract_periods,
    )
    from custom_components.be_electricity_prices.cohort import ytd_window_start
    from tests import make_snapshot

    today = date(2026, 9, 16)
    held = {**make_entry(supplier="engie", contract="engie_easy_fixed").data}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_flex",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "contract_start_date": "2026-06-15",
            "previous_contracts": [{"until": "2026-06-15", "data": held}],
        },
    )
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    cache: dict[str, set[date]] = {}
    walked_from: dict[str, date | None] = {}

    async def _walk(
        hass_: Any, session: Any, ext: Any, snap: Any, e: Any, **kw: Any
    ) -> float:
        # As the real walk does: from the override when one is given.
        contract = kw.get("contract_override") or e.data["contract"]
        walked_from[contract] = kw.get("window_start_override")
        start = kw.get("window_start_override") or ytd_window_start(e, today)
        cache.setdefault(contract, set()).update(
            date(today.year, m, 1) for m in range(start.month, today.month + 1)
        )
        return 1000.0

    async def _warm(
        hass_: Any,
        session: Any,
        ext: Any,
        contract: str,
        region: str,
        month: date,
        *a: Any,
        **kw: Any,
    ) -> Any:
        cache.setdefault(contract, set()).add(month)
        return object()

    def _present(
        hass_: Any, supplier: str, contract: str, region: str, months: Any
    ) -> set[tuple[int, int]]:
        return {(m.year, m.month) for m in months if m in cache.get(contract, set())}

    async def _with_previous(*a: Any, **kw: Any) -> float | None:
        own = a[5]
        return None if own is None else own + 250.0

    household = SimpleNamespace(
        today_local=today,
        ytd_from=ytd_window_start(entry, today),
        current_snapshot=object(),
        raw_snapshot=object(),
        quote_entry=entry,
        peak_kw=4.0,
        current_meter="mono",
        dso_mode="bi_horaire",
        regime="none",
    )
    sweep = {
        "region": "wallonia",
        "rows": [
            RankedRow(label="Eneco Zon & Wind Flex", annual=1272.75, is_own=True),
            RankedRow(label="Engie Easy Fixed", annual=3432.93),
        ],
        "labels": {"Engie Easy Fixed": ("engie", "engie_easy_fixed", False)},
        "household": household,
    }
    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _walk,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            "._snapshot_for_month",
            _warm,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            ".archived_months_present",
            _present,
        ),
        patch.object(contract_periods, "with_previous_contracts", _with_previous),
        patch.object(compare_engine, "get_extractor", return_value=object()),
        patch.object(
            compare_engine,
            "_sweep_rows",
            return_value={
                ("wallonia", "engie", "engie_easy_fixed"): (make_snapshot(), False)
            },
        ),
    ):
        rows = await engine.fill_ytd_column(sweep, _coord_with_spots({}))
        assert [r.ytd for r in rows] == [1250.0, 1000.0]
        # A candidate covers the own row's days, which after a switch the
        # entry's settings no longer give it.
        assert walked_from["engie_easy_fixed"] == household.ytd_from
        # A candidate missing a month before the switch is still refused.
        cache.clear()
        cache["engie_easy_fixed"] = {date(2026, m, 1) for m in range(1, 10) if m != 3}

        async def _gap(
            hass_: Any, session: Any, ext: Any, snap: Any, e: Any, **kw: Any
        ) -> float:
            if kw.get("contract_override"):
                return 1000.0
            return await _walk(hass_, session, ext, snap, e, **kw)

        with patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _gap,
        ):
            rows = await engine.fill_ytd_column(sweep, _coord_with_spots({}))
    assert [r.ytd for r in rows] == [1250.0, None]


async def test_the_pass_hands_the_engine_the_spots_it_credits_feed_in_from(
    hass: HomeAssistant,
) -> None:
    """A spot-indexed feed-in is dropped whole, not approximated, without them.

    _ytd_spot_injection_credit returns 0.0 on an empty cache, so a row priced
    without spots is a solar household's bill with no solar in it. The
    one-to-one page has always passed them; this pass did not, which put the
    household's OWN row at odds with its current_year_cost sensor.
    """
    from custom_components.be_electricity_prices import compare_engine

    spots = {datetime(2026, 1, 1, 5, tzinfo=dt_util.UTC): 0.08}
    seen: dict[str, Any] = {}

    async def _capture(*args: Any, **kw: Any) -> float:
        seen.update(kw)
        return 708.11

    entry = MockConfigEntry(domain=DOMAIN, data={"supplier": "eneco", "contract": "x"})
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    sweep = {
        "region": "wallonia",
        "rows": [RankedRow(label="Eneco Zon & Wind Flex", annual=1272.75, is_own=True)],
        "labels": {},
        "household": SimpleNamespace(
            today_local=dt_util.now().date(),
            ytd_from=date(dt_util.now().year, 1, 1),
            current_snapshot=object(),
            raw_snapshot=object(),
            quote_entry=entry,
            peak_kw=4.0,
        ),
    }
    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            _capture,
        ),
        patch(
            "custom_components.be_electricity_prices.snapshot_months"
            ".archived_months_present",
            return_value=[],
        ),
        patch.object(compare_engine, "get_extractor", return_value=object()),
        patch.object(compare_engine, "_sweep_rows", return_value={}),
    ):
        await engine.fill_ytd_column(sweep, _coord_with_spots(spots))

    assert seen["historical_spots"] == spots
    assert seen["spot_quarters"] == {}


async def test_a_row_needing_spots_that_are_absent_prints_no_figure(
    hass: HomeAssistant,
) -> None:
    """Better no figure than a solar bill with the solar silently missing.

    The contract-kind test above describes the ENERGY leg only, so a static
    card whose injection alone is spot-indexed walks straight past it.
    """
    from custom_components.be_electricity_prices import compare_inputs
    from custom_components.be_electricity_prices.providers._rates import InjectionRates

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"supplier": "cociter", "contract": "x", "solar_regime": "injection"},
    )
    entry.add_to_hass(hass)
    snap = SimpleNamespace(
        injection=InjectionRates(current=None, factor=1.0, base=0.0),
        energy=SimpleNamespace(),
    )
    # Needs spots and has none: no figure.
    assert compare_inputs._needs_missing_spots(snap, entry, {}) is True  # type: ignore[arg-type]
    # The same card once the cache holds something.
    spots = {datetime(2026, 1, 1, tzinfo=dt_util.UTC): 0.08}
    assert compare_inputs._needs_missing_spots(snap, entry, spots) is False  # type: ignore[arg-type]
    # A printed monthly indicative needs no spot at all.
    monthly = SimpleNamespace(
        injection=InjectionRates(current=0.05, factor=1.0, base=0.0),
        energy=SimpleNamespace(),
    )
    assert compare_inputs._needs_missing_spots(monthly, entry, {}) is False  # type: ignore[arg-type]


async def test_the_pass_reads_the_meter_once_for_every_candidate(
    hass: HomeAssistant,
) -> None:
    """The household's kWh does not depend on the supplier being priced.

    _compute_current_year_cost re-reads the recorder on every call and nothing
    memoised it, so pricing N candidates against one household made N+1
    identical full-window queries in a job that runs nightly and unattended.
    """
    from custom_components.be_electricity_prices import compare_engine
    from custom_components.be_electricity_prices.energy_meters import (
        _sum_hourly_kwh,
        memoise_meter_reads,
    )

    reads: list[tuple[Any, ...]] = []

    async def _counting_recorder(
        _hass: Any, entity_id: str, start: Any, end: Any
    ) -> dict[Any, float]:
        reads.append((entity_id, start, end))
        return {}

    target = (
        "custom_components.be_electricity_prices.energy_meters._recorder_hourly_kwh"
    )
    ids = ["sensor.cons"]
    start, end = date(2026, 1, 1), date(2026, 9, 3)

    # Without the memo, every caller hits the recorder.
    with patch(target, _counting_recorder):
        for _ in range(5):
            await _sum_hourly_kwh(hass, ids, start, end)
    assert len(reads) == 5

    # Inside one, the first read answers the rest.
    reads.clear()
    with patch(target, _counting_recorder), memoise_meter_reads({}):
        for _ in range(5):
            await _sum_hourly_kwh(hass, ids, start, end)
    assert len(reads) == 1, f"expected one recorder read, got {len(reads)}"

    # A different window is a different question and is still read.
    reads.clear()
    with patch(target, _counting_recorder), memoise_meter_reads({}):
        await _sum_hourly_kwh(hass, ids, start, end)
        await _sum_hourly_kwh(hass, ids, date(2026, 7, 1), end)
    assert len(reads) == 2

    # And the pass turns the memo on rather than leaving it to a caller.
    assert "memoise_meter_reads" in inspect.getsource(
        compare_engine._SweepEngine.fill_ytd_column
    )


async def test_the_pass_reads_the_live_day_once_for_every_candidate(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Today's kWh comes from a walk of every state the meter recorded since
    midnight, which on a meter storing a row per Wh is tens of thousands of
    rows by evening. The year-to-date pass asked it again for each hourly
    billed candidate, so a late run read the same day a hundred times.

    Inside one memo the day is read once per meter; a new memo, which the next
    run opens, reads it afresh, so no tick is served a stale live figure.
    """
    from unittest.mock import MagicMock

    from homeassistant.core import State

    from custom_components.be_electricity_prices.energy_meters import (
        _live_today_kwh,
        memoise_meter_reads,
    )

    freezer.move_to("2026-07-16 18:00:00+02:00")
    attrs = {
        "unit_of_measurement": "kWh",
        "device_class": "energy",
        "state_class": "total_increasing",
    }
    hass.states.async_set("sensor.a", "150.0", attrs)
    hass.states.async_set("sensor.b", "40.0", attrs)
    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(
        return_value={
            "sensor.a": [State("sensor.a", "100.0")],
            "sensor.b": [State("sensor.b", "30.0")],
        }
    )
    today = date(2026, 7, 16)
    target = "homeassistant.components.recorder.get_instance"

    with patch(target, return_value=instance):
        for _ in range(5):
            assert await _live_today_kwh(hass, "sensor.a", today) == 50.0
    assert instance.async_add_executor_job.await_count == 5

    instance.async_add_executor_job.reset_mock()
    with patch(target, return_value=instance), memoise_meter_reads({}):
        for _ in range(5):
            assert await _live_today_kwh(hass, "sensor.a", today) == 50.0
        # Another meter is another question.
        assert await _live_today_kwh(hass, "sensor.b", today) == 10.0
    assert instance.async_add_executor_job.await_count == 2

    # The next run opens its own memo and reads the meter again.
    hass.states.async_set("sensor.a", "160.0", attrs)
    instance.async_add_executor_job.reset_mock()
    with patch(target, return_value=instance), memoise_meter_reads({}):
        assert await _live_today_kwh(hass, "sensor.a", today) == 60.0
    assert instance.async_add_executor_job.await_count == 1


async def test_the_memo_key_separates_households_that_must_not_share(
    hass: HomeAssistant,
) -> None:
    """A memo is only safe while its key names everything the answer depends on.

    The daily reader's answer turns on the meter type, the region and the six
    sensor ids, all read off entry.data, plus the window. Keyed short of any
    of those, a sweep would hand one household's kWh to another's pricing,
    which is the one way this optimisation could reach a bill.
    """
    from custom_components.be_electricity_prices.energy_meters import (
        _resolve_daily_kwh,
        memoise_meter_reads,
    )

    calls: list[str] = []

    async def _counting(
        _hass: Any, entity_id: str, start: date, _end: date
    ) -> dict[date, float]:
        calls.append(entity_id)
        return {start: 1.0}

    def _entry(**over: Any) -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            data={
                "meter": "mono",
                "region": "wallonia",
                "consumption_kwh": "sensor.a",
                **over,
            },
        )

    today = date(2026, 9, 3)
    target = "custom_components.be_electricity_prices.energy_meters._recorder_daily_kwh"
    with patch(target, _counting), memoise_meter_reads({}):
        await _resolve_daily_kwh(hass, _entry(), today)
        assert len(calls) == 1
        # The same question again is the memo's whole purpose.
        await _resolve_daily_kwh(hass, _entry(), today)
        assert len(calls) == 1
        # Every dimension the answer turns on is a different question, and
        # the meter is one of them whether it comes from the entry or from
        # the override a comparison quotes a target contract on.
        cases: tuple[tuple[dict[str, Any], date | None, MeterType | None], ...] = (
            ({"consumption_kwh": "sensor.b"}, None, None),
            ({"meter": "bi"}, None, None),
            ({"region": "flanders"}, None, None),
            ({}, date(2026, 7, 1), None),
            ({}, None, "exclusive_night"),
        )
        for over, start, meter in cases:
            before = len(calls)
            await _resolve_daily_kwh(hass, _entry(**over), today, start, meter=meter)
            assert len(calls) > before, (
                f"{over or start or meter} must not hit the memo"
            )

        # ...and the key is on the EFFECTIVE meter, so an override that lands
        # where another entry's own meter already did shares its answer. That
        # is the point of keying on the meter rather than on where it came
        # from: the band split, and so the recorder work, is identical.
        before = len(calls)
        await _resolve_daily_kwh(hass, _entry(), today, meter="bi")
        assert len(calls) == before


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
    # The page names no region or group: those are English slugs
    # ("wallonia", "static") that read as prose in every translation.
    assert set(result["description_placeholders"]) == {"ranking"}


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


async def test_refresh_after_the_year_to_date_pass_keeps_the_own_row(
    hass: Any,
) -> None:
    """Refresh on a stored ranking, after the year-to-date box was ticked,
    ranked the alternatives with no baseline: the progress step appends the
    household's own row only when it resolves the household itself, and the
    year-to-date pass had already resolved it. Refresh has to drop the
    household with the rows, and offer the year-to-date pass again on the
    rows it just priced."""
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
            result["flow_id"], {"with_ytd": True}
        )
        assert result["step_id"] == "compare_all_result"
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"refresh": True}
        )
        for _ in range(400):
            if result["type"] is not data_entry_flow.FlowResultType.SHOW_PROGRESS:
                break
            await hass.async_block_till_done()
            result = await hass.config_entries.options.async_configure(
                result["flow_id"]
            )
        assert result["step_id"] == "compare_all_result"

    ranking = result["description_placeholders"]["ranking"]
    assert "YOUR CONTRACT" in ranking
    assert "with_ytd" in result["data_schema"].schema


async def test_the_sweep_persists_its_ranking_at_once(
    hass: HomeAssistant,
) -> None:
    """The sweep runs once a day and takes minutes. Left for the next hourly
    tick to write, a restart inside that window threw away a ranking that had
    just been built."""
    from custom_components.be_electricity_prices.compare_sweep_flow import (
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
        "custom_components.be_electricity_prices.compare_sweep_flow._SweepEngine"
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
    from custom_components.be_electricity_prices.compare_engine import _SweepEngine

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    engine = _SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    priced = RankedRow(label="Mega Online Fixed", annual=1102.75)

    async def _fake_fill(_self: Any, sweep: Any, coord: Any) -> list[RankedRow]:
        # The pass sees the rows the sweep just priced, and enriches them,
        # and it is handed the coordinator whose spot cache it credits from.
        assert coord is not None
        assert [r.label for r in sweep["rows"]] == ["Mega Online Fixed"]
        return [
            RankedRow(label=r.label, annual=r.annual, ytd=612.40) for r in sweep["rows"]
        ]

    with (
        patch.object(
            _SweepEngine,
            "build_sweep",
            return_value={"candidates": [("mega", "x", False)], "rows": []},
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
    from custom_components.be_electricity_prices.compare_engine import _SweepEngine

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
            return_value={"candidates": [("mega", "x", False)], "rows": []},
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
    from custom_components.be_electricity_prices.compare_sweep_flow import (
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
        "custom_components.be_electricity_prices.compare_sweep_flow._SweepEngine"
    ) as engine:
        engine.return_value.run_full_sweep = AsyncMock(return_value=ranking)
        await async_run_daily_compare(hass, entry, coord)

    assert coord.daily_compare is ranking


async def test_a_card_published_as_images_is_priced_from_the_archive_reading(
    hass: HomeAssistant,
) -> None:
    """The live tick answers an unreadable card with the archive's OCR
    reading, and the ranking did not, so a supplier publishing page images
    read "card has no text layer" on the one screen that says whether to
    switch to it. Ecofix's four contracts did exactly that.

    Only for that failure: every other one is a supplier unreachable or
    broken, where a stale reading would be the wrong answer, which is why the
    fallback tests the exception type rather than its message.
    """
    from custom_components.be_electricity_prices import compare_engine
    from custom_components.be_electricity_prices.providers.base import (
        CardNotReadableError,
        ExtractorError,
    )
    from custom_components.be_electricity_prices.snapshot_months import ArchivedCard
    from tests import make_snapshot

    entry = MockConfigEntry(domain=DOMAIN, data={"supplier": "eneco", "contract": "x"})
    entry.add_to_hass(hass)
    engine = compare_engine._SweepEngine(hass, entry, {})  # type: ignore[arg-type]
    archived = ArchivedCard(snapshot=make_snapshot(), read_by_ocr=True)

    unreadable = SimpleNamespace(
        error=CardNotReadableError("card has no text layer: 348 characters"),
        error_message="card has no text layer",
    )
    with patch(
        "custom_components.be_electricity_prices.snapshot_months"
        ".card_for_unreadable_month",
        AsyncMock(return_value=archived),
    ):
        got = await engine._ocr_fallback(
            "ecofix", "ecofix_flexy", "flanders", unreadable
        )
    assert got is archived

    # A supplier that is simply down gets no stale reading.
    down = SimpleNamespace(error=ExtractorError("HTTP 503"), error_message="HTTP 503")
    with patch(
        "custom_components.be_electricity_prices.snapshot_months"
        ".card_for_unreadable_month",
        AsyncMock(side_effect=AssertionError("must not ask the archive")),
    ):
        assert await engine._ocr_fallback("bolt", "bolt_fix", "flanders", down) is None


def test_a_row_priced_from_a_reading_is_tagged_in_the_ranking() -> None:
    """A figure someone might switch supplier over must not hide that it was
    read off a picture of the card. The live entry gets a Repairs card saying
    so; the ranking row says it inline."""
    from custom_components.be_electricity_prices.compare_table import _ranking_table

    rows = (
        RankedRow(label="Eneco Zon & Wind Flex", annual=1200.0, is_own=True),
        RankedRow(label="Ecofix Flexy", annual=1100.0, read_by_ocr=True),
        RankedRow(label="Mega Online Fixed", annual=1150.0),
    )
    text = _ranking_table(rows, ran_at=None, deferred=0)
    ecofix = next(line for line in text.splitlines() if "Ecofix" in line)
    mega = next(line for line in text.splitlines() if "Mega" in line)
    assert "`OCR`" in ecofix
    assert "`OCR`" not in mega


def test_every_annual_row_on_the_page_clamps_per_register() -> None:
    """The compare page prices each row through ``_annual_bill``.

    Under compensation that function clamps per meter register, but only when
    the caller hands it the household's day/night split; without it the whole
    year is netted and clamped once, which is the shape a reversing meter
    does not have. The clamp shipped wired into the projection alone, so the
    page kept the old arithmetic and printed an annual figure beside a
    year-to-date computed by the real walk: measured at 60,00 against 356,51
    on a net-exporting bi-hourly Walloon install, and the ranking inverted.

    Source-level on purpose. The defect was a call site nobody passed the
    argument at, so what has to be pinned is that no call site is missed
    again, not the arithmetic of any one of them, in any compare module.
    """
    from tests import compare_page_calls

    calls = compare_page_calls("_annual_bill")
    assert calls, "the page no longer prices rows through _annual_bill"
    missing = [
        f"{name}:{call.lineno}"
        for name, call in calls
        if "register_weights" not in {kw.arg for kw in call.keywords}
    ]
    assert not missing, (
        f"{len(missing)} of {len(calls)} _annual_bill calls on the compare page "
        f"pass no register_weights, so those rows are netted and clamped once: "
        f"{missing}"
    )
