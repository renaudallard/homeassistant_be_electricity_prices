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

"""Fixture-based tests for the Ecopower extractor."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from custom_components.be_electricity_prices.providers.base import VariableRates
from custom_components.be_electricity_prices.providers.ecopower import (
    extract_pdf_text_layout,
    fetch_for_month,
    parse_snapshot,
)

_FIX = Path(__file__).parent / "fixtures"


def _text(name: str) -> str:
    return extract_pdf_text_layout((_FIX / name).read_bytes())


def _april_snap() -> object:
    return parse_snapshot(
        _text("ecopower_burgerstroom_apr.pdf"),
        "test://ecopower-apr",
        "april 2026",
    )


def test_april_card_energy_is_groene_burgerstroom_resolved_rate() -> None:
    """The card prints '(50% vast aan 0,17 euro + 50% variabel aan
    0,08472117 euro)   0,1274 euro/kWh'. We use the resolved rate."""
    snap = _april_snap()
    assert isinstance(snap.energy, VariableRates)
    assert snap.energy.current == pytest.approx(0.1274)


def test_april_card_dsos_cover_all_eight_fluvius_subareas() -> None:
    snap = _april_snap()
    expected = {
        "fluvius_antwerpen",
        "fluvius_halle_vilvoorde",
        "fluvius_imewo",
        "fluvius_intergem",
        "fluvius_iveka",
        "fluvius_limburg",
        "fluvius_west",
        "fluvius_zenne_dijle",
    }
    assert set(snap.dsos) == expected


def test_april_card_extracts_distribution_and_capacity_for_antwerpen() -> None:
    """Spot-check Fluvius Antwerpen against the printed values:
    databeheer 17.85, capacity 49.40 EUR/kW/yr, distribution 0.0505027."""
    snap = _april_snap()
    a = snap.dsos["fluvius_antwerpen"]
    assert a.distribution_single == pytest.approx(0.0505027)
    assert a.capacity_eur_per_kw_year == pytest.approx(49.40)
    assert a.data_management_per_year == pytest.approx(17.85)
    # Ecopower rolls Elia transport into the network distribution; the
    # card has no separate transport line, so ``transport`` stays 0
    # rather than being silently double-counted via a guess.
    assert a.transport == 0.0


def test_april_card_extracts_imewo_with_optional_max_column() -> None:
    """Imewo's row carries an optional 'Maximumtarief' value
    (``0,3276168``) inserted between the off-peak rate and the
    trailing dash. The regex must skip past it without mis-aligning
    the distribution rate."""
    snap = _april_snap()
    assert snap.dsos["fluvius_imewo"].distribution_single == pytest.approx(0.0522864)
    assert snap.dsos["fluvius_imewo"].capacity_eur_per_kw_year == pytest.approx(54.20)


def test_april_card_taxes_are_htva_with_vat_06() -> None:
    """Ecopower publishes HTVA values; vat_rate=0.06 instructs
    compute_breakdown to scale to TVAC."""
    snap = _april_snap()
    t = snap.taxes
    assert t.vat_rate == 0.06
    assert t.federal_excise == pytest.approx(0.04748)
    assert t.energy_contribution == pytest.approx(0.0019261)
    # GSC + WKK = 0.0110 + 0.00392 = 0.01492.
    assert t.flanders_renewables == pytest.approx(0.01492)
    assert t.energy_fund_eur_per_month == pytest.approx(0.006)
    # Wallonia / Brussels surcharges stay 0 -- Ecopower is Flanders-only.
    assert t.wallonia_renewables == 0.0
    assert t.brussels_renewables == 0.0


def test_april_card_injection_is_negative_for_digital_meter() -> None:
    """Ecopower CHARGES residential prosumers for grid use --
    'Terugleververgoeding (digitale meter): -0,0200 euro/kWh'.
    The negative sign must survive parsing."""
    snap = _april_snap()
    assert snap.injection is not None
    assert snap.injection.current == pytest.approx(-0.02)


def test_april_card_publication_and_supplier_metadata() -> None:
    snap = _april_snap()
    assert snap.supplier == "ecopower"
    assert snap.contract == "ecopower_burgerstroom"
    assert snap.publication_label == "april 2026"


# ---- fetch_for_month -----------------------------------------------------------


_LISTING_HTML = """
<a href="https://cdn.example/202601_gbs_tariefkaart.pdf">January</a>
<a href="https://cdn.example/202602_gbs_tariefkaart.pdf">February</a>
<a href="https://cdn.example/202603_gbs_tariefkaart.pdf">March</a>
<a href="https://cdn.example/202604_gbs_tariefkaart.pdf">April</a>
<a href="https://cdn.example/202605_gbs_inschatting_tariefkaart_ecopower.pdf">May preview</a>
"""


class _Resp:
    status = 200

    def __init__(self, body: str) -> None:
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _Session:
    def __init__(self, body: str) -> None:
        self._body = body

    def get(self, *_args: Any, **_kwargs: Any) -> _Resp:
        return _Resp(self._body)


def test_fetch_for_month_returns_snapshot_when_listing_has_url() -> None:
    """The Feb-2026 fixture parses cleanly and the listing URL with
    matching YYYYMM prefix is what fetch_for_month must surface."""
    text = _text("ecopower_burgerstroom_feb.pdf")
    with patch(
        "custom_components.be_electricity_prices.providers.ecopower.fetch_pdf_text_layout",
        new=AsyncMock(return_value=text),
    ):
        snap = asyncio.run(
            fetch_for_month(
                _Session(_LISTING_HTML),  # type: ignore[arg-type]
                "ecopower_burgerstroom",
                "flanders",
                date(2026, 2, 1),
            )
        )
    assert snap is not None
    assert snap.publication_label == "2026-02"
    assert isinstance(snap.energy, VariableRates)


def test_fetch_for_month_skips_inschatting_preview() -> None:
    """The next-month preview (gbs_inschatting) is on the listing but
    is not a billable card. fetch_for_month must not return it as the
    historical snapshot for any month."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_burgerstroom",
            "flanders",
            date(2026, 5, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_returns_none_when_listing_has_no_match() -> None:
    """Months Ecopower doesn't carry on the listing return None so the
    coordinator falls back to the proxy. Ecopower keeps only the last
    few months around."""
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_burgerstroom",
            "flanders",
            date(2024, 6, 1),
        )
    )
    assert snap is None


def test_fetch_for_month_unknown_contract_returns_none() -> None:
    snap = asyncio.run(
        fetch_for_month(
            _Session(_LISTING_HTML),  # type: ignore[arg-type]
            "ecopower_zakelijk",
            "flanders",
            date(2026, 2, 1),
        )
    )
    assert snap is None
