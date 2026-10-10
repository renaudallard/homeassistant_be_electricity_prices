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

"""The social tariff, for protected customers.

A household granted protected status pays the social tariff whoever supplies
it: one price per meter register for the whole country, set each quarter by
the CREG under the ministerial decree of 30 March 2007. The CREG publishes it
as one PDF per quarter (E-TSS-FR-<year>-Q<n>.pdf), with the energy and the
network components before and after VAT. The network component replaces the
distribution operator's tariffs altogether, so there is no standing charge,
no capacity term, no data management fee and no prosumer tariff (the CWaPE
says so for the last). On top of it the household pays only the special
excise at the protected rate, which the law sets (``excise_law``), and in
Wallonia the connection fee; it is exempt from the energy contribution and
the Flemish energy fund.

What it is paid for feed-in is not regulated: each supplier sets its own and
most publish nothing. Engie and Luminus print it on their social card, and
Fluvius, as the social supplier in Flanders, pays none. So the contract is
the household's supplier, and only the first two carry a feed-in credit.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import date

import aiohttp
from homeassistant.util import dt as dt_util

from ..const import (
    BRUSSELS_DSO_KEYS,
    FLUVIUS_KEYS,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SUPPLIER_SOCIAL,
    WALLONIA_DSO_KEYS,
)
from ..excise_law import protected_excise
from ._parse import to_float
from ._pdf import fetch_pdf_text, fetch_text, is_transient_fetch_error
from ._rates import Contract, InjectionRates, VariableRates
from ._validity import end_of_month
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    with_vat_basis,
)

CONTRACT_ENGIE = "social_engie"
CONTRACT_LUMINUS = "social_luminus"
CONTRACT_OTHER = "social_other"

_CREG_URL = (
    "https://www.creg.be/sites/default/files/assets/Tarifs/Social/"
    "E-TSS-FR-{year}-Q{quarter}.pdf"
)
# The first quarter the CREG still publishes, which is as far back as a month
# can be billed on its own card.
FIRST_QUARTER = date(2022, 10, 1)

_DSO_KEYS = {
    REGION_FLANDERS: FLUVIUS_KEYS,
    REGION_WALLONIA: WALLONIA_DSO_KEYS,
    REGION_BRUSSELS: BRUSSELS_DSO_KEYS,
}

# ---- the CREG card -----------------------------------------------------------

_NUM = r"(\d+,\d+)"
# Every row of a register's table, ex-VAT then VAT-inclusive, in c/kWh. The
# unit is spelled "c€/kWh" on the Q4 2022 card and "€cent/kWh" since, a
# footnote mark of one or two asterisks may follow it, and the network is one
# "réseau" row since 2026 and a distribution plus a transport row before.
_ROW_RE = re.compile(
    r"(Composante (?:énergie|réseau|distribution|transport)|Total)"
    rf"\s*\((?:c€|€cent)\s*/kWh\)\**\s+{_NUM}\s+{_NUM}"
)
_VAT_RE = re.compile(r"TVA\s+(\d+)\s*%\s+comprise")
_QUARTER_RE = re.compile(r"(\d)(?:er|e|ème)\s+trimestre\s+(\d{4})")
# The rounding the card states its figures at, in c/kWh, twice: a component
# sum and a VAT gross-up may each be a unit off in the last place.
_TOLERANCE = 0.0021


@dataclass(frozen=True)
class _Register:
    total: float  # c/kWh including VAT
    network: float  # c/kWh including VAT


@dataclass(frozen=True)
class CregCard:
    """One quarter's social prices, VAT-inclusive, in c/kWh."""

    quarter: date
    vat: float
    mono: _Register
    day: _Register
    night: _Register
    excl_night: _Register


def _registers(text: str, vat: float) -> list[_Register]:
    """The four registers' tables in the order the card prints them: single,
    day, night, exclusive night. Each opens on its energy row and closes on
    its total, and its rows have to add up to that total on both sides of
    VAT, and each VAT-inclusive figure to its ex-VAT one at the card's rate,
    or the card is not read."""
    out: list[_Register] = []
    rows: list[tuple[str, float, float]] = []
    for label, ex, inc in _ROW_RE.findall(text):
        if label == "Composante énergie":
            rows = []
        rows.append((label, to_float(ex), to_float(inc)))
        if label != "Total":
            continue
        *parts, (_, total_ex, total_inc) = rows
        if not parts or parts[0][0] != "Composante énergie":
            raise ExtractorError("CREG social tariff: a total with no energy row")
        if abs(sum(p[1] for p in parts) - total_ex) > _TOLERANCE * len(parts):
            raise ExtractorError("CREG social tariff: components do not add up")
        if abs(sum(p[2] for p in parts) - total_inc) > _TOLERANCE * len(parts):
            raise ExtractorError("CREG social tariff: components do not add up")
        for _, ex_, inc_ in rows:
            if abs(ex_ * (1.0 + vat) - inc_) > _TOLERANCE:
                raise ExtractorError("CREG social tariff: VAT does not add up")
        energy_inc = parts[0][2]
        out.append(_Register(total=total_inc, network=total_inc - energy_inc))
        rows = []
    return out


def parse_creg(text: str) -> CregCard:
    """Read one quarter's CREG card. Raises on anything else."""
    vat_match = _VAT_RE.search(text)
    quarter = _QUARTER_RE.search(text)
    if vat_match is None or quarter is None:
        raise ExtractorError("CREG social tariff: not a quarter's card")
    number, year = int(quarter.group(1)), int(quarter.group(2))
    if not 1 <= number <= 4:
        raise ExtractorError(f"CREG social tariff: quarter {number}")
    vat = int(vat_match.group(1)) / 100.0
    registers = _registers(text, vat)
    if len(registers) != 4:
        raise ExtractorError(
            f"CREG social tariff: {len(registers)} registers where 4 are printed"
        )
    mono, day, night, excl_night = registers
    return CregCard(
        quarter=date(year, 3 * number - 2, 1),
        vat=vat,
        mono=mono,
        day=day,
        night=night,
        excl_night=excl_night,
    )


def _quarter_of(month: date) -> date:
    return date(month.year, 3 * ((month.month - 1) // 3) + 1, 1)


def _creg_url(quarter: date) -> str:
    return _CREG_URL.format(year=quarter.year, quarter=(quarter.month + 2) // 3)


async def _creg_card(session: aiohttp.ClientSession, month: date) -> CregCard:
    quarter = _quarter_of(month)
    card = parse_creg(await fetch_pdf_text(session, _creg_url(quarter)))
    if card.quarter != quarter:
        raise ExtractorError(
            f"CREG social tariff: the card for {quarter} prices {card.quarter}"
        )
    return card


# ---- the suppliers' social cards ---------------------------------------------

# Engie's document service serves the social card like any product card, one
# per region, and the month offset counts back from the current one.
_ENGIE_URL = (
    "https://www.engie.be/api/engie/be/ms/pricing/v1/public/pricesAndConditionsPDF"
    "?document=E_SOCIAL_R_GREY_C_F_00_{code}_F&monthOffset={offset}"
    "&segment=R&language=F"
)
_ENGIE_REGION = {REGION_FLANDERS: "V", REGION_WALLONIA: "W", REGION_BRUSSELS: "B"}
# The footnote mark follows the label, or the line under it on the 2023 cards.
_ENGIE_CONSUMPTION_RE = re.compile(
    rf"Consommation\s*\(\d+\)\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}"
)
_ENGIE_INJECTION_RE = re.compile(rf"Injection\s*\(\d+\)\s+{_NUM}\s+{_NUM}\s+{_NUM}")
# "0,0500 + (0,0632 x EPEXDAM)" since March 2024, "0,0500 + 0,0632 x EPEX DAM"
# on the cards before it, without the parentheses and with a space.
_ENGIE_FORMULA = r"=\s*(\d+,\d+)\s*\+\s*\(?(\d+,\d+)\s*x\s*EPEX\s?DAM\)?"
_ENGIE_FORMULAS = (
    re.compile(rf"Normal\s*{_ENGIE_FORMULA}"),
    re.compile(rf"heures pleines\s*{_ENGIE_FORMULA}"),
    re.compile(rf"heures creuses\s*{_ENGIE_FORMULA}"),
)
_ENGIE_FEE_RE = re.compile(rf"Redevance raccordement\s*\(\d+\)\s+{_NUM}")

# Luminus serves its social card through the price-list archive, the current
# month included, by product id for a month and a region.
_LUMINUS_PRODUCTS_URL = (
    "https://www.luminus.be/api/pricelist/products?language=FR"
    "&customerSegment=Residential&energyType=Electricity&region={tab}"
    "&signing={month}"
)
_LUMINUS_PDF_URL = (
    "https://www.luminus.be/api/pricelist/pdf?language=FR&productId={product}"
    "&date={month}&region={tab}&inline=true"
)
_LUMINUS_TAB = {
    REGION_FLANDERS: "Flanders",
    REGION_WALLONIA: "Wallonia",
    REGION_BRUSSELS: "Brussels",
}
_LUMINUS_PRODUCT_RE = re.compile(r"tarif social", re.IGNORECASE)
_LUMINUS_CONSUMPTION_RE = re.compile(
    rf"Énergie \(c€/kWh\)\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}"
)
# "Tarif de l'énergie injectée" since 2026, "Compensation pour l'énergie
# injectée" before. The card a change of rate falls in prints two rows, the
# first "jusqu'au" the end of its own month (June 2025), which is the one.
_LUMINUS_INJECTION_RE = re.compile(
    r"injectée \(c€/\s*kWh\)\s*1?(?:\s*-\s*jusqu'au [\d/]+)?"
    rf"\s+{_NUM}\s+{_NUM}\s+{_NUM}"
)
# Any Walloon operator's row: connection fee, energy fund (none), excise
# (none before April 2023).
_LUMINUS_FEE_RE = re.compile(rf"AIEG\s+{_NUM}\s+-\s+(?:-|\d+,\d+)")


def _matches_creg(printed: tuple[float, ...], card: CregCard, places: int) -> bool:
    """Whether a supplier's social card prints this quarter's CREG prices,
    rounded to ``places`` decimals, which is what dates it."""
    creg = (card.mono.total, card.day.total, card.night.total, card.excl_night.total)
    step = 10.0**-places
    return all(abs(a - b) <= step for a, b in zip(printed, creg, strict=True))


def parse_engie(text: str, card: CregCard) -> tuple[InjectionRates, float | None]:
    """Engie's social feed-in and the Walloon connection fee it prints.

    The card prints its feed-in for a single register, the day and the night
    one, last month's EPEXDAM put through a formula on the delivery month's.
    The index itself is left out of this card, so the formulas are bound by
    arithmetic: all three printed rates have to imply one index. A card with
    no formula at all is credited as printed.
    """
    consumption = _ENGIE_CONSUMPTION_RE.search(text)
    if consumption is None or not _matches_creg(
        tuple(to_float(v) for v in consumption.groups()), card, 3
    ):
        raise ExtractorError("Engie social card: not this quarter's CREG prices")
    injection = _ENGIE_INJECTION_RE.search(text)
    if injection is None:
        raise ExtractorError("Engie social card: no feed-in row")
    printed = [to_float(v) for v in injection.groups()]
    fee = _ENGIE_FEE_RE.search(text)
    rates = InjectionRates(
        current=printed[0] / 100.0,
        peak=printed[1] / 100.0,
        offpeak=printed[2] / 100.0,
        bi_hourly=True,
    )
    matches = [pattern.search(text) for pattern in _ENGIE_FORMULAS]
    found = [match for match in matches if match is not None]
    if found:
        if len(found) != len(matches):
            raise ExtractorError("Engie social card: a feed-in formula is missing")
        formulas = [(to_float(m.group(1)), to_float(m.group(2))) for m in found]
        implied = [(p - b) / f for p, (b, f) in zip(printed, formulas, strict=True)]
        if max(implied) - min(implied) > 0.5:
            raise ExtractorError("Engie social card: the formulas do not fit the rates")
        # c/kWh per EUR/MWh onto a EUR/kWh spot: x10 the factor, /100 the
        # base. Injection is exempt from VAT. One pair per register, so a
        # two-register meter is credited on the delivery month's mean too.
        (base, factor), (base_day, factor_day), (base_night, factor_night) = formulas
        rates = replace(
            rates,
            factor=factor * 10.0,
            base=base / 100.0,
            factor_peak=factor_day * 10.0,
            base_peak=base_day / 100.0,
            factor_offpeak=factor_night * 10.0,
            base_offpeak=base_night / 100.0,
            formula=f"{base:.4f} + ({factor:.4f} x EPEXDAM) c€/kWh",
            month_indexed=True,
        )
    return rates, None if fee is None else to_float(fee.group(1)) / 100.0


def parse_luminus(text: str, card: CregCard) -> tuple[InjectionRates, float | None]:
    """Luminus's social feed-in and the Walloon connection fee it prints.

    The feed-in is indexed on the delivery quarter's Belpex and printed on
    the previous quarter's, which is the one figure the card states for a
    quarter whose own index is not known until it ends.
    """
    consumption = _LUMINUS_CONSUMPTION_RE.search(text)
    if consumption is None or not _matches_creg(
        tuple(to_float(v) for v in consumption.groups()), card, 2
    ):
        raise ExtractorError("Luminus social card: not this quarter's CREG prices")
    injection = _LUMINUS_INJECTION_RE.search(text)
    if injection is None:
        raise ExtractorError("Luminus social card: no feed-in row")
    mono, day, night = (to_float(v) / 100.0 for v in injection.groups())
    fee = _LUMINUS_FEE_RE.search(text)
    return (
        InjectionRates(current=mono, peak=day, offpeak=night, bi_hourly=True),
        None if fee is None else to_float(fee.group(1)) / 100.0,
    )


def _engie_offset(month: date) -> int:
    today = dt_util.now().date()
    return (today.year - month.year) * 12 + (today.month - month.month)


async def _engie_card(
    session: aiohttp.ClientSession, region: str, month: date
) -> tuple[str, str]:
    offset = _engie_offset(month)
    if offset < 0:
        raise ExtractorError("Engie social card: a month not published yet")
    url = _ENGIE_URL.format(code=_ENGIE_REGION[region], offset=offset)
    return await fetch_pdf_text(session, url), url


async def _luminus_card(
    session: aiohttp.ClientSession, region: str, month: date
) -> tuple[str, str]:
    label = f"{month.year:04d}-{month.month:02d}"
    tab = _LUMINUS_TAB[region]
    body = await fetch_text(
        session, _LUMINUS_PRODUCTS_URL.format(tab=tab, month=label), timeout=15
    )
    try:
        products = json.loads(body)
    except json.JSONDecodeError as err:
        raise ExtractorError(f"Luminus social card: {err}") from err
    if not isinstance(products, list):
        raise ExtractorError("Luminus social card: expected a product list")
    product = next(
        (
            row.get("ProductId")
            for row in products
            if isinstance(row, dict)
            and _LUMINUS_PRODUCT_RE.search(str(row.get("Product", "")))
        ),
        None,
    )
    if not isinstance(product, str) or not product:
        raise ExtractorError(f"Luminus social card: none listed for {label}")
    url = _LUMINUS_PDF_URL.format(product=product, month=label, tab=tab)
    return await fetch_pdf_text(session, url), url


# ---- the snapshot -------------------------------------------------------------


def build_snapshot(
    contract_id: str,
    region: str,
    card: CregCard,
    month: date,
    *,
    injection: InjectionRates | None,
    connection_fee: float | None,
    source_url: str,
) -> SupplierSnapshot:
    """The snapshot a protected household in ``region`` is billed on for
    ``month``, from the quarter's CREG card.

    A month the law has not been read for yet carries no excise and says so
    (``protected_excise_unread``): billing it without the excise would
    under-bill every kWh, and billing the old rate would over-bill, so the
    resolver fills it once the law is held and the coordinator publishes
    nothing until then.
    """
    excise = protected_excise(month)
    vat = card.vat

    def _eur(c_per_kwh: float) -> float:
        return c_per_kwh / 100.0

    network = _eur(card.mono.network)
    day_net, night_net = _eur(card.day.network), _eur(card.night.network)
    split = abs(day_net - network) > 1e-9 or abs(night_net - network) > 1e-9
    overlay = DsoOverlay(
        distribution_single=network,
        distribution_peak=day_net if split else None,
        distribution_offpeak=night_net if split else None,
        distribution_exclusive_night=_eur(card.excl_night.network),
        transport=0.0,
        # The network component is the same in every Impact band, and the
        # CWaPE exempts a protected customer from the prosumer tariff, so both
        # are stated rather than left missing.
        distribution_pic=network,
        distribution_medium=network,
        distribution_eco=network,
        prosumer_eur_per_kva_year=0.0 if region == REGION_WALLONIA else None,
    )
    walloon = region == REGION_WALLONIA
    taxes = TaxOverlay(
        # The law's rate excludes VAT; this card's prices include it.
        federal_excise=0.0 if excise is None else excise * (1.0 + vat),
        protected_excise_unread=excise is None,
        energy_contribution=0.0,
        region_connection_fee=(connection_fee or 0.0) if walloon else 0.0,
        region_connection_fee_unavailable=walloon and connection_fee is None,
    )
    number = (card.quarter.month + 2) // 3
    return with_vat_basis(
        SupplierSnapshot(
            supplier=SUPPLIER_SOCIAL,
            contract=contract_id,
            energy=VariableRates(
                current=_eur(card.mono.total) - network,
                peak=_eur(card.day.total) - day_net,
                offpeak=_eur(card.night.total) - night_net,
                exclusive_night=_eur(card.excl_night.total)
                - _eur(card.excl_night.network),
            ),
            dsos=dict.fromkeys(_DSO_KEYS[region], overlay),
            taxes=taxes,
            injection=injection,
            source_url=source_url,
            publication_label=f"Q{number} {card.quarter.year}",
            valid_until=end_of_month(card.quarter.year, card.quarter.month + 2),
        ),
        vat,
    )


async def _for_month(
    session: aiohttp.ClientSession, contract_id: str, region: str, month: date
) -> SupplierSnapshot:
    if contract_id not in _CONTRACT_IDS:
        raise ExtractorError(f"unknown social tariff contract {contract_id!r}")
    if region not in _DSO_KEYS:
        raise ExtractorError(f"social tariff: unknown region {region!r}")
    card = await _creg_card(session, month)
    injection: InjectionRates | None = None
    fee: float | None = None
    if contract_id == CONTRACT_ENGIE:
        text, _ = await _engie_card(session, region, month)
        injection, fee = parse_engie(text, card)
    elif contract_id == CONTRACT_LUMINUS or region == REGION_WALLONIA:
        # Luminus prints the Walloon connection fee too, which is the same for
        # every supplier, so a household on another supplier reads it there.
        try:
            text, _ = await _luminus_card(session, region, month)
            luminus_injection, fee = parse_luminus(text, card)
        except ExtractorError as err:
            # A card that cannot be read leaves the fee disclosed as missing,
            # but a network failure says nothing about the card: raised, so
            # the month is asked again rather than filed without the fee.
            if contract_id == CONTRACT_LUMINUS or is_transient_fetch_error(str(err)):
                raise
            luminus_injection = None
        if contract_id == CONTRACT_LUMINUS:
            injection = luminus_injection
    return build_snapshot(
        contract_id,
        region,
        card,
        month,
        injection=injection,
        connection_fee=fee,
        source_url=_creg_url(card.quarter),
    )


async def fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """This month's social tariff."""
    return await _for_month(session, contract_id, region, dt_util.now().date())


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """A past month's social tariff, from its own quarter's CREG card, or
    ``None`` before the first quarter the CREG still publishes."""
    first = year_month.replace(day=1)
    if first < FIRST_QUARTER or first > dt_util.now().date():
        return None
    try:
        return await _for_month(session, contract_id, region, first)
    except ExtractorError as err:
        # A timeout, a reset or a 5xx says nothing about the month: raise, so
        # the month cache retries it instead of caching it as absent.
        if is_transient_fetch_error(str(err)):
            raise
        return None


_CONTRACT_IDS = frozenset({CONTRACT_ENGIE, CONTRACT_LUMINUS, CONTRACT_OTHER})

EXTRACTOR = SupplierExtractor(
    id=SUPPLIER_SOCIAL,
    label="Social tariff (CREG, protected customers only)",
    # The CREG card and Engie's social card, the slower of the two supplier
    # cards, measured on a Raspberry Pi 4.
    sweep_cost_s=0.7,
    contracts=(
        Contract(
            id=CONTRACT_ENGIE,
            label="Supplied by Engie",
            kind="variable",
            spot_indexed_injection=True,
        ),
        Contract(id=CONTRACT_LUMINUS, label="Supplied by Luminus", kind="variable"),
        Contract(
            id=CONTRACT_OTHER,
            label="Supplied by another supplier or the network operator",
            kind="variable",
        ),
    ),
    fetch=fetch,
    fetch_for_month=fetch_for_month,
)
