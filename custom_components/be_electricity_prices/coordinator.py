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

from .cohort import (
    _cohort_energy_leg,
)
from .fees import (
    _compute_capacity,
    _compute_prosumer,
)
from .injection import (
    _bake_monthly_injection,
    _compute_injection_price,
    _injection_hourly_on_cohort,
    _injection_needs_spot,
    _injection_price_for_slot,
    _injection_varies_intraday,
)
from .snapshot_store import (
    SNAPSHOT_REFRESH_HOURS,
    SNAPSHOT_STALE_DAYS,
    _MigratingStore,
    _SHARED_FAILURE_TTL,
    _SharedSnapshot,
    _bump_tuple_generation,
    _drop_monthly_rows,
    _resolve_snapshot,
    _shared_failed_fetches,
    _shared_lock,
    _shared_snapshots,
    _snapshot_from_dict,
    _snapshot_to_dict,
    _tuple_generation,
)
from .spot_stats import (
    _drop_future_spots,
    _energy_is_quarter_hourly,
    _mean_of_month,
    _spp_weighted_month_mean,
    _spp_weighting_enabled,
)
from .ytd_cost import (
    _compute_current_year_cost,
)

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
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
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    REGION_FLANDERS,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SOLAR_REGIME_INJECTION,
    STORAGE_VERSION,
    SUPPLIER_CUSTOM,
    UPDATE_INTERVAL_MINUTES,
    VREG_CAPACITY_FLOOR_KW,
)
from .pricing import (
    PriceBreakdown,
    compute_breakdown,
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
    EnergyRates,
    SupplierExtractor,
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


def _successor_for(supplier_id: str | None, region: str) -> SupplierExtractor | None:
    """The successor supplier, but only when it can serve ``region``.

    A withdrawal announcement names one successor for the whole country,
    while our coverage is per region: EnergyVision took over DATS 24's
    Flemish and Walloon customers alike, but only its Flanders cards are
    modelled. Returns ``None`` when the successor is unset, unknown to this
    build, or has no contract in the region, so the caller can avoid telling
    a user to pick a supplier the config flow would then refuse.
    """
    if not supplier_id:
        return None
    try:
        successor = get_extractor(supplier_id)
    except ExtractorError:
        return None
    if not any(region in c.regions for c in successor.contracts):
        return None
    return successor


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


# A single failed fetch is almost always a transient CDN timeout that the
# next hourly tick recovers. Raising the user-facing "extractor failed"
# repair issue on the very first failure produced false alarms that wrongly
# told the user the supplier had changed its tariff layout. Only raise the
# issue once a failure has survived this many consecutive fetch attempts.
# The shared negative-fetch row carries the running count and it resets the
# moment a fetch succeeds; the 7-day snapshot_stale issue stays the backstop
# for a breakage that outlives every threshold.
_EXTRACTOR_ISSUE_THRESHOLD = 2


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


def local_year_start(when: datetime | None = None) -> datetime:
    """Local 1 January 00:00 of ``when``'s year, or of the current year.

    The billing year's anchor. It was spelled out six times as the same
    ``.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)``,
    and one of those pairs is a cross-file invariant rather than a
    convenience: ``_seed_short_term_sum`` has to hand the recorder the SAME
    instant the ``current_year_cost`` sensor reports as its ``last_reset``,
    and its docstring said so with nothing enforcing it. A divergence puts the
    cost compiler on the meter-reset branch and adds a whole year's reading on
    top of the resumed sum.

    Deliberately a function, never a module-level constant: a Home Assistant
    process that stays up across midnight on 31 December would otherwise keep
    reporting last year's anchor.
    """
    return (when or dt_util.now()).replace(
        month=1, day=1, hour=0, minute=0, second=0, microsecond=0
    )


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
        # _snapshot is what this entry prices against: the card resolved
        # against its VAT preference. _snapshot_raw is the card exactly as
        # parsed, which is what gets shared with sibling entries and
        # persisted - they may answer the VAT question differently.
        self._snapshot: SupplierSnapshot | None = None
        self._snapshot_raw: SupplierSnapshot | None = None
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
                self._set_snapshot(_snapshot_from_dict(snap))
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
                self._set_snapshot(None)
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
        self._set_snapshot(
            build_custom_snapshot(
                self.entry.data,
                self.entry.data.get(CONF_REGION, ""),
                self.entry.data.get(CONF_DSO, ""),
            )
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
        # Dynamic contracts replay historical hourly spots to bill the
        # YTD energy term; spot-monthly contracts average them per month;
        # static-energy contracts with a spot-indexed injection replay them
        # to credit the YTD injection. Backfill any missing hours in
        # [Jan 1, today] before anything reads the cache; failures degrade to
        # "no data" for those hours rather than tearing the tick down.
        #
        # This has to run BEFORE the monthly mean below. _monthly_spot_mean
        # averages self._historical_spots, and this is the only thing that
        # fills it, so computing the mean first made a tick that started with
        # an empty cache average today's curve alone and call it the month.
        # On a cold start that flat rate was ~46% off, and it is what the
        # whole today+tomorrow table and the baked injection credit use until
        # the next tick.
        if isinstance(
            priced.energy, (DynamicRates, SpotMonthlyRates)
        ) or _injection_needs_spot(self._snapshot, self.entry):
            today_local = dt_util.now().date()
            await self._ensure_historical_spots(
                date(today_local.year, 1, 1), today_local
            )

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
        # Gate on the EFFECTIVE (cohort) energy so a variable contract re-priced
        # to a SpotMonthlyRates cohort bakes its mean-indexed injection too;
        # self._snapshot.energy stays VariableRates for such a contract, so
        # keying off it would skip the bake. The bake is a no-op for a flat
        # monthly-indicative injection (EBEM/Eneco/Mega).
        #
        # EXCEPT when the injection carries its own PER-HOUR index. The cohort
        # re-price is an energy-leg concept: it freezes the coefficients the
        # customer signed for the commodity, which a variable card indexes
        # monthly. Cociter Tarif Variable indexes the two legs differently and
        # says so on the card - note (7) "le prix ... est indexe mensuellement
        # ... moyenne arithmetique ... (BELIX) durant le mois de fourniture"
        # for consumption, note (9) "le prix de l'injection varie chaque heure"
        # for injection. Baking that hourly formula to a month mean prices the
        # feed-in credit off an index the contract never mentions, and because
        # PV output peaks exactly when the day-ahead price troughs, a flat mean
        # systematically over-credits. _injection_needs_spot identifies that
        # shape (factor/base with no printed indicative), so leave it alone.
        injection_snapshot = self._snapshot
        if isinstance(
            priced.energy, SpotMonthlyRates
        ) and not _injection_hourly_on_cohort(self._snapshot, self.entry):
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
        self._sync_exclusive_night_gap_issue()
        self._sync_impact_gap_issue()
        self._sync_connection_fee_issue()
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
            capacity_peak_months=len(self._peak_terms()),
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

    def _sync_issue(
        self,
        key: str,
        active: bool,
        *,
        extra: dict[str, str] | None = None,
        severity: ir.IssueSeverity = ir.IssueSeverity.WARNING,
    ) -> None:
        """Raise or clear one Repairs issue for this entry.

        Five syncers spelled this out: the unloaded guard, the
        ``f"{translation_key}_{entry_id}"`` id, the create call with its
        supplier / contract placeholders, and the delete in the else. Only the
        key, the predicate and a couple of extra placeholders differ.

        The id shape is load-bearing and must stay byte-identical: Repairs
        persists it, so a changed id leaves an already-raised issue orphaned
        with no way for the user to clear it.
        """
        if self._unloaded:
            return
        issue_id = f"{key}_{self.entry.entry_id}"
        if not active:
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return
        placeholders = {
            "supplier": str(self.entry.data.get(CONF_SUPPLIER, "")),
            "contract": str(self.entry.data.get(CONF_CONTRACT, "")),
        }
        placeholders.update(extra or {})
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=severity,
            translation_key=key,
            translation_placeholders=placeholders,
        )

    def _sync_stale_issue(self, stale: bool) -> None:
        """Raise or clear the 'snapshot stale' repair issue for this entry."""
        self._sync_issue(
            "snapshot_stale",
            stale,
            extra={
                "days": str(SNAPSHOT_STALE_DAYS),
                "last_error": self._last_error or "unknown",
            },
        )

    def _sync_exclusive_night_gap_issue(self) -> None:
        """Flag an exclusive-night meter whose DSO overlay cannot price it.

        ``network_eur_per_kwh`` bills an exclusive-night circuit at its own
        distribution rate, falling back to off-peak and then to the single
        (day) rate. When a supplier's card publishes neither, that last
        fallback silently bills the dedicated night circuit at the day rate.
        TotalEnergies' Flemish card is the case: its DSO table prints
        digital/classic prelevement and capacitaire, metering, cotisation,
        transport and prosumer, with no exclusive-night column at all - even
        though it does publish an exclusive-night ENERGY rate, so the entry
        looks fully configured.

        The rate cannot be substituted from anywhere: no EUR value may live
        in Python source, and borrowing another supplier's Fluvius figure
        would be a guess. So price it as the engine already does and tell the
        user, rather than hiding the meter type or silently over-billing.
        """
        overlay = (
            self._snapshot.dsos.get(self.entry.data.get(CONF_DSO, ""))
            if self._snapshot is not None
            else None
        )
        gap = (
            self.entry.data.get(CONF_METER) == METER_EXCLUSIVE_NIGHT
            and overlay is not None
            and overlay.distribution_exclusive_night is None
            and overlay.distribution_offpeak is None
        )
        self._sync_issue(
            "exclusive_night_rate_missing",
            gap,
            extra={"dso": str(self.entry.data.get(CONF_DSO, ""))},
        )

    def _sync_impact_gap_issue(self) -> None:
        """Flag an Impact DSO mode the supplier's card cannot price.

        Only Luminus' Wallonia DYNAMIC card prints the CWaPE Tarif Impact
        block; its static, variable and TOU Wallonia cards omit it, so the
        overlay's pic / medium / eco stay None. ``network_eur_per_kwh`` then
        falls back to the bi-horaire branch while ``_routed_rate`` keeps
        routing the ENERGY side through ``dso_impact_band``. The two schedules
        agree for most of the day but not between 22:00 and 01:00, where the
        Impact MEDIUM band bills the peak energy rate against an off-peak
        distribution rate.

        The bill stays close (this is a band mismatch, not the mono-rate
        fallback it looks like from the overlay alone: the static cards do
        publish peak / offpeak). Still worth telling the user, since they
        explicitly opted into Impact and are not being billed on it.
        """
        overlay = (
            self._snapshot.dsos.get(self.entry.data.get(CONF_DSO, ""))
            if self._snapshot is not None
            else None
        )
        gap = (
            self.entry.data.get(CONF_DSO_TARIFF_MODE) == DSO_MODE_IMPACT
            and overlay is not None
            and overlay.distribution_pic is None
        )
        self._sync_issue(
            "impact_rates_missing",
            gap,
            extra={"dso": str(self.entry.data.get(CONF_DSO, ""))},
        )

    def _sync_connection_fee_issue(self) -> None:
        """Flag a Walloon card that stopped printing the connection fee.

        EnergyVision deleted the row from every one of its Walloon cards on
        1 August 2026, together with the energy contribution that really was
        abolished that day. The connection fee was not: Wallonia still levies
        it and the card's own terms keep taxes and redevances fully passed
        through to the customer.

        The extractor bills 0 for it rather than failing the fetch, which
        would leave the entry frozen on a July snapshot still carrying the
        abolished contribution and the superseded excise, and be the larger
        error of the two. Say what the cost excludes so the gap is disclosed
        rather than silent, and clear it the moment the row comes back.
        """
        self._sync_issue(
            "connection_fee_missing",
            self._snapshot is not None
            and self._snapshot.taxes.region_connection_fee_unavailable,
        )

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
        self._sync_issue(
            "entsoe_auth_failed",
            active,
            extra={"error": message or "401 Unauthorized"},
            severity=ir.IssueSeverity.ERROR,
        )

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
        placeholders = {
            "supplier": extractor.label,
            "ends_on": extractor.deprecated_until.isoformat(),
        }
        successor = _successor_for(
            extractor.deprecated_successor, str(self.entry.data.get(CONF_REGION, ""))
        )
        if successor is not None:
            placeholders["successor"] = successor.label
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            # Only tell the user to switch to the successor when we can
            # actually price it for their region. Naming a supplier the
            # config flow will refuse (it aborts at the contract step with
            # supplier_region_unavailable) sends them down a dead end;
            # the fallback card states the situation without the bad advice.
            translation_key=(
                "supplier_deprecated"
                if successor is not None
                else "supplier_deprecated_no_successor"
            ),
            # Labels, not registry ids: the card tells the user to pick a
            # supplier from a label-based dropdown, so "DATS 24" and
            # "EnergyVision" are what they will actually look for.
            translation_placeholders=placeholders,
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
        # And the per-month archive rows. The year-to-date walk runs Jan 1
        # through today INCLUSIVE, so the CURRENT delivery month is cached
        # there too, with no TTL: a supplier that re-issues this month's card
        # (Eneco reissues a corrected volume under the same month) would go on
        # being billed from the first card fetched for the life of the HA
        # process, and this service, which exists precisely to pick up a
        # corrected card, could not clear it.
        for month_key in _drop_monthly_rows(self.hass, key, key[0]):
            _bump_tuple_generation(self.hass, month_key)
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

    def _set_snapshot(self, snap: SupplierSnapshot | None) -> None:
        """Keep the card as parsed and resolve this entry's VAT preference.

        Every path that produces a snapshot - a fetch, a sibling's shared
        copy, the persisted cache, the custom-formula build - lands here,
        so the VAT choice is applied exactly once and never leaks into
        what other entries on the same tuple see.
        """
        self._snapshot_raw = snap
        self._snapshot = None if snap is None else _resolve_snapshot(self.entry, snap)

    def _adopt_shared(
        self,
        shared: _SharedSnapshot,
        probe_key: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Take a fresh shared snapshot as our own.

        When the freshness decision came from a PROBE key rather than the
        TTL, restamp ``fetched_at`` to now, exactly as the self-fresh branch
        below does and for the same reason: the probe just verified the
        supplier has not published a new card, so the snapshot is "checked
        just now", not "fetched whenever the cold fetch happened".

        This path is the one that runs in steady state. The shared row is
        normally this coordinator's OWN row, written by its cold fetch, and
        the shortcut is tried before the self-fresh branch, so leaving the
        stamp alone pinned it at the cold-fetch instant for as long as the
        supplier kept publishing the same card. Cards are monthly, so after
        seven days every probe-based supplier raised a false "snapshot
        stale" Repairs card, with `snapshot_age_hours` reading days while the
        card had been verified minutes earlier. Restamp the shared row too so
        siblings agree.

        A TTL-based match must NOT restamp: that would reset the TTL clock on
        every tick and a probe-less supplier would never be re-fetched.
        """
        self._set_snapshot(shared.snapshot)
        if probe_key is not None and now is not None:
            shared.fetched_at = now
            self._snapshot_fetched_at = now
        else:
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
            self._adopt_shared(shared, probe_key, now)
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
            if (
                cache.get(key) is None
                and self._snapshot_raw is not None
                and self._snapshot_fetched_at is not None
            ):
                cache[key] = _SharedSnapshot(
                    snapshot=self._snapshot_raw,
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
            locked_now = dt_util.utcnow()
            if shared is not None and self._shared_is_fresh(
                shared, probe_key, locked_now, ttl
            ):
                self._adopt_shared(shared, probe_key, locked_now)
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
                self._set_snapshot(snap)
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

    def _peak_terms(self) -> list[float]:
        """The monthly peaks that go into the mean, newest last.

        Only count the in-progress month once it HAS a measurement. It is
        reset to 0 on the local 1st, and a zero floored to 2.5 is not a
        measured peak: including it dropped the twelve-term mean at every
        rollover, stepping capacity_cost and current_year_cost down for the
        first hours of every month and back up as the month accrued. This is
        the same rule ``_billed_peak_kw`` documents for a month that was never
        measured, and for the same reason: Fluvius estimates a missing month
        as the mean of the validated ones, and inserting a set's own mean
        leaves the mean unchanged, so leaving the gap out lands on the same
        number.

        The count is published as ``capacity_peak_months``, so it has to come
        from here rather than be recomputed at the call site: reporting
        ``len(self._peak_history) + 1`` claimed a month the mean had not taken
        for as long as the in-progress one stayed unmeasured.
        """
        peaks = list(self._peak_history.values())
        if self._peak_kw > 0.0:
            peaks.append(self._peak_kw)
        return peaks

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
        peaks = self._peak_terms()
        if not peaks:
            # A brand-new entry in the first hours of its first month has
            # nothing measured anywhere; the regulated minimum is the answer.
            return VREG_CAPACITY_FLOOR_KW
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

    def _billable_spots(
        self, extra_spots: dict[datetime, float]
    ) -> dict[datetime, float]:
        """Persisted year-to-date spots merged with this tick's fresh curve,
        with anything past today dropped.

        The drop has to happen on the MERGED dict, not on either half: the
        freshly fetched curve is exactly where tomorrow's prices come from, and
        letting them into a month mean pulls the flat monthly rate toward a day
        that has not been billed. Both month means spelled this out.
        """
        merged = dict(self._historical_spots)
        merged.update(extra_spots)
        return _drop_future_spots(merged, dt_util.now().date())

    def _monthly_spot_mean(
        self, year: int, month: int, extra_spots: dict[datetime, float]
    ) -> float | None:
        """Arithmetic mean of the (year, month)'s hourly Day-Ahead spots.

        Merges the persisted year-to-date cache with ``extra_spots`` (today's
        freshly fetched curve) so the current month's running mean stays up to
        date within a tick, and de-duplicates by timestamp. Returns ``None``
        when no spot for that month is available yet (cold start).
        """
        return _mean_of_month(self._billable_spots(extra_spots), year, month)

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
        return _spp_weighted_month_mean(
            self._billable_spots(extra_spots), self._spp_weights, year, month
        )

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
        # Mutate in place rather than rebinding. _ensure_historical_spots
        # merges each fetched chunk into this attribute and re-resolves it
        # after every await, so a prune landing between two chunks (the tick
        # calls it from _save_persistent while a backfill is mid-fetch) would
        # rebind the attribute and silently discard everything the earlier
        # chunks had already merged into the old dict.
        for stale_hour in [h for h in self._historical_spots if h < keep_after]:
            del self._historical_spots[stale_hour]
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
        if self._snapshot_raw is not None and self._snapshot_fetched_at is not None:
            # Persist the card as parsed, not as priced: flipping the VAT
            # preference must re-resolve the cached card on the next load
            # rather than serve a snapshot baked for the old answer.
            payload["snapshot"] = _snapshot_to_dict(
                self._snapshot_raw,
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


# ---- snapshot serialization for the HA Store ----------------------------------
