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

"""Fixture-based tests for the EnergyVision extractor."""

from __future__ import annotations

from datetime import date

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.energyvision import (
    DISCOVER_IDS,
    parse_snapshot,
)
from tests import fixture_text

_DYNAMIC = "energyvision_dynamic"
_FIXED = "energyvision_fixed_3y"
_FIXED_WAL = "energyvision_fixed_1y"


def _dyn_text() -> str:
    return fixture_text("energyvision_dynamic_jul.pdf", layout=True)


def _fixed_text() -> str:
    return fixture_text("energyvision_fixed_3y_jul.pdf", layout=True)


def _dyn() -> SupplierSnapshot:
    return parse_snapshot(_DYNAMIC, _dyn_text(), "test://ev-dynamic-jul")


def _fixed() -> SupplierSnapshot:
    return parse_snapshot(_FIXED, _fixed_text(), "test://ev-fixed-jul")


def _wal_text() -> str:
    return fixture_text("energyvision_fixed_1y_wal_jul.pdf", layout=True)


def _wal() -> SupplierSnapshot:
    return parse_snapshot(_FIXED_WAL, _wal_text(), "test://ev-wal-jul")


def _wal_aug_text() -> str:
    """The August 2026 card: EnergyVision deleted the whole supplements
    sub-block (energy contribution + connection fee) and replaced the
    four-tier excise table with one flat "Accise speciale" row."""
    return fixture_text("energyvision_fixed_1y_wal_aug.pdf", layout=True)


def _wal_aug() -> SupplierSnapshot:
    return parse_snapshot(_FIXED_WAL, _wal_aug_text(), "test://ev-wal-aug")


# ---- dynamic card (GSDYN) ---------------------------------------------------


def test_dynamic_energy_is_quarter_hourly() -> None:
    """The card indexes 'op kwartierbasis' on the EPEX day-ahead 15-min curve."""
    snap = _dyn()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True


def test_dynamic_energy_factor() -> None:
    """1,05 x Belpex + 15 EUR/MWh, Belpex in EUR/MWh => factor = 1,05 * 1,06.

    The coefficient is a dimensionless Belpex multiplier (Bolt axis), so it is
    NOT scaled by ten the way Frank's cents-output coefficient is.
    """
    snap = _dyn()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.05 * 1.06)


def test_dynamic_energy_base() -> None:
    """base = 15 EUR/MWh => 0,015 EUR/kWh, times the 1,06 VAT multiplier."""
    snap = _dyn()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.base == pytest.approx(15.0 / 1000.0 * 1.06)


def test_dynamic_yearly_fixed_fee() -> None:
    """Vaste vergoeding 50 EUR/jaar, carried through unscaled."""
    snap = _dyn()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(50.0)


def test_dynamic_injection_factor_is_one() -> None:
    """1 x Belpex - 15 EUR/MWh, VAT-exempt => factor exactly 1,0."""
    snap = _dyn()
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_dynamic_injection_base() -> None:
    """base = -15 EUR/MWh => -0,015 EUR/kWh (no VAT)."""
    snap = _dyn()
    assert snap.injection is not None
    assert snap.injection.base == pytest.approx(-15.0 / 1000.0)


def test_dynamic_injection_current_is_none() -> None:
    """Spot-indexed injection: priced off the live spot, no monthly indicative."""
    snap = _dyn()
    assert snap.injection is not None
    assert snap.injection.current is None


# ---- fixed card (GS3JV) -----------------------------------------------------


def test_fixed_energy_is_fixed_rates() -> None:
    snap = _fixed()
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(13.57 / 100.0)


def test_fixed_yearly_fixed_fee() -> None:
    """Vaste vergoeding 75 EUR/jaar on the 3-year fixed card."""
    snap = _fixed()
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(75.0)


def test_fixed_injection_carries_the_spp_formula() -> None:
    """The card's "0,6 x Belpex-SPP-M - 15 EUR/MWh" is surfaced as month
    coefficients, with the printed 2,07 c€/kWh kept as the fallback for a
    month whose index is not published yet."""
    snap = _fixed()
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(2.07 / 100.0)
    assert inj.factor == pytest.approx(0.6)
    assert inj.base == pytest.approx(-15.0 / 1000.0)
    assert inj.spp_indexed is True
    assert inj.formula == "0,6 x Belpex-SPP-M - 15 EUR/MWh"


def test_fixed_injection_guarantee_is_parsed() -> None:
    """ "Als de berekening van onze formule lager zou uitkomen dan 1 EURcent
    /kWh, dan garanderen wij in elk geval 1 EURcent/kWh." Parsed off the card,
    not hardcoded, and it is a floor above zero rather than a zero clamp."""
    snap = _fixed()
    inj = snap.injection
    assert inj is not None
    assert inj.minimum == pytest.approx(0.01)
    assert inj.floor_at_zero is False


def test_fixed_injection_is_never_priced_per_hour() -> None:
    """Month coefficients on a flat-energy card. If the engine read them as an
    hourly formula the credit would follow the current slot's spot, which is
    the mis-credit the spp_indexed flag exists to prevent."""
    from custom_components.be_electricity_prices.injection import (
        _injection_is_spot_formula,
    )

    snap = _fixed()
    assert snap.injection is not None
    assert _injection_is_spot_formula(snap.injection, snap.energy) is False


def test_fixed_injection_resolves_the_delivery_month() -> None:
    """April 2026's Belpex-SPP-M settled far below the July card's printed
    figure, and low enough that the 1 c€/kWh guarantee binds."""
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.injection import (
        _bake_monthly_injection,
        _compute_injection_price,
        _injection_needs_month_spot,
    )

    snap = _fixed()
    entry = SimpleNamespace(data={"solar_regime": "injection"})
    assert _injection_needs_month_spot(snap, entry) is True  # type: ignore[arg-type]

    july = _bake_monthly_injection(snap, 0.063445)
    assert _compute_injection_price(july, entry, {}) == pytest.approx(  # type: ignore[arg-type]
        0.6 * 0.063445 - 0.015
    )
    # April resolves to 0,25 c€/kWh, so the guarantee lifts it to 1,00.
    april = _bake_monthly_injection(snap, 0.029166)
    assert _compute_injection_price(april, entry, {}) == pytest.approx(0.01)  # type: ignore[arg-type]


# ---- shared DSO + tax blocks (identical on both cards) ----------------------


@pytest.mark.parametrize("snap_fn", [_dyn, _fixed])
def test_dsos_cover_all_eight_fluvius_subareas(snap_fn) -> None:  # type: ignore[no-untyped-def]
    assert set(snap_fn().dsos) == set(FLUVIUS_KEYS)


def test_dso_antwerpen_digital_meter_columns() -> None:
    """Antwerpen digital meter: cap 52,3679 EUR/kW/yr, kWh 5,35329 c€,
    excl-nacht 4,81301 c€, databeheer 18,92 EUR/yr (maximumtarief ignored)."""
    a = _dyn().dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.3679)
    assert a.distribution_single == pytest.approx(5.35329 / 100.0)
    assert a.distribution_exclusive_night == pytest.approx(4.81301 / 100.0)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_dso_kempen_maps_to_iveka() -> None:
    """'FLUVIUS KEMPEN' is the Iveka sub-area."""
    assert _dyn().dsos["fluvius_iveka"].capacity_eur_per_kw_year == pytest.approx(
        59.5794
    )


def test_dso_midden_vlaanderen_maps_to_intergem() -> None:
    assert _dyn().dsos["fluvius_intergem"].distribution_single == pytest.approx(
        5.27945 / 100.0
    )


def test_dso_transport_is_zero() -> None:
    for overlay in _dyn().dsos.values():
        assert overlay.transport == 0.0


def test_taxes_federal_excise() -> None:
    assert _dyn().taxes.federal_excise == pytest.approx(5.03288 / 100.0)


def test_taxes_energy_contribution() -> None:
    assert _dyn().taxes.energy_contribution == pytest.approx(0.20417 / 100.0)


def test_taxes_flanders_renewables_combined_gsc_wkc() -> None:
    """GSC + WKC print as a single combined 1,554 c€/kWh."""
    assert _dyn().taxes.flanders_renewables == pytest.approx(1.554 / 100.0)


def test_taxes_energy_fund_domiciled_zero() -> None:
    """Standard residential is domiciled (0 EUR/month), not the 10,07 row."""
    assert _dyn().taxes.energy_fund_eur_per_month == pytest.approx(0.0)


def test_taxes_vat_rate_zero() -> None:
    """The energy leg is pre-scaled to VAT-inclusive, so vat_rate stays 0.0."""
    assert _dyn().taxes.vat_rate == 0.0


# ---- metadata ---------------------------------------------------------------


def test_publication_label_and_valid_until() -> None:
    snap = _dyn()
    assert snap.publication_label == "juli 2026"
    assert snap.valid_until == date(2026, 7, 31)


def test_dot_decimal_render_matches_comma() -> None:
    # A dot-decimal PDF re-render must extract identical values, not truncate a
    # mandatory value to its integer part as a comma-only regex would.
    comma = _dyn()
    dot = parse_snapshot(_DYNAMIC, _dyn_text().replace(",", "."), "test://ev")
    assert isinstance(comma.energy, DynamicRates)
    assert isinstance(dot.energy, DynamicRates)
    assert dot.energy.factor == pytest.approx(comma.energy.factor)
    assert dot.energy.base == pytest.approx(comma.energy.base)
    assert dot.taxes.federal_excise == pytest.approx(comma.taxes.federal_excise)
    assert dot.injection is not None and comma.injection is not None
    assert dot.injection.base == pytest.approx(comma.injection.base)


# ---- loud-fail contract -----------------------------------------------------


def test_missing_fee_is_fatal() -> None:
    text = _dyn_text().replace("Vaste vergoeding", "XXX")
    with pytest.raises(ExtractorError, match="vaste vergoeding"):
        parse_snapshot(_DYNAMIC, text, "test://ev")


def test_missing_gsc_wkc_is_fatal() -> None:
    text = _dyn_text().replace("GSC en WKC", "XXX")
    # The shared Flemish helper names the row it lost, where this extractor
    # used to report "tax block" for a missing excise and a missing GSC alike.
    with pytest.raises(ExtractorError, match="GSC/WKK"):
        parse_snapshot(_DYNAMIC, text, "test://ev")


def test_missing_dynamic_injection_is_fatal() -> None:
    # Every dynamic card prints the injectietarief formula; a miss must raise
    # rather than silently zero the solar feed-in credit.
    text = _dyn_text().replace("injectietarief", "XXX")
    with pytest.raises(ExtractorError, match="injectie"):
        parse_snapshot(_DYNAMIC, text, "test://ev")


def test_missing_fixed_energy_is_fatal() -> None:
    text = _fixed_text().replace("vast tarief", "XXX")
    with pytest.raises(ExtractorError, match="fixed energy"):
        parse_snapshot(_FIXED, text, "test://ev")


# ---- registration -----------------------------------------------------------


def test_energyvision_is_registered() -> None:
    assert "energyvision" in EXTRACTORS
    assert EXTRACTORS["energyvision"].label == "EnergyVision"
    contract_ids = {c.id for c in EXTRACTORS["energyvision"].contracts}
    assert contract_ids == {_DYNAMIC, _FIXED, _FIXED_WAL}


def test_each_contract_serves_exactly_one_region() -> None:
    # EnergyVision publishes each product for one region in one language, so
    # a contract is never offered in a region whose card does not exist.
    regions = {c.id: c.regions for c in EXTRACTORS["energyvision"].contracts}
    assert regions[_DYNAMIC] == frozenset({"flanders"})
    assert regions[_FIXED] == frozenset({"flanders"})
    assert regions[_FIXED_WAL] == frozenset({"wallonia"})


def test_contract_kinds() -> None:
    kinds = {c.id: c.kind for c in EXTRACTORS["energyvision"].contracts}
    assert kinds[_DYNAMIC] == "dynamic"
    assert kinds[_FIXED] == "fixed"
    assert kinds[_FIXED_WAL] == "fixed"


def test_discover_ids_superset_of_supported() -> None:
    assert {"GSDYN", "GS3JV", "GS1JV"} <= DISCOVER_IDS


# ---- Wallonia fixed card (GS1JV) --------------------------------------------


def test_wallonia_card_publication_metadata() -> None:
    snap = _wal()
    assert snap.supplier == "energyvision"
    assert snap.contract == _FIXED_WAL
    # French card: "Carte tarifaire juillet 2026". The label regex uses \w
    # rather than [A-Za-z] so the accented months (fevrier, aout, decembre)
    # do not silently blank it.
    assert snap.publication_label == "juillet 2026"
    assert snap.valid_until == date(2026, 7, 31)


def test_wallonia_energy_is_one_flat_vat_inclusive_rate() -> None:
    """The card prints a single "Electricite verte - tarif fixe" rate: no
    bi-horaire or exclusive-night energy price (those words appear only as
    DSO-table column headers), so every meter type bills `single`."""
    snap = _wal()
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1357)
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    # "Frais fixes 75 EUR/an", used as printed (TVAC).
    assert snap.energy.yearly_fixed_fee == pytest.approx(75.0)


def test_wallonia_injection_carries_the_same_spp_formula() -> None:
    """The French card states the identical formula, so the Walloon leg is
    month-indexed too and keeps its printed figure only as the fallback."""
    snap = _wal()
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.0207)
    assert inj.factor == pytest.approx(0.6)
    assert inj.base == pytest.approx(-15.0 / 1000.0)
    assert inj.spp_indexed is True
    # The guarantee is a floor above zero, so the zero clamp stays off.
    assert inj.minimum == pytest.approx(0.01)
    assert inj.floor_at_zero is False


def test_wallonia_taxes_carry_cv_and_connection_fee() -> None:
    """A Walloon card has no GSC/WKC and no Flemish energiefonds; it carries
    the CV green-certificate quota and the connection fee instead. Both are
    per-kWh, so silently zeroing either under-bills the whole contract."""
    taxes = _wal().taxes
    assert taxes.federal_excise == pytest.approx(0.0503288)
    assert taxes.energy_contribution == pytest.approx(0.0020417)
    assert taxes.wallonia_renewables == pytest.approx(0.03)
    assert taxes.region_connection_fee == pytest.approx(0.00075)
    assert taxes.flanders_renewables == 0.0
    assert taxes.energy_fund_eur_per_month == 0.0
    # Header: "Tous les prix et tarifs incluent la TVA a 6 %".
    assert taxes.vat_rate == 0.0


def test_wallonia_dsos_cover_every_walloon_key() -> None:
    snap = _wal()
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    # Seven ORES sub-areas print identical numbers and collapse onto one key;
    # Brabant Wallon is the representative row, as on the DATS 24 card.
    ores = snap.dsos["ores"]
    assert ores.distribution_single == pytest.approx(0.1198)
    assert ores.distribution_peak == pytest.approx(0.1327)
    assert ores.distribution_offpeak == pytest.approx(0.0739)
    assert ores.distribution_exclusive_night == pytest.approx(0.0739)
    assert ores.transport == pytest.approx(0.0274)
    assert ores.data_management_per_year == pytest.approx(14.10)
    assert ores.prosumer_eur_per_kva_year == pytest.approx(85.84)


def test_wallonia_impact_bands_are_not_swapped() -> None:
    """EnergyVision prints the CWaPE Impact bands cheapest-first
    (ECO | MEDIUM | PIC), the REVERSE of the DATS 24 card carrying the same
    regulated numbers. Reusing that positional mapping swaps peak and
    off-peak distribution for every Walloon Impact user, so pin the order by
    value rather than by column index."""
    ores = _wal().dsos["ores"]
    eco, medium, pic = (
        ores.distribution_eco,
        ores.distribution_medium,
        ores.distribution_pic,
    )
    assert eco is not None and medium is not None and pic is not None
    assert eco == pytest.approx(0.0509)
    assert medium == pytest.approx(0.1083)
    assert pic == pytest.approx(0.1657)
    assert eco < medium < pic


def test_wallonia_dsos_are_not_all_the_same_row() -> None:
    """Guards against a regex that anchors loosely and aligns every operator
    to the first matching row."""
    dsos = _wal().dsos
    assert dsos["aieg"].distribution_single == pytest.approx(0.1087)
    assert dsos["aiesh"].distribution_single == pytest.approx(0.1363)
    assert dsos["resa"].distribution_single == pytest.approx(0.1106)
    assert dsos["rew"].distribution_single == pytest.approx(0.1247)
    assert dsos["rew"].data_management_per_year == pytest.approx(26.44)


def test_wallonia_mandatory_rows_fail_loud() -> None:
    """Rows no Walloon card omits. A miss is layout drift, so raise rather
    than price a partial card. The energy contribution and the connection fee
    are deliberately absent from this list: see the two tests below."""
    for needle, match in (
        ("Électricité verte - tarif fixe", "energy price"),
        ("Injection – variable", "injection price"),
        ("Frais fixes", "frais fixes"),
        ("certificats de cogénération", "tax block"),
    ):
        text = _wal_text().replace(needle, "XXX")
        with pytest.raises(ExtractorError, match=match):
            parse_snapshot(_FIXED_WAL, text, "test://ev-wal-jul")


def test_august_card_parses_the_flat_excise() -> None:
    """The four-tier table is gone; the flat "Accise speciale" row is the
    authoritative single rate."""
    assert _wal_aug().taxes.federal_excise == pytest.approx(0.04876)


def test_august_card_reads_the_absent_contribution_as_abolished() -> None:
    """The levy went to zero on 2026-08-01 and the card drops the row, so an
    absent row is the abolished levy rather than layout drift."""
    assert _wal_aug().taxes.energy_contribution == 0.0


def test_august_card_bills_the_absent_connection_fee_as_zero_and_flags_it() -> None:
    """Wallonia still levies the fee, but EnergyVision publishes it nowhere.
    Bill 0 rather than take the contract offline, and flag it so the
    coordinator can disclose what the cost excludes."""
    taxes = _wal_aug().taxes
    assert taxes.region_connection_fee == 0.0
    assert taxes.region_connection_fee_unavailable is True


def test_august_card_still_reads_the_green_certificate_cost() -> None:
    """The one tax row that survived the rewrite."""
    assert _wal_aug().taxes.wallonia_renewables == pytest.approx(0.03)


def test_july_card_keeps_the_tiered_excise_and_the_connection_fee() -> None:
    """An older card must keep pricing off the 0-3.000 kWh tier and must not
    be flagged as missing a fee it actually prints."""
    taxes = _wal().taxes
    assert taxes.federal_excise == pytest.approx(0.0503288)
    assert taxes.energy_contribution == pytest.approx(0.0020417)
    assert taxes.region_connection_fee == pytest.approx(0.00075)
    assert taxes.region_connection_fee_unavailable is False


def test_flanders_parsers_are_not_used_for_the_walloon_card() -> None:
    """The two cards share no wording, so a Walloon card fed through the
    Dutch path must fail rather than silently produce a partial snapshot."""
    with pytest.raises(ExtractorError):
        parse_snapshot(_FIXED, _wal_text(), "test://ev-wal-jul")


def test_august_2026_flat_excise_replaces_the_tier_table() -> None:
    """On 2026-08-01 the federal scheme folded the separate energy
    contribution into the special excise and flattened it. EnergyVision
    made the switch a month after Engie / Mega / Eneco: its August Flemish
    card dropped both the "Verbruik tussen 0 & 3.000 kWh" tier row and the
    Energiebijdrage line, which took every Flemish contract offline."""
    from custom_components.be_electricity_prices.providers.energyvision import (
        _extract_taxes,
    )

    august = (
        "Kosten GSC en WKC geldig voor 1,554 €cent/kWh.\n"
        "Bijdrage energiefonds / toeslagen en federale accijns\n"
        "Standaard tarief gedomicilieerd: 0 €/maand\n"
        "Federale accijns (€cent/kWh)\n"
        "Bijzondere accijns 4,876 €cent/kWh\n"
    )
    taxes = _extract_taxes(august)
    assert taxes.federal_excise == pytest.approx(0.04876)
    # Row deleted because the levy is abolished, not because of drift.
    assert taxes.energy_contribution == 0.0
    assert taxes.flanders_renewables == pytest.approx(0.01554)

    # The pre-August tiered card keeps working, contribution and all.
    july = (
        "Kosten GSC en WKC geldig voor 1,554 €cent/kWh.\n"
        "Energiebijdrage 0,20417\n"
        "Verbruik tussen 0 & 3.000 kWh 5,0329\n"
    )
    taxes = _extract_taxes(july)
    assert taxes.federal_excise == pytest.approx(0.050329)
    assert taxes.energy_contribution == pytest.approx(0.0020417)


def test_missing_renewables_row_is_still_fatal() -> None:
    """Tolerating the abolished contribution must not make the whole tax
    block optional: the GSC/WKC quota cost is a per-kWh charge, so
    silently zeroing it under-bills every Flemish user."""
    from custom_components.be_electricity_prices.providers.energyvision import (
        _extract_taxes,
    )

    with pytest.raises(ExtractorError, match="GSC/WKK"):
        _extract_taxes("Bijzondere accijns 4,876 €cent/kWh\n")
