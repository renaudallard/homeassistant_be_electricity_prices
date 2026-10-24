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

"""Turning the dialog's answers into one priced household.

The sweep prices every candidate against a single picture of the home: its
meter, its regime, its volumes and the spot curves those volumes are settled
on. Building that picture is most of the work and none of the ranking, so it
sits apart from the engine that loops over candidates.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .cohort import _parse_iso_date, signing_month_snapshot, ytd_window_start
from .compare_inputs import (
    _credit_index_for,
    _credit_year,
    _detached_spot_view,
    _effective_regime,
    _HouseholdQuote,
    _label_for_contract,
    _label_for_supplier,
    _months_billed,
    _needs_month_mean,
    _quote_entry,
    _target_dso_mode,
)
from .compare_quote import (
    _annual_volume,
    _annual_welcome_credit,
    _covers_a_year,
    _full_year_kwh,
    _read_total_kwh,
)
from .compare_table import IndexGaps, _solar_note
from .compare_weighting import (
    _consumption_weighted_spot,
    _register_weights,
    _tou_weighted_per_kwh,
)
from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_KEYLESS_SPOTS,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    CONF_WHATIF_CONSUMPTION_KWH,
    CONF_WHATIF_INJECTION_KWH,
    DSO_MODE_BI_HORAIRE,
    MEASURED_FULL_YEAR_DAYS,
    METER_DYNAMIC,
    METER_MONO,
    METER_SENSOR_KEYS,
    SMART_METER_CONTRACT_KINDS,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_NONE,
    SPOT_PRICED_CONTRACT_KINDS,
    SUPPLIER_CUSTOM,
)
from .contract_periods import billed_from, previous_periods
from .day_night import day_start_for
from .flow_contracts import _contract_has_spot_injection, _contract_kind
from .meter_daily import _measured_kwh
from .meter_hourly import (
    _hour_of_day_shares,
    _measured_hour_weights,
    _measured_hourly,
)
from .pricing import MeterType
from .providers import get as get_extractor
from .providers import settlement_answer
from .providers.base import SupplierSnapshot
from .spot_stats import (
    _energy_is_rlp_indexed,
    _injection_is_spp_indexed,
    _rlp_blend_for,
)

_LOGGER = logging.getLogger(__name__)


def _meters_rewired(entry: ConfigEntry, today: date) -> bool:
    """Whether an earlier contract in this year's window read other meter
    sensors than the entry does now.

    The own row prices each earlier contract on the sensors kept with it, and
    a quoted contract can only be replayed on the entry's own. When those were
    rewired at the switch they hold nothing for the earlier contract's days,
    so the alternative billed them at its fees alone and the page printed a
    saving of most of those days' bill. The year-to-date of a quoted side is
    left out instead.
    """
    data = entry.data
    return any(
        (period.data.get(key) or None) != (data.get(key) or None)
        for period in previous_periods(data, ytd_window_start(entry, today), today)
        for key in METER_SENSOR_KEYS
    )


class _HouseholdMixin:
    """Mixed into _SweepEngine."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _compare: dict[str, Any]
    config_entry: ConfigEntry
    hass: HomeAssistant

    def _target_side(
        self,
        hh: Any,
        supplier: str,
        contract: str,
        quarter_hourly: bool,
        snap: SupplierSnapshot,
    ) -> tuple[MeterType, str, ConfigEntry, SupplierSnapshot]:
        """The meter, DSO mode, entry and resolved card a candidate is priced on.

        The three target-side adjustments the one-to-one page makes, which a
        ranking needs for exactly the same reasons, and which its annual row
        and its year-to-date row have to make alike: left out of one of them,
        the column is a second pricing path that quietly disagrees with the
        first (the year-to-date pass billed a Tarif Impact card on the
        household's bi-horaire columns and an ex-VAT card's running month off
        the raw card).

        METER: the household's own meter is the right default, because a
        ranking has no step to ask a what-if and the physical meter is a
        fact. But where the target's KIND forces one, that is not an override
        at all, it is the only meter the product is sold on, and quoting a
        dynamic card on a mono meter routes distribution through the
        bi-horaire split while the supplier bills energy by slot.

        DSO MODE: a Tarif Impact card carries three CWaPE band rates and no
        mono/bi structure, so the band schedule prices its energy whatever the
        household is on while the network leg and the Walloon terme fixe
        follow the mode. Quoting it on the household's own mode bands the
        energy and then bills the network off the standard columns.

        The card is resolved per entry (VAT, excise band, volume tranche,
        settlement grid) the way every other quote path resolves it.
        """
        from .snapshot_resolve import _resolve_snapshot

        kind = _contract_kind(supplier, contract, quarter_hourly=quarter_hourly)
        meter: MeterType = (
            METER_DYNAMIC if kind in SMART_METER_CONTRACT_KINDS else hh.current_meter
        )
        dso_mode = _target_dso_mode(kind, hh.dso_mode)
        target_entry = _quote_entry(
            self.config_entry,
            hh.regime,
            dso_mode,
            quarter_hourly=quarter_hourly,
            meter=meter,
        )
        return meter, dso_mode, target_entry, _resolve_snapshot(target_entry, snap)

    async def _resolve_household(
        self,
        coord: Any,
        *,
        candidates: Sequence[tuple[str, str, bool]],
    ) -> _HouseholdQuote:
        """Everything a quote needs that does not depend on which contract is
        being quoted.

        Resolved once, whatever the page above it is doing. The one-to-one
        compare passes a single candidate; a ranking passes the whole cell and
        pays for this exactly once rather than once per row, which is what
        makes a sweep affordable at all: the meter reads, the recorder walk,
        the measured hour shapes and the day-ahead window are all O(1) in the
        number of contracts being compared.

        ``candidates`` is read for one decision only: whether any row will
        need day-ahead spots, because that window is fetched here and shared.

        Only the household's own meter is resolved here, on
        ``current_meter``: the target's is the quote's, so a mono household's
        own bill is never quoted at the target's rates.
        """
        current = self.config_entry.data
        region = current[CONF_REGION]
        day_start = day_start_for(current)
        dso = current[CONF_DSO]
        # Comparison may override the meter type for static contracts;
        # falls back to the current entry's setting.
        # The comparison may override the meter for the TARGET only (a
        # dynamic/TOU target forces METER_DYNAMIC). The user's current side
        # must keep its real meter, else a mono user's current bill gets
        # quoted at bi-horaire / dynamic rates and biases the decision.
        current_meter = current.get(CONF_METER, METER_MONO)
        dso_mode = current.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
        # The quantity the capacity tariff is charged on, not this month's
        # reading: _billed_peak_kw applies the regulated floor per month and
        # means the rolling twelve, so the comparison quotes the same kW the
        # live sensor bills. Flooring _peak_kw here instead would quote a
        # seasonal household its winter peak against the year.
        peak_kw = coord._billed_peak_kw()
        # The what-if regime, if the compare_solar step ran, and a proxy
        # entry carrying it. Everything downstream that prices money takes
        # the proxy; the real entry stays for runtime_data and for reading
        # the household's own meters, which are facts, not hypotheses.
        stored_regime = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
        regime = _effective_regime(current, self._compare)
        # The household's own side: its meter is a fact, not a hypothesis.
        quote_entry = _quote_entry(self.config_entry, regime, meter=None)
        overridden = quote_entry is not self.config_entry

        now_utc = dt_util.utcnow()
        today_local = dt_util.now().date()
        # The same window current_year_cost accumulates over: 1 January, or the
        # contract start date on an entry that bills from it. The archive path
        # below runs _compute_current_year_cost, which resolves this itself off
        # entry.data, so reading it here is what keeps the simple model, and
        # the kWh figure printed beside both: telling the same story as the
        # sensor on the page rather than a January one. After a recorded switch
        # it opens where the earliest contract began billing, which the entry's
        # own settings no longer say, so the quoted side, the kWh read and every
        # ranking candidate cover the same days as the own row.
        ytd_from = billed_from(
            current, ytd_window_start(self.config_entry, today_local), today_local
        )
        # 364, not 365: energy_meters._recorder_rows anchors end_dt on the next
        # local midnight, so the window is end-inclusive and today counts. The
        # old arithmetic read 366 buckets under a "365 days" label.
        year_ago = today_local - timedelta(days=MEASURED_FULL_YEAR_DAYS - 1)
        # Inclusive of today: leap years -> 366. Computed off 1 January whatever
        # the window is: it is the denominator of an ANNUAL fee, so it stays a
        # year even when only part of one has been billed.
        days_in_year = (
            date(today_local.year + 1, 1, 1) - date(today_local.year, 1, 1)
        ).days
        days_elapsed = (today_local - ytd_from).days + 1
        fee_proration = days_elapsed / days_in_year
        # The prosumer fee and the Flanders capacity tariff are both billed
        # per-month in the live sensor and backfill (each month's charge
        # prorated by its OWN days), not by the uniform days_in_year fraction,
        # so mirror that: sum each month's billed days over its own length.
        # _ytd_prosumer and _ytd_capacity sum exactly this, which is why one
        # number serves both, and summing it rather than counting whole
        # months is what carries a window that starts mid-month.
        month_proration = _months_billed(ytd_from, today_local)
        spot_dict: dict[datetime, float] = (
            dict(coord._spot_cache) if coord._spot_cache else {}
        )
        # Cross-kind comparisons (static <-> spot-priced) need spot data
        # for the spot-priced side. The user's coordinator already has
        # spots when they're on one; otherwise borrow the api key
        # they just typed in compare_api_key (or the one already on
        # their entry) and fetch the day-ahead window for today.
        # A spot-monthly side counts: without a spot its energy leg cannot be
        # priced at all and the quote renders a bare "-" for a contract the
        # user explicitly asked about.
        current_kind = _contract_kind(
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            quarter_hourly=settlement_answer(self.config_entry.data),
        )
        # A spot-indexed-injection side (Cociter Variable) prices its feed-in
        # credit off the hourly day-ahead even though its energy kind is
        # "variable", so it needs spots just like a dynamic side. Asked across
        # every candidate rather than one, because the day-ahead window is
        # fetched once and shared by every row that reads it. For the
        # one-to-one page ``candidates`` is a list of one and the answer is
        # exactly what it was; for a ranking this is why the key is collected
        # once for the whole sweep rather than per row.
        compare_spot_injection = regime == SOLAR_REGIME_INJECTION and (
            _contract_has_spot_injection(current[CONF_SUPPLIER], current[CONF_CONTRACT])
            or any(
                _contract_has_spot_injection(supplier, contract)
                for supplier, contract, _q in candidates
            )
        )
        need_spot = (
            current_kind in SPOT_PRICED_CONTRACT_KINDS
            or any(
                _contract_kind(supplier, contract, quarter_hourly=q)
                in SPOT_PRICED_CONTRACT_KINDS
                for supplier, contract, q in candidates
            )
            or compare_spot_injection
        )
        # The key typed on the page or the entry's, and the keyless setting
        # ticked on either: every day-ahead read below goes through them.
        spot_key = self._compare.get(CONF_API_KEY) or current.get(CONF_API_KEY)
        spot_keyless = bool(
            self._compare.get(CONF_KEYLESS_SPOTS) or current.get(CONF_KEYLESS_SPOTS)
        )
        if need_spot and not spot_dict and (spot_key or spot_keyless):
            from .api import fetch_day_ahead_or_fallback

            try:
                # Today's local day, as the live fetch and the auction cut it:
                # a window from UTC midnight started at 01:00 or 02:00 local
                # and returned a day short of its first hours.
                local_midnight = dt_util.start_of_local_day(today_local)
                spot_dict, _source = await fetch_day_ahead_or_fallback(
                    spot_key,
                    async_get_clientsession(self.hass),
                    local_midnight.astimezone(UTC),
                    dt_util.start_of_local_day(
                        today_local + timedelta(days=1)
                    ).astimezone(UTC),
                )
            except Exception:  # degrade to '-' for the dynamic side
                pass
        # For the ANNUAL estimate a dynamic contract's all-in is
        # factor*spot + base, linear in spot, so the time-averaged yearly
        # bill equals the breakdown at the MEAN spot over the fetched
        # day-ahead window. Use that rather than an instantaneous spot so
        # the estimate doesn't reflect whichever minute the dialog opened
        # (Belgian day-ahead swings from negative to >0.30 EUR/kWh intraday).
        avg_spot = sum(spot_dict.values()) / len(spot_dict) if spot_dict else None
        # A SPOT-MONTHLY leg is different: it does not average out over a year,
        # it bills one flat rate per delivery month, and that month's mean is a
        # number the coordinator already computes. Pricing it off a single
        # day-ahead window instead makes the quote swing with the day the
        # dialog happened to open, and prints a "per kWh now" for the user's
        # OWN entry that contradicts their current_price sensor. Resolved
        # lazily and once: the month backfill is only worth its fetch when a
        # side actually bills on it, and the target's snapshot is not retrieved
        # until further down.
        month_spot_resolved: list[float | None] = []
        # The copy _month_spot fetched the month into. The weighted means are
        # taken on it too: the coordinator never sees what it fetched, so on
        # an entry holding no spots of its own they averaged today alone.
        month_view: list[Any] = []

        async def _month_spot() -> float | None:
            if month_spot_resolved:
                return month_spot_resolved[0]
            value = avg_spot
            # Merged rather than isolated: a month mean is the same number
            # whoever asks, so whatever the entry has already cached is valid
            # input, and it is what still answers when the fetch fails.
            view = _detached_spot_view(coord, isolate=False)
            month_view.append(view)
            # A failed fetch degrades to the day-ahead mean.
            with contextlib.suppress(Exception):
                await view._ensure_historical_spots(
                    today_local.replace(day=1),
                    today_local,
                    spot_key,
                    keyless=spot_keyless,
                )
            resolved = view._monthly_spot_mean(
                today_local.year, today_local.month, spot_dict
            )
            if resolved is not None:
                value = resolved
            month_spot_resolved.append(value)
            return value

        spp_spot_resolved: list[float | None] = []

        async def _spp_month_spot() -> float | None:
            """The SPP-weighted month mean an SPP-indexed credit bills on.

            One index further than _month_spot: a card that indexes its
            feed-in on Belpex_SPP may not be resolved against any other mean,
            so when no Synergrid solar profile is held this answers None and
            the caller keeps the card's printed indicative rather than
            quoting the consumption index. Reads the entry's profile, else
            the installation's (``quoted_spp_weights``); only the nightly
            ranking downloads one, never the dialog, and a row priced without
            it says so (``index_gaps``).
            """
            if spp_spot_resolved:
                return spp_spot_resolved[0]
            await _month_spot()  # fetches the month into month_view
            spp_spot_resolved.append(
                month_view[0]._spp_weighted_month_mean(
                    today_local.year, today_local.month, spot_dict
                )
            )
            return spp_spot_resolved[0]

        async def _credit_month_spot_for(
            snapshot: SupplierSnapshot | None,
            *,
            entry: ConfigEntry,
            own: bool,
            raw: SupplierSnapshot | None = None,
        ) -> float | None:
            """The delivery month's mean this side's FEED-IN settles on.

            Two indices, and the card names which. A formula on Belpex_SPP
            takes the solar-weighted mean; one on the plain arithmetic mean
            takes that. Either way the answer is a month, never the two-day
            day-ahead window the energy leg is quoted at, and never the
            printed indicative, which is that formula on the PREVIOUS month.

            The plain branch is what the page was missing: it resolved the
            SPP cards and let every other month-indexed card (Eneco Fix, Flex
            and Flex One, Engie's and Luminus' EPEXDAM cards, TotalEnergies
            Impact) fall through to the live helper, which answers the
            printed figure unless the snapshot has been baked and only the
            coordinator bakes. The gap is ``factor`` times one month of index
            drift, about 25 EUR a year on an Eneco card at 10 EUR/MWh and
            3000 kWh exported.

            Which index applies is ``_credit_index_for``; this only turns
            that answer into a number, because resolving either mean needs the
            fetched month the closure holds.

            ``entry`` is the one the side's credit is priced under, and it has
            no default because the answer turns on its regime: the SPP gate
            and the per-hour cohort test both need the injection regime on
            the entry they are handed. A what-if into the injection tariff
            prices the own side on the quote proxy, and asked of the real
            entry, still on compensation, an SPP-indexed credit read as plain
            and was resolved on the arithmetic mean: 167 to 245 EUR a year
            too much credit on the own side alone. Only the as-configured
            baseline is asked of the real entry.
            """
            index = _credit_index_for(entry, snapshot, own=own, raw=raw)
            if index == "spp":
                return await _spp_month_spot()
            if index == "plain":
                return await _month_spot()
            return None

        async def _spot_for(snapshot: SupplierSnapshot | None) -> float | None:
            """The spot this side's energy shape actually bills on.

            A per-slot leg takes the mean weighted by when the household draws,
            because its bill is the sum over slots of kWh times that slot's
            rate, and consumption is evening-heavy while the day-ahead curve
            troughs at midday. A month-mean leg takes its delivery month's own
            index, which is a published number and not a shape question.
            """
            if (
                snapshot is not None
                and _needs_month_mean(snapshot)
                and getattr(snapshot.energy, "quarter_indexed", False)
            ):
                # Bolt bills the quarter's index, so the quarter is fetched,
                # into a copy, and resolved the way the live price is.
                view = _detached_spot_view(coord, isolate=False)
                first = today_local.replace(
                    month=(today_local.month - 1) // 3 * 3 + 1, day=1
                )
                # A failed fetch degrades to the month mean below.
                with contextlib.suppress(Exception):
                    await view._ensure_historical_spots(
                        first, today_local, spot_key, keyless=spot_keyless
                    )
                quarter: float | None = view._quarter_index(
                    snapshot.energy, today_local.year, today_local.month, spot_dict
                )
                if quarter is not None:
                    return quarter
            if snapshot is not None and _needs_month_mean(snapshot):
                month = await _month_spot()
                if _energy_is_rlp_indexed(snapshot.energy):
                    # Eneco's index is the RLP-weighted mean; the plain one
                    # is the fallback while no profile is held. The entry's
                    # profiles, else the installation's, laid on the view
                    # (``_detached_spot_view``); never downloaded here.
                    #
                    # THIS side's blend, not the entry's. The three reductions
                    # are three different indices, not one at three
                    # resolutions: measured on the August 2026 Belgian
                    # day-ahead curve they stood at 133,44 / 134,93 / 135,66
                    # EUR/MWh, so pricing an Energy Knights card on Eneco's
                    # curve moves it 2,2 EUR/MWh against what it bills, which
                    # is enough to reorder neighbouring rows on a page whose
                    # whole job is the order.
                    weighted: float | None = month_view[0]._rlp_weighted_month_mean(
                        today_local.year,
                        today_local.month,
                        spot_dict,
                        blend=_rlp_blend_for(snapshot.energy),
                    )
                    if weighted is not None:
                        return weighted
                return month
            return _consumption_weighted_spot(spot_dict, hour_weights) or avg_spot

        def _index_gaps(snapshot: SupplierSnapshot | None) -> IndexGaps:
            """The index this side's card names that the page could not
            resolve, by cause (``IndexGaps``), so the page and the ranking say
            so rather than ranking the row as though its own index had been
            read.

            Without a day-ahead source no month index is resolved, whatever
            curve is held: a feed-in on one is credited at the figure its card
            prints, and an energy leg on one is not priced at all, so the cause
            is the source and never the profile. With one, a weighted index
            whose curve is not held is priced without it: the plain mean for
            the energy, the printed figure for the feed-in. A feed-in only
            counts where it is credited at a price on export the household
            measured, and where the card prints a figure to fall back on;
            without one the credit is left out, which ``feed_in_uncredited``
            says.
            """
            if snapshot is None:
                return IndexGaps()
            energy = snapshot.energy
            inj = snapshot.injection
            credited = (
                regime == SOLAR_REGIME_INJECTION
                and rolling_inj_kwh > 0
                and getattr(inj, "current", None) is not None
            )
            if not (spot_key or spot_keyless):
                return IndexGaps(
                    day_ahead=credited
                    and (
                        bool(getattr(inj, "month_indexed", False))
                        or _injection_is_spp_indexed(snapshot)
                    )
                )
            return IndexGaps(
                load=_needs_month_mean(snapshot)
                and _energy_is_rlp_indexed(energy)
                and not coord.quoted_rlp_weights(_rlp_blend_for(energy)),
                solar=credited
                and _injection_is_spp_indexed(snapshot)
                and not coord.quoted_spp_weights(),
            )

        # Measured consumption / injection from the user's kWh sensors.
        # Injection is only relevant when a solar regime is configured; for
        # the "none" regime it stays 0 even if a sensor is wired. Read it
        # when EITHER regime has solar, not just the quoted one: a what-if
        # into "none" still prices the baseline leg on the entry's own
        # regime, and zeroing the volume there would un-net a compensation
        # baseline (or drop an injection credit) and quote the user's own
        # contract as costing more than it does.
        ytd_kwh = await _read_total_kwh(
            self.hass, self.config_entry, ytd_from, today_local
        )
        rolling_inj_kwh = 0.0
        ytd_inj_kwh = 0.0
        inj_full_year = False
        if regime != SOLAR_REGIME_NONE or stored_regime != SOLAR_REGIME_NONE:
            # Short of a full year, injection stays on the raw window sum.
            # Putting it through _annual_volume looked symmetric and is wrong in
            # both bands: below the floor it discards a real feed-in
            # measurement, and _solar_note reads that zero as "no injection
            # sensor wired" while the same page prints the YTD injected kWh;
            # above it, PV is far more seasonal than consumption, so a 365/days
            # factor on a spring window can over-credit enough to drive the
            # compensation net to its zero clamp and quote both sides at fees
            # only. Annualising a partial window needs a production profile,
            # not a day count.
            #
            # A window that counts as a full year is another matter: it is
            # scaled across the few days the recorder missed, as consumption
            # is and as the rolling-year cost sensor scales this leg. Left raw,
            # 14 missing days netted a scaled year of draw against 96% of a
            # year of feed-in, and the own row stood 54 EUR above the sensor.
            measured_inj = await _measured_kwh(
                self.hass, self.config_entry, year_ago, today_local, side="injection"
            )
            y = await _read_total_kwh(
                self.hass, self.config_entry, ytd_from, today_local, side="injection"
            )
            year_inj = _full_year_kwh(measured_inj)
            rolling_inj_kwh = (
                year_inj if year_inj is not None else max(measured_inj.kwh, 0.0)
            )
            inj_full_year = _covers_a_year(measured_inj.days_with_data)
            ytd_inj_kwh = y or 0.0
        annual = await _annual_volume(
            self.hass, self.config_entry, year_ago, today_local
        )
        annual_kwh = annual.kwh
        consumption_source = annual.source
        # Under the netting regime the two legs are SUBTRACTED, so they have to
        # be on one basis. Annualising consumption while injection stays on the
        # raw window nets a whole year of draw against a fraction of a year of
        # feed-in: measured on a seasonal prosumer with 300 days of history that
        # quoted 434 EUR against a true 186, and the page printed its own
        # contradiction ("annual_kwh 3706" beside "netted, consumption -= 2625").
        # Scaling the feed-in leg to match is not the answer either, since PV is
        # seasonal enough that a summer window over-credits into the zero clamp.
        # So when the feed-in side cannot be annualised honestly, neither side
        # is, and the quote is the measured window on both legs, which is what
        # this page did before the volume resolver existed.
        if (
            regime == SOLAR_REGIME_COMPENSATION
            or stored_regime == SOLAR_REGIME_COMPENSATION
        ) and not inj_full_year:
            raw = await _read_total_kwh(
                self.hass, self.config_entry, year_ago, today_local
            )
            if raw is not None:
                annual_kwh = raw
                consumption_source = (
                    f"{annual.days_with_data} days measured, netted against the same "
                    "window's injection rather than annualised"
                )
        # Volumes typed on the what-if step replace the measured pair. The
        # step only offers them when no injection sensor is wired, which is
        # the wiring whose consumption register may already be netted, so
        # the typed figures are the only gross ones available.
        typed_cons = self._compare.get(CONF_WHATIF_CONSUMPTION_KWH)
        typed_inj = self._compare.get(CONF_WHATIF_INJECTION_KWH)
        volumes_typed = typed_cons is not None and typed_inj is not None
        if typed_cons is not None and typed_inj is not None:
            annual_kwh = float(typed_cons)
            rolling_inj_kwh = float(typed_inj)
            consumption_source = "entered for the what-if"

        placeholders: dict[str, str] = {
            "current_supplier": _label_for_supplier(current[CONF_SUPPLIER]),
            "current_contract": _label_for_contract(
                current[CONF_SUPPLIER], current[CONF_CONTRACT]
            ),
            # The compare side is looked up leniently because the ranking
            # resolves the same household context with no single target: it
            # has a whole cell of them and never reads this dict. Keyed access
            # here would make the sweep fail on a placeholder it discards.
            "compare_supplier": _label_for_supplier(
                self._compare.get(CONF_SUPPLIER, "")
            ),
            "compare_contract": _label_for_contract(
                self._compare.get(CONF_SUPPLIER, ""),
                self._compare.get(CONF_CONTRACT, ""),
            ),
            "current_per_kwh": "-",
            "compare_per_kwh": "-",
            "current_annual": "-",
            "compare_annual": "-",
            "delta_annual": "-",
            "current_ytd": "-",
            "compare_ytd": "-",
            "delta_ytd": "-",
            "annual_kwh": f"{annual_kwh:.0f}",
            # Typed volumes describe a full year, not the elapsed part of
            # this one, and the year-to-date legs replay meter history that
            # was recorded under the configured regime, so both are left
            # blank rather than mixing the two.
            "ytd_from": ytd_from.strftime("%d/%m/%Y"),
            # Not on rewired meters either: the entry's sensors hold none of
            # an earlier contract's days, so the window's kWh would be short.
            "ytd_kwh": (
                "-"
                if volumes_typed
                or ytd_kwh is None
                or _meters_rewired(self.config_entry, today_local)
                else f"{ytd_kwh:.0f}"
            ),
            "annual_chart": "",
            "ytd_chart": "",
            "ytd_injection_kwh": (
                f"{ytd_inj_kwh:.0f}"
                if regime != SOLAR_REGIME_NONE and not volumes_typed
                else "-"
            ),
            "solar_note": _solar_note(regime, rolling_inj_kwh),
            "consumption_source": consumption_source,
            "card_note": "",
            "error": "",
        }

        # Price the user's CURRENT side off the legs the live sensors bill, not
        # the raw card. A contract with a signing start date is billed at the
        # rates it locked in, energy and feed-in both, which _cohort_legs
        # resolves and the coordinator splices on every tick. Reading
        # coord._snapshot here compared the alternative against today's
        # published card instead, so the quoted delta was wrong for exactly the
        # users the start-date feature exists for. _cohort_legs overrides
        # nothing for a contract that is not the entry's own, so it can never
        # touch the other side.
        current_snapshot = coord._snapshot
        # Kept before the cohort splice, for two readers. The feed-in credit
        # asks the raw card's energy kind whether the CREDIT rides a month
        # mean, since the splice can put a month-priced leg on a card whose
        # feed-in varies per hour. The own row's year to date walks it as the
        # current_year_cost sensor does: that walk resolves the cohort per
        # month itself, and a month before the cohort's first bills on its own
        # card, or on this one where no archive holds it, never on a leg
        # spliced on here.
        raw_snapshot = coord._snapshot
        # The card the entry is actually configured on, which the baseline
        # leg prices. Only the expert custom supplier builds its snapshot
        # out of entry.data, so only there can the what-if card and the
        # configured one differ at all.
        baseline_snapshot = current_snapshot
        if overridden and current[CONF_SUPPLIER] == SUPPLIER_CUSTOM:
            # That supplier has no card to fetch, and its injection block is
            # dropped unless entry.data says injection, so a what-if has to
            # rebuild it from the proxy or the custom side credits nothing
            # whatever the user picks. Resolve it the way the coordinator
            # does: build_snapshot returns the card ex-VAT with the entered
            # rate on taxes, and nothing else grosses the fixed fees.
            from .providers.custom import build_snapshot
            from .snapshot_resolve import _resolve_snapshot

            # A failure keeps the configured snapshot.
            with contextlib.suppress(Exception):
                current_snapshot = _resolve_snapshot(
                    quote_entry, build_snapshot(quote_entry.data, region, dso)
                )
        if current_snapshot is not None:
            from .cohort import _cohort_legs

            legs = await _cohort_legs(
                self.hass,
                async_get_clientsession(self.hass),
                get_extractor(current[CONF_SUPPLIER]),
                current[CONF_CONTRACT],
                region,
                quote_entry,
                current_snapshot,
            )
            spliced = legs.splice(current_snapshot)
            # The splice carries the signed yearly fee, which the fee legs
            # read, so the baseline has to follow it whenever the two are the
            # same card.
            if baseline_snapshot is current_snapshot:
                baseline_snapshot = spliced
            current_snapshot = spliced

        # The household's own hour-of-day consumption shape, so a time-of-use
        # card is quoted on the kWh it actually bills rather than on clock
        # hours. Reads only entry.data, so the _QuoteEntry proxy is safe here.
        hour_weights = await _measured_hour_weights(
            self.hass, self.config_entry, year_ago, today_local
        )
        # And the export shape, for a per-slot feed-in credit. Averaging those
        # slots by duration credits the overnight block, which is a third of
        # the clock and produces nothing. Kept per hour too: a static card's
        # spot-indexed credit weighs each hour of the year by its own export.
        export = await _measured_hourly(
            self.hass, self.config_entry, year_ago, today_local, side="injection"
        )
        inj_hour_weights = _hour_of_day_shares(export)
        current_per_kwh: float | None = None
        current_export_per_kwh: float | None = None

        async def _export_rate_for(
            snapshot: SupplierSnapshot | None, meter_type: Any, mode: str
        ) -> float | None:
            """All-in EUR/kWh weighted by when the panels EXPORT.

            Only a compensation meter needs it: it nets against the rate in
            force at the time, so the exported side has to be priced on its
            own shape rather than on the consumption one. Every other regime
            never reads it, so it is not worth the second pass. Asked when
            either the what-if or the stored regime nets: the note's figure
            as configured prices a compensation entry on its own regime, and
            without this rate it netted the annual totals instead.

            ``mode`` is the side's own DSO tariff mode, since a Tarif Impact
            target is billed on a configuration the household need not be on.
            """
            if SOLAR_REGIME_COMPENSATION not in (regime, stored_regime):
                return None
            if snapshot is None or inj_hour_weights is None:
                return None
            return _tou_weighted_per_kwh(
                snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                await _spot_for(snapshot),
                meter_type,
                mode,
                inj_hour_weights,
                day_start=day_start,
            )

        signing_snapshot = current_snapshot
        own_welcome_credit = 0.0
        if current_snapshot is not None:
            current_per_kwh = _tou_weighted_per_kwh(
                current_snapshot,
                dso,
                region,
                dt_util.as_local(now_utc),
                await _spot_for(current_snapshot),
                current_meter,
                dso_mode,
                hour_weights,
                day_start=day_start,
            )
            current_export_per_kwh = await _export_rate_for(
                current_snapshot, current_meter, dso_mode
            )
            # What the household's own first year still has to give over the
            # coming one, read off the card it signed: the live tick resolves
            # the same row every hour, so this is a cache hit.
            signing_snapshot = await signing_month_snapshot(
                self.hass,
                async_get_clientsession(self.hass),
                get_extractor(current[CONF_SUPPLIER]),
                current[CONF_CONTRACT],
                region,
                quote_entry,
                current_snapshot,
            )
            own_welcome_credit = _annual_welcome_credit(
                current_snapshot,
                signing_snapshot,
                _parse_iso_date(current.get(CONF_CONTRACT_START_DATE)),
                dt_util.as_local(now_utc),
                dso,
                region,
                await _spot_for(current_snapshot),
                current_meter,
                dso_mode,
                hour_weights,
                annual_kwh,
                rolling_inj_kwh,
                regime=regime,
                day_start=day_start,
            )
        return _HouseholdQuote(
            region=region,
            day_start=day_start,
            dso=dso,
            current_meter=current_meter,
            dso_mode=dso_mode,
            peak_kw=peak_kw,
            stored_regime=stored_regime,
            regime=regime,
            quote_entry=quote_entry,
            overridden=overridden,
            now_utc=now_utc,
            today_local=today_local,
            ytd_from=ytd_from,
            fee_proration=fee_proration,
            month_proration=month_proration,
            spot_dict=spot_dict,
            current_kind=current_kind,
            avg_spot=avg_spot,
            compare_spot_injection=compare_spot_injection,
            ytd_kwh=ytd_kwh,
            rolling_inj_kwh=rolling_inj_kwh,
            ytd_inj_kwh=ytd_inj_kwh,
            annual_kwh=annual_kwh,
            volumes_typed=volumes_typed,
            placeholders=placeholders,
            current_snapshot=current_snapshot,
            raw_snapshot=raw_snapshot,
            parsed_snapshot=coord._snapshot_raw,
            baseline_snapshot=baseline_snapshot,
            hour_weights=hour_weights,
            inj_hour_weights=inj_hour_weights,
            register_weights=(
                _register_weights(
                    region,
                    hour_weights,
                    meter=current_meter,
                    dso_mode=dso_mode,
                    day_start=day_start,
                ),
                _register_weights(
                    region,
                    inj_hour_weights,
                    meter=current_meter,
                    dso_mode=dso_mode,
                    day_start=day_start,
                ),
            ),
            current_per_kwh=current_per_kwh,
            current_export_per_kwh=current_export_per_kwh,
            signing_snapshot=signing_snapshot,
            own_welcome_credit=own_welcome_credit,
            spot_for=_spot_for,
            index_gaps=_index_gaps,
            credit_month_spot_for=_credit_month_spot_for,
            export_rate_for=_export_rate_for,
            credit_year=_credit_year(
                getattr(coord, "_historical_spots", None) or {}, export, today_local
            ),
        )

    async def _ocr_fallback(
        self,
        supplier: str,
        contract: str,
        region: str,
        fetched: Any,
    ) -> Any:
        """The archive's OCR reading, for a card no parser can read.

        Only for a card that downloaded fine and carries no text layer, which
        is what ``CardNotReadableError`` means and why this tests the exception
        rather than its message. Every other failure is a supplier that is
        unreachable or broken, and a stale reading would be the wrong answer
        for those.

        The same helper the live tick uses, so the two pages agree about which
        month is served and both honour the entry's card-archive box.
        """
        from .providers.base import CardNotReadableError
        from .snapshot_stand_ins import card_for_unreadable_month

        if not isinstance(fetched.error, CardNotReadableError):
            return None
        try:
            return await card_for_unreadable_month(
                async_get_clientsession(self.hass),
                supplier,
                contract,
                region,
                dt_util.now().date(),
                self.config_entry,
            )
        except Exception as err:  # a blip on the archive is not this row's problem
            _LOGGER.debug(
                "card archive read failed for %s/%s: %s", supplier, contract, err
            )
            return None
