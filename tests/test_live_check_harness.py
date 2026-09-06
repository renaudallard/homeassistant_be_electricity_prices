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

"""Failure containment in the live-check harness itself.

The harness runs every supplier concurrently under a per-supplier
wallclock cap. What it must guarantee is that ONE supplier going wrong
costs exactly one supplier's rows: the report still prints, and the other
suppliers still get checked. On 2026-08-03 that guarantee broke and the
whole run died with no output, so the auto-filed issue carried an empty
body and the actual failures were only visible in the run log.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime
from types import SimpleNamespace
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path

import aiohttp
import pytest

from custom_components.be_electricity_prices.providers.base import SpotMonthlyRates

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# scripts/ is not a package, so it is added to sys.path above rather than
# imported by dotted path; mypy cannot follow that.
import live_check as lc  # type: ignore[import-not-found]  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_checks() -> None:
    lc.CHECKS.clear()


def _rows(prefix: str) -> list[lc.Check]:
    return [c for c in lc.CHECKS if c.label.startswith(prefix)]


async def test_escaping_cancellederror_is_contained() -> None:
    """The exact shape of the 2026-08-03 crash.

    wait_for cancels the inner coroutine when the cap expires and normally
    converts that to TimeoutError. It only does so when its own cancel was
    the sole one outstanding; when the coroutine is parked on something
    uncancellable (the PDF parse runs under asyncio.to_thread, and a
    thread cannot be interrupted) a bare CancelledError can surface
    instead. CancelledError is a BaseException, so an `except Exception`
    clause never sees it: it escaped the gather and killed the run before
    the report was printed, and the auto-filed issue carried an empty body.

    Asserted at the contract level rather than by racing a real thread,
    because whether wait_for converts depends on cancellation bookkeeping
    that is not reliably reproducible in a unit test.
    """

    async def _cancelled_supplier() -> None:
        raise asyncio.CancelledError

    async def _healthy_supplier() -> None:
        lc._record("healthy: fine", True)

    # Must not raise: the whole point is that the run survives.
    await asyncio.gather(
        lc._attributed_check("slow", _cancelled_supplier),
        lc._attributed_check("healthy", _healthy_supplier),
    )

    timed_out = _rows("slow:")
    assert timed_out, "the overrunning supplier recorded no row at all"
    assert all(not c.ok for c in timed_out)
    assert "hard timeout" in timed_out[0].label
    # The healthy supplier still ran and still reported.
    assert [c.ok for c in _rows("healthy:")] == [True]


async def test_uncancellable_overrun_records_a_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplier overrunning the cap inside an uncancellable parse costs
    exactly one row, however the cancellation surfaces."""
    monkeypatch.setattr(lc, "_SUPPLIER_HARD_TIMEOUT_S", 0.05)

    def _blocking_parse() -> None:
        # Stands in for extract_pdf_text_layout: a real thread, which
        # cancellation cannot interrupt.
        import time

        time.sleep(0.3)

    async def _slow_supplier() -> None:
        await asyncio.to_thread(_blocking_parse)

    await asyncio.gather(lc._attributed_check("slow", _slow_supplier))
    rows = _rows("slow:")
    assert rows and not rows[0].ok
    assert "hard timeout" in rows[0].label


async def test_ordinary_exception_is_still_contained(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _broken() -> None:
        raise RuntimeError("card layout changed")

    async def _healthy() -> None:
        lc._record("healthy: fine", True)

    await asyncio.gather(
        lc._attributed_check("broken", _broken),
        lc._attributed_check("healthy", _healthy),
    )
    rows = _rows("broken:")
    assert rows and not rows[0].ok
    assert "card layout changed" in rows[0].detail
    assert [c.ok for c in _rows("healthy:")] == [True]


async def test_external_cancellation_still_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swallowing our own timeout must not swallow a real teardown.

    If the run is being cancelled from outside, the CancelledError has to
    keep travelling or the harness would ignore a shutdown.
    """
    monkeypatch.setattr(lc, "_SUPPLIER_HARD_TIMEOUT_S", 30.0)
    started = asyncio.Event()

    async def _long_supplier() -> None:
        started.set()
        await asyncio.sleep(30)

    task = asyncio.create_task(lc._attributed_check("outer", _long_supplier))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_every_latency_budget_stays_under_the_hard_cap() -> None:
    """A budget at or above the cap can never fire: the supplier is killed
    before it can report the drift the budget exists to catch."""
    cap = lc._SUPPLIER_HARD_TIMEOUT_S
    over = {
        supplier: budget
        for supplier, budget in lc._LATENCY_BUDGET_OVERRIDES.items()
        if budget >= cap
    }
    assert not over, f"latency budgets at/above the {cap}s hard cap: {over}"
    assert lc.LATENCY_WARN_THRESHOLD_S < cap


def _blown(supplier: str) -> dict[str, dict[str, float]]:
    """Metrics that blow both budgets for one supplier."""
    return {
        supplier: {
            "fetches": 6.0,
            "elapsed_s": lc._latency_budget(supplier) + 10.0,
            "bytes": float(lc._bytes_budget(supplier) + 1_000_000),
            "failed": 0.0,
            "failed_s": 0.0,
        }
    }


def test_drift_reports_a_blown_budget_when_the_extractor_passed() -> None:
    warnings = lc._drift_warnings(_blown("ecofix"), frozenset())
    assert len(warnings) == 2
    assert any("fetch time" in w for w in warnings)
    assert any("bytes" in w for w in warnings)


def test_drift_is_skipped_for_a_supplier_whose_extractor_failed() -> None:
    """The extractor failure is the louder signal and the likely cause of
    the numbers, so drift must not split it into a second issue."""
    assert lc._drift_warnings(_blown("ecofix"), frozenset({"ecofix"})) == []


def test_drift_still_fires_for_a_supplier_that_passed() -> None:
    """A failure at one supplier must not silence drift at another."""
    warnings = lc._drift_warnings(_blown("eneco"), frozenset({"ecofix"}))
    assert len(warnings) == 2


def test_an_unreadable_card_is_recorded_as_expected() -> None:
    """A card with no text layer is a real failure but not a regression, so
    _record must mark it expected.

    Classification comes from the exception type the fetch sites already
    write into the detail, not from a per-supplier list: the previous
    attempt at this was a `cards_unreadable` registry flag, which froze one
    month of observation into source and kept claiming a supplier was
    unreadable after it went back to publishing text.
    """
    lc.CHECKS.clear()
    lc._record(
        "ecofix/ecofix_motion/flanders: fetch",
        False,
        "CardNotReadableError: card has no text layer: 172 characters "
        "across 5 page(s), so it is published as page images",
    )
    lc._record(
        "eneco/power_fix/wallonia: fetch",
        False,
        "ExtractorError: could not parse Eneco fixed energy block",
    )
    unreadable, regression = lc.CHECKS
    assert unreadable.expected is True
    assert regression.expected is False
    # Both still count as failures; only the gating differs.
    assert unreadable.ok is False
    lc.CHECKS.clear()


def test_an_unreadable_card_alone_does_not_fail_the_run() -> None:
    """The whole point: Ecofix alone must not set the extractor bit.

    Before this, one permanently-unreadable supplier failed every run,
    exhausted the workflow's 7-attempt retry loop, and refiled a fresh issue
    each time the previous one was closed (#53, #56, #58 all carried the
    same six Ecofix rows).
    """
    expected_only = [
        lc.Check(label="ecofix/a/flanders: fetch", ok=False, expected=True),
        lc.Check(label="eneco/b/wallonia: fetch", ok=True),
    ]
    assert lc._extractor_regressions(expected_only) == []

    # A genuine failure alongside it must still gate.
    genuine = lc.Check(label="bolt/c/flanders: fetch", ok=False)
    assert lc._extractor_regressions([*expected_only, genuine]) == [genuine]


def test_the_report_separates_expected_failures_from_regressions() -> None:
    """A run reading "0 fail" while the table lists failing rows looks like
    a harness bug, so the headline and the tables have to distinguish."""
    report = lc._render_report(
        [
            lc.Check(
                label="ecofix/a/flanders: fetch",
                ok=False,
                expected=True,
                detail="CardNotReadableError: card has no text layer",
            ),
            lc.Check(
                label="eneco/b/wallonia: fetch",
                ok=False,
                detail="ExtractorError: regex miss",
            ),
            lc.Check(label="bolt/c/flanders: energy", ok=True),
        ]
    )
    assert "1 pass, 1 fail, 1 unreadable (expected)" in report
    assert "## Failures" in report
    assert "## Unreadable cards (expected, not a regression)" in report
    # The regression must not be filed under the expected table.
    failures_block = report.split("## Failures")[1].split("##")[0]
    assert "eneco/b/wallonia" in failures_block
    assert "ecofix/a/flanders" not in failures_block


def test_failed_suppliers_reads_the_supplier_off_the_label() -> None:
    checks = [
        lc.Check("ecofix/ecofix_motion/flanders: fetch", False, "boom"),
        lc.Check("eneco/power_fix: publication label", True),
        lc.Check("totalenergies: hard timeout", False, "exceeded 600s"),
    ]
    assert lc._failed_suppliers(checks) == frozenset({"ecofix", "totalenergies"})


def test_failure_labels_carry_only_the_regressions(tmp_path: Path) -> None:
    """The workflow intersects this file across its retry attempts, so an
    unreadable card in it would intersect with itself and refile an issue
    every day - the noise the expected flag exists to drop."""
    checks = [
        lc.Check(
            label="ecofix/ecofix_motion/flanders: fetch",
            ok=False,
            detail="CardNotReadableError: card has no text layer",
            expected=True,
        ),
        lc.Check(
            label="eneco/power_flex: fetch",
            ok=False,
            detail="ExtractorError: network error fetching: TimeoutError",
        ),
        lc.Check(label="bolt/bolt_online/flanders: energy", ok=True),
    ]
    path = tmp_path / "extractor_failures.txt"
    lc._write_failure_labels(path, lc._extractor_regressions(checks))
    assert path.read_text() == "eneco/power_flex: fetch\n"


def test_failure_labels_are_sorted_for_comm(tmp_path: Path) -> None:
    """comm -12 in the workflow needs both sides sorted, and it re-sorts
    under LC_ALL=C; emit the same order here so the two agree."""
    checks = [
        lc.Check(label="mega/mega_smart_fixed: fetch", ok=False, detail="boom"),
        lc.Check(label="bolt/bolt_online: fetch", ok=False, detail="boom"),
        lc.Check(label="engie/engie_dynamic: fetch", ok=False, detail="boom"),
    ]
    path = tmp_path / "extractor_failures.txt"
    lc._write_failure_labels(path, checks)
    assert path.read_text().splitlines() == [
        "bolt/bolt_online: fetch",
        "engie/engie_dynamic: fetch",
        "mega/mega_smart_fixed: fetch",
    ]


def test_no_failures_writes_an_empty_file(tmp_path: Path) -> None:
    """An empty intersection is how the loop decides not to file, so a
    green attempt must leave an empty file rather than no file at all."""
    path = tmp_path / "extractor_failures.txt"
    lc._write_failure_labels(path, [])
    assert path.exists()
    assert path.read_text() == ""


_GBS_PAGE = """
<a href="https://cdn.example/202607_gbs_tariefkaart.pdf?x=1">July</a>
<a href="https://cdn.example/202608_gbs_inschatting_tariefkaart_ecopower.pdf?x=1">Aug preview</a>
"""

_VERSIONED_PAGE = """
<a href="https://f.example/pricelists/var/bolt_res_el_fr_9.pdf">old</a>
<a href="https://f.example/pricelists/var/bolt_res_el_fr_13.pdf">current</a>
"""

_VERSION_RE = r"pricelists/var/bolt_res_el_fr_(\w+)\.pdf"
_GBS_RE = r"/(\d{4,8})[a-z]?_gbs_[a-z_]*\.pdf"


_version_key = lc._version_key


def _date_key(stamp: str) -> int:
    return int(stamp.ljust(8, "0"))


def test_freshness_flags_a_superseded_version() -> None:
    """The Bolt shape: the pinned card still exists and still parses, so
    only the listing says it has been superseded."""
    lc._expect_newest_card(
        "bolt/freshness", _VERSIONED_PAGE, _VERSION_RE, "11", key=_version_key
    )
    (row,) = _rows("bolt/freshness")
    assert row.ok is False
    assert "advertises 13" in row.detail


def test_freshness_compares_versions_numerically() -> None:
    """A lexical max would rank "9" above "13" and report a current card
    as stale every time the version crosses a digit boundary."""
    lc._expect_newest_card(
        "bolt/freshness", _VERSIONED_PAGE, _VERSION_RE, "13", key=_version_key
    )
    assert _rows("bolt/freshness")[0].ok is True


def test_freshness_ignores_the_inschatting_preview() -> None:
    """Ecopower publishes next month's estimate alongside the definitive
    cards. It is advertised but not billable, so serving July while an
    August preview is up is correct, not stale."""
    lc._expect_newest_card(
        "ecopower/freshness",
        _GBS_PAGE,
        _GBS_RE,
        "202607",
        key=_date_key,
        exclude="inschatting",
    )
    assert _rows("ecopower/freshness")[0].ok is True


def test_freshness_spans_stamp_widths() -> None:
    """The Ecopower shape: a renamed card the extractor's stricter pattern
    cannot see, so it keeps resolving the older one."""
    page = """
    <a href="https://cdn.example/202601_dbs_tariefkaart.pdf">Jan</a>
    <a href="https://cdn.example/20260801_dbs_tariefkaart.pdf">Aug</a>
    """
    pattern = r"/(\d{4,8})[a-z]?_dbs_[a-z_]*\.pdf"
    lc._expect_newest_card("ecopower/dbs", page, pattern, "202601", key=_date_key)
    (row,) = _rows("ecopower/dbs")
    assert row.ok is False
    assert "advertises 20260801" in row.detail


def test_freshness_fails_when_the_version_stops_being_numeric() -> None:
    """The shape change this gate exists to catch. The extractor matches
    \\d+ and cannot resolve `_13b` at all, so it keeps serving `_13` and
    every other check passes. Scoring a non-numeric version LOW ranked it
    below the stale one and let exactly that case through green."""
    page = '<a href="/pricelists/var/bolt_res_el_fr_13b.pdf">renamed</a>'
    lc._expect_newest_card("bolt/freshness", page, _VERSION_RE, "13", key=_version_key)
    (row,) = _rows("bolt/freshness")
    assert row.ok is False
    assert "13b" in row.detail


def test_freshness_only_forgives_a_page_that_will_not_load() -> None:
    """The gate caught bare Exception and recorded a pass, so a renamed
    provider symbol would have read as green for exactly as long as nobody
    looked. Only the provider fetch error is forgiven; anything else has to
    reach the caller and be recorded as a failure."""
    assert lc._EXTRACTOR_ERROR is not Exception
    assert not issubclass(AttributeError, lc._EXTRACTOR_ERROR)
    assert not issubclass(TypeError, lc._EXTRACTOR_ERROR)


def test_freshness_stays_quiet_when_the_page_shape_is_unrecognised() -> None:
    """A redesigned Ecopower page must not read as staleness: there is
    nothing to compare against, and its resolver RAISES when it cannot find
    a card, so the extractor check already covers a real break."""
    lc._expect_newest_card(
        "ecopower/freshness", "<p>no cards here</p>", _GBS_RE, "202607", key=_date_key
    )
    assert _rows("ecopower/freshness")[0].ok is True


def test_freshness_fails_when_a_fallback_supplier_advertises_nothing() -> None:
    """Bolt is the other case. Its resolver swallows the failure and returns
    a hardcoded version, so the card still downloads, still parses, and this
    row is the only thing that can report that a pin is being served."""
    lc._expect_newest_card(
        "bolt/freshness",
        "<p>redesigned</p>",
        _VERSION_RE,
        "13",
        key=_version_key,
        empty_is_ok=False,
    )
    (row,) = _rows("bolt/freshness")
    assert row.ok is False
    assert "fallback" in row.detail


# --- ordering keys ------------------------------------------------------------
#
# None of the eight supplier stamp formats sorts naively, and each breaks at a
# different boundary. Every case below is one a naive max() gets WRONG.


@pytest.mark.parametrize(
    ("key", "older", "newer", "what"),
    [
        pytest.param(
            lc._mega_month_key,
            "122026",
            "012027",
            "MMYYYY month-major",
            id="mega-year-end",
        ),
        pytest.param(
            lc._eneco_issue_key,
            "032_022604",
            "032_012605",
            "volume-major issue",
            id="eneco-reissue",
        ),
        pytest.param(
            lc._month_year_key, "12-2025", "08-2026", "MM-YYYY", id="ebem-numeric"
        ),
        pytest.param(
            lc._month_year_key,
            "december-2025",
            "augustus-2026",
            "month name",
            id="ebem-named",
        ),
        pytest.param(
            lc._frank_month_key,
            "December 2026",
            "April 2027",
            "month word",
            id="frank-year-end",
        ),
        pytest.param(
            lc._mmyy_key, "1226", "0127", "MMYY month-major", id="energyvision-year-end"
        ),
    ],
)
def test_stamp_keys_order_where_a_naive_max_does_not(
    key: Callable[[str], int], older: str, newer: str, what: str
) -> None:
    assert max(older, newer) == older, f"{what}: pick a case naive max gets wrong"
    assert max([older, newer], key=key) == newer


@pytest.mark.parametrize(
    ("key", "stamp", "real"),
    [
        pytest.param(lc._mega_month_key, "2026-09", "012027", id="mega-dashed"),
        pytest.param(
            lc._eneco_issue_key, "032_2026-09", "032_012605", id="eneco-dashed"
        ),
        pytest.param(lc._month_year_key, "2026Q3", "08-2026", id="ebem-quarter"),
        pytest.param(lc._frank_month_key, "Q3 2026", "April 2027", id="frank-quarter"),
        pytest.param(lc._mmyy_key, "0826b", "0127", id="energyvision-letter"),
        pytest.param(lc._version_key, "13b", "13", id="bolt-letter"),
        pytest.param(
            lc._ecopower_date_key, "2026-09", "20260801", id="ecopower-dashed"
        ),
    ],
)
def test_an_unreadable_stamp_sorts_above_every_real_one(
    key: Callable[[str], int], stamp: str, real: str
) -> None:
    """A shape the extractor cannot resolve must become the NEWEST advertised
    so the comparison fails. Scoring it low is what let Bolt's filename-shape
    change pass green. ``real`` is a stamp this key genuinely understands."""
    assert key(real) != lc._UNREADABLE_STAMP, "pick a stamp this key can read"
    assert key(stamp) == lc._UNREADABLE_STAMP
    assert key(stamp) > key(real)


def test_cociter_key_ignores_the_reupload_counter() -> None:
    """The captured stamp carries the language token and a WordPress duplicate
    counter, which must not join the comparison."""
    assert lc._yymm_key("2608-fr") == lc._yymm_key("2608-fr-1")
    assert lc._yymm_key("2601-fr") > lc._yymm_key("2512-fr-1")


# --- the two sides of an unknown stamp ----------------------------------------


def test_a_served_url_the_pattern_cannot_read_fails() -> None:
    """The unreadable sentinel sorts ABOVE every real stamp, which is right for
    the advertised side and exactly backwards for the served side: reused there
    it reads as "newer than anything advertised" and passes. Caught by
    sabotaging a resolver, which is the only reason it did not ship."""
    assert lc._stamps_from(_GBS_RE, "https://cdn.example/not-a-card.pdf") == [None]
    assert lc._stamps_from(_GBS_RE, "https://cdn.example/202607_gbs_x.pdf") == [
        "202607"
    ]


def test_freshness_matching_is_case_insensitive() -> None:
    """Every provider module compiles its card patterns with re.IGNORECASE, so a
    case-sensitive gate is stricter than the extractor it audits and goes blind
    exactly where the extractor still sees."""
    lc._expect_newest_card(
        "ecopower/freshness",
        '<a href="https://cdn.example/202608_GBS_Tariefkaart.pdf">Aug</a>',
        _GBS_RE,
        "202607",
        key=_date_key,
    )
    (row,) = _rows("ecopower/freshness")
    assert row.ok is False
    assert "202608" in row.detail


_MEGA_RE = r"/tarif/Mega-\w+-EL-\w+-(?:BX|VL|WL)-(\w+)-[^\"'\s]*\.pdf"
_MEGA_PAGE = (
    '<a href="https://x/tarif/Mega-FR-EL-B2C-VL-092026-A-Fixed.pdf">rolled</a>'
    '<a href="https://x/tarif/Mega-FR-EL-B2C-WL-082026-A-Fixed.pdf">not yet</a>'
)


def test_a_supplier_publication_stagger_is_not_staleness() -> None:
    """Comparing the OLDEST resolved card looks stricter and is wrong: a
    supplier that rolls one region's card before another's then gets named
    broken for its own schedule. Mega's listing is demonstrably not atomic."""
    lc._expect_newest_card(
        "mega/freshness", _MEGA_PAGE, _MEGA_RE, "092026", key=lc._mega_month_key
    )
    assert _rows("mega/freshness")[0].ok is True


def test_a_family_wide_stale_resolution_still_fails() -> None:
    """What the row exists for: a resolver blind to the new filename shape
    takes out every card at once, so the newest resolved is still behind."""
    lc._expect_newest_card(
        "mega/freshness", _MEGA_PAGE, _MEGA_RE, "072026", key=lc._mega_month_key
    )
    (row,) = _rows("mega/freshness")
    assert row.ok is False
    assert "092026" in row.detail


_EBEM_RE = (
    r"tariefkaart[-_][a-z]*[-_]?"
    r"([a-z]{3,10}[-_]\d{4}|\d{2}[-_]\d{4}|\d{4}[-_]\d{2})"
    r"[^\"'/]*\.pdf"
)
_EBEM_PAGE = (
    '<a href="/media/a/ebem_tariefkaart-elek-08-2026.pdf">elek</a>'
    '<a href="/media/b/ebem_tariefkaart-gas-09-2026.pdf">gas, published early</a>'
)


def test_ebem_gas_cards_are_not_advertised_electricity_cards() -> None:
    """The kind token is matched loosely so a renamed electricity card still
    registers, and that looseness swallows "gas". EBEM has twice rolled its
    gas card days ahead of the electricity one, which would fail this row for
    a card the integration does not parse."""
    lc._expect_newest_card(
        "ebem/freshness",
        _EBEM_PAGE,
        _EBEM_RE,
        "2026-08",
        key=lc._month_year_key,
        exclude="gas",
    )
    assert _rows("ebem/freshness")[0].ok is True


def test_ebem_still_fails_on_a_stale_electricity_card() -> None:
    lc._expect_newest_card(
        "ebem/freshness",
        _EBEM_PAGE,
        _EBEM_RE,
        "2026-07",
        key=lc._month_year_key,
        exclude="gas",
    )
    (row,) = _rows("ebem/freshness")
    assert row.ok is False
    assert "08-2026" in row.detail


# --- Mega professional cards --------------------------------------------------
#
# These have no advertised set at all: Mega never links the B2B cards from a
# page, so the CDN's own answer is the signal (application/pdf = published,
# text/html stub = not). What makes it worth checking is that fetch() rolls
# back a month when the card is missing, and four of the nine professional
# contracts are variable or dynamic, so last month's card carries last
# month's index.


class _FakeHead:
    def __init__(self, ctype: str) -> None:
        self.headers = {"Content-Type": ctype}

    async def __aenter__(self) -> "_FakeHead":
        return self

    async def __aexit__(self, *a: object) -> None:
        return None


class _FakeSession:
    def __init__(self, ctype: str) -> None:
        self._ctype = ctype
        self.calls = 0

    def head(self, _url: str, **_kw: object) -> _FakeHead:
        self.calls += 1
        return _FakeHead(self._ctype)


class _FakeClock:
    """Stands in for homeassistant.util.dt, whose now() returns a datetime."""

    def __init__(self, when: date) -> None:
        self._when = datetime(when.year, when.month, when.day, 12, 0)

    def now(self) -> datetime:
        return self._when


def _mega_module() -> object:
    from custom_components.be_electricity_prices.providers import mega

    return mega


@pytest.mark.parametrize(
    ("ctype", "day", "ok", "needle"),
    [
        pytest.param("application/pdf", 20, True, "", id="published"),
        pytest.param(
            "text/html; charset=utf-8", 2, True, "grace", id="missing-in-grace"
        ),
        pytest.param(
            "text/html; charset=utf-8",
            20,
            False,
            "past publication",
            id="missing-past-grace",
        ),
    ],
)
def test_mega_professional_publication_check(
    ctype: str, day: int, ok: bool, needle: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    mega = _mega_module()
    monkeypatch.setattr(mega, "dt_util", _FakeClock(date(2026, 9, day)), raising=False)
    session = _FakeSession(ctype)
    asyncio.run(lc._check_mega_professional(session, mega))  # type: ignore[arg-type]
    (row,) = _rows("mega/freshness: professional")
    assert row.ok is ok
    if needle:
        assert needle in row.detail


def test_mega_professional_check_covers_every_contract_region(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One HEAD per (professional contract, region). A per-contract check
    would miss Mega publishing one region and not another, which its
    residential listing has already done once (the Wallonia Dynamic block)."""
    mega = _mega_module()
    monkeypatch.setattr(mega, "dt_util", _FakeClock(date(2026, 9, 20)), raising=False)
    session = _FakeSession("application/pdf")
    asyncio.run(lc._check_mega_professional(session, mega))  # type: ignore[arg-type]
    expected = len(
        [
            1
            for c in mega._CONTRACTS  # type: ignore[attr-defined]
            if c.professional
            for r in mega._REGION_TO_CODE  # type: ignore[attr-defined]
            if r in c.regions
        ]
    )
    assert session.calls == expected == 30


def test_catalog_baseline_ignores_editions_the_listing_never_shows() -> None:
    """A professional edition must not vouch for a residential product.

    Bolt advertises only its residential cards, and so does Mega apart from
    the SME pair, while a B2B edition reuses the residential product name
    (Mega) or folder/slug (Bolt). Counting every professional contract in the
    baseline is what kept the catalog diff quiet when Mega dropped Zen Fixed
    from the listing for the August 2026 card and put it back in September:
    mega_pro_zen_fixed carried the name throughout, so neither the loss nor
    the return was ever a new product. Counting none of them is wrong in the
    other direction, which the SME row below is.

    Stubbed rather than run against the real registry, because the shapes that
    matter -- a product sold only to businesses and only advertised there, and
    one sold to both -- are one contract apart in the registry and identical in
    the baseline until the day they are not.
    """
    mega = SimpleNamespace(
        _CONTRACTS=(
            SimpleNamespace(product_name="Smart Fixed", advertised=True),
            # The B2B edition of the same product, and one whose residential
            # edition has left the listing: neither is advertised.
            SimpleNamespace(product_name="Smart Fixed", advertised=False),
            SimpleNamespace(product_name="Zen Fixed", advertised=False),
            # A professional card Mega DOES link from the listing: leaving it
            # out reports it as a new product every day.
            SimpleNamespace(product_name="SME Flex", advertised=True),
        )
    )
    assert lc._CATALOG_BASELINES["mega"](mega) == {  # type: ignore[arg-type]
        "Smart Fixed",
        "SME Flex",
    }

    bolt = SimpleNamespace(
        _CONTRACTS=(
            SimpleNamespace(folder="go", slug="fix", professional=False),
            SimpleNamespace(folder="go", slug="fix", professional=True),
            SimpleNamespace(folder="pro", slug="only", professional=True),
        )
    )
    assert lc._CATALOG_BASELINES["bolt"](bolt) == {"go/fix"}  # type: ignore[arg-type]


def test_mega_professional_transport_failure_is_not_a_publication_signal() -> None:
    """A dead network is not Mega failing to publish, and the extractor rows
    already report a real break."""

    class _Boom:
        def head(self, _url: str, **_kw: object) -> _FakeHead:
            raise aiohttp.ClientError("connection reset")

    mega = _mega_module()
    asyncio.run(lc._check_mega_professional(_Boom(), mega))  # type: ignore[arg-type]
    (row,) = _rows("mega/freshness: professional")
    assert row.ok is True
    assert "HEAD failed" in row.detail


# --- card period --------------------------------------------------------------
#
# The third freshness mechanism. The rows above ask whether a NEWER card exists
# somewhere; this asks the card itself whether it is current, which is the only
# question available for a supplier that overwrites one fixed URL in place
# (OCTA+, TotalEnergies, Engie, Luminus).


def _snap(label: str, valid_until: date | None = None) -> SimpleNamespace:
    return SimpleNamespace(publication_label=label, valid_until=valid_until)


@pytest.mark.parametrize(
    ("label", "expect"),
    [
        pytest.param("08/2026", (2026, 8), id="numeric-slash"),
        pytest.param("2026-08", (2026, 8), id="iso"),
        pytest.param("août 2026", (2026, 8), id="fr-accented"),
        pytest.param("Août 2026", (2026, 8), id="fr-titlecase"),
        pytest.param("Aôut 2026", (2026, 8), id="fr-supplier-typo"),
        pytest.param("augustus 2026", (2026, 8), id="nl"),
        pytest.param("décembre 2026", (2026, 12), id="fr-december"),
        pytest.param("Q3 2026", None, id="unknown-shape"),
        pytest.param("", None, id="empty"),
    ],
)
def test_label_month_reads_every_shape_the_cards_print(
    label: str, expect: tuple[int, int] | None
) -> None:
    """A character class that forgets the u in "aout" fails to read 104 of the
    236 live labels, and an unreadable label is skipped -- so the check would
    have quietly covered almost nothing."""
    assert lc._label_month(label) == expect


def test_a_card_from_a_past_month_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    lc._expect_card_period(
        "octaplus/octaplus_fixed", "octaplus_fixed", _snap("Juin 2026")
    )
    rows = [c for c in lc.CHECKS if not c.ok]
    assert rows and "Juin 2026" in rows[0].detail


def test_a_card_published_early_is_not_stale() -> None:
    """A label NEWER than the current month means the supplier published
    ahead, which is not staleness."""
    lc._expect_card_period("engie/x", "engie_easy_fixed", _snap("december 2099"))
    assert all(c.ok for c in lc.CHECKS)


def test_an_expired_valid_until_fails() -> None:
    lc._expect_card_period(
        "luminus/x", "luminus_comfy", _snap("december 2099", date(2020, 1, 31))
    )
    rows = [c for c in lc.CHECKS if not c.ok]
    assert rows and "valid_until" in rows[0].detail


def test_an_unknown_label_shape_is_reported_but_does_not_fail() -> None:
    """Unknown is not evidence of staleness -- but it is recorded, so a new
    label shape is visible rather than silently uncovered."""
    lc._expect_card_period("engie/x", "engie_easy_fixed", _snap("carte tarifaire"))
    (row,) = lc.CHECKS
    assert row.ok is True
    assert "unparsed" in row.detail


@pytest.mark.parametrize(
    ("label", "today", "ok", "note"),
    [
        pytest.param(
            "2026-07", date(2026, 8, 20), True, "normal arrears", id="one-month"
        ),
        pytest.param(
            "2026-06", date(2026, 8, 20), False, "not arrears", id="two-months"
        ),
        pytest.param(
            "2026-01", date(2026, 8, 20), False, "stopped publishing", id="seven-months"
        ),
        pytest.param(
            "2026-06",
            date(2026, 8, 2),
            True,
            "grace, publishing late",
            id="two-in-grace",
        ),
    ],
)
def test_the_arrears_allowance_has_a_ceiling(
    label: str, today: date, ok: bool, note: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ecopower's definitive card publishes in arrears, so ONE month behind is
    expected every month. Two is not arrears, it is Ecopower having stopped -
    an unbounded skip would have hidden that forever."""
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(today))
    lc._expect_card_period("ecopower/x", "ecopower_burgerstroom", _snap(label))
    rows = [c for c in lc.CHECKS if "recent enough" in c.label]
    assert rows and rows[0].ok is ok, note


def test_a_supplier_with_no_allowance_must_be_on_the_current_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(date(2026, 8, 20)))
    lc._expect_card_period("engie/x", "engie_easy_fixed", _snap("juli 2026"))
    rows = [c for c in lc.CHECKS if "recent enough" in c.label]
    assert rows and rows[0].ok is False


def test_the_arrears_allowance_carries_a_review_date() -> None:
    """It rests on a publishing convention, not on a date the supplier
    declares, so nothing can expire it automatically. The review date is
    enforced by the test below instead of at runtime, because a runtime
    expiry would fail on perfectly normal arrears."""
    assert lc._PERIOD_LAG_REVIEW_BY > date(2026, 8, 13)


def test_the_arrears_allowance_is_still_believed() -> None:
    """Fails once the review date passes. Re-verify that Ecopower still
    publishes its definitive card at month end -- the page should carry
    contiguous YYYYMM cards ending one month back - then move the date."""
    today = datetime.now().date()
    assert today < lc._PERIOD_LAG_REVIEW_BY, (
        f"_PERIOD_MAX_LAG_MONTHS is due for review ({lc._PERIOD_LAG_REVIEW_BY}): "
        "confirm each allowance still reflects how that supplier publishes, "
        "then move the date"
    )


class _FrozenDatetime:
    def __init__(self, when: date) -> None:
        self._when = datetime(when.year, when.month, when.day, 12, 0)

    def now(self, _tz: object = None) -> datetime:
        return self._when


@pytest.mark.parametrize(
    ("today", "rows", "note"),
    [
        pytest.param(date(2026, 8, 13), 0, "still selling", id="before"),
        pytest.param(date(2026, 8, 31), 0, "last day", id="on-the-date"),
        pytest.param(date(2026, 9, 15), 2, "gone", id="after"),
    ],
)
def test_a_withdrawing_supplier_allowance_expires_by_itself(
    today: date, rows: int, note: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The allowance is derived from the supplier's own deprecated_until, not
    from a name listed here, so it ends when the withdrawal does and there is
    nothing to remember to remove."""
    monkeypatch.setitem(lc._DEPRECATED_UNTIL, "dats24", date(2026, 8, 31))
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(today))
    lc._expect_card_period(
        "dats24/dats24_groen_variabel",
        "dats24_groen_variabel",
        _snap("juli 2026", date(2026, 7, 31)),
    )
    assert len(lc.CHECKS) == rows, note


def test_a_withdrawn_supplier_reports_but_does_not_gate_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its final card stays up and stays stale forever. Real and worth
    showing, but no change here can fix it, so it must not set the extractor
    bit and refile an issue every night -- the same treatment an unreadable
    card already gets."""
    monkeypatch.setitem(lc._DEPRECATED_UNTIL, "dats24", date(2026, 8, 31))
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(date(2026, 11, 1)))
    lc._expect_card_period(
        "dats24/dats24_groen_variabel",
        "dats24_groen_variabel",
        _snap("juli 2026", date(2026, 7, 31)),
    )
    failures = [c for c in lc.CHECKS if not c.ok]
    assert failures
    assert all(c.expected for c in failures)
    assert lc._extractor_regressions(lc.CHECKS) == []


def test_the_withdrawal_date_is_read_from_the_registry() -> None:
    """Declared on the supplier's own EXTRACTOR, so it lives in one place."""
    from custom_components.be_electricity_prices.providers import dats24

    assert dats24.EXTRACTOR.deprecated_until == date(2026, 8, 31)
    assert "dats24" not in lc._PERIOD_MAX_LAG_MONTHS


def test_every_allowance_is_a_ceiling_not_a_skip() -> None:
    """A zero or negative entry would be pointless and a huge one would be a
    skip wearing a number."""
    assert all(1 <= n <= 2 for n in lc._PERIOD_MAX_LAG_MONTHS.values())


def test_the_ecopower_dynamic_card_is_not_exempt() -> None:
    """The supplier is never exempt wholesale, and the dynamic card never gets
    a lag ceiling: it is republished on rate changes, not monthly, so a
    calendar allowance either false-alarms or becomes a skip wearing a number.

    Its freshness is asserted against the listing instead, which is strictly
    stronger than the calendar was. That is what keeps the 0.12.5 bug caught:
    the extractor's pattern stopped matching a new filename shape and the
    resolver fell back to a January card that downloaded and parsed clean, so
    the only signal was the page carrying something newer than what we
    resolved.
    """
    assert "ecopower" not in lc._PERIOD_MAX_LAG_MONTHS
    assert "ecopower_dynamische_burgerstroom" not in lc._PERIOD_MAX_LAG_MONTHS
    assert "ecopower_dynamische_burgerstroom" in lc._PERIOD_NO_ROTATION
    assert "ecopower" not in lc._PERIOD_NO_ROTATION
    # The two sets must not overlap: a contract cannot be both bounded by a
    # ceiling and told the ceiling does not apply to it.
    assert not (set(lc._PERIOD_MAX_LAG_MONTHS) & lc._PERIOD_NO_ROTATION)


def test_a_no_rotation_card_is_checked_against_the_listing() -> None:
    """Every contract excused from the calendar has to be covered by the
    listing assertion instead, or the exemption is just a skip."""
    import inspect

    src = inspect.getsource(lc._check_ecopower)
    assert "_PERIOD_NO_ROTATION" in src
    assert "_expect_newest_listed_card" in src
    # and the listing pattern must not be the extractor's own, or a regex
    # regression would hide from the check written to catch it
    helper = inspect.getsource(lc._expect_newest_listed_card)
    assert "_DBS_CARD_RE" not in helper


def test_injection_shape_is_asserted_even_when_the_card_prints_an_indicative() -> None:
    """Several dynamic cards publish BOTH a formula and an indicative, and the
    shape assertion used to hang off the else of the indicative's range check.
    So the formula was only ever tested while the indicative happened to be
    absent, and a card redesign that dropped the formula and kept the
    indicative passed green while the credit silently went flat."""
    inj = SimpleNamespace(
        current=0.09136,
        factor=None,
        base=None,
        spp_indexed=False,
        peak=None,
        transition=None,
        offpeak=None,
    )
    lc._validate_injection("x", SimpleNamespace(injection=inj), "present")
    shape_rows = [c for c in lc.CHECKS if "factor + base present" in c.label]
    assert shape_rows and not shape_rows[0].ok

    lc.CHECKS.clear()
    inj.factor, inj.base = 1.0, -0.0131
    lc._validate_injection("x", SimpleNamespace(injection=inj), "present")
    shape_rows = [c for c in lc.CHECKS if "factor + base present" in c.label]
    assert shape_rows and shape_rows[0].ok


def test_a_dropped_standing_charge_fails_the_check() -> None:
    """The floor is the half of the fee bound that pays.

    ``yearly_fixed_fee`` defaults to 0,0, so an abonnement anchor that stops
    matching after a card re-render drops the whole standing charge in
    silence. A contract off the allowlist reading 0,00 therefore has to fail,
    and one on it has to pass, or the bound is decoration.
    """
    assert "energyknights_essentia" not in lc._NO_STANDING_CHARGE
    failures = _energy_failures(SpotMonthlyRates(factor=1.19, base=0.009))
    assert [f for f in failures if "standing charge" in f], failures

    lc.CHECKS.clear()
    lc._validate_energy(
        "x", "engie_empty_house", SpotMonthlyRates(factor=1.19, base=0.009)
    )
    assert not [c for c in lc.CHECKS if not c.ok and "standing charge" in c.label]


def test_no_standing_charge_matches_the_cards() -> None:
    """Only a card that prints no standing charge may sit on the allowlist.

    The bound in ``_validate_energy`` refuses a 0,00 EUR/yr abonnement,
    because that is what a fee anchor looks like once it stops matching.
    Engie's Empty House and Ecopower's Groene Burgerstroom genuinely print
    none, so they are listed rather than inferred. Held against the fixtures
    here so the list cannot outlive the fact, the way _PRO_INJECTION_VAT_EXEMPT
    did before it got its own test.
    """
    from custom_components.be_electricity_prices.const import REGION_FLANDERS
    from custom_components.be_electricity_prices.providers import ecopower, engie

    from tests import fixture_text

    cards = {
        "engie_empty_house": lambda: engie.parse_snapshot(
            "engie_empty_house",
            {REGION_FLANDERS: fixture_text("engie_empty_house_v.pdf")},
        ),
        "ecopower_burgerstroom": lambda: ecopower.parse_snapshot(
            fixture_text("ecopower_burgerstroom_jul.pdf", layout=True),
            "test://gbs",
            "juli 2026",
        ),
    }
    for contract_id, parse in cards.items():
        assert contract_id in lc._NO_STANDING_CHARGE, contract_id
        assert parse().energy.yearly_fixed_fee == 0.0, contract_id
    # No pro Empty House fixture exists and the residential one cannot stand in
    # for it: the two editions take different branches. Everything else on the
    # allowlist has to be one of these cards.
    assert lc._NO_STANDING_CHARGE - set(cards) == {"engie_pro_empty_house"}


def test_pro_injection_vat_expectation_matches_the_cards() -> None:
    """The live check's expectation and the extractor must agree.

    Whether a professional card taxes injection is printed on the card, not
    implied by the edition, and Mega splits: its pro fixed and smart cards say
    "a majorer de la TVA", its pro DYNAMIC card says "exemptes de TVA". The
    extractor reads that sentence; the live check cannot, because it is handed
    a parsed snapshot rather than the text, so it carries an expectation set
    instead. This holds the two together against the real fixtures, which is
    the check that was missing when the extractor was corrected and the live
    run started failing three rows a day (issue #71).
    """
    from custom_components.be_electricity_prices.providers.mega import (
        _injection_vat_applies,
    )
    from tests import fixture_text

    cards = {
        "mega_pro_dynamic": "mega_pro_dynamic_w.pdf",
        "mega_pro_smart_fixed": "mega_pro_smart_fixed_w.pdf",
        "mega_pro_offpeak_fixed": "mega_pro_offpeak_fixed_v.pdf",
    }
    for cid, fixture in cards.items():
        # professional=True is what the live check keys on; the point is that
        # the CARD's own sentence overrides the edition.
        taxed = _injection_vat_applies(fixture_text(fixture, layout=True), True)
        expected_exempt = cid in lc._PRO_INJECTION_VAT_EXEMPT
        assert taxed is not expected_exempt, cid


def test_the_vat_check_reads_the_contract_shape_it_is_actually_given() -> None:
    """Two contract shapes reach _expect_professional_basis: the registry
    Contract, which names the field "id", and a provider's internal
    _ContractDef, which names it "contract_id". The Mega call site iterates
    the latter.

    Reading only "id" yielded "" for it, which matches no exemption, so the
    check failed three rows on every live run against a card being parsed
    correctly - and the first version of this test passed anyway, because it
    built the registry shape by hand. It now drives the object the caller
    really passes.
    """
    from custom_components.be_electricity_prices.providers import mega
    from custom_components.be_electricity_prices.providers.base import (
        InjectionRates,
        TaxOverlay,
    )
    from custom_components.be_electricity_prices.providers.mega import (
        _injection_vat_applies,
    )
    from tests import fixture_text

    text = fixture_text("mega_pro_dynamic_w.pdf", layout=True)
    snap = SimpleNamespace(
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.0, vat_rate=0.21),
        injection=InjectionRates(
            current=0.06, vat_applies=_injection_vat_applies(text, True)
        ),
    )
    internal = next(c for c in mega._CONTRACTS if c.contract_id == "mega_pro_dynamic")
    assert not hasattr(internal, "id")

    for contract in (
        internal,
        SimpleNamespace(id="mega_pro_dynamic", professional=True),
    ):
        lc.CHECKS.clear()
        lc._expect_professional_basis("x", contract, snap)
        row = next(c for c in lc.CHECKS if "injection VAT" in c.label)
        assert row.ok, f"{contract}: {row.detail}"


def test_every_month_indexed_card_can_collect_a_key() -> None:
    """The two flags that have to agree, and the test that makes them.

    A parser sets ``InjectionRates.spp_indexed`` / ``month_indexed`` on the
    snapshot; the config flow decides whether to offer the optional ENTSO-E key
    from ``Contract.spot_indexed_injection`` on the registry. The first is
    known only after a card is fetched, the second has to be known before.
    When they disagree the parser wins on paper and loses in practice: no key
    step, no spots, no monthly mean, and every path falls back to the card's
    printed figure. Nine contracts across five suppliers shipped that way.

    ``_INJECTION_SHAPE`` is the live check's own record of which cards carry a
    formula their energy leg does not fetch spots for, month or per-slot, so
    it is what the registry is held against here. A contract
    whose KIND already collects the key for its energy leg (dynamic,
    spot_monthly) is exempt and must leave the flag False: energie.be Variabel
    is spot_monthly and would otherwise be asked for a key it already has.
    """
    from custom_components.be_electricity_prices.const import (
        SPOT_PRICED_CONTRACT_KINDS,
    )
    from custom_components.be_electricity_prices.providers import EXTRACTORS

    by_id = {c.id: c for ex in EXTRACTORS.values() for c in ex.contracts}
    missing = sorted(
        cid
        for cid, shape in lc._INJECTION_SHAPE.items()
        if shape in ("spp", "month", "spot")
        and cid in by_id
        and by_id[cid].kind not in SPOT_PRICED_CONTRACT_KINDS
        and not by_id[cid].spot_indexed_injection
    )
    assert missing == [], (
        "these contracts index injection on a monthly mean but no flow step "
        f"offers them an ENTSO-E key, so the formula can never resolve: {missing}"
    )


def test_a_month_indexed_card_losing_its_flag_fails() -> None:
    """The five cards that credit a monthly index carry coefficients AND the
    flag that names which mean they resolve against. Dropping the flag leaves
    the coefficients looking like a per-hour formula, so the credit would
    follow the current slot's spot without any check going red."""
    assert lc._expected_injection_shape("power_fix") == "month"
    assert lc._expected_injection_shape("ebem_variable") == "spp"

    inj = SimpleNamespace(
        current=0.0476,
        factor=0.8,
        base=-0.0265,
        spp_indexed=False,
        month_indexed=False,
        peak=None,
        transition=None,
        offpeak=None,
    )
    lc._validate_injection("x", SimpleNamespace(injection=inj), "month")
    rows = [c for c in lc.CHECKS if "month-indexed injection" in c.label]
    assert rows and not rows[0].ok

    lc.CHECKS.clear()
    inj.month_indexed = True
    lc._validate_injection("x", SimpleNamespace(injection=inj), "month")
    rows = [c for c in lc.CHECKS if "month-indexed injection" in c.label]
    assert rows and rows[0].ok


def test_every_month_indexed_eneco_card_is_pinned_in_the_shape_map() -> None:
    """An unpinned Eneco card derives the WRONG shape, not a weaker one.

    Fix, Flex and Flex One all carry ``spot_indexed_injection`` so the flow
    offers them a key, and that flag is the first thing
    ``_expected_injection_shape`` derives from. In a live run, where
    ``_CONTRACTS_BY_ID`` is populated, an unpinned card resolves to "spot",
    which asserts a factor and a base and never looks at ``month_indexed`` at
    all, so a card that dropped the flag would ship green and credit the
    current slot's spot. Derived from the fixtures rather than listed, so a
    fourth Eneco product cannot be added without landing in the map, which is
    exactly how Flex One arrived.
    """
    from custom_components.be_electricity_prices.const import REGION_FLANDERS
    from custom_components.be_electricity_prices.providers.eneco import parse_snapshot

    from tests import fixture_text

    for fixture, cid in (
        ("eneco_fix.pdf", "power_fix"),
        ("eneco_flex.pdf", "power_flex"),
        ("eneco_flex_one.pdf", "power_flex_one"),
        ("eneco_dyn.pdf", "power_dynamic"),
    ):
        snap = parse_snapshot(
            fixture_text(fixture), cid, f"test://{fixture}", REGION_FLANDERS
        )
        assert snap.injection is not None, fixture
        if snap.injection.month_indexed:
            assert lc._expected_injection_shape(cid) == "month", cid


def test_a_month_indexed_card_losing_its_coefficients_fails() -> None:
    """The printed indicative alone is the PREVIOUS month's rate, so a card
    that keeps it and loses the formula must not pass. That is exactly what
    the old "monthly" pin asserted, in reverse."""
    inj = SimpleNamespace(
        current=0.013354,
        factor=None,
        base=None,
        spp_indexed=True,
        month_indexed=False,
        peak=None,
        transition=None,
        offpeak=None,
    )
    lc._validate_injection("x", SimpleNamespace(injection=inj), "spp")
    rows = [c for c in lc.CHECKS if "SPP-indexed injection" in c.label]
    assert rows and not rows[0].ok


def test_a_tou_card_losing_its_injection_triplet_fails() -> None:
    """Empower Flextime is the one card whose feed-in tariff varies by slot.
    Its kind is neither fixed nor variable, so the shape derived to "present",
    which asserts a factor/base it does not have and never looks at the
    triplet at all."""
    assert lc._expected_injection_shape("engie_empower_flextime") == "triplet"

    inj = SimpleNamespace(
        current=0.04918,
        factor=None,
        base=None,
        spp_indexed=False,
        peak=None,
        transition=None,
        offpeak=None,
    )
    lc._validate_injection("x", SimpleNamespace(injection=inj), "triplet")
    rows = [c for c in lc.CHECKS if "triplet present" in c.label]
    assert rows and not rows[0].ok

    lc.CHECKS.clear()
    inj.peak, inj.transition, inj.offpeak = 0.08417, 0.04834, 0.01465
    lc._validate_injection("x", SimpleNamespace(injection=inj), "triplet")
    rows = [c for c in lc.CHECKS if "triplet present" in c.label]
    assert rows and rows[0].ok


@pytest.fixture
def _bound_rate_types() -> Iterator[None]:
    """Bind the rate classes _validate_energy dispatches on.

    live_check leaves them as ``object`` until _load_providers() has run, and
    ``isinstance(anything, object)`` is True, so an unbound harness sends every
    leg down the FIRST branch and reports a fixed-rate failure for a card that
    has no such field. Bind the real classes instead of loading the whole
    provider set: identity is all the dispatch needs.
    """
    from custom_components.be_electricity_prices.providers import base

    saved = (
        lc._RATE_FIXED,
        lc._RATE_VARIABLE,
        lc._RATE_DYNAMIC,
        lc._RATE_SPOT_MONTHLY,
        lc._RATE_TOU,
        lc._RATE_IMPACT,
    )
    lc._RATE_FIXED = base.FixedRates
    lc._RATE_VARIABLE = base.VariableRates
    lc._RATE_DYNAMIC = base.DynamicRates
    lc._RATE_SPOT_MONTHLY = base.SpotMonthlyRates
    lc._RATE_TOU = base.TimeOfUseRates
    lc._RATE_IMPACT = base.ImpactRates
    yield
    (
        lc._RATE_FIXED,
        lc._RATE_VARIABLE,
        lc._RATE_DYNAMIC,
        lc._RATE_SPOT_MONTHLY,
        lc._RATE_TOU,
        lc._RATE_IMPACT,
    ) = saved


def _essentia_leg() -> SpotMonthlyRates:
    """Energy Knights Essentia Online, August 2026, as the extractor parses it."""
    return SpotMonthlyRates(
        factor=1.0918,
        base=0.00742,
        factor_peak=1.1077,
        base_peak=0.00848,
        factor_offpeak=1.05682,
        base_offpeak=0.00848,
        factor_exclusive_night=1.05682,
        base_exclusive_night=0.00848,
        yearly_fixed_fee=10.0,
    )


def _energy_failures(energy: object) -> list[str]:
    lc.CHECKS.clear()
    lc._validate_energy("x", "energyknights_essentia", energy)
    return [c.label.removeprefix("x: ") for c in lc.CHECKS if not c.ok]


def test_a_spot_monthly_card_with_per_meter_bands_is_fully_bounded(
    _bound_rate_types: None,
) -> None:
    """Until Essentia, no card populated these pairs at parse time.

    energie.be Variabel and the custom supplier both print one formula for
    every meter, so this branch only ever bounded the mono pair and a card
    that prints them per register could drift a band without failing
    anything.
    """
    assert _energy_failures(_essentia_leg()) == []

    # The two rows swapped. Both coefficients stay inside every range, so only
    # the ordering catches it.
    card = _essentia_leg()
    swapped = replace(
        card, factor_peak=card.factor_offpeak, factor_offpeak=card.factor_peak
    )
    assert _energy_failures(swapped) == ["spot-monthly peak factor not below offpeak"]

    # A card that flattens the two bands is normal publishing, not a swap:
    # this supplier's predecessor printed one formula in all four registers
    # for nineteen consecutive months.
    flat = replace(card, factor_peak=card.factor_offpeak, base_peak=card.base_offpeak)
    assert _energy_failures(flat) == []

    # Half a bi-hourly pair silently sends both bands back to the mono
    # formula, because pricing routes onto them only when both are set.
    half = replace(card, factor_offpeak=None, base_offpeak=None)
    assert _energy_failures(half) == [
        "spot-monthly bi-hourly pair is complete or absent"
    ]

    # Each band's own bounds, including the night circuit.
    assert _energy_failures(replace(card, factor_peak=11.0)) == [
        "spot-monthly peak factor in [0.5, 3.0]"
    ]
    assert _energy_failures(replace(card, base_peak=1.5)) == [
        "spot-monthly peak base in [0, 0.10] EUR/kWh"
    ]
    assert _energy_failures(replace(card, factor_exclusive_night=0.1)) == [
        "spot-monthly exclusive_night factor in [0.5, 3.0]"
    ]


def test_a_mono_only_spot_monthly_card_is_unaffected(_bound_rate_types: None) -> None:
    """energie.be Variabel prints one formula for every meter. The new band
    assertions must not start demanding pairs it never had.

    The card's own 35,00 EUR/yr standing charge is set because the fee bound
    reads every rate shape, and a stub left at the dataclass default 0,0 would
    fail it for a reason this test is not about.
    """
    assert (
        _energy_failures(
            SpotMonthlyRates(factor=1.19, base=0.009, yearly_fixed_fee=35.0)
        )
        == []
    )


def test_sweep_cost_is_reported_and_never_warned_on(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The declared sweep cost is fetch PLUS parse on a Raspberry Pi, and
    parse CPU dominates it for the expensive suppliers. A GitHub runner's CPU
    and network are neither comparable nor stable - four suppliers already
    carry latency-budget notes saying they are slow only from runners - and
    this workflow opens issues by itself, so a threshold here would file
    supplier-side bugs for runner variance.

    Logged for whoever tunes the value later; never a warning."""
    import live_check as lc

    lc.METRICS.clear()
    lc.METRICS["bolt"] = {
        "fetches": 6.0,
        "elapsed_s": 120.0,
        "bytes": 0.0,
        "failed": 0.0,
        "failed_s": 0.0,
    }
    lc._DECLARED_SWEEP_COST["bolt"] = 45.3

    # Wildly over the declared figure, which is exactly the runner case.
    lc._record_sweep_cost("bolt", 900.0)
    err = capsys.readouterr().err
    assert "sweep-cost: bolt" in err
    assert "150.00s per card" in err
    assert "declares 45.3s" in err

    # And it contributes nothing to the warning list that files issues.
    assert not any("sweep" in w.lower() for w in lc._drift_warnings(lc.METRICS))


def test_sweep_cost_reporting_is_silent_without_a_measurement() -> None:
    """A supplier whose check made no request, or one this build does not
    ship, must not divide by zero or raise out of a logging helper."""
    import live_check as lc

    lc.METRICS.clear()
    lc._record_sweep_cost("nosuch", 5.0)
    lc.METRICS["bolt"] = {
        "fetches": 0.0,
        "elapsed_s": 0.0,
        "bytes": 0.0,
        "failed": 0.0,
        "failed_s": 0.0,
    }
    lc._record_sweep_cost("bolt", 5.0)


def test_a_withdrawn_suppliers_fetch_failure_does_not_gate_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allowance used to cover only the staleness question. Everything
    else still filed: DATS 24 left residential on 2026-08-31 and its August
    card 404ed the next morning, opening an extractor-broken issue that named
    a supplier which no longer sells electricity (issue #78). A supplier past
    its own date cannot fail in a way this repository can fix.
    """
    monkeypatch.setitem(lc._DEPRECATED_UNTIL, "dats24", date(2026, 8, 31))
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(date(2026, 9, 1)))
    lc._record(
        "dats24/dats24_groen_variabel/flanders: fetch",
        False,
        "ExtractorError: HTTP 404 fetching https://example.invalid/aug.pdf",
    )
    (check,) = lc.CHECKS
    assert check.expected
    assert lc._extractor_regressions(lc.CHECKS) == []


def test_a_withdrawing_supplier_still_fails_on_its_last_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Narrowness guard. Up to and including its final day the supplier is
    still trading, so a fetch failure then is a real break worth filing."""
    monkeypatch.setitem(lc._DEPRECATED_UNTIL, "dats24", date(2026, 8, 31))
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(date(2026, 8, 31)))
    lc._record("dats24/x/flanders: fetch", False, "ExtractorError: HTTP 500")
    (check,) = lc.CHECKS
    assert not check.expected
    assert lc._extractor_regressions(lc.CHECKS) != []


def test_a_live_suppliers_failure_is_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A supplier with no withdrawal date must be unaffected, and a label
    carrying no supplier prefix must not be parsed into one."""
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(date(2026, 9, 1)))
    lc._record("bolt/bolt_variable/flanders: parse", False, "ExtractorError: boom")
    lc._record("spot/fallback: check crashed", False, "RuntimeError: boom")
    assert all(not c.expected for c in lc.CHECKS)


@pytest.mark.parametrize(
    ("today", "valid_until", "fails", "note"),
    [
        pytest.param(
            date(2026, 9, 1),
            date(2026, 8, 31),
            False,
            "1st of the month, card ran to the end of last month",
            id="grace-first-day",
        ),
        pytest.param(
            date(2026, 9, 5),
            date(2026, 8, 31),
            False,
            "last day of the grace window",
            id="grace-last-day",
        ),
        pytest.param(
            date(2026, 9, 6),
            date(2026, 8, 31),
            True,
            "past the window, the supplier really is late",
            id="past-the-window",
        ),
        pytest.param(
            date(2026, 9, 1),
            date(2026, 6, 30),
            True,
            "expired in JUNE: stale, and the allowance must not hide it",
            id="stale-since-june",
        ),
    ],
)
def test_expiry_check_forgives_only_a_card_that_just_lapsed(
    today: date,
    valid_until: date,
    fails: bool,
    note: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The card-period lag check already forgave a supplier publishing a few
    days late; the expiry check did not, so on the 1st of every month every
    card written to run to the end of the previous month failed. Seven did on
    2026-09-01. The allowance is scoped to a card that lapsed at the end of
    LAST month so it cannot cover one that has been stale since June."""
    monkeypatch.setattr(lc, "datetime", _FrozenDatetime(today))
    lc._expect_card_period(
        "cociter/cociter_variable",
        "cociter_variable",
        _snap("2026-08", valid_until),
    )
    expired = [c for c in lc.CHECKS if c.label.endswith("card has not expired")]
    assert bool(expired) is fails, note
