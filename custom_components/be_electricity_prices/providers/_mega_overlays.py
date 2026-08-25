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

"""Mega grid and levy overlays: the DSO tables and the tax block.

Split out of ``mega.py`` alongside ``_mega_cards.py``. The cut is the call
graph: these are the leaf parsers ``parse_snapshot`` calls to build the
``DsoOverlay`` map and the ``TaxOverlay``, where ``_mega_cards`` holds the ones
that build the energy and injection legs.

``_extract_supplier_prosumer`` sits here despite being Mega's own PV forfait
rather than a regulated rate: it is read from the same DSO table as its
neighbours, and moving it alone would buy a third cross-module import for
nothing.

No behaviour change: every function here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

import re

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
from ._pdf import (
    parse_brussels_osp,
    to_float,
)
from .base import (
    DsoOverlay,
    ExtractorError,
    brussels_sibelga_overlay,
    walloon_dso_overlay,
)


def _extract_energy_fund(text: str, region: str, *, professional: bool) -> float:
    """Flemish energy fund in EUR/month, from the row this contract bills.

    Only the Flemish cards carry the block, so Wallonia and Brussels read 0
    without looking. Inside it the card prints a "Montant de base" that a
    business connection pays, and, on residential cards only, a "Montant
    réduit (résidentiel avec domicile)" that a domiciled household pays
    instead. A professional card simply omits the reduced row.

    A residential contract never falls back to the base amount: if the
    reduced row is missing the levy reads 0, which is what it was before
    this was parsed at all, rather than billing a household the business
    rate. Scoped to a window after the label so "Montant de base" cannot
    match an unrelated table.
    """
    if region != REGION_FLANDERS:
        return 0.0
    block = re.search(r"Fonds Energie.{0,160}", text, re.S)
    if block is None:
        return 0.0
    window = block.group(0)
    if not professional:
        reduced = re.search(
            r"Montant réduit \(résidentiel avec domicile\)\s*\n\s*([\d.,]+)", window
        )
        return to_float(reduced.group(1)) if reduced else 0.0
    base = re.search(r"Montant de base\s*\n\s*([\d.,]+)", window)
    return to_float(base.group(1)) if base else 0.0


def _extract_supplier_prosumer(text: str, region: str, kind: str) -> float | None:
    """Mega's supplier-side compensation-regime PV forfait.

    Cards for prosumers on the compensation regime print a
    "Forfait panneaux solaires (EUR/kVA par mois)" line; 7,63 EUR/kVA
    per month annualises to 91,56 EUR/kVA/an. The figure is TVA 6%
    incl, so it must NOT be VAT-scaled (it is summed raw on top of the
    DSO "Tarif prosumer" column by _compute_prosumer, exactly like the
    Cociter Variable forfait).

    pypdf splits the label and value three ways across the card family
    (value after the label, before it, or with the label line-wrapped),
    so anchor on the "Forfait panneaux" lead-in and take the first
    decimal in the row rather than matching a fixed layout.

    Brussels cards and the Flanders Dynamic card carry no compensation
    regime and omit the line, so a miss there is legitimate. Everywhere
    else (all Wallonia cards and the Flanders non-dynamic cards) the
    forfait is mandatory, so a miss is a layout drift; raise rather than
    silently drop it, the same way the injection and tax parsers do.
    """
    anchor = re.search(r"Forfait\s+panneaux", text)
    if anchor is not None:
        window = text[anchor.start() : anchor.start() + 200]
        value = re.search(r"(\d+[.,]\d+)", window)
        if value is not None:
            return to_float(value.group(1)) * 12.0
    if region == REGION_BRUSSELS or (region == REGION_FLANDERS and kind == "dynamic"):
        return None
    raise ExtractorError("could not parse Mega PV compensation-regime forfait")


_PRO_TIER_RE = re.compile(
    r"Consommation entre\s+([\d.]+)\s+et\s+([\d.]+)\s+kWh\s+([\d.,]+)\s+([\d.,]+)"
)


def _extract_pro_excise_tiers(text: str) -> list[tuple[str, str, str, str]]:
    """The professional card's degressive excise table.

    Each row carries both levies: the tranche's excise and, beside it, the
    energy contribution, which professional cards still print where the
    residential ones folded it into the excise in August 2026.
    """
    tiers = _PRO_TIER_RE.findall(text)
    if not tiers:
        raise ExtractorError("Mega: professional excise tiers not found")
    return tiers


def _extract_federal_excise(text: str) -> float:
    """Federal excise, uniform across regions.

    Two card shapes. Until July 2026 the excise was degressive and printed
    as consumption tiers, of which 0-3000 kWh is the residential one. From
    1 August 2026 the federal scheme folded the separate energy
    contribution into the excise and flattened it, so the card prints a
    single value under an "Accise speciale (c€/kWh)" heading. Mega renders
    that one with a DOT decimal ("4.876") where the tiered rows used
    commas, which to_float handles either way.

    Federal excise is mandatory on every Belgian residential card; a miss
    on both shapes is a layout drift that would silently undercount the
    bill by ~5 c€/kWh (50 EUR/year at 1000 kWh). Raise rather than
    default to 0.
    """
    flat = re.search(
        r"Accise\s+sp[ée]ciale\s*\n?\s*\(c€/kWh\)\s*\n?\s*([\d.,]+)",
        text,
    )
    if flat is not None:
        return to_float(flat.group(1)) / 100.0
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*([\d.,]+)",
        text,
    )
    if match is None:
        raise ExtractorError("Mega: federal excise (0-3000 kWh tier) not found")
    return to_float(match.group(1)) / 100.0


def _extract_energy_contribution(text: str) -> float:
    """Federal energy contribution; same row as the excise.

    The levy went to zero on 2026-08-01 and was folded into the special
    excise, so the August cards drop the row along with the whole tier
    table. An absent row is the abolished levy, not a layout drift:
    return 0 rather than failing the fetch and taking every Mega contract
    offline.
    """
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)",
        text,
    )
    if match is None:
        return 0.0
    return to_float(match.group(1)) / 100.0


def _extract_connection_fee(text: str) -> float:
    """Wallonia raccordement (`Redevance de raccordement 0,075` c€/kWh).

    Called only for Wallonia, where it is a mandatory charge; raise on a
    miss rather than silently zero it (matching the federal block).
    """
    match = re.search(r"Redevance de raccordement\s*\n\s*([\d.,]+)", text)
    if match is None:
        raise ExtractorError("Mega: Wallonia connection fee (raccordement) not found")
    return to_float(match.group(1)) / 100.0


def _extract_flanders_renewables(text: str) -> float:
    """Flanders green-energy + cogeneration surcharge.

    Mega's card combines both into a single "Cotisation Verte (c€/kWh) /
    Certificat vert et Cogénération" line, so the one value already
    includes cogeneration (a separate cogénération row never appears).
    Called only for Flanders, where the surcharge is mandatory; raise on a
    miss rather than silently zero it.
    """
    match = re.search(
        r"Cotisation Verte\s*\(c€/kWh\).{0,400}?Flandre\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    if match is None:
        raise ExtractorError("Mega: Flanders green-energy surcharge not found")
    return to_float(match.group(1)) / 100.0


def _extract_renewables(text: str, region_label: str) -> float:
    """Wallonie / Bruxelles - single 'Cotisation Verte' line.

    Called only for the matching region, where the green-energy levy is
    mandatory; raise on a miss rather than silently zero it.
    """
    match = re.search(
        rf"Cotisation Verte\s*\(c€/kWh\).{{0,400}}?{region_label}\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    if match is None:
        raise ExtractorError(
            f"Mega: {region_label} renewables (Cotisation Verte) not found"
        )
    return to_float(match.group(1)) / 100.0


_FLANDERS_LABELS = FLUVIUS_CARD_LABELS


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Flanders Fluvius rows.

    Static cards print 6 numbers per row (digital + classic bundles):
      capacity_digital | dist_digital_normal | dist_digital_excl_night |
      terme_fixe_classic | dist_classic_normal | dist_classic_excl_night

    Dynamic cards print only the 2 digital-meter numbers, with the
    ``Tarif de gestion des données`` fee surfaced in a separate
    ``18.92 €/an`` line outside the table.

    Distribution rates already include transport ('incluant déjà les
    coûts de transport'), same convention as Engie/Luminus Flanders.
    """
    data_mgmt = 0.0
    data_match = re.search(
        r"Tarif de gestion des données\s*\(€/an[^)]*\).*?(\d+(?:[.,]\d+)?)\s*€",
        text,
        re.S,
    )
    if data_match:
        data_mgmt = to_float(data_match.group(1))
    # Compensation-regime cards also print a Fluvius "Tarif Prosumer"
    # (EUR/kW/an) table whose per-DSO rate is billed on top of any supplier
    # forfait by _compute_prosumer. The distribution table below uses the same
    # "label then value" layout, so scope the match to the "Tarif Prosumer"
    # block (bounded by its footnote) to avoid picking up a distribution rate.
    # Dynamic cards carry no compensation regime and omit the table, so a miss
    # is legitimate.
    prosumer_by_key: dict[str, float] = {}
    prosumer_block = re.search(r"Tarif Prosumer\b.*?(?=\*\s*Le\b|\Z)", text, re.S)
    if prosumer_block:
        block = prosumer_block.group(0)
        for label, key in _FLANDERS_LABELS.items():
            pmatch = re.search(rf"{re.escape(label)}\s*\n\s*(\d+[.,]\d+)", block)
            if pmatch:
                prosumer_by_key[key] = to_float(pmatch.group(1))
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)"
            rf"(?:\s*\n\s*([\d.,]+))?",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        capacity = to_float(match.group(1))
        dist_normal = to_float(match.group(2))
        # Static cards print a third digital column, the exclusive-night
        # distribution rate (lower than normal); dynamic cards stop at
        # two, so capture it optionally and only set it when present.
        excl = match.group(3)
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            distribution_exclusive_night=to_float(excl) / 100.0 if excl else None,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant wallon)": DSO_ORES,
    "RESA": DSO_RESA,
    "Régie de Wavre": DSO_REW,
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Wallonia rows, vertical extraction.

    Layout (9 numbers per row):
      mono | jour | nuit | excl_nuit | terme_fixe (€/an) |
      PIC | MEDIUM | ECO | transport (c€/kWh)
    """
    # Mega lists prosumer rates in a separate small table further down.
    prosumer_by_key: dict[str, float] = {}
    prosumer_block = re.search(
        r"Tarif Prosumer\s*\n\s*\(€/kW/an\).+?(?=\(3\)|$)", text, re.S
    )
    if prosumer_block:
        prosumer_text = prosumer_block.group(0)
        for label, key in _WALLONIA_LABELS.items():
            match = re.search(
                rf"{re.escape(label)}\s*\n\s*([\d.,]+)",
                prosumer_text,
                re.IGNORECASE,
            )
            if match:
                prosumer_by_key[key] = to_float(match.group(1))
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s*\n\s*"
            rf"([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*"
            rf"([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*"
            rf"([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        excl_night = to_float(match.group(4))
        terme_fixe = to_float(match.group(5))
        pic = to_float(match.group(6))
        medium = to_float(match.group(7))
        eco = to_float(match.group(8))
        transport = to_float(match.group(9))
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
            prosumer=prosumer_by_key.get(key),
        )
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Brussels Sibelga, 8-number row.

    Layout: mono | jour | nuit | excl_nuit | transport |
            mesure_comptage (€/an) | terme_fixe_<=13kVA (€/an) |
            terme_fixe_>13kVA (€/an)
    """
    match = re.search(
        r"Sibelga\s*\n\s*"
        r"([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*"
        r"([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
    )
    if not match:
        return {}
    mono = to_float(match.group(1))
    peak = to_float(match.group(2))
    offpeak = to_float(match.group(3))
    excl_night = to_float(match.group(4))
    transport = to_float(match.group(5))
    mesure = to_float(match.group(6))
    # A Brussels connection is billed both the metering fee (mesure_comptage)
    # and the Sibelga power term for its band. Brussels has no separate
    # capacity charge (capacity is Flanders-only), so fold both flat annual
    # euros into data_management_per_year for the <=13kVA band, and carry the
    # >13kVA one (group 8) beside it: a 3x400 V / 25 A house is 17,3 kVA, so
    # the larger band is residential too.
    fixed_term_le13 = to_float(match.group(7))
    fixed_term_above = to_float(match.group(8))
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=excl_night,
            transport=transport,
            data_management_per_year=mesure + fixed_term_le13,
            power_term_above_13kva=mesure + fixed_term_above,
            osp_by_tier=parse_brussels_osp(text),
        )
    }
