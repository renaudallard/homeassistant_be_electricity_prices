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

"""Tests for the ENTSO-E XML parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.be_electricity_prices.api import (
    EntsoeClient,
    EntsoeError,
    _parse_iso_utc,
    parse_day_ahead_xml,
)


async def test_client_error_redacts_security_token() -> None:
    """aiohttp client errors stringify with the request URL, which carries
    securityToken=<api_key>. The wrapped EntsoeError must not leak it: it
    reaches the current_price last_error attribute, the Repairs card and
    the HA log."""

    class _RaisingCM:
        async def __aenter__(self) -> None:
            raise aiohttp.ClientError(
                "Cannot connect to host web-api.tp.entsoe.eu "
                "?securityToken=SECRET_KEY_123&documentType=A44"
            )

        async def __aexit__(self, *_: object) -> bool:
            return False

    session = MagicMock()
    session.get = MagicMock(return_value=_RaisingCM())
    client = EntsoeClient("SECRET_KEY_123", session)
    now = datetime(2026, 4, 30, tzinfo=UTC)
    with pytest.raises(EntsoeError) as excinfo:
        await client.fetch_day_ahead(now, now + timedelta(days=1))
    msg = str(excinfo.value)
    assert "SECRET_KEY_123" not in msg
    assert "securityToken=***" in msg


def _doc(points_xml: str, resolution: str = "PT60M") -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <Period>
      <timeInterval>
        <start>2026-04-29T22:00Z</start>
        <end>2026-04-30T22:00Z</end>
      </timeInterval>
      <resolution>{resolution}</resolution>
      {points_xml}
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


def test_parses_hourly_points_and_converts_to_eur_per_kwh() -> None:
    points = "".join(
        f"<Point><position>{i}</position><price.amount>{i * 10}</price.amount></Point>"
        for i in range(1, 4)
    )
    parsed = parse_day_ahead_xml(_doc(points))
    start = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    assert parsed[start] == pytest.approx(0.010)
    assert parsed[start + timedelta(hours=1)] == pytest.approx(0.020)
    assert parsed[start + timedelta(hours=2)] == pytest.approx(0.030)


def test_supports_quarter_hour_resolution() -> None:
    points = "<Point><position>1</position><price.amount>40</price.amount></Point>"
    parsed = parse_day_ahead_xml(_doc(points, resolution="PT15M"))
    assert parsed[datetime(2026, 4, 29, 22, 0, tzinfo=UTC)] == pytest.approx(0.040)


def test_quarter_hour_points_aggregate_to_hour_mean() -> None:
    """When ENTSO-E publishes PT15M points with varying prices, the
    parser must collapse them to one hour-start key carrying the
    arithmetic mean. Downstream sensors and the price table assume
    hourly granularity, so a per-15-min keyspace would silently break
    cheapest_window slot semantics and current_year_cost binning."""
    # First hour: 4 distinct prices (10, 20, 30, 40 EUR/MWh) -> mean
    # = 25 EUR/MWh = 0.025 EUR/kWh.
    # Second hour: a single point at position 5, the carry-forward
    # rule replays 40 EUR/MWh across all 4 slots.
    points = (
        "<Point><position>1</position><price.amount>10</price.amount></Point>"
        "<Point><position>2</position><price.amount>20</price.amount></Point>"
        "<Point><position>3</position><price.amount>30</price.amount></Point>"
        "<Point><position>4</position><price.amount>40</price.amount></Point>"
    )
    parsed = parse_day_ahead_xml(_doc(points, resolution="PT15M"))
    h0 = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    h1 = h0 + timedelta(hours=1)
    assert parsed[h0] == pytest.approx(0.025)
    # Every hour after the last explicit point inherits the last
    # value via carry-forward, then averages to that value.
    assert parsed[h1] == pytest.approx(0.040)


def test_quarter_hourly_keeps_native_slots() -> None:
    """With quarter_hourly=True each PT15M point keeps its own
    :00/:15/:30/:45 key instead of being averaged into the hour, so an
    Engie-style per-quarter contract is priced on the real curve."""
    points = (
        "<Point><position>1</position><price.amount>10</price.amount></Point>"
        "<Point><position>2</position><price.amount>20</price.amount></Point>"
        "<Point><position>3</position><price.amount>30</price.amount></Point>"
        "<Point><position>4</position><price.amount>40</price.amount></Point>"
    )
    parsed = parse_day_ahead_xml(_doc(points, resolution="PT15M"), quarter_hourly=True)
    base = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    assert parsed[base] == pytest.approx(0.010)
    assert parsed[base + timedelta(minutes=15)] == pytest.approx(0.020)
    assert parsed[base + timedelta(minutes=30)] == pytest.approx(0.030)
    assert parsed[base + timedelta(minutes=45)] == pytest.approx(0.040)
    # Full 24h interval at quarter resolution = 96 keys, each on a
    # :00/:15/:30/:45 boundary (positions past 4 carry forward 0.040).
    assert len(parsed) == 96
    assert all(k.minute in (0, 15, 30, 45) for k in parsed)


def test_quarter_hourly_on_hourly_source_still_hourly() -> None:
    """A PT60M document yields hourly keys even with quarter_hourly=True:
    there are no sub-hour points to keep."""
    points = "".join(
        f"<Point><position>{i}</position><price.amount>{i * 10}</price.amount></Point>"
        for i in range(1, 4)
    )
    parsed = parse_day_ahead_xml(_doc(points), quarter_hourly=True)
    base = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    assert parsed[base] == pytest.approx(0.010)
    assert parsed[base + timedelta(hours=1)] == pytest.approx(0.020)
    assert parsed[base + timedelta(hours=2)] == pytest.approx(0.030)
    # Every key sits on the hour; the 24h interval fills via carry-forward.
    assert all(k.minute == 0 for k in parsed)
    assert len(parsed) == 24


def test_invalid_xml_raises_entsoe_error() -> None:
    with pytest.raises(EntsoeError):
        parse_day_ahead_xml("<<<not xml")


def test_malformed_time_interval_raises_entsoe_error() -> None:
    # A bad timeInterval start/end must surface as EntsoeError (which the
    # coordinator categorises and degrades on), not a bare ValueError.
    doc = _doc("<Point><position>1</position><price.amount>50.0</price.amount></Point>")
    doc = doc.replace("2026-04-29T22:00Z", "NOT-A-TIMESTAMP")
    with pytest.raises(EntsoeError):
        parse_day_ahead_xml(doc)


def test_zoneless_timestamp_is_treated_as_utc() -> None:
    # ENTSO-E timestamps carry a 'Z', but a zoneless one must be read as
    # UTC (the publication document is UTC by spec), not as the HA host's
    # local time -- otherwise astimezone would shift every slot.
    assert _parse_iso_utc("2026-04-29T22:00Z") == datetime(
        2026, 4, 29, 22, 0, tzinfo=UTC
    )
    assert _parse_iso_utc("2026-04-29T22:00") == datetime(
        2026, 4, 29, 22, 0, tzinfo=UTC
    )


def test_unknown_resolution_skips_series_instead_of_aborting() -> None:
    """A series at a resolution we don't bucket (e.g. PT5M) must be
    skipped silently so the rest of the document still parses. Aborting
    the whole document would empty the spot table whenever ENTSO-E
    publishes a mixed-resolution document or moves to a new granularity.
    """
    point = "<Point><position>1</position><price.amount>40</price.amount></Point>"
    body = (
        "<TimeSeries><Period><timeInterval>"
        "<start>2026-04-29T22:00Z</start><end>2026-04-29T23:00Z</end>"
        "</timeInterval><resolution>PT5M</resolution>"
        f"{point}</Period></TimeSeries>"
        "<TimeSeries><Period><timeInterval>"
        "<start>2026-04-30T22:00Z</start><end>2026-04-30T23:00Z</end>"
        "</timeInterval><resolution>PT60M</resolution>"
        f"{point}</Period></TimeSeries>"
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Publication_MarketDocument"
        ' xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">'
        f"{body}</Publication_MarketDocument>"
    )
    parsed = parse_day_ahead_xml(xml)
    assert datetime(2026, 4, 30, 22, 0, tzinfo=UTC) in parsed
    assert datetime(2026, 4, 29, 22, 0, tzinfo=UTC) not in parsed
