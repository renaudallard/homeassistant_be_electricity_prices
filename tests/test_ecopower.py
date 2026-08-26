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
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    InjectionRates,
    SupplierSnapshot,
    VariableRates,
    apply_vat,
)
from custom_components.be_electricity_prices.providers.ecopower import (
    _card_stamp_keys,
    _extract_energy,
    _extract_injection,
    _resolve_latest_dbs_pdf,
    _resolve_latest_pdf,
    fetch_for_month,
    parse_dbs_snapshot,
    parse_snapshot,
)
from tests import make_text_session, fixture_text


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
    expected = set(FLUVIUS_KEYS)
    assert set(snap.dsos) == expected


def test_april_card_extracts_distribution_and_capacity_for_antwerpen() -> None:
    """Spot-check Fluvius Antwerpen against the printed values:
    databeheer 17.85, capacity 49.40 EUR/kW/yr, distribution 0.0505027.
    The card is HTVA and declares vat_rate=0.06, so the snapshot stores
    the flat fees exactly as printed and base.apply_vat grosses them
    once per entry (17.85 -> 18.92, the same Fluvius fee the other
    suppliers print TVAC)."""
    snap = _april_snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(0.0505027)
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40)
    assert a.data_management_per_year == pytest.approx(17.85)
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
    # 54.20 EUR/kW/yr as printed; apply_vat grosses it per entry.
    assert snap.dsos["fluvius_imewo"].capacity_eur_per_kw_year == pytest.approx(54.20)


def test_flat_fees_are_grossed_exactly_once_end_to_end() -> None:
    """The card is the only residential one that declares vat_rate=0.06, so
    it is the only one where apply_vat is not a no-op. The extractor used to
    bake the 6% as well, billing it twice: Fluvius Antwerpen's databeheer was
    served at 17,85 x 1,06^2 = 20,06 instead of 18,92, and capacity at 55,51
    instead of 52,36, overstating a 4 kW entry by about 13,70 EUR/yr."""
    snap = _april_snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.data_management_per_year == pytest.approx(17.85)
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40)

    served = apply_vat(snap, include_vat=True).dsos["fluvius_antwerpen"]
    assert served.data_management_per_year == pytest.approx(17.85 * 1.06)
    assert served.capacity_eur_per_kw_year == pytest.approx(49.40 * 1.06)

    # A business that deducts VAT keeps the card's printed figures.
    net = apply_vat(snap, include_vat=False).dsos["fluvius_antwerpen"]
    assert net.data_management_per_year == pytest.approx(17.85)
    assert net.capacity_eur_per_kw_year == pytest.approx(49.40)


def test_dbs_subscription_is_grossed_exactly_once_end_to_end() -> None:
    """Same for the dynamic card's Abonnementskost: 5,00/month HTVA is
    stored as 60,00 and served as 63,60, not 67,42."""
    snap = _dbs_snap()
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)
    served = apply_vat(snap, include_vat=True)
    assert served.energy.yearly_fixed_fee == pytest.approx(63.6)


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
    # The card prints "Bijdrage Energiefonds 0,006 euro/maand 10,07 euro/maand":
    # 0,00 for a domiciled residential customer, then a SUPERSCRIPT footnote
    # marker, then the non-residential column. The marker is the footnote's
    # number, so reading it as a decimal made the levy drift card to card
    # (0,004 / 0,005 / 0,006 across the fixtures) instead of staying 0.
    assert t.energy_fund_eur_per_month == pytest.approx(0.0)
    # Wallonia / Brussels surcharges stay 0 -- Ecopower is Flanders-only.
    assert t.wallonia_renewables == 0.0
    assert t.brussels_renewables == 0.0


def test_july_card_splits_the_credit_fixed_and_spp() -> None:
    """From July 2026 the credit is half fixed and half indexed: "VAST 50% x
    0,02 euro / VARIABEL +50% x 0,04638137 euro deze waarde volgt de formule
    0,9 x 0,06264597 [EPEX SPP 2] - 0,01", footnote 2 naming the index as "het
    werkelijke SPP gewogen gemiddelde van de Day Ahead EPEX ... voor de maand
    juli".

    The two halves blend into one pair: 0,50 x 0,02 + 0,50 x (0,9 SPP - 0,01)
    = 0,45 SPP + 0,005. No unit conversion, because this card's index is
    already EUR/kWh - unlike the dynamic sibling, which prints EUR/MWh.
    """
    snap = parse_snapshot(
        fixture_text("ecopower_burgerstroom_jul.pdf", layout=True), "t://x", "2026-07"
    )
    inj = snap.injection
    assert inj is not None
    assert inj.factor == pytest.approx(0.45)
    assert inj.base == pytest.approx(0.005)
    assert inj.spp_indexed is True
    # "De terugleververgoeding kan nooit negatief zijn." - on this card only.
    assert inj.floor_at_zero is True
    # Round-trips to the card's own printed figure at its own index.
    assert inj.factor is not None and inj.base is not None
    assert inj.factor * 0.06264597 + inj.base == pytest.approx(0.0332, abs=1e-4)
    assert inj.current == pytest.approx(0.0332)


def test_the_pre_july_and_split_cards_keep_a_flat_credit() -> None:
    """Two separate reasons not to surface coefficients.

    Feb/Apr/May are 100% fixed at 0,020 and the May card says the change is
    still ahead: "Vanaf 1 juli 2026 wordt de prijs van de terugleververgoeding
    50% variabel." The June card prints the formula but pins the credit,
    "OPGELET t.e.m. 30 juni is de terugleververgoeding 0,020 euro/kWh en 100%
    vast", and its own index is a twelve-month VNR forecast rather than a
    settled month - it must never be surfaced as one.

    This matters beyond today: fetch_for_month re-parses each past month, so a
    year-to-date walk crosses all of these.
    """
    for fixture in (
        "ecopower_burgerstroom_feb.pdf",
        "ecopower_burgerstroom_apr.pdf",
        "ecopower_burgerstroom_may.pdf",
        "ecopower_burgerstroom_jun_split.pdf",
    ):
        snap = parse_snapshot(fixture_text(fixture, layout=True), "t://x", "2026-01")
        inj = snap.injection
        assert inj is not None, fixture
        assert inj.current == pytest.approx(0.02), fixture
        assert inj.factor is None, fixture
        assert inj.spp_indexed is False, fixture
        assert inj.floor_at_zero is False, fixture


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
    assert set(snap.dsos) == set(FLUVIUS_KEYS)


def test_energy_regex_ignores_same_line_injection_in_split_layout() -> None:
    """Split-layout energy (resolved value on the line below the label)
    together with a same-line injection value must not bind the energy rate
    to the injection figure. The unanchored 'Groene burgerstroom' pattern
    also matched the 'Injectie Groene Burgerstroom ... euro/kWh' line and
    won before the split fallback ran, pricing energy ~7x too low."""
    text = (
        "Afname Groene Burgerstroom (50% vast aan 0,17 euro + 50% variabel "
        "aan 0,10558785 euro)\n"
        "0,1378 euro/kWh\n"
        "Injectie Groene Burgerstroom (terugleververgoeding)2 -0,0200 euro/kWh\n"
    )
    energy = _extract_energy(text)
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.1378)


@pytest.mark.parametrize(
    "sign",
    ["-", "‐", "‑", "‒", "–", "—", "−"],
)
def test_injection_normalises_every_minus_glyph(sign: str) -> None:
    """The injection regex admits every SIGN_CHARS minus glyph, so the value
    normalisation must strip all of them. U+2010 and U+2011 slipped past the
    hand-rolled variant list and reached to_float unnormalised, raising
    ValueError and crashing the refresh."""
    text = (
        f"Injectie Groene Burgerstroom (terugleververgoeding)2 {sign}0,0200 euro/kWh\n"
    )
    inj = _extract_injection(text)
    assert inj is not None
    assert inj.current == pytest.approx(0.02)


def test_energy_regex_tolerates_a_bullet_prefix() -> None:
    """A re-render that prefixes the consumption line with a bullet must still
    parse. The line anchor allows a leading run of punctuation but cannot
    consume the leading word of "Injectie", so the injection line on the next
    line stays excluded and the energy rate binds to the bulleted line."""
    text = (
        "• Afname Groene burgerstroom (50% vast aan 0,17 euro) 0,1341 euro/kWh\n"
        "Injectie Groene Burgerstroom (terugleververgoeding)2 -0,0200 euro/kWh\n"
    )
    energy = _extract_energy(text)
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.1341)


def _july_snap() -> SupplierSnapshot:
    return parse_snapshot(
        _text("ecopower_burgerstroom_jul.pdf"),
        "test://ecopower-jul",
        "juli 2026",
    )


def test_variabel_layout_card_parses_energy_and_injection() -> None:
    """The July 2026 card broke the 50/50 split onto its own VAST and
    VARIABEL lines, with the resolved rate trailing the VARIABEL half
    instead of sitting on the label line or the one below it. Both the
    same-line and next-line regexes missed: energy raised and took the
    whole supplier offline, while injection quietly returned None, which
    costs a solar user their entire feed-in credit without any error."""
    snap = _july_snap()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1422)
    assert snap.injection is not None
    # Injection went 50% variable from 1 July, so the resolved -0,0332
    # printed as a negative cost is credited as +0.0332.
    assert snap.injection.current == pytest.approx(0.0332)
    assert set(snap.dsos) == set(FLUVIUS_KEYS)


def test_variabel_layout_does_not_capture_the_wkk_levy() -> None:
    """The VARIABEL fallback must anchor on the literal VAST / VARIABEL
    rows, not merely skip a line. A looser 'label, then one or two lines'
    pattern matched 'Kost WKK 0,00392 euro/kWh' two lines under the label
    on the same-line cards, which would bill the cogeneration levy as the
    commodity rate the moment the same-line regex missed."""
    for fixture, expected in (
        ("ecopower_burgerstroom_feb.pdf", 0.1287),
        ("ecopower_burgerstroom_apr.pdf", 0.1274),
        ("ecopower_burgerstroom_may.pdf", 0.1341),
        ("ecopower_burgerstroom_jun_split.pdf", 0.1378),
    ):
        energy = _extract_energy(_text(fixture))
        assert isinstance(energy, VariableRates)
        assert energy.current == pytest.approx(expected)


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
    """Abonnementskost 5,00 euro/maand HTVA -> the 12-month total, still
    HTVA: base.apply_vat grosses every flat annual fee once per entry."""
    snap = _dbs_snap()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)


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
    assert set(snap.dsos) == set(FLUVIUS_KEYS)


def test_dbs_card_dso_row_columns_for_antwerpen() -> None:
    """The dynamic row layout (databeheer | capacity | enkelvoudig |
    uitsluitend-nacht | injectietarief) parses the same four columns the
    gbs parser keeps; the trailing injection network tariff is ignored."""
    snap = _dbs_snap()
    a = snap.dsos["fluvius_antwerpen"]
    # Stored as printed; apply_vat grosses both once per entry.
    assert a.data_management_per_year == pytest.approx(17.85)
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40)
    assert a.distribution_single == pytest.approx(0.0505027)
    assert a.distribution_exclusive_night == pytest.approx(0.0454058)
    assert a.transport == 0.0


def test_dbs_card_wrapped_midden_vlaanderen_row_parses() -> None:
    """The stitched 'Fluvius Midden-Vlaanderen' row keeps its real rates
    rather than dropping the sub-area or mis-aligning a neighbour's."""
    snap = _dbs_snap()
    mv = snap.dsos["fluvius_intergem"]
    assert mv.distribution_single == pytest.approx(0.0498061)
    # 50.12 EUR/kW/yr as printed; apply_vat grosses it per entry.
    assert mv.capacity_eur_per_kw_year == pytest.approx(50.12)


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
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
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
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
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
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_burgerstroom",
            "flanders",
            date(2024, 6, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    snap = asyncio.run(
        fetch_for_month(
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
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
                make_text_session(_DBS_LISTING_HTML),  # type: ignore[arg-type]
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
            make_text_session(_DBS_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_dynamische_burgerstroom",
            "flanders",
            date(2024, 1, 1),
        )
    )
    assert snap is None


_DBS_LISTING_HTML_DATED = """
<a href="https://cdn.example/202510_dbs_tariefkaart.pdf">2025-10</a>
<a href="https://cdn.example/202601_dbs_tariefkaart.pdf">2026-01</a>
<a href="https://cdn.example/20260801_dbs_tariefkaart.pdf">2026-08</a>
"""


_GBS_LISTING_DATED = """
<a href="https://cdn.example/202607_gbs_tariefkaart.pdf">July</a>
<a href="https://cdn.example/20260715_gbs_tariefkaart.pdf">July reissue</a>
<a href="https://cdn.example/202608_gbs_inschatting_tariefkaart_ecopower.pdf">Aug preview</a>
"""


def test_gbs_resolver_takes_the_dated_reissue_and_skips_the_preview() -> None:
    # The gbs half of the widened pattern: a month can carry both a bare
    # and a dated card, and the dated reissue is the one billing. The
    # next-month inschatting preview is still not a billable card.
    url, label = asyncio.run(
        _resolve_latest_pdf(
            make_text_session(_GBS_LISTING_DATED),  # type: ignore[arg-type]
        )
    )
    assert url.endswith("20260715_gbs_tariefkaart.pdf")
    assert label == "2026-07"


def test_gbs_fetch_for_month_prefers_the_dated_reissue() -> None:
    # docs/providers/ecopower.md states this explicitly ("take the highest
    # stamp among them"), and nothing pinned it.
    text = _text("ecopower_burgerstroom_jul.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.ecopower.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_GBS_LISTING_DATED),  # type: ignore[arg-type]
                "ecopower_burgerstroom",
                "flanders",
                date(2026, 7, 1),
            )
        )
    assert snap is not None
    assert snap.source_url.endswith("20260715_gbs_tariefkaart.pdf")


def test_dbs_resolver_reads_the_dated_yyyymmdd_card() -> None:
    # Ecopower switched the dynamic card to a YYYYMMDD filename with the
    # August 2026 issue. A six-digit pattern cannot match eight digits, so
    # the resolver silently kept serving the January card -- and it parsed
    # fine, so nothing failed. The month label must stay YYYY-MM.
    url, label = asyncio.run(
        _resolve_latest_dbs_pdf(
            make_text_session(_DBS_LISTING_HTML_DATED),  # type: ignore[arg-type]
        )
    )
    assert url.endswith("20260801_dbs_tariefkaart.pdf")
    assert label == "2026-08"


def test_dbs_dated_card_sorts_above_the_bare_month_form() -> None:
    # Both filename forms are live on the page at once, so they have to
    # order against each other rather than within their own shape.
    assert _card_stamp_keys("202601") == ("20260100", "202601")
    assert _card_stamp_keys("20260801") == ("20260801", "202608")
    assert _card_stamp_keys("202608") < _card_stamp_keys("20260801")
    assert _card_stamp_keys("20260801") < _card_stamp_keys("202609")


@pytest.mark.parametrize(
    ("month", "expected"),
    [
        pytest.param(date(2026, 7, 1), "2026-01", id="before-the-dated-card"),
        pytest.param(date(2026, 8, 1), "2026-08", id="the-dated-card-month"),
        pytest.param(date(2026, 9, 1), "2026-08", id="after-the-dated-card"),
    ],
)
def test_dbs_fetch_for_month_spans_both_filename_forms(
    month: date, expected: str
) -> None:
    # The archive comparison was string-vs-YYYYMM, which excluded a
    # YYYYMMDD card from its own month: "20260801" > "202608".
    text = _text("ecopower_dynamische_burgerstroom_jan.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.ecopower.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_DBS_LISTING_HTML_DATED),  # type: ignore[arg-type]
                "ecopower_dynamische_burgerstroom",
                "flanders",
                month,
            )
        )
    assert snap is not None
    assert snap.publication_label == expected


def test_energy_fund_marker_is_not_read_as_a_third_decimal() -> None:
    """The footnote number after the value changes between cards, so reading
    it made a fixed levy look like it moved every month."""
    for fixture in (
        "ecopower_burgerstroom_feb.pdf",
        "ecopower_burgerstroom_apr.pdf",
        "ecopower_burgerstroom_jul.pdf",
    ):
        snap = parse_snapshot(fixture_text(fixture, layout=True), "t://", "x")
        assert snap.taxes.energy_fund_eur_per_month == pytest.approx(0.0), fixture
