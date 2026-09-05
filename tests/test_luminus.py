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

"""Luminus PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    SupplierSnapshot,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.luminus import parse_snapshot


def _dynamic_w() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_w.pdf"), "wallonia"
    )


def _dynamic_v() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_v.pdf"), "flanders"
    )


def _comfy_w() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w.pdf"), "wallonia"
    )


def _comfyflex_v() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_comfyflex", fixture_text("luminus_comfyflex_v.pdf"), "flanders"
    )


def test_luminus_is_registered() -> None:
    assert "luminus" in EXTRACTORS
    assert EXTRACTORS["luminus"].label == "Luminus"
    contract_ids = {c.id for c in EXTRACTORS["luminus"].contracts}
    assert "luminus_comfy" in contract_ids
    assert "luminus_comfyflex" in contract_ids
    assert "luminus_comfyflex_plus" in contract_ids
    assert "luminus_maxxfix" in contract_ids
    assert "luminus_maxxflex" in contract_ids
    assert "luminus_smartflex" in contract_ids
    assert "luminus_dynamic" in contract_ids


def test_dynamic_wallonia_extracts_consumption_formula() -> None:
    snap = _dynamic_w()
    assert isinstance(snap.energy, DynamicRates)
    # PDF prints "hors TVA  0,1019 x Belpex H + 2,4591" at 6% VAT.
    # Literal pinning (vs `0.1019 * 1.06 * 10`) so a unit-conversion
    # swap of 1.06 ⇄ 10 can't cancel out and pass the assertion.
    assert snap.energy.factor == pytest.approx(1.08014)
    assert snap.energy.base == pytest.approx(0.02606646)
    assert snap.energy.yearly_fixed_fee == pytest.approx(75.0)


def test_dynamic_bills_per_clock_hour() -> None:
    """The card indexes on "Belpex H", the hourly quotation, so the contract
    stays on the hourly ENTSO-E product. Unpinned until now."""
    snap = _dynamic_w()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is False


def test_dynamic_flanders_has_a_different_base() -> None:
    # Luminus's hourly formula has a region-specific base; Flanders is
    # 50 cents below Wallonia. This is the one fact that motivates the
    # whole region-aware fetcher signature - if we ever merged the two
    # regions into one snapshot, one of them would silently get the
    # wrong base.
    w = _dynamic_w()
    v = _dynamic_v()
    assert isinstance(w.energy, DynamicRates)
    assert isinstance(v.energy, DynamicRates)
    assert w.energy.factor == pytest.approx(v.energy.factor)
    assert w.energy.base != v.energy.base
    assert v.energy.base == pytest.approx(0.02076646)


def test_dynamic_extracts_injection_formula_with_negative_base() -> None:
    snap = _dynamic_w()
    inj = snap.injection
    assert inj is not None
    # PDF injection: hors TVA  0,1019 x Belpex H - 1,2737 (VAT-exempt).
    assert inj.factor == pytest.approx(1.019)
    assert inj.base == pytest.approx(-0.012737)


def test_missing_injection_row_fails_loud() -> None:
    # Every Luminus card publishes an injection rate; a non-dynamic card
    # whose injection row went missing is a layout drift, not a fee-free
    # contract, so it must raise rather than silently credit nothing.
    text = fixture_text("luminus_comfy_w.pdf").replace("injectée", "verwijderd")
    with pytest.raises(ExtractorError, match="injection"):
        parse_snapshot("luminus_comfy", text, "wallonia")


def test_comfy_wallonia_fixed_rates_and_dso() -> None:
    snap = _comfy_w()
    assert isinstance(snap.energy, FixedRates)
    # PDF:  20,38   23,74   17,71   17,71  (mono / pleines / creuses / excl_nuit).
    assert snap.energy.single == pytest.approx(0.2038)
    assert snap.energy.peak == pytest.approx(0.2374)
    assert snap.energy.offpeak == pytest.approx(0.1771)
    assert snap.energy.exclusive_night == pytest.approx(0.1771)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # The Redevance fixe row prints "65,00 65,00 -": the exclusive-night
    # circuit carries no separate abonnement, so its yearly fee is 0, not the
    # standard 65 (billed once on the main connection).
    from custom_components.be_electricity_prices.pricing import (
        yearly_fixed_fee_for_meter,
    )

    assert snap.energy.yearly_fixed_fee_exclusive_night == pytest.approx(0.0)
    assert yearly_fixed_fee_for_meter(snap.energy, "exclusive_night") == pytest.approx(
        0.0
    )
    # All five Wallonia DSOs and a sanity-check on the AIEG row.
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_comfyflex_flanders_uses_current_monthly_not_annual_estimate() -> None:
    # ComfyFlex prints two energy rows: 'Énergie fournie' (current month)
    # and 'Estimation annuelle de l'énergie fournie'. Take the first or
    # we'd over- / under-bill users by ~5% in a moving market.
    snap = _comfyflex_v()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1558)
    assert snap.energy.peak == pytest.approx(0.1684)
    assert snap.energy.offpeak == pytest.approx(0.1366)


def test_flanders_dynamic_dso_table_is_smaller_than_static() -> None:
    # Dynamic (SMR3) cards print 4 numbers per Fluvius row (digital
    # meter only). Static cards add 4 more (analog + prosumer). The
    # parser handles both.
    dyn = _dynamic_v()
    flex = _comfyflex_v()
    antwerpen_dyn = dyn.dsos["fluvius_antwerpen"]
    antwerpen_static = flex.dsos["fluvius_antwerpen"]
    # Distribution + capacity should agree to within 2 decimals
    # (dynamic is rounded, static prints 4-decimal).
    assert antwerpen_dyn.distribution_single == pytest.approx(0.0535)
    assert antwerpen_static.distribution_single == pytest.approx(0.0535)
    # Only the static card carries a prosumer rate.
    assert antwerpen_dyn.prosumer_eur_per_kva_year is None
    assert antwerpen_static.prosumer_eur_per_kva_year == pytest.approx(54.63)
    # The SMR3 dynamic regime is billed the reduced data-management fee
    # from the "(**) ... quart d'heure" footnote (18,56), not the table's
    # monthly-regime 18,92 that the static card bills.
    assert antwerpen_dyn.data_management_per_year == pytest.approx(18.56)
    assert antwerpen_static.data_management_per_year == pytest.approx(18.92)


def test_taxes_split_correctly_per_region() -> None:
    w = _dynamic_w()
    v = _dynamic_v()
    # Federal excise is uniform across regions.
    assert w.taxes.federal_excise == pytest.approx(0.050329)
    assert v.taxes.federal_excise == pytest.approx(0.050329)
    # Energy contribution is uniform too.
    assert w.taxes.energy_contribution == pytest.approx(0.002042)
    assert v.taxes.energy_contribution == pytest.approx(0.002042)
    # Wallonia: green energy 3,03 c€/kWh, no Flanders renewables.
    assert w.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert w.taxes.flanders_renewables == 0.0
    # Wallonia: connection fee 0,075 c€/kWh.
    assert w.taxes.region_connection_fee == pytest.approx(0.00075)
    # Flanders: green 1,17 + cogen 0,39 = 1,56 c€/kWh, no connection fee.
    assert v.taxes.flanders_renewables == pytest.approx(0.0156)
    assert v.taxes.wallonia_renewables == 0.0
    assert v.taxes.region_connection_fee == 0.0
    # Energy fund is BTR (résidentiel) which is '-' for residential users
    # in both regions today.
    assert w.taxes.energy_fund_eur_per_month == 0.0
    assert v.taxes.energy_fund_eur_per_month == 0.0


def test_comfyflex_plus_parses_as_variable() -> None:
    snap = parse_snapshot(
        "luminus_comfyflex_plus",
        fixture_text("luminus_comfyflex_plus_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, VariableRates)
    # Drop-in addition: same parser path as the existing ComfyFlex.
    # The energy row must produce four populated rates.
    assert snap.energy.current is not None
    assert snap.energy.peak is not None
    assert snap.energy.offpeak is not None
    assert snap.energy.exclusive_night is not None
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


def test_maxxflex_parses_as_variable() -> None:
    snap = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current is not None
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


def test_smartflex_parses_as_time_of_use() -> None:
    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    # SmartFlex's only sensible energy schema is TOU. The PDF prints
    #   "Énergie fournie  (c€/kWh) 15,54 13,29 6,72"
    # mapped to peak / transition / offpeak.
    assert isinstance(snap.energy, TimeOfUseRates)
    assert snap.energy.peak == pytest.approx(0.1554)
    assert snap.energy.transition == pytest.approx(0.1329)
    assert snap.energy.offpeak == pytest.approx(0.0672)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # SmartFlex bills seasonal windows, not the generic CWaPE schedule.
    assert snap.energy.weekend_rule == "smartflex_seasonal"
    # peak > transition > offpeak — correct slot ordering in EUR/kWh.
    assert snap.energy.peak > snap.energy.transition > snap.energy.offpeak
    # All five Wallonia DSOs present (no schema change for the network side).
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


def test_maxxflex_energy_carries_the_monthly_formula() -> None:
    """MaxxFlex indexes the COMMODITY on the delivery month too: "Le parametre
    d'indexation est base sur la moyenne arithmetique des cotations
    journalieres Day Ahead Belpex Baseload ... pendant le mois de livraison."
    The printed 14,41 is that formula at March's index on an April card.
    """
    from custom_components.be_electricity_prices.providers.base import VariableRates

    snap = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True
    assert energy.current == pytest.approx(0.1441)
    # c/kWh HTVA per EUR/MWh of index, printed TVAC, so x10 and x1,06 on both.
    assert energy.formula_factor == pytest.approx(0.1096 * 10.0 * 1.06)
    assert energy.formula_base == pytest.approx(3.4516 / 100.0 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(0.1290 * 10.0 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0940 * 10.0 * 1.06)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.0940 * 10.0 * 1.06)
    # Round-trips to the card's own printed rate at its own index.
    assert energy.formula_factor is not None and energy.formula_base is not None
    assert energy.formula_factor * 0.09261 + energy.formula_base == pytest.approx(
        0.1441, abs=1e-4
    )


def test_quarterly_and_tou_cards_get_no_energy_formula() -> None:
    """Three traps on one supplier, all resolved by gating on the
    arithmetic-mean sentence rather than the formula.

    ComfyFlex prints a TWO-term formula, "x Belpex + 0,0000 x Endex 1-0-3 +
    4,2102", against a QUARTERLY index; without the tail guard _NUM
    backtracks and binds "0,000" as the base. SmartFlex carries a
    MaxxFlex-identical block for a non-SMR3 meter but not the sentence. A
    fixed card has no energy formula at all, and searching the whole document
    rather than the energy block would hand it the INJECTION one.
    """
    from custom_components.be_electricity_prices.providers.base import VariableRates
    from custom_components.be_electricity_prices.providers.luminus import (
        _monthly_energy_coefficients,
    )

    for cid, fixture, region in (
        ("luminus_comfyflex", "luminus_comfyflex_v.pdf", "flanders"),
        ("luminus_comfyflex_plus", "luminus_comfyflex_plus_w.pdf", "wallonia"),
    ):
        energy = parse_snapshot(cid, fixture_text(fixture), region).energy
        assert isinstance(energy, VariableRates), cid
        assert energy.month_indexed is False, cid
        assert energy.formula_factor is None, cid

    assert _monthly_energy_coefficients(fixture_text("luminus_smartflex_w.pdf")) == {}
    assert _monthly_energy_coefficients(fixture_text("luminus_comfy_w.pdf")) == {}


def test_monthly_cards_carry_the_injection_formula() -> None:
    """The card says the printed figure is not the rate: "Votre tarif sera
    indexe tous les mois. La valeur Belpex du mois en cours n'est connue qu'a
    la fin du mois. Les prix affiches sont calcules sur la base de la derniere
    valeur Belpex connue (mois precedent)."
    """
    for cid, fixture, region in (
        ("luminus_comfy", "luminus_comfy_w.pdf", "wallonia"),
        ("luminus_maxxflex", "luminus_maxxflex_w.pdf", "wallonia"),
        ("luminus_smartflex", "luminus_smartflex_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture), region)
        inj = snap.injection
        assert inj is not None, cid
        assert inj.current == pytest.approx(0.0381), cid
        # "0,0481 x Belpex - 0,6392", c/kWh per EUR/MWh, and the block is
        # HTVA with the card noting "La TVA s'eleve a 0%".
        assert inj.factor == pytest.approx(0.481), cid
        assert inj.base == pytest.approx(-0.006392), cid
        assert inj.month_indexed is True, cid
        assert inj.factor is not None and inj.base is not None
        # Round-trips to the card's own printed figure at its own index.
        assert inj.factor * 0.09261 + inj.base == pytest.approx(0.0381, abs=1e-4), cid


def test_quarterly_cards_keep_their_printed_figure() -> None:
    """ComfyFlex and ComfyFlex+ print the IDENTICAL formula and index it
    QUARTERLY: "Votre tarif sera indexe tous les trimestres", against "la
    valeur de l'indice du 1re trimestre 2026". There is no quarterly mean to
    resolve that against, and resolving it monthly would be strictly worse
    than the lag: measured, the printed figure is 1,9% from the truth while
    April's month mean is 16,3% from it the other way.

    So the gate is the cadence sentence, never the formula.
    """
    for cid, fixture, region in (
        ("luminus_comfyflex", "luminus_comfyflex_v.pdf", "flanders"),
        ("luminus_comfyflex_plus", "luminus_comfyflex_plus_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture), region)
        inj = snap.injection
        assert inj is not None, cid
        assert inj.current == pytest.approx(0.0396), cid
        assert inj.factor is None, cid
        assert inj.month_indexed is False, cid


def test_injection_uses_applicable_rate_not_annual_estimate() -> None:
    # Cards print two injection rows: the applicable "Tarif de l'énergie
    # injectée" (3,81 / 3,96 c/kWh) and an "Estimation annuelle du tarif
    # de l'énergie injectée" 12-month forecast (4,38 / 4,44). Credit the
    # applicable rate, not the forecast.
    comfy = _comfy_w()
    assert comfy.injection is not None
    assert comfy.injection.current == pytest.approx(0.0381)
    comfyflex = _comfyflex_v()
    assert comfyflex.injection is not None
    assert comfyflex.injection.current == pytest.approx(0.0396)
    # May card: the annual estimate (3,68) is *below* the applicable rate
    # (3,81), so the old behaviour under-credited; confirm we still pick
    # the applicable row regardless of which is larger.
    may = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w_may.pdf"), "wallonia"
    )
    assert may.injection is not None
    assert may.injection.current == pytest.approx(0.0381)


def test_brussels_is_unsupported() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="not available in region"):
            await EXTRACTORS["luminus"].fetch(None, "luminus_comfy", "brussels")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_publication_label_tolerates_padded_parens() -> None:
    """The April-2026 cards print "(avril 2026)" but May-2026 cards print
    "(mai 2026 )" with a trailing space inside the parens. Both must
    surface a non-empty publication_label."""
    apr = _comfy_w()
    assert apr.publication_label == "avril 2026"
    may = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w_may.pdf"), "wallonia"
    )
    assert may.publication_label == "mai 2026"


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Luminus contract"):
            await EXTRACTORS["luminus"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_smartflex_carries_a_formula_per_slot() -> None:
    """SmartFlex prints one monthly formula per TOU band, "Prelevement Heures
    pleines = 0,1300 x Belpex + 2,6200; ... creuses = 0,1080 ...;
    ... super-creuses = 0,0410 ...", and attributes its index to a past month:
    "Belpex = 92,61 EUR/MWh (valeur de l'indice de mars 2026)" on an April
    card. So the printed triplet is March's.

    The gate is that month attribution, not a cadence sentence: unlike its
    bi-hourly siblings this card's energy block carries none, the sentence
    sits under the injection block and governs that tariff. ComfyFlex
    attributes its index to a QUARTER, which is what keeps it out.
    """
    from custom_components.be_electricity_prices.providers.base import TimeOfUseRates

    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    energy = snap.energy
    assert isinstance(energy, TimeOfUseRates)
    assert energy.month_indexed is True
    assert energy.formula_factor_peak == pytest.approx(0.1300 * 10 * 1.06)
    assert energy.formula_factor_transition == pytest.approx(0.1080 * 10 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0410 * 10 * 1.06)
    # The printed rates survive as the keyless fallback.
    assert energy.peak == pytest.approx(0.1554)


def test_a_smartflex_cohort_prices_each_slot_on_the_month() -> None:
    """The cohort leg becomes a three-band SpotMonthlyRates carrying the
    card's own weekend rule, so every existing month-mean gate keeps working
    without a second month-priced energy kind.
    """
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.cohort import (
        _cohort_energy_from_archived,
    )
    from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
        TimeOfUseRates,
    )

    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    leg = _cohort_energy_from_archived(snap)
    assert isinstance(leg, SpotMonthlyRates)
    assert leg.weekend_rule == "smartflex_seasonal"

    mean = 0.08096  # April 2026 arithmetic mean, EUR/kWh

    def at(hour: int) -> float:
        when = datetime(2026, 4, 15, hour, tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return energy_eur_per_kwh(leg, when, mean, meter="dynamic", region="wallonia")

    # Three distinct bands, each on its own formula, and each below the
    # printed rate because April's index sits under March's.
    assert at(8) == pytest.approx(0.1300 * 10 * 1.06 * mean + 2.6200 / 100 * 1.06)
    assert at(13) == pytest.approx(0.0410 * 10 * 1.06 * mean + 2.5400 / 100 * 1.06)
    assert at(8) > at(23) > at(13)
    printed = snap.energy
    assert isinstance(printed, TimeOfUseRates)
    assert at(8) < printed.peak
