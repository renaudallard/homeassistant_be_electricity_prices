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

"""DATS 24 (Colruyt subsidiary) tariff extractor.

DATS 24 sold one residential electricity product, "Elektriciteit Groen
Variabel", in Flanders and Wallonia. It's a variable contract indexed
monthly against the BE_spotRLP (Belgian quarter-hourly spot prices,
RLP-weighted) parameter, with injection settled on the SPP-weighted
BE_spotSPP:

    afname        = (BE_spotRLP * <factor> + 0.511) * 1.06   c€/kWh
    teruglevering = (BE_spotSPP * <factor> - 1.11)           c€/kWh (VAT-exempt)

The per-meter-type coefficients are re-published with every card, so the
parser reads the printed monthly indicative rather than re-solving the
formula (see :func:`_extract_energy`).

WITHDRAWAL: DATS 24 left residential energy supply. Its last card is of July
2026, none was published for August, and its contracts moved automatically
to EnergyVision on 1 September 2026, which keeps their contract and tariff
and publishes no card for them (issues #100 and #169). So the product is
withdrawn from 1 August 2026 (``Contract.withdrawn``): a household signed on
one of its cards prices on that card's formulas, and with no signing month
on the July 2026 card, each re-indexed on the delivery month's BE_spotRLP and
BE_spotSPP. No current card is ever asked for.

The cards are published monthly on the Colruyt Group static CDN, one PDF per
month at a URL that carries the month in its filename (see
:func:`_card_url`). It replaces ``profile.dats24.be/api/v1/ratecard``,
which answered every request with HTTP 500 from 2026-07-29 on; the CDN file
is the very same document (its April 2026 PDF is byte-identical to this
repo's April test fixture).

Each PDF carries the current-month rates plus the year-estimate
(jaarschatting) values, full Fluvius / Walloon DSO tables, the Flemish
GSC + WKC certificate cost, the Walloon CV cost, and federal taxes. All
printed amounts are TVAC except where the card explicitly notes otherwise,
so ``vat_rate=0.0`` matches the project's standard convention.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

import aiohttp

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    FLUVIUS_AREA_LABELS_UPPER,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._parse import SIGN_CHARS, excise_tier_bands, numeric_row, parse_sign, to_float
from ._pdf import (
    fetch_pdf_text_layout,
    is_missing_card_error,
)
from ._pdf_render import (
    extract_pdf_text_layout,
)
from ._rates import (
    Contract,
    EnergyRates,
    InjectionRates,
    RlpBlend,
    VariableRates,
    last_card_month,
)
from ._validity import parse_valid_until
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    walloon_dso_overlay,
)

_CDN_BASE = "https://api.colruytgroup.com/api/static/dats24/parameters/site"

_CONTRACT_ID = "dats24_groen_variabel"
_CONTRACT_LABEL = "DATS 24 Elektriciteit Groen Variabel (transferred to EnergyVision)"

# The first day the product was no longer sold, the month after its last
# card: the CDN has none for August 2026 or later.
_WITHDRAWN = date(2026, 8, 1)
_LAST_CARD = last_card_month(_WITHDRAWN)
# The first card whose formulas read, the energy's checked against the rate it
# prints and in Flanders the feed-in's against its figure: every card from
# April 2023 to July 2026 does in both regions, and those of January to March
# 2023 fail on their tax block, with none on the CDN before. A signing card
# gives the cohort its formulas alone, the network coming from each month's
# own card. A household that signed outside these is refused: no card would
# be read for it, and each month would bill on its own card's formulas
# without a word (``Contract.signing_cards_from``).
_SIGNING_CARDS_FROM = date(2023, 4, 1)


_FLANDERS_DSOS = FLUVIUS_AREA_LABELS_UPPER

# Wallonia DSO labels as they appear on DATS 24's card. Multiple ORES
# sub-areas (Brabant Wallon, Est, Hainaut, Luxembourg, Mouscron, Namur,
# Verviers) all carry identical numbers, so we collapse them onto our
# single "ores" key by picking the Brabant Wallon row.
_WALLONIA_DSOS: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    ("ORES (Brabant Wallon)", DSO_ORES),
    ("RÉGIE DE WAVRE", DSO_REW),
    ("RESA", DSO_RESA),
)


# ---- card resolution ---------------------------------------------------------


def _card_url(year: int, month: int) -> str:
    """The CDN URL of one month's Dutch-language card.

    The filename spells the month out as "Versie MM YYYY"; the spaces are
    written pre-encoded so the string can be pasted into curl as-is. Cards
    are retained back to 2023, until July 2026.
    """
    return (
        f"{_CDN_BASE}/{year}/ELEK/NL/"
        f"Elektriciteit%20Groen%20Variabel%20-%20Versie%20{month:02d}%20{year}.pdf"
    )


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """The last card published, July 2026's, whatever the month.

    DATS 24 publishes no card after it, so the running month's address is
    never asked for. Once the CDN drops this one the 404 propagates, and the
    coordinator keeps the card it holds or reads the card archive's copy of
    the month before ``Contract.withdrawn``.
    """
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown DATS 24 contract {contract_id!r}")
    if region not in (REGION_FLANDERS, REGION_WALLONIA):
        raise ExtractorError(
            "DATS 24 only sold residential electricity in Flanders / Wallonia"
        )
    url = _card_url(_LAST_CARD.year, _LAST_CARD.month)
    return parse_snapshot(await fetch_pdf_text_layout(session, url), url, region)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """A key that never changes, as the card read never does: the last card
    is fetched once and held, rather than asked for every hour as a card
    past its validity would be."""
    return f"withdrawn {_WITHDRAWN.isoformat()}"


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The published snapshot for a specific (year, month), or ``None``.

    The CDN keeps every month under the same month-keyed URL ``_card_url``
    already builds, so the archive costs one HTTP GET per past month and no
    new resolution logic at all.

    This was a documented omission whose stated reason was that the payoff
    would be one month. Measured, it is not: a year-to-date walk spans every
    month since January, and proxying the current card into all of them ran
    Flanders 10,5% high over Jan-Jul at 3500 kWh (EUR 543,12 against 491,54)
    and Wallonia 5,7%, worst in May at 18,0%. The backfill shares the same
    cache, so those prices were being written into the recorder as well.

    ``None`` for a month the CDN does not hold, and without asking for one
    after the last card, which has none.
    """
    if contract_id != _CONTRACT_ID:
        return None
    if region not in (REGION_FLANDERS, REGION_WALLONIA):
        return None
    if year_month.replace(day=1) > _LAST_CARD:
        return None
    url = _card_url(year_month.year, year_month.month)
    try:
        text = await fetch_pdf_text_layout(session, url)
    except ExtractorError as err:
        if is_missing_card_error(str(err)):
            return None
        raise
    return parse_snapshot(text, url, region)


def parse_snapshot(text: str, source_url: str, region: str) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    return SupplierSnapshot(
        supplier="dats24",
        contract=_CONTRACT_ID,
        energy=_extract_energy(text),
        dsos=_extract_dsos(text, region),
        taxes=_extract_taxes(text, region),
        source_url=source_url,
        publication_label=_extract_publication(text),
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text, region),
    )


# ---- energy ------------------------------------------------------------------


# BE_spotRLP is published monthly and the card puts it into the formula as
# "de meest recente waarde van BE_spotRLP (maart 2026: 97,64 EUR/MWh)".
_RLP_INDEX_RE = re.compile(r"BE_spotRLP\s*\(\w+\s+\d{4}:\s*([\d.,]+)\s*€/MWh\)")
# One formula per register, c€/kWh before VAT: "(BE_spotRLP x 0,1124 + 0,511)
# + 6% btw". The day row is labelled "Tweevoudige meter Dag", the night one
# "Nacht" alone, on the line after the feed-in formula.
_FORMULA = rf"\(BE_spotRLP\s*x\s*([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)\)\s*\+\s*(\d+)%\s*btw"
_FORMULA_RES = {
    "mono": re.compile(rf"Enkelvoudige meter\s*{_FORMULA}"),
    "peak": re.compile(rf"\bDag\s*{_FORMULA}"),
    "offpeak": re.compile(rf"(?<!Uitsluitend )\bNacht\s*{_FORMULA}"),
    "excl_night": re.compile(rf"Uitsluitend nachtmeter\s*{_FORMULA}"),
}
# The index the formula is resolved on. The card defines BE_spotRLP as the
# RLP-weighted mean "van de profielen van de verschillende
# distributienetbeheerders", the words Eneco and energie.be also use for two
# different blends, so the blend is the one the card's own published values
# reproduce. Against the values the February to July 2026 cards state for
# January to June, the mean over every DSO column of the Synergrid sheet
# weighting quarter-hour prices gives them to 0,04 EUR/MWh on average (the
# first three months to the cent), and the engine's hourly weighting to
# 0,15; the equal mean of the three distinct curves is 1,1 off and the
# Flemish curve alone 0,4.
_RLP_BLEND: RlpBlend = "columns"


def _register_formulas(
    text: str, printed: dict[str, float]
) -> dict[str, tuple[float, float]] | None:
    """Each register's ``(factor, base)`` onto a EUR/kWh spot, VAT included,
    or ``None`` when the card's formulas do not reproduce its own prices.

    Bound by arithmetic, not by position: every formula has to give back the
    rate the card prints for it at the BE_spotRLP the card says it used, to
    the cent it is rounded to. The May 2026 card is why: it printed
    "(BE_spotRLP x 00,000 + 0,001)" in every row and an index of "085" beside
    correct prices, and reading those coefficients would bill a few
    hundredths of a cent. Such a card keeps its printed prices instead.
    """
    index_match = _RLP_INDEX_RE.search(text)
    if index_match is None:
        return None
    index = to_float(index_match.group(1))
    out: dict[str, tuple[float, float]] = {}
    for register, pattern in _FORMULA_RES.items():
        match = pattern.search(text)
        if match is None:
            return None
        factor_c = to_float(match.group(1))
        base_c = parse_sign(match.group(2)) * to_float(match.group(3))
        vat = 1.0 + int(match.group(4)) / 100.0
        if abs((index * factor_c + base_c) * vat - printed[register]) > 0.006:
            return None
        # c€/kWh per EUR/MWh of index onto a EUR/kWh spot: x10 the factor,
        # /100 the base, both grossed by the VAT the row adds.
        out[register] = (factor_c * 10.0 * vat, base_c / 100.0 * vat)
    return out


def _extract_energy(text: str) -> EnergyRates:
    """The four registers' rates, and the formulas they are indexed on.

    The card prints four values under "Afname1": single rate (mono),
    bi-hourly day, bi-hourly night and exclusive-night, VAT included:

        Afname1 (c€/kWh) 12,18 13,48 10,97 10,97

    They are the formulas at the PREVIOUS month's BE_spotRLP, the most recent
    one published. The contract is "maandelijks geïndexeerd" on it, so the
    month delivered settles on its own BE_spotRLP: the formulas are kept with
    ``month_indexed`` and resolved against the delivery month's RLP-weighted
    mean, and the printed rates stay the fallback for an entry without the
    profile or a key.
    """
    match = re.search(
        r"Afname1?\s*\(c€/kWh\)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)\s+([\d,.]+)",
        text,
    )
    if not match:
        raise ExtractorError("could not parse DATS 24 indicative afname row")
    printed = dict(
        zip(
            ("mono", "peak", "offpeak", "excl_night"),
            (to_float(value) for value in match.groups()),
            strict=True,
        )
    )
    fee_match = re.search(r"VASTE VERGOEDING\s*\(€/jaar\)\s+([\d,.]+)", text)
    if fee_match is None:
        # The yearly standing charge is mandatory on every DATS 24 card;
        # a miss is a layout drift, not a fee-free contract. Raise like
        # the afname row above rather than silently dropping the base fee.
        raise ExtractorError("could not parse DATS 24 yearly fixed fee")
    yearly_fee = to_float(fee_match.group(1))
    rates = VariableRates(
        current=printed["mono"] / 100.0,
        peak=printed["peak"] / 100.0,
        offpeak=printed["offpeak"] / 100.0,
        exclusive_night=printed["excl_night"] / 100.0,
        yearly_fixed_fee=yearly_fee,
    )
    formulas = _register_formulas(text, printed)
    if formulas is None:
        return rates
    return replace(
        rates,
        formula=" · ".join(
            f"{register} ({factor / 10.0:.5f} BE_spotRLP {base * 100.0:+.4f}) c€/kWh"
            for register, (factor, base) in formulas.items()
        ),
        formula_factor=formulas["mono"][0],
        formula_base=formulas["mono"][1],
        formula_factor_peak=formulas["peak"][0],
        formula_base_peak=formulas["peak"][1],
        formula_factor_offpeak=formulas["offpeak"][0],
        formula_base_offpeak=formulas["offpeak"][1],
        formula_factor_exclusive_night=formulas["excl_night"][0],
        formula_base_exclusive_night=formulas["excl_night"][1],
        month_indexed=True,
        rlp_indexed=True,
        rlp_blend=_RLP_BLEND,
    )


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str, region: str) -> dict[str, DsoOverlay]:
    if region == REGION_FLANDERS:
        return _extract_flanders_dsos(text)
    if region == REGION_WALLONIA:
        return _extract_wallonia_dsos(text)
    return {}


def _extract_flanders_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the Flanders Fluvius block (page 2 of the card).

    Each row has ten numeric columns:

        cap_digital | afname_dig | afname_dig_excl_nacht | max_tarief
        cap_classical | afname_class | afname_class_excl_nacht | prosumer
        meteropname_kwartier | meteropname_jaarlijks

    The integration only models digital-meter rates (post-2024 Fluvius
    rollout target); the second four numbers describe the analog-meter
    path which we ignore. Distribution rates are TVAC c€/kWh.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_DSOS.items():
        row = numeric_row(text, label, 10)
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row[1]) / 100.0,
            distribution_exclusive_night=to_float(row[2]) / 100.0,
            transport=0.0,  # rolled into Fluvius distribution on this card
            capacity_eur_per_kw_year=to_float(row[0]),
            # Column 8 is PROSUMENTEN-TARIEF (reverse-metering forfait);
            # parse it like the Wallonia block and the sibling Ecofix / EBEM
            # Flanders cards so the overlay is complete.
            prosumer_eur_per_kva_year=to_float(row[7]),
            data_management_per_year=to_float(row[9]),
            # Column 4 is the VREG MAXIMUM-TARIEF, captured all along and then
            # discarded. The card's header reads "AFNAME AFNAME MAXIMUM-" over
            # units "c€/kWh c€/kWh c€/kWh", and the footer says "Alle prijzen
            # zijn inclusief 6% btw", so it is stored TVAC as printed like the
            # rest of this card.
            network_ceiling_eur_per_kwh=to_float(row[3]) / 100.0,
        )
    return out


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the Wallonia DSO block (page 3 of the card).

    Each row has ten numeric columns:

        single | day | night | PIC | MEDIUM | ECO | excl_nacht
        transport | data-beheer (€/yr) | prosumer (€/kVA/yr)

    All distribution rates are TVAC c€/kWh; transport is c€/kWh.
    DATS 24 lists seven ORES sub-areas (Brabant Wallon, Est, Hainaut,
    Luxembourg, Mouscron, Namur, Verviers) with identical rates: we
    collapse them onto the integration's single "ores" key.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_DSOS:
        row = numeric_row(text, label, 10)
        if not row:
            continue
        out[key] = walloon_dso_overlay(
            mono=to_float(row[0]),
            peak=to_float(row[1]),
            offpeak=to_float(row[2]),
            pic=to_float(row[3]),
            medium=to_float(row[4]),
            eco=to_float(row[5]),
            excl_night=to_float(row[6]),
            transport=to_float(row[7]),
            terme_fixe=to_float(row[8]),
            prosumer=to_float(row[9]),
        )
    return out


# ---- taxes -------------------------------------------------------------------


def _extract_taxes(text: str, region: str) -> TaxOverlay:
    """Parse the federal + regional tax block.

    The card prints all tax values TVAC (footer: "Alle prijzen ...
    inclusief 6% btw, tenzij anders vermeld"), with two explicit
    exceptions tagged "Niet aan btw onderworpen": the Walloon
    connection fee and the Flemish Energiefonds. Both happen to use
    the same per-kWh / per-month conventions as the other extractors,
    so they slot directly into TaxOverlay without conversion.

    Flemish renewables = GSC + WKC (Vlaams Gewest); Walloon
    renewables = CV (Waals Gewest). Each is a per-kWh certificate
    quota cost the supplier must doorstort. Per-region overlays are
    gated by ``region`` so a Wallonia customer never sees the Flemish
    Energiefonds added to their YTD even if the value rises above 0
    (matching what Bolt and Engie do).
    """
    contrib_match = re.search(r"Energiebijdrage\s+([\d,.]+)\s*c€/kWh", text)
    excise_match = re.search(
        r"Verbruik tussen 0 kWh en 3\.000 kWh\s+([\d,.]+)\s*c€/kWh", text
    )
    if not contrib_match or not excise_match:
        raise ExtractorError("could not parse DATS 24 federal tax block")

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    energy_fund_per_month = 0.0
    connection_fee = 0.0

    if region == REGION_FLANDERS:
        gsc_match = re.search(r"Vlaams Gewest:\s*GSC\s*\(c€/kWh\)\s+([\d,.]+)", text)
        wkc_match = re.search(r"WKC\s*\(c€/kWh\)\s+([\d,.]+)", text)
        if gsc_match is None or wkc_match is None:
            # GSC and WKC are both mandatory Flemish certificate costs,
            # always printed together; a single miss is a layout drift, not
            # a one-component card. The GSC regex needs the fragile "Vlaams
            # Gewest:" prefix and is the dominant half (1,183 vs 0,378), so
            # raise on either miss, matching the sibling Frank provider,
            # rather than silently substituting 0 for the missing half.
            raise ExtractorError("DATS 24: Flanders GSC/WKC renewables not found")
        flanders_renewables = (
            to_float(gsc_match.group(1)) / 100.0 + to_float(wkc_match.group(1)) / 100.0
        )
        fund_match = re.search(
            r"Hoofdverblijf\s*\(domicilie\)\s+([\d,.]+)\s*€/maand", text
        )
        energy_fund_per_month = to_float(fund_match.group(1)) if fund_match else 0.0
    elif region == REGION_WALLONIA:
        cv_match = re.search(r"Waals Gewest:\s*CV\s*\(c€/kWh\)\s+([\d,.]+)", text)
        # The layout-aware PDF text emits the row as
        # "Aansluitingsvergoeding Wallonië<footnote-digit> 0,07500 c€/kWh"
        # on one line; tolerate the footnote digit.
        connection_match = re.search(
            r"Aansluitingsvergoeding\s+Walloni[eë]\d*\s+([\d,.]+)\s*c€/kWh",
            text,
        )
        if cv_match is None or connection_match is None:
            # CV (green certificates) and the Walloon connection fee are
            # mandatory; raise rather than silently zero a c€/kWh charge.
            raise ExtractorError("DATS 24: Wallonia CV / connection fee not found")
        wallonia_renewables = to_float(cv_match.group(1)) / 100.0
        connection_fee = to_float(connection_match.group(1)) / 100.0

    federal_excise = to_float(excise_match.group(1)) / 100.0
    return TaxOverlay(
        federal_excise=federal_excise,
        federal_excise_bands=excise_tier_bands(
            text,
            federal_excise,
            start=excise_match.start(),
            within=excise_match.end(),
        ),
        energy_contribution=to_float(contrib_match.group(1)) / 100.0,
        flanders_renewables=flanders_renewables,
        wallonia_renewables=wallonia_renewables,
        region_connection_fee=connection_fee,
        energy_fund_eur_per_month=energy_fund_per_month,
        vat_rate=0.0,  # card values are already TVAC
    )


# ---- injection ---------------------------------------------------------------


def _extract_injection(text: str, region: str) -> InjectionRates | None:
    """Parse the teruglevering monthly indicative (formula kept for diagnostics).

    DATS 24 "Groen Variabel" settles injection on BE_spotSPP, a MONTHLY
    synthetic-profile index, not the hourly day-ahead spot. The card prints a
    figure right after the formula and says which month it came from: "de
    terugleveringsvergoeding wordt verkregen door de MEEST RECENTE waarde van
    BE_spotSPP (maart 2026: 57,11 EUR/MWh) in te vullen in de tariefformule",
    on the April card.
      formula:    (BE_spotSPP x 0,0766 - 1,11)   c€/kWh, VAT-exempt
      indicative: Teruglevering2 (c€/kWh) 3,26   <- from MARCH's index

    So crediting the printed figure credits last month's index. The
    coefficients are surfaced with ``spp_indexed`` instead, which routes them
    to the DELIVERY month's own solar-weighted mean and keeps them away from
    the hourly spot; the card's own SPP is Synergrid's, the same profile the
    coordinator downloads. ``current`` remains the fallback for an entry
    without that profile.

    Returns ``None`` in Wallonia: the card footnote reserves the
    teruglevering tariff to Flemish customers with a digital meter, so a
    Walloon prosumer is not paid a feed-in credit and the same card's
    indicative must not be surfaced for them.

    Some users have a single-rate meter, others bi-hourly: the card
    publishes one shared teruglevering value across all three meter
    types, so a single InjectionRates entry covers everyone.
    """
    if region == REGION_WALLONIA:
        return None
    # The monthly indicative can go negative when BE_spotSPP is low, so
    # capture an optional leading sign - otherwise a negative indicative
    # fails to match and the credit is silently dropped. The blanks before
    # the sign are taken whole: shared with those after it when no sign is
    # printed, a run of blanks was split between the two in every way.
    indicative = re.search(
        rf"Teruglevering2?\s*\(c€/kWh\)\s++([{SIGN_CHARS}]?)\s*([\d,.]+)", text
    )
    if not indicative:
        # The card always prints the monthly indicative; a miss is a layout
        # drift, not a fee-free contract. Fail loud rather than emit a
        # spot-shaped credit the pipeline cannot price for this variable card.
        raise ExtractorError("DATS 24 injection: monthly indicative missing")
    formula = re.search(
        rf"\(BE_spotSPP\s*x\s*([\d,.]+)\s*([{SIGN_CHARS}])\s*([\d,.]+)\)",
        text,
    )
    factor: float | None = None
    base: float | None = None
    if formula:
        # c/kWh from BE_spotSPP in EUR/MWh: the factor scales by 10 to meet a
        # spot in EUR/kWh, the base by 100. Injection is VAT-exempt, so both
        # stay on the card's basis.
        factor = to_float(formula.group(1)) * 10.0
        base = parse_sign(formula.group(2)) * to_float(formula.group(3)) / 100.0
    return InjectionRates(
        current=parse_sign(indicative.group(1)) * to_float(indicative.group(2)) / 100.0,
        factor=factor,
        base=base,
        spp_indexed=factor is not None,
        formula=formula.group(0) if formula else "",
    )


# ---- publication label -------------------------------------------------------


def _extract_publication(text: str) -> str:
    match = re.search(r"TARIEFKAART\s+(\w+\s+20\d{2})", text, re.IGNORECASE)
    return match.group(1).strip().lower() if match else ""


__all__ = ["EXTRACTOR", "extract_pdf_text_layout", "fetch", "parse_snapshot", "probe"]


_DATS24_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    sweep_cost_s=9.3,
    id="dats24",
    label="DATS 24",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="variable",
            regions=_DATS24_REGIONS,
            # Injection indexes on the monthly BE_spotSPP, in Flanders only:
            # the card reserves its feed-in tariff to Flemish customers, so a
            # Walloon entry on the injection regime is credited nothing. The
            # flow still offers that regime there, and the key step with it,
            # which then has no credit to index.
            spot_indexed_injection=True,
            # Energy is indexed on the delivery month's BE_spotRLP and the
            # card prints last month's, so the same key re-prices it.
            month_indexed_energy=True,
            # Offered marked as withdrawn to the households EnergyVision took
            # over on 1 September 2026 on the same contract and tariff.
            withdrawn=_WITHDRAWN,
            signing_cards_from=_SIGNING_CARDS_FROM,
        ),
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
    probe=probe,
)
