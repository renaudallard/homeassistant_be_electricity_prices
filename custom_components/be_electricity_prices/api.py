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

from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET

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
        except aiohttp.ClientError as err:
            raise EntsoeError(str(err)) from err

        return parse_day_ahead_xml(payload)


def parse_day_ahead_xml(xml: str) -> dict[datetime, float]:
    """Parse an A44 publication document into hour-start -> EUR/kWh."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as err:
        raise EntsoeError(f"invalid XML: {err}") from err

    out: dict[datetime, float] = {}
    for ts in root.findall("ns:TimeSeries", _NS):
        period = ts.find("ns:Period", _NS)
        if period is None:
            continue
        interval = period.find("ns:timeInterval", _NS)
        resolution = period.findtext("ns:resolution", default="", namespaces=_NS)
        if interval is None or not resolution.startswith("PT"):
            continue
        start_text = interval.findtext("ns:start", default="", namespaces=_NS)
        if not start_text:
            continue
        start = _parse_iso_utc(start_text)
        step = _resolution_to_timedelta(resolution)
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
            ts_start = start + step * (position - 1)
            out[ts_start] = price / 1000.0
    return out


def _fmt(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y%m%d%H%M")


def _parse_iso_utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def _resolution_to_timedelta(resolution: str) -> timedelta:
    if resolution == "PT60M":
        return timedelta(hours=1)
    if resolution == "PT15M":
        return timedelta(minutes=15)
    if resolution == "PT30M":
        return timedelta(minutes=30)
    raise EntsoeError(f"unsupported resolution: {resolution}")
