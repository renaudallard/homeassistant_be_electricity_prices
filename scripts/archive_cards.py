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
    python scripts/archive_cards.py --out tmp/archive [--only mega ...] [--backfill 12]
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
# One card, fetch and parse together. The PDF helpers cap each download on
# their own; this bounds a parse that never returns so the rest of the
# registry is still archived and the summary still prints.
_CARD_TIMEOUT_S = 300
# Keys the daily run rewrites; two files that differ only here hold the
# same card and the older one is kept.
_VOLATILE_KEYS = ("_cached_at", "_seen_on")

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
    failed: list[str] = field(default_factory=list)


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


def _source_entry(key: str, path: str) -> dict[str, str]:
    """Describe one memo entry: the PDF helpers key a rendered document as
    ``<variant>\\0<url>`` and a plain text fetch by its URL alone."""
    variant, sep, url = key.partition("\0")
    if not sep:
        return {"url": key, "variant": "text", "text": path}
    return {"url": url, "variant": variant, "text": path}


def _write_text(out: Path, seen_month: str, text: str) -> str:
    """Store ``text`` once, content-addressed, and return its path in ``out``."""
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rel = f"texts/{seen_month}/{digest}.txt"
    path = out / rel
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
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
) -> bool:
    """Write the card's month file; True when the file changed.

    The dict is what the integration's own Store persists for a month row,
    round-tripped through Home Assistant's encoder so the file holds exactly
    the types ``_snapshot_from_dict`` reads back, then laid out one key per
    line so a day's diff on the branch is readable. ``via`` records which
    path produced it, ``live`` (today's card, filed by its label) or
    ``archive`` (the supplier's own archive, filed by the month asked for).
    """
    today = now.astimezone(_BRUSSELS).date()
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


async def archive(
    out: Path,
    *,
    only: set[str] | None = None,
    keep_months: int = 36,
    backfill_months: int = 0,
    extractors: Iterable[SupplierExtractor] | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Any] = asyncio.sleep,
) -> _Summary:
    """Fetch every card, store what changed, prune the old, report."""
    now = now or datetime.now(UTC)
    today = now.astimezone(_BRUSSELS).date()
    seen_month = _month_id(today.year, today.month)
    out.mkdir(parents=True, exist_ok=True)
    readme = out / "README.md"
    if not readme.exists():
        readme.write_text(_README, encoding="utf-8")
    summary = _Summary()
    memo = _RecordingMemo()
    targets = _targets(
        all_extractors() if extractors is None else extractors, only or set(), today
    )
    async with aiohttp.ClientSession() as session:
        with memoise_text_fetches(memo):
            for ex, contract, region in targets:
                label = f"{ex.id}/{contract}/{region}"
                memo.touched.clear()
                try:
                    snap = await _fetch_card(
                        lambda: ex.fetch(session, contract, region), sleep
                    )
                except Exception as err:  # noqa: BLE001 - one card must not stop the walk
                    summary.failed.append(f"{label}: {type(err).__name__}: {err}")
                    continue
                sources = [
                    _source_entry(key, _write_text(out, seen_month, memo[key]))
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
                if fetch_for_month is None:
                    continue
                for back in range(1, backfill_months + 1):
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
                        continue
                    if past is None or past.provisional:
                        # Not out yet, past the horizon, or still carrying an
                        # estimate: leave the month for a later backfill.
                        summary.absent += 1
                        continue
                    sources = [
                        _source_entry(key, _write_text(out, seen_month, memo[key]))
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
    removed = _prune(out, keep_months, today)
    print(
        f"{summary.stored} stored, {summary.unchanged} unchanged, "
        f"{summary.backfilled} backfilled, {summary.absent} absent, "
        f"{len(summary.failed)} failed, {removed} pruned, "
        f"{len(targets)} cards asked"
    )
    for line in summary.failed:
        print(f"  failed {line[:300]}")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True, help="archive checkout")
    parser.add_argument(
        "--only", action="append", default=[], help="restrict to a supplier id"
    )
    parser.add_argument("--keep-months", type=int, default=36)
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
        )
    )
    return 0 if summary.stored or summary.unchanged else 1


if __name__ == "__main__":
    sys.exit(main())
