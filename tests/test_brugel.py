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

"""Brugel's published Sibelga power term, and the card it completes."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import brugel, snapshot_resolve
from custom_components.be_electricity_prices.const import DSO_SIBELGA
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
)
from custom_components.be_electricity_prices.providers._resolve import (
    resolve_brussels_power_term,
)
from tests import make_snapshot

# The two rows as the 2026 sheet prints them, under "1.2. Sans mesure de
# pointe", which is the block a residential connection is billed on. The dashes
# are the MT columns a household has none of, and the figure repeats once per
# BT column.
_SHEET = (
    "Grille tarifaire - Electricité\n"
    "Distribution Électricité Année 2026\n"
    "prix hors TVA\n"
    "1.2. Sans mesure de pointe (**)\n"
    "Puissance mise à disposition inférieure ou égale à 13 kVA EUR / an (°)"
    " - - - 47,24 47,24\n"
    "EUR / jour - - - 0,1294270 0,1294270\n"
    "Puissance mise à disposition supérieure à 13 kVA EUR / an (°)"
    " - - - 94,48 94,48\n"
    "EUR / jour - - - 0,2588540 0,2588540\n"
)


@pytest.fixture(autouse=True)
def _clear_brugel_cache() -> Iterator[None]:
    """The module caches per year for the life of the process.

    ``_locks`` belongs in this list as much as the two caches do, and more
    dangerously: an uncontended ``asyncio.Lock`` never binds an event loop, so
    one left behind is invisible until a test actually contends it, and the
    next test in the file then dies with "bound to a different event loop".
    The lock shipped without a test for exactly that reason, and the test it
    needs is the one that trips over it.
    """
    brugel._cache.clear()
    brugel._failed_at.clear()
    brugel._locks.clear()
    yield
    brugel._cache.clear()
    brugel._failed_at.clear()
    brugel._locks.clear()


def test_the_power_term_is_read_off_the_published_sheet() -> None:
    """47,24 and 94,48 EUR/year, excluding VAT, for 2026."""
    assert brugel._parse(_SHEET) == (pytest.approx(47.24), pytest.approx(94.48))


def test_the_per_day_figure_beside_it_is_not_mistaken_for_the_term() -> None:
    """Each row is followed by the same charge per DAY, three orders of
    magnitude smaller. Anchoring on the label alone and taking the first
    number on the block would read 0,1294270 as an annual term."""
    assert brugel._parse(_SHEET) != (
        pytest.approx(0.1294270),
        pytest.approx(0.2588540),
    )
    # Against the real sheet the anchors do the work, so the SIZE bound was
    # only ever detectable together with a broken anchor: `None` satisfies
    # the assertion above too. Drive _parse on rows that match and carry the
    # wrong magnitude, which is the bound on its own.
    assert brugel._parse(_row_sheet("0,1294270", "0,2588540")) is None
    assert brugel._parse(_row_sheet("472,40", "944,80")) is None
    assert brugel._parse(_row_sheet("47,24", "94,48")) == (
        pytest.approx(47.24),
        pytest.approx(94.48),
    )


def _row_sheet(low: str, high: str) -> str:
    """The two rows the parser anchors on, carrying whatever figures a test
    wants to put past the anchors."""
    return (
        f"Puissance mise à disposition inférieure ou égale à 13 kVA {low}\n"
        f"Puissance mise à disposition supérieure à 13 kVA {high}\n"
    )


def test_a_sheet_that_cannot_be_read_yields_nothing() -> None:
    """Never a guess: the caller then bills what it billed before."""
    assert brugel._parse("no tariff table here") is None
    assert brugel._parse("Puissance mise à disposition inférieure") is None
    # A pair the wrong way round is two columns read out of order.
    assert (
        brugel._parse(
            "Puissance mise à disposition inférieure ou égale à 13 kVA 94,48\n"
            "Puissance mise à disposition supérieure à 13 kVA 47,24\n"
        )
        is None
    )


def _brussels_card(*, fixed_term: float, vat_rate: float, above: float | None = None):
    return make_snapshot(
        dsos={
            DSO_SIBELGA: DsoOverlay(
                distribution_single=0.0996,
                transport=0.0227,
                data_management_per_year=fixed_term,
                brussels_power_term_above_13kva=above,
            )
        },
        taxes=TaxOverlay(
            federal_excise=0.05, energy_contribution=0.0, vat_rate=vat_rate
        ),
    )


def test_a_card_printing_only_the_metering_half_is_completed() -> None:
    """Bolt prints 14,73 where Engie, Mega, TotalEnergies and EnergyVision
    print the 64,80 sum and a 114,88 band above 13 kVA. The household pays
    Sibelga either way, so the entry was about 50 EUR a year short."""
    out = resolve_brussels_power_term(
        _brussels_card(fixed_term=14.73, vat_rate=0.0), terms=(47.24, 94.48)
    )
    overlay = out.dsos[DSO_SIBELGA]
    # The published figures are ex-VAT and a residential card is VAT-inclusive.
    assert overlay.data_management_per_year == pytest.approx(64.80, abs=0.01)
    assert overlay.brussels_power_term_above_13kva == pytest.approx(114.88, abs=0.01)


def test_a_professional_card_takes_the_figures_on_its_own_basis() -> None:
    """A professional card prints excluding VAT, which is what Brugel
    publishes, so nothing is grossed: 13,90 + 47,24 is the 61,14 its peers
    print."""
    out = resolve_brussels_power_term(
        _brussels_card(fixed_term=13.90, vat_rate=0.21), terms=(47.24, 94.48)
    )
    overlay = out.dsos[DSO_SIBELGA]
    assert overlay.data_management_per_year == pytest.approx(61.14, abs=0.01)
    assert overlay.brussels_power_term_above_13kva == pytest.approx(108.38, abs=0.01)


def test_a_card_that_already_carries_the_term_is_left_alone() -> None:
    """Both signals have to agree, so a complete card is untouched and the
    workaround retires itself if Bolt completes its row."""
    complete = _brussels_card(fixed_term=64.80, vat_rate=0.0, above=114.88)
    assert resolve_brussels_power_term(complete, terms=(47.24, 94.48)) is complete

    # No band above 13 kVA, but a fixed term already larger than the power
    # part alone: it cannot be the metering half by itself.
    summed = _brussels_card(fixed_term=64.80, vat_rate=0.0)
    out = resolve_brussels_power_term(summed, terms=(47.24, 94.48))
    assert out.dsos[DSO_SIBELGA].data_management_per_year == pytest.approx(64.80)


def test_without_the_sheet_the_card_bills_exactly_as_before() -> None:
    """Before the sheet is fetched, and if Brugel cannot be read at all."""
    card = _brussels_card(fixed_term=14.73, vat_rate=0.0)
    assert resolve_brussels_power_term(card, terms=None) is card
    assert brugel.cached_power_term(2026) is None


class _Response:
    """Whatever Brugel answered, as an async context manager."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    async def read(self) -> bytes:
        return self._body

    async def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.calls = 0

    def get(self, *_a: object, **_k: object) -> _Response:
        self.calls += 1
        return _Response(self._body, self._status)


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("a maintenance page answered with 200", b"<html>maintenance</html>"),
        ("a truncated download", b"%PDF-1.7 truncated"),
        ("an empty body", b""),
    ],
)
async def test_a_body_that_is_not_a_sheet_never_raises(label: str, body: bytes) -> None:
    """The docstring promises this never raises, and it has to be true.

    The reader raises ExtractorError for a body that is not a PDF, and
    ExtractorError is not a ValueError: catching a tuple of the usual network
    errors let it escape ``ensure_power_term`` into the coordinator tick,
    whose only handler is for ``UpdateFailed``. One maintenance page from
    Brugel therefore took every entity on the device unavailable, and on a
    first refresh it became ConfigEntryNotReady and the entry never set up.
    """
    session = _Session(body)
    assert await brugel.ensure_power_term(session, 2026) is None, label  # type: ignore[arg-type]
    assert brugel.cached_power_term(2026) is None


async def test_a_failure_is_only_attempted_once() -> None:
    """The backoff used to sit below the raise, so it was never recorded and
    the next tick tried again: a blocked Brugel cost a download an hour
    forever."""
    session = _Session(b"<html>nope</html>")
    await brugel.ensure_power_term(session, 2026)  # type: ignore[arg-type]
    assert 2026 in brugel._failed_at
    after_first = session.calls
    assert after_first > 0

    await brugel.ensure_power_term(session, 2026)  # type: ignore[arg-type]
    assert session.calls == after_first, "the backoff did not hold"


async def test_a_good_sheet_is_fetched_once_and_kept() -> None:
    """A success is cached for the life of the process: the figure is annual."""
    import pathlib as _pathlib

    sheet = _pathlib.Path("tmp/audit_2026_09_19/Z/brugel_2026.pdf")
    if not sheet.exists():
        pytest.skip("the archived Brugel sheet is not on this machine")
    session = _Session(sheet.read_bytes())
    assert await brugel.ensure_power_term(session, 2026) == (  # type: ignore[arg-type]
        pytest.approx(47.24),
        pytest.approx(94.48),
    )
    after_first = session.calls
    assert await brugel.ensure_power_term(session, 2026) == (  # type: ignore[arg-type]
        pytest.approx(47.24),
        pytest.approx(94.48),
    )
    assert session.calls == after_first, "a cached year was fetched twice"


def test_the_sheet_is_fetched_before_the_card_is_resolved() -> None:
    """The resolver reads the cache synchronously and cannot await.

    ``_resolve_snapshot`` calls ``cached_power_term``, so the term has to
    already be there when ``_set_snapshot`` runs. Fetching afterwards left
    the card resolved without it for the whole tick that fetched it, 50,07
    EUR a year short on a Brussels Bolt entry, and the tick disagreed with
    the one after it, which is the shape of a bug nobody can reproduce.

    Held on the source rather than by driving a tick: the defect is an
    ordering one and nothing but the order can express it.
    """
    import inspect

    from custom_components.be_electricity_prices.coordinator import BePricesCoordinator

    body = inspect.getsource(BePricesCoordinator._update_body)
    fetch = body.index("ensure_power_term(")
    resolve = body.index("_maybe_refresh_snapshot()")
    assert fetch < resolve, (
        "ensure_power_term must run before the snapshot is resolved, "
        "or the tick that fetched the card bills without the term"
    )
    # And the resolver really does read it synchronously, which is why.

    assert "cached_power_term(" in inspect.getsource(snapshot_resolve._resolve_snapshot)


async def test_one_request_per_year_and_no_search_for_the_link() -> None:
    """Brugel's theme page carries no PDF href, so searching it never worked.

    The fallback fetched the page on every cold start, matched nothing (it is
    rendered client side, and serves no link to curl or to a browser user
    agent either), and charged 71 KB to the one request the setup budget can
    least afford. One address, one request.
    """
    import pathlib as _pathlib

    sheet = _pathlib.Path("tmp/audit_2026_09_19/Z/brugel_2026.pdf")
    if not sheet.exists():
        pytest.skip("the archived Brugel sheet is not on this machine")
    session = _Session(sheet.read_bytes())
    assert await brugel.ensure_power_term(session, 2026) is not None  # type: ignore[arg-type]
    assert session.calls == 1, f"{session.calls} requests for one sheet"

    # And a miss costs one request too, not one plus a search.
    missing = _Session(b"", status=404)
    assert await brugel.ensure_power_term(missing, 2031) is None  # type: ignore[arg-type]
    assert missing.calls == 1, f"{missing.calls} requests for a sheet that is not there"


async def test_a_backfill_fetches_the_term_for_every_year_it_prices() -> None:
    """``_resolve_snapshot`` asks the cache for the DELIVERY month's year.

    The coordinator tick only ever fetches the current one, so a run
    rebuilding rows in a finished year found nothing cached and priced those
    months without Sibelga's power term, while the live sensor beside them
    carried it. Sized by the window, so an ordinary year-to-date run still
    asks for one year.
    """
    import inspect

    from custom_components.be_electricity_prices import backfill

    source = inspect.getsource(backfill._build_context)
    assert "ensure_power_term(" in source, (
        "the backfill never fetches the term, so a past-year window prices "
        "Brussels rows without it"
    )
    # Every year the hours span, not today's.
    assert "for hour in hours" in source
    assert "REGION_BRUSSELS" in source, "fetched for every region, not just Brussels"


def test_both_signals_have_to_agree_before_a_card_is_touched() -> None:
    """A card is completed only when it carries NEITHER half of the term.

    ``docs/providers/bolt.md`` says two signals have to agree, and they catch
    different cards: the band above 13 kVA says the card already prints the
    power part, and the size of the metering figure says the same thing for a
    card printing one combined number. Only Bolt's card fails both.

    The size guard was doing all the work on the archive, so the band signal
    could be removed with every test still green. This drives the one case
    that separates them: a band present beside a metering figure small enough
    for the size guard to wave through.
    """
    terms = (47.24, 94.48)
    banded = _brussels_card(fixed_term=14.73, vat_rate=0.0, above=112.04)
    assert resolve_brussels_power_term(banded, terms=terms) is banded

    # The same small figure with no band is Bolt's card, and is completed.
    bolt = _brussels_card(fixed_term=14.73, vat_rate=0.0)
    assert resolve_brussels_power_term(bolt, terms=terms) is not bolt


async def test_a_restart_does_not_bill_a_brussels_entry_without_the_term(
    hass: Any,
) -> None:
    """The cache is a module global, so it is empty after every restart.

    ``async_load_persistent`` resolves the stored card before the first refresh
    can fill it, which correctly leaves the term out, and the tick that does
    fetch it then keeps the card it already has without resolving anything.
    ``_reresolve_snapshot`` asked only whether the yearly volume had moved, so
    the term never arrived: 50,07 EUR a year short on a Brussels entry, and on
    one with no meter configured, never healed at all.
    """
    from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
    from custom_components.be_electricity_prices.const import DOMAIN

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "bolt",
            "contract": "bolt_fix",
            "region": "brussels",
            "dso": DSO_SIBELGA,
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)

    # Boot: the card is resolved with the cache empty, as a restart does.
    brugel._cache.clear()
    card = _brussels_card(fixed_term=14.73, vat_rate=0.0)
    coord._set_snapshot(card)
    booted = coord._snapshot
    assert booted is not None
    assert coord._snapshot_power_term is None

    # Nothing has changed yet, so nothing is redone.
    coord._reresolve_snapshot()
    assert coord._snapshot is booted

    # The tick fetches the term. The volume has not moved, and before this the
    # card was left exactly as it booted.
    brugel._cache[dt_util.now().year] = (47.24, 94.48)
    coord._reresolve_snapshot()
    assert coord._snapshot is not booted
    assert coord._snapshot_power_term == (47.24, 94.48)
    # 14,73 metering alone while the cache was empty, then the 64,80 its peers
    # print once Brugel answers: 14,73 plus the 47,24 power term grossed to the
    # card's own VAT-inclusive basis. The 50,07 difference is the year's.
    assert booted.dsos[DSO_SIBELGA].data_management_per_year == pytest.approx(
        14.73, abs=0.01
    )
    assert coord._snapshot.dsos[DSO_SIBELGA].data_management_per_year == pytest.approx(
        64.80, abs=0.01
    )

    # And it settles: a second tick with the same term redoes nothing.
    again = coord._snapshot
    coord._reresolve_snapshot()
    assert coord._snapshot is again


async def test_a_brussels_entry_billing_without_the_term_says_so(hass: Any) -> None:
    """Four suppliers print Sibelga's fixed charge whole and Bolt prints the
    metering half alone, so the power part is completed from Brugel's sheet.
    When that sheet cannot be read the card is billed as printed, 50,07 EUR a
    year short, and the four sibling gaps all raise a Repairs card where this
    said nothing.

    The signal needs no threshold: a card carrying the power part prints the
    band above 13 kVA, and the resolver sets that band on any card it
    completes, so a resolved Brussels snapshot without one is billing short.
    Measured over the 349 archived Brussels rows: every supplier printing the
    sum prints the band on all of its rows, Bolt on none of its 44.
    """
    from homeassistant.helpers import issue_registry as ir

    from custom_components.be_electricity_prices.const import DOMAIN
    from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
    from custom_components.be_electricity_prices.providers._resolve import (
        resolve_brussels_power_term,
    )

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "bolt",
            "contract": "bolt_fix",
            "region": "brussels",
            "dso": DSO_SIBELGA,
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    issue_id = f"brussels_power_term_missing_{entry.entry_id}"
    registry = ir.async_get(hass)

    # This year's sheet cannot be read, and last year's is still in hand: the
    # metering figure can be recognised for what it is, and the entry is short.
    brugel._cache[dt_util.now().year - 1] = (46.10, 92.20)
    card = _brussels_card(fixed_term=14.73, vat_rate=0.0)
    coord._snapshot = resolve_brussels_power_term(card, terms=None)
    coord._sync_brussels_power_term_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is not None

    # Brugel answers: the term lands and the notice clears by itself.
    coord._snapshot = resolve_brussels_power_term(card, terms=(47.24, 94.48))
    coord._sync_brussels_power_term_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # A card that prints the whole charge never needed it and never says so.
    coord._snapshot = resolve_brussels_power_term(
        _brussels_card(fixed_term=64.80, vat_rate=0.0, above=114.88), terms=None
    )
    coord._sync_brussels_power_term_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # And the shape the band signal ALONE cannot see: one combined number, no
    # band. Correctly priced, and the first version of this card told it that
    # it was 50 EUR a year short. The resolver has always used two signals and
    # this now asks the same question it does.
    brugel._cache.clear()
    brugel._cache[dt_util.now().year] = (47.24, 94.48)
    coord._snapshot = resolve_brussels_power_term(
        _brussels_card(fixed_term=64.80, vat_rate=0.0), terms=None
    )
    coord._sync_brussels_power_term_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None

    # With no term known for any year there is nothing to compare a metering
    # figure against, so it says nothing rather than guessing.
    brugel._cache.clear()
    coord._snapshot = resolve_brussels_power_term(
        _brussels_card(fixed_term=14.73, vat_rate=0.0), terms=None
    )
    coord._sync_brussels_power_term_issue()
    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_the_power_term_notice_is_silent_outside_brussels(hass: Any) -> None:
    """Sibelga is Brussels only, so a Flemish or Walloon entry has no term to
    miss and raising there would be noise on most of the fleet."""
    from homeassistant.helpers import issue_registry as ir

    from custom_components.be_electricity_prices.const import DOMAIN
    from custom_components.be_electricity_prices.coordinator import BePricesCoordinator

    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "bolt",
            "contract": "bolt_fix",
            "region": "flanders",
            "dso": "fluvius_antwerpen",
            "meter": "mono",
        },
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = _brussels_card(fixed_term=14.73, vat_rate=0.0)
    coord._sync_brussels_power_term_issue()
    assert (
        ir.async_get(hass).async_get_issue(
            DOMAIN, f"brussels_power_term_missing_{entry.entry_id}"
        )
        is None
    )


async def test_one_fetch_per_year_however_many_entries_ask() -> None:
    """Entries tick together and a backfill asks for several years at once, so
    without the lock a cold start opens the same download once per Brussels
    entry. The answer is identical either way; what it costs is the setup
    budget, where the sheet's 30 s ceiling is 10% of the 300 s HA allows.
    """
    calls: list[int] = []

    async def _slow_sheet(_session: object, year: int) -> str:
        calls.append(year)
        await asyncio.sleep(0.05)
        return _SHEET

    with patch.object(brugel, "_sheet_text", _slow_sheet):
        got = await asyncio.gather(
            *(brugel.ensure_power_term(cast(Any, None), 2026) for _ in range(4))
        )

    assert calls == [2026], "one download, however many entries asked"
    assert got == [(47.24, 94.48)] * 4, "and every caller gets the answer"

    # Asking again costs nothing at all: the answer is cached, so the lock is
    # never even reached.
    calls.clear()
    with patch.object(brugel, "_sheet_text", _slow_sheet):
        await brugel.ensure_power_term(cast(Any, None), 2026)
    assert calls == []

    # A different year is a different lock, so a backfill spanning two still
    # fetches both, concurrently.
    brugel._cache.clear()
    calls.clear()
    with patch.object(brugel, "_sheet_text", _slow_sheet):
        await asyncio.gather(
            brugel.ensure_power_term(cast(Any, None), 2026),
            brugel.ensure_power_term(cast(Any, None), 2026),
            brugel.ensure_power_term(cast(Any, None), 2027),
        )
    assert sorted(calls) == [2026, 2027]
