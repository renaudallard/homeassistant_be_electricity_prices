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

"""What the coordinator keeps on disk between restarts.

The entry's Store holds the parsed card, the archived month cards the
year-to-date walk bills each past month with, the historical spots, the
capacity peaks and the last ranking. Loading it back is not a plain
deserialisation: every row is re-checked against the schema version and the
clock before it is trusted, because a row that no longer parses or is no
longer settled has to be re-fetched rather than believed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import CONF_CONTRACT, CONF_REGION, CONF_SUPPLIER
from datetime import UTC, date, datetime, timedelta
from .coordinator_profiles import (
    _load_profile_cache,
    _save_profile_cache,
    _seed_profile_cache,
)
from .snapshot_codec import _snapshot_from_dict, _snapshot_to_dict
from .coordinator_spots import _spot_is_sane, _spots_for_local_days
from .cohort import _tariff_card_month, ytd_window_start
from homeassistant.util import dt as dt_util
from .snapshot_months import monthly_rows_to_store, restore_monthly_rows
from .providers.base import SupplierSnapshot
from .contract_periods import PricedPeriods, priced_from_dict, priced_to_dict
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
import logging

_LOGGER = logging.getLogger(__name__)


class _PersistMixin:
    """Mixed into BePricesCoordinator."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _historical_spot_quarters: dict[datetime, list[float]]
    _historical_spots: dict[datetime, float]
    _peak_history: dict[str, float]
    _peak_kw: float
    _peak_month: date | None
    _previous_priced: PricedPeriods | None
    _quarter_grid_days: set[date]
    _saved_payload: dict[str, Any] | None
    _snapshot_fetched_at: datetime | None
    _snapshot_probe_key: str | None
    _snapshot_raw: SupplierSnapshot | None
    _spot_cache: dict[datetime, float]
    _stale_snapshot: dict[str, Any] | None
    _store: Store[dict[str, Any]]
    _supplier_tuple: tuple[str, str, str]
    daily_compare: Any
    entry: ConfigEntry
    _card_read_by_ocr: bool
    _snapshot_schema_version: int
    _unloaded: bool
    hass: HomeAssistant

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins. Declared
        # for the type checker rather than inherited, so each mixin is checked
        # on its own and BePricesCoordinator's bases say how they compose. The
        # one mixin composed anywhere else is the profiles mixin, which the
        # spots mixin extends.
        def _prune_historical_spots(self) -> None: ...
        def _restore_read_by_ocr(self, blob: dict[str, Any]) -> None: ...
        def _set_snapshot(self, snap: SupplierSnapshot | None) -> None: ...

    async def async_load_persistent(self) -> None:
        """Restore the latest snapshot + monthly peak from HA Store."""
        # Before the entry's own blob is even looked at, and whether or not it
        # has one: the Synergrid curves live in a store shared by the whole
        # installation, so a new entry, or one whose blob was discarded, still
        # finds what another entry already downloaded rather than fetching the
        # file again.
        await _load_profile_cache(self.hass)
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
                self._restore_read_by_ocr(snap)
            except (KeyError, ValueError, TypeError) as err:
                _LOGGER.warning(
                    "discarding cached snapshot for %s: %s",
                    self.entry.entry_id,
                    err,
                )
                self._set_snapshot(None)
                self._snapshot_fetched_at = None
                self._snapshot_probe_key = None
                # Hold on to the rejected blob. For a supplier whose card can
                # still be fetched the next _set_snapshot drops it again and
                # this costs one dict; for one publishing page images there is
                # no next fetch, and dropping it here is what took every
                # Ecofix entry off the air on the first restart after 0.11.32.
                self._stale_snapshot = snap
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
        # The archived per-month cards, behind the same tuple gate as the
        # snapshot: they are one contract's published rates, and serving them
        # for another one would bill the year-to-date off a card the household
        # never had. Restoring them is what keeps a restart from re-fetching a
        # PDF per elapsed month: 226 s of it on a Raspberry Pi with Frank
        # Energie, which is what cancelled setup in issue #88.
        stored_months = stored.get("monthly_cards")
        if isinstance(stored_months, dict) and not tuple_mismatch:
            restored = restore_monthly_rows(
                self.hass, *self._supplier_tuple, stored_months
            )
            if restored:
                _LOGGER.debug(
                    "restored %d archived month card(s) for %s",
                    restored,
                    self.entry.entry_id,
                )
        # Same tuple_mismatch gate as the snapshot above: ENTSO-E spots
        # were collected while the entry was on a *dynamic* contract on
        # the previous tuple. After an OptionsFlow swap to a static
        # supplier they're never queried again but would otherwise be
        # re-saved for a year (the prune keeps a trailing year), wasting
        # up to ~0,4 MB of disk and memory.
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
        grid_days = stored.get("historical_spot_quarter_days")
        if isinstance(grid_days, list) and not tuple_mismatch:
            for k in grid_days:
                if isinstance(k, str):
                    try:
                        self._quarter_grid_days.add(date.fromisoformat(k))
                    except ValueError:
                        continue
        # Today's (and tomorrow's) day-ahead curve, so an ENTSO-E outage that
        # spans a restart still has something to price with. _historical_spots
        # above cannot stand in for it: it is only ever filled up to today, so
        # it never holds tomorrow, and it is bucketed to the hour, so it cannot
        # give a quarter-hourly contract its own slots back.
        #
        # Restored as a FALLBACK only: _spot_cache_day deliberately stays
        # None, so the first tick still fetches from ENTSO-E as it always did
        # and this is consulted only when that fetch fails. That also avoids
        # adopting a partially-written curve as authoritative for the day.
        cached_curve = stored.get("spot_cache")
        if isinstance(cached_curve, dict) and not tuple_mismatch:
            for k, v in cached_curve.items():
                if not isinstance(k, str) or not isinstance(v, (int, float)):
                    continue
                try:
                    when = datetime.fromisoformat(k)
                except ValueError:
                    continue
                if when.tzinfo is None:
                    when = when.replace(tzinfo=UTC)
                if not _spot_is_sane(float(v)):
                    dropped_spots += 1
                    continue
                self._spot_cache[when] = float(v)
            # Drop whatever the blob outlived. Gating on load rather than on
            # save keeps the rule in one place, and the cache is two days of
            # slots at most either way.
            local_today = dt_util.now().date()
            self._spot_cache = _spots_for_local_days(
                self._spot_cache, {local_today, local_today + timedelta(days=1)}
            )
        if dropped_spots:
            _LOGGER.warning(
                "Discarded %d cached day-ahead price(s) outside the publishable "
                "range for %s; a day missing several of them is re-fetched from "
                "ENTSO-E",
                dropped_spots,
                self.entry.title,
            )
        # Same tuple gate as the snapshot and the spots: a ranking is priced
        # against the household's own contract, so one computed before an
        # OptionsFlow swap compares them to a contract they no longer hold.
        # That is worse than showing nothing, because the sensor reads as a
        # live figure either way.
        stored_compare = stored.get("daily_compare")
        if isinstance(stored_compare, dict) and not tuple_mismatch:
            self.daily_compare = _daily_compare_from_dict(stored_compare)
        # The earlier contracts' pricing, outside the tuple gate: it carries
        # the periods it was priced for, and the tick serves it only for those,
        # so a switch recorded since simply leaves it unused.
        stored_previous = stored.get("previous_contracts")
        if isinstance(stored_previous, dict):
            self._previous_priced = priced_from_dict(stored_previous)
        # A blob written before the profiles moved to the shared store carries
        # them still, and adopting those is what keeps an upgrade from
        # downloading again what this entry already had. Irrespective of the
        # entry-tuple gate above, since a national curve does not depend on a
        # supplier. The store itself was read at the top of this method.
        seeded = False
        for kind in ("spp", "rlp"):
            legacy = stored.get(f"{kind}_weights")
            if isinstance(legacy, dict):
                seeded |= _seed_profile_cache(self.hass, kind, legacy)
        if seeded:
            # Write what the legacy blob gave us into the shared store now. The
            # blob is not rewritten with these keys, so without this the curve
            # would be gone by the next restart and downloaded again.
            await _save_profile_cache(self.hass)

    def _persisted_months(self, today: date) -> list[date]:
        """The months whose archived card is worth keeping on disk.

        The year-to-date window, plus the month of the card the entry is
        billed on when it names one, whether through its own tariff card month
        or through its start date. That one is not part of the walk, it can be
        years back, but ``_cohort_legs`` resolves it on every tick to freeze
        the rate the customer signed for, and it does so INSIDE config-entry
        setup, because the live price table is built from it. One row on disk
        is what keeps that from being a card fetch on every restart.
        """
        months = self._ytd_months(today)
        signing = _tariff_card_month(self.entry)
        if signing is not None and signing not in months:
            months.append(signing)
        return months

    def _ytd_months(self, today: date) -> list[date]:
        """First of each month the year-to-date walk covers, up to today."""
        months: list[date] = []
        cur = ytd_window_start(self.entry, today)
        while cur <= today:
            months.append(date(cur.year, cur.month, 1))
            cur = (
                date(cur.year + 1, 1, 1)
                if cur.month == 12
                else date(cur.year, cur.month + 1, 1)
            )
        return months

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
        # that runs from ``async_config_entry_first_refresh``: only
        # skip the save when it has been explicitly assigned to a
        # *different* coordinator.
        # Local: the concrete class mixes this one in, so importing it at the
        # top would close a cycle. The check is a real isinstance and needs the
        # class itself, not an annotation.
        from .coordinator import BePricesCoordinator

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
                schema_version=self._snapshot_schema_version,
            )
            if self._card_read_by_ocr:
                # Restored with the card, so a restart keeps saying where the
                # figures came from until a readable card lands.
                payload["snapshot"]["_read_by_ocr"] = True
        # Prune in memory (not just in the serialized copy) so a long-running
        # coordinator keeps a trailing year of hours and no more.
        self._prune_historical_spots()
        # The archived cards each past month is billed with. Written under
        # the tuple guard above like everything else, and only for the months
        # the year-to-date window covers, so the blob does not grow past a
        # year of them (about 5 KB apiece).
        monthly_cards = monthly_rows_to_store(
            self.hass,
            *self._supplier_tuple,
            self._persisted_months(dt_util.now().date()),
        )
        if monthly_cards:
            payload["monthly_cards"] = monthly_cards
        if self._historical_spots:
            payload["historical_spots"] = {
                h.isoformat(): v for h, v in self._historical_spots.items()
            }
        if self._historical_spot_quarters:
            payload["historical_spot_quarters"] = {
                h.isoformat(): v for h, v in self._historical_spot_quarters.items()
            }
        if self._quarter_grid_days:
            payload["historical_spot_quarter_days"] = sorted(
                d.isoformat() for d in self._quarter_grid_days
            )
        if self._spot_cache:
            payload["spot_cache"] = {
                h.isoformat(): v for h, v in self._spot_cache.items()
            }
        if self.daily_compare is not None:
            payload["daily_compare"] = _daily_compare_to_dict(self.daily_compare)
        if self._previous_priced is not None:
            payload["previous_contracts"] = priced_to_dict(self._previous_priced)
        # Nothing to write when nothing moved. The blob is rebuilt whole on
        # every tick and is mostly slow-changing: the card, the peak history,
        # the spot cache and the compare rows are identical on 23 ticks out of
        # 24, so an hourly entry rewrote 342 KB an hour for one changed
        # timestamp and a quarter-hourly one 1 MB, which is 24 MB a day per
        # entry of disk and of HA's JSON encoder.
        #
        # Compared as the object, before ``async_save`` serialises it, so the
        # skip costs a dict comparison and saves the encode as well as the
        # write. The copy kept here is the payload itself, which nothing
        # mutates afterwards: it is rebuilt from scratch each time.
        if payload == self._saved_payload:
            return
        await self._store.async_save(payload)
        self._saved_payload = payload


def _daily_compare_to_dict(result: Any) -> dict[str, Any]:
    """Flatten one scheduled ranking for the Store.

    Every ``RankedRow`` field is written, the ones the sensor never shows
    included: the options page re-serves these rows to skip a two-minute
    sweep, and a row restored short of a field it reads is the shape that
    crashed that page once already.
    """
    return {
        "ran_at": result.ran_at.isoformat(),
        "own": result.own,
        "priced": result.priced,
        "total": result.total,
        "rows": [
            {
                "label": row.label,
                "annual": row.annual,
                "ytd": row.ytd,
                "status": row.status,
                "is_own": row.is_own,
                "read_by_ocr": row.read_by_ocr,
            }
            for row in result.rows
        ],
    }


def _daily_compare_from_dict(blob: dict[str, Any]) -> Any | None:
    """Rebuild a ranking from the Store, or ``None`` if it is not intact.

    All or nothing on purpose. A ranking is a comparison between its rows, so
    restoring the readable half would silently re-rank the household against a
    subset and name a "cheapest" that only won because its rivals were
    dropped. There is no partial answer worth publishing here.

    No age limit. The sweep already keeps serving yesterday's ranking when a
    run fails, the sensor states ``last_run`` beside the figure, and the next
    scheduled run replaces it, so an old ranking is a dated answer rather than
    a wrong one.
    """
    from .compare_table import (
        DailyCompare,
        RankedRow,
    )

    ran_at_raw = blob.get("ran_at")
    if not isinstance(ran_at_raw, str):
        return None
    try:
        ran_at = datetime.fromisoformat(ran_at_raw)
    except ValueError:
        return None
    if ran_at.tzinfo is None:
        ran_at = ran_at.replace(tzinfo=UTC)
    own = blob.get("own")
    if own is not None and not isinstance(own, (int, float)):
        return None
    priced, total = blob.get("priced"), blob.get("total")
    if not isinstance(priced, int) or not isinstance(total, int):
        return None
    raw_rows = blob.get("rows")
    if not isinstance(raw_rows, list):
        return None
    rows: list[Any] = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            return None
        label, annual = raw.get("label"), raw.get("annual")
        ytd, status, is_own = raw.get("ytd"), raw.get("status", ""), raw.get("is_own")
        if not isinstance(label, str) or not isinstance(status, str):
            return None
        if annual is not None and not isinstance(annual, (int, float)):
            return None
        if ytd is not None and not isinstance(ytd, (int, float)):
            return None
        if not isinstance(is_own, bool):
            return None
        rows.append(
            RankedRow(
                label=label,
                annual=None if annual is None else float(annual),
                ytd=None if ytd is None else float(ytd),
                status=status,
                is_own=is_own,
                # Absent from every blob written before the tag existed, and
                # a ranking restored without it must still render.
                read_by_ocr=raw.get("read_by_ocr") is True,
            )
        )
    return DailyCompare(
        rows=tuple(rows),
        own=None if own is None else float(own),
        priced=priced,
        total=total,
        ran_at=ran_at,
    )
