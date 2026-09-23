"""scripts/archive_cards.py: the daily writer behind the repository's card archive."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import types
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import ANY

import aiohttp
import pytest

from custom_components.be_electricity_prices.const import SUPPLIER_CUSTOM
from custom_components.be_electricity_prices.providers import _pdf
from custom_components.be_electricity_prices.providers._pdf import fetch_text
from custom_components.be_electricity_prices.providers.base import (
    CardNotReadableError,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers._rates import (
    Contract,
    FixedRates,
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


# Written out rather than read off _READERS, which is what is being checked:
# the two text readers the extractors call, and the OCR engine the archiver
# reads Ecofix's page-image cards with (ocr_price_cards), with the page
# rasterizer and the array library it reads them through.
@pytest.mark.parametrize(
    "reader", ["pypdf", "pdfplumber", "ocr-price-cards", "pypdfium2", "numpy"]
)
def test_the_parser_digest_moves_with_the_reader_versions(
    monkeypatch: pytest.MonkeyPatch, reader: str
) -> None:
    """A stored text is served to every later replay and to the live check
    for as long as the card's bytes stand, so a pypdf or pdfplumber release
    that lays a card out differently reached neither until someone dispatched
    a re-render by hand: the digest hashed the parser sources only. A reader
    bump has to move it, the OCR engine's included, or an OCR release leaves
    every Ecofix reading as it was."""
    import importlib.metadata

    real = importlib.metadata.version
    before = ac._parser_digest()

    def _bumped(name: str) -> str:
        return "99.0.0" if name == reader else real(name)

    monkeypatch.setattr(importlib.metadata, "version", _bumped)
    assert ac._parser_digest() != before
    monkeypatch.setattr(importlib.metadata, "version", real)
    assert ac._parser_digest() == before


def test_the_parser_digest_covers_the_codec_the_rows_are_written_with() -> None:
    """The digest named snapshot_store.py, which held the codec until the
    module split moved it to snapshot_codec.py: a change to how rows are
    written, or a schema bump on its own, stopped starting the replay that
    rewrites them, and an edit to the runtime cache replayed every row for
    nothing. Held to the definitions rather than to a file name, so the next
    move is caught too."""
    from custom_components.be_electricity_prices import snapshot_codec

    package = Path(snapshot_codec.__file__).parent
    hashed = "".join(
        path.read_text(encoding="utf-8")
        for pattern in ac._PARSER_SOURCES
        for path in package.glob(pattern)
    )
    assert "_SNAPSHOT_SCHEMA_VERSION = " in hashed
    assert "def _snapshot_to_dict(" in hashed


def test_a_newly_listed_reader_is_recorded_rather_than_rendered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OCR engine joined the stamped readers. Comparing the whole line
    would have called that a reader moving and rendered every kept card again
    with nothing changed, so readers are compared one by one and one the
    stamp never named only starts being recorded."""
    (tmp_path / "parser.txt").write_text(
        "digest\npypdf==6.18.0 pdfplumber==0.11.9\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        ac,
        "_readers_line",
        lambda: "pypdf==6.18.0 pdfplumber==0.11.9 ocr-price-cards==0.4.0+abc",
    )
    assert not ac.rerender_due(tmp_path)
    monkeypatch.setattr(
        ac,
        "_readers_line",
        lambda: "pypdf==6.18.0 pdfplumber==0.12.0 ocr-price-cards==0.4.0+abc",
    )
    assert ac.rerender_due(tmp_path)
    assert ac._reader_version("no-such-reader-installed") == "absent"


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
    card = json.loads(
        (tmp_path / "cards/acme/acme_fix/wallonia/2026-08.json").read_text()
    )
    assert card["publication_label"] == "augustus 2026"
    assert card["_seen_on"] == "2026-09-11"
    assert card["energy"]["single"] == 0.2
    [source] = card["_sources"]
    assert source == {"url": CARD_URL, "variant": "text", "text": ANY}
    assert source["text"].startswith("texts/2026-09/")
    assert (tmp_path / source["text"]).read_text() == "card text"


async def test_an_unreadable_label_files_under_the_month_seen(tmp_path: Path) -> None:
    await ac.archive(
        tmp_path,
        extractors=[_extractor(_card_fetch("carte tarifaire"))],
        now=NOW,
        sleep=_no_sleep,
    )
    assert (tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json").exists()


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
        json.loads((tmp_path / f"cards/acme/{c}/wallonia/2026-09.json").read_text())[
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
    path = tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json"
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
    july = json.loads(
        (tmp_path / "cards/acme/acme_fix/wallonia/2026-07.json").read_text()
    )
    assert july["_via"] == "archive"
    assert july["energy"]["single"] == 0.21
    assert (tmp_path / "cards/acme/acme_fix/wallonia/2026-06.json").exists()
    assert not (tmp_path / "cards/acme/acme_fix/wallonia/2026-08.json").exists()
    assert not (tmp_path / "cards/acme/acme_fix/wallonia/2026-05.json").exists()
    assert not (tmp_path / "cards/beta/acme_fix/wallonia/2026-08.json").exists()
    live = json.loads(
        (tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json").read_text()
    )
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
    card = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
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
    card = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
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
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-a"
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-08.json"
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
    assert (tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json").exists()

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
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-b"
    assert seen == [date.today(), date(2026, 8, 5), date(2026, 9, 18)]

    # And the forced flag replays even when the digest matches.
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=later, reparse=True, sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed) == (2, 0)


@pytest.mark.parametrize(
    "added",
    [
        pytest.param(None, id="stamp-only"),
        # v71 wrote fixed_for_term: false into every row with a feed-in leg,
        # and the run counted 1427 rows reparsed and 0 restamped.
        pytest.param("welcome_credit_kind", id="a-field-added-at-its-default"),
    ],
)
async def test_a_schema_bump_restamps_rows_without_calling_them_reparsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, added: str | None
) -> None:
    """A bump rewrites every replayable row to stamp the running schema, and
    each of those counted as reparsed, so the one figure a parser change is
    judged by read the whole archive after every bump. A row whose parse is
    unchanged is now counted as restamped, including under a bump that adds
    a field, written at its default into a row that lacked the key."""
    session = _Session({CARD_URL: "price=0.2 month=augustus 2026"})
    extractor = _extractor(_text_fetch(session, {"version": "plain"}, []))
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    august = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-08.json"
    stored = json.loads(row.read_text())
    stored["_schema_version"] -= 1
    if added is not None:
        del stored[added]
    row.write_text(json.dumps(stored), encoding="utf-8")

    # September: the live walk files September's card, so August is left to
    # the replay alone.
    session.pages[CARD_URL] = "price=0.2 month=september 2026"
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=NOW.replace(day=18), sleep=_no_sleep
    )
    assert (summary.replayed, summary.reparsed, summary.restamped) == (2, 0, 1)
    assert (
        json.loads(row.read_text())["_schema_version"] == stored["_schema_version"] + 1
    )


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
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-08.json"
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
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-c"


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
    card = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
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
    row = out / "cards/acme/acme_fix/wallonia/2026-09.json"
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


async def test_a_reader_that_moved_renders_the_kept_cards_by_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reader versions moved the digest, but a moved digest replayed the
    rows off the texts the OLD reader produced, so a pypdf or pdfplumber bump
    never ran the new reader on a card the archive held. The versions are
    stamped beside the digest and a run that finds them changed renders every
    kept card afresh, as --rerender does; the workflow reads the same answer."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    renders: list[bytes] = []
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    monkeypatch.setattr(ac, "_readers_line", lambda: "pypdf==6.18.0 pdfplumber==0.11.9")
    await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, renders))],
        pdf_dir=pdfs,
        now=NOW.replace(day=5),
        sleep=_no_sleep,
    )
    assert renders == [b"%PDF v1"]
    assert not ac.rerender_due(out)
    monkeypatch.setattr(ac, "_readers_line", lambda: "pypdf==7.0.0 pdfplumber==0.11.9")
    assert ac.rerender_due(out)
    session.pdfs.clear()  # the supplier is gone; only the kept copy is left
    summary = await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, renders))],
        pdf_dir=pdfs,
        now=NOW.replace(day=6),
        sleep=_no_sleep,
    )
    assert (summary.replayed, summary.unreplayable) == (1, [])
    assert renders == [b"%PDF v1", b"%PDF v1"]
    assert not ac.rerender_due(out)
    assert (out / "parser.txt").read_text().splitlines() == [
        "digest-a",
        "pypdf==7.0.0 pdfplumber==0.11.9",
    ]


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


async def test_a_nonce_leaves_no_text_that_no_row_names(tmp_path: Path) -> None:
    """A row is kept when only a nonce moved, but the day's copy of the page
    was stored all the same and named by nothing: 84 of the 1732 texts in the
    store were such copies. A run now removes every text no row names."""
    session = _Session({CARD_URL: "card text nonce=1"})
    extractor = _extractor(_card_fetch("september 2026", session=session))
    await ac.archive(tmp_path, extractors=[extractor], now=NOW, sleep=_no_sleep)
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json"
    named = [s["text"] for s in json.loads(row.read_text())["_sources"]]

    session.pages[CARD_URL] = "card text nonce=2"
    summary = await ac.archive(
        tmp_path, extractors=[extractor], now=NOW.replace(day=12), sleep=_no_sleep
    )
    assert summary.unchanged == 1
    assert [
        str(p.relative_to(tmp_path)) for p in tmp_path.glob("texts/*/*.txt")
    ] == named


def test_no_text_goes_while_a_row_cannot_be_read(tmp_path: Path) -> None:
    """A row that does not parse could be naming any text, so none goes."""
    text = tmp_path / "texts/2026-09/a.txt"
    text.parent.mkdir(parents=True)
    text.write_text("x")
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json"
    row.parent.mkdir(parents=True)
    row.write_text("{not json")
    assert ac._drop_unnamed_texts(tmp_path) == 0
    assert text.exists()
    row.write_text(json.dumps({"_sources": []}))
    assert ac._drop_unnamed_texts(tmp_path) == 1
    assert not (tmp_path / "texts/2026-09").exists()


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


def test_replay_answers_404_for_a_read_card_asked_under_another_folder(
    tmp_path: Path,
) -> None:
    """Brusol files each card under the month it uploaded it, so the
    extractor asks the folder of the month before delivery first. The site
    answered 404 there and the walk moved on to the card the row records;
    refused as a network error in a replay, the month could never be
    replayed. The same file under another folder gets the 404 the site gave,
    and any other URL the row never read is still refused."""
    (tmp_path / "electricity-2026-02-1").mkdir()
    (tmp_path / "electricity-2026-02-1/abc.pdf").write_bytes(b"%PDF kept")
    replay = ac._ReplaySession(None, tmp_path, None)  # type: ignore[arg-type]
    card = "EV-0226-GRS-BXL-nl.pdf"
    replay.pdfs = {f"https://www.brusol.be/sites/default/files/2026-02/{card}": "abc"}

    async def status(url: str) -> int:
        async with replay.get(url) as resp:
            return int(resp.status)

    assert (
        asyncio.run(status(f"https://www.brusol.be/sites/default/files/2026-01/{card}"))
        == 404
    )
    assert (
        asyncio.run(status(f"https://www.brusol.be/sites/default/files/2026-02/{card}"))
        == 200
    )
    with pytest.raises(aiohttp.ClientConnectionError):
        asyncio.run(
            status("https://www.brusol.be/sites/default/files/2026-01/other.pdf")
        )


@pytest.mark.parametrize(
    ("status", "error", "failed"),
    [
        (404, None, False),
        (503, None, True),
        (429, None, True),
        (None, TimeoutError(), True),
    ],
)
def test_a_kept_card_that_did_not_download_marks_the_replay(
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
    error: BaseException | None,
    failed: bool,
) -> None:
    """A kept card that does not come back for a reason unrelated to the card
    marks the replay, so the parser stamp is not moved over the rows it
    missed. A 404 says the card is not kept: retrying tomorrow finds nothing
    more, so it does not hold the stamp."""
    import live_check  # type: ignore[import-not-found]

    monkeypatch.setattr(live_check, "_RETRY_BACKOFF_S", (0.0,))
    answered = status

    class _Response:
        async def __aenter__(self) -> "_Response":
            if error is not None:
                raise error
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        @property
        def status(self) -> int:
            assert answered is not None
            return answered

        async def read(self) -> bytes:
            return b"%PDF kept"

    class _Session:
        def get(self, _url: str, **_kw: object) -> _Response:
            return _Response()

    replay = ac._ReplaySession(
        _Session(),  # type: ignore[arg-type]
        None,
        "https://cards.test/download",
        {"abc": "electricity-2026-09/abc.pdf"},
    )
    replay.pdfs = {"https://acme.test/card.pdf": "abc"}

    async def run() -> bytes:
        async with replay.get("https://acme.test/card.pdf") as resp:
            return await resp.read()

    with pytest.raises((aiohttp.ClientConnectionError, TimeoutError)):
        asyncio.run(run())
    assert replay.download_failed is failed


def test_a_kept_card_download_is_retried_before_it_holds_the_stamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-render downloads every kept card, about 1500, and one blip among
    them held the stamp and repeated the whole re-render the next day. The
    download is retried like a live fetch, so a 503 that clears on the
    second try costs nothing."""
    import live_check  # type: ignore[import-not-found]

    monkeypatch.setattr(live_check, "_RETRY_BACKOFF_S", (0.0,))
    answers = [503, 200]

    class _Response:
        def __init__(self) -> None:
            self.status = answers.pop(0)

        async def __aenter__(self) -> "_Response":
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def read(self) -> bytes:
            return b"%PDF kept"

    class _Session:
        def get(self, _url: str, **_kw: object) -> _Response:
            return _Response()

    replay = ac._ReplaySession(
        _Session(),  # type: ignore[arg-type]
        None,
        "https://cards.test/download",
        {"abc": "electricity-2026-09/abc.pdf"},
    )
    replay.pdfs = {"https://acme.test/card.pdf": "abc"}

    async def run() -> bytes:
        async with replay.get("https://acme.test/card.pdf") as resp:
            return await resp.read()

    assert asyncio.run(run()) == b"%PDF kept"
    assert not answers
    assert replay.download_failed is False


async def test_the_parser_stamp_waits_for_a_replay_that_could_not_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every OCTA+ row stores its card folded to a reference, so replaying it
    downloads the kept copy. When that download failed the row was reported
    and left as it was, the new digest was stamped anyway, and the next run
    replayed nothing: the rows a parser fix existed to heal stayed wrong
    behind a green run, and the integration reads a closed month from here
    first. The stamp now moves only once a replay has had its cards."""
    session = _Session({CARD_URL: "price=0.2 month=augustus 2026"})
    extractor = _extractor(_text_fetch(session, {"version": "plain"}, []))
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    august = datetime(2026, 8, 5, 6, 0, tzinfo=UTC)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-a"

    replay_all = ac._replay_all

    async def _failing(*args: Any, **kwargs: Any) -> None:
        await replay_all(*args, **kwargs)
        args[3].download_failed = True

    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    monkeypatch.setattr(ac, "_replay_all", _failing)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-a"

    # The next run gets its cards, and only then is the parser recorded.
    monkeypatch.setattr(ac, "_replay_all", replay_all)
    await ac.archive(tmp_path, extractors=[extractor], now=august, sleep=_no_sleep)
    assert (tmp_path / "parser.txt").read_text().splitlines()[0] == "digest-b"


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


@contextmanager
def _ocr_engine(read_pdf: object) -> Iterator[None]:
    """Stand a fake ``ocr_price_cards`` in front of the archiver's import.

    The real engine needs Python 3.14 and is installed by the card-archive
    workflow alone; what these tests are about is what the archiver does
    with its answer.
    """
    module = types.ModuleType("ocr_price_cards")
    module.read_pdf = read_pdf  # type: ignore[attr-defined]
    sys.modules["ocr_price_cards"] = module
    try:
        yield
    finally:
        del sys.modules["ocr_price_cards"]


def _page_image_fetch(session: _PdfSession) -> Fetch:
    """A supplier whose card carries no text layer, the way Ecofix's does."""

    def render(_payload: bytes) -> str:
        raise CardNotReadableError("card has no text layer: 172 characters")

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        # The canned session while it holds the card; once the test empties
        # it, the session handed in, which in a retry is the kept copy.
        reader = session if PDF_URL in session.pdfs else _session
        text = await _pdf._pdf_text(
            reader,  # type: ignore[arg-type]
            PDF_URL,
            variant="plain",
            timeout=5,
            render=render,
        )
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=0.2 if "11,81" in text else 0.0),
            publication_label="september 2026",
            source_url=PDF_URL,
        )

    return fetch


async def test_a_page_image_card_is_read_by_ocr_and_the_row_says_so(
    tmp_path: Path,
) -> None:
    """The last resort. The card downloaded, carries no text layer and no
    parser can read it; the engine reads one off its pixels, the supplier's
    own extractor parses that, and the row records that it was read that
    way so an installation reading the row can tell its user."""
    out = tmp_path / "out"
    session = _PdfSession({PDF_URL: b"%PDF page images"})
    read = "Maandprijs: 11,81 11,81 11,81 11,81\n" * 40
    with _ocr_engine(lambda payload, strict: SimpleNamespace(trusted_text=read)):
        summary = await ac.archive(
            out,
            extractors=[_extractor(_page_image_fetch(session))],
            now=NOW,
            sleep=_no_sleep,
        )
    assert summary.stored == 1
    assert summary.failed == []
    row = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
    assert any(source.get("ocr") for source in row["_sources"])
    assert row["energy"]["single"] == 0.2
    # Nothing is left owing an explanation: the card parsed.
    assert not (out / "unparsed.json").exists()


async def test_the_ocr_mark_survives_a_text_served_from_the_archive(
    tmp_path: Path,
) -> None:
    """A card whose bytes have not changed is served its stored text without
    the reader running at all, and the reader is what discovers that the card
    has no text layer. So the fact lasted exactly one day: every row written
    after the first said the card had been read normally, and an installation
    reading those rows never told its user the prices came off pixels.

    Measured on the live archive before this: not one of 1.686 rows carried the
    mark, including four Ecofix months that are read off page images.
    """
    out = tmp_path / "out"
    session = _PdfSession({PDF_URL: b"%PDF page images"})
    read = "Maandprijs: 11,81 11,81 11,81 11,81\n" * 40

    # Day one: the reader refuses, the engine reads the pixels, the row says so.
    with _ocr_engine(lambda payload, strict: SimpleNamespace(trusted_text=read)):
        await ac.archive(
            out,
            extractors=[_extractor(_page_image_fetch(session))],
            now=NOW,
            sleep=_no_sleep,
        )
    row = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
    assert any(source.get("ocr") for source in row["_sources"]), (
        "the fact belongs beside the text, or the next run cannot learn it"
    )

    # Day two: same bytes, so the stored text is served and no OCR runs. The
    # engine is not even installed, which is what proves it was not asked.
    await ac.archive(
        out,
        extractors=[_extractor(_page_image_fetch(session))],
        now=NOW,
        sleep=_no_sleep,
    )
    row = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
    assert any(source.get("ocr") for source in row["_sources"]), (
        "a served text must not lose what the card is"
    )


async def test_a_row_read_from_the_card_itself_carries_no_ocr_mark(
    tmp_path: Path,
) -> None:
    """The key is written only when true, so every row already on the branch
    stays byte-identical and a day that changed nothing still commits
    nothing."""
    out = tmp_path / "out"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, []))],
        now=NOW,
        sleep=_no_sleep,
    )
    row = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
    assert not any(source.get("ocr") for source in row["_sources"])


async def test_an_ocr_engine_move_reads_only_the_page_image_cards_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine is installed from its main branch, so any commit there moves
    its version, and a moved reader rendered every kept card afresh: about
    1500 downloads and half an hour, where only the cards read off their
    pixels can come out differently. Those are read again; a card with a text
    layer keeps its stored text, and the workflow keeps its ordinary budget."""
    read = "Maandprijs: 11,81 11,81 11,81 11,81\n" * 40
    engine: list[bytes] = []

    def read_pdf(payload: bytes, strict: bool) -> SimpleNamespace:
        engine.append(payload)
        return SimpleNamespace(trusted_text=read)

    def readers(ocr: str) -> None:
        monkeypatch.setattr(
            ac,
            "_readers_line",
            lambda: f"pypdf==6.18.0 pdfplumber==0.11.9 ocr-price-cards==0.4.0+{ocr}",
        )

    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    readers("aaa")
    text_out, image_out = tmp_path / "text", tmp_path / "image"
    renders: list[bytes] = []
    text_session = _PdfSession({PDF_URL: b"%PDF v1"})
    image_session = _PdfSession({PDF_URL: b"%PDF page images"})
    with _ocr_engine(read_pdf):
        await ac.archive(
            text_out,
            extractors=[_extractor(_pdf_fetch(text_session, renders))],
            pdf_dir=tmp_path / "text-pdfs",
            now=NOW.replace(day=5),
            sleep=_no_sleep,
        )
        await ac.archive(
            image_out,
            extractors=[_extractor(_page_image_fetch(image_session))],
            pdf_dir=tmp_path / "image-pdfs",
            now=NOW.replace(day=5),
            sleep=_no_sleep,
        )
        assert (len(renders), len(engine)) == (1, 1)

        readers("bbb")
        monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
        assert not ac.rerender_due(text_out)
        await ac.archive(
            text_out,
            extractors=[_extractor(_pdf_fetch(text_session, renders))],
            pdf_dir=tmp_path / "text-pdfs",
            now=NOW.replace(day=6),
            sleep=_no_sleep,
        )
        # Only the kept copy is left, so the second reading is the replay's.
        image_session.pdfs.clear()
        summary = await ac.archive(
            image_out,
            extractors=[_extractor(_page_image_fetch(image_session))],
            pdf_dir=tmp_path / "image-pdfs",
            now=NOW.replace(day=6),
            sleep=_no_sleep,
        )
    assert summary.replayed == 1
    assert len(renders) == 1
    assert len(engine) == 2
    row = json.loads(
        (image_out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text()
    )
    assert any(source.get("ocr") for source in row["_sources"])


async def test_without_the_engine_a_page_image_card_is_refused_as_before(
    tmp_path: Path,
) -> None:
    """The engine is installed by one workflow and nowhere else. Without it
    the card fails exactly as it did, and is named on the sheet instead."""
    out = tmp_path / "out"
    session = _PdfSession({PDF_URL: b"%PDF page images"})
    assert "ocr_price_cards" not in sys.modules
    summary = await ac.archive(
        out,
        extractors=[_extractor(_page_image_fetch(session))],
        pdf_dir=tmp_path / "pdfs",
        now=NOW,
        sleep=_no_sleep,
    )
    assert summary.stored == 0
    assert len(summary.failed) == 1
    assert not (out / "cards/acme/acme_fix/wallonia/2026-09.json").exists()
    assert "acme/acme_fix/wallonia/2026-09" in json.loads(
        (out / "unparsed.json").read_text()
    )


async def test_a_reading_too_thin_to_price_on_is_refused(tmp_path: Path) -> None:
    """``trusted_text`` drops every line carrying a mark the engine refused,
    so a bad reading comes back short rather than wrong. Too short to price
    on is held to the same floor a text layer is, and the card is named on
    the sheet rather than written as a row of silent misses."""
    out = tmp_path / "out"
    session = _PdfSession({PDF_URL: b"%PDF page images"})
    with _ocr_engine(
        lambda payload, strict: SimpleNamespace(trusted_text="Maandprijs:")
    ):
        summary = await ac.archive(
            out,
            extractors=[_extractor(_page_image_fetch(session))],
            pdf_dir=tmp_path / "pdfs",
            now=NOW,
            sleep=_no_sleep,
        )
    assert summary.stored == 0
    assert "too little of a card to price on" in summary.failed[0]
    assert "acme/acme_fix/wallonia/2026-09" in json.loads(
        (out / "unparsed.json").read_text()
    )


async def test_a_card_nobody_could_read_is_tried_again_when_the_reader_changes(
    tmp_path: Path,
) -> None:
    """A card no reader could read cannot be fetched again: the supplier
    serves one url and overwrites it next month, so the kept bytes are the
    only copy there will ever be. The one thing that can change the answer
    is the reader, which is what parser.txt already tracks, so the retry
    runs on the same trigger the row replay does."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    base = "https://cards.test/releases/download"
    session = _PdfSession({PDF_URL: b"%PDF page images"})
    digest = hashlib.sha256(b"%PDF page images").hexdigest()

    # Day one: no engine, so the card is kept and named, and there is no row.
    await ac.archive(
        out,
        extractors=[_extractor(_page_image_fetch(session))],
        pdf_dir=pdfs,
        pdf_base_url=base,
        now=NOW,
        sleep=_no_sleep,
    )
    assert not (out / "cards/acme/acme_fix/wallonia/2026-09.json").exists()
    assert "acme/acme_fix/wallonia/2026-09" in json.loads(
        (out / "unparsed.json").read_text()
    )
    # The upload step records where the bytes landed; that is what a retry
    # fetches them back from.
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )

    # Day two: the reader learnt to read it. The card is not re-fetched
    # (the session is empty); it is read back from the kept copy.
    read = "Maandprijs: 11,81 11,81 11,81 11,81\n" * 40
    with _ocr_engine(lambda payload, strict: SimpleNamespace(trusted_text=read)):
        summary = await ac.archive(
            out,
            extractors=[_extractor(_page_image_fetch(_PdfSession({})))],
            pdf_dir=pdfs,
            pdf_base_url=base,
            reparse=True,
            now=NOW,
            sleep=_no_sleep,
        )
    assert summary.reparsed == 1
    row = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
    assert any(source.get("ocr") for source in row["_sources"])
    # Nothing left owing an explanation: the month has a row now.
    assert not (out / "unparsed.json").exists()


async def test_a_card_that_would_not_parse_is_still_named_on_the_sheet(
    tmp_path: Path,
) -> None:
    """Ecofix publishes page images some months and no reader can read them.
    The bytes are kept and uploaded like any other card, so the sheet has to
    say which card they are: a cell with the PDF and no JSON. The entry
    leaves by itself once a month parses."""
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    base = "https://cards.test/releases/download"
    session = _PdfSession({PDF_URL: b"%PDF page images"})

    def render(payload: bytes) -> str:
        return payload.decode()

    async def unreadable(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        await _pdf._pdf_text(
            session,  # type: ignore[arg-type]
            PDF_URL,
            variant="plain",
            timeout=5,
            render=render,
        )
        raise ExtractorError("the card is a page image")

    await ac.archive(
        out,
        extractors=[_extractor(unreadable)],
        pdf_dir=pdfs,
        pdf_base_url=base,
        now=NOW,
        sleep=_no_sleep,
    )
    digest = hashlib.sha256(b"%PDF page images").hexdigest()
    assert json.loads((out / "unparsed.json").read_text()) == {
        "acme/acme_fix/wallonia/2026-09": [{"url": PDF_URL, "pdf": digest}]
    }
    # The upload step records where it landed; the sheet then links to it.
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )
    ac._write_listings(out, base, None)
    url = f"{base}/electricity-2026-09/{digest}.pdf"
    coverage = (out / "coverage/acme.md").read_text()
    assert f"| acme_fix | wallonia | [pdf]({url}) (not parsed) |" in coverage
    assert (
        "- [acme](coverage/acme.md): 1 rows, 2026-09 to 2026-09"
        in (out / "coverage.md").read_text()
    )

    # A later run whose card parses leaves nothing behind to explain.
    session.pdfs[PDF_URL] = b"%PDF v1"
    await ac.archive(
        out,
        extractors=[_extractor(_pdf_fetch(session, []))],
        pdf_dir=pdfs,
        pdf_base_url=base,
        now=NOW,
        sleep=_no_sleep,
    )
    assert not (out / "unparsed.json").exists()
    # The legend still explains the mark; no cell carries it any more.
    assert "(not parsed) |" not in (out / "coverage/acme.md").read_text()


async def test_a_card_inside_a_document_is_stored_once_not_twice(
    tmp_path: Path,
) -> None:
    """OCTA+'s archive answers with the card base64'd inside JSON. The bytes
    are kept as a release asset like any other card, so storing the base64 as
    well is the same PDF a second time, a third larger for the encoding: 150
    such texts held 80,9 MB of the branch's 111,5. The envelope is stored, the
    payload is a reference, and a replay puts the kept copy back."""
    import base64

    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    card = b"%PDF inside json" + b"\0padding" * 200
    envelope = (
        '{"Response":{"Ok":"True","TariffSheet":"data:application/pdf;base64,'
        + base64.b64encode(card).decode("ascii")
        + '"}}'
    )
    api = "https://acme.test/api/card"
    session = _Session({api: envelope})

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        body = await fetch_text(session, api)  # type: ignore[arg-type]
        payload = base64.b64decode(body.split("base64,")[1].rstrip('"}'))
        text = await _pdf.render_pdf(
            "plain", api, payload, lambda b: b.decode("latin-1")
        )
        return make_snapshot(
            supplier="acme",
            contract=contract,
            energy=FixedRates(single=0.2 if "%PDF" in text else 0.0),
            publication_label="september 2026",
        )

    await ac.archive(
        out, extractors=[_extractor(fetch)], pdf_dir=pdfs, now=NOW, sleep=_no_sleep
    )
    digest = hashlib.sha256(card).hexdigest()
    # Two texts are stored for this row: the render of the card, and the
    # envelope it arrived in. The envelope is the one under test.
    body = next(
        t.read_text()
        for t in (out / "texts").rglob("*.txt")
        if "TariffSheet" in t.read_text()
    )
    assert f"{{{{card:{digest}}}}}" in body, "the payload should be a reference"
    assert base64.b64encode(card).decode("ascii") not in body
    assert len(body) < len(envelope), "the stored text must be the smaller one"
    # The card itself is kept, which is what makes the reference resolvable.
    assert (pdfs / f"electricity-2026-09/{digest}.pdf").read_bytes() == card


async def test_a_folded_card_is_put_back_when_the_row_is_replayed(
    tmp_path: Path,
) -> None:
    """Folding is only safe because the unfolding works: a replay reads the
    envelope back, puts the kept card into it, and the parse sees exactly
    what it saw the first time -- without the supplier being reachable."""
    import base64

    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    card = b"%PDF inside json" + b"\0padding" * 200
    envelope = (
        '{"TariffSheet":"data:application/pdf;base64,'
        + base64.b64encode(card).decode("ascii")
        + '"}'
    )
    api = "https://acme.test/api/card"
    session = _Session({api: envelope})
    seen: list[str] = []

    async def fetch(_session: Any, contract: str, region: str) -> SupplierSnapshot:
        body = await fetch_text(session, api)  # type: ignore[arg-type]
        seen.append(body)
        payload = base64.b64decode(body.split("base64,")[1].rstrip('"}'))
        await _pdf.render_pdf("plain", api, payload, lambda b: b.decode("latin-1"))
        return make_snapshot(
            supplier="acme", contract=contract, publication_label="september 2026"
        )

    await ac.archive(
        out, extractors=[_extractor(fetch)], pdf_dir=pdfs, now=NOW, sleep=_no_sleep
    )
    first = seen[-1]
    # The upload step records where the card landed, which is where a replay
    # fetches it back from.
    digest = hashlib.sha256(card).hexdigest()
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )

    session.pages.clear()  # the supplier is gone; only the kept copy remains
    summary = await ac.archive(
        out,
        extractors=[_extractor(fetch)],
        pdf_dir=pdfs,
        reparse=True,
        now=NOW,
        sleep=_no_sleep,
    )
    assert summary.unreplayable == []
    assert summary.replayed == 1
    assert seen[-1] == first, "the replayed parse must read the same bytes"


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
    card = json.loads((out / "cards/acme/acme_fix/wallonia/2026-09.json").read_text())
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
    row = json.loads(
        (tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json").read_text()
    )
    text = row["_sources"][0]["text"]
    assert (
        f"| acme_fix | wallonia | [page]({branch}/{text})"
        f" [json]({branch}/cards/acme/acme_fix/wallonia/2026-09.json) |"
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
    row = out / "cards/acme/acme_fix/wallonia/2026-07.json"
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
    without fetching anything and drops the PDF index earlier versions
    wrote."""
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
        f"| a | wallonia | pdf [json]({branch}/cards/acme/a/wallonia/2026-09.json) |"
        in coverage
    )
    # The upload step recorded it; the index-only pass links everything up.
    (out / "pdfs.json").write_text(
        json.dumps({digest: f"electricity-2026-09/{digest}.pdf"})
    )
    (out / "pdfs.md").write_text("stale index")
    ac._write_listings(out, base, branch)
    url = f"{base}/electricity-2026-09/{digest}.pdf"
    coverage = (out / "coverage/acme.md").read_text()
    assert (
        f"| a | wallonia | [pdf]({url}) [json]({branch}/cards/acme/a/wallonia/2026-09.json) |"
        in coverage
    )
    assert (
        f"| b | wallonia | [pdf]({url}) [json]({branch}/cards/acme/b/wallonia/2026-09.json) |"
        in coverage
    )
    assert not (out / "pdfs.md").exists()


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
    assert not (tmp_path / "pdfs.md").exists()


def test_a_supplier_that_left_the_branch_loses_its_sheet(tmp_path: Path) -> None:
    (tmp_path / "coverage").mkdir()
    (tmp_path / "coverage/gone.md").write_text("stale sheet")
    row = tmp_path / "cards/acme/acme_fix/wallonia/2026-09.json"
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
        card = tmp_path / "cards/acme/acme_fix/wallonia" / f"{month}.json"
        card.parent.mkdir(parents=True, exist_ok=True)
        card.write_text("{}")
        text = tmp_path / "texts" / month / "abc.txt"
        text.parent.mkdir(parents=True, exist_ok=True)
        text.write_text("x")
    old = tmp_path / "cards/old/old_fix/wallonia/2023-01.json"
    old.parent.mkdir(parents=True)
    old.write_text("{}")
    assert ac._prune(tmp_path, 36, date(2026, 9, 11)) == 3
    assert not (tmp_path / "old").exists()
    assert not (tmp_path / "cards/acme/acme_fix/wallonia/2023-08.json").exists()
    assert (tmp_path / "cards/acme/acme_fix/wallonia/2023-09.json").exists()
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


async def test_the_next_months_text_never_shadows_a_rows_own_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay seeds the FOLLOWING month's sources so a settlement that
    reads that card can resolve offline (EBEM's index, Trevion's two). It must
    seed them behind the row's own, never over them.

    The memo is keyed by URL, and several suppliers publish every month at one
    unchanging "current" address. Seeding the follower first replayed Ecofix's
    six August 2026 rows as September cards, label, validity and price alike.
    Here both months are served from one URL with different content, and
    August must come back as August.
    """
    out, pdfs = tmp_path / "out", tmp_path / "pdfs"
    session = _PdfSession({PDF_URL: b"%PDF v1"})
    renders: list[bytes] = []

    async def fetch_for_month(
        _session: Any, contract: str, region: str, month: date
    ) -> SupplierSnapshot | None:
        if month.month != 8:
            return None
        return await _pdf_fetch(session, renders)(_session, contract, region)

    live = _pdf_fetch(session, renders)
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
        fetch=live,
        fetch_for_month=fetch_for_month,
    )
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-a")
    # August is captured off the "v1" card, then September replaces it at the
    # same URL, which is what a "current" address does.
    await ac.archive(
        tmp_path / "out",
        extractors=[extractor],
        pdf_dir=pdfs,
        backfill_months=1,
        now=NOW,
        sleep=_no_sleep,
    )
    august = out / "cards/acme/acme_fix/wallonia/2026-08.json"
    before = json.loads(august.read_text())
    session.pdfs[PDF_URL] = b"%PDF v2"
    monkeypatch.setattr(ac, "_parser_digest", lambda: "digest-b")
    await ac.archive(
        out, extractors=[extractor], pdf_dir=pdfs, now=NOW, sleep=_no_sleep
    )
    after = json.loads(august.read_text())
    assert after["energy"] == before["energy"], "August replayed as another month"
    assert after["publication_label"] == before["publication_label"]
