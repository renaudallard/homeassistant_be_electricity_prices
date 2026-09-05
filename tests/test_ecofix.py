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

"""Ecofix PDF extractor tests against May 2026 fixtures."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import pytest

from custom_components.be_electricity_prices.const import FLUVIUS_KEYS
from custom_components.be_electricity_prices.providers import EXTRACTORS
from custom_components.be_electricity_prices.providers.base import (
    DynamicRates,
    ExtractorError,
    VariableRates,
)
from custom_components.be_electricity_prices.providers.ecofix import (
    _dynamic_formula_match,
    _extract_fee_and_flanders_renewables,
    _extract_flanders_dsos,
    _extract_injection,
    _extract_publication,
    _head_probe_ids,
    parse_snapshot,
)
from tests import fixture_text

_MOTION_ONLINE = "ecofix_motion_online.pdf"
_MOTION = "ecofix_motion.pdf"
_FLEXY = "ecofix_flexy.pdf"


def _layout(name: str) -> str:
    return fixture_text(name, layout=True)


# ---- registry ---------------------------------------------------------------


def test_ecofix_is_registered() -> None:
    assert "ecofix" in EXTRACTORS
    extractor = EXTRACTORS["ecofix"]
    assert extractor.label == "Ecofix"
    assert {c.id for c in extractor.contracts} == {
        "ecofix_motion",
        "ecofix_motion_online",
        "ecofix_flexy",
        "ecofix_flexy_online",
    }
    # Brussels is not on any current Ecofix card; the registry must
    # advertise Flanders + Wallonia only so config-flow doesn't offer
    # Ecofix to Brussels households where every fetch would fail.
    for contract in extractor.contracts:
        assert "brussels" not in contract.regions


def test_afname_anchor_does_not_reach_injection_formula() -> None:
    # The Afname match is tempered so it can't cross the Injectie label:
    # if the consumption formula is reworded/absent, the Afname anchor
    # must fail rather than bind the injection formula to consumption.
    txt = _layout(_MOTION)
    assert _dynamic_formula_match(txt, "Afname") is not None
    broken = txt.replace("x Belpex 15M) + 1,1020", "x BelpexFOO) + 1,1020", 1)
    # Consumption formula gone -> Afname anchor returns None (clean miss),
    # NOT the injection block's formula further down the page.
    assert _dynamic_formula_match(broken, "Afname") is None


# ---- Motion Online (dynamic, low yearly fee) --------------------------------


def test_motion_online_energy_formula() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True
    assert snap.energy.yearly_fixed_fee == pytest.approx(10.0)
    # PDF: (0.1010 x Belpex 15M) + 0,9  c€/kWh ex-VAT, 6% VAT applied.
    # factor_pdf * 1.06 * 10 = 0.1010 * 10.6 = 1.0706
    # base_pdf * 1.06 / 100  = 0.9   * 0.0106 = 0.009540
    assert snap.energy.factor == pytest.approx(1.0706, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.00954, rel=1e-4)
    # At spot 100 EUR/MWh = 0.10 EUR/kWh, all-in energy ~0.11660 EUR/kWh.
    assert snap.energy.factor * 0.10 + snap.energy.base == pytest.approx(0.1166)


def test_motion_online_injection() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    inj = snap.injection
    assert inj is not None
    # PDF: (0.0884 x Belpex 15M) - 0.5000 c€/kWh ex-VAT.
    # Injection is VAT-exempt for residential, so no VAT applied.
    assert inj.factor == pytest.approx(0.884, rel=1e-4)
    assert inj.base == pytest.approx(-0.005, rel=1e-4)
    # Indicative monthly average printed on the card.
    assert inj.current == pytest.approx(0.0483)


def test_motion_online_publication() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert snap.publication_label == "2026-05"
    # Last day of May 2026: Sunday 31st.
    assert snap.valid_until == date(2026, 5, 31)


def test_motion_online_taxes_flanders() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    assert snap.taxes.federal_excise == pytest.approx(0.0503288)
    assert snap.taxes.energy_contribution == pytest.approx(0.0020417)
    assert snap.taxes.flanders_renewables == pytest.approx(0.016)
    # Flanders pays no Wallonia connection fee or Wallonia renewables.
    assert snap.taxes.wallonia_renewables == 0.0
    assert snap.taxes.region_connection_fee == 0.0
    assert snap.taxes.vat_rate == 0.0


def test_motion_online_taxes_wallonia() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    assert snap.taxes.wallonia_renewables == pytest.approx(0.0305)
    assert snap.taxes.region_connection_fee == pytest.approx(0.00075)
    assert snap.taxes.flanders_renewables == 0.0


def test_motion_online_flanders_dsos() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "test://mo"
    )
    expected_keys = set(FLUVIUS_KEYS)
    assert set(snap.dsos) == expected_keys
    # Issue reporter is on Fluvius Kempen (= fluvius_iveka in the
    # integration's DSO key namespace).
    iveka = snap.dsos["fluvius_iveka"]
    assert iveka.distribution_single == pytest.approx(0.0633708)
    assert iveka.distribution_exclusive_night == pytest.approx(0.056606)
    assert iveka.capacity_eur_per_kw_year == pytest.approx(59.5794)
    assert iveka.data_management_per_year == pytest.approx(18.92)
    # Analog-meter prosumer rate is attached to every Flanders overlay,
    # digital rows included. Nothing filters it by meter type: the only gate
    # is _compensation_kva, which bills the prosumer fee on Walloon
    # compensation entries alone, so on a Flemish overlay it is never read.
    assert iveka.prosumer_eur_per_kva_year == pytest.approx(67.79)


def test_dynamic_injection_missing_formula_is_fatal() -> None:
    # The injection Belpex 15M formula is mandatory on every dynamic card;
    # a miss must raise rather than silently zeroing the spot-indexed
    # feed-in credit (this was the only injection path that returned None).
    text = _layout(_MOTION).replace("Injectie", "XXX")
    with pytest.raises(ExtractorError, match="Belpex 15M formula missing"):
        _extract_injection(text, "dynamic")


def test_flanders_data_management_column_follows_metering_regime() -> None:
    # The Fluvius row carries two data-management columns: per-kwartier
    # (quarter-hourly, billed to dynamic contracts) and monthly/yearly
    # (billed to Flexy). They are equal on today's cards, so craft a row
    # where they diverge and assert each kind reads its own column.
    text = (
        "Vlaams gewest Digitale meter\n"
        "Fluvius Antwerpen 52,3679 5,35329 4,81301 11,11 22,22\n"
        "Vlaams gewest Analoge meter\n"
        "Fluvius Antwerpen 52,3679 5,35329 4,81301 11,11 33,33\n"
        "Ecofix Gas & Power\n"
    )
    dynamic = _extract_flanders_dsos(text, "dynamic")
    variable = _extract_flanders_dsos(text, "variable")
    assert next(iter(dynamic.values())).data_management_per_year == pytest.approx(11.11)
    assert next(iter(variable.values())).data_management_per_year == pytest.approx(
        22.22
    )


def test_motion_online_wallonia_dsos() -> None:
    snap = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "wallonia", "test://mo"
    )
    assert set(snap.dsos) == {"aieg", "aiesh", "ores", "resa", "rew"}
    aieg = snap.dsos["aieg"]
    assert aieg.distribution_single == pytest.approx(0.1087)
    assert aieg.distribution_peak == pytest.approx(0.1205)
    assert aieg.distribution_offpeak == pytest.approx(0.0666)
    assert aieg.distribution_pic == pytest.approx(0.1508)
    assert aieg.distribution_medium == pytest.approx(0.0982)
    assert aieg.distribution_eco == pytest.approx(0.0456)
    assert aieg.transport == pytest.approx(0.0274)
    assert aieg.data_management_per_year == pytest.approx(19.49)
    assert aieg.prosumer_eur_per_kva_year == pytest.approx(81.03)


# ---- Motion (dynamic, full yearly fee + Ecofix Digi) ------------------------


def test_motion_energy_formula() -> None:
    snap = parse_snapshot("ecofix_motion", _layout(_MOTION), "flanders", "test://m")
    assert isinstance(snap.energy, DynamicRates)
    assert snap.energy.quarter_hourly is True
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)
    # PDF: (0.1000 x Belpex 15M) + 1.1020  c€/kWh ex-VAT.
    # factor = 0.1000 * 1.06 * 10 = 1.0600
    # base   = 1.1020 * 1.06 / 100 = 0.0116812
    assert snap.energy.factor == pytest.approx(1.06, rel=1e-4)
    assert snap.energy.base == pytest.approx(0.0116812, rel=1e-4)


def test_motion_publication_and_renewables_match_motion_online() -> None:
    """Motion and Motion Online ship the same monthly DSO + tax overlay
    even though the energy formula and yearly fee differ. Pin the
    parser so a future supplier-side divergence raises a real test
    failure rather than silently changing the bill.
    """
    snap_m = parse_snapshot("ecofix_motion", _layout(_MOTION), "flanders", "x")
    snap_mo = parse_snapshot(
        "ecofix_motion_online", _layout(_MOTION_ONLINE), "flanders", "x"
    )
    assert snap_m.taxes.flanders_renewables == snap_mo.taxes.flanders_renewables
    assert snap_m.taxes.federal_excise == snap_mo.taxes.federal_excise
    assert snap_m.taxes.energy_contribution == snap_mo.taxes.energy_contribution
    assert snap_m.publication_label == snap_mo.publication_label
    assert snap_m.dsos["fluvius_iveka"] == snap_mo.dsos["fluvius_iveka"]


# ---- Flexy (variable, RLP-M monthly) ----------------------------------------


def test_flexy_is_variable_with_indicative_monthly_rate() -> None:
    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "flanders", "test://f")
    assert isinstance(snap.energy, VariableRates)
    # PDF prints "Maandprijs: 11,81 11,81 11,81 11,81" - same value
    # across mono / peak / off-peak / exclusive night today.
    assert snap.energy.current == pytest.approx(0.1181)
    assert snap.energy.peak == pytest.approx(0.1181)
    assert snap.energy.offpeak == pytest.approx(0.1181)
    assert snap.energy.exclusive_night == pytest.approx(0.1181)
    assert snap.energy.yearly_fixed_fee == pytest.approx(60.0)
    assert snap.energy.formula is not None and "BELPEX-RLP-M" in snap.energy.formula


def test_publication_scan_skips_colliding_version_token() -> None:
    # The product name prints on the line above the month; a future
    # "Versie 2026" header token must not shadow the real month line.
    label, valid = _extract_publication("Ecofix Motion Online Versie 2026\nMei 2026\n")
    assert label == "2026-05"
    assert valid == date(2026, 5, 31)


def test_missing_wallonia_connection_fee_is_fatal() -> None:
    # The Walloon connection fee is mandatory; a miss must raise rather
    # than silently zero it.
    text = _layout(_FLEXY).replace("Aansluitingsvergoeding", "XXX")
    with pytest.raises(ExtractorError, match="connection fee"):
        parse_snapshot("ecofix_flexy", text, "wallonia", "test://f")


def test_flexy_injection_carries_the_spp_formula() -> None:
    """The card states "Injectie: (BELPEX-SPP-M * 0,0884) - 0,5000" and says
    the settlement uses "de index die van toepassing is tijdens de periode
    waarvoor je wordt gefactureerd". spp_indexed routes the coefficients to
    the delivery month's weighted mean and keeps them off the hourly spot;
    the printed Maandprijs stays as the keyless fallback."""
    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "wallonia", "test://f")
    inj = snap.injection
    assert inj is not None
    assert inj.current == pytest.approx(0.0432)
    # c/kWh per EUR/MWh of index -> a x10 factor onto a EUR/kWh spot, /100 base.
    assert inj.factor == pytest.approx(0.884)
    assert inj.base == pytest.approx(-0.005)
    assert inj.spp_indexed is True
    assert inj.formula is not None and "BELPEX-SPP-M" in inj.formula


def test_flexy_printed_figure_is_two_months_stale() -> None:
    """Why the formula is worth parsing. Invert the Mei 2026 card's printed
    4,32 c/kWh through its own coefficients and the index comes out at 54,52
    EUR/MWh, which is MARCH's SPP-weighted mean, not May's."""
    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "wallonia", "test://f")
    inj = snap.injection
    assert inj is not None and inj.factor is not None and inj.base is not None
    assert inj.current is not None
    implied_index = (inj.current - inj.base) / inj.factor
    assert implied_index == pytest.approx(0.05452, abs=1e-5)
    # April's own index credits less than half of what the card printed.
    assert inj.factor * 0.029166 + inj.base == pytest.approx(0.02078, abs=1e-5)


def test_flexy_injection_is_never_priced_per_hour() -> None:
    """Month coefficients on a variable card. The old comment refused to emit
    them at all for fear of exactly this, and was right to: without the flag
    the engine would credit whatever the current slot costs."""
    from custom_components.be_electricity_prices.injection import (
        _injection_is_spot_formula,
    )

    snap = parse_snapshot("ecofix_flexy", _layout(_FLEXY), "wallonia", "test://f")
    assert snap.injection is not None
    assert _injection_is_spot_formula(snap.injection, snap.energy) is False


def test_flexy_renewables_survives_number_before_verbruik_layout() -> None:
    # The July 2026 Flexy card moved the Vlaanderen renewable onto its own
    # line ABOVE the "Verbruik" label ("1,60\nVerbruik" instead of
    # "Verbruik 1,60"), which raised "Vlaanderen renewables not found" and
    # took the whole card offline. Both orders must yield 0.016 EUR/kWh.
    base = _layout(_FLEXY)
    assert "Verbruik 1,60" in base
    snap_same = parse_snapshot("ecofix_flexy", base, "flanders", "test://f")
    assert snap_same.taxes.flanders_renewables == pytest.approx(0.016)

    reflowed = base.replace("Verbruik 1,60", "1,60\nVerbruik", 1)
    snap_reflow = parse_snapshot("ecofix_flexy", reflowed, "flanders", "test://f")
    assert snap_reflow.taxes.flanders_renewables == pytest.approx(0.016)


# ---- ORES sub-area drift detection -----------------------------------------


def test_ores_subarea_drift_is_rejected() -> None:
    """The Wallonia card lists 9 ORES sub-areas with identical numbers;
    the parser collapses them to one ``ores`` overlay. If a future card
    splits sub-areas (different numbers per row), the parser must raise
    rather than silently bill at the first sub-area's rate.
    """
    text = _layout(_MOTION_ONLINE)
    # Tweak one ORES row so its monohoraire rate diverges from the rest.
    bumped = text.replace(
        "ORES (Namur) 11,98 13,27 7,39",
        "ORES (Namur) 99,99 13,27 7,39",
        1,
    )
    with pytest.raises(ExtractorError, match="ORES sub-area .* diverged"):
        parse_snapshot("ecofix_motion_online", bumped, "wallonia", "x")


def test_unknown_contract_raises() -> None:
    with pytest.raises(ExtractorError, match="unknown Ecofix contract"):
        parse_snapshot("bogus", _layout(_MOTION_ONLINE), "wallonia", "x")


def test_flexy_swapped_fee_and_renewable_columns_are_rejected() -> None:
    """The variable branch reads the fee and the Flemish renewable from two
    SEPARATE positional anchors, so a reflowed card silently swaps them. The
    dynamic branch below cannot: it takes max/min of one slice. Measured on
    this fixture the swap bills a 1,60 EUR/jaar fee with 0,6000 EUR/kWh of
    renewables, which is +1.985,60 EUR a year at 3.500 kWh, so it has to
    fail loudly instead."""
    text = _layout(_FLEXY)
    fee, renewable = _extract_fee_and_flanders_renewables(text, "variable")
    assert fee == pytest.approx(60.0)
    assert renewable == pytest.approx(0.016)

    swapped = text.replace("60,00 meter Piekuren", "1,60 meter Piekuren", 1)
    if "Verbruik 1,60" in swapped:
        swapped = swapped.replace("Verbruik 1,60", "Verbruik 60,00", 1)
    else:
        swapped = swapped.replace("1,60\nVerbruik", "60,00\nVerbruik", 1)
    assert swapped != text, "the swap did not change the card"
    with pytest.raises(ExtractorError, match="columns swapped"):
        _extract_fee_and_flanders_renewables(swapped, "variable")


def test_flexy_online_reads_the_flexy_card_unchanged() -> None:
    """Flexy Online is the same product sold online, so it parses on Flexy's
    own branch: only the dispatch and the URL differ. Nothing here should
    need a fixture of its own, and that is the point."""
    text = _layout(_FLEXY)
    as_flexy = parse_snapshot("ecofix_flexy", text, "flanders")
    as_online = parse_snapshot("ecofix_flexy_online", text, "flanders")
    assert as_online.energy == as_flexy.energy
    assert as_online.taxes == as_flexy.taxes
    assert as_online.contract == "ecofix_flexy_online"


# ---- discover() fallback -----------------------------------------------------


class _HeadResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}

    async def __aenter__(self) -> "_HeadResponse":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _StubHeadSession:
    """Minimal session: returns 200 for the URLs in ``ok``, else 404."""

    def __init__(self, ok: set[str]) -> None:
        self._ok = ok

    def head(self, url: str, *_args: Any, **_kwargs: Any) -> _HeadResponse:
        return _HeadResponse(200 if url in self._ok else 404)


def test_head_probe_fallback_returns_every_contract_whose_url_200s() -> None:
    """The listing is the discovery surface now; this is what runs when the
    listing itself is unreachable, so it still has to hold."""
    base = "https://portal.ecofixgp.be/docs/prices/current"
    session = _StubHeadSession(
        ok={
            f"{base}/EL_Ecofix_Motion_NL.pdf",
            f"{base}/EL_Ecofix_Motion_Online_NL.pdf",
            f"{base}/EL_Ecofix_Flexy_NL.pdf",
        }
    )
    discovered = asyncio.run(_head_probe_ids(session))  # type: ignore[arg-type]
    assert discovered == {"ecofix_motion", "ecofix_motion_online", "ecofix_flexy"}


def test_head_probe_fallback_drops_a_retired_product() -> None:
    base = "https://portal.ecofixgp.be/docs/prices/current"
    # Simulate Ecofix retiring "Motion" while keeping the other two.
    session = _StubHeadSession(
        ok={
            f"{base}/EL_Ecofix_Motion_Online_NL.pdf",
            f"{base}/EL_Ecofix_Flexy_NL.pdf",
        }
    )
    discovered = asyncio.run(_head_probe_ids(session))  # type: ignore[arg-type]
    assert discovered == {"ecofix_motion_online", "ecofix_flexy"}
