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

"""What a card's PDF streams may decode to, how many pages it may have,
what a reader may parse of it, build of its fonts and make of its text,
how many steps pdfminer may take, how long a token or line it may gather
and what it may lay out and hold of it, and how many lines pypdf may read
backwards, held to while pypdf, or pdfminer under pdfplumber, reads it.
What pdfminer is held to on top of this, and the wrappers holding it, are
in :mod:`._pdf_bound_pdfminer`."""

from __future__ import annotations

import importlib
import inspect
import sys
import threading
import zlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, SupportsIndex

from pypdf import PageObject
from pypdf import _cmap as pypdf_cmap
from pypdf import _reader as pypdf_reader
from pypdf import filters as pypdf_filters
from pypdf.errors import LimitReachedError
from pypdf.generic import DecodedStreamObject, EncodedStreamObject, StreamObject
from pypdf.generic import _data_structures as pypdf_structures

from .base import ExtractorError

# pypdf's font module, in its generic package from 6.20.0 and beside its
# CMaps before.
try:
    pypdf_font: Any = importlib.import_module("pypdf.generic._font")
except ModuleNotFoundError:
    pypdf_font = importlib.import_module("pypdf._font")
# Where pypdf's text extraction keeps the state of a page it reads.
pypdf_extractor: Any = importlib.import_module("pypdf._text_extraction._text_extractor")

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

# The most pages a card may have, and the most layout objects, glyphs,
# forms and images, pdfminer may build for one page and for the whole card.
# A page costs pdfplumber about 15 ms with nothing on it, and pypdf 1,4 ms,
# so a 2 MB file of 20 000 empty pages took 302 s; a glyph holds about 3
# kB under pdfplumber, so a 6,5 kB card drawing 488 000 on a page held 1,1
# GB. Measured on 2026-10-15 as pdfplumber reads them, across the 179
# shipped fixtures and the 2488 cards the archive held: at most 5 pages,
# 11 213 objects on a page, Luminus' SmartFlex card for Wallonia of October
# 2026, and 17 374 on a card, Cociter's dynamic card of October 2026.
# These are about ten times that: a page of 119 000 glyphs holds 437 MB and
# takes 56 s on a Raspberry Pi.
_MAX_PAGES = 50
_MAX_PAGE_OBJECTS = 120_000
_MAX_CARD_OBJECTS = 180_000

# The most a reader may parse of a card, counted as it parses, each time it
# does: under pdfminer every token but a comment of the file, of its content
# streams for each page drawing them and of a form each time it is drawn, of
# a font's maps and programs, and each font, colour space, form or image and
# procedure set of a page's or form's resources it walks for each draw, the
# kinds it reads of them, each reference it resolves and each line it reads
# backwards from the end of the file; under pypdf every operand and operator
# of a content stream each time it is interpreted, and every element of an
# array or dictionary. What the bytes draw cost even when it built nothing:
# 78 kB inflating to 32 MB of numbers no operator took held pypdf 238 s and
# 1,2 GB, and 3 MB of paths pdfminer does not paint took 45 s, while a
# 700-byte card naming an object that refers to itself had pdfminer resolve
# it for good. Measured as above: at most 935 796 under pdfminer and 912 313
# under pypdf, both on a Bolt card of 2,8 MB. This is about three times
# that, since what it lets through is CPU: a token of a Bolt card costs
# pdfminer about 40 us on a Raspberry Pi, and of 30 kinds of operator made
# up to cost more none costs over a twentieth more but one showing a glyph
# for every four tokens, half as much again, which the object bound holds to
# 180 000 glyphs, so 3 million take about 140 s. A comment counts nothing:
# pdfminer passes over one in a time linear in its length, the decode bound
# and the download cap holding how many bytes it passes over, and
# _MAX_TOKEN_BYTES how long one may be.
_MAX_PARSED = 3_000_000

# The most a reader may build of a card's fonts, counted as it builds them:
# each code a range of a font's ToUnicode map, encoding or widths gives, or
# its program's own tables, counted before the range is gone through, and
# under pypdf each code of a ToUnicode map once it is built. A code listed
# on its own is a token parsed, and a reader builds a font from a dictionary
# once a read. A range takes a line: 2,8 kB mapping 2^32 codes ran pdfminer
# out of memory after 52 s, and pypdf, which maps at most 100 000 codes for
# a font from 6.7.1, held 970 MB for 48 fonts of that many. Under pdfminer
# an entry of a map counts once more for each character past its first it
# maps to, since a range gives every code of it the characters its line
# names, up to 4 000: a 1 kB card holding one font of 65 536 codes so
# mapped held 500 MB, where the codes alone let 15 such fonts through.
# pypdf gives a code of a range at most 8. Measured as above: at most
# 196 659 under pdfminer and 198 676 under pypdf, Energy Knights' cards
# mapping three fonts of 65 536 codes each to a character apiece, and
# measured again on 2026-11-06 across the 243 shipped fixtures, at most 163
# characters past the first, on a Bolt card. This is about five times that:
# pypdf holds about 180 bytes a code.
_MAX_FONT_ENTRIES = 1_000_000

# The most characters of text a reader may make of a card, counted as they
# are made: each glyph's as pdfminer lays it out, and under pypdf each a
# font's map gives a character of a code shown, each an encoding gives a
# code past its first, and each space or line end pypdf puts between them.
# A glyph shows the characters its font maps its code to, which no other
# bound counts: a ToUnicode map gives a code up to 4 000 under pdfminer and
# 256 under pypdf, and an encoding gives it a glyph name as long as the card
# writes it, so a 4,8 kB card had pypdf make 1 024 000 000 characters and
# hold 1,1 GB, and a 1 kB card had pdfplumber lay out 32 000 000 and hold
# 2,7 GB. pypdf also copies all a page holds each time it ends a run of
# text, so in a time quadratic in the characters, and puts in, uncounted,
# a space for a wide gap between the strings of an array it shows, so a
# page holds up to twice what is counted: 99 000 runs of a glyph and such
# a space each, the most this lets through, took 65 s on a busy Raspberry
# Pi. Measured on 2026-11-06 across the 243 shipped fixtures:
# at most 17 037 under pdfminer and 17 321 under pypdf, both on Cociter's
# dynamic card of April 2026, while the longest text the archive held that
# day is the same card's of November 2026, 18 197 characters. This is
# about five times that.
_MAX_CHARACTERS = 100_000

# The longest token pdfminer may gather, a comment included, the longest
# line it may read forwards and the most data of an inline image: it grows
# each a byte or a buffer at a time, copying what it holds each time, so in
# a time quadratic in its length, while the parse count sees each as one
# object at most. 1 MB of inline image data took 352 s on a Raspberry Pi,
# from a file under 2 kB, and a stream saying it is empty carrying 16 MB
# with no line end 47 s. Measured as above: at most 1815 bytes, on a
# TotalEnergies card, lines of at most 140 bytes, and no card carries an
# inline image. This is about nine times that: 8 MB of strings this long
# took 29 s on a Raspberry Pi, where the same bytes in strings of 1 kB took
# 22 s.
_MAX_TOKEN_BYTES = 16 * 1024

# The most steps pdfminer's parsers may take reading a card, a step each
# time one looks at its buffer again. Most make or end a token, but a NUL
# byte, a lone ">", each escape in a string or a name and each line a parser
# reads take one each, which the parse count does not see: 2 MB of NUL bytes
# took 7,5 s on a Raspberry Pi, in the file itself held only by the download
# cap. Measured as above: at most 2 612 279, on a Bolt card of 2,8 MB. This
# is about three times that: a step costs at most about 8,5 us, a line of a
# stream read to its end, so the most it lets through take about 68 s.
_MAX_STEPS = 8_000_000

# The most lines pypdf may read backwards from the end of a file, looking
# for its end marker and the pointer to its table. It reads a block of 8 kB
# for each, so a file of short lines with no end marker took about 9 s a
# MB on a Raspberry Pi, with nothing counting it. Measured as above: 4 at
# most. pypdf itself reads up to 1000 more recovering a broken pointer past
# up to 1000 end markers repeated, which this lets through: 4096 lines take
# about 70 ms.
_MAX_LINES_BACK = 4096


def _over_the_bound() -> str:
    return (
        f"PDF streams decode to more than {_MAX_DECODED_STREAM_BYTES} bytes; "
        "refusing to read them"
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
                    raise ExtractorError(_over_the_bound())
            if inflater.eof:
                break
        out += inflater.flush()
    except zlib.error:
        return bytes(out), False, True
    if len(out) > room:
        raise ExtractorError(_over_the_bound())
    return bytes(out), inflater.eof, False


def _damaged(again: int, length: int) -> int:
    """What a damaged FlateDecode stream ``length`` bytes long counts on top
    of what it inflated to: ``again`` once more for every
    :data:`_DAMAGED_FLATE_UNIT` of its length, for a reader inflating it
    again a byte at a time."""
    return again * max(length, _DAMAGED_FLATE_UNIT) // _DAMAGED_FLATE_UNIT


class _Refused(BaseException):
    """A refusal on its way out of the readers, which catch any Exception
    in places: pypdf goes on to a form's next draw past one, logging it
    each time (99 984 lines for one file), and pdfplumber past one in a
    file's metadata. :func:`_held_to_the_bound` turns it back into the
    refusal, and nothing raises it outside one."""


@dataclass
class _Reading:
    """What is left of the bound while a card is read, the refusal that
    ended it, for each stream being decoded, innermost last, what its last
    inflate handed back, counted already, under pypdf which stream that is,
    the fonts built so far, by the dictionary they were built from, the
    CMaps of its own pdfminer loaded, by name, each entry the read added
    to pdfminer's caches of them, by cache and key, with what it holds,
    and the names and keywords pdfminer made for the read, by the kind
    it made and the name.

    Every refusal is made here, so it is an error of the copy of this
    module the read was started from, whichever copy put the wrappers in
    place."""

    left: int
    refusal: ExtractorError | None = None
    pages: int = 0
    built: int = 0
    parsed: int = 0
    steps: int = 0
    font_entries: int = 0
    characters: int = 0
    lines_back: int = 0
    run: int | None = None
    inflated: list[int] = field(default_factory=list)
    streams: list[EncodedStreamObject] = field(default_factory=list)
    fonts: dict[int, tuple[Any, Any]] = field(default_factory=dict)
    cmaps: set[str] = field(default_factory=set)
    cached: list[tuple[dict[str, Any], str, Any]] = field(default_factory=list)
    symbols: dict[tuple[Any, Any], Any] = field(default_factory=dict)

    def refuse(self, message: str) -> _Refused:
        if self.refusal is None:
            self.refusal = ExtractorError(message)
        return _Refused()

    def charge(self, cost: int) -> None:
        self.left -= cost
        if self.left < 0:
            raise self.refuse(_over_the_bound())

    def list_page(self, count: int) -> None:
        """Refuse a reader listing its ``count``-th page, past
        :data:`_MAX_PAGES`."""
        if count > _MAX_PAGES:
            raise self.refuse(
                f"PDF has more than {_MAX_PAGES} pages; refusing to read it"
            )

    def build(self, on_page: int) -> None:
        """Count a layout object built, the page's ``on_page``-th."""
        self.built += 1
        if on_page > _MAX_PAGE_OBJECTS or self.built > _MAX_CARD_OBJECTS:
            raise self.refuse(
                f"PDF lays out more than {_MAX_PAGE_OBJECTS} objects on a "
                f"page or {_MAX_CARD_OBJECTS} in all; refusing to read it"
            )

    def parse(self, count: int = 1) -> None:
        """Count ``count`` objects parsed, or resources walked."""
        self.parsed += count
        if self.parsed > _MAX_PARSED:
            raise self.refuse(
                f"PDF has a reader parse more than {_MAX_PARSED} objects; "
                "refusing to read it"
            )

    def step(self, parser: Any) -> None:
        """Refuse the card once pdfminer's parsers have taken more than
        :data:`_MAX_STEPS` steps, or once the token, line or inline image
        ``parser`` gathers is past :data:`_MAX_TOKEN_BYTES`, checked as it
        has read through its buffer: a line or an inline image is as long as
        what it read through gathering it."""
        if self.steps > _MAX_STEPS:
            raise self.refuse(
                f"PDF has pdfminer take more than {_MAX_STEPS} steps; "
                "refusing to read it"
            )
        if self.run is None:
            length = len(parser._curtoken)
        else:
            self.run = length = self.run + len(parser.buf)
        if length > _MAX_TOKEN_BYTES:
            raise self.refuse(
                f"PDF has pdfminer gather a token, line or inline image over "
                f"{_MAX_TOKEN_BYTES} bytes; refusing to read it"
            )

    def font(self, spec: Any, build: Callable[[], Any]) -> Any:
        """The font ``build`` makes of the dictionary ``spec``, made once a
        read: a reader makes the same dictionary into the same font each
        time."""
        key = id(spec)
        if key not in self.fonts:
            self.fonts[key] = (spec, build())
        return self.fonts[key][1]

    def read_back(self) -> None:
        """Count a line pypdf reads backwards from the end of the file."""
        self.lines_back += 1
        if self.lines_back > _MAX_LINES_BACK:
            raise self.refuse(
                f"PDF has pypdf read more than {_MAX_LINES_BACK} lines backwards; "
                "refusing to read it"
            )

    def make_font(self, entries: int) -> None:
        """Count ``entries`` built for a font."""
        self.font_entries += entries
        if self.font_entries > _MAX_FONT_ENTRIES:
            raise self.refuse(
                f"PDF has a reader build more than {_MAX_FONT_ENTRIES} font "
                "entries; refusing to read it"
            )

    def show(self, characters: int) -> None:
        """Count ``characters`` of text made."""
        self.characters += characters
        if self.characters > _MAX_CHARACTERS:
            raise self.refuse(
                f"PDF has a reader make more than {_MAX_CHARACTERS} characters "
                "of text; refusing to read it"
            )

    def hold(self, count: int, most: int) -> None:
        """Refuse the card once pdfminer holds ``count`` of something, past
        ``most``."""
        if count > most:
            raise self.refuse(
                f"PDF has pdfminer hold more than {most} items at once; "
                "refusing to read it"
            )

    def inflate(
        self, data: bytes, wbits: int = zlib.MAX_WBITS
    ) -> tuple[bytes, bool, bool]:
        """``data`` inflated within what is left of the bound and counted,
        whether zlib read it whole, and whether it failed on it."""
        try:
            out, whole, failed = _inflate(data, self.left, wbits)
        except ExtractorError as err:
            raise self.refuse(str(err)) from None
        if self.inflated:
            self.inflated[-1] = len(out)
        self.charge(len(out))
        return out, whole, failed


# What the wrappers this module and _pdf_bound_pdfminer put on pypdf and on
# pdfminer carry: the card being read in this thread, or None outside one,
# the readers being held to the bound only then, and the lock pdfminer is
# wrapped under. A second copy of this module, as the live check loads
# under a package of its own, finds them there and reads through the same
# wrappers, rather than wrapping the readers again and having each read
# counted once for every copy. So does the gas integration's copy of it,
# with both integrations installed: a second set of wrappers would put its
# own in place of some of the first's, pdfminer's zlib, its tables of names
# and the font modules' range, and leave the other integration's reads
# held to none of them.
_MARK = "_be_electricity_prices_bound"
_READING: ContextVar[_Reading | None]
_HOLDING: threading.Lock
_READING, _HOLDING = getattr(EncodedStreamObject.get_data, _MARK, None) or (
    ContextVar[_Reading | None]("_READING", default=None),
    threading.Lock(),
)


def _refused_while_reading(name: str, decode: Any) -> Any:
    """``decode``, refused while a card is read: the readers never decode it
    on an image to read text, pdfminer runs it in pure Python with no limit,
    and no other stream of a card carries it."""

    def refused(*args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            raise reading.refuse(
                f"PDF carries a {name} stream outside an image; refusing to read it"
            )
        return decode(*args, **kwargs)

    return refused


def _ascii85_held(decode: Any) -> Any:
    """``decode``, refused while a card is read on more than
    :data:`_MAX_ASCII85_BYTES` of ASCII85, whatever filter made them."""

    def held(data: bytes, *args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None and len(data) > _MAX_ASCII85_BYTES:
            raise reading.refuse(
                f"PDF carries an ASCII85 stream over {_MAX_ASCII85_BYTES} bytes; "
                "refusing to read it"
            )
        return decode(data, *args, **kwargs)

    return held


def _parsed(parse: Any) -> Any:
    """``parse``, counted while a card is read as one object parsed."""

    def counted(*args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            reading.parse()
        return parse(*args, **kwargs)

    return counted


def _font_range(*args: SupportsIndex) -> range:
    """``range``, as the readers' font modules find it ahead of the builtin
    one, every loop of theirs building a font going through it: counted
    while a card is read, as that many entries built, before it is gone
    through."""
    span = range(*args)
    reading = _READING.get()
    if reading is not None:
        try:
            entries = len(span)
        except OverflowError:
            entries = sys.maxsize
        reading.make_font(entries)
    return span


def _stepped(fill: Any) -> Any:
    """``fill``, the method a pdfminer parser runs at each step, looking at
    its buffer again, counted while a card is read, and what the parser
    gathers checked first once it has read through its buffer."""

    def filled(self: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            reading.steps += 1
            if reading.steps > _MAX_STEPS or self.charpos >= len(self.buf):
                reading.step(self)
        return fill(self)

    return filled


class _Shown(dict[Any, Any]):
    """A font's encoding or character map as pypdf shows a card's codes
    through it: while a card is read, each value it hands out counts the
    characters it holds past the first ``free``, as it is handed out. pypdf
    looks each code of a string up in the encoding, then each character
    that gives up in the map, where it counts, so an encoding's value counts
    only the characters past its first. pypdf joins what a string's codes
    give only once it has looked up the whole string, so the text it joins
    could not be counted before it was held."""

    def __init__(self, mapping: dict[Any, Any], free: int) -> None:
        super().__init__(mapping)
        self._free = free

    def get(self, key: Any, default: Any = None, /) -> Any:
        return self._shown(super().get(key, default))

    def __getitem__(self, key: Any, /) -> Any:
        return self._shown(super().__getitem__(key))

    def _shown(self, value: Any) -> Any:
        reading = _READING.get()
        if reading is not None and isinstance(value, str) and len(value) > self._free:
            _show(reading, len(value) - self._free)
        return value


def _show(reading: _Reading, characters: int) -> None:
    """Count ``characters`` of text made on ``reading``, where its bound
    counts them: the copy of this module that started the read may come
    from a release of the other integration that counts none."""
    show = getattr(reading, "show", None)
    if show is not None:
        show(characters)


# What pypdf's check of a move is handed first and where it takes what the
# page holds, from pypdf 6.7.1 to 6.20.1 at least. The check is left as it
# is should a release name other arguments there, or have none.
_PAGE_ARGUMENT = 6
_CHECK_ARGUMENTS = {0: "text", _PAGE_ARGUMENT: "output"}


def _tail_only(check: Any) -> Any:
    """``check``, the function pypdf runs for each move or string shown to
    tell whether it starts a line or a word, handed while a card is read
    only the last character of what the page holds rather than all of it,
    and the space or line end it puts in counted as characters made. It
    reads no more than that character, and appends to the page a line it
    ends, but making the two one string to take that character copied the
    whole page each time: on a page holding 400 000 characters, 20 000
    moves that went nowhere took 6,4 s on a Raspberry Pi. What it appends is
    put back on the page, so the page reads as it did. A move to the right
    puts a space in and one down ends the line, with no glyph shown, so
    moves alone grew a page by a character each: 80 000 pairs of them took
    15 s."""

    def checked(*args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is None or len(args) <= _PAGE_ARGUMENT:
            return check(*args, **kwargs)
        page = args[_PAGE_ARGUMENT]
        tail = page[-1:]
        text, ended, *rest = check(
            *args[:_PAGE_ARGUMENT], tail, *args[_PAGE_ARGUMENT + 1 :], **kwargs
        )
        _show(reading, max(len(text) + len(ended) - len(tail) - len(args[0]), 0))
        return (text, page + ended[len(tail) :], *rest)

    return checked


def _count_pypdf_text() -> None:
    """Have pypdf count the characters of text it makes while a card is
    read, through the encoding and map of each ``Font`` it makes, and hand
    its text extractor's ``crlf_space_check`` only the last character of
    the page. Done once whatever the copy of this module, over the other
    wrappers too where a release of either integration from before put
    them in place without these."""
    font = pypdf_font.Font
    if hasattr(font.__init__, _MARK):
        return
    made = font.__init__

    def make(self: Any, *args: Any, **kwargs: Any) -> None:
        # Every font pypdf shows a code through, the one it stands in for
        # a font a page lacks included. Only an encoding giving a code
        # more than a character can make more of a string than its codes,
        # and counting each code through it cost a string of 4 MB 2,5 s
        # more on a Raspberry Pi.
        made(self, *args, **kwargs)
        if _READING.get() is not None:
            encoding = self.encoding
            if isinstance(encoding, dict) and any(
                isinstance(value, str) and len(value) > 1 for value in encoding.values()
            ):
                self.encoding = _Shown(encoding, 1)
            self.character_map = _Shown(self.character_map, 0)

    extractor = vars(pypdf_extractor)
    check = extractor.get("crlf_space_check")
    named = dict(enumerate(inspect.signature(check).parameters)) if check else {}
    if all(named.get(at) == name for at, name in _CHECK_ARGUMENTS.items()):
        extractor["crlf_space_check"] = _tail_only(check)
    setattr(make, _MARK, True)
    font.__init__ = make


def _hold_pypdf_to_the_bound() -> None:
    """Have pypdf held to the bound while a card is read, and left as it is
    otherwise. Every stream it reads goes through ``get_data``, every one it
    inflates through ``pypdf.filters.decompress``, every font it loads
    through ``Font.from_font_resource``, every page it lists through
    ``PageObject``, every operand, operator, and element of an array or
    dictionary it parses through its ``generic`` module's ``read_object``
    and ``read_until_regex``, every line it reads backwards from the end of
    the file through its reader module's ``read_previous_line``, every font
    map it builds through ``_parse_to_unicode`` and every loop building a
    font through its font modules' ``range``, and every character of text
    it makes as :func:`_count_pypdf_text` says. Done once whatever the copy
    of this module."""
    _count_pypdf_text()
    if hasattr(EncodedStreamObject.get_data, _MARK):
        return
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

    listed = PageObject.__init__

    def page(self: PageObject, *args: Any, **kwargs: Any) -> None:
        listed(self, *args, **kwargs)
        reading = _READING.get()
        if reading is not None:
            reading.pages += 1
            reading.list_page(reading.pages)

    font = pypdf_font.Font
    load = font.from_font_resource.__func__

    def loaded(cls: Any, pdf_font_dict: Any) -> Any:
        reading = _READING.get()
        if reading is None:
            return load(cls, pdf_font_dict)
        reading.charge(_FONT_LOAD_BYTES)
        return reading.font(pdf_font_dict, lambda: load(cls, pdf_font_dict))

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
    parse_map = pypdf_cmap._parse_to_unicode

    def mapped(*args: Any, **kwargs: Any) -> Any:
        # Counted once built, pypdf building no more than 100 000 codes
        # for a map.
        built = parse_map(*args, **kwargs)
        reading = _READING.get()
        if reading is not None:
            reading.make_font(len(built[1]))
        return built

    for module in (pypdf_cmap, pypdf_font):
        vars(module)["range"] = _font_range
    vars(pypdf_cmap)["_parse_to_unicode"] = mapped
    previous_line = vars(pypdf_reader)["read_previous_line"]

    def read_back(stream: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            reading.read_back()
        return previous_line(stream)

    vars(pypdf_reader)["read_previous_line"] = read_back
    structures = vars(pypdf_structures)
    for name in ("read_object", "read_until_regex"):
        structures[name] = _parsed(structures[name])
    pypdf_filters.decompress = inflate
    setattr(read_encoded, _MARK, (_READING, _HOLDING))
    EncodedStreamObject.get_data = read_encoded  # type: ignore[method-assign]
    DecodedStreamObject.get_data = read_decoded  # type: ignore[method-assign]
    font.from_font_resource = classmethod(loaded)
    PageObject.__init__ = page  # type: ignore[method-assign]


_hold_pypdf_to_the_bound()


@contextmanager
def _held_to_the_bound() -> Iterator[_Reading]:
    """Hold the readers to :data:`_MAX_DECODED_STREAM_BYTES` while the card
    is read inside this block, the read it yields, and raise the refusal
    that ended the read whatever a reader made of it. pypdf reaching one of
    its own limits is refused too, under pypdf's own words for it."""
    reading = _Reading(_MAX_DECODED_STREAM_BYTES)
    token = _READING.set(reading)
    try:
        yield reading
    except _Refused:
        pass
    except Exception as err:
        # One the read raised past a refusal, as it let go of what it held.
        if reading.refusal is not None:
            raise reading.refusal from err
        if isinstance(err, LimitReachedError):
            raise ExtractorError(
                f"PDF reaches a limit of pypdf ({err}); refusing to read it"
            ) from err
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
    :data:`_MAX_ASCII85_BYTES`. pypdf parses no more than
    :data:`_MAX_PARSED` operands, operators and elements of arrays and
    dictionaries, a content stream's each time it is interpreted, reads
    no more than :data:`_MAX_LINES_BACK` lines backwards from the end of
    the file, builds no more than :data:`_MAX_FONT_ENTRIES` entries of a
    card's fonts, and makes no more than :data:`_MAX_CHARACTERS`
    characters of text, each value a font's encoding or map hands it for a
    code shown counted as it is handed out, so a glyph its font maps to
    many characters counts each. Checking a move or a string shown for the
    line or word it starts, pypdf is handed only the last character of what
    the page holds, which is all it reads of it. A refusal ends the read
    where it is made: pypdf catches any Exception a form raises and goes on
    to its next draw, and a refusal is not one.
    """
    with _held_to_the_bound():
        yield
