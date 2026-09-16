#!/usr/bin/env python3
"""Store today's tariff cards in the repository's card archive.

Walks every registered (supplier, contract, region), fetches the card the
integration would price on right now and writes what it parsed to
``<out>/cards/<supplier>/<contract>/<region>/<YYYY-MM>.json``, with every text
the parse read under ``<out>/texts/<YYYY-MM>/<sha256>.txt``. A card is
filed under the month its own publication label names, so a supplier
publishing in arrears (Ecopower's definitive card lands at the end of the
month it covers) or ahead of time is filed where its card says; only a
card whose label cannot be read is filed under the month it was seen in.

Run daily by .github/workflows/archive_cards.yml against the card archive
in ``be_price_cards``, which the integration reads for any month a
supplier's own archive cannot serve
(``snapshot_store._archived_card_from_github``). A month
already on disk is rewritten only when the parse changed, so a quiet day
leaves nothing to commit, and months older than ``--keep-months`` are
removed on every run.

The cards themselves are kept too. With ``--pdfs DIR`` every PDF whose
bytes the archive has not recorded yet is written to
``DIR/electricity-<YYYY-MM>/<sha256>.pdf``, where the month is the one the
card is for (the month of the row that read it, so a mirrored March card
goes to March), and the workflow uploads each directory as the assets of
the release of that name in the cards repository shared with
be_water_prices: one release per month of cards, about two hundred files,
since a month of cards is about 100 MB and three years of them no git
tree can hold. Where each one landed is recorded in ``<out>/pdfs.json``. A card's
``_sources`` entry names its PDF by digest alone; the manifest is the one
place that says where it lives. The same digest is what keeps a daily run
cheap: a card whose bytes have not changed is served the text the archive
already holds for it instead of being rendered again.

A parser fix reaches the stored months on its own. Every row carries the
texts its parse read, so the run replays each row through the current
extractor with those texts served from the archive, the clock pinned to
the day the row was captured and no supplier contacted, and rewrites the
row when the parse came out differently. That replay costs a regex pass
per row and nothing else, and it happens only when the parser sources
changed since the archive was last replayed (a digest of them is stamped in
``parser.txt``), so a day without a code change replays nothing;
``--reparse`` forces it. A parser that now reads a card with a different
PDF reader finds no stored text for that reading and gets the kept PDF
back from the cards releases instead; ``--rerender`` asks for that on
every card, which is the way to pick up a reader upgrade.

``--backfill N`` also asks every supplier that keeps an archive of its own
for the N closed months before this one, through the same
``fetch_for_month`` the integration uses, and stores each month not held
yet. That makes this a mirror of the supplier archives: insurance against a
supplier dropping its own (DATS 24 did) and a cheap read for any month a
supplier's own path cannot serve. A month the
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
        [--archive-base-url https://github.com/<owner>/<this repo>/blob/archive]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import importlib.metadata
import json
import re
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
    _MIN_TEXT_LAYER_CHARS,
    is_transient_fetch_error,
    memoise_text_fetches,
    render_through,
)
from custom_components.be_electricity_prices.providers.base import (  # noqa: E402
    CardNotReadableError,
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
# The digest of the parser sources the archive was last replayed with.
_PARSER_STAMP = "parser.txt"
# This integration's namespace in the cards repository, which it shares
# with be_water_prices: its releases are electricity-<YYYY-MM>, one per
# month of cards, its listings live under electricity/ in that
# repository's tree.
_RELEASE_PREFIX = "electricity"
# One sheet per supplier of which months the archive holds, for the reader
# who wants to know whether a given month of a given contract is covered
# without listing directories, each month linking to the PDF it was
# parsed from (a release lists them by digest only), to the page it read
# and to the JSON it produced; and an index of the sheets, since one
# table of every supplier grows by a column a month.
_COVERAGE = "coverage.md"
_COVERAGE_DIR = "coverage"
# Which cards were downloaded and could not be read, by the row they would
# have become. Ecofix publishes page images some months and no reader can
# read those; the bytes are kept and uploaded like any other card, and
# without this nothing on the archive would say what they are.
_UNPARSED = "unparsed.json"
_LEGEND = (
    "Each month links to what it was parsed from and to what came out of it: `pdf` is the",
    "card itself, in the cards repository's releases, `page` the text of a page as it",
    "was read, and `json` the card as the integration parsed it, both in this repository.",
    "A month marked `(mirror)` was copied from the supplier's own archive rather than",
    "captured while it was current; a month marked `(not parsed)` is a card the archive",
    "holds but no reader could read, so there is no JSON to link; a blank cell is a",
    "month the archive does not hold.",
)
# What a parse depends on: the extractors, the shared readers and rate
# dataclasses beside them, the constants they key on, and the codec the
# rows are written with.
_PARSER_SOURCES = ("providers/*.py", "const.py", "snapshot_store.py")
# The PDF readers whose installed version is part of what a parse depends on.
_READERS = ("pypdf", "pdfplumber")


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
# The rows sit under their own directory of the cards repository, so the
# texts, the manifest and the sheets keep the top of it readable.
_ROWS = "cards"


def _ocr_text(payload: bytes) -> str:
    """What an OCR engine reads off a card that carries no text layer.

    The last thing tried, and only here. Ecofix has published its cards as
    page images since August, and a document with nothing in it is the end
    of the road for a parser, but not for a reader that knows the fonts
    these cards are set in. ``ocr_price_cards`` returns text shaped exactly
    like pdfplumber's, so the supplier's own extractor parses it without
    knowing anything happened.

    This runs in CI, never in anyone's Home Assistant: the engine needs
    Python 3.14 where Home Assistant still allows 3.13, and it carries a
    17 MB glyph library. What reaches an installation is the row this walk
    writes, which is JSON like every other row.

    Read with ``strict=False`` and taken from ``trusted_text``: a line
    carrying a mark the engine refused is left out of it, so a figure that
    is there was read whole, and one it could not read is missing rather
    than wrong: a missing mandatory figure fails the parse, which is the
    bargain the extractors already make. Anything it cannot deliver raises
    the error the renderer raised, so the card falls to ``unparsed.json``
    exactly as it did before.
    """
    try:
        from ocr_price_cards import read_pdf
    except ImportError as err:
        raise CardNotReadableError(
            "card has no text layer and ocr_price_cards is not installed"
        ) from err
    try:
        text = str(read_pdf(payload, strict=False).trusted_text)
    except Exception as err:
        raise CardNotReadableError(f"OCR could not read the card: {err}") from err
    # A reading is held to the floor a text layer is held to, for the reason
    # that floor exists: one that refused most of the page leaves a row full
    # of silent misses, and no row at all is the better answer.
    if len(text.strip()) < _MIN_TEXT_LAYER_CHARS:
        raise CardNotReadableError(
            f"OCR read only {len(text.strip())} characters it was sure of, "
            "which is too little of a card to price on"
        )
    return text


class _Cards(StoredTexts):
    """What the run knows about card bytes, plus where they are kept.

    The render cache is the shared one (``card_texts.StoredTexts``): a
    downloaded card whose bytes the archive has already seen is served the
    stored text instead of being rendered again, which is what makes a
    daily walk over 250 cards cheap. On top of it, ``pdfs.json`` says which
    digests are already uploaded, and bytes the archive has not recorded yet
    are held until a row names them and then written under ``pdf_dir`` in
    the directory of that row's month, for the workflow to upload to the
    release of that month.
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
        # Downloaded, not recorded anywhere yet, waiting for the row that
        # names it to say which month it is for.
        self.pending: dict[str, bytes] = {}
        # Digests of the cards read off their pixels, so the rows they produce
        # can say they were read that way. Seeded by ``StoredTexts`` from the
        # rows already on the archive and added to whenever this run OCRs one:
        # a card whose bytes have not changed is served its stored text without
        # the reader running, so the fact has to outlive the run that found it.
        # Every card downloaded for the target being walked, as (url, digest),
        # whether or not it rendered. ``calls`` cannot answer this: it is
        # appended to after the render, and a card published as page images
        # raises there, which is precisely the card that has to be named. The
        # url is what lets a later run hand the kept bytes back to the
        # extractor. Cleared per target by the caller, beside ``calls``.
        self.seen: list[tuple[str, str]] = []

    async def render(
        self, variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        """The card's text, or what OCR reads off it when it carries none.

        Wraps the cache rather than replacing it: a card whose bytes the
        archive has already read is still served its stored text, OCR or not,
        and only a card the renderer refuses reaches the engine. The stored
        text is the row's own, so a card read by OCR once is not read again
        the next day.
        """
        digest = hashlib.sha256(payload).hexdigest()
        self.seen.append((url, digest))
        try:
            return await super().render(variant, url, payload, renderer)
        except CardNotReadableError:
            text = await asyncio.to_thread(_ocr_text, payload)
        self.rendered += 1
        self.fresh[(variant, digest)] = text
        self.calls.append((variant, url, digest, text))
        self.ocr.add(digest)
        return text

    def keep(self, digest: str, payload: bytes) -> None:
        if self.pdf_dir is None or digest in self.kept or digest in self.saved:
            return
        self.pending[digest] = payload

    def knows(self, digest: str) -> bool:
        """Whether this card is kept, or about to be: only then may a text
        that embeds it be folded down to a reference."""
        return digest in self.kept or digest in self.saved or digest in self.pending

    def file(self, month_id: str, digests: Iterable[str]) -> None:
        """Write the pending bytes a row read under that row's month."""
        if self.pdf_dir is None:
            return
        for digest in digests:
            payload = self.pending.pop(digest, None)
            if payload is None:
                continue
            rel = f"{_RELEASE_PREFIX}-{month_id}/{digest}.pdf"
            path = self.pdf_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.saved[digest] = rel

    def file_the_rest(self) -> None:
        """Bytes no row ever named, a card whose parse failed (Ecofix's page
        images), go under the month they were captured in: kept, findable
        by digest, just without a row to say what they are."""
        self.file(self.seen_month, list(self.pending))


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

    async def bytes_for(self, digest: str) -> bytes:
        """The kept card with this digest, wherever it is held."""
        self.pdfs.setdefault(f"card:{digest}", digest)
        return await self._fetch(f"card:{digest}")

    def _get(self, url: str) -> _Pending:
        return self._Pending(self._fetch(url), self.pdfs.get(url, ""))


def _parser_digest() -> str:
    """One digest over every source a parse depends on.

    The readers count as sources: a pypdf or pdfplumber release can lay a
    card out differently (a 6.16 against a 6.18 render gave different texts
    on 2026-09-13), and a stored text is served to every later replay and to
    the live check for as long as the card's bytes stand, so without the
    reader version in here a pin bump replayed nothing and the new reader was
    never run on a card the archive already held.
    """
    root = ROOT / "custom_components" / "be_electricity_prices"
    digest = hashlib.sha256()
    for pattern in _PARSER_SOURCES:
        for path in sorted(root.glob(pattern)):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(path.read_bytes())
    for reader in _READERS:
        digest.update(f"{reader}=={importlib.metadata.version(reader)}".encode("utf-8"))
    return digest.hexdigest()


def _month_id(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def _release_month(path: str) -> str:
    """The month a release path was captured in, from its tag."""
    found = re.search(r"(\d{4}-\d{2})", path.split("/")[0])
    return found.group(1) if found else ""


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


# A card handed over inside a document rather than downloaded on its own:
# OCTA+'s archive answers with {"TariffSheet":"data:application/pdf;base64,..."}.
# The bytes are kept as a release asset like any other card, so storing the
# base64 too is the same PDF a second time, a third larger for the encoding and
# incompressible with it. 150 such texts held 80,9 MB of the archive's 111,5.
# The envelope is what matters; the payload is folded to a reference and put
# back from the kept copy when a replay needs it.
_EMBEDDED_CARD = re.compile(r"(data:[\w/+.-]+;base64,)([A-Za-z0-9+/=]{512,})")
_CARD_REF = re.compile(r"\{\{card:([0-9a-f]{64})\}\}")


def _fold_embedded_cards(text: str, cards: "_Cards") -> str:
    """Replace every embedded card this run keeps with a reference to it."""

    def fold(match: re.Match[str]) -> str:
        try:
            payload = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error):
            return match.group(0)
        digest = hashlib.sha256(payload).hexdigest()
        if not cards.knows(digest):
            # Nothing to put back from later, so keep it as it came.
            return match.group(0)
        return f"{match.group(1)}{{{{card:{digest}}}}}"

    return _EMBEDDED_CARD.sub(fold, text)


async def _unfold_embedded_cards(text: str, replay: "_ReplaySession") -> str:
    """Put the kept cards back into a stored text, for a parse to read."""
    for digest in dict.fromkeys(_CARD_REF.findall(text)):
        payload = await replay.bytes_for(digest)
        text = text.replace(
            f"{{{{card:{digest}}}}}", base64.b64encode(payload).decode("ascii")
        )
    return text


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
        if digest in cards.ocr:
            # Written only for a card read off its pixels, so every other row
            # on the archive stays byte-identical to what it already holds.
            entry["ocr"] = "1"
    return entry


def _sources_of(
    memo: _RecordingMemo, cards: _Cards, out: Path, seen_month: str
) -> list[dict[str, str]]:
    """Everything one parse read: the memo entries it touched, plus any card
    the render hook saw that never passed through the memo (a card handed
    over inside a JSON answer), each with its text stored and its digest."""
    sources = [
        _source_entry(
            key,
            _write_text(out, seen_month, _fold_embedded_cards(memo[key], cards)),
            cards,
        )
        for key in sorted(memo.touched)
    ]
    named = {(s["variant"], s["url"]) for s in sources}
    for variant, url, digest, text in cards.calls:
        if (variant, url) in named:
            continue
        named.add((variant, url))
        entry = {
            "url": url,
            "variant": variant,
            "text": _write_text(out, seen_month, text),
            "pdf": digest,
        }
        if digest in cards.ocr:
            entry["ocr"] = "1"
        sources.append(entry)
    return sorted(
        sources, key=lambda s: (s["variant"] != "text", s["variant"], s["url"])
    )


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
    line so a day's diff on the archive is readable. ``via`` records which
    path produced it, ``live`` (today's card, filed by its label) or
    ``archive`` (the supplier's own archive, filed by the month asked for).
    A replay passes the day the row was first captured as ``seen_on``.

    A card read off its pixels is marked on the SOURCE that names it, by
    ``_source_entry``, not on the row: a row can read two documents and only
    one of them need be the unreadable one. The row carried a derived copy as
    ``_ocr`` and nothing kept it in step, so it is gone; readers ask the
    sources.
    """
    today = seen_on or now.astimezone(_BRUSSELS).date()
    card: dict[str, Any] = json.loads(json_dumps(_snapshot_to_dict(snap, now)))
    card["_seen_on"] = today.isoformat()
    card["_sources"] = sources
    card["_via"] = via
    path = out / _ROWS / supplier / contract / region / f"{month_id}.json"
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
    for path in out.glob(f"{_ROWS}/*/*/*/????-??.json"):
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
        # A release path is <prefix>-<YYYY-MM>[-n]/<digest>.pdf; the workflow
        # deletes the release itself on the same cutoff.
        current = {d: p for d, p in kept.items() if _release_month(p) >= cutoff}
        if len(current) != len(kept):
            removed += len(kept) - len(current)
            manifest.write_text(
                json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8"
            )
    return removed


@dataclass
class _Held:
    """One stored month, as the coverage table needs it."""

    via: str
    digests: list[str]
    page: str | None
    # None for a card the archive holds but could not parse: there is no row.
    path: str | None


def _kept_rows(
    out: Path,
) -> tuple[dict[str, dict[tuple[str, str], dict[str, _Held]]], dict[str, str]]:
    """Every row on disk, by supplier, contract and region, then month: how
    it was captured, the digests of the PDFs it read, the first page it read
    and its own path; and the manifest."""
    held: dict[str, dict[tuple[str, str], dict[str, _Held]]] = {}
    for path in sorted(out.glob(f"{_ROWS}/*/*/*/????-??.json")):
        supplier, contract, region = path.parts[-4], path.parts[-3], path.parts[-2]
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        sources = row.get("_sources", [])
        pages = [s["text"] for s in sources if s.get("variant") == "text"]
        held.setdefault(supplier, {}).setdefault((contract, region), {})[path.stem] = (
            _Held(
                row.get("_via", "live"),
                [digest_of(s["pdf"]) for s in sources if "pdf" in s],
                pages[0] if pages else None,
                path.relative_to(out).as_posix(),
            )
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


def _link(label: str, base: str | None, rel: str) -> str:
    """A link into the archive, or the bare label with nowhere to link to."""
    return f"[{label}]({base}/{rel})" if base else label


def _cell(
    held: _Held | None,
    kept: dict[str, str],
    pdf_base_url: str | None,
    archive_base_url: str | None,
) -> str:
    """What a month links to: the card when one was read (once it is
    uploaded), the page it was parsed from otherwise, and the JSON the
    parse produced; a mirrored month says so."""
    if held is None:
        return ""
    links = [
        link
        for link in (_pdf_link(d, kept, pdf_base_url) for d in held.digests)
        if link
    ]
    if links:
        parts = [f"[pdf]({links[0]})"]
    elif held.digests:
        parts = ["pdf"]
    elif held.page is not None:
        parts = [_link("page", archive_base_url, held.page)]
    else:
        parts = []
    if held.path is None:
        parts.append("(not parsed)")
        return " ".join(parts)
    parts.append(_link("json", archive_base_url, held.path))
    if held.via == "archive":
        parts.append("(mirror)")
    return " ".join(parts)


def _unparsed_key(supplier: str, contract: str, region: str, month: str) -> str:
    return f"{supplier}/{contract}/{region}/{month}"


def _unparsed_sources(cards: "_Cards") -> list[dict[str, str]]:
    """The cards this target downloaded, shaped like a row's ``_sources``.

    The url is the half that matters later: it is what lets a retry hand the
    kept bytes back to the extractor, which asks for a card by url and
    cannot be told to want a digest.
    """
    seen: dict[str, str] = dict(cards.seen)
    return [{"url": url, "pdf": digest} for url, digest in sorted(seen.items())]


def _read_unparsed(out: Path) -> dict[str, list[Any]]:
    """What earlier runs recorded as downloaded but unreadable."""
    path = out / _UNPARSED
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _as_sources(value: list[Any]) -> list[dict[str, str]]:
    """One entry's cards. The first version of this file held bare digests,
    which name the bytes but not where they came from; those are kept as they
    are and simply cannot be retried."""
    return [
        {"pdf": item} if isinstance(item, str) else item
        for item in value
        if isinstance(item, str | dict)
    ]


def _write_unparsed(
    out: Path, seen: dict[str, list[dict[str, str]]], keep_months: int, today: date
) -> None:
    """Merge this run's unreadable cards into the store and drop what no
    longer belongs there.

    Merged rather than replaced, so a run over one supplier does not forget
    the others; an entry whose month now has a row is dropped, so a card
    that starts parsing leaves by itself; and the retention window is the
    same one the rows are pruned on.
    """
    known = {key: _as_sources(value) for key, value in _read_unparsed(out).items()}
    known.update(seen)
    held, _kept = _kept_rows(out)
    cutoff = _months_before(today, keep_months)
    entries: dict[str, list[dict[str, str]]] = {}
    for key, digests in known.items():
        parts = key.split("/")
        if len(parts) != 4:
            continue
        supplier, contract, region, month = parts
        if month < cutoff:
            continue
        if month in held.get(supplier, {}).get((contract, region), {}):
            continue
        entries[key] = sorted(_as_sources(digests), key=lambda s: s.get("url", ""))
    path = out / _UNPARSED
    if not entries:
        path.unlink(missing_ok=True)
        return
    path.write_text(
        json.dumps(dict(sorted(entries.items())), indent=2) + "\n", encoding="utf-8"
    )


def _write_coverage(
    out: Path, pdf_base_url: str | None = None, archive_base_url: str | None = None
) -> None:
    """Rewrite the coverage sheets from the rows on disk: one per supplier
    under ``coverage/`` and an index naming them.

    Deterministic in their order, so a day that changed nothing rewrites
    them to the same bytes and the archive gets no commit for it. A sheet
    whose supplier has no rows any more is removed.
    """
    held, kept = _kept_rows(out)
    for key, value in _read_unparsed(out).items():
        supplier, contract, region, month = key.split("/")
        by_month = held.setdefault(supplier, {}).setdefault((contract, region), {})
        pdfs = [s["pdf"] for s in _as_sources(value) if s.get("pdf")]
        by_month.setdefault(month, _Held("live", pdfs, None, None))
    folder = out / _COVERAGE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    index = [
        "# Coverage",
        "",
        "One sheet per supplier, each a table with a row per contract and region and a",
        "column per month the archive holds.",
        *_LEGEND,
        "",
    ]
    for supplier in sorted(held):
        rows = held[supplier]
        months = sorted({m for have in rows.values() for m in have})
        lines = [
            f"# {supplier}",
            "",
            "One row per contract and region, one column per month the archive holds.",
            *_LEGEND,
            "",
            "| contract | region | " + " | ".join(months) + " |",
            "| --- | --- | " + " | ".join("---" for _ in months) + " |",
        ]
        for (contract, region), have in sorted(rows.items()):
            cells = [
                _cell(have.get(m), kept, pdf_base_url, archive_base_url) for m in months
            ]
            lines.append(f"| {contract} | {region} | " + " | ".join(cells) + " |")
        lines.append("")
        (folder / f"{supplier}.md").write_text("\n".join(lines), encoding="utf-8")
        index.append(
            f"- [{supplier}]({_COVERAGE_DIR}/{supplier}.md): {len(rows)} rows,"
            f" {months[0]} to {months[-1]}"
        )
    for stale in folder.glob("*.md"):
        if stale.stem not in held:
            stale.unlink()
    index.append("")
    (out / _COVERAGE).write_text("\n".join(index), encoding="utf-8")


def _write_listings(
    out: Path, pdf_base_url: str | None = None, archive_base_url: str | None = None
) -> None:
    """The coverage sheets, rewritten when out of date. The index of PDFs by
    release that earlier versions wrote is removed, the coverage sheets having
    taken it over; the README beside them is the workflow's."""
    _write_coverage(out, pdf_base_url, archive_base_url)
    (out / "pdfs.md").unlink(missing_ok=True)


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
    still come from the archive, since there is nothing to re-render there.
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
        stored = read_text(text_path)
        if _CARD_REF.search(stored):
            try:
                stored = await _unfold_embedded_cards(stored, replay)
            except Exception as err:  # noqa: BLE001 - a card we cannot put back is a row we cannot replay
                summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
                return
        dict.__setitem__(memo, key, stored)
    replay.pdfs = {
        s["url"]: digest_of(s["pdf"]) for s in row.get("_sources", []) if "pdf" in s
    }
    cards.digests.update(replay.pdfs)
    cards.calls.clear()
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
    sources = _sources_of(memo, cards, out, seen_month)
    # A card the row had never named (OCTA+'s, before its archive path went
    # through the seam) is kept now, under this row's month.
    cards.file(path.stem, (s["pdf"] for s in sources if "pdf" in s))
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


async def _retry_unparsed(
    out: Path,
    extractors: dict[str, SupplierExtractor],
    cards: _Cards,
    replay: _ReplaySession,
    now: datetime,
    summary: _Summary,
) -> None:
    """Try the cards the archive holds but could not read, again.

    A card that no reader could read is kept, uploaded and named in
    ``unparsed.json``, and there it stays: it cannot be re-fetched, because
    a supplier serving one url overwrites it the next month. The bytes are
    the only copy there will ever be, so the one thing that can change the
    answer is the reader, which is exactly what ``parser.txt`` already
    tracks. So this runs under the same condition the row replay does, and
    the day a reader learns to read those cards they become rows.

    The clock is pinned to the middle of the month the card was captured in,
    the way a row's replay is pinned to its capture day: a parse that reads
    "valid until" against today must not decide a card from two months ago
    is expired.
    """
    entries = {key: _as_sources(value) for key, value in _read_unparsed(out).items()}
    for key, sources in sorted(entries.items()):
        supplier, contract, region, month = key.split("/")
        label = f"{supplier}/{contract}/{region}/{month}"
        extractor = extractors.get(supplier)
        if extractor is None:
            continue
        pdfs = {s["url"]: s["pdf"] for s in sources if s.get("url") and s.get("pdf")}
        if not pdfs:
            summary.unreplayable.append(f"{label}: kept bytes name no url to replay")
            continue
        replay.pdfs = pdfs
        cards.digests.update(pdfs)
        cards.calls.clear()
        cards.seen.clear()
        memo = _RecordingMemo()
        session: Any = replay
        with (
            freeze_time(f"{month}-15T12:00:00+02:00", tick=True),
            memoise_text_fetches(memo),
            render_through(cards.render),
        ):
            try:
                snap = await extractor.fetch(session, contract, region)
            except Exception as err:  # noqa: BLE001 - still unreadable is the normal answer
                summary.unreplayable.append(f"{label}: {type(err).__name__}: {err}")
                continue
        if snap is None or snap.provisional:
            continue
        read = _sources_of(memo, cards, out, month)
        summary.replayed += 1
        if _write_card(
            out,
            supplier,
            contract,
            region,
            month,
            snap,
            read,
            now,
            "live",
            date(int(month[:4]), int(month[5:]), 15),
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
    for path in sorted(out.glob(f"{_ROWS}/*/*/*/????-??.json")):
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
    archive_base_url: str | None = None,
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
    summary = _Summary()
    memo = _RecordingMemo()
    patience = _Patience()
    # A card that downloaded but did not parse, by the row it would have
    # become. A card that failed on the network read no bytes and leaves
    # nothing here.
    unreadable: dict[str, list[dict[str, str]]] = {}
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
                cards.calls.clear()
                cards.seen.clear()
                try:
                    snap = await _fetch_card(
                        lambda: ex.fetch(session, contract, region), sleep
                    )
                except Exception as err:  # noqa: BLE001 - one card must not stop the walk
                    summary.failed.append(f"{label}: {type(err).__name__}: {err}")
                    read = _unparsed_sources(cards)
                    if read:
                        unreadable[
                            _unparsed_key(ex.id, contract, region, seen_month)
                        ] = read
                    if patience.note(ex.id, err):
                        summary.given_up.append(ex.id)
                    continue
                patience.ok(ex.id)
                sources = _sources_of(memo, cards, out, seen_month)
                month_id = _card_month(snap, today)
                if _write_card(
                    out,
                    ex.id,
                    contract,
                    region,
                    month_id,
                    snap,
                    sources,
                    now,
                    "live",
                ):
                    summary.stored += 1
                else:
                    summary.unchanged += 1
                cards.file(month_id, (s["pdf"] for s in sources if "pdf" in s))
            for ex, contract, region in targets:
                fetch_for_month = ex.fetch_for_month
                if fetch_for_month is None or ex.id in patience.given_up:
                    continue
                for back in range(1, backfill_months + 1):
                    if ex.id in patience.given_up:
                        break
                    month_id = _months_before(today, back)
                    if (
                        out / _ROWS / ex.id / contract / region / f"{month_id}.json"
                    ).exists():
                        continue
                    first = date(int(month_id[:4]), int(month_id[5:]), 1)
                    label = f"{ex.id}/{contract}/{region}/{month_id}"
                    memo.touched.clear()
                    cards.calls.clear()
                    cards.seen.clear()
                    try:
                        past = await _fetch_card(
                            lambda: fetch_for_month(session, contract, region, first),
                            sleep,
                        )
                    except Exception as err:  # noqa: BLE001 - one month must not stop the walk
                        summary.failed.append(f"{label}: {type(err).__name__}: {err}")
                        read = _unparsed_sources(cards)
                        if read:
                            unreadable[
                                _unparsed_key(ex.id, contract, region, month_id)
                            ] = read
                        if patience.note(ex.id, err):
                            summary.given_up.append(ex.id)
                        continue
                    patience.ok(ex.id)
                    if past is None or past.provisional:
                        # Not out yet, past the horizon, or still carrying an
                        # estimate: leave the month for a later backfill.
                        summary.absent += 1
                        continue
                    sources = _sources_of(memo, cards, out, seen_month)
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
                    cards.file(month_id, (s["pdf"] for s in sources if "pdf" in s))
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
            # Same trigger, same reason: a reader that changed is the only
            # thing that can turn a card nobody could read into a row.
            await _retry_unparsed(
                out, {ex.id: ex for ex in registry}, cards, replay, now, summary
            )
        stamp.write_text(parser + "\n", encoding="utf-8")
    cards.file_the_rest()
    _write_unparsed(out, unreadable, keep_months, today)
    summary.rendered = cards.rendered
    summary.unrendered = cards.unrendered
    summary.pdfs_saved = len(cards.saved)
    removed = _prune(out, keep_months, today)
    _write_listings(out, pdf_base_url, archive_base_url)
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
        help="write every PDF the archive has not recorded yet under DIR",
    )
    parser.add_argument(
        "--pdf-base-url",
        default=None,
        metavar="URL",
        help="where the kept PDFs are served from, for a replay that needs one",
    )
    parser.add_argument(
        "--archive-base-url",
        default=None,
        metavar="URL",
        help="where the archive is browsed, for the listing's links to rows and pages",
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
        help="only rewrite the coverage sheets from what is on disk; no fetch",
    )
    args = parser.parse_args()
    if args.index_only:
        # After the workflow's upload step has extended the manifest, so the
        # links written by the walk before it point at files that now exist.
        _write_listings(args.out, args.pdf_base_url, args.archive_base_url)
        return 0
    summary = asyncio.run(
        archive(
            args.out,
            only=set(args.only),
            keep_months=args.keep_months,
            backfill_months=args.backfill,
            pdf_dir=args.pdfs,
            pdf_base_url=args.pdf_base_url,
            archive_base_url=args.archive_base_url,
            reparse=args.reparse,
            rerender=args.rerender,
        )
    )
    return 0 if summary.stored or summary.unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
