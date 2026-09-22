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

"""Engie: the regulated legs its card reprints.

The distribution and transport tariffs for all three regions, the federal
excise bands, the energy contribution and the regional levies. The excise
table in particular is the federal one, and it moved under Engie's feet in
2026 when the contribution was folded into a flat band.
"""

from __future__ import annotations

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    DSO_SIBELGA,
)
from ._parse import numeric_row, parse_sibelga_row, tier_bound_kwh, to_float
from .base import DsoOverlay, ExtractorError, walloon_dso_overlay
import re


def _extract_consumption_renewables(text: str) -> float:
    """Pick the trailing 'Coûts énergie verte' value off the Consommation row.

    The row carries 3 (dynamic) or 5 (fixed/variable) numbers and the last
    one is always the regional renewable surcharge: Flanders cogen + green,
    Wallonia green-energy contribution, or Brussels green-energy levy.

    Mandatory in every region (~1.5-3 c€/kWh); raise on miss so a layout
    drift surfaces as an extractor failure rather than silently dropping
    the levy from the user's bill.
    """
    match = re.search(r"Consommation\(2\)\s+((?:[\d,.]+\s+)+[\d,.]+)", text)
    if not match:
        raise ExtractorError("Engie: Consommation(2) row (renewables) not found")
    nums = match.group(1).split()
    return to_float(nums[-1]) / 100.0


def _extract_federal_excise(
    text: str, *, professional: bool = False
) -> tuple[float, tuple[tuple[float, float], ...] | None]:
    """Federal excise, mandatory across regions.

    Returns ``(rate, bands)``. ``bands`` is None whenever the card prices
    one rate; the coordinator resolves a banded card against the entry's
    annual volume.

    Three card shapes. Until July 2026 the residential excise was degressive
    and printed as four consumption tiers, and the whole table is read: the
    first two tiers carry the same rate so a household under 20.000 kWh is
    billed exactly as before, while one above it was being billed the
    0-3.000 rate on every kWh. That is 5,03288 c€/kWh where the card says
    4,81876 above 20.000 and 4,74668 above 50.000, about 11 EUR a year at
    25.000 kWh and 64 at 50.000, which a heat pump and a car reach.

    From 1 August 2026 the federal scheme folded the separate energy
    contribution into the excise and flattened it, so the residential card
    prints one rate under "Toutes consommations". Try the flat form first: a
    card that carries both would be the tiered one being phased out, and the
    flat row is the authoritative single rate when it is present.

    Mega's residential cards print the same four rows, read by its own
    parser; the claim that they print the 0-3.000 row alone was made here
    from a check whose pattern could not match a bound with a thousands dot,
    so it only ever saw the first row.

    Professional cards kept the schedule the residential ones lost, in
    three bands (0-20.000 / 20.000-50.000 / 50.000-1.000.000 kWh), and the
    card says so: "calcule sur base annuelle, suivant un tarif degressif
    par tranche de consommation". Read the whole table.
    """
    if professional:
        tiers = _EXCISE_TIER_RE.findall(text)
        if not tiers:
            raise ExtractorError("Engie: professional federal excise tiers not found")
        bands = tuple(
            (tier_bound_kwh(upper), to_float(rate) / 100.0)
            for _lower, upper, rate in tiers
        )
        return bands[0][1], bands
    flat = re.search(
        r"Accise\s+f[ée]d[ée]rale[^\n]*\n\s*Toutes\s+consommations\s+([\d,.]+)",
        text,
    )
    if flat:
        return to_float(flat.group(1)) / 100.0, None
    tiers = _EXCISE_TIER_RE.findall(text)
    if not tiers:
        raise ExtractorError("Engie: federal excise (0-3000 kWh tier) not found")
    if len(tiers) < 2:
        # One row is a rate, not a schedule: the card prices every kWh at it.
        return to_float(tiers[0][2]) / 100.0, None
    bands = tuple(
        (tier_bound_kwh(upper), to_float(rate) / 100.0) for _lower, upper, rate in tiers
    )
    return bands[0][1], bands


def _extract_energy_contribution(text: str) -> float:
    """Engie's PDF strips the comma: ``0,20417`` renders as ``020417``.

    Match either shape and reconstruct the decimal value as
    ``0.<digits>``. The regulated rate has 5-6 fractional digits so the
    quantifier ``\\d{4,6}`` covers it without picking up unrelated
    integers.

    The levy went to zero on 2026-08-01 and was folded into the special
    excise, so the August cards drop the row entirely. An absent row is the
    abolished levy, not a layout drift: return 0 rather than failing the
    fetch and taking every Engie contract offline.
    """
    match = re.search(
        r"Cotisation sur l['©]énergie\s+0\s*[,.]?\s*(\d{4,6})",
        text,
    )
    if not match:
        return 0.0
    return float(f"0.{match.group(1)}") / 100.0


def _extract_energy_fund(
    text: str, *, sans_domicile: bool = False, professional: bool = False
) -> float:
    """Flemish energy fund. Optional outside Flanders, so a miss
    legitimately means 'no fund on this card': keep the silent default.

    The residential card prints two sub-cases: 'avec domicile' (0 for most
    products) and 'sans domicile' (a positive fee). The Empty House product
    is created for vacant homes, which by definition have no registered
    domicile, so it bills the 'sans domicile' rate rather than 0.

    The professional card has neither: it prints one 'Professionnel (basse
    tension)' row, which applies to every professional product including
    the Empty House one."""
    if professional:
        match = re.search(r"Professionnel\s+\(basse\s+tension\)\s+([\d,.]+)", text)
        return to_float(match.group(1)) if match else 0.0
    label = "sans" if sans_domicile else "avec"
    match = re.search(
        rf"Résidentiel\s+\({label}\s+domicile\)\s+([\d,.]+)",
        text,
    )
    return to_float(match.group(1)) if match else 0.0


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Compteur digital Fluvius table.

    Static cards include both a digital and an analog meter table; the
    integration only uses the digital one. Distribution rates already
    include transport ('incluant déjà les coûts de transport') so we set
    ``transport=0`` and put the full c€/kWh into ``distribution_single``.
    """
    digital_block = re.search(
        r"Compteur\s+digital(.+?)(?=Compteur\s+analogique|Suppléments)",
        text,
        re.S,
    )
    block_text = digital_block.group(1) if digital_block else text
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = numeric_row(block_text, label, 5)
        if not row:
            continue
        capacity = to_float(row[0])
        dist_normal = to_float(row[1])
        dist_excl = to_float(row[2])
        data_qh = to_float(row[3])
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            # Group 3 is the "tarif-kWh exclusif nuit" column, lower than
            # the single rate; bill a dedicated night meter at it instead
            # of falling back to the day rate.
            distribution_exclusive_night=dist_excl / 100.0,
            transport=0.0,
            data_management_per_year=data_qh,
            capacity_eur_per_kw_year=capacity,
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static-contract rows have 10 numbers (with prosumer column) and
    dynamic-contract rows have 9 (the prosumer column is replaced with
    nothing). Last column is always the c€/kWh transport rate.

    Layout (c€/kWh except where noted):
        single | peak | offpeak | PIC | MEDIUM | ECO | excl_night |
        data_mgmt (€/an) | [prosumer (€/kVA/an)?] | transport
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        # Horizontal whitespace only ([^\S\n] = whitespace minus newline)
        # between the numbers so a greedy match can't span a blank line
        # and pull the next row's (or a footnote's) leading number into
        # this row: that shifted every column right and billed
        # transport at a stray value while dropping the real rate.
        row = re.search(
            rf"^{re.escape(label)}[^\S\n]+((?:[\d,.]+[^\S\n]+){{8,}}[\d,.]+)",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) < 9:
            continue
        prosumer: float | None = None
        if len(nums) >= 10:
            data_mgmt = nums[7]
            prosumer = nums[8]
            transport = nums[9]
        else:
            data_mgmt = nums[7]
            transport = nums[8]
        out[key] = walloon_dso_overlay(
            mono=nums[0],
            peak=nums[1],
            offpeak=nums[2],
            pic=nums[3],
            medium=nums[4],
            eco=nums[5],
            excl_night=nums[6],
            transport=transport,
            terme_fixe=data_mgmt,
            prosumer=prosumer,
        )

    # The card lists ~7 ORES sub-areas (Brab. Wal., Est, Hainaut, ...),
    # numerically identical today; the loop above maps only the
    # "ORES (Brab. Wal.)" row into the single ORES key. Assert the other
    # sub-areas match it so a future sub-area tariff split is caught here
    # rather than silently billing every ORES customer at the Brab. Wal.
    # rate (mirrors the Ecofix ORES guard).
    ores_rows = re.findall(
        r"^ORES\s*\([^)]+\)[^\S\n]+((?:[\d,.]+[^\S\n]+){8,}[\d,.]+)",
        text,
        re.MULTILINE | re.IGNORECASE,
    )
    first = ores_rows[0].split() if ores_rows else None
    for other in ores_rows[1:]:
        if other.split() != first:
            raise ExtractorError(
                "Engie: ORES sub-area tariffs diverged from the first ORES "
                "row; a sub-area split needs an explicit DSO key"
            )
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the Sibelga row off the eight-column table Engie prints.

    A card that does not carry the row yields no Brussels overlay, which is
    what this did before the row reader moved into ``_pdf``.
    """
    overlay = parse_sibelga_row(text)
    return {} if overlay is None else {DSO_SIBELGA: overlay}


_EXCISE_TIER_RE = re.compile(
    r"Consommation entre\s+([\d.]+)\s+et\s+([\d.]+)\s+kWh\s+([\d,.]+)"
)


_FLANDERS_LABELS: dict[str, str] = {
    "FLUVIUS ANTWERPEN": DSO_FLUVIUS_ANTWERPEN,
    "FLUVIUS HALLE-VILVOORDE": DSO_FLUVIUS_HALLE_VILVOORDE,
    "FLUVIUS IMEWO": DSO_FLUVIUS_IMEWO,
    "FLUVIUS KEMPEN": DSO_FLUVIUS_IVEKA,
    "FLUVIUS LIMBURG": DSO_FLUVIUS_LIMBURG,
    "FLUVIUS MIDDEN-VLAANDEREN": DSO_FLUVIUS_INTERGEM,
    "FLUVIUS WEST": DSO_FLUVIUS_WEST,
    "FLUVIUS ZENNE-DIJLE": DSO_FLUVIUS_ZENNE_DIJLE,
}
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brab. Wal.)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "TECTEO - RESA": DSO_RESA,
}
