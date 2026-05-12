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

"""Mega PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers import mega as mega_mod
from tests import FIXTURES, fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    ImpactRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.mega import (
    _find_pdf_url,
    parse_snapshot,
)


def test_mega_is_registered() -> None:
    assert "mega" in EXTRACTORS
    assert EXTRACTORS["mega"].label == "Mega"
    contract_ids = {c.id for c in EXTRACTORS["mega"].contracts}
    # Spot-check the flagship products.
    assert "mega_smart_fixed" in contract_ids
    assert "mega_smart_flex" in contract_ids
    assert "mega_zen_fixed" in contract_ids
    assert "mega_dynamic" in contract_ids
    assert len(contract_ids) == 12


def test_listing_url_finder_picks_electricity_for_region() -> None:
    listing = (FIXTURES / "mega_listing.html").read_text()
    url = _find_pdf_url(listing, "Smart Fixed", "WL")
    assert url is not None
    assert url.startswith("https://my.mega.be/resources/tarif/")
    assert "Mega-FR-EL-B2C-WL-" in url
    assert url.endswith("Smart2204-Fixed.pdf")
    # The same listing has gas variants and other regions; they must NOT
    # match Smart Fixed/Wallonia.
    assert "NG" not in url


def test_listing_url_finder_returns_none_for_unknown_product() -> None:
    listing = (FIXTURES / "mega_listing.html").read_text()
    assert _find_pdf_url(listing, "Bogus Product", "WL") is None


def test_dynamic_extracts_consumption_formula_tvac() -> None:
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    # Mega's PDF: "formule tarifaire suivante : Day Ahead Epex Spot
    # * 1,05 + 1,35 c€/kWh" - already TVAC, spot is in c€/kWh.
    # In our model (spot in EUR/kWh): factor = 1.05, base = 0.0135 EUR.
    assert snap.energy.factor == pytest.approx(1.05)
    assert snap.energy.base == pytest.approx(0.0135)
    assert snap.energy.yearly_fixed_fee == pytest.approx(42.4)


def test_dynamic_injection_uses_separate_htva_formula_with_endash() -> None:
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    inj = snap.injection
    assert inj is not None
    # Injection block: "formule suivante (HTVA) : Day Ahead EPEX SPOT
    # Belgium * 1 – 4 c€/kWh". The dash here is a Unicode en-dash, not
    # an ASCII hyphen - the parser must read it as negative.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.04)


def test_dynamic_consumption_and_injection_are_not_swapped() -> None:
    # Mega prints the injection formula BEFORE the consumption formula
    # in the document. A naive 'first formula' / 'second formula' policy
    # gets them backwards. The parser anchors on each formula's distinct
    # label ('formule tarifaire suivante' vs 'formule suivante (HTVA)').
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.injection is not None
    assert snap.injection.factor is not None
    assert snap.injection.base is not None
    # Consumption factor is higher and base is positive.
    assert snap.energy.factor > snap.injection.factor
    assert snap.energy.base > 0.0
    # Injection base is negative (you pay to inject at low spot).
    assert snap.injection.base < 0.0


def test_smart_fixed_wallonia_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 17.12 (mono), 19.38 (jour), 15.49 (nuit / excl_nuit).
    assert snap.energy.single == pytest.approx(0.1712)
    assert snap.energy.peak == pytest.approx(0.1938)
    assert snap.energy.offpeak == pytest.approx(0.1549)
    assert snap.energy.exclusive_night == pytest.approx(0.1549)
    assert snap.energy.yearly_fixed_fee == pytest.approx(111.3)


def test_smart_fixed_brussels_extracts_sibelga_row() -> None:
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "brussels"
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_peak == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    assert sibelga.data_management_per_year == pytest.approx(14.73)


def test_wallonia_dso_carries_prosumer_rate_from_separate_table() -> None:
    # Mega lists prosumer rates in their own small table further down
    # the PDF, separate from the main DSO row. The parser cross-references
    # the two and still produces a complete DsoOverlay.
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), "wallonia"
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_flanders_dynamic_smaller_dso_table_with_external_data_fee() -> None:
    # Dynamic V cards list only 2 columns per Fluvius row (digital meter
    # only). The Tarif de gestion des données fee is broken out in a
    # separate paragraph - the parser pulls it from there.
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_v.pdf"), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.3679)
    assert antwerpen.distribution_single == pytest.approx(0.053533)
    assert antwerpen.transport == 0.0  # Rolled into distribution.
    assert antwerpen.data_management_per_year == pytest.approx(18.92)


def test_taxes_split_correctly_per_region() -> None:
    w = parse_snapshot("mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia")
    v = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    b = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "brussels"
    )
    # Federal excise + energy contribution match across regions.
    assert w.taxes.federal_excise == pytest.approx(0.0503288)
    assert v.taxes.federal_excise == pytest.approx(0.0503288)
    assert b.taxes.federal_excise == pytest.approx(0.0503288)
    assert w.taxes.energy_contribution == pytest.approx(0.0020417)
    # Wallonia: Cotisation Verte + Redevance de raccordement.
    assert w.taxes.wallonia_renewables == pytest.approx(0.03008)
    assert w.taxes.region_connection_fee == pytest.approx(0.00075)
    # Flanders: combined green + cogeneration into flanders_renewables.
    assert v.taxes.flanders_renewables > 0.0
    assert v.taxes.region_connection_fee == 0.0
    # Brussels: brussels_renewables only.
    assert b.taxes.brussels_renewables > 0.0
    assert b.taxes.flanders_renewables == 0.0
    assert b.taxes.wallonia_renewables == 0.0


def test_smart_flex_is_a_variable_contract() -> None:
    snap = parse_snapshot(
        "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    # Mega 'Flex' product values change month to month; just assert the
    # current rate is in a plausible Belgian residential range.
    assert 0.10 <= snap.energy.current <= 0.30


def test_offpeak_impact_parses_three_tier_rates() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, ImpactRates)
    # PIC is the most expensive band, ECO the cheapest -- enforced by
    # live_check too.
    assert snap.energy.pic > snap.energy.medium > snap.energy.eco
    assert snap.energy.pic == pytest.approx(0.182)
    assert snap.energy.medium == pytest.approx(0.1496)
    assert snap.energy.eco == pytest.approx(0.1011)
    assert snap.energy.yearly_fixed_fee == pytest.approx(74.2)
    # Formula text captures all three tiers from the footnote.
    assert snap.energy.formula is not None
    assert "Tarif ECO" in snap.energy.formula
    assert "Tarif MEDIUM" in snap.energy.formula
    assert "PIC" in snap.energy.formula


def test_offpeak_impact_injection_uses_per_tier_column() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    # The Impact card has no ``Compteur mono-horaire`` anchor; injection
    # lives as the second number under each Tarif row. All three rows
    # carry the same rate, so the parser pulls the first occurrence.
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0292)


def test_offpeak_impact_wallonia_dsos_carry_impact_triplet() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    for dso_key, overlay in snap.dsos.items():
        assert overlay.distribution_pic is not None, dso_key
        assert overlay.distribution_medium is not None, dso_key
        assert overlay.distribution_eco is not None, dso_key
        # Same band ordering invariant as the supplier-side rates.
        assert (
            overlay.distribution_pic
            >= overlay.distribution_medium
            >= overlay.distribution_eco
        ), dso_key


def test_offpeak_impact_contract_is_wallonia_only() -> None:
    from custom_components.be_electricity_prices.const import (
        REGION_BRUSSELS,
        REGION_FLANDERS,
        REGION_WALLONIA,
    )

    contract = next(
        c for c in EXTRACTORS["mega"].contracts if c.id == "mega_offpeak_impact_var"
    )
    assert contract.regions == frozenset({REGION_WALLONIA})
    assert REGION_FLANDERS not in contract.regions
    assert REGION_BRUSSELS not in contract.regions


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Mega contract"):
            await EXTRACTORS["mega"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


# ---- discover() filters known-unsupported products ----------------------------


async def test_discover_filters_known_unsupported_products() -> None:
    """Mega's listing exposes prepaid topup-card products that this
    integration deliberately does not model. The catalog discovery
    must exclude them so the daily live-check doesn't re-open the
    same issue every day (regression: 2026-05-05)."""
    listing = (
        '<a data-product-element="Smart Fixed" href="x">'
        '<a data-product-element="Prepaid Fixed" href="y">'
        '<a data-product-element="Prepaid Flex" href="z">'
        '<a data-product-element="Hypothetical New" href="w">'
    )
    with patch.object(mega_mod, "_fetch_listing_html", return_value=listing):
        out = await mega_mod.discover(None)  # type: ignore[arg-type]
    assert "Smart Fixed" in out
    assert "Hypothetical New" in out
    assert "Prepaid Fixed" not in out
    assert "Prepaid Flex" not in out
