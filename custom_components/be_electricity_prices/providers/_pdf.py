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

from io import BytesIO

import aiohttp
import pypdf

from .base import ExtractorError


async def fetch_pdf_text(session: aiohttp.ClientSession, url: str) -> str:
    """Download ``url`` and return the concatenated extracted text."""
    try:
        async with session.get(
            url,
            headers={"User-Agent": "Home Assistant be_electricity_prices/0.1"},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {url}")
            payload = await resp.read()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {url}: {err}") from err

    return extract_pdf_text(payload)


def extract_pdf_text(payload: bytes) -> str:
    try:
        reader = pypdf.PdfReader(BytesIO(payload))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as err:
        raise ExtractorError(f"PDF parse error: {err}") from err


def to_float(text: str) -> float:
    """Parse a Belgian / French decimal number ('15,93' or '0.102')."""
    cleaned = text.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    return float(cleaned)
