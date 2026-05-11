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

"""Button platform for the Belgian Electricity Prices integration."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_REGION, REGION_FLANDERS
from .coordinator import BePricesCoordinator, supplier_device_info

_RESET_PEAK = ButtonEntityDescription(
    key="reset_monthly_peak",
    translation_key="reset_monthly_peak",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create buttons for one config entry.

    The reset-peak button only applies to Flemish entries: outside
    Flanders the capacity tariff is not billed and ``_peak_kw`` is
    forced to 0 every tick by ``_track_monthly_peak`` anyway, so a
    user-facing reset would do nothing.
    """
    if entry.data.get(CONF_REGION) != REGION_FLANDERS:
        return
    coordinator: BePricesCoordinator = entry.runtime_data
    async_add_entities([ResetMonthlyPeakButton(coordinator)])


class ResetMonthlyPeakButton(CoordinatorEntity[BePricesCoordinator], ButtonEntity):
    """Drops the persisted monthly peak so the next tick rebuilds it."""

    _attr_has_entity_name = True
    entity_description = _RESET_PEAK

    def __init__(self, coordinator: BePricesCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_reset_monthly_peak"
        self._attr_device_info = supplier_device_info(coordinator)

    async def async_press(self) -> None:
        await self.coordinator.reset_monthly_peak()
