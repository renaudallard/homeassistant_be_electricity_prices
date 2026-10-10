"""The federal excise read from the law (``excise_law.py``) and billed by
``resolve_federal_excise``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import excise_law
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.providers import (
    aspiravi,
    bolt,
    dats24,
    ebem,
    ecofix,
    ecopower,
    eneco,
    energiebe,
    energyknights,
    energyvision,
    frank,
    octaplus,
    trevion,
)
from custom_components.be_electricity_prices.providers._resolve import (
    resolve_federal_excise,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import fixture_text, make_snapshot

_PAGE = (
    Path(__file__).resolve().parent / "fixtures" / "justel_loi_programme_2004_fr.html"
)


@pytest.fixture(scope="module")
def page() -> str:
    return _PAGE.read_bytes().decode("iso-8859-1")


def test_the_article_as_published_sets_the_august_2026_rates(page: str) -> None:
    """46 EUR/MWh for a household, with the three January steps the law
    voted, and 1 EUR/MWh for a protected one, all from 2026-08-01, the entry
    into force of the law of 30 May 2026 that wrote the article."""
    table = excise_law.parse(page)
    assert table[excise_law.STANDARD] == (
        (date(2026, 8, 1), pytest.approx(0.046)),
        (date(2027, 1, 1), pytest.approx(0.043)),
        (date(2028, 1, 1), pytest.approx(0.040)),
        (date(2029, 1, 1), pytest.approx(0.038)),
    )
    assert table[excise_law.PROTECTED] == ((date(2026, 8, 1), pytest.approx(0.001)),)


def test_the_rate_is_dated_by_the_bracket_around_it(page: str) -> None:
    """The first step's date is the amendment's entry into force, read off
    the bracket Justel puts around the passage it wrote, not a constant."""
    moved = page.replace("En vigueur : 01-08-2026>'", "En vigueur : 01-09-2026>'")
    assert moved != page
    table = excise_law.parse(moved)
    assert table[excise_law.STANDARD][0][0] == date(2026, 9, 1)
    assert table[excise_law.PROTECTED][0][0] == date(2026, 9, 1)


def test_an_amendment_rewriting_only_the_rate_is_dated_by_its_own_bracket(
    page: str,
) -> None:
    """Justel brackets the passage a law replaced, so a later law that rewrites
    only the rate after "b) autres:" opens its bracket after the label. The
    rate it sets takes effect when that law does, not when the paragraph was
    first written: read at the label, a 2027 rate was billed from August 2026."""
    sup = (
        "<sup><font color=red><a href=#t title='<L 2027-02-10/01, art. 5, 072; "
        "En vigueur : 01-03-2027>'><span style='color:red;'>2</span></a></font></sup>"
    )
    at = page.rindex("b) autres:") + len("b) autres:")
    end = page.index("]<sup>", page.index("38 euros par MWh", at))
    rewritten = (
        "<BR>  - droit d'accise: 0 euro par MWh;"
        "<BR>  - droit d'accise spécial: 44 euros par MWh;"
        "<BR>  - cotisation sur l'énergie: 0 euro par MWh;"
        "<BR>  A partir du 1er janvier 2029:"
        "<BR>  - droit d'accise: 0 euro par MWh;"
        "<BR>  - droit d'accise spécial: 38 euros par MWh;"
        "<BR>  - cotisation sur l'énergie: 0 euro par MWh."
    )
    amended = f"{page[:at]}[{sup}{rewritten}]{sup}{page[end:]}"
    table = excise_law.parse(amended)
    assert table[excise_law.STANDARD] == (
        (date(2027, 3, 1), pytest.approx(0.044)),
        (date(2029, 1, 1), pytest.approx(0.038)),
    )
    # The protected rate, which the amendment left alone, keeps its date.
    assert table[excise_law.PROTECTED][0][0] == date(2026, 8, 1)


def test_a_page_with_no_bracket_around_the_rate_is_refused(page: str) -> None:
    unbracketed = excise_law._OPEN_RE.sub("[", page)
    with pytest.raises(excise_law.ExciseLawError, match="entry into force"):
        excise_law.parse(unbracketed)


def test_a_rate_banded_by_volume_is_refused(page: str) -> None:
    """The household rate was a schedule by annual volume before August
    2026. That is not one rate, so it is not read as one."""
    # The last "b) autres" is the electricity one; the gas one comes first.
    at = page.rindex("b) autres:")
    banded = f"{page[:at]}b) autres: i) pour la tranche de 0 à 3 MWh:{page[at + 10 :]}"
    with pytest.raises(excise_law.ExciseLawError, match="banded"):
        excise_law.parse(banded)


def test_a_page_that_is_not_the_article_is_refused() -> None:
    """What Justel serves GitHub's runners is a bot check, not the law."""
    with pytest.raises(excise_law.ExciseLawError, match="article 419"):
        excise_law.parse("<html><body>Please enable JavaScript</body></html>")


def test_a_month_takes_the_rate_in_force_on_its_first_day() -> None:
    assert excise_law.standard_excise(date(2026, 7, 31)) is None
    assert excise_law.standard_excise(date(2026, 8, 15)) == pytest.approx(0.046)
    assert excise_law.standard_excise(date(2026, 12, 1)) == pytest.approx(0.046)
    assert excise_law.standard_excise(date(2027, 1, 1)) == pytest.approx(0.043)
    assert excise_law.standard_excise(date(2031, 6, 1)) == pytest.approx(0.038)


def test_the_store_round_trips_and_never_replaces_what_is_held() -> None:
    stored = excise_law.held_table()
    excise_law._HELD.clear()
    excise_law.restore(stored)
    assert excise_law.standard_excise(date(2027, 3, 1)) == pytest.approx(0.043)
    # A second entry's older copy does not replace what is held.
    excise_law.restore({excise_law.STANDARD: [["2026-08-01", 0.05]]})
    assert excise_law.standard_excise(date(2026, 9, 1)) == pytest.approx(0.046)
    # A copy that does not read as steps is ignored.
    excise_law._HELD.clear()
    excise_law.restore({excise_law.STANDARD: [["2026-08-01", 5.0]]})
    excise_law.restore({excise_law.PROTECTED: "nonsense"})
    assert excise_law.held_table() == {}


@pytest.fixture
def fresh_fetch() -> Iterator[None]:
    """Forget when the law was last read, so the next ensure asks."""
    excise_law._fetched_at = None
    excise_law._failed_at = None
    yield
    excise_law._fetched_at = None
    excise_law._failed_at = None


async def test_a_failed_read_keeps_what_is_held_and_backs_off(
    fresh_fetch: None, caplog: pytest.LogCaptureFixture
) -> None:
    held = excise_law.held_table()
    fetch = AsyncMock(side_effect=ExtractorError("HTTP 503 fetching the law"))
    with patch.object(excise_law, "fetch_text", fetch):
        await excise_law.ensure_excise_law(AsyncMock())
        await excise_law.ensure_excise_law(AsyncMock())
    assert fetch.await_count == 1
    assert excise_law.held_table() == held
    excise_law._failed_at = dt_util.utcnow() - timedelta(hours=7)
    bot_check = AsyncMock(return_value="<html>challenge</html>")
    with (
        patch.object(excise_law, "fetch_text", bot_check),
        caplog.at_level(logging.WARNING),
    ):
        await excise_law.ensure_excise_law(AsyncMock())
    assert bot_check.await_count == 1
    assert "could not be read" in caplog.text
    assert excise_law.held_table() == held


async def test_an_unread_law_is_a_warning_and_a_held_one_is_not(
    fresh_fetch: None, caplog: pytest.LogCaptureFixture
) -> None:
    """With nothing held every card bills its own excise, which a stale card
    gets wrong, so a host that cannot reach Justel has to be told; with the
    law held from the store it goes on billing the law."""
    down = AsyncMock(side_effect=ExtractorError("HTTP 403 fetching the law"))
    held = excise_law.held_table()
    with patch.object(excise_law, "fetch_text", down), caplog.at_level(logging.DEBUG):
        await excise_law.ensure_excise_law(AsyncMock())
    assert [r.levelno for r in caplog.records if "Justel" in r.message] == [
        logging.DEBUG
    ]
    caplog.clear()
    excise_law._failed_at = None
    excise_law._HELD.clear()
    with patch.object(excise_law, "fetch_text", down), caplog.at_level(logging.DEBUG):
        await excise_law.ensure_excise_law(AsyncMock())
    assert [r.levelno for r in caplog.records if "Justel" in r.message] == [
        logging.WARNING
    ]
    excise_law.hold(held)


async def test_a_read_replaces_the_table(fresh_fetch: None, page: str) -> None:
    excise_law._HELD.clear()
    with patch.object(excise_law, "fetch_text", AsyncMock(return_value=page)):
        await excise_law.ensure_excise_law(AsyncMock())
    assert excise_law.standard_excise(date(2028, 2, 1)) == pytest.approx(0.040)


def _card(excise: float, vat_rate: float = 0.0) -> SupplierSnapshot:
    return make_snapshot(
        taxes=TaxOverlay(
            federal_excise=excise,
            energy_contribution=0.0,
            vat_rate=vat_rate,
            card_vat_rate=0.06 if vat_rate == 0.0 else None,
        )
    )


def test_a_stale_card_is_billed_the_law_and_an_earlier_month_the_card() -> None:
    """Ecofix's September 2026 card prints July's 5,03288 including VAT."""
    stale = _card(0.0503288)
    september = resolve_federal_excise(stale, date(2026, 9, 1), professional=False)
    assert september.taxes.federal_excise == pytest.approx(0.04876)
    july = resolve_federal_excise(stale, date(2026, 7, 1), professional=False)
    assert july is stale
    january = resolve_federal_excise(stale, date(2027, 1, 1), professional=False)
    assert january.taxes.federal_excise == pytest.approx(0.043 * 1.06)


_EK = ((3000.0, 0.050329), (20000.0, 0.050329), (50000.0, 0.048188))
_EV = (
    (3000.0, 0.0503288),
    (20000.0, 0.0503288),
    (50000.0, 0.0481876),
    (1000000.0, 0.0474668),
)
_OCTA = (
    (3000.0, 0.050329),
    (20000.0, 0.050329),
    (50000.0, 0.048188),
    (1000000.0, 0.047467),
)


@pytest.mark.parametrize(
    ("parse", "bands"),
    [
        pytest.param(
            lambda: energyknights.parse_snapshot(
                "energyknights_agilior",
                fixture_text("energyknights_agilior_may.pdf", layout=True),
                "t://",
            ),
            _EK,
            id="energy knights may",
        ),
        pytest.param(
            lambda: aspiravi.parse_snapshot(
                "aspiravi_eco_plus_flex",
                fixture_text("aspiravi_eco_plus_flex_2026-03.pdf"),
            ),
            ((20000.0, 0.0503288), (50000.0, 0.0481876), (100000.0, 0.0474668)),
            id="aspiravi march",
        ),
        pytest.param(
            lambda: energyvision.parse_snapshot(
                "energyvision_dynamic",
                fixture_text("energyvision_dynamic_jul.pdf", layout=True),
                "t://",
            ),
            _EV,
            id="energyvision flanders july",
        ),
        pytest.param(
            lambda: energyvision.parse_snapshot(
                "energyvision_fixed_1y",
                fixture_text("energyvision_fixed_1y_wal_jul.pdf", layout=True),
                "t://",
            ),
            _EV,
            id="energyvision wallonia july",
        ),
        pytest.param(
            lambda: energyvision.parse_snapshot(
                "energyvision_groene_stroom",
                fixture_text("energyvision_groene_stroom_bxl_jan.pdf", layout=True),
                "t://",
                region="brussels",
            ),
            _EV,
            id="energyvision brussels january",
        ),
        pytest.param(
            lambda: octaplus.parse_snapshot(
                "octaplus_dynamic",
                fixture_text("octaplus_dynamic_v_jan.pdf", aligned=True),
                "flanders",
            ),
            _OCTA,
            id="octa+ flanders january",
        ),
        pytest.param(
            lambda: octaplus.parse_snapshot(
                "octaplus_dynamic",
                fixture_text("octaplus_dynamic_w.pdf", aligned=True),
                "wallonia",
            ),
            _OCTA,
            id="octa+ wallonia",
        ),
        pytest.param(
            lambda: ecofix.parse_snapshot(
                "ecofix_flexy",
                fixture_text("ecofix_flexy.pdf", layout=True),
                "flanders",
            ),
            _EV,
            id="ecofix",
        ),
        pytest.param(
            lambda: dats24.parse_snapshot(
                fixture_text("dats24_groen_variabel_apr.pdf", layout=True),
                "t://",
                "flanders",
            ),
            _EV,
            id="dats 24 april",
        ),
        pytest.param(
            lambda: trevion.parse_snapshot(
                "groene_energie_vast",
                fixture_text("trevion_vast_2026-04.pdf", layout=True),
            ),
            _EV,
            id="trevion april",
        ),
        pytest.param(
            lambda: eneco.parse_snapshot(
                fixture_text("eneco_flex_dec25.pdf"), "power_flex", "t://", "flanders"
            ),
            _OCTA,
            id="eneco december 2025",
        ),
        pytest.param(
            lambda: ebem.parse_snapshot(
                "ebem_variable", fixture_text("ebem_variable_2026-05.pdf", layout=True)
            ),
            # EBEM prints 4,7569 above 50 MWh where the others print 4,7467.
            (*_OCTA[:3], (1000000.0, 0.047569)),
            id="ebem may",
        ),
        pytest.param(
            lambda: ecopower.parse_snapshot(
                fixture_text("ecopower_burgerstroom_jul.pdf", layout=True),
                "t://",
                "2026-07",
            ),
            # In euro and excluding VAT, like the excise beside it.
            (
                (3000.0, 0.04748),
                (20000.0, 0.04748),
                (50000.0, 0.04546),
                (1000000.0, 0.04478),
            ),
            id="ecopower july",
        ),
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_variable",
                fixture_text("bolt_variable.pdf", layout=True),
                "wallonia",
            ),
            _EK,
            id="bolt footnote wrapped at its thousands space",
        ),
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_fix",
                fixture_text("bolt_fix_jan_legacy.pdf", layout=True),
                "brussels",
            ),
            _EK,
            id="bolt footnote wrapped after a colon",
        ),
        # The professional card prints the household footnote beside its own
        # 1,4210, which is not its first tier.
        pytest.param(
            lambda: bolt.parse_snapshot(
                "bolt_pro_fix",
                fixture_text("bolt_pro_fix.pdf", layout=True),
                "wallonia",
            ),
            None,
            id="bolt professional",
        ),
        # From August 2026 the table is gone or carries one rate.
        pytest.param(
            lambda: eneco.parse_snapshot(
                fixture_text("eneco_flex_aug26.pdf"), "power_flex", "t://", "flanders"
            ),
            None,
            id="eneco august",
        ),
        pytest.param(
            lambda: energyknights.parse_snapshot(
                "energyknights_agilior",
                fixture_text("energyknights_agilior_aug.pdf", layout=True),
                "t://",
            ),
            None,
            id="energy knights august",
        ),
        pytest.param(
            lambda: energyvision.parse_snapshot(
                "energyvision_fixed_1y",
                fixture_text("energyvision_fixed_1y_wal_aug.pdf", layout=True),
                "t://",
            ),
            None,
            id="energyvision wallonia august",
        ),
        pytest.param(
            lambda: energyvision.parse_snapshot(
                "energyvision_groene_stroom",
                fixture_text("energyvision_groene_stroom_bxl_sep.pdf", layout=True),
                "t://",
                region="brussels",
            ),
            None,
            id="energyvision brussels september",
        ),
        pytest.param(
            lambda: octaplus.parse_snapshot(
                "octaplus_fixed",
                fixture_text("octaplus_fixed_w_aug.pdf", aligned=True),
                "wallonia",
            ),
            None,
            id="octa+ wallonia august",
        ),
        # A card that says the excise is degressive and prints no tier rates.
        pytest.param(
            lambda: energiebe.parse_snapshot(
                fixture_text("energiebe_dynamic_jul.pdf", layout=True), "t://"
            ),
            None,
            id="energie.be july",
        ),
        pytest.param(
            lambda: frank.parse_snapshot(
                fixture_text("frank_dynamic_apr.pdf", layout=True),
                "t://",
                "frank_dynamic",
            ),
            None,
            id="frank april",
        ),
    ],
)
def test_a_card_printing_the_degressive_table_is_read_whole(
    parse: Any, bands: tuple[tuple[float, float], ...] | None
) -> None:
    """Until July 2026 the household excise fell by annual volume, and these
    cards print the whole table where only its first row was read: a household
    above 20.000 kWh was billed that row's rate on every kWh. The first row
    stays the excise the card states; from August the table is gone or flat."""
    taxes = parse().taxes
    if bands is None:
        assert taxes.federal_excise_bands is None
        return
    assert taxes.federal_excise == pytest.approx(bands[0][1])
    got = taxes.federal_excise_bands
    assert got is not None
    assert [upper for upper, _rate in got] == [upper for upper, _rate in bands]
    assert [rate for _upper, rate in got] == pytest.approx(
        [rate for _upper, rate in bands]
    )


def test_a_stale_table_gives_way_to_the_law() -> None:
    """Aspiravi's September 2026 card still prints July's table. September is
    billed the law's one rate and the table goes; July keeps it."""
    card = aspiravi.parse_snapshot(
        "aspiravi_eco_plus_flex", fixture_text("aspiravi_eco_plus_flex_2026-09.pdf")
    )
    assert card.taxes.federal_excise_bands is not None
    september = resolve_federal_excise(card, date(2026, 9, 1), professional=False)
    assert september.taxes.federal_excise == pytest.approx(0.04876)
    assert september.taxes.federal_excise_bands is None
    assert resolve_federal_excise(card, date(2026, 7, 1), professional=False) is card


def test_an_ex_vat_card_takes_the_law_as_it_is() -> None:
    card = _card(0.0475, vat_rate=0.06)
    got = resolve_federal_excise(card, date(2026, 10, 1), professional=False)
    assert got.taxes.federal_excise == pytest.approx(0.046)


def test_without_the_law_the_card_is_billed_as_printed() -> None:
    excise_law._HELD.clear()
    stale = _card(0.0503288)
    assert resolve_federal_excise(stale, date(2026, 9, 1), professional=False) is stale


def test_a_professional_card_keeps_its_own_excise() -> None:
    card = _card(0.01421, vat_rate=0.21)
    assert resolve_federal_excise(card, date(2026, 9, 1), professional=True) is card


# The coordinator.


async def test_a_card_resolved_before_the_law_is_read_is_resolved_again(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The first start after an upgrade has no law in any store, so the
    stored card is resolved on its own excise; the tick reads the law after,
    and the card has to follow without waiting for the next one."""
    freezer.move_to("2026-11-02 08:00:00+01:00")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "ecofix",
            "contract": "ecofix_flexy",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    held = excise_law.held_table()
    excise_law._HELD.clear()
    coord._set_snapshot(_card(0.0503288))
    assert coord._snapshot is not None
    assert coord._snapshot.taxes.federal_excise == pytest.approx(0.0503288)
    excise_law.hold(held)
    coord._reresolve_snapshot()
    assert coord._snapshot.taxes.federal_excise == pytest.approx(0.04876)


async def test_the_law_travels_through_the_store(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"supplier": "eneco", "contract": "power_fix", "region": "wallonia"},
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    saved: list[dict[str, Any]] = []

    async def save(payload: dict[str, Any]) -> None:
        saved.append(payload)

    with patch.object(coord._store, "async_save", save):
        await coord._save_persistent()
    assert saved[-1]["excise_law"][excise_law.STANDARD][0] == ["2026-08-01", 0.046]
    excise_law._HELD.clear()
    coord._store.async_load = AsyncMock(return_value=saved[-1])  # type: ignore[method-assign]
    await coord.async_load_persistent()
    assert excise_law.standard_excise(date(2027, 2, 1)) == pytest.approx(0.043)
