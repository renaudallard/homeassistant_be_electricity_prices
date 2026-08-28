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

"""Energy Knights tariff extractors.

Energy Knights sells residential electricity in Flanders only and publishes
one tariff card per product per month as a text-layer PDF. Every card is
served from a stable, product-keyed URL, so there is no listing to resolve
and no versioned blob to chase:

    /website/getCurrentTariffchart/<slug>/nl

The ``/website/`` prefix is part of the path; without it the site answers 404.
An unknown slug redirects to the marketing homepage with HTTP 302, which
aiohttp follows, so a typo yields 480 KB of HTML rather than an error status.
_fetch_validated_pdf_bytes catches that on the magic bytes and raises, which
is why nothing here inspects the payload itself.

Six of the eight published products are modelled. Agilior Online prices each
quarter hour off Belpex_15 and Agilis Online each hour off Belpex_h; they are
the same card with a different index token, and the only thing separating them
in the snapshot is DynamicRates.quarter_hourly. Essentia Online is indexed
monthly, on Belpex-RLP for offtake and the solar-weighted Belpex-SPP for the
credit, and it is the contractual fallback the other two bill on when Fluvius
cannot deliver quarter values.

Each also has a "green" twin, which is the same card plus one flat
"Groene stroom" row applying to every kWh, so the six share every parser here.
That row is not a constant: it printed 0,42 c€/kWh in September 2025 against
0,32 everywhere else.

Optima Online and its twin are the two products left out. Optima carries a
"Service fee onbalans handelen" whose amount depends on which home energy
management system the customer runs: 10,00 EUR/jaar in December 2025, printed
without a value in August 2026, and no field here can hold it. Both stay in
DISCOVER_IDS so discover() flags only a genuinely new product.

Two things on these cards are easy to get wrong.

The header says "Alle prijzen en tarieven zijn inclusief 6% btw", but that is
not true line by line. The formula column is labelled "(*)Tariefformule in
EUR/MWh excl BTW", and the injection row carries footnote (1), "Bedrag niet
onderworpen aan BTW". The card's own arithmetic settles it: the August 2026
Agilior card prints 14,79 c€/kWh against a VREG index of 127,50 EUR/MWh,
which is (127,50 + 12) / 10 x 1,06, while its injection prints 5,85 against
70,54, which is (70,54 - 12) / 10 with no VAT at all. Grossing the credit
would overstate every solar user's compensation by 6%.

The coefficients drift monthly and none of them may be pinned. Agilior ran
"x 1,07 + 7" on a 15,00 EUR standing charge in May 2026 and "x 1 + 12" on
25,00 in August, and its injection went "x 0,86 - 5" to "x 0,94 - 11" to
"x 1 - 12" inside the same year.

The printed c€/kWh figures are indicatives computed from the VREG weighted
average annual price, not the rate that gets billed, and Energy Knights
publishes both series at /priceparameters. Over the 26 months that table
covers the two sit at least 10% apart in 19 of them, ranging -24,7% to +56,2%,
because the VREG series barely moves while the settled index swings by half.
Nothing here stores one for offtake: every contract modelled settles against
the spot, whether per slot or per month, so the coefficients are the contract
and the printed column is an illustration. That is also why Essentia is
spot_monthly rather than variable - the kind is what makes the ENTSO-E key
mandatory, and there is no honest way to price this card without one.

Region: Flanders only (all 8 Fluvius sub-areas).
"""

from __future__ import annotations

import re
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
    NUM_NO_THOUSANDS,
    archive_validity_check,
    SIGN_CHARS,
    fetch_pdf_text_layout,
    fetch_text,
    flanders_tax_overlay,
    head_freshness_key,
    parse_sign,
    parse_valid_until,
    to_float,
    vat_multiplier,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
)

_SITE_BASE = "https://www.energyknights.be"
_CARD_URL = _SITE_BASE + "/website/getCurrentTariffchart/{slug}/{lang}"
_ARCHIVE_URL = _SITE_BASE + "/website/getHistoricalTariffchart/{month}/{slug}/{lang}"
_LISTING_URL = f"{_SITE_BASE}/tariffcharts"
_FLANDERS_ONLY = frozenset({REGION_FLANDERS})

# Cards are published in nl, fr and en with identical numbers. The Dutch one
# is pinned because it is the only one that keeps each DSO row on a single
# line: the French card wraps the long area names ("Fluvius (Halle-" then the
# figures then "Vilvoorde)"), which splits the label away from its columns.
_LANG = "nl"

# Energy Knights renamed all four products at the turn of 2026. From this month
# the current slugs serve the archive; before it, the predecessors do. The
# handover is clean: no month resolves under both.
_RENAMED_FROM = date(2026, 1, 1)

# No month before this can be billed. The 2024 cards print the PRE-MERGER ten
# Fluvius areas (Gaselwest, Iverlek, Pbe, Sibelgas) and omit Halle-Vilvoorde
# and Zenne-Dijle, two live DSO keys, and 2024-06 prints EUR/kWh values under a
# c€/kWh column header. Their ENERGY block would parse perfectly well, so the
# refusal has to be explicit: an old card whose overlays land wrong bills a
# whole month at the wrong network cost, which is worse than not billing it.
_ARCHIVE_HORIZON = date(2025, 1, 1)


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str
    # The product name as the card's own intro line prints it, which is the
    # only thing in the document identifying which product it is for. It has
    # to be checked: Agilior Online and Optima Online printed byte-identical
    # energy blocks in August 2026 ("Belpex_15 * 1 + 12", 25,00 abonnement),
    # so the formula cannot tell them apart, and Energy Knights renamed every
    # product once already (Elektriciteit Dynamisch15 became Agilior Online at
    # the turn of 2026). Matching exactly also keeps the "Green" twin, which
    # prints "Agilior Online Green", from parsing as the plain product.
    product: str
    # Index token the card names on the offtake rows. Belpex_15 is the
    # quarter-hour day-ahead price, Belpex_h the hourly one and BelpexRLP the
    # load-profile-weighted monthly mean. Reading one against another's axis is
    # a silent mis-price rather than a failure, so the token is captured and
    # checked on every row.
    index: str
    # Index token on the "optie solar" row, when it differs. The two dynamic
    # products settle the credit on the same per-slot index as the offtake
    # leg, which the card states outright ("voor zowel afname als injectie");
    # Essentia settles offtake on the load-weighted BelpexRLP and the credit
    # on the solar-weighted BelpexSPP, which is a different series. Empty
    # means "same as the offtake index".
    injection_index: str = ""
    quarter_hourly: bool = False
    # The slug and product names this product was published under before
    # Energy Knights renamed everything at the turn of 2026, and the first
    # month the archive can be billed from. Agilior reaches back only to
    # 2025-09 because its predecessor did not exist before that; the other
    # two stop at the horizon below.
    legacy_slug: str = ""
    legacy_products: tuple[str, ...] = ()
    archive_from: date = _ARCHIVE_HORIZON
    # The "green" twin of a product is the same card plus one flat
    # "Groene stroom" row, which is added to every register's offset. The rate
    # is not constant: it printed 0,42 c€/kWh in September 2025 and 0,32
    # everywhere else, so it is parsed like everything else here.
    green: bool = False

    @property
    def credit_index(self) -> str:
        return self.injection_index or self.index

    def slug_for(self, year_month: date) -> str:
        return self.slug if year_month >= _RENAMED_FROM else self.legacy_slug

    def products_for(self, year_month: date) -> tuple[str, ...]:
        return (self.product,) if year_month >= _RENAMED_FROM else self.legacy_products


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "energyknights_agilior",
        "Energy Knights Agilior Online",
        "dynamic",
        "agilioronline",
        "Agilior Online",
        "Belpex_15",
        quarter_hourly=True,
        legacy_slug="dynamic15",
        # 2025-09 spells it with a space and every later month without one.
        legacy_products=("Elektriciteit Dynamisch 15", "Elektriciteit Dynamisch15"),
        # The predecessor's first published month; "dynamic15" 302s before it.
        archive_from=date(2025, 9, 1),
    ),
    _ContractDef(
        "energyknights_agilis",
        "Energy Knights Agilis Online",
        "dynamic",
        "agilisonline",
        "Agilis Online",
        "Belpex_h",
        legacy_slug="dynamic",
        # Prefix of Agilior's legacy name, so the match has to stay exact:
        # "Elektriciteit Dynamisch" must not accept a "Dynamisch15" card.
        legacy_products=("Elektriciteit Dynamisch",),
    ),
    # "spot_monthly", not "variable". The kind is what decides whether the
    # ENTSO-E key is mandatory, and this contract cannot be priced without
    # one: the c-EUR/kWh figures beside the formula are computed from the VREG
    # weighted average ANNUAL price, not the Belpex-RLP-M the contract
    # settles on, and Energy Knights publishes both series at /priceparameters
    # so the gap is measurable rather than arguable. Over the 26 months that
    # table covers, the printed offtake figure sits at least 10% from the
    # settled index in 19 of them (-24,7% to +56,2%) and the printed credit in
    # 23 of them (-56,1% to +242,9%). Resolved against the month's own mean the
    # offtake leg lands about 5% low with a known, one-directional
    # RLP-weighting residual, and the credit is exact.
    _ContractDef(
        "energyknights_essentia",
        "Energy Knights Essentia Online",
        "spot_monthly",
        "essentiaonline",
        "Essentia Online",
        "BelpexRLP",
        injection_index="BelpexSPP",
        legacy_slug="variable",
        legacy_products=("Elektriciteit Variabel",),
    ),
    # The green twins. Same cards plus one flat "Groene stroom" row, so they
    # share every parser here and differ only by that offset. Optima has no
    # twin listed because Optima itself is out of scope.
    _ContractDef(
        "energyknights_agilior_green",
        "Energy Knights Agilior Online Green",
        "dynamic",
        "agilioronlinegreen",
        "Agilior Online Green",
        "Belpex_15",
        quarter_hourly=True,
        legacy_slug="dynamic15green",
        legacy_products=(
            "Elektriciteit Dynamisch 15 Groen",
            "Elektriciteit Dynamisch15 Groen",
        ),
        archive_from=date(2025, 9, 1),
        green=True,
    ),
    _ContractDef(
        "energyknights_agilis_green",
        "Energy Knights Agilis Online Green",
        "dynamic",
        "agilisonlinegreen",
        "Agilis Online Green",
        "Belpex_h",
        legacy_slug="dynamicgreen",
        legacy_products=("Elektriciteit Dynamisch Groen",),
        green=True,
    ),
    _ContractDef(
        "energyknights_essentia_green",
        "Energy Knights Essentia Online Green",
        "spot_monthly",
        "essentiaonlinegreen",
        "Essentia Online Green",
        "BelpexRLP",
        injection_index="BelpexSPP",
        legacy_slug="variablegreen",
        legacy_products=("Elektriciteit Variabel Groen",),
        green=True,
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Every residential electricity slug on the tariff card listing, so discover()
# flags only a genuinely new product. The four "green" twins and Optima Online
# are catalogued but not modelled; see the module docstring.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "agilioronline",
        "agilioronlinegreen",
        "agilisonline",
        "agilisonlinegreen",
        "essentiaonline",
        "essentiaonlinegreen",
        "optimaonline",
        "optimaonlinegreen",
    }
)
assert {c.slug for c in _CONTRACTS} <= DISCOVER_IDS

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = NUM_NO_THOUSANDS

# "Alle prijzen en tarieven zijn inclusief 6% btw", in the card header.
# Anchored on "inclusief" rather than matching any percentage beside "btw":
# page 3 carries footnote (3), "Op administratieve kosten is 21% BTW van
# toepassing", and vat_multiplier takes the FIRST match. Today the header wins
# on page order alone, which would silently gross the whole energy leg by 21%
# the day the footnote moved ahead of it.
_VAT_RE = re.compile(r"inclusief\s+(\d+)\s*%\s*btw", re.IGNORECASE)

# "Groene stroom (c€/kWh) 0,32", present only on a green card and applying to
# every kWh whatever the register. No formula and no footnote, so it is on the
# same VAT-inclusive basis as the printed rate columns.
_GREEN_RE = re.compile(rf"Groene\s+stroom\s*\(c[^)]*\)\s*{_NUM}", re.IGNORECASE)

# "Met Agilior Online van Energy Knights kies je voor:"
_PRODUCT_RE = re.compile(
    r"Met\s+(.+?)\s+van\s+Energy\s+Knights\s+kies\s+je\s+voor", re.IGNORECASE
)

# "Tariefkaart 2026-08", the first line of every card.
_LABEL_RE = re.compile(r"Tariefkaart\s+(\d{4}-\d{2})")

# "Abonnement (€/jaar) 25,00". The unit is matched loosely as "(...jaar)" for
# the same reason the sibling extractors do it: a renderer that drops or
# doubles the euro glyph must not take the contract offline over a mandatory
# row that is otherwise perfectly readable.
_FEE_RE = re.compile(rf"Abonnement\s*\([^)]*jaar\)\s+{_NUM}", re.IGNORECASE)

# The four consumption rows and the injection row all print as
#   <label> (c€/kWh) <indicative> <index> * <coefficient> <sign> <offset>
# with the injection row carrying its VAT-exemption footnote between the unit
# and the number. Each register is anchored on its own label rather than read
# positionally: "Verbruik nacht" and "Verbruik exclusief nacht" are distinct
# rows carrying distinct coefficients on this supplier's variable card, and
# every card published before December 2025 printed all four identically, so
# a parser that read one row and fanned it out would have passed every fixture
# it was likely to be given.
# (snapshot register, the card's own wording, the row anchor). The wording is
# carried so a failure names the row a maintainer has to go and look at rather
# than this module's internal register key.
_ROW_LABELS: tuple[tuple[str, str, str], ...] = (
    ("single", "enkelvoudig", r"Verbruik\s+enkelvoudig"),
    ("peak", "dag", r"Verbruik\s+dag"),
    ("offpeak", "nacht", r"Verbruik\s+nacht"),
    ("exclusive_night", "exclusief nacht", r"Verbruik\s+exclusief\s+nacht"),
)
# Horizontal whitespace only, never a newline. A bare \s would let a row bind
# to the NEXT row's formula whenever the renderer emits the indicative column
# and the formula column as separate blocks, and the index-token guard cannot
# see that: both rows name the same index, so it checks the name and never
# that the formula belongs to the row: on a card whose registers carry
# different coefficients that silently gives one register another's rate.
_H = r"[^\S\n]"
_FORMULA_TAIL = (
    rf"{_H}*\(c[^)]*\){_H}*(?:\(\d\){_H}*)?{_NUM}{_H}+"
    rf"(Belpex[A-Za-z_0-9]*){_H}*\*{_H}*{_NUM}{_H}*([{SIGN_CHARS}]){_H}*{_NUM}"
)
_ROW_RES: dict[str, re.Pattern[str]] = {
    slot: re.compile(anchor + _FORMULA_TAIL, re.IGNORECASE)
    for slot, _wording, anchor in _ROW_LABELS
}
_ROW_CARD_LABELS: dict[str, str] = {
    slot: wording for slot, wording, _anchor in _ROW_LABELS
}
# 'optie "solar" (c€/kWh) (1) 5,85 Belpex_15 * 1 - 12'. The quotes are
# straight on every card seen, but a renderer that curls them would otherwise
# take the whole injection leg offline.
_INJECTION_RE = re.compile(
    r"optie\s*[\"“”]?\s*solar\s*[\"“”]?" + _FORMULA_TAIL,
    re.IGNORECASE,
)

# The net-tariff section prints the same eight areas twice, once for a digital
# meter and once for a classic one, and the two differ by more than half on
# the capacity term. Only the digital block is read, cut at the classic
# header so no analog row can leak in.
# Both markers must stand alone on their line. A plain substring search binds
# the digital one to the solar footnote instead, "Heb je zonnepanelen en een
# digitale meter?", which sits about 900 characters above the table on every
# card where that sentence is not wrapped. Case-insensitive for the same
# reason every other anchor here is: a re-render that capitalises the header
# would otherwise leave the classic table inside the slice.
_DIGITAL_RE = re.compile(
    r"^[^\S\n]*digitale\s+meter[^\S\n]*$", re.IGNORECASE | re.MULTILINE
)
_CLASSIC_RE = re.compile(
    r"^[^\S\n]*klassieke\s+meter[^\S\n]*$", re.IGNORECASE | re.MULTILINE
)

# Row label prefix -> canonical DSO key. Anchored on the leading token so a
# wrapped label ("Fluvius (Halle-" / "Vilvoorde)") still binds to its figures.
_DSO_ROWS: tuple[tuple[str, str], ...] = (
    ("Antwerpen", DSO_FLUVIUS_ANTWERPEN),
    ("Halle", DSO_FLUVIUS_HALLE_VILVOORDE),
    ("Imewo", DSO_FLUVIUS_IMEWO),
    ("Kempen", DSO_FLUVIUS_IVEKA),
    ("Limburg", DSO_FLUVIUS_LIMBURG),
    ("Midden", DSO_FLUVIUS_INTERGEM),
    ("West", DSO_FLUVIUS_WEST),
    ("Zenne-Dijle", DSO_FLUVIUS_ZENNE_DIJLE),
)

# Excise, taken from the first consumption band. The card prints three bands
# and they have not always been equal: every month from 2024-06 to 2026-07 read
# 5,0329 / 5,0329 / 4,8188, and only the August 2026 card is flat at 4,8760.
# Band 1 is what a residential entry bills on for the volumes this integration
# models, and it is the same convention every sibling extractor follows.
_EXCISE_RE = re.compile(
    rf"Verbruik\s+tussen\s+0\s+en\s+3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
_GSC_RE = re.compile(rf"Bijdrage\s+groene\s+stroom\s+{_NUM}", re.IGNORECASE)
_WKK_RE = re.compile(rf"Bijdrage\s+WKK\s+{_NUM}", re.IGNORECASE)
# The tax block is a two-column interleave and "Standaard tarief" heads a row
# in both columns, once under the energy fund in EUR/month and once under the
# energiebijdrage in c€/kWh. Reading either without its header attached is a
# hundredfold unit error in whichever direction the match lands, so both
# anchor on the header line above them.
_CONTRIB_RE = re.compile(
    rf"Energiebijdrage\s*\(c[^)]*\)[^\n]*\n\s*Standaard\s+tarief\s+{_NUM}",
    re.IGNORECASE,
)
# The "Standaard tarief" row, not the 10,07 "Niet-gedomicilieerd" one: this
# integration prices a domiciled residential entry, the same choice every
# sibling makes. The card spells it "Niet-gedomiciliëerd" with two e's.
_FUND_RE = re.compile(
    rf"Bijdrage\s+energiefonds\s*\([^)]*\)[^\n]*\n\s*Standaard\s+tarief\s+{_NUM}",
    re.IGNORECASE,
)


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown Energy Knights contract {contract_id!r}")
    if region not in _FLANDERS_ONLY:
        raise ExtractorError("Energy Knights only operates in Flanders")
    url = _card_url(contract)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - the card is published for Flanders only.
) -> str | None:
    """Cheap freshness key: HEAD the product's own card.

    Energy Knights serves a Last-Modified stamped at the moment it generated
    the month's card (09:00 on the last day of the preceding month for the
    August 2026 set), so the key flips exactly when the rates do.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    return await head_freshness_key(
        session, _card_url(contract), prefer=("Last-Modified", "ETag")
    )


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Energy Knights only sells in Flanders.
    year_month: date,
) -> SupplierSnapshot | None:
    """The card Energy Knights published for one past month, or ``None``.

    ``None`` means "no archive here, bill the proxy": before the contract's own
    horizon, or when the month does not resolve, or when the served card fails
    the validity cross-check. Every failure is swallowed rather than raised,
    because this runs inside the year-to-date walk and one unpublished month
    must not take the whole year down.

    An out-of-range month or a retired slug answers 302 to the marketing
    homepage, which aiohttp follows, so the payload is 480 bytes of HTML rather
    than an error status. Nothing here inspects it: _fetch_validated_pdf_bytes
    rejects it on the magic bytes and raises, and that raise lands in the
    except below.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    first = date(year_month.year, year_month.month, 1)
    if first < contract.archive_from:
        return None
    url = _ARCHIVE_URL.format(
        month=f"{first.year:04d}-{first.month:02d}",
        slug=contract.slug_for(first),
        lang=_LANG,
    )
    try:
        text = await fetch_pdf_text_layout(session, url)
        snap = parse_snapshot(contract_id, text, url, contract.products_for(first))
    except ExtractorError:
        return None
    # The card carries its own "geldig van ... tot en met ..." range, so the
    # authoritative tier of the cross-check always applies and the textual
    # fallback is never needed.
    return archive_validity_check(snap, text, first)


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Product slugs on the tariff card listing, diffed against DISCOVER_IDS
    so live_check can flag a new product."""
    try:
        html = await fetch_text(session, _LISTING_URL)
    except ExtractorError:
        return set()
    return set(re.findall(r"getCurrentTariffchart/([a-z0-9]+)/", html))


def _card_url(contract: _ContractDef) -> str:
    return _CARD_URL.format(slug=contract.slug, lang=_LANG)


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str,
    products: tuple[str, ...] | None = None,
) -> SupplierSnapshot:
    """Parse one card. ``products`` overrides the names the intro line may
    carry, which an archived month needs: these products were called something
    else before 2026."""
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown Energy Knights contract {contract_id!r}")
    _require_product(text, contract, products or (contract.product,))
    vat = vat_multiplier(text, _VAT_RE)
    rows = _extract_rows(text, contract)
    fee = _yearly_fee(text)
    green = _green_premium(text, contract)
    energy: EnergyRates
    if contract.kind == "dynamic":
        energy = _dynamic_energy(rows, fee, vat, contract, green)
    else:
        energy = _spot_monthly_energy(rows, fee, vat, green)
    return SupplierSnapshot(
        supplier="energyknights",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=_publication_label(text),
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text, contract),
    )


def _require_product(
    text: str, contract: _ContractDef, accepted: tuple[str, ...]
) -> None:
    """Fail unless the card names the product we asked for.

    A slug that stops resolving would otherwise be answered with whichever
    card the site decides to serve, and two of these products print the same
    formula, so the wrong card is a wrong price rather than a missing one.

    The comparison stays EXACT for the same reason: Agilis Online's
    predecessor was "Elektriciteit Dynamisch" and Agilior's was "Elektriciteit
    Dynamisch15", so a prefix match would hand every archived Agilis month the
    quarter-hourly card.
    """
    m = _PRODUCT_RE.search(text)
    if m is None:
        raise ExtractorError("Energy Knights: card does not name its product")
    served = " ".join(m.group(1).split())
    if served.casefold() not in {name.casefold() for name in accepted}:
        raise ExtractorError(
            f"Energy Knights {contract.contract_id}: asked for "
            f"{' / '.join(accepted)!r}, card is for {served!r}"
        )


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return m.group(1) if m else ""


def _yearly_fee(text: str) -> float:
    m = _FEE_RE.search(text)
    if m is None:
        # The abonnement is mandatory; fail loud rather than silently bill a
        # zero standing charge on a layout drift.
        raise ExtractorError("Energy Knights: abonnement row not found")
    return to_float(m.group(1))


def _extract_rows(
    text: str, contract: _ContractDef
) -> dict[str, tuple[float, float, float]]:
    """Per-register (indicative EUR/kWh, coefficient, offset EUR/MWh).

    The indicative is VAT-inclusive as printed and the formula is not, so the
    two are returned on their own bases and the callers convert.
    """
    out: dict[str, tuple[float, float, float]] = {}
    missing: list[str] = []
    for slot, pattern in _ROW_RES.items():
        m = pattern.search(text)
        if m is None:
            missing.append(_ROW_CARD_LABELS[slot])
            continue
        index = m.group(2)
        if index.casefold() != contract.index.casefold():
            raise ExtractorError(
                f"Energy Knights {contract.contract_id}: the "
                f"{_ROW_CARD_LABELS[slot]} row is indexed on {index!r}, "
                f"expected {contract.index!r}"
            )
        out[slot] = (
            to_float(m.group(1)) / 100.0,
            to_float(m.group(3)),
            parse_sign(m.group(4)) * to_float(m.group(5)),
        )
    # How many rows are mandatory depends on how many the contract bills.
    # A dynamic card repeats one formula in all four registers and
    # DynamicRates carries a single coefficient pair for every meter, so only
    # the mono row is read and a card that stopped printing one of the others
    # must not take a working contract offline. A monthly-indexed card bills
    # all four, each with its own coefficients, so a row that goes missing
    # there is a silent re-price: relabelling the dag row on the August 2026
    # Essentia card moves peak hours -2,05% and off-peak +2,39%, and every
    # bound in the live check still passes because the values that remain are
    # all plausible.
    required = (
        [_ROW_CARD_LABELS["single"]]
        if contract.kind == "dynamic"
        else list(_ROW_CARD_LABELS.values())
    )
    absent = [label for label in required if label in missing]
    if absent:
        raise ExtractorError(
            f"Energy Knights {contract.contract_id}: could not parse the "
            f"{', '.join(absent)} consumption row(s)"
        )
    return out


def _green_premium(text: str, contract: _ContractDef) -> float:
    """The "Groene stroom" adder in EUR/kWh, or 0.0 on a plain card.

    Mandatory on a green product: the row is what the customer is paying the
    premium for, so a card that stopped printing it would silently bill them
    the grey rate. It is not constant either, 0,42 c€/kWh in September 2025
    against 0,32 everywhere else, so it is parsed rather than pinned. The row
    carries no formula and no footnote, which puts it on the same
    VAT-inclusive basis as the printed rate columns.
    """
    m = _GREEN_RE.search(text)
    if m is None:
        if contract.green:
            raise ExtractorError(
                f"Energy Knights {contract.contract_id}: "
                "groene stroom row not found on a green card"
            )
        return 0.0
    return to_float(m.group(1)) / 100.0


def _dynamic_energy(
    rows: dict[str, tuple[float, float, float]],
    fee: float,
    vat: float,
    contract: _ContractDef,
    green: float = 0.0,
) -> DynamicRates:
    """The per-slot leg. Agilior settles per quarter hour, Agilis per hour.

    DynamicRates carries one coefficient pair for every meter, which is what
    these cards print: the dag, nacht and exclusief-nacht rows repeat the
    enkelvoudig formula verbatim on every dynamic card published so far.
    """
    _, coefficient, offset = rows["single"]
    # The card states "Tariefformule in EUR/MWh excl BTW", so the coefficient
    # is a dimensionless multiplier on a EUR/kWh spot (scaled by VAT, never by
    # 10) and the offset goes EUR/MWh to EUR/kWh.
    return DynamicRates(
        factor=coefficient * vat,
        # The green adder rides on the offset: it is a flat per-kWh charge on
        # top of whatever the formula resolves to, already VAT-inclusive.
        base=offset / 1000.0 * vat + green,
        yearly_fixed_fee=fee,
        quarter_hourly=contract.quarter_hourly,
    )


def _spot_monthly_energy(
    rows: dict[str, tuple[float, float, float]],
    fee: float,
    vat: float,
    green: float = 0.0,
) -> SpotMonthlyRates:
    """Essentia Online's monthly-indexed leg.

    The card prints a separate formula per register and they differ: August
    2026 reads "x 1,03 + 7" mono, "x 1,045 + 8" day and "x 0,997 + 8" for both
    night registers. Every register the card prints is carried, including the
    dedicated exclusive-night pair, which pricing routes ahead of the
    bi-hourly band test because that circuit is billed per meter rather than
    per hour of the day.

    Nothing of the printed c-EUR/kWh column is stored. It is an estimate off
    the VREG weighted average annual price rather than the Belpex-RLP-M this
    contract settles on, and SpotMonthlyRates has nowhere to keep a fallback
    anyway: the kind makes the ENTSO-E key mandatory, so the coefficients
    always resolve.
    """

    def pair(slot: str) -> tuple[float | None, float | None]:
        row = rows.get(slot)
        if row is None:
            return (None, None)
        # The card states "Tariefformule in EUR/MWh excl BTW", so the
        # coefficient is a dimensionless multiplier on a EUR/kWh mean (scaled
        # by VAT, never by 10) and the offset goes EUR/MWh to EUR/kWh.
        # The green adder is a flat per-kWh charge on top of whatever the
        # formula resolves to, so it rides on EVERY register's offset rather
        # than on the mono one alone: a bi-hourly customer pays it on every
        # kWh too, and it is already VAT-inclusive.
        return (row[1] * vat, row[2] / 1000.0 * vat + green)

    factor, base = pair("single")
    assert factor is not None and base is not None  # _extract_rows guarantees it
    peak_factor, peak_base = pair("peak")
    offpeak_factor, offpeak_base = pair("offpeak")
    night_factor, night_base = pair("exclusive_night")
    return SpotMonthlyRates(
        factor=factor,
        base=base,
        factor_peak=peak_factor,
        base_peak=peak_base,
        factor_offpeak=offpeak_factor,
        base_offpeak=offpeak_base,
        factor_exclusive_night=night_factor,
        base_exclusive_night=night_base,
        yearly_fixed_fee=fee,
    )


def _extract_injection(text: str, contract: _ContractDef) -> InjectionRates:
    m = _INJECTION_RE.search(text)
    if m is None:
        # Every card prints the "optie solar" row; a miss is a layout drift,
        # not a contract that pays nothing. Raise rather than silently credit
        # a solar user 0 EUR/kWh.
        raise ExtractorError("Energy Knights: injection row not found")
    index = m.group(2)
    if index.casefold() != contract.credit_index.casefold():
        raise ExtractorError(
            f"Energy Knights {contract.contract_id}: injection is indexed on "
            f"{index!r}, expected {contract.credit_index!r}"
        )
    coefficient = to_float(m.group(3))
    offset = parse_sign(m.group(4)) * to_float(m.group(5))
    formula = (
        f"{contract.credit_index} * {m.group(3)} {m.group(4)} {m.group(5)} EUR/MWh"
    )
    # Residential injection is VAT-exempt and the card says so on this row
    # with footnote (1), so neither the coefficient nor the offset is grossed.
    # The offset is a deduction in EUR/MWh, and it is large enough that the
    # credit turns negative whenever the index falls below it, which the
    # August 2026 card does below 12 EUR/MWh.
    if contract.kind == "dynamic":
        # No `current`: the credit settles against each slot's own index, so
        # the printed figure is an illustration rather than a rate to prefer.
        return InjectionRates(factor=coefficient, base=offset / 1000.0, formula=formula)
    # Essentia settles the credit on Belpex-SPP-M, the solar-weighted monthly
    # mean, while its energy leg indexes on the load-weighted Belpex-RLP-M.
    # spp_indexed is what stops the coordinator resolving this formula against
    # the energy leg's mean: measured against Energy Knights' own published
    # series, the SPP-weighted mean the integration already computes matches
    # the settled index to 0,01%, while the two series themselves run far
    # apart in a sunny month.
    #
    # `current` is kept even though the kind guarantees a key, because the
    # SPP mean needs Synergrid's solar profile on top of the spots and the
    # coordinator leaves the credit on the printed figure until that lands.
    # It is a VREG-derived illustration, averaging 56% from the settled index,
    # so it is the cold-start value and never the answer.
    return InjectionRates(
        current=to_float(m.group(1)) / 100.0,
        factor=coefficient,
        base=offset / 1000.0,
        formula=formula,
        spp_indexed=True,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """The digital-meter half of the net-tariff table.

    The card prints a classic-meter table below it whose distribution rate is
    half again as high and whose capacity term is more than double, so the cut
    is not cosmetic. Every Energy Knights product is sold as "100% digitaal"
    and the dynamic ones bill on meetregime 3 quarter values, which only a
    digital meter produces.
    """
    head = _DIGITAL_RE.search(text)
    if head is None:
        raise ExtractorError("Energy Knights: could not locate the DSO table")
    tail = _CLASSIC_RE.search(text, head.end())
    section = text[head.end() :] if tail is None else text[head.end() : tail.start()]
    out: dict[str, DsoOverlay] = {}
    for prefix, key in _DSO_ROWS:
        row = re.search(
            rf"Fluvius{_H}*\({_H}*{re.escape(prefix)}[^\d\n]*"
            rf"{_NUM}{_H}+{_NUM}{_H}+{_NUM}{_H}+{_NUM}{_H}+{_NUM}",
            section,
            re.IGNORECASE,
        )
        if row is None:
            continue
        # Columns: afname normaal, afname ex nacht, databeheer SMR1,
        # databeheer SMR3, capaciteitstarief. SMR3 is the quarter-hourly
        # regime the dynamic products bill on. Energy Knights has printed the
        # two databeheer columns at the same value on every card since
        # January 2025, so the column choice has never mattered in practice.
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(1)) / 100.0,
            distribution_exclusive_night=to_float(row.group(2)) / 100.0,
            transport=0.0,
            data_management_per_year=to_float(row.group(4)),
            capacity_eur_per_kw_year=to_float(row.group(5)),
        )
    missing = [key for _, key in _DSO_ROWS if key not in out]
    if missing:
        # A partial table is worse than none: the eight areas are what every
        # Flemish entry picks its network cost from, and a missing one would
        # leave that user with no distribution charge at all.
        raise ExtractorError(
            f"Energy Knights: DSO rows not found for {sorted(missing)}"
        )
    return out


def _extract_taxes(text: str) -> TaxOverlay:
    """Every value in this block is VAT-inclusive as printed, the excise
    included: 4,8760 is 4,60 x 1,06, and the one card in this repo that prints
    ex-VAT figures (Ecopower) carries 0,04748 for the same band, which is
    5,0329 / 1,06. The energy fund is the single exemption, per the card's own
    footnote (1) "Bedrag niet onderworpen aan BTW", and flanders_tax_overlay
    leaves it unscaled."""
    return flanders_tax_overlay(
        text,
        supplier="Energy Knights",
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_CONTRIB_RE,
        fund=_FUND_RE,
    )


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    id="energyknights",
    label="Energy Knights",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_FLANDERS_ONLY,
            # spot_indexed_injection stays False on all three. It exists to
            # offer an OPTIONAL key to a contract whose kind does not collect
            # one, and every kind here is in SPOT_PRICED_CONTRACT_KINDS, so
            # the key is already mandatory: setting it would add a second,
            # redundant key step to the flow.
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)


__all__ = [
    "DISCOVER_IDS",
    "EXTRACTOR",
    "discover",
    "fetch",
    "fetch_for_month",
    "parse_snapshot",
    "probe",
]
