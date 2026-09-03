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
from datetime import date, datetime, timedelta
from typing import Any, TypeVar

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

from .binary_sensor import _has_tomorrow
from .const import (
    ENERGY_CHARTS_ATTRIBUTION,
    CONF_CONTRACT_END_DATE,
    CONF_DAILY_COMPARE,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    REGION_FLANDERS,
    RESOLUTION_HOURLY,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    DEFAULT_DAILY_COMPARE,
)
from .cohort import (
    _parse_iso_date,
)
from .coordinator import (
    BePricesCoordinator,
    CoordinatorData,
    supplier_device_info,
    ytd_window_reset,
)
from .pricing import PriceBreakdown, breakdown_row, slot_start

# What one slot of a per-slot table holds: a PriceBreakdown for the price
# table, a plain EUR/kWh float for the injection one.
_SlotValue = TypeVar("_SlotValue")


@dataclass(frozen=True, kw_only=True)
class BePriceSensorDescription(SensorEntityDescription):
    """Sensor description with a pure value extractor."""

    value_fn: Callable[[CoordinatorData], float | None]
    # Takes the entry: current_year_cost's reset instant is per-entry now,
    # since an entry can bill from its contract start date instead of 1
    # January.
    last_reset_fn: Callable[[ConfigEntry], datetime] | None = None


def _current_slot_value(
    slots: dict[datetime, _SlotValue], resolution: str
) -> _SlotValue | None:
    """Look ``slots`` up at the slot the wall clock is in.

    Reading the clock here rather than at coordinator-refresh time is what
    keeps a sensor aligned to the slot the user is billed for: the
    coordinator's own tick is a plain 60-minute interval anchored on setup,
    so a value baked into it lags the boundary by however far the tick has
    drifted.

    On an exact miss the temporally nearest slot is substituted, but only
    within one billing slot of "now" (15 min on a quarter-hourly contract,
    1 h otherwise, the latter also absorbing the DST seam). That bound
    stops a stale spot cache from silently surfacing yesterday's last slot
    as "now"; a fixed 1 h window let a quarter-hourly sensor surface an
    up-to-45-min-stale slot as current. Returns ``None`` when the table is
    empty or nothing falls inside the window.
    """
    if not slots:
        return None
    now = slot_start(dt_util.utcnow(), resolution)
    if (exact := slots.get(now)) is not None:
        return exact
    nearest_slot = min(slots, key=lambda h: abs((h - now).total_seconds()))
    max_gap = 3600.0 if resolution == RESOLUTION_HOURLY else 900.0
    if abs((nearest_slot - now).total_seconds()) > max_gap:
        return None
    return slots[nearest_slot]


def _current(data: CoordinatorData) -> PriceBreakdown | None:
    return _current_slot_value(data.hourly, data.resolution)


def _current_injection(data: CoordinatorData) -> float | None:
    """Injection price for the slot the wall clock is in.

    ``injection_price_eur_per_kwh`` is resolved once per coordinator tick,
    so on a contract whose injection varies intra-day (Engie Empower
    Flextime's TOU schedule, every spot-indexed injection) the sensor kept
    the previous slot's rate until the next tick, while the consumption
    sensors moved on the boundary (issue #44). ``injection_hourly`` already
    holds the per-slot rate over the same grid as ``hourly``, so read the
    current slot out of it the way ``_current`` reads the price table.

    A single slot the coordinator could not price (a dynamic contract with
    a hole in the day-ahead curve) is covered by the shared nearest-slot
    rule, so the sensor shows an adjacent slot's rate. The tick's scalar is
    the last resort: the flat contracts that emit no array at all, and a
    table with nothing inside the window.
    """
    rate = _current_slot_value(data.injection_hourly, data.resolution)
    return data.injection_price_eur_per_kwh if rate is None else rate


def _next_hour(data: CoordinatorData) -> PriceBreakdown | None:
    if not data.hourly:
        return None
    # One hour ahead of the current slot. For a 15-minute contract this
    # is the same quarter in the next hour, so the sensor keeps its "next
    # hour" meaning rather than becoming "next 15 minutes".
    target = slot_start(dt_util.utcnow(), data.resolution) + timedelta(hours=1)
    return data.hourly.get(target)


def _bucket(
    data: CoordinatorData,
    when: date,
    reducer: Callable[[list[float]], float],
) -> float | None:
    values = [
        bd.all_in
        for hour, bd in data.hourly.items()
        if dt_util.as_local(hour).date() == when
    ]
    if not values:
        return None
    return reducer(values)


def _avg(values: list[float]) -> float:
    return sum(values) / len(values)


def _avg_breakdown(bds: list[PriceBreakdown]) -> PriceBreakdown:
    """Mean of each component across a list of breakdowns."""
    n = len(bds)
    return PriceBreakdown(
        energy=sum(b.energy for b in bds) / n,
        network=sum(b.network for b in bds) / n,
        taxes=sum(b.taxes for b in bds) / n,
        all_in=sum(b.all_in for b in bds) / n,
    )


def _hourly_view(data: CoordinatorData) -> dict[datetime, PriceBreakdown]:
    """Hourly-resolution view of the price table, for the ranked lists.

    Returns ``data.hourly`` unchanged for hourly contracts; for a
    quarter-hourly one it averages each hour's four slots into one breakdown.

    Only ``cheapest_4h_today`` / ``most_expensive_4h_today`` read this, and
    they read it because they are counted in HOURS. Ranking the native slots
    and taking four of them would quietly turn "the cheapest four hours" into
    the cheapest one, which is not what either name says.

    The ``today`` / ``tomorrow`` curves do NOT come through here. They carry
    whatever grid the contract settles on, which is the whole curve a battery
    or an EV schedule needs to plan against.
    """
    if data.resolution == RESOLUTION_HOURLY:
        return data.hourly
    buckets: dict[datetime, list[PriceBreakdown]] = {}
    for slot, bd in data.hourly.items():
        hour = slot.replace(minute=0, second=0, microsecond=0)
        buckets.setdefault(hour, []).append(bd)
    return {hour: _avg_breakdown(bds) for hour, bds in buckets.items()}


def _today_avg(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), _avg)


def _today_min(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), min)


def _today_max(data: CoordinatorData) -> float | None:
    return _bucket(data, dt_util.now().date(), max)


def _tomorrow_bucket(
    data: CoordinatorData, reducer: Callable[[list[float]], float]
) -> float | None:
    """Reduce tomorrow's slots, but only while the card actually covers them.

    The price table forward-fills 48 hours, so on the last day of a monthly
    card's validity the "tomorrow" rows are an extrapolation the supplier has
    not published: next month's rates do not exist yet. ``_has_tomorrow`` has
    always refused to claim those hours, so the binary sensor went off while
    these three reported the extrapolation as a number, and the two entities
    contradicted each other for a full day every month. Sharing the predicate
    makes the invariant explicit: a tomorrow_* sensor has a value exactly when
    tomorrow_prices_available is on.

    Only the tomorrow side is gated. An expired card still describes today
    better than nothing does, and a snapshot stale enough to worry about
    raises its own repair issue.
    """
    if not _has_tomorrow(data):
        return None
    return _bucket(data, dt_util.now().date() + timedelta(days=1), reducer)


def _tomorrow_avg(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, _avg)


def _tomorrow_min(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, min)


def _tomorrow_max(data: CoordinatorData) -> float | None:
    return _tomorrow_bucket(data, max)


def _today_ranked(
    data: CoordinatorData, count: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pick the ``count`` cheapest and ``count`` most-expensive today-hours.

    The two lists are always disjoint: when fewer than ``2 * count`` today
    hours are populated (e.g. right after midnight on a static contract),
    the cheapest take their share first and the most-expensive list gets
    only what remains. Each list is returned in chronological order.

    On flat tariffs (every hour rounded to the same all-in price) the
    chronological tie-break makes the result fully deterministic: the
    cheapest list will always be the first ``count`` hours of the day
    and the most-expensive list will be the last ``count``. Automations
    keying on these attributes for "cheapest window" should treat the
    output as undefined when prices don't actually vary across the day.
    """
    hourly = _hourly_view(data)
    today = dt_util.now().date()
    pairs = [(h, bd) for h, bd in hourly.items() if dt_util.as_local(h).date() == today]
    if not pairs:
        return [], []
    # Secondary key on the hour breaks ties deterministically across
    # reloads. Without it, dict-insertion order leaks into the
    # cheapest_4h_today / most_expensive_4h_today attributes whenever
    # multiple hours share the same all-in price (common on static
    # contracts where every hour rounds to the same four decimals).
    by_price_asc = sorted(pairs, key=lambda x: (x[1].all_in, x[0]))
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


def _split_hourly_today_tomorrow(
    hourly: dict[datetime, Any],
    row_fn: Callable[[datetime, Any], dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group ``hourly`` into today and tomorrow buckets in chronological
    order, serialising each slot with ``row_fn(local, value)``. Slots
    outside the two-day window (typically none) are dropped."""
    today = dt_util.now().date()
    tomorrow = today + timedelta(days=1)
    today_rows: list[dict[str, Any]] = []
    tomorrow_rows: list[dict[str, Any]] = []
    for h, value in sorted(hourly.items()):
        local = dt_util.as_local(h)
        row = row_fn(local, value)
        if local.date() == today:
            today_rows.append(row)
        elif local.date() == tomorrow:
            tomorrow_rows.append(row)
    return today_rows, tomorrow_rows


def _split_today_tomorrow(
    data: CoordinatorData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group the cached breakdowns into today and tomorrow buckets.

    At the grid the contract settles on: 24 rows per day on an hourly
    contract, 96 on a quarter-hourly one. Both lists are returned in
    chronological order, and slots outside the today/tomorrow window
    (typically there are none) are dropped.

    These used to be averaged down to hourly on the grounds that ~192 rows
    would exceed HA's 16 KB per-state-attribute limit. They would, but that
    limit never applied here: both keys are in ``_unrecorded_attributes``, and
    the recorder strips excluded attributes BEFORE it measures
    (``db_schema.shared_attrs_bytes_from_event``). So the downsampling bought
    nothing and cost the one thing a 15-minute contract is chosen for, which
    is knowing which quarter is cheap.
    """
    return _split_hourly_today_tomorrow(data.hourly, breakdown_row)


def _split_injection_today_tomorrow(
    data: CoordinatorData,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Group the per-slot injection prices into today and tomorrow buckets.

    Each bucket is a chronological list of ``{start, injection}`` rows, at the
    grid the contract settles on. Both are empty for a contract whose
    injection doesn't vary intra-day (the coordinator emits no
    ``injection_hourly`` for it).

    Publishing the slots rather than an hourly mean of them also retires an
    approximation. A floored feed-in formula is convex, so the mean of four
    floored quarter rates is not the rate of their mean, and the hourly row
    this used to show was the former while the credit is earned per slot.
    """
    return _split_hourly_today_tomorrow(
        data.injection_hourly,
        lambda local, rate: {"start": local.isoformat(), "injection": round(rate, 6)},
    )


def _current_field(field: str) -> Callable[[CoordinatorData], float | None]:
    def _inner(data: CoordinatorData) -> float | None:
        bd = _current(data)
        return None if bd is None else getattr(bd, field)

    return _inner


def _eur_per_kwh(
    key: str, value_fn: Callable[[CoordinatorData], float | None]
) -> BePriceSensorDescription:
    """Build a EUR/kWh measurement description with the standard precision."""
    return BePriceSensorDescription(
        key=key,
        translation_key=key,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="EUR/kWh",
        suggested_display_precision=4,
        value_fn=value_fn,
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
        # pinned to local midnight of the window start -- Jan 1, or the
        # contract start date on an entry that bills from it -- lets the
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
        last_reset_fn=ytd_window_reset,
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
        # not what the user wants here -- ask HA's developer-tools
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

    entities: list[SensorEntity] = [
        BePriceSensor(coordinator, desc) for desc in descriptions
    ]
    end_date = _parse_iso_date(entry.data.get(CONF_CONTRACT_END_DATE))
    if end_date is not None:
        entities.append(ContractEndDateSensor(coordinator, end_date))
    if entry.data.get(CONF_DAILY_COMPARE, DEFAULT_DAILY_COMPARE):
        entities.append(PotentialSavingSensor(coordinator))
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
    # fees) climbs every tick as well, so keep it out of the recorder too.
    _unrecorded_attributes = frozenset(
        {
            "today",
            "tomorrow",
            "cheapest_4h_today",
            "most_expensive_4h_today",
            "snapshot_age_hours",
            "last_error",
            "consumption_ytd_kwh",
            "injection_ytd_kwh",
            "consumption_today_kwh",
            "injection_today_kwh",
            "energy_ytd_raw_eur",
            "fees_ytd_eur",
            "hours_seen",
            "hours_priced",
            "energy_basis",
            "fee_basis",
            "volume_basis",
            "injection_basis",
            "annual_kwh",
            "annual_injection_kwh",
            "contract_basis",
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
        fn = self.entity_description.last_reset_fn
        return fn(self.coordinator.entry) if fn is not None else None

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
                "snapshot_age_hours": round(data.snapshot_age_hours, 2),
                "snapshot_stale": data.snapshot_stale,
                "last_error": data.last_error,
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
            # Only spot-indexed / TOU contracts populate injection_hourly, so
            # a flat contract stays attribute-free rather than repeating one
            # value 24-48 times.
            today, tomorrow = _split_injection_today_tomorrow(data)
            if not today and not tomorrow:
                return {}
            return {"today": today, "tomorrow": tomorrow}
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
            # On the static per-day path a negative energy_ytd_raw_eur means
            # the compensation zero-floor is hiding banked injection (working
            # as designed), and a consumption_today_kwh that never grows points
            # at a stalled meter input. On the hourly path hours_priced below
            # hours_seen means the spot cache could not price part of the
            # window, so the bill is missing those hours' energy term.
            diag = data.ytd_diagnostics
            if not diag:
                return {}
            return {k: round(v, 4) for k, v in diag.items()}
        if self.entity_description.key == "projected_year_cost":
            # Its own branch rather than sharing the one above: these
            # attributes are a mix of strings and floats, and round() raises
            # TypeError on a string. The strings are the point of them, since
            # a projection is only as trustworthy as the basis it names.
            proj = data.projection_diagnostics
            if not proj:
                return {}
            return {
                k: round(v, 4) if isinstance(v, float) else v for k, v in proj.items()
            }
        return {}


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
                }
                for row in sorted(
                    result.rows,
                    key=lambda r: (r.annual is None, r.annual or 0.0),
                )
            ],
        }
