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

"""Tests for the contract end date renewal-reminder sensor."""

from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import CoordinatorData
from custom_components.be_electricity_prices.sensor import (
    ContractEndDateSensor,
    async_setup_entry,
)
from tests import make_entry


def _entry(end_date: str | None = "2027-11-14") -> MockConfigEntry:
    if end_date is None:
        return make_entry()
    return make_entry(contract_end_date=end_date)


def _coord(entry: MockConfigEntry) -> SimpleNamespace:
    return SimpleNamespace(data=CoordinatorData(), entry=entry)


def test_native_value_is_tz_aware_local_midnight() -> None:
    entry = _entry()
    sensor = ContractEndDateSensor(_coord(entry), date(2027, 11, 14))  # type: ignore[arg-type]
    value = sensor.native_value
    assert isinstance(value, datetime)
    # TIMESTAMP rejects a naive datetime.
    assert value.tzinfo is not None
    local = dt_util.as_local(value)
    assert (local.year, local.month, local.day) == (2027, 11, 14)
    assert (local.hour, local.minute, local.second) == (0, 0, 0)


def test_metadata_matches_conventions() -> None:
    entry = _entry()
    sensor = ContractEndDateSensor(_coord(entry), date(2027, 11, 14))  # type: ignore[arg-type]
    assert sensor.device_class == SensorDeviceClass.TIMESTAMP
    assert sensor.translation_key == "contract_end_date"
    assert sensor.unique_id == f"{entry.entry_id}_contract_end_date"
    assert sensor.device_info is not None
    assert (DOMAIN, entry.entry_id) in sensor.device_info["identifiers"]


def test_stays_available_when_a_fetch_fails() -> None:
    """The end date is a static config value, so a failed supplier fetch
    (last_update_success False) must not hide the reminder sensor."""
    entry = _entry()
    coord = _coord(entry)
    coord.last_update_success = False
    sensor = ContractEndDateSensor(coord, date(2027, 11, 14))  # type: ignore[arg-type]
    assert sensor.available is True


async def test_setup_registers_reminder_only_when_end_date_set() -> None:
    # With an end date configured, the reminder sensor is added.
    entry = _entry()
    entry.runtime_data = _coord(entry)
    added: list[Any] = []
    await async_setup_entry(
        None,  # type: ignore[arg-type]
        entry,
        lambda entities: added.extend(entities),  # type: ignore[arg-type]
    )
    assert sum(isinstance(e, ContractEndDateSensor) for e in added) == 1

    # Without one, no reminder sensor appears.
    bare = _entry(end_date=None)
    bare.runtime_data = _coord(bare)
    added_bare: list[Any] = []
    await async_setup_entry(
        None,  # type: ignore[arg-type]
        bare,
        lambda entities: added_bare.extend(entities),  # type: ignore[arg-type]
    )
    assert not any(isinstance(e, ContractEndDateSensor) for e in added_bare)
