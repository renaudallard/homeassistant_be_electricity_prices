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
every supplier that settles a month that way, and for Bolt, whose variable
cards do the same on the delivery quarter.
"""

from __future__ import annotations

from collections.abc import Mapping
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
    0,9 EUR/MWh above both published series in every month, and Elegant's
    EPEXDAMSPP is the same series as theirs from May 2026 on. It is not a bug
    in the mean: Energy Knights defines its Belpex-SPP-M on the hourly
    quotation and the same computation reproduces its published series to
    0,007%, so the resolution is a property of the card. See
    ``spot_stats._spp_month_mean``.

    Shared by every provider whose card publishes the settled value: EBEM
    names it as "vorige maand", Trevion, Engie and Elegant name the month
    outright. A leg priced per time-of-use slot (Engie Empower Flextime) has
    each slot rebuilt from its own pair the same way.
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
    keyed entry bills the supplier's figure too. Any other leg, or one with
    no formula, comes back as it was.
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
    return replace(energy, index_realised=index, **rates)


# The rates of a leg billed on one formula at an index per meter register and
# CWaPE band (Bolt's variable cards).
_REGISTER_RATES = (
    "current",
    "peak",
    "offpeak",
    "exclusive_night",
    "impact_eco",
    "impact_medium",
    "impact_pic",
)


def settled_registers(
    energy: VariableRates, indices: Mapping[str, float]
) -> VariableRates | None:
    """A quarter-indexed leg recomputed at the indices its quarter settled at.

    Bolt prints one formula and bills it on an index per meter register and
    CWaPE band, the quarter's RLP-weighted mean over that register's hours,
    so each rate is rebuilt on the formula at its own index (``indices``, by
    rate). The single meter's is recorded as ``index_realised``, which is
    what a keyed entry's monthly leg resolves on; that leg reads each
    register's spread off these rates (``cohort_legs._quarter_leg``), so it
    bills them too. ``None``, settling nothing, when the leg has no formula
    or ``indices`` lacks a rate it carries.
    """
    factor, base = energy.formula_factor, energy.formula_base
    carried = [field for field in _REGISTER_RATES if getattr(energy, field) is not None]
    if factor is None or base is None or any(f not in indices for f in carried):
        return None
    rates: dict[str, Any] = {field: factor * indices[field] + base for field in carried}
    return replace(energy, index_realised=indices["current"], **rates)


def _register_indices(energy: VariableRates) -> dict[str, float]:
    """The index each rate of a leg :func:`settled_registers` settled stands
    at, read back through its formula."""
    factor, base = energy.formula_factor, energy.formula_base
    if not factor or base is None:
        return {}
    return {
        field: (rate - base) / factor
        for field in _REGISTER_RATES
        if (rate := getattr(energy, field)) is not None
    }


# The feed-in figures a Bolt variable card prints per meter register.
_SPP_REGISTER_RATES = ("current", "peak", "offpeak")


def settled_spp_registers(
    inj: InjectionRates, indices: Mapping[str, float]
) -> InjectionRates:
    """A feed-in leg crediting its variable settlement per meter register
    (``InjectionRates.spp_redistributed``) rebuilt on ``indices``, each
    register's SPP-weighted index by the field it prices, and marked
    ``spp_settled``.

    The formula stays on the leg for the dynamic settlement, and no
    ``index_realised`` is recorded: on that leg it would stand in for every
    slot's spot (``spot_stats._spp_injection_spot``). Only the figures the
    leg carries are rebuilt, so a Walloon leg keeps its single register; a
    leg with no formula, or ``indices`` lacking one of them, comes back as
    it was.
    """
    carried = [f for f in _SPP_REGISTER_RATES if getattr(inj, f) is not None]
    if inj.factor is None or inj.base is None or any(f not in indices for f in carried):
        return inj
    rates: dict[str, Any] = {
        field: inj.factor * indices[field] + inj.base for field in carried
    }
    return replace(inj, spp_settled=True, **rates)


def settled_stand_in(
    inj: InjectionRates, reference: InjectionRates
) -> InjectionRates | None:
    """``inj``, a feed-in leg crediting figures per register, standing in for
    a closed month, rebuilt on the indices ``reference`` prints for that
    month's quarter, or ``None`` where it prints no single-register one.

    ``reference`` is a card of the quarter after, whose figures per register
    are the formula at those indices (``_spp_register_indices``). A figure it
    prints no index for is dropped rather than left at the current card's
    quarter, so a two-register meter is credited the quarter's single figure.
    """
    indices = _spp_register_indices(reference)
    if "current" not in indices:
        return None
    unread: dict[str, Any] = {f: None for f in _SPP_REGISTER_RATES if f not in indices}
    return settled_spp_registers(replace(inj, **unread), indices)


def settles_registers(inj: InjectionRates) -> bool:
    """Whether a feed-in leg credits figures per register that its quarter
    settles (``InjectionRates.spp_redistributed``): one carrying the formula
    they are rebuilt on, which a card printing none does not."""
    return inj.spp_redistributed and inj.factor is not None and inj.base is not None


def _spp_register_indices(inj: InjectionRates) -> dict[str, float]:
    """The index each register figure of a leg :func:`settled_spp_registers`
    built stands at, read back through its formula."""
    if not inj.factor or inj.base is None:
        return {}
    return {
        field: (rate - inj.base) / inj.factor
        for field in _SPP_REGISTER_RATES
        if (rate := getattr(inj, field)) is not None
    }


def indexed_energy(energy: object) -> bool:
    """Whether an energy leg is indexed on its delivery month, or on its
    delivery quarter (Bolt's variable cards), and so settles once that has
    closed."""
    return bool(
        getattr(energy, "month_indexed", False)
        or getattr(energy, "quarter_indexed", False)
    )


def _indexed_legs(snap: SupplierSnapshot) -> list[EnergyRates | InjectionRates]:
    """The legs of ``snap`` its card indexes on the month or the quarter, and
    so settles."""
    legs: list[EnergyRates | InjectionRates] = []
    if indexed_energy(snap.energy):
        legs.append(snap.energy)
    inj = snap.injection
    if inj is not None and (
        inj.month_indexed or inj.spp_indexed or settles_registers(inj)
    ):
        legs.append(inj)
    return legs


def _leg_settled(leg: EnergyRates | InjectionRates) -> bool:
    """Whether one indexed leg carries what its month settled at: the index,
    or for figures per register the mark they were rebuilt with."""
    if isinstance(leg, InjectionRates) and leg.spp_redistributed:
        return leg.spp_settled
    return getattr(leg, "index_realised", None) is not None


def is_settled(snap: SupplierSnapshot) -> bool:
    """Whether every leg ``snap`` indexes on the month carries what the
    month settled at.

    Every leg: a Bolt variable month whose energy settled on the quarter's
    table while its feed-in figures stayed on the quarter before is still
    asked again, by the month cache and by the card archive alike."""
    legs = _indexed_legs(snap)
    return bool(legs) and all(_leg_settled(leg) for leg in legs)


def settled_as(snap: SupplierSnapshot, settled: SupplierSnapshot) -> SupplierSnapshot:
    """``snap`` re-priced on the indices ``settled``, the same month's card,
    was settled at.

    A card re-parsed after a parser change comes back as printed; this puts
    back what its month settled at, read off the stored row rather than off
    a next card that may no longer be served. A leg ``settled`` holds no
    index for is left as it is. A quarter-indexed leg settled on an index
    per register takes each back from the rate it was settled to, and so do
    feed-in figures per register, but only where the stored ones were
    settled (``spp_settled``): figures a row printed for the quarter before,
    or a row older than the figures themselves, are left as parsed, so the
    month is asked again (``is_settled``).
    """
    held = settled.energy
    index = getattr(held, "index_realised", None)
    energy = snap.energy if index is None else settled_energy(snap.energy, index)
    if (
        index is not None
        and isinstance(energy, VariableRates)
        and isinstance(held, VariableRates)
        and energy.quarter_indexed
    ):
        energy = settled_registers(energy, _register_indices(held)) or energy
    injection = snap.injection
    held_injection = settled.injection
    if (
        injection is not None
        and held_injection is not None
        and held_injection.index_realised is not None
        and (injection.month_indexed or injection.spp_indexed)
    ):
        injection = settled_injection(injection, held_injection.index_realised)
    if (
        injection is not None
        and held_injection is not None
        and injection.spp_redistributed
        and held_injection.spp_settled
    ):
        # Settled off the energy leg's table and recorded in the figures and
        # their mark (``settled_spp_registers``).
        injection = settled_spp_registers(
            injection, _spp_register_indices(held_injection)
        )
    return replace(snap, energy=energy, injection=injection)
