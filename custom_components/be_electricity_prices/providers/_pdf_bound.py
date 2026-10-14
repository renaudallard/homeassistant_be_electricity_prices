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

"""What a card's PDF streams may decode to: checked before pypdf reads its text,
and held to while pdfminer, under pdfplumber, reads it."""

from __future__ import annotations

import threading
import zlib
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import pypdf
from pypdf.errors import LimitReachedError
from pypdf.filters import ASCII85Decode, ASCIIHexDecode
from pypdf.generic import ArrayObject, IndirectObject, StreamObject

from .base import ExtractorError

# What every stream of a card may decode to, together, counted as
# require_bounded_reader says. Across the 179 shipped fixtures the most
# counted is 9,9 MB, a 2,4 MB Bolt card. Across the 2468 cards the archive
# held on 2026-10-14 the most counted is 16,6 MB, Bolt's professional Plenty
# fixed card of June 2026 with its damaged stream, so this leaves twice that.
_MAX_DECODED_STREAM_BYTES = 32 * 1024 * 1024

# A damaged FlateDecode stream counts what it decodes to again, once for
# every this many bytes of the stream. zlib refuses one cut short or failing
# its checksum, and pdfminer then inflates it again a byte at a time,
# appending each piece to what it holds: a cost of its length times its
# output. The costliest in the archive, 211 kB decoding to 658 kB on that
# Bolt card, takes 12,6 s of CPU on a Raspberry Pi and counts 8,9 MB more.
# Under the bound one damaged stream can carry close to four times that
# length times output, which took 52 to 105 s for text, random bytes or
# blanks cut short.
_DAMAGED_FLATE_UNIT = 16384

# What an inflate is fed and drained at a time, and the widest predictor
# row pdfminer may hold for a stream narrower than that row.
_PIECE = 16384

# The most ASCII85 a stream may carry: decoding it takes about 90 bytes of
# memory for each, so 16 MiB of it peaked at 1,7 GiB. The largest a card
# carries is 2,2 kB.
_MAX_ASCII85_BYTES = 1024 * 1024

_FLATE = ("/FlateDecode", "/Fl")
_CCITT = ("/CCITTFaxDecode", "/CCF")
_ASCII85 = ("/ASCII85Decode", "/A85")


def _bound_refusal() -> ExtractorError:
    return ExtractorError(
        f"PDF streams decode to more than {_MAX_DECODED_STREAM_BYTES} bytes; "
        "refusing to read them"
    )


def _ascii85_refusal() -> ExtractorError:
    return ExtractorError(
        f"PDF carries an ASCII85 stream over {_MAX_ASCII85_BYTES} bytes; "
        "refusing to read it"
    )


def _filter_names(stream: StreamObject) -> list[str]:
    """The stream's filters, in the order they are undone."""
    filters = stream.get("/Filter")
    if filters is None:
        return []
    if isinstance(filters, ArrayObject):
        return [str(name.get_object()) for name in filters]
    return [str(filters)]


def _content_uses(reader: pypdf.PdfReader) -> Counter[int]:
    """How many of the pages pypdf walks draw each content stream, by object
    number. A tree pypdf cannot walk gives none, and pypdf then reads no
    text either; a page it cannot read is left out, as its text is."""
    uses: Counter[int] = Counter()
    try:
        pages = list(reader.pages)
    except LimitReachedError as err:
        raise _bound_refusal() from err
    except Exception:  # pypdf cannot walk the page tree
        return uses
    for page in pages:
        try:
            if "/Contents" not in page:
                continue
            contents = page.raw_get("/Contents")
            if isinstance(contents, IndirectObject) and isinstance(
                contents.get_object(), ArrayObject
            ):
                contents = contents.get_object()
        except LimitReachedError as err:
            raise _bound_refusal() from err
        except Exception:  # a page pypdf cannot read
            continue
        refs = contents if isinstance(contents, ArrayObject) else [contents]
        for ref in refs:
            if isinstance(ref, IndirectObject):
                uses[ref.idnum] += 1
    return uses


def _inflate(data: bytes, room: int) -> tuple[bytes, bool]:
    """``data`` inflated, refused past ``room`` bytes, and whether zlib read
    it to its end, checksum included, the way a one-shot ``zlib.decompress``
    has to. Fed and drained a :data:`_PIECE` at a time, so no more than
    ``room`` is ever held, and a stream zlib refuses keeps all it inflated to
    but the last piece: inflated in one call, one failing its checksum came
    back empty, and the weight of a damaged stream with it."""
    inflater = zlib.decompressobj()
    out = bytearray()
    try:
        for start in range(0, len(data), _PIECE):
            pending = data[start : start + _PIECE]
            while pending and not inflater.eof:
                out += inflater.decompress(pending, _PIECE)
                pending = inflater.unconsumed_tail
                if len(out) > room:
                    raise _bound_refusal()
            if inflater.eof:
                break
        out += inflater.flush()
    except zlib.error:
        return bytes(out), False
    if len(out) > room:
        raise _bound_refusal()
    return bytes(out), inflater.eof


def _inflates_whole(data: bytes) -> bool:
    """Whether zlib reads ``data`` to its end, checksum included."""
    return _inflate(data, _MAX_DECODED_STREAM_BYTES)[1]


def _flate_damaged(stream: StreamObject, filters: list[str]) -> bool:
    """Whether pdfminer would find a FlateDecode step of ``stream`` damaged.

    The step is checked on what reaches it, so the ASCII85 and ASCIIHex
    wrappers some cards put around it are undone first. A chain this cannot
    follow to its FlateDecode step counts as damaged.
    """
    data = stream._data
    for index, name in enumerate(filters):
        if name in _FLATE:
            later = filters[index + 1 :]
            return not _inflates_whole(data) or any(f in _FLATE for f in later)
        if name in ("/ASCII85Decode", "/A85"):
            data = ASCII85Decode.decode(data)
        elif name in ("/ASCIIHexDecode", "/AHx"):
            data = ASCIIHexDecode.decode(data)
        else:
            return any(f in _FLATE for f in filters[index + 1 :])
    return False


def require_bounded_reader(reader: pypdf.PdfReader) -> None:
    """Refuse a PDF whose streams inflate past anything a card decodes to,
    ahead of pypdf reading its text.

    :func:`read_capped` bounds the download, not what it inflates to. pypdf
    decodes under limits of its own (from 6.0.0) but sets none on what a
    whole document decodes to, in a worker thread the fetch timeout cannot
    cancel: a 20 kB file inflating to 20 MB took 26 s on a Raspberry Pi. So
    every stream pypdf may decode is decoded, on the reader the text is then
    read from, and the total is held to :data:`_MAX_DECODED_STREAM_BYTES`,
    weighted by what the reader does with it:

    - a content stream counts once for each page that draws it, since each
      page interprets it again: a 2,7 kB file whose one stream served 16
      pages kept pdfplumber busy for 100 s;
    - an image is left out, since no text is read from one, but a stream a
      page draws as its content counts whatever its ``/Subtype`` says;
    - a damaged FlateDecode stream counts again, many times over
      (:data:`_DAMAGED_FLATE_UNIT`).

    A CCITT fax stream outside an image is refused outright, as no card
    carries one, and so is an ASCII85 stream over
    :data:`_MAX_ASCII85_BYTES` or behind another filter. Only these refuse,
    or pypdf reaching one of its own limits. A stream pypdf cannot decode is
    skipped, so this never refuses a card for any other reason, and it only
    reads.
    """
    uses = _content_uses(reader)
    total = 0
    for generation, ids in reader.xref.items():
        for idnum in ids:
            try:
                obj = reader.get_object(IndirectObject(idnum, generation, reader))
                if not isinstance(obj, StreamObject):
                    continue
                drawn = uses[idnum]
                if not drawn and obj.get("/Subtype") == "/Image":
                    continue
                filters = _filter_names(obj)
                if any(name in _CCITT for name in filters):
                    raise ExtractorError(
                        "PDF carries a CCITT fax stream outside an image; "
                        "refusing to read it"
                    )
                if any(name in _ASCII85 for name in filters) and (
                    filters[0] not in _ASCII85 or len(obj._data) > _MAX_ASCII85_BYTES
                ):
                    raise _ascii85_refusal()
                size = len(obj.get_data())
                cost = size * max(drawn, 1)
                if any(name in _FLATE for name in filters) and _flate_damaged(
                    obj, filters
                ):
                    cost += (
                        size
                        * max(len(obj._data), _DAMAGED_FLATE_UNIT)
                        // _DAMAGED_FLATE_UNIT
                    )
            except ExtractorError:
                raise
            except LimitReachedError as err:
                raise _bound_refusal() from err
            except Exception:  # an object or filter pypdf cannot read
                continue
            total += cost
            if total > _MAX_DECODED_STREAM_BYTES:
                raise _bound_refusal()


@dataclass
class _Reading:
    """What is left of the bound while pdfplumber reads a card, the
    refusal that ended it, which pdfminer or pdfplumber may wrap in an
    error of their own, and for each stream being decoded, innermost last,
    what its last inflate handed back, counted already."""

    left: int
    refusal: ExtractorError | None = None
    inflated: list[int] = field(default_factory=list)

    def refuse(self, err: ExtractorError) -> ExtractorError:
        if self.refusal is None:
            self.refusal = err
        return err

    def charge(self, cost: int) -> None:
        self.left -= cost
        if self.left < 0:
            raise self.refuse(_bound_refusal())


# The card being read in this thread, or None outside one: pdfminer is only
# held to the bound then.
_READING: ContextVar[_Reading | None] = ContextVar("_READING", default=None)

# pdfminer decrypts RC4 a byte at a time, appending each to what it holds:
# 64 kB took 0,27 s of CPU on a Raspberry Pi and 1 MiB 365 s. A decryption
# counts its length times itself over this many bytes, before it runs, as a
# damaged inflate does. Every encrypted card is AES.
_RC4_UNIT = 16384


class _BoundedZlib:
    """``zlib`` as pdfminer's stream decoding sees it: while a card is read,
    every inflate counts, each step of a stream filtered more than once, and
    is held to what is left of the bound; one zlib refuses, which pdfminer
    then inflates again a byte at a time, counts again for every
    :data:`_DAMAGED_FLATE_UNIT` of the stream."""

    error = zlib.error
    decompressobj = staticmethod(zlib.decompressobj)

    @staticmethod
    def decompress(data: bytes) -> bytes:
        reading = _READING.get()
        if reading is None:
            return zlib.decompress(data)
        try:
            out, whole = _inflate(data, reading.left)
        except ExtractorError as err:
            raise reading.refuse(err) from None
        cost = len(out)
        if not whole:
            cost += (
                len(out) * max(len(data), _DAMAGED_FLATE_UNIT) // _DAMAGED_FLATE_UNIT
            )
        if reading.inflated:
            reading.inflated[-1] = len(out)
        reading.charge(cost)
        if whole:
            return out
        raise zlib.error("stream cut short or failing its checksum")


def _refused_while_reading(name: str, decode: Any) -> Any:
    """``decode``, refused while a card is read: pdfminer runs it in pure
    Python with no limit, never on an image to read text, and no other
    stream of a card carries it."""

    def refused(*args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            raise reading.refuse(
                ExtractorError(
                    f"PDF carries a {name} stream outside an image; refusing to read it"
                )
            )
        return decode(*args, **kwargs)

    return refused


_HOLDING = threading.Lock()


def _hold_pdfminer_to_the_bound() -> None:
    """Have pdfminer held to the bound while a card is read, and left as it
    is otherwise. Every stream it reads goes through ``PDFStream.get_data``,
    and every stream it inflates through its module's ``zlib``."""
    from pdfminer import pdftypes
    from pdfminer.arcfour import Arcfour

    with _HOLDING:
        names = vars(pdftypes)
        if isinstance(names["zlib"], _BoundedZlib):
            return
        get_data = pdftypes.PDFStream.get_data

        def measured(self: pdftypes.PDFStream) -> bytes:
            reading = _READING.get()
            if reading is None:
                return get_data(self)
            reading.inflated.append(0)
            try:
                data = get_data(self)
            finally:
                inflated = reading.inflated.pop()
            # A read counted its inflates as they ran, and counts what it
            # hands back past the last of them: all of it for a stream
            # decoded already.
            reading.charge(max(len(data) - inflated, 0))
            return data

        decrypt = Arcfour.decrypt

        def charged(self: Arcfour, data: bytes) -> bytes:
            reading = _READING.get()
            if reading is not None:
                reading.charge(len(data) * len(data) // _RC4_UNIT)
            return decrypt(self, data)

        png_predictor = names["apply_png_predictor"]

        def predicted(
            pred: int, colors: int, columns: int, bits: int, data: bytes
        ) -> bytes:
            # pdfminer holds a row of the width named before it reads any,
            # about 9 bytes for each column: 64 Mi columns took 581 MiB.
            reading = _READING.get()
            if reading is not None and columns > max(len(data), _PIECE):
                raise reading.refuse(
                    ExtractorError(
                        "PDF names a predictor row wider than its stream; "
                        "refusing to read it"
                    )
                )
            return bytes(png_predictor(pred, colors, columns, bits, data))

        ascii85decode = names["ascii85decode"]

        def held(data: bytes) -> bytes:
            reading = _READING.get()
            if reading is not None and len(data) > _MAX_ASCII85_BYTES:
                raise reading.refuse(_ascii85_refusal())
            return bytes(ascii85decode(data))

        for name, label in (
            ("lzwdecode", "LZW"),
            ("rldecode", "run-length"),
            ("ccittfaxdecode", "CCITT fax"),
        ):
            names[name] = _refused_while_reading(label, names[name])
        names["ascii85decode"] = held
        names["apply_png_predictor"] = predicted
        pdftypes.PDFStream.get_data = measured  # type: ignore[method-assign]
        Arcfour.decrypt = charged
        names["zlib"] = _BoundedZlib()


@contextmanager
def bounded_pdfminer() -> Iterator[None]:
    """Hold pdfminer, under pdfplumber, to :data:`_MAX_DECODED_STREAM_BYTES`
    while the card is read inside this block.

    Counted on what pdfminer reads as it reads it, not on a reading of the
    file made beforehand: pypdf's let the two readers find different pages
    and objects (a page tree only one of them walks, a reference by another
    generation, two cross-reference tables, a table entry one skips), and a
    copy of pdfminer's reading missed what pdfminer itself does (a font
    file tagged as an image, a filter after DCTDecode, a predictor between
    two inflates); each let pdfminer decode 40 MiB and more uncounted.
    Every time pdfminer reads a stream counts, so a content stream counts
    once for each page drawing it, a form once for each time it is drawn,
    and a font written into a page's resources once for each page loading
    it, since pdfminer interprets each again; each inflate counts, every
    step of a stream inflated more than once, and is refused past what is
    left, before it is held, the cross-reference and object streams pdfminer
    inflates opening the file included; and a stream counts what its
    decoding hands back past its last inflate. A refusal pdfminer or
    pdfplumber wrap in an error of their own is refused all the same.
    """
    _hold_pdfminer_to_the_bound()
    reading = _Reading(_MAX_DECODED_STREAM_BYTES)
    token = _READING.set(reading)
    try:
        yield
    except Exception as err:
        if reading.refusal is not None and err is not reading.refusal:
            raise reading.refusal from err
        raise
    finally:
        _READING.reset(token)
    if reading.refusal is not None:
        raise reading.refusal
