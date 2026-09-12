#!/usr/bin/env python3
"""Store today's tariff cards in the repository's card archive.

Walks every registered (supplier, contract, region), fetches the card the
integration would price on right now and writes what it parsed to
``<out>/<supplier>/<contract>/<region>/<YYYY-MM>.json``, with every text
the parse read under ``<out>/texts/<YYYY-MM>/<sha256>.txt``. A card is
filed under the month its own publication label names, so a supplier
publishing in arrears (Ecopower's definitive card lands at the end of the
month it covers) or ahead of time is filed where its card says; only a
card whose label cannot be read is filed under the month it was seen in.

Run daily by .github/workflows/archive_cards.yml against the ``archive``
branch, which the integration reads for any month a supplier's own archive
cannot serve (``snapshot_store._archived_card_from_github``). A month
already on disk is rewritten only when the parse changed, so a quiet day
leaves nothing to commit, and months older than ``--keep-months`` are
removed on every run.

The cards themselves are kept too. With ``--pdfs DIR`` every PDF whose
bytes the branch has not recorded yet is written to
``DIR/cards-<YYYY-MM>/<sha256>.pdf``, and the workflow uploads that
directory as release assets of a separate cards repository (releases
named by the capture month, at most a thousand files each, since a month
of cards is about 100 MB and three years of them no git branch can hold),
then records where each one landed in ``<out>/pdfs.json``. A card's
``_sources`` entry names its PDF by digest alone; the manifest is the one
place that says where it lives. The same digest is what keeps a daily run
cheap: a card whose bytes have not changed is served the text the branch
already holds for it instead of being rendered again.

A parser fix reaches the stored months on its own. Every row carries the
texts its parse read, so the run replays each row through the current
extractor with those texts served from the branch, the clock pinned to
the day the row was captured and no supplier contacted, and rewrites the
row when the parse came out differently. That replay costs a regex pass
per row and nothing else, and it happens only when the parser sources
changed since the branch was last replayed (a digest of them is stamped in
``parser.txt``), so a day without a code change replays nothing;
``--reparse`` forces it. A parser that now reads a card with a different
PDF reader finds no stored text for that reading and gets the kept PDF
back from the cards releases instead; ``--rerender`` asks for that on
every card, which is the way to pick up a reader upgrade.

``--backfill N`` also asks every supplier that keeps an archive of its own
for the N closed months before this one, through the same
``fetch_for_month`` the integration uses, and stores each month the branch
does not hold yet. That makes the branch a mirror of those archives:
insurance against a supplier dropping its archive (DATS 24 did) and a
cheap read for any month a supplier's own path cannot serve. A month the
supplier answers None for is left absent, as is a card still flagged
provisional, so a later backfill fills it once it has settled.

Exits 0 when at least one card was stored or confirmed unchanged and 1 when
none was: that is a runner-wide problem rather than a supplier's, so the
run goes red without filing anything. The live check already files
per-supplier issues and this is not a second checker.

Usage:
    python scripts/archive_cards.py --out tmp/archive --pdfs tmp/pdfs [--only mega ...]
        [--backfill 12] [--reparse] [--rerender]
        [--pdf-base-url https://github.com/<owner>/<cards repo>/releases/download]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import aiohttp
from freezegun import freeze_time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from custom_components.be_electricity_prices.const import (  # noqa: E402
    SUPPLIER_CUSTOM,
)
from custom_components.be_electricity_prices.providers import (  # noqa: E402
    all_extractors,
)
from custom_components.be_electricity_prices.providers._pdf import (  # noqa: E402
    is_transient_fetch_error,
    memoise_text_fetches,
    render_through,
)
from custom_components.be_electricity_prices.providers.base import (  # noqa: E402
    SupplierExtractor,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.snapshot_store import (  # noqa: E402
    _snapshot_to_dict,
)
from homeassistant.helpers.json import json_dumps  # noqa: E402

# scripts/ is not a package; the line above puts it on sys.path so the card
# month is read by the same function the live check's freshness gate uses,
# and the render cache is the one the live check reads too.
from card_texts import StoredTexts, digest_of, read_text  # type: ignore[import-not-found]  # noqa: E402
from live_check import label_month  # type: ignore[import-not-found]  # noqa: E402

_T = TypeVar("_T")
_BRUSSELS = ZoneInfo("Europe/Brussels")
_ATTEMPTS = 3
_RETRY_BACKOFF_S = (10, 30)
# A supplier whose cards fail on the network this many times in a row is
# not answering this runner today (Mega has blocked the runner range
# before), and every further card would cost the same three timeouts and
# two sleeps, a couple of minutes each: sixty Mega cards would run the job
# into its timeout with nothing committed. Skip the rest of the supplier
# and say so; tomorrow is another run.
_GIVE_UP_AFTER = 3
# One card, fetch and parse together. The PDF helpers cap each download on
# their own; this bounds a parse that never returns so the rest of the
# registry is still archived and the summary still prints.
_CARD_TIMEOUT_S = 300
# Keys the daily run rewrites; two files that differ only here hold the
# same card and the older one is kept.
_VOLATILE_KEYS = ("_cached_at", "_seen_on")
# The digest of the parser sources the branch was last replayed with.
_PARSER_STAMP = "parser.txt"
# One table per supplier of which months the branch holds, for the reader
# who wants to know whether a given month of a given contract is covered
# without listing directories, each month linking to the PDF it was
# parsed from; and the kept PDFs the other way round, each with the cards
# it was read for, since a release lists them by digest only.
_COVERAGE = "coverage.md"
_PDF_INDEX = "pdfs.md"
_VIA_LABELS = {"live": "live", "archive": "mirror"}
# What a parse depends on: the extractors, the shared readers and rate
# dataclasses beside them, the constants they key on, and the codec the
# rows are written with.
_PARSER_SOURCES = ("providers/*.py", "const.py", "snapshot_store.py")

_README = """# Tariff card archive

Written daily by `.github/workflows/archive_cards.yml` running
`scripts/archive_cards.py` from the main branch. Not edited by hand.

- `<supplier>/<contract>/<region>/<YYYY-MM>.json`: the card as the
  integration parsed it, filed under the month the card names, or under
  the month it was seen in when it names none.
- `texts/<YYYY-MM>/<sha256>.txt`: every document text a parse read that
  month, stored once and shared between the cards that read it. Each card
  lists its own under `_sources`, and names the PDF it read by SHA-256.
- `pdfs.json`: where each PDF is kept, as `<release tag>/<sha256>.pdf` in
  the cards repository's releases.
- `coverage.md`: which months the branch holds for each contract and
  region, whether each was captured live or mirrored from the supplier's
  archive, and a link from each month to the PDF it was parsed from.
- `pdfs.md`: every kept PDF, by release, with each card it was read for.

To get the original card of a contract and month: open `coverage.md`, find
the row, click the month. Months older than three years are removed.
"""


class _RecordingMemo(dict[str, str]):
    """The text memo the fetch helpers consult, noting what one fetch touched.

    One memo spans the whole run so a listing page, or a card two products
    share, is downloaded and parsed once. Attribution is still per card,
    and a shared dict cannot say which entries a given parse read, so every
    read and write lands in ``touched`` and the caller clears it before
    each fetch. The helpers test membership and then index, so a hit is a
    read here and a miss is a write.
    """

    def __init__(self) -> None:
        super().__init__()
        self.touched: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.touched.add(key)
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: str) -> None:
        self.touched.add(key)
        super().__setitem__(key, value)


@dataclass
class _Summary:
    stored: int = 0
    unchanged: int = 0
    backfilled: int = 0
    absent: int = 0
    rendered: int = 0
    unrendered: int = 0
    pdfs_saved: int = 0
    replayed: int = 0
    reparsed: int = 0
    unreplayable: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    given_up: list[str] = field(default_factory=list)


class _Patience:
    """Per-supplier count of network failures in a row, and the verdict."""

    def __init__(self) -> None:
        self.failures: dict[str, int] = {}
        self.given_up: set[str] = set()

    def note(self, supplier: str, err: BaseException) -> bool:
        """Record one failed card; True when the supplier is now given up on."""
        if not _transient(err):
            self.failures[supplier] = 0
            return False
        self.failures[supplier] = self.failures.get(supplier, 0) + 1
        if self.failures[supplier] >= _GIVE_UP_AFTER:
            self.given_up.add(supplier)
            return True
        return False

    def ok(self, supplier: str) -> None:
        self.failures[supplier] = 0


_MANIFEST = "pdfs.json"


class _Cards(StoredTexts):
    """What the run knows about card bytes, plus where they are kept.

    The render cache is the shared one (``card_texts.StoredTexts``): a
    downloaded card whose bytes the branch has already seen is served the
    stored text instead of being rendered again, which is what makes a
    daily walk over 250 cards cheap. On top of it, ``pdfs.json`` says which
    digests are already uploaded, and bytes the branch has not recorded yet
    are written under ``pdf_dir`` for the workflow to upload.
    """

    def __init__(
        self,
        out: Path,
        pdf_dir: Path | None,
        seen_month: str,
        *,
        serve_texts: bool = True,
    ) -> None:
        super().__init__(out, serve=serve_texts)
        self.out = out
        self.pdf_dir = pdf_dir
        self.seen_month = seen_month
        self.kept: dict[str, str] = {}
        manifest = out / _MANIFEST
        if manifest.exists():
            self.kept = json.loads(manifest.read_text(encoding="utf-8"))
        self.saved: dict[str, str] = {}

    def keep(self, digest: str, payload: bytes) -> None:
        if self.pdf_dir is None or digest in self.kept or digest in self.saved:
            return
        rel = f"cards-{self.seen_month}/{digest}.pdf"
        path = self.pdf_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        self.saved[digest] = rel


class _KeptResponse:
    """The response shape the readers use, over bytes already in hand; a
    probe with nothing behind it answers 404.

    A kept card also answers the freshness headers a probe may ask for:
    Eneco's archive walks issue numbers with ``head_freshness_key`` and
    skips a candidate that carries neither an ETag nor a Last-Modified,
    so a bare 200 would still look like a missing card.
    """

    content_length = None

    def __init__(self, payload: bytes | None, etag: str = "") -> None:
        self._payload = payload or b""
        self.status = 200 if payload is not None else 404
        self.headers: dict[str, str] = (
            {"ETag": f'"{etag}"', "Last-Modified": "Thu, 01 Jan 2026 00:00:00 GMT"}
            if payload is not None
            else {}
        )

    async def read(self) -> bytes:
        return self._payload

    async def text(self) -> str:
        return self._payload.decode("utf-8", errors="replace")

    async def __aenter__(self) -> _KeptResponse:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _ReplaySession:
    """What a replayed parse may fetch: nothing from a supplier.

    Every text the row read is seeded into the memo before the parse runs,
    so the readers never reach this session for those. The one request
    that can still arrive is a card the parser now wants read with another
    PDF reader (a variant the row has no text for, or every PDF under
    ``--rerender``), and that is served from the kept PDF: the local copy
    first, the cards releases otherwise, with a download kept on disk for
    the sibling rows that read the same card. A HEAD answers 200 for a
    kept card and 404 for anything else, so an extractor that probes
    candidate URLs before choosing one (Eneco's archive walks issue
    numbers) lands on the card the row was parsed from. Anything else is
    refused as a network error, which the readers wrap the way they wrap
    a real one, and the row is left as it was and reported.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        pdf_dir: Path | None,
        pdf_base_url: str | None,
        kept: dict[str, str] | None = None,
    ) -> None:
        self._session = session
        self._pdf_dir = pdf_dir
        self._pdf_base_url = pdf_base_url
        # digest -> release path, the manifest; where a kept card is served from.
        self._kept = kept or {}
        # url -> digest for the row being replayed.
        self.pdfs: dict[str, str] = {}
        self._cache = Path(tempfile.mkdtemp(prefix="cards-replay-"))

    def get(self, url: str, **_kw: Any) -> Any:
        return self._get(url)

    def head(self, url: str, **_kw: Any) -> Any:
        return self._Pending(self._probe(url), self.pdfs.get(url, ""))

    async def _probe(self, url: str) -> bytes | None:
        return b"" if url in self.pdfs else None

    async def _fetch(self, url: str) -> bytes:
        digest = self.pdfs.get(url)
        if digest is None:
            raise aiohttp.ClientConnectionError(f"offline replay has nothing for {url}")
        if self._pdf_dir is not None:
            local = next(self._pdf_dir.glob(f"*/{digest}.pdf"), None)
            if local is not None:
                return local.read_bytes()
        cached = self._cache / f"{digest}.pdf"
        if cached.exists():
            return cached.read_bytes()
        path = self._kept.get(digest)
        if self._pdf_base_url is None or path is None:
            raise aiohttp.ClientConnectionError(f"no kept copy of {url} to replay from")
        async with self._session.get(
            f"{self._pdf_base_url}/{path}", timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientConnectionError(
                    f"HTTP {resp.status} fetching the kept copy of {url}"
                )
            payload = await resp.read()
        cached.write_bytes(payload)
        return payload

    class _Pending:
        """An awaitable-and-enterable stand-in for aiohttp's request context."""

        def __init__(self, fetch: Awaitable[bytes | None], etag: str = "") -> None:
            self._fetch = fetch
            self._etag = etag

        async def __aenter__(self) -> _KeptResponse:
            return _KeptResponse(await self._fetch, self._etag)

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def _get(self, url: str) -> _Pending:
        return self._Pending(self._fetch(url), self.pdfs.get(url, ""))


def _parser_digest() -> str:
    """One digest over every source a parse depends on."""
    root = ROOT / "custom_components" / "be_electricity_prices"
    digest = hashlib.sha256()
    for pattern in _PARSER_SOURCES:
        for path in sorted(root.glob(pattern)):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _month_id(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _months_before(today: date, months: int) -> str:
    """The month id ``months`` before ``today``'s month."""
    index = today.year * 12 + today.month - 1 - months
    return _month_id(index // 12, index % 12 + 1)


def _card_month(snap: SupplierSnapshot, today: date) -> str:
    """The month to file ``snap`` under: the one its label names, else today's."""
    named = label_month(snap.publication_label)
    if named is None:
        return _month_id(today.year, today.month)
    return _month_id(*named)


def _source_entry(key: str, path: str, cards: _Cards) -> dict[str, str]:
    """Describe one memo entry: the PDF helpers key a rendered document as
    ``<variant>\\0<url>`` and a plain text fetch by its URL alone. A
    rendered document also names its PDF by digest when the bytes were
    seen; ``pdfs.json`` says where that digest lives."""
    variant, sep, url = key.partition("\0")
    if not sep:
        return {"url": key, "variant": "text", "text": path}
    entry = {"url": url, "variant": variant, "text": path}
    digest = cards.digest_for(url)
    if digest is not None:
        entry["pdf"] = digest
    return entry


def _write_text(out: Path, seen_month: str, text: str) -> str:
    """Store ``text`` once, content-addressed, and return its path in ``out``."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rel = f"texts/{seen_month}/{digest}.txt"
    path = out / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    return rel


def _same_card(existing: dict[str, Any] | None, fresh: dict[str, Any]) -> bool:
    """Whether two rows hold the same card.

    The timestamps are not the card, and neither is the path of a text a
    source was read from: a listing page that carries a nonce or a render
    that is not byte-stable gives a new text file every day while the parse,
    the URL, the reader variant and the PDF are all the same. What a source
    was and what it parsed to is what counts.
    """
    if existing is None:
        return False

    def settled(card: dict[str, Any]) -> dict[str, Any]:
        out = {k: v for k, v in card.items() if k not in _VOLATILE_KEYS}
        out["_sources"] = [
            {k: v for k, v in source.items() if k != "text"}
            for source in card.get("_sources", [])
        ]
        return out

    return settled(existing) == settled(fresh)


def _write_card(
    out: Path,
    supplier: str,
    contract: str,
    region: str,
    month_id: str,
    snap: SupplierSnapshot,
    sources: list[dict[str, str]],
    now: datetime,
    via: str,
    seen_on: date | None = None,
) -> bool:
    """Write the card's month file; True when the file changed.

    The dict is what the integration's own Store persists for a month row,
    round-tripped through Home Assistant's encoder so the file holds exactly
    the types ``_snapshot_from_dict`` reads back, then laid out one key per
    line so a day's diff on the branch is readable. ``via`` records which
    path produced it, ``live`` (today's card, filed by its label) or
    ``archive`` (the supplier's own archive, filed by the month asked for).
    A replay passes the day the row was first captured as ``seen_on``.
    """
    today = seen_on or now.astimezone(_BRUSSELS).date()
    card: dict[str, Any] = json.loads(json_dumps(_snapshot_to_dict(snap, now)))
    card["_seen_on"] = today.isoformat()
    card["_sources"] = sources
    card["_via"] = via
    path = out / supplier / contract / region / f"{month_id}.json"
    existing: dict[str, Any] | None = None
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            existing = None
    if _same_card(existing, card):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(card, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return True


def _prune(out: Path, keep_months: int, today: date) -> int:
    """Remove months more than ``keep_months`` before today's; count them."""
    cutoff = _months_before(today, keep_months)
    removed = 0
    for path in out.glob("*/*/*/????-??.json"):
        if path.stem < cutoff:
            path.unlink()
            removed += 1
    for path in out.glob("texts/????-??"):
        if path.is_dir() and path.name < cutoff:
            shutil.rmtree(path)
            removed += 1
    # Deepest first, so a contract directory emptied above goes too.
    for folder in sorted(
        (p for p in out.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)
    ):
        if not any(folder.iterdir()):
            folder.rmdir()
    manifest = out / _MANIFEST
    if manifest.exists():
        kept = json.loads(manifest.read_text(encoding="utf-8"))
        # A release path is cards-<YYYY-MM>/<digest>.pdf; the workflow
        # deletes the release itself on the same cutoff.
        current = {d: p for d, p in kept.items() if p[len("cards-") :][:7] >= cutoff}
        if len(current) != len(kept):
            removed += len(kept) - len(current)
            manifest.write_text(
                json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
    return removed


def _kept_rows(
    out: Path,
) -> tuple[
    dict[str, dict[tuple[str, str], dict[str, tuple[str, list[str]]]]], dict[str, str]
]:
    """Every row on disk, by supplier, contract and region, then month: how
    it was captured and the digests of the PDFs it read; and the manifest."""
    held: dict[str, dict[tuple[str, str], dict[str, tuple[str, list[str]]]]] = {}
    for path in sorted(out.glob("*/*/*/????-??.json")):
        supplier, contract, region = path.parts[-4], path.parts[-3], path.parts[-2]
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        digests = [digest_of(s["pdf"]) for s in row.get("_sources", []) if "pdf" in s]
        held.setdefault(supplier, {}).setdefault((contract, region), {})[path.stem] = (
            row.get("_via", "live"),
            digests,
        )
    manifest = out / _MANIFEST
    kept: dict[str, str] = (
        json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    )
    return held, kept


def _pdf_link(
    digest: str, kept: dict[str, str], pdf_base_url: str | None
) -> str | None:
    """Where a person can download this PDF, once it has been uploaded."""
    path = kept.get(digest)
    if path is None or pdf_base_url is None:
        return None
    return f"{pdf_base_url}/{path}"


def _write_coverage(out: Path, pdf_base_url: str | None = None) -> None:
    """Rewrite the coverage table from the rows on disk.

    Each cell links to the PDF the month was parsed from, once that PDF is
    in the manifest. Deterministic in its order, so a day that changed
    nothing rewrites the file to the same bytes and the branch gets no
    commit for it.
    """
    held, kept = _kept_rows(out)
    months = sorted(
        {m for rows in held.values() for have in rows.values() for m in have}
    )
    lines = [
        "# Coverage",
        "",
        "One row per contract and region, one column per month the branch holds.",
        "`live` is a card captured while it was current, `mirror` one copied from the",
        "supplier's own archive; a blank cell is a month the branch does not hold.",
        "Each month links to the PDF it was parsed from, in the cards repository's",
        "releases; `pdfs.md` lists those files the other way round.",
        "",
    ]
    for supplier in sorted(held):
        lines += [
            f"## {supplier}",
            "",
            "| contract | region | " + " | ".join(months) + " |",
            "| --- | --- | " + " | ".join("---" for _ in months) + " |",
        ]
        for (contract, region), have in sorted(held[supplier].items()):
            cells = []
            for month in months:
                via, digests = have.get(month, ("", []))
                label = _VIA_LABELS.get(via, "")
                links = [
                    link
                    for link in (_pdf_link(d, kept, pdf_base_url) for d in digests)
                    if link is not None
                ]
                cells.append(f"[{label}]({links[0]})" if label and links else label)
            lines.append(f"| {contract} | {region} | " + " | ".join(cells) + " |")
        lines.append("")
    (out / _COVERAGE).write_text("\n".join(lines), encoding="utf-8")


def _write_pdf_index(out: Path, pdf_base_url: str | None = None) -> None:
    """Rewrite the list of kept PDFs: by release, each file with every card
    it was read for, since the release itself lists digests only."""
    held, kept = _kept_rows(out)
    readers: dict[str, list[str]] = {}
    for supplier, rows in held.items():
        for (contract, region), have in rows.items():
            for month, (_via, digests) in have.items():
                for digest in digests:
                    readers.setdefault(digest, []).append(
                        f"{supplier} / {contract} / {region} / {month}"
                    )
    by_release: dict[str, list[str]] = {}
    for digest in readers:
        path = kept.get(digest)
        tag = path.split("/")[0] if path else "not uploaded yet"
        by_release.setdefault(tag, []).append(digest)
    lines = [
        "# Kept cards",
        "",
        "Every PDF kept in the cards repository's releases, named by its SHA-256,",
        "with each card it was read for. `coverage.md` is the same index from the",
        "contract's side.",
        "",
    ]
    for tag in sorted(by_release, key=lambda t: (t == "not uploaded yet", t)):
        lines += [f"## {tag}", "", "| file | read for |", "| --- | --- |"]
        for digest in sorted(by_release[tag]):
            link = _pdf_link(digest, kept, pdf_base_url)
            name = f"{digest[:12]}….pdf"
            cell = f"[{name}]({link})" if link else name
            used = "<br>".join(sorted(readers[digest]))
            lines.append(f"| {cell} | {used} |")
        lines.append("")
    (out / _PDF_INDEX).write_text("\n".join(lines), encoding="utf-8")


def _transient(err: BaseException) -> bool:
    return isinstance(err, TimeoutError) or is_transient_fetch_error(str(err))


async def _fetch_card(
    fetch: Callable[[], Awaitable[_T]],
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> _T:
    """One card, retrying the transient failures the live check retries.

    ``fetch`` builds a fresh awaitable per attempt, since one can only be
    awaited once.
    """
    for attempt in range(_ATTEMPTS):
        try:
            return await asyncio.wait_for(fetch(), _CARD_TIMEOUT_S)
        except Exception as err:
            if attempt == _ATTEMPTS - 1 or not _transient(err):
                raise
            await sleep(_RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)])
    raise AssertionError("unreachable")


def _targets(
    extractors: Iterable[SupplierExtractor], only: set[str], today: date
) -> list[tuple[SupplierExtractor, str, str]]:
    """Every (extractor, contract, region) with a card to fetch today.

    The custom supplier is assembled from the entry and has no card, and a
    supplier past its withdrawal date has left the market: its last card
    stays up and stays stale, which is not worth a daily failure line.
    """
    out: list[tuple[SupplierExtractor, str, str]] = []
    for ex in extractors:
        if ex.id == SUPPLIER_CUSTOM or (only and ex.id not in only):
            continue
        if ex.deprecated_until is not None and today > ex.deprecated_until:
            continue
        for c in ex.contracts:
            for region in sorted(c.regions):
                out.append((ex, c.id, region))
    return out


async def _replay_row(
    path: Path,
    extractors: dict[str, SupplierExtractor],
    cards: _Cards,
    replay: _ReplaySession,
    now: datetime,
    summary: _Summary,
    *,
    rerender: bool = False,
) -> None:
    """Re-run one stored row through the current parser, offline.

    Under ``rerender`` the PDF texts are not seeded, so every card is
    fetched back from the kept copy and rendered afresh; the listing pages
    still come from the branch, since there is nothing to re-render there.
    """
    out = cards.out
    supplier, contract, region = path.parts[-4], path.parts[-3], path.parts[-2]
    label = f"{supplier}/{contract}/{region}/{path.stem}"
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        summary.unreplayable.append(f"{label}: not a JSON row")
        return
    extractor = extractors.get(supplier)
    if extractor is None:
        summary.unreplayable.append(f"{label}: no extractor registered")
        return
    memo = _RecordingMemo()
    for source in row.get("_sources", []):
        text_path = out / source["text"]
        if not text_path.exists():
            summary.unreplayable.append(f"{label}: {source['text']} is missing")
            return
        if rerender and source["variant"] != "text":
            continue
        key = (
            source["url"]
            if source["variant"] == "text"
            else f"{source['variant']}\0{source['url']}"
        )
        # Seeded, not touched: only what the parse actually reads counts.
        dict.__setitem__(memo, key, read_text(text_path))
    replay.pdfs = {
        s["url"]: digest_of(s["pdf"]) for s in row.get("_sources", []) if "pdf" in s
    }
    cards.digests.update(replay.pdfs)
    try:
        seen_on = date.fromisoformat(row["_seen_on"])
    except (KeyError, ValueError):
        summary.unreplayable.append(f"{label}: no capture date")
        return
    first = date(int(path.stem[:4]), int(path.stem[5:]), 1)
    session: Any = replay
    with memoise_text_fetches(memo), render_through(cards.render):
        try:
            if row.get("_via") == "archive":
                fetch_for_month = extractor.fetch_for_month
                if fetch_for_month is None:
                    summary.unreplayable.append(f"{label}: supplier has no archive now")
                    return
                snap = await fetch_for_month(session, contract, region, first)
            else:
                snap = await extractor.fetch(session, contract, region)
        except Exception as err:  # noqa: BLE001 - a row that will not replay is reported, not fatal
            summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
            return
    if snap is None or snap.provisional:
        summary.unreplayable.append(f"{label}: the archive path no longer settles it")
        return
    seen_month = _month_id(seen_on.year, seen_on.month)
    sources = [
        _source_entry(key, _write_text(out, seen_month, memo[key]), cards)
        for key in sorted(memo.touched)
    ]
    summary.replayed += 1
    if _write_card(
        out,
        supplier,
        contract,
        region,
        path.stem,
        snap,
        sources,
        now,
        row.get("_via", "live"),
        seen_on,
    ):
        summary.reparsed += 1


async def _replay_all(
    out: Path,
    extractors: dict[str, SupplierExtractor],
    cards: _Cards,
    replay: _ReplaySession,
    now: datetime,
    summary: _Summary,
    *,
    rerender: bool = False,
) -> None:
    """Every stored row, grouped by capture day so the clock is pinned
    once per day rather than once per row."""
    by_day: dict[str, list[Path]] = {}
    for path in sorted(out.glob("*/*/*/????-??.json")):
        try:
            day = json.loads(path.read_text(encoding="utf-8")).get("_seen_on", "")
        except ValueError:
            day = ""
        by_day.setdefault(day, []).append(path)
    for day, paths in sorted(by_day.items()):
        if not day:
            for path in paths:
                await _replay_row(
                    path, extractors, cards, replay, now, summary, rerender=rerender
                )
            continue
        # Ticking, so the loop's own timers and the render threads keep
        # working; the date stays the capture day for the seconds this takes.
        with freeze_time(f"{day}T12:00:00+02:00", tick=True):
            for path in paths:
                await _replay_row(
                    path, extractors, cards, replay, now, summary, rerender=rerender
                )


async def archive(
    out: Path,
    *,
    only: set[str] | None = None,
    keep_months: int = 36,
    backfill_months: int = 0,
    pdf_dir: Path | None = None,
    pdf_base_url: str | None = None,
    reparse: bool = False,
    rerender: bool = False,
    extractors: Iterable[SupplierExtractor] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> _Summary:
    """Fetch every card, store what changed, replay what the parser
    changed for, prune the old, report."""
    now = now or datetime.now(UTC)
    today = now.astimezone(_BRUSSELS).date()
    seen_month = _month_id(today.year, today.month)
    out.mkdir(parents=True, exist_ok=True)
    readme = out / "README.md"
    if not readme.exists():
        readme.write_text(_README, encoding="utf-8")
    summary = _Summary()
    memo = _RecordingMemo()
    patience = _Patience()
    cards = _Cards(out, pdf_dir, seen_month, serve_texts=not rerender)
    registry = tuple(all_extractors() if extractors is None else extractors)
    targets = _targets(registry, only or set(), today)
    async with aiohttp.ClientSession() as session:
        with memoise_text_fetches(memo), render_through(cards.render):
            for ex, contract, region in targets:
                if ex.id in patience.given_up:
                    continue
                label = f"{ex.id}/{contract}/{region}"
                memo.touched.clear()
                try:
                    snap = await _fetch_card(
                        lambda: ex.fetch(session, contract, region), sleep
                    )
                except Exception as err:  # noqa: BLE001 - one card must not stop the walk
                    summary.failed.append(f"{label}: {type(err).__name__}: {err}")
                    if patience.note(ex.id, err):
                        summary.given_up.append(ex.id)
                    continue
                patience.ok(ex.id)
                sources = [
                    _source_entry(key, _write_text(out, seen_month, memo[key]), cards)
                    for key in sorted(memo.touched)
                ]
                month_id = _card_month(snap, today)
                if _write_card(
                    out, ex.id, contract, region, month_id, snap, sources, now, "live"
                ):
                    summary.stored += 1
                else:
                    summary.unchanged += 1
            for ex, contract, region in targets:
                fetch_for_month = ex.fetch_for_month
                if fetch_for_month is None or ex.id in patience.given_up:
                    continue
                for back in range(1, backfill_months + 1):
                    if ex.id in patience.given_up:
                        break
                    month_id = _months_before(today, back)
                    if (out / ex.id / contract / region / f"{month_id}.json").exists():
                        continue
                    first = date(int(month_id[:4]), int(month_id[5:]), 1)
                    label = f"{ex.id}/{contract}/{region}/{month_id}"
                    memo.touched.clear()
                    try:
                        past = await _fetch_card(
                            lambda: fetch_for_month(session, contract, region, first),
                            sleep,
                        )
                    except Exception as err:  # noqa: BLE001 - one month must not stop the walk
                        summary.failed.append(f"{label}: {type(err).__name__}: {err}")
                        if patience.note(ex.id, err):
                            summary.given_up.append(ex.id)
                        continue
                    patience.ok(ex.id)
                    if past is None or past.provisional:
                        # Not out yet, past the horizon, or still carrying an
                        # estimate: leave the month for a later backfill.
                        summary.absent += 1
                        continue
                    sources = [
                        _source_entry(
                            key, _write_text(out, seen_month, memo[key]), cards
                        )
                        for key in sorted(memo.touched)
                    ]
                    _write_card(
                        out,
                        ex.id,
                        contract,
                        region,
                        month_id,
                        past,
                        sources,
                        now,
                        "archive",
                    )
                    summary.backfilled += 1
        # A fresh archive holds nothing older than this parser, so the first
        # run only stamps it; from then on a changed digest replays the rows.
        stamp = out / _PARSER_STAMP
        parser = _parser_digest()
        stamped = (
            stamp.read_text(encoding="utf-8").strip() if stamp.exists() else parser
        )
        if reparse or rerender or stamped != parser:
            replay = _ReplaySession(session, pdf_dir, pdf_base_url, cards.kept)
            await _replay_all(
                out,
                {ex.id: ex for ex in registry},
                cards,
                replay,
                now,
                summary,
                rerender=rerender,
            )
        stamp.write_text(parser + "\n", encoding="utf-8")
    summary.rendered = cards.rendered
    summary.unrendered = cards.unrendered
    summary.pdfs_saved = len(cards.saved)
    removed = _prune(out, keep_months, today)
    _write_coverage(out, pdf_base_url)
    _write_pdf_index(out, pdf_base_url)
    print(
        f"{summary.stored} stored, {summary.unchanged} unchanged, "
        f"{summary.backfilled} backfilled, {summary.absent} absent, "
        f"{len(summary.failed)} failed, {removed} pruned, "
        f"{len(targets)} cards asked; {summary.rendered} rendered, "
        f"{summary.unrendered} served from stored text, "
        f"{summary.pdfs_saved} new PDFs kept; {summary.replayed} replayed, "
        f"{summary.reparsed} reparsed, {len(summary.unreplayable)} not replayable"
    )
    for line in summary.failed:
        print(f"  failed {line[:300]}")
    for supplier in summary.given_up:
        print(
            f"  gave up on {supplier} after {_GIVE_UP_AFTER} network failures in a row"
        )
    for line in summary.unreplayable:
        print(f"  not replayable {line[:300]}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="archive checkout")
    parser.add_argument(
        "--only", action="append", default=[], help="restrict to a supplier id"
    )
    parser.add_argument("--keep-months", type=int, default=36)
    parser.add_argument(
        "--pdfs",
        type=Path,
        default=None,
        metavar="DIR",
        help="write every PDF the branch has not recorded yet under DIR",
    )
    parser.add_argument(
        "--pdf-base-url",
        default=None,
        metavar="URL",
        help="where the kept PDFs are served from, for a replay that needs one",
    )
    parser.add_argument(
        "--reparse",
        action="store_true",
        help="replay every stored row through the parser even if it did not change",
    )
    parser.add_argument(
        "--rerender",
        action="store_true",
        help="replay every row with its PDFs rendered afresh, for a reader upgrade",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="also mirror the N closed months before this one from the supplier archives",
    )
    parser.add_argument(
        "--index-only",
        action="store_true",
        help="only rewrite coverage.md and pdfs.md from what is on disk; no fetch",
    )
    args = parser.parse_args()
    if args.index_only:
        # After the workflow's upload step has extended the manifest, so the
        # links written by the walk before it point at files that now exist.
        _write_coverage(args.out, args.pdf_base_url)
        _write_pdf_index(args.out, args.pdf_base_url)
        return 0
    summary = asyncio.run(
        archive(
            args.out,
            only=set(args.only),
            keep_months=args.keep_months,
            backfill_months=args.backfill,
            pdf_dir=args.pdfs,
            pdf_base_url=args.pdf_base_url,
            reparse=args.reparse,
            rerender=args.rerender,
        )
    )
    return 0 if summary.stored or summary.unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
