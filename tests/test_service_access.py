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

"""Who may call the integration's actions."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.setup import async_setup_component

import custom_components.be_electricity_prices as pkg
from custom_components.be_electricity_prices.const import DOMAIN


async def test_only_an_administrator_can_backfill(
    hass: HomeAssistant, hass_read_only_user: Any, hass_admin_user: Any
) -> None:
    """clear deletes a statistic's whole history, so a user who is not an
    administrator is refused, as on Home Assistant's own statistics
    actions; an administrator and an automation, which has no user, are
    not."""
    backfill = AsyncMock(return_value=None)
    with patch.object(pkg, "_async_backfill_service", backfill):
        assert await pkg.async_setup(hass, {})
        data = {"clear": True}
        with pytest.raises(Unauthorized):
            await hass.services.async_call(
                DOMAIN,
                pkg.SERVICE_BACKFILL_STATISTICS,
                data,
                blocking=True,
                context=Context(user_id=hass_read_only_user.id),
            )
        assert backfill.await_count == 0
        for context in (Context(user_id=hass_admin_user.id), None):
            await hass.services.async_call(
                DOMAIN,
                pkg.SERVICE_BACKFILL_STATISTICS,
                data,
                blocking=True,
                context=context,
            )
    assert backfill.await_count == 2


async def test_only_an_administrator_can_refresh(
    hass: HomeAssistant, hass_read_only_user: Any, hass_admin_user: Any
) -> None:
    """A refresh fetches every card again past its age and its backoff, and
    clear_history throws away the day-ahead prices the year to date replays,
    the hours before 1 January for good. So a user who is not an
    administrator is refused, read-only or not. An administrator is not, and
    neither is an automation: its actions run with no user, even when an
    event a user fired set it off."""
    refresh = AsyncMock(return_value=None)
    household = await hass.auth.async_create_user(
        "Household", group_ids=[GROUP_ID_USER]
    )
    assert not household.is_admin
    data = {"clear_history": True}
    with patch.object(pkg, "_async_refresh_service", refresh):
        assert await pkg.async_setup(hass, {})
        for user in (hass_read_only_user, household):
            with pytest.raises(Unauthorized):
                await hass.services.async_call(
                    DOMAIN,
                    pkg.SERVICE_REFRESH,
                    data,
                    blocking=True,
                    context=Context(user_id=user.id),
                )
        assert refresh.await_count == 0
        await hass.services.async_call(
            DOMAIN,
            pkg.SERVICE_REFRESH,
            data,
            blocking=True,
            context=Context(user_id=hass_admin_user.id),
        )
        assert refresh.await_count == 1
        assert await async_setup_component(
            hass,
            "automation",
            {
                "automation": {
                    "triggers": {"trigger": "event", "event_type": "refresh_now"},
                    "actions": {"action": f"{DOMAIN}.refresh", "data": data},
                }
            },
        )
        hass.bus.async_fire("refresh_now", context=Context(user_id=household.id))
        await hass.async_block_till_done()
    assert refresh.await_count == 2
