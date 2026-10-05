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

"""The costs half of an update tick.

The year and the month to date, the rolling-year and year-end projections and
the calendar year's volumes, handed back to the tick as one record. With them
the background fill of the archived month cards and the pricing of the
contracts held earlier in the year.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .coordinator_data import (
    month_window_reset,
    month_window_start,
    ytd_window_reset,
)
from .coordinator_persist import settings_digest
from .providers import (
    SupplierSnapshot,
    get as get_extractor,
)
from .snapshot_store import _monthly_snapshots
from datetime import date, datetime, timedelta
from .cohort import (
    _effective_snapshot_for_month,
    signing_month_snapshot,
    ytd_window_start,
)
from .ytd_cost import _compute_current_year_cost
from .contract_periods import (
    ContractPeriod,
    PricedPeriods,
    current_period_start,
    keep_settled,
    periods_key,
    periods_need_rlp,
    periods_need_spots,
    previous_costs,
    previous_periods,
    price_previous_periods,
)
from .projected_cost import _compute_projected_year_cost
from .projected_volume import _compute_projected_year_kwh, _compute_rolling_year_kwh
from .year_end_cost import _compute_year_end_cost
from .snapshot_months import archived_months_present
import asyncio
from homeassistant.util import dt as dt_util
from .synergrid import RlpWeights, SppWeights
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import aiohttp
import logging
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

# How long an earlier contract's pricing that could not price a period waits
# before it is tried again. Just under the hourly tick, so the next one asks
# rather than the one after it, and well over the few seconds between the
# refresh a pricing asks for and the tick that answers it.
_PREVIOUS_RETRY = timedelta(minutes=50)

# The figures a refresh that read the meters published and the first refresh
# after a restart publishes again, before it reads any (_held_tick_costs).
_HELD_FIGURES = (
    "current_year_cost",
    "month_cost",
    "projected_year_cost",
    "projected_consumption",
    "projected_injection",
    "rolling_consumption",
    "rolling_injection",
    "year_end_cost",
)


@dataclass(frozen=True, slots=True)
class TickCosts:
    """What the costs half of a tick hands back to the tick that publishes it."""

    window_now: datetime
    periods: list[ContractPeriod]
    current_year_cost: float | None
    month_cost: float | None
    ytd_breakdown: dict[str, float]
    signing: SupplierSnapshot
    projected_year_cost: float | None
    projection_breakdown: dict[str, Any]
    projected_consumption: float | None
    projected_injection: float | None
    volume_breakdown: dict[str, dict[str, Any]]
    rolling_consumption: float | None
    rolling_injection: float | None
    rolling_breakdown: dict[str, dict[str, Any]]
    year_end_cost: float | None
    year_end_breakdown: dict[str, Any]


def held_costs_blob(
    entry: ConfigEntry, costs: TickCosts, inputs: str
) -> dict[str, Any]:
    """``costs`` as the entry's store keeps them for ``_held_tick_costs``.

    With the windows they cover and the settings they were priced under,
    ``inputs``, the digest the tick took before it read anything: an edit
    saved while the tick runs reloads the entry, but the tick finishes
    first. A restart serves them only while both still hold.
    """
    blob: dict[str, Any] = {name: getattr(costs, name) for name in _HELD_FIGURES}
    blob["inputs"] = inputs
    blob["year_reset"] = ytd_window_reset(entry, costs.window_now).isoformat()
    blob["month_reset"] = month_window_reset(entry, costs.window_now).isoformat()
    return blob


class _CostsMixin:
    """Mixed into BePricesCoordinator."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _held_costs: dict[str, Any] | None
    _historical_spot_quarters: dict[datetime, list[float]]
    _historical_spots: dict[datetime, float]
    _previous_priced: PricedPeriods | None
    _previous_pricing: asyncio.Task[None] | None
    _previous_tried: tuple[str, datetime] | None
    _year_end_memo: tuple[tuple[Any, ...], float | None, dict[str, Any]] | None
    _rlp_blend: str
    _rlp_fetched_at: datetime | None
    _rlp_weights: RlpWeights
    _rlp_weights_year: int | None
    _session: aiohttp.ClientSession
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _spp_fetched_at: datetime | None
    _spp_weights: SppWeights
    _spp_weights_year: int | None
    _supplier_tuple: tuple[str, str, str]
    entry: ConfigEntry
    _month_cards_deferred: bool
    _unloaded: bool
    hass: HomeAssistant

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins. Declared
        # for the type checker rather than inherited, so each mixin is checked
        # on its own and BePricesCoordinator's bases say how they compose. The
        # one mixin composed anywhere else is the profiles mixin, which the
        # spots mixin extends.
        async def _ensure_historical_spots(
            self, start: date, end: date, api_key: str | None = None
        ) -> None: ...
        async def _ensure_rlp_weights(self, blend: str = "distinct") -> None: ...
        async def async_request_refresh(self) -> "None": ...
        def _billed_peak_kw(self) -> float: ...
        def _ytd_months(self, today: date) -> list[date]: ...

    async def _tick_costs(
        self,
        priced: SupplierSnapshot,
        injection_snapshot: SupplierSnapshot,
        energy_mean: float | None,
        spp_weighted: bool,
        rlp_weighted: bool,
        allocating: bool,
        billed_peak: float,
    ) -> TickCosts:
        """The bills: the year and the month to date, the projections and the year end."""
        # _update_body raised before calling this when there was no card.
        assert self._snapshot is not None
        ytd_breakdown: dict[str, float] = {}
        # FIRST tick only, and for the same reason the year's spots are
        # deferred above: this one runs inside config-entry setup. The
        # year-to-date walk bills each past month with that month's own
        # archived card, one PDF apiece, and a Frank Energie card takes about
        # 25 s to lay out on a Raspberry Pi: 226 s for a September start,
        # against the 300 s Home Assistant allows the whole of bootstrap
        # stage 2. That is what cancelled setup in issue #88.
        #
        # So walk the months against whatever cards the process already holds
        # (after a restart, the ones restored from the store), and fetch the
        # rest in the background. What the deferral costs is the months still
        # missing: they bill their fees, network and tax legs off the current
        # card rather than their own, which is what a supplier with no archive
        # bills all year anyway, and the refresh the fill requests puts the
        # right ones back.
        cached_months_only = self._month_cards_deferred
        # One reading of the clock for both windows and the resets published
        # beside them, so a figure and its last_reset always name one period.
        window_now = dt_util.now()
        ytd_start = ytd_window_start(self.entry, window_now.date())
        # A recorded switch splits the window: this contract bills from the day
        # it started and the earlier ones are added from their own pricing
        # below. Without one this is the window's first day, as it always was.
        periods = previous_periods(self.entry.data, ytd_start, window_now.date())
        own_start = current_period_start(self.entry.data, ytd_start)
        current_year_cost = await _compute_current_year_cost(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self._snapshot,
            self.entry,
            historical_spots=self._historical_spots,
            spot_quarters=self._historical_spot_quarters,
            spp_weights=self._spp_weights if spp_weighted else None,
            rlp_weights=(
                (self._rlp_weights or None) if (rlp_weighted or allocating) else None
            ),
            breakdown=ytd_breakdown,
            billed_peak_kw=billed_peak,
            cached_only=cached_months_only,
            window_start_override=own_start if own_start != ytd_start else None,
            snapshot_raw=self._snapshot_raw,
        )
        # The same bill over the running month. A second pass rather than an
        # accumulator inside the first: the walk has four branches and four
        # fees-floor exits, and a month total threaded through all eight is the
        # shape that drifts. A month is an eighth of a mid-year window, and the
        # month cards and spots the first pass resolved are all cached, so what
        # it costs is one short recorder read and the pricing loop over ~30
        # days.
        month_start = month_window_start(self.entry, window_now.date())
        month_cost = await _compute_current_year_cost(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self._snapshot,
            self.entry,
            historical_spots=self._historical_spots,
            spot_quarters=self._historical_spot_quarters,
            spp_weights=self._spp_weights if spp_weighted else None,
            rlp_weights=(
                (self._rlp_weights or None) if (rlp_weighted or allocating) else None
            ),
            billed_peak_kw=billed_peak,
            cached_only=cached_months_only,
            window_start_override=max(month_start, own_start),
            snapshot_raw=self._snapshot_raw,
        )
        if periods:
            self._schedule_previous_pricing(periods, window_now.date())
        # Unknown rather than short while the earlier contracts are not priced
        # yet, which is only the minutes after a switch is recorded: a year
        # missing a whole contract lands on the recorder as a large negative
        # change and then the same positive one.
        prev_year, prev_month = previous_costs(
            self._previous_priced, periods, month_start
        )
        if current_year_cost is not None:
            current_year_cost = (
                None if prev_year is None else current_year_cost + prev_year
            )
        if month_cost is not None:
            month_cost = None if prev_month is None else month_cost + prev_month
        if periods and prev_year is not None:
            ytd_breakdown["previous_contracts_eur"] = prev_year
        if cached_months_only:
            self._month_cards_deferred = False
            self.entry.async_create_background_task(
                self.hass,
                self._fill_month_cards(),
                f"{DOMAIN}_month_cards_{self.entry.entry_id}",
            )
        projection_breakdown: dict[str, Any] = {}
        # The card the welcome credit is read off, the same row the
        # year-to-date walk just resolved, so a cache hit.
        signing = await signing_month_snapshot(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data.get(CONF_REGION, ""),
            self.entry,
            self._snapshot,
            cached_only=cached_months_only,
        )
        projected_year_cost = await _compute_projected_year_cost(
            self.hass,
            self.entry,
            self._snapshot,
            priced,
            billed_peak_kw=billed_peak,
            today=dt_util.now().date(),
            # The month-baked leg, so the projection credits feed-in at the
            # rate the injection_price sensor shows rather than at the card's
            # printed figure, which is that formula on the previous month.
            credited=injection_snapshot,
            # The day-ahead history, for a feed-in credit that follows the
            # spot price per slot, on the year the compare page credits it
            # on. Not the live table's day or two, which moved this recorded
            # figure by tens of euro a day.
            spots=self._historical_spots,
            signing=signing,
            # The running month's index, held for the year on a month-indexed
            # leg, as current_price bills this month.
            energy_index=energy_mean,
            breakdown=projection_breakdown,
        )
        # The calendar year's metered volume on each side. A profile is used
        # only where the pricing already loaded this year's: never fetched
        # for this.
        today = window_now.date()
        volume_breakdown: dict[str, dict[str, Any]] = {"consumption": {}}
        projected_consumption = await _compute_projected_year_kwh(
            self.hass,
            self.entry,
            today,
            side="consumption",
            profile=self._rlp_weights if self._rlp_weights_year == today.year else None,
            breakdown=volume_breakdown["consumption"],
        )
        # And what each side metered over the last 365 days.
        rolling_breakdown: dict[str, dict[str, Any]] = {"consumption": {}}
        rolling_consumption = await _compute_rolling_year_kwh(
            self.hass,
            self.entry,
            today,
            side="consumption",
            breakdown=rolling_breakdown["consumption"],
        )
        projected_injection = None
        rolling_injection = None
        if self.entry.data.get(CONF_SOLAR_REGIME) in (
            SOLAR_REGIME_COMPENSATION,
            SOLAR_REGIME_INJECTION,
        ):
            rolling_breakdown["injection"] = {}
            rolling_injection = await _compute_rolling_year_kwh(
                self.hass,
                self.entry,
                today,
                side="injection",
                breakdown=rolling_breakdown["injection"],
            )
            volume_breakdown["injection"] = {}
            projected_injection = await _compute_projected_year_kwh(
                self.hass,
                self.entry,
                today,
                side="injection",
                profile=(
                    self._spp_weights if self._spp_weights_year == today.year else None
                ),
                profile_utc=True,
                breakdown=volume_breakdown["injection"],
            )

        # And what the calendar year's bill will stand at on 31 December. It
        # walks the whole year, hour by hour on an hourly-billed contract, and
        # moves only with its inputs, so the last result is reused while none
        # of them changed: the day, once more from 01:00 when yesterday's last
        # hour has compiled, the cards and the month cards held, this month's
        # index, the day-ahead held, the profiles, the billed peak, the window
        # and the earlier contracts. A handful of walks a day instead of 24.
        year_end_key = (
            today,
            window_now.hour >= 1,
            self._snapshot,
            injection_snapshot,
            energy_mean,
            prev_year,
            billed_peak,
            own_start,
            cached_months_only,
            len(self._historical_spots),
            len(self._historical_spot_quarters),
            (self._rlp_weights_year, self._rlp_blend, self._rlp_fetched_at),
            (self._spp_weights_year, self._spp_fetched_at),
            sorted(
                (key[3], card)
                for key, card in _monthly_snapshots(self.hass).items()
                if key[:3] == self._supplier_tuple
            ),
        )
        year_end_breakdown: dict[str, Any] = {}
        year_end_cost = None
        memo = self._year_end_memo
        if memo is not None and memo[0] == year_end_key:
            year_end_cost, year_end_breakdown = memo[1], dict(memo[2])
        elif prev_year is None:
            year_end_breakdown["energy_basis"] = (
                "not projected: the earlier contracts this year are still being priced"
            )
        else:
            year_end_cost = await _compute_year_end_cost(
                self.hass,
                self._session,
                get_extractor(self.entry.data[CONF_SUPPLIER]),
                self._snapshot,
                self.entry,
                injection_snapshot,
                today,
                energy_index=energy_mean,
                previous_eur=prev_year,
                breakdown=year_end_breakdown,
                historical_spots=self._historical_spots,
                spot_quarters=self._historical_spot_quarters,
                spp_weights=self._spp_weights if spp_weighted else None,
                rlp_weights=(
                    (self._rlp_weights or None)
                    if (rlp_weighted or allocating)
                    else None
                ),
                billed_peak_kw=billed_peak,
                cached_only=cached_months_only,
                window_start_override=own_start if own_start != ytd_start else None,
                snapshot_raw=self._snapshot_raw,
            )
        self._year_end_memo = (year_end_key, year_end_cost, dict(year_end_breakdown))
        return TickCosts(
            window_now=window_now,
            periods=periods,
            current_year_cost=current_year_cost,
            month_cost=month_cost,
            ytd_breakdown=ytd_breakdown,
            signing=signing,
            projected_year_cost=projected_year_cost,
            projection_breakdown=projection_breakdown,
            projected_consumption=projected_consumption,
            projected_injection=projected_injection,
            volume_breakdown=volume_breakdown,
            rolling_consumption=rolling_consumption,
            rolling_injection=rolling_injection,
            rolling_breakdown=rolling_breakdown,
            year_end_cost=year_end_cost,
            year_end_breakdown=year_end_breakdown,
        )

    async def _held_tick_costs(self) -> TickCosts:
        """The costs setup's own refresh publishes, without reading a meter.

        Every figure below reads a year of every meter from the recorder, and
        Home Assistant waits on that refresh: on a MariaDB on a NAS the reads
        took most of a 287 s start, close to the 300 s it allows the whole of
        startup (issue #107). So that refresh publishes what the last one
        before the restart did, and setup asks for the one that reads the
        meters once Home Assistant no longer waits on it. A figure is served
        only while it still covers the window the sensor publishes beside it
        and was priced under the entry's settings: a new year or month, or a
        setting edited since, leaves it unknown until that refresh lands
        rather than show a period it does not cover. No breakdown is kept:
        the attributes come back with the figures they explain.
        """
        assert self._snapshot is not None
        window_now = dt_util.now()
        today = window_now.date()
        held = self._held_costs or {}
        priced_under = held.get("inputs") == settings_digest(self.entry)
        same_year = (
            priced_under
            and held.get("year_reset")
            == ytd_window_reset(self.entry, window_now).isoformat()
        )
        same_month = (
            same_year
            and held.get("month_reset")
            == month_window_reset(self.entry, window_now).isoformat()
        )

        def _figure(name: str, valid: bool) -> float | None:
            value = held.get(name)
            if not valid or not isinstance(value, (int, float)):
                return None
            return float(value)

        signing = await signing_month_snapshot(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data.get(CONF_REGION, ""),
            self.entry,
            self._snapshot,
            cached_only=True,
        )
        return TickCosts(
            window_now=window_now,
            periods=previous_periods(
                self.entry.data, ytd_window_start(self.entry, today), today
            ),
            current_year_cost=_figure("current_year_cost", same_year),
            month_cost=_figure("month_cost", same_month),
            ytd_breakdown={},
            signing=signing,
            projected_year_cost=_figure("projected_year_cost", same_year),
            projection_breakdown={},
            projected_consumption=_figure("projected_consumption", same_year),
            projected_injection=_figure("projected_injection", same_year),
            volume_breakdown={},
            rolling_consumption=_figure("rolling_consumption", same_year),
            rolling_injection=_figure("rolling_injection", same_year),
            rolling_breakdown={},
            year_end_cost=_figure("year_end_cost", same_year),
            year_end_breakdown={},
        )

    async def _fill_month_cards(self) -> None:
        """Fetch the year's archived tariff cards, off the setup path.

        Scheduled by the first tick, which priced the year-to-date from the
        cards already in hand so that config-entry setup did not wait on one
        PDF per elapsed month (issue #88). Runs as an entry-tied background
        task, so unloading the entry cancels it.

        Failures need no handling here: _snapshot_for_month logs the month it
        could not fetch, leaves the row uncached and hands back the current
        card, which is the same proxy the deferred tick already billed with.

        The refresh is only asked for when the walk actually retrieved a card
        the tick did not have. A restart on a supplier with no archive, or one
        whose months are all restored from the store, changes nothing, and an
        extra full tick per entry per restart would buy nothing.
        """
        if self._snapshot is None:
            return
        supplier, contract, region = self._supplier_tuple
        extractor = get_extractor(supplier)
        today = dt_util.now().date()
        months = self._ytd_months(today)
        before = archived_months_present(self.hass, supplier, contract, region, months)
        for month_first in months:
            if self._unloaded:
                return
            await _effective_snapshot_for_month(
                self.hass,
                self._session,
                extractor,
                contract,
                region,
                month_first,
                self._snapshot,
                self.entry,
            )
        after = archived_months_present(self.hass, supplier, contract, region, months)
        if self._unloaded or after == before:
            return
        await self.async_request_refresh()

    def _schedule_previous_pricing(
        self, periods: list[ContractPeriod], today: date
    ) -> None:
        """Price the earlier contracts in the background, at most once a day.

        A contract the household has left is a closed window: its figure moves
        only when an archive publishes one of its months, the day-ahead cache
        fills an hour it lacked, or, in Flanders, the billed capacity peak
        moves. Pricing it means fetching the old supplier's cards, which
        neither the tick nor config-entry setup should wait on, so a day old
        stands for the first two, and a peak other than the one it was priced
        on is priced again at once, as the entry's own contract takes it on the
        tick. Stale pricing for the same periods keeps being served until the
        new one lands.

        A pricing that could not price one of the periods at all, or priced
        it on the entry's current card because a read failed just now, is not
        kept for the day: the year reads unknown, or bills those days on
        another supplier's card, while it stands, so the next hourly tick asks
        again rather than tomorrow's. A period no archive kept any card of is
        settled on that stand-in and asked again the next day, as its cards
        will not turn up within the hour. Not sooner: the pricing asks for a
        refresh when it lands, and that refresh is a tick, so without the wait
        a period that cannot be priced was fetched and walked again every few
        seconds.
        """
        key = periods_key(periods)
        peak = self._previous_peak_kw(periods)
        priced = self._previous_priced
        if (
            priced is not None
            and priced.key == key
            and priced.day == today
            and priced.peak_kw == peak
            and all(row.settled for row in priced.rows)
        ):
            return
        if self._previous_pricing is not None and not self._previous_pricing.done():
            return
        # The wait is per peak: a new one is priced at once, and a pricing that
        # failed on this one waits like any other.
        attempt = f"{key} {peak!r}"
        now = dt_util.utcnow()
        tried = self._previous_tried
        if (
            tried is not None
            and tried[0] == attempt
            and now - tried[1] < _PREVIOUS_RETRY
        ):
            return
        self._previous_tried = (attempt, now)
        self._previous_pricing = self.entry.async_create_background_task(
            self.hass,
            self._price_previous(periods, today),
            f"{DOMAIN}_previous_contracts_{self.entry.entry_id}",
        )

    def _previous_peak_kw(self, periods: list[ContractPeriod]) -> float:
        """The billed peak the earlier contracts are priced on: the household's
        own in Flanders, as ``price_previous_periods`` takes it, else none."""
        if any(p.data.get(CONF_REGION) == REGION_FLANDERS for p in periods):
            return self._billed_peak_kw()
        return 0.0

    async def _price_previous(self, periods: list[ContractPeriod], today: date) -> None:
        """Price the earlier contracts, keep the result and ask for a refresh.

        The year's day-ahead first when an old contract settles on it: on the
        day a switch is recorded the tick's own fill is still running in the
        background, and pricing an old dynamic contract before it lands would
        settle that contract without its energy for the whole day. The fill is
        behind the spot lock, so this waits for it rather than fetching twice.
        The load profile likewise, for an old contract settled on its weighted
        mean or netted over it, and the solar profile for one whose feed-in
        settles on it (decided on the card, in ``price_previous_periods``).
        """
        if periods_need_spots(periods):
            # The entry's own key first. A household that left a dynamic
            # contract for a fixed one may hold none any more, and the settings
            # kept with the contract it left still carry the one it used: the
            # day-ahead is the same for every supplier, and the walk refuses to
            # fetch at all without one.
            api_key = self.entry.data.get(CONF_API_KEY) or next(
                (p.data[CONF_API_KEY] for p in periods if p.data.get(CONF_API_KEY)),
                None,
            )
            try:
                await self._ensure_historical_spots(
                    periods[0].start, periods[-1].end, api_key
                )
            except Exception as err:  # noqa: BLE001 - priced on what is cached
                # An hour with no spot still bills its network and tax legs, as
                # it does for the entry's own contract, so price on what the
                # cache holds rather than not at all.
                _LOGGER.debug("Day-ahead history for earlier contracts: %s", err)
        if periods_need_rlp(periods):
            # The profile too, which the first tick after a switch fills in the
            # background and could still be fetching. In the entry's own blend:
            # an old card's index is reduced from the same workbook read, and
            # asking for its blend here would move the live coordinator's.
            await self._ensure_rlp_weights(self._rlp_blend)
        # Read before the pricing reads it, so a peak that moves meanwhile is
        # priced again on the next tick rather than taken as priced.
        peak = self._previous_peak_kw(periods)
        try:
            month_start = month_window_start(self.entry, today)
            rows = await price_previous_periods(
                self.hass,
                self._session,
                self,
                periods,
                month_start=month_start,
                load_profiles=True,
            )
        except Exception as err:  # noqa: BLE001 - the next tick asks again
            _LOGGER.warning(
                "Could not price the contracts %s held earlier this year: %s",
                self.entry.title,
                err,
            )
            return
        if self._unloaded:
            return
        key = periods_key(periods)
        self._previous_priced = PricedPeriods(
            key=key,
            day=today,
            month=month_start,
            rows=keep_settled(self._previous_priced, key, rows),
            peak_kw=peak,
        )
        await self.async_request_refresh()
