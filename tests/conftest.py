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

"""Make the integration importable from tests without a Home Assistant install."""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,  # noqa: ARG001
) -> None:
    """Enable custom_components/ loading for every test that uses hass."""
    return


@pytest.fixture(autouse=True)
def _force_brussels_timezone(request: pytest.FixtureRequest):
    """Pin every test to Europe/Brussels.

    The pytest-homeassistant-custom-component ``hass`` fixture sets
    ``US/Pacific`` by default; for a Belgian-electricity integration
    that hides DST, off-peak window, and per-month archive bugs that
    would surface in production. Sync fixture so it doesn't drag the
    event loop into pure-helper tests; when ``hass`` is requested we
    force it to set up first (it sets US/Pacific) and then override
    to Brussels.
    """
    if "hass" in request.fixturenames:
        hass: HomeAssistant = request.getfixturevalue("hass")
        # async_set_time_zone updates dt_util.DEFAULT_TIME_ZONE in
        # addition to hass.config.time_zone; the call has to await,
        # but we're in a sync fixture, so route through hass's
        # async_create_task and block on it.
        hass.loop.run_until_complete(hass.config.async_set_time_zone("Europe/Brussels"))
        yield
        return
    orig = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(ZoneInfo("Europe/Brussels"))
    try:
        yield
    finally:
        dt_util.set_default_time_zone(orig)
