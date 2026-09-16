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

from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.sensor import async_setup_entry
from tests import make_entry


def _added(entry: Any) -> set[str]:
    """The sensor keys the platform creates for one entry."""
    entry.runtime_data = SimpleNamespace(data=CoordinatorData(), entry=entry)
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
    assert BANDS <= _added(make_entry(meter="bi"))


def test_a_single_rate_meter_does_not() -> None:
    """A mono meter has no two bands to be billed on, so a peak price would
    sit unavailable for good. Two dead entities per entry is worse than
    none."""
    assert not (BANDS & _added(make_entry(meter="mono")))


def test_a_dynamic_meter_does_not() -> None:
    """A dynamic contract's price moves every hour; there is no constant for
    a band to report."""
    assert not (BANDS & _added(make_entry(meter="dynamic")))


def test_the_feed_in_pair_needs_both_a_two_tariff_meter_and_injection() -> None:
    both = _added(make_entry(meter="bi", solar_regime="injection", solar_kva=5.0))
    assert INJECTION_BANDS <= both

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
    assert INJECTION_BANDS <= both
    assert not (
        INJECTION_BANDS & _added(make_entry(meter="mono", solar_regime="injection"))
    )


def test_a_band_with_no_constant_reads_unavailable_not_unknown() -> None:
    """Only one card in the registry prints a feed-in register pair, and a
    bi-hourly meter on a monthly-indexed card has no constant day rate. The
    sensors read unknown for good on those, which looks like a broken
    sensor; a constant the card does not print is unavailable."""
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
