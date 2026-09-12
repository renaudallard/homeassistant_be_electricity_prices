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

"""Trevion PDF extractor tests against April and September 2026 fixtures."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS, REGION_FLANDERS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.trevion import (
    _BY_ID,
    _extract_validity,
    _find_card,
    fetch,
    fetch_for_month,
    parse_snapshot,
    probe,
)
from tests import fixture_text, make_text_session

_VAST = "trevion_vast_2026-09.pdf"
_VAST_APRIL = "trevion_vast_2026-04.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


def test_trevion_is_registered_with_six_flemish_contracts() -> None:
    extractor = EXTRACTORS["trevion"]
    assert extractor.label == "Trevion"
    assert {contract.id for contract in extractor.contracts} == {
        "groene_energie_vast",
        "groene_stroom_flex",
        "groene_energie_dynamisch",
        "groene_energie_dynamisch_plus",
        "lifepowr",
        "energreen",
    }
    assert all(
        contract.regions == frozenset({REGION_FLANDERS})
        for contract in extractor.contracts
    )
    assert all(not contract.spot_indexed_injection for contract in extractor.contracts)


@pytest.mark.parametrize("layout", [False, True])
def test_fixed_card_parses_both_pdf_text_orders(layout: bool) -> None:
    snap = parse_snapshot("groene_energie_vast", fixture_text(_VAST, layout=layout))
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.190518)
    assert snap.energy.peak == pytest.approx(0.210743)
    assert snap.energy.offpeak == pytest.approx(0.173402)
    assert snap.energy.exclusive_night == pytest.approx(0.173402)
    assert snap.energy.yearly_fixed_fee == pytest.approx(39.0)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.057615)
    assert snap.injection.peak == pytest.approx(0.063329)
    assert snap.injection.offpeak == pytest.approx(0.043330)
    assert snap.injection.bi_hourly is True
    assert snap.valid_until == date(2026, 9, 30)


@pytest.mark.parametrize("layout", [False, True])
def test_april_fixed_card_parses_tiered_excise_fallback(layout: bool) -> None:
    snap = parse_snapshot(
        "groene_energie_vast", fixture_text(_VAST_APRIL, layout=layout)
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0474668)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    assert snap.valid_until == date(2026, 4, 30)


@pytest.mark.parametrize(
    (
        "contract_id",
        "fixture",
        "factor",
        "base",
        "fee",
        "inj_current",
        "inj_factor",
        "inj_base",
    ),
    [
        (
            "groene_stroom_flex",
            "trevion_flex_2026-09.pdf",
            0.11342,
            0.00159,
            39.0,
            0.0501545,
            0.95,
            -0.025,
        ),
        (
            "lifepowr",
            "trevion_lifepowr_2026-09.pdf",
            0.11342,
            0.00159,
            26.5,
            0.056199,
            0.90,
            -0.015,
        ),
    ],
)
def test_monthly_contracts_parse_rlp_and_spp_formulas(
    contract_id: str,
    fixture: str,
    factor: float,
    base: float,
    fee: float,
    inj_current: float,
    inj_factor: float,
    inj_base: float,
) -> None:
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == pytest.approx(factor)
    assert snap.energy.base == pytest.approx(base)
    assert snap.energy.rlp_indexed is True
    assert snap.energy.rlp_blend == "flanders"
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(inj_current)
    assert snap.injection.factor == pytest.approx(inj_factor)
    assert snap.injection.base == pytest.approx(inj_base)
    assert snap.injection.spp_indexed is True


@pytest.mark.parametrize(
    ("contract_id", "fixture", "factor", "fee", "inj_factor", "inj_base"),
    [
        (
            "groene_energie_dynamisch",
            "trevion_dynamic_2026-09.pdf",
            0.11342,
            39.0,
            0.86,
            -0.005,
        ),
        (
            "groene_energie_dynamisch_plus",
            "trevion_dynamic_plus_2026-09.pdf",
            0.11342,
            39.0,
            0.86,
            -0.005,
        ),
        (
            "energreen",
            "trevion_energreen_2026-09.pdf",
            0.106,
            26.5,
            1.0,
            -0.013,
        ),
    ],
)
def test_dynamic_contracts_parse_quarter_hourly_formulas(
    contract_id: str,
    fixture: str,
    factor: float,
    fee: float,
    inj_factor: float,
    inj_base: float,
) -> None:
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(factor)
    assert snap.energy.base == pytest.approx(0.001378)
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.energy.quarter_hourly is True
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(inj_factor)
    assert snap.injection.base == pytest.approx(inj_base)


@pytest.mark.parametrize(
    "fixture",
    [
        _VAST,
        "trevion_flex_2026-09.pdf",
        "trevion_dynamic_2026-09.pdf",
        "trevion_dynamic_plus_2026-09.pdf",
        "trevion_lifepowr_2026-09.pdf",
        "trevion_energreen_2026-09.pdf",
    ],
)
def test_every_card_parses_regulated_flemish_costs(fixture: str) -> None:
    contract_id = {
        _VAST: "groene_energie_vast",
        "trevion_flex_2026-09.pdf": "groene_stroom_flex",
        "trevion_dynamic_2026-09.pdf": "groene_energie_dynamisch",
        "trevion_dynamic_plus_2026-09.pdf": "groene_energie_dynamisch_plus",
        "trevion_lifepowr_2026-09.pdf": "lifepowr",
        "trevion_energreen_2026-09.pdf": "energreen",
    }[fixture]
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert set(snap.dsos) == FLUVIUS_KEYS
    assert all(
        overlay.distribution_single is not None
        and overlay.capacity_eur_per_kw_year is not None
        and overlay.data_management_per_year is not None
        for overlay in snap.dsos.values()
    )
    assert snap.taxes.federal_excise == pytest.approx(0.04876)
    assert snap.taxes.energy_contribution == pytest.approx(0.0)
    assert snap.taxes.flanders_renewables == pytest.approx(0.016112)
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(0.0)
    assert snap.taxes.vat_rate == pytest.approx(0.0)
    assert snap.taxes.published_vat_rate == pytest.approx(0.0)


async def test_listing_resolves_all_cards_without_confusing_dynamic_plus() -> None:
    html = "\n".join(
        f'<a href="/tariefkaarten/{name}">{name}</a>'
        for name in (
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202609.pdf",
            "Tariefkaart-Groene-Stroom-Flex-Particulier-202609.pdf",
            "Tariefkaart-Groene-Energie-Dynamisch-Particulier-202609-1.pdf",
            "Tariefkaart-Groene-Energie-Dynamisch-Plus-Particulier-202609-1.pdf",
            "Tariefkaart-LifePowrByTrevion-Particulier-202609.pdf",
            "Tariefkaart-EnergreenByTrevion-Particulier-202609.pdf",
        )
    )
    session = make_text_session(html)
    resolved = {
        contract_id: await _find_card(session, contract)
        for contract_id, contract in _BY_ID.items()
    }
    assert "Dynamisch-Particulier" in resolved["groene_energie_dynamisch"][0]
    assert "Dynamisch-Plus-Particulier" in resolved["groene_energie_dynamisch_plus"][0]
    assert all(label == "2026-09" for _, label in resolved.values())


async def test_fetch_for_month_parses_archive_and_rejects_missing_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.be_electricity_prices.providers import trevion

    html = (
        '<a href="/tariefkaarten/'
        'Trevion-tariefkaart-Groene-energie-VAST-particulier-202604.pdf">card</a>'
    )
    session = make_text_session(html)
    render = AsyncMock(return_value=_layout(_VAST_APRIL))
    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", render)
    snap = await fetch_for_month(
        session, "groene_energie_vast", REGION_FLANDERS, date(2026, 4, 12)
    )
    assert snap is not None
    assert snap.publication_label == "2026-04"
    assert snap.valid_until == date(2026, 4, 30)
    assert (
        await fetch_for_month(
            session, "groene_energie_vast", REGION_FLANDERS, date(2026, 5, 1)
        )
        is None
    )


async def test_unsupported_contracts_and_regions_do_not_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.be_electricity_prices.providers import trevion

    freshness = AsyncMock(return_value="etag")
    monkeypatch.setattr(trevion, "head_freshness_key", freshness)
    with pytest.raises(ExtractorError, match="unknown or unsupported Trevion contract"):
        await fetch(None, "groene_energie_vast", "wallonia")  # type: ignore[arg-type]
    assert (
        await fetch_for_month(
            None,  # type: ignore[arg-type]
            "unknown",
            REGION_FLANDERS,
            date(2026, 9, 1),
        )
        is None
    )
    assert await probe(None, "groene_energie_vast", "wallonia") is None  # type: ignore[arg-type]
    freshness.assert_not_awaited()


def test_may_flex_card_writes_its_feed_in_formula_with_an_x() -> None:
    """The March to May 2026 Flex cards print the feed-in formula as
    "0,095 x Belpex_SPP_BE", the later ones with an asterisk; the backfill
    left those months absent until both signs were read."""
    snap = parse_snapshot("groene_stroom_flex", _layout("trevion_flex_2026-05.pdf"))
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == pytest.approx(0.11342)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(0.95)
    assert snap.injection.base == pytest.approx(-0.025)
    # 0,095 x 29,17 EUR/MWh - 2,5 c/kWh, the April index the card names.
    assert snap.injection.current == pytest.approx(0.95 * 0.02917 - 0.025)
    assert snap.valid_until == date(2026, 5, 31)


def test_validity_parses_dutch_month_name() -> None:
    assert _extract_validity("geldig in september 2026") == date(2026, 9, 30)
    assert _extract_validity("geen periode") is None


@pytest.mark.parametrize(
    ("old", "message"),
    [
        ("Piekuren", "fixed energy table not found"),
        ("Bijzondere accijns", "tax block not found"),
        ("Tweevoudig", "shared meter costs not found"),
    ],
)
def test_missing_fixed_card_sections_fail_loudly(old: str, message: str) -> None:
    with pytest.raises(ExtractorError, match=message):
        parse_snapshot("groene_energie_vast", _layout(_VAST).replace(old, "missing"))
