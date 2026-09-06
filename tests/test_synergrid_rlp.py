"""Synergrid RLP profile fetch/parse and the RLP-weighted month mean."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from custom_components.be_electricity_prices import synergrid
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
