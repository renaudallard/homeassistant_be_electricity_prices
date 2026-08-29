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

"""energie.be dynamic and variable tariff extractors.

energie.be publishes its tariff cards as PDFs. The dynamic residential card
(Elektriciteit dynamisch tarief particulier) is served at one stable document
API URL whose content is replaced monthly; a GET 302-redirects to the versioned
Azure blob and aiohttp follows it, so that fetch is the single-URL DATS 24
shape with no archive and no cheap probe.

The variable card (Elektriciteit particulier online) has no such document key.
Its URL is resolved from the site's own contracts endpoint, which names the
current PDF per product. The sibling document key that looks like it should
serve it, ``?key=Tariffs``, is a DEAD LEGACY LINK: it still answers 200 with a
card from April 2024 carrying the pre-merger 10-area Fluvius naming and two
years of superseded network tariffs, and only the site footer still points at
it. It must never be used as a fallback. Today it would fail rather than
mis-bill - its older layout prints the formulas unparenthesised and in EUR/MWh,
so four mandatory rows raise and only 4 of its 10 DSO rows read - but that is
the layout's doing, not a safeguard, and the DSO rows it DOES parse are the
2024 ones.

The dynamic card bundles a residential and a professional block in one PDF;
only the residential ("particulier") section is parsed. The variable card
publishes the professional block as a separate PDF, so its residential cut is
a no-op. Unlike Frank Energie and Bolt, energie.be prints its energy and
injection formulas against Belpex in c€/kWh (not EUR/MWh), so the spot
coefficient is NOT scaled by 10.

Region: Flanders only (all 8 Fluvius sub-areas).
"""

from __future__ import annotations

import json
import logging
import re

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
    flanders_tax_overlay,
    SIGN_CHARS,
    fetch_pdf_text_layout,
    fetch_text,
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
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_LOGGER = logging.getLogger(__name__)

# The document API 302-redirects this key to the current month's blob PDF;
# the content is replaced in place, so the URL is stable across months.
_CARD_URL = (
    "https://energie-production-api.azurewebsites.net"
    "/api/v1/data/document?key=DynamicTariffs"
)

# The site's Angular front end reads its own origin for the API, and the
# azurewebsites host behind it answers 401 on this path, so the www host is
# the address, not a convenience mirror. The JSON names the CURRENT PDF for
# every product; the blob it points at is replaced in place each month.
_CONTRACTS_URL = "https://www.energie.be/api/v1/data/contracts"

_CONTRACT_ID = "energiebe_dynamic"
_CONTRACT_LABEL = "energie.be Dynamisch"
_VARIABLE_ID = "energiebe_variable"
_VARIABLE_LABEL = "energie.be Variabel"
_FIXED_ID = "energiebe_fixed"
_FIXED_LABEL = "energie.be Vast"
_ENERGIEBE_REGIONS = frozenset({REGION_FLANDERS})
_VALID_IDS = frozenset({_CONTRACT_ID, _VARIABLE_ID, _FIXED_ID})
# The contracts API's own tariffType per contract id; the residential
# electricity document of that entry is the card.
_TARIFF_TYPE = {_VARIABLE_ID: "Variable", _FIXED_ID: "Fixed"}

# Belgian residential electricity VAT. energie.be quotes its energy and
# injection formulas "(excl. BTW)" while stating every other value on the
# card is VAT-inclusive, so only the energy leg is scaled to the same
# VAT-inclusive basis (vat_rate stays 0.0, matching Frank). Verified against
# the card's own printed price: 11,93 c€/kWh (incl. VAT) equals
# (1,04 x 10,34 + 0,50) x 1,06. The card's only printed percentage (21% on
# energiedelen) is unrelated, so the rate is a constant, not scraped.
_VAT_MULT = 1.06

# Only the residential block is priced; the card appends a professional block
# whose GSC/WKK, taxes and DSO rows differ. Cut at the professional section
# header so no professional row leaks into a residential snapshot.
_PROF_MARKER = "dynamisch tarief professioneel"

# Row label prefix (regex-safe, unique) -> DSO key. The card wraps two long
# labels across the number row ("Fluvius (Halle-\n<numbers>\nVilvoorde)" and
# the Midden-Vlaanderen row), so each entry anchors on the leading token and
# grabs the four digital-meter columns that follow, wherever they land.
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

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = NUM_NO_THOUSANDS

# The dynamic card indexes on the bare "Belpex", the variable one on the
# monthly "Belpex_RLP"; the formula is otherwise printed identically, so one
# pattern reads both and the caller decides what shape to build from it.
#
# The parameter name is CAPTURED, not just tolerated, because it is the only
# thing in the text that says which product this card is for. Accepting either
# spelling for either contract means a card served at the wrong URL parses
# silently into the other product's coefficients - a dynamic entry billing the
# variable formula against the per-slot spot. This supplier already serves one
# stale card at a legacy key, so a mixed-up URL is not hypothetical, and the
# failure is a wrong price rather than a missing one.
_ENERGY_RE = re.compile(
    rf"formule\s*\(excl\.?\s*BTW\)\s*:?\s*"
    rf"\(\s*{_NUM}\s*x\s*Belpex(?P<rlp>_RLP)?\s*([{SIGN_CHARS}])\s*{_NUM}\)",
    re.IGNORECASE,
)
# The unit label "(c€/kWh)" is interleaved between "de formule:" and the
# parenthesised injection formula, so anchor on the section header both cards
# share and skip to the first "(factor x Belpex +/- base)" that follows. The
# body wording differs ("injectievergoeding" on the dynamic card,
# "terugleververgoeding" on the variable one) and the header does not.
_INJECTION_RE = re.compile(
    rf"Terugleveringsvergoeding.*?"
    rf"(\(\s*{_NUM}\s*x\s*Belpex(?P<spp>_SPP)?\s*([{SIGN_CHARS}])\s*{_NUM}\))",
    re.IGNORECASE | re.DOTALL,
)
# The fixed card prints a rate where the other two print a formula. The column
# label is identical on all three, so the rate alone cannot say which card this
# is; _extract_fixed_energy pairs it with the wording below and with the
# ABSENCE of an indexation formula.
_FIXED_ENERGY_RE = re.compile(rf"Energieprijs\s+{_NUM}", re.IGNORECASE)
_VASTE_PRIJS_RE = re.compile(r"energieprijs\s+is\s+een\s+vaste\s+prijs", re.IGNORECASE)

# The printed monthly indicative, left of the formula in its own column. The
# sign is captured: this formula (0,60 x Belpex_SPP - 0,80) prints NEGATIVE
# whenever Belpex_SPP falls below 1,33 c€/kWh, and the lowest value in
# energie.be's own published table is 1,65. A sign-blind pattern would not
# mis-price such a card, it would fail to read it at all and take the whole
# contract offline - the opposite of what InjectionRates and _validate_injection
# both say a monthly indicative is allowed to do.
_INJECTION_CURRENT_RE = re.compile(
    rf"Zonnestroom\s+([{SIGN_CHARS}]?)\s*{_NUM}", re.IGNORECASE
)
# The fee's number is followed by its "(EUR/jaar)" unit, which the dynamic card
# puts on the next line and the variable card puts on the next line too - but
# with a wrapped sentence of body text in between, on the number's own line. So
# allow the rest of that line and AT MOST one newline before the unit.
# The unit itself stays `\([^)]*jaar`, matching the tax regexes below: the same
# cards render the sibling energy-fund unit as "(€/maand )" with a stray space,
# and pinning the € glyph exactly would take both contracts offline over a
# renderer quirk, since a missing fee is fatal.
_FEE_RE = re.compile(
    rf"Vaste\s+vergoeding\s+{_NUM}[^\n]*\n?\s*\([^)]*jaar", re.IGNORECASE
)
_GSC_RE = re.compile(rf"\bGSC\b\s+{_NUM}")
_WKK_RE = re.compile(rf"\bWKK\b\s+{_NUM}")
_EXCISE_RE = re.compile(
    rf"Bijzondere\s+accijns\s+op\s+Energie\s*\([^)]*\)\s*\*{{0,2}}\s*{_NUM}"
)
_CONTRIB_RE = re.compile(rf"Bijdrage\s+op\s+de\s+Energie\s*\([^)]*\)\s*{_NUM}")
# Anchored on "Residentieel" right after "Energiefonds"; the sibling
# "Niet-Residentieel" row (VAT-exempt, non-residential) cannot match.
_FUND_RE = re.compile(
    rf"Bijdrage\s+Energiefonds\s+Residentieel\s*\([^)]*\)\s*\*?\s*{_NUM}"
)
_LABEL_RE = re.compile(
    r"particulier\s+online\s*[–\-]\s*([A-Za-z]+)\s*(20\d{2})", re.IGNORECASE
)


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    if contract_id not in _VALID_IDS:
        raise ExtractorError(f"unknown energie.be contract {contract_id!r}")
    if region != REGION_FLANDERS:
        raise ExtractorError("energie.be only operates in Flanders")
    url = _CARD_URL
    if contract_id in _TARIFF_TYPE:
        url = await _resolve_card_url(session, _TARIFF_TYPE[contract_id])
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(text, url, contract_id)


async def _resolve_card_url(session: aiohttp.ClientSession, tariff_type: str) -> str:
    """URL of the current residential card for ``tariff_type``, from the
    contracts API ("Variable" or "Fixed"; the dynamic product has its own
    document key and does not come through here).

    Raises rather than falling back to any other card. The obvious fallback,
    the ``?key=Tariffs`` document key, still serves an April 2024 card whose
    DSO table lists sub-areas that no longer exist. It does not currently
    parse - four mandatory rows fail on its older layout - but that is an
    accident of the layout, not a safeguard: the day it re-templates, a
    fallback would start billing two-year-old network tariffs in silence.
    Being offline for a tick is the better failure.

    Every shape the endpoint can answer with is funnelled into ExtractorError.
    Callers catch that and nothing else, so a stray TypeError from a payload
    that is well-formed JSON but the wrong shape would escape the extractor
    contract and surface as an unhandled error instead of a failed fetch.
    """
    body = await fetch_text(session, _CONTRACTS_URL, timeout=15)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as err:
        raise ExtractorError(f"energie.be contracts API parse error: {err}") from err
    contracts = payload.get("contracts") if isinstance(payload, dict) else None
    if not isinstance(contracts, list):
        raise ExtractorError(
            "energie.be contracts API parse error: no 'contracts' list in the response"
        )
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        if contract.get("tariffType") != tariff_type:
            continue
        residential = contract.get("contractTypeElRes")
        url = (
            residential.get("tariffDocument") if isinstance(residential, dict) else None
        )
        # A non-string here would be str()-ed into a nonsense URL and fetched;
        # treat it as a missing card instead.
        if isinstance(url, str) and url.startswith("https://"):
            return url
    raise ExtractorError(
        f"energie.be: no residential {tariff_type} card in contracts API"
    )


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    text: str,
    source_url: str,
    contract_id: str = _CONTRACT_ID,
    publication_label: str = "",
) -> SupplierSnapshot:
    section = _residential(text)
    variable = contract_id == _VARIABLE_ID
    fixed = contract_id == _FIXED_ID
    if fixed:
        energy: EnergyRates = _extract_fixed_energy(section)
    elif variable:
        energy = _extract_variable_energy(section)
    else:
        energy = _extract_energy(section)
    return SupplierSnapshot(
        supplier="energiebe",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(section),
        taxes=_extract_taxes(section),
        source_url=source_url,
        publication_label=publication_label or _publication_label(section),
        valid_until=parse_valid_until(section),
        injection=(
            _extract_monthly_injection(section)
            if (variable or fixed)
            else _extract_injection(section)
        ),
    )


def _residential(text: str) -> str:
    """Return the residential slice, dropping the professional block."""
    cut = text.find(_PROF_MARKER)
    return text[:cut] if cut > 0 else text


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return f"{m.group(1).lower()} {m.group(2)}" if m else ""


def _extract_energy(text: str) -> DynamicRates:
    m = _ENERGY_RE.search(text)
    if not m:
        raise ExtractorError("could not parse energie.be energy formula")
    if m.group("rlp"):
        # This is the variable card. Its coefficients are a MONTHLY index;
        # applied to the per-slot spot they would mis-price every hour.
        raise ExtractorError(
            "energie.be: dynamic contract served a Belpex_RLP (variable) card"
        )
    factor_pdf = to_float(m.group(1))
    base_cents = parse_sign(m.group(3)) * to_float(m.group(4))
    # energie.be prints Belpex in c€/kWh (not EUR/MWh like Frank / Bolt) and
    # quotes the formula excl. BTW. ENTSO-E spot is EUR/kWh, and
    # Belpex_c€/kWh = spot_EUR/kWh * 100, so:
    #   price_c€/kWh (excl VAT)  = factor_pdf * (spot * 100) + base_cents
    #   price_EUR/kWh (incl VAT) = (factor_pdf * spot + base_cents / 100) * VAT
    # => factor = factor_pdf * VAT ; base = base_cents / 100 * VAT  (no * 10).
    factor = factor_pdf * _VAT_MULT
    base = base_cents / 100.0 * _VAT_MULT
    return DynamicRates(
        factor=factor,
        base=base,
        yearly_fixed_fee=_yearly_fee(text),
        quarter_hourly=True,
    )


def _extract_variable_energy(text: str) -> SpotMonthlyRates:
    """The variable card's monthly-indexed energy leg.

    The card prices a delivery month at ``factor x Belpex_RLP + base``, where
    Belpex_RLP is that month's RLP-weighted mean day-ahead price - a
    SpotMonthlyRates leg, resolved against the running monthly mean of the
    ENTSO-E curve (a close, few-percent approximation of the RLP weighting,
    same as every other monthly-indexed card here).

    The resolved price the card prints alongside the formula is deliberately
    NOT read. Unlike the realized "maandprijs" other variable cards publish,
    energie.be prints the VNR twelve-month FORECAST of the index: the July
    2026 card showed 13,13 c€/kWh where the month settled at 14,41 on a
    realized Belpex_RLP of 11,42. Billing it would ship a knowingly wrong
    rate that no later tick corrects.
    """
    m = _ENERGY_RE.search(text)
    if not m:
        raise ExtractorError("could not parse energie.be variable energy formula")
    if not m.group("rlp"):
        # The bare "Belpex" is the dynamic card's per-slot index. Resolving it
        # against a monthly mean would bill this contract on the wrong axis.
        raise ExtractorError(
            "energie.be: variable contract served a card without Belpex_RLP"
        )
    factor_pdf = to_float(m.group(1))
    base_cents = parse_sign(m.group(3)) * to_float(m.group(4))
    # Same c€/kWh basis and VAT treatment as the dynamic leg (see
    # _extract_energy): no * 10 rescale, energy grossed to VAT-inclusive.
    return SpotMonthlyRates(
        factor=factor_pdf * _VAT_MULT,
        base=base_cents / 100.0 * _VAT_MULT,
        yearly_fixed_fee=_yearly_fee(text),
    )


def _extract_fixed_energy(text: str) -> FixedRates:
    """The fixed card's flat rate.

    One rate for every meter type: the card prints no peak / offpeak or
    exclusive-night column, so a bi-hourly customer bills the same number and
    the pricing engine's fallback to ``single`` is correct rather than a
    silent approximation.

    NO VAT multiplier here, unlike the other two products. Their formulas are
    labelled "(excl. btw)" and have to be grossed; this column is not, and the
    card's header says every price on it is VAT-inclusive unless marked. The
    same 18,26 read through _VAT_MULT would bill 19,36.
    """
    if _ENERGY_RE.search(text):
        # An indexation formula means this is the variable or dynamic card.
        raise ExtractorError(
            "energie.be: fixed contract served an indexed (variable/dynamic) card"
        )
    if not _VASTE_PRIJS_RE.search(text):
        raise ExtractorError(
            "energie.be: fixed contract card does not print a 'vaste prijs'"
        )
    m = _FIXED_ENERGY_RE.search(text)
    if not m:
        raise ExtractorError("could not parse energie.be fixed energy price")
    return FixedRates(
        single=to_float(m.group(1)) / 100.0,
        yearly_fixed_fee=_yearly_fee(text),
    )


def _extract_monthly_injection(text: str) -> InjectionRates:
    """The variable and fixed cards' injection row. Both print the same
    formula and both settle on it.

    A FIXED energy leg does not make the feed-in credit fixed: the card says
    the compensation "wordt geindexeerd op basis van de Belpex_SPP parameter",
    and that the invoiced amount follows the index of the month being billed,
    so the credit is monthly-indexed on both contracts. The fixed contract
    therefore declares ``spot_indexed_injection`` and offers the same
    optional, skippable ENTSO-E key that Cociter Variable does; without a key
    the printed indicative below is credited instead.

    The formula indexes on Belpex_SPP, the SOLAR-weighted monthly mean, while
    the energy leg indexes on Belpex_RLP. ``spp_indexed`` says so, which makes
    the coordinator resolve this formula against the SPP-weighted mean and
    refuse to resolve it against any other: July 2026 settled at 6,34 c€/kWh
    SPP against 11,42 RLP, so the energy leg's mean would pay 6,05 where the
    contract owes 3,00.

    ``current`` is the card's printed Zonnestroom column. It is itself derived
    from the VNR forecast rather than the realized month (2,77 printed against
    3,00 realized for July 2026), so it is the fallback, not the answer - used
    only for the cold start and whenever the Synergrid profile is unavailable,
    where it beats both no credit at all and a wrong-index one.
    """
    formula = _INJECTION_RE.search(text)
    current = _INJECTION_CURRENT_RE.search(text)
    if formula and not formula.group("spp"):
        raise ExtractorError(
            "energie.be: variable injection is not indexed on Belpex_SPP"
        )
    if not formula or not current:
        # Every card prints a terugleveringsvergoeding; a miss is a layout
        # drift, not a fee-free contract. Raise rather than silently credit
        # a solar user 0 EUR/kWh.
        raise ExtractorError("energie.be: injection indicative row not found")
    # Injection is VAT-exempt residential, so the printed c€/kWh converts
    # straight to EUR/kWh, and Belpex is in c€/kWh here too (no * 10). The
    # sign is honoured on the indicative: at a low enough Belpex_SPP this
    # formula settles negative and the producer pays to inject.
    factor_pdf = to_float(formula.group(2))
    base_cents = parse_sign(formula.group(4)) * to_float(formula.group(5))
    rates: dict[str, object] = {
        "current": parse_sign(current.group(1)) * to_float(current.group(2)) / 100.0,
        "formula": formula.group(1),
    }
    rates["factor"] = factor_pdf
    rates["base"] = base_cents / 100.0
    rates["spp_indexed"] = True
    return InjectionRates(**rates)  # type: ignore[arg-type]


def _yearly_fee(text: str) -> float:
    fee = _FEE_RE.search(text)
    if fee is None:
        # The vaste vergoeding standing charge is mandatory; fail loud rather
        # than silently bill a zero yearly fee on a layout drift.
        raise ExtractorError("energie.be: vaste vergoeding row not found")
    return to_float(fee.group(1))


def _extract_injection(text: str) -> InjectionRates:
    m = _INJECTION_RE.search(text)
    if not m:
        # Every dynamic card prints a terugleveringsvergoeding formula; a miss
        # is a layout drift, not a fee-free contract. Raise rather than
        # silently credit a solar user 0 EUR/kWh.
        raise ExtractorError("energie.be: injection formula row not found")
    # On this card BOTH formulas print the bare "Belpex", so the only thing
    # separating them is that the injection section sits below the energy one.
    # That has held on every card seen, but nothing enforced it, and binding
    # the energy row here would credit a solar user the CONSUMPTION rate.
    energy_m = _ENERGY_RE.search(text)
    if energy_m is not None and m.start(1) < energy_m.end():
        raise ExtractorError("energie.be: injection formula matched the energy row")
    factor_pdf = to_float(m.group(2))
    base_cents = parse_sign(m.group(4)) * to_float(m.group(5))
    # Injection is VAT-exempt. Belpex is in c€/kWh here too, so
    # factor = factor_pdf and base = base_cents / 100 (no * 10, no VAT).
    return InjectionRates(
        factor=factor_pdf,
        base=base_cents / 100.0,
        formula=m.group(1),
    )


def _extract_taxes(text: str) -> TaxOverlay:
    """Every value on the card is VAT-inclusive.

    The contribution row was mandatory here while Frank and EnergyVision had
    already made it optional for the 2026-08-01 abolition, so energie.be would
    have gone offline the moment its card dropped the row the way theirs did.
    The shared helper holds that policy now.
    """
    return flanders_tax_overlay(
        text,
        supplier="energie.be",
        excise=(_EXCISE_RE,),
        renewables=(_GSC_RE, _WKK_RE),
        contribution=_CONTRIB_RE,
        fund=_FUND_RE,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find("Nettarieven")
    if start < 0:
        raise ExtractorError("could not locate energie.be DSO table")
    # The residential block only. The dynamic card carries a SECOND
    # "Nettarieven" table, the professional one, and it prints its own
    # maximumtarief HTVA (0,3276168 against the residential 0,3472738). A
    # whole-document search hands a residential entry a ceiling 6% low.
    nxt = text.find("Nettarieven", start + 1)
    section = text[start:] if nxt < 0 else text[start:nxt]
    # "Voor digitale meters geldt een maximumtarief van 0,3472738 EUR/kWh
    # (excl. databeheer)." pdfplumber renders it "maxi mumtarief" on the two
    # August cards and "maximumtarief" on the July dynamic one, so the space
    # is optional or only one of the three generations matches. Stored as
    # printed, on this card's own basis.
    ceiling_match = re.search(
        r"maxi\s*mumtarief\s+van\s+" + _NUM + r"\s*EUR/kWh", section, re.IGNORECASE
    )
    ceiling = to_float(ceiling_match.group(1)) if ceiling_match else None
    out: dict[str, DsoOverlay] = {}
    for prefix, key in _DSO_ROWS:
        row = re.search(
            rf"Fluvius\s*\(\s*{re.escape(prefix)}[^\d]*"
            rf"{_NUM}\s+{_NUM}\s+{_NUM}\s+{_NUM}",
            section,
            re.IGNORECASE,
        )
        if not row:
            continue
        databeheer = to_float(row.group(1))
        capacity = to_float(row.group(2))
        normal = to_float(row.group(3)) / 100.0
        excl_night = to_float(row.group(4)) / 100.0
        out[key] = DsoOverlay(
            distribution_single=normal,
            distribution_exclusive_night=excl_night,
            transport=0.0,
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
            network_ceiling_eur_per_kwh=ceiling,
        )
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=18.6,
    id="energiebe",
    label="energie.be",
    contracts=(
        Contract(
            id=_CONTRACT_ID,
            label=_CONTRACT_LABEL,
            kind="dynamic",
            regions=_ENERGIEBE_REGIONS,
        ),
        # "spot_monthly", not "variable": the card resolves the month's price
        # from a monthly index it names, and prints only a forecast of that
        # index. The kind is what makes the config flow collect an ENTSO-E
        # key, which this contract needs to price at all.
        Contract(
            id=_VARIABLE_ID,
            label=_VARIABLE_LABEL,
            kind="spot_monthly",
            regions=_ENERGIEBE_REGIONS,
        ),
        # The only one of the three whose card prints a rate rather than a
        # formula for ENERGY, so it needs no key to price consumption. Its
        # INJECTION is the same monthly Belpex_SPP formula the variable card
        # carries, hence the optional key: skip it and the credit falls back
        # to the card's printed indicative.
        Contract(
            id=_FIXED_ID,
            label=_FIXED_LABEL,
            kind="fixed",
            regions=_ENERGIEBE_REGIONS,
            spot_indexed_injection=True,
        ),
    ),
    fetch=fetch,
)


__all__ = ["EXTRACTOR", "fetch", "parse_snapshot"]
