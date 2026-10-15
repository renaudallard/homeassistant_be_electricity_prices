"""The federal excise read from the law (``excise_law.py``) and billed by
``resolve_federal_excise``."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import excise_law
from custom_components.be_electricity_prices.const import (
    DOMAIN,
    EXCISE_LAW_ARCHIVE_URL,
    EXCISE_LAW_URL,
)
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
from custom_components.be_electricity_prices.providers import get as get_extractor
from custom_components.be_electricity_prices.providers._resolve import (
    apply_vat,
    resolve_federal_excise,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from custom_components.be_electricity_prices.snapshot_months import (
    _snapshot_for_month,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _resolve_snapshot,
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


async def test_an_impossible_date_backs_off_like_any_unreadable_page(
    fresh_fetch: None, page: str
) -> None:
    """A step dated 31 April raised a bare ValueError out of date(), which
    is neither of the errors ensure_excise_law catches: it escaped into the
    coordinator tick, and with no backoff recorded it did so on every one."""
    assert "1er janvier 2028" in page
    bad = page.replace("1er janvier 2028", "31 avril 2028")
    with pytest.raises(excise_law.ExciseLawError):
        excise_law.parse(bad)
    fetch = AsyncMock(return_value=bad)
    with patch.object(excise_law, "fetch_text", fetch):
        await excise_law.ensure_excise_law(AsyncMock())
        await excise_law.ensure_excise_law(AsyncMock())
    assert fetch.await_count == 1
    assert excise_law._failed_at is not None


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
            # Its aligned text mangles the unit on the second and third rows,
            # "3.000 & 20.000 kWhW 5,0329" and "20.000 & 50.000 k h 4,8188".
            lambda: octaplus.parse_snapshot(
                "octaplus_ecoflux",
                fixture_text("octaplus_ecoflux_w_mar.pdf", aligned=True),
                "wallonia",
            ),
            _OCTA,
            id="octa+ ecoflux march",
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


def test_an_ex_vat_card_billed_without_vat_takes_the_law_as_it_is() -> None:
    """apply_vat runs first and zeroes vat_rate for an entry that deducts
    VAT. The card still printed ex-VAT, so the law's rate is not grossed."""
    card = apply_vat(_card(0.0475, vat_rate=0.06), include_vat=False)
    assert card.taxes.vat_rate == 0.0
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


# The household wording before the one in force, from Justel's archive.

_ARCHIVE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "justel_loi_programme_2004_fr_arch070.html"
)


# The law of 19 March 2023 that wrote it, as its footnote links it.
_AMENDING = (
    Path(__file__).resolve().parent / "fixtures" / "justel_loi_2023_03_19_fr.html"
)


@pytest.fixture(scope="module")
def archive() -> str:
    return _ARCHIVE.read_bytes().decode("iso-8859-1")


@pytest.fixture(scope="module")
def amending() -> str:
    return _AMENDING.read_bytes().decode("iso-8859-1")


def _schedule(archive: str, amending: str) -> excise_law.Schedule:
    return excise_law.after_transitions(*excise_law.parse_archive(archive), amending)


@pytest.fixture
def schedule_held(archive: str, amending: str) -> Iterator[None]:
    excise_law._SCHEDULES[excise_law.SCHEDULE] = _schedule(archive, amending)
    yield
    excise_law._SCHEDULES.clear()


def test_the_archived_article_sets_the_schedule_before_august_2026(
    archive: str,
) -> None:
    """Article 419 as it stood before the law of 30 May 2026: the household
    excise by annual volume and the energy contribution beside it, from the
    entry into force of the law of 19 March 2023 that wrote them (footnote
    32 dates its bracket and links the law) to the day the archive's note
    says it ended."""
    schedule, dossier = excise_law.parse_archive(archive)
    assert (schedule.start, schedule.until) == (date(2023, 4, 1), date(2026, 8, 1))
    assert dossier == "2023031902"
    assert excise_law.law_url(dossier) == (
        "https://www.ejustice.just.fgov.be/cgi_loi/article.pl"
        "?language=fr&lg_txt=F&cn_search=2023031902"
    )
    assert [upper for upper, _rate in schedule.bands] == [
        3000.0,
        20000.0,
        50000.0,
        1000000.0,
        25000000.0,
        25000000.0,
    ]
    assert [rate for _upper, rate in schedule.bands] == pytest.approx(
        [0.04748, 0.04748, 0.04546, 0.04478, 0.04411, 0.03628]
    )
    assert schedule.contribution == pytest.approx(0.0019261)


def test_a_page_that_is_not_the_archived_article_is_refused(
    page: str, archive: str
) -> None:
    # The wording in force has no volume bands, and no note saying it ended.
    with pytest.raises(excise_law.ExciseLawError, match="note"):
        excise_law.parse_archive(page)
    at = archive.rindex("cotisation sur l'énergie: 1,9261")
    banded = f"{archive[:at]}cotisation sur l'énergie: 1,9262{archive[at + 32 :]}"
    with pytest.raises(excise_law.ExciseLawError, match="banded"):
        excise_law.parse_archive(banded)
    ended = archive.replace("01-08-2026", "01-02-2023", 1)
    with pytest.raises(excise_law.ExciseLawError, match="no day"):
        excise_law.parse_archive(ended)
    # Nor is the archived wording read as the one in force.
    with pytest.raises(excise_law.ExciseLawError, match="banded"):
        excise_law.parse(archive)
    # A bracket no footnote links to its law cannot be checked for the
    # rates that law fixed in its own articles.
    unlinked = archive.replace("cn_search=2023031902", "")
    with pytest.raises(excise_law.ExciseLawError, match="no link"):
        excise_law.parse_archive(unlinked)


def test_the_schedule_starts_after_the_transition_its_law_fixed(
    archive: str, amending: str, page: str
) -> None:
    """The law of 19 March 2023 billed the first band 42,5755 EUR/MWh from
    April to June 2023 in its own article 15, which the archived wording does
    not show, so the schedule is billed from July (the cards of April to June
    2023 print 4,5130 c/kWh on 0 to 3 MWh, those from July 5,0329)."""
    schedule, dossier = excise_law.parse_archive(archive)
    held = excise_law.after_transitions(schedule, dossier, amending)
    assert held == replace(schedule, start=date(2023, 7, 1))
    # Neither the archive nor the law in force is the law that wrote it.
    for other in (archive, page):
        with pytest.raises(excise_law.ExciseLawError, match="not the law"):
            excise_law.after_transitions(schedule, dossier, other)
    impossible = amending.replace("30 juin 2023 inclus", "31 juin 2023 inclus")
    with pytest.raises(excise_law.ExciseLawError, match="unreadable"):
        excise_law.after_transitions(schedule, dossier, impossible)
    # Without its article 15, or with it reworded, the schedule would be
    # billed from April 2023 over the rate that article fixed.
    for unread in _without_transition(amending):
        with pytest.raises(excise_law.ExciseLawError, match="no household"):
            excise_law.after_transitions(schedule, dossier, unread)


def _without_transition(amending: str) -> tuple[str, str]:
    """The law of 19 March 2023 without its article 15, and with its
    transitions reworded ("periode" unaccented)."""
    at = amending.index("42,5755")
    cut = (
        amending[: amending.index("<A NAME='Art.15'")]
        + amending[amending.index("Titre 4", at) :]
    )
    return cut, amending.replace("pour la période du", "pour la periode du")


@pytest.mark.usefixtures("schedule_held")
def test_a_month_takes_the_schedule_only_inside_its_days() -> None:
    assert excise_law.household_schedule(date(2023, 3, 1)) is None
    assert excise_law.household_schedule(date(2023, 6, 30)) is None
    assert excise_law.household_schedule(date(2023, 7, 1)) is not None
    assert excise_law.household_schedule(date(2026, 7, 31)) is not None
    assert excise_law.household_schedule(date(2026, 8, 1)) is None


@pytest.mark.usefixtures("schedule_held")
def test_the_store_round_trips_the_schedule() -> None:
    stored = excise_law.held_table()
    held = excise_law._SCHEDULES[excise_law.SCHEDULE]
    excise_law._SCHEDULES.clear()
    excise_law.restore(stored)
    assert excise_law._SCHEDULES[excise_law.SCHEDULE] == held
    excise_law.restore({excise_law.SCHEDULE: {**stored[excise_law.SCHEDULE]}})
    assert excise_law._SCHEDULES[excise_law.SCHEDULE] == held
    excise_law._SCHEDULES.clear()
    excise_law.restore({excise_law.SCHEDULE: {"start": "2023-07-01"}})
    excise_law.restore({excise_law.SCHEDULE: "nonsense"})
    assert excise_law.household_schedule(date(2026, 3, 1)) is None


async def test_the_archived_wording_is_read_once(
    fresh_fetch: None, page: str, archive: str, amending: str
) -> None:
    """It never changes, nor does the law that wrote it, so both are asked
    for with the law until the schedule is held."""
    law = excise_law.law_url("2023031902")
    pages = {EXCISE_LAW_URL: page, EXCISE_LAW_ARCHIVE_URL: archive, law: amending}

    async def fetch(_session: Any, url: str, **_kwargs: Any) -> str:
        return pages[url]

    got = AsyncMock(side_effect=fetch)
    try:
        with patch.object(excise_law, "fetch_text", got):
            await excise_law.ensure_excise_law(AsyncMock())
            excise_law._fetched_at = None
            await excise_law.ensure_excise_law(AsyncMock())
        assert [call.args[1] for call in got.await_args_list] == [
            EXCISE_LAW_URL,
            EXCISE_LAW_ARCHIVE_URL,
            law,
            EXCISE_LAW_URL,
        ]
        assert excise_law.household_schedule(date(2023, 6, 1)) is None
        assert excise_law.household_schedule(date(2026, 3, 1)) is not None
    finally:
        excise_law._SCHEDULES.clear()


@pytest.mark.parametrize("reworded", [False, True])
async def test_a_schedule_whose_law_cannot_be_read_is_not_held(
    fresh_fetch: None,
    page: str,
    archive: str,
    amending: str,
    reworded: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Held without its law, or without the transition that law fixed,
    April to June 2023 would bill the schedule over that rate, so the
    card's levies are billed until it is read."""

    async def fetch(_session: Any, url: str, **_kwargs: Any) -> str:
        if url == EXCISE_LAW_URL:
            return page
        if url == EXCISE_LAW_ARCHIVE_URL:
            return archive
        if reworded:
            return _without_transition(amending)[1]
        raise ExtractorError("HTTP 503")

    with patch.object(excise_law, "fetch_text", AsyncMock(side_effect=fetch)):
        await excise_law.ensure_excise_law(AsyncMock())
    assert excise_law.SCHEDULE not in excise_law._SCHEDULES
    assert "billed the levies it prints" in caplog.text


@pytest.mark.parametrize("annual_kwh", [3500.0, 30000.0])
async def test_a_card_standing_in_bills_the_levies_the_law_then_set(
    hass: HomeAssistant, archive: str, amending: str, annual_kwh: float
) -> None:
    """TotalEnergies keeps no archive and the project's starts in August
    2026, so its January to July months bill on October's card, which prints
    0,0488 and a stale 0,002. Those months owed the household schedule and
    the energy contribution the law then set; the month's own card, where
    there is one, is billed as printed, and so is a month of April to June
    2023, when the law that wrote the schedule billed the first band
    another rate."""
    raw = make_snapshot(
        supplier="totalenergies",
        contract="totalenergies_myessential",
        taxes=TaxOverlay(
            federal_excise=0.0488,
            energy_contribution=0.002,
            vat_rate=0.0,
            card_vat_rate=0.06,
        ),
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "totalenergies",
            "contract": "totalenergies_myessential",
            "region": "wallonia",
            "dso": "ores",
            "meter": "mono",
            "annual_consumption_kwh": annual_kwh,
        },
    )
    march = date(2026, 3, 1)
    current = _resolve_snapshot(entry, raw)

    async def stand_in(month: date = march) -> TaxOverlay:
        got = await _snapshot_for_month(
            hass,
            None,  # type: ignore[arg-type]
            get_extractor("totalenergies"),
            "totalenergies_myessential",
            "wallonia",
            month,
            current,
            entry,
            current_raw=raw,
        )
        return got.taxes

    before = await stand_in()
    assert (before.federal_excise, before.energy_contribution) == (0.0488, 0.002)
    excise_law._SCHEDULES[excise_law.SCHEDULE] = _schedule(archive, amending)
    try:
        taxes = await stand_in()
        transition = await stand_in(date(2023, 5, 1))
        own = _resolve_snapshot(entry, raw, delivery_month=march).taxes
    finally:
        excise_law._SCHEDULES.clear()
    first = min(annual_kwh, 20000.0) * 0.04748
    second = max(annual_kwh - 20000.0, 0.0) * 0.04546
    assert taxes.federal_excise == pytest.approx((first + second) / annual_kwh * 1.06)
    assert taxes.energy_contribution == pytest.approx(0.0019261 * 1.06)
    assert (own.federal_excise, own.energy_contribution) == (0.0488, 0.002)
    assert (transition.federal_excise, transition.energy_contribution) == (
        0.0488,
        0.002,
    )
