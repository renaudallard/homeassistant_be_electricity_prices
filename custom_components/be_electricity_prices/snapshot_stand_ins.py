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

"""The archive's row of an earlier month, for an entry left with no card.

A withdrawn product is priced off the last month it was sold, and an entry
whose card cannot be had or has gone stale off the newest row the
repository's card archive holds for it. Both honour the entry's card-archive
box through ``_archive_allowed``.

The archive is read through ``snapshot_months`` at call time rather than
imported by name, so a patch on its reader there reaches these too.
"""

from __future__ import annotations

from datetime import date, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry

from . import snapshot_months
from .snapshot_months import ArchivedCard, _archive_allowed


def month_before(day: date) -> date:
    """The first day of the month before ``day``'s."""
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


async def card_of_month_before(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    day: date,
    entry: ConfigEntry | None,
) -> ArchivedCard | None:
    """The archive's row for the month before ``day``'s, for an entry left
    with no card. Honours the card-archive box like
    ``card_for_unreadable_month``.

    A withdrawn product whose card the supplier took down is priced off the
    last month it was sold, the month before ``withdrawn``.
    """
    if not _archive_allowed(entry):
        return None
    return await snapshot_months._archived_card_from_github(
        session, supplier, contract, region, month_before(day)
    )


# How far back the card archive is asked for a card to stand in, a month at a
# time: as far as it keeps rows.
_ARCHIVE_MONTHS_BACK = 12


async def newest_archived_card(
    session: aiohttp.ClientSession,
    supplier: str,
    contract: str,
    region: str,
    today: date,
    entry: ConfigEntry | None,
) -> tuple[date, ArchivedCard] | None:
    """The newest row the archive holds for this card, and its month.

    This month's row first, then back a month at a time, stopping at the
    first one that holds a card. For an entry whose supplier's card cannot be
    had or has gone stale: the archive walks every supplier daily, and its
    row may well be newer than what the supplier answered this entry. A
    transient failure propagates, since a month it could not read may still
    hold one. Honours the card-archive box like the reader above.
    """
    if not _archive_allowed(entry):
        return None
    month = today.replace(day=1)
    for _ in range(_ARCHIVE_MONTHS_BACK):
        archived = await snapshot_months._archived_card_from_github(
            session, supplier, contract, region, month
        )
        if archived is not None:
            return month, archived
        month = month_before(month)
    return None
