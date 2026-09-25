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

from .snapshot_store import (
    SNAPSHOT_STALE_DAYS,
    _bump_tuple_generation,
    _drop_monthly_rows,
    _shared_failed_fetches,
    _shared_snapshots,
)
from .snapshot_codec import (
    _MigratingStore,
    _SNAPSHOT_SCHEMA_VERSION,
)

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
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

from .const import (
    CONF_CONTRACT,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    STORAGE_VERSION,
    UPDATE_INTERVAL_MINUTES,
)
from .providers import (
    ExtractorError,
    SupplierSnapshot,
    get as get_extractor,
)
from .synergrid import RlpWeights, SppWeights
from .contract_periods import PricedPeriods
from .coordinator_data import (
    CoordinatorData,
)
from .coordinator_persist import _PersistMixin
from .coordinator_tick import _TickMixin

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


class BePricesCoordinator(
    _TickMixin,
    _PersistMixin,
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
        # The cohort-spliced snapshot of the last tick; the spot layer reads
        # its grid off this (_billing_snapshot). Never persisted.
        self._priced: SupplierSnapshot | None = None
        # The last scheduled ranking, when the entry opted into one. Held on
        # the coordinator rather than in hass.data because the sensor that
        # publishes it is a CoordinatorEntity: setting this and asking for a
        # listener update is the whole delivery path, with no dispatcher.
        self.daily_compare: Any = None
        # What the contracts the household held earlier in the year cost
        # (contract_periods.py), priced once a day in the background because
        # it fetches the old supplier's cards, and kept with the periods it was
        # priced for. Persisted, so a restart the same day serves it.
        self._previous_priced: PricedPeriods | None = None
        self._previous_pricing: asyncio.Task[None] | None = None
        # Which periods the last pricing was started for, and when, so one
        # that could not price them waits for the next hourly tick.
        self._previous_tried: tuple[str, datetime] | None = None
        self._snapshot_raw: SupplierSnapshot | None = None
        # The household's measured yearly consumption, and the day it was
        # measured on. ``None`` until the recorder holds enough of it to be
        # worth trusting, which is what makes entry_annual_kwh fall back to
        # the typed estimate. _snapshot_annual_kwh is the figure _snapshot was
        # resolved against, so a card already in hand can be re-resolved when
        # the measurement lands or moves.
        self._annual_kwh: float | None = None
        # Whether that figure covers a full year of meter or is a quarter
        # scaled up to one. Only the first outranks a volume a business typed.
        self._annual_kwh_full_year: bool = False
        self._annual_kwh_day: date | None = None
        # A full trailing year of the export on the injection regime, read the
        # same day; None otherwise. A first-year feed-in bonus multiplies it.
        self._annual_injection_kwh: float | None = None
        # The day/night register(s) the daily measurement found silent or
        # stopped, which the Repairs card names; empty while every pair is
        # whole.
        self._register_pair_fault: str = ""
        self._snapshot_annual_kwh: float | None = None
        # The last blob written to the Store, so a tick that changed nothing
        # writes nothing. Not loaded from disk on startup: the first tick
        # should write once, both to prove the file is writable and because a
        # blob this version wrote differs from the one it read.
        self._saved_payload: dict[str, Any] | None = None
        # The Brugel power term the snapshot was resolved against, stamped for
        # the same reason the volume is: the cache is a module global and so is
        # empty after a restart, the persisted card is resolved before the
        # first refresh can fill it, and the arm of the tick that keeps a card
        # it already has does not resolve anything. Without this an entry in
        # Brussels billed 50,07 EUR a year less until the yearly volume next
        # moved, and never at all on an entry with no meter configured.
        self._snapshot_power_term: tuple[float, float] | None = None
        self._snapshot_fetched_at: datetime | None = None
        self._snapshot_probe_key: str | None = None
        # Which schema the snapshot in hand was parsed under, and what
        # _save_persistent stamps back. Only a replayed blob ever moves it off
        # the running version; see _replay_stale_snapshot.
        self._snapshot_schema_version = _SNAPSHOT_SCHEMA_VERSION
        # The blob async_load_persistent had to reject, kept rather than
        # dropped so _replay_stale_snapshot can fall back on it.
        self._stale_snapshot: dict[str, Any] | None = None
        # Whether the last fetch this coordinator ran itself came back with a
        # card that carries no text layer. Set per fetch, never per supplier:
        # it stops being true the moment readable cards return.
        self._card_unreadable = False
        # Whether the prices being served were read off a picture of the
        # card rather than out of it: the archive walk's OCR reading,
        # adopted because the supplier publishes page images. Set when
        # that row is adopted and cleared by any readable card.
        self._card_read_by_ocr = False
        # Set by async_force_refresh; cleared on the next successful
        # extractor fetch. Acts as an out-of-band signal to bypass both
        # the probe-based and TTL-based freshness paths in
        # fetch_shared without having to lie about fetched_at: the
        # latter would block _save_persistent from writing the cached
        # snapshot until the next successful fetch lands.
        self._force_refresh = False
        self._spot_cache: dict[datetime, float] = {}
        # Which source last supplied the live curve: "entsoe" normally,
        # "energy-charts" when the keyless fallback answered for it. Surfaced
        # on current_price so a user can tell a fallback price from a
        # source-of-record one rather than having to trust it blindly.
        self._spot_source: str = "entsoe"
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
        # Serialises the historical spot walk. Two callers reach it from
        # outside the tick: the statistics backfill and the compare page,
        # and on a fresh install the deferred year-fill below runs beside
        # them. Without this they walk the same empty cache at the same time
        # and each fetches what the other is already fetching, which is how a
        # rate-limited source gets asked twice for one window.
        self._spot_fetch_lock = asyncio.Lock()
        # True until the year's spots have been fetched once, off the setup
        # path. See _update_body: the first tick fetches only the month it
        # cannot do without, because that tick is what the config flow's final
        # step is waiting on.
        self._year_spots_deferred = True
        # Same deal for the archived tariff cards the year-to-date walk bills
        # each past month with. See _fill_month_cards.
        self._month_cards_deferred = True
        # And for the Synergrid load / production profiles. See _fill_profiles.
        self._profiles_deferred = True
        # Synergrid solar production profile: hourly weights keyed by UTC
        # (month, day, hour), for SPP-weighted custom injection. Persisted so a
        # restart doesn't force a fresh 52 MB download; refreshed monthly (the
        # ex-ante file is revised in-year).
        self._spp_weights: SppWeights = {}
        self._spp_weights_year: int | None = None
        self._spp_fetched_at: datetime | None = None
        self._spp_failed_at: datetime | None = None
        # Synergrid residential load profile: hourly weights keyed by LOCAL
        # (month, day, hour), for an energy leg indexed on the RLP-weighted
        # month mean (Eneco Flex). Same lifecycle as the SPP profile.
        self._rlp_weights: RlpWeights = {}
        self._rlp_weights_year: int | None = None
        self._rlp_blend: str = "distinct"
        # Every blend this process holds, the entry's own included. The
        # compare page prices foreign cards and each is billed on the index
        # its own card names, so one reduction is not enough.
        self._rlp_blend_weights: dict[str, RlpWeights] = {}
        self._rlp_fetched_at: datetime | None = None
        self._rlp_failed_at: datetime | None = None
        # Stable past days the spot walk should not ask for again yet, each
        # holding the instant it may be retried at. Written when a fetch left
        # the day short of 20 hours (_SHORT_SPOT_DAY_TTL) and when both
        # sources refused the window outright (_SPOT_OUTAGE_TTL, shorter,
        # the data exists, the servers were down).
        self._spot_day_retry_at: dict[date, datetime] = {}
        # Local days already confirmed to hold >= 20 cached spot hours. Within
        # a calendar year spots are only ever added, so a complete day stays
        # complete; caching the set lets the per-tick coverage scan skip the
        # timezone conversion and 24 dict lookups for every settled day. Prior
        # year entries are dropped in _prune_historical_spots at the boundary.
        self._complete_spot_days: set[date] = set()
        # Backfills running right now. The backfill reads _historical_spots
        # itself, and a window in a past year needs exactly the hours the
        # prune drops, so the prune waits while this is non-zero.
        self._spot_prune_holds = 0
        # The unique_ids each platform's setup is about to add, by platform,
        # recorded before it adds them. What the settings create, as opposed
        # to what got added: Home Assistant swallows a platform that fails to
        # set up, and that must not read as the settings dropping its
        # entities (_remove_unprovided_entities).
        self.intended_unique_ids: dict[str, set[str]] = {}
        # Which cache the days above were measured against: the hourly one or
        # the quarter one; the set is dropped when that flips.
        self._complete_spot_days_quarters = False
        # Local days whose cached spots came from ENTSO-E's 15-minute product,
        # for an entry billed hourly whose archived month card bills per
        # quarter-hour: the walk fetches such a month on that product, and a
        # day of it fetched on the hourly one before the card was known is
        # fetched again once the card says otherwise.
        self._quarter_grid_days: set[date] = set()
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

    @property
    def card_read_by_ocr(self) -> bool:
        """Whether these prices come from an OCR reading of the card.

        True from the moment such a row is adopted until a readable card
        replaces it. A fact about the last card resolved, like
        ``card_unreadable`` beside it, never a property of the supplier.
        """
        return self._card_read_by_ocr

    @property
    def card_unreadable(self) -> bool:
        """Whether this entry's supplier publishes its card as page images.

        Read by ``async_setup_entry`` to decide that a failed first refresh is
        not worth retrying. Derived from the last fetch this coordinator ran,
        so it clears by itself the moment the supplier publishes text again.
        """
        return self._card_unreadable

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
                stale = age > SNAPSHOT_STALE_DAYS * 24 and not self._supply_ended()
                self._sync_stale_issue(stale)
            raise

    async def async_force_refresh(self, clear_history: bool = False) -> None:
        """Force the next coordinator tick to re-fetch the supplier.

        Invoked by the be_electricity_prices.refresh service when the user
        wants the integration to pick up a new tariff card or correct an
        error without waiting for the 24h refresh tick. Sets a one-shot
        ``_force_refresh`` flag that ``fetch_shared`` honours, clears
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
            self._spot_day_retry_at.clear()
            self._quarter_grid_days.clear()
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

        Each value is compared by its JSON form. A recorded supplier switch
        stores a list of the earlier contracts' settings, and a frozenset
        cannot hold one: built from the values themselves, this would raise in
        the constructor of any entry that had recorded a switch.
        """
        return frozenset(
            (key, json.dumps(value, sort_keys=True, default=str))
            for key, value in entry.data.items()
        )


# ---- snapshot serialization for the HA Store ----------------------------------


# ---- daily-comparison serialization -------------------------------------------
