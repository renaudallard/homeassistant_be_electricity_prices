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

"""The pricing engine behind both comparison pages.

Loops over candidate contracts and bills each one against the household the
dialog resolved, whether a user is watching the progress bar or a schedule is
running it at night. Ranking only: the figures come from the shared pricing
code, and how they are shown is somebody else's job.
"""

from __future__ import annotations

from .compare_household import _HouseholdMixin
from .compare_inputs import (
    _HouseholdQuote,
    _candidate_label,
    _coordinator_rlp_index_weights,
    _coordinator_rlp_weights,
    _coordinator_spp_weights,
    _needs_missing_spots,
)
from .compare_table import DailyCompare, RankedRow
from .compare_quote import _annual_bill, _annual_welcome_credit
from .compare_weighting import _compare_injection_credit, _tou_weighted_per_kwh
from .const import (
    CONF_CONTRACT,
    CONF_METER,
    CONF_REGION,
    CONF_SUPPLIER,
    DOMAIN,
    METER_MONO,
    SPOT_PRICED_CONTRACT_KINDS,
)
from .energy_meters import memoise_meter_reads
from .flow_contracts import (
    _contract_group,
    _contract_is_professional,
    _contract_kind,
    _sweep_candidates,
)
from .providers import get as get_extractor, settlement_answer
from .providers._pdf import memoise_text_fetches
from dataclasses import replace
from datetime import date
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from typing import Any
import contextlib
import logging

_LOGGER = logging.getLogger(__name__)


class _SweepEngine(_HouseholdMixin):
    """The pricing engine behind both comparison pages.

    Holds only what pricing needs: the household's entry, the hass it reads
    its meters and recorder through, and the what-if overrides the dialog
    collects, so the same code prices a sweep the user is watching and one
    running on a schedule with nobody watching. It is deliberately not a flow:
    a scheduled sweep has no steps, no progress and no abort, and reaching
    into ``OptionsFlow`` for ``config_entry`` would tie a background job to
    flow-manager internals that move between releases.

    ``overrides`` is the dialog's ``_compare`` dict, shared by reference so a
    what-if picked on one step is seen by the pricing on the next. A scheduled
    sweep passes an empty one: there is no user to ask, so the entry's own
    settings are the only answer.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        overrides: dict[str, Any],
    ) -> None:
        self.hass = hass
        self.config_entry = config_entry
        self._compare = overrides

    async def fill_ytd_column(
        self, sweep: dict[str, Any], coord: Any
    ) -> list[RankedRow]:
        """Fill the year-to-date column, for rows that can honestly carry one.

        A row prints a figure only when it replayed the SAME real archived
        months the household's own side did. ``fetch_for_month is not None``
        is a property of the supplier and not of the contract: seventeen
        candidates pass it and then have no month-addressable card, and the
        year-to-date walk quietly substitutes the current one for every past
        month. That prints 8,5% to 23,3% high next to a real figure, in a
        column the user can sort, with nothing to tell the two apart.

        A candidate is abandoned at the first month it cannot supply, so a
        contract with no archive costs one month rather than eight.

        On the engine rather than in the dialog step because the scheduled
        sweep runs it too: the column was reachable only by clicking through
        the options page, and nothing stored the result, so the figure existed
        for as long as that page was open and nowhere afterwards.

        ``coord`` supplies the historical spot cache. The archive engine
        credits a spot-indexed feed-in per hour off it, so a pass without it
        prices those rows with NO credit at all rather than a slightly wrong
        one, and that includes the household's own row, which is where it
        stops agreeing with the current_year_cost sensor next to it.

        Returns the rows; never raises for one row's sake, and every row it
        cannot price honestly comes back exactly as it went in.
        """
        # One meter read for the whole pass. The household's metered kWh is
        # the same for every candidate: it depends on the entry and the
        # window, never on the supplier being priced, but
        # _compute_current_year_cost reads it afresh on every call, so the
        # pass was making N+1 identical recorder queries over the whole year,
        # nightly and unattended on hardware that is often a Pi.
        with memoise_meter_reads({}):
            return await self._fill_ytd_column(sweep, coord)

    async def _fill_ytd_column(
        self, sweep: dict[str, Any], coord: Any
    ) -> list[RankedRow]:
        """The pass itself; see :meth:`fill_ytd_column`, which memoises it."""
        from .contract_periods import current_period_start, with_previous_contracts
        from .snapshot_months import (
            _snapshot_for_month,
            archived_months_present,
        )
        from .ytd_cost import _compute_current_year_cost

        hh = sweep["household"]
        current = self.config_entry.data
        today = hh.today_local
        # The months the walk bills: from the entry's own year-to-date window,
        # not from January. A household billing from its contract start date
        # walks fewer months, and measuring coverage over January onwards
        # gave every candidate a month the baseline could never have, so the
        # column stayed empty for exactly the households the option exists
        # for.
        months = [
            date(today.year, m, 1) for m in range(hh.ytd_from.month, today.month + 1)
        ]
        # The household's own side sets the standard, so it is walked first -
        # again, before anything asks about coverage. Its own snapshot is the
        # fallback the walk needs, and _compute_current_year_cost is what
        # fills every month of the cache read below.
        session = async_get_clientsession(self.hass)
        # The spots the archive engine credits a per-hour feed-in off. Read
        # rather than fetched: the coordinator already maintains a year of
        # them for any entry whose own pricing needs them, they are the
        # Belgian day-ahead and so supplier-independent, and a year-long
        # ENTSO-E fetch does not belong in a timer job. A household that
        # needs none leaves this empty, and the gate below is what keeps
        # that from printing an uncredited figure.
        hist_spots = dict(getattr(coord, "_historical_spots", {}) or {})
        hist_quarters = dict(getattr(coord, "_historical_spot_quarters", {}) or {})
        own_ytd: float | None = None
        own_start = current_period_start(current, hh.ytd_from)
        # Judged on the same raw card the walk below is handed, so the guard
        # and the thing it guards agree about whether a spot is needed: the
        # cohort splice can turn a spot-monthly leg into a variable one, which
        # answers "no spots needed" for a walk that still needs them.
        if hh.raw_snapshot is not None and not _needs_missing_spots(
            hh.raw_snapshot, hh.quote_entry, hist_spots
        ):
            with contextlib.suppress(Exception):
                own_ytd = await _compute_current_year_cost(
                    self.hass,
                    session,
                    get_extractor(current[CONF_SUPPLIER]),
                    # The raw card, for the reason the other call site gives:
                    # the cohort splice is not idempotent through the month
                    # walk, and this figure sits beside the sensor's.
                    hh.raw_snapshot,
                    hh.quote_entry,
                    historical_spots=hist_spots,
                    spot_quarters=hist_quarters,
                    billed_peak_kw=hh.peak_kw,
                    rlp_weights=_coordinator_rlp_weights(self.config_entry),
                    # The entry's own blend, so this says the same thing as the
                    # default it would fall back to. Passed anyway, because the
                    # rule every call site is held to is what stops the next one
                    # from pricing a foreign card on the household's index.
                    rlp_index_weights=_coordinator_rlp_index_weights(
                        self.config_entry, hh.current_snapshot
                    ),
                    spp_weights=_coordinator_spp_weights(
                        self.config_entry, hh.current_snapshot, own=True
                    ),
                    # From the day the entry's contract started when it recorded
                    # a switch; the contracts before it are added below.
                    window_start_override=(
                        own_start if own_start != hh.ytd_from else None
                    ),
                )
                own_ytd = await with_previous_contracts(
                    self.hass,
                    session,
                    coord,
                    self.config_entry,
                    hh.quote_entry,
                    own_ytd,
                    window_start=hh.ytd_from,
                    today=today,
                )
        # The own contract's cards only from the month it started in: the walk
        # above starts there after a recorded switch, so the months before it
        # were never fetched for this contract. They are the earlier
        # contracts', priced on their own cards, and a candidate replaying the
        # year from January is asked to hold a real card for every one of
        # them. Held to the own contract's months alone, no candidate could
        # ever match and a switch emptied the whole column.
        own_first = own_start.replace(day=1)
        baseline = archived_months_present(
            self.hass,
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            sweep["region"],
            [month for month in months if month >= own_first],
        )
        if baseline:
            baseline |= {
                (month.year, month.month) for month in months if month < own_first
            }
        cached = _sweep_rows(self.hass, self.config_entry.entry_id, sweep["region"])
        rows: list[RankedRow] = []
        for row in sweep["rows"]:
            if row.is_own:
                # The own row is the column's whole point: every other figure
                # is only readable against it. It never carried one, because
                # the candidate list drops the household's own contract by
                # design ("a ranking is a list of alternatives"), so its label
                # is absent from the label map and the lookup below skipped
                # it. The number was being computed right above and discarded.
                #
                # No coverage gate on this one: the baseline IS its coverage,
                # so there is nothing for it to disagree with.
                rows.append(row if own_ytd is None else replace(row, ytd=own_ytd))
                continue
            pair = sweep["labels"].get(row.label)
            # Keyed by the CARD, not by the settlement: the two readings of one
            # card share a fetch, so splatting the whole candidate here builds a
            # four-element key that matches nothing and silently skipped every
            # row of the year-to-date pass.
            held = (
                cached.get((sweep["region"], pair[0], pair[1]))
                if pair is not None
                else None
            )
            snap = held[0] if held is not None else None
            if row.annual is None or pair is None or snap is None or not baseline:
                rows.append(row)
                continue
            supplier, contract, quarter_hourly = pair
            meter, _dso_mode, target_entry, resolved = self._target_side(
                hh, supplier, contract, quarter_hourly, snap
            )
            # Spot-priced kinds are excluded for the same reason the
            # one-to-one page excludes them: the archive engine bills each
            # past hour at factor*spot+base and needs a historical spot for
            # every hour of the year, which the cache below is not required
            # to hold. Called without it the energy leg silently vanishes,
            # measured 33,7% low on a dynamic card: in a column the table
            # sorts.
            if (
                _contract_kind(supplier, contract, quarter_hourly=quarter_hourly)
                in SPOT_PRICED_CONTRACT_KINDS
            ):
                rows.append(row)
                continue
            # A static card can still carry a spot-indexed FEED-IN, which the
            # kind above does not describe: the energy is fixed and only the
            # injection is indexed. Without spots that credit is not
            # approximated, it is dropped whole, so the row would print a
            # solar household's bill with no solar in it.
            if _needs_missing_spots(resolved, target_entry, hist_spots):
                rows.append(row)
                continue
            # The window's first month first, and BEFORE asking about
            # coverage. The coverage cache is only ever written by this walk,
            # so checking it up front answers "nothing is covered" for every
            # candidate and the whole pass becomes a no-op that hides its own
            # checkbox. One month is also the cheap reject: a contract with no
            # month-addressable card costs one fetch here rather than a full
            # year of them.
            try:
                await _snapshot_for_month(
                    self.hass,
                    session,
                    get_extractor(supplier),
                    contract,
                    sweep["region"],
                    months[0],
                    resolved,
                    target_entry,
                )
            except Exception:  # noqa: BLE001 - one row loses its history
                rows.append(row)
                continue
            if not archived_months_present(
                self.hass, supplier, contract, sweep["region"], months[:1]
            ):
                rows.append(row)
                continue
            try:
                value = await _compute_current_year_cost(
                    self.hass,
                    session,
                    get_extractor(supplier),
                    resolved,
                    target_entry,
                    contract_override=contract,
                    meter_override=meter,
                    historical_spots=hist_spots,
                    spot_quarters=hist_quarters,
                    billed_peak_kw=hh.peak_kw,
                    rlp_weights=_coordinator_rlp_weights(self.config_entry),
                    rlp_index_weights=_coordinator_rlp_index_weights(
                        self.config_entry, snap
                    ),
                    spp_weights=_coordinator_spp_weights(
                        self.config_entry, snap, own=False
                    ),
                    # The own row's days, which after a recorded switch the
                    # entry's settings no longer give.
                    window_start_override=hh.ytd_from,
                )
            except Exception:  # noqa: BLE001 - one row loses its history
                rows.append(row)
                continue
            # Coverage is judged AFTER the walk, which is what filled the
            # cache. Equal to the baseline's, not merely non-empty: a row
            # replaying nine of the baseline's twelve months is not a smaller
            # figure in the same column, it is a different question answered
            # in it, and the walk quietly proxies the current card for the
            # months it could not fetch.
            covered = archived_months_present(
                self.hass, supplier, contract, sweep["region"], months
            )
            if covered != baseline:
                rows.append(row)
                continue
            rows.append(replace(row, ytd=value))
        return rows

    async def run_full_sweep(self, coord: Any) -> DailyCompare | str:
        """Price the whole cell with nobody watching, or say why not.

        No budget and no skipping, unlike the dialog. The wall-clock budget
        exists because somebody is staring at a progress bar; a scheduled run
        has all night, and stopping early would publish a ranking whose
        cheapest row is merely the cheapest one that fitted.

        Sequential rather than gathered, deliberately. Sixteen suppliers
        fetched at once is a burst on sixteen servers to save a couple of
        minutes nobody is waiting through, and the listing memo below only
        pays off when candidates sharing a listing page run one after another.
        """
        sweep = self.build_sweep()
        if isinstance(sweep, str):
            return sweep
        sweep["household"] = await self._resolve_household(
            coord,
            candidates=sweep["candidates"],
            meter=self.config_entry.data.get(CONF_METER, METER_MONO),
        )
        rows: list[RankedRow] = []
        own = await self._sweep_own_row(sweep["household"])
        if own is not None:
            rows.append(own)
        for supplier, contract, quarter_hourly in sweep["candidates"]:
            try:
                rows.append(
                    await self._sweep_one(sweep, supplier, contract, quarter_hourly)
                )
            except Exception as err:  # noqa: BLE001 - one row, not the sweep
                # Same rule as the dialog: a row that raised is still a row,
                # because dropping it would read as "not competitive".
                rows.append(
                    RankedRow(
                        label=_candidate_label(supplier, contract, quarter_hourly),
                        annual=None,
                        status=f"could not be priced: {err}",
                    )
                )
        # The year-to-date column, which used to be reachable only by clicking
        # through the options page and was stored nowhere afterwards. A
        # scheduled run is the right place for it: the pass replays a year per
        # candidate, and this is the run with all night and no progress bar.
        # Wrapped because it is an extra, not the ranking: a failure here must
        # leave the annual figures standing rather than cost the whole sweep.
        sweep["rows"] = rows
        try:
            rows = await self.fill_ytd_column(sweep, coord)
        except Exception:  # noqa: BLE001 - the annual ranking still stands
            _LOGGER.exception(
                "Year-to-date column failed for %s; publishing annual only",
                self.config_entry.title,
            )
        return DailyCompare(
            rows=tuple(rows),
            own=own.annual if own is not None else None,
            priced=sum(1 for r in rows if r.annual is not None and not r.is_own),
            total=len(sweep["candidates"]),
            ran_at=dt_util.utcnow(),
        )

    def build_sweep(self) -> dict[str, Any] | str:
        """The sweep state for this entry's cell, or why there is none.

        Returns the reason string rather than raising, because the dialog
        turns it into an abort and the scheduled run into a log line, and the
        two disagree about what a missing cell means to the user.
        """
        current = self.config_entry.data
        region = current[CONF_REGION]
        # Through the household's own settlement, not the registered kind: a
        # Bolt variable card is a static contract settled monthly and a spot
        # one settled per quarter-hour, and the ranking only ranks within one
        # group. Read off the registry alone it put a quarter-hourly household
        # in the static cell, measuring their bill against 52 monthly
        # contracts and none of the dynamic ones they could actually move to.
        group = _contract_group(
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            quarter_hourly=settlement_answer(current),
        )
        if not group:
            # The entry's contract has left the catalogue, so there is no
            # group to rank it within. Distinct from an empty cell: nothing is
            # missing from the market, we just cannot place this household.
            return "compare_all_unknown_contract"

        candidates = _sweep_candidates(
            region,
            group,
            _contract_is_professional(current[CONF_SUPPLIER], current[CONF_CONTRACT]),
            current[CONF_CONTRACT],
        )
        if not candidates:
            # A real answer, not a failure: a Brussels time-of-use household
            # has exactly one slot contract in the region and it is theirs.
            # Saying so is more use than an empty table.
            return "compare_all_no_alternatives"

        # Cheapest card first, so a budget buys many rows before few. Ties
        # broken on the label so the order is stable between opens and the
        # table does not reshuffle when a user reopens to finish it.
        candidates.sort(
            key=lambda cand: (
                get_extractor(cand[0]).sweep_cost_s,
                cand[0],
                cand[1].id,
                cand[2],
            )
        )
        return {
            "region": region,
            "group": group,
            "candidates": [(supplier, c.id, q) for supplier, c, q in candidates],
            "index": 0,
            "rows": [],
            # One listing memo for the whole sweep; see _sweep_one.
            "listings": {},
            # A row carries only its rendered label, so the year-to-date pass
            # needs a way back to the contract that produced it.
            "labels": {
                _candidate_label(supplier, c.id, q): (supplier, c.id, q)
                for supplier, c, q in candidates
            },
        }

    async def _sweep_own_row(self, hh: _HouseholdQuote) -> RankedRow | None:
        """The household's own contract, priced from the card it already has.

        Not a candidate and not re-fetched. ``_sweep_candidates`` drops it
        because a ranking is a list of alternatives, but the row still belongs
        in the table: it is what every gap is measured against, and it carries
        the signing-rate and cohort splice the household is actually billed
        on, which re-fetching it as though it were a stranger's card would
        silently discard.

        Returns None when the entry has no usable snapshot yet - a cold start
        - in which case the table ranks the alternatives against each other
        and simply has no "yours" row to point at.
        """
        current = self.config_entry.data
        if hh.current_snapshot is None or hh.current_per_kwh is None:
            return None
        # Named the way the candidates are, settlement included: on a card
        # sold both ways the alternatives beside it carry the marker, and a
        # bare name would read as the monthly settlement while the row below
        # it prices the same card per quarter-hour. Its label is never looked
        # up in the sweep's map: the own row is handled before that, so
        # sharing the helper costs nothing but keeps the column readable.
        label = _candidate_label(
            current[CONF_SUPPLIER],
            current[CONF_CONTRACT],
            settlement_answer(current),
        )
        try:
            annual = _annual_bill(
                hh.current_snapshot,
                hh.quote_entry,
                hh.peak_kw,
                hh.current_per_kwh,
                hh.annual_kwh,
                hh.rolling_inj_kwh,
                _compare_injection_credit(
                    hh.current_snapshot,
                    hh.quote_entry,
                    hh.spot_dict,
                    hh.avg_spot,
                    await hh.credit_month_spot_for(
                        hh.current_snapshot, own=True, raw=hh.raw_snapshot
                    ),
                    hh.inj_hour_weights,
                    raw_snapshot=hh.raw_snapshot,
                    credit_year=hh.credit_year,
                ),
                export_per_kwh=hh.current_export_per_kwh,
                register_weights=hh.register_weights,
                meter=hh.current_meter,
                welcome_credit_eur=hh.own_welcome_credit,
            )
        except Exception:  # noqa: BLE001 - the alternatives are still useful
            return None
        return RankedRow(label=label, annual=annual, is_own=True)

    async def _sweep_one(
        self,
        sweep: dict[str, Any],
        supplier: str,
        contract: str,
        quarter_hourly: bool = False,
    ) -> RankedRow:
        """Fetch and price one candidate on the settlement it was listed for.

        The sweep state is passed rather than held, so the same engine can
        price a dialog's sweep and a scheduled one without either owning it.

        A card sold on both settlements arrives here twice. The fetch is
        shared (the row cache is keyed by the card, not by the settlement) and
        only the pricing differs, so the second row costs a dict lookup.
        """
        from .snapshot_store import fetch_shared

        label = _candidate_label(supplier, contract, quarter_hourly)
        region = sweep["region"]
        cached = _sweep_rows(self.hass, self.config_entry.entry_id, region)
        held = cached.get((region, supplier, contract))
        snap, read_by_ocr = held if held is not None else (None, False)
        if snap is None:
            # Share one listing memo across every candidate in this sweep.
            # Nine providers resolve a per-supplier listing page inside
            # fetch() and pick one product out of it, so a Flanders static
            # sweep would otherwise pull Mega's listing nine times, Engie's
            # eight and Luminus's eight - about 3 MB and 25 round trips that
            # buy nothing, spent out of a wall-clock budget that is measured
            # in rows.
            with memoise_text_fetches(sweep["listings"]):
                fetched = await fetch_shared(
                    self.hass,
                    async_get_clientsession(self.hass),
                    get_extractor(supplier),
                    contract,
                    region,
                    supplier=supplier,
                    # The sweep DOES adopt a cached card, unlike the one-off quote
                    # above: it is pricing fifty rows against a wall-clock budget,
                    # and re-downloading a card a sibling already holds is the
                    # whole cost it is trying to avoid. Still read-only, so still
                    # no negative-cache write.
                    record_failure=False,
                )
            if fetched.row is None:
                # A card published as page images leaves a parser nothing to
                # work with, and the live tick answers that by serving the
                # archive's OCR reading. This page has to do the same or the
                # supplier shows up as an error on the one screen that says
                # whether to switch to it: Ecofix's four contracts read
                # "card has no text layer" where every rival showed a price.
                archived = await self._ocr_fallback(supplier, contract, region, fetched)
                if archived is None:
                    return RankedRow(
                        label=label,
                        annual=None,
                        status=fetched.error_message or "supplier unreachable",
                    )
                snap = archived.snapshot
                read_by_ocr = archived.read_by_ocr
            else:
                snap = fetched.row.snapshot
                read_by_ocr = False
            cached[(region, supplier, contract)] = (snap, read_by_ocr)

        hh = sweep["household"]
        meter, dso_mode, target_entry, resolved = self._target_side(
            hh, supplier, contract, quarter_hourly, snap
        )
        if hh.dso not in resolved.dsos:
            return RankedRow(
                label=label, annual=None, status=f"does not serve DSO {hh.dso}"
            )
        per_kwh = _tou_weighted_per_kwh(
            resolved,
            hh.dso,
            region,
            dt_util.as_local(hh.now_utc),
            await hh.spot_for(resolved),
            meter,
            dso_mode,
            hour_weights=hh.hour_weights,
        )
        if per_kwh is None:
            return RankedRow(label=label, annual=None, status="could not be priced")
        # WELCOME CREDIT: what a customer signing this card today is granted
        # over the coming year, off the card as it prints today. The own row
        # carries what is left of its own first year, so a tier that exists
        # for its cashback (Frank's Korting) ranks on the year it would cost,
        # not on the year it would cost someone the credit was never offered.
        welcome_credit = _annual_welcome_credit(
            resolved,
            resolved,
            hh.today_local,
            dt_util.as_local(hh.now_utc),
            hh.dso,
            region,
            await hh.spot_for(resolved),
            meter,
            dso_mode,
            hh.hour_weights,
            hh.annual_kwh,
            hh.rolling_inj_kwh,
            regime=hh.regime,
        )
        annual = _annual_bill(
            resolved,
            target_entry,
            hh.peak_kw,
            per_kwh,
            hh.annual_kwh,
            hh.rolling_inj_kwh,
            _compare_injection_credit(
                resolved,
                target_entry,
                hh.spot_dict,
                hh.avg_spot,
                await hh.credit_month_spot_for(resolved, own=False),
                hh.inj_hour_weights,
                meter=meter,
                credit_year=hh.credit_year,
            ),
            # EXPORT RATE: under compensation the bill nets consumption
            # against injection, and each side has to be priced on its own
            # hour-of-day shape or the netting values exported kWh at the
            # hours the household draws them instead of the hours the panels
            # produce. Omitted, a compensation row came out 23% low.
            export_per_kwh=await hh.export_rate_for(resolved, meter, dso_mode),
            register_weights=hh.register_weights,
            meter=meter,
            welcome_credit_eur=welcome_credit,
        )
        return RankedRow(label=label, annual=annual, read_by_ocr=read_by_ocr)


def _sweep_rows(
    hass: HomeAssistant, entry_id: str, region: str
) -> dict[tuple[str, str, str], Any]:
    """Snapshots this entry's sweep has already fetched, for the life of the
    process.

    Keyed by (supplier, contract) and holding the CARD rather than the priced
    row, so reopening after changing a household setting re-prices from what
    was already downloaded instead of re-downloading it. The expensive half of
    a sweep is the fetch and the parse; the arithmetic on top is free.

    The value is ``(card, read_by_ocr)``: a supplier publishing page images is
    priced off the archive's OCR reading, and the row has to say so however
    many times it is re-priced from this cache, so the two travel together
    rather than in a second map that can drift from this one.

    Keyed by region as well as contract. A household that edits its region
    between two opens is asking about a different market with different DSOs,
    and a card fetched for the old one would be re-priced against the new one
    without being re-fetched.

    Deliberately not the shared snapshot cache: that one is keyed by tuple and
    shared between entries, and evicting it is the coordinator's business.
    This is scratch belonging to one dialog. It is dropped when the entry
    unloads (``evict_sweep_rows``), which is the only lifetime it needs: a
    ranking is read in one sitting, and the shared cache underneath it already
    applies the freshness rules.
    """
    bucket: dict[str, Any] = hass.data.setdefault(DOMAIN, {})
    store: dict[str, dict[tuple[str, str, str], Any]] = bucket.setdefault(
        "sweep_rows", {}
    )
    return store.setdefault(entry_id, {})


def evict_sweep_rows(hass: HomeAssistant, entry_id: str) -> None:
    """Drop an entry's ranking scratch when it unloads.

    Without it the cards a sweep fetched outlive the entry that asked for
    them, for the life of the Home Assistant process.
    """
    bucket: dict[str, Any] = hass.data.get(DOMAIN, {})
    store: dict[str, Any] = bucket.get("sweep_rows", {})
    store.pop(entry_id, None)
