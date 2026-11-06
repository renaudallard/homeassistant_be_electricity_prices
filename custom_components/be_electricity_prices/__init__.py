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

"""Belgian Electricity Prices integration entry point."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import zlib
from datetime import datetime, timedelta
from typing import Any, Final

import voluptuous as vol
from homeassistant.config_entries import (
    SIGNAL_CONFIG_ENTRY_CHANGED,
    ConfigEntry,
    ConfigEntryChange,
    ConfigEntryState,
)
from homeassistant.core import (
    HomeAssistant,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .backfill import backfill_if_missing
from .compare_engine import evict_sweep_rows
from .const import (
    CONF_CONTRACT,
    CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
    CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
    CONF_DAILY_COMPARE,
    CONF_DSO_TARIFF_MODE,
    CONF_QUARTER_HOURLY,
    CONF_REGION,
    CONF_SUPPLIER,
    COSTS_STORAGE_VERSION,
    DAILY_COMPARE_WINDOW_MINUTES,
    DEFAULT_DAILY_COMPARE,
    DOMAIN,
    DSO_MODE_IMPACT,
    PLATFORMS,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    STORAGE_VERSION,
    SUPPLIER_CUSTOM,
)
from .coordinator import BePricesCoordinator
from .coordinator_issues import clear_issues
from .coordinator_profiles import async_remove_profile_store
from .creg_ev import async_remove_store as async_remove_creg_store
from .services import (
    _async_backfill_service,
    _async_cheapest_window_service,
    _async_most_expensive_window_service,
    _async_refresh_service,
)
from .snapshot_store import evict_shared_caches

type BePricesConfigEntry = ConfigEntry[BePricesCoordinator]

_LOGGER = logging.getLogger(__name__)

# The recorder compiles the hour that just closed at 00:00:10 (UTC minute 0,
# second 10, which Brussels shares) and commits it when its queue reaches the
# task. The midnight rebuild fires no earlier than this second, so the compile
# is queued by then, and waits at most this long for the commit.
_ROLLOVER_FIRST_SECOND: Final = 15
_RECORDER_COMMIT_WAIT_SECONDS: Final = 60


async def _recorder_caught_up(hass: HomeAssistant) -> None:
    """Wait until the recorder has committed every task queued so far.

    The yesterday part of the year-to-date reads the long-term statistics,
    and the midnight rebuild ran before the recorder had compiled
    yesterday's last hour on one entry in six (each whose second fell
    before 00:00:10) and on any install with a slow database. That hour was
    missing from current_year_cost, the month cost and the volumes until
    the next tick. Bounded, because a recorder with a backlog of minutes is
    a reason to price late, not to stop pricing.
    """
    if "recorder" not in hass.config.components:
        return
    from homeassistant.components.recorder import (  # type: ignore[attr-defined]
        get_instance,
    )

    future = get_instance(hass).async_get_commit_future()
    if future is None:
        return
    with contextlib.suppress(TimeoutError):
        async with asyncio.timeout(_RECORDER_COMMIT_WAIT_SECONDS):
            await future


SERVICE_REFRESH = "refresh"
SERVICE_CHEAPEST_WINDOW = "cheapest_window"
SERVICE_MOST_EXPENSIVE_WINDOW = "most_expensive_window"
SERVICE_BACKFILL_STATISTICS = "backfill_statistics"

WINDOW_SCHEMA = vol.Schema(
    {
        # Whole hours only. services.yaml advertises step=1, min=1, and
        # _resolve_window_inputs rejects anything < 1 at runtime; keep the
        # voluptuous bounds in sync so a YAML-only call hits the same rule.
        # On a 15-minute contract the duration is scaled to slots (x4) and
        # the window may start on any quarter boundary.
        vol.Required("duration_hours"): vol.All(
            vol.Coerce(float), vol.Range(min=1.0, max=48.0)
        ),
        vol.Optional("entry_id"): cv.string,
        vol.Optional("earliest_start"): cv.datetime,
        vol.Optional("latest_end"): cv.datetime,
    }
)

REFRESH_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("clear_history", default=False): cv.boolean,
    }
)


BACKFILL_SCHEMA = vol.Schema(
    {
        vol.Optional("entry_id"): cv.string,
        vol.Optional("start"): cv.datetime,
        vol.Optional("end"): cv.datetime,
        vol.Optional("clear", default=False): cv.boolean,
    }
)


CONFIG_SCHEMA = cv.empty_config_schema(DOMAIN)


async def async_setup(
    hass: HomeAssistant, config: ConfigType
) -> bool:  # HA hook signature
    """Register the integration's services once at startup.

    Service handlers are domain-scoped, not entry-scoped, so they live
    here rather than in ``async_setup_entry``. Registering once
    eliminates the deregister-then-reregister window when the user
    reloads the only config entry: a ``be_electricity_prices.refresh``
    automation firing in that window used to fail with "service not
    found" until setup completed again.
    """

    @callback
    def _entry_changed(_change: ConfigEntryChange, entry: ConfigEntry) -> None:
        # An entry whose setup was retrying is not unloaded when it is
        # disabled, only stopped: its Repairs cards go with it here.
        if entry.domain == DOMAIN and entry.state is ConfigEntryState.NOT_LOADED:
            clear_issues(hass, entry.entry_id)

    async_dispatcher_connect(hass, SIGNAL_CONFIG_ENTRY_CHANGED, _entry_changed)
    # Administrators only, as backfill is. A refresh drops the failure backoff
    # and the month cards and fetches the card again whatever its age, and
    # clear_history throws away the day-ahead prices the year to date replays:
    # they are fetched again from 1 January on the household's own ENTSO-E
    # key, and the hours before it are not fetched again at all. An
    # automation, with no user, goes through.
    async_register_admin_service(
        hass, DOMAIN, SERVICE_REFRESH, _async_refresh_service, schema=REFRESH_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_CHEAPEST_WINDOW,
        _async_cheapest_window_service,
        schema=WINDOW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MOST_EXPENSIVE_WINDOW,
        _async_most_expensive_window_service,
        schema=WINDOW_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    # Administrators only, as HA's own statistics services: clear deletes a
    # statistic's whole history. An automation, with no user, goes through.
    async_register_admin_service(
        hass,
        DOMAIN,
        SERVICE_BACKFILL_STATISTICS,
        _async_backfill_service,
        schema=BACKFILL_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


@callback
def _remove_unprovided_entities(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: BePricesCoordinator
) -> None:
    """Drop the registry rows of entities this entry's settings no longer create.

    Several entities exist only for some settings (the EV rate box, the band
    sensors of a bi-hourly meter, the Flemish capacity sensors and reset
    button, the solar sensors, the contract end and saving sensors). An edit
    or a recorded switch that turns one off stops creating it, and without
    this the registry kept its row, restored as unavailable, until the user
    deleted it by hand. Its recorded history is not touched.

    Judged on what each platform set out to create, never on what got added.
    Home Assistant logs a platform whose setup raises or times out and carries
    on, so a failed platform adds nothing; removing its rows then would lose
    every rename, area and icon the user gave them. A platform that did not
    record its set failed before it got that far, so nothing is removed at
    all. A disabled row is kept too: it is the user's choice, and removing it
    would bring the entity back enabled.
    """
    intended = coordinator.intended_unique_ids
    missing = [platform for platform in PLATFORMS if platform not in intended]
    if missing:
        _LOGGER.debug(
            "%s: %s did not set up, so no entity is removed",
            entry.entry_id,
            ", ".join(missing),
        )
        return
    keep: set[str] = set().union(*intended.values())
    registry = er.async_get(hass)
    for reg_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg_entry.disabled_by is None and reg_entry.unique_id not in keep:
            registry.async_remove(reg_entry.entity_id)


@callback
def _migrate_current_year_cost_unique_id(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    """Carry the current-year-cost sensor across its 0.5.2 key rename.

    The sensor's key changed ``yearly_cost`` -> ``current_year_cost`` in
    0.5.2, which changed its unique_id (``{entry_id}_{key}``) and orphaned
    the existing entity on the upgrade that crossed it: dropping its
    recorded history, its name and any dashboard references, while a fresh
    entity took over under the new id. Rename the registry entry so the
    sensor keeps its identity across that boundary. Runs before the sensor
    platform is set up so the reborn entity is adopted rather than
    duplicated. A no-op once migrated, and skipped when the new id already
    exists (the entry was first set up after the rename, or the user has
    already run past it) so the rename can never collide with a live entity.
    """
    registry = er.async_get(hass)
    old_unique_id = f"{entry.entry_id}_yearly_cost"
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, old_unique_id)
    if entity_id is None:
        return
    new_unique_id = f"{entry.entry_id}_current_year_cost"
    if registry.async_get_entity_id("sensor", DOMAIN, new_unique_id) is not None:
        return
    registry.async_update_entity(entity_id, new_unique_id=new_unique_id)


# The eight Bolt contract ids that used to model the quarter-hourly settlement
# as a product of its own, mapped to the variable contract they were the other
# reading of. Bolt sells one card per slug and lets the customer settle it
# either way, so the pair collapsed into one contract plus the settlement box.
_RETIRED_BOLT_DYNAMIC: Final[dict[str, str]] = {
    "bolt_dynamic": "bolt_variable",
    "bolt_plenty_dynamic": "bolt_plenty",
    "bolt_online_dynamic": "bolt_online",
    "bolt_plenty_online_dynamic": "bolt_plenty_online",
    "bolt_pro_dynamic": "bolt_pro_variable",
    "bolt_pro_plenty_dynamic": "bolt_pro_plenty",
    "bolt_pro_online_dynamic": "bolt_pro_online",
    "bolt_pro_plenty_online_dynamic": "bolt_pro_plenty_online",
}


def _migrate_bolt_dynamic_contract(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    """Point a retired Bolt dynamic entry at its variable card, box ticked.

    The entry keeps billing exactly what it billed: the settlement box applies
    the same printed Belpex coefficients to the same quarter-hourly spot, and
    the standing charge and the feed-in formula come off the same card. Only
    the id it is stored under changes.

    Without this the entry holds a contract the registry no longer knows, and
    the coordinator has no card to fetch at all.

    The unique id embeds the contract, so it moves too, but only when it is
    free. A household that deliberately ran the variable and the dynamic
    reading as two entries would otherwise have the second one claim the
    first's key; the data migration still happens for both, and a stale unique
    id costs nothing but a duplicate check the user has already passed.
    """
    old_contract = entry.data.get(CONF_CONTRACT)
    new_contract = _RETIRED_BOLT_DYNAMIC.get(str(old_contract))
    if entry.data.get(CONF_SUPPLIER) != "bolt" or new_contract is None:
        return
    data = {**entry.data, CONF_CONTRACT: new_contract, CONF_QUARTER_HOURLY: True}
    unique_id = entry.unique_id
    if unique_id is not None and old_contract is not None:
        candidate = unique_id.replace(f":{old_contract}:", f":{new_contract}:", 1)
        taken = {
            other.unique_id
            for other in hass.config_entries.async_entries(DOMAIN)
            if other.entry_id != entry.entry_id
        }
        if candidate != unique_id and candidate not in taken:
            unique_id = candidate
    # The title names the contract, so it would otherwise keep advertising a
    # product that no longer exists. Imported inside the function: config_flow
    # is a leaf HA loads on its own, and pulling it in at module scope would
    # tie setup to it.
    from .config_flow import _entry_title

    hass.config_entries.async_update_entry(
        entry, data=data, unique_id=unique_id, title=_entry_title(data)
    )
    _LOGGER.info(
        "Bolt %s is now %s with quarter-hourly settlement; the bill is unchanged",
        old_contract,
        new_contract,
    )


def _migrate_zeroed_custom_impact_bands(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    """Drop a custom entry's all-zero CWaPE Impact triplet.

    The three Impact distribution boxes were built with a 0,00 default rather
    than as fallback fields, so a Walloon custom entry that left them alone
    stored three zeros. ``network_eur_per_kwh`` takes the Impact branch as
    soon as all three are non-None, so those zeros did not fall back to the
    single rate, they billed no distribution at all: 0,1198 EUR/kWh gone,
    about EUR 419/yr at 3500 kWh, on the live tick, the year-to-date walk,
    the backfill and the compare quote alike. No Repairs card fired either,
    because that check tests for None and a stored zero is not None.

    Fixing the schema stops new entries taking the zeros but cannot clear the
    ones already stored, and the user has no way to blank a box that is not
    shown as blankable. Only an ALL-zero triplet is dropped: a genuine tariff
    has no zero bands, and a partly filled one is the user's own data.
    """
    if entry.data.get(CONF_SUPPLIER) != SUPPLIER_CUSTOM:
        return
    if entry.data.get(CONF_DSO_TARIFF_MODE) != DSO_MODE_IMPACT:
        return
    bands = (
        CONF_CUSTOM_DSO_DISTRIBUTION_PIC,
        CONF_CUSTOM_DSO_DISTRIBUTION_MEDIUM,
        CONF_CUSTOM_DSO_DISTRIBUTION_ECO,
    )
    if not all(entry.data.get(k) == 0.0 for k in bands):
        return
    data = {k: v for k, v in entry.data.items() if k not in bands}
    hass.config_entries.async_update_entry(entry, data=data)


async def async_setup_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Set up one config entry."""
    _migrate_current_year_cost_unique_id(hass, entry)
    _migrate_zeroed_custom_impact_bands(hass, entry)
    _migrate_bolt_dynamic_contract(hass, entry)
    coordinator = BePricesCoordinator(hass, entry, defer_meter_reads=True)
    # Assigned BEFORE the first refresh, not after it as the usual pattern
    # has it. The tick resolves the archived month rows, the cohort card and
    # the Flemish network ceiling against the yearly volume, and those read
    # it through entry.runtime_data (entry_annual_kwh); assigned afterwards,
    # the first tick of every restart could not see the measurement and
    # priced them on the typed figure or the household default, so a banded
    # card billed the past months of the year-to-date on the wrong tier for
    # an hour and stepped down at the second tick. Every reader still copes
    # with the attribute being absent, which it is until this line, after a
    # setup that failed below, and after an unload.
    entry.runtime_data = coordinator
    try:
        await coordinator.async_load_persistent()
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryNotReady:
        # A card published as page images does not become readable by asking
        # again, so ConfigEntryNotReady parks the entry in SETUP_RETRY with no
        # entities at all and nothing ever gets it out. Set up regardless: the
        # entities exist and read unavailable, the Repairs card explains the
        # workaround, and the entry stays reconfigurable. Every other
        # cold-start failure keeps the retry, which is what it is for, and
        # leaves no coordinator on the entry, the way Home Assistant drops it
        # on unload.
        if not coordinator.card_unreadable:
            object.__delattr__(entry, "runtime_data")
            raise
    except BaseException:
        # A store that cannot be read, or a start that was cancelled, ends
        # the setup before the retry above; Home Assistant deletes the
        # attribute only when unloading an entry that loaded, so take it
        # back here too rather than leave a coordinator that never ran.
        object.__delattr__(entry, "runtime_data")
        raise

    # The first refresh read no meter (issue #107): the refresh that does
    # runs now, while the rest of Home Assistant starts, rather than inside
    # the setup it waits on.
    if coordinator.meter_reads_pending:
        entry.async_create_background_task(
            hass,
            coordinator.async_request_reprice(),
            f"{DOMAIN}_meter_reads_{entry.entry_id}",
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _remove_unprovided_entities(hass, entry, coordinator)

    # Re-evaluate the price sensors at every slot boundary without
    # re-fetching: current_price / next_hour_price read the wall clock
    # live, so a push at :00 (and :15/:30/:45 where the price table or the
    # feed-in is per quarter-hour, as on Bolt's fixed cards) keeps them
    # aligned to the slot the user is actually billed for. The
    # coordinator's own 60-minute tick is not clock-aligned, so without
    # this the current price could lag the slot boundary by most of an
    # hour. The cadence follows those two grids and is re-registered
    # whenever one moves: it was fixed once from coordinator.data here,
    # which is None after a first refresh that was tolerated for an
    # unreadable card, so a quarter-hourly Ecofix entry set up that way was
    # pushed hourly for as long as it lived, its price sitting on a stale
    # quarter for up to 45 minutes once the archive's reading was adopted.
    slot_push: dict[str, Any] = {"resolution": None, "cancel": None}

    @callback
    def _push_slot_boundary(_now: datetime) -> None:
        coordinator.async_update_listeners()

    @callback
    def _track_slot_boundary() -> None:
        data = coordinator.data
        grids = () if data is None else (data.resolution, data.injection_resolution)
        quarter = RESOLUTION_QUARTER in grids
        resolution = RESOLUTION_QUARTER if quarter else RESOLUTION_HOURLY
        if resolution == slot_push["resolution"]:
            return
        if slot_push["cancel"] is not None:
            slot_push["cancel"]()
        slot_push["cancel"] = async_track_time_change(
            hass,
            _push_slot_boundary,
            minute=[0, 15, 30, 45] if resolution == RESOLUTION_QUARTER else 0,
            second=0,
        )
        slot_push["resolution"] = resolution

    @callback
    def _untrack_slot_boundary() -> None:
        if slot_push["cancel"] is not None:
            slot_push["cancel"]()

    _track_slot_boundary()
    entry.async_on_unload(coordinator.async_add_listener(_track_slot_boundary))
    entry.async_on_unload(_untrack_slot_boundary)

    # Crossing local midnight invalidates the price table itself, which no
    # amount of re-reading can fix. ``_build_hourly`` anchors the today +
    # tomorrow table at the local midnight of the tick that built it, so once
    # the date rolls over the table covers yesterday + today: the
    # tomorrow_average / tomorrow_min / tomorrow_max sensors read unknown and
    # tomorrow_prices_available drops off, until the next non-clock-aligned
    # tick happens to land. Ask for a rebuild on the day boundary instead of
    # widening the table, which would let the window services return a slot
    # two days out. ``async_track_time_change`` matches local time, so this
    # follows Brussels across both DST seams; midnight exists on each.
    #
    # Every install of a Belgian integration shares one timezone, so firing on
    # the same second would put the whole user base on a supplier's doorstep at
    # once. Spread the rebuild over the first minute using a stable per-entry
    # offset: crc32 rather than hash() because the latter is salted per process
    # and would move the entry to a different second on every restart. Past
    # the recorder's compile of yesterday's last hour, which the rebuild then
    # waits for (_recorder_caught_up).
    rollover_second = _ROLLOVER_FIRST_SECOND + zlib.crc32(entry.entry_id.encode()) % (
        60 - _ROLLOVER_FIRST_SECOND
    )

    async def _rebuild_on_local_day_rollover(_now: datetime) -> None:
        await _recorder_caught_up(hass)
        await coordinator.async_request_refresh_after_tick()

    entry.async_on_unload(
        async_track_time_change(
            hass,
            _rebuild_on_local_day_rollover,
            hour=0,
            minute=0,
            second=rollover_second,
        )
    )

    # The rebuild above anchors the hourly ticks at HH:00, so the last tick
    # of a month ran at 23:00 and nothing read the month after it: a peak the
    # capacity sensor set in that hour was never banked, and the hour was
    # missing from the month's last cost. One more tick a minute before
    # midnight on the month's last day closes it, in the first half of the
    # minute. A close still running at midnight does not swallow the rebuild,
    # which waits for it (async_request_refresh_after_tick).
    async def _close_the_month(now: datetime) -> None:
        if (now + timedelta(days=1)).day == 1:
            await coordinator.async_request_reprice()

    entry.async_on_unload(
        async_track_time_change(
            hass,
            _close_the_month,
            hour=23,
            minute=59,
            second=rollover_second // 2,
        )
    )

    # Opt-in daily ranking. Spread with the same crc32-on-entry-id trick as
    # the rollover above, but across the whole day rather than the first
    # minute: this one fetches a tariff card from every supplier selling in
    # the region, so two installs sharing a second is a burst on somebody
    # else's server rather than a wasted one of ours. Salted so an entry does
    # not land on a time correlated with its rollover second.
    if entry.data.get(CONF_DAILY_COMPARE, DEFAULT_DAILY_COMPARE):
        minute_of_day = (
            zlib.crc32(b"daily-compare:" + entry.entry_id.encode())
            % DAILY_COMPARE_WINDOW_MINUTES
        )

        async def _run_daily_compare() -> None:
            # Imported here rather than at module scope: compare_flow pulls in
            # the whole options-flow branch, which setup has no other reason
            # to load.
            from .compare_sweep_flow import async_run_daily_compare

            await async_run_daily_compare(hass, entry, coordinator)

        # A callback that hands the sweep to the entry, because Home
        # Assistant runs a coroutine listener as a background task of its
        # own that an unload does not cancel: a reload during the sweep left
        # it fetching cards for a coordinator that was gone.
        @callback
        def _daily_compare(_now: datetime) -> None:
            entry.async_create_background_task(
                hass,
                _run_daily_compare(),
                f"{DOMAIN}_daily_compare_{entry.entry_id}",
            )

        entry.async_on_unload(
            async_track_time_change(
                hass,
                _daily_compare,
                hour=minute_of_day // 60,
                minute=minute_of_day % 60,
                second=0,
            )
        )

    # Automatic backfill: only fires when the recorder has no statistics for
    # current_price at the Jan 1 anchor, so a normal restart adds zero work,
    # and runs once more an hour later when a card read failed or the recorder
    # did not answer. Runs in a background task because the ENTSO-E historical
    # fetch can take tens of seconds for a fresh install on a dynamic supplier
    # and must not block setup.
    entry.async_create_background_task(
        hass,
        backfill_if_missing(hass, entry),
        f"{DOMAIN}_backfill_{entry.entry_id}",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> bool:
    """Unload one config entry."""
    # Snapshot the supplier tuple from the coordinator BEFORE
    # async_unload_platforms tears it down. The coordinator stores
    # the tuple it was constructed with so an OptionsFlow edit that
    # mutated entry.data prior to reload can still evict the
    # *previous* tuple's cache rows.
    # entry.runtime_data may be HA's UNDEFINED sentinel rather than None
    # if async_setup_entry raised before the explicit assignment (e.g.
    # async_config_entry_first_refresh raised ConfigEntryNotReady). The
    # `is not None` test would still pass, then access to ._supplier_tuple
    # raises AttributeError and masks the real setup failure.
    coordinator = getattr(entry, "runtime_data", None)
    if not isinstance(coordinator, BePricesCoordinator):
        coordinator = None
    cached_key = coordinator._supplier_tuple if coordinator is not None else None
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        # Nothing resolves this entry's cards once it stops ticking; the next
        # setup raises again whatever still holds.
        clear_issues(hass, entry.entry_id)
        # The ranking page's scratch belongs to this entry alone, so it goes
        # unconditionally - unlike the shared caches below, which are keyed by
        # supplier tuple and may still be referenced by a sibling entry.
        evict_sweep_rows(hass, entry.entry_id)
        if coordinator is not None:
            # Flag the coordinator so a slow in-flight tick that resumes after
            # this unload/removal skips its Repairs-issue sync and persistent
            # write instead of resurrecting a just-deleted issue or storage
            # blob. Set only once unload has succeeded: a failed unload leaves
            # the entry loaded, and muting a still-live coordinator would stop
            # its persistence and Repairs sync until an HA restart.
            coordinator._unloaded = True
        # Drop any shared-cache rows pinned to this entry's previous
        # (supplier, contract, region) tuple, but only when no other
        # loaded entry still references the same tuple. Without this
        # the snapshot, per-month archive, failed-fetch marker, and
        # asyncio.Lock leak into hass.data for the rest of the HA
        # process lifetime.
        if cached_key and all(cached_key):
            siblings = [
                other
                for other in hass.config_entries.async_loaded_entries(DOMAIN)
                if other.entry_id != entry.entry_id
                and (
                    other.data.get(CONF_SUPPLIER),
                    other.data.get(CONF_CONTRACT),
                    other.data.get(CONF_REGION),
                )
                == cached_key
            ]
            if not siblings:
                evict_shared_caches(hass, cached_key, cached_key[0])
    # Services live for the integration's lifetime (registered in
    # async_setup), so they're not torn down here. The previous
    # per-entry registration window briefly returned "service not
    # found" to in-flight automations during a single-entry reload.
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: BePricesConfigEntry) -> None:
    """Drop per-entry state when the user removes the entry.

    The Repairs cards go here too: an entry removed while its setup was
    retrying was never unloaded, which is where they are cleared otherwise.

    HA calls this hook after ``async_unload_entry`` for a removal (not
    for a reload). The persistent snapshot Store is also deleted so the
    JSON blob the coordinator writes under ``.storage/`` doesn't outlive
    the entry, and with the last entry the installation-wide profile and
    CREG stores go too.
    """
    clear_issues(hass, entry.entry_id)
    store: Store[dict[str, Any]] = Store(
        hass, STORAGE_VERSION, f"{DOMAIN}_cache_{entry.entry_id}"
    )
    await store.async_remove()
    costs: Store[dict[str, Any]] = Store(
        hass, COSTS_STORAGE_VERSION, f"{DOMAIN}_costs_{entry.entry_id}"
    )
    await costs.async_remove()
    if not any(
        other.entry_id != entry.entry_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        await async_remove_profile_store(hass)
        await async_remove_creg_store(hass)


async def _async_options_updated(
    hass: HomeAssistant, entry: BePricesConfigEntry
) -> None:
    """Reload the entry only when entry.data actually changed.

    HA fires this listener whenever ``entry.options`` (or ``entry.data``)
    changes via ``async_update_entry``. The OptionsFlow's no-op
    ``async_create_entry(data={})`` finalize writes ``options = {}``;
    if the entry pre-existed with non-empty options (a hold-over from
    an older HA version's options-flow contract), HA sees options
    `{...} -> {}` as a real change and would otherwise trigger a
    needless reload that tears down the warmed snapshot. The
    integration carries every load-bearing field in ``entry.data``,
    not in ``entry.options``, so an options-only change can skip the
    reload safely.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if isinstance(coordinator, BePricesCoordinator):
        live_data = coordinator._entry_data_signature
        if BePricesCoordinator._compute_data_signature(entry) == live_data:
            return
    await hass.config_entries.async_reload(entry.entry_id)
