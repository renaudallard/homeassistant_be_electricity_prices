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

"""The shapes a tariff card can print.

One dataclass per kind of energy leg, one for the feed-in side, and the union
the rest of the integration dispatches on. Data only: what a card says, in the
units it said it in. Nothing here decides a price, applies a tax or reads a
household's settings, which is what keeps every extractor free to build one
without pulling in the pricing engine.
"""

from __future__ import annotations

from ..const import REGIONS
from dataclasses import dataclass
from dataclasses import field
from typing import Literal


TariffKind = Literal[
    "fixed", "variable", "dynamic", "tou", "tou_impact", "spot_monthly"
]

# Which residential-load blend an RLP-weighted month index uses. Three Belgian
# suppliers read Synergrid's one workbook three ways, each reproducing its own
# published value to the cent: "distinct" is the equal mean of the three
# distinct regional curves (Fluvius, the Walloon DSOs, Sibelga), which is
# Eneco's Belpex-RLP-M; "columns" is the mean over every DSO column, weighting
# each region by its number of sub-areas, which is energie.be's Belpex_RLP;
# "flanders" is the Fluvius curve alone, which Energy Knights, a Flanders-only
# supplier, bills on. Meaningful only where ``rlp_indexed`` is set.
RlpBlend = Literal["distinct", "columns", "flanders"]

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
    # True when this (non-dynamic) product's injection needs ENTSO-E spots
    # that its ENERGY leg does not fetch. Two shapes qualify: a per-hour spot
    # formula with no printed indicative (Cociter Variable), and a formula
    # indexed on a monthly mean (every spp_indexed / month_indexed card).
    # Either way the config flow offers the API-key step on the injection
    # regime; dynamic contracts collect the key via their energy formula and
    # leave this False.
    #
    # This is a REGISTRY flag, known before any card is fetched, while the
    # shape itself is only known after parsing. The two must agree or the fix
    # is unreachable: a parser can set spp_indexed all it likes, but with this
    # False no flow step ever offers the key, no spots are fetched, no mean is
    # available and every path falls back to the card's printed figure. That
    # silently disabled nine contracts across five suppliers.
    # ``test_every_month_indexed_card_can_collect_a_key`` pins the agreement.
    spot_indexed_injection: bool = False
    # True when this (non-spot-priced) product's ENERGY is indexed on the
    # delivery month's mean and its card prints last month's figure: Cociter
    # Variable and Trihoraire, Engie's EPEXDAM cards, Luminus MaxxFlex and
    # SmartFlex, OCTA+ Smart Variable / Flux / Eco Flux, Eneco Flex and Flex
    # One, TotalEnergies's five BELPEXM_RLP variable cards, and every Mega
    # Flex plus Off-peak Impact, whose cards name the settled month outright
    # ("pour le mois de <MONTH>"). The re-price needs ENTSO-E spots the kind
    # never collects a key for,
    # so the config flow offers the optional key step on EVERY solar regime,
    # not only the injection one the flag above serves. Same registry-versus-
    # parser agreement as that flag: the live check holds each fetched card's
    # ``month_indexed`` against it, since a flag set here with no formula
    # parsed offers a key nothing resolves, and a formula parsed with no flag
    # here is a re-price no flow step can ever switch on.
    month_indexed_energy: bool = False
    # True when the supplier lets the customer settle this product on the
    # 15-minute grid instead of the hourly one, and the card says so. Frank
    # Energie is the case: every Dynamisch tier prices "BELPEX per uur" by
    # default and its footnote offers "kwartierprijzen [Quarter Hourly
    # BELPEX]" through the app, switchable from one month to the next. The
    # coefficients do not change, only the index the formula reads, so the
    # choice cannot be read off the card and cannot be a second contract
    # either: it is a per-entry fact the config flow asks for and
    # ``resolve_settlement_grid`` applies.
    #
    # Not the same thing as a supplier selling both grids as two products
    # (Energy Knights Agilior on Belpex_15 against Agilis on Belpex_h), which
    # the contract picker already separates, nor as a card that mandates one
    # grid (OCTA+ requires an SMR3 meter and bills per quarter regardless).
    # Only set it where the card documents a choice, or the flow offers a
    # toggle that moves the bill away from what the supplier actually invoices.
    quarter_hourly_option: bool = False
    # True when this product's card prices a direct-debit payer differently,
    # so the config flow asks how the household pays. Brusol's Groene stroom
    # is the case: 250 EUR/yr standing charge, 230 on domiciliering.
    #
    # Same registry-versus-parser agreement as the two flags above, and the
    # same failure if they disagree: with this False no step ever asks, the
    # answer is never stored and ``resolve_direct_debit`` has nothing to
    # apply, so a discount the extractor parsed is billed to nobody. Set it
    # only where the card states the reduction, never to offer a box a
    # supplier's own invoice would not honour.
    direct_debit_discount: bool = False


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
    # only a resolved rate. A leg flagged ``rlp_indexed`` is resolved against
    # Synergrid's residential load profile instead, in the blend it names; an
    # unflagged leg keeps the arithmetic mean, a close (few-percent)
    # approximation that runs below any of the weighted blends.
    formula_factor: float | None = None
    formula_base: float | None = None
    # True when the card says its rate IS the delivery month's index and the
    # printed value is only an indicative computed from the PREVIOUS month's.
    # The coordinator then resolves the coefficients against the delivery
    # month's own mean, cohort or not, and the printed rate is the fallback for
    # an entry with no ENTSO-E key. False -> the printed rate is what is billed.
    month_indexed: bool = False
    # The value that index settled at for THIS card's own month, in EUR/kWh,
    # once the supplier has published it. Eneco prints each month's realised
    # Belpex-RLP-M in the footnote of the FOLLOWING month's card, and EBEM
    # names the month just closed the same way ("vorige maand bedroeg deze
    # index"), so an archived month is settled on the figure the supplier
    # itself bills, and ``current`` is then that figure rather than the
    # printed estimate. None while the month is still running or the next
    # card is not out yet.
    index_realised: float | None = None
    # True when the index is the RLP-weighted month mean (Eneco's
    # Belpex-RLP-M) rather than the plain one: each hour's Belpex quotation
    # weighted by Synergrid's residential load profile. The coordinator then
    # resolves the coefficients against that weighted mean, which sits 3 to 6
    # percent above the plain mean on the 2026 months because households draw
    # in the expensive hours. Meaningful only with ``month_indexed``.
    rlp_indexed: bool = False
    # Which DSO blend of the RLP profile the weighting uses (see ``RlpBlend``).
    # Only read when ``rlp_indexed``; the default is Eneco's distinct-curve mean.
    rlp_blend: RlpBlend = "distinct"
    # The same coefficients for a bi-hourly meter's two bands, when the card
    # prints them per meter. Mega does: mono "Epex x 1,1095 + 3,6", peak
    # "x 1,3275 + 3,6", off-peak "x 0,94 + 3,6". Billing a bi-hourly cohort at
    # the mono pair over-charges its peak hours by a fifth and under-charges
    # its off-peak ones. None -> the card publishes one formula for every
    # meter and the mono pair applies throughout.
    formula_factor_peak: float | None = None
    formula_base_peak: float | None = None
    formula_factor_offpeak: float | None = None
    formula_base_offpeak: float | None = None
    # And for a dedicated night-circuit meter, which is a THIRD formula and
    # not the off-peak one. OCTA+ prints "exclusif nuit : Epex RLP M * 1,061"
    # against an off-peak 1,011 and a mono 1,150, so routing that circuit onto
    # either neighbour is wrong, and it draws the large volumes. None -> the
    # card publishes no separate night formula and the mono pair applies.
    formula_factor_exclusive_night: float | None = None
    formula_base_exclusive_night: float | None = None
    # Contractual ceiling on the ENERGY component, per meter type, on the same
    # basis as the rates above (TVAC on a residential card, HTVA on a
    # professional one). Mega Cap was the product, retired in September 2026;
    # the parser keys off the card's "plafond" sentence, not off the product.
    # None on every card that caps nothing, which is all of them today.
    ceiling_single: float | None = None
    ceiling_peak: float | None = None
    ceiling_offpeak: float | None = None
    ceiling_exclusive_night: float | None = None
    # The CWaPE incitative bands for the SUPPLIER energy, on a card that
    # prices the same product two ways and lets the customer pick. Bolt's
    # Walloon variable cards print a "Tarif Impact (Wallonie)" block beside
    # the standard rates.
    #
    # Carried here rather than as a separate contract because that is what the
    # card is: one product, two network configurations, chosen by
    # ``dso_tariff_mode``. The DSO side already works that way, and it was the
    # ENERGY side that stayed on the mono / bi-hourly rates while the network
    # moved with the band. ``energy_eur_per_kwh`` routes these whenever the
    # entry is on the incitative mode and all three are present.
    impact_pic: float | None = None
    impact_medium: float | None = None
    impact_eco: float | None = None


@dataclass(frozen=True, kw_only=True)
class DynamicRates:
    """Dynamic energy contract: ``factor x spot + base`` per price slot.

    ``quarter_hourly`` selects the spot grid the contract bills on. Some
    Belgian dynamic suppliers (Frank Energie, Luminus, Mega, TotalEnergies,
    Eneco, Energy Knights Agilis Online) price per clock hour, so the
    integration aggregates ENTSO-E's 15-minute day-ahead curve to hourly.
    Engie, Cociter, EBEM, Ecofix, OCTA+, Ecopower (Dynamische
    Burgerstroom), Bolt (Dynamisch), energie.be, EnergyVision and Energy
    Knights (Agilior Online) bill per quarter-hour (their cards multiply
    the 15-minute Belpex / eSpot_15 / Epex 15 / EPEX DA spot); those
    extractors set this True so the live price table, current /
    next-slot sensors and the cheapest-window service keep the native
    15-minute slots. YTD billing stays hourly regardless: Home Assistant
    only retains hourly long-term statistics.

    Where the supplier lets the customer pick the grid rather than fixing
    it on the card, the extractor parses the hourly default the card
    prints and the entry's own answer flips it in
    :func:`resolve_settlement_grid`; see ``Contract.quarter_hourly_option``.
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
    # The bi-hourly bands' own coefficients, when the card printed them per
    # meter and a signing cohort was re-priced onto this leg. None -> one
    # formula for every meter, which is every card that does not split them.
    factor_peak: float | None = None
    base_peak: float | None = None
    factor_offpeak: float | None = None
    base_offpeak: float | None = None
    # A dedicated night circuit is a third formula, not the off-peak one.
    # Routed ahead of the bi-hourly band test, because that circuit is billed
    # per meter rather than per hour of the day.
    factor_exclusive_night: float | None = None
    base_exclusive_night: float | None = None
    # The signing cohort's contractual ceiling on the ENERGY component, per
    # meter. Mega Cap was the product, and its cap is cohort-scoped: "Le plafond
    # et la formule tarifaire sont garantis pour une duree de 1 an a compter du
    # debut de la fourniture ... valables pour tout contrat signe en 08/2026".
    # Converting a Cap cohort to this kind without carrying them dropped the
    # cap outright, and the early-2026 cards cap TIGHT (16,23 / 16,65 c/kWh
    # mono) against an index already above them, so it binds today.
    ceiling_single: float | None = None
    ceiling_peak: float | None = None
    ceiling_offpeak: float | None = None
    ceiling_exclusive_night: float | None = None
    # A THIRD band, and the marker that this leg follows a time-of-use
    # schedule rather than a bi-hourly one. Luminus SmartFlex is the case: it
    # prints one monthly formula per TOU slot (pleines / creuses /
    # super-creuses) and its printed rates are the previous month's.
    #
    # Carried on this kind rather than making TimeOfUseRates month-priced in
    # its own right: twenty places already gate the month-mean machinery on
    # SpotMonthlyRates, and a second month-priced kind would have to be added
    # to every one of them. Missing one is a silently unpriced hour.
    #
    # When ``factor_transition`` is set the band is chosen by ``tou_slot``
    # with ``weekend_rule``, not by the bi-hourly day/night split.
    factor_transition: float | None = None
    base_transition: float | None = None
    # The three CWaPE Impact bands, for a Tarif Impact card that indexes each
    # band monthly. Cociter Tarif Variable Trihoraire is the case: one BELIX
    # formula per band and printed rates that are the previous month's, the
    # same sentence as its variable sibling. Chosen by ``dso_impact_band``
    # rather than by the bi-hourly or time-of-use rule, and checked before
    # both, since a card prints one schedule or the other. The per-band
    # ceilings are the same cap ``ImpactRates.ceiling_*`` carries.
    factor_pic: float | None = None
    base_pic: float | None = None
    factor_medium: float | None = None
    base_medium: float | None = None
    factor_eco: float | None = None
    base_eco: float | None = None
    ceiling_pic: float | None = None
    ceiling_medium: float | None = None
    ceiling_eco: float | None = None
    # Carried from the variable card this leg re-prices: True when the month
    # mean the coefficients resolve against is the RLP-weighted one (Eneco).
    # ``_energy_month_spot`` reads it to pick the weighted mean over the plain.
    rlp_indexed: bool = False
    # Which DSO blend of the RLP profile the weighting uses (see ``RlpBlend``);
    # carried from the card, meaningful only with ``rlp_indexed``.
    rlp_blend: RlpBlend = "distinct"
    # The supplier's own published value of the index for the ONE delivery
    # month this leg is being applied to, in EUR/kWh. Set by the month splice
    # (``_effective_snapshot_for_month``) from that month's archived card, never
    # by the cohort conversion: the coefficients are the contract's, the index
    # is the month's. When present it settles the month exactly and no mean is
    # computed. Always None on the live tick's leg.
    index_realised: float | None = None
    # A card that prices a first tranche of the YEAR's volume at a flat rate
    # and only the remainder on the formula above. EnergyVision's tiered range
    # is the case: "de vaste tariefcomponent ... is van toepassing op de eerste
    # 1.800 kWh verbruik", with the rest on 1,12 x Belpex-RLP-M + 20 EUR/MWh.
    #
    # Carried as data rather than priced here, because the engine bills per
    # slot and cannot know where in the year's cumulative volume an hour sits.
    # ``resolve_volume_tier`` folds the pair into the coefficients above
    # against the entry's annual volume, so nothing downstream learns the word
    # tier: the same arrangement ``federal_excise_bands`` has with
    # ``resolve_excise_band``. Both are None on every card that prices its
    # whole volume one way, which is all of them but that range.
    tier_kwh: float | None = None
    tier_rate: float | None = None
    weekend_rule: WeekendRule = "weekend_offpeak"
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
    # Per-slot monthly coefficients, for a card that indexes each band on the
    # delivery month rather than publishing a settled rate. The printed
    # peak / transition / offpeak stay as the keyless fallback.
    month_indexed: bool = False
    formula_factor_peak: float | None = None
    formula_base_peak: float | None = None
    formula_factor_transition: float | None = None
    formula_base_transition: float | None = None
    formula_factor_offpeak: float | None = None
    formula_base_offpeak: float | None = None


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
    # With ``month_indexed`` set they are what the contract bills: the card's
    # printed bands are the previous month's index and note (7) settles the
    # delivery month on its own BELIX, so ``_cohort_energy_from_archived``
    # turns the three pairs into a banded SpotMonthlyRates leg that every
    # month-mean gate already resolves, the way the variable card's mono pair
    # is. Without the flag they stay diagnostic and the printed bands bill.
    pic_factor: float | None = None
    pic_base: float | None = None
    medium_factor: float | None = None
    medium_base: float | None = None
    eco_factor: float | None = None
    eco_base: float | None = None
    # Per-band cap on the SUPPLY price, when the card publishes one. Same
    # meaning as ``VariableRates.ceiling_*``: the customer pays the lower of
    # the indexed rate and this, and network, taxes and surcharges stay due
    # in full on top. Per band rather than one value because the column is
    # printed per row, and a card is free to cap the expensive band alone.
    ceiling_pic: float | None = None
    ceiling_medium: float | None = None
    ceiling_eco: float | None = None
    # True when every band above carries its coefficient pair AND the card
    # says the printed rate is last month's index (Cociter trihoraire). Same
    # meaning as ``VariableRates.month_indexed``.
    month_indexed: bool = False


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
    behaviour every scraped card relies on. ``minimum`` is the same clamp at a
    card-stated floor above 0.
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
    # True when ``peak`` and ``offpeak`` are the day and night register rates
    # of a bi-hourly meter rather than time-of-use slots: the card prints one
    # feed-in rate per meter register beside its consumption rates (Trevion
    # Groene Energie Vast). The pricing engine then credits a bi-hourly or
    # digital meter by register on the DSO's day/night schedule and a
    # single-register meter at ``current``. Without the flag a pair on a
    # fixed or variable card is not read at all, so no other supplier's
    # credit moves.
    bi_hourly: bool = False
    # Month coefficient pair per TOU slot, for a card whose per-slot credit is
    # itself indexed on the delivery month. Engie Empower Flextime is the case:
    # one EPEXDAM formula per Flextime band on the injection side too, and the
    # triplet above is printed at the PREVIOUS month's index. ``month_indexed``
    # names the mean they resolve against, exactly as for ``factor`` / ``base``,
    # and the printed triplet stays the fallback until that mean is known. All
    # six or none: a partial set is not a formula.
    factor_peak: float | None = None
    base_peak: float | None = None
    factor_transition: float | None = None
    base_transition: float | None = None
    factor_offpeak: float | None = None
    base_offpeak: float | None = None
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
    # True when the formula resolves against the delivery month's PLAIN
    # arithmetic mean (Eneco's Belpex-injectie, which reproduces that mean to
    # four decimals). ``spp_indexed`` is the solar-weighted sibling; a card is
    # one or the other, never both. Either way the coefficients are month
    # coefficients, so the pricing engine must never hand them an hourly spot.
    month_indexed: bool = False
    # The supplier's own published value of the index this credit is formulated
    # on, for the ONE delivery month the leg is applied to, in EUR/kWh. EBEM
    # names the month just closed on the FOLLOWING month's card ("de SPP0
    # vorige maand bedroeg 79,11"), and that figure is what it invoices, so an
    # archived month settles on it and no mean is computed here. The energy
    # leg's field of the same name does the same job for its own index; the two
    # are different indices (solar-weighted against residential-load-weighted)
    # and are settled independently. None while the month is still running, or
    # while the next card is not out yet.
    index_realised: float | None = None
    # True when the card bills the credit PER SETTLEMENT SLOT whatever the
    # energy leg does, so the printed figure is an illustration rather than the
    # rate. Bolt's fixed and variable cards say it outright: *"Le tableau
    # ci-dessus indique le prix de vente base sur la valeur Belpex la plus
    # recente. Dans la facturation, l'injection par quart d'heure est
    # multipliee par la valeur Belpex pour ce quart d'heure"*, and, on the
    # FIXED card, *"Contrairement au prix fixe de consommation ..., le prix
    # pour l'injection est quant a lui variable selon l'indice Belpex"*.
    #
    # Without it the engine prefers a printed ``current`` on any card whose
    # ENERGY is static, which is right for the cards that publish a realized
    # monthly rate and wrong for these. ``current`` is still kept, as the
    # fallback for an entry with no ENTSO-E key. Mutually exclusive with
    # ``month_indexed`` / ``spp_indexed``: a credit settles per slot or per
    # month, never both.
    slot_indexed: bool = False
    # An explicit guaranteed minimum in EUR/kWh, for a card that promises more
    # than "never negative". EnergyVision guarantees 1 c/kWh: *"Als de
    # berekening van onze formule lager zou uitkomen dan 1 EURcent/kWh, dan
    # garanderen wij in elk geval 1 EURcent/kWh. Dat wordt berekend op
    # maandbasis"*. ``floor_at_zero`` is the same clamp at 0, so a card sets
    # one or the other and the pricing engine applies whichever is present.
    minimum: float | None = None
    # True when the card taxes injection (professional cards do, at 21%).
    # None of these rates passes through the pricing engine's per-component
    # VAT gross-up, so ``apply_vat`` bakes them, like the fixed fees. Left
    # False by every residential extractor, where injection is exempt.
    vat_applies: bool = False


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
