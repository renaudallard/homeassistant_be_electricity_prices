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

"""EnergyVision tariff extractor.

EnergyVision publishes its "Goedkope stroom" residential cards as monthly
PDFs named ``EV-<MMYY>-<CODE>-<lang>.pdf`` under
``/sites/default/files/inline-files/``. The filenames carry the pricing
month (``EV-0726-...`` = July 2026) and the server adds Drupal dedup
suffixes (the fixed card ships as ``EV-0726-GS3JV-nl_0.pdf``), so a
constructed URL would miss it. The fetch therefore scrapes the current
card href off the tariefkaart listing page (the Mega / Frank shape).

A product is published per region, one card each: the Flemish cards are
``-nl``, the Walloon ones ``-WAL-fr`` and the Brussels ones ``-BXL-nl``.
So ``_ContractDef`` holds a ``_CardDef`` per region rather than one filename
token, and that card says which site, which index page and which archive
layout the region's publication uses.

Brussels is not on this site at all. EnergyVision sells there as **Brusol**,
on ``brusol.be``, which advertises each current card on the page you sign up
from and files the archived ones under the month it uploaded them rather than
the month they price. The Brussels cards are published in both languages and
the Dutch one is worded exactly like the Flemish card of the same product, so
the energy leg needs no second parser; only the Sibelga network row and the
Brussels tax block are the region's own.

The residential electricity products supported:

* ``GSDYN`` (Goedkope Stroom Dynamisch, Flanders): quarter-hourly Belpex
  formula, the same EUR/MWh HTVA axis as Bolt / Frank. The coefficient is a
  dimensionless Belpex multiplier (NOT scaled by ten the way Frank's
  cents-output coefficient is), the base goes EUR/MWh to EUR/kWh, and 6%
  VAT is baked into both. The injection coefficient is exactly 1,0.
* ``GS3JV`` (Goedkope stroom 3 jaar vast, Flanders): a flat fixed rate for
  3 years; its injection is indexed monthly (Belpex-SPP-M, known at
  month-end), so the printed monthly indicative is billed rather than a
  live spot formula.
* ``GS1JV`` (Électricité bon marché 1 an fixe, Wallonia): the same fixed
  shape on a 1-year lock, off a French card that shares no wording with the
  Dutch ones. Parsed by the ``*_fr`` helpers below. This is where DATS 24's
  Walloon customers land after the 2026-08-31 transfer.

* ``GS1800V`` (Flanders and Brussels) / ``GSVI3`` / ``GSLP`` (Flanders):
  the tiered range, which bills a first tranche of the YEAR at a flat rate
  and the remainder on ``factor x Belpex-RLP-M + 20 EUR/MWh``. Parsed as a
  ``SpotMonthlyRates`` leg carrying the tranche, which ``resolve_volume_tier``
  folds into the coefficients against the entry's annual volume. GSVI3 fixes
  its feed-in price instead of indexing it, which is the only shape
  difference. The two GS1800V cards print the same energy leg figure for
  figure and differ only below it.

Out of scope: gas (``GSG``, ``GS1JVG``) and the two tiered products that also
price self-consumed solar (``GSEZ``, ``GSEZLP``): their "Groene stroom uit
zonnepanelen op je dak" row is a third energy leg with no representation in
the model.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

import aiohttp

from ..const import (
    FLUVIUS_AREA_LABELS_UPPER,
    DSO_AIEG,
    DSO_AIESH,
    DSO_ORES,
    DSO_RESA,
    DSO_REW,
    DSO_SIBELGA,
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from ._pdf import (
    NUM_NO_THOUSANDS,
    archive_validity_check,
    regional_tax_overlay,
    SIGN_CHARS,
    fetch_pdf_text_layout,
    fetch_text,
    head_freshness_key,
    numeric_row,
    parse_sibelga_row,
    parse_sign,
    parse_valid_until,
    tier_bound_kwh,
    to_float,
    vat_multiplier,
    is_transient_fetch_error,
)
from .base import (
    walloon_dso_overlay,
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    InjectionRates,
    SpotMonthlyRates,
    SupplierExtractor,
    SupplierSnapshot,
    TariffKind,
    TaxOverlay,
)

_SITE_BASE = "https://www.energyvision.be"
# One listing page carries every card on the EnergyVision site, Flemish and
# Walloon alike, so the freshness probe covers both.
_LISTING_URL = f"{_SITE_BASE}/nl-be/tariefkaart"

# EnergyVision sells in Brussels under the Brusol brand, off its own site.
# Nothing on energyvision.be links to it.
_BRUSOL_SITE = "https://www.brusol.be"
# Brusol has no equivalent of the tariefkaart listing: each product's current
# card is advertised on the page you sign up for it from. It does publish an
# archive page, but that one stopped being updated in May 2026 and lists every
# product it has ever sold, so it is the wrong page to read for either job.
# The FR path segment on the NL page is Brusol's own alias, not a typo.
_BRUSOL_GS1800V_URL = (
    f"{_BRUSOL_SITE}/nl/%C3%A9lectricit%C3%A9-et-gaz"
    "/schrijf-je-in-voor-goedkope-stroom-van-brusol"
)
_BRUSOL_GRS_URL = f"{_BRUSOL_SITE}/nl/schrijf-je-in-voor-groene-stroom-van-brusol"


@dataclass(frozen=True)
class _CardDef:
    """Where one product's card for one region is published.

    A product is published per region, and the publication is not one
    catalogue: the region decides the filename token, the site and the page
    the current card is resolved off.
    """

    # Filename language / region token, the part between the product code and
    # the Drupal dedup suffix: "nl" for the Flemish cards, "WAL-fr" for the
    # Walloon ones.
    token: str
    # Site the card and the page that advertises it live on.
    site: str = _SITE_BASE
    # Page whose HTML carries the current card's href.
    index_url: str = _LISTING_URL
    # How an archived card is located under <site>/sites/default/files/.
    # False is EnergyVision's flat "inline-files" folder, where the filename
    # alone locates every month; True is Brusol, which files each card under
    # the month it uploaded it.
    archive_by_upload_month: bool = False


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    code: str  # EV filename product code (GSDYN, GS3JV, ...)
    # One card per region this product is sold in. Most products are sold in
    # one region only, but not all: GS1800V is published for Flanders and,
    # under the Brusol brand, for Brussels, off two different sites. The
    # mapping makes this class unhashable, which nothing needs it to be.
    cards: Mapping[str, _CardDef]
    # Whether a spot_monthly card bills a first tranche of the year at a flat
    # rate before the indexed remainder. Declared rather than discovered: a
    # tiered card whose tranche row went missing has drifted and must fail
    # loud, not quietly bill every kWh at the indexed rate.
    tranche: bool = True

    @property
    def regions(self) -> frozenset[str]:
        return frozenset(self.cards)

    def card(self, region: str) -> _CardDef | None:
        return self.cards.get(region)


_FLANDERS_CARD = _CardDef(token="nl")


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "energyvision_dynamic",
        "EnergyVision Dynamisch",
        "dynamic",
        "GSDYN",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    _ContractDef(
        "energyvision_fixed_3y",
        "EnergyVision 3 jaar vast",
        "fixed",
        "GS3JV",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    # Wallonia's own fixed product, on a French card. It is a 1-year lock
    # where Flanders gets 3, so it is a distinct contract rather than the same
    # one in another region.
    #
    # It used to say here that this is where DATS 24's Walloon customers land
    # after the 2026-08-31 transfer. Nothing sourced that: DATS 24's own site
    # named EnergyVision and no product. Issue #100's reporter, who was
    # actually transferred, landed on the legacy DATS 24 card continued under
    # EnergyVision's name, and that card covers Wallonia on its face, so the
    # Walloon customers plausibly went the same way. Unknown either way, and
    # the Repairs card no longer tells anyone which product to pick.
    _ContractDef(
        "energyvision_fixed_1y",
        "EnergyVision 1 an fixe",
        "fixed",
        "GS1JV",
        {REGION_WALLONIA: _CardDef(token="WAL-fr")},
    ),
    # The tiered range. All three share one shape and differ only in the
    # tranche, the flat rate, the coefficient, the standing charge and how the
    # feed-in is priced, so one parser reads the three of them.
    # The one product sold in all three regions. The Dutch Brussels card is
    # worded exactly like the Flemish one and prints the same energy leg
    # figure for figure, so it goes through the same parser; only the network
    # and tax blocks are the region's own. The Walloon card is the separate
    # French publication and needs the *_fr anchors, but the same body behind
    # them, and it charges NO standing charge where the other two charge 50.
    #
    # Who may take it differs too, which the config flow cannot express and
    # the README says instead: Flanders and Wallonia sell it to any
    # residential customer, Brussels only to roofs already carrying
    # EnergyVision/Brusol panels.
    _ContractDef(
        "energyvision_tiered_1800",
        "EnergyVision 1.800 kWh vast",
        "spot_monthly",
        "GS1800V",
        {
            REGION_FLANDERS: _FLANDERS_CARD,
            REGION_WALLONIA: _CardDef(token="WAL-fr"),
            REGION_BRUSSELS: _CardDef(
                token="BXL-nl",
                site=_BRUSOL_SITE,
                index_url=_BRUSOL_GS1800V_URL,
                archive_by_upload_month=True,
            ),
        },
    ),
    _ContractDef(
        "energyvision_fixed_injection_3y",
        "EnergyVision vaste injectieprijs 3 jaar",
        "spot_monthly",
        "GSVI3",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    _ContractDef(
        "energyvision_laadpunt",
        "EnergyVision Laadpunt",
        "spot_monthly",
        "GSLP",
        {REGION_FLANDERS: _FLANDERS_CARD},
    ),
    # Brusol's other Brussels product, and the one any household can sign:
    # GS1800V is sold only to roofs carrying EnergyVision/Brusol panels,
    # while this card's condition 3 asks for nothing but a residential
    # connection. Same monthly RLP index as the tiered range with no tranche
    # in front of it, so it is a spot_monthly card that bills the formula
    # from the first kWh.
    #
    # It names Belpex-RLP-M and Belpex-SPP-M without defining either, where
    # the GS1800V card spells out that the weighting is the mean of the
    # Flemish DSOs' profiles. Same supplier, same index names, same published
    # parameter page, so the blend is read off that card rather than guessed;
    # if EnergyVision ever defines a Brussels profile, this is what changes.
    _ContractDef(
        "energyvision_groene_stroom",
        "EnergyVision Groene stroom",
        "spot_monthly",
        "GRS",
        {
            REGION_BRUSSELS: _CardDef(
                token="BXL-nl",
                site=_BRUSOL_SITE,
                index_url=_BRUSOL_GRS_URL,
                archive_by_upload_month=True,
            )
        },
        tranche=False,
    ),
)
_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Every residential electricity product code EnergyVision currently lists,
# across both regions, so discover() flags only a genuinely new SKU. All of
# them are implemented except the catalogued-but-declined ones: GSEZ / GSEZLP
# price self-consumed solar as a third energy leg, GSG / GS1JVG are gas, and
# GRSO is a transient group-buy SKU.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "GSDYN",
        "GS3JV",
        "GS1JV",
        "GSVI3",
        "GRS",
        "GS1800V",
        "GSLP",
        "GSEZ",
        "GSEZLP",
        "GSG",
        "GS1JVG",
        "GRSO",
    }
)

# Accept both decimal separators: a dot-decimal re-render must not truncate a
# mandatory value to its integer part (matches the sibling extractors).
_NUM = NUM_NO_THOUSANDS

# The card header prints "Alle prijzen en tarieven zijn inclusief 6% BTW".
_VAT_RE = re.compile(r"(\d+)\s*%\s*BTW", re.IGNORECASE)

# Dynamic card: "afnametarief ... formule (exclusief btw): 1,05 x Belpex per
# kwartier + 15 EUR/MWh" and "injectietarief ... formule: 1 x Belpex per
# kwartier - 15 EUR/MWh". One findall yields both rows; group 1 keys which.
_DYN_FORMULA_RE = re.compile(
    rf"(afname|injectie)tarief\b[^:]*?:\s*"
    rf"{_NUM}\s*x\s*Belpex\s+per\s+kwartier\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)

# Fixed card energy + its printed monthly injection indicative (page 1).
_FIXED_ENERGY_RE = re.compile(
    rf"Groene\s+stroom\s*[{SIGN_CHARS}]\s*vast\s+tarief\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_RE = re.compile(
    rf"Injectie\s*[{SIGN_CHARS}]\s*variabel\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)

# Tiered cards, page 1: "Groene stroom (<1.800 kWh - vast tarief) 10,60
# €cent/kWh" and its ">" twin for the remainder. The bound's dot is a
# thousands separator, so it goes through tier_bound_kwh rather than to_float,
# which would read 1.800 kWh as one point eight.
_TIER_FIXED_RE = re.compile(
    rf"Groene\s+stroom[^(\n]*\(\s*<\s*([\d.,]+)\s*kWh\s*[{SIGN_CHARS}]\s*"
    rf"vast\s+tarief\s*\)\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# Their feed-in row is either indexed ("variabel") or fixed for the term
# ("vast", GSVI3). Both print one figure; which of the two it is decides
# whether _spp_injection finds a formula to index it on.
# The Brusol "Groene stroom" card qualifies the row, "Injectie - variabel
# (indien van toepassing) 1,28€cent/kWh", so the parenthetical is tolerated.
# It cannot swallow a figure: the group still has to be the next number.
_TIER_INJECTION_RE = re.compile(
    rf"Injectie\s*[{SIGN_CHARS}]\s*(?:variabel|vast)\s*(?:\([^)]*\))?\s+"
    rf"{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# The tranche's remainder: "1,12 x Belpex-RLP-M + 20 EUR/MWh". Same shape as
# the SPP formula below and matched the same way, with the index name's
# hyphens literal so the sign group cannot bind one of them.
_RLP_FORMULA_RE = re.compile(
    rf"{_NUM}\s*x\s*Belpex[\s{SIGN_CHARS}]*RLP[\s{SIGN_CHARS}]*M\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)

# The fixed cards state the injection formula in prose, identically in both
# languages: "0,6 x Belpex-SPP-M - 15 EUR/MWh". The separators inside the
# index name are hyphens, so they are matched literally rather than through
# SIGN_CHARS, which would let the sign group bind one of them.
_SPP_FORMULA_RE = re.compile(
    rf"{_NUM}\s*x\s*Belpex[\s{SIGN_CHARS}]*SPP[\s{SIGN_CHARS}]*M\s*"
    rf"([{SIGN_CHARS}])\s*{_NUM}\s*EUR\s*/\s*MWh",
    re.IGNORECASE,
)

# "dan garanderen wij in elk geval 1 EURcent/kWh" / "nous garantissons en tout
# etat de cause 1 EURcent/kWh". Parsed rather than hardcoded so a change to the
# guarantee is picked up instead of silently under-crediting.
_GUARANTEE_RE = re.compile(
    r"(?:garanderen\s+wij\s+in\s+elk\s+geval"
    r"|garantissons\s+en\s+tout\s+(?:é|e)tat\s+de\s+cause)"
    rf"\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)

_FEE_RE = re.compile(rf"Vaste\s+vergoeding\s+{_NUM}\s*€\s*/\s*jaar", re.IGNORECASE)

# "Eenmalige welkomstkorting  200 €", the one-off first-year credit, printed
# above the standing charge on four of the six cards. Optional on purpose:
# Laadpunt prints no such row, and the Walloon card carries footnote e
# describing the credit while printing no amount for it, so there is nothing
# to grant. Only the Dutch wording is matched, because that is the only one
# any card has ever printed a figure next to.
_WELCOME_RE = re.compile(rf"Eenmalige\s+welkomstkorting\s+{_NUM}\s*€", re.IGNORECASE)

# Taxes (Flanders). GSC + WKC print as a single combined value; the
# energiefonds shows a domiciled (standard residential = 0 EUR/month) and a
# non-domiciled row; bill the domiciled one.
# "Kosten GSC en WKC geldig voor 1,554 €cent/kWh" on most cards and
# "... bedragen 1,554 €cent/kWh" on the laadpunt one. Same levy, same figure,
# two verbs; pinning one of them lost the whole tax overlay on the other.
_GSC_WKC_RE = re.compile(
    rf"GSC\s+en\s+WKC\s+(?:geldig\s+voor|bedragen)\s+{_NUM}", re.IGNORECASE
)
_CONTRIB_RE = re.compile(rf"Energiebijdrage\s+{_NUM}", re.IGNORECASE)
_EXCISE_RE = re.compile(
    rf"Verbruik\s+tussen\s+0\s*&\s*3\.000\s+kWh\s+{_NUM}", re.IGNORECASE
)
# From 1 August 2026 the federal scheme folded the separate energy
# contribution into the special excise and flattened it, so the tier table
# and the Energiebijdrage row both left the card and one "Bijzondere
# accijns" rate took their place. EnergyVision switched on its August
# Flemish card, a month after Engie / Mega / Eneco. The Walloon card is
# still on the old shape and keeps its own parser (_extract_taxes_fr).
_FLAT_EXCISE_RE = re.compile(rf"Bijzondere\s+accijns\s+{_NUM}", re.IGNORECASE)
_FUND_RE = re.compile(
    rf"Standaard\s+tarief\s+gedomicilieerd\s*:\s*{_NUM}\s*€\s*/\s*maand",
    re.IGNORECASE,
)

# Taxes (Brussels). One green levy instead of the Flemish GSC + WKC pair:
# "Kosten Groene stroom 2,737 €cent/kWh". The "Kosten" is what keeps this off
# the page-1 energy rows, which name the same product without it.
_BRUSSELS_GREEN_RE = re.compile(
    rf"Kosten\s+Groene\s+stroom\s+{_NUM}\s*€?\s*cent", re.IGNORECASE
)

_LABEL_RE = re.compile(r"Tariefkaart\s+([A-Za-z]+\s+20\d{2})", re.IGNORECASE)

# ---- Wallonia (French card) --------------------------------------------------
#
# The Walloon cards are a separate publication in French, so none of the
# patterns above match them: every one was verified to miss. They are kept as
# a parallel set rather than widened into bilingual alternations, because the
# two cards also differ in structure (no digital/analog meter split, a ten-
# column DSO table, CV instead of GSC/WKC, no energiefonds).

# "Carte tarifaire juillet 2026". \w rather than [A-Za-z]: the accented month
# names (fevrier, aout, decembre) would otherwise blank the label for three
# months a year, and a miss is silent here.
_LABEL_FR_RE = re.compile(r"Carte\s+tarifaire\s+(\w+\s+20\d{2})", re.IGNORECASE)

# "Tous les prix et tarifs incluent la TVA a 6 %" - the number follows the tax
# name here, the reverse of the Dutch "6% BTW".
_VAT_FR_RE = re.compile(r"TVA\s*(?:à|a)\s*(\d+)\s*%", re.IGNORECASE)

# "Electricite verte - tarif fixe 13,57 EURcent/kWh" and, on the same page,
# "Injection - variable 2,07 EURcent/kWh". The separator is an ASCII hyphen on
# the first and a U+2013 en dash on the second, both already in SIGN_CHARS.
_FIXED_ENERGY_FR_RE = re.compile(
    rf"Électricité\s+verte\s*[{SIGN_CHARS}]\s*tarif\s+fixe\s+"
    rf"{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FIXED_INJECTION_FR_RE = re.compile(
    rf"Injection\s*[{SIGN_CHARS}]\s*variable\s+{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
# The Walloon tiered card's tranche row, "Électricité verte (<1.800 kWh –
# tarif fixe) 10,60 €cent/kWh". The parenthetical is what tells it apart from
# the flat card's "Électricité verte – tarif fixe", which _FIXED_ENERGY_FR_RE
# reads and which correctly misses this one. The bound's dot is a thousands
# separator, so it goes through tier_bound_kwh rather than to_float, which
# would read 1.800 kWh as one point eight.
_TIER_FIXED_FR_RE = re.compile(
    rf"Électricité\s+verte[^(\n]*\(\s*<\s*([\d.,]+)\s*kWh\s*[{SIGN_CHARS}]\s*"
    rf"tarif\s+fixe\s*\)\s*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)
_FEE_FR_RE = re.compile(rf"Frais\s+fixes\s+{_NUM}\s*€\s*/\s*an", re.IGNORECASE)

# Walloon tax block. The units live in the section headers ("Suppléments
# (€cent/kWh)", "Accise fédérale (€cent/kWh)"), not on the rows, so every
# value here is c€/kWh and divides by 100.
# EnergyVision groups the thousand two ways in the same row of the same
# regulated table: "3.000" on the 1-year fixed cards and "3 000" on the
# 1.800 kWh ones, for the same month and the same rate. Anchored on either,
# because pinning the dot lost the whole tax block on the other publication.
_EXCISE_FR_RE = re.compile(
    rf"Consommation\s+entre\s+0\s*&\s*3[.\s]000\s+kWh\s+{_NUM}", re.IGNORECASE
)
# From 1 August 2026 the federal scheme folded the energy contribution into
# the special excise and flattened it, so the card prints one rate under
# "Accise speciale" instead of the four-tier consumption table.
_EXCISE_FLAT_FR_RE = re.compile(
    rf"Accise\s+sp[ée]ciale\s+{_NUM}\s*€?\s*cent\s*/\s*kWh", re.IGNORECASE
)
_CONTRIB_FR_RE = re.compile(rf"Contribution\s+énergétique\s+{_NUM}", re.IGNORECASE)
_CONNECTION_FR_RE = re.compile(
    rf"Redevance\s+de\s+raccordement\s+{_NUM}", re.IGNORECASE
)
# The Walloon green-certificate quota cost, the CV counterpart of Flanders'
# GSC + WKC. Supplier-specific (EnergyVision prints 3,00 where DATS 24 prints
# 2,860 for the same month), so it is always read off this card.
_CV_FR_RE = re.compile(
    rf"certificats\s+verts\s+et\s+certificats\s+de\s+cogénération"
    rf"[^\d]*{_NUM}\s*€?\s*cent\s*/\s*kWh",
    re.IGNORECASE,
)

# Walloon DSO row label -> DSO key. EnergyVision drops the "ORES" prefix from
# six of the seven ORES sub-areas (BRABANT WALLON, EST, HAINAUT ELECTRICITÉ,
# ORES LUXEMBOURG, MOUSCRON, NAMUR, VERVIERS), all carrying identical numbers,
# so the project's collapse-to-one-key convention picks Brabant Wallon as the
# representative row, matching dats24.py. Note the labels differ from DATS
# 24's card ("TECTEO RESA" vs "RESA", "WAVRE" vs "RÉGIE DE WAVRE").
_DSO_ROWS_FR: tuple[tuple[str, str], ...] = (
    ("AIEG", DSO_AIEG),
    ("AIESH", DSO_AIESH),
    ("BRABANT WALLON", DSO_ORES),
    ("TECTEO RESA", DSO_RESA),
    ("WAVRE", DSO_REW),
)

# The Flanders DSO table prints two blocks (digital + analog meter). Only the
# digital-meter block is billed (modern smart meters); its five columns are
# capaciteitstarief (EUR/kW/yr) | kWh-tarief (c€/kWh) | kWh excl. nacht
# (c€/kWh) | databeheer (EUR/yr) | maximumtarief (c€/kWh), the VREG ceiling on
# capacity plus the per-kWh network term.
_DIGITAL_MARKER = "Digitale Meter"
_ANALOG_MARKER = "Analoge Meter"

# Upper-case Fluvius area label -> DSO key (EnergyVision prints them in caps,
# so the shared Title-case FLUVIUS_CARD_LABELS map doesn't apply). Kempen is
# the Iveka sub-area; Midden-Vlaanderen is Intergem.
_DSO_ROWS: tuple[tuple[str, str], ...] = tuple(FLUVIUS_AREA_LABELS_UPPER.items())


# ---- public entry points -----------------------------------------------------


async def fetch(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> SupplierSnapshot:
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    card = contract.card(region)
    if card is None:
        raise ExtractorError(
            f"EnergyVision {contract_id} is not sold in {region!r}; "
            f"published for {sorted(contract.regions)}"
        )
    url = await _resolve_card_url(session, contract, card)
    text = await fetch_pdf_text_layout(session, url)
    return parse_snapshot(contract_id, text, url, region=region)


async def fetch_for_month(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
    year_month: date,
) -> SupplierSnapshot | None:
    """The card EnergyVision published for one past month, or ``None``.

    The live fetch has to scrape the listing because the CURRENT card carries
    Drupal's dedup suffix (``EV-0726-GS3JV-nl_0.pdf``), but a past month is not
    on that listing at all and its plain filename resolves directly: every
    product answered 200 for every month it existed, measured across GSDYN /
    GS3JV / GS1800V / GSVI3 / GSLP and March to September 2026.

    Each product has its own horizon rather than a shared one (GS1800V reaches
    back to March 2026, GSDYN only to June), and there is nothing on the site
    that states it. A month before it answers Drupal's 404 page, which is HTML
    rather than a PDF, so ``fetch_pdf_text_layout`` rejects it on the magic
    bytes and the ``except`` below turns that into "no archive here". Letting
    the 404 be the horizon keeps a constant from going stale behind the site.

    Every failure of the card itself is swallowed: this runs inside the
    year-to-date walk, and one unpublished month must not take the whole year
    down. A transient fetch failure is raised, so the month cache retries the
    month rather than caching it as absent.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        return None
    card = contract.card(region)
    if card is None:
        return None
    first = date(year_month.year, year_month.month, 1)
    for url in _archive_card_urls(contract, card, first):
        try:
            text = await fetch_pdf_text_layout(session, url)
            snap = parse_snapshot(contract_id, text, url, region=region)
        except ExtractorError as err:
            # A timeout, a reset or a 5xx says nothing about the month: raise,
            # so the month cache retries it instead of caching it as absent.
            if is_transient_fetch_error(str(err)):
                raise
            continue
        # Every card prints "geldig ... tot en met" so valid_until is parsed and
        # the authoritative tier of the cross-check applies. It is what catches
        # a CDN serving the current card under an archived name. A candidate
        # that turns out to hold another month's card is not the end of the
        # search: try the next one before giving the month up.
        checked = archive_validity_check(snap, text, first)
        if checked is not None:
            return checked
    return None


async def probe(
    session: aiohttp.ClientSession,
    contract_id: str,
    region: str,
) -> str | None:
    """Cheap freshness key: HEAD the page this card is advertised on. Its
    ETag / Last-Modified flips when EnergyVision rotates the monthly cards,
    which is exactly when the resolved PDF URL changes.

    The page is per region, not per supplier: the Brussels cards are
    advertised on the Brusol site and rotate on their own schedule.

    Brusol's pages are Drupal dynamic pages and answer with neither header,
    so Brussels has no probe key and falls back to the 24h TTL, the path
    Engie and Luminus take. Returning None for that is the documented way to
    say so; it does not refetch the card every tick.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    card = None if contract is None else contract.card(region)
    if card is None:
        return None
    return await head_freshness_key(
        session, card.index_url, prefer=("ETag", "Last-Modified")
    )


def _index_urls() -> tuple[str, ...]:
    """Every page a current card is advertised on, in registration order and
    without repeats."""
    seen: dict[str, None] = {}
    for contract in _CONTRACTS:
        for card in contract.cards.values():
            seen.setdefault(card.index_url, None)
    return tuple(seen)


def _card_href_re(code: str, token: str) -> re.Pattern[str]:
    """Match one card's href on an index page.

    The href is site-relative on the EnergyVision listing and absolute on the
    Brusol pages, so the site prefix is optional. The directory is not
    anchored: EnergyVision keeps every card in one ``inline-files`` folder
    while Brusol files each one under the month it uploaded it.
    """
    return re.compile(
        rf'href="((?:https?://[^"/]+)?/sites/default/files/[^"]*?'
        rf'EV-\d{{4}}-{re.escape(code)}-{re.escape(token)}[^"]*\.pdf)"',
        re.IGNORECASE,
    )


async def discover(session: aiohttp.ClientSession) -> set[str]:
    """Return the residential electricity product codes currently advertised,
    so live_check can flag a new SKU. Diffed against :data:`DISCOVER_IDS`.

    Every index page is walked and so is every token in use: a product is
    published for one region in one language, so matching one page or one
    token would silently drop a whole region's catalogue from the drift
    check. The tokens come from the registered cards rather than a literal,
    so a card added with a new token extends the check with it.

    Only pages advertising the CURRENT cards are read. Brusol also publishes
    an archive page, and reading that would report every product it has ever
    sold as a new SKU.
    """
    tokens = "|".join(
        sorted(
            {re.escape(card.token) for c in _CONTRACTS for card in c.cards.values()},
            key=lambda token: (-len(token), token),
        )
    )
    found: set[str] = set()
    for url in _index_urls():
        try:
            html = await fetch_text(session, url)
        except ExtractorError:
            continue
        found |= set(re.findall(rf"EV-\d{{4}}-([A-Z0-9]+)-(?:{tokens})", html))
    return found


def _archive_card_urls(
    contract: _ContractDef, card: _CardDef, first: date
) -> tuple[str, ...]:
    """The URLs an archived card for ``first`` could sit at, in order.

    EnergyVision keeps every month in one ``inline-files`` folder, so the
    filename locates the card on its own. Brusol files each card under the
    month it UPLOADED it, which is usually the month before delivery and
    sometimes the delivery month itself, so both are tried; measured over
    March to September 2026, the pair covers every card published.
    """
    stamp = f"{first.month:02d}{first.year % 100:02d}"
    name = f"EV-{stamp}-{contract.code}-{card.token}.pdf"
    if not card.archive_by_upload_month:
        return (f"{card.site}/sites/default/files/inline-files/{name}",)
    previous = date(first.year, first.month, 1) - timedelta(days=1)
    return tuple(
        f"{card.site}/sites/default/files/{folder:%Y-%m}/{name}"
        for folder in (previous, first)
    )


async def _resolve_card_url(
    session: aiohttp.ClientSession, contract: _ContractDef, card: _CardDef
) -> str:
    html = await fetch_text(session, card.index_url)
    match = _card_href_re(contract.code, card.token).search(html)
    if not match:
        raise ExtractorError(
            f"EnergyVision: no listing entry for card {contract.code} "
            f"({card.token}) on {card.index_url}"
        )
    href = match.group(1)
    return href if href.startswith("http") else card.site + href


# ---- snapshot parser ---------------------------------------------------------


def parse_snapshot(
    contract_id: str,
    text: str,
    source_url: str,
    publication_label: str = "",
    *,
    region: str | None = None,
) -> SupplierSnapshot:
    """Parse one card. ``region`` says which of the contract's cards this is.

    Keyword-only, and defaulted to the contract's own region where it has
    exactly one: ``region`` and ``source_url`` are both ``str``, so a
    positional argument in the wrong slot would parse a Brussels card as a
    Flemish one rather than raise. A contract sold in more than one region
    has to be told, because the card decides the DSO and tax block.
    """
    contract = _CONTRACTS_BY_ID.get(contract_id)
    if contract is None:
        raise ExtractorError(f"unknown EnergyVision contract {contract_id!r}")
    if region is None:
        if len(contract.regions) != 1:
            raise ExtractorError(
                f"EnergyVision {contract_id} is sold in "
                f"{sorted(contract.regions)}; parse_snapshot needs the region"
            )
        region = next(iter(contract.regions))
    elif region not in contract.regions:
        raise ExtractorError(
            f"EnergyVision {contract_id} is not sold in {region!r}; "
            f"published for {sorted(contract.regions)}"
        )
    if region == REGION_WALLONIA:
        return _parse_wallonia(contract_id, text, source_url, publication_label)
    energy: EnergyRates
    if contract.kind == "dynamic":
        energy, injection = _extract_dynamic(text)
    elif contract.kind == "spot_monthly":
        energy, injection = _extract_tiered(text, tranche=contract.tranche)
    else:
        energy, injection = _extract_fixed(text)
    # The energy leg is worded identically in both regions and needs no
    # branch; the network and tax blocks are each region's own.
    brussels = region == REGION_BRUSSELS
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_brussels_dsos(text) if brussels else _extract_dsos(text),
        taxes=_extract_brussels_taxes(text) if brussels else _extract_taxes(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label(text),
        valid_until=parse_valid_until(text),
        injection=injection,
        welcome_credit_eur=_welcome_credit(text),
    )


def _parse_wallonia(
    contract_id: str, text: str, source_url: str, publication_label: str
) -> SupplierSnapshot:
    """Parse the French Walloon card.

    Same snapshot shape as the Flemish cards, off an entirely separate
    publication. Two energy shapes now: the 1-year fixed product's flat
    VAT-inclusive rate, and the 1.800 kWh product's tranche plus monthly
    formula. Both carry a yearly standing charge and a monthly-indexed
    injection indicative, and both share the Walloon DSO and tax blocks.
    """
    contract = _CONTRACTS_BY_ID[contract_id]
    energy: EnergyRates
    if contract.kind == "spot_monthly":
        energy, injection = _extract_tiered_fr(text)
    else:
        energy, injection = _extract_fixed_fr(text)
    return SupplierSnapshot(
        supplier="energyvision",
        contract=contract_id,
        energy=energy,
        dsos=_extract_dsos_fr(text),
        taxes=_extract_taxes_fr(text),
        source_url=source_url,
        publication_label=publication_label or _publication_label_fr(text),
        valid_until=parse_valid_until(text),
        injection=injection,
    )


def _publication_label(text: str) -> str:
    m = _LABEL_RE.search(text)
    return m.group(1).lower() if m else ""


def _publication_label_fr(text: str) -> str:
    m = _LABEL_FR_RE.search(text)
    return m.group(1).lower() if m else ""


def _welcome_credit(text: str) -> float | None:
    """The one-off welcome credit in EUR, or ``None`` where the card prints none.

    Optional where the standing charge is mandatory: a missing row here is a
    card that grants no credit, not a layout drift, so it must not raise.
    """
    m = _WELCOME_RE.search(text)
    return None if m is None else to_float(m.group(1))


def _fee(text: str) -> float:
    m = _FEE_RE.search(text)
    if m is None:
        # The vaste vergoeding standing charge is mandatory; fail loud rather
        # than silently bill a zero yearly fee on a layout drift.
        raise ExtractorError("EnergyVision: vaste vergoeding row not found")
    return to_float(m.group(1))


def _extract_dynamic(text: str) -> tuple[DynamicRates, InjectionRates]:
    fee = _fee(text)
    # The card quotes the formula "(exclusief btw)"; every printed price is
    # VAT-inclusive, so the energy leg is scaled to the same basis (vat_rate
    # then stays 0.0, matching Frank / Bolt).
    vat = vat_multiplier(text, _VAT_RE)
    energy: DynamicRates | None = None
    injection: InjectionRates | None = None
    for word, factor_s, sign, base_s in _DYN_FORMULA_RE.findall(text):
        base_eur_mwh = parse_sign(sign) * to_float(base_s)
        if word.lower() == "afname":
            # EUR/MWh HTVA -> EUR/kWh incl VAT: the coefficient is a
            # dimensionless Belpex multiplier (* VAT, NO * 10), the base goes
            # EUR/MWh -> EUR/kWh (/1000 * VAT).
            energy = DynamicRates(
                factor=to_float(factor_s) * vat,
                base=base_eur_mwh / 1000.0 * vat,
                yearly_fixed_fee=fee,
                quarter_hourly=True,
            )
        else:
            # Injection is VAT-exempt: factor as-is (exactly 1,0 here), base
            # EUR/MWh -> EUR/kWh, no VAT.
            injection = InjectionRates(
                factor=to_float(factor_s),
                base=base_eur_mwh / 1000.0,
                formula=f"{factor_s} x Belpex {sign} {base_s} EUR/MWh",
            )
    if energy is None:
        raise ExtractorError("EnergyVision: could not parse dynamic afname formula")
    if injection is None:
        # Every dynamic card prints an injection formula; a miss is a layout
        # drift, not a fee-free contract. Raise rather than silently credit 0.
        raise ExtractorError("EnergyVision: could not parse dynamic injectie formula")
    return energy, injection


def _spp_injection(text: str, indicative: float) -> InjectionRates:
    """Injection leg for a fixed card, off its monthly Belpex-SPP-M formula.

    The printed c/kWh figure cannot be the delivery month's rate: the card
    says so itself, *"De waarde van Belpex-SPP-M van de lopende maand is pas
    gekend aan het einde van de maand"*. Surface the formula's coefficients
    with ``spp_indexed`` so the coordinator fetches the Synergrid profile and
    resolves the credit against the delivery month's own solar-weighted mean,
    and keep the printed figure as ``current`` for the months where that mean
    is not available yet.

    ``spp_indexed`` also keeps the coefficients away from the hourly spot:
    they are month coefficients, and the energy leg here is a flat rate that
    fetches no spots of its own.
    """
    formula = _SPP_FORMULA_RE.search(text)
    guarantee = _GUARANTEE_RE.search(text)
    factor: float | None = None
    base: float | None = None
    if formula is not None:
        # Injection is VAT-exempt, so neither coefficient is grossed. The
        # factor is a dimensionless multiplier on the index; the base is
        # EUR/MWh and divides by 1000, as on the dynamic card.
        factor = to_float(formula.group(1))
        base = parse_sign(formula.group(2)) * to_float(formula.group(3)) / 1000.0
    return InjectionRates(
        current=indicative,
        factor=factor,
        base=base,
        formula=formula.group(0) if formula else None,
        spp_indexed=factor is not None,
        minimum=to_float(guarantee.group(1)) / 100.0 if guarantee else None,
    )


def _extract_fixed(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _fee(text)
    m = _FIXED_ENERGY_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse fixed energy price")
    # The fixed rate is printed VAT-inclusive, so it is used as-is.
    energy = FixedRates(single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=fee)
    inj = _FIXED_INJECTION_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse fixed injection price")
    injection = _spp_injection(text, to_float(inj.group(1)) / 100.0)
    return energy, injection


def _tiered_legs(
    text: str,
    *,
    fee: float,
    vat_re: re.Pattern[str],
    tier_re: re.Pattern[str],
    injection_re: re.Pattern[str],
    tranche: bool,
) -> tuple[SpotMonthlyRates, InjectionRates]:
    """Energy + feed-in for a monthly-indexed card, tranche or not.

    The anchors come from the caller, because the Flemish and Walloon cards
    share no wording and merging them into bilingual alternations would cost
    the fail-loud guarantee each set gives on its own publication. What they
    do share is everything below: the formula, the VAT basis, the sign and
    the feed-in, which is why this body is one and not two.

    The tranche and the formula are carried side by side rather than blended
    here: which of them a household actually pays depends on its yearly
    volume, which is entry data and not card data, so ``resolve_volume_tier``
    folds them together when the snapshot is read for an entry.

    ``tranche=False`` is Brusol's "Groene stroom", which prints the same
    monthly formula with nothing in front of it and so bills it from the
    first kWh. The flag comes from the contract rather than from whether the
    row was found, so a tiered card that stops printing its tranche still
    fails loud instead of quietly billing every kWh at the indexed rate.

    ``rlp_blend`` is the Flanders curve because that is what the Dutch cards
    name, "het rekenkundig gemiddelde van de RLP-verbruiksprofielen stroom
    van de verschillende distributienetbeheerders van Vlaanderen". Every
    Flemish sub-area shares one Synergrid curve, so the mean over them IS
    that curve.

    The French card says only "les différents gestionnaires de réseau de
    distribution", naming no region, so the blend is not read off it. It is
    read off the figures instead: EnergyVision publishes its Belpex-RLP-M
    month table in both languages and the two are identical value for value
    (60,280 / 55,349 / ... / 135,655 from June 2024 to August 2026), so
    there is one index for the country and the Dutch card is what defines
    it. A Walloon household on this product is billed on the Flemish curve.
    """
    tier = tier_re.search(text) if tranche else None
    if tranche and tier is None:
        raise ExtractorError("EnergyVision: could not parse the fixed tranche row")
    formula = _RLP_FORMULA_RE.search(text)
    if formula is None:
        raise ExtractorError("EnergyVision: could not parse the Belpex-RLP-M formula")
    # The card quotes the formula "(exclusief btw)" against VAT-inclusive
    # printed prices, so the coefficients are scaled to the same basis the way
    # the dynamic leg's are. The tranche's own rate is printed inclusive and
    # is used as-is.
    vat = vat_multiplier(text, vat_re)
    energy = SpotMonthlyRates(
        factor=to_float(formula.group(1)) * vat,
        base=parse_sign(formula.group(2)) * to_float(formula.group(3)) / 1000.0 * vat,
        tier_kwh=tier_bound_kwh(tier.group(1)) if tier else None,
        tier_rate=to_float(tier.group(2)) / 100.0 if tier else None,
        rlp_indexed=True,
        rlp_blend="flanders",
        yearly_fixed_fee=fee,
    )
    inj = injection_re.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse the injection price")
    # A card that fixes its feed-in price for the term (GSVI3) prints no SPP
    # formula, so this returns the printed figure as a flat credit; the two
    # that index it get the coefficients and the monthly guarantee.
    return energy, _spp_injection(text, to_float(inj.group(1)) / 100.0)


def _extract_tiered(
    text: str, *, tranche: bool = True
) -> tuple[SpotMonthlyRates, InjectionRates]:
    """The Dutch monthly cards: the Flemish tiered range and Brusol's two."""
    return _tiered_legs(
        text,
        fee=_fee(text),
        vat_re=_VAT_RE,
        tier_re=_TIER_FIXED_RE,
        injection_re=_TIER_INJECTION_RE,
        tranche=tranche,
    )


def _extract_tiered_fr(text: str) -> tuple[SpotMonthlyRates, InjectionRates]:
    """The Walloon 1.800 kWh card, on the French publication.

    Its standing charge is zero where the Flemish and Brussels cards of the
    same product charge 50 EUR/yr, which is a figure to read rather than a
    row to treat as missing: ``_FEE_FR_RE`` matching "Frais fixes 0 €/an" is
    the card saying nothing is owed.
    """
    fee = _FEE_FR_RE.search(text)
    if fee is None:
        raise ExtractorError("EnergyVision: frais fixes row not found")
    return _tiered_legs(
        text,
        fee=to_float(fee.group(1)),
        vat_re=_VAT_FR_RE,
        tier_re=_TIER_FIXED_FR_RE,
        injection_re=_FIXED_INJECTION_FR_RE,
        tranche=True,
    )


def _extract_taxes(text: str) -> TaxOverlay:
    """Every value on the card is VAT-inclusive.

    The flat August-2026 excise row is tried before the tiered one being
    phased out: a card carrying both is mid-transition and the flat rate is
    authoritative. GSC and WKK arrive pre-summed in one row here.
    """
    return regional_tax_overlay(
        text,
        supplier="EnergyVision",
        region=REGION_FLANDERS,
        excise=(_FLAT_EXCISE_RE, _EXCISE_RE),
        renewables=(_GSC_WKC_RE,),
        contribution=_CONTRIB_RE,
        fund=_FUND_RE,
    )


def _extract_brussels_taxes(text: str) -> TaxOverlay:
    """The Brussels tax block, VAT-inclusive like the Flemish one.

    Two rows only. Brussels levies no energy fund, and the federal energy
    contribution was abolished on 2026-08-01 and is printed by no current
    card, so both are left at zero rather than looked for: on this card an
    absent row is the levy not existing, not a layout drift.
    """
    return regional_tax_overlay(
        text,
        supplier="EnergyVision",
        region=REGION_BRUSSELS,
        excise=(_FLAT_EXCISE_RE, _EXCISE_RE),
        renewables=(_BRUSSELS_GREEN_RE,),
    )


def _extract_brussels_dsos(text: str) -> dict[str, DsoOverlay]:
    """The Sibelga row, in the eight-column layout Engie's cards also print.

    Mandatory: this runs only on a Brussels card, where a missing row is a
    drift that would leave the entry with no network cost at all. Refusing
    keeps the last good card serving.
    """
    overlay = parse_sibelga_row(text)
    if overlay is None:
        raise ExtractorError("EnergyVision: Sibelga row not found")
    return {DSO_SIBELGA: overlay}


def _extract_dsos(text: str) -> dict[str, DsoOverlay]:
    start = text.find(_DIGITAL_MARKER)
    if start < 0:
        raise ExtractorError("EnergyVision: digital-meter DSO table not found")
    end = text.find(_ANALOG_MARKER, start)
    section = text[start:end] if end > start else text[start:]
    out: dict[str, DsoOverlay] = {}
    for area, key in _DSO_ROWS:
        row = numeric_row(section, f"FLUVIUS {area}", 5)
        if not row:
            continue
        out[key] = DsoOverlay(
            distribution_single=to_float(row[1]) / 100.0,
            distribution_exclusive_night=to_float(row[2]) / 100.0,
            transport=0.0,
            capacity_eur_per_kw_year=to_float(row[0]),
            data_management_per_year=to_float(row[3]),
            network_ceiling_eur_per_kwh=to_float(row[4]) / 100.0,
        )
    missing = [key for _, key in _DSO_ROWS if key not in out]
    if missing:
        # A partial table is worse than none: the areas are what every
        # entry picks its network cost from, and a card missing one would
        # be adopted, persisted and shared, leaving the entry on that area
        # with no overlay and every tick failing, while the last good card
        # is gone from the cache. Refusing keeps that card serving.
        raise ExtractorError(f"EnergyVision: DSO rows not found for {sorted(missing)}")
    return out


# ---- Wallonia parsers --------------------------------------------------------


def _extract_fixed_fr(text: str) -> tuple[FixedRates, InjectionRates]:
    fee = _FEE_FR_RE.search(text)
    if fee is None:
        raise ExtractorError("EnergyVision: frais fixes row not found")
    m = _FIXED_ENERGY_FR_RE.search(text)
    if m is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia energy price")
    # One flat rate: the card prints no bi-horaire or exclusive-night energy
    # price (those words appear only as DSO-table column headers), so peak /
    # offpeak / exclusive_night stay unset and the engine bills `single` for
    # every meter type. Printed VAT-inclusive, so used as-is.
    energy = FixedRates(
        single=to_float(m.group(1)) / 100.0, yearly_fixed_fee=to_float(fee.group(1))
    )
    inj = _FIXED_INJECTION_FR_RE.search(text)
    if inj is None:
        raise ExtractorError("EnergyVision: could not parse Wallonia injection price")
    injection = _spp_injection(text, to_float(inj.group(1)) / 100.0)
    return energy, injection


def _extract_taxes_fr(text: str) -> TaxOverlay:
    """Parse the Walloon tax block across both card generations.

    Until July 2026 the card carried a "Supplements et accise federale"
    section holding the energy contribution, the connection fee and a
    four-tier excise table. On 1 August 2026 EnergyVision deleted the whole
    supplements sub-block and replaced the tiers with one flat "Accise
    speciale" row, on every one of its Walloon cards at once.

    Only the green-certificate quota cost survives on both, so it stays
    mandatory. The excise takes the flat row when present and falls back to
    the 0-3.000 kWh tier for an older card.
    """
    excise = _EXCISE_FLAT_FR_RE.search(text) or _EXCISE_FR_RE.search(text)
    cv = _CV_FR_RE.search(text)
    if not excise or not cv:
        # The excise and the CV quota cost are both per-kWh charges that no
        # Walloon card omits, so a miss here is layout drift rather than a
        # component that stopped existing.
        raise ExtractorError("EnergyVision: could not parse Wallonia tax block")
    # The energy contribution was abolished on 2026-08-01 and folded into the
    # excise above, so an absent row is the levy being gone, not drift.
    contrib = _CONTRIB_FR_RE.search(text)
    # The connection fee is a different case: Wallonia still levies it, and
    # this card's own terms say taxes and redevances stay "entierement
    # repercutables sur le client". EnergyVision dropped the row along with
    # the abolished contribution, and publishes the rate nowhere else, so
    # there is nothing to read. Bill 0 rather than take the contract offline
    # over a charge worth ~0,075 c€/kWh, and flag it so the coordinator can
    # tell the user what their cost excludes. Peers that still print the row
    # (Engie, Mega, Bolt, OCTA+, DATS 24) keep reading it off their cards.
    connection = _CONNECTION_FR_RE.search(text)
    # There is no Flemish energiefonds and no GSC/WKC row on this card; the
    # header states every price includes 6% VAT, so vat_rate stays 0.0.
    return TaxOverlay(
        federal_excise=to_float(excise.group(1)) / 100.0,
        energy_contribution=to_float(contrib.group(1)) / 100.0 if contrib else 0.0,
        wallonia_renewables=to_float(cv.group(1)) / 100.0,
        region_connection_fee=(
            to_float(connection.group(1)) / 100.0 if connection else 0.0
        ),
        region_connection_fee_unavailable=connection is None,
        vat_rate=0.0,
    )


def _extract_dsos_fr(text: str) -> dict[str, DsoOverlay]:
    """Parse the Walloon DSO table (one ten-column block, no meter split).

    Column order, left to right:

        mono | bi-peak | bi-offpeak | ECO | MEDIUM | PIC | exclusive-night
        | transport | data-management (EUR/yr) | prosumer (EUR/kW/yr)

    The three CWaPE Impact bands print CHEAPEST FIRST here, the reverse of
    the PIC | MEDIUM | ECO order on the DATS 24 card that carries the same
    regulated numbers. Reusing that positional mapping would swap the peak
    and off-peak bands and mis-price every Walloon Impact user, so the
    ordering is asserted in the tests by value (eco < medium < pic).
    """
    out: dict[str, DsoOverlay] = {}
    for label, key in _DSO_ROWS_FR:
        row = re.search(
            rf"^{re.escape(label)}\s+" + r"\s+".join([_NUM] * 10),
            text,
            re.MULTILINE,
        )
        if not row:
            continue
        # Bands print ECO | MEDIUM | PIC here, the reverse of the DATS 24
        # card's order. The keyword-only helper is what makes that safe to
        # share: the mapping stays visible at the call site.
        out[key] = walloon_dso_overlay(
            mono=to_float(row.group(1)),
            peak=to_float(row.group(2)),
            offpeak=to_float(row.group(3)),
            eco=to_float(row.group(4)),
            medium=to_float(row.group(5)),
            pic=to_float(row.group(6)),
            excl_night=to_float(row.group(7)),
            transport=to_float(row.group(8)),
            terme_fixe=to_float(row.group(9)),
            prosumer=to_float(row.group(10)),
        )
    missing = [key for _, key in _DSO_ROWS_FR if key not in out]
    if missing:
        # A partial table is worse than none: the areas are what every
        # entry picks its network cost from, and a card missing one would
        # be adopted, persisted and shared, leaving the entry on that area
        # with no overlay and every tick failing, while the last good card
        # is gone from the cache. Refusing keeps that card serving.
        raise ExtractorError(f"EnergyVision: DSO rows not found for {sorted(missing)}")
    return out


# ---- EXTRACTOR ---------------------------------------------------------------


# Contracts whose feed-in credit indexes on a MONTHLY mean. The credit
# resolves against ENTSO-E spots the energy leg never fetches, so the config
# flow has to offer the optional key or the formula can never resolve and every
# path falls back to the card's printed figure. See
# ``Contract.spot_indexed_injection``.
_MONTH_INDEXED_INJECTION = frozenset({"energyvision_fixed_3y", "energyvision_fixed_1y"})


EXTRACTOR = SupplierExtractor(
    # Re-measured when the tiered range landed: those three cards take 6,4 to
    # 8,2 s to lay out and parse against 4,3 to 5,2 s for the older pair, in
    # both measurement orders, so warm-up does not explain the gap. The budget
    # reserves the worst card of the supplier plus 10%, and leaving it at the
    # old 5,7 would let the sweep start a laadpunt card it cannot finish,
    # which is the one thing the reservation exists to prevent.
    sweep_cost_s=9.1,
    id="energyvision",
    label="EnergyVision",
    contracts=tuple(
        Contract(
            id=c.contract_id,
            label=c.label,
            kind=c.kind,
            regions=c.regions,
            spot_indexed_injection=c.contract_id in _MONTH_INDEXED_INJECTION,
        )
        for c in _CONTRACTS
    ),
    fetch=fetch,
    probe=probe,
    fetch_for_month=fetch_for_month,
)


__all__ = [
    "DISCOVER_IDS",
    "EXTRACTOR",
    "discover",
    "fetch",
    "fetch_for_month",
    "parse_snapshot",
    "probe",
]
