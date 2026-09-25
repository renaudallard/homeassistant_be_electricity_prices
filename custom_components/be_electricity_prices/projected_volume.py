# Copyright (c) 2026 Renaud Allard
#
# Permission to use, copy, modify, and distribute this software for any
# purpose with or without fee is hereby granted, provided that the above
# copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
# ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
# WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
# ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
# OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""Projected calendar-year consumption and injection.

What this calendar year will have metered by 31 December: the closed days
since 1 January as measured, plus the rest of the year taken from the same
calendar days of last year. Today counts as part of the rest, so the figure
moves once a day rather than with every live meter reading.

The rest is never a day count applied to a yearly total. Load is seasonal: on
Synergrid's 2026 residential profile the last 97 days of the year carry 30% of
the load against the 27% their share of the calendar suggests, and solar
output is lopsided the other way. Last year's same days carry the household's
own season, which is why they come first.

When the recorder does not hold last year's days, an entry that already holds
a Synergrid profile (the residential load profile for consumption, the solar
production profile for injection) extrapolates this year's measured days on
it instead. The profile is never downloaded for this: it is used only where
the pricing already loaded it. Otherwise there is no number, and the basis
says what is missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import MEASURED_FULL_YEAR_DAYS, MEASURED_MIN_DAYS, MEASURED_YEAR_GAP_DAYS
from .energy_meters import _kwh_sensor_ids, _measured_kwh

_PROFILE_NAME = {
    "consumption": "residential load",
    "injection": "solar production",
}


def _covers(days_with_data: int, window: int) -> bool:
    """Whether a metered window is complete enough to stand for itself.

    The gap ``_covers_a_year`` allows on a year, scaled to the window, so a
    few missing recorder buckets do not throw away a whole measurement.
    """
    gap = MEASURED_YEAR_GAP_DAYS * window / MEASURED_FULL_YEAR_DAYS
    return days_with_data > 0 and days_with_data >= window - gap


def _elapsed_share(
    weights: Mapping[tuple[int, int, int], float], today: date, *, utc: bool
) -> float | None:
    """The profile's share of the year before local midnight of ``today``.

    The RLP is keyed on local (month, day, hour) and the SPP on UTC, so the
    cut is taken on the profile's own clock.
    """
    total = sum(weights.values())
    if total <= 0.0:
        return None
    cut = dt_util.start_of_local_day(today)
    if utc:
        cut = cut.astimezone(UTC)
    if cut.year < today.year:
        return 0.0
    key = (cut.month, cut.day, cut.hour)
    return sum(w for k, w in weights.items() if k < key) / total


async def _compute_projected_year_kwh(
    hass: HomeAssistant,
    entry: ConfigEntry,
    today: date,
    *,
    side: str,
    profile: Mapping[tuple[int, int, int], float] | None = None,
    profile_utc: bool = False,
    breakdown: dict[str, Any],
) -> float | None:
    """kWh ``side`` will have metered over ``today``'s calendar year, or ``None``.

    ``profile`` is this year's Synergrid curve for ``side`` where the
    coordinator holds one, ``None`` otherwise; ``profile_utc`` says which
    clock its keys are on. ``breakdown`` receives the basis and both halves.
    """
    if not any(_kwh_sensor_ids(entry, side)):
        breakdown["volume_basis"] = f"not projected: no {side} meter is wired"
        return None

    jan1 = date(today.year, 1, 1)
    elapsed = (today - jan1).days
    ytd_kwh = 0.0
    if elapsed:
        ytd = await _measured_kwh(
            hass, entry, jan1, today - timedelta(days=1), side=side
        )
        if not _covers(ytd.days_with_data, elapsed):
            breakdown["volume_basis"] = (
                f"not projected: the {side} meter recorded {ytd.days_with_data} "
                f"of the {elapsed} days since 1 January"
            )
            return None
        ytd_kwh = ytd.kwh * elapsed / ytd.days_with_data

    remaining = (date(today.year, 12, 31) - today).days + 1
    # 29 February has no counterpart, so the 28th stands in. The two windows
    # differ by a day whenever either holds a leap day, and the scaling below
    # absorbs it.
    last_start = (
        date(today.year - 1, 2, 28)
        if (today.month, today.day) == (2, 29)
        else today.replace(year=today.year - 1)
    )
    last_end = date(today.year - 1, 12, 31)
    last = await _measured_kwh(hass, entry, last_start, last_end, side=side)
    if _covers(last.days_with_data, (last_end - last_start).days + 1):
        rest = last.kwh * remaining / last.days_with_data
        basis = (
            f"measured: {elapsed} days this year, and the same "
            f"{remaining} days of last year"
        )
    else:
        share = _elapsed_share(profile, today, utc=profile_utc) if profile else None
        if not share or elapsed < MEASURED_MIN_DAYS:
            missing = (
                f"no Synergrid {_PROFILE_NAME[side]} profile is held"
                if share is None
                else f"{elapsed} days of this year are too few to extrapolate"
            )
            breakdown["volume_basis"] = (
                f"not projected: last year's history does not cover "
                f"{last_start} to {last_end}, and {missing}"
            )
            return None
        rest = ytd_kwh * (1.0 - share) / share
        basis = (
            f"measured: {elapsed} days this year, the rest extrapolated on "
            f"Synergrid's {_PROFILE_NAME[side]} profile"
        )

    breakdown["volume_basis"] = basis
    breakdown["ytd_kwh"] = ytd_kwh
    breakdown["remaining_kwh"] = rest
    return ytd_kwh + rest
