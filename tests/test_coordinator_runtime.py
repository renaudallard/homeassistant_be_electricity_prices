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

"""Tests for force-refresh and the stale-snapshot repair issue."""

from __future__ import annotations


from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.api import (
    EntsoeAuthError,
    EntsoeError,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.providers.base import (
    CardNotReadableError,
)
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
)
from custom_components.be_electricity_prices.coordinator_issues import (
    _successor_for,
)
from custom_components.be_electricity_prices.snapshot_store import (
    _SNAPSHOT_SCHEMA_VERSION,
    _monthly_snapshots,
    _shared_failed_fetches,
    _shared_lock,
    _shared_snapshots,
    _snapshot_to_dict,
    evict_shared_caches,
)
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    InjectionRates,
    SupplierSnapshot,
)
from tests import make_entry, make_snapshot, make_stub_extractor


_SPOTS = "custom_components.be_electricity_prices.coordinator_spots"


def _entry() -> MockConfigEntry:
    return make_entry()


@contextmanager
def _patch_spot_fetch(fake_fetch: Any) -> Iterator[None]:
    """Answer BOTH day-ahead paths with one ``fetch_day_ahead``-shaped fake.

    The live tick calls ``fetch_day_ahead_or_fallback``; the historical walk
    drives ``EntsoeClient`` directly and hands whatever it could not answer to
    the keyless fallback in a single request afterwards. The fake stands in for
    the ENTSO-E leg of both, so a test controls exactly what it did before.

    The keyless leg is stubbed to fail rather than left alone: it keeps the
    real endpoint off the network when the fake raises, and it keeps a raising
    fake meaning what it has always meant here -- no prices for that window.
    """

    async def _wrapper(
        _api_key: str,
        _session: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> tuple[dict[datetime, float], str]:
        return await fake_fetch(start, end, quarter_hourly=quarter_hourly), "entsoe"

    async def _method(
        _self: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        return await fake_fetch(start, end, quarter_hourly=quarter_hourly)

    async def _no_fallback(
        _self: Any,
        _start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        raise EntsoeError("keyless fallback disabled for this test")

    with (
        patch(_SPOTS + ".fetch_day_ahead_or_fallback", _wrapper),
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _method),
        patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _no_fallback),
    ):
        yield


async def test_ensure_historical_spots_anchors_on_local_day(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The dynamic YTD spot backfill must anchor day boundaries on local
    midnight (in UTC), matching the recorder window. In winter Brussels
    is UTC+1, so local Jan 1 00:00 == Dec 31 23:00 UTC; a UTC-midnight
    anchor would skip that hour and never credit the first hour of the
    local year. Verify the first fetch window starts at the local Jan 1
    boundary."""
    freezer.move_to("2026-01-10 12:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    captured: list[tuple[datetime, datetime]] = []

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        captured.append((start, end))
        return {}

    with _patch_spot_fetch(_fake_fetch):
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 3))

    assert captured
    # Local Jan 1 00:00 CET = 2025-12-31 23:00 UTC, not 2026-01-01 00:00 UTC.
    assert captured[0][0] == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)


async def test_month_mean_does_not_overweight_a_quarter_hourly_today(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The two halves of a month mean need not be on the same grid.

    The persisted cache is hourly by construction; today's freshly fetched
    curve is whatever the contract settles on, which for a quarter-hourly one
    is four keys an hour. Averaging the union unweighted counted today four
    times over against every other day of the month, so the flat monthly rate
    was dragged toward whichever day the dialog happened to be opened on."""

    freezer.move_to("2026-08-15 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    # Fourteen earlier days of the month cached hourly at 0.10.
    for day in range(1, 15):
        base = datetime(2026, 8, day, 0, 0, tzinfo=UTC)
        for h in range(24):
            coord._historical_spots[base + timedelta(hours=h)] = 0.10

    # Today, quarter-hourly, at a very different level.
    today_q: dict[datetime, float] = {}
    base = datetime(2026, 8, 15, 0, 0, tzinfo=UTC)
    for slot in range(24 * 4):
        today_q[base + timedelta(minutes=15 * slot)] = 0.30

    # The same day handed over as 24 hourly keys is the control: which grid
    # the curve arrives on must not change what the month averaged to.
    today_h = {base + timedelta(hours=h): 0.30 for h in range(24)}

    as_quarters = coord._monthly_spot_mean(2026, 8, today_q)
    as_hours = coord._monthly_spot_mean(2026, 8, today_h)
    assert as_quarters is not None and as_hours is not None
    assert as_quarters == pytest.approx(as_hours)
    # And the answer is the hourly one, not today counted four times over:
    # 336 h at 0.10 plus today's 22 in-month hours at 0.30 is 0.1123, while
    # weighting today's 88 in-month slots against them gives 0.1415.
    assert as_quarters == pytest.approx(0.1123, abs=1e-4)


async def test_ensure_historical_spots_requests_the_contract_grid(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The replay must be priced off the series the contract settles on.

    ENTSO-E publishes Belgium as two products, a PT60M and a PT15M series for
    the same delivery period, and parse_day_ahead_xml refuses to blend them:
    hourly mode takes the hourly product, quarter mode the 15-minute one. This
    fetch omitted the flag, so a quarter-hourly contract replayed its whole
    year off a different auction than the one its live price came from, while
    the live fetch asked for the right grid all along."""

    freezer.move_to("2026-01-10 12:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)

    async def _ask(quarter: bool) -> list[bool]:
        coord = BePricesCoordinator(hass, entry)
        coord._snapshot = make_snapshot(
            energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=quarter)
        )
        seen: list[bool] = []

        async def _fake_fetch(
            start: datetime, end: datetime, *, quarter_hourly: bool = False
        ) -> dict[datetime, float]:
            seen.append(quarter_hourly)
            return {}

        with _patch_spot_fetch(_fake_fetch):
            await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 3))
        return seen

    assert await _ask(True) == [True]
    # And an hourly-billed dynamic contract still gets the hourly product.
    assert await _ask(False) == [False]


async def test_ensure_historical_spots_stores_quarters_by_hour(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A 15-minute response is cached as one price per clock hour, on the mean.

    The recorder only keeps hourly consumption, so an hour is the finest thing
    a replay can price, and every reader of this cache (the year-to-date walk,
    the backfill, the 20-of-24 completeness test, the persisted form) assumes
    one key per hour. The mean is exact for every formula that is linear in
    the spot, so pricing the hour's mean equals replaying each quarter against
    a quarter of that hour's kWh. A floored feed-in formula is the one that is
    not, and it keeps a sibling cache; this contract has no floor, so it must
    grow nothing."""

    freezer.move_to("2026-01-10 12:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=True)
    )
    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    ramp = {
        hour: 0.05,
        hour + timedelta(minutes=15): 0.15,
        hour + timedelta(minutes=30): 0.25,
        hour + timedelta(minutes=45): 0.35,
    }

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        return dict(ramp)

    with _patch_spot_fetch(_fake_fetch):
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))

    assert [k for k in coord._historical_spots if k.minute or k.second] == []
    assert coord._historical_spots[hour] == pytest.approx(0.20)
    assert coord._historical_spot_quarters == {}


def _floored_quarter_entry() -> MockConfigEntry:
    """An expert custom entry that bills per quarter-hour and floors its
    feed-in formula, the one shape whose hour is not priced by its mean."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "custom",
            "contract": "custom_dynamic",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "dynamic",
            "solar_regime": "injection",
            "api_key": "test-token",
        },
    )


def _floored_quarter_snapshot() -> Any:
    return make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=True),
        injection=InjectionRates(
            factor=1.0, base=0.0, current=None, floor_at_zero=True
        ),
    )


async def test_ensure_historical_spots_caches_quarters_for_a_floored_entry(
    hass: HomeAssistant, freezer: Any
) -> None:
    """max() is convex, so the hour mean does not price a floored feed-in
    formula: the hour is worth the mean of its quarters' floored rates, which
    only the quarters themselves can answer.

    Kept beside the hourly mean rather than instead of it, so every other
    reader of the cache is untouched."""
    freezer.move_to("2026-01-10 12:00:00+01:00")
    entry = _floored_quarter_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = _floored_quarter_snapshot()
    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    ramp = {
        hour: 0.05,
        hour + timedelta(minutes=15): 0.15,
        hour + timedelta(minutes=30): 0.25,
        hour + timedelta(minutes=45): 0.35,
    }

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        return dict(ramp)

    with _patch_spot_fetch(_fake_fetch):
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))

    assert coord._historical_spot_quarters[hour] == [0.05, 0.15, 0.25, 0.35]
    # The hourly mean is still there and still the mean of those slots.
    assert coord._historical_spots[hour] == pytest.approx(0.20)


async def test_the_tick_hands_the_quarter_cache_to_the_year_cost(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The one line that carries the whole fix to the number the user reads.

    Everything under it can be right and current_year_cost still credit the
    hour mean, because the year-to-date walk only sees the slots if the tick
    passes them."""
    freezer.move_to("2026-07-01 10:30:00+00:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "custom",
            "contract": "custom_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "injection",
            "api_key": "TESTKEY",
        },
        title="Custom floored quarter-hourly",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = _floored_quarter_snapshot()
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(  # type: ignore[method-assign]
        return_value={datetime(2026, 7, 1, h, tzinfo=UTC): 0.30 for h in range(24)}
    )
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    coord._historical_spot_quarters = {
        datetime(2026, 1, 6, 13, tzinfo=UTC): [-0.060, -0.020, 0.010, 0.050]
    }
    spy = AsyncMock(return_value=0.0)
    with (
        patch(
            "custom_components.be_electricity_prices.coordinator."
            "_compute_current_year_cost",
            spy,
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        await coord._update_body()

    assert spy.await_args is not None
    assert spy.await_args.kwargs["spot_quarters"] is coord._historical_spot_quarters


async def test_quarters_are_dropped_when_the_entry_stops_needing_them(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Unticking the quarter-hourly box or the floor leaves the supplier,
    contract and region alone, which is all the reload gate looks at.

    So a cached year would be restored and re-persisted for the life of the
    entry, and the replay would go on crediting those hours per slot while the
    injection_price sensor beside it credits the hour: the very divergence the
    cache was added to remove, pointing the other way."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = _floored_quarter_entry()
    entry.add_to_hass(hass)
    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    async def _after(snapshot: Any, api_key: str | None) -> dict[datetime, list[float]]:
        coord = BePricesCoordinator(hass, entry)
        coord._snapshot = snapshot
        coord._historical_spot_quarters = {hour: [0.05, 0.15, 0.25, 0.35]}
        coord._historical_spots = {hour: 0.20}
        coord._complete_spot_days = {date(2026, 1, 1)}
        with _patch_spot_fetch(AsyncMock(return_value={})):
            await coord._ensure_historical_spots(
                date(2026, 1, 1), date(2026, 1, 1), api_key
            )
        return coord._historical_spot_quarters

    hourly_billed = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0),
        injection=InjectionRates(
            factor=1.0, base=0.0, current=None, floor_at_zero=True
        ),
    )
    assert await _after(hourly_billed, "test-token") == {}
    # An entry with no key still replays what is cached, so the drop cannot
    # sit below the key check.
    assert await _after(hourly_billed, None) == {}
    # And an entry that still needs them keeps them.
    assert await _after(_floored_quarter_snapshot(), "test-token") != {}


async def test_a_complete_hourly_day_is_refetched_when_its_quarters_are_missing(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The upgrade path. An affected entry already holds 24 hourly spots a
    day, and a day holding 20 of 24 is never re-fetched, so without measuring
    coverage against the cache the entry actually replays from, the quarters
    would stay empty for the rest of the year."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = _floored_quarter_entry()
    entry.add_to_hass(hass)
    day_start = datetime(2025, 12, 31, 23, 0, tzinfo=UTC)
    seeded = {day_start + timedelta(hours=h): 0.05 for h in range(24)}

    async def _fetches(snapshot: Any) -> int:
        coord = BePricesCoordinator(hass, entry)
        coord._snapshot = snapshot
        coord._historical_spots = dict(seeded)
        calls = 0

        async def _fake_fetch(
            start: datetime, end: datetime, *, quarter_hourly: bool = False
        ) -> dict[datetime, float]:
            nonlocal calls
            calls += 1
            return {
                start + timedelta(hours=h, minutes=15 * q): 0.05
                for h in range(24)
                for q in range(4)
            }

        with _patch_spot_fetch(_fake_fetch):
            await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
            after_fill = calls
            # Now that the quarters are cached the day is covered again.
            await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
            assert calls == after_fill
        return calls

    assert await _fetches(_floored_quarter_snapshot()) == 1
    # An entry that replays off the hourly mean sees the seeded day as
    # complete and fetches nothing, exactly as it always did.
    assert (
        await _fetches(
            make_snapshot(
                energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=True)
            )
        )
        == 0
    )


async def test_ensure_historical_spots_skips_permanently_short_day(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A stable past day that ENTSO-E only ever returns < 20 hours for
    must not be re-fetched on every tick: after one short fetch it is
    marked and skipped until the TTL expires."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    calls: list[tuple[datetime, datetime]] = []

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        calls.append((start, end))
        # Only five hours come back -> the day stays short (< 20).
        return {start + timedelta(hours=h): 0.05 for h in range(5)}

    with _patch_spot_fetch(_fake_fetch):
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
        first = len(calls)
        assert date(2026, 1, 1) in coord._spot_day_retry_at
        # Second call within the TTL must not re-fetch the short day.
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
        assert len(calls) == first
        # A permanently short day is never recorded as complete.
        assert date(2026, 1, 1) not in coord._complete_spot_days


async def test_ensure_historical_spots_backs_off_after_a_rejected_key(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A refused key must not re-pull the whole year on every tick.

    A failed fetch leaves each day exactly as short as it was, and the
    short-day marker was only written after a SUCCESSFUL fetch, so a revoked
    token or an exhausted daily quota re-requested every week-chunk of the
    year on every hourly tick and logged a warning for each.

    The two classes still part company at the fallback. A rejected credential
    has to keep raising its Repairs card, so the keyless source is never asked
    for it; a timeout or a 5xx is exactly what that source is there for, and
    the days come back filled with nothing left to retry. A window NEITHER
    source could serve is held by the shorter outage TTL instead --
    test_a_window_neither_source_could_serve_is_not_re_walked_hourly."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)

    async def _keyless_answered(
        _self: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        hours = int((end - start).total_seconds() // 3600)
        return {start + timedelta(hours=h): 0.05 for h in range(hours)}

    async def _refuse(exc: Exception) -> tuple[int, bool]:
        """Re-fetches on the next tick, and whether the keyless leg filled it."""
        coord = BePricesCoordinator(hass, entry)
        calls = 0

        async def _entsoe_refuses(
            _self: Any,
            start: datetime,
            end: datetime,
            *,
            quarter_hourly: bool = False,
        ) -> dict[datetime, float]:
            nonlocal calls
            calls += 1
            raise exc

        with (
            patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_refuses),
            patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _keyless_answered),
        ):
            await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 3))
            first = calls
            assert first > 0
            await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 3))
        return calls - first, bool(coord._historical_spots)

    refetched, filled = await _refuse(EntsoeAuthError("rejected"))
    assert refetched == 0, "a revoked token must not re-pull the year every tick"
    assert not filled, "and must not be papered over by the keyless source"

    refetched, filled = await _refuse(EntsoeError("timeout"))
    assert filled, "a 5xx is what the keyless source is there for"
    assert refetched == 0, "a filled day is not short, so there is nothing to retry"


async def test_ensure_historical_spots_records_and_skips_complete_days(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A local day holding >= 20 cached spot hours is recorded complete and
    is neither re-fetched nor re-scanned on later ticks."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    calls: list[tuple[datetime, datetime]] = []

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        calls.append((start, end))
        # A full local day: 24 hours from the local-midnight anchor.
        return {start + timedelta(hours=h): 0.05 for h in range(24)}

    with _patch_spot_fetch(_fake_fetch):
        # First pass fetches to fill the empty day.
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
        assert len(calls) == 1
        # The next pass sees the full day, records it complete, no re-fetch.
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
        assert date(2026, 1, 1) in coord._complete_spot_days
        assert len(calls) == 1
        # Once complete the day is skipped: dropping its cached hours does not
        # trigger a rescan-driven re-fetch.
        for h in range(24):
            coord._historical_spots.pop(
                datetime(2025, 12, 31, 23, tzinfo=UTC) + timedelta(hours=h), None
            )
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 1, 1))
        assert len(calls) == 1


async def test_build_hourly_covers_both_days_across_dst_seams(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The static price table must cover today + tomorrow in full on both
    DST seams. Fall-back Sunday has 25 local hours today, so a fixed
    48-slot walk left only 23 UTC slots for tomorrow and dropped its
    23:00; spring-forward has 23 local hours today. Assert the local-hour
    distribution directly (invariant I11)."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot()

    # Fall-back: today (Oct 25) has 25 local hours, tomorrow (Oct 26) 24.
    freezer.move_to("2026-10-25 12:00:00+01:00")
    hourly = coord._build_hourly(coord._snapshot, {})
    today = sorted(
        dt_util.as_local(k)
        for k in hourly
        if dt_util.as_local(k).date() == date(2026, 10, 25)
    )
    tomorrow = sorted(
        dt_util.as_local(k).hour
        for k in hourly
        if dt_util.as_local(k).date() == date(2026, 10, 26)
    )
    assert len(today) == 25
    assert tomorrow == list(range(24))  # all 24 incl the previously-dropped 23:00

    # Spring-forward: today (Mar 29) has 23 local hours, tomorrow 24.
    freezer.move_to("2026-03-29 12:00:00+02:00")
    hourly = coord._build_hourly(coord._snapshot, {})
    today = sorted(
        dt_util.as_local(k)
        for k in hourly
        if dt_util.as_local(k).date() == date(2026, 3, 29)
    )
    tomorrow = sorted(
        dt_util.as_local(k).hour
        for k in hourly
        if dt_util.as_local(k).date() == date(2026, 3, 30)
    )
    assert len(today) == 23
    assert tomorrow == list(range(24))


async def test_fetch_spot_prices_window_covers_local_day_on_dst_fallback(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Brussels fall-back Sunday (Oct 25 2026) has 25 local hours but
    a naive ``end = start + timedelta(days=N)`` only walks 24 UTC hours,
    leaving the last local hour (23:00 CET = Oct 25 22:00 UTC) outside
    the fetched window. Pin a morning hour so want_tomorrow=False and
    confirm the request reaches the actual local Oct 26 midnight."""
    freezer.move_to("2026-10-25 09:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    captured: dict[str, datetime] = {}

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        captured["start"] = start
        captured["end"] = end
        return {}

    with _patch_spot_fetch(_fake_fetch):
        await coord._fetch_spot_prices()

    # Local Oct 25 00:00 CEST = Oct 24 22:00 UTC; local Oct 26 00:00 CET
    # = Oct 25 23:00 UTC (25-hour day spans 25 UTC hours).
    assert captured["start"] == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert captured["end"] == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)


async def test_fetch_spot_prices_tomorrow_flag_follows_response_content(
    hass: HomeAssistant, freezer: Any
) -> None:
    """ENTSO-E publishes the day-ahead curve around 12-13 CET. An 11:00
    tick that asks for tomorrow can come back with today only; the
    cache flag must reflect what we GOT so the next hourly tick re-
    fetches once publication lands -- otherwise tomorrow's prices stay
    missing until local midnight and only an entry reload pulls them
    in (GitHub issue #29)."""
    freezer.move_to("2026-05-28 11:30:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    # 2026-05-28 00:00 CEST == 2026-05-27 22:00 UTC.
    base = datetime(2026, 5, 27, 22, 0, tzinfo=UTC)
    today_only = {base + timedelta(hours=h): 0.10 for h in range(24)}
    today_plus_tomorrow = {base + timedelta(hours=h): 0.10 for h in range(48)}

    async def _fake_pre(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        return today_only

    async def _fake_post(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        return today_plus_tomorrow

    # Pre-publication tick: response carries today only -> flag stays
    # False so the next tick will retry.
    with _patch_spot_fetch(_fake_pre):
        await coord._fetch_spot_prices()
    assert coord._spot_cache_includes_tomorrow is False

    # The False flag forces the cache check to miss on the next call,
    # mirroring the next hourly coordinator tick.
    with _patch_spot_fetch(_fake_post):
        await coord._fetch_spot_prices()
    assert coord._spot_cache_includes_tomorrow is True

    # Once True, the cache short-circuits and ENTSO-E is not re-hit.
    fetch_calls = 0

    async def _fake_should_not_run(*_a: object, **_kw: object) -> dict[datetime, float]:
        nonlocal fetch_calls
        fetch_calls += 1
        return {}

    with _patch_spot_fetch(_fake_should_not_run):
        result = await coord._fetch_spot_prices()
    assert fetch_calls == 0
    assert result == today_plus_tomorrow


async def test_fetch_spot_prices_uses_quarter_hourly_for_quarter_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A dynamic snapshot flagged ``quarter_hourly`` routes the live
    ENTSO-E fetch to the native 15-minute grid (Engie); a plain hourly
    dynamic snapshot keeps the hourly aggregate."""
    freezer.move_to("2026-05-28 09:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "engie",
            "contract": "engie_dynamic",
            "region": "flanders",
            "dso": "fluvius",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    captured: dict[str, bool] = {}

    async def _fake_fetch(
        start: datetime, end: datetime, *, quarter_hourly: bool = False
    ) -> dict[datetime, float]:
        captured["quarter_hourly"] = quarter_hourly
        return {}

    coord._snapshot = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=True)
    )
    with _patch_spot_fetch(_fake_fetch):
        await coord._fetch_spot_prices()
    assert captured["quarter_hourly"] is True

    # An hourly dynamic snapshot (the common case) keeps the aggregate.
    coord._spot_cache = {}
    coord._spot_cache_day = None
    coord._snapshot = make_snapshot(
        energy=DynamicRates(factor=1.0, base=0.0, quarter_hourly=False)
    )
    with _patch_spot_fetch(_fake_fetch):
        await coord._fetch_spot_prices()
    assert captured["quarter_hourly"] is False


async def test_force_refresh_drops_caches_and_requests_update(
    hass: HomeAssistant,
) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    sentinel = object()
    coord._snapshot_fetched_at = sentinel  # type: ignore[assignment]
    coord._spot_cache = {object(): 0.10}  # type: ignore[dict-item]
    coord._spot_cache_day = date(2026, 4, 29)
    coord._spot_cache_includes_tomorrow = True
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.async_force_refresh()

    # fetched_at is intentionally preserved so _save_persistent can
    # still write the cached snapshot if the forced refresh fails.
    assert coord._snapshot_fetched_at is sentinel
    assert coord._force_refresh is True
    assert coord._spot_cache == {}
    assert coord._spot_cache_day is None
    assert coord._spot_cache_includes_tomorrow is False
    coord.async_request_refresh.assert_awaited_once()


def _fake_snapshot(supplier: str = "eneco") -> SupplierSnapshot:
    return make_snapshot(supplier=supplier, contract="power_fix")


async def test_two_coordinators_share_snapshot_and_only_fetch_once(
    hass: HomeAssistant,
) -> None:
    """Two entries pointing at the same (supplier, contract, region) must
    share the snapshot — extractor.fetch may run for the first one only."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)

    fetched = _fake_snapshot()
    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return fetched

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()
        await coord_b._maybe_refresh_snapshot()

    assert fetch_calls == 1
    assert coord_a._snapshot is fetched
    assert coord_b._snapshot is fetched


async def test_force_refresh_keeps_snapshot_when_refetch_fails(
    hass: HomeAssistant,
) -> None:
    """A failing forced refetch must not blank the cached snapshot or
    its fetched_at marker, so _save_persistent can still write the
    blob to disk and survive an HA restart before the next attempt."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    initial_call = True

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        if initial_call:
            return _fake_snapshot()
        raise ExtractorError("boom")

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()
    assert coord._snapshot is not None
    initial_fetched_at = coord._snapshot_fetched_at
    initial_snapshot = coord._snapshot

    await coord.async_force_refresh()
    assert coord._force_refresh is True

    initial_call = False
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    # Force flag remains set so the next tick retries; cached snapshot
    # is intact so _save_persistent can still write it.
    assert coord._force_refresh is True
    assert coord._snapshot is initial_snapshot
    assert coord._snapshot_fetched_at is initial_fetched_at


async def test_force_refresh_evicts_shared_cache_for_other_coordinator(
    hass: HomeAssistant,
) -> None:
    """async_force_refresh on entry A must evict the shared (supplier,
    contract, region) entry, so entry B's next refresh re-fetches."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)
    coord_a.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()  # populates the shared cache
        assert fetch_calls == 1
        await coord_a.async_force_refresh()  # evicts; calls async_request_refresh
        await coord_b._maybe_refresh_snapshot()  # must re-fetch
        assert fetch_calls == 2


async def test_pruning_spots_keeps_the_same_dict_object(
    hass: HomeAssistant, freezer: Any
) -> None:
    """_ensure_historical_spots merges each fetched chunk into
    self._historical_spots and re-resolves the attribute after every await.

    A prune landing between two chunks (the tick calls it from
    _save_persistent while a backfill is mid-fetch) used to REBIND the
    attribute, so everything the earlier chunks had merged into the old dict
    was silently discarded. Pruning in place keeps every holder on one dict.
    """
    freezer.move_to("2026-01-02 12:00:00+01:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    before = coord._historical_spots
    before[datetime(2025, 6, 1, 10, tzinfo=UTC)] = 0.05  # prior year, prunable
    before[datetime(2026, 1, 1, 10, tzinfo=UTC)] = 0.06  # current year, kept
    before_quarters = coord._historical_spot_quarters
    before_quarters[datetime(2025, 6, 1, 10, tzinfo=UTC)] = [0.05] * 4
    before_quarters[datetime(2026, 1, 1, 10, tzinfo=UTC)] = [0.06] * 4

    coord._prune_historical_spots()

    assert coord._historical_spots is before, "prune rebound the dict"
    assert datetime(2025, 6, 1, 10, tzinfo=UTC) not in before
    assert before[datetime(2026, 1, 1, 10, tzinfo=UTC)] == 0.06
    # The sibling cache is pruned the same way, and in place for the same
    # reason: a fetch mid-flight holds a reference to it too.
    assert coord._historical_spot_quarters is before_quarters
    assert datetime(2025, 6, 1, 10, tzinfo=UTC) not in before_quarters
    assert before_quarters[datetime(2026, 1, 1, 10, tzinfo=UTC)] == [0.06] * 4


async def test_probe_match_through_the_shared_cache_refreshes_fetched_at(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A probe match must restamp the age clock even when the snapshot came
    back through the shared-cache shortcut.

    That shortcut is tried BEFORE the self-fresh branch, and in steady state
    the shared row is this coordinator's own row, so it is the path that
    actually runs every tick. Leaving the stamp alone pinned it at the
    cold-fetch instant for as long as the supplier published the same card:
    cards are monthly, so after seven days every probe-based supplier raised a
    false "snapshot stale" repair while the card had been verified minutes
    earlier.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _probe(*_a: object, **_k: object) -> str:
        return "same-card"

    from dataclasses import replace as _replace

    extractor = _replace(
        make_stub_extractor(fetch=AsyncMock(return_value=_fake_snapshot())),
        probe=_probe,
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        freezer.move_to("2026-08-01 09:00:00+00:00")
        await coord._maybe_refresh_snapshot()  # cold fetch, seeds the shared row
        first = coord._snapshot_fetched_at

        freezer.move_to("2026-08-10 09:00:00+00:00")
        await coord._maybe_refresh_snapshot()  # probe matches, adopts shared

    assert coord._snapshot_fetched_at is not None
    assert first is not None
    assert coord._snapshot_fetched_at > first, "age clock never moved"
    assert coord._snapshot_age_hours() < 1.0
    # The shared row is restamped too, so a sibling sees the same age.
    from custom_components.be_electricity_prices.snapshot_store import _shared_snapshots

    assert _shared_snapshots(hass)[coord._shared_key()].fetched_at == (
        coord._snapshot_fetched_at
    )


async def test_force_refresh_drops_the_per_month_archive_rows(
    hass: HomeAssistant,
) -> None:
    """The refresh service must clear the per-month archive cache too.

    The year-to-date walk runs Jan 1 through today INCLUSIVE, so the CURRENT
    delivery month is cached there with no TTL. A supplier that re-issues
    this month's card under the same month (Eneco publishes corrected
    volumes) would otherwise keep being billed from the first card fetched
    for the life of the HA process, and this service, whose whole purpose is
    to pick up a corrected card, could not clear it.
    """
    from custom_components.be_electricity_prices.snapshot_store import (
        _monthly_failed_fetches,
        _monthly_snapshots,
    )

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    key = coord._shared_key()
    mine = (key[0], key[1], key[2], "2026-08")
    other = ("someone_else", key[1], key[2], "2026-08")
    _monthly_snapshots(hass)[mine] = _fake_snapshot()
    _monthly_snapshots(hass)[other] = _fake_snapshot()
    _monthly_failed_fetches(hass)[mine] = datetime(2026, 8, 1, tzinfo=UTC)

    await coord.async_force_refresh()

    assert mine not in _monthly_snapshots(hass)
    assert mine not in _monthly_failed_fetches(hass)
    # Another supplier's rows are none of this entry's business.
    assert other in _monthly_snapshots(hass)


async def test_force_refresh_not_defeated_by_sibling_cache(
    hass: HomeAssistant,
) -> None:
    """Regression for c073448: A's async_force_refresh pops the shared
    cache row and sets _force_refresh, then sibling B re-seeds the
    shared cache from its own already-warm snapshot before A's next
    tick. _shared_is_fresh must return False under _force_refresh so
    A still calls extractor.fetch instead of silently adopting B's
    snapshot. Without the guard, the user-facing be_electricity_prices.
    refresh service is a no-op on multi-entry installs."""
    from custom_components.be_electricity_prices.snapshot_store import (
        _SharedSnapshot,
        _shared_snapshots,
    )

    entry_a = _entry()
    entry_a.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_a.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        # A starts the user-initiated refresh: own snapshot blanked,
        # shared row popped, _force_refresh raised.
        await coord_a.async_force_refresh()
        # Simulate sibling B re-seeding the shared cache between A's
        # pop and A's next tick (the actual race the regression covers).
        cache = _shared_snapshots(hass)
        cache[coord_a._shared_key()] = _SharedSnapshot(
            snapshot=_fake_snapshot(),
            fetched_at=dt_util.utcnow(),
            probe_key=None,
        )
        # A's next refresh tick must NOT adopt B's seed; it must fetch.
        await coord_a._maybe_refresh_snapshot()

    assert fetch_calls == 1, (
        f"force_refresh should still fetch even when sibling re-seeded; "
        f"saw {fetch_calls} fetches"
    )


async def test_force_refresh_not_defeated_by_sibling_failure_marker(
    hass: HomeAssistant,
) -> None:
    """Symmetric to test_force_refresh_not_defeated_by_sibling_cache:
    a sibling that fails between A's async_force_refresh clear and A's
    next tick re-populates _shared_failed_fetches[key]. The negative-
    cache short-circuit must NOT fire for A's force-refresh tick or the
    user-facing be_electricity_prices.refresh service silently no-ops
    for up to _SHARED_FAILURE_TTL (5 min)."""
    from custom_components.be_electricity_prices.snapshot_store import (
        _shared_failed_fetches,
    )

    entry_a = _entry()
    entry_a.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_a.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        # Step 1: user-initiated refresh sets _force_refresh and
        # clears the (own view of the) failure marker.
        await coord_a.async_force_refresh()
        # Step 2: sibling fails; re-populates the shared failure
        # marker with a recent timestamp.
        _shared_failed_fetches(hass)[coord_a._shared_key()] = (
            dt_util.utcnow(),
            "transient sibling failure",
            1,
        )
        # Step 3: A's next refresh tick must NOT short-circuit on the
        # sibling marker; it must call extractor.fetch.
        await coord_a._maybe_refresh_snapshot()

    assert fetch_calls == 1, (
        f"force_refresh should bypass the negative cache; saw {fetch_calls} fetches"
    )


async def test_shared_cache_expires_after_ttl(hass: HomeAssistant) -> None:
    """Snapshots older than SNAPSHOT_REFRESH_HOURS (24h) must be re-fetched."""
    entry_a = _entry()
    entry_a.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry_a)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()
        # Hand-age the shared entry past the TTL.
        cache = _shared_snapshots(hass)
        key = coord._shared_key()
        cache[key].fetched_at = dt_util.utcnow().replace(year=2020)
        coord._snapshot_fetched_at = cache[key].fetched_at
        await coord._maybe_refresh_snapshot()
        assert fetch_calls == 2


async def test_probe_match_skips_fetch(hass: HomeAssistant) -> None:
    """When extractor.probe returns the same key on a subsequent refresh,
    the coordinator must NOT call extractor.fetch again."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0
    probe_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        nonlocal probe_calls
        probe_calls += 1
        return "key-stable"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # first call - fetch
        await coord._maybe_refresh_snapshot()  # probe says unchanged - no fetch
        await coord._maybe_refresh_snapshot()  # idem
    assert fetch_calls == 1
    assert probe_calls == 3
    assert coord._snapshot_probe_key == "key-stable"


async def test_probe_confirmed_recovery_clears_stale_failure(
    hass: HomeAssistant,
) -> None:
    """A probe-confirmed-fresh tick must clear a stale _last_error and the
    negative-cache marker left by an earlier transient fetch failure. The
    probe path never re-fetches, so on a single-entry install (no sibling
    to trigger _adopt_shared) the extractor Repairs card would otherwise
    linger until the supplier published a new card."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "key-stable"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # fetch, stores probe_key
        # Drop the shared snapshot so the next tick can't take the
        # _adopt_shared shortcut (which already clears state); this forces
        # the _self_is_fresh path, the one that used to leave the failure
        # stuck. Mirrors a single-entry install after a reload eviction.
        key = coord._shared_key()
        _shared_snapshots(hass).pop(key, None)
        # Simulate a transient failure that left the card + marker up.
        coord._last_error = "TimeoutError"
        _shared_failed_fetches(hass)[key] = (dt_util.utcnow(), "TimeoutError", 3)
        await coord._maybe_refresh_snapshot()  # probe unchanged -> recovers

    assert coord._last_error == ""
    assert key not in _shared_failed_fetches(hass)


async def test_probe_mismatch_triggers_fetch(hass: HomeAssistant) -> None:
    """When extractor.probe returns a different key, the coordinator
    must refetch even if the snapshot is still within TTL."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0
    keys = iter(["key-A", "key-B"])

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return next(keys)

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # key-A, fetch
        await coord._maybe_refresh_snapshot()  # key-B, refetch
    assert fetch_calls == 2
    assert coord._snapshot_probe_key == "key-B"


async def test_probe_none_self_fresh_does_not_reset_fetched_at(
    hass: HomeAssistant,
) -> None:
    """The TTL fallback must elapse based on the *real* fetch time.

    A persisted snapshot loaded from disk with the shared cache empty
    (typical state right after an HA restart) hits the self-fresh
    branch in _maybe_refresh_snapshot. That branch used to stamp
    _snapshot_fetched_at = now on every tick that passed the TTL
    check, which reset the TTL clock and the supplier was never
    re-fetched. Probe-less suppliers must keep the original
    fetched_at so the 24h TTL actually triggers.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return None  # probe-less supplier (or probe failed)

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    # Simulate a post-restart state: snapshot loaded from disk, shared
    # cache (in-memory) empty. fetched_at is well within TTL.
    # Both, the way _set_snapshot always writes them: the shared cache is
    # seeded from the RAW card, so the freshness gate reads the raw one too.
    snap = _fake_snapshot()
    coord._snapshot = snap
    coord._snapshot_raw = snap
    original_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    coord._snapshot_fetched_at = original_fetched_at

    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    # The self-fresh return must not move fetched_at forward; doing so
    # resets the TTL clock and the snapshot would never expire.
    assert coord._snapshot_fetched_at == original_fetched_at


async def test_self_fresh_populates_empty_shared_cache(
    hass: HomeAssistant,
) -> None:
    """Post-restart, the shared cache is empty; the self-fresh return
    must populate it so a sibling coord on the same tuple can adopt
    without re-running its own probe / TTL check on every tick."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "stable-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    coord._set_snapshot(_fake_snapshot())
    coord._snapshot_probe_key = "stable-key"
    coord._snapshot_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    assert _shared_snapshots(hass).get(coord._shared_key()) is None

    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    shared = _shared_snapshots(hass).get(coord._shared_key())
    assert shared is not None
    # The shared row carries the card as parsed, which siblings resolve
    # against their own VAT preference; on a VAT-incl card that is the
    # very object this entry prices against.
    assert shared.snapshot is coord._snapshot_raw
    assert shared.snapshot is coord._snapshot
    assert shared.probe_key == "stable-key"


async def test_probe_match_self_fresh_refreshes_fetched_at(
    hass: HomeAssistant,
) -> None:
    """Probe-based suppliers can stamp fetched_at on a probe match -- we
    just verified the supplier hasn't published a new card, so the
    snapshot_age sensor should reset to "just checked"."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "stable-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(AsyncMock()), "probe": staticmethod(_fake_probe)},
    )

    # Both, the way _set_snapshot always writes them: the shared cache is
    # seeded from the RAW card, so the freshness gate reads the raw one too.
    snap = _fake_snapshot()
    coord._snapshot = snap
    coord._snapshot_raw = snap
    coord._snapshot_probe_key = "stable-key"
    old_fetched_at = dt_util.utcnow() - timedelta(hours=12)
    coord._snapshot_fetched_at = old_fetched_at

    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    assert coord._snapshot_fetched_at is not None
    assert coord._snapshot_fetched_at > old_fetched_at


async def test_probe_none_falls_back_to_ttl(hass: HomeAssistant) -> None:
    """A None probe (extractor doesn't expose one, or probe failed) keeps
    the existing 24h-TTL behaviour: don't refetch within the window."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return None  # no probe available

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()  # fetch, fresh
        await coord._maybe_refresh_snapshot()  # within TTL, no fetch
        # Hand-age past TTL: must refetch even though probe returned None.
        coord._snapshot_fetched_at = dt_util.utcnow().replace(year=2020)
        _shared_snapshots(hass)[
            coord._shared_key()
        ].fetched_at = coord._snapshot_fetched_at
        await coord._maybe_refresh_snapshot()
    assert fetch_calls == 2


async def test_probe_match_on_shared_cache_avoids_fetch(hass: HomeAssistant) -> None:
    """A second coordinator with the same shared key must adopt the
    sibling's snapshot when its probe returns the matching key, even
    if its own snapshot is None."""
    entry_a = _entry()
    entry_b = _entry()
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, entry_a)
    coord_b = BePricesCoordinator(hass, entry_b)

    fetch_calls = 0

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        nonlocal fetch_calls
        fetch_calls += 1
        return _fake_snapshot()

    async def _fake_probe(*_args: object, **_kwargs: object) -> str | None:
        return "shared-key"

    extractor = type(
        "E",
        (),
        {"fetch": staticmethod(_fake_fetch), "probe": staticmethod(_fake_probe)},
    )
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord_a._maybe_refresh_snapshot()  # populates cache + probe key
        await coord_b._maybe_refresh_snapshot()  # adopts via probe-key match
    assert fetch_calls == 1
    assert coord_b._snapshot_probe_key == "shared-key"


async def test_sync_stale_issue_creates_and_clears(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"snapshot_stale_{entry.entry_id}"

    coord._sync_stale_issue(True)
    registry = ir.async_get(hass)
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    coord._sync_stale_issue(False)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_sync_deprecated_supplier_issue_creates_and_clears(
    hass: HomeAssistant, freezer: Any
) -> None:
    """An entry on a supplier that has announced its exit gets a Repairs
    card naming the successor and the date, and an entry on any other
    supplier gets none. WHETHER the card shows is driven by the registry
    flag alone; only its tense reads the clock, so this is frozen before
    DATS 24's end date or it would flip variant on 2026-09-01."""
    freezer.move_to("2026-08-06 12:00:00+02:00")
    entry = make_entry(
        supplier="dats24", contract="dats24_groen_variabel", region="flanders"
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"supplier_deprecated_{entry.entry_id}"

    coord._sync_deprecated_supplier_issue()
    registry = ir.async_get(hass)
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    # Labels, not registry ids: the card tells the user which entry to pick
    # from the config flow's label-based supplier dropdown.
    assert issue.translation_placeholders == {
        "supplier": "DATS 24",
        "successor": "EnergyVision",
        "ends_on": "2026-08-31",
    }
    assert issue.translation_key == "supplier_deprecated"

    # A supplier with no lifecycle flag must not raise the card, and must
    # clear one left behind by a previous supplier on the same entry.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "supplier": "eneco", "contract": "power_fix"}
    )
    coord._sync_deprecated_supplier_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_extractor_cards_are_suppressed_once_supply_ended(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Past its end date a withdrawn supplier stops publishing, so the fetch
    failing is expected, not news.

    Reporting it stacks an alarming "could not reach the supplier" card on
    top of the deprecation card that already explains the situation and says
    what to do, leaving the user to work out that the two describe one
    event. DATS 24 stops supplying on 2026-08-31.
    """
    entry = make_entry(
        supplier="dats24", contract="dats24_groen_variabel", region="flanders"
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    registry = ir.async_get(hass)
    failed_id = f"extractor_failed_{entry.entry_id}"
    unreachable_id = f"extractor_unreachable_{entry.entry_id}"

    # Before the date the cards still publish, so a failure IS a regression
    # worth surfacing. Suppressing it early would hide a real break.
    freezer.move_to("2026-08-30 12:00:00+02:00")
    coord._sync_extractor_issue("404 fetching the August card")
    assert registry.async_get_issue(DOMAIN, failed_id) is not None

    # On the last supplied day it is still a real failure.
    freezer.move_to("2026-08-31 23:00:00+02:00")
    coord._sync_extractor_issue("404 fetching the August card")
    assert registry.async_get_issue(DOMAIN, failed_id) is not None

    # The day after, the same 404 is the expected end state and is dropped,
    # along with any card an earlier tick left behind.
    freezer.move_to("2026-09-01 09:00:00+02:00")
    coord._sync_extractor_issue("404 fetching the September card")
    assert registry.async_get_issue(DOMAIN, failed_id) is None
    assert registry.async_get_issue(DOMAIN, unreachable_id) is None
    coord._sync_extractor_issue("TimeoutError", transient=True)
    assert registry.async_get_issue(DOMAIN, unreachable_id) is None


async def test_deprecation_card_switches_tense_after_the_end_date(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Past the end date the card must stop saying the transfer is still
    coming. The pre-date wording promises prices stay correct and that
    nothing is broken yet, both false once the supplier has gone, and it
    names a date now in the past as if it were future."""
    entry = make_entry(
        supplier="dats24", contract="dats24_groen_variabel", region="flanders"
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    registry = ir.async_get(hass)
    issue_id = f"supplier_deprecated_{entry.entry_id}"

    freezer.move_to("2026-08-31 23:00:00+02:00")
    coord._sync_deprecated_supplier_issue()
    before = registry.async_get_issue(DOMAIN, issue_id)
    assert before is not None
    assert before.translation_key == "supplier_deprecated"

    freezer.move_to("2026-09-01 09:00:00+02:00")
    coord._sync_deprecated_supplier_issue()
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "supplier_deprecated_ended"
    # Same id either way, so the panel shows one card that changes wording
    # rather than a second one appearing beside the first.
    assert issue.translation_placeholders == {
        "supplier": "DATS 24",
        "successor": "EnergyVision",
        "ends_on": "2026-08-31",
    }


async def test_supply_ended_uses_the_local_date_not_utc(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The withdrawal is a Belgian calendar event. At 00:30 local on 1 Sep
    it is still 22:30 UTC on 31 Aug, so a UTC comparison would call supply
    live for another 90 minutes and keep raising extractor cards."""
    entry = make_entry(
        supplier="dats24", contract="dats24_groen_variabel", region="flanders"
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    freezer.move_to("2026-09-01 00:30:00+02:00")
    assert coord._supply_ended() is True


async def test_deprecated_supplier_issue_names_the_successor_in_both_regions(
    hass: HomeAssistant, freezer: Any
) -> None:
    """DATS 24 sold in Flanders and Wallonia, and EnergyVision is modelled in
    both, so a Walloon entry gets routed too. Before the Walloon card was
    added this named a supplier the contract step then refused.

    What is under test is the regional successor routing, not the tense, so
    the clock is pinned before DATS 24's end date. Without that this flips to
    the ``_ended`` variant on 2026-09-01, exactly as the sibling coverage of
    the two wordings warns."""
    freezer.move_to("2026-08-06 12:00:00+02:00")
    for region in ("flanders", "wallonia"):
        entry = make_entry(
            supplier="dats24", contract="dats24_groen_variabel", region=region
        )
        entry.add_to_hass(hass)
        BePricesCoordinator(hass, entry)._sync_deprecated_supplier_issue()
        issue = ir.async_get(hass).async_get_issue(
            DOMAIN, f"supplier_deprecated_{entry.entry_id}"
        )
        assert issue is not None
        assert issue.translation_key == "supplier_deprecated"
        assert issue.translation_placeholders == {
            "supplier": "DATS 24",
            "successor": "EnergyVision",
            "ends_on": "2026-08-31",
        }


def test_successor_is_dropped_when_it_does_not_serve_the_region() -> None:
    """A withdrawal names one successor nationally while our coverage is per
    region, so the card must not send a user to a supplier the config flow
    would refuse. EnergyVision sells nothing in Brussels."""
    assert _successor_for("energyvision", "flanders") is not None
    assert _successor_for("energyvision", "wallonia") is not None
    assert _successor_for("energyvision", "brussels") is None
    # Unset or unknown successors resolve to None rather than raising.
    assert _successor_for(None, "flanders") is None
    assert _successor_for("no_such_supplier", "flanders") is None


async def test_sync_extractor_failed_issue_creates_and_clears(
    hass: HomeAssistant,
) -> None:
    """A persistent ExtractorError from the supplier path must surface
    as a Repairs entry the user can act on, and clear the moment a
    refresh succeeds."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"extractor_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._sync_extractor_issue("could not parse Eneco fixed energy block")
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_key == "extractor_failed"
    assert "Eneco fixed" in (issue.translation_placeholders or {}).get("error", "")

    coord._sync_extractor_issue(None)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_unreadable_cards_swap_the_extractor_failed_card(
    hass: HomeAssistant,
) -> None:
    """A card with no text layer must raise its own Repairs card instead of
    ``extractor_failed``.

    Ecofix went to page images in August 2026. The default card tells the
    user the layout changed and asks them to open a GitHub issue, which is
    advice nobody can act on: there is no text layer to re-anchor a parser
    against. The signal is derived from the error the fetch raised, not a
    per-supplier flag, so any supplier that starts doing this is covered and
    the card stops by itself when readable cards return.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    registry = ir.async_get(hass)
    failed_id = f"extractor_failed_{entry.entry_id}"
    unreachable_id = f"extractor_unreachable_{entry.entry_id}"
    unreadable_id = f"extractor_unreadable_{entry.entry_id}"
    no_prices_id = f"extractor_unreadable_no_prices_{entry.entry_id}"

    # With no snapshot in hand there is nothing to drift, so the card that
    # warns about drift is the wrong one: the entry gets the no-prices twin.
    coord._sync_extractor_issue("card has no text layer", unreadable=True)
    issue = registry.async_get_issue(DOMAIN, no_prices_id)
    assert issue is not None
    assert issue.translation_key == "extractor_unreadable_no_prices"
    assert registry.async_get_issue(DOMAIN, unreadable_id) is None
    assert registry.async_get_issue(DOMAIN, failed_id) is None

    # With a cached card still being served, the drift warning is the right
    # one, and raising it clears the twin.
    coord._set_snapshot(make_snapshot())
    coord._sync_extractor_issue("card has no text layer", unreadable=True)
    issue = registry.async_get_issue(DOMAIN, unreadable_id)
    assert issue is not None
    assert issue.translation_key == "extractor_unreadable"
    assert registry.async_get_issue(DOMAIN, no_prices_id) is None
    assert registry.async_get_issue(DOMAIN, failed_id) is None

    # A timeout is still a timeout, and raising it clears the unreadable
    # card rather than stacking beside it.
    coord._sync_extractor_issue("TimeoutError", transient=True)
    transient_issue = registry.async_get_issue(DOMAIN, unreachable_id)
    assert transient_issue is not None
    assert transient_issue.translation_key == "extractor_unreachable"
    assert registry.async_get_issue(DOMAIN, unreadable_id) is None

    # The next fetch reading a card WITH text must fall back to the ordinary
    # card, with no code change: this is the self-healing property the
    # derived signal exists for.
    coord._sync_extractor_issue("could not parse energy block")
    assert registry.async_get_issue(DOMAIN, failed_id) is not None
    assert registry.async_get_issue(DOMAIN, unreadable_id) is None

    coord._sync_extractor_issue(None)
    for issue_id in (failed_id, unreachable_id, unreadable_id, no_prices_id):
        assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_a_textless_card_fetch_reaches_the_unreadable_repairs_card(
    hass: HomeAssistant,
) -> None:
    """The SEAM: a fetch raising CardNotReadableError must end up showing
    the extractor_unreadable card, not the generic one.

    Detection (providers/_pdf.py) and routing (_sync_extractor_issue) each
    have their own tests, but nothing joined them, and the join is the part
    that rots: the coordinator computes ``unreadable`` from the exception
    type, so any refactor that widens the except clause or drops the
    isinstance check would silently fall back to "open a GitHub issue"
    while both unit tests stayed green.
    """
    from custom_components.be_electricity_prices.providers.base import (
        CardNotReadableError,
    )

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _textless_fetch(*args: Any, **kwargs: Any) -> None:
        raise CardNotReadableError(
            "card has no text layer: 172 characters across 5 page(s)"
        )

    extractor = make_stub_extractor(fetch=_textless_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    registry = ir.async_get(hass)
    # This coordinator has never held a snapshot, so the card that lands is
    # the no-prices twin rather than the drift warning.
    issue = registry.async_get_issue(
        DOMAIN, f"extractor_unreadable_no_prices_{entry.entry_id}"
    )
    assert issue is not None
    assert issue.translation_key == "extractor_unreadable_no_prices"
    # and NOT the card that asks the user to report a layout change
    assert (
        registry.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}") is None
    )


async def test_an_ordinary_parse_failure_still_reaches_the_failed_card(
    hass: HomeAssistant,
) -> None:
    """The other side of the seam: a normal ExtractorError must NOT be
    routed to the unreadable card, or every layout drift would stop asking
    to be reported."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    async def _bad_parse(*args: Any, **kwargs: Any) -> None:
        raise ExtractorError("could not parse Eneco fixed energy block")

    extractor = make_stub_extractor(fetch=_bad_parse)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}")
        is not None
    )
    assert (
        registry.async_get_issue(DOMAIN, f"extractor_unreadable_{entry.entry_id}")
        is None
    )


async def test_readable_supplier_keeps_the_extractor_failed_card(
    hass: HomeAssistant,
) -> None:
    """The unreadable-card routing must not leak to normal suppliers: a
    parse failure on Eneco still raises the actionable card that asks for
    a GitHub issue, because there it IS actionable."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    registry = ir.async_get(hass)

    coord._sync_extractor_issue("could not parse Eneco fixed energy block")
    issue = registry.async_get_issue(DOMAIN, f"extractor_failed_{entry.entry_id}")
    assert issue is not None
    assert issue.translation_key == "extractor_failed"
    assert (
        registry.async_get_issue(DOMAIN, f"extractor_unreadable_{entry.entry_id}")
        is None
    )


async def test_sync_entsoe_auth_issue_creates_and_clears(
    hass: HomeAssistant,
) -> None:
    """An ENTSO-E 401 must raise an ERROR-severity Repairs entry that
    points the user at rotating the API key, distinct from transient
    network issues which the coordinator absorbs silently."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"entsoe_auth_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._sync_entsoe_auth_issue(True, "ENTSO-E rejected the API key")
    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "entsoe_auth_failed"

    coord._sync_entsoe_auth_issue(False)
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_static_contract_clears_stuck_entsoe_auth_issue(
    hass: HomeAssistant,
) -> None:
    """Regression for f085501: a previously-set ENTSO-E auth issue must
    auto-resolve on the next successful tick when the coordinator is
    holding a static (non-Dynamic) snapshot. Without the unconditional
    clear, the issue lingers in Repairs forever after the user
    switches a stuck dynamic entry to a static contract via OptionsFlow."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"entsoe_auth_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    # Pre-set the auth issue (the user's stuck-dynamic state).
    coord._sync_entsoe_auth_issue(True, "ENTSO-E rejected the API key")
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Drop a static snapshot in place; mock _maybe_refresh_snapshot and
    # _track_monthly_peak so the tick reaches the auth-issue clear without
    # going through a network round-trip.
    coord._snapshot = make_snapshot()  # default is FixedRates (static)
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]

    await coord._async_update_data()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_update_data_fetches_spots_for_spot_indexed_injection(
    hass: HomeAssistant,
) -> None:
    """Shape-c (Cociter Variable): a static-energy contract whose injection
    is a per-hour spot formula must trigger the historical-spot fetch from
    the live tick, or the YTD injection credit silently drops to zero."""
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_variable",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
            "api_key": "TESTKEY",
        },
        title="Cociter Variable injection",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=VariableRates(current=0.17),
        injection=InjectionRates(current=None, factor=0.97, base=-0.021),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(return_value={})  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "custom_components.be_electricity_prices.ytd_cost._compute_current_year_cost",
        AsyncMock(return_value=0.0),
    ):
        await coord._async_update_data()

    coord._ensure_historical_spots.assert_awaited()


async def test_successful_tick_clears_stuck_extractor_failed_issue(
    hass: HomeAssistant,
) -> None:
    """Regression for cycle-9 #1: a previously-set extractor_failed
    issue must auto-resolve on the next successful tick regardless of
    whether the snapshot came from a fresh fetch or from any of the
    short-circuit paths (sibling adoption, self-fresh probe-match).
    The clear is gated on ``_last_error`` so a failing-fetch-kept-
    cached tick does NOT clear the alert (covered by
    ``test_failing_fetch_keeps_extractor_failed_issue`` below). Same
    shape as the cycle-7 entsoe_auth_failed fix."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"extractor_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._sync_extractor_issue("regex drift in tax block")
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Drop a static snapshot in place; mock _maybe_refresh_snapshot so
    # the tick reaches the conditional clear without a network round-
    # trip. _last_error is empty (clean state), mimicking the sibling-
    # cache-adopt or self-fresh probe-match path.
    coord._snapshot = make_snapshot()
    coord._last_error = ""
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]

    await coord._async_update_data()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_failing_fetch_keeps_extractor_failed_issue(
    hass: HomeAssistant,
) -> None:
    """Regression for the F1 fix: when _maybe_refresh_snapshot fails
    its fresh fetch but keeps serving the cached snapshot, it sets
    _last_error and raises the extractor_failed issue itself. The
    outer _async_update_data must NOT clear that alert just because a
    cached snapshot is still usable - the user has to see that the
    supplier extractor is currently broken."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"extractor_failed_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._snapshot = make_snapshot()

    async def _fail_fetch() -> None:
        coord._last_error = "extractor: layout drift"
        coord._sync_extractor_issue(coord._last_error)

    coord._maybe_refresh_snapshot = _fail_fetch  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]

    await coord._async_update_data()

    assert registry.async_get_issue(DOMAIN, issue_id) is not None


async def test_transient_failure_defers_extractor_issue_until_threshold(
    hass: HomeAssistant,
) -> None:
    """A lone transient fetch failure (network timeout) must NOT raise any
    repair issue: a single CDN timeout is almost always recovered on the
    next tick, so alarming the user on the first failure is a false
    positive. Once the failure survives _EXTRACTOR_ISSUE_THRESHOLD
    consecutive attempts it raises the softer extractor_unreachable card
    (never the actionable extractor_failed one, which is reserved for parse
    drift), and it clears the moment a fetch succeeds. _force_refresh
    bypasses the 5-min negative cache so the test can drive consecutive
    attempts back to back."""
    from custom_components.be_electricity_prices.coordinator_snapshot import (
        _EXTRACTOR_ISSUE_THRESHOLD,
    )
    from custom_components.be_electricity_prices.snapshot_store import (
        _shared_failed_fetches,
    )

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    unreachable_id = f"extractor_unreachable_{entry.entry_id}"
    failed_id = f"extractor_failed_{entry.entry_id}"
    registry = ir.async_get(hass)
    key = coord._shared_key()

    fail = True

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        if fail:
            raise ExtractorError("network error fetching https://x.pdf: TimeoutError")
        return _fake_snapshot()

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        # Every attempt up to (threshold - 1) records the error but must
        # not raise the user-facing repair issue yet.
        for _ in range(_EXTRACTOR_ISSUE_THRESHOLD - 1):
            coord._force_refresh = True
            await coord._maybe_refresh_snapshot()
        assert registry.async_get_issue(DOMAIN, unreachable_id) is None
        assert coord._last_error.startswith("network error fetching")
        assert _shared_failed_fetches(hass)[key][2] == _EXTRACTOR_ISSUE_THRESHOLD - 1

        # The threshold-th consecutive failure raises the transient card,
        # never the actionable "layout changed" one.
        coord._force_refresh = True
        await coord._maybe_refresh_snapshot()
        assert _shared_failed_fetches(hass)[key][2] == _EXTRACTOR_ISSUE_THRESHOLD
        assert registry.async_get_issue(DOMAIN, unreachable_id) is not None
        assert registry.async_get_issue(DOMAIN, failed_id) is None

        # A subsequent success clears the issue and resets the counter.
        fail = False
        coord._force_refresh = True
        await coord._maybe_refresh_snapshot()
    assert key not in _shared_failed_fetches(hass)
    assert registry.async_get_issue(DOMAIN, unreachable_id) is None


async def test_actionable_failure_raises_extractor_failed_immediately(
    hass: HomeAssistant,
) -> None:
    """A parse / layout failure won't self-heal, so it must raise the
    actionable extractor_failed card on the very first failure instead of
    waiting for the transient threshold, and must not raise the softer
    extractor_unreachable card."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    failed_id = f"extractor_failed_{entry.entry_id}"
    unreachable_id = f"extractor_unreachable_{entry.entry_id}"
    registry = ir.async_get(hass)

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        raise ExtractorError("could not parse Eneco fixed energy block")

    extractor = make_stub_extractor(fetch=_fake_fetch)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        await coord._maybe_refresh_snapshot()

    assert registry.async_get_issue(DOMAIN, failed_id) is not None
    assert registry.async_get_issue(DOMAIN, unreachable_id) is None


# Every distinct Repairs issue id this integration raises, by prefix.
# supplier_deprecated_no_successor is deliberately absent: it shares the
# supplier_deprecated id, since an entry only ever carries one of the two.
_REPAIR_ISSUE_KINDS = (
    "snapshot_stale",
    "extractor_failed",
    "extractor_unreachable",
    "extractor_unreadable",
    "extractor_unreadable_no_prices",
    "entsoe_auth_failed",
    "supplier_deprecated",
    "exclusive_night_rate_missing",
    "impact_rates_missing",
    "connection_fee_missing",
)


def test_repair_issue_kinds_match_the_declared_strings() -> None:
    """strings.json is the source of truth for what can be raised. Adding an
    issue there without adding it here (and to async_remove_entry) leaves it
    lingering in the Repairs panel after the entry is gone, which is how
    exclusive_night_rate_missing and impact_rates_missing were missed."""
    import json
    from pathlib import Path

    strings = json.loads(
        (
            Path(__file__).resolve().parent.parent
            / "custom_components"
            / "be_electricity_prices"
            / "strings.json"
        ).read_text(encoding="utf-8")
    )
    # The three supplier_deprecated variants share ONE issue id and differ
    # only in translation_key, so only the base name is a removable kind.
    declared = set(strings["issues"]) - {
        "supplier_deprecated_no_successor",
        "supplier_deprecated_ended",
        "supplier_deprecated_ended_no_successor",
    }
    assert declared == set(_REPAIR_ISSUE_KINDS)


async def test_async_remove_entry_clears_all_repair_issues(
    hass: HomeAssistant,
) -> None:
    """Every issue id embeds the entry id, so once the entry is gone the
    coordinator can no longer auto-resolve any of them: async_remove_entry
    has to clear each one or it lingers in the Repairs panel forever.

    Raised straight through the registry rather than via the coordinator:
    several are mutually exclusive by construction (extractor_failed vs
    extractor_unreachable), and what is under test is the removal list, not
    how each issue comes to be raised."""
    from custom_components.be_electricity_prices import async_remove_entry

    entry = _entry()
    entry.add_to_hass(hass)
    for kind in _REPAIR_ISSUE_KINDS:
        ir.async_create_issue(
            hass,
            DOMAIN,
            f"{kind}_{entry.entry_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=kind,
        )

    registry = ir.async_get(hass)
    for kind in _REPAIR_ISSUE_KINDS:
        assert registry.async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}") is not None

    await async_remove_entry(hass, entry)

    for kind in _REPAIR_ISSUE_KINDS:
        assert registry.async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}") is None


async def test_evict_shared_caches_drops_rows_for_tuple(hass: HomeAssistant) -> None:
    """evict_shared_caches must remove every cache row pinned to the
    given (supplier, contract, region) tuple, leaving rows for other
    tuples untouched."""
    from custom_components.be_electricity_prices.snapshot_store import _SharedSnapshot

    key_us = ("eneco", "power_fix", "wallonia")
    key_other = ("bolt", "bolt_fix", "wallonia")

    snap = _fake_snapshot()
    fetched_at = dt_util.utcnow()
    _shared_snapshots(hass)[key_us] = _SharedSnapshot(
        snapshot=snap, fetched_at=fetched_at, probe_key="ours"
    )
    _shared_snapshots(hass)[key_other] = _SharedSnapshot(
        snapshot=snap, fetched_at=fetched_at, probe_key="theirs"
    )
    _shared_failed_fetches(hass)[key_us] = (fetched_at, "ours-error", 1)
    _shared_failed_fetches(hass)[key_other] = (fetched_at, "theirs-error", 1)
    monthly = _monthly_snapshots(hass)
    monthly[("eneco", "power_fix", "wallonia", "2026-01")] = snap
    monthly[("bolt", "bolt_fix", "wallonia", "2026-01")] = snap

    evict_shared_caches(hass, key_us, "eneco")

    assert key_us not in _shared_snapshots(hass)
    assert key_other in _shared_snapshots(hass)  # other tuple preserved
    assert key_us not in _shared_failed_fetches(hass)
    assert key_other in _shared_failed_fetches(hass)
    assert ("eneco", "power_fix", "wallonia", "2026-01") not in _monthly_snapshots(hass)
    assert ("bolt", "bolt_fix", "wallonia", "2026-01") in _monthly_snapshots(hass)


async def test_evict_shared_caches_keeps_held_lock(hass: HomeAssistant) -> None:
    """A held lock must NOT be popped during eviction; otherwise a
    re-created entry on the same tuple would get a fresh lock and the
    dedup property would silently break."""
    key = ("eneco", "power_fix", "wallonia")
    lock = _shared_lock(hass, key)
    await lock.acquire()
    try:
        evict_shared_caches(hass, key, "eneco")
        # Held lock stays in the bucket: future _shared_lock(hass, key)
        # must return the same Lock object.
        assert _shared_lock(hass, key) is lock
    finally:
        lock.release()


async def test_async_remove_entry_clears_stale_issue(hass: HomeAssistant) -> None:
    """async_remove_entry must drop the per-entry repair issue so it
    doesn't linger after the entry that owns it is gone."""
    from custom_components.be_electricity_prices import async_remove_entry

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._sync_stale_issue(True)
    registry = ir.async_get(hass)
    issue_id = f"snapshot_stale_{entry.entry_id}"
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    await async_remove_entry(hass, entry)

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_save_persistent_skipped_after_runtime_data_swapped(
    hass: HomeAssistant,
) -> None:
    """A slow tick that finishes after the entry has been reloaded
    (runtime_data points at a fresh coordinator) must not overwrite
    the new coord's saved file."""
    entry = _entry()
    entry.add_to_hass(hass)
    old_coord = BePricesCoordinator(hass, entry)
    new_coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = new_coord  # simulate post-reload state

    saved = False

    async def _fake_save(_payload: object) -> None:
        nonlocal saved
        saved = True

    with patch.object(old_coord._store, "async_save", new=_fake_save):
        await old_coord._save_persistent()

    assert saved is False, "obsolete coordinator must not overwrite the cache file"


async def test_save_persistent_runs_during_first_refresh(
    hass: HomeAssistant,
) -> None:
    """Regression: BePricesCoordinator.async_config_entry_first_refresh
    triggers _save_persistent before HA's setup hook assigns
    entry.runtime_data. The identity guard must not raise (older
    runtime_data was unset; recent HA cores expose UNDEFINED) and
    must allow the save to proceed."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    # Do not assign entry.runtime_data — that's the pre-first-refresh
    # state. The coordinator's _snapshot is None too, so the file
    # carries only the peak/identity payload, but the call must not
    # raise.
    saved_payload: dict[str, object] | None = None

    async def _fake_save(payload: dict[str, object]) -> None:
        nonlocal saved_payload
        saved_payload = payload

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    assert saved_payload is not None, (
        "first-refresh save must succeed (runtime_data not yet assigned)"
    )
    assert saved_payload["entry_supplier"] == entry.data["supplier"]


async def test_save_persistent_skips_when_entry_tuple_drifted(
    hass: HomeAssistant,
) -> None:
    """OptionsFlow mutates entry.data via async_update_entry before the
    reload listener swaps runtime_data. A slow tick on the OLD
    coordinator that resumes in that window must NOT write to disk:
    the save would either stamp the OLD tuple (which the load path
    later has to discard) or worse, race the new coord's first write.
    Skipping outright lets the new coord own the blob from the first
    save."""
    entry = _entry()
    entry.add_to_hass(hass)
    old_coord = BePricesCoordinator(hass, entry)

    # Simulate OptionsFlow: entry.data swapped to a different supplier.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "supplier": "bolt", "contract": "bolt_fix"},
    )

    saved_payload: dict[str, object] | None = None

    async def _fake_save(payload: dict[str, object]) -> None:
        nonlocal saved_payload
        saved_payload = payload

    with patch.object(old_coord._store, "async_save", new=_fake_save):
        await old_coord._save_persistent()

    assert saved_payload is None


async def test_save_persistent_skipped_during_reload_window(
    hass: HomeAssistant,
) -> None:
    """The OLD coordinator's tuple guard must skip the save when
    OptionsFlow has already swapped entry.data but the new coordinator
    isn't yet assigned to runtime_data.

    Production _save_persistent has two guards: (1) the runtime_data
    isinstance check (covered by
    test_save_persistent_skipped_when_runtime_data_replaced) and (2)
    the tuple guard against entry.data drift. This test exercises the
    second guard. The synthetic UNDEFINED-shaped runtime_data is a
    realistic stand-in for the brief window after async_unload but
    before async_setup_entry assigns the new coord; that branch is
    not what the assertion validates."""
    entry = _entry()
    entry.add_to_hass(hass)
    old_coord = BePricesCoordinator(hass, entry)

    # Sentinel-shaped runtime_data so the isinstance(BePricesCoordinator)
    # check fails identically to the real UNDEFINED case.
    entry.runtime_data = type("UndefinedType", (), {"_singleton": 0})()

    # Simulate OptionsFlow having swapped entry.data BEFORE the new
    # coord lands. The tuple guard must skip the save.
    hass.config_entries.async_update_entry(
        entry,
        data={**entry.data, "supplier": "bolt", "contract": "bolt_fix"},
    )

    saved: list[dict[str, object]] = []

    async def _fake_save(payload: dict[str, object]) -> None:
        saved.append(payload)

    with patch.object(old_coord._store, "async_save", new=_fake_save):
        await old_coord._save_persistent()

    assert saved == [], (
        "obsolete coordinator must not write during the reload window "
        "when entry.data has drifted"
    )


async def test_load_persistent_discards_blob_for_other_supplier(
    hass: HomeAssistant,
) -> None:
    """async_load_persistent must reject a cached snapshot whose
    persisted (supplier, contract, region) tuple differs from the
    entry's current data, so an OptionsFlow change followed by a
    restart does not serve the previous supplier's rates."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",  # entry currently configured for eneco
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    stale_payload: dict[str, object] = {
        "entry_supplier": "bolt",  # blob written under a different supplier
        "entry_contract": "bolt_fix",
        "entry_region": "wallonia",
        "snapshot": {
            "_cached_at": "2026-04-30T12:00:00+00:00",
            "_probe_key": "stale",
            "_schema_version": 7,
            "supplier": "bolt",
            "contract": "bolt_fix",
            "energy_kind": "fixed",
            "energy": {"single": 0.18},
            "dsos": {"ores": {"distribution_single": 0.10, "transport": 0.0145}},
            "taxes": {},
            "source_url": "test://",
            "publication_label": "april 2026",
            "valid_until": None,
            "injection": None,
        },
    }

    async def _fake_load() -> dict[str, object]:
        return stale_payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    assert coord._snapshot is None
    assert coord._snapshot_fetched_at is None


async def test_load_persistent_drops_historical_spots_on_tuple_mismatch(
    hass: HomeAssistant,
) -> None:
    """When the persisted snapshot tuple differs from the current entry
    (e.g. user just swapped a Cociter dynamic contract for an Eneco
    fixed one via OptionsFlow), the ENTSO-E historical spots harvested
    under the previous tuple are no longer queried by any code path on
    the new contract. Loading them anyway leaves stale state in memory
    and re-saves it indefinitely; the load must skip them whenever the
    tuple guard rejects the snapshot."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "none",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    stale_payload: dict[str, object] = {
        "entry_supplier": "cociter",
        "entry_contract": "cociter_dynamic",
        "entry_region": "wallonia",
        "historical_spots": {
            "2026-01-01T00:00:00+00:00": 0.123,
            "2026-01-01T01:00:00+00:00": 0.125,
        },
    }

    async def _fake_load() -> dict[str, object]:
        return stale_payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    assert coord._historical_spots == {}, (
        "historical_spots from a different supplier tuple must be discarded"
    )


async def test_force_refresh_keeps_the_price_history_by_default(
    hass: HomeAssistant,
) -> None:
    """An ordinary refresh must not throw away the year's spot cache.

    Refilling it re-fetches every day since 1 January in week-sized chunks
    against a rate-limited endpoint, which is far too much to spend on a
    service users call to pick up a new tariff card."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hour = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)
    coord._historical_spots = {hour: 0.11}
    coord._complete_spot_days = {date(2026, 3, 1)}
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.async_force_refresh()

    assert coord._historical_spots == {hour: 0.11}
    assert coord._complete_spot_days == {date(2026, 3, 1)}
    # The live today/tomorrow cache still goes, as it always did.
    assert coord._spot_cache == {}


async def test_force_refresh_clears_the_price_history_on_request(
    hass: HomeAssistant,
) -> None:
    """clear_history is the only thing that can repair that cache.

    _ensure_historical_spots re-fetches a day holding fewer than 20 of its 24
    hours, so a day that is complete but WRONG is never revisited, and nothing
    else empties the dict before the year-end prune. Without this the only
    escape from one bad cached price was deleting and re-adding the entry.

    The day-completeness markers have to go with it: leaving them would tell
    the next fill that every day is already covered, and the cache would come
    back empty rather than refreshed."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._historical_spots = {datetime(2026, 3, 1, 10, 0, tzinfo=UTC): 62.5}
    coord._historical_spot_quarters = {
        datetime(2026, 3, 1, 10, 0, tzinfo=UTC): [62.5] * 4
    }
    coord._complete_spot_days = {date(2026, 3, 1)}
    coord._spot_day_retry_at = {date(2026, 2, 2): datetime(2026, 2, 2, tzinfo=UTC)}
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.async_force_refresh(clear_history=True)

    assert coord._historical_spots == {}
    # Both caches come off the same fetch, so the repair has to take both or
    # it leaves half the bad hour behind.
    assert coord._historical_spot_quarters == {}
    assert coord._complete_spot_days == set()
    assert coord._spot_day_retry_at == {}


async def test_load_persistent_drops_an_impossible_cached_spot(
    hass: HomeAssistant,
) -> None:
    """A cached price that could not have been published is discarded.

    A persisted spot used to be trusted forever: _ensure_historical_spots only
    fetches a day holding fewer than 20 of its 24 hours, so a day that is
    COMPLETE but wrong was never revisited, and nothing else clears the cache
    before the year-end prune. One value on the wrong scale therefore skewed a
    dynamic contract's whole year-to-date bill for the life of the entry, with
    no way for the user to correct it short of deleting the entry.

    Dropping it is what repairs the cache: the day falls under the refetch
    threshold and the next tick replaces it from ENTSO-E."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    payload: dict[str, object] = {
        "entry_supplier": "cociter",
        "entry_contract": "cociter_dynamic",
        "entry_region": "wallonia",
        "historical_spots": {
            # Ordinary hour, and a negative one: Belgian day-ahead goes
            # negative routinely and must survive.
            "2026-01-01T00:00:00+00:00": 0.123,
            "2026-01-01T01:00:00+00:00": -0.04,
            # EUR/MWh left unscaled among EUR/kWh neighbours, three orders out.
            "2026-01-01T02:00:00+00:00": 62.5,
        },
    }

    async def _fake_load() -> dict[str, object]:
        return payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    assert coord._historical_spots == {
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 0.123,
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC): -0.04,
    }


async def test_load_persistent_drops_an_hour_whose_quarter_is_impossible(
    hass: HomeAssistant,
) -> None:
    """The whole list goes, not the offending slot.

    A short quarter list would silently re-weight the hour's mean. The hourly
    value stays, because it passed its own check: the hour then prices energy
    as it always did and credits feed-in off that mean, which is the answer
    the slots refine rather than the one they replace.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "custom",
            "contract": "custom_dynamic",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "dynamic",
            "solar_regime": "injection",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    good = "2026-01-01T00:00:00+00:00"
    bad = "2026-01-01T01:00:00+00:00"
    payload: dict[str, object] = {
        "entry_supplier": "custom",
        "entry_contract": "custom_dynamic",
        "entry_region": "flanders",
        "historical_spots": {good: 0.1, bad: 0.1},
        "historical_spot_quarters": {
            # Negative slots are routine on the Belgian day-ahead and survive.
            good: [-0.06, -0.02, 0.01, 0.05],
            # EUR/MWh left unscaled among EUR/kWh neighbours.
            bad: [0.1, 0.1, 62.5, 0.1],
        },
    }

    async def _fake_load() -> dict[str, object]:
        return payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    when = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert coord._historical_spot_quarters == {when: [-0.06, -0.02, 0.01, 0.05]}
    # The bad hour keeps its own hourly price, which passed its own check: it
    # prices that hour's energy as it always did and credits feed-in off the
    # mean, and a day one hour short is not re-fetched anyway, so taking the
    # hourly value out too would forfeit a sane energy price for good.
    assert coord._historical_spots == {
        when: 0.1,
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 0.1,
    }


async def test_save_persistent_round_trips_the_quarter_cache(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A restart must not cost the entry its one-time year of 15-minute slots."""
    freezer.move_to("2026-06-29 12:00:00+02:00")
    entry = _floored_quarter_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hour = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    coord._historical_spots = {hour: 0.20}
    coord._historical_spot_quarters = {hour: [0.05, 0.15, 0.25, 0.35]}
    saved: dict[str, object] = {}

    async def _fake_save(payload: dict[str, object]) -> None:
        saved.update(payload)

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    assert saved["historical_spot_quarters"] == {
        "2026-01-01T10:00:00+00:00": [0.05, 0.15, 0.25, 0.35]
    }

    restored = BePricesCoordinator(hass, entry)

    async def _fake_load() -> dict[str, object]:
        return saved

    with patch.object(restored._store, "async_load", new=_fake_load):
        await restored.async_load_persistent()

    assert restored._historical_spot_quarters == {hour: [0.05, 0.15, 0.25, 0.35]}


async def test_load_persistent_keeps_historical_spots_on_tuple_match(
    hass: HomeAssistant,
) -> None:
    """Symmetric to the discard test: when the persisted tuple matches
    the current entry, historical_spots survive the load. Without this
    a future refactor that always-drops historical_spots would still
    pass the discard test but silently lose every dynamic-contract
    entry's YTD spot cache across HA restarts."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "solar_regime": "none",
            "api_key": "k",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    payload: dict[str, object] = {
        "entry_supplier": "cociter",
        "entry_contract": "cociter_dynamic",
        "entry_region": "wallonia",
        "historical_spots": {
            "2026-01-01T00:00:00+00:00": 0.123,
            "2026-01-01T01:00:00+00:00": 0.125,
        },
    }

    async def _fake_load() -> dict[str, object]:
        return payload

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()

    expected = {
        datetime(2026, 1, 1, 0, 0, tzinfo=UTC): 0.123,
        datetime(2026, 1, 1, 1, 0, tzinfo=UTC): 0.125,
    }
    assert coord._historical_spots == expected


async def test_evict_bumps_tuple_generation_blocks_inflight_write(
    hass: HomeAssistant,
) -> None:
    """A coroutine mid-fetch when eviction runs must NOT re-create the
    cache row on resume, otherwise the row would orphan and a future
    re-add of the same tuple could read stale data."""
    from custom_components.be_electricity_prices.snapshot_store import (
        _bump_tuple_generation,
        _shared_failed_fetches,
        _tuple_generation,
        evict_shared_caches,
    )

    key = ("eneco", "power_fix", "wallonia")
    gen_before = _tuple_generation(hass, key)

    # Simulate an in-flight cache writer that captured the generation
    # at lock entry, then the user removed the entry mid-fetch.
    gen_at_entry = gen_before
    evict_shared_caches(hass, key, "eneco")
    gen_after = _tuple_generation(hass, key)

    assert gen_after > gen_at_entry, "eviction must bump the tuple generation"

    # The writer's resume-side guard would compare the generation;
    # confirm that comparison rejects the write.
    assert _tuple_generation(hass, key) != gen_at_entry

    # And the explicit bump helper increments by one.
    _bump_tuple_generation(hass, key)
    assert _tuple_generation(hass, key) == gen_after + 1

    # Sanity: the failed-fetch bucket can be empty without this
    # affecting the generation.
    assert _shared_failed_fetches(hass).get(key) is None


async def test_first_refresh_end_to_end_does_not_crash(hass: HomeAssistant) -> None:
    """Regression for the v0.5.14 production crash: drive the actual
    coordinator tick (refresh → update → save → load) with a mocked
    extractor while ``entry.runtime_data`` is unset. The chain must
    complete without raising AttributeError or comparing against an
    UNDEFINED sentinel.

    The previous regression test only called _save_persistent directly,
    which masked the production failure mode -- runtime_data being
    unset *because* we are inside the very first refresh entry-point."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    snap = make_snapshot(supplier="eneco", contract="power_fix")

    async def _fake_fetch(*_args: object, **_kwargs: object) -> SupplierSnapshot:
        return snap

    extractor = make_stub_extractor(extractor_id="eneco", fetch=_fake_fetch)
    # entry.runtime_data is intentionally NOT assigned -- this is the
    # state HA core is in before async_setup_entry's coordinator =
    # ... line completes.
    assert getattr(entry, "runtime_data", None) is None

    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=extractor,
    ):
        # async_refresh runs the same _async_update_data path as
        # async_config_entry_first_refresh; either would have crashed
        # under v0.5.14's bare runtime_data read at line 887. Use
        # async_refresh because the first-refresh helper requires
        # config_entry to be wired into DataUpdateCoordinator from
        # 2024.10+ -- and the bug manifests on the inner tick path
        # regardless of which entry-point invokes it.
        await coord.async_refresh()

    assert coord.last_update_success
    assert coord._snapshot is snap


def _flanders_sensor_entry(peak_entity: str) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "capacity_mode": "sensor",
            "capacity_peak_sensor": peak_entity,
        },
        title="Eneco (Flanders)",
    )


async def test_capacity_peak_scales_watts_to_kilowatts(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Power sensors selected by the auto-pick land on Riemann
    integration sources, which report in W. Without unit awareness a
    4481 W reading was stored as 4481 kW and the capacity_cost
    sensor inflated by 1000x (issue #19)."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set(entity_id, "4481", {"unit_of_measurement": "W"})

    await coord._track_monthly_peak()

    assert coord._peak_kw == 4.481


async def test_capacity_peak_keeps_kilowatts_unscaled(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A native kW sensor must be passed through unchanged so the
    fix doesn't regress users who already had the right unit."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power_kw"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set(entity_id, "4.481", {"unit_of_measurement": "kW"})

    await coord._track_monthly_peak()

    assert coord._peak_kw == 4.481


async def test_capacity_peak_treats_missing_unit_as_kilowatts(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Sensors that never set unit_of_measurement existed in the wild
    before the fix and were treated as kW. Keep that legacy path so
    the fix is purely additive."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power_unitless"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set(entity_id, "3.0", {})

    await coord._track_monthly_peak()

    assert coord._peak_kw == 3.0


async def test_capacity_peak_scales_volt_amperes_to_kilowatts(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Some Belgian P1 readers expose apparent_power in VA. Treat
    VA the same way as W so those users don't see the same x1000
    inflation."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_apparent_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set(entity_id, "4481", {"unit_of_measurement": "VA"})

    await coord._track_monthly_peak()

    assert coord._peak_kw == 4.481


async def test_capacity_peak_rejects_energy_sensor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A user that mistakenly picked a kWh sensor must NOT see the
    cumulative kWh climb into the monthly-peak slot. Ignore the
    update; the billed quantity still falls back to the floor."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.monthly_consumption"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hass.states.async_set(entity_id, "4481", {"unit_of_measurement": "kWh"})

    await coord._track_monthly_peak()

    # The bogus 4481 kWh reading is ignored, so nothing is measured. The
    # measurement stays raw at 0; the VREG floor lives on the billed quantity.
    assert coord._peak_kw == 0.0
    assert coord._billed_peak_kw() == 2.5


async def test_reset_monthly_peak_drops_persisted_value(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The diagnostic reset button must clear the rolling max so a
    previously inflated value (e.g. 4481 stored when the W-as-kW bug
    was live) doesn't survive the upgrade. The next tick rebuilds the
    peak from the corrected sensor reading."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_kw = 4481.0  # legacy bad value
    coord._save_persistent = AsyncMock()  # type: ignore[method-assign]
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    await coord.reset_monthly_peak()

    assert coord._peak_kw == 0.0
    coord._save_persistent.assert_awaited_once()
    coord.async_request_refresh.assert_awaited_once()


async def test_variable_cohort_keeps_its_per_hour_injection_index(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A Cociter Variable contract with a start date re-prices its ENERGY leg
    to a SpotMonthlyRates cohort, but its INJECTION keeps its own per-hour
    index. The card indexes the two legs on different periods and says so:
    note (7) "le prix ... est indexe mensuellement ... moyenne arithmetique ...
    (BELIX) durant le mois de fourniture" for consumption, note (9) "le prix de
    l'injection varie chaque heure" for injection.

    The cohort re-price freezes the commodity coefficients the customer signed,
    not the feed-in formula, so the bake must not reach the injection. It used
    to, which priced the credit off a flat month mean; since PV output peaks
    when the day-ahead price troughs, that systematically over-credited."""
    from unittest.mock import MagicMock

    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
        VariableRates,
    )

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_variable",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "solar_regime": "injection",
            "api_key": "TESTKEY",
            "contract_start_date": "2025-11-10",
        },
        title="Cociter Variable cohort injection",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        supplier="cociter",
        contract="cociter_variable",
        energy=VariableRates(current=0.17, formula_factor=0.8, formula_base=0.05),
        injection=InjectionRates(current=None, factor=0.97, base=-0.021),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    freezer.move_to("2026-07-01 10:30:00+00:00")
    coord._fetch_spot_prices = AsyncMock(  # type: ignore[method-assign]
        return_value={datetime(2026, 7, 1, h, tzinfo=UTC): 0.30 for h in range(24)}
    )
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    # Fix the month mean so a regression back to the bake is unambiguous.
    coord._monthly_spot_mean = MagicMock(return_value=0.10)  # type: ignore[method-assign]

    async def _cohort(*_a: object, **_k: object) -> SpotMonthlyRates:
        return SpotMonthlyRates(factor=0.8, base=0.05)

    with (
        patch(
            "custom_components.be_electricity_prices.cohort._cohort_energy_leg",
            new=_cohort,
        ),
        patch(
            "custom_components.be_electricity_prices.ytd_cost._compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        data = await coord._update_body()

    # Priced at the hour's own spot: 0.97 * 0.30 - 0.021 = 0.270, NOT the
    # month mean 0.97 * 0.10 - 0.021 = 0.076.
    value = data.injection_price_eur_per_kwh
    assert value is not None and abs(value - 0.270) < 1e-9
    # And the today/tomorrow injection arrays survive: a flat baked rate made
    # _injection_varies_intraday False and emitted nothing (issue #40 arrays).
    assert data.injection_hourly


async def test_variable_cohort_without_key_still_prices(hass: HomeAssistant) -> None:
    """A variable contract with a past start date and no ENTSO-E key must load
    and price off the current card. The cohort re-price used to hand back a
    SpotMonthlyRates leg, which took the spot path and failed setup with
    "missing ENTSO-E API key" on a key the variable flow never asks for."""
    from custom_components.be_electricity_prices.providers.base import (
        DsoOverlay,
        VariableRates,
    )

    dsos = {"fluvius_limburg": DsoOverlay(distribution_single=0.10, transport=0.0145)}
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_flex",
            "region": "flanders",
            "dso": "fluvius_limburg",
            "meter": "mono",
            "solar_regime": "injection",
            "contract_start_date": "2025-11-01",
        },
        title="Eneco Zon & Wind Flex cohort without key",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    # Coefficients as parsed from the real Zon & Wind Flex card.
    coord._snapshot = make_snapshot(
        supplier="eneco",
        contract="power_flex",
        dsos=dsos,
        energy=VariableRates(
            current=0.1219, formula_factor=1.0812, formula_base=0.0343122
        ),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]

    # The archived signing-month card carries its own coefficients, so the
    # cohort re-price is live here; _fetch_spot_prices is deliberately NOT
    # stubbed so a regression raises the real missing-key EntsoeError.
    async def _archived(*_a: object, **_k: object) -> object:
        return make_snapshot(
            supplier="eneco",
            contract="power_flex",
            dsos=dsos,
            energy=VariableRates(
                current=0.1219,
                formula_factor=1.0812,
                formula_base=0.03763,
                yearly_fixed_fee=65.0,
            ),
        )

    with (
        patch(
            "custom_components.be_electricity_prices.snapshot_store._snapshot_for_month",
            new=_archived,
        ),
        patch(
            "custom_components.be_electricity_prices.ytd_cost._compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        data = await coord._update_body()

    # A full hourly table priced off the current card's resolved rate. A
    # SpotMonthlyRates leg with no mean to price against yields an empty one.
    assert data.hourly
    assert data.last_error == ""


async def test_billed_peak_averages_the_last_twelve_months(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Fluvius bills the mean of the last twelve monthly peaks, not the month
    being accumulated. A seasonal household must therefore see a steady billed
    figure rather than one that tracks whichever month it is in."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {
        f"2025-{m:02d}-01": kw
        for m, kw in zip(range(6, 12), [6.0, 5.5, 4.5, 3.5, 3.0, 2.8], strict=True)
    }
    hass.states.async_set(entity_id, "4.0", {"unit_of_measurement": "kW"})

    await coord._track_monthly_peak()

    assert coord._peak_kw == 4.0  # this month, raw
    # Every month clears the 2.5 kW floor, so the per-month Max is a no-op:
    # (6.0 + 5.5 + 4.5 + 3.5 + 3.0 + 2.8 + 4.0) / 7
    assert coord._billed_peak_kw() == pytest.approx(29.3 / 7)


async def test_billed_peak_floors_each_month_before_averaging(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Fluvius's methodology spells the formula out: "Rekenkundig gemiddelde
    van de Max (Maandpiek (m), 2.5)". The minimum lands on each month, not on
    the mean, so 11 x 1.0 plus one 20.0 bills (11 x 2.5 + 20) / 12 = 3.96 kW,
    not the 2.58 kW that flooring the mean would give."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    # Eleven months that are actually inside the twelve-month window ending in
    # May 2026. Seeding Jan-Nov 2025 instead would leave five of them older
    # than the window, which the age-based prune correctly drops.
    coord._peak_history = {f"2025-{m:02d}-01": 1.0 for m in range(6, 13)}
    coord._peak_history.update({f"2026-{m:02d}-01": 1.0 for m in range(1, 5)})
    hass.states.async_set(entity_id, "20.0", {"unit_of_measurement": "kW"})

    await coord._track_monthly_peak()

    assert coord._billed_peak_kw() == pytest.approx((11 * 2.5 + 20.0) / 12)
    assert coord._billed_peak_kw() > 2.6  # flooring the mean would give 2.58


async def test_billed_peak_ignores_the_unmeasured_running_month(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The running month is reset to 0 on the local 1st, and a zero floored to
    2,5 is not a measured peak.

    Counting it dragged the twelve-term mean down at every rollover: eleven
    banked months at 6,0 kW billed (11 x 6,0 + 2,5) / 12 = 5,71 kW for the
    first hours of the month, stepping capacity_cost and current_year_cost
    down and back up as the month accrued. The function already leaves out a
    month it never measured, on Fluvius's own estimate-the-gap rule; an
    in-progress month with no reading yet is exactly that.
    """
    freezer.move_to("2026-05-01 00:30:00+02:00")
    entry = _flanders_sensor_entry("sensor.house_power")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2025-{m:02d}-01": 6.0 for m in range(6, 12)}
    coord._peak_kw = 0.0  # just rolled over, nothing measured yet

    assert coord._billed_peak_kw() == pytest.approx(6.0)

    # Once the month HAS a reading it counts, even a low one.
    coord._peak_kw = 1.0
    assert coord._billed_peak_kw() == pytest.approx((6 * 6.0 + 2.5) / 7)


async def test_months_counted_matches_the_months_actually_averaged(
    hass: HomeAssistant, freezer: Any
) -> None:
    """capacity_peak_months must report the mean's real term count.

    It was computed at the call site as len(self._peak_history) + 1, which
    claims the in-progress month unconditionally -- but the mean leaves that
    month out until it has a reading, so the attribute over-reported for the
    whole window the skip exists for. Both now read the same list.
    """
    freezer.move_to("2026-05-01 00:30:00+02:00")
    entry = _flanders_sensor_entry("sensor.house_power")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2025-{m:02d}-01": 6.0 for m in range(6, 12)}

    # Just rolled over, nothing measured: six banked months, six averaged.
    coord._peak_kw = 0.0
    assert len(coord._peak_terms()) == 6
    assert coord._billed_peak_kw() == pytest.approx(6.0)

    # A reading arrives: the seventh term joins both the mean and the count.
    coord._peak_kw = 1.0
    assert len(coord._peak_terms()) == 7
    assert coord._billed_peak_kw() == pytest.approx((6 * 6.0 + 2.5) / 7)

    # Nothing anywhere: the floor is returned without averaging anything.
    coord._peak_history = {}
    coord._peak_kw = 0.0
    assert coord._peak_terms() == []
    assert coord._billed_peak_kw() == pytest.approx(2.5)


async def test_billed_peak_of_a_brand_new_entry_is_the_floor(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No history and no reading yet: the regulated minimum, not a crash."""
    freezer.move_to("2026-05-01 00:10:00+02:00")
    entry = _flanders_sensor_entry("sensor.house_power")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {}
    coord._peak_kw = 0.0
    assert coord._billed_peak_kw() == pytest.approx(2.5)


async def test_billed_peak_falls_back_to_the_floor_when_low(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2025-{m:02d}-01": 1.8 for m in range(1, 12)}
    hass.states.async_set(entity_id, "1.8", {"unit_of_measurement": "kW"})

    await coord._track_monthly_peak()

    assert coord._billed_peak_kw() == 2.5


async def test_month_rollover_banks_the_closed_month_and_prunes(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The closing month joins the window; only the eleven most recent
    completed months are kept, so with the running month the mean covers
    twelve."""
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2025-{m:02d}-01": 3.0 for m in range(1, 12)}
    coord._peak_month = date(2025, 12, 1)
    coord._peak_kw = 7.0

    freezer.move_to("2026-01-05 12:00:00+01:00")
    hass.states.async_set(entity_id, "1.0", {"unit_of_measurement": "kW"})
    await coord._track_monthly_peak()

    assert "2025-12-01" in coord._peak_history
    assert coord._peak_history["2025-12-01"] == 7.0
    assert len(coord._peak_history) == 11  # oldest dropped
    assert "2025-01-01" not in coord._peak_history
    assert coord._peak_month == date(2026, 1, 1)
    assert coord._peak_kw == 1.0


async def test_month_rollover_does_not_bank_a_month_with_no_reading(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A zero peak means the month never collected a reading (fresh entry, or
    HA down throughout). That is not a measured zero and must not drag the
    mean down."""
    entity_id = "sensor.house_power"
    entry = _flanders_sensor_entry(entity_id)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_month = date(2025, 12, 1)
    coord._peak_kw = 0.0

    freezer.move_to("2026-01-05 12:00:00+01:00")
    hass.states.async_set(entity_id, "3.0", {"unit_of_measurement": "kW"})
    await coord._track_monthly_peak()

    assert coord._peak_history == {}


async def test_billed_peak_ignores_history_in_fixed_mode(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Fixed mode is the user stating a peak, not measuring one, so it bypasses
    the window entirely and behaves exactly as it did before."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "capacity_mode": "fixed",
            "capacity_fixed_kw": 6.0,
        },
        title="Eneco (Flanders)",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2025-{m:02d}-01": 1.0 for m in range(1, 12)}

    await coord._track_monthly_peak()

    assert coord._billed_peak_kw() == 6.0


async def test_reset_monthly_peak_also_clears_the_history(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A bad value that already rolled into a completed month would otherwise
    drag the twelve-month mean for a year."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entry = _flanders_sensor_entry("sensor.house_power")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_kw = 4481.0
    coord._peak_history = {"2025-12-01": 4481.0}

    with patch.object(coord, "async_request_refresh", AsyncMock()):
        await coord.reset_monthly_peak()

    assert coord._peak_kw == 0.0
    assert coord._peak_history == {}


async def test_non_flanders_tick_clears_the_banked_peak_window(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Moving an entry out of Flanders drops the whole peak state, window
    included. Leaving the window behind would let a later move back to
    Flanders resume billing on year-old peaks from the previous address, and
    Fluvius restarts the window when the grid user changes."""
    freezer.move_to("2026-05-11 12:00:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_fix",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "capacity_mode": "sensor",
            "capacity_peak_sensor": "sensor.house_power",
        },
        title="Eneco (Wallonia)",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_kw = 7.0
    coord._peak_month = date(2026, 5, 1)
    coord._peak_history = {f"2025-{m:02d}-01": 6.0 for m in range(6, 12)}

    await coord._track_monthly_peak()

    assert coord._peak_kw == 0.0
    assert coord._peak_month is None
    assert coord._peak_history == {}


async def test_exclusive_night_gap_raises_a_repair_issue(hass: HomeAssistant) -> None:
    """network_eur_per_kwh bills an exclusive-night circuit at its own rate,
    then off-peak, then the day rate. TotalEnergies' Flemish card publishes an
    exclusive-night ENERGY rate but no exclusive-night distribution column, so
    the entry looks fully configured while the network side quietly falls all
    the way through to the day rate. The rate cannot be substituted (no EUR
    values in source), so surface it instead of hiding the meter type."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.be_electricity_prices.providers.base import DsoOverlay

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "totalenergies",
            "contract": "totalenergies_mycomfort",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "exclusive_night",
        },
        title="TE exclusive night",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"exclusive_night_rate_missing_{entry.entry_id}"
    registry = ir.async_get(hass)

    # No exclusive-night and no off-peak column -> the day rate is billed.
    coord._snapshot = make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(distribution_single=0.0535, transport=0.002)
        }
    )
    coord._sync_exclusive_night_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # A card that publishes the dedicated rate clears it again.
    coord._snapshot = make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.0535,
                distribution_exclusive_night=0.0454,
                transport=0.002,
            )
        }
    )
    coord._sync_exclusive_night_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # So does an off-peak rate, which the engine prefers over the day rate.
    coord._snapshot = make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.0535,
                distribution_offpeak=0.0460,
                transport=0.002,
            )
        }
    )
    coord._sync_exclusive_night_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # And a normal meter never raises it at all.
    hass.config_entries.async_update_entry(entry, data={**entry.data, "meter": "mono"})
    coord._snapshot = make_snapshot(
        dsos={
            "fluvius_antwerpen": DsoOverlay(distribution_single=0.0535, transport=0.002)
        }
    )
    coord._sync_exclusive_night_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_impact_mode_without_impact_rates_raises_a_repair_issue(
    hass: HomeAssistant,
) -> None:
    """Only Luminus' Wallonia DYNAMIC card prints the CWaPE Tarif Impact block;
    its static / variable / TOU Wallonia cards omit it. network_eur_per_kwh
    then falls back to bi-horaire while the energy side keeps routing through
    dso_impact_band, and the two disagree from 22:00 to 01:00 where Impact
    bills MEDIUM. Not the mono-rate fallback the overlay alone suggests (those
    cards do publish peak/offpeak), but the user opted into Impact and is not
    being billed on it, so surface it."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.be_electricity_prices.providers.base import DsoOverlay

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "luminus",
            "contract": "luminus_comfy",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "dso_tariff_mode": "impact",
        },
        title="Luminus impact",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"impact_rates_missing_{entry.entry_id}"
    registry = ir.async_get(hass)

    # Static Wallonia card: peak/offpeak present, Impact triplet absent.
    coord._snapshot = make_snapshot(
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.1087,
                distribution_peak=0.1205,
                distribution_offpeak=0.0666,
                transport=0.002,
            )
        }
    )
    coord._sync_impact_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # The dynamic card publishes the triplet, so it clears.
    coord._snapshot = make_snapshot(
        dsos={
            "ores": DsoOverlay(
                distribution_single=0.1087,
                distribution_pic=0.1508,
                distribution_medium=0.0982,
                distribution_eco=0.0456,
                transport=0.002,
            )
        }
    )
    coord._sync_impact_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # A user not on Impact mode never sees it.
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "dso_tariff_mode": "bi_horaire"}
    )
    coord._snapshot = make_snapshot(
        dsos={"ores": DsoOverlay(distribution_single=0.1087, transport=0.002)}
    )
    coord._sync_impact_gap_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_missing_walloon_connection_fee_raises_and_clears_a_repair(
    hass: HomeAssistant,
) -> None:
    """EnergyVision stopped printing the connection fee on its Walloon cards
    in August 2026. The extractor bills 0 rather than taking the contract
    offline, so the gap has to be disclosed instead of silently under-billing,
    and it must clear the moment the row comes back."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.be_electricity_prices.providers.base import TaxOverlay

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "energyvision",
            "contract": "energyvision_fixed_1y",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
        },
        title="EnergyVision Wallonia",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"connection_fee_missing_{entry.entry_id}"
    registry = ir.async_get(hass)

    coord._snapshot = make_snapshot(
        taxes=TaxOverlay(
            federal_excise=0.04876,
            energy_contribution=0.0,
            wallonia_renewables=0.03,
            region_connection_fee=0.0,
            region_connection_fee_unavailable=True,
        )
    )
    coord._sync_connection_fee_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # The row comes back: the fee is read again and the notice clears.
    coord._snapshot = make_snapshot(
        taxes=TaxOverlay(
            federal_excise=0.04876,
            energy_contribution=0.0,
            wallonia_renewables=0.03,
            region_connection_fee=0.00075,
        )
    )
    coord._sync_connection_fee_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_spot_monthly_mean_waits_for_the_historical_spot_fill(
    hass: HomeAssistant, freezer: Any
) -> None:
    """_monthly_spot_mean averages self._historical_spots, and
    _ensure_historical_spots is the only thing that fills it. The mean used to
    be computed first, so a tick starting with an empty cache averaged today's
    curve alone and called it the delivery month's mean. That flat rate is what
    the whole today+tomorrow table and the baked injection credit use until the
    next tick, so a cold start mis-priced the lot.

    Pins the ordering: by the time the mean is taken, the fill must have run.
    """
    from unittest.mock import MagicMock

    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
    )

    freezer.move_to("2026-07-22 10:30:00+02:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "custom",
            "contract": "custom_monthly",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
            "solar_regime": "none",
            "api_key": "TESTKEY",
        },
        title="spot monthly ordering",
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        energy=SpotMonthlyRates(factor=0.96, base=-0.009),
        injection=InjectionRates(current=None, factor=0.9, base=-0.01),
    )

    order: list[str] = []

    async def _fill(*_a: object, **_k: object) -> None:
        order.append("fill")
        # What the fill would have put in the cache.
        coord._historical_spots.update(
            {
                datetime(2026, 7, d, h, tzinfo=UTC): 0.12
                for d in range(1, 22)
                for h in range(24)
            }
        )

    def _mean(*a: object, **k: object) -> float:
        order.append("mean")
        return 0.10

    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(  # type: ignore[method-assign]
        return_value={datetime(2026, 7, 22, h, tzinfo=UTC): 0.05 for h in range(24)}
    )
    coord._ensure_historical_spots = _fill  # type: ignore[method-assign]
    coord._monthly_spot_mean = MagicMock(side_effect=_mean)  # type: ignore[method-assign]

    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost._compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        await coord._update_body()

    # A fill has to precede the first mean. Counting them would pin something
    # else: the first tick deliberately fills twice, the delivery month inline
    # and the rest of the year from a background task, and only the first of
    # those has to beat the mean.
    assert order.index("fill") < order.index("mean"), (
        f"the spot cache must be filled before the month mean is taken, got {order}"
    )


async def test_a_month_indexed_card_keeps_its_indicative_when_the_mean_is_missing(
    hass: HomeAssistant,
) -> None:
    """Eneco Power Fix and Flex index the credit on the plain arithmetic
    Belpex-injectie and print the last known value of it. With no spots there
    is no mean, and the printed figure has to be credited.

    The skip used to be gated on spp_indexed, on the belief that an SPP card
    was the only shape carrying an indicative. A month_indexed card carries
    one too, and fell through to the bake, which sets current, factor and base
    all to None: the feed-in credit disappears off the sensor rather than
    degrading by a month's lag. These contracts cannot be handed an ENTSO-E
    key from any flow step, so that was every entry on them.
    """
    from custom_components.be_electricity_prices.providers.base import (
        FixedRates,
        InjectionRates,
    )

    entry = make_entry(solar_regime="injection")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        energy=FixedRates(single=0.1234),
        injection=InjectionRates(
            current=0.0638, factor=0.84, base=-0.028, month_indexed=True
        ),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    # No key, so no spots anywhere: the shape every such entry is in.
    coord._historical_spots = {}
    coord._spot_cache = {}
    coord._spp_weights = {}
    coord._fetch_spot_prices = AsyncMock(return_value={})  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_spp_weights = AsyncMock()  # type: ignore[method-assign]

    data = await coord._async_update_data()

    assert data.injection_price_eur_per_kwh == pytest.approx(0.0638)


async def test_a_formula_only_leg_still_bakes_to_none_without_a_mean(
    hass: HomeAssistant,
) -> None:
    """The other half of the same guard, and the reason it cannot simply skip
    the bake whenever the mean is missing. A card with coefficients and NO
    printed indicative has nothing to fall back to, and leaving factor/base
    standing is the shape _injection_is_spot_formula reads as "price this per
    hour" - turning a flat monthly credit into the current slot's spot.
    """
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
    )

    entry = make_entry(solar_regime="injection")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        injection=InjectionRates(current=None, factor=0.9, base=-0.01),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._historical_spots = {}
    coord._spot_cache = {}
    coord._spp_weights = {}
    coord._fetch_spot_prices = AsyncMock(return_value={})  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_spp_weights = AsyncMock()  # type: ignore[method-assign]

    data = await coord._async_update_data()

    assert data.injection_price_eur_per_kwh is None


async def test_live_tick_never_bakes_an_spp_formula_against_the_energy_mean(
    hass: HomeAssistant,
) -> None:
    """energie.be Variabel prices consumption on Belpex_RLP and injection on
    the solar-weighted Belpex_SPP. Those are different numbers, not a coarse
    and a fine version of one, so the live bake must resolve the injection
    formula against the SPP-weighted mean or not at all.

    Without the Synergrid profile there is no SPP mean, and the card's own
    printed indicative is credited instead. Baking against the energy leg's
    mean would pay 6,05 c/kWh where the contract owes 3,00 - and silently,
    because nothing downstream can tell which mean produced the number.
    """
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        SpotMonthlyRates,
    )

    # make_entry's default is Wallonia / ORES, matching make_snapshot's DSO.
    entry = make_entry(solar_regime="injection")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    # The shape the energie.be variable card parses to.
    coord._snapshot = make_snapshot(
        energy=SpotMonthlyRates(factor=1.1872, base=0.00848),
        injection=InjectionRates(
            current=0.0343, factor=0.60, base=-0.008, spp_indexed=True
        ),
    )
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    # A month of spots at the ENERGY index, and no SPP profile at all.
    coord._historical_spots = {
        datetime(2026, 7, 15, h, tzinfo=UTC): 0.1142 for h in range(24)
    }
    coord._spot_cache = dict(coord._historical_spots)
    coord._spp_weights = {}
    coord._fetch_spot_prices = AsyncMock(  # type: ignore[method-assign]
        return_value=dict(coord._historical_spots)
    )
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_spp_weights = AsyncMock()  # type: ignore[method-assign]

    data = await coord._async_update_data()

    # The card's printed indicative, not 0.60 * 0.1142 - 0.008 = 0.0605.
    assert data.injection_price_eur_per_kwh == pytest.approx(0.0343)


def _dynamic_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "cociter",
            "contract": "cociter_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
            "api_key": "test-token",
        },
    )


async def test_spot_cache_survives_a_restart(hass: HomeAssistant, freezer: Any) -> None:
    """The day-ahead curve must round-trip through the Store, so an ENTSO-E
    outage spanning a Home Assistant restart still has something to price
    with. _historical_spots cannot stand in: it is only ever filled up to
    today, so it never carries tomorrow.

    It is restored as a FALLBACK, not as an authority: _spot_cache_day stays
    None so the first tick still fetches from ENTSO-E as usual."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord
    today = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    tomorrow = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)
    coord._spot_cache = {today: 0.12, tomorrow: 0.14}
    coord._spot_cache_day = date(2026, 8, 31)
    coord._spot_cache_includes_tomorrow = True

    saved: dict[str, Any] = {}

    async def _fake_save(payload: dict[str, Any]) -> None:
        saved.update(payload)

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    assert saved["spot_cache"] == {today.isoformat(): 0.12, tomorrow.isoformat(): 0.14}

    fresh = BePricesCoordinator(hass, entry)
    with patch.object(fresh._store, "async_load", AsyncMock(return_value=saved)):
        await fresh.async_load_persistent()

    assert fresh._spot_cache == {today: 0.12, tomorrow: 0.14}, (
        "restart must not lose the curve, tomorrow included"
    )
    assert fresh._spot_cache_day is None, (
        "restored curve is a fallback; the first tick must still ask ENTSO-E"
    )


async def test_restored_spot_cache_drops_an_outlived_day(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A blob written before the day rolled over must not come back as the
    current curve. Only today's and tomorrow's slots survive the load."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    stale = datetime(2026, 8, 29, 6, 0, tzinfo=UTC)
    current = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    payload = {
        "entry_supplier": "cociter",
        "entry_contract": "cociter_dynamic",
        "entry_region": "wallonia",
        "spot_cache": {stale.isoformat(): 0.90, current.isoformat(): 0.12},
    }
    coord = BePricesCoordinator(hass, entry)
    with patch.object(coord._store, "async_load", AsyncMock(return_value=payload)):
        await coord.async_load_persistent()

    assert coord._spot_cache == {current: 0.12}, (
        "a curve from an earlier day must not be restored"
    )


async def test_fallback_spots_prefers_the_day_ahead_cache(
    hass: HomeAssistant, freezer: Any
) -> None:
    """_spot_cache is at the resolution the contract bills on; _historical_spots
    is bucketed to the hour. When both cover today the fallback must return the
    former alone -- blending them would price a quarter-hourly entry's slots off
    two different day-ahead products."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    quarter = datetime(2026, 8, 31, 6, 15, tzinfo=UTC)
    hour = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    coord._spot_cache = {quarter: 0.12}
    coord._historical_spots = {hour: 0.99}

    assert coord._fallback_spots() == {quarter: 0.12}


async def test_fallback_spots_uses_persisted_history_on_a_cold_start(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The headline restart case: _spot_cache is empty because the process just
    started, but the persisted year-to-date cache was restored before the first
    tick and already holds today. The entry must price from it rather than go
    unavailable."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    hour = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)
    yesterday = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
    coord._spot_cache = {}
    coord._historical_spots = {yesterday: 0.99, hour: 0.12}

    assert coord._fallback_spots() == {hour: 0.12}, (
        "today's hours must come back, and only today's"
    )


async def test_fallback_spots_refuses_a_curve_that_cannot_price_today(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing on hand covers today, so there is no honest answer. Returning
    empty is what makes the caller fail the tick instead of showing a price
    carried over from an earlier day."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    yesterday = datetime(2026, 8, 30, 6, 0, tzinfo=UTC)
    coord._spot_cache = {yesterday: 0.90}
    coord._historical_spots = {yesterday: 0.90}

    assert coord._fallback_spots() == {}


async def test_spot_source_records_which_source_answered(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The coordinator must carry the provenance of the curve it priced with.

    A fallback price is still a real price, but a user has to be able to tell
    it from a source-of-record one without reading the log, so it rides out to
    the current_price sensor as spot_source rather than being folded into
    last_error (which drives the staleness Repairs card)."""
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    assert coord._spot_source == "entsoe", "defaults to the source of record"

    slot = datetime(2026, 8, 31, 6, 0, tzinfo=UTC)

    async def _fallback_answered(
        _key: str,
        _session: Any,
        _start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> tuple[dict[datetime, float], str]:
        return {slot: 0.12}, "energy-charts"

    with patch(
        "custom_components.be_electricity_prices.coordinator_spots"
        ".fetch_day_ahead_or_fallback",
        _fallback_answered,
    ):
        prices = await coord._fetch_spot_prices()

    assert prices == {slot: 0.12}
    assert coord._spot_source == "energy-charts"


async def test_backfill_falling_back_does_not_relabel_the_live_price(
    hass: HomeAssistant, freezer: Any
) -> None:
    """spot_source describes the curve current_price was built from.

    _ensure_historical_spots runs AFTER _fetch_spot_prices in the tick, so if
    the backfill is allowed to write the field, one historical chunk that had
    to fall back relabels a live price ENTSO-E served perfectly well. The
    year-to-date replay and the live curve are answered independently and can
    legitimately come from different sources.
    """
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    # The live curve came from the source of record.
    coord._spot_source = "entsoe"

    async def _entsoe_is_down(
        _self: Any,
        _start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        raise EntsoeError("ENTSO-E HTTP 503")

    async def _keyless_answered(
        _self: Any,
        start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        return {start + timedelta(hours=h): 0.10 for h in range(24)}

    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_is_down),
        patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _keyless_answered),
    ):
        await coord._ensure_historical_spots(date(2026, 8, 20), date(2026, 8, 21))

    assert coord._historical_spots, "the keyless leg still has to fill the cache"
    assert coord._spot_source == "entsoe", (
        "the backfill's source must not overwrite the live curve's"
    )


async def test_the_keyless_fallback_answers_the_whole_span_in_one_request(
    hass: HomeAssistant, freezer: Any
) -> None:
    """energy-charts limits /price to two requests per MINUTE per client IP.

    The historical walk chunks by week for ENTSO-E's benefit, and routing each
    chunk through the fallback asked that endpoint 35 times for a year: two
    chunks were answered and the rest came back HTTP 429, so an ENTSO-E outage
    left the replay with a fortnight of prices and a warning per week. The
    fallback takes plain dates with no length cap, so everything ENTSO-E could
    not answer is one request spanning the lot.
    """
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    entsoe_windows: list[tuple[datetime, datetime]] = []
    keyless_windows: list[tuple[datetime, datetime]] = []

    async def _entsoe_is_down(
        _self: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        entsoe_windows.append((start, end))
        raise EntsoeError("ENTSO-E HTTP 503")

    async def _keyless_answered(
        _self: Any,
        start: datetime,
        end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        keyless_windows.append((start, end))
        hours = int((end - start).total_seconds() // 3600)
        return {start + timedelta(hours=h): 0.10 for h in range(hours)}

    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_is_down),
        patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _keyless_answered),
    ):
        await coord._ensure_historical_spots(date(2026, 1, 1), date(2026, 8, 30))

    assert len(entsoe_windows) == 35, "the source of record is still asked week by week"
    assert len(keyless_windows) == 1, (
        f"the rate-limited fallback must be asked once, not {len(keyless_windows)}x"
    )
    # One window covering every week ENTSO-E refused, local-midnight anchored.
    assert keyless_windows[0][0] == entsoe_windows[0][0]
    assert keyless_windows[0][1] == entsoe_windows[-1][1]
    # And the year is actually filled, not merely requested.
    assert len(coord._historical_spots) > 5000


async def test_a_window_neither_source_could_serve_is_not_re_walked_hourly(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A double outage used to re-pull the whole year on every tick.

    Only an EntsoeAuthError marked its days, so a 5xx from ENTSO-E with the
    fallback rate-limited behind it left every day exactly as short as it was
    and nothing recorded the attempt: the next hourly tick walked all 35
    chunks again, and the one after that, for as long as the outage lasted.
    """
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    attempts = {"entsoe": 0, "keyless": 0}

    async def _entsoe_is_down(
        _self: Any,
        _start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        attempts["entsoe"] += 1
        raise EntsoeError("ENTSO-E HTTP 503")

    async def _keyless_is_rate_limited(
        _self: Any,
        _start: datetime,
        _end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        attempts["keyless"] += 1
        raise EntsoeError("energy-charts HTTP 429: Too Many Requests")

    with (
        patch(_SPOTS + ".EntsoeClient.fetch_day_ahead", _entsoe_is_down),
        patch(_SPOTS + ".EnergyChartsClient.fetch_day_ahead", _keyless_is_rate_limited),
    ):
        await coord._ensure_historical_spots(date(2026, 8, 1), date(2026, 8, 20))
        first = dict(attempts)
        # Same tick shape an hour later, still inside the outage TTL.
        freezer.move_to("2026-08-31 10:00:00+02:00")
        await coord._ensure_historical_spots(date(2026, 8, 1), date(2026, 8, 20))
        assert attempts == first, "the next tick must not re-walk a held window"

        # Today and yesterday are never held back: their data is still landing.
        assert date(2026, 8, 31) not in coord._spot_day_retry_at
        assert date(2026, 8, 30) not in coord._spot_day_retry_at

        # And the hold expires rather than lasting the day.
        freezer.move_to("2026-08-31 12:30:00+02:00")
        await coord._ensure_historical_spots(date(2026, 8, 1), date(2026, 8, 20))
        assert attempts["entsoe"] > first["entsoe"], (
            "a three-hour hold has to expire, the servers do come back"
        )


async def test_the_first_tick_fetches_the_month_not_the_year(
    hass: HomeAssistant, freezer: Any
) -> None:
    """async_config_entry_first_refresh runs inside setup, and setup is what
    the config flow's final step waits on. A cold cache spent that step
    fetching 35 week-chunks, which is minutes of spinner on a fresh install
    and far longer while ENTSO-E was down.

    The first tick fetches only the delivery month, which the monthly mean
    computed right after cannot do without, and schedules the rest of the year
    as a background task. Every later tick asks for the whole year again, which
    with a warm cache costs nothing.
    """
    freezer.move_to("2026-08-31 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(energy=DynamicRates(factor=1.0, base=0.01))

    windows: list[tuple[date, date]] = []
    scheduled: list[Any] = []

    async def _fill(start: date, end: date, api_key: str | None = None) -> None:
        windows.append((start, end))
        # Stand in for what the walk would have cached, so the deferred fill
        # can tell "I fetched a year" from "the cache was already warm".
        coord._historical_spots[datetime(2026, 1, 1, len(windows), tzinfo=UTC)] = 0.1

    def _capture_task(_hass: Any, coro: Any, _name: str) -> Any:
        scheduled.append(coro)
        return None

    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._fetch_spot_prices = AsyncMock(return_value={})  # type: ignore[method-assign]
    coord._ensure_historical_spots = _fill  # type: ignore[method-assign]

    with (
        patch(
            "custom_components.be_electricity_prices.ytd_cost"
            "._compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord._store, "async_save", AsyncMock()),
        patch.object(entry, "async_create_background_task", _capture_task),
    ):
        await coord._update_body()
        assert windows == [(date(2026, 8, 1), date(2026, 8, 31))], (
            "the tick setup waits on must not walk back to 1 January"
        )
        assert len(scheduled) == 1, "the rest of the year is deferred, not dropped"

        # What was deferred is the whole year, and it asks for a refresh so the
        # past hours the first tick could not price land in the sensor.
        coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
        await scheduled[0]
        assert windows[-1] == (date(2026, 1, 1), date(2026, 8, 31))
        coord.async_request_refresh.assert_awaited_once()

        # A restart runs the same fill against a cache the Store already
        # warmed. Nothing is fetched, so nothing asks for an extra full tick.
        coord.async_request_refresh.reset_mock()
        coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
        await coord._fill_year_spots()
        coord.async_request_refresh.assert_not_awaited()

        # And the deferral is once per coordinator, not once per tick.
        await coord._update_body()
        assert windows[-1] == (date(2026, 1, 1), date(2026, 8, 31))
        assert len(scheduled) == 1


def _ranking(ran_at: datetime) -> Any:
    from custom_components.be_electricity_prices.compare_quote import (
        DailyCompare,
        RankedRow,
    )

    return DailyCompare(
        rows=(
            RankedRow(label="Mine", annual=1400.0, ytd=900.0, is_own=True),
            RankedRow(label="Cheaper", annual=1150.0, ytd=740.0),
            RankedRow(label="Unpriced", annual=None, status="card unreadable"),
        ),
        own=1400.0,
        priced=2,
        total=3,
        ran_at=ran_at,
    )


async def test_potential_saving_survives_a_restart(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The ranking is produced once a day by a sweep that takes minutes. Held
    only in memory it was gone at the next restart, and the sensor read
    unknown until the following night's run."""
    freezer.move_to("2026-09-01 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord
    ran_at = datetime(2026, 9, 1, 3, 17, tzinfo=UTC)
    coord.daily_compare = _ranking(ran_at)

    saved: dict[str, Any] = {}

    async def _fake_save(payload: dict[str, Any]) -> None:
        saved.update(payload)

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    fresh = BePricesCoordinator(hass, entry)
    assert fresh.daily_compare is None, "a cold coordinator starts empty"
    with patch.object(fresh._store, "async_load", AsyncMock(return_value=saved)):
        await fresh.async_load_persistent()

    restored = fresh.daily_compare
    assert restored is not None
    assert restored.saving == pytest.approx(250.0)
    assert restored.ran_at == ran_at
    assert restored.priced == 2 and restored.total == 3
    # Every field, including the ones the sensor never shows: the options page
    # re-serves these rows, and a row short of a field it reads is what
    # crashed that page once before.
    assert [r.ytd for r in restored.rows] == [900.0, 740.0, None]
    assert [r.status for r in restored.rows] == ["", "", "card unreadable"]
    assert [r.is_own for r in restored.rows] == [True, False, False]


async def test_a_ranking_for_a_different_contract_is_not_restored(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A ranking is priced against the household's own contract. After a
    supplier swap it compares them to one they no longer hold, and the sensor
    reads as a live figure either way, so it must not come back."""
    freezer.move_to("2026-09-01 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord
    coord.daily_compare = _ranking(datetime(2026, 9, 1, 3, 17, tzinfo=UTC))
    saved: dict[str, Any] = {}

    async def _fake_save(payload: dict[str, Any]) -> None:
        saved.update(payload)

    with patch.object(coord._store, "async_save", new=_fake_save):
        await coord._save_persistent()

    saved["entry_supplier"] = "eneco"  # the swap the gate exists for
    fresh = BePricesCoordinator(hass, entry)
    with patch.object(fresh._store, "async_load", AsyncMock(return_value=saved)):
        await fresh.async_load_persistent()

    assert fresh.daily_compare is None


async def test_a_half_written_ranking_is_dropped_whole(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A ranking is a comparison between its rows. Restoring the readable half
    would re-rank the household against a subset and name a cheapest that only
    won because its rivals were dropped."""
    freezer.move_to("2026-09-01 09:00:00+02:00")
    entry = _dynamic_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    blob = {
        "entry_supplier": entry.data["supplier"],
        "entry_contract": entry.data["contract"],
        "entry_region": entry.data["region"],
        "daily_compare": {
            "ran_at": "2026-09-01T03:17:00+00:00",
            "own": 1400.0,
            "priced": 2,
            "total": 3,
            "rows": [
                {
                    "label": "Mine",
                    "annual": 1400.0,
                    "ytd": None,
                    "status": "",
                    "is_own": True,
                },
                {
                    "label": "Cheaper",
                    "annual": "not-a-number",
                    "ytd": None,
                    "status": "",
                    "is_own": False,
                },
            ],
        },
    }
    with patch.object(coord._store, "async_load", AsyncMock(return_value=blob)):
        await coord.async_load_persistent()

    assert coord.daily_compare is None


def _stale_blob(schema_version: int) -> dict[str, Any]:
    """A persisted snapshot stamped at an OLD schema, as a real cache holds it.

    Built through the production serialiser and then stripped of three
    ``InjectionRates`` fields that postdate v16 (``spp_indexed`` v21,
    ``month_indexed`` v27, ``slot_indexed`` v35), so it loads the way a genuine
    old blob does: the missing keys come back at their dataclass defaults.
    Hand-writing the body instead would pin a field list that is not the thing
    under test, and would go stale the next time a field is added.
    """
    blob = _snapshot_to_dict(
        make_snapshot(supplier="ecofix", contract="ecofix_flexy"),
        datetime(2026, 8, 2, 6, 0, tzinfo=UTC),
        "jul-etag",
        schema_version=schema_version,
    )
    blob["injection"] = {
        k: v
        for k, v in (blob["injection"] or {}).items()
        if k not in ("spp_indexed", "month_indexed", "slot_indexed")
    }
    return blob


async def _load_then_fetch_an_unreadable_card(
    hass: HomeAssistant, coord: BePricesCoordinator, blob: dict[str, Any]
) -> None:
    """Restore ``blob`` from the store, then run a fetch that finds page images."""

    async def _textless_fetch(*args: Any, **kwargs: Any) -> None:
        raise CardNotReadableError(
            "card has no text layer: 348 characters across 5 page(s)"
        )

    async def _fake_load() -> dict[str, Any]:
        return {
            "entry_supplier": coord.entry.data["supplier"],
            "entry_contract": coord.entry.data["contract"],
            "entry_region": coord.entry.data["region"],
            "snapshot": blob,
        }

    with patch.object(coord._store, "async_load", new=_fake_load):
        await coord.async_load_persistent()
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=make_stub_extractor(fetch=_textless_fetch),
    ):
        await coord._maybe_refresh_snapshot()


async def test_an_unreadable_card_replays_the_schema_rejected_snapshot(
    hass: HomeAssistant,
) -> None:
    """A supplier that went to page images has no next fetch to heal with.

    The schema gate is right in every other case: it is how a parser fix
    reaches a cached user. But it assumes a re-fetch exists, and for a card
    published as images there is none, so discarding the blob left every
    Ecofix entry with no prices at all from the first restart after 0.11.32.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    await _load_then_fetch_an_unreadable_card(hass, coord, _stale_blob(16))

    assert coord._snapshot is not None, "the rejected card must be replayed"
    assert coord._snapshot_fetched_at == datetime(2026, 8, 2, 6, 0, tzinfo=UTC), (
        "the replayed card keeps the age it was fetched at, so snapshot_age "
        "reads honestly and the 7-day stale card still fires"
    )
    assert coord._snapshot_probe_key == "jul-etag"
    assert coord._snapshot_schema_version == 16, (
        "the replayed card is written back under its own version, so a later "
        "parser fix can still invalidate it"
    )


async def test_a_blob_below_the_replay_floor_is_still_refused(
    hass: HomeAssistant,
) -> None:
    """v16 is the floor, and the number is load-bearing rather than decorative.

    Before v16 the stored blob held the card as PRICED, so replaying one
    through _resolve_snapshot would gross a professional entry's rates by its
    VAT rate a second time. A pre-v16 blob is not stale, it is wrong.
    """
    from custom_components.be_electricity_prices.snapshot_store import (
        _DEGRADED_MIN_SCHEMA_VERSION,
    )

    assert _DEGRADED_MIN_SCHEMA_VERSION == 16

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    await _load_then_fetch_an_unreadable_card(hass, coord, _stale_blob(15))

    assert coord._snapshot is None


async def test_a_replayed_entry_restamps_the_blob_once_the_card_is_readable(
    hass: HomeAssistant,
) -> None:
    """Recovery must not leave the entry writing v16 for ever.

    Without resetting the stamp in _set_snapshot, an entry that replayed once
    would keep persisting the old version even after its supplier went back to
    publishing text, and its own cache would be rejected on every boot.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    await _load_then_fetch_an_unreadable_card(hass, coord, _stale_blob(16))
    assert coord._snapshot_schema_version == 16

    async def _readable_fetch(*args: Any, **kwargs: Any) -> Any:
        return make_snapshot(supplier="ecofix", contract="ecofix_flexy")

    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=make_stub_extractor(fetch=_readable_fetch),
    ):
        coord._force_refresh = True
        await coord._maybe_refresh_snapshot()

    assert coord._snapshot_schema_version == _SNAPSHOT_SCHEMA_VERSION
    assert coord._stale_snapshot is None


async def test_an_unreadable_card_sets_a_new_entry_up_with_no_prices(
    hass: HomeAssistant,
) -> None:
    """Asking again does not turn page images into text.

    ConfigEntryNotReady parks the entry in SETUP_RETRY with no entities at all
    and nothing ever gets it out, so a brand-new Ecofix entry was unusable and
    silent. Set up anyway: the entities exist, read unavailable, and the
    Repairs card explains the workaround.
    """

    async def _textless_fetch(*args: Any, **kwargs: Any) -> None:
        raise CardNotReadableError(
            "card has no text layer: 348 characters across 5 page(s)"
        )

    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=make_stub_extractor(fetch=_textless_fetch),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is True
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    states = [
        hass.states.get(eid)
        for domain in ("sensor", "binary_sensor", "button")
        for eid in hass.states.async_entity_ids(domain)
    ]
    assert states, "the entry must still create its entities"
    assert all(s is not None and s.state in ("unavailable", "unknown") for s in states)

    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(
            DOMAIN, f"extractor_unreadable_no_prices_{entry.entry_id}"
        )
        is not None
    )


async def test_a_transient_cold_start_failure_still_retries_setup(
    hass: HomeAssistant,
) -> None:
    """The guard above must not swallow every cold-start failure.

    A timeout on the first fetch is exactly what ConfigEntryNotReady is for,
    and HA's retry is what recovers it.
    """

    async def _timeout_fetch(*args: Any, **kwargs: Any) -> None:
        raise TimeoutError

    entry = _entry()
    entry.add_to_hass(hass)
    with patch(
        "custom_components.be_electricity_prices.coordinator_snapshot.get_extractor",
        return_value=make_stub_extractor(fetch=_timeout_fetch),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id) is False
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
