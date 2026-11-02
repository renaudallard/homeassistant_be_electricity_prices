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

"""A table the card archive publishes beside the cards, read once a day.

The VAT rate the cards of each month agree on (vat_rates.py) and the
excise the archive kept (law_archive.py) are each one JSON file in
be_price_cards, read by an entry that lets the card archive be read. A
failure keeps what is held and is asked again after six hours.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Final

import aiohttp
from homeassistant.util import dt as dt_util

from .providers._pdf import fetch_text

_LOGGER = logging.getLogger(__name__)

_REFRESH_S: Final = 24 * 3600
_FAILURE_RETRY_S: Final = 6 * 3600


class ArchiveTable:
    """One of those files, which ``hold`` takes as read."""

    def __init__(self, url: str, hold: Callable[[Mapping[str, Any]], None]) -> None:
        self.url = url
        self._hold = hold
        self.fetched_at: datetime | None = None
        self.failed_at: datetime | None = None

    async def ensure(self, session: aiohttp.ClientSession) -> None:
        """Read the table again once a day. Nothing here raises, since the
        caller is the coordinator tick."""
        now = dt_util.utcnow()
        if (
            self.fetched_at is not None
            and (now - self.fetched_at).total_seconds() < _REFRESH_S
        ):
            return
        if (
            self.failed_at is not None
            and (now - self.failed_at).total_seconds() < _FAILURE_RETRY_S
        ):
            return
        try:
            table = json.loads(await fetch_text(session, self.url))
            if not isinstance(table, Mapping):
                raise ValueError("not a table")
        except Exception as err:  # the docstring promises no raise
            _LOGGER.debug("%s could not be read: %s", self.url, err)
            self.failed_at = now
            return
        self._hold(table)
        self.fetched_at = now
        self.failed_at = None
