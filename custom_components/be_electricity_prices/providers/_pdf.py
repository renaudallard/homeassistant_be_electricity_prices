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

"""Shared helpers for fetching and reading PDF tariff cards."""

from __future__ import annotations

import asyncio
import calendar
import json
import logging
import re
import unicodedata
from datetime import date
from io import BytesIO
from pathlib import Path

import aiohttp
import pypdf
from homeassistant.util import dt as dt_util

from .base import ExtractorError, SupplierSnapshot

_LOGGER = logging.getLogger(__name__)


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


USER_AGENT = f"Home Assistant be_electricity_prices/{_read_version()}"


def _is_pdf_payload(payload: bytes) -> bool:
    """Return True if the bytes look like a PDF.

    PDFs start with the magic bytes ``%PDF``. Some publishers prepend
    a UTF-8 BOM (\\ufeff = 3 bytes EF BB BF) — OCTA+'s tariff PDFs do
    this. Allow the BOM as a one-time prefix.
    """
    if payload.startswith(b"%PDF"):
        return True
    if payload.startswith(b"\xef\xbb\xbf%PDF"):
        return True
    return False


async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    # Catch TimeoutError alongside ClientError in every fetch helper:
    # aiohttp's ClientTimeout fires asyncio.TimeoutError (==
    # builtins.TimeoutError on 3.11+), which is NOT a subclass of
    # ClientError, so a slow supplier endpoint would otherwise bubble a
    # bare TimeoutError out of discover/fetch and crash the live-check.
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err

    if not _is_pdf_payload(payload):
        # Some CDNs return 200 + text/html for missing PDFs (a 404
        # disguised as success). Engie's API returns octet-stream
        # for valid PDFs, so checking the magic bytes is more
        # reliable than the Content-Type header.
        snippet = payload[:80]
        raise ExtractorError(
            f"expected a PDF at {url}, payload starts with {snippet!r}"
        )
    # pypdf does pure-Python parsing; offload to a worker thread so a
    # multi-page tariff card never stalls Home Assistant's event loop.
    return await asyncio.to_thread(extract_pdf_text, payload)


def extract_pdf_text(payload: bytes) -> str:
    try:
        reader = pypdf.PdfReader(BytesIO(payload))
        pages = list(reader.pages)
        chunks: list[str] = []
        failures = 0
        for idx, page in enumerate(pages):
            text = page.extract_text()
            if text is None:
                # pypdf returns None when a page cannot be decoded (e.g.
                # an unsupported font). The caller would otherwise see a
                # corrupt snapshot with regex misses on whatever was on
                # that page; log so the failure is visible in HA logs.
                _LOGGER.warning(
                    "pypdf returned None for page %d/%d", idx + 1, len(pages)
                )
                failures += 1
                continue
            chunks.append(text)
        if pages and failures == len(pages):
            raise ExtractorError("PDF parse error: every page failed to decode")
        return "\n".join(chunks)
    except ExtractorError:
        raise
    except Exception as err:  # noqa: BLE001 - rewrap pypdf surface as ExtractorError
        raise ExtractorError(f"PDF parse error: {err}") from err


def extract_pdf_text_layout(payload: bytes) -> str:
    """Extract PDF text via pdfplumber, preserving table layout.

    Used by suppliers (e.g. TotalEnergies) whose tariff cards include
    rotated DSO / tax columns that pypdf drops silently. pdfplumber
    walks the underlying pdfminer character stream and reassembles rows
    using glyph coordinates, so each DSO row comes out as one line with
    every numeric column in the right order.

    Pages are passed through ``dedupe_chars()`` first: TotalEnergies
    occasionally publishes cards with duplicated glyphs stacked at the
    same coordinates (e.g. ORES Namur ECO band rendered as ``55,,09``
    instead of ``5,09`` in the April-2026 myDrive Wallonia card). The
    dedupe drops those overlapped copies before text reconstruction.
    """
    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(payload)) as pdf:
            return "\n".join(
                (page.dedupe_chars().extract_text() or "") for page in pdf.pages
            )
    except Exception as err:  # noqa: BLE001 - rewrap pdfplumber surface as ExtractorError
        raise ExtractorError(f"PDF layout parse error: {err}") from err


def extract_pdf_text_aligned(
    payload: bytes,
    y_tolerance: int = 3,
    x_join_threshold: float = 0.0,
) -> str:
    """Extract PDF text by re-grouping words by their visual row.

    OCTA+'s tariff cards interleave column data such that the standard
    text and table extractors return one number per line in column-major
    order. ``extract_words()`` returns each word with x/y coordinates;
    bucketing by y reassembles each visual row into a single line, in
    left-to-right order. Pages are joined with form-feeds so callers
    can split per page if they need to.

    ``x_join_threshold`` is opt-in: leave at 0.0 to keep every word
    separate (the safe default for tightly-columned tables). Pass a
    positive value (~1.0pt) to merge adjacent words whose horizontal
    gap to the previous word is below it. OCTA+'s tax block needs this
    because each glyph is its own pdfplumber word with sub-point gaps
    between them ("5 ,0 3 2 9" should be "5,0329"); a non-OCTA+ caller
    with tight numeric columns would silently glue values together if
    this defaulted to non-zero.
    """
    try:
        import pdfplumber

        from collections import defaultdict

        out: list[str] = []
        with pdfplumber.open(BytesIO(payload)) as pdf:
            for page in pdf.pages:
                rows: defaultdict[int, list[tuple[float, float, str]]] = defaultdict(
                    list
                )
                for word in page.extract_words():
                    bucket = round(float(word["top"]) / y_tolerance) * y_tolerance
                    rows[bucket].append(
                        (float(word["x0"]), float(word["x1"]), word["text"])
                    )
                lines: list[str] = []
                for y in sorted(rows.keys()):
                    cells = sorted(rows[y])
                    parts: list[str] = []
                    prev_x1: float | None = None
                    for x0, x1, text in cells:
                        if prev_x1 is not None and x0 - prev_x1 < x_join_threshold:
                            parts[-1] += text
                        else:
                            parts.append(text)
                        prev_x1 = x1
                    lines.append(" ".join(parts))
                out.append("\n".join(lines))
        return "\f".join(out)
    except Exception as err:  # noqa: BLE001 - rewrap pdfplumber surface as ExtractorError
        raise ExtractorError(f"PDF aligned parse error: {err}") from err


async def fetch_pdf_text_aligned(
    session: aiohttp.ClientSession,
    url: str,
    x_join_threshold: float = 0.0,
) -> str:
    """Word-coordinate aligned variant of :func:`fetch_pdf_text`."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err
    if not _is_pdf_payload(payload):
        raise ExtractorError(
            f"expected a PDF at {url}, payload starts with {payload[:80]!r}"
        )
    return await asyncio.to_thread(
        extract_pdf_text_aligned, payload, 3, x_join_threshold
    )


async def fetch_pdf_text_layout(session: aiohttp.ClientSession, url: str) -> str:
    """Layout-preserving variant of :func:`fetch_pdf_text`.

    Some CDNs return HTTP 200 with ``text/html`` for missing PDFs (404
    pages disguised as success). We treat those as fetch failures so the
    parser never tries to read a PDF that isn't.
    """
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await resp.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err
    if not _is_pdf_payload(payload):
        raise ExtractorError(
            f"expected a PDF at {url}, payload starts with {payload[:80]!r}"
        )
    return await asyncio.to_thread(extract_pdf_text_layout, payload)


async def head_freshness_key(
    session: aiohttp.ClientSession,
    url: str,
    *,
    prefer: tuple[str, ...] = ("Last-Modified", "ETag"),
) -> str | None:
    """HEAD ``url`` and return the first present header from ``prefer``.

    Used as a cheap freshness probe by suppliers whose tariff cards live
    behind a CDN that honours ``If-Modified-Since`` / ``If-None-Match``.
    Returns ``None`` on any 4xx/5xx, network error, or when none of the
    preferred headers are populated; the coordinator treats ``None`` as
    "no signal" and falls back to its time-based TTL.

    Bolt prefers ETag first because its listing returns a stable ETag
    while ``Last-Modified`` flips on every CDN edge cache; every other
    supplier prefers Last-Modified.
    """
    try:
        async with session.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return None
            for key in prefer:
                value = resp.headers.get(key)
                if value:
                    return value
            return None
    except aiohttp.ClientError:
        return None


def vat_multiplier(
    text: str,
    *patterns: str | re.Pattern[str],
    default: float = 1.06,
) -> float:
    """Read the VAT percentage from a card header and return 1 + N/100.

    Each supplier's tariff card prints the rate in a different phrasing
    ("Tarifs 6% TVAC", "TVA 6%", "6% de TVA comprise", ...); every
    extractor passes its own ``patterns``. The helper picks the first
    match across the list and converts the captured group via
    :func:`to_float` so cards that print a fractional rate (e.g.
    ``"21,5%"``) work without per-provider parsing.

    Falls back to ``default`` (1.06, the current Belgian residential rate)
    when none of the patterns match - the value of the multiplier itself
    is not load-bearing because most cards either ship VAT-incl numbers
    (no rescaling needed) or print the rate explicitly.
    """
    for pattern in patterns:
        match = (
            re.search(pattern, text)
            if isinstance(pattern, str)
            else pattern.search(text)
        )
        if match:
            return 1.0 + to_float(match.group(1)) / 100.0
    return default


async def fetch_text(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 20
) -> str:
    """GET ``url`` and return the response body as text.

    Raises :class:`ExtractorError` on any HTTP non-2xx, network error,
    or aiohttp client failure. Use for HTML listing / index pages and
    other plain-text sources; reach for :func:`fetch_pdf_text` (or its
    layout / aligned variants) when the body is expected to be a PDF.

    Callers that prefer a soft None-on-failure can wrap this in a
    ``try / except ExtractorError`` block; concentrating the network /
    HTTP error handling here keeps the ~6 lines of boilerplate out of
    every provider.
    """
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            return await resp.text()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err


_NUMERIC_SEPARATORS = (
    " ",  # ASCII space
    " ",  # NBSP (U+00A0)
    " ",  # THIN SPACE (U+2009)
    " ",  # NARROW NO-BREAK SPACE (U+202F, CLDR French thousands)
    " ",  # LINE SEPARATOR (U+2028)
)


def fold_accents(text: str) -> str:
    """Lowercase and strip Latin diacritics.

    Belgian / French / Dutch tariff PDFs sometimes lose their accents
    when extracted (font / CMap quirks in pypdf), so a literal substring
    test for ``"août"`` misses an extracted ``"aout"``. Provider-side
    cross-checks should fold both haystack and needle through this
    helper to compare apples-to-apples.
    """
    return "".join(
        c
        for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def to_float(text: str) -> float:
    """Parse a Belgian / French decimal number ('15,93' or '0.102').

    Strips every Unicode space variant Belgian PDFs use as a
    thousands separator or unit padder before swapping the comma
    for a decimal point. Without this, NNBSP-separated values like
    '5 029' raise ValueError mid-page.
    """
    cleaned = text.strip()
    for sep in _NUMERIC_SEPARATORS:
        cleaned = cleaned.replace(sep, "")
    return float(cleaned.replace(",", "."))


# Single source of truth for the sign character that appears between
# BELPEX/Epex factor and base across every supplier formula (both
# consumption and injection sides). Hyphen-minus, ASCII plus,
# figure-dash, en-dash, em-dash, and U+2212 mathematical minus are
# all encountered in the wild; supplier PDFs flip silently between
# them on re-renders.
_NEGATIVE_SIGNS = ("-", "‒", "–", "—", "−")
SIGN_CHARS = r"+\-‒–—−"
"""Drop into a regex character class: ``[`` + SIGN_CHARS + ``]``."""


def parse_sign(char: str) -> float:
    """Return -1.0 for any hyphen / dash / Unicode-minus, +1.0 otherwise.

    Use as ``base = parse_sign(m.group(N)) * to_float(m.group(N+1))`` so
    a future card that swaps to U+2212 (or '+' for an indexation that
    flips polarity) doesn't silently break the parser.
    """
    return -1.0 if char in _NEGATIVE_SIGNS else 1.0


# Month names recognised in publication strings, mapped to their 1-12
# index. Each language's full name + a few common abbreviations Belgian
# tariff cards use. The lookup key is lowercase, accent-stripped not
# guaranteed (we accept both forms explicitly).
_MONTH_NAMES: dict[str, int] = {
    # Dutch
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
    # French (with and without accents)
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
    # English (some cards mix languages on cross-region documents).
    # april / september / november / december share their spelling
    # with Dutch and are already registered above; repeating them
    # here would trip ruff's F601 (duplicate dict key literal), so
    # the English block lists only the names that actually differ.
    "january": 1,
    "february": 2,
    "march": 3,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "october": 10,
}


_VALID_KEYWORDS = ("geldig", "valable", "validit", "valid ")


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
    Both anchors run on every call -- either alone is enough to
    accept; together they catch the legitimate mention while excluding
    retrospective references buried in footers and comparison tables
    further down.
    """
    haystack = fold_accents(text)
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
    ``validit``, ``valid``) -- the parser only considers dates that
    appear within a short window (~200 chars) **after** one of these
    keywords. This avoids picking up unrelated dates elsewhere in the
    document (contract end dates, regulatory dates, footer
    boilerplate).

    Inside each window we try, in order:

      1. Spelled-out ``<day> <month-name> <year>``
         ("30 april 2026", "30 avril 2026").
      2. Numeric ``DD/MM/YYYY``.
      3. Bare ``<month-name> <year>``, returning the last day of that
         month -- e.g. "Tariefkaart april 2026" implies "valid until
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

    # Build the set of windows to scan: every occurrence of a validity
    # keyword + the next ~200 chars. Stops at the next validity
    # keyword to avoid bleed between adjacent statements.
    windows: list[str] = []
    for keyword in _VALID_KEYWORDS:
        start = 0
        while True:
            idx = lower.find(keyword, start)
            if idx < 0:
                break
            windows.append(lower[idx : idx + 200])
            start = idx + len(keyword)

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
                month = _MONTH_NAMES[month_name]
                last_day = calendar.monthrange(int(year), month)[1]
                cand = date(int(year), month, last_day)
            except (KeyError, ValueError):
                continue
            if _accept(cand):
                candidates.append(cand)
    return max(candidates) if candidates else None
