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

"""Synergrid solar production profile (SPP) fetcher.

Some Belgian variable/injection contracts index the solar feed-in tariff to the
SPP-weighted average of the day-ahead price: the price weighted by the national
Synthetic Production Profile that Synergrid publishes for PV settlement. This
module downloads that profile so the coordinator can compute the weighted
average against the ENTSO-E prices it already caches.

Synergrid publishes it as a public, no-login workbook at
``synergrid.be/images/downloads/SLP-RLP-SPP/<year>/SPP_ex-ante_and_ex-post_<year>.xlsx``.
The file is ~52 MB, almost entirely the ex-post sheet, which we never touch: we
stream the download to a temp file and parse only the ex-ante sheet (a few MB of
XML) with the stdlib, keeping peak memory around 20 MB. Only the ex-ante
(forecast) profile is available for the running year; the realized ex-post lags,
so an SPP-weighted average from this file is close but not the settled value.

``fetch_spp_weights`` returns hourly-aggregated weights keyed by the UTC
``(month, day, hour)`` so they line up with the coordinator's hourly spot cache.
Any failure (download, format drift, 404 for a not-yet-published year) returns an
empty mapping so the caller degrades to the plain arithmetic mean.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

# The four parses below run over a REMOTE workbook. The stdlib parser
# already refuses an EXTERNAL entity (it raises ParseError rather than
# fetching the URL), so the exposure that matters here is entity
# EXPANSION: a bare xml.etree parse happily expands a nested-entity
# payload, which is a memory DoS on a file we do not control. defusedxml
# refuses the DTD outright. It is a declared requirement already, and its
# iterparse streams incrementally with el.clear() exactly like the stdlib
# one, so peak memory on the 52 MB file is unchanged.
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

import aiohttp

from .providers._pdf import USER_AGENT

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://www.synergrid.be/images/downloads/SLP-RLP-SPP"
# The ex-ante sheet name and its value column header. Resolved by prefix / text
# rather than hardcoded position so a minor layout change doesn't silently break.
_SHEET_PREFIX = "SPP_ex-ante"
_VALUE_HEADER = "SPPExanteBE"
# The workbook's first column is the true UTC instant (an Excel date serial);
# its Year/Month/Day/Hour columns are LOCAL Belgian wall-clock, so we key on
# this column to line up with the coordinator's UTC-keyed spot cache.
_UTC_HEADER = "UTC"
# Excel serial day 0 (the 1899-12-30 epoch absorbs Excel's 1900 leap-year bug
# for post-1900 dates).
_EXCEL_EPOCH = datetime(1899, 12, 30)
# The file is ~52 MB today; cap the stream well above that to bound a runaway
# download without rejecting a legitimately larger future edition.
_MAX_BYTES = 200 * 1024 * 1024
_TIMEOUT = 120

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Hourly weights keyed by (month, day, hour) in UTC.
SppWeights = dict[tuple[int, int, int], float]


async def fetch_spp_weights(session: aiohttp.ClientSession, year: int) -> SppWeights:
    """Return the year's hourly-aggregated ex-ante SPP weights, or ``{}``.

    Never raises: a download or parse failure logs and returns an empty mapping
    so the coordinator falls back to the plain arithmetic monthly mean.
    """
    url = f"{_BASE_URL}/{year}/SPP_ex-ante_and_ex-post_{year}.xlsx"
    try:
        path = await _download(session, url)
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        _LOGGER.warning("Synergrid SPP download failed (%s): %s", url, err)
        return {}
    try:
        return await asyncio.to_thread(_parse_hourly_weights, path)
    except (
        zipfile.BadZipFile,
        ET.ParseError,
        # defusedxml rejects entity expansion / external references with its
        # own exceptions, which are NOT ParseError subclasses. They do inherit
        # ValueError below, but name them so this stays covered if that ever
        # changes -- the docstring promises this function never raises.
        DefusedXmlException,
        LookupError,  # KeyError (missing column) or IndexError (bad string index)
        ValueError,
        ArithmeticError,  # OverflowError from an out-of-range date serial
        OSError,
    ) as err:
        _LOGGER.warning("Synergrid SPP parse failed (%s): %s", url, err)
        return {}
    finally:
        path.unlink(missing_ok=True)


async def _download(session: aiohttp.ClientSession, url: str) -> Path:
    """Stream ``url`` to a temp file (never into memory) and return its path."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    written = 0
    try:
        async with session.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT),
        ) as resp:
            if resp.status >= 400:
                raise aiohttp.ClientResponseError(
                    resp.request_info,
                    resp.history,
                    status=resp.status,
                    message=f"HTTP {resp.status}",
                )
            async for chunk in resp.content.iter_chunked(1 << 16):
                written += len(chunk)
                if written > _MAX_BYTES:
                    raise ValueError(f"SPP file exceeds {_MAX_BYTES} bytes")
                tmp.write(chunk)
    except BaseException:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise
    tmp.close()
    return Path(tmp.name)


def _resolve_sheet_path(z: zipfile.ZipFile) -> str:
    """Map the ex-ante sheet name to its worksheet XML member."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rid = None
    for sheet in wb.iter(_NS + "sheet"):
        if (sheet.get("name") or "").startswith(_SHEET_PREFIX):
            rid = sheet.get(_REL_NS + "id")
            break
    if rid is None:
        raise KeyError(f"no sheet starting {_SHEET_PREFIX!r}")
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    for rel in rels.iter(_PKG_REL_NS + "Relationship"):
        if rel.get("Id") == rid:
            return "xl/" + (rel.get("Target") or "").lstrip("/")
    raise KeyError(f"no relationship target for {rid!r}")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    """The workbook's shared-string table (header cells reference it)."""
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    root = ET.fromstring(data)
    return [
        "".join(t.text or "" for t in si.iter(_NS + "t"))
        for si in root.iter(_NS + "si")
    ]


def _column_of(ref: str) -> str:
    """'AB12' -> 'AB' (the column letters of a cell reference)."""
    return ref.rstrip("0123456789")


def _cell_number(cell: tuple[str | None, str | None]) -> float | None:
    """Numeric value of a (type, text) cell, or ``None`` for a string/blank."""
    ctype, text = cell
    if text is None or ctype == "s":
        return None
    return float(text)


def _parse_hourly_weights(path: Path) -> SppWeights:
    """Parse the ex-ante sheet, summing the four quarters of each UTC hour.

    Streams the worksheet XML, clearing elements as it goes so peak memory stays
    a few tens of MB regardless of the 52 MB file. The UTC instant and value
    columns are resolved from the header row (by name, not fixed position); the
    hour is taken from the UTC column, not the local Year/Month/Day/Hour columns.
    """
    with zipfile.ZipFile(path) as z:
        target = _resolve_sheet_path(z)
        strings = _shared_strings(z)
        header: dict[str, str] = {}  # header name -> column letter
        weights: SppWeights = {}
        row_cells: dict[str, tuple[str | None, str | None]] = {}
        with z.open(target) as fh:
            for _event, el in ET.iterparse(fh, events=("end",)):
                if el.tag == _NS + "c":
                    ref = el.get("r", "")
                    val = el.find(_NS + "v")
                    row_cells[_column_of(ref)] = (
                        el.get("t"),
                        val.text if val is not None else None,
                    )
                    el.clear()
                elif el.tag == _NS + "row":
                    if int(el.get("r", "0")) == 1:
                        for col, (ctype, text) in row_cells.items():
                            name = (
                                strings[int(text)]
                                if ctype == "s" and text is not None
                                else text
                            )
                            if name:
                                header[name] = col
                    else:
                        _accumulate_row(row_cells, header, weights)
                    row_cells = {}
                    el.clear()
    if _UTC_HEADER not in header or _VALUE_HEADER not in header:
        raise KeyError(f"{_UTC_HEADER!r}/{_VALUE_HEADER!r} column not found")
    return weights


def _accumulate_row(
    cells: dict[str, tuple[str | None, str | None]],
    header: dict[str, str],
    weights: SppWeights,
) -> None:
    try:
        serial = _cell_number(cells[header[_UTC_HEADER]])
        value = _cell_number(cells[header[_VALUE_HEADER]])
        if serial is None or value is None:
            return
        # Excel serial -> UTC datetime; +30s absorbs float imprecision before
        # the hour is floored (the quarter's minute is irrelevant once
        # aggregated). An out-of-range serial raises OverflowError -- skip that
        # row rather than aborting the whole parse.
        utc = _EXCEL_EPOCH + timedelta(days=serial) + timedelta(seconds=30)
    except (KeyError, TypeError, ValueError, OverflowError):
        return
    key = (utc.month, utc.day, utc.hour)
    weights[key] = weights.get(key, 0.0) + value
