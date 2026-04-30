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

from .base import (
    Contract,
    DsoOverlay,
    DynamicRates,
    EnergyRates,
    ExtractorError,
    FixedRates,
    SupplierExtractor,
    SupplierSnapshot,
    TaxOverlay,
    VariableRates,
)
from .bolt import EXTRACTOR as _BOLT
from .cociter import EXTRACTOR as _COCITER
from .eneco import EXTRACTOR as _ENECO
from .engie import EXTRACTOR as _ENGIE
from .luminus import EXTRACTOR as _LUMINUS
from .mega import EXTRACTOR as _MEGA
from .octaplus import EXTRACTOR as _OCTAPLUS
from .totalenergies import EXTRACTOR as _TOTALENERGIES

EXTRACTORS: dict[str, SupplierExtractor] = {
    _ENECO.id: _ENECO,
    _ENGIE.id: _ENGIE,
    _TOTALENERGIES.id: _TOTALENERGIES,
    _LUMINUS.id: _LUMINUS,
    _MEGA.id: _MEGA,
    _BOLT.id: _BOLT,
    _COCITER.id: _COCITER,
    _OCTAPLUS.id: _OCTAPLUS,
}


def get(supplier_id: str) -> SupplierExtractor:
    try:
        return EXTRACTORS[supplier_id]
    except KeyError as err:
        raise ExtractorError(
            f"no extractor registered for supplier {supplier_id!r}"
        ) from err


def all_extractors() -> tuple[SupplierExtractor, ...]:
    return tuple(EXTRACTORS.values())


__all__ = [
    "Contract",
    "DsoOverlay",
    "DynamicRates",
    "EnergyRates",
    "EXTRACTORS",
    "ExtractorError",
    "FixedRates",
    "SupplierExtractor",
    "SupplierSnapshot",
    "TaxOverlay",
    "VariableRates",
    "all_extractors",
    "get",
]
