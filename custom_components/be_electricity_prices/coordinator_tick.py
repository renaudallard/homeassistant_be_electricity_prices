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

Fetches the card, resolves the signing cohort, then hands the day's prices to
``coordinator_prices`` and the bills to ``coordinator_costs``, and assembles
the CoordinatorData every sensor reads from what they return. What only the
tick decides stays here: the repairs it raises and the static band rates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import (
    CONF_CARD_ARCHIVE,
    DEFAULT_CARD_ARCHIVE,
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_EV_HOME_CHARGING_RATE,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    DEFAULT_EV_HOME_CHARGING_RATE,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    RESOLUTION_HOURLY,
    RESOLUTION_QUARTER,
    SUPPLIER_CUSTOM,
)
from .coordinator_data import (
    CoordinatorData,
    month_window_reset,
    ytd_window_reset,
)
from .providers import (
    SupplierSnapshot,
    get as get_extractor,
)
from .providers._rates import EnergyRates
from collections.abc import Iterable
from .pricing import (
    PriceBreakdown,
    static_breakdown,
    yearly_fixed_fee_for_meter,
)
from .snapshot_store import SNAPSHOT_STALE_DAYS, cached_month_card
from datetime import date, datetime
from homeassistant.helpers.update_coordinator import UpdateFailed
from .injection import (
    _compute_injection_price,
    _static_injection_bands,
)
from .cohort import (
    _cohort_legs,
    _tariff_card_month,
)
from .fees import _compute_capacity, _compute_prosumer
from .contract_periods import (
    PricedPeriods,
    previous_rows,
)
from .spot_stats import (
    _energy_is_quarter_hourly,
)
from homeassistant.util import dt as dt_util
from .brugel import ensure_power_term
from .vat_rates import ensure_vat_rates
from .creg_ev import (
    ensure_rates as ensure_ev_rates,
    quarter_start as ev_quarter_start,
    rate_for as ev_rate_for,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import aiohttp
import logging
from .coordinator_costs import TickCosts

_LOGGER = logging.getLogger(__name__)


class _TickMixin:
    """Mixed into BePricesCoordinator."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _last_error: str
    _peak_kw: float
    _peak_month: date | None
    _previous_priced: PricedPeriods | None
    _priced: SupplierSnapshot | None
    _session: aiohttp.ClientSession
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _spot_source: str
    entry: ConfigEntry
    _card_read_by_ocr: bool
    hass: HomeAssistant

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator and the sibling mixins. Declared
        # for the type checker rather than inherited, so each mixin is checked
        # on its own and BePricesCoordinator's bases say how they compose. The
        # one mixin composed anywhere else is the profiles mixin, which the
        # spots mixin extends.
        async def _ensure_annual_volume(self) -> None: ...
        async def _maybe_refresh_snapshot(self) -> None: ...
        async def _save_persistent(self) -> None: ...
        async def _track_monthly_peak(self) -> None: ...
        def _billed_peak_kw(self) -> float: ...
        def _peak_terms(self) -> list[float]: ...
        def _refresh_custom_snapshot(self) -> None: ...
        def _reresolve_snapshot(self) -> None: ...
        def _snapshot_age_hours(self) -> float: ...
        def _supply_ended(self) -> bool: ...
        def _sync_brussels_power_term_issue(self) -> None: ...
        def _sync_connection_fee_issue(self) -> None: ...
        def _sync_deprecated_supplier_issue(self) -> None: ...
        def _sync_withdrawn_contract_issue(self) -> None: ...
        def _sync_direct_debit_unanswered_issue(
            self, signing: SupplierSnapshot | None
        ) -> None: ...
        def _sync_exclusive_night_gap_issue(self) -> None: ...
        def _sync_impact_gap_issue(self) -> None: ...
        def _sync_prosumer_gap_issue(self) -> None: ...
        def _sync_compensation_kva_issue(self) -> None: ...
        def _sync_register_pair_issue(self) -> None: ...
        def _sync_stale_issue(self, stale: bool) -> None: ...
        def _build_hourly(
            self,
            snap: SupplierSnapshot,
            spot_prices: dict[datetime, float],
            monthly_mean: float | None = None,
        ) -> dict[datetime, PriceBreakdown]: ...
        def _build_injection_hourly(
            self,
            injection_snapshot: SupplierSnapshot,
            energy: EnergyRates,
            spot_prices: dict[datetime, float],
            grid_keys: Iterable[datetime],
        ) -> dict[datetime, float]: ...
        async def _tick_costs(
            self,
            priced: SupplierSnapshot,
            injection_snapshot: SupplierSnapshot,
            energy_mean: float | None,
            spp_weighted: bool,
            rlp_weighted: bool,
            allocating: bool,
            billed_peak: float,
        ) -> TickCosts: ...
        def _tick_injection_leg(
            self,
            priced: SupplierSnapshot,
            spot_prices: dict[datetime, float],
            plain_mean: float | None,
            spp_weighted: bool,
        ) -> SupplierSnapshot: ...
        def _tick_month_means(
            self,
            priced: SupplierSnapshot,
            spot_prices: dict[datetime, float],
            rlp_weighted: bool,
        ) -> tuple[float | None, float | None]: ...
        async def _tick_profiles(
            self, priced: SupplierSnapshot, spot_prices: dict[datetime, float]
        ) -> tuple[bool, bool, bool]: ...
        async def _tick_spot_prices(
            self, priced: SupplierSnapshot
        ) -> dict[datetime, float]: ...

    async def _update_body(self) -> CoordinatorData:
        self._sync_deprecated_supplier_issue()
        self._sync_withdrawn_contract_issue()
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
        # The month's VAT rate, read before the card is resolved like the term
        # above, and from the card archive, so only where the entry reads it.
        if self.entry.data.get(CONF_CARD_ARCHIVE, DEFAULT_CARD_ARCHIVE):
            await ensure_vat_rates(self._session)
        if self.entry.data.get(CONF_SUPPLIER) == SUPPLIER_CUSTOM:
            self._refresh_custom_snapshot()
        else:
            await self._maybe_refresh_snapshot()
        self._reresolve_snapshot()
        await self._track_monthly_peak()
        # Only for an entry that ticked the box, which is the one thing that
        # lets it contact creg.be. Independent of the card: a failure only
        # leaves its own sensor as is. One date for the fetch, the rate and its
        # quarter, so the record can never name one quarter and carry another's
        # rate.
        ev_rate: float | None = None
        ev_quarter: date | None = None
        if self.entry.data.get(
            CONF_EV_HOME_CHARGING_RATE, DEFAULT_EV_HOME_CHARGING_RATE
        ):
            ev_today = dt_util.now().date()
            await ensure_ev_rates(self.hass, self._session, ev_today)
            ev_rate = ev_rate_for(self.entry.data.get(CONF_REGION, ""), ev_today)
            ev_quarter = ev_quarter_start(ev_today)

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
        # one row on disk, so a setup fetches it the first time and then only
        # when the row was not kept: after a schema bump drops the stored rows,
        # while the card archive still holds it under an older schema or the
        # extractor flags it provisional (a provisional row is never written),
        # and when the month had no card, whose marker is trusted for a day.
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

        spot_prices = await self._tick_spot_prices(priced)
        spp_weighted, rlp_weighted, allocating = await self._tick_profiles(
            priced, spot_prices
        )
        plain_mean, energy_mean = self._tick_month_means(
            priced, spot_prices, rlp_weighted
        )

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
        injection_snapshot = self._tick_injection_leg(
            priced, spot_prices, plain_mean, spp_weighted
        )
        injection_price = _compute_injection_price(
            injection_snapshot, self.entry, spot_prices
        )
        costs = await self._tick_costs(
            priced,
            injection_snapshot,
            energy_mean,
            spp_weighted,
            rlp_weighted,
            allocating,
            billed_peak,
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
        self._sync_compensation_kva_issue()
        self._sync_register_pair_issue()
        # The signing card as parsed: the resolved one has the unanswered
        # question settled as no, which clears a credit granted only to a
        # direct-debit payer. None while the month's card is not in yet, when
        # the credit is withheld anyway.
        signed_on = self._snapshot_raw
        month = _tariff_card_month(self.entry)
        if costs.signing is not self._snapshot and month is not None:
            signed_on = cached_month_card(
                self.hass,
                self.entry.data[CONF_SUPPLIER],
                self.entry.data[CONF_CONTRACT],
                self.entry.data.get(CONF_REGION, ""),
                month,
            )
        self._sync_direct_debit_unanswered_issue(signed_on)
        self._sync_brussels_power_term_issue()

        # Compute static peak/offpeak breakdowns for the Energy Dashboard.
        # These are the constant all-in rates for day and night, independent
        # of the current time. None for dynamic/TOU contracts or impact tariff.
        # A monthly leg is constant for the month, so it takes the mean its
        # price table was built on; without it every card priced on the
        # month's index left both sensors unavailable.
        dso_key = self.entry.data.get(CONF_DSO, "")
        region = self.entry.data.get(CONF_REGION, "")
        dso_mode = self.entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        try:
            static_peak = static_breakdown(
                priced, dso_key, region, "peak", dso_mode, energy_mean
            )
            static_offpeak = static_breakdown(
                priced, dso_key, region, "offpeak", dso_mode, energy_mean
            )
        except KeyError:
            # DSO not in snapshot, which happens for custom entries or incomplete
            # cards. The sensors will be unavailable, which is correct.
            static_peak = None
            static_offpeak = None

        # Static injection (feed-in) rates for bi-hourly meters. None when the
        # contract has a single injection rate, is spot-indexed, or has TOU slots.
        # Trevion Vast and similar cards print separate day/night injection rates.
        static_inj_peak, static_inj_offpeak = _static_injection_bands(priced.injection)

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
            ev_home_charging_rate_eur_per_kwh=ev_rate,
            ev_home_charging_quarter_start=ev_quarter,
            current_year_cost_eur=costs.current_year_cost,
            previous_contracts=previous_rows(self._previous_priced, costs.periods),
            current_month_cost_eur=costs.month_cost,
            current_year_cost_reset=ytd_window_reset(self.entry, costs.window_now),
            current_month_cost_reset=month_window_reset(self.entry, costs.window_now),
            ytd_diagnostics=costs.ytd_breakdown or None,
            projected_year_cost_eur=costs.projected_year_cost,
            projection_diagnostics=costs.projection_breakdown or None,
            projected_year_consumption_kwh=costs.projected_consumption,
            projected_year_injection_kwh=costs.projected_injection,
            volume_projection_diagnostics=costs.volume_breakdown,
            rolling_year_consumption_kwh=costs.rolling_consumption,
            rolling_year_injection_kwh=costs.rolling_injection,
            rolling_volume_diagnostics=costs.rolling_breakdown,
            year_end_cost_eur=costs.year_end_cost,
            year_end_diagnostics=costs.year_end_breakdown,
            static_peak_price=static_peak,
            static_offpeak_price=static_offpeak,
            static_injection_peak=static_inj_peak,
            static_injection_offpeak=static_inj_offpeak,
        )
