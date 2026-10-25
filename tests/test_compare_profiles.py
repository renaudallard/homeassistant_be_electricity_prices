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

"""The Synergrid curves a quoted card's own index is weighted by.

An entry loads the load profile only for its own card or its compensation net,
and the solar one only for its own SPP-indexed feed-in. The comparison pages
price every other card on its own index, so a household whose contract names
none ranked every RLP-indexed card on the plain mean, 14 to 28 EUR a year low
at October 2026's prices. The curves any entry fetched are kept for the whole
installation, so the pages read those, and the nightly ranking fetches them.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices.compare_table import (
    DailyCompare,
    IndexGaps,
    RankedRow,
    _ranking_table,
)
from custom_components.be_electricity_prices.coordinator_persist import (
    _daily_compare_from_dict,
    _daily_compare_to_dict,
)
from custom_components.be_electricity_prices.coordinator_profiles import (
    _profile_cache,
)
from custom_components.be_electricity_prices.providers._rates import (
    InjectionRates,
    SpotMonthlyRates,
)
from tests import make_entry
from tests.test_options_flow import _real_coordinator, _stub_snapshot

_EVENING = {
    (9, day, hour): 2.0 if hour == 19 else 1.0
    for day in range(1, 31)
    for hour in range(24)
}


def _month() -> dict[datetime, float]:
    """1 to 20 September, dear at 19:00 and cheap otherwise, so the load
    weighting and the plain mean are far apart."""
    first = dt_util.start_of_local_day(date(2026, 9, 1)).astimezone(UTC)
    today = dt_util.start_of_local_day(date(2026, 9, 20)).astimezone(UTC)
    return {
        first + timedelta(hours=h): (
            0.30 if dt_util.as_local(first + timedelta(hours=h)).hour == 19 else 0.05
        )
        for h in range(int((today - first).total_seconds() // 3600))
    }


async def _household(
    hass: HomeAssistant,
    regime: str = "none",
    *,
    source: bool = True,
    export: float = 0.0,
) -> tuple[Any, Any]:
    """A household on a fixed card, whose coordinator holds no profile at all,
    resolved for one RLP-indexed candidate. With a day-ahead source it holds
    the month; without one it holds no day-ahead, as an entry that lost its
    source no longer keeps any. ``export`` is the trailing year it measured."""
    from custom_components.be_electricity_prices import compare_household
    from custom_components.be_electricity_prices.compare_engine import _SweepEngine
    from custom_components.be_electricity_prices.meter_daily import MeasuredKwh

    entry = (
        make_entry(solar_regime=regime, injection_kwh="sensor.i", api_key="key")
        if source
        else make_entry(solar_regime=regime, injection_kwh="sensor.i")
    )
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    entry.runtime_data = coord
    if source:
        coord._historical_spots = _month()
        today = dt_util.start_of_local_day(date(2026, 9, 20)).astimezone(UTC)
        coord._spot_cache = {today + timedelta(hours=h): 0.05 for h in range(24)}

    async def measured(*_a: Any, side: str = "consumption", **_k: Any) -> Any:
        return MeasuredKwh(export if side == "injection" else 0.0, 365)

    engine = _SweepEngine(hass, entry, {})
    with (
        patch.object(type(coord), "_ensure_historical_spots", AsyncMock()),
        patch.object(compare_household, "_measured_kwh", measured),
    ):
        household = await engine._resolve_household(
            coord, candidates=[("energyvision", "x", False)]
        )
    return coord, household


def _spp_card() -> Any:
    return replace(
        _stub_snapshot("mega", "mega_smart_flex", 0.2),
        injection=InjectionRates(
            current=0.06, factor=0.8, base=-0.02, spp_indexed=True
        ),
    )


def _rlp_card() -> Any:
    return replace(
        _stub_snapshot("energyvision", "x", 0.2),
        energy=SpotMonthlyRates(
            factor=1.0, base=0.0, rlp_indexed=True, rlp_blend="flanders"
        ),
    )


async def test_a_quoted_card_is_weighted_on_the_installation_s_curve(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The household's coordinator loads no load profile, another entry of the
    installation did: the quoted card is weighted on that curve, not priced on
    the plain mean."""
    from custom_components.be_electricity_prices.compare_inputs import (
        _coordinator_rlp_index_weights,
    )
    from custom_components.be_electricity_prices.spot_stats import (
        _mean_of_month,
        _rlp_weighted_month_mean,
    )

    freezer.move_to("2026-09-20 12:00:00+02:00")
    _profile_cache(hass)[("rlp", 2026, "flanders")] = (_EVENING, dt_util.utcnow())
    coord, household = await _household(hass)
    assert not coord._rlp_weights and not coord._rlp_blend_weights
    card = _rlp_card()
    spots = {**coord._historical_spots, **coord._spot_cache}
    weighted = _rlp_weighted_month_mean(spots, _EVENING, 2026, 9)
    assert weighted is not None
    assert weighted != pytest.approx(_mean_of_month(spots, 2026, 9))
    assert await household.spot_for(card) == pytest.approx(weighted)
    assert household.index_gaps(card) == IndexGaps()
    # The year to date of a quoted card reads the same curve; the backfill and
    # an earlier contract, which ask without quoting, still read only the
    # entry's own.
    entry = coord.entry
    assert _coordinator_rlp_index_weights(entry, card, quoted=True) == _EVENING
    assert _coordinator_rlp_index_weights(entry, card) is None


async def test_a_card_priced_without_its_curve_says_so(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing in the installation holds the curve: the plain mean stands in,
    and the quote is marked as priced without it."""
    from custom_components.be_electricity_prices.spot_stats import _mean_of_month

    freezer.move_to("2026-09-20 12:00:00+02:00")
    coord, household = await _household(hass)
    card = _rlp_card()
    spots = {**coord._historical_spots, **coord._spot_cache}
    assert await household.spot_for(card) == pytest.approx(
        _mean_of_month(spots, 2026, 9)
    )
    assert household.index_gaps(card) == IndexGaps(load=True)
    # A card that names no weighted index is never marked.
    assert household.index_gaps(_stub_snapshot("eneco", "power_fix", 0.2)) == (
        IndexGaps()
    )
    # The ranking tags the row, and the one-off quote names it in its note.
    from custom_components.be_electricity_prices.compare_engine import (
        _sweep_rows,
        _SweepEngine,
    )
    from tests.test_options_flow import _compare_placeholders

    entry = coord.entry
    _sweep_rows(hass, entry.entry_id, "wallonia")[
        ("wallonia", "mega", "mega_smart_flex")
    ] = (card, False)
    row = await _SweepEngine(hass, entry, {})._sweep_one(
        {"region": "wallonia", "household": household, "listings": {}},
        "mega",
        "mega_smart_flex",
    )
    assert row.annual is not None
    assert row.profile_missing and not row.day_ahead_missing
    with patch.object(type(coord), "_ensure_historical_spots", AsyncMock()):
        p = await _compare_placeholders(
            hass,
            entry,
            supplier="mega",
            contract="mega_smart_flex",
            target_snapshot=card,
            meter="mono",
        )
    assert "energy index is weighted by the Synergrid load profile" in p["card_note"]
    assert "feed-in" not in p["card_note"]


async def test_an_spp_indexed_feed_in_is_credited_on_the_installation_s_curve(
    hass: HomeAssistant, freezer: Any
) -> None:
    """On the injection tariff an SPP-indexed candidate's credit settles on the
    solar-weighted mean. The household's own card names none, so its
    coordinator holds no solar curve, and the credit fell back to the figure
    the card prints."""
    from custom_components.be_electricity_prices.compare_inputs import (
        _coordinator_spp_weights,
    )

    freezer.move_to("2026-09-20 12:00:00+02:00")
    noon = {
        (9, day, hour): 1.0 if 10 <= hour <= 14 else 0.0
        for day in range(1, 31)
        for hour in range(24)
    }
    card = _spp_card()
    coord, household = await _household(hass, "injection", export=3000.0)
    assert household.index_gaps(card) == IndexGaps(solar=True)
    assert (
        await household.credit_month_spot_for(
            card, entry=household.quote_entry, own=False
        )
        is None
    )
    # With no export measured the credit moves nothing, so nothing is said.
    coord, household = await _household(hass, "injection")
    assert household.index_gaps(card) == IndexGaps()
    _profile_cache(hass)[("spp", 2026, "")] = (noon, dt_util.utcnow())
    coord, household = await _household(hass, "injection", export=3000.0)
    assert household.index_gaps(card) == IndexGaps()
    spot = await household.credit_month_spot_for(
        card, entry=household.quote_entry, own=False
    )
    # The noon hours, which this month prices at 0,05, against a plain mean
    # lifted by every evening at 0,30.
    assert spot == pytest.approx(0.05)
    assert _coordinator_spp_weights(coord.entry, card, own_entry=None) == noon


async def test_the_nightly_ranking_fetches_the_curves_before_pricing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The scheduled ranking has all night, so it fetches the curves the
    cards' own indices are weighted by, once for the installation, and before
    the household is resolved, whose means are taken on what is held then.
    The solar curve only where a feed-in is credited at a price."""
    from custom_components.be_electricity_prices import coordinator_profiles
    from custom_components.be_electricity_prices.compare_engine import _SweepEngine

    freezer.move_to("2026-09-20 03:00:00+02:00")
    asked: list[str] = []

    async def _blends(_session: Any, year: int, blends: Any) -> dict[str, Any]:
        asked.append(f"rlp {year}")
        return dict.fromkeys(blends, _EVENING)

    async def _solar(_session: Any, year: int) -> dict[tuple[int, int, int], float]:
        asked.append(f"spp {year}")
        return {(9, 1, 12): 1.0}

    held_at_resolve: list[bool] = []

    async def _resolve(_self: Any, coord: Any, **_k: Any) -> None:
        held_at_resolve.append(coord.quoted_rlp_weights("flanders") is not None)

    for regime, expected in (
        ("none", ["rlp 2026"]),
        ("injection", ["rlp 2026", "spp 2026"]),
    ):
        _profile_cache(hass).clear()
        asked.clear()
        entry = make_entry(solar_regime=regime, api_key="key")
        entry.add_to_hass(hass)
        coord = _real_coordinator(
            hass, entry, _stub_snapshot("eneco", "power_fix", 0.18)
        )
        entry.runtime_data = coord
        with (
            patch.object(coordinator_profiles, "fetch_rlp_blends", _blends),
            patch.object(coordinator_profiles, "fetch_spp_weights", _solar),
            patch.object(
                _SweepEngine,
                "build_sweep",
                return_value={"candidates": [], "rows": []},
            ),
            patch.object(_SweepEngine, "_resolve_household", _resolve),
            patch.object(_SweepEngine, "_sweep_own_row", AsyncMock(return_value=None)),
            patch.object(_SweepEngine, "fill_ytd_column", AsyncMock(return_value=[])),
        ):
            await _SweepEngine(hass, entry, {}).run_full_sweep(coord)
            # Fresh now, so the next night fetches nothing.
            await _SweepEngine(hass, entry, {}).run_full_sweep(coord)
        assert asked == expected
        assert held_at_resolve[-2:] == [True, True]
        # The entry's own pricing is untouched: it loads what its card needs.
        assert not coord._rlp_weights


def test_the_ranking_says_which_rows_lack_their_curve() -> None:
    rows = [
        RankedRow(
            label="Engie Dynamic", annual=1050.0, is_own=True, day_ahead_missing=True
        ),
        RankedRow(label="energie.be Variabel", annual=1370.0, profile_missing=True),
        RankedRow(label="Ecofix Flexy", annual=1280.0, day_ahead_missing=True),
    ]
    table = _ranking_table(rows)
    assert "energie.be Variabel · +320,00 `NO PROFILE`" in table
    # The profile line says what its rows were priced on, leg by leg, and names
    # the source a profile is fetched for.
    assert "an energy index is then priced on the plain monthly mean" in table
    assert "with an ENTSO-E key or the keyless setting" in table
    assert "`NO PROFILE`" not in _ranking_table(rows[:1])
    # A household without a day-ahead source is told so once, under the rows,
    # and no row carries it as a tag.
    assert "Ecofix Flexy · +230,00\n" in table + "\n"
    assert "DAY-AHEAD`" not in table
    line = (
        "Without a day-ahead source (an ENTSO-E key or the keyless setting) every "
        "feed-in indexed on a month is credited at the figure its card prints, "
        "your own contract's included."
    )
    assert table.count(line) == 1
    assert line not in _ranking_table(rows[1:2])
    stored = DailyCompare(
        rows=tuple(rows),
        own=1050.0,
        priced=2,
        total=2,
        ran_at=datetime(2026, 10, 23, 3, tzinfo=UTC),
    )
    restored = _daily_compare_from_dict(_daily_compare_to_dict(stored))
    assert restored is not None
    assert [row.profile_missing for row in restored.rows] == [False, True, False]
    assert [row.day_ahead_missing for row in restored.rows] == [True, False, True]
    # A stored ranking says it too.
    assert _ranking_table(restored.rows).count(line) == 1
    # The own contract is named only when its own feed-in is one of them: one
    # fixed for the term is credited as its card says.
    fixed_own = [replace(rows[0], day_ahead_missing=False), *rows[1:]]
    without = _ranking_table(fixed_own)
    assert "your own contract" not in without
    assert without.count(line.removesuffix(", your own contract's included.")) == 1
    # The own row alone: a key typed on the page resolved every other row, and
    # the own one stays on its printed figure, as its sensor does.
    own_only = _ranking_table([rows[0], replace(rows[2], day_ahead_missing=False)])
    assert "every feed-in" not in own_only
    assert (
        own_only.count(
            "Your own contract's feed-in, indexed on a month, is credited at the "
            "figure its card prints, as its sensor credits it without a day-ahead "
            "source (an ENTSO-E key or the keyless setting) of its own."
        )
        == 1
    )


async def test_without_a_day_ahead_source_the_cause_is_the_source(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No ENTSO-E key and the keyless setting off: no month index resolves,
    whatever curve is held, and the nightly ranking fetches none. A feed-in on
    a month index, plain or solar-weighted, is credited at the figure its card
    prints, and the page says that, not that a profile is missing."""
    from custom_components.be_electricity_prices.compare_engine import (
        _sweep_rows,
        _SweepEngine,
    )

    freezer.move_to("2026-09-20 12:00:00+02:00")
    plain = replace(
        _stub_snapshot("eneco", "power_flex", 0.2),
        injection=InjectionRates(
            current=0.09, factor=1.0, base=-0.01, month_indexed=True
        ),
    )
    fixed = replace(
        _stub_snapshot("eneco", "power_fix", 0.2),
        injection=InjectionRates(current=0.05),
    )
    coord, household = await _household(hass, "injection", source=False, export=3000.0)
    assert household.index_gaps(_spp_card()) == IndexGaps(day_ahead=True)
    assert household.index_gaps(plain) == IndexGaps(day_ahead=True)
    assert household.index_gaps(fixed) == IndexGaps()
    # An RLP-indexed energy leg cannot be priced at all without a day-ahead,
    # so it is never put down to the profile.
    assert household.index_gaps(_rlp_card()) == IndexGaps()
    # A curve held changes nothing.
    _profile_cache(hass)[("spp", 2026, "")] = ({(9, 1, 12): 1.0}, dt_util.utcnow())
    assert household.index_gaps(_spp_card()) == IndexGaps(day_ahead=True)
    entry = coord.entry
    _sweep_rows(hass, entry.entry_id, "wallonia")[
        ("wallonia", "mega", "mega_smart_flex")
    ] = (_spp_card(), False)
    row = await _SweepEngine(hass, entry, {})._sweep_one(
        {"region": "wallonia", "household": household, "listings": {}},
        "mega",
        "mega_smart_flex",
    )
    assert row.annual is not None
    assert row.day_ahead_missing and not row.profile_missing


async def test_the_one_off_quote_says_once_that_there_is_no_day_ahead(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The one-off quote of a household with no day-ahead source says once that
    a month-indexed feed-in is credited at the printed figure, however many
    sides it concerns, and puts it down to no card in particular."""
    from custom_components.be_electricity_prices import compare_household
    from custom_components.be_electricity_prices.meter_daily import MeasuredKwh
    from tests.test_options_flow import _drive_compare

    freezer.move_to("2026-09-20 12:00:00+02:00")
    card = replace(
        _stub_snapshot("eneco", "power_flex", 0.2),
        injection=InjectionRates(
            current=0.09, factor=1.0, base=-0.01, month_indexed=True
        ),
    )
    entry = make_entry(
        contract="power_flex", solar_regime="injection", injection_kwh="sensor.i"
    )
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(hass, entry, card)

    async def measured(*_a: Any, side: str = "consumption", **_k: Any) -> Any:
        return MeasuredKwh(3000.0 if side == "injection" else 3500.0, 365)

    with patch.object(compare_household, "_measured_kwh", measured):
        p = await _drive_compare(
            hass,
            entry,
            other_snap=card,
            other_supplier="eneco",
            other_contract="power_flex",
        )
    note = p["card_note"]
    assert note.count("without a day-ahead source") == 1
    assert "Eneco's feed-in" not in note
    assert note.endswith("your own contract's included")
    # An own feed-in fixed for the term is credited as its card says, so the
    # note names only the others when the quoted card is month-indexed.
    own = replace(card, injection=InjectionRates(current=0.05))
    entry = make_entry(solar_regime="injection", injection_kwh="sensor.i")
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(hass, entry, own)
    with patch.object(compare_household, "_measured_kwh", measured):
        p = await _drive_compare(
            hass,
            entry,
            other_snap=card,
            other_supplier="eneco",
            other_contract="power_flex",
        )
    note = p["card_note"]
    assert note.count("without a day-ahead source") == 1
    assert "your own contract" not in note


async def test_a_key_typed_on_the_page_says_the_own_feed_in_stays_printed(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The own side reads only the entry's own day-ahead source, as its sensor
    does, so a key typed on the page resolves the quoted side's month and not
    the own one. The gap was put down to the page's source, so the note said
    nothing and the contract quoted against itself cost 130 EUR a year more
    than itself, unexplained."""
    from custom_components.be_electricity_prices import compare_flow, ytd_cost
    from custom_components.be_electricity_prices.meter_daily import MeasuredKwh
    from tests.test_options_flow import _compare_placeholders

    freezer.move_to("2026-10-24 12:00:00+02:00")
    card = replace(
        _stub_snapshot("eneco", "power_flex", 0.2),
        injection=InjectionRates(
            current=0.0904, factor=0.77, base=-0.03, month_indexed=True
        ),
    )
    october = datetime(2026, 9, 30, 22, tzinfo=UTC)
    month = {october + timedelta(hours=h): 0.10 for h in range(23 * 24)}

    async def fetch(
        self: Any, start: Any, end: Any, api_key: Any = None, *, keyless: bool = False
    ) -> None:
        if api_key or keyless:
            self._historical_spots.update(month)

    async def measured(*_a: Any, side: str = "consumption", **_k: Any) -> Any:
        return MeasuredKwh(3000.0 if side == "injection" else 3500.0, 365)

    household = "custom_components.be_electricity_prices.compare_household."
    for typed, note in (
        (" ", "every feed-in indexed on a month"),
        ("TESTKEY", "as its sensor credits it without a day-ahead source"),
    ):
        entry = make_entry(
            contract="power_flex",
            solar_regime="injection",
            consumption_kwh="sensor.c",
            injection_kwh="sensor.i",
        )
        entry.add_to_hass(hass)
        coord = _real_coordinator(hass, entry, card)
        entry.runtime_data = coord
        with (
            patch(household + "_measured_hourly", AsyncMock(return_value=None)),
            patch(household + "_measured_kwh", measured),
            patch.object(type(coord), "_ensure_historical_spots", fetch),
            patch.object(
                compare_flow, "_validate_entsoe_key", AsyncMock(return_value=None)
            ),
            patch(
                "custom_components.be_electricity_prices.api."
                "fetch_day_ahead_or_fallback",
                AsyncMock(return_value=({}, "entsoe")),
            ),
            patch.object(
                ytd_cost, "_compute_current_year_cost", AsyncMock(return_value=100.0)
            ),
        ):
            p = await _compare_placeholders(
                hass,
                entry,
                supplier="eneco",
                contract="power_flex",
                target_snapshot=card,
                api_key=typed,
            )
        assert p["card_note"].count(note) == 1


async def test_the_own_contract_against_itself_says_each_caveat_once(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Quoted against itself the own contract is one card on both sides, and
    every caveat line of it read twice in a row."""
    from custom_components.be_electricity_prices.providers._rates import (
        VariableRates,
    )
    from tests.test_options_flow import _compare_placeholders

    freezer.move_to("2026-09-20 12:00:00+02:00")
    card = replace(
        _stub_snapshot("eneco", "power_flex", 0.2),
        energy=VariableRates(current=0.2, month_indexed=True),
    )
    entry = make_entry(contract="power_flex")
    entry.add_to_hass(hass)
    entry.runtime_data = _real_coordinator(hass, entry, card)
    p = await _compare_placeholders(
        hass,
        entry,
        supplier="eneco",
        contract="power_flex",
        target_snapshot=card,
        meter="mono",
    )
    assert p["card_note"].count("computed on an earlier month's index") == 1


async def test_the_year_to_date_of_a_quoted_card_reads_the_installation_s_curve(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The quoted side's year to date is walked on the index its card names,
    with the curve the installation holds; the household's own is walked on
    what its coordinator loaded, as the current_year_cost sensor is."""
    from custom_components.be_electricity_prices import ytd_cost
    from tests.test_options_flow import _compare_placeholders

    freezer.move_to("2026-09-20 12:00:00+02:00")
    _profile_cache(hass)[("rlp", 2026, "columns")] = (_EVENING, dt_util.utcnow())
    entry = make_entry(api_key="key")
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, _stub_snapshot("eneco", "power_fix", 0.18))
    entry.runtime_data = coord
    target = replace(
        _stub_snapshot("mega", "mega_smart_flex", 0.2),
        energy=SpotMonthlyRates(
            factor=1.0, base=0.0, rlp_indexed=True, rlp_blend="columns"
        ),
    )
    walked: list[Any] = []

    async def walk(*_a: Any, rlp_index_weights: Any = None, **_k: Any) -> float:
        walked.append(rlp_index_weights)
        return 100.0

    with patch.object(ytd_cost, "_compute_current_year_cost", walk):
        await _compare_placeholders(
            hass,
            entry,
            supplier="mega",
            contract="mega_smart_flex",
            target_snapshot=target,
            meter="mono",
        )
    assert walked == [None, _EVENING]


async def test_the_ranking_s_year_to_date_reads_the_installation_s_curve(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The same on the ranking's year-to-date column."""
    from custom_components.be_electricity_prices import snapshot_months, ytd_cost
    from custom_components.be_electricity_prices.compare_engine import (
        _sweep_rows,
        _SweepEngine,
    )
    from custom_components.be_electricity_prices.compare_inputs import (
        _candidate_label,
    )

    freezer.move_to("2026-09-20 12:00:00+02:00")
    _profile_cache(hass)[("rlp", 2026, "columns")] = (_EVENING, dt_util.utcnow())
    coord, household = await _household(hass)
    entry = coord.entry
    card = replace(
        _stub_snapshot("mega", "mega_smart_flex", 0.2),
        energy=SpotMonthlyRates(
            factor=1.0, base=0.0, rlp_indexed=True, rlp_blend="columns"
        ),
    )
    label = _candidate_label("mega", "mega_smart_flex", False)
    _sweep_rows(hass, entry.entry_id, "wallonia")[
        ("wallonia", "mega", "mega_smart_flex")
    ] = (card, False)
    sweep = {
        "region": "wallonia",
        "household": household,
        "labels": {label: ("mega", "mega_smart_flex", False)},
        "rows": [
            RankedRow(label="own", annual=1000.0, is_own=True),
            RankedRow(label=label, annual=1100.0),
        ],
    }
    walked: list[Any] = []

    async def walk(*_a: Any, rlp_index_weights: Any = None, **_k: Any) -> float:
        walked.append(rlp_index_weights)
        return 100.0

    months = {(2026, m) for m in range(1, 9)}
    with (
        patch.object(ytd_cost, "_compute_current_year_cost", walk),
        patch.object(snapshot_months, "_snapshot_for_month", AsyncMock()),
        patch.object(
            snapshot_months, "archived_months_present", lambda *_a: set(months)
        ),
    ):
        rows = await _SweepEngine(hass, entry, {}).fill_ytd_column(sweep, coord)
    assert walked == [None, _EVENING]
    assert [row.ytd for row in rows] == [100.0, 100.0]
