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
from pathlib import Path

import pytest

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
