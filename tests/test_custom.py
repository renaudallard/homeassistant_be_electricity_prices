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

"""Expert custom-formula supplier: config flow, snapshot build, pricing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.compare_flow import (
    _compare_supplier_options,
)
from custom_components.be_electricity_prices.coordinator import (
    BePricesCoordinator,
)
from custom_components.be_electricity_prices.flow_schemas_custom import (
    _custom_dso_schema,
    _custom_energy_schema,
)
from custom_components.be_electricity_prices.injection import (
    _bake_monthly_injection,
    _floor_injection,
)
from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh
from custom_components.be_electricity_prices.providers._rates import (
    DynamicRates,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.base import ExtractorError
from custom_components.be_electricity_prices.providers.custom import (
    EXTRACTOR,
    build_snapshot,
)
from custom_components.be_electricity_prices.snapshot_codec import (
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from custom_components.be_electricity_prices.spot_stats import (
    _mean_of_month,
)
from tests import make_entry

WHEN = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


# ---- build_snapshot ----------------------------------------------------------


def test_build_snapshot_dynamic() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_DYNAMIC,
        const.CONF_CUSTOM_ENERGY_FACTOR: 1.0,
        const.CONF_CUSTOM_ENERGY_BASE: 0.02,
        const.CONF_CUSTOM_ENERGY_QUARTER_HOURLY: True,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
        const.CONF_CUSTOM_TAX_FEDERAL_EXCISE: 0.005,
        const.CONF_CUSTOM_VAT_RATE: 0.06,
    }
    snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.factor == 1.0 and snap.energy.base == 0.02
    assert snap.energy.quarter_hourly is True
    assert snap.dsos[const.DSO_FLUVIUS_ANTWERPEN].distribution_single == 0.05
    assert snap.taxes.federal_excise == 0.005
    assert snap.taxes.vat_rate == 0.06
    assert snap.injection is None  # no injection regime


def test_build_snapshot_monthly_with_injection() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
        const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
        const.CONF_CUSTOM_ENERGY_BASE: 0.0,
        const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
        const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
        const.CONF_CUSTOM_INJECTION_BASE: -0.009,
        const.CONF_CUSTOM_INJECTION_FLOOR: True,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
    }
    snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.energy.factor == 1.0834
    assert snap.injection is not None
    assert snap.injection.factor == 0.96 and snap.injection.base == -0.009
    assert snap.injection.floor_at_zero is True
    # spot-monthly energy prices to factor * mean + base
    assert energy_eur_per_kwh(snap.energy, WHEN, 0.08) == pytest.approx(1.0834 * 0.08)


def test_build_snapshot_fixed_with_a_month_indexed_injection() -> None:
    """A fixed consumption price whose feed-in credit is a formula on the
    month's mean: the leg says it is month-indexed, which is what makes the
    engine fetch spots for it, settle it on the month's mean and never on the
    hour's spot, while the energy leg needs none. A flat credit stays flat,
    and the monthly mode's formula rides its energy leg's mean as before."""
    from custom_components.be_electricity_prices.injection import (
        _injection_is_spot_formula,
        _injection_needs_month_spot,
    )
    from custom_components.be_electricity_prices.spot_stats import (
        _injection_on_month_mean,
    )
    from tests import make_entry

    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.12,
        const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
        const.CONF_CUSTOM_INJECTION_FACTOR: 0.9,
        const.CONF_CUSTOM_INJECTION_BASE: -0.012,
        const.CONF_CUSTOM_INJECTION_FLOOR: True,
    }
    snap = build_snapshot(data, const.REGION_BRUSSELS, const.DSO_SIBELGA)
    assert isinstance(snap.energy, FixedRates)
    inj = snap.injection
    assert inj is not None
    assert (inj.factor, inj.base, inj.current) == (0.9, -0.012, None)
    assert inj.floor_at_zero and inj.month_indexed and not inj.spp_indexed
    entry = make_entry(solar_regime=const.SOLAR_REGIME_INJECTION)
    assert _injection_on_month_mean(snap)
    assert _injection_needs_month_spot(snap, entry)
    assert not _injection_is_spot_formula(inj, snap.energy)

    flat = build_snapshot(
        {
            **data,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_CURRENT,
            const.CONF_CUSTOM_INJECTION_CURRENT: 0.04,
        },
        const.REGION_BRUSSELS,
        const.DSO_SIBELGA,
    )
    assert flat.injection is not None
    assert flat.injection.current == 0.04 and flat.injection.factor is None
    assert not flat.injection.month_indexed

    monthly = build_snapshot(
        {
            **data,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0,
            const.CONF_CUSTOM_ENERGY_BASE: 0.0,
        },
        const.REGION_BRUSSELS,
        const.DSO_SIBELGA,
    )
    assert monthly.injection is not None and not monthly.injection.month_indexed
    assert _injection_on_month_mean(monthly)


def test_build_snapshot_fixed_routes_regional_renewables() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
        const.CONF_CUSTOM_TAX_REGIONAL_RENEWABLES: 0.031,
    }
    snap = build_snapshot(data, const.REGION_WALLONIA, const.DSO_ORES)
    assert isinstance(snap.energy, FixedRates)
    assert snap.energy.single == 0.30
    # the single renewables field lands in the region's slot
    assert snap.taxes.wallonia_renewables == 0.031
    assert snap.taxes.flanders_renewables == 0.0
    assert snap.taxes.brussels_renewables == 0.0


def test_build_snapshot_brussels_osp_tier() -> None:
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CONNECTION_KVA_TIER: const.CONNECTION_KVA_TIER_LE13,
        const.CONF_CUSTOM_DSO_BRUSSELS_OSP: 42.0,
        const.CONF_CUSTOM_VAT_RATE: 0.0,  # isolate the tier routing from the VAT bake
    }
    snap = build_snapshot(data, const.REGION_BRUSSELS, const.DSO_SIBELGA)
    osp = snap.dsos[const.DSO_SIBELGA].brussels_osp_by_tier
    assert osp == {const.CONNECTION_KVA_TIER_LE13: 42.0}


# ---- helpers -----------------------------------------------------------------


def test_mean_of_month_filters_by_local_month() -> None:
    spots = {
        datetime(2026, 7, 1, 10, tzinfo=UTC): 0.10,
        datetime(2026, 7, 2, 10, tzinfo=UTC): 0.20,
        datetime(2026, 6, 30, 10, tzinfo=UTC): 99.0,  # excluded
    }
    assert _mean_of_month(spots, 2026, 7) == pytest.approx(0.15)
    assert _mean_of_month(spots, 2026, 5) is None


def test_bake_monthly_injection_and_floor() -> None:
    snap = build_snapshot(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
        const.REGION_FLANDERS,
        const.DSO_FLUVIUS_ANTWERPEN,
    )
    # mean 0.005 -> 0.96*0.005 - 0.009 = -0.0042, baked into a flat current
    baked = _bake_monthly_injection(snap, 0.005)
    assert baked.injection is not None
    assert baked.injection.factor is None and baked.injection.base is None
    raw = baked.injection.current
    assert raw == pytest.approx(0.96 * 0.005 - 0.009)
    # floor clamps the negative to 0
    assert _floor_injection(raw, baked.injection) == 0.0
    # a cold-start None mean bakes to None (injection unavailable this tick)
    cold = _bake_monthly_injection(snap, None)
    assert cold.injection is not None
    assert cold.injection.current is None


def test_the_zero_floor_can_never_meet_a_tou_injection() -> None:
    """The floor is applied on the formula and indicative branches, never on
    the per-slot TOU triplet.

    That asymmetry is only safe while the two cannot meet. The custom expert
    supplier is the one thing that sets ``floor_at_zero``, and it has no TOU
    shape on either leg; the one card that publishes a triplet (Engie Empower
    Flextime) sets no floor. Pinned here rather than guarded in the pricing
    code, which would be a dead branch in the most shape-sensitive module in
    the package."""
    from custom_components.be_electricity_prices.providers._rates import TimeOfUseRates

    for contract in const.CUSTOM_CONTRACTS:
        for mode in (
            const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CUSTOM_INJECTION_MODE_CURRENT,
        ):
            snap = build_snapshot(
                {
                    const.CONF_CONTRACT: contract,
                    const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
                    const.CONF_CUSTOM_INJECTION_MODE: mode,
                    const.CONF_CUSTOM_INJECTION_FLOOR: True,
                    const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
                    const.CONF_CUSTOM_INJECTION_BASE: -0.009,
                    const.CONF_CUSTOM_INJECTION_CURRENT: 0.04,
                },
                const.REGION_FLANDERS,
                const.DSO_FLUVIUS_ANTWERPEN,
            )
            assert snap.injection is not None
            assert snap.injection.floor_at_zero is True
            assert snap.injection.peak is None
            assert not isinstance(snap.energy, TimeOfUseRates)


def test_impact_boxes_left_blank_do_not_bill_zero_distribution() -> None:
    """The three CWaPE Impact boxes had a 0,00 default, so a Walloon custom
    entry that filled in only the single rate stored three zeros.
    network_eur_per_kwh takes the Impact branch as soon as all three are
    non-None, so those zeros did not fall back: they billed no distribution
    at all, in every band, every hour."""
    from custom_components.be_electricity_prices.flow_schemas_custom import (
        _custom_dso_schema,
    )

    schema = _custom_dso_schema(
        {"region": "wallonia", "meter": "mono", "dso_tariff_mode": "impact"}
    )
    stored = schema(
        {"custom_dso_distribution_single": 0.1198, "custom_dso_transport": 0.0274}
    )
    for key in (
        "custom_dso_distribution_pic",
        "custom_dso_distribution_medium",
        "custom_dso_distribution_eco",
    ):
        assert key not in stored, key


def test_a_zeroed_impact_triplet_costs_the_whole_distribution_leg() -> None:
    """What the zeros were worth, through the real engine."""
    from datetime import datetime

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.pricing import network_eur_per_kwh
    from custom_components.be_electricity_prices.providers.custom import build_snapshot

    base = {
        "custom_dso_distribution_single": 0.1198,
        "custom_dso_transport": 0.0274,
    }
    zeroed = dict(
        base,
        custom_dso_distribution_pic=0.0,
        custom_dso_distribution_medium=0.0,
        custom_dso_distribution_eco=0.0,
    )
    when = datetime(2026, 4, 15, 14, tzinfo=dt_util.DEFAULT_TIME_ZONE)

    def net(data: dict[str, float]) -> float:
        overlay = build_snapshot(data, "wallonia", "custom").dsos["custom"]
        return network_eur_per_kwh(overlay, when, "mono", "impact", "wallonia")

    assert net(base) == pytest.approx(0.1472)
    # Transport alone: the whole distribution leg silently gone.
    assert net(zeroed) == pytest.approx(0.0274)


async def test_a_stored_zero_impact_triplet_is_migrated_away(
    hass: HomeAssistant,
) -> None:
    """Fixing the schema stops new entries taking the zeros; it cannot clear
    the ones already stored, and the user cannot blank a box that is not
    shown as blankable."""
    from custom_components.be_electricity_prices import (
        _migrate_zeroed_custom_impact_bands,
    )

    entry = make_entry(
        supplier="custom",
        region="wallonia",
        dso_tariff_mode="impact",
        custom_dso_distribution_single=0.1198,
        custom_dso_distribution_pic=0.0,
        custom_dso_distribution_medium=0.0,
        custom_dso_distribution_eco=0.0,
    )
    entry.add_to_hass(hass)
    _migrate_zeroed_custom_impact_bands(hass, entry)
    assert "custom_dso_distribution_pic" not in entry.data
    assert entry.data["custom_dso_distribution_single"] == pytest.approx(0.1198)


async def test_a_real_impact_triplet_is_left_alone(hass: HomeAssistant) -> None:
    """Only an ALL-zero triplet is dropped. A genuine tariff has no zero
    bands, and a partly filled one is the user's own data."""
    from custom_components.be_electricity_prices import (
        _migrate_zeroed_custom_impact_bands,
    )

    entry = make_entry(
        supplier="custom",
        region="wallonia",
        dso_tariff_mode="impact",
        custom_dso_distribution_pic=0.1511,
        custom_dso_distribution_medium=0.1183,
        custom_dso_distribution_eco=0.0666,
    )
    entry.add_to_hass(hass)
    _migrate_zeroed_custom_impact_bands(hass, entry)
    assert entry.data["custom_dso_distribution_pic"] == pytest.approx(0.1511)


async def test_a_stale_signing_rate_cannot_override_the_typed_formula(
    hass: HomeAssistant,
) -> None:
    """An entry EDITED onto the custom supplier keeps the manual rate and
    start date from its previous life. The signing-rate step is never offered
    for custom, so nothing pops them, and the overlay silently replaced the
    formula the user typed with the old supplier's rate - measured at +0,09
    EUR/kWh and +60 EUR of fee, in whichever direction that supplier charged.
    """
    from custom_components.be_electricity_prices.cohort import _cohort_energy_leg
    from custom_components.be_electricity_prices.providers.custom import (
        build_snapshot,
    )

    data = {
        "supplier": "custom",
        "contract": "custom_fixed",
        "region": "wallonia",
        "custom_energy_single": 0.1300,
        "custom_yearly_fixed_fee": 60.0,
        # left behind by the entry's previous life as a real supplier
        "manual_energy_single": 0.22,
        "manual_yearly_fee": 120.0,
        "contract_start_date": "2025-06-01",
    }
    entry = make_entry(**data)  # type: ignore[arg-type]
    entry.add_to_hass(hass)
    snap = build_snapshot(data, "wallonia", "custom")

    leg = await _cohort_energy_leg(
        hass,
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        "custom_fixed",
        "wallonia",
        entry,
        snap,
    )
    # Not the 0,22 the old supplier charged; the typed 0,13 stands.
    assert leg is None


def test_compare_honours_the_entry_level_spp_opt_in() -> None:
    """The SPP-weighting opt-in lives on the ENTRY for the custom supplier,
    because a hand-entered contract has no card to read a flag off. The
    compare page gated on the card flag alone, so the one supplier the opt-in
    exists for never reached the branch, and the page quoted the credit at the
    two-day day-ahead window mean while the sensor beside it used the month's
    SPP-weighted one.
    """
    from custom_components.be_electricity_prices.compare_weighting import (
        _compare_injection_credit,
    )
    from custom_components.be_electricity_prices.providers.custom import (
        build_snapshot,
    )

    data = {
        "supplier": "custom",
        "contract": "custom_monthly",
        "region": "wallonia",
        "solar_regime": "injection",
        "custom_injection_mode": "formula",
        "custom_injection_factor": 0.9,
        "custom_injection_base": -0.01,
        "custom_injection_spp_weighted": True,
    }
    snap = build_snapshot(data, "wallonia", "custom")
    entry = make_entry(**data)  # type: ignore[arg-type]
    assert snap.injection is not None
    # spp_indexed is False - no card said so - yet the caller resolved an
    # SPP mean, and that mean must be what the credit is priced at.
    assert snap.injection.spp_indexed is False
    credit = _compare_injection_credit(snap, entry, {}, 0.1142, month_spot=0.0634)
    assert credit == pytest.approx(0.9 * 0.0634 - 0.01)


def test_floor_injection_passthrough_without_flag() -> None:
    inj = InjectionRates(current=-0.001, floor_at_zero=False)
    assert _floor_injection(-0.001, inj) == -0.001
    assert _floor_injection(None, inj) is None


def test_floor_injection_honours_a_stated_minimum() -> None:
    """EnergyVision guarantees 1 c€/kWh rather than merely non-negative, so
    the clamp has to lift a positive-but-smaller rate, not only a negative
    one. A zero clamp would leave 0,25 c€/kWh standing."""
    inj = InjectionRates(current=0.0025, minimum=0.01)
    assert _floor_injection(0.0025, inj) == 0.01
    assert _floor_injection(-0.02, inj) == 0.01
    assert _floor_injection(0.03, inj) == 0.03
    assert _floor_injection(None, inj) is None


# ---- serialization -----------------------------------------------------------


def test_spot_monthly_snapshot_round_trips() -> None:
    snap = build_snapshot(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
        const.REGION_FLANDERS,
        const.DSO_FLUVIUS_ANTWERPEN,
    )
    restored = _snapshot_from_dict(_snapshot_to_dict(snap, WHEN))
    assert isinstance(restored.energy, SpotMonthlyRates)
    assert restored.energy.factor == 1.0834
    assert restored.injection is not None
    assert restored.injection.floor_at_zero is True


# ---- provider registry -------------------------------------------------------


async def test_custom_fetch_stub_raises() -> None:
    with pytest.raises(ExtractorError):
        await EXTRACTOR.fetch(None, const.CUSTOM_CONTRACT_DYNAMIC, "flanders")  # type: ignore[arg-type]


def test_custom_excluded_from_compare_targets() -> None:
    options = _compare_supplier_options(const.REGION_FLANDERS, "dynamic", False)
    assert const.SUPPLIER_CUSTOM not in {o["value"] for o in options}


def test_custom_listed_last_in_supplier_dropdown() -> None:
    from custom_components.be_electricity_prices.flow_contracts import _supplier_options

    values = [o["value"] for o in _supplier_options()]
    assert values[-1] == const.SUPPLIER_CUSTOM


def test_suppliers_are_listed_alphabetically_before_the_custom_one() -> None:
    """The dropdown followed the registry's insertion order, which put a
    supplier added later wherever its import happened to land: Trevion sat
    between TotalEnergies and Luminus."""
    from custom_components.be_electricity_prices.flow_contracts import _supplier_options

    labels = [o["label"] for o in _supplier_options()][:-1]
    assert labels == sorted(labels, key=str.casefold)


def test_compare_targets_are_listed_alphabetically() -> None:
    """The compare page builds its own supplier list and kept walking the
    registry in import order after the install picker was sorted, so the two
    dropdowns disagreed on where Trevion sits."""
    labels = [
        o["label"]
        for o in _compare_supplier_options(const.REGION_FLANDERS, "dynamic", False)
    ]
    assert labels == sorted(labels, key=str.casefold)


# ---- withdrawn suppliers -----------------------------------------------------


def test_withdrawn_supplier_not_offered_to_new_setups() -> None:
    from custom_components.be_electricity_prices.flow_contracts import _supplier_options

    assert "dats24" not in {o["value"] for o in _supplier_options()}
    assert "dats24" not in {
        o["value"] for o in _supplier_options(const.REGION_FLANDERS)
    }


def test_withdrawn_supplier_still_editable_on_an_existing_entry() -> None:
    """The load-bearing half: a SelectSelector rejects a default that is not
    among its options, so an entry already on a withdrawn supplier would
    become impossible to edit if the filter had no ``keep`` escape hatch."""
    from custom_components.be_electricity_prices.flow_contracts import _supplier_options

    assert "dats24" in {o["value"] for o in _supplier_options(keep="dats24")}
    # keep= is an exception for one entry, not a global switch-off.
    assert "dats24" not in {o["value"] for o in _supplier_options(keep="eneco")}


def test_withdrawn_supplier_not_a_comparison_target() -> None:
    for region in (const.REGION_FLANDERS, const.REGION_WALLONIA):
        for kind in ("variable", "dynamic"):
            assert "dats24" not in {
                o["value"] for o in _compare_supplier_options(region, kind, False)
            }


def test_withdrawn_contract_offered_to_new_setups_with_a_dagger() -> None:
    """OCTA+ stopped selling Fixed, Eco Fixed, Flux and Eco Flux with its
    October 2026 cards, but a household still under contract on one has to be
    able to set it up (issue #135). The setup picker lists them marked with a
    dagger the step's description explains; the products still sold are not."""
    from custom_components.be_electricity_prices.flow_schemas import _contract_schema

    schema = _contract_schema("octaplus", const.REGION_WALLONIA, {})
    marker = next(k for k in schema.schema if str(k) == const.CONF_CONTRACT)
    labels = {o["value"]: o["label"] for o in schema.schema[marker].config["options"]}
    for withdrawn in (
        "octaplus_fixed",
        "octaplus_fixed_impact",
        "octaplus_ecofixed",
        "octaplus_flux",
        "octaplus_ecoflux",
    ):
        assert labels[withdrawn].endswith(" †")
    assert labels["octaplus_flux"] == "OCTA+ Flux †"
    assert "†" not in labels["octaplus_boostfix"]


def test_withdrawn_contract_not_a_comparison_target() -> None:
    """Neither ranked nor offered on the one-off page, except as the entry's
    own contract, which the page keeps for its what-if."""
    from custom_components.be_electricity_prices.compare_flow import (
        _compare_contract_schema,
    )
    from custom_components.be_electricity_prices.flow_contracts import (
        _sweep_candidates,
    )

    for group in ("static", "spot", "slot"):
        ranked = {
            c.id
            for _, c, _ in _sweep_candidates(const.REGION_WALLONIA, group, False, "")
        }
        assert "octaplus_flux" not in ranked
        assert "octaplus_fixed_impact" not in ranked

    def offered(keep: str | None) -> set[str]:
        schema = _compare_contract_schema(
            "octaplus", const.REGION_WALLONIA, "fixed", "", False, keep=keep
        )
        selector = next(iter(schema.schema.values()))
        return {o["value"] for o in selector.config["options"]}

    assert "octaplus_fixed" not in offered(None)
    assert "octaplus_fixed" in offered("octaplus_fixed")
    assert "octaplus_flux" not in offered("octaplus_fixed")


# ---- coordinator: flat monthly live table ------------------------------------


def _monthly_entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=const.DOMAIN,
        data={
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_REGION: const.REGION_FLANDERS,
            const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN,
            const.CONF_METER: const.METER_MONO,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
            const.CONF_CUSTOM_ENERGY_BASE: 0.0,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
            const.CONF_CUSTOM_VAT_RATE: 0.06,
        },
        title="Custom monthly",
    )


async def test_build_hourly_spot_monthly_is_flat(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Every slot of the live table bills the same flat monthly rate."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _monthly_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = build_snapshot(
        dict(entry.data), const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN
    )
    hourly = coord._build_hourly(coord._snapshot, {}, 0.08)
    all_in = {bd.all_in for bd in hourly.values()}
    assert len(hourly) >= 24
    assert len(all_in) == 1  # perfectly flat
    # (energy 1.0834*0.08 + distribution 0.05) * 1.06 VAT
    expected = (1.0834 * 0.08 + 0.05) * 1.06
    assert next(iter(all_in)) == pytest.approx(expected)


async def test_build_hourly_spot_monthly_empty_without_mean(
    hass: HomeAssistant, freezer: Any
) -> None:
    """No mean yet (cold start) leaves the table empty, not crashing."""
    freezer.move_to("2026-07-15 12:00:00+02:00")
    entry = _monthly_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._snapshot = build_snapshot(
        dict(entry.data), const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN
    )
    assert coord._build_hourly(coord._snapshot, {}, None) == {}


# ---- config-flow walks -------------------------------------------------------


@pytest.fixture
def _no_setup() -> Any:
    """A completed config flow makes HA set the entry up, which spins a
    coordinator that needs a recorder. Stub it out for the flow walks."""
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        yield


def _mock_key() -> Any:
    return patch(
        "custom_components.be_electricity_prices.flow_wizard._validate_entsoe_key",
        return_value=None,
    )


async def _start(hass: HomeAssistant, supplier: str, region: str) -> Any:
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": "user"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {const.CONF_SUPPLIER: supplier, const.CONF_REGION: region},
    )


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_dynamic_flanders(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Dynamic custom reaches the api-key step and collects the formula."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_FLANDERS)
    assert result["step_id"] == "contract"
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_DYNAMIC})
    assert result["step_id"] == "dso"
    result = await cfg(flow, {const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN})
    assert result["step_id"] == "meter"
    result = await cfg(flow, {const.CONF_METER: const.METER_DYNAMIC})
    assert result["step_id"] == "analog_replaced"  # Flanders, left blank
    result = await cfg(flow, {})
    assert result["step_id"] == "api_key"  # dynamic gates the key
    with _mock_key():
        result = await cfg(flow, {const.CONF_API_KEY: "k"})
    assert result["step_id"] == "custom_energy"
    result = await cfg(
        flow,
        {
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0,
            const.CONF_CUSTOM_ENERGY_BASE: 0.02,
            const.CONF_CUSTOM_ENERGY_QUARTER_HOURLY: False,
            const.CONF_CUSTOM_YEARLY_FIXED_FEE: 60.0,
        },
    )
    assert result["step_id"] == "capacity"  # Flanders
    result = await cfg(
        flow,
        {
            const.CONF_CAPACITY_MODE: const.CAPACITY_MODE_FIXED,
            const.CONF_CAPACITY_FIXED_KW: 4.0,
        },
    )
    assert result["step_id"] == "solar"
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05})
    assert result["step_id"] == "custom_tax"
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_ENERGY_FACTOR] == 1.0
    assert result["data"][const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE] == 0.05


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_monthly_injection(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Monthly custom also gates the key and inserts the injection step."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_FLANDERS)
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY})
    result = await cfg(flow, {const.CONF_DSO: const.DSO_FLUVIUS_ANTWERPEN})
    result = await cfg(flow, {const.CONF_METER: const.METER_MONO})
    assert result["step_id"] == "analog_replaced"  # Flanders, left blank
    result = await cfg(flow, {})
    assert result["step_id"] == "api_key"  # spot_monthly gates the key too
    with _mock_key():
        result = await cfg(flow, {const.CONF_API_KEY: "k"})
    assert result["step_id"] == "custom_energy"
    result = await cfg(
        flow,
        {const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834, const.CONF_CUSTOM_ENERGY_BASE: 0.0},
    )
    result = await cfg(
        flow,
        {
            const.CONF_CAPACITY_MODE: const.CAPACITY_MODE_FIXED,
            const.CONF_CAPACITY_FIXED_KW: 4.0,
        },
    )
    assert result["step_id"] == "solar"
    result = await cfg(
        flow,
        {
            const.CONF_SOLAR_KVA: 5.0,
            const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_INJECTION,
        },
    )
    assert result["step_id"] == "custom_injection"
    result = await cfg(
        flow,
        {
            const.CONF_CUSTOM_INJECTION_MODE: const.CUSTOM_INJECTION_MODE_FORMULA,
            const.CONF_CUSTOM_INJECTION_FACTOR: 0.96,
            const.CONF_CUSTOM_INJECTION_BASE: -0.009,
            const.CONF_CUSTOM_INJECTION_FLOOR: True,
        },
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05})
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_INJECTION_FLOOR] is True


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_flow_custom_fixed_wallonia_no_api_key(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Fixed custom skips the api-key step (no spot needed)."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_WALLONIA)
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    result = await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED})
    result = await cfg(flow, {const.CONF_DSO: const.DSO_ORES})
    result = await cfg(flow, {const.CONF_METER: const.METER_MONO})
    assert result["step_id"] == "dso_tariff_mode"  # Wallonia
    result = await cfg(flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_SIMPLE})
    assert result["step_id"] == "custom_energy"  # no api-key step for fixed
    result = await cfg(flow, {const.CONF_CUSTOM_ENERGY_SINGLE: 0.30})
    assert result["step_id"] == "solar"  # no capacity outside Flanders
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    result = await cfg(flow, {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.06})
    result = await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    assert result["step_id"] == "meters"
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_CUSTOM_ENERGY_SINGLE] == 0.30


def test_custom_dso_schema_offers_the_bihourly_split_to_a_dynamic_meter() -> None:
    """A dynamic / TOU contract FORCES METER_DYNAMIC (`_meter_schema`), and
    `pricing.network_eur_per_kwh` routes both `bi` and `dynamic` through
    `distribution_peak` / `distribution_offpeak` whenever the DSO mode is not
    "simple". Gating the two boxes on METER_BI alone meant a custom dynamic
    entry could never supply the rates its own network leg is billed on, so
    every hour silently fell back to `distribution_single`."""
    base = {
        const.CONF_REGION: const.REGION_WALLONIA,
        const.CONF_DSO_TARIFF_MODE: "bi_horaire",
    }

    def keys(meter: str) -> set[str]:
        return {
            str(k) for k in _custom_dso_schema({**base, const.CONF_METER: meter}).schema
        }

    for meter in (const.METER_BI, const.METER_DYNAMIC):
        got = keys(meter)
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK in got, meter
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK in got, meter

    # Meters that are never routed through the split must not be asked for it.
    for meter in (const.METER_MONO, const.METER_EXCLUSIVE_NIGHT):
        got = keys(meter)
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK not in got, meter
        assert const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK not in got, meter


def test_custom_energy_schema_offers_the_day_night_split_to_a_dynamic_meter() -> None:
    """`pricing.energy_eur_per_kwh` sets `bi_capable = meter in ("bi",
    "dynamic")` and routes both through `peak` / `offpeak`, so a custom fixed
    contract on a smart meter must be able to enter them. Gating on METER_BI
    alone left both None and billed all 24 hours at the single rate -- the
    same root cause as the DSO step, and together they cost such an entry both
    the supplier and the network day/night split."""
    base = {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED}

    def keys(meter: str) -> set[str]:
        return {
            str(k)
            for k in _custom_energy_schema({**base, const.CONF_METER: meter}).schema
        }

    for meter in (const.METER_BI, const.METER_DYNAMIC):
        got = keys(meter)
        assert const.CONF_CUSTOM_ENERGY_PEAK in got, meter
        assert const.CONF_CUSTOM_ENERGY_OFFPEAK in got, meter
    mono = keys(const.METER_MONO)
    assert const.CONF_CUSTOM_ENERGY_PEAK not in mono
    assert const.CONF_CUSTOM_ENERGY_OFFPEAK not in mono


def test_fallback_rate_boxes_never_carry_a_default() -> None:
    """A rate the pricing engine falls back for must not be submitted as 0,00.

    `vol.Optional(key, default=0.0)` sends the default verbatim when the user
    leaves the box alone, so simply clicking through the wizard wrote peak =
    offpeak = 0,00 into the entry and `_routed_rate` billed ZERO instead of
    falling back to the single rate: an existing custom entry re-opened and
    clicked through priced at 0,00000 EUR/kWh, energy and network both nil.

    The base rates keep their defaults; only the ones with a fallback lose them.
    """
    import voluptuous as vol

    def has_default(schema: Any, key: str) -> bool:
        for k in schema.schema:
            if str(k) == key:
                return getattr(k, "default", vol.UNDEFINED) is not vol.UNDEFINED
        raise AssertionError(f"{key} not in schema")

    energy = _custom_energy_schema(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
            const.CONF_METER: const.METER_DYNAMIC,
        }
    )
    assert has_default(energy, const.CONF_CUSTOM_ENERGY_SINGLE)
    assert not has_default(energy, const.CONF_CUSTOM_ENERGY_PEAK)
    assert not has_default(energy, const.CONF_CUSTOM_ENERGY_OFFPEAK)

    dso = _custom_dso_schema(
        {
            const.CONF_REGION: const.REGION_WALLONIA,
            const.CONF_METER: const.METER_DYNAMIC,
            const.CONF_DSO_TARIFF_MODE: "bi_horaire",
        }
    )
    assert has_default(dso, const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE)
    assert not has_default(dso, const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK)
    assert not has_default(dso, const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK)

    night = _custom_dso_schema(
        {
            const.CONF_REGION: const.REGION_WALLONIA,
            const.CONF_METER: const.METER_EXCLUSIVE_NIGHT,
            const.CONF_DSO_TARIFF_MODE: "bi_horaire",
        }
    )
    assert not has_default(night, const.CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT)

    # The ENERGY twin of that last box was the one left behind, and it is the
    # worst of the six: an exclusive-night meter routes the whole entry through
    # this single rate, so the energy leg went to zero for every hour.
    night_energy = _custom_energy_schema(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
            const.CONF_METER: const.METER_EXCLUSIVE_NIGHT,
        }
    )
    assert not has_default(night_energy, const.CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT)


def test_a_blanked_fallback_box_is_removed_from_the_entry() -> None:
    """Dropping the default is only half of it: the step has to pop the key.

    ha-form omits a blanked selector from user_input entirely, so a bare
    data.update(user_input) leaves the stored number in place and the re-shown
    form pre-fills it again as a suggestion. That matters because 0.11.40 and
    0.11.41 briefly shipped these boxes with a 0.0 default, so an entry edited
    in that window holds a billed zero, and without the pop there is no way
    to clear it. Each step pops only its own boxes: the other step's rates
    are not on its form and stay.
    """
    from custom_components.be_electricity_prices.flow_schemas import (
        _CUSTOM_DSO_FALLBACK_KEYS,
        _CUSTOM_ENERGY_FALLBACK_KEYS,
        _drop_blanked,
    )

    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_METER: const.METER_EXCLUSIVE_NIGHT,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
        const.CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT: 0.0,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK: 0.0,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK: 0.0,
    }
    # The user clears the energy step's fallback box and submits just the
    # single rate: the network step's boxes are not on that form and stay.
    _drop_blanked(
        data, {const.CONF_CUSTOM_ENERGY_SINGLE: 0.30}, _CUSTOM_ENERGY_FALLBACK_KEYS
    )
    assert const.CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT not in data
    assert data[const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK] == 0.0
    # The network step pops its own, including the peak / off-peak pair an
    # earlier bi-hourly meter left behind and this meter no longer shows.
    _drop_blanked(
        data,
        {const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05},
        _CUSTOM_DSO_FALLBACK_KEYS,
    )
    for key in (
        const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK,
    ):
        assert key not in data, key
    # A box the user DID fill is kept, and the non-fallback rates are untouched.
    user_input = {const.CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT: 0.12}
    _drop_blanked(data, user_input, _CUSTOM_ENERGY_FALLBACK_KEYS)
    data.update(user_input)
    assert data[const.CONF_CUSTOM_ENERGY_EXCLUSIVE_NIGHT] == pytest.approx(0.12)
    assert data[const.CONF_CUSTOM_ENERGY_SINGLE] == pytest.approx(0.30)


async def _custom_bi_install(hass: HomeAssistant) -> Any:
    """Walk a Walloon custom fixed entry on a bi-hourly meter, filling the
    day / night split on both the energy and the network step."""
    result = await _start(hass, const.SUPPLIER_CUSTOM, const.REGION_WALLONIA)
    flow = result["flow_id"]
    cfg = hass.config_entries.flow.async_configure
    await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED})
    await cfg(flow, {const.CONF_DSO: const.DSO_ORES})
    await cfg(flow, {const.CONF_METER: const.METER_BI})
    result = await cfg(flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_BI_HORAIRE})
    assert result["step_id"] == "custom_energy"
    await cfg(
        flow,
        {
            const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
            const.CONF_CUSTOM_ENERGY_PEAK: 0.34,
            const.CONF_CUSTOM_ENERGY_OFFPEAK: 0.24,
        },
    )
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    await cfg(
        flow,
        {
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.10,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK: 0.12,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK: 0.07,
        },
    )
    await cfg(flow, {const.CONF_CUSTOM_VAT_RATE: 0.06})
    result = await cfg(flow, {})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    return result


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_network_step_keeps_the_energy_split(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """Each custom step pops only the boxes it shows. Both steps used to pop
    every fallback key missing from their own input, so the network step
    threw away the energy peak / off-peak the step before had just stored
    and a bi-hourly card billed its single rate (zero, left at the default)
    every hour."""
    data = (await _custom_bi_install(hass))["data"]
    assert data[const.CONF_CUSTOM_ENERGY_PEAK] == 0.34
    assert data[const.CONF_CUSTOM_ENERGY_OFFPEAK] == 0.24
    assert data[const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK] == 0.12
    assert data[const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK] == 0.07


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_edit_still_suggests_the_stored_network_split(
    hass: HomeAssistant, _no_setup: Any
) -> None:
    """The energy step of an edit must not empty the network step's boxes
    before that step builds its suggestions from the entry."""
    await _custom_bi_install(hass)
    entry = hass.config_entries.async_entries(const.DOMAIN)[0]
    cfg = hass.config_entries.options.async_configure
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await cfg(result["flow_id"], {"next_step_id": "edit"})
    flow = result["flow_id"]
    await cfg(
        flow,
        {
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_REGION: const.REGION_WALLONIA,
        },
    )
    await cfg(flow, {const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED})
    await cfg(flow, {const.CONF_DSO: const.DSO_ORES})
    await cfg(flow, {const.CONF_METER: const.METER_BI})
    result = await cfg(flow, {const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_BI_HORAIRE})
    assert result["step_id"] == "custom_energy"
    await cfg(
        flow,
        {
            const.CONF_CUSTOM_ENERGY_SINGLE: 0.30,
            const.CONF_CUSTOM_ENERGY_PEAK: 0.34,
            const.CONF_CUSTOM_ENERGY_OFFPEAK: 0.24,
        },
    )
    result = await cfg(
        flow,
        {const.CONF_SOLAR_KVA: 0.0, const.CONF_SOLAR_REGIME: const.SOLAR_REGIME_NONE},
    )
    assert result["step_id"] == "custom_dso"
    schema = result["data_schema"]
    assert schema is not None
    suggested = {
        str(k.schema): (k.description or {}).get("suggested_value")
        for k in schema.schema
    }
    assert suggested[const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK] == 0.12
    assert suggested[const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK] == 0.07


def test_absent_fallback_rates_price_off_the_single_rate() -> None:
    """The behaviour the missing defaults protect: with peak / offpeak absent
    a bi-hourly hour still bills the single rate, not zero."""
    from datetime import UTC, datetime

    from custom_components.be_electricity_prices import pricing
    from custom_components.be_electricity_prices.providers.custom import build_snapshot

    entry = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.20,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
        const.CONF_CUSTOM_VAT_RATE: 0.0,
    }
    snap = build_snapshot(entry, const.REGION_WALLONIA, const.DSO_ORES)
    bd = pricing.compute_breakdown(
        snap,
        const.DSO_ORES,
        const.REGION_WALLONIA,
        datetime(2026, 8, 5, 12, tzinfo=UTC),
        None,
        const.METER_DYNAMIC,
        dso_tariff_mode="bi_horaire",
    )
    assert bd.energy == pytest.approx(0.20)
    assert bd.network == pytest.approx(0.05)
    assert bd.all_in == pytest.approx(0.25)


def test_custom_tax_step_offers_the_connection_fee_in_wallonia_only() -> None:
    """The only Belgian levy of that shape is the Walloon redevance de
    raccordement, and the pricing engine bills region_connection_fee for
    Wallonia alone. Offering the box to a Flemish entry stored a value that was
    never priced: a Flanders customer typed the WKK levy into it, which belongs
    in the renewables box with GSC, and saw 0 and 100 behave the same."""
    from custom_components.be_electricity_prices.flow_schemas_custom import (
        _custom_tax_schema,
    )

    def keys(region: str) -> set[str]:
        schema = _custom_tax_schema({const.CONF_REGION: region})
        return {str(getattr(k, "schema", k)) for k in schema.schema}

    for region in (const.REGION_FLANDERS, const.REGION_BRUSSELS):
        assert const.CONF_CUSTOM_TAX_REGION_CONNECTION_FEE not in keys(region), region
        assert const.CONF_CUSTOM_TAX_REGIONAL_RENEWABLES in keys(region)
    assert const.CONF_CUSTOM_TAX_REGION_CONNECTION_FEE in keys(const.REGION_WALLONIA)


def test_custom_taxes_book_the_connection_fee_in_wallonia_only() -> None:
    """A value stored on a Flemish or Brussels entry before the box was gated
    must not linger; the renewables box is routed per region the same way."""
    from custom_components.be_electricity_prices.providers.custom import _build_taxes

    data = {
        const.CONF_CUSTOM_TAX_REGION_CONNECTION_FEE: 0.00075,
        const.CONF_CUSTOM_TAX_REGIONAL_RENEWABLES: 0.0156,
    }
    assert _build_taxes(data, const.REGION_WALLONIA).region_connection_fee == 0.00075
    assert _build_taxes(data, const.REGION_FLANDERS).region_connection_fee == 0.0
    assert _build_taxes(data, const.REGION_BRUSSELS).region_connection_fee == 0.0
    assert _build_taxes(data, const.REGION_FLANDERS).flanders_renewables == 0.0156


def test_every_custom_contract_is_offered_an_injection_formula() -> None:
    """A fixed price for consumption can come with a feed-in credit on the
    month's average spot (discussion #157), so the fixed mode is offered the
    formula as the two spot modes are, and the solar-profile weighting the
    monthly mode has. Only the dynamic mode's formula runs on the live spot,
    which no profile weights."""
    from custom_components.be_electricity_prices import const
    from custom_components.be_electricity_prices.flow_schemas_custom import (
        _custom_injection_schema,
    )

    def keys(contract: str) -> set[str]:
        schema = _custom_injection_schema({const.CONF_CONTRACT: contract})
        return {getattr(key, "schema", key) for key in schema.schema}

    def modes(contract: str) -> list[str]:
        schema = _custom_injection_schema({const.CONF_CONTRACT: contract})
        for key, selector in schema.schema.items():
            if getattr(key, "schema", key) == const.CONF_CUSTOM_INJECTION_MODE:
                return list(selector.config["options"])
        raise AssertionError(f"no injection pricing selector for {contract}")

    formula = {const.CONF_CUSTOM_INJECTION_FACTOR, const.CONF_CUSTOM_INJECTION_BASE}
    spp = const.CONF_CUSTOM_INJECTION_SPP_WEIGHTED
    for contract in (
        const.CUSTOM_CONTRACT_FIXED,
        const.CUSTOM_CONTRACT_DYNAMIC,
        const.CUSTOM_CONTRACT_MONTHLY,
    ):
        assert formula <= keys(contract)
        assert modes(contract) == list(const.CUSTOM_INJECTION_MODES)
    assert spp in keys(const.CUSTOM_CONTRACT_FIXED)
    assert spp in keys(const.CUSTOM_CONTRACT_MONTHLY)
    assert spp not in keys(const.CUSTOM_CONTRACT_DYNAMIC)


def test_a_per_kwh_figure_typed_in_cents_is_refused() -> None:
    """A hand-entered per-kWh box took any figure, so one typed in c€/kWh was
    billed a hundred times over; the small levies still look plausible that
    way. Each box refuses what no real figure reaches and keeps real ones."""
    import voluptuous as vol

    from custom_components.be_electricity_prices import const
    from custom_components.be_electricity_prices.flow_schemas import _custom_num

    fee = _custom_num(key=const.CONF_CUSTOM_TAX_REGION_CONNECTION_FEE)
    assert fee(0.00075) == 0.00075
    with pytest.raises(vol.Invalid):
        fee(0.075)
    base = _custom_num(negative=True, key=const.CONF_CUSTOM_ENERGY_BASE)
    assert base(-0.02) == -0.02
    for cents in (1.5, -1.5):
        with pytest.raises(vol.Invalid):
            base(cents)
    rate = _custom_num(key=const.CONF_MANUAL_ENERGY_SINGLE)
    assert rate(0.7) == 0.7
    with pytest.raises(vol.Invalid):
        rate(18.021)
    # A yearly figure is not a per-kWh one and keeps no bound.
    assert (
        _custom_num(key=const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR)(400.0) == 400.0
    )


def _business_levies(professional: bool) -> dict[str, Any]:
    """A custom fixed Flanders entry typing the professional federal levies
    (Ecopower's 'zakelijk' column) at 21% VAT, as a Yuso customer would."""
    return {
        const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_REGION: const.REGION_FLANDERS,
        const.CONF_DSO: "fluvius_antwerpen",
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.12,
        const.CONF_CUSTOM_TAX_FEDERAL_EXCISE: 0.01421,
        const.CONF_CUSTOM_TAX_ENERGY_CONTRIBUTION: 0.0019261,
        const.CONF_CUSTOM_VAT_RATE: 0.21,
        const.CONF_CUSTOM_PROFESSIONAL: professional,
    }


@pytest.mark.parametrize("professional", [True, False])
def test_a_business_keeps_the_professional_levies_it_typed(
    professional: bool,
) -> None:
    """From August 2026 the residential scheme drops the contribution and
    bills a flat excise, and a residential custom entry is corrected to it.
    A business still owes the professional excise and the contribution, so
    one that says so on the tax step is billed what it typed."""
    from datetime import date
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.snapshot_resolve import (
        _resolve_snapshot,
    )

    data = _business_levies(professional)
    entry = SimpleNamespace(data=data)
    raw = build_snapshot(data, const.REGION_FLANDERS, "fluvius_antwerpen")
    snap = _resolve_snapshot(
        entry,  # type: ignore[arg-type]
        raw,
        annual_kwh=3500.0,
        delivery_month=date(2026, 10, 1),
    )
    if professional:
        assert snap.taxes.federal_excise == pytest.approx(0.01421)
        assert snap.taxes.energy_contribution == pytest.approx(0.0019261)
    else:
        assert snap.taxes.federal_excise == pytest.approx(0.046)
        assert snap.taxes.energy_contribution == 0.0
    # The typed VAT rate is the business's own either way.
    assert snap.taxes.vat_rate == pytest.approx(0.21)


def test_the_business_answer_stays_on_the_custom_card() -> None:
    """The compare page resolves another supplier's card through a proxy
    carrying this entry's data: the answer describes the levies typed here,
    so a residential card priced for the household is still corrected."""
    from dataclasses import replace
    from datetime import date
    from types import SimpleNamespace

    from custom_components.be_electricity_prices.snapshot_resolve import (
        _resolve_snapshot,
    )

    data = _business_levies(True)
    raw = replace(
        build_snapshot(data, const.REGION_FLANDERS, "fluvius_antwerpen"),
        supplier="ecopower",
        contract="ecopower_burgerstroom",
    )
    snap = _resolve_snapshot(
        SimpleNamespace(data=data),  # type: ignore[arg-type]
        raw,
        annual_kwh=3500.0,
        delivery_month=date(2026, 10, 1),
    )
    assert snap.taxes.energy_contribution == 0.0


@pytest.mark.parametrize(
    ("professional", "factor"),
    [
        # The tax step's reading of a 0 rate: VAT-inclusive figures typed.
        (False, 1.06),
        # A business typing 0 deducts VAT: its figures are excluding it.
        (True, 1.0),
    ],
)
def test_a_business_deducting_vat_bills_regulated_figures_excluding_it(
    professional: bool, factor: float
) -> None:
    """The Flemish network ceiling and Brugel's power term are published
    excluding VAT and put onto the basis the entry typed on. A business at a
    0 rate typed excluding VAT, so neither may be grossed by 6%."""
    from datetime import date
    from types import SimpleNamespace

    from custom_components.be_electricity_prices import snapshot_resolve

    month = date(2026, 10, 1)
    for region, dso in (
        (const.REGION_FLANDERS, "fluvius_antwerpen"),
        (const.REGION_BRUSSELS, const.DSO_SIBELGA),
    ):
        data = {
            const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
            const.CONF_REGION: region,
            const.CONF_DSO: dso,
            const.CONF_CUSTOM_ENERGY_SINGLE: 0.10,
            const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
            const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 13.90,
            const.CONF_CUSTOM_VAT_RATE: 0.0,
            const.CONF_CUSTOM_PROFESSIONAL: professional,
        }
        with patch.object(
            snapshot_resolve, "cached_power_term", lambda year: (47.24, 94.48)
        ):
            snap = snapshot_resolve._resolve_snapshot(
                SimpleNamespace(data=data),  # type: ignore[arg-type]
                build_snapshot(data, region, dso),
                annual_kwh=1000.0,
                delivery_month=month,
            )
        overlay = snap.dsos[dso]
        if region == const.REGION_FLANDERS:
            assert overlay.network_ceiling_eur_per_kwh == pytest.approx(
                0.3276168 * factor
            )
        else:
            assert overlay.data_management_per_year == pytest.approx(
                13.90 + 47.24 * factor
            )
            assert overlay.brussels_power_term_above_13kva == pytest.approx(
                13.90 + 94.48 * factor
            )


def test_the_custom_tax_step_asks_whether_the_contract_is_a_business() -> None:
    from custom_components.be_electricity_prices.flow_schemas_custom import (
        _custom_tax_schema,
    )

    schema = _custom_tax_schema({const.CONF_REGION: const.REGION_FLANDERS})
    assert schema({})[const.CONF_CUSTOM_PROFESSIONAL] is False
    stored = {
        const.CONF_REGION: const.REGION_FLANDERS,
        const.CONF_CUSTOM_PROFESSIONAL: True,
    }
    assert _custom_tax_schema(stored)({})[const.CONF_CUSTOM_PROFESSIONAL] is True


def test_an_unset_custom_vat_rate_is_the_months_residential_one() -> None:
    """The tax step offers the residential rate of the month, and an entry
    that never stored a rate bills on it, rather than on a 6% typed here."""
    from custom_components.be_electricity_prices import vat_rates
    from custom_components.be_electricity_prices.flow_schemas_custom import (
        _custom_tax_schema,
    )

    today = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    vat_rates.hold({"residential": {"2026-11": {"rate": 0.07}}})
    data = {
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.10,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.05,
    }
    with patch("homeassistant.util.dt.now", return_value=today):
        schema = _custom_tax_schema({const.CONF_REGION: const.REGION_FLANDERS})
        snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert schema({})[const.CONF_CUSTOM_VAT_RATE] == 0.07
    assert snap.taxes.vat_rate == 0.07


def _flemish_network(**extra: Any) -> dict[str, Any]:
    """A Flemish custom entry whose network was typed by hand."""
    return {
        const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_REGION: const.REGION_FLANDERS,
        const.CONF_DSO: "fluvius_zenne_dijle",
        const.CONF_METER: const.METER_MONO,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.1,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.04,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT: 0.03,
        const.CONF_CUSTOM_DSO_TRANSPORT: 0.01,
        const.CONF_CUSTOM_DSO_CAPACITY_EUR_PER_KW_YEAR: 50.0,
        const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 15.0,
        const.CONF_CUSTOM_VAT_RATE: 0.06,
        **extra,
    }


def _resolved_network(
    data: dict[str, Any],
    region: str = const.REGION_FLANDERS,
    dso: str = "fluvius_zenne_dijle",
    hour: int = 11,
) -> tuple[float, Any]:
    """The per-kWh network of 5 November 2026 at ``hour`` with VAT, on the
    entry's own tariff mode, and the overlay."""
    from datetime import date
    from types import SimpleNamespace

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.pricing import compute_breakdown
    from custom_components.be_electricity_prices.snapshot_resolve import (
        _resolve_snapshot,
    )

    raw = build_snapshot(data, region, dso)
    snap = _resolve_snapshot(
        SimpleNamespace(data=data),  # type: ignore[arg-type]
        raw,
        annual_kwh=3500.0,
        delivery_month=date(2026, 11, 1),
    )
    breakdown = compute_breakdown(
        snap,
        dso,
        region,
        datetime(2026, 11, 5, hour, tzinfo=dt_util.get_default_time_zone()),
        meter=data[const.CONF_METER],
        dso_tariff_mode=data.get(const.CONF_DSO_TARIFF_MODE, "bi_horaire"),
    )
    return breakdown.network, snap.dsos[dso]


def _hold_zenne_dijle_2026() -> None:
    from pathlib import Path

    from custom_components.be_electricity_prices import fluvius

    sheet = Path(__file__).parent / "fixtures" / "fluvius_zenne_dijle_2026.xlsx"
    terms = fluvius.parse_area_terms(sheet.read_bytes(), 2026)
    assert terms is not None
    fluvius._area[(2026, "fluvius_zenne_dijle")] = terms


def test_a_custom_entry_can_take_its_network_off_fluvius_s_sheet() -> None:
    """Ticked, the sheet of the delivery year replaces what was typed: on
    Zenne-Dijle for November 2026 the 5,87 c/kWh, 59,49 EUR/kW and 18,92 EUR
    a year every card prints, no transport, which the sheet's rates include,
    and no day/night split, which a peak-metered connection does not have."""
    _hold_zenne_dijle_2026()
    network, overlay = _resolved_network(
        _flemish_network(
            **{
                const.CONF_CUSTOM_DSO_FROM_FLUVIUS: True,
                const.CONF_CUSTOM_DSO_DISTRIBUTION_PEAK: 0.06,
                const.CONF_CUSTOM_DSO_DISTRIBUTION_OFFPEAK: 0.02,
            }
        )
    )
    assert round(network * 100, 2) == 5.87
    assert network == pytest.approx(0.0553921 * 1.06)
    assert overlay.transport == 0.0
    assert overlay.distribution_peak is None
    assert overlay.distribution_offpeak is None
    assert overlay.distribution_exclusive_night == pytest.approx(0.0501456)
    assert round(overlay.capacity_eur_per_kw_year, 2) == 59.49
    assert round(overlay.data_management_per_year, 2) == 18.92


def test_the_typed_network_stands_unticked_or_with_no_sheet_held() -> None:
    """Unticked, the figures typed are billed whatever sheet is held; ticked
    with none held, they stand in, so the entry still prices."""
    from custom_components.be_electricity_prices import fluvius

    typed = (0.04 + 0.01) * 1.06
    _hold_zenne_dijle_2026()
    network, overlay = _resolved_network(_flemish_network())
    assert network == pytest.approx(typed)
    assert overlay.transport == pytest.approx(0.01)
    fluvius._area.clear()
    network, _overlay = _resolved_network(
        _flemish_network(**{const.CONF_CUSTOM_DSO_FROM_FLUVIUS: True})
    )
    assert network == pytest.approx(typed)


def test_an_analog_meter_on_fluvius_s_network_pays_the_analog_rate() -> None:
    """The analog surcharge, kept off a custom entry's typed figures, is laid
    on the sheet's: 9,70 c/kWh on Zenne-Dijle in 2026, the analog column every
    card prints."""
    from custom_components.be_electricity_prices import fluvius

    _hold_zenne_dijle_2026()
    fluvius._analog[(2026, "fluvius_zenne_dijle")] = 0.0360809
    analog = {const.CONF_ANALOG_METER: True}
    network, _overlay = _resolved_network(
        _flemish_network(**{const.CONF_CUSTOM_DSO_FROM_FLUVIUS: True, **analog})
    )
    assert round(network * 100, 2) == 9.70
    network, _overlay = _resolved_network(_flemish_network(**analog))
    assert network == pytest.approx((0.04 + 0.01) * 1.06)


def test_only_a_flemish_network_step_offers_fluvius_s_sheet() -> None:
    """Every Flemish area is Fluvius's; the box comes first there and nowhere
    else."""
    for region, offered in (
        (const.REGION_FLANDERS, True),
        (const.REGION_WALLONIA, False),
        (const.REGION_BRUSSELS, False),
    ):
        keys = [
            str(k)
            for k in _custom_dso_schema(
                {const.CONF_REGION: region, const.CONF_METER: const.METER_MONO}
            ).schema
        ]
        assert (const.CONF_CUSTOM_DSO_FROM_FLUVIUS in keys) is offered, region
        if offered:
            assert keys[0] == const.CONF_CUSTOM_DSO_FROM_FLUVIUS


def _brussels_network(**extra: Any) -> dict[str, Any]:
    """A Brussels custom entry whose network was typed by hand."""
    return {
        const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_REGION: const.REGION_BRUSSELS,
        const.CONF_DSO: const.DSO_SIBELGA,
        const.CONF_METER: const.METER_MONO,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.1,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.04,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_EXCLUSIVE_NIGHT: 0.03,
        const.CONF_CUSTOM_DSO_TRANSPORT: 0.01,
        const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 15.0,
        const.CONF_CUSTOM_VAT_RATE: 0.06,
        **extra,
    }


@pytest.fixture
def brugel_2026() -> Any:
    """Brugel's 2026 sheet held as the tick would have read it."""
    from pathlib import Path

    from custom_components.be_electricity_prices import brugel
    from custom_components.be_electricity_prices.providers._pdf_render import (
        extract_pdf_text,
    )

    sheet = Path(__file__).parent / "fixtures" / "brugel_elec_2026.pdf"
    terms = brugel._parse_network(extract_pdf_text(sheet.read_bytes()))
    assert terms is not None
    brugel._network[2026] = terms
    yield terms
    brugel._network.clear()


def test_a_brussels_custom_entry_can_take_its_network_off_brugel_s_sheet(
    brugel_2026: Any,
) -> None:
    """Ticked, the sheet of the delivery year replaces what was typed: the
    9,96 c/kWh day rate and 2,27 c/kWh transport every commercial Brussels card
    prints, the 7,53 c/kWh night rate on an exclusive-night circuit, and the
    metering and power terms together, with the band above 13 kVA beside
    them."""
    ticked = _brussels_network(**{const.CONF_CUSTOM_DSO_FROM_BRUGEL: True})
    network, overlay = _resolved_network(ticked, const.REGION_BRUSSELS, "sibelga")
    assert round(network * 100, 2) == 12.23
    assert network == pytest.approx((brugel_2026.day + brugel_2026.transport) * 1.06)
    assert overlay.distribution_peak == pytest.approx(brugel_2026.day)
    assert overlay.distribution_offpeak == pytest.approx(brugel_2026.night)
    assert overlay.distribution_exclusive_night == pytest.approx(brugel_2026.night)
    # The yearly charges with the VAT the engine grosses fees by.
    assert overlay.data_management_per_year == pytest.approx(61.14 * 1.06)
    assert overlay.brussels_power_term_above_13kva == pytest.approx(108.38 * 1.06)
    night = {**ticked, const.CONF_METER: const.METER_EXCLUSIVE_NIGHT}
    network, _overlay = _resolved_network(night, const.REGION_BRUSSELS, "sibelga")
    assert round(network * 100, 2) == 9.80


def test_the_typed_brussels_network_stands_unticked_or_with_no_sheet_held(
    brugel_2026: Any,
) -> None:
    """Unticked, the figures typed are billed whatever sheet is held; ticked
    with none held, they stand in."""
    from custom_components.be_electricity_prices import brugel

    typed = (0.04 + 0.01) * 1.06
    network, overlay = _resolved_network(
        _brussels_network(), const.REGION_BRUSSELS, "sibelga"
    )
    assert network == pytest.approx(typed)
    assert overlay.data_management_per_year == pytest.approx(15.0 * 1.06)
    brugel._network.clear()
    network, _overlay = _resolved_network(
        _brussels_network(**{const.CONF_CUSTOM_DSO_FROM_BRUGEL: True}),
        const.REGION_BRUSSELS,
        "sibelga",
    )
    assert network == pytest.approx(typed)


def test_brugel_s_power_term_is_not_added_to_the_sheet_s_twice(
    brugel_2026: Any,
) -> None:
    """The sheet's yearly charge already carries the power term, so the step
    that completes a card printing the metering half alone leaves it be,
    while a typed metering half is still completed."""
    from custom_components.be_electricity_prices import snapshot_resolve

    with patch.object(
        snapshot_resolve, "cached_power_term", lambda year: (47.24, 94.48)
    ):
        _network, overlay = _resolved_network(
            _brussels_network(**{const.CONF_CUSTOM_DSO_FROM_BRUGEL: True}),
            const.REGION_BRUSSELS,
            "sibelga",
        )
        assert overlay.data_management_per_year == pytest.approx(61.14 * 1.06)
        _network, overlay = _resolved_network(
            _brussels_network(), const.REGION_BRUSSELS, "sibelga"
        )
        assert overlay.data_management_per_year == pytest.approx((15.0 + 47.24) * 1.06)


def test_each_region_s_network_step_offers_its_own_sheet() -> None:
    """Flanders offers Fluvius's sheet, Brussels Brugel's and Wallonia the
    CWaPE's grid, first on the step and nowhere else."""
    offered = {
        const.REGION_FLANDERS: const.CONF_CUSTOM_DSO_FROM_FLUVIUS,
        const.REGION_BRUSSELS: const.CONF_CUSTOM_DSO_FROM_BRUGEL,
        const.REGION_WALLONIA: const.CONF_CUSTOM_DSO_FROM_CWAPE,
    }
    boxes = set(offered.values())
    for region in (const.REGION_FLANDERS, const.REGION_BRUSSELS, const.REGION_WALLONIA):
        keys = [
            str(k)
            for k in _custom_dso_schema(
                {const.CONF_REGION: region, const.CONF_METER: const.METER_MONO}
            ).schema
        ]
        expected = offered.get(region)
        assert boxes & set(keys) == ({expected} if expected else set()), region
        if expected:
            assert keys[0] == expected


async def test_the_dispatcher_reads_the_sheet_of_the_entry_s_region() -> None:
    """One call where the tick, the backfill and an earlier contract read the
    sheet a custom entry takes its network from: Fluvius's area sheet in
    Flanders, Brugel's in Brussels, nothing for a typed network."""
    from unittest.mock import AsyncMock, call

    from custom_components.be_electricity_prices import brugel, snapshot_resolve

    session = object()
    area = AsyncMock()
    power = AsyncMock()
    with (
        patch.object(snapshot_resolve, "ensure_area_terms", area),
        patch.object(snapshot_resolve, "ensure_power_term", power),
    ):
        await snapshot_resolve.ensure_custom_network(
            session,  # type: ignore[arg-type]
            _flemish_network(**{const.CONF_CUSTOM_DSO_FROM_FLUVIUS: True}),
            2026,
        )
        area.assert_awaited_once_with(session, 2026, "fluvius_zenne_dijle")
        power.assert_not_awaited()
        ticked = _brussels_network(**{const.CONF_CUSTOM_DSO_FROM_BRUGEL: True})
        # Nothing held for 2027: the year before is read to stand in.
        await snapshot_resolve.ensure_custom_network(
            session,  # type: ignore[arg-type]
            ticked,
            2027,
        )
        assert power.await_args_list == [call(session, 2027), call(session, 2026)]
        # The year's own sheet held: nothing more.
        power.reset_mock()
        brugel._network[2027] = brugel.NetworkTerms(0.09, 0.07, 0.02, 60.0, 100.0)
        try:
            await snapshot_resolve.ensure_custom_network(
                session,  # type: ignore[arg-type]
                ticked,
                2027,
            )
        finally:
            brugel._network.clear()
        power.assert_awaited_once_with(session, 2027)
        area.reset_mock()
        power.reset_mock()
        for data in (_flemish_network(), _brussels_network()):
            await snapshot_resolve.ensure_custom_network(
                session,  # type: ignore[arg-type]
                data,
                2026,
            )
        area.assert_not_awaited()
        power.assert_not_awaited()


@pytest.mark.parametrize(
    ("professional", "factor"),
    [
        # A 0 rate typed by a household reads as VAT-inclusive figures.
        (False, 1.06),
        # A business typing 0 deducts VAT: its figures are excluding it.
        (True, 1.0),
    ],
)
def test_brugel_s_sheet_is_billed_on_the_basis_the_entry_typed_on(
    brugel_2026: Any, professional: bool, factor: float
) -> None:
    """The sheet is excluding VAT. An entry typing VAT-inclusive figures at a
    0 rate takes it grossed, as it takes Brugel's power term, and a business
    deducting VAT takes it as published."""
    data = _brussels_network(
        **{
            const.CONF_CUSTOM_DSO_FROM_BRUGEL: True,
            const.CONF_CUSTOM_VAT_RATE: 0.0,
            const.CONF_CUSTOM_PROFESSIONAL: professional,
        }
    )
    network, overlay = _resolved_network(data, const.REGION_BRUSSELS, "sibelga")
    assert network == pytest.approx((brugel_2026.day + brugel_2026.transport) * factor)
    assert overlay.data_management_per_year == pytest.approx(61.14 * factor)


def _walloon_network(**extra: Any) -> dict[str, Any]:
    """A Walloon custom entry on ORES whose network was typed by hand."""
    return {
        const.CONF_SUPPLIER: const.SUPPLIER_CUSTOM,
        const.CONF_CONTRACT: const.CUSTOM_CONTRACT_FIXED,
        const.CONF_REGION: const.REGION_WALLONIA,
        const.CONF_DSO: const.DSO_ORES,
        const.CONF_METER: const.METER_MONO,
        const.CONF_CUSTOM_ENERGY_SINGLE: 0.1,
        const.CONF_CUSTOM_DSO_DISTRIBUTION_SINGLE: 0.04,
        const.CONF_CUSTOM_DSO_TRANSPORT: 0.01,
        const.CONF_CUSTOM_DSO_DATA_MANAGEMENT_PER_YEAR: 15.0,
        const.CONF_CUSTOM_DSO_PROSUMER_EUR_PER_KVA_YEAR: 70.0,
        const.CONF_CUSTOM_VAT_RATE: 0.06,
        **extra,
    }


@pytest.fixture
def ores_2026() -> Any:
    """ORES's 2026 grid and the 2026 transport grid held as the tick would
    have read them."""
    from pathlib import Path

    from custom_components.be_electricity_prices import cwape

    fixtures = Path(__file__).parent / "fixtures"
    grid = cwape.parse_grid((fixtures / "cwape_ores_2026.xlsx").read_bytes())
    transport = cwape.parse_transport(
        (fixtures / "cwape_transport_2026.xlsx").read_bytes()
    )
    assert grid is not None and transport is not None
    cwape._grids[const.DSO_ORES] = {"grid": grid}
    cwape._transport["transport"] = transport
    yield grid, transport
    cwape._grids.clear()
    cwape._transport.clear()


def test_a_walloon_custom_entry_can_take_its_network_off_the_cwape_s_grid(
    ores_2026: Any,
) -> None:
    """Ticked, ORES's grid valid in November 2026 replaces what was typed: the
    11,98 c/kWh single rate and 2,74 c/kWh transport of the CWaPE's summary,
    and its fixed term and prosumer rate, 14,10 EUR a year and 85,84 EUR per
    kVA with VAT."""
    ticked = _walloon_network(**{const.CONF_CUSTOM_DSO_FROM_CWAPE: True})
    network, overlay = _resolved_network(ticked, const.REGION_WALLONIA, "ores")
    assert round(network * 100, 2) == 14.72
    assert round(overlay.data_management_per_year, 2) == 14.10
    assert round(overlay.prosumer_eur_per_kva_year, 2) == 85.84


def test_an_impact_entry_on_the_cwape_s_grid_bills_its_band(ores_2026: Any) -> None:
    """The PIC band from 17:00, 16,57 c/kWh with VAT, and the ECO one at
    noon, 5,09, each with transport."""
    impact = _walloon_network(
        **{
            const.CONF_CUSTOM_DSO_FROM_CWAPE: True,
            const.CONF_METER: const.METER_DYNAMIC,
            const.CONF_DSO_TARIFF_MODE: const.DSO_MODE_IMPACT,
        }
    )
    pic, _overlay = _resolved_network(impact, const.REGION_WALLONIA, "ores", hour=18)
    eco, _overlay = _resolved_network(impact, const.REGION_WALLONIA, "ores", hour=12)
    assert round(pic * 100, 2) == round(16.57474 + 2.74252, 2)
    assert round(eco * 100, 2) == round(5.08965 + 2.74252, 2)


def test_the_typed_walloon_network_stands_where_no_grid_is_held(
    ores_2026: Any,
) -> None:
    """Unticked, the figures typed are billed; ticked, each grid not held
    leaves its own figures typed: the transport typed beside the operator's
    rates until the transport grid is read."""
    from custom_components.be_electricity_prices import cwape

    typed = (0.04 + 0.01) * 1.06
    network, overlay = _resolved_network(
        _walloon_network(), const.REGION_WALLONIA, "ores"
    )
    assert network == pytest.approx(typed)
    assert overlay.prosumer_eur_per_kva_year == pytest.approx(70.0 * 1.06)
    grid, _transport = ores_2026
    cwape._transport.clear()
    ticked = _walloon_network(**{const.CONF_CUSTOM_DSO_FROM_CWAPE: True})
    network, overlay = _resolved_network(ticked, const.REGION_WALLONIA, "ores")
    assert network == pytest.approx((grid.single + 0.01) * 1.06)
    cwape._grids.clear()
    network, _overlay = _resolved_network(ticked, const.REGION_WALLONIA, "ores")
    assert network == pytest.approx(typed)


@pytest.mark.parametrize(("professional", "factor"), [(False, 1.06), (True, 1.0)])
def test_the_cwape_s_grid_is_billed_on_the_basis_the_entry_typed_on(
    ores_2026: Any, professional: bool, factor: float
) -> None:
    """Excluding VAT: grossed for a household typing VAT-inclusive figures at a
    0 rate, as published for a business deducting VAT."""
    grid, transport = ores_2026
    data = _walloon_network(
        **{
            const.CONF_CUSTOM_DSO_FROM_CWAPE: True,
            const.CONF_CUSTOM_VAT_RATE: 0.0,
            const.CONF_CUSTOM_PROFESSIONAL: professional,
        }
    )
    network, _overlay = _resolved_network(data, const.REGION_WALLONIA, "ores")
    assert network == pytest.approx((grid.single + transport.rate) * factor)


async def test_the_dispatcher_reads_the_cwape_s_grids_for_a_walloon_entry() -> None:
    from unittest.mock import AsyncMock

    from custom_components.be_electricity_prices import snapshot_resolve

    session = object()
    grids = AsyncMock()
    with patch.object(snapshot_resolve, "ensure_cwape_network", grids):
        await snapshot_resolve.ensure_custom_network(
            session,  # type: ignore[arg-type]
            _walloon_network(**{const.CONF_CUSTOM_DSO_FROM_CWAPE: True}),
            2027,
        )
        grids.assert_awaited_once_with(session, 2027, "ores")
        grids.reset_mock()
        await snapshot_resolve.ensure_custom_network(
            session,  # type: ignore[arg-type]
            _walloon_network(),
            2027,
        )
        grids.assert_not_awaited()
