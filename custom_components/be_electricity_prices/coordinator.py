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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
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
    CONF_CONTRACT,
    CONF_DSO,
    CONF_METER,
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
from .pricing import PriceBreakdown, compute_breakdown
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


@dataclass
class CoordinatorData:
    """Snapshot the coordinator hands to entities."""

    hourly: dict[datetime, PriceBreakdown] = field(default_factory=dict)
    snapshot_publication: str = ""
    snapshot_age_hours: float = 0.0
    snapshot_stale: bool = False
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

    async def _async_update_data(self) -> CoordinatorData:
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

        await self._save_persistent()

        age = self._snapshot_age_hours()
        stale = age > SNAPSHOT_STALE_DAYS * 24
        self._sync_stale_issue(stale)
        return CoordinatorData(
            hourly=hourly,
            snapshot_publication=self._snapshot.publication_label,
            snapshot_age_hours=age,
            snapshot_stale=stale,
            last_error=self._last_error,
            monthly_peak_kw=self._peak_kw,
            monthly_peak_month=self._peak_month,
            capacity_cost_eur=capacity_cost,
            prosumer_cost_eur=prosumer_cost,
            injection_price_eur_per_kwh=injection_price,
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
        error without waiting for the 24h refresh tick.
        """
        self._snapshot_fetched_at = None
        self._spot_cache = {}
        self._spot_cache_day = None
        self._spot_cache_includes_tomorrow = False
        await self.async_request_refresh()

    async def _maybe_refresh_snapshot(self) -> None:
        if self._snapshot_fetched_at and (
            dt_util.utcnow() - self._snapshot_fetched_at
            < timedelta(hours=SNAPSHOT_REFRESH_HOURS)
        ):
            return
        try:
            extractor = get_extractor(self.entry.data[CONF_SUPPLIER])
            snap = await extractor.fetch(
                self._session,
                self.entry.data[CONF_CONTRACT],
                self.entry.data[CONF_REGION],
            )
            self._snapshot = snap
            self._snapshot_fetched_at = dt_util.utcnow()
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

        hourly: dict[datetime, PriceBreakdown] = {}
        if isinstance(snap.energy, DynamicRates):
            for utc_hour, spot in spot_prices.items():
                local = dt_util.as_local(utc_hour)
                hourly[utc_hour] = compute_breakdown(
                    snap, dso, region, local, spot, meter
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
            hourly[utc] = compute_breakdown(snap, dso, region, local, None, meter)
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
            }
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


# ---- snapshot serialization for the HA Store ----------------------------------


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
_SNAPSHOT_SCHEMA_VERSION = 4


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
    return SupplierSnapshot(
        supplier=data["supplier"],
        contract=data["contract"],
        energy=energy,
        dsos={k: DsoOverlay(**v) for k, v in data["dsos"].items()},
        taxes=TaxOverlay(**data["taxes"]),
        source_url=data["source_url"],
        fetched_at_iso=data["fetched_at_iso"],
        publication_label=data.get("publication_label", ""),
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
