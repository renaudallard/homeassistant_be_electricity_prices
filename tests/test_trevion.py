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

"""Trevion PDF extractor tests against April and September 2026 fixtures."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import re
from typing import Any

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS, REGION_FLANDERS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import ExtractorError
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.trevion import (
    _BY_ID,
    _extract_validity,
    _find_card,
    fetch,
    fetch_for_month,
    parse_snapshot,
    probe,
)
from tests import fixture_text, make_text_session

_VAST = "trevion_vast_2026-09.pdf"
_VAST_APRIL = "trevion_vast_2026-04.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


def test_trevion_is_registered_with_six_flemish_contracts() -> None:
    extractor = EXTRACTORS["trevion"]
    assert extractor.label == "Trevion"
    assert {contract.id for contract in extractor.contracts} == {
        "groene_energie_vast",
        "groene_stroom_flex",
        "groene_energie_dynamisch",
        "groene_energie_dynamisch_plus",
        "lifepowr",
        "energreen",
    }
    assert all(
        contract.regions == frozenset({REGION_FLANDERS})
        for contract in extractor.contracts
    )
    assert all(not contract.spot_indexed_injection for contract in extractor.contracts)


@pytest.mark.parametrize("layout", [False, True])
def test_fixed_card_parses_both_pdf_text_orders(layout: bool) -> None:
    snap = parse_snapshot("groene_energie_vast", fixture_text(_VAST, layout=layout))
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == pytest.approx(0.190518)
    assert snap.energy.peak == pytest.approx(0.210743)
    assert snap.energy.offpeak == pytest.approx(0.173402)
    assert snap.energy.exclusive_night == pytest.approx(0.173402)
    assert snap.energy.yearly_fixed_fee == pytest.approx(39.0)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.057615)
    assert snap.injection.peak == pytest.approx(0.063329)
    assert snap.injection.offpeak == pytest.approx(0.043330)
    assert snap.injection.bi_hourly is True
    assert snap.valid_until == date(2026, 9, 30)


@pytest.mark.parametrize("layout", [False, True])
def test_april_fixed_card_reads_the_residential_excise_tier(layout: bool) -> None:
    """Until July 2026 the card printed the federal excise as four degressive
    tiers. A household pays the 0-3 MWh one, which is the tier every sibling
    extractor reads; this one took the last value of the block, the 50-1000
    MWh industrial tier, and billed every pre-August month 0,29 c/kWh low. The
    two readers lay the block out differently (one value per row against the
    four labels followed by the four values), and both must land on 5,03288."""
    snap = parse_snapshot(
        "groene_energie_vast", fixture_text(_VAST_APRIL, layout=layout)
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    assert snap.valid_until == date(2026, 4, 30)


@pytest.mark.parametrize(
    (
        "contract_id",
        "fixture",
        "factor",
        "base",
        "fee",
        "inj_current",
        "inj_factor",
        "inj_base",
    ),
    [
        (
            "groene_stroom_flex",
            "trevion_flex_2026-09.pdf",
            1.1342,
            0.0159,
            39.0,
            0.0501545,
            0.95,
            -0.025,
        ),
        (
            "lifepowr",
            "trevion_lifepowr_2026-09.pdf",
            1.1342,
            0.0159,
            26.5,
            0.056199,
            0.90,
            -0.015,
        ),
    ],
)
def test_monthly_contracts_parse_rlp_and_spp_formulas(
    contract_id: str,
    fixture: str,
    factor: float,
    base: float,
    fee: float,
    inj_current: float,
    inj_factor: float,
    inj_base: float,
) -> None:
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == pytest.approx(factor)
    assert snap.energy.base == pytest.approx(base)
    assert snap.energy.rlp_indexed is True
    assert snap.energy.rlp_blend == "flanders"
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(inj_current)
    assert snap.injection.factor == pytest.approx(inj_factor)
    assert snap.injection.base == pytest.approx(inj_base)
    assert snap.injection.spp_indexed is True


@pytest.mark.parametrize(
    ("contract_id", "fixture", "factor", "fee", "inj_factor", "inj_base"),
    [
        (
            "groene_energie_dynamisch",
            "trevion_dynamic_2026-09.pdf",
            1.1342,
            39.0,
            0.86,
            -0.005,
        ),
        (
            "groene_energie_dynamisch_plus",
            "trevion_dynamic_plus_2026-09.pdf",
            1.1342,
            39.0,
            0.86,
            -0.005,
        ),
        (
            "energreen",
            "trevion_energreen_2026-09.pdf",
            1.06,
            26.5,
            1.0,
            -0.013,
        ),
    ],
)
def test_dynamic_contracts_parse_quarter_hourly_formulas(
    contract_id: str,
    fixture: str,
    factor: float,
    fee: float,
    inj_factor: float,
    inj_base: float,
) -> None:
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(factor)
    assert snap.energy.base == pytest.approx(0.01378)
    assert snap.energy.yearly_fixed_fee == pytest.approx(fee)
    assert snap.energy.quarter_hourly is True
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(inj_factor)
    assert snap.injection.base == pytest.approx(inj_base)


@pytest.mark.parametrize(
    "fixture",
    [
        _VAST,
        "trevion_flex_2026-09.pdf",
        "trevion_dynamic_2026-09.pdf",
        "trevion_dynamic_plus_2026-09.pdf",
        "trevion_lifepowr_2026-09.pdf",
        "trevion_energreen_2026-09.pdf",
    ],
)
def test_every_card_parses_regulated_flemish_costs(fixture: str) -> None:
    contract_id = {
        _VAST: "groene_energie_vast",
        "trevion_flex_2026-09.pdf": "groene_stroom_flex",
        "trevion_dynamic_2026-09.pdf": "groene_energie_dynamisch",
        "trevion_dynamic_plus_2026-09.pdf": "groene_energie_dynamisch_plus",
        "trevion_lifepowr_2026-09.pdf": "lifepowr",
        "trevion_energreen_2026-09.pdf": "energreen",
    }[fixture]
    snap = parse_snapshot(contract_id, _layout(fixture))
    assert set(snap.dsos) == FLUVIUS_KEYS
    assert all(
        overlay.distribution_single is not None
        and overlay.capacity_eur_per_kw_year is not None
        and overlay.data_management_per_year is not None
        for overlay in snap.dsos.values()
    )
    assert snap.taxes.federal_excise == pytest.approx(0.04876)
    assert snap.taxes.energy_contribution == pytest.approx(0.0)
    assert snap.taxes.flanders_renewables == pytest.approx(0.016112)
    assert snap.taxes.energy_fund_eur_per_month == pytest.approx(0.0)
    assert snap.taxes.vat_rate == pytest.approx(0.0)
    assert snap.taxes.published_vat_rate == pytest.approx(0.0)


async def test_listing_resolves_all_cards_without_confusing_dynamic_plus() -> None:
    html = "\n".join(
        f'<a href="/tariefkaarten/{name}">{name}</a>'
        for name in (
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202609.pdf",
            "Tariefkaart-Groene-Stroom-Flex-Particulier-202609.pdf",
            "Tariefkaart-Groene-Energie-Dynamisch-Particulier-202609-1.pdf",
            "Tariefkaart-Groene-Energie-Dynamisch-Plus-Particulier-202609-1.pdf",
            "Tariefkaart-LifePowrByTrevion-Particulier-202609.pdf",
            "Tariefkaart-EnergreenByTrevion-Particulier-202609.pdf",
        )
    )
    session = make_text_session(html)
    resolved = {
        contract_id: await _find_card(session, contract)
        for contract_id, contract in _BY_ID.items()
    }
    assert "Dynamisch-Particulier" in resolved["groene_energie_dynamisch"][0]
    assert "Dynamisch-Plus-Particulier" in resolved["groene_energie_dynamisch_plus"][0]
    assert all(label == "2026-09" for _, label in resolved.values())


async def test_listing_entry_that_is_not_a_month_is_skipped_not_raised() -> None:
    """The six digits are read as YYYYMM, and a file named for something else
    matches the pattern just as well. Building a date out of it raised a bare
    ValueError, which reached the user as "month must be in 1..12" on the card
    that asks them to report a layout change."""
    contract = _BY_ID["groene_energie_vast"]
    good = "Trevion-tariefkaart-Groene-energie-VAST-particulier-202609.pdf"
    junk = "Trevion-tariefkaart-Groene-energie-VAST-particulier-202699.pdf"

    def _listing(*names: str) -> Any:
        return make_text_session(
            "\n".join(f'<a href="/tariefkaarten/{n}">{n}</a>' for n in names)
        )

    # The unparseable entry is skipped and the real card still resolves.
    url, label = await _find_card(_listing(junk, good), contract)
    assert label == "2026-09"
    assert good in url
    # With nothing left, the caller gets an ExtractorError like any other miss.
    with pytest.raises(ExtractorError) as err:
        await _find_card(_listing(junk), contract)
    assert "name no month" in str(err.value)


async def test_fetch_for_month_parses_archive_and_rejects_missing_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.be_electricity_prices.providers import trevion

    html = (
        '<a href="/tariefkaarten/'
        'Trevion-tariefkaart-Groene-energie-VAST-particulier-202604.pdf">card</a>'
    )
    session = make_text_session(html)
    render = AsyncMock(return_value=_layout(_VAST_APRIL))
    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", render)
    snap = await fetch_for_month(
        session, "groene_energie_vast", REGION_FLANDERS, date(2026, 4, 12)
    )
    assert snap is not None
    assert snap.publication_label == "2026-04"
    assert snap.valid_until == date(2026, 4, 30)
    assert (
        await fetch_for_month(
            session, "groene_energie_vast", REGION_FLANDERS, date(2026, 5, 1)
        )
        is None
    )


async def test_listing_resolves_the_newest_month_of_a_product() -> None:
    """Two months of one product on the listing: the newer one is the card.
    The listing tests used one month per product, so the resolver's max()
    could have been a min() and every test stayed green, which is the
    0.12.5 Ecopower shape: the oldest card, downloading and parsing clean."""
    contract = _BY_ID["groene_energie_vast"]
    html = "\n".join(
        f'<a href="/tariefkaarten/{n}">{n}</a>'
        for n in (
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202608.pdf",
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202609.pdf",
        )
    )
    url, label = await _find_card(make_text_session(html), contract)
    assert label == "2026-09"
    assert "202609" in url


async def test_fetch_for_month_takes_the_requested_month_not_the_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The archive lookup hands the month to the resolver; without it the
    resolver answers the newest card, which parses and passes for April's."""
    from custom_components.be_electricity_prices.providers import trevion

    html = "\n".join(
        f'<a href="/tariefkaarten/{n}">{n}</a>'
        for n in (
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202604.pdf",
            "Trevion-tariefkaart-Groene-energie-VAST-particulier-202609.pdf",
        )
    )
    render = AsyncMock(return_value=_layout(_VAST_APRIL))
    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", render)
    snap = await fetch_for_month(
        make_text_session(html),
        "groene_energie_vast",
        REGION_FLANDERS,
        date(2026, 4, 12),
    )
    assert snap is not None and snap.publication_label == "2026-04"
    assert "202604" in render.call_args.args[1]


async def test_fetch_for_month_rejects_a_card_that_names_another_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CDN serving the current card under an archived name must not bill
    April at September's rates: the validity cross-check answers None."""
    from custom_components.be_electricity_prices.providers import trevion

    html = (
        '<a href="/tariefkaarten/'
        'Trevion-tariefkaart-Groene-energie-VAST-particulier-202604.pdf">card</a>'
    )
    monkeypatch.setattr(
        trevion,
        "fetch_pdf_text_layout",
        AsyncMock(return_value=_layout("trevion_vast_2026-09.pdf")),
    )
    assert (
        await fetch_for_month(
            make_text_session(html),
            "groene_energie_vast",
            REGION_FLANDERS,
            date(2026, 4, 12),
        )
        is None
    )


async def test_unsupported_contracts_and_regions_do_not_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.be_electricity_prices.providers import trevion

    freshness = AsyncMock(return_value="etag")
    monkeypatch.setattr(trevion, "head_freshness_key", freshness)
    with pytest.raises(ExtractorError, match="unknown or unsupported Trevion contract"):
        await fetch(None, "groene_energie_vast", "wallonia")  # type: ignore[arg-type]
    assert (
        await fetch_for_month(
            None,  # type: ignore[arg-type]
            "unknown",
            REGION_FLANDERS,
            date(2026, 9, 1),
        )
        is None
    )
    assert await probe(None, "groene_energie_vast", "wallonia") is None  # type: ignore[arg-type]
    freshness.assert_not_awaited()


def test_may_flex_card_writes_its_feed_in_formula_with_an_x() -> None:
    """The March to May 2026 Flex cards print the feed-in formula as
    "0,095 x Belpex_SPP_BE", the later ones with an asterisk; the backfill
    left those months absent until both signs were read."""
    snap = parse_snapshot("groene_stroom_flex", _layout("trevion_flex_2026-05.pdf"))
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == pytest.approx(1.1342)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(0.95)
    assert snap.injection.base == pytest.approx(-0.025)
    # 0,095 x 29,17 EUR/MWh - 2,5 c/kWh, the April index the card names.
    assert snap.injection.current == pytest.approx(0.95 * 0.02917 - 0.025)
    assert snap.valid_until == date(2026, 5, 31)


def test_lifepowr_cards_before_june_are_the_dynamic_product() -> None:
    """LifePowr billed per quarter-hour on Belpex 15 MTU until May 2026 and
    became a monthly Belpex_RLP_VL product in June. The May card is read as
    the dynamic product it was, under the contract's monthly kind, so the
    year-to-date walk bills those months on the grid they were billed on."""
    snap = parse_snapshot("lifepowr", _layout("trevion_lifepowr_2026-05.pdf"))
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True
    assert snap.energy.factor == pytest.approx(1.06)
    assert snap.energy.base == pytest.approx(0.01378)
    assert snap.energy.yearly_fixed_fee == pytest.approx(26.5)
    assert snap.injection is not None
    assert snap.injection.factor == pytest.approx(1.0)
    assert snap.injection.base == pytest.approx(-0.013)
    assert snap.valid_until == date(2026, 5, 31)


@pytest.mark.parametrize(
    "row",
    [
        "Bijdrage op de energie (c€/kWh) 0",
        "Bijdrage energiefonds met domicilie (€/maand) (2) 0",
    ],
)
def test_a_zero_tax_row_the_card_drops_is_read_as_zero(row: str) -> None:
    """Both rows print 0 since August 2026. The other Flemish extractors made
    them optional when the levy was abolished, because suppliers answered by
    deleting the row; this parser still required them, so Trevion doing the
    same would have taken all six contracts offline with no figure to read."""
    text = fixture_text("trevion_vast_2026-09.pdf", layout=True)
    assert row in text
    snap = parse_snapshot("groene_energie_vast", text.replace(row + "\n", ""))
    assert snap.taxes.energy_contribution == 0.0
    assert snap.taxes.energy_fund_eur_per_month == 0.0
    assert snap.taxes.federal_excise == pytest.approx(0.04876)


def test_validity_parses_dutch_month_name() -> None:
    assert _extract_validity("geldig in september 2026") == date(2026, 9, 30)
    assert _extract_validity("geen periode") is None


@pytest.mark.parametrize(
    ("old", "message"),
    [
        ("Piekuren", "fixed energy table not found"),
        ("Bijzondere accijns", "tax block not found"),
        ("Tweevoudig", "shared meter costs not found"),
    ],
)
def test_missing_fixed_card_sections_fail_loudly(old: str, message: str) -> None:
    with pytest.raises(ExtractorError, match=message):
        parse_snapshot("groene_energie_vast", _layout(_VAST).replace(old, "missing"))


@pytest.mark.parametrize(
    ("contract_id", "fixture"),
    [
        ("groene_energie_dynamisch", "trevion_dynamic_2026-09.pdf"),
        ("groene_energie_dynamisch_plus", "trevion_dynamic_plus_2026-09.pdf"),
        ("energreen", "trevion_energreen_2026-09.pdf"),
    ],
)
def test_the_formula_reproduces_the_price_the_card_prints(
    contract_id: str, fixture: str
) -> None:
    """Price the card's own formula at the card's own index and meet its own
    figure.

    A constant like ``factor == 0.11342`` says only that the parser still does
    what it did yesterday. It cannot notice a missing unit conversion, which is
    how every Trevion dynamic and monthly contract came to bill its commodity
    leg at a tenth of the card: 1,5958 c€/kWh where the card, three lines from
    the formula, prints 15,96. The daily live check caught it on a bound the
    unit tests could not.

    So this reads both figures off the card and checks they agree. The card
    quotes the index its own simulation used ("de laatst gekende waarde is deze
    van augustus 2026 (128,55 EUR/MWh)") and prints the resulting price in the
    SMR3 row, so nothing here is hardcoded but the tolerance, and a future card
    carries its own numbers with it.
    """
    text = _layout(fixture)
    quoted = re.search(
        r"laatst gekende waarde is deze van \w+ \d{4} \(([\d,]+)\s*€/MWh\)", text
    )
    printed = re.search(r"SMR3\s+([\d,]+)\s", text)
    assert quoted and printed, "the card no longer quotes its own index or price"
    spot = float(quoted.group(1).replace(",", ".")) / 1000.0
    want = float(printed.group(1).replace(",", "."))

    snap = parse_snapshot(contract_id, text)
    # Narrowed rather than guarded: energy is a union of six rate kinds and
    # only the spot-indexed pair carries a factor at all.
    assert isinstance(snap.energy, (DynamicRates, SpotMonthlyRates))
    got = (snap.energy.factor * spot + snap.energy.base) * 100.0
    # A centime: the card rounds its own printed figure to two decimals.
    assert got == pytest.approx(want, abs=0.01)


def _flex_listing(*stamps: str) -> str:
    return "\n".join(
        '<a href="/tariefkaarten/'
        f'Trevion-tariefkaart-Groene-Stroom-Flex-particulier-{stamp}.pdf">card</a>'
        for stamp in stamps
    )


async def test_fetch_for_month_settles_both_legs_on_the_next_cards_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The monthly card prices both legs on a Belpex mean of the delivery
    month, which is not known while that month runs, so what it prints is the
    last value published. The following card names this month's, with the month
    spelled out: "De laatst gekende waarde is deze van april 2026 (85,60
    EUR/MWh)" for consumption and 29,17 for the feed-in credit.

    Both were left to a mean computed here from the spot cache. That mean is a
    near miss rather than the same number, because the card defines each index
    on "de Belgische kwartierprijzen" and the cache is hourly: over the 2026
    months the credit's index came out about 0,9 EUR/MWh high and the energy
    leg's 0,19 low.
    """
    from custom_components.be_electricity_prices.providers import trevion

    may_text = _layout("trevion_flex_2026-05.pdf")
    april_text = may_text.replace("mei 2026", "april 2026")

    async def _render(_session: object, url: str, *a: object, **k: object) -> str:
        return april_text if "202604" in url else may_text

    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", _render)
    april = await fetch_for_month(
        make_text_session(_flex_listing("202604", "202605")),
        "groene_stroom_flex",
        REGION_FLANDERS,
        date(2026, 4, 1),
    )
    assert april is not None
    energy = april.energy
    assert isinstance(energy, SpotMonthlyRates)
    assert energy.index_realised == pytest.approx(0.0856)
    inj = april.injection
    assert inj is not None
    assert inj.index_realised == pytest.approx(0.02917)
    assert inj.factor is not None and inj.base is not None
    assert inj.current == pytest.approx(inj.factor * 0.02917 + inj.base)
    # A month with a settling card behind it is a fact, not an estimate.
    assert april.provisional is False


async def test_settling_a_month_reads_the_listing_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both months are listed on the same page, so it is fetched once and both
    are resolved out of it. Asking twice is a round trip that buys nothing, on
    the walk that already costs one extra PDF per month."""
    from custom_components.be_electricity_prices.providers import trevion

    may_text = _layout("trevion_flex_2026-05.pdf")
    april_text = may_text.replace("mei 2026", "april 2026")
    listing = AsyncMock(return_value=_flex_listing("202604", "202605"))
    monkeypatch.setattr(trevion, "fetch_text", listing)

    async def _render(_session: object, url: str, *a: object, **k: object) -> str:
        return april_text if "202604" in url else may_text

    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", _render)
    april = await fetch_for_month(
        object(),  # type: ignore[arg-type]
        "groene_stroom_flex",
        REGION_FLANDERS,
        date(2026, 4, 1),
    )
    assert april is not None
    assert april.injection is not None
    assert april.injection.index_realised == pytest.approx(0.02917)
    assert listing.await_count == 1


async def test_fetch_for_month_flags_a_month_the_next_card_cannot_settle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no following card the printed estimate stands, and the row says so:
    the month cache re-asks after its TTL and the archive walk leaves it absent
    rather than filing last month's index as this month's fact."""
    from custom_components.be_electricity_prices.providers import trevion

    monkeypatch.setattr(
        trevion,
        "fetch_pdf_text_layout",
        AsyncMock(return_value=_layout("trevion_flex_2026-05.pdf")),
    )
    may = await fetch_for_month(
        make_text_session(_flex_listing("202605")),
        "groene_stroom_flex",
        REGION_FLANDERS,
        date(2026, 5, 1),
    )
    assert may is not None
    assert may.provisional is True
    assert may.injection is not None
    assert may.injection.index_realised is None


async def test_a_card_indexed_on_neither_asks_for_no_second_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixed contract is settled by neither index, so it must not pay for
    the following month's download, and must never come back provisional
    because that month is not out yet."""
    from custom_components.be_electricity_prices.providers import trevion

    render = AsyncMock(return_value=_layout(_VAST_APRIL))
    monkeypatch.setattr(trevion, "fetch_pdf_text_layout", render)
    snap = await fetch_for_month(
        make_text_session(
            '<a href="/tariefkaarten/'
            'Trevion-tariefkaart-Groene-energie-VAST-particulier-202604.pdf">c</a>'
        ),
        "groene_energie_vast",
        REGION_FLANDERS,
        date(2026, 4, 1),
    )
    assert snap is not None
    assert snap.provisional is False
    assert render.await_count == 1
