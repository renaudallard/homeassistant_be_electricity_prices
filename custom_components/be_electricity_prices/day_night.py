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

"""The classic two-register (day / night) schedule, by region.

Which band an hour falls in on a bi-hourly or digital meter, and the federal
public holidays that schedule and the time-of-use ones read. Pure functions,
no Home Assistant dependencies.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Final

from .const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)

# Federal fixed-date holidays (month, day). Lifted to module scope so
# is_belgian_holiday doesn't reallocate the set on every call along the
# 8760-iteration backfill path and the per-hour TOU slot lookup.
_FIXED_HOLIDAYS: Final[frozenset[tuple[int, int]]] = frozenset(
    {(1, 1), (5, 1), (7, 21), (8, 15), (11, 1), (11, 11), (12, 25)}
)


def _easter_sunday(year: int) -> date:
    """Anonymous Gregorian computus for Western (Catholic) Easter Sunday."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def is_belgian_holiday(d: date) -> bool:
    """Federal Belgian public holidays.

    Fixed dates: New Year (1/1), Labour Day (1/5), National Day (21/7),
    Assumption (15/8), All Saints (1/11), Armistice (11/11), Christmas
    (25/12). Easter-derived: Easter Monday (+1), Ascension (+39),
    Pentecost Monday (+50). Regional holidays (Walloon, Flemish,
    Brussels) are deliberately excluded: DSO billing applies federal
    rules uniformly.
    """
    if (d.month, d.day) in _FIXED_HOLIDAYS:
        return True
    easter = _easter_sunday(d.year)
    if d == easter + timedelta(days=1):  # Easter Monday
        return True
    if d == easter + timedelta(days=39):  # Ascension (Thursday)
        return True
    return d == easter + timedelta(days=50)  # Pentecost Monday


def is_offpeak(when: datetime, region: str = REGION_FLANDERS) -> bool:
    """Classic bi-hourly (tweevoudig / bi-horaire) off-peak hour, by region.

    The night/weekend schedule and the public-holiday treatment differ by
    region, and Wallonia changed on 2026-01-01:

      Flanders (Fluvius): off-peak Mon-Fri 22:00-07:00 and all weekend.
        Weekday public holidays bill at the DAY rate - the meter's tariff
        clock switches on weekday/weekend only, not on holidays.
      Brussels (Sibelga): off-peak 22:00-07:00, all weekend AND weekday
        public holidays (the historical Brussels exception; unchanged).
      Wallonia (from 2026-01-01): one uniform schedule every day including
        weekends and holidays - off-peak 22:00-07:00 and 11:00-17:00.
    """
    if region == REGION_WALLONIA and when.date() >= _WALLONIA_SCHEDULE_FROM:
        return when.hour < 7 or when.hour >= 22 or 11 <= when.hour < 17
    if when.weekday() >= 5:
        return True
    if region == REGION_BRUSSELS and is_belgian_holiday(when.date()):
        return True
    return when.hour < 7 or when.hour >= 22


# Wallonia's uniform every-day off-peak schedule (22:00-07:00 plus the
# 11:00-17:00 midday window) took effect on this date. Before it, Wallonia
# billed the same classic bi-hourly schedule as Flanders. The date matters
# because ``backfill_statistics`` accepts a user-supplied start with no lower
# bound, so a manual backfill reaching into 2025 would otherwise re-band six
# hours of every day onto a tariff that was not in force yet. Every automatic
# path anchors at 1 January of the current year and never sees these hours.
_WALLONIA_SCHEDULE_FROM: Final[date] = date(2026, 1, 1)
