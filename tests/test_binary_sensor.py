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

"""Tests for the tomorrow_prices_available binary sensor entity."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.binary_sensor import (
    TomorrowPricesAvailable,
)
from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.pricing import PriceBreakdown


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "supplier": "eneco",
            "contract": "power_dynamic",
            "region": "wallonia",
            "dso": "ores",
            "meter": "dynamic",
        },
        title="Eneco - Eneco Zon & Wind Dynamisch (Wallonia)",
    )


def _coord(data: CoordinatorData, entry: MockConfigEntry) -> SimpleNamespace:
    return SimpleNamespace(data=data, entry=entry)


def _today_and_tomorrow_data(today_n: int, tomorrow_n: int) -> CoordinatorData:
    midnight = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)
    hourly: dict[datetime, PriceBreakdown] = {}
    for i in range(today_n):
        hourly[dt_util.as_utc(midnight + timedelta(hours=i))] = PriceBreakdown(
            energy=0.10, network=0.0, taxes=0.0, all_in=0.10
        )
    for i in range(tomorrow_n):
        hourly[dt_util.as_utc(midnight + timedelta(days=1, hours=i))] = PriceBreakdown(
            energy=0.20, network=0.0, taxes=0.0, all_in=0.20
        )
    return CoordinatorData(hourly=hourly)


def test_entity_is_on_when_tomorrow_loaded() -> None:
    entry = _entry()
    coordinator = _coord(_today_and_tomorrow_data(24, 24), entry)
    sensor = TomorrowPricesAvailable(coordinator)  # type: ignore[arg-type]
    assert sensor.is_on is True
    assert sensor.unique_id == f"{entry.entry_id}_tomorrow_prices_available"
    # Device info groups the binary sensor with the rest of the entry's entities.
    assert sensor.device_info is not None
    assert (DOMAIN, entry.entry_id) in sensor.device_info["identifiers"]


def test_entity_is_off_when_only_today_loaded() -> None:
    entry = _entry()
    coordinator = _coord(_today_and_tomorrow_data(24, 0), entry)
    sensor = TomorrowPricesAvailable(coordinator)  # type: ignore[arg-type]
    assert sensor.is_on is False


def test_entity_is_off_when_no_data() -> None:
    entry = _entry()
    coordinator = _coord(CoordinatorData(), entry)
    sensor = TomorrowPricesAvailable(coordinator)  # type: ignore[arg-type]
    assert sensor.is_on is False


def test_entity_uses_supplier_label_for_manufacturer() -> None:
    entry = _entry()
    coordinator = _coord(CoordinatorData(), entry)
    sensor = TomorrowPricesAvailable(coordinator)  # type: ignore[arg-type]
    # Eneco extractor's label is "Eneco" — falls back to the supplier id
    # only when the extractor lookup raises (unknown supplier).
    assert sensor.device_info is not None
    assert sensor.device_info["manufacturer"] == "Eneco"
