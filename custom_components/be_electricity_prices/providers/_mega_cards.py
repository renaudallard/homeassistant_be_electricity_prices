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

"""Mega card parsers: the energy leg, the injection leg and the card's dates.

Split out of ``mega.py``, which was the largest module in the package. These
are the parsers that read a Mega tariff card's own product figures -- the
formula coefficients, the realized-rate sentence, the per-meter rates, the
yearly fee, the publication month and the validity date. The grid and levy
overlays live in ``_mega_overlays.py``; ``mega.py`` keeps the URL resolution,
the archive and ``parse_snapshot``, which calls into both.

No behaviour change: every function here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

import re
from datetime import date

from ._pdf import (
    FR_MONTHS,
    SIGN_CHARS,
    end_of_month,
    parse_sign,
    to_float,
    vat_multiplier,
)
from .base import (
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    ImpactRates,
    InjectionRates,
    TariffKind,
    VariableRates,
)

# Mega prints two distinct formulas in every Dynamic PDF:
#   - Consumption: "...la formule tarifaire suivante : Day Ahead ... * X + Y c€/kWh"
#     (TVAC; spot is in c€/kWh and result is in c€/kWh)
#   - Injection:   "...la formule suivante (HTVA) : Day Ahead ... * X - Y c€/kWh"
#     (HTVA but injection is VAT-exempt residential, so no scaling needed)
_FORMULA_TAIL = (
    r"Day Ahead [Ee][Pp][Ee][Xx]\s*[Ss][Pp][Oo][Tt](?:\s*Belgium)?\s*\*\s*"
    # Accept dot or comma decimals for the factor and base: pypdf already
    # extracts the energy table on these cards with dot decimals, so a
    # re-render of the formula as "* 1.05 + 1.35" must not dead-end the
    # Dynamic snapshot. to_float normalises either separator.
    rf"([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh"
)
_CONSUMPTION_FORMULA_RE = re.compile(
    r"formule tarifaire suivante[^*]+?" + _FORMULA_TAIL, re.S
)
_INJECTION_FORMULA_RE = re.compile(
    r"formule suivante\s*\(HTVA\)[^*]+?" + _FORMULA_TAIL, re.S
)


def _parse_formula(match: re.Match[str] | None) -> tuple[float, float] | None:
    if match is None:
        return None
    factor = to_float(match.group(1))
    base_cents = parse_sign(match.group(2)) * to_float(match.group(3))
    return factor, base_cents / 100.0


# Variable (Flex) cards print the indexation as prose (HTVA), e.g.
# "Compteur mono-horaire : Epex * 1,1095 + 3,6 c€/kWh". Unlike the dynamic
# formula this is ex-VAT, so it is grossed by the card's VAT below. Epex is in
# c€/kWh (same as the dynamic formula) so the factor maps directly to spot in
# EUR/kWh with no /MWh unit conversion.
_VARIABLE_MONO_FORMULA_RE = re.compile(
    rf"Compteur mono-horaire\s*:\s*Epex\s*\*\s*([\d.,]+)\s*"
    rf"([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
    re.IGNORECASE,
)
# The same sentence continues with the bi-hourly bands, which carry their own
# factors: "bi-horaire heures pleines : Epex * 1,3275 + 3,6 c€/kWh" and
# "heures creuses : Epex * 0,94 + 3,6 c€/kWh" against the mono 1,1095.
_VARIABLE_BAND_FORMULA_RES: dict[str, re.Pattern[str]] = {
    "peak": re.compile(
        rf"heures pleines\s*:\s*Epex\s*\*\s*([\d.,]+)\s*"
        rf"([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
        re.IGNORECASE,
    ),
    "offpeak": re.compile(
        rf"heures creuses\s*:\s*Epex\s*\*\s*([\d.,]+)\s*"
        rf"([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
        re.IGNORECASE,
    ),
}


def _impact_band_formula_re(band: str) -> re.Pattern[str]:
    """Per-band Impact formula matcher.

    Case-insensitive on the label because the card mixes "Tarif ECO" with a
    lower-case "tarif PIC" in the same sentence.
    """
    return re.compile(
        rf"tarif\s+{band}\s*:\s*Epex\s*\*\s*([\d.,]+)\s*"
        rf"([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
        re.IGNORECASE,
    )


_IMPACT_BAND_RES = {
    band: _impact_band_formula_re(band) for band in ("pic", "medium", "eco")
}


def _variable_cohort_coefficients(
    text: str, *, professional: bool = False
) -> tuple[float | None, float | None]:
    """Numeric coefficients of the variable indexation formula, or
    ``(None, None)``.

    On a residential card the numbers are baked to the TVAC EUR/kWh basis the
    snapshot carries (``vat_rate`` 0). A professional card is published Hors
    TVA and its snapshot carries ``vat_rate`` 0,21, so its coefficients stay
    ex-VAT and the entry's own VAT preference resolves them later.

    That distinction has to be explicit. ``vat_multiplier`` falls back to the
    residential 1,06 when its pattern misses, and a professional card never
    prints "TVA N% incluse" (it prints "Hors TVA"), so the shared call baked
    6% into an ex-VAT formula and inflated a pro entry's whole energy leg.

    The Epex index is the monthly RLP-weighted spot; the coordinator applies
    these against the plain arithmetic monthly mean (a close, few-percent
    approximation).
    """
    match = _VARIABLE_MONO_FORMULA_RE.search(re.sub(r"\s+", " ", text))
    if match is None:
        return None, None
    vat_mult = _cohort_vat_multiplier(text, professional=professional)
    factor = to_float(match.group(1)) * vat_mult
    base = parse_sign(match.group(2)) * to_float(match.group(3)) * vat_mult / 100.0
    return factor, base


def _cohort_vat_multiplier(text: str, *, professional: bool) -> float:
    """Basis multiplier for a cohort formula: 1 ex-VAT, the card's rate else."""
    if professional:
        return 1.0
    return vat_multiplier(text, re.compile(r"TVA\s*(\d+)\s*%\s*incluse", re.I))


def _variable_band_coefficients(
    text: str, *, professional: bool = False
) -> dict[str, tuple[float, float]]:
    """Per-band coefficients of the variable indexation formula.

    Mega prints one formula per meter in the same sentence as the mono one,
    and the bands differ by a fifth: mono ``Epex x 1,1095 + 3,6``, peak
    ``x 1,3275 + 3,6``, off-peak ``x 0,94 + 3,6``. A signing cohort on a
    bi-hourly meter was re-priced onto the mono pair for every hour, which
    over-charges its peak hours and under-charges its off-peak ones.

    Same basis conversion as the mono formula. Empty for a card that prints a
    single formula, and the mono pair then applies to every meter.
    """
    flat = re.sub(r"\s+", " ", text)
    vat_mult = _cohort_vat_multiplier(text, professional=professional)
    out: dict[str, tuple[float, float]] = {}
    for band, pattern in _VARIABLE_BAND_FORMULA_RES.items():
        match = pattern.search(flat)
        if match is None:
            continue
        out[band] = (
            to_float(match.group(1)) * vat_mult,
            parse_sign(match.group(2)) * to_float(match.group(3)) * vat_mult / 100.0,
        )
    return out


def _impact_band_coefficients(
    text: str, *, professional: bool = False
) -> dict[str, float | None]:
    """Per-band coefficients of the Impact indexation formulas.

    Same basis conversion as :func:`_variable_cohort_coefficients`, and for
    the same reason: the formula block is printed Hors TVA in c€/kWh while a
    residential card's resolved band rates are TVAC EUR/kWh. Verified against
    the real Walloon card, where inverting all three bands on the TVAC
    reading yields one consistent Epex (8,47 c€/kWh) while the ex-VAT reading
    does not.

    Missing or unparseable bands come back ``None`` individually, so a card
    that prints only some of them still contributes what it has.
    """
    flat = re.sub(r"\s+", " ", text)
    vat_mult = (
        1.0
        if professional
        else vat_multiplier(text, re.compile(r"TVA\s*(\d+)\s*%\s*incluse", re.I))
    )
    out: dict[str, float | None] = {}
    for band, pattern in _IMPACT_BAND_RES.items():
        match = pattern.search(flat)
        if match is None:
            out[f"{band}_factor"] = None
            out[f"{band}_base"] = None
            continue
        out[f"{band}_factor"] = to_float(match.group(1)) * vat_mult
        out[f"{band}_base"] = (
            parse_sign(match.group(2)) * to_float(match.group(3)) * vat_mult / 100.0
        )
    return out


def _extract_energy(
    text: str, kind: TariffKind, *, professional: bool = False
) -> EnergyRates:
    yearly_fee = _extract_yearly_fee(text)
    if kind == "dynamic":
        consumption = _parse_formula(_CONSUMPTION_FORMULA_RE.search(text))
        if consumption is None:
            raise ExtractorError("could not parse Mega dynamic consumption formula")
        # Mega's consumption formula is TVAC and uses spot in c€/kWh, so
        # `factor` already maps EUR/kWh-spot to EUR/kWh-energy directly.
        # Just convert the base cents to EUR.
        factor, base = consumption
        return DynamicRates(
            factor=factor,
            base=base,
            yearly_fixed_fee=yearly_fee,
        )

    # A variable / Impact card's headline table is a 12-month forward
    # simulation; the rates it actually bills are printed below it as "les
    # derniers prix constates et utilises pour le calcul de votre facture de
    # regularisation". Prefer those, and fall back to the table on a card
    # that carries no such sentence.
    realized = _realized_rates(text) if kind in ("variable", "tou_impact") else {}

    if kind == "tou_impact":
        pic = realized.get("pic") or _extract_impact_tier(text, "PIC")
        medium = realized.get("medium") or _extract_impact_tier(text, "MEDIUM")
        eco = realized.get("eco") or _extract_impact_tier(text, "ECO")
        if pic is None or medium is None or eco is None:
            raise ExtractorError("could not parse Mega Off-peak Impact energy block")
        formula_match = re.search(
            r"La formule tarifaire est la suivante[^.]+?(?=\s*Cette formule)",
            text,
            re.S | re.I,
        )
        formula = (
            re.sub(r"\s+", " ", formula_match.group(0)).strip()
            if formula_match
            else None
        )
        return ImpactRates(
            pic=pic,
            medium=medium,
            eco=eco,
            yearly_fixed_fee=yearly_fee,
            formula=formula,
            **_impact_band_coefficients(text, professional=professional),
        )

    mono = _extract_meter_value(text, "Compteur mono-horaire")
    peak = _extract_meter_value(text, "Tarif jour")
    offpeak = _extract_meter_value(text, "Tarif nuit")
    excl_night = _extract_meter_value(text, "Exclusif nuit")
    if mono is None:
        raise ExtractorError(f"could not parse Mega {kind} energy block")
    if kind == "fixed":
        return FixedRates(
            single=mono,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=excl_night,
            yearly_fixed_fee=yearly_fee,
        )
    f_factor, f_base = _variable_cohort_coefficients(text, professional=professional)
    bands = _variable_band_coefficients(text, professional=professional)
    ceiling = _energy_price_ceiling(text)
    return VariableRates(
        current=realized.get("mono", mono),
        peak=realized.get("peak", peak),
        offpeak=realized.get("offpeak", offpeak),
        exclusive_night=realized.get("exclusive_night", excl_night),
        yearly_fixed_fee=yearly_fee,
        formula_factor=f_factor,
        formula_base=f_base,
        formula_factor_peak=bands.get("peak", (None, None))[0],
        formula_base_peak=bands.get("peak", (None, None))[1],
        formula_factor_offpeak=bands.get("offpeak", (None, None))[0],
        formula_base_offpeak=bands.get("offpeak", (None, None))[1],
        ceiling_single=ceiling.get("mono"),
        ceiling_peak=ceiling.get("peak"),
        ceiling_offpeak=ceiling.get("offpeak"),
        ceiling_exclusive_night=ceiling.get("exclusive_night"),
    )


_CEILING_RE = re.compile(
    r"limit\w+\s+[\u00e0a]\s+un plafond de\s*:?\s*\([^)]*\)\s*"
    r"Compteur\s+mono-horaire\s*:\s*(?P<mono>\d+[.,]\d+)\s*;\s*"
    r"Jour\s*:\s*(?P<peak>\d+[.,]\d+)\s*;\s*"
    r"Nuit\s*:\s*(?P<offpeak>\d+[.,]\d+)\s*;\s*"
    r"Exclusif\s+nuit\s*:\s*(?P<exclusive_night>\d+[.,]\d+)",
    re.IGNORECASE,
)


def _energy_price_ceiling(text: str) -> dict[str, float]:
    """Per-meter ceiling on the energy component, in EUR/kWh.

    Mega Cap is the one product that caps what the commodity can cost: "la
    composante energie facturee est limitee a un plafond de ... vous payez le
    minimum entre les prix variables mensuels et ce plafond", guaranteed a
    year from the start of supply. The card prints the four meter columns on
    the same basis as its rates, TVAC on the residential edition and HTVA on
    the professional one, so nothing here has to know which it is reading.

    Empty for every other card, which prints no such sentence.
    """
    match = _CEILING_RE.search(" ".join(text.split()))
    if match is None:
        return {}
    return {
        name: to_float(value) / 100.0
        for name, value in match.groupdict().items()
        if value is not None
    }


def _realized_rates(text: str) -> dict[str, float]:
    """The rates Mega says it actually bills, in EUR/kWh.

    On a variable or Off-peak Impact card the headline table is a forward
    simulation, and the card says so: "Les prix affiches dans le tableau
    ci-dessus et utilises pour realiser une simulation tarifaire sont calcules
    sur base d'une prevision des prix de l'energie pour une livraison les 12
    prochains mois." The billed figures are in the sentence after it, "Les
    derniers prix constates et utilises pour le calcul de votre facture de
    regularisation pour le mois de <month>", printed in c€/kWh.

    Two label sets, one per product family:
      variable      Compteur mono-horaire / Jour / Nuit / Exclusif nuit
      Off-peak Imp. tarif ECO / tarif MEDIUM / tarif PIC
    both followed by Injection. Returns whatever it finds keyed by
    mono / peak / offpeak / exclusive_night / eco / medium / pic / injection;
    an empty mapping means the sentence is absent (a fixed card, or a layout
    change) and the caller keeps the table.

    The text layer wraps mid-word, so every label tolerates internal
    whitespace and soft hyphens.
    """
    # The gap between the two anchors must tolerate a colon. On a card where
    # the sentence straddles a page break the extractor splices the page
    # footer into it, and that footer is full of colons ("Sources d'energie
    # pour :", "votre produit :", "(telles qu'approuvees par la CWaPE) :").
    # A [^:]* gap then fails to match and the whole override quietly no-ops
    # for that month, so one month of a year-to-date walk sits on the
    # simulation table while its neighbours use the realized rates. Both
    # anchors are specific enough that a non-greedy bounded gap is safe.
    block = re.search(
        r"derniers prix constat.{0,400}?sont les suivants \(c€/kWh\)\s*:(.{0,400})",
        text,
        re.S | re.I,
    )
    if block is None:
        return {}
    body = re.sub(r"-\s*\n\s*", "", block.group(1))
    body = re.sub(r"\s+", " ", body)
    labels = (
        ("mono", r"Compteur mono-?horaire"),
        ("peak", r"Jour"),
        ("offpeak", r"Nuit"),
        ("exclusive_night", r"Exclusif nuit"),
        ("eco", r"tarif ECO"),
        ("medium", r"tarif MEDIUM"),
        ("pic", r"tarif PIC"),
        ("injection", r"Injection"),
    )
    out: dict[str, float] = {}
    for key, label in labels:
        # "Nuit" also matches inside "Exclusif nuit", so require the label to
        # start the item: after the colon-separated list separator or the
        # start of the block.
        # Match the digits exactly, never a trailing sentence period: the
        # list ends "Injection : 2.32." and a greedy [\d.,]+ ate the stop.
        # A leading minus is part of the value: the May 2026 cards print
        # "Injection : -0.32", a month the customer PAYS to inject. Without
        # it the key went missing and the caller fell back to the 12-month
        # simulation table, crediting +2,42 c€/kWh against a billed -0,32 --
        # the wrong sign, 82 EUR out over 3000 kWh injected. The soft-hyphen
        # join above has already run, so a minus left here is a real one.
        # The value needs a right-hand boundary as well. Without one the
        # pattern takes the first well-formed PREFIX of a malformed token, and
        # the June 2026 Flanders cards collide two runs in the text layer:
        # "Compteur mono- horaire : 16.76.38". That yielded mono = 16,76 --
        # which is the Jour value, so mono == peak while offpeak was 14,20, a
        # combination the card cannot print. Refusing the token drops the key
        # and the caller falls back to the headline table, which is honest;
        # taking a prefix that belongs to another row is not. A trailing
        # sentence period still has to pass ("Injection : 2.32."), so the
        # boundary rejects only a further DIGIT or a decimal group.
        m = re.search(
            rf"(?:^|[;:,])\s*{label}\s*:\s*(-?\d+(?:[.,]\d+)?)(?!\d|[.,]\d)",
            body,
            re.I,
        )
        if m is not None:
            out[key] = to_float(m.group(1)) / 100.0
    return out


def _extract_impact_tier(text: str, tier: str) -> float | None:
    """Pull one ECO / MEDIUM / PIC rate from an Off-peak Impact card.

    The energy block prints ``Tarif <TIER>\\n<consumption>\\n<injection>``;
    we want the first number after the label. The cards in circulation
    use bare ``PIC`` (not ``Tarif PIC``) on the last row, and lowercase
    ``tarif`` in the footnote formula, so the regex is intentionally
    permissive on the ``Tarif`` prefix.
    """
    match = re.search(rf"(?:Tarif\s+)?{tier}\s*\n\s*([\d.,]+)", text)
    return to_float(match.group(1)) / 100.0 if match else None


def _extract_meter_value(text: str, label: str) -> float | None:
    """Pull the consumption rate that follows a meter-type label.

    Mega prints labels and values on separate lines; the consumption rate
    is the first number after the label and the injection rate is the
    second. Tarif jour / Tarif nuit / Exclusif nuit are only meaningful
    under the ``Compteur bi-horaire`` header; anchor on it so a later
    mention in dynamic-formula footnotes can't shadow the energy-block
    value.
    """
    scope = text
    if label in ("Tarif jour", "Tarif nuit", "Exclusif nuit"):
        # pypdf splits "Compteur bi-horaire" across a newline on every
        # current Mega card, so a literal ``find("Compteur bi-horaire")``
        # never matched. Match either the joined or the split spelling.
        anchor_match = re.search(r"Compteur\s+bi-horaire", text)
        if anchor_match is not None:
            scope = text[anchor_match.start() :]
    match = re.search(
        rf"{re.escape(label)}\s*\n\s*([\d.,]+)",
        scope,
    )
    return to_float(match.group(1)) / 100.0 if match else None


def _extract_yearly_fee(text: str) -> float:
    # Mega's dynamic card splits the heading across two lines
    # ('Redevance fixe\n(€/an)\n42.4'); fixed cards keep them together
    # ('Redevance fixe (€/an)\n111.3'). Accept either layout.
    match = re.search(r"Redevance fixe\s*\n?\s*\(€/an\)\s*\n?\s*([\d.,]+)", text)
    if match is None:
        # The standing charge (42-111 EUR/yr) is on every card; raise on a
        # miss rather than silently drop it, matching every other Mega line.
        raise ExtractorError("Mega: yearly fixed fee (Redevance fixe) not found")
    return to_float(match.group(1))


_FR_MONTH_NAMES = FR_MONTHS


def _extract_publication_month(text: str) -> str:
    # Smart Fixed cards prefix the version + month ("V2 avril 2026").
    # Include û so August ("août") keeps its version prefix.
    match = re.search(r"V(\d+\s+[a-zéû]+\s+\d{4})", text)
    if match:
        return match.group(1)
    # Smart Flex and Dynamic cards drop the version prefix and only print
    # "Prix du mois MM/YYYY". Surface MM/YYYY as "<month-name> YYYY" so
    # the publication_label sensor still tells the user which month the
    # snapshot covers.
    fallback = re.search(r"mois\s+(\d{2})/(\d{4})", text)
    if fallback:
        month = int(fallback.group(1))
        year = fallback.group(2)
        if 1 <= month <= 12:
            return f"{_FR_MONTH_NAMES[month - 1]} {year}"
    return ""


def _extract_valid_until(text: str) -> date | None:
    """Mega cards are valid for the printed month; use end-of-month."""
    fallback = re.search(r"mois\s+(\d{2})/(\d{4})", text)
    if not fallback:
        return None
    month = int(fallback.group(1))
    year = int(fallback.group(2))
    if not 1 <= month <= 12:
        return None
    return end_of_month(year, month)


# The monthly feed-in formula on a variable / Impact card. Kept apart from the
# dynamic anchor deliberately: see the comment at its use site.
_VARIABLE_INJECTION_SPP_RE = re.compile(
    rf"Epex\s*SPP\s*\*\s*([\d.,]+)\s*([{SIGN_CHARS}])\s*([\d.,]+)\s*c€/kWh",
    re.IGNORECASE,
)


def _extract_injection(text: str, kind: TariffKind) -> InjectionRates | None:
    """Mega prints injection rates in the same energy block, second column.

    On a variable / Impact card that block is the 12-month simulation, so the
    realized "derniers prix constates" sentence wins here as well: reading the
    table credited 3,84 c€/kWh where the card bills 2,32.
    """
    realized_injection = (
        _realized_rates(text).get("injection")
        if kind in ("variable", "tou_impact")
        else None
    )
    if kind == "tou_impact":
        # Off-peak Impact cards lack the ``Compteur mono-horaire`` anchor;
        # injection sits as the second number under any of the three tier
        # labels (all three rows print the same value, so pick the first
        # one we find).
        inj_match = re.search(
            r"(?:Tarif\s+)?(?:ECO|MEDIUM|PIC)\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)", text
        )
        current = to_float(inj_match.group(1)) / 100.0 if inj_match else None
    else:
        pattern = re.compile(
            r"Compteur mono-horaire\s*\n\s*[\d.,]+\s*\n\s*([\d.,]+)",
        )
        match = pattern.search(text)
        current = to_float(match.group(1)) / 100.0 if match else None

    if realized_injection is not None:
        current = realized_injection

    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    spp_indexed = False
    if kind == "dynamic":
        # Distinct anchor: injection is the formula after "(HTVA)".
        # Residential injection is VAT-exempt so the HTVA value is
        # already what the user receives.
        inj_match = _INJECTION_FORMULA_RE.search(text)
        parsed = _parse_formula(inj_match)
        if parsed is not None and inj_match is not None:
            factor, base = parsed
            formula = inj_match.group(0)
    elif kind in ("variable", "tou_impact"):
        # The variable and Impact cards state their own monthly formula: "le
        # prix de rachat de votre energie injectee est indexe mensuellement
        # selon la formule tarifaire. Il est base sur la moyenne des valeurs
        # quart-horaires Day-Ahead EPEX SPOT Belgium, ponderee par le SPP
        # (publie par Synergrid), sur le mois de fourniture ... : Epex SPP *
        # 0,85 - 2,2 c€/kWh". The figure read above is the PREVIOUS month's
        # regularisation, so it is the fallback and the formula is the
        # contract.
        #
        # A separate anchor from the dynamic one on purpose. Both card
        # generations contain the literal "formule suivante (HTVA)", so
        # widening _INJECTION_FORMULA_RE to also accept "Epex SPP" would let
        # the dynamic anchor bind this MONTHLY formula and price it at the
        # current slot's spot.
        spp_match = _VARIABLE_INJECTION_SPP_RE.search(re.sub(r"\s+", " ", text))
        if spp_match is not None:
            # The card quotes the formula in c€/kWh throughout, index
            # included, so the factor is used as-is and only the base scales.
            # Not the x10 rule, which is for a card quoting c/kWh per EUR/MWh.
            factor = to_float(spp_match.group(1))
            base = parse_sign(spp_match.group(2)) * to_float(spp_match.group(3)) / 100.0
            formula = spp_match.group(0)
            # Epex SPP is the solar-weighted mean, and the commodity leg on
            # the same card indexes on the RLP one. The flag is what keeps
            # the credit off the energy leg's index.
            spp_indexed = True

    if current is None and factor is None:
        return None
    return InjectionRates(
        current=current,
        factor=factor,
        base=base,
        formula=formula,
        spp_indexed=spp_indexed,
    )
