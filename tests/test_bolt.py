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

"""Bolt PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers import bolt as bolt_mod
from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.bolt import parse_snapshot


def test_bolt_is_registered() -> None:
    assert "bolt" in EXTRACTORS
    assert EXTRACTORS["bolt"].label == "Bolt"
    contract_ids = {c.id for c in EXTRACTORS["bolt"].contracts}
    assert "bolt_fix" in contract_ids
    assert "bolt_variable" in contract_ids
    assert "bolt_dynamic" in contract_ids
    assert len(contract_ids) == 7


def test_fix_yearly_fee_is_monthly_x_12() -> None:
    # Bolt prints the platform fee per month (€10,99/mois). The
    # integration's yearly_fixed_fee carries the EUR/year amount, so the
    # parser multiplies by 12.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(10.99 * 12.0)


def test_fix_extracts_consumption_rates() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    # Bolt Fix prints all four meter rates as 16,71 c€/kWh.
    assert snap.energy.single == pytest.approx(0.1671)
    assert snap.energy.peak == pytest.approx(0.1671)
    assert snap.energy.offpeak == pytest.approx(0.1671)
    assert snap.energy.exclusive_night == pytest.approx(0.1671)


def test_injection_is_flat_monthly_indicative() -> None:
    # Bolt's feed-in is a flat monthly indicative printed under the
    # "Injection" header ("Prix mensuel 5,31 ..."), the same on fix and
    # variable cards. It is anchored on that header, not a positional
    # "Prix mensuel" match, and carries no spot factor/base.
    for cid, fixture in (
        ("bolt_fix", "bolt_fix.pdf"),
        ("bolt_variable", "bolt_variable.pdf"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture, layout=True), "wallonia")
        assert snap.injection is not None
        assert snap.injection.current == pytest.approx(0.0531)
        assert snap.injection.factor is None
        assert snap.injection.base is None


def test_injection_accepts_negative_second_column() -> None:
    # The July 2026 fix cards print a NEGATIVE "Exclusif nuit" second
    # injection column ("Prix mensuel 3,40 -0,43"). Only the first column
    # is billed, but the second is a required anchor token, so the parser
    # must tolerate its minus sign instead of returning None.
    text = "Injection\nPrix mensuel 3,40 -0,43 Compteur\n"
    inj = bolt_mod._extract_injection(text, "fixed")
    assert inj is not None
    assert inj.current == pytest.approx(0.034)
    assert inj.factor is None
    assert inj.base is None


def test_variable_missing_bihourly_rates_fails_loud() -> None:
    # Variable cards always publish distinct Jour/Nuit rates; if the
    # bi-horaire block drifts, raise rather than silently bill at mono.
    text = fixture_text("bolt_variable.pdf", layout=True).replace("Jour", "XXX")
    with pytest.raises(ExtractorError, match="bi-hourly"):
        parse_snapshot("bolt_variable", text, "wallonia")


def test_variable_uses_current_monthly_not_annual_estimate() -> None:
    # The bihoraire block lists the annual estimate first
    # (15,20 / 15,20) then the current monthly (14,56 / 12,09). Anchor
    # on the trailing 'Jour Nuit' header to skip the annual values.
    snap = parse_snapshot(
        "bolt_variable", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1325)
    assert snap.energy.peak == pytest.approx(0.1456)
    assert snap.energy.offpeak == pytest.approx(0.1209)
    assert snap.energy.exclusive_night == pytest.approx(0.1209)


def test_taxes_split_correctly_per_region() -> None:
    fl = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders"
    )
    wa = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    bx = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "brussels"
    )
    # Federal excise + energy contribution are nationwide.
    assert fl.taxes.federal_excise == pytest.approx(0.050329)
    assert fl.taxes.energy_contribution == pytest.approx(0.002042)
    # Flanders renewables: certificats verts + WKK. Bolt's WKK row prints
    # a single-digit footnote ref before the value ('WKK (c€/kWh) 8 0,39')
    # which the parser must skip.
    assert fl.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    assert fl.taxes.region_connection_fee == 0.0
    # Wallonia: green-energy + connection fee.
    assert wa.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert wa.taxes.region_connection_fee == pytest.approx(0.00075)
    # Brussels: green-energy only.
    assert bx.taxes.brussels_renewables == pytest.approx(0.0269)
    assert bx.taxes.region_connection_fee == 0.0


def test_wallonia_dso_handles_vertical_layout() -> None:
    # pdfplumber renders Bolt's Wallonia rows with each value on its
    # own line: "AIEG\n 10,58\n 11,77\n 6,38\n ...". The regex uses
    # `\s+` (which matches newlines) between values to handle this.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1058)
    assert aieg.distribution_peak == pytest.approx(0.1177)
    assert aieg.distribution_offpeak == pytest.approx(0.0638)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_resa_is_cheaper_than_rew_after_label_swap() -> None:
    # Bolt's PDF renders the Liege (RESA / TECTEO) and Wavre (REW /
    # Régie de Wavre) rows under swapped labels in pdfplumber's text
    # extraction; bolt.py compensates with an inverted dict. Across
    # every other supplier in the registry, RESA's distribution_single
    # is consistently lower than REW's. If a future Bolt PDF or
    # pdfplumber release fixes the upstream layout silently, the swap
    # would invert correct pricing — this assertion catches that.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert snap.dsos["resa"].distribution_single < snap.dsos["rew"].distribution_single


def test_flanders_dso_includes_transport_in_distribution() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    # Dedicated exclusive-night circuit rate (group 4), lower than the
    # normal digital distribution so a night meter isn't billed the day rate.
    excl = antwerpen.distribution_exclusive_night
    assert excl == pytest.approx(0.0481)
    assert excl is not None and excl < antwerpen.distribution_single
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_brussels_extracts_sibelga() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "brussels"
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    # The exclusive-night column is now wired (was dropped, falling back
    # to off-peak); 7,53 c€/kWh on this card.
    assert sibelga.distribution_exclusive_night == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Bolt contract"):
            await EXTRACTORS["bolt"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_fetch_for_month_rejects_mismatched_month() -> None:
    # The fix card is URL-keyed by month but carries no parseable
    # valid_until, so fetch_for_month cross-checks the printed
    # "<Month> <Year>" header against the requested month. The April
    # fixture is accepted for April and rejected for January, so a CDN
    # serving the wrong month can't mis-bill a past month at its rates.
    from custom_components.be_electricity_prices.providers import bolt

    april_text = fixture_text("bolt_fix.pdf", layout=True)

    async def _run() -> None:
        with patch.object(
            bolt, "fetch_pdf_text_layout", new=AsyncMock(return_value=april_text)
        ):
            accepted = await bolt.fetch_for_month(
                None,  # type: ignore[arg-type]
                "bolt_fix",
                "wallonia",
                date(2026, 4, 1),
            )
            assert accepted is not None
            rejected = await bolt.fetch_for_month(
                None,  # type: ignore[arg-type]
                "bolt_fix",
                "wallonia",
                date(2026, 1, 1),
            )
            assert rejected is None

    asyncio.run(_run())


def test_dynamic_extracts_belpex_formula() -> None:
    """Bolt Dynamic reads the same variable card but applies the printed
    formula to the quarter-hourly Belpex spot. The card prints EUR/MWh HTVA:
    consumption ``Belpex * 1,1192 + 13,94``, injection ``Belpex * 0,94 - 11,33``.
    Converted to EUR/kWh on the EUR/kWh spot, VAT-baked for energy (snapshot
    vat_rate 0), VAT-exempt for injection."""
    snap = parse_snapshot(
        "bolt_dynamic", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True
    assert snap.energy.factor == pytest.approx(1.1192 * 1.06)
    assert snap.energy.base == pytest.approx(13.94 / 1000.0 * 1.06)
    # Cross-check: at the card's implied Belpex (~0.0992 EUR/kWh) the formula
    # reproduces Bolt Variable's validated resolved rate (0.1325 EUR/kWh TVAC).
    assert snap.energy.factor * 0.0992 + snap.energy.base == pytest.approx(
        0.1325, abs=1e-3
    )
    # Injection is spot-indexed (factor/base, current None), VAT-exempt.
    assert isinstance(snap.injection, InjectionRates)
    assert snap.injection.current is None
    assert snap.injection.factor == pytest.approx(0.94)
    assert snap.injection.base == pytest.approx(-11.33 / 1000.0)


def test_dynamic_injection_selected_by_factor_not_position() -> None:
    """The dynamic injection row is the Belpex formula whose factor is < 1
    (Bolt redistributes a fraction of the spot). Even when the card prints
    per-meter-type consumption rows with DIFFERING factors, the parser must
    pick the injection row, not the first consumption row that differs from
    the first."""
    text = (
        "Consommation\n"
        "Simple Belpex * 1,10 + 13,94\n"
        "Jour Belpex * 1,12 + 13,94\n"
        "Nuit Belpex * 1,11 + 13,94\n"
        "Injection\n"
        "Injection nuit Belpex * 0,94 - 11,33\n"
    )
    inj = bolt_mod._extract_injection(text, "dynamic")
    assert inj is not None
    assert inj.current is None
    assert inj.factor == pytest.approx(0.94)
    assert inj.base == pytest.approx(-11.33 / 1000.0)


def test_variable_unchanged_by_dynamic_addition() -> None:
    """Adding the dynamic contract must not change how the variable card prices
    its resolved monthly rate."""
    snap = parse_snapshot(
        "bolt_variable", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1325)
    # Variable injection stays the flat monthly indicative (not spot-indexed).
    assert snap.injection is not None
    assert snap.injection.factor is None and snap.injection.base is None
