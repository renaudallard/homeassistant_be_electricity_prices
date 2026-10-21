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

import json
import pathlib
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.be_electricity_prices import api
from custom_components.be_electricity_prices.api import (
    _MAX_PERIOD_SLOTS,
    EnergyChartsClient,
    EntsoeAuthError,
    EntsoeClient,
    EntsoeError,
    EntsoeNoDataError,
    SmardClient,
    _bucket_slots,
    _parse_energy_charts,
    _parse_iso_utc,
    _retry_after,
    fetch_day_ahead_or_fallback,
    parse_day_ahead_xml,
)
from custom_components.be_electricity_prices.const import SMARD_QUARTERS_FROM
from tests import FakeBody


def _ack(text: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <Reason>
    <code>999</code>
    <text>{text}</text>
  </Reason>
</Acknowledgement_MarketDocument>
"""


def test_acknowledgement_document_raises_auth_error() -> None:
    """An HTTP 200 acknowledgement other than "no matching data" carries no
    TimeSeries either. The parser surfaces it as an auth error so the dynamic
    table isn't silently blanked and a key problem reaches Repairs."""
    with pytest.raises(EntsoeAuthError, match="Quota exceeded"):
        parse_day_ahead_xml(_ack("Quota exceeded"))


def test_a_no_matching_data_acknowledgement_is_not_an_auth_error() -> None:
    """A rejected token is answered HTTP 401. "No matching data found" is
    ENTSO-E holding nothing for the window, which a new key cannot fix and
    the keyless fallback can, so it must not raise the "rotate your API key"
    card."""
    with pytest.raises(EntsoeNoDataError, match="No matching data found"):
        parse_day_ahead_xml(_ack("No matching data found"))


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


_DUAL_RESOLUTION_DOC = """<?xml version="1.0" encoding="UTF-8"?>
<Publication_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3">
  <TimeSeries>
    <Period>
      <timeInterval><start>2026-04-29T22:00Z</start><end>2026-04-29T23:00Z</end></timeInterval>
      <resolution>PT60M</resolution>
      <Point><position>1</position><price.amount>100</price.amount></Point>
    </Period>
  </TimeSeries>
  <TimeSeries>
    <Period>
      <timeInterval><start>2026-04-29T22:00Z</start><end>2026-04-29T23:00Z</end></timeInterval>
      <resolution>PT15M</resolution>
      <Point><position>1</position><price.amount>10</price.amount></Point>
      <Point><position>2</position><price.amount>20</price.amount></Point>
      <Point><position>3</position><price.amount>30</price.amount></Point>
      <Point><position>4</position><price.amount>40</price.amount></Point>
    </Period>
  </TimeSeries>
</Publication_MarketDocument>
"""


def test_dual_resolution_series_do_not_blend() -> None:
    """ENTSO-E returns both a PT60M and a PT15M series for the same period
    in 15-minute day-ahead zones (Belgium). The two resolutions must not
    be averaged together: hourly mode takes the hourly product, quarter
    mode takes the native 15-minute slot."""
    h = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
    # Hourly mode -> the PT60M product (0.100), not the blend
    # (100 + 10 + 20 + 30 + 40) / 5 = 40 -> 0.040.
    assert parse_day_ahead_xml(_DUAL_RESOLUTION_DOC)[h] == pytest.approx(0.100)
    # Quarter mode -> the PT15M :00 slot (0.010), not (100 + 10) / 2 = 55.
    assert parse_day_ahead_xml(_DUAL_RESOLUTION_DOC, quarter_hourly=True)[
        h
    ] == pytest.approx(0.010)


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


def test_a_period_the_calendar_cannot_carry_raises_entsoe_error() -> None:
    """A year-9999 timeInterval passed the timestamp check and overflowed the
    date arithmetic of the forward-fill loop on the second position, as a
    bare OverflowError no caller catches: the tick took the generic failure
    path and the entry went unavailable instead of serving its cached curve."""
    doc = _doc(
        "<Point><position>1</position><price.amount>50.0</price.amount></Point>"
        "<Point><position>2</position><price.amount>60.0</price.amount></Point>"
    )
    doc = doc.replace("2026-04-29T22:00Z", "9999-12-31T22:00Z").replace(
        "2026-04-30T22:00Z", "9999-12-31T23:00Z"
    )
    with pytest.raises(EntsoeError, match="cannot carry"):
        parse_day_ahead_xml(doc)


def test_energy_charts_skips_an_instant_the_platform_cannot_represent() -> None:
    """Finite is not enough: 1e18 seconds and a negative year both pass the
    isfinite check and raise OSError or ValueError out of fromtimestamp,
    which the fallback path does not catch. Skipped like a null gap, so the
    point costs its own slot and the good one beside it is kept."""
    body = json.dumps(
        {"unix_seconds": [1e18, -1e15, 1788128100], "price": [50.0, 60.0, 100.0]}
    )
    out = _parse_energy_charts(
        body, datetime(2026, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), True
    )
    assert list(out.values()) == [0.1]


def test_zoneless_timestamp_is_treated_as_utc() -> None:
    # ENTSO-E timestamps carry a 'Z', but a zoneless one must be read as
    # UTC (the publication document is UTC by spec), not as the HA host's
    # local time; otherwise astimezone would shift every slot.
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


_NS_URL = "urn:iec62325.351:tc57wg16:451-3:publicationdocument:7:3"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", "1e400"])
def test_non_finite_price_is_rejected(bad: str) -> None:
    """float() accepts NaN / Infinity / -Infinity and overflows a long literal
    like 1e400 to inf, so a malformed price reached the spot cache looking like
    a real number. From there it spreads: factor*spot + base is nan, the
    month-mean propagates it so a spot-monthly contract's whole flat rate goes
    nan, and the backfill writes it into recorder statistics where it outlives
    the document. 1e400 matters most: that is a plausible upstream typo rather
    than a hostile literal."""
    doc = _doc(
        f"<Point><position>1</position><price.amount>{bad}</price.amount></Point>"
    )
    with pytest.raises(EntsoeError, match="non-finite price"):
        parse_day_ahead_xml(doc)


def test_finite_prices_including_negative_zero_still_parse() -> None:
    """Belgian day-ahead genuinely goes to and below zero, so the guard must
    only reject non-finite values."""
    for raw, want in (("85.5", 0.0855), ("-0.0", 0.0), ("-12.34", -0.01234)):
        doc = _doc(
            f"<Point><position>1</position><price.amount>{raw}</price.amount></Point>"
        )
        assert next(iter(parse_day_ahead_xml(doc).values())) == pytest.approx(want)


def test_out_of_range_period_end_cannot_allocate_without_bound() -> None:
    """The document's own timeInterval end drives the carry-forward loop, so a
    malformed end allocated without limit: a 100-year PT15M interval produced
    3.5 million slots and about a gigabyte of RSS, an OOM on the hardware this
    usually runs on. The span is capped at 31 days' worth of slots, far past
    any legitimate day-ahead publication."""
    doc = f"""<?xml version="1.0"?><Publication_MarketDocument xmlns="{_NS_URL}">
<TimeSeries><Period><timeInterval><start>2026-08-01T00:00Z</start>
<end>2126-08-01T00:00Z</end></timeInterval><resolution>PT15M</resolution>
<Point><position>1</position><price.amount>50</price.amount></Point>
</Period></TimeSeries></Publication_MarketDocument>"""
    out = parse_day_ahead_xml(doc, quarter_hourly=True)
    assert len(out) == 31 * 24 * 4
    # A normal two-day window is untouched by the cap.
    doc = doc.replace("2126-08-01", "2026-08-03")
    assert len(parse_day_ahead_xml(doc, quarter_hourly=True)) == 192


def test_every_period_in_a_timeseries_is_read() -> None:
    """`find` returned only the first Period, so a TimeSeries that splits its
    window into consecutive Periods silently lost every slot after the first
    block."""
    doc = f"""<?xml version="1.0"?><Publication_MarketDocument xmlns="{_NS_URL}">
<TimeSeries>
<Period><timeInterval><start>2026-08-01T00:00Z</start><end>2026-08-01T02:00Z</end>
</timeInterval><resolution>PT60M</resolution>
<Point><position>1</position><price.amount>10</price.amount></Point>
<Point><position>2</position><price.amount>20</price.amount></Point></Period>
<Period><timeInterval><start>2026-08-02T00:00Z</start><end>2026-08-02T02:00Z</end>
</timeInterval><resolution>PT60M</resolution>
<Point><position>1</position><price.amount>30</price.amount></Point>
<Point><position>2</position><price.amount>40</price.amount></Point></Period>
</TimeSeries></Publication_MarketDocument>"""
    out = parse_day_ahead_xml(doc)
    assert len(out) == 4
    assert out[datetime(2026, 8, 2, 0, tzinfo=UTC)] == pytest.approx(0.03)


def test_out_of_range_point_position_cannot_blow_the_slot_cap() -> None:
    """The interval cap bounds what a document's own timeInterval can ask
    for, but the loop ran to `max(inferred, max(explicit))`, so one Point
    carrying a bogus position walked straight past it.

    Measured before the bound: 3 000 000 slots, 870 MB of peak memory and
    163 s of CPU from a single ordinary-looking day-ahead document, which is
    the same OOM the interval cap was added to prevent. A malformed upstream
    document must cost a bounded amount of work.
    """
    points = (
        "<Point><position>1</position><price.amount>10</price.amount></Point>"
        "<Point><position>3000000</position><price.amount>20</price.amount></Point>"
    )
    parsed = parse_day_ahead_xml(_doc(points))
    assert len(parsed) <= _MAX_PERIOD_SLOTS(timedelta(minutes=60))
    # The legitimate leading point still parses.
    assert parsed[datetime(2026, 4, 29, 22, 0, tzinfo=UTC)] == pytest.approx(0.010)


# ---- keyless day-ahead fallback --------------------------------------------------


class _Resp:
    def __init__(
        self, status: int, body: str, headers: dict[str, str] | None = None
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content_length = None
        self.charset = None
        self.content = FakeBody(body.encode("utf-8"))


class _CM:
    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None) -> None:
        self._resp = resp
        self._exc = exc

    async def __aenter__(self) -> _Resp:
        if self._exc is not None:
            raise self._exc
        assert self._resp is not None
        return self._resp

    async def __aexit__(self, *_: object) -> bool:
        return False


class _FakeSession:
    """Dispatches on URL so one session can serve both legs of the fallback."""

    def __init__(
        self,
        entsoe: Exception | str,
        fallback_body: str,
        smard: dict[str, str] | None = None,
    ) -> None:
        self._entsoe = entsoe
        self._fallback_body = fallback_body
        # SMARD files by name; any other name is a 404, as on the real site.
        self._smard = smard or {}
        self.fallback_params: list[object] = []
        self.smard_files: list[str] = []

    def get(self, url: str, **kwargs: object) -> _CM:
        if "entsoe" in url:
            if isinstance(self._entsoe, str):
                return _CM(resp=_Resp(200, self._entsoe))
            if isinstance(self._entsoe, EntsoeAuthError):
                # A rejected token is an HTTP 401 on the wire.
                return _CM(resp=_Resp(401, ""))
            return _CM(exc=aiohttp.ClientError("503 Service Unavailable"))
        if "smard" in url:
            name = url.rsplit("/", 1)[1]
            self.smard_files.append(name)
            body = self._smard.get(name)
            return _CM(resp=_Resp(404, "") if body is None else _Resp(200, body))
        self.fallback_params.append(kwargs.get("params"))
        return _CM(resp=_Resp(200, self._fallback_body))


def test_energy_charts_converts_mwh_to_kwh_and_aggregates_to_the_hour() -> None:
    """Same contract as the ENTSO-E parser: EUR/kWh keyed by slot start, and
    an hour is the mean of the quarters inside it. Verified against the NEMO's
    own published aggregates, which are plain means of their quarters."""
    body = json.dumps(
        {
            "unix_seconds": [1788127200, 1788128100, 1788129000, 1788129900],
            "price": [200.0, 100.0, 100.0, 0.0],
        }
    )
    start = datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    end = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)

    hourly = _parse_energy_charts(body, start, end, False)
    assert hourly == {datetime(2026, 8, 30, 22, 0, tzinfo=UTC): 0.1}

    quarters = _parse_energy_charts(body, start, end, True)
    assert quarters[datetime(2026, 8, 30, 22, 0, tzinfo=UTC)] == 0.2
    assert quarters[datetime(2026, 8, 30, 22, 45, tzinfo=UTC)] == 0.0


def test_energy_charts_rejects_a_plain_text_body() -> None:
    """A range the upstream holds no data for answers 200 with a PLAIN-TEXT
    body ("end must be >= start"), not JSON and not an error status. Without
    the decode guard that is an uncaught exception out of the coordinator
    tick instead of the EntsoeError its caller already handles."""
    with pytest.raises(EntsoeError, match="non-JSON"):
        _parse_energy_charts(
            "end must be >= start",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            False,
        )


def test_energy_charts_skips_a_null_slot_rather_than_zeroing_it() -> None:
    """A gap is published as null. Dropping the slot leaves it absent, which
    every caller reads as "no data"; coercing it to 0.0 would bill a free
    hour."""
    body = json.dumps(
        {"unix_seconds": [1788127200, 1788128100], "price": [100.0, None]}
    )
    out = _parse_energy_charts(
        body, datetime(2026, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), True
    )
    assert len(out) == 1


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("nan price", '{"unix_seconds":[1788127200],"price":[NaN]}'),
        ("inf price", '{"unix_seconds":[1788127200],"price":[Infinity]}'),
        ("-inf price", '{"unix_seconds":[1788127200],"price":[-Infinity]}'),
        # Not a hostile literal: a plausible upstream typo, and the case that
        # actually matters, since json overflows it to inf rather than failing.
        ("overflowing literal", '{"unix_seconds":[1788127200],"price":[1e400]}'),
        # The timestamp side too: datetime.fromtimestamp raises ValueError on
        # NaN and OverflowError on inf, neither of which the caller catches.
        ("nan timestamp", '{"unix_seconds":[NaN],"price":[100.0]}'),
    ],
)
def test_energy_charts_rejects_a_non_finite_point(label: str, body: str) -> None:
    """The keyless fallback has to reject what the XML parser rejects.

    json.loads accepts NaN / Infinity / -Infinity by default, so a malformed
    response reached the spot cache as a real-looking number. The two sources
    are interchangeable to every caller, and from the cache the value spreads
    into factor*spot + base, into a spot-monthly contract's whole flat rate
    through _mean_of_month, and into recorder statistics through the backfill,
    where it outlives the response.
    """
    out = _parse_energy_charts(
        body, datetime(2026, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), True
    )
    assert out == {}, label


def test_energy_charts_drops_only_the_bad_point_not_the_window() -> None:
    """Skipped rather than raised, matching the rule for a null slot: one
    unusable point costs its own slot, not every good one beside it."""
    body = '{"unix_seconds":[1788127200,1788128100],"price":[NaN,100.0]}'
    out = _parse_energy_charts(
        body, datetime(2026, 1, 1, tzinfo=UTC), datetime(2030, 1, 1, tzinfo=UTC), True
    )
    assert list(out.values()) == [0.1]


def test_energy_charts_trims_the_response_to_the_requested_window() -> None:
    """The request is day-granular but the window is not, so the response
    overhangs it whenever it does not start at local midnight."""
    body = json.dumps(
        {"unix_seconds": [1788127200, 1788130800], "price": [100.0, 200.0]}
    )
    out = _parse_energy_charts(
        body,
        datetime(2026, 8, 30, 23, 0, tzinfo=UTC),
        datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        True,
    )
    assert list(out) == [datetime(2026, 8, 30, 23, 0, tzinfo=UTC)]


async def test_fallback_answers_when_entsoe_is_unreachable() -> None:
    """A transient ENTSO-E failure hands over to the keyless source, and the
    caller is told which one answered."""
    body = json.dumps({"unix_seconds": [1788127200], "price": [100.0]})
    session = _FakeSession(entsoe=EntsoeError("503"), fallback_body=body)
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
        datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
    )
    assert source == "energy-charts"
    assert prices == {datetime(2026, 8, 30, 22, 0, tzinfo=UTC): 0.1}


async def test_fallback_answers_when_entsoe_has_no_matching_data() -> None:
    """ENTSO-E holding nothing for the window is an outage of its data, not of
    the key, so the keyless source answers it."""
    body = json.dumps({"unix_seconds": [1788127200], "price": [100.0]})
    session = _FakeSession(entsoe=_ack("No matching data found"), fallback_body=body)
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
        datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
    )
    assert source == "energy-charts"
    assert prices == {datetime(2026, 8, 30, 22, 0, tzinfo=UTC): 0.1}


async def test_fallback_never_masks_a_rejected_key() -> None:
    """EntsoeAuthError must propagate. A rejected or exhausted token has to
    keep raising its Repairs card: answering from a keyless source instead is
    how an entry runs for months on a credential nobody renews."""
    session = _FakeSession(entsoe=EntsoeAuthError("rejected"), fallback_body="{}")
    with pytest.raises(EntsoeAuthError):
        await fetch_day_ahead_or_fallback(
            "key",
            session,  # type: ignore[arg-type]
            datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
            datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        )


# 2026-04-30 and 2026-05-01 in Brussels; _doc's period covers the first only.
_TODAY_START = datetime(2026, 4, 29, 22, 0, tzinfo=UTC)
_TOMORROW_START = datetime(2026, 4, 30, 22, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 5, 1, 22, 0, tzinfo=UTC)


def _today_only_doc() -> str:
    return _doc(
        "".join(
            f"<Point><position>{i}</position><price.amount>50</price.amount></Point>"
            for i in range(1, 25)
        )
    )


def _tomorrow_body() -> str:
    # The last hour of today rides along to show it cannot displace ENTSO-E.
    stamps = [_TOMORROW_START - timedelta(hours=1)] + [
        _TOMORROW_START + timedelta(hours=h) for h in range(24)
    ]
    return json.dumps(
        {
            "unix_seconds": [int(t.timestamp()) for t in stamps],
            "price": [999.0] + [100.0] * 24,
        }
    )


async def test_fallback_fills_a_day_entsoe_has_not_published() -> None:
    """ENTSO-E answering for today but not yet for tomorrow is a success, so
    the outage path never ran and tomorrow stayed empty until ENTSO-E caught
    up, though energy-charts already carried it. The missing day is asked
    for alone and the hours ENTSO-E served keep the source of record."""
    session = _FakeSession(entsoe=_today_only_doc(), fallback_body=_tomorrow_body())
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        _TODAY_START,
        _WINDOW_END,
    )
    assert source == "entsoe+energy-charts"
    assert session.fallback_params == [
        {"bzn": "BE", "start": "2026-05-01", "end": "2026-05-01"}
    ]
    assert len(prices) == 48
    assert prices[_TOMORROW_START - timedelta(hours=1)] == pytest.approx(0.05)
    assert prices[_TOMORROW_START] == pytest.approx(0.1)
    assert list(prices) == sorted(prices)


async def test_a_failed_fill_keeps_the_entsoe_answer() -> None:
    """Before the auction is out energy-charts has nothing either. That is no
    error: ENTSO-E's today stands as it was and the next tick asks again."""
    session = _FakeSession(
        entsoe=_today_only_doc(), fallback_body="no content available"
    )
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        _TODAY_START,
        _WINDOW_END,
    )
    assert source == "entsoe"
    assert len(prices) == 24
    assert len(session.fallback_params) == 1
    assert session.smard_files == ["index_quarterhour.json"]


async def test_a_complete_entsoe_answer_never_asks_the_fallback() -> None:
    """Every day of the window answered: energy-charts is rate limited per IP
    and shared by every entry on the host, so it is not asked."""
    session = _FakeSession(entsoe=_today_only_doc(), fallback_body="{}")
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        _TODAY_START,
        _TOMORROW_START,
    )
    assert source == "entsoe"
    assert len(prices) == 24
    assert session.fallback_params == []
    assert session.smard_files == []


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


# Mondays at Brussels midnight, which is where SMARD starts its week files.
_WEEK_APR27 = _ms(datetime(2026, 4, 26, 22, 0, tzinfo=UTC))
_WEEK_MAY04 = _ms(datetime(2026, 5, 3, 22, 0, tzinfo=UTC))


def _smard(weeks: Mapping[int, Mapping[datetime, float | None]]) -> dict[str, str]:
    """An index of ``weeks`` and one file per week, as SMARD serves them."""
    files = {
        "index_quarterhour.json": json.dumps({"timestamps": sorted(weeks)}),
    }
    for week, points in weeks.items():
        files[f"4996_DE_quarterhour_{week}.json"] = json.dumps(
            {"meta_data": {}, "series": [[_ms(t), v] for t, v in points.items()]}
        )
    return files


def _quarters(start: datetime, hours: int, price: float) -> dict[datetime, float]:
    return {start + timedelta(minutes=15 * q): price for q in range(hours * 4)}


def test_smard_groups_to_the_hour_before_the_15_minute_mtu() -> None:
    """Before 2025-10-01 SMARD's quarter-hour file repeats the hour's price
    four times, where energy-charts publishes the hour itself. Kept as
    quarters, a quarter-hourly entry's replay of those months would carry
    four slots per hour that no auction ever cleared."""
    hourly_era = datetime(2025, 9, 30, 21, 0, tzinfo=UTC)
    quarter_era = datetime(2025, 9, 30, 22, 0, tzinfo=UTC)
    points: list[tuple[object, object]] = [
        (_ms(hourly_era + timedelta(minutes=15 * q)), 100.0) for q in range(4)
    ]
    points += [
        (_ms(quarter_era + timedelta(minutes=15 * q)), p)
        for q, p in enumerate([40.0, 80.0, None, 120.0])
    ]
    out = _bucket_slots(
        points,
        hourly_era,
        quarter_era + timedelta(hours=1),
        True,
        millis=True,
        quarters_from=SMARD_QUARTERS_FROM,
    )
    assert out == {
        hourly_era: 0.1,
        quarter_era: 0.04,
        quarter_era + timedelta(minutes=15): 0.08,
        quarter_era + timedelta(minutes=45): 0.12,
    }


async def test_smard_reads_only_the_weeks_the_window_touches() -> None:
    """A window across Sunday night needs the two week files around it and
    nothing else: a year's index lists about 600 of them."""
    sunday = datetime(2026, 5, 2, 22, 0, tzinfo=UTC)
    files = _smard(
        {
            _ms(datetime(2026, 4, 19, 22, 0, tzinfo=UTC)): {},
            _WEEK_APR27: _quarters(sunday, 24, 50.0),
            _WEEK_MAY04: _quarters(sunday + timedelta(days=1), 24, 70.0),
        }
    )
    session = _FakeSession(entsoe=EntsoeError("503"), fallback_body="", smard=files)
    prices = await SmardClient(session).fetch_day_ahead(  # type: ignore[arg-type]
        sunday, sunday + timedelta(days=2)
    )
    assert session.smard_files == [
        "index_quarterhour.json",
        f"4996_DE_quarterhour_{_WEEK_APR27}.json",
        f"4996_DE_quarterhour_{_WEEK_MAY04}.json",
    ]
    assert len(prices) == 48
    assert prices[sunday] == pytest.approx(0.05)
    assert prices[sunday + timedelta(days=1)] == pytest.approx(0.07)


async def test_smard_fills_a_day_energy_charts_has_not_published() -> None:
    """The second keyless source is asked for the days the first left empty,
    and only those: ENTSO-E keeps today. A slot SMARD has not published yet
    is null and stays absent rather than priced at zero."""
    tomorrow: dict[datetime, float | None] = dict(_quarters(_TOMORROW_START, 24, 100.0))
    tomorrow[_TOMORROW_START + timedelta(hours=23, minutes=45)] = None
    session = _FakeSession(
        entsoe=_today_only_doc(),
        fallback_body="no content available",
        smard=_smard({_WEEK_APR27: tomorrow}),
    )
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        _TODAY_START,
        _WINDOW_END,
    )
    assert source == "entsoe+smard"
    assert len(prices) == 48
    assert prices[_TODAY_START] == pytest.approx(0.05)
    assert prices[_TOMORROW_START] == pytest.approx(0.1)


async def test_smard_answers_an_outage_energy_charts_cannot() -> None:
    session = _FakeSession(
        entsoe=EntsoeError("503"),
        fallback_body="no content available",
        smard=_smard({_WEEK_APR27: _quarters(_TODAY_START, 24, 100.0)}),
    )
    prices, source = await fetch_day_ahead_or_fallback(
        "key",
        session,  # type: ignore[arg-type]
        _TODAY_START,
        _TOMORROW_START,
    )
    assert source == "smard"
    assert len(prices) == 24


async def test_an_outage_of_every_source_names_each_cause() -> None:
    """Each failure is in the message: which keyless source broke is what the
    reader of last_error needs next."""
    session = _FakeSession(entsoe=EntsoeError("503"), fallback_body="nope")
    with pytest.raises(EntsoeError) as excinfo:
        await fetch_day_ahead_or_fallback(
            "key",
            session,  # type: ignore[arg-type]
            _TODAY_START,
            _TOMORROW_START,
        )
    msg = str(excinfo.value)
    assert msg.startswith("ENTSO-E: ")
    assert "energy-charts: non-JSON" in msg
    assert "smard HTTP 404" in msg


def test_key_validation_does_not_use_the_fallback() -> None:
    """The config flow's key check must keep calling EntsoeClient directly.

    Its whole job is to tell an invalid key from an unreachable server. A
    fallback answering for it would report a dead token as working and let a
    user finalise an entry whose every refresh then fails -- the failure would
    surface days later as a Repairs card nobody connects to setup.

    Asserted on the source rather than by driving the flow because what is
    under test is which client the function reaches for, and a behavioural
    test would pass just as well if someone swapped it for the wrapper and
    ENTSO-E happened to be up.
    """
    source = pathlib.Path(
        "custom_components/be_electricity_prices/flow_schemas.py"
    ).read_text()
    start = source.index("async def _validate_entsoe_key")
    body = source[start : source.index("\ndef ", start)]
    assert "EntsoeClient(" in body
    assert "fetch_day_ahead_or_fallback" not in body


async def test_double_failure_names_both_causes() -> None:
    """When the fallback fails too, the ENTSO-E half is the half that explains
    the outage. Reporting only the fallback's complaint for a day ENTSO-E
    spent returning 503 sends whoever reads last_error after the wrong
    service."""
    session = _FakeSession(
        entsoe=EntsoeError("x"), fallback_body="end must be >= start"
    )
    with pytest.raises(EntsoeError) as excinfo:
        await fetch_day_ahead_or_fallback(
            "key",
            session,  # type: ignore[arg-type]
            datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
            datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        )
    msg = str(excinfo.value)
    assert "ENTSO-E" in msg and "fallback" in msg


async def test_empty_fallback_window_still_names_the_entsoe_cause() -> None:
    """Same reasoning for a fallback that answers cleanly with nothing."""
    session = _FakeSession(
        entsoe=EntsoeError("x"), fallback_body='{"unix_seconds": [], "price": []}'
    )
    with pytest.raises(EntsoeError, match="ENTSO-E"):
        await fetch_day_ahead_or_fallback(
            "key",
            session,  # type: ignore[arg-type]
            datetime(2026, 8, 30, 22, 0, tzinfo=UTC),
            datetime(2026, 8, 31, 22, 0, tzinfo=UTC),
        )


def test_retry_after_reads_the_header_and_refuses_a_wild_one() -> None:
    """The endpoint sends an integer count of seconds (measured: 29).

    Everything else takes the fallback wait rather than zero, which would
    retry straight back into the limit, and the whole thing is capped: the
    header is a hint from a server we do not control, and a units mix-up
    would otherwise park the keyless source for the rest of the year.
    """
    assert _retry_after("29") == timedelta(seconds=29)
    assert _retry_after(" 29 ") == timedelta(seconds=29)
    # Absent, an HTTP-date, nonsense, or a value that cannot be waited.
    assert _retry_after(None) == timedelta(seconds=60)
    assert _retry_after("Wed, 02 Sep 2026 17:06:41 GMT") == timedelta(seconds=60)
    assert _retry_after("") == timedelta(seconds=60)
    assert _retry_after("0") == timedelta(seconds=60)
    assert _retry_after("-5") == timedelta(seconds=60)
    assert _retry_after("86400") == timedelta(minutes=15)


async def test_a_429_stops_the_next_request_being_sent_at_all() -> None:
    """The endpoint asks clients to honour Retry-After rather than assume a
    fixed rate, because it tunes the limit down under load. A request sent
    inside that window cannot succeed and is itself what the server counts,
    so the cooldown is enforced before the socket, not after the response."""

    class _Limited:
        def __init__(self) -> None:
            self.requests = 0

        def get(self, _url: str, **_: object) -> _CM:
            self.requests += 1
            return _CM(resp=_Resp(429, "Too Many Requests", {"Retry-After": "29"}))

    session = _Limited()
    client = EnergyChartsClient(session)  # type: ignore[arg-type]
    start = datetime(2026, 8, 30, 22, 0, tzinfo=UTC)
    end = datetime(2026, 8, 31, 22, 0, tzinfo=UTC)

    with pytest.raises(EntsoeError, match="429"):
        await client.fetch_day_ahead(start, end)
    assert session.requests == 1

    # A second entry on the same host shares the bucket, so it shares the
    # cooldown: a fresh client must not walk into the 429 the first one earned.
    with pytest.raises(EntsoeError, match="rate limited"):
        await EnergyChartsClient(session).fetch_day_ahead(start, end)  # type: ignore[arg-type]
    assert session.requests == 1, "the held request must never reach the wire"

    # And it lifts on its own once the window the server named has passed.
    api._energy_charts_retry_at = datetime(2020, 1, 1, tzinfo=UTC)
    with pytest.raises(EntsoeError, match="429"):
        await client.fetch_day_ahead(start, end)
    assert session.requests == 2
