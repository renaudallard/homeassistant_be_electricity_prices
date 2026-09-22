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

"""Everything the comparison page renders, assembled once.

The result step is a single description-placeholder dict: the ranking table,
the chart series, the caveats and the per-row breakdown all arrive as one
blob of pre-formatted text. That formatting is long, and none of it decides
anything, so it lives away from the steps that do.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from homeassistant.config_entries import OptionsFlow
from .const import CONF_API_KEY
from .const import CONF_CONTRACT
from .const import CONF_CONTRACT_START_DATE
from .const import CONF_METER
from .const import CONF_SUPPLIER
from .const import DEFAULT_ANNUAL_CONSUMPTION_KWH
from .const import DSO_MODE_IMPACT
from .const import METER_MONO
from .const import SOLAR_REGIME_INJECTION
from .const import SPOT_PRICED_CONTRACT_KINDS
from .compare_quote import _annual_bill
from .compare_quote import _annual_welcome_credit
from .compare_table import _card_caveats
from .compare_weighting import _compare_injection_credit
from .flow_schemas import _contract_kind
from .cohort import _parse_iso_date
from .compare_table import _populate_charts
from .compare_table import _solar_note
from .compare_weighting import _tou_weighted_per_kwh
from .compare_table import _uncredited_note
from .compare_table import _vintage_note
from .compare_table import _whatif_note
from .compare_quote import _ytd_welcome_credit
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from .providers import get as get_extractor
from .compare_engine import _SweepEngine
from .compare_inputs import _borrowed_spot_cache
from .compare_inputs import _coordinator_rlp_index_weights
from .compare_inputs import _coordinator_rlp_weights
from .compare_inputs import _coordinator_spp_weights
from .compare_inputs import _kva
from .compare_inputs import _label_for_contract
from .compare_inputs import _label_for_supplier
from .compare_inputs import _quote_entry
from .compare_inputs import _settlement_of
from collections.abc import Mapping
from typing import Any


class _PlaceholdersMixin(OptionsFlow):
    """Mixed into _CompareStepsMixin."""

    # State the concrete class owns, declared as BARE annotations with no
    # value: a valued class attribute would change hasattr() and instance-dict
    # behaviour. __init__ over there is what actually creates these.
    _compare: dict[str, Any]

    if TYPE_CHECKING:
        # Provided by the concrete class; stubs rather than inheritance,
        # which would be a cycle.
        @property
        def _engine(self) -> _SweepEngine: ...

    async def _build_compare_placeholders(self) -> dict[str, str]:
        """Fetch the picked supplier's snapshot and compute a side-by-side
        annual estimate against the user's current entry.

        Annual = per_kwh_now * the resolved yearly volume + yearly fees, where the
        yearly fees are yearly_fixed_fee + 12 * energy_fund + 12 *
        capacity (Flanders) + 12 * prosumer (Wallonia compensation +
        solar). Errors collapse to ``-`` so the page always renders.
        """

        from .coordinator import BePricesCoordinator

        current = self.config_entry.data
        coord = getattr(self.config_entry, "runtime_data", None)
        # Coordinator may not be a BePricesCoordinator if the entry is
        # mid-reload (UNDEFINED sentinel) or never finished setup. We
        # still need to populate every placeholder the result template
        # references; otherwise HA renders the missing ones as raw
        # ``{token}`` text.
        if not isinstance(coord, BePricesCoordinator):
            return {
                "current_supplier": str(current.get(CONF_SUPPLIER, "")),
                "current_contract": str(current.get(CONF_CONTRACT, "")),
                "compare_supplier": str(self._compare.get(CONF_SUPPLIER, "")),
                "compare_contract": str(self._compare.get(CONF_CONTRACT, "")),
                "current_per_kwh": "-",
                "compare_per_kwh": "-",
                "current_annual": "-",
                "compare_annual": "-",
                "delta_annual": "-",
                "current_ytd": "-",
                "compare_ytd": "-",
                "delta_ytd": "-",
                "annual_kwh": f"{DEFAULT_ANNUAL_CONSUMPTION_KWH:.0f}",
                "ytd_from": "-",
                "ytd_kwh": "-",
                "ytd_injection_kwh": "-",
                "solar_note": "",
                "meter_used": str(
                    self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
                ),
                "consumption_source": "default (entry reloading)",
                "annual_chart": "",
                "ytd_chart": "",
                "card_note": "",
                "error": "current entry is reloading; try again in a moment",
            }

        meter = self._compare.get(CONF_METER, current.get(CONF_METER, METER_MONO))
        hh = await self._engine._resolve_household(
            coord,
            candidates=[
                (
                    self._compare[CONF_SUPPLIER],
                    self._compare[CONF_CONTRACT],
                    _settlement_of(self._compare),
                )
            ],
            meter=meter,
        )
        region = hh.region
        dso = hh.dso
        current_meter = hh.current_meter
        dso_mode = hh.dso_mode
        peak_kw = hh.peak_kw
        stored_regime = hh.stored_regime
        regime = hh.regime
        quote_entry = hh.quote_entry
        overridden = hh.overridden
        now_utc = hh.now_utc
        today_local = hh.today_local
        ytd_from = hh.ytd_from
        fee_proration = hh.fee_proration
        month_proration = hh.month_proration
        spot_dict = hh.spot_dict
        current_kind = hh.current_kind
        avg_spot = hh.avg_spot
        compare_spot_injection = hh.compare_spot_injection
        ytd_kwh = hh.ytd_kwh
        rolling_inj_kwh = hh.rolling_inj_kwh
        ytd_inj_kwh = hh.ytd_inj_kwh
        annual_kwh = hh.annual_kwh
        volumes_typed = hh.volumes_typed
        placeholders = hh.placeholders
        current_snapshot = hh.current_snapshot
        raw_snapshot = hh.raw_snapshot
        baseline_snapshot = hh.baseline_snapshot
        hour_weights = hh.hour_weights
        inj_hour_weights = hh.inj_hour_weights
        current_per_kwh = hh.current_per_kwh
        current_export_per_kwh = hh.current_export_per_kwh
        _spot_for = hh.spot_for
        _credit_month_spot_for = hh.credit_month_spot_for
        _export_rate_for = hh.export_rate_for

        # The target side. Unlike everything above, each of these is a
        # property of the ONE contract being quoted, and a ranking recomputes
        # them per row against the household context resolved once.
        other_kind = _contract_kind(
            self._compare[CONF_SUPPLIER],
            self._compare[CONF_CONTRACT],
            quarter_hourly=_settlement_of(self._compare),
        )
        # A Tarif Impact product is sold only on the CWaPE incitative
        # configuration: its energy carries three band rates and no
        # mono/bi structure at all, so the band schedule prices it whatever
        # the household is on, while the network leg and the Walloon terme
        # fixe both follow the mode. Quoting the TARGET on the household's
        # own mode therefore banded its energy, billed its network off the
        # standard jour/nuit columns and charged it a fixed term the tariff
        # does not have. The install flow forces the mode for exactly this
        # reason; mirror it here, for the target only, the same way the
        # meter override applies to the target only.
        #
        # Gated on the registered kind, which deliberately leaves
        # totalenergies_impact out: it is registered "variable" and its impact
        # bands are read only in impact mode, so a household on the standard
        # configuration quoting it still bills the target's network leg on the
        # jour/nuit columns, worth about EUR 29/yr on a bi meter and EUR 113 on
        # a mono one. Not forced, for the reason _IMPACT_DEFAULT_CONTRACTS
        # gives at flow_schemas.py:514: the TE card states only that a
        # communicating digital meter is required, so a holder on the standard
        # configuration genuinely exists and forcing would under-bill them by
        # the same amount in the other direction. The install flow pre-selects
        # the mode for that card and lets the user say otherwise, which is the
        # decision this flow has no step to ask about.
        other_dso_mode = DSO_MODE_IMPACT if other_kind == "tou_impact" else dso_mode
        target_entry = _quote_entry(
            self.config_entry, regime, other_dso_mode, meter=meter
        )
        other_export_per_kwh: float | None = None

        # Other supplier: fetch + compute.
        session = async_get_clientsession(self.hass)
        other_extractor = get_extractor(self._compare[CONF_SUPPLIER])
        other_per_kwh: float | None = None
        other_welcome_credit = 0.0
        other_snap = None
        # Resolve the quote against this entry's site facts through the same
        # helper the coordinator uses, not apply_vat alone. Both transforms are
        # per-entry, and skipping the excise band priced a professional quote
        # at the card's first tier however much the household actually uses:
        # 1,421 c€/kWh instead of 1,139 at 60 000 kWh/yr, overstating the
        # alternative by about 169 EUR/yr. The user's own side comes off the
        # coordinator and IS resolved, so the comparison was biased.
        from .snapshot_store import fetch_shared
        from .snapshot_resolve import _resolve_snapshot

        # Through the shared policy rather than extractor.fetch directly, but
        # asking for a fresh card: this is one quote the user explicitly asked
        # for, and three compare targets (Engie, Luminus, energie.be) publish
        # no probe, so adopting a cached row would quote them off a card up to
        # a day old where this page always downloaded. The other two wins of
        # going through the policy are kept: the per-key lock, so two dialogs
        # on one tuple do not both download, and the write into the shared
        # cache on success, so the coordinators can adopt what this fetched.
        #
        # record_failure=False because this is a read-only page. A background
        # tick's failure is evidence about the supplier and the negative row
        # exists so siblings back off; a dialog's failure is not, and writing
        # it here cancels a real entry's due download for five minutes and
        # inflates the counter the Repairs card is thresholded on.
        fetched = await fetch_shared(
            self.hass,
            session,
            other_extractor,
            self._compare[CONF_CONTRACT],
            region,
            supplier=self._compare[CONF_SUPPLIER],
            force=True,
            record_failure=False,
        )
        archived = (
            None
            if fetched.row is not None
            else await self._engine._ocr_fallback(
                self._compare[CONF_SUPPLIER],
                self._compare[CONF_CONTRACT],
                region,
                fetched,
            )
        )
        if fetched.row is None and archived is None:
            # Includes the backoff arm, which carries a sibling's recent
            # failure and no exception of its own.
            placeholders["error"] = f"could not fetch quote: {fetched.error_message}"
        else:
            if archived is not None:
                # Page images: the archive's OCR reading is the only price
                # that exists for this month, and the note below says so.
                other_read_by_ocr = archived.read_by_ocr
                fetched_snapshot = archived.snapshot
            else:
                # Not None here: the branch above covers the failed fetch, so
                # one of the two always carries a card.
                assert fetched.row is not None
                other_read_by_ocr = False
                fetched_snapshot = fetched.row.snapshot
            other_snap = _resolve_snapshot(
                _quote_entry(
                    self.config_entry,
                    regime,
                    quarter_hourly=_settlement_of(self._compare),
                    meter=meter,
                ),
                fetched_snapshot,
            )
            if dso not in other_snap.dsos:
                placeholders["error"] = (
                    f"{self._compare[CONF_SUPPLIER]} doesn't serve DSO {dso}"
                )
            else:
                other_per_kwh = _tou_weighted_per_kwh(
                    other_snap,
                    dso,
                    region,
                    dt_util.as_local(now_utc),
                    await _spot_for(other_snap),
                    meter,
                    other_dso_mode,
                    hour_weights,
                )
                other_export_per_kwh = await _export_rate_for(
                    other_snap, meter, other_dso_mode
                )
                if other_per_kwh is None:
                    placeholders["error"] = "compute failed"
                else:
                    # A customer signing this card today, over the coming
                    # year, off the card as it prints today. The own side
                    # carries what is left of its own first year.
                    other_welcome_credit = _annual_welcome_credit(
                        other_snap,
                        other_snap,
                        today_local,
                        dt_util.as_local(now_utc),
                        dso,
                        region,
                        await _spot_for(other_snap),
                        meter,
                        other_dso_mode,
                        hour_weights,
                        annual_kwh,
                        rolling_inj_kwh,
                        regime=regime,
                    )

        # Per-supplier injection price (only used in the "injection"
        # regime; compensation regime nets at the meter, none has
        # nothing to credit). Compute from each snapshot via the
        # coordinator's existing helper, which returns None when the
        # snapshot has no injection data or the user isn't on the
        # injection regime.
        current_inj_price: float | None = None
        compare_inj_price: float | None = None
        # One clause per side that ends up crediting nothing. Both the
        # "no injection tariff on the card" and the "spot-indexed but no
        # spot" cases land on the same silent no-credit branch of
        # _annual_bill, so the page has to name them or it reads as if the
        # printed credit applied to both sides.
        uncredited: list[str] = []
        if regime == SOLAR_REGIME_INJECTION:
            if current_snapshot is not None:
                current_inj_price = _compare_injection_credit(
                    current_snapshot,
                    quote_entry,
                    spot_dict,
                    avg_spot,
                    await _credit_month_spot_for(
                        current_snapshot, own=True, raw=raw_snapshot
                    ),
                    inj_hour_weights,
                    raw_snapshot=raw_snapshot,
                )
                if current_inj_price is None and rolling_inj_kwh > 0:
                    uncredited.append(
                        _uncredited_note(
                            current_snapshot,
                            _label_for_supplier(current[CONF_SUPPLIER]),
                        )
                    )
            if other_snap is not None:
                compare_inj_price = _compare_injection_credit(
                    other_snap,
                    quote_entry,
                    spot_dict,
                    avg_spot,
                    await _credit_month_spot_for(other_snap, own=False),
                    inj_hour_weights,
                    meter=meter,
                )
                if compare_inj_price is None and rolling_inj_kwh > 0:
                    uncredited.append(
                        _uncredited_note(
                            other_snap,
                            _label_for_supplier(self._compare[CONF_SUPPLIER]),
                        )
                    )

        current_annual: float | None = None
        if current_per_kwh is not None:
            current_annual = _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                export_per_kwh=current_export_per_kwh,
                register_weights=hh.register_weights,
                meter=current_meter,
                welcome_credit_eur=hh.own_welcome_credit,
            )
            placeholders["current_per_kwh"] = f"{current_per_kwh:.4f}"
            placeholders["current_annual"] = f"{current_annual:.2f}"
        if other_per_kwh is not None and other_snap is not None:
            placeholders["compare_per_kwh"] = f"{other_per_kwh:.4f}"
            placeholders["compare_annual"] = (
                f"{_annual_bill(other_snap, target_entry, peak_kw, other_per_kwh, annual_kwh, rolling_inj_kwh, compare_inj_price, export_per_kwh=other_export_per_kwh, register_weights=hh.register_weights, meter=meter, welcome_credit_eur=other_welcome_credit):.2f}"
            )

        # A what-if moves BOTH sides together, so the printed supplier delta
        # barely shifts and the interesting number goes missing. Price the
        # user's own contract once more under the entry AS CONFIGURED: that
        # difference is the whole question a what-if is asking.
        #
        # It matters more now that the picker offers the user's own contract:
        # quoting that against itself with a different meter or regime makes
        # the supplier delta zero by construction, and this baseline is the
        # only line on the page that answers what the change is worth.
        baseline_annual: float | None = None
        if overridden and current_per_kwh is not None and baseline_snapshot is not None:
            baseline_inj_price = (
                _compare_injection_credit(
                    baseline_snapshot,
                    self.config_entry,
                    spot_dict,
                    avg_spot,
                    await _credit_month_spot_for(
                        baseline_snapshot, own=True, raw=raw_snapshot
                    ),
                    inj_hour_weights,
                    raw_snapshot=raw_snapshot,
                )
                if stored_regime == SOLAR_REGIME_INJECTION
                else None
            )
            baseline_annual = _annual_bill(
                baseline_snapshot,
                self.config_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                baseline_inj_price,
                export_per_kwh=current_export_per_kwh,
                register_weights=hh.register_weights,
                meter=current_meter,
                # The same first-year share the what-if side carries, on the
                # card as configured: a what-if moves the regime or the meter,
                # never the day the household signed.
                welcome_credit_eur=_annual_welcome_credit(
                    baseline_snapshot,
                    hh.signing_snapshot,
                    _parse_iso_date(current.get(CONF_CONTRACT_START_DATE)),
                    dt_util.as_local(now_utc),
                    dso,
                    region,
                    await _spot_for(baseline_snapshot),
                    current_meter,
                    dso_mode,
                    hour_weights,
                    annual_kwh,
                    rolling_inj_kwh,
                    regime=regime,
                ),
            )
        placeholders["solar_note"] = _whatif_note(
            _solar_note(regime, rolling_inj_kwh, uncredited),
            stored_regime=stored_regime,
            regime=regime,
            baseline_eur=baseline_annual,
            whatif_eur=current_annual,
            volumes_typed=volumes_typed,
            missing_kva=_kva(current) <= 0.0,
        )
        caveats: list[str] = []
        if current_snapshot is not None:
            caveats += _card_caveats(
                current_snapshot, _label_for_supplier(current[CONF_SUPPLIER])
            )
        if other_snap is not None:
            caveats += _card_caveats(
                other_snap,
                _label_for_supplier(self._compare[CONF_SUPPLIER]),
                read_by_ocr=other_read_by_ocr,
            )
        vintage = _vintage_note(
            current_snapshot,
            _label_for_supplier(current[CONF_SUPPLIER]),
            other_snap,
            _label_for_supplier(self._compare[CONF_SUPPLIER]),
        )
        if vintage:
            caveats.append(vintage)
        placeholders["card_note"] = ("Note: " + "; ".join(caveats)) if caveats else ""
        if (
            current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and current_snapshot is not None
        ):
            delta = _annual_bill(
                other_snap,
                target_entry,
                peak_kw,
                other_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                compare_inj_price,
                export_per_kwh=other_export_per_kwh,
                register_weights=hh.register_weights,
                meter=meter,
                welcome_credit_eur=other_welcome_credit,
            ) - _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                annual_kwh,
                rolling_inj_kwh,
                current_inj_price,
                export_per_kwh=current_export_per_kwh,
                register_weights=hh.register_weights,
                meter=current_meter,
                welcome_credit_eur=hh.own_welcome_credit,
            )
            placeholders["delta_annual"] = f"{'+' if delta >= 0 else ''}{delta:.2f}"

        # Both year-to-date paths replay the household's own meter history,
        # which was recorded under the configured regime and whose
        # consumption register may already be netted. Typed volumes are a
        # yearly hypothesis with no history behind them, so the legs stay
        # blank rather than mixing a what-if with a measured past.
        if volumes_typed:
            _populate_charts(
                placeholders,
                current_label=_chart_labels(current, self._compare)[0],
                compare_label=_chart_labels(current, self._compare)[1],
            )
            return placeholders

        # Year-to-date what-if. Two paths:
        #   1. Archive-capable pairs, both suppliers keeping a month archive
        #      (every scraped supplier but Ecofix and TotalEnergies today)
        #      and neither side spot-priced: reuse the coordinator's
        #      _compute_current_year_cost engine against each snapshot
        #      chain, so per-month tariff transitions and the same proration
        #      model the user's actual bill uses apply to both sides. Most
        #      accurate.
        #   2. Everything else (a side without an archive, the custom
        #      supplier, a spot-priced side): fall back to the simple
        #      "current rate * ytd_kwh + pro-rated fees" model. Same per_kwh
        #      and same proration on both sides, so the delta still isolates
        #      the supplier-driven difference.
        from .ytd_cost import _compute_current_year_cost

        current_extractor = get_extractor(current[CONF_SUPPLIER])
        # Exclude spot-priced sides from the archive engine: it bills each
        # past hour at factor*spot+base (or the month's mean) and needs the
        # historical spot cache, which _compute_current_year_cost only
        # receives on the live coordinator path: called without it here it
        # returns the fees-only floor (zero energy), so a fixed-vs-dynamic
        # compare would show the dynamic side missing its entire energy bill.
        # The simple per-kwh model below prices both sides off the same
        # current per-kwh rate and proration, so the delta stays honest.
        # spot_monthly is in that set for the same reason as dynamic, and it
        # is what holds archive_capable False for Energy Knights Essentia:
        # that contract DOES keep an archive now, so the fetch_for_month test
        # alone no longer excludes it and the kind test is the one doing the
        # work. Quoting it through the historical replay would need the same
        # spot cache the dynamic side needs and does not have here.
        archive_capable = (
            current_extractor.fetch_for_month is not None
            and other_extractor.fetch_for_month is not None
            and current_kind not in SPOT_PRICED_CONTRACT_KINDS
            and other_kind not in SPOT_PRICED_CONTRACT_KINDS
        )
        if archive_capable and other_snap is not None and current_snapshot is not None:
            # Replay the coordinator's historical spot cache so a
            # spot-indexed injection (Cociter Variable) gets the same
            # per-hour feed-in credit the live YTD applies; spots are the
            # Belgian day-ahead, supplier-independent, so the same cache
            # prices both sides. A no-op for monthly-indicative contracts.
            hist_spots = coord._historical_spots
            # The slots go with them, or a floored feed-in formula would be
            # replayed here off the hour mean while the annual row printed
            # right above it credits each slot. Unreachable today, this
            # block needs an archive-capable pair and the only supplier
            # that floors exposes no archive, and threaded so it stays
            # unreachable rather than latent.
            hist_quarters = coord._historical_spot_quarters
            if compare_spot_injection and not hist_spots:
                # The user's own entry isn't spot-needing, so the live
                # coordinator never backfilled its cache. Fetch into a
                # LOCAL dict for this throwaway quote with the key typed in
                # compare_api_key (or the entry's own); without it the
                # credit silently drops and the YTD overstates the
                # spot-indexed target's cost. Save/restore the coordinator
                # cache so a read-only comparison doesn't mutate (and have
                # the next tick persist) live coordinator state.
                borrowed = self._compare.get(CONF_API_KEY) or current.get(CONF_API_KEY)
                if borrowed:
                    # Isolated: this wants the target's own year, not whatever
                    # the entry happens to hold. Copied out before the context
                    # manager puts the entry's caches back, since it restores
                    # into the same dicts rather than rebinding them.
                    with _borrowed_spot_cache(coord, isolate=True):
                        await coord._ensure_historical_spots(
                            ytd_from, today_local, borrowed
                        )
                        hist_spots = dict(coord._historical_spots)
                        hist_quarters = dict(coord._historical_spot_quarters)
            try:
                current_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    current_extractor,
                    # The RAW card, which is what the coordinator hands the
                    # same function for the current_year_cost sensor. Handing
                    # it the cohort-spliced one instead is not idempotent, for
                    # all that it re-resolves the cohort itself: the splice has
                    # already turned a spot-monthly leg into a variable one, so
                    # the month-indexed re-price finds nothing to do and every
                    # past month falls back to the figure its card printed,
                    # which is the PREVIOUS month's index. The page's own row
                    # and the sensor beside it then answered differently on all
                    # 29 month-indexed contracts.
                    raw_snapshot,
                    quote_entry,
                    historical_spots=hist_spots,
                    spot_quarters=hist_quarters,
                    billed_peak_kw=peak_kw,
                    rlp_weights=_coordinator_rlp_weights(self.config_entry),
                    rlp_index_weights=_coordinator_rlp_index_weights(
                        self.config_entry, current_snapshot
                    ),
                    spp_weights=_coordinator_spp_weights(
                        self.config_entry, current_snapshot, own=True
                    ),
                )
                compare_ytd_val = await _compute_current_year_cost(
                    self.hass,
                    session,
                    other_extractor,
                    other_snap,
                    target_entry,
                    contract_override=self._compare[CONF_CONTRACT],
                    meter_override=meter,
                    historical_spots=hist_spots,
                    spot_quarters=hist_quarters,
                    billed_peak_kw=peak_kw,
                    # The household's profile for the load shape, the quoted
                    # card's own blend for the index it settles on. This pair
                    # is the whole point of the argument: the two rows sit side
                    # by side and the delta between them is what the page says.
                    rlp_weights=_coordinator_rlp_weights(self.config_entry),
                    rlp_index_weights=_coordinator_rlp_index_weights(
                        self.config_entry, other_snap
                    ),
                    spp_weights=_coordinator_spp_weights(
                        self.config_entry, other_snap, own=False
                    ),
                )
            except Exception:  # noqa: BLE001 - degrade to '-'
                current_ytd_val = None
                compare_ytd_val = None
            if current_ytd_val is not None and compare_ytd_val is not None:
                placeholders["current_ytd"] = f"{current_ytd_val:.2f}"
                placeholders["compare_ytd"] = f"{compare_ytd_val:.2f}"
                ytd_delta = compare_ytd_val - current_ytd_val
                placeholders["delta_ytd"] = (
                    f"{'+' if ytd_delta >= 0 else ''}{ytd_delta:.2f}"
                )
                _populate_charts(
                    placeholders,
                    current_label=_chart_labels(current, self._compare)[0],
                    compare_label=_chart_labels(current, self._compare)[1],
                )
                return placeholders
            # Fall through to the simple model on engine failure.

        if (
            ytd_kwh is not None
            and current_per_kwh is not None
            and other_per_kwh is not None
            and other_snap is not None
            and current_snapshot is not None
        ):
            # The YTD what-if mirrors the live current_year_cost sensor and
            # the archive YTD path, both of which DO accrue the Flanders
            # capacity tariff, so it is kept here too and prorated the same
            # per-month way rather than by the uniform year fraction.
            current_ytd = _annual_bill(
                current_snapshot,
                quote_entry,
                peak_kw,
                current_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                current_inj_price,
                export_per_kwh=current_export_per_kwh,
                register_weights=hh.register_weights,
                fee_proration=fee_proration,
                prosumer_proration=month_proration,
                capacity_proration=month_proration,
                meter=current_meter,
                # Window-scoped, not the year-ahead figure the annual rows
                # carry: this is what these days have already accrued. The
                # engine path above credits it, so a row that fell back here
                # was the only one on the page priced without one.
                welcome_credit_eur=_ytd_welcome_credit(
                    current_snapshot,
                    hh.signing_snapshot,
                    _parse_iso_date(current.get(CONF_CONTRACT_START_DATE)),
                    dt_util.as_local(now_utc),
                    dso,
                    region,
                    await _spot_for(current_snapshot),
                    current_meter,
                    dso_mode,
                    hour_weights,
                    ytd_kwh,
                    ytd_inj_kwh,
                    annual_kwh=annual_kwh,
                    regime=regime,
                    window_start=ytd_from,
                    fee_proration=fee_proration,
                ),
            )
            compare_ytd = _annual_bill(
                other_snap,
                target_entry,
                peak_kw,
                other_per_kwh,
                ytd_kwh,
                ytd_inj_kwh,
                compare_inj_price,
                export_per_kwh=other_export_per_kwh,
                register_weights=hh.register_weights,
                fee_proration=fee_proration,
                prosumer_proration=month_proration,
                capacity_proration=month_proration,
                meter=meter,
                # A candidate is granted what its own card prints today, on the
                # same window, exactly as the annual row beside it reads it.
                welcome_credit_eur=_ytd_welcome_credit(
                    other_snap,
                    other_snap,
                    today_local,
                    dt_util.as_local(now_utc),
                    dso,
                    region,
                    await _spot_for(other_snap),
                    meter,
                    other_dso_mode,
                    hour_weights,
                    ytd_kwh,
                    ytd_inj_kwh,
                    annual_kwh=annual_kwh,
                    regime=regime,
                    window_start=ytd_from,
                    fee_proration=fee_proration,
                ),
            )
            placeholders["current_ytd"] = f"{current_ytd:.2f}"
            placeholders["compare_ytd"] = f"{compare_ytd:.2f}"
            ytd_delta = compare_ytd - current_ytd
            placeholders["delta_ytd"] = (
                f"{'+' if ytd_delta >= 0 else ''}{ytd_delta:.2f}"
            )
        _populate_charts(
            placeholders,
            current_label=_chart_labels(current, self._compare)[0],
            compare_label=_chart_labels(current, self._compare)[1],
        )
        return placeholders


def _chart_labels(
    current: Mapping[str, Any], compare: Mapping[str, Any]
) -> tuple[str, str]:
    """The two row labels for the comparison charts.

    The supplier name alone stops distinguishing the sides as soon as both
    contracts come from one supplier, which the picker now allows outright.
    Fall back through what actually differs: supplier, then contract, then
    neither - the same contract quoted against itself under a different meter
    or regime, where the only honest labels are which side is which.
    """
    cur_supplier = _label_for_supplier(current[CONF_SUPPLIER])
    cmp_supplier = _label_for_supplier(compare[CONF_SUPPLIER])
    if cur_supplier != cmp_supplier:
        return cur_supplier, cmp_supplier
    cur_contract = _label_for_contract(current[CONF_SUPPLIER], current[CONF_CONTRACT])
    cmp_contract = _label_for_contract(compare[CONF_SUPPLIER], compare[CONF_CONTRACT])
    if cur_contract != cmp_contract:
        return cur_contract, cmp_contract
    return "Your entry", "Quoted"
