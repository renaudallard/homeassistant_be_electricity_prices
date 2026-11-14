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

"""Turning a tariff card's bytes into text.

Every stored text of the card archive was rendered by one of these readers,
and each is stamped with a digest of this module and of the decode bounds
(``scripts/card_texts.py``), so a change here renders every kept card again.
Downloading, validating and memoising a card stay in ``_pdf.py``, where a
change renders nothing.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from io import BytesIO
from typing import Any

import pypdf

from ._pdf_bound import bounded_pypdf
from ._pdf_bound_pdfminer import bounded_pdfminer
from .base import CardNotReadableError, ExtractorError

_LOGGER = logging.getLogger(__name__)


# A tariff card that carries a text layer is never anywhere near this small:
# across the shipped fixtures, each read with its supplier's reader, the LEAST
# texty is Wase Wind's one-page Vast-dynamisch card for 2026 at 1270
# characters through the layout reader, while Ecofix's rasterized August 2026
# cards yield 172 and 342. Anything in between separates the two cleanly, and
# 600 sits 1,75 times above the rasterized ones and 2,1 times below the
# smallest real one. Raising here only changes
# WHICH error the user is shown: a card with no text layer was already going
# to fail its parse.
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
        chunks: list[str] = []
        failures = 0
        with bounded_pypdf():
            pages = list(pypdf.PdfReader(BytesIO(payload)).pages)
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
    except Exception as err:  # rewrap pypdf surface as ExtractorError
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

        with bounded_pdfminer(), pdfplumber.open(BytesIO(payload)) as pdf:
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
    except Exception as err:  # rewrap pdfplumber surface as ExtractorError
        raise ExtractorError(f"PDF {kind} parse error: {err}") from err


def _each_page(pdf: Any) -> Iterator[Any]:
    """``pdf``'s pages, each let go of once read: pdfplumber holds every
    page it laid out until the document is closed."""
    for page in pdf.pages:
        yield page
        page.close()


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
            (page.dedupe_chars().extract_text() or "") for page in _each_page(pdf)
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
        for page in _each_page(pdf):
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
