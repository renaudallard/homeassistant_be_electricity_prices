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

"""EBEM (Ebem bvba, Merksplas) tariff card extractor.

EBEM is a small Flemish supplier serving the Mol/Geel area. It sells three
residential electricity products today:

  - Groen Variabel : monthly RLP-weighted Belpex variable, mono / bi / excl. night.
  - Groen B@sic+   : monthly RLP-weighted Belpex variable, single rate (online-only).
  - Groen Dyn@mic  : 15-minute Belpex spot dynamic, SMR3 only.

Both variable products live in the same monthly PDF; the dynamic product has
its own card. PDFs are linked from ``https://www.ebem.be/tarieven/`` under
opaque-hash URLs (Umbraco CMS media-folder ids that change per file), so every
fetch must scrape the listing page rather than constructing the URL directly.

EBEM keeps a public archive of past months on the same page (≥ 6 months at
last check), so ``fetch_for_month`` walks the listing and resolves the URL
whose filename suffix matches the requested ``MM-YYYY``. This lets the
coordinator bill past consumption at each month's actual rates.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

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
    TariffKind,
    TaxOverlay,
    VariableRates,
)

_LISTING_URL = "https://www.ebem.be/tarieven/"
_PDF_BASE = "https://www.ebem.be"

# EBEM links each monthly PDF as ``/media/<hash>/ebem_tariefkaart-{kind}-MM-YYYY.pdf``
# from the listing page. Captures (path, kind, MM, YYYY). The 2026-01 dynamic
# file is named ``ebem_tariefkaart-dynamic_01-2026.pdf`` (underscore between
# kind and MM) -- accept either separator so the archive walker doesn't lose
# that month silently. The kind group is open-ended so a future third
# PDF kind (e.g. ``fix`` if Ebem revives fixed contracts) surfaces in
# ``discover()`` even though only ``elek`` and ``dynamic`` map to contract ids.
_PDF_RE = re.compile(
    r'href="(/media/[^"]+/ebem_tariefkaart-([a-z]+)[-_](\d{2})-(\d{4})\.pdf)"',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    pdf_kind: str  # "elek" (variable) or "dynamic"


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("ebem_variable", "EBEM Groen Variabel", "variable", "elek"),
    _ContractDef("ebem_basic_plus", "EBEM Groen B@sic+", "variable", "elek"),
    _ContractDef("ebem_dynamic", "EBEM Groen Dyn@mic", "dynamic", "dynamic"),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


# ---- top-level fetch / probe / discover --------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - EBEM only sells in Flanders.
) -> SupplierSnapshot:
    """Fetch and parse the latest EBEM PDF for ``contract_id``.

    Resolves the URL by scraping ``_LISTING_URL`` for the most recent
    ``(elek|dynamic)`` entry. Two of the three contracts share the same
    ``elek`` PDF; the parser branches on contract id when extracting the
    energy block.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EBEM contract {contract_id!r}")
    url, label = await _find_latest(session, contract.pdf_kind)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url, label)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - EBEM only sells in Flanders.
    year_month: date,
) -> SupplierSnapshot | None:
    """Return the published snapshot for a specific (year, month).

    Walks the same listing page, resolves the URL whose filename matches
    the requested ``MM-YYYY``, parses, and validates that the parsed
    ``valid_until`` falls in the requested month -- a defensive check
    against a CDN-substituted current card mis-billing past consumption.
    Returns ``None`` when the month isn't published (or the validity
    cross-check rejects the served PDF).
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return None
    target = (
        contract.pdf_kind,
        f"{year_month.month:02d}",
        f"{year_month.year:04d}",
    )
    match = next(
        (m for m in _PDF_RE.findall(html) if (m[1].lower(), m[2], m[3]) == target),
        None,
    )
    if match is None:
        return None
    url = _PDF_BASE + match[0]
    label = f"{match[3]}-{match[2]}"
    try:
        text = await fetch_pdf_text_layout(session, url)
        snap = parse_snapshot(contract_id, text, url, label)
    except ExtractorError:
        return None
    if snap.valid_until is not None and (
        snap.valid_until.year != year_month.year
        or snap.valid_until.month != year_month.month
    ):
        return None
    return snap


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,  # noqa: ARG001 - listing key covers every contract.
    region: str,  # noqa: ARG001 - EBEM only sells in Flanders.
) -> str | None:
    """HEAD the listing page; return its ``Last-Modified`` / ``ETag``.

    EBEM publishes new monthly cards by editing the listing page (the
    opaque media-hash URL changes for every month), so the listing's
    freshness header is the right key for every contract at once.
    """
    return await head_freshness_key(session, _LISTING_URL)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return EBEM contract ids visible on the listing page.

    Maps the two distinct PDF kinds to contract ids:
      - any ``elek`` PDF surfaces ``ebem_variable`` and ``ebem_basic_plus``
        (both products live in the same card),
      - any ``dynamic`` PDF surfaces ``ebem_dynamic``.

    A future third PDF kind (e.g. ``fix`` for a fixed contract -- the
    variable card explicitly notes Ebem stopped selling those for now)
    surfaces verbatim so live_check files a tracking issue.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    out: set[str] = set()
    for _, kind, _, _ in _PDF_RE.findall(html):
        kind = kind.lower()
        if kind == "elek":
            out.add("ebem_variable")
            out.add("ebem_basic_plus")
        elif kind == "dynamic":
            out.add("ebem_dynamic")
        else:
            out.add(kind)
    return out


async def _find_latest(
    session: aiohttp.ClientSession, pdf_kind: str
) -> tuple[str, str]:
    """Pick the most recent (path, MM, YYYY) row of ``pdf_kind`` from the listing."""
    html = await fetch_text(session, _LISTING_URL)
    matches = [m for m in _PDF_RE.findall(html) if m[1].lower() == pdf_kind]
    if not matches:
        raise ExtractorError(f"no EBEM {pdf_kind!r} card linked at {_LISTING_URL}")
    # Sort by (YYYY, MM) ascending so the last entry is the freshest.
    matches.sort(key=lambda m: (m[3], m[2]))
    path, _, mm, yyyy = matches[-1]
    return _PDF_BASE + path, f"{yyyy}-{mm}"


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str = _LISTING_URL,
    publication_label: str = "",
) -> SupplierSnapshot:
    """Parse one EBEM tariff card."""
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EBEM contract {contract_id!r}")
    energy = _extract_energy(text, contract)
    injection = _extract_injection(text, contract)
    federal_excise, energy_contribution = _extract_federal_taxes(text)
    flanders_renewables = _extract_flanders_renewables(text)
    return SupplierSnapshot(
        supplier="ebem",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text, contract),
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=0.0,
            brussels_renewables=0.0,
            region_connection_fee=0.0,
            energy_fund_eur_per_month=0.0,
            vat_rate=0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=_extract_validity(text),
        injection=injection,
    )


_DUTCH_MONTHS: dict[str, int] = {
    "januari": 1,
    "februari": 2,
    "maart": 3,
    "april": 4,
    "mei": 5,
    "juni": 6,
    "juli": 7,
    "augustus": 8,
    "september": 9,
    "oktober": 10,
    "november": 11,
    "december": 12,
}


def _extract_validity(text: str) -> date | None:
    """Return the last day of the printed Dutch month + year, e.g. ``mei 2026``.

    EBEM's tariff cards have no validity-keyword anchor (``geldig`` /
    ``valable``) that ``_pdf.parse_valid_until`` would key on, so the
    shared helper would return ``None``. The card title ends with
    ``<maand> <jaar>``; parse that directly.
    """
    match = re.search(r"\b([a-z]+)\s+(20\d{2})\b", text[:600])
    if not match:
        return None
    month_name = match.group(1).lower()
    if month_name not in _DUTCH_MONTHS:
        return None
    year = int(match.group(2))
    month = _DUTCH_MONTHS[month_name]
    return date(year, month, calendar.monthrange(year, month)[1])


# ---- energy + injection -----------------------------------------------------

# 6% Belgian residential VAT applied to the ex-VAT formula factors so the
# stored snapshot value matches the registry's VAT-incl convention. Identical
# to ``cociter.py``'s dynamic conversion: factor * 1.06 * 10 (cents/kWh per
# €/MWh -> EUR/kWh per EUR/kWh), base * 1.06 / 100.
_VAT = 1.06


def _formula_to_dynamic(
    factor_pdf: float, base_pdf_cents: float
) -> tuple[float, float]:
    return factor_pdf * _VAT * 10.0, base_pdf_cents * _VAT / 100.0


def _extract_energy(text: str, contract: _ContractDef) -> EnergyRates:
    if contract.contract_id == "ebem_dynamic":
        # The dynamic card prints "alle uren 0,108 Belpex15' + 1,625" for
        # consumption and "injectie alle uren 0,0925 Belpex15' - 1,10" for
        # injection. Anchor on a line that starts with "alle uren" but is
        # NOT preceded by "injectie" so the regex doesn't pick the
        # injection row by accident.
        match = re.search(
            r"(?<!injectie\s)\balle uren\s+([\d,]+)\s+Belpex\s*15\s*[’']?\s*"
            r"\+\s*([\d,]+)",
            text,
        )
        if not match:
            raise ExtractorError("EBEM Dyn@mic: consumption formula not found")
        factor, base = _formula_to_dynamic(
            to_float(match.group(1)), to_float(match.group(2))
        )
        yearly_fee = _extract_yearly_fee_abonnement(text)
        return DynamicRates(
            factor=factor,
            base=base,
            yearly_fixed_fee=yearly_fee,
        )

    if contract.contract_id == "ebem_basic_plus":
        match = re.search(
            r"Verbruik alle uren\s+([\d,]+)\s+Belpex\s*\+\s*([\d,]+)",
            text,
        )
        if not match:
            raise ExtractorError("EBEM B@sic+: 'Verbruik alle uren' formula not found")
        factor_pdf = to_float(match.group(1))
        base_pdf_cents = to_float(match.group(2))
        # B@sic+ has no peak / off-peak / excl-night split.
        return VariableRates(
            current=_indicative_from_row(text, "Verbruik alle uren"),
            yearly_fixed_fee=_extract_yearly_fee_abonnement(text),
            formula=f"({factor_pdf} BelpexRLP0 + {base_pdf_cents}) c€/kWh ex-VAT",
        )

    # ebem_variable: parse all four meter-type rows.
    rows = {
        "mono": r"Enkelvoudige teller\s+([\d,]+)\s+Belpex\s*\+\s*([\d,]+)",
        "peak": r"Dubbele teller piek\s+([\d,]+)\s+Belpex\s*\+\s*([\d,]+)",
        "offpeak": r"Dubbele teller dal\s+([\d,]+)\s+Belpex\s*\+\s*([\d,]+)",
        "excl_night": r"Exclusief nacht\s+([\d,]+)\s+Belpex\s*\+\s*([\d,]+)",
    }
    parsed: dict[str, tuple[float, float]] = {}
    for label, pattern in rows.items():
        m = re.search(pattern, text)
        if not m:
            raise ExtractorError(
                f"EBEM Groen Variabel: '{label}' formula row not found"
            )
        parsed[label] = (to_float(m.group(1)), to_float(m.group(2)))
    yearly_fee = _extract_yearly_fee_variable(text)
    return VariableRates(
        current=_indicative_from_row(text, "Enkelvoudige teller"),
        peak=_indicative_from_row(text, "Dubbele teller piek"),
        offpeak=_indicative_from_row(text, "Dubbele teller dal"),
        exclusive_night=_indicative_from_row(text, "Exclusief nacht"),
        yearly_fixed_fee=yearly_fee,
        formula=(
            f"mono ({parsed['mono'][0]} BelpexRLP0 + {parsed['mono'][1]}) "
            f"· peak ({parsed['peak'][0]} + {parsed['peak'][1]}) "
            f"· off-peak ({parsed['offpeak'][0]} + {parsed['offpeak'][1]}) "
            f"c€/kWh ex-VAT"
        ),
    )


def _indicative_from_row(text: str, label: str) -> float:
    """Parse the printed ``INCL. BTW 6% (c€/kWh)`` indicative rate.

    EBEM's variable cards print four numeric columns per row after the
    formula expression: ``EXCL.BTW``, ``INCL.BTW 6%``, ``GESCHATTE
    JAARPRIJS EXCL.BTW``, ``GESCHATTE JAARPRIJS INCL.BTW 6%``. Columns
    1 + 2 are the per-kWh indicative at last-month's Belpex; columns 3 +
    4 use the VNR yearly-forecast Belpex. We surface column 2 (incl-VAT
    per-kWh at last-month's Belpex) as the snapshot's ``current`` -- it
    matches the value EBEM customers see on their bill more faithfully
    than recomputing against a placeholder spot.
    """
    match = re.search(
        rf"{re.escape(label)}\s+[\d,]+\s+Belpex\s*\+\s*[\d,]+\s+"
        rf"[\d,]+\s+([\d,]+)\s+[\d,]+\s+[\d,]+",
        text,
    )
    if not match:
        raise ExtractorError(
            f"EBEM Groen Variabel: indicative incl-VAT row for {label!r} not found"
        )
    return to_float(match.group(1)) / 100.0


def _extract_yearly_fee_variable(text: str) -> float:
    """``Vaste vergoeding (jaarlijkse vaste bijdrage) 80,19 €/jaar 85,00 €/jaar``.

    The card prints both ex-VAT and incl-VAT columns; the registry's
    convention is to store the incl-VAT value (the second number).
    """
    match = re.search(
        r"Vaste vergoeding\s*\(jaarlijkse[^)]*\)\s+[\d,]+\s*€/jaar\s+([\d,]+)\s*€/jaar",
        text,
    )
    if not match:
        raise ExtractorError("EBEM Groen Variabel: 'Vaste vergoeding' row not found")
    return to_float(match.group(1))


def _extract_yearly_fee_abonnement(text: str) -> float:
    """``Abonnement 66,04 €/jaar 70 €/jaar`` -- B@sic+ and Dyn@mic share the label."""
    match = re.search(
        r"Abonnement\s+[\d,]+\s*€/jaar\s+([\d,]+)\s*€/jaar",
        text,
    )
    if not match:
        raise ExtractorError("EBEM: 'Abonnement' yearly fee row not found")
    return to_float(match.group(1))


def _extract_injection(text: str, contract: _ContractDef) -> InjectionRates | None:
    """Parse the injection formula. Belgian residential injection is VAT-exempt."""
    if contract.contract_id == "ebem_dynamic":
        match = re.search(
            rf"injectie alle uren\s+([\d,]+)\s+Belpex\s*15\s*[’']?\s*"
            rf"([{SIGN_CHARS}])\s*([\d,]+)",
            text,
        )
        formula_label = "Belpex15'"
    else:
        # Both 'Variabel' and 'B@sic+' use the same SPP0-weighted monthly
        # formula. The variable card prints the row twice (once per
        # product); the first match is enough -- they're identical.
        match = re.search(
            rf"Injectie alle uren\s+([\d,]+)\s+Belpex\s*([{SIGN_CHARS}])\s*([\d,]+)",
            text,
        )
        formula_label = "BelpexSPP0"
    if not match:
        return None
    factor_pdf = to_float(match.group(1))
    base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
    return InjectionRates(
        current=None,
        factor=factor_pdf * 10.0,
        base=base_pdf_cents / 100.0,
        formula=f"({factor_pdf} {formula_label} {match.group(2)} {match.group(3)}) c€/kWh ex-VAT",
    )


# ---- taxes ------------------------------------------------------------------


def _extract_federal_taxes(text: str) -> tuple[float, float]:
    """Return (federal_excise, energy_contribution) in EUR/kWh.

    The card prints residential federal excise across four kWh bands;
    the 0-3 MWh tier is what residential customers pay (``0-3 MWH``,
    capital "MWH" only on this row -- the others use lowercase "MWh").
    Energy contribution sits next to the residential energy-fund row
    on a single visual line (``Beschermende ... €0 0,20417``).
    """
    excise = re.search(r"0-3\s+MWH\s+([\d,]+)", text)
    contribution = re.search(
        r"Beschermende klanten[\s\S]+?€0\s+([\d,]+)",
        text,
    )
    if excise is None:
        raise ExtractorError("EBEM: federal excise (0-3 MWH) row not found")
    if contribution is None:
        raise ExtractorError("EBEM: federal energy contribution row not found")
    return to_float(excise.group(1)) / 100.0, to_float(contribution.group(1)) / 100.0


def _extract_flanders_renewables(text: str) -> float:
    """``Bijdrage groene stroom`` + ``Bijdrage WKK`` combined contribution.

    The card prints both the ex-VAT total (``1,520 c€/kWh excl. BTW``)
    and the incl-VAT total (``1,6112 c€/kWh incl. BTW 6%``). Read the
    incl-VAT value directly so we don't double-apply VAT.
    """
    match = re.search(
        r"([\d,]+)\s*c€/kWh\s+incl\.?\s*BTW\s*6%",
        text,
    )
    if not match:
        raise ExtractorError(
            "EBEM: Flanders renewables 'Totale bijdrage incl. BTW 6%' value not found"
        )
    return to_float(match.group(1)) / 100.0


# ---- DSO row parsers --------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}


def _extract_dsos(text: str, contract: _ContractDef) -> dict[str, DsoOverlay]:
    """Read the digital-meter Fluvius rows.

    Variable card has BOTH analog and digital meter tables; dynamic card
    has digital only. We anchor on the ``DIGITALE METER`` heading and
    read each DSO row's 4 numbers:
        capacity (€/kW/jaar) | netkosten (c€/kWh) |
        netkosten excl. nacht (c€/kWh) | tarief databeheer (€/jaar)

    For the variable card we additionally walk the ``ANALOGE METER``
    table to attach the prosumer rate (``prosumer_eur_per_kva_year``);
    on the dynamic card that column doesn't exist (SMR3-only product).
    """
    digital_section = re.search(
        r"DIGITALE METER([\s\S]+?)(?:Belastingen|ANALOGE METER|$)", text
    )
    if not digital_section:
        raise ExtractorError("EBEM: 'DIGITALE METER' table heading not found")

    prosumer_by_key: dict[str, float] = {}
    if contract.pdf_kind == "elek":
        analog_section = re.search(r"ANALOGE METER([\s\S]+?)DIGITALE METER", text)
        if analog_section:
            for label, key in _FLANDERS_LABELS.items():
                row = re.search(
                    rf"{re.escape(label)}\s+"
                    + r"[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+[\d.,]+\s+([\d.,]+)",
                    analog_section.group(1),
                )
                if row:
                    prosumer_by_key[key] = to_float(row.group(1))

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            digital_section.group(1),
        )
        if not row:
            continue
        capacity = to_float(row.group(1))
        kwh_total = to_float(row.group(2)) / 100.0
        kwh_excl_night = to_float(row.group(3)) / 100.0
        data_mgmt_year = to_float(row.group(4))
        out[key] = DsoOverlay(
            distribution_single=kwh_total,
            distribution_exclusive_night=kwh_excl_night,
            transport=0.0,
            data_management_per_year=data_mgmt_year,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer_by_key.get(key),
        )
    return out


# ---- registry entry ---------------------------------------------------------


_EBEM_REGIONS = frozenset({REGION_FLANDERS})

EXTRACTOR = SupplierExtractor(
    id="ebem",
    label="EBEM",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_EBEM_REGIONS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
