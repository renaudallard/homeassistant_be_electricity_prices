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

"""The compare-all page: sweeping every candidate, a few at a time.

Ranking the whole market takes longer than a dialog step may block for, so
this branch runs the sweep in slices behind a progress step and parks the
rows until the result page asks for them. The daily job enters here too, with
nobody watching.
"""

from __future__ import annotations

from .compare_flow import _CompareStepsMixin, _REFRESH_FIELD, _YTD_FIELD

from .compare_engine import _SweepEngine
from .compare_inputs import _candidate_label
from .compare_inputs import _effective_regime
from .compare_quote import RankedRow
from .compare_quote import _ranking_table
from .const import COMPARE_SWEEP_BUDGET_S
from .const import CONF_API_KEY
from .const import CONF_METER
from .const import METER_MONO
from .const import SOLAR_REGIME_INJECTION
from .const import SPOT_PRICED_CONTRACT_KINDS
from .flow_schemas import _contract_has_spot_injection
from .flow_schemas import _contract_kind
from .providers import get as get_extractor
from homeassistant.config_entries import ConfigEntry
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from typing import Any
import asyncio
import logging
import voluptuous as vol

_LOGGER = logging.getLogger(__name__)


async def async_run_daily_compare(
    hass: HomeAssistant, entry: ConfigEntry, coord: Any
) -> None:
    """Run the scheduled ranking and publish it through the coordinator.

    Swallows its own failures on purpose. This runs on a timer with nobody
    watching, and a supplier that changed its site overnight must not take an
    entry down with it: the ranking simply keeps yesterday's answer, which the
    sensor timestamps, rather than the whole entry going unavailable over a
    comparison nobody asked for at that moment.
    """
    engine = _SweepEngine(hass, entry, {})
    try:
        result = await engine.run_full_sweep(coord)
    except Exception:  # noqa: BLE001 - a timer job, not a user action
        _LOGGER.exception("Scheduled comparison failed for %s", entry.title)
        return
    if isinstance(result, str):
        # No cell to rank, which is an answer rather than a fault: the entry's
        # contract is the only one of its kind sold where it lives.
        _LOGGER.debug("Scheduled comparison skipped for %s: %s", entry.title, result)
        return
    coord.daily_compare = result
    coord.async_update_listeners()
    # Persist now rather than waiting for the next hourly tick to carry it.
    # The sweep runs once a day, so a restart inside that window would lose a
    # ranking that had just cost a couple of minutes of fetching to build.
    # Failing to write must not undo the publish above: the ranking is live in
    # this session either way, and the next tick saves it again.
    try:
        await coord._save_persistent()
    except Exception:  # noqa: BLE001 - a timer job, not a user action
        _LOGGER.exception("Could not persist the ranking for %s", entry.title)


class _SweepStepsMixin(_CompareStepsMixin):
    """The ranking page: every same-group contract in the region, sorted.

    Subclasses ``_CompareStepsMixin`` rather than sitting beside it: the
    sweep genuinely reuses its household resolution and its live-validated key
    prompt, and inheriting says so where a sibling mixin would only work
    because both happen to be mixed into the same flow.

    Its MENU ENTRY is separate, because the two answer different questions. The one-to-one page explains a single pair
    and has room to say why it crosses a kind boundary or quotes a different
    meter; a ranked table has neither, so its candidates are narrower and its
    output is one block of rows.

    The sweep is budgeted rather than timed out. A PDF parse runs in a worker
    thread and ``asyncio.wait_for`` cancels the await, not the thread, so
    nothing here can cut one short; the clock is checked BETWEEN candidates,
    which is the only place stopping is honest.
    """

    _sweep: dict[str, Any]
    _sweep_task: asyncio.Task[Any] | None = None

    async def async_step_compare_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve the cell, then hand off to the sweep."""
        sweep = self._engine.build_sweep()
        if isinstance(sweep, str):
            return self.async_abort(reason=sweep)
        self._sweep = sweep
        # An entry that ranks on a schedule already has the answer, so show it
        # instead of making the reader watch the same two minutes again. This
        # is the whole point of the daily option: the wait disappears rather
        # than moving. The table says when it ran and offers to price again.
        stored = getattr(
            getattr(self.config_entry, "runtime_data", None), "daily_compare", None
        )
        if stored is not None and stored.rows:
            self._sweep["rows"] = list(stored.rows)
            self._sweep["ran_at"] = stored.ran_at
            return await self.async_step_compare_all_result()
        self._sweep_task = None
        if not hasattr(self, "_compare"):
            self._compare = {}
        return await self._sweep_start()

    async def _sweep_start(self) -> ConfigFlowResult:
        """Collect the ENTSO-E key once for the whole sweep, then begin.

        Once, not per row: on the injection regime about eight in ten static
        contracts carry a spot-indexed feed-in formula, so a per-target prompt
        would interrupt the sweep at nearly every row. The one-to-one page
        already owns the prompt and its live validation; this borrows both.
        """
        current = self.config_entry.data
        candidates = self._sweep["candidates"]
        needs_key = _effective_regime(current, {}) == SOLAR_REGIME_INJECTION and any(
            _contract_has_spot_injection(supplier, contract)
            for supplier, contract, _q in candidates
        )
        needs_key = needs_key or any(
            _contract_kind(supplier, contract, quarter_hourly=q)
            in SPOT_PRICED_CONTRACT_KINDS
            for supplier, contract, q in candidates
        )
        if needs_key and not current.get(CONF_API_KEY):
            self._api_key_next_step = self.async_step_compare_all_progress
            return await self.async_step_compare_api_key()
        return await self.async_step_compare_all_progress()

    async def _ensure_household(self) -> str | None:
        """Resolve the household half once, for whichever page needs it first.

        Returns an abort reason, or None when the household is in hand.

        The progress step used to be the only way in, so it owned this. A
        ranking served from the schedule skips that step entirely, which left
        the year-to-date pass as the first thing to read a household nobody
        had resolved: a KeyError on the one page whose whole job is to be
        slow but correct.

        Resolved once for the whole sweep whoever asks. This is the half that
        makes a ranking affordable: the meter reads, the recorder walk and the
        day-ahead window are O(1) in the number of rows, and asking every
        candidate up front is what lets the key be collected once.
        """
        sweep = self._sweep
        if "household" in sweep:
            return None
        from .coordinator import BePricesCoordinator

        coord = getattr(self.config_entry, "runtime_data", None)
        if not isinstance(coord, BePricesCoordinator):
            return "compare_all_entry_reloading"
        sweep["household"] = await self._engine._resolve_household(
            coord,
            candidates=sweep["candidates"],
            meter=self.config_entry.data.get(CONF_METER, METER_MONO),
        )
        return None

    async def async_step_compare_all_progress(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Price one candidate per task, re-showing progress between them.

        One task per candidate rather than one for the whole sweep, because
        Home Assistant only re-renders a progress step when the step returns a
        new result, and a step only returns when its task finishes. A single
        task spanning the sweep could never move the counter.
        """
        sweep = self._sweep
        if "household" not in sweep:
            reason = await self._ensure_household()
            if reason is not None:
                return self.async_abort(reason=reason)
            own = await self._engine._sweep_own_row(sweep["household"])
            if own is not None:
                # Placed before the first candidate so the table has a
                # baseline from the very first render: every other row's gap
                # is measured against it, and a ranking that never shows the
                # household where it currently sits cannot answer "should I
                # switch" at all.
                sweep["rows"].append(own)
        if self._sweep_task is not None:
            if not self._sweep_task.done():
                # Re-show the SAME task. Creating a second one here is the
                # classic duplicate-task bug: the flow manager re-enters this
                # step on every frontend poll.
                return self._sweep_progress()
            task, self._sweep_task = self._sweep_task, None
            try:
                sweep["rows"].append(task.result())
            except Exception as err:  # noqa: BLE001 - one row, not the sweep
                # A row that raised is still a row: dropping it would read as
                # "not competitive". Recorded with its reason and moved past.
                supplier, contract, quarter_hourly = sweep["candidates"][sweep["index"]]
                sweep["rows"].append(
                    RankedRow(
                        label=_candidate_label(supplier, contract, quarter_hourly),
                        annual=None,
                        status=f"could not be priced: {err}",
                    )
                )
            sweep["index"] += 1

        nxt = self._sweep_next_index()
        if nxt is None:
            return self.async_show_progress_done(next_step_id="compare_all_result")
        # Anything skipped on the way here could not fit and is left pending.
        sweep["index"] = nxt
        supplier, contract, quarter_hourly = sweep["candidates"][sweep["index"]]
        self._sweep_task = self.hass.async_create_task(
            self._engine._sweep_one(sweep, supplier, contract, quarter_hourly),
            f"be_electricity_prices sweep {supplier}/{contract}",
            # Not eagerly: an eager start runs the coroutine up to its first
            # await inside the HTTP request the frontend is still waiting on.
            eager_start=False,
        )
        return self._sweep_progress()

    def _sweep_remaining_s(self) -> float:
        """Seconds of budget left, starting the clock on first call."""
        started = self._sweep.get("started_at")
        if started is None:
            self._sweep["started_at"] = dt_util.utcnow()
            return COMPARE_SWEEP_BUDGET_S
        elapsed: float = (dt_util.utcnow() - started).total_seconds()
        return COMPARE_SWEEP_BUDGET_S - elapsed

    def _sweep_next_index(self) -> int | None:
        """The next candidate that FITS in what is left, or None to stop.

        Asking only whether the budget is already spent is not enough, and
        this is what made the page look hung. Cheapest-first puts the
        expensive cards last, so the sweep would reach 110 s of a 120 s budget
        and then start a 45 s Bolt card because the budget was not YET spent -
        overrunning by most of a minute with the counter frozen on one row,
        which from the outside is indistinguishable from stuck. Measured on
        Wallonia: rows 1-37 cost 110 s together, then five TotalEnergies cards
        at 12,8 s and six Bolt at 45,3 s.

        So a candidate that cannot fit is skipped rather than started, and the
        sweep keeps taking cheaper ones behind it. Skipped rows are reported
        as still pending, exactly like the ones never reached.

        The FIRST candidate always runs whatever it costs: a household whose
        whole cell is expensive should get a row, not an empty page.
        """
        sweep = self._sweep
        remaining = self._sweep_remaining_s()
        for index in range(sweep["index"], len(sweep["candidates"])):
            supplier, _contract, _quarter_hourly = sweep["candidates"][index]
            if not sweep["rows"]:
                return index
            if get_extractor(supplier).sweep_cost_s <= remaining:
                return index
        return None

    def _sweep_progress(self) -> ConfigFlowResult:
        """Re-show the running sweep, with the table so far.

        The counter alone is what made this feel hung: cheapest-first means
        the tail is the expensive cards, so it races to the high thirties and
        then sits on one row for up to 45 s with nothing moving. The rows are
        already priced by then, so show them - the table fills and reorders as
        each card lands, and a stall reads as one slow supplier rather than as
        a broken dialog.

        Home Assistant re-renders a progress step when its placeholders
        change, and they change here because each candidate is its own task;
        that is the whole reason the sweep is built one task per row.
        """
        sweep = self._sweep
        # Counted over CANDIDATES only. The household's own row rides in the
        # same list so it can be ranked and compared against, but it was never
        # fetched and counting it would report one more than the sweep did.
        priced = sum(1 for r in sweep["rows"] if r.annual is not None and not r.is_own)
        return self.async_show_progress(
            step_id="compare_all_progress",
            progress_action="sweeping",
            description_placeholders={
                "total": str(len(sweep["candidates"])),
                "priced": str(priced),
                # Deferred is not reported mid-sweep: rows behind the current
                # one are still candidates, and printing a pending count that
                # only grows reads as failure rather than as progress.
                "ranking": _ranking_table(sweep["rows"]),
            },
            progress_task=self._sweep_task,
        )

    async def async_step_compare_all_result(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Render the ranked table; submitting closes the dialog.

        The year-to-date column is offered here rather than computed with the
        annual figures, because it is a different order of cost: every
        archived month is another fetch and parse PER CANDIDATE, and inline it
        would spend the whole budget on history and drop candidate rows. A
        ranking over an incomplete candidate set is wrong rather than
        unfinished, so the annual table is completed first and history is a
        second, deliberate pass.
        """
        if user_input is not None:
            if user_input.get(_REFRESH_FIELD):
                # Drop the stored answer and sweep live. The cards themselves
                # are still cached and probe-gated underneath, so asking again
                # an hour later re-prices rather than re-downloads. The
                # household goes too: the progress step appends the own row
                # only when it resolves the household itself, and a
                # year-to-date pass run on the stored ranking had already
                # resolved it, so a refresh after that pass ranked the
                # alternatives against no baseline at all. Resolving it again
                # is what a live sweep does anyway, and it re-offers the
                # year-to-date box on the rows just priced.
                self._sweep["rows"] = []
                self._sweep["index"] = 0
                self._sweep.pop("ran_at", None)
                self._sweep.pop("household", None)
                self._sweep.pop("ytd_done", None)
                return await self._sweep_start()
            if user_input.get(_YTD_FIELD):
                return await self.async_step_compare_all_ytd()
            return self.async_abort(reason="compare_done")
        sweep = self._sweep
        ran_at = sweep.get("ran_at")
        attempted = sum(1 for r in sweep["rows"] if not r.is_own)
        deferred = len(sweep["candidates"]) - attempted
        schema: dict[Any, Any] = {}
        if ran_at is not None:
            schema[vol.Optional(_REFRESH_FIELD, default=False)] = bool
        if not sweep.get("ytd_done") and sweep["rows"]:
            schema[vol.Optional(_YTD_FIELD, default=False)] = bool
        return self.async_show_form(
            step_id="compare_all_result",
            data_schema=vol.Schema(schema),
            description_placeholders={
                "region": sweep["region"],
                "group": sweep["group"],
                "ranking": _ranking_table(
                    sweep["rows"],
                    # A stored ranking priced the whole cell, so nothing is
                    # pending; today's cell can differ from the night's if a
                    # supplier came or went, which is not a deferred row.
                    deferred=0 if ran_at is not None else max(deferred, 0),
                    ran_at=ran_at,
                ),
            },
            last_step=True,
        )

    async def async_step_compare_all_ytd(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fill the year-to-date column, then show the table.

        The pass itself lives on the engine, because the scheduled sweep runs
        the same one: see ``_SweepEngine.fill_ytd_column``.
        """
        from .coordinator import BePricesCoordinator

        reason = await self._ensure_household()
        if reason is not None:
            return self.async_abort(reason=reason)
        # The same coordinator _ensure_household just resolved, for its spot
        # cache: the pass credits a spot-indexed feed-in off it.
        coord = getattr(self.config_entry, "runtime_data", None)
        if not isinstance(coord, BePricesCoordinator):
            return self.async_abort(reason="compare_all_entry_reloading")
        self._sweep["rows"] = await self._engine.fill_ytd_column(self._sweep, coord)
        self._sweep["ytd_done"] = True
        return await self.async_step_compare_all_result()
