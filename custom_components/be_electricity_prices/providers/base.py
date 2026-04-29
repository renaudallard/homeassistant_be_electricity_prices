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

"""Per-supplier extractor protocol and shared dataclasses.

Each supplier exposes a module under ``providers/`` that:

  - declares the contracts it sells (id, label, kind),
  - fetches the *current* tariff card from the supplier's own publication,
  - parses out the energy formula plus the network / tax / capacity
    overlay for every relevant DSO sub-area.

The coordinator picks the configured contract + DSO and feeds the result
into ``pricing.compute_breakdown``.

No EUR values live in Python source - everything in :class:`SupplierSnapshot`
comes from a live fetch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

import aiohttp

TariffKind = Literal["fixed", "variable", "dynamic"]


@dataclass(frozen=True, kw_only=True)
class Contract:
    """One product sold by a supplier."""

    id: str
    label: str
    kind: TariffKind


@dataclass(frozen=True, kw_only=True)
class FixedRates:
    """Fixed energy contract: constant EUR/kWh, optionally bi-hourly."""

    single: float
    peak: float | None = None
    offpeak: float | None = None
    exclusive_night: float | None = None
    yearly_fixed_fee: float = 0.0


@dataclass(frozen=True, kw_only=True)
class VariableRates:
    """Variable energy contract: current month's effective EUR/kWh."""

    current: float
    yearly_fixed_fee: float = 0.0
    formula: str | None = None


@dataclass(frozen=True, kw_only=True)
class DynamicRates:
    """Dynamic (hourly) energy contract: factor x spot + base."""

    factor: float
    base: float
    yearly_fixed_fee: float = 0.0


EnergyRates = FixedRates | VariableRates | DynamicRates


@dataclass(frozen=True, kw_only=True)
class DsoOverlay:
    """Network + capacity costs for one DSO sub-area, in EUR/kWh and EUR/kW/yr."""

    distribution_single: float
    distribution_peak: float | None = None
    distribution_offpeak: float | None = None
    transport: float
    data_management_per_year: float = 0.0
    capacity_eur_per_kw_year: float | None = None


@dataclass(frozen=True, kw_only=True)
class TaxOverlay:
    """Federal + regional levies, all in EUR/kWh except the energy fund."""

    federal_excise: float
    energy_contribution: float
    regional_renewables: float = 0.0
    region_connection_fee: float = 0.0
    energy_fund_eur_per_month: float = 0.0
    vat_rate: float = 0.06


@dataclass(frozen=True, kw_only=True)
class SupplierSnapshot:
    """Everything extracted from one supplier's tariff card.

    A snapshot is per (supplier, contract). The coordinator combines it
    with the user's selected DSO sub-area to produce the all-in price.
    """

    supplier: str
    contract: str
    energy: EnergyRates
    dsos: dict[str, DsoOverlay]
    taxes: TaxOverlay
    source_url: str
    fetched_at_iso: str
    publication_label: str = ""


SnapshotFetcher = Callable[[aiohttp.ClientSession, str], Awaitable[SupplierSnapshot]]


@dataclass(frozen=True, kw_only=True)
class SupplierExtractor:
    """Registry entry for one supplier."""

    id: str
    label: str
    contracts: tuple[Contract, ...]
    fetch: SnapshotFetcher
    dso_keys: tuple[str, ...] = field(default_factory=tuple)


class SupplierProtocol(Protocol):
    """Each supplier module must expose a top-level ``EXTRACTOR`` of this shape."""

    EXTRACTOR: SupplierExtractor


class ExtractorError(Exception):
    """Raised when a supplier's source cannot be fetched or parsed."""
