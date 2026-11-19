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
``snapshot_store.py``), and when a month's row is kept for good."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

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


@pytest.mark.parametrize(
    ("zone", "first", "asked"),
    [
        # Five past midnight on 1 December in Tokyo is 16:05 on 30 November
        # in Belgium, before a card put up again at 17:00 that day: asked as
        # an open month, then once more when November is over in Belgium.
        ("Asia/Tokyo", "2026-11-30 15:05:00+00:00", ["16:05", "17:00"]),
        # Five past midnight on 1 December in Los Angeles is 09:05 on 1
        # December in Belgium: November is over there too, asked once.
        ("America/Los_Angeles", "2026-12-01 08:05:00+00:00", ["17:00"]),
    ],
)
async def test_a_month_is_kept_for_good_once_it_is_over_in_belgium(
    hass: HomeAssistant, freezer: Any, zone: str, first: str, asked: list[str]
) -> None:
    """Cards are Belgian, and a closed month's card is kept for good. Asked
    as soon as Home Assistant's date turned, east of Belgium a card put up
    later that day in Belgium was never read."""
    served: list[str] = []

    async def fetch_for_month(
        _session: object, _contract: str, _region: str, _month: date
    ) -> SupplierSnapshot:
        # November's card as served when asked: put up again at 17:00 on
        # 30 November in Belgium.
        late = dt_util.utcnow() >= datetime(2026, 11, 30, 16, 0, tzinfo=UTC)
        served.append("17:00" if late else "16:05")
        return replace(make_snapshot(), publication_label=served[-1])

    extractor = replace(
        make_stub_extractor(extractor_id=_TUPLE[0]), fetch_for_month=fetch_for_month
    )
    entry = make_entry(card_archive=False)
    await hass.config.async_set_time_zone(zone)

    async def november() -> str:
        card = await month_card(
            hass,
            None,  # type: ignore[arg-type]
            extractor,
            _TUPLE[1],
            _TUPLE[2],
            date(2026, 11, 1),
            entry,
        )
        assert card is not None
        return card.publication_label

    freezer.move_to(first)
    assert await november() == asked[0]
    for later in ("2026-11-30 23:30:00+00:00", "2026-12-02 09:00:00+00:00"):
        freezer.move_to(later)
        assert await november() == asked[-1]
    assert served == asked
