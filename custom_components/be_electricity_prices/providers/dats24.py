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

"""DATS 24 (Colruyt subsidiary) tariff extractor.

DATS 24 sells one residential electricity product, "Elektriciteit Groen
Variabel", in Flanders and Wallonia. It's a variable contract indexed
monthly against the BE_spotRLP (Belgian quarter-hourly spot prices,
RLP-weighted) parameter:

    afname  = (BE_spotRLP * 0.1124 + 0.511) * 1.06   c€/kWh   (single rate)
    teruglevering = (BE_spotSPP * 0.0766 - 1.11)     c€/kWh   (VAT-exempt)

The card is published monthly via a stable public API:

    https://profile.dats24.be/api/v1/ratecard?energyType=electricity
        &contractType=variable&language=nl

The endpoint has a JSON-looking URL but actually returns a PDF; the
PDF carries the current-month rates plus the year-estimate (jaarschatting)
values, full Fluvius / Walloon DSO tables, the Flemish GSC + WKC
certificate cost, the Walloon CV cost, and federal taxes. All printed
amounts are TVAC except where the card explicitly notes otherwise --
``vat_rate=0.0`` matches the project's standard convention.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiohttp

from ..const import REGION_FLANDERS, REGION_WALLONIA
from ._pdf import (
    USER_AGENT,
    extract_pdf_text_layout,
    fetch_pdf_text_layout,
    parse_valid_until,
    to_float,
)
from .base import (
    Contract,
    DsoOverlay,
    EnergyRates,
    ExtractorError,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_RATECARD_URL = (
    "https://profile.dats24.be/api/v1/ratecard"
    "?energyType=electricity&contractType=variable&language=nl"
)

_CONTRACT_ID = "dats24_groen_variabel"
_CONTRACT_LABEL = "DATS 24 Elektriciteit Groen Variabel"


_FLANDERS_DSOS: dict[str, str] = {
    "ANTWERPEN": "fluvius_antwerpen",
    "HALLE-VILVOORDE": "fluvius_halle_vilvoorde",
    "IMEWO": "fluvius_imewo",
    "KEMPEN": "fluvius_iveka",
    "LIMBURG": "fluvius_limburg",
    "MIDDEN-VLAANDEREN": "fluvius_intergem",
    "WEST": "fluvius_west",
    "ZENNE-DIJLE": "fluvius_zenne_dijle",
}

# Wallonia DSO labels as they appear on DATS 24's card. Multiple ORES
# sub-areas (Brabant Wallon, Est, Hainaut, Luxembourg, Mouscron, Namur,
# Verviers) all carry identical numbers, so we collapse them onto our
# single "ores" key by picking the Brabant Wallon row.
_WALLONIA_DSOS: tuple[tuple[str, str], ...] = (
    ("AIEG", "aieg"),
    ("AIESH", "aiesh"),
    ("ORES (Brabant Wallon)", "ores"),
    ("RÉGIE DE WAVRE", "rew"),
    ("RESA", "resa"),
)


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown DATS 24 contract {contract_id!r}")
    if region not in (REGION_FLANDERS, REGION_WALLONIA):
        raise ExtractorError(
            "DATS 24 only sells residential electricity in Flanders / Wallonia"
        )
    text = await fetch_pdf_text_layout(session, _RATECARD_URL)
    return parse_snapshot(text, _RATECARD_URL, region)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Confirm the public ratecard endpoint still serves a card.

    DATS 24's rate card is published from a stable URL; the catalog
    "drift" we want to detect is the endpoint disappearing or
    splitting into multiple variants. A 200 + PDF magic bytes is
    enough -- if they ever add a second contract type ("vast", "tou",
    etc.) this check stays green and we'd notice via a separate
    extractor failure rather than a false-positive new-product alert.
    """
    try:
        async with session.head(
            _RATECARD_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return set()
    except aiohttp.ClientError:
        return set()
    return {_CONTRACT_ID}


def parse_snapshot(text: str, source_url: str, region: str) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    return SupplierSnapshot(
        supplier="dats24",
        contract=_CONTRACT_ID,
        energy=_extract_energy(text),
        dsos=_extract_dsos(text, region),
        taxes=_extract_taxes(text),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=_extract_publication(text),
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
    )


# ---- energy ------------------------------------------------------------------


def _extract_energy(text: str) -> EnergyRates:
    """Parse the indicative TVAC c€/kWh values for the current month.

    The card prints four values under "Afname1" -- single rate (mono),
    bi-hourly day, bi-hourly night, and exclusive-night -- computed
    from the previous calendar month's BE_spotRLP applied to the
    contract's coefficients. We use those figures directly rather than
    re-solving the formula: spot data isn't available at parse time
    and the printed values are exactly what the customer's monthly
    invoice settles at.

    Layout on the card (pdfplumber, columns separated by spaces):

        Afname1 (c€/kWh) 12,18 13,48 10,97 10,97
                         single  Day   Night Excl-night

    All values include 6% VAT.
    """
    match = re.search(
        r"Afname1?\s*\(c€/kWh\)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)",
        text,
    )
    if not match:
        raise ExtractorError("could not parse DATS 24 indicative afname row")
    single_c = to_float(match.group(1))
    peak_c = to_float(match.group(2))
    offpeak_c = to_float(match.group(3))
    excl_c = to_float(match.group(4))
    fee_match = re.search(r"VASTE VERGOEDING\s*\(€/jaar\)\s+([\d,.]+)", text)
    yearly_fee = to_float(fee_match.group(1)) if fee_match else 0.0
    return VariableRates(
        current=single_c / 100.0,
        peak=peak_c / 100.0,
        offpeak=offpeak_c / 100.0,
        exclusive_night=excl_c / 100.0,
        yearly_fixed_fee=yearly_fee,
    )


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str, region: str) -> dict[str, DsoOverlay]:
    if region == REGION_FLANDERS:
        return _extract_flanders_dsos(text)
    if region == REGION_WALLONIA:
        return _extract_wallonia_dsos(text)
    return {}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the Flanders Fluvius block (page 2 of the card).

    Each row has ten numeric columns:

        cap_digital | afname_dig | afname_dig_excl_nacht | max_tarief
        cap_classical | afname_class | afname_class_excl_nacht | prosumer
        meteropname_kwartier | meteropname_jaarlijks

    The integration only models digital-meter rates (post-2024 Fluvius
    rollout target); the second four numbers describe the analog-meter
    path which we ignore. Distribution rates are TVAC c€/kWh.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_DSOS.items():
        row = re.search(
            rf"^{re.escape(label)}\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
            rf"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
            rf"([\d,.]+)\s+([\d,.]+)",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(2)) / 100.0,
            transport=0.0,  # rolled into Fluvius distribution on this card
            capacity_eur_per_kw_year=to_float(row.group(1)),
            data_management_per_year=to_float(row.group(10)),
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the Wallonia DSO block (page 3 of the card).

    Each row has ten numeric columns:

        single | day | night | PIC | MEDIUM | ECO | excl_nacht
        transport | data-beheer (€/yr) | prosumer (€/kVA/yr)

    All distribution rates are TVAC c€/kWh; transport is c€/kWh.
    DATS 24 lists seven ORES sub-areas (Brabant Wallon, Est, Hainaut,
    Luxembourg, Mouscron, Namur, Verviers) with identical rates -- we
    collapse them onto the integration's single "ores" key.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_DSOS:
        row = re.search(
            rf"^{re.escape(label)}\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
            rf"([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+"
            rf"([\d,.]+)\s+([\d,.]+)",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(1)) / 100.0,
            distribution_peak=to_float(row.group(2)) / 100.0,
            distribution_offpeak=to_float(row.group(3)) / 100.0,
            distribution_pic=to_float(row.group(4)) / 100.0,
            distribution_medium=to_float(row.group(5)) / 100.0,
            distribution_eco=to_float(row.group(6)) / 100.0,
            transport=to_float(row.group(8)) / 100.0,
            data_management_per_year=to_float(row.group(9)),
            prosumer_eur_per_kva_year=to_float(row.group(10)),
        )
    return out


# ---- taxes -------------------------------------------------------------------


def _extract_taxes(text: str) -> TaxOverlay:
    """Parse the federal + regional tax block.

    The card prints all tax values TVAC (footer: "Alle prijzen ...
    inclusief 6% btw, tenzij anders vermeld"), with two explicit
    exceptions tagged "Niet aan btw onderworpen": the Walloon
    connection fee and the Flemish Energiefonds. Both happen to use
    the same per-kWh / per-month conventions as the other extractors,
    so they slot directly into TaxOverlay without conversion.

    Flemish renewables = GSC + WKC (Vlaams Gewest); Walloon
    renewables = CV (Waals Gewest). Each is a per-kWh certificate
    quota cost the supplier must doorstort.
    """
    contrib_match = re.search(r"Energiebijdrage\s+([\d,.]+)\s*c€/kWh", text)
    excise_match = re.search(
        r"Verbruik tussen 0 kWh en 3\.000 kWh\s+([\d,.]+)\s*c€/kWh", text
    )
    if not contrib_match or not excise_match:
        raise ExtractorError("could not parse DATS 24 federal tax block")

    gsc_match = re.search(r"Vlaams Gewest:\s*GSC\s*\(c€/kWh\)\s+([\d,.]+)", text)
    wkc_match = re.search(r"WKC\s*\(c€/kWh\)\s+([\d,.]+)", text)
    cv_match = re.search(r"Waals Gewest:\s*CV\s*\(c€/kWh\)\s+([\d,.]+)", text)
    flanders_renewables = (
        to_float(gsc_match.group(1)) / 100.0 if gsc_match else 0.0
    ) + (to_float(wkc_match.group(1)) / 100.0 if wkc_match else 0.0)
    wallonia_renewables = to_float(cv_match.group(1)) / 100.0 if cv_match else 0.0

    fund_match = re.search(r"Hoofdverblijf\s*\(domicilie\)\s+([\d,.]+)\s*€/maand", text)
    energy_fund_per_month = to_float(fund_match.group(1)) if fund_match else 0.0
    connection_match = re.search(
        r"Aansluitingsvergoeding Walloni[eë].*?([\d,.]+)\s*c€/kWh", text
    )
    connection_fee = (
        to_float(connection_match.group(1)) / 100.0 if connection_match else 0.0
    )
    return TaxOverlay(
        federal_excise=to_float(excise_match.group(1)) / 100.0,
        energy_contribution=to_float(contrib_match.group(1)) / 100.0,
        flanders_renewables=flanders_renewables,
        wallonia_renewables=wallonia_renewables,
        region_connection_fee=connection_fee,
        energy_fund_eur_per_month=energy_fund_per_month,
        vat_rate=0.0,  # card values are already TVAC
    )


# ---- injection ---------------------------------------------------------------


def _extract_injection(text: str) -> InjectionRates | None:
    """Parse the teruglevering formula and indicative value.

    The card prints both:
      formula:    (BE_spotSPP x 0,0766 - 1,11)         c€/kWh, VAT-exempt
      indicative: Teruglevering2 (c€/kWh) 3,26 ...

    BE_spotSPP is in EUR/MWh on the card; for our model where spot is
    in EUR/kWh, factor scales by 1000 / 100 = 10:
      factor = 0.0766 * 10 = 0.766
      base   = -0.0111 EUR/kWh

    Some users have a single-rate meter, others bi-hourly: the card
    publishes one shared teruglevering value across all three meter
    types, so a single InjectionRates entry covers everyone.
    """
    formula = re.search(
        r"\(BE_spotSPP\s*x\s*([\d,.]+)\s*[-–]\s*([\d,.]+)\)",
        text,
    )
    indicative = re.search(r"Teruglevering2?\s*\(c€/kWh\)\s+([\d,.]+)", text)
    if not formula and not indicative:
        return None
    factor: float | None = None
    base: float | None = None
    formula_text = ""
    if formula:
        factor = to_float(formula.group(1)) * 10.0
        base = -to_float(formula.group(2)) / 100.0
        formula_text = formula.group(0)
    current = to_float(indicative.group(1)) / 100.0 if indicative else None
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula_text,
    )


# ---- publication label -------------------------------------------------------


def _extract_publication(text: str) -> str:
    match = re.search(r"TARIEFKAART\s+(\w+\s+20\d{2})", text, re.IGNORECASE)
    return match.group(1).strip().lower() if match else ""


__all__ = ["EXTRACTOR", "extract_pdf_text_layout", "fetch", "parse_snapshot"]


_DATS24_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="dats24",
    label="DATS 24",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="variable",
            regions=_DATS24_REGIONS,
        ),
    ),
    fetch=fetch,
)
