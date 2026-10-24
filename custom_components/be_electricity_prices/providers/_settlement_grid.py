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

"""The settlement grid a household chose, for a supplier that sells one card
on two (``resolve_settlement_grid``): the formula of a dynamic card moved
to the quarter-hours, and Bolt's variable card read as either of its
settlements, the feed-in included.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ._rates import DynamicRates, VariableRates
from .base import SupplierSnapshot


def resolve_settlement_grid(
    snapshot: SupplierSnapshot, *, quarter_hourly: bool, impact: bool = False
) -> SupplierSnapshot:
    """Move a dynamic card onto the 15-minute grid the entry settles on.

    Identity unless the household actually asked for it, which is every entry
    whose supplier does not offer the choice, so the cost is one boolean for
    all of them.

    Two legs can move, because the suppliers that offer the choice print two
    different defaults:

    * :class:`DynamicRates` on the hourly grid, which is Frank Energie. Its
      card prints one formula against ``BELPEX per uur``; ticking the box
      applies the same coefficients to the quarter-hourly index instead.
    * :class:`VariableRates` carrying the coefficients of its own indexation
      formula, which is Bolt. Its card prints a resolved monthly price AND the
      ``Belpex * factor + base`` formula behind it, and says the customer
      chooses whether that formula is settled per quarter-hour or against the
      RLP-weighted month. Ticking the box takes the second reading, so the leg
      becomes the dynamic one the card describes rather than the printed
      monthly figure.

    Anything else comes back untouched: a card that already prints a
    quarter-hourly index carries ``quarter_hourly`` from its own parser, a
    fixed leg has no formula to settle, and a variable card that exposes only
    a resolved rate has no coefficients to move onto the spot.

    Deliberately applied here rather than in the extractor. The grid is an
    account setting the supplier lets the customer flip (Frank monthly through
    its app, Bolt as a settlement option on the same contract), so the card
    cannot say which side a given household is on, and a second contract id
    per product would ask the user to re-pick their contract to change a
    billing preference. Resolving it beside the VAT treatment and the excise
    band keeps it on every path that produces a snapshot, the cached and
    archived ones included, so unticking the box takes effect without a
    refetch.

    Note this changes the leg's TYPE, which is why the contract's effective
    kind has to move with it: see ``offers_quarter_hourly`` and
    ``effective_kind``.

    The feed-in leg moves the other way. Bolt's variable card credits it per
    quarter-hour on the dynamic settlement and, on the variable one, at the
    SPP-weighted figures it prints per meter register
    (``InjectionRates.spp_redistributed``). The leg is parsed as the
    per-slot one, so a household left unticked has it turned into those
    register rates here, with the formula dropped so that no spot reaches
    it. A Walloon card prints them per CWaPE band of its Tarif Impact too,
    which are kept for a household on that tariff (``impact``) alone: a
    Walloon day/night meter is credited the single figure.
    """
    if not quarter_hourly:
        return _spp_redistributed(snapshot, impact=impact)
    energy = snapshot.energy
    if isinstance(energy, DynamicRates):
        if energy.quarter_hourly:
            return snapshot
        return replace(snapshot, energy=replace(energy, quarter_hourly=True))
    if isinstance(energy, VariableRates) and energy.formula_factor is not None:
        return replace(
            snapshot,
            energy=DynamicRates(
                factor=energy.formula_factor,
                base=energy.formula_base or 0.0,
                yearly_fixed_fee=energy.yearly_fixed_fee,
                quarter_hourly=True,
            ),
        )
    return snapshot


def _spp_redistributed(snapshot: SupplierSnapshot, *, impact: bool) -> SupplierSnapshot:
    """``snapshot`` with a feed-in leg the card credits per register on its
    variable settlement turned into those register rates: ``current`` for a
    single register, ``peak`` and ``offpeak`` read by register for a meter
    that has two (``bi_hourly``), and the figures per CWaPE band for an
    entry on the Impact tariff, dropped for any other."""
    inj = snapshot.injection
    if inj is None or not inj.spp_redistributed or not inj.slot_indexed:
        return snapshot
    bands: dict[str, Any] = (
        {} if impact else dict.fromkeys(("impact_pic", "impact_medium", "impact_eco"))
    )
    return replace(
        snapshot,
        injection=replace(
            inj,
            factor=None,
            base=None,
            slot_indexed=False,
            bi_hourly=inj.peak is not None and inj.offpeak is not None,
            **bands,
        ),
    )
