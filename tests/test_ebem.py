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

"""EBEM PDF extractor tests against May 2026 fixtures."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers import ebem as ebem_mod
from custom_components.be_electricity_prices.providers._ebem_cards import (
    _extract_energy,
    _extract_injection,
)
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import ExtractorError
from custom_components.be_electricity_prices.providers.ebem import (
    _extract_validity,
    _settled_on,
    discover,
    fetch,
    fetch_for_month,
    parse_snapshot,
    published_index,
)
from tests import FIXTURES, fixture_text, make_text_session

_VARIABLE = "ebem_variable_2026-05.pdf"
_DYNAMIC = "ebem_dynamic_2026-05.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


def test_validity_scans_past_colliding_version_token() -> None:
    # The dynamic card header carries a colliding "VERSIE 2026" token; the
    # validity parser must skip it and bind to the real month line.
    assert _extract_validity("EBEM Dynamic VERSIE 2026\nmei 2026\n") == date(
        2026, 5, 31
    )


# ---- registry ---------------------------------------------------------------


def test_ebem_is_registered() -> None:
    assert "ebem" in EXTRACTORS
    extractor = EXTRACTORS["ebem"]
    assert extractor.label == "EBEM"
    assert {c.id for c in extractor.contracts} == {
        "ebem_variable",
        "ebem_basic_plus",
        "ebem_dynamic",
    }
    # EBEM only sells in Flanders today; the registry must not advertise
    # the other regions or config_flow would offer EBEM to households
    # where every fetch would 404.
    for contract in extractor.contracts:
        assert contract.regions == frozenset({"flanders"})


# ---- Groen Variabel ---------------------------------------------------------


def test_variable_extracts_energy_for_each_meter_type() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    assert isinstance(snap.energy, VariableRates)
    # PDF prints both excl-VAT and incl-VAT columns; we surface incl-VAT.
    # mono: (0,110 BelpexRLP0 + 2,2) at last-month Belpex 85,80 incl 6% VAT
    # = (0.110*85.80 + 2.2)*1.06 = 12.3363 c€/kWh = 0.123363 EUR/kWh
    assert snap.energy.current == pytest.approx(0.123363, rel=1e-4)
    # peak: (0,120 BelpexRLP0 + 2,2)
    assert snap.energy.peak == pytest.approx(0.132458, rel=1e-4)
    # off-peak / excl-night: (0,099 BelpexRLP0 + 2,2)
    assert snap.energy.offpeak == pytest.approx(0.113359, rel=1e-4)
    assert snap.energy.exclusive_night == pytest.approx(0.113359, rel=1e-4)
    # Yearly fee: 85,00 €/jaar incl-VAT (the registry stores incl-VAT).
    assert snap.energy.yearly_fixed_fee == pytest.approx(85.0)
    # The card also prints a dedicated exclusive-night fixed fee
    # (35,04 €/jaar incl-VAT) billed instead of the standard one on an
    # exclusive-night meter.
    assert snap.energy.yearly_fixed_fee_exclusive_night == pytest.approx(35.04)
    assert snap.energy.formula is not None
    assert "BelpexRLP0" in snap.energy.formula
    # Signing-cohort coefficients from the mono row, VAT-baked to EUR/kWh:
    # 0,110 * 1.06 * 10 and 2,2 * 1.06 / 100. Applying them to the card's
    # stated last-month Belpex (85,80 -> 0.0858 EUR/kWh) reproduces `current`.
    factor = snap.energy.formula_factor
    base = snap.energy.formula_base
    assert factor is not None and base is not None
    assert factor == pytest.approx(1.166, rel=1e-4)
    assert base == pytest.approx(0.02332, rel=1e-4)
    assert factor * 0.0858 + base == pytest.approx(snap.energy.current, rel=1e-3)


def test_variable_publication_and_validity() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    assert snap.publication_label == "2026-05"
    # mei 2026 -> last day = 31 May 2026 (Sunday)
    assert snap.valid_until == date(2026, 5, 31)


def test_variable_extracts_taxes_flanders_only() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    # Federal excise residential 0-3 MWh band: 5,0329 c€/kWh
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    # Energy contribution: 0,20417 c€/kWh
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    # Flanders renewables (groene stroom + WKK incl-VAT total): 1,6112 c€/kWh
    assert snap.taxes.flanders_renewables == pytest.approx(0.016112)
    # EBEM doesn't sell in Wallonia / Brussels: those overlays must stay 0.
    assert snap.taxes.wallonia_renewables == 0.0
    assert snap.taxes.brussels_renewables == 0.0
    assert snap.taxes.region_connection_fee == 0.0
    # Residential energy-fund tariff is €0; non-residential is €10,07/maand
    # which we don't model.
    assert snap.taxes.energy_fund_eur_per_month == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_variable_extracts_flanders_dsos_with_prosumer() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    expected_keys = set(FLUVIUS_KEYS)
    assert set(snap.dsos) == expected_keys
    # Spot-check Fluvius Kempen (= fluvius_iveka in the integration's DSO
    # key namespace). The variable card publishes both digital and analog
    # meter rows; we surface the digital row plus the analog prosumer rate.
    iveka = snap.dsos["fluvius_iveka"]
    # Card prints '59,58' and '6,34' / '5,66' / '18,92' / '67,79' on the
    # digital + analog rows; values are rounded to 2 decimals upstream.
    assert iveka.capacity_eur_per_kw_year == pytest.approx(59.58)
    assert iveka.distribution_single == pytest.approx(0.0634)
    assert iveka.distribution_exclusive_night == pytest.approx(0.0566)
    assert iveka.data_management_per_year == pytest.approx(18.92)
    assert iveka.prosumer_eur_per_kva_year == pytest.approx(67.79)


def test_a_live_card_missing_an_area_is_refused(freezer: Any) -> None:
    """A live card whose table lost a row to a relabel is refused rather than
    adopted without that area, which left an entry on it failing every tick
    while the last good card was gone from the cache. The same card as a past
    month keeps its energy, which a contract signed then is billed on. As the
    running month's it is refused too, or the month walk would drop that
    month on the missing area where the last good card stands in."""
    text = _layout(_VARIABLE)
    relabelled = text.replace("Fluvius West", "Fluvius W.")
    assert relabelled != text, "the fixture stopped printing Fluvius West"
    with (
        patch(
            "custom_components.be_electricity_prices.providers.ebem._find_latest",
            new=AsyncMock(return_value=("test://v", "2026-05")),
        ),
        patch(
            "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
            new=AsyncMock(return_value=relabelled),
        ),
    ):
        with pytest.raises(
            ExtractorError, match=r"DSO rows not found for \['fluvius_west'\]"
        ):
            asyncio.run(fetch(None, "ebem_variable", "flanders"))  # type: ignore[arg-type]
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 5, 1),
            )
        )
        freezer.move_to("2026-05-20 12:00:00+02:00")
        running = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 5, 1),
            )
        )
    assert snap is not None
    assert isinstance(snap.energy, VariableRates)
    assert "fluvius_west" not in snap.dsos and "fluvius_antwerpen" in snap.dsos
    assert running is None


def test_variable_extracts_injection_formula() -> None:
    snap = parse_snapshot("ebem_variable", _layout(_VARIABLE), "test://v", "2026-05")
    inj = snap.injection
    assert inj is not None
    # The SPP0 index is MONTHLY, and the printed figure is computed from LAST
    # month's value of it (0,0925 x SPP0(April) 27,95 - 1,25 = 1,3354 c/kWh).
    # It is surfaced as `current`, the fallback for an entry with no Synergrid
    # profile, alongside the coefficients. spp_indexed is what keeps those
    # coefficients away from the hourly spot and routes them to the delivery
    # month's own solar-weighted mean.
    assert inj.current == pytest.approx(0.013354, rel=1e-4)
    assert inj.spp_indexed is True
    assert inj.factor == pytest.approx(0.925)
    assert inj.base == pytest.approx(-0.0125)
    assert inj.formula is not None
    assert "BelpexSPP0" in inj.formula


def test_variable_injection_missing_indicative_raises() -> None:
    """When the monthly indicative is absent the SPP0 coefficients can't
    be priced (they weight the monthly average, not the hourly spot), so
    fail loud instead of emitting a spot-shaped credit."""
    text = "Injectie alle uren 0,0925 Belpex - 1,25\n"
    with pytest.raises(ExtractorError, match="indicative"):
        _extract_injection(text, "ebem_variable")


def test_missing_injection_row_is_fatal() -> None:
    # A fully-absent injection row (e.g. a capitalization re-render) must
    # raise rather than return None and silently zero the feed-in credit.
    with pytest.raises(ExtractorError, match=r"variable injection: .* not found"):
        _extract_injection("no injection here", "ebem_variable")
    with pytest.raises(ExtractorError, match=r"dynamic injection: .* not found"):
        _extract_injection("no injection here", "ebem_dynamic")


# ---- Groen B@sic+ -----------------------------------------------------------


def test_basic_plus_is_variable_with_single_rate() -> None:
    snap = parse_snapshot("ebem_basic_plus", _layout(_VARIABLE), "test://b", "2026-05")
    assert isinstance(snap.energy, VariableRates)
    # B@sic+ has one printed indicative rate: (0,110 BelpexRLP0 + 2,0) incl-VAT
    # at last-month Belpex 85,80 = 12,1243 c€/kWh.
    assert snap.energy.current == pytest.approx(0.121243, rel=1e-4)
    # Single-rate product: no peak / off-peak / excl-night split.
    assert snap.energy.peak is None
    assert snap.energy.offpeak is None
    assert snap.energy.exclusive_night is None
    # Yearly fee: 70 €/jaar incl-VAT (the 'Abonnement' row).
    assert snap.energy.yearly_fixed_fee == pytest.approx(70.0)
    # Single energy rate, but an exclusive-night meter still bills the card's
    # dedicated night fixed fee (35,04) rather than the 70 abonnement.
    assert snap.energy.yearly_fixed_fee_exclusive_night == pytest.approx(35.04)


def test_basic_plus_shares_pdf_with_variable_but_distinct_energy() -> None:
    """``Variabel`` and ``B@sic+`` live in the same elek PDF; verify the
    parser branches by contract id and gives each its own correct rate.
    """
    text = _layout(_VARIABLE)
    var = parse_snapshot("ebem_variable", text, "test://v", "2026-05")
    basic = parse_snapshot("ebem_basic_plus", text, "test://b", "2026-05")
    assert isinstance(var.energy, VariableRates)
    assert isinstance(basic.energy, VariableRates)
    # The two products differ in offset (2,2 vs 2,0) and yearly fee.
    assert var.energy.current != basic.energy.current
    assert var.energy.yearly_fixed_fee == 85.0
    assert basic.energy.yearly_fixed_fee == 70.0
    # But the DSO + tax overlays come from the same card and are byte-identical.
    assert var.dsos == basic.dsos
    assert var.taxes == basic.taxes


# ---- Groen Dyn@mic ----------------------------------------------------------


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    assert isinstance(snap.energy, DynamicRates)
    # Groen Dyn@mic bills on the 15-minute Belpex spot.
    assert snap.energy.quarter_hourly is True
    # Card formula: (0,108 Belpex15' + 1,625) c€/kWh ex-VAT, 6% VAT applied.
    # factor = 0.108 * 1.06 * 10 = 1.1448
    # base   = 1.625 * 1.06 / 100 = 0.017225
    assert snap.energy.factor == pytest.approx(1.1448, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.017225, rel=1e-4)
    # Dynamic card: 'Abonnement 66,04 €/jaar 70 €/jaar'.
    assert snap.energy.yearly_fixed_fee == pytest.approx(70.0)


def test_dynamic_extracts_injection_formula() -> None:
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    inj = snap.injection
    assert inj is not None
    # Card: 0,0925 Belpex15' - 1,10 c€/kWh ex-VAT (injection is VAT-exempt).
    assert inj.factor == pytest.approx(0.925, rel=1e-4)
    assert inj.base == pytest.approx(-0.011, rel=1e-4)
    assert inj.formula is not None and "Belpex15" in inj.formula


def test_dynamic_has_no_prosumer_column() -> None:
    """The dynamic card requires SMR3 (smart meter), so it omits the
    analog meter table and therefore the prosumer rate column.
    """
    snap = parse_snapshot("ebem_dynamic", _layout(_DYNAMIC), "test://d", "2026-05")
    iveka = snap.dsos["fluvius_iveka"]
    assert iveka.prosumer_eur_per_kva_year is None
    # Distribution rates still parse: that's the digital-meter row.
    assert iveka.distribution_single == pytest.approx(0.0634)


# The price each formula gives at the index its card prints.


def _edited(name: str, *edits: tuple[str, str]) -> str:
    """The card with each ``(old, new)`` of ``edits`` applied, every ``old``
    on it."""
    text = _layout(name)
    for old, new in edits:
        assert old in text, old
        text = text.replace(old, new)
    return text


def _restated(name: str, old: str, new: str) -> tuple[str, str]:
    """The card as printed, and the same card with every ``old`` reading ``new``."""
    return _layout(name), _edited(name, (old, new))


@pytest.mark.parametrize(
    ("contract", "old", "new", "refusal"),
    [
        # A factor or a base of each consumption row, ten times out.
        ("ebem_variable", "teller 0,110 Belpex", "teller 1,10 Belpex", "gives"),
        (
            "ebem_variable",
            "piek 0,120 Belpex + 2,2 ",
            "piek 0,120 Belpex + 22 ",
            "gives",
        ),
        ("ebem_variable", "dal 0,099 Belpex", "dal 0,99 Belpex", "gives"),
        (
            "ebem_variable",
            "nacht 0,099 Belpex + 2,2 ",
            "nacht 0,099 Belpex + 0,22 ",
            "gives",
        ),
        ("ebem_basic_plus", "uren 0,110 Belpex", "uren 1,10 Belpex", "gives"),
        # The feed-in row both contracts read.
        ("ebem_variable", "Belpex - 1,25 ", "Belpex - 12,5 ", "gives"),
        ("ebem_basic_plus", "uren 0,0925 Belpex", "uren 0,925 Belpex", "gives"),
        # Either index stated ten times smaller, as in c€/kWh, or not at all.
        ("ebem_variable", "deze index 85,80", "deze index 8,580", "gives"),
        ("ebem_basic_plus", "deze index 85,80", "deze index 8,580", "gives"),
        ("ebem_variable", "bedroeg 27,95", "bedroeg 2,795", "gives"),
        ("ebem_variable", "maand bedroeg deze", "maand was deze", "not printed"),
        # The prices headed in another unit, a formula restated with them.
        (
            "ebem_variable",
            "(type verbruik) (c€/kWh) (c€/kWh)",
            "(type verbruik) (€/MWh) (€/MWh)",
            "headed",
        ),
        (
            "ebem_basic_plus",
            "TARIEFFORMULE (c€/kWh) (c€/kWh)",
            "TARIEFFORMULE (€/MWh) (€/MWh)",
            "headed",
        ),
    ],
)
def test_a_variable_formula_that_misses_its_printed_price_is_refused(
    contract: str, old: str, new: str, refusal: str
) -> None:
    """No unit is printed for EBEM's formulas or for the index they multiply,
    but each row prints the price its formula gives at the index the card says
    last month closed at, under a "(c€/kWh)" heading: 0,110 x 85,80 + 2,2 is
    the 11,6380 excluding VAT beside the mono formula, 0,0925 x 27,95 - 1,25
    the 1,3354 beside the feed-in one. A formula, an index or a heading on
    another scale is refused as a row that does not read."""
    text, restated = _restated(_VARIABLE, old, new)
    snap = parse_snapshot(contract, text, "test://v", "2026-05")
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.formula_factor is not None
    assert snap.injection is not None and snap.injection.factor == pytest.approx(0.925)
    with pytest.raises(ExtractorError, match=refusal):
        parse_snapshot(contract, restated, "test://v", "2026-05")


@pytest.mark.parametrize(
    ("old", "new", "kept"),
    [
        # 0,08 EUR/MWh off, as April 2025's card was, then either side of 0,25.
        ("deze index 85,80", "deze index 85,88", True),
        ("deze index 85,80", "deze index 86,04", True),
        ("deze index 85,80", "deze index 86,06", False),
        ("bedroeg 27,95", "bedroeg 28,19", True),
        ("bedroeg 27,95", "bedroeg 28,21", False),
    ],
)
def test_a_variable_price_may_be_worked_out_a_quarter_euro_off_its_index(
    old: str, new: str, kept: bool
) -> None:
    """April 2025's card named 94,61 and worked its prices out at 94,69. A
    price is held to its formula at an index within 0,25 EUR/MWh of the one
    printed, on every row alike."""
    text = _edited(_VARIABLE, (old, new))
    if kept:
        snap = parse_snapshot("ebem_variable", text, "test://v", "2026-05")
        assert snap.injection is not None and snap.injection.spp_indexed
    else:
        with pytest.raises(ExtractorError, match="gives"):
            parse_snapshot("ebem_variable", text, "test://v", "2026-05")


@pytest.mark.parametrize(
    ("spp0", "price", "credit"),
    [
        # 0,0925 x 13,55 - 1,25 is 0,003375, printed to four decimals.
        ("13,55", "0,0034", 0.000034),
        # 0,0925 x 13,47 - 1,25 is -0,004025, under zero.
        ("13,47", "-0,0040", -0.00004),
        # Exact ties at the fourth decimal, printed either way.
        ("13,38", "-0,0123", -0.000123),
        ("13,38", "-0,0124", -0.000124),
        ("13,38", "−0,0124", -0.000124),
        ("13,66", "0,0136", 0.000136),
        ("13,54", "0,0025", 0.000025),
        # Worked out 0,08 EUR/MWh off a low SPP0: 0,0925 x 30,08 - 1,25.
        ("30,00", "1,5324", 0.015324),
    ],
)
def test_a_feed_in_price_near_or_under_zero_is_held_to_its_index(
    spp0: str, price: str, credit: float
) -> None:
    """In a month of low solar prices the feed-in formula comes out near zero
    or under it: 0,0925 x SPP0 - 1,25 is zero at 13,5 EUR/MWh, and May 2025
    closed at 15,08. Printed to four decimals, such a price is out by more
    than any share of it and lands on rounding ties, so it is held to its
    index rather than to a share of it, and read with its sign. A base ten
    times out is still refused."""
    text = _edited(
        _VARIABLE,
        ("bedroeg 27,95", f"bedroeg {spp0}"),
        ("Belpex - 1,25 1,3354 ", f"Belpex - 1,25 {price} "),
    )
    for contract in ("ebem_variable", "ebem_basic_plus"):
        snap = parse_snapshot(contract, text, "test://v", "2026-05")
        assert snap.injection is not None
        assert snap.injection.current == pytest.approx(credit)
        assert snap.injection.factor == pytest.approx(0.925)
    slipped = text.replace("Belpex - 1,25 ", "Belpex - 12,5 ")
    with pytest.raises(ExtractorError, match="gives"):
        parse_snapshot("ebem_variable", slipped, "test://v", "2026-05")


@pytest.mark.parametrize(
    ("old", "new", "refusal"),
    [
        ("uren 0,108 Belpex15’", "uren 1,08 Belpex15’", "gives"),
        ("Belpex15’ + 1,625 ", "Belpex15’ + 16,25 ", "gives"),
        ("uren 0,0925 Belpex15’", "uren 0,925 Belpex15’", "gives"),
        ("Belpex15’ - 1,10 ", "Belpex15’ - 11,0 ", "gives"),
        # Either base flipped, dropped or a tenth of itself.
        ("Belpex15’ + 1,625 ", "Belpex15’ - 1,625 ", "gives"),
        ("Belpex15’ + 1,625 ", "Belpex15’ + 0 ", "gives"),
        ("Belpex15’ + 1,625 ", "Belpex15’ + 0,1625 ", "gives"),
        ("Belpex15’ + 1,625 ", "Belpex15’ + 0,01625 ", "gives"),
        ("Belpex15’ - 1,10 ", "Belpex15’ + 1,10 ", "gives"),
        ("Belpex15’ - 1,10 ", "Belpex15’ - 0 ", "gives"),
        ("Belpex15’ - 1,10 ", "Belpex15’ - 0,110 ", "gives"),
        ("Belpex15’ - 1,10 ", "Belpex15’ - 0,0110 ", "gives"),
        # The regulator's means the prices are worked out at, ten times smaller.
        ("gemiddeldes: 96,48305", "gemiddeldes: 9,648305", "gives"),
        ("en 89,87334 voor", "en 8,987334 voor", "gives"),
        ("voor de afname en", "voor afname en", "not printed"),
        # Prices headed in another unit.
        ("TARIEFFORMULE (c€/kWh) (c€/kWh)", "TARIEFFORMULE (€/MWh) (€/MWh)", "headed"),
        # One leg's prices left stale while the other moved on.
        ("Belpex15’ - 1,10 7,2133", "Belpex15’ - 1,10 8,5659", "gives"),
    ],
)
def test_a_dynamic_formula_that_misses_its_printed_price_is_refused(
    old: str, new: str, refusal: str
) -> None:
    """The dynamic card prints its prices at the regulator's monthly means it
    names: 0,108 x 96,48305 + 1,625 is the 12,0452 excluding VAT beside the
    consumption formula, 0,0925 x 89,87334 - 1,10 the 7,2133 beside the feed-in
    one. Both come back within their rounding on every card but one left
    stale, so a slip in one formula or one mean is refused, a base as much as
    a factor."""
    text, restated = _restated(_DYNAMIC, old, new)
    snap = parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.1448)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(0.925)
    with pytest.raises(ExtractorError, match=refusal):
        parse_snapshot("ebem_dynamic", restated, "test://d", "2026-05")


@pytest.mark.parametrize(
    ("edits", "kept"),
    [
        # April's prices beside May's means, as February 2026 kept January's.
        (
            (("12,0452 12,7679", "13,5860 14,4012"), ("1,10 7,2133", "1,10 8,5659")),
            True,
        ),
        # April's means beside March's prices, a move of 1,43 times.
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 110,75016"),
                ("en 89,87334 voor", "en 104,4958 voor"),
                ("12,0452 12,7679", "10,1724 10,7827"),
                ("1,10 7,2133", "1,10 5,6430"),
            ),
            True,
        ),
        # May's prices beside means just under twice theirs, then just over.
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 192,96"),
                ("en 89,87334 voor", "en 179,74 voor"),
            ),
            True,
        ),
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 192,98"),
                ("en 89,87334 voor", "en 179,76 voor"),
            ),
            False,
        ),
        # And means just over half theirs, then just under.
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 48,25"),
                ("en 89,87334 voor", "en 44,94 voor"),
            ),
            True,
        ),
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 48,24"),
                ("en 89,87334 voor", "en 44,93 voor"),
            ),
            False,
        ),
        # Both means restated ten times smaller.
        (
            (
                ("gemiddeldes: 96,48305", "gemiddeldes: 9,648305"),
                ("en 89,87334 voor", "en 8,987334 voor"),
            ),
            False,
        ),
    ],
)
def test_a_dynamic_card_left_with_another_months_prices_stands(
    edits: tuple[tuple[str, str], ...], kept: bool
) -> None:
    """February 2026's card kept January's prices beside February's means, and
    both legs missed by about a tenth. A card missing on both legs stands
    while each price is its formula at an index within twice or half the
    printed mean; the means moved by 1,43 times at most between two months on
    file, and a unit restated misses by ten."""
    text = _edited(_DYNAMIC, *edits)
    if kept:
        snap = parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")
        assert isinstance(snap.energy, DynamicRates)
        assert snap.energy.factor == pytest.approx(1.1448)
        assert snap.injection is not None
        assert (snap.injection.factor, snap.injection.base) == pytest.approx(
            (0.925, -0.011)
        )
    else:
        with pytest.raises(ExtractorError, match="gives"):
            parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")


# April's prices beside May's means, the card left stale.
_DYNAMIC_STALE = (
    ("12,0452 12,7679", "13,5860 14,4012"),
    ("1,10 7,2133", "1,10 8,5659"),
)


@pytest.mark.parametrize(
    ("edits", "kept"),
    [
        (_DYNAMIC_STALE, True),
        # Both bases a tenth of themselves, a hundredth, dropped or flipped.
        ((("+ 1,625 ", "+ 0,1625 "), ("- 1,10 ", "- 0,110 ")), False),
        ((("+ 1,625 ", "+ 0,01625 "), ("- 1,10 ", "- 0,0110 ")), False),
        ((("+ 1,625 ", "+ 0 "), ("- 1,10 ", "- 0 ")), False),
        ((("+ 1,625 ", "- 1,625 "), ("- 1,10 ", "+ 1,10 ")), False),
        # The stale consumption price moved until the two legs sit 11,9
        # EUR/MWh apart off their means, then 12,1.
        (
            (("12,0452 12,7679", "12,3392 13,0796"), _DYNAMIC_STALE[1]),
            True,
        ),
        (
            (("12,0452 12,7679", "12,3176 13,0567"), _DYNAMIC_STALE[1]),
            False,
        ),
    ],
)
def test_a_dynamic_card_missing_on_both_legs_must_miss_alike(
    edits: tuple[tuple[str, str], ...], kept: bool
) -> None:
    """A card left stale prices both legs at one earlier month's means, which
    moved at most 5,48 EUR/MWh apart between two months on file, so both legs
    are worked out off their printed means by about the same amount. Both
    bases read wrong move the consumption leg one way and the feed-in leg the
    other, 24 EUR/MWh apart or more, and the card is refused although each leg
    stays within twice or half its mean."""
    text = _edited(_DYNAMIC, *edits)
    if kept:
        snap = parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")
        assert isinstance(snap.energy, DynamicRates)
        assert snap.energy.factor == pytest.approx(1.1448)
    else:
        with pytest.raises(ExtractorError, match="off the printed means"):
            parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")


@pytest.mark.parametrize(
    ("mean", "price"),
    [
        # 0,0925 x 11,892 - 1,10 is 0,00001, printed as zero.
        ("11,892", "0,0000"),
        # 0,0925 x 11,80 - 1,10 is -0,0085.
        ("11,80", "-0,0085"),
    ],
)
def test_a_dynamic_feed_in_price_near_or_under_zero_is_held_to_its_index(
    mean: str, price: str
) -> None:
    """The dynamic feed-in formula is zero at a monthly mean of 11,9 EUR/MWh.
    A price printed as zero, or under it, still reads, and a base ten times
    out is still refused."""
    text = _edited(
        _DYNAMIC,
        ("en 89,87334 voor", f"en {mean} voor"),
        ("Belpex15’ - 1,10 7,2133", f"Belpex15’ - 1,10 {price}"),
    )
    snap = parse_snapshot("ebem_dynamic", text, "test://d", "2026-05")
    assert snap.injection is not None
    assert (snap.injection.factor, snap.injection.base) == pytest.approx(
        (0.925, -0.011)
    )
    slipped = text.replace("Belpex15’ - 1,10 ", "Belpex15’ - 11,0 ")
    with pytest.raises(ExtractorError, match="gives"):
        parse_snapshot("ebem_dynamic", slipped, "test://d", "2026-05")


# ---- error handling ---------------------------------------------------------


def test_unknown_contract_raises() -> None:
    with pytest.raises(ExtractorError, match="unknown EBEM contract"):
        parse_snapshot("bogus", _layout(_VARIABLE), "test://x", "2026-05")


def test_missing_renewables_block_is_fatal() -> None:
    """The Bijdrage groene stroom + WKK total is mandatory on every EBEM
    card; a layout drift that wipes the 'incl. BTW <N>%' row would silently
    zero ~1.6 c€/kWh of bills without this guard.
    """
    text = _layout(_VARIABLE)
    truncated = text.replace("c€/kWh incl. BTW 6%", "c€/kWh excl. BTW 6%")
    with pytest.raises(ExtractorError, match=r"Totale bijdrage incl\. BTW"):
        parse_snapshot("ebem_variable", truncated, "test://v", "2026-05")


# ---- fetch_for_month --------------------------------------------------------


_LISTING_HTML = (FIXTURES / "discover" / "ebem.html").read_text(encoding="utf-8")


def test_fetch_for_month_returns_snapshot_when_listing_has_url() -> None:
    """May 2026 is on the listing fixture; with the parsed PDF text mocked
    in, ``fetch_for_month`` resolves the URL and parses cleanly."""
    text = _layout(_VARIABLE)
    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 5, 1),
            )
        )
    assert snap is not None
    assert snap.publication_label == "2026-05"
    assert isinstance(snap.energy, VariableRates)


def test_fetch_for_month_handles_underscore_separator() -> None:
    """EBEM's 2026-01 dynamic file is named ``ebem_tariefkaart-dynamic_01-2026.pdf``
    (underscore instead of dash between kind and MM). The regex must
    tolerate either separator so historic months don't silently fall
    through to a coordinator-side proxy."""
    text = _layout(_DYNAMIC)
    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_dynamic",
                "flanders",
                date(2026, 1, 1),
            )
        )
    # Validity-stamping: parsed PDF says 'mei 2026'. The cross-check in
    # fetch_for_month rejects a CDN-substituted current card for a past
    # month, so this returns None, which is the correct safety behaviour
    # (the URL was resolved, the underscore separator was handled, but
    # the served PDF was the wrong month).
    assert snap is None


def test_fetch_for_month_settles_a_closed_month_on_the_next_cards_index() -> None:
    """EBEM's card says the delivery month's index is not known when it is
    published, labels its columns "GESCHATTE", and gives the month just closed:
    "vorige maand bedroeg deze index 85,80". So the rate it prints is the
    formula at the PREVIOUS month, and the card of the following month is what
    settles this one.

    Measured over the nine archived 2026 cards, the printed rate for a month
    equalled the settled rate of the month before it to within 5e-7 every time,
    and a 3.500 kWh year-to-date ran 16,55 EUR under after eight months.

    Both texts served here are the same card, relabelled for April, because the
    fixtures hold one month. What that pins is the mechanism: the following
    month's footnote is read, and every register is rebuilt from its own
    coefficients rather than kept as printed.
    """
    may_text = _layout(_VARIABLE)
    april_text = may_text.replace("mei 2026", "april 2026").replace("E05", "E04")

    async def _read(_session: object, url: str, *a: object, **k: object) -> str:
        return april_text if "-04-2026" in url else may_text

    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=_read,
    ):
        april = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 4, 1),
            )
        )
    assert april is not None
    energy = april.energy
    assert isinstance(energy, VariableRates)
    # 85,80 EUR/MWh, the figure the May card publishes for April.
    assert energy.index_realised == pytest.approx(0.0858)
    assert energy.formula_factor is not None and energy.formula_base is not None
    assert energy.current == pytest.approx(
        energy.formula_factor * 0.0858 + energy.formula_base
    )
    # A month with a settling card behind it is a fact, not an estimate.
    assert april.provisional is False


def test_fetch_for_month_keeps_the_estimate_and_flags_it_until_the_next_card() -> None:
    """The running month has no card to settle it, so the printed estimate
    stands and the row is provisional: the monthly cache re-asks after its TTL
    instead of filing a forecast as a closed month's fact. The listing fixture
    ends at May, so May is that case."""
    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=AsyncMock(return_value=_layout(_VARIABLE)),
    ):
        may = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 5, 1),
            )
        )
    assert may is not None
    energy = may.energy
    assert isinstance(energy, VariableRates)
    assert may.provisional is True
    assert energy.index_realised is None
    # Untouched: still the card's own printed figure.
    assert energy.current == pytest.approx(0.123363)


def test_settling_rebuilds_every_register_from_its_own_pair() -> None:
    """The bi-hourly and exclusive-night rows carry their own coefficients, so
    settling only ``current`` would leave a bi-hourly meter on the estimate.
    Real May coefficients, real published index."""
    printed = parse_snapshot("ebem_variable", _layout(_VARIABLE), "u", "2026-05").energy
    assert isinstance(printed, VariableRates)
    index = published_index(_layout(_VARIABLE))
    assert index is not None and index == pytest.approx(0.0858)
    factor, base = printed.formula_factor, printed.formula_base
    assert factor is not None and base is not None
    settled = _settled_on(printed, index, factor, base)
    for value, pair_factor, pair_base in (
        (settled.current, printed.formula_factor, printed.formula_base),
        (settled.peak, printed.formula_factor_peak, printed.formula_base_peak),
        (settled.offpeak, printed.formula_factor_offpeak, printed.formula_base_offpeak),
        (
            settled.exclusive_night,
            printed.formula_factor_exclusive_night,
            printed.formula_base_exclusive_night,
        ),
    ):
        assert pair_factor is not None and pair_base is not None
        assert value == pytest.approx(pair_factor * index + pair_base)
    # The three bands really are distinct, or the loop above proves nothing.
    assert len({settled.current, settled.peak, settled.offpeak}) == 3


def test_fetch_for_month_returns_none_when_listing_has_no_match() -> None:
    """If the listing doesn't carry the requested month, return None so
    the coordinator falls back to the current snapshot as a proxy."""
    snap = asyncio.run(
        fetch_for_month(
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
            "ebem_dynamic",
            "flanders",
            date(2024, 7, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    snap = asyncio.run(
        fetch_for_month(
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
            "bogus",
            "flanders",
            date(2026, 5, 1),
        )
    )
    assert snap is None


def test_discover_drops_non_electricity_kinds() -> None:
    """EBEM publishes gas tariff cards on the same listing page; this
    integration is electricity-only, so discover() must skip them
    rather than surface 'gas' as an unknown contract id and trip the
    catalog-drift alert."""
    listing = (
        '<a href="/media/abc/ebem_tariefkaart-elek-05-2026.pdf">elek</a>'
        '<a href="/media/def/ebem_tariefkaart-gas-05-2026.pdf">gas</a>'
        '<a href="/media/ghi/ebem_tariefkaart-aardgas-05-2026.pdf">aardgas</a>'
    )
    out = asyncio.run(discover(make_text_session(listing)))  # type: ignore[arg-type]
    assert "gas" not in out
    assert "aardgas" not in out
    assert {"ebem_variable", "ebem_basic_plus"} <= out


def test_injection_resolves_the_delivery_month_not_last_month() -> None:
    """The card indexes the credit on BelpexSPP0, the solar-weighted monthly
    mean, and prints a figure computed from LAST month's value of it: "de SPP0
    vorige maand bedroeg 27,95" on the May card.

    That printed 1,3354 c/kWh is exactly 0,0925 x 27,95 - 1,25, i.e. April's
    index. May settled at SPP0 42,37 and 2,6692 c/kWh, so crediting the printed
    figure paid half of what the contract owed."""
    from custom_components.be_electricity_prices.injection import (
        _historical_injection_rate,
        _injection_is_spot_formula,
    )

    for contract in ("ebem_variable", "ebem_basic_plus"):
        snap = parse_snapshot(contract, _layout(_VARIABLE), "test://v", "2026-05")
        inj = snap.injection
        assert inj is not None, contract
        assert inj.spp_indexed is True
        assert inj.factor == pytest.approx(0.925)
        assert inj.base == pytest.approx(-0.0125)

        # An SPP formula must never be resolved against an hourly spot.
        assert _injection_is_spot_formula(inj, snap.energy) is False

        # The printed indicative IS April's index, which is the defect.
        assert inj.current == pytest.approx(0.013354)
        assert _historical_injection_rate(inj, 0.02795) == pytest.approx(
            0.013354, abs=1e-6
        )
        # May's own index is what May is credited at: twice as much.
        assert _historical_injection_rate(inj, 0.04237) == pytest.approx(
            0.026692, abs=1e-6
        )


def test_variable_cohort_carries_a_formula_per_meter() -> None:
    """All four rows were parsed and then dropped, so a bi-hourly cohort was
    billed the mono pair around the clock: at the card's own May index that is
    7,0% low at peak and 8,9% high off-peak.

    The sharpest part is that the SAME entry with no start date already billed
    both bands correctly off the printed columns, so setting a start date -
    the feature meant to price a cohort more accurately - destroyed the split.
    """
    from custom_components.be_electricity_prices.providers._rates import VariableRates

    snap = parse_snapshot(
        "ebem_variable",
        fixture_text("ebem_variable_2026-05.pdf", layout=True),
        "t://v",
        "2026-05",
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    # c/kWh per EUR/MWh of index, grossed to the card's INCL. BTW 6% basis.
    assert energy.formula_factor == pytest.approx(0.110 * 10 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(0.120 * 10 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.099 * 10 * 1.06)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.099 * 10 * 1.06)


def test_basic_plus_keeps_no_band_formulas() -> None:
    """B@sic+ prints ONE formula for every meter, "Verbruik alle uren 0,110
    Belpex + 2,0", and mentions exclusive night only as a fee.

    The trap is concrete rather than hypothetical: both cards live in one PDF,
    so the text handed to the B@sic+ branch contains the Groen Variabel rows
    too, and searching it for "Exclusief nacht" finds the OTHER product's
    coefficients. Its branch must stay where it is and read none of them.
    """
    from custom_components.be_electricity_prices.providers._rates import VariableRates

    snap = parse_snapshot(
        "ebem_basic_plus",
        fixture_text("ebem_variable_2026-05.pdf", layout=True),
        "t://b",
        "2026-05",
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.formula_factor_peak is None
    assert energy.formula_factor_offpeak is None
    assert energy.formula_factor_exclusive_night is None


def test_the_variable_energy_leg_is_indexed_on_the_delivery_month() -> None:
    """EBEM's card states the delivery month's index is not known while the
    month runs, prints its price columns as "GESCHATTE", and gives the month
    just closed. So the printed rate is the formula at the PREVIOUS month, and
    for the running month the delivery month's own weighted mean is the better
    estimate of what will be billed: 15,62 EUR/MWh from it on average against
    1,61 for the blend, about 64 EUR a year at 3.500 kWh against 7.

    The printed figure survives as ``current``, which is what an entry with no
    ENTSO-E key is billed, and a closed month never uses either: the following
    card publishes what the month actually settled at.

    The blend is the one value here that is a fit rather than a settlement,
    pinned so a later change to it is a decision rather than a drift.
    """
    for cid, row in (
        ("ebem_variable", "Enkelvoudige teller"),
        ("ebem_basic_plus", "Verbruik alle uren"),
    ):
        energy = parse_snapshot(cid, _layout(_VARIABLE), "u", "2026-05").energy
        assert isinstance(energy, VariableRates), cid
        assert energy.month_indexed is True, cid
        assert energy.rlp_indexed is True, cid
        assert energy.rlp_blend == "flanders", cid
        # The card's own printed estimate is kept as the keyless fallback.
        assert energy.current == pytest.approx(
            0.123363 if cid == "ebem_variable" else 0.121243
        ), cid
        assert energy.formula_factor is not None and energy.formula_base is not None
        assert row in _layout(_VARIABLE)

    # And the registry agrees, which is what offers the key in the config flow
    # and engages the re-price: the two halves have drifted apart before.
    contracts = {c.id: c for c in EXTRACTORS["ebem"].contracts}
    assert contracts["ebem_variable"].month_indexed_energy is True
    assert contracts["ebem_basic_plus"].month_indexed_energy is True
    assert contracts["ebem_dynamic"].month_indexed_energy is False


def test_fetch_for_month_settles_the_feed_in_leg_on_the_next_cards_spp0() -> None:
    """The credit is indexed on SPP0, the solar-weighted monthly mean, and the
    card prints the figure for the month BEFORE it the same way the
    consumption side does: "de SPP0 vorige maand bedroeg 27,95". So the
    following month's card settles this month's credit too.

    Until now only the consumption leg was settled and the credit was left to
    the SPP-weighted mean computed from the spot cache. That mean is close but
    biased: hour by hour it weights the hour's MEAN price by the hour's solar
    share, while the published index weights each quarter by its own, and over
    January to August 2026 it came out about 0,9 EUR/MWh above every published
    SPP0, always in the same direction. Trevion publishes the identical 79,11
    for August 2026, so the figure is the market's and not EBEM's.

    The April text here carries a DIFFERENT SPP0 from the May one, so what the
    assertions separate is the printed estimate from the settled figure.
    """
    may_text = _layout(_VARIABLE)
    april_text = (
        may_text.replace("mei 2026", "april 2026")
        .replace("E05", "E04")
        # March's index, and the indicative April printed from it:
        # 0,0925 x 19,00 - 1,25 = 0,5075 c/kWh.
        .replace("bedroeg 27,95", "bedroeg 19,00")
        .replace("Belpex - 1,25 1,3354", "Belpex - 1,25 0,5075")
    )

    printed = parse_snapshot("ebem_variable", april_text, "test://v", "2026-04")
    assert printed.injection is not None
    assert printed.injection.current == pytest.approx(0.005075)
    assert printed.injection.index_realised is None

    async def _read(_session: object, url: str, *a: object, **k: object) -> str:
        return april_text if "-04-2026" in url else may_text

    with patch(
        "custom_components.be_electricity_prices.providers.ebem.fetch_pdf_text_layout",
        new=_read,
    ):
        april = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "ebem_variable",
                "flanders",
                date(2026, 4, 1),
            )
        )
    assert april is not None
    inj = april.injection
    assert inj is not None
    # 27,95 EUR/MWh, the SPP0 the May card publishes for April.
    assert inj.index_realised == pytest.approx(0.02795)
    assert inj.factor is not None and inj.base is not None
    assert inj.current == pytest.approx(inj.factor * 0.02795 + inj.base)
    # Which is the 1,3354 c/kWh the May card prints, to its own four decimals,
    # and not the 0,5075 April printed.
    assert inj.current == pytest.approx(0.013354, abs=5e-7)


def test_the_settled_feed_in_index_is_what_the_pricing_engine_bills() -> None:
    """A settled month must not be re-derived from the spot cache.

    ``_spp_injection_spot`` is the one place the live tick, the year-to-date
    walk and the backfill all resolve a mean-indexed credit, so the published
    figure short-circuits it there and all three bill the same number. Without
    that the field would ride along on the snapshot and change nothing.
    """
    from custom_components.be_electricity_prices.spot_stats import _spp_injection_spot

    cache: dict[tuple[int, int, bool], float | None] = {}
    kwargs: dict[str, object] = {
        "monthly_mean": True,
        "spp_weights": {(4, 1, 12): 1.0},
        "bucket": {},
        "year": 2026,
        "month": 4,
        "today": date(2026, 5, 15),
        "cache": cache,
        "strict": True,
    }
    # With nothing settled and no usable bucket, a strict SPP formula answers
    # "no spot" and the caller falls back to the card's printed indicative.
    assert _spp_injection_spot(None, **kwargs) is None  # type: ignore[arg-type]
    # With the month settled, that is the number, whatever the cache holds.
    assert _spp_injection_spot(
        None,
        index_realised=0.02795,
        **kwargs,  # type: ignore[arg-type]
    ) == pytest.approx(0.02795)


async def test_probe_keys_on_the_card_the_fetcher_resolves() -> None:
    """The listing sends neither Last-Modified nor ETag, so a header probe
    always answered None and a new card waited on the 24 h TTL. The key is
    the card URL the fetcher picks, whose media hash changes per upload."""
    from custom_components.be_electricity_prices.providers.ebem import probe

    session = make_text_session(_LISTING_HTML)
    elek = await probe(session, "ebem_variable", "flanders")
    assert elek is not None
    assert elek.startswith("https://www.ebem.be/media/")
    assert "elek" in elek
    assert await probe(session, "ebem_basic_plus", "flanders") == elek
    dynamic = await probe(session, "ebem_dynamic", "flanders")
    assert dynamic is not None and "dynamic" in dynamic
    assert await probe(make_text_session(""), "ebem_variable", "flanders") is None
    assert await probe(session, "unknown", "flanders") is None


_VARIABLE_CONTRACT = ebem_mod._CONTRACTS_BY_ID["ebem_variable"]


def _quietly(read: Callable[[], object]) -> Callable[[], None]:
    """``read``, whatever it makes of a card it cannot parse."""

    def call() -> None:
        with contextlib.suppress(ExtractorError):
            read()

    return call


# Card text a host could serve instead, about 100,000 characters each, on
# which a pattern was tried again from every place it could start: a run of
# blanks after the dynamic formula's index, labels of the protected-customer
# row with no figure after them, and analog-meter headings with no digital
# one after them.
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            _quietly(
                lambda: _extract_energy(
                    "alle uren 1,0 Belpex15 " + "\xa0" * 100_000, "ebem_dynamic"
                )
            ),
            id="energy formula",
        ),
        pytest.param(
            _quietly(
                lambda: _extract_injection(
                    "injectie alle uren 1,0 Belpex15" + "\n" * 100_000,
                    "ebem_dynamic",
                )
            ),
            id="injection formula",
        ),
        pytest.param(
            _quietly(
                lambda: ebem_mod._extract_federal_taxes(
                    "Beschermende klanten €0 \n" * 4_000
                )
            ),
            id="contribution",
        ),
        pytest.param(
            _quietly(
                lambda: ebem_mod._extract_dsos(
                    "DIGITALE METER\n" + "ANALOGE METER " * 7_000, _VARIABLE_CONTRACT
                )
            ),
            id="analog table",
        ),
    ],
)
def test_a_crafted_card_is_read_in_time_linear_in_its_length(
    read: Callable[[], object],
) -> None:
    started = time.perf_counter()
    read()
    assert time.perf_counter() - started < 2.0
