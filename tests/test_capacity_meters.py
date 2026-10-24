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

"""The Flemish capacity charge per meter.

Fluvius bills a household with an exclusive-night meter one monthly peak, the
highest simultaneous sum of both meters' quarter-hour peaks, so the night
circuit's own entry is billed no charge: the main entry carries it. An analog
meter has no peak to measure: it is billed the charge on the minimum peak and
a higher kWh rate, read off its area's Fluvius sheet.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.button import (
    async_setup_entry as button_setup,
)
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from custom_components.be_electricity_prices.coordinator_data import CoordinatorData
from custom_components.be_electricity_prices.fees import (
    _compute_capacity,
    bills_capacity,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    TaxOverlay,
)
from custom_components.be_electricity_prices.sensor import (
    async_setup_entry as sensor_setup,
)
from custom_components.be_electricity_prices.snapshot_resolve import (
    _resolve_snapshot,
    analog_meter,
)
from tests import make_entry, make_snapshot
from tests.test_config_flow_energy_defaults import _add_dsmr_monthly_peak, _shown

_PEAK = "sensor.house_power"


def _flemish(meter: str, **extra: Any) -> MockConfigEntry:
    return make_entry(
        region="flanders",
        dso="fluvius_antwerpen",
        meter=meter,
        title=f"Eneco ({meter})",
        **extra,
    )


@pytest.mark.parametrize(
    ("region", "meter", "billed"),
    [
        ("flanders", "mono", True),
        ("flanders", "bi", True),
        ("flanders", "dynamic", True),
        ("flanders", "exclusive_night", False),
        ("wallonia", "mono", False),
        ("brussels", "bi", False),
    ],
)
def test_only_a_flemish_main_meter_is_billed_a_charge_of_its_own(
    region: str, meter: str, billed: bool
) -> None:
    assert bills_capacity({"region": region, "meter": meter}) is billed
    # Analog or not: the regulator bills an analog household one flat amount,
    # on the main entry.
    assert (
        bills_capacity({"region": region, "meter": meter, "analog_meter": True})
        is billed
    )


async def test_a_night_circuit_tracks_no_peak_and_is_billed_none(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The night entry was offered the household's DSMR peak sensor and billed
    a second full charge on it, 272,32 EUR a year at 5,2 kW on Fluvius
    Antwerpen, and 130,92 on the 2,5 kW minimum with the sensor cleared or a
    fixed peak stated. Every path prices the charge off the billed peak."""
    freezer.move_to("2026-10-23 12:00:00+02:00")
    hass.states.async_set(_PEAK, "5.2", {"unit_of_measurement": "kW"})
    billed: dict[str, float] = {}
    for label, entry in (
        ("main", _flemish("mono", capacity_mode="sensor", capacity_peak_sensor=_PEAK)),
        (
            "night",
            _flemish(
                "exclusive_night", capacity_mode="sensor", capacity_peak_sensor=_PEAK
            ),
        ),
        ("night, sensor cleared", _flemish("exclusive_night", capacity_mode="sensor")),
        (
            "night, fixed",
            _flemish("exclusive_night", capacity_mode="fixed", capacity_fixed_kw=4.0),
        ),
    ):
        entry.add_to_hass(hass)
        coord = BePricesCoordinator(hass, entry)
        coord._peak_history = {"2026-09-01": 6.0}
        await coord._track_monthly_peak()
        billed[label] = coord._billed_peak_kw()
        if label != "main":
            assert coord._peak_kw == 0.0, label
            assert coord._peak_history == {}, label
    assert billed == {
        "main": pytest.approx((6.0 + 5.2) / 2),
        "night": 0.0,
        "night, sensor cleared": 0.0,
        "night, fixed": 0.0,
    }


def _sensor_keys(entry: MockConfigEntry) -> set[str]:
    entry.runtime_data = SimpleNamespace(  # type: ignore[misc]
        data=CoordinatorData(), entry=entry, intended_unique_ids={}
    )
    out: list[Any] = []

    async def _run() -> None:
        await sensor_setup(None, entry, out.extend)  # type: ignore[arg-type]

    asyncio.run(_run())
    return {e.entity_description.key for e in out if hasattr(e, "entity_description")}


def test_a_night_circuit_gets_no_capacity_sensors() -> None:
    capacity = {"capacity_cost", "monthly_peak_kw"}
    assert capacity <= _sensor_keys(_flemish("mono"))
    assert not capacity & _sensor_keys(_flemish("exclusive_night"))


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_a_night_circuit_gets_no_reset_peak_button(hass: HomeAssistant) -> None:
    for meter, analog, count in (
        ("mono", False, 1),
        ("exclusive_night", False, 0),
        ("exclusive_night", True, 0),
    ):
        entry = _flemish(
            meter,
            capacity_mode="sensor",
            capacity_peak_sensor=_PEAK,
            analog_meter=analog,
        )
        entry.add_to_hass(hass)
        entry.runtime_data = BePricesCoordinator(hass, entry)  # type: ignore[misc]
        added: list[Any] = []
        await button_setup(hass, entry, added.extend)  # type: ignore[arg-type]
        assert len(added) == count, (meter, analog)


async def _steps(
    hass: HomeAssistant, entry: MockConfigEntry, answers: dict[str, dict[str, Any]]
) -> list[str]:
    """The steps Edit settings walks, every form submitted as shown but for
    ``answers``, up to the energy meters (not submitted)."""
    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    result = await opts.async_init(entry.entry_id)
    result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
    steps: list[str] = []
    while result["type"] == FlowResultType.FORM and result["step_id"] != "meters":
        steps.append(result["step_id"])
        if result["step_id"] == "capacity":
            # What the step offers the entry before anything is submitted.
            steps.append(f"offered {_shown(result).get('capacity_peak_sensor')}")
        result = await opts.async_configure(
            result["flow_id"],
            {**_shown(result), **answers.get(result["step_id"], {})},
        )
    return steps


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_an_edit_onto_a_night_circuit_skips_the_capacity_step(
    hass: HomeAssistant,
) -> None:
    """Asked of the night entry, the step offered the household's own DSMR
    peak sensor, which is how the second charge was billed in full."""
    peak = _add_dsmr_monthly_peak(hass)
    entry = _flemish("mono", solar_regime="none")
    entry.add_to_hass(hass)
    main = await _steps(hass, entry, {})
    assert "capacity" in main, "a main meter still has a peak to give"
    night = await _steps(hass, entry, {"meter": {"meter": "exclusive_night"}})
    assert "capacity" not in night
    assert "solar" in night
    assert f"offered {peak}" not in night


# An analog meter: Fluvius "kan je pieken niet meten. Je betaalt dan een
# forfaitair bedrag voor 2.5 kW als maandpiek en een aanvullend tarief voor je
# verbruik in kWh". The surcharge is Fluvius Antwerpen's for 2026, 0,0492427
# less 0,0234492, and the card figures below are Luminus Comfy's October 2026
# row for the area, whose "Compteur classique" columns print 130,92 EUR a year,
# 8,09 c/kWh and 7,55 c/kWh on the exclusive-night circuit.
_ANTWERPEN_2026 = 0.0257935


def _card(supplier: str = "luminus") -> Any:
    return make_snapshot(
        supplier=supplier,
        contract="luminus_comfy",
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.0535,
                distribution_exclusive_night=0.0481,
                transport=0.0,
                capacity_eur_per_kw_year=52.37,
                network_ceiling_eur_per_kwh=0.3472738,
            ),
            "fluvius_west": DsoOverlay(distribution_single=0.0566, transport=0.0),
        },
    )


@pytest.mark.parametrize(
    ("data", "analog"),
    [
        ({}, True),
        ({"meter": "bi"}, True),
        ({"meter": "exclusive_night"}, True),
        ({"meter": "dynamic"}, False),
        ({"analog_meter": False}, False),
        ({"region": "wallonia", "dso": "ores"}, False),
        ({"supplier": "custom"}, False),
        ({"supplier": "social"}, False),
    ],
)
def test_an_analog_meter_is_a_flemish_one_billed_on_fluvius_tariffs(
    data: dict[str, Any], analog: bool
) -> None:
    """Only Fluvius bills it apart, a dynamic contract needs a digital meter,
    the custom supplier bills the network figures typed for the meter, and the
    social tariff's network component is the CREG's."""
    entry = _flemish("mono", analog_meter=True)
    assert analog_meter({**entry.data, **data}) is analog


def test_an_analog_meter_is_billed_the_areas_kwh_rate_and_no_ceiling() -> None:
    from datetime import date

    from custom_components.be_electricity_prices import fluvius

    fluvius._analog[(2026, "fluvius_antwerpen")] = _ANTWERPEN_2026
    october = date(2026, 10, 1)
    card = _card()
    analog = _resolve_snapshot(
        _flemish("mono", analog_meter=True),
        card,
        annual_kwh=3500.0,
        delivery_month=october,
    )
    overlay = analog.dsos["fluvius_antwerpen"]
    # On the card's VAT-inclusive basis: 8,08 and 7,54 c/kWh against the 8,09
    # and 7,55 the card rounds the sheet's own sums to.
    assert overlay.distribution_single == pytest.approx(0.0535 + _ANTWERPEN_2026 * 1.06)
    assert overlay.distribution_exclusive_night == pytest.approx(
        0.0481 + _ANTWERPEN_2026 * 1.06
    )
    assert overlay.network_ceiling_eur_per_kwh is None
    # The capacity rate stays, billed on the minimum peak: 2,5 x 52,37.
    assert overlay.capacity_eur_per_kw_year == 52.37
    # Another area's figures are its own.
    assert analog.dsos["fluvius_west"].distribution_single == 0.0566
    for label, entry, snap in (
        ("digital", _flemish("mono"), card),
        ("dynamic", _flemish("dynamic", analog_meter=True), card),
        ("custom card", _flemish("mono", analog_meter=True), _card("custom")),
    ):
        same = _resolve_snapshot(
            entry, snap, annual_kwh=3500.0, delivery_month=october
        ).dsos["fluvius_antwerpen"]
        assert same.distribution_single == 0.0535, label
        assert same.network_ceiling_eur_per_kwh is not None, label
    # Before the area's sheet is read the meter is billed as before.
    fluvius._analog.clear()
    before = _resolve_snapshot(
        _flemish("mono", analog_meter=True),
        card,
        annual_kwh=3500.0,
        delivery_month=october,
    )
    assert before.dsos["fluvius_antwerpen"].distribution_single == 0.0535


async def test_an_analog_meter_is_billed_the_minimum_peak(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Nothing measured and nothing stated counts: 2,5 kW, the sheet's "Vaste
    term" of 130,92 EUR a year on Luminus's 52,37 per kW. A night meter pays
    none, analog or digital: "Heb je nog een klassieke meter? Dan betaal je het
    capaciteitstarief als een vast bedrag", one for the household, which the
    main entry carries."""
    freezer.move_to("2026-10-23 12:00:00+02:00")
    hass.states.async_set(_PEAK, "5.2", {"unit_of_measurement": "kW"})
    billed: dict[str, float] = {}
    for label, entry in (
        (
            "analog",
            _flemish(
                "mono",
                analog_meter=True,
                capacity_mode="sensor",
                capacity_peak_sensor=_PEAK,
            ),
        ),
        (
            "analog, fixed",
            _flemish(
                "bi", analog_meter=True, capacity_mode="fixed", capacity_fixed_kw=6.0
            ),
        ),
        ("analog night", _flemish("exclusive_night", analog_meter=True)),
        ("digital night", _flemish("exclusive_night")),
    ):
        entry.add_to_hass(hass)
        coord = BePricesCoordinator(hass, entry)
        await coord._track_monthly_peak()
        assert coord._peak_kw == 0.0, label
        assert coord._peak_terms() == [], label
        billed[label] = coord._billed_peak_kw()
    assert billed == {
        "analog": 2.5,
        "analog, fixed": 2.5,
        "analog night": 0.0,
        "digital night": 0.0,
    }
    analog = _flemish("mono", analog_meter=True)
    assert 12 * _compute_capacity(_card(), analog, 2.5) == pytest.approx(130.925)


def test_an_analog_night_meter_gets_no_capacity_sensors_either() -> None:
    """The household's one flat amount is on the main entry's capacity_cost."""
    assert not {"capacity_cost", "monthly_peak_kw"} & _sensor_keys(
        _flemish("exclusive_night", analog_meter=True)
    )


async def test_the_tick_reads_the_areas_sheet_and_the_year_before_it(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Until Fluvius publishes the year, the year before stands in, and the
    card in hand is re-resolved the moment the rate arrives. The tick bills
    the minimum peak's charge, 130,92 EUR a year."""
    from unittest.mock import AsyncMock, patch

    from custom_components.be_electricity_prices import fluvius
    from custom_components.be_electricity_prices.cohort import _CohortLegs

    freezer.move_to("2027-01-05 12:00:00+01:00")
    entry = _flemish("mono", analog_meter=True)
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._set_snapshot(_card())
    booted = coord._snapshot
    assert booted is not None
    assert booted.dsos["fluvius_antwerpen"].distribution_single == 0.0535

    async def _read(_session: Any, year: int, dso: str) -> float | None:
        if year == 2026:
            fluvius._analog[(year, dso)] = _ANTWERPEN_2026
            return _ANTWERPEN_2026
        return None

    read = AsyncMock(side_effect=_read)
    coord._maybe_refresh_snapshot = AsyncMock()  # type: ignore[method-assign]
    with (
        patch(
            "custom_components.be_electricity_prices.coordinator_tick."
            "ensure_analog_surcharge",
            read,
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_tick._cohort_legs",
            AsyncMock(return_value=_CohortLegs(None, None)),
        ),
        patch(
            "custom_components.be_electricity_prices.coordinator_costs."
            "_compute_current_year_cost",
            AsyncMock(return_value=0.0),
        ),
        patch.object(coord, "_save_persistent", AsyncMock()),
    ):
        data = await coord._update_body()
    assert [call.args[1:] for call in read.await_args_list] == [
        (2027, "fluvius_antwerpen"),
        (2026, "fluvius_antwerpen"),
    ]
    resolved = coord._snapshot
    assert resolved is not None and resolved is not booted
    assert resolved.dsos["fluvius_antwerpen"].distribution_single == pytest.approx(
        0.0535 + _ANTWERPEN_2026 * 1.06
    )
    assert data.capacity_billed_peak_kw == 2.5
    assert 12 * data.capacity_cost_eur == pytest.approx(130.925)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_the_meter_step_asks_and_drops_the_analog_answer(
    hass: HomeAssistant,
) -> None:
    """Asked of a Flemish meter only, never kept on a dynamic one, and an
    analog meter has no peak source to give."""
    entry = _flemish("mono", solar_regime="none")
    entry.add_to_hass(hass)
    steps = await _steps(hass, entry, {"meter": {"analog_meter": True}})
    assert "capacity" not in steps
    assert "solar" in steps

    wallonia = make_entry(solar_regime="none")
    wallonia.add_to_hass(hass)
    shown = await _form(hass, wallonia, "meter")
    assert "analog_meter" not in shown
    shown = await _form(hass, entry, "meter")
    assert shown["analog_meter"] is False

    stored = _flemish("mono", solar_regime="none", analog_meter=True)
    stored.add_to_hass(hass)
    shown = await _form(hass, stored, "meter")
    assert shown["analog_meter"] is True
    data = await _saved(hass, stored, {"meter": {"meter": "dynamic"}})
    assert "analog_meter" not in data


async def _form(hass: HomeAssistant, entry: MockConfigEntry, step: str) -> Any:
    """What Edit settings shows on ``step``, every form before it as shown."""
    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    result = await opts.async_init(entry.entry_id)
    result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
    while result["type"] == FlowResultType.FORM and result["step_id"] != step:
        result = await opts.async_configure(result["flow_id"], _shown(result))
    return _shown(result)


async def _saved(
    hass: HomeAssistant, entry: MockConfigEntry, answers: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """The entry's data once Edit settings is walked through with ``answers``."""
    from unittest.mock import patch

    from homeassistant.data_entry_flow import FlowResultType

    opts = hass.config_entries.options
    with patch(
        "custom_components.be_electricity_prices.async_setup_entry",
        return_value=True,
    ):
        result = await opts.async_init(entry.entry_id)
        result = await opts.async_configure(result["flow_id"], {"next_step_id": "edit"})
        while result["type"] == FlowResultType.FORM:
            result = await opts.async_configure(
                result["flow_id"],
                {**_shown(result), **answers.get(result["step_id"], {})},
            )
    return dict(entry.data)


def test_an_earlier_contract_takes_the_meters_analog_answer() -> None:
    """A contract recorded before the question was asked was on the same
    meter; one recorded after keeps the answer it was given."""
    from datetime import date

    from custom_components.be_electricity_prices.contract_periods import (
        previous_periods,
    )

    def _held(**extra: Any) -> dict[str, Any]:
        return {
            "previous_contracts": [
                {
                    "until": "2026-06-30",
                    "data": {
                        "supplier": "engie",
                        "contract": "engie_easy_fixed",
                        "region": "flanders",
                        "dso": "fluvius_antwerpen",
                        "meter": "mono",
                        **extra,
                    },
                }
            ]
        }

    entry = {**_flemish("mono", analog_meter=True).data}
    for held, analog in (({}, True), ({"analog_meter": False}, False)):
        periods = previous_periods(
            {**entry, **_held(**held)}, date(2026, 1, 1), date(2026, 10, 23)
        )
        assert [analog_meter(p.data) for p in periods] == [analog], held


async def test_an_earlier_contract_is_billed_its_own_meters_peak(
    hass: HomeAssistant, freezer: Any
) -> None:
    """A contract kept at a recorded switch is billed the peak of the meter it
    ran on: the minimum on an analog meter, none on a night circuit or outside
    Flanders, the household's own on a digital one. Billing an analog half-year
    on the 5,2 kW the digital meter after it measures charged 70,70 EUR too
    much on Fluvius Antwerpen."""
    from datetime import date
    from unittest.mock import patch

    from custom_components.be_electricity_prices import contract_periods
    from custom_components.be_electricity_prices.compare_inputs import _QuoteEntry
    from tests import make_stub_extractor

    freezer.move_to("2026-10-23 13:00:00+02:00")
    card = _card()
    seen: list[float] = []

    async def _cost(*_args: Any, **kwargs: Any) -> float:
        seen.append(kwargs["billed_peak_kw"])
        return 0.0

    async def _period_card(
        _hass: Any, _session: Any, coordinator: Any, period: Any, _overrides: Any = None
    ) -> Any:
        proxy = _QuoteEntry(data=dict(period.data), runtime_data=coordinator)
        return proxy, make_stub_extractor(), card, False, False, False, None

    for held, peak in (
        ({}, 5.2),
        ({"analog_meter": True}, 2.5),
        ({"meter": "exclusive_night"}, 0.0),
        ({"region": "wallonia", "dso": "ores"}, 0.0),
    ):
        earlier = {**_flemish("mono").data, **held}
        entry = _flemish(
            "mono",
            capacity_mode="sensor",
            capacity_peak_sensor=_PEAK,
            previous_contracts=[{"until": "2026-07-01", "data": earlier}],
        )
        entry.add_to_hass(hass)
        coord = BePricesCoordinator(hass, entry)
        coord._peak_history = {"2026-08-01": 5.2, "2026-09-01": 5.2}
        periods = contract_periods.previous_periods(
            entry.data, date(2026, 1, 1), date(2026, 10, 23)
        )
        seen.clear()
        with (
            patch.object(contract_periods, "period_card", new=_period_card),
            patch.object(contract_periods, "_compute_current_year_cost", new=_cost),
        ):
            await contract_periods.price_previous_periods(
                hass,
                None,  # type: ignore[arg-type]
                coord,
                periods,
                month_start=date(2026, 10, 1),
            )
        assert seen == [pytest.approx(peak)], held
        # The one peak watched for a re-pricing is the one each is billed on.
        assert coord._previous_peak_kw(periods) == pytest.approx(peak), held


async def test_the_backfill_bills_a_contract_on_its_own_meters_peak(
    hass: HomeAssistant,
) -> None:
    """The backfill asks each contract's own settings too, so an analog one
    accrues the flat charge whatever peak the coordinator measures."""
    from datetime import UTC, date, datetime, timedelta
    from unittest.mock import AsyncMock, MagicMock, patch

    from homeassistant.helpers import entity_registry as er
    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices import backfill as bf
    from custom_components.be_electricity_prices import (
        cohort,
        energy_meters,
        ytd_cost,
        ytd_energy,
    )
    from custom_components.be_electricity_prices.cohort import _CohortLegs
    from custom_components.be_electricity_prices.const import DOMAIN
    from custom_components.be_electricity_prices.providers._rates import FixedRates
    from tests import make_stub_extractor

    snap = make_snapshot(
        energy=FixedRates(single=0.20, yearly_fixed_fee=60.0),
        dsos={
            "fluvius_antwerpen": DsoOverlay(
                distribution_single=0.10,
                transport=0.0145,
                capacity_eur_per_kw_year=52.37,
                data_management_per_year=18.0,
            )
        },
        taxes=TaxOverlay(federal_excise=0.04876, energy_contribution=0.0),
    )
    entry = _flemish(
        "mono",
        analog_meter=True,
        solar_regime="none",
        consumption_kwh="sensor.cons_total",
    )
    entry.add_to_hass(hass)
    er.async_get(hass).async_get_or_create(
        "sensor",
        DOMAIN,
        f"{entry.entry_id}_current_year_cost",
        suggested_object_id="analog_current_year_cost",
        config_entry=entry,
    )
    start = dt_util.start_of_local_day(date(2026, 1, 1)).astimezone(UTC)
    stop = dt_util.start_of_local_day(date(2026, 2, 1)).astimezone(UTC)
    hours: list[datetime] = []
    when = start
    while when < stop:
        hours.append(when)
        when += timedelta(hours=1)
    per_hour = {h: 0.4 for h in hours}
    per_day: dict[date, float] = {}
    for h in hours:
        local_day = dt_util.as_local(h).date()
        per_day[local_day] = per_day.get(local_day, 0.0) + 0.4
    coordinator = SimpleNamespace(
        hass=hass,
        _snapshot=snap,
        _session=None,
        _historical_spots={},
        _historical_spot_quarters={},
        _spp_weights={},
        _rlp_weights={},
        _ensure_historical_spots=AsyncMock(),
        _ensure_spp_weights=AsyncMock(),
        _ensure_rlp_weights=AsyncMock(),
        # What a digital meter measures; the analog contract is billed 2,5 kW.
        _billed_peak_kw=lambda: 5.0,
    )
    entry.runtime_data = coordinator  # type: ignore[misc]

    async def fake_hourly(
        _h: Any, entity_id: str, _s: Any, _e: Any
    ) -> dict[datetime, float]:
        return dict(per_hour) if entity_id == "sensor.cons_total" else {}

    async def fake_daily(
        _h: Any, entity_id: str, _s: Any, _e: Any
    ) -> dict[date, float]:
        return dict(per_day) if entity_id == "sensor.cons_total" else {}

    captured: list[dict[str, Any]] = []

    def fake_import(_h: Any, _meta: Any, stats: Any) -> None:
        captured.extend(stats)

    instance = MagicMock()
    instance.async_add_executor_job = AsyncMock(return_value={})
    with (
        patch.object(energy_meters, "_recorder_hourly_kwh", new=fake_hourly),
        patch.object(energy_meters, "_recorder_daily_kwh", new=fake_daily),
        patch.object(ytd_energy, "_top_up_today_hourly", new=AsyncMock()),
        patch.object(
            cohort, "_effective_snapshot_for_month", AsyncMock(return_value=snap)
        ),
        patch.object(
            ytd_cost, "_effective_snapshot_for_month", AsyncMock(return_value=snap)
        ),
        patch.object(
            ytd_cost, "_cohort_legs", AsyncMock(return_value=_CohortLegs(None, None))
        ),
        patch.object(bf, "BePricesCoordinator", SimpleNamespace),
        patch(
            "homeassistant.components.recorder.statistics.async_import_statistics",
            new=fake_import,
        ),
        patch("homeassistant.components.recorder.get_instance", return_value=instance),
        patch(
            "homeassistant.util.dt.now",
            lambda: (
                dt_util.start_of_local_day(date(2026, 1, 31))
                + timedelta(hours=23, minutes=59)
            ),
        ),
    ):
        await bf._backfill_cost_sensor(
            hass,
            entry,
            coordinator,  # type: ignore[arg-type]
            hours,
            {},
            {},
        )
        live = {
            peak: await ytd_cost._compute_current_year_cost(
                hass,
                None,  # type: ignore[arg-type]
                make_stub_extractor(),
                snap,
                entry,
                billed_peak_kw=peak,
            )
            for peak in (2.5, 5.0)
        }
    assert captured, "the backfill imported nothing"
    assert live[2.5] is not None and live[5.0] is not None
    assert live[5.0] - live[2.5] > 10.0
    assert captured[-1]["sum"] == pytest.approx(live[2.5], abs=1e-3)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_leaving_an_analog_meter_offers_the_new_meters_peak(
    hass: HomeAssistant,
) -> None:
    """An analog entry never met the capacity step, so once Fluvius replaces
    the meter and the box is unticked the step is new to it and offers the
    DSMR peak sensor, as at install: left blank, the entry is billed the 2,5 kW
    minimum where the household's peak is higher."""
    peak = _add_dsmr_monthly_peak(hass)
    for meter in ("mono", "exclusive_night"):
        entry = _flemish(meter, solar_regime="none", analog_meter=True)
        entry.add_to_hass(hass)
        steps = await _steps(
            hass, entry, {"meter": {"meter": "mono", "analog_meter": False}}
        )
        assert f"offered {peak}" in steps, meter
    # A digital entry that cleared its sensor keeps it cleared.
    digital = _flemish("mono", solar_regime="none")
    digital.add_to_hass(hass)
    assert f"offered {peak}" not in await _steps(hass, digital, {})


async def test_ticking_analog_drops_the_peaks_measured_before(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Months a clamp sensor measured before the box was ticked belong to no
    meter Fluvius bills on: kept, they came back into the mean the day the
    replaced meter's box was unticked, 6 kW against the 3 kW it reads."""
    freezer.move_to("2026-10-23 12:00:00+02:00")
    hass.states.async_set(_PEAK, "3.0", {"unit_of_measurement": "kW"})
    entry = _flemish(
        "mono",
        analog_meter=True,
        capacity_mode="sensor",
        capacity_peak_sensor=_PEAK,
    )
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    coord._peak_history = {f"2026-{m:02d}-01": 6.0 for m in range(3, 7)}
    coord._peak_kw = 6.0
    await coord._track_monthly_peak()
    assert (coord._peak_kw, coord._peak_history) == (0.0, {})
    assert coord._billed_peak_kw() == 2.5
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "analog_meter": False}
    )
    await coord._track_monthly_peak()
    assert coord._peak_terms() == [3.0]
    assert coord._billed_peak_kw() == 3.0
