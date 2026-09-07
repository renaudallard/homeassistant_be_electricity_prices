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
import struct
import tempfile
import zipfile
from datetime import datetime, timedelta
from collections.abc import Iterable
from pathlib import Path
from typing import IO, Any

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
# (LOCAL month, day, hour) -> the hour's share of the year's residential load.
# Local, not UTC: Synergrid keys the RLP workbook's Year / Month / Day / h
# columns in Belgian local time with DST, and Eneco's published Belpex-RLP-M is
# reproduced to the cent only on that alignment.
RlpWeights = dict[tuple[int, int, int], float]


async def fetch_rlp_weights(
    session: aiohttp.ClientSession, year: int, blend: str = "distinct"
) -> RlpWeights:
    """Return the year's hourly RLP weights in local time, or ``{}``.

    The residential load profile (RLP0N) is the other Synergrid profile Belgian
    cards index on: a card weights each hour's Belpex quotation by it. Synergrid
    publishes it per DSO as a binary workbook,
    ``RLP0N <year> Electricity all DSOs.xlsb``, about 3,4 MB, which
    :func:`_parse_rlp_weights` reduces to one hourly curve for the requested
    ``blend`` (see ``RlpBlend``). Never raises: a download or parse failure logs
    and returns an empty mapping, and the caller keeps the plain arithmetic mean.
    """
    url = f"{_BASE_URL}/{year}/RLP0N%20{year}%20Electricity%20all%20DSOs.xlsb"
    try:
        path = await _download(session, url, suffix=".xlsb")
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        _LOGGER.warning("Synergrid RLP download failed (%s): %s", url, err)
        return {}
    try:
        return await asyncio.to_thread(_parse_rlp_weights, path, blend)
    except (
        ImportError,  # pyxlsb missing: the manifest requirement was not installed
        zipfile.BadZipFile,  # an xlsb is a zip container; a non-workbook fails here
        struct.error,  # pyxlsb unpacks the binary records with struct
        LookupError,
        ValueError,
        TypeError,
        AttributeError,
        ArithmeticError,
        OSError,
    ) as err:
        _LOGGER.warning("Synergrid RLP parse failed (%s): %s", url, err)
        return {}
    finally:
        await asyncio.to_thread(path.unlink, True)


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
        # Syscall: off the loop like every other filesystem call here.
        await asyncio.to_thread(path.unlink, True)


# Bytes buffered in memory before a write is handed to the executor. The file
# is ~52 MB, so a 4 MB buffer is ~13 executor round-trips instead of ~800 at
# the old 64 KB read size, while holding a bounded slice rather than the lot.
_WRITE_BUFFER_BYTES = 4 * 1024 * 1024


async def _download(
    session: aiohttp.ClientSession, url: str, *, suffix: str = ".xlsx"
) -> Path:
    """Stream ``url`` to a temp file (never into memory) and return its path.

    Every filesystem call goes through the executor. This runs on the event
    loop, the file is ~52 MB, and on the SD-card installs Home Assistant is
    commonly deployed to a write can block for a long time once the kernel
    starts throttling dirty pages -- long enough for HA to log a blocking-call
    warning and for every other integration's callbacks to stall behind it.
    Chunks are accumulated into a bounded buffer so the offload happens a few
    times rather than once per network read.
    """

    def _open_temp() -> IO[bytes]:
        # Named factory with a concrete return type: asyncio.to_thread cannot
        # resolve NamedTemporaryFile's overloads, so it picked the text one.
        return tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below
            mode="w+b", delete=False, suffix=suffix
        )

    tmp = await asyncio.to_thread(_open_temp)
    written = 0
    buf = bytearray()

    def _flush(data: bytes) -> None:
        # Concrete signature: NamedTemporaryFile.write is overloaded, which
        # asyncio.to_thread cannot resolve under --strict.
        tmp.write(data)

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
                buf += chunk
                if len(buf) >= _WRITE_BUFFER_BYTES:
                    await asyncio.to_thread(_flush, bytes(buf))
                    buf.clear()
            if buf:
                await asyncio.to_thread(_flush, bytes(buf))
    except BaseException:
        # close() flushes, so it is a write too; unlink is a syscall. Both go
        # through the executor like everything else on this path.
        await asyncio.to_thread(tmp.close)
        await asyncio.to_thread(Path(tmp.name).unlink, True)
        raise
    await asyncio.to_thread(tmp.close)
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


_RLP_SHEET = "RLP96UbyDGO"
# Row layout of that sheet: 0 = DSO names, 1 = "DGO" labels, 2 = EAN codes,
# then one quarter-hour per row as CET | Year | Month | Day | h | Min | Date |
# one unit curve per DSO column. Only the local-time columns and the curves
# are read; the CET serial is fixed UTC+1 and is not used.
_RLP_FIRST_CURVE_COLUMN = 7
_RLP_MONTH_COLUMN, _RLP_DAY_COLUMN, _RLP_HOUR_COLUMN = 2, 3, 4
_RLP_YEAR_COLUMN = 1
_RLP_CURVE_ROUNDING = 12


def _parse_rlp_weights(path: Path, blend: str = "distinct") -> RlpWeights:
    """Read the all-DSO RLP0N workbook into hourly local-time weights.

    ``pyxlsb`` is imported here rather than at module level so an install
    without the manifest requirement fails the fetch, not the integration.
    """
    from pyxlsb import open_workbook

    with open_workbook(str(path)) as workbook, workbook.get_sheet(_RLP_SHEET) as sheet:
        return _rlp_weights_from_rows(
            ([c.v for c in row] for row in sheet.rows()), blend
        )


def _rlp_weights_from_rows(
    rows: Iterable[list[Any]], blend: str = "distinct"
) -> RlpWeights:
    """One DSO blend of the RLP profile, summed to local clock hours.

    Synergrid's workbook lists one column per DSO sub-area, but only three
    curves are distinct (Fluvius, the Walloon DSOs with the small ones,
    Sibelga): the same Fluvius curve appears eight times. Three suppliers read
    the same sheet three ways, and each reproduces its own published values to
    the cent, so the blend is what the caller asks for:

      - "distinct": the equal mean of the three distinct curves. Eneco's
        Belpex-RLP-M; averaging the columns as printed instead weights Flanders
        eight to one and misses Eneco by up to 2,2 EUR/MWh in summer.
      - "columns": the mean over every column, i.e. each distinct curve
        weighted by how many sub-areas share it. energie.be's Belpex_RLP,
        which its card defines as the mean "van de verschillende
        distributienetbeheerders" read literally.
      - "flanders": the Fluvius curve alone, identified by name. Energy Knights
        sells in Flanders only and bills on the customer's DSO.

    Curves are deduplicated by value, not by name, so a sub-area that gains its
    own curve counts once. Raises ``ValueError`` when the sheet has no curve,
    the flanders blend finds no Fluvius column, or the weights do not sum to
    about one over the year, which is what a wrong sheet or a truncated
    download looks like.
    """
    header: list[Any] | None = None
    keys: list[tuple[int, int, int]] = []
    curves: list[list[float]] = []
    for row in rows:
        if header is None:
            header = list(row)
            continue
        year = row[_RLP_YEAR_COLUMN] if len(row) > _RLP_YEAR_COLUMN else None
        if not isinstance(year, (int, float)):
            continue
        values = row[_RLP_FIRST_CURVE_COLUMN:]
        if not curves:
            curves = [[] for _ in values]
        if len(values) != len(curves):
            raise ValueError("RLP sheet row has a different column count")
        keys.append(
            (
                int(row[_RLP_MONTH_COLUMN]),
                int(row[_RLP_DAY_COLUMN]),
                int(row[_RLP_HOUR_COLUMN]),
            )
        )
        for curve, value in zip(curves, values, strict=True):
            curve.append(
                float(value) if isinstance(value, (int, float)) else float("nan")
            )
    col_names = list(header[_RLP_FIRST_CURVE_COLUMN:]) if header else []
    # Group the columns by value: each distinct curve, how many columns share
    # it, and the sub-area names it was printed under (for the flanders blend).
    sigs: dict[tuple[float, ...], int] = {}
    group_curves: list[list[float]] = []
    group_counts: list[int] = []
    group_names: list[list[str]] = []
    for idx, curve in enumerate(curves):
        if any(value != value for value in curve):  # a column with gaps
            continue
        sig = tuple(round(v, _RLP_CURVE_ROUNDING) for v in curve)
        group = sigs.get(sig)
        if group is None:
            group = len(group_curves)
            sigs[sig] = group
            group_curves.append(curve)
            group_counts.append(0)
            group_names.append([])
        group_counts[group] += 1
        group_names[group].append(str(col_names[idx]) if idx < len(col_names) else "")
    if not group_curves or not keys:
        raise ValueError("RLP sheet holds no complete curve")
    chosen = _blend_curves(group_curves, group_counts, group_names, blend)
    weights: RlpWeights = {}
    for index, key in enumerate(keys):
        weights[key] = weights.get(key, 0.0) + chosen[index]
    total = sum(weights.values())
    if not 0.99 < total < 1.01:
        raise ValueError(f"RLP weights sum to {total:.4f}, expected 1")
    return weights


def _blend_curves(
    group_curves: list[list[float]],
    group_counts: list[int],
    group_names: list[list[str]],
    blend: str,
) -> list[float]:
    """Reduce the distinct DSO groups to one per-quarter curve for ``blend``."""
    length = len(group_curves[0])
    if blend == "flanders":
        for curve, names in zip(group_curves, group_names, strict=True):
            if any(name.strip().lower().startswith("fluvius") for name in names):
                return curve
        raise ValueError("RLP sheet has no Fluvius curve for the flanders blend")
    if blend == "columns":
        total_cols = float(sum(group_counts))
        return [
            sum(curve[i] * count for curve, count in zip(group_curves, group_counts))
            / total_cols
            for i in range(length)
        ]
    count = float(len(group_curves))
    return [sum(curve[i] for curve in group_curves) / count for i in range(length)]
