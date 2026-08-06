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

"""The Flemish capacity tariff's monthly peak tracking.

Split out of coordinator.py. Kept separate from the spot mixin: zero calls in
either direction and zero shared state, and a module named for spot prices
containing _billed_peak_kw would be actively misleading.

This file holds the only back-edge to the concrete coordinator --
reset_monthly_peak calls _save_persistent and async_request_refresh -- so both
stubs live here, with signatures matching DataUpdateCoordinator exactly."""

from __future__ import annotations

from homeassistant.core import State

import logging

from .const import (
    CAPACITY_MODE_FIXED,
    CAPACITY_MODE_SENSOR,
    CONF_CAPACITY_FIXED_KW,
    CONF_CAPACITY_MODE,
    CONF_CAPACITY_PEAK_SENSOR,
    CONF_REGION,
    REGION_FLANDERS,
    VREG_CAPACITY_FLOOR_KW,
)

from datetime import date, datetime
from typing import TYPE_CHECKING
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .providers.base import SupplierSnapshot


_LOGGER = logging.getLogger(__name__)


class _PeakMixin:
    """Mixed into BePricesCoordinator."""

    # Entry-owned state, declared as BARE annotations with no value. A valued
    # class attribute would change hasattr() and instance-dict behaviour;
    # __init__ in the concrete class is what actually creates these.
    entry: ConfigEntry
    _peak_kw: float
    _peak_month: date | None
    _peak_history: dict[str, float]
    _unloaded: bool
    _snapshot: SupplierSnapshot | None
    _snapshot_raw: SupplierSnapshot | None
    _snapshot_fetched_at: datetime | None
    _snapshot_probe_key: str | None
    _last_error: str | None
    _supplier_tuple: tuple[str, str, str]

    if TYPE_CHECKING:
        # Provided by DataUpdateCoordinator, and by the sibling mixins. Stubs
        # rather than inheritance: a mixin inheriting
        # DataUpdateCoordinator[CoordinatorData] would need CoordinatorData,
        # which lives in coordinator.py and is imported from there by sensor,
        # binary_sensor and diagnostics -- a cycle.
        hass: HomeAssistant

        async def _save_persistent(self) -> None: ...
        async def async_request_refresh(self) -> None: ...

    async def reset_monthly_peak(self) -> None:
        """Drop the persisted monthly peak so the next tick rebuilds it.

        Exposed via the diagnostic Reset peak button. Required when an
        earlier release stored an inflated peak (e.g. a W-unit sensor
        misread as kW pre-0.5.45) and the rolling-max comparison would
        otherwise hold the bad value until the next 1st of the month.
        Persists immediately so the reset survives an HA restart between
        now and the next coordinator tick. The banked history goes too: a
        bad value that has already rolled into a completed month would
        otherwise keep dragging the twelve-month mean for a year.
        """
        self._peak_kw = 0.0
        self._peak_history.clear()
        await self._save_persistent()
        await self.async_request_refresh()

    async def _track_monthly_peak(self) -> None:
        if self.entry.data.get(CONF_REGION) != REGION_FLANDERS:
            # Outside Flanders the capacity tariff doesn't apply. Reset
            # any peak left over from a previous Flanders config so it
            # doesn't linger in diagnostics or the persistent store. The
            # banked window goes too, or moving back to Flanders later would
            # resume billing on year-old peaks from the previous address;
            # Fluvius likewise restarts the window when the grid user changes.
            self._peak_kw = 0.0
            self._peak_month = None
            self._peak_history.clear()
            return
        # Roll over on the local 1st-of-month; using UTC would lag CET/CEST
        # users by 1-2 hours on the boundary and miss late-Dec-31 / early-Jan-1.
        local_now = dt_util.now()
        current_month = date(local_now.year, local_now.month, 1)
        if self._peak_month != current_month:
            # Bank the month that just closed before resetting: the capacity
            # tariff bills the mean of the last twelve monthly peaks, not the
            # one being accumulated. A peak of 0 means the month collected no
            # reading at all (fresh entry, or HA down throughout), which is not
            # a measured 0 and must not drag the mean down.
            if self._peak_month is not None and self._peak_kw > 0.0:
                self._peak_history[self._peak_month.isoformat()] = self._peak_kw
            # Eleven completed months plus the running one make twelve.
            for stale in sorted(self._peak_history)[:-11]:
                del self._peak_history[stale]
            self._peak_month = current_month
            self._peak_kw = 0.0

        mode = self.entry.data.get(CONF_CAPACITY_MODE)
        if mode == CAPACITY_MODE_FIXED:
            # Use the configured value directly; rolling-max would
            # ignore a mid-month decrease the user just made via
            # OptionsFlow until next month rollover.
            self._peak_kw = float(
                self.entry.data.get(CONF_CAPACITY_FIXED_KW, VREG_CAPACITY_FLOOR_KW)
            )
        elif mode == CAPACITY_MODE_SENSOR:
            entity_id = self.entry.data.get(CONF_CAPACITY_PEAK_SENSOR)
            state: State | None = self.hass.states.get(entity_id) if entity_id else None
            if state is not None and state.state not in ("unknown", "unavailable"):
                try:
                    value = float(state.state)
                except (TypeError, ValueError):
                    value = 0.0
                # Scale by the source unit: the auto-pick walks back
                # from the Energy dashboard kWh sensor to a Riemann
                # integration source, which is almost always a power
                # sensor in W. Without scaling, 4481 W is stored as
                # 4481 kW and the capacity_cost sensor inflates by
                # 1000x (issue #19). An empty / missing unit is kept
                # as kW for back-compat with sensors that never set
                # the attribute.
                unit = (state.attributes.get("unit_of_measurement") or "").strip()
                if unit in ("W", "VA"):
                    value *= 0.001
                elif unit not in ("", "kW", "kVA"):
                    _LOGGER.warning(
                        "capacity peak sensor %s reports in %r; "
                        "expected kW/W/VA/kVA, ignoring this update",
                        entity_id,
                        unit,
                    )
                    value = 0.0
                if value > self._peak_kw:
                    self._peak_kw = value

    def _peak_terms(self) -> list[float]:
        """The monthly peaks that go into the mean, newest last.

        Only count the in-progress month once it HAS a measurement. It is
        reset to 0 on the local 1st, and a zero floored to 2.5 is not a
        measured peak: including it dropped the twelve-term mean at every
        rollover, stepping capacity_cost and current_year_cost down for the
        first hours of every month and back up as the month accrued. This is
        the same rule ``_billed_peak_kw`` documents for a month that was never
        measured, and for the same reason: Fluvius estimates a missing month
        as the mean of the validated ones, and inserting a set's own mean
        leaves the mean unchanged, so leaving the gap out lands on the same
        number.

        The count is published as ``capacity_peak_months``, so it has to come
        from here rather than be recomputed at the call site: reporting
        ``len(self._peak_history) + 1`` claimed a month the mean had not taken
        for as long as the in-progress one stayed unmeasured.
        """
        peaks = list(self._peak_history.values())
        if self._peak_kw > 0.0:
            peaks.append(self._peak_kw)
        return peaks

    def _billed_peak_kw(self) -> float:
        """The kW the capacity tariff is actually charged on.

        Fluvius bills the "gemiddelde maandpiek", and its own methodology gives
        the formula outright: "Rekenkundig gemiddelde van de Max (Maandpiek
        (m), 2.5) voor elke maand (m) ... Er worden maximaal 12 maanden
        gebruikt." So the regulated minimum lands on EACH month before the
        mean, not on the mean, and one monthly peak is the highest
        quarter-hour offtake of that month. Because every term is then at
        least the floor, the mean is too, and no outer clamp is needed.

        Fewer than twelve months of history means the mean is taken over what
        there is, so a fresh entry starts out billing on this month alone
        (exactly what it did before the window existed) and converges over the
        first year. That also covers a month we never measured: Fluvius
        estimates a missing month as the mean of the validated ones, and
        inserting a set's own mean into it leaves the mean unchanged, so
        simply leaving the gap out lands on the same number. Fixed mode
        bypasses the window entirely: the user is stating a peak, not
        measuring one.
        """
        if self.entry.data.get(CONF_CAPACITY_MODE) == CAPACITY_MODE_FIXED:
            return max(self._peak_kw, VREG_CAPACITY_FLOOR_KW)
        peaks = self._peak_terms()
        if not peaks:
            # A brand-new entry in the first hours of its first month has
            # nothing measured anywhere; the regulated minimum is the answer.
            return VREG_CAPACITY_FLOOR_KW
        return sum(max(kw, VREG_CAPACITY_FLOOR_KW) for kw in peaks) / len(peaks)
