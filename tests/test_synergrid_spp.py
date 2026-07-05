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

"""Synergrid SPP profile fetch/parse and SPP-weighted custom injection."""

from __future__ import annotations

import tempfile
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const, synergrid
from custom_components.be_electricity_prices.config_flow import _custom_injection_schema
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
    _compute_current_year_cost,
    _spp_weighted_month_mean,
    _spp_weighting_enabled,
)
from custom_components.be_electricity_prices.providers.base import (
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
)
from tests import make_snapshot

# ---- minimal xlsx fixture (built with the stdlib, no openpyxl) ---------------

_HEADERS = ("UTC", "Year", "Month", "Day", "Hour", "Min", "SPPExanteBE")
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _col(i: int) -> str:
    return chr(ord("A") + i)


def _build_xlsx(rows: list[tuple[int, int, int, int, int, float]]) -> bytes:
    """A one-sheet workbook mimicking the Synergrid ex-ante layout."""
    header_cells = "".join(
        f'<c r="{_col(i)}1" t="s"><v>{i}</v></c>' for i in range(len(_HEADERS))
    )
    body = f'<row r="1">{header_cells}</row>'
    for n, (year, month, day, hour, minute, value) in enumerate(rows, start=2):
        vals = (year, month, day, hour, minute, value)
        cells = "".join(
            f'<c r="{_col(1 + j)}{n}"><v>{v}</v></c>' for j, v in enumerate(vals)
        )
        body += f'<row r="{n}">{cells}</row>'
    sheet = f'<?xml version="1.0"?><worksheet {_NS}><sheetData>{body}</sheetData></worksheet>'
    shared = (
        f'<?xml version="1.0"?><sst {_NS} count="{len(_HEADERS)}" '
        f'uniqueCount="{len(_HEADERS)}">'
        + "".join(f"<si><t>{h}</t></si>" for h in _HEADERS)
        + "</sst>"
    )
    workbook = (
        f'<?xml version="1.0"?><workbook {_NS} '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="SPP_ex-ante_2026" sheetId="1" r:id="rId1"/></sheets>'
        "</workbook>"
    )
    wb_rels = (
        '<?xml version="1.0"?><Relationships '
        'xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        "</Relationships>"
    )
    buf = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    buf.close()
    data = Path(buf.name).read_bytes()
    Path(buf.name).unlink()
    return data


def _write_xlsx(rows: list[tuple[int, int, int, int, int, float]]) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.write(_build_xlsx(rows))
    tmp.close()
    return Path(tmp.name)


# ---- parser ------------------------------------------------------------------


def test_parse_hourly_weights_aggregates_quarters() -> None:
    path = _write_xlsx(
        [
            (2026, 6, 15, 10, 0, 0.5),
            (2026, 6, 15, 10, 15, 0.5),
            (2026, 6, 15, 10, 30, 0.5),
            (2026, 6, 15, 10, 45, 0.5),  # hour 10 -> 2.0
            (2026, 6, 15, 2, 0, 0.0),
            (2026, 6, 15, 2, 15, 0.0),  # night -> 0.0
            (2026, 5, 1, 12, 0, 0.3),
            (2026, 5, 1, 12, 15, 0.3),  # other month kept in the year map
        ]
    )
    try:
        weights = synergrid._parse_hourly_weights(path)
    finally:
        path.unlink(missing_ok=True)
    assert weights[(6, 15, 10)] == pytest.approx(2.0)
    assert weights[(6, 15, 2)] == pytest.approx(0.0)
    assert weights[(5, 1, 12)] == pytest.approx(0.6)


def test_parse_missing_value_column_raises() -> None:
    # A sheet whose header lacks SPPExanteBE must raise (caught by fetch).
    global _HEADERS
    saved = _HEADERS
    _HEADERS = ("UTC", "Year", "Month", "Day", "Hour", "Min", "Other")
    try:
        path = _write_xlsx([(2026, 6, 1, 12, 0, 1.0)])
        with pytest.raises(KeyError):
            synergrid._parse_hourly_weights(path)
    finally:
        _HEADERS = saved
        path.unlink(missing_ok=True)


# ---- fetch degradation -------------------------------------------------------


async def test_fetch_returns_empty_on_download_error() -> None:
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(side_effect=aiohttp.ClientError("boom"))
    ):
        assert await synergrid.fetch_spp_weights(session, 2026) == {}


async def test_fetch_returns_empty_on_bad_payload() -> None:
    garbage = tempfile.NamedTemporaryFile(delete=False)
    garbage.write(b"not a zip")
    garbage.close()
    session = MagicMock()
    with patch.object(
        synergrid, "_download", new=AsyncMock(return_value=Path(garbage.name))
    ):
        assert await synergrid.fetch_spp_weights(session, 2026) == {}
    # the fetcher must have cleaned the temp file up
    assert not Path(garbage.name).exists()


# ---- weighting math + gating -------------------------------------------------


def test_spp_weighted_month_mean_downweights_night() -> None:
    spots = {
        datetime(2026, 6, 15, 10, tzinfo=UTC): 0.10,
        datetime(2026, 6, 15, 11, tzinfo=UTC): 0.30,
        datetime(2026, 6, 15, 2, tzinfo=UTC): 1.00,  # night, expensive
    }
    weights = {(6, 15, 10): 3.0, (6, 15, 11): 1.0, (6, 15, 2): 0.0}
    # weighted mean ignores the zero-weight night hour
    assert _spp_weighted_month_mean(spots, weights, 2026, 6) == pytest.approx(
        (0.10 * 3 + 0.30 * 1) / 4
    )
    # no weights or no month overlap -> None (caller uses the flat mean)
    assert _spp_weighted_month_mean(spots, {}, 2026, 6) is None
    assert _spp_weighted_month_mean(spots, weights, 2026, 5) is None


def test_spp_weighting_enabled_gating() -> None:
    base = {
        const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
        const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: True,
        const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
    }
    assert _spp_weighting_enabled(SimpleNamespace(data=base))  # type: ignore[arg-type]
    for override in (
        {const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
        {const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: False},
        {const.CONF_SUPPLIER: "mega"},
    ):
        entry = SimpleNamespace(data={**base, **override})
        assert not _spp_weighting_enabled(entry)  # type: ignore[arg-type]


# ---- coordinator refresh + persistence --------------------------------------


def _entry(**extra: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=const.DOMAIN,
        data={
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_REGION: const.REGION_FLANDERS,
            const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN,
            **extra,
        },
    )


async def test_ensure_spp_weights_fetches_when_stale(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    fake = {(6, 15, 10): 2.0}
    with patch(
        "custom_components.be_electricity_prices.coordinator.fetch_spp_weights",
        new=AsyncMock(return_value=fake),
    ) as mock:
        await coord._ensure_spp_weights()
    assert mock.await_count == 1
    assert coord._spp_weights == fake
    assert coord._spp_weights_year == 2026


async def test_ensure_spp_weights_skips_when_fresh(
    hass: HomeAssistant, freezer: Any
) -> None:
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._spp_weights = {(6, 15, 10): 2.0}
    coord._spp_weights_year = 2026
    coord._spp_fetched_at = datetime(2026, 7, 10, tzinfo=UTC)  # 5 days old
    with patch(
        "custom_components.be_electricity_prices.coordinator.fetch_spp_weights",
        new=AsyncMock(return_value={}),
    ) as mock:
        await coord._ensure_spp_weights()
    assert mock.await_count == 0  # still fresh, no re-download


async def test_spp_weights_survive_persist_round_trip(hass: HomeAssistant) -> None:
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._spp_weights = {(6, 15, 10): 2.0, (1, 1, 12): 1.5}
    coord._spp_weights_year = 2026
    coord._spp_fetched_at = datetime(2026, 7, 1, tzinfo=UTC)
    entry.runtime_data = coord
    await coord._save_persistent()

    reloaded = BePricesCoordinator(hass, entry)
    await reloaded.async_load_persistent()
    assert reloaded._spp_weights == coord._spp_weights
    assert reloaded._spp_weights_year == 2026


# ---- config-flow toggle ------------------------------------------------------


def _has_spp_toggle(contract: str) -> bool:
    schema = _custom_injection_schema({const.CONF_CONTRACT: contract})
    return any(
        getattr(key, "schema", None) == const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED
        for key in schema.schema
    )


def test_spp_toggle_only_on_monthly() -> None:
    assert _has_spp_toggle(const.CUSTOM_CONTRACT_MONTHLY)
    assert not _has_spp_toggle(const.CUSTOM_CONTRACT_DYNAMIC)
    assert not _has_spp_toggle(const.CUSTOM_CONTRACT_FIXED)


# ---- YTD dual-mean: energy flat, injection SPP-weighted ----------------------


def _stub_extractor() -> SupplierExtractor:
    async def _fetch(
        _s: aiohttp.ClientSession, _c: str, _r: str
    ) -> Any:  # pragma: no cover
        raise NotImplementedError

    return SupplierExtractor(id="custom", label="Custom", contracts=(), fetch=_fetch)


async def test_ytd_injection_uses_spp_not_flat_mean(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Year-to-date injection credit must use the SPP-weighted month mean
    while energy stays on the flat mean."""
    from custom_components.be_electricity_prices import coordinator

    freezer.move_to("2026-07-15 12:00:00+02:00")
    snap = make_snapshot(
        supplier="custom",
        contract=const.CUSTOM_CONTRACT_MONTHLY,
        energy=SpotMonthlyRates(factor=1.0, base=0.0),
        injection=InjectionRates(factor=0.5, base=0.0, floor_at_zero=False),
    )
    entry = _entry(
        **{
            # match make_snapshot's default "ores" DSO overlay so the hour
            # is priced rather than skipped on a missing-DSO KeyError.
            const.CONF_REGION: const.REGION_WALLONIA,
            const.CONF_DSO: const.DSO_ORES,
            const.CONF_METER: const.METER_MONO,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_INJECTION_KWH: "sensor.inj_total",
            const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_BI_HORAIRE,
        }
    )
    # flat mean 0.20; SPP-weighted 0.15 (hour 10 weighted 3x vs hour 11)
    spots = {
        datetime(2026, 6, 15, 10, tzinfo=UTC): 0.10,
        datetime(2026, 6, 15, 11, tzinfo=UTC): 0.30,
    }
    weights = {(6, 15, 10): 3.0, (6, 15, 11): 1.0}

    async def _fake_hourly(
        _hass: object, entity_id: str, _start: date, _end: date
    ) -> dict[datetime, float]:
        if entity_id == "sensor.inj_total":
            return {datetime(2026, 6, 15, 10, tzinfo=UTC): 1.0}
        return {}

    with patch.object(coordinator, "_recorder_hourly_kwh", new=_fake_hourly):
        cost = await _compute_current_year_cost(
            hass,
            None,  # type: ignore[arg-type]
            _stub_extractor(),
            snap,
            entry,
            historical_spots=spots,
            spp_weights=weights,
        )
    # 1 kWh injected, no consumption: cost = -(1 * 0.5 * spp_mean).
    # spp_mean = 0.15 -> -0.075; the flat mean 0.20 would give -0.10.
    assert cost == pytest.approx(-(0.5 * 0.15))
