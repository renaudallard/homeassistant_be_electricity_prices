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

"""Cociter (Wallonian citizen cooperative) tariff extractor.

Cociter publishes monthly tariff cards under predictable filenames at
https://www.cociter.be/electricite/cartes-tarifaires/:

    RCVar_YMR_Coop-YYMM-fr.pdf   - variable contract (BELIX-indexed)
    RCDyn_SM3_Coop-YYMM-fr.pdf   - dynamic contract (quarter-hourly BELPEX)

YYMM is e.g. ``2604`` for April 2026. Each card includes the energy
formula plus the full DSO + tax overlay for every Wallonian DSO Cociter
serves (AIEG, AIESH, ORES, RESA, REW). All values are VAT-inclusive.

Cociter only sells in Wallonia.
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
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_INDEX_URL = "https://www.cociter.be/electricite/cartes-tarifaires/"

# Cociter's current monthly publication patterns. The 4-digit group is YYMM.
_VAR_RE = re.compile(
    r'href="(https?://[^"]*RCVar_YMR_Coop-(\d{4})-fr\.pdf)"', re.IGNORECASE
)
_DYN_RE = re.compile(
    r'href="(https?://[^"]*RCDyn_SM3_Coop-(\d{4})-fr\.pdf)"', re.IGNORECASE
)

_DSO_LABELS = ("AIEG", "AIESH", "ORES", "RESA", "REW")
_DSO_KEY = {label: label.lower() for label in _DSO_LABELS}


async def fetch(session: aiohttp.ClientSession, contract_id: str) -> SupplierSnapshot:
    """Fetch + parse Cociter's latest published card for ``contract_id``."""
    if contract_id == "cociter_variable":
        pattern = _VAR_RE
    elif contract_id == "cociter_dynamic":
        pattern = _DYN_RE
    else:
        raise ExtractorError(f"unknown Cociter contract {contract_id!r}")

    pdf_url, label = await _find_latest(session, pattern)
    text = await fetch_pdf_text(session, pdf_url)
    return parse_snapshot(text, contract_id, pdf_url, label)


def parse_snapshot(
    text: str, contract_id: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    energy = _extract_energy(text, contract_id)
    return SupplierSnapshot(
        supplier="cociter",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=publication_label,
    )


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    yearly_fee_match = re.search(r"(\d+,\d+)\s*€/an\s*\n?\s*TVAC", text)
    yearly_fee = to_float(yearly_fee_match.group(1)) if yearly_fee_match else 0.0

    if contract_id == "cociter_variable":
        mono = re.search(r"Compteur monohoraire[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        peak = re.search(r"Heures pleines[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        offpeak = re.search(r"Heures creuses[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        excl = re.search(r"Compteur exclusif nuit[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        if not mono:
            raise ExtractorError(
                "could not parse Cociter variable monohoraire indicative rate"
            )
        formula = re.search(
            r"Compteur monohoraire\s*\(([\d,]+)\s*x\s*BELIX\s*\+\s*([\d,]+)\)",
            text,
        )
        return VariableRates(
            current=to_float(mono.group(1)) / 100.0,
            peak=to_float(peak.group(1)) / 100.0 if peak else None,
            offpeak=to_float(offpeak.group(1)) / 100.0 if offpeak else None,
            exclusive_night=to_float(excl.group(1)) / 100.0 if excl else None,
            yearly_fixed_fee=yearly_fee,
            formula=(
                f"({formula.group(1)} x BELIX + {formula.group(2)}) c€/kWh + 6% VAT"
                if formula
                else None
            ),
        )

    # cociter_dynamic
    formula = re.search(
        r"Compteur SMR3\s*\(([\d,]+)\s*x\s*QUARTER\s*HOURL\s*Y\s*BELPEX\s*\+\s*([\d,]+)\)",
        text,
    )
    if not formula:
        raise ExtractorError("could not parse Cociter dynamic formula")
    factor_pdf = to_float(formula.group(1))
    base_pre_vat_cents = to_float(formula.group(2))
    # PDF formula yields c€/kWh from BELPEX in €/MWh; convert to EUR/kWh
    # against spot already in EUR/kWh: factor *= 10.6, base = base_c * 1.06 / 100.
    return DynamicRates(
        factor=factor_pdf * 10.6,
        base=base_pre_vat_cents * 1.06 / 100.0,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    transport = _extract_transport(text)
    out: dict[str, DsoOverlay] = {}
    for label in _DSO_LABELS:
        row = re.search(
            rf"^{label}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        out[_DSO_KEY[label]] = DsoOverlay(
            distribution_single=to_float(row.group(2)) / 100.0,
            distribution_peak=to_float(row.group(3)) / 100.0,
            distribution_offpeak=to_float(row.group(4)) / 100.0,
            transport=transport,
            data_management_per_year=to_float(row.group(1)),
        )
    return out


def _extract_transport(text: str) -> float:
    match = re.search(r"Tarifs de transport TVAC[^\n]*?([\d,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_taxes(text: str) -> TaxOverlay:
    # The energy block labels the renewable contribution with quoted text:
    #   "énergies renouvelables" ... TVAC <X> c€/kWh
    # PDFs use straight "..." or curly “…” depending on the export; accept any
    # adjacent quote glyph and require the literal heading near the number to
    # avoid silently grabbing some other 'TVAC ... c€/kWh' value.
    renewables = re.search(
        r"[\"'“”«»]?\s*énergies renouvelables"
        r"[\"'“”«»]?.{0,200}?TVAC\s*([\d,]+)\s*c€/kWh",
        text,
        re.S,
    )

    # The "Taxes et redevances" block lists three numbers on one line:
    #   Cotisation énergie | Droit d'accises spécial | Redevance de raccordement
    taxes_block = re.search(
        r"Taxes et redevances.*?([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        text,
        re.S,
    )
    if not taxes_block:
        raise ExtractorError("could not parse Cociter taxes block")

    energy_contrib = to_float(taxes_block.group(1)) / 100.0
    federal_excise = to_float(taxes_block.group(2)) / 100.0
    connection_fee = to_float(taxes_block.group(3)) / 100.0

    # Cociter only operates in Wallonia; Flanders renewables stay at 0.
    return TaxOverlay(
        federal_excise=federal_excise,
        energy_contribution=energy_contrib,
        wallonia_renewables=to_float(renewables.group(1)) / 100.0
        if renewables
        else 0.0,
        region_connection_fee=connection_fee,
        vat_rate=0.0,
    )


async def _find_latest(
    session: aiohttp.ClientSession, pattern: re.Pattern[str]
) -> tuple[str, str]:
    try:
        async with session.get(
            _INDEX_URL,
            headers={"User-Agent": "Home Assistant be_electricity_prices/0.1"},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {_INDEX_URL}")
            html = await resp.text()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {_INDEX_URL}: {err}") from err

    matches = pattern.findall(html)
    if not matches:
        raise ExtractorError(f"no matching tariff card linked at {_INDEX_URL}")
    matches.sort(key=lambda m: m[1])
    url, yymm = matches[-1]
    label = _yymm_to_label(yymm)
    return url, label


def _yymm_to_label(yymm: str) -> str:
    """Convert ``2604`` -> ``2026-04``."""
    if len(yymm) == 4 and yymm.isdigit():
        return f"20{yymm[:2]}-{yymm[2:]}"
    return yymm


EXTRACTOR = SupplierExtractor(
    id="cociter",
    label="Cociter",
    contracts=(
        Contract(
            id="cociter_variable", label="Cociter Tarif Variable", kind="variable"
        ),
        Contract(id="cociter_dynamic", label="Cociter Tarif Dynamique", kind="dynamic"),
    ),
    fetch=fetch,
)
