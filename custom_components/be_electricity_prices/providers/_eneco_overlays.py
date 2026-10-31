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

"""Eneco's network and tax readers, split out of ``eneco.py`` to keep it
under the package's module size limit.

Every card prints both regions' network tables, whichever region it is read
for, and one tax block for the two regions it serves. The number and space
fragments live here as well, since ``eneco.py`` reads its energy and feed-in
rows with the same ones.
"""

from __future__ import annotations

import re

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
    REGION_FLANDERS,
)
from ._parse import excise_tier_bands, numeric_row, ores_sub_areas_split, to_float
from .base import DsoOverlay, ExtractorError, TaxOverlay

# Wallonia DSO labels (column layout: 4 distribution + optional [MEDIUM PIC ECO]
# + transport + databeheer + prosument). ORES sub-zones share one rate since
# 2024, so the first is the canonical "ores" row; a card splitting them leaves
# ORES out (``_extract_dsos``). The cards up to May 2025 print RESA's row as a
# bare "TECTEO", too far from "TECTEO RESA" for the similarity match.
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "TECTEO RESA": DSO_RESA,
    "TECTEO": DSO_RESA,
}

# Flanders Fluvius digital-meter sub-areas (column layout: Normaal,
# Uitsluitend nacht, SMR1 databeheer, SMR3 databeheer, capaciteitstarief,
# then two `-` placeholders). Each sub-area has its own distribution and
# capacity rates. Transport is not in the row and is set to 0 for Flanders:
# the Flemish Afnametarief already bundles Elia transmission (see
# _find_fluvius_row); the Wallonia Transport-kosten column does not apply.
_FLUVIUS_LABELS: dict[str, str] = {
    "FLUVIUS HALLE VILVOORDE": DSO_FLUVIUS_HALLE_VILVOORDE,
    "FLUVIUS ANTWERPEN": DSO_FLUVIUS_ANTWERPEN,
    "FLUVIUS IMEWO": DSO_FLUVIUS_IMEWO,
    "FLUVIUS LIMBURG": DSO_FLUVIUS_LIMBURG,
    "FLUVIUS WEST": DSO_FLUVIUS_WEST,
    "FLUVIUS MIDDEN VLAANDEREN (INTERGEM)": DSO_FLUVIUS_INTERGEM,
    "FLUVIUS KEMPEN (IVEKA)": DSO_FLUVIUS_IVEKA,
    "FLUVIUS ZENNE DIJLE": DSO_FLUVIUS_ZENNE_DIJLE,
}

# Every card prints both regions' tables, whichever region it is read for.
_AREAS = (*_WALLONIA_LABELS.values(), *_FLUVIUS_LABELS.values())

# A Belgian decimal number. Two forms: a thousands-grouped value using a
# non-breaking / thin / narrow-no-break space (the separators to_float
# strips, and the only ones Eneco's PDFs use for grouping: columns are
# separated by ordinary spaces, so grouping on those is unambiguous), or
# an ungrouped run of digits. The previous (\d{1,3}...) capped the
# integer part at three digits, so any value >= 1000 (e.g. a four-digit
# yearly fee) was truncated to its first 1.xxx and mis-parsed.
_NUM = r"(\d{1,3}(?:[\xa0\u2009\u202f]\d{3})+(?:,\d{1,4})?|\d+(?:[\.,]\d{1,4})?)"
_WS = r"[\s\xa0]"
# Horizontal whitespace only: the space variants pypdf prints between two
# figures on one row, and never a newline.
_HWS = r"[ \t\xa0\u2009\u202f]"


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Both regions' tables, which every card prints whichever it is read for."""
    out: dict[str, DsoOverlay] = {}
    for pdf_label, key in _WALLONIA_LABELS.items():
        if key in out:
            continue
        row = _find_wallonia_row(text, pdf_label)
        if row is not None:
            out[key] = row
    if ores_sub_areas_split(text):
        out.pop(DSO_ORES, None)
    for pdf_label, key in _FLUVIUS_LABELS.items():
        row = _find_fluvius_row(text, pdf_label)
        if row is not None:
            out[key] = row
    return out


def _find_wallonia_row(text: str, label: str) -> DsoOverlay | None:
    """Wallonia rows carry 7 (Power Dynamic) or 10 (Power Fix) numbers.

    Layout: Enkelvoudig | Dag | Nacht | Uitsl. nacht | [MEDIUM PIC ECO] |
            Transport | Databeheer (€/jaar) | Prosument (€/kVA/jaar)
    """
    # Ten figures where the card prints the Tarif Impact triplet (Power
    # Fix), seven where it does not (Power Dynamic).
    groups = numeric_row(text, label, 10) or numeric_row(text, label, 7)
    if groups is None:
        return None
    # When Eneco prints the Tarif Impact triplet (Power Fix layout) the
    # row carries 10 columns; Power Dynamic only has 7. Eneco's column
    # order is MEDIUM | PIC | ECO (different from OCTA+/Bolt where it's
    # PIC | MEDIUM | ECO).
    medium = pic = eco = None
    if len(groups) == 10:
        medium = to_float(groups[4]) / 100.0
        pic = to_float(groups[5]) / 100.0
        eco = to_float(groups[6]) / 100.0
    return DsoOverlay(
        distribution_single=to_float(groups[0]) / 100.0,
        distribution_peak=to_float(groups[1]) / 100.0,
        distribution_offpeak=to_float(groups[2]) / 100.0,
        distribution_exclusive_night=to_float(groups[3]) / 100.0,
        distribution_pic=pic,
        distribution_medium=medium,
        distribution_eco=eco,
        transport=to_float(groups[-3]) / 100.0,
        data_management_per_year=to_float(groups[-2]),
        prosumer_eur_per_kva_year=to_float(groups[-1]),
    )


def _find_fluvius_row(text: str, label: str) -> DsoOverlay | None:
    """Fluvius digital-meter rows: 5 numbers + 2 placeholder dashes.

    Layout: Normaal | Uitsl. nacht | SMR1 (€/jaar) | SMR3 (€/jaar) |
            Capaciteitstarief (€/kW/jaar) | -- | --

    The Flemish Afnametarief already bundles Elia transmission, so
    transport is 0 here - same convention as the Engie and Luminus
    Flanders rows. The Wallonia "Transport-kosten" column does not apply.
    """
    # Bound to the digital-meter section: the analoge meter table below
    # repeats every label and is the same five figures wide, so an unbounded
    # lookup could bill a digital meter on the analog row.
    row = numeric_row(text, label, 5, after="DIGITALE METER", before="ANALOGE METER")
    if row is None:
        return None
    return DsoOverlay(
        distribution_single=to_float(row[0]) / 100.0,
        # Post-capacity-tariff Flemish meters bill at a single rate, so
        # peak / off-peak don't apply; the Uitsl. nacht column (the second)
        # is the dedicated exclusive-night meter circuit rate.
        distribution_peak=None,
        distribution_offpeak=None,
        distribution_exclusive_night=to_float(row[1]) / 100.0,
        transport=0.0,
        data_management_per_year=to_float(row[3]),
        capacity_eur_per_kw_year=to_float(row[4]),
    )


def _extract_taxes(text: str, region: str) -> TaxOverlay:
    tier_match = re.search(
        rf"(?:Verbruik tussen{_WS}*\n*{_WS}*0{_WS}+en{_WS}+3\.000{_WS}+kWh|Alle verbruik){_WS}*\n*{_WS}*"
        + _NUM
        # The optional second figure is the energy contribution printed on
        # the SAME row. Horizontal whitespace only: with a class that matched
        # a newline, the first figure on the line below the excise would be
        # read as a contribution the law folded into the excise on
        # 2026-08-01, and the WKK levy is exactly one line below on the
        # September 2026 card.
        + rf"(?:{_HWS}+"
        + _NUM
        + r")?",
        text,
    )
    if not tier_match:
        raise ExtractorError("could not parse Eneco federal excise block")
    excise = to_float(tier_match.group(1)) / 100.0
    contribution = to_float(tier_match.group(2)) / 100.0 if tier_match.group(2) else 0.0

    # Anchor on the "(€cent/kWh)" unit token (like the connection fee
    # below) rather than the first number after the label: the sibling
    # rows already carry "(2)(4)" footnote markers, so a lazy {_NUM}
    # would capture a footnote digit if these rows ever gain one too.
    wkk = re.search(
        rf"Bijdrage groene stroom en WKK.+?\(€cent/kWh\){_WS}*\n?{_WS}*{_NUM}",
        text,
        re.S,
    )
    wallonia_renewables = re.search(
        rf"Bijdrage groene stroom Wallonië.+?\(€cent/kWh\){_WS}*\n?{_WS}*{_NUM}",
        text,
        re.S,
    )
    connection = re.search(
        rf"Aansluitingsvergoeding elektriciteit.+?"
        rf"\(€cent/kWh\){_WS}*\n?{_WS}*{_NUM}",
        text,
        re.S,
    )
    if not connection:
        # The Walloon connection fee row is on every Eneco card and is a
        # mandatory all-in component for Walloon customers; unlike the
        # renewables (gated in live_check) nothing else catches it, so a
        # silent 0.0 would under-bill. Fail loud, matching the excise tier.
        raise ExtractorError(
            "could not parse Eneco Walloon connection fee (Aansluitingsvergoeding)"
        )
    # Flanders only, gated here rather than at billing time: the table is
    # headed "Bijdrage Energiefonds (Vlaanderen)" but rides on the one card
    # Eneco serves to both regions, and fees.py bills 12 x this field with no
    # region check of its own. Every issue since January 2025 prints 0,00 in
    # the domiciled low-voltage cell, so nothing is mis-billed today; the gate
    # is what keeps a Walloon entry right the month that changes.
    fund = (
        # _WS already matches a newline, so this reads the label whether or
        # not the cell wraps. Requiring one tied the read to a layout Eneco
        # changed: only the Fix and Flex cards issued from January 2026 wrap
        # here, so 45 of the 63 cards published since January 2025 fell
        # through to the 0.0 default with nothing to say so.
        re.search(
            rf"Standaard tarief{_WS}*\(domicilieadres\){_WS}+{_NUM}",
            text,
        )
        if region == REGION_FLANDERS
        else None
    )
    return TaxOverlay(
        federal_excise=excise,
        federal_excise_bands=excise_tier_bands(
            text, excise, start=tier_match.start(), within=tier_match.end()
        ),
        energy_contribution=contribution,
        flanders_renewables=to_float(wkk.group(1)) / 100.0 if wkk else 0.0,
        wallonia_renewables=(
            to_float(wallonia_renewables.group(1)) / 100.0
            if wallonia_renewables
            else 0.0
        ),
        region_connection_fee=to_float(connection.group(1)) / 100.0,
        energy_fund_eur_per_month=to_float(fund.group(1)) if fund else 0.0,
        vat_rate=0.0,
    )
