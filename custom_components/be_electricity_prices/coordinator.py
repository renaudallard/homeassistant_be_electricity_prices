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
serve last-known prices. The coordinator ticks hourly
(UPDATE_INTERVAL_MINUTES): each tick runs the supplier's cheap freshness
probe and only re-fetches the full card when the probe key changes, while
probe-less suppliers fall back to the SNAPSHOT_REFRESH_HOURS (24h) TTL. Per
the project's fail policy, if a refresh fails the coordinator keeps serving
the cached snapshot and surfaces a repair issue.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import partial
from statistics import fmean
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfEnergy,
)
from homeassistant.core import (
    HomeAssistant,
    State,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import EntsoeAuthError, EntsoeClient, EntsoeError
from .const import (
    CAPACITY_MODE_FIXED,
    CAPACITY_MODE_SENSOR,
    CONF_API_KEY,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_CONNECTION_KVA_TIER,
    CONF_CONSUMPTION_KWH,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_CUSTOM_INJECTION_MODE,
    CONF_CUSTOM_INJECTION_SPP_WEIGHTED,
    CONF_DAY_CONSUMPTION_KWH,
    CONF_DAY_INJECTION_KWH,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_INJECTION_KWH,
    CONF_MANUAL_ENERGY_BASE,
    CONF_MANUAL_ENERGY_FACTOR,
    CONF_MANUAL_ENERGY_OFFPEAK,
    CONF_MANUAL_ENERGY_PEAK,
    CONF_MANUAL_ENERGY_SINGLE,
    CONF_MANUAL_YEARLY_FEE,
    CONF_METER,
    CONF_NIGHT_CONSUMPTION_KWH,
    CONF_NIGHT_INJECTION_KWH,
    CONF_REGION,
    CONF_SOLAR_KVA,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DEFAULT_CONNECTION_KVA_TIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_FLANDERS,
    REGION_WALLONIA,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    STORAGE_VERSION,
    CUSTOM_CONTRACT_MONTHLY,
    CUSTOM_INJECTION_MODE_FORMULA,
    SUPPLIER_CUSTOM,
    UPDATE_INTERVAL_MINUTES,
    VREG_CAPACITY_FLOOR_KW,
)
from .pricing import (
    MeterType,
    PriceBreakdown,
    compute_breakdown,
    is_offpeak,
    slot_start,
    static_breakdown,
    tou_slot,
    yearly_fixed_fee_for_meter,
)
from .providers import (
    DynamicRates,
    ExtractorError,
    SpotMonthlyRates,
    SupplierSnapshot,
    get as get_extractor,
)
from .providers._pdf import is_transient_fetch_error
from .providers.custom import build_snapshot as build_custom_snapshot
from .synergrid import SppWeights, fetch_spp_weights
from .providers.base import (
    DsoOverlay,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SupplierExtractor,
    TaxOverlay,
    TimeOfUseRates,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)


def _supplier_label(supplier_id: str | None) -> str:
    """The supplier's human-facing label, falling back to its raw id.

    Anything user-facing should name a supplier the way the config flow's
    dropdown does; the fallback keeps an entry on an unknown or renamed
    supplier readable instead of blank.
    """
    try:
        return get_extractor(str(supplier_id)).label
    except ExtractorError:
        return str(supplier_id or "") or "Belgian Electricity"


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
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.entry.entry_id)},
        name=coordinator.entry.title,
        manufacturer=_supplier_label(coordinator.entry.data.get(CONF_SUPPLIER, "")),
        entry_type=None,
    )


def _energy_is_quarter_hourly(energy: EnergyRates) -> bool:
    """True when the energy model bills on the native 15-minute grid.

    Engie, Cociter, EBEM, Ecofix, OCTA+ and Ecopower (Dynamische
    Burgerstroom) dynamic contracts set ``quarter_hourly`` (their cards
    price on the 15-minute Belpex / eSpot_15 / Epex 15 / EPEX DA spot);
    every other contract (static, TOU, hourly-billed dynamic such as
    Eneco, Frank, Luminus, Mega, TotalEnergies) stays hourly.
    """
    return isinstance(energy, DynamicRates) and energy.quarter_hourly


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

# A single failed fetch is almost always a transient CDN timeout that the
# next hourly tick recovers. Raising the user-facing "extractor failed"
# repair issue on the very first failure produced false alarms that wrongly
# told the user the supplier had changed its tariff layout. Only raise the
# issue once a failure has survived this many consecutive fetch attempts.
# The shared negative-fetch row carries the running count and it resets the
# moment a fetch succeeds; the 7-day snapshot_stale issue stays the backstop
# for a breakage that outlives every threshold.
_EXTRACTOR_ISSUE_THRESHOLD = 2

# Per-(supplier, contract, region, YYYY-MM) cache of historical snapshots
# the time-correct yearly-cost flow uses to bill each past month at its
# own rate. ``None`` is a negative cache so a probe-less supplier or a
# month outside the supplier's archive horizon doesn't refetch every
# refresh. Lives in-memory only; rebuilt fresh on HA restart.
_MONTHLY_SNAPSHOTS_KEY = "monthly_snapshot_cache"

# Per-(supplier, contract, region, YYYY-MM) timestamp of the last
# transient ``fetch_for_month`` failure. ``_snapshot_for_month``
# deliberately does NOT cache a transient error as a negative result
# (cached None means "no archive for this month"), so without this
# secondary marker every hourly tick would re-attempt every still-
# uncached past month against a flaky CDN. The TTL matches the live
# TTL: long enough to dedupe one hour of update ticks, short enough
# that a real recovery is picked up promptly.
_MONTHLY_FAILED_FETCHES_KEY = "monthly_snapshot_failed_fetches"
_MONTHLY_FAILURE_TTL = timedelta(minutes=30)

# Some past days genuinely have < 20 of 24 hourly day-ahead points at
# ENTSO-E (source gaps). Without a marker, _ensure_historical_spots
# re-pulls a whole week-chunk for such a day on every hourly tick for
# the rest of the year. Record the last attempt per stable past day and
# skip it for this long; 12 h re-attempts twice a day in case the data
# lands late, without hammering the rate-limited endpoint hourly.
_SHORT_SPOT_DAY_TTL = timedelta(hours=12)

# The Synergrid ex-ante SPP profile is revised within the year, so re-fetch the
# 52 MB workbook at most this often (weights survive restarts via the Store).
_SPP_REFRESH_DAYS = 30
# Back off this long after a failed SPP fetch so a persistent problem (e.g. the
# new-year file not yet published) doesn't re-download 52 MB every hourly tick.
_SPP_RETRY_TTL = timedelta(hours=12)


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
) -> dict[tuple[str, str, str], tuple[datetime, str, int]]:
    """Per-key (timestamp, last-error-message, consecutive-count) of recent
    fetch failures.

    Storing the error message alongside the timestamp lets a sibling
    coordinator that hits the negative-cache short-circuit surface the
    real failure reason in its UpdateFailed instead of an opaque
    'cold start'. The third field counts consecutive failures on the key so
    the coordinator can defer the 'extractor failed' repair issue past a lone
    transient timeout (see _EXTRACTOR_ISSUE_THRESHOLD); it resets whenever a
    fetch succeeds and the row is popped.
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
    monthly_failed = _monthly_failed_fetches(hass)
    for k in stale:
        monthly.pop(k, None)
        monthly_failed.pop(k, None)
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


def _monthly_failed_fetches(
    hass: HomeAssistant,
) -> dict[tuple[str, str, str, str], datetime]:
    """Per-(supplier, contract, region, YYYY-MM) timestamp of the last
    transient ``fetch_for_month`` failure."""
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    return bucket.setdefault(_MONTHLY_FAILED_FETCHES_KEY, {})  # type: ignore[no-any-return]


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
    failed = _monthly_failed_fetches(hass)
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
    # Negative cache: a transient fetch_for_month failure is intentionally
    # NOT written to ``cache`` (a cached None means "no archive for
    # this month"); without this secondary marker the hourly YTD walk
    # would re-attempt every uncached month against a flaky CDN. Skip
    # the retry while the marker is fresh; current_snapshot is the
    # documented proxy for non-archive months.
    last_fail = failed.get(cache_key)
    if last_fail is not None and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL:
        return current_snapshot
    gen_at_entry = _tuple_generation(hass, cache_key)
    async with _monthly_lock(hass, cache_key):
        # Re-check under the lock so the second waiter doesn't repeat
        # what the first just did.
        if cache_key in cache:
            cached = cache[cache_key]
            return cached if cached is not None else current_snapshot
        last_fail = failed.get(cache_key)
        if (
            last_fail is not None
            and dt_util.utcnow() - last_fail < _MONTHLY_FAILURE_TTL
        ):
            return current_snapshot
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
            failed[cache_key] = dt_util.utcnow()
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


def _parse_iso_date(value: Any) -> date | None:
    """Parse a stored ISO ``YYYY-MM-DD`` date string, or ``None``.

    Accepts the DateSelector return value used for the contract lifecycle
    fields; returns ``None`` for a missing / malformed value.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _contract_start_month(entry: ConfigEntry) -> date | None:
    """First-of-month of the configured contract start date, or ``None``.

    The signing month is what a fixed/dynamic contract's rate is locked
    against; the day within the month is irrelevant to which monthly card
    applies, so normalise to the first.
    """
    d = _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE))
    if d is None:
        return None
    return date(d.year, d.month, 1)


def _manual_energy_leg(
    entry: ConfigEntry, current_snapshot: "SupplierSnapshot"
) -> EnergyRates | None:
    """Build the energy leg from a hand-entered signing rate, or ``None``.

    The fallback for a start date the archive cannot cover: the user typed the
    rate they signed at. Shaped to match the contract's current kind (dynamic
    -> factor / base, fixed -> single / peak / offpeak). Per-kWh values are
    stored as entered (grossed by compute_breakdown at the current card's VAT
    rate); the yearly fee is stored VAT-inclusive, matching how cards store it.
    ``None`` when the override was left blank, or the contract is neither fixed
    nor dynamic.
    """
    energy = current_snapshot.energy
    fee_raw = entry.data.get(CONF_MANUAL_YEARLY_FEE)
    fee = float(fee_raw) if fee_raw is not None else 0.0
    if isinstance(energy, DynamicRates):
        factor = entry.data.get(CONF_MANUAL_ENERGY_FACTOR)
        base = entry.data.get(CONF_MANUAL_ENERGY_BASE)
        if factor is None and base is None:
            return None
        return DynamicRates(
            factor=float(factor) if factor is not None else 0.0,
            base=float(base) if base is not None else 0.0,
            yearly_fixed_fee=fee,
            quarter_hourly=energy.quarter_hourly,
        )
    if isinstance(energy, FixedRates):
        single = entry.data.get(CONF_MANUAL_ENERGY_SINGLE)
        if single is None:
            return None
        peak = entry.data.get(CONF_MANUAL_ENERGY_PEAK)
        offpeak = entry.data.get(CONF_MANUAL_ENERGY_OFFPEAK)
        return FixedRates(
            single=float(single),
            peak=float(peak) if peak is not None else None,
            offpeak=float(offpeak) if offpeak is not None else None,
            yearly_fixed_fee=fee,
        )
    return None


def _cohort_energy_from_archived(
    archived: "SupplierSnapshot",
) -> EnergyRates | None:
    """The energy leg a signing cohort bills at, from its archived card.

    Fixed / dynamic: the archived leg is exactly the locked rate. Variable:
    re-price the cohort's numeric formula coefficients against the CURRENT
    month's mean (a SpotMonthlyRates leg) rather than freeze the archived
    card's stale resolved rate, which would pin the signing-month index.
    ``None`` when the archived card exposes no re-priceable rate (a variable
    card whose coefficients couldn't be parsed, or a TOU / Impact kind).
    """
    energy = archived.energy
    if isinstance(energy, (FixedRates, DynamicRates)):
        return energy
    if isinstance(energy, VariableRates) and energy.formula_factor is not None:
        return SpotMonthlyRates(
            factor=energy.formula_factor,
            base=energy.formula_base if energy.formula_base is not None else 0.0,
            yearly_fixed_fee=energy.yearly_fixed_fee,
            # Carry the dedicated exclusive-night standing fee so an
            # exclusive-night meter keeps its own fee instead of falling back to
            # the standard abonnement (yearly_fixed_fee_for_meter reads it).
            yearly_fixed_fee_exclusive_night=energy.yearly_fixed_fee_exclusive_night,
        )
    return None


async def _cohort_energy_leg(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    entry: ConfigEntry,
    current_snapshot: "SupplierSnapshot",
) -> EnergyRates | None:
    """Resolve the energy leg a contract actually bills at, or ``None``.

    A fixed / dynamic contract signed months ago is billed at the rate it
    locked in at signing, not today's card. This returns the signing-month
    card's energy leg so the caller can splice it onto the current
    delivery-month DSO / tax overlays; ``None`` means "no cohort override,
    keep the current energy" (no start date set, this month / a future
    start, no archived card or manual rate to re-price with, or a variable
    cohort with no ENTSO-E key to resolve its monthly mean).

    Resolution order is archive, then a hand-entered manual rate, then the
    current card: the actual archived signing-month card is authoritative when
    the supplier keeps one; a manually entered rate covers non-archive
    suppliers and start dates older than the archive reaches.

    ``None`` is also returned for a ``contract`` that isn't the entry's own
    (the OptionsFlow compare path walks an alternative contract with no
    signing history, so it must always price at the current card).
    """
    if contract != entry.data.get(CONF_CONTRACT):
        return None
    start = _contract_start_month(entry)
    if start is None:
        return None
    now = dt_util.now()
    if start >= date(now.year, now.month, 1):
        # Signed this month or dated in the future: the current card already
        # is the signing-month card, so there is nothing to splice.
        return None
    # Prefer the actual archived signing-month card. Fixed / dynamic re-price
    # from its leg directly (the locked value); variable re-prices from the
    # cohort's parsed coefficients against the current month's mean (see
    # _cohort_energy_from_archived). TOU / Impact are not re-priced yet.
    if extractor.fetch_for_month is not None:
        snap_start = await _snapshot_for_month(
            hass, session, extractor, contract, region, start, current_snapshot
        )
        # _snapshot_for_month returns the SAME current_snapshot object when the
        # signing month has no archive; identity means "no archived card".
        if snap_start is not current_snapshot:
            cohort = _cohort_energy_from_archived(snap_start)
            # A SpotMonthlyRates leg bills at the current month's mean spot,
            # which needs an ENTSO-E key. Only the dynamic and spot-monthly
            # contract kinds are asked for one, so a variable cohort can reach
            # here without a key: keep the current card (priced off its own
            # resolved rate) instead of tearing the entry down over a key the
            # user was never prompted for.
            #
            # An archived DynamicRates leg needs a spot just as much, and is
            # deliberately not gated here: every extractor derives the energy
            # shape from the static catalogue kind rather than from the card
            # text, so a dynamic leg implies kind == "dynamic", which always
            # collected a key. Flipping an existing contract's kind in place,
            # or sniffing the shape out of the card, would break that and let
            # a keyless entry reach the spot fetch again.
            if isinstance(cohort, SpotMonthlyRates) and not entry.data.get(
                CONF_API_KEY
            ):
                cohort = None
            if cohort is not None:
                return cohort
    # No retrievable archived card: use a hand-entered signing rate if present,
    # else keep the current card (``_manual_energy_leg`` returns None when the
    # override was left blank).
    return _manual_energy_leg(entry, current_snapshot)


async def _effective_snapshot_for_month(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: "SupplierExtractor",
    contract: str,
    region: str,
    year_month: date,
    current_snapshot: "SupplierSnapshot",
    entry: ConfigEntry,
) -> "SupplierSnapshot":
    """Delivery-month snapshot with the signing cohort's energy leg spliced in.

    Every archive-walking cost path calls this instead of
    :func:`_snapshot_for_month`: it resolves the delivery month's regulated
    DSO / tax overlays as before, then overlays the frozen signing-month
    energy so a locked contract bills its own rate every month while network
    tariffs and taxes still track the delivery month. A no-op (returns the
    plain delivery-month snapshot) when there is no cohort override.
    """
    snap_m = await _snapshot_for_month(
        hass, session, extractor, contract, region, year_month, current_snapshot
    )
    cohort = await _cohort_energy_leg(
        hass, session, extractor, contract, region, entry, current_snapshot
    )
    if cohort is None:
        return snap_m
    return replace(snap_m, energy=cohort)


@dataclass
class CoordinatorData:
    """Snapshot the coordinator hands to entities."""

    hourly: dict[datetime, PriceBreakdown] = field(default_factory=dict)
    # Grid resolution of the keys in ``hourly``: RESOLUTION_HOURLY for
    # every static / hourly-billed contract, RESOLUTION_QUARTER for
    # dynamic suppliers that bill per quarter-hour (Engie). Consumers use
    # it to truncate "now" to the right slot and to size the
    # cheapest-window service.
    resolution: str = RESOLUTION_HOURLY
    snapshot_publication: str = ""
    snapshot_age_hours: float = 0.0
    snapshot_stale: bool = False
    # Last calendar day the snapshot's rates apply to. ``None`` means
    # the extractor couldn't parse a validity end -- callers should
    # fall back to "treat as valid".
    snapshot_valid_until: date | None = None
    last_error: str = ""
    # This month's running peak, as measured. NOT floored at the regulated
    # minimum: it is a measurement, and the floor is a billing rule that
    # belongs on the quantity below.
    monthly_peak_kw: float = 0.0
    monthly_peak_month: date | None = None
    # The kW the capacity tariff is charged on: the mean of the last twelve
    # monthly peaks, floored at VREG_CAPACITY_FLOOR_KW. Surfaced as attributes
    # on capacity_cost so the bill can be told apart from this month's reading,
    # together with how many months the mean covers (12 once a full year of
    # history has accumulated).
    capacity_billed_peak_kw: float = 0.0
    capacity_peak_months: int = 0
    capacity_cost_eur: float = 0.0
    prosumer_cost_eur: float = 0.0
    # EUR/kWh injection price for the slot this tick ran in. The sensor only
    # publishes it for contracts with no ``injection_hourly``; everything that
    # varies intra-day is read per slot from that table instead, so this value
    # does not follow the clock between ticks. None when:
    #   - the user is not on the injection regime, or
    #   - the snapshot's injection block has no usable data (formula needs
    #     spot but contract is variable so we don't fetch ENTSO-E).
    injection_price_eur_per_kwh: float | None = None
    # Per-slot injection price (EUR/kWh) across the same today+tomorrow grid
    # as ``hourly``. Drives BOTH the injection_price sensor's state (looked up
    # at the current slot, which is what keeps it on the slot the user is
    # billed for) and its today/tomorrow arrays, so narrowing or dropping this
    # table would silently put the state back on the tick's scalar (issue #44).
    # Empty except on the injection regime for a contract whose injection
    # varies intra-day (spot-indexed dynamic + Cociter Variable, or the Engie
    # Empower Flextime TOU schedule); flat contracts emit no array since it
    # would just repeat the scalar above. Same quarter->hour downsampling as
    # the consumption arrays happens in the sensor layer, for the arrays only.
    injection_hourly: dict[datetime, float] = field(default_factory=dict)
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
    # Optional diagnostic breakdown behind current_year_cost: YTD and today
    # consumption / injection kWh, the pre-clamp raw energy term and the fees
    # floor. Populated only on the static per-day (fixed / variable) path;
    # None for hourly-billed contracts and when no meter is wired. Surfaced as
    # attributes so a flat sensor can be told apart (negative raw energy = the
    # compensation clamp; a today kWh that never moves = stalled meter input).
    ytd_diagnostics: dict[str, float] | None = None


class _MigratingStore(Store[dict[str, Any]]):
    """Store subclass that drops blobs from a previous STORAGE_VERSION.

    Every field in the persisted snapshot is re-derivable from a fresh
    extractor fetch, so wiping the cache on a major-version mismatch is
    safe and avoids HA logging the default migrator's "missing migration
    function" warning. Returning an empty dict from
    ``_async_migrate_func`` makes ``async_load`` return ``{}`` and the
    coordinator re-fetches on its first refresh.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,  # noqa: ARG002 - HA signature.
        old_data: dict[str, Any],  # noqa: ARG002 - dropped wholesale.
    ) -> dict[str, Any]:
        if old_major_version < STORAGE_VERSION:
            return {}
        return old_data


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
        # Frozen snapshot of every load-bearing entry.data field at
        # construction. Used by ``__init__._async_options_updated`` to
        # decide whether a finalize-time options write actually changed
        # anything that needs a reload, or was a no-op options-clear.
        self._entry_data_signature: frozenset[tuple[str, Any]] = (
            self._compute_data_signature(entry)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)
        # Older blobs (any STORAGE_VERSION < the current one) are
        # discarded rather than migrated: every field they hold is
        # re-derivable from a fresh extractor fetch on the next tick,
        # so silencing the auto-migrator's warning is the goal here.
        self._store: Store[dict[str, Any]] = _MigratingStore(
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
        # UTC-hour -> EUR/kWh spot prices for past hours, used to
        # replay dynamic energy costs in current_year_cost. Persisted
        # to Store so a fresh restart doesn't lose the YTD window.
        self._historical_spots: dict[datetime, float] = {}
        # Synergrid solar production profile: hourly weights keyed by UTC
        # (month, day, hour), for SPP-weighted custom injection. Persisted so a
        # restart doesn't force a fresh 52 MB download; refreshed monthly (the
        # ex-ante file is revised in-year).
        self._spp_weights: SppWeights = {}
        self._spp_weights_year: int | None = None
        self._spp_fetched_at: datetime | None = None
        self._spp_failed_at: datetime | None = None
        # Stable past days whose last spot fetch still came back short of
        # 20 hours, with the attempt time, so we don't re-fetch them every
        # tick (see _SHORT_SPOT_DAY_TTL).
        self._short_spot_days: dict[date, datetime] = {}
        # Local days already confirmed to hold >= 20 cached spot hours. Within
        # a calendar year spots are only ever added, so a complete day stays
        # complete; caching the set lets the per-tick coverage scan skip the
        # timezone conversion and 24 dict lookups for every settled day. Prior
        # year entries are dropped in _prune_historical_spots at the boundary.
        self._complete_spot_days: set[date] = set()
        self._peak_kw: float = 0.0
        self._peak_month: date | None = None
        # Completed months' peaks, keyed by their ISO first-of-month, capped at
        # the 11 most recent. Together with the running _peak_kw they form the
        # rolling twelve Fluvius averages to bill the capacity tariff.
        self._peak_history: dict[str, float] = {}
        self._last_error: str = ""
        # Set by async_unload_entry. A slow in-flight tick can resume after
        # the entry was unloaded or removed; without this flag it would
        # resurrect a just-deleted Repairs issue or rewrite the removed
        # storage blob (the reload guards in _save_persistent don't fire on
        # a removal, which leaves entry.data unchanged), or contradict the
        # successor coordinator after a reload.
        self._unloaded = False

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
            # Absent on a blob written before the rolling average shipped, in
            # which case the entry simply starts its twelve-month window over.
            history = peak.get("history")
            if isinstance(history, dict):
                self._peak_history = {
                    key: float(kw)
                    for key, kw in history.items()
                    if isinstance(key, str) and isinstance(kw, (int, float))
                }
        # Same tuple_mismatch gate as the snapshot above: ENTSO-E spots
        # were collected while the entry was on a *dynamic* contract on
        # the previous tuple. After an OptionsFlow swap to a static
        # supplier they're never queried again but would otherwise be
        # re-saved indefinitely (pruned only at year-end), wasting
        # ~140KB of disk/memory until the next Jan 1.
        hist = stored.get("historical_spots")
        if isinstance(hist, dict) and not tuple_mismatch:
            for k, v in hist.items():
                if not isinstance(k, str) or not isinstance(v, (int, float)):
                    continue
                try:
                    when = datetime.fromisoformat(k)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                self._historical_spots[when] = float(v)
        # The SPP profile is the same national curve regardless of supplier, so
        # it is restored irrespective of the entry-tuple gate above.
        spp = stored.get("spp_weights")
        if isinstance(spp, dict):
            self._restore_spp_weights(spp)
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
        try:
            return await self._update_body()
        except UpdateFailed as err:
            # Snapshot age is independent of the current tick's
            # success: if the snapshot was already stale and *this*
            # tick fails for an unrelated reason (ENTSO-E auth,
            # missing DSO, ENTSO-E transient), refresh the
            # stale-snapshot Repairs placeholder with the latest
            # last_error so the user sees the current error rather
            # than whatever failure first raised the issue. Without
            # this the placeholder freezes until the next *clean*
            # tick reaches the bottom of _update_body.
            #
            # When _maybe_refresh_snapshot succeeded (``_last_error``
            # empty) but a downstream step like _build_hourly raised
            # UpdateFailed, fall back to the UpdateFailed message so
            # the placeholder doesn't render as the "unknown" sentinel
            # from _sync_stale_issue.
            if self._snapshot is not None and self._snapshot_fetched_at is not None:
                if not self._last_error:
                    self._last_error = str(err)
                age = self._snapshot_age_hours()
                stale = age > SNAPSHOT_STALE_DAYS * 24
                self._sync_stale_issue(stale)
            raise

    def _refresh_custom_snapshot(self) -> None:
        """Build the snapshot locally for the expert custom supplier.

        There is no card to fetch: the user typed the formula and all
        regulated values, so we assemble the snapshot from the config entry
        every tick. Always fresh (no probe / TTL), so it never goes stale.
        """
        self._snapshot = build_custom_snapshot(
            self.entry.data,
            self.entry.data.get(CONF_REGION, ""),
            self.entry.data.get(CONF_DSO, ""),
        )
        self._snapshot_fetched_at = dt_util.utcnow()
        self._last_error = ""

    async def _update_body(self) -> CoordinatorData:
        self._sync_deprecated_supplier_issue()
        if self.entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
            self._refresh_custom_snapshot()
        else:
            await self._maybe_refresh_snapshot()
        await self._track_monthly_peak()

        if self._snapshot is None:
            raise UpdateFailed(
                f"no supplier snapshot available: {self._last_error or 'cold start'}"
            )

        # Resolve the signing-cohort energy leg for a contract with a start
        # date: a fixed / dynamic contract signed months ago bills at the rate
        # it locked in, not today's card. ``priced`` splices that leg onto the
        # current delivery-month DSO / tax / injection overlays and is read at
        # every energy-pricing site below. ``self._snapshot`` is never mutated:
        # it is persisted and seeds the shared (supplier, contract, region)
        # cache row that sibling entries with a different start date adopt, so
        # baking cohort energy into it would mis-price co-tenants; ``priced`` is
        # a per-tick local. A no-op (``priced is self._snapshot``) when no start
        # date is set.
        cohort_energy = await _cohort_energy_leg(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data.get(CONF_REGION, ""),
            self.entry,
            self._snapshot,
        )
        priced = (
            self._snapshot
            if cohort_energy is None
            else replace(self._snapshot, energy=cohort_energy)
        )

        spot_prices: dict[datetime, float] = {}
        # Auth + extractor issue clear paths run OUTSIDE the
        # DynamicRates branch so that an existing Repairs entry
        # auto-resolves regardless of how the snapshot got refreshed
        # this tick (sibling-cache adoption, self-fresh probe match,
        # or a fresh fetch). Reaching this point with no live
        # ``_last_error`` means the extractor produced a clean
        # snapshot; the cycle-7 entsoe_auth_failed clear is
        # unconditional because that issue can only ever be set
        # inside the DynamicRates branch below.
        #
        # The extractor clear is gated on ``_last_error`` because
        # _maybe_refresh_snapshot raises the same Repairs issue when
        # a fresh fetch fails but a cached snapshot is still usable
        # (the kept-cached path). Without the gate the unconditional
        # clear immediately undoes that legitimate alert.
        self._sync_entsoe_auth_issue(False)
        if not self._last_error:
            self._sync_extractor_issue(None)
        if isinstance(priced.energy, (DynamicRates, SpotMonthlyRates)):
            # Both the live per-slot price (dynamic) and the flat monthly rate
            # (spot-monthly, from the month mean) need ENTSO-E spots, so they
            # share the hard-fail-on-cold-start path.
            try:
                spot_prices = await self._fetch_spot_prices()
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
        elif _injection_needs_spot(self._snapshot, self.entry):
            # Static-energy contract with a spot-indexed injection
            # (Cociter Variable): the energy is priced without a spot, so
            # a spot failure (missing key, ENTSO-E outage) must NOT tear
            # the entry down -- only the
            # injection credit goes unavailable. Fetch softly, falling
            # back to the cached curve, then to no injection price.
            try:
                spot_prices = await self._fetch_spot_prices()
            except (EntsoeError, EntsoeAuthError) as err:
                _LOGGER.debug(
                    "injection spot fetch failed (energy unaffected): %s", err
                )
                spot_prices = dict(self._spot_cache) if self._spot_cache else {}

        # Refresh the Synergrid SPP profile for a custom entry that opted into
        # SPP-weighted injection (soft-fail; degrades to the plain mean below).
        spp_weighted = _spp_weighting_enabled(self.entry)
        if spp_weighted:
            await self._ensure_spp_weights()

        # A spot-monthly contract bills a flat rate = factor * this month's
        # mean spot + base. Compute the running mean once (over the persisted
        # year-to-date hours plus today's fetched curve) and reuse it for the
        # live price table and for baking the mean-indexed injection.
        monthly_mean: float | None = None
        if isinstance(priced.energy, SpotMonthlyRates):
            now_local = dt_util.now()
            monthly_mean = self._monthly_spot_mean(
                now_local.year, now_local.month, spot_prices
            )

        try:
            hourly = self._build_hourly(priced, spot_prices, monthly_mean)
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
        billed_peak = 0.0
        if self.entry.data.get(CONF_REGION) == REGION_FLANDERS:
            billed_peak = self._billed_peak_kw()
            capacity_cost = _compute_capacity(self._snapshot, self.entry, billed_peak)

        prosumer_cost = _compute_prosumer(self._snapshot, self.entry)
        # For a spot-monthly contract, price the injection off the same
        # monthly mean rather than the live hourly spot: bake the mean-indexed
        # formula into a flat indicative for this tick (the stored snapshot
        # keeps factor/base so the YTD path recomputes each month's own mean).
        # Gate on the EFFECTIVE (cohort) energy: a variable contract re-priced
        # to a SpotMonthlyRates cohort must bake its spot-indexed injection at
        # the month mean too, so the live credit matches the YTD walk (which
        # dispatches on the cohort) and the backfill; self._snapshot.energy
        # stays VariableRates for such a contract, so keying off it would skip
        # the bake and leave live crediting at the per-hour spot. The bake is a
        # no-op for a flat monthly-indicative injection (EBEM/Eneco/Mega).
        injection_snapshot = self._snapshot
        if isinstance(priced.energy, SpotMonthlyRates):
            inj_mean = monthly_mean
            if spp_weighted:
                # SPP-weight the injection month-mean; keep the flat mean for
                # energy. Fall back to the flat mean when the profile isn't
                # available for the month yet.
                now = dt_util.now()
                spp_mean = self._spp_weighted_month_mean(
                    now.year, now.month, spot_prices
                )
                if spp_mean is not None:
                    inj_mean = spp_mean
            injection_snapshot = _bake_monthly_injection(self._snapshot, inj_mean)
        injection_price = _compute_injection_price(
            injection_snapshot, self.entry, spot_prices
        )
        # Dynamic contracts replay historical hourly spots to bill the
        # YTD energy term; spot-monthly contracts average them per month;
        # static-energy contracts with a spot-indexed injection replay them
        # to credit the YTD injection. Backfill any missing hours in
        # [Jan 1, today] before calling the engine; failures degrade to "no
        # data" for those hours rather than tearing the tick down.
        if isinstance(
            priced.energy, (DynamicRates, SpotMonthlyRates)
        ) or _injection_needs_spot(self._snapshot, self.entry):
            today_local = dt_util.now().date()
            await self._ensure_historical_spots(
                date(today_local.year, 1, 1), today_local
            )
        ytd_breakdown: dict[str, float] = {}
        current_year_cost = await _compute_current_year_cost(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self._snapshot,
            self.entry,
            historical_spots=self._historical_spots,
            spp_weights=self._spp_weights if spp_weighted else None,
            breakdown=ytd_breakdown,
            billed_peak_kw=billed_peak,
        )

        await self._save_persistent()

        age = self._snapshot_age_hours()
        stale = age > SNAPSHOT_STALE_DAYS * 24
        self._sync_stale_issue(stale)
        return CoordinatorData(
            hourly=hourly,
            resolution=(
                RESOLUTION_QUARTER
                if _energy_is_quarter_hourly(priced.energy)
                else RESOLUTION_HOURLY
            ),
            snapshot_publication=self._snapshot.publication_label,
            snapshot_age_hours=age,
            snapshot_stale=stale,
            snapshot_valid_until=self._snapshot.valid_until,
            last_error=self._last_error,
            monthly_peak_kw=self._peak_kw,
            monthly_peak_month=self._peak_month,
            capacity_billed_peak_kw=billed_peak,
            capacity_peak_months=len(self._peak_history) + 1,
            capacity_cost_eur=capacity_cost,
            prosumer_cost_eur=prosumer_cost,
            injection_price_eur_per_kwh=injection_price,
            injection_hourly=self._build_injection_hourly(
                injection_snapshot, priced.energy, spot_prices, hourly.keys()
            ),
            yearly_fixed_fee_eur=yearly_fixed_fee_for_meter(
                priced.energy,
                self.entry.data.get(CONF_METER, METER_MONO),
            ),
            energy_fund_eur_per_month=self._snapshot.taxes.energy_fund_eur_per_month,
            current_year_cost_eur=current_year_cost,
            ytd_diagnostics=ytd_breakdown or None,
        )

    def _sync_stale_issue(self, stale: bool) -> None:
        """Raise or clear the 'snapshot stale' repair issue for this entry."""
        if self._unloaded:
            return
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

    def _sync_extractor_issue(
        self, message: str | None, *, transient: bool = False
    ) -> None:
        """Raise or clear the supplier-extractor repair issue.

        Two mutually-exclusive flavours share this Repairs slot:

        - actionable (``transient=False``): a parse error, 404 or non-PDF
          payload that will not self-heal. Surfaces the ``extractor_failed``
          card whose advice is "the supplier changed its layout, open a
          GitHub issue".
        - transient (``transient=True``): a network timeout / reset / 5xx /
          anti-bot 403 that a later refresh usually recovers. Surfaces the
          softer ``extractor_unreachable`` card.

        Whichever flavour is raised clears the other so the user never sees
        both at once. ``message`` ``None`` means the latest fetch succeeded
        and clears both.
        """
        if self._unloaded:
            return
        failed_id = f"extractor_failed_{self.entry.entry_id}"
        unreachable_id = f"extractor_unreachable_{self.entry.entry_id}"
        if not message:
            ir.async_delete_issue(self.hass, DOMAIN, failed_id)
            ir.async_delete_issue(self.hass, DOMAIN, unreachable_id)
            return
        raise_id, clear_id, translation_key = (
            (unreachable_id, failed_id, "extractor_unreachable")
            if transient
            else (failed_id, unreachable_id, "extractor_failed")
        )
        ir.async_delete_issue(self.hass, DOMAIN, clear_id)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            raise_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
                "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
                "error": message,
            },
        )

    def _sync_entsoe_auth_issue(self, active: bool, message: str = "") -> None:
        """Raise or clear the 'ENTSO-E rejected the API key' issue.

        Fired only on ``EntsoeAuthError`` (transparency.entsoe.eu
        responded 401), so the user knows the fix is "rotate the token
        in the entry's options" rather than waiting on a transient
        outage. Cleared as soon as a refresh succeeds with a key the
        endpoint accepts.
        """
        if self._unloaded:
            return
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

    def _sync_deprecated_supplier_issue(self) -> None:
        """Raise or clear the 'this supplier is leaving the market' issue.

        Driven purely by the registry's ``deprecated_until`` /
        ``deprecated_successor`` (``providers/base.py``), never by comparing
        a date to the clock: the card is an instruction to switch supplier,
        and it stays up for as long as the entry points at a supplier that
        has announced its exit. Clears by itself when the user re-points the
        entry, and on any release that drops the registry flag.

        Kept separate from the extractor / staleness cards on purpose. Those
        say "the fetch is failing"; this one says "the fetch will keep
        working and then stop, and here is what to do about it". Prices are
        untouched -- a user still supplied by DATS 24 in August must still be
        billed August's rates.
        """
        if self._unloaded:
            return
        issue_id = f"supplier_deprecated_{self.entry.entry_id}"
        supplier_id = str(self.entry.data.get(CONF_SUPPLIER, ""))
        try:
            extractor = get_extractor(supplier_id)
        except ExtractorError:
            # An entry on a supplier this build no longer ships: the
            # extractor cards already cover that, nothing to add here.
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        if extractor.deprecated_until is None:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="supplier_deprecated",
            # Labels, not registry ids: the card tells the user to pick a
            # supplier from a label-based dropdown, so "DATS 24" and
            # "EnergyVision" are what they will actually look for.
            translation_placeholders={
                "supplier": extractor.label,
                "successor": _supplier_label(extractor.deprecated_successor),
                "ends_on": extractor.deprecated_until.isoformat(),
            },
        )

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

    async def reset_monthly_peak(self) -> None:
        """Drop the persisted monthly peak so the next tick rebuilds it.

        Exposed via the diagnostic Reset peak button. Required when an
        earlier release stored an inflated peak (e.g. a W-unit sensor
        misread as kW pre-0.5.45) and the rolling-max comparison would
        otherwise hold the bad value until the next 1st of the month.
        Persists immediately so the reset survives an HA restart between
        now and the next coordinator tick. The banked history goes too: a
        bad value that has already rolled into a completed month would
        otherwise keep dragging the twelve-month mean for a year.
        """
        self._peak_kw = 0.0
        self._peak_history.clear()
        await self._save_persistent()
        await self.async_request_refresh()

    @staticmethod
    def _compute_data_signature(entry: ConfigEntry) -> frozenset[tuple[str, Any]]:
        """Frozen snapshot of every load-bearing entry.data field.

        Used by ``__init__._async_options_updated`` to skip a needless
        reload when the OptionsFlow's no-op finalize wrote
        ``options = {}`` on top of an already-empty options dict (the
        listener fires whenever options changes, even if entry.data
        didn't). Every meaningful field on this integration lives in
        entry.data, so an entry.options change without entry.data
        change can be ignored.
        """
        return frozenset(entry.data.items())

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
                # A successful probe also confirms the supplier is
                # reachable again, so clear any stale failure left by an
                # earlier transient fetch error. This path never
                # re-fetches, so without it a single-entry install (no
                # sibling to trigger _adopt_shared) would keep the "could
                # not reach the supplier" Repairs card and _last_error
                # until the published card changed. Emptying _last_error
                # lets the caller's top-level clear drop the extractor
                # issue; pop the negative-cache row so siblings stop
                # backing off. Gated on probe_key is not None: a failed /
                # absent probe is not proof of recovery.
                self._last_error = ""
                _shared_failed_fetches(self.hass).pop(key, None)
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
        # ``async_force_refresh`` raises ``_force_refresh`` and clears
        # *its own* view of the marker, but a sibling failing in the
        # window between the clear and this tick re-populates the row;
        # bypassing the short-circuit when ``_force_refresh`` is set
        # keeps the user-facing refresh service from silently no-op'ing.
        failed = _shared_failed_fetches(self.hass)
        if not self._force_refresh:
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
            # waiter doesn't repeat what the first just failed; same
            # _force_refresh bypass as above.
            if not self._force_refresh:
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
                self._sync_extractor_issue(None)
            except Exception as err:  # noqa: BLE001 - re-raised below for non-extractor types
                # Any extractor failure (including unexpected aiohttp /
                # parser exceptions) must populate the negative cache so
                # sibling coordinators back off instead of refiring the
                # same broken request on the next tick. The third tuple
                # field counts consecutive failures on this key so a lone
                # transient timeout doesn't immediately raise a repair
                # issue; the count rides the shared row and resets the
                # moment a fetch succeeds (failed.pop above).
                prev = failed.get(key)
                fail_count = (prev[2] if prev is not None else 0) + 1
                if _tuple_generation(self.hass, key) == gen_at_entry:
                    failed[key] = (dt_util.utcnow(), str(err), fail_count)
                self._last_error = str(err)
                # A transient network failure (timeout / reset / 5xx /
                # anti-bot 403) usually recovers on the next tick, so defer
                # its softer "could not reach the supplier" card until it
                # has crossed the threshold. A parse error / 404 / non-PDF
                # payload won't self-heal, so raise the actionable
                # "extractor failed" card on the first failure.
                transient = isinstance(
                    err, asyncio.TimeoutError
                ) or is_transient_fetch_error(str(err))
                if not transient:
                    self._sync_extractor_issue(str(err), transient=False)
                elif fail_count >= _EXTRACTOR_ISSUE_THRESHOLD:
                    self._sync_extractor_issue(str(err), transient=True)
                _LOGGER.warning(
                    "snapshot refresh failed for %s/%s: %s; keeping cached"
                    " (consecutive failure %d)",
                    self.entry.data.get(CONF_SUPPLIER),
                    self.entry.data.get(CONF_CONTRACT),
                    err,
                    fail_count,
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

    def _shared_is_fresh(
        self,
        shared: _SharedSnapshot,
        probe_key: str | None,
        now: datetime,
        ttl: timedelta,
    ) -> bool:
        """Whether a sibling's shared snapshot can be adopted as-is.

        ``async_force_refresh`` flips ``_force_refresh`` to opt the
        coordinator out of every adoption shortcut: without this guard
        a sibling that re-seeded the shared cache between the
        ``_shared_snapshots.pop`` and the next tick would silently
        satisfy the forced refresh, making the user-facing refresh
        service a no-op on multi-entry installs.
        """
        if self._force_refresh:
            return False
        if probe_key is not None:
            return shared.probe_key == probe_key
        return now - shared.fetched_at < ttl

    async def _ensure_historical_spots(
        self, start: date, end: date, api_key: str | None = None
    ) -> None:
        """Make sure ``self._historical_spots`` covers every hour of the
        local days in ``[start, end]``, fetching missing ranges from
        ENTSO-E.

        ``api_key`` overrides the entry's key, letting the compare flow
        backfill spots for a spot-indexed target with a key the user typed
        in the compare step even when their own entry carries none.

        Day boundaries are anchored on local midnight (converted to UTC),
        matching the recorder window (``_recorder_rows``) and the
        persistence cut-off (``_save_persistent``). Anchoring on UTC
        midnight instead would leave the first one or two hours of the
        local year (local Jan 1 00:00 falls on Dec 31 UTC in Brussels)
        unfetched, so the dynamic YTD would never credit them even though
        the recorder reports consumption there.

        Walks the day axis once. A day is considered "present" when at
        least 20 of its 24 hours are already cached -- ENTSO-E
        occasionally leaves gaps under the carry-forward rule (and DST
        seam days have 23/25 hours), and a few missing hours per day
        shouldn't trigger a re-fetch every coordinator tick. Failed
        fetches are logged and skipped; the caller treats absent hours as
        "no data" rather than tearing the YTD computation down.
        """
        api_key = api_key or self.entry.data.get(CONF_API_KEY)
        if not api_key:
            return
        now = dt_util.utcnow()
        # Days older than this are stable enough that a short fetch means
        # a genuine source gap, not data still being published; only those
        # get the "attempted, still short" skip marker. Today and yesterday
        # are always re-fetched so their hours fill in promptly.
        stable_before = dt_util.now().date() - timedelta(days=1)
        # Collect contiguous date ranges where the cache is sparse.
        missing_ranges: list[tuple[date, date]] = []
        range_start: date | None = None
        cur = start
        while cur <= end:
            if cur in self._complete_spot_days:
                # Confirmed fully covered on an earlier tick. Treat as present
                # (so it closes any open missing range) without redoing the tz
                # conversion and 24 dict lookups.
                present = 24
            else:
                day_start_utc = dt_util.start_of_local_day(cur).astimezone(UTC)
                present = sum(
                    1
                    for h in range(24)
                    if (day_start_utc + timedelta(hours=h)) in self._historical_spots
                )
                # >= 20 is the same threshold the fetch decision below uses, so
                # a day recorded here is one that would never be re-fetched
                # anyway; caching it just skips the scan next tick.
                if present >= 20:
                    self._complete_spot_days.add(cur)
            last_attempt = self._short_spot_days.get(cur)
            recently_short = (
                present < 20
                and last_attempt is not None
                and now - last_attempt < _SHORT_SPOT_DAY_TTL
            )
            if present < 20 and not recently_short:
                if range_start is None:
                    range_start = cur
            elif range_start is not None:
                missing_ranges.append((range_start, cur))
                range_start = None
            cur += timedelta(days=1)
        if range_start is not None:
            missing_ranges.append((range_start, cur))
        if not missing_ranges:
            return
        client = EntsoeClient(api_key, self._session)
        for r_start, r_end in missing_ranges:
            chunk_start = r_start
            while chunk_start < r_end:
                # Week-sized chunks: trade off per-request latency
                # against total round-trips for a 365-day backfill.
                chunk_end = min(chunk_start + timedelta(days=7), r_end)
                # Local-midnight anchors (in UTC) so the fetched window
                # lines up with the local-day grid the recorder and the
                # present-check above use.
                start_utc = dt_util.start_of_local_day(chunk_start).astimezone(UTC)
                end_utc = dt_util.start_of_local_day(chunk_end).astimezone(UTC)
                try:
                    prices = await client.fetch_day_ahead(start_utc, end_utc)
                except (EntsoeError, EntsoeAuthError) as err:
                    _LOGGER.warning(
                        "ENTSO-E historical fetch failed for %s..%s: %s",
                        chunk_start,
                        chunk_end,
                        err,
                    )
                    chunk_start = chunk_end
                    continue
                self._historical_spots.update(prices)
                # Mark stable past days that are STILL short after this
                # fetch so the next ticks skip them until the TTL expires;
                # clear the marker for any day that is now complete.
                day = chunk_start
                while day < chunk_end:
                    ds_utc = dt_util.start_of_local_day(day).astimezone(UTC)
                    got = sum(
                        1
                        for h in range(24)
                        if (ds_utc + timedelta(hours=h)) in self._historical_spots
                    )
                    if got < 20 and day < stable_before:
                        self._short_spot_days[day] = now
                    else:
                        self._short_spot_days.pop(day, None)
                    day += timedelta(days=1)
                chunk_start = chunk_end

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
        # Anchor both endpoints on local midnight so the fetched UTC
        # window matches the actual local-day hour count. A naive
        # ``end = start + timedelta(days=N)`` adds 24 UTC hours and
        # falls one hour short on the fall-back Sunday (local day has
        # 25 hours), so the last local hour ends up missing from the
        # spot cache. Same anchoring as ``_recorder_rows`` uses for the
        # recorder window.
        start = dt_util.start_of_local_day(local_today).astimezone(UTC)
        days = 2 if want_tomorrow else 1
        end = dt_util.start_of_local_day(local_today + timedelta(days=days)).astimezone(
            UTC
        )
        # Keep the native 15-minute slots only for suppliers that bill on
        # them (Engie Dynamic); everyone else gets the hourly aggregate.
        snap = self._snapshot
        quarter_hourly = snap is not None and _energy_is_quarter_hourly(snap.energy)
        prices = await client.fetch_day_ahead(start, end, quarter_hourly=quarter_hourly)
        self._spot_cache = prices
        self._spot_cache_day = local_today
        # Flag what the response actually carries, not what we asked
        # for: ENTSO-E publishes the day-ahead curve around 12-13 CET,
        # so a tick that requests tomorrow before publication comes
        # back with today only. Locking the flag to True on intent
        # would block the next hourly tick from retrying and tomorrow's
        # prices wouldn't surface until local midnight (reloading the
        # entry was the only way out).
        tomorrow = local_today + timedelta(days=1)
        self._spot_cache_includes_tomorrow = any(
            dt_util.as_local(h).date() == tomorrow for h in prices
        )
        return prices

    async def _track_monthly_peak(self) -> None:
        if self.entry.data.get(CONF_REGION) != REGION_FLANDERS:
            # Outside Flanders the capacity tariff doesn't apply. Reset
            # any peak left over from a previous Flanders config so it
            # doesn't linger in diagnostics or the persistent store. The
            # banked window goes too, or moving back to Flanders later would
            # resume billing on year-old peaks from the previous address;
            # Fluvius likewise restarts the window when the grid user changes.
            self._peak_kw = 0.0
            self._peak_month = None
            self._peak_history.clear()
            return
        # Roll over on the local 1st-of-month; using UTC would lag CET/CEST
        # users by 1-2 hours on the boundary and miss late-Dec-31 / early-Jan-1.
        local_now = dt_util.now()
        current_month = date(local_now.year, local_now.month, 1)
        if self._peak_month != current_month:
            # Bank the month that just closed before resetting: the capacity
            # tariff bills the mean of the last twelve monthly peaks, not the
            # one being accumulated. A peak of 0 means the month collected no
            # reading at all (fresh entry, or HA down throughout), which is not
            # a measured 0 and must not drag the mean down.
            if self._peak_month is not None and self._peak_kw > 0.0:
                self._peak_history[self._peak_month.isoformat()] = self._peak_kw
            # Eleven completed months plus the running one make twelve.
            for stale in sorted(self._peak_history)[:-11]:
                del self._peak_history[stale]
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
                # Scale by the source unit: the auto-pick walks back
                # from the Energy dashboard kWh sensor to a Riemann
                # integration source, which is almost always a power
                # sensor in W. Without scaling, 4481 W is stored as
                # 4481 kW and the capacity_cost sensor inflates by
                # 1000x (issue #19). An empty / missing unit is kept
                # as kW for back-compat with sensors that never set
                # the attribute.
                unit = (state.attributes.get("unit_of_measurement") or "").strip()
                if unit in ("W", "VA"):
                    value *= 0.001
                elif unit not in ("", "kW", "kVA"):
                    _LOGGER.warning(
                        "capacity peak sensor %s reports in %r; "
                        "expected kW/W/VA/kVA, ignoring this update",
                        entity_id,
                        unit,
                    )
                    value = 0.0
                if value > self._peak_kw:
                    self._peak_kw = value

    def _billed_peak_kw(self) -> float:
        """The kW the capacity tariff is actually charged on.

        Fluvius bills the "gemiddelde maandpiek", and its own methodology gives
        the formula outright: "Rekenkundig gemiddelde van de Max (Maandpiek
        (m), 2.5) voor elke maand (m) ... Er worden maximaal 12 maanden
        gebruikt." So the regulated minimum lands on EACH month before the
        mean, not on the mean, and one monthly peak is the highest
        quarter-hour offtake of that month. Because every term is then at
        least the floor, the mean is too, and no outer clamp is needed.

        Fewer than twelve months of history means the mean is taken over what
        there is, so a fresh entry starts out billing on this month alone
        (exactly what it did before the window existed) and converges over the
        first year. That also covers a month we never measured: Fluvius
        estimates a missing month as the mean of the validated ones, and
        inserting a set's own mean into it leaves the mean unchanged, so
        simply leaving the gap out lands on the same number. Fixed mode
        bypasses the window entirely: the user is stating a peak, not
        measuring one.
        """
        if self.entry.data.get(CONF_CAPACITY_MODE) == CAPACITY_MODE_FIXED:
            return max(self._peak_kw, VREG_CAPACITY_FLOOR_KW)
        peaks = [*self._peak_history.values(), self._peak_kw]
        return sum(max(kw, VREG_CAPACITY_FLOOR_KW) for kw in peaks) / len(peaks)

    def _build_hourly(
        self,
        snap: SupplierSnapshot,
        spot_prices: dict[datetime, float],
        monthly_mean: float | None = None,
    ) -> dict[datetime, PriceBreakdown]:
        # ``snap`` is the signing-cohort-priced snapshot (energy leg swapped
        # to the locked rate; DSO / tax overlays still the delivery month),
        # not necessarily self._snapshot.
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

        # A spot-monthly contract bills a flat rate for the whole month; pass
        # the delivery month's mean as the "spot" so every slot of the 48-slot
        # walk prices to factor * mean + base. Without a mean yet (cold start,
        # no cached spots) leave the table empty so the current price reads
        # unknown rather than crashing on a missing spot.
        slot_spot: float | None = None
        if isinstance(snap.energy, SpotMonthlyRates):
            if monthly_mean is None:
                return hourly
            slot_spot = monthly_mean

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
        # End at the start of the day after tomorrow (local) rather than a
        # fixed 48 UTC hours: the fall-back Sunday has 25 local hours, so
        # a fixed range(48) leaves only 23 UTC slots for tomorrow and
        # drops its last local hour. This bound covers today + tomorrow in
        # full (47 slots on spring-forward, 49 on fall-back, 48 otherwise).
        end_utc = (
            dt_util.start_of_local_day(local_midnight.date() + timedelta(days=2))
            .astimezone(UTC)
            .replace(minute=0, second=0, microsecond=0)
        )
        utc = start_utc
        while utc < end_utc:
            local = dt_util.as_local(utc)
            hourly[utc] = compute_breakdown(
                snap, dso, region, local, slot_spot, meter, dso_mode
            )
            utc += timedelta(hours=1)
        return hourly

    def _build_injection_hourly(
        self,
        injection_snapshot: SupplierSnapshot,
        energy: EnergyRates,
        spot_prices: dict[datetime, float],
        grid_keys: Iterable[datetime],
    ) -> dict[datetime, float]:
        """Per-slot injection price (EUR/kWh) over the same today+tomorrow grid
        as ``hourly``, for the injection sensor's today/tomorrow arrays.

        Empty unless the user is on the injection regime AND the injection
        actually varies intra-day: a flat contract would just repeat its
        scalar, so no array is emitted. ``injection_snapshot`` is the possibly
        mean-baked snapshot and ``energy`` the effective (cohort) energy, so a
        spot-monthly / Cociter-cohort contract is treated as flat and gated
        out -- keeping the array consistent with the live scalar and the YTD
        credit. Slots with no spot (tomorrow before the day-ahead publishes)
        are dropped, exactly like the consumption tomorrow array.
        """
        if self.entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
            return {}
        inj = injection_snapshot.injection
        if inj is None or not _injection_varies_intraday(inj, energy):
            return {}
        out: dict[datetime, float] = {}
        for utc in grid_keys:
            rate = _injection_price_for_slot(
                inj, energy, spot_prices.get(utc), dt_util.as_local(utc)
            )
            if rate is not None:
                out[utc] = rate
        return out

    def _monthly_spot_mean(
        self, year: int, month: int, extra_spots: dict[datetime, float]
    ) -> float | None:
        """Arithmetic mean of the (year, month)'s hourly Day-Ahead spots.

        Merges the persisted year-to-date cache with ``extra_spots`` (today's
        freshly fetched curve) so the current month's running mean stays up to
        date within a tick, and de-duplicates by timestamp. Returns ``None``
        when no spot for that month is available yet (cold start).
        """
        merged = dict(self._historical_spots)
        merged.update(extra_spots)
        merged = _drop_future_spots(merged, dt_util.now().date())
        return _mean_of_month(merged, year, month)

    def _restore_spp_weights(self, blob: dict[str, Any]) -> None:
        """Rehydrate the persisted SPP profile blob into ``_spp_weights``."""
        year = blob.get("year")
        raw = blob.get("weights")
        if not isinstance(year, int) or not isinstance(raw, dict):
            return
        parsed: SppWeights = {}
        for key, value in raw.items():
            if not isinstance(key, str) or not isinstance(value, (int, float)):
                continue
            try:
                month, day, hour = (int(x) for x in key.split(","))
            except ValueError:
                continue
            parsed[(month, day, hour)] = float(value)
        if not parsed:
            return
        self._spp_weights = parsed
        self._spp_weights_year = year
        fetched = blob.get("fetched_at")
        if isinstance(fetched, str):
            try:
                self._spp_fetched_at = datetime.fromisoformat(fetched)
            except ValueError:
                self._spp_fetched_at = None

    async def _ensure_spp_weights(self) -> None:
        """Refresh the Synergrid SPP profile for the current year if stale.

        Only called for a custom entry that opted into SPP-weighted injection.
        The ex-ante file is revised in-year, so re-fetch monthly. Soft-fail: on
        error keep whatever we already have (the caller degrades to the plain
        arithmetic mean) and back off ``_SPP_RETRY_TTL`` so a persistent failure
        doesn't re-download the 52 MB workbook every tick.
        """
        now = dt_util.utcnow()
        year = dt_util.now().year
        fresh = (
            self._spp_weights_year == year
            and self._spp_fetched_at is not None
            and (now - self._spp_fetched_at) < timedelta(days=_SPP_REFRESH_DAYS)
        )
        if fresh:
            return
        if (
            self._spp_failed_at is not None
            and (now - self._spp_failed_at) < _SPP_RETRY_TTL
        ):
            return
        weights = await fetch_spp_weights(self._session, year)
        if weights:
            self._spp_weights = weights
            self._spp_weights_year = year
            self._spp_fetched_at = now
            self._spp_failed_at = None
        else:
            self._spp_failed_at = now

    def _spp_weighted_month_mean(
        self, year: int, month: int, extra_spots: dict[datetime, float]
    ) -> float | None:
        """SPP-weighted mean of the delivery month's Day-Ahead spots, or None.

        Weights each hourly price by the Synergrid solar production profile so
        the injection index matches an SPP-indexed contract. Uses the same
        local-delivery-month filter as :meth:`_monthly_spot_mean`. Returns
        ``None`` (caller falls back to the plain mean) when the profile or the
        month's spots are unavailable.
        """
        if not self._spp_weights:
            return None
        merged = dict(self._historical_spots)
        merged.update(extra_spots)
        merged = _drop_future_spots(merged, dt_util.now().date())
        return _spp_weighted_month_mean(merged, self._spp_weights, year, month)

    def _snapshot_age_hours(self) -> float:
        if self._snapshot_fetched_at is None:
            return float("inf")
        return (dt_util.utcnow() - self._snapshot_fetched_at).total_seconds() / 3600.0

    def _prune_historical_spots(self) -> None:
        """Drop cached spots older than the current YTD window.

        Called each tick so the in-memory dict (and the persisted blob) do
        not grow unbounded across year boundaries. Anchor on local midnight:
        in Brussels (UTC+1/+2) the local Jan 1 00:00 falls one or two hours
        BEFORE UTC Jan 1 00:00, so a UTC anchor would silently drop the first
        hour or two of YTD. Prior-year keys are pure dead weight -- every
        consumer filters by the current (year, month) or an exact current-year
        hour key -- so removing them changes no result."""
        if not self._historical_spots:
            return
        today = dt_util.now().date()
        keep_after = dt_util.start_of_local_day(date(today.year, 1, 1)).astimezone(UTC)
        # Within a calendar year every cached hour already sits at or after the
        # cutoff, so skip rebuilding the whole dict every tick. Only rebuild
        # when a prior-year key actually needs dropping (the year boundary).
        # The min() scan is a cheap comparison; it avoids a full dict
        # reallocation on each of the other 364 days.
        if min(self._historical_spots) >= keep_after:
            return
        self._historical_spots = {
            h: v for h, v in self._historical_spots.items() if h >= keep_after
        }
        # Drop prior year days from the completeness set alongside their spots
        # so it doesn't grow without bound across years.
        self._complete_spot_days = {
            d for d in self._complete_spot_days if d.year >= today.year
        }

    async def _save_persistent(self) -> None:
        # Removal/unload guard: a tick resuming after the entry was removed
        # would recreate the storage blob async_remove_entry just deleted;
        # the reload guards below don't fire on a removal (entry.data is
        # unchanged), so this explicit check is the one that catches it.
        if self._unloaded:
            return
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
        # Tuple guard: covers the window where ``runtime_data`` is
        # still UNDEFINED (in-flight reload) but ``entry.data`` has
        # already been swapped to the new supplier/contract/region by
        # ``async_update_entry``. A late-finishing tick on the obsolete
        # coordinator would otherwise stamp this coord's old tuple over
        # whatever the new coord already wrote; the load path discards
        # mismatched blobs but only at the next HA boot, leaving a
        # window where a crash between writes loses the new state.
        live_tuple = (
            self.entry.data.get(CONF_SUPPLIER),
            self.entry.data.get(CONF_CONTRACT),
            self.entry.data.get(CONF_REGION),
        )
        if live_tuple != self._supplier_tuple:
            _LOGGER.debug(
                "skipping _save_persistent for %s: entry tuple drifted "
                "(coord=%s, entry=%s)",
                self.entry.entry_id,
                self._supplier_tuple,
                live_tuple,
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
                "history": dict(self._peak_history),
            },
        }
        if self._snapshot is not None and self._snapshot_fetched_at is not None:
            payload["snapshot"] = _snapshot_to_dict(
                self._snapshot,
                self._snapshot_fetched_at,
                self._snapshot_probe_key,
            )
        # Prune in memory (not just in the serialized copy) so a coordinator
        # running across a year boundary doesn't retain the prior year's
        # ~8760 hourly entries forever.
        self._prune_historical_spots()
        if self._historical_spots:
            payload["historical_spots"] = {
                h.isoformat(): v for h, v in self._historical_spots.items()
            }
        if self._spp_weights and self._spp_weights_year is not None:
            payload["spp_weights"] = {
                "year": self._spp_weights_year,
                "fetched_at": (
                    self._spp_fetched_at.isoformat() if self._spp_fetched_at else None
                ),
                "weights": {
                    f"{m},{d},{h}": v for (m, d, h), v in self._spp_weights.items()
                },
            }
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


def _brussels_osp_fee(overlay: DsoOverlay | None, entry: ConfigEntry) -> float:
    """Brussels Brugel OSP annual fee (EUR/year) for the configured tier.

    The fee is a flat Sibelga charge scaled by contractual connection power;
    the user picks the tier in the config flow (default 1.44-6.00 kVA).
    Returns 0 outside Brussels or when the card omits the OSP table."""
    if overlay is None or overlay.brussels_osp_by_tier is None:
        return 0.0
    tier = entry.data.get(CONF_CONNECTION_KVA_TIER, DEFAULT_CONNECTION_KVA_TIER)
    return overlay.brussels_osp_by_tier.get(tier, 0.0)


def _annual_static_fees(
    snapshot: SupplierSnapshot, meter: MeterType, entry: ConfigEntry
) -> float:
    """Fixed EUR/year fees that do not depend on consumption: the supplier
    yearly fixed fee (for ``meter``), twelve times the monthly energy-fund
    levy, the digital-meter data-management charge and the Brussels Brugel
    OSP fee.

    Shared by the live YTD sensor, the backfill accrual and the config-flow
    annual estimate so a new static-fee component is added in one place
    instead of drifting between the three paths.
    """
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    return (
        float(yearly_fixed_fee_for_meter(snapshot.energy, meter) or 0.0)
        + 12.0 * float(snapshot.taxes.energy_fund_eur_per_month or 0.0)
        + (overlay.data_management_per_year if overlay is not None else 0.0)
        + _brussels_osp_fee(overlay, entry)
    )


def _month_snapshot_cache(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    contract: str,
    region: str,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
) -> Callable[[date], Awaitable[SupplierSnapshot]]:
    """Return a memoised ``snap_for(month_first)`` fetching each delivery
    month's effective snapshot once.

    The live YTD cost and both backfill passes walk the same months
    repeatedly; the per-call cache keeps archive fetches to at most one
    per month.
    """
    cache: dict[date, SupplierSnapshot] = {}

    async def _snap_for(month_first: date) -> SupplierSnapshot:
        if month_first not in cache:
            cache[month_first] = await _effective_snapshot_for_month(
                hass, session, extractor, contract, region, month_first, snapshot, entry
            )
        return cache[month_first]

    return _snap_for


# A spot cache grouped by local (year, month) so several months' means can be
# read without rescanning the whole year (and re-localising every hour) once
# per month. Each bucket keeps (utc_ts, value) pairs so the SPP-weighted mean
# can still resolve each hour's Synergrid weight.
_SpotMonthBucket = dict[tuple[int, int], list[tuple[datetime, float]]]


def _bucket_by_local_month(spots: dict[datetime, float]) -> _SpotMonthBucket:
    """Group ``spots`` by their local ``(year, month)``, doing the timezone
    conversion once per entry.

    The YTD walk reads up to twelve months' means from the same year long
    cache; without bucketing each month rescans the whole dict and calls
    ``dt_util.as_local`` on every hour. Bucketing once turns each month into a
    dict lookup. Insertion order follows ``spots`` so a mean over a bucket
    matches the pre-bucket scan bit for bit.
    """
    buckets: _SpotMonthBucket = {}
    for ts, value in spots.items():
        local = dt_util.as_local(ts)
        buckets.setdefault((local.year, local.month), []).append((ts, value))
    return buckets


def _month_mean(bucket: _SpotMonthBucket, year: int, month: int) -> float | None:
    """Arithmetic mean of the (year, month) bucket, or ``None`` if empty."""
    entries = bucket.get((year, month))
    return fmean([value for _, value in entries]) if entries else None


def _mean_of_month(spots: dict[datetime, float], year: int, month: int) -> float | None:
    """Arithmetic mean of the spot values whose local timestamp falls in
    (year, month). Returns ``None`` when that month has no cached hours.

    Convenience wrapper for callers holding a raw spot dict; the per-tick hot
    paths bucket once up front and call :func:`_month_mean` directly.
    """
    return _month_mean(_bucket_by_local_month(spots), year, month)


def _drop_future_spots(
    spots: dict[datetime, float], today: date
) -> dict[datetime, float]:
    """Keep only spots whose local date is ``today`` or earlier.

    The live monthly mean must average the same [Jan 1 .. today] window the
    YTD path bills on. Tomorrow's day-ahead curve is present in the fetched
    ``spot_prices`` after ~13:00 CET; leaving it in would nudge the flat
    spot-monthly rate and the mean-baked injection above what
    ``current_year_cost`` charges for the same month."""
    return {ts: v for ts, v in spots.items() if dt_util.as_local(ts).date() <= today}


def _spp_month_mean(
    bucket: _SpotMonthBucket,
    weights: SppWeights,
    year: int,
    month: int,
) -> float | None:
    """SPP-weighted mean of the (year, month) bucket's prices, or ``None``.

    Weights each price by the Synergrid profile weight for its UTC hour. The
    weights span the whole year, so a boundary hour (local month != UTC month)
    still finds its weight. Returns ``None`` when no weighted hour is available.
    """
    num = 0.0
    den = 0.0
    for ts, price in bucket.get((year, month), ()):
        utc = ts.astimezone(UTC)
        weight = weights.get((utc.month, utc.day, utc.hour))
        if weight is None:
            continue
        num += price * weight
        den += weight
    return num / den if den else None


def _spp_weighted_month_mean(
    spots: dict[datetime, float],
    weights: SppWeights,
    year: int,
    month: int,
) -> float | None:
    """SPP-weighted mean of the (year, month)'s prices, or ``None``.

    Convenience wrapper over :func:`_spp_month_mean` for callers holding a raw
    spot dict; selects the same local-delivery-month hours as
    :func:`_mean_of_month`. The per-tick hot path buckets once and calls
    :func:`_spp_month_mean` directly.
    """
    return _spp_month_mean(_bucket_by_local_month(spots), weights, year, month)


def _spp_weighting_enabled(entry: ConfigEntry) -> bool:
    """True when this entry actually uses SPP-weighted injection: a custom
    monthly-average contract, injection regime, formula injection, flag set.

    The contract + injection-mode conditions matter: without them a stale flag
    left after switching a monthly entry to fixed/dynamic (the options flow
    reseeds it), or a monthly entry on flat-rate injection, would trigger the
    52 MB profile download even though the weights are then discarded."""
    return (
        entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM
        and entry.data.get(CONF_CONTRACT) == CUSTOM_CONTRACT_MONTHLY
        and bool(entry.data.get(CONF_CUSTOM_INJECTION_SPP_WEIGHTED))
        and entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION
        and entry.data.get(CONF_CUSTOM_INJECTION_MODE) == CUSTOM_INJECTION_MODE_FORMULA
    )


def _bake_monthly_injection(
    snapshot: SupplierSnapshot, mean: float | None
) -> SupplierSnapshot:
    """Turn a mean-indexed injection formula into this month's flat indicative.

    A spot-monthly contract's injection is indexed to the same monthly mean as
    its energy (e.g. the Mega groepsaankoop ``SPP_mean * 0.96 - 0.9``), not the
    live hourly spot. Baking the formula into ``current`` routes it through the
    monthly-indicative injection path; the floor (if any) is applied there.
    A flat ``current`` injection or none is returned unchanged.
    """
    inj = snapshot.injection
    if inj is None or inj.factor is None or inj.base is None:
        return snapshot
    current = None if mean is None else inj.factor * mean + inj.base
    return replace(
        snapshot,
        injection=replace(inj, current=current, factor=None, base=None),
    )


def _injection_needs_spot(snapshot: SupplierSnapshot, entry: ConfigEntry) -> bool:
    """True when pricing this entry's injection requires an ENTSO-E spot
    even though the ENERGY contract isn't dynamic.

    The case is a static-energy card (Fixed / Variable / TOU) whose
    injection is a per-hour spot formula (``factor``/``base``) with no
    printed monthly indicative (``current is None``): Cociter Variable.
    Such a card doesn't fetch ENTSO-E spots through the DynamicRates
    energy path, so the coordinator must fetch spots for it too (and the
    config flow must collect an API key) to credit the injection.
    DynamicRates contracts already fetch spots via the energy
    path and are excluded here. Only relevant on the injection regime.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    inj = snapshot.injection
    return (
        inj is not None
        and inj.current is None
        and inj.factor is not None
        and inj.base is not None
        and not isinstance(snapshot.energy, DynamicRates)
    )


def _tou_injection_rate(
    inj: InjectionRates, energy: EnergyRates, when: datetime
) -> float | None:
    """Per-slot injection rate for a time-of-use contract whose feed-in
    tariff varies by slot (Engie Empower Flextime).

    Returns ``None`` when the contract isn't TOU or its injection is a
    single rate (``peak`` unset), so the caller falls back to the normal
    current / factor+base path. Uses the energy contract's own
    ``weekend_rule`` so injection and consumption agree on the slot for a
    given hour.
    """
    if not isinstance(energy, TimeOfUseRates) or inj.peak is None:
        return None
    slot = tou_slot(when, energy.weekend_rule)
    if slot == "peak":
        return inj.peak
    if slot == "transition":
        return inj.transition
    return inj.offpeak


def _floor_injection(rate: float | None, inj: InjectionRates) -> float | None:
    """Clamp an injection rate at 0 when the contract forbids negatives
    (``floor_at_zero``). A ``None`` rate (no data) passes through unchanged."""
    if rate is None or not inj.floor_at_zero:
        return rate
    return max(rate, 0.0)


def _injection_price_for_slot(
    inj: InjectionRates,
    energy: EnergyRates,
    spot: float | None,
    when: datetime,
) -> float | None:
    """Injection price in EUR/kWh for a single slot.

    The per-slot core shared by the live current-hour scalar and the
    today/tomorrow injection array. Priority (identical to the historical
    walk): a per-slot TOU rate first (Engie Empower Flextime), then the
    spot-indexed formula ``factor*spot + base`` when the contract is
    spot-indexed, otherwise the printed monthly ``current`` indicative.
    ``spot`` is the already-resolved spot for ``when``'s billing slot (None
    when unavailable); the spot branch returns None rather than fabricate a
    value when it has no spot.

    The spot branch fires only when the energy bills per hour (DynamicRates)
    OR the injection is a spot formula with no monthly indicative (``current``
    is None) -- e.g. Cociter Variable. A static-energy contract whose injection
    carries a MONTHLY index but also a printed ``current`` (Ecofix Flexy's
    BELPEX-SPP-M, EBEM Groen Variabel / B@sic+'s SPP0) uses that realized
    monthly rate instead, keeping the live sensor consistent with the YTD
    credit. Do NOT drop this guard: without it a flat monthly-indicative
    credit would flip to a spot-varying one on the several dynamic-injection
    cards that publish BOTH a ``current`` and ``factor``/``base``.
    """
    tou_rate = _tou_injection_rate(inj, energy, when)
    if tou_rate is not None:
        return tou_rate
    if (
        inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    ):
        if spot is None:
            return None
        return _floor_injection(inj.factor * spot + inj.base, inj)
    return _floor_injection(inj.current, inj)


def _now_slot_spot(
    energy: EnergyRates, spot_prices: dict[datetime, float]
) -> float | None:
    """ENTSO-E spot for the current billing slot, matching the grid the
    contract bills on so an Engie injection price tracks the current
    quarter-hour, not the hourly mean. Falls back to the nearest cached spot
    within one billing slot (15 min quarter-hourly, 1 h otherwise); returns
    None when none are cached or none are within range."""
    if not spot_prices:
        return None
    resolution = (
        RESOLUTION_QUARTER if _energy_is_quarter_hourly(energy) else RESOLUTION_HOURLY
    )
    now_slot = slot_start(dt_util.utcnow(), resolution)
    spot = spot_prices.get(now_slot)
    if spot is None:
        nearest = min(
            spot_prices.keys(),
            key=lambda h: abs((h - now_slot).total_seconds()),
        )
        # A fixed 1 h window let a quarter-hourly injection price use a spot
        # up to four slots away.
        max_gap = 900.0 if resolution == RESOLUTION_QUARTER else 3600.0
        if abs((nearest - now_slot).total_seconds()) > max_gap:
            return None
        spot = spot_prices[nearest]
    return spot


def _compute_injection_price(
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    spot_prices: dict[datetime, float],
) -> float | None:
    """Current-hour injection price in EUR/kWh for HA Energy's price entity.

    Only returned when the user is on the injection regime AND the supplier's
    snapshot has injection data. Prefers a per-slot TOU rate (Engie Empower
    Flextime), then the formula+spot when a spot is available (dynamic
    contracts), otherwise falls back to the snapshot's static "current"
    indicative (Eneco Fix/Flex monthly value).
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return None
    inj = snapshot.injection
    if inj is None:
        return None
    return _injection_price_for_slot(
        inj,
        snapshot.energy,
        _now_slot_spot(snapshot.energy, spot_prices),
        dt_util.now(),
    )


def _injection_varies_intraday(inj: InjectionRates, energy: EnergyRates) -> bool:
    """True when this contract's injection changes across the day -- a TOU
    schedule (Engie Empower Flextime) or a spot-indexed formula (every dynamic
    contract plus Cociter Tarif Variable). Flat monthly-indicative, fixed and
    (mean-baked) spot-monthly injection is constant intra-day, so no per-hour
    array is worth emitting for it. Mirrors the branch conditions of
    ``_injection_price_for_slot``."""
    if isinstance(energy, TimeOfUseRates) and inj.peak is not None:
        return True
    return (
        inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    )


def _historical_injection_rate(
    injection: InjectionRates | None,
    spot: float | None = None,
    *,
    energy: EnergyRates | None = None,
    when: datetime | None = None,
) -> float | None:
    """Best-effort EUR/kWh injection rate for a *past* hour.

    Mirrors the live ``_compute_injection_price`` priority: a per-slot TOU
    rate first (Engie Empower Flextime, when ``energy`` + ``when`` are
    given), then the spot-indexed formula ``factor*spot + base`` when both
    the formula and a historical spot are available, falling back to the
    monthly indicative ``current`` otherwise. Several dynamic-injection
    contracts (Engie, OCTA+, TotalEnergies, Luminus, Mega) publish BOTH a
    ``current`` indicative and ``factor``/``base``; checking ``current``
    first made the YTD credit use the flat indicative while the live
    injection-price sensor used the spot formula, so the two user-facing
    numbers diverged. Static contracts have no spot, so they fall through
    to ``current``.
    """
    if injection is None:
        return None
    if energy is not None and when is not None:
        tou_rate = _tou_injection_rate(injection, energy, when)
        if tou_rate is not None:
            return tou_rate
    if injection.factor is not None and injection.base is not None and spot is not None:
        return _floor_injection(injection.factor * spot + injection.base, injection)
    if injection.current is not None:
        return _floor_injection(injection.current, injection)
    return None


def _prosumer_monthly_fee(
    overlay: DsoOverlay | None, snapshot: SupplierSnapshot, kva: float
) -> float:
    """Monthly prosumer (compensation-regime) fee for ``kva`` of inverter.

    Sums the DSO per-kVA/year tariff and the supplier-side compensation
    forfait (Cociter Variable), the latter already TVAC so it is summed
    raw, then divides to a monthly amount. Callers gate this to Walloon
    compensation installs; a missing rate contributes zero.
    """
    dso_rate = (
        overlay.prosumer_eur_per_kva_year
        if overlay is not None and overlay.prosumer_eur_per_kva_year is not None
        else 0.0
    )
    supplier_rate = snapshot.supplier_prosumer_eur_per_kva_year or 0.0
    return kva * (dso_rate + supplier_rate) / 12.0


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
    # Compensation is Walloon-only: a Flanders PV owner is either on net-
    # metering (no capacity tariff, but that regime is not modelled here) or
    # on a digital meter paying the capaciteitstarief. Billing the prosumer
    # fee in Flanders on top of the always-billed capacity tariff would
    # double-count grid-recovery, so gate it to Wallonia.
    if entry.data.get(CONF_REGION) != REGION_WALLONIA:
        return 0.0
    try:
        kva = float(entry.data.get(CONF_SOLAR_KVA, 0.0))
    except (TypeError, ValueError):
        return 0.0
    if kva <= 0.0:
        return 0.0
    overlay = snapshot.dsos.get(entry.data.get(CONF_DSO, ""))
    return _prosumer_monthly_fee(overlay, snapshot, kva)


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

    Requests the ``change`` in kWh via ``units={"energy": "kWh"}`` so a
    meter sensor that stores its statistics in Wh or MWh is normalised by
    HA's EnergyConverter rather than read as raw kWh, which would bill the
    user 1000x too much (Wh) or too little (MWh). The OptionsFlow picker
    restricts the choice to device_class=energy but not the unit, so a
    Wh / MWh sensor is a legitimate, reachable selection.

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
    # Anchor end_dt on the next local midnight so the bucket containing
    # ``end`` is included. ``start_of_local_day(end).astimezone(UTC) +
    # timedelta(days=1)`` would be exactly 24 UTC hours later, which
    # mis-aligns by one hour on Brussels DST seam days (the next local
    # midnight is 23 or 25 UTC hours away). Computing
    # start_of_local_day(end + 1 day) keeps the cap on the right local
    # boundary year-round.
    end_dt = dt_util.start_of_local_day(end + timedelta(days=1)).astimezone(UTC)
    try:
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            start_dt,
            end_dt,
            {entity_id},
            period,
            {"energy": "kWh"},
            {"change"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return []
    rows: list[Any] = list(stats.get(entity_id, []))
    return rows


async def _live_today_kwh(
    hass: HomeAssistant, entity_id: str, today: date
) -> float | None:
    """Today's kWh for ``entity_id`` from the live meter, or ``None``.

    Reads ``current cumulative total - total at local midnight`` from the
    state machine and the recorder's state history, bypassing the long-term
    daily statistics the past-day path relies on. This keeps the running year
    cost tracking today's consumption in real time and, crucially, keeps it
    moving when statistics compilation lags or stalls -- states are still
    recorded regardless. ``None`` means "no reliable live reading": the meter
    is unavailable / non-numeric, has no reading at midnight yet, or carries a
    unit that can't be converted to kWh; the caller then keeps the daily
    statistic as a fallback rather than risk a wrong figure.
    """
    state = hass.states.get(entity_id)
    if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        current = float(state.state)
    except (TypeError, ValueError):
        return None
    unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

    midnight = dt_util.start_of_local_day(today).astimezone(UTC)
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.history import (
            get_significant_states,
        )
    except ImportError:
        return None
    try:
        history = await get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                midnight,
                midnight + timedelta(seconds=1),
                [entity_id],
                include_start_time_state=True,
                significant_changes_only=False,
                no_attributes=True,
            )
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return None
    rows = history.get(entity_id, [])
    if not rows or not isinstance(rows[0], State):
        return None
    try:
        opening = float(rows[0].state)
    except (TypeError, ValueError):
        return None
    delta = current - opening
    if delta < 0.0:
        # A ``total_increasing`` meter that reset since midnight: everything
        # it has counted since the reset is today's consumption.
        delta = current
    if unit == UnitOfEnergy.KILO_WATT_HOUR:
        return delta
    try:
        return EnergyConverter.convert(delta, unit, UnitOfEnergy.KILO_WATT_HOUR)
    except HomeAssistantError:
        # Unknown / non-energy unit: fall back to the normalized daily
        # statistic rather than risk a 1000x mis-bill from an assumed unit.
        return None


async def _recorder_daily_kwh(
    hass: HomeAssistant, entity_id: str, start: date, end: date
) -> dict[date, float]:
    """Per-day kWh deltas for ``entity_id`` keyed by local-day date.

    Past days come from the recorder's long-term daily statistics. When
    ``end`` is today, that day is overridden with a live meter reading (see
    :func:`_live_today_kwh`) so the running year cost tracks today's usage in
    real time and does not freeze if statistics compilation lags or stalls;
    it falls back to the daily statistic when no live reading is available.
    """
    out: dict[date, float] = {}
    for row in await _recorder_rows(hass, entity_id, start, end, "day"):
        ts = row.get("start")
        delta = row.get("change")
        if ts is None or delta is None:
            continue
        local_day = dt_util.as_local(datetime.fromtimestamp(ts, tz=UTC)).date()
        out[local_day] = float(delta)
    if end == dt_util.now().date():
        live_today = await _live_today_kwh(hass, entity_id, end)
        if live_today is not None:
            out[end] = live_today
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


async def _sum_hourly_kwh(
    hass: HomeAssistant,
    entity_ids: Iterable[str],
    start: date,
    end: date,
) -> dict[datetime, float]:
    """Per-UTC-hour kWh summed across ``entity_ids`` into one dict.

    A house with several consumption (or injection) sensors totals them
    hour by hour; used by the live YTD cost, the injection-credit and the
    backfill accrual so the binning is written once.
    """
    out: dict[datetime, float] = {}
    for entity_id in entity_ids:
        for utc_hour, kwh in (
            await _recorder_hourly_kwh(hass, entity_id, start, end)
        ).items():
            out[utc_hour] = out.get(utc_hour, 0.0) + kwh
    return out


async def _recorder_daily_band_ratio(
    hass: HomeAssistant, entity_id: str, start: date, end: date, region: str
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
        if is_offpeak(local, region):
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
            out[day] = _default_band_ratio_for(day, region)
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
    region = entry.data.get(CONF_REGION, "")
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
            ratios = await _recorder_daily_band_ratio(
                hass, total_id, jan1, today, region
            )
            for day, total in per_day.items():
                d_ratio, n_ratio = ratios.get(day, _default_band_ratio_for(day, region))
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


def _default_band_ratio_for(day: date, region: str) -> tuple[float, float]:
    """Time-weighted (day_ratio, night_ratio) fallback for a day with no
    hourly recorder stats yet.

    Assumes uniform consumption across the day's 24 hours (the most
    neutral guess without a usage profile) and uses the region's
    bi-horaire schedule (so a Wallonia day picks up the 11-17 off-peak
    window, a Flanders weekday holiday stays peak). Replaces a previous
    hardcoded (1.0, 0.0) default that systematically pushed totals into
    the peak band when hourly stats lagged daily stats."""
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
        if not is_offpeak(when, region):
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
        snap_m = await _effective_snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot, entry
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
    meter: MeterType | None = None,
) -> float:
    """Pro-rated YTD total of yearly_fixed_fee + 12*energy_fund using each
    month's archived snapshot.

    ``meter`` defaults to the entry's meter; the compare flow passes a
    meter override so the fixed fee is billed at the same meter the energy
    is billed at (e.g. an exclusive-night override).

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
        annual = _annual_static_fees(
            snap_m, meter or entry.data.get(CONF_METER, METER_MONO), entry
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
    # Compensation is Walloon-only (see _compute_prosumer): gate it so a
    # Flanders entry never bills prosumer on top of the capacity tariff.
    if entry.data.get(CONF_REGION) != REGION_WALLONIA:
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
        monthly_fee = _prosumer_monthly_fee(overlay, snap_m, kva)
        if monthly_fee == 0.0:
            continue
        total += monthly_fee * (days_in_ytd / days_in_full_month)
    return total


async def _ytd_capacity(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    billed_peak_kw: float,
    *,
    contract: str | None = None,
) -> float:
    """Sum the monthly Flemish capacity charge across YTD, reading each
    month's archived DSO overlay so a VREG indexation landing mid-year is
    honoured for the months it applies to.

    ``billed_peak_kw`` is the CURRENT gemiddelde maandpiek, applied to every
    month of the year rather than reconstructed per month. Reconstruction is
    not available in general: the rolling window holds at most twelve months
    and an entry installed mid-year has no history for the months before it,
    where Fluvius billed against meter history we never saw. The current mean
    is the honest stand-in precisely because it is a twelve-month mean, so it
    moves slowly and is close to what each month of this year was billed on.
    """
    if entry.data.get(CONF_REGION) != REGION_FLANDERS:
        return 0.0
    dso = entry.data.get(CONF_DSO)
    if dso is None:
        return 0.0

    total = 0.0
    async for snap_m, _, days_in_full_month, days_in_ytd in _walk_ytd_months(
        hass, session, extractor, snapshot, entry, today, contract=contract
    ):
        overlay = snap_m.dsos.get(dso)
        if overlay is None or overlay.capacity_eur_per_kw_year is None:
            continue
        monthly = billed_peak_kw * overlay.capacity_eur_per_kw_year / 12.0
        total += monthly * (days_in_ytd / days_in_full_month)
    return total


def _kwh_sensor_ids(
    entry: ConfigEntry, side: str
) -> tuple[str | None, str | None, str | None]:
    """The (day, night, total) recorder entity ids configured for ``side``
    ("injection" or "consumption"); any element may be ``None``."""
    if side == "injection":
        return (
            entry.data.get(CONF_DAY_INJECTION_KWH),
            entry.data.get(CONF_NIGHT_INJECTION_KWH),
            entry.data.get(CONF_INJECTION_KWH),
        )
    return (
        entry.data.get(CONF_DAY_CONSUMPTION_KWH),
        entry.data.get(CONF_NIGHT_CONSUMPTION_KWH),
        entry.data.get(CONF_CONSUMPTION_KWH),
    )


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


def _spp_injection_spot(
    spot: float | None,
    *,
    monthly_mean: bool,
    spp_weights: SppWeights | None,
    historical_spots: dict[datetime, float] | None = None,
    bucket: _SpotMonthBucket | None = None,
    year: int,
    month: int,
    cache: dict[tuple[int, int], float | None],
) -> float | None:
    """The spot value to price mean-indexed injection at.

    Energy bills at the flat month-mean (``spot``); when the entry opted
    into SPP-weighted injection (a custom monthly contract) and the
    Synergrid profile is available, the injection credit instead uses the
    SPP-weighted month-mean, falling back to ``spot`` when the profile is
    missing for the month. ``cache`` memoises the per-month weighted mean.

    Callers that already bucketed the spot cache for the tick pass ``bucket``;
    the rest pass the raw ``historical_spots`` and it is bucketed here on the
    first miss for a month. Shared by the live YTD credit and the backfill
    accrual so the two price mean-indexed injection identically.
    """
    if not (monthly_mean and spp_weights is not None):
        return spot
    key = (year, month)
    if key not in cache:
        if bucket is None:
            if historical_spots is None:
                return spot
            bucket = _bucket_by_local_month(historical_spots)
        cache[key] = _spp_month_mean(bucket, spp_weights, year, month)
    weighted = cache[key]
    return weighted if weighted is not None else spot


async def _ytd_hourly_energy(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    contract: str | None = None,
    meter: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    monthly_mean: bool = False,
    spp_weights: SppWeights | None = None,
) -> float | None:
    """YTD energy cost for hourly-billed contracts (TOU + dynamic).

    Bins the recorder's hourly kWh deltas through ``compute_breakdown``
    at each local hour, picking up the TOU slot rate (or the dynamic
    factor*spot+base) from the supplier and the bi-hourly / Impact
    distribution band from the user's DSO mode in one call. Reads from
    ``CONF_CONSUMPTION_KWH`` (single totals) when available, else sums
    the four day/night register sensors at hourly granularity. Each
    side -- consumption, injection -- is resolved independently,
    mirroring the static-path behaviour: a user with only injection
    wired (e.g. an inverter exposing solar export but no smart-meter
    consumption sensor) still gets the injection credit recognised.

    ``historical_spots`` is required for dynamic contracts (factor*spot+
    base needs a spot per hour); hours missing from the cache are
    skipped so a partial backfill still produces a meaningful YTD
    instead of falling all the way back to the fees-only floor. TOU
    callers pass ``None`` and every hour gets billed at the slot rate.

    Quarter-hourly dynamic contracts (Engie, Cociter, EBEM, Ecofix,
    OCTA+, Ecopower DBS) bill the live price on 15-minute slots, but the
    recorder only retains hourly long-term statistics, so this YTD replay
    aggregates consumption / injection to the clock hour and prices each
    hour at its hourly spot. When intra-hour load correlates with the
    intra-hour price the YTD total is a close approximation, not a
    bit-exact reconciliation with the live 15-minute sensor.

    Solar handling is uniform across both paths:
      - ``compensation``: per-hour ``(cons - inj) * all_in``, summed
        and clamped at zero (Walloon meter forfeits surplus).
      - ``injection``: per-hour ``cons * all_in - inj * inj_rate``
        where ``inj_rate`` is the supplier's monthly indicative for TOU
        and ``factor*spot+base`` for dynamic at that hour's spot.
      - ``none``: per-hour ``cons * all_in``.

    Returns ``None`` only when neither side has any meters wired (the
    caller surfaces the fees-only floor).
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
    cons_per_hour = await _sum_hourly_kwh(hass, cons_ids, jan1, today)
    inj_per_hour = await _sum_hourly_kwh(hass, inj_ids, jan1, today)

    _snap_for = _month_snapshot_cache(
        hass, session, extractor, contract, region, snapshot, entry
    )

    # Spot-monthly contracts bill every hour of a delivery month at that
    # month's mean spot (energy and mean-indexed injection alike); cache the
    # mean per month so it's computed once.
    month_means: dict[tuple[int, int], float | None] = {}
    # SPP-weighted per-month injection means, when the entry opted in. Energy
    # keeps the flat mean above; only the injection credit uses these.
    month_spp: dict[tuple[int, int], float | None] = {}
    # Bucket the year's spots by local month once so each month's mean is a
    # lookup rather than a full-year rescan (the loop reads up to twelve
    # distinct months). Only the spot-monthly path reads it; a dynamic
    # contract prices per hour, so skip the bucketing there entirely.
    month_bucket = (
        _bucket_by_local_month(historical_spots)
        if monthly_mean and historical_spots
        else {}
    )

    energy_cost = 0.0
    # Iterate the union of both sides so an injection-only wiring
    # still contributes its credit (mirroring _resolve_daily_kwh).
    for utc_hour in cons_per_hour.keys() | inj_per_hour.keys():
        local = dt_util.as_local(utc_hour)
        spot: float | None = None
        if monthly_mean:
            key = (local.year, local.month)
            if key not in month_means:
                month_means[key] = _month_mean(month_bucket, *key)
            spot = month_means[key]
            if spot is None:
                continue
        elif historical_spots is not None:
            spot = historical_spots.get(utc_hour)
            if spot is None:
                continue
        snap_h = await _snap_for(date(local.year, local.month, 1))
        try:
            bd = compute_breakdown(snap_h, dso, region, local, spot, meter, dso_mode)
        except (KeyError, ValueError):
            # Missing DSO row or non-static rate kind: skip this hour.
            continue
        kwh_cons = cons_per_hour.get(utc_hour, 0.0)
        kwh_inj = inj_per_hour.get(utc_hour, 0.0)
        if regime == SOLAR_REGIME_COMPENSATION:
            d_cost = (kwh_cons - kwh_inj) * bd.all_in
        elif regime == SOLAR_REGIME_INJECTION:
            d_cost = kwh_cons * bd.all_in
            # Energy bills at the flat month-mean (spot); the injection credit
            # uses the SPP-weighted month-mean when the entry opted in, falling
            # back to the flat mean when the profile is missing for the month.
            inj_spot = _spp_injection_spot(
                spot,
                monthly_mean=monthly_mean,
                spp_weights=spp_weights,
                bucket=month_bucket,
                year=local.year,
                month=local.month,
                cache=month_spp,
            )
            inj_rate = _historical_injection_rate(
                snap_h.injection, inj_spot, energy=snap_h.energy, when=local
            )
            if inj_rate is not None:
                d_cost -= kwh_inj * inj_rate
        else:
            d_cost = kwh_cons * bd.all_in
        energy_cost += d_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        energy_cost = max(energy_cost, 0.0)
    return energy_cost


async def _ytd_spot_injection_credit(
    hass: HomeAssistant,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    historical_spots: dict[datetime, float] | None,
) -> float:
    """YTD solar-injection credit (EUR) for a contract whose injection is
    a per-hour spot formula with no monthly indicative.

    Sums per-hour injected kWh * (factor*spot + base) from the recorder's
    hourly statistics and the persistent historical-spot cache, for
    Cociter Variable -- a static-energy card that publishes an hourly
    BELPEX injection formula but no fixed credit. The static per-day YTD
    path can't price these (no spot per
    day), so this isolated term replays the spots the same way the
    dynamic energy path does, and the caller subtracts it from the bill.

    Returns 0.0 (a no-op) unless the injection is exactly that shape
    (``factor``/``base`` set, ``current is None``), spots are cached, and
    an injection sensor is wired. Hours with no cached spot are skipped.
    """
    inj = snapshot.injection
    if (
        inj is None
        or inj.factor is None
        or inj.base is None
        or inj.current is not None
        or not historical_spots
    ):
        return 0.0
    inj_ids = _hourly_injection_sensors(entry)
    if not inj_ids:
        return 0.0
    jan1 = date(today.year, 1, 1)
    per_hour = await _sum_hourly_kwh(hass, inj_ids, jan1, today)
    credit = 0.0
    for utc_hour, kwh in per_hour.items():
        spot = historical_spots.get(utc_hour)
        if spot is None:
            continue
        # Route through the shared helper so the floor_at_zero clamp the live
        # scalar and array apply is honoured here too, rather than summing the
        # raw factor*spot+base and diverging on a negative-spot hour.
        credit += kwh * (_historical_injection_rate(inj, spot) or 0.0)
    return credit


async def _compute_current_year_cost(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    contract_override: str | None = None,
    meter_override: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    spp_weights: SppWeights | None = None,
    breakdown: dict[str, float] | None = None,
    billed_peak_kw: float = 0.0,
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
    OCTA+ Dynamic, etc.) replay historical hourly ENTSO-E spots from
    the coordinator's persistent cache (filled lazily by
    ``_ensure_historical_spots``). Each past kWh is then billed at
    its actual ``factor*spot+base`` rate via ``compute_breakdown``,
    same code path as the live current_price. Hours with no spot in
    the cache (cold start before the backfill, or a gap left by an
    ENTSO-E publication outage) are skipped rather than zeroed; the
    caller still gets the fees-only floor when the cache is entirely
    empty.

    Returns ``None`` only when there is no meter input wired at all
    AND no snapshot to show fees against. In every other case the
    function returns a number, falling back to the fees-only floor
    rather than exposing ``unknown`` to the user.

    The whole year is recomputed from scratch on every coordinator tick
    by design: today's cost grows each hour, and prior days are NOT safely
    immutable between ticks (a late ENTSO-E spot fill or a backfill
    correction changes a past day's rate). Memoizing prior-day totals
    would risk serving a stale YTD; the full replay is O(hours-in-year)
    pure arithmetic (~100 ms by December), which is negligible at the
    hourly update cadence, so keep it simple.

    ``breakdown`` is an optional diagnostic out-dict. When passed (only the
    live coordinator does; the compare / backfill callers leave it ``None``),
    the static per-day branch records the YTD and today kWh totals, the
    pre-clamp raw energy term and the fees floor into it, so the
    current_year_cost sensor can surface them as attributes. This piggybacks
    on the walk already happening here rather than reading the recorder twice.
    It stays empty for the dynamic / spot-monthly / TOU (hourly) branches,
    which don't produce daily kWh totals.
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

    # Dispatch on the EFFECTIVE energy leg. A variable contract with a start
    # date re-prices its signing cohort to a SpotMonthlyRates leg, which bills
    # on the monthly-mean hourly path rather than the variable static daily
    # path. _cohort_energy_leg returns None for the compare flow and for
    # contracts without a start date, leaving the current card's kind. The
    # per-month walk resolves the same cohort leg through
    # _effective_snapshot_for_month, so dispatch and per-month pricing agree.
    cohort_energy = await _cohort_energy_leg(
        hass, session, extractor, contract, region, entry, snapshot
    )
    eff_energy = snapshot.energy if cohort_energy is None else cohort_energy

    jan1 = date(today.year, 1, 1)

    static_fees = await _ytd_static_fees(
        hass, session, extractor, snapshot, entry, today, contract=contract, meter=meter
    )
    prosumer_ytd = await _ytd_prosumer(
        hass, session, extractor, snapshot, entry, today, contract=contract
    )
    capacity_ytd = await _ytd_capacity(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        today,
        billed_peak_kw,
        contract=contract,
    )
    fees = static_fees + prosumer_ytd + capacity_ytd

    # Dynamic contracts replay historical hourly ENTSO-E spots so each
    # past kWh hits its actual factor*spot+base rate. Caller passes the
    # spot cache (the coordinator persists it between runs); when
    # absent or empty we fall back to the fees-only floor.
    if isinstance(eff_energy, DynamicRates):
        if not historical_spots:
            return fees
        dyn_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
            historical_spots=historical_spots,
        )
        if dyn_energy is None:
            return fees
        return dyn_energy + fees

    # Spot-monthly contracts bill each past hour at its delivery month's mean
    # spot (a flat rate within the month); the hourly replay threads that mean
    # in place of the live spot and credits mean-indexed injection the same way.
    if isinstance(eff_energy, SpotMonthlyRates):
        if not historical_spots:
            return fees
        monthly_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            contract=contract,
            meter=meter,
            historical_spots=historical_spots,
            monthly_mean=True,
            spp_weights=spp_weights,
        )
        if monthly_energy is None:
            return fees
        return monthly_energy + fees

    # Per-hour billing is required when the supplier's energy rates
    # vary by hour (TOU + Impact energy contracts), when the DSO bills
    # per Impact band (PIC / MEDIUM / ECO change with hour-of-day), or
    # for an exclusive_night meter (its energy + distribution use the
    # dedicated exclusive-night rates, which the static per-day branch's
    # single/peak/offpeak breakdowns don't carry -- so without this it
    # would bill the YTD at the day rate while the live sensor uses the
    # cheaper exclusive-night rate). All go through the same hourly path,
    # which routes the meter through compute_breakdown.
    needs_hourly = (
        isinstance(eff_energy, (TimeOfUseRates, ImpactRates))
        or dso_mode == DSO_MODE_IMPACT
        or meter == METER_EXCLUSIVE_NIGHT
    )
    if needs_hourly:
        hourly_energy = await _ytd_hourly_energy(
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
        if regime == SOLAR_REGIME_INJECTION:
            # _ytd_hourly_energy here runs without historical spots, so a
            # spot-indexed injection (Cociter Variable) credited nothing
            # above. Apply the same per-hour spot-replayed credit the
            # daily path uses; a no-op for monthly-indicative injection.
            hourly_energy -= await _ytd_spot_injection_credit(
                hass, snapshot, entry, today, historical_spots
            )
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
        snap_m = await _effective_snapshot_for_month(
            hass, session, extractor, contract, region, month_first, snapshot, entry
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

    # Raw energy term before the compensation zero-floor: a negative value
    # here is what the clamp below hides, so surface it for diagnostics.
    energy_ytd_raw = energy_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        # YTD clamp at zero: the bill never goes negative, surplus
        # injection past consumption is forfeited (by most Walloon
        # suppliers).
        energy_cost = max(energy_cost, 0.0)

    if regime == SOLAR_REGIME_INJECTION:
        # Spot-indexed injection on a static-energy contract (Cociter
        # Variable): the daily loop above credited nothing for it (its
        # injection has no monthly indicative), so subtract the per-hour
        # spot-replayed credit
        # here. A no-op (0.0) for every other contract.
        energy_cost -= await _ytd_spot_injection_credit(
            hass, snapshot, entry, today, historical_spots
        )
        # This regime has no compensation clamp, so the billed energy is
        # already the raw energy term.
        energy_ytd_raw = energy_cost

    if breakdown is not None:
        breakdown["consumption_ytd_kwh"] = sum(r[0] + r[1] for r in daily_kwh.values())
        breakdown["injection_ytd_kwh"] = sum(r[2] + r[3] for r in daily_kwh.values())
        today_kwh = daily_kwh.get(today, (0.0, 0.0, 0.0, 0.0))
        breakdown["consumption_today_kwh"] = today_kwh[0] + today_kwh[1]
        breakdown["injection_today_kwh"] = today_kwh[2] + today_kwh[3]
        breakdown["energy_ytd_raw_eur"] = energy_ytd_raw
        breakdown["fees_ytd_eur"] = fees

    return energy_cost + fees


# ---- snapshot serialization for the HA Store ----------------------------------


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
# v9: DynamicRates gained ``quarter_hourly``. Bump so a cached dynamic
# snapshot from a pre-15-min release (Engie, Cociter, EBEM, Ecofix) is
# dropped and re-fetched with the flag set, rather than lingering on the
# hourly default until the snapshot next refreshes. The probe-based
# suppliers (Cociter, EBEM, Ecofix) would otherwise keep the stale flag
# for weeks, until their next monthly card changes the probe key.
# v10: OCTA+ Dynamic was missed by the v9 sweep; it indexes on the
# 15-minute Epex spot and now sets ``quarter_hourly`` too. Bump so a
# cached OCTA+ dynamic snapshot is dropped and re-fetched with the flag
# set rather than lingering on the hourly default.
# v11: snapshots gained supplier_prosumer_eur_per_kva_year (Cociter's
# compensation-regime PV forfait). Bump so a cached Cociter Variable
# snapshot is re-fetched with the forfait parsed instead of None.
# v12: InjectionRates gained per-slot peak/transition/offpeak (Engie
# Empower Flextime's per-slot feed-in tariff). Bump so a cached Flextime
# snapshot is re-fetched with the triplet instead of the flat single rate.
# v13: the July 2026 Eneco cards dropped the "/ VALORISATIE" suffix from
# the injection heading, so 0.8.3 parsed every Eneco injection to None and
# cached it. 0.8.4 fixed the anchor but probe-based freshness keeps serving
# that stale None until Eneco republishes. Bump so the mis-parsed snapshot
# is dropped and re-fetched with the injection block populated.
# v14: added the SpotMonthlyRates energy kind (expert custom monthly-average
# supplier) and the InjectionRates.floor_at_zero flag. Bump so a cached
# snapshot from before the field existed is dropped and rebuilt with it.
# v15: VariableRates gained formula_factor / formula_base (numeric BELIX-style
# coefficients) so a variable contract with a contract start date re-prices its
# signing cohort against the current month's mean. Bump so a cached variable
# snapshot from before the fields existed is dropped and re-parsed with them.
_SNAPSHOT_SCHEMA_VERSION = 15


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
        "supplier_prosumer_eur_per_kva_year": snap.supplier_prosumer_eur_per_kva_year,
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
    elif energy_kind == "tou_impact":
        energy = ImpactRates(**energy_args)
    elif energy_kind == "spot_monthly":
        energy = SpotMonthlyRates(**energy_args)
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
        supplier_prosumer_eur_per_kva_year=data.get(
            "supplier_prosumer_eur_per_kva_year"
        ),
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
    if isinstance(energy, ImpactRates):
        return "tou_impact"
    if isinstance(energy, SpotMonthlyRates):
        return "spot_monthly"
    raise TypeError(f"unknown energy rates type {type(energy).__name__}")
