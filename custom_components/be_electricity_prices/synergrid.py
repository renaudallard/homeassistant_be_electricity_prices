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
``synergrid.be/images/downloads/SLP-RLP-SPP/<year>/SPP_ex-ante_and_ex-post_<year>.xlsx``
in 2026; the address has changed every year, so when it is not served the
year's file is looked up on Synergrid's download page (``_listed_url``).
The file is ~52 MB, almost entirely the ex-post sheet, which we never touch: we
stream the download to a temp file and parse only the ex-ante sheet (a few MB of
XML) with the stdlib, keeping peak memory around 20 MB. The 2025 file carries
the national curve beside one per grid area, as ``ModelBE`` (``_VALUE_HEADERS``),
and on it 2025's months reproduce Energy Knights' Belpex-SPP-M to 0,004
EUR/MWh, where 2026's curve misses them by up to 0,14. The ex-post half lags by
about a year (the 2026 file stops in 2025S1), and that costs nothing: the
suppliers' own settled indices are computed on the ex-ante profile too, and
weighting it against the hourly day-ahead reproduces the Belpex-SPP-M Energy
Knights publishes to 0,007% over January to August 2026. Where a published index
disagrees it is over resolution, not profile vintage: see
``spot_stats._spp_month_mean``.

``fetch_spp_weights`` returns hourly-aggregated weights keyed by the UTC
``(month, day, hour)`` so they line up with the coordinator's hourly spot cache.
Any failure (download, format drift, 404 for a not-yet-published year) returns an
empty mapping so the caller degrades to the plain arithmetic mean.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import struct
import tempfile
import zipfile
import zlib
from collections.abc import Callable, Collection, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import IO, Any, Final, get_args
from urllib.parse import unquote, urljoin

import aiohttp

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

from .providers._pdf import USER_AGENT, read_text_capped
from .providers._rates import RlpBlend
from .providers.base import ExtractorError

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://www.synergrid.be/images/downloads/SLP-RLP-SPP"
# The download page lists every edition. The folder and the file name changed
# every year from 2022 to 2026, so the address built from _BASE_URL is only a
# guess for the next year; the page is read when that guess is not served.
_SITE = "https://www.synergrid.be/"
_PAGE_URL = f"{_SITE}nl/documentencentrum/statistieken-gegevens/profielen-slp-spp-rlp"
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_RLP_TOKENS = ("rlp", "elec", "all", "dso")
_SPP_TOKENS = ("spp", "ex-ante")
# The ex-ante sheet's name and the national curve's column header, resolved by
# prefix and text rather than position so a layout change fails rather than
# misreads. 2026 names them SPP_ex-ante_2026 and SPPExanteBE. 2025 names its
# sheet "Ex-ante 2025 (IP8)" and carries one column per grid area beside the
# national model, ModelBE. 2024's has the areas alone, as Energy Knights'
# per-area 2024 indices show, and is refused rather than read off one area.
_SHEET_PREFIXES = ("SPP_ex-ante", "Ex-ante")
_VALUE_HEADERS = ("SPPExanteBE", "ModelBE")
# The ex-ante sheet is one row per quarter-hour of its year under a header
# row, nested five deep (worksheet, sheetData, row, c, v): measured on 6
# November 2026, 35.041 rows in 2025 and 2026 and 35.137 in 2024, a leap
# year. Past these the sheet is not the year's curve, and the work a sheet
# of empty rows asks for stays bounded with the memory.
_SPP_MAX_ROWS: Final = 40_000
_SPP_MAX_DEPTH: Final = 64
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
# What a workbook's members may inflate to, counted on the bytes read
# (``WorkbookZip``), measured on 6 November 2026. The parts read before the
# sheet, the workbook, its relationships and shared strings, take 9 KB in
# every RLP file from 2024 to 2026 and 23 KB in the SPP ones; they are parsed
# whole, at some twenty times their size, so they get a budget of their own.
# Then the sheet: the SPP parse streams 9,5 MB of the 2026 file and 40,6 MB of
# the 2025 one, whose ex-ante sheet carries a curve per grid area, and the
# RLP parse reads a 20,8 MB sheet in every file. Four and three times those.
_PARTS_MAX_UNPACKED_BYTES: Final = 1024 * 1024
_SPP_MAX_UNPACKED_BYTES: Final = 160 * 1024 * 1024
_RLP_MAX_UNPACKED_BYTES: Final = 64 * 1024 * 1024
# What a workbook's central directory may take. Every real one measured, 2024
# to 2026, runs 0,8 to 2,8 KB, and the largest workbook read here, RESA's
# grid, 13 KB for its 157 entries.
_MAX_DIRECTORY_BYTES: Final = 256 * 1024

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"

# Hourly weights keyed by (month, day, hour) in UTC.
SppWeights = dict[tuple[int, int, int], float]
# (LOCAL month, day, hour) -> the hour's share of the year's residential load.
# Local, not UTC: Synergrid keys the RLP workbook's Year / Month / Day / h
# columns in Belgian local time with DST, and Eneco's published Belpex-RLP-M is
# reproduced to the cent only on that alignment. Keyed without the year, so a
# curve weights the days of its own year (``rlp_weights_for_year``).
RlpWeights = dict[tuple[int, int, int], float]

# Every reduction of the sheet a shipped card names. One download yields
# all of them, so the compare page can price each card on its own index.
RLP_BLENDS: Final[tuple[str, ...]] = get_args(RlpBlend)


@dataclass(frozen=True)
class _RlpGroups:
    """The RLP sheet reduced to its distinct DSO curves, before any blend.

    Held between the read and the blends because the read is the expensive
    half: one workbook pass yields this, and every blend is a combination of
    ``curves`` that costs a fraction of it.
    """

    # One (month, day, hour) per sheet row, in row order; the sheet is
    # quarter-hourly, so several rows share a key and their weights add.
    keys: list[tuple[int, int, int]]
    curves: list[list[float]]
    # How many columns carried each curve, and under which sub-area names.
    counts: list[int]
    names: list[list[str]]


async def fetch_rlp_blends(
    session: aiohttp.ClientSession, year: int, blends: Collection[str]
) -> dict[str, RlpWeights]:
    """Every requested blend of the year's RLP profile, from one download.

    The blends are reductions of the same sheet, so asking for several is one
    download and one workbook read with a cheap reduction apiece: measured on a
    Raspberry Pi 5 over the 2026 file, 16 s to read the 35.043 rows and 2 s to
    reduce them to one curve. That is what lets the compare page price each
    card on the index its own card names without a download per blend.

    A blend whose reduction fails is left out rather than failing the others,
    since only the flanders one can fail on its own (no Fluvius column). Never
    raises: a download or parse failure logs and returns what it has, and the
    caller keeps the plain arithmetic mean for whatever is missing.
    """
    wanted = list(dict.fromkeys(blends))
    if not wanted:
        return {}
    url = f"{_BASE_URL}/{year}/RLP0N%20{year}%20Electricity%20all%20DSOs.xlsb"
    try:
        path, url = await _download_edition(
            session, url, year, _RLP_TOKENS, suffix=".xlsb"
        )
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as err:
        _LOGGER.warning("Synergrid RLP download failed (%s): %s", url, err)
        return {}
    try:
        return await asyncio.to_thread(_parse_rlp_blends, path, wanted)
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
        path, url = await _download_edition(session, url, year, _SPP_TOKENS)
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
        # changes: the docstring promises this function never raises.
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


async def _download_edition(
    session: aiohttp.ClientSession,
    url: str,
    year: int,
    tokens: tuple[str, ...],
    **kwargs: str,
) -> tuple[Path, str]:
    """Download the year's workbook from ``url``, or from where the page lists it.

    Only an HTTP error sends it to the page: a timeout or a reset says nothing
    about the address. Returns the file and the address it came from, so a
    later log line names the one that was read.
    """
    try:
        return await _download(session, url, **kwargs), url
    except aiohttp.ClientResponseError:
        listed = await _listed_url(session, year, tokens, kwargs.get("suffix", ".xlsx"))
        if listed is None or listed == url:
            raise
    return await _download(session, listed, **kwargs), listed


async def _listed_url(
    session: aiohttp.ClientSession, year: int, tokens: tuple[str, ...], suffix: str
) -> str | None:
    """The download page's link to the year's workbook, or None.

    A link qualifies when it stays on Synergrid's site and its file name,
    lower-cased, carries the year, every token and the suffix. The page lists
    a year's revisions oldest first (spp-2023-...-v1.0, then v1.1), so the
    last match is the current one.
    """
    try:
        async with session.get(
            _PAGE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status >= 400:
                return None
            html = await read_text_capped(resp, _PAGE_URL)
    except (aiohttp.ClientError, TimeoutError, ExtractorError) as err:
        _LOGGER.debug("Synergrid download page unavailable: %s", err)
        return None
    found: str | None = None
    for href in _HREF_RE.findall(html):
        url = urljoin(_PAGE_URL, href)
        name = unquote(url.rsplit("/", 1)[-1]).lower()
        if (
            url.startswith(_SITE)
            and name.endswith(suffix)
            and str(year) in name
            and all(token in name for token in tokens)
        ):
            found = url
    return found


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
    starts throttling dirty pages: long enough for HA to log a blocking-call
    warning and for every other integration's callbacks to stall behind it.
    Chunks are accumulated into a bounded buffer so the offload happens a few
    times rather than once per network read.
    """

    def _open_temp() -> IO[bytes]:
        # Named factory with a concrete return type: asyncio.to_thread cannot
        # resolve NamedTemporaryFile's overloads, so it picked the text one.
        return tempfile.NamedTemporaryFile(mode="w+b", delete=False, suffix=suffix)

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


# The end of central directory record and the signature of the ZIP64 locator
# that would sit just before it, as zipfile reads them.
_END_RECORD: Final = struct.Struct("<4s4H2LH")
_END_SIGNATURE: Final = b"PK\x05\x06"
_ZIP64_LOCATOR: Final = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE: Final = 20
_MAX_ZIP_COMMENT: Final = 0xFFFF
# Stored and deflated are the methods every workbook here uses, and the two
# a read of a given size holds to that size. Python 3.13's ZipFile inflates
# the others with no limit on a single call: a 38 KB lzma member inflated
# its whole 256 MiB on a read of one MiB.
_READ_METHODS: Final = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
# Encrypted, patched or strongly encrypted members: ZipFile refuses them
# with RuntimeError or NotImplementedError, which the fetchers here do not
# expect, so they are refused before it opens them.
_UNREAD_FLAGS: Final = 0x01 | 0x20 | 0x40
_READ_PIECE: Final = 1024 * 1024


class WorkbookZip:
    """A workbook's zip, its members read through one budget of inflated bytes.

    A zip states how large each member unpacks to and nothing holds it to
    that: ZipFile.read inflates up to 2 GiB in one call before it compares
    the result with the size stated, so a capped sum of the stated sizes
    caps nothing. Every read here counts the bytes it really inflates, a
    workbook's members together, and refuses the workbook past ``budget``.
    The central directory is checked before ZipFile reads it, since it makes
    an object per entry for as many entries as the directory's size holds.

    It has the ZipFile surface pyxlsb's Workbook uses (``open`` and
    ``close``), so the RLP reader is held to the same budget. Refuses with
    BadZipFile or ValueError, and raises KeyError for a member the workbook
    does not hold, as ZipFile does.
    """

    def __init__(self, source: Path | bytes, budget: int) -> None:
        self._file: IO[bytes] = (
            source.open("rb") if isinstance(source, Path) else io.BytesIO(source)
        )
        try:
            _check_directory(self._file)
            self._zip = zipfile.ZipFile(self._file)
        except NotImplementedError as err:
            # A directory entry stating a zip version past what ZipFile reads.
            self._file.close()
            raise zipfile.BadZipFile(str(err)) from err
        except BaseException:
            self._file.close()
            raise
        self._left = budget
        self._vet: Callable[[bytes], None] | None = None

    def __enter__(self) -> WorkbookZip:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._zip.close()
        self._file.close()

    def open(self, name: str, mode: str = "r") -> _Member | io.BytesIO:
        """Member ``name`` as a file whose reads count against the budget,
        read whole and vetted first once :meth:`allow` was given a vet."""
        if mode != "r":
            raise ValueError(f"workbook members are read only, not {mode!r}")
        info = self._zip.getinfo(name)
        if info.compress_type not in _READ_METHODS or info.flag_bits & _UNREAD_FLAGS:
            raise ValueError(
                f"workbook member {name} is not plainly stored or deflated"
            )
        member = _Member(self._zip.open(info), self)
        if self._vet is None:
            return member
        with member:
            data = member.read()
        self._vet(data)
        return io.BytesIO(data)

    def read(self, name: str) -> bytes:
        """Member ``name`` whole, counted against the budget."""
        with self.open(name) as member:
            return member.read()

    def allow(self, more: int, vet: Callable[[bytes], None] | None = None) -> None:
        """Let the reads that follow inflate ``more`` bytes besides what the
        budget has left: a sheet gets its own, past what the parts read
        before it were allowed. With ``vet``, a member opened from then on is
        read whole and passed to it first, so a reader that builds what a
        sheet's records say as it opens the sheet, as pyxlsb does, is never
        handed one ``vet`` refuses."""
        self._left += more
        self._vet = vet

    def _take(self, fh: IO[bytes], size: int) -> bytes:
        """Up to ``size`` bytes of a member, all of it for a negative size.

        No read asks for more than one byte past what the budget has left:
        that byte tells a workbook that fits from one that does not. A read
        to the end goes a MiB at a time, since one inflate holds what it
        returns twice over as it finishes, and a member past the budget is
        refused before its pieces are joined.
        """
        try:
            if size >= 0:
                return self._count(fh.read(min(size, self._left + 1)))
            pieces = []
            while piece := self._count(fh.read(min(_READ_PIECE, self._left + 1))):
                pieces.append(piece)
        except zlib.error as err:
            # A stream that does not inflate: ZipFile lets zlib's own error
            # through, which the fetchers here do not expect.
            raise zipfile.BadZipFile(str(err)) from err
        return b"".join(pieces)

    def _count(self, data: bytes) -> bytes:
        self._left -= len(data)
        if self._left < 0:
            raise ValueError("workbook unpacks past the size cap")
        return data


class _Member:
    """One member of a :class:`WorkbookZip`, opened for reading."""

    def __init__(self, fh: IO[bytes], book: WorkbookZip) -> None:
        self._fh = fh
        self._book = book

    def __enter__(self) -> _Member:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._fh.close()

    def read(self, size: int = -1) -> bytes:
        return self._book._take(self._fh, size)


def _check_directory(fh: IO[bytes]) -> None:
    """Refuse a zip whose central directory is larger than a workbook's.

    The end record is found where zipfile looks for it, so the size checked
    is the one zipfile reads. That size is what bounds the entries: zipfile
    reads entries until it has read that many bytes, whatever count the
    record states, and an entry takes at least 46 of them. ZIP64 records are
    refused rather than read, since zipfile takes its sizes from them
    instead and no workbook needs them.
    """
    end = fh.seek(0, os.SEEK_END)
    fh.seek(max(end - _END_RECORD.size - _MAX_ZIP_COMMENT - _ZIP64_LOCATOR_SIZE, 0))
    tail = fh.read()
    # A record with no comment ends the file, and zipfile takes that one
    # before it searches for the last signature.
    at = len(tail) - _END_RECORD.size
    if at < 0 or not (
        tail.startswith(_END_SIGNATURE, at) and tail.endswith(b"\x00\x00")
    ):
        at = tail.rfind(_END_SIGNATURE)
    if at < 0 or len(tail) - at < _END_RECORD.size:
        raise zipfile.BadZipFile("File is not a zip file")
    if at >= _ZIP64_LOCATOR_SIZE and tail.startswith(
        _ZIP64_LOCATOR, at - _ZIP64_LOCATOR_SIZE
    ):
        raise ValueError("workbook zip carries ZIP64 records")
    size = _END_RECORD.unpack_from(tail, at)[5]
    if size > _MAX_DIRECTORY_BYTES:
        raise ValueError(f"workbook zip directory takes {size} bytes")


def _resolve_sheet_path(
    z: WorkbookZip, prefix: str | tuple[str, ...] = _SHEET_PREFIXES
) -> str:
    """Map the first sheet whose name starts with ``prefix``, or with one of
    them (the ex-ante sheet by default), to its worksheet XML member."""
    for name, path in sheet_paths(z):
        if name.startswith(prefix):
            return path
    raise KeyError(f"no sheet starting {prefix!r}")


def sheet_paths(z: WorkbookZip) -> list[tuple[str, str]]:
    """Each worksheet's name and XML member, in the workbook's order; a sheet
    whose relationship names no target is left out."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {
        rel.get("Id"): rel.get("Target") or ""
        for rel in rels.iter(_PKG_REL_NS + "Relationship")
    }
    paths: list[tuple[str, str]] = []
    for sheet in wb.iter(_NS + "sheet"):
        target = targets.get(sheet.get(_REL_NS + "id"))
        if target is not None:
            paths.append((sheet.get("name") or "", "xl/" + target.lstrip("/")))
    return paths


def sheet_rows(z: WorkbookZip, path: str, strings: list[str]) -> list[dict[str, str]]:
    """One worksheet's cells as text, one ``{column: text}`` per row, the
    shared strings resolved and blanks left out."""
    sheet = ET.fromstring(z.read(path))
    rows: list[dict[str, str]] = []
    for row in sheet.iter(_NS + "row"):
        cells: dict[str, str] = {}
        for cell in row.iter(_NS + "c"):
            kind = cell.get("t")
            if kind == "inlineStr":
                text = "".join(t.text or "" for t in cell.iter(_NS + "t"))
            else:
                value = cell.find(_NS + "v")
                if value is None or value.text is None:
                    continue
                text = strings[int(value.text)] if kind == "s" else value.text
            cells[_column_of(cell.get("r", ""))] = text.strip()
        rows.append(cells)
    return rows


def _shared_strings(z: WorkbookZip) -> list[str]:
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

    Streams the worksheet XML, dropping each element once read so peak memory
    stays a few tens of MB regardless of the 52 MB file. The UTC instant and value
    columns are resolved from the header row (by name, not fixed position); the
    hour is taken from the UTC column, not the local Year/Month/Day/Hour columns.
    A header naming neither curve raises there, before the rest of the sheet is
    read.
    """
    cell, row, value = _NS + "c", _NS + "row", _NS + "v"
    with WorkbookZip(path, _PARTS_MAX_UNPACKED_BYTES) as z:
        target = _resolve_sheet_path(z)
        strings = _shared_strings(z)
        z.allow(_SPP_MAX_UNPACKED_BYTES)
        columns: tuple[str, str] | None = None
        weights: SppWeights = {}
        row_cells: dict[str, tuple[str | None, str | None]] = {}
        # The open elements, and for each open cell whether its first value
        # has been read and its text. Every element is dropped from its
        # parent once it ends, so the tree holds the open path and no more:
        # a cleared element still hangs off its parent, and a sheet of empty
        # rows inside the budget held 470 MB that way. The tree runs up to a
        # chunk ahead of the events, so nothing is looked up in it: a cell's
        # value is read off its value's own end event.
        open_path: list[Any] = []
        values: list[tuple[bool, str | None]] = []
        rows = 0
        with z.open(target) as fh:
            for event, el in ET.iterparse(fh, events=("start", "end")):
                if event == "start":
                    open_path.append(el)
                    if len(open_path) > _SPP_MAX_DEPTH:
                        raise ValueError("the ex-ante sheet nests too deep")
                    if el.tag == cell:
                        values.append((False, None))
                    continue
                open_path.pop()
                if el.tag == value:
                    if open_path and open_path[-1].tag == cell and not values[-1][0]:
                        values[-1] = (True, el.text)
                elif el.tag == cell:
                    row_cells[_column_of(el.get("r", ""))] = (
                        el.get("t"),
                        values.pop()[1],
                    )
                elif el.tag == row:
                    rows += 1
                    if rows > _SPP_MAX_ROWS:
                        raise ValueError("the ex-ante sheet holds more than a year")
                    if int(el.get("r", "0")) == 1:
                        columns = _header_columns(row_cells, strings)
                    elif columns is not None:
                        _accumulate_row(row_cells, columns, weights)
                    row_cells = {}
                if open_path:
                    del open_path[-1][:]
    if columns is None:
        raise KeyError("the ex-ante sheet has no header row")
    return weights


def _header_columns(
    cells: dict[str, tuple[str | None, str | None]], strings: list[str]
) -> tuple[str, str]:
    """The UTC and national curve column letters off the header row.

    The first of ``_VALUE_HEADERS`` the row names is the curve; a row naming
    none, or no UTC column, raises KeyError.
    """
    header: dict[str, str] = {}
    for col, (ctype, text) in cells.items():
        name = strings[int(text)] if ctype == "s" and text is not None else text
        if name:
            header[name] = col
    value = next((header[name] for name in _VALUE_HEADERS if name in header), None)
    if _UTC_HEADER not in header or value is None:
        raise KeyError(f"{_UTC_HEADER!r} or one of {_VALUE_HEADERS!r} not found")
    return header[_UTC_HEADER], value


def _accumulate_row(
    cells: dict[str, tuple[str | None, str | None]],
    columns: tuple[str, str],
    weights: SppWeights,
) -> None:
    utc_column, value_column = columns
    try:
        serial = _cell_number(cells[utc_column])
        value = _cell_number(cells[value_column])
        if serial is None or value is None:
            return
        # Excel serial -> UTC datetime; +30s absorbs float imprecision before
        # the hour is floored (the quarter's minute is irrelevant once
        # aggregated). An out-of-range serial raises OverflowError: skip that
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
# The sheet's size, measured on 6 November 2026 over the 2024 to 2026 files:
# 32 columns, and 35.041 to 35.139 rows, one per quarter-hour of the year
# under its header rows. pyxlsb builds every row as wide as the dimension
# the sheet declares, and every row up to the last one a record numbers,
# empty or not, so both are held to these: a 596 byte file declaring four
# billion columns ran 27 s and took 1,4 GB.
_RLP_MAX_COLUMNS: Final = 64
_RLP_MAX_ROWS: Final = 40_000
# And its records, measured on the same files: 1.261.502 to 1.265.018, three
# of them column records (BrtColInfo). pyxlsb walks every record as it reads
# the sheet, and keeps every column record ahead of the data: a 153 KB file
# of three and a half million of them held 376 MB for 132 s inside the
# budget. So the sheet is scanned before pyxlsb is handed it, and refused past
# a column record per column it may declare, or past the records the largest
# sheet it may declare holds, a row record and a cell per column a row.
_BIFF12_COLUMN: Final = 0x003C
_RLP_MAX_RECORDS: Final = _RLP_MAX_ROWS * (_RLP_MAX_COLUMNS + 1)


def _parse_rlp_blends(path: Path, blends: Collection[str]) -> dict[str, RlpWeights]:
    """Read the workbook once and reduce it to each requested blend.

    The read is the expensive half, so the sheet is grouped into its distinct
    DSO curves once and every blend is a combination of those groups. A blend
    that cannot be reduced is dropped from the result; the others stand.
    """
    from pyxlsb import Workbook

    # pyxlsb reads each member whole through the zip it is handed, which
    # open_workbook would make a plain ZipFile. Opening the workbook reads
    # its parts; the sheet is read by get_sheet.
    with (
        WorkbookZip(path, _PARTS_MAX_UNPACKED_BYTES) as z,
        Workbook(fp=z) as workbook,
    ):
        z.allow(_RLP_MAX_UNPACKED_BYTES, vet=_check_rlp_records)
        with workbook.get_sheet(_RLP_SHEET) as sheet:
            groups = _rlp_groups_from_rows(_rlp_rows(sheet))
    out: dict[str, RlpWeights] = {}
    for blend in dict.fromkeys(blends):
        try:
            out[blend] = _weights_for_blend(groups, blend)
        except ValueError as err:
            _LOGGER.warning("Synergrid RLP blend %s unusable: %s", blend, err)
    if not out:
        raise ValueError("RLP sheet yielded no usable blend")
    return out


def _check_rlp_records(data: bytes) -> None:
    """Refuse an RLP sheet holding more records or column records than the
    RLP sheet may, or a record running past the sheet's end. Each record's
    type and size are read as pyxlsb's reader reads them: the type's bytes
    low first while the top bit is set, the size seven bits a byte, at most
    four bytes each, and a type or size cut short ends the sheet."""
    at, end = 0, len(data)
    records = columns = 0
    while at < end:
        kind = 0
        for shift in (0, 8, 16, 24):
            if at == end:
                return
            byte = data[at]
            at += 1
            kind += byte << shift
            if not byte & 0x80:
                break
        size = 0
        for shift in (0, 7, 14, 21):
            if at == end:
                return
            byte = data[at]
            at += 1
            size += (byte & 0x7F) << shift
            if not byte & 0x80:
                break
        at += size
        if at > end:
            raise ValueError("RLP sheet holds a record running past its end")
        records += 1
        if records > _RLP_MAX_RECORDS:
            raise ValueError(f"RLP sheet holds more than {_RLP_MAX_RECORDS} records")
        if kind == _BIFF12_COLUMN:
            columns += 1
            if columns > _RLP_MAX_COLUMNS:
                raise ValueError(
                    f"RLP sheet holds more than {_RLP_MAX_COLUMNS} column records"
                )


def _rlp_rows(sheet: Any) -> Iterator[list[Any]]:
    """The sheet's rows as values, refusing a sheet wider or longer than the
    RLP's: the width before a row is built, the length as the rows come."""
    dimension = sheet.dimension
    width = None if dimension is None else dimension.c + dimension.w
    if width is None or width > _RLP_MAX_COLUMNS:
        raise ValueError(f"RLP sheet declares {width} columns")
    for count, row in enumerate(sheet.rows(), 1):
        if count > _RLP_MAX_ROWS:
            raise ValueError(f"RLP sheet holds more than {_RLP_MAX_ROWS} rows")
        yield [c.v for c in row]


def _rlp_groups_from_rows(rows: Iterable[list[Any]]) -> _RlpGroups:
    """The sheet's distinct DSO curves, which every blend is built from.

    Synergrid's workbook lists one column per DSO sub-area, but only three
    curves are distinct (Fluvius, the Walloon DSOs with the small ones,
    Sibelga): the same Fluvius curve appears eight times. Curves are
    deduplicated by value, not by name, so a sub-area that gains its own curve
    counts once, and each group keeps how many columns share it and the names
    they were printed under, which is what the blends select on.

    Raises ``ValueError`` when the sheet holds no complete curve, which is what
    a wrong sheet or a truncated download looks like.
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
    return _RlpGroups(keys, group_curves, group_counts, group_names)


def _weights_for_blend(groups: _RlpGroups, blend: str) -> RlpWeights:
    """One DSO blend of the grouped curves, summed to local clock hours.

    Three suppliers read the same sheet three ways, and each reproduces its own
    published values to the cent, so the blend is what the caller asks for:

      - "distinct": the equal mean of the three distinct curves. Eneco's
        Belpex-RLP-M; averaging the columns as printed instead weights Flanders
        eight to one and misses Eneco by up to 2,2 EUR/MWh in summer.
      - "columns": the mean over every column, i.e. each distinct curve
        weighted by how many sub-areas share it. energie.be's Belpex_RLP,
        which its card defines as the mean "van de verschillende
        distributienetbeheerders" read literally.
      - "flanders": the Fluvius curve alone, identified by name. Energy Knights
        sells in Flanders only and bills on the customer's DSO.
      - "wallonia": the Walloon DSOs' curve alone (ORES and the DSOs sharing
        its curve), identified by name. Bolt's printed quarterly Belpex index
        is this curve's RLP-weighted quarter mean, in every region.

    Raises ``ValueError`` when the flanders or wallonia blend finds no column
    of its own, or
    when the weights do not sum to about one over the year.
    """
    chosen = _blend_curves(groups.curves, groups.counts, groups.names, blend)
    weights: RlpWeights = {}
    for index, key in enumerate(groups.keys):
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
    named = {"flanders": "Fluvius", "wallonia": "ORES"}.get(blend)
    if named is not None:
        for curve, names in zip(group_curves, group_names, strict=True):
            if any(n.strip().lower().startswith(named.lower()) for n in names):
                return curve
        raise ValueError(f"RLP sheet has no {named} curve for the {blend} blend")
    if blend == "columns":
        total_cols = float(sum(group_counts))
        return [
            sum(
                curve[i] * count
                for curve, count in zip(group_curves, group_counts, strict=True)
            )
            / total_cols
            for i in range(length)
        ]
    count = float(len(group_curves))
    return [sum(curve[i] for curve in group_curves) / count for i in range(length)]
