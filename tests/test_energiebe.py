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

"""Fixture-based tests for the energie.be extractor."""

from __future__ import annotations

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    SpotMonthlyRates,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.energiebe import parse_snapshot
from tests import fixture_text


def _text() -> str:
    return fixture_text("energiebe_dynamic_jul.pdf", layout=True)


def _snap() -> SupplierSnapshot:
    return parse_snapshot(_text(), "test://energiebe-jul")


def _var_text() -> str:
    return fixture_text("energiebe_variable_aug.pdf", layout=True)


def _var_snap() -> SupplierSnapshot:
    return parse_snapshot(_var_text(), "test://energiebe-var-aug", "energiebe_variable")


def _fixed_text() -> str:
    return fixture_text("energiebe_fixed_aug.pdf", layout=True)


def _fixed_snap() -> SupplierSnapshot:
    return parse_snapshot(_fixed_text(), "test://energiebe-fix-aug", "energiebe_fixed")


def test_energy_is_dynamic_rates() -> None:
    assert isinstance(_snap().energy, DynamicRates)


def test_energy_is_quarter_hourly() -> None:
    """The card bills 'op kwartierbasis' on the EPEX day-ahead 15-minute curve."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True


def test_energy_formula_factor() -> None:
    """(1,04 x Belpex + 0,50) c€/kWh, Belpex in c€/kWh => factor = 1.04 * 1.06.

    energie.be prints Belpex in c€/kWh (not EUR/MWh like Frank), so the factor
    is NOT multiplied by 10.
    """
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.04 * 1.06)


def test_energy_formula_base() -> None:
    """base = 0.50 c€/kWh => 0.005 EUR/kWh, times the 1.06 VAT multiplier."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.base == pytest.approx(0.50 / 100.0 * 1.06)


def test_yearly_fixed_fee_is_already_annual() -> None:
    """Vaste vergoeding is quoted 25 EUR/jaar; carried through unscaled."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(25.0)


def test_dsos_cover_all_eight_fluvius_subareas() -> None:
    assert set(_snap().dsos) == set(FLUVIUS_KEYS)


def test_dso_antwerpen_distribution() -> None:
    """Antwerpen digital meter: normaal 5,35 ct/kWh, excl nacht 4,81 ct/kWh."""
    a = _snap().dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(5.35 / 100.0)
    assert a.distribution_exclusive_night == pytest.approx(4.81 / 100.0)


def test_dso_antwerpen_capacity_and_databeheer() -> None:
    a = _snap().dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_dso_transport_is_zero() -> None:
    """Transport is bundled into distribution on energie.be's card."""
    for overlay in _snap().dsos.values():
        assert overlay.transport == 0.0


def test_dso_wrapped_label_halle_vilvoorde() -> None:
    """The label wraps as 'Fluvius (Halle-\\n<numbers>\\nVilvoorde)'; the row
    numbers still bind (normaal 5,64, cap 59,41)."""
    hv = _snap().dsos["fluvius_halle_vilvoorde"]
    assert hv.distribution_single == pytest.approx(5.64 / 100.0)
    assert hv.capacity_eur_per_kw_year == pytest.approx(59.41)


def test_dso_wrapped_label_midden_vlaanderen() -> None:
    """Midden-Vlaanderen (Intergem key) also wraps across the number row."""
    mv = _snap().dsos["fluvius_intergem"]
    assert mv.distribution_single == pytest.approx(5.28 / 100.0)
    assert mv.capacity_eur_per_kw_year == pytest.approx(53.13)


def test_injection_factor() -> None:
    """(1 x Belpex - 0,98), Belpex in c€/kWh, VAT-exempt => factor = 1.0."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_injection_base() -> None:
    """base = -0.98 c€/kWh => -0.0098 EUR/kWh (no *10, no VAT)."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.base == pytest.approx(-0.98 / 100.0)


def test_injection_current_is_none() -> None:
    """Spot-indexed injection: priced off the live spot, no monthly indicative."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.current is None


def test_taxes_federal_excise() -> None:
    assert _snap().taxes.federal_excise == pytest.approx(5.0329 / 100.0)


def test_taxes_energy_contribution() -> None:
    """'Bijdrage op de Energie' 0,2042 c€/kWh."""
    assert _snap().taxes.energy_contribution == pytest.approx(0.2042 / 100.0)


def test_taxes_flanders_renewables_gsc_plus_wkk() -> None:
    """Residential GSC 1,17 + WKK 0,39 = 1,56 c€/kWh."""
    assert _snap().taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)


def test_taxes_energy_fund_residential_zero() -> None:
    assert _snap().taxes.energy_fund_eur_per_month == pytest.approx(0.0)


def test_taxes_vat_rate_zero() -> None:
    """The energy leg is pre-scaled to VAT-inclusive, so vat_rate stays 0.0."""
    assert _snap().taxes.vat_rate == 0.0


def test_only_residential_block_is_parsed() -> None:
    """The PDF appends a professional block (GSC 1,10 / WKK 0,36, databeheer
    17,85). None of it may leak into the residential snapshot."""
    snap = _snap()
    assert snap.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    for overlay in snap.dsos.values():
        assert overlay.data_management_per_year == pytest.approx(18.92)


def test_dot_decimal_render_matches_comma() -> None:
    # A dot-decimal PDF re-render must extract identical values, not truncate a
    # mandatory value to its integer part as a comma-only regex would.
    comma = _snap()
    dot = parse_snapshot(_text().replace(",", "."), "test://energiebe-jul")
    assert isinstance(comma.energy, DynamicRates)
    assert isinstance(dot.energy, DynamicRates)
    assert dot.energy.factor == pytest.approx(comma.energy.factor)
    assert dot.energy.base == pytest.approx(comma.energy.base)
    assert dot.taxes.federal_excise == pytest.approx(comma.taxes.federal_excise)
    assert dot.injection is not None and comma.injection is not None
    assert dot.injection.base == pytest.approx(comma.injection.base)


def test_missing_fee_is_fatal() -> None:
    # The vaste vergoeding standing charge is mandatory; a miss must raise.
    text = _text().replace("Vaste vergoeding", "XXX")
    with pytest.raises(ExtractorError, match="vaste vergoeding"):
        parse_snapshot(text, "test://energiebe-jul")


def test_missing_gsc_wkk_is_fatal() -> None:
    # energie.be dynamic is Flanders-only, so GSC + WKK are mandatory; a miss
    # must raise rather than silently zero the renewables levy.
    text = _text().replace("GSC", "XXX").replace("WKK", "YYY")
    with pytest.raises(ExtractorError, match="GSC/WKK"):
        parse_snapshot(text, "test://energiebe-jul")


def test_missing_injection_is_fatal() -> None:
    # Every card prints the terugleveringsvergoeding; a miss must raise rather
    # than silently zero the solar feed-in credit. The parser anchors on the
    # section header, which both products share, rather than on the body
    # wording, which does not ("injectievergoeding" on the dynamic card,
    # "terugleververgoeding" on the variable one).
    text = _text().replace("Terugleveringsvergoeding", "XXX")
    with pytest.raises(ExtractorError, match="injection formula"):
        parse_snapshot(text, "test://energiebe-jul")


def test_dynamic_body_wording_alone_is_not_the_anchor() -> None:
    """The two products word the same row differently; neither wording is load-bearing."""
    text = _text().replace("injectievergoeding", "terugleververgoeding")
    snap = parse_snapshot(text, "test://energiebe-jul")
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_supplier_and_contract_metadata() -> None:
    snap = _snap()
    assert snap.supplier == "energiebe"
    assert snap.contract == "energiebe_dynamic"
    assert snap.publication_label == "juli 2026"


# ---- registration ---------------------------------------------------------------


def test_energiebe_is_registered() -> None:
    assert "energiebe" in EXTRACTORS
    assert EXTRACTORS["energiebe"].label == "energie.be"
    contract_ids = {c.id for c in EXTRACTORS["energiebe"].contracts}
    assert contract_ids == {
        "energiebe_dynamic",
        "energiebe_variable",
        "energiebe_fixed",
    }


def test_contract_kinds_and_flanders_only() -> None:
    kinds = {c.id: c.kind for c in EXTRACTORS["energiebe"].contracts}
    assert kinds == {
        "energiebe_dynamic": "dynamic",
        # Not "variable": the card resolves the month from a monthly index and
        # prints only a forecast of it, so the price has to be computed from
        # the index rather than read off the card.
        "energiebe_variable": "spot_monthly",
        # The one card of the three that prints a rate, so it needs no key.
        "energiebe_fixed": "fixed",
    }
    for c in EXTRACTORS["energiebe"].contracts:
        assert c.regions == frozenset({"flanders"})


def test_an_abolished_contribution_row_no_longer_takes_the_supplier_offline() -> None:
    """The three Flemish tax extractors had drifted on one policy.

    The federal contribution dropped to zero on 2026-08-01 and suppliers
    answered by deleting the row. Frank and EnergyVision were taught to read an
    absent row as the abolished levy; energie.be still required it, so it would
    have raised on every fetch the moment its card followed suit -- taking the
    entry offline over a levy that is no longer billed. The shared helper holds
    that policy for all three now.

    The rows that ARE still billed stay mandatory: a miss there is a real
    layout drift that would silently under-bill.
    """
    import re

    from custom_components.be_electricity_prices.providers.base import ExtractorError
    from custom_components.be_electricity_prices.providers.energiebe import (
        _extract_taxes,
    )

    text = _text()
    # Today's card still prints the row, zeroed by the supplier.
    assert _extract_taxes(text).energy_contribution == pytest.approx(0.0020420)

    without = re.sub(r"[^\n]*Bijdrage\s+op\s+de\s+Energie[^\n]*\n", "", text)
    assert without != text
    taxes = _extract_taxes(without)
    assert taxes.energy_contribution == 0.0
    # Everything else on the card is untouched by the missing row.
    assert taxes.federal_excise == pytest.approx(0.050329)
    assert taxes.flanders_renewables == pytest.approx(0.0156)

    for pattern in (
        r"[^\n]*Bijzondere\s+accijns[^\n]*\n",
        r"[^\n]*\bGSC\b[^\n]*\n",
        r"[^\n]*\bWKK\b[^\n]*\n",
    ):
        broken = re.sub(pattern, "", text)
        assert broken != text, pattern
        with pytest.raises(ExtractorError):
            _extract_taxes(broken)


# ---- variable contract ----------------------------------------------------------


def test_variable_energy_is_spot_monthly_rates() -> None:
    """(1,12 x Belpex_RLP + 0,80) is a monthly index, not a per-slot spot."""
    assert isinstance(_var_snap().energy, SpotMonthlyRates)


def test_variable_energy_formula_factor() -> None:
    """Belpex_RLP prints in c€/kWh like the dynamic card's Belpex: no *10."""
    snap = _var_snap()
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == pytest.approx(1.12 * 1.06)


def test_variable_energy_formula_base() -> None:
    """base = 0.80 c€/kWh => 0.008 EUR/kWh, times the 1.06 VAT multiplier."""
    snap = _var_snap()
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.base == pytest.approx(0.80 / 100.0 * 1.06)


def test_variable_yearly_fixed_fee() -> None:
    """35 EUR/jaar, and the row's unit label is a line below its number.

    The variable card wraps a sentence of body text between the two ("35
    methodologie): 12,75c €/kWh."), which a same-line pattern misses.
    """
    snap = _var_snap()
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(35.0)


@pytest.mark.parametrize(
    "unit",
    ["( € / jaar )", "(EUR/jaar)", "(euro/jaar)", "(€/jaar )", "(?/jaar)"],
)
def test_fee_unit_spelling_is_not_load_bearing(unit: str) -> None:
    r"""Any "…/jaar" unit must bind the fee, however the renderer spells it.

    The same cards already render the sibling energy-fund unit as
    "(EUR/maand )" with a stray space, and a PDF re-render can drop the € glyph
    entirely. A missing fee is fatal by design, so pinning the exact spelling
    would take BOTH energie.be contracts offline over a font quirk - which is
    why the tax regexes match the unit as `\([^)]*\)` and this one follows them.
    """
    text = _var_text().replace("(€/jaar)", unit)
    snap = parse_snapshot(text, "test://energiebe-var-aug", "energiebe_variable")
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(35.0)


def test_fee_row_on_a_single_line_still_binds() -> None:
    """The unit may also sit on the number's own line.

    Only the variable card wraps body text between the two; the dynamic card
    prints them on consecutive lines and a future re-render could collapse
    them. Pinning the wrap would silently make the collapsed form fatal.
    """
    text = _text().replace("Vaste vergoeding", "Vaste vergoeding 25 (€/jaar) XX", 1)
    snap = parse_snapshot(text, "test://energiebe-jul")
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(25.0)


def test_negative_injection_indicative_is_read_not_fatal() -> None:
    """(0,60 x Belpex_SPP - 0,80) prints NEGATIVE below 1,33 c€/kWh SPP.

    energie.be's own published index table bottoms out at 1,65, so this is one
    sunny spring away. InjectionRates and live_check's _validate_injection both
    state a monthly indicative is allowed to settle negative; a sign-blind
    column pattern would not mis-credit such a card, it would fail to read it
    and take the whole contract offline.
    """
    for rendered in ("-0,15", "–0,15"):  # hyphen and the en dash the card uses
        text = _var_text().replace("3,43 een variabele", f"{rendered} een variabele")
        snap = parse_snapshot(text, "test://energiebe-var-aug", "energiebe_variable")
        assert snap.injection is not None
        assert snap.injection.current == pytest.approx(-0.0015)


def test_variable_energy_coefficients_are_the_formula_not_the_printed_price() -> None:
    """The card's 15,98 c€/kWh is the VNR forecast, and must not reach pricing.

    The July 2026 card printed 13,13 c€/kWh on a forecast index of 10,34 while
    the month settled at a realized Belpex_RLP of 11,42 - a rate of 14,41.

    Pinned by evaluating the parsed coefficients AT the card's own forecast
    index: they must reproduce the printed price exactly, which is what proves
    they are the formula's and not back-derived from the price. Asserting the
    absence of a ``current`` attribute instead would pass whatever the parser
    did - ``SpotMonthlyRates`` has no such field for any instance.
    """
    snap = _var_snap()
    assert isinstance(snap.energy, SpotMonthlyRates)
    forecast_index = 12.75 / 100.0  # "ingeschatte index (VNR methodologie)"
    printed = 15.98 / 100.0  # the Energieprijs column, incl. VAT
    assert snap.energy.factor * forecast_index + snap.energy.base == pytest.approx(
        printed,
        abs=1e-4,  # the card rounds its printed price to two decimals
    )
    # And the printed price itself is nowhere in the leg: a parser that stored
    # it as the base would satisfy neither this nor the factor test.
    assert snap.energy.base != pytest.approx(printed)


def test_variable_injection_carries_the_spp_formula_and_its_fallback() -> None:
    """(0,60 x Belpex_SPP - 0,80), plus Zonnestroom 3,43 c€/kWh as fallback.

    All three parts matter. The formula is what makes the credit exact; the
    printed indicative is what gets credited while the Synergrid profile is
    unavailable; and ``spp_indexed`` is what keeps the formula off the energy
    leg's mean, which indexes on Belpex_RLP - 11,42 c€/kWh in July 2026
    against the SPP's 6,34, so resolving it there would roughly double the
    credit. Belpex prints in c€/kWh on this card, so no x10 and no VAT.
    """
    snap = _var_snap()
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(3.43 / 100.0)
    assert snap.injection.factor == pytest.approx(0.60)
    assert snap.injection.base == pytest.approx(-0.80 / 100.0)
    assert snap.injection.spp_indexed is True


@pytest.mark.parametrize("weights", [None, {}])
def test_variable_injection_falls_back_to_the_card_without_a_profile(
    weights: dict[tuple[int, int, int], float] | None,
) -> None:
    """No Synergrid profile means the card's indicative, never the energy mean.

    Both states a failed profile leaves behind are covered: never fetched
    (``None``) and fetched-but-empty (``{}``, what the back-off leaves). The
    historical walk must answer "no spot" for either, so the credit falls
    through to the printed indicative. Handing back the energy leg's Belpex_RLP
    mean instead would pay 6,05 c€/kWh against a contract that owes 3,00.
    """
    from custom_components.be_electricity_prices.injection import (
        _historical_injection_rate,
    )
    from custom_components.be_electricity_prices.spot_stats import (
        _injection_is_spp_indexed,
        _spp_injection_spot,
    )

    snap = _var_snap()
    assert snap.injection is not None
    rlp_mean = 11.42 / 100.0  # July 2026's energy index - the wrong one here
    spot = _spp_injection_spot(
        rlp_mean,
        monthly_mean=True,
        spp_weights=weights,
        historical_spots={},
        year=2026,
        month=7,
        cache={},
        strict=_injection_is_spp_indexed(snap),
    )
    assert spot is None
    credited = _historical_injection_rate(snap.injection, spot, energy=snap.energy)
    assert credited == pytest.approx(3.43 / 100.0)


def test_variable_injection_never_prices_off_an_hourly_spot() -> None:
    """Carrying factor/base must not turn the credit into an hourly one.

    ``_injection_is_spot_formula`` fires only when the energy leg is dynamic
    or the card prints no indicative; this card is neither, so the live
    sensor keeps the flat monthly value whatever the current hour costs. That
    guard predates this contract and is now load-bearing for a second reason:
    the formula it would otherwise resolve indexes on a MONTHLY solar-weighted
    mean, so pricing it per hour is the wrong axis entirely, not merely noisy.
    """
    from datetime import datetime, timezone

    from custom_components.be_electricity_prices.injection import (
        _injection_price_for_slot,
        _injection_varies_intraday,
    )

    snap = _var_snap()
    assert snap.injection is not None
    when = datetime(2026, 8, 19, 13, 0, tzinfo=timezone.utc)
    flat = 3.43 / 100.0
    for spot in (0.30, -0.02, 0.0, None):
        assert _injection_price_for_slot(
            snap.injection, snap.energy, spot, when
        ) == pytest.approx(flat)
    # and it publishes no today/tomorrow array, which would imply it varies
    assert _injection_varies_intraday(snap.injection, snap.energy) is False


def test_variable_injection_resolves_exactly_on_the_spp_mean() -> None:
    """The whole point of carrying the formula: July 2026 owed 3,00 c€/kWh.

    Belpex_SPP settled at 6,34 that month and Belpex_RLP at 11,42. Baking the
    formula against the SPP mean reproduces the contract exactly; against the
    energy leg's mean it pays 6,05, and the card's own indicative (a VNR
    forecast) pays 2,77.
    """
    from custom_components.be_electricity_prices.injection import (
        _bake_monthly_injection,
    )

    snap = _var_snap()
    on_spp = _bake_monthly_injection(snap, 6.34 / 100.0).injection
    on_rlp = _bake_monthly_injection(snap, 11.42 / 100.0).injection
    assert on_spp is not None and on_rlp is not None
    assert on_spp.current == pytest.approx(3.004 / 100.0, abs=1e-6)
    assert on_rlp.current == pytest.approx(6.052 / 100.0, abs=1e-6)


def test_variable_injection_formula_is_kept_for_display() -> None:
    snap = _var_snap()
    assert snap.injection is not None
    assert snap.injection.formula is not None
    assert "Belpex_SPP" in snap.injection.formula


def test_variable_dsos_cover_all_eight_fluvius_subareas() -> None:
    assert set(_var_snap().dsos) == set(FLUVIUS_KEYS)


def test_variable_shares_the_regulated_overlays_with_the_dynamic_card() -> None:
    """DSO and tax rows are regulated: same month, same values, both products.

    Pinned because the two cards are separate PDFs published independently;
    a parse that drifted on one would show up as a difference here.
    """
    var, dyn = _var_snap(), _snap()
    # The DSO table is a regulated, supplier- and product-independent
    # publication that did not change between the two fixtures' months, so the
    # two cards must yield the SAME overlays, row for row. This is the real
    # cross-check: literals alone would pass even if one parser drifted onto
    # the wrong columns in exactly the way the other did.
    assert var.dsos == dyn.dsos
    # The tax block DID change on 2026-08-01, and the two fixtures straddle it:
    # the excise went flat and the federal contribution was folded into it.
    assert dyn.taxes.federal_excise == pytest.approx(5.0329 / 100.0)
    assert var.taxes.federal_excise == pytest.approx(4.8760 / 100.0)
    assert dyn.taxes.energy_contribution == pytest.approx(0.2042 / 100.0)
    assert var.taxes.energy_contribution == pytest.approx(0.0)
    # Everything else on the regulated half is shared.
    assert var.taxes.flanders_renewables == dyn.taxes.flanders_renewables
    assert var.taxes.energy_fund_eur_per_month == pytest.approx(0.0)
    assert var.taxes.vat_rate == 0.0


def test_variable_metadata_and_publication_label() -> None:
    snap = _var_snap()
    assert snap.supplier == "energiebe"
    assert snap.contract == "energiebe_variable"
    assert snap.publication_label == "augustus 2026"


def test_variable_abolished_contribution_row_is_read_as_zero() -> None:
    """The August 2026 card prints the abolished federal contribution as 0."""
    assert _var_snap().taxes.energy_contribution == pytest.approx(0.0)


def test_variable_missing_fee_is_fatal() -> None:
    text = _var_text().replace("Vaste vergoeding", "XXX")
    with pytest.raises(ExtractorError, match="vaste vergoeding"):
        parse_snapshot(text, "test://energiebe-var-aug", "energiebe_variable")


def test_variable_missing_injection_is_fatal() -> None:
    text = _var_text().replace("Zonnestroom", "XXX")
    with pytest.raises(ExtractorError, match="injection indicative"):
        parse_snapshot(text, "test://energiebe-var-aug", "energiebe_variable")


def test_variable_missing_energy_formula_is_fatal() -> None:
    text = _var_text().replace("Belpex_RLP", "XXX")
    with pytest.raises(ExtractorError, match="variable energy formula"):
        parse_snapshot(text, "test://energiebe-var-aug", "energiebe_variable")


def test_variable_dot_decimal_render_matches_comma() -> None:
    comma = _var_snap()
    dot = parse_snapshot(
        _var_text().replace(",", "."), "test://energiebe-var-aug", "energiebe_variable"
    )
    assert isinstance(comma.energy, SpotMonthlyRates)
    assert isinstance(dot.energy, SpotMonthlyRates)
    assert dot.energy.factor == pytest.approx(comma.energy.factor)
    assert dot.energy.base == pytest.approx(comma.energy.base)
    assert dot.energy.yearly_fixed_fee == pytest.approx(comma.energy.yearly_fixed_fee)
    assert dot.injection is not None and comma.injection is not None
    assert dot.injection.current == pytest.approx(comma.injection.current)


def test_a_card_served_for_the_wrong_product_is_rejected() -> None:
    """The index parameter's name is the only thing in the text that says
    which product a card belongs to.

    Both cards print the same "formule (excl. btw): (N x Belpex... +/- N)"
    row; only the parameter differs - bare ``Belpex`` per slot on the dynamic
    card, monthly ``Belpex_RLP`` on the variable one. Tolerating either
    spelling for either contract means a card served at the wrong URL parses
    silently into the other product's coefficients, and a dynamic entry then
    bills the variable formula against the per-slot spot. This supplier
    already serves one stale card at a legacy document key, so a mixed-up URL
    is not hypothetical - and the failure is a wrong price, not a missing one.
    """
    with pytest.raises(ExtractorError, match="Belpex_RLP \\(variable\\) card"):
        parse_snapshot(_var_text(), "test://x", "energiebe_dynamic")
    with pytest.raises(ExtractorError, match="without Belpex_RLP"):
        parse_snapshot(_text(), "test://x", "energiebe_variable")


def test_variable_injection_must_be_indexed_on_spp() -> None:
    """The injection half of the same discriminator.

    A variable card whose injection stopped naming Belpex_SPP would be
    resolved against the SPP-weighted mean anyway, because ``spp_indexed`` is
    set unconditionally. Better to fail than to weight by a profile the card
    no longer references.
    """
    text = _var_text().replace("Belpex_SPP", "Belpex")
    with pytest.raises(ExtractorError, match="not indexed on Belpex_SPP"):
        parse_snapshot(text, "test://x", "energiebe_variable")


def test_dynamic_injection_cannot_bind_the_energy_row() -> None:
    """On the dynamic card both formulas print the bare "Belpex".

    Only their order separates them, and nothing enforced that until now.
    Deleting the injection section leaves the energy row as the first match
    after the anchor; binding it would credit a solar user the CONSUMPTION
    rate instead of failing.
    """
    text = _text().replace("(1 x Belpex", "(XXX")
    with pytest.raises(ExtractorError):
        parse_snapshot(text, "test://energiebe-jul")


def test_an_unknown_contract_is_rejected() -> None:
    """parse_snapshot defaults to the dynamic shape; fetch guards the id."""
    import asyncio

    from custom_components.be_electricity_prices.providers.energiebe import fetch

    with pytest.raises(ExtractorError, match="unknown energie.be contract"):
        asyncio.run(fetch(None, "energiebe_nope", "flanders"))  # type: ignore[arg-type]


# ---- variable card URL resolution -----------------------------------------------


def _resolve(body: str, tariff_type: str = "Variable") -> str:
    """Run _resolve_card_url against a canned contracts-API body."""
    import asyncio

    from custom_components.be_electricity_prices.providers import energiebe

    async def _fake_fetch_text(session: object, url: str, **kwargs: object) -> str:
        return body

    original = energiebe.fetch_text
    energiebe.fetch_text = _fake_fetch_text  # type: ignore[assignment]
    try:
        return asyncio.run(
            energiebe._resolve_card_url(None, tariff_type)  # type: ignore[arg-type]
        )
    finally:
        energiebe.fetch_text = original  # type: ignore[assignment]


_GOOD_BODY = (
    '{"contracts": ['
    '{"tariffType": "Fixed", "contractTypeElRes": {"tariffDocument": "https://x/vast.pdf"}},'
    '{"tariffType": "Variable", "contractTypeElRes": {"tariffDocument": "https://x/var.pdf"},'
    ' "contractTypeElPro": {"tariffDocument": "https://x/var-pro.pdf"}}]}'
)


def test_resolver_picks_the_residential_variable_document() -> None:
    """Not the fixed sibling, and not the professional edition of the same product."""
    assert _resolve(_GOOD_BODY) == "https://x/var.pdf"


@pytest.mark.parametrize(
    "body",
    [
        '{"contracts": null}',
        '{"contracts": 5}',
        '{"contracts": "Variable"}',
        "[]",
        "7",
        "<html>login</html>",
        '{"contracts": []}',
        '{"contracts": ["Variable"]}',
        '{"contracts": [{"tariffType": "Fixed", "contractTypeElRes": {"tariffDocument": "https://x/a.pdf"}}]}',
        '{"contracts": [{"tariffType": "Variable", "contractTypeElRes": null}]}',
        '{"contracts": [{"tariffType": "Variable", "contractTypeElRes": {"tariffDocument": {"u": 1}}}]}',
        '{"contracts": [{"tariffType": "Variable", "contractTypeElRes": {"tariffDocument": "http://x/a.pdf"}}]}',
    ],
)
def test_resolver_funnels_every_bad_shape_into_extractor_error(body: str) -> None:
    """Callers catch ExtractorError and nothing else.

    A payload that is well-formed JSON but the wrong shape used to walk into
    ``for contract in contracts`` and raise a bare TypeError, which escapes the
    extractor contract and surfaces as an unhandled error rather than a failed
    fetch. A non-string tariffDocument was worse: ``str()`` turned it into a
    nonsense URL that was then fetched.
    """
    with pytest.raises(ExtractorError):
        _resolve(body)


def test_resolver_selects_by_tariff_type() -> None:
    """The tariffType is a parameter now, so a fixed contract must not be able
    to pick up the variable document (or vice versa) from the same payload."""
    assert _resolve(_GOOD_BODY, "Variable") == "https://x/var.pdf"
    assert _resolve(_GOOD_BODY, "Fixed") == "https://x/vast.pdf"


def test_resolver_never_falls_back_to_another_card() -> None:
    """There is deliberately no fallback.

    The ``?key=Tariffs`` document key still answers 200 with an April 2024 card
    whose DSO sub-areas no longer exist. Being offline for a tick is the better
    failure, so a body with no residential variable entry must raise rather
    than return some other product's URL.
    """
    body = (
        '{"contracts": [{"tariffType": "Dynamic",'
        ' "contractTypeElRes": {"tariffDocument": "https://x/dyn.pdf"}}]}'
    )
    with pytest.raises(ExtractorError, match="no residential Variable card"):
        _resolve(body)


# ---- fixed contract -------------------------------------------------------------


def test_fixed_energy_is_a_flat_vat_inclusive_rate() -> None:
    """18,26 c€/kWh, and NOT grossed by the VAT multiplier.

    The other two products print their formula "(excl. btw)" and have to be
    grossed; this column carries no such marker, and the card header says every
    price on it is VAT-inclusive unless marked. Running it through _VAT_MULT
    anyway would bill 19,36 c€/kWh - a 6% overcharge that looks entirely
    plausible on a bill.
    """
    snap = _fixed_snap()
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(18.26 / 100.0)
    assert snap.energy.single != pytest.approx(18.26 / 100.0 * 1.06)
    assert snap.energy.yearly_fixed_fee == pytest.approx(35.0)
    assert snap.taxes.vat_rate == 0.0


def test_fixed_card_publishes_one_rate_for_every_meter() -> None:
    """No peak / offpeak or exclusive-night column exists on this card.

    Leaving them None is what makes the pricing engine fall back to ``single``
    for a bi-hourly meter, which is correct here rather than an approximation.
    """
    snap = _fixed_snap()
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    assert snap.energy.yearly_fixed_fee_exclusive_night is None


def test_fixed_injection_is_the_indicative_only() -> None:
    """The card prints the same Belpex_SPP formula as the variable one, and
    for a fixed contract it is deliberately NOT stored.

    A fixed contract collects no ENTSO-E key and its energy leg fetches no
    spots, so there is no monthly mean to resolve the formula against.
    Emitting factor/base would set ``spp_indexed`` and pull Synergrid's 52 MB
    profile for a weighting that could never be applied.
    """
    snap = _fixed_snap()
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(3.43 / 100.0)
    assert snap.injection.factor is None
    assert snap.injection.base is None
    assert snap.injection.spp_indexed is False
    # the formula string is still carried for display
    assert snap.injection.formula is not None
    assert "Belpex_SPP" in snap.injection.formula


def test_fixed_shares_the_regulated_overlays() -> None:
    snap = _fixed_snap()
    assert snap.dsos == _var_snap().dsos
    assert snap.taxes.federal_excise == pytest.approx(4.8760 / 100.0)
    assert snap.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    assert snap.publication_label == "augustus 2026"
    assert snap.contract == "energiebe_fixed"


@pytest.mark.parametrize(
    ("text_fn", "contract", "match"),
    [
        ("var", "energiebe_fixed", "indexed \\(variable/dynamic\\) card"),
        ("dyn", "energiebe_fixed", "indexed \\(variable/dynamic\\) card"),
        ("fix", "energiebe_variable", "variable energy formula"),
        ("fix", "energiebe_dynamic", "energie.be energy formula"),
    ],
)
def test_a_fixed_card_and_an_indexed_card_are_not_interchangeable(
    text_fn: str, contract: str, match: str
) -> None:
    """The fixed card prints a RATE where the other two print a formula, and
    all three use the identical "Energieprijs" column label.

    So the rate alone cannot say which card this is: the guard is the absence
    of an indexation formula plus the card's own "vaste prijs" wording. Without
    it a fixed entry served the variable card would bill 15,98 c€/kWh as though
    it were locked, and a variable entry served the fixed card would find no
    formula at all.
    """
    text = {"var": _var_text, "dyn": _text, "fix": _fixed_text}[text_fn]()
    with pytest.raises(ExtractorError, match=match):
        parse_snapshot(text, "test://x", contract)


def test_fixed_contract_is_registered() -> None:
    contracts = {c.id: c.kind for c in EXTRACTORS["energiebe"].contracts}
    assert contracts["energiebe_fixed"] == "fixed"
    assert {c.id for c in EXTRACTORS["energiebe"].contracts} == {
        "energiebe_dynamic",
        "energiebe_variable",
        "energiebe_fixed",
    }
