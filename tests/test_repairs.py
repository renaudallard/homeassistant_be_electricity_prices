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

"""The stale-card Repairs card's fix flow: fetch the card again, and close
the card only when that cleared it."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices.const import DOMAIN
from custom_components.be_electricity_prices.coordinator import BePricesCoordinator
from tests import make_entry, make_snapshot


def _stale_entry(hass: HomeAssistant) -> BePricesCoordinator:
    entry = make_entry()
    entry.add_to_hass(hass)
    coord = BePricesCoordinator(hass, entry)
    entry.runtime_data = coord
    coord._set_snapshot(make_snapshot())
    coord._snapshot_fetched_at = dt_util.utcnow() - timedelta(days=10)
    coord._last_error = "HTTP 503 fetching https://card.test/x.pdf"
    coord._sync_stale_issue(coord._snapshot_overdue())
    return coord


@pytest.mark.parametrize("cured", [True, False])
async def test_fetching_again_closes_the_card_only_when_it_is_cleared(
    hass: HomeAssistant, cured: bool
) -> None:
    # The integration set up before its entry is added, so the entry's
    # coordinator is the one made here rather than one its setup would make.
    assert await async_setup_component(hass, "repairs", {})
    assert await async_setup_component(hass, DOMAIN, {})
    coord = _stale_entry(hass)
    issue_id = f"snapshot_stale_{coord.entry.entry_id}"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.is_fixable
    assert issue.data == {"entry_id": coord.entry.entry_id}

    manager = hass.data["repairs"]["flow_manager"]
    form = await manager.async_init(DOMAIN, data={"issue_id": issue_id})
    assert form["type"] is FlowResultType.FORM
    # The card's own wording, placeholders filled.
    assert form["description_placeholders"] is not None
    assert form["description_placeholders"]["days"] == "7"

    async def _refresh(*_args: object, wait: bool = False) -> None:
        assert wait
        if cured:
            coord._snapshot_fetched_at = dt_util.utcnow()

    with patch.object(coord, "async_force_refresh", AsyncMock(side_effect=_refresh)):
        done = await manager.async_configure(form["flow_id"], {})
    left = ir.async_get(hass).async_get_issue(DOMAIN, issue_id)
    if cured:
        assert done["type"] is FlowResultType.CREATE_ENTRY
        assert left is None
    else:
        # An abort leaves the Repairs card where it is.
        assert done["type"] is FlowResultType.ABORT
        assert done["reason"] == "still_stale"
        assert done["description_placeholders"] == {
            "last_error": "HTTP 503 fetching https://card.test/x.pdf"
        }
        assert left is not None


async def test_a_forced_refresh_can_wait_for_its_tick(hass: HomeAssistant) -> None:
    """The fix flow reads the card after the fetch, so the refresh it asks
    for runs now rather than after the debouncer's cooldown."""
    coord = _stale_entry(hass)
    with patch.object(coord, "async_refresh", AsyncMock()) as refresh:
        await coord.async_force_refresh(wait=True)
    refresh.assert_awaited_once()
    assert coord._force_refresh
