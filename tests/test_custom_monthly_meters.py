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

"""The custom monthly-average mode's formula per meter and its RLP index.

The figures are TotalEnergies' Groepsaankooptarief variabel 10-2026 card for
Flanders (issue #176): no base, one Belpex_M_RLP factor per meter, and an
October index of 181,85 EUR/MWh that its indicative rates all solve to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import voluptuous as vol

from custom_components.be_electricity_prices import const
from custom_components.be_electricity_prices.flow_schemas import (
    _CUSTOM_ENERGY_FALLBACK_KEYS,
    _drop_blanked,
)
from custom_components.be_electricity_prices.flow_schemas_custom import (
    _custom_energy_schema,
)
from custom_components.be_electricity_prices.pricing import energy_eur_per_kwh
from custom_components.be_electricity_prices.providers._rates import (
    SpotMonthlyRates,
)
from custom_components.be_electricity_prices.providers.custom import build_snapshot
from custom_components.be_electricity_prices.spot_stats import (
    _energy_is_rlp_indexed,
    _rlp_blend_for,
)

# A Wednesday in Brussels: 13:00 is a peak hour, 03:00 an off-peak one.
PEAK_HOUR = datetime(2026, 11, 18, 12, tzinfo=UTC)
OFFPEAK_HOUR = datetime(2026, 11, 18, 2, tzinfo=UTC)
# The card's October Belpex_M_RLP, in EUR/kWh.
INDEX = 0.18185

CARD = {
    const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
    const.CONF_CUSTOM_ENERGY_FACTOR: 1.0148,
    const.CONF_CUSTOM_ENERGY_BASE: 0.0,
    const.CONF_CUSTOM_ENERGY_FACTOR_PEAK: 1.1358,
    const.CONF_CUSTOM_ENERGY_FACTOR_OFFPEAK: 0.9088,
    const.CONF_CUSTOM_ENERGY_FACTOR_EXCLUSIVE_NIGHT: 0.9561,
    const.CONF_CUSTOM_ENERGY_RLP_WEIGHTED: True,
}


def _energy(data: dict[str, Any]) -> SpotMonthlyRates:
    snap = build_snapshot(data, const.REGION_FLANDERS, const.DSO_FLUVIUS_ANTWERPEN)
    assert isinstance(snap.energy, SpotMonthlyRates)
    return snap.energy


def _keys(meter: str, contract: str = const.CUSTOM_CONTRACT_MONTHLY) -> set[str]:
    schema = _custom_energy_schema(
        {const.CONF_CONTRACT: contract, const.CONF_METER: meter}
    )
    return {str(k) for k in schema.schema}


@pytest.mark.parametrize(
    ("meter", "when", "printed"),
    [
        ("mono", PEAK_HOUR, 0.1956),
        ("bi", PEAK_HOUR, 0.2189),
        ("bi", OFFPEAK_HOUR, 0.1752),
        ("dynamic", OFFPEAK_HOUR, 0.1752),
        ("exclusive_night", OFFPEAK_HOUR, 0.1843),
    ],
)
def test_each_meter_bills_the_card_s_own_formula(
    meter: str, when: datetime, printed: float
) -> None:
    """Every meter bills its column's indicative rate, to the printed cent."""
    rate = energy_eur_per_kwh(_energy(CARD), when, INDEX, meter)  # type: ignore[arg-type]
    assert rate * 1.06 == pytest.approx(printed, abs=0.00005)


def test_blank_boxes_take_the_energy_formula() -> None:
    """A pair is billed only whole, so the half left blank takes the energy
    factor and base, and so does a blank base."""
    energy = _energy(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.1,
            const.CONF_CUSTOM_ENERGY_BASE: 0.02,
            const.CONF_CUSTOM_ENERGY_FACTOR_PEAK: 1.3,
        }
    )
    assert (energy.factor_peak, energy.base_peak) == (1.3, 0.02)
    assert (energy.factor_offpeak, energy.base_offpeak) == (1.1, 0.02)
    assert energy.factor_exclusive_night is None
    assert energy.base_exclusive_night is None


def test_an_entry_typing_no_meter_formula_is_unchanged() -> None:
    """An entry from before these boxes builds the leg it always did."""
    energy = _energy(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0834,
            const.CONF_CUSTOM_ENERGY_BASE: 0.0,
        }
    )
    assert energy == SpotMonthlyRates(factor=1.0834, base=0.0)
    assert not _energy_is_rlp_indexed(energy)


def test_the_rlp_box_indexes_the_leg_on_the_column_blend() -> None:
    """Ticked, the leg resolves against the RLP-weighted mean, in the blend
    TotalEnergies' cards solve to; it is what makes the tick fetch the
    profile."""
    energy = _energy(CARD)
    assert _energy_is_rlp_indexed(energy)
    assert _rlp_blend_for(energy) == "columns"


def test_the_form_asks_for_each_meter_s_formula_without_a_default() -> None:
    """Only the boxes a meter bills are shown, none with a default: a default
    would be submitted untouched and bill that figure instead of falling
    back."""
    pair = {
        const.CONF_CUSTOM_ENERGY_FACTOR_PEAK,
        const.CONF_CUSTOM_ENERGY_BASE_PEAK,
        const.CONF_CUSTOM_ENERGY_FACTOR_OFFPEAK,
        const.CONF_CUSTOM_ENERGY_BASE_OFFPEAK,
    }
    night = {
        const.CONF_CUSTOM_ENERGY_FACTOR_EXCLUSIVE_NIGHT,
        const.CONF_CUSTOM_ENERGY_BASE_EXCLUSIVE_NIGHT,
    }
    for meter in (const.METER_BI, const.METER_DYNAMIC):
        assert _keys(meter) & (pair | night) == pair, meter
    assert _keys(const.METER_EXCLUSIVE_NIGHT) & (pair | night) == night
    assert not _keys(const.METER_MONO) & (pair | night)
    schema = _custom_energy_schema(
        {
            const.CONF_CONTRACT: const.CUSTOM_CONTRACT_MONTHLY,
            const.CONF_METER: const.METER_BI,
        }
    )
    for key in schema.schema:
        if str(key) in pair:
            assert getattr(key, "default", vol.UNDEFINED) is vol.UNDEFINED, key


def test_only_the_monthly_mode_offers_the_new_boxes() -> None:
    """The dynamic mode prices the live spot and the fixed one no index."""
    new = {
        const.CONF_CUSTOM_ENERGY_FACTOR_PEAK,
        const.CONF_CUSTOM_ENERGY_RLP_WEIGHTED,
    }
    assert new <= _keys(const.METER_BI)
    for contract in (const.CUSTOM_CONTRACT_DYNAMIC, const.CUSTOM_CONTRACT_FIXED):
        assert not new & _keys(const.METER_BI, contract), contract


def test_a_meter_change_drops_the_formula_it_no_longer_shows() -> None:
    """The energy step pops every per-meter box it was not sent, so a bi-hourly
    pair left behind by a switch to a mono meter is not kept in the entry."""
    data = dict(CARD)
    _drop_blanked(
        data,
        {
            const.CONF_CUSTOM_ENERGY_FACTOR: 1.0148,
            const.CONF_CUSTOM_ENERGY_BASE: 0.0,
        },
        _CUSTOM_ENERGY_FALLBACK_KEYS,
    )
    energy = _energy(data)
    assert energy.factor_peak is None
    assert energy.factor_offpeak is None
    assert energy.factor_exclusive_night is None
