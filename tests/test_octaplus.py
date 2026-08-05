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

"""OCTA+ PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text_aligned,
)
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.octaplus import (
    _extract_flanders_renewables,
    _extract_taxes,
    _extract_wallonia_renewables,
    parse_snapshot,
)
from tests import FIXTURES


def _text(name: str) -> str:
    return extract_pdf_text_aligned(
        (FIXTURES / name).read_bytes(), x_join_threshold=1.0
    )


def test_octaplus_is_registered() -> None:
    assert "octaplus" in EXTRACTORS
    assert EXTRACTORS["octaplus"].label == "OCTA+"
    contract_ids = {c.id for c in EXTRACTORS["octaplus"].contracts}
    assert "octaplus_fixed" in contract_ids
    assert "octaplus_dynamic" in contract_ids
    assert "octaplus_fixed_impact" in contract_ids
    assert len(contract_ids) == 8
    # Impact comptage is a Walloon CWaPE concept; the Flanders Fixed card
    # carries no Impact block, so the variant must not be offered there.
    impact = next(
        c for c in EXTRACTORS["octaplus"].contracts if c.id == "octaplus_fixed_impact"
    )
    assert impact.regions == frozenset({"wallonia"})


def test_fixed_wallonia_extracts_meter_rates() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1586)
    assert snap.energy.peak == pytest.approx(0.1867)
    assert snap.energy.offpeak == pytest.approx(0.1377)
    assert snap.energy.exclusive_night == pytest.approx(0.1485)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # Injection is the second number on the 'Compteur monohoraire' line; a
    # column-index regression that grabbed the consumption rate instead
    # would over-credit feed-in ~3.4x. Pin it as a flat current rate (no
    # spot factor on a fixed card).
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0472)
    assert snap.injection.factor is None
    assert snap.injection.base is None


def test_fixed_impact_extracts_three_cwape_bands() -> None:
    # Impact comptage prices the three CWaPE bands (Eco / Medium / Pic) as
    # the supplier energy, pairing with the DSO's Impact distribution bands.
    from custom_components.be_electricity_prices.providers.base import ImpactRates

    snap = parse_snapshot(
        "octaplus_fixed_impact", _text("octaplus_fixed_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, ImpactRates)
    assert snap.energy.eco == pytest.approx(0.1284)
    assert snap.energy.medium == pytest.approx(0.1683)
    assert snap.energy.pic == pytest.approx(0.1972)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # Injection is the same flat feed-in credit as the standard Fixed card.
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0472)
    # The Walloon DSO carries the matching Impact distribution bands.
    ores = snap.dsos["ores"]
    assert ores.distribution_pic is not None
    assert ores.distribution_eco is not None


def test_missing_regional_renewables_raises() -> None:
    # The regional green-energy surcharge is mandatory for its region; a
    # miss must raise rather than silently zero ~1.6-3.1 c€/kWh.
    with pytest.raises(ExtractorError, match="Wallonia green-energy"):
        _extract_wallonia_renewables("no green energy row")
    with pytest.raises(ExtractorError, match="Flanders green-energy"):
        _extract_flanders_renewables("no green energy row")


def test_missing_federal_tax_tier_raises() -> None:
    # The federal excise + energy contribution are mandatory; a layout
    # drift on the tier row must fail loud, not silently zero them.
    text = "Consommation entre 0 a 3 000 kWh 5,0329 0,2042\n"
    with pytest.raises(ExtractorError, match="federal tax tier"):
        _extract_taxes(text, "wallonia")


def test_missing_bihourly_rates_raises() -> None:
    # OCTA+ always prints the bi-hourly table; a missing Heures pleines /
    # Heures creuses row is a drift that must fail loud rather than
    # silently billing a bi-hourly user the single (mono) rate.
    text = _text("octaplus_fixed_w.pdf").replace("Heures pleines", "XXX")
    with pytest.raises(ExtractorError, match="bi-hourly rates"):
        parse_snapshot("octaplus_fixed", text, "wallonia")


def test_fixed_flanders_extracts_meter_rates() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1589)


def test_smart_variable_returns_variable_rates() -> None:
    snap = parse_snapshot(
        "octaplus_smartvariable",
        _text("octaplus_smartvariable_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1516)


def test_dynamic_parses_smr3_formula() -> None:
    # OCTA+ Dynamic prints the consumption formula as prose:
    # "Epex 15' * 1,083 + 4,17". The factor and base must be VAT-adjusted
    # (6% residential) and the base converted from EUR/MWh to EUR/kWh.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    # Literal pinning: a unit-conversion bug (e.g. dropping the *1000
    # EUR/MWh→EUR/kWh divide and the 1.06 VAT) could still pass an
    # `approx(1.083 * 1.06)` style assertion. Keep the derivation in
    # the comment, the expected number in the assertion.
    assert snap.energy.factor == pytest.approx(1.14798)
    assert snap.energy.base == pytest.approx(0.0044202)
    # Epex 15' indexation bills per quarter-hour, like Engie / Cociter /
    # EBEM / Ecofix; the price table must keep the native 15-minute grid.
    assert snap.energy.quarter_hourly is True


def test_dynamic_extracts_injection_formula() -> None:
    # The injection formula sits later in the prose, anchored on
    # "Le prix de votre injection ... Epex 15' * 1 - 13,89 €/MWh".
    # Injection is VAT-exempt so the factor / base are not VAT-adjusted.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)
    assert snap.injection.base == pytest.approx(-0.01389)


def test_dynamic_consumption_formula_skips_injection_on_reorder() -> None:
    """Consumption and injection share the 'Epex 15' shape. Even if a
    future card prints the injection paragraph first, the consumption
    picker must not bind the injection formula as the consumption rate."""
    from custom_components.be_electricity_prices.providers.octaplus import (
        _dynamic_consumption_formula,
    )

    text = (
        "Le prix de votre injection est indexe sur Epex 15' * 1 - 13,89\n"
        "La formule tarifaire HTVA est la suivante: Epex 15' * 1,083 + 4,17\n"
    )
    match = _dynamic_consumption_formula(text)
    assert match is not None
    assert match.group(1) == "1,083"  # consumption factor, not injection "1"
    assert match.group(3) == "4,17"


def test_supplier_pv_forfait_extracted_and_absent_on_dynamic() -> None:
    # Fixed/variable cards print "+ 4,77 EUR/kVA par mois" (the Forfait
    # panneaux solaires for the compensation regime); 4,77 * 12 = 57,24
    # EUR/kVA/an, TVAC, billed on top of the DSO prosumer column. The SMR3
    # dynamic product drops the compensation regime and omits it.
    for cid, fixture, region in (
        ("octaplus_fixed", "octaplus_fixed_w.pdf", "wallonia"),
        ("octaplus_fixed", "octaplus_fixed_v.pdf", "flanders"),
        ("octaplus_smartvariable", "octaplus_smartvariable_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(cid, _text(fixture), region)
        assert snap.supplier_prosumer_eur_per_kva_year == pytest.approx(57.24)
    dyn = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert dyn.supplier_prosumer_eur_per_kva_year is None


def test_federal_taxes_use_first_tier() -> None:
    # OCTA+ tax page renders each character as its own pdfplumber word
    # ("5 ,0 3 2 9 0 ,2 0 4 2"); the aligned helper's gap-aware merge
    # must reassemble the values before we read tier 1 (0-3000 kWh).
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.energy_contribution == pytest.approx(0.002042)


def test_taxes_split_correctly_per_region() -> None:
    wa = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    fl = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
    # Wallonia: green-energy + connection fee.
    assert wa.taxes.wallonia_renewables == pytest.approx(0.03095)
    assert wa.taxes.region_connection_fee == pytest.approx(0.00075)
    assert wa.taxes.flanders_renewables == 0.0
    # Flanders: green-energy + WKK, no connection fee.
    assert fl.taxes.flanders_renewables == pytest.approx((1.166 + 0.430) / 100.0)
    assert fl.taxes.region_connection_fee == 0.0


def test_wallonia_dsos_extract_full_set() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_w.pdf"), "wallonia")
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= set(snap.dsos)
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0667)
    assert aieg.transport == pytest.approx(0.0275)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.04)


def test_wallonia_dsos_new_2026_template() -> None:
    # The 2026 template recased/renamed the row labels (AIEG -> Aieg,
    # TECTEO - RESA -> RESA, REGIEDEWAVRE -> Régie de Wavre) and swapped
    # the last two columns (now terme_fixe | transport | prosumer). The
    # parser must still surface every DSO and keep the prosumer forfait
    # (~80 EUR/kVA/an) apart from the transport rate (~2-3 c€/kWh).
    from custom_components.be_electricity_prices.providers.octaplus import (
        _extract_wallonia_dsos,
    )

    text = (
        "Aieg 10,87 12,05 6,67 15,07 9,82 4,56 6,67 19,49 2,75 81,04\n"
        "Aiesh 13,64 15,17 8,22 19,07 12,29 5,50 8,22 17,92 2,75 99,29\n"
        "ORES (Brab. wallon) 11,98 13,27 7,39 16,58 10,83 5,09 7,39 14,10 2,75 85,84\n"
        "Régie de Wavre 12,48 13,78 7,83 17,12 11,31 5,51 7,83 26,44 2,75 93,00\n"
        "RESA 11,07 12,20 7,02 15,12 10,05 4,99 7,02 26,50 2,75 84,22\n"
    )
    dsos = _extract_wallonia_dsos(text)
    assert set(dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = dsos["aieg"]
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.04)
    assert aieg.transport == pytest.approx(0.0275)
    assert aieg.data_management_per_year == pytest.approx(19.49)


def test_publication_month_reads_fiche_tarifaire_banner() -> None:
    # The pre-2026 title line still parses, and the 2026 redesign's
    # "FICHE TARIFAIRE <MOIS> <YYYY>" banner is the fallback, including the
    # accented month names.
    from custom_components.be_electricity_prices.providers.octaplus import (
        _extract_publication_month,
    )

    assert (
        _extract_publication_month(
            "Clients résidentiels en Wallonie - 04/2026 - Tarifs 6% TVAC"
        )
        == "04/2026"
    )
    assert _extract_publication_month("FICHE TARIFAIRE JUILLET 2026") == "07/2026"
    assert _extract_publication_month("FICHE TARIFAIRE FÉVRIER 2026") == "02/2026"
    assert _extract_publication_month("FICHE TARIFAIRE AOÛT 2026") == "08/2026"


def test_dynamic_injection_survives_reworded_lead_in() -> None:
    # The 2026 dynamic card reworded the injection lead-in from "Le prix
    # de votre injection ..." to "les prix de l'électricité injectée sont
    # indexés ..."; the formula anchor must still bind so the feed-in
    # credit is not zeroed.
    reworded = _text("octaplus_dynamic_w.pdf").replace(
        "Le prix de votre injection",
        "les prix de l'électricité injectée sont indexés",
    )
    assert "Le prix de votre injection" not in reworded
    snap = parse_snapshot("octaplus_dynamic", reworded, "wallonia")
    assert snap.injection is not None
    assert snap.injection.factor is not None
    assert snap.injection.base is not None


def test_dynamic_pdf_uses_spaced_dso_label() -> None:
    # The Dynamic card renders "REGIE DE WAVRE" with regular spaces
    # (vs. the Fixed card's "REGIEDEWAVRE"); the label regex tolerates
    # both, so REW is still picked up here.
    snap = parse_snapshot(
        "octaplus_dynamic", _text("octaplus_dynamic_w.pdf"), "wallonia"
    )
    assert "rew" in snap.dsos


def test_flanders_dsos_extract_full_set() -> None:
    snap = parse_snapshot("octaplus_fixed", _text("octaplus_fixed_v.pdf"), "flanders")
    expected = set(FLUVIUS_KEYS)
    assert expected <= set(snap.dsos)
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert antwerpen.prosumer_eur_per_kva_year == pytest.approx(54.63)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown OCTA"):
            await EXTRACTORS["octaplus"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_brussels_region_rejected() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="not available in region"):
            await EXTRACTORS["octaplus"].fetch(None, "octaplus_fixed", "brussels")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_missing_wallonia_connection_fee_fails_loud() -> None:
    """A mandatory per-kWh Walloon levy zeroed on a label drift under-bills
    every Walloon entry silently. Every sibling extractor raises here; OCTA+
    returned 0 and said nothing."""
    text = _text("octaplus_fixed_w.pdf").replace(
        "Redevance raccordement Wallonie", "XXX"
    )
    with pytest.raises(ExtractorError, match="connection fee"):
        parse_snapshot("octaplus_fixed", text, "wallonia")
