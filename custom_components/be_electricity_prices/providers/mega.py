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

"""Mega Belgium tariff card extractor.

Mega publishes monthly tariff cards under predictable filenames at:

    https://my.mega.be/resources/tarif/Mega-FR-EL-B2C-<REGION>-<MMYYYY>-<SUFFIX>.pdf

The MMYYYY rolls every month and the product SUFFIX (e.g. ``Smart0104``,
``Smart2204-Fixed``, ``Cap0104``) carries an internal launch-date code
that drifts when Mega launches new product variants. To resolve a stable
``(contract, region)`` pair to its current PDF without hardcoding any
suffix, the extractor scrapes the public listing page at
``mega.be/fr/cartes-tarifaires``: every product card carries a
``data-product-element="<Product Name>"`` anchor pointing at that
month's PDF, so finding the right URL is a simple regex match.

All thirteen residential electricity products are registered. Mega
serves all three regions (Flanders, Wallonia, Brussels) for every
product. The Tarif Social variant is omitted on purpose, same reasoning
as Engie/Luminus (regulated CREG tariff, auto-assigned, no DSO breakdown).

The Dynamic formula uses a different convention than Engie/Luminus:
``Day Ahead Epex Spot * 1.05 + 1.35 c€/kWh`` where the spot is already
in c€/kWh and the result is TVAC, so factor and base are scaled
straight to EUR/kWh without a VAT multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from ..const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA
from ._pdf import USER_AGENT, fetch_pdf_text, parse_valid_until, to_float
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    VariableRates,
)

_LISTING_URL = "https://www.mega.be/fr/cartes-tarifaires"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WL",
    REGION_BRUSSELS: "BX",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    product_name: str  # the data-product-element value Mega uses on its site


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "mega_smart_fixed", "Mega Smart Fixed (2 years)", "fixed", "Smart Fixed"
    ),
    _ContractDef(
        "mega_smart_flex", "Mega Smart Flex (2 years)", "variable", "Smart Flex"
    ),
    _ContractDef("mega_zen_fixed", "Mega Zen Fixed (3 years)", "fixed", "Zen Fixed"),
    _ContractDef("mega_online_fixed", "Mega Online Fixed", "fixed", "Online Fixed"),
    _ContractDef("mega_online_flex", "Mega Online Flex", "variable", "Online Flex"),
    _ContractDef("mega_cosy_fixed", "Mega Cosy Fixed", "fixed", "Cosy Fixed"),
    _ContractDef("mega_cosy_flex", "Mega Cosy Flex", "variable", "Cosy Flex"),
    _ContractDef(
        "mega_offpeak_fixed", "Mega Off-peak Fixed", "fixed", "Off-peak Fixed"
    ),
    _ContractDef(
        "mega_offpeak_flex", "Mega Off-peak Flex", "variable", "Off-peak Flex"
    ),
    _ContractDef("mega_dynamic", "Mega Dynamic", "dynamic", "Dynamic"),
    _ContractDef("mega_cap", "Mega Cap", "variable", "Mega Cap"),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


# ---- listing HTML -> PDF URL --------------------------------------------------


def _find_pdf_url(listing_html: str, product_name: str, region_code: str) -> str | None:
    """Find the current month's electricity PDF URL for product+region.

    The listing HTML structure repeats per product: each `<a data-product-
    element="<Product Name>" ... href="<PDF URL>">` carries an electricity
    or gas link. We pin the regex to ``Mega-FR-EL-B2C-<REGION>-`` so the
    gas links (``Mega-FR-NG-...``) and other-region links don't match.
    """
    pattern = re.compile(
        r'data-product-element="' + re.escape(product_name) + r'"[^>]*?'
        r'href="(https://my\.mega\.be/resources/tarif/'
        r"Mega-FR-EL-B2C-" + region_code + r"-\d{6}-[^\"]+\.pdf)\"",
        re.S,
    )
    match = pattern.search(listing_html)
    return match.group(1) if match else None


async def _fetch_listing_html(session: aiohttp.ClientSession) -> str:
    try:
        async with session.get(
            _LISTING_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                raise ExtractorError(f"HTTP {resp.status} fetching {_LISTING_URL}")
            return await resp.text()
    except aiohttp.ClientError as err:
        raise ExtractorError(f"network error fetching {_LISTING_URL}: {err}") from err


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every ``data-product-element`` value from Mega's listing.

    Best-effort catalog discovery for the daily live-check: the diff
    against ``{c.product_name for c in _CONTRACTS}`` flags any new Mega
    product that should be added to the registry.
    """
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return set()
    return set(re.findall(r'data-product-element="([^"]+)"', listing))


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Mega contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        raise ExtractorError(f"Mega: unknown region {region!r}")

    listing = await _fetch_listing_html(session)
    pdf_url = _find_pdf_url(listing, contract.product_name, region_code)
    if pdf_url is None:
        raise ExtractorError(
            f"Mega {contract_id}: no listing entry for region {region!r}"
        )
    text = await fetch_pdf_text(session, pdf_url)
    return parse_snapshot(contract_id, text, region, pdf_url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _LISTING_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Mega contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise = _extract_federal_excise(text)
    energy_contribution = _extract_energy_contribution(text)
    region_connection_fee = (
        _extract_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        wallonia_renewables = _extract_renewables(text, "Wallonie")
        dsos = _extract_wallonia_dsos(text)
    else:
        brussels_renewables = _extract_renewables(text, "Bruxelles")
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="mega",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            brussels_renewables=brussels_renewables,
            region_connection_fee=region_connection_fee,
            energy_fund_eur_per_month=0.0,
            vat_rate=0.0,
        ),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


# Mega prints two distinct formulas in every Dynamic PDF:
#   - Consumption: "...la formule tarifaire suivante : Day Ahead ... * X + Y c€/kWh"
#     (TVAC; spot is in c€/kWh and result is in c€/kWh)
#   - Injection:   "...la formule suivante (HTVA) : Day Ahead ... * X - Y c€/kWh"
#     (HTVA but injection is VAT-exempt residential, so no scaling needed)
# Both formulas use the en-dash for negative bases - we match it explicitly.
_FORMULA_TAIL = (
    r"Day Ahead [Ee][Pp][Ee][Xx]\s*[Ss][Pp][Oo][Tt](?:\s*Belgium)?\s*\*\s*"
    r"([\d,]+)\s*([+\-–—])\s*([\d,]+)\s*c€/kWh"
)
_CONSUMPTION_FORMULA_RE = re.compile(
    r"formule tarifaire suivante[^*]+?" + _FORMULA_TAIL, re.S
)
_INJECTION_FORMULA_RE = re.compile(
    r"formule suivante\s*\(HTVA\)[^*]+?" + _FORMULA_TAIL, re.S
)


def _parse_formula(match: re.Match[str] | None) -> tuple[float, float] | None:
    if match is None:
        return None
    factor = to_float(match.group(1))
    sign = -1.0 if match.group(2) in ("-", "–", "—") else 1.0
    base_cents = sign * to_float(match.group(3))
    return factor, base_cents / 100.0


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        consumption = _parse_formula(_CONSUMPTION_FORMULA_RE.search(text))
        if consumption is None:
            raise ExtractorError("could not parse Mega dynamic consumption formula")
        # Mega's consumption formula is TVAC and uses spot in c€/kWh, so
        # `factor` already maps EUR/kWh-spot to EUR/kWh-energy directly.
        # Just convert the base cents to EUR.
        factor, base = consumption
        return DynamicRates(
            factor=factor,
            base=base,
            yearly_fixed_fee=yearly_fee,
        )

    mono = _extract_meter_value(text, "Compteur mono-horaire")
    peak = _extract_meter_value(text, "Tarif jour")
    offpeak = _extract_meter_value(text, "Tarif nuit")
    excl_night = _extract_meter_value(text, "Exclusif nuit")
    if mono is None:
        raise ExtractorError(f"could not parse Mega {kind} energy block")
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=yearly_fee,
        )
    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_meter_value(text: str, label: str) -> float | None:
    """Pull the consumption rate that follows a meter-type label.

    Mega prints labels and values on separate lines; the consumption rate
    is the first number after the label and the injection rate is the
    second. Skip both ``Tarif jour`` and ``Tarif nuit`` for products that
    only label them under a parent ``Compteur bi-horaire`` header.
    """
    match = re.search(
        rf"{re.escape(label)}\s*\n\s*([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) / 100.0 if match else None


def _extract_yearly_fee(text: str) -> float:
    match = re.search(r"Redevance fixe\s*\(€/an\)\s*\n\s*([\d.,]+)", text)
    return to_float(match.group(1)) if match else 0.0


def _extract_publication_month(text: str) -> str:
    match = re.search(r"V(\d+\s+[a-zé]+\s+\d{4})", text)
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    """Mega prints injection rates in the same energy block, second column."""
    pattern = re.compile(
        r"Compteur mono-horaire\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)",
    )
    match = pattern.search(text)
    current = to_float(match.group(1)) / 100.0 if match else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    if kind == "dynamic":
        # Distinct anchor: injection is the formula after "(HTVA)".
        # Residential injection is VAT-exempt so the HTVA value is
        # already what the user receives.
        inj_match = _INJECTION_FORMULA_RE.search(text)
        parsed = _parse_formula(inj_match)
        if parsed is not None and inj_match is not None:
            factor, base = parsed
            formula = inj_match.group(0)

    if current is None and factor is None:
        return None
    return InjectionRates(current=current, factor=factor, base=base, formula=formula)


# ---- taxes --------------------------------------------------------------------


def _extract_federal_excise(text: str) -> float:
    """First excise tier (0-3000 kWh), uniform across regions."""
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_energy_contribution(text: str) -> float:
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)",
        text,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_connection_fee(text: str) -> float:
    """Wallonia raccordement (`Redevance de raccordement 0,075` c€/kWh)."""
    match = re.search(r"Redevance de raccordement\s*\n\s*([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else 0.0


def _extract_flanders_renewables(text: str) -> float:
    """Flanders splits renewables between green energy and cogeneration."""
    green = re.search(
        r"Cotisation Verte\s*\(c€/kWh\).{0,400}?Flandre\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    cogen = re.search(
        r"Cotisation\s+(?:Cog[ée]n[ée]ration|cog[ée]n[ée]ration)\s*\(c€/kWh\)"
        r".{0,400}?Flandre\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    total = 0.0
    if green:
        total += to_float(green.group(1))
    if cogen:
        total += to_float(cogen.group(1))
    return total / 100.0


def _extract_renewables(text: str, region_label: str) -> float:
    """Wallonie / Bruxelles - single 'Cotisation Verte' line."""
    match = re.search(
        rf"Cotisation Verte\s*\(c€/kWh\).{{0,400}}?{region_label}\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    return to_float(match.group(1)) / 100.0 if match else 0.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": "fluvius_antwerpen",
    "Fluvius Halle-Vilvoorde": "fluvius_halle_vilvoorde",
    "Fluvius Imewo": "fluvius_imewo",
    "Fluvius Kempen": "fluvius_iveka",
    "Fluvius Limburg": "fluvius_limburg",
    "Fluvius Midden-Vlaanderen": "fluvius_intergem",
    "Fluvius West": "fluvius_west",
    "Fluvius Zenne-Dijle": "fluvius_zenne_dijle",
}


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
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
            text,
        )
        if not match:
            continue
        capacity = to_float(match.group(1))
        dist_normal = to_float(match.group(2))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": "aieg",
    "AIESH": "aiesh",
    "ORES (Brabant wallon)": "ores",
    "RESA": "resa",
    "Régie de Wavre": "rew",
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
            match = re.search(rf"{re.escape(label)}\s*\n\s*([\d.,]+)", prosumer_text)
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
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        terme_fixe = to_float(match.group(5))
        pic = to_float(match.group(6))
        medium = to_float(match.group(7))
        eco = to_float(match.group(8))
        transport = to_float(match.group(9))
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            distribution_pic=pic / 100.0,
            distribution_medium=medium / 100.0,
            distribution_eco=eco / 100.0,
            transport=transport / 100.0,
            data_management_per_year=terme_fixe,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
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
    transport = to_float(match.group(5))
    mesure = to_float(match.group(6))
    return {
        "sibelga": DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            transport=transport / 100.0,
            data_management_per_year=mesure,
        )
    }


EXTRACTOR = SupplierExtractor(
    id="mega",
    label="Mega",
    contracts=tuple(
        Contract(id=c.contract_id, label=c.label, kind=c.kind) for c in _CONTRACTS
    ),
    fetch=fetch,
    dso_keys=(
        tuple(_FLANDERS_LABELS.values())
        + tuple(_WALLONIA_LABELS.values())
        + ("sibelga",)
    ),
)
