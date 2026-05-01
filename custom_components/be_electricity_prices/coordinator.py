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

"""Data coordinator for the Belgian Electricity Prices integration.

Caches the latest supplier snapshot from disk so an offline boot can still
serve last-known prices, while a daily refresh tries to update from the
supplier source. Per the project's fail policy, if a refresh fails the
coordinator keeps serving the cached snapshot and surfaces a repair issue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EntsoeAuthError, EntsoeClient, EntsoeError
from .const import (
    CAPACITY_MODE_FIXED,
    CAPACITY_MODE_SENSOR,
    CONF_API_KEY,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONSUMPTION_KWH,
    CONF_CONTRACT,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INJECTION_KWH,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    DSO_MODE_BI_HORAIRE,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    VREG_CAPACITY_FLOOR_KW,
    DOMAIN,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from .pricing import PriceBreakdown, compute_breakdown, is_offpeak, static_breakdown
from .providers import (
    DynamicRates,
    ExtractorError,
    SupplierSnapshot,
    get as get_extractor,
)
from .providers.base import (
    DsoOverlay,
    EnergyRates,
    FixedRates,
    InjectionRates,
    TaxOverlay,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)

SNAPSHOT_REFRESH_HOURS = 24
SNAPSHOT_STALE_DAYS = 7

# Process-wide snapshot sharing across config entries. Two entries that
# point at the same (supplier, contract, region) share their freshly
# fetched SupplierSnapshot, so we never poll the same PDF twice. Each
# key also has an asyncio.Lock so concurrent first-fetches deduplicate.
_SHARED_SNAPSHOTS_KEY = "snapshot_cache"
_SHARED_LOCKS_KEY = "snapshot_locks"


@dataclass
class _SharedSnapshot:
    snapshot: "SupplierSnapshot"
    fetched_at: datetime


def _shared_snapshots(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str], _SharedSnapshot]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_SHARED_SNAPSHOTS_KEY, {})  # type: ignore[no-any-return]


def _shared_lock(hass: HomeAssistant, key: tuple[str, str, str]) -> asyncio.Lock:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str], asyncio.Lock] = bucket.setdefault(
        _SHARED_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


@dataclass
class CoordinatorData:
    """Snapshot the coordinator hands to entities."""

    hourly: dict[datetime, PriceBreakdown] = field(default_factory=dict)
    snapshot_publication: str = ""
    snapshot_age_hours: float = 0.0
    snapshot_stale: bool = False
    # Last calendar day the snapshot's rates apply to. ``None`` means
    # the extractor couldn't parse a validity end -- callers should
    # fall back to "treat as valid".
    snapshot_valid_until: date | None = None
    last_error: str = ""
    monthly_peak_kw: float = 0.0
    monthly_peak_month: date | None = None
    capacity_cost_eur: float = 0.0
    prosumer_cost_eur: float = 0.0
    # EUR/kWh injection price for the current hour. None when:
    #   - the user is not on the injection regime, or
    #   - the snapshot's injection block has no usable data (formula needs
    #     spot but contract is variable so we don't fetch ENTSO-E).
    injection_price_eur_per_kwh: float | None = None
    # Supplier yearly fixed fee (EUR/year) and Flemish energy-fund
    # monthly charge (EUR/month). Both are parsed from the tariff card
    # but don't enter the per-kWh all-in number; surfacing them as
    # separate sensors lets users compute total monthly cost.
    yearly_fixed_fee_eur: float = 0.0
    energy_fund_eur_per_month: float = 0.0
    # Running annual bill in EUR. None until the user has populated all
    # four meter sensors (day_cons, night_cons, day_inj, night_inj). For
    # compensation regime the math nets injection 1:1 against
    # consumption (per-band when bi); for injection regime each is
    # multiplied by its own rate; for "none" only consumption counts.
    # Goes negative when injection income exceeds consumption + fees,
    # which only happens under classical compensation regime if you
    # over-produce (most suppliers forfeit the surplus -- the negative
    # value is the *theoretical* net the bill would settle at). Always
    # ``None`` for dynamic / TOU contracts because hourly rates can't
    # be applied to a daily total.
    current_year_cost_eur: float | None = None


class BePricesCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Pull supplier snapshot + ENTSO-E spot, build the hourly price table."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}_cache_{entry.entry_id}"
        )
        self._snapshot: SupplierSnapshot | None = None
        self._snapshot_fetched_at: datetime | None = None
        self._spot_cache: dict[datetime, float] = {}
        self._spot_cache_day: date | None = None
        self._spot_cache_includes_tomorrow = False
        self._peak_kw: float = 0.0
        self._peak_month: date | None = None
        self._last_error: str = ""
        # Day/night kWh buckets fed by deltas of CONF_CONSUMPTION_KWH /
        # CONF_INJECTION_KWH state changes. Persisted across restarts so
        # the running current_year_cost survives HA restarts.
        self._kwh_buckets: dict[str, float] = {
            "consumption_day": 0.0,
            "consumption_night": 0.0,
            "injection_day": 0.0,
            "injection_night": 0.0,
        }
        # Last-seen value for each tracked total sensor; deltas are
        # computed against this. None means we haven't seen a value yet
        # (first state event after setup just records baseline).
        self._kwh_baselines: dict[str, float] = {}
        self._kwh_unsub: list[Callable[[], None]] = []
        # Calendar-year boundary tracking for current_year_cost: we want the
        # sensor to read 0 (just fees) on Jan 1 and grow over the year.
        # ``_year_start`` is the calendar year the register baselines and
        # bucket counters apply to; on year rollover we capture new
        # register baselines from current state and zero the buckets.
        # ``None`` until the first refresh that successfully establishes
        # a baseline.
        self._year_start: int | None = None
        # Snapshot of register-sensor states at the start of
        # ``_year_start``. ``current_year_cost`` then reports
        # ``current - baseline`` per register, so it goes back to 0 on
        # Jan 1. Empty dict when only the bucket-path totals are
        # configured.
        self._year_start_register_baselines: dict[str, float] = {}

    async def async_load_persistent(self) -> None:
        """Restore the latest snapshot + monthly peak from HA Store."""
        stored = await self._store.async_load()
        if not stored:
            return
        snap = stored.get("snapshot")
        if isinstance(snap, dict):
            try:
                self._snapshot = _snapshot_from_dict(snap)
                self._snapshot_fetched_at = datetime.fromisoformat(snap["_cached_at"])
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning(
                    "discarding cached snapshot for %s: %s",
                    self.entry.entry_id,
                    err,
                )
                self._snapshot = None
                self._snapshot_fetched_at = None
        peak = stored.get("peak")
        if isinstance(peak, dict):
            value = peak.get("kw")
            month = peak.get("month")
            if isinstance(value, (int, float)) and isinstance(month, str):
                self._peak_kw = float(value)
                try:
                    self._peak_month = date.fromisoformat(month)
                except ValueError:
                    self._peak_month = None
        kwh = stored.get("kwh_buckets")
        if isinstance(kwh, dict):
            for key in self._kwh_buckets:
                value = kwh.get(key)
                if isinstance(value, (int, float)):
                    self._kwh_buckets[key] = float(value)
        baselines = stored.get("kwh_baselines")
        if isinstance(baselines, dict):
            for entity_id, value in baselines.items():
                if isinstance(entity_id, str) and isinstance(value, (int, float)):
                    self._kwh_baselines[entity_id] = float(value)
        year_start = stored.get("year_start")
        if isinstance(year_start, int):
            self._year_start = year_start
        register_baselines = stored.get("year_start_register_baselines")
        if isinstance(register_baselines, dict):
            for entity_id, value in register_baselines.items():
                if isinstance(entity_id, str) and isinstance(value, (int, float)):
                    self._year_start_register_baselines[entity_id] = float(value)

    @callback
    def async_setup_kwh_listeners(self) -> None:
        """Subscribe to state changes on the configured total kWh sensors.

        Idempotent: tearing down previous subscriptions before re-arming
        lets the options-flow reload swap in different sensor entity_ids
        without leaking listeners.
        """
        self.async_teardown_kwh_listeners()
        entities = [
            eid
            for eid in (
                self.entry.data.get(CONF_CONSUMPTION_KWH),
                self.entry.data.get(CONF_INJECTION_KWH),
            )
            if eid
        ]
        if not entities:
            return
        unsub = async_track_state_change_event(
            self.hass, entities, self._handle_kwh_state_change
        )
        self._kwh_unsub.append(unsub)

    @callback
    def async_teardown_kwh_listeners(self) -> None:
        for unsub in self._kwh_unsub:
            unsub()
        self._kwh_unsub.clear()

    @callback
    def _handle_kwh_state_change(self, event: Event[EventStateChangedData]) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state in ("", "unknown", "unavailable"):
            return
        try:
            value = float(new_state.state)
        except (TypeError, ValueError):
            return
        entity_id = event.data["entity_id"]
        prev = self._kwh_baselines.get(entity_id)
        if prev is None or value < prev:
            # First reading after setup, or counter rolled back (utility
            # meter reset, sensor replaced). Reset baseline silently --
            # no delta to bucket.
            self._kwh_baselines[entity_id] = value
            return
        if value == prev:
            return
        delta = value - prev
        self._kwh_baselines[entity_id] = value
        # Roll the year over before bucketing the delta. Without this, a
        # state event arriving on Jan 1 after midnight but before the
        # next coordinator tick deposits its delta into a bucket the
        # later rollover then zeroes -- the first new-year deltas get
        # silently dropped from current_year_cost.
        self._roll_over_current_year_cost_if_needed()
        kind = (
            "consumption"
            if entity_id == self.entry.data.get(CONF_CONSUMPTION_KWH)
            else "injection"
        )
        band = "night" if is_offpeak(dt_util.now()) else "day"
        self._kwh_buckets[f"{kind}_{band}"] += delta
        # Trigger a coordinator refresh so current_year_cost re-renders. The
        # price snapshot itself is cached for 24h; refresh is essentially
        # free here (no network).
        self.hass.async_create_task(self.async_request_refresh())

    async def _async_update_data(self) -> CoordinatorData:
        # Roll over before any awaits: a state event firing during the
        # awaits below would otherwise add a fresh delta to a bucket
        # the rollover is about to zero.
        self._roll_over_current_year_cost_if_needed()
        # Freeze the kWh accumulators at the start of the tick so the
        # current_year_cost we return matches the bucket state we just
        # rolled over. State events firing during the awaits below
        # update self._kwh_buckets in place, but the entities will see
        # those new deltas on the next tick instead of a value desynced
        # from what was persisted at the end of this one.
        kwh_buckets_snapshot = dict(self._kwh_buckets)
        register_baselines_snapshot = dict(self._year_start_register_baselines)
        await self._maybe_refresh_snapshot()
        await self._track_monthly_peak()

        if self._snapshot is None:
            raise UpdateFailed(
                f"no supplier snapshot available: {self._last_error or 'cold start'}"
            )

        spot_prices: dict[datetime, float] = {}
        if isinstance(self._snapshot.energy, DynamicRates):
            try:
                spot_prices = await self._fetch_spot_prices()
            except EntsoeAuthError as err:
                raise UpdateFailed(f"ENTSO-E auth: {err}") from err
            except EntsoeError as err:
                # A transient ENTSO-E outage must not blank the entry: the
                # last good day-ahead curve in _spot_cache is still usable
                # for breakdown computation. Only fail if we have nothing
                # cached either.
                self._last_error = f"ENTSO-E: {err}"
                _LOGGER.warning("ENTSO-E refresh failed; serving cached spots: %s", err)
                if not self._spot_cache:
                    raise UpdateFailed(f"ENTSO-E: {err}") from err
                spot_prices = dict(self._spot_cache)

        hourly = self._build_hourly(spot_prices)

        capacity_cost = 0.0
        if self.entry.data.get(CONF_REGION) == REGION_FLANDERS:
            capacity_cost = _compute_capacity(self._snapshot, self.entry, self._peak_kw)

        prosumer_cost = _compute_prosumer(self._snapshot, self.entry)
        injection_price = _compute_injection_price(
            self._snapshot, self.entry, spot_prices
        )
        current_year_cost = _compute_current_year_cost(
            self.hass,
            self._snapshot,
            self.entry,
            prosumer_cost_eur_per_month=prosumer_cost,
            injection_price_eur_per_kwh=injection_price,
            kwh_buckets=kwh_buckets_snapshot,
            register_baselines=register_baselines_snapshot,
        )

        await self._save_persistent()

        age = self._snapshot_age_hours()
        stale = age > SNAPSHOT_STALE_DAYS * 24
        self._sync_stale_issue(stale)
        return CoordinatorData(
            hourly=hourly,
            snapshot_publication=self._snapshot.publication_label,
            snapshot_age_hours=age,
            snapshot_stale=stale,
            snapshot_valid_until=self._snapshot.valid_until,
            last_error=self._last_error,
            monthly_peak_kw=self._peak_kw,
            monthly_peak_month=self._peak_month,
            capacity_cost_eur=capacity_cost,
            prosumer_cost_eur=prosumer_cost,
            injection_price_eur_per_kwh=injection_price,
            yearly_fixed_fee_eur=getattr(
                self._snapshot.energy, "yearly_fixed_fee", 0.0
            ),
            energy_fund_eur_per_month=self._snapshot.taxes.energy_fund_eur_per_month,
            current_year_cost_eur=current_year_cost,
        )

    def _sync_stale_issue(self, stale: bool) -> None:
        """Raise or clear the 'snapshot stale' repair issue for this entry."""
        issue_id = f"snapshot_stale_{self.entry.entry_id}"
        if stale:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="snapshot_stale",
                translation_placeholders={
                    "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
                    "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
                    "days": str(SNAPSHOT_STALE_DAYS),
                    "last_error": self._last_error or "unknown",
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_force_refresh(self) -> None:
        """Drop cached snapshot + spot prices and re-fetch immediately.

        Invoked by the be_electricity_prices.refresh service when the user
        wants the integration to pick up a new tariff card or correct an
        error without waiting for the 24h refresh tick. Evicts the shared
        (supplier, contract, region) entry too so other coordinators with
        the same key also see a fresh fetch on their next refresh.
        """
        self._snapshot_fetched_at = None
        self._spot_cache = {}
        self._spot_cache_day = None
        self._spot_cache_includes_tomorrow = False
        _shared_snapshots(self.hass).pop(self._shared_key(), None)
        await self.async_request_refresh()

    def _shared_key(self) -> tuple[str, str, str]:
        return (
            self.entry.data[CONF_SUPPLIER],
            self.entry.data[CONF_CONTRACT],
            self.entry.data[CONF_REGION],
        )

    def _adopt_shared(self, shared: _SharedSnapshot) -> None:
        """Take a fresh shared snapshot as our own."""
        self._snapshot = shared.snapshot
        self._snapshot_fetched_at = shared.fetched_at
        self._last_error = ""

    async def _maybe_refresh_snapshot(self) -> None:
        ttl = timedelta(hours=SNAPSHOT_REFRESH_HOURS)
        now = dt_util.utcnow()
        if self._snapshot_fetched_at and (now - self._snapshot_fetched_at < ttl):
            return

        key = self._shared_key()
        cache = _shared_snapshots(self.hass)
        # Free, non-blocking shortcut when another coordinator has already
        # done the work.
        shared = cache.get(key)
        if shared and (now - shared.fetched_at < ttl):
            self._adopt_shared(shared)
            return

        async with _shared_lock(self.hass, key):
            shared = cache.get(key)
            if shared and (dt_util.utcnow() - shared.fetched_at < ttl):
                self._adopt_shared(shared)
                return
            try:
                extractor = get_extractor(self.entry.data[CONF_SUPPLIER])
                snap = await extractor.fetch(
                    self._session,
                    self.entry.data[CONF_CONTRACT],
                    self.entry.data[CONF_REGION],
                )
                fetched_at = dt_util.utcnow()
                cache[key] = _SharedSnapshot(snapshot=snap, fetched_at=fetched_at)
                self._snapshot = snap
                self._snapshot_fetched_at = fetched_at
                self._last_error = ""
            except (ExtractorError, asyncio.TimeoutError) as err:
                self._last_error = str(err)
                _LOGGER.warning(
                    "snapshot refresh failed for %s/%s: %s; keeping cached",
                    self.entry.data.get(CONF_SUPPLIER),
                    self.entry.data.get(CONF_CONTRACT),
                    err,
                )

    async def _fetch_spot_prices(self) -> dict[datetime, float]:
        api_key = self.entry.data.get(CONF_API_KEY)
        if not api_key:
            raise EntsoeError("missing ENTSO-E API key")

        # Window the request on the *local* day (Europe/Brussels) so a
        # 00:00-02:00 local query doesn't drop yesterday's UTC tail or
        # miss tomorrow because UTC is still on the previous date.
        local_today = dt_util.now().date()
        now_local = dt_util.now()
        want_tomorrow = now_local.hour >= 11
        if (
            self._spot_cache_day == local_today
            and (not want_tomorrow or self._spot_cache_includes_tomorrow)
            and self._spot_cache
        ):
            return self._spot_cache

        client = EntsoeClient(api_key, self._session)
        start_local = datetime.combine(
            local_today, datetime.min.time(), tzinfo=now_local.tzinfo
        )
        start = start_local.astimezone(UTC)
        end = start + timedelta(days=2 if want_tomorrow else 1)
        prices = await client.fetch_day_ahead(start, end)
        self._spot_cache = prices
        self._spot_cache_day = local_today
        self._spot_cache_includes_tomorrow = want_tomorrow
        return prices

    @callback
    def _roll_over_current_year_cost_if_needed(self) -> None:
        """Capture register baselines + zero buckets at year boundaries,
        and top up any baseline still missing within the current year.

        ``current_year_cost`` reports the bill since Jan 1 of the current local
        calendar year (or since first refresh, whichever is later). On
        every Jan 1 boundary we:
          - clear ``_year_start_register_baselines`` so the next pass
            captures fresh values,
          - zero the four ``_kwh_buckets``,
          - update ``_year_start``.

        Then, on every refresh (year-change or not), we top up any
        configured register sensor that doesn't yet have a baseline.
        Without this top-up, a sensor that was temporarily unavailable
        during the first refresh after install would never get a
        baseline (the year-only gate would skip it), and ``current_year_cost``
        would stay unknown until next Jan 1.
        """
        this_year = dt_util.now().year
        if self._year_start != this_year:
            self._year_start_register_baselines = {}
            for key in self._kwh_buckets:
                self._kwh_buckets[key] = 0.0
            self._year_start = this_year
        register_confs = (
            CONF_DAY_CONSUMPTION_KWH,
            CONF_NIGHT_CONSUMPTION_KWH,
            CONF_DAY_INJECTION_KWH,
            CONF_NIGHT_INJECTION_KWH,
        )
        for conf in register_confs:
            entity_id = self.entry.data.get(conf)
            if not entity_id or entity_id in self._year_start_register_baselines:
                continue
            value = _read_kwh(self.hass, entity_id)
            if value is not None:
                self._year_start_register_baselines[entity_id] = value

    async def _track_monthly_peak(self) -> None:
        if self.entry.data.get(CONF_REGION) != REGION_FLANDERS:
            # Outside Flanders the capacity tariff doesn't apply. Reset
            # any peak left over from a previous Flanders config so it
            # doesn't linger in diagnostics or the persistent store.
            self._peak_kw = 0.0
            self._peak_month = None
            return
        # Roll over on the local 1st-of-month; using UTC would lag CET/CEST
        # users by 1-2 hours on the boundary and miss late-Dec-31 / early-Jan-1.
        local_now = dt_util.now()
        current_month = date(local_now.year, local_now.month, 1)
        if self._peak_month != current_month:
            self._peak_month = current_month
            self._peak_kw = 0.0

        mode = self.entry.data.get(CONF_CAPACITY_MODE)
        if mode == CAPACITY_MODE_FIXED:
            # Use the configured value directly; rolling-max would
            # ignore a mid-month decrease the user just made via
            # OptionsFlow until next month rollover.
            self._peak_kw = float(
                self.entry.data.get(CONF_CAPACITY_FIXED_KW, VREG_CAPACITY_FLOOR_KW)
            )
        elif mode == CAPACITY_MODE_SENSOR:
            entity_id = self.entry.data.get(CONF_CAPACITY_PEAK_SENSOR)
            state: State | None = self.hass.states.get(entity_id) if entity_id else None
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    value = float(state.state)
                except (TypeError, ValueError):
                    value = 0.0
                if value > self._peak_kw:
                    self._peak_kw = value

        # Apply the regulated VREG floor regardless of mode - Fluvius bills
        # max(measured_peak, floor), so a household whose monthly peak stays
        # below 2.5 kW still pays the floor in the capacity_cost sensor.
        self._peak_kw = max(self._peak_kw, VREG_CAPACITY_FLOOR_KW)

    def _build_hourly(
        self, spot_prices: dict[datetime, float]
    ) -> dict[datetime, PriceBreakdown]:
        snap = self._snapshot
        assert snap is not None
        dso = self.entry.data[CONF_DSO]
        region = self.entry.data[CONF_REGION]
        meter = self.entry.data.get(CONF_METER, METER_MONO)
        dso_mode = self.entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)

        hourly: dict[datetime, PriceBreakdown] = {}
        if isinstance(snap.energy, DynamicRates):
            for utc_hour, spot in spot_prices.items():
                local = dt_util.as_local(utc_hour)
                hourly[utc_hour] = compute_breakdown(
                    snap, dso, region, local, spot, meter, dso_mode
                )
            return hourly

        # Iterate in UTC so a DST spring-forward day still yields 48 distinct
        # entries; deriving local from a fixed-step UTC anchor preserves the
        # gap correctly. Naively walking local-time + timedelta would either
        # collide two hours into one UTC slot (spring) or duplicate a UTC slot
        # (fall) and silently drop one breakdown.
        # Anchor at local midnight (converted to UTC) so today_min / today_max
        # / today_average cover the full local day, not just "now → midnight".
        local_midnight = dt_util.start_of_local_day()
        start_utc = local_midnight.astimezone(UTC).replace(
            minute=0, second=0, microsecond=0
        )
        for offset in range(48):
            utc = start_utc + timedelta(hours=offset)
            local = dt_util.as_local(utc)
            hourly[utc] = compute_breakdown(
                snap, dso, region, local, None, meter, dso_mode
            )
        return hourly

    def _snapshot_age_hours(self) -> float:
        if self._snapshot_fetched_at is None:
            return float("inf")
        return (dt_util.utcnow() - self._snapshot_fetched_at).total_seconds() / 3600.0

    async def _save_persistent(self) -> None:
        payload: dict[str, Any] = {
            "peak": {
                "kw": self._peak_kw,
                "month": self._peak_month.isoformat() if self._peak_month else "",
            },
            "kwh_buckets": dict(self._kwh_buckets),
            "kwh_baselines": dict(self._kwh_baselines),
            "year_start": self._year_start,
            "year_start_register_baselines": dict(self._year_start_register_baselines),
        }
        if self._snapshot is not None and self._snapshot_fetched_at is not None:
            payload["snapshot"] = _snapshot_to_dict(
                self._snapshot, self._snapshot_fetched_at
            )
        await self._store.async_save(payload)


def _compute_capacity(
    snapshot: SupplierSnapshot, entry: ConfigEntry, peak_kw: float
) -> float:
    overlay = snapshot.dsos.get(entry.data[CONF_DSO])
    if overlay is None or overlay.capacity_eur_per_kw_year is None:
        return 0.0
    return peak_kw * overlay.capacity_eur_per_kw_year / 12.0


def _compute_injection_price(
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    spot_prices: dict[datetime, float],
) -> float | None:
    """Current-hour injection price in EUR/kWh for HA Energy's price entity.

    Only returned when the user is on the injection regime AND the supplier's
    snapshot has injection data. Prefers the formula+spot when a spot is
    available (dynamic contracts), otherwise falls back to the snapshot's
    static "current" indicative (Eneco Fix/Flex monthly value).
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return None
    inj = snapshot.injection
    if inj is None:
        return None
    # Hourly path: pick the spot for the current hour if we have one.
    if inj.factor is not None and inj.base is not None and spot_prices:
        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        spot = spot_prices.get(now_hour)
        if spot is None:
            # ENTSO-E publishes hour-aligned values; if today's curve doesn't
            # have our hour (rare DST / publication-lag edge), fall back to
            # the temporally nearest hour we have.
            nearest = min(
                spot_prices.keys(),
                key=lambda h: abs((h - now_hour).total_seconds()),
            )
            spot = spot_prices[nearest]
        return inj.factor * spot + inj.base
    # Static fallback: the supplier's printed monthly indicative.
    return inj.current


def _compute_prosumer(snapshot: SupplierSnapshot, entry: ConfigEntry) -> float:
    """Monthly prosumer (compensation regime) cost in EUR.

    Only Walloon installations certified before 2024-01-01 are under the
    compensation regime, and only until 2030-12-31. Post-2024 installations
    are on the injection tariff (no per-kVA fee). Returns 0 when:
      - the user has no solar (kVA <= 0),
      - the regime is not 'compensation',
      - the configured DSO has no prosumer rate in the snapshot
        (Flemish digital meters, Cociter SMR3 dynamic).
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_COMPENSATION:
        return 0.0
    try:
        kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if kva <= 0.0:
        return 0.0
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    if overlay is None or overlay.prosumer_eur_per_kva_year is None:
        return 0.0
    return kva * overlay.prosumer_eur_per_kva_year / 12.0


def _read_kwh(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Read a cumulative kWh sensor's state. Returns None if unset, missing,
    unavailable, or non-numeric -- the caller treats any None as "no
    current_year_cost computable yet" (signals the sensor to expose ``None``)."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if state is None or state.state in ("", "unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _read_register(
    hass: HomeAssistant,
    entry: ConfigEntry,
    conf: str,
    register_baselines: dict[str, float],
) -> float | None:
    """Read a register sensor as ``current - year_start_baseline``.

    The day/night register sensors on a digital meter are lifetime
    accumulators (they only reset on physical meter replacement), so we
    subtract a Jan-1 baseline captured by the year-rollover handler.
    Returns ``None`` when the sensor isn't configured, the register
    can't be read, or no baseline has been captured for it yet.
    """
    entity_id = entry.data.get(conf)
    if not entity_id:
        return None
    current = _read_kwh(hass, entity_id)
    if current is None:
        return None
    baseline = register_baselines.get(entity_id)
    if baseline is None:
        # No baseline yet (first refresh after install hasn't run, or
        # the sensor was unavailable when rollover fired). Defer.
        return None
    return current - baseline


def _compute_current_year_cost(
    hass: HomeAssistant,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    prosumer_cost_eur_per_month: float,
    injection_price_eur_per_kwh: float | None,
    kwh_buckets: dict[str, float],
    register_baselines: dict[str, float],
) -> float | None:
    """Running annual bill from cumulative meter readings + snapshot rates.

    Reports the bill *since Jan 1 of the current local calendar year*
    (or since first refresh, whichever is later). Both feed paths reset
    on Jan 1 -- buckets via the rollover handler, registers via a
    refreshed Jan-1 baseline that ``current - baseline`` then anchors
    on. Annual fees stay at full year-due value.

    Math depends on the user's regime + meter type, all using
    *this-year* cons / inj kWh (deltas from the year-start baseline):

      regime=none, mono : day_cons * single + night_cons * single
      regime=none, bi   : day_cons * peak + night_cons * offpeak
      regime=injection,
        mono : (day_cons + night_cons) * single - (day_inj + night_inj) * inj
      regime=injection,
        bi   : day_cons * peak + night_cons * offpeak
               - (day_inj + night_inj) * inj
      regime=compensation, mono :
               (day_cons + night_cons - day_inj - night_inj) * single
      regime=compensation, bi :
               (day_cons - day_inj) * peak + (night_cons - night_inj) * offpeak

    Plus, in every case:
      yearly_fixed_fee + 12 * energy_fund + 12 * prosumer_cost.

    Compensation uses the consumption rate for both sides because that's
    how net metering settles ("compteur qui tourne a l'envers"). No
    surplus floor is applied (negative values surface naturally if
    injection > consumption); see the project memo for why.

    Two ways to feed the consumption (and likewise injection) side:
      * Direct day/night register sensors (CONF_DAY_*_KWH /
        CONF_NIGHT_*_KWH). Used when present, anchored to the year-start
        baseline so the value resets to 0 each Jan 1.
      * Single cumulative-totals sensor (CONF_CONSUMPTION_KWH /
        CONF_INJECTION_KWH). The coordinator's state listener splits
        deltas into ``kwh_buckets`` based on is_offpeak(now); the
        rollover handler zeroes the buckets every Jan 1, so they too
        reflect this-year usage.

    Always returns a number -- never ``None``. Missing meter inputs are
    treated as 0 kWh; dynamic / TOU contracts (no stable rate) and a
    config with no meter sensors at all both fall back to a fees-only
    floor (yearly_fixed_fee + 12 * energy_fund + 12 * prosumer_cost).
    """
    day_cons = _read_register(hass, entry, CONF_DAY_CONSUMPTION_KWH, register_baselines)
    night_cons = _read_register(
        hass, entry, CONF_NIGHT_CONSUMPTION_KWH, register_baselines
    )
    day_inj = _read_register(hass, entry, CONF_DAY_INJECTION_KWH, register_baselines)
    night_inj = _read_register(
        hass, entry, CONF_NIGHT_INJECTION_KWH, register_baselines
    )
    # Bucket fallback: only kick in when the user didn't supply the
    # direct day/night registers AND the corresponding total sensor is
    # configured. Buckets are zeroed every Jan 1 so they already
    # represent this-year usage.
    if day_cons is None and night_cons is None and entry.data.get(CONF_CONSUMPTION_KWH):
        day_cons = kwh_buckets.get("consumption_day", 0.0)
        night_cons = kwh_buckets.get("consumption_night", 0.0)
    if day_inj is None and night_inj is None and entry.data.get(CONF_INJECTION_KWH):
        day_inj = kwh_buckets.get("injection_day", 0.0)
        night_inj = kwh_buckets.get("injection_night", 0.0)
    # Anything still ``None`` means the user hasn't wired that side
    # (e.g. no injection sensors when they have no solar) or the input
    # isn't readable yet. Treat as 0 so the sensor never goes
    # ``unknown`` -- worst case we show the fees-only floor.
    if day_cons is None:
        day_cons = 0.0
    if night_cons is None:
        night_cons = 0.0
    if day_inj is None:
        day_inj = 0.0
    if night_inj is None:
        night_inj = 0.0

    dso = entry.data.get(CONF_DSO, "")
    region = entry.data.get(CONF_REGION, "")
    meter = entry.data.get(CONF_METER, METER_MONO)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    fixed = getattr(snapshot.energy, "yearly_fixed_fee", 0.0)
    fund_yearly = snapshot.taxes.energy_fund_eur_per_month * 12.0
    prosumer_yearly = prosumer_cost_eur_per_month * 12.0
    fees = fixed + fund_yearly + prosumer_yearly

    single_bd = static_breakdown(snapshot, dso, region, "single")
    peak_bd = static_breakdown(snapshot, dso, region, "peak")
    offpeak_bd = static_breakdown(snapshot, dso, region, "offpeak")
    if single_bd is None or peak_bd is None or offpeak_bd is None:
        # Dynamic / TOU contract: no stable rate to apply. Show the
        # fees-only baseline so the sensor stays numeric.
        return fees

    total_cons = day_cons + night_cons
    total_inj = day_inj + night_inj

    if regime == SOLAR_REGIME_COMPENSATION:
        if meter == "bi":
            energy_cost = (day_cons - day_inj) * peak_bd.all_in + (
                night_cons - night_inj
            ) * offpeak_bd.all_in
        else:
            energy_cost = (total_cons - total_inj) * single_bd.all_in
    elif regime == SOLAR_REGIME_INJECTION:
        if meter == "bi":
            energy_cost = day_cons * peak_bd.all_in + night_cons * offpeak_bd.all_in
        else:
            energy_cost = total_cons * single_bd.all_in
        if injection_price_eur_per_kwh is not None:
            energy_cost -= total_inj * injection_price_eur_per_kwh
    else:  # none
        if meter == "bi":
            energy_cost = day_cons * peak_bd.all_in + night_cons * offpeak_bd.all_in
        else:
            energy_cost = total_cons * single_bd.all_in

    return energy_cost + fees


# ---- snapshot serialization for the HA Store ----------------------------------


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
_SNAPSHOT_SCHEMA_VERSION = 5


def _snapshot_to_dict(snap: SupplierSnapshot, fetched_at: datetime) -> dict[str, Any]:
    return {
        "_cached_at": fetched_at.isoformat(),
        "_schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "supplier": snap.supplier,
        "contract": snap.contract,
        "energy_kind": _energy_kind(snap.energy),
        "energy": snap.energy.__dict__,
        "dsos": {k: v.__dict__ for k, v in snap.dsos.items()},
        "taxes": snap.taxes.__dict__,
        "source_url": snap.source_url,
        "fetched_at_iso": snap.fetched_at_iso,
        "publication_label": snap.publication_label,
        "valid_until": snap.valid_until.isoformat() if snap.valid_until else None,
        "injection": snap.injection.__dict__ if snap.injection else None,
    }


def _snapshot_from_dict(data: dict[str, Any]) -> SupplierSnapshot:
    if data.get("_schema_version", 1) < _SNAPSHOT_SCHEMA_VERSION:
        raise ValueError(
            "snapshot schema is older than the running integration; "
            "discarding cache so the next refresh re-fetches"
        )
    energy_kind = data["energy_kind"]
    energy_args = data["energy"]
    energy: EnergyRates
    if energy_kind == "fixed":
        energy = FixedRates(**energy_args)
    elif energy_kind == "variable":
        energy = VariableRates(**energy_args)
    elif energy_kind == "dynamic":
        energy = DynamicRates(**energy_args)
    else:
        raise ValueError(f"unknown energy kind {energy_kind!r}")
    injection_data = data.get("injection")
    valid_until_iso = data.get("valid_until")
    valid_until: date | None = None
    if isinstance(valid_until_iso, str):
        try:
            valid_until = date.fromisoformat(valid_until_iso)
        except ValueError:
            valid_until = None
    return SupplierSnapshot(
        supplier=data["supplier"],
        contract=data["contract"],
        energy=energy,
        dsos={k: DsoOverlay(**v) for k, v in data["dsos"].items()},
        taxes=TaxOverlay(**data["taxes"]),
        source_url=data["source_url"],
        fetched_at_iso=data["fetched_at_iso"],
        publication_label=data.get("publication_label", ""),
        valid_until=valid_until,
        injection=InjectionRates(**injection_data) if injection_data else None,
    )


def _energy_kind(energy: EnergyRates) -> str:
    if isinstance(energy, FixedRates):
        return "fixed"
    if isinstance(energy, VariableRates):
        return "variable"
    if isinstance(energy, DynamicRates):
        return "dynamic"
    raise TypeError(f"unknown energy rates type {type(energy).__name__}")
