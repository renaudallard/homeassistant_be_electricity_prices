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

"""Fixture-based tests for the Frank Energie extractor."""

from __future__ import annotations

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.frank import (
    _matches_suffix,
    parse_snapshot,
)
from tests import fixture_text


def _text() -> str:
    return fixture_text("frank_dynamic_apr.pdf", layout=True)


def _snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text(),
        "test://frank-apr",
        "frank_dynamic",
        "april 2026",
    )


def test_energy_is_dynamic_rates() -> None:
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)


def test_energy_formula_factor() -> None:
    """(0,1068 x BELPEX + 1,500) x 1,06 => factor = 0.1068 * 1.06 * 10."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(0.1068 * 1.06 * 10.0)


def test_energy_formula_base() -> None:
    """base = 1.500 * 1.06 / 100."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.base == pytest.approx(1.500 * 1.06 / 100.0)


def test_yearly_fixed_fee() -> None:
    """2,92 EUR/month => 35.04 EUR/year."""
    snap = _snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(2.92 * 12.0)


def test_dsos_cover_all_eight_fluvius_subareas() -> None:
    snap = _snap()
    expected = set(FLUVIUS_KEYS)
    assert set(snap.dsos) == expected


def test_dso_antwerpen_distribution() -> None:
    """Antwerpen digital meter: normaal 5,35 ct/kWh, excl nacht 4,81 ct/kWh."""
    snap = _snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(5.35 / 100.0)
    assert a.distribution_exclusive_night == pytest.approx(4.81 / 100.0)


def test_dso_antwerpen_capacity_and_databeheer() -> None:
    snap = _snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.capacity_eur_per_kw_year == pytest.approx(52.37)
    assert a.data_management_per_year == pytest.approx(18.92)


def test_dso_transport_is_zero() -> None:
    """Transport is bundled into distribution on Frank Energie's card."""
    snap = _snap()
    for overlay in snap.dsos.values():
        assert overlay.transport == 0.0


def test_dso_kempen_despite_bracket_artifact() -> None:
    """The PDF renders 'Fluvius [Kempen)' with a mismatched bracket."""
    snap = _snap()
    assert "fluvius_iveka" in snap.dsos
    k = snap.dsos["fluvius_iveka"]
    assert k.distribution_single == pytest.approx(6.34 / 100.0)


def test_taxes_federal_excise() -> None:
    """5,0328 EURct/kWh VAT-inclusive."""
    snap = _snap()
    assert snap.taxes.federal_excise == pytest.approx(5.0328 / 100.0)


def test_taxes_energy_contribution() -> None:
    snap = _snap()
    assert snap.taxes.energy_contribution == pytest.approx(0.2042 / 100.0)


def test_dot_decimal_render_matches_comma() -> None:
    # A dot-decimal PDF re-render must extract identical values, not
    # truncate a mandatory tax row / the VAT multiplier to the integer
    # part as the comma-only regex did.
    comma = _snap()
    dot = parse_snapshot(
        _text().replace(",", "."), "test://frank-apr", "frank_dynamic", "april 2026"
    )
    assert dot.taxes.energy_contribution == pytest.approx(
        comma.taxes.energy_contribution
    )
    assert dot.taxes.federal_excise == pytest.approx(comma.taxes.federal_excise)
    assert isinstance(dot.energy, DynamicRates)
    assert isinstance(comma.energy, DynamicRates)
    assert dot.energy.factor == pytest.approx(comma.energy.factor)
    assert dot.energy.base == pytest.approx(comma.energy.base)


def test_august_card_drops_the_energy_contribution_row() -> None:
    # The federal "bijdrage op de energie" fell to zero on 2026-08-01 and
    # Frank deleted the row from the card. Parsing must still succeed and
    # report a zero contribution instead of taking the supplier offline.
    snap = parse_snapshot(
        fixture_text("frank_dynamic_aug.pdf", layout=True),
        "test://frank-aug",
        "frank_dynamic",
        "augustus 2026",
    )
    assert "Bijdrage op Energie" not in fixture_text(
        "frank_dynamic_aug.pdf", layout=True
    )
    assert snap.taxes.energy_contribution == 0.0
    # The excise moved in the same reform and is still billed.
    assert snap.taxes.federal_excise == pytest.approx(4.876 / 100.0)
    assert snap.taxes.flanders_renewables == pytest.approx((1.166 + 0.371) / 100.0)


def test_missing_federal_excise_is_fatal() -> None:
    # The excise is still billed, so a miss there is a real layout drift
    # and must not be softened along with the abolished contribution.
    text = _text().replace("Bijzondere accijns", "XXX")
    with pytest.raises(ExtractorError, match="tax block"):
        parse_snapshot(text, "test://frank-apr", "frank_dynamic", "april 2026")


def test_missing_monthly_fee_is_fatal() -> None:
    # The Abonnementskost standing charge is mandatory; a miss must raise.
    text = _text().replace("Abonnementskost", "XXX")
    with pytest.raises(ExtractorError, match="monthly fixed fee"):
        parse_snapshot(text, "test://frank-apr", "frank_dynamic", "april 2026")


def test_missing_gsc_wkk_is_fatal() -> None:
    # Frank is Flanders-only, so GSC + WKK are mandatory; a miss must
    # raise rather than silently zero the renewables levy.
    text = _text().replace("GSC", "XXX").replace("WKK", "YYY")
    with pytest.raises(ExtractorError, match="GSC/WKK"):
        parse_snapshot(text, "test://frank-apr", "frank_dynamic", "april 2026")


def test_missing_injection_is_fatal() -> None:
    # Every Frank dynamic card prints the terugleveringsvergoeding; a
    # miss must raise rather than silently zero the solar feed-in credit.
    text = _text().replace("rugleveringsvergoeding", "XXX")
    with pytest.raises(ExtractorError, match="injection formula"):
        parse_snapshot(text, "test://frank-apr", "frank_dynamic", "april 2026")


def test_taxes_flanders_renewables_gsc_plus_wkk() -> None:
    """GSC 1,166 + WKK 0,371 = 1,537 EURct/kWh."""
    snap = _snap()
    assert snap.taxes.flanders_renewables == pytest.approx((1.166 + 0.371) / 100.0)


def test_taxes_energy_fund_residential_zero() -> None:
    snap = _snap()
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(0.0)


def test_taxes_vat_rate_zero() -> None:
    """All values on the card are already VAT-inclusive."""
    snap = _snap()
    assert snap.taxes.vat_rate == 0.0


def test_injection_factor() -> None:
    """(0,1 x BELPEX - 1,150) VAT-exempt => factor = 0.1 * 10 = 1.0."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)


def test_injection_base() -> None:
    """base = -1.150 / 100 = -0.01150."""
    snap = _snap()
    assert snap.injection is not None
    assert snap.injection.base == pytest.approx(-1.150 / 100.0)


@pytest.mark.parametrize(
    ("contract_id", "fixture", "factor", "base", "fee", "inj_base"),
    [
        (
            "frank_dynamic_hv",
            "frank_dynamic_hv_jun.pdf",
            1.0812,
            0.0053,
            102.0,
            -0.0115,
        ),
        (
            "frank_dynamic_korting",
            "frank_dynamic_korting_jun.pdf",
            1.13208,
            0.0159,
            35.04,
            -0.0115,
        ),
        ("frank_dynamic_jn", "frank_dynamic_jn_jun.pdf", 1.113, 0.01272, 27.96, -0.02),
        (
            "frank_dynamic_slim",
            "frank_dynamic_slim_may.pdf",
            1.13208,
            0.0159,
            35.04,
            -0.0115,
        ),
    ],
)
def test_non_default_tiers_extract_energy_and_injection(
    contract_id: str,
    fixture: str,
    factor: float,
    base: float,
    fee: float,
    inj_base: float,
) -> None:
    # The five Frank tiers share one PDF layout, but only the default tier
    # had a fixture. Pin the other four tiers' energy + injection so a
    # tier-specific card regression is caught. The JN tier notably carries
    # a different injection base (-0,02 vs -0,0115 on the rest).
    snap = parse_snapshot(
        fixture_text(fixture, layout=True), "test://frank", contract_id, "test"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(factor)
    assert snap.energy.base == pytest.approx(base)
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)
    assert snap.injection.base == pytest.approx(inj_base)


def test_supplier_and_contract_metadata() -> None:
    snap = _snap()
    assert snap.supplier == "frank"
    assert snap.contract == "frank_dynamic"
    assert snap.publication_label == "april 2026"


def test_valid_until_is_end_of_april() -> None:
    snap = _snap()
    assert snap.valid_until is not None
    assert snap.valid_until.month == 4
    assert snap.valid_until.year == 2026


# ---- registration ---------------------------------------------------------------


def test_frank_is_registered() -> None:
    assert "frank" in EXTRACTORS
    assert EXTRACTORS["frank"].label == "Frank Energie"
    contract_ids = {c.id for c in EXTRACTORS["frank"].contracts}
    assert contract_ids == {
        "frank_dynamic",
        "frank_dynamic_hv",
        "frank_dynamic_korting",
        "frank_dynamic_jn",
        "frank_dynamic_slim",
    }


def test_all_contracts_are_dynamic_and_flanders_only() -> None:
    for c in EXTRACTORS["frank"].contracts:
        assert c.kind == "dynamic"
        assert c.regions == frozenset({"flanders"})


# ---- _matches_suffix -------------------------------------------------------------


def test_matches_suffix_standard_accepts_month_name() -> None:
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Mei 2026.pdf", None
    )


def test_matches_suffix_standard_rejects_tier_suffix() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch HV Mei 2026.pdf", None
    )


def test_matches_suffix_hv_accepts_hv() -> None:
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch HV Mei 2026.pdf", "HV"
    )


def test_matches_suffix_hv_rejects_standard() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Mei 2026.pdf", "HV"
    )


def test_matches_suffix_rejects_variable_filename() -> None:
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Variabel Mei 2026.pdf", None
    )


def test_matches_suffix_slim_accepts_both_sl_and_full_word() -> None:
    # Frank alternates the Slim tier token monthly: "Dynamisch SL Juli 2026"
    # vs "Dynamisch Slim Juni 2026". Both must match the SL tier or the tier
    # silently fails to fetch in the full-word months.
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch SL Juli 2026.pdf", "SL"
    )
    assert _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Slim Juni 2026.pdf", "SL"
    )
    # The full word must not leak into an unrelated tier or the standard tier.
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Slim Juni 2026.pdf", "HV"
    )
    assert not _matches_suffix(
        "Frank Energie Tariefkaart Elektriciteit Dynamisch Slim Juni 2026.pdf", None
    )
