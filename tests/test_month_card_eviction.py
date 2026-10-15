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

"""A month row's fetch against an eviction of its supplier tuple
(``month_card`` in ``snapshot_months.py``, ``evict_shared_caches`` in
``snapshot_store.py``)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date

import pytest
from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices.providers.base import SupplierSnapshot
from custom_components.be_electricity_prices.snapshot_months import month_card
from custom_components.be_electricity_prices.snapshot_store import (
    _monthly_snapshots,
    evict_shared_caches,
)
from tests import make_entry, make_snapshot, make_stub_extractor

_TUPLE = ("eneco", "power_fix", "wallonia")
_ROW = (*_TUPLE, "2026-03")


@pytest.mark.parametrize("evicted", [False, True])
async def test_an_eviction_during_a_months_first_fetch_writes_no_row(
    hass: HomeAssistant, evicted: bool
) -> None:
    """Eviction bumped the generation of the month rows it found, and a month
    whose first fetch was still running had none: the fetch wrote its row
    after the tuple was gone, an orphan no entry would ever drop."""
    card = make_snapshot()
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch_for_month(
        _session: object, _contract: str, _region: str, _month: date
    ) -> SupplierSnapshot:
        started.set()
        await release.wait()
        return card

    extractor = replace(
        make_stub_extractor(extractor_id=_TUPLE[0]), fetch_for_month=fetch_for_month
    )
    task = asyncio.create_task(
        month_card(
            hass,
            None,  # type: ignore[arg-type]
            extractor,
            _TUPLE[1],
            _TUPLE[2],
            date(2026, 3, 1),
            make_entry(card_archive=False),
        )
    )
    await started.wait()
    assert _ROW not in _monthly_snapshots(hass)
    if evicted:
        evict_shared_caches(hass, _TUPLE, _TUPLE[0])
    release.set()
    await task
    assert (_ROW in _monthly_snapshots(hass)) is not evicted
