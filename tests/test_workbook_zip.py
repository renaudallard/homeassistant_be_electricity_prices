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

"""The workbooks Synergrid, Fluvius and the CWaPE publish, read only through
a budget of what their members really inflate to.

The zips here are crafted: each is the attack itself. The real workbooks are
the fixtures, read by the readers' own tests and below for their margin.
"""

from __future__ import annotations

import io
import struct
import tracemalloc
import zipfile
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.be_electricity_prices import cwape, fluvius, synergrid

_FIXTURES = Path(__file__).parent / "fixtures"
_MIB = 1024 * 1024
_MAIN = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
_RELS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" Target="worksheets/sheet1.{}"/>'
    "</Relationships>"
)


def _entry(raw: bytes | bytearray, member: str) -> int:
    """Where ``member``'s central directory entry starts in ``raw``."""
    at = raw.find(b"PK\x01\x02")
    while at >= 0:
        size = struct.unpack_from("<H", raw, at + 28)[0]
        if raw[at + 46 : at + 46 + size] == member.encode():
            return at
        at = raw.find(b"PK\x01\x02", at + 4)
    raise AssertionError(f"{member} is not in the directory")


def _zip(
    member: str,
    inflated: int,
    *,
    stated: int | None = None,
    method: int = zipfile.ZIP_DEFLATED,
    others: dict[str, str | bytes] | None = None,
) -> bytes:
    """A zip whose ``member`` inflates to ``inflated`` blanks, the directory
    stating ``stated`` bytes for it (the truth for None), beside ``others``."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, text in (others or {}).items():
            z.writestr(name, text)
        with z.open(member, "w") as fh:
            left = inflated
            while left > 0:
                fh.write(b" " * min(left, _MIB))
                left -= _MIB
    raw = bytearray(buf.getvalue())
    if stated is not None:
        struct.pack_into("<I", raw, _entry(raw, member) + 24, stated)
    return bytes(raw)


def _record(kind: int, data: bytes = b"") -> bytes:
    """One BIFF12 record as pyxlsb reads it: the id's bytes, low first, the
    length seven bits a byte, then the data."""
    head = bytes([kind]) if kind < 0x80 else bytes([kind & 0xFF, kind >> 8])
    size, length = len(data), bytearray()
    while True:
        length.append((size & 0x7F) | (0x80 if size > 0x7F else 0))
        size >>= 7
        if not size:
            return head + bytes(length) + data


def _text(value: str) -> bytes:
    return struct.pack("<I", len(value)) + value.encode("utf-16-le")


# The RLP workbook's part listing its one sheet, under the name read.
_RLP_WORKBOOK = (
    _record(0x0183)
    + _record(0x018F)
    + _record(0x019C, struct.pack("<II", 0, 1) + _text("rId1") + _text("RLP96UbyDGO"))
    + _record(0x0190)
    + _record(0x0184)
)


def _rlp_sheet(
    dimension: tuple[int, int, int, int], rows: list[tuple[int, list[float]]]
) -> bytes:
    """An RLP sheet declaring ``dimension``, its first and last row and first
    and last column, and holding ``rows``, each its number and its figures."""
    body = b"".join(
        _record(0x0000, struct.pack("<I", number))
        + b"".join(
            _record(0x0005, struct.pack("<IId", column, 0, figure))
            for column, figure in enumerate(figures)
        )
        for number, figures in rows
    )
    return (
        _record(0x0181)
        + _record(0x0194, struct.pack("<IIII", *dimension))
        + _record(0x0191)
        + body
        + _record(0x0192)
        + _record(0x0182)
    )


def _rlp_parts() -> dict[str, str | bytes]:
    return {
        "xl/_rels/workbook.bin.rels": _RELS.format("bin"),
        "xl/workbook.bin": _RLP_WORKBOOK,
    }


def _spp_parts() -> dict[str, str | bytes]:
    return {
        "xl/workbook.xml": (
            f'<workbook {_MAIN} xmlns:r="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships"><sheets><sheet name='
            '"SPP_ex-ante_2026" sheetId="1" r:id="rId1"/></sheets></workbook>'
        ),
        "xl/_rels/workbook.xml.rels": _RELS.format("xml"),
        "xl/sharedStrings.xml": (
            f"<sst {_MAIN}><si><t>UTC</t></si><si><t>SPPExanteBE</t></si></sst>"
        ),
    }


def _spp(payload: bytes, tmp_path: Path) -> object:
    path = tmp_path / "spp.xlsx"
    path.write_bytes(payload)
    return synergrid._parse_hourly_weights(path)


def _rlp(payload: bytes, tmp_path: Path) -> object:
    path = tmp_path / "rlp.xlsb"
    path.write_bytes(payload)
    return synergrid._parse_rlp_blends(path, ["distinct"])


# Each reader, a member it reads, and the parts it reads before that one. The
# Synergrid sheets are read on budgets of their own, past their parts'.
_READERS: dict[
    str, tuple[Callable[[bytes, Path], object], str, dict[str, str | bytes]]
] = {
    "fluvius": (lambda p, _t: fluvius.parse_sheet(p, 2026), "xl/workbook.xml", {}),
    "cwape": (lambda p, _t: cwape.parse_grid(p), "xl/workbook.xml", {}),
    "spp": (_spp, "xl/workbook.xml", {}),
    "spp sheet": (_spp, "xl/worksheets/sheet1.xml", _spp_parts()),
    # pyxlsb streams the relationships and reads the workbook part whole.
    "rlp": (
        _rlp,
        "xl/workbook.bin",
        {"xl/_rels/workbook.bin.rels": _RELS.format("bin")},
    ),
    "rlp sheet": (_rlp, "xl/worksheets/sheet1.bin", _rlp_parts()),
}


def _held(call: Callable[[], object]) -> tuple[BaseException | None, int]:
    """What ``call`` raised, and the most memory it held while it ran."""
    tracemalloc.start()
    try:
        call()
    except Exception as err:  # the refusal is what is looked at
        return err, tracemalloc.get_traced_memory()[1]
    else:
        return None, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.fixture
def small_budgets() -> object:
    """Every reader on a budget of a MiB, the Synergrid ones included."""
    with (
        patch.object(fluvius, "_MAX_UNPACKED_BYTES", _MIB),
        patch.object(cwape, "_MAX_UNPACKED_BYTES", _MIB),
        patch.object(synergrid, "_SPP_MAX_UNPACKED_BYTES", _MIB, create=True),
        patch.object(synergrid, "_RLP_MAX_UNPACKED_BYTES", _MIB, create=True),
    ):
        yield


@pytest.mark.usefixtures("small_budgets")
@pytest.mark.parametrize("stated", [1024, None], ids=["understated", "stated"])
@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_member_inflating_past_the_budget_is_cut_off_there(
    reader: str, stated: int | None, tmp_path: Path
) -> None:
    """A member that inflates to 64 MiB is refused at the reader's budget,
    whatever size the zip states for it: ZipFile.read would inflate all of
    it before comparing it with a stated size it understates."""
    parse, member, others = _READERS[reader]
    payload = _zip(member, 64 * _MIB, stated=stated, others=others)
    raised, peak = _held(lambda: parse(payload, tmp_path))
    assert isinstance(raised, (zipfile.BadZipFile, ValueError)), raised
    assert peak < 8 * _MIB, f"{reader} held {peak / _MIB:.0f} MiB"


@pytest.mark.parametrize("reader", sorted(_READERS))
def test_a_member_in_another_compression_method_is_refused_unread(
    reader: str, tmp_path: Path
) -> None:
    """Python 3.13's ZipFile inflates an lzma or bzip2 member whole in one
    call, whatever size is asked for, so only stored and deflated members
    are read at all."""
    parse, member, others = _READERS[reader]
    payload = _zip(
        member, 64 * _MIB, stated=1024, method=zipfile.ZIP_LZMA, others=others
    )
    raised, peak = _held(lambda: parse(payload, tmp_path))
    assert isinstance(raised, ValueError), raised
    assert "not plainly stored or deflated" in str(raised)
    assert peak < 8 * _MIB, f"{reader} held {peak / _MIB:.0f} MiB"


def test_a_directory_larger_than_a_workbook_s_is_refused_before_it_is_read() -> None:
    """ZipFile reads the whole central directory as it opens, an object per
    entry, so its size is checked first: a 64 MiB directory holds 1,4
    million entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for i in range(5000):
            z.writestr(f"xl/media/image{i:04d}.png", b"")
    with (
        patch.object(zipfile, "ZipFile", side_effect=AssertionError("read it")),
        pytest.raises(ValueError, match="directory takes"),
    ):
        fluvius.parse_sheet(buf.getvalue(), 2026)


def test_a_zip64_directory_is_refused() -> None:
    """ZipFile takes the directory's size from the ZIP64 records when they
    are there, and no workbook needs them, so they are not read."""
    buf = io.BytesIO()
    with (
        patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1),
        zipfile.ZipFile(buf, "w") as z,
    ):
        z.writestr("xl/workbook.xml", f"<workbook {_MAIN}/>")
        z.writestr("xl/_rels/workbook.xml.rels", _RELS.format("xml"))
    with pytest.raises(ValueError, match="ZIP64"):
        fluvius.parse_sheet(buf.getvalue(), 2026)


def _encrypted(raw: bytearray, member: str) -> None:
    struct.pack_into("<H", raw, _entry(raw, member) + 8, 0x01)


def _later_version(raw: bytearray, member: str) -> None:
    struct.pack_into("<H", raw, _entry(raw, member) + 6, 99)


def _bad_stream(raw: bytearray, member: str) -> None:
    header = struct.unpack_from("<I", raw, _entry(raw, member) + 42)[0]
    name, extra = struct.unpack_from("<HH", raw, header + 26)
    raw[header + 30 + name + extra] = 0x07  # a final block of a reserved type


@pytest.mark.parametrize(
    "damage",
    [_encrypted, _later_version, _bad_stream],
    ids=[
        "encrypted member",
        "zip version past ZipFile's",
        "stream that does not inflate",
    ],
)
@pytest.mark.parametrize("fetch", ["spp", "rlp"])
async def test_a_workbook_zipfile_refuses_otherwise_still_leaves_the_fetch_empty(
    fetch: str, damage: Callable[[bytearray, str], None], tmp_path: Path
) -> None:
    """ZipFile refuses these with RuntimeError, NotImplementedError or zlib's
    own error, none of which the two fetches, promised never to raise,
    catch: the reader refuses them as a workbook that does not read."""
    member = "xl/workbook.bin" if fetch == "rlp" else "xl/workbook.xml"
    raw = bytearray(_zip(member, 1024, others=_READERS[fetch][2]))
    damage(raw, member)
    path = tmp_path / f"{fetch}.xlsx"
    path.write_bytes(raw)

    async def _download(*_a: object, **_k: object) -> Path:
        return path

    with patch.object(synergrid, "_download", _download):
        if fetch == "spp":
            assert await synergrid.fetch_spp_weights(MagicMock(), 2026) == {}
        else:
            assert (
                await synergrid.fetch_rlp_blends(MagicMock(), 2026, ["distinct"]) == {}
            )


@pytest.mark.parametrize(
    "fixture", sorted(p.name for p in _FIXTURES.glob("*.xlsx")), ids=str
)
def test_a_real_workbook_reads_inside_a_tenth_of_its_budget(fixture: str) -> None:
    """The budgets are sized on what the readers read of the real workbooks,
    with ten times that to spare."""
    payload = (_FIXTURES / fixture).read_bytes()
    if fixture.startswith("fluvius_"):
        tenth = fluvius._MAX_UNPACKED_BYTES // 10
        with patch.object(fluvius, "_MAX_UNPACKED_BYTES", tenth):
            assert fluvius.parse_sheet(payload, int(fixture[-9:-5])) is not None
        return
    tenth = cwape._MAX_UNPACKED_BYTES // 10
    parse = cwape.parse_transport if "transport" in fixture else cwape.parse_grid
    with patch.object(cwape, "_MAX_UNPACKED_BYTES", tenth):
        assert parse(payload) is not None


def _ex_ante(body: str) -> bytes:
    """An SPP workbook whose ex-ante sheet is a header row, then ``body``."""
    header = '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in _spp_parts().items():
            z.writestr(name, text)
        z.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet {_MAIN}><sheetData>{header}{body}</sheetData></worksheet>",
        )
    return buf.getvalue()


# One quarter-hour, 1 July 2026 00:00 UTC, and its weight.
_QUARTER = '<row r="{}"><c r="A{}"><v>46204</v></c><c r="B{}"><v>0.25</v></c></row>'


def test_the_ex_ante_parse_holds_nothing_behind_it(tmp_path: Path) -> None:
    """The ex-ante sheet streams, but a cleared element still hangs off its
    parent: 2 MiB of elements between two rows held 30 MB and more, and a
    sheet of empty rows inside the budget 470 MB. Each element is dropped
    once read, and the row after them still counts."""
    body = "<x/>" * (2 * _MIB // 4) + _QUARTER.format(2, 2, 2)
    raised, peak = _held(lambda: _spp(_ex_ante(body), tmp_path))
    assert raised is None, raised
    assert synergrid._parse_hourly_weights(tmp_path / "spp.xlsx") == {(7, 1, 0): 0.25}
    assert peak < 8 * _MIB, f"held {peak / _MIB:.0f} MiB"


@pytest.mark.usefixtures("small_budgets")
def test_the_ex_ante_sheet_is_read_on_its_own_budget(tmp_path: Path) -> None:
    """The sheet streams, so its budget is its own rather than its parts':
    a sheet of elements past it is refused there."""
    with pytest.raises(ValueError, match="unpacks past the size cap"):
        _spp(_ex_ante("<x/>" * (2 * _MIB // 4)), tmp_path)


@pytest.mark.parametrize(
    ("body", "refusal"),
    [
        ("<row/>" * 40_000, "more than a year"),
        ("<x>" * 65 + "</x>" * 65, "nests too deep"),
    ],
    ids=["rows past a year", "nesting"],
)
def test_an_ex_ante_sheet_past_its_shape_is_refused(
    body: str, refusal: str, tmp_path: Path
) -> None:
    """One row per quarter-hour of a year, five deep: a sheet holding more is
    not the ex-ante curve, and what reading it costs is held to that."""
    with pytest.raises(ValueError, match=refusal):
        _spp(_ex_ante(body), tmp_path)
