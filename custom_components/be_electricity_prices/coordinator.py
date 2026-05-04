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
import calendar
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
    State,
)
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
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
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
    VREG_CAPACITY_FLOOR_KW,
)
from .pricing import (
    MeterType,
    PriceBreakdown,
    compute_breakdown,
    is_offpeak,
    static_breakdown,
)
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
    SupplierExtractor,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)


def supplier_device_info(coordinator: "BePricesCoordinator") -> DeviceInfo:
    """Build the HA DeviceInfo block shared by every entity on this entry.

    Both platforms (sensor + binary_sensor) anchor every entity onto the
    same per-entry device, identified by (DOMAIN, entry.entry_id), with
    the supplier label as ``manufacturer``. Centralising it here keeps
    the device-info shape consistent and saves the ~10 lines that used
    to live in each platform's ``__init__``. Falls back to the raw
    supplier id (or a generic label) when the registry lookup fails so
    the entity still surfaces in HA's UI.
    """
    supplier_id = coordinator.entry.data.get(CONF_SUPPLIER, "")
    try:
        supplier_label = get_extractor(str(supplier_id)).label
    except ExtractorError:
        supplier_label = str(supplier_id) or "Belgian Electricity"
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=coordinator.entry.title,
        manufacturer=supplier_label,
        entry_type=None,
    )


# Coordinator probes the supplier on every update tick (UPDATE_INTERVAL_MINUTES);
# SNAPSHOT_REFRESH_HOURS is the fallback TTL for suppliers that have no probe
# path. With a probe, the snapshot stays cached until the probe key changes.
SNAPSHOT_REFRESH_HOURS = 24
SNAPSHOT_STALE_DAYS = 7

# Process-wide snapshot sharing across config entries. Two entries that
# point at the same (supplier, contract, region) share their freshly
# fetched SupplierSnapshot, so we never poll the same PDF twice. Each
# key also has an asyncio.Lock so concurrent first-fetches deduplicate.
_SHARED_SNAPSHOTS_KEY = "snapshot_cache"
_SHARED_LOCKS_KEY = "snapshot_locks"

# Negative cache for fetch failures: when extractor.fetch raises, a
# sibling coordinator on the same (supplier, contract, region) shouldn't
# repeat the same failing network round-trip on the very next tick.
# The stored timestamp is the last failure; siblings skip retrying for
# _SHARED_FAILURE_TTL after that. Long enough to dedupe a tight burst of
# update ticks, short enough that a real recovery is picked up the next
# minute.
_SHARED_FAILED_FETCHES_KEY = "snapshot_failed_fetches"
_SHARED_FAILURE_TTL = timedelta(minutes=5)

# Per-(supplier, contract, region, YYYY-MM) cache of historical snapshots
# the time-correct yearly-cost flow uses to bill each past month at its
# own rate. ``None`` is a negative cache so a probe-less supplier or a
# month outside the supplier's archive horizon doesn't refetch every
# refresh. Lives in-memory only; rebuilt fresh on HA restart.
_MONTHLY_SNAPSHOTS_KEY = "monthly_snapshot_cache"


@dataclass
class _SharedSnapshot:
    snapshot: "SupplierSnapshot"
    fetched_at: datetime
    # Last probe key seen when this snapshot was fetched. ``None`` for
    # suppliers without a probe path - those fall back to the time-based
    # TTL alone.
    probe_key: str | None = None


def _shared_snapshots(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str], _SharedSnapshot]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_SHARED_SNAPSHOTS_KEY, {})  # type: ignore[no-any-return]


def _shared_failed_fetches(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str], tuple[datetime, str]]:
    """Per-key (timestamp, last-error-message) of recent fetch failures.

    Storing the error message alongside the timestamp lets a sibling
    coordinator that hits the negative-cache short-circuit surface the
    real failure reason in its UpdateFailed instead of an opaque
    'cold start'.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_SHARED_FAILED_FETCHES_KEY, {})  # type: ignore[no-any-return]


def evict_shared_caches(
    hass: HomeAssistant, key: tuple[str, str, str], extractor_id: str
) -> None:
    """Drop every shared-cache entry pinned to the given supplier tuple.

    Called from ``async_unload_entry`` once the unloaded entry's
    (supplier, contract, region) is no longer referenced by any other
    loaded entry. Without this, removing the last entry on a given
    tuple leaks the snapshot, the per-month archive cache, the
    failed-fetch marker, and the asyncio.Lock into ``hass.data`` for
    the lifetime of the HA process.
    """
    # Bump the generation counter first so any in-flight cache
    # writer that resumes after this eviction can detect the change
    # and skip its write (the bucket row is gone, so a write would
    # re-create an orphaned row pointing at evicted-tuple data).
    _bump_tuple_generation(hass, key)
    for month_key in list(_monthly_snapshots(hass)):
        if month_key[0] == extractor_id and month_key[1:3] == key[1:3]:
            _bump_tuple_generation(hass, month_key)
    _shared_snapshots(hass).pop(key, None)
    _shared_failed_fetches(hass).pop(key, None)
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str], asyncio.Lock] = bucket.setdefault(
        _SHARED_LOCKS_KEY, {}
    )
    # Only drop the lock when it isn't currently held. If a coroutine
    # is mid-fetch (held lock) and a future entry on the same tuple
    # acquired a fresh lock through ``_shared_lock``, the dedup
    # property would silently break and both coroutines would fan out
    # the same network call. Leaving a locked lock in place defers
    # cleanup to the next eviction; the alternative (cancelling the
    # in-flight fetch) is more invasive than the leak it would
    # prevent.
    held = locks.get(key)
    if held is not None and not held.locked():
        locks.pop(key, None)
    monthly = _monthly_snapshots(hass)
    monthly_locks: dict[tuple[str, str, str, str], asyncio.Lock] = bucket.setdefault(
        _MONTHLY_LOCKS_KEY, {}
    )
    _, contract, region = key
    stale = [
        k
        for k in monthly
        if k[0] == extractor_id and k[1] == contract and k[2] == region
    ]
    for k in stale:
        monthly.pop(k, None)
        held_m = monthly_locks.get(k)
        if held_m is not None and not held_m.locked():
            monthly_locks.pop(k, None)


def _shared_lock(hass: HomeAssistant, key: tuple[str, str, str]) -> asyncio.Lock:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str], asyncio.Lock] = bucket.setdefault(
        _SHARED_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


def _monthly_snapshots(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str, str], "SupplierSnapshot | None"]:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_MONTHLY_SNAPSHOTS_KEY, {})  # type: ignore[no-any-return]


_MONTHLY_LOCKS_KEY = "monthly_snapshot_locks"

# Generation counter bumped by evict_shared_caches when a tuple's
# rows are dropped. Cache writers that may have been awaiting at the
# moment of eviction (held lock, mid-fetch) check the counter on
# resume and skip the write if it has advanced. Without this guard a
# slow fetcher would re-create an orphaned cache row that future
# entries on the same tuple could read as stale data.
_TUPLE_GENERATIONS_KEY = "tuple_generations"


def _tuple_generation(hass: HomeAssistant, key: tuple[str, ...]) -> int:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    gens: dict[tuple[str, ...], int] = bucket.setdefault(_TUPLE_GENERATIONS_KEY, {})
    return gens.get(key, 0)


def _bump_tuple_generation(hass: HomeAssistant, key: tuple[str, ...]) -> None:
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    gens: dict[tuple[str, ...], int] = bucket.setdefault(_TUPLE_GENERATIONS_KEY, {})
    gens[key] = gens.get(key, 0) + 1


def _monthly_lock(hass: HomeAssistant, key: tuple[str, str, str, str]) -> asyncio.Lock:
    """Per-(supplier, contract, region, YYYY-MM) lock used to dedupe
    concurrent fetch_for_month calls. Without it, two coordinators on
    the same supplier tuple racing on first YTD evaluation each fan
    out 12 monthly fetches before either populates _monthly_snapshots."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    locks: dict[tuple[str, str, str, str], asyncio.Lock] = bucket.setdefault(
        _MONTHLY_LOCKS_KEY, {}
    )
    if key not in locks:
        locks[key] = asyncio.Lock()
    return locks[key]


async def _snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
) -> "SupplierSnapshot":
    """Resolve the historical snapshot for ``year_month`` or fall back.

    Caches the result per (supplier, contract, region, YYYY-MM): a hit
    skips the network round-trip on subsequent refreshes. ``None`` is
    cached too -- "supplier doesn't archive this month" is a stable
    signal we shouldn't keep re-asking. The fallback is the current
    snapshot, used as a proxy for non-archive suppliers (OCTA+,
    TotalEnergies, Engie, Luminus, DATS 24, Mega, Bolt).
    """
    cache = _monthly_snapshots(hass)
    cache_key = (
        extractor.id,
        contract,
        region,
        f"{year_month.year:04d}-{year_month.month:02d}",
    )
    if cache_key in cache:
        cached = cache[cache_key]
        return cached if cached is not None else current_snapshot
    fetch_archived = extractor.fetch_for_month
    if fetch_archived is None:
        cache[cache_key] = None
        return current_snapshot
    gen_at_entry = _tuple_generation(hass, cache_key)
    async with _monthly_lock(hass, cache_key):
        # Re-check under the lock so the second waiter doesn't repeat
        # what the first just did.
        if cache_key in cache:
            cached = cache[cache_key]
            return cached if cached is not None else current_snapshot
        fetch_failed = False
        try:
            snap = await fetch_archived(session, contract, region, year_month)
        except Exception as err:  # noqa: BLE001 - per-month fetch must never break the year loop
            _LOGGER.debug(
                "fetch_for_month failed for %s/%s/%s/%s: %s",
                extractor.id,
                contract,
                region,
                cache_key[3],
                err,
            )
            snap = None
            fetch_failed = True
        # Skip the cache write if eviction ran during the await: the
        # tuple is no longer this entry's, and re-creating the row
        # would orphan it for any future re-add of the same tuple.
        # Also skip when the fetch raised: a transient error must not
        # be cached as "supplier doesn't archive this month", which is
        # the meaning a cached None carries here. Leaving the key
        # absent lets the next refresh retry instead of locking in
        # stale "uncredited" output until the entry reloads.
        if not fetch_failed and _tuple_generation(hass, cache_key) == gen_at_entry:
            cache[cache_key] = snap
    return snap if snap is not None else current_snapshot


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
    # Running annual bill in EUR, accumulated day by day from Jan 1.
    # Falls back to the (pro-rated) fees-only floor when no meter
    # sensors are wired. For compensation regime the math nets
    # injection 1:1 against consumption (per-band when bi) and clamps
    # the YTD energy term at zero (Walloon suppliers forfeit surplus
    # injection past consumption); for injection regime each side is
    # multiplied by its own rate and the running total can dip
    # negative when injection credit exceeds consumption + pro-rated
    # fees; for "none" only consumption counts.
    current_year_cost_eur: float | None = None


class BePricesCoordinator(DataUpdateCoordinator[CoordinatorData]):
    """Pull supplier snapshot + ENTSO-E spot, build the hourly price table."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        # Snapshot the (supplier, contract, region) tuple at construction
        # so async_unload_entry can target the *original* tuple even if
        # the user just changed it via OptionsFlow (HA mutates
        # entry.data before triggering the reload).
        self._supplier_tuple: tuple[str, str, str] = (
            entry.data.get(CONF_SUPPLIER, ""),
            entry.data.get(CONF_CONTRACT, ""),
            entry.data.get(CONF_REGION, ""),
        )
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
        self._snapshot_probe_key: str | None = None
        # Set by async_force_refresh; cleared on the next successful
        # extractor fetch. Acts as an out-of-band signal to bypass both
        # the probe-based and TTL-based freshness paths in
        # _self_is_fresh without having to lie about fetched_at -- the
        # latter would block _save_persistent from writing the cached
        # snapshot until the next successful fetch lands.
        self._force_refresh = False
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
        # If the persisted blob was written under a different supplier
        # tuple (typical case: OptionsFlow swap landed while a tick was
        # still in flight, and the slow tick saved over the file after
        # the reload), discard the snapshot so the next refresh
        # repopulates from the correct supplier. The peak/month is
        # supplier-agnostic and stays.
        persisted_tuple = (
            stored.get("entry_supplier"),
            stored.get("entry_contract"),
            stored.get("entry_region"),
        )
        current_tuple = (
            self.entry.data.get(CONF_SUPPLIER),
            self.entry.data.get(CONF_CONTRACT),
            self.entry.data.get(CONF_REGION),
        )
        # A persisted file that predates the entry-tuple keys was likely
        # written for a different supplier/contract/region: better to drop
        # it and let the next refresh repopulate than to serve stale wrong
        # prices on first boot after an OptionsFlow change.
        tuple_mismatch = persisted_tuple != current_tuple
        snap = stored.get("snapshot")
        if isinstance(snap, dict) and not tuple_mismatch:
            try:
                self._snapshot = _snapshot_from_dict(snap)
                self._snapshot_fetched_at = datetime.fromisoformat(snap["_cached_at"])
                cached_probe = snap.get("_probe_key")
                self._snapshot_probe_key = (
                    cached_probe if isinstance(cached_probe, str) else None
                )
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning(
                    "discarding cached snapshot for %s: %s",
                    self.entry.entry_id,
                    err,
                )
                self._snapshot = None
                self._snapshot_fetched_at = None
                self._snapshot_probe_key = None
        elif tuple_mismatch:
            _LOGGER.info(
                "discarding cached snapshot for %s: stored %s differs from "
                "current %s (entry was reconfigured); next refresh will "
                "repopulate",
                self.entry.entry_id,
                persisted_tuple,
                current_tuple,
            )
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
        # Older persisted blobs may carry kwh_buckets / kwh_baselines /
        # year_start / year_start_register_baselines from a previous
        # release that tracked monthly accumulation in-process. Those
        # are unused now: the recorder is the source of truth. Drop
        # them silently on next save.

    async def _async_update_data(self) -> CoordinatorData:
        # Lifecycle note: a slow tick that started before an OptionsFlow
        # change of supplier / contract / region / meter sensors can
        # finish *after* HA's reload swapped self.entry.runtime_data to
        # a fresh coordinator. Any inconsistent intermediate state this
        # tick computes from the now-mutated self.entry.data is
        # contained: _save_persistent skips when runtime_data is no
        # longer this coord, the platforms have been torn down so no
        # entity reads our self.data after the swap, and the
        # async_load_persistent guard discards a blob whose stamped
        # tuple disagrees with the current entry.
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
                self._sync_entsoe_auth_issue(False)
            except EntsoeAuthError as err:
                self._sync_entsoe_auth_issue(True, str(err))
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

        try:
            hourly = self._build_hourly(spot_prices)
        except KeyError as err:
            # The fresh snapshot does not contain the user's configured
            # DSO -- typically a regex drift on a new card. Surface a
            # clean UpdateFailed instead of bubbling KeyError through HA
            # core; the coordinator keeps serving the last good data.
            # Read CONF_DSO defensively: a corrupt entry that lost the
            # key would otherwise re-raise KeyError on the format
            # string and mask the original error.
            raise UpdateFailed(
                f"snapshot missing DSO {self.entry.data.get(CONF_DSO)!r}: {err}"
            ) from err

        capacity_cost = 0.0
        if self.entry.data.get(CONF_REGION) == REGION_FLANDERS:
            capacity_cost = _compute_capacity(self._snapshot, self.entry, self._peak_kw)

        prosumer_cost = _compute_prosumer(self._snapshot, self.entry)
        injection_price = _compute_injection_price(
            self._snapshot, self.entry, spot_prices
        )
        current_year_cost = await _compute_current_year_cost(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self._snapshot,
            self.entry,
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

    def _sync_extractor_failed_issue(self, message: str | None) -> None:
        """Raise or clear the 'supplier extractor failed' repair issue.

        ``message`` is the extractor's error string (re-raised by the
        provider when the supplier's tariff card layout drifted, the
        URL 404'd, or aiohttp timed out). ``None`` means the most
        recent fetch succeeded and any prior issue should be cleared.
        """
        issue_id = f"extractor_failed_{self.entry.entry_id}"
        if message:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="extractor_failed",
                translation_placeholders={
                    "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
                    "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
                    "error": message,
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    def _sync_entsoe_auth_issue(self, active: bool, message: str = "") -> None:
        """Raise or clear the 'ENTSO-E rejected the API key' issue.

        Fired only on ``EntsoeAuthError`` (transparency.entsoe.eu
        responded 401), so the user knows the fix is "rotate the token
        in the entry's options" rather than waiting on a transient
        outage. Cleared as soon as a refresh succeeds with a key the
        endpoint accepts.
        """
        issue_id = f"entsoe_auth_failed_{self.entry.entry_id}"
        if active:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="entsoe_auth_failed",
                translation_placeholders={
                    "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
                    "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
                    "error": message or "401 Unauthorized",
                },
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)

    async def async_force_refresh(self) -> None:
        """Force the next coordinator tick to re-fetch the supplier.

        Invoked by the be_electricity_prices.refresh service when the user
        wants the integration to pick up a new tariff card or correct an
        error without waiting for the 24h refresh tick. Sets a one-shot
        ``_force_refresh`` flag that ``_self_is_fresh`` honours, clears
        the spot cache, the shared snapshot row, and the negative-fetch
        marker so a sibling coordinator on the same (supplier, contract,
        region) tuple also re-fetches on its next refresh. The current
        ``self._snapshot`` and ``_snapshot_fetched_at`` are intentionally
        kept: a transient fetch failure during the forced refresh
        doesn't blank the entry, and ``_save_persistent`` keeps writing
        the cached snapshot so an HA restart between the forced
        refresh and the next successful tick recovers from disk.
        """
        self._force_refresh = True
        self._spot_cache = {}
        self._spot_cache_day = None
        self._spot_cache_includes_tomorrow = False
        key = self._shared_key()
        _shared_snapshots(self.hass).pop(key, None)
        # Clear the negative-fetch marker too, otherwise the next
        # coordinator tick short-circuits inside _SHARED_FAILURE_TTL
        # and the service appears to do nothing.
        _shared_failed_fetches(self.hass).pop(key, None)
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
        self._snapshot_probe_key = shared.probe_key
        self._last_error = ""
        self._force_refresh = False

    async def _maybe_refresh_snapshot(self) -> None:
        """Run a cheap probe; only refetch the full PDF when it says so.

        Two paths depending on what the supplier exposes:

          * **Probe available** — call ``extractor.probe`` (HEAD or small
            listing GET). If the returned key matches what we last saved,
            the snapshot is still valid; just stamp ``_snapshot_fetched_at``
            and return. If the key changed, fall through to a real fetch.

          * **No probe** — fall back to the time-based TTL: only refetch
            when the snapshot is older than ``SNAPSHOT_REFRESH_HOURS`` (24h).
            DATS 24, Engie and Luminus take this path.

        The shared (supplier, contract, region) cache short-circuits the
        same way: a probe-key match against a sibling coordinator's
        snapshot adopts it without doing any work.
        """
        ttl = timedelta(hours=SNAPSHOT_REFRESH_HOURS)
        now = dt_util.utcnow()

        extractor = get_extractor(self.entry.data[CONF_SUPPLIER])
        contract = self.entry.data[CONF_CONTRACT]
        region = self.entry.data[CONF_REGION]
        key = self._shared_key()
        cache = _shared_snapshots(self.hass)

        # Try a cheap probe first. None means the supplier has no probe
        # path or the probe failed; we fall through to the TTL-only flow.
        probe_key: str | None = None
        probe_fn = getattr(extractor, "probe", None)
        if probe_fn is not None:
            try:
                probe_key = await probe_fn(self._session, contract, region)
            except (ExtractorError, asyncio.TimeoutError) as err:
                _LOGGER.debug(
                    "probe failed for %s/%s: %s",
                    self.entry.data.get(CONF_SUPPLIER),
                    contract,
                    err,
                )
                probe_key = None

        # Free, non-blocking shortcut: a sibling coordinator may have a
        # fresh snapshot we can adopt directly.
        shared = cache.get(key)
        if shared is not None and self._shared_is_fresh(shared, probe_key, now, ttl):
            self._adopt_shared(shared)
            return

        # Our own snapshot may already be valid against this probe.
        if self._snapshot is not None and self._self_is_fresh(probe_key, now, ttl):
            if probe_key is not None:
                # Probe verified the supplier hasn't published a new card,
                # so refresh the snapshot_age sensor's clock to "just
                # checked". The probe-less / probe-failed path keeps the
                # original fetched_at; otherwise stamping it on every
                # tick that passes the TTL check resets the TTL clock
                # and the supplier is never re-fetched.
                self._snapshot_fetched_at = now
            # Populate the shared cache when this tick is the first to
            # verify a disk-loaded snapshot after restart. Without this
            # every sibling on the same tuple would re-run its own
            # probe / TTL check on every tick instead of adopting.
            # Re-use the previous probe_key when the current probe
            # came back empty (probe-less suppliers stay None; a
            # transiently-failing probe keeps the last known key).
            if cache.get(key) is None and self._snapshot_fetched_at is not None:
                cache[key] = _SharedSnapshot(
                    snapshot=self._snapshot,
                    fetched_at=self._snapshot_fetched_at,
                    probe_key=probe_key
                    if probe_key is not None
                    else self._snapshot_probe_key,
                )
            return

        # Negative cache: if a sibling just failed on this same key,
        # don't retry until _SHARED_FAILURE_TTL has elapsed. Propagate
        # the sibling's error to ours so a cold-start coordinator sees
        # the real failure reason instead of "cold start".
        failed = _shared_failed_fetches(self.hass)
        last_fail = failed.get(key)
        if (
            last_fail is not None
            and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
        ):
            self._last_error = last_fail[1]
            return

        gen_at_entry = _tuple_generation(self.hass, key)
        async with _shared_lock(self.hass, key):
            shared = cache.get(key)
            if shared is not None and self._shared_is_fresh(
                shared, probe_key, dt_util.utcnow(), ttl
            ):
                self._adopt_shared(shared)
                return
            # Re-check the negative cache under the lock so the second
            # waiter doesn't repeat what the first just failed.
            last_fail = failed.get(key)
            if (
                last_fail is not None
                and dt_util.utcnow() - last_fail[0] < _SHARED_FAILURE_TTL
            ):
                self._last_error = last_fail[1]
                return
            try:
                snap = await extractor.fetch(self._session, contract, region)
                fetched_at = dt_util.utcnow()
                # Don't write the shared cache if the tuple was evicted
                # mid-fetch (entry removed or supplier swapped). Our
                # local self._snapshot is still useful for this tick;
                # if runtime_data was swapped, _save_persistent will
                # skip the write.
                if _tuple_generation(self.hass, key) == gen_at_entry:
                    cache[key] = _SharedSnapshot(
                        snapshot=snap, fetched_at=fetched_at, probe_key=probe_key
                    )
                    failed.pop(key, None)
                self._snapshot = snap
                self._snapshot_fetched_at = fetched_at
                self._snapshot_probe_key = probe_key
                self._last_error = ""
                self._force_refresh = False
                self._sync_extractor_failed_issue(None)
            except Exception as err:
                # Any extractor failure (including unexpected aiohttp /
                # parser exceptions) must populate the negative cache so
                # sibling coordinators back off instead of refiring the
                # same broken request on the next tick.
                if _tuple_generation(self.hass, key) == gen_at_entry:
                    failed[key] = (dt_util.utcnow(), str(err))
                self._last_error = str(err)
                self._sync_extractor_failed_issue(str(err))
                _LOGGER.warning(
                    "snapshot refresh failed for %s/%s: %s; keeping cached",
                    self.entry.data.get(CONF_SUPPLIER),
                    self.entry.data.get(CONF_CONTRACT),
                    err,
                )
                if not isinstance(err, (ExtractorError, asyncio.TimeoutError)):
                    raise

    def _self_is_fresh(
        self, probe_key: str | None, now: datetime, ttl: timedelta
    ) -> bool:
        """Whether our own snapshot can be reused without a refetch."""
        if self._force_refresh:
            return False
        if probe_key is not None:
            return self._snapshot_probe_key == probe_key
        if self._snapshot_fetched_at is None:
            return False
        return now - self._snapshot_fetched_at < ttl

    @staticmethod
    def _shared_is_fresh(
        shared: _SharedSnapshot,
        probe_key: str | None,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        """Whether a sibling's shared snapshot can be adopted as-is."""
        if probe_key is not None:
            return shared.probe_key == probe_key
        return now - shared.fetched_at < ttl

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
        dso_mode = self.entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)

        hourly: dict[datetime, PriceBreakdown] = {}
        if isinstance(snap.energy, DynamicRates):
            for utc_hour, spot in spot_prices.items():
                local = dt_util.as_local(utc_hour)
                hourly[utc_hour] = compute_breakdown(
                    snap, dso, region, local, spot, meter, dso_mode
                )
            return hourly

        # Iterate in UTC for 48 contiguous slots so a DST seam preserves
        # the wall-clock gap correctly. Spring-forward shifts one of the
        # day's local hours into the next UTC slot (so today carries 23
        # local hours, tomorrow 25); fall-back is the mirror. Naively
        # walking local-time + timedelta would either collide two hours
        # into one UTC slot (spring) or duplicate a UTC slot (fall) and
        # silently drop one breakdown.
        # Anchor at local midnight (converted to UTC) so today_min /
        # today_max / today_average cover the full local day rather
        # than "now → midnight".
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
        # Identity guard: a slow tick that started before the user
        # changed supplier/contract/region via OptionsFlow can finish
        # after the reload has already swapped runtime_data to a fresh
        # coordinator instance. If we wrote the file unconditionally,
        # the obsolete coord would clobber the new coord's saved state
        # and the next HA restart would serve the wrong supplier's
        # rates against the new entry. ``runtime_data`` is unset (or
        # UNDEFINED on recent HA cores) during the very first refresh
        # that runs from ``async_config_entry_first_refresh`` -- only
        # skip the save when it has been explicitly assigned to a
        # *different* coordinator.
        runtime = getattr(self.entry, "runtime_data", None)
        if isinstance(runtime, BePricesCoordinator) and runtime is not self:
            _LOGGER.debug(
                "skipping _save_persistent for %s: coordinator was replaced",
                self.entry.entry_id,
            )
            return
        payload: dict[str, Any] = {
            # Stamp the snapshot's actual provenance (the tuple this
            # coordinator was constructed under) so the load path can
            # refuse a blob written under a different supplier tuple.
            # Reading entry.data here would race with OptionsFlow:
            # async_update_entry mutates entry.data before the reload
            # listener swaps runtime_data, so a slow tick that resumes
            # in that window would stamp the new tuple over the old
            # snapshot and the next HA boot would adopt it as fresh.
            "entry_supplier": self._supplier_tuple[0],
            "entry_contract": self._supplier_tuple[1],
            "entry_region": self._supplier_tuple[2],
            "peak": {
                "kw": self._peak_kw,
                "month": self._peak_month.isoformat() if self._peak_month else "",
            },
        }
        if self._snapshot is not None and self._snapshot_fetched_at is not None:
            payload["snapshot"] = _snapshot_to_dict(
                self._snapshot,
                self._snapshot_fetched_at,
                self._snapshot_probe_key,
            )
        await self._store.async_save(payload)


def _compute_capacity(
    snapshot: SupplierSnapshot, entry: ConfigEntry, peak_kw: float
) -> float:
    # Read CONF_DSO defensively: a corrupt entry that lost the key
    # would otherwise KeyError here and tear the whole tick down via
    # UpdateFailed. _compute_prosumer already takes the same shape.
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0
    overlay = snapshot.dsos.get(dso)
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
    # Formula-based injection (factor x spot + base): contract is
    # dynamic, so the static "current" indicator is the wrong answer
    # when ENTSO-E hasn't given us a spot yet. Return None so the
    # injection_price sensor goes unknown until the next refresh
    # picks up real spots, instead of fabricating a value from the
    # supplier's monthly indicative.
    if inj.factor is not None and inj.base is not None:
        if not spot_prices:
            return None
        now_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        spot = spot_prices.get(now_hour)
        if spot is None:
            nearest = min(
                spot_prices.keys(),
                key=lambda h: abs((h - now_hour).total_seconds()),
            )
            if abs((nearest - now_hour).total_seconds()) > 3600:
                return None
            spot = spot_prices[nearest]
        return inj.factor * spot + inj.base
    # Static contracts: the supplier's printed monthly indicative.
    return inj.current


def _historical_injection_rate(
    injection: InjectionRates | None, spot: float | None = None
) -> float | None:
    """Best-effort EUR/kWh injection rate for a *past* hour.

    Static contracts publish a monthly indicative (``current``); use it.
    Dynamic-injection contracts publish only ``factor*spot + base`` — if
    the caller has the historical spot, compose; otherwise we don't have
    enough to price the hour exactly, so leave it uncredited rather than
    fabricating a rate from a different field. Symmetric across the TOU
    and static YTD paths so both report the same number for the same
    hour.
    """
    if injection is None:
        return None
    if injection.current is not None:
        return injection.current
    if injection.factor is not None and injection.base is not None and spot is not None:
        return injection.factor * spot + injection.base
    return None


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


async def _recorder_rows(
    hass: HomeAssistant, entity_id: str, start: date, end: date, period: str
) -> list[Any]:
    """Fetch HA recorder ``change`` rows for ``entity_id`` over ``[start, end]``.

    Wraps ``statistics_during_period`` via the recorder's executor so a
    SQLite query never runs on the event loop. Returns a (possibly
    empty) list -- every failure mode (recorder not ready, no
    statistics, transient DB error) collapses to ``[]`` so callers can
    fall back to the fees-only floor without raising.

    Reads the ``change`` field, which the recorder defines as the delta
    of the cumulative ``sum`` between the bucket's first and last
    sample. Reading ``sum`` directly would yield the all-time running
    total -- summing those would multiply the bill by however many
    years of statistics the meter has accumulated.

    Pass the date directly: HA's start_of_local_day treats a naive
    datetime as UTC, which round-trips correctly only for tz east of
    the prime meridian. Hand it the date so the function takes its
    date-typed branch and produces the unambiguous local midnight.
    """
    try:
        # mypy --strict flags both names because the recorder module
        # does not re-export them via __all__; they're public per HA's
        # docs and import-time errors degrade gracefully via the
        # ImportError handler below.
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        return []
    start_dt = dt_util.start_of_local_day(start).astimezone(UTC)
    # +1 day so the bucket containing ``end`` is included.
    end_dt = dt_util.start_of_local_day(end).astimezone(UTC) + timedelta(days=1)
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start_dt,
            end_dt,
            {entity_id},
            period,
            None,
            {"change"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return []
    rows: list[Any] = list(stats.get(entity_id, []))
    return rows


async def _recorder_daily_kwh(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[date, float]:
    """Per-day kWh deltas for ``entity_id`` keyed by local-day date."""
    out: dict[date, float] = {}
    for row in await _recorder_rows(hass, entity_id, start, end, "day"):
        ts = row.get("start")
        delta = row.get("change")
        if ts is None or delta is None:
            continue
        local_day = dt_util.as_local(datetime.fromtimestamp(ts, tz=UTC)).date()
        out[local_day] = float(delta)
    return out


async def _recorder_hourly_kwh(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[datetime, float]:
    """Per-hour kWh deltas for ``entity_id`` keyed by UTC hour.

    Used by the TOU year-cost path: TOU contracts have a different
    energy rate per hour-of-day, so day-level granularity is too coarse.
    """
    out: dict[datetime, float] = {}
    for row in await _recorder_rows(hass, entity_id, start, end, "hour"):
        ts = row.get("start")
        delta = row.get("change")
        if ts is None or delta is None:
            continue
        utc_hour = datetime.fromtimestamp(ts, tz=UTC).replace(
            minute=0, second=0, microsecond=0
        )
        out[utc_hour] = float(delta)
    return out


async def _recorder_daily_band_ratio(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[date, tuple[float, float]]:
    """Per-day (day_ratio, night_ratio) for ``entity_id``.

    Used for the totals-only + bi-hourly path: we don't have separate
    day / night registers, so we recover the band split from hourly
    statistics by binning each hour on ``is_offpeak``. The two ratios
    sum to 1.0 (or default to a day-of-week split for days with no
    accumulation, so a Sunday isn't billed at peak rate just because
    the hourly stats are flat).
    """
    per_day_day: dict[date, float] = {}
    per_day_night: dict[date, float] = {}
    for row in await _recorder_rows(hass, entity_id, start, end, "hour"):
        ts = row.get("start")
        delta = row.get("change")
        if ts is None or delta is None:
            continue
        local = dt_util.as_local(datetime.fromtimestamp(ts, tz=UTC))
        bucket = local.date()
        if is_offpeak(local):
            per_day_night[bucket] = per_day_night.get(bucket, 0.0) + float(delta)
        else:
            per_day_day[bucket] = per_day_day.get(bucket, 0.0) + float(delta)
    out: dict[date, tuple[float, float]] = {}
    for day in set(per_day_day) | set(per_day_night):
        d = per_day_day.get(day, 0.0)
        n = per_day_night.get(day, 0.0)
        total = d + n
        if total > 0:
            out[day] = (d / total, n / total)
        else:
            out[day] = _default_band_ratio_for(day)
    return out


async def _resolve_daily_kwh(
    hass: HomeAssistant, entry: ConfigEntry, today: date
) -> dict[date, tuple[float, float, float, float]] | None:
    """Per-day (day_cons, night_cons, day_inj, night_inj) from recorder.

    Each side (consumption, injection) is resolved independently from
    one of three configurations:

      * **Day + night register pair** (``CONF_DAY_*_KWH`` +
        ``CONF_NIGHT_*_KWH``): the recorder gives one delta per day per
        register, fanned out into the corresponding band slots.

      * **Single totals sensor** (``CONF_CONSUMPTION_KWH`` /
        ``CONF_INJECTION_KWH``): one daily total per side, split by
        the ``meter`` setting (mono keeps everything in the "day" slot
        and lets the math sum it; bi/dynamic recovers the per-day
        band ratio from hourly statistics binned on ``is_offpeak``).

      * **Nothing**: that side contributes zero.

    A side that has only one half of its register pair (e.g.
    ``CONF_DAY_CONSUMPTION_KWH`` set, ``CONF_NIGHT_CONSUMPTION_KWH``
    missing) returns ``None`` so the caller falls back to the
    fees-only floor instead of silently undercounting the missing
    band.

    Returns ``None`` when neither side has any meter inputs at all
    or when either side has a partial register wiring.
    """
    meter = entry.data.get(CONF_METER, METER_MONO)
    jan1 = date(today.year, 1, 1)
    out: dict[date, list[float]] = {}

    async def _side(
        day_id: str | None,
        night_id: str | None,
        total_id: str | None,
        slot_day: int,
        slot_night: int,
    ) -> bool:
        """Resolve one side (consumption or injection) into ``out``.

        Returns False when this side has a partial register wiring
        (caller surfaces the fees-only floor); True otherwise.
        """
        if bool(day_id) ^ bool(night_id):
            return False
        if day_id and night_id:
            for day, kwh in (
                await _recorder_daily_kwh(hass, day_id, jan1, today)
            ).items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += kwh
            for day, kwh in (
                await _recorder_daily_kwh(hass, night_id, jan1, today)
            ).items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_night] += kwh
            return True
        if not total_id:
            return True  # nothing wired on this side; contributes zero
        per_day = await _recorder_daily_kwh(hass, total_id, jan1, today)
        if meter in ("bi", "dynamic"):
            ratios = await _recorder_daily_band_ratio(hass, total_id, jan1, today)
            for day, total in per_day.items():
                d_ratio, n_ratio = ratios.get(day, _default_band_ratio_for(day))
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += total * d_ratio
                row[slot_night] += total * n_ratio
        else:  # mono: route everything into the "day" slot
            for day, total in per_day.items():
                row = out.setdefault(day, [0.0, 0.0, 0.0, 0.0])
                row[slot_day] += total
        return True

    cons_ok = await _side(
        entry.data.get(CONF_DAY_CONSUMPTION_KWH),
        entry.data.get(CONF_NIGHT_CONSUMPTION_KWH),
        entry.data.get(CONF_CONSUMPTION_KWH),
        slot_day=0,
        slot_night=1,
    )
    inj_ok = await _side(
        entry.data.get(CONF_DAY_INJECTION_KWH),
        entry.data.get(CONF_NIGHT_INJECTION_KWH),
        entry.data.get(CONF_INJECTION_KWH),
        slot_day=2,
        slot_night=3,
    )
    if not (cons_ok and inj_ok):
        return None
    if not out:
        return None

    return {day: (r[0], r[1], r[2], r[3]) for day, r in out.items()}


def _days_through(start: date, end: date) -> list[date]:
    """Inclusive list of dates from ``start`` to ``end`` (local calendar)."""
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days


def _default_band_ratio_for(day: date) -> tuple[float, float]:
    """Time-weighted (day_ratio, night_ratio) fallback for a day with no
    hourly recorder stats yet.

    Assumes uniform consumption across the day's 24 hours (the most
    neutral guess without a usage profile). Weekends and federal
    holidays are entirely offpeak under the Belgian bi-hourly schedule;
    weekdays split 15h peak / 9h offpeak. Replaces a previous hardcoded
    (1.0, 0.0) default that systematically pushed totals into the peak
    band when hourly stats lagged daily stats."""
    # Construct each local clock hour directly instead of advancing an
    # aware datetime by a fixed UTC timedelta: the latter shifts by one
    # hour on each DST transition, mislabelling one hour twice a year.
    # is_offpeak only reads the local hour + weekday, both of which are
    # well-defined per local clock hour even on DST days.
    peak_hours = 0
    for hour in range(24):
        when = datetime(
            day.year,
            day.month,
            day.day,
            hour,
            tzinfo=dt_util.DEFAULT_TIME_ZONE,
        )
        if not is_offpeak(when):
            peak_hours += 1
    if peak_hours == 0:
        return (0.0, 1.0)
    return (peak_hours / 24.0, (24 - peak_hours) / 24.0)


async def _walk_ytd_months(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
) -> AsyncIterator[tuple[SupplierSnapshot, date, int, int]]:
    """Yield ``(snap_m, month_first, days_in_full_month, days_in_ytd)``
    for each month from Jan 1 of today's year up through today.

    Centralises the per-month walk shared by every YTD accumulator so
    the proration formula and the per-month archive lookup stay in one
    place. ``snap_m`` falls back to the current snapshot for months
    with no archive (see :func:`_snapshot_for_month`).

    ``contract`` overrides the entry's stored contract id; the
    OptionsFlow compare path uses this to walk months for an
    alternative supplier without mutating the live entry.
    """
    region = entry.data.get(CONF_REGION, "")
    contract = contract or entry.data[CONF_CONTRACT]
    cur = date(today.year, 1, 1)
    while cur <= today:
        month_first = date(cur.year, cur.month, 1)
        snap_m = await _snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot
        )
        if cur.month == 12:
            next_first = date(cur.year + 1, 1, 1)
        else:
            next_first = date(cur.year, cur.month + 1, 1)
        days_in_full_month = (next_first - month_first).days
        month_end_in_ytd = min(next_first - timedelta(days=1), today)
        days_in_ytd = (month_end_in_ytd - cur).days + 1
        yield snap_m, month_first, days_in_full_month, days_in_ytd
        cur = next_first


async def _ytd_static_fees(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
) -> float:
    """Pro-rated YTD total of yearly_fixed_fee + 12*energy_fund using each
    month's archived snapshot.

    Uses the uniform days_in_year proration but reads the rate from the
    archived snapshot for each past month, so a supplier indexation
    that lands mid-year is honoured for the months it applies to.
    Falls back to the current snapshot for months with no archive.
    """
    days_in_year = 366 if calendar.isleap(today.year) else 365
    total = 0.0
    async for snap_m, _, _, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        annual = (
            getattr(snap_m.energy, "yearly_fixed_fee", 0.0)
            + snap_m.taxes.energy_fund_eur_per_month * 12.0
        )
        total += annual * (days_in_ytd / days_in_year)
    return total


async def _ytd_prosumer(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
) -> float:
    """Sum the monthly prosumer fee across YTD using each month's archived
    snapshot's DSO overlay, so a CWaPE indexation that lands mid-year is
    honoured for the months it applies to."""
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_COMPENSATION:
        return 0.0
    try:
        kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if kva <= 0.0:
        return 0.0
    dso = entry.data.get(CONF_DSO, "")

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        overlay = snap_m.dsos.get(dso)
        if overlay is None or overlay.prosumer_eur_per_kva_year is None:
            continue
        monthly_fee = kva * overlay.prosumer_eur_per_kva_year / 12.0
        total += monthly_fee * (days_in_ytd / days_in_full_month)
    return total


def _hourly_consumption_sensors(entry: ConfigEntry) -> list[str]:
    """Recorder entity ids whose hourly kWh sums add up to total
    consumption.

    Prefer the single totals sensor when wired; otherwise require the
    full day + night register pair (both halves) so a partial wiring
    can't silently undercount the night band. Returns an empty list
    when nothing is wired or only one register half is wired (caller
    surfaces the fees-only floor in that case).
    """
    total = entry.data.get(CONF_CONSUMPTION_KWH)
    if total:
        return [total]
    day = entry.data.get(CONF_DAY_CONSUMPTION_KWH)
    night = entry.data.get(CONF_NIGHT_CONSUMPTION_KWH)
    if day and night:
        return [day, night]
    return []


def _hourly_injection_sensors(entry: ConfigEntry) -> list[str]:
    """Mirror of ``_hourly_consumption_sensors`` for the injection side.

    Returns an empty list when neither a totals sensor nor the full
    day+night pair is wired, so a partial register wiring doesn't get
    counted as injection coverage."""
    total = entry.data.get(CONF_INJECTION_KWH)
    if total:
        return [total]
    day = entry.data.get(CONF_DAY_INJECTION_KWH)
    night = entry.data.get(CONF_NIGHT_INJECTION_KWH)
    if day and night:
        return [day, night]
    return []


async def _ytd_tou_energy(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
    meter: MeterType | None = None,
) -> float | None:
    """YTD energy cost for a Time-of-Use contract, billed per hour.

    TOU energy rates change with the hour of day, so the per-day path
    used for fixed/variable contracts is too coarse. This helper bins
    the recorder's hourly kWh deltas through ``compute_breakdown`` at
    each local hour, picking up the TOU slot rate from the supplier and
    the bi-hourly distribution band from the user's DSO mode in one
    call. Reads from ``CONF_CONSUMPTION_KWH`` (single totals) when
    available, else sums the four day/night register sensors at hourly
    granularity. Each side -- consumption, injection -- is resolved
    independently, mirroring the static-path behaviour: a user with
    only injection wired (e.g. an inverter exposing solar export but
    no smart-meter consumption sensor) still gets the injection
    credit recognised. Returns ``None`` only when neither side has
    any meters wired (the caller surfaces the fees-only floor).
    """
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    contract = contract or entry.data[CONF_CONTRACT]
    meter = meter or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    cons_ids = _hourly_consumption_sensors(entry)
    inj_ids = _hourly_injection_sensors(entry)
    if not cons_ids and not inj_ids:
        return None

    jan1 = date(today.year, 1, 1)
    cons_per_hour: dict[datetime, float] = {}
    for cid in cons_ids:
        for k, v in (await _recorder_hourly_kwh(hass, cid, jan1, today)).items():
            cons_per_hour[k] = cons_per_hour.get(k, 0.0) + v
    inj_per_hour: dict[datetime, float] = {}
    for iid in inj_ids:
        for k, v in (await _recorder_hourly_kwh(hass, iid, jan1, today)).items():
            inj_per_hour[k] = inj_per_hour.get(k, 0.0) + v

    month_snap_cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in month_snap_cache:
            month_snap_cache[month_first] = await _snapshot_for_month(
                hass, session, extractor, contract, region, month_first, snapshot
            )
        return month_snap_cache[month_first]

    energy_cost = 0.0
    # Iterate the union of both sides so an injection-only wiring
    # still contributes its credit (mirroring _resolve_daily_kwh).
    for utc_hour in cons_per_hour.keys() | inj_per_hour.keys():
        local = dt_util.as_local(utc_hour)
        snap_h = await _snap_for(date(local.year, local.month, 1))
        try:
            bd = compute_breakdown(snap_h, dso, region, local, None, meter, dso_mode)
        except (KeyError, ValueError):
            # Missing DSO row or non-static rate kind: skip this hour.
            continue
        kwh_cons = cons_per_hour.get(utc_hour, 0.0)
        kwh_inj = inj_per_hour.get(utc_hour, 0.0)
        if regime == SOLAR_REGIME_COMPENSATION:
            d_cost = (kwh_cons - kwh_inj) * bd.all_in
        elif regime == SOLAR_REGIME_INJECTION:
            d_cost = kwh_cons * bd.all_in
            inj_rate = _historical_injection_rate(snap_h.injection)
            if inj_rate is not None:
                d_cost -= kwh_inj * inj_rate
        else:
            d_cost = kwh_cons * bd.all_in
        energy_cost += d_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        energy_cost = max(energy_cost, 0.0)
    return energy_cost


async def _compute_current_year_cost(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    contract_override: str | None = None,
    meter_override: MeterType | None = None,
) -> float | None:
    """Time-correct yearly bill from HA recorder + per-month tariff cards.

    For every day from Jan 1 of the current local year up to today,
    pull that day's kWh from the recorder and multiply by the tariff
    of the month the day belongs to (archived snapshot when the
    supplier exposes one, else the current snapshot as a proxy).
    Per-day kWh × per-day tariff handles tariff transitions inside a
    month (e.g. the supplier rotates a monthly card mid-month) without
    re-querying the recorder, and matches what the user reads on a
    smart meter day by day.

    Math per day, after looking up the snapshot for that day's month:

      regime=none, mono : (d_cons + n_cons) * single
      regime=none, bi   : d_cons * peak + n_cons * offpeak
      regime=injection,
        mono : (d_cons + n_cons) * single - (d_inj + n_inj) * inj_m
      regime=injection,
        bi   : d_cons * peak + n_cons * offpeak
               - (d_inj + n_inj) * inj_m
      regime=compensation, mono :
               (d_cons + n_cons - d_inj - n_inj) * single
      regime=compensation, bi :
               (d_cons - d_inj) * peak + (n_cons - n_inj) * offpeak

    Compensation netting happens once over the YTD total at the end
    (clamped at zero), matching how the Walloon annual meter readout
    actually settles -- a day of over-injection can offset a later day
    of higher consumption.

    Plus fees: the supplier yearly fixed fee and the Flemish energy
    fund are summed per archived month using each month's snapshot
    (so a supplier indexation that lands mid-year is honoured for the
    months it applies to), pro-rated by ``days_in_month_in_ytd /
    days_in_year`` so the YTD total still grows uniformly across the
    calendar year. The Walloon prosumer fee follows the same per-month
    walk against each month's DSO overlay. The running bill grows day
    by day instead of jumping to the full annual on Jan 1.

    ``inj_m`` is each month's snapshot's ``injection.current`` (the
    printed monthly indicative).

    **Time-of-Use contracts** (Engie Empower Flextime, Luminus
    SmartFlex) take a per-hour path: the recorder's hourly kWh deltas
    are billed against ``compute_breakdown`` at each local hour, so
    the energy component picks the supplier's TOU slot rate while the
    network component still follows the user's DSO mode. Reads either
    ``CONF_CONSUMPTION_KWH`` (single totals) or the day+night register
    pair via the recorder's hourly statistics; partial register
    wiring is rejected so a missing band can't silently undercount.

    **Dynamic contracts** (Cociter Dynamique, Eneco Power Dynamic,
    OCTA+ Dynamic, etc.) need historical hourly ENTSO-E spots to bill
    correctly. v1 does not replay historical spots, so the energy
    component is reported as 0 and the sensor surfaces the pro-rated
    fees-only floor. The live ``current_price`` sensor is unaffected.

    Returns ``None`` only when there is no meter input wired at all
    AND no snapshot to show fees against. In every other case the
    function returns a number, falling back to the fees-only floor
    rather than exposing ``unknown`` to the user.
    """
    today = dt_util.now().date()
    # contract / meter overrides let the OptionsFlow's compare path run
    # this same engine against an alternative supplier's snapshot
    # without mutating the live entry. The user's region / DSO / regime /
    # solar_kva always come from the entry: those are the user's setup,
    # not the alternative's.
    contract = contract_override or entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    meter = meter_override or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    jan1 = date(today.year, 1, 1)

    static_fees = await _ytd_static_fees(
        hass, session, extractor, snapshot, entry, today, contract=contract
    )
    prosumer_ytd = await _ytd_prosumer(
        hass, session, extractor, snapshot, entry, today, contract=contract
    )
    fees = static_fees + prosumer_ytd

    # Dynamic contracts need historical hourly ENTSO-E spots that v1 does
    # not replay; bail with the fees-only floor *before* iterating hours.
    # Otherwise the TOU loop would silently swallow ValueError on every
    # hour (compute_breakdown rejects DynamicRates without a spot) and
    # the static loop would discard every month (static_breakdown returns
    # None for DynamicRates) — same result, but a wasted recorder pass
    # and an inconsistency for users who picked dso_mode=impact.
    if isinstance(snapshot.energy, DynamicRates):
        return fees

    # Per-hour billing is required when the supplier's energy rates
    # vary by hour (TOU contracts) AND when the DSO bills per Impact
    # band (PIC / MEDIUM / ECO change with hour-of-day). Both go
    # through the same hourly path; the static per-day branch can't
    # represent either.
    needs_hourly = (
        isinstance(snapshot.energy, TimeOfUseRates) or dso_mode == DSO_MODE_IMPACT
    )
    if needs_hourly:
        hourly_energy = await _ytd_tou_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
        )
        if hourly_energy is None:
            return fees
        return hourly_energy + fees

    daily_kwh = await _resolve_daily_kwh(hass, entry, today)
    if daily_kwh is None:
        # No meter inputs at all - fees-only floor.
        return fees

    # Precompute the snapshot + breakdowns for each month touched, so
    # the per-day loop stays O(days) without repeating the breakdown
    # math for every day in a month.
    month_breakdowns: dict[date, tuple[Any, Any, Any, "SupplierSnapshot"] | None] = {}

    async def _resolve_month(
        month_first: date,
    ) -> tuple[Any, Any, Any, "SupplierSnapshot"] | None:
        if month_first in month_breakdowns:
            return month_breakdowns[month_first]
        snap_m = await _snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot
        )
        try:
            single_bd = static_breakdown(snap_m, dso, region, "single", dso_mode)
            peak_bd = static_breakdown(snap_m, dso, region, "peak", dso_mode)
            offpeak_bd = static_breakdown(snap_m, dso, region, "offpeak", dso_mode)
        except KeyError:
            # An archived snapshot can lose the user's DSO key when the
            # supplier renames a row or a regex misses for that month.
            # Treating the month as "no rate to apply" matches dynamic
            # / TOU behaviour and keeps the YTD loop running instead of
            # tearing the whole tick down with UpdateFailed.
            _LOGGER.debug(
                "static_breakdown missing DSO %s for %s/%s/%s; falling back",
                dso,
                snap_m.supplier,
                snap_m.contract,
                month_first,
            )
            month_breakdowns[month_first] = None
            return None
        if single_bd is None or peak_bd is None or offpeak_bd is None:
            month_breakdowns[month_first] = None
            return None
        bundle = (single_bd, peak_bd, offpeak_bd, snap_m)
        month_breakdowns[month_first] = bundle
        return bundle

    energy_cost = 0.0
    for day in _days_through(jan1, today):
        bundle = await _resolve_month(date(day.year, day.month, 1))
        if bundle is None:
            # Dynamic / TOU month: no stable rate to apply for any of
            # its days.
            continue
        single_bd, peak_bd, offpeak_bd, snap_d = bundle

        d_cons, n_cons, d_inj, n_inj = daily_kwh.get(day, (0.0, 0.0, 0.0, 0.0))
        total_cons = d_cons + n_cons
        total_inj = d_inj + n_inj

        bi_capable = meter in ("bi", "dynamic")
        if regime == SOLAR_REGIME_COMPENSATION:
            if bi_capable:
                d_cost = (d_cons - d_inj) * peak_bd.all_in + (
                    n_cons - n_inj
                ) * offpeak_bd.all_in
            else:
                d_cost = (total_cons - total_inj) * single_bd.all_in
        elif regime == SOLAR_REGIME_INJECTION:
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in
            inj_rate = _historical_injection_rate(snap_d.injection)
            if inj_rate is not None:
                d_cost -= total_inj * inj_rate
        else:  # none
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in

        energy_cost += d_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        # YTD clamp at zero: the bill never goes negative, surplus
        # injection past consumption is forfeited (by most Walloon
        # suppliers).
        energy_cost = max(energy_cost, 0.0)

    return energy_cost + fees


# ---- snapshot serialization for the HA Store ----------------------------------


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
_SNAPSHOT_SCHEMA_VERSION = 7


def _snapshot_to_dict(
    snap: SupplierSnapshot, fetched_at: datetime, probe_key: str | None = None
) -> dict[str, Any]:
    return {
        "_cached_at": fetched_at.isoformat(),
        "_probe_key": probe_key,
        "_schema_version": _SNAPSHOT_SCHEMA_VERSION,
        "supplier": snap.supplier,
        "contract": snap.contract,
        "energy_kind": _energy_kind(snap.energy),
        "energy": snap.energy.__dict__,
        "dsos": {k: v.__dict__ for k, v in snap.dsos.items()},
        "taxes": snap.taxes.__dict__,
        "source_url": snap.source_url,
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
    elif energy_kind == "tou":
        energy = TimeOfUseRates(**energy_args)
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
    if isinstance(energy, TimeOfUseRates):
        return "tou"
    raise TypeError(f"unknown energy rates type {type(energy).__name__}")
