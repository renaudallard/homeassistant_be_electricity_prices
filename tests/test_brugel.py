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

from collections.abc import Iterator

import pytest

from custom_components.be_electricity_prices import brugel
from custom_components.be_electricity_prices.const import DSO_SIBELGA
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
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
    """The module caches per year for the life of the process."""
    brugel._cache.clear()
    brugel._failed_at.clear()
    yield
    brugel._cache.clear()
    brugel._failed_at.clear()


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
    from custom_components.be_electricity_prices import snapshot_store

    assert "cached_power_term(" in inspect.getsource(snapshot_store._resolve_snapshot)


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
