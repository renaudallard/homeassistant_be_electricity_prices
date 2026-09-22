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

"""EnergyVision in Wallonia: a card in French, with its own layout.

Same supplier, different regulator and a different document. The French card
prints the CWaPE Impact bands, names its months in French and lays its tables
out unlike the Dutch ones, so it gets its own readers rather than a pile of
language branches inside theirs.
"""

from __future__ import annotations

from ._parse import to_float
from ._rates import EnergyRates
from ._rates import FixedRates
from ._rates import InjectionRates
from ._rates import SpotMonthlyRates
from ._validity import parse_valid_until
from .base import DsoOverlay
from .base import ExtractorError
from .base import SupplierSnapshot
from .base import TaxOverlay
from .base import walloon_dso_overlay
import re
from ._energyvision_cards import _spp_injection
from ._energyvision_cards import _tiered_legs
from ._energyvision_overlays import _NUM
from ..const import DSO_AIEG
from ..const import DSO_AIESH
from ..const import DSO_ORES
from ..const import DSO_RESA
from ..const import DSO_REW
from ._parse import SIGN_CHARS


def _parse_wallonia(
    contract_id: str, text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Parse the French Walloon card.

    Same snapshot shape as the Flemish cards, off an entirely separate
    publication. Two energy shapes now: the 1-year fixed product's flat
    VAT-inclusive rate, and the 1.800 kWh product's tranche plus monthly
    formula. Both carry a yearly standing charge and a monthly-indexed
    injection indicative, and both share the Walloon DSO and tax blocks.
    """
    # Local: energyvision.py imports this module, so the registry cannot be
    # imported at the top without closing a cycle.
    from .energyvision import _CONTRACTS_BY_ID

    contract = _CONTRACTS_BY_ID[contract_id]
    energy: EnergyRates
    if contract.kind == "spot_monthly":
        energy, injection = _extract_tiered_fr(text)
    else:
        energy, injection = _extract_fixed_fr(text)
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos_fr(text),
        taxes=_extract_taxes_fr(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label_fr(text),
        valid_until=parse_valid_until(text),
        injection=injection,
    )


def _publication_label_fr(text: str) -> str:
    m = _LABEL_FR_RE.search(text)
    return m.group(1).lower() if m else ""


def _extract_tiered_fr(text: str) -> tuple[SpotMonthlyRates, InjectionRates]:
    """The Walloon 1.800 kWh card, on the French publication.

    Its standing charge is zero where the Flemish and Brussels cards of the
    same product charge 50 EUR/yr, which is a figure to read rather than a
    row to treat as missing: ``_FEE_FR_RE`` matching "Frais fixes 0 €/an" is
    the card saying nothing is owed.
    """
    fee = _FEE_FR_RE.search(text)
    if fee is None:
        raise ExtractorError("EnergyVision: frais fixes row not found")
    return _tiered_legs(
        text,
        fee=to_float(fee.group(1)),
        vat_re=_VAT_FR_RE,
        tier_re=_TIER_FIXED_FR_RE,
        injection_re=_FIXED_INJECTION_FR_RE,
        tranche=True,
    )


def _extract_fixed_fr(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _FEE_FR_RE.search(text)
    if fee is None:
        raise ExtractorError("EnergyVision: frais fixes row not found")
    m = _FIXED_ENERGY_FR_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia energy price")
    # One flat rate: the card prints no bi-horaire or exclusive-night energy
    # price (those words appear only as DSO-table column headers), so peak /
    # offpeak / exclusive_night stay unset and the engine bills `single` for
    # every meter type. Printed VAT-inclusive, so used as-is.
    energy = FixedRates(
        single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=to_float(fee.group(1))
    )
    inj = _FIXED_INJECTION_FR_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia injection price")
    injection = _spp_injection(text, to_float(inj.group(1)) / 100.0)
    return energy, injection


def _extract_taxes_fr(text: str) -> TaxOverlay:
    """Parse the Walloon tax block across both card generations.

    Until July 2026 the card carried a "Supplements et accise federale"
    section holding the energy contribution, the connection fee and a
    four-tier excise table. On 1 August 2026 EnergyVision deleted the whole
    supplements sub-block and replaced the tiers with one flat "Accise
    speciale" row, on every one of its Walloon cards at once.

    Only the green-certificate quota cost survives on both, so it stays
    mandatory. The excise takes the flat row when present and falls back to
    the 0-3.000 kWh tier for an older card.
    """
    excise = _EXCISE_FLAT_FR_RE.search(text) or _EXCISE_FR_RE.search(text)
    cv = _CV_FR_RE.search(text)
    if not excise or not cv:
        # The excise and the CV quota cost are both per-kWh charges that no
        # Walloon card omits, so a miss here is layout drift rather than a
        # component that stopped existing.
        raise ExtractorError("EnergyVision: could not parse Wallonia tax block")
    # The energy contribution was abolished on 2026-08-01 and folded into the
    # excise above, so an absent row is the levy being gone, not drift.
    contrib = _CONTRIB_FR_RE.search(text)
    # The connection fee is a different case: Wallonia still levies it, and
    # this card's own terms say taxes and redevances stay "entierement
    # repercutables sur le client". EnergyVision dropped the row along with
    # the abolished contribution, and publishes the rate nowhere else, so
    # there is nothing to read. Bill 0 rather than take the contract offline
    # over a charge worth ~0,075 c€/kWh, and flag it so the coordinator can
    # tell the user what their cost excludes. Peers that still print the row
    # (Engie, Mega, Bolt, OCTA+, DATS 24) keep reading it off their cards.
    connection = _CONNECTION_FR_RE.search(text)
    # There is no Flemish energiefonds and no GSC/WKC row on this card; the
    # header states every price includes 6% VAT, so vat_rate stays 0.0.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        energy_contribution=to_float(contrib.group(1)) / 100.0 if contrib else 0.0,
        wallonia_renewables=to_float(cv.group(1)) / 100.0,
        region_connection_fee=(
            to_float(connection.group(1)) / 100.0 if connection else 0.0
        ),
        region_connection_fee_unavailable=connection is None,
        vat_rate=0.0,
    )


def _extract_dsos_fr(text: str) -> dict[str, DsoOverlay]:
    """Parse the Walloon DSO table (one ten-column block, no meter split).

    Column order, left to right:

        mono | bi-peak | bi-offpeak | ECO | MEDIUM | PIC | exclusive-night
        | transport | data-management (EUR/yr) | prosumer (EUR/kW/yr)

    The three CWaPE Impact bands print CHEAPEST FIRST here, the reverse of
    the PIC | MEDIUM | ECO order on the DATS 24 card that carries the same
    regulated numbers. Reusing that positional mapping would swap the peak
    and off-peak bands and mis-price every Walloon Impact user, so the
    ordering is asserted in the tests by value (eco < medium < pic).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_ROWS_FR:
        row = re.search(
            rf"^{re.escape(label)}\s+" + r"\s+".join([_NUM] * 10),
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        # Bands print ECO | MEDIUM | PIC here, the reverse of the DATS 24
        # card's order. The keyword-only helper is what makes that safe to
        # share: the mapping stays visible at the call site.
        out[key] = walloon_dso_overlay(
            mono=to_float(row.group(1)),
            peak=to_float(row.group(2)),
            offpeak=to_float(row.group(3)),
            eco=to_float(row.group(4)),
            medium=to_float(row.group(5)),
            pic=to_float(row.group(6)),
            excl_night=to_float(row.group(7)),
            transport=to_float(row.group(8)),
            terme_fixe=to_float(row.group(9)),
            prosumer=to_float(row.group(10)),
        )
    missing = [key for _, key in _DSO_ROWS_FR if key not in out]
    if missing:
        # A partial table is worse than none: the areas are what every
        # entry picks its network cost from, and a card missing one would
        # be adopted, persisted and shared, leaving the entry on that area
        # with no overlay and every tick failing, while the last good card
        # is gone from the cache. Refusing keeps that card serving.
        raise ExtractorError(f"EnergyVision: DSO rows not found for {sorted(missing)}")
    return out


# "Carte tarifaire juillet 2026". \w rather than [A-Za-z]: the accented month
# names (fevrier, aout, decembre) would otherwise blank the label for three
# months a year, and a miss is silent here.
_LABEL_FR_RE = re.compile(r"Carte\s+tarifaire\s+(\w+\s+20\d{2})", re.IGNORECASE)
# "Tous les prix et tarifs incluent la TVA a 6 %" - the number follows the tax
# name here, the reverse of the Dutch "6% BTW".
_VAT_FR_RE = re.compile(r"TVA\s*(?:à|a)\s*(\d+)\s*%", re.IGNORECASE)
# "Electricite verte - tarif fixe 13,57 EURcent/kWh" and, on the same page,
# "Injection - variable 2,07 EURcent/kWh". The separator is an ASCII hyphen on
# the first and a U+2013 en dash on the second, both already in SIGN_CHARS.
_FIXED_ENERGY_FR_RE = re.compile(
    rf"Électricité\s+verte\s*[{SIGN_CHARS}]\s*tarif\s+fixe\s+"
    rf"{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_FR_RE = re.compile(
    rf"Injection\s*[{SIGN_CHARS}]\s*variable\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# The Walloon tiered card's tranche row, "Électricité verte (<1.800 kWh –
# tarif fixe) 10,60 €cent/kWh". The parenthetical is what tells it apart from
# the flat card's "Électricité verte – tarif fixe", which _FIXED_ENERGY_FR_RE
# reads and which correctly misses this one. The bound's dot is a thousands
# separator, so it goes through tier_bound_kwh rather than to_float, which
# would read 1.800 kWh as one point eight.
_TIER_FIXED_FR_RE = re.compile(
    rf"Électricité\s+verte[^(\n]*\(\s*<\s*([\d.,]+)\s*kWh\s*[{SIGN_CHARS}]\s*"
    rf"tarif\s+fixe\s*\)\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FEE_FR_RE = re.compile(rf"Frais\s+fixes\s+{_NUM}\s*€\s*/\s*an", re.IGNORECASE)
# Walloon tax block. The units live in the section headers ("Suppléments
# (€cent/kWh)", "Accise fédérale (€cent/kWh)"), not on the rows, so every
# value here is c€/kWh and divides by 100.
# EnergyVision groups the thousand two ways in the same row of the same
# regulated table: "3.000" on the 1-year fixed cards and "3 000" on the
# 1.800 kWh ones, for the same month and the same rate. Anchored on either,
# because pinning the dot lost the whole tax block on the other publication.
_EXCISE_FR_RE = re.compile(
    rf"Consommation\s+entre\s+0\s*&\s*3[.\s]000\s+kWh\s+{_NUM}", re.IGNORECASE
)
# From 1 August 2026 the federal scheme folded the energy contribution into
# the special excise and flattened it, so the card prints one rate under
# "Accise speciale" instead of the four-tier consumption table.
_EXCISE_FLAT_FR_RE = re.compile(
    rf"Accise\s+sp[ée]ciale\s+{_NUM}\s*€?\s*cent\s*/\s*kWh", re.IGNORECASE
)
_CONTRIB_FR_RE = re.compile(rf"Contribution\s+énergétique\s+{_NUM}", re.IGNORECASE)
_CONNECTION_FR_RE = re.compile(
    rf"Redevance\s+de\s+raccordement\s+{_NUM}", re.IGNORECASE
)
# The Walloon green-certificate quota cost, the CV counterpart of Flanders'
# GSC + WKC. Supplier-specific (EnergyVision prints 3,00 where DATS 24 prints
# 2,860 for the same month), so it is always read off this card.
_CV_FR_RE = re.compile(
    rf"certificats\s+verts\s+et\s+certificats\s+de\s+cogénération"
    rf"[^\d]*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# Walloon DSO row label -> DSO key. EnergyVision drops the "ORES" prefix from
# six of the seven ORES sub-areas (BRABANT WALLON, EST, HAINAUT ELECTRICITÉ,
# ORES LUXEMBOURG, MOUSCRON, NAMUR, VERVIERS), all carrying identical numbers,
# so the project's collapse-to-one-key convention picks Brabant Wallon as the
# representative row, matching dats24.py. Note the labels differ from DATS
# 24's card ("TECTEO RESA" vs "RESA", "WAVRE" vs "RÉGIE DE WAVRE").
_DSO_ROWS_FR: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    ("BRABANT WALLON", DSO_ORES),
    ("TECTEO RESA", DSO_RESA),
    ("WAVRE", DSO_REW),
)
