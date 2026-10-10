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
This module holds the other axis: a card as it stood in a given month.
Backfill and the year-to-date cost walk months, so these rows outnumber the
live ones by an order of magnitude, and they persist in the entry's own
store blob under ``monthly_cards`` (``coordinator_persist``).

For a closed month the repository's card archive is asked first: one small
JSON against a PDF download and a parse from the supplier. The supplier's
own ``fetch_for_month`` answers for the running month and for what the
archive does not hold, and settles a month the archive only caught while
it ran on a card indexed on that month (``_snapshot_for_month``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CARD_ARCHIVE_FIRST_MONTH,
    CARD_ARCHIVE_URL,
    CONF_CARD_ARCHIVE,
    DEFAULT_CARD_ARCHIVE,
    SUPPLIER_CUSTOM,
)
from .providers._pdf import fetch_text, is_transient_fetch_error
from .providers._settle import is_settled
from .providers.base import (
    ArchivedSnapshotFetcher,
    ExtractorError,
    MonthSettler,
    SupplierExtractor,
    SupplierSnapshot,
)
from .snapshot_codec import (
    _DEGRADED_MIN_SCHEMA_VERSION,
    _SNAPSHOT_SCHEMA_VERSION,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from .snapshot_resolve import _resolve_snapshot
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
from .spot_stats import _injection_on_month_mean

_LOGGER = logging.getLogger(__name__)


def _asked_while_it_ran(
    stamped: datetime | None, year_month: date, today: date
) -> bool:
    """Whether a cached row of a month that has since closed was asked for
    while that month was still running.

    A provisional row of that kind answers a question about the running
    month, not the closed one: "no card can be addressed by date yet" (Bolt's
    variable folder, TotalEnergies, Ecofix), or the estimate a month-indexed
    card printed. Kept on its TTL it outlived the 1st by up to a day, and with
    the live card moved on the closed month was billed on the new month's
    card: 0,0487 EUR/kWh over on a Bolt Variable September for most of 1
    October, and for good wherever a backfill ran meanwhile, while the
    archive, which held the month, was never asked.
    """
    if stamped is None or (year_month.year, year_month.month) >= (
        today.year,
        today.month,
    ):
        return False
    after = date(
        year_month.year + (year_month.month == 12), year_month.month % 12 + 1, 1
    )
    return dt_util.as_local(stamped).date() < after


def archived_months_present(
    hass: HomeAssistant,
    supplier: str,
    contract: str,
    region: str,
    months: Sequence[date],
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
    months: Sequence[date],
) -> dict[str, dict[str, Any]]:
    """Serialise this contract's SETTLED per-month archive rows for the Store.

    The per-month cache lives in memory, so every Home Assistant restart used
    to re-fetch one archived card per elapsed month. That is one PDF apiece,
    about 25 s each for Frank Energie on a Raspberry Pi, and it is what issue
    #88 spent its bootstrap budget on. A closed month's card is a historical
    fact, so writing it to disk retires that cost for good rather than merely
    moving it off the setup path.

    The running month's card is never written: it can still be corrected,
    and it is asked for again on every read anyway. A closed month's row
    that is still provisional, waiting on the index the next card prints or
    parsed by the archive under the schema before the running one, is
    written marked ``_provisional`` and restored as provisional, so it is
    asked again on its TTL exactly as in memory and never served as settled.
    Left out, a restart in the day after a schema bump had no card for those
    months, and its first tick, which reads the meters but may not fetch,
    billed them on the current card until the fill landed.

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
            # every tick anyway, and nor once that month has closed, since
            # it is asked again on the first read after the close.
            if (
                stamp is None
                or (month.year, month.month) >= running
                or _asked_while_it_ran(stamp, month, today)
            ):
                continue
            out[month_id] = {"_cached_at": stamp.isoformat(), "_absent": True}
            continue
        if (month.year, month.month) >= running:
            continue
        if snap.provisional and (
            stamp is None or _asked_while_it_ran(stamp, month, today)
        ):
            # Like the marker above: a row asked while its month ran answers
            # for the running month and is asked again once it has closed.
            continue
        out[month_id] = _snapshot_to_dict(snap, stamp or dt_util.utcnow())
        if snap.provisional:
            out[month_id]["_provisional"] = True
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
    these months as soon as ``_SNAPSHOT_SCHEMA_VERSION`` moves. And a row for
    what is now the running month is dropped too: the file may be older than
    the clock is, and the running month's card must be re-asked whatever the
    disk says. A row written as provisional comes back provisional, with the
    time it was cached, so the month cache asks for it again on its TTL.
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
            if _asked_while_it_ran(stamp, month, today):
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
        if (month.year, month.month) >= (today.year, today.month):
            continue
        if data.get("_provisional") is True:
            snap = replace(snap, provisional=True)
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
    # The row is the card as the daily walk caught it while it was current
    # (``_via: live``), rather than what the supplier's own archive answered
    # for the month once it had closed (``_via: archive``). For most cards the
    # two are the same document. For one indexed on its own month they are
    # not: see ``_awaits_settlement``.
    captured_live: bool = False


def _settles_after_its_month(snap: SupplierSnapshot) -> bool:
    """Whether a card's figures for its own month are an estimate the month
    itself settles.

    A leg indexed on the delivery month's mean cannot print that mean while
    the month runs, so the card prints last month's and the supplier invoices
    the month on its own once it is known: Eneco and EBEM name it on the next
    card, Trevion prints both of its indices there, and Mega's next card states
    the figures it billed "pour le mois de" the one before. The parser marks
    each such leg. It is not the whole test at runtime: a supplier whose
    month lookup answers with the same card has nothing to settle, which is
    what ``SupplierExtractor.settles_on_next_card`` says.
    """
    return bool(getattr(snap.energy, "month_indexed", False)) or (
        _injection_on_month_mean(snap)
    )


def _awaits_settlement(archived: ArchivedCard) -> bool:
    """Whether an archive row for a closed month still carries the estimate.

    A row caught live on a month-indexed card holds what the card printed
    while the month ran. The archive keeps it as the month's card, which it
    is, but the month did not bill at it: from September 2026 every closed
    month is such a row until the daily walk has asked the supplier's own
    archive for it again, and served as settled it billed a keyless Eneco,
    EBEM or Mega September 9 to 10 EUR under what was invoiced, for good.
    """
    return archived.captured_live and _settles_after_its_month(archived.snapshot)


async def _settled_by_supplier(
    session: aiohttp.ClientSession,
    fetch_for_month: ArchivedSnapshotFetcher,
    contract: str,
    region: str,
    year_month: date,
    archived: SupplierSnapshot,
) -> SupplierSnapshot:
    """The month as the supplier settled it, or the archive row meanwhile.

    The supplier's own path is what settles a month on the card after it, so
    it is asked. A settled answer replaces the row. An answer still waiting on
    that card, or a failure to ask, keeps the row but marks it provisional, so
    it bills now, is asked again on the provisional TTL and is never stored
    as settled. A supplier that holds no card for the month has nothing better
    to offer, and the row stands as it is.
    """
    try:
        own = await fetch_for_month(session, contract, region, year_month)
    except Exception as err:  # the archive row still bills the month
        _LOGGER.debug(
            "settling %s/%s/%04d-%02d failed: %s",
            contract,
            region,
            year_month.year,
            year_month.month,
            err,
        )
        return replace(archived, provisional=True)
    if own is None:
        return archived
    if own.provisional:
        return replace(archived, provisional=True)
    return own


async def _settled_in_place(
    session: aiohttp.ClientSession,
    settle: MonthSettler,
    contract: str,
    region: str,
    year_month: date,
    held: SupplierSnapshot,
) -> SupplierSnapshot:
    """``held`` re-priced in place on its month's index (``settle``), or
    ``held`` provisional when that cannot be had yet or failed, so it bills
    now and is asked again. Whatever made ``held`` provisional still holds."""
    try:
        settled = await settle(
            session, contract, region, year_month, replace(held, provisional=False)
        )
    except Exception as err:  # the held card still bills the month
        _LOGGER.debug(
            "settling %s/%s/%04d-%02d in place failed: %s",
            contract,
            region,
            year_month.year,
            year_month.month,
            err,
        )
        return replace(held, provisional=True)
    return replace(settled, provisional=settled.provisional or held.provisional)


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
        # provisional row is re-asked daily and stored only as provisional.
        snapshot = replace(snapshot, provisional=True)
    return ArchivedCard(
        snapshot=snapshot,
        read_by_ocr=_row_read_by_ocr(row),
        captured_live=row.get("_via", "live") == "live",
    )


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
    if not _archive_allowed(entry):
        return None
    return await _archived_card_from_github(
        session, supplier, contract, region, today.replace(day=1)
    )


def month_before(day: date) -> date:
    """The first day of the month before ``day``'s."""
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


async def card_of_month_before(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    day: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the month before ``day``'s, for an entry left
    with no card. Honours the card-archive box like the reader above.

    A withdrawn product whose card the supplier took down is priced off the
    last month it was sold, the month before ``withdrawn``.
    """
    if not _archive_allowed(entry):
        return None
    return await _archived_card_from_github(
        session, supplier, contract, region, month_before(day)
    )


# How far back the card archive is asked for a card to stand in, a month at a
# time: as far as it keeps rows.
_ARCHIVE_MONTHS_BACK = 12


async def newest_archived_card(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> tuple[date, ArchivedCard] | None:
    """The newest row the archive holds for this card, and its month.

    This month's row first, then back a month at a time, stopping at the
    first one that holds a card. For an entry whose supplier's card cannot be
    had or has gone stale: the archive walks every supplier daily, and its
    row may well be newer than what the supplier answered this entry. A
    transient failure propagates, since a month it could not read may still
    hold one. Honours the card-archive box like the readers above.
    """
    if not _archive_allowed(entry):
        return None
    month = today.replace(day=1)
    for _ in range(_ARCHIVE_MONTHS_BACK):
        archived = await _archived_card_from_github(
            session, supplier, contract, region, month
        )
        if archived is not None:
            return month, archived
        month = month_before(month)
    return None


def _archive_allowed(entry: ConfigEntry | None) -> bool:
    """Whether the entry lets the integration read the repository's card
    archive. The box exists so a household can keep it from contacting
    GitHub at all; a caller with no entry in hand keeps the default."""
    return entry is None or bool(
        entry.data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE)
    )


def _card_archive_may_hold(
    extractor: SupplierExtractor,
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
    if not _archive_allowed(entry):
        return False
    month = (year_month.year, year_month.month)
    if month >= (today.year, today.month):
        return False
    return extractor.fetch_for_month is not None or month >= CARD_ARCHIVE_FIRST_MONTH


def _month_card_retrievable(
    extractor: SupplierExtractor,
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


def month_card_failed(
    hass: HomeAssistant, supplier: str, contract: str, region: str, year_month: date
) -> bool:
    """Whether ``_snapshot_for_month`` last handed back the current card for
    this month because reading the month's own card failed.

    It hands back the very same object when no archive holds the month, which
    is a fact about the month, and while a failed read waits out its marker,
    which is not: the archive may well hold it half an hour later. The live
    walk heals on its own next tick; a caller that keeps what it priced (the
    backfill) must tell the two apart, and only the caches can. A cached row,
    a card or ``None``, is an answer; a fresh failure marker with no row is a
    failed read.
    """
    key = _month_key(supplier, contract, region, year_month)
    if key in _monthly_snapshots(hass):
        return False
    failed_at = _monthly_failed_fetches(hass).get(key)
    return failed_at is not None and dt_util.utcnow() - failed_at < _MONTHLY_FAILURE_TTL


def month_card_cached(
    hass: HomeAssistant, supplier: str, contract: str, region: str, year_month: date
) -> bool:
    """Whether the month cache holds an answer for this month: its card, or
    ``None`` for a month no archive has. Either is final, so a caller under
    ``cached_only`` can tell it from a row that was simply never fetched."""
    return _month_key(supplier, contract, region, year_month) in _monthly_snapshots(
        hass
    )


def _month_key(
    supplier: str, contract: str, region: str, year_month: date
) -> tuple[str, str, str, str]:
    """The month cache's key for one contract's card."""
    return (supplier, contract, region, f"{year_month.year:04d}-{year_month.month:02d}")


async def _snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: SupplierSnapshot,
    entry: ConfigEntry | None = None,
    *,
    cached_only: bool = False,
    current_raw: SupplierSnapshot | None = None,
) -> SupplierSnapshot:
    """The card ``year_month`` is billed on: its own (``month_card``), or
    the current card standing in for it.

    The stand-in is ``current_snapshot`` itself, unless the caller hands the
    card it was resolved from as ``current_raw``: the two federal levies are
    then resolved for the delivery month (``_proxy_for_month``), since the
    law and not the card decides them, and a January bill on October's card
    still owes January's. Whether the answer is a stand-in is ``month_card``
    returning None, never the identity of what comes back.
    """
    card = await month_card(
        hass,
        session,
        extractor,
        contract,
        region,
        year_month,
        entry,
        cached_only=cached_only,
    )
    if card is not None:
        return card
    if entry is None or current_raw is None:
        return current_snapshot
    return _proxy_for_month(entry, current_snapshot, current_raw, year_month)


def _proxy_for_month(
    entry: ConfigEntry,
    current: SupplierSnapshot,
    raw: SupplierSnapshot,
    year_month: date,
) -> SupplierSnapshot:
    """``current`` standing in for ``year_month``, with the federal levies
    that month owes.

    ``current`` was resolved for today, so its energy contribution is struck
    out from August 2026 and its excise is the flat August rate, which a
    January to July month billed on it did not owe: 4 to 7 EUR a year on
    the TotalEnergies and Ecofix months no archive holds. Only those two
    figures are taken from ``raw`` resolved for the month, and only where
    they differ from ``raw`` resolved for today, so everything else the
    caller applied to ``current`` stands, and an excise picked from a band
    by the yearly volume is left alone. Identity for every month whose
    levies match today's.
    """
    today = _resolve_snapshot(entry, raw).taxes
    month = _resolve_snapshot(entry, raw, delivery_month=year_month).taxes
    changes = {
        name: getattr(month, name)
        for name in ("energy_contribution", "federal_excise")
        if getattr(month, name) != getattr(today, name)
    }
    if not changes:
        return current
    return replace(current, taxes=replace(current.taxes, **changes))


async def month_card(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    year_month: date,
    entry: ConfigEntry | None = None,
    *,
    cached_only: bool = False,
) -> SupplierSnapshot | None:
    """The month's own card, resolved for the month, or None where the
    caller has to stand its current card in for it.

    Three tiers, in order. The repository's card archive
    (``_archived_card_from_github``) comes first for a closed month it can
    hold (``_card_archive_may_hold``): one small JSON per month, holding the
    supplier archives mirrored and every card captured live, against a PDF
    download and a parse per month from the supplier, which is what made
    the first year-to-date fill of a Frank or Bolt entry minutes on a
    Raspberry Pi. The supplier's own archive (``fetch_for_month``) answers
    for what the project's archive does not hold: the running month, a
    month before its horizon, a row it cannot serve. Mega and Luminus refuse
    the running month, whose archived edition can lag behind a corrected live
    card, so that month falls to the current snapshot. It is also asked over a
    row the archive caught live on a card indexed on its own month, which
    holds the estimate the card printed rather than what the month settled
    at (``_awaits_settlement``); a supplier that settles in place
    (``settle_month``, Luminus) re-prices the row it holds instead, whichever
    tier gave it. None when neither has the month: the current card is then
    the proxy, and it is the running month's card by definition, so that
    month never reaches the repository.
    A blip reading the archive is not "no card": the supplier is still
    asked, and a month neither could give is retried on the failure marker
    rather than cached. Both answer None, so a caller that keeps what it
    priced asks ``month_card_failed`` which it got.

    Caches the result per (supplier, contract, region, YYYY-MM): a hit
    skips the network round-trip on subsequent refreshes. ``None`` is
    cached too: "no archive has this month" is a stable signal we
    shouldn't keep re-asking.

    The cache is shared across entries, so it holds archived cards exactly
    as parsed and each caller's own VAT / consumption facts are applied on
    the way out.

    ``cached_only`` answers from the cache and never reaches the network: a
    month with no row answers None, the same proxy a supplier without an
    archive gets. The first coordinator tick asks for it
    because that tick runs inside config-entry setup, and one archived card
    per elapsed month is not something setup can afford (Frank Energie's
    cards take ~25 s each to lay out on a Raspberry Pi, so a September start
    spent ~226 s there and Home Assistant cancelled the whole of bootstrap
    stage 2 over it). The warm-up that follows fills the cache off the setup
    path and asks for a refresh.
    """

    def resolved(snap: SupplierSnapshot | None) -> SupplierSnapshot | None:
        if snap is None:
            return None
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
                and not _asked_while_it_ran(stamped, year_month, today)
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
        # process even after the card appeared. One asked while its month was
        # still running is re-asked once that month has closed, whatever its
        # age.
        cache.pop(cache_key, None)
        fetched_at.pop(cache_key, None)
    if extractor.id == SUPPLIER_CUSTOM:
        # Assembled from the entry rather than published: no archive holds
        # a card for it, whatever the month, so this row is never provisional.
        cache[cache_key] = None
        fetched_at[cache_key] = dt_util.utcnow()
        return None
    if cached_only:
        # Uncached and no fetch allowed: the documented fallback. Nothing is
        # written to the cache, so the warm-up still asks the archives.
        return None
    # Negative cache: a transient archive failure is intentionally NOT
    # written to ``cache`` (a cached None means "no archive has this
    # month"); without this secondary marker the hourly YTD walk would
    # re-attempt every uncached month against a flaky CDN. Skip the retry
    # while the marker is fresh; the current card is the documented proxy
    # for non-archive months.
    last_fail = failed.get(cache_key)
    if last_fail is not None and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL:
        return None
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
            return None
        fetch_failed = False
        archive_failed = False
        snap: SupplierSnapshot | None = None
        archived: ArchivedCard | None = None
        if _card_archive_may_hold(extractor, year_month, today, entry):
            try:
                archived = await _archived_card_from_github(
                    session, extractor.id, contract, region, year_month
                )
            except (
                Exception
            ) as err:  # a blip on the archive must not cost the supplier tier
                _LOGGER.debug(
                    "card archive read failed for %s/%s/%s/%s: %s",
                    extractor.id,
                    contract,
                    region,
                    cache_key[3],
                    err,
                )
                archive_failed = True
        if archived is not None:
            snap = archived.snapshot
            if (
                extractor.fetch_for_month is not None
                and extractor.settles_on_next_card
                and extractor.settle_month is None
                and _awaits_settlement(archived)
            ):
                snap = await _settled_by_supplier(
                    session,
                    extractor.fetch_for_month,
                    contract,
                    region,
                    year_month,
                    snap,
                )
        if snap is None and extractor.fetch_for_month is not None:
            try:
                snap = await extractor.fetch_for_month(
                    session, contract, region, year_month
                )
            except Exception as err:  # per-month fetch must never break the year loop
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
        if (
            snap is not None
            and extractor.settle_month is not None
            and _settles_after_its_month(snap)
            and not is_settled(snap)
        ):
            snap = await _settled_in_place(
                session, extractor.settle_month, contract, region, year_month, snap
            )
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
