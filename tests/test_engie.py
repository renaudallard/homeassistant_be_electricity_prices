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

"""Engie PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.be_electricity_prices.const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS
from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.engie import parse_snapshot


def _dynamic_three_regions() -> dict[str, str]:
    return {
        REGION_FLANDERS: fixture_text("engie_dynamic_v.pdf"),
        REGION_WALLONIA: fixture_text("engie_dynamic_w.pdf"),
        REGION_BRUSSELS: fixture_text("engie_dynamic_b.pdf"),
    }


def test_engie_is_registered() -> None:
    assert "engie" in EXTRACTORS
    assert EXTRACTORS["engie"].label == "Engie"
    contract_ids = {c.id for c in EXTRACTORS["engie"].contracts}
    assert "engie_dynamic" in contract_ids
    assert "engie_empower_variable" in contract_ids
    assert "engie_empower_flextime" in contract_ids
    assert "engie_easy_fixed" in contract_ids
    assert "engie_easy_variable" in contract_ids


def test_empower_flextime_extracts_tou_triplet() -> None:
    # Empower Flextime shares the Empower Variable PDF; the Consommation
    # row carries the bi-horaire pair AND the Flextime triplet at indices
    # 4/5/6 (peak / transition / offpeak in c€/kWh).
    snap = parse_snapshot(
        "engie_empower_flextime",
        {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
    )
    assert isinstance(snap.energy, TimeOfUseRates)
    # Pinned literals from April 2026 card; they re-index monthly so
    # this fixture is a frozen snapshot, not a forever-fact.
    assert snap.energy.peak == pytest.approx(0.16738)
    assert snap.energy.transition == pytest.approx(0.13072)
    assert snap.energy.offpeak == pytest.approx(0.09796)
    # Engie weekend rule differs from SmartFlex's: weekend keeps the
    # transition/offpeak split rather than collapsing to all-offpeak.
    assert snap.energy.weekend_rule == "weekend_no_peak"
    # Empower's yearly subscription fee lives on the "Prix mensuels" row
    # (not the standard "Type d'usage" anchor); the parser's fallback
    # regex captures it as 90,00 EUR.
    assert snap.energy.yearly_fixed_fee == pytest.approx(90.0)


def test_empower_flextime_injection_varies_by_slot() -> None:
    # Issue #34: the Flextime feed-in tariff has three per-slot rates
    # (Injection(3) columns 4/5/6), not the single non-flextime rate. The
    # extractor must surface the triplet so injection tracks the slot the
    # way consumption does.
    snap = parse_snapshot(
        "engie_empower_flextime",
        {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
    )
    inj = snap.injection
    assert inj is not None
    assert inj.peak == pytest.approx(0.08417)
    assert inj.transition == pytest.approx(0.04834)
    assert inj.offpeak == pytest.approx(0.01465)


def test_empower_variable_injection_is_single_band_but_month_indexed() -> None:
    """The non-Flextime Empower variants credit ONE band, not a per-slot
    triplet, and index that band on the delivery month's EPEXDAM.

    This test used to assert only the two things that stayed true while the
    whole month-indexing was missing - peak is None, current is 4,918 - so it
    sat green through the defect it exists to catch. The coefficients are what
    make it a real assertion.
    """
    snap = parse_snapshot(
        "engie_empower_variable",
        {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
    )
    inj = snap.injection
    assert inj is not None
    assert inj.peak is None
    assert inj.current == pytest.approx(0.04918)
    # "- Normal = 0,0300 + (0,0528 x EPEXDAM)", c/kWh per EUR/MWh, and NOT
    # grossed: the formula reproduces the printed figure with no 1,06, while
    # the energy leg's needs one.
    assert inj.factor == pytest.approx(0.0528 * 10.0)
    assert inj.base == pytest.approx(0.0300 / 100.0)
    assert inj.month_indexed is True
    assert inj.factor * 0.09257 + inj.base == pytest.approx(0.04918, abs=1e-5)


def test_flextime_injection_keeps_its_triplet_and_no_formula() -> None:
    """Flextime's card carries the same EPEXDAM sentence but prints THREE
    injection coefficient pairs, one per slot, and InjectionRates holds one.
    Storing the Normal pair would be a fourth, wrong formula: inert on the
    live sensor because the per-slot rates win, but enough to make the
    coordinator fetch spots for a number nothing reads."""
    snap = parse_snapshot(
        "engie_empower_flextime",
        {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
    )
    inj = snap.injection
    assert inj is not None
    assert inj.peak == pytest.approx(0.08417)
    assert inj.factor is None
    assert inj.base is None
    assert inj.month_indexed is False


def test_endex_injection_keeps_its_printed_rate() -> None:
    """Easy Variable prints a structurally identical injection formula on
    ENDEX101, and it must not be swept in: that index is a futures average
    over the month BEFORE delivery and the card states its value outright, so
    the printed figure is the billed rate with no lag to correct. ENTSO-E
    cannot produce it either."""
    snap = parse_snapshot(
        "engie_easy_variable",
        {REGION_FLANDERS: fixture_text("engie_easy_indexed_v.pdf")},
    )
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.04446)
    assert inj.factor is None
    assert inj.month_indexed is False


def test_empower_flextime_dsos_match_wallonia_set() -> None:
    snap = parse_snapshot(
        "engie_empower_flextime",
        {REGION_WALLONIA: fixture_text("engie_empower_flextime_w.pdf")},
    )
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= set(snap.dsos)


def test_dynamic_extracts_consumption_formula() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    assert isinstance(snap.energy, DynamicRates)
    # PDF prints "hors TVA  0,8702 + (0,1039 x eSpot_15)" at 6% VAT.
    # Pinned literal so a unit-conversion bug that swaps 1.06 ⇄ 10
    # (visually identical: 0.1039 * 10.6 == 0.1039 * 1.06 * 10) can't
    # cancel out and pass the assertion.
    assert snap.energy.factor == pytest.approx(1.10134)
    assert snap.energy.base == pytest.approx(0.00922412)
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.7)


def test_wallonia_ores_subarea_divergence_is_fatal() -> None:
    # The ~7 ORES sub-areas are numerically identical today and collapse
    # to one ORES key. If a future card diverges a sub-area, the parser
    # must raise rather than silently bill every ORES customer the first
    # sub-area's rates.
    wal = fixture_text("engie_dynamic_w.pdf").replace(
        "ORES (Est) 11,98", "ORES (Est) 99,98"
    )
    with pytest.raises(ExtractorError, match="ORES sub-area"):
        parse_snapshot("engie_dynamic", {REGION_WALLONIA: wal})


def test_dynamic_missing_vat_phrase_is_fatal() -> None:
    # The Dynamic formula is printed pre-VAT and scaled by the parsed VAT
    # multiplier; a reworded VAT header must raise rather than silently
    # fall back to 6%.
    texts = {
        region: text.replace("de tva comprise", "XXX")
        for region, text in _dynamic_three_regions().items()
    }
    with pytest.raises(ExtractorError, match="VAT multiplier"):
        parse_snapshot("engie_dynamic", texts)


def test_dynamic_extracts_injection_formula() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    inj = snap.injection
    assert inj is not None
    # PDF injection: hors TVA  -1,3135 + (0,1000 x eSpot_15)
    # Residential injection is VAT-exempt so factor stays at 0.1000 * 10.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.013135)
    # Indicative monthly rate also surfaced (from the Injection(3) row).
    assert inj.current == pytest.approx(0.09136)


def test_dynamic_merges_dsos_from_every_region() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    keys = set(snap.dsos)
    # 8 Fluvius sub-areas + 5 Wallonia + 1 Brussels = 14.
    assert {"fluvius_antwerpen", "fluvius_west", "fluvius_zenne_dijle"} <= keys
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= keys
    assert "sibelga" in keys


def test_dynamic_flanders_dso_includes_transport_in_distribution() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    antwerpen = snap.dsos["fluvius_antwerpen"]
    # Engie's V table prints distribution rates that already include the
    # Elia transport - "incluant déjà les coûts de transport". So the
    # parser sets transport=0 and rolls everything into distribution_single.
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535329)
    # "tarif-kWh exclusif nuit" column, lower than the single rate.
    excl = antwerpen.distribution_exclusive_night
    assert excl == pytest.approx(0.0481301)
    assert excl is not None and excl < antwerpen.distribution_single
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.3679)


def test_dynamic_wallonia_dso_has_separate_transport_no_prosumer() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    # Dynamic SMR3 contracts have no compensation regime - the prosumer
    # column is replaced by IMPACT (PIC/MEDIUM/ECO) on the Wallonia card.
    assert aieg.prosumer_eur_per_kva_year is None


def test_dynamic_brussels_extracts_sibelga() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_peak == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    # Metering fee 14.73 + Sibelga <=13kVA power term 50.07 (both billed to
    # a residential Brussels connection; no separate capacity charge).
    assert sibelga.data_management_per_year == pytest.approx(14.73 + 50.07)


def test_dynamic_extracts_taxes_for_every_region() -> None:
    snap = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    # Federal: same value across regions, so any one PDF is canonical.
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    # Regional renewables: each pulled from its own region's PDF.
    assert snap.taxes.flanders_renewables == pytest.approx(0.01582)
    assert snap.taxes.wallonia_renewables == pytest.approx(0.03095)
    assert snap.taxes.brussels_renewables == pytest.approx(0.02652)
    # Wallonia connection fee + Flanders energy fund (with-domicile = 0).
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.energy_fund_eur_per_month == 0.0
    # Engie's PDF prints 6% VAT inclusive, so the snapshot is post-VAT.
    assert snap.taxes.vat_rate == 0.0


def test_easy_fixed_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "engie_easy_fixed",
        {REGION_FLANDERS: fixture_text("engie_easy_fixed_v.pdf")},
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 18,938  20,197  17,176  17,176  (mono / day / night / excl_night).
    assert snap.energy.single == pytest.approx(0.18938)
    assert snap.energy.peak == pytest.approx(0.20197)
    assert snap.energy.offpeak == pytest.approx(0.17176)
    assert snap.energy.exclusive_night == pytest.approx(0.17176)
    assert snap.energy.yearly_fixed_fee == pytest.approx(69.0)
    # Fixed contracts have an indicative monthly injection price but no
    # formula.
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.03217)
    assert snap.injection.factor is None and snap.injection.base is None


def test_empower_variable_skips_flextime_tiers() -> None:
    # Empower Variable's Consommation row has 7 price columns: standard
    # mono / bi-pleines / bi-creuses, then three Flextime variants
    # (heures pleines / creuses / super-creuses), then exclusive-night.
    # The integration's pricing model only carries mono + bi + excl_night,
    # so the Flextime middle three are skipped on purpose.
    snap = parse_snapshot(
        "engie_empower_variable",
        {REGION_FLANDERS: fixture_text("engie_empower_variable_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.13775)
    assert snap.energy.peak == pytest.approx(0.15058)
    assert snap.energy.offpeak == pytest.approx(0.11625)
    # Last price column on the 8-number row is exclusive-night, NOT the
    # Flextime super-creuses (9,796) which is the cheapest visible value.
    assert snap.energy.exclusive_night == pytest.approx(0.12460)


def test_empower_energy_carries_the_epexdam_formula() -> None:
    """The card indexes consumption on the DELIVERY month's EPEXDAM and says
    the printed price is only informative: "La valeur du EPEXDAM du mois en
    cours ne sera connue qu'en fin de mois. A titre informatif, les prix
    indiques sont bases sur la derniere valeur du EPEXDAM connue (Mars 2026:
    92,57 EUR/MWh)." So the printed 13,775 is March's rate on an April card.
    """
    from custom_components.be_electricity_prices.providers.base import VariableRates

    snap = parse_snapshot(
        "engie_empower_variable",
        {REGION_FLANDERS: fixture_text("engie_empower_variable_v.pdf")},
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True
    assert energy.current == pytest.approx(0.13775)
    # "Normal = 2,1552 + (0,1171 x EPEXDAM)", c/kWh per EUR/MWh and printed
    # hors TVA, so a x10 onto a EUR/kWh spot, a /100 base, both x 1,06.
    assert energy.formula_factor == pytest.approx(0.1171 * 10.0 * 1.06)
    assert energy.formula_base == pytest.approx(2.1552 / 100.0 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(0.1264 * 10.0 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0988 * 10.0 * 1.06)
    # "Exclusif nuit = 2,4510 + (0,1005 x EPEXDAM)": its own formula, between
    # the mono and the off-peak, not either of them.
    assert energy.formula_factor_exclusive_night == pytest.approx(0.1005 * 10.0 * 1.06)
    # And the Flextime rows on the same card are NOT swept in: "Flextime
    # Heures pleines = 2,9422 + (0,1388 x EPEXDAM)" contains "heures
    # pleines", so a substring match binds it to the bi-hourly meter ~10%
    # high. Labels are matched exactly.
    assert energy.formula_factor_peak != pytest.approx(0.1388 * 10.0 * 1.06)
    # The coefficients reproduce the card's own printed price at its own index.
    assert energy.formula_factor * 0.09257 + energy.formula_base == pytest.approx(
        0.13775, abs=1e-5
    )


def test_empty_house_energy_carries_its_bare_epexdam_formula() -> None:
    """Empty House states one formula with no band label at all,
    "3,2150 + (0,2150 x EPEXDAM)", so the parser cannot key on the row
    heading the Empower card uses."""
    from custom_components.be_electricity_prices.providers.base import VariableRates

    snap = parse_snapshot(
        "engie_empty_house",
        {REGION_FLANDERS: fixture_text("engie_empty_house_v.pdf")},
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True
    assert energy.formula_factor == pytest.approx(0.2150 * 10.0 * 1.06)
    assert energy.formula_base == pytest.approx(3.2150 / 100.0 * 1.06)
    # Mono-only card: no band pair to bind.
    assert energy.formula_factor_peak is None


def test_endex_indexed_card_keeps_its_printed_rate() -> None:
    """Easy Variable indexes on ENDEX101, which is published in ADVANCE, so
    its printed rate is the contract and must not be swept into the EPEXDAM
    fix. Its card names no EPEXDAM at all, which is what keeps it out."""
    from custom_components.be_electricity_prices.providers.base import VariableRates

    snap = parse_snapshot(
        "engie_easy_variable",
        {REGION_FLANDERS: fixture_text("engie_easy_indexed_v.pdf")},
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is False
    assert energy.formula_factor is None


def test_epexdam_bands_are_not_lifted_from_the_other_leg() -> None:
    """Both legs print an EPEXDAM block on the same card and the two-column
    PDF interleaves them. Binding by document order paired the energy leg's
    Normal row with the INJECTION block's Heures pleines, which is a factor
    of two out. Each block is grouped from its own Normal row instead."""
    from custom_components.be_electricity_prices.providers.engie import (
        _epexdam_formulas,
    )

    text = fixture_text("engie_empower_variable_v.pdf")
    energy = _epexdam_formulas(text, 0.13775, 1.06)
    injection = _epexdam_formulas(text, 0.04918, 1.0)
    # Each leg's bands belong to its own block.
    assert energy["peak"][0] == pytest.approx(0.1264 * 10.0 * 1.06)
    assert injection["peak"][0] == pytest.approx(0.0528 * 10.0)
    # And a price no block reproduces binds nothing at all.
    assert _epexdam_formulas(text, 0.999, 1.06) == {}


def test_empty_house_is_mono_only() -> None:
    # The 'Tarif bâtiment vide' card has a single rate (no bihoraire, no
    # exclusive-night) because vacant homes don't run time-of-use loads.
    # The parser must accept the 1-price-+-1-renewables row layout
    # instead of the standard 4-prices-+-1-renewables.
    snap = parse_snapshot(
        "engie_empty_house",
        {REGION_FLANDERS: fixture_text("engie_empty_house_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.24505)
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    assert snap.taxes.flanders_renewables == pytest.approx(0.01582)
    # A vacant home has no registered domicile, so it bills the card's
    # 'sans domicile' energy fund (10,07/mo), not the 'avec domicile' 0.
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(10.07)


def test_energy_fund_selects_domicile_case() -> None:
    # Default (normal contracts) reads 'avec domicile' (0 on this card);
    # sans_domicile=True (Empty House) reads the 'sans domicile' 10,07.
    from custom_components.be_electricity_prices.providers.engie import (
        _extract_energy_fund,
    )

    text = fixture_text("engie_empty_house_v.pdf")
    assert _extract_energy_fund(text) == pytest.approx(0.0)
    assert _extract_energy_fund(text, sans_domicile=True) == pytest.approx(10.07)


def test_easy_variable_uses_monthly_not_annual_estimate() -> None:
    snap = parse_snapshot(
        "engie_easy_variable",
        {REGION_FLANDERS: fixture_text("engie_easy_indexed_v.pdf")},
    )
    assert isinstance(snap.energy, VariableRates)
    # The Variable PDF prints two Consommation rows: 'Prix mensuels' (the
    # rate Engie is actually charging this month) and 'Prix annuels
    # estimés'. The integration must take the first - the second would
    # over-bill users by ~7% in a falling-price month.
    assert snap.energy.current == pytest.approx(0.16072)
    assert snap.energy.peak == pytest.approx(0.16992)
    assert snap.energy.offpeak == pytest.approx(0.14335)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Engie contract"):
            await EXTRACTORS["engie"].fetch(None, "bogus", "flanders")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_parse_snapshot_with_partial_regions_still_works() -> None:
    # If Engie's API is down for one region, the coordinator should still
    # build a snapshot from the others. parse_snapshot accepts whatever
    # the caller provides, so a single-region map yields a working
    # snapshot with only that region's DSOs.
    snap = parse_snapshot(
        "engie_dynamic",
        {REGION_BRUSSELS: fixture_text("engie_dynamic_b.pdf")},
    )
    assert set(snap.dsos) == {"sibelga"}
    assert snap.taxes.brussels_renewables == pytest.approx(0.02652)
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.wallonia_renewables == 0.0


def test_august_2026_flat_excise_replaces_the_tier_table() -> None:
    """On 2026-08-01 the federal scheme folded the separate energy
    contribution into the special excise and flattened it, so Engie's card
    dropped the four-tier consumption table for a single "Toutes
    consommations" row and deleted the cotisation line. Parsing must follow
    both shapes: the flat rate when present, the 0-3.000 kWh tier otherwise.
    """
    from custom_components.be_electricity_prices.providers.engie import (
        _extract_energy_contribution,
        _extract_federal_excise,
    )

    august = "Accise fédérale(11) (c€/kWh)\nToutes consommations 4,87600\n"
    rate, bands = _extract_federal_excise(august)
    assert rate == pytest.approx(0.048760)
    assert bands is None
    # Row deleted because the levy is abolished, not because of drift.
    assert _extract_energy_contribution(august) == 0.0

    # The pre-August tiered card keeps working, contribution and all.
    july = (
        "Consommation entre 0 et 3.000 kWh 5,0329\nCotisation sur l'énergie 0,20417\n"
    )
    rate, bands = _extract_federal_excise(july)
    assert rate == pytest.approx(0.050329)
    assert bands is None
    assert _extract_energy_contribution(july) == pytest.approx(0.0020417)


# ---- professional cards ------------------------------------------------------


def _pro_easy_three_regions() -> dict[str, str]:
    return {
        REGION_FLANDERS: fixture_text("engie_pro_easy_indexed_v.pdf"),
        REGION_WALLONIA: fixture_text("engie_pro_easy_indexed_w.pdf"),
        REGION_BRUSSELS: fixture_text("engie_pro_easy_indexed_b.pdf"),
    }


def test_pro_contracts_are_registered() -> None:
    contract_ids = {c.id for c in EXTRACTORS["engie"].contracts}
    assert "engie_pro_easy_variable" in contract_ids
    assert "engie_pro_dynamic" in contract_ids
    assert "engie_pro_empower_flextime" in contract_ids
    # Direct Online and Basic Online are residential-only.
    assert "engie_pro_direct_online" not in contract_ids
    assert "engie_pro_basic_online" not in contract_ids


def test_pro_document_url_switches_segment() -> None:
    from custom_components.be_electricity_prices.providers.engie import (
        _CONTRACTS_BY_ID,
        _document_url,
    )

    res = _document_url(_CONTRACTS_BY_ID["engie_easy_variable"], "V")
    pro = _document_url(_CONTRACTS_BY_ID["engie_pro_easy_variable"], "V")
    assert "document=E_EASY_R_GREEN_C_I_12_V_F" in res
    assert "segment=R" in res
    assert "document=E_EASY_P_GREEN_C_I_12_V_F" in pro
    assert "segment=P" in pro
    # Brussels runs 12 months on the pro card, not the residential 36.
    assert "E_EASY_P_GREEN_C_I_12_B_F" in _document_url(
        _CONTRACTS_BY_ID["engie_pro_easy_variable"], "B"
    )


def test_pro_card_is_parsed_ex_vat_with_the_excise_schedule() -> None:
    snap = parse_snapshot("engie_pro_easy_variable", _pro_easy_three_regions())
    # The card prints "Prix tva exclue"; the snapshot says so rather than
    # grossing anything up here.
    assert snap.taxes.vat_rate == pytest.approx(0.21)
    # The degressive schedule the residential cards lost in August 2026.
    assert snap.taxes.federal_excise_bands == (
        (20_000.0, pytest.approx(0.01421)),
        (50_000.0, pytest.approx(0.01209)),
        (1_000_000.0, pytest.approx(0.01139)),
    )
    # The energy contribution is professional-only again: residential has
    # it folded into the excise.
    assert snap.taxes.energy_contribution == pytest.approx(0.0019261)
    # "Professionnel (basse tension)", not "Résidentiel (avec domicile)".
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(10.07)
    assert set(snap.dsos) >= {"fluvius_antwerpen", "ores", "sibelga"}


def test_pro_regulated_values_are_the_residential_ones_ex_vat() -> None:
    """The professional and residential cards carry the same regulated
    tables; the professional one prints them excluding the 6% VAT the
    residential one includes. Grossing the professional numbers by 1.06
    must reproduce the residential card exactly."""
    pro = parse_snapshot("engie_pro_easy_variable", _pro_easy_three_regions())
    res = parse_snapshot(
        "engie_easy_variable",
        {REGION_FLANDERS: fixture_text("engie_easy_indexed_v.pdf")},
    )
    pro_fl = pro.dsos["fluvius_antwerpen"]
    res_fl = res.dsos["fluvius_antwerpen"]
    assert pro_fl.capacity_eur_per_kw_year is not None
    assert res_fl.capacity_eur_per_kw_year is not None
    assert pro_fl.capacity_eur_per_kw_year * 1.06 == pytest.approx(
        res_fl.capacity_eur_per_kw_year, rel=1e-4
    )
    assert pro_fl.distribution_single * 1.06 == pytest.approx(
        res_fl.distribution_single, rel=1e-4
    )
    assert pro_fl.data_management_per_year * 1.06 == pytest.approx(
        res_fl.data_management_per_year, rel=1e-3
    )


def test_pro_injection_is_taxed() -> None:
    snap = parse_snapshot(
        "engie_pro_dynamic", {REGION_FLANDERS: fixture_text("engie_pro_dynamic_v.pdf")}
    )
    assert snap.injection is not None
    assert snap.injection.vat_applies is True
    # ... and the residential one is not.
    res = parse_snapshot("engie_dynamic", _dynamic_three_regions())
    assert res.injection is not None
    assert res.injection.vat_applies is False


def test_pro_dynamic_formula_is_not_grossed_at_parse_time() -> None:
    """The residential parser bakes the 6% into the dynamic factor because
    the card is otherwise VAT-inclusive. The professional card is ex-VAT
    throughout, so the formula stays as printed and vat_rate carries the
    21% for apply_vat to resolve."""
    from custom_components.be_electricity_prices.providers.engie import (
        _FORMULA_RE,
    )

    text = fixture_text("engie_pro_dynamic_v.pdf")
    snap = parse_snapshot("engie_pro_dynamic", {REGION_FLANDERS: text})
    assert isinstance(snap.energy, DynamicRates)
    printed = _FORMULA_RE.search(text)
    assert printed is not None
    factor_printed = float(printed.group(4).replace(",", "."))
    assert snap.energy.factor == pytest.approx(factor_printed * 10.0)


def test_pro_empower_still_yields_the_flextime_triplet() -> None:
    snap = parse_snapshot(
        "engie_pro_empower_flextime",
        {REGION_FLANDERS: fixture_text("engie_pro_empower_variable_v.pdf")},
    )
    assert isinstance(snap.energy, TimeOfUseRates)
    assert snap.energy.peak > snap.energy.transition > snap.energy.offpeak


def test_pro_card_without_the_ex_vat_header_is_refused() -> None:
    """A professional card that started printing VAT-inclusive numbers
    would silently under-price by 21%. Fail loudly instead."""
    from custom_components.be_electricity_prices.providers.engie import (
        _vat_multiplier,
    )

    with pytest.raises(ExtractorError, match="tva exclue"):
        _vat_multiplier("Prix, 6% de tva comprise", professional=True)
    assert _vat_multiplier("Prix tva exclue", professional=True) == 1.0


def test_pro_excise_tier_bounds_are_whole_kwh() -> None:
    """The tier bound's dot is a thousands separator, not a decimal point.
    Reading 20.000 as twenty would band every site into the top tranche."""
    from custom_components.be_electricity_prices.providers.engie import (
        _extract_federal_excise,
    )

    text = (
        "Accise fédérale(11) (c€/kWh)\n"
        "Consommation entre 0 et 20.000 kWh 1,421\n"
        "Consommation entre 20.000 et 50.000 kWh 1,209\n"
        "Consommation entre 50.000 et 1.000.000 kWh 1,139\n"
    )
    rate, bands = _extract_federal_excise(text, professional=True)
    assert bands is not None
    assert [b[0] for b in bands] == [20_000.0, 50_000.0, 1_000_000.0]
    assert rate == pytest.approx(0.01421)


def test_pro_contract_is_flagged_in_the_registry() -> None:
    """The config flow gates its professional step on this flag, so it has
    to distinguish the two editions."""
    contracts = {c.id: c for c in EXTRACTORS["engie"].contracts}
    assert contracts["engie_pro_easy_variable"].professional is True
    assert contracts["engie_easy_variable"].professional is False
