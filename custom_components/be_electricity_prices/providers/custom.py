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

"""Expert custom-formula supplier.

An escape hatch for suppliers that publish no public, machine-resolvable
tariff card (e.g. Yuso, the Mega iChoosr / Samen Overstappen groepsaankoop),
so the normal scrape-a-card path is impossible. The user types their own
commodity formula and all regulated DSO + tax values in the config flow, and
the coordinator builds the ``SupplierSnapshot`` locally from the entry rather
than fetching anything. There is no card, no probe and no archive, so ``fetch``
is a stub the coordinator never calls; this module exists only to surface the
supplier in the dropdown and to carry its per-mode contract catalogue.

Three contracts double as the energy-mode picker:

  * ``custom_dynamic``  - ``factor * live spot + base`` (kind ``dynamic``)
  * ``custom_monthly``  - ``factor * monthly-mean spot + base`` (kind
    ``spot_monthly``), a flat per-month rate for group-purchase products
  * ``custom_fixed``    - a flat manual rate (kind ``fixed``)

The dynamic and monthly modes are spot-indexed, so the config flow collects an
ENTSO-E API key for them (gated on the contract kind).
"""

from __future__ import annotations

import aiohttp

from ..const import (
    CUSTOM_CONTRACT_DYNAMIC,
    CUSTOM_CONTRACT_FIXED,
    CUSTOM_CONTRACT_MONTHLY,
    SUPPLIER_CUSTOM,
)
from .base import (
    Contract,
    ExtractorError,
    SupplierExtractor,
    SupplierSnapshot,
)

_CONTRACTS: tuple[Contract, ...] = (
    Contract(
        id=CUSTOM_CONTRACT_DYNAMIC,
        label="Dynamic (factor x spot + base)",
        kind="dynamic",
    ),
    Contract(
        id=CUSTOM_CONTRACT_MONTHLY,
        label="Monthly average (factor x monthly-mean spot + base)",
        kind="spot_monthly",
    ),
    Contract(
        id=CUSTOM_CONTRACT_FIXED,
        label="Fixed / manual rate",
        kind="fixed",
    ),
)


async def _fetch(
    session: aiohttp.ClientSession, contract_id: str, region: str
) -> SupplierSnapshot:
    """Never called: custom snapshots are assembled by the coordinator from
    the config entry (there is no card to fetch)."""
    raise ExtractorError(
        "custom-formula snapshots are built by the coordinator, not fetched"
    )


EXTRACTOR = SupplierExtractor(
    id=SUPPLIER_CUSTOM,
    label="Expert: custom formula (no public card)",
    contracts=_CONTRACTS,
    fetch=_fetch,
    probe=None,
    fetch_for_month=None,
)
