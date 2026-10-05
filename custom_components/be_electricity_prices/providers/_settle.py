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

"""A month-indexed card's legs, settled on the index its month closed at.

A card indexed on the delivery month prints its rates at the previous
month's index, and the card after it names the value the month closed at.
These rebuild a leg's rates from its own coefficients at that value, for
every supplier that settles a month that way.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ._rates import (
    EnergyRates,
    ImpactRates,
    InjectionRates,
    TimeOfUseRates,
    VariableRates,
)
from .base import SupplierSnapshot


def settled_injection(inj: InjectionRates, index: float) -> InjectionRates:
    """A feed-in leg recomputed at the index its month actually settled at.

    ``current`` is rebuilt from the card's own coefficients, so the printed
    estimate gives way to the arithmetic the supplier invoices, and
    ``index_realised`` carries the figure so the engine bills it rather than
    the weighted mean it would otherwise compute. A leg with no coefficients
    keeps its printed figure and records the index alone.

    Worth settling because that mean is not the same number for these two
    suppliers. It weights each hour's MEAN price by the hour's solar share,
    while EBEM's SPP0 and Trevion's Belpex_SPP_BE weight each QUARTER by its
    own, and over January to August 2026 the computed one ran about
    0,9 EUR/MWh above both published series in every month. It is not a bug in
    the mean: Energy Knights defines its Belpex-SPP-M on the hourly quotation
    and the same computation reproduces its published series to 0,007%, so the
    resolution is a property of the card. See ``spot_stats._spp_month_mean``.

    Shared by every provider whose card publishes the settled value: EBEM
    names it as "vorige maand", Trevion and Engie name the month outright.
    A leg priced per time-of-use slot (Engie Empower Flextime) has each slot
    rebuilt from its own pair the same way.
    """
    slots: dict[str, Any] = {
        slot: factor * index + base
        for slot, factor, base in (
            ("peak", inj.factor_peak, inj.base_peak),
            ("transition", inj.factor_transition, inj.base_transition),
            ("offpeak", inj.factor_offpeak, inj.base_offpeak),
        )
        if factor is not None and base is not None
    }
    inj = replace(inj, index_realised=index, **slots)
    if inj.factor is None or inj.base is None:
        return inj
    return replace(inj, current=inj.factor * index + inj.base)


def settled_energy(energy: EnergyRates, index: float) -> EnergyRates:
    """A month-indexed energy leg recomputed at the index its month settled at.

    Every rate the card prints with a formula behind it is rebuilt from its
    own pair, one per meter or band: rewriting the mono rate alone would
    settle a mono meter and leave a bi-hourly or time-of-use one on the
    printed estimate. The leg records the index as ``index_realised``, so a
    keyed entry bills the supplier's figure too; an Impact leg has no such
    field, and its three bands are the whole settlement. Any other leg, or
    one with no formula, comes back as it was.
    """
    if not getattr(energy, "month_indexed", False):
        return energy
    if isinstance(energy, ImpactRates):
        pairs = {
            "pic": (energy.pic_factor, energy.pic_base),
            "medium": (energy.medium_factor, energy.medium_base),
            "eco": (energy.eco_factor, energy.eco_base),
        }
    elif isinstance(energy, VariableRates):
        pairs = {
            "current": (energy.formula_factor, energy.formula_base),
            "peak": (energy.formula_factor_peak, energy.formula_base_peak),
            "offpeak": (energy.formula_factor_offpeak, energy.formula_base_offpeak),
            "exclusive_night": (
                energy.formula_factor_exclusive_night,
                energy.formula_base_exclusive_night,
            ),
        }
    elif isinstance(energy, TimeOfUseRates):
        pairs = {
            "peak": (energy.formula_factor_peak, energy.formula_base_peak),
            "transition": (
                energy.formula_factor_transition,
                energy.formula_base_transition,
            ),
            "offpeak": (energy.formula_factor_offpeak, energy.formula_base_offpeak),
            "sunday": (energy.formula_factor_sunday, energy.formula_base_sunday),
        }
    else:
        return energy
    rates: dict[str, Any] = {
        field: factor * index + base
        for field, (factor, base) in pairs.items()
        if factor is not None
        and base is not None
        and getattr(energy, field) is not None
    }
    if isinstance(energy, ImpactRates):
        return replace(energy, **rates)
    return replace(energy, index_realised=index, **rates)


def _indexed_legs(snap: SupplierSnapshot) -> list[EnergyRates | InjectionRates]:
    """The legs of ``snap`` its card indexes on the month, and so settles."""
    legs: list[EnergyRates | InjectionRates] = []
    if getattr(snap.energy, "month_indexed", False):
        legs.append(snap.energy)
    inj = snap.injection
    if inj is not None and (inj.month_indexed or inj.spp_indexed):
        legs.append(inj)
    return legs


def is_settled(snap: SupplierSnapshot) -> bool:
    """Whether every leg ``snap`` indexes on the month carries the index the
    month settled at."""
    legs = _indexed_legs(snap)
    return bool(legs) and all(
        getattr(leg, "index_realised", None) is not None for leg in legs
    )


def settled_as(snap: SupplierSnapshot, settled: SupplierSnapshot) -> SupplierSnapshot:
    """``snap`` re-priced on the indices ``settled``, the same month's card,
    was settled at.

    A card re-parsed after a parser change comes back as printed; this puts
    back what its month settled at, read off the stored row rather than off
    a next card that may no longer be served. A leg ``settled`` holds no
    index for is left as it is.
    """
    index = getattr(settled.energy, "index_realised", None)
    energy = snap.energy if index is None else settled_energy(snap.energy, index)
    injection = snap.injection
    held = settled.injection
    if (
        injection is not None
        and held is not None
        and held.index_realised is not None
        and (injection.month_indexed or injection.spp_indexed)
    ):
        injection = settled_injection(injection, held.index_realised)
    return replace(snap, energy=energy, injection=injection)
