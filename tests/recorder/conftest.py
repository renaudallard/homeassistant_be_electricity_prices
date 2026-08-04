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

"""Fixture overrides for tests that need a REAL recorder database.

``recorder_mock`` builds its database before ``hass`` exists: its
``recorder_db_url`` dependency asserts ``hass_fixture_setup`` is still empty.
The parent ``tests/conftest.py`` has two autouse fixtures that pull ``hass``
in first (``enable_custom_integrations`` depends on it, and the Brussels
timezone pin resolves it), so a recorder test cannot run under them.

Overriding both by name here, for this directory only, keeps the rest of the
suite byte-identical -- resolving them lazily in the parent instead reordered
fixture setup and unpinned the Brussels timezone for six tests.

These tests exercise how Home Assistant's recorder compiles OUR statistics,
so they need neither the custom-integration loader nor a Belgian clock.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations() -> None:
    """No-op override: a recorder test loads no custom integration."""
    return


@pytest.fixture(autouse=True)
def _force_brussels_timezone() -> Iterator[None]:
    """No-op override: these tests assert on UTC statistics rows."""
    yield
