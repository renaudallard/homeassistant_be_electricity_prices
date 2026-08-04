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

"""The uniqueness key a new config entry claims.

An exclusive-night circuit is a whole-entry meter type, so const.py and the
docs tell the user to configure it as a SECOND entry. A household has one
contract on one DSO, so that second entry carries the same (supplier,
contract, region, dso) tuple as the first: while the meter was absent from
the key, the documented setup aborted with already_configured every time and
could not be performed at all.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices.config_flow import (
    BePricesConfigFlow,
    _unique_id_for,
)
from custom_components.be_electricity_prices.const import (
    CONF_CONTRACT,
    CONF_DSO,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    METER_BI,
    METER_DYNAMIC,
    METER_EXCLUSIVE_NIGHT,
    METER_MONO,
)


async def _unique_for(hass: HomeAssistant, meter: str) -> str:
    """Run _after_meter and report the key it claimed."""
    flow = BePricesConfigFlow()
    flow.hass = hass
    flow._data = {
        CONF_SUPPLIER: "ebem",
        CONF_CONTRACT: "ebem_variable",
        CONF_REGION: "flanders",
        CONF_DSO: "fluvius_antwerpen",
        CONF_METER: meter,
    }
    seen: dict[str, Any] = {}

    async def _capture(_self: Any, value: str | None, **kwargs: Any) -> None:
        seen["unique"] = value

    with (
        patch.object(BePricesConfigFlow, "async_set_unique_id", _capture),
        patch.object(BePricesConfigFlow, "_abort_if_unique_id_configured"),
        patch(
            "custom_components.be_electricity_prices.config_flow._WizardStepsMixin"
            "._after_meter",
            AsyncMock(return_value=None),
        ),
    ):
        await flow._after_meter()
    return str(seen["unique"])


async def test_standard_meters_keep_the_historical_key(hass: HomeAssistant) -> None:
    """Entries created before the night circuit was addressable carry the
    four-part key, so the standard meters must keep claiming exactly that or
    an existing entry stops matching and a real duplicate slips through."""
    base = "ebem:ebem_variable:flanders:fluvius_antwerpen"
    assert await _unique_for(hass, METER_MONO) == base
    assert await _unique_for(hass, METER_BI) == base
    assert await _unique_for(hass, METER_DYNAMIC) == base


async def test_exclusive_night_claims_its_own_key(hass: HomeAssistant) -> None:
    """The night circuit gets its own key, so it no longer collides with the
    household's main entry on the same tuple."""
    assert await _unique_for(hass, METER_EXCLUSIVE_NIGHT) == (
        f"ebem:ebem_variable:flanders:fluvius_antwerpen:{METER_EXCLUSIVE_NIGHT}"
    )


async def test_two_night_circuits_on_one_tuple_still_collide(
    hass: HomeAssistant,
) -> None:
    """The duplicate check still bites where it should: a household has one
    night circuit per contract and DSO."""
    first = await _unique_for(hass, METER_EXCLUSIVE_NIGHT)
    second = await _unique_for(hass, METER_EXCLUSIVE_NIGHT)
    assert first == second


def test_install_and_edit_build_the_same_key() -> None:
    """The OptionsFlow compares the edited data against other entries' ids
    using this same helper. If edit rebuilt the plain tuple, opening a
    night-circuit entry's options would find the household's main entry
    holding exactly that string and abort already_configured, making the
    entry uneditable."""
    data = {
        CONF_SUPPLIER: "ebem",
        CONF_CONTRACT: "ebem_variable",
        CONF_REGION: "flanders",
        CONF_DSO: "fluvius_antwerpen",
        CONF_METER: METER_EXCLUSIVE_NIGHT,
    }
    assert _unique_id_for(data) == (
        f"ebem:ebem_variable:flanders:fluvius_antwerpen:{METER_EXCLUSIVE_NIGHT}"
    )
    assert _unique_id_for({**data, CONF_METER: METER_MONO}) == (
        "ebem:ebem_variable:flanders:fluvius_antwerpen"
    )


def test_missing_meter_falls_back_to_the_plain_tuple() -> None:
    """A half-filled dict during the wizard must not invent a key shape."""
    assert (
        _unique_id_for(
            {
                CONF_SUPPLIER: "ebem",
                CONF_CONTRACT: "ebem_variable",
                CONF_REGION: "flanders",
                CONF_DSO: "fluvius_antwerpen",
            }
        )
        == "ebem:ebem_variable:flanders:fluvius_antwerpen"
    )
