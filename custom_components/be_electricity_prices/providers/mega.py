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
``mega.be/fr/energie/cartes-tarifaires``: every product card carries a
``data-product-element="<Product Name>"`` anchor pointing at that
month's PDF, so finding the right URL is a simple regex match.

All eleven residential electricity products are registered. Mega
serves all three regions (Flanders, Wallonia, Brussels) for every
product except Off-peak Impact, which is Wallonia-only because it
requires the CWaPE Tarif réseau IMPACT plus an SMR3 smart meter
(both Wallonia-specific). The Tarif Social variant is omitted on
purpose, same reasoning as Engie/Luminus (regulated CREG tariff,
auto-assigned, no DSO breakdown).

The Dynamic formula uses a different convention than Engie/Luminus:
``Day Ahead Epex Spot * 1.05 + 1.35 c€/kWh`` where the spot is already
in c€/kWh and the result is TVAC, so factor and base are scaled
straight to EUR/kWh without a VAT multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import aiohttp

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
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    FR_MONTHS,
    SIGN_CHARS,
    archive_validity_check,
    end_of_month,
    fetch_pdf_text,
    fetch_text,
    parse_brussels_osp,
    parse_sign,
    parse_valid_until,
    to_float,
    vat_multiplier,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    VariableRates,
)

_LISTING_URL = "https://www.mega.be/fr/energie/cartes-tarifaires"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WL",
    REGION_BRUSSELS: "BX",
}


_MEGA_ALL_REGIONS: frozenset[str] = frozenset(
    {REGION_FLANDERS, REGION_WALLONIA, REGION_BRUSSELS}
)


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    product_name: str  # the data-product-element value Mega uses on its site
    # Regions the product is actually published in. Defaults to all
    # three; Off-peak Impact is Wallonia-only because it requires the
    # CWaPE IMPACT DSO tariff (Wallonia-specific).
    regions: frozenset[str] = _MEGA_ALL_REGIONS


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
    # Mega discontinued "Off-peak Fixed" (July 2026): the listing no longer
    # advertises it and its CDN PDFs are gone. Only "Off-peak Flex" and
    # "Off-peak Impact" remain. discover() re-surfaces it if Mega revives it.
    _ContractDef(
        "mega_offpeak_flex", "Mega Off-peak Flex", "variable", "Off-peak Flex"
    ),
    _ContractDef(
        "mega_offpeak_impact_var",
        "Mega Off-peak Impact",
        "tou_impact",
        "Off-peak Impact",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef("mega_dynamic", "Mega Dynamic", "dynamic", "Dynamic"),
    _ContractDef("mega_cap", "Mega Cap", "variable", "Mega Cap"),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Product names Mega lists on the public catalog page that this
# integration intentionally does not model. The daily live-check
# subtracts both _CONTRACTS and this set from the discovered list, so
# truly new residential electricity products surface as actionable
# signal while these stay quiet.
#
#   * Prepaid Fixed / Prepaid Flex -- topup-card products with a
#     different billing model (no monthly invoice, no recorder-backed
#     consumption sensors), out of scope for the Energy-dashboard
#     integration.
_KNOWN_UNSUPPORTED_PRODUCTS: frozenset[str] = frozenset(
    {
        "Prepaid Fixed",
        "Prepaid Flex",
    }
)


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
    return await fetch_text(session, _LISTING_URL)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every ``data-product-element`` value from Mega's listing,
    minus the products this integration deliberately doesn't model.

    Best-effort catalog discovery for the daily live-check: the diff
    against ``{c.product_name for c in _CONTRACTS}`` flags any new Mega
    product that should be added to the registry. Filtering out
    :data:`_KNOWN_UNSUPPORTED_PRODUCTS` keeps prepaid (topup-card)
    products from re-opening the same catalog issue every day.
    """
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return set()
    found = set(re.findall(r'data-product-element="([^"]+)"', listing))
    return found - _KNOWN_UNSUPPORTED_PRODUCTS


# ---- top-level fetch + parser -------------------------------------------------


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: the resolved PDF URL for (contract, region).

    Mega's listing has neither Last-Modified nor ETag, so the cheapest
    reliable probe is a listing GET + filename match. The URL contains
    the publication month (MMYYYY) so it changes whenever Mega rotates.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    region_code = _REGION_TO_CODE.get(region)
    if contract is None or region_code is None:
        return None
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return None
    return _find_pdf_url(listing, contract.product_name, region_code)


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


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Mega card for a specific ``(year, month)``.

    Mega's CDN keeps every monthly issue under a stable URL pattern:
    ``Mega-FR-EL-B2C-<REGION>-<MMYYYY>-<Product><DD><MM>[-<Variant>].pdf``.
    The month appears twice -- the ``<MMYYYY>`` segment and the ``<MM>``
    half of the product's effective-date ``<DD><MM>`` suffix -- and both
    must rotate while the effective day ``<DD>`` is preserved (most
    products publish on the 1st, but some, e.g. Cosy, use another day).
    The suffix can sit mid-token before a ``-Fixed`` / ``-Green`` /
    ``-Fix`` variant, so the rewrite can't anchor on ``.pdf``. Resolve
    the current URL via the listing first, then swap both month
    placeholders.

    Returns ``None`` when the URL 404s (or returns the CDN's HTML stub
    for a non-archived effective day, which ``_is_pdf_payload`` rejects),
    the parse fails, or the requested month falls outside the archive.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    contract = _CONTRACTS_BY_ID[contract_id]
    region_code = _REGION_TO_CODE.get(region)
    if region_code is None:
        return None
    try:
        listing = await _fetch_listing_html(session)
    except ExtractorError:
        return None
    current_url = _find_pdf_url(listing, contract.product_name, region_code)
    if current_url is None:
        return None
    # Capture the current card's month from the -MMYYYY- segment so we
    # can rewrite the matching <MM> half of the effective-date suffix
    # without touching the year digits.
    mmyyyy_re = re.compile(r"-(\d{2})\d{4}-(?=[^/]*\.pdf$)")
    mmyyyy_match = mmyyyy_re.search(current_url)
    if mmyyyy_match is None:
        return None
    current_mm = mmyyyy_match.group(1)
    target_mm = f"{year_month.month:02d}"
    historical_mmyyyy = f"{target_mm}{year_month.year}"
    new_url = mmyyyy_re.sub(f"-{historical_mmyyyy}-", current_url, count=1)
    # Rewrite the effective-date suffix's month, preserving the day, in
    # the filename tail after the -MMYYYY- segment (so the year is never
    # touched): Cap0106 -> Cap0105, Online0106-Fixed -> Online0105-Fixed,
    # Cosy1306 -> Cosy1305. A product that published on a day that
    # varies month to month resolves to the CDN's HTML stub, which the
    # PDF magic-byte check rejects, so the YTD walk falls back to the
    # proxy snapshot rather than mis-billing.
    prefix, sep, tail = new_url.partition(f"-{historical_mmyyyy}-")
    if sep:
        tail = re.sub(
            rf"(\d{{2}}){current_mm}(?=[-.])",
            rf"\g<1>{target_mm}",
            tail,
            count=1,
        )
        new_url = prefix + sep + tail
    if new_url == current_url:
        return None
    try:
        text = await fetch_pdf_text(session, new_url)
    except ExtractorError:
        return None
    try:
        snap = parse_snapshot(contract_id, text, region, new_url)
    except ExtractorError:
        return None
    # Cross-check the parsed card actually covers the requested month;
    # if Mega ever serves a current PDF under a historical URL, the
    # validity / title check rejects it instead of mis-billing past
    # consumption at current rates. Same shape as eneco / cociter / ebem.
    return archive_validity_check(snap, text, year_month, month_names=_FR_MONTH_NAMES)


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
        publication_label=publication_label,
        valid_until=parse_valid_until(text) or _extract_valid_until(text),
        injection=injection,
        supplier_prosumer_eur_per_kva_year=_extract_supplier_prosumer(
            text, region, contract.kind
        ),
    )


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


# ---- energy block -------------------------------------------------------------


# Mega prints two distinct formulas in every Dynamic PDF:
#   - Consumption: "...la formule tarifaire suivante : Day Ahead ... * X + Y c€/kWh"
#     (TVAC; spot is in c€/kWh and result is in c€/kWh)
#   - Injection:   "...la formule suivante (HTVA) : Day Ahead ... * X - Y c€/kWh"
#     (HTVA but injection is VAT-exempt residential, so no scaling needed)
_FORMULA_TAIL = (
    r"Day Ahead [Ee][Pp][Ee][Xx]\s*[Ss][Pp][Oo][Tt](?:\s*Belgium)?\s*\*\s*"
    # Accept dot or comma decimals for the factor and base: pypdf already
    # extracts the energy table on these cards with dot decimals, so a
    # re-render of the formula as "* 1.05 + 1.35" must not dead-end the
    # Dynamic snapshot. to_float normalises either separator.
    rf"([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh"
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
    base_cents = parse_sign(match.group(2)) * to_float(match.group(3))
    return factor, base_cents / 100.0


# Variable (Flex) cards print the indexation as prose (HTVA), e.g.
# "Compteur mono-horaire : Epex * 1,1095 + 3,6 c€/kWh". Unlike the dynamic
# formula this is ex-VAT, so it is grossed by the card's VAT below. Epex is in
# c€/kWh (same as the dynamic formula) so the factor maps directly to spot in
# EUR/kWh with no /MWh unit conversion.
_VARIABLE_MONO_FORMULA_RE = re.compile(
    rf"Compteur mono-horaire\s*:\s*Epex\s*\*\s*([\d.,]+)\s*"
    rf"([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
    re.IGNORECASE,
)


def _variable_cohort_coefficients(text: str) -> tuple[float | None, float | None]:
    """Numeric coefficients of the variable indexation formula, VAT-baked to
    the TVAC EUR/kWh basis (snapshot vat_rate is 0), or ``(None, None)``.

    The Epex index is the monthly RLP-weighted spot; the coordinator applies
    these against the plain arithmetic monthly mean (a close, few-percent
    approximation). A bi-hourly meter is billed the mono formula for the month.
    """
    match = _VARIABLE_MONO_FORMULA_RE.search(re.sub(r"\s+", " ", text))
    if match is None:
        return None, None
    vat_mult = vat_multiplier(text, re.compile(r"TVA\s*(\d+)\s*%\s*incluse", re.I))
    factor = to_float(match.group(1)) * vat_mult
    base = parse_sign(match.group(2)) * to_float(match.group(3)) * vat_mult / 100.0
    return factor, base


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

    if kind == "tou_impact":
        pic = _extract_impact_tier(text, "PIC")
        medium = _extract_impact_tier(text, "MEDIUM")
        eco = _extract_impact_tier(text, "ECO")
        if pic is None or medium is None or eco is None:
            raise ExtractorError("could not parse Mega Off-peak Impact energy block")
        formula_match = re.search(
            r"La formule tarifaire est la suivante[^.]+?(?=\s*Cette formule)",
            text,
            re.S | re.I,
        )
        formula = (
            re.sub(r"\s+", " ", formula_match.group(0)).strip()
            if formula_match
            else None
        )
        return ImpactRates(
            pic=pic,
            medium=medium,
            eco=eco,
            yearly_fixed_fee=yearly_fee,
            formula=formula,
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
    f_factor, f_base = _variable_cohort_coefficients(text)
    return VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
        formula_factor=f_factor,
        formula_base=f_base,
    )


def _extract_impact_tier(text: str, tier: str) -> float | None:
    """Pull one ECO / MEDIUM / PIC rate from an Off-peak Impact card.

    The energy block prints ``Tarif <TIER>\\n<consumption>\\n<injection>``;
    we want the first number after the label. The cards in circulation
    use bare ``PIC`` (not ``Tarif PIC``) on the last row, and lowercase
    ``tarif`` in the footnote formula, so the regex is intentionally
    permissive on the ``Tarif`` prefix.
    """
    match = re.search(rf"(?:Tarif\s+)?{tier}\s*\n\s*([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else None


def _extract_meter_value(text: str, label: str) -> float | None:
    """Pull the consumption rate that follows a meter-type label.

    Mega prints labels and values on separate lines; the consumption rate
    is the first number after the label and the injection rate is the
    second. Tarif jour / Tarif nuit / Exclusif nuit are only meaningful
    under the ``Compteur bi-horaire`` header; anchor on it so a later
    mention in dynamic-formula footnotes can't shadow the energy-block
    value.
    """
    scope = text
    if label in ("Tarif jour", "Tarif nuit", "Exclusif nuit"):
        # pypdf splits "Compteur bi-horaire" across a newline on every
        # current Mega card, so a literal ``find("Compteur bi-horaire")``
        # never matched. Match either the joined or the split spelling.
        anchor_match = re.search(r"Compteur\s+bi-horaire", text)
        if anchor_match is not None:
            scope = text[anchor_match.start() :]
    match = re.search(
        rf"{re.escape(label)}\s*\n\s*([\d.,]+)",
        scope,
    )
    return to_float(match.group(1)) / 100.0 if match else None


def _extract_yearly_fee(text: str) -> float:
    # Mega's dynamic card splits the heading across two lines
    # ('Redevance fixe\n(€/an)\n42.4'); fixed cards keep them together
    # ('Redevance fixe (€/an)\n111.3'). Accept either layout.
    match = re.search(r"Redevance fixe\s*\n?\s*\(€/an\)\s*\n?\s*([\d.,]+)", text)
    if match is None:
        # The standing charge (42-111 EUR/yr) is on every card; raise on a
        # miss rather than silently drop it, matching every other Mega line.
        raise ExtractorError("Mega: yearly fixed fee (Redevance fixe) not found")
    return to_float(match.group(1))


_FR_MONTH_NAMES = FR_MONTHS


def _extract_publication_month(text: str) -> str:
    # Smart Fixed cards prefix the version + month ("V2 avril 2026").
    # Include û so August ("août") keeps its version prefix.
    match = re.search(r"V(\d+\s+[a-zéû]+\s+\d{4})", text)
    if match:
        return match.group(1)
    # Smart Flex and Dynamic cards drop the version prefix and only print
    # "Prix du mois MM/YYYY". Surface MM/YYYY as "<month-name> YYYY" so
    # the publication_label sensor still tells the user which month the
    # snapshot covers.
    fallback = re.search(r"mois\s+(\d{2})/(\d{4})", text)
    if fallback:
        month = int(fallback.group(1))
        year = fallback.group(2)
        if 1 <= month <= 12:
            return f"{_FR_MONTH_NAMES[month - 1]} {year}"
    return ""


def _extract_valid_until(text: str) -> date | None:
    """Mega cards are valid for the printed month; use end-of-month."""
    fallback = re.search(r"mois\s+(\d{2})/(\d{4})", text)
    if not fallback:
        return None
    month = int(fallback.group(1))
    year = int(fallback.group(2))
    if not 1 <= month <= 12:
        return None
    return end_of_month(year, month)


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    """Mega prints injection rates in the same energy block, second column."""
    if kind == "tou_impact":
        # Off-peak Impact cards lack the ``Compteur mono-horaire`` anchor;
        # injection sits as the second number under any of the three tier
        # labels (all three rows print the same value, so pick the first
        # one we find).
        inj_match = re.search(
            r"(?:Tarif\s+)?(?:ECO|MEDIUM|PIC)\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)", text
        )
        current = to_float(inj_match.group(1)) / 100.0 if inj_match else None
    else:
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
    """First excise tier (0-3000 kWh), uniform across regions.

    Federal excise is mandatory on every Belgian residential card; a
    miss is a layout drift that would silently undercount the bill by
    ~5 c€/kWh (50 EUR/year at 1000 kWh). Raise rather than default to 0.
    """
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*([\d.,]+)",
        text,
    )
    if match is None:
        raise ExtractorError("Mega: federal excise (0-3000 kWh tier) not found")
    return to_float(match.group(1)) / 100.0


def _extract_energy_contribution(text: str) -> float:
    """Federal energy contribution; same row as the excise.

    Mandatory across regions; raise on miss for the same reason as the
    excise above.
    """
    match = re.search(
        r"Consommation entre\s*\n?\s*0\s*et\s*3000\s*kWh\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)",
        text,
    )
    if match is None:
        raise ExtractorError("Mega: energy contribution not found")
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


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
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
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
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
    excl_night = to_float(match.group(4))
    transport = to_float(match.group(5))
    mesure = to_float(match.group(6))
    # A residential <=13kVA Brussels connection is billed both the metering
    # fee (mesure_comptage) and the Sibelga fixed term for <=13kVA. Brussels
    # has no separate capacity charge (capacity is Flanders-only), so fold
    # both flat annual euros into data_management_per_year. The >13kVA term
    # (group 8) is for larger connections and is not billed here.
    fixed_term_le13 = to_float(match.group(7))
    return {
        DSO_SIBELGA: DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            transport=transport / 100.0,
            data_management_per_year=mesure + fixed_term_le13,
            brussels_osp_by_tier=parse_brussels_osp(text),
        )
    }


EXTRACTOR = SupplierExtractor(
    id="mega",
    label="Mega",
    contracts=tuple(
        Contract(id=c.contract_id, label=c.label, kind=c.kind, regions=c.regions)
        for c in _CONTRACTS
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
