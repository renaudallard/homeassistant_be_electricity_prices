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
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from tests import fixture_text
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
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
    assert contract_ids == {"cociter_variable", "cociter_dynamic"}


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
    """The Dec-2025 fixture parses cleanly and the listing URL with
    matching YYMM is what fetch_for_month must surface."""
    text = fixture_text("cociter_var_2512.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.cociter.fetch_pdf_text",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_LISTING_HTML),  # type: ignore[arg-type]
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
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
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
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
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
