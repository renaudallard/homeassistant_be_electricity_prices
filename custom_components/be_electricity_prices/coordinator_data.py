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

"""What the coordinator publishes, and the windows it accumulates over.

A leaf: the sensors, the binary sensors, diagnostics and every coordinator
mixin read CoordinatorData, and a mixin that builds one cannot import it from
the module that mixes it in. The window helpers sit with it because the same
readers need to know which day a running total started from, and because the
yearly and monthly windows have to agree with each other or a month stops
being a slice of the year it sits in.
"""

from __future__ import annotations

from .cohort import ytd_window_start
from .const import RESOLUTION_HOURLY
from .pricing import PriceBreakdown
from dataclasses import dataclass, field
from datetime import date, datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from typing import Any


@dataclass
class CoordinatorData:
    """Snapshot the coordinator hands to entities."""

    hourly: dict[datetime, PriceBreakdown] = field(default_factory=dict)
    # Static all-in prices for peak and offpeak bands, used by the Energy
    # Dashboard for bi-hourly meter configurations. These do NOT vary with the
    # time of day - they represent the constant rate for that band. None when
    # the contract has no static rate (dynamic, TOU) or on the Wallonia impact
    # tariff. The Energy Dashboard needs these as separate sensors because it
    # expects one price entity per grid source (tariff 1 = day, tariff 2 = night).
    static_peak_price: PriceBreakdown | None = None
    static_offpeak_price: PriceBreakdown | None = None
    # Static injection (feed-in) rates for peak and offpeak, for bi-hourly
    # meter configurations with separate day/night injection compensation.
    # None when the contract has a single injection rate or spot-indexed.
    static_injection_peak: float | None = None
    static_injection_offpeak: float | None = None
    # Grid resolution of the keys in ``hourly``: RESOLUTION_HOURLY for
    # every static / hourly-billed contract, RESOLUTION_QUARTER for
    # dynamic suppliers that bill per quarter-hour (Engie). Consumers use
    # it to truncate "now" to the right slot and to size the
    # cheapest-window service.
    resolution: str = RESOLUTION_HOURLY
    snapshot_publication: str = ""
    # Which card a contract that names a cohort month bills on, once the
    # signing cohort has been resolved. Empty when the entry names none, so
    # the attribute appears only where it has something to say.
    signing_card: str = ""
    snapshot_age_hours: float = 0.0
    snapshot_stale: bool = False
    # Last calendar day the snapshot's rates apply to. ``None`` means
    # the extractor couldn't parse a validity end: callers should
    # fall back to "treat as valid".
    snapshot_valid_until: date | None = None
    last_error: str = ""
    # True while these prices come from a card that had no text layer and was
    # read off its pixels by the repository's archive walk. Not an error: the
    # figures are the ones the engine read whole or did not read at all. The
    # user is told because a price read off a picture of a card is not quite
    # the same fact as a price read out of one.
    card_read_by_ocr: bool = False
    # Which source supplied the day-ahead curve behind these prices:
    # "entsoe" (source of record) or "energy-charts" (the keyless fallback,
    # used only while ENTSO-E is unreachable). Not an error, so deliberately
    # kept out of last_error, which drives the staleness Repairs card.
    spot_source: str = "entsoe"
    # This month's running peak, as measured. NOT floored at the regulated
    # minimum: it is a measurement, and the floor is a billing rule that
    # belongs on the quantity below.
    monthly_peak_kw: float = 0.0
    monthly_peak_month: date | None = None
    # The kW the capacity tariff is charged on: the mean of the last twelve
    # monthly peaks, floored at VREG_CAPACITY_FLOOR_KW. Surfaced as attributes
    # on capacity_cost so the bill can be told apart from this month's reading,
    # together with how many months the mean covers (12 once a full year of
    # history has accumulated).
    capacity_billed_peak_kw: float = 0.0
    capacity_peak_months: int = 0
    capacity_cost_eur: float = 0.0
    prosumer_cost_eur: float = 0.0
    # EUR/kWh injection price for the slot this tick ran in. The sensor only
    # publishes it for contracts with no ``injection_hourly``; everything that
    # varies intra-day is read per slot from that table instead, so this value
    # does not follow the clock between ticks. None when:
    #   - the user is not on the injection regime, or
    #   - the snapshot's injection block has no usable data (formula needs
    #     spot but contract is variable so we don't fetch ENTSO-E).
    injection_price_eur_per_kwh: float | None = None
    # Per-slot injection price (EUR/kWh) across the same today+tomorrow grid
    # as ``hourly``. Drives BOTH the injection_price sensor's state (looked up
    # at the current slot, which is what keeps it on the slot the user is
    # billed for) and its today/tomorrow arrays, so narrowing or dropping this
    # table would silently put the state back on the tick's scalar (issue #44).
    # Empty except on the injection regime for a contract whose injection
    # varies intra-day (spot-indexed dynamic + Cociter Variable, or the Engie
    # Empower Flextime TOU schedule); flat contracts emit no array since it
    # would just repeat the scalar above. Same quarter->hour downsampling as
    # the consumption arrays happens in the sensor layer, for the arrays only.
    injection_hourly: dict[datetime, float] = field(default_factory=dict)
    # Supplier yearly fixed fee (EUR/year) and Flemish energy-fund
    # monthly charge (EUR/month). Both are parsed from the tariff card
    # but don't enter the per-kWh all-in number; surfacing them as
    # separate sensors lets users compute total monthly cost.
    yearly_fixed_fee_eur: float = 0.0
    energy_fund_eur_per_month: float = 0.0
    # The SPF's flat-rate ceiling for reimbursing home charging of a company
    # car, for the entry's region and the running quarter, EUR/kWh
    # (``creg_ev.py``). None until the file has been read, and for a quarter
    # it does not cover.
    ev_home_charging_rate_eur_per_kwh: float | None = None
    # The quarter that rate is for, baked with it at the tick. Read off the
    # clock when the sensor rendered, the slot push at 00:00 on the first day
    # of a quarter showed the new quarter beside the old quarter's rate until
    # the next tick.
    ev_home_charging_quarter_start: date | None = None
    # The contracts the household held earlier this year, as the
    # current_year_cost sensor lists them (contract_periods.previous_rows).
    # Empty on an entry that recorded no switch.
    previous_contracts: tuple[dict[str, Any], ...] = ()
    # Running annual bill in EUR, accumulated day by day from Jan 1.
    # Falls back to the (pro-rated) fees-only floor when no meter
    # sensors are wired. For compensation regime the math nets
    # injection 1:1 against consumption (per-band when bi) and clamps
    # the YTD energy term at zero (Walloon suppliers forfeit surplus
    # injection past consumption); for injection regime each side is
    # multiplied by its own rate and the running total can dip
    # negative when injection credit exceeds consumption + pro-rated
    # fees; for "none" only consumption counts.
    current_year_cost_eur: float | None = None
    # The same bill accumulated over the running month instead of the year.
    # Priced as its own window, so under the compensation regime it nets that
    # month's registers rather than taking a slice of the year's netting, and
    # twelve of them do not add up to current_year_cost_eur on such an entry.
    current_month_cost_eur: float | None = None
    # The window start each running cost above was computed over, which its
    # sensor publishes as ``last_reset``. Baked beside the value rather than
    # read off the clock when the state is written: the hourly push at
    # 00:00:00 on the 1st rewrites the last tick's figure, and a last_reset
    # read then named the new period for the old period's total, which is the
    # one reading the Energy dashboard keeps if the refresh after it fails.
    current_year_cost_reset: datetime | None = None
    current_month_cost_reset: datetime | None = None
    # Optional diagnostic breakdown behind current_year_cost: YTD and today
    # consumption / injection kWh, the pre-clamp raw energy term and the fees
    # floor. Populated only on the static per-day (fixed / variable) path;
    # None for hourly-billed contracts and when no meter is wired. Surfaced as
    # attributes so a flat sensor can be told apart (negative raw energy = the
    # compensation clamp; a today kWh that never moves = stalled meter input).
    ytd_diagnostics: dict[str, float] | None = None
    # Roughly what a year on this contract costs: a full year priced at today's
    # tariffs against the entry's own metered yearly volume, computed in one
    # pass rather than as elapsed plus remainder. None for a contract whose
    # rate is a formula over an index that does not exist yet, and for a netted
    # meter with too little feed-in history to net a year against.
    projected_year_cost_eur: float | None = None
    # The basis behind that number, as strings plus a few figures: which legs
    # are measured, which are held flat, and how many days are still ahead. A
    # projection carries more assumptions than the running bill does, so it
    # ships the means to audit it.
    projection_diagnostics: dict[str, Any] | None = None


def ytd_window_reset(entry: ConfigEntry, when: datetime | None = None) -> datetime:
    """Local midnight of the day ``current_year_cost`` accumulates from.

    The datetime form of :func:`cohort.ytd_window_start`, and a cross-file
    invariant rather than a convenience: ``_seed_short_term_sum`` must hand
    the recorder the SAME instant the sensor publishes as ``last_reset``, or
    the cost compiler takes the meter-reset branch and adds the whole window's
    reading on top of the resumed sum. Both sides call this, which is what
    keeps them from drifting apart.

    Local 1 January 00:00 for every entry that has not opted into billing from
    its contract start date, which is all of them by default. Deliberately a
    function, never a module-level constant: a Home Assistant process that
    stays up across midnight on 31 December would otherwise keep reporting
    last year's anchor.
    """
    now = when or dt_util.now()
    start = ytd_window_start(entry, now.date())
    return now.replace(
        month=start.month, day=start.day, hour=0, minute=0, second=0, microsecond=0
    )


def month_window_start(entry: ConfigEntry, today: date | None = None) -> date:
    """First day ``current_month_cost`` accumulates from.

    The 1st of the running month, except on an entry that bills its
    year-to-date from its contract start date and signed part-way through this
    one: then it is the start date. Billing the whole month there would charge
    the days before the contract existed, which on a 15 March signing is a
    fortnight of energy and standing charge the household never owed. The
    yearly window already refuses those days; the monthly one has to agree with
    it, or the month is not a slice of the year it sits in.
    """
    day = today or dt_util.now().date()
    return max(day.replace(day=1), ytd_window_start(entry, day))


def month_window_reset(entry: ConfigEntry, when: datetime | None = None) -> datetime:
    """Local midnight of the day ``current_month_cost`` accumulates from.

    The datetime form of :func:`month_window_start`, and it inherits the same
    invariant its yearly sibling above spells out: the instant published as
    ``last_reset`` has to be the instant actually billed from, or the
    statistics compiler buckets a period the sensor never accumulated over.
    """
    now = when or dt_util.now()
    start = month_window_start(entry, now.date())
    return now.replace(
        month=start.month, day=start.day, hour=0, minute=0, second=0, microsecond=0
    )
