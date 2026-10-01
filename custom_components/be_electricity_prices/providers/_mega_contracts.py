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

"""Mega's contract registry: the products, how their cards are addressed, and
which of them price the ristourne on how the household pays.

Split out of ``mega.py`` to keep it under the package's module size limit.
The registry is data that every other part of the extractor reads and no test
patches, so moving it leaves each reader where it was; the URL resolution, the
archive and ``parse_snapshot`` stay in ``mega.py`` as the convention for
supplier modules has it. The card readers live in ``_mega_cards.py`` and
``_mega_overlays.py``.

No behaviour change: every definition here is byte-identical to the one it
replaced.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..const import REGION_WALLONIA
from ._rates import ALL_REGIONS, TariffKind


@dataclass(frozen=True)
class _ContractDef:
    contract_id: str
    label: str
    kind: TariffKind
    product_name: str  # the data-product-element value Mega uses on its site
    # Regions the product is actually published in. Defaults to all
    # three; Off-peak Impact is Wallonia-only because it requires the
    # CWaPE IMPACT DSO tariff (Wallonia-specific).
    regions: frozenset[str] = ALL_REGIONS
    # B2C (residential) or B2B (professional), the segment in the card's
    # filename. The professional cards are absent from the public listing,
    # so a B2B contract also carries the filename tokens needed to build
    # its URL directly; see _pro_pdf_url.
    segment: str = "B2C"
    file_family: str = ""  # "Smart", "Cosy", "Dynamic", ...
    file_variant: str = ""  # "-Fixed" or empty
    # True for a professional card Mega DOES link from the public listing,
    # which the SME pair is and no other B2B card is. It only affects
    # discovery: the catalog baseline counts advertised products, so an
    # unlisted B2B edition cannot vouch for a product name the listing shows
    # (that is what hid Zen Fixed's return), while a listed one has to, or it
    # is reported as new every day.
    b2b_listed: bool = False

    @property
    def professional(self) -> bool:
        return self.segment == "B2B"

    @property
    def advertised(self) -> bool:
        """Whether Mega's public listing links this product's card."""
        return not self.professional or self.b2b_listed


_CONTRACTS: tuple[_ContractDef, ...] = (
    _ContractDef(
        "mega_smart_fixed", "Mega Smart Fixed (2 years)", "fixed", "Smart Fixed"
    ),
    _ContractDef(
        "mega_smart_flex", "Mega Smart Flex (2 years)", "variable", "Smart Flex"
    ),
    # Mega dropped "Zen Fixed" from the residential listing for the August
    # 2026 card and put it back for September, in all three regions, on the
    # ordinary fixed path with no parser change. Its B2B edition never went
    # away, which is why the catalog diff stayed quiet through the gap: the
    # professional contract carries the same product_name and covered the
    # listing entry for it.
    _ContractDef("mega_zen_fixed", "Mega Zen Fixed (3 years)", "fixed", "Zen Fixed"),
    _ContractDef("mega_online_fixed", "Mega Online Fixed", "fixed", "Online Fixed"),
    _ContractDef("mega_online_flex", "Mega Online Flex", "variable", "Online Flex"),
    _ContractDef("mega_cosy_fixed", "Mega Cosy Fixed", "fixed", "Cosy Fixed"),
    _ContractDef("mega_cosy_flex", "Mega Cosy Flex", "variable", "Cosy Flex"),
    # Mega pulled "Off-peak Fixed" in July 2026 and brought it back for the
    # August 2026 card, in all three regions and with a B2B edition, which is
    # the catalog check doing exactly what it exists for. The card parses on
    # the existing fixed path with no parser change.
    _ContractDef(
        "mega_offpeak_fixed", "Mega Off-peak Fixed", "fixed", "Off-peak Fixed"
    ),
    _ContractDef(
        "mega_offpeak_flex", "Mega Off-peak Flex", "variable", "Off-peak Flex"
    ),
    _ContractDef(
        "mega_offpeak_impact_var",
        "Mega Off-peak Impact",
        "tou_impact",
        "Off-peak Impact",
        regions=frozenset({REGION_WALLONIA}),
    ),
    _ContractDef("mega_dynamic", "Mega Dynamic", "dynamic", "Dynamic"),
    # Mega discontinued "Mega Cap" (the "prix variable plafonne" product)
    # with the September 2026 cards, residential and B2B together: the
    # listing dropped the product block in all three regions and the CDN
    # answers its September filename with the HTML stub it serves for a card
    # it never published, while August's is still there. Only the tariff-type
    # filter button is left on the listing, with no product behind it. Same
    # treatment as Zen Fixed above; discover() re-surfaces it if Mega revives
    # the product.
    # The professional editions. Mega publishes these to the same CDN but
    # never links them from the public listing, so they are addressed by
    # building the filename. Online Flex, Off-peak Flex and Off-peak Impact
    # have no B2B card; Off-peak Fixed gained one when it returned in August
    # 2026, and Zen Fixed kept its own through the month its residential
    # edition was off the listing.
    _ContractDef(
        "mega_pro_offpeak_fixed",
        "Mega Off-peak Fixed (pro)",
        "fixed",
        "Off-peak Fixed",
        segment="B2B",
        file_family="Offpeak-Bi",
        file_variant="-Fix",
    ),
    _ContractDef(
        "mega_pro_smart_fixed",
        "Mega Smart Fixed (pro)",
        "fixed",
        "Smart Fixed",
        segment="B2B",
        file_family="Smart",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_smart_flex",
        "Mega Smart Flex (pro)",
        "variable",
        "Smart Flex",
        segment="B2B",
        file_family="Smart",
    ),
    _ContractDef(
        "mega_pro_online_fixed",
        "Mega Online Fixed (pro)",
        "fixed",
        "Online Fixed",
        segment="B2B",
        file_family="Online",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_cosy_fixed",
        "Mega Cosy Fixed (pro)",
        "fixed",
        "Cosy Fixed",
        segment="B2B",
        file_family="Cosy",
        file_variant="-Fixed",
    ),
    _ContractDef(
        "mega_pro_cosy_flex",
        "Mega Cosy Flex (pro)",
        "variable",
        "Cosy Flex",
        segment="B2B",
        file_family="Cosy",
    ),
    _ContractDef(
        "mega_pro_dynamic",
        "Mega Dynamic (pro)",
        "dynamic",
        "Dynamic",
        segment="B2B",
        file_family="Dynamic",
    ),
    _ContractDef(
        "mega_pro_zen_fixed",
        "Mega Zen Fixed (pro)",
        "fixed",
        "Zen Fixed",
        segment="B2B",
        file_family="Zen",
        file_variant="-Fixed",
    ),
    # "Carte tarifaire PME", the small-business pair Mega added for the
    # September 2026 cards. They are the only professional cards it links
    # from the public listing, hence b2b_listed, and they have no
    # residential edition at all. The filenames follow the same grammar as
    # every other B2B card, with -ND on both variants.
    _ContractDef(
        "mega_pro_sme_fixed",
        "Mega SME Fixed (pro)",
        "fixed",
        "SME Fixed",
        segment="B2B",
        file_family="SME",
        file_variant="-ND-Fixed",
        b2b_listed=True,
    ),
    _ContractDef(
        "mega_pro_sme_flex",
        "Mega SME Flex (pro)",
        "variable",
        "SME Flex",
        segment="B2B",
        file_family="SME",
        file_variant="-ND",
        b2b_listed=True,
    ),
)

_CONTRACTS_BY_ID = {c.contract_id: c for c in _CONTRACTS}

# Product names Mega lists on the public catalog page that this
# integration intentionally does not model. The daily live-check
# subtracts both _CONTRACTS and this set from the discovered list, so
# truly new residential electricity products surface as actionable
# signal while these stay quiet.
#
#   * Prepaid Fixed / Prepaid Flex: topup-card products with a
#     different billing model (no monthly invoice, no recorder-backed
#     consumption sensors), out of scope for the Energy-dashboard
#     integration.
_KNOWN_UNSUPPORTED_PRODUCTS: frozenset[str] = frozenset(
    {
        "Prepaid Fixed",
        "Prepaid Flex",
    }
)


# The products whose ristourne depends on how the household pays. Listed
# rather than derived because the flow has to know before any card is
# fetched, which is the same reason offers_quarter_hourly reads the
# registry; a product dropping the difference drops off this list and the
# flow stops asking. Measured across every archived month of each card.
#
# Two ways a card can depend on it, and the list is the union: sixteen
# grant a direct-debit payer a LARGER credit, fourteen printing the
# supplement and the two Dynamic cards what anyone else loses, and four
# grant the whole thing to nobody else (Cosy Flex, Smart Fixed and their pro
# twins). Pro Cosy Flex has printed both wordings in different months, which
# is why which one applies is parsed off the card and only whether to ask is
# listed here.
_DIRECT_DEBIT_RISTOURNE: frozenset[str] = frozenset(
    {
        "mega_cosy_fixed",
        "mega_cosy_flex",
        "mega_dynamic",
        "mega_offpeak_fixed",
        "mega_offpeak_flex",
        "mega_offpeak_impact_var",
        "mega_online_fixed",
        "mega_online_flex",
        "mega_smart_fixed",
        "mega_smart_flex",
        "mega_zen_fixed",
        "mega_pro_cosy_fixed",
        "mega_pro_cosy_flex",
        "mega_pro_dynamic",
        "mega_pro_offpeak_fixed",
        "mega_pro_online_fixed",
        "mega_pro_smart_fixed",
        "mega_pro_smart_flex",
        "mega_pro_zen_fixed",
    }
)
