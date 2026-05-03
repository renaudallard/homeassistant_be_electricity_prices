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

from datetime import date

from custom_components.be_electricity_prices.providers._pdf import parse_valid_until


def test_parse_valid_until_dutch_geldig_van_tem() -> None:
    """Eneco / Mega Dutch wording -- date inside a 'Geldig...' window."""
    text = "Geldig van 1 april 2026 t.e.m 30 april 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_french_valable_jusqu_au() -> None:
    text = "Cette carte tarifaire est valable jusqu'au 30 avril 2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


def test_parse_valid_until_numeric_dd_mm_yyyy() -> None:
    text = "Validité: du 01/04/2026 au 30/04/2026."
    assert parse_valid_until(text) == date(2026, 4, 30)


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
