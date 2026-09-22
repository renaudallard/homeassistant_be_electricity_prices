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

"""EnergyVision: the regulated legs its Dutch cards reprint.

The distribution and transport tariffs and the levies, for Flanders and for
Brussels, where the same supplier sells as Brusol. The Walloon card prints
its own in French and is read next door.
"""

from __future__ import annotations

from ..const import DSO_SIBELGA
from ..const import REGION_BRUSSELS
from ..const import REGION_FLANDERS
from ._parse import numeric_row
from ._parse import parse_sibelga_row
from ._parse import regional_tax_overlay
from ._parse import to_float
from .base import DsoOverlay
from .base import ExtractorError
from .base import TaxOverlay
import re
from ..const import FLUVIUS_AREA_LABELS_UPPER
from ._pdf import NUM_NO_THOUSANDS
from .energiebe import _NUM


def _extract_taxes(text: str) -> TaxOverlay:
    """Every value on the card is VAT-inclusive.

    The flat August-2026 excise row is tried before the tiered one being
    phased out: a card carrying both is mid-transition and the flat rate is
    authoritative. GSC and WKK arrive pre-summed in one row here.
    """
    return regional_tax_overlay(
        text,
        supplier="EnergyVision",
        region=REGION_FLANDERS,
        excise=(_FLAT_EXCISE_RE, _EXCISE_RE),
        renewables=(_GSC_WKC_RE,),
        contribution=_CONTRIB_RE,
        fund=_FUND_RE,
    )


def _extract_brussels_taxes(text: str) -> TaxOverlay:
    """The Brussels tax block, VAT-inclusive like the Flemish one.

    Two rows only. Brussels levies no energy fund, and the federal energy
    contribution was abolished on 2026-08-01 and is printed by no current
    card, so both are left at zero rather than looked for: on this card an
    absent row is the levy not existing, not a layout drift.
    """
    return regional_tax_overlay(
        text,
        supplier="EnergyVision",
        region=REGION_BRUSSELS,
        excise=(_FLAT_EXCISE_RE, _EXCISE_RE),
        renewables=(_BRUSSELS_GREEN_RE,),
    )


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """The Sibelga row, in the eight-column layout Engie's cards also print.

    Mandatory: this runs only on a Brussels card, where a missing row is a
    drift that would leave the entry with no network cost at all. Refusing
    keeps the last good card serving.
    """
    overlay = parse_sibelga_row(text)
    if overlay is None:
        raise ExtractorError("EnergyVision: Sibelga row not found")
    return {DSO_SIBELGA: overlay}


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find(_DIGITAL_MARKER)
    if start < 0:
        raise ExtractorError("EnergyVision: digital-meter DSO table not found")
    end = text.find(_ANALOG_MARKER, start)
    section = text[start:end] if end > start else text[start:]
    out: dict[str, DsoOverlay] = {}
    for area, key in _DSO_ROWS:
        row = numeric_row(section, f"FLUVIUS {area}", 5)
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row[1]) / 100.0,
            distribution_exclusive_night=to_float(row[2]) / 100.0,
            transport=0.0,
            capacity_eur_per_kw_year=to_float(row[0]),
            data_management_per_year=to_float(row[3]),
            network_ceiling_eur_per_kwh=to_float(row[4]) / 100.0,
        )
    missing = [key for _, key in _DSO_ROWS if key not in out]
    if missing:
        # A partial table is worse than none: the areas are what every
        # entry picks its network cost from, and a card missing one would
        # be adopted, persisted and shared, leaving the entry on that area
        # with no overlay and every tick failing, while the last good card
        # is gone from the cache. Refusing keeps that card serving.
        raise ExtractorError(f"EnergyVision: DSO rows not found for {sorted(missing)}")
    return out


# Taxes (Flanders). GSC + WKC print as a single combined value; the
# energiefonds shows a domiciled (standard residential = 0 EUR/month) and a
# non-domiciled row; bill the domiciled one.
# "Kosten GSC en WKC geldig voor 1,554 €cent/kWh" on most cards and
# "... bedragen 1,554 €cent/kWh" on the laadpunt one. Same levy, same figure,
# two verbs; pinning one of them lost the whole tax overlay on the other.
_GSC_WKC_RE = re.compile(
    rf"GSC\s+en\s+WKC\s+(?:geldig\s+voor|bedragen)\s+{_NUM}", re.IGNORECASE
)
# From 1 August 2026 the federal scheme folded the separate energy
# contribution into the special excise and flattened it, so the tier table
# and the Energiebijdrage row both left the card and one "Bijzondere
# accijns" rate took their place. EnergyVision switched on its August
# Flemish card, a month after Engie / Mega / Eneco. The Walloon card is
# still on the old shape and keeps its own parser (_extract_taxes_fr).
_FLAT_EXCISE_RE = re.compile(rf"Bijzondere\s+accijns\s+{_NUM}", re.IGNORECASE)
# Taxes (Brussels). One green levy instead of the Flemish GSC + WKC pair:
# "Kosten Groene stroom 2,737 €cent/kWh". The "Kosten" is what keeps this off
# the page-1 energy rows, which name the same product without it.
_BRUSSELS_GREEN_RE = re.compile(
    rf"Kosten\s+Groene\s+stroom\s+{_NUM}\s*€?\s*cent", re.IGNORECASE
)
# The Flanders DSO table prints two blocks (digital + analog meter). Only the
# digital-meter block is billed (modern smart meters); its five columns are
# capaciteitstarief (EUR/kW/yr) | kWh-tarief (c€/kWh) | kWh excl. nacht
# (c€/kWh) | databeheer (EUR/yr) | maximumtarief (c€/kWh), the VREG ceiling on
# capacity plus the per-kWh network term.
_DIGITAL_MARKER = "Digitale Meter"
_ANALOG_MARKER = "Analoge Meter"


_CONTRIB_RE = re.compile(rf"Energiebijdrage\s+{_NUM}", re.IGNORECASE)
_EXCISE_RE = re.compile(
    rf"Verbruik\s+tussen\s+0\s*&\s*3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
_FUND_RE = re.compile(
    rf"Standaard\s+tarief\s+gedomicilieerd\s*:\s*{_NUM}\s*€\s*/\s*maand",
    re.IGNORECASE,
)
# Upper-case Fluvius area label -> DSO key (EnergyVision prints them in caps,
# so the shared Title-case FLUVIUS_CARD_LABELS map doesn't apply). Kempen is
# the Iveka sub-area; Midden-Vlaanderen is Intergem.
_DSO_ROWS: tuple[tuple[str, str], ...] = tuple(FLUVIUS_AREA_LABELS_UPPER.items())


# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = NUM_NO_THOUSANDS
