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

"""Supplier extractor registry.

Each supplier module exposes a top-level ``EXTRACTOR``. The registry lists
which suppliers the integration knows about; adding a new supplier means
adding a new module + an entry below.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any

from ..const import CONF_CONTRACT, CONF_QUARTER_HOURLY, CONF_SUPPLIER
from ._moved import with_moved_fallbacks
from ._rates import (
    Contract,
    DynamicRates,
    EnergyRates,
    FixedRates,
    SpotMonthlyRates,
    VariableRates,
)
from .aspiravi import EXTRACTOR as _ASPIRAVI
from .base import (
    DsoOverlay,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
)
from .bolt import EXTRACTOR as _BOLT
from .cociter import EXTRACTOR as _COCITER
from .custom import EXTRACTOR as _CUSTOM
from .dats24 import EXTRACTOR as _DATS24
from .dots import EXTRACTOR as _DOTS
from .ebem import EXTRACTOR as _EBEM
from .ecofix import EXTRACTOR as _ECOFIX
from .ecopower import EXTRACTOR as _ECOPOWER
from .elegant import EXTRACTOR as _ELEGANT
from .eneco import EXTRACTOR as _ENECO
from .energiebe import EXTRACTOR as _ENERGIEBE
from .energyknights import EXTRACTOR as _ENERGYKNIGHTS
from .energyvision import EXTRACTOR as _ENERGYVISION
from .engie import EXTRACTOR as _ENGIE
from .frank import EXTRACTOR as _FRANK
from .luminus import EXTRACTOR as _LUMINUS
from .mega import EXTRACTOR as _MEGA
from .octaplus import EXTRACTOR as _OCTAPLUS
from .social import EXTRACTOR as _SOCIAL
from .totalenergies import EXTRACTOR as _TOTALENERGIES
from .trevion import EXTRACTOR as _TREVION

# Elegant, Engie, EnergyVision and Luminus read a card that left its usual
# address where their own pages list it (see _moved.py).
EXTRACTORS: dict[str, SupplierExtractor] = with_moved_fallbacks(
    {
        _ENECO.id: _ENECO,
        _ENGIE.id: _ENGIE,
        _TOTALENERGIES.id: _TOTALENERGIES,
        _TREVION.id: _TREVION,
        _LUMINUS.id: _LUMINUS,
        _MEGA.id: _MEGA,
        _BOLT.id: _BOLT,
        _COCITER.id: _COCITER,
        _DATS24.id: _DATS24,
        _EBEM.id: _EBEM,
        _ECOFIX.id: _ECOFIX,
        _ECOPOWER.id: _ECOPOWER,
        _FRANK.id: _FRANK,
        _ENERGIEBE.id: _ENERGIEBE,
        _ENERGYVISION.id: _ENERGYVISION,
        _ENERGYKNIGHTS.id: _ENERGYKNIGHTS,
        _OCTAPLUS.id: _OCTAPLUS,
        _ASPIRAVI.id: _ASPIRAVI,
        _SOCIAL.id: _SOCIAL,
        _ELEGANT.id: _ELEGANT,
        _DOTS.id: _DOTS,
        # Expert escape hatch, listed last so it sorts to the bottom of the
        # supplier dropdown.
        _CUSTOM.id: _CUSTOM,
    }
)


def get(supplier_id: str) -> SupplierExtractor:
    try:
        return EXTRACTORS[supplier_id]
    except KeyError as err:
        raise ExtractorError(
            f"no extractor registered for supplier {supplier_id!r}"
        ) from err


def all_extractors() -> tuple[SupplierExtractor, ...]:
    return tuple(EXTRACTORS.values())


def is_professional(supplier_id: str | None, contract_id: str | None) -> bool:
    """True when this product is a professional one, whose card is published
    excluding VAT, may band the federal excise by annual volume and keeps
    levies the residential scheme has dropped.

    Read from the registry rather than from the entry, for the reason
    ``offers_quarter_hourly`` gives below, and tolerant of an unknown pair the
    OptionsFlow can hold after a catalogue change.
    """
    contract = _contract(supplier_id, contract_id)
    return contract is not None and contract.professional


def withdrawn_on(supplier_id: str | None, contract_id: str | None) -> date | None:
    """The first day this product was no longer sold (``Contract.withdrawn``),
    or ``None`` while it is, read from the registry for the reason
    :func:`offers_quarter_hourly` gives."""
    contract = _contract(supplier_id, contract_id)
    return None if contract is None else contract.withdrawn


def feed_in_floor_from(supplier_id: str | None, contract_id: str | None) -> date | None:
    """The first signing month whose card guarantees the feed-in minimum
    (``Contract.feed_in_floor_from``), or ``None`` where every issue read
    states the one it prints, read from the registry for the reason
    :func:`offers_quarter_hourly` gives."""
    contract = _contract(supplier_id, contract_id)
    return None if contract is None else contract.feed_in_floor_from


def _contract(supplier_id: str | None, contract_id: str | None) -> Contract | None:
    """The registry's contract for the pair, or ``None`` for a pair it does
    not know, which the OptionsFlow can hold after a catalogue change."""
    if not supplier_id or not contract_id:
        return None
    try:
        contracts = get(supplier_id).contracts
    except ExtractorError:
        return None
    return next((c for c in contracts if c.id == contract_id), None)


def offers_quarter_hourly(supplier_id: str | None, contract_id: str | None) -> bool:
    """True when the supplier lets this product settle per quarter-hour.

    Read from the registry rather than from the entry alone, so the choice
    disappears by itself if a supplier stops offering it: the config flow
    stops showing the box and ``_resolve_snapshot`` stops applying a stored
    answer, on the same flag, instead of the two drifting apart.

    Tolerates an unknown supplier or contract, which the OptionsFlow can hold
    after a catalogue change, for the same reason ``_contract_kind`` does.
    """
    contract = _contract(supplier_id, contract_id)
    return contract is not None and contract.quarter_hourly_option


def offers_direct_debit(supplier_id: str | None, contract_id: str | None) -> bool:
    """True when this product's card prices a direct-debit payer differently.

    Read from the registry for the reason :func:`offers_quarter_hourly` is:
    the config flow's box and the resolution of a stored answer come off one
    flag, so a supplier dropping the reduction removes both at once instead
    of leaving a stored answer applying a discount nobody grants.

    Tolerates an unknown supplier or contract, which the OptionsFlow can hold
    after a catalogue change.
    """
    contract = _contract(supplier_id, contract_id)
    return contract is not None and contract.direct_debit_discount


def effective_kind(
    supplier_id: str | None, contract_id: str | None, *, quarter_hourly: bool = False
) -> str:
    """The ``TariffKind`` this contract is billed on for this household.

    The registered kind for every product that fixes its own settlement, which
    is all but Bolt's variable cards and Frank's dynamic tiers.

    Where the customer chooses, the answer moves the kind, because the two
    settlements are not one rate on two grids for both suppliers:

    * Frank is. Both sides are ``dynamic``, so this is identity and only
      ``DynamicRates.quarter_hourly`` moves.
    * Bolt is not. Its variable card settles the same printed formula either
      against the RLP-weighted month (``variable``) or per quarter-hour
      (``dynamic``), which are different ``EnergyRates`` kinds, so the kind
      has to follow the answer.

    That is what makes this a function rather than a field. Everything the
    config flow decides before it has ever seen a card reads the kind: which
    meters the product is sold on, whether the ENTSO-E key is mandatory, which
    boxes the signing-rate step offers, and which cell the ranking page puts
    the household in. All of them have to see the settlement the household
    actually picked, which is why the flow asks for it directly after the
    contract and before any of those.

    Returns ``""`` for a contract the registry does not know, exactly as
    ``_contract_kind`` did, so a stale OptionsFlow entry still renders.
    """
    contract = _contract(supplier_id, contract_id)
    if contract is None:
        return ""
    if quarter_hourly and contract.quarter_hourly_option:
        return "dynamic"
    return contract.kind


def settlement_answer(data: Mapping[str, Any]) -> bool:
    """The settlement answer held in one side's config data.

    Gated on that side's OWN contract, so a value left behind by an earlier
    pick, or carried in from the household when the target is a different
    product, can never move a kind it does not belong to.
    """
    if not offers_quarter_hourly(data.get(CONF_SUPPLIER), data.get(CONF_CONTRACT)):
        return False
    return bool(data.get(CONF_QUARTER_HOURLY, False))


def takes_signing_rate(data: Mapping[str, Any]) -> bool:
    """Whether a typed signing rate applies to the contract ``data`` names.

    A fixed card's price and a dynamic or spot-monthly card's coefficient pair
    are what the signing-rate step asks for, so those are the only kinds it
    is offered on and the only ones it may bill. One rule for both, because
    they drifted: the step was gated on the registry kind while the rate was
    laid onto whatever shape the card took, and a month-indexed variable card
    re-priced to a spot-monthly leg picked up a rate typed on the entry's
    previous contract.
    """
    kind = effective_kind(
        data.get(CONF_SUPPLIER),
        data.get(CONF_CONTRACT),
        quarter_hourly=settlement_answer(data),
    )
    return kind in ("fixed", "dynamic", "spot_monthly")


__all__ = [
    "EXTRACTORS",
    "Contract",
    "DsoOverlay",
    "DynamicRates",
    "EnergyRates",
    "ExtractorError",
    "FixedRates",
    "SpotMonthlyRates",
    "SupplierExtractor",
    "SupplierSnapshot",
    "TaxOverlay",
    "VariableRates",
    "all_extractors",
    "effective_kind",
    "get",
    "offers_direct_debit",
    "offers_quarter_hourly",
    "settlement_answer",
    "takes_signing_rate",
]
