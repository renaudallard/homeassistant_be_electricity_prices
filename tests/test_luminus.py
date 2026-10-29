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

"""Luminus PDF extractor tests against April 2026 fixtures."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date
from typing import Any

import pytest

from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    TimeOfUseRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.luminus import parse_snapshot
from tests import fixture_text


def _dynamic_w() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_w.pdf"), "wallonia"
    )


def _dynamic_v() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_v.pdf"), "flanders"
    )


def _comfy_w() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w.pdf"), "wallonia"
    )


def _comfyflex_v() -> SupplierSnapshot:
    return parse_snapshot(
        "luminus_comfyflex", fixture_text("luminus_comfyflex_v.pdf"), "flanders"
    )


def test_luminus_is_registered() -> None:
    assert "luminus" in EXTRACTORS
    assert EXTRACTORS["luminus"].label == "Luminus"
    contract_ids = {c.id for c in EXTRACTORS["luminus"].contracts}
    assert "luminus_comfy" in contract_ids
    assert "luminus_comfyflex" in contract_ids
    assert "luminus_comfyflex_plus" in contract_ids
    assert "luminus_maxxfix" in contract_ids
    assert "luminus_maxxflex" in contract_ids
    assert "luminus_smartflex" in contract_ids
    assert "luminus_dynamic" in contract_ids


def test_dynamic_wallonia_extracts_consumption_formula() -> None:
    snap = _dynamic_w()
    assert isinstance(snap.energy, DynamicRates)
    # PDF prints "hors TVA  0,1019 x Belpex H + 2,4591" at 6% VAT.
    # Literal pinning (vs `0.1019 * 1.06 * 10`) so a unit-conversion
    # swap of 1.06 ⇄ 10 can't cancel out and pass the assertion.
    assert snap.energy.factor == pytest.approx(1.08014)
    assert snap.energy.base == pytest.approx(0.02606646)
    assert snap.energy.yearly_fixed_fee == pytest.approx(75.0)


def test_dynamic_bills_per_clock_hour() -> None:
    """The card indexes on "Belpex H", the hourly quotation, so the contract
    stays on the hourly ENTSO-E product. Unpinned until now."""
    snap = _dynamic_w()
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is False


@pytest.mark.parametrize(
    ("fixture", "eco", "medium", "pic"),
    [
        # Headings ECO | MEDIUM | PIC, the order every card printed until
        # September 2026.
        ("luminus_dynamic_w.pdf", None, None, None),
        # October 2026 turned the headings round to PIC | MEDIUM | ECO;
        # ORES "11,98 13,27 7,39 16,57 10,83 5,09 ...".
        ("luminus_dynamic_w_oct.pdf", 0.0509, 0.1083, 0.1657),
    ],
)
def test_dynamic_wallonia_impact_bands_follow_the_headings(
    fixture: str, eco: float | None, medium: float | None, pic: float | None
) -> None:
    snap = parse_snapshot("luminus_dynamic", fixture_text(fixture), "wallonia")
    ores = snap.dsos["ores"]
    assert ores.distribution_eco is not None
    assert ores.distribution_medium is not None
    assert ores.distribution_pic is not None
    assert ores.distribution_eco < ores.distribution_medium < ores.distribution_pic
    if eco is not None:
        assert ores.distribution_eco == pytest.approx(eco)
        assert ores.distribution_medium == pytest.approx(medium)
        assert ores.distribution_pic == pytest.approx(pic)


def test_dynamic_wallonia_impact_headings_run_together_read_the_same() -> None:
    """pypdf 6.20 runs the headings and their hours into one line with no
    space between them, which a word boundary after "Impact" or a band name
    refused. The fixture is read in whichever form the installed pypdf
    prints, then put in each."""
    split = (
        "Tarif Impact\nHeures PIC\n17h-22h\nHeures MEDIUM\n07h-11h\n22h-01h\n"
        "Heures ECO\n01h-07h\n11h-17h\nExclusif nuit"
    )
    joined = (
        "Tarif ImpactHeures PIC17h-22h Heures MEDIUM07h-11h22h-01h "
        "Heures ECO01h-07h11h-17h Exclusif nuit"
    )
    text = fixture_text("luminus_dynamic_w_oct.pdf").replace(joined, split)
    assert text.count(split) == 1
    want = parse_snapshot("luminus_dynamic", text, "wallonia")
    got = parse_snapshot("luminus_dynamic", text.replace(split, joined), "wallonia")
    assert got.dsos == want.dsos


def test_dynamic_wallonia_without_impact_headings_fails_loud() -> None:
    text = fixture_text("luminus_dynamic_w_oct.pdf").replace("Heures MEDIUM", "")
    with pytest.raises(ExtractorError, match="Impact column headings"):
        parse_snapshot("luminus_dynamic", text, "wallonia")


def test_dynamic_flanders_has_a_different_base() -> None:
    # Luminus's hourly formula has a region-specific base; Flanders is
    # 50 cents below Wallonia. This is the one fact that motivates the
    # whole region-aware fetcher signature - if we ever merged the two
    # regions into one snapshot, one of them would silently get the
    # wrong base.
    w = _dynamic_w()
    v = _dynamic_v()
    assert isinstance(w.energy, DynamicRates)
    assert isinstance(v.energy, DynamicRates)
    assert w.energy.factor == pytest.approx(v.energy.factor)
    assert w.energy.base != v.energy.base
    assert v.energy.base == pytest.approx(0.02076646)


def test_dynamic_extracts_injection_formula_with_negative_base() -> None:
    snap = _dynamic_w()
    inj = snap.injection
    assert inj is not None
    # PDF injection: hors TVA  0,1019 x Belpex H - 1,2737 (VAT-exempt).
    assert inj.factor == pytest.approx(1.019)
    assert inj.base == pytest.approx(-0.012737)


@pytest.mark.parametrize(
    ("fixture", "label", "energy", "feed_in", "expected", "printed"),
    [
        # January 2024 to July 2025: "Jour (compteur de jour) = 0,1054 x
        # Belpex H + 2,0940", then "Jour (compteur de jour et de nuit)" and
        # "et nuit" for the bi-hourly meter, and the feed-in the same way.
        (
            "luminus_dynamic_v_2024-06.pdf",
            "juin 2024",
            (0.1054, 1.1200),
            (0.0825, -0.9550),
            (91.41, 79.77),
            (11.40, 5.63),
        ),
        (
            "luminus_dynamic_v_2025-06.pdf",
            "juin 2025",
            (0.1054, 2.0940),
            (0.0825, -0.7790),
            (90.79, 80.49),
            (12.36, 5.86),
        ),
        # August 2025: "Compteur mono-horaire", "Heures pleines (Compteur
        # bi-horaire)" and "Heures creuses".
        (
            "luminus_dynamic_v_2025-08.pdf",
            "août 2025",
            (0.1018, 2.1316),
            (0.1018, -1.2685),
            (87.43, 78.63),
            (11.69, 6.73),
        ),
    ],
)
def test_dynamic_cards_before_september_2025_read_their_formulas(
    fixture: str,
    label: str,
    energy: tuple[float, float],
    feed_in: tuple[float, float],
    expected: tuple[float, float],
    printed: tuple[float, float],
) -> None:
    """Before September 2025 the dynamic card spelled its formulas one row per
    meter register, where the reader knew only the single "Prelevement
    (...)" row of the later cards. Every card from January 2024 to August
    2025 failed to parse, so a contract signed on one billed each month's own
    card, and a month of that time stood in on the current card. The
    estimates the card prints are its formulas at the Belpex H it expects
    for the year ahead."""
    snap = parse_snapshot("luminus_dynamic", fixture_text(fixture), "flanders")
    assert snap.publication_label == label
    rates, inj = snap.energy, snap.injection
    assert isinstance(rates, DynamicRates) and inj is not None
    assert rates.factor == pytest.approx(energy[0] * 10.0 * 1.06)
    assert rates.base == pytest.approx(energy[1] / 100.0 * 1.06)
    assert inj.factor == pytest.approx(feed_in[0] * 10.0)
    assert inj.base == pytest.approx(feed_in[1] / 100.0)
    assert inj.factor is not None and inj.base is not None
    assert rates.factor * expected[0] / 1000.0 + rates.base == pytest.approx(
        printed[0] / 100.0, abs=1e-4
    )
    assert inj.factor * expected[1] / 1000.0 + inj.base == pytest.approx(
        printed[1] / 100.0, abs=1e-4
    )
    assert "fluvius_antwerpen" in snap.dsos


def test_the_quarter_hour_data_fee_is_billed_while_fluvius_kept_it_apart() -> None:
    """Up to 2024 Fluvius billed the quarter-hourly metering regime, which the
    dynamic contract needs, a data-management fee of its own, and the card's
    "(**) ... quart d'heure" footnote carried it: 15,14 against the table's
    monthly-regime 13,95 in June 2024. From 2025 every regime pays the
    table's fee, and the footnote kept 2025's 18,56 through 2026 where the
    table moved to 18,92."""
    from custom_components.be_electricity_prices.providers._luminus_overlays import (
        _extract_flanders_dsos,
    )

    for fixture, fee in (
        ("luminus_dynamic_v_2024-06.pdf", 15.14),
        ("luminus_dynamic_v_2025-06.pdf", 18.56),
        ("luminus_dynamic_v.pdf", 18.92),
    ):
        snap = parse_snapshot("luminus_dynamic", fixture_text(fixture), "flanders")
        antwerpen = snap.dsos["fluvius_antwerpen"]
        assert antwerpen.data_management_per_year == pytest.approx(fee), fixture
    # A static contract runs on any regime, so it bills the table's.
    june_2024 = fixture_text("luminus_dynamic_v_2024-06.pdf")
    static = _extract_flanders_dsos(june_2024, "fixed", 2024)
    assert static["fluvius_antwerpen"].data_management_per_year == pytest.approx(13.95)


def test_a_dynamic_card_pricing_its_registers_apart_is_refused() -> None:
    """A dynamic leg bills one formula on every meter, and every card on file
    prints the same one on each register. One that did not would bill the
    night register of a bi-hourly meter on the day formula, so it is refused
    rather than read off its first row."""
    text = fixture_text("luminus_dynamic_v_2025-06.pdf")
    apart = text.replace(
        "et nuit =  0,1054  x  Belpex H  +  2,0940",
        "et nuit =  0,0954  x  Belpex H  +  2,0940",
    )
    assert apart != text
    with pytest.raises(ExtractorError, match="meter register"):
        parse_snapshot("luminus_dynamic", apart, "flanders")


async def test_the_archive_serves_a_dynamic_month_before_september_2025(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A contract signed on the June 2025 card is billed off it: the archive
    answered None for every dynamic month before September 2025, its card
    not parsing, and the signing cohort fell back to each month's own card,
    0,57 c/kWh under on energy and 0,72 over on the feed-in in 2026."""
    from custom_components.be_electricity_prices.providers import luminus

    async def _products(session: object, url: str, **kwargs: object) -> str:
        assert "signing=2025-06" in url
        return _archive_products(("Luminus Dynamic Online Electricité", "id-dynamic"))

    async def _card(session: object, url: str, **kwargs: object) -> str:
        assert "productId=id-dynamic&" in url and "date=2025-06" in url
        return fixture_text("luminus_dynamic_v_2025-06.pdf")

    monkeypatch.setattr(luminus, "fetch_text", _products)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _card)
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_dynamic",
        "flanders",
        date(2025, 6, 1),
    )
    assert snap is not None
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(0.1054 * 10.0 * 1.06)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(0.825)


def test_a_2025_card_credits_each_register_on_its_own_formula() -> None:
    """The cards of 2025 call the feed-in row "Compensation pour l'energie
    injectee", which the reader did not know, so no Luminus card of 2025
    parsed and a contract signed on one was billed today's card. Until June
    2025 they price the registers apart: "Jour (compteur de jour) = 0,0644 x
    Belpex - 1,5900; Jour (compteur de jour et de nuit)) = 0,0794 x Belpex -
    1,5900; Nuit = 0,0414 x Belpex - 1,5900", printed at February's 128,75 as
    6,71, 8,64 and 3,75 c/kWh. A card pricing them alike is the one rate."""
    snap = parse_snapshot(
        "luminus_maxxfix", fixture_text("luminus_maxxfix_v_2025-03.pdf"), "flanders"
    )
    assert snap.publication_label == "mars 2025"
    energy = snap.energy
    assert isinstance(energy, FixedRates)
    assert (
        energy.single,
        energy.peak,
        energy.offpeak,
        energy.exclusive_night,
    ) == pytest.approx((0.1781, 0.1937, 0.1596, 0.1596))
    inj = snap.injection
    assert inj is not None and inj.bi_hourly and inj.month_indexed
    assert (inj.current, inj.peak, inj.offpeak) == pytest.approx(
        (0.0671, 0.0864, 0.0375)
    )
    for factor, base, card_factor, printed in (
        (inj.factor, inj.base, 0.0644, inj.current),
        (inj.factor_peak, inj.base_peak, 0.0794, inj.peak),
        (inj.factor_offpeak, inj.base_offpeak, 0.0414, inj.offpeak),
    ):
        assert factor is not None and base is not None
        assert factor == pytest.approx(card_factor * 10.0)
        assert base == pytest.approx(-0.0159)
        assert factor * 0.12875 + base == pytest.approx(printed, abs=1e-4)
    # The Flemish half of a card printed for both regions reads as before.
    assert len(snap.dsos) == 8
    antwerpen = snap.dsos["fluvius_antwerpen"]
    assert antwerpen.data_management_per_year == pytest.approx(18.56)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(53.26)
    assert antwerpen.distribution_single == pytest.approx(0.0599)
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    assert snap.taxes.flanders_renewables == pytest.approx(0.016)
    # Since July 2025 the registers are alike, and the leg is the one rate.
    today = parse_snapshot(
        "luminus_maxxfix", fixture_text("luminus_maxxfix_v_oct.pdf"), "flanders"
    ).injection
    assert today is not None and not today.bi_hourly and today.peak is None
    # Registers priced apart with one of their formulas unread is a drift.
    text = fixture_text("luminus_maxxfix_v_2025-03.pdf")
    unread = text.replace("0,0414 x Belpex", "0,0414 x Endex")
    assert unread != text
    with pytest.raises(ExtractorError, match="register"):
        parse_snapshot("luminus_maxxfix", unread, "flanders")


def test_a_2025_variable_card_reads_its_formulas_by_meter() -> None:
    """Until July 2025 the energy rows are named after the meter, "Jour
    (compteur de jour) = 0,1086 x Belpex + 3,6500", "Jour (compteur de jour et
    de nuit)", "et nuit" and "Exclusif nuit", where the later cards print
    "Compteur mono-horaire" and "Heures pleines". Unread, a MaxxFlex contract
    signed then would keep no formula and be billed each month's card."""
    snap = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_v_2025-03.pdf"), "flanders"
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed and not energy.quarter_indexed
    assert not energy.rlp_indexed
    printed = (energy.current, energy.peak, energy.offpeak, energy.exclusive_night)
    assert printed == pytest.approx((0.1869, 0.2116, 0.1625, 0.1625))
    for factor, base, card, rate in zip(
        (
            energy.formula_factor,
            energy.formula_factor_peak,
            energy.formula_factor_offpeak,
            energy.formula_factor_exclusive_night,
        ),
        (
            energy.formula_base,
            energy.formula_base_peak,
            energy.formula_base_offpeak,
            energy.formula_base_exclusive_night,
        ),
        ((0.1086, 3.65), (0.1274, 3.557), (0.0942, 3.209), (0.0942, 3.209)),
        printed,
        strict=True,
    ):
        assert factor is not None and base is not None
        assert factor == pytest.approx(card[0] * 10.0 * 1.06)
        assert base == pytest.approx(card[1] / 100.0 * 1.06)
        # The printed rate is the formula at February's 128,75.
        assert factor * 0.12875 + base == pytest.approx(rate, abs=1e-4)
    inj = snap.injection
    assert inj is not None and inj.bi_hourly and inj.month_indexed
    # The energy block stops at the feed-in block, headed "pour la
    # compensation de" on these cards, so no feed-in row is read as energy.
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _ENERGY_FORMULA_BLOCK_RE,
    )

    block = _ENERGY_FORMULA_BLOCK_RE.search(
        fixture_text("luminus_maxxflex_v_2025-03.pdf")
    )
    assert block is not None and "0,0644" not in block.group(0)


def _serve_the_archive(
    monkeypatch: pytest.MonkeyPatch, product: str, month: str, fixture: str
) -> None:
    """Luminus's archive listing ``product`` and serving ``fixture`` as its
    card of ``month``, and no card for another month."""
    from custom_components.be_electricity_prices.providers import luminus

    async def _products(session: object, url: str, **kwargs: object) -> str:
        return _archive_products((product, "id-product"))

    async def _card(session: object, url: str, **kwargs: object) -> str:
        if f"date={month}&" not in url:
            raise ExtractorError(f"Luminus archive: no card for {url}")
        return fixture_text(fixture)

    monkeypatch.setattr(luminus, "fetch_text", _products)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _card)


async def _signed_legs(
    hass: Any, contract: str, region: str, dso: str, start: str, today: Any
) -> Any:
    """The cohort legs of an entry on ``contract`` that started on ``start``."""
    from unittest.mock import MagicMock

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.be_electricity_prices.cohort import _cohort_legs
    from custom_components.be_electricity_prices.const import DOMAIN

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "luminus",
            "contract": contract,
            "region": region,
            "dso": dso,
            "meter": "mono",
            "contract_start_date": start,
        },
    )
    return await _cohort_legs(
        hass, MagicMock(), EXTRACTORS["luminus"], contract, region, entry, today
    )


async def test_a_contract_signed_in_2025_is_billed_its_own_card(
    hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MaxxFix runs two years on the card it was signed on. With no card of
    2025 read, the signing cohort found none and billed today's: 22,12 c/kWh
    for a contract signed in March 2025 at 17,81, 150,85 EUR a year more at
    3.500 kWh, on the live price, the rolling year and the projections."""
    freezer.move_to("2026-10-28 12:00:00+01:00")
    _serve_the_archive(
        monkeypatch,
        "Luminus MaxxFix Electricité",
        "2025-03",
        "luminus_maxxfix_v_2025-03.pdf",
    )
    today = parse_snapshot(
        "luminus_maxxfix", fixture_text("luminus_maxxfix_v_oct.pdf"), "flanders"
    )
    legs = await _signed_legs(
        hass, "luminus_maxxfix", "flanders", "fluvius_antwerpen", "2025-03-15", today
    )
    assert legs.card == "mars 2025"
    billed = legs.splice(today).energy
    assert isinstance(billed, FixedRates) and isinstance(today.energy, FixedRates)
    assert billed.single == pytest.approx(0.1781)
    assert today.energy.single == pytest.approx(0.2212)


def test_a_card_printed_for_both_regions_reads_on_the_walloon_side() -> None:
    """Until December 2025 one card served both regions. Its renewables row
    prints the Walloon figure after the Flemish pair ("FL 1,18 0,42 WAL 3,03
    -"), which the reader could not reach, so no Walloon card of 2025 read.
    Its tax block prints the Flemish column then the Walloon one, and the
    Walloon connection fee is the Walloon column's 0,0750, not the Flemish
    column's "-" in fifth place."""
    text = fixture_text("luminus_maxxfix_v_2025-03.pdf")
    wal = parse_snapshot("luminus_maxxfix", text, "wallonia")
    fl = parse_snapshot("luminus_maxxfix", text, "flanders")
    assert wal.energy == fl.energy and wal.injection == fl.injection
    assert wal.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert wal.taxes.flanders_renewables == 0.0
    assert wal.taxes.region_connection_fee == pytest.approx(0.00075)
    assert fl.taxes.region_connection_fee == 0.0
    for taxes in (wal.taxes, fl.taxes):
        assert taxes.federal_excise == pytest.approx(0.050329)
        assert taxes.energy_contribution == pytest.approx(0.002042)
        assert taxes.energy_fund_eur_per_month == 0.0
    assert set(wal.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = wal.dsos["aieg"]
    # "AIEG 9,46 9,56 7,65 7,00 2,98 19,08 78,99"
    assert (
        aieg.distribution_single,
        aieg.distribution_peak,
        aieg.distribution_offpeak,
        aieg.distribution_exclusive_night,
        aieg.transport,
    ) == pytest.approx((0.0946, 0.0956, 0.0765, 0.07, 0.0298))
    assert aieg.data_management_per_year == pytest.approx(19.08)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(78.99)
    # "TECTEO RESA 10,62 ..." and "WAVRE 11,48 ...", as the other suppliers'
    # cards of 2025 print them.
    assert wal.dsos["resa"].distribution_single == pytest.approx(0.1062)
    assert wal.dsos["rew"].distribution_single == pytest.approx(0.1148)


def test_the_dynamic_card_for_both_regions_reads_without_its_walloon_network() -> None:
    """Luminus Dynamic was sold in Flanders alone until April 2025, and from
    May its card served both regions, its Walloon renewables and taxes
    printed as on the other cards. Its Walloon network table prints the
    distribution alone, four figures a row, with no transport and no fixed
    term, so the card reads no row of it on its own: the rows come from a
    sibling card of the month (below). A card of the Flemish months stays
    unread as a Walloon one."""
    snap = parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_v_2025-06.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(0.1054 * 10.0 * 1.06)
    assert snap.energy.base == pytest.approx(2.094 / 100.0 * 1.06)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(0.825)
    assert snap.injection.base == pytest.approx(-0.00779)
    assert snap.taxes.wallonia_renewables == pytest.approx(0.0302)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.dsos == {}
    with pytest.raises(ExtractorError, match="Wallonia renewables"):
        parse_snapshot(
            "luminus_dynamic", fixture_text("luminus_dynamic_v_2024-06.pdf"), "wallonia"
        )


def _serve_june_2025(monkeypatch: pytest.MonkeyPatch, sibling: str) -> dict[str, str]:
    """Luminus's archive serving June 2025's Dynamic card and ``sibling``
    as its ComfyFlex card, both on the Wallonia tab, and no card of another
    month. The cards it serves, by product id, for a test to change."""
    from custom_components.be_electricity_prices.providers import luminus

    cards = {
        "id-dynamic": fixture_text("luminus_dynamic_v_2025-06.pdf"),
        "id-comfyflex": sibling,
    }

    async def _products(session: object, url: str, **kwargs: object) -> str:
        return _archive_products(
            ("Luminus Dynamic Online Electricité", "id-dynamic"),
            ("Luminus ComfyFlex Electricité", "id-comfyflex"),
        )

    async def _card(session: object, url: str, **kwargs: object) -> str:
        if "date=2025-06&" not in url:
            raise ExtractorError(f"Luminus archive: no card for {url}")
        assert "region=Wallonia" in url
        return next(card for pid, card in cards.items() if f"={pid}&" in url)

    monkeypatch.setattr(luminus, "fetch_text", _products)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _card)
    return cards


async def test_the_dynamic_card_takes_its_walloon_network_from_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Dynamic card printed for both regions gives each Walloon row its
    distribution alone, "ORES (Brabant Wallon) 10,79 11,46 6,63 5,41",
    where ComfyFlex's card of the month prints the whole row, "... 2,98
    13,84 86,96". The rows are the DSO's, so they are taken from that card,
    and only while it prints the same distribution: a sibling printing other
    figures lends nothing. Without them a Walloon Dynamic month of 2025 had
    no network to bill and was dropped."""
    from custom_components.be_electricity_prices.providers import luminus

    sibling = fixture_text("luminus_comfyflex_w_2025-06.pdf")
    cards = _serve_june_2025(monkeypatch, sibling)
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_dynamic",
        "wallonia",
        date(2025, 6, 1),
    )
    assert snap is not None
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    ores = snap.dsos["ores"]
    assert (
        ores.distribution_single,
        ores.distribution_peak,
        ores.distribution_offpeak,
        ores.distribution_exclusive_night,
        ores.transport,
    ) == pytest.approx((0.1079, 0.1146, 0.0663, 0.0541, 0.0298))
    assert ores.data_management_per_year == pytest.approx(13.84)
    # No Dynamic card names the prosumer tariff.
    assert ores.prosumer_eur_per_kva_year is None

    cards["id-comfyflex"] = sibling.replace(
        "ORES (Brabant Wallon) 10,79", "ORES (Brabant Wallon) 10,80"
    )
    assert cards["id-comfyflex"] != sibling
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_dynamic",
        "wallonia",
        date(2025, 6, 1),
    )
    assert snap is not None and snap.dsos == {}


async def test_a_walloon_dynamic_month_of_2025_is_backfilled_on_its_own_card(
    hass: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    """June 2025 of a Walloon Dynamic entry is billed on its own card, the
    network borrowed, by the backfill and the year to date alike, where
    both left it out for want of a network row."""
    import logging
    from datetime import UTC, datetime, timedelta

    from tests import make_entry
    from tests.test_backfill import _live_and_backfill

    _serve_june_2025(monkeypatch, fixture_text("luminus_comfyflex_w_2025-06.pdf"))
    entry = make_entry(
        supplier="luminus",
        contract="luminus_dynamic",
        meter="dynamic",
        solar_regime="none",
        api_key="k",
        consumption_kwh="sensor.cons",
    )
    start = datetime(2024, 12, 31, 23, tzinfo=UTC)
    with caplog.at_level(logging.WARNING):
        back, live = await _live_and_backfill(
            hass,
            entry,
            parse_snapshot(
                "luminus_dynamic", fixture_text("luminus_dynamic_w_oct.pdf"), "wallonia"
            ),
            date(2025, 7, 31),
            lambda entity_id, _hour: 0.5 if entity_id == "sensor.cons" else None,
            spots={start + timedelta(hours=i): 0.08 for i in range(24 * 213)},
            extractor=EXTRACTORS["luminus"],
        )
    assert back == pytest.approx(live, abs=0.01)
    left_out = [
        r.getMessage()
        for r in caplog.records
        if "unpriced" in r.getMessage() or "dropped" in r.getMessage()
    ]
    assert not left_out


async def test_a_walloon_contract_signed_in_2025_is_billed_its_own_card(
    hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Walloon MaxxFix signed in March 2025 and a Walloon Dynamic signed in
    June 2025 are billed off their own cards, which no Walloon card of 2025
    allowed: the fixed one at 17,81 c/kWh where today's card says 22,65, the
    dynamic one on 0,1054 x Belpex H + 2,0940 where today's says 0,1019 x
    Belpex H + 2,4591."""
    freezer.move_to("2026-10-28 12:00:00+01:00")
    _serve_the_archive(
        monkeypatch,
        "Luminus MaxxFix Electricité",
        "2025-03",
        "luminus_maxxfix_v_2025-03.pdf",
    )
    today = parse_snapshot(
        "luminus_maxxfix", fixture_text("luminus_maxxfix_w_oct.pdf"), "wallonia"
    )
    legs = await _signed_legs(
        hass, "luminus_maxxfix", "wallonia", "ores", "2025-03-15", today
    )
    assert legs.card == "mars 2025"
    billed = legs.splice(today).energy
    assert isinstance(billed, FixedRates) and isinstance(today.energy, FixedRates)
    assert billed.single == pytest.approx(0.1781)
    assert today.energy.single == pytest.approx(0.2265)

    _serve_the_archive(
        monkeypatch,
        "Luminus Dynamic Online Electricité",
        "2025-06",
        "luminus_dynamic_v_2025-06.pdf",
    )
    today = parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_w_oct.pdf"), "wallonia"
    )
    legs = await _signed_legs(
        hass, "luminus_dynamic", "wallonia", "ores", "2025-06-15", today
    )
    assert legs.card == "juin 2025"
    assert isinstance(legs.energy, DynamicRates)
    assert legs.energy.factor == pytest.approx(0.1054 * 10.0 * 1.06)
    assert legs.energy.base == pytest.approx(2.094 / 100.0 * 1.06)


async def test_a_bi_hourly_meter_signed_in_2025_is_credited_each_register(
    hass: Any, freezer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A card of January to June 2025 prints a feed-in formula per register,
    and a contract signed on one keeps them: 0,0794 x Belpex - 1,5900 for the
    day register and 0,0414 x Belpex - 1,5900 for the night one. The cohort
    froze the single register's 0,0644 x Belpex - 1,5900 alone and credited a
    bi-hourly meter that on both. The live price, the year to date and the
    backfill now credit each register on its own formula, and alike."""
    from datetime import UTC, datetime, timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.day_night import is_offpeak
    from custom_components.be_electricity_prices.injection import (
        _bake_monthly_injection,
        _compute_injection_price,
    )
    from tests import make_entry
    from tests.test_backfill import _live_and_backfill

    _serve_the_archive(
        monkeypatch,
        "Luminus MaxxFix Electricité",
        "2025-03",
        "luminus_maxxfix_v_2025-03.pdf",
    )
    today = parse_snapshot(
        "luminus_maxxfix", fixture_text("luminus_maxxfix_v_oct.pdf"), "flanders"
    )
    mean = 0.10
    day, night = 0.794 * mean - 0.0159, 0.414 * mean - 0.0159

    def entry(title: str) -> Any:
        return make_entry(
            supplier="luminus",
            contract="luminus_maxxfix",
            region="flanders",
            dso="fluvius_antwerpen",
            meter="bi",
            title=title,
            solar_regime="injection",
            api_key="k",
            contract_start_date="2025-03-15",
            consumption_kwh="sensor.cons",
            injection_kwh="sensor.inj",
        )

    # The live price, on the formula of the register counting now: a Wednesday
    # and a Saturday afternoon of January 2026.
    for when, rate in (
        ("2026-01-14 13:00:00+01:00", day),
        ("2026-01-17 13:00:00+01:00", night),
    ):
        freezer.move_to(when)
        legs = await _signed_legs(
            hass,
            "luminus_maxxfix",
            "flanders",
            "fluvius_antwerpen",
            "2025-03-15",
            today,
        )
        baked = _bake_monthly_injection(legs.splice(today), mean)
        assert _compute_injection_price(baked, entry("live"), {}) == pytest.approx(rate)

    # The year to date and the backfill over January 2026, 0,5 kWh fed in
    # each hour from 10:00 to 15:00, against the same days with none.
    freezer.move_to("2026-01-31 23:59:00+01:00")
    start = datetime(2025, 12, 31, 23, tzinfo=UTC)
    spots = {start + timedelta(hours=i): mean for i in range(24 * 31)}
    middays = [h for h in spots if 10 <= dt_util.as_local(h).hour <= 15]

    def fed_in(entity_id: str, hour: datetime) -> float | None:
        if entity_id == "sensor.inj":
            return 0.5 if hour in middays else 0.0
        return 0.0 if entity_id == "sensor.cons" else None

    def nothing(entity_id: str, hour: datetime) -> float | None:
        return 0.0 if entity_id in ("sensor.cons", "sensor.inj") else None

    walks = {}
    for name, per_hour in (("fed in", fed_in), ("nothing", nothing)):
        walks[name] = await _live_and_backfill(
            hass,
            entry(name),
            today,
            date(2026, 1, 31),
            per_hour,
            spots=spots,
            extractor=EXTRACTORS["luminus"],
        )
    for back, live in walks.values():
        assert back == pytest.approx(live, abs=0.01)
    credited = walks["nothing"][1] - walks["fed in"][1]
    per_register = sum(
        0.5 * (night if is_offpeak(dt_util.as_local(h), "flanders") else day)
        for h in middays
    )
    assert credited == pytest.approx(per_register, abs=0.01)


def test_missing_injection_row_fails_loud() -> None:
    # Every Luminus card publishes an injection rate; a non-dynamic card
    # whose injection row went missing is a layout drift, not a fee-free
    # contract, so it must raise rather than silently credit nothing.
    text = fixture_text("luminus_comfy_w.pdf").replace("injectée", "verwijderd")
    with pytest.raises(ExtractorError, match="injection"):
        parse_snapshot("luminus_comfy", text, "wallonia")


def test_comfy_wallonia_fixed_rates_and_dso() -> None:
    snap = _comfy_w()
    assert isinstance(snap.energy, FixedRates)
    # PDF:  20,38   23,74   17,71   17,71  (mono / pleines / creuses / excl_nuit).
    assert snap.energy.single == pytest.approx(0.2038)
    assert snap.energy.peak == pytest.approx(0.2374)
    assert snap.energy.offpeak == pytest.approx(0.1771)
    assert snap.energy.exclusive_night == pytest.approx(0.1771)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # The Redevance fixe row prints "65,00 65,00 -": the exclusive-night
    # circuit carries no separate abonnement, so its yearly fee is 0, not the
    # standard 65 (billed once on the main connection).
    from custom_components.be_electricity_prices.pricing import (
        yearly_fixed_fee_for_meter,
    )

    assert snap.energy.yearly_fixed_fee_exclusive_night == pytest.approx(0.0)
    assert yearly_fixed_fee_for_meter(snap.energy, "exclusive_night") == pytest.approx(
        0.0
    )
    # All five Wallonia DSOs and a sanity-check on the AIEG row.
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


def test_comfyflex_flanders_uses_current_monthly_not_annual_estimate() -> None:
    # ComfyFlex prints two energy rows: 'Énergie fournie' (current month)
    # and 'Estimation annuelle de l'énergie fournie'. Take the first or
    # we'd over- / under-bill users by ~5% in a moving market.
    snap = _comfyflex_v()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1558)
    assert snap.energy.peak == pytest.approx(0.1684)
    assert snap.energy.offpeak == pytest.approx(0.1366)


def test_flanders_dynamic_dso_table_is_smaller_than_static() -> None:
    # Dynamic (SMR3) cards print 4 numbers per Fluvius row (digital
    # meter only). Static cards add 4 more (analog + prosumer). The
    # parser handles both.
    dyn = _dynamic_v()
    flex = _comfyflex_v()
    antwerpen_dyn = dyn.dsos["fluvius_antwerpen"]
    antwerpen_static = flex.dsos["fluvius_antwerpen"]
    # Distribution + capacity should agree to within 2 decimals
    # (dynamic is rounded, static prints 4-decimal).
    assert antwerpen_dyn.distribution_single == pytest.approx(0.0535)
    assert antwerpen_static.distribution_single == pytest.approx(0.0535)
    # Only the static card carries a prosumer rate.
    assert antwerpen_dyn.prosumer_eur_per_kva_year is None
    assert antwerpen_static.prosumer_eur_per_kva_year == pytest.approx(54.63)
    # Both bill the table's data-management fee, which Fluvius charges every
    # low-voltage metering regime in 2026, though the dynamic card's "(**)
    # ... quart d'heure" footnote still prints 2025's 18,56.
    assert antwerpen_dyn.data_management_per_year == pytest.approx(18.92)
    assert antwerpen_static.data_management_per_year == pytest.approx(18.92)


def test_taxes_split_correctly_per_region() -> None:
    w = _dynamic_w()
    v = _dynamic_v()
    # Federal excise is uniform across regions.
    assert w.taxes.federal_excise == pytest.approx(0.050329)
    assert v.taxes.federal_excise == pytest.approx(0.050329)
    # Energy contribution is uniform too.
    assert w.taxes.energy_contribution == pytest.approx(0.002042)
    assert v.taxes.energy_contribution == pytest.approx(0.002042)
    # Wallonia: green energy 3,03 c€/kWh, no Flanders renewables.
    assert w.taxes.wallonia_renewables == pytest.approx(0.0303)
    assert w.taxes.flanders_renewables == 0.0
    # Wallonia: connection fee 0,075 c€/kWh.
    assert w.taxes.region_connection_fee == pytest.approx(0.00075)
    # Flanders: green 1,17 + cogen 0,39 = 1,56 c€/kWh, no connection fee.
    assert v.taxes.flanders_renewables == pytest.approx(0.0156)
    assert v.taxes.wallonia_renewables == 0.0
    assert v.taxes.region_connection_fee == 0.0
    # Energy fund is BTR (résidentiel) which is '-' for residential users
    # in both regions today.
    assert w.taxes.energy_fund_eur_per_month == 0.0
    assert v.taxes.energy_fund_eur_per_month == 0.0


def test_comfyflex_plus_parses_as_variable() -> None:
    snap = parse_snapshot(
        "luminus_comfyflex_plus",
        fixture_text("luminus_comfyflex_plus_w.pdf"),
        "wallonia",
    )
    assert isinstance(snap.energy, VariableRates)
    # Drop-in addition: same parser path as the existing ComfyFlex.
    # The energy row must produce four populated rates.
    assert snap.energy.current is not None
    assert snap.energy.peak is not None
    assert snap.energy.offpeak is not None
    assert snap.energy.exclusive_night is not None
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


def test_maxxflex_parses_as_variable() -> None:
    snap = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current is not None
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


def test_smartflex_parses_as_time_of_use() -> None:
    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    # SmartFlex's only sensible energy schema is TOU. The PDF prints
    #   "Énergie fournie  (c€/kWh) 15,54 13,29 6,72"
    # mapped to peak / transition / offpeak.
    assert isinstance(snap.energy, TimeOfUseRates)
    assert snap.energy.peak == pytest.approx(0.1554)
    assert snap.energy.transition == pytest.approx(0.1329)
    assert snap.energy.offpeak == pytest.approx(0.0672)
    assert snap.energy.yearly_fixed_fee == pytest.approx(65.0)
    # SmartFlex bills seasonal windows, not the generic CWaPE schedule.
    assert snap.energy.weekend_rule == "smartflex_seasonal"
    # peak > transition > offpeak: correct slot ordering in EUR/kWh.
    assert snap.energy.peak > snap.energy.transition > snap.energy.offpeak
    # All five Wallonia DSOs present (no schema change for the network side).
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}


@pytest.mark.parametrize(
    ("fixture", "region", "peak", "transition", "offpeak"),
    [
        # "Énergie fournie (c€/kWh) 20,95 17,85 8,67 0,00": the October 2026
        # row gained a Happy Sunday column, and the three-figure row of the
        # same label further down is the transitional MaxxFlex price
        # "18,87 21,96 16,42", which must never be read as the TOU bands.
        ("luminus_smartflex_w_oct.pdf", "wallonia", 0.2095, 0.1785, 0.0867),
        ("luminus_smartflex_v_oct.pdf", "flanders", 0.2042, 0.1732, 0.0814),
    ],
)
def test_smartflex_reads_its_own_row_beside_happy_sunday(
    fixture: str, region: str, peak: float, transition: float, offpeak: float
) -> None:
    snap = parse_snapshot("luminus_smartflex", fixture_text(fixture), region)
    assert isinstance(snap.energy, TimeOfUseRates)
    assert snap.energy.peak == pytest.approx(peak)
    assert snap.energy.transition == pytest.approx(transition)
    assert snap.energy.offpeak == pytest.approx(offpeak)


def test_maxxflex_energy_carries_the_monthly_formula() -> None:
    """MaxxFlex indexes the COMMODITY on the delivery month too: "Le parametre
    d'indexation est base sur la moyenne arithmetique des cotations
    journalieres Day Ahead Belpex Baseload ... pendant le mois de livraison."
    The printed 14,41 is that formula at March's index on an April card.
    """
    from custom_components.be_electricity_prices.providers._rates import VariableRates

    snap = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True
    assert energy.current == pytest.approx(0.1441)
    # c/kWh HTVA per EUR/MWh of index, printed TVAC, so x10 and x1,06 on both.
    assert energy.formula_factor == pytest.approx(0.1096 * 10.0 * 1.06)
    assert energy.formula_base == pytest.approx(3.4516 / 100.0 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(0.1290 * 10.0 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0940 * 10.0 * 1.06)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.0940 * 10.0 * 1.06)
    # Round-trips to the card's own printed rate at its own index.
    assert energy.formula_factor is not None and energy.formula_base is not None
    assert energy.formula_factor * 0.09261 + energy.formula_base == pytest.approx(
        0.1441, abs=1e-4
    )


def test_quarterly_and_tou_cards_get_no_monthly_energy_formula() -> None:
    """Three traps on one supplier, all resolved by gating on the
    arithmetic-mean sentence rather than the formula.

    ComfyFlex prints a TWO-term formula, "x Belpex + 0,0000 x Endex 1-0-3 +
    4,2102", against a QUARTERLY index; without the tail guard _NUM
    backtracks and binds "0,000" as the base. SmartFlex carries a
    MaxxFlex-identical block for a non-SMR3 meter but not the sentence. A
    fixed card has no energy formula at all, and searching the whole document
    rather than the energy block would hand it the INJECTION one.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _monthly_energy_coefficients,
        _quarterly_energy_coefficients,
    )

    for fixture in ("luminus_comfyflex_v.pdf", "luminus_comfyflex_plus_w.pdf"):
        assert _monthly_energy_coefficients(fixture_text(fixture)) == ({}, False)
    for fixture in ("luminus_smartflex_w.pdf", "luminus_comfy_w.pdf"):
        assert _monthly_energy_coefficients(fixture_text(fixture)) == ({}, False)
        assert _quarterly_energy_coefficients(fixture_text(fixture)) == {}
    # MaxxFlex is indexed on the month, and so not on the quarter.
    assert _quarterly_energy_coefficients(fixture_text("luminus_maxxflex_w.pdf")) == {}


def test_comfyflex_energy_is_indexed_on_the_delivery_quarter() -> None:
    """ "Votre tarif sera indexe tous les trimestres ... Les prix affiches sont
    calcules sur la base de la derniere valeur Belpex connue (trimestre
    precedent) ... Sur le decompte, Luminus appliquera les valeurs moyennes
    trimestrielles." Read as resolved rates, every quarter was billed on the
    one before: ComfyFlex in Flanders 4,33 c/kWh under in the third quarter
    of 2026.

    April's card wraps "Compteur mono-\\nhoraire" after the hyphen, which a
    label without the wrap read as no mono row and so no formula at all.
    """
    for cid, fixture, region, base in (
        ("luminus_comfyflex", "luminus_comfyflex_v.pdf", "flanders", 4.2102),
        ("luminus_comfyflex_plus", "luminus_comfyflex_plus_w.pdf", "wallonia", 6.3302),
    ):
        energy = parse_snapshot(cid, fixture_text(fixture), region).energy
        assert isinstance(energy, VariableRates), cid
        # On the quarter and not on the month, as Bolt's variable cards are.
        assert energy.quarter_indexed is True, cid
        assert energy.month_indexed is False, cid
        assert energy.rlp_indexed is False, cid
        # c/kWh HTVA per EUR/MWh of index, printed TVAC: x10 and x1,06.
        assert energy.formula_factor == pytest.approx(0.1096 * 10.0 * 1.06), cid
        assert energy.formula_base == pytest.approx(base / 100.0 * 1.06), cid
        # Each printed rate is its own pair at "Belpex = 95,75 EUR/MWh (valeur
        # de l'indice du 1re trimestre 2026)", to the printed digit.
        for printed, factor, b in (
            (energy.current, energy.formula_factor, energy.formula_base),
            (energy.peak, energy.formula_factor_peak, energy.formula_base_peak),
            (
                energy.offpeak,
                energy.formula_factor_offpeak,
                energy.formula_base_offpeak,
            ),
            (
                energy.exclusive_night,
                energy.formula_factor_exclusive_night,
                energy.formula_base_exclusive_night,
            ),
        ):
            assert factor is not None and b is not None, cid
            assert factor * 0.09575 + b == pytest.approx(printed, abs=1e-4), cid
    for c in EXTRACTORS["luminus"].contracts:
        if c.id in ("luminus_comfyflex", "luminus_comfyflex_plus"):
            assert c.month_indexed_energy and c.spot_indexed_injection, c.id


def test_a_weighted_endex_term_is_not_read_as_a_formula() -> None:
    """Every row carries a second term on Endex 1-0-3, at 0,0000 on every
    card since January 2026. A card giving it weight prices on a forward
    index nothing here resolves, so it keeps its printed rates."""
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _quarterly_energy_coefficients,
    )

    text = fixture_text("luminus_comfyflex_v.pdf")
    assert _quarterly_energy_coefficients(text)
    weighted = text.replace("+   0,0000  x  Endex", "+   0,0150  x  Endex", 1)
    assert weighted != text
    assert _quarterly_energy_coefficients(weighted) == {}


def test_basicflex_energy_is_indexed_on_the_delivery_months_rlp_belpex() -> None:
    """BasicFlex prints "Compteur mono-horaire = 0,1043 x Belpex RLP M +
    1,4519" and indexes it "pendant le mois de livraison avec une ponderation
    RLP"; the rate it prints is that formula at the previous month's value.
    Read as a resolved rate, every month was billed on the index before it,
    3,26 c/kWh under in September 2026, and no key could correct it."""
    from custom_components.be_electricity_prices.providers._rates import VariableRates

    snap = parse_snapshot(
        "luminus_basicflex", fixture_text("luminus_basicflex_w_oct.pdf"), "wallonia"
    )
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed is True
    assert energy.rlp_indexed is True
    # "la moyenne arithmetique des profils de consommation RLP des differents
    # gestionnaires": every DSO column alike.
    assert energy.rlp_blend == "columns"
    assert energy.formula_factor == pytest.approx(0.1043 * 10.0 * 1.06)
    assert energy.formula_base == pytest.approx(1.4519 / 100.0 * 1.06)
    assert energy.formula_factor_peak == pytest.approx(0.1263 * 10.0 * 1.06)
    assert energy.formula_base_peak == pytest.approx(1.6003 / 100.0 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0913 * 10.0 * 1.06)
    assert energy.formula_factor_exclusive_night == pytest.approx(0.0913 * 10.0 * 1.06)
    # Each printed rate is its formula at "Belpex RLP M = 164,42 (valeur de
    # l'indice de septembre 2026)".
    for printed, factor, base in (
        (energy.current, energy.formula_factor, energy.formula_base),
        (energy.peak, energy.formula_factor_peak, energy.formula_base_peak),
        (energy.offpeak, energy.formula_factor_offpeak, energy.formula_base_offpeak),
    ):
        assert factor is not None and base is not None
        assert factor * 0.16442 + base == pytest.approx(printed, abs=1e-4)
    assert any(
        c.id == "luminus_basicflex" and c.month_indexed_energy
        for c in EXTRACTORS["luminus"].contracts
    )
    # MaxxFlex's bare Belpex is still the plain mean.
    maxx = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w_oct.pdf"), "wallonia"
    ).energy
    assert isinstance(maxx, VariableRates)
    assert maxx.month_indexed is True and maxx.rlp_indexed is False


def test_monthly_cards_carry_the_injection_formula() -> None:
    """The card says the printed figure is not the rate: "Votre tarif sera
    indexe tous les mois. La valeur Belpex du mois en cours n'est connue qu'a
    la fin du mois. Les prix affiches sont calcules sur la base de la derniere
    valeur Belpex connue (mois precedent)."
    """
    for cid, fixture, region in (
        ("luminus_comfy", "luminus_comfy_w.pdf", "wallonia"),
        ("luminus_maxxflex", "luminus_maxxflex_w.pdf", "wallonia"),
        ("luminus_smartflex", "luminus_smartflex_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture), region)
        inj = snap.injection
        assert inj is not None, cid
        assert inj.current == pytest.approx(0.0381), cid
        # "0,0481 x Belpex - 0,6392", c/kWh per EUR/MWh, and the block is
        # HTVA with the card noting "La TVA s'eleve a 0%".
        assert inj.factor == pytest.approx(0.481), cid
        assert inj.base == pytest.approx(-0.006392), cid
        assert inj.month_indexed is True, cid
        assert inj.factor is not None and inj.base is not None
        # Round-trips to the card's own printed figure at its own index.
        assert inj.factor * 0.09261 + inj.base == pytest.approx(0.0381, abs=1e-4), cid


def test_quarterly_cards_carry_the_injection_formula() -> None:
    """ComfyFlex and ComfyFlex+ print the IDENTICAL formula and index it
    QUARTERLY: "Votre tarif sera indexe tous les trimestres", against "la
    valeur de l'indice du 1re trimestre 2026". Read as a printed figure alone,
    every quarter was credited on the one before, for good: 3,89 c/kWh in the
    third quarter of 2026 where its own index gives 5,68.

    The credit is month indexed, the one period flag a feed-in leg carries:
    its months settle on the quarter's index once the next quarter's card
    names it, and the printed figure stays for an entry with no day-ahead
    prices until then. A formula with no cadence beside it still reads as
    none, since its coefficients would be taken for a per-hour formula.
    """
    for cid, fixture, region in (
        ("luminus_comfyflex", "luminus_comfyflex_v.pdf", "flanders"),
        ("luminus_comfyflex_plus", "luminus_comfyflex_plus_w.pdf", "wallonia"),
    ):
        snap = parse_snapshot(cid, fixture_text(fixture), region)
        inj = snap.injection
        assert inj is not None, cid
        assert inj.current == pytest.approx(0.0396), cid
        assert inj.factor == pytest.approx(0.481), cid
        assert inj.base == pytest.approx(-0.006392), cid
        assert inj.month_indexed is True, cid
        assert inj.factor is not None and inj.base is not None
        assert inj.factor * 0.09575 + inj.base == pytest.approx(0.0396, abs=1e-4)

    text = fixture_text("luminus_comfyflex_v.pdf")
    uncadenced = text.replace("Votre tarif sera indexé tous les trimestres", "")
    assert uncadenced != text
    inj = parse_snapshot("luminus_comfyflex", uncadenced, "flanders").injection
    assert inj is not None
    assert inj.factor is None and inj.month_indexed is False


def test_injection_uses_applicable_rate_not_annual_estimate() -> None:
    # Cards print two injection rows: the applicable "Tarif de l'énergie
    # injectée" (3,81 / 3,96 c/kWh) and an "Estimation annuelle du tarif
    # de l'énergie injectée" 12-month forecast (4,38 / 4,44). Credit the
    # applicable rate, not the forecast.
    comfy = _comfy_w()
    assert comfy.injection is not None
    assert comfy.injection.current == pytest.approx(0.0381)
    comfyflex = _comfyflex_v()
    assert comfyflex.injection is not None
    assert comfyflex.injection.current == pytest.approx(0.0396)
    # May card: the annual estimate (3,68) is *below* the applicable rate
    # (3,81), so the old behaviour under-credited; confirm we still pick
    # the applicable row regardless of which is larger.
    may = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w_may.pdf"), "wallonia"
    )
    assert may.injection is not None
    assert may.injection.current == pytest.approx(0.0381)


def test_brussels_is_unsupported() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="not available in region"):
            await EXTRACTORS["luminus"].fetch(None, "luminus_comfy", "brussels")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_publication_label_tolerates_padded_parens() -> None:
    """The April-2026 cards print "(avril 2026)" but May-2026 cards print
    "(mai 2026 )" with a trailing space inside the parens. Both must
    surface a non-empty publication_label."""
    apr = _comfy_w()
    assert apr.publication_label == "avril 2026"
    may = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w_may.pdf"), "wallonia"
    )
    assert may.publication_label == "mai 2026"


def test_unknown_contract_raises() -> None:
    async def _run() -> None:
        with pytest.raises(ExtractorError, match="unknown Luminus contract"):
            await EXTRACTORS["luminus"].fetch(None, "bogus", "wallonia")  # type: ignore[arg-type]

    asyncio.run(_run())


def test_smartflex_carries_a_formula_per_slot() -> None:
    """SmartFlex prints one monthly formula per TOU band, "Prelevement Heures
    pleines = 0,1300 x Belpex + 2,6200; ... creuses = 0,1080 ...;
    ... super-creuses = 0,0410 ...", and attributes its index to a past month:
    "Belpex = 92,61 EUR/MWh (valeur de l'indice de mars 2026)" on an April
    card. So the printed triplet is March's.

    The gate is that month attribution, not a cadence sentence: unlike its
    bi-hourly siblings this card's energy block carries none, the sentence
    sits under the injection block and governs that tariff. ComfyFlex
    attributes its index to a QUARTER, which is what keeps it out.
    """
    from custom_components.be_electricity_prices.providers._rates import TimeOfUseRates

    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    energy = snap.energy
    assert isinstance(energy, TimeOfUseRates)
    assert energy.month_indexed is True
    assert energy.formula_factor_peak == pytest.approx(0.1300 * 10 * 1.06)
    assert energy.formula_factor_transition == pytest.approx(0.1080 * 10 * 1.06)
    assert energy.formula_factor_offpeak == pytest.approx(0.0410 * 10 * 1.06)
    # The printed rates survive as the keyless fallback.
    assert energy.peak == pytest.approx(0.1554)


def test_a_smartflex_cohort_prices_each_slot_on_the_month() -> None:
    """The cohort leg becomes a three-band SpotMonthlyRates carrying the
    card's own weekend rule, so every existing month-mean gate keeps working
    without a second month-priced energy kind.
    """
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.cohort_legs import (
        _cohort_energy_from_archived,
    )
    from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh
    from custom_components.be_electricity_prices.providers._rates import (
        SpotMonthlyRates,
        TimeOfUseRates,
    )

    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    )
    leg = _cohort_energy_from_archived(snap)
    assert isinstance(leg, SpotMonthlyRates)
    assert leg.weekend_rule == "smartflex_seasonal"

    mean = 0.08096  # April 2026 arithmetic mean, EUR/kWh

    def at(hour: int) -> float:
        when = datetime(2026, 4, 15, hour, tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return energy_eur_per_kwh(leg, when, mean, meter="dynamic", region="wallonia")

    # Three distinct bands, each on its own formula, and each below the
    # printed rate because April's index sits under March's.
    assert at(8) == pytest.approx(0.1300 * 10 * 1.06 * mean + 2.6200 / 100 * 1.06)
    assert at(13) == pytest.approx(0.0410 * 10 * 1.06 * mean + 2.5400 / 100 * 1.06)
    assert at(8) > at(23) > at(13)
    printed = snap.energy
    assert isinstance(printed, TimeOfUseRates)
    assert at(8) < printed.peak


def test_happy_sunday_is_a_band_of_its_own() -> None:
    """The October 2026 SmartFlex card prints a fourth column, Happy Sunday:
    Sundays 11h-17h from 21/03 to 20/09, "Prelevement Happy Sunday = 0 x
    Belpex + 0", the super-creuses band now reading "Du lundi au samedi".
    Billed at super-creuses until now, 8,67 c/kWh on every such hour. Printed
    rate and month leg both take it; any other hour keeps its slot."""
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.cohort_legs import (
        _cohort_energy_from_archived,
    )
    from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh

    snap = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w_oct.pdf"), "wallonia"
    )
    printed = snap.energy
    assert isinstance(printed, TimeOfUseRates)
    assert printed.sunday == pytest.approx(0.0)
    assert printed.formula_factor_sunday == pytest.approx(0.0)
    assert printed.formula_base_sunday == pytest.approx(0.0)
    leg = _cohort_energy_from_archived(snap)
    assert leg is not None
    mean = 0.12932  # the card's August 2026 Belpex, EUR/kWh

    def at(rates: object, year: int, month: int, day: int, hour: int) -> float:
        moment = datetime(year, month, day, hour, tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return energy_eur_per_kwh(
            rates,  # type: ignore[arg-type]
            moment,
            mean,
            meter="dynamic",
            region="wallonia",
        )

    for rates in (printed, leg):
        # Sunday 6 June 2027, 13h: Happy Sunday.
        assert at(rates, 2027, 6, 6, 13) == pytest.approx(0.0)
        # Saturday the day before, same hour: super-creuses.
        assert at(rates, 2027, 6, 5, 13) > 0.05
        # Sunday 6 June at 18h, and Sunday 4 October 2026 at 13h (autumn,
        # the card prints "-"): their own slots, never zero.
        assert at(rates, 2027, 6, 6, 18) > 0.15
        assert at(rates, 2026, 10, 4, 13) > 0.15
    assert at(printed, 2027, 6, 5, 13) == pytest.approx(0.0867)
    assert at(printed, 2026, 10, 4, 13) == pytest.approx(0.1785)

    # The April card had no such column: nothing changes on it.
    april = parse_snapshot(
        "luminus_smartflex", fixture_text("luminus_smartflex_w.pdf"), "wallonia"
    ).energy
    assert isinstance(april, TimeOfUseRates)
    assert april.sunday is None
    assert april.formula_factor_sunday is None


async def test_smartflex_takes_the_prosumer_rate_its_card_bills_from_a_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The October 2026 SmartFlex card in Wallonia prints the Impact triplet
    where its prosumer column was, and its footnote still bills the tariff
    ("plus avantageux que la facturation du tarif prosumer"). A household
    under compensation was billed none of it: 35,77 EUR a month at 5 kVA on
    ORES. The rate is the DSO's and comes from a sibling card; Dynamic's card
    names no prosumer tariff and asks for none."""
    from custom_components.be_electricity_prices.providers import luminus

    asked: list[str] = []

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        for slug, fixture in (
            ("smartflex", "luminus_smartflex_w_oct.pdf"),
            ("comfyflex", "luminus_comfyflex_w_oct.pdf"),
            ("dynamic", "luminus_dynamic_w_oct.pdf"),
        ):
            if f"documentSlug={slug}&" in url:
                return fixture_text(fixture)
        raise ExtractorError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    snap = await luminus.fetch(
        None,  # type: ignore[arg-type]
        "luminus_smartflex",
        "wallonia",
    )
    # ComfyFlex's "ORES (EST) ... 14,10 85,84", the figure September's
    # SmartFlex card printed itself.
    assert snap.dsos["ores"].prosumer_eur_per_kva_year == pytest.approx(85.84)
    assert snap.dsos["aieg"].prosumer_eur_per_kva_year == pytest.approx(81.03)
    assert all(o.distribution_eco is not None for o in snap.dsos.values())
    assert len(asked) == 2

    asked.clear()
    dynamic = await luminus.fetch(
        None,  # type: ignore[arg-type]
        "luminus_dynamic",
        "wallonia",
    )
    assert all(o.prosumer_eur_per_kva_year is None for o in dynamic.dsos.values())
    assert len(asked) == 1


async def test_a_past_smartflex_month_takes_the_prosumer_rate_from_that_months_sibling(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """The archive path asks the same month's sibling card, by its own id."""
    from custom_components.be_electricity_prices.providers import luminus

    freezer.move_to("2026-11-05 12:00:00+01:00")

    pdfs: list[str] = []

    async def _fake_fetch_text(session: object, url: str, **kwargs: object) -> str:
        assert "signing=2026-10" in url
        return _archive_products(
            ("Luminus SmartFlex Electricité", "id-smartflex"),
            ("Luminus ComfyFlex Electricité", "id-comfyflex"),
        )

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        pdfs.append(url)
        if "productId=id-smartflex&" in url:
            return fixture_text("luminus_smartflex_w_oct.pdf")
        assert "productId=id-comfyflex&" in url and "date=2026-10" in url
        return fixture_text("luminus_comfyflex_w_oct.pdf")

    monkeypatch.setattr(luminus, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_smartflex",
        "wallonia",
        date(2026, 10, 1),
    )
    assert snap is not None
    assert snap.dsos["ores"].prosumer_eur_per_kva_year == pytest.approx(85.84)
    assert len(pdfs) == 2


async def test_a_prosumer_rate_no_sibling_prints_fails_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every sibling missing leaves nothing to bill the tariff from, and a
    card that bills it silently at zero is a wrong bill, so the card fails."""
    from custom_components.be_electricity_prices.providers import luminus

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        if "documentSlug=smartflex&" in url:
            return fixture_text("luminus_smartflex_w_oct.pdf")
        raise ExtractorError(f"HTTP 404 fetching {url}")

    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    with pytest.raises(ExtractorError, match="prosumer"):
        await luminus.fetch(
            None,  # type: ignore[arg-type]
            "luminus_smartflex",
            "wallonia",
        )


# ---- month archive (fetch_for_month) ------------------------------------------


def _archive_products(*rows: tuple[str, str]) -> str:
    import json

    return json.dumps([{"Product": name, "ProductId": pid} for name, pid in rows])


async def test_archive_resolves_the_product_id_then_the_months_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The price-list archive names a month's products with an opaque id, so
    the contract label is the join: "Luminus Comfy" is the archive's "Luminus
    Comfy Electricité", and the PDF is asked for by that id, the month and the
    region. Until this was wired a contract start date did nothing here."""
    from custom_components.be_electricity_prices.providers import luminus

    asked: list[str] = []

    async def _fake_fetch_text(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        assert "region=Wallonia" in url and "signing=2026-04" in url
        return _archive_products(
            ("Luminus BasicFix Online Electricité", "id-basicfix"),
            ("Luminus Comfy Electricité", "id-comfy"),
            ("Luminus Comfy+ Electricité", "id-comfy-plus"),
            ("Luminus Dynamic Online Electricité", "id-dynamic"),
            ("Tarif social Electricité", "id-social"),
        )

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        return fixture_text("luminus_comfy_w.pdf")  # "avril 2026"

    monkeypatch.setattr(luminus, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_comfy",
        "wallonia",
        date(2026, 4, 9),
    )
    assert snap is not None
    assert "productId=id-comfy&" in asked[-1]
    assert "date=2026-04" in asked[-1] and "region=Wallonia" in asked[-1]
    assert snap.publication_label == "avril 2026"
    assert snap.valid_until == date(2026, 4, 30)
    assert len(snap.dsos) > 0
    # The online marker folds away, "Comfy+" does not collide with "Comfy".
    assert luminus._archive_product_name("Luminus Dynamic Online Electricité") == (
        luminus._archive_product_name("Luminus Dynamic")
    )
    assert luminus._archive_product_name("Luminus Comfy+ Electricité") != (
        luminus._archive_product_name("Luminus Comfy")
    )


async def test_archive_is_not_asked_for_the_running_month(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """The archive kept serving October's first edition after the live card
    was reissued on the 2nd: ComfyFlex 0,1575 against the live 0,2008 on
    5 October 2026, so the month cost billed October 4,3 c/kWh under
    current_price. The running month is the live card's, and the archive is
    asked only once the month has closed."""
    from custom_components.be_electricity_prices.providers import luminus

    asked: list[str] = []

    async def _fake_fetch_text(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        return _archive_products(("Luminus Comfy Electricité", "id-comfy"))

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        return fixture_text("luminus_comfy_w.pdf")  # "avril 2026"

    monkeypatch.setattr(luminus, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    freezer.move_to("2026-04-30 23:30:00+02:00")
    for month in (date(2026, 4, 1), date(2026, 4, 30), date(2026, 5, 1)):
        assert (
            await luminus.fetch_for_month(
                None,  # type: ignore[arg-type]
                "luminus_comfy",
                "wallonia",
                month,
            )
            is None
        )
    assert asked == []
    freezer.move_to("2026-05-01 00:30:00+02:00")
    snap = await luminus.fetch_for_month(
        None,  # type: ignore[arg-type]
        "luminus_comfy",
        "wallonia",
        date(2026, 4, 1),
    )
    assert snap is not None and snap.publication_label == "avril 2026"


async def test_archive_answers_none_without_the_product_or_for_another_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A month whose list lacks the product fetches no PDF, and a card naming
    another month than the one asked for is rejected rather than billed."""
    from custom_components.be_electricity_prices.providers import luminus

    pdfs: list[str] = []

    async def _fake_fetch_text(session: object, url: str, **kwargs: object) -> str:
        return _archive_products(("Luminus Comfy Electricité", "id-comfy"))

    async def _fake_pdf(session: object, url: str, **kwargs: object) -> str:
        pdfs.append(url)
        return fixture_text("luminus_comfy_w.pdf")  # "avril 2026"

    monkeypatch.setattr(luminus, "fetch_text", _fake_fetch_text)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _fake_pdf)
    assert (
        await luminus.fetch_for_month(
            None,  # type: ignore[arg-type]
            "luminus_maxxfix",
            "wallonia",
            date(2026, 4, 1),
        )
        is None
    )
    assert pdfs == []
    assert (
        await luminus.fetch_for_month(
            None,  # type: ignore[arg-type]
            "luminus_comfy",
            "wallonia",
            date(2026, 6, 1),
        )
        is None
    )
    assert len(pdfs) == 1


async def test_archive_swallows_failures_and_unsold_regions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.be_electricity_prices.providers import luminus
    from custom_components.be_electricity_prices.providers.base import ExtractorError

    asked: list[str] = []

    async def _boom(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        raise ExtractorError("listing down")

    monkeypatch.setattr(luminus, "fetch_text", _boom)
    assert (
        await luminus.fetch_for_month(
            None,  # type: ignore[arg-type]
            "luminus_comfy",
            "wallonia",
            date(2026, 4, 1),
        )
        is None
    )
    assert len(asked) == 1
    assert (
        await luminus.fetch_for_month(
            None,  # type: ignore[arg-type]
            "luminus_comfy",
            "brussels",
            date(2026, 4, 1),
        )
        is None
    )
    assert (
        await luminus.fetch_for_month(
            None,  # type: ignore[arg-type]
            "luminus_gas",
            "wallonia",
            date(2026, 4, 1),
        )
        is None
    )
    assert len(asked) == 1
    assert EXTRACTORS["luminus"].fetch_for_month is luminus.fetch_for_month


async def test_a_closed_month_is_settled_in_place_on_the_index_the_next_card_names(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """A card prints its month-indexed rates at the previous month's index,
    and the next card names the month's own: "Belpex = 78,94 EUR/MWh (valeur
    de l'indice de avril 2026)" on May's. A keyless MaxxFlex April was billed
    at March's 92,61 for good. The card held for the month is re-priced in
    place, energy and feed-in, and keeps its 750 kWh campaign, which the
    archive's copy of the same month does not print."""
    from custom_components.be_electricity_prices.providers import luminus
    from custom_components.be_electricity_prices.providers._settle import (
        is_settled,
    )

    held = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    assert held.welcome_credit_kwh == 750.0
    energy0, inj0 = held.energy, held.injection
    assert isinstance(energy0, VariableRates) and inj0 is not None
    may = "Index (HTVA) : Belpex = 78,94 €/MWh (valeur de l'indice de avril 2026)"
    asked: list[str] = []

    async def _next_card(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        return may

    monkeypatch.setattr(luminus, "fetch_pdf_text", _next_card)
    freezer.move_to("2026-05-10 12:00:00+02:00")
    out = await luminus.settle_month(
        None,  # type: ignore[arg-type]
        "luminus_maxxflex",
        "wallonia",
        date(2026, 4, 1),
        held,
    )
    # May runs, so its card is the live one.
    assert asked and "documentSlug=maxxflex&" in asked[0]
    energy, inj = out.energy, out.injection
    assert isinstance(energy, VariableRates) and inj is not None
    assert energy0.formula_factor is not None and energy0.formula_base is not None
    assert energy.current == pytest.approx(
        energy0.formula_factor * 0.07894 + energy0.formula_base
    )
    assert energy.current < energy0.current
    assert energy.index_realised == pytest.approx(0.07894)
    assert inj0.factor is not None and inj0.base is not None
    assert inj.current == pytest.approx(inj0.factor * 0.07894 + inj0.base)
    assert is_settled(out) and not out.provisional
    assert replace(out, energy=energy0, injection=inj0) == held

    # BasicFlex settles its energy on Belpex RLP M and its feed-in on Belpex.
    basic = parse_snapshot(
        "luminus_basicflex", fixture_text("luminus_basicflex_w_oct.pdf"), "wallonia"
    )
    may = (
        "Index (HTVA) : Belpex RLP M = 180,00 €/MWh (valeur de l'indice de "
        "octobre 2026) Index (HTVA): Belpex = 170,00 €/MWh (valeur de l'indice "
        "de octobre 2026)"
    )
    freezer.move_to("2026-11-03 12:00:00+01:00")
    out = await luminus.settle_month(
        None,  # type: ignore[arg-type]
        "luminus_basicflex",
        "wallonia",
        date(2026, 10, 1),
        basic,
    )
    assert isinstance(out.energy, VariableRates) and out.injection is not None
    assert out.energy.index_realised == pytest.approx(0.18)
    assert out.injection.index_realised == pytest.approx(0.17)

    # The live card of the 1st was itself a month behind in October 2026: a
    # running next card naming another month leaves the month to be asked
    # again, while a closed one naming another month settles nothing.
    may = "Index (HTVA) : Belpex = 92,61 €/MWh (valeur de l'indice de mars 2026)"
    freezer.move_to("2026-05-01 08:00:00+02:00")
    waiting = await luminus.settle_month(
        None,  # type: ignore[arg-type]
        "luminus_maxxflex",
        "wallonia",
        date(2026, 4, 1),
        held,
    )
    assert waiting == replace(held, provisional=True)

    async def _product(*_a: object) -> str:
        return "id-maxxflex"

    monkeypatch.setattr(luminus, "_resolve_archive_product_id", _product)
    freezer.move_to("2026-07-10 12:00:00+02:00")
    asked.clear()
    kept = await luminus.settle_month(
        None,  # type: ignore[arg-type]
        "luminus_maxxflex",
        "wallonia",
        date(2026, 4, 1),
        held,
    )
    assert kept == held
    assert "productId=id-maxxflex&" in asked[0] and "date=2026-05" in asked[0]
    assert EXTRACTORS["luminus"].settle_month is luminus.settle_month


async def test_a_closed_quarter_settles_its_months_on_the_next_quarters_card(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """ComfyFlex prints its rates at the previous quarter's index, and the
    first card of the quarter after names the quarter's own: "Belpex = 94,25
    EUR/MWh (valeur de l'indice du 2ieme trimestre 2026)" on July's. Every
    month of the quarter settles there, each register on its own pair and the
    feed-in on its formula, and the leg stays indexed on the quarter."""
    from custom_components.be_electricity_prices.providers import luminus
    from custom_components.be_electricity_prices.providers._settle import (
        is_settled,
    )

    held = _comfyflex_v()  # April 2026, printed at the first quarter's 95,75
    energy0, inj0 = held.energy, held.injection
    assert isinstance(energy0, VariableRates) and inj0 is not None
    july = "Index (HTVA): Belpex = 94,25 €/MWh (valeur de l'indice du 2ième trimestre 2026)"
    asked: list[str] = []

    async def _next_card(session: object, url: str, **kwargs: object) -> str:
        asked.append(url)
        return july

    async def settle(month: date) -> SupplierSnapshot:
        return await luminus.settle_month(
            None,  # type: ignore[arg-type]
            "luminus_comfyflex",
            "flanders",
            month,
            held,
        )

    monkeypatch.setattr(luminus, "fetch_pdf_text", _next_card)
    # The second quarter still runs: its index is not known, nothing is asked.
    freezer.move_to("2026-06-20 12:00:00+02:00")
    assert await settle(date(2026, 5, 1)) == replace(held, provisional=True)
    assert asked == []

    freezer.move_to("2026-07-10 12:00:00+02:00")
    for month in (date(2026, 4, 1), date(2026, 6, 1)):
        asked.clear()
        out = await settle(month)
        # July runs, so its card is the live one.
        assert len(asked) == 1 and "documentSlug=comfyflex&" in asked[0]
        energy, inj = out.energy, out.injection
        assert isinstance(energy, VariableRates) and inj is not None
        assert energy.quarter_indexed and not energy.month_indexed
        assert energy.index_realised == pytest.approx(0.09425)
        for rate, factor, base in (
            (energy.current, energy0.formula_factor, energy0.formula_base),
            (energy.peak, energy0.formula_factor_peak, energy0.formula_base_peak),
            (
                energy.offpeak,
                energy0.formula_factor_offpeak,
                energy0.formula_base_offpeak,
            ),
            (
                energy.exclusive_night,
                energy0.formula_factor_exclusive_night,
                energy0.formula_base_exclusive_night,
            ),
        ):
            assert factor is not None and base is not None
            assert rate == pytest.approx(factor * 0.09425 + base)
        assert inj.current == pytest.approx(0.481 * 0.09425 - 0.006392)
        assert inj.index_realised == pytest.approx(0.09425)
        assert is_settled(out) and not out.provisional
        assert replace(out, energy=energy0, injection=inj0) == held

    # A live card naming another quarter leaves the month to be asked again,
    # and a closed one naming another settles nothing.
    july = (
        "Index (HTVA): Belpex = 95,75 €/MWh (valeur de l'indice du 1re trimestre 2026)"
    )
    assert await settle(date(2026, 5, 1)) == replace(held, provisional=True)

    async def _product(*_a: object) -> str:
        return "id-comfyflex"

    monkeypatch.setattr(luminus, "_resolve_archive_product_id", _product)
    freezer.move_to("2026-08-10 12:00:00+02:00")
    asked.clear()
    assert await settle(date(2026, 5, 1)) == held
    assert "productId=id-comfyflex&" in asked[0] and "date=2026-07" in asked[0]


async def test_the_month_cache_bills_a_closed_quarter_on_its_own_index(
    hass: Any, freezer: Any
) -> None:
    """The card archive holds April 2026 as the walk caught it, at the first
    quarter's index. Asked for it in July, the month cache settles it on the
    live card's second quarter index, so an entry without day-ahead prices no
    longer bills a closed quarter on the one before it."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.be_electricity_prices import snapshot_months
    from custom_components.be_electricity_prices.providers import luminus
    from custom_components.be_electricity_prices.snapshot_months import (
        ArchivedCard,
        _snapshot_for_month,
    )
    from custom_components.be_electricity_prices.snapshot_store import (
        _monthly_snapshots,
    )

    freezer.move_to("2026-07-10 10:00:00+02:00")
    caught = _comfyflex_v()
    github = AsyncMock(
        return_value=ArchivedCard(
            snapshot=caught, read_by_ocr=False, captured_live=True
        )
    )
    fetched = AsyncMock(
        return_value=(
            "Index (HTVA): Belpex = 94,25 €/MWh (valeur de l'indice du 2ième "
            "trimestre 2026)"
        )
    )
    _monthly_snapshots(hass).clear()
    with (
        patch.object(snapshot_months, "_archived_card_from_github", github),
        patch.object(luminus, "fetch_pdf_text", fetched),
    ):
        may = await _snapshot_for_month(
            hass,
            MagicMock(),
            EXTRACTORS["luminus"],
            "luminus_comfyflex",
            "flanders",
            date(2026, 5, 1),
            caught,
        )
    energy = may.energy
    assert isinstance(energy, VariableRates) and not may.provisional
    assert energy.index_realised == pytest.approx(0.09425)
    assert energy.formula_factor is not None and energy.formula_base is not None
    assert energy.current == pytest.approx(
        energy.formula_factor * 0.09425 + energy.formula_base
    )
    assert fetched.await_count == 1


def test_a_comfyflex_cohort_bills_each_register_on_its_own_pair() -> None:
    """Bolt prints one formula and carries each register's spread off the
    mono rate. ComfyFlex prints a pair per register instead, 0,1234 x Belpex
    on the day register against 0,1096 mono on April's card, so a spread read
    at one index is wrong at every other: the signed leg keeps the pairs."""
    from custom_components.be_electricity_prices.cohort_legs import (
        _cohort_energy_from_archived,
    )
    from custom_components.be_electricity_prices.providers._rates import (
        SpotMonthlyRates,
    )

    held = _comfyflex_v()
    energy = held.energy
    assert isinstance(energy, VariableRates)
    leg = _cohort_energy_from_archived(held)
    assert isinstance(leg, SpotMonthlyRates) and leg.quarter_indexed
    assert (leg.factor, leg.base) == (energy.formula_factor, energy.formula_base)
    assert (leg.factor_peak, leg.base_peak) == (
        energy.formula_factor_peak,
        energy.formula_base_peak,
    )
    assert (leg.factor_offpeak, leg.base_offpeak) == (
        energy.formula_factor_offpeak,
        energy.formula_base_offpeak,
    )
    assert (leg.factor_exclusive_night, leg.base_exclusive_night) == (
        energy.formula_factor_exclusive_night,
        energy.formula_base_exclusive_night,
    )


def test_the_new_customer_campaign_is_read_off_the_card() -> None:
    """Luminus prints a signing-month campaign and the integration bills it.

    "En tant que nouveau client, vous beneficiez d'une remise de 11% sur les
    couts energetiques, non-valable sur un compteur exclusif nuit, ... pendant
    12 mois pour la conclusion d'un contrat Luminus Comfy Electricite en avril
    2026." Worth 254,56 EUR on the September card at 3500 kWh, and nothing was
    read at all.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    april = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w.pdf"), "wallonia"
    )
    assert april.welcome_credit_pct_of_energy == pytest.approx(0.11)
    assert april.welcome_credit_kind == "pro_rata"
    assert april.welcome_credit_excludes_night_meter is True

    # A volume campaign is a cashback at the wait the card states.
    maxxflex = parse_snapshot(
        "luminus_maxxflex", fixture_text("luminus_maxxflex_w.pdf"), "wallonia"
    )
    assert maxxflex.welcome_credit_kwh == pytest.approx(750.0)
    assert maxxflex.welcome_credit_kind == "anniversary"
    assert maxxflex.welcome_credit_after_months == 12

    # The thousands separator. May's Comfy grants "une remise de 1.000 kWh",
    # which a plain decimal read turns into 1,0 kWh: 232 EUR of credit into
    # 23 cents. tier_bound_kwh is the shared reader for that, written for the
    # excise tranche bounds.
    may = parse_snapshot(
        "luminus_comfy", fixture_text("luminus_comfy_w_may.pdf"), "wallonia"
    )
    assert may.welcome_credit_kwh == pytest.approx(1000.0)

    # A card running no campaign says so; most months are this.
    for fixture, contract in (
        ("luminus_dynamic_w.pdf", "luminus_dynamic"),
        ("luminus_smartflex_w.pdf", "luminus_smartflex"),
    ):
        snap = parse_snapshot(contract, fixture_text(fixture), "wallonia")
        assert snap.welcome_credit_pct_of_energy is None, contract
        assert snap.welcome_credit_kwh is None, contract

    assert _extract_promo("no promo block on this card") == {}


def test_the_standing_loyalty_discount_is_not_read_as_a_campaign() -> None:
    """The cards print year-2 and year-3 loyalty discounts in the same block
    and in nearly the same words, and they are a different thing.

    "(*) 12 mois apres la date de debut, une reduction de 5 % sur les couts
    energetiques de votre consommation annuelle, non-valable sur un compteur
    exclusif nuit, applicable au tarif Luminus Comfy+ Electricite PENDANT 12
    mois ... au pro rata de la consommation de votre 2e annee."

    It says "pendant 12 mois" like a campaign does and carries its own
    exclusive-night exclusion, so reading the card as a whole attributed that
    exclusion to the campaign and a looser amount pattern would have billed
    5% as a welcome credit. Only the signing gate separates them.
    """
    plus = parse_snapshot(
        "luminus_comfyflex_plus",
        fixture_text("luminus_comfyflex_plus_w.pdf"),
        "wallonia",
    )
    # Its campaign is the volume one, and it states no night exclusion.
    assert plus.welcome_credit_kwh == pytest.approx(750.0)
    assert plus.welcome_credit_pct_of_energy is None
    assert plus.welcome_credit_excludes_night_meter is False


@pytest.mark.parametrize(
    ("fixture", "contract"),
    [
        # "pour la conclusion d'un contrat Luminus MaxxFix Electricite en
        # octobre 2026".
        ("luminus_maxxfix_w_oct.pdf", "luminus_maxxfix"),
        # "En tant que nouveau client pour Luminus ComfyFlex": no month in
        # the sentence, so the card's own "(octobre 2026)".
        ("luminus_comfyflex_w_oct.pdf", "luminus_comfyflex"),
    ],
)
def test_a_campaign_is_tied_to_the_month_it_is_signed_in(
    fixture: str, contract: str
) -> None:
    """Every Luminus campaign is for contracts signed in one month, and the
    card said so where no field carried it: the compare page and the ranking
    credited October's 675 kWh to a candidate as if the household had signed
    it on its own start date, 149 to 174 EUR off a 3500 kWh year-to-date. A
    contract signed in another month is now credited nothing, as on Bolt."""
    from custom_components.be_electricity_prices.cohort import _signed_in

    text = fixture_text(fixture)
    snap = parse_snapshot(contract, text, "wallonia")
    assert snap.welcome_credit_signing_month == date(2026, 10, 1)
    assert _signed_in(snap, date(2026, 10, 1)) is snap
    march = _signed_in(snap, date(2026, 3, 1))
    assert march.welcome_credit_kwh is None
    assert march.welcome_credit_pct_of_energy is None
    if "en octobre 2026" in " ".join(text.split()):
        # The sentence's month wins over the label where it prints one.
        moved = parse_snapshot(
            contract, text.replace("en octobre 2026.", "en novembre 2026."), "wallonia"
        )
        assert moved.publication_label == "octobre 2026"
        assert moved.welcome_credit_signing_month == date(2026, 11, 1)


def test_a_card_without_a_campaign_names_no_signing_month() -> None:
    snap = parse_snapshot(
        "luminus_dynamic", fixture_text("luminus_dynamic_w_oct.pdf"), "wallonia"
    )
    assert snap.welcome_credit_eur is None
    assert snap.welcome_credit_signing_month is None


@pytest.mark.parametrize(
    ("fixture", "contract", "rate"),
    [
        # "le prix unitaire en EUR/kWh TVAC de l'estimation annuelle de
        # l'energie fournie applicable aux compteurs mono-horaires": the
        # "Estimation annuelle" row, 20,70 where the card prints 18,87.
        ("luminus_maxxflex_w_oct.pdf", "luminus_maxxflex", 0.2070),
        # 23,85 against a printed 18,00.
        ("luminus_comfyflex_plus_w_oct.pdf", "luminus_comfyflex_plus", 0.2385),
        # "des couts energetiques": MaxxFix's own printed rate, as before.
        ("luminus_maxxfix_w_oct.pdf", "luminus_maxxfix", None),
    ],
)
def test_a_free_volume_is_valued_at_the_rate_its_card_names(
    fixture: str, contract: str, rate: float | None
) -> None:
    from custom_components.be_electricity_prices.fees import (
        _year_ahead_welcome_credit,
    )
    from custom_components.be_electricity_prices.pricing import (
        static_energy_eur_per_kwh,
    )

    snap = parse_snapshot(contract, fixture_text(fixture), "wallonia")
    assert snap.welcome_credit_kwh == pytest.approx(675.0)
    if rate is None:
        assert snap.welcome_credit_kwh_rate is None
        rate = static_energy_eur_per_kwh(snap.energy, "single")
        assert rate is not None
    else:
        assert snap.welcome_credit_kwh_rate == pytest.approx(rate)
    signed = date(2026, 10, 1)
    credit = _year_ahead_welcome_credit(snap, signed, signed, 99_999.0, 3500.0, 0.2)
    assert credit == pytest.approx(675.0 * rate)


def test_a_campaign_naming_its_product_needs_no_signing_phrase() -> None:
    """October 2026 reworded the ComfyFlex campaign without the signing gate.

    "En tant que nouveau client pour Luminus ComfyFlex Electricite, vous
    beneficiez pour chaque kWh consomme d'une remise de 27 % sur les couts
    energetiques ... pendant 12 mois. Cette remise sera repartie au pro rata
    sur votre (vos) prochain(s) decompte(s)." No "pour la conclusion d'un
    contrat", so nothing was read: 153,85 EUR at 3500 kWh in Wallonia.
    """
    snap = parse_snapshot(
        "luminus_comfyflex", fixture_text("luminus_comfyflex_w_oct.pdf"), "wallonia"
    )
    assert snap.welcome_credit_pct_of_energy == pytest.approx(0.27)
    assert snap.welcome_credit_kind == "pro_rata"
    assert snap.welcome_credit_kwh is None
    assert snap.welcome_credit_excludes_night_meter is False


def test_the_campaign_is_read_from_its_own_sentence_not_the_card() -> None:
    """A loyalty clause worded the campaign's way must not be billed as one.

    On the real cards the two happen to differ in word order, the campaign
    saying "remise de 33%" and the loyalty "10 % de remise" or "reduction de
    5 %", so scanning the whole card gets the right answer by luck rather
    than by rule. That luck is one reworded footnote from being wrong, and a
    loyalty discount billed as a welcome credit is money.

    So the campaign is read from its own sentence, anchored between "En tant
    que nouveau client" and the signing gate. This text is constructed, not a
    card: it is the real structure with the loyalty clause worded the way the
    campaign is, which is exactly the case the scope rule exists for.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    card = (
        "(*) 12 mois apres la date de debut, une remise de 5 % sur les couts "
        "energetiques de votre consommation annuelle, non-valable sur un "
        "compteur exclusif nuit, applicable pendant 12 mois. Cette remise sera "
        "repartie au pro rata de la consommation de votre 2e annee. "
        "(***) En tant que nouveau client, vous beneficiez d'une remise de 33% "
        "sur les couts energetiques, sur votre consommation annuel en heures "
        "pleines et creuses pendant 12 mois pour la conclusion d'un contrat "
        "Luminus Comfy Electricite en septembre 2026. Cette remise sera "
        "repartie au pro rata sur vos prochains decomptes."
    )
    promo = _extract_promo(card)
    assert promo["welcome_credit_pct_of_energy"] == pytest.approx(0.33)
    # The loyalty clause's own night exclusion is not the campaign's.
    assert "welcome_credit_excludes_night_meter" not in promo


def test_the_signing_gate_is_read_whichever_apostrophe_the_card_prints() -> None:
    """The gate was a plain string with a straight apostrophe, and the cards
    print both forms. Two ways that goes wrong.

    A card printing the campaign's own gate curly matched nothing, so the whole
    campaign went unread. And on a card printing both, the search bound the
    gate to the straight occurrence wherever it fell, while the anchor was
    taken from the start of the card, so the scope stretched from the first
    anchor to a gate in a later sentence and swallowed the loyalty clauses the
    scope rule exists to keep out.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    curly = (
        "(***) En tant que nouveau client, vous beneficiez d\u2019une remise de "
        "33% sur les couts energetiques pendant 12 mois pour la conclusion "
        "d\u2019un contrat Luminus Comfy Electricite en septembre 2026. Cette "
        "remise sera repartie au pro rata sur vos prochains decomptes."
    )
    assert _extract_promo(curly)["welcome_credit_pct_of_energy"] == pytest.approx(0.33)

    # The campaign sentence closes with a CURLY gate, and a later sentence
    # carries the straight one. The scope must end at the campaign's own.
    both = (
        "(***) En tant que nouveau client, vous beneficiez d\u2019une remise de "
        "29% sur les couts energetiques pendant 12 mois pour la conclusion "
        "d\u2019un contrat Luminus ComfyFlex en septembre 2026. Cette remise "
        "sera repartie au pro rata sur vos prochains decomptes. "
        "(*) 12 mois apres la date de debut, une remise de 5 % sur les couts "
        "energetiques, non-valable sur un compteur exclusif nuit, accordee 24 "
        "mois apres la signature via un cashback pour la conclusion d'un "
        "contrat de fourniture existant."
    )
    promo = _extract_promo(both)
    assert promo["welcome_credit_pct_of_energy"] == pytest.approx(0.29)
    # Nothing from the sentence after the campaign's own gate.
    assert "welcome_credit_excludes_night_meter" not in promo
    assert promo["welcome_credit_kind"] == "pro_rata"
    assert "welcome_credit_after_months" not in promo


def test_the_campaign_scope_is_one_sentence_on_a_card_carrying_two() -> None:
    """April's Comfy card prints two of these sentences: a flat "remise de
    60,00 EUR TVA incl." at 1.283 and the percentage campaign at 12.513, each
    closed by its own signing gate, and the first gate is curly where the
    second is straight.

    Pairing the first gate found with the first anchor before it crosses the
    two over, and the scope then runs the whole 11.512 characters between them.
    The 11% was read out of that only because it happened to be the first
    percentage inside it.

    The card as published gives the same answer either way, which is why this
    puts a loyalty clause between the two sentences: real wording, in the gap
    the old scope covered and the new one does not. Under the old reader it is
    the first percentage in scope and is billed as the campaign.
    """
    import re

    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    text = fixture_text("luminus_comfy_w.pdf")
    assert _extract_promo(text)["welcome_credit_pct_of_energy"] == pytest.approx(0.11)

    flat = re.sub(r"\s+", " ", text)
    loyalty = (
        " (*) 12 mois apres la date de debut, une remise de 5 % sur les couts "
        "energetiques, accordee 24 mois apres la date de debut via un "
        "cashback. "
    )
    # Between the flat sentence and the percentage one: inside the span the
    # old scope reached, outside the sentence the new one reads.
    spliced = flat[:6000] + loyalty + flat[6000:]

    promo = _extract_promo(spliced)
    assert promo["welcome_credit_pct_of_energy"] == pytest.approx(0.11), (
        "the campaign's own sentence states 11%; 5% is the loyalty clause "
        "that sits between the two campaign sentences"
    )
    assert promo["welcome_credit_kind"] == "pro_rata"
    assert "welcome_credit_after_months" not in promo


def test_a_campaign_stated_only_in_euro_is_read() -> None:
    """April's Comfy card prints "une remise de 60,00 EUR TVA incl." under the
    signing gate, and there was no EUR pattern, so a card whose only campaign
    is a flat amount granted nothing.

    That card also prints an 11% campaign lower down, and it keeps winning: the
    snapshot holds one payout kind and one wait, so the percentage and volume
    shapes are preferred where a card states one. Mixing the amount of one
    sentence with the conditions of another is what marked the 11% campaign as
    a cashback at the wait quoted after the flat one.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    flat_only = (
        "En tant que nouveau client, vous beneficiez d'une remise de 60,00 EUR "
        "TVA incl. pour la conclusion d'un contrat Luminus Comfy Electricite "
        "en avril 2026."
    )
    promo = _extract_promo(flat_only)
    assert promo["welcome_credit_eur"] == pytest.approx(60.0)
    assert "welcome_credit_pct_of_energy" not in promo
    assert "welcome_credit_kwh" not in promo

    # An amount with a thousands separator is left UNREAD rather than guessed
    # at: no Luminus card prints one, and a reader that took it would have to
    # decide whether the dot separates thousands or decimals, billing 1,25 EUR
    # or 125.000 when it decided wrong.
    assert _extract_promo(flat_only.replace("60,00 EUR", "1.250,00 EUR")) == {}

    # The real April card states both, and the percentage still wins whole.
    april = _extract_promo(fixture_text("luminus_comfy_w.pdf"))
    assert april["welcome_credit_pct_of_energy"] == pytest.approx(0.11)
    assert april["welcome_credit_kind"] == "pro_rata"
    assert "welcome_credit_eur" not in april


def test_an_anchor_without_a_gate_cannot_borrow_the_next_sentence_s() -> None:
    """Pairing each anchor with the gate that FOLLOWS it lets an anchor whose
    own sentence carries no gate borrow the next sentence's, and the span then
    runs across everything between the two.

    The scope rule was narrowed from "the whole card" to "whatever sits between
    an anchor and the next gate", which is smaller and still wrong: on this
    text, real wording throughout, the loyalty clause's 5% is the first
    percentage in the borrowed span, so the card grants 33% and 5% is read.
    215,99 EUR a year on a 3.500 kWh Comfy, and the loyalty clause's own
    exclusive-night exclusion comes with it, taking a night-metered household
    to nothing.

    Pairing each GATE with the LAST anchor before it cannot cross a sentence
    boundary, because the nearest anchor is the one the gate belongs to.
    """
    from custom_components.be_electricity_prices.providers._luminus_cards import (
        _extract_promo,
    )

    card = (
        "En tant que nouveau client, 12 mois apres la date de debut, une "
        "remise de 5 % sur les couts energetiques, non-valable sur un compteur "
        "exclusif nuit, applicable pendant 12 mois au pro rata de la "
        "consommation de votre 2e annee "
        "(***) En tant que nouveau client, vous beneficiez d'une remise de 33% "
        "sur les couts energetiques pendant 12 mois pour la conclusion d'un "
        "contrat Luminus Comfy Electricite en septembre 2026."
    )
    promo = _extract_promo(card)
    assert promo["welcome_credit_pct_of_energy"] == pytest.approx(0.33)
    # The loyalty clause's exclusion is not the campaign's.
    assert "welcome_credit_excludes_night_meter" not in promo


def test_the_pre_august_excise_schedule_is_read_into_bands() -> None:
    """Until July 2026 the tax block prints the first excise tier only and the
    footnote prints the schedule ("20.001-50.000 kWh : 4,8188 c€/kWh"), so a
    household above 20.000 kWh was billed 5,0329 on every kWh. Read as bands,
    the resolver blends them over the household's volume, as on Engie's and
    Mega's cards."""
    from custom_components.be_electricity_prices.providers._resolve import (
        resolve_excise_band,
    )

    snap = parse_snapshot(
        "luminus_comfyflex", fixture_text("luminus_comfyflex_v.pdf"), "flanders"
    )
    assert snap.taxes.federal_excise == pytest.approx(0.050329)
    bands = snap.taxes.federal_excise_bands
    assert bands is not None
    assert [x for band in bands for x in band] == pytest.approx(
        [3000.0, 0.050329, 20000.0, 0.050329, 50000.0, 0.048188]
    )
    blended = resolve_excise_band(snap, 25000.0).taxes.federal_excise
    assert blended == pytest.approx((20000 * 0.050329 + 5000 * 0.048188) / 25000)
    # 3500 kWh sits in the first two tiers and is billed exactly as before.
    assert resolve_excise_band(snap, 3500.0).taxes.federal_excise == pytest.approx(
        0.050329
    )


def test_a_flat_excise_footnote_is_one_rate() -> None:
    """From August 2026 the footnote names the same rate in every tier: one
    rate, not a schedule, so the law's excise still applies to the card."""
    snap = parse_snapshot(
        "luminus_comfyflex", fixture_text("luminus_comfyflex_w_oct.pdf"), "wallonia"
    )
    assert snap.taxes.federal_excise_bands is None


# The cards reissued on 29 October 2026 against the first edition of the
# month: the same figures under new labels, with no green energy rows. The
# first editions on file of the indexed cards are those of 1 October, priced
# on August's Belpex where the reissue is on September's, so only the rates
# they print differ.
_REISSUED = (
    ("luminus_maxxfix", "luminus_maxxfix_v_oct", "flanders"),
    ("luminus_maxxflex", "luminus_maxxflex_w_oct", "wallonia"),
    ("luminus_comfyflex", "luminus_comfyflex_w_oct", "wallonia"),
    ("luminus_smartflex", "luminus_smartflex_v_oct", "flanders"),
    ("luminus_dynamic", "luminus_dynamic_w_oct", "wallonia"),
)
_PRINTED_RATES = ("current", "peak", "transition", "offpeak", "exclusive_night")


def _unprinted(snap: SupplierSnapshot) -> SupplierSnapshot:
    """``snap`` without the rates a card prints at the index it was cut on."""

    def _clear(leg: Any) -> Any:
        fields = {name: None for name in _PRINTED_RATES if hasattr(leg, name)}
        return replace(leg, **fields)

    injection = None if snap.injection is None else _clear(snap.injection)
    return replace(snap, energy=_clear(snap.energy), injection=injection)


@pytest.mark.parametrize(("contract", "fixture", "region"), _REISSUED)
def test_a_reissued_card_reads_as_its_first_edition(
    contract: str, fixture: str, region: str
) -> None:
    """Every figure of the reissue is the first edition's: the energy row
    under "Énergie fournie + énergie verte", or in Flanders "Énergie fournie,
    énergie verte + cogénération" wrapped over the figures, the annual
    estimate the MaxxFlex campaign is valued at, and ComfyFlex's formula
    with its Endex term gone and its index line above the block."""
    first = parse_snapshot(contract, fixture_text(f"{fixture}.pdf"), region)
    levy = first.taxes.flanders_renewables or first.taxes.wallonia_renewables
    reissued = parse_snapshot(
        contract, fixture_text(f"{fixture}_reissue.pdf"), region, renewables=levy
    )
    assert _unprinted(reissued) == _unprinted(first)


def test_a_reissued_card_reads_its_own_printed_rates() -> None:
    """The rates the reissue prints, on September's Belpex."""
    comfyflex = parse_snapshot(
        "luminus_comfyflex",
        fixture_text("luminus_comfyflex_w_oct_reissue.pdf"),
        "wallonia",
        renewables=0.0303,
    )
    assert isinstance(comfyflex.energy, VariableRates)
    assert comfyflex.energy.current == pytest.approx(0.2061)
    assert comfyflex.energy.peak == pytest.approx(0.2394)
    assert comfyflex.energy.quarter_indexed
    smartflex = parse_snapshot(
        "luminus_smartflex",
        fixture_text("luminus_smartflex_v_oct_reissue.pdf"),
        "flanders",
        renewables=0.0156,
    )
    assert isinstance(smartflex.energy, TimeOfUseRates)
    assert (
        smartflex.energy.peak,
        smartflex.energy.transition,
        smartflex.energy.offpeak,
        smartflex.energy.sunday,
    ) == pytest.approx((0.2417, 0.2043, 0.0933, 0.0))
    maxxflex = parse_snapshot(
        "luminus_maxxflex",
        fixture_text("luminus_maxxflex_w_oct_reissue.pdf"),
        "wallonia",
        renewables=0.0303,
    )
    assert maxxflex.welcome_credit_kwh_rate == pytest.approx(0.2070)


@pytest.mark.parametrize(("contract", "fixture", "region"), _REISSUED)
def test_a_reissued_card_is_not_billed_without_its_green_energy_cost(
    contract: str, fixture: str, region: str
) -> None:
    """The reissue prints no green energy cost, and Luminus still bills it on
    top of the energy price, so the card alone is refused rather than billed
    with none."""
    with pytest.raises(ExtractorError, match="renewables"):
        parse_snapshot(contract, fixture_text(f"{fixture}_reissue.pdf"), region)


def _reissue_archive(
    monkeypatch: pytest.MonkeyPatch, archived: dict[str, str]
) -> list[str]:
    """Serve the reissued MaxxFix card live and ``archived`` (month to
    fixture) from the archive; returns the archive months asked."""
    from custom_components.be_electricity_prices.providers import luminus

    asked: list[str] = []

    async def _products(session: object, url: str, **kwargs: object) -> str:
        month = url.split("signing=")[1]
        asked.append(month)
        if month not in archived:
            return _archive_products()
        return _archive_products(("Luminus MaxxFix Electricité", f"id-{month}"))

    async def _card(session: object, url: str, **kwargs: object) -> str:
        if "documentSlug=maxxfix&" in url:
            return fixture_text("luminus_maxxfix_v_oct_reissue.pdf")
        month = url.split("productId=id-")[1].split("&")[0]
        assert f"date={month}&" in url and "region=Flanders" in url
        return fixture_text(archived[month])

    monkeypatch.setattr(luminus, "fetch_text", _products)
    monkeypatch.setattr(luminus, "fetch_pdf_text", _card)
    return asked


async def test_a_reissued_card_takes_its_green_energy_cost_from_the_archive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive keeps the month's first edition, which prints the rows."""
    from custom_components.be_electricity_prices.providers import luminus

    asked = _reissue_archive(monkeypatch, {"2026-10": "luminus_maxxfix_v_oct.pdf"})
    snap = await luminus.fetch(
        None,  # type: ignore[arg-type]
        "luminus_maxxfix",
        "flanders",
    )
    assert snap.taxes.flanders_renewables == pytest.approx(0.0117 + 0.0039)
    assert asked == ["2026-10"]


async def test_a_reissued_card_never_takes_the_cost_of_another_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The printed amounts are "valables pour le trimestre en cours": an
    October card whose month the archive holds only as a reissue asks no
    September card and is refused."""
    from custom_components.be_electricity_prices.providers import luminus

    asked = _reissue_archive(
        monkeypatch,
        {
            "2026-10": "luminus_maxxfix_v_oct_reissue.pdf",
            "2026-09": "luminus_maxxfix_v_oct.pdf",
        },
    )
    with pytest.raises(ExtractorError, match="green energy cost"):
        await luminus.fetch(
            None,  # type: ignore[arg-type]
            "luminus_maxxfix",
            "flanders",
        )
    assert asked == ["2026-10"]


async def test_a_reissued_card_walks_back_through_its_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A December card asks December, then November, then October, and takes
    the first that prints the cost."""
    from custom_components.be_electricity_prices.providers import luminus

    asked = _reissue_archive(
        monkeypatch,
        {
            "2026-11": "luminus_maxxfix_v_oct_reissue.pdf",
            "2026-10": "luminus_maxxfix_v_oct.pdf",
        },
    )
    renewables = await luminus._archived_renewables(
        None,  # type: ignore[arg-type]
        luminus._CONTRACTS_BY_ID["luminus_maxxfix"],
        "flanders",
        date(2026, 12, 1),
    )
    assert renewables == pytest.approx(0.0156)
    assert asked == ["2026-12", "2026-11", "2026-10"]


def test_a_reissued_card_never_bills_its_estimate_as_its_rate() -> None:
    """The annual estimate repeats the energy row's label, so a card whose
    rate row did not read must be refused, not billed the estimate."""
    text = fixture_text("luminus_maxxflex_w_oct_reissue.pdf")
    row = "Énergie fournie + énergie verte 22,02 25,67 19,12 19,12"
    assert row in text
    with pytest.raises(ExtractorError, match="variable energy block"):
        parse_snapshot(
            "luminus_maxxflex",
            text.replace(row, "Énergie fournie + énergie verte"),
            "wallonia",
            renewables=0.0303,
        )
