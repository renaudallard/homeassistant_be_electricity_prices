"""Synergrid RLP profile fetch/parse and the RLP-weighted month mean."""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const, synergrid
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
)
from custom_components.be_electricity_prices.spot_stats import (
    _bucket_by_local_month,
    _rlp_month_mean,
    _rlp_weighted_month_mean,
)
from custom_components.be_electricity_prices.synergrid import _rlp_weights_from_rows


def _sheet(curves: list[list[float]], hours: int) -> list[list[Any]]:
    """Rows in the RLP96UbyDGO layout: names, DGO labels, EAN codes, then one
    quarter per row for ``hours`` local clock hours of 1 July, with one
    column per curve in ``curves`` (each already summing to one)."""
    names = [None] * 7 + [f"DSO {i}" for i in range(len(curves))]
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


def test_rlp_weights_average_the_distinct_curves_and_sum_the_quarters() -> None:
    """Eight identical Fluvius columns count once: the mean is over the
    DISTINCT curves, keyed by local clock hour with the four quarters summed."""
    fluvius = [0.1, 0.1, 0.1, 0.1, 0.15, 0.15, 0.15, 0.15]  # sums to 1
    wallonia = [0.2, 0.2, 0.2, 0.2, 0.05, 0.05, 0.05, 0.05]
    sibelga = [0.05, 0.05, 0.05, 0.05, 0.2, 0.2, 0.2, 0.2]
    rows = _sheet([fluvius, fluvius, fluvius, wallonia, wallonia, sibelga], 2)
    weights = _rlp_weights_from_rows(rows)
    assert set(weights) == {(7, 1, 0), (7, 1, 1)}
    # hour 0: mean of (0.4, 0.8, 0.2) = 0.4666..; hour 1: mean of (0.6, 0.2, 0.8)
    assert weights[(7, 1, 0)] == pytest.approx((0.4 + 0.8 + 0.2) / 3)
    assert weights[(7, 1, 1)] == pytest.approx((0.6 + 0.2 + 0.8) / 3)
    assert sum(weights.values()) == pytest.approx(1.0)


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


async def test_fetch_rlp_returns_empty_on_download_error() -> None:
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(side_effect=aiohttp.ClientError("boom"))
    ):
        assert await synergrid.fetch_rlp_weights(session, 2026) == {}


async def test_fetch_rlp_returns_empty_on_a_bad_workbook_and_cleans_up() -> None:
    garbage = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsb")
    garbage.write(b"not a workbook")
    garbage.close()
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(return_value=Path(garbage.name))
    ):
        assert await synergrid.fetch_rlp_weights(session, 2026) == {}
    assert not Path(garbage.name).exists()


async def test_fetch_rlp_asks_for_the_all_dso_workbook_with_an_xlsb_suffix() -> None:
    seen: dict[str, Any] = {}

    async def fake_download(_session: Any, url: str, *, suffix: str = ".xlsx") -> Path:
        seen["url"], seen["suffix"] = url, suffix
        raise aiohttp.ClientError("stop here")

    with patch.object(synergrid, "_download", new=fake_download):
        assert await synergrid.fetch_rlp_weights(MagicMock(), 2026) == {}
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


async def test_ensure_rlp_weights_fetches_when_stale(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    fake = {(9, 15, 10): 2.0}
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_weights",
        new=AsyncMock(return_value=fake),
    ) as mock:
        await coord._ensure_rlp_weights()
        await coord._ensure_rlp_weights()  # fresh: no second download
    assert mock.await_count == 1
    assert coord._rlp_weights == fake
    assert coord._rlp_weights_year == 2026


async def test_ensure_rlp_weights_backs_off_after_failure(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-09-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator_spots.fetch_rlp_weights",
        new=AsyncMock(return_value={}),
    ) as mock:
        await coord._ensure_rlp_weights()
        await coord._ensure_rlp_weights()
    assert mock.await_count == 1
    assert coord._rlp_failed_at is not None
    assert coord._rlp_weighted_month_mean(2026, 9, {}) is None


async def test_rlp_weights_survive_persist_round_trip(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._rlp_weights = {(9, 15, 10): 2.0, (1, 1, 12): 1.5}
    coord._rlp_weights_year = 2026
    coord._rlp_fetched_at = datetime(2026, 9, 1, tzinfo=UTC)
    entry.runtime_data = coord
    await coord._save_persistent()

    reloaded = BePricesCoordinator(hass, entry)
    await reloaded.async_load_persistent()
    assert reloaded._rlp_weights == coord._rlp_weights
    assert reloaded._rlp_weights_year == 2026


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
