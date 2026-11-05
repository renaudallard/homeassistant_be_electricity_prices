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

"""Frank Energie's variable electricity tiers, on their real cards."""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from unittest.mock import AsyncMock

import pytest

from custom_components.be_electricity_prices.const import (
    FLUVIUS_KEYS,
    REGION_FLANDERS,
    WELCOME_CREDIT_PRO_RATA,
)
from custom_components.be_electricity_prices.providers import EXTRACTORS, frank
from custom_components.be_electricity_prices.providers._rates import SpotMonthlyRates
from custom_components.be_electricity_prices.providers.base import (
    ExtractorError,
    SupplierSnapshot,
)
from custom_components.be_electricity_prices.providers.frank import (
    _matches_suffix,
    parse_snapshot,
)
from tests import fixture_text

# A Frank card laid out with no text cached took up to 55 s on a loaded
# Raspberry Pi 4, too close to the suite's 60 s.
pytestmark = pytest.mark.timeout(180)

# The November 2026 cards: (fixture, contract, factor and base the energy
# formula prints, the subscription a month, the feed-in base), all in c/kWh.
_NOVEMBER = (
    ("frank_variable_nov.pdf", "frank_variable", 0.1068, 1.500, 4.17, -1.150),
    ("frank_variable_jn_nov.pdf", "frank_variable_jn", 0.1050, 1.200, 0.00, -2.000),
    (
        "frank_variable_plus_nov.pdf",
        "frank_variable_plus",
        0.10100,
        0.485,
        2.00,
        -1.150,
    ),
)


def _snap(name: str, contract: str) -> SupplierSnapshot:
    return parse_snapshot(
        fixture_text(name, layout=True), f"test://{name}", contract, "november 2026"
    )


@pytest.mark.parametrize(
    ("name", "contract", "factor", "base", "fee", "feed_in"), _NOVEMBER
)
def test_a_variable_card_is_priced_on_the_month_s_index(
    name: str, contract: str, factor: float, base: float, fee: float, feed_in: float
) -> None:
    """(factor x Belpex RLP_BE + base) x 1,06 in c/kWh, on the index in
    EUR/MWh: the leg takes factor x 1,06 x 10 and base x 1,06 / 100, on the
    column-weighted index Frank publishes. The Plus card prints a stray
    bracket after its factor, "(0,10100] x Belpex RLP_BE + 0,485)". The
    feed-in is the month's solar index less the base, with no VAT."""
    snap = _snap(name, contract)
    energy = snap.energy
    assert isinstance(energy, SpotMonthlyRates)
    assert energy.factor == pytest.approx(factor * 1.06 * 10.0)
    assert energy.base == pytest.approx(base * 1.06 / 100.0)
    assert energy.yearly_fixed_fee == pytest.approx(fee * 12.0)
    assert energy.rlp_indexed
    assert energy.rlp_blend == "columns"
    inj = snap.injection
    assert inj is not None
    assert inj.spp_indexed
    assert (inj.factor, inj.base) == pytest.approx((1.0, feed_in / 100.0))
    assert set(snap.dsos) >= set(FLUVIUS_KEYS)
    assert snap.valid_until == date(2026, 11, 30)
    assert snap.taxes.card_vat_rate == 0.06


def test_the_cards_estimates_sit_on_one_index() -> None:
    """Each card prints its prices at VNR's estimate of the month's indices,
    the same for every tier: read back through the coefficients, the three
    energy figures land on one index, 180,03 EUR/MWh, and the three feed-in
    figures on one solar index, 93,78, which is what credits the feed-in
    while the solar profile is not held."""
    estimate = re.compile(
        r"Verwacht volgens methode VNR voor november 2026 \(EURct/kWh\) ([\d,]+)"
    )
    rlp: set[float] = set()
    spp: set[float] = set()
    for name, contract, *_ in _NOVEMBER:
        text = fixture_text(name, layout=True)
        energy_c, feed_in_c = (
            float(v.replace(",", ".")) for v in estimate.findall(" ".join(text.split()))
        )
        snap = parse_snapshot(text, "test://", contract)
        energy, inj = snap.energy, snap.injection
        assert isinstance(energy, SpotMonthlyRates)
        assert inj is not None
        assert inj.factor is not None
        assert inj.base is not None
        rlp.add(round((energy_c / 100.0 - energy.base) / energy.factor * 1000.0, 2))
        spp.add(round((feed_in_c / 100.0 - inj.base) / inj.factor * 1000.0, 2))
        assert inj.current == pytest.approx(feed_in_c / 100.0)
    assert rlp == {180.03}
    assert spp == {93.78}


def test_a_card_crediting_the_feed_in_per_hour_is_read_per_hour() -> None:
    """Up to February 2025 the variable cards credited the feed-in on the
    hour's price, "(0,1 x BELPEX per uur -1,150)", as the dynamic cards do: a
    formula with no estimate, which the engine prices hour by hour."""
    snap = parse_snapshot(
        fixture_text("frank_variable_feb25.pdf", layout=True),
        "test://",
        "frank_variable",
        "februari 2025",
    )
    inj = snap.injection
    assert inj is not None
    assert inj.current is None
    assert not inj.spp_indexed
    assert (inj.factor, inj.base) == pytest.approx((1.0, -0.0115))
    assert isinstance(snap.energy, SpotMonthlyRates)
    assert snap.valid_until == date(2025, 2, 28)


def test_one_product_s_card_is_not_read_as_the_other_s() -> None:
    """A dynamic card priced as a variable one would bill the hour's formula
    on the month's index, and the other way round: each reader refuses the
    other's formula."""
    with pytest.raises(ExtractorError, match="variable energy formula"):
        parse_snapshot(
            fixture_text("frank_dynamic_jn_nov.pdf", layout=True),
            "test://",
            "frank_variable_jn",
        )
    with pytest.raises(ExtractorError, match="energy formula"):
        parse_snapshot(
            fixture_text("frank_variable_jn_nov.pdf", layout=True),
            "test://",
            "frank_dynamic_jn",
        )


_NOV_JN = "test://frank-variable-jn-nov"
_OCT_JN = "test://frank-variable-jn-oct"


def _served_jn(monkeypatch: pytest.MonkeyPatch, nov: str) -> AsyncMock:
    """The November variable JN card as the current one, and the October
    card it names, which prints the subscription the waiver ends on."""

    async def resolve(
        _session: Any, contract: str, target_month: date | None = None
    ) -> tuple[str, str]:
        assert contract == "frank_variable_jn"
        if target_month is None:
            return _NOV_JN, "november 2026"
        assert target_month == date(2026, 10, 1)
        return _OCT_JN, "oktober 2026"

    async def text(_session: Any, url: str) -> str:
        if url == _NOV_JN:
            return nov
        return fixture_text("frank_variable_jn_oct.pdf", layout=True)

    tracked = AsyncMock(side_effect=resolve)
    monkeypatch.setattr(frank, "_resolve_pdf_url", tracked)
    monkeypatch.setattr(frank, "fetch_pdf_text_layout", text)
    monkeypatch.setattr(frank, "_NAMED_FEES", {})
    return tracked


async def test_the_variable_jn_waiver_bills_the_card_it_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The November 2026 variable JN card prints its subscription as 0,00
    for a contract's first twelve months, after which "gelden de
    abonnementskosten volgens de tariefkaart Variabel - JN - Oktober 2026":
    that card's 2,33 a month, billed and credited back over the first year,
    as on the dynamic JN card."""
    _served_jn(monkeypatch, fixture_text("frank_variable_jn_nov.pdf", layout=True))
    snap = await frank.fetch(None, "frank_variable_jn", REGION_FLANDERS)  # type: ignore[arg-type]
    assert snap.energy.yearly_fixed_fee == pytest.approx(27.96)
    assert snap.welcome_credit_eur == pytest.approx(27.96)
    assert snap.welcome_credit_kind == WELCOME_CREDIT_PRO_RATA


async def test_a_waiver_naming_the_other_product_s_card_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The subscription a waiver ends on is read off the card it names, and
    a variable card naming a dynamic one names a card of another price."""
    nov = fixture_text("frank_variable_jn_nov.pdf", layout=True)
    text = re.sub(r"tariefkaart\s+Variabel", "tariefkaart Dynamisch", nov)
    assert text != nov, "the fixture stopped naming the card"
    _served_jn(monkeypatch, text)
    with pytest.raises(ExtractorError, match="waives its subscription"):
        await frank.fetch(None, "frank_variable_jn", REGION_FLANDERS)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "suffix"),
    [
        ("Frank Energie Tariefkaart Elektriciteit Variabel November 2026.pdf", None),
        ("Frank Energie Tariefkaart Elektriciteit Variabel HV November 2026.pdf", "HV"),
        (
            "Frank Energie Tariefkaart Elektriciteit Variabel HV Oktober 2026 (2).pdf",
            "HV",
        ),
        ("Frank Energie Tariefkaart Variabel HV September 2026.pdf", "HV"),
        (
            "Frank Energie Tariefkaart Elektriciteit Variabel VT  Augustus 2026.pdf",
            "VT",
        ),
        ("Frank Energie Tariefkaart Elektricteit Variabel VT Februari 2026.pdf", "VT"),
        ("Frank Energie Tariefkaart Elektriciteit Variabel SL November 2026.pdf", "SL"),
        ("Frank Energie Tariefkaart Variabel Slim April 2026.pdf", "SL"),
        ("Frank Energie Tariefkaart Elektriciteit Variabel JN November 2026.pdf", "JN"),
        (
            "Frank Energie Tariefkaart Elektriciteit Variabel Plus November 2026.pdf",
            "Plus",
        ),
    ],
)
def test_each_variable_file_name_maps_to_its_tier(
    name: str, suffix: str | None
) -> None:
    """Names as the CMS holds them: the "Elektriciteit" token missing (all of
    September 2026) or misspelt (February 2026), a double space, a re-upload's
    "(2)". Each maps to its own tier and to no dynamic one."""
    tiers = [t[2] for t in frank._VARIABLE_TIERS]
    assert [s for s in tiers if _matches_suffix(name, s, "Variabel")] == [suffix]
    assert not any(_matches_suffix(name, t[2]) for t in frank._TIERS)


def test_other_files_sharing_the_word_are_no_tier_s() -> None:
    """The business card "Variabel JN Pro" came out beside the household JN
    card in November 2026, a file naming no month is no month's card, and a
    dynamic card is no variable tier's."""
    for name in (
        "Frank Energie Tariefkaart Elektriciteit Variabel JN Pro November 2026.pdf",
        "Frank Energie Tariefkaart Elektriciteit Variabel June 2025.pdf",
        "Frank Energie Tariefkaart Dynamisch HV November 2026.pdf",
    ):
        assert not any(
            _matches_suffix(name, t[2], "Variabel") for t in frank._VARIABLE_TIERS
        ), name


@pytest.mark.parametrize(
    ("name", "product"),
    [
        (
            "Frank Energie Tariefkaart Elektriciteit Variabel June Mei 2025.pdf",
            "Variabel",
        ),
        (
            "Frank Energie Tariefkaart Elektriciteit Variabel June Mei 2025 (4).pdf",
            "Variabel",
        ),
        (
            "Frank Energie Tariefkaart Elektriciteit Dynamisch June Februari 2025.pdf",
            "Dynamisch",
        ),
        (
            "Frank Energie Tariefkaart Elektriciteit Dynamisch June Mei 2025.pdf",
            "Dynamisch",
        ),
    ],
)
def test_june_was_jn_s_earlier_name(name: str, product: str) -> None:
    """JN was named June until May 2025, in both products: its May 2025 cards
    print the very terms of its first JN card in June, so they are JN's and no
    other tier's."""
    tiers = frank._VARIABLE_TIERS if product == "Variabel" else frank._TIERS
    assert [t[2] for t in tiers if _matches_suffix(name, t[2], product)] == ["JN"]


async def test_the_superseded_february_card_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The variable June card first put up for February 2025 printed the
    standard tier's formula; its replacement of 6 February carries no month
    in its name, so a month lookup sees only the withdrawn one. It is left
    out, while May 2025 reads its newest June card."""
    rows = [
        {
            "originalFilename": name,
            "url": f"https://cdn/{i}.pdf",
            "_createdAt": created,
        }
        for i, (name, created) in enumerate(
            (
                (
                    "Frank Energie Tariefkaart Elektriciteit Variabel June Februari 2025.pdf",
                    "2025-01-31T09:51:00Z",
                ),
                (
                    "Frank Energie Tariefkaart Elektriciteit Variabel June Mei 2025.pdf",
                    "2025-05-07T09:14:00Z",
                ),
                (
                    "Frank Energie Tariefkaart Elektriciteit Variabel June Mei 2025 (4).pdf",
                    "2025-05-07T09:15:00Z",
                ),
            )
        )
    ]

    async def query(_session: Any, q: str) -> list[dict[str, str]]:
        # As the CMS filters: every name the query asks for, gas excluded.
        words = [w for w in re.findall(r'match "\*([^*]+)\*"', q) if w != "Gas"]
        return [
            r
            for r in rows
            if all(w.casefold() in r["originalFilename"].casefold() for w in words)
        ]

    monkeypatch.setattr(frank, "_sanity_query", query)
    with pytest.raises(ExtractorError, match="no Frank Energie tariff card"):
        await frank._resolve_pdf_url(None, "frank_variable_jn", date(2025, 2, 1))  # type: ignore[arg-type]
    found = await frank._resolve_pdf_url(None, "frank_variable_jn", date(2025, 5, 1))  # type: ignore[arg-type]
    assert found == ("https://cdn/2.pdf", "mei 2025")


async def test_a_variable_tier_resolves_on_its_own_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lookup asks the CMS for the variable cards, and the tier's own
    newest card wins over the business card uploaded after it."""
    asked: list[str] = []
    rows = [
        {
            "originalFilename": f"Frank Energie Tariefkaart Elektriciteit {name}.pdf",
            "url": url,
            "_createdAt": created,
        }
        for name, url, created in (
            ("Variabel JN November 2026", "https://cdn/jn.pdf", "2026-10-30T10:00:00Z"),
            (
                "Variabel JN Pro November 2026",
                "https://cdn/pro.pdf",
                "2026-10-30T11:00:00Z",
            ),
            ("Variabel JN Oktober 2026", "https://cdn/oct.pdf", "2026-09-30T10:00:00Z"),
        )
    ]

    async def query(_session: Any, q: str) -> list[dict[str, str]]:
        asked.append(q)
        return rows

    monkeypatch.setattr(frank, "_sanity_query", query)
    found = await frank._resolve_pdf_url(None, "frank_variable_jn")  # type: ignore[arg-type]
    assert found == ("https://cdn/jn.pdf", "november 2026")
    assert 'match "*Variabel*"' in asked[0]
    assert "Dynamisch" not in asked[0]


async def test_the_probe_watches_the_contract_s_own_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []

    async def query(_session: Any, q: str) -> list[dict[str, str]]:
        asked.append(q)
        return [{"_createdAt": "2026-10-30T10:00:00Z"}]

    monkeypatch.setattr(frank, "_sanity_query", query)
    for contract in ("frank_variable_hv", "frank_dynamic_hv"):
        key = await frank.probe(None, contract, REGION_FLANDERS)  # type: ignore[arg-type]
        assert key == "2026-10-30T10:00:00Z"
    assert 'match "*Variabel*"' in asked[0]
    assert 'match "*Dynamisch*"' in asked[1]


async def test_a_past_month_reads_its_own_variable_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A closed month reads the card of that month, here the per-hour feed-in
    of February 2025, and a card naming another month is no month's."""

    async def resolve(
        _session: Any, contract: str, target_month: date | None = None
    ) -> tuple[str, str]:
        assert contract == "frank_variable"
        return "test://frank-variable-feb25", "februari 2025"

    async def text(_session: Any, _url: str) -> str:
        return fixture_text("frank_variable_feb25.pdf", layout=True)

    monkeypatch.setattr(frank, "_resolve_pdf_url", resolve)
    monkeypatch.setattr(frank, "fetch_pdf_text_layout", text)
    snap = await frank.fetch_for_month(
        None,  # type: ignore[arg-type]
        "frank_variable",
        REGION_FLANDERS,
        date(2025, 2, 1),
    )
    assert snap is not None
    assert snap.valid_until == date(2025, 2, 28)
    assert snap.injection is not None
    assert snap.injection.current is None
    other = await frank.fetch_for_month(
        None,  # type: ignore[arg-type]
        "frank_variable",
        REGION_FLANDERS,
        date(2025, 3, 1),
    )
    assert other is None


def test_the_variable_tiers_are_registered_as_month_indexed() -> None:
    """Six tiers, kind spot_monthly so the config flow collects the key the
    index needs, Flanders only, and VT withdrawn with its dynamic twin on 1
    November 2026: no November card, no link on the terms page."""
    contracts = {c.id: c for c in EXTRACTORS["frank"].contracts}
    variable = {cid for cid in contracts if cid.startswith("frank_variable")}
    assert variable == {t[0] for t in frank._VARIABLE_TIERS}
    for cid in variable:
        assert contracts[cid].kind == "spot_monthly"
        assert contracts[cid].regions == frozenset({REGION_FLANDERS})
        assert not contracts[cid].quarter_hourly_option
    assert {cid for cid in variable if contracts[cid].withdrawn} == {
        "frank_variable_vt"
    }
    assert contracts["frank_variable_vt"].withdrawn == date(2026, 11, 1)
