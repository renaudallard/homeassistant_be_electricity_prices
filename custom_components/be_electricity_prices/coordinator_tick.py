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

"""One update tick, start to finish.

Fetches the card, resolves the past months, prices the day, and assembles the
CoordinatorData every sensor reads. The background fills that the first tick
defers live here too: setup runs on Home Assistant's stage-2 budget, so the
tick answers from the cache and finishes the slow walks afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
    REGION_BRUSSELS,
    REGION_FLANDERS,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SUPPLIER_CUSTOM,
)
from .coordinator_data import CoordinatorData, month_window_start
from .providers import (
    DynamicRates,
    SpotMonthlyRates,
    SupplierSnapshot,
    get as get_extractor,
)
from .providers._rates import EnergyRates
from .api import EntsoeAuthError, EntsoeError
from collections.abc import Iterable
from .pricing import (
    PriceBreakdown,
    compute_breakdown,
    static_breakdown,
    yearly_fixed_fee_for_meter,
)
from .snapshot_store import SNAPSHOT_STALE_DAYS
from datetime import UTC, date, datetime, timedelta
from homeassistant.helpers.update_coordinator import UpdateFailed
from .injection import (
    _bake_monthly_injection,
    _compute_injection_price,
    _injection_hourly_on_cohort,
    _injection_needs_month_spot,
    _injection_needs_spot,
    _injection_price_for_slot,
    _injection_varies_intraday,
)
from .cohort import (
    _cohort_legs,
    _effective_snapshot_for_month,
    signing_month_snapshot,
    ytd_window_start,
)
from .fees import _compute_capacity, _compute_prosumer
from .ytd_cost import _compute_current_year_cost
from .projected_cost import _compute_projected_year_cost
from .spot_stats import (
    _energy_is_quarter_hourly,
    _energy_is_rlp_indexed,
    _injection_is_spp_indexed,
    _injection_on_month_mean,
    _rlp_blend_for,
    _spp_weighting_enabled,
)
from .snapshot_months import archived_months_present
import asyncio
from homeassistant.util import dt as dt_util
from .brugel import ensure_power_term
from .synergrid import RlpWeights, SppWeights
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import aiohttp
import logging

_LOGGER = logging.getLogger(__name__)


class _TickMixin:
    """Mixed into BePricesCoordinator."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _historical_spot_quarters: dict[datetime, list[float]]
    _historical_spots: dict[datetime, float]
    _last_error: str
    _peak_kw: float
    _peak_month: date | None
    _priced: SupplierSnapshot | None
    _rlp_blend: str
    _rlp_fetched_at: datetime | None
    _rlp_weights: RlpWeights
    _session: aiohttp.ClientSession
    _snapshot: SupplierSnapshot | None
    _spot_source: str
    _spp_fetched_at: datetime | None
    _spp_weights: SppWeights
    _supplier_tuple: tuple[str, str, str]
    entry: ConfigEntry
    _card_read_by_ocr: bool
    _month_cards_deferred: bool
    _profiles_deferred: bool
    _unloaded: bool
    _year_spots_deferred: bool
    hass: HomeAssistant

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins, which only
        # the concrete class composes. Declared for the type checker rather
        # than inherited, so each mixin is checked on its own while the
        # composition stays in one place, BePricesCoordinator's bases.
        async def _ensure_annual_volume(self) -> None: ...
        async def _ensure_historical_spots(
            self, start: date, end: date, api_key: str | None = None
        ) -> None: ...
        async def _ensure_rlp_weights(self, blend: str = "distinct") -> None: ...
        async def _ensure_spp_weights(self) -> None: ...
        async def _fetch_spot_prices(self) -> dict[datetime, float]: ...
        async def _maybe_refresh_snapshot(self) -> None: ...
        async def _save_persistent(self) -> None: ...
        async def _track_monthly_peak(self) -> None: ...
        async def async_request_refresh(self) -> "None": ...
        def _billed_peak_kw(self) -> float: ...
        def _fallback_spots(self) -> dict[datetime, float]: ...
        def _monthly_spot_mean(
            self, year: int, month: int, extra_spots: dict[datetime, float]
        ) -> float | None: ...
        def _peak_terms(self) -> list[float]: ...
        def _refresh_custom_snapshot(self) -> None: ...
        def _reresolve_snapshot(self) -> None: ...
        def _rlp_weighted_month_mean(
            self,
            year: int,
            month: int,
            extra_spots: dict[datetime, float],
            blend: str | None = None,
        ) -> float | None: ...
        def _snapshot_age_hours(self) -> float: ...
        def _spp_weighted_month_mean(
            self, year: int, month: int, extra_spots: dict[datetime, float]
        ) -> float | None: ...
        def _supply_ended(self) -> bool: ...
        def _sync_brussels_power_term_issue(self) -> None: ...
        def _sync_connection_fee_issue(self) -> None: ...
        def _sync_deprecated_supplier_issue(self) -> None: ...
        def _sync_direct_debit_unanswered_issue(self) -> None: ...
        def _sync_entsoe_auth_issue(self, active: bool, message: str = "") -> None: ...
        def _sync_exclusive_night_gap_issue(self) -> None: ...
        def _sync_extractor_issue(
            self,
            message: str | None,
            *,
            transient: bool = False,
            unreadable: bool = False,
        ) -> None: ...
        def _sync_impact_gap_issue(self) -> None: ...
        def _sync_prosumer_gap_issue(self) -> None: ...
        def _sync_register_pair_issue(self) -> None: ...
        def _sync_stale_issue(self, stale: bool) -> None: ...
        def _ytd_months(self, today: date) -> list[date]: ...

    async def _update_body(self) -> CoordinatorData:
        self._sync_deprecated_supplier_issue()
        # Before the snapshot, because _set_snapshot resolves the volume
        # tranche and the network ceiling against it; _reresolve_snapshot
        # below catches the card that was already in hand.
        await self._ensure_annual_volume()
        # Sibelga's power term, for a Brussels entry whose card prints only the
        # metering half of the fixed charge. One small PDF a year, cached for
        # the life of the process, and the resolver leaves the card alone
        # until it is there, so a slow or blocked Brugel costs nothing but the
        # gap the entry already had.
        #
        # Before the snapshot for the same reason the volume is: _resolve_snapshot
        # reads it out of the cache synchronously and cannot await, so fetching
        # afterwards left the card resolved without the term for the whole tick
        # that fetched it, 50,07 EUR/year short on a Brussels Bolt entry and
        # disagreeing with what the next tick would say.
        if self.entry.data.get(CONF_REGION) == REGION_BRUSSELS:
            await ensure_power_term(self._session, dt_util.now().year)
        if self.entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
            self._refresh_custom_snapshot()
        else:
            await self._maybe_refresh_snapshot()
        self._reresolve_snapshot()
        await self._track_monthly_peak()

        if self._snapshot is None:
            raise UpdateFailed(
                f"no supplier snapshot available: {self._last_error or 'cold start'}"
            )

        # Resolve the signing-cohort energy leg for a contract that names a
        # cohort month: a fixed / dynamic contract signed months ago bills at
        # the rate it locked in, not today's card. ``priced`` splices that leg
        # onto the current delivery-month DSO / tax / injection overlays and is
        # read at every energy-pricing site below. ``self._snapshot`` is never
        # mutated: it is persisted and seeds the shared (supplier, contract,
        # region) cache row that sibling entries on a different cohort month
        # adopt, so baking cohort energy into it would mis-price co-tenants;
        # ``priced`` is a per-tick local. A no-op (``priced is self._snapshot``)
        # only when the entry names neither a tariff card month nor a start
        # date: the card month decides and the start date is its fallback
        # (_tariff_card_month), so an entry carrying just the card month is on
        # a cohort like any other (issue #96).
        #
        # Not cached_only on the first tick, unlike the month cards the walk
        # below defers: the price table this tick publishes is built from the
        # signing card, so deferring it would serve a cohort entry the current
        # card's rate until the fill came back. _persisted_months keeps that
        # one row on disk, so only an entry's first setup fetches it.
        cohort = await _cohort_legs(
            self.hass,
            self._session,
            get_extractor(self.entry.data[CONF_SUPPLIER]),
            self.entry.data[CONF_CONTRACT],
            self.entry.data.get(CONF_REGION, ""),
            self.entry,
            self._snapshot,
        )
        # A contract that locks its offtake formula locks the feed-in one with
        # it, so the credit follows the signing card too (issue #85).
        priced = cohort.splice(self._snapshot)
        # The spot fetch and the historical walk decide their grid off the
        # leg that is priced, so it has to be on the coordinator before either
        # runs; the resolution below is read off the same leg.
        self._priced = priced

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
                if (self._last_error or "").startswith("ENTSO-E:"):
                    # The blip a previous tick recorded has cleared. Only that
                    # message: a probe match clears an extractor error, and
                    # on a probe-less supplier nothing else did, so the blip
                    # sat on the sensor for up to the 24 h TTL. An extractor
                    # failure kept from this tick is not the spot fetch's to
                    # erase.
                    self._last_error = ""
            except EntsoeAuthError as err:
                self._sync_entsoe_auth_issue(True, str(err))
                raise UpdateFailed(f"ENTSO-E auth: {err}") from err
            except EntsoeError as err:
                # A transient ENTSO-E outage must not blank the entry: the
                # last good day-ahead curve is still usable for breakdown
                # computation, whether this session fetched it or it came
                # back from the Store after a restart. Only fail when nothing
                # on hand actually covers today: _fallback_spots refuses a
                # curve from an earlier day rather than pricing today off it.
                self._last_error = f"ENTSO-E: {err}"
                _LOGGER.warning("ENTSO-E refresh failed; serving cached spots: %s", err)
                spot_prices = self._fallback_spots()
                if not spot_prices:
                    raise UpdateFailed(f"ENTSO-E: {err}") from err
        elif _injection_needs_spot(
            self._snapshot, self.entry
        ) or _injection_needs_month_spot(self._snapshot, self.entry):
            # Static-energy contract whose injection carries its own index:
            # Cociter Variable per hour, energie.be Vast on the month's
            # Belpex_SPP. The energy is priced without a spot, so
            # a spot failure (missing key, ENTSO-E outage) must NOT tear
            # the entry down: only the
            # injection credit goes unavailable. Fetch softly, falling
            # back to the cached curve, then to no injection price.
            try:
                spot_prices = await self._fetch_spot_prices()
            except (EntsoeError, EntsoeAuthError) as err:
                _LOGGER.debug(
                    "injection spot fetch failed (energy unaffected): %s", err
                )
                spot_prices = self._fallback_spots()

        # Refresh the Synergrid SPP profile when this entry's injection is
        # SPP-weighted: a card that indexes on Belpex_SPP, or a custom monthly
        # entry that opted in. Soft-fail. What a failure degrades TO differs -
        # the opt-in falls back to the plain mean, an SPP-indexed card must
        # not and keeps its printed indicative instead (see the bake below).
        # Asked of the priced leg, like every gate below: a signing cohort's
        # feed-in reads the index its own card names.
        spp_weighted = _spp_weighting_enabled(self.entry, priced)
        # Only worth the download when there are prices to weight. energie.be
        # Vast offers its ENTSO-E key as optional, so an entry that skipped it
        # reaches here with no spots at all and would otherwise pull 52 MB to
        # weight nothing, every restart.
        wants_spp = bool(spp_weighted and (spot_prices or self._historical_spots))
        # And the RLP profile when the ENERGY leg resolves against the
        # RLP-weighted month mean (Eneco Flex and Flex One, whose Belpex-RLP-M
        # weights each hour's Belpex by the residential load profile). Same
        # soft-fail: without the profile the plain mean stands in, which is
        # what every RLP card was priced on before.
        rlp_weighted = _energy_is_rlp_indexed(priced.energy)
        # A compensation entry wants the same profile for another reason: its
        # yearly net is settled by spreading the volume over the year on it.
        allocating = self.entry.data.get(CONF_SOLAR_REGIME) == SOLAR_REGIME_COMPENSATION
        wants_rlp = bool(
            (rlp_weighted and (spot_prices or self._historical_spots)) or allocating
        )
        blend = _rlp_blend_for(priced.energy)
        # FIRST tick only, same reason as the spots and the archived cards
        # above: this one runs inside config-entry setup. A cold profile is
        # 18 s of download and xlsb parse on a Raspberry Pi, and every
        # compensation entry wants one. The flag is cleared whether or not a
        # profile is wanted, so only the tick setup waits on can defer.
        first_tick = self._profiles_deferred
        self._profiles_deferred = False
        if first_tick and (wants_spp or wants_rlp):
            self.entry.async_create_background_task(
                self.hass,
                self._fill_profiles(wants_spp, wants_rlp, blend),
                f"{DOMAIN}_profiles_{self.entry.entry_id}",
            )
        else:
            if wants_spp:
                await self._ensure_spp_weights()
            if wants_rlp:
                await self._ensure_rlp_weights(blend)

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
            # An entry billing from its contract start date has no use for a
            # spot before it: nothing prices those hours, so fetching them is
            # the one thing issue #84 asked not to happen.
            spots_from = ytd_window_start(self.entry, today_local)
            if self._year_spots_deferred:
                # FIRST tick only, and it is the one the user is watching:
                # async_config_entry_first_refresh runs inside setup, which the
                # config flow's final step waits on, so a cold cache spent that
                # step fetching 35 week-chunks: minutes of a spinner on a
                # fresh install, and far longer while ENTSO-E was down.
                #
                # Fetch the current month here, because the monthly mean below
                # is computed for this month and nothing else can stand in for
                # it, then fill the rest of the year in the background. What
                # the deferral costs is the year-to-date's past hours on this
                # one tick: they bill their network and tax legs and forfeit
                # only the energy term, exactly as a cold cache already does,
                # and the refresh the fill requests puts them back.
                self._year_spots_deferred = False
                # And bounded, because scoping the window is not the same as
                # bounding the wait. Each week-chunk carries a 30 s client
                # timeout and a chunk that times out is logged and followed by
                # the next one, so a month is five of them plus the keyless
                # fallback: about 180 s against a supplier that hangs rather
                # than refuses, inside the same 300 s bootstrap budget issue
                # #88 was cancelled by. On the deadline this keeps whatever
                # chunks did land: they are merged per chunk, and the fill
                # below asks for the rest, which is the arrangement the year
                # already had.
                try:
                    async with asyncio.timeout(_FIRST_TICK_SPOT_BUDGET):
                        await self._ensure_historical_spots(
                            max(
                                spots_from,
                                date(today_local.year, today_local.month, 1),
                            ),
                            today_local,
                        )
                except TimeoutError:
                    _LOGGER.warning(
                        "Day-ahead history for %s was still fetching after %ds "
                        "during setup; continuing in the background",
                        self.entry.title,
                        int(_FIRST_TICK_SPOT_BUDGET),
                    )
                self.entry.async_create_background_task(
                    self.hass,
                    self._fill_year_spots(),
                    f"{DOMAIN}_year_spots_{self.entry.entry_id}",
                )
            else:
                await self._ensure_historical_spots(spots_from, today_local)

        # Two means, because the two legs can name two indices. Eneco's
        # energy is on Belpex-RLP-M, the RLP-weighted month mean, while its
        # injection is on Belpex-injectie, the plain one, so the energy leg
        # takes the weighted mean when the profile is loaded and the feed-in
        # bake below keeps the plain mean whatever the energy did.
        plain_mean: float | None = None
        energy_mean: float | None = None
        if _injection_on_month_mean(priced) or isinstance(
            priced.energy, SpotMonthlyRates
        ):
            # Also for a card whose ENERGY needs no mean but whose feed-in
            # credit is indexed on one: without it the bake below would resolve
            # against None and wipe the credit instead of resolving it. Asked
            # of the EFFECTIVE leg, and of the injection's own flags, so a
            # month-indexed credit is resolved whatever the energy is priced
            # on: a dynamic energy leg fetches its own spots and used to
            # take the credit out of this question with them.
            now_local = dt_util.now()
            plain_mean = self._monthly_spot_mean(
                now_local.year, now_local.month, spot_prices
            )
            energy_mean = plain_mean
            if rlp_weighted:
                weighted = self._rlp_weighted_month_mean(
                    now_local.year, now_local.month, spot_prices
                )
                if weighted is not None:
                    energy_mean = weighted

        try:
            hourly = self._build_hourly(priced, spot_prices, energy_mean)
        except KeyError as err:
            # The fresh snapshot does not contain the user's configured
            # DSO: typically a regex drift on a new card. Surface a
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
        injection_snapshot = priced
        if _injection_on_month_mean(priced) and not _injection_hourly_on_cohort(
            self._snapshot, self.entry
        ):
            inj_mean = plain_mean
            spp_only = _injection_is_spp_indexed(self._snapshot)
            # A card that prints an indicative has something to fall back to
            # when the mean is missing; a formula-only leg does not. That, not
            # which index the formula names, is what decides whether the bake
            # can be skipped.
            inj = self._snapshot.injection
            has_indicative = inj is not None and inj.current is not None
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
            if inj_mean is None and has_indicative:
                # Leave the snapshot alone so the card's printed indicative is
                # credited. This used to test spp_only, on the belief that an
                # SPP-indexed card was the only shape carrying an indicative.
                # It is not: Eneco Power Fix and Flex are month_indexed and
                # print one too, so they fell through to the bake and had
                # current, factor and base all wiped, which drops the feed-in
                # credit off the sensor entirely rather than degrading it to
                # the printed figure.
                #
                # A formula-only leg still bakes to None deliberately. Leaving
                # factor/base standing with no ``current`` is precisely the
                # shape _injection_is_spot_formula reads as "price this per
                # hour", turning a flat monthly credit into an hourly one at
                # whatever the current slot costs.
                pass
            else:
                injection_snapshot = _bake_monthly_injection(priced, inj_mean)
        injection_price = _compute_injection_price(
            injection_snapshot, self.entry, spot_prices
        )
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
        )
        # The same bill over the running month. A second pass rather than an
        # accumulator inside the first: the walk has four branches and four
        # fees-floor exits, and a month total threaded through all eight is the
        # shape that drifts. A month is an eighth of a mid-year window, and the
        # month cards and spots the first pass resolved are all cached, so what
        # it costs is one short recorder read and the pricing loop over ~30
        # days.
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
            window_start_override=month_window_start(self.entry),
        )
        if cached_months_only:
            self._month_cards_deferred = False
            self.entry.async_create_background_task(
                self.hass,
                self._fill_month_cards(),
                f"{DOMAIN}_month_cards_{self.entry.entry_id}",
            )
        projection_breakdown: dict[str, Any] = {}
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
            # The card the welcome credit is read off, the same row the
            # year-to-date walk just resolved, so a cache hit.
            signing=await signing_month_snapshot(
                self.hass,
                self._session,
                get_extractor(self.entry.data[CONF_SUPPLIER]),
                self.entry.data[CONF_CONTRACT],
                self.entry.data.get(CONF_REGION, ""),
                self.entry,
                self._snapshot,
                cached_only=cached_months_only,
            ),
            breakdown=projection_breakdown,
        )

        await self._save_persistent()

        age = self._snapshot_age_hours()
        # A supplier that has left keeps its final card for good; the
        # deprecation card already says so, and a second alarm on top of it
        # asked the user to fix a staleness nothing can fix.
        stale = age > SNAPSHOT_STALE_DAYS * 24 and not self._supply_ended()
        self._sync_stale_issue(stale)
        self._sync_exclusive_night_gap_issue()
        self._sync_impact_gap_issue()
        self._sync_connection_fee_issue()
        self._sync_prosumer_gap_issue()
        self._sync_register_pair_issue()
        self._sync_direct_debit_unanswered_issue()
        self._sync_brussels_power_term_issue()

        # Compute static peak/offpeak breakdowns for the Energy Dashboard.
        # These are the constant all-in rates for day and night, independent
        # of the current time. None for dynamic/TOU contracts or impact tariff.
        dso_key = self.entry.data.get(CONF_DSO, "")
        region = self.entry.data.get(CONF_REGION, "")
        dso_mode = self.entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        try:
            static_peak = static_breakdown(priced, dso_key, region, "peak", dso_mode)
            static_offpeak = static_breakdown(
                priced, dso_key, region, "offpeak", dso_mode
            )
        except KeyError:
            # DSO not in snapshot, which happens for custom entries or incomplete
            # cards. The sensors will be unavailable, which is correct.
            static_peak = None
            static_offpeak = None

        # Static injection (feed-in) rates for bi-hourly meters. None when the
        # contract has a single injection rate, is spot-indexed, or has TOU slots.
        # Trevion Vast and similar cards print separate day/night injection rates.
        inj = priced.injection
        static_inj_peak: float | None = None
        static_inj_offpeak: float | None = None
        # Both or neither. A card that printed one of the pair would
        # otherwise publish a day rate and leave the night one unavailable,
        # which reads as a broken sensor rather than as a card that does not
        # carry the split.
        if (
            inj is not None
            and inj.bi_hourly
            and inj.peak is not None
            and inj.offpeak is not None
        ):
            static_inj_peak = inj.peak
            static_inj_offpeak = inj.offpeak

        return CoordinatorData(
            hourly=hourly,
            resolution=(
                RESOLUTION_QUARTER
                if _energy_is_quarter_hourly(priced.energy)
                else RESOLUTION_HOURLY
            ),
            snapshot_publication=self._snapshot.publication_label,
            signing_card=cohort.card,
            snapshot_age_hours=age,
            snapshot_stale=stale,
            snapshot_valid_until=self._snapshot.valid_until,
            last_error=self._last_error,
            card_read_by_ocr=self._card_read_by_ocr,
            spot_source=self._spot_source,
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
            current_month_cost_eur=month_cost,
            ytd_diagnostics=ytd_breakdown or None,
            projected_year_cost_eur=projected_year_cost,
            projection_diagnostics=projection_breakdown or None,
            static_peak_price=static_peak,
            static_offpeak_price=static_offpeak,
            static_injection_peak=static_inj_peak,
            static_injection_offpeak=static_inj_offpeak,
        )

    async def _fill_year_spots(self) -> None:
        """Fetch the rest of the year's spots, off the setup path.

        Scheduled by the first tick, which fetched only the current month so
        that setup, and with it the config flow's final step, did not wait
        on 35 week-chunks. Runs as an entry-tied background task, so unloading
        the entry cancels it and the user can walk away from a fresh install
        mid-backfill without leaving a fetch running.

        Failures need no handling here: _ensure_historical_spots logs what it
        could not fetch and leaves those hours absent, which every reader
        already treats as "no data".

        The refresh is what puts the year-to-date's past hours back into the
        sensor, since the tick that scheduled this one priced them without
        their energy term, so it is only asked for when the walk actually
        found something. A restart runs this too, and there the persisted
        cache already covers the year: nothing is fetched, nothing changed,
        and an extra full tick per entry per restart would buy nothing.
        """
        today = dt_util.now().date()
        before = (len(self._historical_spots), len(self._historical_spot_quarters))
        await self._ensure_historical_spots(ytd_window_start(self.entry, today), today)
        after = (len(self._historical_spots), len(self._historical_spot_quarters))
        if self._unloaded or after == before:
            return
        await self.async_request_refresh()

    async def _fill_profiles(self, spp: bool, rlp: bool, blend: str) -> None:
        """Fetch the Synergrid profiles, off the setup path.

        Scheduled by the first tick, which priced without them. Both are
        national curves fetched at most once per process (see
        ``_shared_profile``), so N entries scheduling this at the same moment
        cost one download, not N.

        Failures need no handling here: both ensures soft-fail, keep whatever
        is held and back off, and the caller then prices the plain arithmetic
        mean, which is what every RLP-indexed card was billed on before the
        profile existed, and what a compensation entry falls back to when its
        allocation cannot be weighted.

        The refresh is asked for only when a profile actually landed. A restart
        restores both from the Store, so the usual case fetches nothing and an
        extra full tick per entry would buy nothing.
        """
        before = (self._spp_fetched_at, self._rlp_fetched_at, self._rlp_blend)
        if spp:
            await self._ensure_spp_weights()
        if rlp:
            await self._ensure_rlp_weights(blend)
        after = (self._spp_fetched_at, self._rlp_fetched_at, self._rlp_blend)
        if self._unloaded or after == before:
            return
        await self.async_request_refresh()

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
        out: keeping the array consistent with the live scalar and the YTD
        credit. Slots with no spot (tomorrow before the day-ahead publishes)
        are dropped, exactly like the consumption tomorrow array.
        """
        if self.entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
            return {}
        inj = injection_snapshot.injection
        meter = self.entry.data.get(CONF_METER, METER_MONO)
        if inj is None or not _injection_varies_intraday(inj, energy, meter=meter):
            return {}
        region = self.entry.data.get(CONF_REGION, REGION_FLANDERS)
        out: dict[datetime, float] = {}
        for utc in grid_keys:
            rate = _injection_price_for_slot(
                inj,
                energy,
                spot_prices.get(utc),
                dt_util.as_local(utc),
                meter=meter,
                region=region,
            )
            if rate is not None:
                out[utc] = rate
        return out


# How long the FIRST tick may spend filling the running month's day-ahead
# history before it gives up and leaves the rest to the background fill. Sized
# off what it is protecting rather than off the fetch: config-entry setup runs
# inside a bootstrap stage whose 300 s budget is shared with every other
# integration, and the card fetch, the today/tomorrow curve and this all come
# out of it. A month of chunks against a healthy ENTSO-E is a few seconds, so
# this only ever bites on a source that hangs, which is exactly the case where
# waiting buys nothing: the fill retries it off the setup path a moment later.
_FIRST_TICK_SPOT_BUDGET = 45.0
