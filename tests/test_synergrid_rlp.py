"""Synergrid RLP profile fetch/parse and the RLP-weighted month mean."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from collections.abc import Iterable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const, synergrid
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
)
from custom_components.be_electricity_prices.spot_stats import (
    _bucket_by_local_month,
    _rlp_hour_weight,
    _rlp_month_mean,
    _rlp_weighted_month_mean,
)
from custom_components.be_electricity_prices.providers.base import RlpBlend
from custom_components.be_electricity_prices.coordinator_spots import (
    _profile_store,
)
from custom_components.be_electricity_prices.synergrid import RLP_BLENDS


def _rlp_weights_from_rows(rows: Iterable[list[Any]], blend: str = "distinct") -> Any:
    """One blend straight from sheet rows: the two halves the reader runs."""
    return synergrid._weights_for_blend(synergrid._rlp_groups_from_rows(rows), blend)


def _sheet(
    curves: list[list[float]], hours: int, curve_names: list[str] | None = None
) -> list[list[Any]]:
    """Rows in the RLP96UbyDGO layout: names, DGO labels, EAN codes, then one
    quarter per row for ``hours`` local clock hours of 1 July, with one
    column per curve in ``curves`` (each already summing to one)."""
    labelled = curve_names or [f"DSO {i}" for i in range(len(curves))]
    names = [None] * 7 + list(labelled)
    labels = [None] * 7 + ["DGO"] * len(curves)
    eans = ["CET", "Year", "Month", "Day", "h", "Min", "Date"] + [
        str(541448800000 + i) for i in range(len(curves))
    ]
    rows: list[list[Any]] = [names, labels, eans]
    for q in range(hours * 4):
        rows.append(
            [
                46204.0 + q / 96,
                2026.0,
                7.0,
                1.0,
                float(q // 4),
                float(15 * (q % 4)),
                46204.0,
            ]
            + [curve[q] for curve in curves]
        )
    return rows


# Three distinct curves shared by 3 / 2 / 1 columns: the same fixture priced
# three ways, one per supplier's blend.
_FLUVIUS = [0.1, 0.1, 0.1, 0.1, 0.15, 0.15, 0.15, 0.15]  # sums to 1
_WALLONIA = [0.2, 0.2, 0.2, 0.2, 0.05, 0.05, 0.05, 0.05]
_SIBELGA = [0.05, 0.05, 0.05, 0.05, 0.2, 0.2, 0.2, 0.2]
_BLEND_ROWS = _sheet(
    [_FLUVIUS, _FLUVIUS, _FLUVIUS, _WALLONIA, _WALLONIA, _SIBELGA],
    2,
    ["Fluvius Antwerpen", "GASELWEST", "IMEWO", "ORES (Namur)", "RESA", "SIBELGA"],
)


def test_rlp_weights_distinct_blend_averages_the_distinct_curves() -> None:
    """Eight identical Fluvius columns count once: the default "distinct" blend
    is the equal mean over the DISTINCT curves, keyed by local clock hour with
    the four quarters summed (Eneco's Belpex-RLP-M)."""
    weights = _rlp_weights_from_rows(_BLEND_ROWS)
    assert set(weights) == {(7, 1, 0), (7, 1, 1)}
    assert weights[(7, 1, 0)] == pytest.approx((0.4 + 0.8 + 0.2) / 3)
    assert weights[(7, 1, 1)] == pytest.approx((0.6 + 0.2 + 0.8) / 3)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_rlp_weights_columns_blend_weights_each_curve_by_its_columns() -> None:
    """The "columns" blend weights every DSO column equally, i.e. each distinct
    curve by how many share it: 3 Fluvius, 2 Walloon, 1 Sibelga (energie.be)."""
    weights = _rlp_weights_from_rows(_BLEND_ROWS, "columns")
    assert weights[(7, 1, 0)] == pytest.approx((3 * 0.4 + 2 * 0.8 + 1 * 0.2) / 6)
    assert weights[(7, 1, 1)] == pytest.approx((3 * 0.6 + 2 * 0.2 + 1 * 0.8) / 6)
    assert sum(weights.values()) == pytest.approx(1.0)


def test_rlp_weights_flanders_blend_is_the_fluvius_curve_alone() -> None:
    """The "flanders" blend is the group carrying a Fluvius name (Energy
    Knights bills on the customer's DSO); a sheet without one is refused."""
    weights = _rlp_weights_from_rows(_BLEND_ROWS, "flanders")
    # Fluvius alone, four quarters summed: hour 0 = 4 x 0.1, hour 1 = 4 x 0.15.
    assert weights[(7, 1, 0)] == pytest.approx(0.4)
    assert weights[(7, 1, 1)] == pytest.approx(0.6)
    assert sum(weights.values()) == pytest.approx(1.0)
    no_fluvius = _sheet([_WALLONIA, _SIBELGA], 2, ["ORES (Namur)", "SIBELGA"])
    with pytest.raises(ValueError, match="no Fluvius curve"):
        _rlp_weights_from_rows(no_fluvius, "flanders")


def test_rlp_weights_refuse_a_sheet_that_does_not_sum_to_one() -> None:
    half = [0.125] * 4 + [0.0] * 4
    with pytest.raises(ValueError):
        _rlp_weights_from_rows(_sheet([half], 2))


def test_rlp_weights_skip_a_column_with_gaps_and_refuse_an_empty_sheet() -> None:
    full = [0.125] * 8
    gappy: list[Any] = [0.125] * 7 + [None]
    weights = _rlp_weights_from_rows(_sheet([full, gappy], 2))
    assert weights[(7, 1, 0)] == pytest.approx(0.5)
    with pytest.raises(ValueError):
        _rlp_weights_from_rows(_sheet([gappy], 2))
    with pytest.raises(ValueError):
        _rlp_weights_from_rows([[None] * 8])


def test_rlp_month_mean_weights_local_hours() -> None:
    """A UTC 10:00 price in July is the local 12:00 hour: the weight that
    applies is the local one, which is how Synergrid keys the profile and how
    Eneco's published values come out to the cent."""
    spots = {
        datetime(2026, 7, 1, 10, tzinfo=UTC): 50.0,  # local 12:00
        datetime(2026, 7, 1, 18, tzinfo=UTC): 150.0,  # local 20:00
        datetime(2026, 7, 1, 2, tzinfo=UTC): 999.0,  # local 04:00, no weight
    }
    weights = {(7, 1, 12): 1.0, (7, 1, 20): 3.0, (7, 1, 10): 100.0, (7, 1, 18): 100.0}
    bucket = _bucket_by_local_month(spots)
    assert _rlp_month_mean(bucket, weights, 2026, 7) == pytest.approx(
        (50.0 * 1.0 + 150.0 * 3.0) / 4.0
    )
    assert _rlp_weighted_month_mean(spots, weights, 2026, 7) == pytest.approx(125.0)
    assert _rlp_month_mean(bucket, weights, 2026, 6) is None
    assert _rlp_month_mean(bucket, {}, 2026, 7) is None


def test_rlp_hour_weight_halves_the_repeated_autumn_hour() -> None:
    """Synergrid's workbook is in local time: on the autumn changeover day
    the 02:00 row holds both instants' quarters (0,000173 against 0,000086
    for its neighbours in the 2026 file). Each of the two UTC hours that read
    as 02:00 gets half, so the month mean weighs the hour as the two hours it
    is and not four."""
    weights = {(10, 25, 1): 1.0, (10, 25, 2): 2.0, (10, 25, 3): 1.0}
    spots = {
        datetime(2026, 10, 24, 23, tzinfo=UTC): 10.0,  # 01:00 CEST
        datetime(2026, 10, 25, 0, tzinfo=UTC): 20.0,  # 02:00 CEST, first pass
        datetime(2026, 10, 25, 1, tzinfo=UTC): 40.0,  # 02:00 CET, second pass
        datetime(2026, 10, 25, 2, tzinfo=UTC): 30.0,  # 03:00 CET
    }
    assert _rlp_weighted_month_mean(spots, weights, 2026, 10) == pytest.approx(
        (10.0 + 20.0 + 40.0 + 30.0) / 4.0
    )
    plain = dt_util.as_local(datetime(2026, 10, 24, 23, tzinfo=UTC))
    assert _rlp_hour_weight(weights, plain) == 1.0
    assert _rlp_hour_weight(None, plain) is None
    assert _rlp_hour_weight({}, plain) is None
    unlisted = dt_util.as_local(datetime(2026, 10, 25, 5, tzinfo=UTC))
    assert _rlp_hour_weight(weights, unlisted) is None


async def test_fetch_rlp_returns_empty_on_download_error() -> None:
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(side_effect=aiohttp.ClientError("boom"))
    ):
        assert await synergrid.fetch_rlp_blends(session, 2026, RLP_BLENDS) == {}


async def test_fetch_rlp_returns_empty_on_a_bad_workbook_and_cleans_up() -> None:
    garbage = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsb")
    garbage.write(b"not a workbook")
    garbage.close()
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(return_value=Path(garbage.name))
    ):
        assert await synergrid.fetch_rlp_blends(session, 2026, RLP_BLENDS) == {}
    assert not Path(garbage.name).exists()


async def test_fetch_rlp_asks_the_parser_for_exactly_the_blends_wanted() -> None:
    seen: dict[str, Any] = {}

    def fake_parse(_path: Any, blends: Any) -> dict[str, dict[Any, float]]:
        seen["blends"] = list(blends)
        return {b: {(1, 1, 0): 1.0} for b in blends}

    with (
        patch.object(synergrid, "_download", new=AsyncMock(return_value=Path("x"))),
        patch.object(synergrid, "_parse_rlp_blends", new=fake_parse),
        patch.object(
            synergrid.asyncio,
            "to_thread",
            new=AsyncMock(side_effect=lambda f, *a: f(*a)),
        ),
        patch.object(synergrid, "Path"),
    ):
        await synergrid.fetch_rlp_blends(MagicMock(), 2026, ("flanders",))
    assert seen["blends"] == ["flanders"]


async def test_fetch_rlp_blends_reads_the_workbook_once_for_every_blend() -> None:
    """The read is the expensive half and the blends are reductions of it, so
    asking for three is one download and one parse, not three of each. That is
    what lets the compare page price each card on its own index without paying
    3,4 MB per blend."""
    downloads: list[str] = []

    async def fake_download(_session: Any, url: str, **_kw: Any) -> Path:
        downloads.append(url)
        return Path("x")

    def fake_parse(_path: Any, blends: Any) -> dict[str, dict[Any, float]]:
        return {b: {(1, 1, 0): float(len(b))} for b in blends}

    with (
        patch.object(synergrid, "_download", new=fake_download),
        patch.object(synergrid, "_parse_rlp_blends", new=fake_parse),
        patch.object(
            synergrid.asyncio,
            "to_thread",
            new=AsyncMock(side_effect=lambda f, *a: f(*a)),
        ),
        patch.object(synergrid, "Path"),
    ):
        out = await synergrid.fetch_rlp_blends(
            MagicMock(), 2026, ("distinct", "columns", "flanders")
        )
    assert len(downloads) == 1
    assert set(out) == {"distinct", "columns", "flanders"}


def test_one_grouping_serves_every_blend_and_a_failing_one_stands_alone() -> None:
    """The sheet is grouped once and each blend is a combination of the groups,
    which is what makes three blends one read. Only the flanders blend can fail
    on its own, when the sheet carries no Fluvius column, and it must not take
    the other two with it."""
    groups = synergrid._rlp_groups_from_rows(iter(_BLEND_ROWS))
    for blend in synergrid.RLP_BLENDS:
        assert synergrid._weights_for_blend(groups, blend) == _rlp_weights_from_rows(
            iter(_BLEND_ROWS), blend
        )
    no_fluvius = synergrid._rlp_groups_from_rows(
        iter(_sheet([_WALLONIA, _SIBELGA], 2, ["ORES (Namur)", "SIBELGA"]))
    )
    assert synergrid._weights_for_blend(no_fluvius, "distinct")
    with pytest.raises(ValueError):
        synergrid._weights_for_blend(no_fluvius, "flanders")


async def test_fetch_rlp_asks_for_the_all_dso_workbook_with_an_xlsb_suffix() -> None:
    seen: dict[str, Any] = {}

    async def fake_download(_session: Any, url: str, *, suffix: str = ".xlsx") -> Path:
        seen["url"], seen["suffix"] = url, suffix
        raise aiohttp.ClientError("stop here")

    with patch.object(synergrid, "_download", new=fake_download):
        assert await synergrid.fetch_rlp_blends(MagicMock(), 2026, RLP_BLENDS) == {}
    assert seen["suffix"] == ".xlsb"
    assert seen["url"].endswith("/2026/RLP0N%202026%20Electricity%20all%20DSOs.xlsb")


# ---- the energy leg's month spot -----------------------------------------------


def _bucket_for(prices: dict[int, float]) -> Any:
    """A September 2026 bucket with one hourly price per given local hour of
    the 10th, a Thursday."""
    from custom_components.be_electricity_prices.spot_stats import (
        _bucket_by_local_month,
    )
    from homeassistant.util import dt as dt_util

    spots = {
        datetime(2026, 9, 10, h, tzinfo=dt_util.DEFAULT_TIME_ZONE).astimezone(UTC): p
        for h, p in prices.items()
    }
    return _bucket_by_local_month(spots)


def test_energy_month_spot_prefers_the_published_index() -> None:
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
    )
    from custom_components.be_electricity_prices.spot_stats import _energy_month_spot

    leg = SpotMonthlyRates(
        factor=1.0, base=0.0, rlp_indexed=True, index_realised=0.1334366
    )
    bucket = _bucket_for({12: 0.05, 20: 0.15})
    cache: dict[tuple[int, int], float | None] = {}
    assert _energy_month_spot(
        leg, bucket, 2026, 9, date(2026, 9, 30), {(9, 10, 12): 1.0}, cache
    ) == pytest.approx(0.1334366)
    assert cache == {}


def test_energy_month_spot_weights_an_rlp_leg_and_falls_back_to_the_plain_mean() -> (
    None
):
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
    )
    from custom_components.be_electricity_prices.spot_stats import _energy_month_spot

    bucket = _bucket_for({12: 0.05, 20: 0.15})
    weights = {(9, 10, 12): 1.0, (9, 10, 20): 3.0}
    rlp_leg = SpotMonthlyRates(factor=1.0, base=0.0, rlp_indexed=True)
    plain_leg = SpotMonthlyRates(factor=1.0, base=0.0)
    today = date(2026, 9, 30)
    # The weighted mean for an RLP leg with the profile loaded.
    assert _energy_month_spot(rlp_leg, bucket, 2026, 9, today, weights, {}) == (
        pytest.approx((0.05 + 3 * 0.15) / 4)
    )
    # The plain mean for a plain leg, and for an RLP leg without the profile.
    assert _energy_month_spot(plain_leg, bucket, 2026, 9, today, weights, {}) == (
        pytest.approx(0.10)
    )
    assert _energy_month_spot(rlp_leg, bucket, 2026, 9, today, None, {}) == (
        pytest.approx(0.10)
    )
    # Weights that cover none of the cached hours fall to the plain mean too.
    assert _energy_month_spot(
        rlp_leg, bucket, 2026, 9, today, {(1, 1, 0): 1.0}, {}
    ) == pytest.approx(0.10)
    # A closed month with two cached hours is too thin to trust, weighted or not.
    assert (
        _energy_month_spot(rlp_leg, bucket, 2026, 9, date(2026, 11, 1), weights, {})
        is None
    )
    # Memoised per month.
    cache: dict[tuple[int, int], float | None] = {}
    _energy_month_spot(rlp_leg, bucket, 2026, 9, today, weights, cache)
    assert cache == {(2026, 9): pytest.approx((0.05 + 3 * 0.15) / 4)}


# ---- coordinator lifecycle, mirroring the SPP profile ---------------------------


def _entry(**extra: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=const.DOMAIN,
        data={
            const.CONF_SUPPLIER: "eneco",
            const.CONF_CONTRACT: "power_flex",
            const.CONF_REGION: const.REGION_WALLONIA,
            const.CONF_DSO: const.DSO_ORES,
            const.CONF_API_KEY: "k",
            **extra,
        },
    )


def _fake_blends(**per_blend: float) -> AsyncMock:
    """Stand in for the multi-blend fetch: one curve per blend asked for, each
    distinguishable so a caller reading the wrong one is visible."""

    async def fetch(_session: Any, _year: int, blends: Any) -> dict[str, Any]:
        return {b: {(9, 15, 10): per_blend.get(b, 2.0)} for b in blends}

    return AsyncMock(side_effect=fetch)


async def test_ensure_rlp_weights_fetches_when_stale(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await coord._ensure_rlp_weights()
        await coord._ensure_rlp_weights()  # fresh: no second download
    assert mock.await_count == 1
    assert coord._rlp_weights == {(9, 15, 10): 2.0}
    assert coord._rlp_weights_year == 2026


async def test_two_entries_share_one_profile_download(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The RLP curve is national: one year, one blend, one file. Each entry
    used to download and parse its own copy, 18 s of it on a Raspberry Pi, and
    deferring the fetch to a background task made that worse rather than
    better because every entry then started at the same moment instead of one
    after another. The second entry must find the first one's row."""
    freezer.move_to("2026-09-15 12:00:00+02:00")
    first, second = _entry(), _entry()
    first.add_to_hass(hass)
    second.add_to_hass(hass)
    coord_a = BePricesCoordinator(hass, first)
    coord_b = BePricesCoordinator(hass, second)
    fake = {(9, 15, 10): 2.0}
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await asyncio.gather(
            coord_a._ensure_rlp_weights("distinct"),
            coord_b._ensure_rlp_weights("distinct"),
        )
    assert mock.await_count == 1, "two entries, one national curve, one download"
    assert coord_a._rlp_weights == fake
    assert coord_b._rlp_weights == fake

    # A month later the row has aged past the entry's own refresh window, so
    # the shared layer does not hand it on.
    freezer.move_to("2026-10-20 12:00:00+02:00")
    third = _entry()
    third.add_to_hass(hass)
    coord_c = BePricesCoordinator(hass, third)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await coord_c._ensure_rlp_weights("distinct")
    assert mock.await_count == 1


async def test_one_download_serves_every_blend(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The curves differ per blend, but they are reductions of one file, so the
    entry holds all of them after a single download: switching its own blend
    re-reads nothing, and the compare page can price a foreign card on the
    blend that card names."""
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(distinct=1.0, columns=2.0, flanders=3.0),
    ) as mock:
        await coord._ensure_rlp_weights("distinct")
        await coord._ensure_rlp_weights("distinct")  # fresh: no second download
        await coord._ensure_rlp_weights("flanders")  # already held: no download
    assert mock.await_count == 1
    assert coord._rlp_blend == "flanders"
    assert coord._rlp_weights == {(9, 15, 10): 3.0}
    # Each blend answers its own curve, not the entry's.
    assert coord.rlp_weights_for_blend("distinct") == {(9, 15, 10): 1.0}
    assert coord.rlp_weights_for_blend("columns") == {(9, 15, 10): 2.0}
    assert coord.rlp_weights_for_blend("flanders") == {(9, 15, 10): 3.0}
    assert coord.rlp_weights_for_blend("nonesuch") is None


async def test_ensure_rlp_weights_backs_off_after_failure(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=AsyncMock(return_value={}),
    ) as mock:
        await coord._ensure_rlp_weights()
        await coord._ensure_rlp_weights()
    assert mock.await_count == 1
    assert coord._rlp_failed_at is not None
    assert coord._rlp_weighted_month_mean(2026, 9, {}) is None


async def test_the_profiles_are_not_in_the_hourly_blob(hass: HomeAssistant) -> None:
    """The per-entry cache is rewritten whole on every tick, and the Synergrid
    curves change once a month, so they do not belong in it. At 193 KB a curve
    and three RLP blends, carrying them cost 771 KB a tick, 18 MB a day per
    entry, on hardware that is usually a Raspberry Pi writing to an SD card."""
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._spp_weights = {(9, 15, 10): 1.0}
    coord._spp_weights_year = 2026
    coord._spp_fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    coord._rlp_blend_weights = {b: {(9, 15, 10): 1.0} for b in RLP_BLENDS}
    coord._rlp_weights = coord._rlp_blend_weights["distinct"]
    coord._rlp_weights_year = 2026
    coord._rlp_fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    entry.runtime_data = coord
    await coord._save_persistent()

    blob = await coord._store.async_load()
    assert blob is not None
    assert "rlp_weights" not in blob
    assert "spp_weights" not in blob


async def test_the_shared_store_carries_the_profiles_across_a_restart(
    hass: HomeAssistant, freezer: Any
) -> None:
    """One file for the whole installation, written when a profile is fetched
    rather than on every tick. A second entry, and the same entry after a
    restart, must find every blend in it and download nothing."""
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(distinct=1.0, columns=2.0, flanders=3.0),
    ) as mock:
        await coord._ensure_rlp_weights("flanders")
    assert mock.await_count == 1

    # Drop everything this process holds, as a restart does.
    hass.data.pop(const.DOMAIN, None)
    reloaded = BePricesCoordinator(hass, entry)
    await reloaded.async_load_persistent()
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await reloaded._ensure_rlp_weights("flanders")
    assert mock.await_count == 0, "the shared store should have answered"
    assert reloaded._rlp_weights == {(9, 15, 10): 3.0}
    for blend, value in (("distinct", 1.0), ("columns", 2.0), ("flanders", 3.0)):
        assert reloaded.rlp_weights_for_blend(blend) == {(9, 15, 10): value}


async def test_a_blob_written_before_the_shared_store_is_adopted(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Upgrading from 0.23.2, whose blob already carried every blend, must not
    re-download them. Seeding writes the shared store too, so the curves are
    still there on the restart after, when the blob no longer carries them."""
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    await coord._store.async_save(
        {
            "entry_supplier": "eneco",
            "entry_contract": "power_flex",
            "entry_region": const.REGION_WALLONIA,
            "rlp_weights": {
                "year": 2026,
                "blend": "columns",
                "fetched_at": "2026-09-01T00:00:00+00:00",
                "blends": {b: {"9,15,10": 2.0} for b in RLP_BLENDS},
            },
        }
    )
    await coord.async_load_persistent()
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await coord._ensure_rlp_weights("columns")
    assert mock.await_count == 0, "the 0.23.2 blob should have been adopted"
    assert coord.rlp_weights_for_blend("columns") == {(9, 15, 10): 2.0}
    blob = await _profile_store(hass).async_load()
    assert blob is not None
    assert {r["blend"] for r in blob["profiles"]} == set(RLP_BLENDS)


async def test_the_older_one_curve_blob_is_adopted_for_the_blend_it_names(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Before 0.23.2 the blob held one curve under "weights" with its blend
    beside it. That one blend is adopted; the other two were never on disk, so
    they are still fetched, which is the one file the old code would have paid
    for a blend change anyway."""
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    await coord._store.async_save(
        {
            "entry_supplier": "eneco",
            "entry_contract": "power_flex",
            "entry_region": const.REGION_WALLONIA,
            "rlp_weights": {
                "year": 2026,
                "blend": "columns",
                "fetched_at": "2026-09-01T00:00:00+00:00",
                "weights": {"9,15,10": 2.0},
            },
        }
    )
    await coord.async_load_persistent()
    from custom_components.be_electricity_prices.coordinator_spots import (
        _profile_cache,
    )

    assert _profile_cache(hass)[("rlp", 2026, "columns")][0] == {(9, 15, 10): 2.0}
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_blends",
        new=_fake_blends(),
    ) as mock:
        await coord._ensure_rlp_weights("columns")
    # The adopted curve is kept; only what was missing is asked for.
    assert mock.await_count == 1
    assert mock.await_args is not None
    assert sorted(mock.await_args.args[2]) == ["distinct", "flanders"]
    assert coord.rlp_weights_for_blend("columns") == {(9, 15, 10): 2.0}


async def test_the_compare_page_weights_a_card_on_its_own_blend(
    hass: HomeAssistant,
) -> None:
    """Three suppliers index on three reductions of the same sheet and all
    three sit in one Flanders ranking. Reading the entry's own curve for every
    row priced most of them on an index their card never names: measured on the
    August 2026 Belgian day-ahead curve the reductions stood 2,2 EUR/MWh apart,
    which is enough to reorder neighbouring rows."""
    from custom_components.be_electricity_prices.compare_inputs import (
        _coordinator_rlp_index_weights,
    )
    from custom_components.be_electricity_prices.providers.base import SpotMonthlyRates

    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._rlp_blend_weights = {
        "distinct": {(9, 15, 10): 1.0},
        "columns": {(9, 15, 10): 2.0},
        "flanders": {(9, 15, 10): 3.0},
    }
    coord._rlp_blend = "distinct"
    coord._rlp_weights = coord._rlp_blend_weights["distinct"]
    entry.runtime_data = coord

    def _card(blend: RlpBlend) -> Any:
        return SimpleNamespace(
            energy=SpotMonthlyRates(
                factor=1.0, base=0.0, rlp_indexed=True, rlp_blend=blend
            )
        )

    # The household is on the distinct curve; each quoted card still gets its
    # own, and a card that names no RLP index gets nothing.
    cards: tuple[tuple[RlpBlend, float], ...] = (
        ("distinct", 1.0),
        ("columns", 2.0),
        ("flanders", 3.0),
    )
    for blend, value in cards:
        weights = _coordinator_rlp_index_weights(entry, _card(blend))  # type: ignore[arg-type]
        assert weights == {(9, 15, 10): value}
    plain = SimpleNamespace(energy=SpotMonthlyRates(factor=1.0, base=0.0))
    assert _coordinator_rlp_index_weights(entry, plain) is None  # type: ignore[arg-type]
    assert _coordinator_rlp_index_weights(entry, None) is None


async def test_the_tick_prices_energy_on_the_rlp_mean_and_injection_on_the_plain_one(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Eneco names two indices on one card: Belpex-RLP-M for the energy and
    Belpex-injectie, the plain mean, for the feed-in credit. One tick has to
    hold both."""
    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        VariableRates,
    )
    from tests import make_snapshot

    freezer.move_to("2026-09-10 12:30:00+02:00")
    entry = _entry(solar_regime="injection")
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = make_snapshot(
        supplier="eneco",
        contract="power_flex",
        energy=VariableRates(
            current=0.1761,
            formula_factor=1.0,
            formula_base=0.0,
            month_indexed=True,
            rlp_indexed=True,
        ),
        injection=InjectionRates(
            current=0.0786, factor=1.0, base=0.0, month_indexed=True
        ),
    )
    tz = dt_util.DEFAULT_TIME_ZONE
    spots = {
        datetime(2026, 9, 10, 12, tzinfo=tz).astimezone(UTC): 0.05,
        datetime(2026, 9, 10, 20, tzinfo=tz).astimezone(UTC): 0.15,
    }
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    coord._track_monthly_peak = AsyncMock()  # type: ignore[method-assign]
    coord._historical_spots = dict(spots)
    coord._spot_cache = {}
    coord._spp_weights = {}
    coord._rlp_weights = {(9, 10, 12): 1.0, (9, 10, 20): 3.0}
    coord._rlp_weights_year = 2026
    coord._rlp_fetched_at = dt_util.utcnow()
    coord._fetch_spot_prices = AsyncMock(return_value=dict(spots))  # type: ignore[method-assign]
    coord._ensure_historical_spots = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_spp_weights = AsyncMock()  # type: ignore[method-assign]
    coord._ensure_rlp_weights = AsyncMock()  # type: ignore[method-assign]

    data = await coord._async_update_data()

    # Energy leg: factor 1, base 0 on the RLP-weighted mean (0.05 + 3 x 0.15) / 4.
    energies = [bd.energy for bd in data.hourly.values()]
    assert energies and all(e == pytest.approx(0.125) for e in energies)
    # Feed-in credit: the same coefficients on the PLAIN mean.
    assert data.injection_price_eur_per_kwh == pytest.approx(0.10)


# ---- yearly net metering allocated by the profile ------------------------------


def test_net_allocation_prices_each_register_on_its_weighted_rate() -> None:
    """Two registers, three slices. Allocated: each register's net at the
    weight-averaged rate of its slices, clamped per register. As metered: the
    slices' own products, which is the pre-profile behaviour."""
    from custom_components.be_electricity_prices.spot_stats import _NetAllocation

    netting = _NetAllocation()
    netting.add("peak", -10.0, 0.30, 1.0)  # a summer surplus at a dear rate
    netting.add("peak", 16.0, 0.10, 3.0)  # a winter draw at a cheap one
    netting.add("offpeak", -2.0, 0.20, 1.0)
    # peak: net 6 kWh at (0.30 x 1 + 0.10 x 3) / 4 = 0.15 -> 0.90; offpeak: net
    # -2 -> clamped to 0 on its own, never offsetting the peak register.
    assert netting.billed(allocated=True) == pytest.approx(0.90)
    assert netting.raw(allocated=True) == pytest.approx(0.90 - 2.0 * 0.20)
    # As metered: -3.0 + 1.6 = -1.4 on peak -> 0; offpeak -0.4 -> 0.
    assert netting.billed(allocated=False) == pytest.approx(0.0)
    assert netting.raw(allocated=False) == pytest.approx(-1.4 - 0.4)
    # A register with no weights falls back to its metered products.
    netting.add("night", 5.0, 0.20, None)
    assert netting.billed(allocated=True) == pytest.approx(0.90 + 1.0)


def test_register_for_follows_the_meter_and_the_dso_mode() -> None:
    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.spot_stats import _register_for

    tz = dt_util.DEFAULT_TIME_ZONE
    wednesday_noon = datetime(2026, 9, 9, 12, tzinfo=tz)
    wednesday_evening = datetime(2026, 9, 9, 19, tzinfo=tz)
    assert _register_for(wednesday_noon, "mono", "bi_horaire", "wallonia") == "single"
    # Wallonia 2026: 11:00-17:00 is off-peak on the bi-hourly schedule.
    assert _register_for(wednesday_noon, "bi", "bi_horaire", "wallonia") == "offpeak"
    assert (
        _register_for(wednesday_evening, "dynamic", "bi_horaire", "wallonia") == "peak"
    )
    assert _register_for(wednesday_evening, "dynamic", "impact", "wallonia") == "pic"
    assert (
        _register_for(wednesday_noon, "exclusive_night", "bi_horaire", "wallonia")
        == "night"
    )


def test_day_register_weights_sum_the_days_hours_per_register() -> None:
    from custom_components.be_electricity_prices.spot_stats import (
        _day_register_weights,
    )

    day = date(2026, 9, 9)
    weights = {(9, 9, h): 1.0 for h in range(24)}
    assert _day_register_weights(weights, day, False, "wallonia") == {"single": 24.0}
    split = _day_register_weights(weights, day, True, "wallonia")
    # Wallonia: off-peak 22:00-07:00 (9 h) and 11:00-17:00 (6 h).
    assert split == {"peak": 9.0, "offpeak": 15.0}
    assert _day_register_weights(None, day, True, "wallonia") == {}
    # The walk is by wall clock, which is how the workbook is keyed: the
    # spring changeover day has no 02:00 row and yields 23 hours, the autumn
    # one carries both 02:00 instants under one key and its mass is whole.
    spring = date(2026, 3, 29)
    spring_weights = {(3, 29, h): 1.0 for h in range(24) if h != 2}
    assert sum(
        _day_register_weights(spring_weights, spring, False, "wallonia").values()
    ) == pytest.approx(23.0)
    autumn = date(2026, 10, 25)
    autumn_weights = {(10, 25, h): (2.0 if h == 2 else 1.0) for h in range(24)}
    assert sum(
        _day_register_weights(autumn_weights, autumn, False, "wallonia").values()
    ) == pytest.approx(25.0)
