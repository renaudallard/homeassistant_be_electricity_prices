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
