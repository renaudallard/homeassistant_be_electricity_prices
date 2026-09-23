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

"""OCTA+ Belgium tariff card extractor.

OCTA+ publishes residential electricity tariff cards at stable URLs:

    https://files.octaplus.be/tariffs/E_OCTA_<PRODUCT>_RE_<WL|VL>_FR.pdf

OCTA+ only sells residential electricity in Wallonia and Flanders today
(the Brussels offers visible on their site are professional-only).

The PDFs visually present a clean energy + DSO + tax table, but pdfplumber's
default text extractor returns the DSO block in column-major order. The
extractor uses :func:`fetch_pdf_text_aligned` which reassembles each
visual row from word coordinates, giving e.g. ``AIEG 10,87 12,05 ...``
on a single line.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, replace
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp

from ..const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    FR_MONTHS,
    _is_pdf_payload,
    extract_pdf_text_aligned,
    fetch_pdf_text_aligned,
    fetch_text,
    head_freshness_key,
    is_transient_fetch_error,
    render_pdf,
    vat_multiplier,
)
from ._parse import SIGN_CHARS, fold_accents, parse_sign, require_contract, to_float
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    ImpactRates,
    InjectionRates,
    TariffKind,
    VariableRates,
    fixed_or_variable_rates,
)
from ._octaplus_overlays import (
    _extract_flanders_dsos,
    _extract_flanders_renewables,
    _extract_supplier_prosumer,
    _extract_taxes,
    _extract_wallonia_dsos,
    _extract_wallonia_renewables,
)

_BASE_URL = "https://files.octaplus.be/tariffs"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WL",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # OCTA+'s product slug in the URL
    # None means "every region OCTA+ serves"; the Impact comptage variant
    # is Wallonia-only (CWaPE Impact bands), so it overrides this.
    regions: frozenset[str] | None = None


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("octaplus_fixed", "OCTA+ Fixed", "fixed", "FIXED"),
    # Impact comptage (SMR3) reuses the Fixed card but prices the three
    # CWaPE bands; Wallonia-only, the Flanders Fixed card omits the block.
    _ContractDef(
        "octaplus_fixed_impact",
        "OCTA+ Fixed Impact",
        "tou_impact",
        "FIXED",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef("octaplus_ecofixed", "OCTA+ Eco Fixed", "fixed", "ECOFIXED"),
    _ContractDef(
        "octaplus_smartvariable",
        "OCTA+ Smart Variable",
        "variable",
        "SMARTVARIABLE",
    ),
    _ContractDef("octaplus_flux", "OCTA+ Flux", "variable", "FLUX"),
    _ContractDef("octaplus_ecoflux", "OCTA+ Eco Flux", "variable", "ECOFLUX"),
    _ContractDef("octaplus_dynamic", "OCTA+ Dynamic", "dynamic", "DYNAMIC"),
    _ContractDef("octaplus_ecodynamic", "OCTA+ Eco Dynamic", "dynamic", "ECODYNAMIC"),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


_LISTING_URL = "https://www.octaplus.be/fr/electricite-gaz-naturel/tarifs"

# The month archive behind the site's "archive fiches tarifaires" page, which
# is a Next.js page calling these two endpoints: the first names the cards a
# month had for a region and customer segment, the second serves one of them
# as a base64 data URL inside JSON rather than as a file. Residential only
# here (TypeContrat=RE), electricity only (Nrj=E).
_ARCHIVE_LISTING_URL = "https://srv.octaplus.be/websiterest/getTarifArchive"
_ARCHIVE_SHEET_URL = "https://srv.octaplus.be/websiterest/getTariffSheet"


def _document_url(contract: _ContractDef, region: str) -> str:
    return f"{_BASE_URL}/E_OCTA_{contract.slug}_RE_{_REGION_TO_CODE[region]}_FR.pdf"


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: HEAD the per-(contract, region) PDF.

    OCTA+ overwrites its tariff cards in place under stable filenames,
    so the file's Last-Modified header is the right freshness signal.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in _REGION_TO_CODE:
        return None
    return await head_freshness_key(session, _document_url(contract, region))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every residential electricity slug from OCTA+'s tarifs page.

    The listing links each card directly with the URL pattern
    ``E_OCTA_<SLUG>_RE_(VL|WL)_FR.pdf``. live_check diffs against
    ``{c.slug for c in _CONTRACTS}``.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return set(re.findall(r"E_OCTA_([A-Z]+)_RE_(?:VL|WL)_FR\.pdf", html))


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "OCTA+")
    if region not in _REGION_TO_CODE:
        raise ExtractorError(f"OCTA+ {contract_id}: not available in region {region!r}")
    url = _document_url(contract, region)
    # 1.0pt threshold collapses OCTA+'s heavy character spacing in the
    # tax block ("5 ,0 3 2 9 0 ,2 0 4 2" -> "5,0329 0,2042") while
    # still keeping real word spacing intact.
    text = await fetch_pdf_text_aligned(session, url, x_join_threshold=1.0)
    return parse_snapshot(contract_id, text, region, url)


def _archive_name_key(name: str) -> str:
    """Fold an archive file name for comparison: the listing prints
    ``2026-06 E OCTA+DYNAMIC RE VL FR.pdf`` where the live URL spells the same
    card ``E_OCTA_DYNAMIC_RE_VL_FR.pdf``, so spacing, underscores, the plus
    sign and case are noise."""
    return re.sub(r"[\s_+]", "", name).upper()


def _archive_json(body: str, what: str) -> Any:
    """The ``Response`` member of an archive endpoint's JSON, or raise.

    Every shape the endpoints can answer with is funnelled into
    ExtractorError, for the reason the sibling resolvers give: a payload that
    is JSON but not the expected shape must read as a failed fetch, never
    escape as a TypeError.
    """
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise ExtractorError(f"OCTA+ archive {what}: parse error: {err}") from err
    if not isinstance(payload, dict) or "Response" not in payload:
        raise ExtractorError(f"OCTA+ archive {what}: no 'Response' in the reply")
    return payload["Response"]


async def _resolve_archive_name(
    session: aiohttp.ClientSession,
    contract: _ContractDef,
    region: str,
    month_first: date,
) -> str | None:
    """The archive's file name for ``contract`` in ``region`` for one month,
    or ``None`` when that month lists no such card."""
    query = urlencode(
        {
            "Lang": "FR",
            "Region": _REGION_TO_CODE[region],
            "AnneeMois": f"{month_first.year:04d}{month_first.month:02d}",
            "Nrj": "E",
            "Canal": "website",
            "TypeContrat": "RE",
        }
    )
    rows = _archive_json(
        await fetch_text(session, f"{_ARCHIVE_LISTING_URL}?{query}", timeout=15),
        "listing",
    )
    if not isinstance(rows, list):
        raise ExtractorError("OCTA+ archive listing: 'Response' is not a list")
    wanted = _archive_name_key(
        f"{month_first.year:04d}-{month_first.month:02d} E OCTA+{contract.slug}"
        f" RE {_REGION_TO_CODE[region]} FR.pdf"
    )
    names: list[str] = [
        row["NomPdf"]
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("NomPdf"), str)
    ]
    for name in names:
        if _archive_name_key(name) == wanted:
            return name
    # The March 2026 listing names its fixed cards FIXEDD and ECOFIXEDD. A name
    # that differs only by a doubled letter is taken when it is the only one:
    # no two products in the range fold onto each other that way, and a month
    # that lists two such names is left unread rather than guessed.
    loose = _archive_name_loose(wanted)
    near = [
        name for name in names if _archive_name_loose(_archive_name_key(name)) == loose
    ]
    return near[0] if len(near) == 1 else None


def _archive_name_loose(key: str) -> str:
    """A folded archive name with runs of one character collapsed, which is
    the only typo the listing has been seen to make."""
    return re.sub(r"(.)\1+", r"\1", key)


async def _fetch_archive_pdf(session: aiohttp.ClientSession, name: str) -> bytes:
    """The archived card ``name`` as PDF bytes, unwrapped from the JSON data
    URL the sheet endpoint serves it in."""
    query = urlencode({"Canal": "website", "RequestedPDF": name})
    reply = _archive_json(
        await fetch_text(session, f"{_ARCHIVE_SHEET_URL}?{query}", timeout=60),
        "sheet",
    )
    sheet = reply.get("TariffSheet") if isinstance(reply, dict) else None
    if not isinstance(sheet, str) or "base64," not in sheet:
        raise ExtractorError(f"OCTA+ archive sheet: no PDF data for {name!r}")
    try:
        payload = base64.b64decode(sheet.split("base64,", 1)[1], validate=False)
    except (ValueError, TypeError) as err:
        raise ExtractorError(f"OCTA+ archive sheet: bad base64 for {name!r}") from err
    if not _is_pdf_payload(payload):
        raise ExtractorError(f"OCTA+ archive sheet: {name!r} is not a PDF")
    return payload


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card OCTA+ published for one past month, or ``None``.

    The live cards overwrite in place, but the site's archive page is backed
    by two endpoints: one lists the month's cards for a region, the other
    hands one of them back as a base64 data URL in JSON. The text is
    extracted with the same word-coordinate alignment ``fetch`` uses, in a
    worker thread like the shared helper does, since a tariff card is a lot
    of pure-Python parsing to run on the event loop. The dynamic cards print
    a validity date, the authoritative cross-check; the fixed ones print the
    month as ``MM/YYYY`` in their title, which the fallback tier reads.

    Until this existed a contract start date did nothing on an OCTA+ entry:
    the signing-cohort splice had no card to read, and every past month of
    the year-to-date billed on the current card as a proxy. Every failure
    comes back as ``None``, the one-month answer the month cache expects.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region not in _REGION_TO_CODE:
        return None
    if contract.regions is not None and region not in contract.regions:
        return None
    first = date(year_month.year, year_month.month, 1)
    try:
        name = await _resolve_archive_name(session, contract, region, first)
        if name is None:
            return None
        payload = await _fetch_archive_pdf(session, name)
        # Through the readers' render seam, not a bare thread: the card
        # archiver keeps what passes there, and this card arrived base64
        # inside JSON rather than through a reader.
        text = await render_pdf(
            "aligned",
            f"{_ARCHIVE_SHEET_URL}?RequestedPDF={name}",
            payload,
            lambda bytes_: extract_pdf_text_aligned(bytes_, 3, 1.0),
        )
        snap = parse_snapshot(
            contract_id, text, region, f"{_ARCHIVE_SHEET_URL}?RequestedPDF={name}"
        )
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    return archive_validity_check(snap, text, first, month_names=FR_MONTHS)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "OCTA+")

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, region_connection_fee = _extract_taxes(
        text, region
    )
    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text)
    else:
        wallonia_renewables = _extract_wallonia_renewables(text)
        dsos = _extract_wallonia_dsos(text)

    return SupplierSnapshot(
        supplier="octaplus",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            region_connection_fee=region_connection_fee,
            energy_fund_eur_per_month=0.0,
            vat_rate=0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
        supplier_prosumer_eur_per_kva_year=_extract_supplier_prosumer(
            text, contract.kind
        ),
    )


# ---- energy block -------------------------------------------------------------


def _extract_yearly_fee(text: str) -> float:
    """Capture the 'Redevance fixe' / yearly subscription line.

    Every OCTA+ residential card the integration covers prints this
    line (~65 EUR/year). A regex miss is a layout drift that would
    silently drop the fee from the user's annual estimate; raise
    rather than default to 0 so the coordinator surfaces the failure
    and serves the cached snapshot until the layout is fixed.
    """
    match = re.search(r"Redevance fixe \(€/an\)\s+([\d.,]+)", text)
    if match is None:
        raise ExtractorError("OCTA+: yearly fee (Redevance fixe) not found")
    return to_float(match.group(1))


def _vat_multiplier(text: str) -> float:
    """Read the VAT % from the card header ('Tarifs 6% TVAC')."""
    return vat_multiplier(text, r"Tarifs\s+(\d+(?:[.,]\d+)?)\s*%\s*TVAC")


# The January and February 2026 cards name both indices Belpex: "Belpex 15' * 1
# - 13,89" on the dynamic card, "Belpex SPP x 0,852 - 13,39" on the monthly
# ones. From March every card says Epex.
_EPEX_FORMULA = (
    rf"(?:Bel|E)pex\s*15\s*'?\s*\*\s*(\d+(?:[.,]\d+)?)\s*"
    rf"([{SIGN_CHARS}])\s*(\d+(?:[.,]\d+)?)"
)
# The 2026 template reworded the injection lead-in from "Le prix de votre
# injection est indexé ..." to "les prix de l'électricité injectée sont
# indexés ..."; accept either (with the curly apostrophe the card uses).
# The monthly index the non-dynamic cards settle their feed-in credit on. The
# August 2026 redesign renamed the parameter and changed the operator, and the
# first two cards of the year named the index Belpex, so all three spellings
# have to be accepted:
#   January "monohoraire : Belpex SPP x 0,852 - 13,39"  (one row per meter)
#   April  "monohoraire : Epex SPP x 0,852 - 13,39"     (one row per meter)
#   August "en EUR/MWh HTVA : Epex SPP M * 0,8560 - 16,20"  (one row)
# Stated in EUR/MWh either way. The value part is anchored rather than left as
# a character class, or the sentence-final period is swallowed into the number.
# The optional M must be followed by the operator, so the prose mention of the
# parameter name on its own ("le parametre << Epex SPP M >>") cannot match.
_SPP_FORMULA_RE = re.compile(
    rf"(?:Bel|E)pex\s*SPP\s*M?\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}])\s*"
    rf"(\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)
# The monthly index the variable cards bill CONSUMPTION on, one row per meter
# configuration. Both card generations, same as the SPP sibling above:
#   April  "compteur monohoraire : Epex RLP * 1,15 + 10"
#   August "mono-horaire (simple) : Epex RLP M * 1,150 + 10,000"
# Stated in EUR/MWh HTVA. The meter label sits before the formula and the two
# generations word it differently, so each meter gets its own lead-in.
_RLP_FORMULA = (
    rf"Epex\s*RLP\s*M?\s*[x*]\s*(\d+(?:[.,]\d+)?)\s*([{SIGN_CHARS}+])\s*"
    rf"(\d+(?:[.,]\d+)?)"
)
_RLP_METER_RES: dict[str, re.Pattern[str]] = {
    "single": re.compile(rf"mono[\s-]*horaire[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "peak": re.compile(rf"heures\s+pleines[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "offpeak": re.compile(rf"heures\s+creuses[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I),
    "exclusive_night": re.compile(
        rf"exclusi[fv]\s+nuit[^:;.]{{0,20}}:\s*{_RLP_FORMULA}", re.I
    ),
}

_INJECTION_LEAD = (
    r"(?:Le\s+prix\s+de\s+votre\s+injection"
    r"|prix\s+de\s+l['’]électricité\s+injectée\s+sont\s+indexés)"
)


def _injection_formula(text: str) -> re.Match[str] | None:
    """The feed-in formula a dynamic card prints after its lead-in, or None.

    It follows the lead-in within a sentence: 174 to 252 characters on every
    card archived since January 2026. The gap is bounded because the same card
    goes on to quote the formulas it would bill an AMR meter on, the
    CONSUMPTION one first, about 2.600 characters later. An open search walked
    into that clause whenever the real formula went unread and billed the
    consumption formula as the credit; bounded, such a card reads no formula,
    which the live check reports.
    """
    return re.search(rf"{_INJECTION_LEAD}.{{0,500}}?{_EPEX_FORMULA}", text, re.S)


# The sentence that introduces the consumption formula, once on every dynamic
# card: "La formule tarifaire HTVA (en €/MWh) est la suivante:" until the
# August 2026 redesign, "La formule de prix est la suivante, en EUR/MWh HTVA :"
# after it.
_CONSUMPTION_LEAD = (
    r"La\s+formule\s+(?:tarifaire\s+HTVA(?:\s*\(en\s*€\s*/\s*MWh\))?|de\s+prix)"
    r"\s+est\s+la\s+suivante\s*(?:,\s*en\s+(?:EUR|€)\s*/\s*MWh\s+HTVA)?\s*:"
)


def _dynamic_consumption_formula(text: str) -> re.Match[str] | None:
    """The consumption formula a dynamic card prints after its lead-in, or None.

    The consumption and injection formulas share the same shape, and the AMR
    clause further down quotes two more. The formula directly follows its
    lead-in on every archived card, so the search is bounded the way the
    feed-in one is: an open search took the first formula that was not the
    feed-in one, which on a card printing its own in an unknown spelling was
    the AMR clause's.

    Since the June 2026 card the feed-in paragraph opens with the same
    sentence as the consumption one, told apart only by writing the unit
    EUR/MWh against €/MWh. The lead accepts either spelling, and the match
    that is the feed-in formula is skipped, so a card that prints the feed-in
    paragraph first, or spells both units alike, still binds the consumption
    formula rather than raising or billing the feed-in one as energy.
    """
    feed_in = _injection_formula(text)
    for match in re.finditer(
        rf"{_CONSUMPTION_LEAD}.{{0,100}}?{_EPEX_FORMULA}", text, re.S
    ):
        if feed_in is None or match.start(1) != feed_in.start(1):
            return match
    return None


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        # Dynamic cards bury the consumption formula in prose, e.g.:
        #   "La formule tarifaire HTVA (en €/MWh) est la suivante:
        #    Epex 15' * 1,083 + 4,17"
        # The injection formula has the same shape but lives in the
        # "Le prix de votre injection" section; pick the first formula
        # that is not that one.
        formula = _dynamic_consumption_formula(text)
        if not formula:
            raise ExtractorError("could not parse OCTA+ dynamic formula")
        factor_pdf = to_float(formula.group(1))
        base_pdf_eur_mwh = parse_sign(formula.group(2)) * to_float(formula.group(3))
        vat = _vat_multiplier(text)
        # Formula is HTVA; the rest of the snapshot is TVAC, so apply
        # the parsed VAT multiplier. spot in our model is EUR/kWh so:
        #   factor_eur_kwh = factor_pdf * vat
        #   base_eur_kwh   = base_eur_mwh / 1000 * vat
        return DynamicRates(
            factor=factor_pdf * vat,
            base=base_pdf_eur_mwh / 1000.0 * vat,
            yearly_fixed_fee=yearly_fee,
            # OCTA+ indexes on the 15-minute Epex spot ("Epex 15'"), so
            # the contract bills per quarter-hour like Engie / Cociter /
            # EBEM / Ecofix. Without this the live price table aggregates
            # to hourly and the current / next-slot sensors and the
            # cheapest-window service lose the native 15-minute grid.
            quarter_hourly=True,
        )

    if kind == "tou_impact":
        # OCTA+ Impact comptage (SMR3) prints three CWaPE-band supplier rates
        # on the Fixed card (Impact Eco / Medium / Pic, c€/kWh); they follow
        # the same PIC/MEDIUM/ECO windows as the DSO's Impact distribution.
        pic = _meter_value(text, r"Impact Pic")
        medium = _meter_value(text, r"Impact Medium")
        eco = _meter_value(text, r"Impact Eco")
        if pic is None or medium is None or eco is None:
            raise ExtractorError("could not parse OCTA+ Impact energy block")
        return ImpactRates(pic=pic, medium=medium, eco=eco, yearly_fixed_fee=yearly_fee)

    # Static / variable: the energy table prints values column-major
    # so the aligned helper gives clean rows like:
    #   "Compteur monohoraire 15,86 4,72"
    #   "Compteur Heures pleines 18,67 4,72"  (or "Heures pleines 18,67 4,72")
    #   "Heures creuses 13,77 4,72"
    #   "Compteur exclusif nuit 14,85 -"
    mono = _meter_value(text, r"Compteur monohoraire")
    peak = _meter_value(text, r"Heures pleines")
    offpeak = _meter_value(text, r"Heures creuses")
    excl = _meter_value(text, r"Compteur exclusif nuit")
    if mono is None:
        raise ExtractorError(f"could not parse OCTA+ {kind} energy block")
    # OCTA+ cards always print the bi-hourly table, so a missing Heures
    # pleines / Heures creuses row is a layout drift, not a mono-only
    # card. Without this a drift would silently bill a bi-hourly user the
    # single rate. (Compteur exclusif nuit is a separate optional circuit,
    # so it stays nullable.)
    if peak is None or offpeak is None:
        raise ExtractorError(f"could not parse OCTA+ {kind} bi-hourly rates")
    rates = fixed_or_variable_rates(
        kind,
        single=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl,
        yearly_fixed_fee=yearly_fee,
    )
    if not isinstance(rates, VariableRates):
        return rates
    # A variable card indexes consumption on the MONTHLY Epex RLP and says the
    # figures above are not the rate: "Les prix de l'electricite consommee
    # mentionnes en page 1 sont purement indicatifs et sont bases sur la valeur
    # actuelle du parametre 'V-test' ... les prix moyens attendus pour les 12
    # mois a venir". A forward estimate of a year is not a lagged index: over
    # Jan-Aug 2026 the printed figure ran +9,4% to +19,8% against the contract
    # with single months as far out as +50%, while resolving the formula
    # against the plain arithmetic mean sits 3-4% low and never swings.
    #
    # That 3-4% is the residual: Epex RLP M weights by the residual load
    # profile, which is dearer than a flat average because consumption leans
    # into expensive hours, and Synergrid publishes that profile only as .xlsb.
    # Documented in docs/providers/octaplus.md rather than hidden.
    coefs: dict[str, tuple[float, float]] = {}
    for slot, pattern in _RLP_METER_RES.items():
        m = pattern.search(text)
        if m is None:
            continue
        # EUR/MWh HTVA -> EUR/kWh TVAC, same axis as the dynamic branch: the
        # factor is a dimensionless multiplier on a EUR/kWh spot, the base
        # divides by 1000.
        vat = _vat_multiplier(text)
        coefs[slot] = (
            to_float(m.group(1)) * vat,
            parse_sign(m.group(2)) * to_float(m.group(3)) / 1000.0 * vat,
        )
    if "single" not in coefs:
        # Every variable card states the formula. A miss is a layout drift,
        # and silently billing the V-test estimate is what this exists to
        # stop, so leave the printed rates and let the live check say so.
        return rates
    return replace(
        rates,
        month_indexed=True,
        formula_factor=coefs["single"][0],
        formula_base=coefs["single"][1],
        formula_factor_peak=coefs.get("peak", (None, None))[0],
        formula_base_peak=coefs.get("peak", (None, None))[1],
        formula_factor_offpeak=coefs.get("offpeak", (None, None))[0],
        formula_base_offpeak=coefs.get("offpeak", (None, None))[1],
        formula_factor_exclusive_night=coefs.get("exclusive_night", (None, None))[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", (None, None))[1],
    )


def _meter_value(text: str, label_pattern: str) -> float | None:
    match = re.search(rf"{label_pattern}\s+([\d.,]+)", text)
    if not match:
        return None
    return to_float(match.group(1)) / 100.0


# Banner month -> zero-padded number. Keys are accent-folded (matching
# the fold_accents() applied to the parsed banner) so "février" / "août"
# / "décembre" resolve.
_FRENCH_MONTHS: dict[str, str] = {
    fold_accents(name): f"{i:02d}" for i, name in enumerate(FR_MONTHS, 1)
}


def _extract_publication_month(text: str) -> str:
    """Pull MM/YYYY off the OCTA+ card.

    Cards through mid-2026 printed ``Clients résidentiels en <region> -
    MM/YYYY - Tarifs N% TVAC`` near the top; anchor on that prose so a
    footer reference matching ``-MM/YYYY-`` can't shadow the title date.
    The 2026 redesign dropped that line and moved the date to a
    ``FICHE TARIFAIRE <MOIS> <YYYY>`` banner with the French month spelled
    out (accented), so fall back to that.
    """
    match = re.search(
        r"Clients\s+r[ée]sidentiels[^\n]{0,80}?-\s*(\d{1,2})/(\d{4})\s*-",
        text,
    )
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    banner = re.search(r"FICHE\s+TARIFAIRE\s+([^\s\d]+)\s+(\d{4})", text)
    if banner:
        month = _FRENCH_MONTHS.get(fold_accents(banner.group(1)))
        if month:
            return f"{month}/{banner.group(2)}"
    return ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Injection rate sits next to the consumption rate on the
    # 'Compteur monohoraire' line.
    match = re.search(r"Compteur monohoraire\s+[\d.,]+\s+([\d.,]+)", text)
    current = to_float(match.group(1)) / 100.0 if match else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    spp_indexed = False
    if kind == "dynamic":
        # Injection formula appears after the prose
        # "Le prix de votre injection est indexé ..."
        # so we anchor on that lead-in to skip the consumption formula.
        inj = _injection_formula(text)
        if inj is not None:
            f_pdf = to_float(inj.group(1))
            b_eur_mwh = parse_sign(inj.group(2)) * to_float(inj.group(3))
            factor = f_pdf  # injection is VAT-exempt
            base = b_eur_mwh / 1000.0
            formula = inj.group(0)
    else:
        # Every other card settles the credit on a MONTHLY index: "Le prix de
        # votre injection est indexé mensuellement sur base du paramètre
        # d'indexation de la Epex SPP ... La valeur de la Epex du mois en cours
        # ne sera connue qu'en fin de mois". The c/kWh figure read off the
        # meter line above sits in the card's "Prix estimés" column and is an
        # estimate, not the rate, so it is kept only as the fallback.
        #
        # The card states one formula per meter configuration. They are
        # identical today and InjectionRates carries a single pair, so the
        # coefficients are surfaced only while all of them agree: a card that
        # splits them keeps the estimate rather than billing two meter types
        # on a third one's formula.
        rows = _SPP_FORMULA_RE.findall(text)
        distinct = {
            (to_float(f), parse_sign(sign) * to_float(b) / 1000.0)
            for f, sign, b in rows
        }
        if len(distinct) == 1:
            factor, base = distinct.pop()
            match = _SPP_FORMULA_RE.search(text)
            formula = match.group(0) if match else None
            # Epex SPP is the solar-weighted mean. The flag routes the
            # coefficients to the delivery month's own weighted mean and keeps
            # them off the hourly spot, which they are not coefficients for.
            spp_indexed = True

    if current is None and factor is None:
        return None
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        spp_indexed=spp_indexed,
    )


# ---- taxes --------------------------------------------------------------------


# ---- DSO row parsers ----------------------------------------------------------


_OCTAPLUS_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

# Contracts whose feed-in credit indexes on a MONTHLY mean. The credit
# resolves against ENTSO-E spots the energy leg never fetches, so the config
# flow has to offer the optional key or the formula can never resolve and every
# path falls back to the card's printed figure. See
# ``Contract.spot_indexed_injection``.
# Every non-dynamic OCTA+ product indexes injection on the monthly Epex SPP;
# the dynamic pair indexes per quarter-hour through its energy formula.

# The variable cards print one "Epex RLP M" formula per meter and settle on the
# delivery month; Fixed, Eco Fixed and Fixed Impact print the rate they bill.
_MONTHLY_ENERGY_CONTRACTS: frozenset[str] = frozenset(
    {"octaplus_smartvariable", "octaplus_flux", "octaplus_ecoflux"}
)

EXTRACTOR = SupplierExtractor(
    sweep_cost_s=2.2,
    id="octaplus",
    label="OCTA+",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions or _OCTAPLUS_REGIONS,
            spot_indexed_injection=c.kind != "dynamic",
            month_indexed_energy=c.contract_id in _MONTHLY_ENERGY_CONTRACTS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
