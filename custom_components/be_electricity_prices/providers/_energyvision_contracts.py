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

"""EnergyVision's and Brusol's products, and where each region's card is
published.

The registry ``energyvision.py`` reads its cards by: one ``_ContractDef`` per
product, each with a ``_CardDef`` per region it is sold in, and the product
codes the listing names (``DISCOVER_IDS``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from ..const import REGION_BRUSSELS, REGION_FLANDERS, REGION_WALLONIA
from ._rates import TariffKind

_SITE_BASE = "https://www.energyvision.be"

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
    # Page whose HTML carries the current card's href, or None where the
    # card is found by its name, as EnergyVision's own listing finds it.
    index_url: str | None = None
    # How an archived card is located under <site>/sites/default/files/.
    # False is EnergyVision's "inline-files" folder, asked after the upload
    # folders from November 2026; True is Brusol, which files each card
    # under the month it uploaded it.
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
    # The first day the product is no longer sold, as the listing's script
    # says by an end month; None while it is.
    withdrawn: date | None = None
    # How the card's file is named, from the product code, the card's region
    # token and its month as MMYY. The products of 2026 put the month first;
    # the original Goedkope stroom put its code first and a version after,
    # and no region.
    file_name: str = "EV-{month}-{code}-{token}.pdf"
    # The month of the only card read, for a product withdrawn before the
    # cards it was sold on could all be read: every month is billed on it,
    # with its own month's network and levies, and no other issue is asked
    # for. None reads the month's own card.
    last_card: date | None = None
    # The first signing month whose card guarantees the feed-in minimum
    # ``last_card`` prints (``Contract.feed_in_floor_from``); None where
    # every issue read states its own.
    feed_in_floor_from: date | None = None

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
    # Sold in Wallonia too from November 2026 (GS3JV-WAL, "Électricité bon
    # marché 3 ans fixe"), on the French card the 1-year product had.
    _ContractDef(
        "energyvision_fixed_3y",
        "EnergyVision 3 jaar vast",
        "fixed",
        "GS3JV",
        {REGION_FLANDERS: _FLANDERS_CARD, REGION_WALLONIA: _CardDef(token="WAL-fr")},
    ),
    # New in November 2026 in both regions, on the same cards as the 3-year
    # product.
    _ContractDef(
        "energyvision_fixed_5y",
        "EnergyVision 5 jaar vast",
        "fixed",
        "GS5JV",
        {REGION_FLANDERS: _FLANDERS_CARD, REGION_WALLONIA: _CardDef(token="WAL-fr")},
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
        # The listing's script ends it with October 2026 (end:"2026-10"),
        # when the 3-year and 5-year products came to Wallonia.
        withdrawn=date(2026, 11, 1),
    ),
    # The tiered range. All three share one shape and differ only in the
    # tranche, the flat rate, the coefficient, the standing charge and how the
    # feed-in is priced, so one parser reads the three of them.
    # The one product sold in all three regions. The Dutch Brussels card is
    # worded exactly like the Flemish one and prints the same energy leg
    # figure for figure, so it goes through the same parser; only the network
    # and tax blocks are the region's own. The Walloon card is the separate
    # French publication and needs the *_fr anchors, but the same body behind
    # them, and its standing charge is its own: none on the April to
    # September 2026 cards and 70 from October, where the other two charge 50.
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
    # The original Goedkope stroom, sold in Flanders until September 2025 on a
    # card that also prices gas, of which only the electricity is read. Its
    # issues are EV-GS-<MMYY>-01: November and December 2024, January, March,
    # June and September 2025, none from October 2025. Only the last is
    # read: the 2024 ones print the ten Fluvius areas before the merger, the
    # January and March 2025 ones print their network as a picture, December
    # 2024 and March 2025 leave page 1's prices blank, and June 2025's
    # network, levies and terms are September's figure for figure. The price
    # is the same on every issue, the first 1.000 kWh of the year at 9,54
    # c/kWh for 10 years, the rest on 1,05 x Belpex-RLP-M + 15 EUR/MWh and the
    # feed-in on 0,6 x Belpex-SPP-M - 15, so the last card prices every month,
    # each with its own month's index, network and levies. The 1 c/kWh floor
    # under the feed-in is not: it came with the June 2025 card, and a
    # household that signed an earlier one is credited the formula alone.
    _ContractDef(
        "energyvision_goedkope_stroom",
        "EnergyVision Goedkope stroom",
        "spot_monthly",
        "GS",
        {REGION_FLANDERS: _FLANDERS_CARD},
        withdrawn=date(2025, 10, 1),
        # The card's version after the month, "Versie EV-GS-1124-01", the
        # same 01 on every issue; the region is not in the name.
        file_name="EV-{code}-{month}-01.pdf",
        last_card=date(2025, 9, 1),
        # Condition 5, "garanderen wij in elk geval 1 €cent/kWh", came with
        # the June 2025 card: the four issues before it print no floor.
        feed_in_floor_from=date(2025, 6, 1),
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

# Every residential product code EnergyVision and Brusol currently list, so
# discover() flags only a genuinely new SKU. All of them are implemented
# except the catalogued-but-declined ones: GSEZ / GSEZLP price self-consumed
# solar as a third energy leg, and GSG / GS1JVG are gas. The listing names
# its group buys apart, by partner rather than by product code (the iChoosr
# GRSO cards of May to July 2026 among them), and discover() reads none.
DISCOVER_IDS: frozenset[str] = frozenset(
    {
        "GSDYN",
        "GS3JV",
        "GS5JV",
        "GSVI3",
        "GRS",
        "GS1800V",
        "GSLP",
        "GSEZ",
        "GSEZLP",
        "GSG",
        "GS1JVG",
    }
)
