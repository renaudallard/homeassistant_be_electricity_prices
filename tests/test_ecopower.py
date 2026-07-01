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

"""Fixture-based tests for the Ecopower extractor."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    InjectionRates,
    SupplierSnapshot,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.ecopower import (
    fetch_for_month,
    parse_dbs_snapshot,
    parse_snapshot,
)
from tests import fixture_text


def _text(name: str) -> str:
    return fixture_text(name, layout=True)


def _april_snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text("ecopower_burgerstroom_apr.pdf"),
        "test://ecopower-apr",
        "april 2026",
    )


def test_empty_dso_overlay_is_fatal() -> None:
    # Section header present but no DSO row parses (label drift) -> raise,
    # so the backfill path can't silently skip the month.
    text = _text("ecopower_burgerstroom_apr.pdf").replace("Fluvius", "XXX")
    with pytest.raises(ExtractorError, match="no DSO rows"):
        parse_snapshot(text, "test://ecopower-apr", "april 2026")


def test_missing_gsc_or_wkk_surcharge_is_fatal() -> None:
    # GSC/WKK are the mandatory Flanders renewable surcharge; a relabel must
    # fail loud rather than silently zeroing a per-kWh charge.
    for label in ("Kost GSC", "Kost WKK"):
        text = _text("ecopower_burgerstroom_apr.pdf").replace(label, "XXX")
        with pytest.raises(ExtractorError, match="GSC/WKK"):
            parse_snapshot(text, "test://ecopower-apr", "april 2026")


def _may_snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text("ecopower_burgerstroom_may.pdf"),
        "test://ecopower-may",
        "mei 2026",
    )


def test_april_card_energy_is_groene_burgerstroom_resolved_rate() -> None:
    """The card prints '(50% vast aan 0,17 euro + 50% variabel aan
    0,08472117 euro)   0,1274 euro/kWh'. We use the resolved rate."""
    snap = _april_snap()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1274)


def test_april_card_dsos_cover_all_eight_fluvius_subareas() -> None:
    snap = _april_snap()
    expected = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    assert set(snap.dsos) == expected


def test_april_card_extracts_distribution_and_capacity_for_antwerpen() -> None:
    """Spot-check Fluvius Antwerpen against the printed values:
    databeheer 17.85, capacity 49.40 EUR/kW/yr HTVA, distribution
    0.0505027. Both the capacity and databeheer tariffs are flat euro
    fees that bypass the pricing VAT factor, so they carry the 6%
    residential VAT baked in (49.40 * 1.06; 17.85 * 1.06 = 18.92, the
    same Fluvius fee the other suppliers print TVAC)."""
    snap = _april_snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(0.0505027)
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40 * 1.06)
    assert a.data_management_per_year == pytest.approx(17.85 * 1.06)
    # Ecopower rolls Elia transport into the network distribution; the
    # card has no separate transport line, so ``transport`` stays 0
    # rather than being silently double-counted via a guess.
    assert a.transport == 0.0


def test_april_card_extracts_imewo_with_optional_max_column() -> None:
    """Imewo's row carries an optional 'Maximumtarief' value
    (``0,3276168``) inserted between the off-peak rate and the
    trailing dash. The regex must skip past it without mis-aligning
    the distribution rate."""
    snap = _april_snap()
    assert snap.dsos["fluvius_imewo"].distribution_single == pytest.approx(0.0522864)
    # 54.20 EUR/kW/yr HTVA + 6% residential VAT baked in.
    assert snap.dsos["fluvius_imewo"].capacity_eur_per_kw_year == pytest.approx(
        54.20 * 1.06
    )


def test_april_card_taxes_are_htva_with_vat_06() -> None:
    """Ecopower publishes HTVA values; vat_rate=0.06 instructs
    compute_breakdown to scale to TVAC."""
    snap = _april_snap()
    t = snap.taxes
    assert t.vat_rate == 0.06
    assert t.federal_excise == pytest.approx(0.04748)
    assert t.energy_contribution == pytest.approx(0.0019261)
    # GSC + WKK = 0.0110 + 0.00392 = 0.01492.
    assert t.flanders_renewables == pytest.approx(0.01492)
    assert t.energy_fund_eur_per_month == pytest.approx(0.006)
    # Wallonia / Brussels surcharges stay 0 -- Ecopower is Flanders-only.
    assert t.wallonia_renewables == 0.0
    assert t.brussels_renewables == 0.0


def test_april_card_injection_is_positive_credit() -> None:
    """The terugleververgoeding is a feed-in credit the customer
    receives. The card prints it as a negative cost
    ('Terugleververgoeding (digitale meter): -0,0200 euro/kWh'); the
    parser negates that so ``current`` is the positive +0.02 credit."""
    snap = _april_snap()
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.02)


def _split_snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text("ecopower_burgerstroom_jun_split.pdf"),
        "test://ecopower-split",
        "juni 2026",
    )


def test_split_layout_card_parses_energy_and_injection() -> None:
    """Mid-2026 cards moved the resolved energy and injection rates onto
    the line below their label (the 50/50-split layout, previewed on the
    June estimation card). Energy is the blended 50/50 rate (0,1378
    euro/kWh). Injection is still 100% fixed until 30 June, and the card
    says so explicitly ("OPGELET t.e.m. 30 juni is de terugleververgoeding
    0,020 euro/kWh en 100% vast"); the parser must credit that fixed
    0,020 rather than the 0,0329 variable-formula value printed on the
    line below the label, which only applies once injection goes variable."""
    snap = _split_snap()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1378)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.020)
    assert set(snap.dsos) == {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }


def test_stale_fixed_injection_note_is_ignored_on_a_later_card() -> None:
    """The '100% vast' note declares its own expiry (t.e.m. 30 juni). If a
    later month's card still carries the stale note while already printing
    the variable formula, the note must NOT win -- the parser falls back
    to the variable value. Relabel the June split card as a July card to
    simulate the carried-over note."""
    text = _text("ecopower_burgerstroom_jun_split.pdf").replace(
        "Tariefkaart juni 2026", "Tariefkaart juli 2026"
    )
    snap = parse_snapshot(text, "test://ecopower-stale", "juli 2026")
    assert snap.injection is not None
    # July is past the note's 30 June expiry, so the variable-formula
    # value (0,0329) is credited instead of the stale fixed 0,020.
    assert snap.injection.current == pytest.approx(0.0329)


def test_may_card_injection_label_is_matched() -> None:
    """Issue #31: the May 2026 card renamed the injection row from
    'Terugleververgoeding (digitale meter)' to
    'Injectie Groene Burgerstroom (terugleververgoeding)', which the
    old regex missed, so the injection price went unavailable. The
    value is the card's printed figure (sign corrected separately)."""
    snap = _may_snap()
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.02)


def test_april_card_publication_and_supplier_metadata() -> None:
    snap = _april_snap()
    assert snap.supplier == "ecopower"
    assert snap.contract == "ecopower_burgerstroom"
    assert snap.publication_label == "april 2026"


# ---- Dynamische burgerstroom (dynamic) -----------------------------------------


def _dbs_snap() -> SupplierSnapshot:
    return parse_dbs_snapshot(
        _text("ecopower_dynamische_burgerstroom_jan.pdf"),
        "test://ecopower-dbs",
        "2026-01",
    )


def test_dbs_card_energy_is_dynamic_formula_htva() -> None:
    """The card prints 'elk kwartier 0,00102 × EPEX DA +0,004 euro/kWh'.
    EPEX DA is in EUR/MWh, so the factor scales by 1000 to act on the
    EUR/kWh spot (1,02). The base (0,004) and factor stay HTVA --
    vat_rate=0.06 scales the energy to TVAC in the pricing engine."""
    snap = _dbs_snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.02)
    assert snap.energy.base == pytest.approx(0.004)
    assert snap.energy.quarter_hourly is True


def test_dbs_card_subscription_fee_is_vat_inclusive_annual() -> None:
    """Abonnementskost 5,00 euro/maand HTVA. yearly_fixed_fee holds the
    actual annual euros (summed without rescaling), so it is the
    VAT-inclusive 12-month total: 5 × 12 × 1.06 = 63.60."""
    snap = _dbs_snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(63.6)


def test_dbs_card_injection_is_dynamic_formula() -> None:
    """Injection 'Terugleververgoeding elk kwartier 0,00098 × EPEX DA -
    0,015 euro/kWh' -> factor 0.98, base -0.015. The base is negative
    (the credit drops below zero at low spot), and injection is stored
    unscaled (residential injection is VAT-exempt)."""
    snap = _dbs_snap()
    assert isinstance(snap.injection, InjectionRates)
    assert snap.injection.current is None
    assert snap.injection.factor == pytest.approx(0.98)
    assert snap.injection.base == pytest.approx(-0.015)


def test_dbs_card_dsos_cover_all_eight_fluvius_subareas() -> None:
    """The narrower dynamic card wraps 'Fluvius Midden-Vlaanderen' across
    its data row; the label-stitch must keep all eight sub-areas."""
    snap = _dbs_snap()
    assert set(snap.dsos) == {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }


def test_dbs_card_dso_row_columns_for_antwerpen() -> None:
    """The dynamic row layout (databeheer | capacity | enkelvoudig |
    uitsluitend-nacht | injectietarief) parses the same four columns the
    gbs parser keeps; the trailing injection network tariff is ignored."""
    snap = _dbs_snap()
    a = snap.dsos["fluvius_antwerpen"]
    # 17.85 HTVA + 6% residential VAT baked in (= 18.92 TVAC).
    assert a.data_management_per_year == pytest.approx(17.85 * 1.06)
    # 49.40 EUR/kW/yr HTVA + 6% residential VAT baked in.
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40 * 1.06)
    assert a.distribution_single == pytest.approx(0.0505027)
    assert a.distribution_exclusive_night == pytest.approx(0.0454058)
    assert a.transport == 0.0


def test_dbs_card_wrapped_midden_vlaanderen_row_parses() -> None:
    """The stitched 'Fluvius Midden-Vlaanderen' row keeps its real rates
    rather than dropping the sub-area or mis-aligning a neighbour's."""
    snap = _dbs_snap()
    mv = snap.dsos["fluvius_intergem"]
    assert mv.distribution_single == pytest.approx(0.0498061)
    # 50.12 EUR/kW/yr HTVA + 6% residential VAT baked in.
    assert mv.capacity_eur_per_kw_year == pytest.approx(50.12 * 1.06)


def test_dbs_card_taxes_are_htva_with_vat_06() -> None:
    snap = _dbs_snap()
    t = snap.taxes
    assert t.vat_rate == 0.06
    assert t.federal_excise == pytest.approx(0.04748)
    assert t.energy_contribution == pytest.approx(0.0019261)
    # GSC + WKK = 0.0110 + 0.00392 = 0.01492.
    assert t.flanders_renewables == pytest.approx(0.01492)


def test_dbs_card_supplier_and_contract_metadata() -> None:
    snap = _dbs_snap()
    assert snap.supplier == "ecopower"
    assert snap.contract == "ecopower_dynamische_burgerstroom"
    assert snap.publication_label == "2026-01"


# ---- fetch_for_month -----------------------------------------------------------


_LISTING_HTML = """
<a href="https://cdn.example/202601_gbs_tariefkaart.pdf">January</a>
<a href="https://cdn.example/202602_gbs_tariefkaart.pdf">February</a>
<a href="https://cdn.example/202603_gbs_tariefkaart.pdf">March</a>
<a href="https://cdn.example/202604_gbs_tariefkaart.pdf">April</a>
<a href="https://cdn.example/202605_gbs_inschatting_tariefkaart_ecopower.pdf">May preview</a>
"""


class _Resp:
    status = 200

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Session:
    def __init__(self, body: str) -> None:
        self._body = body

    def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
        return _Resp(self._body)


def test_fetch_for_month_returns_snapshot_when_listing_has_url() -> None:
    """The Feb-2026 fixture parses cleanly and the listing URL with
    matching YYYYMM prefix is what fetch_for_month must surface."""
    text = _text("ecopower_burgerstroom_feb.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.ecopower.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_LISTING_HTML),  # type: ignore[arg-type]
                "ecopower_burgerstroom",
                "flanders",
                date(2026, 2, 1),
            )
        )
    assert snap is not None
    assert snap.publication_label == "2026-02"
    assert isinstance(snap.energy, VariableRates)


def test_fetch_for_month_skips_inschatting_preview() -> None:
    """The next-month preview (gbs_inschatting) is on the listing but
    is not a billable card. fetch_for_month must not return it as the
    historical snapshot for any month."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_burgerstroom",
            "flanders",
            date(2026, 5, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_returns_none_when_listing_has_no_match() -> None:
    """Months Ecopower doesn't carry on the listing return None so the
    coordinator falls back to the proxy. Ecopower keeps only the last
    few months around."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_burgerstroom",
            "flanders",
            date(2024, 6, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_zakelijk",
            "flanders",
            date(2026, 2, 1),
        )
    )
    assert snap is None


_DBS_LISTING_HTML = """
<a href="https://cdn.example/202406_dbs_tariefkaart_ecopower.pdf">2024-06</a>
<a href="https://cdn.example/202501b_dbs_tariefkaart.pdf">2025-01</a>
<a href="https://cdn.example/202510_dbs_tariefkaart.pdf">2025-10</a>
<a href="https://cdn.example/202601_dbs_tariefkaart.pdf">2026-01</a>
"""


def test_dbs_fetch_for_month_picks_card_in_effect() -> None:
    """Dynamic cards don't rotate monthly. For Nov 2025 the card in
    effect is the Oct 2025 one (202510), not the Jan 2026 card published
    later, so a year-boundary rate change is billed to the right months."""
    text = _text("ecopower_dynamische_burgerstroom_jan.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.ecopower.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_DBS_LISTING_HTML),  # type: ignore[arg-type]
                "ecopower_dynamische_burgerstroom",
                "flanders",
                date(2025, 11, 1),
            )
        )
    assert snap is not None
    assert snap.contract == "ecopower_dynamische_burgerstroom"
    assert snap.publication_label == "2025-10"


def test_dbs_fetch_for_month_returns_none_before_first_card() -> None:
    """Months before the earliest published dynamic card return None so
    the coordinator falls back to the proxy snapshot."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_DBS_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_dynamische_burgerstroom",
            "flanders",
            date(2024, 1, 1),
        )
    )
    assert snap is None
