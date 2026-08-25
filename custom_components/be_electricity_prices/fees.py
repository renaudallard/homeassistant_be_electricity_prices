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

"""Standing charges: the capacity tariff, the Brussels OSP fee, the prosumer
forfait and the annual fixed fees.

Split out of coordinator.py. A true leaf -- nothing here imports another module
of this package beyond const and providers, and the live sensor, the
year-to-date walk, the backfill and the compare quote all read these, which is
why they must not be duplicated per caller."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_CONNECTION_KVA_TIER,
    CONF_DSO,
    CONNECTION_KVA_TIERS_ABOVE_13,
    CONF_DSO_TARIFF_MODE,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    DEFAULT_CONNECTION_KVA_TIER,
    DSO_MODE_IMPACT,
    REGION_WALLONIA,
    SOLAR_REGIME_COMPENSATION,
)
from .pricing import (
    MeterType,
    yearly_fixed_fee_for_meter,
)
from .providers.base import (
    DsoOverlay,
    SupplierSnapshot,
)


def _capacity_monthly_eur(overlay: DsoOverlay | None, peak_kw: float) -> float:
    """One month of the Flemish capacity charge, or 0.0 when it isn't billed.

    The annual EUR/kW rate over twelve, with the two "nothing to bill" cases
    folded in: no overlay for this DSO, or a card that prints no capacity row.

    Shared because three paths bill this and must agree -- the live tick, the
    year-to-date walk and the backfill's per-hour accrual. ``_annual_static_fees``
    is shared across the same three for the same reason; capacity is the fee
    that was left out of it, so it drifted here instead. Deliberately region
    -agnostic: each caller keeps its own Flanders gate.
    """
    if overlay is None or overlay.capacity_eur_per_kw_year is None:
        return 0.0
    return peak_kw * overlay.capacity_eur_per_kw_year / 12.0


def _compute_capacity(
    snapshot: SupplierSnapshot, entry: ConfigEntry, peak_kw: float
) -> float:
    # Read CONF_DSO defensively: a corrupt entry that lost the key
    # would otherwise KeyError here and tear the whole tick down via
    # UpdateFailed. _compute_prosumer already takes the same shape.
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0
    return _capacity_monthly_eur(snapshot.dsos.get(dso), peak_kw)


def _brussels_osp_fee(overlay: DsoOverlay | None, entry: ConfigEntry) -> float:
    """Brussels Brugel OSP annual fee (EUR/year) for the configured tier.

    The fee is a flat Sibelga charge scaled by contractual connection power;
    the user picks the tier in the config flow (default 1.44-6.00 kVA).
    Returns 0 outside Brussels or when the card omits the OSP table."""
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)


def _brussels_power_term(
    overlay: DsoOverlay | None, entry: ConfigEntry
) -> float | None:
    """Sibelga's power term for this entry's connection, in EUR/year.

    The card prints two columns, at or below 13 kVA and above it, and only the
    first was ever billed. A 3x400 V / 25 A residential connection is 17,3 kVA
    and belongs in the second, so a household with a heat pump or a charger was
    billed the smaller term.

    ``None`` when the card prints a single column or the entry is not above the
    line, and the caller keeps using ``data_management_per_year``.
    """
    if overlay is None or overlay.brussels_power_term_above_13kva is None:
        return None
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    if tier not in CONNECTION_KVA_TIERS_ABOVE_13:
        return None
    return overlay.brussels_power_term_above_13kva


def _walloon_fixed_term_applies(entry: ConfigEntry) -> bool:
    """Whether this entry pays the Walloon DSO's ``terme fixe``.

    The CWaPE tariff sheets print two configurations per DSO. The standard
    one carries a fixed term (row C, ``terme fixe``, in EUR/year); the
    incitative one, which the cards sell as the ``IMPACT`` tariff, carries a
    dash there and charges a capacity term instead. The suppliers say so on
    the cards: "le terme fixe n'est pas d'application pour le tarif IMPACT".

    Nothing offsets the difference, because CWaPE set that capacity term to
    0 EUR/kW for 2026 through 2029 on all five Walloon DSOs, so an entry on
    the incitative configuration simply has no fixed term to pay.

    Region-gated, because ``data_management_per_year`` is one field carrying
    three different charges: the Walloon terme fixe, the Flemish databeheer
    and the Brussels mesure plus fixed-term pair. Only the first one is tied
    to the tariff configuration.
    """
    if entry.data.get(CONF_REGION) != REGION_WALLONIA:
        return True
    return entry.data.get(CONF_DSO_TARIFF_MODE) != DSO_MODE_IMPACT


def _annual_static_fees(
    snapshot: SupplierSnapshot, meter: MeterType, entry: ConfigEntry
) -> float:
    """Fixed EUR/year fees that do not depend on consumption: the supplier
    yearly fixed fee (for ``meter``), twelve times the monthly energy-fund
    levy, the digital-meter data-management charge and the Brussels Brugel
    OSP fee.

    Shared by the live YTD sensor, the backfill accrual and the config-flow
    annual estimate so a new static-fee component is added in one place
    instead of drifting between the three paths. That is also why the
    Walloon IMPACT exemption belongs here: the per-kWh legs already read the
    tariff mode, and this was the one leg that did not, so the fixed term was
    billed on all four paths at once.
    """
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    fixed_term = (
        overlay.data_management_per_year
        if overlay is not None and _walloon_fixed_term_applies(entry)
        else 0.0
    )
    above_13 = _brussels_power_term(overlay, entry)
    if above_13 is not None:
        fixed_term = above_13
    return (
        float(yearly_fixed_fee_for_meter(snapshot.energy, meter) or 0.0)
        + 12.0 * float(snapshot.taxes.energy_fund_eur_per_month or 0.0)
        + fixed_term
        + _brussels_osp_fee(overlay, entry)
    )


def _prosumer_monthly_fee(
    overlay: DsoOverlay | None, snapshot: SupplierSnapshot, kva: float
) -> float:
    """Monthly prosumer (compensation-regime) fee for ``kva`` of inverter.

    Sums the DSO per-kVA/year tariff and the supplier-side compensation
    forfait (Cociter Variable), the latter already TVAC so it is summed
    raw, then divides to a monthly amount. Callers gate this to Walloon
    compensation installs; a missing rate contributes zero.
    """
    dso_rate = (
        overlay.prosumer_eur_per_kva_year
        if overlay is not None and overlay.prosumer_eur_per_kva_year is not None
        else 0.0
    )
    supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year or 0.0
    return kva * (dso_rate + supplier_rate) / 12.0


def _compensation_kva(entry: ConfigEntry) -> float:
    """Inverter kVA this entry bills the prosumer fee on, else 0.0.

    The whole eligibility gate in one place: the compensation regime, Wallonia,
    and a kVA that parses above zero. It was written out three times -- the
    live tick, the year-to-date walk and the backfill, the last one with the
    kVA half in its own helper and the region half 580 lines away from it.

    Compensation is Walloon-only: a Flanders PV owner is either on net metering
    (not modelled here) or on a digital meter paying the capaciteitstarief, and
    billing the prosumer fee there too would double-count grid recovery.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_COMPENSATION:
        return 0.0
    if entry.data.get(CONF_REGION) != REGION_WALLONIA:
        return 0.0
    try:
        kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0
    return kva if kva > 0.0 else 0.0


def _compute_prosumer(snapshot: SupplierSnapshot, entry: ConfigEntry) -> float:
    """Monthly prosumer (compensation regime) cost in EUR.

    Only Walloon installations certified before 2024-01-01 are under the
    compensation regime, and only until 2030-12-31. Post-2024 installations
    are on the injection tariff (no per-kVA fee). Returns 0 when:
      - the user has no solar (kVA <= 0),
      - the regime is not 'compensation',
      - the configured DSO has no prosumer rate in the snapshot
        (Flemish digital meters, Cociter SMR3 dynamic).
    """
    kva = _compensation_kva(entry)
    if not kva:
        return 0.0
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    return _prosumer_monthly_fee(overlay, snapshot, kva)
