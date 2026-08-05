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

"""Cociter (Wallonian citizen cooperative) tariff extractor.

Cociter publishes monthly tariff cards under predictable filenames at
https://www.cociter.be/electricite/cartes-tarifaires/:

    RCVar_YMR_Coop-YYMM-fr.pdf   - variable contract (BELIX-indexed)
    RCDyn_SM3_Coop-YYMM-fr.pdf   - dynamic contract (quarter-hourly BELPEX)

YYMM is e.g. ``2604`` for April 2026. Each card includes the energy
formula plus the full DSO + tax overlay for every Wallonian DSO Cociter
serves (AIEG, AIESH, ORES, RESA, REW). All values are VAT-inclusive.

Cociter only sells in Wallonia.
"""

from __future__ import annotations

import re
from datetime import date

import aiohttp

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    REGION_WALLONIA,
    WALLONIA_DSO_KEYS,
)
from ._pdf import (
    FR_MONTHS,
    SIGN_CHARS,
    archive_validity_check,
    fetch_pdf_text,
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
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

_INDEX_URL = "https://www.cociter.be/electricite/cartes-tarifaires/"

# French month names Cociter prints in the validity header. Used by
# fetch_for_month to confirm a CDN-served PDF actually mentions the
# requested month when parse_valid_until missed.
_FR_MONTHS = FR_MONTHS


# Cociter's current monthly publication patterns. The 4-digit group is YYMM.
#
# The optional ``-<n>`` before ``.pdf`` is WordPress's dedup suffix: it appends
# ``-1``, ``-2``, ... when a file is re-uploaded under a name that already
# exists, and Cociter's site does exactly that (July 2026's dynamic card is
# published as ``RCDyn_SM3_Coop-2607-fr-1.pdf``). Requiring ``-fr.pdf`` to
# follow the month immediately dropped that month from the archive silently:
# the year-to-date walk fell back to the current card for July instead.
_VAR_RE = re.compile(
    r'href="(https?://[^"]*RCVar_YMR_Coop-(\d{4})-fr(?:-\d+)?\.pdf)"', re.IGNORECASE
)
_DYN_RE = re.compile(
    r'href="(https?://[^"]*RCDyn_SM3_Coop-(\d{4})-fr(?:-\d+)?\.pdf)"', re.IGNORECASE
)

# Cociter prints one row per Wallonian DSO it serves; the labels are the
# uppercase strings that anchor each row in the PDF (case-sensitive). The
# registry key on the right is what the rest of the integration uses. The
# set of keys must equal WALLONIA_DSO_KEYS — if Cociter starts (or stops)
# serving a Wallonian DSO, update both this map and const.WALLONIA_DSO_KEYS
# in lockstep so the snapshot's overlays cover every selectable DSO.
_DSO_KEY: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES": DSO_ORES,
    "RESA": DSO_RESA,
    "REW": DSO_REW,
}
_DSO_LABELS = tuple(_DSO_KEY)
assert set(_DSO_KEY.values()) == set(WALLONIA_DSO_KEYS), (
    "Cociter DSO map drifted from const.WALLONIA_DSO_KEYS"
)

_CONTRACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "cociter_variable": _VAR_RE,
    "cociter_dynamic": _DYN_RE,
}


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
) -> SupplierSnapshot:
    """Fetch + parse Cociter's latest published card for ``contract_id``."""
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        raise ExtractorError(f"unknown Cociter contract {contract_id!r}")

    pdf_url, label = await _find_latest(session, pattern)
    text = await fetch_pdf_text(session, pdf_url)
    return parse_snapshot(text, contract_id, pdf_url, label)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
    year_month: date,
) -> SupplierSnapshot | None:
    """Fetch the Cociter card for a specific (year, month).

    Cociter's listing keeps every monthly card linked under the same
    page. We fetch it once, find the URL whose YYMM suffix matches the
    requested year_month, and parse. Returns None when the listing
    doesn't list the month, the URL 404s, or the PDF doesn't parse -
    the coordinator falls back to the current snapshot as a proxy.
    """
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        return None
    target_yymm = f"{year_month.year % 100:02d}{year_month.month:02d}"
    try:
        html = await fetch_text(session, _INDEX_URL)
    except ExtractorError:
        return None
    # A month can appear twice when Cociter re-uploads its card: WordPress
    # keeps the original and adds "-1" to the newcomer, so the suffixed URL is
    # the NEWER file. Take the highest suffix rather than the first match, or
    # a re-upload correcting the original would be ignored.
    candidates = [url for url, yymm in pattern.findall(html) if yymm == target_yymm]
    if not candidates:
        return None
    # Newest first, but fall through to an older edition when it does not
    # parse. A re-upload is not always an improvement: July 2026's dynamic
    # card was republished with the index renamed from "QUARTER HOURLY BELPEX"
    # to "15 MIN BELPEX", the meter labels dropped and the injection prose
    # moved BELOW its own formula. Taking only the newest lost that month from
    # the archive walk outright, and the walk then billed July at the current
    # card's overlays -- while the original, which parses and agrees with the
    # August card, was still served at its unsuffixed URL.
    # Last resort, after every LISTED edition has failed to parse: the
    # original the re-upload displaced. WordPress keeps it at the unsuffixed
    # URL and Cociter keeps serving it, but drops the link, so no ordering
    # over the listing can reach it. Derived from the same "-N" convention
    # _dedup_rank reads, and only ever fetched once the listed ones are out.
    ordered = sorted(candidates, key=_dedup_rank, reverse=True)
    ordered += [
        stripped
        for url in ordered
        if (stripped := re.sub(r"-\d+(\.pdf)$", r"\1", url)) != url
        and stripped not in candidates
    ]
    for pdf_url in ordered:
        try:
            text = await fetch_pdf_text(session, pdf_url)
            snap = parse_snapshot(
                text, contract_id, pdf_url, _yymm_to_label(target_yymm)
            )
        except ExtractorError:
            continue
        return archive_validity_check(snap, text, year_month, month_names=_FR_MONTHS)
    return None


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,  # noqa: ARG001 - Cociter only sells in Wallonia.
) -> str | None:
    """Cheap freshness probe: latest URL for ``contract_id`` from the index.

    Cociter's listing returns no Last-Modified or ETag, so we GET it and
    return the latest matching PDF URL. The URL embeds YYMM so any
    monthly rotation flips the probe key.
    """
    pattern = _CONTRACT_PATTERNS.get(contract_id)
    if pattern is None:
        return None
    try:
        pdf_url, _ = await _find_latest(session, pattern)
    except ExtractorError:
        return None
    return pdf_url


# Family prefix on Cociter's listing -> our registry contract id.
_DISCOVER_FAMILIES = {
    "RCVar_YMR": "cociter_variable",
    "RCDyn_SM3": "cociter_dynamic",
}


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return contract ids visible in Cociter's monthly card index.

    Cociter's listing publishes one PDF per (family, month). Map the
    family prefix (RCVar_YMR / RCDyn_SM3) back to our contract id and
    surface anything else verbatim — that's the new-product signal.
    """
    try:
        html = await fetch_text(session, _INDEX_URL)
    except ExtractorError:
        return set()
    out: set[str] = set()
    for family in re.findall(
        r"(RC[A-Za-z]+_[A-Za-z0-9]+)_Coop-\d+-(?:fr|nl)(?:-\d+)?\.pdf", html
    ):
        out.add(_DISCOVER_FAMILIES.get(family, family))
    return out


def parse_snapshot(
    text: str, contract_id: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    energy = _extract_energy(text, contract_id)
    return SupplierSnapshot(
        supplier="cociter",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos(text),
        taxes=_extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=_extract_injection(text),
        supplier_prosumer_eur_per_kva_year=_extract_supplier_prosumer(
            text, contract_id
        ),
    )


def _extract_supplier_prosumer(text: str, contract_id: str) -> float | None:
    """Cociter Variable's supplier-side compensation-regime PV forfait.

    The variable card bills, on top of the DSO "Tarif prosumer" column, a
    supplier forfait "Forfait panneaux photovoltaiques (en regime de
    compensation)" defined in footnote (6) as "37,10 EUR/kVA/an TVAC". The
    dynamic SMR3 card dispenses with the compensation regime, so it carries
    no such forfait.

    The value is already TVAC and must NOT be VAT-scaled. Anchor on the
    "EUR/kVA/an TVAC" footnote wording, which is unique to this forfait (the
    DSO prosumer column header is the bare "(EUR/kVA/an)"). Every Cociter
    variable card prints it, so a miss is a layout drift; raise rather than
    silently drop it, the same way the injection and tax parsers fail loud.
    """
    if contract_id != "cociter_variable":
        return None
    match = re.search(r"([\d,]+)\s*€/kVA/an\s*TVAC", text)
    if not match:
        raise ExtractorError("could not parse Cociter compensation-regime PV forfait")
    return to_float(match.group(1))


def _extract_injection(text: str) -> InjectionRates:
    """Parse Cociter's injection formula.

    The variable PDF prints ``(0,097 x BELPEX – 2,1)`` (hourly, hTVA).
    The dynamic PDF prints ``(0,097 x QUARTER HOURLY BELPEX – 2,1)``.
    Injection is VAT-exempt for residential.

    Both Cociter products always publish an injection formula, so a miss
    is a layout drift, not a fee-free contract; raise rather than return
    None (which the coordinator would treat as a zero credit), the same
    way the taxes parser fails loud. This keeps last-good data and
    surfaces the breakage in the logs and live-check.

    The dynamic card carries two Compteur SMR3 formulas (consumption
    first, injection later); anchor the regex on the ``Le prix de
    l'injection`` lead-in so the second formula is the one that
    matches even when both sides use the same sign character.

    Accept any of ``+ - ‒ – — U+2212`` between the BELPEX factor and
    the base: Cociter prints en-dash today, but a sign flip (or a
    swap to Unicode minus) shouldn't silently drop the rate. The
    sign is captured and applied to the base instead of being
    hardcoded.
    """
    formula = re.search(
        rf"Le\s+prix\s+de\s+l['‘’ʼ]\s*injection.*?"
        # The meter-type label in front of the formula is prose Cociter
        # rewords: "Tout compteur", "Compteur SMR3", and from the August
        # 2026 card "Compteur pouvant effectuer des mesures par quart
        # d'heure". Match any Compteur label up to the formula's opening
        # bracket rather than enumerating them, so the next rewording
        # doesn't take the injection block offline again. The label stays
        # REQUIRED though: made optional, this pattern matches the
        # CONSUMPTION formula on a card that prints its injection prose after
        # its injection formula, and billed a 1,03 x +3 feed-in.
        rf"(?:Tout\s+)?[Cc]ompteur[^\n(]*"
        rf"\(([\d,]+)\s*x\s*(?:QUARTER\s*HOURL\s*Y\s*|15\s*MIN\s*)?BELPEX\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)\)",
        text,
        re.S,
    )
    if formula is None:
        # Variable PDF has no anchor prose around the injection block;
        # fall back to the first formula on any Compteur line.
        formula = re.search(
            rf"(?:Tout\s+)?[Cc]ompteur[^\n(]*"
            rf"\(([\d,]+)\s*x\s*(?:QUARTER\s*HOURL\s*Y\s*|15\s*MIN\s*)?BELPEX\s*"
            rf"([{SIGN_CHARS}])\s*([\d,]+)\)",
            text,
        )
    if not formula:
        raise ExtractorError("could not parse Cociter injection formula")
    factor_pdf = to_float(formula.group(1))
    base_pdf_cents = parse_sign(formula.group(2)) * to_float(formula.group(3))
    return InjectionRates(
        current=None,
        factor=factor_pdf * 10.0,
        base=base_pdf_cents / 100.0,
        formula=formula.group(0),
    )


def _extract_energy(text: str, contract_id: str) -> EnergyRates:
    yearly_fee_match = re.search(r"(\d+,\d+)\s*€/an\s*\n?\s*TVAC", text)
    if yearly_fee_match is None:
        # The abonnement (53,00 EUR/an TVAC) is on every Cociter card; a
        # miss silently drops the standing charge, so fail loud like the
        # injection / tax / forfait parsers rather than default to 0.
        raise ExtractorError("Cociter: yearly fixed fee (abonnement) row not found")
    yearly_fee = to_float(yearly_fee_match.group(1))

    if contract_id == "cociter_variable":
        mono = re.search(r"Compteur monohoraire[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        peak = re.search(r"Heures pleines[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        offpeak = re.search(r"Heures creuses[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        excl = re.search(r"Compteur exclusif nuit[^\n]*?(\d+,\d+)\s*c€/kWh", text)
        if not mono:
            raise ExtractorError(
                "could not parse Cociter variable monohoraire indicative rate"
            )
        # Accept any sign between BELIX and the base, mirroring the
        # dynamic path, so a card flipping to a Unicode minus or a
        # negative base still renders the diagnostic formula string.
        formula = re.search(
            rf"Compteur monohoraire\s*\(([\d,]+)\s*x\s*BELIX\s*"
            rf"([{SIGN_CHARS}])\s*([\d,]+)\)\s*\+\s*(\d+)\s*%\s*TVA",
            text,
        )
        # Surface the numeric BELIX coefficients so a signing cohort can be
        # re-priced against the current month's mean spot. Same conversion as
        # the dynamic path: BELIX is the monthly mean of Belpex in EUR/MWh, so
        # factor_pdf * BELIX gives c€/kWh -> factor * spot(EUR/kWh) needs
        # * VAT * 10, and the c€ base needs * VAT / 100. BELIX equals the plain
        # arithmetic monthly mean the coordinator already computes, so this is
        # exact.
        formula_factor: float | None = None
        formula_base: float | None = None
        if formula:
            vat_mult = 1.0 + to_float(formula.group(4)) / 100.0
            formula_factor = to_float(formula.group(1)) * vat_mult * 10.0
            formula_base = (
                parse_sign(formula.group(2))
                * to_float(formula.group(3))
                * vat_mult
                / 100.0
            )
        return VariableRates(
            current=to_float(mono.group(1)) / 100.0,
            peak=to_float(peak.group(1)) / 100.0 if peak else None,
            offpeak=to_float(offpeak.group(1)) / 100.0 if offpeak else None,
            exclusive_night=to_float(excl.group(1)) / 100.0 if excl else None,
            yearly_fixed_fee=yearly_fee,
            formula=(
                f"({formula.group(1)} x BELIX {formula.group(2)} "
                f"{formula.group(3)}) c€/kWh + {formula.group(4)}% VAT"
                if formula
                else None
            ),
            formula_factor=formula_factor,
            formula_base=formula_base,
        )

    # cociter_dynamic
    # Cociter's formula always ends with "+ N% TVA" right after the parens;
    # capture N so the conversion follows whatever VAT the PDF actually applies.
    # Accept the full SIGN_CHARS set between factor and base so a future card
    # with a Unicode minus or a negative base doesn't dead-end the parser.
    # The index name and the "Compteur SMR3" prefix are both variable. The
    # July 2026 re-upload prints "(0,103 x 15 MIN BELPEX + 3) + 6% TVA" with
    # no prefix, where every other card says "Compteur SMR3 (0,103 x QUARTER
    # HOURL Y BELPEX + 3)". Same index, same coefficients, renamed: pinning
    # the old spelling lost that month from the archive walk entirely, and the
    # walk then billed July at the CURRENT card's overlays.
    formula = re.search(
        rf"(?:Compteur SMR3\s*)?\(([\d,]+)\s*x\s*"
        rf"(?:QUARTER\s*HOURL\s*Y|15\s*MIN)\s*BELPEX\s*"
        rf"([{SIGN_CHARS}])\s*([\d,]+)\)\s*\+\s*(\d+)\s*%\s*TVA",
        text,
    )
    if not formula:
        raise ExtractorError("could not parse Cociter dynamic formula")
    factor_pdf = to_float(formula.group(1))
    base_pre_vat_cents = parse_sign(formula.group(2)) * to_float(formula.group(3))
    vat_multiplier = 1.0 + to_float(formula.group(4)) / 100.0
    # PDF formula yields c€/kWh from BELPEX in €/MWh; convert to EUR/kWh
    # against spot already in EUR/kWh: factor *= vat_mult * 10, base = base_c * vat_mult / 100.
    # Cociter Dynamique bills on the quarter-hourly BELPEX spot (the
    # card's "QUARTER HOURLY BELPEX" formula), so keep the native
    # 15-minute slots like Engie rather than the hourly mean.
    return DynamicRates(
        factor=factor_pdf * vat_multiplier * 10.0,
        base=base_pre_vat_cents * vat_multiplier / 100.0,
        yearly_fixed_fee=yearly_fee,
        quarter_hourly=True,
    )


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Parse the per-DSO row of the Cociter tariff card.

    The variable card has 6 numbers per row:
        yearly | mono | dag | nacht | uitsl_nacht | tarif_prosumer
    The dynamic (SMR3) card has 8, with the prosumer column replaced by
    three Tarif Impact columns (PIC / MEDIUM / ECO) since SMR3 dispenses
    with the compensation regime.

    The first 6 columns are positionally identical between the two cards,
    but column 6 means different things. We discriminate by looking for
    the literal table header "Tarif prosumer" in the document - this is
    robust against future column additions and avoids the previous
    end-of-line anchor that would silently lose the prosumer value if a
    7th column were ever added to the variable card.
    """
    transport = _extract_transport(text)
    has_prosumer_column = "Tarif prosumer" in text
    out: dict[str, DsoOverlay] = {}
    for label in _DSO_LABELS:
        # Variable card: 6 numbers (last column = prosumer).
        # Dynamic card: 8 numbers (last 3 columns = PIC | MEDIUM | ECO).
        row = re.search(
            rf"^{label}\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)\s+([\d,]+)"
            rf"\s+([\d,]+)(?:\s+([\d,]+)\s+([\d,]+))?",
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        prosumer_rate = to_float(row.group(6)) if has_prosumer_column else None
        pic = medium = eco = None
        if not has_prosumer_column and row.group(7) and row.group(8):
            pic = to_float(row.group(6)) / 100.0
            medium = to_float(row.group(7)) / 100.0
            eco = to_float(row.group(8)) / 100.0
        out[_DSO_KEY[label]] = DsoOverlay(
            distribution_single=to_float(row.group(2)) / 100.0,
            distribution_peak=to_float(row.group(3)) / 100.0,
            distribution_offpeak=to_float(row.group(4)) / 100.0,
            distribution_exclusive_night=to_float(row.group(5)) / 100.0,
            distribution_pic=pic,
            distribution_medium=medium,
            distribution_eco=eco,
            transport=transport,
            data_management_per_year=to_float(row.group(1)),
            prosumer_eur_per_kva_year=prosumer_rate,
        )
    return out


def _extract_transport(text: str) -> float:
    # The ELIA transport row (~2.7-3.2 c€/kWh, ~20% of the all-in) is on
    # every Cociter card and feeds straight into network cost; a regex
    # miss silently dropping it would under-bill every kWh. Fail loud,
    # matching the renewables / tax parsers.
    match = re.search(r"Tarifs de transport TVAC[^\n]*?([\d,]+)", text)
    if match is None:
        raise ExtractorError("Cociter: ELIA transport tariff row not found")
    return to_float(match.group(1)) / 100.0


def _extract_taxes(text: str) -> TaxOverlay:
    # The energy block labels the renewable contribution with quoted text:
    #   "énergies renouvelables" ... TVAC <X> c€/kWh
    # PDFs use straight "..." or curly “…” depending on the export; accept any
    # adjacent quote glyph and require the literal heading near the number to
    # avoid silently grabbing some other 'TVAC ... c€/kWh' value.
    renewables = re.search(
        r"[\"'“”«»]?\s*énergies renouvelables"
        r"[\"'“”«»]?.{0,200}?TVAC\s*([\d,]+)\s*c€/kWh",
        text,
        re.S,
    )

    # The "Taxes et redevances" block lists three values on one line:
    #   Cotisation énergie | Droit d'accises spécial | Redevance de raccordement
    # Anchor on the literal label trio so a future footnote/inserted
    # number above the values can't be mistaken for the row.
    taxes_block = re.search(
        r"Cotisation énergie.*?"
        r"Droit d'accises spécial.*?"
        r"Redevance de raccordement.*?"
        r"([\d,]+)\s+([\d,]+)\s+([\d,]+)",
        text,
        re.S,
    )
    if not taxes_block:
        raise ExtractorError("could not parse Cociter taxes block")
    if not renewables:
        # The Walloon green-energy contribution is ~3 c€/kWh and is
        # mandatory on every Cociter card; a regex miss is a layout
        # drift that would silently zero it.
        raise ExtractorError(
            "could not parse Cociter Walloon renewables (énergies renouvelables)"
        )

    energy_contrib = to_float(taxes_block.group(1)) / 100.0
    federal_excise = to_float(taxes_block.group(2)) / 100.0
    connection_fee = to_float(taxes_block.group(3)) / 100.0

    # Cociter only operates in Wallonia; Flanders renewables stay at 0.
    return TaxOverlay(
        federal_excise=federal_excise,
        energy_contribution=energy_contrib,
        wallonia_renewables=to_float(renewables.group(1)) / 100.0,
        region_connection_fee=connection_fee,
        vat_rate=0.0,
    )


def _dedup_rank(url: str) -> int:
    """WordPress's re-upload counter, or 0 for the original.

    Cociter's site keeps the original when a card is re-uploaded and adds
    "-1", "-2", ... to the newcomer, so the highest suffix is the current
    file. Every path that resolves a card has to rank on this, not on
    listing order: the index is not ordered by it.
    """
    m = re.search(r"-(\d+)\.pdf$", url)
    return int(m.group(1)) if m else 0


async def _find_latest(
    session: aiohttp.ClientSession, pattern: re.Pattern[str]
) -> tuple[str, str]:
    html = await fetch_text(session, _INDEX_URL)
    matches = pattern.findall(html)
    if not matches:
        raise ExtractorError(f"no matching tariff card linked at {_INDEX_URL}")
    # Rank on (month, re-upload counter). Sorting on the month alone left
    # ties in listing order, so a month carrying both the original and a
    # correction resolved to whichever came last in the HTML. fetch_for_month
    # already ranked on the counter, so the live card and the archived one
    # for the SAME month could be different files -- and when the index lists
    # the newest first, the live path served the superseded one.
    matches.sort(key=lambda m: (m[1], _dedup_rank(m[0])))
    url, yymm = matches[-1]
    label = _yymm_to_label(yymm)
    return url, label


def _yymm_to_label(yymm: str) -> str:
    """Convert ``2604`` -> ``2026-04``."""
    if len(yymm) == 4 and yymm.isdigit():
        return f"20{yymm[:2]}-{yymm[2:]}"
    return yymm


_COCITER_REGIONS = frozenset({REGION_WALLONIA})

EXTRACTOR = SupplierExtractor(
    id="cociter",
    label="Cociter",
    contracts=(
        Contract(
            id="cociter_variable",
            label="Cociter Tarif Variable",
            kind="variable",
            regions=_COCITER_REGIONS,
            # Variable energy, but the injection is an hourly BELPEX
            # formula with no fixed indicative -> needs an ENTSO-E spot.
            spot_indexed_injection=True,
        ),
        Contract(
            id="cociter_dynamic",
            label="Cociter Tarif Dynamique",
            kind="dynamic",
            regions=_COCITER_REGIONS,
        ),
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)
