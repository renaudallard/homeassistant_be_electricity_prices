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

Cociter publishes monthly tariff cards as ``Tarifs_Elec_*.pdf`` files
linked from https://www.cociter.be/electricite/cartes-tarifaires/.
Cociter rarely raises rates, so the most recently published card is
usually still representative even if its date is older. The extractor:

  1. fetches the index page and picks the most recently dated PDF
     (filenames embed YYYY-MM),
  2. parses the energy formula + single-meter effective rate +
     yearly fee + renewable contribution from that card,
  3. borrows the regulated DSO / transport / federal-tax overlay from
     Eneco's tariff card - those values are set by the regulator and
     do not depend on which energy supplier you have.

The snapshot's ``publication_label`` makes the publication date visible
("(latest published)" suffix) and ``snapshot_age_hours`` exposes the age
to entities. Cociter only sells in Wallonia.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import aiohttp

from . import eneco
from ._pdf import fetch_pdf_text, to_float
from .base import (
    Contract,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_INDEX_URL = "https://www.cociter.be/electricite/cartes-tarifaires/"
_PDF_RE = re.compile(
    r'href="(https?://[^"]*Tarifs_Elec[^"]*?(\d{4}[-_]\d{2})[^"]*\.pdf)"',
    re.IGNORECASE,
)


async def fetch(session: aiohttp.ClientSession, contract_id: str) -> SupplierSnapshot:
    """Fetch + parse Cociter's latest published variable contract card."""
    if contract_id != "cociter_variable":
        raise ExtractorError(f"unknown Cociter contract {contract_id!r}")

    pdf_url, label = await _find_latest_card(session)
    text = await fetch_pdf_text(session, pdf_url)
    energy, renewables = parse_energy_block(text)
    eneco_snap = await eneco.fetch(session, "power_fix")
    return SupplierSnapshot(
        supplier="cociter",
        contract=contract_id,
        energy=energy,
        dsos=eneco_snap.dsos,
        taxes=TaxOverlay(
            federal_excise=eneco_snap.taxes.federal_excise,
            energy_contribution=eneco_snap.taxes.energy_contribution,
            regional_renewables=renewables,
            region_connection_fee=eneco_snap.taxes.region_connection_fee,
            vat_rate=0.0,
        ),
        source_url=pdf_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=f"{label} (latest published)",
    )


def parse_energy_block(text: str) -> tuple[VariableRates, float]:
    """Pure parser exposed for unit tests.

    Returns the single-meter ``VariableRates`` and the regional renewable
    contribution in EUR/kWh.
    """
    rate_match = re.search(
        r"Co[uû]ts? de l[’']énergie\s+([\d,]+)\s*c€/kWh",
        text,
    )
    if not rate_match:
        raise ExtractorError("could not find Cociter single-meter rate")
    fee_match = re.search(
        r'Abonnement annuel "?coop[eé]rateur"?\s+([\d,]+)\s*€/an',
        text,
    )
    formula_match = re.search(
        r"Formule de prix variable\s+TVAc?:\s*([\d,]+)\s*\+\s*([\d,]+)"
        r"\s*x\s*BEL\s*I\s*X",
        text,
        re.IGNORECASE,
    )
    rate_eur = to_float(rate_match.group(1)) / 100.0
    fee = to_float(fee_match.group(1)) if fee_match else 0.0
    formula: str | None = None
    if formula_match:
        formula = (
            f"({formula_match.group(1)} + {formula_match.group(2)} x BELIX) c€/kWh "
            "(VAT-incl)"
        )

    renewables_match = re.search(
        r"Contribution énergie renouvelable[^\n]*?([\d,]+)\s*c€/kWh",
        text,
    )
    renewables = (
        to_float(renewables_match.group(1)) / 100.0 if renewables_match else 0.0
    )
    return (
        VariableRates(current=rate_eur, yearly_fixed_fee=fee, formula=formula),
        renewables,
    )


async def _find_latest_card(
    session: aiohttp.ClientSession,
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

    matches = _PDF_RE.findall(html)
    if not matches:
        raise ExtractorError("no Tarifs_Elec PDF linked on the Cociter cards page")
    matches.sort(key=lambda m: m[1].replace("_", "-"))
    url, label = matches[-1]
    return url, label


EXTRACTOR = SupplierExtractor(
    id="cociter",
    label="Cociter",
    contracts=(
        Contract(id="cociter_variable", label="Cociter Tarif Indexé", kind="variable"),
    ),
    fetch=fetch,
)
