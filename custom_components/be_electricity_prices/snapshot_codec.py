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

"""Serialising a :class:`SupplierSnapshot` to the Store and back.

Split out of ``snapshot_store`` because it is the one part of it that depends
on nothing else in the package: it turns a card into a row and a row into a
card, and the schema-version history below is most of its length. Every
archived row is read by every installed version, so that history is the
record of what each version may find in one.
"""

from __future__ import annotations

import logging
from dataclasses import fields
from datetime import date, datetime
from typing import Any

from homeassistant.helpers.storage import Store

from .const import STORAGE_VERSION, WELCOME_CREDIT_PRO_RATA
from .providers.base import (
    DsoOverlay,
    SupplierSnapshot,
    TaxOverlay,
)
from .providers._rates import (
    DynamicRates,
    EnergyRates,
    FixedRates,
    ImpactRates,
    InjectionRates,
    SpotMonthlyRates,
    TimeOfUseRates,
    VariableRates,
)

_LOGGER = logging.getLogger(__name__)


class _MigratingStore(Store[dict[str, Any]]):
    """Store subclass that drops blobs from a previous STORAGE_VERSION.

    Every field in the persisted snapshot is re-derivable from a fresh
    extractor fetch, so wiping the cache on a major-version mismatch is
    safe and avoids HA logging the default migrator's "missing migration
    function" warning. Returning an empty dict from
    ``_async_migrate_func`` makes ``async_load`` return ``{}`` and the
    coordinator re-fetches on its first refresh.
    """

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,  # noqa: ARG002 - HA signature.
        old_data: dict[str, Any],  # noqa: ARG002 - dropped wholesale.
    ) -> dict[str, Any]:
        if old_major_version < STORAGE_VERSION:
            return {}
        return old_data


# Bump when a new field is added to the serialized snapshot so old caches
# get invalidated and re-fetched on first load instead of silently lacking
# the new field. Loading a snapshot whose schema_version is below this
# raises in _snapshot_from_dict; async_load_persistent then discards the
# cache and the coordinator's first refresh repopulates from the supplier.
# v9: DynamicRates gained ``quarter_hourly``. Bump so a cached dynamic
# snapshot from a pre-15-min release (Engie, Cociter, EBEM, Ecofix) is
# dropped and re-fetched with the flag set, rather than lingering on the
# hourly default until the snapshot next refreshes. The probe-based
# suppliers (Cociter, EBEM, Ecofix) would otherwise keep the stale flag
# for weeks, until their next monthly card changes the probe key.
# v10: OCTA+ Dynamic was missed by the v9 sweep; it indexes on the
# 15-minute Epex spot and now sets ``quarter_hourly`` too. Bump so a
# cached OCTA+ dynamic snapshot is dropped and re-fetched with the flag
# set rather than lingering on the hourly default.
# v11: snapshots gained supplier_prosumer_eur_per_kva_year (Cociter's
# compensation-regime PV forfait). Bump so a cached Cociter Variable
# snapshot is re-fetched with the forfait parsed instead of None.
# v12: InjectionRates gained per-slot peak/transition/offpeak (Engie
# Empower Flextime's per-slot feed-in tariff). Bump so a cached Flextime
# snapshot is re-fetched with the triplet instead of the flat single rate.
# v13: the July 2026 Eneco cards dropped the "/ VALORISATIE" suffix from
# the injection heading, so 0.8.3 parsed every Eneco injection to None and
# cached it. 0.8.4 fixed the anchor but probe-based freshness keeps serving
# that stale None until Eneco republishes. Bump so the mis-parsed snapshot
# is dropped and re-fetched with the injection block populated.
# v14: added the SpotMonthlyRates energy kind (expert custom monthly-average
# supplier) and the InjectionRates.floor_at_zero flag. Bump so a cached
# snapshot from before the field existed is dropped and rebuilt with it.
# v15: VariableRates gained formula_factor / formula_base (numeric BELIX-style
# coefficients) so a variable contract with a contract start date re-prices its
# signing cohort against the current month's mean. Bump so a cached variable
# snapshot from before the fields existed is dropped and re-parsed with them.
# v16: the persisted snapshot now holds the card as parsed rather than as
# priced, so the entry's VAT preference is re-applied on load, and TaxOverlay
# gained federal_excise_bands for cards that print the special excise as a
# degressive schedule by annual consumption, and InjectionRates gained
# vat_applies for cards that tax injection. Bump so a cache written under any
# of the old meanings is dropped.
# v17: TaxOverlay gained region_connection_fee_unavailable, for a Walloon card
# that stopped printing the connection-fee row. Bump so an EnergyVision Wallonia
# entry stranded on its July snapshot by the August tax-block change drops that
# cache and re-parses the current card instead of waiting for the probe key to
# move.
# v18: three extractor fixes changed what is parsed into the snapshot, and all
# three suppliers are probe-based, so without this bump an existing entry keeps
# serving the wrong figures until its supplier republishes a card, up to a month
# later. Ecopower stopped baking 6% VAT into databeheer / capacity / the
# subscription, Mega now parses the Flemish energy fund instead of hardcoding
# 0.0, and Bolt reads the non-residential fund row on professional contracts.
# The rule this keeps tripping over: the persisted snapshot holds the card AS
# PARSED, so any change to what an extractor produces needs this version moved
# with it. A change that only affects how a stored card is priced (apply_vat,
# resolve_excise_band) does not, since those run on load.
# v19: Mega's realized-rate parser dropped a negative injection rate, so a
# variable or Impact entry fell back to the 12-month simulation table and
# credited a rate the card charges. Mega is probe-based and the May cards are
# already published, so their probe key will not move again; without this bump
# an affected entry keeps the wrong sign indefinitely.
# v20: two extractors were resolving a superseded card URL, so the cache holds a
# card that parsed perfectly and is simply the wrong one. Bolt pinned the
# variable-family version suffix and served June's formula for ten weeks after
# the August revision shipped; Ecopower's six-digit filename pattern could not
# see the YYYYMMDD card that replaced it and kept serving January's tax block.
# Neither supplier's probe key moves on its own here: the pinned URL's card is
# unchanged, which is exactly why nothing noticed, so without this bump an
# existing entry keeps the stale prices indefinitely.
# v21: InjectionRates gained spp_indexed, and energie.be Variabel now parses
# its injection FORMULA rather than only the card's printed indicative. Two
# reasons to move the version with it. A snapshot written before this holds no
# factor/base for that contract, so the entry keeps crediting the VNR forecast
# instead of the realized Belpex_SPP month until the 24 h TTL happens to
# refetch; and every earlier InjectionRates field (per-slot rates, floor_at_zero,
# vat_applies) bumped for the same reason, since _snapshot_from_dict splats the
# stored dict straight into the dataclass and an unknown key is a TypeError.
# v22: energie.be Vast now parses the same injection formula, having only
# emitted the printed indicative before. Same reason as v21: a snapshot written
# earlier carries no factor/base for that contract, so the entry would keep
# crediting the VNR forecast (measured 3,6x the contractual credit in April
# 2026) until the 24 h TTL happened to refetch.
# v23: Mega Cap now parses the contractual ceiling on the energy component
# ("vous payez le minimum entre les prix variables mensuels et ce plafond").
# A snapshot written earlier carries no ceiling, so the entry would price
# straight through the cap the customer is protected by until the card
# happened to refetch, which is exactly when the cap matters.
# v24: the three Brussels extractors now carry Sibelga's power term for a
# connection ABOVE 13 kVA, and parse_brussels_osp reads every band the card
# prints rather than the four at or below 13 kVA. A snapshot written earlier
# has neither, so a connection above the line would keep being billed the
# smaller term and an OSP fee its tier does not have.
# v25: EnergyVision now parses the "maximumtarief" column, the VREG ceiling on
# capacity plus the per-kWh network term, which the card printed and nothing
# read. A snapshot written earlier carries no ceiling.
# v26: Mega's variable cards print one indexation formula per meter and only
# the mono one was parsed, so a bi-hourly signing cohort was re-priced onto it
# for every hour. A snapshot written earlier carries no band coefficients.
# v27: Cociter Tarif Variable now carries month_indexed, so its rate resolves
# against the DELIVERY month's BELIX rather than the printed indicative, which
# the card computes from the previous month's. A snapshot written earlier has
# the flag absent and would keep billing a month late.
# v28: Eneco Power Fix and Flex now surface their injection coefficients with
# InjectionRates.month_indexed, so the credit resolves against the DELIVERY
# month's Belpex-injectie instead of the printed indicative, which the card
# computes from the last known (previous) month's. A snapshot written earlier
# carries neither the coefficients nor the flag.
# v29: EBEM Groen Variabel and B@sic+ now surface their BelpexSPP0 injection
# coefficients with spp_indexed, so the credit resolves against the DELIVERY
# month's solar-weighted mean instead of the printed figure, which the card
# computes from "de SPP0 vorige maand". A snapshot written earlier has neither.
# v30: DATS 24 Groen Variabel now surfaces its BE_spotSPP injection
# coefficients with spp_indexed, for the same reason as v29: the card's printed
# figure is filled in from "de meest recente waarde" of that index, which is the
# previous month's. A snapshot written earlier has neither.
# v31: the EnergyVision fixed cards (Flanders 3-jaar, Wallonia 1-an) now
# surface their "0,6 x Belpex-SPP-M - 15 EUR/MWh" coefficients with
# spp_indexed and the card's 1 c/kWh guarantee as InjectionRates.minimum, so
# the credit resolves against the delivery month's solar-weighted mean instead
# of a printed figure the card says is not that month's. A snapshot written
# earlier has none of the three.
# v32: the six non-dynamic OCTA+ cards now surface their "Epex SPP x 0,852 -
# 13,39" coefficients with spp_indexed. Their printed c/kWh sits in the card's
# "Prix estimes" column and the card says the month's Epex is only known at
# month-end, so a snapshot written earlier carries the estimate and no formula.
# v33: the OCTA+ SPP injection regex now accepts the August 2026 card, which
# renamed the parameter to "Epex SPP M" and swapped the x for a star. v32 was
# written against April cards only, so every live card fell back to the printed
# V-test estimate and a cached v32 snapshot carries no coefficients at all.
# v34: the OCTA+ variable cards now carry their monthly Epex RLP coefficients
# per meter, including a separate night-circuit pair, and month_indexed. A v33
# snapshot holds only the card's V-test 12-month forward estimate.
# v35: every non-dynamic Bolt card now carries the quarter-hourly Belpex
# injection formula its own text describes, flagged slot_indexed, beside the
# illustrative figure it used to credit flat. A v34 snapshot holds the figure
# alone, which is a quarterly-lagged constant that can never go negative.
# v36: Ecofix Flexy now surfaces its BELPEX-SPP-M injection coefficients with
# spp_indexed. A v35 snapshot carries only the printed Maandprijs, which runs
# two months behind the index the card says it settles on.
# v37: Engie Empower Variable and Empty House (and their pro twins) now carry
# their monthly EPEXDAM consumption coefficients and month_indexed. A v36
# snapshot holds only the printed price, which the card itself labels as
# computed from the last KNOWN month rather than the delivery one.
# v38: the eight EPEXDAM-indexed Engie variable contracts now carry their
# monthly injection coefficients and month_indexed. A v37 snapshot holds only
# the printed Injection(3) figure, which is that formula at the PREVIOUS
# month's index.
# v39: the eight non-dynamic TotalEnergies contracts now carry their monthly
# Belpex_M injection coefficients and month_indexed. TotalEnergies is
# probe-based, so a cached v38 snapshot is not re-parsed without this and keeps
# serving the previous month's printed figure.
# v40: the nine Mega variable and Impact contracts now carry their monthly
# "Epex SPP * 0,85 - 2,2" injection coefficients with spp_indexed. A v39
# snapshot holds only the printed figure, which is the previous month's
# regularisation.
# v41: the seven monthly-indexed Luminus contracts now carry their injection
# coefficients and month_indexed. A v40 snapshot holds only the printed figure,
# which the card says is the previous month's.
# v42: Luminus MaxxFlex now carries its monthly Belpex ENERGY coefficients per
# meter, including the night circuit, and month_indexed. A v41 snapshot holds
# only the printed rate, which is the formula at the previous month's index.
# v43: Ecopower Groene Burgerstroom now carries the blended coefficients of its
# 50/50 fixed-plus-SPP feed-in credit, with spp_indexed and the card's
# never-negative floor. A v42 snapshot holds only the printed figure, which for
# an arrears publisher is always a settled past month.
# v44: Cociter Variable now carries a BELIX coefficient pair per meter, the
# night circuit included, where a v43 snapshot held only the mono pair and
# billed every meter on it.
# v45: EBEM Groen Variabel now converts all four of its per-meter formula rows
# into cohort coefficients. A v44 snapshot carries only the mono pair, which
# every meter was then billed on.
# v46: the Mega variable cards now carry the fourth per-meter formula, the
# dedicated night circuit, which a v45 snapshot billed on the mono pair.
# v47: energie.be, DATS 24 and Ecopower now carry the VREG maximumtarief on
# their Flemish overlays. All three printed it and none stored it, so a
# low-volume connection was quoted its uncapped network leg.
# v48: SpotMonthlyRates carries the four ceiling columns, so a Mega Cap
# signing cohort keeps the contractual cap its card guarantees for the year.
# v49: Luminus SmartFlex now carries a monthly coefficient pair per TOU band
# and month_indexed, and SpotMonthlyRates gained the third band plus the
# weekend rule to price them. A v48 snapshot holds only the printed triplet,
# which is the previous month's.
# v50: Bolt's Walloon variable cards now carry the CWaPE incitative supplier
# energy bands beside their standard rates, so an entry on the incitative DSO
# mode bills both halves on the same schedule. A v49 snapshot has the network
# side banded and the energy side flat.
# v51: the Cociter variable and trihoraire cards now carry the "Prix maximum"
# column their note (8) says overrides the indexed rate, on VariableRates and
# on the new ImpactRates ceilings. A v50 snapshot has no cap at all, so a
# month whose BELIX mean cleared it would bill the uncapped formula.
# v52: the Cociter trihoraire card carries month_indexed on its ImpactRates,
# so the three band formulas re-price on the delivery month's BELIX the way
# the variable card's do. A v51 snapshot bills the printed bands, which are
# the previous month's index, and Cociter's probe only flips on a new card.
# v53: Engie Empower Flextime carries a month coefficient pair per band on
# both legs (TimeOfUseRates formula_* and the InjectionRates per-slot pairs)
# plus month_indexed, so the delivery month is billed on its own EPEXDAM. A
# v52 snapshot holds only the printed triplets, which are last month's.
# v54: Eneco Zon & Wind Flex and Flex One carry month_indexed and rlp_indexed,
# so the delivery month is billed on its own RLP-weighted Belpex-RLP-M rather
# than the previous month's figure the card prints. A v53 snapshot bills the
# printed estimate for the whole month.
# v55: Energy Knights Essentia carries rlp_indexed with the flanders blend, so
# its BelpexRLP offtake resolves against the Fluvius curve rather than the
# plain arithmetic mean it billed about 5% low on.
# v56: energie.be Variabel carries rlp_indexed with the columns blend, so its
# Belpex_RLP resolves against the column-weighted profile rather than the plain
# mean.
# v57: Bolt's variable cards carry formula_factor / formula_base, the printed
# Belpex coefficients its quarter-hourly settlement is billed on. A v56
# snapshot has neither, so an entry that ticked the settlement box would keep
# being billed the printed monthly rate until Bolt next republished, which is
# exactly the stale-parse case this version exists for.
# v58: snapshots carry welcome_credit_eur, the one-off first-year credit four
# of EnergyVision's cards print. A v57 snapshot has None there, so an entry
# with a contract start date would be credited nothing until its supplier next
# republished, which on a monthly card is up to a month.
# v59: welcome_credit_kind joins it, and Frank Energie's three cards that grant
# a cashback now parse theirs. A v58 blob carries neither, so a Frank entry
# would keep being ranked as though its tier's whole reason for existing were
# not there.
# v60: no field moved. Trevion's dynamic and monthly formulas were parsed a
# factor of ten small until 0.22.1 (the card prints c€/kWh against a Belpex in
# EUR/MWh), and that release did not bump this, so an entry set up on 0.21.0
# or 0.22.0 went on serving the tenth-of-the-card price out of its own store:
# Trevion's probe is a HEAD on its listing page, which a code release never
# moves, and the gate below refuses only an OLDER schema. Same class as v18.
# v61: EBEM's variable months settle on the index the FOLLOWING card publishes
# rather than on the estimate their own card prints. A closed month is cached
# as a historical fact and persisted, so every month row an entry already
# holds carries the estimate and nothing would ever re-ask for it; this is the
# bump that drops them. Measured over the nine archived 2026 cards the
# estimate was the previous month's settled rate every time, and a 3.500 kWh
# year-to-date ran 16,55 EUR under after eight months.
# v62: the same EBEM months settle their FEED-IN leg too, on the SPP0 the
# following card publishes ("de SPP0 vorige maand bedroeg 79,11"), carried on
# InjectionRates.index_realised. A v61 row holds only the printed estimate and
# leaves the credit to the SPP-weighted mean computed here, which ran about
# 0,9 EUR/MWh high in each of the first eight months of 2026 and so over-paid
# the credit by roughly 1,8 EUR a year at 2.000 kWh injected.
# v63: Trevion's monthly months settle BOTH legs on the indices its following
# card names ("de laatst gekende waarde is deze van augustus 2026"), the
# Belpex_RLP_VL for energy and the Belpex_SPP_BE for the credit. A v62 row was
# computed from the spot cache on hourly prices, while the card defines both
# indices on quarter-hour ones.
# v64: Mega's variable cards carry rlp_indexed. The card says its monthly
# index is "la moyenne des valeurs quart-horaires Day-Ahead EPEX SPOT Belgium,
# ponderee par le RLP (publie par Synergrid)", where only the formula line was
# being read and it names the index "Epex" alone. A v63 row resolves the
# coefficients against the plain arithmetic mean, which sits 2,4 to 7,6 percent
# below the index Mega settles at, about 21 EUR a year at 3500 kWh.
# v65: Luminus and Frank carry network_ceiling_eur_per_kwh. Both state the
# VREG maximumtarief in a footnote under the DSO table rather than as a column
# of it, so it was never read and the cap never bound on their 15 Flemish
# contracts. A v64 row has no ceiling, and a low-volume connection on a high
# peak is billed a capacity term the regulator caps.
# v66: TotalEnergies' variable cards carry month_indexed and rlp_indexed with
# the four per-meter BELPEXM_RLP coefficient pairs. The card prints its rates
# beside the words "calcules sur base de la derniere valeur connue du
# BELPEX_M_RLP (du mois precedent)", so a v65 row bills a month behind where
# the formula indexes on the delivery month.
# v67: Mega's cards carry their ristourne. It is a reduction on the ENERGY
# PRICE plus a flat cut off the standing charge, capped, and granted only
# after twelve uninterrupted months, so the snapshot gains a per-kWh term, a
# ceiling and the direct-debit supplement the card prints beside the base. A
# v66 row has none of them and quotes Mega against Frank and EnergyVision as
# though nobody was ever granted one.
# v68: Mega's cards carry welcome_credit_requires_direct_debit. Four of them
# grant the whole ristourne only to a direct-debit payer ("Si vous souscrivez
# a un nouveau contrat Cosy Flex et optez pour la domiciliation, vous
# beneficiez d'une ristourne composee de ..."), stating no reduced
# alternative, where the other thirteen grant a larger one. A v67 row has the
# euros and not the condition, so it credits 522,58 EUR at 3500 kWh to a
# household the card grants nothing.
# v69: Mega's cards carry welcome_credit_after_months. Zen Fixed and Smart
# Flex, and their pro twins, grant the ristourne "apres QUATORZE mois
# ininterrompus" where every other card says twelve, and it is paid on the
# first regularisation invoice after that. A v68 row waits a year, so a
# December signing is credited 320,65 EUR in the wrong calendar year.
# v70: Luminus's cards carry their new-customer campaign, as
# welcome_credit_pct_of_energy, welcome_credit_kwh and
# welcome_credit_excludes_night_meter. Six September 2026 contracts run one
# ("remise de 33% sur les couts energetiques ... pour la conclusion d'un
# contrat ... en septembre 2026", or 750 kWh paid as a cashback), and a v69
# row carries none of it, so a household that signed in that month is
# credited nothing where its card grants up to 254,56 EUR at 3500 kWh.
_SNAPSHOT_SCHEMA_VERSION = 70

# The oldest stored schema a rejected blob may still be replayed from when no
# fetch can ever replace it (see _SnapshotMixin._replay_stale_snapshot). v16 is
# the floor because it is the one bump in fifty that changed what a stored
# field MEANS rather than adding one: before it the blob held the card as
# priced, so replaying it through _resolve_snapshot would gross a professional
# entry's rates by its VAT rate a second time. Every later bump either adds a
# field, which loads at the dataclass default, or corrects one supplier's
# parsed value, which is drift on a card already being served months late and
# is what the stale-snapshot card says out loud. Move this only for a bump that
# changes a meaning, never for one that adds a field or corrects a value, or
# the replay stops rescuing anybody.
_DEGRADED_MIN_SCHEMA_VERSION = 16


# InjectionRates fields added after the card archive went live (0.20.13),
# in the order they came. Every installed version reads the archive, and one
# that does not know a field cannot decode a row carrying it and falls back
# to the supplier tier for that month; a row rewritten with such a field at
# its default is not a changed card either, and would have cost a commit
# per row. So a field at its default is left out, and only a card that sets
# it carries it: those rows are new cards no earlier version asks for.
# ``index_realised`` joined the list when the EBEM and Trevion settlements
# added it: measured on the 1.686 stored rows, 1.683 carry an injection leg
# and every one of them would have been rewritten with the field at null.
_INJECTION_OPTIONAL_KEYS = ("bi_hourly", "index_realised")
_INJECTION_DEFAULTS = {f.name: f.default for f in fields(InjectionRates)}


def _injection_to_dict(inj: InjectionRates) -> dict[str, Any]:
    """The leg as a row, without the optional fields it does not use.

    A field is left out when it still holds its dataclass DEFAULT, not when
    it is merely falsy. The difference matters for ``index_realised``: a month
    whose index settled at exactly 0 EUR/MWh is a settled month, and dropping
    that row's field would send the pricing engine back to computing a mean
    for a month the supplier has already published.
    """
    data = dict(inj.__dict__)
    for key in _INJECTION_OPTIONAL_KEYS:
        if data.get(key) == _INJECTION_DEFAULTS[key]:
            data.pop(key, None)
    return data


def _known_fields(cls: type, data: dict[str, Any], what: str) -> dict[str, Any]:
    """``data`` with the keys ``cls`` does not declare taken out.

    Every archived row is read by EVERY installed version, so a field added
    later arrives at an older reader as an unexpected keyword and takes the
    whole row down with a TypeError: the card is dropped, the entry falls back
    to the supplier tier, and the only trace is a debug line.

    This was the injection leg's rule alone, and the injection leg is one of
    the eight dataclasses a row is rebuilt from. Nothing is exposed today, but
    the next field added to an overlay or a rate class is, and the failure
    lands on users who are not the ones upgrading.
    """
    known = {f.name for f in fields(cls)}
    unknown = sorted(set(data) - known)
    if unknown:
        _LOGGER.debug("%s carries fields this version does not know: %s", what, unknown)
    return {k: v for k, v in data.items() if k in known}


def _injection_from_dict(data: dict[str, Any]) -> InjectionRates:
    """Rebuild the injection leg, dropping a field this version does not
    know: a row written by a later version is still a card."""
    return InjectionRates(**_known_fields(InjectionRates, data, "injection"))


def _snapshot_to_dict(
    snap: SupplierSnapshot,
    fetched_at: datetime,
    probe_key: str | None = None,
    *,
    schema_version: int = _SNAPSHOT_SCHEMA_VERSION,
) -> dict[str, Any]:
    """Serialise one snapshot for the Store.

    ``schema_version`` is what gets stamped, and it is an argument only so a
    replayed blob is written back under the version it was parsed by rather
    than the running one. Stamping it current would launder a v16 card into
    looking freshly parsed, and the next release whose parser fix is meant to
    reach this user would have nothing left to invalidate. Keyword-only,
    because it is the one argument here that goes silently wrong rather than
    loudly wrong if a caller gets the order out.
    """
    return {
        "_cached_at": fetched_at.isoformat(),
        "_probe_key": probe_key,
        "_schema_version": schema_version,
        "supplier": snap.supplier,
        "contract": snap.contract,
        "energy_kind": _energy_kind(snap.energy),
        "energy": snap.energy.__dict__,
        "dsos": {k: v.__dict__ for k, v in snap.dsos.items()},
        "taxes": snap.taxes.__dict__,
        "source_url": snap.source_url,
        "publication_label": snap.publication_label,
        "valid_until": snap.valid_until.isoformat() if snap.valid_until else None,
        "injection": _injection_to_dict(snap.injection) if snap.injection else None,
        "supplier_prosumer_eur_per_kva_year": snap.supplier_prosumer_eur_per_kva_year,
        "welcome_credit_eur": snap.welcome_credit_eur,
        "direct_debit_discount_eur": snap.direct_debit_discount_eur,
        "welcome_credit_eur_per_kwh": snap.welcome_credit_eur_per_kwh,
        "welcome_credit_cap_eur": snap.welcome_credit_cap_eur,
        "welcome_credit_direct_debit_eur": snap.welcome_credit_direct_debit_eur,
        "welcome_credit_requires_direct_debit": (
            snap.welcome_credit_requires_direct_debit
        ),
        "welcome_credit_after_months": snap.welcome_credit_after_months,
        "welcome_credit_pct_of_energy": snap.welcome_credit_pct_of_energy,
        "welcome_credit_kwh": snap.welcome_credit_kwh,
        "welcome_credit_excludes_night_meter": (
            snap.welcome_credit_excludes_night_meter
        ),
        "welcome_credit_kind": snap.welcome_credit_kind,
    }


def _taxes_from_dict(data: dict[str, Any]) -> TaxOverlay:
    """Rebuild a TaxOverlay, restoring the excise bands' tuple shape.

    JSON has no tuples: a banded excise round-trips as a list of lists and
    has to be put back the way the dataclass declares it.
    """
    data = _known_fields(TaxOverlay, data, "taxes")
    bands = data.get("federal_excise_bands")
    if bands is None:
        return TaxOverlay(**data)
    return TaxOverlay(
        **{**data, "federal_excise_bands": tuple((b[0], b[1]) for b in bands)}
    )


def _snapshot_from_dict(
    data: dict[str, Any], *, min_schema_version: int = _SNAPSHOT_SCHEMA_VERSION
) -> SupplierSnapshot:
    """Rebuild a snapshot from its stored dict.

    ``min_schema_version`` is the oldest schema the caller will read. The
    default is the running one, which is the healing gate: anything older is
    refused so the next refresh re-parses the card with the current extractor.
    The replay path lowers it to ``_DEGRADED_MIN_SCHEMA_VERSION``, because for
    a supplier publishing page images there is no next refresh to heal with.
    """
    if data.get("_schema_version", 1) < min_schema_version:
        raise ValueError(
            "snapshot schema is older than the running integration; "
            "discarding cache so the next refresh re-fetches"
        )
    energy_kind = data["energy_kind"]
    energy_args = data["energy"]
    energy: EnergyRates
    if energy_kind == "fixed":
        energy_args = _known_fields(FixedRates, energy_args, energy_kind)
        energy = FixedRates(**energy_args)
    elif energy_kind == "variable":
        energy_args = _known_fields(VariableRates, energy_args, energy_kind)
        energy = VariableRates(**energy_args)
    elif energy_kind == "dynamic":
        energy_args = _known_fields(DynamicRates, energy_args, energy_kind)
        energy = DynamicRates(**energy_args)
    elif energy_kind == "tou":
        energy_args = _known_fields(TimeOfUseRates, energy_args, energy_kind)
        energy = TimeOfUseRates(**energy_args)
    elif energy_kind == "tou_impact":
        energy_args = _known_fields(ImpactRates, energy_args, energy_kind)
        energy = ImpactRates(**energy_args)
    elif energy_kind == "spot_monthly":
        energy_args = _known_fields(SpotMonthlyRates, energy_args, energy_kind)
        energy = SpotMonthlyRates(**energy_args)
    else:
        raise ValueError(f"unknown energy kind {energy_kind!r}")
    injection_data = data.get("injection")
    valid_until_iso = data.get("valid_until")
    valid_until: date | None = None
    if isinstance(valid_until_iso, str):
        try:
            valid_until = date.fromisoformat(valid_until_iso)
        except ValueError:
            valid_until = None
    return SupplierSnapshot(
        supplier=data["supplier"],
        contract=data["contract"],
        energy=energy,
        dsos={
            k: DsoOverlay(**_known_fields(DsoOverlay, v, "dso overlay"))
            for k, v in data["dsos"].items()
        },
        taxes=_taxes_from_dict(data["taxes"]),
        source_url=data["source_url"],
        publication_label=data.get("publication_label", ""),
        valid_until=valid_until,
        injection=_injection_from_dict(injection_data) if injection_data else None,
        supplier_prosumer_eur_per_kva_year=data.get(
            "supplier_prosumer_eur_per_kva_year"
        ),
        welcome_credit_eur=data.get("welcome_credit_eur"),
        direct_debit_discount_eur=data.get("direct_debit_discount_eur"),
        welcome_credit_eur_per_kwh=data.get("welcome_credit_eur_per_kwh"),
        welcome_credit_cap_eur=data.get("welcome_credit_cap_eur"),
        welcome_credit_direct_debit_eur=data.get("welcome_credit_direct_debit_eur"),
        welcome_credit_requires_direct_debit=bool(
            data.get("welcome_credit_requires_direct_debit")
        ),
        welcome_credit_after_months=int(data.get("welcome_credit_after_months") or 12),
        welcome_credit_pct_of_energy=data.get("welcome_credit_pct_of_energy"),
        welcome_credit_kwh=data.get("welcome_credit_kwh"),
        welcome_credit_excludes_night_meter=bool(
            data.get("welcome_credit_excludes_night_meter")
        ),
        welcome_credit_kind=data.get("welcome_credit_kind", WELCOME_CREDIT_PRO_RATA),
    )


def _energy_kind(energy: EnergyRates) -> str:
    if isinstance(energy, FixedRates):
        return "fixed"
    if isinstance(energy, VariableRates):
        return "variable"
    if isinstance(energy, DynamicRates):
        return "dynamic"
    if isinstance(energy, TimeOfUseRates):
        return "tou"
    if isinstance(energy, ImpactRates):
        return "tou_impact"
    if isinstance(energy, SpotMonthlyRates):
        return "spot_monthly"
    raise TypeError(f"unknown energy rates type {type(energy).__name__}")
