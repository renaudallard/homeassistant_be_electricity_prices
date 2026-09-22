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

"""Which supplier and contract a household can pick, and what each one is.

The registry answers by region, by meter and by what the card prints, and
every step that offers a choice or validates one asks the same questions:
is this contract sold here, is it professional, does it settle on spots,
does its feed-in follow an index. Read-only over the registry.
"""

from __future__ import annotations

from .const import CONF_REGION
from .const import CONF_SUPPLIER
from .const import DSO_CHOICES
from .const import KIND_GROUP
from .const import SUPPLIER_CUSTOM
from .providers import all_extractors
from .providers import effective_kind
from .providers import get as get_extractor
from .providers import is_professional
from .providers._rates import Contract
from .providers.base import ExtractorError
from homeassistant.helpers.selector import SelectOptionDict
from typing import Any


def _supplier_options(
    region: str | None = None, keep: str | None = None
) -> list[SelectOptionDict]:
    """Selectable suppliers, dropping any that has announced its exit.

    ``keep`` is the supplier already stored on the entry being edited. It
    must be passed on every edit path: a SelectSelector rejects a default
    that is not among its options, so filtering unconditionally would make
    an existing entry on a withdrawn supplier impossible to edit.
    """
    extractors = all_extractors()
    if region is not None:
        extractors = tuple(e for e in extractors if region in e.regions())
    # By label, not by registry order, which is insertion order and put a
    # supplier added later wherever its import happened to land; the expert
    # escape hatch stays last.
    ordered = sorted(
        extractors, key=lambda e: (e.id == SUPPLIER_CUSTOM, e.label.casefold())
    )
    return [
        SelectOptionDict(value=e.id, label=e.label)
        for e in ordered
        if e.deprecated_until is None or e.id == keep
    ]


def _region_mismatch_error(data: dict[str, Any]) -> dict[str, str] | None:
    """Report a supplier that sells nothing in the chosen region.

    Supplier and region are picked on the SAME step, so the mismatch can only
    be judged once both are in. Detecting it a step later and aborting ends
    the flow, and in the options flow that discards every other change made in
    the same run: the user re-opens the dialog to find their edits gone. The
    abort text even says "go back and pick a different combination", which HA
    gives no way to do from an abort.

    Returning it as a form error re-shows the step with everything still
    filled in, which is what the text has always described.
    """
    supplier = data.get(CONF_SUPPLIER)
    region = data.get(CONF_REGION)
    if not supplier or not region:
        return None
    try:
        available = _contracts_for(str(supplier), str(region))
    except ExtractorError:
        # Not this check's business: an unknown supplier id is rejected by the
        # selector itself.
        return None
    if available:
        return None
    return {CONF_SUPPLIER: "supplier_region_unavailable"}


def _contracts_for(supplier_id: str, region: str | None = None) -> tuple[Contract, ...]:
    contracts = get_extractor(supplier_id).contracts
    if region is None:
        return contracts
    return tuple(c for c in contracts if region in c.regions)


def _region_dso_options(region: str) -> list[SelectOptionDict]:
    return [
        SelectOptionDict(value=slug, label=label)
        for slug, label in DSO_CHOICES.get(region, ())
    ]


def _region_dso_slugs(region: str) -> tuple[str, ...]:
    return tuple(slug for slug, _ in DSO_CHOICES.get(region, ()))


def _contract_kind(
    supplier_id: str, contract_id: str, *, quarter_hourly: bool = False
) -> str:
    """Return the TariffKind for a contract, or '' if it can't be resolved.

    OptionsFlow can re-open a stale entry whose stored ``contract`` is
    no longer in the supplier's catalogue (supplier dropped a product,
    or the catalogue moved). Returning empty instead of raising lets
    the meter step still render with a sensible default.

    ``quarter_hourly`` is the household's settlement answer, and it MOVES the
    kind on a product sold on both (Bolt's variable cards settle either
    against the RLP-weighted month or per quarter-hour, which are different
    rate kinds). Defaulted rather than required so every caller that asks
    about a card in the abstract keeps the registered kind; a caller holding
    an entry passes its answer, and a caller holding a compare TARGET passes
    that target's, never the user's. See :func:`effective_kind`.
    """
    return effective_kind(supplier_id, contract_id, quarter_hourly=quarter_hourly)


def _contract_is_professional(supplier_id: str | None, contract_id: str | None) -> bool:
    """True when the chosen contract is a professional product, whose card
    is published excluding VAT and may band the federal excise by annual
    volume. Resolved from the registry's ``Contract.professional`` flag.

    The flow's name for the registry lookup the pricing side reads as well.
    """
    return is_professional(supplier_id, contract_id)


def _contract_has_spot_injection(
    supplier_id: str | None, contract_id: str | None
) -> bool:
    """True when the chosen contract's injection is a per-hour spot
    formula needing an ENTSO-E key even though the energy isn't dynamic.
    Resolved from the registry's ``Contract.spot_indexed_injection`` flag.

    This used to name Cociter Variable as the only such card, and went on
    naming it long after most of the static range across a dozen suppliers
    had gained the flag. Deliberately no count here: the number moves
    whenever a card is registered, and the one figure worth stating is in
    the README, where a test derives it from this same flag.

    Two shapes carry it, and ``_injection_needs_spot`` is what tells them
    apart: a card with no printed indicative (``current is None``), which
    loses its whole credit without a key, and one whose indicative the card
    labels an illustration (``slot_indexed``), which falls back to it.
    """
    if not supplier_id or not contract_id:
        return False
    try:
        contracts = get_extractor(supplier_id).contracts
    except ExtractorError:
        return False
    return any(c.id == contract_id and c.spot_indexed_injection for c in contracts)


def _contract_is_month_indexed(
    supplier_id: str | None, contract_id: str | None
) -> bool:
    """True when the chosen contract's ENERGY is indexed on the delivery
    month's mean, so the optional ENTSO-E key is worth offering on every
    solar regime. Resolved from the registry's ``Contract.month_indexed_energy``
    flag, the registry twin of the parser's ``month_indexed``."""
    if not supplier_id or not contract_id:
        return False
    try:
        contracts = get_extractor(supplier_id).contracts
    except ExtractorError:
        return False
    return any(c.id == contract_id and c.month_indexed_energy for c in contracts)


def _sweep_candidates(
    region: str, group: str, professional: bool, own_contract: str
) -> list[tuple[str, Contract, bool]]:
    """Every contract the ranking page may quote for this household.

    The five conditions, and where each already existed for the 1:1 page:

    * region, per CONTRACT and never ``SupplierExtractor.regions()``, which is
      only the union across a supplier's products;
    * not the expert custom supplier, which has no fetchable card and can only
      ever be the current side of a quote;
    * not a supplier on its way out of the market, since quoting a household
      into a contract about to be transferred away is never useful;
    * the same professional segment, for the reason
      ``_compare_contract_schema`` gives at length;
    * the same kind group, which is the one condition the 1:1 page does NOT
      apply: see ``KIND_GROUP``.

    ``own_contract`` is dropped because a ranking is a list of alternatives.
    That is the opposite of the 1:1 page, which keeps it on purpose so a
    household can ask what its own contract would cost on another meter, and
    the two are not in tension: the ranking's own row is printed from the
    baseline the household is already being quoted against, not fetched again
    as a candidate.

    Returns ``(supplier_id, Contract, quarter_hourly)`` triples, because a
    contract does not carry its supplier and a product sold on two settlements
    is two candidates.

    That expansion is what keeps the page whole. A contract the customer may
    settle either way belongs to a different KIND GROUP on each side (Bolt's
    variable cards are ``static`` unticked and ``spot`` ticked), and the
    ranking only ever ranks within one group. Offering just the registered
    settlement would hide Bolt from every dynamic household and hide its
    quarter-hourly settlement from every static one, which is a row the page
    used to have when the two were separate contract ids. Costs nothing to
    fetch: the pair shares one document and the sweep now reads it once.
    """
    out: list[tuple[str, Contract, bool]] = []
    for ext in all_extractors():
        if ext.id == SUPPLIER_CUSTOM or ext.deprecated_until is not None:
            continue
        for c in ext.contracts:
            if region not in c.regions:
                continue
            if c.professional != professional:
                continue
            if c.id == own_contract:
                continue
            # Expanded only where the settlement changes the KIND, which is
            # Bolt: its variable card is a different rate kind read either
            # way, so the two readings are two bills and belong in different
            # cells. Frank's tiers are dynamic on both settlements, so the
            # second candidate would be the same product priced identically
            # (the ranking's annual figure is hourly whatever the live
            # sensors show) and cost a duplicate row and a duplicate fetch.
            settlements = [False]
            if c.quarter_hourly_option and effective_kind(
                ext.id, c.id, quarter_hourly=True
            ) != effective_kind(ext.id, c.id):
                settlements.append(True)
            for quarter_hourly in settlements:
                kind = effective_kind(ext.id, c.id, quarter_hourly=quarter_hourly)
                # Subscripted, not .get(): KIND_GROUP is total over TariffKind
                # and a KeyError here is a new kind nobody grouped, which must
                # fail loudly in CI rather than quietly drop every contract of
                # it.
                if KIND_GROUP[kind] != group:
                    continue
                out.append((ext.id, c, quarter_hourly))
    return out


def _contract_group(
    supplier_id: str, contract_id: str, *, quarter_hourly: bool = False
) -> str:
    """The household's own kind group, or '' when it cannot be resolved.

    ``_contract_kind`` returns '' for an entry whose stored contract has left
    the catalogue, deliberately, so the meter step can still render. That
    empty string has no group, and the ranking page cannot be built for it:
    answer '' here too and let the caller say so, rather than raising out of a
    registry lookup or inventing a group the household is not on.

    The stale SUPPLIER is a second case ``_contract_kind`` does not cover: it
    resolves the extractor first, and that raises for an id this build no
    longer ships. Caught here rather than there, because widening
    ``_contract_kind`` would change what every other caller sees for an entry
    whose supplier is gone, and this is the only caller that needs an answer
    rather than an exception.

    ``quarter_hourly`` is the household's settlement answer, and it decides
    the group as much as the contract does: a Bolt variable card is ``static``
    settled monthly and ``spot`` settled per quarter-hour. The ranking only
    ranks within one group, so reading the registered kind alone put a
    quarter-hourly household in a cell of monthly contracts.
    """
    try:
        kind = _contract_kind(supplier_id, contract_id, quarter_hourly=quarter_hourly)
    except ExtractorError:
        return ""
    return KIND_GROUP.get(kind, "")
