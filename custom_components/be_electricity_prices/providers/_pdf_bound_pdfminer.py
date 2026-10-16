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

"""What pdfminer, under pdfplumber, is held to while a card is read, on
top of the bound :mod:`._pdf_bound` counts: the wrappers holding it to
that bound, and what it may hold at once laying a page out."""

from __future__ import annotations

import sys
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, SupportsIndex

from ._pdf_bound import (
    _HOLDING,
    _MARK,
    _PIECE,
    _READING,
    _ascii85_held,
    _damaged,
    _font_range,
    _held_to_the_bound,
    _parsed,
    _refused_while_reading,
    _stepped,
)

# The most pdfminer may hold at once laying a page out, besides what it
# builds: on any one stack, operands waiting for their operator, graphics
# states saved, or the pieces of an array or dictionary being read; arrays
# and dictionaries nested in one another; and segments of a path not yet
# painted. Each grew with the bytes drawing it: 2 MB of saved states held
# 552 MB, and 400 kB of operands left waiting, each operator then copying
# the rest, took 157 s. Measured on 2026-10-15 as pdfplumber reads them,
# across the 179 shipped fixtures and the 2488 cards the archive held: at
# most 407 on a stack, an OCTA+ card, nested 2 deep, and 42 987 segments on
# a page of a Bolt card. These are about ten times that.
_MAX_STACK = 5_000
_MAX_NESTING = 20
_MAX_PATH_SEGMENTS = 450_000

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


def _built(method: Any) -> Any:
    """``method`` of the device pdfplumber lays a page out on, counted while
    a card is read as one layout object built for that page."""

    def build(self: Any, *args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is not None:
            self._bound_built = vars(self).get("_bound_built", 0) + 1
            reading.build(self._bound_built)
        return method(self, *args, **kwargs)

    return build


class _Positions(list[Any]):
    """A page's glyphs, each found by ``index`` at once: pdfplumber puts the
    glyphs ``dedupe_chars`` keeps back in order by looking each one up in
    the list, which took 303 s for 40 000 glyphs on a page. It keeps the
    first of glyphs alike in every way, so the first found is the one."""

    def __init__(self, chars: list[Any]) -> None:
        super().__init__(chars)
        self._at: dict[int, int] = {}
        for at, char in enumerate(chars):
            self._at.setdefault(id(char), at)

    def index(
        self,
        value: Any,
        start: SupportsIndex = 0,
        stop: SupportsIndex = sys.maxsize,
        /,
    ) -> int:
        at = self._at.get(id(value))
        if at is None or start != 0 or stop != sys.maxsize:
            return super().index(value, start, stop)
        return at


def _run(gather: Any) -> Any:
    """``gather``, a method by which a pdfminer parser gathers a line or an
    inline image a buffer at a time, measured while a card is read by the
    buffers it reads through, no other parser reading meanwhile."""

    def gathered(self: Any, *args: Any, **kwargs: Any) -> Any:
        reading = _READING.get()
        if reading is None:
            return gather(self, *args, **kwargs)
        reading.run = 0
        try:
            return gather(self, *args, **kwargs)
        finally:
            reading.run = None

    return gathered


def _held(count: int, most: int) -> None:
    """Refuse the card being read once pdfminer holds ``count`` of
    something, past ``most``."""
    reading = _READING.get()
    if reading is not None:
        reading.hold(count, most)


def _hold_pdfminer_to_the_bound() -> None:
    """Have pdfminer held to the bound while a card is read, and left as it
    is otherwise. Every stream it reads goes through ``PDFStream.get_data``,
    every stream it inflates through its module's ``zlib``, every page it
    lists through ``PDFPage.create_pages``, everything it lays out through
    the device pdfplumber gives it, what it holds through its interpreter's
    and content parser's ``push``, every token but a comment it parses
    through ``PSBaseParser.nexttoken``, every reference it resolves through
    ``PDFObjRef.resolve``, every step its parsers take, and the token, line
    or inline image they gather, through their ``fillbuf``, ``nextline``
    and a content parser's ``get_inline_data``, every line it reads
    backwards through ``revreadlines``, the resources it walks for a page
    or a form drawn through ``init_resources``, every font it builds through
    ``PDFResourceManager.get_font``, and every loop building a font through
    its font modules' ``range``. Done once whatever the copy of this
    module."""
    from pdfminer import cmapdb, pdffont, pdftypes
    from pdfminer.arcfour import Arcfour
    from pdfminer.pdfinterp import (
        PDFContentParser,
        PDFPageInterpreter,
        PDFResourceManager,
    )
    from pdfminer.pdfpage import PDFPage
    from pdfminer.psparser import PSBaseParser
    from pdfplumber import utils
    from pdfplumber.page import PDFPageAggregatorWithMarkedContent as Device

    with _HOLDING:
        if hasattr(pdftypes.PDFStream.get_data, _MARK):
            return
        names = vars(pdftypes)
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
                    "PDF names a predictor row wider than its stream; "
                    "refusing to read it"
                )
            return bytes(png_predictor(pred, colors, columns, bits, data))

        for name, label in (
            ("lzwdecode", "LZW"),
            ("rldecode", "run-length"),
            ("ccittfaxdecode", "CCITT fax"),
        ):
            names[name] = _refused_while_reading(label, names[name])
        paint = Device.paint_path

        def unpainted(self: Device, *args: Any, **kwargs: Any) -> None:
            # A card's text is read off its glyphs alone, and a page's paths
            # were most of what it held: 123 000 segments on a Bolt page.
            if _READING.get() is None:
                paint(self, *args, **kwargs)

        dedupe = vars(utils)["dedupe_chars"]

        def deduped(chars: list[Any], *args: Any, **kwargs: Any) -> Any:
            if _READING.get() is None:
                return dedupe(chars, *args, **kwargs)
            return dedupe(_Positions(chars), *args, **kwargs)

        push = PDFPageInterpreter.push

        def pushed(self: PDFPageInterpreter, obj: Any) -> None:
            push(self, obj)
            _held(len(self.argstack), _MAX_STACK)
            _held(len(self.curpath), _MAX_PATH_SEGMENTS)

        # pdfminer calls an operator with as many operands as its method
        # names, so these two keep theirs.
        save = PDFPageInterpreter.do_q

        def saved(self: PDFPageInterpreter) -> None:
            save(self)
            _held(len(self.gstack), _MAX_STACK)

        close = PDFPageInterpreter.do_h

        def closed(self: PDFPageInterpreter) -> None:
            close(self)
            _held(len(self.curpath), _MAX_PATH_SEGMENTS)

        parse = PDFContentParser.push

        def parsed(self: Any, *objs: Any) -> None:
            parse(self, *objs)
            _held(len(self.curstack), _MAX_STACK)

        start = PDFContentParser.start_type

        def nested(self: Any, pos: int, type: str) -> None:
            start(self, pos, type)
            _held(len(self.context), _MAX_NESTING)

        init_resources = PDFPageInterpreter.init_resources

        def walked(self: PDFPageInterpreter, resources: dict[object, object]) -> None:
            # Walked again for each form drawn, each entry a font looked up,
            # a colour space made, a form or image named or a procedure set
            # listed. Only those kinds count, the ones init_resources reads:
            # resolving another, which it never does, could fail the card.
            reading = _READING.get()
            listed = pdftypes.resolve1(resources)
            if reading is not None and isinstance(listed, dict):
                for kind in ("Font", "ColorSpace", "XObject", "ProcSet"):
                    entries = pdftypes.resolve1(listed.get(kind))
                    if isinstance(entries, (dict, list)):
                        reading.parse(len(entries))
            init_resources(self, resources)

        revreadlines = PSBaseParser.revreadlines

        def backwards(self: PSBaseParser) -> Iterator[bytes]:
            # pdfminer's, which reads the lines a buffer at a time, putting
            # each in front of the rest of its line: 16 MB with no line end
            # took 46,8 s. Found in the file whole, each counted as parsed.
            reading = _READING.get()
            if reading is None:
                yield from revreadlines(self)
                return
            self.fp.seek(0)
            data = self.fp.read()
            end = len(data)
            while True:
                lf = data.rfind(b"\n", 0, end)
                n = max(lf, data.rfind(b"\r", max(lf, 0), end))
                if n < 0:
                    return
                reading.parse()
                yield data[n:end]
                end = n

        get_font = PDFResourceManager.get_font
        unnamed: dict[str, object] = {}

        def font(self: PDFResourceManager, objid: object, spec: Any) -> Any:
            # pdfminer keeps a font it found by reference, and builds one
            # written into the resources again for each page or form drawn:
            # 100 000 widths drawn 400 times took 46,7 s. It builds one of
            # a new empty dictionary for each font a page names but lacks:
            # naming one cost 146 us a token on a Raspberry Pi.
            reading = _READING.get()
            if reading is None or objid:
                return get_font(self, objid, spec)
            return reading.font(spec or unnamed, lambda: get_font(self, objid, spec))

        create_pages = PDFPage.create_pages

        def paged(document: Any) -> Iterator[PDFPage]:
            # Counted for each listing: pdfplumber lists the pages again to
            # close them.
            for count, page in enumerate(create_pages(document), 1):
                reading = _READING.get()
                if reading is not None:
                    reading.list_page(count)
                yield page

        names["ascii85decode"] = _ascii85_held(names["ascii85decode"])
        names["apply_png_predictor"] = predicted
        setattr(measured, _MARK, True)
        pdftypes.PDFStream.get_data = measured  # type: ignore[method-assign]
        Arcfour.decrypt = charged
        Device.render_char = _built(Device.render_char)  # type: ignore[method-assign]
        Device.begin_figure = _built(Device.begin_figure)  # type: ignore[method-assign]
        Device.paint_path = unpainted  # type: ignore[method-assign]
        vars(utils)["dedupe_chars"] = deduped
        PDFPageInterpreter.push = pushed  # type: ignore[method-assign]
        PDFPageInterpreter.do_q = saved  # type: ignore[method-assign]
        PDFPageInterpreter.do_h = closed  # type: ignore[method-assign]
        PDFContentParser.push = parsed  # type: ignore[method-assign]
        PDFContentParser.start_type = nested  # type: ignore[method-assign]
        PDFPage.create_pages = staticmethod(paged)  # type: ignore[method-assign]
        PDFResourceManager.get_font = font  # type: ignore[method-assign]
        PDFPageInterpreter.init_resources = walked  # type: ignore[method-assign]
        PSBaseParser.nexttoken = _parsed(PSBaseParser.nexttoken)  # type: ignore[method-assign]
        # An object that is a reference to itself, or a ring of them, has
        # pdfminer resolve it for good, from what it has parsed already.
        pdftypes.PDFObjRef.resolve = _parsed(pdftypes.PDFObjRef.resolve)  # type: ignore[method-assign]
        # Every parser goes through fillbuf at each step, a content parser
        # through its own.
        PSBaseParser.fillbuf = _stepped(PSBaseParser.fillbuf)  # type: ignore[method-assign]
        PDFContentParser.fillbuf = _stepped(PDFContentParser.fillbuf)  # type: ignore[method-assign]
        PDFContentParser.get_inline_data = _run(PDFContentParser.get_inline_data)  # type: ignore[method-assign]
        PSBaseParser.nextline = _run(PSBaseParser.nextline)  # type: ignore[method-assign]
        PSBaseParser.revreadlines = backwards  # type: ignore[method-assign]
        for module in (cmapdb, pdffont):
            vars(module)["range"] = _font_range
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
    once for each page drawing it and a form once for each time it is
    drawn, since pdfminer interprets each again, while it builds a font
    written into the resources, rather than referred to, once a read, as it
    does one referred to, where it built it again for each page or form
    drawn, and the font it makes for each name the resources lack once a
    read too; each inflate counts, every step of a stream inflated more than
    once, and is refused past what is left, before it is held, the
    cross-reference and object streams pdfminer inflates opening the file
    included; and a stream counts what its decoding hands back past its
    last inflate. A refusal ends the read where it is made, whatever
    pdfminer or pdfplumber catch.

    What pdfminer lays out and holds is held too, since it grows with what
    the bytes draw, not with the bytes: a 6,5 kB card drawing 488 000 glyphs
    on a page held 1,1 GB. A card lists no more than :data:`_MAX_PAGES`
    pages; each glyph, form and image counts as it is built, up to
    :data:`_MAX_PAGE_OBJECTS` on a page and :data:`_MAX_CARD_OBJECTS` on the
    card; no path is painted, a card's text being read off its glyphs alone;
    and no stack holds more than :data:`_MAX_STACK` at once, arrays and
    dictionaries nest no deeper than :data:`_MAX_NESTING`, and a path not
    yet painted has no more than :data:`_MAX_PATH_SEGMENTS` segments. It
    parses no more than :data:`_MAX_PARSED` tokens, references resolved and
    resources walked, a content stream's for each page drawing it and a
    form's for each draw, takes no more than :data:`_MAX_STEPS` steps,
    gathers no token, comment, line or inline image longer than
    :data:`_MAX_TOKEN_BYTES`, which it grows a byte or a buffer at a time,
    and builds no more than :data:`_MAX_FONT_ENTRIES` entries of a card's
    fonts. pdfplumber's ``dedupe_chars`` finds each glyph it keeps at once,
    where it looked each one up in the page's list.
    """
    _hold_pdfminer_to_the_bound()
    with _held_to_the_bound():
        yield
