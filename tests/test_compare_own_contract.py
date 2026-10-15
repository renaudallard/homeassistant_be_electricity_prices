"""Quoting the household's own contract on the comparison page.

The picker offers the entry's own contract so a household can ask what the
same contract costs on another meter or solar regime. Quoted against itself
with nothing changed, the two sides are one bill and the difference is zero.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.be_electricity_prices.meter_daily import MeasuredKwh
from tests import make_entry
from tests.test_options_flow import (
    _compare_placeholders,
    _real_coordinator,
    _stub_snapshot,
)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        # Signed at 0,12 where today's card prints 0,18.
        {"contract_start_date": "2025-06-01", "manual_energy_single": 0.12},
    ],
)
async def test_the_own_contract_against_itself_costs_nothing_more(
    hass: HomeAssistant, freezer: Any, extra: dict[str, Any]
) -> None:
    """The target side is the contract the household signed, at the rates it
    locked in, not today's card for a new signer: 210 EUR a year apart at
    3500 kWh before."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = make_entry(**extra)
    entry.add_to_hass(hass)
    card = _stub_snapshot("eneco", "power_fix", 0.18)
    entry.runtime_data = _real_coordinator(hass, entry, card)
    p = await _compare_placeholders(
        hass,
        entry,
        supplier="eneco",
        contract="power_fix",
        target_snapshot=card,
        meter="mono",
    )
    assert p["current_annual"] == p["compare_annual"]
    assert float(p["delta_annual"]) == 0.0
    assert float(p["delta_ytd"]) == 0.0


async def test_a_held_contract_is_not_granted_a_new_welcome_credit(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Signed in 2024, so the first year is long over: the 100 EUR the card
    grants a new signer is not the household's to collect again."""
    freezer.move_to("2026-04-29 13:00:00+02:00")
    entry = make_entry(contract_start_date="2024-01-01")
    entry.add_to_hass(hass)
    card = replace(_stub_snapshot("eneco", "power_fix", 0.18), welcome_credit_eur=100.0)
    entry.runtime_data = _real_coordinator(hass, entry, card)
    p = await _compare_placeholders(
        hass,
        entry,
        supplier="eneco",
        contract="power_fix",
        target_snapshot=card,
        meter="mono",
    )
    assert p["current_annual"] == p["compare_annual"]
    assert float(p["delta_annual"]) == 0.0


async def test_a_per_slot_feed_in_is_not_baked_on_the_own_contract(
    hass: HomeAssistant, freezer: Any
) -> None:
    """Bolt bills its feed-in per slot while its energy rides the delivery
    quarter. With a key the own side gets a month-mean energy leg spliced on,
    and the target side, spliced the same way, was asked whether its credit
    rides a month mean on the spliced card alone. It answered yes and credited
    the month mean, so the contract quoted against itself read a delta."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices.providers.bolt import parse_snapshot
    from tests import fixture_text

    freezer.move_to("2026-04-29 13:00:00+02:00")
    card = parse_snapshot(
        "bolt_variable", fixture_text("bolt_variable.pdf", layout=True), "wallonia"
    )
    entry = make_entry(
        supplier="bolt",
        contract="bolt_variable",
        solar_regime="injection",
        solar_kva=5.0,
        api_key="k",
        injection_kwh="sensor.i",
    )
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, card)
    now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    # A year of day-ahead, dearer in the evening, and of midday export, so
    # the per-slot credit and the month mean are far apart.
    hours = [now - timedelta(hours=h) for h in range(1, 370 * 24)]
    coord._historical_spots = {
        t: 0.15 if 17 <= dt_util.as_local(t).hour <= 21 else 0.05 for t in hours
    }
    coord._spot_cache = {now + timedelta(hours=h): 0.08 for h in range(24)}
    entry.runtime_data = coord
    export = {t: 1.0 if 11 <= dt_util.as_local(t).hour <= 15 else 0.0 for t in hours}

    async def hourly(*_a: Any, side: str = "consumption", **_k: Any) -> Any:
        return export if side == "injection" else None

    async def total(*_a: Any, side: str = "consumption", **_k: Any) -> MeasuredKwh:
        return MeasuredKwh(sum(export.values()) if side == "injection" else 3500.0, 365)

    household = "custom_components.be_electricity_prices.compare_household."
    with (
        patch(household + "_measured_hourly", hourly),
        patch(household + "_measured_kwh", total),
    ):
        p = await _compare_placeholders(
            hass,
            entry,
            supplier="bolt",
            contract="bolt_variable",
            target_snapshot=card,
            meter="mono",
            solar_regime="injection",
        )
    assert p["current_annual"] != "-"
    assert p["current_annual"] == p["compare_annual"]
    assert float(p["delta_annual"]) == 0.0


async def test_the_year_to_date_walks_the_card_before_the_cohort_splice(
    hass: HomeAssistant, freezer: Any
) -> None:
    """The walk behind current_year_cost resolves the cohort per month itself
    and bills a month before the signing month on its own card, or on the
    current one where no archive holds it. The page handed its own row, and
    the own contract quoted on another meter, the current card with the
    signed feed-in leg already laid on, so January to August of an Eneco
    Power Flex One signed in September were credited on the signed formula:
    13,61 EUR under the sensor at 10 kWh exported a day."""
    from datetime import timedelta

    from homeassistant.util import dt as dt_util

    from custom_components.be_electricity_prices import cohort, ytd_cost
    from custom_components.be_electricity_prices.providers._rates import (
        InjectionRates,
    )

    freezer.move_to("2026-10-15 13:00:00+02:00")
    card = replace(
        _stub_snapshot("eneco", "power_fix", 0.18),
        injection=InjectionRates(current=0.05),
    )
    signed = InjectionRates(current=0.02, fixed_for_term=True)
    entry = make_entry(
        solar_regime="injection",
        solar_kva=5.0,
        contract_start_date="2026-09-05",
        api_key="k",
        injection_kwh="sensor.i",
    )
    entry.add_to_hass(hass)
    coord = _real_coordinator(hass, entry, card)
    # The page fetches a year of day-ahead for a feed-in it reads as spot
    # indexed when the entry holds none.
    coord._historical_spots = {dt_util.utcnow() - timedelta(days=1): 0.08}
    entry.runtime_data = coord
    walked: list[Any] = []

    async def walk(
        _hass: Any, _session: Any, _ext: Any, snap: Any, *_a: Any, **_k: Any
    ) -> float:
        walked.append(snap)
        return 100.0

    with (
        patch.object(ytd_cost, "_compute_current_year_cost", walk),
        patch.object(
            cohort,
            "_cohort_legs",
            AsyncMock(return_value=cohort._CohortLegs(None, signed)),
        ),
    ):
        p = await _compare_placeholders(
            hass,
            entry,
            supplier="eneco",
            contract="power_fix",
            target_snapshot=card,
            meter="bi",
            solar_regime="injection",
        )
    assert p["current_ytd"] != "-"
    assert len(walked) == 2
    assert walked[0] is coord._snapshot
    assert all(snap.injection != signed for snap in walked)
