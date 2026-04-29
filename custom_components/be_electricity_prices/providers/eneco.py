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

"""Eneco Belgium tariff card extractor.

Eneco publishes a stable PDF per contract at predictable URLs:

    https://cdn.eneco.be/downloads/nl/general/tk/BC_032_012604_NL_ENECO_POWER_FIX.pdf
    https://cdn.eneco.be/downloads/nl/general/tk/BC_032_012604_NL_ENECO_POWER_FLEX.pdf
    https://cdn.eneco.be/downloads/nl/general/tk/BC_032_012604_NL_ENECO_POWER_DYNAMIC.pdf

The PDFs are auto-updated monthly and include the publication month
("Tariefkaart april 2026"). All prices are VAT-inclusive (6 %).

Eneco serves Flanders and Wallonia only (no Brussels).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiohttp

from ._pdf import fetch_pdf_text, to_float
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_BASE_URL = "https://cdn.eneco.be/downloads/nl/general/tk"

_CONTRACT_URLS = {
    "power_fix": f"{_BASE_URL}/BC_032_012604_NL_ENECO_POWER_FIX.pdf",
    "power_flex": f"{_BASE_URL}/BC_032_012604_NL_ENECO_POWER_FLEX.pdf",
    "power_dynamic": f"{_BASE_URL}/BC_032_012604_NL_ENECO_POWER_DYNAMIC.pdf",
}

# DSO row label as printed in the PDF -> integration DSO key.
# Eneco prints multiple Fluvius and ORES sub-areas; we keep the first one
# encountered as the canonical row for the top-level DSO key.
_DSO_LABEL_TO_KEY: dict[str, str] = {
    "AIEG": "aieg",
    "AIESH": "aiesh",
    "ORES (Brabant Wallon)": "ores",
    "REGIE DE WAVRE": "rew",
    "TECTEO RESA": "resa",
    "FLUVIUS ANTWERPEN": "fluvius",
}

_NUM = r"(\d{1,3}(?:[\.,]\d{1,4})?)"
_WS = r"[\s\xa0]"


async def fetch(session: aiohttp.ClientSession, contract_id: str) -> SupplierSnapshot:
    """Fetch and parse the Eneco tariff card for ``contract_id``."""
    if contract_id not in _CONTRACT_URLS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    url = _CONTRACT_URLS[contract_id]
    text = await fetch_pdf_text(session, url)
    return parse_snapshot(text, contract_id, url)


def parse_snapshot(text: str, contract_id: str, source_url: str) -> SupplierSnapshot:
    """Parse already-extracted PDF text. Exposed for unit tests."""
    if contract_id not in _CONTRACT_URLS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    return SupplierSnapshot(
        supplier="eneco",
        contract=contract_id,
        energy=_extract_energy(text, contract_id),
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=_extract_publication_month(text),
    )


def _extract_publication_month(text: str) -> str:
    match = re.search(r"Tariefkaart\s+([a-zA-Z]+\s+\d{4})", text)
    return match.group(1) if match else ""


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    if contract_id == "power_fix":
        return _extract_fixed(text)
    if contract_id == "power_flex":
        return _extract_variable(text)
    if contract_id == "power_dynamic":
        return _extract_dynamic(text)
    raise ExtractorError(f"unknown contract {contract_id!r}")


def _extract_fixed(text: str) -> FixedRates:
    pattern = re.compile(
        r"DAG\s+NACHT\s*\n*"
        rf"\s*{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        raise ExtractorError("could not parse Eneco fixed energy block")
    yearly_fee, single, day, night, exclusive = (
        to_float(match.group(i)) for i in range(1, 6)
    )
    return FixedRates(
        single=single / 100.0,
        peak=day / 100.0,
        offpeak=night / 100.0,
        exclusive_night=exclusive / 100.0,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_variable(text: str) -> VariableRates:
    yearly_fee_match = re.search(
        r"\(€/jaar\)\s+VERBRUIK[^\n]*\n[^\n]*\n[^\n]*\n[^\n]*\n\s*" + _NUM,
        text,
        re.S,
    )
    monthly_match = re.search(rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+Maandprijs", text)
    formula_match = re.search(r"\((0,\d+)\s*X\s*BELPEX[\w\-]+\s*\+\s*(\d+,\d+)\)", text)
    if not yearly_fee_match or not monthly_match:
        raise ExtractorError("could not parse Eneco variable energy block")
    return VariableRates(
        current=to_float(monthly_match.group(1)) / 100.0,
        yearly_fixed_fee=to_float(yearly_fee_match.group(1)),
        formula=formula_match.group(0) if formula_match else None,
    )


def _extract_dynamic(text: str) -> DynamicRates:
    yearly_fee_match = re.search(
        r"Enkelvoudige meter\s*\n\s*" + _NUM,
        text,
    )
    formula_match = re.search(
        r"\((0,\d+)\s*X\s*BELPEX[\w\-]+\s*\+\s*(\d+(?:,\d+)?)\)\s*X\s*1,06",
        text,
    )
    if not yearly_fee_match or not formula_match:
        raise ExtractorError("could not parse Eneco dynamic energy block")
    factor = to_float(formula_match.group(1))
    base_pre_vat_cents = to_float(formula_match.group(2))
    base_eur_per_kwh = base_pre_vat_cents / 100.0 * 1.06
    factor_with_vat = factor * 1.06 / 100.0
    return DynamicRates(
        factor=factor_with_vat,
        base=base_eur_per_kwh,
        yearly_fixed_fee=to_float(yearly_fee_match.group(1)),
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    out: dict[str, DsoOverlay] = {}
    for pdf_label, key in _DSO_LABEL_TO_KEY.items():
        if key in out:
            continue
        row = _find_dso_row(text, pdf_label)
        if row is None:
            continue
        out[key] = row
    return out


def _find_dso_row(text: str, label: str) -> DsoOverlay | None:
    escaped = re.escape(label)
    pattern = re.compile(
        rf"{escaped}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}"
        rf"(?:\s+{_NUM}\s+{_NUM}\s+{_NUM})?\s+{_NUM}\s+{_NUM}\s+{_NUM}",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        return None
    groups = [g for g in match.groups() if g is not None]
    single = to_float(groups[0]) / 100.0
    day = to_float(groups[1]) / 100.0
    night = to_float(groups[2]) / 100.0
    transport = to_float(groups[-3]) / 100.0
    data_year = to_float(groups[-2])
    return DsoOverlay(
        distribution_single=single,
        distribution_peak=day,
        distribution_offpeak=night,
        transport=transport,
        data_management_per_year=data_year,
    )


def _extract_taxes(text: str) -> TaxOverlay:
    tier_match = re.search(
        rf"Verbruik tussen{_WS}*\n*{_WS}*0{_WS}+en{_WS}+3\.000{_WS}+kWh{_WS}*\n*{_WS}*"
        + _NUM
        + rf"{_WS}+"
        + _NUM,
        text,
    )
    if not tier_match:
        raise ExtractorError("could not parse Eneco federal excise block")
    excise = to_float(tier_match.group(1)) / 100.0
    contribution = to_float(tier_match.group(2)) / 100.0

    wkk = re.search(
        rf"Bijdrage groene stroom en WKK{_WS}+Vlaanderen.{{0,80}}?{_NUM}",
        text,
        re.S,
    )
    wallonia_renewables = re.search(
        rf"Bijdrage groene stroom Wallonië.{{0,80}}?{_NUM}",
        text,
        re.S,
    )
    connection = re.search(
        rf"Aansluitingsvergoeding elektriciteit.+?"
        rf"\(€cent/kWh\){_WS}*\n?{_WS}*{_NUM}",
        text,
        re.S,
    )
    fund = re.search(
        rf"Standaard tarief{_WS}*\n{_WS}*\(domicilieadres\){_WS}+{_NUM}",
        text,
    )
    return TaxOverlay(
        federal_excise=excise,
        energy_contribution=contribution,
        regional_renewables=(
            to_float(wallonia_renewables.group(1)) / 100.0
            if wallonia_renewables
            else (to_float(wkk.group(1)) / 100.0 if wkk else 0.0)
        ),
        region_connection_fee=(
            to_float(connection.group(1)) / 100.0 if connection else 0.0
        ),
        energy_fund_eur_per_month=to_float(fund.group(1)) if fund else 0.0,
        vat_rate=0.0,
    )


EXTRACTOR = SupplierExtractor(
    id="eneco",
    label="Eneco",
    contracts=(
        Contract(id="power_fix", label="Eneco Zon & Wind Vast", kind="fixed"),
        Contract(id="power_flex", label="Eneco Zon & Wind Flex", kind="variable"),
        Contract(
            id="power_dynamic", label="Eneco Zon & Wind Dynamisch", kind="dynamic"
        ),
    ),
    fetch=fetch,
    dso_keys=tuple(set(_DSO_LABEL_TO_KEY.values())),
)
