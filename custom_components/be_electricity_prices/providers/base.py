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

No SUPPLIER EUR values live in Python source: every price, fee and coefficient
in :class:`SupplierSnapshot` comes from that supplier's own live card, and a
figure copied out of one into this package is refused however plausible it
looks. That rule is why Sibelga's power term is fetched from Brugel's
published sheet instead of being typed in.

Two REGULATED figures are the exception and they are typed in on purpose,
because no card is their source: the flat federal excise
(``FEDERAL_EXCISE_RESIDENTIAL_TVAC``) and the VREG network ceiling
(``VREG_NETWORK_CEILING_HTVA``), each reaching a snapshot through a resolver
below. Both are set by a regulator for the whole country or region, both are
cross-checked against what the fleet prints, and both carry an explicit
``KNOWN_FROM`` / ``KNOWN_UNTIL`` window so they expire into "read the card"
rather than going stale. A figure that cannot meet all three tests does not
belong here.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date

import aiohttp

from ..const import (
    WELCOME_CREDIT_PRO_RATA,
)
from ._rates import (
    Contract,
    EnergyRates,
    InjectionRates,
)


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
    :func:`_pdf.parse_brussels_osp`) are supplier-specific: some cards
    print a single databeheer line, others sum a measurement and a
    fixed-term charge, so the caller computes them and passes them in.
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
    # annual consumption instead of one rate. Professional cards do, and so
    # did the residential cards of both Engie and Mega until the scheme
    # flattened in August 2026: four tranches, the same bounds and the same
    # rates on either supplier. A card printing one row is a rate rather than
    # a schedule and leaves this None. The
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
    # it is zeroed for an entry that deducts VAT, which loses the only
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
    # True when the extractor knows this ARCHIVED month's figures can still
    # change. Eneco settles a month on the index it publishes on the next
    # card, so a month whose next card is not out yet bills the printed
    # estimate for now; the monthly snapshot cache re-fetches such a row after
    # its TTL instead of keeping it as a closed month's historical fact. Never
    # set on a live card.
    provisional: bool = False
    # One-off welcome credit the card prints, in EUR, or None where it prints
    # none. Granted only during the customer's FIRST subscription year and
    # accrued pro rata per day, so it needs a contract start date to mean
    # anything and expires on its own a year later. It is capped at what the
    # same period charged for energy, the standing charge and the green
    # electricity / CHP contribution, and never comes off network tariffs,
    # taxes or levies. Carried on the basis its card prints it, TVAC on a
    # residential card, the way the prosumer forfait above is.
    #
    # It belongs to the product VERSION signed rather than to the current
    # card: EnergyVision moved this figure four times between March and
    # September 2026 (300, 200, 250, 200), so a cohort with an archive reads it
    # from its signing month.
    welcome_credit_eur: float | None = None
    # WHEN that credit lands, which is the rule its own card states:
    #
    #   "pro_rata"    accrued by the day across the first subscription year
    #                 and capped at what that period charged for energy, the
    #                 standing charge and the green contribution. EnergyVision
    #                 footnote e.
    #   "anniversary" a lump granted on the invoice once a full year has been
    #                 consumed without interruption, with no cap stated. Frank
    #                 Energie's Dynamisch Korting: "De korting wordt toegekend
    #                 via de factuur na een jaar ononderbroken verbruik".
    #
    # The cap rides with the kind because each card states one complete rule
    # rather than two independent ones. A future card that pro-rates without a
    # cap, or caps a lump, is what would split this into two fields.
    welcome_credit_kind: str = WELCOME_CREDIT_PRO_RATA
    # What the supplier takes off the YEARLY STANDING CHARGE when the customer
    # pays by direct debit, in EUR/year, or None where the card offers no such
    # reduction, which is every card but Brusol's Groene stroom today. Read
    # off the card like every other euro: "De vaste vergoeding bedraagt
    # 250 EUR. Indien je kiest voor domiciliering dan krijg je een extra
    # korting van 20 EUR, zodat je totale vaste vergoeding 230 EUR bedraagt."
    #
    # Held as the REDUCTION rather than the reduced fee, so it cannot silently
    # disagree with ``yearly_fixed_fee`` beside it, and so the extractor can
    # check its own reading against the total the card also prints.
    #
    # Whether this household pays that way is not on the card: it is a
    # per-entry answer the config flow collects, and ``resolve_direct_debit``
    # applies it, clearing this field as it goes so no later reader can apply
    # it twice.
    direct_debit_discount_eur: float | None = None
    # A welcome credit that is a reduction on the ENERGY PRICE rather than a
    # lump: Mega's ristourne is "une reduction de 4.929 c EUR/kWh (TVA de 6%
    # incluse) sur le prix de l'energie ... pour votre premiere annee de
    # consommation nette d'electricite", beside a flat cut off the standing
    # charge. So the amount depends on how much the household uses, and
    # ``welcome_credit_eur`` alone cannot express it.
    #
    # In EUR/kWh, on NET consumption: the card says "consommation nette", and
    # a household that exports is credited on what it drew less what it put
    # back.
    welcome_credit_eur_per_kwh: float | None = None
    # What the whole credit may not exceed: "Le montant total de la ristourne
    # est plafonne a 848 EUR (TVA de 6% incluse)". None where the card states
    # no ceiling, which is every other card granting one.
    welcome_credit_cap_eur: float | None = None
    # The part of the credit a household only gets by paying by direct debit:
    # "soit une reduction de base de 37.1 EUR + 5.3 EUR supplementaires en cas
    # de paiement par domiciliation bancaire". Held as the SUPPLEMENT beside
    # the base, for the reason ``direct_debit_discount_eur`` above is, and
    # applied and cleared by the same ``resolve_direct_debit``.
    welcome_credit_direct_debit_eur: float | None = None
    # True when the card grants the WHOLE credit only to a direct-debit
    # payer, rather than a larger one: "Si vous souscrivez a un nouveau
    # contrat Smart Fixed ET OPTEZ POUR LA DOMICILIATION, vous beneficiez
    # d'une ristourne composee d'une reduction de 9.434 c EUR/kWh ... et
    # d'une reduction de 153.7 EUR". There is no reduced version to fall back
    # to: a household paying another way gets nothing, so the base, the
    # per-kWh leg and the cap all go, not just a supplement.
    #
    # A fact about the card, so it is parsed rather than listed: Mega's
    # pro Cosy Flex printed the supplement wording in some months and this
    # one in others, and a static list would bill one of the two wrongly
    # every time the card changed. ``resolve_direct_debit`` settles it and
    # clears the flag, like every other per-entry answer here.
    welcome_credit_requires_direct_debit: bool = False
    # A credit stated as a PERCENTAGE of the supplier's energy cost rather
    # than in euro or per kWh, as a fraction: Luminus's September 2026
    # campaign is "- 33,00 % De remise sur votre consommation annuel en heures
    # pleines et creuses pendant 12 mois" on Comfy and 29% on ComfyFlex.
    #
    # Kept as a ratio and resolved against the household's OWN realised energy
    # rate where the credit is computed, not baked into a per-kWh figure here.
    # A baked figure would be right only for a fixed card: ComfyFlex is
    # variable and the rate moves every month, and a cohort re-prices it
    # again. The ratio is also why this is not VAT-scaled; it is dimensionless
    # and lands on a rate that already carries the entry's basis.
    welcome_credit_pct_of_energy: float | None = None
    # A credit stated as a VOLUME of free energy, credited at that same rate:
    # "3 mois d'electricite gratuite, Cashback de 750 kWh apres 12 mois" on
    # MaxxFix and MaxxFlex. In kWh, so not VAT-scaled either.
    welcome_credit_kwh: float | None = None
    # True when the card excludes an exclusive-night-only connection from the
    # credit: "non-valable sur un compteur exclusif nuit", which Luminus's
    # own conditions repeat ("ne s'applique pas si vous consommez uniquement
    # via un compteur exclusif nuit"). Which meter this entry has is a
    # per-entry answer, so ``resolve_welcome_credit_meter`` settles it and
    # clears the flag, like every other one here.
    welcome_credit_excludes_night_meter: bool = False
    # How many uninterrupted months an ANNIVERSARY credit is granted after:
    # "La ristourne vous est uniquement accordee apres DOUZE mois
    # ininterrompus de consommation ... et octroyee sur la premiere facture de
    # regularisation apres cette periode". Twelve unless the card says
    # otherwise, which Mega's Zen Fixed and Smart Flex do: they say fourteen.
    #
    # Only WHEN it is paid. The amount stays measured over the first year,
    # which is a different sentence on the same card, so this moves the lump
    # into a later window without changing it. Inert on a pro-rata card,
    # which accrues by the day and never waits.
    welcome_credit_after_months: int = 12


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
# accessible archive for that month (an overwrite-in-place supplier like
# TotalEnergies, a card named by version rather than by month like Bolt's
# variable folder, or a month before the supplier's archive horizon); the
# month cache then asks the repository's own card archive before proxying
# the current card.
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
    # means "no archive for this month" - the coordinator then asks the
    # repository's card archive and falls back to the current snapshot as
    # a proxy.
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
    # Roughly what one card of this supplier costs to fetch and parse, in
    # seconds, on the slowest hardware this runs on. The ranking sweep orders
    # by it so a wall-clock budget spends itself on many cheap rows before a
    # few expensive ones. On the 51-contract Flanders static cell these values
    # fill 15 rows in the first ten seconds and 35 in the first sixty, against
    # 458 s to finish; ordered by supplier name the first ten seconds would
    # buy one Bolt card.
    #
    # The worst fixture card of each supplier plus 10%, not the mean: a budget
    # exists to be honoured, and a mean under-reserves for exactly the card
    # that blows it. Deliberately coarse - it schedules work, it does not
    # price anything - so it needs re-measuring only when a supplier changes
    # how it publishes, which scripts/live_check.py watches for.
    #
    # The default is a middling value rather than zero, so a provider added
    # without one is scheduled somewhere sane instead of first.
    sweep_cost_s: float = 5.0

    def regions(self) -> frozenset[str]:
        """Union of regions across this supplier's contracts."""
        out: set[str] = set()
        for c in self.contracts:
            out |= c.regions
        return frozenset(out)


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
