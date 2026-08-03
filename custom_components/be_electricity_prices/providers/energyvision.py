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

Three residential electricity products are supported. Each is published for
exactly one region in exactly one language, so the region is a property of
the product rather than a variant of one card:

* ``GSDYN`` (Goedkope Stroom Dynamisch, Flanders): quarter-hourly Belpex
  formula, the same EUR/MWh HTVA axis as Bolt / Frank. The coefficient is a
  dimensionless Belpex multiplier (NOT scaled by ten the way Frank's
  cents-output coefficient is), the base goes EUR/MWh to EUR/kWh, and 6%
  VAT is baked into both. The injection coefficient is exactly 1,0.
* ``GS3JV`` (Goedkope stroom 3 jaar vast, Flanders): a flat fixed rate for
  3 years; its injection is indexed monthly (Belpex-SPP-M, known at
  month-end), so the printed monthly indicative is billed rather than a
  live spot formula.
* ``GS1JV`` (Électricité bon marché 1 an fixe, Wallonia): the same fixed
  shape on a 1-year lock, off a French card that shares no wording with the
  Dutch ones. Parsed by the ``*_fr`` helpers below. This is where DATS 24's
  Walloon customers land after the 2026-08-31 transfer.

Out of scope: gas (``GSG``, ``GS1JVG``) and the per-volume tiered products
(``GS1800V``, ``GSVI3``, ``GSLP``, ``GSEZ``, ``GSEZLP``), which price a
first tranche of kWh differently and have no representation in the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

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
    REGION_FLANDERS,
    REGION_WALLONIA,
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
# One listing page carries every card, Flemish and Walloon alike, so the
# freshness probe covers both.
_LISTING_URL = f"{_SITE_BASE}/nl-be/tariefkaart"
_FLANDERS_ONLY = frozenset({REGION_FLANDERS})
_WALLONIA_ONLY = frozenset({REGION_WALLONIA})


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    code: str  # EV filename product code (GSDYN, GS3JV, ...)
    # Filename language / region token, the part between the product code and
    # the Drupal dedup suffix. EnergyVision publishes each product for exactly
    # one region in exactly one language: the Flemish cards only as "-nl", the
    # Walloon ones only as "-WAL-fr". There is no card for the other pairing.
    token: str = "nl"
    regions: frozenset[str] = _FLANDERS_ONLY


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("energyvision_dynamic", "EnergyVision Dynamisch", "dynamic", "GSDYN"),
    _ContractDef("energyvision_fixed_3y", "EnergyVision 3 jaar vast", "fixed", "GS3JV"),
    # Wallonia's own fixed product, on a French card. It is a 1-year lock
    # where Flanders gets 3, so it is a distinct contract rather than the same
    # one in another region. This is the product DATS 24's Walloon customers
    # land on after the 2026-08-31 transfer (see providers/dats24.py).
    _ContractDef(
        "energyvision_fixed_1y",
        "EnergyVision 1 an fixe",
        "fixed",
        "GS1JV",
        token="WAL-fr",
        regions=_WALLONIA_ONLY,
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Every residential electricity product code EnergyVision currently lists,
# across both regions, so discover() flags only a genuinely new SKU. Only
# GSDYN / GS3JV (Flanders) and GS1JV (Wallonia) are implemented; the rest are
# catalogued-but-declined: GSVI3 / GS1800V / GSLP / GSEZ / GSEZLP are
# per-volume tiered products the model can't represent, GSG / GS1JVG are gas,
# GRSO is a transient group-buy SKU.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "GSDYN",
        "GS3JV",
        "GS1JV",
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
# From 1 August 2026 the federal scheme folded the separate energy
# contribution into the special excise and flattened it, so the tier table
# and the Energiebijdrage row both left the card and one "Bijzondere
# accijns" rate took their place. EnergyVision switched on its August
# Flemish card, a month after Engie / Mega / Eneco. The Walloon card is
# still on the old shape and keeps its own parser (_extract_taxes_fr).
_FLAT_EXCISE_RE = re.compile(rf"Bijzondere\s+accijns\s+{_NUM}", re.IGNORECASE)
_FUND_RE = re.compile(
    rf"Standaard\s+tarief\s+gedomicilieerd\s*:\s*{_NUM}\s*€\s*/\s*maand",
    re.IGNORECASE,
)

_LABEL_RE = re.compile(r"Tariefkaart\s+([A-Za-z]+\s+20\d{2})", re.IGNORECASE)

# ---- Wallonia (French card) --------------------------------------------------
#
# The Walloon cards are a separate publication in French, so none of the
# patterns above match them: every one was verified to miss. They are kept as
# a parallel set rather than widened into bilingual alternations, because the
# two cards also differ in structure (no digital/analog meter split, a ten-
# column DSO table, CV instead of GSC/WKC, no energiefonds).

# "Carte tarifaire juillet 2026". \w rather than [A-Za-z]: the accented month
# names (fevrier, aout, decembre) would otherwise blank the label for three
# months a year, and a miss is silent here.
_LABEL_FR_RE = re.compile(r"Carte\s+tarifaire\s+(\w+\s+20\d{2})", re.IGNORECASE)

# "Tous les prix et tarifs incluent la TVA a 6 %" - the number follows the tax
# name here, the reverse of the Dutch "6% BTW".
_VAT_FR_RE = re.compile(r"TVA\s*(?:à|a)\s*(\d+)\s*%", re.IGNORECASE)

# "Electricite verte - tarif fixe 13,57 EURcent/kWh" and, on the same page,
# "Injection - variable 2,07 EURcent/kWh". The separator is an ASCII hyphen on
# the first and a U+2013 en dash on the second, both already in SIGN_CHARS.
_FIXED_ENERGY_FR_RE = re.compile(
    rf"Électricité\s+verte\s*[{SIGN_CHARS}]\s*tarif\s+fixe\s+"
    rf"{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_FR_RE = re.compile(
    rf"Injection\s*[{SIGN_CHARS}]\s*variable\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FEE_FR_RE = re.compile(rf"Frais\s+fixes\s+{_NUM}\s*€\s*/\s*an", re.IGNORECASE)

# Walloon tax block. The units live in the section headers ("Suppléments
# (€cent/kWh)", "Accise fédérale (€cent/kWh)"), not on the rows, so every
# value here is c€/kWh and divides by 100.
_EXCISE_FR_RE = re.compile(
    rf"Consommation\s+entre\s+0\s*&\s*3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
_CONTRIB_FR_RE = re.compile(rf"Contribution\s+énergétique\s+{_NUM}", re.IGNORECASE)
_CONNECTION_FR_RE = re.compile(
    rf"Redevance\s+de\s+raccordement\s+{_NUM}", re.IGNORECASE
)
# The Walloon green-certificate quota cost, the CV counterpart of Flanders'
# GSC + WKC. Supplier-specific (EnergyVision prints 3,00 where DATS 24 prints
# 2,860 for the same month), so it is always read off this card.
_CV_FR_RE = re.compile(
    rf"certificats\s+verts\s+et\s+certificats\s+de\s+cogénération"
    rf"[^\d]*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)

# Walloon DSO row label -> DSO key. EnergyVision drops the "ORES" prefix from
# six of the seven ORES sub-areas (BRABANT WALLON, EST, HAINAUT ELECTRICITÉ,
# ORES LUXEMBOURG, MOUSCRON, NAMUR, VERVIERS), all carrying identical numbers,
# so the project's collapse-to-one-key convention picks Brabant Wallon as the
# representative row, matching dats24.py. Note the labels differ from DATS
# 24's card ("TECTEO RESA" vs "RESA", "WAVRE" vs "RÉGIE DE WAVRE").
_DSO_ROWS_FR: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    ("BRABANT WALLON", DSO_ORES),
    ("TECTEO RESA", DSO_RESA),
    ("WAVRE", DSO_REW),
)

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
    if region not in contract.regions:
        raise ExtractorError(
            f"EnergyVision {contract_id} is not sold in {region!r}; "
            f"published for {sorted(contract.regions)}"
        )
    url = await _resolve_card_url(session, contract)
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
    """Return the residential electricity product codes on the listing so
    live_check can flag a new SKU. Diffed against :data:`DISCOVER_IDS`.

    Both language tokens are walked: the Flemish cards are published only as
    ``-nl`` and the Walloon ones only as ``-WAL-fr``, so matching one token
    would silently drop a whole region's catalogue from the drift check.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return set(re.findall(r"inline-files/EV-\d{4}-([A-Z0-9]+)-(?:nl|WAL-fr)", html))


async def _resolve_card_url(
    session: aiohttp.ClientSession, contract: _ContractDef
) -> str:
    html = await fetch_text(session, _LISTING_URL)
    match = re.search(
        rf'href="(/sites/default/files/inline-files/EV-\d{{4}}-'
        rf'{re.escape(contract.code)}-{re.escape(contract.token)}[^"]*\.pdf)"',
        html,
        re.IGNORECASE,
    )
    if not match:
        raise ExtractorError(f"EnergyVision: no listing entry for card {contract.code}")
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
    if REGION_WALLONIA in contract.regions:
        return _parse_wallonia(contract_id, text, source_url, publication_label)
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


def _parse_wallonia(
    contract_id: str, text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Parse the French Walloon card.

    Same snapshot shape as the Flemish fixed card, off an entirely separate
    publication: a flat VAT-inclusive rate, a yearly standing charge, and a
    monthly-indexed injection indicative.
    """
    energy, injection = _extract_fixed_fr(text)
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos_fr(text),
        taxes=_extract_taxes_fr(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label_fr(text),
        valid_until=parse_valid_until(text),
        injection=injection,
    )


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return m.group(1).lower() if m else ""


def _publication_label_fr(text: str) -> str:
    m = _LABEL_FR_RE.search(text)
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
    # Try the flat August-2026 row first: a card carrying both would be the
    # tiered one being phased out, and the flat rate is authoritative when
    # present. Same precedence the other suppliers' parsers use.
    excise = _FLAT_EXCISE_RE.search(text) or _EXCISE_RE.search(text)
    if not gsc or not excise:
        raise ExtractorError("EnergyVision: could not parse tax block")
    fund = _FUND_RE.search(text)
    # Every value on the card is VAT-inclusive (the federal excise and energy
    # fund are VAT-exempt), so vat_rate stays 0.0, matching the energy leg.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        # The contribution was abolished on 2026-08-01, taking its row off
        # the card with it. An absent row is the abolished levy, not a
        # layout drift: return 0 rather than failing the fetch and taking
        # every Flemish EnergyVision contract offline.
        energy_contribution=(to_float(contrib.group(1)) / 100.0 if contrib else 0.0),
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


# ---- Wallonia parsers --------------------------------------------------------


def _extract_fixed_fr(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _FEE_FR_RE.search(text)
    if fee is None:
        raise ExtractorError("EnergyVision: frais fixes row not found")
    m = _FIXED_ENERGY_FR_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia energy price")
    # One flat rate: the card prints no bi-horaire or exclusive-night energy
    # price (those words appear only as DSO-table column headers), so peak /
    # offpeak / exclusive_night stay unset and the engine bills `single` for
    # every meter type. Printed VAT-inclusive, so used as-is.
    energy = FixedRates(
        single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=to_float(fee.group(1))
    )
    inj = _FIXED_INJECTION_FR_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia injection price")
    # Belpex-SPP-M is a month-long average the card states is "connue qu'à la
    # fin du mois", so there is no live spot to apply a factor to: bill the
    # printed monthly indicative and leave factor / base None. Emitting them
    # would apply a monthly coefficient to the hourly spot and mis-price the
    # credit. The card's 1 c€/kWh guarantee is monthly too, so it is already
    # baked into the printed value and floor_at_zero must stay False.
    injection = InjectionRates(current=to_float(inj.group(1)) / 100.0)
    return energy, injection


def _extract_taxes_fr(text: str) -> TaxOverlay:
    excise = _EXCISE_FR_RE.search(text)
    contrib = _CONTRIB_FR_RE.search(text)
    cv = _CV_FR_RE.search(text)
    connection = _CONNECTION_FR_RE.search(text)
    if not excise or not contrib or not cv or not connection:
        # All four are mandatory on a Walloon card. The CV quota cost and the
        # connection fee in particular are per-kWh charges, so silently
        # zeroing either under-bills every user of this contract.
        raise ExtractorError("EnergyVision: could not parse Wallonia tax block")
    # There is no Flemish energiefonds and no GSC/WKC row on this card; the
    # header states every price includes 6% VAT, so vat_rate stays 0.0.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        energy_contribution=to_float(contrib.group(1)) / 100.0,
        wallonia_renewables=to_float(cv.group(1)) / 100.0,
        region_connection_fee=to_float(connection.group(1)) / 100.0,
        vat_rate=0.0,
    )


def _extract_dsos_fr(text: str) -> dict[str, DsoOverlay]:
    """Parse the Walloon DSO table (one ten-column block, no meter split).

    Column order, left to right:

        mono | bi-peak | bi-offpeak | ECO | MEDIUM | PIC | exclusive-night
        | transport | data-management (EUR/yr) | prosumer (EUR/kW/yr)

    The three CWaPE Impact bands print CHEAPEST FIRST here, the reverse of
    the PIC | MEDIUM | ECO order on the DATS 24 card that carries the same
    regulated numbers. Reusing that positional mapping would swap the peak
    and off-peak bands and mis-price every Walloon Impact user, so the
    ordering is asserted in the tests by value (eco < medium < pic).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_ROWS_FR:
        row = re.search(
            rf"^{re.escape(label)}\s+" + r"\s+".join([_NUM] * 10),
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(1)) / 100.0,
            distribution_peak=to_float(row.group(2)) / 100.0,
            distribution_offpeak=to_float(row.group(3)) / 100.0,
            distribution_eco=to_float(row.group(4)) / 100.0,
            distribution_medium=to_float(row.group(5)) / 100.0,
            distribution_pic=to_float(row.group(6)) / 100.0,
            distribution_exclusive_night=to_float(row.group(7)) / 100.0,
            transport=to_float(row.group(8)) / 100.0,
            data_management_per_year=to_float(row.group(9)),
            prosumer_eur_per_kva_year=to_float(row.group(10)),
        )
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    id="energyvision",
    label="EnergyVision",
    contracts=tuple(
        Contract(id=c.contract_id, label=c.label, kind=c.kind, regions=c.regions)
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)


__all__ = ["DISCOVER_IDS", "EXTRACTOR", "discover", "fetch", "parse_snapshot", "probe"]
