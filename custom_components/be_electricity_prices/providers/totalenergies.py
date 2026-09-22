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

"""TotalEnergies Belgium tariff card extractor.

TotalEnergies publishes the current month's tariff card per (product,
region) at stable URLs:

    https://totalenergies.be/static/marketing-documents/b2c/tariff-card/
        latest/<PRODUCT>_ELECTRICITY_<REGION>_FR.pdf

The ``/latest/`` segment auto-rolls each month so no listing scrape is
needed. All nine residential electricity products are registered. Each
is available in V/W/B (TotalEnergies serves all three regions).

The PDFs include rotated DSO / tax columns that pypdf cannot extract
('Rotated text discovered. Output will be incomplete.'). The extractor
uses pdfplumber for the layout-aware extraction it needs to read those
cells; the other extractors keep using pypdf since their cards are
horizontal-text-only.

Dynamic formula format: ``0.1034 * BELPEXH + 1.75`` (HTVA, c€/kWh).
The parser scales factor and base by the parsed VAT multiplier - same
pattern as Engie/Luminus.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

import aiohttp

from ..const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    vat_multiplier,
)
from ._parse import SIGN_CHARS
from ._validity import parse_valid_until
from ._parse import (
    parse_sign,
    require_contract,
    to_float,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from ._rates import (
    ALL_REGIONS,
    Contract,
    DynamicRates,
    EnergyRates,
    InjectionRates,
    TariffKind,
    VariableRates,
    fixed_or_variable_rates,
)
from ._totalenergies_overlays import (
    _energy_contribution_from_table,
    _extract_brussels_dsos,
    _extract_energy_contribution,
    _extract_energy_fund,
    _extract_federal_excise,
    _extract_fee_and_renewables,
    _extract_flanders_dsos,
    _extract_renewables,
    _extract_wallonia_dsos,
)

_BASE_URL = "https://totalenergies.be/static/marketing-documents/b2c/tariff-card/latest"

_REGION_TO_CODE: dict[str, str] = {
    REGION_FLANDERS: "VL",
    REGION_WALLONIA: "WAL",
    REGION_BRUSSELS: "BXL",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # the file prefix in TotalEnergies's URL
    # Regions the product is actually published in. TotalEnergies's
    # listing page advertises every product in V/W/B but a few only
    # have a Wallonia PDF; the others return a 200 OK HTML 404 page.
    regions: frozenset[str] = ALL_REGIONS


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "totalenergies_electricite_fixe",
        "TotalEnergies Electricité Fixe",
        "fixed",
        "ELECTRICITE-FIXE",
    ),
    _ContractDef(
        "totalenergies_electricite_variable",
        "TotalEnergies Electricité Variable",
        "variable",
        "ELECTRICITE-VARIABLE",
    ),
    _ContractDef(
        "totalenergies_impact",
        "TotalEnergies Impact",
        "variable",
        "IMPACT",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef(
        "totalenergies_mycomfort",
        "TotalEnergies myComfort",
        "variable",
        "MYCOMFORT",
    ),
    _ContractDef(
        "totalenergies_mycomfort_fixed",
        "TotalEnergies myComfort Fixe",
        "fixed",
        "MYCOMFORT-FIXED",
    ),
    _ContractDef(
        "totalenergies_mydrive",
        "TotalEnergies myDrive",
        "variable",
        "MYDRIVE",
    ),
    _ContractDef(
        "totalenergies_mydynamic",
        "TotalEnergies myDynamic",
        "dynamic",
        "MYDYNAMIC",
    ),
    _ContractDef(
        "totalenergies_myessential",
        "TotalEnergies myEssential",
        "variable",
        "MYESSENTIAL",
    ),
    _ContractDef(
        "totalenergies_myessential_fixed",
        "TotalEnergies myEssential Fixe",
        "fixed",
        "MYESSENTIAL-FIXED",
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


_LISTING_URL = (
    "https://totalenergies.be/fr/particuliers/electricite-et-gaz/cartes-tarifaires"
)


def _document_url(slug: str, region: str) -> str:
    region_code = _REGION_TO_CODE[region]
    return f"{_BASE_URL}/{slug}_ELECTRICITY_{region_code}_FR.pdf"


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness probe: HEAD the per-(contract, region) PDF.

    TotalEnergies serves every card under ``/tariff-card/latest/<SLUG>_...``
    and overwrites in place, so the file's Last-Modified header is the
    right freshness signal.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if (
        contract is None
        or region not in _REGION_TO_CODE
        or region not in contract.regions
    ):
        return None
    return await head_freshness_key(session, _document_url(contract.slug, region))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return every electricity-product slug from the cartes-tarifaires page.

    The listing page links each card as
    ``tariff-card/latest/<SLUG>_ELECTRICITY_<REGION>_FR.pdf``. Strip
    the regulated TARIFF_SOCIAL entry (not a residential-market product
    and excluded from the registry). live_check diffs the result
    against ``{c.slug for c in _CONTRACTS}``.
    """
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return {
        slug
        for slug in re.findall(
            r"tariff-card/latest/([A-Z0-9\-]+)_ELECTRICITY_(?:VL|WAL|BXL)_FR",
            html,
        )
        if slug != "TARIFF_SOCIAL"
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "TotalEnergies")
    if region not in _REGION_TO_CODE:
        raise ExtractorError(f"TotalEnergies: unknown region {region!r}")
    if region not in contract.regions:
        raise ExtractorError(
            f"TotalEnergies {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _BASE_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "TotalEnergies")

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise = _extract_federal_excise(text)
    energy_contribution = _extract_energy_contribution(text)
    if energy_contribution is None:
        energy_contribution = _energy_contribution_from_table(text, region)
    if energy_contribution is None:
        # Neither the labelled line nor the DSO table fallback exposed the
        # row, so the card drifted. Fail loud rather than silently dropping
        # the contribution. A row that is PRESENT and reads zero is a valid
        # value since the levy fell to zero on 2026-08-01, so only a real
        # miss (None) raises here.
        raise ExtractorError("TotalEnergies: federal energy contribution not found")
    region_connection_fee = (
        _extract_connection_fee(text) if region == REGION_WALLONIA else 0.0
    )
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    brussels_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_renewables(text)
        dsos = _extract_flanders_dsos(text)
    elif region == REGION_WALLONIA:
        wallonia_renewables = _extract_renewables(text)
        dsos = _extract_wallonia_dsos(text)
    else:
        brussels_renewables = _extract_renewables(text)
        dsos = _extract_brussels_dsos(text)

    return SupplierSnapshot(
        supplier="totalenergies",
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
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy block -------------------------------------------------------------


# Brussels Dynamic prints "<factor> * BELPEXH +" on one line and the bases
# on the next: "0.1034 * BELPEXH + ... + Formule tarifaire\n3.85 3.85 ...".
# _resolve_consumption_formula handles both the same-line and split-line
# layouts via these two patterns.
_FACTOR_ONLY_RE = re.compile(rf"([\d.,]+)\s*\*\s*BELPEXH\s*([{SIGN_CHARS}])")
_BASE_AFTER_FORMULE_RE = re.compile(r"Formule tarifaire\s*\n\s*([\d.,]+)")


def _resolve_consumption_formula(text: str) -> tuple[float, float, float] | None:
    """Return ``(factor, sign, base_cents)`` for the consumption formula.

    The consumption formula always appears before the injection formula
    in TotalEnergies's PDFs, so the FIRST ``factor * BELPEXH`` match is
    always the consumption one. Wallonia and Flanders print the base on
    the same line (``0.1034 * BELPEXH + 1.75``); Brussels splits the
    formula across two lines (``0.1034 * BELPEXH +`` then ``3.85`` after
    ``Formule tarifaire``).
    """
    first_match = _FACTOR_ONLY_RE.search(text)
    if first_match is None:
        return None
    factor = to_float(first_match.group(1))
    sign = parse_sign(first_match.group(2))

    # Same-line base: a complete number, terminated by whitespace or
    # end-of-string, that is NOT followed by another ``* BELPEXH``
    # (which would be the next column's formula). The trailing
    # ``(?=\s|$)`` blocks the regex engine from backing off ``[\d.,]+``
    # to a shorter match (e.g. capturing ``0.103`` out of ``0.1034``).
    tail_re = re.compile(
        re.escape(first_match.group(0)) + r"\s*([\d.,]+)(?=\s|$)(?!\s*\*\s*BELPEXH)"
    )
    tail = tail_re.search(text)
    if tail is not None:
        return factor, sign, to_float(tail.group(1))

    after_formule = _BASE_AFTER_FORMULE_RE.search(text)
    if after_formule is None:
        return None
    return factor, sign, to_float(after_formule.group(1))


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(text, r"TVA\s*(\d+)\s*%")


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        consumption = _resolve_consumption_formula(text)
        if consumption is None:
            raise ExtractorError("could not parse TotalEnergies dynamic formula")
        factor_pdf, sign, base_pre_vat_cents_value = consumption
        vat = _vat_multiplier(text)
        # PDF formula yields c€/kWh (HTVA) from BELPEX in EUR/MWh; spot
        # is EUR/kWh = EUR/MWh / 1000:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=sign * base_pre_vat_cents_value * vat / 100.0,
            yearly_fixed_fee=yearly_fee,
        )

    # Variable cards index monthly: the price actually billed is the
    # realized monthly indicative ("prix mensuels calcules sur base de la
    # derniere valeur connue du BELPEX_M_RLP"), not the Vlaamse-Nutsregulator
    # annual ESTIMATE in the table below. The realized block also carries
    # the flat supplier energy of the 3-band Impact card (printed as Heures
    # PIC/MEDIUM/ECO), which the standard 4-column table layout does not
    # expose. Prefer it; fall back to the table estimate only when absent.
    if kind == "variable":
        realized = _realized_monthly_consumption(text)
        if realized is not None:
            # The realized row is that indicative, so it is what a keyless
            # entry keeps; the formula beside it is what re-prices the
            # delivery month for one carrying an ENTSO-E key.
            return _with_month_formula(
                VariableRates(
                    current=realized[0],
                    peak=realized[1],
                    offpeak=realized[2],
                    exclusive_night=realized[3],
                    yearly_fixed_fee=yearly_fee,
                ),
                text,
            )

    # Static / variable table row: 4 space-separated values (mono / jour /
    # nuit / excl_nuit) on a single line. The layout drifts per contract:
    # asterisk count after "Consommation" varies (0-3); for static the
    # values follow directly, for variable a "Tarif mensuel" label sits
    # between. The four values are separated by [ \t]+ (never a newline)
    # and the row ends at the line break: a 3-column card must miss and
    # fail loud here rather than spanning the newline to grab the yearly
    # fee as exclusive_night. For fixed this is the actual fixed price; for
    # a variable card without a realized block it is the V-test fallback.
    consumption_match = re.search(
        r"Consommation\*{0,5}\s*\n(?:\s*Tarif\s+(?:annuel|mensuel)\s*\n)?[ \t]*"
        r"([\d.,]+)[ \t]+([\d.,]+)[ \t]+([\d.,]+)[ \t]+([\d.,]+)[ \t]*(?:\n|$)",
        text,
    )
    if not consumption_match:
        raise ExtractorError(f"could not parse TotalEnergies {kind} consumption block")
    mono = to_float(consumption_match.group(1)) / 100.0
    peak = to_float(consumption_match.group(2)) / 100.0
    offpeak = to_float(consumption_match.group(3)) / 100.0
    excl_night = to_float(consumption_match.group(4)) / 100.0
    rates = fixed_or_variable_rates(
        kind,
        single=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=yearly_fee,
    )
    return _with_month_formula(rates, text)


# The variable cards index on BELPEXM_RLP over the DELIVERY month and print,
# beside the formula, "les prix mensuels calcules sur base de la derniere
# valeur connue du BELPEX_M_RLP (du mois precedent)". So the printed row is an
# indicative at LAST month's index, the same shape Cociter, Engie and Mega
# print, and billing it bills a month behind.
#
# The table flattens into two lines, the four factors on the first and the
# four bases on the second, one column per meter reading:
#
#   0.1099 * 0.1212 * 0.1 * BELPEXM_RLP + 0.1056 * Formule tarifaire
#   BELPEXM_RLP + 2.26 BELPEXM_RLP + 2.26 2.26 BELPEXM_RLP + 2.16
#
# Every dot-decimal number in the formula block, in the order printed. Not a
# formula pattern despite where it is used, which is what the name used to
# claim, and energyvision.py has a real one under that name.
#
# A factor is below 1 and a base above it on every card seen, which is what
# separates the two runs without depending on where the index token lands.
# A count the caller does not recognise is a layout it cannot read, and it
# then keeps the printed row rather than billing a half-read formula.
_DOT_DECIMAL_RE = re.compile(r"\d+\.\d+")


def _consumption_month_formula(text: str) -> list[tuple[float, float]] | None:
    """The ``factor * BELPEXM_RLP + base`` pairs the card prints, in column
    order, or ``None`` for a layout this cannot read.

    Two layouts. The four meter columns of a standard variable card print
    four pairs, verified on the September 2026 cards by inverting each
    against its own printed rate: all four solve to the same index to within
    0,3 EUR/MWh, which four independent columns only do when the pairing is
    right. Impact prints ONE pair repeated once per CWaPE band, because its
    energy leg does not band at all (the bands are the network side), and it
    inverts to the same 135,07 EUR/MWh the four sibling cards do for the same
    month.

    Any other count is refused rather than guessed at: a layout that moved,
    or a four-column card of which one column was read, and billing a
    mispaired coefficient is worse than billing the printed row.
    """
    index = text.find("BELPEXM_RLP")
    if index < 0:
        return None
    line_start = text.rfind("\n", 0, text.rfind("\n", 0, index)) + 1
    # The formula can be the last thing on the page (Impact keeps it on one
    # line), and a missing newline after it is the end of the text, not a
    # layout this cannot read.
    after = text.find("\n", index)
    line_end = text.find("\n", after + 1) if after >= 0 else -1
    block = re.sub(r"\s+", " ", text[line_start : line_end if line_end >= 0 else None])
    numbers = [float(n) for n in _DOT_DECIMAL_RE.findall(block)]
    factors = [n for n in numbers if n < 1.0]
    bases = [n for n in numbers if n >= 1.0]
    if len(factors) != len(bases) or not factors:
        return None
    pairs = list(zip(factors, bases, strict=True))
    if len(pairs) == 4:
        return pairs
    if len(pairs) == 3 and len(set(pairs)) == 1:
        # Impact's three CWaPE bands, all printing the one formula its energy
        # leg has. Three IDENTICAL pairs and no other count: a single pair is
        # a four-column card of which one column was read, and returning it
        # would put one meter's coefficients on every meter.
        return pairs[:1]
    return None


def _with_month_formula(rates: EnergyRates, text: str) -> EnergyRates:
    """Attach the delivery-month formula to a variable leg, if the card has one.

    Returns ``rates`` untouched for any other shape, and for a layout
    :func:`_consumption_month_formula` cannot read, which leaves the card
    billing its printed row exactly as before.

    A card printing ONE formula gets it on the single rate and nothing else.
    Impact is that card: it repeats the pair once per CWaPE band because its
    ENERGY leg does not band at all, the bands being the network side, and
    its leg carries no peak, off-peak or night column to hold a coefficient
    for. It solves to the same index as the four-column cards, 135,07 EUR/MWh
    on the September 2026 Wallonia set, so leaving it out kept one product of
    the range a month behind the rest.
    """
    if not isinstance(rates, VariableRates):
        return rates
    pairs = _consumption_month_formula(text)
    if pairs is None:
        return rates
    # Same conversion the dynamic branch above states, because it is the same
    # card printing the same kind of formula: the PDF yields c EUR/kWh HTVA
    # from an index in EUR/MWh, while the engine holds spots in EUR/kWh.
    #
    #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
    #   base_eur_kwh   = base_cents * vat / 100
    #
    # Dividing both by 100 instead left the factor a thousand times too small
    # and dropped the VAT: the September card's mono column resolved to
    # 0,02275 EUR/kWh against the 0,18140 it prints, about 555 EUR a year at
    # 3500 kWh. With this conversion it reproduces the printed figure exactly,
    # which is what says both the scale and the VAT reading are right.
    vat = _vat_multiplier(text)
    # BELPEXM_RLP is the day-ahead weighted by the residual load profile,
    # which the injection leg's plain BELPEXM is not: the guard on
    # _MONTH_FORMULA_RE exists to keep the two apart.
    if len(pairs) == 1:
        factor, base = pairs[0]
        return replace(
            rates,
            month_indexed=True,
            rlp_indexed=True,
            formula_factor=factor * vat * 10.0,
            formula_base=base * vat / 100.0,
        )
    (fm, bm), (fp, bp), (fo, bo), (fn, bn) = pairs
    return replace(
        rates,
        month_indexed=True,
        rlp_indexed=True,
        formula_factor=fm * vat * 10.0,
        formula_base=bm * vat / 100.0,
        formula_factor_peak=fp * vat * 10.0,
        formula_base_peak=bp * vat / 100.0,
        formula_factor_offpeak=fo * vat * 10.0,
        formula_base_offpeak=bo * vat / 100.0,
        formula_factor_exclusive_night=fn * vat * 10.0,
        formula_base_exclusive_night=bn * vat / 100.0,
    )


def _extract_yearly_fee(text: str) -> float:
    fee, _ = _extract_fee_and_renewables(text)
    return fee


def _extract_publication_month(text: str) -> str:
    match = re.search(
        r"TotalEnergies\s+(?:my\w+|Electricit[eé]\w*|Impact)[^\n]*\n([a-zéûÉ]+\s+\d{4})",
        text,
    )
    return match.group(1) if match else ""


# The realized monthly indicative block: "A titre indicatif ... les prix
# mensuels calcules sur base de la derniere valeur connue du BELPEX_M_RLP".
# This is the price actually billed; the consumption/injection rows in the
# table above it are the Vlaamse-Nutsregulator ANNUAL ESTIMATE.
_MONTHLY_BLOCK_RE = re.compile(r"prix mensuels[\s\S]{0,420}")


def _realized_monthly_consumption(
    text: str,
) -> tuple[float, float | None, float | None, float | None] | None:
    """Realized monthly consumption rates (single/peak/offpeak/excl_night).

    In the block the consumption column is printed first and the injection
    column second, so the consumption value is the first match of each
    meter label. The Impact card prints a single flat supplier rate under
    Heures PIC/MEDIUM/ECO (the band split is DSO-side), so when the
    standard bi-hourly labels are absent the PIC value is the single rate.
    Returns None when the block is absent.
    """
    block = _MONTHLY_BLOCK_RE.search(text)
    if block is None:
        return None
    body = block.group(0)

    def first(label: str) -> float | None:
        m = re.search(label + r"\s*:\s*([\d.,]+)", body)
        return to_float(m.group(1)) / 100.0 if m else None

    mono = first(r"Compteur Simple")
    peak = first(r"Heures Pleines")
    offpeak = first(r"Heures Creuses")
    excl_night = first(r"Compteur Excl\.?\s*Nuit")
    if mono is not None and peak is not None and offpeak is not None:
        return mono, peak, offpeak, excl_night
    # Impact card: flat supplier energy printed as Heures PIC/MEDIUM/ECO.
    pic = first(r"Heures PIC")
    if pic is not None:
        return pic, None, None, None
    return None


def _realized_monthly_injection(text: str) -> float | None:
    """Realized monthly injection indicative.

    Injection is the last "Compteur Simple" value in the block (the
    second/injection column on a variable card, the only one on a fixed
    card). Returns None when the block is absent.
    """
    block = _MONTHLY_BLOCK_RE.search(text)
    if block is None:
        return None
    # Injection is the last "Compteur Simple" value (standard cards) or the
    # last "Heures PIC" value (Impact cards); both are the second/injection
    # column on the line, uniform across meters/bands.
    vals = re.findall(r"Compteur Simple\s*:\s*([\d.,]+)", block.group(0)) or re.findall(
        r"Heures PIC\s*:\s*([\d.,]+)", block.group(0)
    )
    if not vals:
        return None
    return to_float(vals[-1]) / 100.0


# The non-dynamic cards print the injection formula under the
# "Injection*** (Compensation ...)" heading. Anchoring on that heading is
# required rather than reusing the dynamic branch's column-1 prefix: on two of
# the three fixtures the formula lands in the LAST column, not the first.
#
# The two optional "/" allow for the "Compteur excl. nuit" column printing a
# literal slash between the factor and the index, or between the index and the
# sign, depending on the card. \s* spans the newline that carries all three
# layouts.
#
# BELPEXM(?!_) is the guard that matters. The CONSUMPTION formula on the same
# page reads "0.1099 * BELPEXM_RLP + 2.03", a DIFFERENT, load-profile-weighted
# index; without the lookahead the search returns that instead and credits
# injection at roughly six times the right coefficient.
_INJECTION_HEADING_RE = re.compile(r"Injection\*{0,5}\s*\(Compensation[^\n]*\n")
_MONTH_FORMULA_RE = re.compile(
    rf"([\d.,]+)\s*\*\s*/?\s*BELPEXM(?!_)\s*/?\s*([{SIGN_CHARS}])\s*([\d.,]+)"
)


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    indicative = re.search(
        r"Injection\*{0,5}[^\n]*\n\s*([\d.,]+)",
        text,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    month_indexed = False
    if kind == "dynamic":
        # Injection block always prints the formula cleanly on one line
        # ("0.1 * BELPEXH -1.3 ..."). Anchor the search after "Injection"
        # so the consumption formula above can never be picked up.
        match = re.search(
            rf"Injection\*{{0,5}}[^\n]*\n[^\n]*\n\s*([\d.,]+)\s*\*\s*BELPEXH\s*"
            rf"([{SIGN_CHARS}])\s*([\d.,]+)",
            text,
        )
        if match is None:
            # A dynamic contract must price injection off the live spot via
            # factor*BELPEXH + base. Without the formula the snapshot would
            # silently fall back to the flat monthly indicative for every
            # hour - fail loud like the consumption side rather than ship a
            # wrong-shaped credit.
            raise ExtractorError(
                "TotalEnergies dynamic injection: BELPEXH formula not found"
            )
        f_pdf = to_float(match.group(1))
        b_cents = parse_sign(match.group(2)) * to_float(match.group(3))
        # Injection is VAT-exempt residential.
        factor = f_pdf * 10.0
        base = b_cents / 100.0
        formula = match.group(0)
    else:
        # Non-dynamic injection is monthly-indexed: the table value read
        # above is the Vlaamse-Nutsregulator ANNUAL ESTIMATE, while the
        # printed monthly figure is the formula at the LAST KNOWN value of
        # the index. The card says so: "Les prix mensuels de l'injection
        # calcules sur base de la derniere valeur connue du Belpex_M", and
        # the Impact card adds "La valeur exacte de l'indice repris dans
        # votre formule n'est connue qu'a la fin du mois en cours". Prefer
        # the printed figure over the annual estimate, then index it.
        realized = _realized_monthly_injection(text)
        if realized is not None:
            current = realized
        head = _INJECTION_HEADING_RE.search(text)
        match = _MONTH_FORMULA_RE.search(text, head.end()) if head else None
        if match is not None:
            # c/kWh per EUR/MWh of index, HTVA, and residential injection is
            # VAT-exempt, so neither coefficient is grossed.
            factor = to_float(match.group(1)) * 10.0
            base = parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0
            month_indexed = True
            formula = " ".join(match.group(0).split())

    if current is None and factor is None:
        return None
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
    )


# ---- taxes --------------------------------------------------------------------


def _extract_connection_fee(text: str) -> float:
    # Called only for Wallonia, where the raccordement is mandatory; raise
    # on a miss rather than silently zero it.
    match = re.search(r"Redevance de raccordement\s+([\d.,]+)", text)
    if match is None:
        raise ExtractorError("TotalEnergies: Wallonia connection fee not found")
    return to_float(match.group(1)) / 100.0


# ---- DSO row parsers ----------------------------------------------------------


# The variable products whose energy leg is a BELPEXM_RLP formula the parser
# reads, so the re-price needs spots the kind never collects a key for. Every
# variable card is one, Impact included: it prints the pair once per CWaPE
# band rather than once per meter column, and solves to the same index as the
# other four. The fixed cards and myDynamic are neither.
_MONTH_INDEXED_ENERGY: frozenset[str] = frozenset(
    {
        "totalenergies_electricite_variable",
        "totalenergies_impact",
        "totalenergies_mycomfort",
        "totalenergies_mydrive",
        "totalenergies_myessential",
    }
)


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=12.8,
    id="totalenergies",
    label="TotalEnergies",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            # Every non-dynamic card indexes its feed-in credit on the
            # monthly Belpex_M, which the fixed / variable energy leg never
            # fetches spots for. myDynamic collects the key via its own
            # BELPEXH energy formula.
            spot_indexed_injection=c.kind != "dynamic",
            month_indexed_energy=c.contract_id in _MONTH_INDEXED_ENERGY,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)
