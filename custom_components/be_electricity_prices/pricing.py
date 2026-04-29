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

"""All-in price formula given a supplier snapshot, a DSO and a region.

Pure functions, no Home Assistant dependencies.

Meter types follow the Belgian convention:

  - ``mono``    - single-rate meter (compteur simple / enkelvoudige meter).
                  Energy and distribution billed at the supplier's single rate.
  - ``bi``      - bi-hourly meter (compteur bi-horaire / tweevoudige meter).
                  Day rate weekdays 07:00-22:00, night rate the rest of the
                  time and full weekends.
  - ``dynamic`` - smart meter (digitale meter) capable of hourly readings.
                  For dynamic contracts, energy is computed as
                  ``factor x spot + base`` per hour. For fixed or variable
                  contracts on a smart meter, billing degrades to the
                  single rate (smart metering does not by itself imply
                  time-of-use pricing).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .providers.base import (
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)

MeterType = Literal["mono", "bi", "dynamic"]


@dataclass(frozen=True)
class PriceBreakdown:
    """All-in EUR/kWh decomposition for a single hour."""

    energy: float
    network: float
    taxes: float
    all_in: float


def is_offpeak(when: datetime) -> bool:
    """Belgian bi-hourly convention: weekdays 22:00-07:00 and weekends."""
    if when.weekday() >= 5:
        return True
    return when.hour < 7 or when.hour >= 22


def energy_eur_per_kwh(
    energy: EnergyRates,
    when: datetime,
    spot_eur_per_kwh: float | None,
    meter: MeterType = "mono",
) -> float:
    """Return the energy component in EUR/kWh for the given hour."""
    if isinstance(energy, FixedRates):
        if meter == "bi" and energy.peak is not None and energy.offpeak is not None:
            return energy.offpeak if is_offpeak(when) else energy.peak
        return energy.single
    if isinstance(energy, VariableRates):
        if meter == "bi" and energy.peak is not None and energy.offpeak is not None:
            return energy.offpeak if is_offpeak(when) else energy.peak
        return energy.current
    if isinstance(energy, DynamicRates):
        if spot_eur_per_kwh is None:
            raise ValueError("dynamic tariff needs a spot price")
        return energy.factor * spot_eur_per_kwh + energy.base
    raise TypeError(f"unknown energy rates type: {type(energy).__name__}")


def network_eur_per_kwh(
    dso: DsoOverlay,
    when: datetime,
    meter: MeterType = "mono",
) -> float:
    """Distribution + transport (EUR/kWh) for the given hour."""
    if (
        meter == "bi"
        and dso.distribution_peak is not None
        and dso.distribution_offpeak is not None
    ):
        dist = dso.distribution_offpeak if is_offpeak(when) else dso.distribution_peak
    else:
        dist = dso.distribution_single
    return dist + dso.transport


def taxes_eur_per_kwh(taxes: TaxOverlay, region: str) -> float:
    """Per-kWh levies for the configured region."""
    out = taxes.federal_excise + taxes.energy_contribution
    if region == "wallonia":
        out += taxes.region_connection_fee + taxes.regional_renewables
    elif region == "flanders":
        out += taxes.regional_renewables
    return out


def compute_breakdown(
    snapshot: SupplierSnapshot,
    dso_key: str,
    region: str,
    when: datetime,
    spot_eur_per_kwh: float | None = None,
    meter: MeterType = "mono",
) -> PriceBreakdown:
    """Return the all-in EUR/kWh breakdown for one hour."""
    overlay = snapshot.dsos.get(dso_key)
    if overlay is None:
        raise KeyError(
            f"DSO {dso_key!r} not in snapshot for "
            f"{snapshot.supplier}/{snapshot.contract}; "
            f"available: {sorted(snapshot.dsos)}"
        )
    energy = energy_eur_per_kwh(snapshot.energy, when, spot_eur_per_kwh, meter)
    network = network_eur_per_kwh(overlay, when, meter)
    taxes = taxes_eur_per_kwh(snapshot.taxes, region)
    pre_vat = energy + network + taxes
    all_in = pre_vat * (1.0 + snapshot.taxes.vat_rate)
    return PriceBreakdown(
        energy=energy,
        network=network,
        taxes=all_in - energy - network,
        all_in=all_in,
    )
