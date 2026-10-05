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

"""The snapshot state machine: fetch, freshness, and the shared cache.

Split out of coordinator.py. Owns when a snapshot is refetched, when a
sibling entry's fetch can be adopted instead, and what counts as fresh,
a probe key match where the supplier offers one, a TTL otherwise."""

from __future__ import annotations

import asyncio
import contextlib
from .brugel import cached_power_term
from .vat_rates import residential_vat, standard_vat
from .providers import get as get_extractor
from .providers.custom import build_snapshot as build_custom_snapshot
from .providers._pdf import is_missing_card_error, is_transient_fetch_error
from .providers._rates import Contract
from .providers.base import CardNotReadableError, ExtractorError, SupplierSnapshot

import logging

from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    MEASURED_FULL_YEAR_DAYS,
    SOLAR_REGIME_INJECTION,
)
from .snapshot_store import (
    _SharedSnapshot,
    _shared_failed_fetches,
    fetch_shared,
)
from .energy_meters import (
    _bills_injection,
    _kwh_sensor_ids,
    noting_failed_reads,
)
from .coordinator_persist import settings_digest
from .meter_daily import _measured_kwh
from .meter_hourly import _metered_sides
from .cohort import ytd_window_start
from .snapshot_months import (
    ArchivedCard,
    card_for_unreadable_month,
    card_of_month_before,
    month_before,
)
from .snapshot_resolve import (
    _resolve_snapshot,
    entry_annual_kwh,
)
from .snapshot_codec import (
    _DEGRADED_MIN_SCHEMA_VERSION,
    _SNAPSHOT_SCHEMA_VERSION,
    _snapshot_from_dict,
)

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any
import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


# A single failed fetch is almost always a transient CDN timeout that the
# next hourly tick recovers. Raising the user-facing "extractor failed"
# repair issue on the very first failure produced false alarms that wrongly
# told the user the supplier had changed its tariff layout. Only raise the
# issue once a failure has survived this many consecutive fetch attempts.
# The shared negative-fetch row carries the running count and it resets the
# moment a fetch succeeds; the 7-day snapshot_stale issue stays the backstop
# for a breakage that outlives every threshold.
_EXTRACTOR_ISSUE_THRESHOLD = 2


_LOGGER = logging.getLogger(__name__)


def _vat_now() -> tuple[float, float]:
    """The residential and standard rates the running month resolves at: the
    VAT table is refreshed on the tick and a month can change rate, so the
    card resolved before either is resolved again."""
    today = dt_util.now().date()
    return residential_vat(today), standard_vat(today)


class _SnapshotMixin:
    """Mixed into BePricesCoordinator."""

    # Entry-owned state, declared as BARE annotations with no value. A valued
    # class attribute would change hasattr() and instance-dict behaviour;
    # __init__ in the concrete class is what actually creates these.
    entry: ConfigEntry
    _session: aiohttp.ClientSession
    _force_refresh: bool
    _store: Any
    _unloaded: bool
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _annual_kwh: float | None
    _annual_kwh_full_year: bool
    _annual_kwh_day: date | None
    _annual_injection_kwh: float | None
    _meter_results_inputs: str | None
    _register_pair_fault: str
    _register_pair_covered: bool
    _snapshot_annual_kwh: float | None
    _snapshot_power_term: tuple[float, float] | None
    _snapshot_vat: tuple[float, float]
    _snapshot_fetched_at: datetime | None
    _snapshot_probe_key: str | None
    _snapshot_schema_version: int
    _stale_snapshot: dict[str, Any] | None
    _card_unreadable: bool
    _card_read_by_ocr: bool
    _last_error: str | None
    _supplier_tuple: tuple[str, str, str]

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins. Declared
        # for the type checker rather than inherited, so each mixin is checked
        # on its own and BePricesCoordinator's bases say how they compose. The
        # one mixin composed anywhere else is the profiles mixin, which the
        # spots mixin extends.
        hass: HomeAssistant

        def _supply_ended(self) -> bool: ...
        def _entry_contract(self) -> Contract | None: ...

        def _sync_extractor_issue(
            self,
            message: str | None,
            *,
            transient: bool = False,
            unreadable: bool = False,
            missing: bool = False,
        ) -> None: ...
        def _sync_deprecated_supplier_issue(self) -> None: ...
        def _sync_card_read_by_ocr_issue(self, active: bool) -> None: ...

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

    def _shared_key(self) -> tuple[str, str, str]:
        return (
            self.entry.data[CONF_SUPPLIER],
            self.entry.data[CONF_CONTRACT],
            self.entry.data[CONF_REGION],
        )

    async def _ensure_annual_volume(self) -> None:
        """Measure how much this household uses in a year, once a day.

        Three legs price against a yearly volume (the excise band, the Flemish
        network ceiling, a volume-tiered energy card) and the config flow only
        ever asks a professional entry for one, so without this they all fell
        back to the 3.500 kWh default. Measured through ``_annual_volume``, the
        same read the compare page quotes its rows from, so a tiered card is
        split against the volume the ranking beside it used.

        Daily, not per tick: a trailing year moves by at most one day of kWh
        between two hourly ticks, and the read walks a year of recorder
        statistics. Kept as ``None`` while the meter has under 90 days of
        history, which is what hands the answer back to the typed estimate
        rather than to a winter quarter scaled by four.

        Whether the figure covers a FULL year is recorded beside it, because
        the two bands do not outrank the same things. A full year of meter
        beats anything a business typed; a quarter scaled up to a year does
        not, and a professional entry that stated its contracted volume was
        having its excise band decided by a seasonally uncorrected
        extrapolation of its winter.

        Soft-fail like the profile fetches around it: the recorder can be busy
        or mid-purge, and a yearly volume is not worth failing a tick over.
        """
        from .compare_quote import _annual_volume, _covers_a_year

        today = dt_util.now().date()
        if self._annual_kwh_day == today:
            return
        # The settings the read starts under, which is what its figures are
        # stored with: an edit saved while it runs reloads the entry, but
        # this read finishes first.
        inputs = settings_digest(self.entry)
        # Stamped on success only, so a recorder that was busy this tick is
        # asked again on the next one rather than leaving the entry on the
        # default, or on nothing measured, for the rest of the day. A busy
        # recorder seldom raises: every read answers it with no rows, which
        # is why the failed reads are collected.
        with noting_failed_reads() as failed:
            try:
                volume = await _annual_volume(
                    self.hass,
                    self.entry,
                    today - timedelta(days=MEASURED_FULL_YEAR_DAYS - 1),
                    today,
                )
            except Exception as err:  # noqa: BLE001 - never fail a tick over this
                _LOGGER.debug(
                    "%s: annual volume unavailable: %s", self.entry.entry_id, err
                )
                return
            # The same trailing year read for the injection side where the
            # export is SOLD, which is what a first-year feed-in bonus
            # multiplies (entry_annual_injection_kwh). Every other entry pays
            # nothing for it.
            injection: float | None = None
            if self.entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_INJECTION:
                with contextlib.suppress(Exception):
                    injected = await _measured_kwh(
                        self.hass,
                        self.entry,
                        today - timedelta(days=MEASURED_FULL_YEAR_DAYS - 1),
                        today,
                        side="injection",
                    )
                    if injected.kwh > 0 and _covers_a_year(injected.days_with_data):
                        injection = (
                            injected.kwh
                            * MEASURED_FULL_YEAR_DAYS
                            / injected.days_with_data
                        )
        if failed:
            _LOGGER.debug(
                "%s: annual volume unavailable, the recorder did not answer for %s",
                self.entry.entry_id,
                ", ".join(sorted(failed)),
            )
            return
        self._annual_kwh = volume.kwh if volume.measured else None
        self._annual_kwh_full_year = volume.measured and _covers_a_year(
            volume.days_with_data
        )
        self._annual_injection_kwh = injection
        self._meter_results_inputs = inputs
        if await self._find_meter_faults(today):
            self._annual_kwh_day = today

    async def _find_meter_faults(self, today: date) -> bool:
        """Name the meters the bill is short of, for the Repairs card.

        Read over the window the bill reads, not the trailing year of the
        volume: a register rewired last autumn short-changes this year to date
        only until 1 January, and a card read off the trailing year stayed up
        for months after the bill had recovered. Once a day, with the volume,
        and the one read that logs what a broken pair did to the bill.

        The entry's own wiring is read from the day its contract started:
        once a switch is recorded the window opens on 1 January, and the
        days before the switch were billed on the earlier contract's wiring,
        which ``previous_meter_faults`` checks. Reading the current sensors
        over them named a register rewired at the switch.

        False, with the verdict left as it was, when the recorder did not
        answer for some meter: a read it failed looks like a register that
        reported nothing, and the card would name, or stop naming, the wrong
        sensors until the next day.
        """
        from .contract_periods import current_period_start, previous_meter_faults

        start = current_period_start(
            self.entry.data, ytd_window_start(self.entry, today)
        )
        faults: list[str] = []
        # Registers whose side the totals sensor bills in full: named, but
        # on their own wording, since the cost does not move.
        covered: list[str] = []
        with noting_failed_reads() as failed, contextlib.suppress(Exception):
            measured = await _measured_kwh(
                self.hass, self.entry, start, today, warn=True
            )
            (covered if measured.covered else faults).append(measured.pair_fault)
            # The injection pair too, the one injection wiring whose half can
            # go silent. Not without a solar regime: the bill does not read
            # those meters, so a broken one leaves nothing short.
            day_id, night_id, _total = _kwh_sensor_ids(self.entry, "injection")
            if _bills_injection(self.entry) and day_id and night_id:
                injected = await _measured_kwh(
                    self.hass, self.entry, start, today, side="injection", warn=True
                )
                (covered if injected.covered else faults).append(injected.pair_fault)
            # And a side that went silent under the other, which the pair
            # check cannot see: the walks leave a silent consumption meter's
            # days out of both sides and a silent injection meter's feed-in
            # out, so the running cost reads low or high with nothing else to
            # say why.
            sides = await _metered_sides(self.hass, self.entry, start, today)
            if sides is not None:
                faults.extend(sides.silent)

        # Once each: a consumption meter with no statistics is both a volume
        # read on today alone and the silent side under a working feed-in.
        # The covered registers are named only while nothing moves the cost,
        # so the card's wording is true of every sensor it names.
        def _names(found: list[str]) -> list[str]:
            return [name for fault in found for name in fault.split(", ") if name]

        # And the meters each contract held earlier in the year kept, each
        # entry already naming its contract, so it is not split like the rest.
        previous: list[str] = []
        with noting_failed_reads() as failed_before, contextlib.suppress(Exception):
            previous = await previous_meter_faults(self.hass, self.entry, today)
        if failed or failed_before:
            return False
        names = _names(faults) + previous
        self._register_pair_covered = not names
        self._register_pair_fault = ", ".join(dict.fromkeys(names or _names(covered)))
        return True

    def _reresolve_snapshot(self) -> None:
        """Re-apply the site facts to the card already in hand, if they moved.

        ``_set_snapshot`` resolves against the yearly volume known when it
        ran, and a card restored from the store is resolved before the first
        measurement exists. Nothing calls ``_set_snapshot`` again until the
        supplier publishes, so without this an entry kept the split it booted
        with for up to a month.

        Compared against what the resolver would use NOW, and stamped with
        what it actually used, both through the same ``entry_annual_kwh``
        call. The stamp used to be the raw measurement while the resolver read
        the figure through ``entry.runtime_data``, which setup assigned only
        after the first refresh at the time, so the card was split against the
        household default under a stamp saying the measurement had been
        applied, and this method saw nothing to redo until the trailing-year
        figure next moved. Identity while the resolved figures have not
        moved, which is every tick but the first of a day one of them changed
        on.

        TWO inputs, not one. Brugel's Brussels power term is fetched on the
        tick and cached in a module global, so it is absent after every
        restart: ``async_load_persistent`` resolves the stored card before the
        first refresh can fill it, correctly leaving the term out, and no later
        tick puts it back, because the arm that keeps a card it already has
        resolves nothing and this method asked only about the volume. A
        Brussels entry therefore billed 50,07 EUR a year less until the yearly
        volume next moved, and one with no meter configured never healed at
        all. Both are stamped now, and either moving re-resolves.
        """
        if self._snapshot_raw is None:
            return
        annual_kwh = entry_annual_kwh(self.entry, self)
        power_term = cached_power_term(dt_util.now().year)
        vat = _vat_now()
        if (
            self._snapshot_annual_kwh == annual_kwh
            and self._snapshot_power_term == power_term
            and self._snapshot_vat == vat
        ):
            return
        self._snapshot = _resolve_snapshot(
            self.entry, self._snapshot_raw, annual_kwh=annual_kwh
        )
        self._snapshot_annual_kwh = annual_kwh
        self._snapshot_power_term = power_term
        self._snapshot_vat = vat

    def _set_snapshot(self, snap: SupplierSnapshot | None) -> None:
        """Keep the card as parsed and resolve this entry's VAT preference.

        Every path that produces a snapshot - a fetch, a sibling's shared
        copy, the persisted cache, the custom-formula build - lands here,
        so the VAT choice is applied exactly once and never leaks into
        what other entries on the same tuple see.
        """
        self._snapshot_raw = snap
        # The volume is resolved HERE and handed down, not read back through
        # entry.runtime_data inside _resolve_snapshot. Home Assistant assigned
        # runtime_data only after the first refresh returned at the time, so on
        # that tick the resolver could not see the measurement, resolved the
        # tranche against the household default, and the stamp below then
        # claimed the measured figure had been used, which is exactly what
        # stopped _reresolve_snapshot from ever correcting it.
        #
        # Past tense, like the docstring above says it: that is the defect this
        # shape was written for, not a rule about what the current core does.
        # Resolving here and handing the figure down is right whatever the core
        # does with runtime_data, because it is the only way the stamp can
        # record what the resolver actually used.
        annual_kwh = entry_annual_kwh(self.entry, self)
        self._snapshot = (
            None
            if snap is None
            else _resolve_snapshot(self.entry, snap, annual_kwh=annual_kwh)
        )
        self._snapshot_annual_kwh = annual_kwh
        self._snapshot_power_term = cached_power_term(dt_util.now().year)
        self._snapshot_vat = _vat_now()
        # Every snapshot that reaches here was parsed by the running extractor,
        # so this is what _save_persistent stamps. _replay_stale_snapshot is
        # the one caller that overrides it afterwards, and it has to: without
        # this reset an entry that had replayed once would keep writing v16
        # even after its supplier went back to publishing text, and its cache
        # would be rejected on every boot from then on.
        self._snapshot_schema_version = _SNAPSHOT_SCHEMA_VERSION
        # A snapshot in hand supersedes the blob the schema gate rejected, so a
        # healthy entry that upgraded across a bump does not carry it for the
        # life of the coordinator. async_load_persistent assigns
        # _stale_snapshot after its own _set_snapshot(None), so the load path
        # still keeps what it rejected.
        self._stale_snapshot = None

    def _restore_read_by_ocr(self, blob: dict[str, Any]) -> None:
        """Take the OCR marker back from a stored blob, with its notice.

        Shared by the load path and the degraded replay, so a blob served
        either way says where its figures came from, is written back with
        the marker still on it, and is not offered as the entry's own row.
        The replay restored the timestamp, the probe key and the schema
        version and left the marker behind, so an Ecofix entry restarted on a
        bumped schema served the picture's figures as a text card for a tick
        and saved the blob without the marker.
        """
        self._card_read_by_ocr = blob.get("_read_by_ocr") is True
        if self._card_read_by_ocr:
            self._sync_card_read_by_ocr_issue(True)

    def _replay_stale_snapshot(self, reason: str, *, refetch: bool = False) -> None:
        """Serve the blob the schema gate rejected, because nothing replaced it.

        Reached when no fetch is left to heal with: the card downloaded fine
        and carries no text layer, which no amount of parser work can read,
        or the supplier has left the market and its final card is the last
        it will ever publish. Either way the choice is this months-old card
        or no prices at all. The gate is right in every other case and
        stays: it is how a parser fix reaches a cached user. ``reason`` is
        the clause the log line hangs on the supplier's name.

        Also reached, with ``refetch``, when a fetch fails for an entry left
        with no card. An upgrade across a schema bump drops the stored card
        and counts on the next fetch, and a card the parser cannot read that
        day made that every entity unavailable: TotalEnergies served its
        Dutch myComfort card at the French address in October 2026. The
        replayed card then leaves its probe key behind, so the next refresh
        asks the supplier again and a readable card still replaces it.

        Refused below ``_DEGRADED_MIN_SCHEMA_VERSION``, where the stored fields
        do not mean what they say any more. Above it the replayed card is
        stale, not misread: it keeps the timestamp it was fetched at, so
        snapshot_age reads honestly and the 7-day stale card fires on its own,
        and it is written back under its own schema version so a later parser
        fix can still invalidate it.
        """
        blob = self._stale_snapshot
        if blob is None:
            return
        try:
            snap = _snapshot_from_dict(
                blob, min_schema_version=_DEGRADED_MIN_SCHEMA_VERSION
            )
            fetched_at = datetime.fromisoformat(blob["_cached_at"])
        except (KeyError, ValueError, TypeError) as err:
            _LOGGER.warning(
                "cannot replay the cached snapshot for %s: %s",
                self.entry.entry_id,
                err,
            )
            self._stale_snapshot = None
            return
        self._set_snapshot(snap)
        self._snapshot_fetched_at = fetched_at
        cached_probe = blob.get("_probe_key")
        self._snapshot_probe_key = (
            cached_probe if isinstance(cached_probe, str) and not refetch else None
        )
        self._snapshot_schema_version = int(blob.get("_schema_version", 1))
        self._restore_read_by_ocr(blob)
        _LOGGER.warning(
            "%s %s; serving the cached card of %s (schema v%d) rather than "
            "no prices at all",
            self.entry.data.get(CONF_SUPPLIER),
            reason,
            fetched_at.date().isoformat(),
            self._snapshot_schema_version,
        )

    async def _maybe_refresh_snapshot(self) -> None:
        """Run a cheap probe; only refetch the full PDF when it says so.

        Two paths depending on what the supplier exposes:

          * **Probe available**: call ``extractor.probe`` (HEAD or small
            listing GET). If the returned key matches what we last saved,
            the snapshot is still valid; just stamp ``_snapshot_fetched_at``
            and return. If the key changed, fall through to a real fetch.

          * **No probe**: fall back to the time-based TTL, refetching
            when the snapshot is older than ``SNAPSHOT_REFRESH_HOURS`` (24h).
            DATS 24, Engie and Luminus take this path.

        The shared (supplier, contract, region) cache short-circuits the
        same way: a probe-key match against a sibling coordinator's
        snapshot adopts it without doing any work.
        """
        # Cleared per attempt, not per supplier, for the same reason
        # CardNotReadableError is raised per download: a supplier that goes
        # back to publishing text has to stop being unreadable on its own.
        self._card_unreadable = False
        if self._supply_ended():
            # The supplier has left the market: its final card stays up and
            # stays stale for good, and asking for it every hour only logged
            # two warnings per tick against a card that is gone. Keep serving
            # what is held; the year-to-date still reads the archive through
            # fetch_for_month, which is not this path.
            #
            # Held, or refused by the schema gate on load. With no fetch left
            # to heal with, the refused blob is replayed the way an unreadable
            # card's is: after a bump the final card is this or nothing, and
            # without it a DATS 24 entry lost every entity on the first
            # restart after v60, asked nobody and sat in SETUP_RETRY for good.
            if self._snapshot is None:
                self._replay_stale_snapshot("has left the market")
            return
        result = await fetch_shared(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data[CONF_REGION],
            supplier=self.entry.data[CONF_SUPPLIER],
            # Our own row is offered as a cache entry of equal standing, so the
            # freshness rule lives in one place instead of here as well. Built
            # from _snapshot_raw, never _snapshot: the resolved copy carries
            # this entry's VAT preference, and seeding the shared cache from it
            # would mis-price every sibling on the tuple.
            local=(
                _SharedSnapshot(
                    snapshot=self._snapshot_raw,
                    fetched_at=self._snapshot_fetched_at,
                    probe_key=self._snapshot_probe_key,
                )
                if self._snapshot_raw is not None
                and self._snapshot_fetched_at is not None
                # An OCR reading is served, never offered: it is the
                # archive's fallback rather than a card this entry fetched,
                # so the tick asks the supplier again in case readable cards
                # came back, and no sibling adopts a picture's figures as a
                # text card. Offered, a probe-less supplier kept it for the
                # whole TTL and its siblings took it with the notice cleared.
                and not self._card_read_by_ocr
                else None
            ),
            force=self._force_refresh,
        )

        if result.source == "backoff":
            # A sibling failed on this tuple moments ago. Take its reason, so a
            # cold-start coordinator reports the real failure rather than "cold
            # start", and leave the snapshot alone, or with none fall back as
            # the failure itself would.
            self._last_error = result.error_message
            if self._snapshot is None:
                await self._serve_stand_in_card(result.error_message)
            return

        if result.source == "local" and result.row is not None:
            # Our own row stood. Nothing to re-resolve: the snapshot already IS
            # this entry's, and putting it back through _set_snapshot would
            # resolve VAT a second time on every quiet tick. Only the clock
            # moves, and only when a probe actually answered: stamping it on
            # a TTL match would push the expiry out every tick and the supplier
            # would never be re-fetched at all.
            self._snapshot_fetched_at = result.row.fetched_at
            # Clearing a stale error here is gated on the probe for the same
            # reason: a TTL match says our row has not expired, not that the
            # supplier is reachable, and a failed or absent probe is not proof
            # of recovery. Without this a single-entry install would keep a
            # "could not reach the supplier" card until the published card
            # changed; with it relaxed, one would clear while the supplier was
            # still down.
            if result.probe_confirmed:
                self._last_error = ""
                _shared_failed_fetches(self.hass).pop(self._shared_key(), None)
            return

        if result.row is not None:
            self._set_snapshot(result.row.snapshot)
            self._snapshot_fetched_at = result.row.fetched_at
            self._snapshot_probe_key = result.row.probe_key
            self._last_error = ""
            # A card with a text layer came in, ours or a sibling's, so the
            # notice that the prices were read off a picture of the card no
            # longer holds. This is the one arm a readable card lands on,
            # which is why the reset lives here and nowhere else.
            self._card_read_by_ocr = False
            self._sync_card_read_by_ocr_issue(False)
            # No pop here. A successful fetch already clears the negative row
            # inside fetch_shared, and the ADOPT arm must not: adopting a
            # sibling's card says nothing about whether the supplier answered
            # us, so resetting the consecutive-failure counter there delays
            # the "could not reach the supplier" card, or suppresses it while
            # a quiet sibling keeps re-adopting. Clearing _last_error is
            # right, and is what the arm this replaced did.
            if result.source == "fetch":
                # Only a real fetch satisfies a forced refresh, and only a real
                # fetch clears the extractor issue.
                self._force_refresh = False
                self._sync_extractor_issue(None)
            return

        err = result.error
        assert err is not None
        self._last_error = result.error_message
        # A transient network failure (timeout / reset / 5xx / anti-bot 403)
        # usually recovers on the next tick, so defer its softer "could not
        # reach the supplier" card until it has crossed the threshold. A parse
        # error / 404 / non-PDF payload will not self-heal, so raise its card
        # on the first failure.
        transient = isinstance(err, asyncio.TimeoutError) or is_transient_fetch_error(
            result.error_message
        )
        # A card with no text layer is a third case: it downloaded fine and no
        # parser change can read it, so the user needs the workaround rather
        # than a request to report a layout change. Derived from THIS download,
        # so it stops by itself when the supplier publishes text again.
        unreadable = isinstance(err, CardNotReadableError)
        self._card_unreadable = unreadable
        if unreadable and await self._serve_card_read_by_ocr():
            return
        missing = is_missing_card_error(result.error_message)
        contract = self._entry_contract()
        if missing and contract is not None and contract.withdrawn is not None:
            await self._keep_withdrawn_card(contract.withdrawn, result.error_message)
            return
        if unreadable and self._snapshot is None:
            # Nothing left to keep serving. The blob the schema gate rejected
            # on load is the only card this entry will ever have, so replay it
            # here, before the repair below picks which of the two unreadable
            # cards to raise.
            self._replay_stale_snapshot("publishes its tariff card as page images")
        elif self._snapshot is None:
            await self._serve_stand_in_card(result.error_message)
        if not transient:
            # A 404 or 410, or a web page where the card should be, says the
            # supplier has no card at that address, which is a late card, a
            # withdrawn product or a moved one, never a layout to report.
            self._sync_extractor_issue(
                result.error_message,
                transient=False,
                unreadable=unreadable,
                missing=missing,
            )
        elif result.fail_count >= _EXTRACTOR_ISSUE_THRESHOLD:
            self._sync_extractor_issue(result.error_message, transient=True)
        # An error no extractor raises on purpose is a parser meeting a layout
        # it does not expect. Its traceback is logged for the bug report, but
        # it is not raised: that escaped the tick and made every entity
        # unavailable although the cached card could still price them. With
        # no card in hand the tick fails anyway, on "no supplier snapshot".
        expected = isinstance(err, (ExtractorError, asyncio.TimeoutError))
        _LOGGER.warning(
            "snapshot refresh failed for %s/%s: %s; %s (consecutive failure %d)",
            self.entry.data.get(CONF_SUPPLIER),
            self.entry.data.get(CONF_CONTRACT),
            result.error_message,
            "keeping cached" if self._snapshot is not None else "no card to price with",
            result.fail_count,
            exc_info=None if expected else err,
        )

    async def _serve_card_read_by_ocr(self) -> bool:
        """Price this month off the archive's reading of an unreadable card.

        The supplier published its card as page images, so nothing here can
        parse it. The repository's daily walk reads those with an OCR engine
        and files the result as an ordinary row; this reads that row. True
        when one was found and adopted, and the caller stops treating the
        tick as a failure.

        Deliberately not cached as a probe hit: the row is a fallback, not a
        card this entry fetched, and the next tick should ask the supplier
        again in case readable cards have come back.
        """
        try:
            archived = await card_for_unreadable_month(
                self._session,
                self.entry.data[CONF_SUPPLIER],
                self.entry.data[CONF_CONTRACT],
                self.entry.data[CONF_REGION],
                dt_util.now().date(),
                self.entry,
            )
        except Exception as err:  # noqa: BLE001 - a blip on the archive is not this tick's problem
            _LOGGER.debug("card archive read failed for an unreadable card: %s", err)
            return False
        if archived is None:
            return False
        # The card is still one no reader here can read, so the Repairs card
        # that says so would be true, but the entry is being priced, which
        # is the opposite of what it says. This one replaces it, and says
        # where the figures came from instead.
        self._adopt_archived_card(archived)
        return True

    async def _keep_withdrawn_card(self, withdrawn: date, error: str) -> None:
        """Keep a withdrawn product priced once its card's address is gone.

        The contract_withdrawn card already says the entry stays on the
        product's last card, so a file the supplier took down is expected
        rather than a card gone missing, and raises nothing of its own. With
        a card in hand that is all. With none, after a restart or a schema
        bump that refused the stored one, the archive's copy of the last
        month the product was sold stands in, and failing that the refused
        blob, as for a supplier that has left the market. With neither (the
        archive box unticked or the archive out of reach, and nothing stored)
        the sensors are unavailable whatever the withdrawn-product card says,
        so the missing-card card is raised to say why, and clears by itself
        once a card is found.
        """
        if self._snapshot is None:
            try:
                archived = await card_of_month_before(
                    self._session,
                    self.entry.data[CONF_SUPPLIER],
                    self.entry.data[CONF_CONTRACT],
                    self.entry.data[CONF_REGION],
                    withdrawn,
                    self.entry,
                )
            except Exception as err:  # noqa: BLE001 - a blip on the archive is not this tick's problem
                _LOGGER.debug(
                    "card archive read failed for a withdrawn product: %s", err
                )
                archived = None
            if archived is not None:
                self._adopt_archived_card(archived)
            else:
                self._replay_stale_snapshot(f"no longer sells this product ({error})")
        self._sync_extractor_issue(
            error if self._snapshot is None else None, missing=True
        )

    async def _serve_stand_in_card(self, error: str) -> None:
        """Give an entry left with no card the closest one there is.

        Any failure but page images can heal on a later fetch, but until then
        the card the schema gate rejected beats no prices at all, and last
        month's card beats it being missing too.
        """
        self._replay_stale_snapshot(f"could not be refreshed ({error})", refetch=True)
        if self._snapshot is None:
            await self._serve_former_month_card(error)

    async def _serve_former_month_card(self, error: str) -> None:
        """Price an entry left with no card off last month's, from the archive.

        Reached when a fetch fails and nothing is held, not even a card an
        upgrade set aside: an entry set up while its supplier's card cannot
        be read. The archive's row for the month before is the closest card
        there is. Unlike a withdrawn product's last card it is no answer, so
        the failure's Repairs card stays up, and it is dated to the first day
        of its month with no probe key: the snapshot reads as old, no sibling
        takes it for a fresh card, and the next refresh asks the supplier.
        """
        today = dt_util.now().date()
        try:
            archived = await card_of_month_before(
                self._session,
                self.entry.data[CONF_SUPPLIER],
                self.entry.data[CONF_CONTRACT],
                self.entry.data[CONF_REGION],
                today,
                self.entry,
            )
        except Exception as err:  # noqa: BLE001 - a blip on the archive is not this tick's problem
            _LOGGER.debug("card archive read failed for last month's card: %s", err)
            return
        if archived is None:
            return
        former = month_before(today)
        self._set_snapshot(archived.snapshot)
        self._snapshot_fetched_at = datetime(former.year, former.month, 1, tzinfo=UTC)
        self._snapshot_probe_key = None
        self._card_read_by_ocr = archived.read_by_ocr
        self._sync_card_read_by_ocr_issue(archived.read_by_ocr)
        _LOGGER.warning(
            "%s could not be refreshed (%s); serving the card archive's card "
            "of %s rather than no prices at all",
            self.entry.data.get(CONF_SUPPLIER),
            error,
            former.strftime("%Y-%m"),
        )

    def _adopt_archived_card(self, archived: ArchivedCard) -> None:
        """Price the entry off a row of the repository's card archive, as a
        fallback rather than a card this entry fetched."""
        self._set_snapshot(archived.snapshot)
        self._snapshot_fetched_at = dt_util.utcnow()
        self._snapshot_probe_key = None
        self._last_error = ""
        self._card_read_by_ocr = archived.read_by_ocr
        self._sync_extractor_issue(None)
        self._sync_card_read_by_ocr_issue(archived.read_by_ocr)

    def _snapshot_age_hours(self) -> float:
        if self._snapshot_fetched_at is None:
            return float("inf")
        return (dt_util.utcnow() - self._snapshot_fetched_at).total_seconds() / 3600.0
