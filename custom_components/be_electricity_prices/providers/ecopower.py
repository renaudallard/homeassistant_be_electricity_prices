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

"""Ecopower (Flemish citizen cooperative) tariff extractor.

Ecopower sells two residential electricity products in Flanders only:

1. "Groene burgerstroom" (green citizen power) -- a half-fixed,
   half-indexed tariff against the monthly RLP-weighted Belpex
   Day-Ahead average:

       energy = 0.5 * 0.17 + 0.5 * Belpex_DA  (EUR/kWh, HTVA)

   A new card is published every month at a CDN URL that rotates each
   month (``cdn.nimbu.io/.../<YYYYMM>_gbs_tariefkaart.pdf``); the public
   price page at ``ecopower.be/groene-stroom/prijs-nieuw`` lists the
   most recent four or so months. We scrape that page to find the latest
   definitive card (Ecopower also publishes a *next-month* "inschatting"
   / estimation card that we deliberately ignore until it's finalized).

2. "Dynamische burgerstroom" -- a quarter-hourly EPEX Day-Ahead dynamic
   tariff (quarter-hourly since the SDAC 15-minute market switch of
   2025-10-01). The card prints the consumer formula directly:

       afname    = 1.02 * EPEX_DA + 4  EUR/MWh  (HTVA)
       injectie  = 0.98 * EPEX_DA - 15 EUR/MWh  (VAT-exempt)

   The dynamic card lives on the product page
   ``ecopower.be/groene-stroom/dynamische-burgerstroom`` as
   ``<YYYYMM>_dbs_tariefkaart.pdf``. Unlike the monthly gbs card, the
   dynamic card is republished only when the formula, DSO or tax rates
   change, so the latest card is the one in effect today.

All amounts on both cards are HTVA. Residential customers pay 6% VAT;
the snapshot's ``TaxOverlay.vat_rate=0.06`` instructs ``compute_breakdown``
to scale up to TVAC, matching every other supplier's all-in number.
Injection is VAT-exempt for residential customers, so its formula is
stored unscaled.
"""

from __future__ import annotations

import logging
import re
from datetime import date

import aiohttp

from ..const import (
    FLUVIUS_CARD_LABELS,
    REGION_FLANDERS,
)
from ._pdf import (
    NL_MONTHS,
    SIGN_CHARS,
    archive_validity_check,
    extract_pdf_text_layout,
    fetch_pdf_text_layout,
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
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)

# Dutch month names for archive_validity_check; the helper indexes into
# this tuple as month_names[year_month.month - 1].
_NL_MONTHS = NL_MONTHS

_BASE_URL = "https://ecopower.be"
_PRICE_PAGE = f"{_BASE_URL}/groene-stroom/prijs-nieuw"

# Card filenames look like 202604_gbs_tariefkaart.pdf for a definitive
# April 2026 card, or 202605_gbs_inschatting_tariefkaart_ecopower.pdf
# for a next-month "inschatting" (estimation) that gets replaced by the
# definitive card on the 1st. Match only the definitive form.
_CARD_RE = re.compile(
    r'(https?://[^"]+/(?P<yyyymm>20\d{4})_gbs_tariefkaart\.pdf[^"]*)"',
    re.IGNORECASE,
)

_DSO_LABELS = FLUVIUS_CARD_LABELS


_CONTRACT_ID = "ecopower_burgerstroom"
_CONTRACT_LABEL = "Ecopower Groene Burgerstroom"

_DBS_CONTRACT_ID = "ecopower_dynamische_burgerstroom"
_DBS_CONTRACT_LABEL = "Ecopower Dynamische Burgerstroom"
_DBS_PAGE = f"{_BASE_URL}/groene-stroom/dynamische-burgerstroom"

# Dynamic card filenames look like 202601_dbs_tariefkaart.pdf, with older
# variants carrying a letter suffix (202501b_dbs_tariefkaart.pdf) or a
# trailing brand token (202406_dbs_tariefkaart_ecopower.pdf). The yyyymm
# group captures the six leading digits; the optional letter is consumed
# but not captured so month ordering stays numeric.
_DBS_CARD_RE = re.compile(
    r'(https?://[^"]+/(?P<yyyymm>20\d{4})[a-z]?_dbs_tariefkaart[^"]*\.pdf[^"]*)"',
    re.IGNORECASE,
)

# Identifier discover() emits for the dynamic card. Ecopower keys its
# tariefkaart PDFs by filename family ("dbs"), not by contract id, so the
# catalog drift detector diffs on this family-based vocabulary.
_DBS_DISCOVER_ID = "ecopower_dbs"

# The ids discover() emits for the registered products. The live-check
# diffs the live catalogue against this baseline; deriving it here keeps
# the CI baseline from drifting away from the scraper.
DISCOVER_IDS: frozenset[str] = frozenset({_CONTRACT_ID, _DBS_DISCOVER_ID})


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if region != REGION_FLANDERS:
        raise ExtractorError("Ecopower only sells residential electricity in Flanders")
    if contract_id == _CONTRACT_ID:
        pdf_url, label = await _resolve_latest_pdf(session)
        text = await fetch_pdf_text_layout(session, pdf_url)
        return parse_snapshot(text, pdf_url, label)
    if contract_id == _DBS_CONTRACT_ID:
        pdf_url, label = await _resolve_latest_dbs_pdf(session)
        text = await fetch_pdf_text_layout(session, pdf_url)
        return parse_dbs_snapshot(text, pdf_url, label)
    raise ExtractorError(f"unknown Ecopower contract {contract_id!r}")


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Ecopower is Flanders-only.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Ecopower card for a specific (year, month).

    The price page lists the last few months' definitive cards. Find
    the one whose YYYYMM filename prefix matches the requested month
    and parse it. Returns None when the listing doesn't carry the
    month (Ecopower only retains ~4 months back), the URL 404s, or the
    PDF doesn't parse.
    """
    if contract_id == _DBS_CONTRACT_ID:
        return await _fetch_dbs_for_month(session, year_month)
    if contract_id != _CONTRACT_ID:
        return None
    target = f"{year_month.year:04d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _PRICE_PAGE)
    except ExtractorError:
        return None
    pdf_url: str | None = None
    for match in _CARD_RE.finditer(html):
        if (
            match.group("yyyymm") == target
            and "inschatting" not in match.group(1).lower()
        ):
            pdf_url = match.group(1)
            break
    if pdf_url is None:
        return None
    try:
        text = await fetch_pdf_text_layout(session, pdf_url)
        label = f"{target[:4]}-{target[4:]}"
        snap = parse_snapshot(text, pdf_url, label)
    except ExtractorError:
        return None
    # Cross-check the parsed card actually covers the requested month;
    # if the CDN ever serves the current card under a historical URL
    # the validity / title check rejects it instead of mis-billing past
    # consumption at current rates.
    return archive_validity_check(snap, text, year_month, month_names=_NL_MONTHS)


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Ecopower is Flanders-only, but signature is shared.
) -> str | None:
    """Cheap freshness probe: HEAD the price page, return its Last-Modified.

    The page returns a stable Last-Modified header (server-side cache key),
    so a HEAD round-trip is enough to detect a publication. Falls back to
    None on transport / missing-header so the coordinator's TTL takes over.
    """
    if contract_id == _CONTRACT_ID:
        return await head_freshness_key(session, _PRICE_PAGE)
    if contract_id == _DBS_CONTRACT_ID:
        return await head_freshness_key(session, _DBS_PAGE)
    return None


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the family ids visible across Ecopower's price pages.

    Ecopower sells two residential products, each keyed by its
    tariefkaart filename family: the static "Groene burgerstroom" (gbs)
    on the price page and the dynamic "Dynamische burgerstroom" (dbs) on
    its own page. Both pages are scraped and matched together, so a new
    ``..._tariefkaart.pdf`` family on *either* page is surfaced verbatim
    as ``ecopower_<family>`` for the catalog drift detector. The gbs
    family is skipped here (the bare card is already registered and
    ``gbs_inschatting`` is the next-month preview the fetcher ignores) and
    dbs is matched by its dedicated regex.

    A page that fails to fetch is logged rather than swallowed: otherwise
    a partial failure would drop that page's family from a still-non-empty
    result and slip past live_check's empty-result warning.
    """
    bodies: list[str] = []
    for page in (_PRICE_PAGE, _DBS_PAGE):
        try:
            bodies.append(await fetch_text(session, page))
        except ExtractorError as err:
            _LOGGER.warning("Ecopower discover: %s unreachable: %s", page, err)
    combined = "\n".join(bodies)
    out: set[str] = set()
    if _CARD_RE.search(combined):
        out.add(_CONTRACT_ID)
    if _DBS_CARD_RE.search(combined):
        out.add(_DBS_DISCOVER_ID)
    for other in re.findall(
        r'/(20\d{4}[a-z]?_(?:[a-z_]+_)?tariefkaart[^"]*)\.pdf', combined, re.IGNORECASE
    ):
        family = re.sub(r"^20\d{4}[a-z]?_", "", other)
        family = re.sub(r"_tariefkaart.*$", "", family)
        if family and not family.startswith(("gbs", "dbs")):
            out.add(f"ecopower_{family}")
    return out


def parse_snapshot(
    text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    return SupplierSnapshot(
        supplier="ecopower",
        contract=_CONTRACT_ID,
        energy=_extract_energy(text),
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
    )


def parse_dbs_snapshot(
    text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser for the Dynamische burgerstroom card, exposed for tests.

    The tax block (GSC/WKK renewables, federal excise, energy
    contribution, energy fund, 6% VAT) is identical in layout to the
    gbs card, so ``_extract_taxes`` is reused as-is. Only the energy
    (dynamic formula) and DSO row layouts differ.
    """
    return SupplierSnapshot(
        supplier="ecopower",
        contract=_DBS_CONTRACT_ID,
        energy=_extract_dbs_energy(text),
        dsos=_extract_dbs_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_dbs_injection(text),
    )


# ---- energy ------------------------------------------------------------------


# Anchor on the start of the consumption line ("Groene burgerstroom" or
# "Afname Groene burgerstroom"). Without the line anchor this also matched
# the "Injectie Groene Burgerstroom ... euro/kWh" line, so a card that
# printed split-layout energy (value below the label) together with a
# same-line injection value would bind the energy rate to the injection
# figure instead of falling through to _ENERGY_SPLIT_RE.
_ENERGY_RE = re.compile(
    r"^[ \t]*(?:Afname\s+)?Groene\s+burgerstroom[^\n]*?([\d,]+)\s*euro/kWh",
    re.IGNORECASE | re.MULTILINE,
)

# Mid-2026 cards moved the resolved rate onto the line *below* the
# "Afname Groene burgerstroom (50% vast ... + 50% variabel ...)" label
# instead of trailing it on the same line. Fall back to this when the
# same-line form misses.
_ENERGY_SPLIT_RE = re.compile(
    r"Afname\s+Groene\s+burgerstroom[^\n]*\n\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


def _extract_energy(text: str) -> EnergyRates:
    """Parse the "Groene burgerstroom" effective rate (HTVA, EUR/kWh).

    The card prints the formula breakdown
    ``(50% vast aan 0,17 euro + 50% variabel aan 0,08472117 euro)``
    followed by the resolved ``0,1274 euro/kWh`` figure. We use the
    resolved number because (a) we don't have a Belpex feed at parse
    time, and (b) supporting Ecopower's variable cost without a live
    spot is exactly what ``VariableRates`` is for.
    """
    match = _ENERGY_RE.search(text) or _ENERGY_SPLIT_RE.search(text)
    if not match:
        raise ExtractorError("could not parse Ecopower 'Groene burgerstroom' rate")
    return VariableRates(current=to_float(match.group(1)))


# ---- dynamic energy ----------------------------------------------------------

# The dynamic card prints the consumer formula in EUR/kWh terms, with the
# EPEX DA spot expressed in EUR/MWh:
#   "Dynamische burgerstroom elk kwartier 0,00102 × EPEX DA +0,004 euro/kWh"
# The '×' is U+00D7 but a re-render could swap it; accept the common
# multiplication glyphs. SIGN_CHARS covers every minus/plus variant for the
# additive base so a punctuation drift never flips the sign silently.
_DBS_ENERGY_RE = re.compile(
    r"Dynamische\s+burgerstroom\s+elk\s+kwartier\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

_ABONNEMENT_RE = re.compile(r"Abonnementskost\s+([\d,]+)\s*euro/maand", re.IGNORECASE)


def _extract_dbs_energy(text: str) -> DynamicRates:
    """Parse the dynamic ``factor x spot + base`` consumption formula (HTVA).

    The card multiplies the EPEX DA price in EUR/MWh, so ``factor`` is
    scaled by 1000 to act on the spot price the pricing engine feeds in
    EUR/kWh: ``0,00102 × MWh = 1.02 × kWh``. Values are HTVA;
    ``vat_rate=0.06`` in the tax overlay scales the energy component to
    TVAC in ``compute_breakdown`` (the same convention as the gbs card),
    so they are NOT pre-scaled here.

    The monthly subscription (``Abonnementskost``) maps to
    ``yearly_fixed_fee``, which is consumed as the actual annual euros
    without further VAT scaling, so the 6% residential VAT is baked in.
    """
    match = _DBS_ENERGY_RE.search(text)
    if not match:
        raise ExtractorError(
            "could not parse Ecopower 'Dynamische burgerstroom' formula"
        )
    factor = to_float(match.group(1)) * 1000.0
    base = parse_sign(match.group(2)) * to_float(match.group(3))
    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=_extract_dbs_abonnement(text),
        quarter_hourly=True,
    )


def _extract_dbs_abonnement(text: str) -> float:
    """Yearly subscription fee in EUR, VAT-inclusive.

    Printed HTVA as ``Abonnementskost 5,00 euro/maand``. yearly_fixed_fee
    holds the actual euros the customer pays (it is summed without
    rescaling in the YTD path), so multiply out the 12 months and the 6%
    residential VAT here.
    """
    match = _ABONNEMENT_RE.search(text)
    if not match:
        raise ExtractorError("could not parse Ecopower Abonnementskost")
    return to_float(match.group(1)) * 12.0 * 1.06


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the DIGITAL METER block.

    Ecopower's card lists two networks per Fluvius sub-area: digital
    meter rates (capacity tariff per kW/yr, lower per-kWh distribution)
    and analog meter rates (yearly fixed fee, higher distribution,
    spinning-back prosumer fee). The integration only models the
    digital path -- which is what the vast majority of Flemish
    residential is on post-2024-mandatory-rollout. Analog-meter users
    can still see realistic prices because Ecopower bills them at the
    SAME ENERGY rate, only the network costs differ.
    """
    section = _slice_between(text, "DIGITALE METER", "ANALOGE METER")
    if section is None:
        raise ExtractorError("could not locate Ecopower DIGITALE METER block")
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_LABELS.items():
        # Row layout in the digital block:
        #   <label> | databeheer EUR/yr | capacity EUR/kW/yr | -
        #           | enkelvoudig EUR/kWh | uitsluitend_nacht EUR/kWh | -
        #
        # An optional 7th column ("Maximumtarief") slides in between
        # uitsluitend_nacht and the trailing dash on rows where
        # Fluvius publishes a maximum (Imewo's Apr 2026 card has one).
        row = re.search(
            rf"^{re.escape(label)}\s+([\d,]+)\s+([\d,]+)\s+-\s+([\d,]+)\s+([\d,]+)"
            rf"(?:\s+[\d,]+)?\s+-",
            section,
            re.MULTILINE,
        )
        if not row:
            continue
        # Ecopower's card is HTVA. Per-kWh distribution flows through the
        # pricing engine's VAT factor, but the capacity and data-management
        # tariffs are billed as fixed euro amounts that bypass it (the YTD
        # and backfill paths sum them as flat annual fees), so bake the 6%
        # residential VAT into both here, mirroring _extract_dbs_abonnement.
        # The same Fluvius databeheer fee prints 17,85 HTVA here vs 18,92
        # TVAC on the other suppliers' cards (17,85 x 1,06 = 18,92).
        databeheer = to_float(row.group(1)) * 1.06
        capacity = to_float(row.group(2)) * 1.06
        single = to_float(row.group(3))
        # Group 4 is the exclusive-night meter rate (separate circuit
        # for an electric water heater / night-storage heater). It
        # used to be dropped because there was no DsoOverlay column
        # for it; now propagated for users on the exclusive_night
        # meter type. Same scaling as ``single``.
        excl_night = to_float(row.group(4))
        out[key] = DsoOverlay(
            distribution_single=single,
            distribution_exclusive_night=excl_night,
            transport=0.0,  # rolled into distribution on Ecopower's card
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
        )
    if not out:
        # The section header matched but no DSO row did - a column-layout
        # drift. Returning {} would let the backfill path silently skip
        # whole months (it swallows the resulting KeyError); fail loud.
        raise ExtractorError("Ecopower: no DSO rows parsed from the digital block")
    return out


def _slice_between(text: str, start: str, end: str) -> str | None:
    s = text.find(start)
    if s < 0:
        return None
    e = text.find(end, s + len(start))
    return text[s + len(start) : e] if e >= 0 else text[s + len(start) :]


# pdfplumber wraps the longest DSO label across its data row on the
# narrower dynamic card -- "Fluvius Midden-" / "<numbers>" / "Vlaanderen"
# on three lines. Stitch the two label fragments back together around the
# rate row so the per-DSO row regex sees one line. [ \t] (not \s) keeps
# the regex from swallowing the row's trailing newline.
_DBS_WRAPPED_LABEL_RE = re.compile(r"(Fluvius\s+\S*-)\n((?:[\d,]+[ \t]*){4,})\n(\S+)")


def _extract_dbs_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the digital-meter network tariffs from the dynamic card.

    The dynamic card carries only digital (meetregime 3 / SMR3) meter
    rows -- there's no analog block, since a dynamic contract requires a
    smart meter. The row layout differs from the gbs card: the columns
    are ``databeheer (EUR/yr) | capacity (EUR/kW/yr) | afname
    enkelvoudig (EUR/kWh) | afname uitsluitend-nacht (EUR/kWh) |
    [maximumtarief] | injectietarief``, with no separating dashes. We
    read the first four numeric columns -- the same four the gbs parser
    keeps -- and ignore the optional maximumtarief and the injection
    network tariff, which ``DsoOverlay`` does not model.
    """
    section = _slice_between(text, "Nettarieven", "Heffingen")
    if section is None:
        raise ExtractorError("could not locate Ecopower dynamic net-tariff block")
    section = _DBS_WRAPPED_LABEL_RE.sub(
        lambda m: f"{m.group(1)}{m.group(3)} {m.group(2).strip()}", section
    )
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_LABELS.items():
        row = re.search(
            rf"^{re.escape(label)}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)",
            section,
            re.MULTILINE,
        )
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row.group(3)),
            distribution_exclusive_night=to_float(row.group(4)),
            transport=0.0,  # rolled into distribution on Ecopower's card
            # HTVA card; capacity and databeheer are flat euro fees that
            # bypass pricing's VAT factor, so bake the 6% residential VAT
            # into both here (same as the gbs parser).
            capacity_eur_per_kw_year=to_float(row.group(2)) * 1.06,
            data_management_per_year=to_float(row.group(1)) * 1.06,
        )
    if not out:
        # Section header matched but no DSO row did - fail loud rather than
        # return an empty overlay set the backfill path silently skips.
        raise ExtractorError("Ecopower: no DSO rows parsed from the dynamic block")
    return out


# ---- taxes -------------------------------------------------------------------


_FEDERAL_EXCISE_RE = re.compile(
    r"Bijzondere accijns[^\n]*tussen 0\s+en\s+3\.000[^\n]*?([\d,]+)\s*euro/kWh"
)
_ENERGY_CONTRIB_RE = re.compile(r"Bijdrage op de energie\s+([\d,]+)\s*euro/kWh")
_GSC_RE = re.compile(r"Kost GSC\s+([\d,]+)\s*euro/kWh")
_WKK_RE = re.compile(r"Kost WKK\s+([\d,]+)\s*euro/kWh")
_FUND_RE = re.compile(r"Bijdrage Energiefonds\s+([\d,]+)\s*euro/maand", re.IGNORECASE)


def _extract_taxes(text: str) -> TaxOverlay:
    """Parse the federal/regional tax block.

    Ecopower prints all values HTVA. ``vat_rate=0.06`` tells the
    pricing engine to scale up to TVAC for residential customers --
    every other supplier publishes TVAC and uses ``vat_rate=0.0``, but
    Ecopower is the cooperative outlier.

    Flanders renewables: GSC + WKK certificate costs are the regional
    renewable surcharge in disguise. They're listed in the energy
    block but are passed straight through to the user (per-kWh), so
    they belong in ``flanders_renewables`` rather than baking them
    into ``energy.current`` (which would mean their value silently
    moved when Fluvius changes the certificate quota).
    """
    federal_match = _FEDERAL_EXCISE_RE.search(text)
    contrib_match = _ENERGY_CONTRIB_RE.search(text)
    gsc_match = _GSC_RE.search(text)
    wkk_match = _WKK_RE.search(text)
    fund_match = _FUND_RE.search(text)
    if not federal_match or not contrib_match:
        raise ExtractorError("could not parse Ecopower federal tax block")
    # GSC and WKK are the Flanders renewable surcharge and are printed on
    # every card, so a miss is a label drift, not an optional row. Treating
    # them as optional (silently zero) would let a relabel drop a mandatory
    # per-kWh charge without failing, so require them like the federal rows.
    if not gsc_match or not wkk_match:
        raise ExtractorError("could not parse Ecopower GSC/WKK renewable surcharge")
    return TaxOverlay(
        federal_excise=to_float(federal_match.group(1)),
        energy_contribution=to_float(contrib_match.group(1)),
        flanders_renewables=(
            to_float(gsc_match.group(1)) + to_float(wkk_match.group(1))
        ),
        energy_fund_eur_per_month=(
            to_float(fund_match.group(1)) if fund_match else 0.0
        ),
        vat_rate=0.06,
    )


# ---- injection ---------------------------------------------------------------


_INJECTION_RE = re.compile(
    # The injection row was relabelled on the May 2026 card. Match both:
    #   <= Apr 2026:  "Terugleververgoeding (digitale meter) 2 -0,0200 euro/kWh"
    #   >= May 2026:  "Injectie Groene Burgerstroom (terugleververgoeding)2 -0,0200 euro/kWh"
    # SIGN_CHARS covers every minus glyph (hyphen, figure/en/em dash, U+2212)
    # a PDF re-render might swap in, so the sign never flips silently.
    r"(?:Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)"
    r"|Terugleververgoeding[^\n]*digitale\s+meter)"
    rf"[^\n]*?([{SIGN_CHARS}]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# Split-layout fallback: mid-2026 cards print the resolved injection
# value on the formula line *below* the label rather than on the label
# line. Anchor on the label, then take the value on the next line.
_INJECTION_SPLIT_RE = re.compile(
    r"Injectie\s+Groene\s+Burgerstroom\s*\(terugleververgoeding\)[^\n]*\n"
    rf"[^\n]*?([{SIGN_CHARS}]?\s*[\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)

# Authoritative current-month statement on the split-layout cards that
# show a 50% fixed + 50% variable injection formula: an
# "OPGELET t.e.m. <date> is de terugleververgoeding <value> euro/kWh en
# 100% vast" note pins the actually-applied fixed credit. The formula
# line below the label resolves the *variable* half, which only kicks in
# once the note's date passes (Ecopower flips injection to 50% variable
# from 1 July 2026), so this fixed value must win while it's printed.
_INJECTION_FIXED_RE = re.compile(
    r"terugleververgoeding\s+([\d,]+)\s*euro/kWh\s+en\s+100\s*%\s*vast",
    re.IGNORECASE,
)

_NL_MONTH_INDEX = {name: i + 1 for i, name in enumerate(_NL_MONTHS)}
_MONTH_ALT = "|".join(_NL_MONTHS)
_FIXED_NOTE_EXPIRY_RE = re.compile(rf"t\.e\.m\.\s+\d+\s+({_MONTH_ALT})", re.IGNORECASE)
_CARD_MONTH_RE = re.compile(rf"Tariefkaart\s+({_MONTH_ALT})\s+\d{{4}}", re.IGNORECASE)


def _fixed_note_in_effect(text: str) -> bool:
    """Whether the ``... 100% vast`` injection note still applies to this
    card's pricing month.

    The note declares its own expiry (``OPGELET t.e.m. 30 juni ...``).
    Honour the fixed value for cards up to that month, but ignore a stale
    note carried onto a later month's card (e.g. a July card that already
    prints the 50%-variable formula but still carries the old June note),
    which would otherwise credit users the wrong fixed rate. Returns True
    when staleness can't be established, so a card that doesn't print a
    parseable month still trusts the note it shows.
    """
    note = _FIXED_NOTE_EXPIRY_RE.search(text)
    card = _CARD_MONTH_RE.search(text)
    if note is None or card is None:
        return True
    return (
        _NL_MONTH_INDEX[card.group(1).lower()] <= _NL_MONTH_INDEX[note.group(1).lower()]
    )


def _extract_injection(text: str) -> InjectionRates | None:
    """Parse the injection (terugleververgoeding) price.

    The terugleververgoeding is a feed-in credit the customer
    *receives* ("de vergoeding die klanten ... krijgen voor hun
    injectie"; Ecopower states the price is never negative). The card
    prints it as a negative EUR/kWh figure (``-0,0200 euro/kWh``) only
    because it sits in the energy/cost column, where a credit shows as
    a negative cost. Negate it so ``current`` holds the compensation as
    a positive number, matching every other supplier's injection sign.
    """
    # Prefer the explicit current-month fixed credit when the card
    # prints the 100%-vast note: on split-layout cards the label line
    # carries only the 50/50 formula and the line below resolves the
    # variable half, which doesn't apply yet. Falling through to that
    # line credited users the variable value (e.g. 0,0329) instead of
    # the fixed 0,020 they actually receive this month.
    fixed = _INJECTION_FIXED_RE.search(text)
    if fixed is not None and _fixed_note_in_effect(text):
        return InjectionRates(current=abs(to_float(fixed.group(1))))
    match = _INJECTION_RE.search(text) or _INJECTION_SPLIT_RE.search(text)
    if not match:
        return None
    # The credit is never negative (Ecopower states this); the card merely
    # prints it in the energy/cost column as a negative figure. Strip any
    # leading sign glyph -- the regex admits every SIGN_CHARS minus, so the
    # hand-rolled variant list missed U+2010 / U+2011 and to_float raised
    # ValueError on them -- and take the magnitude.
    raw = match.group(1).replace(" ", "").lstrip(SIGN_CHARS)
    return InjectionRates(current=abs(to_float(raw)))


# The dynamic card prints the injection formula like the consumption one:
#   "Terugleververgoeding elk kwartier 0,00098 × EPEX DA - 0,015 euro/kWh"
_DBS_INJECTION_RE = re.compile(
    r"Terugleververgoeding\s+elk\s+kwartier\s+"
    r"([\d,]+)\s*[×xX*]\s*EPEX\s*DA\s*"
    rf"([{SIGN_CHARS}]?)\s*([\d,]+)\s*euro/kWh",
    re.IGNORECASE,
)


def _extract_dbs_injection(text: str) -> InjectionRates | None:
    """Parse the dynamic injection (terugleververgoeding) formula.

    Like the consumption formula, the EPEX DA factor is in EUR/MWh, so
    it is scaled by 1000 to act on the EUR/kWh spot
    (``0,00098 × MWh = 0.98 × kWh``). The card's base is signed
    (``- 0,015``); a negative base means the credit drops below zero at
    low spot, which the pricing engine respects. Residential injection
    is VAT-exempt, so no scaling is applied.
    """
    match = _DBS_INJECTION_RE.search(text)
    if not match:
        return None
    factor = to_float(match.group(1)) * 1000.0
    base = parse_sign(match.group(2)) * to_float(match.group(3))
    return InjectionRates(factor=factor, base=base, formula=match.group(0).strip())


# ---- catalog page scraping ---------------------------------------------------


async def _resolve_latest_pdf(
    session: aiohttp.ClientSession,
) -> tuple[str, str]:
    """Find the latest definitive tariff card PDF on the public price page.

    Ecopower's price page lists the current month plus a few historical
    months, and (around end-of-month) a *next-month* "inschatting" card
    whose URL contains ``inschatting``. We strip those and pick the
    highest YYYYMM among the definitive cards; that's the card whose
    rates are actually being billed today.
    """
    html = await fetch_text(session, _PRICE_PAGE)

    matches = [
        (yyyymm, url)
        for url, yyyymm in (
            (m.group(1), m.group("yyyymm")) for m in _CARD_RE.finditer(html)
        )
        if "inschatting" not in url.lower()
    ]
    if not matches:
        raise ExtractorError(f"no Ecopower tariefkaart link found on {_PRICE_PAGE}")
    matches.sort()
    yyyymm, url = matches[-1]
    label = f"{yyyymm[:4]}-{yyyymm[4:]}"
    return url, label


async def _resolve_latest_dbs_pdf(
    session: aiohttp.ClientSession,
) -> tuple[str, str]:
    """Find the latest Dynamische burgerstroom card on the product page.

    The page lists the current dynamic card plus a few historical ones.
    The dynamic formula is stable across months, so the highest YYYYMM
    is the card billing today.
    """
    html = await fetch_text(session, _DBS_PAGE)
    matches = sorted(
        (m.group("yyyymm"), m.group(1)) for m in _DBS_CARD_RE.finditer(html)
    )
    if not matches:
        raise ExtractorError(f"no Ecopower dbs tariefkaart link found on {_DBS_PAGE}")
    yyyymm, url = matches[-1]
    return url, f"{yyyymm[:4]}-{yyyymm[4:]}"


async def _fetch_dbs_for_month(
    session: aiohttp.ClientSession, year_month: date
) -> SupplierSnapshot | None:
    """Return the dynamic card in effect for ``year_month``.

    Dynamic cards don't rotate monthly; Ecopower republishes one only
    when the formula, DSO or tax rates change (typically at a year
    boundary). Pick the most recent card whose YYYYMM prefix is not after
    the requested month -- that's the card that was billing then. Falls
    back to None (coordinator uses the proxy snapshot) when the page omits
    the month or the PDF doesn't parse.
    """
    target = f"{year_month.year:04d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _DBS_PAGE)
    except ExtractorError:
        return None
    eligible = sorted(
        (m.group("yyyymm"), m.group(1))
        for m in _DBS_CARD_RE.finditer(html)
        if m.group("yyyymm") <= target
    )
    if not eligible:
        return None
    yyyymm, url = eligible[-1]
    try:
        text = await fetch_pdf_text_layout(session, url)
    except ExtractorError:
        return None
    return parse_dbs_snapshot(text, url, f"{yyyymm[:4]}-{yyyymm[4:]}")


# Re-export the layout extractor for fixture-based tests so they can
# parse a local PDF without going through the network path.
__all__ = [
    "EXTRACTOR",
    "extract_pdf_text_layout",
    "fetch",
    "parse_dbs_snapshot",
    "parse_snapshot",
]


_ECOPOWER_REGIONS = frozenset({REGION_FLANDERS})

EXTRACTOR = SupplierExtractor(
    id="ecopower",
    label="Ecopower",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="variable",
            regions=_ECOPOWER_REGIONS,
        ),
        Contract(
            id=_DBS_CONTRACT_ID,
            label=_DBS_CONTRACT_LABEL,
            kind="dynamic",
            regions=_ECOPOWER_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
