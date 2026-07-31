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

"""TotalEnergies PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.totalenergies import (
    _extract_injection,
    parse_snapshot,
)


def test_totalenergies_is_registered() -> None:
    assert "totalenergies" in EXTRACTORS
    assert EXTRACTORS["totalenergies"].label == "TotalEnergies"
    contract_ids = {c.id for c in EXTRACTORS["totalenergies"].contracts}
    assert "totalenergies_mydynamic" in contract_ids
    assert "totalenergies_mycomfort" in contract_ids
    assert "totalenergies_mycomfort_fixed" in contract_ids
    assert len(contract_ids) == 9


def test_dynamic_wallonia_extracts_consumption_formula() -> None:
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_w.pdf", layout=True),
        "wallonia",
    )
    assert isinstance(snap.energy, DynamicRates)
    # PDF prints "0.1034 * BELPEXH + 1.75" (HTVA, 6% VAT).
    # Literal pinning so a 1.06 ⇄ 10 unit-conversion swap can't cancel.
    assert snap.energy.factor == pytest.approx(1.09604)
    assert snap.energy.base == pytest.approx(0.01855)
    assert snap.energy.yearly_fixed_fee == pytest.approx(90.0)


def test_dynamic_brussels_pulls_base_from_split_layout() -> None:
    # Brussels Dynamic prints the formula across two lines:
    #   "0.1034 * BELPEXH + 0.1034 * BELPEXH + ... + Formule tarifaire"
    #   "3.85 3.85 3.85 3.75"
    # The Wallonia/Flanders pattern (formula and base on one line) does
    # NOT match here. The parser must fall back to picking the base from
    # the line right after "Formule tarifaire".
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_b.pdf", layout=True),
        "brussels",
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.09604)
    assert snap.energy.base == pytest.approx(0.04081)


def test_dynamic_injection_formula_uses_distinct_anchor() -> None:
    # Both consumption and injection use the BELPEX formula, but the
    # injection block always prints cleanly. Anchor on the "Injection**"
    # block so the consumption formula above is never picked up by
    # mistake.
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_w.pdf", layout=True),
        "wallonia",
    )
    inj = snap.injection
    assert inj is not None
    # PDF: 0.1 * BELPEXH - 1.3 (HTVA, residential injection is VAT-exempt).
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.013)


def test_missing_federal_excise_is_fatal() -> None:
    # The federal excise is mandatory with no fallback; a miss must raise
    # rather than silently undercount the bill by ~5 c€/kWh.
    text = fixture_text("totalenergies_dynamic_w.pdf", layout=True).replace(
        "Consommation entre 0 et 3.000 kWh", "XXX"
    )
    with pytest.raises(ExtractorError, match="federal excise"):
        parse_snapshot("totalenergies_mydynamic", text, "wallonia")


def test_dynamic_injection_missing_formula_fails_loud() -> None:
    # A dynamic card whose injection block prints the indicative but not
    # the BELPEXH formula must not silently price feed-in at the flat
    # monthly rate every hour - it must raise.
    text = "Injection**\nIndicatif\n9.15\n"
    with pytest.raises(ExtractorError, match="BELPEXH formula not found"):
        _extract_injection(text, "dynamic")


def test_mycomfort_fixed_wallonia_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "totalenergies_mycomfort_fixed",
        fixture_text("totalenergies_mycomfort_fixed_w.pdf", layout=True),
        "wallonia",
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 18.41 / 19.66 / 17.32 / 17.13 c€/kWh.
    assert snap.energy.single == pytest.approx(0.1841)
    assert snap.energy.peak == pytest.approx(0.1966)
    assert snap.energy.offpeak == pytest.approx(0.1732)
    assert snap.energy.exclusive_night == pytest.approx(0.1713)
    assert snap.energy.yearly_fixed_fee == pytest.approx(90.0)


def test_three_column_consumption_card_fails_loud() -> None:
    # A 3-column static card prints only mono / jour / nuit. The old
    # 4-value regex used \s+ between groups, so it spanned the line break
    # and grabbed the 90,00 yearly fee as the exclusive-night rate
    # (0.90 EUR/kWh) with no error. The row must now end at the line
    # break: too few columns must miss and fail loud here.
    text = fixture_text("totalenergies_mycomfort_fixed_w.pdf", layout=True).replace(
        "17,32 17,13", "17,32\n90,00"
    )
    with pytest.raises(ExtractorError, match="consumption block"):
        parse_snapshot("totalenergies_mycomfort_fixed", text, "wallonia")


def test_mycomfort_variable_uses_realized_monthly_indicative() -> None:
    # Variable cards index monthly: the "Consommation" table row is the
    # Vlaamse-Nutsregulator annual ESTIMATE, while the price actually
    # billed is the realized monthly indicative ("prix mensuels ...
    # BELPEX_M_RLP"). Use the realized block (13,53 / 14,65 / 12,55 /
    # 12,39), not the estimate (15,62 / 16,96 / 14,47 / 14,29).
    snap = parse_snapshot(
        "totalenergies_mycomfort",
        fixture_text("totalenergies_mycomfort_v.pdf", layout=True),
        "flanders",
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1353)
    assert snap.energy.peak == pytest.approx(0.1465)
    assert snap.energy.offpeak == pytest.approx(0.1255)
    assert snap.energy.exclusive_night == pytest.approx(0.1239)
    # Flanders also wraps the cotisation header; the value lives only in
    # the 8th Fluvius column and must still reach the all-in price.
    assert snap.taxes.energy_contribution == pytest.approx(0.002)


def test_non_dynamic_injection_uses_realized_monthly_indicative() -> None:
    # Non-dynamic injection is monthly-indexed: use the realized monthly
    # indicative (1,12 c€/kWh) printed in the "prix mensuels de
    # l'injection" block, not the V-test annual estimate in the table.
    # Holds for both variable and fixed cards; factor/base stay None.
    for cid, fixture, region in (
        ("totalenergies_mycomfort", "totalenergies_mycomfort_v.pdf", "flanders"),
        (
            "totalenergies_mycomfort_fixed",
            "totalenergies_mycomfort_fixed_w.pdf",
            "wallonia",
        ),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture, layout=True), region)
        assert snap.injection is not None
        assert snap.injection.current == pytest.approx(0.0112)
        assert snap.injection.factor is None
        assert snap.injection.base is None


def test_impact_parses_as_flat_supplier_energy_with_impact_dso_bands() -> None:
    # TE Impact is a 3-band CWaPE tariff: the supplier energy is flat
    # (Heures PIC/MEDIUM/ECO all equal), the band variation is DSO-side
    # (Tarif IMPACT distribution). It used to fail to parse as a standard
    # variable card; it must now parse with a flat supplier energy and the
    # CWaPE Impact distribution bands (used under dso_tariff_mode=impact).
    snap = parse_snapshot(
        "totalenergies_impact",
        fixture_text("totalenergies_impact_w.pdf", layout=True),
        "wallonia",
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.139)
    # Flat supplier energy: the band split is DSO-side, not supplier-side.
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0147)
    dso = next(iter(snap.dsos.values()))
    assert dso.distribution_pic is not None
    assert dso.distribution_medium is not None
    assert dso.distribution_eco is not None


def test_brussels_extracts_sibelga_row() -> None:
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_b.pdf", layout=True),
        "brussels",
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    # Metering fee 14.73 + Sibelga <=13kVA power term 50.07 (printed on a
    # separate "Terme de puissance" line; both billed to a residential
    # Brussels connection, no separate capacity charge).
    assert sibelga.data_management_per_year == pytest.approx(14.73 + 50.07)
    # The Brussels card wraps the "Cotisation sur l'énergie" header, so
    # the federal contribution only appears as the 7th SIBELGA column;
    # it must still reach the all-in price (was silently dropped to 0).
    assert snap.taxes.energy_contribution == pytest.approx(0.002)


def test_zero_energy_contribution_is_accepted() -> None:
    # The federal levy fell to zero on 2026-08-01. A card that prints the
    # row with a zero value is reporting a real rate, so it must parse
    # rather than trip the "not found" drift guard.
    text = fixture_text("totalenergies_dynamic_w.pdf", layout=True).replace(
        "Cotisation sur l’énergie 0,20", "Cotisation sur l’énergie 0,00"
    )
    snap = parse_snapshot("totalenergies_mydynamic", text, "wallonia")
    assert snap.taxes.energy_contribution == 0.0


def test_missing_energy_contribution_is_fatal() -> None:
    # A row that is absent altogether is still drift: neither the labelled
    # line nor (in Wallonia) any table fallback exposes the value, so the
    # fetch must fail loud instead of billing a silent zero.
    text = fixture_text("totalenergies_dynamic_w.pdf", layout=True).replace(
        "Cotisation sur l’énergie", "XXX"
    )
    with pytest.raises(ExtractorError, match="energy contribution not found"):
        parse_snapshot("totalenergies_mydynamic", text, "wallonia")


def test_wallonia_dso_carries_full_row() -> None:
    # TotalEnergies's Wallonia rows have 12 numbers; the parser pulls
    # mono / jour / nuit (cols 0-2), data_mgmt (col 7), transport (col 8)
    # and prosumer (col 9) - the IMPACT triplet (cols 4-6) and capacity
    # cols 10-11 aren't surfaced.
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_w.pdf", layout=True),
        "wallonia",
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_flanders_dso_includes_transport_in_distribution() -> None:
    # Flanders rows print distribution that already include transport
    # (same convention as Engie/Luminus/Mega Flanders), so transport=0
    # and the c€/kWh value lands in distribution_single.
    snap = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_v.pdf", layout=True),
        "flanders",
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.data_management_per_year == pytest.approx(18.92)


def test_taxes_split_correctly_per_region() -> None:
    w = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_w.pdf", layout=True),
        "wallonia",
    )
    v = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_v.pdf", layout=True),
        "flanders",
    )
    b = parse_snapshot(
        "totalenergies_mydynamic",
        fixture_text("totalenergies_dynamic_b.pdf", layout=True),
        "brussels",
    )
    # Federal excise (rounded to 2 decimals on TotalEnergies cards).
    assert w.taxes.federal_excise == pytest.approx(0.0503)
    assert v.taxes.federal_excise == pytest.approx(0.0503)
    assert b.taxes.federal_excise == pytest.approx(0.0503)
    # Wallonia: green energy + connection fee.
    assert w.taxes.wallonia_renewables == pytest.approx(0.032)
    assert w.taxes.region_connection_fee == pytest.approx(0.0007)
    # Flanders: green + cogen merged on one line.
    assert v.taxes.flanders_renewables == pytest.approx(0.0157)
    assert v.taxes.region_connection_fee == 0.0
    # Brussels: brussels_renewables only.
    assert b.taxes.brussels_renewables == pytest.approx(0.0285)
    assert b.taxes.flanders_renewables == 0.0
    assert b.taxes.wallonia_renewables == 0.0


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown TotalEnergies contract"):
            await EXTRACTORS["totalenergies"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_unknown_region_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown region"):
            await EXTRACTORS["totalenergies"].fetch(
                None,  # type: ignore[arg-type]
                "totalenergies_mydynamic",
                "atlantis",
            )

    asyncio.run(_run())
