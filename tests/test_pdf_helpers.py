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

"""Tests for the PDF parsing helpers in providers/_pdf.py."""

from __future__ import annotations

import asyncio
from datetime import date

import aiohttp
import pytest

import re

from custom_components.be_electricity_prices.providers._pdf import (
    archive_validity_check,
    fetch_pdf_text,
    fetch_text,
    parse_valid_until,
    vat_multiplier,
)
from custom_components.be_electricity_prices.providers.base import (
    DsoOverlay,
    ExtractorError,
    FixedRates,
    SupplierSnapshot,
    TaxOverlay,
)


def test_parse_valid_until_dutch_geldig_van_tem() -> None:
    """Eneco / Mega Dutch wording -- date inside a 'Geldig...' window."""
    text = "Geldig van 1 april 2026 t.e.m 30 april 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_french_valable_jusqu_au() -> None:
    text = "Cette carte tarifaire est valable jusqu'au 30 avril 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_french_mai() -> None:
    """Regression: French May (mai) must be in _MONTH_NAMES so a card
    whose validity is spelled out only in French ("valable jusqu'au
    31 mai 2026") parses its actual end date instead of falling
    through to None."""
    text = "Cette carte tarifaire est valable jusqu'au 31 mai 2026."
    assert parse_valid_until(text) == date(2026, 5, 31)


def test_parse_valid_until_numeric_dd_mm_yyyy() -> None:
    text = "Validité: du 01/04/2026 au 30/04/2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_numeric_accepts_dash_and_dot_separators() -> None:
    """Legal-style date publications occasionally use dashes or dots
    instead of slashes (DD-MM-YYYY, DD.MM.YYYY). No supplier in the
    registry currently does, but the cost of supporting them is one
    regex character class and avoids a future fragility."""
    assert parse_valid_until("validité: 30-04-2026") == date(2026, 4, 30)
    assert parse_valid_until("validité: 30.04.2026") == date(2026, 4, 30)


def test_parse_valid_until_picks_latest_in_window() -> None:
    """Both validity-start and validity-end appear in one statement;
    the helper must return the end (latest)."""
    text = "valable du 1 mars 2026 au 30 avril 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_ignores_unrelated_dates() -> None:
    """Dates that don't sit inside a validity-keyword window must be
    ignored. Without this gate the parser used to mistake long-term
    contract dates (e.g. compensation-regime end 2030-12-31) for
    pricing validity."""
    text = (
        "Le regime de compensation reste applicable jusqu'au 2030-12-31. "
        "Le tarif est valable du 1 avril 2026 au 30 avril 2026."
    )
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_accepts_bare_month_year_inside_window() -> None:
    """Card with no day in the validity statement -- treat the month
    as fully covered."""
    text = "Tariefkaart april 2026, geldig in april 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_returns_none_when_no_keyword_present() -> None:
    """The card lacks any validity wording entirely -- caller must
    fall back to "treat as available", so we return None."""
    assert parse_valid_until("Eneco residential 100% green energy.") is None


def test_parse_valid_until_returns_none_for_keyword_without_date() -> None:
    text = "Cette tarification est valable conformement aux conditions generales."
    assert parse_valid_until(text) is None


def test_parse_valid_until_clamps_implausible_far_future_year() -> None:
    """A captured 4-digit year that's centuries away (typically a
    chunk of a corrupted phone number / fax that happens to look
    like DD/MM/YYYY inside a validity window) must not bubble out as
    a real validity date.
    """
    text = "Cette carte est valable du 01/04/2026 au 30/04/2625."
    # The 2026 candidate is the only one that survives the year clamp.
    assert parse_valid_until(text) == date(2026, 4, 1)


def test_parse_valid_until_rejects_retrospective_only_window() -> None:
    """A pathological card whose validity window mentions only dates
    older than today.year - 5 must not surface a misleading valid_until
    -- archive_validity_check would otherwise accept the snapshot for
    the wrong month. Synthesise a window with only a 2010 date range
    and confirm None is returned."""
    text = "valable du 1 janvier 2010 au 31 decembre 2010."
    assert parse_valid_until(text) is None


def test_parse_valid_until_accepts_archive_year_within_5_years() -> None:
    """fetch_for_month walks back through Eneco/Cociter archives; a
    card from year-3 (well within the legitimate archive horizon)
    must still parse its printed validity rather than fall through
    to the textual-mention fallback."""
    text = "valable du 1 juin 2023 au 30 juin 2023."
    # Today (test run) - 3 years is in range; assert an actual date,
    # not None. Exact date matches the second numeric of the window.
    assert parse_valid_until(text) == date(2023, 6, 30)


# ---- network-error normalization ----------------------------------------------


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in whose .get() raises on entry."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def get(self, *_args: object, **_kwargs: object) -> "_FakeCtx":
        return _FakeCtx(self._exc)


class _FakeCtx:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def __aenter__(self) -> object:  # pragma: no cover - re-raises immediately
        raise self._exc

    async def __aexit__(self, *_exc: object) -> None:
        return None


async def test_fetch_text_converts_timeout_to_extractor_error() -> None:
    """aiohttp's ClientTimeout fires asyncio.TimeoutError, which is NOT
    a ClientError. Without explicit catching the bare TimeoutError
    bubbles out of every supplier's discover/fetch and crashes the
    live-check (regression: engie/catalog 2026-05-05)."""
    session = _FakeSession(asyncio.TimeoutError())
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_text(session, "https://example.com/")  # type: ignore[arg-type]


async def test_fetch_pdf_text_converts_timeout_to_extractor_error() -> None:
    session = _FakeSession(asyncio.TimeoutError())
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_pdf_text(session, "https://example.com/x.pdf")  # type: ignore[arg-type]


async def test_fetch_text_still_converts_aiohttp_client_errors() -> None:
    """Regression guard: the broader except tuple must still catch
    plain ClientError so a network-down case doesn't bubble either."""
    session = _FakeSession(aiohttp.ClientConnectionError("dns failed"))
    with pytest.raises(ExtractorError, match="network error"):
        await fetch_text(session, "https://example.com/")  # type: ignore[arg-type]


# ---- vat_multiplier ----------------------------------------------------------


def test_vat_multiplier_single_pattern_matches() -> None:
    """Single-pattern call: the helper returns 1 + N/100 from the
    capture group when the pattern matches."""
    assert vat_multiplier(
        "Tarifs 6% TVAC", r"Tarifs\s+(\d+)\s*%\s*TVAC"
    ) == pytest.approx(1.06)
    assert vat_multiplier("TVA 21%", r"TVA\s*(\d+)\s*%") == pytest.approx(1.21)


def test_vat_multiplier_returns_default_when_pattern_misses() -> None:
    """No match falls back to the default (1.06 unless overridden)."""
    assert vat_multiplier("nothing relevant", r"TVA\s*(\d+)\s*%") == pytest.approx(1.06)
    assert vat_multiplier(
        "nothing relevant", r"TVA\s*(\d+)\s*%", default=1.21
    ) == pytest.approx(1.21)


def test_vat_multiplier_multi_pattern_falls_back_to_second() -> None:
    """Luminus-style two-regex fallback: if the primary doesn't match,
    the secondary is tried before the default. Verify both branches."""
    primary = re.compile(r"TVA\s*sur\s*les\s*prix.+?(\d+)\s*%", re.S)
    secondary = r"TVA\s*(\d+)\s*%"
    # Primary matches: secondary irrelevant.
    assert vat_multiplier(
        "TVA sur les prix de l'energie 21 %", primary, secondary
    ) == pytest.approx(1.21)
    # Only secondary matches.
    assert vat_multiplier("TVA 6%", primary, secondary) == pytest.approx(1.06)
    # Neither matches: default fires.
    assert vat_multiplier("nothing", primary, secondary) == pytest.approx(1.06)


def test_vat_multiplier_handles_decimal_capture() -> None:
    """Octa+'s pattern allows a fractional rate (e.g. ``21,5%``).
    The helper routes the capture through to_float so a card with a
    decimal VAT parses correctly."""
    pattern = r"Tarifs\s+(\d+(?:[.,]\d+)?)\s*%\s*TVAC"
    assert vat_multiplier("Tarifs 21,5% TVAC", pattern) == pytest.approx(1.215)


# ---- archive_validity_check --------------------------------------------------


def _stub_snapshot(valid_until: date | None) -> SupplierSnapshot:
    return SupplierSnapshot(
        supplier="test",
        contract="test",
        energy=FixedRates(single=0.18),
        dsos={"ores": DsoOverlay(distribution_single=0.10, transport=0.0145)},
        taxes=TaxOverlay(federal_excise=0.05, energy_contribution=0.002),
        source_url="test://",
        valid_until=valid_until,
    )


def test_archive_validity_check_valid_until_in_requested_month_is_accepted() -> None:
    """When valid_until parses cleanly and falls in the requested
    year-month, the snapshot is surfaced as-is."""
    snap = _stub_snapshot(date(2025, 12, 31))
    out = archive_validity_check(snap, "anything", date(2025, 12, 1))
    assert out is snap


def test_archive_validity_check_valid_until_in_other_month_is_rejected() -> None:
    """When valid_until parses but doesn't intersect the requested
    month, the snapshot is rejected even with a textual fallback
    available."""
    snap = _stub_snapshot(date(2025, 12, 31))
    text = "december 2025"  # mentions the wrong month deliberately
    out = archive_validity_check(
        snap, text, date(2025, 3, 1), month_names=("january", "february", "march")
    )
    assert out is None


def test_archive_validity_check_falls_back_to_text_when_valid_until_missing() -> None:
    """No parsed validity, but month_names is provided: require a
    textual mention of the requested month inside the validity window."""
    snap = _stub_snapshot(None)
    months = ("januari", "februari", "maart", "april", "mei", "juni")
    text = "Geldig van 1 mei 2026 t.e.m 31 mei 2026."
    assert (
        archive_validity_check(snap, text, date(2026, 5, 1), month_names=months) is snap
    )
    # Wrong month: the validity window mentions May, not March.
    assert (
        archive_validity_check(snap, text, date(2026, 3, 1), month_names=months) is None
    )


def test_archive_validity_check_no_textual_fallback_when_month_names_none() -> None:
    """EBEM-style: month_names=None means trust the URL resolver. With
    valid_until=None and no textual cross-check available, accept the
    snapshot as-is rather than rejecting on missing validity."""
    snap = _stub_snapshot(None)
    out = archive_validity_check(snap, "no validity here", date(2026, 5, 1))
    assert out is snap
