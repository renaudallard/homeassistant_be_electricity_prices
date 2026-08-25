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

"""Injection (feed-in) pricing.

Split out of coordinator.py. The most shape-sensitive region in the package:
three injection taxonomies (a printed flat indicative, a spot formula, a
per-slot TOU triplet), a monthly-versus-hourly distinction, an optional
zero-floor clamp, and the intraday gate that decides whether the display array
varies at all."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import datetime
from homeassistant.config_entries import ConfigEntry
from homeassistant.util import dt as dt_util
from statistics import fmean

from .const import (
    CONF_SOLAR_REGIME,
    SOLAR_REGIME_INJECTION,
)
from .pricing import (
    tou_slot,
)
from .providers.base import (
    DynamicRates,
    EnergyRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierSnapshot,
    TimeOfUseRates,
)
from .spot_stats import (
    _energy_is_quarter_hourly,
    _now_slot_spot,
)


def _bake_monthly_injection(
    snapshot: SupplierSnapshot, mean: float | None
) -> SupplierSnapshot:
    """Turn a mean-indexed injection formula into this month's flat indicative.

    A spot-monthly contract's injection is indexed to a monthly mean, not the
    live hourly spot. Baking the formula into ``current`` routes it through the
    monthly-indicative injection path; the floor (if any) is applied there.
    A flat ``current`` injection or none is returned unchanged.

    WHICH mean is the caller's business, and it is not always the energy leg's:
    the Mega groepsaankoop indexes both legs on the same one, while energie.be
    Variabel prices consumption on Belpex_RLP and injection on the
    solar-weighted Belpex_SPP. Pass the mean the injection formula names.

    ``mean=None`` wipes the leg (no credit yet) rather than leaving factor/base
    standing, which ``_injection_is_spot_formula`` would then read as "price
    this per hour". Skip the call entirely only for a card that has a printed
    ``current`` to fall back to.
    """
    inj = snapshot.injection
    if inj is None or inj.factor is None or inj.base is None:
        return snapshot
    current = None if mean is None else inj.factor * mean + inj.base
    return replace(
        snapshot,
        injection=replace(inj, current=current, factor=None, base=None),
    )


def _injection_needs_spot(snapshot: SupplierSnapshot, entry: ConfigEntry) -> bool:
    """True when pricing this entry's injection requires an ENTSO-E spot
    even though the ENERGY contract isn't dynamic.

    The case is a static-energy card (Fixed / Variable / TOU) whose
    injection is a per-hour spot formula (``factor``/``base``) with no
    printed monthly indicative (``current is None``): Cociter Variable.
    Such a card doesn't fetch ENTSO-E spots through the DynamicRates
    energy path, so the coordinator must fetch spots for it too (and the
    config flow must collect an API key) to credit the injection.
    DynamicRates contracts already fetch spots via the energy
    path and are excluded here. Only relevant on the injection regime.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    inj = snapshot.injection
    return (
        inj is not None
        and inj.current is None
        and inj.factor is not None
        and inj.base is not None
        and not isinstance(snapshot.energy, DynamicRates)
    )


def _injection_needs_month_spot(snapshot: SupplierSnapshot, entry: ConfigEntry) -> bool:
    """True when the injection is indexed on a MONTHLY mean that nothing
    else on this entry fetches spots for.

    energie.be Vast is the shape: a flat energy rate whose feed-in credit is
    the monthly Belpex_SPP formula ("de terugleveringsvergoeding wordt
    geindexeerd op basis van de Belpex_SPP parameter"). The energy leg needs
    no spot, so without this the credit would sit at the card's printed
    indicative forever, and that indicative is the formula on the VNR
    FORECAST rather than the realized month.

    Deliberately NOT folded into ``_injection_needs_spot``: that predicate
    means a PER-HOUR index, and ``_injection_hourly_on_cohort`` reads it to
    conclude the injection keeps its own hourly formula. Widening it would
    make this monthly shape look hourly and skip the month-mean bake, which
    is the opposite of what the card says. Contracts whose energy leg
    already fetches spots are excluded here for the same reason: they resolve
    through the energy path.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    inj = snapshot.injection
    return (
        inj is not None
        and (inj.spp_indexed or inj.month_indexed)
        and inj.factor is not None
        and inj.base is not None
        and not isinstance(snapshot.energy, (DynamicRates, SpotMonthlyRates))
    )


def _injection_hourly_on_cohort(snapshot: SupplierSnapshot, entry: ConfigEntry) -> bool:
    """True when this entry's injection keeps a PER-HOUR spot index even though
    its energy is being priced on a monthly mean.

    That happens only through a signing-cohort re-price: a variable card whose
    ENERGY is monthly-indexed gets a SpotMonthlyRates leg spliced on, while its
    injection formula is untouched. Cociter Tarif Variable is the one such card
    (note (7) "indexe mensuellement ... (BELIX)" for consumption against note
    (9) "le prix de l'injection varie chaque heure").

    A card that is ITSELF monthly-indexed (the custom monthly contract, the
    Mega groepsaankoop) indexes its injection on the month too, so it must keep
    the month mean - and the SPP weighting when the card or the entry calls for
    it. That is why the snapshot's own energy kind, not the effective one,
    decides.
    """
    return _injection_needs_spot(snapshot, entry) and not isinstance(
        snapshot.energy, SpotMonthlyRates
    )


def _tou_injection_rate(
    inj: InjectionRates, energy: EnergyRates, when: datetime
) -> float | None:
    """Per-slot injection rate for a time-of-use contract whose feed-in
    tariff varies by slot (Engie Empower Flextime).

    Returns ``None`` when the contract isn't TOU or its injection is a
    single rate (``peak`` unset), so the caller falls back to the normal
    current / factor+base path. Uses the energy contract's own
    ``weekend_rule`` so injection and consumption agree on the slot for a
    given hour.
    """
    if not isinstance(energy, TimeOfUseRates) or inj.peak is None:
        return None
    slot = tou_slot(when, energy.weekend_rule)
    if slot == "peak":
        return inj.peak
    if slot == "transition":
        return inj.transition
    return inj.offpeak


def _floor_injection(rate: float | None, inj: InjectionRates) -> float | None:
    """Clamp an injection rate at 0 when the contract forbids negatives
    (``floor_at_zero``). A ``None`` rate (no data) passes through unchanged."""
    if rate is None or not inj.floor_at_zero:
        return rate
    return max(rate, 0.0)


def _injection_is_spot_formula(inj: InjectionRates, energy: EnergyRates) -> bool:
    """True when this injection leg prices off the spot rather than a printed
    indicative: it has both coefficients, and either the energy is dynamic or
    the card publishes no flat ``current`` to prefer.

    Written out twice, once where it decides the rate and once where it
    decides whether the display array varies intraday. A drift between them
    mis-gates the array against the billed value.
    """
    if inj.month_indexed:
        # Month coefficients are never a per-hour formula, whatever else is
        # true. Without this a card that stopped printing its indicative would
        # flip to pricing the credit at the current slot's spot, which is the
        # 0.6.7 mis-credit and is silent.
        return False
    return (
        inj.factor is not None
        and inj.base is not None
        and (isinstance(energy, DynamicRates) or inj.current is None)
    )


def _injection_needs_spot_quarters(
    snapshot: SupplierSnapshot, entry: ConfigEntry
) -> bool:
    """True when replaying this entry's feed-in credit needs the hour's own
    quarter spots rather than their mean.

    Every pricing formula in the package is linear in the spot, and the mean
    of an hour's quarters prices a linear formula exactly. ``floor_at_zero``
    is the exception: ``max(0, factor * spot + base)`` is convex, so the mean
    of the floored quarters is at least the floored mean and an hour whose
    spot crossed the floor inside it is worth more than flooring its mean
    says. The live array already floors per slot, which is what the contract
    bills, so without the quarters the year-to-date credit and the backfilled
    rows sit below the injection_price sensor the user is watching, always in
    the same direction.

    Only an expert custom entry reaches it: nothing else sets
    ``floor_at_zero``, and the 15-minute grid needs ``quarter_hourly`` energy.
    Quarter-hourly energy is always DynamicRates, so the formula branch is the
    one that fires and neither the TOU triplet nor a month mean can be in
    play. Callers do not have to re-ask those questions.

    Deliberately NOT folded into ``_injection_needs_spot``, which means "this
    injection carries a per-hour index" and is read that way elsewhere.
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return False
    inj = snapshot.injection
    return (
        inj is not None
        and inj.floor_at_zero
        and _energy_is_quarter_hourly(snapshot.energy)
        and _injection_is_spot_formula(inj, snapshot.energy)
    )


def _injection_price_for_slot(
    inj: InjectionRates,
    energy: EnergyRates,
    spot: float | None,
    when: datetime,
) -> float | None:
    """Injection price in EUR/kWh for a single slot.

    The per-slot core shared by the live current-hour scalar and the
    today/tomorrow injection array. Priority (identical to the historical
    walk): a per-slot TOU rate first (Engie Empower Flextime), then the
    spot-indexed formula ``factor*spot + base`` when the contract is
    spot-indexed, otherwise the printed monthly ``current`` indicative.
    ``spot`` is the already-resolved spot for ``when``'s billing slot (None
    when unavailable); the spot branch returns None rather than fabricate a
    value when it has no spot.

    The spot branch fires only when the energy bills per hour (DynamicRates)
    OR the injection is a spot formula with no monthly indicative (``current``
    is None) -- e.g. Cociter Variable. A static-energy contract whose injection
    carries a MONTHLY index but also a printed ``current`` (Ecofix Flexy's
    BELPEX-SPP-M, EBEM Groen Variabel / B@sic+'s SPP0) uses that realized
    monthly rate instead, keeping the live sensor consistent with the YTD
    credit. Do NOT drop this guard: without it a flat monthly-indicative
    credit would flip to a spot-varying one on the several dynamic-injection
    cards that publish BOTH a ``current`` and ``factor``/``base``.
    """
    tou_rate = _tou_injection_rate(inj, energy, when)
    if tou_rate is not None:
        return tou_rate
    if _injection_is_spot_formula(inj, energy):
        if spot is None:
            return None
        # Guaranteed by the predicate; spelled out because a call does not
        # narrow the Optionals the way the inline check used to.
        assert inj.factor is not None and inj.base is not None
        return _floor_injection(inj.factor * spot + inj.base, inj)
    return _floor_injection(inj.current, inj)


def _compute_injection_price(
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    spot_prices: dict[datetime, float],
) -> float | None:
    """Current-hour injection price in EUR/kWh for HA Energy's price entity.

    Only returned when the user is on the injection regime AND the supplier's
    snapshot has injection data. Prefers a per-slot TOU rate (Engie Empower
    Flextime), then the formula+spot when a spot is available (dynamic
    contracts), otherwise falls back to the snapshot's static "current"
    indicative (Eneco Fix/Flex monthly value).
    """
    if entry.data.get(CONF_SOLAR_REGIME) != SOLAR_REGIME_INJECTION:
        return None
    inj = snapshot.injection
    if inj is None:
        return None
    return _injection_price_for_slot(
        inj,
        snapshot.energy,
        _now_slot_spot(snapshot.energy, spot_prices),
        dt_util.now(),
    )


def _injection_varies_intraday(inj: InjectionRates, energy: EnergyRates) -> bool:
    """True when this contract's injection changes across the day -- a TOU
    schedule (Engie Empower Flextime) or a spot-indexed formula (every dynamic
    contract plus Cociter Tarif Variable). Flat monthly-indicative, fixed and
    (mean-baked) spot-monthly injection is constant intra-day, so no per-hour
    array is worth emitting for it. Mirrors the branch conditions of
    ``_injection_price_for_slot``."""
    if isinstance(energy, TimeOfUseRates) and inj.peak is not None:
        return True
    return _injection_is_spot_formula(inj, energy)


def _historical_injection_rate(
    injection: InjectionRates | None,
    spot: float | None = None,
    *,
    quarters: Sequence[float] | None = None,
    energy: EnergyRates | None = None,
    when: datetime | None = None,
) -> float | None:
    """Best-effort EUR/kWh injection rate for a *past* hour.

    Mirrors the live ``_compute_injection_price`` priority: a per-slot TOU
    rate first (Engie Empower Flextime, when ``energy`` + ``when`` are
    given), then the spot-indexed formula ``factor*spot + base`` when both
    the formula and a historical spot are available, falling back to the
    monthly indicative ``current`` otherwise. Several dynamic-injection
    contracts (Engie, OCTA+, TotalEnergies, Luminus, Mega) publish BOTH a
    ``current`` indicative and ``factor``/``base``; checking ``current``
    first made the YTD credit use the flat indicative while the live
    injection-price sensor used the spot formula, so the two user-facing
    numbers diverged. Static contracts have no spot, so they fall through
    to ``current``.

    ``quarters`` are the hour's own slot spots and win over ``spot`` when
    given. Pass them only for an hour that is priced off its own spot, never
    for one billed at a month mean, and only when
    ``_injection_needs_spot_quarters`` holds: for every other entry they are
    absent and this function answers exactly what it always did.
    """
    if injection is None:
        return None
    if quarters:
        # The hour's rate is the mean of its quarters' rates, not the rate of
        # their mean. The two agree for a formula that is linear in the spot
        # and part company for a floored one, which is convex: flooring once,
        # at the hour mean, credits nothing for an hour whose spot crossed the
        # floor inside it. Recursing keeps one description of the priority
        # chain above.
        rates = [
            rate
            for rate in (
                _historical_injection_rate(injection, q, energy=energy, when=when)
                for q in quarters
            )
            if rate is not None
        ]
        return fmean(rates) if rates else None
    if energy is not None and when is not None:
        tou_rate = _tou_injection_rate(injection, energy, when)
        if tou_rate is not None:
            return tou_rate
    if injection.factor is not None and injection.base is not None and spot is not None:
        return _floor_injection(injection.factor * spot + injection.base, injection)
    if injection.current is not None:
        return _floor_injection(injection.current, injection)
    return None
