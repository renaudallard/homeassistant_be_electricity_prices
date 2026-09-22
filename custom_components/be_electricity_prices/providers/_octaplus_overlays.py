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

"""OCTA+: the regulated legs its card reprints.

The distribution and transport tariffs, the levies and the prosumer tariff the
supplier states itself. None of it is the supplier's own price: it is the
regulator's, reprinted on the supplier's card.
"""

from __future__ import annotations

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    FLUVIUS_CARD_LABELS,
    REGION_WALLONIA,
)
from ._parse import to_float
from ._rates import TariffKind
from .base import DsoOverlay, ExtractorError, walloon_dso_overlay
import re


def _extract_supplier_prosumer(text: str, kind: TariffKind) -> float | None:
    """OCTA+ supplier-side compensation-regime PV forfait.

    Fixed and variable cards print "+ 4,77 EUR/kVA par mois" (the "Forfait
    panneaux solaires", applicable only under the compensation regime). It
    is TVAC (the card header is 6% TVAC; the unrelated AMR fallback is the
    "1,50 EUR/kVA HTVA par mois"), so it must NOT be VAT-scaled. It is
    billed on top of the DSO prosumer column by _compute_prosumer, exactly
    like the Cociter Variable and Mega forfaits. The card value is per
    MONTH, so annualise it (*12) to match supplier_prosumer_eur_per_kva_year
    (the coordinator divides by 12). The SMR3 dynamic product drops the
    compensation regime and omits the line; everywhere else it is
    mandatory, so a miss is a layout drift and we raise.
    """
    if kind == "dynamic":
        return None
    # Anchor on "<value> EUR/kVA par mois" without the "HTVA" the AMR
    # fallback carries, so a single regex picks the 4,77 forfait and never
    # the 1,50 AMR rate.
    match = re.search(r"([\d.,]+)\s*€/kVA\s+par\s+mois", text)
    if match is None:
        raise ExtractorError("OCTA+: supplier PV compensation forfait not found")
    return to_float(match.group(1)) * 12.0


def _extract_taxes(text: str, region: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, region_connection_fee).

    OCTA+ prints four federal-tax tier rows on the second page:

      ``Consommation entre 0 & 3.000 kWh 5,0329 0,2042``

    The first tier (0-3.000 kWh) is the residential one we surface.
    Wallonia adds a one-line connection fee (``Redevance raccordement
    Wallonie (c€/kWh) 0,075``).
    """
    # Anchor on the kWh range; the leading "Consommation" word can be
    # mangled on Flanders cards where the federal column shares its row
    # bucket with the Fonds Energie sidebar (e.g. "CCCConsommaaaation").
    tier1 = re.search(
        r"0\s*&\s*3\.000\s*kWh\s+([\d.,]+)\s+([\d.,]+)",
        text,
    )
    if tier1 is None:
        # Mandatory federal charges; a miss is a layout drift that would
        # silently under-price every kWh. Fail loud (matching
        # _extract_yearly_fee) rather than default to 0.
        raise ExtractorError("OCTA+: federal tax tier (0-3.000 kWh) not found")
    federal_excise = to_float(tier1.group(1)) / 100.0
    energy_contribution = to_float(tier1.group(2)) / 100.0

    region_connection_fee = 0.0
    if region == REGION_WALLONIA:
        fee = re.search(
            r"Redevance\s+raccordement\s+Wallonie[^0-9]*([\d.,]+)",
            text,
        )
        if fee is None:
            # Mandatory Walloon levy: every sibling extractor raises here
            # rather than return 0, because a per-kWh charge zeroed on a
            # label drift under-bills every Walloon entry silently and the
            # coordinator would rather keep the last good snapshot.
            raise ExtractorError("OCTA+: Wallonia connection fee row not found")
        region_connection_fee = to_float(fee.group(1)) / 100.0
    return federal_excise, energy_contribution, region_connection_fee


def _extract_wallonia_renewables(text: str) -> float:
    # The green-energy rate sits within a few dozen chars of the "Région
    # wallonne" header: on the "Coûts énergie verte" line on most cards,
    # or on its own line just above that header on Smart Variable. Anchor
    # on "Région wallonne" and take its first numeric neighbour, but bound
    # the non-digit run so a layout drift can't silently grab a far-away
    # digit (it then misses and raises below). [^\d] already crosses
    # newlines, so the old re.S flag was inert. Called only for Wallonia,
    # where the ~3.1 c€/kWh surcharge is mandatory; raise on a miss.
    match = re.search(r"Région\s+wallonne[^\d]{0,80}?([\d.,]+)", text)
    if match is None:
        raise ExtractorError("OCTA+: Wallonia green-energy surcharge not found")
    return to_float(match.group(1)) / 100.0


def _row_value(text: str, label: str) -> "re.Match[str] | None":
    """The number belonging to ``label``, printed after it or above it.

    The column reconstruction does not always keep a value on its label's
    line. On the January to May 2026 Smart Variable cards the Flemish block
    reads "Region flamande / 1,166 / Couts energie verte / Couts cogeneration
    0,430": the cogeneration value follows its label and the green-energy one
    sits on the line before. Reading only the first form dropped 1,166 c€/kWh
    and billed the cogeneration row alone, about 41 EUR a year at 3500 kWh.

    The value-above form is anchored on the label with nothing but whitespace
    between them, so a figure belonging to another row cannot be picked up.
    The same label also heads the block and opens the footnote that explains
    it, and there it is followed by its unit or by prose rather than by a
    figure; those occurrences are skipped, or the number ending the tax table
    just above the heading would be read as the row (measured: 0,2042 picked
    up as the green-energy cost).
    """
    after = re.search(rf"{label}\s+(\d+(?:[.,]\d+)?)", text)
    if after is not None:
        return after
    for match in re.finditer(rf"(\d+(?:[.,]\d+)?)\s*\n\s*{label}", text):
        tail = text[match.end() : match.end() + 12].lstrip()
        if tail.startswith("(") or tail[:3].lower() == "les":
            continue
        return match
    return None


def _extract_flanders_renewables(text: str) -> float:
    """Flanders cards split renewables across two rows:
    ``Coûts énergie verte`` and ``Coûts cogénération``.
    """
    green = _row_value(text, "Coûts énergie verte")
    cogen = _row_value(text, "Coûts cogénération")
    if green is None and cogen is None:
        # Called only for Flanders, where the green-energy / cogeneration
        # surcharge is mandatory; both gone means the block drifted, so
        # raise rather than silently zero ~1.6 c€/kWh.
        raise ExtractorError("OCTA+: Flanders green-energy surcharge not found")
    total = 0.0
    if green:
        total += to_float(green.group(1))
    if cogen:
        total += to_float(cogen.group(1))
    return total / 100.0


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Wallonia rows (10 numbers each) in the aligned output:

    mono | jour | nuit | PIC | MEDIUM | ECO | excl_nuit | terme_fixe
    (€/an) | prosumer (€/kVA/an) | transport (c€/kWh)
    """
    out: dict[str, DsoOverlay] = {}
    for pattern, key in _WALLONIA_LABELS:
        match = re.search(
            rf"{pattern}[^\n]*?"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        pic = to_float(match.group(4))
        medium = to_float(match.group(5))
        eco = to_float(match.group(6))
        excl_night = to_float(match.group(7))
        terme_fixe = to_float(match.group(8))
        # Cols 9/10 are the prosumer forfait (€/kVA/an) and the transport
        # rate (c€/kWh), but the 2026 template swapped their order.
        # Disambiguate by magnitude: the prosumer forfait (~80-100) always
        # dwarfs the transport rate (~2-3 c€/kWh).
        col_a = to_float(match.group(9))
        col_b = to_float(match.group(10))
        prosumer = max(col_a, col_b)
        transport = min(col_a, col_b)
        out[key] = walloon_dso_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            pic=pic,
            medium=medium,
            eco=eco,
            transport=transport,
            terme_fixe=terme_fixe,
            prosumer=prosumer,
        )
    return out


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Flanders rows. The digital-meter row carries:

      dist_normal | dist_excl_night | data_mgmt_qh (€/an) |
      data_mgmt_year (€/an) | capacity (€/kW/yr) | - | -

    A second analog-meter row carries the prosumer rate as the last
    column. Some cards (Dynamic Flanders) prepend extra column-header
    glyphs to one digital row and strip the leading 'F' from the rest,
    so the digital regex is anchored on the unique sub-area suffix
    rather than the full ``Fluvius X`` label and tolerates multi-glyph
    cell separators (``-------- --------``).
    """
    prosumer_by_key: dict[str, float] = {}
    # Second row: 'Fluvius X 8,09 7,55 18,92 - - 130,92 54,63'
    for m in re.finditer(
        r"^(Fluvius [^\n]+?)\s+"
        + r"[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+-+\s+-+\s+[\d.,]+\s+([\d.,]+)\s*$",
        text,
        re.MULTILINE,
    ):
        label = m.group(1).strip()
        if label in _FLANDERS_LABELS:
            prosumer_by_key[_FLANDERS_LABELS[label]] = to_float(m.group(2))

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        # Strip the "Fluvius " prefix so a digital row labelled "luvius
        # Halle-Vilvoorde" still matches against the suffix.
        suffix = label.removeprefix("Fluvius ")
        match = re.search(
            rf"{re.escape(suffix)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)"
            + r"(?:\s+-+\s+-+)?",
            text,
        )
        if not match:
            continue
        dist_normal = to_float(match.group(1))
        dist_excl_night = to_float(match.group(2))
        # group(3) is the card's "quart-horaire" data-management column
        # (~61 EUR). It is deliberately NOT used for the SMR3 dynamic
        # product: it contradicts the authoritative Fluvius SMR3
        # data-management fee (~18,56 EUR, per the Luminus card footnote),
        # so billing the dynamic at it would over-charge ~42 EUR/yr. The
        # mensuel/annuel value (group 4, 18,92 EUR) matches the standard
        # databeheer the rest of the integration uses, so use it for all
        # meter regimes pending an authoritative Fluvius quart-horaire rate.
        data_mgmt_year = to_float(match.group(4))
        capacity = to_float(match.group(5))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            distribution_exclusive_night=dist_excl_night / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt_year,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
        )
    return out


# Matched case-insensitively (see _extract_wallonia_dsos): the 2026
# template recased the labels from ALLCAPS to title case and renamed two
# of them ("TECTEO - RESA" -> "RESA", "REGIEDEWAVRE" -> "Régie de Wavre").
_WALLONIA_LABELS: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    # Eight ORES sub-areas share the same tariff line; match the first.
    # ``ORES`` may or may not have a space before the opening paren
    # depending on which OCTA+ card we hit.
    (r"ORES\s*\(", DSO_ORES),
    # Older cards prefixed this "TECTEO - RESA"; the bare "RESA" token
    # anchors both spellings.
    ("RESA", DSO_RESA),
    # Older cards printed "REGIEDEWAVRE" (no spaces); the accent class and
    # optional spacing anchor both.
    (r"R[ée]gie\s*de\s*Wavre", DSO_REW),
)
_FLANDERS_LABELS = FLUVIUS_CARD_LABELS
