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

"""A meter outage's energy, shared out over the days it has no statistics for.

Home Assistant resumes a total_increasing sensor's sum from the last
five-minute row it compiled, so the first day back after an outage holds the
energy of the days without rows as well as its own. Counted as one day of a
window scaled across the days missing, that energy counted twice. An outage
longer than purge_keep_days has lost its last row: the sum starts over, the
first day back reads a negative change that ``_read_deltas`` drops, and that
energy is gone, so its days stay missing.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, timedelta
from itertools import pairwise

from homeassistant.components.sensor import ATTR_STATE_CLASS, SensorStateClass
from homeassistant.core import HomeAssistant

# The local days a daily read dropped for a negative change, inside a
# noting_restarts block.
_RESTARTS: ContextVar[set[date] | None] = ContextVar("_RESTARTS", default=None)


@contextmanager
def noting_restarts() -> Iterator[set[date]]:
    """Collect the days the daily reads inside this block dropped for a
    negative change: a sum the recorder started over."""
    restarts: set[date] = set()
    token = _RESTARTS.set(restarts)
    try:
        yield restarts
    finally:
        _RESTARTS.reset(token)


def note_restart(day: date) -> None:
    restarts = _RESTARTS.get()
    if restarts is not None:
        restarts.add(day)


def carries_outages(hass: HomeAssistant, entity_id: str) -> bool:
    """Whether the meter's statistics carry its count across an outage: a
    total_increasing sensor's do. Another meter may have lost it, as a total
    starting a new cycle, or a statistic some integration imports."""
    state = hass.states.get(entity_id)
    return (
        state is not None
        and state.attributes.get(ATTR_STATE_CLASS) == SensorStateClass.TOTAL_INCREASING
    )


def spread_outages(
    days: Mapping[date, float], restarts: Collection[date]
) -> dict[date, float]:
    """``days`` with the energy of each outage shared out over its days.

    The first day back's figure is split evenly over the days missing
    before it and itself. An outage holding a day in ``restarts`` lost its
    energy and keeps its days missing, as do the days before the first one
    on record.
    """
    out = dict(days)
    for before, after in pairwise(sorted(days)):
        missed = (after - before).days - 1
        if missed <= 0 or any(before < day < after for day in restarts):
            continue
        share = days[after] / (missed + 1)
        for n in range(1, missed + 2):
            out[before + timedelta(days=n)] = share
    return out
