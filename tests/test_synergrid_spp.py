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
from datetime import UTC, date, datetime, timedelta
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
    TaxOverlay,
    apply_vat,
    resolve_excise_band,
)
from custom_components.be_electricity_prices.providers.custom import build_snapshot
from tests import make_snapshot, make_stub_extractor

# ---- minimal xlsx fixture (built with the stdlib, no openpyxl) ---------------

_HEADERS = ("UTC", "Year", "Month", "Day", "Hour", "Min", "SPPExanteBE")
_NS = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'


def _col(i: int) -> str:
    return chr(ord("A") + i)


_EXCEL_EPOCH = datetime(1899, 12, 30)


def _build_xlsx(rows: list[tuple[datetime, float]]) -> bytes:
    """A one-sheet workbook mimicking the Synergrid ex-ante layout.

    Column A (UTC) carries the true UTC instant as an Excel serial; the
    Year/Month/Day/Hour columns are written as a deliberately-wrong LOCAL time
    (UTC + 2h) so a test can prove the parser keys on the UTC column.
    """
    header_cells = "".join(
        f'<c r="{_col(i)}1" t="s"><v>{i}</v></c>' for i in range(len(_HEADERS))
    )
    body = f'<row r="1">{header_cells}</row>'
    for n, (utc_dt, value) in enumerate(rows, start=2):
        serial = (utc_dt - _EXCEL_EPOCH).total_seconds() / 86400.0
        local = utc_dt + timedelta(hours=2)  # wrong on purpose
        vals = (
            serial,
            utc_dt.year,
            local.month,
            local.day,
            local.hour,
            utc_dt.minute,
            value,
        )
        cells = "".join(
            f'<c r="{_col(j)}{n}"><v>{v}</v></c>' for j, v in enumerate(vals)
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


def _write_xlsx(rows: list[tuple[datetime, float]]) -> Path:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.write(_build_xlsx(rows))
    tmp.close()
    return Path(tmp.name)


# ---- parser ------------------------------------------------------------------


def test_parse_keys_by_utc_not_local() -> None:
    # Every row's local Month/Day/Hour columns are UTC+2h; the parser must key
    # on the UTC column so the weight lands on the UTC hour, not the local one.
    path = _write_xlsx(
        [
            (datetime(2026, 6, 15, 8, 0), 0.5),
            (datetime(2026, 6, 15, 8, 15), 0.5),
            (datetime(2026, 6, 15, 8, 30), 0.5),
            (datetime(2026, 6, 15, 8, 45), 0.5),  # UTC hour 8 -> 2.0
            (datetime(2026, 6, 15, 0, 0), 0.0),  # UTC hour 0 -> 0.0
            (datetime(2026, 5, 1, 10, 0), 0.3),
            (datetime(2026, 5, 1, 10, 15), 0.3),  # other-month UTC hour 10 -> 0.6
        ]
    )
    try:
        weights = synergrid._parse_hourly_weights(path)
    finally:
        path.unlink(missing_ok=True)
    assert weights[(6, 15, 8)] == pytest.approx(2.0)
    assert (6, 15, 10) not in weights  # the wrong local hour is never used
    assert weights[(6, 15, 0)] == pytest.approx(0.0)
    assert weights[(5, 1, 10)] == pytest.approx(0.6)


def test_parse_missing_value_column_raises() -> None:
    # A sheet whose header lacks SPPExanteBE must raise (caught by fetch).
    global _HEADERS
    saved = _HEADERS
    _HEADERS = ("UTC", "Year", "Month", "Day", "Hour", "Min", "Other")
    try:
        path = _write_xlsx([(datetime(2026, 6, 1, 12, 0), 1.0)])
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


async def test_fetch_returns_empty_on_index_error() -> None:
    # A malformed shared-string index raises IndexError inside the parse; the
    # fetcher must still degrade to {} rather than tear down the tick.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.close()
    session = MagicMock()
    with (
        patch.object(
            synergrid, "_download", new=AsyncMock(return_value=Path(tmp.name))
        ),
        patch.object(
            synergrid, "_parse_hourly_weights", side_effect=IndexError("bad index")
        ),
    ):
        assert await synergrid.fetch_spp_weights(session, 2026) == {}
    assert not Path(tmp.name).exists()


async def test_fetch_returns_empty_on_overflow_error() -> None:
    # An out-of-range Excel date serial raises OverflowError; it must degrade
    # to {} (caught via ArithmeticError), not tear down the tick.
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.close()
    session = MagicMock()
    with (
        patch.object(
            synergrid, "_download", new=AsyncMock(return_value=Path(tmp.name))
        ),
        patch.object(
            synergrid,
            "_parse_hourly_weights",
            side_effect=OverflowError("date value out of range"),
        ),
    ):
        assert await synergrid.fetch_spp_weights(session, 2026) == {}
    assert not Path(tmp.name).exists()


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
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
        const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: True,
        const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
    }
    assert _spp_weighting_enabled(SimpleNamespace(data=base))  # type: ignore[arg-type]
    for override in (
        {const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
        {const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED: False},
        {const.CONF_SUPPLIER: "mega"},
        # tightened gate: not the monthly contract, or flat-rate injection
        {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_DYNAMIC},
        {const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_CURRENT},
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


async def test_ensure_spp_weights_backs_off_after_failure(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A failed fetch (empty) must back off, not re-download every tick."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    with patch(
        "custom_components.be_electricity_prices.coordinator.fetch_spp_weights",
        new=AsyncMock(return_value={}),
    ) as mock:
        await coord._ensure_spp_weights()  # attempt 1 fails
        await coord._ensure_spp_weights()  # within the retry TTL: no re-fetch
    assert mock.await_count == 1
    assert coord._spp_failed_at is not None


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


# ---- fixed-fee VAT gross-up --------------------------------------------------


@pytest.mark.parametrize(
    ("vat_rate", "factor"),
    [(0.06, 1.06), (0.0, 1.0)],  # custom grosses up; scraped (vat 0) is a no-op
)
def test_custom_bakes_fixed_fees_vat_inclusive(vat_rate: float, factor: float) -> None:
    # apply_vat bakes the fixed fees (so every consumption path -- live, YTD,
    # backfill, compare -- reads the correct value); per-kWh values stay
    # excl-VAT and keep vat_rate for compute_breakdown to gross up.
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
        const.CONF_CUSTOM_YEARLY_FIXED_FEE: 100.0,
        const.CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH: 5.0,
        const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 15.0,
        const.CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR: 40.0,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
        const.CONF_CUSTOM_VAT_RATE: vat_rate,
    }
    snap = apply_vat(
        build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN),
        include_vat=True,
    )
    ov = snap.dsos[const.DSO_FLUVIUS_ANTWERPEN]
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.0 * factor)
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(5.0 * factor)
    assert ov.data_management_per_year == pytest.approx(15.0 * factor)
    assert ov.capacity_eur_per_kw_year == pytest.approx(40.0 * factor)
    # per-kWh values are untouched; vat_rate is preserved for the gross-up
    assert ov.distribution_single == pytest.approx(0.05)
    assert snap.taxes.vat_rate == pytest.approx(vat_rate)


def test_apply_vat_excluded_leaves_the_card_as_printed() -> None:
    # A business that deducts VAT bears the ex-VAT cost: no fee is baked and
    # the per-kWh gross-up is switched off with it.
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
        const.CONF_CUSTOM_YEARLY_FIXED_FEE: 100.0,
        const.CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH: 5.0,
        const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 15.0,
        const.CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR: 40.0,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
        const.CONF_CUSTOM_VAT_RATE: 0.21,
    }
    snap = apply_vat(
        build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN),
        include_vat=False,
    )
    ov = snap.dsos[const.DSO_FLUVIUS_ANTWERPEN]
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.0)
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(5.0)
    assert ov.data_management_per_year == pytest.approx(15.0)
    assert ov.capacity_eur_per_kw_year == pytest.approx(40.0)
    assert ov.distribution_single == pytest.approx(0.05)
    assert snap.taxes.vat_rate == pytest.approx(0.0)


def test_apply_vat_is_identity_on_a_vat_inclusive_card() -> None:
    # Every residential card prints VAT-incl and carries vat_rate 0; the
    # preference cannot change it, and the snapshot is not even copied.
    snap = build_snapshot(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
            const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
            const.CONF_CUSTOM_YEARLY_FIXED_FEE: 100.0,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
            const.CONF_CUSTOM_VAT_RATE: 0.0,
        },
        const.REGION_FLANDERS,
        const.DSO_FLUVIUS_ANTWERPEN,
    )
    assert apply_vat(snap, include_vat=True) is snap
    assert apply_vat(snap, include_vat=False) is snap


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

    return make_stub_extractor(extractor_id="custom", label="Custom", fetch=_fetch)


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


def test_workbook_xml_entity_expansion_is_refused() -> None:
    """The four parses here run over a REMOTE workbook.

    Note what the risk actually is: the stdlib parser already refuses an
    EXTERNAL entity (ParseError, it never fetches the URL), so swapping to
    defusedxml buys nothing there. What the stdlib DOES do is expand nested
    INTERNAL entities, which is a memory DoS on a file we do not control.
    This pins that the parse refuses such a payload rather than expanding it.
    """
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE r ['
        '<!ENTITY a "AAAAAAAAAA">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        "]><r>&d;</r>"
    )
    buf = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/workbook.xml", bomb)
    buf.close()
    path = Path(buf.name)
    try:
        with pytest.raises(Exception) as excinfo:
            synergrid._resolve_sheet_path(zipfile.ZipFile(path))
        assert "Entities" in type(excinfo.value).__name__, (
            f"expansion must be refused, got {type(excinfo.value).__name__}"
        )
    finally:
        path.unlink(missing_ok=True)


async def test_fetch_spp_weights_still_never_raises_on_a_hostile_payload() -> None:
    """fetch_spp_weights promises it never raises. defusedxml's exceptions are
    not ParseError subclasses, so confirm a hostile workbook still degrades to
    an empty mapping and the coordinator falls back to the plain mean."""
    buf = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?>'
            '<!DOCTYPE r [<!ENTITY a "AA"><!ENTITY b "&a;&a;&a;">]><r>&b;</r>',
        )
    buf.close()
    path = Path(buf.name)

    async def _fake_download(_session: object, _url: str) -> Path:
        return path

    with patch.object(synergrid, "_download", _fake_download):
        out = await synergrid.fetch_spp_weights(MagicMock(), 2026)
    assert out == {}


# ---- degressive federal excise ----------------------------------------------


@pytest.mark.parametrize(
    ("annual_kwh", "expected"),
    [
        (0.0, 0.01421),  # a fresh connection still sits in the first band
        (3500.0, 0.01421),
        (20_000.0, 0.01421),  # the boundary belongs to the band it closes
        (20_000.1, 0.01209),
        (50_000.0, 0.01209),
        (750_000.0, 0.01139),
        (5_000_000.0, 0.01139),  # past the card's ceiling: clamp, don't invent
    ],
)
def test_resolve_excise_band_picks_the_band_for_the_volume(
    annual_kwh: float, expected: float
) -> None:
    # The bands are Engie's August 2026 professional schedule, in EUR/kWh.
    snap = make_snapshot(
        taxes=TaxOverlay(
            federal_excise=0.0,
            energy_contribution=0.0019261,
            federal_excise_bands=(
                (20_000.0, 0.01421),
                (50_000.0, 0.01209),
                (1_000_000.0, 0.01139),
            ),
        )
    )
    assert resolve_excise_band(snap, annual_kwh).taxes.federal_excise == pytest.approx(
        expected
    )


def test_resolve_excise_band_is_identity_without_bands() -> None:
    # Every residential card prints one rate; the resolver must not copy.
    snap = make_snapshot(
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002)
    )
    assert resolve_excise_band(snap, 3500.0) is snap
    assert resolve_excise_band(snap, 900_000.0) is snap


def test_resolve_excise_band_leaves_the_rest_of_the_card_alone() -> None:
    snap = make_snapshot(
        taxes=TaxOverlay(
            federal_excise=0.0,
            energy_contribution=0.0019261,
            flanders_renewables=0.01466,
            energy_fund_eur_per_month=10.07,
            vat_rate=0.21,
            federal_excise_bands=((20_000.0, 0.01421), (50_000.0, 0.01209)),
        )
    )
    out = resolve_excise_band(snap, 30_000.0)
    assert out.taxes.federal_excise == pytest.approx(0.01209)
    assert out.taxes.energy_contribution == pytest.approx(0.0019261)
    assert out.taxes.flanders_renewables == pytest.approx(0.01466)
    assert out.taxes.energy_fund_eur_per_month == pytest.approx(10.07)
    assert out.taxes.vat_rate == pytest.approx(0.21)
    assert out.energy is snap.energy
    assert out.dsos is snap.dsos


# ---- professional injection is taxed --------------------------------------


def _pro_snapshot(*, vat_applies: bool) -> Any:
    return make_snapshot(
        taxes=TaxOverlay(
            federal_excise=0.01421, energy_contribution=0.0019261, vat_rate=0.21
        ),
        injection=InjectionRates(
            current=0.05, factor=1.0, base=-0.013, vat_applies=vat_applies
        ),
    )


def test_apply_vat_grosses_a_taxed_injection() -> None:
    # Professional cards state injection IS subject to 21% VAT, the reverse
    # of the residential exemption. None of these rates reaches the pricing
    # engine's per-component gross-up, so apply_vat has to bake them.
    inj = apply_vat(_pro_snapshot(vat_applies=True), include_vat=True).injection
    assert inj is not None
    assert inj.current == pytest.approx(0.05 * 1.21)
    assert inj.factor == pytest.approx(1.21)
    assert inj.base == pytest.approx(-0.013 * 1.21)


def test_apply_vat_leaves_a_taxed_injection_alone_when_vat_is_deducted() -> None:
    inj = apply_vat(_pro_snapshot(vat_applies=True), include_vat=False).injection
    assert inj is not None
    assert inj.current == pytest.approx(0.05)
    assert inj.base == pytest.approx(-0.013)


def test_apply_vat_never_touches_an_exempt_injection() -> None:
    # An ex-VAT card whose injection is exempt: the consumption side grosses
    # up, the injection must not.
    snap = _pro_snapshot(vat_applies=False)
    out = apply_vat(snap, include_vat=True)
    assert out.injection is snap.injection
