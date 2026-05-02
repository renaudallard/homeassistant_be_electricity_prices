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

"""Bolt Belgium tariff card extractor.

Bolt publishes tariff cards at predictable URLs:

    https://files.boltenergie.be/pricelists/fix/<slug>_res_el_fr_<YYYYMM>.pdf
    https://files.boltenergie.be/pricelists/var/<slug>_res_el_fr_11.pdf

Fixed contracts roll monthly via the YYYYMM suffix; variable contracts
use a stable version-number suffix (``_11`` today). Each PDF covers all
three regions in one document - same convention as Eneco.

Bolt's PDFs are visually rich (5 MB each) with rotated columns and a
column-major text layout that pypdf can't read. The extractor goes
through ``pdfplumber`` for layout-aware extraction.

Bolt's price model deviates from the rest in two ways: the fixed fee
is billed per MONTH (``Frais de plateforme 10,99 €/mois``) so the
extractor multiplies by 12 to fit the integration's annual fee
convention, and the Flanders renewables value is split across two
separate lines (``Certificats verts`` + ``WKK``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiohttp

from ..const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA
from ._pdf import USER_AGENT, fetch_pdf_text_layout, parse_valid_until, to_float
from .base import (
    Contract,
    DsoOverlay,
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

_LOGGER = logging.getLogger(__name__)

_BASE_URL = "https://files.boltenergie.be/pricelists"
_LISTING_URL = "https://www.boltenergie.be/fr/listes-des-prix"
_VARIABLE_SUFFIX = "11"  # current variable-card version


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    folder: str  # 'fix' or 'var'
    slug: str  # filename prefix


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("bolt_fix", "Bolt Fixe (1 year)", "fixed", "fix", "fix"),
    _ContractDef(
        "bolt_plenty_fix", "Bolt Plenty Fixe (1 year)", "fixed", "fix", "plenty_fix"
    ),
    _ContractDef("bolt_variable", "Bolt Variable", "variable", "var", "bolt"),
    _ContractDef("bolt_plenty", "Bolt Plenty Variable", "variable", "var", "plenty"),
    _ContractDef("bolt_online", "Bolt Online", "variable", "var", "online"),
    _ContractDef(
        "bolt_plenty_online",
        "Bolt Plenty Online",
        "variable",
        "var",
        "plenty_online",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(contract: _ContractDef, suffix: str | None = None) -> str:
    if contract.folder == "fix":
        # Fixed cards roll monthly; default to the current YYYYMM.
        suffix = suffix or datetime.now(UTC).strftime("%Y%m")
    else:
        suffix = suffix or _VARIABLE_SUFFIX
    return f"{_BASE_URL}/{contract.folder}/{contract.slug}_res_el_fr_{suffix}.pdf"


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Bolt's PDFs cover every region.
) -> str | None:
    """Cheap freshness probe: HEAD the listing page, return its ETag.

    Bolt's listing returns a stable ETag and the server honours
    ``If-None-Match`` with a 304 response. We just want a key that flips
    on supplier changes, so reading the ETag header on a HEAD round-trip
    is enough.
    """
    if contract_id not in _CONTRACTS_BY_ID:
        return None
    try:
        async with session.head(
            _LISTING_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=10),
            allow_redirects=True,
        ) as resp:
            if resp.status >= 400:
                return None
            return resp.headers.get("ETag") or resp.headers.get("Last-Modified")
    except aiohttp.ClientError:
        return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return ``{folder}/{slug}`` for every residential electricity card.

    Bolt's prices listing page links every PDF directly. Filter to
    residential electricity (``_res_el_fr_``) and extract the
    ``<folder>/<slug>`` prefix; live_check diffs against the registry's
    ``{c.folder + '/' + c.slug for c in _CONTRACTS}`` set.
    """
    try:
        async with session.get(
            _LISTING_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status >= 400:
                return set()
            html = await resp.text()
    except aiohttp.ClientError:
        return set()
    return {
        f"{folder}/{slug}"
        for folder, slug in re.findall(
            r"pricelists/(fix|var)/([a-z_]+)_res_el_fr_", html
        )
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the latest Bolt PDF for ``contract_id`` (covers every region)."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Bolt contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]

    url = _document_url(contract)
    try:
        text = await fetch_pdf_text_layout(session, url)
    except ExtractorError:
        # Fixed cards may not be published yet on the 1st of the month;
        # fall back to the previous month.
        if contract.folder != "fix":
            raise
        previous = (datetime.now(UTC).replace(day=1) - timedelta(days=1)).strftime(
            "%Y%m"
        )
        url = _document_url(contract, suffix=previous)
        text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    if contract_id not in _CONTRACTS_BY_ID:
        raise ExtractorError(f"unknown Bolt contract {contract_id!r}")
    contract = _CONTRACTS_BY_ID[contract_id]
    # Bolt's PDFs sprinkle U+2028 LINE SEPARATOR characters where one
    # would expect a newline; normalize to '\n' so a single set of
    # regexes covers every block.
    text = text.replace(" ", "\n")

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, region_connection_fee = _extract_taxes(
        text, region
    )
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0
    flanders_renewables, wallonia_renewables, brussels_renewables = _extract_renewables(
        text
    )
    if region != REGION_FLANDERS:
        flanders_renewables = 0.0
    if region != REGION_WALLONIA:
        wallonia_renewables = 0.0
    if region != REGION_BRUSSELS:
        brussels_renewables = 0.0

    if region == REGION_FLANDERS:
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        dsos = _extract_wallonia_dsos(text)
    else:
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="bolt",
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
            energy_fund_eur_per_month=energy_fund,
            vat_rate=0.0,
        ),
        source_url=source_url,
        fetched_at_iso=datetime.now(UTC).isoformat(timespec="seconds"),
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


def _extract_yearly_fee(text: str) -> float:
    """Bolt prints a monthly platform fee (``€ 10,99 / mois``); convert to /year."""
    match = re.search(r"€\s*(\d+[.,]\d+)\s*/\s*mois", text)
    return to_float(match.group(1)) * 12.0 if match else 0.0


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    # Bolt's 'Prix mensuel' line is the current month's price for all
    # contract kinds. Static cards have only this; variable cards also
    # show 'Prix annuel estimé' which we ignore. Layout: two adjacent
    # numbers (mono+jour) then exclusive-night somewhere nearby.
    match = re.search(r"Prix mensuel\s+([\d.,]+)\s+([\d.,]+)\b", text)
    if not match:
        raise ExtractorError(f"could not parse Bolt {kind} consumption block")
    mono = to_float(match.group(1)) / 100.0
    excl = to_float(match.group(2)) / 100.0
    # The bi-horaire row sits inside the 'Prix de l'électricité verte'
    # block as "<jour> <nuit>" on a single line, immediately followed by
    # the SECOND "Jour Nuit" header (the injection one). Anchoring on
    # that trailing header skips over the annual-estimate values that
    # pdfplumber renders vertically right above.
    bihoraire = re.search(
        r"([\d.,]+)\s+([\d.,]+)\s*\n\s*Jour\s+Nuit",
        text,
    )
    peak = to_float(bihoraire.group(1)) / 100.0 if bihoraire else mono
    offpeak = to_float(bihoraire.group(2)) / 100.0 if bihoraire else mono

    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    if kind == "variable":
        return VariableRates(
            current=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl,
            yearly_fixed_fee=yearly_fee,
        )
    # Bolt has no Dynamic product today, but keep the path explicit.
    raise ExtractorError(f"Bolt: dynamic kind not supported on {kind}")


def _extract_publication_month(text: str) -> str:
    match = re.search(r"^([A-ZÉÈÛ][a-zéèû]+\s+\d{4})\s*/", text, re.MULTILINE)
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Injection block: "Prix mensuel 5,31 4,03" appearing AFTER the
    # consumption block (the second 'Prix mensuel' in the document).
    matches = list(re.finditer(r"Prix mensuel\s+([\d.,]+)\s+([\d.,]+)\b", text))
    if len(matches) < 2:
        return None
    inj = matches[1]
    current = to_float(inj.group(1)) / 100.0
    return InjectionRates(current=current, factor=None, base=None, formula=None)


# ---- taxes --------------------------------------------------------------------


def _extract_taxes(text: str, region: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, region_connection_fee).

    Bolt prints taxes as 3-column rows (Flandres / Wallonie / Bruxelles).
    Caller (parse_snapshot) already normalised any Unicode line
    separators (U+2028) to regular newlines, so the regexes below
    see a uniform layout.
    """
    excise_match = re.search(
        r"Droit d['’]accise spécial[^\n]*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
    )
    contribution_match = re.search(
        r"Contribution sur l['’]énergie[^\n]*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    # Connection fee row prints two single-digit footnote refs ahead of
    # the values: "Redevance de raccordement (c€/kWh) 6 7 - 0,075 -".
    # ``(?:\d\s+)*`` skips the footnote refs; the three trailing tokens
    # are FL/WAL/BX (some are "-" when not applicable).
    connection_match = re.search(
        r"Redevance de raccordement[^\n]*?\(c€/kWh\)\s*(?:\d\s+)*"
        r"(-|[\d.,]+)\s+(-|[\d.,]+)\s+(-|[\d.,]+)",
        text,
    )

    def _per_region(match: re.Match[str] | None, region: str) -> float:
        if match is None:
            return 0.0
        index = {REGION_FLANDERS: 1, REGION_WALLONIA: 2, REGION_BRUSSELS: 3}[region]
        token = match.group(index).strip()
        if token == "-" or not token:
            return 0.0
        return to_float(token) / 100.0

    excise = _per_region(excise_match, region)
    contribution = _per_region(contribution_match, region)
    connection = _per_region(connection_match, region)
    return excise, contribution, connection


def _extract_energy_fund(text: str) -> float:
    """Flanders 'Cotisation Fond énergie, résidentiel' is typically '-' (0)."""
    match = re.search(
        r"Cotisation Fond énergie, résidentiel[^\n]*\n\s*([\d.,-]+)",
        text,
    )
    if match is None or match.group(1).strip() == "-":
        return 0.0
    return to_float(match.group(1))


def _extract_renewables(text: str) -> tuple[float, float, float]:
    """Three columns under 'Certificats verts' + Flanders-only WKK row."""
    cert = re.search(
        r"Certificats verts\s*\(c€/kWh\)[^\n]*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)\s*\n\s*([\d.,]+)",
        text,
        re.S,
    )
    # WKK row: 'WKK (c€/kWh) 8 0,39 -' - skip the single-digit footnote
    # ref before capturing the Flanders value. The remaining ' -' tokens
    # are placeholders for Wallonia / Brussels (no WKK there).
    wkk = re.search(r"WKK\s*\(c€/kWh\)\s*\d?\s*([\d.,]+)", text)
    if cert is None:
        return 0.0, 0.0, 0.0
    fl_cents = to_float(cert.group(1))
    wal_cents = to_float(cert.group(2))
    bx_cents = to_float(cert.group(3))
    if wkk is not None:
        fl_cents += to_float(wkk.group(1))
    return fl_cents / 100.0, wal_cents / 100.0, bx_cents / 100.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS: dict[str, str] = {
    "Fluvius Antwerpen": "fluvius_antwerpen",
    "Fluvius Halle-Vilvoorde": "fluvius_halle_vilvoorde",
    "Fluvius Imewo": "fluvius_imewo",
    "Fluvius Kempen": "fluvius_iveka",
    "Fluvius Limburg": "fluvius_limburg",
    "Fluvius Midden-Vl": "fluvius_intergem",
    "Fluvius West": "fluvius_west",
    "Fluvius Zenne-Dijle": "fluvius_zenne_dijle",
}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Fluvius rows. Each has 8 numbers in this order:

      data_mgmt_classic | capacity_digital | dist_normal_digital |
      dist_excl_digital | terme_fixe_classic | dist_normal_classic |
      dist_excl_classic | prosumer

    pdfplumber sometimes splits the row vertically (one number per line);
    ``\\s+`` matches any whitespace incl newlines, so a single regex
    handles both layouts.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        data_mgmt = to_float(match.group(1))
        capacity = to_float(match.group(2))
        dist_normal = to_float(match.group(3))
        prosumer = to_float(match.group(8))
        out[key] = DsoOverlay(
            distribution_single=dist_normal / 100.0,
            transport=0.0,
            data_management_per_year=data_mgmt,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


# Bolt's PDFs have a pdfplumber row-alignment quirk: the rows labeled
# "TECTEO RESA" and "WAVRE" in the extracted text actually carry each
# other's values. Verified against the regulator's published rates and
# every other supplier's PDF. We swap the labels here so DSO lookups
# return the correct numbers. ``_extract_wallonia_dsos`` runs an
# additional runtime sanity check after parsing (RESA must remain
# cheaper than REW under the current Walloon tariff structure); if
# Bolt's PDF ever stops triggering the misalignment the check logs a
# WARNING so the swap can be removed.
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": "aieg",
    "AIESH": "aiesh",
    "ORES (Brabant Wallon)": "ores",
    "TECTEO RESA": "rew",
    "WAVRE": "resa",
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia rows. Each has 10 numbers:

    mono | jour | nuit | excl_nuit | PIC | MEDIUM | ECO | transport |
    terme_fixe (€/an) | prosumer (€/kVA/an)
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
            + r"([\d.,]+)\s+([\d.,]+)",
            text,
        )
        if not match:
            continue
        mono = to_float(match.group(1))
        peak = to_float(match.group(2))
        offpeak = to_float(match.group(3))
        pic = to_float(match.group(5))
        medium = to_float(match.group(6))
        eco = to_float(match.group(7))
        transport = to_float(match.group(8))
        terme_fixe = to_float(match.group(9))
        prosumer = to_float(match.group(10))
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            distribution_pic=pic / 100.0,
            distribution_medium=medium / 100.0,
            distribution_eco=eco / 100.0,
            transport=transport / 100.0,
            data_management_per_year=terme_fixe,
            prosumer_eur_per_kva_year=prosumer,
        )
    # Sanity check: under the swap, RESA's distribution_single must
    # remain strictly cheaper than REW's (regulator pattern that holds
    # for every Walloon tariff card we've parsed). If the inequality
    # ever flips, Bolt almost certainly fixed the upstream layout and
    # our compensating swap now inverts correct values -- log a
    # warning so the maintainer can drop the swap from
    # _WALLONIA_LABELS instead of silently mis-billing.
    resa = out.get("resa")
    rew = out.get("rew")
    if resa is None and rew is None:
        # Both rows missing: regex drift covers the whole table; the
        # rest of the parser will already have raised. Stay quiet
        # here.
        return out
    if resa is None or rew is None:
        # Only one of the two parsed -- the more dangerous case for
        # the swap, since the surviving row may now be carrying the
        # other DSO's values without anything else to compare it
        # against. Surface it so the maintainer can investigate.
        _LOGGER.warning(
            "Bolt RESA/REW row drift: only %s parsed; the label swap "
            "in _WALLONIA_LABELS may now be inverting %s's values",
            "resa" if resa is not None else "rew",
            "resa" if resa is not None else "rew",
        )
        return out
    if resa.distribution_single >= rew.distribution_single:
        _LOGGER.warning(
            "Bolt RESA/REW post-swap invariant tripped "
            "(resa=%.4f rew=%.4f); the upstream PDF may have been "
            "fixed and the label swap in _WALLONIA_LABELS likely "
            "needs to be removed",
            resa.distribution_single,
            rew.distribution_single,
        )
    return out


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """Sibelga row: ``Sibelga 9,96 9,96 7,53 7,53 2,27 14,73 -``.

    Layout: mono | jour | nuit | excl_nuit | transport | terme_fixe | prosumer (-)
    """
    match = re.search(
        r"Sibelga\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+([\d.,]+)\s+"
        r"([\d.,]+)\s+([\d.,]+)",
        text,
    )
    if not match:
        return {}
    mono = to_float(match.group(1))
    peak = to_float(match.group(2))
    offpeak = to_float(match.group(3))
    transport = to_float(match.group(5))
    terme_fixe = to_float(match.group(6))
    return {
        "sibelga": DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=peak / 100.0,
            distribution_offpeak=offpeak / 100.0,
            transport=transport / 100.0,
            data_management_per_year=terme_fixe,
        )
    }


EXTRACTOR = SupplierExtractor(
    id="bolt",
    label="Bolt",
    contracts=tuple(
        Contract(id=c.contract_id, label=c.label, kind=c.kind) for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    dso_keys=(
        tuple(_FLANDERS_LABELS.values())
        + tuple(_WALLONIA_LABELS.values())
        + ("sibelga",)
    ),
)
