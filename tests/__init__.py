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

"""Shared test helpers."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from custom_components.be_electricity_prices.providers._pdf import (
    extract_pdf_text,
    extract_pdf_text_layout,
)

FIXTURES = Path(__file__).parent / "fixtures"


@lru_cache(maxsize=None)
def fixture_text(name: str, *, layout: bool = False) -> str:
    """Read ``tests/fixtures/<name>`` and run it through the PDF extractor.

    ``layout=True`` routes through ``extract_pdf_text_layout`` for
    suppliers whose tariff cards rely on column positions (Bolt,
    DATS 24, Ecopower, TotalEnergies). Default is ``extract_pdf_text``
    (pypdf), which is fine for the rest.

    Cached for the lifetime of the pytest session: PDF extraction
    is the dominant cost in the test suite (~10s per fixture), and
    every call with the same arguments returns the same string. The
    cache cuts the full suite from ~190s to ~30s. Tests must not
    mutate the returned string (they don't today).
    """
    payload = (FIXTURES / name).read_bytes()
    if layout:
        return extract_pdf_text_layout(payload)
    return extract_pdf_text(payload)


__all__ = ["FIXTURES", "fixture_text"]
