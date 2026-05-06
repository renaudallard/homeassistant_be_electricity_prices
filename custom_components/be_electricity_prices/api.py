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

Uses ``aiohttp`` (provided by Home Assistant) and stdlib XML parsing
to keep ``requirements`` empty in ``manifest.json``.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta

# defusedxml's ElementTree disables entity expansion / external-entity
# loading on the stdlib parser. The ENTSO-E endpoint is HTTPS-trusted,
# but a bare xml.etree parse leaves a TLS-MitM-exposed XXE surface for
# free; defusedxml is a HA core dependency so we get it without
# bumping the manifest.
from defusedxml import ElementTree as ET  # type: ignore[import-untyped]

import aiohttp

from .const import ENTSOE_BASE_URL, ENTSOE_BE_DOMAIN

_NS = {"ns": "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"}


class EntsoeAuthError(Exception):
    """Raised when the API rejects the security token."""


class EntsoeError(Exception):
    """Raised on transport or parsing failure."""


class EntsoeClient:
    """Minimal ENTSO-E client for day-ahead document A44."""

    def __init__(self, api_key: str, session: aiohttp.ClientSession) -> None:
        self._api_key = api_key
        self._session = session

    async def fetch_day_ahead(
        self,
        period_start: datetime,
        period_end: datetime,
    ) -> dict[datetime, float]:
        """Fetch BE day-ahead prices in EUR/kWh for the given UTC window.

        Returns a mapping of hour-start (UTC) -> EUR/kWh.  ENTSO-E
        publishes prices in EUR/MWh; we convert here.
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
                    raise EntsoeError(f"ENTSO-E HTTP {resp.status}: {body[:200]}")
                payload = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            # aiohttp.ClientTimeout fires asyncio.TimeoutError, which is
            # NOT an aiohttp.ClientError on 3.11+; without the second
            # alternative, a slow ENTSO-E response would bubble a bare
            # TimeoutError through the wizard and the coordinator
            # categorisation paths.
            raise EntsoeError(str(err)) from err

        # ENTSO-E's A44 doc is small today (~100 KB hourly, larger if
        # the bidding zone moves to PT15M). Offload XML parsing to a
        # worker thread so the coordinator's update tick can't ever
        # stall HA's event loop.
        return await asyncio.to_thread(parse_day_ahead_xml, payload)


def parse_day_ahead_xml(xml: str) -> dict[datetime, float]:
    """Parse an A44 publication document into hour-start -> EUR/kWh.

    Sub-hourly publications (PT15M, PT30M) are aggregated to hour-start
    by averaging every sub-hour point that falls inside the same UTC
    hour. Downstream (price table, sensors, cheapest_window service,
    YTD billing) assumes hourly keys; flattening sub-hour slots here
    keeps that contract intact when ENTSO-E moves a bidding zone to
    15-minute publication.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise EntsoeError(f"invalid XML: {err}") from err

    # Per-hour accumulators: (sum, count) so we can take the mean at
    # the end without holding every sub-hour point in memory.
    hourly_sum: dict[datetime, float] = {}
    hourly_count: dict[datetime, int] = {}

    for ts in root.findall("ns:TimeSeries", _NS):
        period = ts.find("ns:Period", _NS)
        if period is None:
            continue
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
        # IEC 62325-451-3 / A44 lets a publication document omit any
        # Point whose price is unchanged from the previous position
        # ("carry forward" semantics). Collect only the explicit points
        # first, then forward-fill across the whole interval so the
        # caller never sees a gap that they'd interpolate as a stale
        # neighbour hour.
        explicit: dict[int, float] = {}
        for point in period.findall("ns:Point", _NS):
            position_text = point.findtext("ns:position", default="0", namespaces=_NS)
            price_text = point.findtext("ns:price.amount", default="", namespaces=_NS)
            if not price_text:
                continue
            try:
                position = int(position_text)
                price = float(price_text)
            except ValueError as err:
                raise EntsoeError(f"malformed point in document: {err}") from err
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
            total = max(inferred, max(explicit))
        else:
            total = max(explicit)
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
            slot_start = start + step * (position - 1)
            hour_start = slot_start.replace(minute=0, second=0, microsecond=0)
            hourly_sum[hour_start] = hourly_sum.get(hour_start, 0.0) + last
            hourly_count[hour_start] = hourly_count.get(hour_start, 0) + 1

    return {hour: hourly_sum[hour] / hourly_count[hour] for hour in hourly_sum}


def _fmt(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%d%H%M")


def _parse_iso_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _resolution_to_timedelta(resolution: str) -> timedelta | None:
    if resolution == "PT60M":
        return timedelta(hours=1)
    if resolution == "PT15M":
        return timedelta(minutes=15)
    if resolution == "PT30M":
        return timedelta(minutes=30)
    return None
