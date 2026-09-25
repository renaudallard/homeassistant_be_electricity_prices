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

"""The year-to-date cost engine.

Split out of coordinator.py. Walks the year month by month, pricing each with
that month's own archived card, and sums the energy, the standing charges, the
capacity tariff, the prosumer forfait and the feed-in credit into the running
bill the current_year_cost sensor publishes.

The same figure is built by two other paths: backfill.py per hour and the
options flow's compare quote, so a change here that is not mirrored there
shows up as a seam, not an exception."""

from __future__ import annotations

import logging

from datetime import date, datetime, time
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import aiohttp

from .cohort import (
    _cohort_energy_leg,
    _effective_snapshot_for_month,
    _month_snapshot_cache,
    _parse_iso_date,
    signing_month_snapshot,
    ytd_window_start,
)
from .const import (
    CONF_CONTRACT,
    CONF_CONTRACT_START_DATE,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DSO_MODE_BI_HORAIRE,
    DSO_MODE_IMPACT,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .energy_meters import (
    _resolve_daily_kwh,
)
from .fees import (
    _welcome_credit_eur,
    first_year_net_kwh,
    grants_a_welcome_credit,
    window_energy_rate,
)
from .injection import (
    _historical_injection_rate,
)
from .pricing import (
    MeterType,
    renewables_eur_per_kwh,
    static_breakdown,
)
from .providers.base import (
    SupplierExtractor,
    SupplierSnapshot,
)
from .providers._rates import (
    DynamicRates,
    ImpactRates,
    SpotMonthlyRates,
    TimeOfUseRates,
)
from .snapshot_resolve import entry_annual_injection_kwh, entry_annual_kwh
from .spot_stats import (
    _NetAllocation,
    _bucket_by_local_month,
    _day_register_weights,
    _injection_is_spp_indexed,
    _injection_on_month_mean,
    _spp_weighting_enabled,
    _spp_injection_spot,
)
from .synergrid import (
    RlpWeights,
    SppWeights,
)
from .ytd_energy import (
    _ytd_hourly_energy,
    _ytd_spot_injection_credit,
)
from .ytd_legs import (
    _days_through,
    _ytd_capacity,
    _ytd_prosumer,
    _ytd_static_fees,
)


_LOGGER = logging.getLogger(__name__)


# Which hour stands for each meter register when a card prints one feed-in rate
# per register. The walk is per DAY and the register split is the DSO's day and
# night schedule, so the rate is asked for one hour inside each block: 10:00 is
# always a weekday day register and 03:00 always night. On a weekend or a
# holiday the whole day is the night register, both questions answer the night
# rate, and the day kWh a meter did not record there is zero anyway.
_DAY_REGISTER_HOUR = 10
_NIGHT_REGISTER_HOUR = 3


async def _compute_current_year_cost(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    *,
    contract_override: str | None = None,
    meter_override: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    spot_quarters: dict[datetime, list[float]] | None = None,
    spp_weights: SppWeights | None = None,
    rlp_weights: RlpWeights | None = None,
    rlp_index_weights: RlpWeights | None = None,
    breakdown: dict[str, float] | None = None,
    billed_peak_kw: float = 0.0,
    cached_only: bool = False,
    window_start_override: date | None = None,
    window_end: date | None = None,
) -> float | None:
    """Time-correct yearly bill from HA recorder + per-month tariff cards.

    For every day from Jan 1 of the current local year up to today,
    pull that day's kWh from the recorder and multiply by the tariff
    of the month the day belongs to (archived snapshot when the
    supplier exposes one, else the current snapshot as a proxy).
    Per-day kWh × per-day tariff handles tariff transitions inside a
    month (e.g. the supplier rotates a monthly card mid-month) without
    re-querying the recorder, and matches what the user reads on a
    smart meter day by day.

    Math per day, after looking up the snapshot for that day's month:

      regime=none, mono : (d_cons + n_cons) * single
      regime=none, bi   : d_cons * peak + n_cons * offpeak
      regime=injection,
        mono : (d_cons + n_cons) * single - (d_inj + n_inj) * inj_m
      regime=injection,
        bi   : d_cons * peak + n_cons * offpeak
               - (d_inj + n_inj) * inj_m
      regime=compensation, mono :
               (d_cons + n_cons - d_inj - n_inj) * single
      regime=compensation, bi :
               (d_cons - d_inj) * peak + (n_cons - n_inj) * offpeak

    Compensation netting happens once over the YTD total at the end, per
    register and clamped at zero, matching how the Walloon annual meter
    readout actually settles: a day of over-injection can offset a later
    day of higher consumption. The net is then spread over the elapsed days
    by the RLP profile (``_NetAllocation``), which is how the supplier
    allocates the DSO's single yearly figure, so the rows above give each
    day's register nets and the price is settled after the walk.

    Plus fees: the supplier yearly fixed fee and the Flemish energy
    fund are summed per archived month using each month's snapshot
    (so a supplier indexation that lands mid-year is honoured for the
    months it applies to), pro-rated by ``days_in_month_in_ytd /
    days_in_year`` so the YTD total still grows uniformly across the
    calendar year. The Walloon prosumer fee follows the same per-month
    walk against each month's DSO overlay. The running bill grows day
    by day instead of jumping to the full annual on Jan 1.

    ``inj_m`` is each month's snapshot's ``injection.current`` (the
    printed monthly indicative).

    **Time-of-Use contracts** (Engie Empower Flextime, Luminus
    SmartFlex) take a per-hour path: the recorder's hourly kWh deltas
    are billed against ``compute_breakdown`` at each local hour, so
    the energy component picks the supplier's TOU slot rate while the
    network component still follows the user's DSO mode. Reads either
    ``CONF_CONSUMPTION_KWH`` (single totals) or the day+night register
    pair via the recorder's hourly statistics; partial register
    wiring is rejected so a missing band can't silently undercount.

    **Dynamic contracts** (Cociter Dynamique, Eneco Power Dynamic,
    OCTA+ Dynamic, etc.) replay historical hourly ENTSO-E spots from
    the coordinator's persistent cache (filled lazily by
    ``_ensure_historical_spots``). Each past kWh is then billed at
    its actual ``factor*spot+base`` rate via ``compute_breakdown``,
    same code path as the live current_price. Hours with no spot in
    the cache (cold start before the backfill, or a gap left by an
    ENTSO-E publication outage) still bill their network and tax legs
    and forfeit only the energy term, so an entirely empty cache lands
    on the fees floor plus the grid and tax cost of every metered kWh
    rather than on fees alone.

    Returns ``None`` only when there is no meter input wired at all
    AND no snapshot to show fees against. In every other case the
    function returns a number, falling back to the fees-only floor
    rather than exposing ``unknown`` to the user.

    The whole year is recomputed from scratch on every coordinator tick
    by design: today's cost grows each hour, and prior days are NOT safely
    immutable between ticks (a late ENTSO-E spot fill or a backfill
    correction changes a past day's rate). Memoizing prior-day totals
    would risk serving a stale YTD; the full replay is O(hours-in-year)
    pure arithmetic (~100 ms by December), which is negligible at the
    hourly update cadence, so keep it simple.

    ``breakdown`` is an optional diagnostic out-dict. When passed (only the
    live coordinator does; the compare / backfill callers leave it ``None``),
    the static per-day branch records the YTD and today kWh totals, the
    pre-clamp raw energy term and the fees floor into it, so the
    current_year_cost sensor can surface them as attributes. This piggybacks
    on the walk already happening here rather than reading the recorder twice.
    It stays empty for the dynamic / spot-monthly / TOU (hourly) branches,
    which don't produce daily kWh totals.

    ``cached_only`` prices every past month off whatever archived cards the
    process already holds, fetching none. Only the coordinator's FIRST tick
    passes it, because that tick runs inside config-entry setup and one PDF
    per elapsed month does not fit in Home Assistant's bootstrap budget (see
    :func:`snapshot_months._snapshot_for_month`). What it costs is the months
    the cache is missing: they bill against the current card instead of their
    own, exactly as a supplier with no archive does all year, until the
    warm-up refresh lands.

    ``spp_weights`` is ignored for a card that does not name Belpex_SPP, and
    that gate is here rather than left to the callers. ``_spp_injection_spot``
    applies the weighting to whatever profile it is handed: it cannot tell a
    deliberate opt-in from a caller that passed the profile it had, and the
    two means are different indices rather than one being coarser, so an
    unmeant profile re-prices a plain month-indexed credit onto the
    solar-weighted mean. Five call sites now pass this argument, and asking
    each to remember is the shape of mistake that got the argument dropped in
    the first place.

    ``rlp_weights`` carries two roles that agree on the entry's own bill and
    part company on the compare page: the load shape a compensation net is
    spread over and a bi-hourly day is split by, which is the HOUSEHOLD's, and
    the index an RLP-indexed month leg resolves against, which is the CARD's.
    ``rlp_index_weights`` names the second where they differ, so a foreign card
    is priced on the blend its own card names; it defaults to ``rlp_weights``,
    which is what the entry's own walk wants.

    ``window_end`` closes the window on an earlier day, the last one billed, for
    a contract the household left during the year (``contract_periods.py``). The
    window is then priced exactly as a running one, from the recorder alone:
    no live top-up for today and no fees past that day. ``today`` stays the
    calendar's, since whether a month is still running is not about the window.
    """
    today = dt_util.now().date()
    # contract / meter overrides let the OptionsFlow's compare path run
    # this same engine against an alternative supplier's snapshot
    # without mutating the live entry. The user's region / DSO / regime /
    # solar_kva always come from the entry: those are the user's setup,
    # not the alternative's.
    contract = contract_override or entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    meter = meter_override or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")

    # Dispatch on the EFFECTIVE energy leg. A variable contract with a start
    # date re-prices its signing cohort to a SpotMonthlyRates leg, which bills
    # on the monthly-mean hourly path rather than the variable static daily
    # path. _cohort_energy_leg returns None for the compare flow, leaving the
    # current card's kind, and for a contract without a start date unless its
    # card is month indexed and the entry has a key: that card is re-priced
    # on the monthly mean too (_month_indexed_leg). The per-month walk
    # resolves the same cohort leg through
    # _effective_snapshot_for_month, so dispatch and per-month pricing agree.
    # The entry's own opt-in belongs to the side it was made on; a contract
    # that is not this entry's is judged only by what its card prints. Same
    # split the comparison page's own month-mean resolver makes, and for the
    # same reason: a custom monthly entry that ticked the SPP box must not
    # re-price a foreign card's formula onto an index that card never names.
    if spp_weights is not None and not (
        _spp_weighting_enabled(entry, snapshot)
        if contract == entry.data.get(CONF_CONTRACT)
        else _injection_is_spp_indexed(snapshot)
    ):
        spp_weights = None

    cohort_energy = await _cohort_energy_leg(
        hass, session, extractor, contract, region, entry, snapshot
    )
    eff_energy = snapshot.energy if cohort_energy is None else cohort_energy

    # The window every leg below accumulates over. Normally the year-to-date
    # one; ``window_start_override`` narrows it to the running month for the
    # month-to-date sensor, which is the same bill over a shorter period. It is
    # resolved ONCE here and handed to every accumulator rather than each
    # deriving it: an override reaching the energy walk but not the fee walk is
    # how these legs have drifted apart before.
    window_start = window_start_override or ytd_window_start(entry, today)
    end = today if window_end is None else window_end
    # A welcome credit belongs to the product version signed, and EnergyVision
    # moved that figure four times between March and September 2026, so the
    # amount comes off the SIGNING month's card rather than today's. Identity
    # for an entry naming no cohort month and for a month no archive holds,
    # and the row is the one _cohort_legs already resolves every tick, so this
    # is a cache hit rather than a second fetch.
    signing_snapshot = await signing_month_snapshot(
        hass,
        session,
        extractor,
        contract,
        region,
        entry,
        snapshot,
        cached_only=cached_only,
    )

    static_fees = await _ytd_static_fees(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        end,
        window_start=window_start,
        contract=contract,
        meter=meter,
        cached_only=cached_only,
    )
    prosumer_ytd = await _ytd_prosumer(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        end,
        window_start=window_start,
        contract=contract,
        cached_only=cached_only,
    )
    capacity_ytd = await _ytd_capacity(
        hass,
        session,
        extractor,
        snapshot,
        entry,
        end,
        billed_peak_kw,
        window_start=window_start,
        contract=contract,
        meter=meter,
        cached_only=cached_only,
    )
    fees = static_fees.total + prosumer_ytd + capacity_ytd
    # The breakdown when the caller asked for one, a throwaway otherwise. Every
    # figure written into it is a sum already computed, so filling one nobody
    # reads costs nothing, and it means the credit's cap can read the window's
    # consumption on every path instead of only on the ones being diagnosed.
    stats: dict[str, float] = breakdown if breakdown is not None else {}
    # Reported on every contract kind, not just the static path: the fees
    # floor is what a low bill rests on whichever way energy is priced.
    stats["fees_ytd_eur"] = fees
    # And split, because the lump hid the leg most able to move it. The
    # Flanders capacity tariff is billed per kW of monthly peak per year
    # (52 to 60 EUR/kW across the Fluvius areas), so two entries reading
    # the same meter and the same card still differ by hundreds of euro
    # when they resolve different peaks. None of that shows on the price
    # graph, which is per kWh, so the only way a user could see it was to
    # download diagnostics. One comparison of this attribute now answers
    # "why do my two entries disagree".
    stats["capacity_ytd_eur"] = capacity_ytd
    stats["prosumer_ytd_eur"] = prosumer_ytd
    stats["standing_charges_ytd_eur"] = static_fees.total
    stats["billed_peak_kw"] = billed_peak_kw

    def _bill(energy: float) -> float:
        """The window's bill: energy plus fees, less any welcome credit.

        Every branch below returns through here. This function prices a window
        four different ways (per hour on a dynamic, spot-monthly or TOU /
        night-circuit contract, per day otherwise) and falls back to a
        fees-only floor in four more places, and a credit applied on one of
        those is a credit missing from the other seven.

        Own contract and candidate alike, on the entry's own start date. The
        compare page walks this function for a contract the household never
        signed, and the question that column answers is what THIS year would
        have cost on it, signed when the household signed its own; a welcome
        credit is part of that answer. Crediting the own row and not the
        candidate's put the household's real bill, credit included, beside
        alternatives priced as though nobody was ever granted one, up to a
        whole credit in the household's favour. The signing snapshot resolves
        to the candidate's current card, so a candidate is credited what its
        card prints today, where the own contract reads the month it signed.
        """
        credit = 0.0
        # Every shape, through the predicate that lives beside the leaf: this
        # tested the two EUR halves and dropped 0.27.2's percentage campaign
        # and its kWh cashback whole.
        if grants_a_welcome_credit(signing_snapshot):
            credit = _welcome_credit_eur(
                signing_snapshot,
                _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE)),
                window_start,
                end,
                # The three components a welcome credit may come off and no
                # others: the supplier's ENERGY component of what the window's
                # consumption was charged, the SUPPLIER's standing charge (not
                # the energy fund, the data-management charge or the Brussels
                # OSP fee sitting beside it in static_fees) and the region's
                # green electricity / CHP contribution. Not ``energy``: that
                # is the all-in figure the walks bill, network and taxes
                # included and net of the feed-in credit, and measured against
                # it the cap let a 365 kWh/year connection through at 42,61
                # EUR over a quarter where the card grants 26,28, while a site
                # exporting more than it used was capped below its own
                # energiekost. The walks keep the component beside the bill
                # for exactly this sum.
                stats.get("energy_component_ytd_eur", 0.0)
                + static_fees.supplier_fee
                # Accumulated per month by the walks, on each month's own
                # card, which is what the backfill has always done. Reading
                # today's levy against the window's whole volume priced a
                # past month's kWh at a rate it never carried. The levy moves
                # inside the year on about a fifth of the archived series,
                # 49 of 255 on a replay of the whole card archive; the "54
                # rows" this used to say was a count of series read as a count
                # of rows.
                + stats.get(
                    "green_component_ytd_eur",
                    stats.get("consumption_ytd_kwh", 0.0)
                    * renewables_eur_per_kwh(snapshot.taxes, region),
                ),
                # The FIRST CONTRACT YEAR's net volume, not this window's.
                # The card's per-kWh term is a yearly one ("pour votre
                # premiere annee de consommation nette d'electricite"), and
                # the accrual inside places it in the window; passing the
                # year-to-date volume instead billed it on whatever share of
                # a year had gone by.
                first_year_net_kwh(
                    entry_annual_kwh(entry),
                    stats.get("consumption_ytd_kwh", 0.0),
                    stats.get("injection_ytd_kwh", 0.0),
                    compensation=regime == SOLAR_REGIME_COMPENSATION,
                ),
                # What a percentage credit is a percentage of, and what a
                # volume of free energy is worth: the rate this window really
                # billed, blended across whatever registers and hours it drew.
                window_energy_rate(
                    stats.get("energy_component_ytd_eur", 0.0),
                    stats.get("consumption_ytd_kwh", 0.0),
                ),
                # A year of SOLD export for a first-year feed-in bonus, zero
                # off the injection regime or without a measured year of it.
                first_year_injection_kwh=entry_annual_injection_kwh(entry),
            )
        stats["welcome_credit_eur"] = credit
        return energy + fees - credit

    # Dynamic contracts replay historical hourly ENTSO-E spots so each
    # past kWh hits its actual factor*spot+base rate. Caller passes the
    # spot cache (the coordinator persists it between runs); an hour the
    # cache cannot price still gets its network and tax legs.
    if isinstance(eff_energy, DynamicRates):
        # An empty spot cache is not a reason to bill nothing. Every hour still
        # carries a network and a tax leg, so pass {} rather than bailing to the
        # fees floor and let the replay price those and drop the energy term
        # alone (same rule the per-hour gap follows).
        dyn_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            window_start=window_start,
            window_end=window_end,
            contract=contract,
            meter=meter,
            breakdown=stats,
            historical_spots=historical_spots or {},
            spot_quarters=spot_quarters,
            spp_weights=spp_weights,
            rlp_weights=rlp_weights,
            rlp_index_weights=rlp_index_weights,
            cached_only=cached_only,
        )
        if dyn_energy is None:
            return _bill(0.0)
        return _bill(dyn_energy)

    # Spot-monthly contracts bill each past hour at its delivery month's mean
    # spot (a flat rate within the month); the hourly replay threads that mean
    # in place of the live spot and credits mean-indexed injection the same way.
    if isinstance(eff_energy, SpotMonthlyRates):
        monthly_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            window_start=window_start,
            window_end=window_end,
            contract=contract,
            meter=meter,
            breakdown=stats,
            historical_spots=historical_spots or {},
            spot_quarters=spot_quarters,
            monthly_mean=True,
            spp_weights=spp_weights,
            rlp_weights=rlp_weights,
            rlp_index_weights=rlp_index_weights,
            cached_only=cached_only,
        )
        if monthly_energy is None:
            return _bill(0.0)
        return _bill(monthly_energy)

    # Per-hour billing is required when the supplier's energy rates
    # vary by hour (TOU + Impact energy contracts), when the DSO bills
    # per Impact band (PIC / MEDIUM / ECO change with hour-of-day), or
    # for an exclusive_night meter (its energy + distribution use the
    # dedicated exclusive-night rates, which the static per-day branch's
    # single/peak/offpeak breakdowns don't carry, so without this it
    # would bill the YTD at the day rate while the live sensor uses the
    # cheaper exclusive-night rate). All go through the same hourly path,
    # which routes the meter through compute_breakdown.
    needs_hourly = (
        isinstance(eff_energy, (TimeOfUseRates, ImpactRates))
        or dso_mode == DSO_MODE_IMPACT
        or meter == METER_EXCLUSIVE_NIGHT
    )
    if needs_hourly:
        hourly_energy = await _ytd_hourly_energy(
            hass,
            session,
            extractor,
            snapshot,
            entry,
            today,
            window_start=window_start,
            window_end=window_end,
            contract=contract,
            meter=meter,
            breakdown=stats,
            historical_spots=historical_spots or {},
            spot_quarters=spot_quarters,
            spp_weights=spp_weights,
            rlp_weights=rlp_weights,
            rlp_index_weights=rlp_index_weights,
            cached_only=cached_only,
        )
        if hourly_energy is None:
            return _bill(0.0)
        # No separate feed-in term here, unlike the per-day walk below: this
        # branch holds the spot cache, so the walk itself credits a per-hour
        # formula hour by hour. Adding one would credit it twice.
        return _bill(hourly_energy)

    # The EFFECTIVE meter, not the entry's. The comparison page quotes a
    # target contract on a meter the household need not have, and the band
    # split has to follow the rate about to be applied: on the mono branch a
    # totals sensor puts the whole day in d_cons, which the bi branch below
    # then bills at the peak rate for every kWh of the year.
    daily_kwh = await _resolve_daily_kwh(
        hass, entry, end, start=window_start, meter=meter
    )
    if daily_kwh is None:
        # No meter inputs at all - fees-only floor.
        return _bill(0.0)

    # Precompute the snapshot + breakdowns for each month touched, so
    # the per-day loop stays O(days) without repeating the breakdown
    # math for every day in a month.
    month_breakdowns: dict[date, tuple[Any, Any, Any, "SupplierSnapshot"] | None] = {}

    async def _resolve_month(
        month_first: date,
    ) -> tuple[Any, Any, Any, "SupplierSnapshot"] | None:
        if month_first in month_breakdowns:
            return month_breakdowns[month_first]
        snap_m = await _effective_snapshot_for_month(
            hass,
            session,
            extractor,
            contract,
            region,
            month_first,
            snapshot,
            entry,
            cached_only=cached_only,
        )
        try:
            single_bd = static_breakdown(snap_m, dso, region, "single", dso_mode)
            peak_bd = static_breakdown(snap_m, dso, region, "peak", dso_mode)
            offpeak_bd = static_breakdown(snap_m, dso, region, "offpeak", dso_mode)
        except KeyError:
            # An archived snapshot can lose the user's DSO key when the
            # supplier renames a row or a regex misses for that month.
            # Treating the month as "no rate to apply" matches dynamic
            # / TOU behaviour and keeps the YTD loop running instead of
            # tearing the whole tick down with UpdateFailed.
            #
            # At WARNING, not DEBUG: the month is dropped from the year
            # whole, network leg and taxes included, and the fees beside it
            # bill zero for it. Measured on a Flemish entry that lost one
            # month's row, 2622,54 EUR against 1854,40, and nothing on
            # screen said so. Nobody reads a debug log to find out why a
            # year-to-date figure is a third light.
            _LOGGER.warning(
                "static_breakdown missing DSO %s for %s/%s/%s; that month is "
                "dropped from the year-to-date figure",
                dso,
                snap_m.supplier,
                snap_m.contract,
                month_first,
            )
            month_breakdowns[month_first] = None
            return None
        if single_bd is None or peak_bd is None or offpeak_bd is None:
            month_breakdowns[month_first] = None
            return None
        bundle = (single_bd, peak_bd, offpeak_bd, snap_m)
        month_breakdowns[month_first] = bundle
        return bundle

    energy_cost = 0.0
    # The supplier's energy component of the window's consumption, gross of
    # the feed-in credit: the cap base of a welcome credit (see the hourly
    # walk above, which keeps the same running sum for the same reason).
    energy_component = 0.0
    green_component = 0.0
    netting = _NetAllocation()
    # A flat energy leg can still carry a monthly-indexed feed-in credit
    # (energie.be Vast). The daily walk has no spot of its own, so resolve the
    # delivery month's SPP-weighted mean here, memoised per month, and let the
    # shared helper fall back to the card's indicative when it is missing.
    day_spp: dict[tuple[int, int, bool], float | None] = {}
    day_bucket = _bucket_by_local_month(historical_spots) if historical_spots else {}
    for day in _days_through(window_start, end):
        bundle = await _resolve_month(date(day.year, day.month, 1))
        if bundle is None:
            # Dynamic / TOU month: no stable rate to apply for any of
            # its days.
            continue
        single_bd, peak_bd, offpeak_bd, snap_d = bundle

        d_cons, n_cons, d_inj, n_inj = daily_kwh.get(day, (0.0, 0.0, 0.0, 0.0))
        total_cons = d_cons + n_cons
        total_inj = d_inj + n_inj

        bi_capable = meter in ("bi", "dynamic")
        energy_component += (
            d_cons * peak_bd.energy + n_cons * offpeak_bd.energy
            if bi_capable
            else total_cons * single_bd.energy
        )
        green_component += total_cons * renewables_eur_per_kwh(snap_d.taxes, region)
        if regime == SOLAR_REGIME_COMPENSATION:
            # Yearly net metering, per register, priced after the walk by
            # _NetAllocation: on the day's RLP mass when the profile is
            # loaded, else as metered.
            day_weights = _day_register_weights(rlp_weights, day, bi_capable, region)
            if bi_capable:
                netting.add(
                    "peak", d_cons - d_inj, peak_bd.all_in, day_weights.get("peak")
                )
                netting.add(
                    "offpeak",
                    n_cons - n_inj,
                    offpeak_bd.all_in,
                    day_weights.get("offpeak"),
                )
            else:
                netting.add(
                    "single",
                    total_cons - total_inj,
                    single_bd.all_in,
                    day_weights.get("single"),
                )
            d_cost = 0.0
        elif regime == SOLAR_REGIME_INJECTION:
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in
            inj_spot = _spp_injection_spot(
                None,
                monthly_mean=_injection_on_month_mean(snap_d),
                strict=_injection_is_spp_indexed(snap_d),
                index_realised=getattr(snap_d.injection, "index_realised", None),
                spp_weights=spp_weights,
                bucket=day_bucket,
                year=day.year,
                month=day.month,
                today=today,
                cache=day_spp,
            )

            # Asked once per register, because a card can print one feed-in
            # rate per meter register (Trevion Vast) and this walk holds the
            # day and night kWh apart already. The routing is the shared
            # helper's: it reads the pair only when the card flags it as the
            # registers' own rates and the meter has two, so every other card
            # answers the same rate to both questions and the sum below
            # collapses to what it always was. Without the four arguments the
            # helper cannot reach that branch at all, and a Trevion Vast
            # year-to-date was credited the flat printed rate while the
            # injection_price sensor beside it credited per register.
            def _rate_at(hour: int) -> float | None:
                return _historical_injection_rate(
                    snap_d.injection,
                    inj_spot,
                    energy=snap_d.energy,
                    when=datetime.combine(
                        day, time(hour), tzinfo=dt_util.DEFAULT_TIME_ZONE
                    ),
                    meter=meter,
                    region=region,
                )

            day_rate = _rate_at(_DAY_REGISTER_HOUR)
            night_rate = _rate_at(_NIGHT_REGISTER_HOUR)
            if day_rate is not None:
                d_cost -= d_inj * day_rate
            if night_rate is not None:
                d_cost -= n_inj * night_rate
        else:  # none
            if bi_capable:
                d_cost = d_cons * peak_bd.all_in + n_cons * offpeak_bd.all_in
            else:
                d_cost = total_cons * single_bd.all_in

        energy_cost += d_cost

    # Raw energy term before the compensation zero-floor: a negative value
    # here is what the clamp below hides, so surface it for diagnostics.
    energy_ytd_raw = energy_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        # Yearly net metering: each register's net for the window, priced by
        # _NetAllocation on the profile when it is loaded, else as metered,
        # and clamped at zero per register, since surplus injection past
        # consumption is forfeited (by most Walloon suppliers).
        allocated = rlp_weights is not None
        energy_ytd_raw = netting.raw(allocated=allocated)
        energy_cost = netting.billed(allocated=allocated)

    if regime == SOLAR_REGIME_INJECTION:
        # Spot-indexed injection on a static-energy contract (Cociter
        # Variable): the daily loop above credited nothing for it (its
        # injection has no monthly indicative), so subtract the per-hour
        # spot-replayed credit
        # here. A no-op (0.0) for every other contract.
        energy_cost -= await _ytd_spot_injection_credit(
            hass,
            snapshot,
            entry,
            end,
            historical_spots,
            _month_snapshot_cache(
                hass,
                session,
                extractor,
                contract,
                region,
                snapshot,
                entry,
                cached_only=cached_only,
            ),
            window_start=window_start,
            billed_days=daily_kwh.keys(),
            top_up=window_end is None,
        )
        # This regime has no compensation clamp, so the billed energy is
        # already the raw energy term.
        energy_ytd_raw = energy_cost

    stats["consumption_ytd_kwh"] = sum(r[0] + r[1] for r in daily_kwh.values())
    # Unconditionally, like the consumption beside it: the welcome credit's
    # per-kWh term is measured on NET consumption, so this is read on every
    # path and not only on the ones a caller is diagnosing.
    stats["injection_ytd_kwh"] = sum(r[2] + r[3] for r in daily_kwh.values())
    stats["energy_component_ytd_eur"] = energy_component
    stats["green_component_ytd_eur"] = green_component
    if breakdown is not None:
        # The per-day counterpart of hours_seen / hours_elapsed above: the
        # static branch reported no coverage at all, so a gap here was
        # invisible even in principle.
        breakdown["days_seen"] = float(len(daily_kwh))
        # Both sides of the pair span the window the walk covered. Counting
        # elapsed from 1 January against days the meter read from the contract
        # start is a coverage gap that is not there.
        breakdown["days_elapsed"] = float((end - window_start).days + 1)
        breakdown["injection_ytd_kwh"] = sum(r[2] + r[3] for r in daily_kwh.values())
        today_kwh = daily_kwh.get(today, (0.0, 0.0, 0.0, 0.0))
        breakdown["consumption_today_kwh"] = today_kwh[0] + today_kwh[1]
        breakdown["injection_today_kwh"] = today_kwh[2] + today_kwh[3]
        breakdown["energy_ytd_raw_eur"] = energy_ytd_raw

    return _bill(energy_cost)
