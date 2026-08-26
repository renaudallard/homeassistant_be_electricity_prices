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

"""Bolt PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers import bolt as bolt_mod
from custom_components.be_electricity_prices.pricing import compute_breakdown
from tests import FIXTURES, fixture_text
from custom_components.be_electricity_prices.providers.base import (
    CardNotReadableError,
    DynamicRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.bolt import parse_snapshot


def test_bolt_is_registered() -> None:
    assert "bolt" in EXTRACTORS
    assert EXTRACTORS["bolt"].label == "Bolt"
    contract_ids = {c.id for c in EXTRACTORS["bolt"].contracts}
    assert "bolt_fix" in contract_ids
    assert "bolt_variable" in contract_ids
    assert "bolt_dynamic" in contract_ids
    # Seven residential products, plus the seven professional editions.
    assert len(contract_ids) == 14


def test_fix_yearly_fee_is_monthly_x_12() -> None:
    # Bolt prints the platform fee per month (€10,99/mois). The
    # integration's yearly_fixed_fee carries the EUR/year amount, so the
    # parser multiplies by 12.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.yearly_fixed_fee == pytest.approx(10.99 * 12.0)


def test_fix_extracts_consumption_rates() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, FixedRates)
    # Bolt Fix prints all four meter rates as 16,71 c€/kWh.
    assert snap.energy.single == pytest.approx(0.1671)
    assert snap.energy.peak == pytest.approx(0.1671)
    assert snap.energy.offpeak == pytest.approx(0.1671)
    assert snap.energy.exclusive_night == pytest.approx(0.1671)


def test_injection_carries_the_quarter_hourly_formula() -> None:
    """The printed figure is an illustration, not the rate. The card says so:
    "Le tableau ci-dessus indique le prix de vente base sur la valeur Belpex
    la plus recente. Dans la facturation, l'injection par quart d'heure est
    multipliee par la valeur Belpex pour ce quart d'heure." The fixed card
    goes further: "Contrairement au prix fixe de consommation ..., le prix
    pour l'injection est quant a lui variable selon l'indice Belpex."

    The figure survives as the fallback for an entry with no ENTSO-E key.
    """
    for cid, fixture in (
        ("bolt_fix", "bolt_fix.pdf"),
        ("bolt_variable", "bolt_variable.pdf"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture, layout=True), "wallonia")
        inj = snap.injection
        assert inj is not None, cid
        assert inj.current == pytest.approx(0.0531), cid
        assert inj.factor == pytest.approx(0.94), cid
        assert inj.base == pytest.approx(-11.33 / 1000.0), cid
        assert inj.slot_indexed is True, cid


def test_a_slot_indexed_credit_beats_its_printed_figure() -> None:
    """slot_indexed exists to stop the engine preferring a printed ``current``
    on a card whose ENERGY is static. Without it a Bolt fixed entry credits
    5,31 c/kWh every quarter of the year, and can never show the negative
    rates the contract really pays: 15% of Apr-Aug 2026 quarters are below
    zero, which a flat positive figure cannot express.
    """
    from custom_components.be_electricity_prices.injection import (
        _injection_is_spot_formula,
    )

    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    inj = snap.injection
    assert inj is not None
    assert _injection_is_spot_formula(inj, snap.energy) is True
    # A quarter at 5 EUR/MWh is a NEGATIVE credit; the flat figure is +0,0531.
    assert inj.factor * 0.005 + inj.base < 0.0


def test_injection_accepts_negative_second_column() -> None:
    # The July 2026 fix cards print a NEGATIVE "Exclusif nuit" second
    # injection column ("Prix mensuel 3,40 -0,43"). Only the first column
    # is billed, but the second is a required anchor token, so the parser
    # must tolerate its minus sign instead of returning None.
    text = "Injection\nPrix mensuel 3,40 -0,43 Compteur\n"
    inj = bolt_mod._extract_injection(text, "fixed")
    assert inj is not None
    assert inj.current == pytest.approx(0.034)
    # This stub carries no formula table, so the figure is all there is and
    # crediting it flat beats crediting nothing.
    assert inj.factor is None
    assert inj.base is None
    assert inj.slot_indexed is False


def test_variable_missing_bihourly_rates_fails_loud() -> None:
    # Variable cards always publish distinct Jour/Nuit rates; if the
    # bi-horaire block drifts, raise rather than silently bill at mono.
    text = fixture_text("bolt_variable.pdf", layout=True).replace("Jour", "XXX")
    with pytest.raises(ExtractorError, match="bi-hourly"):
        parse_snapshot("bolt_variable", text, "wallonia")


def test_variable_uses_current_monthly_not_annual_estimate() -> None:
    # The bihoraire block lists the annual estimate first
    # (15,20 / 15,20) then the current monthly (14,56 / 12,09). Anchor
    # on the trailing 'Jour Nuit' header to skip the annual values.
    snap = parse_snapshot(
        "bolt_variable", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1325)
    assert snap.energy.peak == pytest.approx(0.1456)
    assert snap.energy.offpeak == pytest.approx(0.1209)
    assert snap.energy.exclusive_night == pytest.approx(0.1209)


def test_taxes_split_correctly_per_region() -> None:
    fl = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders"
    )
    wa = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    bx = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "brussels"
    )
    # Federal excise + energy contribution are nationwide.
    assert fl.taxes.federal_excise == pytest.approx(0.050329)
    assert fl.taxes.energy_contribution == pytest.approx(0.002042)
    # Flanders renewables: certificats verts + WKK. Bolt's WKK row prints
    # a single-digit footnote ref before the value ('WKK (c€/kWh) 8 0,39')
    # which the parser must skip.
    assert fl.taxes.flanders_renewables == pytest.approx((1.17 + 0.39) / 100.0)
    assert fl.taxes.region_connection_fee == 0.0
    # Wallonia: green-energy + connection fee.
    assert wa.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert wa.taxes.region_connection_fee == pytest.approx(0.00075)
    # Brussels: green-energy only.
    assert bx.taxes.brussels_renewables == pytest.approx(0.0269)
    assert bx.taxes.region_connection_fee == 0.0


def test_wallonia_dso_handles_vertical_layout() -> None:
    # pdfplumber renders Bolt's Wallonia rows with each value on its
    # own line: "AIEG\n 10,58\n 11,77\n 6,38\n ...". The regex uses
    # `\s+` (which matches newlines) between values to handle this.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1058)
    assert aieg.distribution_peak == pytest.approx(0.1177)
    assert aieg.distribution_offpeak == pytest.approx(0.0638)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_resa_is_cheaper_than_rew_after_label_swap() -> None:
    # Bolt's PDF renders the Liege (RESA / TECTEO) and Wavre (REW /
    # Régie de Wavre) rows under swapped labels in pdfplumber's text
    # extraction; bolt.py compensates with an inverted dict. Across
    # every other supplier in the registry, RESA's distribution_single
    # is consistently lower than REW's. If a future Bolt PDF or
    # pdfplumber release fixes the upstream layout silently, the swap
    # would invert correct pricing — this assertion catches that.
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "wallonia"
    )
    assert snap.dsos["resa"].distribution_single < snap.dsos["rew"].distribution_single


def test_flanders_dso_includes_transport_in_distribution() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders"
    )
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.transport == 0.0
    assert antwerpen.distribution_single == pytest.approx(0.0535)
    # Dedicated exclusive-night circuit rate (group 4), lower than the
    # normal digital distribution so a night meter isn't billed the day rate.
    excl = antwerpen.distribution_exclusive_night
    assert excl == pytest.approx(0.0481)
    assert excl is not None and excl < antwerpen.distribution_single
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(52.37)


def test_brussels_extracts_sibelga() -> None:
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "brussels"
    )
    sibelga = snap.dsos["sibelga"]
    assert sibelga.distribution_single == pytest.approx(0.0996)
    assert sibelga.distribution_offpeak == pytest.approx(0.0753)
    # The exclusive-night column is now wired (was dropped, falling back
    # to off-peak); 7,53 c€/kWh on this card.
    assert sibelga.distribution_exclusive_night == pytest.approx(0.0753)
    assert sibelga.transport == pytest.approx(0.0227)


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Bolt contract"):
            await EXTRACTORS["bolt"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_fetch_for_month_rejects_mismatched_month() -> None:
    # The fix card is URL-keyed by month but carries no parseable
    # valid_until, so fetch_for_month cross-checks the printed
    # "<Month> <Year>" header against the requested month. The April
    # fixture is accepted for April and rejected for January, so a CDN
    # serving the wrong month can't mis-bill a past month at its rates.
    from custom_components.be_electricity_prices.providers import bolt

    april_text = fixture_text("bolt_fix.pdf", layout=True)

    async def _run() -> None:
        with patch.object(
            bolt, "fetch_pdf_text_layout", new=AsyncMock(return_value=april_text)
        ):
            accepted = await bolt.fetch_for_month(
                None,  # type: ignore[arg-type]
                "bolt_fix",
                "wallonia",
                date(2026, 4, 1),
            )
            assert accepted is not None
            rejected = await bolt.fetch_for_month(
                None,  # type: ignore[arg-type]
                "bolt_fix",
                "wallonia",
                date(2026, 1, 1),
            )
            assert rejected is None

    asyncio.run(_run())


def test_fetch_for_month_covers_the_whole_fix_folder() -> None:
    """The month archive is keyed on the FOLDER, not on the slug.

    Bolt's fix folder addresses every card in it by month, residential and
    professional, ``fix`` and ``plenty_fix`` alike; only the variable folder
    uses a stable version-number suffix. Requiring the slug to be ``fix`` too
    locked the two plenty_fix contracts out of an archive that exists, so a
    one-year fixed contract signed in January was priced all year at the
    current card, which is the whole point of a fixed product."""
    from custom_components.be_electricity_prices.providers import bolt

    april_text = fixture_text("bolt_fix.pdf", layout=True)

    async def _run() -> None:
        with patch.object(
            bolt, "fetch_pdf_text_layout", new=AsyncMock(return_value=april_text)
        ):
            for contract_id in (
                "bolt_fix",
                "bolt_plenty_fix",
                "bolt_pro_fix",
                "bolt_pro_plenty_fix",
            ):
                got = await bolt.fetch_for_month(
                    None,  # type: ignore[arg-type]
                    contract_id,
                    "wallonia",
                    date(2026, 4, 1),
                )
                assert got is not None, contract_id
            # The variable folder still has no month-addressable card.
            for contract_id in ("bolt_variable", "bolt_plenty", "bolt_dynamic"):
                assert (
                    await bolt.fetch_for_month(
                        None,  # type: ignore[arg-type]
                        contract_id,
                        "wallonia",
                        date(2026, 4, 1),
                    )
                    is None
                ), contract_id

    asyncio.run(_run())


def test_dynamic_extracts_belpex_formula() -> None:
    """Bolt Dynamic reads the same variable card but applies the printed
    formula to the quarter-hourly Belpex spot. The card prints EUR/MWh HTVA:
    consumption ``Belpex * 1,1192 + 13,94``, injection ``Belpex * 0,94 - 11,33``.
    Converted to EUR/kWh on the EUR/kWh spot, VAT-baked for energy (snapshot
    vat_rate 0), VAT-exempt for injection."""
    snap = parse_snapshot(
        "bolt_dynamic", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True
    assert snap.energy.factor == pytest.approx(1.1192 * 1.06)
    assert snap.energy.base == pytest.approx(13.94 / 1000.0 * 1.06)
    # Cross-check: at the card's implied Belpex (~0.0992 EUR/kWh) the formula
    # reproduces Bolt Variable's validated resolved rate (0.1325 EUR/kWh TVAC).
    assert snap.energy.factor * 0.0992 + snap.energy.base == pytest.approx(
        0.1325, abs=1e-3
    )
    # Injection is spot-indexed (factor/base, current None), VAT-exempt.
    assert isinstance(snap.injection, InjectionRates)
    assert snap.injection.current is None
    assert snap.injection.factor == pytest.approx(0.94)
    assert snap.injection.base == pytest.approx(-11.33 / 1000.0)


def test_dynamic_injection_selected_by_factor_not_position() -> None:
    """The dynamic injection row is the Belpex formula whose factor is < 1
    (Bolt redistributes a fraction of the spot). Even when the card prints
    per-meter-type consumption rows with DIFFERING factors, the parser must
    pick the injection row, not the first consumption row that differs from
    the first."""
    text = (
        "Consommation\n"
        "Simple Belpex * 1,10 + 13,94\n"
        "Jour Belpex * 1,12 + 13,94\n"
        "Nuit Belpex * 1,11 + 13,94\n"
        "Injection\n"
        "Injection nuit Belpex * 0,94 - 11,33\n"
    )
    inj = bolt_mod._extract_injection(text, "dynamic")
    assert inj is not None
    assert inj.current is None
    assert inj.factor == pytest.approx(0.94)
    assert inj.base == pytest.approx(-11.33 / 1000.0)


def test_variable_energy_unchanged_by_dynamic_addition() -> None:
    """Adding the dynamic contract must not change how the variable card
    prices its resolved monthly ENERGY rate.

    This used to assert that the variable card's INJECTION carried no
    factor/base either. That was never what the card said - the same Belpex
    formula table sits on the variable card as on the dynamic one - so the
    assertion pinned the defect rather than the intent. The energy half is
    the part this test exists for.
    """
    snap = parse_snapshot(
        "bolt_variable", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1325)
    assert snap.energy.formula_factor is None


def test_publication_month_tolerates_a_misspelled_accent() -> None:
    """Bolt's August 2026 fixed card prints "Aôut 2026" - the circumflex on
    the wrong vowel - and an exact accent class blanked the label on that
    typo. The header is a display label that never feeds pricing, so a
    misspelling must still produce a label rather than an empty string."""
    from custom_components.be_electricity_prices.providers.bolt import (
        _extract_publication_month,
    )

    assert _extract_publication_month("Aôut 2026 /Résidentiel\n") == "Aôut 2026"
    # The correctly spelled months keep working.
    for header in ("Août 2026", "Juin 2026", "Février 2026", "Décembre 2025"):
        assert _extract_publication_month(f"{header} /Résidentiel\n") == header
    # A line that is not a month header still yields no label.
    assert _extract_publication_month("Carte Tarifaire\nBolt Fixe\n") == ""


# ---- professional cards ------------------------------------------------------


def test_pro_contracts_are_registered_and_flagged() -> None:
    contracts = {c.id: c for c in EXTRACTORS["bolt"].contracts}
    assert contracts["bolt_pro_variable"].professional is True
    assert contracts["bolt_variable"].professional is False
    assert "bolt_pro_dynamic" in contracts


def test_pro_document_url_swaps_the_segment() -> None:
    from custom_components.be_electricity_prices.providers.bolt import (
        _CONTRACTS_BY_ID,
        _document_url,
    )

    # Pin the segment, not the version: the variable version is resolved
    # from the listing at fetch time, so asserting a number here is what
    # let a stale pin sit unnoticed for ten weeks.
    assert _document_url(_CONTRACTS_BY_ID["bolt_variable"], suffix="13").endswith(
        "/var/bolt_res_el_fr_13.pdf"
    )
    assert _document_url(_CONTRACTS_BY_ID["bolt_pro_variable"], suffix="13").endswith(
        "/var/bolt_pro_el_fr_13.pdf"
    )


# Deliberately a version AHEAD of _VARIABLE_SUFFIX_FALLBACK. Pinning the
# fixture to the fallback value made every assertion below satisfiable by
# the fallback itself, so deleting the resolver outright still passed.
_LISTING_HTML = """
<a href="https://files.boltenergie.be/pricelists/fix/fix_res_el_fr_202608.pdf">fix</a>
<a href="https://files.boltenergie.be/pricelists/var/bolt_res_el_fr_9.pdf">old</a>
<a href="https://files.boltenergie.be/pricelists/var/bolt_res_el_fr_14.pdf">current</a>
<a href="https://files.boltenergie.be/pricelists/var/bolt_pro_el_fr_14.pdf">pro</a>
<a href="https://files.boltenergie.be/pricelists/var/online_res_el_fr_14.pdf">online</a>
"""

# The four variable slugs move in lockstep today, but nothing makes them.
_LISTING_HTML_LAGGING = """
<a href="https://files.boltenergie.be/pricelists/var/bolt_res_el_fr_13.pdf">bolt</a>
<a href="https://files.boltenergie.be/pricelists/var/online_res_el_fr_12.pdf">online</a>
<a href="https://files.boltenergie.be/pricelists/var/plenty_res_el_fr_11.pdf">plenty</a>
"""


def _var(contract_id: str) -> bolt_mod._ContractDef:
    return bolt_mod._CONTRACTS_BY_ID[contract_id]


def test_variable_suffix_is_resolved_numerically_from_the_listing() -> None:
    # Bolt bumps the variable version in place and leaves every superseded
    # file served, so the stale URL keeps answering 200 with an old card.
    # The listing is the only signal, and "9" must not outrank "13".
    async def _run() -> None:
        with patch.object(
            bolt_mod, "fetch_text", new=AsyncMock(return_value=_LISTING_HTML)
        ):
            suffix = await bolt_mod._resolve_variable_suffix(
                None,  # type: ignore[arg-type]
                _var("bolt_variable"),
            )
            assert suffix == "14"
            # Not the fallback: a resolver that silently stopped working
            # would return that and this assertion must not accept it.
            assert suffix != bolt_mod._VARIABLE_SUFFIX_FALLBACK

    asyncio.run(_run())


@pytest.mark.parametrize(
    "listing",
    [
        pytest.param(ExtractorError("listing down"), id="unreadable"),
        pytest.param(
            "<a href='/pricelists/fix/fix_res_el_fr_202608.pdf'>f</a>", id="no-var-card"
        ),
    ],
)
def test_variable_suffix_falls_back_when_the_listing_gives_nothing(
    listing: str | ExtractorError,
) -> None:
    # A listing outage must degrade to a known version, not fail every
    # Bolt entry: the card itself is still being served.
    mock = (
        AsyncMock(side_effect=listing)
        if isinstance(listing, ExtractorError)
        else AsyncMock(return_value=listing)
    )

    async def _run() -> None:
        with patch.object(bolt_mod, "fetch_text", new=mock):
            resolved = await bolt_mod._resolve_variable_suffix(
                None,  # type: ignore[arg-type]
                _var("bolt_variable"),
            )
            assert resolved == bolt_mod._VARIABLE_SUFFIX_FALLBACK

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("contract_id", "expected"),
    [
        pytest.param("bolt_variable", "13", id="leading-slug"),
        pytest.param("bolt_online", "12", id="lagging-slug"),
        pytest.param("bolt_plenty", "11", id="furthest-behind-slug"),
    ],
)
def test_variable_suffix_is_resolved_per_slug(contract_id: str, expected: str) -> None:
    # A global max across the variable family would hand a lagging slug a
    # version that does not exist for it, turning a stale card into a 404
    # for that product.
    async def _run() -> None:
        with patch.object(
            bolt_mod, "fetch_text", new=AsyncMock(return_value=_LISTING_HTML_LAGGING)
        ):
            resolved = await bolt_mod._resolve_variable_suffix(
                None,  # type: ignore[arg-type]
                _var(contract_id),
            )
            assert resolved == expected

    asyncio.run(_run())


def test_variable_suffix_does_not_borrow_the_other_segment() -> None:
    # res and pro are separate files at the same path. A pro card missing
    # from the listing must fall back, not silently take the res version.
    res_only = '<a href="/pricelists/var/bolt_res_el_fr_13.pdf">res</a>'

    async def _run() -> None:
        with patch.object(bolt_mod, "fetch_text", new=AsyncMock(return_value=res_only)):
            resolved = await bolt_mod._resolve_variable_suffix(
                None,  # type: ignore[arg-type]
                _var("bolt_pro_variable"),
            )
            assert resolved == bolt_mod._VARIABLE_SUFFIX_FALLBACK

    asyncio.run(_run())


def test_variable_fetch_uses_the_resolved_version_not_a_pin() -> None:
    # Regression: a hardcoded _VARIABLE_SUFFIX billed June's formula for
    # ten weeks after Bolt shipped _13, with no error to notice.
    text = fixture_text("bolt_variable.pdf", layout=True)
    seen: list[str] = []

    async def _capture(_session: object, url: str, **_kw: object) -> str:
        seen.append(url)
        return text

    async def _run() -> None:
        with (
            patch.object(
                bolt_mod, "fetch_text", new=AsyncMock(return_value=_LISTING_HTML)
            ),
            patch.object(bolt_mod, "fetch_pdf_text_layout", new=_capture),
        ):
            await bolt_mod.fetch(None, "bolt_variable", "flanders")  # type: ignore[arg-type]

    asyncio.run(_run())
    assert seen == ["https://files.boltenergie.be/pricelists/var/bolt_res_el_fr_14.pdf"]


def test_fix_fetch_does_not_consult_the_listing() -> None:
    # Fixed cards are URL-keyed by month and need no version lookup;
    # spending a listing round-trip on them would be pure waste.
    text = fixture_text("bolt_fix.pdf", layout=True)
    listing = AsyncMock(return_value=_LISTING_HTML)

    async def _run() -> None:
        with (
            patch.object(bolt_mod, "fetch_text", new=listing),
            patch.object(
                bolt_mod, "fetch_pdf_text_layout", new=AsyncMock(return_value=text)
            ),
        ):
            await bolt_mod.fetch(None, "bolt_fix", "flanders")  # type: ignore[arg-type]

    asyncio.run(_run())
    listing.assert_not_awaited()


def test_pro_card_is_parsed_ex_vat() -> None:
    snap = parse_snapshot(
        "bolt_pro_variable",
        fixture_text("bolt_pro_variable.pdf", layout=True),
        "flanders",
    )
    assert snap.taxes.vat_rate == pytest.approx(0.21)
    assert snap.injection is not None
    assert snap.injection.vat_applies is True


def test_pro_inline_bihourly_row_is_not_read_as_exclusive_night() -> None:
    """Some Bolt renders inline all four columns on the Prix mensuel row
    (mono, Jour, Nuit, Exclusif nuit) instead of two. Reading that with
    the two-number rule takes Jour for the exclusive-night rate and bills
    a night circuit at the day price."""
    snap = parse_snapshot(
        "bolt_pro_variable",
        fixture_text("bolt_pro_variable.pdf", layout=True),
        "flanders",
    )
    assert isinstance(snap.energy, VariableRates)
    # Card row: Prix mensuel 12,62  13,85  11,53  11,53
    assert snap.energy.current == pytest.approx(0.1262)
    assert snap.energy.peak == pytest.approx(0.1385)
    assert snap.energy.offpeak == pytest.approx(0.1153)
    assert snap.energy.exclusive_night == pytest.approx(0.1153)


def test_pro_dynamic_formula_is_not_vat_scaled() -> None:
    """The professional card drops the "N% TVA" phrase the multiplier
    reads. Falling back to the residential 6% default would scale the
    formula on a card that is already ex-VAT, and vat_rate would then
    scale it again."""
    snap = parse_snapshot(
        "bolt_pro_dynamic",
        fixture_text("bolt_pro_variable.pdf", layout=True),
        "flanders",
    )
    assert isinstance(snap.energy, DynamicRates)
    # Card formula: 1,1192 x Belpex + 15,10 EUR/MWh HTVA.
    assert snap.energy.factor == pytest.approx(1.1192)
    assert snap.energy.base == pytest.approx(0.01510)


def test_pro_card_without_htva_is_refused() -> None:
    from custom_components.be_electricity_prices.providers.bolt import (
        _extract_dynamic_energy,
    )

    text = fixture_text("bolt_pro_variable.pdf", layout=True).replace("HTVA", "TTC")
    with pytest.raises(ExtractorError, match="HTVA"):
        _extract_dynamic_energy(text.replace(" ", "\n"), 0.0, professional=True)


def test_pro_fixed_card_still_parses() -> None:
    snap = parse_snapshot(
        "bolt_pro_fix", fixture_text("bolt_pro_fix.pdf", layout=True), "flanders"
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.taxes.vat_rate == pytest.approx(0.21)


def test_pro_flanders_bills_the_non_residential_energy_fund() -> None:
    """A business connection pays the 'non-résidentiel' row (10,07 EUR/month
    on this card), not the 'résidentiel' one that is '-' for a domiciled
    household. Reading the residential row dropped 120,84 EUR/yr."""
    snap = parse_snapshot(
        "bolt_pro_fix", fixture_text("bolt_pro_fix.pdf", layout=True), "flanders"
    )
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(10.07)


def test_residential_flanders_energy_fund_stays_zero() -> None:
    """The residential row is '-' for a domiciled household, and the card's
    non-residential row must not leak into a residential contract."""
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders"
    )
    assert snap.taxes.energy_fund_eur_per_month == 0.0


def test_pro_energy_fund_is_flanders_only() -> None:
    """The Flemish fund is not levied in Wallonia or Brussels."""
    text = fixture_text("bolt_pro_fix.pdf", layout=True)
    for region in ("wallonia", "brussels"):
        snap = parse_snapshot("bolt_pro_fix", text, region)
        assert snap.taxes.energy_fund_eur_per_month == 0.0


def test_pre_redesign_archive_card_still_parses() -> None:
    """Bolt redesigned its cards between March and April 2026, and the earlier
    archive PDFs are still served.

    They carry no `Prix mensuel` row: the rates sit under `Coût de l'énergie`
    with one labelled line per meter type, and the three regional tax columns
    are inline on the label line rather than below it. parse_snapshot raised,
    fetch_for_month swallowed it, and every Q1 month of a year-to-date walk
    silently billed at the CURRENT card's rate instead -- 16,71 c€/kWh where
    January was 13,27.
    """
    snap = parse_snapshot(
        "bolt_fix",
        fixture_text("bolt_fix_jan_legacy.pdf", layout=True),
        "flanders",
        "test://bolt-202601",
    )
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.1327)
    assert snap.energy.yearly_fixed_fee == pytest.approx(131.88)
    # The federal levies of that month, read off the inline three-column row.
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)


def test_pre_redesign_archive_card_carries_its_overlays_too() -> None:
    """Making the old layout's ENERGY block parse left its overlays behind.

    The card prints all three, just differently, and each miss was silent:
    ``SIBELGA`` in caps (the DSO regex only matched ``Sibelga``, so the dso
    map came back EMPTY and a Brussels year-to-date skipped every archived
    month outright -- static_breakdown raises KeyError on a missing DSO and
    the walk reads that as "no rate to apply", billing Q1 at zero); the
    connection fee behind parenthesised footnote markers ``(*)(***)`` rather
    than bare digits, so Wallonia billed it at zero; and the feed-in
    indicative as a three-column FL/WAL/BX row instead of ``Prix mensuel``,
    so the credit vanished for those months.
    """
    text = fixture_text("bolt_fix_jan_legacy.pdf", layout=True)

    # "Injection (c€/kWh) 5,87 6,69 3,78" under "Tarif d'injection (HTVA)".
    # Those columns are METER REGISTERS -- their header is "(*) TVA non
    # applicable. Simple Jour Nuit" and the Belpex row above shares them --
    # not regions. The VL/WAL/BX headers on that page govern the tax rows.
    # Reading them as regions credited Wallonia the Jour rate (6,69) and
    # Brussels the Nuit one (3,78); every region bills the Simple column,
    # the same way the current card's "Prix mensuel" branch does.
    for region in ("flanders", "wallonia", "brussels"):
        snap = parse_snapshot("bolt_fix", text, region, "test://bolt-202601")
        assert snap.injection is not None, region
        assert snap.injection.current == pytest.approx(0.0587), region
        assert snap.injection.peak is None, region
        assert snap.injection.offpeak is None, region

    # "Redevance de raccordement (c€/kWh) (*)(***) - 0,075 -": Wallonia only.
    assert parse_snapshot(
        "bolt_fix", text, "wallonia", "test://"
    ).taxes.region_connection_fee == pytest.approx(0.00075)
    assert parse_snapshot(
        "bolt_fix", text, "flanders", "test://"
    ).taxes.region_connection_fee == pytest.approx(0.0)

    # "Cogénération (c€/kWh)* 0,39 -" is the archive card's name for the row
    # the current one labels "WKK": French throughout, and Flanders-only. The
    # certificats row still parsed, so missing it just dropped 0,39 c€/kWh.
    flanders = parse_snapshot("bolt_fix", text, "flanders", "test://")
    assert flanders.taxes.flanders_renewables == pytest.approx(0.0156)

    # "Cotisation Fond énergie (€/mois) (*)" then a separate
    # "Non-résidentiel 10,07 - -" row, where the current card puts both on one
    # labelled line. The professional editions walk this archive too.
    assert bolt_mod._extract_energy_fund(text, professional=True) == pytest.approx(
        10.07
    )
    # The residential row on the same card is "-", so residential stays 0.
    assert bolt_mod._extract_energy_fund(text) == pytest.approx(0.0)

    # "SIBELGA 9,96 9,96 7,53 7,53 2,27 14,73 -": the month must be priceable.
    brussels = parse_snapshot("bolt_fix", text, "brussels", "test://")
    assert "sibelga" in brussels.dsos
    assert (
        compute_breakdown(
            brussels,
            "sibelga",
            "brussels",
            datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        )
        is not None
    )


def test_a_tax_row_is_bounded_and_may_hold_whole_numbers() -> None:
    """Telling a value from a footnote marker by requiring a decimal was the
    wrong discriminator twice over.

    A levy printed as a whole number was rejected outright, and _extract_taxes
    raises on a miss, so EVERY Bolt contract in all three regions would stop
    refreshing -- Belgium zeroed the federal levy in August 2026, so that is
    not hypothetical. And the skip was unbounded, so a row whose own values
    were missing from the render captured the NEXT row's silently: the excise
    came out as the energy contribution, 0,2042 instead of 5,0329 c€/kWh.
    """
    text = fixture_text("bolt_fix.pdf", layout=True)

    # A whole-number levy parses instead of taking the snapshot down.
    i = text.find("Contribution sur l'énergie")
    assert i > 0
    zeroed = text[:i] + text[i : i + 90].replace("0,20417", "0") + text[i + 90 :]
    assert bolt_mod.parse_snapshot(
        "bolt_fix", zeroed, "wallonia"
    ).taxes.energy_contribution == pytest.approx(0.0)

    # A row that really is missing its values raises rather than borrowing the
    # next row's, so the coordinator falls back to its cached snapshot.
    dropped = re.sub(
        r"(Droit d’accise spécial[^\n]*)\n\s*5,0329\n\s*5,0329\n\s*5,0329",
        r"\1",
        text,
        count=1,
    )
    assert dropped != text
    with pytest.raises(ExtractorError, match="accise"):
        bolt_mod.parse_snapshot("bolt_fix", dropped, "flanders")


def test_footnote_marker_is_not_read_as_the_flanders_tax_value() -> None:
    """The current layout prints a bare footnote digit between the label and
    the values (`... (c€/kWh) 5` then 5,0329 on the next lines). Matching the
    first number after the label captured that 5 and billed the excise at
    5 c€/kWh; only a decimal separator distinguishes a value from a marker."""
    snap = parse_snapshot(
        "bolt_fix", fixture_text("bolt_fix.pdf", layout=True), "flanders", "test://"
    )
    assert snap.taxes.federal_excise == pytest.approx(0.050329)


def _textless_pdf() -> bytes:
    """A one-page PDF with no text layer, the Ecofix-rasterized shape."""
    import io

    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=595, height=842)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def test_a_textless_card_is_not_treated_as_an_unpublished_one() -> None:
    """Bolt's fix-family fallback exists for a card that is not published
    yet on the 1st of the month. A card that downloaded fine and carries no
    text layer is a different thing and must NOT take that path.

    Letting it through was a real regression: CardNotReadableError subclasses
    ExtractorError, so the `except ExtractorError` here swallowed it and
    served last month's card with no Repairs card and no staleness signal
    (the successful fetch resets the snapshot age, and live-check only
    asserts a non-empty publication label). The loud failure it replaced was
    strictly better.
    """
    from unittest.mock import patch

    from custom_components.be_electricity_prices.providers import _pdf

    textless = _textless_pdf()
    served: list[str] = []

    async def _serve(session: object, url: str, *, timeout: int = 30) -> bytes:
        served.append(url)
        return textless

    with patch.object(_pdf, "_fetch_validated_pdf_bytes", _serve):
        with pytest.raises(CardNotReadableError):
            await bolt_mod._fetch_pdf_text(
                None,  # type: ignore[arg-type]
                bolt_mod._CONTRACTS_BY_ID["bolt_fix"],
            )
    # One fetch only: it must not even try the previous month.
    assert len(served) == 1


async def test_an_unpublished_card_still_falls_back_to_last_month() -> None:
    """The other direction: the fallback the fix above narrows must keep
    working for the case it was written for, a 404 on the 1st."""
    from unittest.mock import patch

    from custom_components.be_electricity_prices.providers import _pdf

    real = (FIXTURES / "bolt_fix.pdf").read_bytes()
    served: list[str] = []

    async def _serve(session: object, url: str, *, timeout: int = 30) -> bytes:
        served.append(url)
        if len(served) == 1:
            raise ExtractorError(f"HTTP 404 fetching {url}")
        return real

    with patch.object(_pdf, "_fetch_validated_pdf_bytes", _serve):
        url, text = await bolt_mod._fetch_pdf_text(
            None,  # type: ignore[arg-type]
            bolt_mod._CONTRACTS_BY_ID["bolt_fix"],
        )
    assert len(text) > 1000
    assert url == served[-1]
    assert len(served) == 2


@pytest.mark.parametrize(
    "message",
    [
        "network error fetching https://x/fix_res_el_fr_202608.pdf: TimeoutError",
        "HTTP 500 fetching https://x/fix_res_el_fr_202608.pdf",
        "HTTP 403 fetching https://x/fix_res_el_fr_202608.pdf",
    ],
)
async def test_a_transient_failure_is_not_treated_as_an_unpublished_card(
    message: str,
) -> None:
    """Same rule as the textless card, for the other thing that is not an
    unpublished card: a fetch that failed transiently.

    This month's card is probably fine and simply did not arrive, so falling
    back would serve last month's prices with no Repairs card and no
    staleness signal - the successful fallback fetch resets the snapshot age.
    Failing instead lets the coordinator keep the snapshot it already has,
    which IS this month's.

    Measured on live-check run 32223861276: a runner-wide network slowdown
    timed out three fixed contracts, each quietly fell back a month, and the
    card-period gate then reported nine stale-card failures against a
    supplier that was publishing normally.
    """
    from unittest.mock import patch

    from custom_components.be_electricity_prices.providers import _pdf

    served: list[str] = []

    async def _serve(session: object, url: str, *, timeout: int = 30) -> bytes:
        served.append(url)
        raise ExtractorError(message)

    with patch.object(_pdf, "_fetch_validated_pdf_bytes", _serve):
        with pytest.raises(ExtractorError):
            await bolt_mod._fetch_pdf_text(
                None,  # type: ignore[arg-type]
                bolt_mod._CONTRACTS_BY_ID["bolt_fix"],
            )
    # One fetch only: it must not even try the previous month.
    assert len(served) == 1


def test_discover_still_sees_a_reshaped_or_uppercased_card_url() -> None:
    # _CARD_URL_RE is shared with the version resolver. Pinning its version
    # group to \d+ for the resolver's benefit silently narrowed discovery,
    # and a slug discover() cannot see is a new product the catalog diff
    # reports as silence rather than as a finding.
    listing = (
        '<a href="https://files.boltenergie.be/pricelists/var/bolt_res_el_fr_13.pdf">a</a>'
        '<a href="https://files.boltenergie.be/pricelists/var/newprod_res_el_fr_11b.pdf">b</a>'
        '<a href="https://files.boltenergie.be/pricelists/fix/fix_res_el_fr_202608.PDF">c</a>'
    )

    async def _run() -> None:
        with patch.object(bolt_mod, "fetch_text", new=AsyncMock(return_value=listing)):
            found = await bolt_mod.discover(None)  # type: ignore[arg-type]
        assert found == {"var/bolt", "var/newprod", "fix/fix"}

    asyncio.run(_run())


def test_a_non_numeric_version_does_not_break_the_resolver() -> None:
    # The resolver needs a number for the URL and for max(key=int), so it
    # filters rather than crashing on the wider pattern discover() needs.
    listing = '<a href="/pricelists/var/bolt_res_el_fr_11b.pdf">reshaped</a>'

    async def _run() -> None:
        with patch.object(bolt_mod, "fetch_text", new=AsyncMock(return_value=listing)):
            resolved = await bolt_mod._resolve_variable_suffix(
                None,  # type: ignore[arg-type]
                _var("bolt_variable"),
            )
        assert resolved == bolt_mod._VARIABLE_SUFFIX_FALLBACK

    asyncio.run(_run())
