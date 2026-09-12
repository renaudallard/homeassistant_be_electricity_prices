"""scripts/archive_cards.py: the daily writer behind the repository's card archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import ANY

import aiohttp
import pytest

from custom_components.be_electricity_prices.const import SUPPLIER_CUSTOM
from custom_components.be_electricity_prices.providers import _pdf
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


async def test_a_stored_text_keeps_its_line_endings(tmp_path: Path) -> None:
    """Cociter's listing carries carriage returns; a text read back with
    newline translation would be two bytes shorter than what the parser
    saw, and every replay would rewrite the row for nothing."""
    text = "line one\r\nline two\rline three"
    rel = ac._write_text(tmp_path, "2026-09", text)
    assert ac.read_text(tmp_path / rel) == text
    assert hashlib.sha256(text.encode()).hexdigest() in rel


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


class _PdfResponse:
    status = 200
    content_length = None

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload

    async def __aenter__(self) -> _PdfResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _PdfSession:
    """Just enough of aiohttp for the PDF readers: one payload per URL."""

    def __init__(self, pdfs: dict[str, bytes]) -> None:
        self.pdfs = pdfs

    def get(self, url: str, **_kw: Any) -> _PdfResponse:
        return _PdfResponse(self.pdfs[url])


PDF_URL = "https://acme.test/card.pdf"


def _pdf_fetch(session: _PdfSession, renders: list[bytes]) -> Fetch:
    """A fetch that reads its card through the PDF reader seam, the way the
    real extractors do, with a renderer that counts what it rendered."""

    def render(payload: bytes) -> str:
        renders.append(payload)
        return f"card text for {payload.decode()}"

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        # The canned session while it holds the card; once the test empties
        # it, the session handed in, which in a replay is the kept copy.
        reader = session if PDF_URL in session.pdfs else _session
        text = await _pdf._pdf_text(
            reader,  # type: ignore[arg-type]
            PDF_URL,
            variant="plain",
            timeout=5,
            render=render,
        )
        price = 0.2 if "v1" in text else 0.3
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=price),
            publication_label="september 2026",
            source_url=PDF_URL,
        )

    return fetch


async def test_an_unchanged_card_is_kept_once_and_never_rendered_again(
    tmp_path: Path,
) -> None:
    """The bytes' digest decides: the first run renders the card and writes
    it under the PDF directory for upload; a later run with the same bytes
    serves the stored text and renders nothing. The PDF is offered again
    until the manifest says it was uploaded, and a changed card is a new
    digest, rendered and kept afresh."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    renders: list[bytes] = []
    extractor = _extractor(_pdf_fetch(session, renders))
    summary = await ac.archive(
        out, extractors=[extractor], pdf_dir=pdfs, now=NOW, sleep=_no_sleep
    )
    assert (summary.rendered, summary.unrendered, summary.pdfs_saved) == (1, 0, 1)
    digest = hashlib.sha256(b"%PDF v1").hexdigest()
    assert (pdfs / f"electricity-2026-09/{digest}.pdf").read_bytes() == b"%PDF v1"
    card = json.loads((out / "acme/acme_fix/wallonia/2026-09.json").read_text())
    [source] = card["_sources"]
    assert source["pdf"] == digest
    assert source["variant"] == "plain"
    assert card["energy"]["single"] == 0.2

    # Same bytes the next day: nothing rendered, the text came from the
    # branch, the PDF is written again because nothing says it was uploaded.
    shutil.rmtree(pdfs)
    summary = await ac.archive(
        out,
        extractors=[extractor],
        pdf_dir=pdfs,
        now=NOW.replace(day=12),
        sleep=_no_sleep,
    )
    assert (summary.rendered, summary.unrendered, summary.pdfs_saved) == (0, 1, 1)
    assert (summary.stored, summary.unchanged) == (0, 1)
    assert renders == [b"%PDF v1"]

    # Once the manifest records the upload it is neither written nor rendered.
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )
    shutil.rmtree(pdfs)
    summary = await ac.archive(
        out,
        extractors=[extractor],
        pdf_dir=pdfs,
        now=NOW.replace(day=13),
        sleep=_no_sleep,
    )
    assert (summary.rendered, summary.unrendered, summary.pdfs_saved) == (0, 1, 0)
    assert not pdfs.exists()

    # A corrected card is new bytes: rendered, kept, and the row rewritten.
    session.pdfs[PDF_URL] = b"%PDF v2"
    summary = await ac.archive(
        out,
        extractors=[extractor],
        pdf_dir=pdfs,
        now=NOW.replace(day=14),
        sleep=_no_sleep,
    )
    assert (summary.rendered, summary.pdfs_saved, summary.stored) == (1, 1, 1)
    digest2 = hashlib.sha256(b"%PDF v2").hexdigest()
    assert (pdfs / f"electricity-2026-09/{digest2}.pdf").exists()
    card = json.loads((out / "acme/acme_fix/wallonia/2026-09.json").read_text())
    assert card["_sources"][0]["pdf"] == digest2
    assert card["energy"]["single"] == 0.3


def _text_fetch(session: _Session, parser: dict[str, str], seen: list[date]) -> Fetch:
    """A fetch whose parse depends on a knob the test turns, reading its
    page through the memo like a real extractor and noting today's date.
    The page reads ``price=<eur> month=<label>``."""

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        text = await fetch_text(session, CARD_URL)  # type: ignore[arg-type]
        seen.append(date.today())
        fields = dict(part.split("=", 1) for part in text.split(" ", 1))
        price = float(fields["price"]) * (2 if parser["version"] == "doubling" else 1)
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=price),
            publication_label=fields["month"],
            source_url=CARD_URL,
        )

    return fetch


async def test_stored_rows_are_replayed_only_when_the_parser_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser change replays every stored row from its stored texts, with
    the clock pinned to the day the row was captured and no supplier asked;
    a run under the same parser replays nothing."""
    session = _Session({CARD_URL: "price=0.2 month=augustus 2026"})
    parser = {"version": "plain"}
    seen: list[date] = []
    extractor = _extractor(_text_fetch(session, parser, seen))
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    august = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    assert (tmp_path / "parser.txt").read_text().strip() == "digest-a"
    row = tmp_path / "acme/acme_fix/wallonia/2026-08.json"
    assert json.loads(row.read_text())["energy"]["single"] == 0.2

    # September, same parser: the live walk stores this month's card on the
    # real clock, and nothing is replayed.
    session.pages[CARD_URL] = "price=0.2 month=september 2026"
    seen.clear()
    later = NOW.replace(day=18)
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=later, sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed) == (0, 0)
    assert seen == [date.today()]
    assert (tmp_path / "acme/acme_fix/wallonia/2026-09.json").exists()

    # The parser changed: August is replayed from its stored text under
    # August's clock (the page itself is September's by now), and rewritten;
    # September's row was already written by today's live walk.
    parser["version"] = "doubling"
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    seen.clear()
    hits_before = session.hits
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=later, sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed, summary.unreplayable) == (2, 1, [])
    assert session.hits == hits_before + 1  # the live walk only
    card = json.loads(row.read_text())
    assert card["energy"]["single"] == 0.4
    assert card["publication_label"] == "augustus 2026"
    assert card["_seen_on"] == "2026-08-05"
    assert (tmp_path / "parser.txt").read_text().strip() == "digest-b"
    assert seen == [date.today(), date(2026, 8, 5), date(2026, 9, 18)]

    # And the forced flag replays even when the digest matches.
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=later, reparse=True, sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed) == (2, 0)


async def test_a_row_that_cannot_be_reproduced_offline_is_left_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parser that now wants a page the row never read is refused, and a
    row whose text file is gone is skipped; both are reported and neither
    is rewritten."""
    session = _Session({CARD_URL: "price=0.2 month=augustus 2026"})
    parser = {"version": "plain"}
    extractor = _extractor(_text_fetch(session, parser, []))
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    august = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    row = tmp_path / "acme/acme_fix/wallonia/2026-08.json"
    before = row.read_text()

    async def wants_more(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        # The page the row read comes from the memo; the extra one goes to
        # the session handed in, which in a replay refuses it.
        await fetch_text(session, CARD_URL)  # type: ignore[arg-type]
        await fetch_text(_session, "https://acme.test/extra")
        return make_snapshot(supplier="acme", contract=contract)

    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    summary = await ac.archive(
        tmp_path, extractors=[_extractor(wants_more)], now=NOW, sleep=_no_sleep
    )
    assert summary.replayed == 0
    [reason] = summary.unreplayable
    assert reason.startswith(
        "acme/acme_fix/wallonia/2026-08: ExtractorError: network error"
    )
    assert row.read_text() == before

    # The text the row read is gone from the branch. The page has moved on
    # to September, so today's live walk files elsewhere and leaves this row.
    card = json.loads(before)
    card["_sources"][0]["text"] = "texts/2026-08/gone.txt"
    row.write_text(json.dumps(card))
    session.pages[CARD_URL] = "price=0.2 month=september 2026"
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-c")
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=NOW, sleep=_no_sleep
    )
    # September's fresh row replays fine; August is skipped and named.
    assert summary.replayed == 1
    assert summary.unreplayable == [
        "acme/acme_fix/wallonia/2026-08: texts/2026-08/gone.txt is missing"
    ]
    assert json.loads(row.read_text()) == card
    assert (tmp_path / "parser.txt").read_text().strip() == "digest-c"


async def test_a_reader_that_changed_variant_gets_the_kept_pdf_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row has text for the plain reading only; a parser that now reads
    the card in layout mode finds no text and is handed the kept PDF from
    the PDF directory, which is rendered and the row rewritten."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    renders: list[bytes] = []
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, renders))],
        pdf_dir=pdfs,
        now=NOW,
        sleep=_no_sleep,
    )
    assert renders == [b"%PDF v1"]
    session.pdfs.clear()  # the supplier is not there any more
    layout_renders: list[bytes] = []

    def render(payload: bytes) -> str:
        layout_renders.append(payload)
        return "layout text"

    async def layout_fetch(
        _session: Any, contract: str, region: str
    ) -> SupplierSnapshot:
        text = await _pdf._pdf_text(
            _session, PDF_URL, variant="layout", timeout=5, render=render
        )
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=0.5 if text == "layout text" else 0.0),
            publication_label="september 2026",
            source_url=PDF_URL,
        )

    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    summary = await ac.archive(
        out,
        extractors=[_extractor(layout_fetch)],
        pdf_dir=pdfs,
        now=NOW.replace(day=12),
        sleep=_no_sleep,
    )
    # Today's live walk fails (the session is empty) but the replay has the PDF.
    assert len(summary.failed) == 1
    assert (summary.replayed, summary.reparsed, summary.unreplayable) == (1, 1, [])
    assert layout_renders == [b"%PDF v1"]
    card = json.loads((out / "acme/acme_fix/wallonia/2026-09.json").read_text())
    assert card["energy"]["single"] == 0.5
    assert card["_sources"][0]["variant"] == "layout"
    digest = hashlib.sha256(b"%PDF v1").hexdigest()
    assert card["_sources"][0]["pdf"] == digest


async def test_an_archive_row_replays_through_the_supplier_archive_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backfilled row is replayed with fetch_for_month for its own month,
    not with the live fetch."""
    calls: list[tuple[str, date | None]] = []

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        calls.append(("live", None))
        return make_snapshot(
            supplier="acme", contract=contract, publication_label="september 2026"
        )

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        calls.append(("archive", month))
        return (
            make_snapshot(
                supplier="acme", contract=contract, publication_label=f"{month:%Y-%m}"
            )
            if month.month == 8
            else None
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
        fetch=fetch,
        fetch_for_month=fetch_for_month,
    )
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    await ac.archive(
        tmp_path, extractors=[extractor], backfill_months=1, now=NOW, sleep=_no_sleep
    )
    calls.clear()
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=NOW, sleep=_no_sleep
    )
    assert summary.replayed == 2
    assert calls == [("live", None), ("archive", date(2026, 8, 1)), ("live", None)]


async def test_a_rerender_reads_every_card_back_from_the_kept_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under --rerender a stored month's PDF text is not seeded: the card is
    fetched back from the kept copy and rendered again, which is how a reader
    upgrade reaches the branch; an unchanged parse is not rewritten."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    renders: list[bytes] = []
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, renders))],
        pdf_dir=pdfs,
        now=NOW.replace(day=5),
        sleep=_no_sleep,
    )
    assert renders == [b"%PDF v1"]
    row = out / "acme/acme_fix/wallonia/2026-09.json"
    before = json.loads(row.read_text())
    session.pdfs.clear()  # the supplier is gone; only the kept copy is left
    summary = await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, renders))],
        pdf_dir=pdfs,
        now=NOW.replace(day=6),
        rerender=True,
        sleep=_no_sleep,
    )
    assert (summary.replayed, summary.reparsed, summary.unreplayable) == (1, 0, [])
    assert renders == [b"%PDF v1", b"%PDF v1"]
    after = json.loads(row.read_text())
    assert after["energy"] == before["energy"]
    assert after["_seen_on"] == "2026-09-05"


def test_a_probe_in_a_replay_finds_only_the_card_the_row_read(tmp_path: Path) -> None:
    """An extractor that HEADs candidate URLs before choosing one lands on
    the kept card and nowhere else."""
    from custom_components.be_electricity_prices.providers._pdf import (
        head_freshness_key,
        head_ok,
    )

    replay = ac._ReplaySession(None, tmp_path, None)  # type: ignore[arg-type]
    replay.pdfs = {"https://acme.test/2026-08.pdf": "abc"}
    assert asyncio.run(head_ok(replay, "https://acme.test/2026-08.pdf"))  # type: ignore[arg-type]
    assert not asyncio.run(head_ok(replay, "https://acme.test/2026-07.pdf"))  # type: ignore[arg-type]
    # A probe that wants a freshness header gets one for a kept card only:
    # Eneco's archive walk skips a candidate with neither ETag nor
    # Last-Modified, so a bare 200 would still read as a missing card.
    assert (
        asyncio.run(head_freshness_key(replay, "https://acme.test/2026-08.pdf"))  # type: ignore[arg-type]
        is not None
    )
    assert (
        asyncio.run(head_freshness_key(replay, "https://acme.test/2026-07.pdf")) is None
    )  # type: ignore[arg-type]


def test_a_text_that_changed_bytes_but_not_its_parse_is_not_a_new_card() -> None:
    """A listing page with a nonce, or a render that is not byte-stable, gives
    a new text file every day; the row is rewritten only when what a source
    was or what it parsed to changed."""
    source = {"url": "u", "variant": "text", "text": "texts/2026-09/a.txt"}
    card: dict[str, Any] = {
        "_cached_at": "x",
        "_seen_on": "2026-09-11",
        "energy": {"single": 0.2},
        "_sources": [source],
    }
    same_parse = {**card, "_sources": [{**source, "text": "texts/2026-09/b.txt"}]}
    assert ac._same_card(card, same_parse)
    other_variant = {**card, "_sources": [{**source, "variant": "layout"}]}
    assert not ac._same_card(card, other_variant)
    other_parse = {**card, "energy": {"single": 0.3}}
    assert not ac._same_card(card, other_parse)


def test_replay_session_refuses_what_it_does_not_hold(tmp_path: Path) -> None:
    replay = ac._ReplaySession(None, tmp_path, None)  # type: ignore[arg-type]

    async def run() -> bytes:
        async with replay.get("https://acme.test/card.pdf") as resp:
            return await resp.read()

    with pytest.raises(aiohttp.ClientConnectionError):
        asyncio.run(run())
    # A kept copy is found by digest under any release directory.
    (tmp_path / "electricity-2026-09-2").mkdir()
    (tmp_path / "electricity-2026-09-2/abc.pdf").write_bytes(b"%PDF kept")
    replay.pdfs = {"https://acme.test/card.pdf": "abc"}
    assert asyncio.run(run()) == b"%PDF kept"
    # Without a local copy and without a manifest entry there is nowhere to go.
    replay = ac._ReplaySession(None, None, "https://cards.test/download", {})  # type: ignore[arg-type]
    replay.pdfs = {"https://acme.test/card.pdf": "abc"}
    with pytest.raises(aiohttp.ClientConnectionError):
        asyncio.run(run())


async def test_a_pdf_is_filed_under_the_month_of_the_card_not_the_day_taken(
    tmp_path: Path,
) -> None:
    """A card labelled August and captured in September goes to August's
    release directory, a mirrored month to its own month, and bytes no row
    names (a card whose parse failed) to the month they were captured in."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession(
        {
            PDF_URL: b"%PDF august",
            "https://acme.test/july.pdf": b"%PDF july",
            "https://acme.test/broken.pdf": b"%PDF broken",
        }
    )

    def render(payload: bytes) -> str:
        return payload.decode()

    async def read(url: str) -> str:
        return await _pdf._pdf_text(
            session,  # type: ignore[arg-type]
            url,
            variant="plain",
            timeout=5,
            render=render,
        )

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        await read(PDF_URL)
        return make_snapshot(
            supplier="acme", contract=contract, publication_label="augustus 2026"
        )

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        if month.month != 7:
            return None
        await read("https://acme.test/july.pdf")
        return make_snapshot(
            supplier="acme", contract=contract, publication_label="2026-07"
        )

    async def broken(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        await read("https://acme.test/broken.pdf")
        raise ExtractorError("could not parse the card")

    acme = SupplierExtractor(
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
        fetch=fetch,
        fetch_for_month=fetch_for_month,
    )
    summary = await ac.archive(
        out,
        extractors=[acme, _extractor(broken, sid="beta")],
        pdf_dir=pdfs,
        backfill_months=2,
        now=NOW,
        sleep=_no_sleep,
    )
    assert summary.pdfs_saved == 3
    august = hashlib.sha256(b"%PDF august").hexdigest()
    july = hashlib.sha256(b"%PDF july").hexdigest()
    broken_digest = hashlib.sha256(b"%PDF broken").hexdigest()
    assert (pdfs / f"electricity-2026-08/{august}.pdf").exists()
    assert (pdfs / f"electricity-2026-07/{july}.pdf").exists()
    assert (pdfs / f"electricity-2026-09/{broken_digest}.pdf").exists()
    assert sorted(p.name for p in pdfs.iterdir()) == [
        "electricity-2026-07",
        "electricity-2026-08",
        "electricity-2026-09",
    ]


async def test_a_card_handed_over_inside_json_is_still_kept_and_named(
    tmp_path: Path,
) -> None:
    """A provider that gets its card some other way than through a reader
    renders it through render_pdf; the archiver sees it there, keeps the
    bytes under the row's month and names the card in the row's sources,
    text and digest alike, so the row links to it and a replay finds it."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _Session({CARD_URL: '{"sheet": "base64-ish"}'})

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        await fetch_text(session, CARD_URL)  # type: ignore[arg-type]
        text = await _pdf.render_pdf(
            "aligned",
            "https://acme.test/sheet?name=x",
            b"%PDF from json",
            lambda p: "sheet text",
        )
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=0.2 if text == "sheet text" else 0.0),
            publication_label="september 2026",
        )

    summary = await ac.archive(
        out, extractors=[_extractor(fetch)], pdf_dir=pdfs, now=NOW, sleep=_no_sleep
    )
    assert (summary.stored, summary.pdfs_saved, summary.rendered) == (1, 1, 1)
    digest = hashlib.sha256(b"%PDF from json").hexdigest()
    assert (
        pdfs / f"electricity-2026-09/{digest}.pdf"
    ).read_bytes() == b"%PDF from json"
    card = json.loads((out / "acme/acme_fix/wallonia/2026-09.json").read_text())
    assert card["energy"]["single"] == 0.2
    kinds = {(s["variant"], s["url"]): s for s in card["_sources"]}
    sheet = kinds[("aligned", "https://acme.test/sheet?name=x")]
    assert sheet["pdf"] == digest
    assert (out / sheet["text"]).read_text() == "sheet text"
    assert ("text", CARD_URL) in kinds
    coverage = (out / "coverage/acme.md").read_text()
    assert "| acme_fix | wallonia | pdf json |" in coverage


async def test_a_row_that_read_no_pdf_links_its_page_and_its_json(
    tmp_path: Path,
) -> None:
    """A card parsed from a page links the text of that page on the branch
    where a PDF row links its card, and every month links what the parse
    produced; without a branch URL the cells still say which is which."""
    branch = "https://github.test/repo/blob/archive"
    summary = await ac.archive(
        tmp_path,
        extractors=[_extractor(_card_fetch("september 2026"))],
        archive_base_url=branch,
        now=NOW,
        sleep=_no_sleep,
    )
    assert summary.stored == 1
    row = json.loads((tmp_path / "acme/acme_fix/wallonia/2026-09.json").read_text())
    text = row["_sources"][0]["text"]
    assert (
        f"| acme_fix | wallonia | [page]({branch}/{text})"
        f" [json]({branch}/acme/acme_fix/wallonia/2026-09.json) |"
    ) in (tmp_path / "coverage/acme.md").read_text()
    ac._write_coverage(tmp_path)
    assert (
        "| acme_fix | wallonia | page json |"
        in (tmp_path / "coverage/acme.md").read_text()
    )


async def test_a_replay_names_and_keeps_a_card_the_row_never_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mirrored row written before a provider's card went through the
    seam names no PDF. When the parser changes and the row is replayed, the
    card the hook is handed is added to the row's sources and its bytes are
    filed under the row's month, so the row gets its link."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _Session({"https://acme.test/sheet": '{"sheet": "base64-ish"}'})
    renders: list[bytes] = []

    def render_july(payload: bytes) -> str:
        renders.append(payload)
        return "july text"

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        if month.month != 7:
            return None
        await fetch_text(session, "https://acme.test/sheet")  # type: ignore[arg-type]
        text = await _pdf.render_pdf(
            "aligned", "https://acme.test/sheet?name=july", b"%PDF july", render_july
        )
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=0.2 if text == "july text" else 0.0),
            publication_label="2026-07",
        )

    acme = SupplierExtractor(
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
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    await ac.archive(
        out,
        extractors=[acme],
        pdf_dir=pdfs,
        backfill_months=2,
        now=NOW,
        sleep=_no_sleep,
    )
    row = out / "acme/acme_fix/wallonia/2026-07.json"
    card = json.loads(row.read_text())
    assert any("pdf" in s for s in card["_sources"])
    # Make it a row from before the seam: no card named, no bytes kept.
    card["_sources"] = [s for s in card["_sources"] if "pdf" not in s]
    row.write_text(json.dumps(card))
    shutil.rmtree(pdfs)
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    summary = await ac.archive(
        out, extractors=[acme], pdf_dir=pdfs, now=NOW.replace(day=13), sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed, summary.unreplayable) == (2, 1, [])
    card = json.loads(row.read_text())
    [sheet] = [s for s in card["_sources"] if "pdf" in s]
    digest = hashlib.sha256(b"%PDF july").hexdigest()
    assert sheet["pdf"] == digest
    assert sheet["variant"] == "aligned"
    assert (pdfs / f"electricity-2026-07/{digest}.pdf").read_bytes() == b"%PDF july"


def test_prune_drops_manifest_entries_older_than_the_retention(tmp_path: Path) -> None:
    (tmp_path / "pdfs.json").write_text(
        json.dumps(
            {
                "old": "electricity-2023-08/old.pdf",
                "kept": "electricity-2023-09-2/kept.pdf",
            }
        )
    )
    assert ac._prune(tmp_path, 36, date(2026, 9, 11)) == 1
    assert json.loads((tmp_path / "pdfs.json").read_text()) == {
        "kept": "electricity-2023-09-2/kept.pdf"
    }


async def test_a_supplier_not_answering_is_given_up_on_for_the_day(
    tmp_path: Path,
) -> None:
    """Three network failures in a row and the rest of that supplier's cards
    are skipped, live and backfill alike; a parse failure does not count,
    and another supplier is unaffected."""
    asked: list[str] = []

    async def blocked(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        asked.append(contract)
        raise ExtractorError(f"network error fetching {contract}: timeout")

    async def blocked_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        asked.append(f"{contract}/{month:%Y-%m}")
        raise ExtractorError("network error fetching x: timeout")

    mega = SupplierExtractor(
        id="mega",
        label="Mega",
        contracts=tuple(
            Contract(id=c, label=c, kind="fixed", regions=frozenset({"wallonia"}))
            for c in ("a", "b", "c", "d", "e")
        ),
        fetch=blocked,
        fetch_for_month=blocked_month,
    )
    fine = _extractor(_card_fetch("september 2026"))
    summary = await ac.archive(
        tmp_path, extractors=[mega, fine], backfill_months=2, now=NOW, sleep=_no_sleep
    )
    # Each card is retried, so count the cards asked, not the attempts.
    assert list(dict.fromkeys(asked)) == ["a", "b", "c"]
    assert summary.given_up == ["mega"]
    assert len(summary.failed) == 3
    assert summary.stored == 1

    # A parse failure is not the network: it resets the count.
    asked.clear()
    calls = 0

    async def flaky(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        nonlocal calls
        calls += 1
        asked.append(contract)
        if calls % 3 == 0:
            raise ExtractorError("could not parse the card")
        raise ExtractorError("network error fetching x: timeout")

    mixed = SupplierExtractor(
        id="mixed",
        label="Mixed",
        contracts=tuple(
            Contract(id=c, label=c, kind="fixed", regions=frozenset({"wallonia"}))
            for c in ("a", "b", "c", "d", "e")
        ),
        fetch=flaky,
    )
    summary = await ac.archive(tmp_path, extractors=[mixed], now=NOW, sleep=_no_sleep)
    assert list(dict.fromkeys(asked)) == ["a", "b", "c", "d", "e"]
    assert summary.given_up == []


async def test_the_coverage_table_says_what_the_branch_holds(tmp_path: Path) -> None:
    """One table per supplier, one column per month on the branch, and each
    cell says how the month was captured; rewritten to the same bytes when
    nothing changed."""
    settled = {(2026, 8): 0.21}

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
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
    await ac.archive(
        tmp_path, extractors=[extractor], backfill_months=2, now=NOW, sleep=_no_sleep
    )
    coverage = (tmp_path / "coverage/acme.md").read_text()
    assert "# acme" in coverage
    assert "| contract | region | 2026-08 | 2026-09 |" in coverage
    index = (tmp_path / "coverage.md").read_text()
    assert "- [acme](coverage/acme.md): 1 rows, 2026-08 to 2026-09" in index
    # August came from the supplier archive without reading a page.
    assert "| acme_fix | wallonia | json (mirror) | page json |" in coverage
    again = await ac.archive(
        tmp_path, extractors=[extractor], backfill_months=2, now=NOW, sleep=_no_sleep
    )
    assert again.unchanged == 1
    assert (tmp_path / "coverage/acme.md").read_text() == coverage


async def test_a_person_can_get_from_a_month_to_its_pdf_and_its_json(
    tmp_path: Path,
) -> None:
    """Once the manifest says where a card's PDF landed, the coverage cell
    links to it beside the row's JSON; --index-only rewrites the table
    without fetching anything, refreshes a stale branch README and drops
    the PDF index earlier versions wrote."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    extractor = _extractor(_pdf_fetch(session, []), contracts=("a", "b"))
    base = "https://cards.test/releases/download"
    branch = "https://github.test/repo/blob/archive"
    await ac.archive(
        out,
        extractors=[extractor],
        pdf_dir=pdfs,
        pdf_base_url=base,
        archive_base_url=branch,
        now=NOW,
        sleep=_no_sleep,
    )
    digest = hashlib.sha256(b"%PDF v1").hexdigest()
    # Not uploaded yet: the PDF is named without a link, the JSON is linked.
    coverage = (out / "coverage/acme.md").read_text()
    assert (
        f"| a | wallonia | pdf [json]({branch}/acme/a/wallonia/2026-09.json) |"
        in coverage
    )
    # The upload step recorded it; the index-only pass links everything up.
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )
    (out / "pdfs.md").write_text("stale index")
    (out / "README.md").write_text("stale readme")
    ac._write_listings(out, base, branch)
    url = f"{base}/electricity-2026-09/{digest}.pdf"
    coverage = (out / "coverage/acme.md").read_text()
    assert (
        f"| a | wallonia | [pdf]({url}) [json]({branch}/acme/a/wallonia/2026-09.json) |"
        in coverage
    )
    assert (
        f"| b | wallonia | [pdf]({url}) [json]({branch}/acme/b/wallonia/2026-09.json) |"
        in coverage
    )
    assert not (out / "pdfs.md").exists()
    assert (out / "README.md").read_text() == ac._README


def test_index_only_touches_nothing_but_the_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sys, "argv", ["archive_cards.py", "--out", str(tmp_path), "--index-only"]
    )
    monkeypatch.setattr(
        ac, "all_extractors", lambda: (_ for _ in ()).throw(AssertionError("fetched"))
    )
    (tmp_path / "pdfs.md").write_text("stale index")
    assert ac.main() == 0
    assert (tmp_path / "coverage.md").exists()
    assert (tmp_path / "README.md").exists()
    assert not (tmp_path / "pdfs.md").exists()


def test_a_supplier_that_left_the_branch_loses_its_sheet(tmp_path: Path) -> None:
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage/gone.md").write_text("stale sheet")
    row = tmp_path / "acme/acme_fix/wallonia/2026-09.json"
    row.parent.mkdir(parents=True)
    row.write_text(json.dumps({"_sources": []}))
    ac._write_coverage(tmp_path)
    assert sorted(p.name for p in (tmp_path / "coverage").iterdir()) == ["acme.md"]
    assert (
        "| acme_fix | wallonia | json |" in (tmp_path / "coverage/acme.md").read_text()
    )


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
