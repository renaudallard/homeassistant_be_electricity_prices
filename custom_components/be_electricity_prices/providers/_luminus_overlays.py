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

import re

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    FLUVIUS_CARD_LABELS,
)
from ._parse import (
    excise_tier_bands,
    numeric_row,
    parse_vreg_network_ceiling,
    to_float,
)
from ._rates import TariffKind
from .base import DsoOverlay, ExtractorError


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


def _extract_excise_bands(
    text: str, excise: float
) -> tuple[tuple[float, float], ...] | None:
    """The degressive excise the footnote prints, or None for one rate.

    Until July 2026 the residential excise was a schedule by annual volume,
    and the tax block prints only its first tier: a household above 20.000
    kWh was billed 5,0329 on every kWh where the footnote says 4,8188 above
    it, the way Engie's and Mega's cards print it in full. Read as bands, the
    resolver blends them over the entry's volume. The footnote reads "(**)
    Tarif different dependant de la consommation sur base annuelle : 0-3.000
    kWh : 5,0329 c€/kWh, 3.001-20.000 kWh : 5,0329 c€/kWh, 20.001-50.000 kWh :
    4,8188 c€/kWh" until July 2026, and ">= 0 kWh : 4,8760 c€/kWh" once the
    scheme went flat.

    The first tier has to be the rate the block prints, or the footnote is
    not this card's excise and the card has drifted. A footnote whose tiers
    all carry one rate, as from August 2026, is one rate rather than a
    schedule, and is left None so the law's excise still applies to it.
    """
    start = text.find("Tarif différent dépendant de la consommation")
    if start < 0:
        return None
    bands = excise_tier_bands(text, None, start=start)
    if bands is not None and abs(bands[0][1] - excise) > 1e-9:
        raise ExtractorError(
            f"Luminus: excise footnote starts at {bands[0][1]}, the block prints {excise}"
        )
    return bands


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
      mono | pleines | creuses | <Impact triplet> | excl_nuit |
      transport | data_mgmt
    The IMPACT triplet takes the prosumer column's place on the SMR3
    cards. Where such a card still bills the prosumer tariff (SmartFlex's
    footnote does), ``luminus._with_sibling_prosumer`` fills it from a
    sibling card. Its band order
    is read from the headings: cards up to September 2026 print
    ECO | MEDIUM | PIC, October 2026 turned it round to PIC | MEDIUM | ECO
    with the same figures.
    """
    out: dict[str, DsoOverlay] = {}
    order: tuple[str, ...] | None = None
    for label, key in _WALLONIA_LABELS.items():
        # Nine figures on an SMR3 card, which carries the IMPACT triplet,
        # seven on a static one, which carries the prosumer rate instead.
        row = numeric_row(text, label, 9) or numeric_row(text, label, 7)
        if not row:
            continue
        nums = [to_float(n) for n in row]
        eco = medium = pic = None
        if len(nums) == 9:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            if order is None:
                order = _impact_order(text)
            bands = dict(zip(order, nums[3:6], strict=True))
            eco, medium, pic = bands["eco"], bands["medium"], bands["pic"]
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


_IMPACT_HEADINGS_RE = re.compile(r"Tarif\s+Impact\b([\s\S]{0,200}?)Exclusif\s+nuit")
_IMPACT_BAND_RE = re.compile(r"Heures\s+(ECO|MEDIUM|PIC)\b")


def _impact_order(text: str) -> tuple[str, ...]:
    """Return the Impact bands in the order the card prints their columns.

    Each heading may be followed by its hours on lines of their own
    ("Heures PIC / 17h-22h / Heures MEDIUM / ..."), so only the band
    names between "Tarif Impact" and the next column heading count.
    """
    head = _IMPACT_HEADINGS_RE.search(text)
    order = (
        tuple(m.group(1).lower() for m in _IMPACT_BAND_RE.finditer(head.group(1)))
        if head is not None
        else ()
    )
    if sorted(order) != ["eco", "medium", "pic"]:
        raise ExtractorError("Luminus: Impact column headings not found")
    return order


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
