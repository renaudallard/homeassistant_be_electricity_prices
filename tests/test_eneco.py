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

"""Unit tests for the Eneco PDF extractor (run against fixture PDFs)."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.eneco import (
    fetch_for_month,
    parse_snapshot,
)


def test_fix_extracts_energy_block() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1865)
    assert snap.energy.peak == pytest.approx(0.2055)
    assert snap.energy.offpeak == pytest.approx(0.1699)
    assert snap.energy.exclusive_night == pytest.approx(0.1699)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_fix_extracts_dso_overlay() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    # The Wallonia row's "Uitsl. nacht" column is now propagated as the
    # dedicated exclusive-night meter distribution rate; it happens to
    # match offpeak on this card, but the field carries the published
    # number rather than falling back to a different column.
    assert aieg.distribution_exclusive_night == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    # Wallonia DSOs publish a prosumer (compensation-regime) tariff in the
    # last column. AIEG row trails with "81,04" EUR/kVA/year.
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.04)


def test_fix_fluvius_has_no_prosumer_rate() -> None:
    # Flemish digital meter rows print "-" for the prosumer column - SMR3
    # connections don't sit under the compensation regime.
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    assert snap.dsos["fluvius_antwerpen"].prosumer_eur_per_kva_year is None


def test_fix_extracts_all_fluvius_sub_areas() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    expected_keys = {
        "fluvius_halle_vilvoorde",
        "fluvius_antwerpen",
        "fluvius_imewo",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_zenne_dijle",
    }
    assert expected_keys <= set(snap.dsos)

    # Antwerpen is the digital-meter row "FLUVIUS ANTWERPEN 5,35 4,81 18,92
    # 18,92 52,37 - -" -> distribution 5.35 c/kWh, capacity 52.37 EUR/kW/yr.
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    # No peak/offpeak split for Flemish digital meters post-capacity-tariff.
    assert antwerpen.distribution_peak is None
    assert antwerpen.distribution_offpeak is None
    # Fluvius's second column ("Uitsl. nacht" 4,81 c/kWh) is the
    # dedicated exclusive-night meter circuit rate, distinct from the
    # day rate.
    assert antwerpen.distribution_exclusive_night == pytest.approx(0.0481)
    # Transport is the (national) Elia rate, propagated from the Wallonia rows.
    assert antwerpen.transport == pytest.approx(0.0274)
    assert antwerpen.data_management_per_year == pytest.approx(18.92)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_fix_fluvius_sub_areas_have_distinct_rates() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    rates = {
        key: snap.dsos[key].distribution_single
        for key in snap.dsos
        if key.startswith("fluvius_")
    }
    # Fluvius sub-areas publish materially different distribution rates;
    # if all eight collapsed to one value something is wrong upstream.
    assert len(set(rates.values())) > 1


def test_fix_extracts_taxes() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.energy_contribution == pytest.approx(0.002042)
    # Both regional rates are populated from the PDF; the pricing engine
    # picks the right one per region.
    assert snap.taxes.flanders_renewables == pytest.approx(0.0152)
    assert snap.taxes.wallonia_renewables == pytest.approx(0.0313)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.vat_rate == 0.0
    assert snap.publication_label.lower().startswith(
        (
            "januari",
            "februari",
            "maart",
            "april",
            "mei",
            "juni",
            "juli",
            "augustus",
            "september",
            "oktober",
            "november",
            "december",
        )
    )


def test_flex_extracts_current_monthly_rate() -> None:
    snap = parse_snapshot(fixture_text("eneco_flex.pdf"), "power_flex", "test://flex")
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1390)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    assert snap.energy.formula is not None and "BELPEX" in snap.energy.formula


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot(fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn")
    assert isinstance(snap.energy, DynamicRates)
    # PDF formula: (0.102 x BELPEX-H_eur_per_mwh + 1) x 1.06  c€/kWh
    # ENTSO-E client gives spot in EUR/kWh, so the integration uses:
    #   energy_eur_per_kwh = factor * spot_eur_per_kwh + base
    # Literal pinning: `0.102 * 10.6` is exactly what the parser
    # computes; pinning the literal 1.0812 catches a unit-conversion
    # bug that would otherwise cancel.
    assert snap.energy.factor == pytest.approx(1.0812)
    assert snap.energy.base == pytest.approx(0.0106)
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.0)
    # Realism check: at 100 EUR/MWh spot, all-in energy is ~0.119 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.11872)


def test_dynamic_publication_label_present() -> None:
    snap = parse_snapshot(fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn")
    assert snap.publication_label  # non-empty


def test_extracts_valid_until_from_geldig_line() -> None:
    """Eneco's April-2026 cards print "Geldig van 1 april 2026 t.e.m
    30 april 2026"; the snapshot must surface the end date so the
    tomorrow_prices_available binary sensor flips OFF on April 30."""
    from datetime import date

    for fixture, contract in (
        ("eneco_fix.pdf", "power_fix"),
        ("eneco_flex.pdf", "power_flex"),
        ("eneco_dyn.pdf", "power_dynamic"),
    ):
        snap = parse_snapshot(fixture_text(fixture), contract, f"test://{fixture}")
        assert snap.valid_until == date(2026, 4, 30), fixture


def test_fix_extracts_injection_rates() -> None:
    snap = parse_snapshot(fixture_text("eneco_fix.pdf"), "power_fix", "test://fix")
    inj = snap.injection
    assert inj is not None
    # Power Fix prints "Maandprijs 4,76 c/kWh" + formula "0,08 X BELPEX -2,65".
    assert inj.current == pytest.approx(0.0476)
    assert inj.factor == pytest.approx(0.8)  # 0.08 * 10
    assert inj.base == pytest.approx(-0.0265)  # -2.65 / 100
    assert inj.formula is not None and "BELPEX" in inj.formula


def test_dynamic_extracts_injection_rates() -> None:
    snap = parse_snapshot(fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn")
    inj = snap.injection
    assert inj is not None
    # Power Dynamic formula: "0,1 X BELPEX-H -1,188". No "Maandprijs" - falls
    # back to "Geschatte jaarprijs" 5,92 c/kWh.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.01188)
    assert inj.current == pytest.approx(0.0592)


# ---- fetch_for_month (historical billing) ----------------------------------


def _run(coro: object) -> object:
    return asyncio.run(coro)


def test_fetch_for_month_returns_snapshot_when_url_matches_month() -> None:
    """The Dec-2025 fixture parses cleanly and validates against the
    requested year-month: fetch_for_month must surface the snapshot."""
    text = fixture_text("eneco_flex_dec25.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.eneco.fetch_pdf_text",
        new=AsyncMock(return_value=text),
    ):
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2025, 12, 1)))  # type: ignore[arg-type]
    assert snap is not None
    assert snap.publication_label == "december 2025"
    assert snap.valid_until == date(2025, 12, 31)


def test_fetch_for_month_rejects_when_validity_does_not_cover_month() -> None:
    """If the supplier silently overwrote the historical URL with the
    current card (the typical archive-miss failure mode), the parsed
    valid_until won't intersect the requested month and we must
    return None instead of trusting it."""
    text = fixture_text("eneco_flex_dec25.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.eneco.fetch_pdf_text",
        new=AsyncMock(return_value=text),
    ):
        # The Dec-2025 fixture covers December, not March.
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2025, 3, 1)))  # type: ignore[arg-type]
    assert snap is None


def test_fetch_for_month_returns_none_on_404() -> None:
    """An archive miss (HTTP 4xx surfaces as ExtractorError) must
    degrade gracefully so the coordinator can fall back to the proxy."""
    from custom_components.be_electricity_prices.providers.base import ExtractorError

    with patch(
        "custom_components.be_electricity_prices.providers.eneco.fetch_pdf_text",
        new=AsyncMock(side_effect=ExtractorError("HTTP 404")),
    ):
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2024, 6, 1)))  # type: ignore[arg-type]
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    """A contract id that isn't in _CONTRACT_SLUGS must return None
    rather than raise -- the coordinator's monthly cache treats None
    as 'no archive' and falls back to the current snapshot."""
    snap = _run(fetch_for_month(None, "gas_dynamic", "wallonia", date(2025, 12, 1)))  # type: ignore[arg-type]
    assert snap is None
