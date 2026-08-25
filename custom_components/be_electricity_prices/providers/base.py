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

"""Per-supplier extractor protocol and shared dataclasses.

Each supplier exposes a module under ``providers/`` that:

  - declares the contracts it sells (id, label, kind),
  - fetches the *current* tariff card from the supplier's own publication,
  - parses out the energy formula plus the network / tax / capacity
    overlay for every relevant DSO sub-area.

The coordinator picks the configured contract + DSO and feeds the result
into ``pricing.compute_breakdown``.

No EUR values live in Python source - everything in :class:`SupplierSnapshot`
comes from a live fetch.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal, Protocol

import aiohttp

from ..const import REGIONS

TariffKind = Literal[
    "fixed", "variable", "dynamic", "tou", "tou_impact", "spot_monthly"
]

# The three Belgian regions, as a Contract.regions default and for the
# extractors whose every product serves all of them. Public: mega and
# totalenergies each restated it from the REGION_* constants.
ALL_REGIONS: frozenset[str] = frozenset(REGIONS)


@dataclass(frozen=True, kw_only=True)
class Contract:
    """One product sold by a supplier."""

    id: str
    label: str
    kind: TariffKind
    # Regions the product is actually published in. Defaults to all three;
    # extractors override per-contract for products that 404 outside their
    # home region (e.g. TotalEnergies Impact is Wallonia-only).
    regions: frozenset[str] = field(default_factory=lambda: ALL_REGIONS)
    # True when the supplier sells this product to businesses: its card is
    # published excluding VAT and may band the federal excise by annual
    # volume, so the config flow asks for the VAT treatment and the yearly
    # consumption. Nothing else keys off it - a professional contract is
    # otherwise an ordinary contract.
    professional: bool = False
    # True when this (non-dynamic) product's injection is a per-hour spot
    # formula with no printed monthly indicative (Cociter Variable).
    # Pricing the injection then needs an ENTSO-E spot even though the
    # energy is variable, so the config flow offers the API-key step on
    # the injection regime. Dynamic contracts already collect the key via
    # their energy formula and leave this False.
    spot_indexed_injection: bool = False


@dataclass(frozen=True, kw_only=True)
class FixedRates:
    """Fixed energy contract: constant EUR/kWh, optionally bi-hourly.

    ``exclusive_night`` is the rate for a dedicated night-circuit meter.
    The price engine routes the ``exclusive_night`` meter type through it
    (``compute_breakdown`` -> ``energy_eur_per_kwh``), falling back to the
    single rate when it isn't published.
    """

    single: float
    peak: float | None = None
    offpeak: float | None = None
    exclusive_night: float | None = None
    yearly_fixed_fee: float = 0.0
    # Dedicated yearly fixed fee for an exclusive-night meter circuit,
    # billed instead of ``yearly_fixed_fee`` on an exclusive-night config
    # entry when the card prints a separate one. None -> the standard fee
    # applies to every meter type.
    yearly_fixed_fee_exclusive_night: float | None = None


@dataclass(frozen=True, kw_only=True)
class VariableRates:
    """Variable energy contract: current month's effective EUR/kWh.

    Suppliers that publish per-meter indicative monthly rates (e.g. Cociter)
    populate ``peak`` / ``offpeak`` so a bi-hourly meter gets its own rate.
    Suppliers that publish a single rate (e.g. Eneco Power Flex) leave them
    None and the pricing engine falls back to ``current`` for any meter type.

    ``exclusive_night`` is the rate for a dedicated night-circuit meter.
    The price engine routes the ``exclusive_night`` meter type through it,
    falling back to ``current`` when it isn't published; the DSO side has
    a matching ``DsoOverlay.distribution_exclusive_night`` column.
    """

    current: float
    peak: float | None = None
    offpeak: float | None = None
    exclusive_night: float | None = None
    yearly_fixed_fee: float = 0.0
    # Dedicated yearly fixed fee for an exclusive-night meter circuit (EBEM
    # Groen Variabel prints one), billed instead of ``yearly_fixed_fee`` on
    # an exclusive-night config entry. None -> the standard fee applies.
    yearly_fixed_fee_exclusive_night: float | None = None
    formula: str | None = None
    # Numeric coefficients of the monthly indexation formula, already converted
    # to the EUR/kWh basis applied against the arithmetic monthly-mean spot
    # (``factor * this_month_mean + base``), when the extractor can parse them.
    # Used to re-price a signing cohort with a contract start date: the cohort's
    # coefficients are frozen while the index keeps moving, built into a
    # SpotMonthlyRates leg by the coordinator. ``None`` when the card exposes
    # only a resolved rate. For RLP-indexed cards the arithmetic mean is a close
    # (few-percent) approximation of the true residential-load-profile weighting.
    formula_factor: float | None = None
    formula_base: float | None = None
    # Contractual ceiling on the ENERGY component, per meter type, on the same
    # basis as the rates above (TVAC on a residential card, HTVA on a
    # professional one). Mega Cap is the product: "vous payez le minimum entre
    # les prix variables mensuels et ce plafond", guaranteed a year from the
    # start of supply. None on every card that caps nothing.
    ceiling_single: float | None = None
    ceiling_peak: float | None = None
    ceiling_offpeak: float | None = None
    ceiling_exclusive_night: float | None = None


@dataclass(frozen=True, kw_only=True)
class DynamicRates:
    """Dynamic energy contract: ``factor x spot + base`` per price slot.

    ``quarter_hourly`` selects the spot grid the contract bills on. Some
    Belgian dynamic suppliers (Frank Energie by default, Luminus, Mega,
    TotalEnergies, Eneco) price per clock hour, so the integration
    aggregates ENTSO-E's 15-minute day-ahead curve to hourly. Engie,
    Cociter, EBEM, Ecofix, OCTA+ and Ecopower (Dynamische Burgerstroom)
    bill per quarter-hour (their cards multiply the 15-minute Belpex /
    eSpot_15 / Epex 15 / EPEX DA spot); those extractors set this True so
    the live price table, current / next-slot sensors and the
    cheapest-window service keep the native 15-minute slots. YTD billing
    stays hourly regardless: Home Assistant only retains hourly long-term
    statistics.
    """

    factor: float
    base: float
    yearly_fixed_fee: float = 0.0
    quarter_hourly: bool = False


@dataclass(frozen=True, kw_only=True)
class SpotMonthlyRates:
    """Monthly-indexed energy contract: ``factor x monthly_mean(spot) + base``.

    The energy rate is a single flat value for the whole delivery month,
    equal to ``factor`` times the arithmetic mean of that month's hourly
    Day-Ahead spot plus ``base`` (EUR/kWh). Used by group-purchase style
    products (e.g. the Mega iChoosr / Samen Overstappen groepsaankoop)
    that index the commodity to the realized monthly average rather than
    the live hourly spot. The coordinator computes the mean from its
    ENTSO-E spot cache and threads it through the same ``spot_eur_per_kwh``
    parameter ``DynamicRates`` uses, so pricing stays a pure formula.

    Unlike ``DynamicRates`` the rate never varies within the month, so it
    always bills on the hourly grid (no ``quarter_hourly``). The current
    month's mean is a running estimate until the month closes.
    """

    factor: float
    base: float
    yearly_fixed_fee: float = 0.0
    # Dedicated yearly fixed fee for an exclusive-night meter circuit, carried
    # from a variable card re-priced to this monthly-mean leg for a signing
    # cohort (EBEM Groen Variabel / B@sic+ print one). None -> the standard fee
    # applies to every meter type.
    yearly_fixed_fee_exclusive_night: float | None = None


WeekendRule = Literal["weekend_offpeak", "weekend_no_peak", "smartflex_seasonal"]


@dataclass(frozen=True, kw_only=True)
class TimeOfUseRates:
    """Time-of-use energy contract: 3 slots by hour-of-day.

    Weekday rule is shared across products:
      peak       : 07:00-11:00 + 17:00-22:00
      transition : 11:00-17:00 + 22:00-01:00
      offpeak    : 01:00-07:00

    Weekend rule is product-dependent (``weekend_rule``):

      weekend_offpeak (generic CWaPE default):
        Saturday, Sunday and public holidays are entirely off-peak.

      weekend_no_peak (Engie Empower Flextime):
        peak       : never
        transition : 07:00-11:00 + 17:00-01:00
        offpeak    : 01:00-07:00 + 11:00-17:00

      smartflex_seasonal (Luminus SmartFlex):
        Seasonal bands applied every day, no weekend exception. The
        11:00-17:00 midday window is off-peak in spring/summer
        (21/03-20/09) and transition otherwise; 22:00-07:00 is always
        transition. See ``pricing.tou_slot``.

    Requires a smart meter (SMR3). Like ``VariableRates``, the rates
    can be re-published monthly; the formula field carries the
    indexation expression if the supplier publishes one.
    """

    peak: float
    transition: float
    offpeak: float
    yearly_fixed_fee: float = 0.0
    formula: str | None = None
    weekend_rule: WeekendRule = "weekend_offpeak"


@dataclass(frozen=True, kw_only=True)
class ImpactRates:
    """Wallonia Tarif Impact energy contract: 3 slots on CWaPE bands.

    Distinct from :class:`TimeOfUseRates` because the hour-of-day
    schedule is the CWaPE-defined Impact one (every day of the week,
    no weekend exception), matching the DSO Impact tariff that gates
    eligibility:

      pic    17:00-22:00            (highest)
      medium 07:00-11:00 + 22:00-01:00
      eco    01:00-07:00 + 11:00-17:00 (lowest)

    Requires an SMR3 quarter-hourly smart meter and an opt-in to the
    DSO Impact tariff. The supplier publishes per-band formulas; the
    snapshot carries the resolved monthly rates and the formula text
    for diagnostics.
    """

    pic: float
    medium: float
    eco: float
    yearly_fixed_fee: float = 0.0
    formula: str | None = None
    # Numeric coefficients of each band's indexation formula
    # (``factor * this_month_mean + base``), when the extractor can parse
    # them, on the same basis as the resolved rates above: baked to TVAC
    # EUR/kWh on a residential card, left ex-VAT on a professional one. The
    # cards print them in c€/kWh Hors TVA, so both conversions are applied.
    #
    # These are DIAGNOSTIC only today. Signing-cohort re-pricing does not use
    # them: an Impact contract is monthly-indexed, so re-pricing a cohort
    # correctly needs a three-band monthly-mean energy shape that resolves
    # downstream, the way SpotMonthlyRates does for the single-rate case, and
    # that shape does not exist. Freezing the archived card's resolved bands
    # instead would pin the signing-month index, which is the exact bug
    # ``_cohort_energy_from_archived`` exists to avoid, so it returns None for
    # this shape and the entry bills at the current card. Capturing the
    # coefficients here is the prerequisite if that shape is ever built.
    pic_factor: float | None = None
    pic_base: float | None = None
    medium_factor: float | None = None
    medium_base: float | None = None
    eco_factor: float | None = None
    eco_base: float | None = None


EnergyRates = (
    FixedRates
    | VariableRates
    | DynamicRates
    | TimeOfUseRates
    | ImpactRates
    | SpotMonthlyRates
)


@dataclass(frozen=True, kw_only=True)
class InjectionRates:
    """Injection (solar feed-in) compensation, in EUR/kWh.

    Belgian residential injection is exempt from VAT, so values here are
    NEVER VAT-incl on a residential card regardless of the consumption
    snapshot's vat_rate. Professional injection is not exempt - the cards
    print *"Le prix d'injection est soumis a la TVA (21%)"* - so a
    professional extractor sets ``vat_applies`` and ``apply_vat`` grosses
    these rates with the rest of the card. At least one of (current,
    factor+base) must be populated:

      - ``current`` is the supplier's monthly indicative price (e.g. Eneco's
        "Maandprijs" of 4.76 c/kWh on Power Fix). Used when no live spot is
        available.
      - ``factor`` and ``base`` define the hourly formula
        ``injection_eur_per_kwh = factor * spot_eur_per_kwh + base``.
        Belgian formulas can produce negative values at low spot - the
        producer pays to inject - and the pricing engine respects that
        unless ``floor_at_zero`` is set.

    ``floor_at_zero`` clamps the resolved injection rate at 0 EUR/kWh. Some
    contracts (e.g. the Mega groepsaankoop) guarantee the feed-in tariff can
    never go negative; the pricing engine then takes ``max(rate, 0)`` in both
    the live and historical paths. Default False keeps the negative-allowed
    behaviour every scraped card relies on.
    """

    current: float | None = None
    factor: float | None = None
    base: float | None = None
    formula: str | None = None
    floor_at_zero: bool = False
    # Per-slot injection for a time-of-use contract whose feed-in tariff
    # varies by slot (Engie Empower Flextime publishes a peak / transition
    # / super-off-peak triplet, monthly-realized like its consumption
    # rates). When ``peak`` is set, the pricing engine selects the slot
    # with the same ``tou_slot()`` rule as the consumption side and uses
    # the matching rate; ``current`` stays the single-meter fallback.
    # None for the (vast) majority of contracts whose injection is one
    # rate across all hours.
    peak: float | None = None
    transition: float | None = None
    offpeak: float | None = None
    # True when this formula indexes on the SOLAR-weighted monthly mean
    # (Belpex_SPP and friends) rather than on the same index the energy leg
    # uses. energie.be Variabel is the case: consumption on Belpex_RLP,
    # injection on Belpex_SPP, and the two part company badly - July 2026
    # settled at 6,34 c€/kWh SPP against 11,42 RLP, so resolving this formula
    # against the energy leg's mean would roughly DOUBLE the credit, because PV
    # output peaks exactly when the day-ahead price troughs.
    #
    # It makes the coordinator fetch the Synergrid SPP profile for the entry,
    # and it makes the fallback strict: with no weighted mean available the
    # formula is not resolved at all and the card's printed ``current``
    # indicative is credited instead. Never resolve an SPP-indexed formula
    # against a plain arithmetic mean - that is the failure this flag exists
    # to prevent, and it is silent.
    spp_indexed: bool = False
    # True when the card taxes injection (professional cards do, at 21%).
    # None of these rates passes through the pricing engine's per-component
    # VAT gross-up, so ``apply_vat`` bakes them, like the fixed fees. Left
    # False by every residential extractor, where injection is exempt.
    vat_applies: bool = False


@dataclass(frozen=True, kw_only=True)
class DsoOverlay:
    """Network + capacity costs for one DSO sub-area, in EUR/kWh and EUR/kW/yr."""

    distribution_single: float
    distribution_peak: float | None = None
    distribution_offpeak: float | None = None
    # Distribution rate billed on a separate exclusive-night meter
    # circuit (electric water heater, night-storage heater). Belgian
    # DSOs publish this on every tariff card; populated by extractors
    # that parse the dedicated column. None falls back to
    # distribution_offpeak in pricing.network_eur_per_kwh.
    distribution_exclusive_night: float | None = None
    transport: float
    data_management_per_year: float = 0.0
    capacity_eur_per_kw_year: float | None = None
    # Brussels Brugel OSP (Obligations de Service Public) annual fee keyed by
    # connection-power tier, every band the card prints (le1_44 through gt56).
    # Only the Sibelga overlay carries it; the user's configured tier selects
    # the billed value. None outside Brussels or when the card omits the table.
    brussels_osp_by_tier: dict[str, float] | None = None
    # VREG ceiling on the periodic network cost, in EUR/kWh, printed as
    # "maximumtarief" on the Flemish cards. Ecopower states the rule on its
    # card: "zou u met het capaciteitstarief en het nettarief per kWh meer
    # nettarieven betalen dan met het maximumtarief? Dan betaalt u het
    # maximumtarief". So the capacity term plus the per-kWh network term may
    # not exceed this times the volume. None outside Flanders or on a card
    # that omits the column.
    network_ceiling_eur_per_kwh: float | None = None
    # Sibelga's power term for a connection ABOVE 13 kVA, in EUR/year. The
    # card prints two columns and ``data_management_per_year`` holds the one
    # at or below 13 kVA; a 3x400 V / 25 A house is 17,3 kVA and belongs in
    # this one. None outside Brussels or when the card prints a single column.
    brussels_power_term_above_13kva: float | None = None
    # Prosumer (compensation-regime) tariff in EUR per kVA of solar inverter
    # capacity per year, valid in Wallonia until 2030 per CWaPE. Wallonia DSOs
    # publish it on every card. Some Flanders supplier cards also carry a
    # prosumer column for compensation-regime installs, which the extractors
    # parse, so it is not always None in Flanders; it stays None only when a
    # card omits the column.
    prosumer_eur_per_kva_year: float | None = None
    # Tarif Impact (Wallonia-only, opt-in for SMR3 customers). Three
    # distribution rates indexed by CWaPE-defined hour-of-day bands:
    #   pic    : 17:00-22:00            (highest, every day)
    #   medium : 07:00-11:00 + 22:00-01:00
    #   eco    : 01:00-07:00 + 11:00-17:00 (lowest, every day)
    # Wallonia DSOs publish all three on every supplier tariff card;
    # Brussels (Sibelga) and Flanders (Fluvius) do not, so they stay
    # None there.
    distribution_pic: float | None = None
    distribution_medium: float | None = None
    distribution_eco: float | None = None


def fixed_or_variable_rates(
    kind: str,
    *,
    single: float,
    peak: float | None,
    offpeak: float | None,
    exclusive_night: float | None,
    yearly_fixed_fee: float,
) -> FixedRates | VariableRates:
    """Build :class:`FixedRates` (``kind == "fixed"``) or
    :class:`VariableRates` from the same single/peak/offpeak/exclusive-night
    row and yearly fixed fee.

    The two rate classes carry the identical fields under different names
    (``single`` vs ``current``); providers whose variable card also parses a
    dynamic formula or a separate exclusive-night fee build the rate object
    directly instead.
    """
    if kind == "fixed":
        return FixedRates(
            single=single,
            peak=peak,
            offpeak=offpeak,
            exclusive_night=exclusive_night,
            yearly_fixed_fee=yearly_fixed_fee,
        )
    return VariableRates(
        current=single,
        peak=peak,
        offpeak=offpeak,
        exclusive_night=exclusive_night,
        yearly_fixed_fee=yearly_fixed_fee,
    )


def walloon_dso_overlay(
    *,
    mono: float,
    peak: float,
    offpeak: float,
    excl_night: float,
    pic: float,
    medium: float,
    eco: float,
    transport: float,
    terme_fixe: float,
    prosumer: float | None,
) -> DsoOverlay:
    """Build a Walloon :class:`DsoOverlay` from a card's c/kWh row.

    CWaPE tariff cards print the distribution and transport rates in
    c€/kWh; every field here is scaled to EUR/kWh (``/ 100``).
    ``terme_fixe`` (databeheer, EUR/year) and ``prosumer`` (EUR/kVA/year)
    are annual amounts passed through unscaled.

    Every keyword is named rather than positional precisely so a card's own
    column order does not matter: DATS 24 prints the Impact bands
    PIC | MEDIUM | ECO and EnergyVision prints them ECO | MEDIUM | PIC, and
    both map onto the same call.

    Luminus and Eneco build :class:`DsoOverlay` directly, and only for one
    reason: their cards leave the Impact ``pic`` / ``medium`` / ``eco``
    triplet nullable, which these parameters are not. (An earlier version of
    this note also exempted providers that "index the row positionally
    (Engie, DATS24)" and ones whose cards "print values already in EUR/kWh
    (Eneco)". Neither held: all three of those call this helper now, and
    Eneco divides by 100 like everyone else.)
    """
    return DsoOverlay(
        distribution_single=mono / 100.0,
        distribution_peak=peak / 100.0,
        distribution_offpeak=offpeak / 100.0,
        distribution_exclusive_night=excl_night / 100.0,
        distribution_pic=pic / 100.0,
        distribution_medium=medium / 100.0,
        distribution_eco=eco / 100.0,
        transport=transport / 100.0,
        data_management_per_year=terme_fixe,
        prosumer_eur_per_kva_year=prosumer,
    )


def brussels_sibelga_overlay(
    *,
    mono: float,
    peak: float,
    offpeak: float,
    excl_night: float,
    transport: float,
    data_management_per_year: float,
    osp_by_tier: dict[str, float] | None,
    power_term_above_13kva: float | None = None,
) -> DsoOverlay:
    """Build the Brussels (Sibelga) :class:`DsoOverlay` from a card's row.

    The distribution and transport rates print in c€/kWh and scale to
    EUR/kWh (``/ 100``). ``data_management_per_year`` (databeheer / terme
    fixe) and ``osp_by_tier`` (the Brugel OSP table from
    :func:`_pdf.parse_brussels_osp`) are supplier-specific -- some cards
    print a single databeheer line, others sum a measurement and a
    fixed-term charge -- so the caller computes them and passes them in.
    """
    return DsoOverlay(
        distribution_single=mono / 100.0,
        distribution_peak=peak / 100.0,
        distribution_offpeak=offpeak / 100.0,
        distribution_exclusive_night=excl_night / 100.0,
        transport=transport / 100.0,
        data_management_per_year=data_management_per_year,
        brussels_osp_by_tier=osp_by_tier,
        brussels_power_term_above_13kva=power_term_above_13kva,
    )


@dataclass(frozen=True, kw_only=True)
class TaxOverlay:
    """Federal + regional levies, all in EUR/kWh except the energy fund.

    Regional renewables differ across the three regions: Flanders
    (cogen + green-energy surcharge, ~1.5 c/kWh), Wallonia (green energy
    contribution, ~3.1 c/kWh) and Brussels (green energy, ~2.7 c/kWh).
    The pricing engine picks the right one per region; an extractor that
    only operates in one or two of them leaves the others at 0.
    """

    federal_excise: float
    energy_contribution: float
    # Degressive excise bands as ((upper_kwh, eur_per_kwh), ...) ascending,
    # for a card that prints the special excise as a tariff schedule by
    # annual consumption instead of one rate. Professional cards do; every
    # residential card prints a single rate and leaves this None. The
    # schedule is billed PER TRANCHE, so :func:`resolve_excise_band` blends
    # it over the entry's annual volume into ``federal_excise`` and the
    # pricing engine keeps reading one rate, knowing nothing about bands.
    federal_excise_bands: tuple[tuple[float, float], ...] | None = None
    flanders_renewables: float = 0.0
    wallonia_renewables: float = 0.0
    brussels_renewables: float = 0.0
    region_connection_fee: float = 0.0
    # True when the card is Walloon but prints no connection-fee row, so the
    # fee above is a stand-in rather than a reading. Wallonia still levies it
    # and the supplier still passes it through, so a snapshot carrying this
    # under-bills by the regulated rate; the coordinator raises a repair issue
    # telling the user what their cost excludes. Every card that prints the
    # row leaves this False, as does any non-Walloon card, where 0.0 is the
    # honest value rather than a gap.
    region_connection_fee_unavailable: bool = False
    energy_fund_eur_per_month: float = 0.0
    # 0.0 means the snapshot's prices are already VAT-incl (the convention
    # for both Eneco and Cociter today). An extractor that starts shipping
    # ex-VAT numbers must set this to the parsed rate explicitly.
    vat_rate: float = 0.0
    # The rate the CARD was published at, preserved across ``apply_vat``.
    # ``vat_rate`` above is the rate the pricing engine should still apply, so
    # it is zeroed for an entry that deducts VAT -- which loses the only
    # record of what basis the card used. Anything that has to put a
    # hand-entered figure onto the entry's basis needs that, and it must
    # travel WITH the snapshot: threading it through the eight functions that
    # reach the cohort path is how it came to be applied on the live tick
    # only. Extractors leave it 0.0; read it as
    # ``published_vat_rate or vat_rate`` so a raw (unresolved) card, and a
    # cache written before this field existed, both answer correctly.
    published_vat_rate: float = 0.0


@dataclass(frozen=True, kw_only=True)
class SupplierSnapshot:
    """Everything extracted from one supplier's tariff card.

    A snapshot is per (supplier, contract). The coordinator combines it
    with the user's selected DSO sub-area to produce the all-in price.
    """

    supplier: str
    contract: str
    energy: EnergyRates
    dsos: dict[str, DsoOverlay]
    taxes: TaxOverlay
    source_url: str
    publication_label: str = ""
    injection: InjectionRates | None = None
    # Supplier-side compensation-regime prosumer forfait in EUR per kVA of
    # inverter capacity per year, billed ON TOP OF the DSO prosumer tariff
    # (DsoOverlay.prosumer_eur_per_kva_year). Cociter Variable publishes one
    # ("Forfait panneaux photovoltaiques ... en regime de compensation");
    # most cards don't, so it stays None. Carried on the basis its card
    # prints it: TVAC on a residential card, excl-VAT on a professional one,
    # where apply_vat bakes it like every other annual fee.
    supplier_prosumer_eur_per_kva_year: float | None = None
    # Last calendar day the published rates apply to (typically the last
    # day of the supplier's pricing month). ``None`` when the extractor
    # couldn't parse a validity period from the card. Consumers that
    # need to know whether tomorrow's rates are *actually* the right
    # ones (the tomorrow_prices_available binary sensor, in particular)
    # check ``date.today() <= valid_until``; ``None`` means we don't
    # know, so callers should fall back to "treat as available".
    valid_until: date | None = None


def _vat_energy(energy: EnergyRates, factor: float) -> EnergyRates:
    # Only these three carry a separate exclusive-night abonnement; the rest
    # bill the standard fee on every meter type.
    if isinstance(energy, (FixedRates, VariableRates, SpotMonthlyRates)):
        excl_night = energy.yearly_fixed_fee_exclusive_night
        return replace(
            energy,
            yearly_fixed_fee=energy.yearly_fixed_fee * factor,
            yearly_fixed_fee_exclusive_night=(
                None if excl_night is None else excl_night * factor
            ),
        )
    return replace(energy, yearly_fixed_fee=energy.yearly_fixed_fee * factor)


def _vat_dso(dso: DsoOverlay, factor: float) -> DsoOverlay:
    return replace(
        dso,
        data_management_per_year=dso.data_management_per_year * factor,
        capacity_eur_per_kw_year=(
            None
            if dso.capacity_eur_per_kw_year is None
            else dso.capacity_eur_per_kw_year * factor
        ),
        prosumer_eur_per_kva_year=(
            None
            if dso.prosumer_eur_per_kva_year is None
            else dso.prosumer_eur_per_kva_year * factor
        ),
        brussels_osp_by_tier=(
            None
            if dso.brussels_osp_by_tier is None
            else {k: v * factor for k, v in dso.brussels_osp_by_tier.items()}
        ),
        brussels_power_term_above_13kva=(
            None
            if dso.brussels_power_term_above_13kva is None
            else dso.brussels_power_term_above_13kva * factor
        ),
        network_ceiling_eur_per_kwh=(
            None
            if dso.network_ceiling_eur_per_kwh is None
            else dso.network_ceiling_eur_per_kwh * factor
        ),
    )


def _vat_injection(injection: InjectionRates, factor: float) -> InjectionRates:
    def scaled(value: float | None) -> float | None:
        return None if value is None else value * factor

    return replace(
        injection,
        current=scaled(injection.current),
        factor=scaled(injection.factor),
        base=scaled(injection.base),
        peak=scaled(injection.peak),
        transition=scaled(injection.transition),
        offpeak=scaled(injection.offpeak),
    )


def apply_vat(snapshot: SupplierSnapshot, *, include_vat: bool) -> SupplierSnapshot:
    """Resolve a snapshot against the entry's VAT preference.

    ``TaxOverlay.vat_rate == 0.0`` means the card printed VAT-inclusive
    numbers - the convention every residential card follows - so the
    snapshot is returned unchanged (identity, not a copy).

    A non-zero rate means the card printed everything excluding VAT, as
    professional cards do. Two kinds of value then need different handling
    and only one of them is covered by the pricing engine:

      - Per-kWh rates are grossed up per component in
        ``pricing._finalize_breakdown`` from ``vat_rate``, so they stay as
        printed here and only the rate itself is resolved.
      - Fixed and annual fees - the yearly fee, data management, capacity,
        the DSO and supplier prosumer forfaits and the Brussels OSP table -
        never reach that path: the live, year-to-date, backfill and compare
        paths each sum them raw. They are baked here so the choice lands
        exactly once whichever path bills them.

    ``include_vat=False`` serves a business that deducts VAT: the factor
    is 1.0 and the numbers stay as the card printed them.

    Two values are exempt outright and stay as parsed whatever the card's
    basis. Injection is baked only when the card taxes it
    (``InjectionRates.vat_applies``); residential injection is VAT-exempt.
    The Flemish energy fund is never baked at all: the cards say so in as
    many words, Engie footnoting its ``Cotisation Fonds Energie Region
    Flamande`` with "Vous ne payez pas de TVA sur ces couts" and DATS 24 its
    ``Bijdrage Energiefonds Vlaams Gewest`` with "Niet aan btw onderworpen".
    Grossing it charged a professional Flanders entry 12,18 EUR/month
    against an invoiced 10,07, about 25 EUR/yr.

    Call this per config entry, never before the shared snapshot cache:
    the cache is keyed on (supplier, contract, region) and shared between
    entries that may answer this question differently.
    """
    rate = snapshot.taxes.vat_rate
    if rate == 0.0:
        return snapshot
    factor = 1.0 + rate if include_vat else 1.0
    injection = snapshot.injection
    return replace(
        snapshot,
        energy=_vat_energy(snapshot.energy, factor),
        dsos={k: _vat_dso(v, factor) for k, v in snapshot.dsos.items()},
        injection=(
            _vat_injection(injection, factor)
            if injection is not None and injection.vat_applies
            else injection
        ),
        # energy_fund_eur_per_month is deliberately absent: the levy is
        # VAT-free, so it is billed exactly as the card prints it.
        taxes=replace(
            snapshot.taxes,
            vat_rate=rate if include_vat else 0.0,
            published_vat_rate=rate,
        ),
        supplier_prosumer_eur_per_kva_year=(
            None
            if snapshot.supplier_prosumer_eur_per_kva_year is None
            else snapshot.supplier_prosumer_eur_per_kva_year * factor
        ),
    )


def blended_excise_rate(
    bands: tuple[tuple[float, float], ...], annual_kwh: float
) -> float:
    """Average EUR/kWh of a degressive excise schedule over ``annual_kwh``.

    The cards say what the schedule is: "un tarif degressif PAR TRANCHE de
    consommation, calcule sur une base annuelle". Each slice of the year's
    volume is billed at its own band's rate, so a site drawing 30 000 kWh
    pays the first 20 000 at the first band and only the remaining 10 000 at
    the second. Billing the whole volume at the band the total lands in is a
    different, always cheaper number, because the schedule decreases.

    The engine prices per hour and cannot know where in the year's cumulative
    volume an hour sits, but it does not have to: the charge is defined on an
    annual basis, so the honest per-kWh figure is the year's total divided by
    the year's volume. That is what this returns, and it makes the annual
    bill exact whenever the volume estimate is.

    A volume past the last band is billed at the last band's rate for the
    remainder: the schedule stops at the ceiling the card covers (1.000.000
    kWh/year on the current professional cards), and above that the
    connection is out of what these cards price at all, so extending the last
    rate keeps a plausible number rather than inventing one.
    """
    if annual_kwh <= 0.0:
        return bands[0][1]
    total = 0.0
    floor = 0.0
    for upper, rate in bands:
        slice_kwh = min(annual_kwh, upper) - floor
        if slice_kwh > 0.0:
            total += slice_kwh * rate
        floor = upper
        if annual_kwh <= upper:
            break
    if annual_kwh > floor:
        total += (annual_kwh - floor) * bands[-1][1]
    return total / annual_kwh


def resolve_excise_band(
    snapshot: SupplierSnapshot, annual_kwh: float
) -> SupplierSnapshot:
    """Resolve a degressive excise schedule to one rate, or leave the card alone.

    A card without ``federal_excise_bands`` prints one rate and is returned
    unchanged (identity), which is every residential card.

    A card that prints a schedule bills it per tranche, so the rate the engine
    reads is the blend over the entry's estimated annual volume rather than
    the band that volume lands in; see :func:`blended_excise_rate`. Resolving
    it once here keeps the pricing engine reading a single ``federal_excise``
    and knowing nothing about bands.
    """
    bands = snapshot.taxes.federal_excise_bands
    if not bands:
        return snapshot
    rate = blended_excise_rate(bands, annual_kwh)
    if rate == snapshot.taxes.federal_excise:
        return snapshot
    return replace(snapshot, taxes=replace(snapshot.taxes, federal_excise=rate))


SnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str], Awaitable[SupplierSnapshot]
]

# Cheap-probe contract: same return value across calls means the snapshot
# is still valid; a different value means refetch. ``None`` signals the
# supplier has no probe path the coordinator can rely on (Engie/Luminus
# API endpoints, DATS 24 single-PDF) and the time-based TTL takes over.
SnapshotProbe = Callable[[aiohttp.ClientSession, str, str], Awaitable[str | None]]

# Historical-fetch contract: fetch the published card for a specific
# (year, month). Used by the time-correct yearly-cost flow to bill each
# past month at its own rate. Returns ``None`` when the supplier has no
# accessible archive for that month (overwrite-in-place suppliers like
# OCTA+ / TotalEnergies, API-only suppliers like Engie / Luminus / DATS 24,
# or a month before the supplier's archive horizon).
ArchivedSnapshotFetcher = Callable[
    [aiohttp.ClientSession, str, str, "date"], Awaitable["SupplierSnapshot | None"]
]


@dataclass(frozen=True, kw_only=True)
class SupplierExtractor:
    """Registry entry for one supplier."""

    id: str
    label: str
    contracts: tuple[Contract, ...]
    fetch: SnapshotFetcher
    # Optional cheap probe (HEAD or listing GET) that returns a freshness
    # key. The coordinator calls it hourly and only re-runs ``fetch`` when
    # the key changes. ``None`` means no probe is available.
    probe: SnapshotProbe | None = None
    # Optional historical fetch: returns the published snapshot for a
    # given (year, month) so past consumption can be billed at the
    # correct historical rate. ``None`` (or a callable returning ``None``)
    # means "no archive for this month" - the coordinator falls back to
    # using the current snapshot as a proxy.
    fetch_for_month: ArchivedSnapshotFetcher | None = None
    # Set when the supplier has announced it is leaving the residential
    # market: the date its contracts stop being supplied, and the registry
    # id of the supplier taking them over. Two effects, both deliberate:
    # the config flow stops OFFERING the supplier to new users, and every
    # existing entry raises a Repairs card telling the user where their
    # contract is going. Existing entries keep pricing normally until the
    # supplier stops publishing - a withdrawal announcement is not a reason
    # to stop billing someone correctly for the months they are still
    # supplied. Purely declarative: nothing compares these to the clock.
    deprecated_until: date | None = None
    deprecated_successor: str | None = None

    def regions(self) -> frozenset[str]:
        """Union of regions across this supplier's contracts."""
        out: set[str] = set()
        for c in self.contracts:
            out |= c.regions
        return frozenset(out)


class SupplierProtocol(Protocol):
    """Each supplier module must expose a top-level ``EXTRACTOR`` of this shape."""

    EXTRACTOR: SupplierExtractor


class ExtractorError(Exception):
    """Raised when a supplier's source cannot be fetched or parsed."""


class CardNotReadableError(ExtractorError):
    """The card downloaded fine but carries no text layer to read.

    A supplier that publishes its tariff card as page images cannot be
    parsed by any amount of regex work, so the user needs different advice
    from "the layout changed, please report it": there is nothing in the
    document to report. Ecofix started doing this in August 2026.

    Deliberately DERIVED per fetch rather than declared per supplier. The
    first version of this was a ``cards_unreadable`` flag in the registry,
    which encoded one month's observation as a permanent property: had the
    supplier gone back to publishing text, the flag would have kept claiming
    otherwise until someone shipped a release to clear it. Raising on what
    the current download actually contains self-heals the moment readable
    cards return, and covers any supplier that starts doing this.
    """
