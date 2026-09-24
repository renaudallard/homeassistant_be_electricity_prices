"""The CREG's home charging reimbursement rate, and the sensor that carries it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from homeassistant.util import dt as dt_util

from custom_components.be_electricity_prices import creg_ev
from custom_components.be_electricity_prices.const import (
    REGION_BRUSSELS,
    REGION_FLANDERS,
    REGION_WALLONIA,
)
from custom_components.be_electricity_prices.coordinator_data import CoordinatorData
from custom_components.be_electricity_prices.sensor import EV_RATE_SENSORS

_FIXTURE = Path(__file__).parent / "fixtures" / "creg_tariff_ev.csv"


def _csv() -> str:
    return _FIXTURE.read_text(encoding="utf-8-sig")


@pytest.fixture(autouse=True)
def _clear_creg_cache() -> Iterator[None]:
    """The module keeps one table for the life of the process.

    The lock is cleared with it, for the reason the Brugel tests give: an
    uncontended ``asyncio.Lock`` binds no loop, so one left behind fails the
    next test that contends it, and nothing before.
    """
    creg_ev._table.clear()
    creg_ev._fetched_quarter = None
    creg_ev._failed_at = None
    creg_ev._lock = None
    yield
    creg_ev._table.clear()
    creg_ev._fetched_quarter = None
    creg_ev._failed_at = None
    creg_ev._lock = None


def test_the_published_file_reads_as_the_page_prints_it() -> None:
    """The page prints Q4/2026 as 32,25 / 36,88 / 37,79 and Q3 as 32,22 / 37,19 / 37,83."""
    table = creg_ev.parse(_csv())
    q4 = date(2026, 10, 1)
    q3 = date(2026, 7, 1)
    assert table[REGION_FLANDERS][q4] == pytest.approx(0.3225)
    assert table[REGION_BRUSSELS][q4] == pytest.approx(0.3688)
    assert table[REGION_WALLONIA][q4] == pytest.approx(0.3779)
    assert table[REGION_WALLONIA][q3] == pytest.approx(0.3783)
    # Q1/2025, the first quarter the tolerance covered: August to October 2024.
    assert table[REGION_WALLONIA][date(2025, 1, 1)] == pytest.approx(0.3256)


def test_every_quarter_is_the_figure_the_circular_prints() -> None:
    """The SPF's own figures, from circular 2024/C/77 and its addenda.

    They are what an employer is held to, and the SPF computes them from the
    monthly prices. The CREG's mean column rounds differently and says 36,18
    for Wallonia in Q2/2025, where the circular says 36,17.
    """
    circular = {
        date(2025, 1, 1): (28.22, 32.94, 32.56),
        date(2025, 4, 1): (31.94, 35.85, 36.17),
        date(2025, 7, 1): (34.56, 37.87, 38.43),
        date(2025, 10, 1): (30.70, 33.56, 34.57),
        date(2026, 1, 1): (31.32, 34.26, 35.23),
        date(2026, 4, 1): (31.91, 35.55, 36.36),
        date(2026, 7, 1): (32.22, 37.19, 37.83),
    }
    table = creg_ev.parse(_csv())
    for quarter, cents in circular.items():
        for region, figure in zip(
            (REGION_FLANDERS, REGION_BRUSSELS, REGION_WALLONIA), cents, strict=True
        ):
            assert table[region][quarter] == round(figure / 100, 6), (
                region,
                quarter,
            )


def test_a_quarter_is_priced_off_the_three_months_before_it() -> None:
    """The rate of Q4/2026 is the mean of May, June and July.

    The three-month lead is what makes the rate known before its quarter
    starts; read a month as the rate of its own quarter and every quarter
    would be billed at the one before it.
    """
    table = creg_ev.parse(_csv())
    # May, June and July 2026 for Wallonia, as the file prints them.
    assert table[REGION_WALLONIA][date(2026, 10, 1)] == pytest.approx(
        (36.53 + 37.74 + 39.1) / 3 / 100, abs=5e-5
    )
    # And nothing lands on a month that starts no quarter.
    for rows in table.values():
        assert all(start.month in (1, 4, 7, 10) for start in rows)


def test_the_mean_column_is_not_read() -> None:
    """Blanking the CREG's means changes nothing: the monthly prices decide."""
    lines = _csv().splitlines()
    blanked = []
    for line in lines:
        cells = line.split(";")
        if len(cells) == 8 and cells[0].isdigit():
            cells[3] = cells[5] = cells[7] = ""
        blanked.append(";".join(cells))
    assert creg_ev.parse("\r\n".join(blanked)) == creg_ev.parse(_csv())


def test_a_header_a_blank_line_and_a_footnote_cost_nothing() -> None:
    text = (
        "﻿Year;Month;a;b;c;d;e;f\r\n"
        "\r\n"
        "2026;7;33,65;32,25;38,22;36,88;39,1;37,79\r\n"
        "2026;6;32,17;;36,86;;37,74;\r\n"
        "2026;5;30,93;;35,57;;36,53;\r\n"
        "Source: CREG\r\n"
    )
    table = creg_ev.parse(text)
    assert table == {
        REGION_FLANDERS: {date(2026, 10, 1): 0.3225},
        REGION_BRUSSELS: {date(2026, 10, 1): 0.3688},
        REGION_WALLONIA: {date(2026, 10, 1): 0.3779},
    }


def test_a_figure_in_the_wrong_unit_is_dropped_and_the_rest_kept() -> None:
    """One misread cell must not empty the table: 377,4 is EUR/MWh, not cents."""
    text = (
        "2026;7;33,65;32,25;38,22;36,88;39,1;37,79\r\n"
        "2026;6;32,17;;36,86;;377,4;\r\n"
        "2026;5;30,93;;35,57;;36,53;\r\n"
    )
    table = creg_ev.parse(text)
    assert REGION_WALLONIA not in table
    assert table[REGION_FLANDERS] == {date(2026, 10, 1): 0.3225}


def test_a_quarter_missing_a_month_is_not_priced() -> None:
    """Two months of three are not the SPF's figure, whatever the mean says."""
    text = (
        "2026;7;33,65;32,25;38,22;36,88;39,1;37,79\r\n2026;5;30,93;;35,57;;36,53;\r\n"
    )
    assert creg_ev.parse(text) == {}


def test_the_rate_for_a_day_is_the_rate_of_its_quarter() -> None:
    creg_ev._table.update(creg_ev.parse(_csv()))
    assert creg_ev.rate_for(REGION_WALLONIA, date(2026, 9, 23)) == pytest.approx(0.3783)
    assert creg_ev.rate_for(REGION_WALLONIA, date(2026, 10, 1)) == pytest.approx(0.3779)
    assert creg_ev.rate_for(REGION_WALLONIA, date(2026, 12, 31)) == pytest.approx(
        0.3779
    )
    assert creg_ev.rate_for(REGION_WALLONIA, date(2027, 1, 1)) is None
    assert creg_ev.rate_for("elsewhere", date(2026, 9, 23)) is None


def test_the_history_is_oldest_first() -> None:
    creg_ev._table.update(creg_ev.parse(_csv()))
    starts = [start for start, _ in creg_ev.history(REGION_FLANDERS)]
    assert starts == sorted(starts)
    assert starts[0] == date(2025, 1, 1)


class _Response:
    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_Response":
        return self

    async def __aexit__(self, *_a: object) -> bool:
        return False


class _Session:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self._status = status
        self.calls = 0

    def get(self, *_a: object, **_k: object) -> _Response:
        self.calls += 1
        return _Response(self._body, self._status)


@pytest.mark.parametrize(
    ("label", "body", "status"),
    [
        ("a maintenance page answered with 200", b"<html>maintenance</html>", 200),
        ("an empty body", b"", 200),
        ("a missing file", b"", 404),
        ("bytes that are not text", b"\xff\xfe\x00\x01", 200),
    ],
)
async def test_a_body_that_is_not_the_file_never_raises(
    label: str, body: bytes, status: int
) -> None:
    """The fetch runs inside the coordinator tick; nothing may escape it."""
    session = _Session(body, status)
    assert await creg_ev.ensure_rates(session, date(2026, 9, 23)) is False, label  # type: ignore[arg-type]
    assert creg_ev.rate_for(REGION_WALLONIA, date(2026, 9, 23)) is None


async def test_a_failure_is_only_attempted_once() -> None:
    session = _Session(b"<html>nope</html>")
    await creg_ev.ensure_rates(session, date(2026, 9, 23))  # type: ignore[arg-type]
    assert creg_ev._failed_at is not None
    after_first = session.calls
    assert after_first == 1
    await creg_ev.ensure_rates(session, date(2026, 9, 23))  # type: ignore[arg-type]
    assert session.calls == after_first, "the backoff did not hold"


async def test_a_good_file_is_fetched_once_a_quarter_and_kept() -> None:
    """The file gains a row four times a year; a download an hour buys nothing."""
    session = _Session(_FIXTURE.read_bytes())
    assert await creg_ev.ensure_rates(session, date(2026, 9, 23)) is True  # type: ignore[arg-type]
    assert session.calls == 1
    assert await creg_ev.ensure_rates(session, date(2026, 9, 30)) is True  # type: ignore[arg-type]
    assert session.calls == 1, "a cached quarter was fetched twice"
    # The next quarter asks again, once.
    assert await creg_ev.ensure_rates(session, date(2026, 10, 1)) is True  # type: ignore[arg-type]
    assert session.calls == 2
    assert creg_ev.rate_for(REGION_BRUSSELS, date(2026, 10, 1)) == pytest.approx(0.3688)


async def test_a_failed_refetch_keeps_last_quarters_table() -> None:
    """A table that still answers is better than an empty one."""
    good = _Session(_FIXTURE.read_bytes())
    await creg_ev.ensure_rates(good, date(2026, 9, 23))  # type: ignore[arg-type]
    bad = _Session(b"<html>down</html>")
    assert await creg_ev.ensure_rates(bad, date(2026, 10, 1)) is True  # type: ignore[arg-type]
    assert creg_ev.rate_for(REGION_WALLONIA, date(2026, 10, 1)) == pytest.approx(0.3779)
    # And the table is asked for again once the backoff has passed, not before.
    assert creg_ev._failed_at is not None
    assert (dt_util.utcnow() - creg_ev._failed_at).total_seconds() < 5


def test_the_sensor_reads_the_rate_and_is_unavailable_without_one() -> None:
    (desc,) = EV_RATE_SENSORS
    assert desc.key == "ev_home_charging_rate"
    assert desc.native_unit_of_measurement == "EUR/kWh"
    assert desc.unavailable_when_none
    assert desc.value_fn(CoordinatorData()) is None
    assert (
        desc.value_fn(CoordinatorData(ev_home_charging_rate_eur_per_kwh=0.3783))
        == 0.3783
    )


def test_the_rate_is_fetched_by_the_tick_and_read_into_the_record() -> None:
    """Held on the source: the tick fetches, then reads the cache synchronously."""
    import inspect

    from custom_components.be_electricity_prices.coordinator import BePricesCoordinator

    body = inspect.getsource(BePricesCoordinator._update_body)
    assert body.index("ensure_ev_rates(") < body.index("ev_rate_for("), (
        "the rate must be fetched before the record reads it, "
        "or the tick that fetched the file publishes nothing"
    )
