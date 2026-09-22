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

"""What the comparison reads off a household, and what it calls things.

The volumes, the regime, the load profile the spots are weighted by and the
welcome credit still running, plus the names the registry gives a supplier
and a contract. The flow collects them, the engine prices against them, and
the result page prints them; none of it decides anything on its own.
"""

from __future__ import annotations

from .compare_quote import _row_label
from .const import CONF_ANNUAL_CONSUMPTION_KWH
from .const import CONF_CONTRACT
from .const import CONF_DSO_TARIFF_MODE
from .const import CONF_METER
from .const import CONF_QUARTER_HOURLY
from .const import CONF_SOLAR_KVA
from .const import CONF_SOLAR_REGIME
from .const import CONF_SUPPLIER
from .const import DSO_MODE_BI_HORAIRE
from .const import METER_MONO
from .const import SOLAR_REGIME_NONE
from .injection import _injection_needs_spot
from .providers import effective_kind
from .providers import get as get_extractor
from .providers import offers_quarter_hourly
from .providers._rates import SpotMonthlyRates
from .providers.base import SupplierSnapshot
from .snapshot_resolve import entry_annual_kwh
from .spot_stats import _energy_is_rlp_indexed
from .spot_stats import _injection_is_spp_indexed
from .spot_stats import _injection_on_month_mean
from .spot_stats import _rlp_blend_for
from .spot_stats import _spp_weighting_enabled
from .synergrid import RlpWeights
from .synergrid import SppWeights
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from datetime import timedelta
from homeassistant.config_entries import ConfigEntry
from typing import Any
from typing import cast
from collections.abc import Iterator
from contextlib import contextmanager


def _label_for_supplier(supplier_id: str) -> str:
    try:
        return get_extractor(supplier_id).label
    except Exception:  # noqa: BLE001 - stale id
        return supplier_id


def _label_for_contract(supplier_id: str, contract_id: str) -> str:
    try:
        for c in get_extractor(supplier_id).contracts:
            if c.id == contract_id:
                return c.label
    except Exception:  # noqa: BLE001 - stale id
        pass
    return contract_id


def _settlement_of(data: Mapping[str, Any]) -> bool:
    """The settlement answer held in one side's config data.

    Gated on that side's OWN contract, so a value left behind by an earlier
    pick, or carried in from the household when the target is a different
    product, can never move a kind it does not belong to.
    """
    if not offers_quarter_hourly(data.get(CONF_SUPPLIER), data.get(CONF_CONTRACT)):
        return False
    return bool(data.get(CONF_QUARTER_HOURLY, False))


def _candidate_label(supplier_id: str, contract_id: str, quarter_hourly: bool) -> str:
    """One ranking row's name, settlement included where it separates rows.

    A card whose settlement changes its KIND is two rows off one document, and
    they are two different bills. Without the marker they would render
    identically, collide in the label -> candidate map the year-to-date pass
    reads back through, and leave the user unable to tell which row is which.

    Marked on exactly the condition ``_sweep_candidates`` expands on, so a
    marker always distinguishes two rows that can appear together. Frank's
    settlement leaves the kind alone and is never expanded, and its rows are
    all priced on the hourly grid the annual figure uses anyway, so marking
    one would advertise a difference this column does not carry.
    """
    label = _label_for_contract(supplier_id, contract_id)
    if quarter_hourly and effective_kind(
        supplier_id, contract_id, quarter_hourly=True
    ) != effective_kind(supplier_id, contract_id):
        label = f"{label} (quarter-hourly)"
    return _row_label(_label_for_supplier(supplier_id), label)


@dataclass(frozen=True)
class _QuoteEntry:
    """Read-only stand-in for the ConfigEntry, carrying a what-if regime.

    Every helper the quote reaches (the annual bill, the fee legs, the
    injection price, the year-to-date walk) reads the solar regime off
    ``entry.data`` and touches nothing else on the entry, so swapping the
    mapping is enough to price a what-if.

    The alternative, threading a regime override down as a parameter,
    would change signatures the live coordinator and the backfill share.
    A defaulted override falling back to ``entry.data`` in one of those
    three paths and not the others is exactly how the cost legs have
    drifted apart before, and none of that risk buys anything here: the
    compare branch is the only caller that needs a hypothetical regime.

    Purpose-built rather than a copy of the real entry: HA's ConfigEntry
    refuses to rebind ``data``, and a copy would carry the same entry id
    into anything that later looked at it.
    """

    data: Mapping[str, Any]


def _coordinator_rlp_weights(entry: ConfigEntry) -> RlpWeights | None:
    """The RLP profile the entry's coordinator holds, or ``None``.

    The year-to-date figures on the compare page have to allocate a
    compensation entry's net and weight an RLP-indexed leg exactly as the
    sensor beside them does, so they read the same profile. Never downloaded
    from the dialog."""
    coord = getattr(entry, "runtime_data", None)
    weights = getattr(coord, "_rlp_weights", None)
    return weights or None


def _coordinator_rlp_index_weights(
    entry: ConfigEntry, snapshot: SupplierSnapshot | None
) -> RlpWeights | None:
    """The RLP curve the quoted card's own index names, or ``None``.

    Its sibling above carries the household's load shape, which is what a
    compensation net is spread over and a bi-hourly day split by whichever card
    is being priced. This one carries the INDEX, and that belongs to the card:
    Eneco's Belpex-RLP-M is the equal mean of the three regional curves,
    energie.be's Belpex_RLP the column-weighted one, Energy Knights' and
    Trevion's the Fluvius curve alone. All three sit in one Flanders ranking, so
    reading the entry's own for every row prices most of them on an index their
    card never mentions.

    ``None`` for a card that names no RLP index, and for one whose blend this
    process has not loaded; the walk then keeps the plain arithmetic mean, the
    same fallback an entry with no profile at all gets. Never downloaded from
    the dialog: the coordinator reduces every blend from the one workbook read
    it already performs.

    Reads the leg with ``getattr`` rather than an attribute, like its sibling
    reads the coordinator: the caller wraps the whole year-to-date in
    ``suppress(Exception)``, so anything raised here does not surface as a fault
    but as the entire column quietly going blank.
    """
    energy = getattr(snapshot, "energy", None)
    if not _energy_is_rlp_indexed(energy):
        return None
    coord = getattr(entry, "runtime_data", None)
    getter = getattr(coord, "rlp_weights_for_blend", None)
    if getter is None:
        return None
    weights: RlpWeights | None = getter(_rlp_blend_for(energy))
    return weights or None


def _coordinator_spp_weights(
    entry: ConfigEntry, snapshot: SupplierSnapshot | None, *, own: bool
) -> SppWeights | None:
    """The Synergrid solar profile the entry's coordinator holds, or ``None``.

    Its sibling above carries the load profile; this one carries the solar
    weighting an SPP-indexed feed-in formula settles on. The year-to-date
    column has to resolve that credit exactly as the sensor beside it does,
    and it was the one input never passed: with no profile
    ``_spp_injection_spot`` is strict and answers nothing, so the walk
    credited the card's printed indicative and the household's own row
    contradicted its own current_year_cost.

    ``own`` splits the gate the same way ``_credit_month_spot_for`` does, and for the
    same reason: the entry-side opt-in belongs to the side it was made on,
    while a foreign card is judged only by what it prints. Handing a target's
    formula a weighting its card never names inverts the credit.

    Never downloaded from the dialog; a household whose own contract does not
    want the profile simply has none, and the walk keeps the printed figure as
    it always did.
    """
    if not (
        _spp_weighting_enabled(entry, snapshot)
        if own
        else _injection_is_spp_indexed(snapshot)
    ):
        return None
    coord = getattr(entry, "runtime_data", None)
    weights = getattr(coord, "_spp_weights", None)
    return weights or None


def _credit_index_for(
    entry: ConfigEntry,
    snapshot: SupplierSnapshot | None,
    *,
    own: bool,
    raw: SupplierSnapshot | None = None,
) -> str | None:
    """Which delivery-month index this side's feed-in credit settles on.

    ``"spp"`` for the solar-weighted mean, ``"plain"`` for the arithmetic one,
    ``None`` when the credit settles on something that is not a month at all
    and must keep its per-slot rate.

    Three shapes reach ``"plain"`` or ``None`` and the distinction is not
    obvious from the leg alone:

    - a card that names the month itself (Eneco's Belpex-injectie, the EPEXDAM
      cards) carries ``month_indexed`` and is plain.
    - a card that names no index because its ENERGY already does (the expert
      custom monthly contract's formula injection) carries no flag at all,
      and is still plain. Reading only the flag dropped it onto the two-day
      day-ahead window mean, which is not what such a contract bills and moves
      with the day the dialog was opened.
    - Cociter Tarif Variable indexes its two legs on different periods,
      consumption monthly and injection per hour, and a signing cohort splices
      a month-priced energy leg onto it. That one is ``None``, which is why
      the question is asked of the RAW card: on the spliced one it would look
      exactly like the second shape.

    The SPP half is per side. The entry's own opt-in belongs to the side it was
    made on; a contract that is not this entry's is judged only by what its
    card prints, or a custom monthly entry that ticked the box would re-price
    every candidate's formula onto an index its card never names.
    """
    spp = (
        _spp_weighting_enabled(entry, snapshot)
        if own
        else _injection_is_spp_indexed(snapshot)
    )
    if spp:
        return "spp"
    if _injection_on_month_mean(raw if raw is not None else snapshot):
        return "plain"
    return None


def _needs_month_mean(snapshot: SupplierSnapshot | None) -> bool:
    """True when this side's energy bills the delivery month's mean spot.

    A ``SpotMonthlyRates`` leg is flat for the whole month, so quoting it at a
    day-ahead window mean is not an approximation of what it bills, it is a
    different number - and one that moves day to day while the contract's does
    not.
    """
    return snapshot is not None and isinstance(snapshot.energy, SpotMonthlyRates)


def _effective_regime(current: Mapping[str, Any], compare: Mapping[str, Any]) -> str:
    """The solar regime this quote prices on: the what-if pick when the
    compare_solar step ran, else the entry's own."""
    stored = current.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE)
    return str(compare.get(CONF_SOLAR_REGIME, stored))


def _months_billed(start: date, today: date) -> float:
    """Months' worth of a MONTHLY fee billed over ``[start, today]``.

    Each month contributes the fraction of itself that falls inside the
    window, which is what ``_ytd_prosumer`` and ``_ytd_capacity`` sum per
    month, so one number serves the compare page's prosumer and capacity legs
    alike. Counting whole elapsed months instead was equivalent while every
    window began on 1 January; it is not once a window can begin mid-month.
    """
    total = 0.0
    cur = start
    while cur <= today:
        first = date(cur.year, cur.month, 1)
        next_first = (
            date(cur.year + 1, 1, 1)
            if cur.month == 12
            else date(cur.year, cur.month + 1, 1)
        )
        billed_to = min(next_first - timedelta(days=1), today)
        total += ((billed_to - cur).days + 1) / (next_first - first).days
        cur = next_first
    return total


def _quote_entry(
    entry: ConfigEntry,
    regime: str,
    dso_mode: str | None = None,
    *,
    quarter_hourly: bool | None = None,
    meter: str | None,
) -> ConfigEntry:
    """``entry`` itself when the what-if matches it, else a proxy holding
    the overridden regime, DSO tariff mode and settlement.

    Returning the real entry unchanged on the common path keeps every
    quote that does not use the what-if on exactly the code it ran
    before, proxy included.

    ``dso_mode`` is the target side's billing configuration, which is not
    always the household's: a Tarif Impact product is only sold on the
    incitative one. It rides the proxy rather than a parameter for the same
    reason the regime does, and it reaches further, because the fee leg and
    the year-to-date engine both read it straight off ``entry.data``.

    ``quarter_hourly`` is the TARGET's settlement, and it has to be stated
    rather than inherited. ``_resolve_snapshot`` reads the answer off the
    entry it is handed, so a proxy carrying the household's own would settle
    a Bolt card per quarter-hour because the user happens to be on Frank's
    quarter-hourly tariff. ``None`` leaves the household's answer in place,
    which is what the own side wants.

    ``meter`` has no default, so every call site has to name it. Its sibling
    ``regime`` is positional-and-required and mypy refuses a site that drops
    it; this one shipped defaulted, and a site that stopped passing it would
    have reverted the target to the household's meter in silence, which is the
    exact defect the argument was added to fix. Mutation found the asymmetry:
    removing ``regime=`` failed type checking, removing ``meter=`` left 155
    tests green. ``None`` still means "the household's own", and the own side
    now says that out loud.

    ``meter`` is the TARGET's, for the same reason and with the same reach: a
    dynamic or TOU product is only sold on a digital meter, and ``_target_side``
    already prices the rate and the volume on that one. Two resolvers read the
    meter straight off ``entry.data`` instead, so the credit a card denies an
    exclusive-night connection and the volume tranche an energie.be card bands
    were both settled on the HOUSEHOLD's meter while everything else on the row
    used the target's. ``None`` leaves the household's answer in place, which
    is what the own side wants.

    Direct debit is deliberately NOT in that list and rides along inherited.
    It is the one of these that is a fact about the household rather than
    about the product: someone who pays by direct debit would still do so at
    the supplier being quoted. ``_resolve_snapshot`` gates it on the TARGET
    card's own registry flag, so it reaches the reduction only where that
    card states one and is identity everywhere else.
    """
    overrides: dict[str, Any] = {}
    if regime != entry.data.get(CONF_SOLAR_REGIME, SOLAR_REGIME_NONE):
        overrides[CONF_SOLAR_REGIME] = regime
    if dso_mode is not None and dso_mode != entry.data.get(
        CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE
    ):
        overrides[CONF_DSO_TARIFF_MODE] = dso_mode
    if quarter_hourly is not None and quarter_hourly != bool(
        entry.data.get(CONF_QUARTER_HOURLY, False)
    ):
        overrides[CONF_QUARTER_HOURLY] = quarter_hourly
    if meter is not None and meter != entry.data.get(CONF_METER, METER_MONO):
        overrides[CONF_METER] = meter
    if not overrides:
        return entry
    # The yearly volume is the one site fact that does NOT live in entry.data:
    # entry_annual_kwh prefers the coordinator's measured figure, and a proxy
    # carries no coordinator. Freezing the resolved answer into the mapping is
    # what keeps a what-if row splitting a volume tranche and measuring the
    # network ceiling against the same volume as the row above it, instead of
    # silently dropping back to the 3.500 kWh default.
    #
    # It lands under the TYPED key, which is the mapping's own vocabulary for
    # "this entry states this volume" and true of a what-if by construction.
    # The one thing that must not read a proxy is ``_annual_volume``: it treats
    # that key as a figure the user typed and would label a measured or default
    # volume "entered on the entry". Both of its call sites pass the real entry.
    overrides[CONF_ANNUAL_CONSUMPTION_KWH] = entry_annual_kwh(entry)
    # Only entry.data is ever read through this (audited across the quote,
    # fee, injection and year-to-date helpers), so the mapping is a
    # complete stand-in; the cast is what tells mypy that.
    return cast(ConfigEntry, _QuoteEntry({**entry.data, **overrides}))


def _needs_missing_spots(
    snapshot: SupplierSnapshot,
    entry: ConfigEntry,
    spots: Mapping[datetime, float],
) -> bool:
    """Whether this card's feed-in needs spots the caller does not have.

    A static-energy card whose INJECTION is spot-indexed is not caught by
    the contract-kind test, which describes the energy leg only. The
    year-to-date engine drops such a credit whole rather than approximating
    it (``_ytd_spot_injection_credit`` returns 0.0 on an empty cache), so a
    row priced without spots is a solar household's bill with no solar in
    it, printed beside rows that have theirs.

    Judged on the SNAPSHOT's own energy kind through
    ``_injection_needs_spot``, never on the effective one: a
    cohort-respliced contract whose injection genuinely is month-indexed
    would otherwise be swept in and lose a figure it could have carried.
    """
    return not spots and _injection_needs_spot(snapshot, entry)


def _kva(data: Mapping[str, Any]) -> float:
    """Configured inverter capacity, 0.0 when unset or unparseable."""
    try:
        return float(data.get(CONF_SOLAR_KVA, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class _HouseholdQuote:
    """The half of a quote that belongs to the household rather than to any
    contract it is being compared against.

    Wide on purpose: these are the values the compare arithmetic reads, and
    naming them here is what lets a ranking resolve them once for a whole
    cell instead of once per row. Three of the fields are callables closed
    over the rest: the spot, SPP and export-rate resolvers, because each
    memoises a fetch that must happen at most once per page.
    """

    region: str
    dso: str
    current_meter: str
    dso_mode: str
    peak_kw: float
    stored_regime: str
    regime: str
    quote_entry: Any
    overridden: bool
    now_utc: datetime
    today_local: date
    # First day the year-to-date covers: 1 January, or the contract start
    # date on an entry that bills its year-to-date from there.
    ytd_from: date
    fee_proration: float
    month_proration: float
    spot_dict: dict[datetime, float]
    current_kind: str
    avg_spot: float | None
    compare_spot_injection: bool
    ytd_kwh: float | None
    rolling_inj_kwh: float
    ytd_inj_kwh: float
    annual_kwh: float
    volumes_typed: bool
    placeholders: dict[str, str]
    current_snapshot: Any
    raw_snapshot: Any
    baseline_snapshot: Any
    hour_weights: Any
    inj_hour_weights: Any
    # (day, night) of consumption and of export, for the per-register
    # clamp a reversing meter is billed on. Computed once because every
    # annual row on this page needs the same pair.
    register_weights: Any
    current_per_kwh: float | None
    current_export_per_kwh: float | None
    # The card the household's welcome credit is read off (the signing
    # month's where the supplier keeps an archive), and what is left of that
    # credit over the coming year. A candidate's is priced per row instead,
    # since it depends on the card being ranked.
    signing_snapshot: Any
    own_welcome_credit: float
    spot_for: Any
    credit_month_spot_for: Any
    export_rate_for: Any


@contextmanager
def _borrowed_spot_cache(coord: Any, *, isolate: bool) -> Iterator[None]:
    """Put the coordinator's spot caches back after a compare-only fetch.

    The compare page borrows ``_ensure_historical_spots`` to price a target,
    and the next tick persists whatever that leaves behind
    (``_save_persistent``). A household with no stored key can otherwise seed
    its own persistent cache by opening this dialog and typing one, and then
    never refresh it, so a partial month mean gets baked over the card's
    printed indicative for the rest of the month.

    Three attributes are saved, not two. A day listed in
    ``_complete_spot_days`` is treated as fully present without consulting
    the hour dict at all, so it has to travel with them: emptying the dicts
    alone leaves the fetch believing every day the coordinator has already
    walked is covered, and it returns without fetching anything.

    Copied and restored in place rather than rebound, because
    ``_ensure_historical_spots`` merges each chunk into the attribute and
    re-resolves it after every await.

    ``isolate`` empties the caches first, for a caller that wants only the
    hours it fetched itself; without it the fetch merges into what is
    already there, which is what a month mean wants.
    """
    saved_spots = dict(coord._historical_spots)
    saved_quarters = dict(coord._historical_spot_quarters)
    saved_complete = set(coord._complete_spot_days)
    if isolate:
        coord._historical_spots.clear()
        coord._historical_spot_quarters.clear()
        coord._complete_spot_days.clear()
    try:
        yield
    finally:
        coord._historical_spots.clear()
        coord._historical_spots.update(saved_spots)
        coord._historical_spot_quarters.clear()
        coord._historical_spot_quarters.update(saved_quarters)
        coord._complete_spot_days.clear()
        coord._complete_spot_days.update(saved_complete)
