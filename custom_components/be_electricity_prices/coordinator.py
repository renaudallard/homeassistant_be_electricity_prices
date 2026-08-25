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

from .coordinator_issues import _IssuesMixin
from .coordinator_peak import _PeakMixin
from .coordinator_snapshot import _SnapshotMixin
from .coordinator_spots import _SpotsMixin

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
    _injection_needs_month_spot,
    _injection_needs_spot,
    _injection_price_for_slot,
    _injection_varies_intraday,
)
from .snapshot_store import (
    SNAPSHOT_STALE_DAYS,
    _MigratingStore,
    _bump_tuple_generation,
    _drop_monthly_rows,
    _shared_failed_fetches,
    _shared_snapshots,
    _snapshot_from_dict,
    _snapshot_to_dict,
)
from .spot_stats import (
    _energy_is_quarter_hourly,
    _injection_is_spp_indexed,
    _spp_weighting_enabled,
)
from .projected_cost import (
    _compute_projected_year_cost,
)
from .coordinator_spots import _spot_is_sane
from .ytd_cost import (
    _compute_current_year_cost,
)

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import (
    HomeAssistant,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import EntsoeAuthError, EntsoeError
from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    REGION_FLANDERS,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SOLAR_REGIME_INJECTION,
    STORAGE_VERSION,
    SUPPLIER_CUSTOM,
    UPDATE_INTERVAL_MINUTES,
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
from .synergrid import SppWeights
from .providers.base import (
    EnergyRates,
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
    # Roughly what a year on this contract costs: a full year priced at today's
    # tariffs against the entry's own metered yearly volume, computed in one
    # pass rather than as elapsed plus remainder. None for a contract whose
    # rate is a formula over an index that does not exist yet, and for a netted
    # meter with too little feed-in history to net a year against.
    projected_year_cost_eur: float | None = None
    # The basis behind that number, as strings plus a few figures: which legs
    # are measured, which are held flat, and how many days are still ahead. A
    # projection carries more assumptions than the running bill does, so it
    # ships the means to audit it.
    projection_diagnostics: dict[str, Any] | None = None


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


class BePricesCoordinator(
    _SnapshotMixin,
    _IssuesMixin,
    _SpotsMixin,
    _PeakMixin,
    DataUpdateCoordinator[CoordinatorData],
):
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
        # The same hours' individual 15-minute slots, kept only for an entry
        # whose feed-in formula is floored: that formula is convex, so its
        # hour is not priced by the mean above. See
        # _injection_needs_spot_quarters. Empty for every other entry, which
        # is what keeps the persisted blob the size it always was.
        self._historical_spot_quarters: dict[datetime, list[float]] = {}
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
        dropped_spots = 0
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
                if not _spot_is_sane(float(v)):
                    # Dropped rather than kept: leaving it makes the day look
                    # complete, and a complete day is never refetched, so the
                    # bad value would price that hour for the life of the
                    # entry. Dropping it costs the hour its energy leg, which
                    # is the cheaper mistake; a day that loses five of its
                    # hours falls under the refetch threshold and is replaced
                    # from ENTSO-E, a day that loses one does not.
                    dropped_spots += 1
                    continue
                self._historical_spots[when] = float(v)
        quarters = stored.get("historical_spot_quarters")
        if isinstance(quarters, dict) and not tuple_mismatch:
            for k, v in quarters.items():
                if not isinstance(k, str) or not isinstance(v, list) or not v:
                    continue
                try:
                    when = datetime.fromisoformat(k)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                if not all(
                    isinstance(q, (int, float)) and _spot_is_sane(float(q)) for q in v
                ):
                    # The whole list goes, not the offending slot, because a
                    # short list would silently re-weight the hour's mean. The
                    # hourly value stays if it passed its own check above: the
                    # hour then prices its energy as it always did and credits
                    # feed-in off the mean, which is the answer this cache
                    # refines rather than the one it replaces. Taking it out
                    # too would forfeit a sane energy price over a feed-in
                    # refinement, and a day short of one hour is not re-fetched
                    # anyway.
                    dropped_spots += 1
                    continue
                self._historical_spot_quarters[when] = [float(q) for q in v]
        if dropped_spots:
            _LOGGER.warning(
                "Discarded %d cached day-ahead price(s) outside the publishable "
                "range for %s; a day missing several of them is re-fetched from "
                "ENTSO-E",
                dropped_spots,
                self.entry.title,
            )
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
        elif _injection_needs_spot(
            self._snapshot, self.entry
        ) or _injection_needs_month_spot(self._snapshot, self.entry):
            # Static-energy contract whose injection carries its own index:
            # Cociter Variable per hour, energie.be Vast on the month's
            # Belpex_SPP. The energy is priced without a spot, so
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

        # Refresh the Synergrid SPP profile when this entry's injection is
        # SPP-weighted: a card that indexes on Belpex_SPP, or a custom monthly
        # entry that opted in. Soft-fail. What a failure degrades TO differs -
        # the opt-in falls back to the plain mean, an SPP-indexed card must
        # not and keeps its printed indicative instead (see the bake below).
        spp_weighted = _spp_weighting_enabled(self.entry, self._snapshot)
        # Only worth the download when there are prices to weight. energie.be
        # Vast offers its ENTSO-E key as optional, so an entry that skipped it
        # reaches here with no spots at all and would otherwise pull 52 MB to
        # weight nothing, every restart.
        if spp_weighted and (spot_prices or self._historical_spots):
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
        if (
            isinstance(priced.energy, (DynamicRates, SpotMonthlyRates))
            or _injection_needs_spot(self._snapshot, self.entry)
            or _injection_needs_month_spot(self._snapshot, self.entry)
        ):
            today_local = dt_util.now().date()
            await self._ensure_historical_spots(
                date(today_local.year, 1, 1), today_local
            )

        monthly_mean: float | None = None
        if isinstance(priced.energy, SpotMonthlyRates) or _injection_needs_month_spot(
            self._snapshot, self.entry
        ):
            # Also for a card whose ENERGY needs no mean but whose feed-in
            # credit is indexed on one: without it the bake below would resolve
            # against None and wipe the credit instead of resolving it.
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
        if (
            isinstance(priced.energy, SpotMonthlyRates)
            or _injection_needs_month_spot(self._snapshot, self.entry)
        ) and not _injection_hourly_on_cohort(self._snapshot, self.entry):
            inj_mean = monthly_mean
            spp_only = _injection_is_spp_indexed(self._snapshot)
            if spp_weighted:
                # SPP-weight the injection month-mean; keep the flat mean for
                # energy.
                now = dt_util.now()
                spp_mean = self._spp_weighted_month_mean(
                    now.year, now.month, spot_prices
                )
                if spp_mean is not None:
                    inj_mean = spp_mean
                elif spp_only:
                    # The card indexes this formula on Belpex_SPP and the
                    # profile is not available yet. The flat mean is a
                    # DIFFERENT index, not a coarser one - it would roughly
                    # double the credit in a sunny month - so leave the
                    # snapshot alone and credit the card's own indicative.
                    inj_mean = None
            elif spp_only:
                inj_mean = None
            if spp_only and inj_mean is None:
                # Leave the snapshot alone so the card's printed indicative is
                # credited. Only an SPP-indexed card may take this branch: it
                # is the one shape that HAS an indicative to fall back to.
                # Skipping the bake for a formula-only leg instead would leave
                # factor/base standing with no ``current``, which is precisely
                # the shape _injection_is_spot_formula reads as "price this per
                # hour" - turning a flat monthly credit into an hourly one at
                # whatever the current slot costs.
                pass
            else:
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
            spot_quarters=self._historical_spot_quarters,
            spp_weights=self._spp_weights if spp_weighted else None,
            breakdown=ytd_breakdown,
            billed_peak_kw=billed_peak,
        )
        projection_breakdown: dict[str, Any] = {}
        projected_year_cost = await _compute_projected_year_cost(
            self.hass,
            self.entry,
            self._snapshot,
            priced,
            billed_peak_kw=billed_peak,
            today=dt_util.now().date(),
            breakdown=projection_breakdown,
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
            projected_year_cost_eur=projected_year_cost,
            projection_diagnostics=projection_breakdown or None,
        )

    async def async_force_refresh(self, clear_history: bool = False) -> None:
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

        ``clear_history`` additionally drops ``_historical_spots``, the cache of
        past hourly prices that the year-to-date walk replays. That one is NOT
        cleared by default and deliberately so: refilling it costs a fetch of
        every day since 1 January, in week-sized chunks against a rate-limited
        endpoint, which is far too much to spend on an ordinary refresh.

        It exists because nothing else can repair a bad value in there.
        ``_ensure_historical_spots`` only fetches a day holding fewer than 20 of
        its 24 hours, so a day that is complete but wrong is never revisited,
        and the only other thing that touches the dict is the year-end prune. A
        wrong price therefore skewed its hour of the running bill for the life
        of the entry, and the only escape was deleting and re-adding the entry,
        losing every setting with it.
        """
        self._force_refresh = True
        if clear_history:
            self._historical_spots.clear()
            # Both caches come off the same fetch, so a service that exists to
            # repair a bad cached price has to drop both or it leaves half the
            # bad hour behind.
            self._historical_spot_quarters.clear()
            self._complete_spot_days.clear()
            self._short_spot_days.clear()
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
        if self._historical_spot_quarters:
            payload["historical_spot_quarters"] = {
                h.isoformat(): v for h, v in self._historical_spot_quarters.items()
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
