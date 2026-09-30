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
"""Ecopower grid and levy overlays: the DSO tables and the tax block.

Split out of ``ecopower.py`` alongside ``_ecopower_cards.py``, as
``_mega_overlays.py`` was out of ``mega.py``. The cut is the call graph:
these are the leaf parsers ``parse_snapshot`` and ``parse_dbs_snapshot`` call
to build the ``DsoOverlay`` map and the ``TaxOverlay``, where
``_ecopower_cards`` holds the ones that build the energy and injection legs.
Both Ecopower cards share the tax reader.

No behaviour change: every function here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

import re

from ..const import (
    FLUVIUS_CARD_LABELS,
    VAT_RATE_REDUCED,
)
from ._parse import numeric_row, to_float
from ._pdf import printed_vat_rate
from .base import (
    DsoOverlay,
    ExtractorError,
    TaxOverlay,
)

_DSO_LABELS = FLUVIUS_CARD_LABELS


# ---- DSOs --------------------------------------------------------------------


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the DIGITAL METER block.

    Ecopower's card lists two networks per Fluvius sub-area: digital
    meter rates (capacity tariff per kW/yr, lower per-kWh distribution)
    and analog meter rates (yearly fixed fee, higher distribution,
    spinning-back prosumer fee). The integration only models the
    digital path, which is what the vast majority of Flemish
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
        # An optional Maximumtarief column slides in between
        # uitsluitend_nacht and the trailing dash on rows where
        # Fluvius publishes a maximum (Imewo's Apr 2026 card has one),
        # so a row carries either five figures or four. Ask for the wider
        # shape first: the narrow count would also match the wide row's
        # opening columns if the dashes ever moved.
        row = numeric_row(section, label, 5) or numeric_row(section, label, 4)
        if not row:
            continue
        # Ecopower's card is HTVA and declares vat_rate=0.06, so store both
        # flat fees exactly as printed: _resolve.apply_vat grosses them once per
        # entry, alongside every other flat annual fee. The same Fluvius
        # databeheer prints 17,85 HTVA here vs 18,92 TVAC on the other
        # suppliers' cards, and apply_vat is what turns one into the other.
        databeheer = to_float(row[0])
        capacity = to_float(row[1])
        single = to_float(row[2])
        # Column 4 is the exclusive-night meter rate (separate circuit
        # for an electric water heater / night-storage heater). It
        # used to be dropped because there was no DsoOverlay column
        # for it; now propagated for users on the exclusive_night
        # meter type. Same scaling as ``single``.
        excl_night = to_float(row[3])
        # Column 5 is the optional Maximumtarief. The card states the rule the
        # engine applies: "Zou u met het capaciteitstarief en het nettarief
        # per kWh meer nettarieven betalen dan met het maximumtarief? Dan
        # betaalt u het maximumtarief. U betaalt dus nooit meer dan dat. U
        # betaalt wel minstens de minimumbijdrage van 2,5 kW." Stored HTVA as
        # printed, like every other per-kWh figure on this card; apply_vat
        # grosses it per entry.
        out[key] = DsoOverlay(
            distribution_single=single,
            distribution_exclusive_night=excl_night,
            transport=0.0,  # rolled into distribution on Ecopower's card
            capacity_eur_per_kw_year=capacity,
            data_management_per_year=databeheer,
            network_ceiling_eur_per_kwh=(to_float(row[4]) if len(row) > 4 else None),
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
# narrower dynamic card: "Fluvius Midden-" / "<numbers>" / "Vlaanderen"
# on three lines. Stitch the two label fragments back together around the
# rate row so the per-DSO row regex sees one line. [ \t] (not \s) keeps
# the regex from swallowing the row's trailing newline.
_DBS_WRAPPED_LABEL_RE = re.compile(r"(Fluvius\s+\S*-)\n((?:[\d,]+[ \t]*){4,})\n(\S+)")


def _extract_dbs_dsos(text: str) -> dict[str, DsoOverlay]:
    """Read the digital-meter network tariffs from the dynamic card.

    The dynamic card carries only digital (meetregime 3 / SMR3) meter
    rows: there's no analog block, since a dynamic contract requires a
    smart meter. The row layout differs from the gbs card: the columns
    are ``databeheer (EUR/yr) | capacity (EUR/kW/yr) | afname
    enkelvoudig (EUR/kWh) | afname uitsluitend-nacht (EUR/kWh) |
    [maximumtarief] | injectietarief``, with no separating dashes. We
    read the first four numeric columns: the same four the gbs parser
    keeps, and ignore the optional maximumtarief and the injection
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
        # Six figures when the card prints a maximumtarief, five when it
        # doesn't; the four we keep lead the row either way.
        row = numeric_row(section, label, 6) or numeric_row(section, label, 5)
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row[2]),
            distribution_exclusive_night=to_float(row[3]),
            transport=0.0,  # rolled into distribution on Ecopower's card
            # HTVA card, stored as printed: _resolve.apply_vat grosses both flat
            # fees once per entry (same as the gbs parser).
            capacity_eur_per_kw_year=to_float(row[1]),
            data_management_per_year=to_float(row[0]),
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
# The card prints the domiciled amount with two decimals and a superscript
# footnote marker immediately after it, which the text layer flattens onto the
# number: "Bijdrage Energiefonds 0,006 euro/maand 10,07 euro/maand" is 0,00
# plus footnote 6, not 0,006. The marker is just the footnote's number, so it
# moves between cards (0,004 / 0,005 / 0,006 across the fixtures) and read as a
# value it made the levy drift card to card. Anchor on the two decimals the
# card actually prints and let the marker fall outside the group. The second
# column is the non-residential amount, which a residential entry never pays.
_FUND_RE = re.compile(
    r"Bijdrage Energiefonds\s+([\d.]+,\d{2})\d?\s*euro/maand", re.IGNORECASE
)


# "Alle bedragen zijn exclusief btw. Particuliere klanten betalen 6% btw."
_VAT_RE = re.compile(
    r"Particuliere\s+klanten\s+betalen\s+(\d+)\s*%\s*btw", re.IGNORECASE
)


def _extract_taxes(text: str) -> TaxOverlay:
    """Parse the federal/regional tax block.

    Ecopower prints all values HTVA. ``vat_rate`` tells the pricing engine
    to scale up to TVAC for residential customers, at the rate the card
    states for them ("Particuliere klanten betalen 6% btw"); every other
    supplier publishes TVAC and uses ``vat_rate=0.0``, but Ecopower is the
    cooperative outlier. A card that stopped stating it would be priced at
    the residential rate and say it was assumed.

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
    printed = printed_vat_rate(text, _VAT_RE)
    return TaxOverlay(
        federal_excise=to_float(federal_match.group(1)),
        energy_contribution=to_float(contrib_match.group(1)),
        flanders_renewables=(
            to_float(gsc_match.group(1)) + to_float(wkk_match.group(1))
        ),
        energy_fund_eur_per_month=(
            to_float(fund_match.group(1)) if fund_match else 0.0
        ),
        vat_rate=VAT_RATE_REDUCED if printed is None else printed,
        card_vat_rate=printed,
        assumed_vat_rate=VAT_RATE_REDUCED if printed is None else None,
    )
