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

"""One month's card: reading it, storing it, and serving it back.

The shared cache in ``snapshot_store`` holds today's card per supplier tuple.
This module holds the other axis: a card as it stood in a given month, read
off the repository's card archive when the supplier no longer publishes it.
Backfill and the year-to-date cost walk months, so these rows outnumber the
live ones by an order of magnitude and persist to their own store.

The archive is a fallback, not a source: a month is read from it only when
the supplier's own card cannot be had, and a row that came from it says so,
because a figure the user cannot check against a published PDF needs to be
labelled as what it is.
"""

from __future__ import annotations

from .const import (
    CARD_ARCHIVE_FIRST_MONTH,
    CARD_ARCHIVE_URL,
    CONF_CARD_ARCHIVE,
    DEFAULT_CARD_ARCHIVE,
    SUPPLIER_CUSTOM,
)
from .providers._pdf import fetch_text, is_transient_fetch_error
from .providers.base import ExtractorError, SupplierExtractor, SupplierSnapshot
from .snapshot_codec import (
    _DEGRADED_MIN_SCHEMA_VERSION,
    _SNAPSHOT_SCHEMA_VERSION,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from .snapshot_resolve import _resolve_snapshot
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp
import json
import logging

from .snapshot_store import (
    _MONTHLY_FAILURE_TTL,
    _MONTHLY_PROVISIONAL_TTL,
    _month_row_is_provisional,
    _monthly_failed_fetches,
    _monthly_fetched_at,
    _monthly_lock,
    _monthly_snapshots,
    _tuple_generation,
)

_LOGGER = logging.getLogger(__name__)


def archived_months_present(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: "Sequence[date]",
) -> set[tuple[int, int]]:
    """Which of ``months`` this contract has a REAL archived card for.

    Read-only over the cache ``_snapshot_for_month`` already fills, so it costs
    nothing beyond the walk that has happened anyway.

    The distinction matters because ``fetch_for_month is not None`` is a
    property of the SUPPLIER, not of the contract or the month, and seventeen
    candidate contracts pass it and then have no month-addressable card:
    Bolt's whole variable folder returns None before any I/O because its cards
    carry a version suffix rather than a date, and the year-to-date path
    silently substitutes the current card for every past month. That reads as
    a real figure and is 8,5% to 23,3% out - against a row-to-row gap of about
    16 EUR, enough to move a row several places in a column the user can sort,
    with nothing on screen to tell the two kinds of row apart.

    A month is present only when the cache holds an actual snapshot for it.
    A cached ``None`` is the proxy case and is deliberately not counted.
    """
    cache = _monthly_snapshots(hass)
    out: set[tuple[int, int]] = set()
    for month in months:
        key = (supplier, contract, region, f"{month.year:04d}-{month.month:02d}")
        if cache.get(key) is not None:
            out.add((month.year, month.month))
    return out


def monthly_rows_to_store(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: "Sequence[date]",
) -> dict[str, dict[str, Any]]:
    """Serialise this contract's SETTLED per-month archive rows for the Store.

    The per-month cache lives in memory, so every Home Assistant restart used
    to re-fetch one archived card per elapsed month. That is one PDF apiece,
    about 25 s each for Frank Energie on a Raspberry Pi, and it is what issue
    #88 spent its bootstrap budget on. A closed month's card is a historical
    fact, so writing it to disk retires that cost for good rather than merely
    moving it off the setup path.

    Only rows ``_month_row_is_provisional`` calls settled are written: the
    running month's card can still be corrected, and a row the extractor
    flagged provisional is waiting on the index the next card prints. Neither
    is a fact worth outliving the process.

    A CLOSED month that came back with no card is written too, as a marker
    carrying the instant it was asked. Establishing that costs the same
    download and parse as a card does: Frank Energie publishes nothing for
    March 2026 and spends 24 s saying so, and dropping the marker made every
    restart pay it again, for ever. It is not a permanent answer: a supplier
    publishing in arrears turns "not out yet" into a real card days later, so
    the marker expires on the same ``_MONTHLY_PROVISIONAL_TTL`` it does in
    memory, which the restore applies.

    ``months`` bounds the write to the months the year-to-date walk actually
    asks for, so a blob does not accumulate every month an entry has ever
    walked (about 5 KB per row).
    """
    today = dt_util.now().date()
    running = (today.year, today.month)
    cache = _monthly_snapshots(hass)
    stamped = _monthly_fetched_at(hass)
    out: dict[str, dict[str, Any]] = {}
    for month in months:
        month_id = f"{month.year:04d}-{month.month:02d}"
        cache_key = (supplier, contract, region, month_id)
        if cache_key not in cache:
            continue
        snap = cache[cache_key]
        stamp = stamped.get(cache_key)
        if snap is None:
            # Written on its stamp alone, so a marker with no stamp, which
            # nothing writes today, is skipped rather than restored as
            # ageless. Never for the running month: that one is re-asked on
            # every tick anyway.
            if stamp is None or (month.year, month.month) >= running:
                continue
            out[month_id] = {"_cached_at": stamp.isoformat(), "_absent": True}
            continue
        if _month_row_is_provisional(snap, month, today):
            continue
        out[month_id] = _snapshot_to_dict(snap, stamp or dt_util.utcnow())
    return out


def restore_monthly_rows(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    rows: dict[str, Any],
) -> int:
    """Seed the per-month archive cache from a stored blob, returning the
    number of months restored.

    A row already in the cache is left alone: this process fetched it, which
    outranks what the last one wrote. A row that no longer parses, or that was
    written under an older snapshot schema, is dropped rather than migrated,
    the same healing gate the live snapshot uses, so a parser fix reaches
    these months as soon as ``_SNAPSHOT_SCHEMA_VERSION`` moves. And a row that
    is no longer settled is dropped too: the file may be older than the clock
    is, and the running month's card must be re-asked whatever the disk says.
    """
    today = dt_util.now().date()
    cache = _monthly_snapshots(hass)
    stamped = _monthly_fetched_at(hass)
    restored = 0
    for month_id, data in rows.items():
        if not isinstance(month_id, str) or not isinstance(data, dict):
            continue
        try:
            month = date(int(month_id[:4]), int(month_id[5:7]), 1)
        except ValueError:
            continue
        cache_key = (supplier, contract, region, month_id)
        if cache_key in cache:
            continue
        if data.get("_absent"):
            # "The archive had nothing when asked", and asking again is what
            # it costs to find out. Honour it only while the in-memory rule
            # would have, so a card published in arrears is still picked up a
            # day later rather than never.
            try:
                stamp = datetime.fromisoformat(data["_cached_at"])
            except (KeyError, TypeError, ValueError):
                continue
            if dt_util.utcnow() - stamp >= _MONTHLY_PROVISIONAL_TTL:
                continue
            if (month.year, month.month) >= (today.year, today.month):
                # A marker for the running month is never written, but a blob
                # written before midnight on the 1st carries one for what has
                # since become the running month.
                continue
            cache[cache_key] = None
            stamped[cache_key] = stamp
            restored += 1
            continue
        try:
            snap = _snapshot_from_dict(data)
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.debug("discarding stored archive row %s: %s", month_id, err)
            continue
        if _month_row_is_provisional(snap, month, today):
            continue
        cache[cache_key] = snap
        try:
            stamped[cache_key] = datetime.fromisoformat(data["_cached_at"])
        except (KeyError, TypeError, ValueError):
            stamped[cache_key] = dt_util.utcnow()
        restored += 1
    return restored


def _row_read_by_ocr(row: dict[str, Any]) -> bool:
    """Whether any document this row read had to be read off its pixels.

    The archive marks the SOURCE, not the row: ``ocr`` sits beside the pdf
    digest it describes, so a row that reads a readable card and an unreadable
    one says which was which. This is the row-level question the Repairs card
    asks, answered by looking.
    """
    sources = row.get("_sources")
    if not isinstance(sources, list):
        return False
    return any(isinstance(source, dict) and source.get("ocr") for source in sources)


@dataclass(frozen=True)
class ArchivedCard:
    """One month's row off the repository's card archive.

    ``read_by_ocr`` says the card that month carried no text layer and the
    archive walk read it off its pixels. It rides beside the snapshot because
    the figures are the same shape either way and only the user needs to know
    the difference.

    Read off the sources rather than a mark on the row, because the fact
    belongs to a document: a row can read two, and only one of them need be
    the unreadable one (137 of the archive's rows read two cards). The row
    once carried its own derived copy as ``_ocr``; nothing kept it in step
    with the sources, and it is gone.
    """

    snapshot: SupplierSnapshot
    read_by_ocr: bool


async def _archived_card_from_github(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    year_month: date,
) -> ArchivedCard | None:
    """The card the repository's own archive holds for this month, or None.

    ``archive_cards.yml`` stores what every extractor parsed, daily, in
    the ``be_price_cards`` repository (``scripts/archive_cards.py``), and
    mirrors the supplier archives into it, so a closed month is one small
    JSON there against a PDF download and a parse from the supplier: that is
    why the month cache asks here first. The row was parsed by the extractor
    of its day and re-parsed by the daily walk the day after a parser change,
    so it is read at the degraded schema floor, the page-image replay's
    position: for a past month a row parsed under an older schema beats the
    current card as a proxy, and the supplier's own archive still answers for
    a month the project's archive does not hold.

    None on a 404, which is a month the archive predates, a contract it does
    not cover or a row never written, and on a row that no longer
    decodes. A transient failure propagates so the caller's negative cache
    treats it like a supplier archive blip and asks again later.
    """
    url = (
        f"{CARD_ARCHIVE_URL}/{supplier}/{contract}/{region}/"
        f"{year_month.year:04d}-{year_month.month:02d}.json"
    )
    try:
        body = await fetch_text(session, url)
    except ExtractorError as err:
        if is_transient_fetch_error(str(err)):
            raise
        return None
    try:
        row = json.loads(body)
        snapshot = _snapshot_from_dict(
            row, min_schema_version=_DEGRADED_MIN_SCHEMA_VERSION
        )
    except (KeyError, TypeError, ValueError) as err:
        _LOGGER.debug("card archive row %s does not decode: %s", url, err)
        return None
    if row.get("_schema_version", 1) < _SNAPSHOT_SCHEMA_VERSION:
        # Parsed before the running parser, and re-parsed on the archive's next
        # run. Good enough to bill until then, not to keep: cached as settled
        # it was persisted under the running schema and served from disk for
        # good, so a fix that reached the archive never reached the entry. A
        # provisional row is re-asked daily and never written.
        snapshot = replace(snapshot, provisional=True)
    return ArchivedCard(snapshot=snapshot, read_by_ocr=_row_read_by_ocr(row))


async def card_for_unreadable_month(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the RUNNING month, for a card nobody can read.

    The last resort, and reached only on a card that downloaded fine and
    carries no text layer. A supplier publishing page images leaves a parser
    nothing to work with, but the repository's daily walk reads those with an
    OCR engine and files what it gets, so the row is the one place a price
    for this month exists at all.

    Asking for the running month is exactly what ``_card_archive_may_hold``
    refuses, and rightly: while a card can be read the live parse is the
    better answer and the archive's copy is a day behind at best. That
    reasoning runs out when the card cannot be read, which is the only door
    into this function.

    Honours the entry's card-archive box, which exists so a household can
    keep the integration from contacting GitHub at all.
    """
    if entry is not None and not entry.data.get(
        CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE
    ):
        return None
    return await _archived_card_from_github(
        session, supplier, contract, region, today.replace(day=1)
    )


def _card_archive_may_hold(
    extractor: "SupplierExtractor",
    year_month: date,
    today: date,
    entry: ConfigEntry | None,
) -> bool:
    """Whether the repository's card archive may be asked for this month.

    Not when the entry has switched the archive off: that box exists so a
    household can keep the integration from contacting GitHub, and a caller
    with no entry in hand (the shared cache's own bookkeeping) keeps the
    default. Only a closed month: the running month's card is the one being
    served live, which is what the current snapshot holds, and the
    repository's copy of it is a day behind at best. And a supplier with no
    archive of its own has nothing in the project's archive from before
    ``CARD_ARCHIVE_FIRST_MONTH``: a backfill only mirrors a supplier's own
    archive, so asking for an earlier month is a 404 a day for nothing.
    """
    if entry is not None and not entry.data.get(
        CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE
    ):
        return False
    month = (year_month.year, year_month.month)
    if month >= (today.year, today.month):
        return False
    return extractor.fetch_for_month is not None or month >= CARD_ARCHIVE_FIRST_MONTH


def _month_card_retrievable(
    extractor: "SupplierExtractor",
    year_month: date,
    today: date,
    entry: ConfigEntry | None,
) -> bool:
    """Whether ``_snapshot_for_month`` may find the month's own card rather
    than hand back the current one as its proxy: from the supplier's archive,
    or from the repository's, which holds every supplier's cards from
    ``CARD_ARCHIVE_FIRST_MONTH`` whether or not the supplier keeps one."""
    return extractor.fetch_for_month is not None or _card_archive_may_hold(
        extractor, year_month, today, entry
    )


async def _snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry | None = None,
    *,
    cached_only: bool = False,
) -> "SupplierSnapshot":
    """Resolve the historical snapshot for ``year_month`` or fall back.

    Three tiers, in order. The repository's card archive
    (``_archived_card_from_github``) comes first for a closed month it can
    hold (``_card_archive_may_hold``): one small JSON per month, holding the
    supplier archives mirrored and every card captured live, against a PDF
    download and a parse per month from the supplier, which is what made
    the first year-to-date fill of a Frank or Bolt entry minutes on a
    Raspberry Pi. The supplier's own archive (``fetch_for_month``) answers
    for what the project's archive does not hold: the running month, a
    month before its horizon, a row it cannot serve. The current snapshot is
    the proxy when neither has the month, and it is the running month's card
    by definition, so that month never reaches the repository.
    A blip reading the archive is not "no card": the supplier is still
    asked, and a month neither could give is retried on the failure marker
    rather than cached.

    Caches the result per (supplier, contract, region, YYYY-MM): a hit
    skips the network round-trip on subsequent refreshes. ``None`` is
    cached too: "no archive has this month" is a stable signal we
    shouldn't keep re-asking.

    The cache is shared across entries, so it holds archived cards exactly
    as parsed and each caller's own VAT / consumption facts are applied on
    the way out. ``current_snapshot`` is the caller's own and already
    resolved, so it is passed through untouched.

    ``cached_only`` answers from the cache and never reaches the network: a
    month with no row falls back to the current snapshot, the same proxy a
    supplier without an archive gets. The first coordinator tick asks for it
    because that tick runs inside config-entry setup, and one archived card
    per elapsed month is not something setup can afford (Frank Energie's
    cards take ~25 s each to lay out on a Raspberry Pi, so a September start
    spent ~226 s there and Home Assistant cancelled the whole of bootstrap
    stage 2 over it). The warm-up that follows fills the cache off the setup
    path and asks for a refresh.
    """

    def resolved(snap: "SupplierSnapshot | None") -> "SupplierSnapshot":
        if snap is None:
            return current_snapshot
        return (
            snap
            if entry is None
            else _resolve_snapshot(entry, snap, delivery_month=year_month)
        )

    cache = _monthly_snapshots(hass)
    failed = _monthly_failed_fetches(hass)
    cache_key = (
        extractor.id,
        contract,
        region,
        f"{year_month.year:04d}-{year_month.month:02d}",
    )
    fetched_at = _monthly_fetched_at(hass)
    today = dt_util.now().date()
    if cache_key in cache:
        row = cache[cache_key]
        stamped = fetched_at.get(cache_key)
        if (
            cached_only
            or not _month_row_is_provisional(row, year_month, today)
            or (
                stamped is not None
                and dt_util.utcnow() - stamped < _MONTHLY_PROVISIONAL_TTL
            )
        ):
            # A caller that cannot fetch keeps the row it has, expired or not:
            # dropping it here would forfeit a month it is already holding and
            # hand back the current card in its place.
            return resolved(row)
        # Provisional and past its TTL: drop the row and re-ask. Without this
        # a supplier correcting the running month's card moved current_price
        # within a day while the year-to-date and every backfilled row kept
        # billing the vintage first cached at startup, and a month whose card
        # had not published yet stayed "no archive" for the life of the HA
        # process even after the card appeared.
        cache.pop(cache_key, None)
        fetched_at.pop(cache_key, None)
    if extractor.id == SUPPLIER_CUSTOM:
        # Assembled from the entry rather than published: no archive holds
        # a card for it, whatever the month, so this row is never provisional.
        cache[cache_key] = None
        fetched_at[cache_key] = dt_util.utcnow()
        return current_snapshot
    if cached_only:
        # Uncached and no fetch allowed: the documented fallback. Nothing is
        # written to the cache, so the warm-up still asks the archives.
        return current_snapshot
    # Negative cache: a transient archive failure is intentionally NOT
    # written to ``cache`` (a cached None means "no archive has this
    # month"); without this secondary marker the hourly YTD walk would
    # re-attempt every uncached month against a flaky CDN. Skip the retry
    # while the marker is fresh; current_snapshot is the documented proxy
    # for non-archive months.
    last_fail = failed.get(cache_key)
    if last_fail is not None and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL:
        return current_snapshot
    gen_at_entry = _tuple_generation(hass, cache_key)
    async with _monthly_lock(hass, cache_key):
        # Re-check under the lock so the second waiter doesn't repeat
        # what the first just did.
        if cache_key in cache:
            return resolved(cache[cache_key])

        last_fail = failed.get(cache_key)
        if (
            last_fail is not None
            and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL
        ):
            return current_snapshot
        fetch_failed = False
        archive_failed = False
        snap: SupplierSnapshot | None = None
        if _card_archive_may_hold(extractor, year_month, today, entry):
            try:
                archived = await _archived_card_from_github(
                    session, extractor.id, contract, region, year_month
                )
                snap = archived.snapshot if archived is not None else None
            except Exception as err:  # noqa: BLE001 - a blip on the archive must not cost the supplier tier
                _LOGGER.debug(
                    "card archive read failed for %s/%s/%s/%s: %s",
                    extractor.id,
                    contract,
                    region,
                    cache_key[3],
                    err,
                )
                archive_failed = True
        if snap is None and extractor.fetch_for_month is not None:
            try:
                snap = await extractor.fetch_for_month(
                    session, contract, region, year_month
                )
            except Exception as err:  # noqa: BLE001 - per-month fetch must never break the year loop
                _LOGGER.debug(
                    "fetch_for_month failed for %s/%s/%s/%s: %s",
                    extractor.id,
                    contract,
                    region,
                    cache_key[3],
                    err,
                )
                snap = None
                fetch_failed = True
        if snap is None and archive_failed:
            # The archive may well hold the month; ask again on the failure
            # marker rather than cache a None the TTL would hold for a day.
            fetch_failed = True
        if fetch_failed:
            failed[cache_key] = dt_util.utcnow()
        # Skip the cache write if eviction ran during the await: the
        # tuple is no longer this entry's, and re-creating the row
        # would orphan it for any future re-add of the same tuple.
        # Also skip when the fetch raised: a transient error must not
        # be cached as "supplier doesn't archive this month", which is
        # the meaning a cached None carries here. Leaving the key
        # absent lets the next refresh retry instead of locking in
        # stale "uncredited" output until the entry reloads.
        if not fetch_failed and _tuple_generation(hass, cache_key) == gen_at_entry:
            cache[cache_key] = snap
            fetched_at[cache_key] = dt_util.utcnow()
    return resolved(snap)
