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

"""Energy Knights grid and levy overlays: the DSO table and the tax block.

Split out of ``energyknights.py``, as ``_ecopower_overlays.py`` was out of
``ecopower.py``. The cut is the call graph: these are the leaf parsers
``parse_snapshot`` calls to build the ``DsoOverlay`` map and the
``TaxOverlay``, where ``energyknights.py`` keeps the urls, the archive and the
energy and injection legs.

The split changed no behaviour: every function here was byte-identical to
the one it replaced.
"""

from __future__ import annotations

import re

from ..const import FLUVIUS_CARD_TOKENS, REGION_FLANDERS
from ._parse import regional_tax_overlay, to_float
from ._pdf import NUM_NO_THOUSANDS
from .base import DsoOverlay, ExtractorError, TaxOverlay

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = NUM_NO_THOUSANDS
# Horizontal whitespace only, never a newline. A bare \s would let a row bind
# to the NEXT row's formula whenever the renderer emits the indicative column
# and the formula column as separate blocks, and the index-token guard cannot
# see that: both rows name the same index, so it checks the name and never
# that the formula belongs to the row: on a card whose registers carry
# different coefficients that silently gives one register another's rate.
_H = r"[^\S\n]"

# The net-tariff section prints the same eight areas twice, once for a digital
# meter and once for a classic one, and the two differ by more than half on
# the capacity term. Only the digital block is read, cut at the classic
# header so no analog row can leak in.
# Both markers must stand alone on their line. A plain substring search binds
# the digital one to the solar footnote instead, "Heb je zonnepanelen en een
# digitale meter?", which sits about 900 characters above the table on every
# card where that sentence is not wrapped. Case-insensitive for the same
# reason every other anchor here is: a re-render that capitalises the header
# would otherwise leave the classic table inside the slice.
_DIGITAL_RE = re.compile(
    r"^[^\S\n]*digitale\s+meter[^\S\n]*$", re.IGNORECASE | re.MULTILINE
)
_CLASSIC_RE = re.compile(
    r"^[^\S\n]*klassieke\s+meter[^\S\n]*$", re.IGNORECASE | re.MULTILINE
)

# Row label prefix -> canonical DSO key. Anchored on the leading token so a
# wrapped label ("Fluvius (Halle-" / "Vilvoorde)") still binds to its figures.
_DSO_ROWS = FLUVIUS_CARD_TOKENS

# Excise, taken from the first consumption band. The card prints three bands
# and they have not always been equal: every month from 2024-06 to 2026-07 read
# 5,0329 / 5,0329 / 4,8188, and only the August 2026 card is flat at 4,8760.
# regional_tax_overlay reads the table this row opens into bands.
_EXCISE_RE = re.compile(
    rf"Verbruik\s+tussen\s+0\s+en\s+3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
_GSC_RE = re.compile(rf"Bijdrage\s+groene\s+stroom\s+{_NUM}", re.IGNORECASE)
_WKK_RE = re.compile(rf"Bijdrage\s+WKK\s+{_NUM}", re.IGNORECASE)
# The tax block is a two-column interleave and "Standaard tarief" heads a row
# in both columns, once under the energy fund in EUR/month and once under the
# energiebijdrage in c€/kWh. Reading either without its header attached is a
# hundredfold unit error in whichever direction the match lands, so both
# anchor on the header line above them.
_CONTRIB_RE = re.compile(
    rf"Energiebijdrage\s*\(c[^)]*\)[^\n]*\n\s*Standaard\s+tarief\s+{_NUM}",
    re.IGNORECASE,
)
# The "Standaard tarief" row, not the 10,07 "Niet-gedomicilieerd" one: this
# integration prices a domiciled residential entry, the same choice every
# sibling makes. The card spells it "Niet-gedomiciliëerd" with two e's.
_FUND_RE = re.compile(
    rf"Bijdrage\s+energiefonds\s*\([^)]*\)[^\n]*\n\s*Standaard\s+tarief\s+{_NUM}",
    re.IGNORECASE,
)


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital-meter half of the net-tariff table.

    The card prints a classic-meter table below it whose distribution rate is
    half again as high and whose capacity term is more than double, so the cut
    is not cosmetic. Every Energy Knights product is sold as "100% digitaal"
    and the dynamic ones bill on meetregime 3 quarter values, which only a
    digital meter produces.

    An area whose row does not read is left out. ``fetch`` refuses a live
    card short of an area and ``fetch_for_month`` the running month's
    (``require_every_area``), while a past month's card keeps its energy and
    the month cache bills the missing area on the nearest card of the same
    calendar year that prints it. A table nothing could be read from raises.
    """
    head = _DIGITAL_RE.search(text)
    if head is None:
        raise ExtractorError("Energy Knights: could not locate the DSO table")
    tail = _CLASSIC_RE.search(text, head.end())
    section = text[head.end() :] if tail is None else text[head.end() : tail.start()]
    out: dict[str, DsoOverlay] = {}
    for prefix, key in _DSO_ROWS:
        row = re.search(
            rf"Fluvius{_H}*\({_H}*{re.escape(prefix)}[^\d\n]*"
            rf"{_NUM}{_H}+{_NUM}{_H}+{_NUM}{_H}+{_NUM}{_H}+{_NUM}",
            section,
            re.IGNORECASE,
        )
        if row is None:
            continue
        # Columns: afname normaal, afname ex nacht, databeheer SMR1,
        # databeheer SMR3, capaciteitstarief. SMR3 is the quarter-hourly
        # regime the dynamic products bill on. Energy Knights has printed the
        # two databeheer columns at the same value on every card since
        # January 2025, so the column choice has never mattered in practice.
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(1)) / 100.0,
            distribution_exclusive_night=to_float(row.group(2)) / 100.0,
            transport=0.0,
            data_management_per_year=to_float(row.group(4)),
            capacity_eur_per_kw_year=to_float(row.group(5)),
        )
    if not out:
        raise ExtractorError("Energy Knights: no DSO row read")
    return out


def _extract_taxes(text: str) -> TaxOverlay:
    """Every value in this block is VAT-inclusive as printed, the excise
    included: 4,8760 is 4,60 x 1,06, and the one card in this repo that prints
    ex-VAT figures (Ecopower) carries 0,04748 for the same band, which is
    5,0329 / 1,06. The energy fund is the single exemption, per the card's own
    footnote (1) "Bedrag niet onderworpen aan BTW", and regional_tax_overlay
    leaves it unscaled."""
    return regional_tax_overlay(
        text,
        supplier="Energy Knights",
        region=REGION_FLANDERS,
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_CONTRIB_RE,
        fund=_FUND_RE,
    )
