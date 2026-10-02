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
from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import creg_ev
from .const import (
    ENERGY_CHARTS_ATTRIBUTION,
    CONF_CONTRACT_END_DATE,
    CONF_DAILY_COMPARE,
    CONF_DSO_TARIFF_MODE,
    CONF_EV_HOME_CHARGING_RATE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DSO_MODE_IMPACT,
    METER_BI,
    METER_DYNAMIC,
    REGION_FLANDERS,
    REGION_WALLONIA,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SUPPLIER_CUSTOM,
    DEFAULT_DAILY_COMPARE,
    DEFAULT_EV_HOME_CHARGING_RATE,
)
from .cohort import (
    _parse_iso_date,
)
from .coordinator import (
    BePricesCoordinator,
    supplier_device_info,
)
from .coordinator_data import CoordinatorData
from .flow_contracts import _ranking_candidates
from .sensor_values import (
    _current_field,
    _current_injection,
    _next_hour,
    _split_injection_today_tomorrow,
    _split_today_tomorrow,
    _today_avg,
    _today_max,
    _today_min,
    _today_ranked,
    _tomorrow_avg,
    _tomorrow_max,
    _tomorrow_min,
)


@dataclass(frozen=True, kw_only=True)
class BePriceSensorDescription(SensorEntityDescription):
    """Sensor description with a pure value extractor."""

    value_fn: Callable[[CoordinatorData], float | None]
    # Takes the entry: current_year_cost's reset instant is per-entry now,
    # since an entry can bill from its contract start date instead of 1
    # January.
    last_reset_fn: Callable[[CoordinatorData], datetime | None] | None = None
    # A None from value_fn reads as unavailable rather than unknown. For the
    # band sensors, which exist for a constant the entry may not have: a
    # monthly-indexed card has one only once the month's mean is known, and
    # a bi-hourly meter on the Walloon Impact tariff has none at all.
    unavailable_when_none: bool = False


def _eur_per_kwh(
    key: str,
    value_fn: Callable[[CoordinatorData], float | None],
    *,
    unavailable_when_none: bool = False,
) -> BePriceSensorDescription:
    """Build a EUR/kWh measurement description with the standard precision."""
    return BePriceSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=value_fn,
        unavailable_when_none=unavailable_when_none,
    )


SENSORS: tuple[BePriceSensorDescription, ...] = (
    _eur_per_kwh("current_price", _current_field("all_in")),
    _eur_per_kwh(
        "next_hour_price",
        lambda d: None if (bd := _next_hour(d)) is None else bd.all_in,
    ),
    _eur_per_kwh("today_average", _today_avg),
    _eur_per_kwh("today_min", _today_min),
    _eur_per_kwh("today_max", _today_max),
    _eur_per_kwh("tomorrow_average", _tomorrow_avg),
    _eur_per_kwh("tomorrow_min", _tomorrow_min),
    _eur_per_kwh("tomorrow_max", _tomorrow_max),
    _eur_per_kwh("energy_component", _current_field("energy")),
    _eur_per_kwh("network_component", _current_field("network")),
    _eur_per_kwh("taxes_component", _current_field("taxes")),
)

# Static peak/offpeak prices for the Energy Dashboard. These do NOT vary with
# the time of day - they represent the constant all-in rate for that tariff
# band. Useful for bi-hourly meter configurations where the Energy Dashboard
# needs separate price entities for tariff 1 (day) and tariff 2 (night).
# Returns None for dynamic/TOU contracts or Wallonia impact tariff.
BI_HOURLY_SENSORS: tuple[BePriceSensorDescription, ...] = (
    _eur_per_kwh(
        "price_peak",
        lambda d: None if d.static_peak_price is None else d.static_peak_price.all_in,
        unavailable_when_none=True,
    ),
    _eur_per_kwh(
        "price_offpeak",
        lambda d: (
            None if d.static_offpeak_price is None else d.static_offpeak_price.all_in
        ),
        unavailable_when_none=True,
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
    _eur_per_kwh("injection_price", _current_injection),
)

# The feed-in rate of each register of a two-register meter. A card that
# prints one per register (Trevion Vast) gives each its own constant. Every
# other card credits both registers on the one formula or rate, so both carry
# the credit of the current slot, the injection_price one: that is what the
# register counting now is paid, and the Energy dashboard prices each return
# register off its own sensor. They sat unavailable on those cards before.
BI_HOURLY_INJECTION_SENSORS: tuple[BePriceSensorDescription, ...] = (
    _eur_per_kwh(
        "injection_price_peak",
        lambda d: (
            _current_injection(d)
            if d.static_injection_peak is None
            else d.static_injection_peak
        ),
        unavailable_when_none=True,
    ),
    _eur_per_kwh(
        "injection_price_offpeak",
        lambda d: (
            _current_injection(d)
            if d.static_injection_offpeak is None
            else d.static_injection_offpeak
        ),
        unavailable_when_none=True,
    ),
)

EV_RATE_SENSORS: tuple[BePriceSensorDescription, ...] = (
    # Unavailable rather than unknown until the CREG's file has been read.
    _eur_per_kwh(
        "ev_home_charging_rate",
        lambda d: d.ev_home_charging_rate_eur_per_kwh,
        unavailable_when_none=True,
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
        # Flemish Energiefonds: supplier-collected residential charge
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
        # annual fees, with injection netted per regime. Missing meter
        # inputs collapse to the fees-only floor rather than ``unknown``.
        # Unknown while setup's held figure does not apply (a new entry, a
        # year turned over across the restart, settings edited since) until
        # the background meter read lands, and while a recorded supplier
        # switch's earlier contracts are not all priced. ``TOTAL`` with ``last_reset``
        # pinned to local midnight of the window start: Jan 1, or the
        # contract start date on an entry that bills from it: lets the
        # long-term-statistics engine
        # bucket each calendar year as its own period; the value can
        # dip day-over-day on heavy-injection days under the
        # compensation regime, which rules out ``TOTAL_INCREASING``.
        # ``MONETARY`` device class lets HA's Energy dashboard auto-
        # suggest this entity in the "Cost" picker rather than the
        # user having to type it as a manual price/cost entity.
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.current_year_cost_eur,
        last_reset_fn=lambda d: d.current_year_cost_reset,
    ),
    BePriceSensorDescription(
        key="current_month_cost",
        translation_key="current_month_cost",
        # The same bill as current_year_cost over the running month, which is
        # the period a household budgets in and the one an invoice covers.
        # Priced as its own window rather than sliced off the year, so under
        # the compensation regime it nets THAT month's registers and twelve of
        # these do not add up to the yearly figure; on every other regime they
        # do. ``TOTAL`` with ``last_reset`` on the 1st for the same reason the
        # yearly one carries it, and MEASUREMENT would be wrong twice over:
        # this is money accumulating over a period, not a reading.
        device_class=SensorDeviceClass.MONETARY,
        state_class=SensorStateClass.TOTAL,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.current_month_cost_eur,
        last_reset_fn=lambda d: d.current_month_cost_reset,
    ),
    BePriceSensorDescription(
        key="projected_year_cost",
        translation_key="projected_year_cost",
        # Roughly what a year on this contract costs, priced in one pass at
        # today's tariffs against the entry's own metered volume rather than
        # as a running bill plus a remainder. No device class on purpose.
        # ``MONETARY``
        # admits only ``TOTAL``, which compiles a cumulative sum from
        # state deltas, and this figure is revised both up and down as
        # the year goes on, so that sum would record the drift of the
        # projection rather than money. Plain MEASUREMENT with the EUR
        # unit is the honest fit, the same call ``capacity_cost`` makes.
        # The cost of it is that the Energy dashboard will not auto-
        # suggest this entity, which is correct for an estimate.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.projected_year_cost_eur,
    ),
    BePriceSensorDescription(
        key="projected_year_end_cost",
        translation_key="projected_year_end_cost",
        # The calendar year's bill as it will stand on 31 December. No device
        # class for the reason projected_year_cost carries none: it is revised
        # both ways as the year runs.
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR",
        suggested_display_precision=2,
        value_fn=lambda d: d.year_end_cost_eur,
    ),
)


# What this calendar year will have metered by 31 December. No device class
# for the reason projected_year_cost carries none: ``ENERGY`` admits only
# ``TOTAL`` and ``TOTAL_INCREASING``, and a projection revised both ways is
# neither.
_PROJECTED_CONSUMPTION = BePriceSensorDescription(
    key="projected_year_consumption",
    translation_key="projected_year_consumption",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="kWh",
    suggested_display_precision=0,
    value_fn=lambda d: d.projected_year_consumption_kwh,
)
_PROJECTED_INJECTION = BePriceSensorDescription(
    key="projected_year_injection",
    translation_key="projected_year_injection",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="kWh",
    suggested_display_precision=0,
    value_fn=lambda d: d.projected_year_injection_kwh,
)
# What the meter recorded over the last 365 days, the volume projected_year_cost
# prices. MEASUREMENT for the same reason: a window sum falls as well as rises.
_ROLLING_CONSUMPTION = BePriceSensorDescription(
    key="rolling_year_consumption",
    translation_key="rolling_year_consumption",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="kWh",
    suggested_display_precision=0,
    value_fn=lambda d: d.rolling_year_consumption_kwh,
)
_ROLLING_INJECTION = BePriceSensorDescription(
    key="rolling_year_injection",
    translation_key="rolling_year_injection",
    state_class=SensorStateClass.MEASUREMENT,
    native_unit_of_measurement="kWh",
    suggested_display_precision=0,
    value_fn=lambda d: d.rolling_year_injection_kwh,
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
        # MEASUREMENT is the only state class HA's sensor base class
        # accepts under the POWER device class
        # (DEVICE_CLASS_STATE_CLASSES[POWER] == {MEASUREMENT}); TOTAL
        # would log a "state class is impossible considering device
        # class" warning on every entity setup. The Energy /
        # statistics graph defaults to the mean aggregation, which is
        # not what the user wants here: ask HA's developer-tools
        # statistics view for the per-hour MAX instead, which tracks
        # the true monthly running peak.
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
    descriptions.extend((_PROJECTED_CONSUMPTION, _ROLLING_CONSUMPTION))
    # Only for a household that asked for it: a company car charged at home.
    if entry.data.get(CONF_EV_HOME_CHARGING_RATE, DEFAULT_EV_HOME_CHARGING_RATE):
        descriptions.extend(EV_RATE_SENSORS)
    # Only where the two bands are a thing the household is billed on. On a
    # single-rate or dynamic meter these have no constant to report and would
    # sit unavailable for good, which is two dead entities per entry. The
    # same on the Walloon Impact tariff, whose distribution follows the
    # CWaPE bands every card prints, except a custom entry left without them.
    impact = (
        entry.data.get(CONF_DSO_TARIFF_MODE) == DSO_MODE_IMPACT
        and entry.data.get(CONF_SUPPLIER) != SUPPLIER_CUSTOM
    )
    if entry.data.get(CONF_METER) == METER_BI and not impact:
        descriptions.extend(BI_HOURLY_SENSORS)
    if entry.data.get(CONF_REGION) == REGION_FLANDERS:
        descriptions.extend(CAPACITY_SENSORS)
    try:
        solar_kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        solar_kva = 0.0
    regime = entry.data.get(CONF_SOLAR_REGIME)
    # Wallonia alone bills the prosumer tariff (fees._walloon_compensation_kva),
    # and an entry saved with compensation elsewhere before the regime was
    # restricted to Wallonia read 0 for good.
    if (
        solar_kva > 0.0
        and regime == SOLAR_REGIME_COMPENSATION
        and entry.data.get(CONF_REGION) == REGION_WALLONIA
    ):
        descriptions.extend(PROSUMER_SENSORS)
    if regime in (SOLAR_REGIME_COMPENSATION, SOLAR_REGIME_INJECTION):
        descriptions.extend((_PROJECTED_INJECTION, _ROLLING_INJECTION))
    if regime == SOLAR_REGIME_INJECTION:
        descriptions.extend(INJECTION_SENSORS)
        # The engine credits a register pair on both two-register meters, the
        # bi-hourly and the digital one, so the band sensors follow it.
        if entry.data.get(CONF_METER) in (METER_BI, METER_DYNAMIC):
            descriptions.extend(BI_HOURLY_INJECTION_SENSORS)

    entities: list[SensorEntity] = [
        BePriceSensor(coordinator, desc) for desc in descriptions
    ]
    end_date = _parse_iso_date(entry.data.get(CONF_CONTRACT_END_DATE))
    if end_date is not None:
        entities.append(ContractEndDateSensor(coordinator, end_date))
    # Not where the ranking has nothing to rank, such as Engie Empower
    # Flextime in Brussels: the nightly run skips the entry and the sensor
    # would read unknown for good.
    if entry.data.get(CONF_DAILY_COMPARE, DEFAULT_DAILY_COMPARE) and not isinstance(
        _ranking_candidates(entry.data), str
    ):
        entities.append(PotentialSavingSensor(coordinator))
    coordinator.intended_unique_ids["sensor"] = {
        e.unique_id for e in entities if e.unique_id
    }
    async_add_entities(entities)


class BePriceSensor(CoordinatorEntity[BePricesCoordinator], SensorEntity):
    """A single all-in electricity price sensor."""

    _attr_has_entity_name = True
    # The current_price sensor carries the full today / tomorrow price
    # arrays and the ranked-window lists, which change every hour. Keep
    # them out of the recorder (HA stores state attributes by default) so
    # they don't bloat the long-term database; they are live display
    # helpers, not history.
    #
    # This is also what lets today / tomorrow carry a quarter-hourly
    # contract's own 96 rows per day. HA's only attribute size cap,
    # MAX_STATE_ATTRS_BYTES (16 KB), is applied by the recorder AFTER it
    # drops the keys named here (db_schema.shared_attrs_bytes_from_event
    # builds exclude_attrs, filters, and only then measures), so an excluded
    # attribute is never weighed against it. Anything ADDED here that is not
    # excluded has to fit: the recorded remainder currently runs about 6 KB.
    # snapshot_age_hours rises ~1/hour and last_error is diagnostic, so
    # recording them would write a fresh states row every tick even for a flat
    # contract whose price never moves; keep them out of history too.
    # The current_year_cost diagnostic breakdown (YTD/today kWh, raw energy,
    # fees, the components, the credit, the coverage counts) climbs every tick
    # as well, so keep it out of the recorder too; only the billed peak stays,
    # because the capacity sensor publishes it as history and it moves only
    # when a peak does.
    _unrecorded_attributes = frozenset(
        {
            "today",
            "tomorrow",
            "cheapest_4h_today",
            "most_expensive_4h_today",
            "history",
            "snapshot_age_hours",
            "last_error",
            "card_read_by_ocr",
            "consumption_ytd_kwh",
            "injection_ytd_kwh",
            "consumption_today_kwh",
            "injection_today_kwh",
            "energy_ytd_raw_eur",
            "fees_ytd_eur",
            "hours_seen",
            "hours_priced",
            "hours_elapsed",
            "injection_hours_uncredited",
            "days_seen",
            "days_priced",
            "days_elapsed",
            "energy_component_ytd_eur",
            "green_component_ytd_eur",
            "credit_energy_component_eur",
            "credit_consumption_kwh",
            "capacity_ytd_eur",
            "prosumer_ytd_eur",
            "net_network_ytd_eur",
            "gross_network_ytd_eur",
            "network_cap_rebate_eur",
            "standing_charges_ytd_eur",
            "welcome_credit_eur",
            "previous_contracts_eur",
            "previous_contracts",
            "energy_basis",
            "fee_basis",
            "volume_basis",
            "injection_basis",
            "annual_kwh",
            "annual_injection_kwh",
            "contract_basis",
            "ytd_kwh",
            "remaining_kwh",
            "consumption_kwh",
            "injection_kwh",
            "fees_eur",
        }
    )
    entity_description: BePriceSensorDescription

    def __init__(
        self,
        coordinator: BePricesCoordinator,
        description: BePriceSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = supplier_device_info(coordinator)

    @property
    def last_reset(self) -> datetime | None:
        # The window the published figure was computed over, baked with it at
        # the tick, so the two can never name different periods.
        fn = self.entity_description.last_reset_fn
        data = self.coordinator.data
        return fn(data) if fn is not None and data is not None else None

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        if not self.entity_description.unavailable_when_none:
            return True
        data = self.coordinator.data
        return data is not None and self.entity_description.value_fn(data) is not None

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
        data = self.coordinator.data
        if self.entity_description.key == "current_price":
            cheapest, most_expensive = _today_ranked(data, 4)
            today, tomorrow = _split_today_tomorrow(data)
            return {
                "snapshot_publication": data.snapshot_publication,
                # Only for an entry that carries a contract start date, and
                # then always: a cohort entry whose archive came up empty
                # bills the current card, and saying so is the whole point.
                **({"signing_card": data.signing_card} if data.signing_card else {}),
                "snapshot_age_hours": round(data.snapshot_age_hours, 2),
                "snapshot_stale": data.snapshot_stale,
                "last_error": data.last_error,
                # Only while it is true, like the attribution below: a price
                # read off a picture of the card has to say so wherever it is
                # shown, and every other entry should carry nothing extra.
                **({"card_read_by_ocr": True} if data.card_read_by_ocr else {}),
                "spot_source": data.spot_source,
                # CC BY 4.0 obliges us to credit the fallback's source
                # wherever its data is shown, so the credit rides with the
                # prices and appears only while the fallback is in use.
                **(
                    {"attribution": ENERGY_CHARTS_ATTRIBUTION}
                    if data.spot_source == "energy-charts"
                    else {}
                ),
                "cheapest_4h_today": cheapest,
                "most_expensive_4h_today": most_expensive,
                "today": today,
                "tomorrow": tomorrow,
            }
        if self.entity_description.key == "injection_price":
            # Empty only off the injection regime or on a card with no feed-in
            # price; a flat feed-in repeats its one figure, as the price
            # arrays do for a fixed card.
            today, tomorrow = _split_injection_today_tomorrow(data)
            if not today and not tomorrow:
                return {}
            return {"today": today, "tomorrow": tomorrow}
        if self.entity_description.key == "ev_home_charging_rate":
            # The quarter comes from the tick that read the rate, never from
            # the clock, for the reason CoordinatorData gives. The history lets
            # last quarter's kWh be settled at last quarter's rate after the
            # state has moved on. Unrecorded: it changes once a quarter and
            # would otherwise be written every tick.
            region = self.coordinator.entry.data.get(CONF_REGION, "")
            quarter = data.ev_home_charging_quarter_start
            return {
                "quarter_start": quarter.isoformat() if quarter else None,
                "region": region,
                "source": creg_ev.SOURCE_URL,
                "history": [
                    {"quarter_start": start.isoformat(), "rate": rate}
                    for start, rate in creg_ev.history(region)
                ],
            }
        if self.entity_description.key == "capacity_cost":
            # The cost is charged on the twelve-month mean, not on this month's
            # reading, so without these the number looks disconnected from the
            # monthly_peak_kw sensor sitting next to it. months_counted says how
            # far the window has filled: it reaches 12 after a full year, and
            # until then the mean covers only what has been measured.
            return {
                "billed_peak_kw": round(data.capacity_billed_peak_kw, 3),
                "months_counted": data.capacity_peak_months,
            }
        if self.entity_description.key == "current_year_cost":
            # Diagnostic breakdown: lets a flat or low sensor be told apart.
            # A negative energy_ytd_raw_eur (every static entry, and an hourly
            # one on the compensation regime) means the compensation zero-floor
            # is hiding banked injection (working as designed), and a
            # consumption_today_kwh that never grows points at a stalled meter
            # input. hours_priced below hours_seen (days_priced below
            # days_seen per day) means part of what the meter recorded is not
            # fully on the bill: an hour the spot cache could not price bills
            # without its energy term, and a month whose card lacks the
            # entry's DSO row, or has no flat rate for the per-day walk, is
            # left out whole with a warning in the log. And
            # injection_hours_uncredited counts the exported hours whose
            # feed-in credit follows the spot and had none to follow.
            diag = data.ytd_diagnostics
            attrs: dict[str, Any] = (
                {k: round(v, 4) for k, v in diag.items()} if diag else {}
            )
            # The contracts held earlier in the year, each with its own days
            # and what it cost, when the household recorded a switch. The
            # figure above already includes them (previous_contracts_eur).
            if data.previous_contracts:
                attrs["previous_contracts"] = [
                    dict(row) for row in data.previous_contracts
                ]
            return attrs
        key = self.entity_description.key
        if key == "projected_year_cost":
            proj = data.projection_diagnostics
        elif key == "projected_year_end_cost":
            proj = data.year_end_diagnostics
        elif key in ("projected_year_consumption", "projected_year_injection"):
            side = key.removeprefix("projected_year_")
            proj = (data.volume_projection_diagnostics or {}).get(side)
        elif key in ("rolling_year_consumption", "rolling_year_injection"):
            side = key.removeprefix("rolling_year_")
            proj = (data.rolling_volume_diagnostics or {}).get(side)
        else:
            return {}
        # Its own branch rather than sharing the one above: these attributes
        # are a mix of strings and floats, and round() raises TypeError on a
        # string. The strings are the point of them, since a projection is only
        # as trustworthy as the basis it names.
        if not proj:
            return {}
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in proj.items()}


class ContractEndDateSensor(CoordinatorEntity[BePricesCoordinator], SensorEntity):
    """Timestamp of the configured contract end date.

    A standalone entity: it can't reuse ``BePriceSensor`` because that
    class's ``value_fn`` is typed float-only and ``native_value`` rounds
    it. The value is a static config value, and its main use is letting an
    automation fire a renewal reminder ahead of the end date. It changes no
    billed rate, but it is no longer inert: ``projected_year_cost`` reads the
    same date to say how much of the projected year today's contract actually
    covers. Created only when an end date is configured.
    """

    _attr_has_entity_name = True
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "contract_end_date"

    def __init__(self, coordinator: BePricesCoordinator, end_date: date) -> None:
        super().__init__(coordinator)
        self._end_date = end_date
        self._attr_unique_id = f"{coordinator.entry.entry_id}_contract_end_date"
        self._attr_device_info = supplier_device_info(coordinator)

    @property
    def available(self) -> bool:
        # A static config value, not fetched data, so it stays available
        # even when a supplier fetch fails; the default
        # CoordinatorEntity.available would hide it on the first failure.
        return True

    @property
    def native_value(self) -> datetime:
        # TIMESTAMP requires a tz-aware datetime; anchor the date at local
        # (Europe/Brussels) midnight.
        return dt_util.start_of_local_day(self._end_date)


class PotentialSavingSensor(CoordinatorEntity[BePricesCoordinator], SensorEntity):
    """Yearly euro the cheapest alternative contract would save.

    Created only for an entry that opted into the daily ranking. The state is
    one number because a sensor state is capped at 255 characters and because
    one number is what an automation can act on; the ranking behind it rides
    in the attributes.

    Negative is a real reading, not an error: it means nothing on the market
    beats what this household already has, which is the answer somebody
    watching a comparison sensor most wants to be given. Unknown means the
    sweep has not run yet, or ran and could not price the household's own
    contract, in which case there is no baseline to subtract from and
    reporting zero would read as "no saving available".
    """

    _attr_has_entity_name = True
    _attr_translation_key = "potential_saving"
    # The ranking is a live table, not history: it is replaced wholesale by
    # each nightly run, and a snapshot of every row stored daily forever
    # answers a question nobody asks of history. Excluding it also takes it
    # out of the 16 KB attribute cap, which the recorder applies to what is
    # left AFTER the exclusions, so the table can carry a figure per row
    # without the small keys beside it losing their history to an over-cap
    # state (which stores none of its attributes at all).
    _unrecorded_attributes = frozenset({"ranking"})
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "EUR"
    # No state_class. MONETARY with a measurement class asks the recorder for
    # long-term statistics on a figure that is a standing comparison rather
    # than something metered, and a mean of it over a month means nothing.
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator: BePricesCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_potential_saving"
        self._attr_device_info = supplier_device_info(coordinator)

    @property
    def available(self) -> bool:
        # Deliberately not the CoordinatorEntity default. This value comes
        # from the daily sweep, not from the hourly price fetch, so a supplier
        # fetch that failed this hour says nothing about whether last night's
        # ranking is still worth showing.
        return True

    @property
    def native_value(self) -> float | None:
        result = self.coordinator.daily_compare
        return None if result is None else result.saving

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The ranking behind the number.

        Rows are flattened to plain values rather than rendered, so the
        dialog and an automation read the same figures.

        ``ytd_eur`` is what each contract would have cost this household since
        1 January, on the same real archived months its own side replayed, and
        it is absent from a row that could not answer that honestly rather
        than carrying a figure computed against a different set of months. It
        used to exist only on the options page while that page was open; the
        nightly sweep fills it now, and ``ranking`` is unrecorded so carrying
        it costs no database.
        """
        result = self.coordinator.daily_compare
        if result is None:
            return {}
        best = result.cheapest
        return {
            "own_annual_eur": result.own,
            "cheapest": best.label if best is not None else None,
            "cheapest_annual_eur": best.annual if best is not None else None,
            "priced": result.priced,
            "total": result.total,
            "last_run": result.ran_at.isoformat(),
            "ranking": [
                {
                    "label": row.label,
                    "annual_eur": row.annual,
                    "is_own": row.is_own,
                    **({"ytd_eur": row.ytd} if row.ytd is not None else {}),
                    **({"status": row.status} if row.status else {}),
                    **({"feed_in_uncredited": True} if row.feed_in_uncredited else {}),
                }
                for row in sorted(
                    result.rows,
                    key=lambda r: (r.annual is None, r.annual or 0.0),
                )
            ],
        }
