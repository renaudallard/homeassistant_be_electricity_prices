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
into force (an archived copy numbers it and dates the number in a footnote),
so the date a wording took effect is read off the innermost bracket around
it.

The consolidated text is the wording in force. The household rate before
it, a schedule by annual volume with the energy contribution beside it, is in
Justel's archived copy of the law as it stood before the amendment, read once
and held with the days it was in force: the bracket around it and the note
the archive carries ("En vigueur jusqu'au"). Those are its words alone, not
the rates a transition fixed in the amending law's own articles, so the law
the bracket's footnote links to is read as well and the schedule starts the
day after the last transition it fixed for the household rate (42,5755
EUR/MWh on the first band from April to June 2023). Nor are they the
quarterly notices article 420 provides for, which Justel does not
consolidate; the archived cards of July 2023 to July 2026 all print the
schedule, so none was issued in its days. It only stands in for a card that
does not print its own month (``household_schedule``); a month's own card is
billed as printed. The protected customer has no card that prints it
reliably, so its two closed rates are typed in beside the law they came from.
Both are held for the process, the law read again daily, and kept in every
entry's store so a restart without the network still knows the last one.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from html import unescape
from itertools import pairwise
from typing import Any, Final
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import aiohttp
from homeassistant.util import dt as dt_util

from .const import EXCISE_LAW_ARCHIVE_URL, EXCISE_LAW_URL
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
# The household schedule the archived wording sets, under this key in the
# table an entry stores.
SCHEDULE: Final = "standard_schedule"

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
# An archived copy titles no bracket. It numbers them and dates each number
# in its footnotes: (32)<L 2023-03-19/02, art. 9, 068; En vigueur : 01-04-2023>,
# the law linked to its own page by its dossier number (cn_search=2023031902).
# A number no footnote dates is marked with no date, which is refused only
# when it is the bracket a rate is dated by.
_NUMBERED_RE = re.compile(r"\[<sup><font color=red>(\d+)</font></sup>")
_FOOTNOTE_RE = re.compile(
    r"\(<font color=red>(\d+)</font>\)&lt;(?:(?!\(<font color=red>).)*?"
    r"(?:cn_search=(\d+)(?:(?!\(<font color=red>).)*?)?"
    r"En vigueur\s*:\s*(\d{2}-\d{2}-\d{4})",
    re.S | re.I,
)
_UNDATED = "00-00-0000"
_OPEN = "\x01"
_CLOSE = "\x02"
_TAG_RE = re.compile(r"<[^>]+>")

_ELECTRICITY = "électricité du code NC 2716"
_NON_PROFESSIONAL_RE = re.compile(r"consommation non-professionnelle\s*:")
# "a)" in the wording in force, "a." in the one before it.
_PROTECTED_RE = re.compile(r"a[.)]\s*client protégé résidentiel")
_OTHERS_RE = re.compile(r"b[.)]\s*autres\s*:")
# A date as the law writes it: "1er janvier 2027", "30 juin 2023".
_DAY = r"(1er|\d{1,2}) (" + "|".join(FR_MONTHS) + r") (\d{4})"
_STEP_RE = re.compile(r"A partir du " + _DAY + r"\s*:")
_EXCISE_RE = re.compile(r"droit d['’]accise\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh")
_SPECIAL_RE = re.compile(
    r"droit d['’]accise spécial\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh"
)
_CONTRIBUTION_RE = re.compile(
    r"cotisation sur l['’]énergie\s*:\s*([\d.,]+)\s*euros?\s+par\s+MWh"
)
_TRANCHE_RE = re.compile(
    r"pour la tranche (?:de ([\d.]+) à ([\d.]+)|à partir de ([\d.]+)) MWh\s*:"
)
# The archive's note, which says until when each article it lists was in
# force: "Art. 419 *** 420 *** ... En vigueur jusqu'au 01-08-2026".
_NOTE_RE = re.compile(r'id="note".*?</div>', re.S)
_UNTIL_RE = re.compile(
    r"Art\.\s*(.*?)\s*En vigueur jusqu['’]au\s*(\d{2})-(\d{2})-(\d{4})"
)
# The number a law's own page gives it: "Dossier numéro : 2023031902".
_DOSSIER_RE = re.compile(r"Dossier ?(?:numéro|nummer)\s*:\s*(\d+)")
# A rate the amending law fixed for a while in its own articles rather than
# in the wording: "Le taux du droit d'accise spécial, prévu à l'article 419,
# k), 2), b), i), ... s'élève, pour la période du 1er avril 2023 jusqu'au 30
# juin 2023 inclus, à 42,5755 euros/MWh". The citation is the household
# rate's ("b) autres" under electricity's "2. consommation
# non-professionnelle"), any of its bands.
_TRANSITION_RE = re.compile(
    r"article 419, k\), 2\), b\),[^.]*?pour la période du "
    + _DAY
    + r" jusqu['’]au "
    + _DAY
    + r" inclus"
)


@dataclass(frozen=True)
class Schedule:
    """A household wording the law no longer prints, billed from ``start``
    to the day before ``until``: the excise by annual volume as ((upper kWh,
    EUR/kWh excluding VAT), ...), the last band open above its bound, and the
    energy contribution beside it, EUR/kWh excluding VAT. ``start`` is the
    day after the last rate the law that wrote it fixed in its own articles
    for a while, rather than that law's entry into force."""

    start: date
    until: date
    bands: tuple[tuple[float, float], ...]
    contribution: float


# (kind) -> ((first day, EUR/kWh excluding VAT), ...), ascending.
_HELD: dict[str, tuple[tuple[date, float], ...]] = {}
# SCHEDULE -> the household wording before the one in force.
_SCHEDULES: dict[str, Schedule] = {}
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


def _day(day: str, month: str, year: str) -> date:
    """A date as the law writes it, in the three parts ``_DAY`` reads."""
    return date(int(year), FR_MONTHS.index(month) + 1, 1 if day == "1er" else int(day))


def _amendment(text: str, at: int) -> tuple[date, str]:
    """The entry into force of the innermost amendment around ``at``, and
    the dossier number of the law that made it where the page links that
    law, else ``""``."""
    stack: list[str] = []
    pos = 0
    while pos < at:
        nxt = min(
            (i for i in (text.find(_OPEN, pos), text.find(_CLOSE, pos)) if i >= 0),
            default=-1,
        )
        if nxt < 0 or nxt >= at:
            break
        if text[nxt] == _OPEN:
            stack.append(text[nxt + 1 : text.index(" ", nxt)])
        elif stack:
            stack.pop()
        pos = nxt + 1
    if not stack:
        raise ExciseLawError("no entry into force around the rate")
    day, month, year = stack[-1][:10].split("-")
    return date(int(year), int(month), int(day)), stack[-1][10:]


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
        (
            _amendment(text, start + special.start())[0],
            _rate(chunk[bounds[0] : bounds[1]]),
        )
    ]
    for i, head in enumerate(heads):
        when = _day(*head.groups())
        out.append((when, _rate(chunk[head.end() : bounds[i + 2]])))
    if not _ascending(out):
        raise ExciseLawError("steps out of order")
    return tuple(out)


def parse(page: str) -> dict[str, tuple[tuple[date, float], ...]]:
    """The two household rates article 419 sets, with their steps.

    Raises :class:`ExciseLawError` on anything that is not the article as
    expected, rather than reading half of it. That includes a date the
    calendar does not have or a number that does not convert, which the
    readers below raise as ValueError: let through, it escaped the backoff in
    ``ensure_excise_law`` and failed every tick.
    """
    try:
        return _parse(page)
    except (ValueError, OverflowError) as err:
        raise ExciseLawError(f"unreadable article: {err}") from err


def _household(page: str) -> tuple[str, int, int, int, int]:
    """Article 419 as read, and where its household rates sit: the end of
    the protected customer's label, the start and the end of the others'
    label, and the end of the paragraph."""
    match = _ARTICLE_RE.search(page)
    if match is None:
        raise ExciseLawError("article 419 not found")
    # An opening is marked with its entry into force, then the amending
    # law's dossier number where a footnote links it, and a space.
    dated = {
        number: f"{when}{dossier}"
        for number, dossier, when in _FOOTNOTE_RE.findall(match.group(1))
    }
    marked = _CLOSE_RE.sub(_CLOSE, match.group(1))
    marked = _OPEN_RE.sub(lambda m: f"{_OPEN}{m[1]}-{m[2]}-{m[3]} ", marked)
    marked = _NUMBERED_RE.sub(lambda m: f"{_OPEN}{dated.get(m[1], _UNDATED)} ", marked)
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
    return text, protected.end(), others.start(), others.end(), end


def _parse(page: str) -> dict[str, tuple[tuple[date, float], ...]]:
    text, protected, others_at, others, end = _household(page)
    return {
        PROTECTED: _steps(text, protected, others_at),
        STANDARD: _steps(text, others, end),
    }


def _until(page: str) -> date:
    """The day article 419's archived wording stopped being in force."""
    note = _NOTE_RE.search(page)
    if note is None:
        raise ExciseLawError("no note on the archived article")
    text = " ".join(unescape(_TAG_RE.sub(" ", note.group())).split())
    for match in _UNTIL_RE.finditer(text):
        if "419" in (article.strip() for article in match[1].split("***")):
            return date(int(match[4]), int(match[3]), int(match[2]))
    raise ExciseLawError("no end to article 419")


def parse_archive(page: str) -> tuple[Schedule, str]:
    """The household schedule an archived copy of article 419 sets, from the
    entry into force of the law that wrote it to the day it ended, and that
    law's dossier number, which :func:`after_transitions` reads it by.

    Raises :class:`ExciseLawError` on anything else, as :func:`parse` does.
    """
    try:
        return _parse_archive(page)
    except (ValueError, OverflowError) as err:
        raise ExciseLawError(f"unreadable article: {err}") from err


def _parse_archive(page: str) -> tuple[Schedule, str]:
    until = _until(page)
    text, _protected, _others_at, others, end = _household(page)
    chunk = text[others:end]
    heads = list(_TRANCHE_RE.finditer(chunk))
    if len(heads) < 2:
        raise ExciseLawError("the rate is not banded by volume")
    bands: list[tuple[float, float]] = []
    contributions: set[float] = set()
    floor = 0.0
    for i, head in enumerate(heads):
        last = i == len(heads) - 1
        lower = _number(head[1] if head[3] is None else head[3]) * 1000.0
        upper = None if head[2] is None else _number(head[2]) * 1000.0
        if (
            lower != floor
            or (upper is None) != last
            or (upper is not None and upper <= lower)
        ):
            raise ExciseLawError("volume bands out of order")
        body = chunk[head.end() : len(chunk) if last else heads[i + 1].start()]
        found = _CONTRIBUTION_RE.findall(body)
        if len(found) != 1:
            raise ExciseLawError("no single energy contribution")
        contributions.add(_number(found[0]) / 1000.0)
        # The open band is one of no width: blended_excise_rate bills the
        # volume past the last bound at the last band's rate.
        bands.append((lower if upper is None else upper, _rate(body)))
        floor = lower if upper is None else upper
    if len(contributions) != 1:
        raise ExciseLawError("the energy contribution is banded by volume")
    contribution = contributions.pop()
    if not 0.0 <= contribution < _MAX_EUR_PER_KWH:
        raise ExciseLawError(f"implausible contribution {contribution} EUR/kWh")
    special = _SPECIAL_RE.search(chunk, heads[0].end())
    if special is None:
        raise ExciseLawError("no special excise")
    start, dossier = _amendment(text, others + special.start())
    if not dossier:
        raise ExciseLawError("no link to the law that wrote the schedule")
    if start >= until:
        raise ExciseLawError("in force for no day")
    return Schedule(start, until, tuple(bands), contribution), dossier


def law_url(dossier: str) -> str:
    """Justel's page of the law numbered ``dossier``, asked for as the
    archived copy is, as its footnotes link it."""
    parts = urlsplit(EXCISE_LAW_ARCHIVE_URL)
    query = {**parse_qs(parts.query), "cn_search": [dossier]}
    query.pop("arch", None)
    return urlunsplit(parts._replace(query=urlencode(query, doseq=True)))


def after_transitions(schedule: Schedule, dossier: str, page: str) -> Schedule:
    """``schedule`` from the day after the last household rate its law,
    ``page``, fixed for a while in its own articles rather than in the
    wording, since those days were billed that rate and not the schedule.

    Raises :class:`ExciseLawError` on a page that is not the law numbered
    ``dossier``, as :func:`parse` does, and on one where it reads no such
    rate: a page that dropped or reworded it would otherwise bill the
    schedule from that law's entry into force, over the days its rate was.
    """
    text = " ".join(unescape(_TAG_RE.sub(" ", page)).split())
    if dossier not in _DOSSIER_RE.findall(text):
        raise ExciseLawError(f"not the law numbered {dossier}")
    transitions = list(_TRANSITION_RE.finditer(text))
    if not transitions:
        raise ExciseLawError("no household transition")
    start = schedule.start
    try:
        for match in transitions:
            start = max(start, _day(*match.groups()[3:]) + timedelta(days=1))
    except (ValueError, OverflowError) as err:
        raise ExciseLawError(f"unreadable transition: {err}") from err
    if start >= schedule.until:
        raise ExciseLawError("in force for no day")
    return replace(schedule, start=start)


def _rate_for(kind: str, month: date) -> float | None:
    first = month.replace(day=1)
    rate = None
    for when, value in _HELD.get(kind, ()):
        if when > first:
            break
        rate = value
    return rate


def household_schedule(month: date) -> Schedule | None:
    """The household schedule the archived wording set, if it was in force
    on ``month``'s first day and has been read."""
    first = month.replace(day=1)
    schedule = _SCHEDULES.get(SCHEDULE)
    if schedule is None or not schedule.start <= first < schedule.until:
        return None
    return schedule


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


def _schedule(stored: Any) -> Schedule | None:
    try:
        schedule = Schedule(
            date.fromisoformat(stored["start"]),
            date.fromisoformat(stored["until"]),
            tuple((float(upper), float(rate)) for upper, rate in stored["bands"]),
            float(stored["contribution"]),
        )
    except (TypeError, ValueError, KeyError):
        return None
    uppers = [upper for upper, _rate in schedule.bands]
    if (
        schedule.start >= schedule.until
        or len(uppers) < 2
        or uppers != sorted(uppers)
        or not all(0.0 <= rate < _MAX_EUR_PER_KWH for _upper, rate in schedule.bands)
        or not 0.0 <= schedule.contribution < _MAX_EUR_PER_KWH
    ):
        return None
    return schedule


def hold(table: Mapping[str, Any]) -> None:
    """Hold ``table`` (the shape :func:`held_table` writes), replacing what
    was held for each kind it carries."""
    for kind in (STANDARD, PROTECTED):
        steps = _section(table.get(kind))
        if steps:
            _HELD[kind] = steps
    schedule = _schedule(table.get(SCHEDULE))
    if schedule is not None:
        _SCHEDULES[SCHEDULE] = schedule


def restore(table: Mapping[str, Any]) -> None:
    """Hold ``table`` from an entry's store, for a kind nothing holds yet.

    Several entries restore their own copy at start-up and one of them may
    already have fetched the law, so a stored copy never replaces it.
    """
    for kind in (STANDARD, PROTECTED):
        steps = _section(table.get(kind))
        if steps and kind not in _HELD:
            _HELD[kind] = steps
    schedule = _schedule(table.get(SCHEDULE))
    if schedule is not None and SCHEDULE not in _SCHEDULES:
        _SCHEDULES[SCHEDULE] = schedule


def held_table() -> dict[str, Any]:
    """What is held, in the shape :func:`hold` reads, for an entry's store."""
    table: dict[str, Any] = {
        kind: [[when.isoformat(), rate] for when, rate in steps]
        for kind, steps in _HELD.items()
    }
    for kind, schedule in _SCHEDULES.items():
        table[kind] = {
            "start": schedule.start.isoformat(),
            "until": schedule.until.isoformat(),
            "bands": [list(band) for band in schedule.bands],
            "contribution": schedule.contribution,
        }
    return table


async def ensure_excise_law(session: aiohttp.ClientSession) -> None:
    """Read the law once a day, and the wording before it once.

    A failure keeps what is held and is retried after ``_FAILURE_RETRY_S``;
    nothing here raises, since the caller is the coordinator tick. The
    archived wording and the law that wrote it never change, so they are
    asked for only while the schedule is not held, with each daily read of
    the law until both have been read.
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
    if SCHEDULE in _SCHEDULES:
        return
    try:
        schedule, dossier = parse_archive(
            await fetch_text(session, EXCISE_LAW_ARCHIVE_URL, timeout=15)
        )
        schedule = after_transitions(
            schedule, dossier, await fetch_text(session, law_url(dossier), timeout=15)
        )
    except ExtractorError as err:
        # Only a month a card stands in for needs it, which then bills the
        # levies that card prints, as it did before this was read.
        _LOGGER.warning(
            "The household excise in force before the current law could not "
            "be read from Justel, so a card standing in for one of those "
            "months is billed the levies it prints: %s",
            err,
        )
        return
    _SCHEDULES[SCHEDULE] = schedule
