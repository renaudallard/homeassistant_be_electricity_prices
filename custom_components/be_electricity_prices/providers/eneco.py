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
from dataclasses import replace
from datetime import date

import aiohttp

from ..const import (
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._eneco_overlays import _AREAS, _NUM, _WS, _extract_dsos, _extract_taxes
from ._parse import (
    SIGN_CHARS,
    parse_sign,
    require_every_area,
    running_month_short_of_an_area,
    to_float,
)
from ._pdf import (
    NL_MONTHS,
    fetch_pdf_text,
    fetch_text,
    head_or_raise,
    is_transient_fetch_error,
    printed_vat_rate,
    vat_multiplier,
)
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    FixedRates,
    InjectionRates,
    VariableRates,
    vat_basis,
)
from ._settle import settled_injection
from ._validity import (
    archive_validity_check,
    parse_valid_until,
)
from .base import (
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)

_BASE_URL = "https://cdn.eneco.be/downloads/nl/general/tk"
_LISTING_URL = "https://eneco.be/nl/elektriciteit-gas/tariefkaarten"


# Contract id -> the POWER_<NAME> token Eneco uses in its filenames.
_CONTRACT_SLUGS: dict[str, str] = {
    "power_fix": "FIX",
    "power_fix_one": "FIX_ONE",
    "power_flex": "FLEX",
    "power_flex_one": "FLEX_ONE",
    "power_dynamic": "DYNAMIC",
}

# "laatst gekende waarde van Belpex-RLP-M (08/2026: €133,4366/MWh)": the month
# the printed Maandprijs was computed on and the value it settled at. Belpex-
# RLP-M is only known at month end, so the card printed in September carries
# August's realised value, which is what August is billed at.
_INDEX_FOOTNOTE_RE = re.compile(
    r"Belpex-RLP-M\s*\((\d{2})/(\d{4}):\s*€\s*([\d.,]+)\s*/MWh\)"
)
# The same footnote for the feed-in credit, on the injection page of every
# card but Dynamic: "laatst gekende waarde van Belpex-injectie (09/2026:
# €156,4069/MWh)". A different index from the one above (162,88 against
# 156,4069 for September), so it has a pattern of its own. The yearly
# estimate beside it, "Belpex-injectie (€122,1988/MWh)", names no month and
# does not match.
_INJECTION_FOOTNOTE_RE = re.compile(
    r"Belpex-injectie\s*\((\d{2})/(\d{4}):\s*€\s*([\d.,]+)\s*/MWh\)"
)
# Eneco prefixed every priced row label with a ">" bullet until the
# June 2025 issue ("10,67 10,67 10,67 10,67 > Maandprijs"); the July
# 2025 redesign dropped it. Accept it optionally so the archive stays
# readable: with the bullet rejected, fetch_for_month discarded the 27
# Flex issues from April 2023 to June 2025 outright and left
# injection.current empty on 26 Fix and 23 Dynamic issues the CDN
# still serves.
_BULLET = rf"(?:>{_WS}*)?"


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch and parse the Eneco tariff card for ``contract_id``."""
    if contract_id not in _CONTRACT_SLUGS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    listing = await _fetch_listing(session)
    url = _resolve_url(listing, contract_id)
    if url is None:
        raise ExtractorError(
            f"Eneco listing page did not advertise a PDF for {contract_id!r}"
        )
    text = await fetch_pdf_text(session, url)
    snap = parse_snapshot(text, contract_id, url, region)
    # Here and not in the reader: a past month's card keeps a short table.
    require_every_area("Eneco", snap, _AREAS)
    return snap


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
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
    found = await _archived_card(session, slug, contract_id, region, year_month)
    if found is None:
        return None
    snap, _text = found
    if running_month_short_of_an_area(snap.dsos, _AREAS, year_month):
        return None
    return await _settle_on_published_index(
        session, slug, contract_id, region, snap, year_month
    )


async def _archived_card(
    session: aiohttp.ClientSession,
    slug: str,
    contract_id: str,
    region: str,
    year_month: date,
) -> tuple[SupplierSnapshot, str] | None:
    """The validated card for ``year_month`` and its text, or ``None``."""
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
        try:
            await head_or_raise(session, url)
            text = await fetch_pdf_text(session, url)
        except ExtractorError as err:
            # A timeout, a reset or a 5xx says nothing about the volume:
            # raise, so the month cache retries the month instead of
            # caching it as absent once the five volumes are tried.
            if is_transient_fetch_error(str(err)):
                raise
            continue
        try:
            snap = parse_snapshot(text, contract_id, url, region)
        except ExtractorError:
            continue
        result = archive_validity_check(snap, text, year_month, month_names=NL_MONTHS)
        if result is not None:
            return result, text
    return None


def published_index(text: str) -> tuple[date, float] | None:
    """The Belpex-RLP-M value a card prints, as ``(month, EUR/kWh)``.

    Every Flex card footnotes the index its Maandprijs was computed on, which
    is the last month closed at publication: "de maandprijs berekend op basis
    van de laatst gekende waarde van Belpex-RLP-M (08/2026: €133,4366/MWh)".
    Eneco's own indexation page defines the parameter as the RLP-weighted mean
    of the delivery month, known only at month end, so this figure is what the
    named month is finally billed at. Four decimals, straight from Eneco.
    """
    return _footnoted_index(_INDEX_FOOTNOTE_RE, text)


def published_injection_index(text: str) -> tuple[date, float] | None:
    """The Belpex-injectie value a card prints, as ``(month, EUR/kWh)``.

    The feed-in twin of :func:`published_index`: the printed Maandprijs of the
    credit is its formula at the previous month's Belpex-injectie, and the
    card for the month after names what the month settled at.
    """
    return _footnoted_index(_INJECTION_FOOTNOTE_RE, text)


def _footnoted_index(pattern: re.Pattern[str], text: str) -> tuple[date, float] | None:
    match = pattern.search(text)
    if match is None:
        return None
    return (
        date(int(match.group(2)), int(match.group(1)), 1),
        to_float(match.group(3)) / 1000.0,
    )


async def _settle_on_published_index(
    session: aiohttp.ClientSession,
    slug: str,
    contract_id: str,
    region: str,
    snap: SupplierSnapshot,
    year_month: date,
) -> SupplierSnapshot:
    """Re-resolve an archived month on the indices Eneco published for it.

    The card for ``year_month`` prints its Maandprijs on the PREVIOUS month's
    Belpex-RLP-M, so billing that figure bills last month's index: measured on
    the 2026 cards, between 1,5 c/kWh over (April) and 2,5 c/kWh under (June)
    on a 13 to 17 c/kWh energy leg. The card for the month after prints what
    the month actually settled at, and that is the figure Eneco invoices, so
    the archived month takes it: ``current`` becomes the card's own formula at
    the realised index and ``index_realised`` records the value.

    The feed-in credit of every card but Dynamic works the same way on its own
    index, Belpex-injectie, which the next card footnotes too, so it is
    settled from the same fetch with the month's own coefficients. That holds
    for Fix and Fix One as well: their energy is fixed, their credit is not.
    Measured on the 2026 cards, the printed credit ran between 1,87 c/kWh
    over and 2,28 c/kWh under the settled one.

    While that next card is not out yet (the running month, or the first days
    after it closes) the printed estimate stands and the snapshot is flagged
    ``provisional`` so the monthly cache asks again after its TTL rather than
    filing the estimate as a closed month's fact. A next card whose footnote
    names some other month is left alone: nothing on it settles this one.
    """
    energy = snap.energy
    settles_energy = (
        isinstance(energy, VariableRates)
        and energy.formula_factor is not None
        and energy.formula_base is not None
    )
    injection = snap.injection
    settles_injection = injection is not None and injection.month_indexed
    if not settles_energy and not settles_injection:
        return snap
    following = date(
        year_month.year + (year_month.month == 12), year_month.month % 12 + 1, 1
    )
    found = await _archived_card(session, slug, contract_id, region, following)
    if found is None:
        return replace(snap, provisional=True)
    published = published_index(found[1])
    if (
        isinstance(energy, VariableRates)
        and energy.formula_factor is not None
        and energy.formula_base is not None
        and published is not None
        and published[0] == year_month
    ):
        index = published[1]
        snap = replace(
            snap,
            energy=replace(
                energy,
                current=energy.formula_factor * index + energy.formula_base,
                index_realised=index,
            ),
        )
    published = published_injection_index(found[1])
    if (
        injection is not None
        and injection.month_indexed
        and published is not None
        and published[0] == year_month
    ):
        snap = replace(snap, injection=settled_injection(injection, published[1]))
    return snap


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # Eneco's listing is region-agnostic.
) -> str | None:
    """Cheap freshness probe: the current PDF URL for ``contract_id``.

    Eneco's listing returns a per-request ETag (``Cache-Control: no-store``)
    so a header-only probe is unusable. The cheapest reliable probe is
    therefore "scrape the listing and return the resolved URL" - the
    issue number rotates monthly so the URL is the freshness signal.
    """
    if contract_id not in _CONTRACT_SLUGS:
        return None
    try:
        listing = await _fetch_listing(session)
    except ExtractorError:
        return None
    return _resolve_url(listing, contract_id)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the set of POWER_* product slugs from Eneco's listing page.

    Eneco's tariefkaarten page links every product as a direct PDF.
    Extract every ``BC_..._NL_ENECO_POWER_<NAME>.pdf`` and lower-case
    to match the registry's contract id (``power_fix``, etc.).

    The product token accepts underscores. A bare ``[A-Z]+`` matched the
    three single-word slugs and nothing else, so Zon & Wind Flex One
    (``POWER_FLEX_ONE``) stayed invisible here for as long as it was sold
    while every catalog check reported green. Discovery has to be looser
    than the resolver, never narrower.
    """
    try:
        listing = await _fetch_listing(session)
    except ExtractorError:
        return set()
    return {
        f"power_{name.lower()}"
        for name in re.findall(r"BC_[\d_]+_NL_ENECO_POWER_([A-Z_]+)\.pdf", listing)
    }


async def _fetch_listing(session: aiohttp.ClientSession) -> str:
    return await fetch_text(session, _LISTING_URL)


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


# "Alle prijzen en tarieven zijn inclusief 6% btw". The card's other rate,
# "(3) inclusief 21% btw", is written without "zijn" and reads the same, so
# the first match is taken, which is the residential footer on every card.
_VAT_RE = re.compile(r"inclusief\s*(\d+)\s*%\s*btw", re.IGNORECASE)
# "In het geval u kiest voor het tarief IMPACT ... zullen de “ECO”-uren
# overeenkomen met de nachturen": see FixedRates.day_night_under_impact.
_IMPACT_PAIR_RE = re.compile(r"ECO\W+uren\s+overeenkomen\s+met\s+de\s+nachturen")


def parse_snapshot(
    text: str, contract_id: str, source_url: str, region: str
) -> SupplierSnapshot:
    """Parse already-extracted PDF text. Exposed for unit tests.

    ``region`` reaches the tax overlay only: one card serves both regions and
    the Flemish energy fund is the single row on it a Walloon customer does
    not pay.
    """
    if contract_id not in _CONTRACT_SLUGS:
        raise ExtractorError(f"unknown Eneco contract {contract_id!r}")
    energy = _extract_energy(text, contract_id)
    if _IMPACT_PAIR_RE.search(text) and isinstance(energy, (FixedRates, VariableRates)):
        energy = replace(energy, day_night_under_impact=True)
    # The dynamic formula prints its own multiplier ("x 1,06") and is grossed
    # by that, so only the variable one is grossed by the footer's rate.
    card_vat, assumed_vat = vat_basis(
        printed_vat_rate(text, _VAT_RE),
        energy,
        grossed=not isinstance(energy, DynamicRates),
    )
    return SupplierSnapshot(
        supplier="eneco",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text),
        taxes=replace(
            _extract_taxes(text, region),
            card_vat_rate=card_vat,
            assumed_vat_rate=assumed_vat,
        ),
        source_url=source_url,
        publication_label=_extract_publication_month(text),
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text, contract_id),
    )


def _extract_publication_month(text: str) -> str:
    match = re.search(r"Tariefkaart\s+([a-zA-Z]+\s+\d{4})", text)
    return match.group(1) if match else ""


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    if contract_id in ("power_fix", "power_fix_one"):
        # Fix One is the one-year twin of Fix on the same card layout.
        return _extract_fixed(text)
    if contract_id in ("power_flex", "power_flex_one"):
        # Flex One is the same variable card with its own base and fee
        # (1,462 against Flex's 3,001 on the September 2026 cards), so it
        # parses on the same block.
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
    # The yearly fixed fee (VASTE VERGOEDING, €/jaar) prints as a
    # standalone number on the line directly above the energy rate row
    # that ends in "Geschatte jaarprijs". Anchor on those two stable
    # tokens rather than counting a fixed number of header lines between
    # them: a single extra header line on a future card used to break
    # the rigid four-newline skip and take Power Flex offline.
    yearly_fee_match = re.search(
        r"\(€/jaar\)[\s\S]*?\n\s*" + _NUM + r"\s*\n[^\n]*Geschatte\s+jaarprijs",
        text,
        re.S,
    )
    # Fix and Flex reprint the same "<figures> > Maandprijs" row on the
    # injection side, and the optional bullet lets this anchor reach it: on
    # the June 2021 Flex card the only match in the whole document sits on
    # the injection page. So search the consumption side alone, cutting at
    # whichever injection heading the card prints, "AFNAME EN INJECTIE" on
    # the modern layout and the btw exemption line on the 2021 two-page one.
    # Measured over the 143 archived cards: the cut moves no match and drops
    # only that one. Five 2020/2021 cards print neither heading, and none of
    # them carries an injection section at all.
    injection = re.search(
        r"AFNAME\s+EN\s+INJECTIE|Injectietarieven\s+zijn\s+vrijgesteld", text
    )
    consumption = text[: injection.start()] if injection else text
    monthly_match = re.search(
        rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}\s+{_BULLET}Maandprijs", consumption
    )
    # Accept any sign character between the BELPEX factor and the base
    # so a future card flipping to '-' or to a Unicode minus doesn't
    # silently drop the formula display string, and an integer base as
    # the dynamic and injection formulas do: the card already prints
    # "0,077 X BELPEX -3", and a missed formula here parses cleanly
    # while dropping the month indexing.
    formula_match = re.search(
        rf"\((0,\d+)\s*X\s*BELPEX[\w\-]+\s*([{SIGN_CHARS}])\s*(\d+(?:,\d+)?)\)",
        text,
    )
    if not yearly_fee_match or not monthly_match:
        raise ExtractorError("could not parse Eneco variable energy block")
    # Numeric coefficients, VAT-baked to EUR/kWh like the dynamic path
    # (snapshot vat_rate is 0). The variable formula does not print its VAT
    # multiplier, so read it from the card header ("inclusief 6% btw").
    #
    # The card is month-indexed on Belpex-RLP-M, the RLP-weighted mean of the
    # DELIVERY month's Belpex quotations, known only at month end; the printed
    # Maandprijs is the formula at the PREVIOUS month's value and the footnote
    # says so. So the coefficients are what the contract bills: the coordinator
    # resolves them against the running month's RLP-weighted mean (Synergrid's
    # profile, reproducing Eneco's published values to the cent), and an
    # archived month is settled on the value the next card prints.
    f_factor: float | None = None
    f_base: float | None = None
    if formula_match:
        vat_mult = vat_multiplier(text, _VAT_RE)
        f_factor = to_float(formula_match.group(1)) * vat_mult * 10.0
        f_base = (
            parse_sign(formula_match.group(2))
            * to_float(formula_match.group(3))
            * vat_mult
            / 100.0
        )
    return VariableRates(
        current=to_float(monthly_match.group(1)) / 100.0,
        yearly_fixed_fee=to_float(yearly_fee_match.group(1)),
        formula=formula_match.group(0) if formula_match else None,
        formula_factor=f_factor,
        formula_base=f_base,
        month_indexed=f_factor is not None,
        rlp_indexed=f_factor is not None,
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


def _extract_injection(text: str, contract_id: str) -> InjectionRates | None:
    """Parse the injection block of an Eneco tariff card.

    Layout (every contract):

      AFNAME EN INJECTIE [/ VALORISATIE]
       ... [optional 'Zie afname' recap block on Power Dynamic] ...
       INJECTIE
        <c/kWh value(s)> Geschatte jaarprijs
        [<c/kWh value(s)> Maandprijs]                    (Fix/Flex only)
        <factor> X BELPEX[-H] [+-] <base> Tariefformule

    Power Fix and Flex use Belpex monthly; Power Dynamic uses Belpex-H
    (hourly). Injection is VAT-exempt for residential, so values are
    EUR/kWh = c/kWh / 100.

    The July 2026 cards dropped the "/ VALORISATIE" suffix from the
    section heading, so anchor on the stable "AFNAME EN INJECTIE" prefix
    (present on both the old and the new cards) instead.
    """
    anchor = re.search(r"AFNAME\s+EN\s+INJECTIE", text)
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
    maand = re.search(rf"((?:{_NUM}\s+){{1,4}}){_WS}*{_BULLET}Maandprijs", section)
    yearly = re.search(
        rf"((?:{_NUM}\s+){{1,4}}){_WS}*{_BULLET}Geschatte jaarprijs", section
    )
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
    # A card that prints a numeric injection row we could not read has to
    # fail the card instead of storing a snapshot with an empty leg. The
    # '>' bullet nulled current on 49 archived issues (26 Fix, 23
    # Dynamic) that archive_validity_check waved through, and a keyless
    # entry then credits nothing for those months. Live, this raises and
    # surfaces a Repairs card; on the archive path fetch_for_month
    # swallows it and the month falls back to the current card, which is
    # still silent but at least credits a figure the supplier published.
    # The "Zie afname" recap rows carry the same two labels with no
    # figure, so key the guard on a row that starts with a number, and
    # match whatever glyph sits between it and the label rather than
    # reusing _BULLET, so the next change of that kind is caught too.
    if current_cents is None and re.search(
        rf"^{_WS}*{_NUM}[^\n]*(?:Maandprijs|Geschatte jaarprijs)", section, re.M
    ):
        raise ExtractorError("Eneco injection row is printed but unreadable")

    factor: float | None = None
    base: float | None = None
    # Power Dynamic indexes injection on the HOURLY spot (Belpex-H); Fix and
    # Flex settle on the monthly Belpex-injectie. Both surface the same
    # coefficients, and ``month_indexed`` is what keeps them apart: it makes
    # the engine resolve them against the delivery month's mean and refuses
    # them to the per-hour path outright. Without that flag these coefficients
    # would be priced at the current slot's spot the moment a card stopped
    # printing its Maandprijs, which is the 0.6.7 mis-credit.
    month_indexed = contract_id != "power_dynamic"
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
        month_indexed=month_indexed and factor is not None,
        formula=formula.group(0) if formula else None,
    )


_ENECO_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    sweep_cost_s=0.7,
    settles_on_next_card=True,
    id="eneco",
    label="Eneco",
    contracts=(
        Contract(
            id="power_fix",
            label="Eneco Zon & Wind Vast",
            kind="fixed",
            regions=_ENECO_REGIONS,
            # Injection indexes on the monthly Belpex-injectie, which needs
            # spots the fixed energy leg never fetches.
            spot_indexed_injection=True,
        ),
        Contract(
            # A one-year fixed card that appeared on the October 2026
            # listing ("een contract van bepaalde duur met een looptijd van 1
            # jaar"), priced apart from Vast: 21,69 against 22,10 c/kWh mono
            # on the same 65,00 EUR fee. Same layout and the same monthly
            # Belpex-injectie formula, hence the same flag.
            id="power_fix_one",
            label="Eneco Zon & Wind Fix One",
            kind="fixed",
            regions=_ENECO_REGIONS,
            spot_indexed_injection=True,
        ),
        Contract(
            id="power_flex",
            label="Eneco Zon & Wind Flex",
            kind="variable",
            regions=_ENECO_REGIONS,
            spot_indexed_injection=True,
            # Belpex-RLP-M is the delivery month's index, so the key is
            # offered on every solar regime, compensation included.
            month_indexed_energy=True,
        ),
        Contract(
            # A second variable card, not a relabel: its own text sells a
            # "contract van 1 jaar" against Flex's "contract van onbepaalde
            # duur". Same layout and the same 0,102 factor; the base and the
            # fee are its own and move per card. On the September 2026 cards
            # the base was 1,462 against 3,001 on the same 65,00 EUR fee, so
            # it priced 15,98 c/kWh against Flex's 17,61 and a Flex One
            # household forced onto Flex overpaid 57,05 EUR a year at
            # 3500 kWh. Injection is identical, hence the same flag.
            id="power_flex_one",
            label="Eneco Zon & Wind Flex One",
            kind="variable",
            regions=_ENECO_REGIONS,
            spot_indexed_injection=True,
            month_indexed_energy=True,
        ),
        Contract(
            # Sold in both regions since the October 2026 card: "enkel
            # beschikbaar in Vlaanderen en Wallonie, en enkel indien u
            # beschikt over een digitale meter en meetregime SMR3". Until
            # September it read "beschikbaar in Vlaanderen" and the Walloon
            # rows it printed were reference only. Those rows carry no Tarif
            # Impact columns, so a Walloon entry on the Impact mode is billed
            # the bi-horaire network rates and told so by the
            # impact_rates_missing repair.
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
