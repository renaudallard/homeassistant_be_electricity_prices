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

"""Mega PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
import re
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers import mega as mega_mod
from tests import FIXTURES, fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    FixedRates,
    ImpactRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.mega import (
    _find_pdf_url,
    _resolve_pdf_url,
    parse_snapshot,
)


def test_mega_is_registered() -> None:
    assert "mega" in EXTRACTORS
    assert EXTRACTORS["mega"].label == "Mega"
    contract_ids = {c.id for c in EXTRACTORS["mega"].contracts}
    # Spot-check the flagship products.
    assert "mega_smart_fixed" in contract_ids
    assert "mega_smart_flex" in contract_ids
    assert "mega_dynamic" in contract_ids
    # Zen Fixed was discontinued in August 2026 (listing block dropped in all
    # three regions; Wallonia resolves to the CDN's HTML stub).
    assert "mega_zen_fixed" not in contract_ids
    # Off-peak Fixed was pulled in July 2026 and came back for the August card,
    # with a B2B edition it did not have before.
    assert "mega_offpeak_fixed" in contract_ids
    assert "mega_pro_offpeak_fixed" in contract_ids
    # Eleven residential products, plus the nine professional editions.
    assert len(contract_ids) == 20


def test_listing_url_finder_picks_electricity_for_region() -> None:
    listing = (FIXTURES / "mega_listing.html").read_text()
    url = _find_pdf_url(listing, "Smart Fixed", "WL")
    assert url is not None
    assert url.startswith("https://my.mega.be/resources/tarif/")
    assert "Mega-FR-EL-B2C-WL-" in url
    assert url.endswith("Smart2204-Fixed.pdf")
    # The same listing has gas variants and other regions; they must NOT
    # match Smart Fixed/Wallonia.
    assert "NG" not in url


def test_listing_url_finder_returns_none_for_unknown_product() -> None:
    listing = (FIXTURES / "mega_listing.html").read_text()
    assert _find_pdf_url(listing, "Bogus Product", "WL") is None


def test_resolver_returns_direct_url_when_block_present() -> None:
    # When the listing carries the requested region's block, the resolver
    # is a pass-through of _find_pdf_url -- no rewrite, byte-identical.
    listing = (FIXTURES / "mega_listing.html").read_text()
    assert _resolve_pdf_url(listing, "Dynamic", "WL") == _find_pdf_url(
        listing, "Dynamic", "WL"
    )


def test_resolver_falls_back_to_sibling_region_when_block_missing() -> None:
    # Reproduce #42: Mega dropped the Wallonia Dynamic block from the
    # listing while the card PDF stayed published. The three regional
    # editions differ only by the -B2C-<REGION>- filename segment, so the
    # resolver must rewrite a surviving sibling's URL to the Wallonia code.
    listing = (FIXTURES / "mega_listing.html").read_text()
    gapped = re.sub(
        r'<a data-product-element="Dynamic"[^>]*Mega-FR-EL-B2C-WL-[^<]*</a>',
        "",
        listing,
    )
    assert _find_pdf_url(gapped, "Dynamic", "WL") is None
    assert _find_pdf_url(gapped, "Dynamic", "VL") is not None
    resolved = _resolve_pdf_url(gapped, "Dynamic", "WL")
    assert (
        resolved
        == "https://my.mega.be/resources/tarif/Mega-FR-EL-B2C-WL-042026-Dynamic0104.pdf"
    )


def test_resolver_returns_none_when_no_region_has_the_product() -> None:
    listing = (FIXTURES / "mega_listing.html").read_text()
    assert _resolve_pdf_url(listing, "Bogus Product", "WL") is None


def test_fetch_for_month_rewrites_effective_date_month_preserving_day() -> None:
    # Smart Fixed publishes on day 22 with the effective-date suffix
    # mid-token before -Fixed (Smart2204-Fixed), exactly the case the old
    # `01<MM>.pdf$` rewrite missed. fetch_for_month must rotate both the
    # -MMYYYY- segment and the suffix month to the requested month while
    # preserving the publication day, so the historical URL resolves.
    listing = (FIXTURES / "mega_listing.html").read_text()
    captured: dict[str, str] = {}

    async def _capture(_session: object, url: str, **_kw: object) -> str:
        captured["url"] = url
        raise ExtractorError("short-circuit before parse")

    async def _run() -> None:
        with (
            patch.object(
                mega_mod, "_fetch_listing_html", new=AsyncMock(return_value=listing)
            ),
            patch.object(mega_mod, "fetch_pdf_text", new=_capture),
        ):
            out = await mega_mod.fetch_for_month(
                None,  # type: ignore[arg-type]
                "mega_smart_fixed",
                "wallonia",
                date(2026, 3, 1),
            )
        assert out is None  # the patched fetch raised
        # April 2204 (22 April) -> March 2203 (22 March): both months
        # rotate, the day stays put, the year is untouched.
        assert captured["url"].endswith("-032026-Smart2203-Fixed.pdf")

    asyncio.run(_run())


def test_fetch_for_month_builds_the_b2b_url_for_a_professional_contract() -> None:
    # The professional cards are absent from the public listing, and the
    # B2C entry they used to match carries the same product name ("Smart
    # Fixed"), so resolving them through the listing billed a B2B contract
    # at residential rates. The archive must build the B2B filename the
    # same way fetch() does, without touching the listing at all.
    captured: list[str] = []

    async def _capture(_session: object, url: str, **_kw: object) -> str:
        captured.append(url)
        raise ExtractorError("short-circuit before parse")

    listing_calls = AsyncMock(side_effect=AssertionError("listing must not be used"))

    async def _run() -> None:
        with (
            patch.object(mega_mod, "_fetch_listing_html", new=listing_calls),
            patch.object(mega_mod, "fetch_pdf_text", new=_capture),
        ):
            for contract_id, expected in (
                ("mega_pro_smart_fixed", "-072026-Smart0107-Fixed.pdf"),
                ("mega_pro_smart_flex", "-072026-Smart0107.pdf"),
            ):
                captured.clear()
                out = await mega_mod.fetch_for_month(
                    None,  # type: ignore[arg-type]
                    contract_id,
                    "wallonia",
                    date(2026, 7, 1),
                )
                assert out is None  # the patched fetch raised
                # One attempt only: no previous-month retry, or an
                # unpublished month would bill the neighbour's card.
                assert len(captured) == 1
                assert "-B2B-WL-" in captured[0]
                assert captured[0].endswith(expected)

    asyncio.run(_run())


def test_dynamic_extracts_consumption_formula_tvac() -> None:
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    # Mega's PDF: "formule tarifaire suivante : Day Ahead Epex Spot
    # * 1,05 + 1,35 c€/kWh" - already TVAC, spot is in c€/kWh.
    # In our model (spot in EUR/kWh): factor = 1.05, base = 0.0135 EUR.
    assert snap.energy.factor == pytest.approx(1.05)
    assert snap.energy.base == pytest.approx(0.0135)
    assert snap.energy.yearly_fixed_fee == pytest.approx(42.4)


def test_dynamic_injection_uses_separate_htva_formula_with_endash() -> None:
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    inj = snap.injection
    assert inj is not None
    # Injection block: "formule suivante (HTVA) : Day Ahead EPEX SPOT
    # Belgium * 1 – 4 c€/kWh". The dash here is a Unicode en-dash, not
    # an ASCII hyphen - the parser must read it as negative.
    assert inj.factor == pytest.approx(1.0)
    assert inj.base == pytest.approx(-0.04)


def test_dynamic_consumption_and_injection_are_not_swapped() -> None:
    # Mega prints the injection formula BEFORE the consumption formula
    # in the document. A naive 'first formula' / 'second formula' policy
    # gets them backwards. The parser anchors on each formula's distinct
    # label ('formule tarifaire suivante' vs 'formule suivante (HTVA)').
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.injection is not None
    assert snap.injection.factor is not None
    assert snap.injection.base is not None
    # Consumption factor is higher and base is positive.
    assert snap.energy.factor > snap.injection.factor
    assert snap.energy.base > 0.0
    # Injection base is negative (you pay to inject at low spot).
    assert snap.injection.base < 0.0


def test_smart_fixed_wallonia_extracts_bihourly_rates() -> None:
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    # PDF: 17.12 (mono), 19.38 (jour), 15.49 (nuit / excl_nuit).
    assert snap.energy.single == pytest.approx(0.1712)
    assert snap.energy.peak == pytest.approx(0.1938)
    assert snap.energy.offpeak == pytest.approx(0.1549)
    assert snap.energy.exclusive_night == pytest.approx(0.1549)
    assert snap.energy.yearly_fixed_fee == pytest.approx(111.3)


def test_smart_fixed_brussels_extracts_sibelga_row() -> None:
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "brussels"
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_peak == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)
    # Metering fee 14.73 + Sibelga <=13kVA fixed term 50.0744 (both billed
    # to a residential Brussels connection; no separate capacity charge).
    assert sibelga.data_management_per_year == pytest.approx(14.73 + 50.0744)
    # Brugel OSP fee tiers, billed per the configured connection power.
    assert sibelga.brussels_osp_by_tier == {
        "le1_44": pytest.approx(0.0),
        "le6": pytest.approx(13.36),
        "le9_6": pytest.approx(21.37),
        "le13": pytest.approx(26.71),
    }


def test_wallonia_dso_carries_prosumer_rate_from_separate_table() -> None:
    # Mega lists prosumer rates in their own small table further down
    # the PDF, separate from the main DSO row. The parser cross-references
    # the two and still produces a complete DsoOverlay.
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_w.pdf"), "wallonia"
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_supplier_pv_forfait_extracted_where_card_prints_it() -> None:
    # Compensation-regime cards print a supplier-side "Forfait panneaux
    # solaires (EUR/kVA par mois) 7.63" that is billed on top of the DSO
    # prosumer column. 7,63/month annualises to 91,56 EUR/kVA/an (TVAC, so
    # not VAT-scaled). pypdf splits the label/value differently per card.
    for contract, fixture, region in (
        ("mega_smart_fixed", "mega_smart_fixed_w.pdf", "wallonia"),
        ("mega_smart_fixed", "mega_smart_fixed_v.pdf", "flanders"),
        ("mega_smart_flex", "mega_smart_flex_w.pdf", "wallonia"),
        ("mega_offpeak_impact_var", "mega_offpeak_impact_w.pdf", "wallonia"),
        ("mega_dynamic", "mega_dynamic_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(contract, fixture_text(fixture), region)
        assert snap.supplier_prosumer_eur_per_kva_year == pytest.approx(91.56)


def test_supplier_pv_forfait_absent_on_brussels_and_flanders_dynamic() -> None:
    # Brussels cards and the Flanders Dynamic card carry no compensation
    # regime and omit the forfait line; that absence is legitimate, not a
    # drift, so the field stays None rather than raising.
    brussels = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "brussels"
    )
    assert brussels.supplier_prosumer_eur_per_kva_year is None
    flanders_dynamic = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_v.pdf"), "flanders"
    )
    assert flanders_dynamic.supplier_prosumer_eur_per_kva_year is None


def test_flanders_dynamic_smaller_dso_table_with_external_data_fee() -> None:
    # Dynamic V cards list only 2 columns per Fluvius row (digital meter
    # only). The Tarif de gestion des données fee is broken out in a
    # separate paragraph - the parser pulls it from there.
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_v.pdf"), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.3679)
    assert antwerpen.distribution_single == pytest.approx(0.053533)
    assert antwerpen.transport == 0.0  # Rolled into distribution.
    assert antwerpen.data_management_per_year == pytest.approx(18.92)
    # Dynamic cards print only the two digital columns, no exclusive-night.
    assert antwerpen.distribution_exclusive_night is None


def test_flanders_static_carries_exclusive_night_distribution() -> None:
    # Static cards print a third digital column, the exclusive-night
    # distribution rate (lower than the normal digital rate).
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.distribution_single == pytest.approx(0.053533)
    excl = antwerpen.distribution_exclusive_night
    assert excl == pytest.approx(0.048130)
    assert excl is not None and excl < antwerpen.distribution_single


def test_flanders_static_carries_fluvius_prosumer_rate() -> None:
    # Compensation-regime Flanders cards print a Fluvius "Tarif Prosumer"
    # (EUR/kW/an) table; the per-DSO rate must be billed on top of the
    # supplier forfait, so it has to land on the DSO overlay.
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    assert snap.dsos["fluvius_antwerpen"].prosumer_eur_per_kva_year == pytest.approx(
        54.63
    )
    assert snap.dsos["fluvius_west"].prosumer_eur_per_kva_year == pytest.approx(69.59)


def test_flanders_dynamic_has_no_prosumer_rate() -> None:
    # The Flanders Dynamic card carries no compensation regime, so the
    # prosumer table is absent and the DSO overlay must leave the field unset.
    snap = parse_snapshot(
        "mega_dynamic", fixture_text("mega_dynamic_v.pdf"), "flanders"
    )
    assert snap.dsos["fluvius_antwerpen"].prosumer_eur_per_kva_year is None


def test_taxes_split_correctly_per_region() -> None:
    w = parse_snapshot("mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia")
    v = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    b = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "brussels"
    )
    # Federal excise + energy contribution match across regions.
    assert w.taxes.federal_excise == pytest.approx(0.0503288)
    assert v.taxes.federal_excise == pytest.approx(0.0503288)
    assert b.taxes.federal_excise == pytest.approx(0.0503288)
    assert w.taxes.energy_contribution == pytest.approx(0.0020417)
    # Wallonia: Cotisation Verte + Redevance de raccordement.
    assert w.taxes.wallonia_renewables == pytest.approx(0.03008)
    assert w.taxes.region_connection_fee == pytest.approx(0.00075)
    # Flanders: combined green + cogeneration into flanders_renewables.
    assert v.taxes.flanders_renewables > 0.0
    assert v.taxes.region_connection_fee == 0.0
    # Brussels: brussels_renewables only.
    assert b.taxes.brussels_renewables > 0.0
    assert b.taxes.flanders_renewables == 0.0
    assert b.taxes.wallonia_renewables == 0.0


def test_publication_month_keeps_version_for_august() -> None:
    # "août" contains û, which the version-month token class must include
    # so an August Smart Fixed card keeps its version number ("2 août
    # 2026") instead of falling back to the month-only label.
    assert mega_mod._extract_publication_month("Prix V2 août 2026 ...") == "2 août 2026"


def test_missing_yearly_fee_is_fatal() -> None:
    # The Redevance fixe standing charge is mandatory; a miss must raise.
    with pytest.raises(ExtractorError, match="Redevance fixe"):
        mega_mod._extract_yearly_fee("no fee row here")


def test_missing_wallonia_connection_fee_is_fatal() -> None:
    # The Wallonia raccordement is mandatory; a miss must raise rather
    # than silently zero it.
    text = fixture_text("mega_dynamic_w.pdf").replace(
        "Redevance de raccordement", "XXX"
    )
    with pytest.raises(ExtractorError, match="connection fee"):
        parse_snapshot("mega_dynamic", text, "wallonia")


def test_missing_flanders_renewables_is_fatal() -> None:
    # The combined green-energy / cogeneration line gone means the block
    # drifted; raise rather than silently zero the surcharge.
    text = fixture_text("mega_smart_fixed_v.pdf").replace("Cotisation", "XXX")
    with pytest.raises(ExtractorError, match="green-energy"):
        parse_snapshot("mega_smart_fixed", text, "flanders")


def test_wrong_region_card_is_rejected() -> None:
    # parse_snapshot applies region-specific DSO and levy overlays, so the
    # sibling-region URL fallback (#42) must never let a mismatched card
    # through. Feeding the Flanders Dynamic card as a Wallonia request is
    # rejected on the "Client résidentiel - <Region>" header, even though
    # both cards name every region in the cross-region Cotisation table.
    with pytest.raises(ExtractorError, match="not the wallonia edition"):
        parse_snapshot("mega_dynamic", fixture_text("mega_dynamic_v.pdf"), "wallonia")
    with pytest.raises(ExtractorError, match="not the flanders edition"):
        parse_snapshot(
            "mega_smart_fixed", fixture_text("mega_smart_fixed_b.pdf"), "flanders"
        )


def test_smart_flex_is_a_variable_contract() -> None:
    snap = parse_snapshot(
        "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    # Mega 'Flex' product values change month to month; just assert the
    # current rate is in a plausible Belgian residential range.
    assert 0.10 <= snap.energy.current <= 0.30


def test_offpeak_impact_parses_three_tier_rates() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, ImpactRates)
    # PIC is the most expensive band, ECO the cheapest -- enforced by
    # live_check too.
    assert snap.energy.pic > snap.energy.medium > snap.energy.eco
    # The three-tier table on this card is a 12-month forward simulation
    # (0.1011 / 0.1496 / 0.182). The card states the rates it actually bills
    # below it, as "les derniers prix constates ... pour le calcul de votre
    # facture de regularisation", and those are what an entry is charged.
    assert snap.energy.pic == pytest.approx(0.1578)
    assert snap.energy.medium == pytest.approx(0.1295)
    assert snap.energy.eco == pytest.approx(0.0866)
    assert snap.energy.yearly_fixed_fee == pytest.approx(74.2)
    # Formula text captures all three tiers from the footnote.
    assert snap.energy.formula is not None
    assert "Tarif ECO" in snap.energy.formula
    assert "Tarif MEDIUM" in snap.energy.formula
    assert "PIC" in snap.energy.formula


def test_offpeak_impact_injection_uses_per_tier_column() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    # The Impact card has no ``Compteur mono-horaire`` anchor, so the table
    # path reads injection as the second number under each Tarif row (0.0292,
    # all three rows equal). That table is the forward simulation though, so
    # the realized sentence wins and the credit is the 0.18 c€/kWh the card
    # says it settles.
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0018)


def test_offpeak_impact_wallonia_dsos_carry_impact_triplet() -> None:
    snap = parse_snapshot(
        "mega_offpeak_impact_var",
        fixture_text("mega_offpeak_impact_w.pdf"),
        "wallonia",
    )
    for dso_key, overlay in snap.dsos.items():
        assert overlay.distribution_pic is not None, dso_key
        assert overlay.distribution_medium is not None, dso_key
        assert overlay.distribution_eco is not None, dso_key
        # Same band ordering invariant as the supplier-side rates.
        assert (
            overlay.distribution_pic
            >= overlay.distribution_medium
            >= overlay.distribution_eco
        ), dso_key


def test_offpeak_impact_contract_is_wallonia_only() -> None:
    from custom_components.be_electricity_prices.const import (
        REGION_BRUSSELS,
        REGION_FLANDERS,
        REGION_WALLONIA,
    )

    contract = next(
        c for c in EXTRACTORS["mega"].contracts if c.id == "mega_offpeak_impact_var"
    )
    assert contract.regions == frozenset({REGION_WALLONIA})
    assert REGION_FLANDERS not in contract.regions
    assert REGION_BRUSSELS not in contract.regions


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Mega contract"):
            await EXTRACTORS["mega"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


# ---- discover() filters known-unsupported products ----------------------------


async def test_discover_filters_known_unsupported_products() -> None:
    """Mega's listing exposes prepaid topup-card products that this
    integration deliberately does not model. The catalog discovery
    must exclude them so the daily live-check doesn't re-open the
    same issue every day (regression: 2026-05-05)."""
    listing = (
        '<a data-product-element="Smart Fixed" href="x">'
        '<a data-product-element="Prepaid Fixed" href="y">'
        '<a data-product-element="Prepaid Flex" href="z">'
        '<a data-product-element="Hypothetical New" href="w">'
    )
    with patch.object(mega_mod, "_fetch_listing_html", return_value=listing):
        out = await mega_mod.discover(None)  # type: ignore[arg-type]
    assert "Smart Fixed" in out
    assert "Hypothetical New" in out
    assert "Prepaid Fixed" not in out
    assert "Prepaid Flex" not in out


def test_flex_extracts_cohort_coefficients() -> None:
    """The Flex prose formula (HTVA) is parsed into VAT-baked factor / base so a
    signing cohort re-prices against the monthly mean. Mega's Epex is already in
    c€/kWh, so the factor is not scaled by 10 (unlike BELIX / BELPEX cards)."""
    snap = parse_snapshot(
        "mega_smart_flex",
        fixture_text("mega_smart_flex_w.pdf"),
        "wallonia",
        "test://flex",
    )
    assert isinstance(snap.energy, VariableRates)
    # "Compteur mono-horaire : Epex * 1,1095 + 3,6 c€/kWh" (HTVA), TVA 6%:
    # factor 1,1095 * 1.06, base 3,6 * 1.06 / 100.
    assert snap.energy.formula_factor == pytest.approx(1.17607, rel=1e-4)
    assert snap.energy.formula_base == pytest.approx(0.038160, rel=1e-4)


def test_august_2026_flat_excise_replaces_the_tier_table() -> None:
    """On 2026-08-01 the federal scheme folded the separate energy
    contribution into the special excise and flattened it, so Mega's card
    dropped the consumption-tier table for a single "Accise speciale" value
    and deleted the contribution column. Mega renders the flat value with a
    DOT decimal where the tiered rows used commas."""
    from custom_components.be_electricity_prices.providers.mega import (
        _extract_energy_contribution,
        _extract_federal_excise,
    )

    august = "Accise spéciale\n(c€/kWh)\n4.876\n*\n"
    assert _extract_federal_excise(august) == pytest.approx(0.04876)
    assert _extract_energy_contribution(august) == 0.0

    july = "Consommation entre\n0 et 3000 kWh\n5,0329\n0,20417\n"
    assert _extract_federal_excise(july) == pytest.approx(0.050329)
    assert _extract_energy_contribution(july) == pytest.approx(0.0020417)


# ---- professional cards ------------------------------------------------------


def test_pro_contracts_are_registered_and_flagged() -> None:
    contracts = {c.id: c for c in EXTRACTORS["mega"].contracts}
    assert contracts["mega_pro_smart_fixed"].professional is True
    assert contracts["mega_smart_fixed"].professional is False
    # Online Flex and the Off-peak family have no B2B card.
    assert "mega_pro_online_flex" not in contracts
    assert "mega_pro_offpeak_flex" not in contracts
    # Zen Fixed is retired residentially but still published for business.
    assert "mega_pro_zen_fixed" in contracts
    assert "mega_zen_fixed" not in contracts


def test_pro_pdf_url_is_built_not_scraped() -> None:
    """Mega serves the professional cards from the same CDN but never
    links them from the public listing, so the filename is constructed."""
    from custom_components.be_electricity_prices.providers.mega import (
        _CONTRACTS_BY_ID,
        _pro_pdf_url,
    )

    url = _pro_pdf_url(_CONTRACTS_BY_ID["mega_pro_smart_fixed"], "WL", date(2026, 7, 9))
    assert url == (
        "https://my.mega.be/resources/tarif/"
        "Mega-FR-EL-B2B-WL-072026-Smart0107-Fixed.pdf"
    )
    # The flex variant drops the -Fixed suffix, the same way the
    # residential filenames do.
    assert _pro_pdf_url(
        _CONTRACTS_BY_ID["mega_pro_smart_flex"], "VL", date(2026, 8, 1)
    ).endswith("Mega-FR-EL-B2B-VL-082026-Smart0108.pdf")


async def test_pro_lane_has_no_probe() -> None:
    """The built URL only changes at a month boundary, so returning it as
    a freshness key would pin the snapshot for a whole month. The pro lane
    falls back to the time-based TTL instead."""
    assert await mega_mod.probe(AsyncMock(), "mega_pro_smart_fixed", "wallonia") is None


def test_pro_card_is_parsed_ex_vat_with_the_excise_schedule() -> None:
    snap = parse_snapshot(
        "mega_pro_smart_fixed", fixture_text("mega_pro_smart_fixed_w.pdf"), "wallonia"
    )
    assert snap.taxes.vat_rate == pytest.approx(0.21)
    assert snap.taxes.federal_excise_bands == (
        (20_000.0, pytest.approx(0.01421)),
        (50_000.0, pytest.approx(0.01209)),
        (1_000_000.0, pytest.approx(0.01139)),
    )
    # Read off the same tier rows, where the residential card no longer
    # prints it at all.
    assert snap.taxes.energy_contribution == pytest.approx(0.0019261)
    assert {"aieg", "aiesh", "ores", "resa", "rew"} <= set(snap.dsos)


def test_pro_injection_is_taxed() -> None:
    snap = parse_snapshot(
        "mega_pro_dynamic", fixture_text("mega_pro_dynamic_w.pdf"), "wallonia"
    )
    assert snap.injection is not None
    assert snap.injection.vat_applies is True
    res = parse_snapshot("mega_dynamic", fixture_text("mega_dynamic_w.pdf"), "wallonia")
    assert res.injection is not None
    assert res.injection.vat_applies is False


def test_pro_regulated_values_are_the_residential_ones_ex_vat() -> None:
    """Both editions carry the same regulated tables; the professional one
    prints them without the 6% VAT the residential one includes."""
    pro = parse_snapshot(
        "mega_pro_smart_fixed", fixture_text("mega_pro_smart_fixed_v.pdf"), "flanders"
    )
    res = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    pro_dso = pro.dsos["fluvius_antwerpen"]
    res_dso = res.dsos["fluvius_antwerpen"]
    assert pro_dso.distribution_single * 1.06 == pytest.approx(
        res_dso.distribution_single, rel=1e-3
    )
    assert pro_dso.data_management_per_year * 1.06 == pytest.approx(
        res_dso.data_management_per_year, rel=1e-3
    )


def test_pro_flanders_bills_the_base_energy_fund() -> None:
    """A business connection pays "Montant de base"; the professional card
    simply omits the reduced row a domiciled household would pay. The value
    used to be hardcoded 0.0, dropping 120,84 EUR/yr."""
    snap = parse_snapshot(
        "mega_pro_smart_fixed", fixture_text("mega_pro_smart_fixed_v.pdf"), "flanders"
    )
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(10.07)


def test_residential_flanders_bills_the_reduced_energy_fund() -> None:
    """A domiciled household pays "Montant réduit", 0.00 on this card, and
    must never fall through to the 10,07 base amount printed above it."""
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    assert snap.taxes.energy_fund_eur_per_month == 0.0


def test_energy_fund_is_flanders_only() -> None:
    """Wallonia and Brussels cards carry no Fonds Energie block at all."""
    for cid, fx, region in (
        ("mega_pro_smart_fixed", "mega_pro_smart_fixed_w.pdf", "wallonia"),
        ("mega_smart_fixed", "mega_smart_fixed_b.pdf", "brussels"),
    ):
        snap = parse_snapshot(cid, fixture_text(fx), region)
        assert snap.taxes.energy_fund_eur_per_month == 0.0


def test_pro_card_region_header_is_still_checked() -> None:
    """The gate now accepts "client professionnel" as well, but must keep
    rejecting the wrong region: a wrong-region card mis-prices silently."""
    text = fixture_text("mega_pro_smart_fixed_w.pdf")
    with pytest.raises(ExtractorError, match="not the flanders edition"):
        parse_snapshot("mega_pro_smart_fixed", text, "flanders")


def test_variable_prefers_the_realized_rates_over_the_simulation_table() -> None:
    """A variable card's headline table is a 12-month forward simulation and
    the card says so; the rates it actually bills are printed underneath as
    "les derniers prix constates et utilises pour le calcul de votre facture
    de regularisation".

    Reading the table billed 17,42 c€/kWh where Mega settles 15,30, about
    74 EUR/yr at 3500 kWh, and credited injection at 3,84 where it pays 2,32.
    """
    snap = parse_snapshot(
        "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.153)
    assert snap.energy.peak == pytest.approx(0.1756)
    assert snap.energy.offpeak == pytest.approx(0.1355)
    assert snap.energy.exclusive_night == pytest.approx(0.1355)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.0232)


def test_a_negative_realized_injection_keeps_its_sign() -> None:
    """Some months Mega settles injection BELOW zero: every May 2026 card
    prints "Injection : -0.32", a month the customer pays to inject.

    The value regex accepted digits only, so the key went missing and
    _extract_injection fell back to the 12-month simulation table, crediting
    +2,42 c€/kWh against a billed -0,32. Wrong sign, 82 EUR out over 3000 kWh
    injected, and nothing in the parse looked wrong.
    """
    from custom_components.be_electricity_prices.providers.mega import _realized_rates

    body = (
        "Les derniers prix constates et utilises pour le calcul de votre "
        "facture de regularisation pour le mois de mai sont les suivants "
        "(c€/kWh) : Compteur monohoraire : 12.67; Jour : 13.86; Nuit : 11.86; "
        "Exclusif nuit : 11.86; Injection : -0.32. Pour plus de renseignements"
    )
    rates = _realized_rates(body)
    assert rates["injection"] == pytest.approx(-0.0032)
    # The consumption legs are unaffected by the change.
    assert rates["mono"] == pytest.approx(0.1267)
    assert rates["peak"] == pytest.approx(0.1386)
    assert rates["offpeak"] == pytest.approx(0.1186)
    assert rates["exclusive_night"] == pytest.approx(0.1186)


def test_a_page_break_in_the_realized_sentence_does_not_kill_the_block() -> None:
    """The two block anchors must tolerate a colon between them.

    Where the sentence straddles a page break the extractor splices the page
    footer into it, and that footer is full of colons ("Sources d'energie
    pour :", "votre produit :"). A colon-free gap then failed to match and the
    whole override quietly no-opped for that month, leaving one month of a
    year-to-date walk on the simulation table while its neighbours used the
    realized rates.
    """
    from custom_components.be_electricity_prices.providers.mega import _realized_rates

    spliced = (
        "Les derniers prix constates et utilises pour le calcul de votre "
        "facture de regularisation pour le mois de juin\n"
        "Sources d'energie pour :\nvotre produit : \n 100% verte\n"
        "la region wallonne (telles qu'approuvees par la CWaPE) : 89,5% verte\n"
        "et 10,5% grise\nPrix du \nmois 07/2026\n - \n"
        "TVA 6% incluse - Publie le 30-06-2026\n 1 / 3\nPower Online SA "
        "sont les suivants (c€/kWh) : Compteur mono-horaire : 17.99; "
        "Jour : 19.8; Nuit : 16.7; Exclusif nuit : 16.7 ; Injection : 3.63."
    )
    rates = _realized_rates(spliced)
    assert rates["mono"] == pytest.approx(0.1799)
    assert rates["peak"] == pytest.approx(0.198)
    assert rates["offpeak"] == pytest.approx(0.167)
    assert rates["injection"] == pytest.approx(0.0363)


def test_a_collided_value_token_is_refused_not_truncated() -> None:
    """The June 2026 Flanders cards collide two runs in the text layer.

    "Compteur mono- horaire : 16.76.38" is not a number. With no right-hand
    boundary the pattern took the well-formed prefix 16.76 -- which is the
    Jour value on that same card -- and billed it as mono, giving
    mono == peak while offpeak was 14,20, a combination the card cannot
    print. Refusing the token drops the key and the caller falls back to the
    headline table for that field.
    """
    from custom_components.be_electricity_prices.providers.mega import _realized_rates

    body = (
        "Les derniers prix constates et utilises pour le calcul de votre "
        "facture de regularisation pour le mois de mai sont les suivants "
        "(c€/kWh) : Compteur mono- horaire : 16.76.38; Jour : 16.76; "
        "Nuit : 14.2; Exclusif nuit : 14.2 ; Injection : 1.4. Pour plus"
    )
    rates = _realized_rates(body)
    assert "mono" not in rates
    # Every well-formed value on the same line still parses, including the
    # one closing the sentence with a period.
    assert rates["peak"] == pytest.approx(0.1676)
    assert rates["offpeak"] == pytest.approx(0.142)
    assert rates["exclusive_night"] == pytest.approx(0.142)
    assert rates["injection"] == pytest.approx(0.014)


def test_fixed_and_dynamic_cards_keep_reading_their_own_tables() -> None:
    """Only variable and Impact cards carry the simulation disclaimer. A
    fixed card's table IS the billed rate, so it must be untouched."""
    from custom_components.be_electricity_prices.providers.mega import _realized_rates

    assert _realized_rates(fixture_text("mega_smart_fixed_v.pdf")) == {}
    assert _realized_rates(fixture_text("mega_dynamic_w.pdf")) == {}
    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1718)


def test_pro_variable_cohort_coefficients_are_not_vat_baked() -> None:
    """A professional card is published Hors TVA and its snapshot carries
    vat_rate 0,21, so the cohort coefficients must stay ex-VAT.

    vat_multiplier falls back to the residential 1,06 when its pattern misses,
    and a pro card never prints "TVA N% incluse", so the shared call baked 6%
    into an ex-VAT formula and inflated a pro entry's whole energy leg.
    """
    from custom_components.be_electricity_prices.providers.mega import (
        _variable_cohort_coefficients,
    )

    text = fixture_text("mega_smart_flex_w.pdf")
    res_factor, res_base = _variable_cohort_coefficients(text, professional=False)
    pro_factor, pro_base = _variable_cohort_coefficients(text, professional=True)
    assert res_factor is not None and pro_factor is not None
    assert res_base is not None and pro_base is not None
    # Same card, same printed formula: the residential read bakes 6%, the
    # professional read does not.
    assert res_factor == pytest.approx(pro_factor * 1.06)
    assert res_base == pytest.approx(pro_base * 1.06)


def test_returned_offpeak_fixed_parses_in_every_region() -> None:
    """Mega pulled Off-peak Fixed in July 2026 and republished it for August,
    in all three regions. It parses on the existing fixed path: the card is an
    ordinary bi-hourly fixed card, so re-adding it needed no parser change."""
    snap = parse_snapshot(
        "mega_offpeak_fixed", fixture_text("mega_offpeak_fixed_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1932)
    assert snap.energy.peak == pytest.approx(0.2350)
    assert snap.energy.offpeak == pytest.approx(0.1610)
    assert snap.energy.yearly_fixed_fee == pytest.approx(74.2)
    # The August flat excise, not the retired tier table.
    assert snap.taxes.federal_excise == pytest.approx(0.04876)
    assert len(snap.dsos) == 5


def test_returned_offpeak_fixed_has_a_professional_edition() -> None:
    """The residential product came back WITH a B2B card, which it did not
    have before July. It prices ex-VAT with the degressive excise schedule and
    carries the Flemish energy fund a business connection pays."""
    snap = parse_snapshot(
        "mega_pro_offpeak_fixed",
        fixture_text("mega_pro_offpeak_fixed_v.pdf"),
        "flanders",
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.taxes.vat_rate == pytest.approx(0.21)
    assert snap.taxes.federal_excise_bands is not None
    assert snap.energy.yearly_fixed_fee == pytest.approx(70.0)
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(10.07)


def test_returned_offpeak_fixed_builds_its_b2b_url() -> None:
    """The B2B filename grammar for the returned product:
    Offpeak-Bi + 01 + MM + -Fix."""
    from datetime import date

    from custom_components.be_electricity_prices.providers.mega import (
        _CONTRACTS_BY_ID,
        _pro_pdf_url,
    )

    url = _pro_pdf_url(
        _CONTRACTS_BY_ID["mega_pro_offpeak_fixed"], "VL", date(2026, 8, 1)
    )
    assert url.endswith("Mega-FR-EL-B2B-VL-082026-Offpeak-Bi0108-Fix.pdf")


def test_next_month_rolls_the_year() -> None:
    from custom_components.be_electricity_prices.providers.mega import _next_month

    assert _next_month(date(2026, 6, 1)) == date(2026, 7, 1)
    assert _next_month(date(2026, 12, 1)) == date(2027, 1, 1)


async def test_archive_bills_each_month_at_its_own_realized_rate() -> None:
    """A Mega card reports the PREVIOUS month's regularisation figures.

    "Les derniers prix constates ... pour le mois de <month>" on the June card
    names May. On the live path that lag is unavoidable -- June's index does
    not exist yet -- but the archive walk was taking it as June's price, so
    every past month of the year-to-date was billed at the month before it,
    while the figure that actually bills it sat unread on the next card.
    Verified on four consecutive real cards: June was billed 14,24 c€/kWh
    (May's) where Mega settled 16,95.

    Only the energy and injection legs move. The DSO and tax overlays, the
    yearly fee and the cohort coefficients stay the delivery month's, because
    those really are properties of its own card.
    """
    from custom_components.be_electricity_prices.providers import mega as mega_mod

    snap = parse_snapshot(
        "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    contract = mega_mod._CONTRACTS_BY_ID["mega_smart_flex"]
    before_fee = snap.energy.yearly_fixed_fee
    before_dsos = dict(snap.dsos)
    before_taxes = snap.taxes

    async def _realized(*_a: object, **_k: object) -> dict[str, float]:
        return {
            "mono": 0.1799,
            "peak": 0.198,
            "offpeak": 0.167,
            "exclusive_night": 0.167,
            "injection": 0.0363,
        }

    with patch.object(mega_mod, "_realized_rates_for_month", new=_realized):
        out = await mega_mod._apply_realized_for_month(
            None,  # type: ignore[arg-type]
            contract,
            "wallonia",
            "WL",
            date(2026, 6, 1),
            snap,
        )
    assert isinstance(out.energy, VariableRates)
    assert out.energy.current == pytest.approx(0.1799)
    assert out.energy.peak == pytest.approx(0.198)
    assert out.energy.offpeak == pytest.approx(0.167)
    assert out.injection is not None
    assert out.injection.current == pytest.approx(0.0363)
    # Everything that belongs to the delivery month's own card is untouched.
    assert out.energy.yearly_fixed_fee == pytest.approx(before_fee)
    assert out.dsos == before_dsos
    assert out.taxes == before_taxes


async def test_archive_keeps_its_own_rates_when_the_next_card_is_absent() -> None:
    """The newest month has no following card yet, so it keeps the figures it
    has -- the behaviour before this, and still the best available."""
    from custom_components.be_electricity_prices.providers import mega as mega_mod

    snap = parse_snapshot(
        "mega_smart_flex", fixture_text("mega_smart_flex_w.pdf"), "wallonia"
    )
    contract = mega_mod._CONTRACTS_BY_ID["mega_smart_flex"]

    async def _none(*_a: object, **_k: object) -> dict[str, float]:
        return {}

    with patch.object(mega_mod, "_realized_rates_for_month", new=_none):
        out = await mega_mod._apply_realized_for_month(
            None,  # type: ignore[arg-type]
            contract,
            "wallonia",
            "WL",
            date(2026, 6, 1),
            snap,
        )
    assert out is snap


async def test_a_fixed_card_is_never_re_read_from_the_next_month() -> None:
    """Only variable and Impact cards carry the simulation disclaimer; a fixed
    card's table IS its billed rate, so it must not trigger the extra fetch."""
    from custom_components.be_electricity_prices.providers import mega as mega_mod

    snap = parse_snapshot(
        "mega_smart_fixed", fixture_text("mega_smart_fixed_v.pdf"), "flanders"
    )
    contract = mega_mod._CONTRACTS_BY_ID["mega_smart_fixed"]

    async def _boom(*_a: object, **_k: object) -> dict[str, float]:
        raise AssertionError("a fixed contract must not fetch the next card")

    with patch.object(mega_mod, "_realized_rates_for_month", new=_boom):
        out = await mega_mod._apply_realized_for_month(
            None,  # type: ignore[arg-type]
            contract,
            "flanders",
            "VL",
            date(2026, 6, 1),
            snap,
        )
    assert out is snap
