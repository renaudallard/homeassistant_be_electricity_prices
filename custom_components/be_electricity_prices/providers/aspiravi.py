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

"""Aspiravi Energy tariff card extractor.

Aspiravi Energy sells one residential product, Eco Plus Flex, to members of
its partner cooperatives, in Flanders only. Its energy is indexed on the
arithmetic mean of the Belpex day-ahead quarter-hour prices of the delivery
month, with one formula per meter register, and the card prints the rates at
the previous month's mean. The feed-in credit is indexed on the same mean.

The current card is linked as "Huidige tariefkaart" from the downloads page,
and every past card from the tariff page under a label naming its month. File
names follow no pattern a URL could be built from, so both are scraped.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date

import aiohttp

from ..const import FLUVIUS_CARD_LABELS, REGION_FLANDERS
from ._parse import SIGN_CHARS, numeric_row, parse_sign, regional_tax_overlay, to_float
from ._pdf import (
    NL_MONTHS,
    fetch_pdf_text,
    fetch_text,
    head_freshness_key,
    is_transient_fetch_error,
    printed_vat_rate,
    vat_multiplier,
)
from ._rates import Contract, InjectionRates, VariableRates
from ._validity import archive_validity_check, end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    with_vat_basis,
)

_SUPPLIER = "aspiravi"
_CONTRACT_ID = "aspiravi_eco_plus_flex"
_DOWNLOADS_URL = "https://aspiravi-energy.be/downloads/"
_ARCHIVE_URL = "https://aspiravi-energy.be/tariefkaarten/"

# Every tariff card is a PDF named after the product's code, 900027 for
# Eco Plus Flex, whatever else the file name says. A code no contract carries
# is a product the registry does not know yet.
_PRODUCT_CODES = {"900027": _CONTRACT_ID}
_PRODUCT_CODE_RE = re.compile(r"(?:^|[_-])(\d{6})[_-]")

_LINK_RE = re.compile(r'<a\b[^>]*\bhref="([^"]+\.pdf)"[^>]*>(.*?)</a>', re.I | re.S)
# "900027 eco plus flex (huishoudelijk) aug 26": the month, spelled out or cut
# short, and a two-digit year. The two labels naming a year alone ("eco plus
# flex 2023") are cards of an older layout and match nothing.
_ARCHIVE_LABEL_RE = re.compile(r"\(huishoudelijk\)\s+([a-z]+)\s+(\d{2})$", re.I)
# The months by their first three letters, which is how the price table and
# the archive labels both abbreviate them; "mrt" is the table's March.
_MONTHS = {name[:3]: index for index, name in enumerate(NL_MONTHS, 1)} | {"mrt": 3}

_NUM = r"(\d+(?:[.,]\d+)?)"
_FORMULA = rf"{_NUM}\s*\*\s*Belpex\s*([{SIGN_CHARS}])\s*{_NUM}"
_MONO_RE = re.compile(rf"Enkelvoudige dagmeter\s+{_FORMULA}")
_PEAK_RE = re.compile(rf"Tweevoudige meter\s+Dag\s+{_FORMULA}")
# Capitalised and followed by the formula, so the "Exclusief nachtmeter" row
# below it never matches.
_OFFPEAK_RE = re.compile(rf"\bNacht\s+{_FORMULA}")
_NIGHT_RE = re.compile(rf"Exclusief nachtmeter\s+{_FORMULA}")
_INJECTION_RE = re.compile(rf"Terugleververgoeding\s+{_FORMULA}")
# The last row of the "Energiekost van de afgelopen 12 maanden" table: the
# month the printed rates were computed on, its Belpex mean and the four
# rates. It is what dates the card. The sentence naming the month the card is
# for, and the start of the period its formulas hold for, were both left on
# the previous month on the September 2025 and March 2026 cards, while this
# row moved on every time.
_INDEX_ROW_RE = re.compile(
    rf"^([A-Za-z]{{3}})/(\d{{2}})\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s*$",
    re.M,
)
_FEE_RE = re.compile(
    rf"Vaste vergoeding \(€/jaar\) incl\.\s*BTW\s+{_NUM}\s+{_NUM}\s+{_NUM}"
)
_INJECTION_PRINTED_RE = re.compile(
    rf"Terugleververgoeding \(c€/kWh\) excl\.\s*BTW\s+{_NUM}"
)
# The charity contribution, 1 EUR/MWh before VAT on every meter. The card says
# it is part of the energy price, while its formulas and its printed rates both
# leave it out: 0,116 x 129,32 + 2 grossed by 6% is the 18,021 printed for
# August 2026, without it.
_CHARITY_RE = re.compile(rf"goed doel incl\.\s*BTW\s+{_NUM}")
_VAT_RE = re.compile(r"incl\.\s*(\d+)\s*%\s*BTW", re.I)
_EXCISE_RE = re.compile(rf"Verbruik tussen 0\s*[{SIGN_CHARS}]\s*20\.000 kWh\s+{_NUM}")
_CONTRIBUTION_RE = re.compile(rf"Bijdrage op de energie \(c€/kWh\)\s+{_NUM}")
_FUND_RE = re.compile(rf"Laagspanning residentieel\s+{_NUM}")
_GSC_RE = re.compile(rf"Kosten Groene stroom excl\.\s*BTW\s+{_NUM}")
_WKK_RE = re.compile(rf"Kosten WKK excl\.\s*BTW\s+{_NUM}")


def _links(page: str) -> list[tuple[str, str]]:
    """``(url, label)`` for every PDF the page links, the label as read."""
    return [
        (url, " ".join(re.sub(r"<[^>]+>", " ", label).split()))
        for url, label in _LINK_RE.findall(page)
    ]


async def _current_card_url(session: aiohttp.ClientSession) -> str:
    page = await fetch_text(session, _DOWNLOADS_URL)
    for url, label in _links(page):
        if label.lower() == "huidige tariefkaart":
            return url
    raise ExtractorError(f"Aspiravi: no current card linked at {_DOWNLOADS_URL}")


def _archive_month(label: str) -> date | None:
    """The month an archive link's label names, or ``None``."""
    match = _ARCHIVE_LABEL_RE.search(label)
    if match is None:
        return None
    month = _MONTHS.get(match.group(1).lower()[:3])
    return None if month is None else date(2000 + int(match.group(2)), month, 1)


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Aspiravi only sells in Flanders.
) -> SupplierSnapshot:
    """Fetch and parse the current Eco Plus Flex card."""
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown Aspiravi contract {contract_id!r}")
    url = await _current_card_url(session)
    return parse_snapshot(contract_id, await fetch_pdf_text(session, url), url)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Aspiravi only sells in Flanders.
) -> str | None:
    """The current card's ``Last-Modified``.

    The downloads page sends no freshness header, so it is read to find the
    card and the card itself is asked. A new card is a new upload, so its
    header moves with it.
    """
    if contract_id != _CONTRACT_ID:
        return None
    try:
        url = await _current_card_url(session)
    except ExtractorError:
        return None
    return await head_freshness_key(session, url)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Aspiravi only sells in Flanders.
    year_month: date,
) -> SupplierSnapshot | None:
    """The card published for ``year_month``, or ``None``.

    Every link labelled with the month is tried, because two months carry a
    second link to a card of another year, and the first that proves to be
    the month's card wins. The cards before April 2024 use another layout and
    do not parse, which leaves those months to the proxy.
    """
    if contract_id != _CONTRACT_ID:
        return None
    try:
        page = await fetch_text(session, _ARCHIVE_URL)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise,
        # so the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None
    for url, label in _links(page):
        if _archive_month(label) != year_month.replace(day=1):
            continue
        try:
            text = await fetch_pdf_text(session, url)
            snap = parse_snapshot(contract_id, text, url)
        except ExtractorError as err:
            if is_transient_fetch_error(str(err)):
                raise
            continue
        checked = archive_validity_check(snap, text, year_month)
        if checked is not None:
            return checked
    return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """The contract ids of every product whose card either page links.

    A product code no contract carries surfaces as ``aspiravi_<code>``.
    """
    out: set[str] = set()
    for page_url in (_DOWNLOADS_URL, _ARCHIVE_URL):
        try:
            page = await fetch_text(session, page_url)
        except ExtractorError:
            continue
        for url, _label in _links(page):
            match = _PRODUCT_CODE_RE.search(url.rsplit("/", 1)[-1])
            if match is not None:
                code = match.group(1)
                out.add(_PRODUCT_CODES.get(code, f"{_SUPPLIER}_{code}"))
    return out


# ---- pure parser -------------------------------------------------------------


def parse_snapshot(
    contract_id: str, text: str, source_url: str = _DOWNLOADS_URL
) -> SupplierSnapshot:
    """Parse one Eco Plus Flex card."""
    if contract_id != _CONTRACT_ID:
        raise ExtractorError(f"unknown Aspiravi contract {contract_id!r}")
    index = _index_row(text)
    card_month = date(
        index[0].year + (index[0].month == 12), index[0].month % 12 + 1, 1
    )
    vat = vat_multiplier(text, _VAT_RE)
    taxes = regional_tax_overlay(
        text,
        supplier="Aspiravi",
        region=REGION_FLANDERS,
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_CONTRIBUTION_RE,
        fund=_FUND_RE,
    )
    return with_vat_basis(
        SupplierSnapshot(
            supplier=_SUPPLIER,
            contract=contract_id,
            energy=_extract_energy(text, index[1], vat),
            injection=_extract_injection(text),
            dsos=_extract_dsos(text),
            # The card prints its green power and WKK costs before VAT, unlike
            # the rest of the block.
            taxes=replace(taxes, flanders_renewables=taxes.flanders_renewables * vat),
            source_url=source_url,
            publication_label=f"{NL_MONTHS[card_month.month - 1]} {card_month.year}",
            valid_until=end_of_month(card_month.year, card_month.month),
        ),
        printed_vat_rate(text, _VAT_RE),
    )


def _index_row(text: str) -> tuple[date, list[float]]:
    """The month the printed rates are for and those four rates, c€/kWh."""
    rows = _INDEX_ROW_RE.findall(text)
    if not rows:
        raise ExtractorError("Aspiravi: price table of the past 12 months not found")
    name, year, _belpex, *rates = rows[-1]
    month = _MONTHS.get(name.lower())
    if month is None:
        raise ExtractorError(f"Aspiravi: unknown month {name!r} in the price table")
    return date(2000 + int(year), month, 1), [to_float(r) for r in rates]


def _formula(pattern: re.Pattern[str], text: str, label: str) -> tuple[float, float]:
    """The ``factor * Belpex + base`` of one row, as printed: c€/kWh per EUR/MWh
    and c€/kWh, before VAT."""
    match = pattern.search(text)
    if match is None:
        raise ExtractorError(f"Aspiravi: {label} formula not found")
    return to_float(match.group(1)), parse_sign(match.group(2)) * to_float(
        match.group(3)
    )


def _extract_energy(text: str, printed: list[float], vat: float) -> VariableRates:
    """The four registers' formulas and the rates printed at last month's mean.

    Both carry the charity contribution the card leaves out of them. The
    formulas are grossed by the card's VAT onto the EUR/kWh basis the rates
    are in.
    """
    match = _CHARITY_RE.search(text)
    if match is None:
        raise ExtractorError("Aspiravi: charity contribution row not found")
    charity = to_float(match.group(1)) / 100.0
    printed_formulas = {
        label: _formula(pattern, text, label)
        for label, pattern in (
            ("mono", _MONO_RE),
            ("peak", _PEAK_RE),
            ("off-peak", _OFFPEAK_RE),
            ("excl. night", _NIGHT_RE),
        )
    }
    mono, peak, offpeak, night = (
        (factor * vat * 10.0, base * vat / 100.0 + charity)
        for factor, base in printed_formulas.values()
    )
    fees = _FEE_RE.search(text)
    if fees is None:
        raise ExtractorError("Aspiravi: yearly fixed fee row not found")
    mono_fee, bi_fee, night_fee = (to_float(fee) for fee in fees.groups())
    if bi_fee != mono_fee:
        # There is no field for a dual meter's own fee, and billing it the
        # single meter's would be a silent error.
        raise ExtractorError("Aspiravi: the dual meter carries its own yearly fee")
    single, day_rate, night_rate, excl_night = (
        rate / 100.0 + charity for rate in printed
    )
    return VariableRates(
        current=single,
        peak=day_rate,
        offpeak=night_rate,
        exclusive_night=excl_night,
        yearly_fixed_fee=mono_fee,
        yearly_fixed_fee_exclusive_night=None if night_fee == mono_fee else night_fee,
        formula=" · ".join(
            f"{label} ({factor} Belpex {base:+}) c€/kWh ex-VAT"
            for label, (factor, base) in printed_formulas.items()
        ),
        formula_factor=mono[0],
        formula_base=mono[1],
        formula_factor_peak=peak[0],
        formula_base_peak=peak[1],
        formula_factor_offpeak=offpeak[0],
        formula_base_offpeak=offpeak[1],
        formula_factor_exclusive_night=night[0],
        formula_base_exclusive_night=night[1],
        month_indexed=True,
    )


def _extract_injection(text: str) -> InjectionRates:
    """The feed-in formula on the delivery month's mean, VAT-exempt, with the
    rate printed at last month's mean as the fallback."""
    factor, base = _formula(_INJECTION_RE, text, "feed-in")
    printed = _INJECTION_PRINTED_RE.search(text)
    if printed is None:
        raise ExtractorError("Aspiravi: printed feed-in rate not found")
    return InjectionRates(
        current=to_float(printed.group(1)) / 100.0,
        factor=factor * 10.0,
        base=base / 100.0,
        formula=f"({factor} Belpex {base:+}) c€/kWh",
        month_indexed=True,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital-meter half of the network table.

    A row reads "Fluvius (Imewo)" then the data management fee, the digital
    meter's distribution and exclusive-night rates and its capacity rate, and
    the classic meter's four columns, of which only the prosumer rate is
    used.
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in FLUVIUS_CARD_LABELS.items():
        row = numeric_row(text, label.replace("Fluvius ", "Fluvius (", 1) + ")", 8)
        if row is None:
            continue
        data, single, excl_night, capacity, _, _, prosumer, _ = (
            to_float(value) for value in row
        )
        out[key] = DsoOverlay(
            distribution_single=single / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            transport=0.0,
            data_management_per_year=data,
            capacity_eur_per_kw_year=capacity,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


EXTRACTOR = SupplierExtractor(
    id=_SUPPLIER,
    label="Aspiravi Energy",
    sweep_cost_s=1.3,
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label="Eco Plus Flex",
            kind="variable",
            regions=frozenset({REGION_FLANDERS}),
            spot_indexed_injection=True,
            month_indexed_energy=True,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
