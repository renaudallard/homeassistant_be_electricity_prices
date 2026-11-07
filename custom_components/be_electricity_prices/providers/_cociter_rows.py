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

"""Cociter's per-meter rows: the indicative rate, the BELIX formula and the
price ceiling the row of each meter prints.

Split out of ``cociter.py``, as ``_cociter_settle.py`` was, so that module
stays under a thousand lines. These are the leaf readers ``_extract_energy``
and ``_impact_energy`` call.
"""

from __future__ import annotations

import re

from ._parse import SIGN_CHARS, parse_sign, to_float

# One BELIX row per meter. The label anchors are mandatory: an optional one
# lets the pattern wander onto the consumption formula of the July 2026
# dynamic re-upload, which is the trap the injection pattern's own mandatory
# "Compteur" label already exists for.
_BELIX_ROW_TAIL = (
    rf"\(([\d,]+)\s*x\s*BELIX\s*"
    rf"([{SIGN_CHARS}])\s*([\d,]+)\)\s*\+\s*(\d+)\s*%\s*TVA"
)


def _row(label: str, tail: str) -> str:
    """A pattern for ``label`` and the first ``tail`` after it on its line.

    The gap between them stops at the next ``label`` on the line. A ``tail``
    before that one is found as before; past it, the next ``label`` finds the
    same first ``tail``, so the groups are those an open gap read and only the
    match starts later. Open to the end of the line, a line of labels was read
    to its end again from each. Where a tail opens on a figure, the tail
    itself reads it from the start of its digits.
    """
    return rf"{label}(?:(?!{label})[^\n])*?{tail}"


_BELIX_BANDS: tuple[tuple[str, str], ...] = (
    ("peak", r"Heures pleines"),
    ("offpeak", r"Heures creuses"),
    ("exclusive_night", r"Compteur exclusif nuit"),
)


def _belix_band_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """Per-meter BELIX ``(factor, base)`` on a Cociter variable card.

    Same conversion as the mono row: the coefficients print c€/kWh against a
    BELIX expressed in EUR/MWh, so the factor takes a x10 and the base a /100,
    and the "+ N% TVA" printed outside the parens lands on both. The VAT is
    read per row rather than hardcoded.
    """
    out: dict[str, tuple[float, float]] = {}
    for key, label in _BELIX_BANDS:
        match = re.search(_row(label, _BELIX_ROW_TAIL), text)
        if match is None:
            continue
        vat_mult = 1.0 + to_float(match.group(4)) / 100.0
        out[key] = (
            to_float(match.group(1)) * vat_mult * 10.0,
            parse_sign(match.group(2)) * to_float(match.group(3)) * vat_mult / 100.0,
        )
    return out


# The "Prix maximum" column, printed on the same row as the indicative and
# in the same unit. Both figures end in "c€/kWh", so the ceiling is the
# SECOND one on the row.
_CEILING_TAIL = r"(?<!\d)\d+,\d+\s*c€/kWh[^\n]*?(?<!\d)(\d+,\d+)\s*c€/kWh"
# The indicative, the first figure on the row.
_INDICATIVE_TAIL = r"(?<!\d)(\d+,\d+)\s*c€/kWh"

_VARIABLE_CEILING_ROWS: tuple[tuple[str, str], ...] = (
    ("single", r"Compteur monohoraire"),
    ("peak", r"Heures pleines"),
    ("offpeak", r"Heures creuses"),
    ("exclusive_night", r"Compteur exclusif nuit"),
)


def _price_ceilings(
    text: str, rows: tuple[tuple[str, str], ...]
) -> dict[str, float | None]:
    """The card's "Prix maximum" column, per row, as TVAC EUR/kWh.

    Note (8): "COCITER a decide de plafonner le prix de fourniture de
    l'energie [...] si le Belix venait a augmenter au point que
    l'application du prix variable (indexe) donne une valeur superieure au
    prix maximum, c'est le prix maximum qui s'applique." The cap is on the
    supply price only; network, taxes and surcharges stay due in full, which
    is exactly what ``pricing`` already does with a ceiling.

    A row without the column yields None rather than 0.0, so a card that
    stops publishing the cap goes back to uncapped instead of pricing every
    kWh at zero. The dynamic card prints no cap at all and reaches this with
    no matching row.
    """
    out: dict[str, float | None] = {}
    for key, label in rows:
        match = re.search(_row(label, _CEILING_TAIL), text)
        out[key] = to_float(match.group(1)) / 100.0 if match else None
    return out
