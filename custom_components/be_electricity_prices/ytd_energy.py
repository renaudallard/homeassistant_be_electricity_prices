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

"""The year-to-date energy and feed-in legs, hour by hour.

The only part of the running bill that has to replay the year: a spot-priced
contract is billed at the price of each hour the household consumed in, so
the walk cannot be short-cut to a monthly mean, and the feed-in credit it
earned is settled the same way. The standing charges beside it are
arithmetic on a count of days.
"""

from __future__ import annotations

from .cohort import _month_snapshot_cache, _parse_iso_date
from .const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_CONTRACT_START_DATE,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
)
from .energy_meters import (
    _hourly_injection_sensors,
    _metered_hourly_kwh,
    _top_up_today_hourly,
)
from .fees import in_first_contract_year
from .injection import (
    _historical_injection_rate,
    _injection_hourly_on_cohort,
    _injection_is_spot_formula,
    _injection_replays_hourly_spot,
)
from .pricing import (
    MeterType,
    compute_breakdown,
    compute_network_and_taxes,
    renewables_eur_per_kwh,
)
from .providers._rates import InjectionRates
from .providers.base import SupplierExtractor, SupplierSnapshot
from .spot_stats import (
    _NetAllocation,
    _bucket_by_local_month,
    _energy_needs_spot,
    _hour_spot,
    _injection_is_spp_indexed,
    _injection_on_month_mean,
    _register_for,
    _rlp_hour_weight,
    _spp_injection_spot,
)
from .synergrid import RlpWeights, SppWeights
from collections.abc import Awaitable, Callable, Collection
from datetime import date, datetime, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
import aiohttp


async def _ytd_hourly_energy(
    hass: HomeAssistant,
    session: aiohttp.ClientSession,
    extractor: SupplierExtractor,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    *,
    window_start: date,
    contract: str | None = None,
    meter: MeterType | None = None,
    historical_spots: dict[datetime, float] | None = None,
    spot_quarters: dict[datetime, list[float]] | None = None,
    monthly_mean: bool = False,
    spp_weights: SppWeights | None = None,
    rlp_weights: RlpWeights | None = None,
    rlp_index_weights: RlpWeights | None = None,
    breakdown: dict[str, float] | None = None,
    cached_only: bool = False,
    window_end: date | None = None,
) -> float | None:
    """YTD energy cost for hourly-billed contracts (TOU + dynamic).

    Bins the recorder's hourly kWh deltas through ``compute_breakdown``
    at each local hour, picking up the TOU slot rate (or the dynamic
    factor*spot+base) from the supplier and the bi-hourly / Impact
    distribution band from the user's DSO mode in one call. Reads from
    ``CONF_CONSUMPTION_KWH`` (single totals) when available, else sums
    the four day/night register sensors at hourly granularity. Each
    side (consumption, injection) is resolved independently,
    mirroring the static-path behaviour: a user with only injection
    wired (e.g. an inverter exposing solar export but no smart-meter
    consumption sensor) still gets the injection credit recognised.

    ``historical_spots`` is required for dynamic contracts (factor*spot+
    base needs a spot per hour). An hour the cache cannot price is NOT
    dropped: its network and tax legs are known from the month's snapshot
    regardless of the day-ahead price, so it bills those and loses the
    energy term alone. A partial backfill therefore understates the YTD
    by the commodity it could not price, rather than by the whole hour.
    TOU callers pass ``None`` and every hour gets billed at the slot rate.

    Quarter-hourly dynamic contracts (Engie, Cociter, EBEM, Ecofix, OCTA+,
    Ecopower DBS, Bolt Dynamisch, energie.be, EnergyVision, Energy Knights
    Agilior Online) bill the live price on 15-minute slots, but the
    recorder only retains hourly long-term statistics, so this YTD replay
    aggregates consumption / injection to the clock hour and prices each
    hour at its hourly spot. When intra-hour load correlates with the
    intra-hour price the YTD total is a close approximation, not a
    bit-exact reconciliation with the live 15-minute sensor.

    Solar handling is uniform across both paths:
      - ``compensation``: the hour's net ``cons - inj`` lands in its meter
        register and ``_NetAllocation`` prices each register's net for the
        window at the RLP-weighted mean of its all-in rates when the
        profile is loaded, else as metered, clamped at zero per register
        (Walloon meter forfeits surplus).
      - ``injection``: per-hour ``cons * all_in - inj * inj_rate``
        where ``inj_rate`` is the supplier's monthly indicative for TOU
        and ``factor*spot+base`` for dynamic at that hour's spot.
      - ``none``: per-hour ``cons * all_in``.

    Returns ``None`` only when neither side has any meters wired (the
    caller surfaces the fees-only floor).

    ``window_end`` closes the window on an earlier day, the last one billed,
    for a contract the household left during the year. ``today`` stays the
    calendar's: it decides whether a month is still running, which is a fact
    about the date and not about the window.
    """
    region = entry.data.get(CONF_REGION, "")
    dso = entry.data.get(CONF_DSO, "")
    contract = contract or entry.data[CONF_CONTRACT]
    meter = meter or entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")
    end = today if window_end is None else window_end

    cons = await _metered_hourly_kwh(hass, entry, "consumption", window_start, end)
    inj = await _metered_hourly_kwh(hass, entry, "injection", window_start, end)
    if cons is None or inj is None:
        # Same rule the static per-day path applies: a half-wired pair, or one
        # whose other half records nothing, means the missing band's kWh are
        # unavailable, so bill nothing rather than bill the wired half.
        # Without this the empty side vanished silently and any wired
        # injection was credited against zero consumption.
        return None
    if not cons.sensors and not inj.sensors:
        return None
    # An hour one half of a pair did not report leaves both sides.
    unknown = cons.unknown | inj.unknown
    cons_per_hour = {h: kwh for h, kwh in cons.kwh.items() if h not in unknown}
    inj_per_hour = {h: kwh for h, kwh in inj.kwh.items() if h not in unknown}
    # Statistics only carry the last COMPILED hour, so top today up from the
    # live meters the way the per-day branch has since 0.11.9. Without this
    # every hourly-billed contract stepped once an hour at best and froze
    # outright whenever compilation lagged or stalled. Off the sensors each
    # side was actually read from, which is the total when it stood in for a
    # broken pair.
    # Neither side is topped up while a pair on either cannot bill today: its
    # hours drop out at midnight, and the other side's live reading would be
    # billed against nothing in the meantime. Nor for a window that closed
    # before today: the live reading is not one of its hours.
    if window_end is None and cons.today_ok and inj.today_ok:
        await _top_up_today_hourly(hass, cons.sensors, cons_per_hour, today)
        await _top_up_today_hourly(hass, inj.sensors, inj_per_hour, today)

    _snap_for = _month_snapshot_cache(
        hass,
        session,
        extractor,
        contract,
        region,
        snapshot,
        entry,
        cached_only=cached_only,
    )

    # Spot-monthly contracts bill every hour of a delivery month at that
    # month's mean spot (energy and mean-indexed injection alike); cache the
    # mean per month so it's computed once.
    month_means: dict[tuple[int, int], float | None] = {}
    # SPP-weighted per-month injection means, when the entry opted in. Energy
    # keeps the flat mean above; only the injection credit uses these.
    month_spp: dict[tuple[int, int, bool], float | None] = {}
    # Bucket the year's spots by local month once so each month's mean is a
    # lookup rather than a full-year rescan (the loop reads up to twelve
    # distinct months). TWO independent readers: a month-priced ENERGY leg,
    # and a feed-in credit that settles on a month index whatever the energy
    # does (Eneco Fix prices energy without a mean and indexes its credit on
    # one).
    #
    # Built whenever there are spots to bucket, and no longer gated on what
    # the CURRENT card needs. The walk prices each month off that month's own
    # archived card and asks it, at ``snap_h``, whether it wants a month mean;
    # the gate asked today's card the same question and answered for all of
    # them. A card that has since dropped its monthly formula therefore left
    # the bucket empty under a month that still wanted one, and the backfill
    # beside this buckets internally and did not agree: 0,003026 EUR/kWh on an
    # Eneco month, and up to 20,8% on Ecopower's SPP shape. Nothing in the
    # archive is in that state today, and Ecopower has already moved a card
    # the other way, so it is one ordinary revision away.
    #
    # The cost of dropping the gate is one linear pass over the cached year
    # for the dynamic contracts that read neither, which is not worth a
    # divergence between two walks of the same bill.
    month_bucket = _bucket_by_local_month(historical_spots) if historical_spots else {}
    # A static card whose injection is a per-hour spot formula with no printed
    # indicative (Cociter Tarif Variable) keeps that hourly index even on the
    # monthly-mean path, which it reaches only via a signing-cohort re-price of
    # the ENERGY leg. Same gate the live tick applies before baking, asked of
    # the leg the hour's month credits, once per month.
    hourly_by_month: dict[tuple[int, int], bool] = {}
    # The hour's own 15-minute spots, for the one feed-in formula that is not
    # linear in the spot and so is not priced by their mean (see
    # _injection_needs_spot_quarters). Empty for every other entry, which is
    # what makes reading them a no-op there. A credit that settles on a month
    # mean is deliberately excluded: a mean of means says nothing about what
    # one hour's quarters did.
    quarters: dict[datetime, list[float]] = spot_quarters or {}

    energy_cost = 0.0
    # The supplier's ENERGY component of what the window's consumption was
    # charged, gross of any feed-in credit. Not part of the bill, which the
    # all-in figure above already carries; it is the base a welcome credit is
    # capped against, and the card names exactly this component, not the
    # network or tax legs beside it and not the feed-in line either.
    energy_component = 0.0
    green_component = 0.0
    # The same component over the contract's own first-year hours alone, and
    # what they drew: the rate a percentage welcome credit is a share of.
    credit_start = _parse_iso_date(entry.data.get(CONF_CONTRACT_START_DATE))
    credit_component = 0.0
    credit_kwh = 0.0
    # How much of the window actually got an energy price. A YTD that is low
    # because the spot cache is thin looks identical to a low one that is
    # correct, so report the coverage instead of leaving the user to guess.
    hours_seen = 0
    hours_priced = 0
    netting = _NetAllocation()
    # Iterate the union of both sides so an injection-only wiring
    # still contributes its credit (mirroring _resolve_daily_kwh).
    for utc_hour in cons_per_hour.keys() | inj_per_hour.keys():
        hours_seen += 1
        local = dt_util.as_local(utc_hour)
        snap_h = await _snap_for(date(local.year, local.month, 1))
        # The HOUR's own leg decides, not the branch the caller dispatched on:
        # a spot-monthly leg takes the month's index (the supplier's published
        # realised value first, then the RLP-weighted or plain mean of the
        # cached hours, gated on coverage so a closed month cached too thinly
        # does not price every one of its hours off an unrepresentative
        # handful), a dynamic leg the hour's own price, and every other kind
        # carries a resolved rate and needs none. Asking the leg is also what
        # lets this walk hold the spot cache on the branches whose energy does
        # not need it, so the feed-in credit beside it can still resolve.
        spot = _hour_spot(
            snap_h.energy,
            local,
            utc_hour,
            historical_spots or {},
            month_bucket,
            month_means,
            today,
            # The card's own RLP blend, which is the entry's for its own bill
            # and the quoted card's on the compare page. Everything else below
            # weights by the HOUSEHOLD's profile, because there it stands for
            # the household's load shape rather than for a published index.
            rlp_weights if rlp_index_weights is None else rlp_index_weights,
        )
        # Distinguishes "this contract needs no spot" (fixed, variable, TOU,
        # Impact) from "it needs one and the cache has none", which are billed
        # differently. Same rule the backfill applies to the same question.
        spot_missing = spot is None and _energy_needs_spot(snap_h.energy)
        try:
            if spot_missing:
                # No spot for this hour. Bill the two legs that do not depend
                # on one instead of dropping the hour whole; the energy term is
                # the only part actually unknown.
                bd = compute_network_and_taxes(
                    snap_h, dso, region, local, meter, dso_mode
                )
            else:
                bd = compute_breakdown(
                    snap_h, dso, region, local, spot, meter, dso_mode
                )
                hours_priced += 1
        except (KeyError, ValueError):
            # Missing DSO row or non-static rate kind: skip this hour.
            continue
        kwh_cons = cons_per_hour.get(utc_hour, 0.0)
        kwh_inj = inj_per_hour.get(utc_hour, 0.0)
        # An unpriced hour carries a zero energy component, so it adds nothing
        # here either: what could not be charged cannot be credited against.
        energy_component += kwh_cons * bd.energy
        if in_first_contract_year(credit_start, local.date()):
            credit_component += kwh_cons * bd.energy
            credit_kwh += kwh_cons
        # On the HOUR's own card, the way the backfill accumulates it: the
        # green levy belongs to the delivery month, and a welcome credit is
        # capped against what the window was actually charged.
        green_component += kwh_cons * renewables_eur_per_kwh(snap_h.taxes, region)
        if regime == SOLAR_REGIME_COMPENSATION:
            # Yearly net metering: the hour's net lands in a register and is
            # priced by _NetAllocation after the walk, on the profile when it
            # is loaded, else as metered.
            netting.add(
                _register_for(local, meter, dso_mode, region),
                kwh_cons - kwh_inj,
                bd.all_in,
                _rlp_hour_weight(rlp_weights, local),
            )
            d_cost = 0.0
        elif regime == SOLAR_REGIME_INJECTION:
            d_cost = kwh_cons * bd.all_in
            month_key = (local.year, local.month)
            hourly_injection = hourly_by_month.get(month_key)
            if hourly_injection is None:
                hourly_injection = monthly_mean and _injection_hourly_on_cohort(
                    snapshot, snap_h.injection, entry
                )
                hourly_by_month[month_key] = hourly_injection
            # Energy bills at the flat month-mean (spot); the injection credit
            # uses the SPP-weighted month-mean when the entry opted in, falling
            # back to the flat mean when the profile is missing for the month
            # - unless the CARD indexes on Belpex_SPP, where the flat mean is
            # a different index rather than a coarser one and the card's own
            # indicative is credited instead.
            inj_spot = _spp_injection_spot(
                # The hour's spot goes in only when the CREDIT is the one that
                # replays it, judged by the same predicate the live scalar
                # uses. ``spot`` is resolved for the ENERGY leg, and the two
                # legs need not agree: a card that prints a monthly indicative
                # beside its formula bills the indicative, and handing this the
                # energy's spot would price a whole year of feed-in off a
                # formula the card calls an illustration.
                (
                    spot
                    if snap_h.injection is not None
                    and _injection_is_spot_formula(snap_h.injection, snap_h.energy)
                    else None
                ),
                # The INJECTION's flag, not the energy leg's. They differ on
                # exactly the cards this matters for: Eneco Fix and Flex price
                # energy without a mean and index the credit on one, so the
                # energy flag is False here and ``spot`` is this hour's own
                # price. Passing it resolved a month formula per hour.
                monthly_mean=_injection_on_month_mean(snap_h),
                strict=_injection_is_spp_indexed(snap_h),
                # The month's own settled index when the supplier has published
                # it, which makes every source below moot.
                index_realised=getattr(snap_h.injection, "index_realised", None),
                spp_weights=spp_weights,
                bucket=month_bucket,
                year=local.year,
                month=local.month,
                today=today,
                cache=month_spp,
                hourly=hourly_injection,
                hourly_spot=(
                    historical_spots.get(utc_hour)
                    if historical_spots is not None
                    else None
                ),
            )
            inj_rate = _historical_injection_rate(
                snap_h.injection,
                inj_spot,
                quarters=(
                    quarters.get(utc_hour)
                    if hourly_injection or not monthly_mean
                    else None
                ),
                energy=snap_h.energy,
                when=local,
                meter=meter,
                region=region,
            )
            if inj_rate is not None:
                d_cost -= kwh_inj * inj_rate
        else:
            d_cost = kwh_cons * bd.all_in
        energy_cost += d_cost

    if regime == SOLAR_REGIME_COMPENSATION:
        allocated = rlp_weights is not None
        energy_cost = netting.billed(allocated=allocated)
        if breakdown is not None:
            breakdown["energy_ytd_raw_eur"] = netting.raw(allocated=allocated)
    if breakdown is not None:
        breakdown["hours_seen"] = float(hours_seen)
        breakdown["hours_priced"] = float(hours_priced)
        # And what the window SHOULD hold. hours_seen counts only the buckets
        # the recorder returned, so it shrinks with a gap and hours_priced
        # shrinks with it: the pair reads a confident 100% while hundreds of
        # hours are missing entirely. Comparing against elapsed is the only
        # way that failure is visible from the sensor.
        #
        # Measured from the WINDOW, not from 1 January. hours_seen counts the
        # window's buckets, so an entry billing from its contract start date
        # was reporting 1560 hours seen against 5892 elapsed and inviting its
        # owner to go looking for a recorder fault that was not there.
        # To the window's end: the running hour for a window that runs to
        # today, the midnight after its last day for one that closed earlier.
        until = (
            dt_util.now()
            if window_end is None
            else dt_util.start_of_local_day(window_end + timedelta(days=1))
        )
        elapsed = until - dt_util.start_of_local_day(window_start)
        breakdown["hours_elapsed"] = float(int(elapsed.total_seconds() // 3600))
        breakdown["consumption_ytd_kwh"] = sum(cons_per_hour.values())
        breakdown["injection_ytd_kwh"] = sum(inj_per_hour.values())
        breakdown["energy_component_ytd_eur"] = energy_component
        breakdown["green_component_ytd_eur"] = green_component
        breakdown["credit_energy_component_eur"] = credit_component
        breakdown["credit_consumption_kwh"] = credit_kwh
    return energy_cost


async def _any_month_replays_hourly_spot(
    snap_for: Callable[[date], Awaitable[SupplierSnapshot]],
    window_start: date,
    today: date,
) -> bool:
    """Whether any month of the window is priced on a card whose feed-in
    settles on the hour's own spot, judged the way the hour loop judges it."""
    month = date(window_start.year, window_start.month, 1)
    while month <= today:
        inj = (await snap_for(month)).injection
        if inj is not None and _injection_replays_hourly_spot(inj):
            return True
        month = (month + timedelta(days=32)).replace(day=1)
    return False


async def _ytd_spot_injection_credit(
    hass: HomeAssistant,
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    today: date,
    historical_spots: dict[datetime, float] | None,
    snap_for: Callable[[date], Awaitable[SupplierSnapshot]] | None = None,
    *,
    window_start: date,
    billed_days: Collection[date] | None = None,
    top_up: bool = True,
) -> float:
    """YTD solar-injection credit (EUR) for a contract whose injection is
    a per-hour spot formula with no monthly indicative.

    Sums per-hour injected kWh * (factor*spot + base) from the recorder's
    hourly statistics and the persistent historical-spot cache, for
    Cociter Variable: a static-energy card that publishes an hourly
    BELPEX injection formula but no fixed credit. The static per-day YTD
    path can't price these (no spot per
    day), so this isolated term replays the spots the same way the
    dynamic energy path does, and the caller subtracts it from the bill.

    Returns 0.0 (a no-op) unless the injection is one of the two shapes
    ``_injection_replays_hourly_spot`` names, spots are cached, and an
    injection sensor is wired. Hours with no cached spot are skipped.

    The second of those shapes is the card that prints an indicative and
    calls it an illustration (every Bolt fixed and variable card). It used to
    be excluded here on the printed figure alone, so the walk credited that
    figure while the injection_price sensor, the backfill and the compare
    page all billed the Belpex formula. The two guards move together with the
    fallback in ``_historical_injection_rate``: relaxing one without the
    other either double-credits the feed-in or drops it.

    ``snap_for`` resolves each hour to its own delivery month's card, the way
    the sibling walks and the backfill already do. Without it every past hour
    was credited at TODAY's coefficients, so a contract whose feed-in formula
    moved during the year was re-credited for the whole year at its newest
    terms. An hour whose month printed an indicative is skipped here, because
    the walk this term is added to already credited that month off it, and
    crediting it twice would double the feed-in.

    ``today`` is the window's last day. ``top_up`` is off for a window that
    closed before today, whose hours the live meter reading is not one of.
    """
    inj = snapshot.injection
    if not historical_spots:
        return 0.0
    if snap_for is None and (inj is None or not _injection_replays_hourly_spot(inj)):
        # With no resolver every hour takes the current card, so its shape
        # decides. With one, each month's own card decides below: judging the
        # current card here too skipped a month whose card replays the spot
        # whenever the newest card printed only an indicative, and that
        # month's credit was then dropped by both walks.
        return 0.0
    inj_ids = _hourly_injection_sensors(entry)
    if not inj_ids:
        return 0.0
    if snap_for is not None and not await _any_month_replays_hourly_spot(
        snap_for, window_start, today
    ):
        # The months are asked before the recorder is: at most twelve memoised
        # resolutions, against the statistics query over the whole window that
        # a card printing an indicative every month otherwise paid on every
        # tick for a credit that is always zero.
        return 0.0
    metered = await _metered_hourly_kwh(hass, entry, "injection", window_start, today)
    if metered is None:
        return 0.0
    per_hour = metered.kwh
    # Topped up from the live meter, exactly as both sibling paths do: the
    # daily branch through _recorder_daily_kwh and the hourly branch through
    # its own two _top_up_today_hourly calls. Without it the consumption leg
    # of one bill was live to the minute while its offsetting feed-in credit
    # trailed the last COMPILED hour, so current_year_cost over-stated the
    # bill by whatever of today's injection statistics had not booked yet, and
    # did not heal at all while compilation was stalled.
    if top_up and metered.today_ok:
        await _top_up_today_hourly(hass, metered.sensors, per_hour, today)
    credit = 0.0
    for utc_hour, kwh in per_hour.items():
        # Only the days the per-day walk this credit is added to billed: a day
        # it left out because one half of a register pair did not report it
        # has no consumption on the bill, and crediting its feed-in anyway
        # drove the year down on days nothing was charged.
        if billed_days is not None and dt_util.as_local(utc_hour).date() not in (
            billed_days
        ):
            continue
        spot = historical_spots.get(utc_hour)
        if spot is None:
            continue
        inj_h: InjectionRates | None = inj
        if snap_for is not None:
            local = dt_util.as_local(utc_hour)
            inj_h = (await snap_for(date(local.year, local.month, 1))).injection
            if inj_h is None or not _injection_replays_hourly_spot(inj_h):
                # That month is not this shape, so its own card was already
                # credited by the walk this term is added to.
                continue
        # Route through the shared helper so the floor_at_zero clamp the live
        # scalar and array apply is honoured here too, rather than summing the
        # raw factor*spot+base and diverging on a negative-spot hour.
        #
        # No quarters here, and none can exist: this term serves a card whose
        # ENERGY leg is static, and only DynamicRates carries quarter_hourly,
        # so the hour's spot IS the hour's price.
        credit += kwh * (_historical_injection_rate(inj_h, spot) or 0.0)
    return credit
