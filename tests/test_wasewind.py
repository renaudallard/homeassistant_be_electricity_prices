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

"""Wase Wind extractor tests against its 2026 cards and the 2027 ones."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import fluvius
from custom_components.be_electricity_prices.const import (
    DOMAIN,
    FLUVIUS_KEYS,
    REGION_FLANDERS,
)
from custom_components.be_electricity_prices.fluvius import AreaTerms
from custom_components.be_electricity_prices.providers import EXTRACTORS, wasewind
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _resolve_snapshot,
)
from tests import FIXTURES, fixture_text, fluvius_network

_VOL = "wasewind_vol_dynamisch"
_VAST_DYNAMISCH = "wasewind_vast_dynamisch"
_VAST_VARIABEL = "wasewind_vast_variabel"
_VAST = "wasewind_vast"
_NAMES = {
    _VOL: "vol_dynamisch",
    _VAST_DYNAMISCH: "vast_dynamisch",
    _VAST_VARIABEL: "vast_variabel",
    _VAST: "vast",
}


def _card(contract: str, month: str) -> str:
    return fixture_text(f"wasewind_{_NAMES[contract]}_{month}.pdf", layout=True)


def _network() -> dict[str, AreaTerms]:
    return fluvius_network(2026)


def _snap(contract: str, month: str) -> SupplierSnapshot:
    return wasewind.parse_snapshot(contract, _card(contract, month), _network(), "t://")


def test_wase_wind_sells_four_flemish_contracts() -> None:
    extractor = EXTRACTORS["wasewind"]
    assert extractor.label == "Wase Wind"
    by_id = {c.id: c for c in extractor.contracts}
    assert set(by_id) == set(_NAMES)
    assert by_id[_VOL].label == "Vol dynamisch"
    assert by_id[_VAST_DYNAMISCH].label == "Vast-dynamisch"
    assert by_id[_VAST_VARIABEL].label == "Vast-variabel"
    assert by_id[_VAST].label == "Vast"
    assert by_id[_VOL].kind == by_id[_VAST_DYNAMISCH].kind == "dynamic"
    assert by_id[_VAST_VARIABEL].kind == "variable"
    assert by_id[_VAST].kind == "fixed"
    assert not by_id[_VAST].month_indexed_energy
    assert by_id[_VAST_VARIABEL].month_indexed_energy
    assert not by_id[_VOL].month_indexed_energy
    for contract in by_id.values():
        assert contract.regions == frozenset({REGION_FLANDERS})
        assert contract.earlier_network_cards
    assert extractor.fetch_for_month is not None
    assert extractor.probe is not None


def test_vol_dynamisch() -> None:
    """0,106 x Belpex kwartier + 1,59 c€/kWh including VAT, the feed-in
    0,10 x Belpex kwartier - 1,50 without it."""
    snap = _snap(_VOL, "2026-11")
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == pytest.approx(1.06)
    assert snap.energy.base == pytest.approx(0.0159)
    assert snap.energy.quarter_hourly
    assert snap.energy.yearly_fixed_fee == 65.0
    assert snap.injection is not None
    assert (snap.injection.factor, snap.injection.base) == pytest.approx((1.0, -0.015))
    assert snap.injection.current is None
    assert not snap.injection.month_indexed
    assert snap.publication_label == "november 2026"
    assert snap.valid_until == date(2026, 11, 30)
    # The card's own check: 20,6733 at the estimated 180,03122 EUR/MWh.
    assert snap.energy.factor * 0.18003122 + snap.energy.base == pytest.approx(
        0.206733, abs=1e-6
    )


def test_the_january_card_prints_its_prices_with_a_point() -> None:
    snap = _snap(_VOL, "2026-01")
    assert isinstance(snap.energy, DynamicRates)
    assert (snap.energy.factor, snap.energy.base) == pytest.approx((1.06, 0.0159))
    assert snap.valid_until == date(2026, 1, 31)


def test_vast_dynamisch() -> None:
    """Half the index, 0,106 x 50% x Belpex kwartier + 7,72, and a fixed
    2,0 c€/kWh feed-in."""
    snap = _snap(_VAST_DYNAMISCH, "2026-11")
    assert isinstance(snap.energy, DynamicRates)
    assert (snap.energy.factor, snap.energy.base) == pytest.approx((0.53, 0.0772))
    assert snap.energy.quarter_hourly
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.02)
    assert snap.injection.factor is None
    assert snap.supplier_prosumer_eur_per_kva_year is None


def test_vast_variabel() -> None:
    """The delivery month's mean, one formula for the single, day and night
    registers and one for an exclusive-night meter, each at the price the
    card works out at the VNR estimate."""
    snap = _snap(_VAST_VARIABEL, "2026-11")
    energy = snap.energy
    assert isinstance(energy, VariableRates)
    assert energy.month_indexed
    assert not energy.rlp_indexed
    assert (energy.formula_factor, energy.formula_base) == pytest.approx((0.61, 0.0772))
    assert (
        energy.formula_factor_exclusive_night,
        energy.formula_base_exclusive_night,
    ) == pytest.approx((0.53, 0.061))
    assert energy.formula_factor_peak is None
    assert energy.current == pytest.approx(0.18287)
    assert energy.exclusive_night == pytest.approx(0.152812)
    assert energy.yearly_fixed_fee == 65.0
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.02)
    # The analog meter's forfait, read but billed only under compensation.
    assert snap.supplier_prosumer_eur_per_kva_year == 30.0


def test_vast_variabel_in_january_printed_another_factor() -> None:
    energy = _snap(_VAST_VARIABEL, "2026-01").energy
    assert isinstance(energy, VariableRates)
    assert energy.formula_factor == pytest.approx(0.69)
    assert energy.current == pytest.approx(0.132292)


def test_a_price_printed_on_the_line_after_its_estimate() -> None:
    """June's card prints the estimated Belpex on its own line and the
    exclusive-night price with a point."""
    energy = _snap(_VAST_VARIABEL, "2026-06").energy
    assert isinstance(energy, VariableRates)
    assert energy.current == pytest.approx(0.138903)
    assert energy.exclusive_night == pytest.approx(0.114611)


def test_the_2027_cards_are_read_in_their_new_layout() -> None:
    """Published in November 2026 for January 2027 on: no "50%", and the two
    fixed products price the feed-in on a formula too."""
    vol = _snap(_VOL, "2027-00")
    assert isinstance(vol.energy, DynamicRates)
    assert (vol.energy.factor, vol.energy.base) == pytest.approx((1.06, 0.0069))
    assert vol.publication_label == "januari 2027"
    assert vol.valid_until == date(2027, 1, 31)
    dynamisch = _snap(_VAST_DYNAMISCH, "2027-00")
    assert dynamisch.injection is not None
    assert (dynamisch.injection.factor, dynamisch.injection.base) == pytest.approx(
        (0.5, 0.02)
    )
    assert not dynamisch.injection.month_indexed
    variabel = _snap(_VAST_VARIABEL, "2027-00")
    assert isinstance(variabel.energy, VariableRates)
    assert (variabel.energy.formula_factor, variabel.energy.formula_base) == (
        pytest.approx((0.583, 0.0922))
    )
    feed_in = variabel.injection
    assert feed_in is not None
    assert feed_in.month_indexed
    assert (feed_in.factor, feed_in.base) == pytest.approx((0.2, 0.02))
    assert feed_in.current == pytest.approx(0.049122)


def test_vast() -> None:
    """Sold from 2027: fixed prices for the single, day and night registers
    and an exclusive-night one, and a fixed feed-in, for the whole year."""
    snap = _snap(_VAST, "2027-00")
    energy = snap.energy
    assert isinstance(energy, FixedRates)
    assert energy.single == pytest.approx(0.20)
    assert energy.peak is None and energy.offpeak is None
    assert energy.exclusive_night == pytest.approx(0.16)
    assert energy.yearly_fixed_fee == 65.0
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(0.04)
    assert snap.injection.factor is None and snap.injection.base is None
    assert snap.supplier_prosumer_eur_per_kva_year == 30.0
    assert snap.taxes.flanders_renewables == pytest.approx(0.0146)
    assert snap.taxes.federal_levies_unread
    assert snap.taxes.card_vat_rate == 0.06
    # The card names no month, so it prices January unless listed for another.
    assert snap.publication_label == "januari 2027"
    assert snap.valid_until == date(2027, 1, 31)
    march = wasewind.parse_snapshot(
        _VAST, _card(_VAST, "2027-00"), _network(), listed_month=date(2027, 3, 1)
    )
    assert march.valid_until == date(2027, 3, 31)
    with pytest.raises(ExtractorError, match="does not cover 2026-11"):
        wasewind.parse_snapshot(
            _VAST, _card(_VAST, "2027-00"), _network(), listed_month=date(2026, 11, 1)
        )


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            (
                "Enkelvoudig, piek en dal 20,00",
                "Enkelvoudig, piek en dal 0,1 x Belpex maand + 2",
            ),
            "a formula on a card of fixed prices",
        ),
        (("Uitsluitend nacht 16,00", ""), "one fixed price expected"),
        (("Alle meetmomenten 4,00", ""), "one fixed price expected"),
        (("Energiekost in c€/kWh", "Energiekost in €/kWh"), "not in c€/kWh"),
        (
            ("Injectievergoeding in c€/kWh", "Injectievergoeding in €/kWh"),
            "not in c€/kWh",
        ),
        (("TOT 01/01/28", "TOT 01/01/29"), "not one calendar year"),
    ],
)
def test_a_vast_card_that_does_not_read_is_refused(
    change: tuple[str, str], message: str
) -> None:
    text = _card(_VAST, "2027-00")
    assert change[0] in text
    with pytest.raises(ExtractorError, match=message):
        wasewind.parse_snapshot(_VAST, text.replace(*change), _network())


def test_the_network_is_fluvius_s_sheet_on_the_card_s_basis() -> None:
    snap = _snap(_VOL, "2026-11")
    assert set(snap.dsos) == FLUVIUS_KEYS
    for dso, terms in _network().items():
        overlay = snap.dsos[dso]
        assert overlay.distribution_single == pytest.approx(terms.kwh_normal * 1.06)
        assert overlay.distribution_exclusive_night == pytest.approx(
            terms.kwh_exclusive_night * 1.06
        )
        assert overlay.capacity_eur_per_kw_year == pytest.approx(
            terms.capacity_per_kw_year * 1.06
        )
        assert overlay.data_management_per_year == pytest.approx(
            terms.data_management_per_year * 1.06
        )
        assert overlay.transport == 0.0
    assert snap.network_year is None


def test_the_levies_are_the_certificates_and_the_law_s() -> None:
    taxes = _snap(_VOL, "2026-11").taxes
    assert taxes.flanders_renewables == pytest.approx(0.0146)
    assert taxes.federal_levies_unread
    assert taxes.federal_excise == taxes.energy_contribution == 0.0
    assert taxes.energy_fund_eur_per_month == 0.0
    assert taxes.vat_rate == 0.0
    assert taxes.card_vat_rate == 0.06


def test_a_resolved_card_bills_the_excise_of_the_law() -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "wasewind",
            "contract": _VOL,
            "region": REGION_FLANDERS,
            "dso": "fluvius_imewo",
            "meter": "mono",
        },
    )
    taxes = _resolve_snapshot(
        entry, _snap(_VOL, "2026-11"), delivery_month=date(2026, 11, 1)
    ).taxes
    assert taxes.federal_excise == pytest.approx(0.0422 * 1.06)
    assert taxes.energy_contribution == 0.0
    assert not taxes.federal_levies_unread


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("+ 1,59", "+ 1,69"), "does not give the price printed"),
        (("Particulier incl. 6% btw", "Particulier"), "VAT rate not stated"),
        (("btw niet van toepassing", ""), "feed-in VAT basis not stated"),
        (("in €/MWh tijdens", "tijdens"), "indexed on Belpex kwartier"),
        (("Groene stroom certificaten 1,13", ""), "certificates not found"),
        (("Abonnement: € 65 per jaar", ""), "yearly fee not found"),
    ],
)
def test_a_card_that_does_not_read_is_refused(
    change: tuple[str, str], message: str
) -> None:
    text = _card(_VOL, "2026-11")
    assert change[0] in text
    with pytest.raises(ExtractorError, match=message):
        wasewind.parse_snapshot(_VOL, text.replace(*change), _network())


def test_another_product_s_card_is_refused() -> None:
    with pytest.raises(ExtractorError, match="not the Vast-dynamisch card"):
        wasewind.parse_snapshot(_VAST_DYNAMISCH, _card(_VOL, "2026-11"), _network())
    with pytest.raises(ExtractorError, match="not the Vast card"):
        wasewind.parse_snapshot(_VAST, _card(_VAST_DYNAMISCH, "2027-00"), _network())
    with pytest.raises(ExtractorError, match="not the Vast-variabel card"):
        wasewind.parse_snapshot(_VAST_VARIABEL, _card(_VAST, "2027-00"), _network())


def test_a_network_short_of_an_area_is_refused() -> None:
    short = dict(_network())
    del short["fluvius_west"]
    with pytest.raises(ExtractorError, match="no network for fluvius_west"):
        wasewind.parse_snapshot(_VOL, _card(_VOL, "2026-11"), short)


def _listing(year: int, product: str = "vast_dynamisch") -> str:
    name = f"wasewind_tariefkaarten_{year}_{product}.html"
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_the_listing_gives_each_month_s_card() -> None:
    product = wasewind._BY_ID[_VAST_DYNAMISCH]
    cards = wasewind.listed_cards(_listing(2026), product, 2026)
    assert sorted(cards) == [date(2026, m, 1) for m in range(1, 12)]
    site = "https://www.wasewind.be/frontend/files/userfiles/files"
    assert cards[date(2026, 4, 1)] == f"{site}/Tariefkaart%20WW-VD-HH-2604(1).pdf"
    assert cards[date(2026, 9, 1)] == (
        f"{site}/Tariefkaarten/Tarieven%202026/2026-09/Tariefkaart%20WW-VD-HH-2609.pdf"
    )
    # Another product's code, or another year's card, is not this listing's.
    assert wasewind.listed_cards(_listing(2026), wasewind._BY_ID[_VOL], 2026) == {}
    assert wasewind.listed_cards(_listing(2026), product, 2027) == {}


def test_the_card_published_before_its_year_prices_january() -> None:
    product = wasewind._BY_ID[_VAST_DYNAMISCH]
    cards = wasewind.listed_cards(_listing(2027), product, 2027)
    assert list(cards) == [date(2027, 1, 1)]
    assert cards[date(2027, 1, 1)].endswith("Tariefkaart%20WW-VD-HH-2700.pdf")


def _hold_network(year: int) -> None:
    for dso, terms in _network().items():
        fluvius._area[(year, dso)] = terms


async def _fetch(
    contract: str, card: str, listing: str, **kwargs: Any
) -> SupplierSnapshot | None:
    """``fetch`` over the listing and the card fixtures, or ``fetch_for_month``
    with a ``year_month``."""
    month = kwargs.pop("year_month", None)
    text = AsyncMock(return_value=listing)
    pdf = AsyncMock(return_value=_card(contract, card))
    with (
        patch.object(wasewind, "fetch_text", text),
        patch.object(wasewind, "fetch_pdf_text_layout", pdf),
        patch.object(wasewind, "ensure_area_terms", AsyncMock()),
    ):
        if month is None:
            return await wasewind.fetch(None, contract, REGION_FLANDERS)  # type: ignore[arg-type]
        return await wasewind.fetch_for_month(
            None,  # type: ignore[arg-type]
            contract,
            REGION_FLANDERS,
            month,
        )


async def test_fetch_reads_the_running_month_s_card(freezer: Any) -> None:
    freezer.move_to("2026-11-14 12:00:00+01:00")
    _hold_network(2026)
    with patch.object(wasewind, "fetch_pdf_text_layout") as pdf:
        pdf.return_value = _card(_VAST_DYNAMISCH, "2026-11")
        with (
            patch.object(
                wasewind, "fetch_text", AsyncMock(return_value=_listing(2026))
            ),
            patch.object(wasewind, "ensure_area_terms", AsyncMock()),
        ):
            snap = await wasewind.fetch(None, _VAST_DYNAMISCH, REGION_FLANDERS)  # type: ignore[arg-type]
    assert pdf.call_args.args[1].endswith(
        "Tariefkaarten/Tariefkaart%20WW-VD-HH-2611.pdf"
    )
    assert snap.publication_label == "november 2026"
    assert snap.network_year is None


async def test_fetch_never_reads_a_card_ahead_of_the_month(freezer: Any) -> None:
    freezer.move_to("2026-04-10 12:00:00+02:00")
    _hold_network(2026)
    with patch.object(wasewind, "fetch_pdf_text_layout") as pdf:
        pdf.return_value = _card(_VAST_DYNAMISCH, "2026-11")
        with (
            patch.object(
                wasewind, "fetch_text", AsyncMock(return_value=_listing(2026))
            ),
            patch.object(wasewind, "ensure_area_terms", AsyncMock()),
        ):
            await wasewind.fetch(None, _VAST_DYNAMISCH, REGION_FLANDERS)  # type: ignore[arg-type]
    assert pdf.call_args.args[1].endswith("WW-VD-HH-2604(1).pdf")


async def test_a_year_whose_sheets_are_not_out_bills_the_year_before(
    freezer: Any,
) -> None:
    """Fluvius had not listed its 2027 sheets on 14 November 2026, so a 2027
    card stands on 2026's until they are, and says so."""
    freezer.move_to("2027-01-05 12:00:00+01:00")
    _hold_network(2026)
    snap = await _fetch(_VAST_DYNAMISCH, "2027-00", _listing(2027))
    assert snap is not None
    assert snap.network_year == 2026
    assert snap.dsos["fluvius_imewo"].distribution_single == pytest.approx(
        _network()["fluvius_imewo"].kwh_normal * 1.06
    )


async def test_no_sheet_held_is_refused(freezer: Any) -> None:
    freezer.move_to("2026-11-14 12:00:00+01:00")
    with pytest.raises(ExtractorError, match="Fluvius network sheets not read"):
        await _fetch(_VAST_DYNAMISCH, "2026-11", _listing(2026))
    # The month walk asks again rather than file the month as having no card.
    with pytest.raises(ExtractorError, match="Fluvius network sheets not read"):
        await _fetch(
            _VAST_DYNAMISCH, "2026-11", _listing(2026), year_month=date(2026, 11, 1)
        )


async def test_fetch_for_month() -> None:
    _hold_network(2026)
    snap = await _fetch(
        _VAST_DYNAMISCH, "2026-11", _listing(2026), year_month=date(2026, 11, 1)
    )
    assert snap is not None
    assert snap.valid_until == date(2026, 11, 30)
    # A month the listing has no card for, and a card that is not the month's.
    assert (
        await _fetch(
            _VAST_DYNAMISCH, "2026-11", _listing(2026), year_month=date(2026, 12, 1)
        )
        is None
    )
    assert (
        await _fetch(
            _VAST_DYNAMISCH, "2026-11", _listing(2026), year_month=date(2026, 10, 1)
        )
        is None
    )


async def test_vast_prices_on_its_first_card_before_its_year(freezer: Any) -> None:
    """Sold from 2027, so in November 2026 an entry on it reads the card
    published for January, and a month before 2027 has no card."""
    freezer.move_to("2026-11-14 12:00:00+01:00")
    _hold_network(2026)
    listing = AsyncMock(return_value=_listing(2027, "vast"))
    with (
        patch.object(wasewind, "fetch_text", listing),
        patch.object(
            wasewind,
            "fetch_pdf_text_layout",
            AsyncMock(return_value=_card(_VAST, "2027-00")),
        ) as pdf,
        patch.object(wasewind, "ensure_area_terms", AsyncMock()),
    ):
        snap = await wasewind.fetch(None, _VAST, REGION_FLANDERS)  # type: ignore[arg-type]
        probed = await wasewind.probe(None, _VAST, REGION_FLANDERS)  # type: ignore[arg-type]
        before = await wasewind.fetch_for_month(
            None,  # type: ignore[arg-type]
            _VAST,
            REGION_FLANDERS,
            date(2026, 11, 1),
        )
    assert listing.call_args.args[1].endswith("/tariefkaarten-2027-incl.-btw-vast")
    assert pdf.call_args.args[1].endswith(
        "Tarieven%202027/Tariefkaart%20WW-V-HH-2700.pdf"
    )
    assert probed == pdf.call_args.args[1]
    assert snap.publication_label == "januari 2027"
    assert snap.network_year == 2026
    assert before is None
    assert listing.await_count == 2


async def test_vast_fetch_for_month() -> None:
    _hold_network(2026)
    snap = await _fetch(
        _VAST, "2027-00", _listing(2027, "vast"), year_month=date(2027, 1, 1)
    )
    assert snap is not None
    assert snap.valid_until == date(2027, 1, 31)
    assert (
        await _fetch(
            _VAST, "2027-00", _listing(2027, "vast"), year_month=date(2027, 2, 1)
        )
        is None
    )


def _gezin(year: int) -> str:
    return (FIXTURES / f"wasewind_tarieven_{year}_gezin.html").read_text(
        encoding="utf-8"
    )


async def test_discover_reads_the_year_s_household_tariffs(freezer: Any) -> None:
    """The running year's page, and the next year's once it is up: in
    November 2026 Vast is listed only on the 2027 page."""
    freezer.move_to("2026-11-14 12:00:00+01:00")

    def page(_session: object, url: str) -> str:
        for year in (2026, 2027):
            if url.endswith(f"/tarieven-{year}-gezin"):
                return _gezin(year)
        raise ExtractorError("HTTP 404")

    with patch.object(wasewind, "fetch_text", AsyncMock(side_effect=page)):
        assert await wasewind.discover(None) == set(_NAMES)  # type: ignore[arg-type]
    # The next year's page not up yet.
    with patch.object(wasewind, "fetch_text", AsyncMock(return_value=_gezin(2026))):
        assert await wasewind.discover(None) == set(_NAMES) - {_VAST}  # type: ignore[arg-type]
    freezer.move_to("2027-02-01 12:00:00+01:00")
    with patch.object(wasewind, "fetch_text", AsyncMock(side_effect=page)):
        assert await wasewind.discover(None) == set(_NAMES)  # type: ignore[arg-type]
    with patch.object(
        wasewind, "fetch_text", AsyncMock(side_effect=ExtractorError("HTTP 500"))
    ):
        assert await wasewind.discover(None) == set()  # type: ignore[arg-type]


# Card text a host could serve instead, on which a pattern tried from every
# place it could start would cost the square of its length.
@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            lambda: wasewind._FORMULA_RE.search("Alle meetmomenten " + "1" * 200_000),
            id="formula digits",
        ),
        pytest.param(
            lambda: wasewind._FORMULA_RE.search(
                "\nAlle meetmomenten 0,1 x Belpex" * 20_000
            ),
            id="formula rows",
        ),
        pytest.param(
            lambda: wasewind._TITLE_RE.search("Tariefkaart" + " " * 200_000 + "1"),
            id="title blanks",
        ),
        pytest.param(
            lambda: wasewind._HREF_RE.findall('href="' * 40_000),
            id="unclosed links",
        ),
        pytest.param(
            lambda: wasewind._CERTIFICATES_RE.search(
                "Bijdrage certificaten" + "x" * 200_000
            ),
            id="certificates heading",
        ),
        pytest.param(
            lambda: wasewind._INDEX_UNIT_RE.search("Belpex maand =" + "x" * 200_000),
            id="index footnote",
        ),
        pytest.param(
            lambda: wasewind._MONTH_RE.search(
                "Vlaamse nutsregulator in november" + " " * 200_000
            ),
            id="month blanks",
        ),
        pytest.param(
            lambda: wasewind._FEE_RE.search("Abonnement € " + " " * 200_000),
            id="fee blanks",
        ),
    ],
)
def test_a_crafted_card_is_read_in_time_linear_in_its_length(
    read: Callable[[], object],
) -> None:
    started = time.perf_counter()
    read()
    assert time.perf_counter() - started < 2.0
