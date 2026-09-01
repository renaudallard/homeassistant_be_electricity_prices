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

"""ENTSO-E day-ahead price client (Belgian bidding zone).

Uses ``aiohttp`` (provided by Home Assistant) for HTTP and ``defusedxml``
for safe XML parsing; ``defusedxml`` is declared in ``manifest.json``
requirements.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime, timedelta

# defusedxml's ElementTree disables entity expansion / external-entity
# loading on the stdlib parser. The ENTSO-E endpoint is HTTPS-trusted,
# but a bare xml.etree parse leaves a TLS-MitM-exposed XXE surface for
# free; defusedxml is declared in manifest.json requirements.
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]

import aiohttp

from homeassistant.util import dt as dt_util

from .const import (
    ENERGY_CHARTS_BE_ZONE,
    ENERGY_CHARTS_URL,
    ENTSOE_BASE_URL,
    ENTSOE_BE_DOMAIN,
)
from .providers._pdf import error_text

_NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}

# Strip the credential from any error text that may surface to the user.
_TOKEN_RE = re.compile(r"(securityToken=)[^&\s'\"]+")

_LOGGER = logging.getLogger(__name__)


class EntsoeAuthError(Exception):
    """Raised when the API rejects the security token."""


class EntsoeError(Exception):
    """Raised on transport or parsing failure."""


class EntsoeClient:
    """Minimal ENTSO-E client for day-ahead document A44."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._api_key = api_key
        self._session = session

    def _redact(self, text: str) -> str:
        """Strip the security token from error text.

        aiohttp client errors stringify with the full request URL, which
        carries ``securityToken=<api_key>``. The resulting EntsoeError
        message reaches user-visible surfaces (the current_price
        last_error attribute, the snapshot_stale Repairs card, the HA
        log), so scrub the credential before it is raised. Diagnostics
        scrubs the same field separately.
        """
        if self._api_key:
            text = text.replace(self._api_key, "***")
        return _TOKEN_RE.sub(r"\1***", text)

    async def fetch_day_ahead(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        """Fetch BE day-ahead prices in EUR/kWh for the given UTC window.

        Returns a mapping of slot-start (UTC) -> EUR/kWh.  ENTSO-E
        publishes prices in EUR/MWh; we convert here.  Sub-hourly points
        are aggregated to the hour by default; pass
        ``quarter_hourly=True`` to keep the native 15-minute slots.
        """
        params = {
            "documentType": "A44",
            "in_Domain": ENTSOE_BE_DOMAIN,
            "out_Domain": ENTSOE_BE_DOMAIN,
            "periodStart": _fmt(period_start),
            "periodEnd": _fmt(period_end),
            "securityToken": self._api_key,
        }
        try:
            async with self._session.get(
                ENTSOE_BASE_URL, params=params, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 401:
                    raise EntsoeAuthError("ENTSO-E rejected the API key")
                if resp.status >= 400:
                    body = await resp.text()
                    raise EntsoeError(
                        self._redact(f"ENTSO-E HTTP {resp.status}: {body[:200]}")
                    )
                payload = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # aiohttp.ClientTimeout fires asyncio.TimeoutError, which is
            # NOT an aiohttp.ClientError on 3.11+; without the second
            # alternative, a slow ENTSO-E response would bubble a bare
            # TimeoutError through the wizard and the coordinator
            # categorisation paths.
            raise EntsoeError(self._redact(error_text(err))) from err

        # ENTSO-E's A44 doc is small today (~100 KB hourly, larger if
        # the bidding zone moves to PT15M). Offload XML parsing to a
        # worker thread so the coordinator's update tick can't ever
        # stall HA's event loop.
        return await asyncio.to_thread(parse_day_ahead_xml, payload, quarter_hourly)


def _MAX_PERIOD_SLOTS(step: timedelta) -> int:  # noqa: N802 - reads as a bound
    """Most points one Period may contribute, for a resolution step.

    Bounds the forward-fill by 31 days' worth of slots. The document's own
    ``timeInterval`` end drives that loop, so an out-of-range end would
    otherwise allocate without limit.
    """
    return math.ceil(timedelta(days=31).total_seconds() / step.total_seconds())


def parse_day_ahead_xml(
    xml: str, quarter_hourly: bool = False
) -> dict[datetime, float]:
    """Parse an A44 publication document into slot-start -> EUR/kWh.

    By default sub-hourly publications (PT15M, PT30M) are aggregated to
    hour-start by averaging every sub-hour point that falls inside the
    same UTC hour, because most consumers (YTD billing, hourly-billed
    suppliers) assume hourly keys.

    Pass ``quarter_hourly=True`` to keep the native sub-hour slots: each
    point is keyed by its own start instant instead of being folded into
    the hour. Used for suppliers that bill per quarter-hour (Engie
    Dynamic). A PT60M source still yields hourly keys either way.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise EntsoeError(f"invalid XML: {err}") from err
    except DefusedXmlException as err:
        # defusedxml rejects entity expansion / external references with
        # its own exceptions, which are NOT ParseError subclasses; wrap
        # them so a hostile (TLS-MitM) payload surfaces as EntsoeError
        # rather than an unhandled exception out of the coordinator tick.
        raise EntsoeError(f"unsafe XML rejected: {err}") from err

    # ENTSO-E answers a rejected or quota-exhausted token with HTTP 200 +
    # an Acknowledgement_MarketDocument (no TimeSeries) rather than a 401.
    # Returning {} here would silently blank the dynamic price table with
    # no Repairs guidance. The runtime always requests a window that
    # includes today, and the BE zone always publishes today's curve, so a
    # document carrying zero matching data really means the request was
    # refused. Surface it as an auth error so the coordinator raises the
    # "rotate your token" Repairs card.
    if _local_name(root.tag) == "Acknowledgement_MarketDocument":
        raise EntsoeAuthError(
            f"ENTSO-E returned no data ({_ack_reason(root)}); the API key "
            "may be invalid or its daily quota exhausted"
        )

    # Per-slot accumulators: (sum, count) so we can take the mean at the
    # end without holding every sub-hour point in memory. The slot key is
    # the hour (default) or the native sub-hour instant (quarter_hourly).
    # Bucket per resolution step so two overlapping TimeSeries of
    # different resolution are never blended into an unweighted mean.
    # ENTSO-E returns BOTH a PT60M and a PT15M series for the same
    # delivery period in 15-minute day-ahead zones (Belgium; entsoe-py
    # #204), and averaging "1 hourly point + 4 quarter points" mis-prices
    # every hour. Within one resolution, duplicate points still average
    # (a corrected re-publication). Keyed by the resolution step seconds.
    sums: dict[float, dict[datetime, float]] = {}
    counts: dict[float, dict[datetime, int]] = {}

    for ts in root.findall("ns:TimeSeries", _NS):
        # Iterate EVERY Period, not just the first. `find` returned one, so a
        # TimeSeries carrying consecutive Periods (a publication that splits
        # the window per day) silently lost every slot after the first block.
        for period in ts.findall("ns:Period", _NS):
            interval = period.find("ns:timeInterval", _NS)
            resolution = period.findtext("ns:resolution", default="", namespaces=_NS)
            if interval is None or not resolution.startswith("PT"):
                continue
            start_text = interval.findtext("ns:start", default="", namespaces=_NS)
            end_text = interval.findtext("ns:end", default="", namespaces=_NS)
            if not start_text:
                continue
            start = _parse_iso_utc(start_text)
            step = _resolution_to_timedelta(resolution)
            if step is None:
                # Skip series at a resolution we don't know how to bucket
                # (e.g. PT5M) instead of aborting the whole document; other
                # series in the same publication may still be hourly.
                continue
            step_s = step.total_seconds()
            bucket_sum = sums.setdefault(step_s, {})
            bucket_count = counts.setdefault(step_s, {})
            # IEC 62325-451-3 / A44 lets a publication document omit any
            # Point whose price is unchanged from the previous position
            # ("carry forward" semantics). Collect only the explicit points
            # first, then forward-fill across the whole interval so the
            # caller never sees a gap that they'd interpolate as a stale
            # neighbour hour.
            explicit: dict[int, float] = {}
            for point in period.findall("ns:Point", _NS):
                position_text = point.findtext(
                    "ns:position", default="0", namespaces=_NS
                )
                price_text = point.findtext(
                    "ns:price.amount", default="", namespaces=_NS
                )
                if not price_text:
                    continue
                try:
                    position = int(position_text)
                    price = float(price_text)
                except ValueError as err:
                    raise EntsoeError(f"malformed point in document: {err}") from err
                # float() accepts "NaN" / "Infinity" / "-Infinity", and overflows a
                # long literal like "1e400" to inf, so a malformed price reached
                # the spot cache as a real-looking number. From there it spreads:
                # factor*spot + base is nan, _mean_of_month propagates it so a
                # spot-monthly contract's whole flat rate goes nan, and the
                # backfill writes it into recorder statistics where it outlives
                # the document. Reject it like any other unparseable point; the
                # coordinator already degrades that to the cached curve.
                if not math.isfinite(price):
                    raise EntsoeError(
                        f"malformed point in document: non-finite price {price_text!r}"
                    )
                explicit[position] = price / 1000.0
            if not explicit:
                continue
            if end_text:
                end = _parse_iso_utc(end_text)
                # Round up so a window that isn't an exact multiple of the
                # resolution doesn't drop its trailing sub-hour slot. Use
                # max() with the explicit positions as a floor in case the
                # publication shrinks the interval relative to the points.
                span_s = max(0.0, (end - start).total_seconds())
                inferred = math.ceil(span_s / step.total_seconds())
                # Cap the span before it becomes a point count. `end` is taken
                # from the document, so a malformed one drives the forward-fill
                # loop below directly: a 100-year PT15M interval produced 3.5M
                # slots, 1 GB of RSS and eight seconds of CPU, which is an OOM on
                # the hardware this usually runs on. A day-ahead publication
                # covers a day or two; 31 days is far past anything legitimate
                # and far short of doing damage.
                inferred = min(inferred, _MAX_PERIOD_SLOTS(step))
                total = max(inferred, max(explicit))
            else:
                total = max(explicit)
            # The cap above bounds what the document's own timeInterval can
            # ask for, but a single Point carrying an out-of-range position
            # walked straight past it: one bogus <position>3000000</position>
            # on an ordinary day-ahead document produced 3 000 000 slots,
            # 870 MB of peak memory and 163 s of CPU, which is the same OOM
            # the interval cap exists to prevent. Bound the loop itself.
            total = min(total, _MAX_PERIOD_SLOTS(step))
            # Carry-forward only: ENTSO-E documents fill *forward* from the
            # previous explicit point, never backward. If position 1 itself
            # is missing, every position before the first explicit one
            # contributes nothing to the hourly buckets and the affected
            # hours simply don't appear in the output dict. Downstream
            # callers treat a missing key as "no data for that hour"
            # (current_price falls back to the nearest hour, sensors go
            # unknown), which is the correct degradation when the upstream
            # document is genuinely unspecified for the slot.
            last: float | None = None
            for position in range(1, total + 1):
                if position in explicit:
                    last = explicit[position]
                if last is None:
                    continue
                point_start = start + step * (position - 1)
                if quarter_hourly:
                    key = point_start
                else:
                    key = point_start.replace(minute=0, second=0, microsecond=0)
                bucket_sum[key] = bucket_sum.get(key, 0.0) + last
                bucket_count[key] = bucket_count.get(key, 0) + 1

    # Prefer the native resolution for the requested grid: the hourly
    # product (largest step) in hourly mode, the 15-minute series
    # (smallest step) in quarter mode. Other resolutions only fill keys
    # the preferred one does not cover, so overlapping series never blend.
    result: dict[datetime, float] = {}
    for step_s in sorted(sums, reverse=not quarter_hourly):
        bucket_sum = sums[step_s]
        bucket_count = counts[step_s]
        for key, summed in bucket_sum.items():
            result.setdefault(key, summed / bucket_count[key])
    return result


def _local_name(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree prepends to a tag."""
    return tag.rsplit("}", 1)[-1]


def _ack_reason(root: object) -> str:
    """Best-effort ``code text`` from an Acknowledgement's Reason block."""
    for el in root.iter():  # type: ignore[attr-defined]
        if _local_name(el.tag) != "Reason":
            continue
        code = ""
        text = ""
        for child in el:
            name = _local_name(child.tag)
            if name == "code":
                code = (child.text or "").strip()
            elif name == "text":
                text = (child.text or "").strip()
        return f"{code} {text}".strip() or "no reason given"
    return "no reason given"


def _fmt(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%d%H%M")


def _parse_iso_utc(text: str) -> datetime:
    # A malformed timeInterval start/end would otherwise raise a bare
    # ValueError out of parse_day_ahead_xml; wrap it as EntsoeError so
    # the coordinator's EntsoeError handler keeps serving cached spots
    # instead of the exception escaping uncategorised.
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as err:
        raise EntsoeError(f"malformed timeInterval timestamp {text!r}: {err}") from err
    # ENTSO-E A44 timestamps are UTC (they carry a 'Z'/offset), but if a
    # document ever omits the zone, fromisoformat returns a naive value
    # and astimezone would treat it as the HA host's local time. Treat a
    # naive timestamp as UTC -- the publication document is UTC by spec.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _resolution_to_timedelta(resolution: str) -> timedelta | None:
    if resolution == "PT60M":
        return timedelta(hours=1)
    if resolution == "PT15M":
        return timedelta(minutes=15)
    if resolution == "PT30M":
        return timedelta(minutes=30)
    return None


# ---- keyless day-ahead fallback ----------------------------------------------


class EnergyChartsClient:
    """Keyless BE day-ahead client, used only when ENTSO-E is unreachable.

    Same contract as ``EntsoeClient.fetch_day_ahead``: slot-start (UTC) ->
    EUR/kWh, aggregated to the hour unless ``quarter_hourly``. The upstream
    publishes EUR/MWh on whatever grid the auction cleared on, so a pre-2025
    range comes back hourly and a current one quarter-hourly, exactly as the
    ENTSO-E path already handles.

    Unlike ENTSO-E this returns ONE series, never a PT60M and a PT15M for the
    same period, so there is no resolution-blending hazard to guard against
    here -- the hour is simply the mean of whatever slots fall inside it.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def fetch_day_ahead(
        self,
        period_start: datetime,
        period_end: datetime,
        *,
        quarter_hourly: bool = False,
    ) -> dict[datetime, float]:
        """Fetch BE day-ahead prices in EUR/kWh for the given UTC window."""
        # The endpoint windows on the LOCAL (Brussels) day and takes plain
        # dates, inclusive at both ends. Callers hand us local-midnight
        # anchored UTC instants, so resolve the local days they span and
        # trim the response back to the exact window afterwards. `end` is
        # exclusive for us, so step back inside it before taking its date.
        start_day = dt_util.as_local(period_start).date()
        end_day = dt_util.as_local(period_end - timedelta(seconds=1)).date()
        if end_day < start_day:
            return {}
        params = {
            "bzn": ENERGY_CHARTS_BE_ZONE,
            "start": start_day.isoformat(),
            "end": end_day.isoformat(),
        }
        try:
            async with self._session.get(
                ENERGY_CHARTS_URL,
                params=params,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise EntsoeError(f"energy-charts HTTP {resp.status}: {body[:200]}")
                payload = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EntsoeError(f"energy-charts: {error_text(err)}") from err

        return await asyncio.to_thread(
            _parse_energy_charts, payload, period_start, period_end, quarter_hourly
        )


def _parse_energy_charts(
    body: str,
    period_start: datetime,
    period_end: datetime,
    quarter_hourly: bool,
) -> dict[datetime, float]:
    """Parse an energy-charts /price payload into slot-start -> EUR/kWh.

    A range the upstream holds no data for answers 200 with a PLAIN-TEXT body
    ("end must be >= start"), not JSON and not an error status, so the decode
    failure is the only thing standing between that and an uncaught exception
    out of the coordinator tick.
    """
    try:
        doc = json.loads(body)
    except json.JSONDecodeError as err:
        raise EntsoeError(f"energy-charts: non-JSON response: {body[:120]!r}") from err
    if not isinstance(doc, dict):
        raise EntsoeError("energy-charts: response was not an object")
    seconds = doc.get("unix_seconds")
    prices = doc.get("price")
    if not isinstance(seconds, list) or not isinstance(prices, list):
        raise EntsoeError("energy-charts: response carried no price series")

    # Bucket per slot, then mean. Same shape as the ENTSO-E parser so the
    # two sources are interchangeable to every caller.
    sums: dict[datetime, float] = {}
    counts: dict[datetime, int] = {}
    for raw_when, raw_price in zip(seconds, prices):
        if not isinstance(raw_when, (int, float)) or not isinstance(
            raw_price, (int, float)
        ):
            # A gap is published as null. Skipping leaves the slot absent,
            # which every caller already reads as "no data for that slot".
            continue
        when = datetime.fromtimestamp(float(raw_when), UTC)
        if not period_start <= when < period_end:
            # The request is day-granular, so the response overhangs the
            # asked-for window whenever it does not start at local midnight.
            continue
        slot = (
            when if quarter_hourly else when.replace(minute=0, second=0, microsecond=0)
        )
        sums[slot] = sums.get(slot, 0.0) + float(raw_price) / 1000.0
        counts[slot] = counts.get(slot, 0) + 1
    return {slot: sums[slot] / counts[slot] for slot in sums}


async def fetch_day_ahead_or_fallback(
    api_key: str,
    session: aiohttp.ClientSession,
    period_start: datetime,
    period_end: datetime,
    *,
    quarter_hourly: bool = False,
) -> tuple[dict[datetime, float], str]:
    """Day-ahead prices plus the source that supplied them.

    ENTSO-E stays the source of record; energy-charts answers only when
    ENTSO-E could not. An ``EntsoeAuthError`` is deliberately NOT caught: a
    rejected or exhausted token has to keep raising its Repairs card, and
    quietly papering over it with a keyless source is how an entry runs for
    months on a credential its owner never renews.

    Not used by the config flow's key check. That check exists to tell an
    invalid key from an unreachable server, and a fallback that answered for
    it would let a user finalise an entry whose key never worked.
    """
    client = EntsoeClient(api_key, session)
    try:
        return await client.fetch_day_ahead(
            period_start, period_end, quarter_hourly=quarter_hourly
        ), "entsoe"
    except EntsoeAuthError:
        raise
    except EntsoeError as err:
        primary = err
        _LOGGER.debug("ENTSO-E unavailable (%s); trying the keyless fallback", err)

    # Both messages travel together from here on. When the fallback fails too
    # this is what reaches last_error and the log, and the ENTSO-E half is the
    # half that explains the outage -- reporting only "energy-charts: non-JSON
    # response" for a day ENTSO-E spent returning 503 sends the reader after
    # the wrong service entirely.
    try:
        prices = await EnergyChartsClient(session).fetch_day_ahead(
            period_start, period_end, quarter_hourly=quarter_hourly
        )
    except EntsoeError as err:
        raise EntsoeError(f"ENTSO-E: {primary}; fallback: {err}") from err
    if not prices:
        # Nothing usable from either side. Raise rather than return empty so
        # the caller's existing EntsoeError path decides what to degrade to.
        raise EntsoeError(
            f"ENTSO-E: {primary}; fallback returned no prices for the window"
        )
    return prices, "energy-charts"
