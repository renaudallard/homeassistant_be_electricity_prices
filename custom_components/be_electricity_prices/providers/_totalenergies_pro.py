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

"""TotalEnergies: the professional cards.

proComfort, proEssential, their fixed twins and proDrive are printed on the
residential template, excluding VAT, and differ from it where a business is
billed differently:

- the green energy contribution is a column of its own after the meter
  rates, billed on top of them, where a residential card folds it into its
  prices and states it in a footnote;
- the federal excise is the professional schedule, three tranches by annual
  volume (the law of 25 October 2026: 11,10, 9 and 8,30 EUR/MWh);
- the Flemish energy fund row is the one for a low-voltage connection;
- in Brussels the Sibelga row carries transport, metering and both capacity
  terms, and the energy contribution sits beside the excise.

Everything else, the Flemish and Walloon network rows, the Walloon connection
fee, the Brussels public service levy and the indexation, reads as on a
residential card. ``totalenergies.parse_snapshot`` checks the title and the
region and hands the card here.
"""

from __future__ import annotations

import re

from ..const import (
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    VAT_RATE_STANDARD,
)
from ._parse import (
    SIGN_CHARS,
    TIER_BOUND,
    numeric_row,
    parse_brussels_osp,
    tier_bound_kwh,
    to_float,
)
from ._rates import EnergyRates, TariffKind, fixed_or_variable_rates
from ._totalenergies_cards import (
    _extract_connection_fee,
    _extract_energy,
    _extract_publication_month,
)
from ._totalenergies_overlays import (
    _energy_contribution_from_table,
    _extract_energy_contribution,
    _extract_flanders_dsos,
    _extract_wallonia_dsos,
)
from ._validity import parse_valid_until
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierSnapshot,
    TaxOverlay,
    brussels_sibelga_overlay,
    with_vat_basis,
)

# The row's heading, alone on its line: "Consommation", "Consommation**" or
# the Dutch "Verbruik     **".
_SUPPLIER_HEAD_RE = re.compile(r"(?m)^[ \t]*(?:Consommation|Verbruik)[ \t*]*$")
_ROW_FORMULA_RE = re.compile(
    rf"\d+(?:\.\d+)?[ \t]*\*[ \t]*BELPEXM_RLP[ \t]*[{SIGN_CHARS}][ \t]*\d+(?:\.\d+)?"
)
_FIGURES_RE = re.compile(r"\d+,\d+(?:[ \t]+\d+,\d+)*")
# Four meter rates, the yearly fee and the green energy contribution.
_SUPPLIER_FIGURES = 6


def supplier_row(text: str, kind: TariffKind) -> tuple[float, list[float], float]:
    """The yearly fee (EUR), the four meter rates and the green energy
    contribution (c EUR/kWh) the card prints under its consumption heading.

    The figures run over a few lines, around the formulas on a variable card,
    and end at the first line of words. A fixed card prints the fee first,
    then the rates and the contribution:

      90,00 23,73 25,22 22,40 23,05 1,382

    a variable one the rates, then the fee and the contribution, which
    Brussels puts on the line after the formulas:

      21,54 23,08 20,02 20,76
      90,00 1,382
      0.1064 * BELPEXM_RLP + 2.38 0.115 * BELPEXM_RLP + 2.38 ...
    """
    head = _SUPPLIER_HEAD_RE.search(text)
    if head is None:
        raise ExtractorError(
            "TotalEnergies: the professional consumption row not found"
        )
    figures: list[float] = []
    for line in text[head.end() :].split("\n"):
        rest = _ROW_FORMULA_RE.sub(" ", line).strip()
        if not rest:
            continue
        if not _FIGURES_RE.fullmatch(rest):
            break
        figures += [to_float(f) for f in rest.split()]
    if len(figures) != _SUPPLIER_FIGURES:
        raise ExtractorError(
            "TotalEnergies: the professional consumption row prints "
            f"{len(figures)} figures, not {_SUPPLIER_FIGURES}"
        )
    if kind == "fixed":
        return figures[0], figures[1:5], figures[5]
    return figures[4], figures[:4], figures[5]


def pro_energy(text: str, kind: TariffKind) -> tuple[EnergyRates, float]:
    """The energy leg and the green energy contribution in EUR/kWh.

    A variable card bills, like a residential one, the monthly rates it
    prints at the last known index, beside the formula that re-prices the
    delivery month; it is read the same way once its fee is known. A fixed
    card bills its row.
    """
    fee, rates, renewables = supplier_row(text, kind)
    energy: EnergyRates
    if kind == "fixed":
        single, peak, offpeak, night = (rate / 100.0 for rate in rates)
        energy = fixed_or_variable_rates(
            kind,
            single=single,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=night,
            yearly_fixed_fee=fee,
        )
    else:
        energy = _extract_energy(text, kind, yearly_fee=fee)
    return energy, renewables / 100.0


_EXCISE_HEAD_RE = re.compile(r"Accise\s+f[ée]d[ée]rale|Federale\s+bijdrage")
# Flanders and Wallonia print a row per tranche, the Dutch one put in French.
_EXCISE_ROW_RE = re.compile(
    rf"Consommation entre ({TIER_BOUND}) et ({TIER_BOUND}) kWh[ \t]+(\d+,\d+)"
)
# Brussels prints the tranches as columns, "Entre 0 / et 20.000 kWh" ("Tussen
# 0 / en 20.000 kWh"), with the energy contribution's column before them and
# the figures on one line below.
_EXCISE_UPPER_RE = re.compile(rf"(?<![\w.])(?:et|en)[ \t]+({TIER_BOUND})[ \t]*kWh")
_EXCISE_WINDOW = 1500
# What the first row's lower bound reads: 0, or 3.000 on the Flemish cards.
_FIRST_LOWER_BOUNDS = (0.0, 3000.0)


def pro_excise(text: str) -> tuple[tuple[tuple[float, float], ...], float | None]:
    """The excise schedule as ``((upper_kwh, eur_per_kwh), ...)`` and, where
    the card prints it in the same block (Brussels), the energy contribution
    in EUR/kWh.

    The first tranche runs from 0 kWh whatever the card says: the Flemish
    cards head it "entre 3.000 et 20.000 kWh", where the Walloon and Brussels
    ones say "entre 0 et 20.000", and the law's professional schedule has no
    tranche below 20 MWh. It has to be there, though: the Dutch proEssential
    card in Flanders of November 2026 leaves its figure blank, and read from
    the next row it billed every kWh up to 50 MWh at 0,90. Each later
    tranche has to start where the one before it ended.
    """
    head = _EXCISE_HEAD_RE.search(text)
    if head is None:
        raise ExtractorError("TotalEnergies: the professional excise not found")
    window = text[head.end() : head.end() + _EXCISE_WINDOW]
    contribution: float | None = None
    rows = _EXCISE_ROW_RE.findall(window)
    if rows:
        uppers = [tier_bound_kwh(upper) for _lower, upper, _rate in rows]
        lowers = [tier_bound_kwh(lower) for lower, _upper, _rate in rows]
        if lowers[0] not in _FIRST_LOWER_BOUNDS:
            raise ExtractorError("TotalEnergies: the first excise tranche not found")
        if lowers[1:] != uppers[:-1]:
            raise ExtractorError("TotalEnergies: the excise tranches do not join")
        rates = [to_float(rate) / 100.0 for _lower, _upper, rate in rows]
    else:
        bounds = list(_EXCISE_UPPER_RE.finditer(window))
        if not bounds:
            raise ExtractorError("TotalEnergies: the excise tranches not found")
        uppers = [tier_bound_kwh(b.group(1)) for b in bounds]
        figures = next(
            (
                line.split()
                for line in window[bounds[-1].end() :].split("\n")
                if _FIGURES_RE.fullmatch(line.strip())
            ),
            [],
        )
        if len(figures) != len(uppers) + 1:
            raise ExtractorError(
                "TotalEnergies: the excise row does not print a figure per tranche"
            )
        contribution = to_float(figures[0]) / 100.0
        rates = [to_float(rate) / 100.0 for rate in figures[1:]]
    if len(rates) < 2 or uppers != sorted(set(uppers)) or min(rates) <= 0.0:
        raise ExtractorError("TotalEnergies: the excise schedule does not read")
    return tuple(zip(uppers, rates, strict=True)), contribution


# "Basse tension 10,07" ("Laagspanning"): the monthly levy on a low-voltage
# connection, the only one these cards price.
_ENERGY_FUND_RE = re.compile(r"(?:Basse\s+tension|Laagspanning)[ \t]+(\d+,\d+)")


def pro_energy_fund(text: str) -> float:
    """The Flemish energy fund in EUR/month."""
    match = _ENERGY_FUND_RE.search(text)
    if match is None:
        raise ExtractorError("TotalEnergies: the professional energy fund not found")
    return to_float(match.group(1))


def pro_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """The Sibelga row: the four meter rates and transport (c EUR/kWh), then
    metering and the capacity terms up to and above 13 kVA (EUR/year)."""
    row = numeric_row(text, "SIBELGA", 8)
    if not row:
        raise ExtractorError("TotalEnergies: the professional Sibelga row not found")
    mono, peak, offpeak, night, transport, metering, low, high = map(to_float, row)
    return {
        DSO_SIBELGA: brussels_sibelga_overlay(
            mono=mono,
            peak=peak,
            offpeak=offpeak,
            excl_night=night,
            transport=transport,
            data_management_per_year=metering + low,
            power_term_above_13kva=metering + high,
            osp_by_tier=parse_brussels_osp(text),
        )
    }


def parse_pro_snapshot(
    contract_id: str, kind: TariffKind, text: str, region: str, source_url: str
) -> SupplierSnapshot:
    """A professional card, its Dutch labels already put in French. The
    feed-in is on a card of its own, read by the caller."""
    energy, renewables = pro_energy(text, kind)
    bands, contribution = pro_excise(text)
    if contribution is None:
        contribution = _extract_energy_contribution(text)
    if contribution is None:
        contribution = _energy_contribution_from_table(text, region)
    if contribution is None:
        raise ExtractorError("TotalEnergies: federal energy contribution not found")
    energy_fund = 0.0
    connection_fee = 0.0
    if region == REGION_FLANDERS:
        dsos = _extract_flanders_dsos(text)
        energy_fund = pro_energy_fund(text)
    elif region == REGION_WALLONIA:
        dsos = _extract_wallonia_dsos(text)
        connection_fee = _extract_connection_fee(text)
    else:
        dsos = pro_brussels_dsos(text)
    if not dsos:
        raise ExtractorError("TotalEnergies: the professional network table not found")
    return with_vat_basis(
        SupplierSnapshot(
            supplier="totalenergies",
            contract=contract_id,
            energy=energy,
            dsos=dsos,
            taxes=TaxOverlay(
                federal_excise=bands[0][1],
                federal_excise_bands=bands,
                energy_contribution=contribution,
                flanders_renewables=renewables if region == REGION_FLANDERS else 0.0,
                wallonia_renewables=renewables if region == REGION_WALLONIA else 0.0,
                brussels_renewables=renewables if region == REGION_BRUSSELS else 0.0,
                region_connection_fee=connection_fee,
                energy_fund_eur_per_month=energy_fund,
                # Every figure excludes VAT at 21%; _resolve.apply_vat
                # resolves it for the entry.
                vat_rate=VAT_RATE_STANDARD,
            ),
            source_url=source_url,
            publication_label=_extract_publication_month(text),
            valid_until=parse_valid_until(text),
        ),
        None,
        professional=True,
    )
