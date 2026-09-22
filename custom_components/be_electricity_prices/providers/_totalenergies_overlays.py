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

"""TotalEnergies: the regulated legs its card reprints.

The distribution and transport tariffs, the federal excise bands, the energy
contribution and the regional levies. None of it is the supplier's own price:
it is the regulator's, reprinted on the supplier's card, and it changes on the
regulator's calendar rather than the product's.
"""

from __future__ import annotations

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    DSO_SIBELGA,
    FLUVIUS_CARD_LABELS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
)
from ._parse import numeric_row, parse_brussels_osp, to_float
from .base import (
    DsoOverlay,
    ExtractorError,
    brussels_sibelga_overlay,
    walloon_dso_overlay,
)
import re


def _extract_fee_and_renewables(text: str) -> tuple[float, float]:
    """Pull the (yearly_fee_eur, renewables_eur_per_kwh) pair.

    TotalEnergies prints them on a dedicated 2-number line in the energy
    block: ``90,00 1,57``. The position varies per contract (after the
    consumption row for variable/dynamic, between Tarif annuel and
    Injection for static), but every layout precedes the line with a
    ``Tarif (mensuel|annuel)`` header. Anchor on that header and require
    the following 2-number line; this rejects unrelated value pairs that
    happen to share the shape (e.g. footer rows).

    Both numbers are mandatory on every TE residential card (~90 EUR/yr
    yearly fee; regional renewables surcharge between 1.6 and 3.2
    c€/kWh). Raise on miss so a layout drift surfaces as an extractor
    failure instead of silently dropping ~90 EUR/year and the regional
    renewables levy from the bill.
    """
    match = re.search(
        r"Tarif\s+(?:mensuel|annuel)[\s\S]{0,400}?"
        r"^(\d{2,3}[.,]\d{2})\s+(\d[.,]\d{1,3})\s*$",
        text,
        re.MULTILINE,
    )
    if not match:
        raise ExtractorError("TotalEnergies: yearly fee + renewables row not found")
    return to_float(match.group(1)), to_float(match.group(2)) / 100.0


def _extract_federal_excise(text: str) -> float:
    """First excise tier (0-3000 kWh).

    Mandatory on every Belgian residential card with no fallback; a miss
    is a layout drift that would silently undercount the bill by ~5
    c€/kWh. Raise rather than default to 0.
    """
    match = re.search(
        r"Consommation entre 0 et 3\.000 kWh\s+([\d.,]+)",
        text,
    )
    if match is None:
        raise ExtractorError(
            "TotalEnergies: federal excise (0-3.000 kWh tier) not found"
        )
    return to_float(match.group(1)) / 100.0


def _extract_energy_contribution(text: str) -> float | None:
    """The labelled "Cotisation sur l'énergie" line, or None when absent.

    Returns ``None`` (not ``0.0``) on a miss so the caller can tell a card
    that omits the row from one that prints a genuine zero: the federal
    levy fell to zero on 2026-08-01, so a zero is now a real value and
    must not trigger the DSO-table fallback or the drift error.
    """
    match = re.search(r"Cotisation sur l[\"'’]\s*énergie\s+([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else None


def _energy_contribution_from_table(text: str, region: str) -> float | None:
    """Fallback: read the federal energy contribution from the DSO table.

    The Wallonia card prints "Cotisation sur l'énergie <value>" on a
    labelled line that _extract_energy_contribution catches. The
    Brussels and Flanders cards wrap that header across two lines, so
    the only machine-readable copy of the value is the cotisation
    column of the DSO table: the 7th SIBELGA number on Brussels, the
    8th of nine on each Flanders Fluvius row. It is a federal levy,
    identical across rows, so any one row yields it. Returns ``None`` when
    the layout doesn't expose it. Without this the Brussels / Flanders
    all-in price silently drops the contribution (~0.20 c€/kWh).
    """
    if region == REGION_BRUSSELS:
        row = numeric_row(text, "SIBELGA", 7)
        return to_float(row[6]) / 100.0 if row else None
    if region == REGION_FLANDERS:
        for label in _FLANDERS_LABELS:
            row = numeric_row(text, label, 9)
            if row:
                return to_float(row[7]) / 100.0
    return None


def _extract_energy_fund(text: str) -> float:
    """Flanders 'Cotisations Fonds Energie' line, principal-with-domicile entry."""
    match = re.search(
        r"Résidence principale\s+sans\s+tarif\s+social\s+([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) if match else 0.0


def _extract_renewables(text: str) -> float:
    """The renewables value is the second number on the fee+renewables line.

    Each PDF is region-specific, so we just pick the value next to the
    yearly fee; the caller's region is not needed to disambiguate.
    """
    _, renewables = _extract_fee_and_renewables(text)
    return renewables


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Flanders Fluvius rows (9 numbers each).

    Layout:
      dist_digital_mono | capacity_digital | dist_classic_mono |
      dist_classic_excl_night | data_mgmt_classic | data_mgmt_digital |
      tarif_capacity_max | cotisation_energie | prosumer

    Distribution already includes transport (same convention as
    Engie/Luminus/Mega Flanders).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = numeric_row(text, label, 9)
        if not row:
            continue
        dist_digital = to_float(row[0])
        capacity = to_float(row[1])
        data_mgmt = to_float(row[5])  # digital meter column
        prosumer = to_float(row[8])
        out[key] = DsoOverlay(
            distribution_single=dist_digital / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Wallonia rows (12 numbers each).

    Layout:
      mono | jour | nuit | excl_nuit | PIC | MEDIUM | ECO |
      terme_fixe (€/an) | transport (c€/kWh) | prosumer (€/kVA/an) |
      cap_base | cap_supplementary
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        # Twelve columns, not the ten this reads: every Walloon row ends in
        # two more the card prints and nothing here uses, measured 0,00 on
        # all five DSOs of all three Walloon fixtures. The regex this
        # replaces took the first ten and never noticed the rest, so it
        # would have matched just as happily on a row that had lost one.
        row = numeric_row(text, label, 12)
        if not row:
            continue
        mono = to_float(row[0])
        peak = to_float(row[1])
        offpeak = to_float(row[2])
        excl_night = to_float(row[3])
        pic = to_float(row[4])
        medium = to_float(row[5])
        eco = to_float(row[6])
        terme_fixe = to_float(row[7])
        transport = to_float(row[8])
        prosumer = to_float(row[9])
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


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Brussels Sibelga row (7 numbers) plus the separate power term.

    Layout: mono | jour | nuit | excl_nuit | mesure_comptage (€/an) |
            transport (c€/kWh) | cotisation_energie (c€/kWh)
    The Sibelga <=13kVA fixed power term is printed on its own
    "Terme de puissance mise a disposition" line, not in this row.
    """
    row = numeric_row(text, "SIBELGA", 7)
    if not row:
        return {}
    mono = to_float(row[0])
    peak = to_float(row[1])
    offpeak = to_float(row[2])
    excl_night = to_float(row[3])
    mesure = to_float(row[4])
    transport = to_float(row[5])
    # A Brussels connection also pays the Sibelga power term, printed on a
    # separate "Terme de puissance mise a disposition" line with a band at or
    # below 13 kVA and one above it. Brussels has no separate capacity charge
    # (capacity is Flanders-only), so fold the flat annual euros into the DSO
    # fee, one figure per band: a 3x400 V / 25 A house is 17,3 kVA, so the
    # larger band is residential too. Mandatory on every Brussels card, so
    # raise on a miss.
    power = re.search(
        r"Terme de puissance[\s\S]{0,80}?(?:<=|≤)\s*13\s*kVA\s+([\d.,]+)", text
    )
    if power is None:
        raise ExtractorError("TotalEnergies: Sibelga <=13kVA power term not found")
    fixed_term = to_float(power.group(1))
    above = re.search(r">\s*13\s*kVA\s+([\d.,]+)", text)
    fixed_term_above = to_float(above.group(1)) if above else None
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            transport=transport,
            data_management_per_year=mesure + fixed_term,
            power_term_above_13kva=(
                None if fixed_term_above is None else mesure + fixed_term_above
            ),
            osp_by_tier=parse_brussels_osp(text),
        )
    }


_FLANDERS_LABELS = FLUVIUS_CARD_LABELS
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Namur - Namen)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "RESA SA": DSO_RESA,
}
