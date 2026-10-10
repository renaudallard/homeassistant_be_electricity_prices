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

"""The federal excise on electricity, read from the law.

Article 419, i), of the programme law of 27 December 2004 sets the excise a
household pays per MWh, and Justel serves its consolidated text. Two lines of
it matter here: the protected residential customer's rate and everyone
else's, under "consommation non-professionnelle". Each is a rate followed by
the steps the law has already voted ("A partir du 1er janvier 2027: ..."), so
a step is billed in its own month without anyone editing a constant.

The rate a step starts from is dated by the law too. Justel brackets every
passage an amendment wrote and titles the bracket with that amendment's entry
into force, so the date the current wording took effect is read off the
innermost bracket around it.

Justel keeps only the wording in force. A household rate the law no longer
prints is not here, so the card is read for the months before the first step
this holds. The protected customer has no card that prints it reliably, so
its two closed rates are typed in beside the law they came from. The table is held for the process, refreshed daily, and kept in every
entry's store so a restart without the network still knows the last one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from html import unescape
from itertools import pairwise
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util

from .const import EXCISE_LAW_URL
from .providers._pdf import FR_MONTHS, fetch_text
from .providers.base import ExtractorError

_LOGGER = logging.getLogger(__name__)

_REFRESH_S: Final = 24 * 3600
_FAILURE_RETRY_S: Final = 6 * 3600
# A rate outside these is a misread, not a rate: the highest the household
# rate has been is 50,33 EUR/MWh.
_MAX_EUR_PER_KWH: Final = 0.2

STANDARD: Final = "standard"
PROTECTED: Final = "protected"

_ARTICLE_RE = re.compile(
    r"NAME='Art\.419'(.*?)(?:NAME='Art\.419bis'|NAME='Art\.420')", re.S
)
# An amendment's bracket, opening and closing. The opening carries the
# amendment as a title: <L 2026-05-30/01, art. 38, 071; En vigueur : 01-08-2026>.
_OPEN_RE = re.compile(
    r"\[<sup>(?:(?!</sup>).)*?En vigueur\s*:\s*(\d{2})-(\d{2})-(\d{4})"
    r"(?:(?!</sup>).)*</sup>",
    re.S,
)
_CLOSE_RE = re.compile(r"\]<sup>(?:(?!</sup>).)*</sup>", re.S)
_OPEN = "\x01"
_CLOSE = "\x02"
_TAG_RE = re.compile(r"<[^>]+>")

_ELECTRICITY = "électricité du code NC 2716"
_NON_PROFESSIONAL_RE = re.compile(r"consommation non-professionnelle\s*:")
_PROTECTED_RE = re.compile(r"a\)\s*client protégé résidentiel")
_OTHERS_RE = re.compile(r"b\)\s*autres\s*:")
_STEP_RE = re.compile(
    r"A partir du (1er|\d{1,2}) (" + "|".join(FR_MONTHS) + r") (\d{4})\s*:"
)
_EXCISE_RE = re.compile(r"droit d['’]accise\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh")
_SPECIAL_RE = re.compile(
    r"droit d['’]accise spécial\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh"
)

# (kind) -> ((first day, EUR/kWh excluding VAT), ...), ascending.
_HELD: dict[str, tuple[tuple[date, float], ...]] = {}
_fetched_at: datetime | None = None
_failed_at: datetime | None = None


class ExciseLawError(ExtractorError):
    """The page could not be read as article 419."""


def _ascending(steps: list[tuple[date, float]]) -> bool:
    return all(a[0] < b[0] for a, b in pairwise(steps))


def _number(raw: str) -> float:
    return float(raw.replace(".", "").replace(",", "."))


def _rate(chunk: str) -> float:
    """The ordinary plus the special excise one paragraph sets, EUR/kWh."""
    if "tranche" in chunk:
        # A schedule by annual volume, which is what the household rate was
        # before August 2026. It is not one rate, and the card bills it.
        raise ExciseLawError("the rate is banded by volume")
    ordinary = _EXCISE_RE.findall(chunk)
    special = _SPECIAL_RE.findall(chunk)
    if len(ordinary) != 1 or len(special) != 1:
        raise ExciseLawError("no single excise rate")
    rate = (_number(ordinary[0]) + _number(special[0])) / 1000.0
    if not 0.0 <= rate < _MAX_EUR_PER_KWH:
        raise ExciseLawError(f"implausible excise {rate} EUR/kWh")
    return rate


def _in_force(text: str, at: int) -> date:
    """The entry into force of the innermost amendment around ``at``."""
    stack: list[date] = []
    pos = 0
    while pos < at:
        nxt = min(
            (i for i in (text.find(_OPEN, pos), text.find(_CLOSE, pos)) if i >= 0),
            default=-1,
        )
        if nxt < 0 or nxt >= at:
            break
        if text[nxt] == _OPEN:
            day, month, year = text[nxt + 1 : nxt + 11].split("-")
            stack.append(date(int(year), int(month), int(day)))
        elif stack:
            stack.pop()
        pos = nxt + 1
    if not stack:
        raise ExciseLawError("no entry into force around the rate")
    return stack[-1]


def _steps(text: str, start: int, end: int) -> tuple[tuple[date, float], ...]:
    """The rate set between ``start`` and ``end`` and the steps after it."""
    chunk = text[start:end]
    heads = list(_STEP_RE.finditer(chunk))
    bounds = [0, *(m.start() for m in heads), len(chunk)]
    # Dated where the rate itself is printed, not where its label starts: an
    # amendment that rewrites only the rate opens its bracket after the label,
    # and the label's own bracket is the older law that wrote the paragraph.
    special = _SPECIAL_RE.search(chunk, 0, bounds[1])
    if special is None:
        raise ExciseLawError("no special excise")
    out = [
        (_in_force(text, start + special.start()), _rate(chunk[bounds[0] : bounds[1]]))
    ]
    for i, head in enumerate(heads):
        day = 1 if head.group(1) == "1er" else int(head.group(1))
        when = date(int(head.group(3)), FR_MONTHS.index(head.group(2)) + 1, day)
        out.append((when, _rate(chunk[head.end() : bounds[i + 2]])))
    if not _ascending(out):
        raise ExciseLawError("steps out of order")
    return tuple(out)


def parse(page: str) -> dict[str, tuple[tuple[date, float], ...]]:
    """The two household rates article 419 sets, with their steps.

    Raises :class:`ExciseLawError` on anything that is not the article as
    expected, rather than reading half of it.
    """
    match = _ARTICLE_RE.search(page)
    if match is None:
        raise ExciseLawError("article 419 not found")
    marked = _CLOSE_RE.sub(_CLOSE, match.group(1))
    marked = _OPEN_RE.sub(lambda m: f"{_OPEN}{m[1]}-{m[2]}-{m[3]}", marked)
    text = " ".join(unescape(_TAG_RE.sub(" ", marked)).split())
    start = text.find(_ELECTRICITY)
    if start < 0:
        raise ExciseLawError("no electricity paragraph")
    footnotes = text.find("----------", start)
    end = footnotes if footnotes >= 0 else len(text)
    household = _NON_PROFESSIONAL_RE.search(text, start, end)
    if household is None:
        raise ExciseLawError("no non-professional consumption")
    protected = _PROTECTED_RE.search(text, household.end(), end)
    others = _OTHERS_RE.search(text, household.end(), end)
    if protected is None or others is None or others.start() < protected.end():
        raise ExciseLawError("no protected and other household rates")
    return {
        PROTECTED: _steps(text, protected.end(), others.start()),
        STANDARD: _steps(text, others.end(), end),
    }


def _rate_for(kind: str, month: date) -> float | None:
    first = month.replace(day=1)
    rate = None
    for when, value in _HELD.get(kind, ()):
        if when > first:
            break
        rate = value
    return rate


def standard_excise(month: date) -> float | None:
    """The household excise in force on ``month``'s first day, EUR/kWh
    excluding VAT, or ``None`` before the first step held."""
    return _rate_for(STANDARD, month)


# The protected customer's rates the law no longer prints, as (first day,
# EUR/kWh excluding VAT), and the day the wording read from Justel replaced
# them. Exempt before the law of 19 March 2023 (the CREG's Q4 2022 social
# tariff card says so), still 0 under its article 14 until 30 June 2023, then
# 23,62 EUR/MWh under its article 9, until the law of 30 May 2026. From the
# first quarter the CREG still publishes, which is as far back as the social
# tariff is billed.
_PROTECTED_CLOSED: Final = ((date(2022, 10, 1), 0.0), (date(2023, 7, 1), 0.02362))
_PROTECTED_CLOSED_UNTIL: Final = date(2026, 8, 1)


def protected_excise(month: date) -> float | None:
    """The protected residential customer's excise in force on ``month``'s
    first day, EUR/kWh excluding VAT, or ``None`` when it is not known: a
    month from August 2026 while the law has not been read, which is never
    billed at the rate it replaced."""
    rate = _rate_for(PROTECTED, month)
    if rate is not None:
        return rate
    first = month.replace(day=1)
    if first >= _PROTECTED_CLOSED_UNTIL:
        return None
    for when, value in _PROTECTED_CLOSED:
        if when <= first:
            rate = value
    return rate


def _section(rows: Any) -> tuple[tuple[date, float], ...]:
    out: list[tuple[date, float]] = []
    if not isinstance(rows, list):
        return ()
    for row in rows:
        try:
            when, rate = date.fromisoformat(row[0]), float(row[1])
        except (TypeError, ValueError, IndexError):
            return ()
        if not 0.0 <= rate < _MAX_EUR_PER_KWH:
            return ()
        out.append((when, rate))
    return tuple(out) if _ascending(out) else ()


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape :func:`held_table` writes), replacing what
    was held for each kind it carries."""
    for kind in (STANDARD, PROTECTED):
        steps = _section(table.get(kind))
        if steps:
            _HELD[kind] = steps


def restore(table: Mapping[str, Any]) -> None:
    """Hold ``table`` from an entry's store, for a kind nothing holds yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched the law, so a stored copy never replaces it.
    """
    for kind in (STANDARD, PROTECTED):
        steps = _section(table.get(kind))
        if steps and kind not in _HELD:
            _HELD[kind] = steps


def held_table() -> dict[str, list[list[Any]]]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    return {
        kind: [[when.isoformat(), rate] for when, rate in steps]
        for kind, steps in _HELD.items()
    }


async def ensure_excise_law(session: aiohttp.ClientSession) -> None:
    """Read the law once a day.

    A failure keeps what is held and is retried after ``_FAILURE_RETRY_S``;
    nothing here raises, since the caller is the coordinator tick.
    """
    global _fetched_at, _failed_at
    now = dt_util.utcnow()
    if _fetched_at is not None and (now - _fetched_at).total_seconds() < _REFRESH_S:
        return
    if _failed_at is not None and (now - _failed_at).total_seconds() < _FAILURE_RETRY_S:
        return
    try:
        table = parse(await fetch_text(session, EXCISE_LAW_URL, timeout=15))
    except ExciseLawError as err:
        # The page came back and is not the article: a layout change, or a
        # bot check served instead of the law. Worth a warning, since the
        # excise is read off the cards until it is fixed.
        _LOGGER.warning("The excise law could not be read: %s", err)
        _failed_at = now
        return
    except ExtractorError as err:
        # With nothing held, every card is billed the excise it prints, which
        # a stale card gets wrong, so say so where it is seen: once per retry,
        # since the backoff below spaces the attempts six hours apart. A held
        # table goes on billing the law, so a failure then is no news.
        log = _LOGGER.debug if _HELD else _LOGGER.warning
        log(
            "The excise law could not be fetched from Justel, so each card's "
            "own excise is billed until it is: %s",
            err,
        )
        _failed_at = now
        return
    _HELD.update(table)
    _fetched_at = now
    _failed_at = None
