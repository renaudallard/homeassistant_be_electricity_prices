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

"""Which month a card is for, and until when it is good.

A card rarely says plainly. It carries a validity sentence in one of a dozen
wordings, or only a month name in a heading, or nothing at all and has to be
dated by what it does not mention. Getting this wrong bills a month at
another month's rates, so the readings are deliberately strict and a card
that cannot be dated is treated as undated rather than assumed current.
"""

from __future__ import annotations

from ._parse import fold_accents
from .base import SupplierSnapshot
from datetime import date
from homeassistant.util import dt as dt_util
import calendar
import re
from ._pdf import _MONTH_NAMES
from ._pdf import _MONTH_YEAR_RE
from ._pdf import _VALID_KEYWORDS


def end_of_month(year: int, month: int) -> date:
    """The last calendar day of ``year``-``month`` as a :class:`date`."""
    return date(year, month, calendar.monthrange(year, month)[1])


def scan_month_end(
    text: str, month_names: dict[str, int], *, limit: int
) -> date | None:
    """Find the first "<month name> <year>" token in ``text[:limit]`` and
    return that month's last day, or ``None`` if none is present.

    Word+year tokens whose word is not a known month (e.g. a
    "Versie 2026" edition marker that shares the shape) are skipped
    rather than aborting the scan, so a colliding token ahead of the real
    month line does not drop validity.
    """
    for match in _MONTH_YEAR_RE.finditer(text[:limit]):
        name = match.group(1).lower()
        if name in month_names:
            return end_of_month(int(match.group(2)), month_names[name])
    return None


def _validity_windows(lower: str, span: int = 200) -> list[str]:
    """Return up to ``span`` chars of context after each validity-keyword
    occurrence in ``lower`` (which is expected to be already accent-folded
    or lowercased). Used to anchor heuristic month-name searches so a
    retrospective mention elsewhere in the PDF doesn't masquerade as a
    validity statement.
    """
    windows: list[str] = []
    for keyword in _VALID_KEYWORDS:
        start = 0
        while True:
            idx = lower.find(keyword, start)
            if idx < 0:
                break
            windows.append(lower[idx : idx + span])
            start = idx + len(keyword)
    return windows


def text_mentions_month(
    text: str,
    year_month: date,
    month_names: tuple[str, ...],
) -> bool:
    """Heuristic check that ``text`` references the requested year+month
    inside an anchored window.

    Looks for the printed month name + year, the numeric MM/YYYY form,
    and the ISO YYYY-MM form. Accent-folds both haystack and needles
    so an extraction that lost diacritics still matches. The search
    is scoped to two anchors: the first 1000 characters (where Belgian
    tariff cards print ``Carte tarifaire <month> <year>`` /
    ``Tariefkaart <month> <year>``) plus 200-char windows after each
    validity keyword (``geldig``, ``valable``, ``validit``, ``valid``).
    Both anchors run on every call: either alone is enough to
    accept; together they catch the legitimate mention while excluding
    retrospective references buried in footers and comparison tables
    further down.
    """
    # Collapse whitespace runs so a month name and its year that PDF
    # extraction split across a newline or padded with extra spaces
    # ("mei\n2026", "mei  2026") still match the single-space needle. The
    # numeric / ISO needles carry no spaces, so they are unaffected.
    haystack = re.sub(r"\s+", " ", fold_accents(text))
    needles = tuple(
        fold_accents(n)
        for n in (
            f"{month_names[year_month.month - 1]} {year_month.year}",
            f"{year_month.month:02d}/{year_month.year}",
            f"{year_month.year}-{year_month.month:02d}",
        )
    )
    # Search both the PDF header (first 1000 chars: that's where most
    # tariff cards print "Carte tarifaire <month> <year>" / "Tariefkaart
    # <month> <year>") and the windows after each validity keyword.
    # Either anchor is enough; together they catch the legitimate
    # mentions while excluding retrospective references buried in
    # footers and comparison tables further down.
    windows = [haystack[:1000], *_validity_windows(haystack)]
    return any(n in w for n in needles for w in windows)


def archive_validity_check(
    snap: SupplierSnapshot,
    text: str,
    year_month: date,
    *,
    month_names: tuple[str, ...] | None = None,
) -> SupplierSnapshot | None:
    """Confirm an archived snapshot actually covers ``year_month``.

    Returns ``snap`` when the cross-check passes, ``None`` otherwise -
    so the caller (a provider's ``fetch_for_month``) can fall back to
    the proxy snapshot rather than mis-billing past consumption at a
    CDN-substituted current card's rates.

    Two tiers, matching the ``fetch_for_month`` pattern shared between
    eneco / cociter / ebem:

    1. ``snap.valid_until`` parsed: reject when it doesn't fall in the
       requested month. Authoritative when present.
    2. ``snap.valid_until`` missing: when ``month_names`` is provided
       (eneco / cociter), require a textual mention of the requested
       month via :func:`text_mentions_month`; reject when missing.
       When ``month_names`` is ``None`` (ebem) the textual fallback is
       skipped and the snapshot is accepted on the strength of the URL
       resolver alone.
    """
    if snap.valid_until is not None:
        if (
            snap.valid_until.year != year_month.year
            or snap.valid_until.month != year_month.month
        ):
            return None
    elif month_names is not None and not text_mentions_month(
        text, year_month, month_names
    ):
        return None
    return snap


def parse_valid_until(text: str) -> date | None:
    """Best-effort parse of a "valid until" date from a tariff card.

    Anchored on a validity keyword (``geldig``, ``valable``,
    ``validit``, ``valid``): the parser only considers dates that
    appear within a short window (~200 chars) **after** one of these
    keywords. This avoids picking up unrelated dates elsewhere in the
    document (contract end dates, regulatory dates, footer
    boilerplate).

    Inside each window we try, in order:

      1. Spelled-out ``<day> <month-name> <year>``
         ("30 april 2026", "30 avril 2026").
      2. Numeric ``DD/MM/YYYY``.
      3. Bare ``<month-name> <year>``, returning the last day of that
         month: e.g. "Tariefkaart april 2026" implies "valid until
         the last day of April".

    Returns the latest matching date across all windows, or ``None``
    when no pattern matches. ``None`` is the right signal for callers
    to fall back to "treat as available" rather than locking the entry.
    """
    lower = text.lower()
    name_alt = "|".join(re.escape(m) for m in _MONTH_NAMES)
    spelled_re = re.compile(rf"\b(\d{{1,2}})\s+({name_alt})\s+(20\d{{2}})\b")
    # Accept either DD/MM/YYYY or DD/MM/YY (Cociter prints 2-digit years
    # like "30/04/26"). 2-digit years are normalized to 20YY downstream.
    # Word-boundary on both ends so an embedded run like "02/123/4567"
    # in a phone number can't fragment into a fake "02/12/34" match.
    # The separator class also covers DD-MM-YYYY and DD.MM.YYYY for
    # publications that legal-style their dates with dashes or dots
    # (no Belgian supplier in the registry uses these today, but the
    # cost is one regex character class).
    numeric_re = re.compile(
        r"(?<!\d)(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2}(?:\d{2})?)(?!\d)"
    )
    bare_month_re = re.compile(rf"\b({name_alt})\s+(20\d{{2}})\b")

    # Scan every validity-keyword window (keyword + next ~200 chars).
    # Candidates are pooled across all windows and the latest is taken
    # below, which is what a "du X au Y" range needs. A stray later date
    # within a window would also win, but no published card has shown that.
    windows = _validity_windows(lower)
    if not windows:
        return None

    # Tariff cards never advertise validity past a few years out;
    # numeric_re will happily eat a 4-digit run that follows DD/MM
    # (e.g. "30/04/2625" from a corrupted phone-number footnote)
    # and produce date(2625, 4, 30). Clamp candidates to a symmetric
    # 5-year horizon around today so the year-2625 typo and the
    # year-1900 typo are both rejected, but legitimate archive cards
    # (Eneco / Cociter going several years back via fetch_for_month)
    # still parse a real validity_until rather than silently falling
    # through to the textual fallback. Anchor on Brussels local time so
    # a HA host running UTC doesn't compute a wrong year off the OS
    # clock late in the local evening (the +-5-year window absorbs the
    # narrow miss anyway, but matching the timezone is honest).
    today = dt_util.now().date()
    max_year = today.year + 5
    min_year = today.year - 5

    def _accept(d: date) -> bool:
        return min_year <= d.year <= max_year

    candidates: list[date] = []
    for window in windows:
        for match in spelled_re.finditer(window):
            day, month_name, year = match.group(1), match.group(2), match.group(3)
            try:
                cand = date(int(year), _MONTH_NAMES[month_name], int(day))
            except ValueError:
                continue
            if _accept(cand):
                candidates.append(cand)
        for match in numeric_re.finditer(window):
            day, month, year = match.group(1), match.group(2), match.group(3)
            try:
                year_i = int(year)
                if year_i < 100:
                    year_i += 2000
                cand = date(year_i, int(month), int(day))
            except ValueError:
                continue
            if _accept(cand):
                candidates.append(cand)

    if candidates:
        return max(candidates)

    # Fall back to bare "<month> <year>" inside any validity window.
    for window in windows:
        for match in bare_month_re.finditer(window):
            month_name, year = match.group(1), match.group(2)
            try:
                cand = end_of_month(int(year), _MONTH_NAMES[month_name])
            except (KeyError, ValueError):
                continue
            if _accept(cand):
                candidates.append(cand)
    return max(candidates) if candidates else None
