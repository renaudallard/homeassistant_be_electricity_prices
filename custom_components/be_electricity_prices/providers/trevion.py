# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# Copyright (c) 2026, Koen Dierckx <koen.dierckx@gmail.com>
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

"""Trevion residential electricity tariff cards.

Trevion publishes a public, month-labelled PDF archive.  The cards are
VAT-inclusive for residential customers; injection is VAT-exempt.
"""

from __future__ import annotations

import re
from calendar import monthrange
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
    archive_validity_check,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
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
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
)

_LISTING_URL = "https://trevion.be/tariefkaarten/"
_BASE_URL = "https://trevion.be"
_NUM = r"[\d.,]+"
_MONTH = r"(?:januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)"

_DSOS = {
    "Fluvius Antwerpen": DSO_FLUVIUS_ANTWERPEN,
    "Fluvius Halle-Vilvoorde": DSO_FLUVIUS_HALLE_VILVOORDE,
    "Fluvius Imewo": DSO_FLUVIUS_IMEWO,
    "Fluvius Kempen": DSO_FLUVIUS_IVEKA,
    "Fluvius Limburg": DSO_FLUVIUS_LIMBURG,
    "Fluvius Midden-Vlaanderen": DSO_FLUVIUS_INTERGEM,
    "Fluvius West": DSO_FLUVIUS_WEST,
    "Fluvius Zenne-Dijle": DSO_FLUVIUS_ZENNE_DIJLE,
}


@dataclass(frozen=True)
class _ContractDef:
    id: str
    label: str
    kind: TariffKind
    card_re: str


_CONTRACTS = (
    _ContractDef("groene_energie_vast", "Groene Energie Vast", "fixed", "VAST"),
    _ContractDef(
        "groene_stroom_flex", "Groene Stroom Flex", "spot_monthly", "Groene-Stroom-Flex"
    ),
    _ContractDef(
        "groene_energie_dynamisch",
        "Groene Energie Dynamisch",
        "dynamic",
        r"Groene-Energie-Dynamisch(?!-Plus)",
    ),
    _ContractDef(
        "groene_energie_dynamisch_plus",
        "Groene Energie Dynamisch Plus",
        "dynamic",
        "Groene-Energie-Dynamisch-Plus",
    ),
    _ContractDef(
        "lifepowr",
        "LifePowr by Trevion",
        "spot_monthly",
        "LifePowrByTrevion",
    ),
    _ContractDef("energreen", "Energreen by Trevion", "dynamic", "EnergreenByTrevion"),
)
_BY_ID = {item.id: item for item in _CONTRACTS}


def _number(value: str) -> float:
    return to_float(value.replace(".", "") if "," in value and "." in value else value)


def _archive_re(contract: _ContractDef) -> re.Pattern[str]:
    return re.compile(
        rf'href="([^" ]*{contract.card_re}[^" ]*particulier[^" ]*-(\d{{6}})(?:-\d+)?\.pdf)"',
        re.IGNORECASE,
    )


def _card_url(path: str) -> str:
    return path if path.startswith("http") else _BASE_URL + path


async def _find_card(
    session: aiohttp.ClientSession, contract: _ContractDef, month: date | None = None
) -> tuple[str, str]:
    html = await fetch_text(session, _LISTING_URL)
    matches = _archive_re(contract).findall(html)
    if not matches:
        raise ExtractorError(f"Trevion: no card found for {contract.id}")
    parsed = [
        (path, stamp, date(int(stamp[:4]), int(stamp[4:]), 1))
        for path, stamp in matches
    ]
    if month is not None:
        parsed = [
            item for item in parsed if item[2] == date(month.year, month.month, 1)
        ]
        if not parsed:
            raise ExtractorError(
                f"Trevion: no archived card for {contract.id} {month:%Y-%m}"
            )
    path, stamp, _ = max(parsed, key=lambda item: item[2])
    return _card_url(path), f"{stamp[:4]}-{stamp[4:]}"


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    contract = _BY_ID.get(contract_id)
    if contract is None or region != REGION_FLANDERS:
        raise ExtractorError(f"unknown or unsupported Trevion contract {contract_id!r}")
    url, label = await _find_card(session, contract)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url, label)


async def fetch_for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, year_month: date
) -> SupplierSnapshot | None:
    contract = _BY_ID.get(contract_id)
    if contract is None or region != REGION_FLANDERS:
        return None
    try:
        url, label = await _find_card(session, contract, year_month)
        text = await fetch_pdf_text_layout(session, url)
        return archive_validity_check(
            parse_snapshot(contract_id, text, url, label), text, year_month
        )
    except ExtractorError:
        return None


async def probe(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> str | None:
    return (
        await head_freshness_key(session, _LISTING_URL)
        if contract_id in _BY_ID and region == REGION_FLANDERS
        else None
    )


def _extract_validity(text: str) -> date | None:
    months = {
        name: index
        for index, name in enumerate(
            (
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
            ),
            1,
        )
    }
    match = re.search(rf"{_MONTH}\s+(20\d{{2}})", text, re.IGNORECASE)
    if not match:
        return None
    month_name = match.group(0).split()[0].lower()
    year = int(match.group(1))
    month = months[month_name]
    return date(year, month, monthrange(year, month)[1])


def _meter_shared_values(text: str) -> tuple[float, float, float]:
    """Return the green, CHP, and yearly-fee columns from the meter table."""
    row = re.search(r"^(?:Tweevoudig|SMR3)\s+(.+)$", text, re.IGNORECASE | re.MULTILINE)
    if row:
        values = [_number(value) for value in re.findall(_NUM, row.group(1))]
        if len(values) >= 5:
            return values[2], values[3], values[4]
        if len(values) == 3:
            return values[0], values[1], values[2]

    table = re.sub(r"\s+", " ", text)
    reordered = re.search(
        rf"Enkelvoudig\s+{_NUM}\s+{_NUM}\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+Tweevoudig",
        table,
        re.IGNORECASE,
    )
    if reordered:
        green, chp, fee = (_number(value) for value in reordered.groups())
        return green, chp, fee
    raise ExtractorError("Trevion: shared meter costs not found")


def _fee(text: str) -> float:
    return _meter_shared_values(text)[2]


def _extract_fixed(text: str) -> tuple[FixedRates, InjectionRates]:
    table = re.sub(r"\s+", " ", text)
    single = re.search(rf"Enkelvoudig\s+({_NUM})\s+({_NUM})", table, re.IGNORECASE)
    peak = re.search(rf"Piekuren\s+({_NUM})\s+({_NUM})", table, re.IGNORECASE)
    offpeak = re.search(rf"Daluren\s+({_NUM})\s+({_NUM})", table, re.IGNORECASE)
    night = re.search(rf"Exclusief\s+Nacht\s+({_NUM})", table, re.IGNORECASE)
    if not single or not peak or not offpeak or not night:
        raise ExtractorError("Trevion: fixed energy table not found")
    return (
        FixedRates(
            single=_number(single.group(1)) / 100.0,
            peak=_number(peak.group(1)) / 100.0,
            offpeak=_number(offpeak.group(1)) / 100.0,
            exclusive_night=_number(night.group(1)) / 100.0,
            yearly_fixed_fee=_fee(text),
        ),
        InjectionRates(
            current=_number(single.group(2)) / 100.0,
            peak=_number(peak.group(2)) / 100.0,
            offpeak=_number(offpeak.group(2)) / 100.0,
            bi_hourly=True,
        ),
    )


def _extract_formula(text: str, marker: str) -> tuple[float, float]:
    match = re.search(
        rf"\(({_NUM})\*\s*{marker}\s*([{SIGN_CHARS}])\s*({_NUM})\)\s*\*\s*1[.,]06",
        text,
        re.IGNORECASE,
    )
    if not match:
        raise ExtractorError(f"Trevion: formula not found for {marker}")
    factor = _number(match.group(1)) * 1.06
    base = (
        (_number(match.group(3)) if match.group(2) == "+" else -_number(match.group(3)))
        / 1000.0
        * 1.06
    )
    return factor, base


def _extract_monthly(text: str) -> tuple[SpotMonthlyRates, InjectionRates]:
    marker = "Belpex_RLP_VL"
    factor, base = _extract_formula(text, marker)
    # The multiplication sign moved from "x" to "*" between the May and the
    # June 2026 cards; both are read.
    injection = re.search(
        rf"teruglevering.*?formule.*?({_NUM})\s*[*x×]\s*Belpex_SPP_BE\s*([{SIGN_CHARS}])\s*({_NUM})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not injection:
        raise ExtractorError("Trevion: monthly injection formula not found")
    known_index = re.search(
        rf"Belpex_SPP_BE parameter.*?laatst gekende waarde.*?\(({_NUM})\s*€/MWh\)",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not known_index:
        raise ExtractorError("Trevion: latest SPP index not found")
    inj_factor = _number(injection.group(1)) * 10.0
    inj_base = (
        _number(injection.group(3))
        if injection.group(2) == "+"
        else -_number(injection.group(3))
    ) / 100.0
    current = inj_factor * (_number(known_index.group(1)) / 1000.0) + inj_base
    return SpotMonthlyRates(
        factor=factor,
        base=base,
        rlp_indexed=True,
        rlp_blend="flanders",
        yearly_fixed_fee=_fee(text),
    ), InjectionRates(
        current=current, factor=inj_factor, base=inj_base, spp_indexed=True
    )


def _extract_dynamic(text: str) -> tuple[DynamicRates, InjectionRates]:
    factor, base = _extract_formula(text, "Belpex 15 MTU")
    injection = re.search(
        rf"teruglevering.*?formule.*?({_NUM})\s*[*x×]\s*Belpex 15 MTU\s*([{SIGN_CHARS}])\s*({_NUM})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not injection:
        raise ExtractorError("Trevion: dynamic injection formula not found")
    inj_factor = _number(injection.group(1)) * 10.0
    inj_base = (
        _number(injection.group(3))
        if injection.group(2) == "+"
        else -_number(injection.group(3))
    ) / 100.0
    return DynamicRates(
        factor=factor, base=base, quarter_hourly=True, yearly_fixed_fee=_fee(text)
    ), InjectionRates(factor=inj_factor, base=inj_base)


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find("Digitale Meter")
    end = text.find("Analoge Meter", start)
    section = text[start : end if end >= 0 else len(text)]
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSOS.items():
        match = re.search(
            rf"{re.escape(label)}\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})",
            section,
            re.IGNORECASE,
        )
        if match:
            out[key] = DsoOverlay(
                distribution_single=_number(match.group(2)) / 100.0,
                distribution_exclusive_night=_number(match.group(3)) / 100.0,
                transport=0.0,
                capacity_eur_per_kw_year=_number(match.group(1)),
                data_management_per_year=_number(match.group(4)),
            )
    if not out:
        raise ExtractorError("Trevion: digital-meter DSO table not found")
    return out


def _extract_taxes(text: str) -> TaxOverlay:
    contribution = re.search(
        rf"^Bijdrage op de energie[^\n]*?\)\s+({_NUM})\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    excise = re.search(
        rf"^Bijzondere accijns[^\n]*?\)\s+({_NUM})\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    if not excise:
        band = re.search(
            r"50-1000\s*MWh(.*?)Bijdrage energiefonds",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        band_values = re.findall(_NUM, band.group(1)) if band else []
        excise_value = band_values[-1] if band_values else None
    else:
        excise_value = excise.group(1)
    fund = re.search(
        rf"Bijdrage energiefonds met domicilie.*?\(\d+\)\s+({_NUM})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if not contribution or excise_value is None or not fund:
        raise ExtractorError("Trevion: tax block not found")
    green, chp, _ = _meter_shared_values(text)
    return TaxOverlay(
        federal_excise=_number(excise_value) / 100.0,
        energy_contribution=_number(contribution.group(1)) / 100.0,
        flanders_renewables=(green + chp) / 100.0,
        energy_fund_eur_per_month=_number(fund.group(1)),
        vat_rate=0.0,
    )


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str = _LISTING_URL,
    publication_label: str = "",
) -> SupplierSnapshot:
    contract = _BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown Trevion contract {contract_id!r}")
    energy: EnergyRates
    injection: InjectionRates
    if contract.kind == "fixed":
        energy, injection = _extract_fixed(text)
    elif contract.kind == "spot_monthly" and "Belpex_RLP_VL" in text:
        energy, injection = _extract_monthly(text)
    else:
        # The dynamic products, and LifePowr's cards up to May 2026: it
        # billed per quarter-hour on Belpex 15 MTU before it became a monthly
        # Belpex_RLP_VL product in June, and a past month is stored as the
        # product it was.
        energy, injection = _extract_dynamic(text)
    return SupplierSnapshot(
        supplier="trevion",
        contract=contract_id,
        energy=energy,
        injection=injection,
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=_extract_validity(text),
    )


EXTRACTOR = SupplierExtractor(
    id="trevion",
    label="Trevion",
    sweep_cost_s=8.0,
    contracts=tuple(
        Contract(
            id=item.id,
            label=item.label,
            kind=item.kind,
            regions=frozenset({REGION_FLANDERS}),
        )
        for item in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)

__all__ = ["EXTRACTOR", "fetch", "fetch_for_month", "parse_snapshot", "probe"]
