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
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TypeVar
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import Any

import aiohttp
import pypdf
from homeassistant.util import dt as dt_util

from .base import (
    CardNotReadableError,
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


USER_AGENT = f"Home Assistant be_electricity_prices/{_read_version()}"


def is_transient_fetch_error(message: str) -> bool:
    """Whether an ExtractorError message describes a transient fetch
    failure a later refresh is likely to recover, rather than a permanent
    one (parse error, 404, non-PDF payload) that needs a code fix.

    The fetch helpers in this module wrap aiohttp surface errors with three
    stable prefixes: ``network error fetching`` (timeout / reset / DNS),
    ``storage error fetching`` (the object store behind the url refused the
    read) and ``HTTP <status>``. A bare ``network error`` is always
    transient. Among HTTP statuses, 5xx plus 408 / 429 / 403 are transient
    (the Cloudflare-fronted suppliers intermittently answer an otherwise
    healthy resource with a 403 anti-bot challenge or a 429 that succeeds
    on retry); 404 / 410 mean the card was renamed or withdrawn and must
    fail fast.
    """
    if message.startswith("network error fetching"):
        return True
    if message.startswith("storage error fetching"):
        return True
    if message.startswith("HTTP "):
        head = message[len("HTTP ") :].split(None, 1)[0]
        if head.isdigit():
            status = int(head)
            return status >= 500 or status in (403, 408, 429)
    return False


def error_text(err: BaseException) -> str:
    """The exception's message, or its class name when it carries none.

    aiohttp raises its timeouts argless, and ``str()`` of an argless
    exception is ``""``. Interpolated into a message that ends in ``": "``
    that produced a user-facing sentence trailing off after the colon, on
    all three surfaces that show ``last_error``: the ``snapshot_stale``
    Repairs card, the ``current_price`` sensor attribute, and diagnostics.
    Naming the class is the smallest thing that stays informative -- the
    caller's own prefix already says what was being attempted.

    Also used by the ENTSO-E client, whose ``EntsoeError`` reaches the same
    three surfaces through ``ENTSO-E: {err}``.
    """
    return str(err) or type(err).__name__


# 64 MiB: ~12x the largest real tariff card (Bolt's ~5 MiB PDFs), so it
# never trips on a legitimate card while bounding what a broken or
# hostile CDN can pull into the coordinator's memory in one fetch.
_MAX_PDF_BYTES = 64 * 1024 * 1024


async def _read_pdf_bytes(resp: aiohttp.ClientResponse, url: str) -> bytes:
    """Read a (PDF) response body, rejecting an endpoint that declares a
    Content-Length far larger than any real tariff card.

    Reading the whole body keeps the magic-byte / parse path simple; the
    guard only refuses payloads the server itself advertises as oversize
    (a streamed response with no Content-Length still reads normally,
    which is fine for the trusted supplier endpoints we fetch).
    """
    declared = resp.content_length
    if declared is not None and declared > _MAX_PDF_BYTES:
        raise ExtractorError(
            f"refusing PDF at {url}: declared {declared} bytes (limit {_MAX_PDF_BYTES})"
        )
    return await resp.read()


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


# An object store that refuses the read answers the proxy in front of it
# with its own XML error document, and the proxy passes that through as a
# 200. Azure Blob and S3 both shape it as a root <Error> carrying a <Code>.
_STORAGE_ERROR_RE = re.compile(
    rb"^(?:\xef\xbb\xbf)?\s*(?:<\?xml[^>]*\?>\s*)?<Error>\s*<Code>([^<]{1,64})</Code>",
    re.IGNORECASE,
)


def _storage_error_code(payload: bytes) -> str | None:
    """The error code if ``payload`` is an object-store error document.

    Luminus switched anonymous access off on the storage account behind
    ``api-next/get-pricelist`` on 2026-08-10, and every card came back as
    a 248-byte ``PublicAccessNotPermitted`` document under a 200. Read as
    a plain non-PDF payload that reports as a permanent parse failure, so
    every Luminus entry raised the ``extractor_failed`` card asking the
    user to report a layout change that had not happened. The supplier's
    own download button was broken the same way, and nothing here could
    fix it, which is the definition of transient in this taxonomy.
    """
    match = _STORAGE_ERROR_RE.match(payload)
    if match is None:
        return None
    return match.group(1).decode("ascii", "replace").strip() or None


async def _fetch_validated_pdf_bytes(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> bytes:
    """Download ``url`` and return its bytes once validated as a PDF.

    Shared by the three ``fetch_pdf_text*`` variants. Catches TimeoutError
    alongside ClientError: aiohttp's ClientTimeout fires
    asyncio.TimeoutError (== builtins.TimeoutError on 3.11+), which is NOT
    a ClientError subclass, so a slow supplier endpoint would otherwise
    bubble a bare TimeoutError out of discover/fetch and crash the
    live-check. The ``network error fetching`` prefix is load-bearing -
    :func:`is_transient_fetch_error` keys on it to decide whether to retry -
    so only this function and :func:`fetch_text` may write it.
    """
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await _read_pdf_bytes(resp, url)
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(
            f"network error fetching {url}: {error_text(err)}"
        ) from err

    if not _is_pdf_payload(payload):
        # An object store refusing the read is the one non-PDF payload no
        # code change can fix, so it gets its own prefix and lands on the
        # transient side of is_transient_fetch_error rather than asking
        # the user to report a layout change.
        code = _storage_error_code(payload)
        if code is not None:
            raise ExtractorError(f"storage error fetching {url}: {code}")
        # Some CDNs return 200 + text/html for missing PDFs (a 404
        # disguised as success). Engie's API returns octet-stream
        # for valid PDFs, so checking the magic bytes is more
        # reliable than the Content-Type header.
        raise ExtractorError(
            f"expected a PDF at {url}, payload starts with {payload[:80]!r}"
        )
    # Strip the BOM the validator above deliberately tolerates. Accepting it
    # there only keeps the download from being rejected; the bytes still have
    # to parse, and pdfplumber cannot read them -- it fails a BOM-prefixed
    # file with "No /Root object! - Is this really a PDF?", which reads like a
    # corrupt card rather than three stray bytes. pypdf recovers on its own,
    # so the two aligned/layout variants are the ones this protects.
    if payload.startswith(b"\xef\xbb\xbf"):
        payload = payload[3:]
    return payload


async def fetch_pdf_text(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    payload = await _fetch_validated_pdf_bytes(session, url, timeout=timeout)
    # pypdf does pure-Python parsing; offload to a worker thread so a
    # multi-page tariff card never stalls Home Assistant's event loop.
    return await asyncio.to_thread(extract_pdf_text, payload)


# A tariff card that carries a text layer is never anywhere near this small:
# measured across all 82 shipped fixtures the LEAST texty is 6134 characters,
# while Ecofix's rasterized August 2026 cards yield 172 and 342. Anything in
# between separates the two cleanly, and 600 leaves an order of magnitude of
# headroom on both sides. Raising here only changes WHICH error the user is
# shown: a card with no text layer was already going to fail its parse.
_MIN_TEXT_LAYER_CHARS = 600


def _require_text_layer(text: str, pages: int) -> None:
    """Raise :class:`CardNotReadableError` for an image-only PDF."""
    if pages and len(text.strip()) < _MIN_TEXT_LAYER_CHARS:
        raise CardNotReadableError(
            f"card has no text layer: {len(text.strip())} characters across "
            f"{pages} page(s), so it is published as page images"
        )


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
        text = "\n".join(chunks)
        _require_text_layer(text, len(pages))
        return text
    except ExtractorError:
        raise
    except Exception as err:  # noqa: BLE001 - rewrap pypdf surface as ExtractorError
        raise ExtractorError(f"PDF parse error: {err}") from err


def _pdfplumber_text(payload: bytes, kind: str, render: Callable[[Any], str]) -> str:
    """Open ``payload`` with pdfplumber, reconstruct text via ``render``,
    and rewrap failures uniformly.

    ``render`` receives the open pdfplumber document and returns the
    reconstructed text; ``kind`` ("layout" / "aligned") only shapes the
    :class:`ExtractorError` message. A PDF with pages but no decodable
    text fails loud rather than returning "" and letting every downstream
    regex miss silently (only mandatory fields fail loud; nullable ones
    zero), matching the pypdf path's all-pages guard.
    """
    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(payload)) as pdf:
            text = render(pdf)
            # Ecofix reaches this path, not the pypdf one, so the text-layer
            # check has to live on both or the classification would depend on
            # which extractor a supplier happens to use. This subsumes the
            # older "pages present but no text decoded" check: zero decoded
            # characters is the same condition, just its extreme.
            _require_text_layer(text, len(pdf.pages))
            return text
    except ExtractorError:
        raise
    except Exception as err:  # noqa: BLE001 - rewrap pdfplumber surface as ExtractorError
        raise ExtractorError(f"PDF {kind} parse error: {err}") from err


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
    return _pdfplumber_text(
        payload,
        "layout",
        lambda pdf: "\n".join(
            (page.dedupe_chars().extract_text() or "") for page in pdf.pages
        ),
    )


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
    from collections import defaultdict

    def render(pdf: Any) -> str:
        out: list[str] = []
        for page in pdf.pages:
            rows: defaultdict[int, list[tuple[float, float, str]]] = defaultdict(list)
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

    return _pdfplumber_text(payload, "aligned", render)


async def fetch_pdf_text_aligned(
    session: aiohttp.ClientSession,
    url: str,
    x_join_threshold: float = 0.0,
    *,
    timeout: int = 30,
) -> str:
    """Word-coordinate aligned variant of :func:`fetch_pdf_text`."""
    payload = await _fetch_validated_pdf_bytes(session, url, timeout=timeout)
    return await asyncio.to_thread(
        extract_pdf_text_aligned, payload, 3, x_join_threshold
    )


async def fetch_pdf_text_layout(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 30
) -> str:
    """Layout-preserving variant of :func:`fetch_pdf_text`.

    Some CDNs return HTTP 200 with ``text/html`` for missing PDFs (404
    pages disguised as success). We treat those as fetch failures so the
    parser never tries to read a PDF that isn't.
    """
    payload = await _fetch_validated_pdf_bytes(session, url, timeout=timeout)
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
    except (aiohttp.ClientError, TimeoutError):
        # aiohttp's ClientTimeout fires asyncio.TimeoutError (==
        # builtins.TimeoutError on 3.11+), which is NOT a ClientError;
        # without this a slow HEAD would break the documented
        # None-on-failure contract and bubble out of the probe path.
        return None


async def head_ok(
    session: aiohttp.ClientSession, url: str, *, timeout: int = 10
) -> bool:
    """Whether ``url`` answers a HEAD probe with a non-error status.

    A small existence check for ``discover()`` paths that only need to
    know the card still resolves. Returns ``False`` on any HTTP >= 400 or
    on a network failure, including the bare ``TimeoutError`` that
    aiohttp's total ``ClientTimeout`` raises (it is not an
    ``aiohttp.ClientError``, so catching only the latter would let a slow
    endpoint bubble a crash out of the probe).
    """
    try:
        async with session.head(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
            allow_redirects=True,
        ) as resp:
            return resp.status < 400
    except (aiohttp.ClientError, TimeoutError):
        return False


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


# A caller that will GET the same listing many times in quick succession can
# ask fetch_text to serve repeats from memory. Off by default: every existing
# caller wants a live read, and a global time-based cache would quietly hand a
# coordinator tick a stale page. This is opt-in, explicit, and scoped to the
# block that entered it.
_TEXT_MEMO: ContextVar[dict[str, str] | None] = ContextVar("_TEXT_MEMO", default=None)


@contextmanager
def memoise_text_fetches(store: dict[str, str]) -> Iterator[None]:
    """Serve repeat text GETs of one URL from ``store`` inside this block.

    Nine providers resolve a per-supplier listing page inside ``fetch()`` and
    then pick one product out of it, so pricing a whole supplier re-downloads
    the same page once per contract: a Flanders static sweep pulls Mega's
    listing nine times, Engie's eight and Luminus's eight, about 3 MB and 25
    round trips that buy nothing. Under a wall-clock budget that is rows the
    user does not get.

    ``store`` is passed in rather than created here so a caller can share one
    memo across several tasks - an ``asyncio.Task`` copies the context at
    creation, which copies the reference and not the dict, so every candidate
    in a sweep sees what the first one fetched.

    Deliberately not a TTL cache inside fetch_text: the coordinator and the
    one-off quote both want a live read, and the failure mode of guessing a
    TTL for them is a stale card nobody asked for.
    """
    token = _TEXT_MEMO.set(store)
    try:
        yield
    finally:
        _TEXT_MEMO.reset(token)


async def fetch_text(
    session: aiohttp.ClientSession,
    url: str,
    *,
    params: dict[str, str] | None = None,
    timeout: int = 20,
) -> str:
    """GET ``url`` and return the response body as text.

    Raises :class:`ExtractorError` on any HTTP non-2xx, network error,
    or aiohttp client failure. Use for HTML listing / index pages and
    other plain-text sources; reach for :func:`fetch_pdf_text` (or its
    layout / aligned variants) when the body is expected to be a PDF.

    ``params`` is passed straight to :meth:`aiohttp.ClientSession.get`
    for endpoints that carry their query in the URL string (e.g. a
    Sanity CMS ``query``).

    Callers that prefer a soft None-on-failure can wrap this in a
    ``try / except ExtractorError`` block; concentrating the network /
    HTTP error handling here keeps the ~6 lines of boilerplate out of
    every provider, and routes every fetch through the one
    :func:`is_transient_fetch_error` taxonomy so transient failures
    retry uniformly.
    """
    memo = _TEXT_MEMO.get()
    # Keyed on the full request, not the bare URL: one endpoint answers
    # different questions by query string (Frank's Sanity GROQ, for one).
    memo_key = url if not params else f"{url}?{sorted(params.items())}"
    if memo is not None and memo_key in memo:
        return memo[memo_key]
    try:
        async with session.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            body = await resp.text()
            # Only a success is memoised. A failure is re-attempted by the
            # next caller, which is what the negative cache one layer up is
            # for; caching it here would give it a second, untracked lifetime.
            if memo is not None:
                memo[memo_key] = body
            return body
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ExtractorError(
            f"network error fetching {url}: {error_text(err)}"
        ) from err


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


def parse_prosumer_column(
    section: str, labels: dict[str, str], *, skip_columns: int = 4
) -> dict[str, float]:
    """DSO key -> prosumer EUR/kVA/year, off an analog-meter DSO table.

    The Flemish analog-meter table prints the prosumer forfait as the column
    after ``skip_columns`` numeric ones. Ecofix and EBEM read it with the same
    eight lines; only the section they read it from differs, and that stays at
    the call site along with any per-supplier gate on whether the table exists
    at all.

    A label the table does not carry is simply absent from the result, the same
    as before: the caller falls back to leaving the rate unset.
    """
    out: dict[str, float] = {}
    skip = r"[\d.,]+\s+" * skip_columns
    for label, key in labels.items():
        row = re.search(rf"{re.escape(label)}\s+" + skip + r"([\d.,]+)", section)
        if row:
            out[key] = to_float(row.group(1))
    return out


def require_contract(by_id: dict[str, _T], contract_id: str, label: str) -> _T:
    """The contract definition for ``contract_id``, or raise.

    Fourteen call sites across seven extractors spelled out the same lookup and
    the same "unknown <supplier> contract" message. The guard inside
    ``parse_snapshot`` is NOT redundant with the one in ``fetch``: that function
    is the public entry point the tests and the live check call directly.

    ``fetch_for_month`` keeps its own ``return None`` instead: a month a
    supplier never published has to resolve to None so the caller falls back to
    the current-card proxy, not blow up the year-to-date walk.
    """
    try:
        return by_id[contract_id]
    except KeyError:
        raise ExtractorError(f"unknown {label} contract {contract_id!r}") from None


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


def tier_bound_kwh(text: str) -> float:
    """Parse a consumption-tier bound like ``20.000`` or ``1.000.000``.

    Distinct from :func:`to_float`: the dot here is a thousands separator,
    not a decimal point, so ``to_float`` would read 20.000 kWh as twenty
    and band every site into the wrong tranche. Tier bounds are always
    whole kWh, so dropping the separators is exact.
    """
    cleaned = text.strip()
    for sep in _NUMERIC_SEPARATORS:
        cleaned = cleaned.replace(sep, "")
    return float(cleaned.replace(".", ""))


# Single source of truth for the sign character that appears between
# BELPEX/Epex factor and base across every supplier formula (both
# consumption and injection sides). Hyphen-minus, ASCII plus,
# figure-dash, en-dash, em-dash, and U+2212 mathematical minus are
# all encountered in the wild; supplier PDFs flip silently between
# them on re-renders.
_NEGATIVE_SIGNS = ("-", "‐", "‑", "‒", "–", "—", "−")
# One capture group around a plain decimal, with NO thousands separator.
# Named for that constraint on purpose: a card whose values run into four
# digits needs eneco's wider pattern instead, and reusing this one there
# truncates the value to its first digits (recorded at eneco.py:140).
NUM_NO_THOUSANDS = r"([\d]+(?:[.,][\d]+)?)"

SIGN_CHARS = r"+\-‐‑‒–—−"
"""Drop into a regex character class: ``[`` + SIGN_CHARS + ``]``."""


def parse_sign(char: str) -> float:
    """Return -1.0 for any hyphen / dash / Unicode-minus, +1.0 otherwise.

    Use as ``base = parse_sign(m.group(N)) * to_float(m.group(N+1))`` so
    a future card that swaps to U+2212 (or '+' for an indexation that
    flips polarity) doesn't silently break the parser.
    """
    return -1.0 if char in _NEGATIVE_SIGNS else 1.0


# Residential connection-power upper bounds (kVA) mapped to the OSP tier keys
# shared with const.CONNECTION_KVA_TIER_*. Kept as literals so this low-level
# helper stays decoupled from the config module; the keys must match.
_OSP_BOUND_TO_TIER: dict[float, str] = {
    1.44: "le1_44",
    6.0: "le6",
    9.6: "le9_6",
    13.0: "le13",
    18.0: "le18",
    36.0: "le36",
    56.0: "le56",
}
# The table's last row has no upper bound, so it is keyed by the bound it opens
# at rather than by one it closes. Suppliers write that bound as the top of the
# band below ("> 56 kVA") or as the first value above it ("> 56,01 kVA"), so
# the check is "at least", not equality.
_OSP_OPEN_TIER = "gt56"
_OSP_OPEN_MIN_BOUND = 56.0


def parse_brussels_osp(text: str) -> dict[str, float] | None:
    """Parse the Brussels Brugel OSP annual-fee table off a Sibelga card.

    Every Brussels card that prints the "Obligations de Service Public"
    block lists one flat EUR/year fee per connection-power tier. The three
    supplier extractors render it three different ways (label above vs.
    beside the value; ``et``/``Entre``/``<=`` phrasings), but each tier row
    always ends ``<bound> kVA <value>``, so anchor on the value-bearing
    ``kVA`` token. Returns every tier the card prints, keyed by the shared tier
    ids, or None when the block is absent (a card that omits it, or a
    non-Brussels card).

    The rows have to be told apart by their operator, not by the number alone.
    "> 36 et <= 56 kVA" and "> 56 kVA" both end in ``56 kVA``, so keying on the
    bound would make the open-ended top row overwrite the one below it and
    charge a 40 kVA connection the 56-and-above fee.
    """
    # Case-insensitive: Bolt prints "Obligations de service publique" (lower
    # 's'), the others "Obligations de Service Public".
    block = re.search(r"Obligations de Service.*?(?=\n\s*\(\d\)|\Z)", text, re.S | re.I)
    if block is None:
        return None
    out: dict[str, float] = {}
    for match in re.finditer(
        r"(?P<open>>\s*)?(?P<bound>[\d.,]+)\s*kVA[\s\n]+(?P<value>[\d.,]+)",
        block.group(0),
    ):
        bound = round(to_float(match.group("bound")), 2)
        if match.group("open"):
            # A ">" immediately before the value-bearing bound is the
            # open-ended top row, in both layouts. A closed row reaches this
            # bound through "<=" (Engie, TotalEnergies) or "et" (Mega), so its
            # own ">" sits in front of the LOWER bound and never here.
            tier = _OSP_OPEN_TIER if bound >= _OSP_OPEN_MIN_BOUND else None
        else:
            tier = _OSP_BOUND_TO_TIER.get(bound)
        if tier is not None:
            out[tier] = to_float(match.group("value"))
    return out or None


# Full month names in calendar order (index 0 == January). The single
# source of truth for the per-supplier archive-validity checks, which
# match a card's spelled-out month against ``month_names[month - 1]``.
# Suppliers import the tuple for their card's language rather than
# re-listing the twelve names (and drifting on accents); dict-shaped
# lookups derive from these with ``enumerate(.., 1)``.
NL_MONTHS: tuple[str, ...] = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)
FR_MONTHS: tuple[str, ...] = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


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


def end_of_month(year: int, month: int) -> date:
    """The last calendar day of ``year``-``month`` as a :class:`date`."""
    return date(year, month, calendar.monthrange(year, month)[1])


_MONTH_YEAR_RE = re.compile(r"\b([A-Za-z]+)\s+(20\d{2})\b")


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


def flanders_tax_overlay(
    text: str,
    *,
    supplier: str,
    excise: Sequence[re.Pattern[str]],
    renewables: Sequence[re.Pattern[str]],
    contribution: re.Pattern[str] | None = None,
    fund: re.Pattern[str] | None = None,
) -> TaxOverlay:
    """The tax block of a Flanders-only, VAT-inclusive card.

    Every Flemish card carries the same four rows and the same policy about
    which of them may be missing. Only the anchors differ, so the callers pass
    compiled patterns and this holds the policy:

    * ``excise`` -- MANDATORY. Patterns are tried in order and the first match
      wins, so a card printing both the flat August-2026 row and the tiered
      one being phased out resolves to the flat rate.
    * ``renewables`` -- MANDATORY, and ALL of them must match. Summed. Some
      cards print GSC and WKK separately, others one pre-summed row.
    * ``contribution`` -- OPTIONAL, absent means 0.0. The federal levy dropped
      to zero on 2026-08-01 and suppliers answered by deleting the row, so an
      absent row is the abolished levy, not a layout drift.
    * ``fund`` -- OPTIONAL, absent means 0.0, and it is EUR/month so it is NOT
      scaled by 100 like the c€/kWh rows.

    Sharing the policy is the point. It was written out three times and had
    already drifted: two suppliers defaulted an absent contribution row to
    zero while the third still raised on it, so that one would have gone
    offline the moment its card dropped the row like the others' did.
    """
    excise_match = next((m for p in excise if (m := p.search(text))), None)
    if excise_match is None:
        raise ExtractorError(f"{supplier}: could not parse the tax block")
    renewables_matches = [p.search(text) for p in renewables]
    if not renewables or any(m is None for m in renewables_matches):
        # Flanders-only cards always bill the green-certificate levies; a miss
        # is a layout drift that would silently under-bill, so fail loud and
        # let the coordinator keep serving its cached snapshot.
        raise ExtractorError(f"{supplier}: could not parse the GSC/WKK levies")
    contribution_match = contribution.search(text) if contribution else None
    fund_match = fund.search(text) if fund else None
    return TaxOverlay(
        federal_excise=to_float(excise_match.group(1)) / 100.0,
        energy_contribution=(
            to_float(contribution_match.group(1)) / 100.0 if contribution_match else 0.0
        ),
        flanders_renewables=sum(
            to_float(m.group(1)) / 100.0 for m in renewables_matches if m is not None
        ),
        energy_fund_eur_per_month=(
            to_float(fund_match.group(1)) if fund_match else 0.0
        ),
        # These cards print every value VAT-inclusive (the federal excise and
        # the energy fund are VAT-exempt), so the snapshot needs no gross-up.
        vat_rate=0.0,
    )
