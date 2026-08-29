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

"""Luminus Belgium tariff card extractor.

Luminus publishes the current month's tariff card per (product, region)
through a public REST endpoint:

    https://www.luminus.be/api-next/get-pricelist/
        ?documentSlug=<slug>&energyType=electricity&language=fr
        &tabValue=<Wallonia|Flanders>

Each request returns a fresh PDF (e.g. April 2026 -> 202604 in the
filename). Luminus only sells residential market products in Flanders
and Wallonia; Brussels carries only the regulated Social tariff which
this extractor does not include (auto-assigned, no DSO breakdown).

Energy prices, distribution rows and renewables surcharges all vary
between V and W on every product, so the extractor fetches exactly the
configured region's PDF and never merges. Prices are 6% VAT inclusive
in the printed values; the Dynamic formula is hors TVA so factor and
base are scaled by the parsed VAT multiplier.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

import aiohttp

from ..const import (
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    FLUVIUS_CARD_LABELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    require_contract,
    SIGN_CHARS,
    fetch_pdf_text,
    fetch_text,
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
    FixedRates,
    InjectionRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)

_API_URL = "https://www.luminus.be/api-next/get-pricelist/"

_REGION_TO_TAB: dict[str, str] = {
    REGION_FLANDERS: "Flanders",
    REGION_WALLONIA: "Wallonia",
}


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    slug: str  # Luminus's documentSlug query parameter


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef("luminus_comfy", "Luminus Comfy", "fixed", "comfy"),
    _ContractDef("luminus_comfy_plus", "Luminus Comfy+", "fixed", "comfy-plus"),
    _ContractDef("luminus_comfyflex", "Luminus ComfyFlex", "variable", "comfyflex"),
    _ContractDef(
        "luminus_comfyflex_plus", "Luminus ComfyFlex+", "variable", "comfyflex-plus"
    ),
    _ContractDef("luminus_maxxfix", "Luminus MaxxFix", "fixed", "maxxfix"),
    _ContractDef("luminus_maxxflex", "Luminus MaxxFlex", "variable", "maxxflex"),
    _ContractDef("luminus_basicfix", "Luminus BasicFix", "fixed", "basicfix"),
    _ContractDef("luminus_basicflex", "Luminus BasicFlex", "variable", "basicflex"),
    _ContractDef("luminus_smartflex", "Luminus SmartFlex", "tou", "smartflex"),
    _ContractDef("luminus_dynamic", "Luminus Dynamic", "dynamic", "dynamic"),
    # Luminus Sociaal/Social (regulated CREG tariff) is omitted on purpose:
    # it is auto-assigned to protected customers (not user-selectable) and
    # its PDF carries an all-in regulated price with no DSO breakdown -
    # same reasoning as Engie's Tarif Social.
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}


def _document_url(slug: str, region: str) -> str:
    tab = _REGION_TO_TAB[region]
    return (
        f"{_API_URL}?documentSlug={slug}&energyType=electricity"
        f"&language=fr&tabValue={tab}"
    )


_SITEMAP_URL = "https://www.luminus.be/sitemap.xml"

# Luminus's sitemap exposes one product page per slug under the
# tariffs root, e.g. /fr/particuliers/tarifs-energie/comfyflex/.
_PRODUCT_PAGE_RE = re.compile(
    r"/(?:fr|nl)/particuliers/(?:tarifs-energie|onze-tarieven)/([a-z0-9\-]+)/"
)

# Excluded slugs: regulated tariffs not offered on the residential
# market, plus the parent index pages.
_EXCLUDED_SLUGS = frozenset({"tarif-social", "sociaal-tarief"})


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Discover Luminus products from the public sitemap.

    The /fr/particuliers/tarifs-energie/<slug>/ structure is the
    canonical product directory. Every slug there is a product
    (residential + market only). Excludes the regulated social
    tariff which is not user-selectable.
    """
    try:
        xml = await fetch_text(session, _SITEMAP_URL)
    except ExtractorError:
        return set()
    return {
        slug for slug in _PRODUCT_PAGE_RE.findall(xml) if slug not in _EXCLUDED_SLUGS
    }


# ---- top-level fetch + parser -------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    """Fetch the configured region's PDF for ``contract_id``."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Luminus")
    if region not in _REGION_TO_TAB:
        raise ExtractorError(
            f"Luminus {contract_id}: not available in region {region!r}"
        )
    url = _document_url(contract.slug, region)
    text = await fetch_pdf_text(session, url)
    return parse_snapshot(contract_id, text, region, url)


def parse_snapshot(
    contract_id: str, text: str, region: str, source_url: str = _API_URL
) -> SupplierSnapshot:
    """Pure parser exposed for unit tests."""
    contract = require_contract(_CONTRACTS_BY_ID, contract_id, "Luminus")

    energy = _extract_energy(text, contract.kind)
    injection = _extract_injection(text, contract.kind)
    publication_label = _extract_publication_month(text)
    federal_excise, energy_contribution, connection_fee = _extract_per_kwh_taxes(text)
    energy_fund = _extract_energy_fund(text) if region == REGION_FLANDERS else 0.0

    flanders_renewables = 0.0
    wallonia_renewables = 0.0
    if region == REGION_FLANDERS:
        flanders_renewables = _extract_flanders_renewables(text)
        dsos = _extract_flanders_dsos(text, contract.kind)
    else:
        wallonia_renewables = _extract_wallonia_renewables(text)
        dsos = _extract_wallonia_dsos(text)

    return SupplierSnapshot(
        supplier="luminus",
        contract=contract_id,
        energy=energy,
        dsos=dsos,
        taxes=TaxOverlay(
            federal_excise=federal_excise,
            energy_contribution=energy_contribution,
            flanders_renewables=flanders_renewables,
            wallonia_renewables=wallonia_renewables,
            region_connection_fee=connection_fee,
            energy_fund_eur_per_month=energy_fund,
            vat_rate=0.0,
        ),
        source_url=source_url,
        publication_label=publication_label,
        valid_until=parse_valid_until(text),
        injection=injection,
    )


# ---- energy + tax block -------------------------------------------------------


# Numeric token: digits optionally followed by a single decimal separator
# + digits. Anchors on starting + ending digit so a trailing sentence
# punctuation can't be captured (e.g. '0,1019 x Belpex H + 2,4591.\n'
# from luminus_dynamic_w would otherwise grab the final '.' if the
# regex were the lazier '[\d,.]+').
_NUM = r"\d+(?:[,.]\d+)?"

_DYNAMIC_FORMULA_RE = re.compile(
    rf"Prélèvement\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)
_INJECTION_FORMULA_RE = re.compile(
    rf"Injection\s*\([^)]+\)\s*=\s*({_NUM})\s*x\s*Belpex\s*H\s*([{SIGN_CHARS}])\s*({_NUM})",
    re.S,
)


# A non-dynamic card's feed-in formula, accepted ONLY when that same block
# says the tariff is indexed monthly.
#
# The cadence sentence is the whole point. ComfyFlex and ComfyFlex+ print the
# IDENTICAL "0,0481 x Belpex - 0,6392" and index it QUARTERLY ("Votre tarif
# sera indexe tous les trimestres", against "du 1re trimestre 2026"). Keying on
# the formula would sweep them onto a monthly mean the contract never mentions:
# measured, their printed quarterly figure is 1,9% off the truth while April's
# month mean is 16,3% off it in the other direction, so the "fix" would be
# strictly worse than the lag it replaces.
#
# The leading guard keeps the scan inside the injection block: the SmartFlex
# card carries a second, MaxxFlex-identical block for a non-SMR3 meter.
_INJECTION_MONTHLY_RE = re.compile(
    r"Formule\s+tarifaire\s+de\s+l['\u2019\u00a9]énergie\s+injectée"
    r"(?:(?!Formule\s+tarifaire).)*?"
    rf"=\s*((?P<factor>{_NUM})\s*x\s*Belpex\s*"
    rf"(?P<sign>[{SIGN_CHARS}])\s*(?P<base>{_NUM}))"
    r"(?:(?!Formule\s+tarifaire|Votre\s+tarif\s+sera\s+indexé).){0,400}?"
    r"Votre\s+tarif\s+sera\s+indexé\s+(?:chaque|tous\s+les)\s+mois\b",
    re.S,
)


# MaxxFlex indexes the COMMODITY on the delivery month too: "Le parametre
# d'indexation est base sur la moyenne arithmetique des cotations journalieres
# Day Ahead Belpex Baseload ... pendant le mois de livraison. La valeur Belpex M
# du mois en cours n'est connue qu'a la fin du mois."
#
# That sentence is the gate. ComfyFlex quotes a QUARTERLY index and SmartFlex
# carries a second MaxxFlex-identical block for a non-SMR3 meter, so neither
# may be swept in by the formula shape.
_MONTHLY_ARITHMETIC_RE = re.compile(
    r"moyenne\s+arithm[ée]tique.{0,200}?pendant\s+le\s+mois\s+de\s+livraison",
    re.S,
)
# The ENERGY block only. Scoping matters: searched over the whole document the
# mono pattern also finds the INJECTION formula ("0,0481 x Belpex - 0,6392"),
# and a fixed card would gain an energy formula it does not have.
_ENERGY_FORMULA_BLOCK_RE = re.compile(
    r"Formules\s+tarifaires\s+pour\s+le\s+co[uû]t\s+de\s+l['\u2019\u00a9]\s*[ée]nergie"
    r"(?:(?!Formule\s+tarifaire\s+de).)*",
    re.S,
)


def _band_formula_re(label: str) -> re.Pattern[str]:
    """One per-meter row inside the energy block.

    The two tail guards are load-bearing. ComfyFlex prints a TWO-term formula,
    "x Belpex + 0,0000 x Endex 1-0-3 + 4,2102"; without them ``_NUM``
    backtracks and binds "0,000" as the base, which is how a first cut swept
    the quarterly cards in. A bare ``Belpex`` is also required, so "Belpex M",
    "Belpex RLP M" and the dynamic "Belpex H" cannot match.
    """
    return re.compile(
        rf"{label}\s*=\s*({_NUM})\s*x\s*Belpex\s+"
        rf"([{SIGN_CHARS}])\s*({_NUM})(?![\d,])(?!\s*x)",
        re.S,
    )


_ENERGY_BANDS: tuple[tuple[str, str], ...] = (
    ("single", r"Compteur\s+mono-horaire"),
    ("peak", r"Heures\s+pleines(?:\s*\([^)]*\))?"),
    ("offpeak", r"Heures\s+creuses"),
    ("exclusive_night", r"Exclusif\s+nuit"),
)


def _vat_multiplier(text: str) -> float:
    return vat_multiplier(
        text,
        re.compile(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", re.S),
        r"TVA\s*(\d+)\s*%",
    )


def _extract_yearly_fee(text: str) -> float:
    """Capture the 'Redevance fixe' line.

    Every Luminus residential card the integration covers prints this
    line (~65 EUR for static, ~75 EUR for dynamic). A regex miss is a
    layout drift, not a fee-free contract; raise rather than default to
    0 so the coordinator surfaces the failure instead of silently
    dropping ~70 EUR/year from the user's annual estimate.
    """
    match = re.search(rf"Redevance fixe\s*\(€/an\)\s+({_NUM})", text)
    if match is None:
        raise ExtractorError("Luminus: yearly fee (Redevance fixe) not found")
    return to_float(match.group(1))


def _extract_excl_night_fee(text: str) -> float | None:
    """Yearly fixed fee for an exclusive-night circuit.

    Static / variable cards print the Redevance fixe row with three columns
    (mono | bi | exclusif nuit), e.g. "65,00 65,00 -". The "-" means the
    exclusive-night circuit carries no separate abonnement (0) - it is billed
    once on the main connection - so a second exclusive-night entry must bill
    0, not the standard fee. Returns None when the row has no third column
    (dynamic cards print a single value and don't offer exclusive-night), so
    the standard fee applies.
    """
    match = re.search(
        rf"Redevance fixe\s*\(€/an\)\s+{_NUM}\s+{_NUM}\s+({_NUM}|-)", text
    )
    if match is None:
        return None
    col = match.group(1)
    return 0.0 if col == "-" else to_float(col)


def _extract_energy(text: str, kind: TariffKind) -> EnergyRates:
    fee = _extract_yearly_fee(text)
    if kind == "tou":
        # SmartFlex's TOU table prints exactly three rates on the first
        # "Énergie fournie" row, e.g. "(c€/kWh) 15,54 13,29 6,72". The
        # second occurrence later in the PDF is the bi-horaire fallback
        # for non-SMR3 customers; we anchor on the first match.
        tou_match = re.search(
            rf"Énergie fournie\s*\(c€/kWh\)\s+({_NUM})\s+({_NUM})\s+({_NUM})(?!\s+\d)",
            text,
        )
        if not tou_match:
            raise ExtractorError("could not parse Luminus TOU energy block")
        peak = to_float(tou_match.group(1)) / 100.0
        transition = to_float(tou_match.group(2)) / 100.0
        offpeak = to_float(tou_match.group(3)) / 100.0
        # SmartFlex's three bands (pleines / creuses / super-creuses) use
        # SEASONAL windows: peak (pleines) is 07-11 + 17-22 all year, the
        # cheapest super-creuses band applies 11-17 only in spring/summer
        # (21/03-20/09), and 22-07 is always creuses. The weekend_rule
        # "smartflex_seasonal" tells pricing.tou_slot to bill those windows
        # (the "free Sundays" first-year promo is not modelled).
        tou_rates = TimeOfUseRates(
            peak=peak,
            transition=transition,
            offpeak=offpeak,
            yearly_fixed_fee=fee,
            weekend_rule="smartflex_seasonal",
        )
        # SmartFlex indexes each band on the delivery month, same sentence as
        # its bi-hourly siblings: "Votre tarif sera indexe tous les mois. La
        # valeur Belpex du mois en cours n'est connue qu'a la fin du mois."
        # So the printed triplet is the previous month's.
        coefs = _monthly_tou_coefficients(text)
        if not coefs:
            return tou_rates
        return replace(
            tou_rates,
            month_indexed=True,
            formula_factor_peak=coefs["peak"][0],
            formula_base_peak=coefs["peak"][1],
            formula_factor_transition=coefs["transition"][0],
            formula_base_transition=coefs["transition"][1],
            formula_factor_offpeak=coefs["offpeak"][0],
            formula_base_offpeak=coefs["offpeak"][1],
        )

    if kind == "dynamic":
        match = _DYNAMIC_FORMULA_RE.search(text)
        if not match:
            raise ExtractorError("could not parse Luminus dynamic formula")
        factor_pdf = to_float(match.group(1))
        base_pre_vat_cents = parse_sign(match.group(2)) * to_float(match.group(3))
        vat = _vat_multiplier(text)
        # PDF formula: c€/kWh hors TVA = factor_pdf * Belpex_eur_mwh + base_cents.
        # Spot in EUR/kWh = Belpex_eur_mwh / 1000. Convert to:
        #   factor_eur_kwh = factor_pdf * vat * 1000 / 100 = factor_pdf * vat * 10
        #   base_eur_kwh   = base_cents  * vat / 100
        return DynamicRates(
            factor=factor_pdf * vat * 10.0,
            base=base_pre_vat_cents * vat / 100.0,
            yearly_fixed_fee=fee,
        )

    energy_match = re.search(
        rf"Énergie fournie\s*\(c€/kWh\)\s+({_NUM})\s+({_NUM})\s+({_NUM})\s+({_NUM})",
        text,
    )
    if not energy_match:
        raise ExtractorError(f"could not parse Luminus {kind} energy block")
    mono = to_float(energy_match.group(1)) / 100.0
    peak = to_float(energy_match.group(2)) / 100.0
    offpeak = to_float(energy_match.group(3)) / 100.0
    excl_night = to_float(energy_match.group(4)) / 100.0

    excl_night_fee = _extract_excl_night_fee(text)
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=fee,
            yearly_fixed_fee_exclusive_night=excl_night_fee,
        )
    rates = VariableRates(
        current=mono,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=excl_night,
        yearly_fixed_fee=fee,
        yearly_fixed_fee_exclusive_night=excl_night_fee,
    )
    coefs = _monthly_energy_coefficients(text)
    if not coefs:
        return rates
    none2: tuple[float | None, float | None] = (None, None)
    return replace(
        rates,
        month_indexed=True,
        formula_factor=coefs["single"][0],
        formula_base=coefs["single"][1],
        formula_factor_peak=coefs.get("peak", none2)[0],
        formula_base_peak=coefs.get("peak", none2)[1],
        formula_factor_offpeak=coefs.get("offpeak", none2)[0],
        formula_base_offpeak=coefs.get("offpeak", none2)[1],
        formula_factor_exclusive_night=coefs.get("exclusive_night", none2)[0],
        formula_base_exclusive_night=coefs.get("exclusive_night", none2)[1],
    )


_TOU_BANDS: tuple[tuple[str, str], ...] = (
    ("peak", r"Pr[ée]l[èe]vement\s+Heures\s+pleines"),
    ("transition", r"Pr[ée]l[èe]vement\s+Heures\s+creuses"),
    ("offpeak", r"Pr[ée]l[èe]vement\s+Heures\s+super[\s-]*creuses"),
)


# "(valeur de l'indice de mars 2026)" against ComfyFlex's "(valeur de l'indice
# du 1re trimestre 2026)". A MONTH attribution is the discriminator that works
# on the SmartFlex card, whose energy block carries no cadence sentence of its
# own: the sentence sits under the injection block and governs that tariff.
_MONTHLY_INDEX_ATTRIBUTION_RE = re.compile(
    r"valeur\s+de\s+l['\u2019\u00a9]indice\s+de\s+(?!\s*\d)\w+", re.IGNORECASE
)


def _monthly_tou_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """The three SmartFlex per-slot monthly coefficients, or ``{}``.

    Gated on the energy block attributing its index to a MONTH, which is what
    separates this card from ComfyFlex's quarterly one, and scoped to that
    block so the card's second MaxxFlex-identical section (the non-SMR3
    fallback) cannot supply them.

    All three bands are required. A partial match would leave one slot on the
    printed rate and the others on the index, which is worse than either.
    """
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}
    if _MONTHLY_INDEX_ATTRIBUTION_RE.search(block.group(0)) is None:
        return {}
    vat = _vat_multiplier(text)
    out: dict[str, tuple[float, float]] = {}
    for key, label in _TOU_BANDS:
        match = _band_formula_re(label).search(block.group(0))
        if match is None:
            continue
        out[key] = (
            to_float(match.group(1)) * 10.0 * vat,
            parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0 * vat,
        )
    return out if len(out) == len(_TOU_BANDS) else {}


def _monthly_energy_coefficients(text: str) -> dict[str, tuple[float, float]]:
    """Per-meter ``(factor, base)`` for a card that indexes energy monthly.

    Empty unless the card carries the arithmetic-mean sentence AND its energy
    block yields a mono row. Both halves matter: ComfyFlex has the block but
    quotes a QUARTERLY index, SmartFlex has a MaxxFlex-identical block for a
    non-SMR3 meter but not the sentence, and a fixed card has neither.
    """
    if _MONTHLY_ARITHMETIC_RE.search(text) is None:
        return {}
    block = _ENERGY_FORMULA_BLOCK_RE.search(text)
    if block is None:
        return {}
    # The row prints c/kWh HTVA against an index in EUR/MWh, while the energy
    # row itself is TVAC, so both coefficients take the x10 / 100 conversion
    # and the VAT multiplier. Round-trips to the printed 14,41 at the card's
    # own 92,61 index.
    vat = _vat_multiplier(text)
    out: dict[str, tuple[float, float]] = {}
    for key, label in _ENERGY_BANDS:
        match = _band_formula_re(label).search(block.group(0))
        if match is None:
            continue
        out[key] = (
            to_float(match.group(1)) * 10.0 * vat,
            parse_sign(match.group(2)) * to_float(match.group(3)) / 100.0 * vat,
        )
    return out if "single" in out else {}


def _extract_publication_month(text: str) -> str:
    # The first page usually says e.g. "Luminus Comfy Electricité (avril 2026)".
    # The May 2026 cards started padding the inside of the parens with a
    # trailing space ("(mai 2026 )"), so tolerate optional whitespace
    # against future-similar formatting drift.
    match = re.search(
        r"\(\s*([a-zA-Zéèû]+\s+\d{4})\s*\)",
        text,
    )
    return match.group(1) if match else ""


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    # Anchor on the applicable "Tarif de l'énergie injectée" row, not the
    # "Estimation annuelle du tarif de l'énergie injectée" forecast
    # printed just below it (footnote: a 12-month estimate, not the rate
    # in force). The two share the "de l'énergie injectée" tail, but only
    # the applicable row capitalises "Tarif" (the estimate has lowercase
    # "tarif" after "du"), so a case-sensitive "Tarif" binds to the
    # applicable rate. Mirrors the consumption side, which deliberately
    # takes the current month over the annual estimate.
    #
    # Some cards print a footnote digit right after the unit
    # ("(c€/kWh)2 3,81"); skip an optional digit-then-whitespace before
    # the value. Use \s+ between every word: the row wraps mid-phrase
    # ("Tarif de l'énergie \ninjectée").
    indicative = re.search(
        rf"Tarif\s+de\s+l[\"'’©]énergie\s+injectée"
        rf"[^0-9-]*(?:\d+\s+)?({_NUM})",
        text,
        re.S,
    )
    current = to_float(indicative.group(1)) / 100.0 if indicative else None

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    month_indexed = False
    if kind == "dynamic":
        match = _INJECTION_FORMULA_RE.search(text)
        if match:
            factor_pdf = to_float(match.group(1))
            base_pdf_cents = parse_sign(match.group(2)) * to_float(match.group(3))
            # Residential injection is VAT-exempt in Belgium.
            factor = factor_pdf * 10.0
            base = base_pdf_cents / 100.0
            formula = match.group(0)
    else:
        # A non-dynamic card indexes the credit on the delivery month and says
        # the printed figure is not it: "Votre tarif sera indexe tous les mois.
        # La valeur Belpex du mois en cours n'est connue qu'a la fin du mois.
        # Les prix affiches sont calcules sur la base de la derniere valeur
        # Belpex connue (mois precedent)."
        monthly = _INJECTION_MONTHLY_RE.search(text)
        if monthly is not None:
            # c/kWh per EUR/MWh of index, and the block is HTVA with the card
            # noting "La TVA s'eleve a 0%", so nothing is grossed.
            factor = to_float(monthly.group("factor")) * 10.0
            base = (
                parse_sign(monthly.group("sign"))
                * to_float(monthly.group("base"))
                / 100.0
            )
            formula = " ".join(monthly.group(1).split())
            month_indexed = True

    if current is None and factor is None:
        # Both Luminus card families always publish injection: the
        # applicable indicative on fixed/variable/TOU cards, the spot
        # formula on dynamic. A miss is a layout drift, not a fee-free
        # contract; fail loud rather than silently crediting an injection
        # user nothing, mirroring the consumption helpers that raise.
        raise ExtractorError("Luminus: could not parse injection rate")
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        month_indexed=month_indexed,
    )


def _tax_block_values(text: str) -> list[str]:
    """Return the contiguous ['-', '5,0329', ...] run after the tax labels.

    The 'Taxes et redevances' section prints every label first then the
    matching values on their own lines, in the same order:

      [labels]
        Cotisation Fonds énergie (€/mois)
            Basse tension non résidentiel
            Basse tension résidentiel
        Droit d'accise spécial (c€/kWh)
        Cotisation sur l'énergie (c€/kWh)
        Redevance de raccordement (c€/kWh)        # Wallonia only
      [values]
        BTNR
        BTR
        Excise
        Cotisation
        Redevance                                  # Wallonia only

    Each value sits alone on its line - that's what tells us where the
    value list ends and the footnotes begin (the footnotes start with
    '(*) ...' and intermix numbers with text on the same line).
    """
    # 'Taxes et redevances' is mentioned twice in every PDF: once in the
    # 'Composition du prix' legend (no colon, no region) and once for the
    # actual tax table (`3 Taxes et redevances : WAL/FL`). Anchor on the
    # colon to only match the second one.
    block = re.search(
        r"3 Taxes et redevances\s*:\s*(?:WAL|FL|BRU).+?"
        r"(?=INFORMATION SUR VOTRE TARIF|Conditions\b)",
        text,
        re.S,
    )
    if not block:
        return []
    return re.findall(rf"^\s*(-|{_NUM})\s*$", block.group(0), re.MULTILINE)


def _extract_per_kwh_taxes(text: str) -> tuple[float, float, float]:
    """Return (federal_excise, energy_contribution, connection_fee) in EUR/kWh.

    Federal excise + energy contribution are mandatory across regions;
    Walloon connection fee is mandatory in Wallonia (the
    'Redevance de raccordement' label is present iff the card is a
    Wallonia card). Raise on a layout drift that would otherwise zero
    out the regulated tax silently and underbill ~50 EUR/year per
    missed tier.
    """
    values = _tax_block_values(text)

    def _decimal(s: str | None) -> float:
        if s is None or s == "-":
            return 0.0
        return to_float(s) / 100.0

    if len(values) < 4:
        raise ExtractorError(
            f"Luminus: 'Taxes et redevances' block too short ({len(values)} values; "
            "expected ≥4 BTNR / BTR / excise / contribution)"
        )
    excise = _decimal(values[2])
    contribution = _decimal(values[3])
    has_connection = "Redevance de raccordement" in text
    if has_connection and len(values) < 5:
        raise ExtractorError(
            "Luminus: Walloon connection-fee row missing from tax block"
        )
    connection = _decimal(values[4]) if has_connection else 0.0
    return excise, contribution, connection


def _extract_energy_fund(text: str) -> float:
    """Pick the BTR (Basse tension résidentiel) value from the tax block.

    Flanders prints BTNR (non-residential) first then BTR (residential);
    the integration's residential users want BTR. A '-' means no fee.
    """
    values = _tax_block_values(text)
    if len(values) < 2 or values[1] == "-":
        return 0.0
    return to_float(values[1])


def _extract_flanders_renewables(text: str) -> float:
    """Flanders splits renewables across green energy + cogeneration.

    Layout:
        Coûts énergie verte (c€/kWh)
        Coûts cogénération (c€/kWh)
        FL
        <green>
        <cogen>

    Caller gates on REGION_FLANDERS so a miss is a layout drift, not a
    'no levy on this card' case. Raise rather than silently zero.
    """
    match = re.search(
        rf"Coûts énergie verte.*?Coûts cogénération.*?FL\s*\n?\s*"
        rf"({_NUM})\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match:
        return (to_float(match.group(1)) + to_float(match.group(2))) / 100.0
    # Some fixed cards may print only the green-energy line.
    fallback = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?FL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if fallback is None:
        raise ExtractorError(
            "Luminus: Flanders renewables (Coûts énergie verte) row not found"
        )
    return to_float(fallback.group(1)) / 100.0


def _extract_wallonia_renewables(text: str) -> float:
    """Mandatory in Wallonia (caller gates on REGION_WALLONIA); raise on
    miss rather than silently zero out."""
    match = re.search(
        rf"Coûts énergie verte\s*\(c€/kWh\)[^A-Z]*?WAL\s*\n?\s*({_NUM})",
        text,
        re.S,
    )
    if match is None:
        raise ExtractorError(
            "Luminus: Wallonia renewables (Coûts énergie verte) row not found"
        )
    return to_float(match.group(1)) / 100.0


# ---- DSO row parsers ----------------------------------------------------------


_FLANDERS_LABELS = FLUVIUS_CARD_LABELS


def _extract_flanders_dsos(text: str, kind: TariffKind) -> dict[str, DsoOverlay]:
    """Read the Compteur digital columns from the Flanders DSO table.

    Static cards print 8 numbers per row (digital + classic + prosumer):
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal c€/kWh |
      dist_excl_night | capacity_classic €/yr | dist_classic_normal |
      dist_classic_excl | prosumer €/kW/yr

    Dynamic (SMR3) cards omit the analog-meter and prosumer columns and
    print only 4 numbers:
      data_mgmt €/an | capacity_digital €/kW/yr | dist_normal | dist_excl_night

    Distribution already includes transport (same convention as Engie's
    Flanders rows).
    """
    # The dynamic product meters quarter-hourly (SMR3); its data-management
    # fee is the reduced value in the "(**) ... quart d'heure ... gestion
    # des donnees" footnote, not the table's monthly-regime column. Fall
    # back to the table value if the footnote is absent.
    quarter_data_mgmt: float | None = None
    if kind == "dynamic":
        footnote = re.search(
            r"quart d['’]heure[\s\S]{0,80}?gestion des donn[\s\S]{0,40}?([\d,]+)\s*€",
            text,
        )
        if footnote is not None:
            quarter_data_mgmt = to_float(footnote.group(1))

    out: dict[str, DsoOverlay] = {}
    for label, key in _FLANDERS_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+((?:{_NUM}\s+){{3,}}{_NUM})",
            text,
            re.IGNORECASE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        if len(nums) < 4:
            continue
        prosumer: float | None = nums[7] if len(nums) >= 8 else None
        out[key] = DsoOverlay(
            distribution_single=nums[2] / 100.0,
            distribution_exclusive_night=nums[3] / 100.0,
            transport=0.0,
            data_management_per_year=(
                quarter_data_mgmt if quarter_data_mgmt is not None else nums[0]
            ),
            capacity_eur_per_kw_year=nums[1],
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_WALLONIA_LABELS: dict[str, str] = {
    "AIEG": DSO_AIEG,
    "AIESH": DSO_AIESH,
    "ORES (Brabant Wallon)": DSO_ORES,
    "TECTEO RESA": DSO_RESA,
    "WAVRE": DSO_REW,
}


def _extract_wallonia_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read Wallonia DSO rows.

    Static rows have 7 numbers:
      mono | pleines | creuses | excl_nuit | transport | data_mgmt | prosumer
    Dynamic rows have 9:
      mono | pleines | creuses | ECO | MEDIUM | PIC | excl_nuit |
      transport | data_mgmt
    The IMPACT triplet (ECO/MEDIUM/PIC) is unique to dynamic; its
    presence flips the prosumer column off (SMR3 has no compensation
    regime).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _WALLONIA_LABELS.items():
        row = re.search(
            rf"{re.escape(label)}\s+((?:{_NUM}\s+){{6,}}{_NUM})",
            text,
            re.IGNORECASE,
        )
        if not row:
            continue
        nums = [to_float(n) for n in row.group(1).split()]
        eco = medium = pic = None
        if len(nums) >= 9:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            # Luminus prints ECO | MEDIUM | PIC in ascending order
            # (different from OCTA+/Bolt where the columns are PIC
            # first, descending). Map to the schema's distribution_*.
            eco, medium, pic = nums[3], nums[4], nums[5]
            excl_night = nums[6]
            transport = nums[7]
            data_mgmt = nums[8]
            prosumer: float | None = None
        elif len(nums) >= 7:
            mono, pleines, creuses = nums[0], nums[1], nums[2]
            excl_night = nums[3]
            transport = nums[4]
            data_mgmt = nums[5]
            prosumer = nums[6]
        else:
            continue
        out[key] = DsoOverlay(
            distribution_single=mono / 100.0,
            distribution_peak=pleines / 100.0,
            distribution_offpeak=creuses / 100.0,
            distribution_exclusive_night=excl_night / 100.0,
            distribution_pic=pic / 100.0 if pic is not None else None,
            distribution_medium=medium / 100.0 if medium is not None else None,
            distribution_eco=eco / 100.0 if eco is not None else None,
            transport=transport / 100.0,
            data_management_per_year=data_mgmt,
            prosumer_eur_per_kva_year=prosumer,
        )
    return out


_LUMINUS_REGIONS = frozenset({REGION_FLANDERS, REGION_WALLONIA})

# Contracts whose card indexes the feed-in credit MONTHLY, so the credit needs
# ENTSO-E spots their own energy leg does not fetch. ComfyFlex and ComfyFlex+
# are absent on purpose: they print the same formula and index it quarterly,
# and there is no quarterly mean here to resolve it against. Dynamic collects
# its key through its own Belpex H energy formula.
_MONTHLY_INJECTION_CONTRACTS: frozenset[str] = frozenset(
    {
        "luminus_comfy",
        "luminus_comfy_plus",
        "luminus_maxxfix",
        "luminus_maxxflex",
        "luminus_basicfix",
        "luminus_basicflex",
        "luminus_smartflex",
    }
)


EXTRACTOR = SupplierExtractor(
    sweep_cost_s=1.1,
    id="luminus",
    label="Luminus",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=_LUMINUS_REGIONS,
            spot_indexed_injection=c.contract_id in _MONTHLY_INJECTION_CONTRACTS,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
)
