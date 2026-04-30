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
import json
from io import BytesIO
from pathlib import Path

import aiohttp
import pypdf

from .base import ExtractorError


def _read_version() -> str:
    manifest = Path(__file__).resolve().parent.parent / "manifest.json"
    try:
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version", "0"))
    except (OSError, ValueError):
        return "0"


USER_AGENT = f"Home Assistant be_electricity_prices/{_read_version()}"


async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await resp.read()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err

    # pypdf does pure-Python parsing; offload to a worker thread so a
    # multi-page tariff card never stalls Home Assistant's event loop.
    return await asyncio.to_thread(extract_pdf_text, payload)


def extract_pdf_text(payload: bytes) -> str:
    try:
        reader = pypdf.PdfReader(BytesIO(payload))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        raise ExtractorError(f"PDF parse error: {err}") from err


def extract_pdf_text_layout(payload: bytes) -> str:
    """Extract PDF text via pdfplumber, preserving table layout.

    Used by suppliers (e.g. TotalEnergies) whose tariff cards include
    rotated DSO / tax columns that pypdf drops silently. pdfplumber
    walks the underlying pdfminer character stream and reassembles rows
    using glyph coordinates, so each DSO row comes out as one line with
    every numeric column in the right order.
    """
    try:
        import pdfplumber

        with pdfplumber.open(BytesIO(payload)) as pdf:
            return "\n".join((page.extract_text() or "") for page in pdf.pages)
    except Exception as err:
        raise ExtractorError(f"PDF layout parse error: {err}") from err


def extract_pdf_text_aligned(
    payload: bytes,
    y_tolerance: int = 3,
    x_join_threshold: float = 1.0,
) -> str:
    """Extract PDF text by re-grouping words by their visual row.

    OCTA+'s tariff cards interleave column data such that the standard
    text and table extractors return one number per line in column-major
    order. ``extract_words()`` returns each word with x/y coordinates;
    bucketing by y reassembles each visual row into a single line, in
    left-to-right order. Pages are joined with form-feeds so callers
    can split per page if they need to.

    ``x_join_threshold`` controls how to render adjacent words on the
    same row. OCTA+'s tax block uses heavy character spacing where each
    glyph is its own pdfplumber word with sub-point gaps between them
    ("5 ,0 3 2 9" should be "5,0329"). Words whose horizontal gap to the
    previous word is below ``x_join_threshold`` are concatenated with no
    separator; everything else is joined by a single space. The default
    of 1.0pt is below typical inter-word spacing (~1.4pt at 8pt font)
    and well above intra-cluster gaps (<0.5pt).
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
    except Exception as err:
        raise ExtractorError(f"PDF aligned parse error: {err}") from err


async def fetch_pdf_text_aligned(session: aiohttp.ClientSession, url: str) -> str:
    """Word-coordinate aligned variant of :func:`fetch_pdf_text`."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type.lower():
                raise ExtractorError(f"expected a PDF at {url}, got {content_type!r}")
            payload = await resp.read()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err
    return await asyncio.to_thread(extract_pdf_text_aligned, payload)


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
            content_type = resp.headers.get("content-type", "")
            if "pdf" not in content_type.lower():
                raise ExtractorError(f"expected a PDF at {url}, got {content_type!r}")
            payload = await resp.read()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err
    return await asyncio.to_thread(extract_pdf_text_layout, payload)


_NUMERIC_SEPARATORS = (
    " ",  # ASCII space
    " ",  # NBSP (U+00A0)
    " ",  # THIN SPACE (U+2009)
    " ",  # NARROW NO-BREAK SPACE (U+202F, CLDR French thousands)
    " ",  # LINE SEPARATOR (U+2028)
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
