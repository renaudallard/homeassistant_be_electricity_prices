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

"""Binary sensor platform for the Belgian Electricity Prices integration."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_SUPPLIER, DOMAIN
from .coordinator import BePricesCoordinator, CoordinatorData
from .providers import get as get_extractor
from .providers.base import ExtractorError


def _has_tomorrow(data: CoordinatorData) -> bool:
    """Whether the integration knows tomorrow's *actually-billable* rates.

    Two gates, both required:

      1. The price table has at least one hour with tomorrow's local
         date. For dynamic contracts this only happens after ENTSO-E
         publishes the next-day curve; for fixed/variable/TOU contracts
         the coordinator forward-fills 48 hours so this is always true.
      2. The supplier snapshot's published validity period covers
         tomorrow. For monthly variable cards (Eneco, Mega...) the
         month-end rollover invalidates the previously-extrapolated
         "tomorrow" hours -- the supplier hasn't published the new
         month's rates yet, so we shouldn't claim they're available.
         When the extractor couldn't parse a validity end (None), we
         skip this gate and trust the price table alone.
    """
    if not data.hourly:
        return False
    tomorrow = dt_util.now().date() + timedelta(days=1)
    if data.snapshot_valid_until is not None and tomorrow > data.snapshot_valid_until:
        return False
    return any(dt_util.as_local(h).date() == tomorrow for h in data.hourly)


_DESCRIPTION = BinarySensorEntityDescription(
    key="tomorrow_prices_available",
    translation_key="tomorrow_prices_available",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create binary sensors for one config entry."""
    coordinator: BePricesCoordinator = entry.runtime_data
    async_add_entities([TomorrowPricesAvailable(coordinator)])


class TomorrowPricesAvailable(
    CoordinatorEntity[BePricesCoordinator], BinarySensorEntity
):
    """ON once the price table holds at least one hour with tomorrow's local date."""

    _attr_has_entity_name = True
    entity_description = _DESCRIPTION

    def __init__(self, coordinator: BePricesCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_tomorrow_prices_available"
        try:
            extractor = get_extractor(coordinator.entry.data[CONF_SUPPLIER])
            supplier_label = extractor.label
        except ExtractorError:
            supplier_label = coordinator.entry.data[CONF_SUPPLIER]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title,
            manufacturer=supplier_label,
            entry_type=None,
        )

    @property
    def is_on(self) -> bool:
        return _has_tomorrow(self.coordinator.data)
