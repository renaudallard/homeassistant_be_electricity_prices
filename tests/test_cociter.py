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

"""Cociter PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.pricing import (
    dso_impact_band,
    energy_eur_per_kwh,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS
from tests import make_text_session, fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    ImpactRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.cociter import (
    fetch_for_month,
    parse_snapshot,
)


def test_cociter_is_registered() -> None:
    assert "cociter" in EXTRACTORS
    assert EXTRACTORS["cociter"].label == "Cociter"
    contract_ids = {c.id for c in EXTRACTORS["cociter"].contracts}
    assert contract_ids == {
        "cociter_variable",
        "cociter_variable_impact",
        "cociter_dynamic",
    }


def test_variable_extracts_indicative_rates() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert isinstance(snap.energy, VariableRates)
    # Indicative rates printed in the PDF (TVAC).
    assert snap.energy.current == pytest.approx(0.126625)
    assert snap.energy.peak == pytest.approx(0.136442)
    assert snap.energy.offpeak == pytest.approx(0.116808)
    assert snap.energy.exclusive_night == pytest.approx(0.116808)
    assert snap.energy.yearly_fixed_fee == pytest.approx(53.0)
    assert snap.energy.formula is not None and "BELIX" in snap.energy.formula
    # Numeric BELIX coefficients for signing-cohort re-pricing, converted to the
    # EUR/kWh basis applied against the monthly mean: (0,075 x BELIX + 5) c€/kWh
    # + 6% VAT -> factor 0.075 * 1.06 * 10, base 5 * 1.06 / 100.
    assert snap.energy.formula_factor == pytest.approx(0.795)
    assert snap.energy.formula_base == pytest.approx(0.053)


def test_variable_extracts_dso_overlay() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274252)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    # Variable PDF prints the compensation-regime prosumer tariff per DSO.
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_variable_extracts_supplier_prosumer_forfait() -> None:
    # The variable card also bills a supplier-side compensation-regime PV
    # forfait (37,10 EUR/kVA/an TVAC) on top of the DSO prosumer tariff.
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert snap.supplier_prosumer_eur_per_kva_year == pytest.approx(37.10)


def test_trihoraire_extracts_the_three_impact_bands() -> None:
    """The September 2026 trihoraire card prints one BELIX formula per CWaPE
    band, so the energy leg is ImpactRates and not a variable rate with an
    Impact overlay. Rates are the card's own indicatives (TVAC)."""
    snap = parse_snapshot(
        fixture_text("cociter_vai_2609.pdf"),
        "cociter_variable_impact",
        "test://vai",
        "2026-09",
    )
    assert isinstance(snap.energy, ImpactRates)
    assert snap.energy.pic == pytest.approx(0.190079)
    assert snap.energy.medium == pytest.approx(0.162663)
    assert snap.energy.eco == pytest.approx(0.135248)
    assert snap.energy.yearly_fixed_fee == pytest.approx(53.0)
    # (0,1 x BELIX + 5) c€/kWh + 6% VAT -> factor 0.1 * 1.06 * 10, base
    # 5 * 1.06 / 100. Same conversion as the variable card's mono row.
    assert snap.energy.pic_factor == pytest.approx(1.06)
    assert snap.energy.medium_factor == pytest.approx(0.848)
    assert snap.energy.eco_factor == pytest.approx(0.636)
    for base in (
        snap.energy.pic_base,
        snap.energy.medium_base,
        snap.energy.eco_base,
    ):
        assert base == pytest.approx(0.053)
    # The coefficients resolve to the printed indicative at the BELIX the card
    # quotes (129,32 EUR/MWh), which is what catches a x10 or a VAT slip.
    pic_factor, pic_base = snap.energy.pic_factor, snap.energy.pic_base
    assert pic_factor is not None and pic_base is not None
    assert pic_factor * 0.12932 + pic_base == pytest.approx(snap.energy.pic, abs=1e-6)


def test_trihoraire_band_miss_is_fatal() -> None:
    """Every band is mandatory: defaulting one to another's rate would bill a
    third of the day at the wrong price, silently."""
    raw = fixture_text("cociter_vai_2609.pdf")
    with pytest.raises(ExtractorError):
        parse_snapshot(
            raw.replace("Heures MEDIUM", "Heures MIDDLE"),
            "cociter_variable_impact",
            "test://vai",
            "2026-09",
        )


def test_trihoraire_dso_row_is_five_columns() -> None:
    """The trihoraire card drops the mono/bi columns outright: its row is
    terme fixe | PIC | MEDIUM | ECO | exclusif nuit, and there is no prosumer
    column beside them."""
    snap = parse_snapshot(
        fixture_text("cociter_vai_2609.pdf"),
        "cociter_variable_impact",
        "test://vai",
        "2026-09",
    )
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.distribution_pic == pytest.approx(0.1508)
    assert aieg.distribution_medium == pytest.approx(0.0982)
    assert aieg.distribution_eco == pytest.approx(0.0456)
    assert aieg.distribution_exclusive_night == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274252)
    assert aieg.prosumer_eur_per_kva_year is None
    # No mono column exists on this card; the field is filled with the PIC
    # rate, which nothing reads because the contract always bills on the
    # Impact bands.
    assert aieg.distribution_single == pytest.approx(0.1508)
    # The supplier-side forfait IS printed, unlike on the dynamic card.
    assert snap.supplier_prosumer_eur_per_kva_year == pytest.approx(37.10)


def test_trihoraire_layout_is_chosen_by_the_card_title() -> None:
    """The five-number row is not distinguishable by counting: the six-number
    pattern matches it by running past the end of the line, since the last row
    is followed by the "3." of the taxes heading, and it lands PIC on the mono
    rate. Take the title away and that is exactly what happens - which is why
    the layout is chosen by the card's own "prix variable trihoraire" title
    before anything counts columns.
    """
    from custom_components.be_electricity_prices.providers.cociter import (
        _extract_dsos,
    )

    raw = fixture_text("cociter_vai_2609.pdf")
    proper = _extract_dsos(raw)
    assert proper["aieg"].distribution_pic == pytest.approx(0.1508)

    untitled = _extract_dsos(raw.replace("prix variable trihoraire", "prix variable"))
    assert set(untitled) == {"rew"}
    assert untitled["rew"].distribution_pic is None
    assert untitled["rew"].distribution_single == pytest.approx(0.1711)


def test_trihoraire_injection_is_the_variable_card_formula() -> None:
    """Same injection block as the variable card: a BELPEX formula with no
    printed indicative, so the credit needs a spot the three-band energy leg
    never fetches, and the contract carries spot_indexed_injection."""
    snap = parse_snapshot(
        fixture_text("cociter_vai_2609.pdf"),
        "cociter_variable_impact",
        "test://vai",
        "2026-09",
    )
    assert snap.injection is not None
    assert snap.injection.current is None
    assert snap.injection.factor == pytest.approx(0.97)
    assert snap.injection.base == pytest.approx(-0.021)
    contracts = {c.id: c for c in EXTRACTORS["cociter"].contracts}
    assert contracts["cociter_variable_impact"].spot_indexed_injection is True
    assert contracts["cociter_variable_impact"].kind == "tou_impact"
    assert contracts["cociter_variable_impact"].regions == frozenset({"wallonia"})


def test_dynamic_has_no_prosumer_rate() -> None:
    # Dynamic SMR3 contract has no compensation regime - the row swaps the
    # prosumer column for three Tarif Impact columns.
    snap = parse_snapshot(
        fixture_text("cociter_dyn_2604.pdf"),
        "cociter_dynamic",
        "test://dyn",
        "2026-04",
    )
    assert snap.dsos["aieg"].prosumer_eur_per_kva_year is None
    # Dynamic dispenses with the compensation regime -> no supplier forfait.
    assert snap.supplier_prosumer_eur_per_kva_year is None


def test_dso_extraction_keys_off_header_not_column_count() -> None:
    # A future card layout could grow extra columns, but we discriminate
    # by the literal "Tarif prosumer" header text rather than column
    # count. Strip the header out of the variable card and the parser
    # must report no prosumer rate even though column 6 still has a
    # number that looks like one.
    raw = fixture_text("cociter_var_2604.pdf")
    without_header = raw.replace("Tarif prosumer", "Tarif Impact")
    from custom_components.be_electricity_prices.providers.cociter import (
        _extract_dsos,
    )

    overlay = _extract_dsos(without_header)["aieg"]
    assert overlay.prosumer_eur_per_kva_year is None
    # Distribution rates still parse - they don't depend on the header.
    assert overlay.distribution_single == pytest.approx(0.1087)


def test_missing_transport_or_abonnement_is_fatal() -> None:
    # The ELIA transport row and the abonnement are mandatory on every
    # card; a regex miss must raise rather than silently zero them.
    raw = fixture_text("cociter_var_2604.pdf")
    with pytest.raises(ExtractorError, match="transport"):
        parse_snapshot(
            raw.replace("Tarifs de transport", "XXX"),
            "cociter_variable",
            "test://var",
            "2026-04",
        )
    with pytest.raises(ExtractorError, match="abonnement"):
        parse_snapshot(
            raw.replace("€/an", "XXX"),
            "cociter_variable",
            "test://var",
            "2026-04",
        )


def test_variable_extracts_taxes() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.00204167)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    # Cociter only operates in Wallonia.
    assert snap.taxes.wallonia_renewables == pytest.approx(0.02968)
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_dynamic_extracts_factor_and_base() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_dyn_2604.pdf"),
        "cociter_dynamic",
        "test://dyn",
        "2026-04",
    )
    assert isinstance(snap.energy, DynamicRates)
    # Cociter Dynamique bills on the quarter-hourly BELPEX spot.
    assert snap.energy.quarter_hourly is True
    # PDF: (0.103 x QUARTER_HOURLY_BELPEX_eur_per_mwh + 3) x 1.06 c€/kWh
    # Literal pinning so a unit-conversion swap can't cancel the test.
    assert snap.energy.factor == pytest.approx(1.0918, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.0318, rel=1e-4)
    # At spot = 100 EUR/MWh = 0.10 EUR/kWh, all-in energy is ~0.14098 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.14098)


def test_variable_extracts_injection_formula() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    inj = snap.injection
    assert inj is not None
    # PDF: "(0,097 x BELPEX – 2,1)" -> factor 0.97, base -0.021 (VAT-exempt).
    assert inj.factor == pytest.approx(0.97)
    assert inj.base == pytest.approx(-0.021)
    # No "maandprijs" printed for hourly-injection - current stays None.
    assert inj.current is None


def test_dynamic_extracts_injection_formula() -> None:
    snap = parse_snapshot(
        fixture_text("cociter_dyn_2604.pdf"),
        "cociter_dynamic",
        "test://dyn",
        "2026-04",
    )
    inj = snap.injection
    assert inj is not None
    # SMR3 quarter-hourly formula: same coefficients as variable.
    assert inj.factor == pytest.approx(0.97)
    assert inj.base == pytest.approx(-0.021)


def test_injection_missing_formula_raises() -> None:
    """Both Cociter products always publish an injection formula, so a
    parse miss must fail loud (keeping last-good data) rather than
    silently zeroing the solar credit."""
    from custom_components.be_electricity_prices.providers.cociter import (
        _extract_injection,
    )

    with pytest.raises(ExtractorError, match="injection"):
        _extract_injection("Tarief zonder injectieformule\n")


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Cociter contract"):
            await EXTRACTORS["cociter"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


# ---- fetch_for_month -----------------------------------------------------------


_LISTING_HTML = """
<a href="https://www.cociter.be/wp-content/uploads/RCVar_YMR_Coop-2511-fr.pdf">November 2025</a>
<a href="https://www.cociter.be/wp-content/uploads/RCVar_YMR_Coop-2512-fr.pdf">December 2025</a>
<a href="https://www.cociter.be/wp-content/uploads/RCVar_YMR_Coop-2601-fr.pdf">January 2026</a>
"""


def test_fetch_for_month_returns_snapshot_when_listing_has_url() -> None:
    """The Dec-2025 fixture parses cleanly and the listing URL with
    matching YYMM is what fetch_for_month must surface."""
    text = fixture_text("cociter_var_2512.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.cociter.fetch_pdf_text",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
                "cociter_variable",
                "wallonia",
                date(2025, 12, 1),
            )
        )
    assert snap is not None
    assert snap.publication_label == "2025-12"
    assert isinstance(snap.energy, VariableRates)


def test_fetch_for_month_returns_none_when_listing_has_no_match() -> None:
    """If Cociter never published (or has dropped) the requested month
    from its listing, fetch_for_month must return None so the
    coordinator falls back to the proxy."""
    snap = asyncio.run(
        fetch_for_month(
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
            "cociter_variable",
            "wallonia",
            date(2024, 6, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    """A contract id without a registered pattern must return None."""
    snap = asyncio.run(
        fetch_for_month(
            make_text_session(_LISTING_HTML),  # type: ignore[arg-type]
            "unknown_family",
            "wallonia",
            date(2025, 12, 1),
        )
    )
    assert snap is None


def test_injection_formula_survives_a_meter_label_rewording() -> None:
    """Cociter rewords the meter-type label in front of the injection
    formula: "Tout compteur", "Compteur SMR3", and from the August 2026
    card "Compteur pouvant effectuer des mesures par quart d'heure". The
    label is prose, so match any of them rather than enumerating - the
    August rewording took the whole contract offline."""
    from custom_components.be_electricity_prices.providers.cociter import (
        _extract_injection,
    )

    august = (
        "Injection (10)\n"
        "Le prix de l'injection varie chaque quart d'heure. Il est calculé en "
        "fonction de l'indice QUARTER HOURL Y BELPEX *** avec la formule "
        "suivante :\n"
        "Type de compteur FORMULE DE PRIX\n"
        "Compteur pouvant effectuer des mesures par quart d'heure "
        "(0,097 x QUARTER HOURL Y BELPEX – 2,1)\n"
    )
    inj = _extract_injection(august)
    assert inj is not None
    assert inj.factor == pytest.approx(0.97)
    assert inj.base == pytest.approx(-0.021)

    # The older wordings must keep working.
    older = august.replace(
        "Compteur pouvant effectuer des mesures par quart d'heure", "Compteur SMR3"
    )
    assert _extract_injection(older).factor == pytest.approx(0.97)


def test_wordpress_dedup_suffix_does_not_hide_a_month() -> None:
    """Cociter's site is WordPress, which appends "-1", "-2", ... when a file
    is re-uploaded under an existing name. July 2026's dynamic card is
    published as ``RCDyn_SM3_Coop-2607-fr-1.pdf``; requiring ``-fr.pdf`` to
    follow the month immediately dropped that month from the archive, and the
    year-to-date walk billed July at the current card instead."""
    from custom_components.be_electricity_prices.providers.cociter import (
        _DYN_RE,
        _VAR_RE,
    )

    html = (
        '<a href="https://x/RCDyn_SM3_Coop-2606-fr.pdf">jun</a>'
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr-1.pdf">jul</a>'
        '<a href="https://x/RCVar_YMR_Coop-2607-fr-2.pdf">jul var</a>'
    )
    assert sorted(m[1] for m in _DYN_RE.findall(html)) == ["2606", "2607"]
    assert sorted(m[1] for m in _VAR_RE.findall(html)) == ["2607"]


async def test_reupload_wins_over_the_superseded_card() -> None:
    """When both the original and a re-upload are listed, the suffixed URL is
    the newer file and must be the one fetched."""
    from datetime import date
    from unittest.mock import AsyncMock, patch

    from custom_components.be_electricity_prices.providers import cociter as cociter_mod

    html = (
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr.pdf">old</a>'
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr-1.pdf">new</a>'
    )
    seen: list[str] = []

    async def _capture(_session: object, url: str, **_kw: object) -> str:
        seen.append(url)
        raise ExtractorError("short-circuit before parse")

    with (
        patch.object(cociter_mod, "fetch_text", new=AsyncMock(return_value=html)),
        patch.object(cociter_mod, "fetch_pdf_text", new=_capture),
    ):
        await cociter_mod.fetch_for_month(
            None,  # type: ignore[arg-type]
            "cociter_dynamic",
            "wallonia",
            date(2026, 7, 1),
        )
    # The re-upload is tried FIRST. It is not necessarily the one used: the
    # stub raises, and an edition that does not parse now falls through to the
    # one it displaced (see the test below), so assert the order, not the last.
    assert seen and seen[0].endswith("-fr-1.pdf")


async def test_an_unparseable_re_upload_falls_back_to_the_edition_it_displaced() -> (
    None
):
    """A re-upload is not always an improvement.

    July 2026's dynamic card was republished with the index renamed from
    "QUARTER HOURLY BELPEX" to "15 MIN BELPEX", the meter labels dropped and
    the injection prose moved below its own formula. Taking only the newest
    edition lost that month from the archive walk, and the walk billed July at
    the CURRENT card's overlays. The original still parses and is still served
    -- Cociter just stopped linking it -- so it is derived from the same "-N"
    convention and tried once every LISTED edition has failed.
    """
    from unittest.mock import AsyncMock, patch

    from custom_components.be_electricity_prices.providers import cociter as cociter_mod

    html = '<a href="https://x/RCDyn_SM3_Coop-2607-fr-1.pdf">jul</a>'
    fetched: list[str] = []

    async def _fetch(_session: object, url: str, **_kw: object) -> str:
        fetched.append(url)
        return "text-of-" + url.rsplit("/", 1)[1]

    def _parse(text: str, *_a: object, **_kw: object) -> Any:
        # Only the displaced original parses.
        if text.endswith("-fr-1.pdf"):
            raise ExtractorError("could not parse Cociter dynamic formula")
        return SimpleNamespace(supplier="cociter", contract="cociter_dynamic")

    with (
        patch.object(cociter_mod, "fetch_text", new=AsyncMock(return_value=html)),
        patch.object(cociter_mod, "fetch_pdf_text", new=_fetch),
        patch.object(cociter_mod, "parse_snapshot", new=_parse),
        patch.object(cociter_mod, "archive_validity_check", new=lambda s, *a, **k: s),
    ):
        snap = await cociter_mod.fetch_for_month(
            None,  # type: ignore[arg-type]
            "cociter_dynamic",
            "wallonia",
            date(2026, 7, 1),
        )
    assert snap is not None, "the month must not be lost"
    assert [u.rsplit("/", 1)[1] for u in fetched] == [
        "RCDyn_SM3_Coop-2607-fr-1.pdf",
        "RCDyn_SM3_Coop-2607-fr.pdf",
    ]

    # A healthy month costs exactly one fetch: there is no suffix to strip, so
    # nothing extra is derived.
    fetched.clear()
    html = '<a href="https://x/RCDyn_SM3_Coop-2606-fr.pdf">jun</a>'
    with (
        patch.object(cociter_mod, "fetch_text", new=AsyncMock(return_value=html)),
        patch.object(cociter_mod, "fetch_pdf_text", new=_fetch),
        patch.object(cociter_mod, "parse_snapshot", new=_parse),
        patch.object(cociter_mod, "archive_validity_check", new=lambda s, *a, **k: s),
    ):
        await cociter_mod.fetch_for_month(
            None,  # type: ignore[arg-type]
            "cociter_dynamic",
            "wallonia",
            date(2026, 6, 1),
        )
    assert len(fetched) == 1


async def test_the_live_card_and_the_archived_one_resolve_the_same_file() -> None:
    """`_find_latest` (the live fetch and the probe) must rank re-uploads too.

    It sorted on the month alone, so a month carrying both the original and a
    correction fell back to listing order, while `fetch_for_month` ranked on
    the re-upload counter. The two disagreed whenever the index listed the
    newest first -- the live price came off the superseded card, and the probe
    pinned the cache to it.
    """
    from unittest.mock import AsyncMock, patch

    from custom_components.be_electricity_prices.providers import cociter as cociter_mod

    for html in (
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr.pdf">old</a>'
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr-1.pdf">new</a>',
        # Newest first, the ordering that used to pick the superseded file.
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr-1.pdf">new</a>'
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr.pdf">old</a>',
    ):
        with patch.object(cociter_mod, "fetch_text", new=AsyncMock(return_value=html)):
            url, label = await cociter_mod._find_latest(
                None,  # type: ignore[arg-type]
                cociter_mod._DYN_RE,
            )
        assert url.endswith("-fr-1.pdf"), html
        assert label == "2026-07"

    # A newer month still wins over a heavily re-uploaded older one.
    html = (
        '<a href="https://x/RCDyn_SM3_Coop-2607-fr-9.pdf">jul</a>'
        '<a href="https://x/RCDyn_SM3_Coop-2608-fr.pdf">aug</a>'
    )
    with patch.object(cociter_mod, "fetch_text", new=AsyncMock(return_value=html)):
        url, label = await cociter_mod._find_latest(
            None,  # type: ignore[arg-type]
            cociter_mod._DYN_RE,
        )
    assert url.endswith("2608-fr.pdf")
    assert label == "2026-08"


def test_variable_bills_the_delivery_month_not_the_printed_indicative() -> None:
    """The printed rate is computed from the PREVIOUS month's BELIX and the
    card says so: "le prix indique est calcule avec l'indice BELIX du mois
    precedent ... renseigne a titre indicatif". Note (7) indexes the contract
    on "la moyenne arithmetique des cotations journalieres Day Ahead EPEX SPOT
    Belgium durant le mois de fourniture" and settles it retroactively.

    So billing the printed rate bills last month's index. The April 2026 card
    proves it: its printed 12,6625 c/kWh is exactly the formula at MARCH's
    BELIX of 92,61, while April settled at 78,93 and 11,5749."""
    from types import SimpleNamespace
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.cohort import _month_indexed_leg
    from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh

    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True

    entry = SimpleNamespace(data={"contract": "cociter_variable", "api_key": "k"})
    leg = _month_indexed_leg(snap, entry)  # type: ignore[arg-type]
    assert leg is not None
    when = datetime(2026, 4, 15, 12, tzinfo=dt_util.DEFAULT_TIME_ZONE)

    def at(belix_eur_per_kwh: float) -> float:
        return energy_eur_per_kwh(
            leg, when, belix_eur_per_kwh, meter="mono", region="wallonia"
        )

    # The printed indicative IS the formula at March's index, which is what the
    # card says and what made this a defect rather than a rounding difference.
    assert at(0.09261) == pytest.approx(energy.current)
    # April's own index is what April is billed at.
    assert at(0.07893) == pytest.approx(0.115749, abs=1e-6)
    # And May's, printed on the June card, matches too.
    assert at(0.09177) == pytest.approx(0.125957, abs=1e-6)

    # An entry with no ENTSO-E key keeps the printed rate rather than losing
    # its energy leg: the variable kind never prompts for one.
    assert (
        _month_indexed_leg(
            snap,
            SimpleNamespace(data={"contract": "cociter_variable"}),  # type: ignore[arg-type]
        )
        is None
    )


def test_variable_carries_a_formula_per_meter() -> None:
    """The card prints FOUR formulas, one per meter: "Compteur monohoraire
    (0,075 x BELIX + 5) + 6% TVA ... Heures pleines (0,085 x BELIX + 5) ...
    Heures creuses (0,065 x BELIX + 5) ... Compteur exclusif nuit (0,065 x
    BELIX + 5)". Only the mono row was read, so a bi-hourly or night cohort
    was re-priced on the mono pair.

    The night circuit is parsed on its own key even though it coincides with
    off-peak here: they are separate contractual formulas and Cociter can
    move one without the other.
    """
    snap = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"), "cociter_variable", "t://v", "2026-04"
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    # c/kWh per EUR/MWh of index, with the "+ 6% TVA" printed outside the
    # parens landing on both coefficients.
    assert energy.formula_factor == pytest.approx(0.075 * 1.06 * 10)
    assert energy.formula_factor_peak == pytest.approx(0.085 * 1.06 * 10)
    assert energy.formula_factor_offpeak == pytest.approx(0.065 * 1.06 * 10)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.065 * 1.06 * 10)
    for base in (
        energy.formula_base,
        energy.formula_base_peak,
        energy.formula_base_offpeak,
        energy.formula_base_exclusive_night,
    ):
        assert base == pytest.approx(5 * 1.06 / 100)


def test_the_dynamic_card_gains_no_bands() -> None:
    """The dynamic card publishes one SMR3 formula and excludes the very
    meter this is about: "Si le Client a un compteur exclusif nuit,
    l'application du present Contrat a prix dynamique est exclue"."""
    from custom_components.be_electricity_prices.providers.cociter import (
        _belix_band_coefficients,
    )

    assert _belix_band_coefficients(fixture_text("cociter_dyn_2604.pdf")) == {}


def test_variable_card_carries_the_price_ceiling() -> None:
    """Note (8) caps the supply price: "si le Belix venait a augmenter au
    point que l'application du prix variable donne une valeur superieure au
    prix maximum, c'est le prix maximum qui s'applique". The column is
    printed on every meter row and was read on none of them, so a month
    whose BELIX cleared the cap billed the uncapped formula."""
    snap = parse_snapshot(
        fixture_text("cociter_var_2609.pdf"),
        "cociter_variable",
        "test://var",
        "2026-09",
    )
    assert isinstance(snap.energy, VariableRates)
    for ceiling in (
        snap.energy.ceiling_single,
        snap.energy.ceiling_peak,
        snap.energy.ceiling_offpeak,
        snap.energy.ceiling_exclusive_night,
    ):
        assert ceiling == pytest.approx(0.265)
    # The cap is the SECOND c€/kWh figure on the row; reading the first would
    # silently clamp every kWh to the indicative.
    assert snap.energy.current == pytest.approx(0.155809)


def test_trihoraire_card_carries_the_price_ceiling() -> None:
    """Same cap, printed per band on the trihoraire card."""
    snap = parse_snapshot(
        fixture_text("cociter_vai_2609.pdf"),
        "cociter_variable_impact",
        "test://vai",
        "2026-09",
    )
    assert isinstance(snap.energy, ImpactRates)
    assert snap.energy.ceiling_pic == pytest.approx(0.265)
    assert snap.energy.ceiling_medium == pytest.approx(0.265)
    assert snap.energy.ceiling_eco == pytest.approx(0.265)
    assert snap.energy.pic == pytest.approx(0.190079)


def test_a_card_without_the_maximum_column_stays_uncapped() -> None:
    """The column arrived with the September 2026 cards: April prints the row
    ending at the indicative. Such a card has to yield None, not 0.0, or every
    kWh on it would clamp to zero. The dynamic card never carries a cap at
    all, which is the supplier leaving that contract deliberately uncapped."""
    older = parse_snapshot(
        fixture_text("cociter_var_2604.pdf"),
        "cociter_variable",
        "test://var",
        "2026-04",
    )
    assert isinstance(older.energy, VariableRates)
    assert older.energy.ceiling_single is None
    assert older.energy.ceiling_peak is None
    assert older.energy.ceiling_offpeak is None
    assert older.energy.ceiling_exclusive_night is None
    # and the rate it does publish is untouched by the miss
    assert older.energy.current == pytest.approx(0.126625)


def test_impact_band_rate_is_capped_by_its_ceiling() -> None:
    """The engine has to apply the Impact ceiling the way it already applies
    the variable one. Rates above the cap here because the published bands sit
    well under it: the cap only bites above a BELIX of about 200 EUR/MWh."""
    when = datetime(2026, 9, 15, 19, 30, tzinfo=ZoneInfo("Europe/Brussels"))
    assert dso_impact_band(when) == "pic"
    uncapped = ImpactRates(pic=0.30, medium=0.20, eco=0.10)
    assert energy_eur_per_kwh(uncapped, when, None, "dynamic", "wallonia") == (
        pytest.approx(0.30)
    )
    capped = ImpactRates(pic=0.30, medium=0.20, eco=0.10, ceiling_pic=0.265)
    assert energy_eur_per_kwh(capped, when, None, "dynamic", "wallonia") == (
        pytest.approx(0.265)
    )
    # A band under its own cap is untouched, and a band with no cap is too.
    eco_when = datetime(2026, 9, 15, 3, 30, tzinfo=ZoneInfo("Europe/Brussels"))
    assert energy_eur_per_kwh(capped, eco_when, None, "dynamic", "wallonia") == (
        pytest.approx(0.10)
    )


def test_trihoraire_is_billed_on_the_delivery_month() -> None:
    """The trihoraire card carries the variable card's note (7): "indexe
    mensuellement ... moyenne arithmetique des cotations journalieres Day Ahead
    EPEX SPOT Belgium durant le mois de fourniture", and prints its three bands
    at LAST month's BELIX (129,32 for August on the September 2026 card). So the
    printed bands are the fallback and the delivery month is billed on its own
    mean, per band, the way the variable card's mono pair already is."""
    from custom_components.be_electricity_prices.cohort import _month_indexed_leg
    from custom_components.be_electricity_prices.providers.base import (
        SpotMonthlyRates,
    )

    snap = parse_snapshot(
        fixture_text("cociter_vai_2609.pdf"),
        "cociter_variable_impact",
        "test://vai",
        "2026-09",
    )
    energy = snap.energy
    assert isinstance(energy, ImpactRates)
    assert energy.month_indexed is True

    entry = SimpleNamespace(
        data={"contract": "cociter_variable_impact", "api_key": "k"}
    )
    leg = _month_indexed_leg(snap, entry)  # type: ignore[arg-type]
    assert isinstance(leg, SpotMonthlyRates)
    assert leg.factor_pic == pytest.approx(1.06)
    assert leg.factor_medium == pytest.approx(0.848)
    assert leg.factor_eco == pytest.approx(0.636)
    assert leg.ceiling_pic == pytest.approx(0.265)
    assert leg.yearly_fixed_fee == pytest.approx(53.0)

    def at(hour: int, belix_eur_per_kwh: float) -> float:
        when = datetime(2026, 9, 15, hour, 30, tzinfo=ZoneInfo("Europe/Brussels"))
        return energy_eur_per_kwh(
            leg, when, belix_eur_per_kwh, "dynamic", "wallonia", "impact"
        )

    # At August's index every band reproduces the printed indicative, which
    # is what the card says it computed and what made this a lag, not noise.
    assert at(19, 0.12932) == pytest.approx(energy.pic, abs=1e-6)
    assert at(9, 0.12932) == pytest.approx(energy.medium, abs=1e-6)
    assert at(13, 0.12932) == pytest.approx(energy.eco, abs=1e-6)
    # September's own mean is what September is billed at: 136,24 EUR/MWh to
    # the 7th moves PIC from 19,0079 to 19,7414 c/kWh.
    assert at(19, 0.13624) == pytest.approx(0.197414, abs=1e-6)
    assert at(13, 0.13624) == pytest.approx(0.139649, abs=1e-6)
    # The band is the CWaPE one every day of the week: a Saturday evening is
    # still PIC, unlike the bi-hourly or time-of-use rules.
    saturday = datetime(2026, 9, 19, 19, 30, tzinfo=ZoneInfo("Europe/Brussels"))
    assert energy_eur_per_kwh(leg, saturday, 0.13624, "dynamic", "wallonia") == (
        pytest.approx(0.197414, abs=1e-6)
    )
    # And the card's cap still binds on the re-priced leg.
    assert at(19, 0.30) == pytest.approx(0.265)

    # No ENTSO-E key: the printed bands stand, as on the variable card.
    assert (
        _month_indexed_leg(
            snap,
            SimpleNamespace(data={"contract": "cociter_variable_impact"}),  # type: ignore[arg-type]
        )
        is None
    )


def test_trihoraire_with_a_band_formula_unreadable_keeps_its_printed_bands() -> None:
    """A card whose indicative rates parse but one band's formula does not is
    billed on the printed bands for the whole month rather than on two
    formulas and a hole: the flag is only set when all three pairs are read."""
    from custom_components.be_electricity_prices.cohort import _month_indexed_leg

    raw = fixture_text("cociter_vai_2609.pdf").replace(
        "(0,08 x BELIX + 5) + 6% TVA", "(0,08 x BELIX + 5) TVAC"
    )
    snap = parse_snapshot(raw, "cociter_variable_impact", "test://vai", "2026-09")
    energy = snap.energy
    assert isinstance(energy, ImpactRates)
    assert energy.medium == pytest.approx(0.162663)
    assert energy.medium_factor is None
    assert energy.pic_factor == pytest.approx(1.06)
    assert energy.month_indexed is False
    entry = SimpleNamespace(
        data={"contract": "cociter_variable_impact", "api_key": "k"}
    )
    assert _month_indexed_leg(snap, entry) is None  # type: ignore[arg-type]
