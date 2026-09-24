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

"""What a backfill is run over, and against which recorder rows.

The window it covers once clamped to what exists, the statistic ids it will
write, the recorder models it needs and the one context object carrying the
prices, profiles and cards the whole run is priced from. Both halves of a
backfill are built on this, and neither of them owns it.
"""

from __future__ import annotations

from .brugel import ensure_power_term
from .cohort import _month_snapshot_cache, signing_month_snapshot, ytd_window_start
from .compare_inputs import _coordinator_rlp_index_weights
from .contract_periods import ContractPeriod, period_card, previous_periods
from .const import (
    CONF_API_KEY,
    CONF_CONTRACT,
    CONF_DSO,
    CONF_DSO_TARIFF_MODE,
    CONF_METER,
    CONF_REGION,
    CONF_SOLAR_REGIME,
    CONF_SUPPLIER,
    DOMAIN,
    DSO_MODE_BI_HORAIRE,
    METER_MONO,
    REGION_BRUSSELS,
    SOLAR_REGIME_COMPENSATION,
)
from .coordinator import BePricesCoordinator
from .injection import _injection_hourly_on_cohort
from .pricing import DsoTariffMode, MeterType
from .providers import get as get_extractor
from .providers._rates import InjectionRates
from .providers.base import SupplierSnapshot
from .snapshot_resolve import entry_annual_kwh
from .spot_stats import _energy_is_rlp_indexed, _rlp_blend_for, _spp_weighting_enabled
from .synergrid import RlpWeights, SppWeights
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from datetime import UTC, date, datetime, timedelta
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from typing import Any


def _stat_id(hass: HomeAssistant, entry: ConfigEntry, key: str) -> str | None:
    """Resolve the entity id (== statistic id) for one of this entry's sensors.

    Looks up the entity registry by unique id. Returns ``None`` when
    the entity hasn't been registered yet: callers skip silently
    rather than fabricating a slug from the description key, which
    would diverge from the user's renamed entity id.
    """
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}"
    )


def _hour_iter(start: datetime, end: datetime) -> list[datetime]:
    """UTC hour anchors in [start, end), aligned to the top of each hour."""
    cur = start.replace(minute=0, second=0, microsecond=0)
    if cur < start:
        cur += timedelta(hours=1)
    out: list[datetime] = []
    while cur < end:
        out.append(cur)
        cur += timedelta(hours=1)
    return out


def _floor_to_hour_utc(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _normalize_window(
    start: datetime | date | None,
    end: datetime | date | None,
    default_start: datetime,
) -> tuple[datetime, datetime]:
    """Return aware UTC [start_utc, end_utc) clamped to whole-hour buckets.

    The default window is [``default_start``, current hour), which the caller
    resolves from the entry: local 1 January for almost everyone, the contract
    start date for an entry that bills its year-to-date from there. End is
    exclusive so we don't write a row for the in-progress hour the
    live coordinator is about to fill itself.
    """
    now_local = dt_util.now()
    if start is None:
        start_local = default_start
    elif isinstance(start, datetime):
        start_local = (
            start
            if start.tzinfo is not None
            else start.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        )
    else:
        start_local = datetime.combine(
            start, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
    if end is None:
        end_local = now_local
    elif isinstance(end, datetime):
        end_local = (
            end
            if end.tzinfo is not None
            else end.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        )
    else:
        end_local = datetime.combine(
            end, datetime.min.time(), tzinfo=dt_util.DEFAULT_TIME_ZONE
        )
    start_utc = _floor_to_hour_utc(start_local)
    # Clamp the end to the current hour. compute_breakdown happily evaluates a
    # future hour for a fixed / variable / TOU / Impact contract, so an end
    # date past now (a mistyped year on the backfill_statistics service, whose
    # schema has no upper bound) wrote a full year of phantom price rows and
    # kept the cost sensor's fee, capacity and prosumer accrual running into
    # hours that have not happened. The None default already stopped at now;
    # an explicit end now gets the same bound.
    end_utc = min(_floor_to_hour_utc(end_local), _floor_to_hour_utc(now_local))
    return start_utc, end_utc


async def _existing_stat_window(
    hass: HomeAssistant, statistic_id: str, anchor: datetime
) -> bool:
    """Return True when at least one statistic row exists in a short
    window from ``anchor``.

    Used by :func:`backfill_if_missing` to derive the "is the recorder
    already populated" signal directly from the recorder, so we never
    need to persist a separate "backfill done" flag that would go
    stale across DB resets or supplier changes.

    Probes a 2-day window rather than the single anchor hour: a dynamic
    contract whose Jan 1 00:00 spot is genuinely missing skips that hour
    during backfill, so a single-hour probe would read empty and re-run
    the whole-year backfill on every restart. A short window still reads
    empty after a real DB reset (self-healing preserved) but tolerates a
    legitimately-absent leading hour.
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )
    except ImportError:
        return False
    try:
        rows = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            anchor,
            anchor + timedelta(days=2),
            {statistic_id},
            "hour",
            None,
            {"mean"},
        )
    except Exception:  # noqa: BLE001 - recorder may surface anything
        return False
    return bool(rows.get(statistic_id))


async def _clear_all(hass: HomeAssistant, statistic_ids: list[str]) -> None:
    """Delete every statistic row for ``statistic_ids``: the WHOLE series.

    The recorder's ``clear_statistics`` is the only public primitive
    here and it is series-scoped, not range-scoped. Callers must
    therefore restrict the use of ``clear=True`` to full-year re-runs;
    a narrower window with ``clear=True`` would wipe rows OUTSIDE the
    requested range and leave them gone. The user-facing service
    description in services.yaml + every locale's strings warn about
    this destructive scope.
    """
    try:
        from homeassistant.components.recorder import (  # type: ignore[attr-defined]
            get_instance,
        )
        from homeassistant.components.recorder.statistics import clear_statistics
    except ImportError:
        return
    instance = get_instance(hass)
    await instance.async_add_executor_job(clear_statistics, instance, statistic_ids)


@dataclass(frozen=True)
class _BackfillContext:
    """Everything both backfill passes resolve before their hour loop.

    The two passes opened with ~33 verbatim identical lines: the lazy recorder
    imports, the entry unpack, the per-month snapshot cache, the SPP weights
    and the three per-run caches. A new per-run input had to be added twice.
    """

    region: str
    dso: str
    meter: MeterType
    dso_mode: DsoTariffMode
    regime: str
    snap_for: Callable[[date], Awaitable[Any]]
    spp_weights: SppWeights | None
    rlp_weights: RlpWeights | None
    month_spp_cache: dict[tuple[int, int, bool], float | None]
    month_mean_cache: dict[tuple[int, int], float | None]
    # Whether the feed-in leg an hour credits keeps its per-hour index under a
    # cohort's monthly energy re-price, judged on today's card.
    hourly_injection: Callable[[InjectionRates | None], bool]
    # The card the welcome credit is read off: the signing month's where the
    # supplier keeps an archive and the entry has a start date, else the
    # current one. Same resolution the live year-to-date walk makes, so the
    # backfilled series credits the amount the sensor does.
    signing: Any
    # The entry's yearly volume, for the welcome credit's per-kWh term, which
    # the card measures over the first contract year rather than the window
    # the series covers. Resolved once here for the same reason everything
    # else on this class is.
    annual_kwh: float


def _recorder_models() -> tuple[Any, Any, Any, Any]:
    """The recorder symbols both passes import, imported lazily.

    Kept inside a function, never at module scope: backfill.py must import
    cleanly on an installation with no recorder (the annotations live under
    TYPE_CHECKING for the same reason). mypy --strict needs the ignore because
    the recorder does not re-export StatisticMeanType via __all__.
    """
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMetaData,
    )
    from homeassistant.components.recorder.statistics import (  # type: ignore[attr-defined]
        StatisticMeanType,
        async_import_statistics,
    )

    return StatisticData, StatisticMetaData, StatisticMeanType, async_import_statistics


async def _build_context(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
    *,
    snapshot: SupplierSnapshot | None = None,
) -> _BackfillContext:
    """Resolve the per-run inputs shared by both passes.

    ``_ensure_spp_weights`` must be awaited before ``_spp_weights`` is read;
    doing it here is what keeps that ordering from having to be remembered at
    two call sites.

    ``snapshot`` is set for the hours of a contract the household held earlier
    in the year (``_contract_segments``): its card, with ``entry`` the stand-in
    holding its settings. The load profile is then read as loaded rather than
    asked for, because asking for another card's blend would move the one the
    live coordinator prices its own contract on.
    """
    snap = snapshot if snapshot is not None else coordinator._snapshot
    assert snap is not None
    extractor = get_extractor(entry.data[CONF_SUPPLIER])
    contract = entry.data[CONF_CONTRACT]
    region = entry.data.get(CONF_REGION, "")
    spp_weights = None
    if _spp_weighting_enabled(entry, snap):
        await coordinator._ensure_spp_weights()
        spp_weights = coordinator._spp_weights
    # The RLP profile serves two things here: the month mean of an energy leg
    # indexed on it, and the allocation of a compensation entry's yearly net
    # over the year, which is how such a meter is settled.
    rlp_weights = None
    regime = entry.data.get(CONF_SOLAR_REGIME, "none")
    if snapshot is not None:
        rlp_weights = (
            _coordinator_rlp_index_weights(coordinator.entry, snap)
            if _energy_is_rlp_indexed(snap.energy)
            else (
                coordinator._rlp_weights or None
                if regime == SOLAR_REGIME_COMPENSATION
                else None
            )
        )
    elif (
        _energy_is_rlp_indexed(snap.energy) and entry.data.get(CONF_API_KEY)
    ) or regime == SOLAR_REGIME_COMPENSATION:
        await coordinator._ensure_rlp_weights(_rlp_blend_for(snap.energy))
        rlp_weights = coordinator._rlp_weights or None
    # A cache hit whenever the live tick has run, which resolves the same row
    # to freeze the signed energy rate; identity for an entry with no start
    # date or a supplier with no archive.
    signing = await signing_month_snapshot(
        hass, coordinator._session, extractor, contract, region, entry, snap
    )
    # Sibelga's power term for every year this run prices, not just the
    # current one. _resolve_snapshot asks the cache for the DELIVERY month's
    # year, so a run reaching back into a finished year found nothing there
    # and rebuilt those rows without the term, disagreeing with the live
    # sensor beside them. The coordinator tick only ever fetches this year.
    if region == REGION_BRUSSELS:
        for year in sorted({dt_util.as_local(hour).year for hour in hours}):
            await ensure_power_term(coordinator._session, year)
    return _BackfillContext(
        region=region,
        dso=entry.data[CONF_DSO],
        meter=entry.data.get(CONF_METER, METER_MONO),
        dso_mode=entry.data.get(CONF_DSO_TARIFF_MODE, DSO_MODE_BI_HORAIRE),
        regime=entry.data.get(CONF_SOLAR_REGIME, "none"),
        # Cache per-month snapshot lookups so a 365-day window touches at
        # most 12 archive fetches.
        snap_for=_month_snapshot_cache(
            hass, coordinator._session, extractor, contract, region, snap, entry
        ),
        # Entries whose injection is SPP-weighted - a card that indexes on
        # Belpex_SPP, or a custom monthly entry that opted in - price the
        # mean-indexed credit off the Synergrid solar profile; mirror the live
        # YTD credit so the backfill meets it at the seam.
        spp_weights=spp_weights,
        rlp_weights=rlp_weights,
        month_spp_cache={},
        month_mean_cache={},
        # A card whose injection is a per-hour spot formula with no printed
        # indicative (Cociter Tarif Variable) keeps that hourly index even when
        # a signing cohort re-prices its ENERGY leg to a monthly mean. Same
        # gate the live tick and the YTD walk apply, asked of each hour's
        # credited leg.
        hourly_injection=partial(_injection_hourly_on_cohort, snap, entry=entry),
        signing=signing,
        annual_kwh=entry_annual_kwh(entry, coordinator),
    )


async def _contract_segments(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    hours: list[datetime],
) -> list[tuple[ConfigEntry, SupplierSnapshot | None, list[datetime]]]:
    """``hours`` cut at each recorded supplier switch, oldest first.

    Each piece is priced on the contract that supplied it: the stand-in entry
    and card of a contract held earlier in the year (``period_card``), or the
    entry itself with ``None`` for its own contract and card. An entry that
    recorded no switch gets one piece, which is what every run was before. An
    hour outside every earlier contract's days, including one in a previous
    year, stays on the entry's own contract, as it always was.
    """
    if not hours:
        return []
    today = dt_util.now().date()
    periods = previous_periods(entry.data, ytd_window_start(entry, today), today)
    if not periods:
        return [(entry, None, hours)]
    segments: list[tuple[ConfigEntry, SupplierSnapshot | None, list[datetime]]] = []
    owner: int | None = None
    run: list[datetime] = []
    for hour in hours:
        day = dt_util.as_local(hour).date()
        index = next((i for i, p in enumerate(periods) if p.start <= day <= p.end), -1)
        if index != owner and run:
            segments.append(
                await _segment_for(hass, entry, coordinator, periods, owner, run)
            )
            run = []
        owner = index
        run.append(hour)
    if run:
        segments.append(
            await _segment_for(hass, entry, coordinator, periods, owner, run)
        )
    return segments


async def _segment_for(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: BePricesCoordinator,
    periods: list[ContractPeriod],
    index: int | None,
    hours: list[datetime],
) -> tuple[ConfigEntry, SupplierSnapshot | None, list[datetime]]:
    if index is None or index < 0:
        return entry, None, hours
    proxy, _extractor, card, _stand_in = await period_card(
        hass, coordinator._session, coordinator, periods[index]
    )
    return proxy, card, hours


_COST_SENSOR_KEY = "current_year_cost"
