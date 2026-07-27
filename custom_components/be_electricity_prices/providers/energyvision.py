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

"""EnergyVision tariff extractor.

EnergyVision publishes its "Goedkope stroom" residential cards as monthly
PDFs named ``EV-<MMYY>-<CODE>-<lang>.pdf`` under
``/sites/default/files/inline-files/``. The filenames carry the pricing
month (``EV-0726-...`` = July 2026) and the server adds Drupal dedup
suffixes (the fixed card ships as ``EV-0726-GS3JV-nl_0.pdf``), so a
constructed URL would miss it. The fetch therefore scrapes the current
card href off the tariefkaart listing page (the Mega / Frank shape).

Two residential Flanders electricity products are supported:

* ``GSDYN`` (Goedkope Stroom Dynamisch): quarter-hourly Belpex formula,
  the same EUR/MWh HTVA axis as Bolt / Frank. The coefficient is a
  dimensionless Belpex multiplier (NOT scaled by ten the way Frank's
  cents-output coefficient is), the base goes EUR/MWh to EUR/kWh, and 6%
  VAT is baked into both. The injection coefficient is exactly 1,0.
* ``GS3JV`` (Goedkope stroom 3 jaar vast): a flat fixed rate for 3 years;
  its injection is indexed monthly (Belpex-SPP-M, known at month-end), so
  the printed monthly indicative is billed rather than a live spot formula.

Region: Flanders only (all 8 Fluvius sub-areas). Wallonia and gas ship as
separate cards that are out of scope here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import aiohttp

from ..const import (
    DSO_FLUVIUS_ANTWERPEN,
    DSO_FLUVIUS_HALLE_VILVOORDE,
    DSO_FLUVIUS_IMEWO,
    DSO_FLUVIUS_INTERGEM,
    DSO_FLUVIUS_IVEKA,
    DSO_FLUVIUS_LIMBURG,
    DSO_FLUVIUS_WEST,
    DSO_FLUVIUS_ZENNE_DIJLE,
    REGION_FLANDERS,
)
from ._pdf import (
    SIGN_CHARS,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
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
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
)

_SITE_BASE = "https://www.energyvision.be"
_LISTING_URL = f"{_SITE_BASE}/nl-be/tariefkaart"
_REGIONS = frozenset({REGION_FLANDERS})


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    code: str  # EV filename product code (GSDYN, GS3JV, ...)


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("energyvision_dynamic", "EnergyVision Dynamisch", "dynamic", "GSDYN"),
    _ContractDef("energyvision_fixed_3y", "EnergyVision 3 jaar vast", "fixed", "GS3JV"),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Every residential NL (Flanders) electricity product code EnergyVision
# currently lists, so discover() flags only a genuinely new SKU. Only GSDYN
# and GS3JV are implemented; the rest are catalogued-but-declined: GSVI3 /
# GS1800V / GSLP / GSEZ / GSEZLP are per-volume tiered products the model
# can't represent, GSG / GS1JVG are gas, GRSO is a transient group-buy SKU.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "GSDYN",
        "GS3JV",
        "GSVI3",
        "GS1800V",
        "GSLP",
        "GSEZ",
        "GSEZLP",
        "GSG",
        "GS1JVG",
        "GRSO",
    }
)

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = r"([\d]+(?:[.,][\d]+)?)"

# The card header prints "Alle prijzen en tarieven zijn inclusief 6% BTW".
_VAT_RE = re.compile(r"(\d+)\s*%\s*BTW", re.IGNORECASE)

# Dynamic card: "afnametarief ... formule (exclusief btw): 1,05 x Belpex per
# kwartier + 15 EUR/MWh" and "injectietarief ... formule: 1 x Belpex per
# kwartier - 15 EUR/MWh". One findall yields both rows; group 1 keys which.
_DYN_FORMULA_RE = re.compile(
    rf"(afname|injectie)tarief\b[^:]*?:\s*"
    rf"{_NUM}\s*x\s*Belpex\s+per\s+kwartier\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)

# Fixed card energy + its printed monthly injection indicative (page 1).
_FIXED_ENERGY_RE = re.compile(
    rf"Groene\s+stroom\s*[{SIGN_CHARS}]\s*vast\s+tarief\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_RE = re.compile(
    rf"Injectie\s*[{SIGN_CHARS}]\s*variabel\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)

_FEE_RE = re.compile(rf"Vaste\s+vergoeding\s+{_NUM}\s*€\s*/\s*jaar", re.IGNORECASE)

# Taxes (Flanders). GSC + WKC print as a single combined value; the
# energiefonds shows a domiciled (standard residential = 0 EUR/month) and a
# non-domiciled row; bill the domiciled one.
_GSC_WKC_RE = re.compile(rf"GSC\s+en\s+WKC\s+geldig\s+voor\s+{_NUM}", re.IGNORECASE)
_CONTRIB_RE = re.compile(rf"Energiebijdrage\s+{_NUM}", re.IGNORECASE)
_EXCISE_RE = re.compile(
    rf"Verbruik\s+tussen\s+0\s*&\s*3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
_FUND_RE = re.compile(
    rf"Standaard\s+tarief\s+gedomicilieerd\s*:\s*{_NUM}\s*€\s*/\s*maand",
    re.IGNORECASE,
)

_LABEL_RE = re.compile(r"Tariefkaart\s+([A-Za-z]+\s+20\d{2})", re.IGNORECASE)

# The Flanders DSO table prints two blocks (digital + analog meter). Only the
# digital-meter block is billed (modern smart meters); its five columns are
# capaciteitstarief (EUR/kW/yr) | kWh-tarief (c€/kWh) | kWh excl. nacht
# (c€/kWh) | databeheer (EUR/yr) | maximumtarief (c€/kWh, unused ceiling).
_DIGITAL_MARKER = "Digitale Meter"
_ANALOG_MARKER = "Analoge Meter"

# Upper-case Fluvius area label -> DSO key (EnergyVision prints them in caps,
# so the shared Title-case FLUVIUS_CARD_LABELS map doesn't apply). Kempen is
# the Iveka sub-area; Midden-Vlaanderen is Intergem.
_DSO_ROWS: tuple[tuple[str, str], ...] = (
    ("ANTWERPEN", DSO_FLUVIUS_ANTWERPEN),
    ("HALLE-VILVOORDE", DSO_FLUVIUS_HALLE_VILVOORDE),
    ("IMEWO", DSO_FLUVIUS_IMEWO),
    ("KEMPEN", DSO_FLUVIUS_IVEKA),
    ("LIMBURG", DSO_FLUVIUS_LIMBURG),
    ("MIDDEN-VLAANDEREN", DSO_FLUVIUS_INTERGEM),
    ("WEST", DSO_FLUVIUS_WEST),
    ("ZENNE-DIJLE", DSO_FLUVIUS_ZENNE_DIJLE),
)


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("EnergyVision cards are Flanders-only")
    url = await _resolve_card_url(session, contract.code)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - the listing key is region-independent.
) -> str | None:
    """Cheap freshness key: HEAD the listing page. Its ETag / Last-Modified
    flips when EnergyVision rotates the monthly cards, which is exactly when
    the resolved PDF URL changes."""
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    return await head_freshness_key(
        session, _LISTING_URL, prefer=("ETag", "Last-Modified")
    )


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the residential NL electricity product codes on the listing so
    live_check can flag a new SKU. Diffed against :data:`DISCOVER_IDS`."""
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return set(re.findall(r"inline-files/EV-\d{4}-([A-Z0-9]+)-nl", html))


async def _resolve_card_url(session: aiohttp.ClientSession, code: str) -> str:
    html = await fetch_text(session, _LISTING_URL)
    match = re.search(
        rf'href="(/sites/default/files/inline-files/EV-\d{{4}}-{re.escape(code)}-nl[^"]*\.pdf)"',
        html,
        re.IGNORECASE,
    )
    if not match:
        raise ExtractorError(f"EnergyVision: no listing entry for card {code}")
    return _SITE_BASE + match.group(1)


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str,
    publication_label: str = "",
) -> SupplierSnapshot:
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    energy: EnergyRates
    if contract.kind == "dynamic":
        energy, injection = _extract_dynamic(text)
    else:
        energy, injection = _extract_fixed(text)
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label(text),
        valid_until=parse_valid_until(text),
        injection=injection,
    )


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return m.group(1).lower() if m else ""


def _fee(text: str) -> float:
    m = _FEE_RE.search(text)
    if m is None:
        # The vaste vergoeding standing charge is mandatory; fail loud rather
        # than silently bill a zero yearly fee on a layout drift.
        raise ExtractorError("EnergyVision: vaste vergoeding row not found")
    return to_float(m.group(1))


def _extract_dynamic(text: str) -> tuple[DynamicRates, InjectionRates]:
    fee = _fee(text)
    # The card quotes the formula "(exclusief btw)"; every printed price is
    # VAT-inclusive, so the energy leg is scaled to the same basis (vat_rate
    # then stays 0.0, matching Frank / Bolt).
    vat = vat_multiplier(text, _VAT_RE)
    energy: DynamicRates | None = None
    injection: InjectionRates | None = None
    for word, factor_s, sign, base_s in _DYN_FORMULA_RE.findall(text):
        base_eur_mwh = parse_sign(sign) * to_float(base_s)
        if word.lower() == "afname":
            # EUR/MWh HTVA -> EUR/kWh incl VAT: the coefficient is a
            # dimensionless Belpex multiplier (* VAT, NO * 10), the base goes
            # EUR/MWh -> EUR/kWh (/1000 * VAT).
            energy = DynamicRates(
                factor=to_float(factor_s) * vat,
                base=base_eur_mwh / 1000.0 * vat,
                yearly_fixed_fee=fee,
                quarter_hourly=True,
            )
        else:
            # Injection is VAT-exempt: factor as-is (exactly 1,0 here), base
            # EUR/MWh -> EUR/kWh, no VAT.
            injection = InjectionRates(
                factor=to_float(factor_s),
                base=base_eur_mwh / 1000.0,
                formula=f"{factor_s} x Belpex {sign} {base_s} EUR/MWh",
            )
    if energy is None:
        raise ExtractorError("EnergyVision: could not parse dynamic afname formula")
    if injection is None:
        # Every dynamic card prints an injection formula; a miss is a layout
        # drift, not a fee-free contract. Raise rather than silently credit 0.
        raise ExtractorError("EnergyVision: could not parse dynamic injectie formula")
    return energy, injection


def _extract_fixed(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _fee(text)
    m = _FIXED_ENERGY_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse fixed energy price")
    # The fixed rate is printed VAT-inclusive, so it is used as-is.
    energy = FixedRates(single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=fee)
    inj = _FIXED_INJECTION_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse fixed injection price")
    # Injection is indexed monthly (Belpex-SPP-M, known only at month-end), so
    # the card prints the resolved monthly indicative, which is what we bill,
    # never a live hourly factor/base against the spot. VAT-exempt, and the
    # card's 1 c€/kWh floor is already applied to the printed value.
    injection = InjectionRates(current=to_float(inj.group(1)) / 100.0)
    return energy, injection


def _extract_taxes(text: str) -> TaxOverlay:
    gsc = _GSC_WKC_RE.search(text)
    contrib = _CONTRIB_RE.search(text)
    excise = _EXCISE_RE.search(text)
    if not gsc or not contrib or not excise:
        raise ExtractorError("EnergyVision: could not parse tax block")
    fund = _FUND_RE.search(text)
    # Every value on the card is VAT-inclusive (the federal excise and energy
    # fund are VAT-exempt), so vat_rate stays 0.0, matching the energy leg.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        energy_contribution=to_float(contrib.group(1)) / 100.0,
        flanders_renewables=to_float(gsc.group(1)) / 100.0,
        energy_fund_eur_per_month=(to_float(fund.group(1)) if fund else 0.0),
        vat_rate=0.0,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find(_DIGITAL_MARKER)
    if start < 0:
        raise ExtractorError("EnergyVision: digital-meter DSO table not found")
    end = text.find(_ANALOG_MARKER, start)
    section = text[start:end] if end > start else text[start:]
    out: dict[str, DsoOverlay] = {}
    for area, key in _DSO_ROWS:
        row = re.search(
            rf"FLUVIUS\s+{re.escape(area)}\s+"
            rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
            section,
            re.IGNORECASE,
        )
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(2)) / 100.0,
            distribution_exclusive_night=to_float(row.group(3)) / 100.0,
            transport=0.0,
            capacity_eur_per_kw_year=to_float(row.group(1)),
            data_management_per_year=to_float(row.group(4)),
        )
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    id="energyvision",
    label="EnergyVision",
    contracts=tuple(
        Contract(id=c.contract_id, label=c.label, kind=c.kind, regions=_REGIONS)
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)


__all__ = ["DISCOVER_IDS", "EXTRACTOR", "discover", "fetch", "parse_snapshot", "probe"]
