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

"""Luminus: the regulated legs its card reprints.

The distribution and transport tariffs, the per-kWh taxes and the regional
levies. Luminus prints the tax block as a table whose rows move between
editions, so the block reader sits here with the rows it feeds.
"""

from __future__ import annotations

from ._parse import numeric_row
from ._parse import parse_vreg_network_ceiling
from ._parse import to_float
from ._rates import TariffKind
from .base import DsoOverlay
from .base import ExtractorError
import re
from ..const import DSO_AIEG
from ..const import DSO_AIESH
from ..const import DSO_ORES
from ..const import DSO_RESA
from ..const import DSO_REW
from ..const import FLUVIUS_CARD_LABELS


def _tax_block_values(text: str) -> list[str]:
    """Return the contiguous ['-', '5,0329', ...] run after the tax labels.

    The 'Taxes et redevances' section prints every label first then the
    matching values on their own lines, in the same order:

      [labels]
        Cotisation Fonds énergie (€/mois)
            Basse tension non résidentiel
            Basse tension résidentiel
        Droit d'accise spécial (c€/kWh)
        Cotisation sur l'énergie (c€/kWh)
        Redevance de raccordement (c€/kWh)        # Wallonia only
      [values]
        BTNR
        BTR
        Excise
        Cotisation
        Redevance                                  # Wallonia only

    Each value sits alone on its line - that's what tells us where the
    value list ends and the footnotes begin (the footnotes start with
    '(*) ...' and intermix numbers with text on the same line).
    """
    # 'Taxes et redevances' is mentioned twice in every PDF: once in the
    # 'Composition du prix' legend (no colon, no region) and once for the
    # actual tax table (`3 Taxes et redevances : WAL/FL`). Anchor on the
    # colon to only match the second one.
    block = re.search(
        r"3 Taxes et redevances\s*:\s*(?:WAL|FL|BRU).+?"
        r"(?=INFORMATION SUR VOTRE TARIF|Conditions\b)",
        text,
        re.S,
    )
    if not block:
        return []
    return re.findall(rf"^\s*(-|{_NUM})\s*$", block.group(0), re.MULTILINE)


def _extract_per_kwh_taxes(text: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, connection_fee) in EUR/kWh.

    Federal excise + energy contribution are mandatory across regions;
    Walloon connection fee is mandatory in Wallonia (the
    'Redevance de raccordement' label is present iff the card is a
    Wallonia card). Raise on a layout drift that would otherwise zero
    out the regulated tax silently and underbill ~50 EUR/year per
    missed tier.
    """
    values = _tax_block_values(text)

    def _decimal(s: str | None) -> float:
        if s is None or s == "-":
            return 0.0
        return to_float(s) / 100.0

    if len(values) < 4:
        raise ExtractorError(
            f"Luminus: 'Taxes et redevances' block too short ({len(values)} values; "
            "expected ≥4 BTNR / BTR / excise / contribution)"
        )
    excise = _decimal(values[2])
    contribution = _decimal(values[3])
    has_connection = "Redevance de raccordement" in text
    if has_connection and len(values) < 5:
        raise ExtractorError(
            "Luminus: Walloon connection-fee row missing from tax block"
        )
    connection = _decimal(values[4]) if has_connection else 0.0
    return excise, contribution, connection


def _extract_energy_fund(text: str) -> float:
    """Pick the BTR (Basse tension résidentiel) value from the tax block.

    Flanders prints BTNR (non-residential) first then BTR (residential);
    the integration's residential users want BTR. A '-' means no fee.
    """
    values = _tax_block_values(text)
    if len(values) < 2 or values[1] == "-":
        return 0.0
    return to_float(values[1])


def _extract_flanders_renewables(text: str) -> float:
    """Flanders splits renewables across green energy + cogeneration.

    Layout:
        Coûts énergie verte (c€/kWh)
        Coûts cogénération (c€/kWh)
        FL
        <green>
        <cogen>

    Caller gates on REGION_FLANDERS so a miss is a layout drift, not a
    'no levy on this card' case. Raise rather than silently zero.
    """
    match = re.search(
        rf"Coûts énergie verte.*?Coûts cogénération.*?FL\s*\n?\s*"
        rf"({_NUM})\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match:
        return (to_float(match.group(1)) + to_float(match.group(2))) / 100.0
    # Some fixed cards may print only the green-energy line.
    fallback = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?FL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if fallback is None:
        raise ExtractorError(
            "Luminus: Flanders renewables (Coûts énergie verte) row not found"
        )
    return to_float(fallback.group(1)) / 100.0


def _extract_wallonia_renewables(text: str) -> float:
    """Mandatory in Wallonia (caller gates on REGION_WALLONIA); raise on
    miss rather than silently zero out."""
    match = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?WAL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match is None:
        raise ExtractorError(
            "Luminus: Wallonia renewables (Coûts énergie verte) row not found"
        )
    return to_float(match.group(1)) / 100.0


def _extract_flanders_dsos(text: str, kind: TariffKind) -> dict[str, DsoOverlay]:
    """Read the Compteur digital columns from the Flanders DSO table.

    Static cards print 8 numbers per row (digital + classic + prosumer):
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal c€/kWh |
      dist_excl_night | capacity_classic €/yr | dist_classic_normal |
      dist_classic_excl | prosumer €/kW/yr

    Dynamic (SMR3) cards omit the analog-meter and prosumer columns and
    print only 4 numbers:
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal | dist_excl_night

    Distribution already includes transport (same convention as Engie's
    Flanders rows).
    """
    # The dynamic product meters quarter-hourly (SMR3); its data-management
    # fee is the reduced value in the "(**) ... quart d'heure ... gestion
    # des donnees" footnote, not the table's monthly-regime column. Fall
    # back to the table value if the footnote is absent.
    quarter_data_mgmt: float | None = None
    if kind == "dynamic":
        footnote = re.search(
            r"quart d['’]heure[\s\S]{0,80}?gestion des donn[\s\S]{0,40}?([\d,]+)\s*€",
            text,
        )
        if footnote is not None:
            quarter_data_mgmt = to_float(footnote.group(1))

    out: dict[str, DsoOverlay] = {}
    # One VREG figure for the whole region, stated once in a footnote rather
    # than per area, so it goes on every Fluvius overlay this card produces.
    ceiling = parse_vreg_network_ceiling(text)
    for label, key in _FLANDERS_LABELS.items():
        # Eight figures on a static card, four on a dynamic one, which
        # prints neither the analog-meter columns nor the prosumer rate.
        row = numeric_row(text, label, 8) or numeric_row(text, label, 4)
        if not row:
            continue
        nums = [to_float(n) for n in row]
        prosumer: float | None = nums[7] if len(nums) == 8 else None
        out[key] = DsoOverlay(
            distribution_single=nums[2] / 100.0,
            distribution_exclusive_night=nums[3] / 100.0,
            transport=0.0,
            data_management_per_year=(
                quarter_data_mgmt if quarter_data_mgmt is not None else nums[0]
            ),
            capacity_eur_per_kw_year=nums[1],
            prosumer_eur_per_kva_year=prosumer,
            network_ceiling_eur_per_kwh=ceiling,
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static rows have 7 numbers:
      mono | pleines | creuses | excl_nuit | transport | data_mgmt | prosumer
    Dynamic rows have 9:
      mono | pleines | creuses | ECO | MEDIUM | PIC | excl_nuit |
      transport | data_mgmt
    The IMPACT triplet (ECO/MEDIUM/PIC) is unique to dynamic; its
    presence flips the prosumer column off (SMR3 has no compensation
    regime).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        # Nine figures on a dynamic card, which carries the IMPACT triplet,
        # seven on a static one, which carries the prosumer rate instead.
        row = numeric_row(text, label, 9) or numeric_row(text, label, 7)
        if not row:
            continue
        nums = [to_float(n) for n in row]
        eco = medium = pic = None
        if len(nums) == 9:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            # Luminus prints ECO | MEDIUM | PIC in ascending order
            # (different from OCTA+/Bolt where the columns are PIC
            # first, descending). Map to the schema's distribution_*.
            eco, medium, pic = nums[3], nums[4], nums[5]
            excl_night = nums[6]
            transport = nums[7]
            data_mgmt = nums[8]
            prosumer: float | None = None
        elif len(nums) == 7:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            excl_night = nums[3]
            transport = nums[4]
            data_mgmt = nums[5]
            prosumer = nums[6]
        else:
            continue
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=pleines / 100.0,
            distribution_offpeak=creuses / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            distribution_pic=pic / 100.0 if pic is not None else None,
            distribution_medium=medium / 100.0 if medium is not None else None,
            distribution_eco=eco / 100.0 if eco is not None else None,
            transport=transport / 100.0,
            data_management_per_year=data_mgmt,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_FLANDERS_LABELS = FLUVIUS_CARD_LABELS
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "TECTEO RESA": DSO_RESA,
    "WAVRE": DSO_REW,
}


# Numeric token: digits optionally followed by a single decimal separator
# + digits. Anchors on starting + ending digit so a trailing sentence
# punctuation can't be captured (e.g. '0,1019 x Belpex H + 2,4591.\n'
# from luminus_dynamic_w would otherwise grab the final '.' if the
# regex were the lazier '[\d,.]+').
_NUM = r"\d+(?:[,.]\d+)?"
