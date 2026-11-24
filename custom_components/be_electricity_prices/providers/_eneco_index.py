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

"""Eneco's indexation parameters, the quarter's Belpex-RLP among them.

The Flex cards up to March 2023 bill each delivery quarter on its Belpex-RLP,
and Eneco still bills a contract signed on one of them so, a 2025 bill on the
October 2022 card pricing every month at "0,1 x BELPEX-RLP + 5,966" on its
quarter's value. No later card prints that value. Eneco lists
it in "Indexatieparameters elektriciteit", one row per month: the quarter's
Belpex-RLP, repeated over its three months and left out until the quarter
is over, then Belpex-RLP-M and Belpex - Injectie. The older months are in a
second document, which adds Belpex, Endex103 and Endex213 columns and leaves
Belpex-RLP-M out before March 2023.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import aiohttp

from ._parse import to_float
from ._pdf import NL_MONTHS, fetch_pdf_text
from .base import ExtractorError

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://cdn.eneco.be/downloads/nl/b2c/acq"
# The current document, then the one with the older months, which it wins
# over on a month both list.
_DOCUMENTS = (
    f"{_BASE_URL}/indexatieparameters-elek.pdf",
    f"{_BASE_URL}/indexatieparameters-elek-oud.pdf",
)
_COLUMN_RE = re.compile(r"Belpex - Injectie|Belpex-RLP-M|Belpex-RLP|Belpex|Endex\d+")
_HEADER_RE = re.compile(r"^[^\n]*Belpex-RLP-M[^\n]*$", re.M)
_ROW_RE = re.compile(
    rf"^({'|'.join(NL_MONTHS)})[^\S\n]+(\d{{4}})"
    r"((?:[^\S\n]+(?>\d+(?:,\d+)?))+)[^\S\n]*$",
    re.M | re.I,
)


def parse_index_document(text: str) -> dict[date, tuple[float, float]]:
    """Each month's Belpex-RLP and Belpex-RLP-M, in EUR/kWh, off one document.

    Only a row with a figure in every column is read: the figures carry no
    position, so in a shorter row nothing says which one is missing, and the
    running quarter's months are such rows until it is over. A quarter's
    other months give its value where one of them lacks a figure. Raises
    ExtractorError when the document names neither column.
    """
    header = _HEADER_RE.search(text)
    columns = _COLUMN_RE.findall(header.group(0)) if header else []
    if columns.count("Belpex-RLP") != 1 or columns.count("Belpex-RLP-M") != 1:
        raise ExtractorError("Eneco indexation document has no Belpex-RLP column")
    quarter, month = columns.index("Belpex-RLP"), columns.index("Belpex-RLP-M")
    out: dict[date, tuple[float, float]] = {}
    for row in _ROW_RE.finditer(text):
        figures = row.group(3).split()
        if len(figures) != len(columns):
            continue
        number = NL_MONTHS.index(row.group(1).lower()) + 1
        out[date(int(row.group(2)), number, 1)] = (
            to_float(figures[quarter]) / 1000.0,
            to_float(figures[month]) / 1000.0,
        )
    return out


async def published_quarters(
    session: aiohttp.ClientSession,
) -> dict[date, tuple[float, float]]:
    """Every month Eneco lists, with its quarter's Belpex-RLP and its own
    Belpex-RLP-M in EUR/kWh. The current document has to be read; the older
    one only adds months before it, and is left out when it cannot be."""
    out = parse_index_document(await fetch_pdf_text(session, _DOCUMENTS[0]))
    try:
        older = parse_index_document(await fetch_pdf_text(session, _DOCUMENTS[1]))
    except ExtractorError as err:
        _LOGGER.debug("Eneco's older indexation document was not read: %s", err)
        return out
    return older | out
