"""scripts/archive_cards.py: the daily writer behind the repository's card archive."""

from __future__ import annotations

import json
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import pytest

from custom_components.be_electricity_prices.const import SUPPLIER_CUSTOM
from custom_components.be_electricity_prices.providers._pdf import fetch_text
from custom_components.be_electricity_prices.providers.base import (
    Contract,
    ExtractorError,
    FixedRates,
    SupplierExtractor,
    SupplierSnapshot,
)
from tests import make_snapshot

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import archive_cards as ac  # type: ignore[import-not-found]  # noqa: E402

NOW = datetime(2026, 9, 11, 6, 0, tzinfo=UTC)
CARD_URL = "https://acme.test/card"

Fetch = Callable[[Any, str, str], Awaitable[SupplierSnapshot]]


class _Response:
    status = 200

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    """Just enough of aiohttp for fetch_text: one canned body per URL."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.hits = 0

    def get(self, url: str, **_kw: Any) -> _Response:
        self.hits += 1
        return _Response(self.pages[url])


def _extractor(
    fetch: Fetch,
    *,
    sid: str = "acme",
    contracts: tuple[str, ...] = ("acme_fix",),
    deprecated_until: date | None = None,
) -> SupplierExtractor:
    return SupplierExtractor(
        id=sid,
        label="Acme",
        contracts=tuple(
            Contract(id=c, label=c, kind="fixed", regions=frozenset({"wallonia"}))
            for c in contracts
        ),
        fetch=fetch,
        deprecated_until=deprecated_until,
    )


def _card_fetch(
    label: str, price: float = 0.2, session: _Session | None = None
) -> Fetch:
    """A fetch that reads its page through the memoised helper, as every
    real extractor does, then parses it into a snapshot."""
    page_session = session or _Session({CARD_URL: "card text"})

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        await fetch_text(page_session, CARD_URL)  # type: ignore[arg-type]
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=price),
            publication_label=label,
            source_url=CARD_URL,
        )

    return fetch


async def _no_sleep(_seconds: float) -> None:
    return None


async def test_a_card_is_filed_under_the_month_its_label_names(tmp_path: Path) -> None:
    """Seen in September, labelled August: filed under August, as a supplier
    publishing in arrears needs, while the text it read is kept under the
    month it was seen in and the card points at it."""
    summary = await ac.archive(
        tmp_path,
        extractors=[_extractor(_card_fetch("augustus 2026"))],
        now=NOW,
        sleep=_no_sleep,
    )
    assert (summary.stored, summary.unchanged, summary.failed) == (1, 0, [])
    card = json.loads((tmp_path / "acme/acme_fix/wallonia/2026-08.json").read_text())
    assert card["publication_label"] == "augustus 2026"
    assert card["_seen_on"] == "2026-09-11"
    assert card["energy"]["single"] == 0.2
    [source] = card["_sources"]
    assert source == {"url": CARD_URL, "variant": "text", "text": ANY}
    assert source["text"].startswith("texts/2026-09/")
    assert (tmp_path / source["text"]).read_text() == "card text"
    assert (tmp_path / "README.md").exists()


async def test_an_unreadable_label_files_under_the_month_seen(tmp_path: Path) -> None:
    await ac.archive(
        tmp_path,
        extractors=[_extractor(_card_fetch("carte tarifaire"))],
        now=NOW,
        sleep=_no_sleep,
    )
    assert (tmp_path / "acme/acme_fix/wallonia/2026-09.json").exists()


async def test_a_shared_page_is_read_once_and_credited_to_every_card(
    tmp_path: Path,
) -> None:
    """The memo spans the run, so the second product's parse is a memo hit;
    the hit still lands in that card's sources."""
    session = _Session({CARD_URL: "card text"})
    extractor = _extractor(
        _card_fetch("september 2026", session=session), contracts=("a", "b")
    )
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=NOW, sleep=_no_sleep
    )
    assert summary.stored == 2
    assert session.hits == 1
    texts = {
        json.loads((tmp_path / f"acme/{c}/wallonia/2026-09.json").read_text())[
            "_sources"
        ][0]["text"]
        for c in ("a", "b")
    }
    assert len(texts) == 1


async def test_a_repeat_run_keeps_the_first_capture(tmp_path: Path) -> None:
    """The same parse a day later changes nothing on disk, so a quiet day
    has nothing to commit; a changed parse is a new capture."""
    extractors = [_extractor(_card_fetch("september 2026"))]
    await ac.archive(tmp_path, extractors=extractors, now=NOW, sleep=_no_sleep)
    path = tmp_path / "acme/acme_fix/wallonia/2026-09.json"
    first = path.read_text()
    later = NOW.replace(day=12)
    summary = await ac.archive(
        tmp_path, extractors=extractors, now=later, sleep=_no_sleep
    )
    assert (summary.stored, summary.unchanged) == (0, 1)
    assert path.read_text() == first
    summary = await ac.archive(
        tmp_path,
        extractors=[_extractor(_card_fetch("september 2026", price=0.25))],
        now=later,
        sleep=_no_sleep,
    )
    assert (summary.stored, summary.unchanged) == (1, 0)
    card = json.loads(path.read_text())
    assert card["energy"]["single"] == 0.25
    assert card["_seen_on"] == "2026-09-12"


async def test_transient_failures_are_retried_and_permanent_ones_are_not(
    tmp_path: Path,
) -> None:
    good = _card_fetch("september 2026")
    attempts = 0

    async def flaky(session: Any, contract: str, region: str) -> SupplierSnapshot:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ExtractorError(f"network error fetching {CARD_URL}: reset")
        return await good(session, contract, region)

    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    summary = await ac.archive(
        tmp_path, extractors=[_extractor(flaky)], now=NOW, sleep=sleep
    )
    assert summary.stored == 1
    assert slept == [10, 30]

    async def gone(*_args: Any) -> SupplierSnapshot:
        raise ExtractorError(f"HTTP 404 fetching {CARD_URL}")

    slept.clear()
    summary = await ac.archive(
        tmp_path, extractors=[_extractor(gone, sid="beta")], now=NOW, sleep=sleep
    )
    assert summary.stored == 0
    assert slept == []
    assert summary.failed == [
        f"beta/acme_fix/wallonia: ExtractorError: HTTP 404 fetching {CARD_URL}"
    ]


async def test_backfill_mirrors_the_supplier_archive_for_months_not_held(
    tmp_path: Path,
) -> None:
    """A supplier with an archive is asked for each closed month the branch
    lacks; a month it answers None for or still flags provisional stays
    absent, a month already on disk is not asked again, and a supplier
    without an archive is not asked at all."""
    asked: list[date] = []
    settled = {(2026, 7): 0.21, (2026, 6): 0.22}

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        asked.append(month)
        if (month.year, month.month) == (2026, 8):
            snap = make_snapshot(
                supplier="acme", contract=contract, publication_label="augustus 2026"
            )
            return replace(snap, provisional=True)
        price = settled.get((month.year, month.month))
        if price is None:
            return None
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=price),
            publication_label=f"{month:%Y-%m}",
        )

    extractor = SupplierExtractor(
        id="acme",
        label="Acme",
        contracts=(
            Contract(
                id="acme_fix",
                label="Fix",
                kind="fixed",
                regions=frozenset({"wallonia"}),
            ),
        ),
        fetch=_card_fetch("september 2026"),
        fetch_for_month=fetch_for_month,
    )
    without = _extractor(_card_fetch("september 2026"), sid="beta")
    summary = await ac.archive(
        tmp_path,
        extractors=[extractor, without],
        backfill_months=4,
        now=NOW,
        sleep=_no_sleep,
    )
    assert (summary.stored, summary.backfilled, summary.absent) == (2, 2, 2)
    assert asked == [
        date(2026, 8, 1),
        date(2026, 7, 1),
        date(2026, 6, 1),
        date(2026, 5, 1),
    ]
    july = json.loads((tmp_path / "acme/acme_fix/wallonia/2026-07.json").read_text())
    assert july["_via"] == "archive"
    assert july["energy"]["single"] == 0.21
    assert (tmp_path / "acme/acme_fix/wallonia/2026-06.json").exists()
    assert not (tmp_path / "acme/acme_fix/wallonia/2026-08.json").exists()
    assert not (tmp_path / "acme/acme_fix/wallonia/2026-05.json").exists()
    assert not (tmp_path / "beta/acme_fix/wallonia/2026-08.json").exists()
    live = json.loads((tmp_path / "acme/acme_fix/wallonia/2026-09.json").read_text())
    assert live["_via"] == "live"
    # A second run asks only for the months still missing.
    asked.clear()
    summary = await ac.archive(
        tmp_path, extractors=[extractor], backfill_months=4, now=NOW, sleep=_no_sleep
    )
    assert asked == [date(2026, 8, 1), date(2026, 5, 1)]
    assert (summary.backfilled, summary.absent) == (0, 2)


def test_targets_skip_the_custom_and_withdrawn_suppliers() -> None:
    live = _extractor(_card_fetch("x"))
    gone = _extractor(
        _card_fetch("x"), sid="dats24", deprecated_until=date(2026, 8, 31)
    )
    custom = _extractor(_card_fetch("x"), sid=SUPPLIER_CUSTOM)
    today = date(2026, 9, 11)
    assert [
        (e.id, c, r) for e, c, r in ac._targets([live, gone, custom], set(), today)
    ] == [("acme", "acme_fix", "wallonia")]
    # On the withdrawal day itself the supplier is still trading.
    assert len(ac._targets([gone], set(), date(2026, 8, 31))) == 1
    assert ac._targets([live], {"beta"}, today) == []
    assert len(ac._targets([live], {"acme"}, today)) == 1


def test_prune_removes_months_older_than_the_retention(tmp_path: Path) -> None:
    for month in ("2023-08", "2023-09", "2026-09"):
        card = tmp_path / "acme/acme_fix/wallonia" / f"{month}.json"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text("{}")
        text = tmp_path / "texts" / month / "abc.txt"
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text("x")
    old = tmp_path / "old/old_fix/wallonia/2023-01.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    assert ac._prune(tmp_path, 36, date(2026, 9, 11)) == 3
    assert not (tmp_path / "old").exists()
    assert not (tmp_path / "acme/acme_fix/wallonia/2023-08.json").exists()
    assert (tmp_path / "acme/acme_fix/wallonia/2023-09.json").exists()
    assert not (tmp_path / "texts/2023-08").exists()
    assert (tmp_path / "texts/2023-09/abc.txt").exists()


def test_months_before() -> None:
    assert ac._months_before(date(2026, 9, 11), 36) == "2023-09"
    assert ac._months_before(date(2026, 1, 1), 1) == "2025-12"
    assert ac._months_before(date(2026, 1, 1), 0) == "2026-01"


def test_main_fails_the_run_only_when_nothing_was_archived(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def gone(*_args: Any) -> SupplierSnapshot:
        raise ExtractorError("HTTP 404 fetching x")

    monkeypatch.setattr(sys, "argv", ["archive_cards.py", "--out", str(tmp_path)])
    monkeypatch.setattr(ac, "all_extractors", lambda: (_extractor(gone),))
    assert ac.main() == 1
    monkeypatch.setattr(
        ac, "all_extractors", lambda: (_extractor(_card_fetch("september 2026")),)
    )
    assert ac.main() == 0
