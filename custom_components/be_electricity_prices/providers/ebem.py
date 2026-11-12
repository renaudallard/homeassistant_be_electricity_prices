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

import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any

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
from ._ebem_cards import (
    _PREV_INDEX_RE,
    _PREV_SPP_RE,
    _VAT_PATTERNS,
    _extract_energy,
    _extract_injection,
)
from ._parse import (
    excise_tier_bands,
    numeric_row,
    parse_prosumer_column,
    require_every_area,
    running_month_short_of_an_area,
    to_float,
)
from ._pdf import (
    NL_MONTHS,
    fetch_pdf_text_layout,
    fetch_text,
    is_transient_fetch_error,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    EnergyRates,
    InjectionRates,
    TariffKind,
    VariableRates,
    vat_basis,
)
from ._settle import settled_injection
from ._validity import (
    archive_validity_check,
    scan_month_end,
)
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_LISTING_URL = "https://www.ebem.be/tarieven/"
_PDF_BASE = "https://www.ebem.be"

# EBEM links each monthly PDF as ``/media/<hash>/ebem_tariefkaart-{kind}-MM-YYYY.pdf``
# from the listing page. Captures (path, kind, MM, YYYY). The 2026-01 dynamic
# file is named ``ebem_tariefkaart-dynamic_01-2026.pdf`` (underscore between
# kind and MM): accept either separator so the archive walker doesn't lose
# that month silently. The kind group is open-ended so a future third
# PDF kind (e.g. ``fix`` if Ebem revives fixed contracts) surfaces in
# ``discover()`` even though only ``elek`` and ``dynamic`` map to contract ids.
_PDF_RE = re.compile(
    r'href="(/media/[^"]+/ebem_tariefkaart-([a-z]+)[-_](\d{2})-(\d{4})\.pdf)"',
    re.IGNORECASE,
)

# Non-electricity PDF kinds EBEM publishes on the same listing page;
# this integration is electricity-only, so drop them from discover()
# instead of routing them through the catalog drift alert.
_NON_ELEC_KINDS: frozenset[str] = frozenset({"gas", "aardgas"})


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
    region: str,  # EBEM only sells in Flanders.
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
    snap = parse_snapshot(contract_id, text, url, label)
    # Here and not in the reader: a past month's card keeps a short table.
    require_every_area("EBEM", snap, _FLANDERS_LABELS.values())
    return snap


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # EBEM only sells in Flanders.
    year_month: date,
) -> SupplierSnapshot | None:
    """Return the published snapshot for a specific (year, month).

    Walks the same listing page, resolves the URL whose filename matches
    the requested ``MM-YYYY``, parses, and validates that the parsed
    ``valid_until`` falls in the requested month: a defensive check
    against a CDN-substituted current card mis-billing past consumption.
    Returns ``None`` when the month isn't published (or the validity
    cross-check rejects the served PDF).
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    found = await _archived_card(session, contract_id, contract, year_month, html)
    if found is None:
        return None
    snap, _text = found
    if running_month_short_of_an_area(snap.dsos, _FLANDERS_LABELS.values(), year_month):
        return None
    return await _settle_on_published_index(
        session, contract_id, contract, snap, year_month, html
    )


async def _archived_card(
    session: aiohttp.ClientSession,
    contract_id: str,
    contract: _ContractDef,
    year_month: date,
    html: str,
) -> tuple[SupplierSnapshot, str] | None:
    """The validated card for ``year_month`` and its text, or ``None``.

    Takes the listing ``html`` rather than fetching it, so settling a month on
    the next month's card costs one PDF and not a second listing round trip.
    """
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
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    checked = archive_validity_check(snap, text, year_month)
    return None if checked is None else (checked, text)


def published_index(text: str) -> float | None:
    """The index the card says the month before it settled at, in EUR/kWh."""
    match = _PREV_INDEX_RE.search(text)
    return None if match is None else to_float(match.group(1)) / 1000.0


def published_spp_index(text: str) -> float | None:
    """The SPP0 the card says the month before it settled at, in EUR/kWh.

    The feed-in leg's own index, and a different number from
    :func:`published_index`: the consumption side weights Belpex by the Flemish
    residential load profile, the credit by the solar production profile, and
    in August 2026 those were 135,84 and 79,11 EUR/MWh. Trevion publishes the
    same 79,11 for that month on its own card, which is the cross-check that
    this is the market's settled figure rather than an EBEM house number.
    """
    match = _PREV_SPP_RE.search(text)
    return None if match is None else to_float(match.group(1)) / 1000.0


def _settled_on(
    energy: VariableRates, index: float, factor: float, base: float
) -> VariableRates:
    """Every register the card prices, recomputed at ``index``.

    One coefficient pair per register, not one for the card: the bi-hourly and
    exclusive-night rows carry their own, so rewriting ``current`` alone would
    settle a mono meter and leave a bi-hourly one on the estimate.
    """

    def rate(
        value: float | None, factor: float | None, base: float | None
    ) -> float | None:
        if value is None or factor is None or base is None:
            return value
        return factor * index + base

    return replace(
        energy,
        # The mono pair is checked by the caller, so this one always resolves.
        current=factor * index + base,
        peak=rate(energy.peak, energy.formula_factor_peak, energy.formula_base_peak),
        offpeak=rate(
            energy.offpeak, energy.formula_factor_offpeak, energy.formula_base_offpeak
        ),
        exclusive_night=rate(
            energy.exclusive_night,
            energy.formula_factor_exclusive_night,
            energy.formula_base_exclusive_night,
        ),
        index_realised=index,
    )


async def _settle_on_published_index(
    session: aiohttp.ClientSession,
    contract_id: str,
    contract: _ContractDef,
    snap: SupplierSnapshot,
    year_month: date,
    html: str,
) -> SupplierSnapshot:
    """Bill an archived month at the index it actually settled at.

    EBEM's card states that the delivery month's index is not known when it is
    published, so the rates it prints are the formula at the month BEFORE it,
    and it labels the columns "GESCHATTE". The card of the following month
    names what this month closed at, and that is the figure EBEM invoices. So
    the archived month takes it: every register is recomputed from its own
    coefficients and ``index_realised`` records the value.

    Measured over the nine archived 2026 cards, the printed rate for a month
    was the settled rate of the month before it to within 5e-7 every time, and
    the year-to-date at 3.500 kWh ran 16,55 EUR under after eight months
    because the index trended up across them.

    While the next card is not out (the running month, or the first days after
    it closes) the printed estimate stands and the snapshot is flagged
    ``provisional``, so the monthly cache asks again after its TTL rather than
    filing an estimate as a closed month's fact. A following card that prints
    no index at all leaves the month alone: retrying cannot conjure a footnote
    a re-render dropped.
    """
    energy = snap.energy
    injection = snap.injection
    if not _settleable(energy, injection):
        return snap
    following = date(
        year_month.year + (year_month.month == 12), year_month.month % 12 + 1, 1
    )
    found = await _archived_card(session, contract_id, contract, following, html)
    if found is None:
        return replace(snap, provisional=True)
    changed: dict[str, Any] = {}
    if (
        isinstance(energy, VariableRates)
        and energy.formula_factor is not None
        and energy.formula_base is not None
    ):
        index = published_index(found[1])
        if index is not None:
            changed["energy"] = _settled_on(
                energy, index, energy.formula_factor, energy.formula_base
            )
    if injection is not None and injection.spp_indexed:
        spp = published_spp_index(found[1])
        if spp is not None:
            changed["injection"] = settled_injection(injection, spp)
    return replace(snap, **changed) if changed else snap


def _settleable(energy: EnergyRates, injection: InjectionRates | None) -> bool:
    """True when either leg could be settled on the following card's figures.

    Asked before that card is fetched, so the dynamic contract, which is
    indexed on neither, pays for no download and is never flagged provisional
    because next month is not out yet.
    """
    if (
        isinstance(energy, VariableRates)
        and energy.formula_factor is not None
        and energy.formula_base is not None
    ):
        return True
    return injection is not None and injection.spp_indexed


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # EBEM only sells in Flanders.
) -> str | None:
    """The card URL the fetcher would resolve for ``contract_id``.

    The listing sends neither ``Last-Modified`` nor ``ETag``, so a header
    probe always came back ``None`` and the card waited on the 24 h TTL. The
    URL carries an opaque media hash that changes with every upload, so it
    is the signal instead. ``None`` on a failed resolve, so the TTL takes
    over.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    try:
        url, _label = await _find_latest(session, contract.pdf_kind)
    except ExtractorError:
        return None
    return url


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return EBEM contract ids visible on the listing page.

    Maps the two distinct electricity PDF kinds to contract ids:
      - any ``elek`` PDF surfaces ``ebem_variable`` and ``ebem_basic_plus``
        (both products live in the same card),
      - any ``dynamic`` PDF surfaces ``ebem_dynamic``.

    Non-electricity kinds on the listing (``gas`` / ``aardgas`` / etc.)
    are out of scope for this integration and dropped silently. A
    future electricity PDF kind (e.g. ``fix`` for a fixed contract,
    the variable card explicitly notes Ebem stopped selling those
    for now) surfaces verbatim so live_check files a tracking issue.
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
        elif kind in _NON_ELEC_KINDS:
            continue
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
    energy = _extract_energy(text, contract_id)
    injection = _extract_injection(text, contract_id)
    federal_excise, excise_bands, energy_contribution = _extract_federal_taxes(text)
    flanders_renewables = _extract_flanders_renewables(text)
    card_vat, assumed_vat = vat_basis(printed_vat_rate(text, *_VAT_PATTERNS), energy)
    return SupplierSnapshot(
        supplier="ebem",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text, contract),
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            federal_excise_bands=excise_bands,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=0.0,
            brussels_renewables=0.0,
            region_connection_fee=0.0,
            energy_fund_eur_per_month=0.0,
            vat_rate=0.0,
            card_vat_rate=card_vat,
            assumed_vat_rate=assumed_vat,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=_extract_validity(text),
        injection=injection,
    )


_DUTCH_MONTHS: dict[str, int] = {name: i for i, name in enumerate(NL_MONTHS, 1)}


def _extract_validity(text: str) -> date | None:
    """Return the last day of the printed Dutch month + year, e.g. ``mei 2026``.

    EBEM's tariff cards have no validity-keyword anchor (``geldig`` /
    ``valable``) that ``_validity.parse_valid_until`` would key on, so the
    shared helper would return ``None``. The card title ends with
    ``<maand> <jaar>``; parse that directly.
    """
    # The card title ends with "<maand> <jaar>" and has no validity
    # keyword; scan_month_end lowercases and skips the dynamic card's
    # colliding "VERSIE 2026" token, returning the first real month. A
    # None here silently skips the past-month CDN-substitution cross-check
    # in fetch_for_month.
    return scan_month_end(text, _DUTCH_MONTHS, limit=600)


# ---- taxes ------------------------------------------------------------------


def _extract_federal_taxes(
    text: str,
) -> tuple[float, tuple[tuple[float, float], ...] | None, float]:
    """Return (federal_excise, federal_excise_bands, energy_contribution)
    in EUR/kWh.

    The card prints residential federal excise across four kWh bands; the
    0-3 MWh tier is the excise (``0-3 MWH``, capital "MWH" only on this row:
    the others use lowercase "MWh"), and the table from it is read into
    bands while it falls by volume (until July 2026).
    Energy contribution sits next to the residential energy-fund row
    on a single visual line (``Beschermende ... €0 0,20417``): the first
    "€0" figure past the label, found in a pass of its own. As one pattern,
    a card without it was read to its end again from every label.
    """
    excise = re.search(r"0-3\s+MWH\s+([\d,.]+)", text)
    head = "Beschermende klanten"
    at = text.find(head)
    zero = re.compile(r"€0\s+([\d,.]+)")
    contribution = None if at < 0 else zero.search(text, at + len(head) + 1)
    if excise is None:
        raise ExtractorError("EBEM: federal excise (0-3 MWH) row not found")
    if contribution is None:
        raise ExtractorError("EBEM: federal energy contribution row not found")
    rate = to_float(excise.group(1)) / 100.0
    return (
        rate,
        excise_tier_bands(text, rate, start=excise.start(), within=excise.end()),
        to_float(contribution.group(1)) / 100.0,
    )


def _extract_flanders_renewables(text: str) -> float:
    """``Bijdrage groene stroom`` + ``Bijdrage WKK`` combined contribution.

    The card prints both the ex-VAT total (``1,520 c€/kWh excl. BTW``)
    and the incl-VAT total (``1,6112 c€/kWh incl. BTW 6%``). Read the
    incl-VAT value directly so we don't double-apply VAT. The VAT
    percentage in the row label is whatever the card printed - match
    any 1-2 digit number so a future Belgian VAT change doesn't fail
    the snapshot fetch.

    Anchor on ``Totale bijdrage`` so a future footnote that prints
    another ``incl. BTW N%`` line (e.g. for a different fee) can't
    shadow the renewables value.
    """
    match = re.search(
        r"Totale bijdrage[\s\S]{0,200}?([\d,.]+)\s*c€/kWh\s+incl\.?\s*BTW\s*\d+\s*%",
        text,
    )
    if not match:
        raise ExtractorError(
            "EBEM: Flanders renewables 'Totale bijdrage incl. BTW' value not found"
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
        # From the analog heading to the first digital one past it, found in
        # two passes: as one pattern, a card without the second heading was
        # read to its end again from every first one.
        head = "ANALOGE METER"
        at = text.find(head)
        end = -1 if at < 0 else text.find("DIGITALE METER", at + len(head) + 1)
        if end >= 0:
            prosumer_by_key = parse_prosumer_column(
                text[at + len(head) : end], _FLANDERS_LABELS
            )

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = numeric_row(digital_section.group(1), label, 4)
        if not row:
            continue
        capacity = to_float(row[0])
        kwh_total = to_float(row[1]) / 100.0
        kwh_excl_night = to_float(row[2]) / 100.0
        data_mgmt_year = to_float(row[3])
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

# Contracts whose feed-in credit indexes on a MONTHLY mean. The credit
# resolves against ENTSO-E spots the energy leg never fetches, so the config
# flow has to offer the optional key or the formula can never resolve and every
# path falls back to the card's printed figure. See
# ``Contract.spot_indexed_injection``.
_MONTH_INDEXED_INJECTION = frozenset({"ebem_variable", "ebem_basic_plus"})
# The same two on the CONSUMPTION side: their card states the delivery month's
# index is not known while the month runs, prints "GESCHATTE" columns computed
# from the month before, and the following card names what the month closed at.
_MONTH_INDEXED_ENERGY = _MONTH_INDEXED_INJECTION


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=5.3,
    settles_on_next_card=True,
    id="ebem",
    label="EBEM",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_EBEM_REGIONS,
            spot_indexed_injection=c.contract_id in _MONTH_INDEXED_INJECTION,
            month_indexed_energy=c.contract_id in _MONTH_INDEXED_ENERGY,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
