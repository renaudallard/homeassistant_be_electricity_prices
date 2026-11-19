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

"""energie.be's network table reader, split out of ``energiebe.py`` to keep
it under the package's module size limit.
"""

from __future__ import annotations

import re

from ..const import FLUVIUS_CARD_LABELS, FLUVIUS_CARD_TOKENS
from ._parse import numeric_row, to_float
from ._pdf import NUM_NO_THOUSANDS
from .base import DsoOverlay, ExtractorError

# Row label prefix (regex-safe, unique) -> DSO key. The card wraps two long
# labels across the number row ("Fluvius (Halle-\n<numbers>\nVilvoorde)" and
# the Midden-Vlaanderen row), so each entry anchors on the leading token and
# grabs the four digital-meter columns that follow, wherever they land.
_DSO_ROWS = FLUVIUS_CARD_TOKENS
# The whole label, "Fluvius (Halle-Vilvoorde)", for a row the card prints on
# one line. numeric_row scores the label it is given against the whole row
# label, so the leading token alone falls below its threshold there.
_DSO_LABELS = {
    key: label.replace("Fluvius ", "Fluvius (", 1) + ")"
    for label, key in FLUVIUS_CARD_LABELS.items()
}

_NUM = NUM_NO_THOUSANDS


# A row whose whole label pdfplumber prints on the line under its eight
# figures, as the November 2026 cards' Fluvius (West): the label is moved in
# front of them before the rows are read. A label wrapped mid-word, whose
# tail sits under the figures ("Vilvoorde)"), is no whole label.
_LABEL_BELOW_RE = re.compile(
    r"^((?:\d+(?:[.,]\d+)?[ \t]+){7}\d+(?:[.,]\d+)?)[ \t]*\n"
    r"(Fluvius \([^()\n]*\))[ \t]*$",
    re.MULTILINE,
)


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The residential network table.

    An area whose row does not read is left out. ``fetch`` refuses a live
    card short of an area and ``fetch_for_month`` the running month's
    (``require_every_area``), while a past month's card keeps its energy and
    the month cache bills the missing area on the nearest card of the same
    calendar year that prints it. A table nothing could be read from raises.
    """
    start = text.find("Nettarieven")
    if start < 0:
        raise ExtractorError("could not locate energie.be DSO table")
    # The residential block only. The dynamic card carries a SECOND
    # "Nettarieven" table, the professional one, and it prints its own
    # maximumtarief HTVA (0,3276168 against the residential 0,3472738). A
    # whole-document search hands a residential entry a ceiling 6% low.
    nxt = text.find("Nettarieven", start + 1)
    section = _LABEL_BELOW_RE.sub(
        r"\2 \1", text[start:] if nxt < 0 else text[start:nxt]
    )
    # "Voor digitale meters geldt een maximumtarief van 0,3472738 EUR/kWh
    # (excl. databeheer)." pdfplumber renders it "maxi mumtarief" on the two
    # August cards and "maximumtarief" on the July dynamic one, so the space
    # is optional or only one of the three generations matches. Stored as
    # printed, on this card's own basis.
    ceiling_match = re.search(
        r"maxi\s*mumtarief\s+van\s+" + _NUM + r"\s*EUR/kWh", section, re.IGNORECASE
    )
    ceiling = to_float(ceiling_match.group(1)) if ceiling_match else None
    out: dict[str, DsoOverlay] = {}
    for prefix, key in _DSO_ROWS:
        # Eight figures: the four digital-meter columns read here, then the
        # four classic-meter ones. Asked for by the whole label first, then
        # by the leading token, because the card wraps its two longest
        # labels mid-word ("Fluvius (Halle-" sits on a line of its own above
        # the figures) and that head scores too low against the whole label.
        # Neither form reaches the threshold against another area's row.
        row = numeric_row(section, _DSO_LABELS[key], 8) or numeric_row(
            section, f"Fluvius ({prefix}", 8
        )
        if not row:
            continue
        databeheer = to_float(row[0])
        capacity = to_float(row[1])
        normal = to_float(row[2]) / 100.0
        excl_night = to_float(row[3]) / 100.0
        out[key] = DsoOverlay(
            distribution_single=normal,
            distribution_exclusive_night=excl_night,
            transport=0.0,
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
            network_ceiling_eur_per_kwh=ceiling,
        )
    if not out:
        raise ExtractorError("energie.be: no DSO row read")
    return out
