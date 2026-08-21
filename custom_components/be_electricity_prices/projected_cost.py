# Copyright (c) 2026 Renaud Allard
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""Projected full-calendar-year cost.

``current_year_cost`` answers "what has this year cost so far". This module
answers "roughly what does a year on this contract cost", which is the figure a
user can set beside a supplier's monthly advance without a translation step.

It is an indication and never a forecast. A full year is priced at today's
tariffs against a yearly volume taken from the user's own metered history.
Tariffs move during the year and consumption does not repeat exactly, so the
number will not match a settlement. Every leg publishes its basis as an
attribute so the figure can be interrogated rather than trusted.

It is deliberately a STANDALONE full-year estimate rather than the running bill
plus a priced remainder. The blend reads better in principle, and is wrong in
practice in three ways that a review demonstrated with numbers:

- when no meter is wired, ``current_year_cost`` is the fees-only floor while
  the remainder is priced off the household default, so the projection decays
  from a plausible figure in January to the bare fees floor in December;
- an entry whose recorder history starts mid-year has the same asymmetry in
  weaker form, and reads about a third of the real annual cost;
- under the Walloon compensation regime the zero-floor clamp would apply
  separately to each leg, so a banked summer surplus is forfeited against the
  remaining months and the projection can never fall below the running bill.

Pricing the whole year in one pass fixes all three: one volume basis, one set
of tariffs, and the compensation net clamped exactly once. It also makes the
standing charge consistent, since a sensor that says "at today's tariffs" and
then bills half the year off an archived card would contradict itself.

What is refused is a leg carried as a FORMULA over an index nobody has yet:
DynamicRates and SpotMonthlyRates. ENTSO-E publishes day-ahead only and there
is no free forward curve, so those report no value and say why.

The test is the shape the rate is stored in, not whether the product is called
indexed. A Variable or Impact card is monthly-indexed in the market sense, but
its extractor has already RESOLVED this month's rate, and holding a resolved
rate flat is the same assumption the whole sensor rests on. A contract start
date can move a card across that line: the signing-cohort splice rewrites a
Variable card with parsed coefficients into SpotMonthlyRates, which genuinely
is a formula over a future index, so such an entry reports no value and the
basis says the cohort re-price is why.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CONTRACT_END_DATE,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    DSO_MODE_BI_HORAIRE,
    MEASURED_FULL_YEAR_DAYS,
    METER_MONO,
    SOLAR_REGIME_COMPENSATION,
    SOLAR_REGIME_INJECTION,
    SOLAR_REGIME_NONE,
)
from .providers.base import DynamicRates, SpotMonthlyRates, SupplierSnapshot

_SPOT_BASIS = (
    "not projected: this contract settles on a Belpex index for months that "
    "have not happened yet, and no forward price exists"
)
_NO_INJECTION_YEAR = (
    "not folded in: needs a full year of feed-in history, and a partial one "
    "cannot be scaled without a production profile"
)
_UNNETTABLE = (
    "not projected: this meter is netted against its own feed-in, and there is "
    "not enough feed-in history to net a full year against"
)
_NO_INJECTION_RATE = (
    "measured, but not credited: this card indexes its feed-in on the spot "
    "price, which a projection has no forward value for"
)
_NO_INJECTION_CARD = "measured, but not credited: this card publishes no feed-in tariff"
_COHORT_SPOT_BASIS = (
    "not projected: the contract start date re-prices this card to its signing "
    "cohort, which settles on a monthly Belpex index that does not exist yet"
)


def _contract_basis(entry: ConfigEntry, today: date) -> str:
    """How much of the projected year the current contract actually covers.

    The projection holds today's rate for a full year. When a contract end
    date falls inside that year, the months after it are priced on a card the
    user will not be on, at whatever they renew to. The figure stays as it is,
    because nothing better exists to price those months with, but saying so is
    the difference between an estimate and a claim.

    The end date is optional and independent of the start date, and it was
    collected as an inert renewal reminder, so some stored values are
    approximate. Every path here degrades to "no end date set" rather than
    refusing to produce a number.
    """
    from .cohort import _parse_iso_date

    end = _parse_iso_date(entry.data.get(CONF_CONTRACT_END_DATE))
    if end is None:
        return "today's contract, no end date set"
    if end <= today:
        # Already expired. The entry is stale rather than wrong, and the card
        # it still points at is the only rate available, so behave exactly as
        # if no date were set and say which.
        return f"today's contract, whose recorded end date ({end}) has passed"
    horizon = date(today.year, 12, 31)
    if end >= horizon:
        return f"today's contract, which runs past this year (ends {end})"
    covered = (end - today).days
    total = (horizon - today).days
    pct = round(100.0 * covered / total) if total else 100
    return (
        f"today's contract for {covered} of the {total} days left this year "
        f"({pct}%), ends {end}; the rest is priced on a contract you have not "
        "signed yet"
    )


async def _compute_projected_year_cost(
    hass: HomeAssistant,
    entry: ConfigEntry,
    snapshot: SupplierSnapshot,
    priced: SupplierSnapshot,
    *,
    billed_peak_kw: float,
    today: date,
    breakdown: dict[str, Any] | None = None,
) -> float | None:
    """Cost of a full year on this contract at today's tariffs, or ``None``.

    Two snapshots, and swapping them is a real bug rather than a style choice.
    ``snapshot`` is the card as resolved, which is what the fee helpers read.
    ``priced`` is the cohort-spliced one, whose energy leg is what the live
    sensors actually bill, so a signing-cohort entry takes its per-kWh rate
    from there and not from the card a new customer would get today.

    Returns ``None`` when the contract cannot be projected or the rate cannot
    be resolved. It never raises: the caller runs inside the coordinator tick,
    where an exception would mark the whole update failed and take every
    entity on the device unavailable.
    """
    from .compare_quote import (
        _annual_bill,
        _annual_volume,
        _compare_injection_credit,
        _covers_a_year,
        _tou_weighted_per_kwh,
    )
    from .energy_meters import _measured_kwh

    if breakdown is None:
        breakdown = {}

    if isinstance(priced.energy, (DynamicRates, SpotMonthlyRates)):
        # A card the user's own contract does not settle on: blaming a Belpex
        # index reads as wrong to someone holding a Variable card, so say when
        # the signing-cohort splice is what put them on that axis.
        spliced = isinstance(priced.energy, SpotMonthlyRates) and not isinstance(
            snapshot.energy, SpotMonthlyRates
        )
        breakdown["energy_basis"] = _COHORT_SPOT_BASIS if spliced else _SPOT_BASIS
        return None

    dso = entry.data[CONF_DSO]
    region = entry.data[CONF_REGION]
    meter = entry.data.get(CONF_METER, METER_MONO)
    dso_mode = entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE)

    # Fixed, Variable, TOU and Impact all ignore the spot argument, so the
    # projection needs no spot data at all and cannot perturb the live price
    # table or the spot cache.
    per_kwh = _tou_weighted_per_kwh(
        priced, dso, region, dt_util.now(), None, meter, dso_mode
    )
    if per_kwh is None:
        return None

    trailing_start = today - timedelta(days=MEASURED_FULL_YEAR_DAYS - 1)
    annual = await _annual_volume(hass, entry, trailing_start, today)

    # Feed-in counts only against a full trailing year of it, gated on the
    # INJECTION side's own coverage. Reading the consumption side's would
    # credit a 45-day measurement as a year for a household that wired panels
    # part-way through a metered year. Below that there is nothing honest to
    # put here: PV is far more seasonal than consumption, so scaling a partial
    # window by a day count would credit winter output at summer rates.
    annual_inj = 0.0
    injection_basis = "not applicable"
    inj_rate: float | None = None
    regime = entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    if regime != SOLAR_REGIME_NONE:
        measured_inj = await _measured_kwh(
            hass, entry, trailing_start, today, side="injection"
        )
        # Time-weighted, not the tick hour's slot rate. The live scalar swings
        # with the clock on a per-slot TOU credit and on a spot-indexed one,
        # which made this sensor step by hundreds of euro between consecutive
        # ticks. Passing no spot leaves a spot-indexed credit unresolved, which
        # is the honest outcome for a figure carrying no forward price.
        inj_rate = _compare_injection_credit(priced, entry, {}, None)
        if _covers_a_year(measured_inj.days_with_data) and measured_inj.kwh > 0:
            # Scaled across any missing days, the same way the consumption leg
            # is. Demanding all 365 made one absent bucket drop the whole leg,
            # which under the netting regime stopped the meter being netted at
            # all and multiplied the bill by ten.
            annual_inj = (
                measured_inj.kwh * MEASURED_FULL_YEAR_DAYS / measured_inj.days_with_data
            )
            injection_basis = f"measured ({measured_inj.days_with_data} days)"
            if regime == SOLAR_REGIME_INJECTION and inj_rate is None:
                # _annual_bill's injection branch needs a rate; without one it
                # bills gross. Say that, rather than claiming a credit that
                # was never applied, and say WHICH: a card with no feed-in
                # tariff at all is a different fact from one whose feed-in is
                # spot-indexed and therefore unpriceable here.
                injection_basis = (
                    _NO_INJECTION_CARD
                    if getattr(priced, "injection", None) is None
                    else _NO_INJECTION_RATE
                )
        elif regime == SOLAR_REGIME_COMPENSATION:
            # Under netting there is no small error available. Billing the year
            # gross because the feed-in window is short is not a missing credit,
            # it is the wrong bill: measured at ten times the netted figure. A
            # partial window cannot be scaled either, because PV is seasonal
            # enough that a summer sample nets the year to the zero clamp. So
            # refuse, the way a spot-priced contract does.
            breakdown["injection_basis"] = _UNNETTABLE
            breakdown["energy_basis"] = _UNNETTABLE
            return None
        else:
            injection_basis = _NO_INJECTION_YEAR

    projected = _annual_bill(
        snapshot,
        entry,
        billed_peak_kw,
        per_kwh,
        annual.kwh,
        annual_inj,
        inj_rate,
        meter=meter,
    )

    breakdown["energy_basis"] = "today's published rate, held for a full year"
    breakdown["contract_basis"] = _contract_basis(entry, today)
    breakdown["fee_basis"] = "today's network tariffs, taxes and fees, held for a year"
    breakdown["volume_basis"] = annual.source
    breakdown["injection_basis"] = injection_basis
    breakdown["annual_kwh"] = annual.kwh
    breakdown["annual_injection_kwh"] = annual_inj
    return projected
