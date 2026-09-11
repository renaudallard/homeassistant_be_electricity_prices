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
directory as release assets of a separate cards repository (one release per
month, since a month of cards is about 100 MB and three years of them no
git branch can hold), then records each upload in ``<out>/pdfs.json``. A
card's ``_sources`` entry names its PDF by that release path. The same
digest is what keeps a daily run cheap: a card whose bytes have not
changed is served the text the branch already holds for it instead of
being rendered again.

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
# month is read by the same function the live check's freshness gate uses.
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
  lists its own under `_sources`.

Months older than three years are removed.
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


class _Cards:
    """What the run knows about card bytes.

    Seeded from the branch: ``pdfs.json`` maps every digest already uploaded
    to its release path, and each row's ``_sources`` maps a (variant,
    digest) pair to the text it rendered to. Installed as the readers'
    render hook, so a downloaded card whose bytes the branch has already
    seen is served that stored text instead of being rendered again, which
    is what makes a daily walk over 250 cards cheap: the download is
    seconds, the render is the cost. Bytes the branch has not recorded yet
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
        self.out = out
        self.pdf_dir = pdf_dir
        self.seen_month = seen_month
        # False under --rerender: every card is rendered afresh, which is
        # how a reader upgrade reaches the stored months.
        self.serve_texts = serve_texts
        self.kept: dict[str, str] = {}
        manifest = out / _MANIFEST
        if manifest.exists():
            self.kept = json.loads(manifest.read_text(encoding="utf-8"))
        # (variant, digest) -> text path on the branch, and digest -> release
        # path, from every stored row: the second is how a replayed row keeps
        # naming its PDF when the memo served the text and no download ran.
        self.texts: dict[tuple[str, str], str] = {}
        self.known: dict[str, str] = {}
        for row in out.glob("*/*/*/????-??.json"):
            try:
                sources = json.loads(row.read_text(encoding="utf-8")).get(
                    "_sources", []
                )
            except ValueError:
                continue
            for source in sources:
                if "pdf" in source:
                    digest = Path(source["pdf"]).stem
                    self.texts[(source["variant"], digest)] = source["text"]
                    self.known[digest] = source["pdf"]
        self.fresh: dict[tuple[str, str], str] = {}
        self.digests: dict[str, str] = {}
        self.saved: dict[str, str] = {}
        self.rendered = 0
        self.unrendered = 0

    def path_for(self, url: str) -> str | None:
        """Where the PDF behind ``url`` is, or will be once uploaded."""
        digest = self.digests.get(url)
        if digest is None:
            return None
        return self.kept.get(digest) or self.saved.get(digest) or self.known.get(digest)

    async def render(
        self, variant: str, url: str, payload: bytes, renderer: Callable[[bytes], str]
    ) -> str:
        digest = hashlib.sha256(payload).hexdigest()
        self.digests[url] = digest
        if (
            self.pdf_dir is not None
            and digest not in self.kept
            and digest not in self.saved
        ):
            rel = f"cards-{self.seen_month}/{digest}.pdf"
            path = self.pdf_dir / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            self.saved[digest] = rel
        key = (variant, digest)
        text = self.fresh.get(key)
        if text is None and self.serve_texts:
            stored = self.texts.get(key)
            if stored is not None and (self.out / stored).exists():
                text = _read_text(self.out / stored)
        if text is not None:
            self.unrendered += 1
            return text
        text = await asyncio.to_thread(renderer, payload)
        self.rendered += 1
        self.fresh[key] = text
        return text


class _KeptResponse:
    """The response shape the readers use, over bytes already in hand."""

    status = 200
    content_length = None

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

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
    PDF reader (a variant the row has no text for), and that is served
    from the kept PDF, the local copy first and the cards releases
    otherwise. Anything else is refused as a network error, which the
    readers wrap the way they wrap a real one, and the row is left as it
    was and reported.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        pdf_dir: Path | None,
        pdf_base_url: str | None,
    ) -> None:
        self._session = session
        self._pdf_dir = pdf_dir
        self._pdf_base_url = pdf_base_url
        self.pdfs: dict[str, str] = {}

    def get(self, url: str, **_kw: Any) -> Any:
        return self._get(url)

    async def _fetch(self, url: str) -> bytes:
        path = self.pdfs.get(url)
        if path is None:
            raise aiohttp.ClientConnectionError(f"offline replay has nothing for {url}")
        if self._pdf_dir is not None and (self._pdf_dir / path).exists():
            return (self._pdf_dir / path).read_bytes()
        if self._pdf_base_url is None:
            raise aiohttp.ClientConnectionError(f"no kept copy of {url} to replay from")
        async with self._session.get(
            f"{self._pdf_base_url}/{path}", timeout=aiohttp.ClientTimeout(total=60)
        ) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientConnectionError(
                    f"HTTP {resp.status} fetching the kept copy of {url}"
                )
            return await resp.read()

    class _Pending:
        """An awaitable-and-enterable stand-in for aiohttp's request context."""

        def __init__(self, fetch: Awaitable[bytes]) -> None:
            self._fetch = fetch

        async def __aenter__(self) -> _KeptResponse:
            return _KeptResponse(await self._fetch)

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def _get(self, url: str) -> _Pending:
        return self._Pending(self._fetch(url))


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
    rendered document also names its PDF's release path when the bytes
    were seen."""
    variant, sep, url = key.partition("\0")
    if not sep:
        return {"url": key, "variant": "text", "text": path}
    entry = {"url": url, "variant": variant, "text": path}
    pdf = cards.path_for(url)
    if pdf is not None:
        entry["pdf"] = pdf
    return entry


def _read_text(path: Path) -> str:
    """A stored text exactly as it was fetched: no newline translation, so
    a replay hands the parser the very bytes it read the first time."""
    with path.open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


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
    if existing is None:
        return False

    def settled(card: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in card.items() if k not in _VOLATILE_KEYS}

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
) -> None:
    """Re-run one stored row through the current parser, offline."""
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
        key = (
            source["url"]
            if source["variant"] == "text"
            else f"{source['variant']}\0{source['url']}"
        )
        # Seeded, not touched: only what the parse actually reads counts.
        dict.__setitem__(memo, key, _read_text(text_path))
    replay.pdfs = {s["url"]: s["pdf"] for s in row.get("_sources", []) if "pdf" in s}
    for url, pdf in replay.pdfs.items():
        cards.digests[url] = Path(pdf).stem
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
                await _replay_row(path, extractors, cards, replay, now, summary)
            continue
        # Ticking, so the loop's own timers and the render threads keep
        # working; the date stays the capture day for the seconds this takes.
        with freeze_time(f"{day}T12:00:00+02:00", tick=True):
            for path in paths:
                await _replay_row(path, extractors, cards, replay, now, summary)


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
            replay = _ReplaySession(session, pdf_dir, pdf_base_url)
            await _replay_all(
                out, {ex.id: ex for ex in registry}, cards, replay, now, summary
            )
        stamp.write_text(parser + "\n", encoding="utf-8")
    summary.rendered = cards.rendered
    summary.unrendered = cards.unrendered
    summary.pdfs_saved = len(cards.saved)
    removed = _prune(out, keep_months, today)
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
    args = parser.parse_args()
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
