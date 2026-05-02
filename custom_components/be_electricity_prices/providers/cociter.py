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
from datetime import UTC, date, datetime

import aiohttp

from ..const import REGION_WALLONIA
from ._pdf import (
    USER_AGENT,
    fetch_pdf_text,
    parse_valid_until,
    text_mentions_month,
    to_float,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_INDEX_URL = "https://www.cociter.be/electricite/cartes-tarifaires/"

# French month names Cociter prints in the validity header. Used by
# fetch_for_month to confirm a CDN-served PDF actually mentions the
# requested month when parse_valid_until missed.
_FR_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


def _text_mentions_month(text: str, year_month: date) -> bool:
    """Validity-anchored cross-check that ``text`` references the
    requested year+month -- delegates to the shared helper so a
    retrospective mention elsewhere in the PDF doesn't masquerade as
    the validity statement."""
    return text_mentions_month(text, year_month, _FR_MONTHS)


# Cociter's current monthly publication patterns. The 4-digit group is YYMM.
_VAR_RE = re.compile(
    r'href="(https?://[^"]*RCVar_YMR_Coop-(\d{4})-fr\.pdf)"', re.IGNORECASE
)
_DYN_RE = re.compile(
    r'href="(https?://[^"]*RCDyn_SM3_Coop-(\d{4})-fr\.pdf)"', re.IGNORECASE
)

_DSO_LABELS = ("AIEG", "AIESH", "ORES", "RESA", "REW")
_DSO_KEY = {label: label.lower() for label in _DSO_LABELS}

_CONTRACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "cociter_variable": _VAR_RE,
    "cociter_dynamic": _DYN_RE,
}


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
) -> SupplierSnapshot:
    """Fetch + parse Cociter's latest published card for ``contract_id``."""
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        raise ExtractorError(f"unknown Cociter contract {contract_id!r}")

    pdf_url, label = await _find_latest(session, pattern)
    text = await fetch_pdf_text(session, pdf_url)
    return parse_snapshot(text, contract_id, pdf_url, label)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Cociter card for a specific (year, month).

    Cociter's listing keeps every monthly card linked under the same
    page. We fetch it once, find the URL whose YYMM suffix matches the
    requested year_month, and parse. Returns None when the listing
    doesn't list the month, the URL 404s, or the PDF doesn't parse -
    the coordinator falls back to the current snapshot as a proxy.
    """
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        return None
    target_yymm = f"{year_month.year % 100:02d}{year_month.month:02d}"
    try:
        async with session.get(
            _INDEX_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                return None
            html = await resp.text()
    except aiohttp.ClientError:
        return None
    pdf_url: str | None = None
    for url, yymm in pattern.findall(html):
        if yymm == target_yymm:
            pdf_url = url
            break
    if pdf_url is None:
        return None
    try:
        text = await fetch_pdf_text(session, pdf_url)
        snap = parse_snapshot(text, contract_id, pdf_url, _yymm_to_label(target_yymm))
    except ExtractorError:
        return None
    if snap.valid_until is not None:
        if (
            snap.valid_until.year != year_month.year
            or snap.valid_until.month != year_month.month
        ):
            return None
    elif not _text_mentions_month(text, year_month):
        # No parsed validity *and* no textual mention of the requested
        # month -- this is what a CDN-substituted current card looks
        # like. Reject so the caller falls back to the proxy snapshot
        # rather than mis-billing past months at the served PDF's
        # rates.
        return None
    return snap


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
) -> str | None:
    """Cheap freshness probe: latest URL for ``contract_id`` from the index.

    Cociter's listing returns no Last-Modified or ETag, so we GET it and
    return the latest matching PDF URL. The URL embeds YYMM so any
    monthly rotation flips the probe key.
    """
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        return None
    try:
        pdf_url, _ = await _find_latest(session, pattern)
    except ExtractorError:
        return None
    return pdf_url


# Family prefix on Cociter's listing -> our registry contract id.
_DISCOVER_FAMILIES = {
    "RCVar_YMR": "cociter_variable",
    "RCDyn_SM3": "cociter_dynamic",
}


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return contract ids visible in Cociter's monthly card index.

    Cociter's listing publishes one PDF per (family, month). Map the
    family prefix (RCVar_YMR / RCDyn_SM3) back to our contract id and
    surface anything else verbatim — that's the new-product signal.
    """
    try:
        async with session.get(
            _INDEX_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                return set()
            html = await resp.text()
    except aiohttp.ClientError:
        return set()
    out: set[str] = set()
    for family in re.findall(
        r"(RC[A-Za-z]+_[A-Za-z0-9]+)_Coop-\d+-(?:fr|nl)\.pdf", html
    ):
        out.add(_DISCOVER_FAMILIES.get(family, family))
    return out


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
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
    )


def _extract_injection(text: str) -> InjectionRates | None:
    """Parse Cociter's injection formula.

    The variable PDF prints ``(0,097 x BELPEX – 2,1)`` (hourly, hTVA).
    The dynamic PDF prints ``(0,097 x QUARTER HOURLY BELPEX – 2,1)``.
    Injection is VAT-exempt for residential.
    """
    formula = re.search(
        r"(?:Tout compteur[^\n]*|Compteur SMR3)\s*"
        r"\(([\d,]+)\s*x\s*(?:QUARTER\s*HOURL\s*Y\s*)?BELPEX\s*"
        r"[–—\-]\s*([\d,]+)\)",
        text,
    )
    if not formula:
        return None
    factor_pdf = to_float(formula.group(1))
    base_pdf_cents = -to_float(formula.group(2))
    return InjectionRates(
        current=None,
        factor=factor_pdf * 10.0,
        base=base_pdf_cents / 100.0,
        formula=formula.group(0),
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
    # Cociter's formula always ends with "+ N% TVA" right after the parens;
    # capture N so the conversion follows whatever VAT the PDF actually applies.
    formula = re.search(
        r"Compteur SMR3\s*\(([\d,]+)\s*x\s*QUARTER\s*HOURL\s*Y\s*BELPEX\s*\+\s*"
        r"([\d,]+)\)\s*\+\s*(\d+)\s*%\s*TVA",
        text,
    )
    if not formula:
        raise ExtractorError("could not parse Cociter dynamic formula")
    factor_pdf = to_float(formula.group(1))
    base_pre_vat_cents = to_float(formula.group(2))
    vat_multiplier = 1.0 + to_float(formula.group(3)) / 100.0
    # PDF formula yields c€/kWh from BELPEX in €/MWh; convert to EUR/kWh
    # against spot already in EUR/kWh: factor *= vat_mult * 10, base = base_c * vat_mult / 100.
    return DynamicRates(
        factor=factor_pdf * vat_multiplier * 10.0,
        base=base_pre_vat_cents * vat_multiplier / 100.0,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the per-DSO row of the Cociter tariff card.

    The variable card has 6 numbers per row:
        yearly | mono | dag | nacht | uitsl_nacht | tarif_prosumer
    The dynamic (SMR3) card has 8, with the prosumer column replaced by
    three Tarif Impact columns (PIC / MEDIUM / ECO) since SMR3 dispenses
    with the compensation regime.

    The first 6 columns are positionally identical between the two cards,
    but column 6 means different things. We discriminate by looking for
    the literal table header "Tarif prosumer" in the document - this is
    robust against future column additions and avoids the previous
    end-of-line anchor that would silently lose the prosumer value if a
    7th column were ever added to the variable card.
    """
    transport = _extract_transport(text)
    has_prosumer_column = "Tarif prosumer" in text
    out: dict[str, DsoOverlay] = {}
    for label in _DSO_LABELS:
        # Variable card: 6 numbers (last column = prosumer).
        # Dynamic card: 8 numbers (last 3 columns = PIC | MEDIUM | ECO).
        row = re.search(
            rf"^{label}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"
            rf"\s+([\d,]+)(?:\s+([\d,]+)\s+([\d,]+))?",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        prosumer_rate = to_float(row.group(6)) if has_prosumer_column else None
        pic = medium = eco = None
        if not has_prosumer_column and row.group(7) and row.group(8):
            pic = to_float(row.group(6)) / 100.0
            medium = to_float(row.group(7)) / 100.0
            eco = to_float(row.group(8)) / 100.0
        out[_DSO_KEY[label]] = DsoOverlay(
            distribution_single=to_float(row.group(2)) / 100.0,
            distribution_peak=to_float(row.group(3)) / 100.0,
            distribution_offpeak=to_float(row.group(4)) / 100.0,
            distribution_pic=pic,
            distribution_medium=medium,
            distribution_eco=eco,
            transport=transport,
            data_management_per_year=to_float(row.group(1)),
            prosumer_eur_per_kva_year=prosumer_rate,
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

    # The "Taxes et redevances" block lists three values on one line:
    #   Cotisation énergie | Droit d'accises spécial | Redevance de raccordement
    # Anchor on the literal label trio so a future footnote/inserted
    # number above the values can't be mistaken for the row.
    taxes_block = re.search(
        r"Cotisation énergie.*?"
        r"Droit d'accises spécial.*?"
        r"Redevance de raccordement.*?"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)",
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
            headers={"User-Agent": USER_AGENT},
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


_COCITER_REGIONS = frozenset({REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="cociter",
    label="Cociter",
    contracts=(
        Contract(
            id="cociter_variable",
            label="Cociter Tarif Variable",
            kind="variable",
            regions=_COCITER_REGIONS,
        ),
        Contract(
            id="cociter_dynamic",
            label="Cociter Tarif Dynamique",
            kind="dynamic",
            regions=_COCITER_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
