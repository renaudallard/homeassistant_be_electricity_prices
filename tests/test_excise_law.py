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
from custom_components.be_electricity_prices.providers._resolve import (
    resolve_federal_excise,
)
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)
from tests import make_snapshot

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
