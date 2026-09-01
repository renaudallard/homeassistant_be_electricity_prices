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
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.be_electricity_prices.api import (
    EntsoeAuthError,
    EntsoeClient,
    EntsoeError,
    _parse_iso_utc,
    _MAX_PERIOD_SLOTS,
    _parse_energy_charts,
    fetch_day_ahead_or_fallback,
    parse_day_ahead_xml,
)


def test_acknowledgement_document_raises_auth_error() -> None:
    """A rejected or quota-exhausted token returns HTTP 200 + an
    Acknowledgement_MarketDocument. The parser must surface it as an auth
    error so the dynamic table isn't silently blanked."""
    ack = """<?xml version="1.0" encoding="UTF-8"?>
<Acknowledgement_MarketDocument xmlns="urn:iec62325.351:tc57wg16:451-1:acknowledgementdocument:7:0">
  <Reason>
    <code>999</code>
    <text>No matching data found</text>
  </Reason>
</Acknowledgement_MarketDocument>
"""
    with pytest.raises(EntsoeAuthError, match="No matching data found"):
        parse_day_ahead_xml(ack)


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
        assert list(parse_day_ahead_xml(doc).values())[0] == pytest.approx(want)


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
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


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

    def __init__(self, entsoe: Exception, fallback_body: str) -> None:
        self._entsoe = entsoe
        self._fallback_body = fallback_body

    def get(self, url: str, **_: object) -> _CM:
        if "entsoe" in url:
            if isinstance(self._entsoe, EntsoeAuthError):
                # A rejected token is an HTTP 401 on the wire.
                return _CM(resp=_Resp(401, ""))
            return _CM(exc=aiohttp.ClientError("503 Service Unavailable"))
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
