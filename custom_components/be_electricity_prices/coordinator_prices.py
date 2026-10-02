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

"""The prices half of an update tick.

The day-ahead curve the tick prices on, the profiles and the year's spots the
month means are weighted on, those means, the today / tomorrow price tables and
the leg the feed-in is credited on. The background fills that the first tick
defers live here too: setup runs on Home Assistant's stage-2 budget, so the
tick answers from the cache and finishes the slow walks afterwards.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    REGION_FLANDERS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .providers import (
    DynamicRates,
    SpotMonthlyRates,
    SupplierSnapshot,
)
from .providers._rates import EnergyRates
from .api import EntsoeAuthError, EntsoeError
from collections.abc import Iterable
from .pricing import (
    PriceBreakdown,
    compute_breakdown,
)
from datetime import UTC, date, datetime, timedelta
from homeassistant.helpers.update_coordinator import UpdateFailed
from .injection import (
    _bake_monthly_injection,
    _injection_bakes_to_month_mean,
    _injection_needs_month_spot,
    _injection_needs_spot,
    _injection_price_for_slot,
)
from .cohort import (
    ytd_window_start,
)
from .contract_periods import (
    periods_need_rlp,
    periods_need_spots,
    previous_periods,
)
from .spot_stats import (
    _energy_is_rlp_indexed,
    _injection_is_spp_indexed,
    _injection_on_month_mean,
    _rlp_blend_for,
    _spp_weighting_enabled,
)
import asyncio
from homeassistant.util import dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import logging

_LOGGER = logging.getLogger(__name__)

# How long the FIRST tick may spend filling the running month's day-ahead
# history before it gives up and leaves the rest to the background fill. Sized
# off what it is protecting rather than off the fetch: config-entry setup runs
# inside a bootstrap stage whose 300 s budget is shared with every other
# integration, and the card fetch, the today/tomorrow curve and this all come
# out of it. A month of chunks against a healthy ENTSO-E is a few seconds, so
# this only ever bites on a source that hangs, which is exactly the case where
# waiting buys nothing: the fill retries it off the setup path a moment later.
_FIRST_TICK_SPOT_BUDGET = 45.0


class _PricesMixin:
    """Mixed into BePricesCoordinator."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _historical_spot_quarters: dict[datetime, list[float]]
    _historical_spots: dict[datetime, float]
    _last_error: str
    _rlp_blend: str
    _rlp_fetched_at: datetime | None
    _snapshot: SupplierSnapshot | None
    _spp_fetched_at: datetime | None
    entry: ConfigEntry
    _profiles_deferred: bool
    _unloaded: bool
    _year_spots_deferred: bool
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
        async def _ensure_spp_weights(self) -> None: ...
        async def _fetch_spot_prices(self) -> dict[datetime, float]: ...
        async def async_request_refresh(self) -> "None": ...
        def _fallback_spots(self) -> dict[datetime, float]: ...
        def _monthly_spot_mean(
            self, year: int, month: int, extra_spots: dict[datetime, float]
        ) -> float | None: ...
        def _rlp_weighted_month_mean(
            self,
            year: int,
            month: int,
            extra_spots: dict[datetime, float],
            blend: str | None = None,
        ) -> float | None: ...
        def _spp_weighted_month_mean(
            self, year: int, month: int, extra_spots: dict[datetime, float]
        ) -> float | None: ...
        def _sync_entsoe_auth_issue(self, active: bool, message: str = "") -> None: ...
        def _sync_extractor_issue(
            self,
            message: str | None,
            *,
            transient: bool = False,
            unreadable: bool = False,
            missing: bool = False,
        ) -> None: ...

    async def _tick_spot_prices(
        self, priced: SupplierSnapshot
    ) -> dict[datetime, float]:
        """This tick's day-ahead curve, for a leg that is priced off it."""
        # _update_body raised before calling this when there was no card.
        assert self._snapshot is not None
        spot_prices: dict[datetime, float] = {}
        # Auth + extractor issue clear paths run OUTSIDE the
        # DynamicRates branch so that an existing Repairs entry
        # auto-resolves regardless of how the snapshot got refreshed
        # this tick (sibling-cache adoption, self-fresh probe match,
        # or a fresh fetch). Reaching this point with no live
        # ``_last_error`` means the extractor produced a clean
        # snapshot; the cycle-7 entsoe_auth_failed clear is
        # unconditional because that issue can only ever be set by
        # one of the two spot fetches below, each on its own failure.
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
                if isinstance(err, EntsoeAuthError):
                    # A rejected key is not an outage: the credit stays out
                    # until the key is replaced, and the key step promised a
                    # notice when that happens.
                    self._sync_entsoe_auth_issue(True, str(err))
                spot_prices = self._fallback_spots()
        return spot_prices

    async def _tick_profiles(
        self, priced: SupplierSnapshot, spot_prices: dict[datetime, float]
    ) -> tuple[bool, bool, bool]:
        """Load what the month means are weighted on, and the year's spots.

        Returns whether the feed-in is SPP-weighted, whether the energy is
        RLP-weighted, and whether the entry allocates on the load profile.
        """
        # _update_body raised before calling this when there was no card.
        assert self._snapshot is not None
        # The contracts the household held earlier this year, when it recorded
        # a switch. Their days are priced off the tick (_price_previous), but
        # the gates below decide on the load profile and the year's spots
        # first, and an old dynamic contract needs those whatever this one does.
        gate_day = dt_util.now().date()
        gate_periods = previous_periods(
            self.entry.data, ytd_window_start(self.entry, gate_day), gate_day
        )

        # Refresh the Synergrid SPP profile when this entry's injection is
        # SPP-weighted: a card that indexes on Belpex_SPP, or a custom monthly
        # entry that opted in. Soft-fail. What a failure degrades TO differs -
        # the opt-in falls back to the plain mean, an SPP-indexed card must
        # not and keeps its printed indicative instead (see the bake below).
        # Asked of the priced leg: a signing cohort's feed-in reads the index
        # its own card names. Not every gate here does: the spot fetches and
        # the bake's SPP-only and indicative tests read today's card, and the
        # per-hour test reads today's energy kind with the priced leg, which it
        # must (see _injection_hourly_on_cohort).
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
            (rlp_weighted and (spot_prices or self._historical_spots))
            or allocating
            or periods_need_rlp(gate_periods)
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
            or periods_need_spots(gate_periods)
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
        return spp_weighted, rlp_weighted, allocating

    def _tick_month_means(
        self,
        priced: SupplierSnapshot,
        spot_prices: dict[datetime, float],
        rlp_weighted: bool,
    ) -> tuple[float | None, float | None]:
        """The running month's plain mean, and the one the energy leg bills on."""
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
        return plain_mean, energy_mean

    def _tick_injection_leg(
        self,
        priced: SupplierSnapshot,
        spot_prices: dict[datetime, float],
        plain_mean: float | None,
        spp_weighted: bool,
    ) -> SupplierSnapshot:
        """The leg the feed-in is credited on this tick."""
        # _update_body raised before calling this when there was no card.
        assert self._snapshot is not None
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
        if _injection_bakes_to_month_mean(priced, self._snapshot, self.entry):
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
        return injection_snapshot

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

        Empty off the injection regime and on a card that prints no feed-in.
        Every other shape gets a row per slot, a flat or monthly-indexed one
        too: it repeats the scalar, which is what the contract pays, and the
        price arrays do the same for a fixed card, so a chart drawn off them
        works whatever the contract (issue #108). ``injection_snapshot`` is
        the possibly mean-baked snapshot and ``energy`` the effective (cohort)
        energy, the same pair the scalar reads, so each slot is the figure the
        sensor shows for it. A slot the card cannot price (a per-slot spot
        formula before the day-ahead publishes, or without a key) is dropped
        rather than credited at zero, exactly like the consumption array.
        """
        if self.entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
            return {}
        inj = injection_snapshot.injection
        if inj is None:
            return {}
        meter = self.entry.data.get(CONF_METER, METER_MONO)
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
