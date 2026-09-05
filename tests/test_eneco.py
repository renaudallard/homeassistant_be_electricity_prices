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

"""Unit tests for the Eneco PDF extractor (run against fixture PDFs)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Coroutine
from datetime import date
from typing import Any, TypeVar
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.const import (
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_electricity_prices.providers import eneco as eneco_mod
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.eneco import (
    fetch_for_month,
    parse_snapshot,
)
from tests import fixture_text

_T = TypeVar("_T")


def test_power_dynamic_offered_in_flanders_only() -> None:
    # The Dynamic card is "voor Vlaanderen" and needs a Flemish SMR3
    # digital meter; Fix and Flex cover both regions. Dynamic must not be
    # offered to Wallonia users.
    regions = {c.id: set(c.regions) for c in eneco_mod.EXTRACTOR.contracts}
    assert regions["power_dynamic"] == {"flanders"}
    assert "wallonia" in regions["power_fix"]
    assert "wallonia" in regions["power_flex"]


def test_fix_extracts_energy_block() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1865)
    assert snap.energy.peak == pytest.approx(0.2055)
    assert snap.energy.offpeak == pytest.approx(0.1699)
    assert snap.energy.exclusive_night == pytest.approx(0.1699)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_fix_extracts_dso_overlay() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    # The Wallonia row's "Uitsl. nacht" column is now propagated as the
    # dedicated exclusive-night meter distribution rate; it happens to
    # match offpeak on this card, but the field carries the published
    # number rather than falling back to a different column.
    assert aieg.distribution_exclusive_night == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    # Wallonia DSOs publish a prosumer (compensation-regime) tariff in the
    # last column. AIEG row trails with "81,04" EUR/kVA/year.
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.04)


def test_fix_fluvius_has_no_prosumer_rate() -> None:
    # Flemish digital meter rows print "-" for the prosumer column - SMR3
    # connections don't sit under the compensation regime.
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    assert snap.dsos["fluvius_antwerpen"].prosumer_eur_per_kva_year is None


def test_fix_extracts_all_fluvius_sub_areas() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    expected_keys = set(FLUVIUS_KEYS)
    assert expected_keys <= set(snap.dsos)

    # Antwerpen is the digital-meter row "FLUVIUS ANTWERPEN 5,35 4,81 18,92
    # 18,92 52,37 - -" -> distribution 5.35 c/kWh, capacity 52.37 EUR/kW/yr.
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    # No peak/offpeak split for Flemish digital meters post-capacity-tariff.
    assert antwerpen.distribution_peak is None
    assert antwerpen.distribution_offpeak is None
    # Fluvius's second column ("Uitsl. nacht" 4,81 c/kWh) is the
    # dedicated exclusive-night meter circuit rate, distinct from the
    # day rate.
    assert antwerpen.distribution_exclusive_night == pytest.approx(0.0481)
    # The Flemish Afnametarief already bundles Elia transmission, so the
    # Fluvius overlay carries no separate transport (the Walloon
    # "Transport-kosten" column does not apply here).
    assert antwerpen.transport == 0.0
    assert antwerpen.data_management_per_year == pytest.approx(18.92)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_fix_fluvius_sub_areas_have_distinct_rates() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    rates = {
        key: snap.dsos[key].distribution_single
        for key in snap.dsos
        if key.startswith("fluvius_")
    }
    # Fluvius sub-areas publish materially different distribution rates;
    # if all eight collapsed to one value something is wrong upstream.
    assert len(set(rates.values())) > 1


def test_missing_connection_fee_is_fatal() -> None:
    # The Walloon connection fee is a mandatory all-in component with no
    # live_check gate; a regex miss must fail loud, not silently zero it.
    text = fixture_text("eneco_fix.pdf").replace(
        "Aansluitingsvergoeding elektriciteit", "REMOVED"
    )
    with pytest.raises(eneco_mod.ExtractorError, match="connection fee"):
        parse_snapshot(text, "power_fix", "test://fix", REGION_FLANDERS)


def test_energy_fund_is_flanders_only() -> None:
    """The Energiefonds table is headed "(Vlaanderen)" but rides on the one
    card Eneco serves to both regions, and fees.py bills 12 x the field with
    no region check of its own. Every issue published so far prints 0,00 in
    the domiciled low-voltage cell, so the real rate has to be patched in:
    against the real fixture both regions read 0,00 and the test proves
    nothing.
    """
    text = fixture_text("eneco_fix.pdf").replace(
        "(domicilieadres) 0,00", "(domicilieadres) 7,77"
    )
    flanders = parse_snapshot(text, "power_fix", "test://fix", REGION_FLANDERS)
    wallonia = parse_snapshot(text, "power_fix", "test://fix", REGION_WALLONIA)
    assert flanders.taxes.energy_fund_eur_per_month == pytest.approx(7.77)
    assert wallonia.taxes.energy_fund_eur_per_month == 0.0


def test_energy_fund_is_read_on_every_card_layout() -> None:
    """Only the Fix and Flex cards issued from January 2026 wrap between the
    label and "(domicilieadres)"; every 2025 issue and every Dynamic issue
    prints them on one line. Requiring the newline returned the 0.0 default
    for 45 of the 63 cards published since January 2025, which no test could
    see while that cell prints 0,00 on every one of them.

    The two archive fixtures carry the split: aug26 wraps, dec25 does not.
    """
    for fixture, contract in (
        ("eneco_fix.pdf", "power_fix"),
        ("eneco_flex.pdf", "power_flex"),
        ("eneco_dyn.pdf", "power_dynamic"),
        ("eneco_flex_aug26.pdf", "power_flex"),
        ("eneco_flex_dec25.pdf", "power_flex"),
    ):
        text = fixture_text(fixture).replace(
            "(domicilieadres) 0,00", "(domicilieadres) 7,77"
        )
        snap = parse_snapshot(text, contract, "test://x", REGION_FLANDERS)
        assert snap.taxes.energy_fund_eur_per_month == pytest.approx(7.77), fixture


def test_fix_extracts_taxes() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.energy_contribution == pytest.approx(0.002042)
    # Both regional rates are populated from the PDF; the pricing engine
    # picks the right one per region.
    assert snap.taxes.flanders_renewables == pytest.approx(0.0152)
    assert snap.taxes.wallonia_renewables == pytest.approx(0.0313)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.vat_rate == 0.0
    assert snap.publication_label.lower().startswith(
        (
            "januari",
            "februari",
            "maart",
            "april",
            "mei",
            "juni",
            "juli",
            "augustus",
            "september",
            "oktober",
            "november",
            "december",
        )
    )


def test_august_card_drops_the_energy_contribution_row() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_flex_aug26.pdf"),
        "power_flex",
        "test://fix",
        REGION_FLANDERS,
    )
    assert snap.taxes.federal_excise == pytest.approx(0.048760)
    assert snap.taxes.energy_contribution == pytest.approx(0.0)


def test_flex_extracts_current_monthly_rate() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_flex.pdf"), "power_flex", "test://flex", REGION_FLANDERS
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1390)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    assert snap.energy.formula is not None and "BELPEX" in snap.energy.formula


def test_flex_yearly_fee_survives_extra_header_line() -> None:
    # The fee anchor keys off the (€/jaar) header and the "Geschatte
    # jaarprijs" row, not a fixed newline count, so an extra header line
    # the supplier might insert between them must not break parsing.
    text = fixture_text("eneco_flex.pdf").replace(
        "DAG NACHT", "DAG NACHT\nEXTRA HEADER LINE", 1
    )
    snap = parse_snapshot(text, "power_flex", "test://flex", REGION_FLANDERS)
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)


def test_num_parses_thousands_grouped_and_four_digit_values() -> None:
    # _NUM previously capped the integer part at three digits, truncating
    # any value >= 1000. A non-breaking-space-grouped fee and an
    # ungrouped four-digit value must now parse to their full magnitude.
    from custom_components.be_electricity_prices.providers._pdf import to_float

    pattern = re.compile(eneco_mod._NUM)
    # Grouping uses a non-breaking space (U+00A0); ordinary ASCII
    # spaces stay column separators, so the two forms that must
    # round-trip to the full value are NBSP-grouped and ungrouped.
    for text, expected in (("1\u00a0200,00", 1200.0), ("1200,00", 1200.0)):
        m = pattern.search(text)
        assert m is not None
        assert to_float(m.group(1)) == pytest.approx(expected)


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn", REGION_FLANDERS
    )
    assert isinstance(snap.energy, DynamicRates)
    # PDF formula: (0.102 x BELPEX-H_eur_per_mwh + 1) x 1.06  c€/kWh
    # ENTSO-E client gives spot in EUR/kWh, so the integration uses:
    #   energy_eur_per_kwh = factor * spot_eur_per_kwh + base
    # Literal pinning: `0.102 * 10.6` is exactly what the parser
    # computes; pinning the literal 1.0812 catches a unit-conversion
    # bug that would otherwise cancel.
    assert snap.energy.factor == pytest.approx(1.0812)
    assert snap.energy.base == pytest.approx(0.0106)
    assert snap.energy.yearly_fixed_fee == pytest.approx(100.0)
    # Realism check: at 100 EUR/MWh spot, all-in energy is ~0.119 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.11872)


def test_dynamic_publication_label_present() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn", REGION_FLANDERS
    )
    assert snap.publication_label  # non-empty


def test_extracts_valid_until_from_geldig_line() -> None:
    """Eneco's April-2026 cards print "Geldig van 1 april 2026 t.e.m
    30 april 2026"; the snapshot must surface the end date so the
    tomorrow_prices_available binary sensor flips OFF on April 30."""
    from datetime import date

    for fixture, contract in (
        ("eneco_fix.pdf", "power_fix"),
        ("eneco_flex.pdf", "power_flex"),
        ("eneco_dyn.pdf", "power_dynamic"),
    ):
        snap = parse_snapshot(
            fixture_text(fixture), contract, f"test://{fixture}", REGION_FLANDERS
        )
        assert snap.valid_until == date(2026, 4, 30), fixture


def test_fix_extracts_injection_rates() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_fix.pdf"), "power_fix", "test://fix", REGION_FLANDERS
    )
    inj = snap.injection
    assert inj is not None
    # Power Fix prints "Maandprijs 4,76 c/kWh" and a MONTHLY Belpex-injectie
    # formula. Both are surfaced, with month_indexed marking the coefficients
    # as month coefficients: the engine resolves them against the delivery
    # month's mean and refuses them to the hourly-spot path.
    assert inj.current == pytest.approx(0.0476)
    assert inj.month_indexed is True
    assert inj.factor == pytest.approx(0.8)
    assert inj.base == pytest.approx(-0.0265)
    assert inj.formula is not None and "BELPEX" in inj.formula


def test_flex_extracts_injection_rates() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_flex.pdf"), "power_flex", "test://flex", REGION_FLANDERS
    )
    inj = snap.injection
    assert inj is not None
    # Power Flex settles injection on the monthly Belpex-injectie. It surfaces
    # the indicative AND the month coefficients, with month_indexed set: that
    # flag is what routes them to the delivery month's mean and refuses them to
    # the hourly-spot path, which is why they can be surfaced at all.
    assert inj.current == pytest.approx(0.0476)
    assert inj.month_indexed is True
    assert inj.factor == pytest.approx(0.8)
    assert inj.base == pytest.approx(-0.0265)


def test_dynamic_extracts_injection_rates() -> None:
    snap = parse_snapshot(
        fixture_text("eneco_dyn.pdf"), "power_dynamic", "test://dyn", REGION_FLANDERS
    )
    inj = snap.injection
    assert inj is not None
    # Power Dynamic formula: "0,1 X BELPEX-H -1,188". No "Maandprijs" - falls
    # back to "Geschatte jaarprijs" 5,92 c/kWh.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.01188)
    assert inj.current == pytest.approx(0.0592)


def test_injection_survives_valorisatie_suffix_drop() -> None:
    # The July 2026 cards renamed the injection heading from
    # "AFNAME EN INJECTIE / VALORISATIE" to just "AFNAME EN INJECTIE".
    # The old anchor keyed off the dropped "/ VALORISATIE" suffix, which
    # zeroed every Eneco injection credit (issue #35). Parsing must still
    # find the block after the rename.
    for fixture, contract, expected in (
        ("eneco_fix.pdf", "power_fix", 0.0476),
        ("eneco_flex.pdf", "power_flex", 0.0476),
        ("eneco_dyn.pdf", "power_dynamic", 0.0592),
    ):
        text = fixture_text(fixture).replace(
            "AFNAME EN INJECTIE / VALORISATIE", "AFNAME EN INJECTIE"
        )
        assert "VALORISATIE" not in text, fixture
        snap = parse_snapshot(text, contract, f"test://{fixture}", REGION_FLANDERS)
        assert snap.injection is not None, fixture
        assert snap.injection.current == pytest.approx(expected), fixture


# ---- fetch_for_month (historical billing) ----------------------------------


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    return asyncio.run(coro)


def test_fetch_for_month_returns_snapshot_when_url_matches_month() -> None:
    """The Dec-2025 fixture parses cleanly and validates against the
    requested year-month: fetch_for_month must surface the snapshot."""
    text = fixture_text("eneco_flex_dec25.pdf")
    with (
        patch(
            "custom_components.be_electricity_prices.providers.eneco.head_freshness_key",
            new=AsyncMock(return_value="ok"),
        ),
        patch(
            "custom_components.be_electricity_prices.providers.eneco.fetch_pdf_text",
            new=AsyncMock(return_value=text),
        ),
    ):
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2025, 12, 1)))  # type: ignore[arg-type]
    assert snap is not None
    assert snap.publication_label == "december 2025"
    assert snap.valid_until == date(2025, 12, 31)


def test_fetch_for_month_rejects_when_validity_does_not_cover_month() -> None:
    """If the supplier silently overwrote the historical URL with the
    current card (the typical archive-miss failure mode), the parsed
    valid_until won't intersect the requested month and we must
    return None instead of trusting it."""
    text = fixture_text("eneco_flex_dec25.pdf")
    with (
        patch(
            "custom_components.be_electricity_prices.providers.eneco.head_freshness_key",
            new=AsyncMock(return_value="ok"),
        ),
        patch(
            "custom_components.be_electricity_prices.providers.eneco.fetch_pdf_text",
            new=AsyncMock(return_value=text),
        ),
    ):
        # The Dec-2025 fixture covers December, not March.
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2025, 3, 1)))  # type: ignore[arg-type]
    assert snap is None


def test_fetch_for_month_returns_none_on_404() -> None:
    """An archive miss (HEAD returns None for every candidate volume)
    must degrade gracefully so the coordinator can fall back to the
    proxy."""
    with patch(
        "custom_components.be_electricity_prices.providers.eneco.head_freshness_key",
        new=AsyncMock(return_value=None),
    ):
        snap = _run(fetch_for_month(None, "power_flex", "wallonia", date(2024, 6, 1)))  # type: ignore[arg-type]
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    """A contract id that isn't in _CONTRACT_SLUGS must return None
    rather than raise -- the coordinator's monthly cache treats None
    as 'no archive' and falls back to the current snapshot."""
    snap = _run(fetch_for_month(None, "gas_dynamic", "wallonia", date(2025, 12, 1)))  # type: ignore[arg-type]
    assert snap is None


def test_flex_extracts_cohort_coefficients() -> None:
    """The variable card's BELPEX-RLP-M factor / base are surfaced (VAT-baked)
    so a signing cohort can be re-priced against the monthly mean. Applying them
    to the card's stated last-known index (96,8502 EUR/MWh) reproduces the
    printed monthly indicative."""
    snap = parse_snapshot(
        fixture_text("eneco_flex.pdf"), "power_flex", "test://flex", REGION_FLANDERS
    )
    assert isinstance(snap.energy, VariableRates)
    # (0,102 X BELPEX-RLP-M + 3,237) incl 6% VAT:
    # factor 0,102 * 1.06 * 10, base 3,237 * 1.06 / 100.
    factor = snap.energy.formula_factor
    base = snap.energy.formula_base
    assert factor is not None and base is not None
    assert factor == pytest.approx(1.0812, rel=1e-4)
    assert base == pytest.approx(0.034312, rel=1e-4)
    ref = 96.8502 / 1000.0
    assert factor * ref + base == pytest.approx(snap.energy.current, rel=2e-3)


def test_fix_and_flex_credit_the_delivery_month_not_the_printed_indicative() -> None:
    """The card says the credit is "maandelijks geindexeerd op basis van de
    indexatieparameter Belpex-injectie" and that the printed figures are "een
    prijsinschatting ... berekend op basis van de LAATST GEKENDE waarde van
    Belpex-injectie (07/2026)". So the printed rate is last month's index and
    the volume settles on the delivery month's, retroactively.

    Belpex-injectie is the plain arithmetic monthly mean of the Belgian
    day-ahead: measured against the real 2026 series it reproduces the card's
    own published values to four decimals (March 92,6102 against 92,6114 and
    July 109,2488 against 109,2498), so the coefficients resolve exactly."""
    from custom_components.be_electricity_prices.injection import (
        _bake_monthly_injection,
        _historical_injection_rate,
        _injection_is_spot_formula,
    )

    snap = parse_snapshot(
        fixture_text("eneco_flex_aug26.pdf"), "power_flex", "flanders", REGION_FLANDERS
    )
    inj = snap.injection
    assert inj is not None
    assert inj.month_indexed is True
    assert inj.factor == pytest.approx(0.84)
    assert inj.base == pytest.approx(-0.028)

    # Month coefficients are refused to the per-hour path outright, whatever
    # else the card prints. That guard is what makes surfacing them safe.
    assert _injection_is_spot_formula(inj, snap.energy) is False

    august = 0.1293439  # the delivery month's own Belpex-injectie, EUR/kWh
    assert _historical_injection_rate(inj, august) == pytest.approx(0.080649, abs=1e-6)
    # The printed indicative is July's index, and 20,9% below what August pays.
    assert inj.current == pytest.approx(0.0638)
    # The live sensor bakes the same number the year-to-date bills.
    baked = _bake_monthly_injection(snap, august)
    assert baked.injection is not None
    assert baked.injection.current == pytest.approx(0.080649, abs=1e-6)

    # Power Dynamic keeps its HOURLY index and is untouched.
    dyn = parse_snapshot(
        fixture_text("eneco_dyn.pdf"), "power_dynamic", "flanders", REGION_FLANDERS
    )
    assert dyn.injection is not None
    assert dyn.injection.month_indexed is False
    assert _injection_is_spot_formula(dyn.injection, dyn.energy) is True
