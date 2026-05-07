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

"""Eneco Belgium tariff card extractor.

Eneco publishes one PDF per contract under

    https://cdn.eneco.be/downloads/nl/general/tk/BC_032_<NNNNNN>_NL_ENECO_POWER_<NAME>.pdf

The 6-digit issue number rotates every month (e.g. ``012604`` for the
April 2026 cards, ``012605`` for May 2026). Stale issues remain served
on the CDN, so we always resolve the live URL from the public listing
page instead of hardcoding it. The PDFs include the publication month
("Tariefkaart mei 2026"); all prices are VAT-inclusive (6 %).

Eneco serves Flanders and Wallonia only (no Brussels).
"""

from __future__ import annotations

import re
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
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    SIGN_CHARS,
    archive_validity_check,
    fetch_pdf_text,
    fetch_text,
    head_freshness_key,
    parse_sign,
    parse_valid_until,
    to_float,
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
    TaxOverlay,
    VariableRates,
)

_BASE_URL = "https://cdn.eneco.be/downloads/nl/general/tk"
_LISTING_URL = "https://eneco.be/nl/elektriciteit-gas/tariefkaarten"

# Dutch month names Eneco prints in the validity header. Used by
# fetch_for_month to confirm a CDN-served PDF actually mentions the
# requested month when parse_valid_until missed.
_NL_MONTHS = (
    "januari",
    "februari",
    "maart",
    "april",
    "mei",
    "juni",
    "juli",
    "augustus",
    "september",
    "oktober",
    "november",
    "december",
)


# Contract id -> the POWER_<NAME> token Eneco uses in its filenames.
_CONTRACT_SLUGS: dict[str, str] = {
    "power_fix": "FIX",
    "power_flex": "FLEX",
    "power_dynamic": "DYNAMIC",
}

# Wallonia DSO labels (column layout: 4 distribution + optional [MEDIUM PIC ECO]
# + transport + databeheer + prosument). ORES sub-zones share a uniform rate so
# we just keep the first one encountered as the canonical "ores" row.
_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "REGIE DE WAVRE": DSO_REW,
    "TECTEO RESA": DSO_RESA,
}

# Flanders Fluvius digital-meter sub-areas (column layout: Normaal,
# Uitsluitend nacht, SMR1 databeheer, SMR3 databeheer, capaciteitstarief,
# then two `-` placeholders). Each sub-area has its own distribution and
# capacity rates. Transport is not in the row; the Wallonia rows carry the
# (national) Elia transport value, which we propagate.
_FLUVIUS_LABELS: dict[str, str] = {
    "FLUVIUS HALLE VILVOORDE": DSO_FLUVIUS_HALLE_VILVOORDE,
    "FLUVIUS ANTWERPEN": DSO_FLUVIUS_ANTWERPEN,
    "FLUVIUS IMEWO": DSO_FLUVIUS_IMEWO,
    "FLUVIUS LIMBURG": DSO_FLUVIUS_LIMBURG,
    "FLUVIUS WEST": DSO_FLUVIUS_WEST,
    "FLUVIUS MIDDEN VLAANDEREN (INTERGEM)": DSO_FLUVIUS_INTERGEM,
    "FLUVIUS KEMPEN (IVEKA)": DSO_FLUVIUS_IVEKA,
    "FLUVIUS ZENNE DIJLE": DSO_FLUVIUS_ZENNE_DIJLE,
}

_NUM = r"(\d{1,3}(?:[\.,]\d{1,4})?)"
_WS = r"[\s\xa0]"


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Eneco's PDF covers every region in one document.
) -> SupplierSnapshot:
    """Fetch and parse the Eneco tariff card for ``contract_id``."""
    if contract_id not in _CONTRACT_SLUGS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    listing = await _fetch_listing(session)
    if listing is None:
        raise ExtractorError("could not fetch Eneco listing page")
    url = _resolve_url(listing, contract_id)
    if url is None:
        raise ExtractorError(
            f"Eneco listing page did not advertise a PDF for {contract_id!r}"
        )
    text = await fetch_pdf_text(session, url)
    return parse_snapshot(text, contract_id, url)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Eneco's PDF covers every region in one document.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Eneco card for a specific (year, month).

    The CDN keeps every monthly issue indefinitely under the URL pattern
    ``BC_032_<VOLYYMM>_NL_ENECO_POWER_<NAME>.pdf``. ``VOL`` is the
    issue number for that month: ``01`` for the first publication,
    ``02`` and higher for re-issues. Most months only ever have ``01``
    so that's tried first; on 404 or validity-mismatch we walk a
    small range of higher volumes before giving up. Returns ``None``
    when no volume in the range parses to a snapshot whose validity
    covers the requested month.
    """
    slug = _CONTRACT_SLUGS.get(contract_id)
    if slug is None:
        return None
    yymm = f"{year_month.year % 100:02d}{year_month.month:02d}"
    for volume in ("01", "02", "03", "04", "05"):
        issue = f"{volume}{yymm}"
        url = f"{_BASE_URL}/BC_032_{issue}_NL_ENECO_POWER_{slug}.pdf"
        # HEAD-probe first so a missing volume returns inside the
        # 10s probe budget instead of waiting on the 30s GET timeout
        # for each candidate; missing-month worst-case latency drops
        # from 5 x 30s = 150s to 5 x 10s ~= 50s under sustained CDN
        # issues, and is unchanged in the typical case (HEAD returns
        # 404 instantly).
        if await head_freshness_key(session, url) is None:
            continue
        try:
            text = await fetch_pdf_text(session, url)
        except ExtractorError:
            continue
        try:
            snap = parse_snapshot(text, contract_id, url)
        except ExtractorError:
            continue
        result = archive_validity_check(snap, text, year_month, month_names=_NL_MONTHS)
        if result is not None:
            return result
    return None


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Eneco's listing is region-agnostic.
) -> str | None:
    """Cheap freshness probe: the current PDF URL for ``contract_id``.

    Eneco's listing returns a per-request ETag (``Cache-Control: no-store``)
    so a header-only probe is unusable. The cheapest reliable probe is
    therefore "scrape the listing and return the resolved URL" - the
    issue number rotates monthly so the URL is the freshness signal.
    """
    if contract_id not in _CONTRACT_SLUGS:
        return None
    listing = await _fetch_listing(session)
    if listing is None:
        return None
    return _resolve_url(listing, contract_id)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the set of POWER_* product slugs from Eneco's listing page.

    Eneco's tariefkaarten page links every product as a direct PDF.
    Extract every ``BC_..._NL_ENECO_POWER_<NAME>.pdf`` and lower-case
    to match the registry's contract id (``power_fix``, etc.).
    """
    listing = await _fetch_listing(session)
    if listing is None:
        return set()
    return {
        f"power_{name.lower()}"
        for name in re.findall(r"BC_[\d_]+_NL_ENECO_POWER_([A-Z]+)\.pdf", listing)
    }


async def _fetch_listing(session: aiohttp.ClientSession) -> str | None:
    try:
        return await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return None


def _resolve_url(listing_html: str, contract_id: str) -> str | None:
    """Pick the live PDF URL for ``contract_id`` out of the listing page.

    The page may carry either bare filenames (``BC_..._POWER_FLEX.pdf``)
    or full hrefs; we accept both and reconstruct the absolute URL when
    only the filename is present. When several issues appear we keep the
    first match: the listing page only ever advertises one issue at a
    time, but a defensive single-shot keeps a future duplicate from
    silently switching to a stale revision.
    """
    slug = _CONTRACT_SLUGS.get(contract_id)
    if slug is None:
        return None
    full = re.search(
        rf"https?://[^\s\"'<>]+?/BC_[\d_]+_NL_ENECO_POWER_{slug}\.pdf",
        listing_html,
    )
    if full:
        return full.group(0)
    bare = re.search(rf"BC_[\d_]+_NL_ENECO_POWER_{slug}\.pdf", listing_html)
    if bare:
        return f"{_BASE_URL}/{bare.group(0)}"
    return None


def parse_snapshot(text: str, contract_id: str, source_url: str) -> SupplierSnapshot:
    """Parse already-extracted PDF text. Exposed for unit tests."""
    if contract_id not in _CONTRACT_SLUGS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    return SupplierSnapshot(
        supplier="eneco",
        contract=contract_id,
        energy=_extract_energy(text, contract_id),
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=_extract_publication_month(text),
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text, contract_id),
    )


def _extract_publication_month(text: str) -> str:
    match = re.search(r"Tariefkaart\s+([a-zA-Z]+\s+\d{4})", text)
    return match.group(1) if match else ""


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    if contract_id == "power_fix":
        return _extract_fixed(text)
    if contract_id == "power_flex":
        return _extract_variable(text)
    if contract_id == "power_dynamic":
        return _extract_dynamic(text)
    raise ExtractorError(f"unknown contract {contract_id!r}")


def _extract_fixed(text: str) -> FixedRates:
    pattern = re.compile(
        r"DAG\s+NACHT\s*\n*"
        rf"\s*{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
        re.S,
    )
    match = pattern.search(text)
    if not match:
        raise ExtractorError("could not parse Eneco fixed energy block")
    yearly_fee, single, day, night, exclusive = (
        to_float(match.group(i)) for i in range(1, 6)
    )
    return FixedRates(
        single=single / 100.0,
        peak=day / 100.0,
        offpeak=night / 100.0,
        exclusive_night=exclusive / 100.0,
        yearly_fixed_fee=yearly_fee,
    )


def _extract_variable(text: str) -> VariableRates:
    yearly_fee_match = re.search(
        r"\(€/jaar\)\s+VERBRUIK[^\n]*\n[^\n]*\n[^\n]*\n[^\n]*\n\s*" + _NUM,
        text,
        re.S,
    )
    monthly_match = re.search(rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+Maandprijs", text)
    formula_match = re.search(r"\((0,\d+)\s*X\s*BELPEX[\w\-]+\s*\+\s*(\d+,\d+)\)", text)
    if not yearly_fee_match or not monthly_match:
        raise ExtractorError("could not parse Eneco variable energy block")
    return VariableRates(
        current=to_float(monthly_match.group(1)) / 100.0,
        yearly_fixed_fee=to_float(yearly_fee_match.group(1)),
        formula=formula_match.group(0) if formula_match else None,
    )


def _extract_dynamic(text: str) -> DynamicRates:
    yearly_fee_match = re.search(
        r"Enkelvoudige meter\s*\n\s*" + _NUM,
        text,
    )
    # Capture the VAT multiplier the PDF actually applies (e.g. 1,06 for the
    # current 6% residential rate, 1,21 if Belgium reverts to 21% VAT).
    # Accept '+' or '-' between factor*BELPEX and base; the injection
    # branch already does, and a future card with a negative base must
    # not silently 404 the parser.
    formula_match = re.search(
        rf"\((0,\d+)\s*X\s*BELPEX[\w\-]+\s*([{SIGN_CHARS}])\s*(\d+(?:,\d+)?)\)\s*X\s*(\d+,\d+)",
        text,
    )
    if not yearly_fee_match or not formula_match:
        raise ExtractorError("could not parse Eneco dynamic energy block")
    factor_pdf = to_float(formula_match.group(1))
    base_pre_vat_cents = parse_sign(formula_match.group(2)) * to_float(
        formula_match.group(3)
    )
    vat_multiplier = to_float(formula_match.group(4))
    # PDF formula yields c€/kWh from BELPEX in €/MWh:
    #   energy_c_eur_kwh = (factor_pdf * BELPEX_eur_mwh + base_cents) * vat_mult
    # ENTSO-E client returns spot in EUR/kWh = BELPEX_eur_mwh / 1000.
    # Convert to: energy_eur_kwh = factor * spot_eur_kwh + base
    #   factor = factor_pdf * vat_mult * 1000 / 100 = factor_pdf * vat_mult * 10
    #   base   = base_cents  * vat_mult / 100
    base_eur_per_kwh = base_pre_vat_cents * vat_multiplier / 100.0
    factor_eur_per_kwh = factor_pdf * vat_multiplier * 10.0
    return DynamicRates(
        factor=factor_eur_per_kwh,
        base=base_eur_per_kwh,
        yearly_fixed_fee=to_float(yearly_fee_match.group(1)),
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    out: dict[str, DsoOverlay] = {}
    transport = _extract_transport(text)
    for pdf_label, key in _WALLONIA_LABELS.items():
        if key in out:
            continue
        row = _find_wallonia_row(text, pdf_label)
        if row is not None:
            out[key] = row
    for pdf_label, key in _FLUVIUS_LABELS.items():
        row = _find_fluvius_row(text, pdf_label, transport)
        if row is not None:
            out[key] = row
    return out


def _find_wallonia_row(text: str, label: str) -> DsoOverlay | None:
    """Wallonia rows carry 7 (Power Dynamic) or 10 (Power Fix) numbers.

    Layout: Enkelvoudig | Dag | Nacht | Uitsl. nacht | [MEDIUM PIC ECO] |
            Transport | Databeheer (€/jaar) | Prosument (€/kVA/jaar)
    """
    escaped = re.escape(label)
    pattern = re.compile(
        rf"{escaped}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}"
        rf"(?:\s+{_NUM}\s+{_NUM}\s+{_NUM})?\s+{_NUM}\s+{_NUM}\s+{_NUM}",
        re.S | re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    groups = [g for g in match.groups() if g is not None]
    # When Eneco prints the Tarif Impact triplet (Power Fix layout) the
    # row carries 10 columns; Power Dynamic only has 7. Eneco's column
    # order is MEDIUM | PIC | ECO (different from OCTA+/Bolt where it's
    # PIC | MEDIUM | ECO).
    medium = pic = eco = None
    if len(groups) == 10:
        medium = to_float(groups[4]) / 100.0
        pic = to_float(groups[5]) / 100.0
        eco = to_float(groups[6]) / 100.0
    return DsoOverlay(
        distribution_single=to_float(groups[0]) / 100.0,
        distribution_peak=to_float(groups[1]) / 100.0,
        distribution_offpeak=to_float(groups[2]) / 100.0,
        distribution_exclusive_night=to_float(groups[3]) / 100.0,
        distribution_pic=pic,
        distribution_medium=medium,
        distribution_eco=eco,
        transport=to_float(groups[-3]) / 100.0,
        data_management_per_year=to_float(groups[-2]),
        prosumer_eur_per_kva_year=to_float(groups[-1]),
    )


def _find_fluvius_row(text: str, label: str, transport: float) -> DsoOverlay | None:
    """Fluvius digital-meter rows: 5 numbers + 2 placeholder dashes.

    Layout: Normaal | Uitsl. nacht | SMR1 (€/jaar) | SMR3 (€/jaar) |
            Capaciteitstarief (€/kW/jaar) | -- | --
    """
    escaped = re.escape(label)
    # Anchor on the digital-meter Fluvius section so we don't accidentally
    # pick up the analoge meter row that follows further down.
    digital_match = re.search(
        rf"DIGITALE METER.*?{escaped}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
        text,
        re.S | re.IGNORECASE,
    )
    if not digital_match:
        return None
    return DsoOverlay(
        distribution_single=to_float(digital_match.group(1)) / 100.0,
        # Post-capacity-tariff Flemish meters bill at a single rate, so
        # peak / off-peak don't apply; the Uitsl. nacht column (group 2)
        # is the dedicated exclusive-night meter circuit rate.
        distribution_peak=None,
        distribution_offpeak=None,
        distribution_exclusive_night=to_float(digital_match.group(2)) / 100.0,
        transport=transport,
        data_management_per_year=to_float(digital_match.group(4)),
        capacity_eur_per_kw_year=to_float(digital_match.group(5)),
    )


def _extract_transport(text: str) -> float:
    """Pull the (national) Elia transport rate from the first Wallonia row.

    The Fluvius rows omit transport from their layout, but it is the same
    regulated value across all DSOs.
    """
    aieg = _find_wallonia_row(text, "AIEG")
    if aieg is not None:
        return aieg.transport
    # Fallback: any Wallonia row.
    for label in _WALLONIA_LABELS:
        row = _find_wallonia_row(text, label)
        if row is not None:
            return row.transport
    return 0.0


def _extract_taxes(text: str) -> TaxOverlay:
    tier_match = re.search(
        rf"Verbruik tussen{_WS}*\n*{_WS}*0{_WS}+en{_WS}+3\.000{_WS}+kWh{_WS}*\n*{_WS}*"
        + _NUM
        + rf"{_WS}+"
        + _NUM,
        text,
    )
    if not tier_match:
        raise ExtractorError("could not parse Eneco federal excise block")
    excise = to_float(tier_match.group(1)) / 100.0
    contribution = to_float(tier_match.group(2)) / 100.0

    wkk = re.search(
        rf"Bijdrage groene stroom en WKK{_WS}+Vlaanderen.{{0,80}}?{_NUM}",
        text,
        re.S,
    )
    wallonia_renewables = re.search(
        rf"Bijdrage groene stroom Wallonië.{{0,80}}?{_NUM}",
        text,
        re.S,
    )
    connection = re.search(
        rf"Aansluitingsvergoeding elektriciteit.+?"
        rf"\(€cent/kWh\){_WS}*\n?{_WS}*{_NUM}",
        text,
        re.S,
    )
    fund = re.search(
        rf"Standaard tarief{_WS}*\n{_WS}*\(domicilieadres\){_WS}+{_NUM}",
        text,
    )
    return TaxOverlay(
        federal_excise=excise,
        energy_contribution=contribution,
        flanders_renewables=to_float(wkk.group(1)) / 100.0 if wkk else 0.0,
        wallonia_renewables=(
            to_float(wallonia_renewables.group(1)) / 100.0
            if wallonia_renewables
            else 0.0
        ),
        region_connection_fee=(
            to_float(connection.group(1)) / 100.0 if connection else 0.0
        ),
        energy_fund_eur_per_month=to_float(fund.group(1)) if fund else 0.0,
        vat_rate=0.0,
    )


def _extract_injection(text: str, contract_id: str) -> InjectionRates | None:
    """Parse the injection block of an Eneco tariff card.

    Layout (every contract):

      INJECTIE / VALORISATIE
       ... [optional 'Zie afname' recap block on Power Dynamic] ...
       INJECTIE
        <c/kWh value(s)> Geschatte jaarprijs
        [<c/kWh value(s)> Maandprijs]                    (Fix/Flex only)
        <factor> X BELPEX[-H] [+-] <base> Tariefformule

    Power Fix and Flex use Belpex monthly; Power Dynamic uses Belpex-H
    (hourly). Injection is VAT-exempt for residential, so values are
    EUR/kWh = c/kWh / 100.
    """
    anchor = re.search(r"INJECTIE\s*/\s*VALORISATIE", text)
    if not anchor:
        return None
    section = text[anchor.end() :]
    # Restrict to the section before the next ALL-CAPS heading so unrelated
    # blocks ('ENERGIEDELEN', 'BELASTINGEN', ...) don't pollute the matches.
    cutoff = re.search(
        r"\n(?:ENERGIEDELEN|BELASTINGEN|TAXES|De opgewekte|Voorwaarden)", section
    )
    if cutoff:
        section = section[: cutoff.start()]

    # Numeric prefix only: dodges "Zie afname Geschatte jaarprijs" lines on
    # Power Dynamic (the Zie-afname block is the consumption recap).
    maand = re.search(rf"((?:{_NUM}\s+){{1,4}}){_WS}*Maandprijs", section)
    yearly = re.search(rf"((?:{_NUM}\s+){{1,4}}){_WS}*Geschatte jaarprijs", section)
    formula = re.search(
        rf"(0,\d+)\s*X\s*BELPEX[\w\-]*\s*([{SIGN_CHARS}])\s*(\d+(?:,\d+)?)",
        section,
    )

    def _first_num(m: re.Match[str] | None) -> float | None:
        if m is None:
            return None
        first = re.search(_NUM, m.group(1))
        return to_float(first.group(0)) if first else None

    current_cents = _first_num(maand)
    if current_cents is None:
        current_cents = _first_num(yearly)

    factor: float | None = None
    base: float | None = None
    if formula:
        factor_pdf = to_float(formula.group(1))
        base_pdf_cents = parse_sign(formula.group(2)) * to_float(formula.group(3))
        # PDF formula yields c/kWh (no VAT) from BELPEX in EUR/MWh; spot is
        # EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * 1000 / 100 = factor_pdf * 10
        #   base_eur_kwh   = base_cents / 100
        factor = factor_pdf * 10.0
        base = base_pdf_cents / 100.0

    if current_cents is None and factor is None:
        return None
    return InjectionRates(
        current=current_cents / 100.0 if current_cents is not None else None,
        factor=factor,
        base=base,
        formula=formula.group(0) if formula else None,
    )


_ENECO_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="eneco",
    label="Eneco",
    contracts=(
        Contract(
            id="power_fix",
            label="Eneco Zon & Wind Vast",
            kind="fixed",
            regions=_ENECO_REGIONS,
        ),
        Contract(
            id="power_flex",
            label="Eneco Zon & Wind Flex",
            kind="variable",
            regions=_ENECO_REGIONS,
        ),
        Contract(
            id="power_dynamic",
            label="Eneco Zon & Wind Dynamisch",
            kind="dynamic",
            regions=_ENECO_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
