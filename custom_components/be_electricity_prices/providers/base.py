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
from datetime import date
from typing import Literal, Protocol

import aiohttp

from ..const import REGIONS

TariffKind = Literal["fixed", "variable", "dynamic", "tou"]

_ALL_REGIONS: frozenset[str] = frozenset(REGIONS)


@dataclass(frozen=True, kw_only=True)
class Contract:
    """One product sold by a supplier."""

    id: str
    label: str
    kind: TariffKind
    # Regions the product is actually published in. Defaults to all three;
    # extractors override per-contract for products that 404 outside their
    # home region (e.g. TotalEnergies Impact is Wallonia-only).
    regions: frozenset[str] = field(default_factory=lambda: _ALL_REGIONS)


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
    """Variable energy contract: current month's effective EUR/kWh.

    Suppliers that publish per-meter indicative monthly rates (e.g. Cociter)
    populate ``peak`` / ``offpeak`` so a bi-hourly meter gets its own rate.
    Suppliers that publish a single rate (e.g. Eneco Power Flex) leave them
    None and the pricing engine falls back to ``current`` for any meter type.
    """

    current: float
    peak: float | None = None
    offpeak: float | None = None
    exclusive_night: float | None = None
    yearly_fixed_fee: float = 0.0
    formula: str | None = None


@dataclass(frozen=True, kw_only=True)
class DynamicRates:
    """Dynamic (hourly) energy contract: factor x spot + base."""

    factor: float
    base: float
    yearly_fixed_fee: float = 0.0


WeekendRule = Literal["weekend_offpeak", "weekend_no_peak"]


@dataclass(frozen=True, kw_only=True)
class TimeOfUseRates:
    """Time-of-use energy contract: 3 slots by hour-of-day.

    Weekday rule is shared across products:
      peak       : 07:00-11:00 + 17:00-22:00
      transition : 11:00-17:00 + 22:00-01:00
      offpeak    : 01:00-07:00

    Weekend rule is product-dependent (``weekend_rule``):

      weekend_offpeak (Luminus SmartFlex):
        Saturday and Sunday are entirely off-peak.

      weekend_no_peak (Engie Empower Flextime):
        peak       : never
        transition : 07:00-11:00 + 17:00-01:00
        offpeak    : 01:00-07:00 + 11:00-17:00

    Requires a smart meter (SMR3). Like ``VariableRates``, the rates
    can be re-published monthly; the formula field carries the
    indexation expression if the supplier publishes one.
    """

    peak: float
    transition: float
    offpeak: float
    yearly_fixed_fee: float = 0.0
    formula: str | None = None
    weekend_rule: WeekendRule = "weekend_offpeak"


EnergyRates = FixedRates | VariableRates | DynamicRates | TimeOfUseRates


@dataclass(frozen=True, kw_only=True)
class InjectionRates:
    """Injection (solar feed-in) compensation, in EUR/kWh.

    Belgian residential injection is exempt from VAT, so values here are
    NEVER VAT-incl regardless of the consumption snapshot's vat_rate. At
    least one of (current, factor+base) must be populated:

      - ``current`` is the supplier's monthly indicative price (e.g. Eneco's
        "Maandprijs" of 4.76 c/kWh on Power Fix). Used when no live spot is
        available.
      - ``factor`` and ``base`` define the hourly formula
        ``injection_eur_per_kwh = factor * spot_eur_per_kwh + base``.
        Belgian formulas can produce negative values at low spot - the
        producer pays to inject - and the pricing engine respects that.
    """

    current: float | None = None
    factor: float | None = None
    base: float | None = None
    formula: str | None = None


@dataclass(frozen=True, kw_only=True)
class DsoOverlay:
    """Network + capacity costs for one DSO sub-area, in EUR/kWh and EUR/kW/yr."""

    distribution_single: float
    distribution_peak: float | None = None
    distribution_offpeak: float | None = None
    transport: float
    data_management_per_year: float = 0.0
    capacity_eur_per_kw_year: float | None = None
    # Prosumer (compensation-regime) tariff in EUR per kVA of solar inverter
    # capacity per year. Wallonia DSOs publish this; Flanders digital meters
    # don't (post-2024 SMR3 connections), so it stays None there. Valid in
    # Wallonia until 2030 per CWaPE.
    prosumer_eur_per_kva_year: float | None = None
    # Tarif Impact (Wallonia-only, opt-in for SMR3 customers). Three
    # distribution rates indexed by CWaPE-defined hour-of-day bands:
    #   pic    : 17:00-22:00            (highest, every day)
    #   medium : 07:00-11:00 + 22:00-01:00
    #   eco    : 01:00-07:00 + 11:00-17:00 (lowest, every day)
    # Wallonia DSOs publish all three on every supplier tariff card;
    # Brussels (Sibelga) and Flanders (Fluvius) do not, so they stay
    # None there.
    distribution_pic: float | None = None
    distribution_medium: float | None = None
    distribution_eco: float | None = None


@dataclass(frozen=True, kw_only=True)
class TaxOverlay:
    """Federal + regional levies, all in EUR/kWh except the energy fund.

    Regional renewables differ across the three regions: Flanders
    (cogen + green-energy surcharge, ~1.5 c/kWh), Wallonia (green energy
    contribution, ~3.1 c/kWh) and Brussels (green energy, ~2.7 c/kWh).
    The pricing engine picks the right one per region; an extractor that
    only operates in one or two of them leaves the others at 0.
    """

    federal_excise: float
    energy_contribution: float
    flanders_renewables: float = 0.0
    wallonia_renewables: float = 0.0
    brussels_renewables: float = 0.0
    region_connection_fee: float = 0.0
    energy_fund_eur_per_month: float = 0.0
    # 0.0 means the snapshot's prices are already VAT-incl (the convention
    # for both Eneco and Cociter today). An extractor that starts shipping
    # ex-VAT numbers must set this to the parsed rate explicitly.
    vat_rate: float = 0.0


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
    publication_label: str = ""
    injection: InjectionRates | None = None
    # Last calendar day the published rates apply to (typically the last
    # day of the supplier's pricing month). ``None`` when the extractor
    # couldn't parse a validity period from the card. Consumers that
    # need to know whether tomorrow's rates are *actually* the right
    # ones (the tomorrow_prices_available binary sensor, in particular)
    # check ``date.today() <= valid_until``; ``None`` means we don't
    # know, so callers should fall back to "treat as available".
    valid_until: date | None = None


SnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str], Awaitable[SupplierSnapshot]
]

# Cheap-probe contract: same return value across calls means the snapshot
# is still valid; a different value means refetch. ``None`` signals the
# supplier has no probe path the coordinator can rely on (Engie/Luminus
# API endpoints, DATS 24 single-PDF) and the time-based TTL takes over.
SnapshotProbe = Callable[[aiohttp.ClientSession, str, str], Awaitable[str | None]]

# Historical-fetch contract: fetch the published card for a specific
# (year, month). Used by the time-correct yearly-cost flow to bill each
# past month at its own rate. Returns ``None`` when the supplier has no
# accessible archive for that month (overwrite-in-place suppliers like
# OCTA+ / TotalEnergies, API-only suppliers like Engie / Luminus / DATS 24,
# or a month before the supplier's archive horizon).
ArchivedSnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str, "date"], Awaitable["SupplierSnapshot | None"]
]


@dataclass(frozen=True, kw_only=True)
class SupplierExtractor:
    """Registry entry for one supplier."""

    id: str
    label: str
    contracts: tuple[Contract, ...]
    fetch: SnapshotFetcher
    # Optional cheap probe (HEAD or listing GET) that returns a freshness
    # key. The coordinator calls it hourly and only re-runs ``fetch`` when
    # the key changes. ``None`` means no probe is available.
    probe: SnapshotProbe | None = None
    # Optional historical fetch: returns the published snapshot for a
    # given (year, month) so past consumption can be billed at the
    # correct historical rate. ``None`` (or a callable returning ``None``)
    # means "no archive for this month" - the coordinator falls back to
    # using the current snapshot as a proxy.
    fetch_for_month: ArchivedSnapshotFetcher | None = None

    def regions(self) -> frozenset[str]:
        """Union of regions across this supplier's contracts."""
        out: set[str] = set()
        for c in self.contracts:
            out |= c.regions
        return frozenset(out)


class SupplierProtocol(Protocol):
    """Each supplier module must expose a top-level ``EXTRACTOR`` of this shape."""

    EXTRACTOR: SupplierExtractor


class ExtractorError(Exception):
    """Raised when a supplier's source cannot be fetched or parsed."""
