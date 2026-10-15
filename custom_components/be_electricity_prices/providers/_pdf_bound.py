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

"""What a card's PDF streams may decode to, held to while pypdf, or pdfminer
under pdfplumber, reads it."""

from __future__ import annotations

import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from pypdf import filters as pypdf_filters
from pypdf._font import Font
from pypdf.errors import LimitReachedError
from pypdf.generic import DecodedStreamObject, EncodedStreamObject, StreamObject

from .base import ExtractorError

# What every stream of a card may decode to, together, counted as each
# reader reads it. Across the 179 shipped fixtures pdfminer counts at most
# 9,9 MB, for a 2,4 MB Bolt card, and pypdf 7,4 MB. Across the 2468 cards
# the archive held on 2026-10-14 the most counted is 16,6 MB, pdfminer
# reading Bolt's professional Plenty fixed card of June 2026 with its
# damaged stream, so this leaves twice that.
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

# How a gzip stream starts, which pypdf inflates only a byte at a time.
_GZIP = b"\x1f\x8b"

# The most ASCII85 a stream may carry: decoding it takes about 90 bytes of
# memory for each, so 16 MiB of it peaked at 1,7 GiB. The largest a card
# carries is 2,2 kB.
_MAX_ASCII85_BYTES = 1024 * 1024

# What a font pypdf loads counts. pypdf loads every font a page or form
# names each time it is drawn, reading its widths and encoding again, which
# no stream holds: a 3 kB file naming one font 200 times in a form drawn
# 200 times took 10,8 s. A load costs about what pypdf spends on a few
# hundred bytes of content, and the most any fixture loads is 321, an OCTA+
# card of four pages and six forms.
_FONT_LOAD_BYTES = 1024


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


def _inflate(
    data: bytes, room: int, wbits: int = zlib.MAX_WBITS
) -> tuple[bytes, bool, bool]:
    """``data`` inflated, refused past ``room`` bytes, whether zlib read it
    to its end, checksum included, the way a one-shot ``zlib.decompress``
    has to, and whether zlib failed on it rather than found it cut short.
    Fed and drained a :data:`_PIECE` at a time, so no more than ``room`` is
    ever held, and a stream zlib refuses keeps all it inflated to but the
    last piece: inflated in one call, one failing its checksum came back
    empty, and the weight of a damaged stream with it."""
    inflater = zlib.decompressobj(wbits)
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
        return bytes(out), False, True
    if len(out) > room:
        raise _bound_refusal()
    return bytes(out), inflater.eof, False


def _damaged(again: int, length: int) -> int:
    """What a damaged FlateDecode stream ``length`` bytes long counts on top
    of what it inflated to: ``again`` once more for every
    :data:`_DAMAGED_FLATE_UNIT` of its length, for a reader inflating it
    again a byte at a time."""
    return again * max(length, _DAMAGED_FLATE_UNIT) // _DAMAGED_FLATE_UNIT


@dataclass
class _Reading:
    """What is left of the bound while a card is read, the refusal that
    ended it, which a reader may wrap in an error of its own or swallow,
    for each stream being decoded, innermost last, what its last inflate
    handed back, counted already, and under pypdf which stream that is and
    the fonts loaded so far, by the dictionary they were loaded from."""

    left: int
    refusal: ExtractorError | None = None
    inflated: list[int] = field(default_factory=list)
    streams: list[EncodedStreamObject] = field(default_factory=list)
    fonts: dict[int, tuple[Any, Font]] = field(default_factory=dict)

    def refuse(self, err: ExtractorError) -> ExtractorError:
        if self.refusal is None:
            self.refusal = err
        return err

    def charge(self, cost: int) -> None:
        self.left -= cost
        if self.left < 0:
            raise self.refuse(_bound_refusal())

    def inflate(
        self, data: bytes, wbits: int = zlib.MAX_WBITS
    ) -> tuple[bytes, bool, bool]:
        """``data`` inflated within what is left of the bound and counted,
        whether zlib read it whole, and whether it failed on it."""
        try:
            out, whole, failed = _inflate(data, self.left, wbits)
        except ExtractorError as err:
            raise self.refuse(err) from None
        if self.inflated:
            self.inflated[-1] = len(out)
        self.charge(len(out))
        return out, whole, failed


# The card being read in this thread, or None outside one: the readers are
# only held to the bound then.
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
        out, whole, _ = reading.inflate(data)
        if whole:
            return out
        reading.charge(_damaged(len(out), len(data)))
        raise zlib.error("stream cut short or failing its checksum")


def _refused_while_reading(name: str, decode: Any) -> Any:
    """``decode``, refused while a card is read: the readers never decode it
    on an image to read text, pdfminer runs it in pure Python with no limit,
    and no other stream of a card carries it."""

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


def _ascii85_held(decode: Any) -> Any:
    """``decode``, refused while a card is read on more than
    :data:`_MAX_ASCII85_BYTES` of ASCII85, whatever filter made them."""

    def held(data: bytes, *args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None and len(data) > _MAX_ASCII85_BYTES:
            raise reading.refuse(_ascii85_refusal())
        return decode(data, *args, **kwargs)

    return held


def _hold_pypdf_to_the_bound() -> None:
    """Have pypdf held to the bound while a card is read, and left as it is
    otherwise. Every stream it reads goes through ``get_data``, every one it
    inflates through ``pypdf.filters.decompress``, and every font it loads
    through ``Font.from_font_resource``."""
    decompress = pypdf_filters.decompress

    def inflate(data: bytes) -> bytes:
        reading = _READING.get()
        if reading is None:
            return decompress(data)
        # Either header, as pypdf's recovery reads a gzip one too.
        out, whole, failed = reading.inflate(data, zlib.MAX_WBITS | 32)
        if whole:
            return out
        # pypdf hands back what a stream cut short inflates to, and goes on
        # past a failure, or a gzip header, a byte at a time to the end of
        # the stream, so its length counts too. Either way it hands back no
        # more than the inflate counted above and the piece zlib dropped
        # refusing it.
        if failed or data.startswith(_GZIP):
            reading.charge(_damaged(len(out) + len(data), len(data)))
        recovered = bytes(decompress(data))
        reading.charge(max(len(recovered) - len(out), 0))
        if reading.inflated:
            reading.inflated[-1] = len(recovered)
        return recovered

    get_encoded = EncodedStreamObject.get_data
    get_decoded = DecodedStreamObject.get_data

    def read_encoded(self: EncodedStreamObject) -> bytes:
        reading = _READING.get()
        if reading is None:
            return get_encoded(self)
        reading.inflated.append(0)
        reading.streams.append(self)
        try:
            data = get_encoded(self)
        finally:
            inflated = reading.inflated.pop()
            reading.streams.pop()
        # A read counted its inflates as they ran, and counts what it hands
        # back past the last of them: all of it for a stream decoded
        # already.
        reading.charge(max(len(data) - inflated, 0))
        return data

    def read_decoded(self: StreamObject) -> bytes:
        data = get_decoded(self)
        reading = _READING.get()
        # Every read counts but the one a stream makes of its own decoding,
        # counted with it.
        if reading is not None and not (
            reading.streams and reading.streams[-1].decoded_self is self
        ):
            reading.charge(len(data))
        return data

    load = Font.from_font_resource.__func__  # type: ignore[attr-defined]

    def loaded(cls: type[Font], pdf_font_dict: Any) -> Font:
        reading = _READING.get()
        if reading is None:
            return load(cls, pdf_font_dict)  # type: ignore[no-any-return]
        reading.charge(_FONT_LOAD_BYTES)
        # Loaded once a read: pypdf reads the same dictionary to the same
        # font each time.
        key = id(pdf_font_dict)
        if key not in reading.fonts:
            reading.fonts[key] = (pdf_font_dict, load(cls, pdf_font_dict))
        return reading.fonts[key][1]

    for decoder, label in (
        (pypdf_filters.LZWDecode, "LZW"),
        (pypdf_filters.RunLengthDecode, "run-length"),
        (pypdf_filters.CCITTFaxDecode, "CCITT fax"),
        (pypdf_filters.JBIG2Decode, "JBIG2"),
    ):
        decoder.decode = staticmethod(  # type: ignore[method-assign]
            _refused_while_reading(label, decoder.decode)
        )
    pypdf_filters.ASCII85Decode.decode = staticmethod(  # type: ignore[method-assign]
        _ascii85_held(pypdf_filters.ASCII85Decode.decode)
    )
    pypdf_filters.decompress = inflate
    EncodedStreamObject.get_data = read_encoded  # type: ignore[method-assign]
    DecodedStreamObject.get_data = read_decoded  # type: ignore[method-assign]
    Font.from_font_resource = classmethod(loaded)  # type: ignore[assignment]


_hold_pypdf_to_the_bound()

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

        for name, label in (
            ("lzwdecode", "LZW"),
            ("rldecode", "run-length"),
            ("ccittfaxdecode", "CCITT fax"),
        ):
            names[name] = _refused_while_reading(label, names[name])
        names["ascii85decode"] = _ascii85_held(names["ascii85decode"])
        names["apply_png_predictor"] = predicted
        pdftypes.PDFStream.get_data = measured  # type: ignore[method-assign]
        Arcfour.decrypt = charged
        names["zlib"] = _BoundedZlib()


@contextmanager
def _held_to_the_bound() -> Iterator[None]:
    """Hold the readers to :data:`_MAX_DECODED_STREAM_BYTES` while the card
    is read inside this block, and raise the refusal that ended the read
    whatever a reader made of it. pypdf reaching one of its own limits is
    refused too."""
    reading = _Reading(_MAX_DECODED_STREAM_BYTES)
    token = _READING.set(reading)
    try:
        yield
    except Exception as err:
        if reading.refusal is not None and err is not reading.refusal:
            raise reading.refusal from err
        if isinstance(err, LimitReachedError):
            raise _bound_refusal() from err
        raise
    finally:
        _READING.reset(token)
    if reading.refusal is not None:
        raise reading.refusal


@contextmanager
def bounded_pypdf() -> Iterator[None]:
    """Hold pypdf to :data:`_MAX_DECODED_STREAM_BYTES` while the card is
    opened and read inside this block.

    pypdf limits each stream (from 6.0.0) but not what a whole document
    decodes to, in a worker thread the fetch timeout cannot cancel: a 20 kB
    file inflating to 20 MB took 26 s on a Raspberry Pi. Counted on what
    pypdf reads as it reads it, not on a reading of the file made
    beforehand, which missed what pypdf does again: a page's content
    written into the page counted nothing (a 175 kB file decoded 60 MB), a
    form counted once and was interpreted on each of its draws (a 934 B
    file took 77 s), and a ToUnicode map pages share counted once and was
    parsed on each page. Every read of a stream counts, so a page's content
    counts for each page, a form for each draw and a font's ToUnicode map
    or font file each time a font is loaded from it; each inflate counts,
    every step of a stream inflated more than once, and is refused past
    what is left, before it is held, the cross-reference and object streams
    pypdf inflates opening the file included; a damaged FlateDecode
    stream, which pypdf recovers a byte at a time to its end, counts again
    as pdfminer's does, its length too; a stream counts what its decoding
    hands back past its last inflate; and a font load counts
    :data:`_FONT_LOAD_BYTES`, pypdf loading the same dictionary to the same
    font once a read. LZW, run length, CCITT fax and JBIG2 are refused,
    since pypdf never decodes an image to read text, and ASCII85 past
    :data:`_MAX_ASCII85_BYTES`. A refusal pypdf swallows, as it does any
    error in a form, is refused all the same.
    """
    with _held_to_the_bound():
        yield


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
    with _held_to_the_bound():
        yield
