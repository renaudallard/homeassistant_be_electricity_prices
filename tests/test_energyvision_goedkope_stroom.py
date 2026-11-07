"""EnergyVision's original Goedkope stroom, withdrawn after its September 2025
card, which prices every month it is billed for."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import fluvius
from custom_components.be_electricity_prices.const import DOMAIN, FLUVIUS_KEYS
from custom_components.be_electricity_prices.injection import _floor_injection
from custom_components.be_electricity_prices.providers import (
    EXTRACTORS,
    energyvision,
    withdrawn_on,
)
from custom_components.be_electricity_prices.providers._energyvision_contracts import (
    _CONTRACTS_BY_ID,
)
from custom_components.be_electricity_prices.providers._rates import (
    FixedRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers._resolve import (
    resolve_volume_tier,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _proxy_for_month,
    _resolve_snapshot,
)
from tests import fixture_text

_GS = "energyvision_goedkope_stroom"
_FIXTURE = "energyvision_goedkope_stroom_sep2025.pdf"
_FILES = "https://www.energyvision.be/sites/default/files/inline-files/"
_ANTWERPEN = "fluvius_antwerpen"


def _text() -> str:
    return fixture_text(_FIXTURE, layout=True)


def _card() -> SupplierSnapshot:
    return energyvision.parse_snapshot(_GS, _text(), f"{_FILES}EV-GS-0925-01_1.pdf")


def _entry(meter: str = "mono") -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "energyvision",
            "contract": _GS,
            "region": "flanders",
            "dso": _ANTWERPEN,
            "meter": meter,
        },
    )


def _hold_antwerpen_2026() -> None:
    sheet = Path(__file__).parent / "fixtures" / "fluvius_antwerpen_2026.xlsx"
    terms = fluvius.parse_area_terms(sheet.read_bytes(), 2026)
    assert terms is not None
    fluvius._area[(2026, _ANTWERPEN)] = terms


def test_it_is_a_flemish_product_withdrawn_from_october_2025() -> None:
    """Its last issue is September 2025 and October's answers 404, so it is
    offered marked withdrawn, in Flanders, the only region its cards name."""
    contract = next(c for c in EXTRACTORS["energyvision"].contracts if c.id == _GS)
    assert contract.label == "EnergyVision Goedkope stroom"
    assert contract.kind == "spot_monthly"
    assert contract.regions == frozenset({"flanders"})
    assert withdrawn_on("energyvision", _GS) == date(2025, 10, 1)


def test_the_last_card_reads_the_tranche_formula_and_feed_in() -> None:
    """Page 1 prints the tranche, "Groene stroom (<1.000 kWh - vast tarief)
    9,54 €cent/kWh", and footnote b what bills the rest, "1,05 x
    Belpex-RLP-M + 15 EUR/MWh" excluding VAT, and the feed-in, "0,6 x
    Belpex-SPP-M - 15 EUR/MWh", with the 1 €cent/kWh guarantee of condition
    5. The gas half of the card is not read."""
    snap = _card()
    energy = snap.energy
    assert isinstance(energy, SpotMonthlyRates)
    assert energy.tier_kwh == pytest.approx(1000.0)
    assert energy.tier_rate == pytest.approx(0.0954)
    assert energy.factor == pytest.approx(1.05 * 1.06)
    assert energy.base == pytest.approx(0.015 * 1.06)
    assert energy.rlp_indexed is True
    assert energy.rlp_blend == "flanders"
    assert energy.yearly_fixed_fee == pytest.approx(50.0)
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.0175)
    assert inj.factor == pytest.approx(0.6)
    assert inj.base == pytest.approx(-0.015)
    assert inj.spp_indexed is True
    assert inj.minimum == pytest.approx(0.01)
    assert snap.valid_until == date(2025, 9, 30)
    assert snap.publication_label == "september 2025"


def test_the_card_prints_the_eight_merged_areas_and_its_levies() -> None:
    """The September 2025 card prints the eight Fluvius areas of 2025 and
    the tax block of its month, which the law replaces where it has moved."""
    snap = _card()
    assert set(snap.dsos) == set(FLUVIUS_KEYS)
    antwerpen = snap.dsos[_ANTWERPEN]
    assert antwerpen.distribution_single == pytest.approx(0.0599008)
    assert antwerpen.capacity_eur_per_kw_year == pytest.approx(53.2565)
    assert antwerpen.data_management_per_year == pytest.approx(18.56)
    assert snap.taxes.flanders_renewables == pytest.approx(0.01554)
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)


def test_no_welcome_credit_is_read_off_the_last_card() -> None:
    """The card prints "Eenmalige welkomstkorting 125 € (*)", the figure for
    a household paying by direct debit, 105 otherwise. Each issue printed its
    own and none but the last is read, so no credit is granted rather than
    one taken from a card the household did not sign."""
    assert "Eenmalige welkomstkorting 125" in _text()
    assert _card().welcome_credit_eur is None


def test_the_formula_gives_back_the_printed_indicatives() -> None:
    """The card prints 11,59 and 1,75 €cent/kWh, "berekend volgens
    methodologie Vlaamse Nutsregulator". EnergyVision's Indexatieparameters
    (EV-1126-Indexatieparameters-nl.pdf, read 7 November 2026) give that
    method's August 2025 values as 89,891 EUR/MWh for Belpex-RLP-M and
    54,235 for Belpex-SPP-M, the last known when the card came out. At those
    the parsed coefficients give back both figures, which pins the units: a
    dimensionless factor on EUR/kWh, the base EUR/MWh over 1000, 6% VAT on
    the offtake and none on the feed-in."""
    snap = _card()
    energy = snap.energy
    assert isinstance(energy, SpotMonthlyRates)
    offtake = energy.factor * 0.089891 + energy.base
    assert offtake == pytest.approx(0.115949, abs=1e-6)
    assert round(offtake * 100, 2) == 11.59
    inj = snap.injection
    assert inj is not None and inj.factor is not None and inj.base is not None
    feed_in = inj.factor * 0.054235 + inj.base
    assert feed_in == pytest.approx(0.017541, abs=1e-6)
    assert round(feed_in * 100, 2) == 1.75


@pytest.mark.parametrize(
    ("meter", "annual", "factor", "base"),
    [
        # 3.500 kWh: 1.000 of them at 9,54, the other 2.500 on the formula.
        (
            "mono",
            3500.0,
            2500 / 3500 * 1.113,
            2500 / 3500 * 0.0159 + 1000 / 3500 * 0.0954,
        ),
        (
            "bi",
            3500.0,
            2500 / 3500 * 1.113,
            2500 / 3500 * 0.0159 + 1000 / 3500 * 0.0954,
        ),
        # The tranche is not for an exclusive-night register.
        ("exclusive_night", 3500.0, 1.113, 0.0159),
    ],
)
def test_the_tranche_folds_into_the_formula(
    meter: str, annual: float, factor: float, base: float
) -> None:
    """The existing tier fold bills the year's first 1.000 kWh at 9,54 and the
    rest on the formula. A day/night meter is billed the same, which is exact
    while the solar hours' kWh the card gives the tranche to reach 1.000 a
    year, both registers paying one formula; and the card has no
    Voordeelzekerheid, so a month short of its share of the tranche is
    assumed to have used it."""
    folded = resolve_volume_tier(_card(), annual, meter).energy
    assert isinstance(folded, SpotMonthlyRates)
    assert folded.factor == pytest.approx(factor)
    assert folded.base == pytest.approx(base)


def test_a_year_inside_the_tranche_is_billed_at_its_fixed_rate() -> None:
    folded = resolve_volume_tier(_card(), 800.0).energy
    assert isinstance(folded, FixedRates)
    assert folded.single == pytest.approx(0.0954)


def test_the_card_file_is_named_code_first() -> None:
    """The issues are EV-GS-<MMYY>-01, the code first and a version after,
    where every product of 2026 is EV-<MMYY>-<code>-<lang>, and they sit in
    inline-files alone."""
    contract = _CONTRACTS_BY_ID[_GS]
    card = contract.cards["flanders"]
    assert energyvision._archive_card_urls(contract, card, date(2025, 9, 1)) == (
        f"{_FILES}EV-GS-0925-01.pdf",
    )


class _Uploads:
    """A HEAD answering for the three uploads of the September 2025 card, all
    of 1 September 2025, and 404 for any other name; it keeps the names it
    was asked."""

    names = ("EV-GS-0925-01.pdf", "EV-GS-0925-01_0.pdf", "EV-GS-0925-01_1.pdf")

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def __call__(self, _session: Any, url: str, **_kw: Any) -> str:
        name = url.removeprefix(_FILES)
        self.asked.append(name)
        if name not in self.names:
            raise ExtractorError(f"HTTP 404 fetching {url}")
        return "Mon, 01 Sep 2025 06:09:31 GMT"


async def test_every_month_is_read_off_the_last_card(
    monkeypatch: pytest.MonkeyPatch, freezer: Any
) -> None:
    """In November 2026 the fetch reads the newest upload of the September
    2025 card and asks for no card of the month, which never comes; the
    probe keys on it. The month archive reads that card for its own month
    and no other issue: a month without a card of its own is billed on the
    last one standing in."""
    freezer.move_to("2026-11-15 12:00:00+01:00")
    head = _Uploads()
    read: list[str] = []

    async def text(_session: Any, url: str, **_kw: Any) -> str:
        read.append(url)
        return _text()

    monkeypatch.setattr(energyvision, "head_or_raise", head)
    monkeypatch.setattr(energyvision, "fetch_pdf_text_layout", text)
    snap = await energyvision.fetch(None, _GS, "flanders")  # type: ignore[arg-type]
    assert read == [f"{_FILES}EV-GS-0925-01_1.pdf"]
    assert snap.valid_until == date(2025, 9, 30)
    assert all(name.startswith("EV-GS-0925-01") for name in head.asked)
    key = await energyvision.probe(None, _GS, "flanders")  # type: ignore[arg-type]
    assert key == f"{_FILES}EV-GS-0925-01_1.pdf Mon, 01 Sep 2025 06:09:31 GMT"

    head.asked.clear()
    read.clear()
    for month in (date(2025, 3, 1), date(2025, 12, 1), date(2026, 2, 1)):
        assert (
            await energyvision.fetch_for_month(None, _GS, "flanders", month)  # type: ignore[arg-type]
            is None
        )
    assert head.asked == []
    september = await energyvision.fetch_for_month(
        None,  # type: ignore[arg-type]
        _GS,
        "flanders",
        date(2025, 9, 1),
    )
    assert september is not None
    assert read == [f"{_FILES}EV-GS-0925-01_1.pdf"]


def test_a_2026_month_bills_the_2026_fluvius_sheet() -> None:
    """The card prints 2025's network table, 5,99008 €cent/kWh, 53,2565
    EUR/kW and 18,56 EUR a year on Antwerpen. A 2026 month is billed on
    Fluvius's 2026 sheet, the 5,35329, 52,3679 and 18,92 the 2026 cards
    print, and a month of 2025 keeps the card's."""
    _hold_antwerpen_2026()
    snap = _card()
    entry = _entry()
    own = _resolve_snapshot(entry, snap, delivery_month=date(2025, 11, 1))
    assert own.dsos[_ANTWERPEN].distribution_single == pytest.approx(0.0599008)
    later = _resolve_snapshot(entry, snap, delivery_month=date(2026, 3, 1))
    overlay = later.dsos[_ANTWERPEN]
    assert round(overlay.distribution_single * 100, 5) == 5.35329
    assert overlay.capacity_eur_per_kw_year is not None
    assert round(overlay.capacity_eur_per_kw_year, 4) == 52.3679
    assert round(overlay.data_management_per_year, 2) == 18.92


def test_a_walk_bills_2025_on_the_card_and_2026_on_the_sheet(freezer: Any) -> None:
    """Held in November 2026, the card is resolved on the 2026 sheet, and a
    walk standing it in for a 2025 month gives that month the card's own
    table back."""
    freezer.move_to("2026-11-15 12:00:00+01:00")
    _hold_antwerpen_2026()
    raw = _card()
    entry = _entry()
    current = _resolve_snapshot(entry, raw)
    assert round(current.dsos[_ANTWERPEN].distribution_single * 100, 5) == 5.35329
    december = _proxy_for_month(entry, current, raw, date(2025, 12, 1))
    assert december.dsos[_ANTWERPEN].distribution_single == pytest.approx(0.0599008)
    march = _proxy_for_month(entry, current, raw, date(2026, 3, 1))
    assert march.dsos[_ANTWERPEN] == current.dsos[_ANTWERPEN]


@pytest.mark.parametrize(
    ("dates", "floor"),
    [
        # Signed on the March 2025 card, or supplied from February: condition
        # 5 is not on the card the household signed.
        ({"tariff_card_date": "2025-03-15"}, None),
        ({"contract_start_date": "2025-02-01"}, None),
        # The June 2025 card brought it, and the September one keeps it.
        ({"tariff_card_date": "2025-06-01"}, 0.01),
        ({"contract_start_date": "2025-09-10"}, 0.01),
        # A card month typed beside the start date is the one signed.
        ({"tariff_card_date": "2025-01-20", "contract_start_date": "2025-07-01"}, None),
        # Unknown: the terms of the card read, the September one.
        ({}, 0.01),
    ],
)
def test_the_feed_in_floor_follows_the_card_signed(
    dates: dict[str, str], floor: float | None
) -> None:
    """Condition 5, "Bij ons is zonne-energie nooit waardeloos ... dan
    garanderen wij in elk geval 1 €cent/kWh", is on the June and September
    2025 cards only. A household that signed an earlier issue is credited
    the formula with no floor, in every month and on every path, since each
    resolves the card for the entry."""
    entry = MockConfigEntry(domain=DOMAIN, data={**_entry().data, **dates})
    for month in (date(2025, 11, 1), date(2026, 5, 1)):
        inj = _resolve_snapshot(entry, _card(), delivery_month=month).injection
        assert inj is not None
        assert inj.minimum == floor
        # May 2026 at a solar-weighted mean of 30 EUR/MWh: 0,6 x 30 - 15 is
        # 3 EUR/MWh, 0,3 c/kWh, which the floor lifts to 1 c/kWh.
        credit = _floor_injection(0.6 * 0.030 - 0.015, inj)
        assert credit == pytest.approx(0.003 if floor is None else 0.01)


def test_the_floor_is_the_entry_s_own_card_s() -> None:
    """The signing month is a fact about the entry's own contract, so another
    contract's card resolved for it, an earlier contract or a compare quote,
    keeps its own floor."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **_entry().data,
            "contract": "energyvision_tiered_1800",
            "tariff_card_date": "2025-03-15",
        },
    )
    inj = _resolve_snapshot(entry, _card(), delivery_month=date(2025, 11, 1)).injection
    assert inj is not None
    assert inj.minimum == 0.01
