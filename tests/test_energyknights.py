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

"""Fixture-based tests for the Energy Knights extractor."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    SpotMonthlyRates,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.energyknights import (
    DISCOVER_IDS,
    parse_snapshot,
)
from tests import fixture_text

_AGILIOR = "energyknights_agilior"
_AGILIS = "energyknights_agilis"
_ESSENTIA = "energyknights_essentia"

# 6% VAT, as the card header states it. Written out here so the expected
# values below read as the card's own arithmetic rather than as magic numbers.
_VAT = 1.06


def _agilior_text() -> str:
    return fixture_text("energyknights_agilior_aug.pdf", layout=True)


def _agilis_text() -> str:
    return fixture_text("energyknights_agilis_aug.pdf", layout=True)


def _essentia_text() -> str:
    return fixture_text("energyknights_essentia_aug.pdf", layout=True)


def _essentia_jan_text() -> str:
    """January 2026. Its mono coefficient is 1,04 against August's 1,03 and
    its peak 1,12 against 1,045, which is what stops any of them being
    pinned as constants."""
    return fixture_text("energyknights_essentia_jan.pdf", layout=True)


def _may_text() -> str:
    """The May 2026 Agilior card.

    Kept because it differs from August in every way that matters: a 15,00
    standing charge instead of 25,00, "x 1,07 + 7" instead of "x 1 + 12",
    an injection of "x 0,94 - 11", a tiered excise at 5,0329 instead of the
    flat 4,8760, and an energy contribution that had not yet been folded into
    the excise. Nothing on this card may be pinned as a constant.
    """
    return fixture_text("energyknights_agilior_may.pdf", layout=True)


def _agilior() -> SupplierSnapshot:
    return parse_snapshot(_AGILIOR, _agilior_text(), "test://ek-agilior-aug")


def _agilis() -> SupplierSnapshot:
    return parse_snapshot(_AGILIS, _agilis_text(), "test://ek-agilis-aug")


def _essentia() -> SupplierSnapshot:
    return parse_snapshot(_ESSENTIA, _essentia_text(), "test://ek-essentia-aug")


def _essentia_jan() -> SupplierSnapshot:
    return parse_snapshot(_ESSENTIA, _essentia_jan_text(), "test://ek-essentia-jan")


def _may() -> SupplierSnapshot:
    return parse_snapshot(_AGILIOR, _may_text(), "test://ek-agilior-may")


# ---- Agilior Online: the quarter-hourly card --------------------------------


def test_agilior_energy_is_quarter_hourly() -> None:
    energy = _agilior().energy
    assert isinstance(energy, DynamicRates)
    # "Belpex_15 * 1 + 12", quoted in EUR/MWh excluding VAT. The coefficient
    # is a dimensionless multiplier on a EUR/kWh spot, so it takes the VAT
    # gross-up and no rescale; the offset goes EUR/MWh to EUR/kWh.
    assert energy.factor == pytest.approx(1.0 * _VAT)
    assert energy.base == pytest.approx(12 / 1000 * _VAT)
    assert energy.yearly_fixed_fee == 25.00
    assert energy.quarter_hourly is True


def _printed_cents_from(
    factor: float | None, base: float | None, index_eur_per_mwh: float
) -> float:
    """Re-derive the card's printed c€/kWh from one parsed coefficient pair.

    Energy Knights rounds the ex-VAT cents to two decimals BEFORE applying
    the 6%, so the reconstruction rounds twice. A single multiply is one cent
    low on roughly a quarter of the cards, the May 2026 one included, and
    getting that wrong here would look like a VAT bug in the parser.
    """
    assert factor is not None and base is not None
    coefficient = factor / _VAT
    offset_eur_per_mwh = base / _VAT * 1000
    ex_vat_cents = round((coefficient * index_eur_per_mwh + offset_eur_per_mwh) / 10, 2)
    return round(ex_vat_cents * _VAT, 2)


def _printed_cents(energy: DynamicRates, index_eur_per_mwh: float) -> float:
    return _printed_cents_from(energy.factor, energy.base, index_eur_per_mwh)


def test_the_offtake_indicative_reconciles_with_its_own_formula() -> None:
    """Each card prints its rate beside the formula and names the index it
    used, so the VAT basis is checkable against the card itself.

    August 2026: "Belpex_15 * 1 + 12" at a VREG index of 127,50 EUR/MWh
    printed as 14,79 c€/kWh. May 2026: "x 1,07 + 7" at 100,71 printed as
    12,17, which is the month where the double rounding shows.
    """
    august = _agilior().energy
    may = _may().energy
    assert isinstance(august, DynamicRates)
    assert isinstance(may, DynamicRates)
    assert _printed_cents(august, 127.50) == 14.79
    assert _printed_cents(may, 100.71) == 12.17
    # What the same figures give without the intermediate rounding, kept so
    # the reason for the two-step form is visible rather than folklore.
    assert round((1.07 * 100.71 + 7) / 10 * _VAT, 2) == 12.16


def test_agilior_injection_is_spot_indexed_and_vat_exempt() -> None:
    injection = _agilior().injection
    assert injection is not None
    # "Belpex_15 * 1 - 12". Residential injection is VAT-exempt and the card
    # says so on this row with footnote (1), so neither coefficient is
    # grossed: 5,85 c€/kWh is (70,54 - 12) / 10 exactly, where grossing it
    # would print 6,21.
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-12 / 1000)
    assert (70.54 - 12) / 10 == pytest.approx(5.85, abs=0.005)
    assert injection.vat_applies is False
    # A dynamic card settles the credit per slot, so the printed figure is an
    # illustration rather than a rate to fall back on.
    assert injection.current is None
    assert injection.spp_indexed is False
    assert injection.month_indexed is False


def test_agilior_card_metadata() -> None:
    snap = _agilior()
    assert snap.supplier == "energyknights"
    assert snap.contract == _AGILIOR
    assert snap.publication_label == "2026-08"
    assert snap.valid_until == date(2026, 8, 31)


# ---- Agilis Online: the hourly twin -----------------------------------------


def test_agilis_is_the_same_card_on_the_hourly_grid() -> None:
    agilis = _agilis().energy
    agilior = _agilior().energy
    assert isinstance(agilis, DynamicRates)
    assert isinstance(agilior, DynamicRates)
    # Same coefficients, same standing charge, different index token. The
    # only thing separating the two products is the settlement grid, and it
    # is a single boolean, so it is pinned here explicitly rather than left
    # inside a bulk comparison.
    assert agilis.factor == pytest.approx(agilior.factor)
    assert agilis.base == pytest.approx(agilior.base)
    assert agilis.yearly_fixed_fee == agilior.yearly_fixed_fee
    assert agilis.quarter_hourly is False


def test_agilis_injection_matches_its_own_index() -> None:
    injection = _agilis().injection
    assert injection is not None
    assert injection.formula is not None
    assert "Belpex_h" in injection.formula
    assert injection.factor == pytest.approx(1.0)
    assert injection.base == pytest.approx(-12 / 1000)


# ---- Essentia Online: the monthly-indexed card ------------------------------


def test_essentia_energy_is_a_monthly_leg_with_every_register() -> None:
    energy = _essentia().energy
    assert isinstance(energy, SpotMonthlyRates)
    # "BelpexRLP * 1,03 + 7" mono, "* 1,045 + 8" day, "* 0,997 + 8" for both
    # night registers, quoted in EUR/MWh excluding VAT: the coefficient takes
    # the gross-up and no rescale, the offset goes EUR/MWh to EUR/kWh.
    assert energy.factor == pytest.approx(1.03 * _VAT)
    assert energy.base == pytest.approx(7 / 1000 * _VAT)
    assert energy.factor_peak == pytest.approx(1.045 * _VAT)
    assert energy.base_peak == pytest.approx(8 / 1000 * _VAT)
    assert energy.factor_offpeak == pytest.approx(0.997 * _VAT)
    assert energy.base_offpeak == pytest.approx(8 / 1000 * _VAT)
    assert energy.yearly_fixed_fee == 10.00
    # No quarter_hourly on this kind: the rate is flat for the whole month.
    assert not hasattr(energy, "quarter_hourly")


def test_essentia_night_circuit_gets_its_own_pair() -> None:
    """The dedicated exclusive-night pair is populated even though it happens
    to equal the off-peak one on every card so far.

    pricing routes it ahead of the bi-hourly band test, because that circuit
    is billed per meter rather than per hour of the day, and OCTA+ proves the
    two rows can diverge. Asserting the two are DIFFERENT would be asserting a
    coincidence, so this pins that the pair is present and correct instead.
    """
    energy = _essentia().energy
    assert isinstance(energy, SpotMonthlyRates)
    assert energy.factor_exclusive_night == pytest.approx(0.997 * _VAT)
    assert energy.base_exclusive_night == pytest.approx(8 / 1000 * _VAT)
    # The day band really is a different contract from the mono one, though.
    assert energy.factor_peak != pytest.approx(energy.factor)


def test_essentia_printed_rates_reconcile_with_their_own_formulas() -> None:
    """Each register's printed c-EUR/kWh re-derives from the coefficients we
    parsed and the VREG index the card names (2026-08 = 127,50 EUR/MWh).

    Nothing of the printed column is stored - it is an estimate off a
    different series than the contract settles on - so this is the only place
    the VAT basis of the monthly leg is checked against the card itself.
    """
    energy = _essentia().energy
    assert isinstance(energy, SpotMonthlyRates)
    idx = 127.50
    assert _printed_cents_from(energy.factor, energy.base, idx) == 14.66
    assert _printed_cents_from(energy.factor_peak, energy.base_peak, idx) == 14.97
    assert _printed_cents_from(energy.factor_offpeak, energy.base_offpeak, idx) == 14.32


def test_essentia_injection_is_spp_indexed_and_vat_exempt() -> None:
    injection = _essentia().injection
    assert injection is not None
    # "BelpexSPP * 0,98 - 10". The energy leg indexes on the load-weighted
    # Belpex-RLP-M and the credit on the solar-weighted Belpex-SPP-M; the flag
    # is what stops the coordinator resolving this formula against the energy
    # leg's mean, which is a different series entirely.
    assert injection.spp_indexed is True
    assert injection.factor == pytest.approx(0.98)
    assert injection.base == pytest.approx(-10 / 1000)
    assert injection.vat_applies is False
    # VAT-exempt, so the printed 5,91 only divides by 100 - and it reconciles
    # against the card's own solar index with no gross-up at all.
    assert injection.current == pytest.approx(0.0591)
    assert round((70.54 * 0.98 - 10) / 10, 2) == 5.91


def test_essentia_january_card_carries_its_own_coefficients() -> None:
    """Nothing on these cards may be pinned. The mono coefficient moved 1,04
    to 1,03 across 2026 and the peak one 1,12 to 1,045, so a bi-hourly entry
    priced off August's card in January runs about 6% out on its peak hours.
    """
    energy = _essentia_jan().energy
    assert isinstance(energy, SpotMonthlyRates)
    assert energy.factor == pytest.approx(1.04 * _VAT)
    assert energy.factor_peak == pytest.approx(1.12 * _VAT)
    assert energy.factor_offpeak == pytest.approx(0.997 * _VAT)
    injection = _essentia_jan().injection
    assert injection is not None
    assert injection.factor == pytest.approx(0.86)
    assert injection.base == pytest.approx(-5 / 1000)
    assert _essentia_jan().publication_label == "2026-01"
    assert _essentia_jan().valid_until == date(2026, 1, 31)


def test_essentia_standing_charge_is_not_the_coin_rebate() -> None:
    """Both rows print in EUR/jaar, and on the two dynamic cards both read
    25,00. Essentia's abonnement is 10,00 against the same 25,00 rebate, so
    this card is what proves the two regexes are not reading the same row.
    """
    assert _essentia().energy.yearly_fixed_fee == 10.00
    assert "Spaar korting met munten" in _essentia_text()


def test_essentia_injection_index_swap_raises() -> None:
    """The credit settles on BelpexSPP while the energy leg indexes on
    BelpexRLP. A card that printed the energy index on the solar row would
    resolve the credit against the wrong monthly mean, which is a silent
    mis-credit rather than a failure."""
    text = _essentia_text().replace("BelpexSPP * 0,98 - 10", "BelpexRLP * 0,98 - 10")
    with pytest.raises(ExtractorError, match="injection is indexed on 'BelpexRLP'"):
        parse_snapshot(_ESSENTIA, text, "test://ek-swapped")


def test_every_essentia_register_is_mandatory() -> None:
    """A monthly-indexed card bills all four registers, so a row that goes
    missing is a silent re-price rather than an unread column.

    Relabelling the dag row moves peak hours -2,05% and off-peak +2,39%, and
    every bound in the live check still passes because what remains is
    plausible. The dynamic products bill only the mono row, so they keep the
    old leniency and are covered separately below.
    """
    for card_row, wording in (
        ("Verbruik enkelvoudig", "enkelvoudig"),
        ("Verbruik dag", "dag"),
        ("Verbruik nacht", "nacht"),
        ("Verbruik exclusief nacht", "exclusief nacht"),
    ):
        text = _essentia_text().replace(card_row, "XXX")
        with pytest.raises(ExtractorError, match=f"{wording} consumption row"):
            parse_snapshot(_ESSENTIA, text, "test://ek-missing")


def test_a_dynamic_card_still_only_needs_its_mono_row() -> None:
    """DynamicRates carries one coefficient pair for every meter and these
    cards repeat the same formula in all four registers, so a card that
    stopped printing one of the others must not take a working contract
    offline over a row nothing reads."""
    for card_row in ("Verbruik dag", "Verbruik nacht", "Verbruik exclusief nacht"):
        text = _agilior_text().replace(card_row, "XXX")
        energy = parse_snapshot(_AGILIOR, text, "test://ek-partial").energy
        assert isinstance(energy, DynamicRates)
        assert energy.factor == pytest.approx(1.0 * _VAT)


# ---- the May card: nothing may be pinned as a constant ----------------------


def test_may_card_carries_its_own_coefficients() -> None:
    energy = _may().energy
    assert isinstance(energy, DynamicRates)
    assert energy.yearly_fixed_fee == 15.00
    assert energy.factor == pytest.approx(1.07 * _VAT)
    assert energy.base == pytest.approx(7 / 1000 * _VAT)
    injection = _may().injection
    assert injection is not None
    assert injection.factor == pytest.approx(0.94)
    assert injection.base == pytest.approx(-11 / 1000)


def test_may_card_predates_the_flat_excise() -> None:
    """The excise went flat on 2026-08-01 when the federal contribution was
    folded into it. Before that the card printed three bands and a separate
    energiebijdrage, and both have to read correctly or an archived month
    would bill on August's numbers."""
    taxes = _may().taxes
    assert taxes.federal_excise == pytest.approx(0.050329)
    assert taxes.energy_contribution == pytest.approx(0.002042)
    assert _agilior().taxes.federal_excise == pytest.approx(0.048760)
    assert _agilior().taxes.energy_contribution == 0.0


def test_may_card_period() -> None:
    snap = _may()
    assert snap.publication_label == "2026-05"
    assert snap.valid_until == date(2026, 5, 31)


# ---- network tariffs --------------------------------------------------------


def test_all_eight_fluvius_areas_parse() -> None:
    assert set(_agilior().dsos) == set(FLUVIUS_KEYS)


def test_antwerpen_row_reads_the_digital_meter_columns() -> None:
    """The card prints the same eight areas twice, digital then classic.

    Reading the classic block instead would charge 8,09 c€/kWh of
    distribution against 5,35 and 130,92 EUR/kW/year of capacity against
    52,37, which on a typical Flemish entry is a few hundred euros a year.
    """
    dso = _agilior().dsos["fluvius_antwerpen"]
    assert dso.distribution_single == pytest.approx(0.0535)
    assert dso.distribution_exclusive_night == pytest.approx(0.0481)
    assert dso.data_management_per_year == pytest.approx(18.92)
    assert dso.capacity_eur_per_kw_year == pytest.approx(52.37)
    # The card folds transmission into the distribution figure: "tarieven
    # verbonden aan het gebruik van het distributie- en transmissienet".
    assert dso.transport == 0.0
    # Nothing on the classic-meter row leaked in.
    assert dso.distribution_single != pytest.approx(0.0809)
    assert dso.capacity_eur_per_kw_year != pytest.approx(130.92)


def test_every_product_reads_the_same_network_tariffs() -> None:
    # The net-tariff and tax blocks are regulated pass-through, identical on
    # every product's card, so a per-product drift would mean a parse bug.
    assert _agilior().dsos == _agilis().dsos == _essentia().dsos
    assert _agilior().taxes == _agilis().taxes == _essentia().taxes


# ---- taxes ------------------------------------------------------------------


def test_tax_block_reads_each_column_of_the_interleave() -> None:
    """The tax block is two columns side by side and "Standaard tarief" heads
    a row in both: once under the energy fund in EUR/month, once under the
    energiebijdrage in c€/kWh. Matching the wrong one is a hundredfold unit
    error, and in August 2026 both happen to be zero, so only a card where
    they differ can prove the anchors hold. That is the May card.
    """
    august = _agilior().taxes
    assert august.federal_excise == pytest.approx(0.048760)
    assert august.flanders_renewables == pytest.approx((1.16 + 0.36) / 100)
    assert august.energy_fund_eur_per_month == 0.0
    assert august.energy_contribution == 0.0
    # Every value on the card is already VAT-inclusive.
    assert august.vat_rate == 0.0
    may = _may().taxes
    assert may.energy_fund_eur_per_month == 0.0
    assert may.energy_contribution == pytest.approx(0.002042)


def test_energy_fund_takes_the_domiciled_row() -> None:
    # The card offers "Standaard tarief" 0,00 and "Niet-gedomiciliëerd" 10,07
    # in EUR/month. This integration prices a domiciled residential entry,
    # the same choice every sibling extractor makes, so 10,07 must not leak in.
    assert _agilior().taxes.energy_fund_eur_per_month == 0.0
    assert "10,07" in _agilior_text()


# ---- the wrong card served --------------------------------------------------


def test_optima_card_does_not_parse_as_agilior() -> None:
    """Optima Online printed a byte-identical energy block in August 2026:
    the same "Belpex_15 * 1 + 12", the same "- 12" injection and the same
    25,00 standing charge. No check on the formula or the figures can tell
    them apart, so the guard reads the card's own intro line instead.
    """
    text = fixture_text("energyknights_optima_aug.pdf", layout=True)
    with pytest.raises(ExtractorError, match="card is for 'Optima Online'"):
        parse_snapshot(_AGILIOR, text, "test://ek-optima")


def test_a_sibling_product_card_does_not_parse_as_agilior() -> None:
    with pytest.raises(ExtractorError, match="card is for 'Agilis Online'"):
        parse_snapshot(_AGILIOR, _agilis_text(), "test://ek-wrong")
    with pytest.raises(ExtractorError, match="card is for 'Essentia Online'"):
        parse_snapshot(_AGILIOR, _essentia_text(), "test://ek-wrong")


def test_a_card_that_names_no_product_raises() -> None:
    text = _agilior_text().replace("van Energy Knights", "van Someone Else")
    with pytest.raises(ExtractorError, match="does not name its product"):
        parse_snapshot(_AGILIOR, text, "test://ek-anonymous")


def test_an_index_swap_raises_rather_than_repricing() -> None:
    """A card that kept its product name but changed its index token would
    otherwise be resolved against the wrong axis: monthly coefficients
    applied to a per-slot spot, or the reverse."""
    text = _agilior_text().replace("Belpex_15 * 1 + 12", "BelpexRLP * 1 + 12")
    with pytest.raises(
        ExtractorError, match="enkelvoudig row is indexed on 'BelpexRLP'"
    ):
        parse_snapshot(_AGILIOR, text, "test://ek-swapped")


def test_unknown_contract_raises() -> None:
    with pytest.raises(ExtractorError, match="unknown Energy Knights contract"):
        parse_snapshot("energyknights_nope", _agilior_text(), "test://ek")


# ---- mandatory rows fail loud ------------------------------------------------


def test_a_missing_standing_charge_raises() -> None:
    text = _agilior_text().replace("Abonnement (€/jaar)", "XXX")
    with pytest.raises(ExtractorError, match="abonnement row not found"):
        parse_snapshot(_AGILIOR, text, "test://ek")


def test_a_missing_injection_row_raises() -> None:
    # Silently crediting a solar user nothing is worse than being offline.
    text = _agilior_text().replace('optie "solar"', "XXX")
    with pytest.raises(ExtractorError, match="injection row not found"):
        parse_snapshot(_AGILIOR, text, "test://ek")


def test_a_missing_consumption_row_raises() -> None:
    text = _agilior_text().replace("Verbruik enkelvoudig", "XXX")
    with pytest.raises(ExtractorError, match="enkelvoudig consumption row"):
        parse_snapshot(_AGILIOR, text, "test://ek")


def test_a_row_cannot_take_another_row_formula() -> None:
    """The indicative and the formula must stay on one line.

    A renderer that emitted them as separate blocks would otherwise let a row
    bind to whatever formula came next. The index-token guard cannot see it,
    since every row on these cards names the same index, so the only defence
    is that the pattern refuses to cross a newline.
    """
    text = _agilior_text().replace(
        "Verbruik enkelvoudig (c€/kWh) 14,79 Belpex_15 * 1 + 12",
        "Verbruik enkelvoudig (c€/kWh) 14,79\nBelpex_15 * 1 + 12",
    )
    with pytest.raises(ExtractorError, match="enkelvoudig consumption row"):
        parse_snapshot(_AGILIOR, text, "test://ek-split")


def test_the_standing_charge_is_not_the_coin_rebate() -> None:
    """Both rows print in EUR/jaar, and on the August card both read 25,00.

    Only the May card can tell the two regexes apart: its abonnement is 15,00
    against the same 25,00 rebate. The rebate is an average potential discount
    for shopping through the supplier's platform, settled on the ex-VAT
    amount, so it is not a recurring charge and is deliberately not modelled.
    """
    assert _may().energy.yearly_fixed_fee == 15.00
    assert "Spaar korting met munten" in _may_text()
    assert "25,00" in _may_text()


def test_a_dso_row_cannot_inherit_the_next_row_figures() -> None:
    """A row that loses its figures must fail, not borrow its neighbour's.

    Dropping Midden-Vlaanderen's numbers used to hand it Fluvius West's,
    which is 27% more distribution and 14% more capacity, reported silently.
    Nothing downstream bounds a distribution rate, so CI would stay green.
    """
    text = _agilior_text().replace(
        "Fluvius (Midden-Vlaanderen) 5,28 4,78 18,92 18,92 53,13",
        "Fluvius (Midden-Vlaanderen)",
    )
    with pytest.raises(ExtractorError, match="fluvius_intergem"):
        parse_snapshot(_AGILIOR, text, "test://ek-wrapped")


def test_the_classic_meter_table_cannot_leak_in() -> None:
    """The digital / classic cut is case-insensitive and line-anchored.

    A capitalised classic header used to leave the whole classic table inside
    the slice, and the digital anchor used to bind to the solar footnote
    ("Heb je zonnepanelen en een digitale meter?") about 900 characters above
    the table. Together they billed Antwerpen's classic row: 8,09 c€/kWh of
    distribution against 5,35 and 130,92 EUR/kW/year against 52,37.
    """
    text = _agilior_text().replace("klassieke meter", "Klassieke meter")
    dso = parse_snapshot(_AGILIOR, text, "test://ek-case").dsos["fluvius_antwerpen"]
    assert dso.distribution_single == pytest.approx(0.0535)
    assert dso.capacity_eur_per_kw_year == pytest.approx(52.37)
    # pdfplumber happens to wrap the solar footnote on this card, which is the
    # only reason a plain substring search finds the table at all. Unwrap it to
    # reproduce the January 2026 layout, where the footnote sits 921 characters
    # above the table and would otherwise become the anchor.
    unwrapped = _agilior_text().replace("digitale\nmeter?", "digitale meter?")
    assert "digitale meter?" in unwrapped
    dso = parse_snapshot(_AGILIOR, unwrapped, "test://ek-unwrapped").dsos[
        "fluvius_antwerpen"
    ]
    assert dso.distribution_single == pytest.approx(0.0535)
    assert dso.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_a_missing_dso_row_raises() -> None:
    # A partial network table would leave that area's users with no
    # distribution charge at all, which prices lower than the truth.
    text = _agilior_text().replace("Fluvius (Kempen)", "Fluvius (Elders)")
    with pytest.raises(ExtractorError, match="fluvius_iveka"):
        parse_snapshot(_AGILIOR, text, "test://ek")


def test_a_missing_dso_table_raises() -> None:
    text = _agilior_text().replace("digitale meter", "XXX")
    with pytest.raises(ExtractorError, match="could not locate the DSO table"):
        parse_snapshot(_AGILIOR, text, "test://ek")


# ---- registration -----------------------------------------------------------


def test_energyknights_is_registered() -> None:
    assert "energyknights" in EXTRACTORS
    assert EXTRACTORS["energyknights"].label == "Energy Knights"
    contract_ids = {c.id for c in EXTRACTORS["energyknights"].contracts}
    assert {_AGILIOR, _AGILIS, _ESSENTIA} < contract_ids


def test_contract_kinds_and_regions() -> None:
    contracts = {c.id: c for c in EXTRACTORS["energyknights"].contracts}
    assert contracts[_AGILIOR].kind == "dynamic"
    assert contracts[_AGILIS].kind == "dynamic"
    # "spot_monthly", not "variable": the kind is what makes the ENTSO-E key
    # mandatory, and this contract cannot be priced without one. Its printed
    # rate comes off the VREG weighted average rather than the Belpex-RLP-M it
    # settles on, and the two sit at least 10% apart in 19 of the 26 months
    # Energy Knights publishes.
    assert contracts[_ESSENTIA].kind == "spot_monthly"
    for contract in contracts.values():
        assert contract.regions == frozenset({"flanders"})
        assert contract.professional is False


def test_no_contract_asks_for_a_second_key() -> None:
    # spot_indexed_injection exists to offer an OPTIONAL key to a contract
    # whose kind does not collect one. Every kind here is spot-priced, so the
    # key is already mandatory and setting the flag would add a redundant
    # second key step to the flow.
    from custom_components.be_electricity_prices.const import (
        SPOT_PRICED_CONTRACT_KINDS,
    )

    for contract in EXTRACTORS["energyknights"].contracts:
        assert contract.kind in SPOT_PRICED_CONTRACT_KINDS
        assert contract.spot_indexed_injection is False


def test_discover_ids_cover_the_published_catalogue() -> None:
    # Every slug the listing publishes, including the four "green" twins and
    # Optima Online, so live_check flags only a genuinely new product.
    assert len(DISCOVER_IDS) == 8
    # Every slug we sell has to be catalogued, or the nightly check reports
    # our own products as new ones. The module asserts this too.
    from custom_components.be_electricity_prices.providers.energyknights import (
        _CONTRACTS,
    )

    assert {c.slug for c in _CONTRACTS} < DISCOVER_IDS
    # Optima and its twin are published but out of scope; they stay listed so
    # discover() does not report them as new.
    assert {"optimaonline", "optimaonlinegreen"} < DISCOVER_IDS


# ---- the archive -------------------------------------------------------------


def _legacy(fixture: str, contract: str) -> SupplierSnapshot:
    """Parse an archived card the way fetch_for_month does: through the
    product names that month was published under."""
    from custom_components.be_electricity_prices.providers.energyknights import (
        _CONTRACTS_BY_ID,
    )

    products = _CONTRACTS_BY_ID[contract].legacy_products
    return parse_snapshot(
        contract, fixture_text(fixture, layout=True), "test://", products
    )


def test_the_legacy_slug_and_names_switch_at_2026_01() -> None:
    """Energy Knights renamed all four products at the turn of 2026, so an
    archived month resolves under a different slug AND a different name than
    the current card. The handover is clean: no month resolves under both.
    """
    from custom_components.be_electricity_prices.providers.energyknights import (
        _CONTRACTS_BY_ID,
    )

    agilior = _CONTRACTS_BY_ID[_AGILIOR]
    assert agilior.slug_for(date(2025, 12, 1)) == "dynamic15"
    assert agilior.slug_for(date(2026, 1, 1)) == "agilioronline"
    assert agilior.products_for(date(2026, 1, 1)) == ("Agilior Online",)
    # 2025-09 spells it with a space and every later month without one.
    assert agilior.products_for(date(2025, 12, 1)) == (
        "Elektriciteit Dynamisch 15",
        "Elektriciteit Dynamisch15",
    )
    assert _CONTRACTS_BY_ID[_AGILIS].slug_for(date(2025, 12, 1)) == "dynamic"
    assert _CONTRACTS_BY_ID[_ESSENTIA].slug_for(date(2025, 12, 1)) == "variable"


def test_an_archived_card_parses_under_its_old_name() -> None:
    agilior = _legacy("energyknights_agilior_sep25.pdf", _AGILIOR)
    assert agilior.publication_label == "2025-09"
    assert agilior.valid_until == date(2025, 9, 30)
    assert isinstance(agilior.energy, DynamicRates)
    assert agilior.energy.quarter_hourly is True
    assert agilior.energy.yearly_fixed_fee == 15.00
    assert set(agilior.dsos) == set(FLUVIUS_KEYS)

    essentia = _legacy("energyknights_essentia_dec25.pdf", _ESSENTIA)
    assert essentia.publication_label == "2025-12"
    dec = essentia.energy
    aug = _essentia().energy
    assert isinstance(dec, SpotMonthlyRates)
    assert isinstance(aug, SpotMonthlyRates)
    # December still printed all four registers, with its own coefficients.
    assert dec.factor_peak is not None
    assert dec.factor != pytest.approx(aug.factor)


def test_the_two_dynamic_products_do_not_share_an_archived_card() -> None:
    """ "Elektriciteit Dynamisch" is a PREFIX of "Elektriciteit Dynamisch15".

    A prefix match would hand every archived Agilis month the quarter-hourly
    card, and the two products price the same formula against different grids,
    so the swap is a wrong bill rather than a failure.
    """
    agilis_card = fixture_text("energyknights_agilis_dec25.pdf", layout=True)
    agilior_card = fixture_text("energyknights_agilior_sep25.pdf", layout=True)
    from custom_components.be_electricity_prices.providers.energyknights import (
        _CONTRACTS_BY_ID,
    )

    agilis_names = _CONTRACTS_BY_ID[_AGILIS].legacy_products
    agilior_names = _CONTRACTS_BY_ID[_AGILIOR].legacy_products
    # Each parses under its own names.
    hourly = parse_snapshot(_AGILIS, agilis_card, "t", agilis_names).energy
    quarterly = parse_snapshot(_AGILIOR, agilior_card, "t", agilior_names).energy
    assert isinstance(hourly, DynamicRates) and hourly.quarter_hourly is False
    assert isinstance(quarterly, DynamicRates) and quarterly.quarter_hourly is True
    # And rejects the other's card.
    # This fixture is the September card, the one month spelled with a space.
    with pytest.raises(
        ExtractorError, match="card is for 'Elektriciteit Dynamisch 15'"
    ):
        parse_snapshot(_AGILIS, agilior_card, "t", agilis_names)
    with pytest.raises(ExtractorError, match="card is for 'Elektriciteit Dynamisch'"):
        parse_snapshot(_AGILIOR, agilis_card, "t", agilior_names)


def test_the_archive_horizon_refuses_2024() -> None:
    """The 2024 cards print the PRE-MERGER ten Fluvius areas and omit
    Halle-Vilvoorde and Zenne-Dijle, and 2024-06 prints EUR/kWh values under a
    c-EUR/kWh header. Their energy block parses perfectly well, so the refusal
    has to be explicit rather than left to the DSO table failing.
    """
    from custom_components.be_electricity_prices.providers.energyknights import (
        _ARCHIVE_HORIZON,
        _CONTRACTS_BY_ID,
    )

    assert _ARCHIVE_HORIZON == date(2025, 1, 1)
    assert _CONTRACTS_BY_ID[_AGILIS].archive_from == _ARCHIVE_HORIZON
    assert _CONTRACTS_BY_ID[_ESSENTIA].archive_from == _ARCHIVE_HORIZON
    # Agilior's predecessor did not exist before September 2025.
    assert _CONTRACTS_BY_ID[_AGILIOR].archive_from == date(2025, 9, 1)


def test_fetch_for_month_is_registered() -> None:
    assert EXTRACTORS["energyknights"].fetch_for_month is not None


# ---- the green twins ---------------------------------------------------------

_AGILIOR_GREEN = "energyknights_agilior_green"
_ESSENTIA_GREEN = "energyknights_essentia_green"


def test_a_green_card_is_its_base_card_plus_a_flat_adder() -> None:
    """The only pricing difference is one "Groene stroom" row, and it applies
    to every kWh whatever the register.

    Adding it to the mono offset alone would leave a bi-hourly or
    night-circuit customer paying for green power they are not charged for on
    those registers.
    """
    green = parse_snapshot(
        _AGILIOR_GREEN,
        fixture_text("energyknights_agilior_green_aug.pdf", layout=True),
        "test://",
    ).energy
    base = _agilior().energy
    assert isinstance(green, DynamicRates) and isinstance(base, DynamicRates)
    # 0,32 c€/kWh, already VAT-inclusive: the row carries no formula and no
    # footnote, so it sits on the same basis as the printed rate columns.
    assert green.factor == pytest.approx(base.factor)
    assert green.base == pytest.approx(base.base + 0.0032)
    assert green.yearly_fixed_fee == base.yearly_fixed_fee
    assert green.quarter_hourly is base.quarter_hourly


def test_the_green_adder_reaches_every_register() -> None:
    green = parse_snapshot(
        _ESSENTIA_GREEN,
        fixture_text("energyknights_essentia_green_aug.pdf", layout=True),
        "test://",
    ).energy
    base = _essentia().energy
    assert isinstance(green, SpotMonthlyRates) and isinstance(base, SpotMonthlyRates)
    for reg in ("base", "base_peak", "base_offpeak", "base_exclusive_night"):
        assert getattr(green, reg) == pytest.approx(getattr(base, reg) + 0.0032), reg
    for reg in ("factor", "factor_peak", "factor_offpeak", "factor_exclusive_night"):
        assert getattr(green, reg) == pytest.approx(getattr(base, reg)), reg


def test_the_green_premium_is_parsed_not_pinned() -> None:
    """It moves. September 2025 printed 0,42 c€/kWh against 0,32 everywhere
    else, so a hardcoded adder would have under-billed that month by a third.
    """
    sep = parse_snapshot(
        _AGILIOR_GREEN,
        fixture_text("energyknights_agilior_green_sep25.pdf", layout=True),
        "test://",
        ("Elektriciteit Dynamisch 15 Groen", "Elektriciteit Dynamisch15 Groen"),
    ).energy
    aug = parse_snapshot(
        _AGILIOR_GREEN,
        fixture_text("energyknights_agilior_green_aug.pdf", layout=True),
        "test://",
    ).energy
    assert isinstance(sep, DynamicRates) and isinstance(aug, DynamicRates)
    # The September card's own base card carries 0,00742, so the adder is the
    # difference: 0,42 c€/kWh there against 0,32 in August.
    assert sep.base == pytest.approx(0.00742 + 0.0042)
    plain_aug = _agilior().energy
    assert isinstance(plain_aug, DynamicRates)
    assert aug.base - plain_aug.base == pytest.approx(0.0032)


def test_a_green_card_that_stops_printing_the_row_raises() -> None:
    """The row is what the customer pays the premium for; losing it would
    silently bill them the grey rate."""
    text = fixture_text("energyknights_agilior_green_aug.pdf", layout=True).replace(
        "Groene stroom", "XXX"
    )
    with pytest.raises(ExtractorError, match="groene stroom row not found"):
        parse_snapshot(_AGILIOR_GREEN, text, "test://")


def test_a_green_card_does_not_parse_as_its_base_product() -> None:
    green_text = fixture_text("energyknights_agilior_green_aug.pdf", layout=True)
    with pytest.raises(ExtractorError, match="card is for 'Agilior Online Green'"):
        parse_snapshot(_AGILIOR, green_text, "test://")
    with pytest.raises(ExtractorError, match="card is for 'Agilior Online'"):
        parse_snapshot(_AGILIOR_GREEN, _agilior_text(), "test://")


def test_all_six_products_are_registered() -> None:
    contracts = {c.id: c for c in EXTRACTORS["energyknights"].contracts}
    assert len(contracts) == 6
    assert contracts[_AGILIOR_GREEN].kind == "dynamic"
    assert contracts[_ESSENTIA_GREEN].kind == "spot_monthly"
    for contract in contracts.values():
        assert contract.regions == frozenset({"flanders"})
        assert contract.spot_indexed_injection is False
    # Optima has no twin here because Optima itself is out of scope.
    assert "energyknights_optima_green" not in contracts
