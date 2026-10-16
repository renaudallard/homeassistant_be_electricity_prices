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

"""Elegant tariff card extractor.

Elegant sells electricity in Flanders only. Each of its three residential
products has one card at a stable address, overwritten in place when the
prices move:

    https://cdn.elegant.be/Pricing/TariffCharts/Current/<Product>Electricity_Residential.pdf

Flex (open-ended) and ComfortFlex (one year) print the same figures: one
formula per meter register, "(1,0800 x EPEXDAMREK + 1,850) x 1,06" in
c€/kWh against the index in c€/kWh, with the VAT inside. EPEXDAMREK is the
plain arithmetic mean of the delivery month's Belgian day-ahead prices, so
the card prints its rates at the last value known, the month before's, and
names it ("namelijk de waarde van september 2026. Deze bedroeg 15,641
c€/kWh voor afname"). Zeker & Vast fixes the energy for a year.

All three index the feed-in credit on EPEXDAMSPP, the SPP-weighted mean of
the same prices, "0,8500 x EPEXDAMSPP - 1,500" in c€/kWh, VAT-exempt.

Elegant publishes both series behind elegant.be/indexatie, the page the card
refers to. Its EPEXDAMREK for August 2026, 129,3175 EUR/MWh, is the plain
mean Aspiravi prints for the same month (129,32), which the integration's
own monthly mean reproduces. Its EPEXDAMSPP is the series EBEM and Trevion
publish (79,11 for August 2026, 100,53 for September), which weights each
quarter-hour by its own solar share and sits about 0,9 EUR/MWh below the
hourly SPP mean the integration computes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

import aiohttp

from ..const import DSO_FLUVIUS_INTERGEM, FLUVIUS_CARD_LABELS, REGION_FLANDERS
from ._parse import (
    SIGN_CHARS,
    numeric_row,
    parse_sign,
    regional_tax_overlay,
    require_contract,
    to_float,
)
from ._pdf import (
    NL_MONTHS,
    NUM_NO_THOUSANDS,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    printed_vat_rate,
)
from ._rates import (
    Contract,
    EnergyRates,
    FixedRates,
    InjectionRates,
    TariffKind,
    VariableRates,
)
from ._validity import end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    with_vat_basis,
)

_SUPPLIER = "elegant"
_LISTING_URL = "https://www.elegant.be/tariefkaarten"
_CURRENT_URL = "https://cdn.elegant.be/Pricing/TariffCharts/Current"
_FLANDERS_ONLY = frozenset({REGION_FLANDERS})


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    kind: TariffKind
    # The product as the card's second line names it, which is also the
    # label the config flow shows.
    name: str
    # The product's key, as the current card's file name and the listing's
    # links spell it: "<key>_Residential.pdf".
    key: str


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("elegant_flex", "variable", "Flex", "FlexElectricity"),
    _ContractDef(
        "elegant_comfortflex", "variable", "ComfortFlex", "ComfortFlexElectricity"
    ),
    _ContractDef("elegant_zeker_vast", "fixed", "Zeker & Vast", "ZekerVastElectricity"),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

_MONTH_NUMBERS = {name: index for index, name in enumerate(NL_MONTHS, 1)}
_NUM = NUM_NO_THOUSANDS
# Horizontal whitespace only, so a row never binds to the next row's figures.
_H = r"[^\S\n]"

# The first three lines of every card:
#   Tariefkaart Elektriciteit
#   Flex
#   Variabel Onbepaalde duur Versie oktober 2026 Particulier Prijzen ...
# The archive's copy follows the product with its month, "Flex 09.26".
_HEADING_RE = re.compile(
    r"Tariefkaart\s+Elektriciteit\s*\n(?P<name>[^\n]+?)(?:\s+\d{2}\.\d{2})?\s*\n"
    r"[^\n]*?\bVersie\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\s+(?P<segment>\w+)",
    re.IGNORECASE,
)
_VAT_RE = re.compile(r"Prijzen\s+inclusief\s+(\d+)\s*%\s*btw", re.IGNORECASE)
_FEE_RE = re.compile(
    rf"Vaste\s+vergoeding\s*\([^)]*jaar\)\s*(?:\(\d\))?{_H}*{_NUM}", re.IGNORECASE
)
# "Forfait zonnepanelen (€/kVA/maand)(4) 4,00": owed only on a compensation
# meter, which the integration bills in Wallonia alone.
_PV_FORFAIT_RE = re.compile(
    rf"Forfait\s+zonnepanelen\s*\([^)]*maand\)\s*(?:\(\d\))?{_H}*{_NUM}",
    re.IGNORECASE,
)
# The monthly prices, which the card prints above the yearly estimate it
# computes on the regulator's method. Only the first is read: the estimate
# is no price anybody is billed.
_MONTHLY_START = "Maandelijkse prijzen"
_MONTHLY_END = "Geschatte jaarprijs"
_INJECTION_PRINTED_RE = re.compile(
    rf"^Injectie(?:\(\d\))?{_H}+{_NUM}{_H}+{_NUM}{_H}+{_NUM}",
    re.IGNORECASE | re.MULTILINE,
)
_TIMES = r"[x×*]"
# One register's consumption formula, in c€/kWh on the index in c€/kWh:
# "Piek (1,0920 x EPEXDAMREK + 1,850) x 1,06". Anchored at the start of its
# line, where the layout reader puts the consumption column.
_ENERGY_ROWS: tuple[tuple[str, str], ...] = (
    ("single", "24h"),
    ("peak", "Piek"),
    ("offpeak", "Dal"),
    ("exclusive_night", r"Excl\.\s*nacht"),
)
_ENERGY_FORMULA = (
    rf"{_H}+\(\s*{_NUM}\s*{_TIMES}\s*EPEXDAMREK\s*([{SIGN_CHARS}])\s*{_NUM}\s*\)"
    rf"\s*{_TIMES}\s*{_NUM}"
)
_ENERGY_RES = {
    slot: re.compile(rf"^{label}{_ENERGY_FORMULA}", re.IGNORECASE | re.MULTILINE)
    for slot, label in _ENERGY_ROWS
}
# The feed-in formula, printed once per register beside the consumption one:
# "24h (1,0800 x EPEXDAMREK + 1,850) x 1,06 24h 0,8500 x EPEXDAMSPP - 1,500".
_INJECTION_FORMULA_RES = tuple(
    re.compile(
        rf"\b{label}{_H}+{_NUM}\s*{_TIMES}\s*EPEXDAMSPP\s*([{SIGN_CHARS}])\s*{_NUM}",
        re.IGNORECASE,
    )
    for label in ("24h", "Piek", "Dal")
)

# The network table prints the eight Fluvius areas as the shared table spells
# them, Midden-Vlaanderen cut short.
_AREAS = {
    **{
        label: key
        for label, key in FLUVIUS_CARD_LABELS.items()
        if key != DSO_FLUVIUS_INTERGEM
    },
    "Fluvius Midden-Vl.": DSO_FLUVIUS_INTERGEM,
}
_NETWORK_START = "Nettarieven"
_NETWORK_END = "Tarief databeheer"
_DATA_MANAGEMENT_RE = re.compile(
    rf"Tarief\s+databeheer\s+{_NUM}\s*€\s*/\s*jaar", re.IGNORECASE
)

# The tax block. The excise opens a table by annual volume, which
# regional_tax_overlay reads into bands while its rates differ.
_EXCISE_RE = re.compile(
    rf"Federale\s+Accijns\s+Verbruik\s+0\s*[{SIGN_CHARS}]\s*[\d.]+\s*kWh\s+{_NUM}",
    re.IGNORECASE,
)
_GSC_RE = re.compile(
    rf"Bijdrage\s+groene\s+stroom\s*\(c[^)]*\)\s*{_NUM}", re.IGNORECASE
)
_WKK_RE = re.compile(
    rf"Bijdrage\s+warmtekrachtkoppeling\s*\(c[^)]*\)\s*{_NUM}", re.IGNORECASE
)
_CONTRIBUTION_RE = re.compile(rf"Energiebijdrage\s+{_NUM}\s*c€", re.IGNORECASE)
# The residential row, not the 10,07 "Niet-residentiëel" one under it.
_FUND_RE = re.compile(
    rf"Bijdrage\s+energiefonds\s+Residenti\w*\s+{_NUM}\s*€", re.IGNORECASE
)

# A residential electricity card the listing links: "<Product>Electricity".
_LISTED_RE = re.compile(r"/Current/([A-Za-z]+Electricity)_Residential\.pdf")


# ---- public entry points -----------------------------------------------------


def _contract(contract_id: str, region: str) -> _ContractDef:
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Elegant")
    if region != REGION_FLANDERS:
        raise ExtractorError(f"Elegant {contract_id}: not sold in region {region!r}")
    return contract


def _current_url(contract: _ContractDef) -> str:
    return f"{_CURRENT_URL}/{contract.key}_Residential.pdf"


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """The card at the product's stable address."""
    contract = _contract(contract_id, region)
    url = _current_url(contract)
    return parse_snapshot(contract_id, await fetch_pdf_text_layout(session, url), url)


async def probe(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> str | None:
    """The card's ``Last-Modified``, which moves when the card at its stable
    address is overwritten."""
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None or region != REGION_FLANDERS:
        return None
    return await head_freshness_key(session, _current_url(contract))


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The residential electricity products the listing links, by contract id
    where one is registered and by Elegant's own product key where none is."""
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    known = {c.key: c.contract_id for c in _CONTRACTS}
    return {known.get(key, key) for key in _LISTED_RE.findall(html)}


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, source_url: str = _LISTING_URL
) -> SupplierSnapshot:
    """Parse one card's text as pdfplumber lays it out."""
    contract = _contract(contract_id, REGION_FLANDERS)
    month = _card_month(text, contract)
    energy: EnergyRates = (
        _fixed_energy(text) if contract.kind == "fixed" else _variable_energy(text)
    )
    forfait = _PV_FORFAIT_RE.search(text)
    return with_vat_basis(
        SupplierSnapshot(
            supplier=_SUPPLIER,
            contract=contract_id,
            energy=energy,
            injection=_injection(text),
            dsos=_dsos(text),
            taxes=regional_tax_overlay(
                text,
                supplier="Elegant",
                region=REGION_FLANDERS,
                excise=(_EXCISE_RE,),
                renewables=(_GSC_RE, _WKK_RE),
                contribution=_CONTRIBUTION_RE,
                fund=_FUND_RE,
            ),
            source_url=source_url,
            publication_label=f"{NL_MONTHS[month.month - 1]} {month.year}",
            valid_until=end_of_month(month.year, month.month),
            supplier_prosumer_eur_per_kva_year=(
                to_float(forfait.group(1)) * 12.0 if forfait else None
            ),
        ),
        printed_vat_rate(text, _VAT_RE),
        # The consumption formula carries its own "x 1,06".
        grossed=False,
    )


def _card_month(text: str, contract: _ContractDef) -> date:
    """The month the card is for, once it is shown to be the product's own.

    Flex and ComfortFlex print the same figures, so nothing below the heading
    would tell one card from the other, and the non-residential edition of
    each prints the same layout.
    """
    match = _HEADING_RE.search(text)
    if match is None:
        raise ExtractorError("Elegant: card heading not found")
    name, segment = " ".join(match.group("name").split()), match.group("segment")
    if name != contract.name or segment.casefold() != "particulier":
        raise ExtractorError(
            f"Elegant: card is for {name} {segment}, not {contract.name} Particulier"
        )
    month = _MONTH_NUMBERS.get(match.group("month").lower())
    if month is None:
        raise ExtractorError(f"Elegant: unknown month {match.group('month')!r}")
    return date(int(match.group("year")), month, 1)


def _fee(text: str) -> float:
    match = _FEE_RE.search(text)
    if match is None:
        raise ExtractorError("Elegant: yearly fixed fee not found")
    return to_float(match.group(1))


def _monthly_block(text: str) -> str:
    start = text.find(_MONTHLY_START)
    end = text.find(_MONTHLY_END, start)
    if start < 0 or end < 0:
        raise ExtractorError("Elegant: monthly price table not found")
    return text[start:end]


def _printed_rates(text: str) -> list[float]:
    """The single, peak, off-peak and exclusive night rates the card prints,
    in EUR/kWh."""
    row = numeric_row(_monthly_block(text), "Afname", 4)
    if row is None:
        raise ExtractorError("Elegant: printed consumption rates not found")
    return [to_float(value) / 100.0 for value in row]


def _fixed_energy(text: str) -> FixedRates:
    single, peak, offpeak, night = _printed_rates(text)
    return FixedRates(
        single=single,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=night,
        yearly_fixed_fee=_fee(text),
    )


def _variable_energy(text: str) -> VariableRates:
    """The four registers' formulas and the rates printed at last month's
    index.

    The formula is in c€/kWh against an index in c€/kWh, so against a mean in
    EUR/kWh the factor keeps its value and the base goes from cents to euros;
    both are grossed by the multiplier the formula prints. Every register
    carries its own pair, piek and dal apart, so each is read on its own row.
    """
    pairs: dict[str, tuple[float, float]] = {}
    formulas: list[str] = []
    for slot, pattern in _ENERGY_RES.items():
        match = pattern.search(text)
        if match is None:
            raise ExtractorError(f"Elegant: {slot} consumption formula not found")
        factor, sign, base, vat = match.groups()
        multiplier = to_float(vat)
        pairs[slot] = (
            to_float(factor) * multiplier,
            parse_sign(sign) * to_float(base) / 100.0 * multiplier,
        )
        formulas.append(" ".join(match.group(0).split()))
    single, peak, offpeak, night = _printed_rates(text)
    return VariableRates(
        current=single,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=night,
        yearly_fixed_fee=_fee(text),
        formula=" · ".join(formulas) + " c€/kWh",
        formula_factor=pairs["single"][0],
        formula_base=pairs["single"][1],
        formula_factor_peak=pairs["peak"][0],
        formula_base_peak=pairs["peak"][1],
        formula_factor_offpeak=pairs["offpeak"][0],
        formula_base_offpeak=pairs["offpeak"][1],
        formula_factor_exclusive_night=pairs["exclusive_night"][0],
        formula_base_exclusive_night=pairs["exclusive_night"][1],
        # EPEXDAMREK is the plain mean, so the coefficients resolve on it and
        # not on a load-weighted one.
        month_indexed=True,
    )


def _injection(text: str) -> InjectionRates:
    """The feed-in formula on the delivery month's EPEXDAMSPP, with the rate
    printed at last month's as the fallback.

    The card prints the formula and the rate once per register, the same on
    each. A field holds one credit for every register, so a card that gives
    a register its own is refused rather than credited at the single meter's.
    """
    formulas = []
    for pattern in _INJECTION_FORMULA_RES:
        match = pattern.search(text)
        if match is None:
            raise ExtractorError("Elegant: feed-in formula not found")
        factor, sign, base = match.groups()
        formulas.append((to_float(factor), parse_sign(sign) * to_float(base)))
    printed = _INJECTION_PRINTED_RE.search(_monthly_block(text))
    if printed is None:
        raise ExtractorError("Elegant: printed feed-in rate not found")
    rates = {to_float(value) for value in printed.groups()}
    if len(set(formulas)) != 1 or len(rates) != 1:
        raise ExtractorError("Elegant: the feed-in credit differs per register")
    factor, base = formulas[0]
    return InjectionRates(
        # Residential feed-in is VAT-exempt, as the card's footnote says, so
        # neither the rate nor the formula is grossed.
        current=rates.pop() / 100.0,
        factor=factor,
        base=base / 100.0,
        formula=f"{factor} x EPEXDAMSPP {base:+} c€/kWh",
        spp_indexed=True,
    )


def _dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital meter's half of the network table, and the classic
    meter's prosumer rate.

    A row reads the digital meter's capacity, distribution and
    exclusive-night rates, then the classic meter's three and its prosumer
    rate. Every area has to read: a partial table would be adopted as the
    card and an entry on the missing area would fail every tick.
    """
    data = _DATA_MANAGEMENT_RE.search(text)
    if data is None:
        raise ExtractorError("Elegant: data management fee not found")
    out: dict[str, DsoOverlay] = {}
    for label, key in _AREAS.items():
        row = numeric_row(text, label, 7, after=_NETWORK_START, before=_NETWORK_END)
        if row is None:
            raise ExtractorError(f"Elegant: network row {label!r} not read")
        capacity, single, night, _, _, _, prosumer = (to_float(v) for v in row)
        out[key] = DsoOverlay(
            distribution_single=single / 100.0,
            distribution_exclusive_night=night / 100.0,
            # The table is headed "Transport- en distributiekosten": one rate
            # holding both, as on every Flemish card.
            transport=0.0,
            data_management_per_year=to_float(data.group(1)),
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


EXTRACTOR = SupplierExtractor(
    id=_SUPPLIER,
    label="Elegant",
    sweep_cost_s=1.8,
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.name,
            kind=c.kind,
            regions=_FLANDERS_ONLY,
            spot_indexed_injection=True,
            month_indexed_energy=c.kind == "variable",
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
)

__all__ = [
    "EXTRACTOR",
    "discover",
    "fetch",
    "parse_snapshot",
    "probe",
]
