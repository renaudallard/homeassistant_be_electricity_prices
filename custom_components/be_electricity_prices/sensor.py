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

"""Sensor platform for the Belgian Electricity Prices integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .coordinator import BePricesCoordinator, CoordinatorData
from .pricing import PriceBreakdown
from .providers import get as get_extractor
from .providers.base import ExtractorError


@dataclass(frozen=True, kw_only=True)
class BePriceSensorDescription(SensorEntityDescription):
    """Sensor description with a pure value extractor."""

    value_fn: Callable[[CoordinatorData], float | None]
    last_reset_fn: Callable[[], datetime] | None = None


def _current(data: CoordinatorData) -> PriceBreakdown | None:
    if not data.hourly:
        return None
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    if (exact := data.hourly.get(now)) is not None:
        return exact
    nearest_hour = min(
        data.hourly.keys(),
        key=lambda h: abs((h - now).total_seconds()),
    )
    # Bound the nearest-hour fallback so a stale spot cache doesn't
    # silently surface yesterday's last hour as "now". An hour off is
    # tolerated for DST seams; anything beyond that means the price
    # table is stale relative to wall-clock and the sensor should go
    # unknown rather than mislead.
    if abs((nearest_hour - now).total_seconds()) > 3600:
        return None
    return data.hourly[nearest_hour]


def _next_hour(data: CoordinatorData) -> PriceBreakdown | None:
    if not data.hourly:
        return None
    target = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    return data.hourly.get(target)


def _today_hours(data: CoordinatorData) -> list[PriceBreakdown]:
    today = dt_util.now().date()
    return [
        bd for hour, bd in data.hourly.items() if dt_util.as_local(hour).date() == today
    ]


def _today_avg(data: CoordinatorData) -> float | None:
    hours = _today_hours(data)
    if not hours:
        return None
    return sum(h.all_in for h in hours) / len(hours)


def _today_min(data: CoordinatorData) -> float | None:
    hours = _today_hours(data)
    if not hours:
        return None
    return min(h.all_in for h in hours)


def _today_max(data: CoordinatorData) -> float | None:
    hours = _today_hours(data)
    if not hours:
        return None
    return max(h.all_in for h in hours)


def _tomorrow_hours(data: CoordinatorData) -> list[PriceBreakdown]:
    tomorrow = dt_util.now().date() + timedelta(days=1)
    return [
        bd
        for hour, bd in data.hourly.items()
        if dt_util.as_local(hour).date() == tomorrow
    ]


def _tomorrow_avg(data: CoordinatorData) -> float | None:
    hours = _tomorrow_hours(data)
    if not hours:
        return None
    return sum(h.all_in for h in hours) / len(hours)


def _tomorrow_min(data: CoordinatorData) -> float | None:
    hours = _tomorrow_hours(data)
    if not hours:
        return None
    return min(h.all_in for h in hours)


def _tomorrow_max(data: CoordinatorData) -> float | None:
    hours = _tomorrow_hours(data)
    if not hours:
        return None
    return max(h.all_in for h in hours)


def _today_ranked(
    data: CoordinatorData, count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the ``count`` cheapest and ``count`` most-expensive today-hours.

    The two lists are always disjoint: when fewer than ``2 * count`` today
    hours are populated (e.g. right after midnight on a static contract),
    the cheapest take their share first and the most-expensive list gets
    only what remains. Each list is returned in chronological order.
    """
    today = dt_util.now().date()
    pairs = [
        (h, bd) for h, bd in data.hourly.items() if dt_util.as_local(h).date() == today
    ]
    if not pairs:
        return [], []
    by_price_asc = sorted(pairs, key=lambda x: x[1].all_in)
    cheapest_pairs = by_price_asc[:count]
    remaining = by_price_asc[count:]
    most_expensive_pairs = remaining[-count:] if remaining else []
    cheapest = sorted(cheapest_pairs, key=lambda x: x[0])
    most_expensive = sorted(most_expensive_pairs, key=lambda x: x[0])

    def _fmt(h: Any, bd: PriceBreakdown) -> dict[str, Any]:
        return {
            "start": dt_util.as_local(h).isoformat(),
            "price": round(bd.all_in, 6),
        }

    return (
        [_fmt(h, bd) for h, bd in cheapest],
        [_fmt(h, bd) for h, bd in most_expensive],
    )


def _split_today_tomorrow(
    data: CoordinatorData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group the cached hourly breakdowns into today and tomorrow buckets.

    Both lists are returned in chronological order. Hours outside the
    today/tomorrow window (typically there are none) are dropped.
    """
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    today_rows: list[dict[str, Any]] = []
    tomorrow_rows: list[dict[str, Any]] = []
    for h, bd in sorted(data.hourly.items()):
        local = dt_util.as_local(h)
        row = {
            "start": local.isoformat(),
            "energy": round(bd.energy, 6),
            "network": round(bd.network, 6),
            "taxes": round(bd.taxes, 6),
            "all_in": round(bd.all_in, 6),
        }
        if local.date() == today:
            today_rows.append(row)
        elif local.date() == tomorrow:
            tomorrow_rows.append(row)
    return today_rows, tomorrow_rows


def _current_field(field: str) -> Callable[[CoordinatorData], float | None]:
    def _inner(data: CoordinatorData) -> float | None:
        bd = _current(data)
        return None if bd is None else getattr(bd, field)

    return _inner


SENSORS: tuple[BePriceSensorDescription, ...] = (
    BePriceSensorDescription(
        key="current_price",
        translation_key="current_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_current_field("all_in"),
    ),
    BePriceSensorDescription(
        key="next_hour_price",
        translation_key="next_hour_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=lambda d: None if (bd := _next_hour(d)) is None else bd.all_in,
    ),
    BePriceSensorDescription(
        key="today_average",
        translation_key="today_average",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_today_avg,
    ),
    BePriceSensorDescription(
        key="today_min",
        translation_key="today_min",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_today_min,
    ),
    BePriceSensorDescription(
        key="today_max",
        translation_key="today_max",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_today_max,
    ),
    BePriceSensorDescription(
        key="tomorrow_average",
        translation_key="tomorrow_average",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_tomorrow_avg,
    ),
    BePriceSensorDescription(
        key="tomorrow_min",
        translation_key="tomorrow_min",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_tomorrow_min,
    ),
    BePriceSensorDescription(
        key="tomorrow_max",
        translation_key="tomorrow_max",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_tomorrow_max,
    ),
    BePriceSensorDescription(
        key="energy_component",
        translation_key="energy_component",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_current_field("energy"),
    ),
    BePriceSensorDescription(
        key="network_component",
        translation_key="network_component",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_current_field("network"),
    ),
    BePriceSensorDescription(
        key="taxes_component",
        translation_key="taxes_component",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=_current_field("taxes"),
    ),
)

PROSUMER_SENSORS: tuple[BePriceSensorDescription, ...] = (
    BePriceSensorDescription(
        key="prosumer_cost",
        translation_key="prosumer_cost",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.prosumer_cost_eur,
    ),
)

INJECTION_SENSORS: tuple[BePriceSensorDescription, ...] = (
    BePriceSensorDescription(
        key="injection_price",
        translation_key="injection_price",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=lambda d: d.injection_price_eur_per_kwh,
    ),
)

FEE_SENSORS: tuple[BePriceSensorDescription, ...] = (
    BePriceSensorDescription(
        key="fixed_fee_eur_per_year",
        translation_key="fixed_fee_eur_per_year",
        # The supplier's flat annual subscription fee. Plain MEASUREMENT
        # since the user pays it once per year, not metered.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.yearly_fixed_fee_eur,
    ),
    BePriceSensorDescription(
        key="energy_fund_eur_per_month",
        translation_key="energy_fund_eur_per_month",
        # Flemish Energiefonds — supplier-collected residential charge
        # billed per month. Free for domiciliated customers (0,00) and
        # ~10 EUR/month otherwise depending on the supplier's card.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.energy_fund_eur_per_month,
    ),
    BePriceSensorDescription(
        key="current_year_cost",
        translation_key="current_year_cost",
        # Running bill since Jan 1: this-year cons / inj kWh x rates +
        # annual fees, with injection netted per regime. Always numeric;
        # missing meter inputs collapse to the fees-only floor so the
        # sensor never goes ``unknown``. ``TOTAL`` with ``last_reset``
        # pinned to Jan 1 local lets the long-term-statistics engine
        # bucket each calendar year as its own period; the value can
        # dip day-over-day on heavy-injection days under the
        # compensation regime, which rules out ``TOTAL_INCREASING``.
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.current_year_cost_eur,
        last_reset_fn=lambda: dt_util.now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        ),
    ),
)


CAPACITY_SENSORS: tuple[BePriceSensorDescription, ...] = (
    BePriceSensorDescription(
        key="capacity_cost",
        translation_key="capacity_cost",
        # MONETARY device class would require state_class=TOTAL with a
        # last_reset attribute on the monthly boundary; we are showing a
        # rolling instant estimate ("if the month ended now") so plain
        # MEASUREMENT with the EUR unit is the honest fit.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.capacity_cost_eur,
    ),
    BePriceSensorDescription(
        key="monthly_peak_kw",
        translation_key="monthly_peak_kw",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kW",
        suggested_display_precision=2,
        value_fn=lambda d: d.monthly_peak_kw,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create entities for one config entry."""
    coordinator: BePricesCoordinator = entry.runtime_data

    descriptions: list[BePriceSensorDescription] = list(SENSORS)
    descriptions.extend(FEE_SENSORS)
    if entry.data.get(CONF_REGION) == REGION_FLANDERS:
        descriptions.extend(CAPACITY_SENSORS)
    try:
        solar_kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        solar_kva = 0.0
    regime = entry.data.get(CONF_SOLAR_REGIME)
    if solar_kva > 0.0 and regime == SOLAR_REGIME_COMPENSATION:
        descriptions.extend(PROSUMER_SENSORS)
    if regime == SOLAR_REGIME_INJECTION:
        descriptions.extend(INJECTION_SENSORS)

    async_add_entities(BePriceSensor(coordinator, desc) for desc in descriptions)


class BePriceSensor(CoordinatorEntity[BePricesCoordinator], SensorEntity):
    """A single all-in electricity price sensor."""

    _attr_has_entity_name = True
    entity_description: BePriceSensorDescription

    def __init__(
        self,
        coordinator: BePricesCoordinator,
        description: BePriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        supplier_id = coordinator.entry.data.get(CONF_SUPPLIER, "")
        try:
            supplier_label = get_extractor(str(supplier_id)).label
        except ExtractorError:
            supplier_label = str(supplier_id) or "Belgian Electricity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry.entry_id)},
            name=coordinator.entry.title,
            manufacturer=supplier_label,
            entry_type=None,
        )

    @property
    def last_reset(self) -> datetime | None:
        fn = self.entity_description.last_reset_fn
        return fn() if fn is not None else None

    @property
    def native_value(self) -> float | None:
        # Float arithmetic in compute_breakdown / cost helpers leaks
        # binary-representation noise (e.g. 0.353221 ends up stored as
        # 0.35322099999999995). suggested_display_precision only affects
        # the displayed string; the recorder writes native_value as-is,
        # so the long-tail value shows up on the history chart and in
        # the statistics. Round here to two decimals beyond what the
        # UI displays so we kill the noise without losing precision.
        value = self.entity_description.value_fn(self.coordinator.data)
        if value is None:
            return None
        precision = self.entity_description.suggested_display_precision
        return round(value, (precision + 2) if precision is not None else 6)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.entity_description.key != "current_price":
            return {}
        data = self.coordinator.data
        cheapest, most_expensive = _today_ranked(data, 4)
        today, tomorrow = _split_today_tomorrow(data)
        return {
            "snapshot_publication": data.snapshot_publication,
            "snapshot_age_hours": round(data.snapshot_age_hours, 2),
            "snapshot_stale": data.snapshot_stale,
            "last_error": data.last_error,
            "cheapest_4h_today": cheapest,
            "most_expensive_4h_today": most_expensive,
            "today": today,
            "tomorrow": tomorrow,
        }
