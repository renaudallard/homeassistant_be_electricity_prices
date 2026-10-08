"""The static peak/offpeak sensors, from pull request #98.

They exist so a two-tariff meter can drive the energy dashboard, which wants
one price entity per grid source and cannot use `current_price`: that one
follows the clock and holds whichever band applies now. These hold the
contract's constant rate for each band instead.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from custom_components.be_electricity_prices.coordinator_data import CoordinatorData
from custom_components.be_electricity_prices.sensor import async_setup_entry
from tests import make_entry


def _added(entry: Any) -> set[str]:
    """The sensor keys the platform creates for one entry."""
    entry.runtime_data = SimpleNamespace(
        data=CoordinatorData(), entry=entry, intended_unique_ids={}
    )
    out: list[Any] = []

    async def _run() -> None:
        await async_setup_entry(
            None,  # type: ignore[arg-type]
            entry,
            lambda entities: out.extend(entities),  # type: ignore[arg-type]
        )

    asyncio.run(_run())
    return {e.entity_description.key for e in out if hasattr(e, "entity_description")}


BANDS = {"price_peak", "price_offpeak"}
INJECTION_BANDS = {"injection_price_peak", "injection_price_offpeak"}


def test_a_two_tariff_meter_gets_the_band_prices() -> None:
    assert _added(make_entry(meter="bi")) >= BANDS


def test_a_single_rate_meter_does_not() -> None:
    """A mono meter has no two bands to be billed on, so a peak price would
    sit unavailable for good. Two dead entities per entry is worse than
    none."""
    assert not (BANDS & _added(make_entry(meter="mono")))


def test_a_dynamic_contract_does_not() -> None:
    """A dynamic contract's price moves every hour; there is no constant for
    a band to report. Bolt's variable card settled per quarter-hour is one,
    as the meter step reads it."""
    assert not (BANDS & _added(make_entry(meter="dynamic", contract="power_dynamic")))
    bolt = make_entry(
        meter="dynamic",
        supplier="bolt",
        contract="bolt_variable",
        region="flanders",
        dso="fluvius_antwerpen",
        quarter_hourly=True,
    )
    assert not (BANDS & _added(bolt))


def test_a_digital_meter_on_a_two_rate_card_gets_the_band_prices() -> None:
    """The engine bills a digital meter's day and night registers on the
    card's two rates exactly as it bills a bi-hourly meter, and the Energy
    dashboard's two-tariff grid source needs one constant price for each."""
    assert _added(make_entry(meter="dynamic")) >= BANDS


def test_the_walloon_impact_tariff_gets_no_band_prices() -> None:
    """On the Impact tariff the energy leg follows the CWaPE bands whether or
    not the card prints them, so there is no constant day or night rate and
    the pair sat unavailable for good. A custom entry left without the bands
    bills one distribution rate and keeps them."""
    assert not (BANDS & _added(make_entry(meter="bi", dso_tariff_mode="impact")))
    assert _added(make_entry(meter="bi", dso_tariff_mode="bi_horaire")) >= BANDS
    custom = make_entry(
        meter="bi", supplier="custom", contract="custom_fixed", dso_tariff_mode="impact"
    )
    assert _added(custom) >= BANDS


def test_the_feed_in_pair_needs_both_a_two_tariff_meter_and_injection() -> None:
    both = _added(make_entry(meter="bi", solar_regime="injection", solar_kva=5.0))
    assert both >= INJECTION_BANDS

    no_injection = _added(make_entry(meter="bi"))
    assert not (INJECTION_BANDS & no_injection)

    mono = _added(make_entry(meter="mono", solar_regime="injection", solar_kva=5.0))
    assert not (INJECTION_BANDS & mono)


def test_the_feed_in_pair_follows_the_engine_onto_a_digital_meter() -> None:
    """The engine credits a register pair on both two-register meters, the
    bi-hourly and the digital one; the sensors were created for the first
    only, so a Trevion Vast entry on a digital meter was credited per
    register with no band sensor to show it."""
    both = _added(make_entry(meter="dynamic", solar_regime="injection", solar_kva=5.0))
    assert both >= INJECTION_BANDS
    assert not (
        INJECTION_BANDS & _added(make_entry(meter="mono", solar_regime="injection"))
    )


def test_a_band_with_no_constant_reads_unavailable_not_unknown() -> None:
    """With no band rate resolved, a monthly-indexed card before its month's
    mean is known for one, and no feed-in credit either, there is nothing for
    the band sensors to show. Unknown looks like a broken sensor; a figure
    the entry does not have is unavailable."""
    from unittest.mock import MagicMock

    from custom_components.be_electricity_prices.sensor import (
        BI_HOURLY_INJECTION_SENSORS,
        BI_HOURLY_SENSORS,
        SENSORS,
        BePriceSensor,
    )

    coordinator = MagicMock()
    coordinator.entry = make_entry(meter="bi", solar_regime="injection", solar_kva=5.0)
    coordinator.data = CoordinatorData()
    coordinator.last_update_success = True
    for description in (*BI_HOURLY_SENSORS, *BI_HOURLY_INJECTION_SENSORS):
        entity = BePriceSensor(coordinator, description)
        assert entity.available is False, description.key
    # The ordinary price sensors keep reading unknown on a missing value.
    entity = BePriceSensor(
        coordinator, next(d for d in SENSORS if d.key == "current_price")
    )
    assert entity.available is True


def test_the_feed_in_pair_shows_the_one_credit_a_card_gives_both_registers() -> None:
    """Only Trevion Vast prints a feed-in rate per register. Every other card
    credits both registers on its one formula or rate, so both sensors show
    the credit of the current slot, the one injection_price shows: it is what
    the register counting now is paid, and the Energy dashboard prices each
    return register off its own sensor. On Frank they sat unavailable."""
    from unittest.mock import MagicMock

    from custom_components.be_electricity_prices.sensor import (
        BI_HOURLY_INJECTION_SENSORS,
        INJECTION_SENSORS,
        BePriceSensor,
    )

    coordinator = MagicMock()
    coordinator.entry = make_entry(
        meter="dynamic", solar_regime="injection", solar_kva=5.0
    )
    coordinator.last_update_success = True

    coordinator.data = CoordinatorData(injection_price_eur_per_kwh=0.07052)
    (injection,) = INJECTION_SENSORS
    assert BePriceSensor(coordinator, injection).native_value == 0.07052
    for description in BI_HOURLY_INJECTION_SENSORS:
        entity = BePriceSensor(coordinator, description)
        assert entity.available is True, description.key
        assert entity.native_value == 0.07052, description.key

    # A card that prints the pair keeps its own constants.
    coordinator.data = CoordinatorData(
        injection_price_eur_per_kwh=0.057615,
        static_injection_peak=0.063329,
        static_injection_offpeak=0.04333,
    )
    peak, offpeak = (
        BePriceSensor(coordinator, d).native_value for d in BI_HOURLY_INJECTION_SENSORS
    )
    assert (peak, offpeak) == (0.063329, 0.04333)


def test_the_prosumer_cost_is_only_created_where_it_is_billed() -> None:
    """Wallonia alone bills the prosumer tariff. An entry saved with the
    compensation regime elsewhere, before the regime was restricted to
    Wallonia, read 0 for good."""

    def entry(region: str, dso: str) -> Any:
        return make_entry(
            region=region, dso=dso, solar_regime="compensation", solar_kva=5.0
        )

    assert "prosumer_cost" in _added(entry("wallonia", "ores"))
    for region, dso in (("flanders", "fluvius_imewo"), ("brussels", "sibelga")):
        assert "prosumer_cost" not in _added(entry(region, dso))


def test_no_saving_sensor_where_the_ranking_has_nothing_to_rank() -> None:
    """Engie Empower Flextime is the only slot contract sold in Brussels, so
    the nightly ranking skips such an entry and the sensor read unknown for
    good. It is created where there is an alternative to rank."""
    from unittest.mock import patch

    def created(**data: Any) -> bool:
        with patch(
            "custom_components.be_electricity_prices.sensor.PotentialSavingSensor"
        ) as sensor:
            _added(make_entry(daily_compare=True, **data))
        return sensor.called

    assert not created(
        supplier="engie",
        contract="engie_empower_flextime",
        region="brussels",
        dso="sibelga",
        meter="dynamic",
    )
    assert created(
        supplier="engie",
        contract="engie_empower_flextime",
        region="flanders",
        dso="fluvius_imewo",
        meter="dynamic",
    )
