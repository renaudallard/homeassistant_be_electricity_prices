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

"""Expert custom-formula supplier.

An escape hatch for suppliers that publish no public, machine-resolvable
tariff card (e.g. Yuso, the Mega iChoosr / Samen Overstappen groepsaankoop),
so the normal scrape-a-card path is impossible. The user types their own
commodity formula and all regulated DSO + tax values in the config flow, and
the coordinator builds the ``SupplierSnapshot`` locally from the entry rather
than fetching anything. There is no card, no probe and no archive, so ``fetch``
is a stub the coordinator never calls; this module exists only to surface the
supplier in the dropdown and to carry its per-mode contract catalogue.

Three contracts double as the energy-mode picker:

  * ``custom_dynamic``  - ``factor * live spot + base`` (kind ``dynamic``)
  * ``custom_monthly``  - ``factor * monthly-mean spot + base`` (kind
    ``spot_monthly``), a flat per-month rate for group-purchase products
  * ``custom_fixed``    - a flat manual rate (kind ``fixed``)

The dynamic and monthly modes are spot-indexed, so the config flow collects an
ENTSO-E API key for them (gated on the contract kind).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import aiohttp

from ..const import (
    CONF_CONNECTION_KVA_TIER,
    CONF_CONTRACT,
    CONF_CUSTOM_DSO_BRUSSELS_OSP,
    CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR,
    CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE,
    CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR,
    CONF_CUSTOM_DSO_TRANSPORT,
    CONF_CUSTOM_ENERGY_BASE,
    CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT,
    CONF_CUSTOM_ENERGY_FACTOR,
    CONF_CUSTOM_ENERGY_OFFPEAK,
    CONF_CUSTOM_ENERGY_PEAK,
    CONF_CUSTOM_ENERGY_QUARTER_HOURLY,
    CONF_CUSTOM_ENERGY_SINGLE,
    CONF_CUSTOM_INJECTION_BASE,
    CONF_CUSTOM_INJECTION_CURRENT,
    CONF_CUSTOM_INJECTION_FACTOR,
    CONF_CUSTOM_INJECTION_FLOOR,
    CONF_CUSTOM_INJECTION_MODE,
    CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION,
    CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH,
    CONF_CUSTOM_TAX_FEDERAL_EXCISE,
    CONF_CUSTOM_TAX_REGION_CONNECTION_FEE,
    CONF_CUSTOM_TAX_REGIONAL_RENEWABLES,
    CONF_CUSTOM_VAT_RATE,
    CONF_CUSTOM_YEARLY_FIXED_FEE,
    CONF_SOLAR_REGIME,
    CUSTOM_CONTRACT_DYNAMIC,
    CUSTOM_CONTRACT_FIXED,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_INJECTION_MODE_FORMULA,
    DEFAULT_CONNECTION_KVA_TIER,
    DEFAULT_CUSTOM_VAT_RATE,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SOLAR_REGIME_INJECTION,
    SUPPLIER_CUSTOM,
)
from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)

_CONTRACTS: tuple[Contract, ...] = (
    Contract(
        id=CUSTOM_CONTRACT_DYNAMIC,
        label="Dynamic (factor x spot + base)",
        kind="dynamic",
    ),
    Contract(
        id=CUSTOM_CONTRACT_MONTHLY,
        label="Monthly average (factor x monthly-mean spot + base)",
        kind="spot_monthly",
    ),
    Contract(
        id=CUSTOM_CONTRACT_FIXED,
        label="Fixed / manual rate",
        kind="fixed",
    ),
)


async def _fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """Never called: custom snapshots are assembled by the coordinator from
    the config entry (there is no card to fetch)."""
    raise ExtractorError(
        "custom-formula snapshots are built by the coordinator, not fetched"
    )


EXTRACTOR = SupplierExtractor(
    id=SUPPLIER_CUSTOM,
    label="Expert: custom formula (no public card)",
    contracts=_CONTRACTS,
    fetch=_fetch,
    probe=None,
    fetch_for_month=None,
)


# --- snapshot assembly from the config entry ---------------------------------
#
# The coordinator calls build_snapshot() every tick instead of fetching a card.
# Every EUR value is read from the entry the user filled in; missing optional
# fields default to 0.0 (a legitimate "unset" here, since the user is the
# source, not a scraped card).


def _num(data: Mapping[str, Any], key: str, default: float = 0.0) -> float:
    value = data.get(key)
    return float(value) if value is not None else default


def _opt(data: Mapping[str, Any], key: str) -> float | None:
    value = data.get(key)
    return float(value) if value is not None else None


def _build_energy(data: Mapping[str, Any], contract: str) -> EnergyRates:
    fee = _num(data, CONF_CUSTOM_YEARLY_FIXED_FEE)
    if contract == CUSTOM_CONTRACT_DYNAMIC:
        return DynamicRates(
            factor=_num(data, CONF_CUSTOM_ENERGY_FACTOR),
            base=_num(data, CONF_CUSTOM_ENERGY_BASE),
            yearly_fixed_fee=fee,
            quarter_hourly=bool(data.get(CONF_CUSTOM_ENERGY_QUARTER_HOURLY, False)),
        )
    if contract == CUSTOM_CONTRACT_MONTHLY:
        return SpotMonthlyRates(
            factor=_num(data, CONF_CUSTOM_ENERGY_FACTOR),
            base=_num(data, CONF_CUSTOM_ENERGY_BASE),
            yearly_fixed_fee=fee,
        )
    return FixedRates(
        single=_num(data, CONF_CUSTOM_ENERGY_SINGLE),
        peak=_opt(data, CONF_CUSTOM_ENERGY_PEAK),
        offpeak=_opt(data, CONF_CUSTOM_ENERGY_OFFPEAK),
        exclusive_night=_opt(data, CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT),
        yearly_fixed_fee=fee,
    )


def _build_dso(data: Mapping[str, Any], region: str) -> DsoOverlay:
    osp: dict[str, float] | None = None
    if region == REGION_BRUSSELS:
        tier = data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
        osp = {tier: _num(data, CONF_CUSTOM_DSO_BRUSSELS_OSP)}
    return DsoOverlay(
        distribution_single=_num(data, CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE),
        distribution_peak=_opt(data, CONF_CUSTOM_DSO_DISTRIBUTION_PEAK),
        distribution_offpeak=_opt(data, CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK),
        distribution_exclusive_night=_opt(
            data, CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT
        ),
        transport=_num(data, CONF_CUSTOM_DSO_TRANSPORT),
        data_management_per_year=_num(data, CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR),
        capacity_eur_per_kw_year=_opt(data, CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR),
        prosumer_eur_per_kva_year=_opt(data, CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR),
        distribution_pic=_opt(data, CONF_CUSTOM_DSO_DISTRIBUTION_PIC),
        distribution_medium=_opt(data, CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM),
        distribution_eco=_opt(data, CONF_CUSTOM_DSO_DISTRIBUTION_ECO),
        brussels_osp_by_tier=osp,
    )


def _build_taxes(data: Mapping[str, Any], region: str) -> TaxOverlay:
    renewables = _num(data, CONF_CUSTOM_TAX_REGIONAL_RENEWABLES)
    return TaxOverlay(
        federal_excise=_num(data, CONF_CUSTOM_TAX_FEDERAL_EXCISE),
        energy_contribution=_num(data, CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION),
        flanders_renewables=renewables if region == REGION_FLANDERS else 0.0,
        wallonia_renewables=renewables if region == REGION_WALLONIA else 0.0,
        brussels_renewables=renewables if region == REGION_BRUSSELS else 0.0,
        region_connection_fee=_num(data, CONF_CUSTOM_TAX_REGION_CONNECTION_FEE),
        energy_fund_eur_per_month=_num(data, CONF_CUSTOM_TAX_ENERGY_FUND_PER_MONTH),
        vat_rate=_num(data, CONF_CUSTOM_VAT_RATE, DEFAULT_CUSTOM_VAT_RATE),
    )


def _build_injection(data: Mapping[str, Any]) -> InjectionRates | None:
    if data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return None
    floor = bool(data.get(CONF_CUSTOM_INJECTION_FLOOR, False))
    if data.get(CONF_CUSTOM_INJECTION_MODE) == CUSTOM_INJECTION_MODE_FORMULA:
        # factor/base are applied against the live spot (dynamic) or the
        # delivery month's mean (monthly-average); the coordinator threads
        # the right value in and applies the floor.
        return InjectionRates(
            factor=_num(data, CONF_CUSTOM_INJECTION_FACTOR),
            base=_num(data, CONF_CUSTOM_INJECTION_BASE),
            floor_at_zero=floor,
        )
    return InjectionRates(
        current=_num(data, CONF_CUSTOM_INJECTION_CURRENT),
        floor_at_zero=floor,
    )


def build_snapshot(data: Mapping[str, Any], region: str, dso: str) -> SupplierSnapshot:
    """Assemble a SupplierSnapshot from a custom (expert) config entry.

    There is no card to fetch: the user typed the commodity formula and every
    regulated DSO + tax value, so the whole snapshot is built from the entry.
    The coordinator calls this every tick (cheap, pure). For the monthly-average
    mode the energy carries only factor/base; the coordinator threads the
    delivery month's mean spot through pricing.
    """
    contract = str(data.get(CONF_CONTRACT, CUSTOM_CONTRACT_FIXED))
    return SupplierSnapshot(
        supplier=SUPPLIER_CUSTOM,
        contract=contract,
        energy=_build_energy(data, contract),
        dsos={dso: _build_dso(data, region)},
        taxes=_build_taxes(data, region),
        source_url="custom formula (config entry)",
        publication_label="Custom formula",
        injection=_build_injection(data),
    )
